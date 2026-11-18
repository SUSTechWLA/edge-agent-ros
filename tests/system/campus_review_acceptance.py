"""SDD-031. Restore, interrupt and resume Session on a disposable campus console with a real task ledger."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
import uuid

ROOT=Path(__file__).resolve().parents[2]
HTTP=build_opener(ProxyHandler({}))


def run(image,out):
    project="edge-review-"+uuid.uuid4().hex[:12]
    env={k:v for k,v in os.environ.items() if not k.upper().startswith(("COMPOSE_","EDGE_","DEEPSEEK_"))}
    env.update(EDGE_IMAGE=image,EDGE_CAMPUS_CONSOLE_PORT="0",EDGE_ROS_DOMAIN_ID="149",COMPOSE_DISABLE_ENV_FILE="1")
    prefix=["docker","compose","-p",project,"-f","compose.yaml","-f","compose.campus.yaml"]
    def compose(*args):
        return subprocess.check_output(prefix+list(args),cwd=ROOT,env=env,timeout=90,text=True,encoding="utf-8").strip()
    def get(path,body=None):
        try:
            req=Request(base+path,data=json.dumps(body).encode() if body else None,headers={"Content-Type":"application/json"})
            with HTTP.open(req,timeout=8) as r:
                return r.status,json.load(r)
        except HTTPError as exc:
            return exc.code,json.load(exc)
    def until(f):
        for _ in range(90):
            try:
                result=f()
                if result:return result
            except (URLError,OSError):
                pass
            time.sleep(.35)
        raise AssertionError("condition did not converge")
    def session(readable):
        status,doc=get("/api/session?task_id="+key)
        return doc if (status==200)==readable else None
    report={"image":image,"project":project,"passed":False}
    try:
        compose("up","--no-deps","--no-build","-d","agent","console")
        base="http://"+compose("port","console","8080")
        until(lambda:get("/api/sessions")[0]==200)
        key="sdd031-"+uuid.uuid4().hex[:16]
        get("/agent/tasks",{"id":key,"text":"请执行未注册的校园测试工具"})
        until(lambda:any(t["id"]==key for t in get("/api/sessions")[1].get("tasks",[])))
        before=session(False)
        assert before["code"]=="session.service_missing"
        def ids():return {k:compose("ps","-q",k) for k in ("console","agent")}
        prior=ids()
        compose("up","--no-build","-d","session")
        first=until(lambda:session(True))
        compose("run","--rm","--no-deps","-T","session-admin","--if-missing")
        same=until(lambda:session(True))
        assert same["service"]==first["service"] and first["scope"]["task_id"]==key
        compose("stop","session")
        down=until(lambda:session(False))
        assert down["code"]!="session.service_missing"
        assert get("/api/review/tasks?limit=5")[0]==200 and get("/api/events?limit=3")[0]==200
        compose("start","session")
        recovered=until(lambda:session(True))
        assert recovered["scope"]==first["scope"] and recovered["service"]==first["service"]
        assert ids()==prior
        references=sum(len(message.get("refs",[])) for message in recovered["messages"])
        assert references>0
        report.update(passed=True,task=key,before=before["code"],outage=down["code"],scope=recovered["scope"],
                      binding=recovered["service"],references=references,
                      unchanged=list(prior),review_and_events_during_outage=True)
    finally:
        try:compose("--profile","*","down","--volumes","--remove-orphans","--timeout","3")
        finally:
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--image",required=True)
    p.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    run(args.image,args.out)
