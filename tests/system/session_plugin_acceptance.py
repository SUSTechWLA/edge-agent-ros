"""Prove Session provider replacement and withdrawal through the existing plugin registry."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT=Path("/opt/edge") if Path("/opt/edge/client").is_dir() else Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"client"))
from register_session import register
import plugin_manager

HTTP=build_opener(ProxyHandler({}))


def request(url,body=None):
    req=Request(url,data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type":"application/json"})
    with HTTP.open(req,timeout=15) as response:
        return json.load(response)


class Alternate(BaseHTTPRequestHandler):
    """A separately deployed consumer/provider adapter; the console never imports it."""
    def log_message(self,*args):
        pass

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        forwarded={**body,"plugin_id":"session.default","plugin_version":"1"}
        result=request("http://session:8126/session",forwarded)
        result.update({key:body[key] for key in ("schema_version","request_id","plugin_id","plugin_version","binding")})
        result["document"]["headline"] += "（替代插件验收）"
        raw=json.dumps(result,ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run(url,project,registry,image,out):
    registry=registry.resolve()
    if not registry.is_relative_to((ROOT/".ci-results").resolve()):
        raise ValueError("Only a runner-owned registry may be changed")
    if not project.startswith("edge-test-"):
        raise ValueError("Only isolated edge-test projects may be exercised")
    api=lambda path:request(url.rstrip("/")+path)
    tasks=api("/api/review/tasks?limit=100")["tasks"]
    before=sorted((t["id"],t["state"]) for t in tasks)
    target=next(t for t in tasks if "navigation.path_blocked" in t["detail"])
    route="/api/session?"+urlencode({"task_id":target["id"]})
    baseline=api(route)
    assert baseline["service"]["plugin_id"]=="session.default"
    descriptor=json.loads((registry/"runtime/session.default.json").read_text(encoding="utf-8"))
    compose=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml")]
    cases={}
    def change(item):
        current,revision=plugin_manager.RuntimeRelease.installed(registry/"runtime","session.default")
        with tempfile.TemporaryDirectory() as folder:
            definition=Path(folder)/"session.default.json"
            definition.write_text(json.dumps(item),encoding="utf-8")
            key="session-test-"+uuid.uuid4().hex
            return plugin_manager.change(argparse.Namespace(action="publish",kind="runtime",root=registry,
                definition=definition,cases=None,expect=revision,trace_id=key,parent_event_id=None,request_id=key))
    def rejected(fragment):
        try:
            api(route)
        except HTTPError as error:
            body=json.load(error)
            assert error.code==503,body
            assert fragment in body.get("code",""),body
            assert api("/api/events?task_id="+target["id"]+"&limit=1")["events"]
            assert api("/agent/tasks?id="+target["id"])["state"]==target["state"]
            return body["code"]
        raise AssertionError("Unavailable session plugin silently returned a narrative")
    name=project+"-session-alt"
    started=False
    try:
        change({**descriptor,"draining":True})
        cases["draining"]=rejected("draining")
        change(descriptor)
        assert api(route)["headline"]==baseline["headline"]
        register(registry,remove=True)
        cases["uninstalled"]=rejected("missing")
        # The independently deployed evidence query does not need Session to read raw events.
        endpoint="http://"+subprocess.check_output([*compose,"port","evidence","8125"],cwd=ROOT,text=True).strip()
        raw=request(endpoint+"/invoke?tool=query_evidence",{"operation_id":"without-session-"+uuid.uuid4().hex,
            "version":"1","arguments":{"task_id":target["id"],"sql":"SELECT count(*) AS n FROM events",
                "after_seq":0,"limit":1,"archive_id":""}})
        assert raw["state"]=="SUCCEEDED" and raw["evidence"]["rows"][0][0]>0
        cases["evidence_without_session"]=True
        register(registry)
        subprocess.run([*compose,"stop","-t","2","session"],cwd=ROOT,check=True,capture_output=True)
        cases["provider_offline"]=rejected("transport")
        subprocess.run([*compose,"up","-d","--no-deps","--no-build","session"],cwd=ROOT,check=True,capture_output=True)
        deadline=time.monotonic()+20
        while True:
            try:
                api(route)
                break
            except HTTPError:
                if time.monotonic()>deadline: raise
                time.sleep(.15)
        change({**descriptor,"version":"unserved-version"})
        cases["wrong_version"]=rejected("identity_mismatch")
        change(descriptor)
        subprocess.run(["docker","run","-d","--name",name,"--network",project+"_default",
            "--read-only","--tmpfs","/tmp","--cap-drop","ALL","--entrypoint","python3",
            "--mount",f"type=bind,source={Path(__file__).resolve()},target=/fixture.py,readonly",
            image,"/fixture.py","--fixture"],cwd=ROOT,check=True,capture_output=True)
        started=True
        change({**descriptor,"version":"fixture-v2","endpoint":f"http://{name}:8127/session"})
        deadline=time.monotonic()+20
        while True:
            try:
                alternate=api(route)
                break
            except HTTPError:
                if time.monotonic()>deadline: raise
                time.sleep(.15)
        assert alternate["headline"].endswith("（替代插件验收）")
        assert alternate["service"]["plugin_version"]=="fixture-v2"
        assert alternate["service"]["binding"]!=baseline["service"]["binding"]
        assert alternate["task"]["state"]==baseline["task"]["state"]
        cases["separate_provider_replacement"]={"version":alternate["service"]["plugin_version"],
                                                 "binding_changed":True}
    finally:
        change(descriptor)
        subprocess.run([*compose,"up","-d","--no-deps","--no-build","session"],cwd=ROOT,check=True,capture_output=True)
        if started:
            subprocess.run(["docker","rm","-f",name],cwd=ROOT,capture_output=True)
    restored=api(route)
    assert restored["headline"]==baseline["headline"]
    for field in ("plugin_id","plugin_version","binding"):
        assert restored["service"][field]==baseline["service"][field]
    after=sorted((t["id"],t["state"]) for t in api("/api/review/tasks?limit=100")["tasks"])
    assert after==before
    report={"passed":True,"task_id":target["id"],"cases":cases,"restored_original_binding":True,
            "robot_tasks_unchanged":True,"task_count":len(before),"source":"isolated Gazebo and independent HTTP provider"}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture",action="store_true")
    p.add_argument("--url")
    p.add_argument("--project")
    p.add_argument("--registry",type=Path)
    p.add_argument("--image")
    p.add_argument("--out",type=Path)
    a=p.parse_args()
    if a.fixture:
        ThreadingHTTPServer(("0.0.0.0",8127),Alternate).serve_forever()
    else:
        run(a.url,a.project,a.registry,a.image,a.out)
