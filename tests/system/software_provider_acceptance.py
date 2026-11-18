"""Build a private SDD-013 design, approve a checked text function and hot-install it in a fresh Agent."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from plugin_design_acceptance import DeclineOdometry, MODEL_IMAGE, ROOT, Watch
from lifecycle_acceptance import check,wait_for
from runtime_plugin_acceptance import mounted
from plugin_design import ModuleJournal,run as design
from software_provider import digest,program,Invalid


SAMPLE = ROOT/"examples/software-provider"
BODY = "ｒｏｂｏｔ  ROBOT\trobot   "
TEXT = "统计 robot 词频【"+BODY+"】"
GOAL = "Count exact whitespace-separated words equal to robot ignoring case in supplied text."


def run(args):
    project = "edge-software-"+uuid.uuid4().hex[:12]
    report = {"project":project,"passed":False,"stages":{},"image":args.image,"candidate_author":
              args.model if args.model_cache else "coding agent fixture","goal":GOAL}
    env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",NO_PROXY="localhost,127.0.0.1",no_proxy="localhost,127.0.0.1")
    def command(*cmd,fail=False):
        result = subprocess.run(cmd,capture_output=True,env=env,cwd=ROOT,encoding="utf-8",errors="replace",timeout=180)
        if bool(result.returncode)!=fail:
            raise RuntimeError((result.stdout+" "+result.stderr)[-1300:])
        return result.stdout.strip()
    def http(path,body=None):
        with urlopen(Request(base+path,data=None if body is None else json.dumps(body).encode(),
                             headers={"Content-Type":"application/json"}),timeout=5) as res:
            return json.load(res)
    def fetch(**filters):
        return http("/api/events?"+urlencode({"after":0,"limit":200,**filters}))["events"]
    def done(key):
        def query():
            t=http("/agent/tasks?id="+key)
            return t if t["state"] in ("SUCCEEDED","BLOCKED","RECOVERY_REQUIRED") else None
        return wait_for(query,key)
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        root = Path(temp)
        for p in ("package","inputs","registry","store","events/plugin.software","events/plugin.design","events/plugin.author"):
            (root/p).mkdir(parents=True)
        for name in ("contract.json","cases.json"):
            shutil.copyfile(SAMPLE/name,root/("package" if name=="contract.json" else "inputs")/name)
        if not args.model_cache:
            shutil.copyfile(SAMPLE/"candidate.py",root/"package/candidate.py")
        common={"image":args.image,"pull_policy":"never","cap_drop":["ALL"],"security_opt":["no-new-privileges:true"]}
        ros={"ROS_LOCALHOST_ONLY":"1","EDGE_EXTRA_PLUGIN_DIR":"","EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":""}
        shared=[mounted(root/"inputs","/inputs"),mounted(root/"package","/package"),
                mounted(root/"events/plugin.software","/e",read_only=False)]
        admin={"entrypoint":["python3","-B","/opt/edge/client/software_release.py"],"read_only":True}
        services={"agent":{**common,"command":["python3","/opt/edge/agent/service.py"],"environment":ros,
                          "volumes":["tasks:/var/lib/edge-tasks",mounted(root/"registry","/etc/edge/plugins")]},
                  "console":{**common,"command":["python3","/opt/edge/console/server/main.py"],"environment":ros,
                            "networks":["default","reader"],
                            "ports":["127.0.0.1::8080"],"volumes":["tasks:/var/lib/edge-tasks:ro",
                            mounted(root/"events","/var/lib/edge-module-sources"),
                            mounted(root/"events/plugin.author","/var/lib/edge-author-events")]},
                  "provider":{**common,"entrypoint":["python3","-IB","/opt/edge/client/software_provider.py","--package","/package"],
                             "read_only":True,"user":"65534:65534","mem_limit":"96m","cpus":0.5,"pids_limit":16,
                             "volumes":[mounted(root/"package","/package")]},
                  "admin":{**common,**admin,"volumes":[*shared,mounted(root/"registry","/registry",read_only=False),
                            mounted(root/"store","/store",read_only=False)]},
                  "author":{**common,**admin,"volumes":[shared[0],shared[2],mounted(root/"package","/package",read_only=False)]}}
        volumes={"tasks":{}}
        if args.model_cache:
            services["model"]={"image":MODEL_IMAGE,"pull_policy":"never","volumes":["cache:/cache:ro","model:/root/.ollama"]}
            volumes.update(cache={"external":True,"name":args.model_cache},model={})
        stack=root/"compose.json"
        stack.write_text(json.dumps({"services":services,"volumes":volumes,
                                   "networks":{"default":{"internal":True},"reader":{}}}),encoding="utf-8")
        def compose(*cmd,fail=False):
            return command("docker","compose","-p",project,"-f",str(stack),*cmd,fail=fail)
        def cli(action,*extra,fail=False):
            options=["--event","/inputs/event.json","--design","/inputs/design.json","--journal",
                     "/e/module-events.sqlite","--package","/package","--archive","http://console:8080"]
            if action!="generate":
                options.extend(["--endpoint","http://provider:8125/invoke"])
            if action in ("verify","publish"):
                options.extend(["--cases","/inputs/cases.json","--store","/store"])
            if action in ("remove","publish"):
                options.extend(["--registry","/registry","--request-id",action+"-word-tool"])
            return json.loads(compose("run","--no-deps","--rm","-T","author" if action=="generate" else "admin",
                                      action,*options,*extra,fail=fail))
        stub=ThreadingHTTPServer(("127.0.0.1",0),DeclineOdometry)
        thread=threading.Thread(target=stub.serve_forever)
        thread.start()
        try:
            report["image_id"]=command("docker","image","inspect","--format","{{.Id}}",args.image)
            compose("up","--no-build","-d","agent","console")
            base="http://"+compose("port","console","8080")
            wait_for(lambda:http("/agent/capabilities")["capabilities"],"Agent startup")
            agent_id=compose("ps","-q","agent")
            def started():
                return command("docker","inspect","-f","{{.State.StartedAt}}",agent_id)
            started_at=started()
            http("/agent/tasks",{"id":"word-gap","text":GOAL})
            wait_for(lambda:fetch(kind="agent.blocked",task_id="word-gap"),"original failure")
            previous=os.environ.get("EDGE_AUTHOR_EVENT_DB")
            os.environ["EDGE_AUTHOR_EVENT_DB"]=str(root/"events/plugin.author/module-events.sqlite")
            try:
                with closing(Watch(base,"http://127.0.0.1:"+str(stub.server_port),"fixture",root/"author")) as watch:
                    check(watch.poll()[0]["declined"]==1,"expected author decline")
            finally:
                if previous is None:
                    os.environ.pop("EDGE_AUTHOR_EVENT_DB",None)
                else:
                    os.environ["EDGE_AUTHOR_EVENT_DB"]=previous
            refusal=wait_for(lambda:fetch(kind="plugin.author.declined",task_id="word-gap"),"archive decline")[0]
            refusal_path=root/"author-event.json"
            refusal_path.write_text(json.dumps(refusal),encoding="utf-8")
            with closing(ModuleJournal(root/"events/plugin.design/module-events.sqlite")) as j:
                draft=design(refusal_path,root/"author/decisions"/(refusal["data"]["receipt_id"]+".json"),
                             root/"designs",j)
            shutil.copyfile(draft["path"],root/"inputs/design.json")
            origin=wait_for(lambda:fetch(kind="plugin.design.drafted",task_id="word-gap"),"archive design")[0]
            (root/"inputs/event.json").write_text(json.dumps(origin),encoding="utf-8")
            if args.model_cache:
                compose("run","--rm","--no-deps","-T","--entrypoint","/bin/sh","model","-ec",
                        "test -d /cache/models/blobs && mkdir -p /root/.ollama/models && cp -a /cache/models/. /root/.ollama/models/")
                compose("up","-d","model")
                wait_for(lambda:args.model in compose("exec","-T","model","ollama","list"),"Ollama startup")
                report["stages"]["generation"]=cli("generate","--ollama-url","http://model:11434","--model",args.model)
            # Only the container evaluates the candidate. Host statically checks a benign out-of-contract example.
            try:
                program(b"def transform(text):\n    return len(text)\n")
            except Invalid:
                pass
            else:
                raise AssertionError("unsupported function accepted")
            compose("up","-d","provider")
            verified=cli("verify")
            version=verified["version"]
            approved=verified["approval_sha256"]
            sample_file=root/"inputs/cases.json"
            first=sample_file.read_bytes()
            changed=json.loads(first)
            changed[0]["expected"]=100
            sample_file.write_text(json.dumps(changed),encoding="utf-8")
            check(cli("verify",fail=True)["reason"]=="software.case_mismatch","incorrect sample validated")
            refused=cli("publish","--approve",approved,"--expect","absent",fail=True)
            check(refused["reason"]=="software.approval_mismatch" and not list((root/"registry").glob("*.json")),
                  "stale authorization published")
            sample_file.write_bytes(first)
            applied=cli("publish","--approve",approved,"--expect","absent")
            p={"id":"word-active","text":TEXT,"trigger":{"kind":"user","source":"sample","id":"word-request"}}
            http("/agent/tasks",p)
            t=done("word-active")
            check(t["state"]=="SUCCEEDED" and len(t["steps"])==1,"registered task did not succeed")
            step=t["steps"][0]
            check(step["args"]=={"text":BODY} and step["evidence"]["value"]==2 and
                  step["evidence"]["input_sha256"]==digest(BODY.encode()),"text or count mismatch")
            http("/agent/tasks",{"id":"word-empty","text":"统计 robot 词频【】"})
            empty=done("word-empty")
            check(empty["state"]=="SUCCEEDED" and empty["steps"][0]["args"]=={"text":""}
                  and empty["steps"][0]["evidence"]["value"]==0,"empty original argument changed")
            probe=("import json,sys; from urllib.request import Request,urlopen; "
                   "req=Request('http://127.0.0.1:8125/invoke',data=sys.argv[1].encode(),"
                   "headers={'Content-Type':'application/json'}); print(json.load(urlopen(req))['code'])")
            for ver,arg,code in (("old","robot","software.version_mismatch"),
                                 (verified["declared_version"],"A"*4097,"software.input_invalid")):
                request={"version":ver,"operation_id":"direct-check","arguments":{"text":arg}}
                check(compose("exec","-T","provider","python3","-c",probe,json.dumps(request))==code,
                      "provider did not reject invalid input or version")
            check(http("/agent/tasks",p)["id"]==t["id"],"trigger replay changed identity")
            compose("stop","-t","2","provider")
            replay=cli("publish","--approve",approved,"--expect","absent")
            check(replay["event_id"]==applied["event_id"],"duplicate publish unsupported with stopped provider")
            removed=cli("remove","--expect",version)
            http("/agent/tasks",{"id":"word-removed","text":TEXT})
            check(done("word-removed")["state"]=="BLOCKED" and done("word-active")["steps"]==t["steps"]
                  and done("word-gap")["state"]=="BLOCKED" and started()==started_at,"removal changed past tasks")
            archived=wait_for(lambda:fetch(event_id=removed["event_id"]),"release archive")
            report["stages"].update(design_event=origin["event_id"],design_task=origin["task_id"],trace_id=origin["trace_id"],
                candidate_source=(root/"package/candidate.py").read_text(encoding="utf-8"),verify=verified,
                published=applied["event_id"],removed=archived[0]["event_id"],old_task=t["state"],
                input_sha256=step["evidence"]["input_sha256"],value=step["evidence"]["value"],
                original_operation=step["operation_id"],empty_value=empty["steps"][0]["evidence"]["value"],
                rejected=["software.approval_mismatch","software.case_mismatch","software.version_mismatch",
                          "software.input_invalid"],duplicate_publication_id=replay["event_id"],
                software_events=[{"kind":e["kind"],"event_id":e["event_id"],"parent":e["parent_event_id"]}
                  for e in fetch(source="journal:plugin.software",trace_id=origin["trace_id"])])
            check((root/"store"/(approved+".json")).is_file(),"candidate bundle missing")
            check(t["steps"][0]["version"]==verified["declared_version"],"execution used another version")
            report["passed"]=True
        except Exception as exc:
            report["error"]=str(exc)
            report["diagnostic_tail"]=compose("logs","--no-color","--tail","8","agent","console","provider")[-3000:]
            raise
        finally:
            stub.shutdown()
            thread.join()
            stub.server_close()
            try:
                compose("down","--volumes","--remove-orphans","--timeout","2")
            finally:
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--out",required=True,type=Path)
    parser.add_argument("--model-cache")
    parser.add_argument("--model",default="qwen2.5:1.5b")
    run(parser.parse_args())
