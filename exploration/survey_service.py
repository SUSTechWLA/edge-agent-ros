"""HTTP tool adapter for the isolated SDD-058 Gazebo profile."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
from pathlib import Path
import re
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import rclpy
from rclpy.executors import ExternalShutdownException

from exploration.survey_ros import Rover
from exploration.survey_session import ID, Survey


class Handler(BaseHTTPRequestHandler):
    def send(self, code, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/status":
            return self.send(404, {"error":"unknown_endpoint"})
        params = parse_qs(url.query, keep_blank_values=True)
        ids = params.get("task_id", [])
        if params and (set(params)!={"task_id"} or len(ids)!=1 or not ID.fullmatch(ids[0])):
            return self.send(400, {"error":"invalid_task_id"})
        return self.send(200, self.server.survey.show(ids[0] if ids else None))

    def do_POST(self):
        op = ""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 16384:
                return self.send(400, {"error":"invalid_request_size"})
            self.connection.settimeout(2)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise ValueError("expected_object")
            if self.path == "/intervene":
                if set(body) != {"task_id","anomaly_id","decision"}:
                    raise ValueError("invalid_operator_decision")
                return self.send(200, self.server.survey.intervene(body))
            if self.path not in ("/survey", "/navigate"):
                return self.send(404, {"error":"unknown_endpoint"})
            op = body["operation_id"]
            return self.send(200, self.server.survey.poll(self.path[1:], body))
        except (ValueError, TypeError, KeyError) as error:
            if self.path == "/intervene":
                return self.send(409, {"error":str(error)})
            return self.send(200, Survey.reply(op,"BLOCKED","仿真任务准入失败："+str(error)))
        except (TimeoutError, OSError):
            return self.send(503, {"error":"survey_dependency_unavailable"})


def main():
    if (os.environ.get("EDGE_SURVEY_MODE") != "isolated_gazebo"
            or not re.fullmatch(r"sdd058_[A-Za-z0-9_-]{1,30}", os.environ.get("GZ_PARTITION", ""))
            or not re.fullmatch(r"[0-9]{1,3}", os.environ.get("ROS_DOMAIN_ID", ""))):
        raise SystemExit("SDD-058 physical adapter is available only in isolated Gazebo")
    from exploration.site_config import site_config
    config = site_config(os.environ["EDGE_SURVEY_CONFIG"])
    if os.environ.get("EDGE_SURVEY_DYNAMIC_RECOVERY") == "1":
        timeout = int(os.environ.get("EDGE_SURVEY_CLEARANCE_TIMEOUT_S", "38"))
        if not 8<=timeout<=55:
            raise ValueError("clearance_review_timeout_outside_simulation_budget")
        config["clearance_timeout_s"] = timeout
    rclpy.init()
    rover = Rover("/sdd058/reset_mapping", "/sdd058/semantic/observation")
    survey = Survey(rover, config, os.environ["EDGE_SURVEY_STATE"], Path(os.environ["EDGE_SURVEY_REGISTRY"]),
                    os.environ["EDGE_SURVEY_AGENT"])
    server = ThreadingHTTPServer(("0.0.0.0",8139), Handler)
    server.survey = survey
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        while rclpy.ok():
            rover.spin()
            survey.tick()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception:
        logging.exception("survey loop unavailable")
        raise
    finally:
        server.shutdown()
        rover.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
