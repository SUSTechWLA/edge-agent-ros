"""Promote one preauthorized read-only service projection for an evidenced, undispatched capability gap."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from planning import match_utterances, normalized
from site_pack import verify
from v2_registry import V2Registry


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def save(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    pending = path.with_suffix(".pending")
    pending.write_bytes(raw)
    os.replace(pending, path)


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request("http://127.0.0.1:8794/agent/" + path, data, {"Content-Type":"application/json"})
    with urlopen(req, timeout=5) as response:
        raw = response.read(1024*1024+1)
    require(len(raw) <= 1024*1024, "agent response exceeds limit")
    return json.loads(raw)


def selection(task, base, admitted):
    require(task["state"] == "BLOCKED" and not task["steps"], "expected an undispatched capability gap")
    gap = next(e for e in task["events"] if e["kind"] == "blocked")
    diagnostic = json.loads(gap["payload"])["diagnostic"]["code"]
    require(diagnostic in ("planner.not_configured", "planner.capability_missing"), "unrelated original failure")
    require(all(key in admitted and admitted[key]["content_digest"] == item["content_digest"] for key,item in base.items()),
            "projection must preserve the complete original pack")
    options = set(admitted) - set(base)
    require(len(options) == 1, "approved site must add exactly one read tool")
    item = admitted[options.pop()]
    spec = item["config_schema"]["properties"]["profile"]["const"]
    require(item["effects"] == "read" and item["kind"] == "tool" and item["class"] == "soft"
            and item["runtime"] == "inproc" and item["entrypoint"] == "agent.read_service_tool:apply"
            and not item["mounts"] and item["network"] == {"ingress": [], "egress": []}
            and len(item["requires"]) == 1 and item["requires"][0]["name"] == spec["service"]
            and item["requires"][0]["type"] == "service"
            and any(c in p["provides"] for p in base.values() for c in item["requires"]),
            "new tool is not a projection of a previously available service")
    cap = spec["legacy"]
    steps = match_utterances(normalized(task["text"]), {cap["id"]:cap}, task["text"])
    require(steps and len(steps) == 1, "original text does not match the approved projection")
    return {"plugin":item["id"], "tool":cap["id"], "capability":cap, "service":spec["service"],
            "arguments":steps[0]["args"], "candidate_digest":item["content_digest"],
            "code_digest":item["provenance"]["code_digest"], "gap_event":gap["event_id"], "gap_code":diagnostic}


def run(args):
    root = args.out
    with (root/"supervisor.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _, base = verify(args.ready_site, args.ready_lock)
        release, approved = verify(args.approved_site, args.approved_lock)
        require(all(not p["site_approval"] and p["effects"] == "read" and p["class"] == "soft"
                    for p in release["plugins"].values()), "site requires external handoff")
        task = api("tasks?id="+args.task)
        proposal = selection(task, base, approved)
        events = root/"events.jsonl"
        prior = {r["phase"]:r for r in (json.loads(s) for s in events.read_text().splitlines())} if events.exists() else {}
        def emit(phase, **details):
            if phase in prior:
                require(all(prior[phase].get(k)==v for k,v in details.items()), "evolution replay differs from prior receipt")
                return
            row = {"phase":phase,"utc":datetime.now(timezone.utc).isoformat(),"task_id":args.task,
                   "trace_id":task["trace_id"],**details}
            with events.open("a",encoding="utf-8") as stream:
                stream.write(json.dumps(row,ensure_ascii=False)+"\n")
                stream.flush()
                os.fsync(stream.fileno())
            prior[phase]=row
        emit("gap_observed", source_event=proposal["gap_event"], code=proposal["gap_code"])
        save(root/"candidate.json",proposal)
        emit("candidate_generated", generator="declarative_service_projection", plugin=proposal["plugin"],
             service=proposal["service"], candidate_digest=proposal["candidate_digest"])
        if args.phase == "propose":
            return proposal
        raw = args.validation.read_bytes()
        require(args.validation_digest == digest(raw), "independent validation digest changed")
        receipt = json.loads(raw)
        settings = json.loads((args.approved_site/"config"/(proposal["service"]+".json")).read_bytes())
        require(receipt.get("passed") is True and receipt.get("schema_version")==1 and receipt.get("checks")
                and receipt.get("candidate_digest")==proposal["candidate_digest"]
                and receipt.get("tool_id")==proposal["tool"]
                and receipt.get("arguments", {"target":receipt.get("query")})==proposal["arguments"]
                and all(receipt.get(k)==settings[k] for k in ("backend_digest","profile_digest")),
                "validation failed or belongs to a different provider, query or release")
        emit("candidate_verified", validation_digest=args.validation_digest, checks=receipt["checks"])
        emit("site_policy_matched", allowed_site_lock=args.approved_lock, effect="read")
        with V2Registry(args.registry) as registry:
            if "release_applied" in prior:
                event = prior["release_applied"]["event_id"]
                require(any(row["descriptor"]["content_digest"]==proposal["candidate_digest"] and not row["draining"]
                            for row in registry.entries()), "previous release is no longer active")
            else:
                before={r["descriptor"]["id"]:r["descriptor"]["content_digest"] for r in registry.entries()}
                wanted={k:v["content_digest"] for k,v in base.items()}
                if before == wanted:
                    result=registry.install_pack(list(approved.values()),args.approved_lock)
                    require(result["admitted"]==[proposal["plugin"]], "unexpected admission")
                    event=result["event_id"]
                else:
                    require(before == {k:v["content_digest"] for k,v in approved.items()},
                            "registry changed before admission")
                    row=registry.db.execute("""SELECT event_id FROM module_bus_events
                        WHERE json_extract(event,'$.code')='release.pack_applied' AND json_extract(event,'$.data.lock')=?
                        AND json_extract(event,'$.data.count')=1 ORDER BY seq DESC LIMIT 1""",(args.approved_lock,)).fetchone()
                    require(row,"original atomic release event unavailable")
                    event=row[0]
            emit("release_applied", event_id=event,candidate_digest=proposal["candidate_digest"])
        deadline=time.monotonic()+18
        while time.monotonic()<deadline:
            catalog=api("capabilities")
            row=next((r for r in catalog["records"] if r["id"]==proposal["tool"] and
                      r["provider"]==proposal["plugin"] and r["source"]=="v2_soft" and r["effects"]=="read"
                      and r.get("availability")=="available" and r.get("binding")),None)
            if row: break
            time.sleep(.25)
        else: raise ValueError("approved projection did not enter the live directory")
        save(root/"catalog-after.json",catalog)
        follow="recover-"+hashlib.sha256((task["id"]+proposal["candidate_digest"]).encode()).hexdigest()[:32]
        trigger={"kind":"agent","source":"plugin.supervisor","id":follow,"parent_task_id":task["id"],
                 "trace_id":task["trace_id"],"parent_event_id":event,
                 "read_only_tool":{"id":proposal["tool"],"binding":row["binding"]}}
        body={"id":follow,"trigger":trigger,"text":task["text"]}
        resumed=api("tasks",body)
        require(resumed["id"]==follow and resumed["trigger"]==trigger, "continuation lost original identity")
        emit("continuation_submitted", followup_task_id=follow,binding=row["binding"],parent_event_id=event)
        for _ in range(180):
            resumed=api("tasks?id="+follow)
            if resumed["state"] in ("SUCCEEDED","BLOCKED","FAILED","RECOVERY_REQUIRED","CANCELED"): break
            time.sleep(.2)
        save(root/"followup.json",resumed)
        emit("continuation_terminal",followup_task_id=follow,state=resumed["state"])
        require(resumed["state"]=="SUCCEEDED", "continuation did not succeed")
        return {"followup":follow,"release_event":event,"state":resumed["state"]}


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("phase",choices=("propose","apply"))
    for name in ("out","ready-site","approved-site","registry","validation"):
        cli.add_argument("--"+name,type=Path,required=name not in ("registry","validation"))
    for name in ("task","ready-lock","approved-lock","validation-digest"):
        cli.add_argument("--"+name,required=name != "validation-digest")
    args=cli.parse_args()
    if args.phase=="apply" and not (args.registry and args.validation and args.validation_digest):
        cli.error("apply needs registry and independently pinned validation")
    print(json.dumps(run(args),ensure_ascii=False))
