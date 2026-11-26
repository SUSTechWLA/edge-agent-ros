"""Watch native Agent gap events and qualify read-only DSH plugins under deployment authority."""
import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, Request

from client import field_plugin_publish as deploy
from agent.kernel import authorization, read_authority
from edge_contracts.descriptor import validate_instance
from perception.spatial_facts import authored_screen


@dataclass(frozen=True)
class Family:
    instructions: str
    schema: dict
    lower: object
    validate: object
    qualify: object
    publish: object
    provider_prefix: str = "field."
    repair_issues: object = None


def comparison_schema(left, right):
    return {"type":"object", "properties":{"left":left, "right":right,
            "op":{"type":"string","enum":list(authored_screen.COMPARISONS)}},
            "required":["left","right","op"], "additionalProperties":False}


DEPTH_FIELDS = {"type":"string","enum":["depth_m","distance_m"]}
DEPTH_AUTHOR_SCHEMA = {
    **authored_screen.PROGRAM_SCHEMA,
    "properties": {
        **authored_screen.PROGRAM_SCHEMA["properties"],
        "unknown_if":comparison_schema({"type":"string","enum":["valid_fraction"]},
                                       {"type":"number","enum":list(authored_screen.FRACTIONS)}),
        "near_if":comparison_schema(DEPTH_FIELDS, DEPTH_FIELDS),
    },
}
DEPTH_AUTHOR_CONTRACT = (
    "Implement a reusable read-only camera review capability. Its callers supply columns and distance_m each time. "
    "For each column of one frame use the 10th percentile of valid optical depths. Coverage strictly below 60 percent is unknown. "
    "A measured depth at or below the caller's distance threshold marks a candidate for human review. "
    "Order the marked columns by increasing measured depth. Write the comparisons as objects with left, right and op. "
    "The statistic vocabulary is p10 = 10th percentile, p25 = 25th percentile, median = 50th percentile. "
    "An expression reads 'left op right'. Operators lt (<), le (<=), gt (>), ge (>=) preserve equality exactly. "
    "valid_fraction is the fraction of valid pixels from zero to one, depth_m is the measured depth, "
    "and distance_m is the per-call threshold. Keep the name and description general for future callers."
)


def lower_depth(program):
    validate_instance(DEPTH_AUTHOR_SCHEMA, program)
    result = dict(program)
    for field in ("unknown_if", "near_if"):
        expression = program[field]
        result[field] = [expression[k] for k in ("left", "right", "op")]
    return result


def depth_repair_issues(program):
    """Explain the public family contract using only the model's own proposal."""
    if not isinstance(program, dict):
        return [{"field":"program","requirement":"Return the four named fields in the program schema."}]
    coverage = program.get("unknown_if")
    distance = program.get("near_if")
    clauses = (
        ("statistic", program.get("statistic") == "p10",
         "Use the 10th percentile of valid depths. A median measures the 50th percentile."),
        ("unknown_if", coverage == {"left":"valid_fraction","right":0.6,"op":"lt"},
         "Use a comparison object. Its left operand is valid_fraction and its right operand is a numeric "
         "fraction. Coverage strictly below 60 percent is unknown; equality has sufficient coverage."),
        ("near_if", distance in ({"left":"depth_m","right":"distance_m","op":"le"},
                                 {"left":"distance_m","right":"depth_m","op":"ge"}),
         "Compare the measured depth to the caller's distance in a comparison object. A depth exactly "
         "at the threshold is included, as are closer surfaces."),
        ("review_order", program.get("review_order") == "nearest_first",
         "Rank the marked candidates by increasing measured optical depth."),
    )
    return [{"field":field,"requirement":requirement} for field, matches, requirement in clauses if not matches]


FAMILIES = {"depth_column_review": Family(DEPTH_AUTHOR_CONTRACT, DEPTH_AUTHOR_SCHEMA, lower_depth,
    authored_screen.validate_program, authored_screen.qualify, deploy.publish, repair_issues=depth_repair_issues)}
TERMINAL = {"SUCCEEDED","BLOCKED","FAILED","CANCELED","RECOVERY_REQUIRED"}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
CODE = "planner.plugin_design_requested"


