"""Serve the developer console. ROS executor and HTTP requests run independently."""
import http.client
import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import rclpy

from history import TRACE_FIELDS, encode
from campus_planning import request_preview
import campus_atlas
from exploration_mission import decide as decide_exploration, read_mission as read_exploration
from observer import Observer
from session_service import SessionUnavailable, read_session
from task_data import FactsQueryError, FactsUnavailable
from edge_contracts.trace_content import sanitized


def integer(params, key, default=None, ceiling=2**63-1):
    if key not in params:
        return default
    number = int(params[key][0])
    if number < 0 or number > ceiling:
        raise ValueError(f"{key} must be between 0 and {ceiling}")
    return number


def campus_mission_views():
    def url(setting, path):
        port = os.environ.get(setting, "")
        if len(port) <= 5 and port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535:
            return f"http://127.0.0.1:{int(port)}{path}"
        return None

    return {"campus_2d": url("EDGE_CAMPUS_MAP_PORT", "/?view=2d"),
            "campus_3d": url("EDGE_CAMPUS_MAP_PORT", "/?view=3d&place=teaching"),
            "overview": url("EDGE_CAMPUS_OVERVIEW_PORT", "/"),
            "recording": "/#review"}


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

    def json_request_body(self, maximum):
        origin = self.headers.get("Origin")
        if origin:
            source = urlsplit(origin)
            if source.scheme not in ("http", "https") or source.netloc != self.headers.get("Host"):
                self.respond(403, {"error": "cross-origin request rejected"})
                return None
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "application/json required"})
            return None
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            self.respond(411, {"error": "Content-Length required"})
            return None
        if not 0 < length <= maximum:
            self.respond(413, {"error": f"request must be 1 to {maximum} bytes"})
            return None
        return self.rfile.read(length)

    def agent(self, method, url):
        if os.getenv("EDGE_CONSOLE_OBSERVE_ONLY") == "1":
            self.respond(403, {"code":"console.observe_only","error":"当前工作台只读，未连接任务代理"})
            return
        dest = urlsplit(os.environ.get("EDGE_AGENT_URL","http://agent:8090"))
        body=None
        if method == "POST":
            body = self.json_request_body(81920)
            if body is None:
                return
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
        elif url.path=="/api/exploration-mission/intervene" and not url.query:
            decide_exploration(self)
        elif url.path == "/api/calibration-board-geometry" and not url.query:
            self.calibration_workstation("/board-geometry", {})
        else:
            self.respond(404,{"error":"unknown endpoint"})

    def do_GET(self):
        url=urlsplit(self.path)
        if url.path == "/api/calibration-motion-preflight" and not url.query:
            self.calibration_motion_preflight()
            return
        if url.path == "/api/calibration-live-frame" and not url.query:
            self.calibration_live_frame()
            return
        if url.path in ("/api/calibration-status", "/api/calibration-clock") and not url.query:
            self.calibration_status(clock=url.path == "/api/calibration-clock")
            return
        if url.path in ("/api/calibration-preview.jpg", "/api/calibration-capture.avi"):
            route = "/preview.jpg" if url.path.endswith(".jpg") else "/capture.avi"
            self.calibration_workstation(route, parse_qs(url.query, keep_blank_values=True))
            return
        if url.path in ("/agent/tasks","/agent/capabilities","/agent/plugins","/agent/services"):
            self.agent("GET",url)
            return
        if campus_atlas.serve(self,url.path):
            return
        if not url.path.startswith("/api/"):
            super().do_GET()
            return
        params=parse_qs(url.query,keep_blank_values=True)
        filters={k:params[k][0] for k in ("kind","mission_id","session_id","operation_id","level","source",*TRACE_FIELDS) if k in params}
        store=self.observer.history
        try:
            if "exclude_kind_prefixes" in params:
                prefixes=params["exclude_kind_prefixes"][0].split(",")
                if (len(params["exclude_kind_prefixes"])!=1 or len(prefixes)>8
                        or any(not 1 <= len(prefix) <= 128 for prefix in prefixes)):
                    raise ValueError("exclude_kind_prefixes requires 1 to 8 prefixes of 1 to 128 characters")
                filters["exclude_kind_prefixes"]=prefixes
            if "view" in params and url.path in ("/api/events","/api/export","/api/event-stats"):
                view=params["view"][0]
                if view not in ("all","issues","changes"):
                    raise ValueError("view must be all, changes or issues")
                filters["view"]=view
            if url.path=="/api/state":
                snapshot = self.observer.snapshot()
                if os.getenv("EDGE_CALIBRATION_URL"):
                    snapshot["calibration_status_available"] = True
                snapshot["task_proxy_mode"] = "observe" if os.getenv("EDGE_CONSOLE_OBSERVE_ONLY")=="1" else "agent"
                self.respond(200,snapshot)
            elif url.path=="/api/campus-mission/views":
                self.respond(200,campus_mission_views())
            elif url.path=="/api/exploration-mission":
                read_exploration(self,params)
            elif url.path=="/api/campus-mission/local-preview":
                if set(params) != {"task_id"} or len(params["task_id"]) != 1:
                    raise ValueError("provide one task ID")
                self.respond(200,request_preview(self.observer,params["task_id"][0]))
            elif url.path=="/api/task-detail":
                if set(params) - {"id","after","limit"} or not params.get("id",[""])[0] or any(
                        len(values) != 1 for values in params.values()):
                    raise ValueError("provide one task id and optional event cursor and limit")
                if os.getenv("EDGE_CONSOLE_OBSERVE_ONLY") != "1":
                    self.agent("GET",url._replace(path="/agent/tasks"))
                    return
                try:
                    task = self.observer.facts.get("tasks", {key:values[0] for key,values in params.items()})
                except FactsUnavailable:
                    self.respond(503,{"code":"facts.unavailable","error":"只读任务服务暂时不可用。"})
                    return
                self.respond(200,task)
            elif url.path in ("/api/review/tasks","/api/sessions"):
                try:
                    query = {"state":params.get("state", ["all"])[0], "limit":integer(params, "limit", 30, 100)}
                    if "before" in params:
                        query["before"] = params["before"][0]
                    page = self.observer.facts.get("task-index", query)
                except FactsUnavailable:
                    self.respond(503,{"error":"任务账本暂时不可读，请检查 Agent 连接。"})
                    return
                if url.path=="/api/sessions" and os.getenv("EDGE_CONSOLE_OBSERVE_ONLY") == "1":
                    for row in page["tasks"]:
                        for field in ("text", "detail"):
                            if isinstance(row.get(field), str):
                                row[field] = sanitized(row[field])
                self.respond(200,page)
            elif url.path in ("/api/session","/api/task-context"):
                if any(len(params[key])!=1 for key in ("task_id","trace_id","format") if key in params):
                    raise ValueError("each task, trace or format parameter must occur once")
                task_id,trace_id=params.get("task_id",[None])[0],params.get("trace_id",[None])[0]
                if url.path=="/api/task-context":
                    paging = {}
                    for key in ("after", "limit", "source_id"):
                        if key in params:
                            if len(params[key]) != 1:
                                raise ValueError("each pagination parameter must occur once")
                            paging[key] = params[key][0]
                    self.respond(200,self.observer.facts.context(task_id,trace_id,**paging))
                    return
                output=params.get("format",["json"])[0]
                if output not in ("json","markdown"):
                    raise ValueError("format must be json or markdown")
                try:
                    document=read_session(task_id,trace_id)
                except SessionUnavailable as error:
                    result = {"error":str(error),"code":error.code,"details":error.details}
                    if os.getenv("EDGE_CONSOLE_OBSERVE_ONLY") == "1":
                        result["observe_only"] = True
                    self.respond(error.status,result)
                    return
                if output=="markdown":
                    body=document["markdown"].encode("utf-8")
                    self.api_headers(200,"text/markdown; charset=utf-8",len(body))
                    self.wfile.write(body)
                else:
                    self.respond(200,document)
            elif url.path=="/api/history":
                self.respond(200,store.retention())
            elif url.path=="/api/event-stats":
                self.respond(200,store.stats(filters,integer(params,"before")))
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
        except FactsQueryError as error:
            self.respond(error.status, {"error":error.details.get("detail", str(error)), **error.details})
        except ValueError as error:
            self.respond(400,{"error":str(error)})
        except (ConnectionResetError,BrokenPipeError):
            pass

    def list_directory(self,path):
        self.respond(404,{"error":"not found"})
        return None

    def station_request(self, endpoint, body=None):
        dest = urlsplit(os.environ["EDGE_CALIBRATION_URL"])
        if dest.scheme not in ("http", "https") or not dest.hostname or dest.query or dest.fragment:
            raise ValueError("invalid calibration source")
        transport = http.client.HTTPSConnection if dest.scheme == "https" else http.client.HTTPConnection
        conn = transport(dest.hostname, dest.port, timeout=5)
        try:
            conn.request("POST" if body is not None else "GET", dest.path.rstrip("/") + endpoint,
                         body=body, headers={"Content-Type": "application/json"} if body is not None else {})
            return conn, conn.getresponse()
        except Exception:
            conn.close()
            raise

    def read_station_status(self, clock=False):
        conn, response = self.station_request("/clock-status" if clock else "/status")
        try:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError("calibration status unavailable")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("state"), str):
                raise ValueError("invalid calibration status")
            if not clock and (not isinstance(data.get("message"), str) or data["state"] not in (
                    "NEEDS_CALIBRATION", "CALIBRATING", "VERIFIED", "RECOVERY_REQUIRED", "PAUSED_FOR_OPERATOR")):
                raise ValueError("invalid spatial calibration status")
            if data.get("robot_id") != self.observer.robot_id or data.get("environment") != self.observer.environment:
                raise ValueError("calibration station identity mismatch")
            return data
        finally:
            conn.close()

    def calibration_status(self, clock=False):
        if not os.getenv("EDGE_CALIBRATION_URL"):
            self.respond(404, {"error": "此站未安装标定服务"})
            return
        try:
            data = self.read_station_status(clock)
            keys = (("state", "revision", "spatial_revision", "clock_epoch", "sources", "reason", "message")
                    if clock else ("state", "message", "active_operation", "phase", "revision", "scope",
                                   "unobserved", "samples", "task_id", "method", "methods", "board_geometry",
                                   "capture_feedback", "sensor_frames", "camera_result"))
            self.respond(200, {key: data.get(key) for key in keys})
        except (OSError, ValueError, http.client.HTTPException):
            self.respond(503, {"error": "暂时无法读取当前装配的标定状态"})

    def calibration_motion_preflight(self):
        if not os.getenv("EDGE_CALIBRATION_URL") or self.observer.environment != "hardware":
            return self.respond(404, {"error": "此站未接入底盘试动预检"})
        path = os.getenv("EDGE_CALIBRATION_MOTION_PREFLIGHT",
                         "/opt/edge/console/field-state/motion-preflight.json")
        try:
            with open(path, "rb") as source:
                payload = source.read(32769)
            if len(payload) > 32768:
                raise ValueError("preflight record too large")
            record = json.loads(payload)
            if (record["schema"] != "edge.calibration.motion-preflight.v1" or
                    record["robot_id"] != self.observer.robot_id or
                    record["status"] not in ("blocked", "unavailable", "ready") or
                    not isinstance(record["task_id"], str) or not record["task_id"] or
                    not isinstance(record["checks"], list) or len(record["checks"]) > 32):
                raise ValueError("invalid preflight")
            checks = []
            for item in record["checks"]:
                if (type(item["ok"]) not in (bool, type(None)) or
                        not isinstance(item["label"], str) or not isinstance(item["detail"], str)):
                    raise ValueError("invalid check")
                checks.append({k: item[k] for k in ("label", "ok", "detail")})
            if self.read_station_status().get("task_id") != record["task_id"]:
                return self.respond(404, {"error": "当前任务没有底盘试动预检"})
            age = (time.time_ns() - int(record["observed_at_ns"])) // 1_000_000
            lifetime = min(30000, max(0, int(record["valid_for_ms"])))
            fresh = 0 <= age < lifetime
            test = record.get("last_test")
            if test is None:
                receipt_path = os.path.join(os.path.dirname(path), "last-motion-test.json")
                try:
                    with open(receipt_path, "rb") as source:
                        payload = source.read(8193)
                    if len(payload) > 8192:
                        raise ValueError("motion test summary too large")
                    receipt = json.loads(payload)
                    if (receipt.get("schema") == "edge.calibration.motion-test-summary.v1" and
                            receipt.get("robot_id") == record["robot_id"] and
                            receipt.get("task_id") == record["task_id"]):
                        test = receipt
                except FileNotFoundError:
                    pass
            if test is not None:
                if not isinstance(test, dict) or any(not isinstance(test.get(k), str)
                                                      for k in ("status", "summary")):
                    raise ValueError("invalid test receipt")
                test = {k: test[k] for k in ("status", "summary")}
            self.respond(200, {"schema": record["schema"], "robot_id": record["robot_id"],
                               "task_id": record["task_id"], "observed_at_ns": str(record["observed_at_ns"]),
                               "valid_for_ms": lifetime, "age_ms": age, "fresh": fresh,
                               "status": record["status"] if fresh else "unavailable",
                               "observed_status": record["status"], "checks": checks, "last_test": test})
        except FileNotFoundError:
            self.respond(404, {"error": "此站未接入底盘试动预检"})
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            self.respond(503, {"error": "暂时无法核对底盘试动预检"})

    def calibration_live_frame(self):
        if not os.getenv("EDGE_CALIBRATION_URL"):
            return self.respond(404, {"error":"此站未安装标定服务"})
        conn = None
        try:
            conn, response = self.station_request("/live-frame")
            raw = response.read(360001)
            if response.status != 200 or len(raw) > 360000:
                raise ValueError("camera unavailable")
            data = json.loads(raw)
            if (data.get("available") is not True or data.get("source") != "live_camera" or
                    data.get("robot_id") != self.observer.robot_id or data.get("environment") != self.observer.environment or
                    not isinstance(data.get("jpeg"),str) or not data["jpeg"] or data.get("max_age_ms") != 1500):
                raise ValueError("unexpected live camera")
            age = (time.time_ns() - int(data["received_unix_ns"]))//1_000_000
            if not 0 <= age <= data["max_age_ms"]:
                raise ValueError("expired live camera frame")
            self.respond(200,{**data,"age_ms":age})
        except (OSError, ValueError, TypeError, KeyError, AttributeError, http.client.HTTPException):
            self.respond(503, {"code":"camera_unavailable","error":"当前没有新鲜相机帧"})
        finally:
            if conn:
                conn.close()

    def calibration_workstation(self, route, params):
        if not os.getenv("EDGE_CALIBRATION_URL"):
            self.respond(404, {"error": "此站未安装标定服务"})
            return
        body = None
        if route == "/board-geometry":
            if os.getenv("EDGE_CONSOLE_OBSERVE_ONLY") == "1":
                self.respond(403, {"error": "当前工作台只读，无法登记标定板"})
                return
            body = self.json_request_body(4096)
            if body is None:
                return
        conn = None
        media_started = False
        try:
            status = self.read_station_status()
            if body is None:
                task = params.get("task_id", [])
                if len(task) != 1 or not task[0] or task[0] != status.get("task_id"):
                    self.respond(404, {"error": "当前标定任务没有这份记录"})
                    return
                route += "?" + urlencode({"task_id": task[0]})
            conn, response = self.station_request(route, body)
            if body is not None or response.status != 200:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("oversized station response")
                data = json.loads(raw)
                self.respond(response.status, data)
            else:
                is_image = route.startswith("/preview.jpg?")
                limit = 2097152 if is_image else 67108864
                size = int(response.getheader("Content-Length") or 0)
                if not 0 < size <= limit:
                    raise ValueError("invalid recording size")
                self.api_headers(200, "image/jpeg" if is_image else "video/x-msvideo", size)
                media_started = True
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            if media_started:
                self.close_connection = True
            else:
                self.respond(503, {"error": "暂时无法读取当前标定工位"})
        finally:
            if conn:
                conn.close()


def main():
    rclpy.init()
    node=Observer()
    address=(os.environ.get("EDGE_HTTP_BIND","0.0.0.0"),int(os.environ.get("EDGE_HTTP_PORT","8080")))
    httpd=ThreadingHTTPServer(address, partial(Handler,observer=node))
    httpd.campus_atlas = campus_atlas.load(os.getenv("EDGE_CAMPUS_WORLD_DIR")) if node.environment == "simulation" else None
    worker=threading.Thread(target=httpd.serve_forever,daemon=True)
    worker.start()
    stop = threading.Event()
    journals = node.journal_workers(stop)
    for journal in journals:
        journal.start()
    print(f"Edge console listening on {address[0]}:{address[1]}",flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for journal in journals:
            journal.join()
        httpd.shutdown()
        httpd.server_close()
        worker.join()
        node.history.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__=="__main__":
    main()
