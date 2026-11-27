"""SDD-045. Exercise the production soft-runtime overlay in a fresh, software-only Compose project."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from lifecycle_acceptance import check


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"agent"))
from plugin_descriptor import compute_content_digest


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf8")


def until(probe, description, seconds=45):
    end = time.monotonic()+seconds
    last = None
    while time.monotonic() < end:
        try:
            last = probe()
            if last:
                return last
        except (URLError, TimeoutError, ConnectionError) as exc:
            last = type(exc).__name__
        time.sleep(.2)
    raise TimeoutError(f"{description}: last observation {last!r}")


def command(args, env=None, timeout=120):
    result = subprocess.run(args,cwd=ROOT,env=env,capture_output=True,encoding="utf8",errors="replace",timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[:5]} failed ({result.returncode}): {result.stderr[-1800:]} {result.stdout[-600:]}")
    return result.stdout.strip()


def prepare_upgrade(site, item):
    """The test operator attests a second reviewed implementation before the read-only site is mounted."""
    trust = site/"trust"
    catalog = json.loads((trust/"catalog.json").read_text(encoding="utf8"))
    old_key = item["provenance"]["code_digest"]
    records = catalog["implementations"][old_key]
    record = next(r for r in (records if isinstance(records,list) else [records]) if r["id"]==item["id"])
    old_code = (trust/record["artifact"]).read_bytes()
    field = b'"definition_version":legacy["version"]'
    check(old_code.count(field)==1, "review the pilot fixture after changing its receipt")
    source = old_code.replace(field,field+b', "tested_release":"v2"')
    sha = hashlib.sha256(source).hexdigest()
    relative = f"assets/{sha}.py"
    (trust/relative).parent.mkdir(exist_ok=True)
    (trust/relative).write_bytes(source)
    catalog["implementations"]["sha256:"+sha] = {**record,"artifact":relative}
    write(trust/"catalog.json",catalog)
    update = {**item,"version":"2.0.0","provenance":{**item["provenance"],"code_digest":"sha256:"+sha}}
    update["content_digest"] = compute_content_digest(update,trust)
    write(site/"releases/upgrade.plugin.json",update)
    return update


IDENTITY = """
from pathlib import Path
import json
hits = []
for f in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = f.read_bytes().split(b'\\0')
        if b'/opt/edge/agent/service.py' in args[1:4]:
            hits.append([int(f.parent.name), (f.parent/'stat').read_text().split(') ')[1].split()[19]])
    except (OSError, IndexError):
        continue
assert len(hits) == 1, hits
print(json.dumps(hits[0]))
"""

NET_PROBE = """
from http.client import HTTPConnection
import json, socket, sys
conn = HTTPConnection('soft-gateway',8129,timeout=5)
conn.request('POST','/agent/tasks',body=b'{}')
status = conn.getresponse().status
assert status==403,status
try:
    info=socket.getaddrinfo('agent',8090)
except socket.gaierror:
    info=[]
assert not info,info
with socket.socket() as s:
    s.settimeout(1)
    rc=s.connect_ex((sys.argv[1],8090))