class Rejected(ValueError):
    """Permanent source, model or policy rejection for just this task."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def approved_family(path, name):
    raw = path.read_bytes()
    require(len(raw) <= 65536, "invalid site family profile size")
    obj = decode(raw)
    require(isinstance(obj, dict) and obj.get("schema_version") == 1 and isinstance(obj.get("families"), dict),
            "invalid site family profile")
    entry = obj["families"].get(name)
    require(name in FAMILIES and isinstance(entry, dict) and set(entry)=={"description","provider_prefix"}
            and isinstance(entry["description"], str) and entry["description"].strip()
            and entry["provider_prefix"] == FAMILIES[name].provider_prefix, "family is not approved by this site")
    return deploy.sha(raw)


def wire(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",",":")).encode()


def unique(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "duplicate JSON field")
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        return None


def fetch(url, data=None, timeout=12, size=4*1024*1024):
    # Endpoints are deployment config only. Redirects cannot forward task text off the edge host.
    request = Request(url, data, {"Content-Type":"application/json"} if data is not None else {})
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(size+1)
    require(len(raw) <= size, "oversize HTTP response")
    return raw


def save(path, value, *, immutable=False):
    deploy.write(path, wire(value)+b"\n", immutable=immutable)


def event(folder, phase, task, *, key=None, **details):
    path = folder/"events.jsonl"
    key = key or phase
    if path.exists():
        with path.open(encoding="utf8") as stream:
            for line in stream:
                row = decode(line)
                if row["key"] == key:
                    return row
    row = {"phase":phase,"utc":now(),"task_id":task["id"],"trace_id":task["trace_id"],"key":key,**details}
    with path.open("ab") as stream:
        stream.write(wire(row)+b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(row["utc"], task["id"], phase, flush=True)
    return row


def model_schema(family):
    return {"type":"object","additionalProperties":False,"required":["name","description","program"],"properties":{
        "name":{"type":"string","pattern":"^[a-z][a-z0-9]*(_[a-z0-9]+)*$","minLength":2,"maxLength":40},
        "description":{"type":"string","minLength":1,"maxLength":300},"program":family.schema}}


def failure_context(exc):
    # The model may receive its own previous proposal, never a hidden test identifier or expected result.
    text = str(exc)
    if text.startswith("screen.qualification_failed"):
        return "independent_validation_failed"
    if text.startswith("screen.") and re.fullmatch(r"screen\.[a-z_]+",text):
        return text
    return "invalid_candidate"


def authored(folder, task, family_id, model_url, model, timeout):
    family = FAMILIES[family_id]
    prior = None
    for attempt in (1,2):
        request_file = folder/f"author-request-{attempt}.json"
        response_file = folder/f"author-response-{attempt}.json"
        input_data = {"family":family_id, "specification":family.instructions}
        if prior is not None:
            input_data["previous_attempt"] = prior
        system = ("You author reusable, read-only Edge Agent plugins within one deployment-approved specification. "
                  "Follow the site specification. "
                  "Give a meaningful lower_snake_case tool name, a concise plain description of capabilities and call "
                  "arguments in Chinese, and the program. Return exactly name, description, program. "
                  "Do not add task-specific constants, code, network access or movement. "
                  "Translate each clause of the user's specification to the corresponding program field. "
                  "When revising a rejected proposal, re-check the statistic and both comparison boundaries.")
        request = {"model":model, "stream":False, "format":model_schema(family),
                   "options":{"temperature":0,"num_predict":384,"num_ctx":2048},
                   "messages":[{"role":"system","content":system},{"role":"user","content":wire(input_data).decode()}]}
        previous = request_file.exists()
        if previous:
            # A restart or software upgrade must not rewrite the original request or resend it.
            require(not request_file.is_symlink(), "unexpected symlink: " + request_file.name)
            raw_request = request_file.read_bytes()
            saved = decode(raw_request)
            saved_input = decode(saved["messages"][1]["content"])
            require(saved.get("model") == model and saved_input.get("family") == family_id,
                    "saved author request identity mismatch")
        else:
            raw_request = wire(request)
            deploy.write(request_file, raw_request, immutable=True)
        request_digest = deploy.sha(raw_request)
        recorded = event(folder,"author_requested",task,key=f"request-{attempt}",attempt=attempt,model=model,
                         request_sha256=request_digest)
        require(recorded["request_sha256"] == request_digest, "saved author request differs from its receipt")
        proposal = None
        try:
            if not response_file.exists():
                # A persisted request may have reached Ollama before a process crash. Count this attempt.
                require(not previous, "previous request interrupted without a durable reply")
                raw = fetch(model_url+"/api/chat", raw_request, timeout, 131072)
                deploy.write(response_file,raw,immutable=True)
            raw = response_file.read_bytes()
            event(folder,"author_replied",task,key=f"reply-{attempt}",attempt=attempt,response_sha256=deploy.sha(raw))
            answer = decode(raw)
            require(isinstance(answer,dict) and answer.get("done") is True and answer.get("done_reason") != "length"
                    and answer.get("model") == model, "model reply invalid or truncated")
            proposal = decode(answer["message"]["content"])
            save(folder/f"author-proposal-{attempt}.json",proposal,immutable=True)
            require(isinstance(proposal,dict) and set(proposal) == {"name","description","program"},"invalid candidate fields")
            name, desc = proposal["name"], proposal["description"]
            require(isinstance(name,str) and 2<=len(name)<=40 and deploy.TOOL_NAME.fullmatch(name)
                    and name not in ("status","inspect") and isinstance(desc,str) and 1<=len(desc)<=300
                    and desc.strip()==desc and all(c.isprintable() for c in desc), "invalid name or description")
            candidate = {**proposal, "program":family.lower(proposal["program"])}
            save(folder/f"candidate-{attempt}.json",candidate,immutable=True)
            proof = family.qualify(family.validate(candidate["program"]))
            save(folder/f"validation-{attempt}.json",proof,immutable=True)
            event(folder,"candidate_verified",task,key=f"verified-{attempt}",attempt=attempt,name=name,
                  program_sha256=proof["program_sha256"],case_count=proof["case_count"])
            return candidate
        except (ValueError, KeyError, TypeError, IndexError, RecursionError, OSError) as exc:
            code = failure_context(exc)
            prior = {"reason":code}
            if isinstance(proposal, dict) and "program" in proposal and family.repair_issues:
                prior["issues"] = family.repair_issues(proposal["program"])
            event(folder,"candidate_rejected",task,key=f"rejected-{attempt}",attempt=attempt,
                  code=code,local_reason=str(exc)[:300],issues=prior.get("issues",[]))
            if proposal is not None:
                prior["candidate"] = proposal if len(wire(proposal)) <= 2500 else "oversize"
    raise Rejected("two model attempts exhausted")


def gap(task, source):
    trigger = task.get("trigger",{})
    require(task.get("state") == "BLOCKED" and task.get("steps") == [] and trigger.get("kind") == "user"
            and trigger.get("source") == "user" and trigger.get("id") == task["id"] and task["id"] == source["task_id"]
            and isinstance(task.get("text"),str) and 0 < len(task["text"]) <= 10000, "original task is not an undispatched user request")
    try:
        read_authority.check_read_request(task["text"])
    except Exception as exc:
        raise Rejected("protected system scope") from exc
    require(not authorization.explicit_motion_request(task["text"]), "request includes actual motion")
    blocked = next((r for r in task["events"] if r.get("event_id") == source["event_id"]),None)
    require(blocked == source and blocked.get("kind") == "blocked", "source event differs from native task ledger")
    payload = decode(blocked["payload"])
    trace = payload["trace"]
    require(payload.get("diagnostic",{}).get("code")==CODE and trace.get("event_id")==source["event_id"]
            and trace.get("trace_id")==task["trace_id"] and trace.get("trigger")==trigger, "blocked event provenance mismatch")
    matches=[]
    for r in task["events"]:
        if r.get("kind") != "planner.plugin_gap" or r.get("task_id") != task["id"] or r["seq"] >= source["seq"]:
            continue
        p = decode(r["payload"])
        t = p.get("trace",{})
        if (t.get("module")=="planner" and t.get("layer")=="planner" and t.get("event_id")==r.get("event_id")
                and t.get("trace_id")==task["trace_id"] and t.get("trigger")==trigger and t.get("provider")):
            matches.append((p.get("family",t.get("family")),r))
    require(matches and matches[-1][0] in FAMILIES, "no preceding authorised design family selected by the native planner")
    return matches[-1]


class Watch:
    def __init__(self,a,db):
        self.a,self.db = a,db
        self.agent = a.agent_url.rstrip("/")
        self.api = self.agent if self.agent.endswith("/agent") else self.agent+"/agent"
        self.profile = a.family_profile
        db.executescript("""
            CREATE TABLE IF NOT EXISTS feed(id INTEGER PRIMARY KEY CHECK(id=1),source TEXT NOT NULL,after INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(event TEXT PRIMARY KEY,task TEXT UNIQUE NOT NULL,fact TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',due REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS published(family TEXT PRIMARY KEY,receipt TEXT NOT NULL);
        """)

    def page(self,source,after):
        params = {"limit":128,"after":after}
        if source:
            params["source_id"] = source
        page = decode(fetch(self.api+"/task-events?"+urlencode(params)))
        require(isinstance(page["source_id"],str) and (not source or source==page["source_id"]), "event source changed")
        seq = after
        for row in page["events"]:
            require(type(row["seq"]) is int and row["seq"]>seq, "nonmonotonic event feed")
            seq=row["seq"]
        require(seq==page["next_after"] and (page["events"] or not page["events_has_more"]), "invalid feed cursor")
        return page

    def scan(self):
        cursor = self.db.execute("SELECT source,after FROM feed WHERE id=1").fetchone()
        source,after = cursor or (None,0)
        while True:
            page=self.page(source,after)
            source,after=page["source_id"],page["next_after"]
            if cursor:
                with self.db:
                    for row in page["events"]:
                        if row.get("kind")!="blocked" or not SAFE_NAME.fullmatch(row.get("task_id","")):
                            continue
                        if decode(row["payload"]).get("diagnostic",{}).get("code")==CODE:
                            self.db.execute("INSERT OR IGNORE INTO jobs(event,task,fact) VALUES(?,?,?)",
                                            (row["event_id"],row["task_id"],wire(row).decode()))
                    self.db.execute("UPDATE feed SET after=? WHERE id=1",(after,))
            if not page["events_has_more"]:
                break
        if not cursor:
            with self.db:
                self.db.execute("INSERT INTO feed VALUES(1,?,?)",(source,after))
        if not (self.a.state/"ready.json").exists():
            save(self.a.state/"ready.json",{"source_id":source,"after":after,"utc":now()})
            print("field plugin watcher ready",source,after,flush=True)

    def process(self,task_id,source):
        folder = self.a.state/"jobs"/task_id
        folder.mkdir(parents=True, exist_ok=True)
        raw = fetch(self.api+"/tasks?"+urlencode({"id":task_id,"limit":500}))
        task = decode(raw)
        family, selection = gap(task,source)
        profile_digest = approved_family(self.profile, family)
        original = folder/"original-task.json"
        if not original.exists():
            deploy.write(original,raw,immutable=True)
        save(folder/"source-event.json",source,immutable=True)
        event(folder,"gap_observed",task,family=family,family_profile_sha256=profile_digest,
              gap_event_id=selection["event_id"],blocked_event_id=source["event_id"])
        cached = self.db.execute("SELECT receipt FROM published WHERE family=?",(family,)).fetchone()
        if cached:
            receipt = decode(cached[0])
            require(deploy.authorised_policy(self.a.policy)["bindings"].get(receipt["tool_id"])==receipt["binding"],
                    "family binding has been withdrawn")
            deploy.live_record(self.agent,receipt["plugin_id"],receipt["tool_id"],receipt["binding"])
            if not (folder/"published-plugin.json").exists():
                save(folder/"published-plugin.json",receipt)
                event(folder,"plugin_reused",task,binding=receipt["binding"],plugin_id=receipt["plugin_id"])
        else:
            candidate=authored(folder,task,family,self.a.ollama_url.rstrip("/"),self.a.model,self.a.model_timeout)
            require(approved_family(self.profile, family)==profile_digest, "site design authority changed during authoring")
            receipt=FAMILIES[family].publish(self.a.site,self.a.registry,self.a.policy,candidate,self.agent,folder)
            with self.db:
                self.db.execute("INSERT INTO published VALUES(?,?)",(family,wire(receipt).decode()))
        event(folder,"plugin_loaded",task,binding=receipt["binding"],plugin_id=receipt["plugin_id"],
              content_digest=receipt["content_digest"],event_id=receipt["event_id"])
        follow="field-"+hashlib.sha256(wire([task_id,source["event_id"],receipt["content_digest"]])).hexdigest()[:32]
        trigger={"kind":"agent","source":"plugin.supervisor","id":follow,"parent_task_id":task_id,
                 "trace_id":task["trace_id"],"parent_event_id":receipt["event_id"],
                 "read_only_tool":{"id":receipt["tool_id"],"binding":receipt["binding"]}}
        post=wire({"id":follow,"trigger":trigger,"text":task["text"]})
        deploy.write(folder/"followup-post.json",post,immutable=True)
        ack_file=folder/"followup-response.json"
        if not ack_file.exists():
            raw_ack=fetch(self.api+"/tasks",post,45)
            ack=decode(raw_ack)
            require(ack["id"]==follow and ack["trigger"]==trigger and ack["trace_id"]==task["trace_id"],
                    "continuation lost its identity")
            deploy.write(ack_file,raw_ack,immutable=True)
        event(folder,"continuation_submitted",task,followup_task_id=follow,binding=receipt["binding"])
        raw_follow=fetch(self.api+"/tasks?"+urlencode({"id":follow,"limit":500}))
        child=decode(raw_follow)
        require(child["id"]==follow and child["trace_id"]==task["trace_id"] and child["trigger"]==trigger,
                "unrelated continuation")
        if child["state"] not in TERMINAL:
            return "pending"
        deploy.write(folder/"followup.json",raw_follow)
        if child["state"]=="SUCCEEDED":
            require(len(child["steps"])==1 and child["steps"][0].get("binding")==receipt["binding"]
                    and child["steps"][0].get("tool")==receipt["tool_id"],"wrong tool executed")
        event(folder,"continuation_terminal",task,followup_task_id=follow,state=child["state"])
        return child["state"]

    def tick(self):
        self.scan()
        rows=list(self.db.execute("SELECT event,task,fact FROM jobs WHERE state='pending' AND due<=? ORDER BY rowid LIMIT 8",(time.time(),)))
        for event_id,tid,raw in rows:
            state="pending"
            delay=max(1,self.a.poll_seconds)
            try:
                state=self.process(tid,decode(raw))
            except (ValueError,TypeError,KeyError,OSError,RecursionError) as exc:
                retry=(isinstance(exc,OSError) and not(isinstance(exc,HTTPError) and 400 <= exc.code < 500)
                       or "no available matching live binding" in str(exc))
                state="pending" if retry else "rejected"
                folder=self.a.state/"jobs"/tid
                folder.mkdir(parents=True,exist_ok=True)
                trace=decode(decode(raw)["payload"]).get("trace",{}).get("trace_id",tid)
                phase="retrying" if retry else "failed"
                # Fresh staging paths vary per retry. Persist one event per actual error class.
                reason_key=str(getattr(exc, "errno", None) or getattr(exc, "code", None) or str(exc)[:80])
                if retry:
                    delay=max(10,delay)
                event(folder,phase,{"id":tid,"trace_id":trace},key=phase+":"+type(exc).__name__+":"+reason_key,
                      reason=str(exc)[:300])
            with self.db:
                self.db.execute("UPDATE jobs SET state=?,due=? WHERE event=?",
                                (state,time.time()+delay,event_id))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("site","registry","policy","state"):
        parser.add_argument("--"+name,required=True,type=Path)
    for name in ("agent-url","ollama-url","model"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--poll-seconds",type=float,default=1)
    parser.add_argument("--model-timeout",type=int,default=360)
    parser.add_argument("--family-profile",type=Path,
                        default=Path(__file__).resolve().parents[1]/"profiles/field-read-families.json",
                        help="Read-only site profile also mounted for the native planner")
    parser.add_argument("--once",action="store_true")
    a=parser.parse_args()
    for base in (a.agent_url,a.ollama_url):
        p=urlsplit(base)
        require(p.scheme=="http" and p.hostname and not(p.username or p.password or p.query or p.fragment),"invalid local endpoint")
    require(0.1<=a.poll_seconds<=60 and 1<=a.model_timeout<=600,"invalid runtime bounds")
    if a.policy.is_dir():
        a.policy=a.policy/"approved.json"
    os.umask(0o077)
    a.state.mkdir(parents=True,exist_ok=True)
    # The exclusive process lease prevents concurrent model attempts for the same persisted work.
    with closing(sqlite3.connect(a.state/"watch-lease.sqlite",timeout=1)) as lease, closing(sqlite3.connect(a.state/"watch.sqlite")) as db:
        lease.execute("BEGIN IMMEDIATE")
        w=Watch(a,db)
        while True:
            try:
                w.tick()
            except OSError as exc:
                if a.once:
                    raise
                print("waiting for configured Agent endpoint:",str(exc)[:200],file=sys.stderr,flush=True)
            if a.once:
                return
            time.sleep(a.poll_seconds)


if __name__=="__main__":
    main()
