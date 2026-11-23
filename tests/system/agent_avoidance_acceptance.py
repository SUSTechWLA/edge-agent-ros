"""Submit a real language task to an isolated Agent; the world and scorer own the ground truth."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "/opt/edge/tests/system/agent_avoidance_acceptance.py"
RESOURCE = "sim.lingao.base"
TEXT = "躲避前方障碍物"
MODEL = "qwen3:4b-instruct"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf8")


def volumes():
    model_volume = os.environ.get("EDGE_AVOIDANCE_MODEL_VOLUME")
    if not model_volume:
        raise ValueError("EDGE_AVOIDANCE_MODEL_VOLUME must name an existing local Ollama model volume")
    return {"operator-token":{"external":False}, "model-weights":{"name":model_volume, "external":True}}


def configure(overrides, common, a, out, env, project):
    sys.path.insert(0, str(ROOT / "scripts"))
    from prepare_soft_pilots import local_image

    site = out / "site"
    site.mkdir()
    cached_manifest=os.environ.get("EDGE_AVOIDANCE_PLATFORM_MANIFEST")
    manifest = Path(cached_manifest).read_bytes() if cached_manifest else local_image(a.image)
    (site / "platform.json").write_bytes(manifest)
    shutil.copyfile(ROOT / "capabilities/rover-maneuver/sim.json", site / "sim.json")
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    image = json.loads(subprocess.check_output(["docker","image","inspect","--platform","linux/amd64",a.image]))[0]
    if image["Descriptor"]["digest"] != digest:
        raise RuntimeError("deployment manifest and selected image differ")
    reference=image["RepoDigests"][0]
    repository, index_digest=reference.split("@",1)
    env.update(EDGE_ROVER_IMAGE_REPOSITORY=repository, EDGE_ROVER_IMAGE_DIGEST=index_digest,
               EDGE_ROVER_PLATFORM="linux/amd64", EDGE_ROVER_SITE=str(site),
               EDGE_PHYSICAL_PLATFORM_DIGEST=digest, EDGE_ROVER_TOKEN_VOLUME=project+"_operator-token")
    runtime = {"image":reference, "platform":"linux/amd64", "pull_policy":"never", "init":True, "read_only":True,
               "cap_drop":["ALL"], "security_opt":["no-new-privileges:true"], "tmpfs":["/tmp"]}
    token_dir = "/run/operator"
    model_name = os.getenv("EDGE_AVOIDANCE_MODEL", MODEL)
    ollama_ref = os.getenv("EDGE_AVOIDANCE_OLLAMA_IMAGE", "ollama/ollama:latest")
    ollama_id = json.loads(subprocess.check_output(["docker","image","inspect",ollama_ref]))[0]["RepoDigests"][0]
    overrides["local-model"] = {"image":ollama_id, "pull_policy":"never",
        "volumes":[{"type":"volume","source":"model-weights","target":"/root/.ollama/models/"+part,
                    "read_only":True,"volume":{"subpath":"models/"+part}} for part in ("blobs","manifests")],
        "environment":{"OLLAMA_NO_CLOUD":"1","OLLAMA_NOPRUNE":"true"}}
    overrides["operator-init"] = {**runtime, "network_mode":"none", "volumes":["operator-token:"+token_dir],
        "command":["python3","-c","import os,secrets; from pathlib import Path; os.umask(0o077); "
                   "p=Path('/run/operator/motion'); p.exists() or p.write_text(secrets.token_hex(32))"]}
    overrides["agent"] = {"environment":{**common, "EDGE_OLLAMA_URL":"http://local-model:11434",
        "EDGE_OLLAMA_MODEL":model_name, "EDGE_PLANNER_URL":"", "EDGE_TRACE_CONTENT":"redacted",
        "EDGE_PLUGIN_DIR":"/etc/edge/no-site-tools", "EDGE_EXTRA_PLUGIN_DIR":""},
        "depends_on":{"operator-init":{"condition":"service_completed_successfully"}}}
    overrides["audit"]["volumes"].append("operator-token:"+token_dir+":ro")
    overrides["audit"]["environment"]["EDGE_AVOIDANCE_MODEL"] = model_name
    save(out/"deployment.json", {"edge_platform_digest":digest,"edge_image":reference,
         "ollama_image":ollama_id,"model":model_name,"site_sha256":hashlib.sha256((site/"sim.json").read_bytes()).hexdigest()})
    return ("local-model","maneuver-controller","rover-execute","rover-verify","agent")


def execute(compose, ros, out, task, scenario):
    try:
        ros("python3","-B",ENTRY,"--task",task,"--out","/audit",timeout=500)
    finally:
        records=json.loads(compose("exec","-T","maneuver-controller","python3","-B",ENTRY,
                                   "--task",task,"--physical-receipt"))
        save(out/"physical-ledger.json",records)
    if len(records)!=1 or records[0]["state"]!="SUCCEEDED" or not records[0].get("proof"):
        raise AssertionError("DSH ledger and successful Agent task disagree")
    return json.loads((out/"mission.json").read_bytes())


def request(url, data=None, token=None, timeout=12):
    headers = {"Content-Type":"application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if data is None else json.dumps(data,ensure_ascii=False).encode()
    req = Request(url, body, headers=headers)
    with build_opener(ProxyHandler({})).open(req,timeout=timeout) as resp:
        return json.load(resp)


def receipt_with_core(value):
    if isinstance(value,dict):
        if all(k in value for k in ("core_mission_id","core_operation","odom_initial","odom_goal")):
            return value
        for child in value.values():
            found = receipt_with_core(child)
            if found:
                return found
    if isinstance(value,list):
        for child in value:
            found = receipt_with_core(child)
            if found:
                return found
    return None


def submit(task, out):
    model = os.environ.get("EDGE_AVOIDANCE_MODEL",MODEL)
    endpoint = "http://agent:8090/agent/tasks"
    deadline = time.monotonic()+80
    while time.monotonic() < deadline:
        try:
            caps = request("http://agent:8090/agent/capabilities")["capabilities"]
            if any(cap["effect"] == "actuation" for cap in caps):
                break
        except OSError:
            pass
        time.sleep(.5)
    else:
        raise TimeoutError("active physical maneuver capability unavailable")
    save(out/"catalog.json",caps)
    model_meta = request("http://local-model:11434/api/show",{"model":model})
    installed = request("http://local-model:11434/api/tags")["models"]
    identity = next(row for row in installed if model in (row.get("model"),row.get("name")))
    request("http://local-model:11434/api/generate",{"model":model,"stream":False,"keep_alive":"5m"},timeout=150)
    save(out/"model.json",{"model":model,"digest":identity["digest"],"details":model_meta["details"]})
    intent = {"id":task,"text":TEXT,"motion_resource":RESOURCE}
    try:
        request(endpoint,{**intent,"id":task+"-denied"})
    except HTTPError as err:
        if err.code not in (401,403):
            raise
        save(out/"authorization-denied.json",{"http":err.code,"response":json.load(err)})
    else:
        raise AssertionError("server accepted a motion grant without operator credentials")
    save(out/"submitted.json",intent)
    first = request(endpoint,intent,token=Path("/run/operator/motion").read_text().strip())
    if first["id"] != task:
        raise ValueError("Agent assigned a different task ID")
    deadline=time.monotonic()+220
    while time.monotonic()<deadline:
        result=request(endpoint+"?"+urlencode({"id":task,"limit":256}))
        if result["state"] in {"SUCCEEDED","FAILED","REFUSED","BLOCKED","CANCELED","RECOVERY_REQUIRED"}:
            break
        time.sleep(.5)
    save(out/"agent-task.json",result)
    kinds=[event["kind"] for event in result["events"]]
    if (result["state"]!="SUCCEEDED" or result.get("provider")!="ollama/"+model or result.get("events_has_more")
            or not {"model.request","model.response"}.issubset(kinds)):
        raise AssertionError(f"missing verified model-driven task: {result['state']}, events={kinds}")
    physical=[s for s in result["steps"] if s.get("physical_pin")]
    if len(physical)!=1 or physical[0]["physical_pin"]["resource_id"]!=RESOURCE:
        raise AssertionError("expected one frozen physical maneuver on the authorized base")
    responses=[json.loads(e["payload"])["trace"] for e in result["events"] if e["kind"]=="model.response"]
    choices=json.loads(responses[-1]["artifact"]["content"]["message"]["content"])
    if sum(s["tool"]==physical[0]["tool"] for s in choices["steps"])!=1:
        raise AssertionError("actual model completion and frozen physical dispatch differ")
    receipt=receipt_with_core(physical[0].get("evidence"))
    if not receipt:
        raise AssertionError("verified tool evidence lacks the frozen maneuver and linked Core receipt")
    core=receipt["core_mission_id"]
    with sqlite3.connect("file:/core-state/edge.db?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        events=[dict(r) for r in db.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq",(core,))]
        steps=[dict(r) for r in db.execute("SELECT * FROM steps WHERE mission_id=? ORDER BY step",(core,))]
    save(out/"core-ledger.json",{"mission_id":core,"events":events,"steps":steps})
    verified=[e for e in events if e["kind"]=="step.verified" and e["operation_id"]==receipt["core_operation"]]
    if (not events or events[-1]["state"]!="SUCCEEDED" or len(steps)!=1 or len(verified)!=1 or
            steps[0]["operation_id"]!=receipt["core_operation"] or steps[0]["odom_stamp_ns"]<=0 or
            [steps[0]["x"],steps[0]["y"]]!=receipt["odom_goal"]):
        raise AssertionError("Agent completion and independent Core ledger disagree")
    save(out/"mission.json",{"task":core,"agent_task":task,"operation":receipt["core_operation"],"state":events[-1]["state"],
        "verified_count":len(verified),"accepted":True,"odom_initial":receipt["odom_initial"],"odom_goal":receipt["odom_goal"]})


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--task",required=True)
    mode=cli.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out",type=Path)
    mode.add_argument("--physical-receipt",action="store_true")
    args=cli.parse_args()
    if args.physical_receipt:
        with sqlite3.connect("file:/ledger/physical.sqlite?mode=ro",uri=True) as db:
            db.row_factory=sqlite3.Row
            prefix=args.task+"/step/"
            rows=[dict(r) for r in db.execute("SELECT op,resource,state,reason,pin,candidate,proof "
                                              "FROM physical_ops WHERE substr(op,1,?)=? ORDER BY op",(len(prefix),prefix))]
        for row in rows:
            for field in ("pin","candidate","proof"):
                if row[field]:
                    row[field]=json.loads(row[field])
        print(json.dumps(rows,allow_nan=False))
    else:
        submit(args.task,args.out)
