"""Serve the developer console. ROS executor and HTTP requests run independently."""
import http.client
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import rclpy

from history import TRACE_FIELDS, encode
from observer import Observer


def integer(params, key, default=None, ceiling=2**63-1):
    if key not in params:
        return default
    number = int(params[key][0])
    if number < 0 or number > ceiling:
        raise ValueError(f"{key} must be between 0 and {ceiling}")
    return number


class Handler(SimpleHTTPRequestHandler):
    server_version = "EdgeConsole/1"

    def __init__(self, *args, observer, **kw):
        self.observer = observer
        super().__init__(*args, directory=os.environ.get("EDGE_WEB_DIR","/opt/edge/console/web"), **kw)

    def log_message(self, fmt, *args):
        # Repeated telemetry fetches have no diagnostic value; keep HTTP failures.
        if args and len(args) >= 2 and str(args[1]).startswith(("4","5")):
            super().log_message(fmt, *args)

    def api_headers(self, status, mime, size=None, download=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if size is not None:
            self.send_header("Content-Length",str(size))
        if download:
            self.send_header("Content-Disposition",'attachment; filename="edge-events.ndjson"')
        self.end_headers()

    def respond(self, status, data):
        body=encode(data).encode("utf-8")
        self.send_payload(status,body)

    def send_payload(self, status, body):
        self.api_headers(status,"application/json; charset=utf-8",len(body))
        self.wfile.write(body)

    def agent(self, method, url):
        dest = urlsplit(os.environ.get("EDGE_AGENT_URL","http://agent:8090"))
        body=None
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin:
                source = urlsplit(origin)
                if source.scheme not in ("http","https") or source.netloc != self.headers.get("Host"):
                    self.respond(403,{"error":"cross-origin task request rejected"})
                    return
            if self.headers.get_content_type() != "application/json":
                self.respond(415,{"error":"application/json required"})
                return
            try:
                length=int(self.headers["Content-Length"])
            except (TypeError, ValueError):
                self.respond(411,{"error":"Content-Length required"})
                return
            if not 0 < length <= 81920:
                self.respond(413,{"error":"task request must be 1 to 81920 bytes"})
                return
            body=self.rfile.read(length)
        connection = None
        try:
            cls = http.client.HTTPSConnection if dest.scheme == "https" else http.client.HTTPConnection
            connection=cls(dest.hostname,dest.port,timeout=45)
            target=dest.path.rstrip("/") + url.path + (("?"+url.query) if url.query else "")
            connection.request(method,target,body=body,headers={"Content-Type":"application/json"})
            response=connection.getresponse()
            payload=response.read(1024*1024+1)
            if len(payload)>1024*1024:
                self.respond(502,{"error":"agent response exceeds 1 MiB"})
                return
            self.send_payload(response.status,payload)
        except TimeoutError:
            self.respond(504,{"error":"agent timed out; query task status by id before retrying"})
        except (OSError,http.client.HTTPException):
            self.respond(502,{"error":"mission agent unavailable"})
        finally:
            if connection is not None:
                connection.close()

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path in ("/agent/tasks","/agent/retry"):
            self.agent("POST",url)
        else:
            self.respond(404,{"error":"unknown endpoint"})

    def do_GET(self):
        url=urlsplit(self.path)
        if url.path in ("/agent/tasks","/agent/capabilities","/agent/plugins","/agent/services"):
            self.agent("GET",url)
            return
        if not url.path.startswith("/api/"):
            super().do_GET()
            return
        params=parse_qs(url.query)
        filters={k:params[k][0] for k in ("kind","mission_id","session_id","operation_id","level","source",*TRACE_FIELDS) if k in params}
        store=self.observer.history
        try:
            if url.path=="/api/state":
                self.respond(200,self.observer.snapshot())
            elif url.path=="/api/history":
                self.respond(200,store.retention())
            elif url.path=="/api/events":
                limit=integer(params,"limit",80,500)
                if limit==0:
                    raise ValueError("positive limit required")
                self.respond(200,store.page(integer(params,"after"),integer(params,"before"),limit,filters))
            elif url.path=="/api/replay":
                at=integer(params,"at")
                if at is None:
                    raise ValueError("at=event sequence required")
                frame=store.replay(at)
                self.respond(200 if frame else 404,frame or {"error":"event not found"})
            elif url.path=="/api/export":
                include = integer(params,"snapshot",0,1)
                self.api_headers(200,"application/x-ndjson; charset=utf-8",download=True)
                for line in store.export(filters,include_snapshot=bool(include)):
                    self.wfile.write(line)
            else:
                self.respond(404,{"error":"unknown endpoint"})
        except ValueError as error:
            self.respond(400,{"error":str(error)})
        except (ConnectionResetError,BrokenPipeError):
            pass

    def list_directory(self,path):
        self.respond(404,{"error":"not found"})
        return None


def main():
    rclpy.init()
    node=Observer()
    address=(os.environ.get("EDGE_HTTP_BIND","0.0.0.0"),int(os.environ.get("EDGE_HTTP_PORT","8080")))
    httpd=ThreadingHTTPServer(address, partial(Handler,observer=node))
    worker=threading.Thread(target=httpd.serve_forever,daemon=True)
    worker.start()
    stop = threading.Event()
    journals = threading.Thread(target=node.watch_journals, args=(stop,), name="module-journals")
    journals.start()
    print(f"Edge console listening on {address[0]}:{address[1]}",flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        journals.join()
        httpd.shutdown()
        httpd.server_close()
        worker.join()
        node.history.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__=="__main__":
    main()
