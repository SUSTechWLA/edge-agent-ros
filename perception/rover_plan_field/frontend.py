"""LAN view and operator-only submission to the plan site's isolated Agent."""
from http.client import HTTPConnection, HTTPException
from http.server import ThreadingHTTPServer
from ipaddress import ip_address
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

import pose_window
import site_auth

WEB=Path("/opt/edge/console/web")
PAGES={"plan-observe.html":"text/html; charset=utf-8","plan-observe.css":"text/css","plan-observe.js":"text/javascript",
       "plan-scene.js":"text/javascript","plan-example.json":"application/json"}
FACT_ROUTE=re.compile(r"/(?:camera/(?:latest|[0-9a-f]{64})\.png|scene/(?:rgbd/(?:latest|[0-9a-f]{64})|[0-9a-f]{64})\.json)")
TERMINAL={"SUCCEEDED","FAILED","BLOCKED","REFUSED","CANCELED","CANCELLED","RECOVERY_REQUIRED"}
ACTIVE=set()
LOCK=threading.Lock()


def boundary_sample(iso_stamp, sample):
    try:
        boundary=datetime.fromisoformat(iso_stamp).timestamp()
        if abs(int(sample["boundary_received_ns"])/1e9-boundary)>1.5:
            return {"valid":False,"reason":"task_boundary_sample_missed"}
    except (ValueError,TypeError,KeyError):
        return {"valid":False,"reason":sample.get("reason","task_boundary_time_unavailable")}
    return sample


def observe_end(key):
    with LOCK:
        if key in ACTIVE:
            return
        ACTIVE.add(key)
    def run():
        try:
            for _ in range(2400):
                conn=HTTPConnection("agent",8090,timeout=3)
                try:
                    conn.request("GET","/agent/tasks?"+urlencode({"id":key,"limit":1}))
                    reply=conn.getresponse()
                    item=json.loads(reply.read())
                    if reply.status==200 and item.get("state") in TERMINAL:
                        pose_window.finish(key,boundary_sample(item.get("updated"),pose_window.current()))
                        return
                except (ValueError,OSError,HTTPException):
                    pass
                finally:
                    conn.close()
                time.sleep(.25)
            pose_window.finish(key,{"valid":False,"reason":"terminal_observation_timed_out"})
        finally:
            with LOCK:
                ACTIVE.discard(key)
    threading.Thread(target=run,daemon=True).start()


class Frontend(site_auth.Gateway):
    def forward(self):
        url=urlsplit(self.path)
        if ip_address(self.client_address[0]) not in site_auth.ADDRESS or self.headers.get("Host")!=site_auth.SITE:
            return self.reply(403,b"{}")
        if url.path=="/login" and not url.query:
            return self.login()
        role=self.role()
        if role not in ("viewer","operator"):
            if self.command=="GET" and url.path in ("/","/plan-observe.html"):
                return self.login_page()
            return self.reply(401,b"{}")
        if self.command=="GET":
            if url.path=="/field/recent-task":
                return self.reply(200,json.dumps({"id":pose_window.recent()}).encode())
            if url.path=="/field/pose-window":
                key=parse_qs(url.query).get("id",[""])[0]
                if not re.fullmatch(r"[\w.-]{1,96}",key):
                    return self.reply(400,b"{}")
                window=pose_window.get(key)
                return self.reply(200 if window else 404,json.dumps(window or {}).encode())
            page="plan-observe.html" if url.path=="/" else url.path[1:]
            if page in PAGES:
                return self.reply(200,(WEB/page).read_bytes(),PAGES[page])
        body=before=None
        if self.command=="POST":
            if (role!="operator" or self.path!="/agent/tasks" or
                    self.headers.get("Origin") not in (None,"http://"+site_auth.SITE)):
                return self.reply(403,b"{}")
            try:
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length",[]))!=1:
                    raise ValueError()
                length=int(self.headers["Content-Length"])
                if not 0<length<=8192 or self.headers.get_content_type()!="application/json":
                    raise ValueError()
                data=json.loads(self.rfile.read(length))
                if set(data)-{"id","text"} or not isinstance(data.get("text"),str):
                    raise ValueError()
                data.setdefault("id",uuid.uuid4().hex)
                if not isinstance(data["id"],str) or not re.fullmatch(r"[\w.-]{1,96}",data["id"]):
                    raise ValueError()
                body=json.dumps(data).encode()
                before=pose_window.current()
            except (ValueError,TypeError,KeyError):
                return self.reply(400,b"{}")
        elif url.path not in ("/agent/tasks","/agent/capabilities") and not FACT_ROUTE.fullmatch(url.path):
            return self.reply(404,b"{}")
        host,port=("facts",8150) if FACT_ROUTE.fullmatch(url.path) else ("agent",8090)
        conn=HTTPConnection(host,port,timeout=15)
        try:
            conn.request(self.command,self.path,body,{"Content-Type":"application/json"})
            resp=conn.getresponse()
            raw=resp.read(6_000_001)
            if len(raw)>6_000_000:
                return self.reply(502,b"{}")
            if before is not None and resp.status==202:
                item=json.loads(raw)
                key=item["id"]
                prev=pose_window.start(key,boundary_sample(item.get("created"),before))
                if prev["status"]=="pending":
                    observe_end(key)
            if url.path=="/agent/capabilities" and resp.status==200:
                data=json.loads(raw)
                data["capabilities"]=[c for c in data["capabilities"] if c["effect"]=="read"]
                raw=json.dumps(data,ensure_ascii=False).encode()
            return self.reply(resp.status,raw,resp.getheader("Content-Type","application/json"))
        except (OSError,HTTPException,ValueError):
            return self.reply(502,b'{"error":"site service unavailable"}')
        finally:
            conn.close()

    do_GET=forward
    do_POST=forward


if __name__=="__main__":
    for key in pose_window.unfinished():
        observe_end(key)
    ThreadingHTTPServer(("0.0.0.0",8089),Frontend).serve_forever()
