"""Front-end projection for an isolated DSH read site. All execution stays with the native Agent."""
import argparse
import hashlib
import json
import mimetypes
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def file_json(path):
    return json.loads(path.read_bytes())


def native(base, endpoint, body=None, **query):
    uri = base + "/agent/" + endpoint + ("?" + urlencode(query) if query else "")
    req = Request(uri, body, {"Content-Type":"application/json"})
    with urlopen(req, timeout=12) as result:
        raw = result.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("oversize native response")
        return result.status, json.loads(raw)


def trace_for(event):
    payload = event.get("payload") or {}
    if isinstance(payload,str):
        payload=json.loads(payload)
    return payload.get("trace",{})


def project(o):
    config=(o.config/"implementations.json").read_bytes()
    contract, = json.loads(config)["contracts"].values()
    options=contract["implementations"]
    available=native(o.agent,"capabilities")[1]["records"]
    by_id={r["id"]:r for r in available}
    providers=[]
    for i, entry in enumerate(options):
        row=by_id.get(entry["tool"],{})
        ready=(row.get("provider")==entry["provider"] and row.get("binding")==entry["binding"]
               and row.get("availability")=="available" and not row.get("draining"))
        providers.append({"id":entry["provider"], "tool":entry["tool"], "label":entry["tool"], "origin":entry["origin"],
                          "available":ready, "preferred":i==0, "rank":i+1, "binding":entry["binding"]})
    events=[]
    if o.history.is_file():
        with o.history.open(encoding="utf8") as f:
            for line in f:
                try:
                    item=json.loads(line)
                    datetime.fromisoformat(item["utc"].replace("Z","+00:00"))
                except (KeyError,ValueError):
                    continue
                if item.get("kind") in ("site.preference","site.drain","site.remove","site.restored"):
                    events.append({k:item[k] for k in ("utc","kind","detail","provider","source_ref","policy_sha256") if k in item})
    frame=file_json(o.frame)
    frame["frame_ref"]="sha256:"+hashlib.sha256(o.frame.read_bytes()).hexdigest()
    runs=[]
    for item in reversed(native(o.agent,"task-index",limit=32)[1].get("tasks",[])):
        task=native(o.agent,"tasks",id=item["id"],limit=140)[1]
        if task.get("trigger",{}).get("kind")!="user":
            continue
        tid=task["id"]
        run={"task_id":tid,"text":task["text"],"state":task["state"],"created":task["created"]}
        runs.append(run)
        events.append({"utc":task["created"],"task_id":tid,"kind":"submitted","detail":task["text"]})
        for event in task.get("events",[]):
            kind=event["kind"]
            tr=trace_for(event)
            traceable=tr.get("event_id")==event.get("event_id") and tr.get("trace_id")==task["trace_id"]
            if not traceable:
                continue
            source=event["event_id"]
            extra={}
            if kind.startswith("planner.") and ("implement" in kind or "capability_selected" in kind):
                run["selection"]={k:v for k,v in tr.items() if k in (
                    "contract","origin","selected","tool","binding","provider","reason","candidates","skipped","policy_sha256",
                    "index_digest","index_sha256","rank","rejected","priority","selected_provider","excluded","considered")}
                choices=tr.get("candidates",[])
                selected=next((row for row in choices if row.get("eligible") and row.get("tool")==tr.get("tool")),None)
                detail="站点核对已授权绑定、目录可用性和优先级"
                if selected:
                    extra={k:selected[k] for k in ("provider","origin","binding")}
                    skipped=[row["provider"] for row in choices[:choices.index(selected)] if not row.get("eligible")]
                    detail=(("优先实现 "+ "、".join(skipped)+" 当前不可选，自动改选 ") if skipped else "按当前站点优先级选中 ")+selected["provider"]
                elif choices and not any(row.get("eligible") for row in choices):
                    detail="当前没有可接收此任务的已授权实现"
            elif kind=="planner.graph_composed":
                run.update(graph=tr["graph"],graph_model=tr["provider"],graph_event_id=source)
                extra={"graph":tr["graph"],"provider":tr["provider"]}
                detail="本地模型生成类型化步骤图，规划器已校验各节点端口"
            elif kind in ("planner.graph_rejected","tool.error","blocked","retry","retry_exhausted"):
                detail=tr.get("reason") or event.get("detail") or kind
            elif kind=="tool.request":
                detail="Agent 已按冻结绑定派发只读工具"
            elif kind=="verified":
                detail="Agent 已核验工具结果"
                run.update(receipt_id=source,verified_utc=event["utc"])
            else:
                continue
            events.append({"utc":event["utc"],"kind":kind,"task_id":tid,"detail":str(detail)[:400],"source_ref":source,**extra})
        for step in task.get("steps",[]):
            pin=step.get("soft_pin") or {}
            # Historical runs own their original selection, including after an implementation is removed.
            selected=run.get("selection",{})
            match=next((x for x in selected.get("candidates",[]) if x.get("eligible")
                        and selected.get("tool")==x.get("tool")==step.get("tool")
                        and x.get("provider")==pin.get("plugin_id")
                        and x.get("binding")==pin.get("binding")==step.get("binding")),None)
            if not match:
                continue
            run.update(tool=step["tool"],operation_id=step.get("operation_id"),provider=pin["plugin_id"],
                       binding=step["binding"],version=pin["version"],content_digest=pin["content_digest"],
                       origin=match["origin"],args=step["args"])
            facts=step.get("evidence") or {}
            if facts.get("frame_ref")==frame["frame_ref"] and step.get("status")=="SUCCEEDED":
                analysis=facts.get("analysis") or {}
                if all(analysis.get(k)==frame[k] for k in ("rgb_ref","depth_ref")):
                    run.update(frame={k:frame[k] for k in ("rgb_ref","depth_ref","frame_ref","depth_stamp_ns")},
                               review=facts.get("review"),graph_receipts=facts.get("graph_receipts",[]))
        if task["state"] in ("FAILED","BLOCKED","RECOVERY_REQUIRED"):
            run["detail"]=task.get("detail","")
    return {"schema_version":1,"mode":"live","frame":frame,"providers":providers,"runs":runs,
            "events":sorted(events,key=lambda e:e["utc"]),"policy_sha256":"sha256:"+hashlib.sha256(config).hexdigest(),
            "submit_path":"/review-api/tasks"}


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self,status,body,mime):
        self.send_response(status)
        for key,value in [("Content-Type",mime),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff"),
                          ("Content-Length",str(len(body)))]: self.send_header(key,value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self,status,obj):
        self.send_bytes(status,json.dumps(obj,ensure_ascii=False,allow_nan=False).encode(),"application/json; charset=UTF-8")

    def do_POST(self):
        if self.path!="/review-api/tasks":
            return self.send_error(404)
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=16000 or self.headers.get_content_type()!="application/json":
                return self.send_error(400)
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict) or set(data)!={"id","text"} or not isinstance(data["id"],str) or not re.fullmatch(
                    r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}",data["id"]) or not isinstance(data["text"],str) or not 0<len(data["text"])<=3500:
                return self.send_error(400)
            status,doc=native(self.server.options.agent,"tasks",json.dumps(data,ensure_ascii=False).encode())
            self.send_json(status,{k:doc[k] for k in ("id","state","created","detail") if k in doc})
        except HTTPError as e:
            self.send_json(e.code,{"error":"Agent rejected request"})
        except ValueError:
            self.send_error(400)
        except OSError:
            self.send_error(503)

    def do_GET(self):
        p=urlsplit(self.path).path
        o=self.server.options
        if p=="/records/review-compare/manifest.json":
            try:
                return self.send_json(200,project(o))
            except (OSError,ValueError,KeyError) as exc:
                return self.send_json(503,{"error":type(exc).__name__})
        if p in ("/","/review-compare.html","/review-compare.js","/review-compare-media.js","/review-compare.css"):
            path=o.web/(p[1:] or "review-compare.html")
        elif re.fullmatch(r"/records/review-compare/[0-9a-f]{64}\.png",p):
            path=o.frame.parent/Path(p).name
        elif re.fullmatch(r"/records/review-compare/archives/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(?:manifest\.json|[0-9a-f]{64}\.png)",p):
            path=o.web/p.lstrip("/")
        else:
            return self.send_error(404)
        try:
            data=path.read_bytes()
            if path.suffix==".png" and hashlib.sha256(data).hexdigest()!=path.stem:
                return self.send_error(409)
            self.send_bytes(200,data,mimetypes.guess_type(path)[0] or "application/octet-stream")
        except FileNotFoundError:
            self.send_error(404)

    def log_message(self,*args):
        pass


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("web","history","frame","config"):
        p.add_argument("--"+k,type=Path,required=True)
    p.add_argument("--agent",default="http://agent:8090")
    p.add_argument("--port",type=int,default=8080)
    opts=p.parse_args()
    server=ThreadingHTTPServer(("0.0.0.0",opts.port),Handler)
    server.options=opts
    server.serve_forever()
