"""Simulation workbench projection and operator decision proxy."""

import http.client
import json
import os
import re
from urllib.parse import urlencode, urlsplit


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DECISION_BYTES = 4096


def _task_id(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _target():
    value = os.environ.get("EDGE_SURVEY_URL", "")
    try:
        endpoint = urlsplit(value)
        endpoint.port
    except ValueError:
        return None
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.path not in {"", "/"}
            or endpoint.query or endpoint.fragment or endpoint.username or endpoint.password):
        return None
    return endpoint


def _relay(handler, endpoint, method, suffix, body=None):
    connection_cls = (http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection)
    connection = connection_cls(endpoint.hostname, endpoint.port, timeout=3)
    try:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, suffix, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("exploration response too large")
        content = json.loads(payload)
        if not isinstance(content, dict):
            raise ValueError("invalid exploration response")
    except (OSError, ValueError, http.client.HTTPException):
        handler.respond(502, {"code": "exploration.provider_unavailable", "error": "探索服务暂不可用"})
        return
    finally:
        connection.close()
    handler.respond(response.status, content)


def _deployment(handler, *, write=False):
    if handler.observer.environment != "simulation":
        return None
    if write and os.environ.get("EDGE_CONSOLE_OBSERVE_ONLY") == "1":
        return None
    return _target()


def read_mission(handler, params):
    endpoint = _deployment(handler)
    if endpoint is None:
        handler.respond(404, {"code": "exploration.disabled", "error": "此站点未开放仿真探索工作台"})
        return
    query = ""
    if params:
        ids = params.get("task_id", [])
        if set(params) != {"task_id"} or len(ids) != 1 or not _task_id(ids[0]):
            handler.respond(400, {"code": "exploration.task_id_invalid", "error": "请提供一个有效任务 ID"})
            return
        query = "?" + urlencode({"task_id": ids[0]})
    _relay(handler, endpoint, "GET", "/status" + query)


def decide(handler):
    endpoint = _deployment(handler, write=True)
    if endpoint is None:
        handler.respond(403, {"code": "exploration.observe_only", "error": "此站点未开放异常处理"})
        return
    origin = handler.headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            valid_origin = parsed.scheme in {"http", "https"} and parsed.netloc == handler.headers.get("Host")
        except ValueError:
            valid_origin = False
        if not valid_origin:
            handler.respond(403, {"code": "exploration.origin_mismatch", "error": "当前页面来源无权处理异常"})
            return
    try:
        if handler.headers.get_content_type() != "application/json":
            raise ValueError("expected JSON")
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_DECISION_BYTES:
            raise ValueError("invalid request length")
        body = json.loads(handler.rfile.read(length))
        if (not isinstance(body, dict) or set(body) != {"task_id", "anomaly_id", "decision"}
                or not _task_id(body["task_id"]) or not _task_id(body["anomaly_id"])
                or body["decision"] not in ("skip", "retry")):
            raise ValueError("invalid decision")
    except (ValueError, OSError):
        handler.respond(400, {"code": "exploration.decision_invalid", "error": "异常处理请求无效"})
        return
    _relay(handler, endpoint, "POST", "/intervene", json.dumps(body).encode("utf-8"))