assert rc!=0, 'isolated software provider can reach Agent control port directly'
print(json.dumps({'agent_dns_resolves':bool(info),'control_socket_denied':True,'gateway_submit_response_status':status}))
"""

PLANNER = """
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,re
class KnownQuery(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        match = re.fullmatch(r'count public evidence for (soft-ac-[a-f0-9]+)',body['request'])
        args = {'task_id':match[1],'sql':'SELECT COUNT(*) AS n FROM events',
                'after_seq':0,'limit':1,'archive_id':''} if match else None
        tools = {cap['id'] for cap in body['capabilities']}
        result = {'steps':[{'tool':'query_evidence','args':args}] if match and 'query_evidence' in tools else [],
                  'reason':'isolated test planner has no matching tool'}
        raw=json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
ThreadingHTTPServer(('0.0.0.0',8080),KnownQuery).serve_forever()
"""

AUDIT = """
import json,sqlite3
db=sqlite3.connect('file:/journal/module-events.sqlite?mode=ro',uri=True)
data=[json.loads(raw)['data'] for raw, in db.execute('SELECT event FROM module_bus_events ORDER BY seq')]
print(json.dumps([{k:r[k] for k in ('plugin_id','version','content_digest','from','to')} for r in data
                  if r.get('type')=='plugin.lifecycle' and r.get('plugin_id')=='derived.series_mean']))
"""


def run(image, out):
    if out.exists():
        raise FileExistsError(f"preserve previous run; choose a new --out: {out}")
    out.parent.mkdir(parents=True,exist_ok=True)
    project = "edge-test-soft-"+uuid.uuid4().hex[:12]
    report = {"project":project, "passed":False, "image":image, "stages":{}, "tasks":[]}
    stages = report["stages"]
    with tempfile.TemporaryDirectory(prefix=project+"-") as work:
        site = Path(work)/"site"
        plugins = Path(work)/"plugins"
        plugins.mkdir()
        contracts = shutil.copytree(ROOT/"contracts", Path(work)/"contracts")
        write(contracts/"planner-profile.json",{"schema_version":1,"protocol":"http-planner",
                                                "url":"http://ac-planner:8080","version":"ac-1"})
        fixture = Path(work)/"planner.json"
        write(fixture,{"services":{"ac-planner":{"image":image,"pull_policy":"never","read_only":True,
                                      "cap_drop":["ALL"],"entrypoint":["python3","-Bc",PLANNER]}}})
        compose = ["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose/soft-runtime.yaml"),
                   "-f",str(fixture)]
        env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
        started = False
        try:
            info = json.loads(command(["docker","image","inspect",image]))[0]
            platform = info["Os"]+"/"+info["Architecture"]
            descriptor = json.loads(command(["docker","image","inspect","--platform",platform,image]))[0]["Descriptor"]["digest"]
            labels = info["Config"].get("Labels") or {}
            revision = labels.get("org.opencontainers.image.revision","")
            commit = revision.removesuffix("-dirty")
            check(len(commit) in (40,64) and all(c in "0123456789abcdef" for c in commit),
                  "image needs a full source revision label; local candidates may append -dirty")
            dirty = revision.endswith("-dirty") or labels.get("edge.test.worktree")=="dirty"
            report.update(image_id=info["Id"],source_commit=commit,platform=platform,platform_digest=descriptor,
                          image_revision_label=revision, dirty=dirty, source_worktree="dirty" if dirty else "committed")
            command([sys.executable,"-B","scripts/prepare_soft_pilots.py","--site",str(site),
                     "--image",image,"--source-commit",commit],timeout=240)
            releases = {item["id"]:item for path in (site/"releases").glob("*.plugin.json")
                        for item in [json.loads(path.read_text(encoding="utf8"))]}
            series = releases["derived.series_mean"]
            upgrade = prepare_upgrade(site, series)
            env.update(COMPOSE_DISABLE_ENV_FILE="1",EDGE_SOFT_SITE=str(site),EDGE_SOFT_IMAGE=info["Id"],
                       EDGE_SOFT_PLATFORM_DIGEST=descriptor,EDGE_IMAGE=image,EDGE_CONSOLE_PORT="0",
                       EDGE_EVIDENCE_PORT="0",EDGE_PLUGINS_DIR=str(plugins),EDGE_EXTRA_PLUGIN_DIR="",
                       EDGE_CONTRACTS_DIR=str(contracts))
            def dc(*args):
                return command([*compose,*args],env)

            started = True
            dc("up","-d","--no-build","agent","console","soft-host","soft-gateway","evidence","session","ac-planner")
            port = dc("port","console","8080").splitlines()[0]
            check(port.startswith("127.0.0.1:"),"expected isolated loopback port")
            url = "http://"+port
            opener = build_opener(ProxyHandler({}))

            def api(path, body=None):
                req = Request(url+path, data=None if body is None else json.dumps(body).encode(),
                              headers={"Content-Type":"application/json"})
                with opener.open(req,timeout=8) as response:
                    return json.load(response)

            until(lambda:api("/agent/capabilities"),"Agent HTTP startup")
            pid = json.loads(dc("exec","-T","agent","python3","-c",IDENTITY))
            container = dc("ps","-q","agent")
            stages["original_agent"] = {"pid":pid[0],"start_ticks":pid[1],"container":container}
            agent_net = json.loads(command(["docker","inspect",container]))[0]["NetworkSettings"]["Networks"]
            agent_ip = agent_net[project+"_default"]["IPAddress"]
            def admin(action, target, expect):
                request_id = "ac-"+uuid.uuid4().hex
                return json.loads(dc("run","--rm","-T","--no-deps","soft-admin",action,target,"--kind","v2",
                                     "--root","/registry","--trust-root","/site/trust","--expect",expect,"--request-id",request_id))

            def native_rows():
                data = api("/agent/plugins?schema=2")
                return {row["id"]:row for row in data["plugins"] if not row.get("legacy")}

            installed = {}
            for key in sorted(releases):
                installed[key] = admin("publish","/site/releases/"+key+".plugin.json","absent")
            def active():
                rows = native_rows()
                return rows if all(key in rows and rows[key]["lifecycle"]=="ACTIVE" and
                                   rows[key]["health"]=="ready" for key in releases) else None
            stages["active"] = until(active,"all installed native releases active and healthy")

            def task(text, tool, success=True, key=None):
                key = key or "soft-ac-"+uuid.uuid4().hex[:10]
                api("/agent/tasks",{"id":key,"text":text})
                def terminal():
                    row = api("/agent/tasks?"+urlencode({"id":key}))
                    return row if row["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED","CANCELED") else None
                try:
                    result = until(terminal,"task "+key)
                except TimeoutError:
                    pending = api("/agent/tasks?"+urlencode({"id":key}))
                    report["tasks"].append({k:pending[k] for k in ("id","state","detail","steps")})
                    raise
                report["tasks"].append({"id":key,"state":result["state"],"steps":result["steps"],"detail":result["detail"]})
                if success:
                    check(result["state"]=="SUCCEEDED" and len(result["steps"])==1 and
                          result["steps"][0]["tool"]==tool,"unexpected tool/terminal state: "+str(report["tasks"][-1])[:900])
                else:
                    check(result["state"]!="SUCCEEDED","withdrawn tool reported success")
                return result

            phrase = "求输入数列的平均值 [2,4,6]"
            first = task(phrase,"derived.series_mean")
            check(first["steps"][0]["evidence"]["value"]==4,"unexpected calculation")
            read = task("复盘任务 "+first["id"], "read_session")
            check(read["steps"][0]["evidence"]["scope"]["task_id"]==first["id"],"session returned another task")
            session = api("/api/session?"+urlencode({"task_id":first["id"]}))
            check(session["service"]["plugin_id"]=="session.default" and session["scope"]["task_id"]==first["id"],
                  "session did not use the deployed plugin")
            stages["session"] = session["service"]
            counted = task("count public evidence for "+first["id"],"query_evidence")
            check(counted["steps"][0]["evidence"]["rows"][0][0]>0, "SQL tool did not receive the task's public events")

            # Delay one real HTTP provider after discovery. The Agent must finish the
            # already-frozen operation through its original release while new admission is closed.
            schema_key = "evidence.evidence_schema"
            phrase_schema = releases[schema_key]["config_schema"]["properties"]["profile"]["const"]["legacy"]["description"]
            pending_id = "soft-frozen-"+uuid.uuid4().hex[:10]
            with ThreadPoolExecutor(max_workers=1) as pool:
                dc("pause","evidence")
                try:
                    job = pool.submit(task,phrase_schema,"evidence_schema",True,pending_id)
                    until(lambda:native_rows().get(schema_key,{}).get("frozen")==1,"provider reservation",10)
                    receipt = admin("drain",schema_key,installed[schema_key]["revision"])
                    check(receipt["frozen"]==1, "provider lost original reservation during drain")
                finally:
                    dc("unpause","evidence")
                original = job.result(timeout=50)
                step = original["steps"][0]
                check(step["soft_pin"]["content_digest"]==releases[schema_key]["content_digest"] and
                      step["operation_id"]==pending_id+"/step/1","in-flight task lost its frozen operation")
                stages["inflight_drain"] = {"operation_id":step["operation_id"],"digest":step["soft_pin"]["content_digest"],
                                            "frozen_at_drain":receipt["frozen"]}

            for service in ("evidence","session","soft-host"):
                ident = dc("ps","-q",service)
                config = json.loads(command(["docker","inspect",ident]))[0]
                host = config["HostConfig"]
                mounts = [m["Destination"] for m in config["Mounts"] if m.get("RW")]
                check(not host["Privileged"] and not host.get("Devices") and not host.get("DeviceRequests"),
                      "unexpected hardware permission "+service)
                check(host["ReadonlyRootfs"] and "ALL" in (host["CapDrop"] or []),"unexpected process privileges")
                check(all(not m.startswith("/dev") and "docker.sock" not in m for m in mounts),"device/socket mount")
                check(service=="soft-host" or not any(m in ("/site","/registry") for m in mounts),"provider registry write access")
                stages["isolation_"+service] = {**json.loads(dc("exec","-T",service,"python3","-c",NET_PROBE,agent_ip)),
                    "networks":sorted(config["NetworkSettings"]["Networks"]),"mounts_rw":mounts,
                    "device_count":len(host.get("Devices") or []),"cap_drop":host["CapDrop"]}

            def retire(key, revision):
                drained = admin("drain",key,revision)
                def disposed():
                    return json.loads(dc("exec","-T","soft-host","python3","-c",
                        "import sys,json;sys.path.insert(0,'/opt/edge/agent');from v2_registry import V2Registry;"
                        "r=V2Registry('/registry');print(json.dumps(next(x['disposed'] for x in r.entries() if x['descriptor']['id']==sys.argv[1])));r.close()",key))
                until(disposed,"confirmed disposal of "+key)
                return drained["revision"]

            key = series["id"]
            drained = retire(key,installed[key]["revision"])
            changed = admin("publish","/site/releases/upgrade.plugin.json",drained)
            until(lambda:native_rows().get(key,{}).get("content_digest")==upgrade["content_digest"] and
                  native_rows().get(key,{}).get("health")=="ready",
                  "updated generation")
            second = task(phrase,key)
            old_step,new_step = first["steps"][0],second["steps"][0]
            check(old_step["binding"] != new_step["binding"] and new_step["evidence"]["value"]==4 and
                  "tested_release" not in old_step["evidence"] and new_step["evidence"].get("tested_release")=="v2",
                  "new code marker, binding or value inconsistent")
            saved = api("/agent/tasks?"+urlencode({"id":first["id"]}))
            check(saved["steps"]==first["steps"],"previous task was rewritten")
            drained = retire(key,changed["revision"])
            admin("remove",key,drained)
            until(lambda:native_rows().get(key,{}).get("lifecycle") in (None,"DISPOSED"),"removal from active catalog")
            task(phrase,key,success=False)
            check(container==dc("ps","-q","agent") and pid==json.loads(dc("exec","-T","agent","python3","-c",IDENTITY)),
                  "Agent process changed")
            stages["upgrade_remove"] = {"previous_digest":series["content_digest"],"updated_digest":upgrade["content_digest"],
                                        "agent_pid_and_start_unchanged":True,"saved_step_unchanged":True}
            lifecycle = json.loads(dc("exec","-T","soft-host","python3","-c",AUDIT))
            expected = ["PENDING","LOADING","ACTIVE","UNLOADING","DISPOSED"]
            for item in (series,upgrade):
                check([row["to"] for row in lifecycle if row["version"]==item["version"] and
                       row["content_digest"]==item["content_digest"]]==expected,"lost or repeated lifecycle transition")
            stages["lifecycle"] = lifecycle
            report["passed"] = True
        except BaseException as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if started:
                try:
                    command([*compose,"down","-v","--remove-orphans","--timeout","3"],env)
                except Exception as exc:
                    report.update(passed=False,teardown_error=str(exc))
            write(out,report)
    check(report["passed"],"inspect verification evidence")
    print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    run(args.image,args.out)
