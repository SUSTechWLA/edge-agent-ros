"""Bounded read-only client for task facts owned by the Agent."""
import http.client
import json
import os
import socket
import threading
from urllib.parse import urlencode, urlsplit


class FactsUnavailable(Exception):
    """Unable to complete a read; leave the durable archive cursor unchanged."""


class FactsQueryError(Exception):
    def __init__(self, status, details):
        super().__init__(details.get("code", "facts.invalid_request"))
        self.status = status
        self.details = details


def identifiers(task_id=None, trace_id=None):
    if (task_id is None) == (trace_id is None):
        raise ValueError("provide exactly one task_id or trace_id")
    value = task_id if task_id is not None else trace_id
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("task or trace identifier must be 1 to 512 printable characters")
    return {"task_id":task_id} if task_id is not None else {"trace_id":trace_id}


class AgentFacts:
    def __init__(self, url=None):
        self.url = urlsplit(url or os.getenv("EDGE_FACTS_URL") or os.getenv("EDGE_AGENT_URL", "http://agent:8090"))
        if (self.url.scheme not in ("http", "https") or not self.url.hostname or self.url.username
                or self.url.password or self.url.query or self.url.fragment):
            raise ValueError("the task facts source must be a fixed HTTP(S) service URL")
        self.sources = {}

    def get(self, route, params):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        connection = cls(self.url.hostname, self.url.port, timeout=1.5)
        limit = 4 * 1024 * 1024
        transport = None
        def interrupt_read():
            sock = transport or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(2.0, interrupt_read)
        timer.daemon = True
        timer.start()
        try:
            path = self.url.path.rstrip("/") + "/agent/" + route
            connection.request("GET", path + "?" + urlencode(params), headers={"Accept":"application/json"})
            transport = connection.sock
            reply = connection.getresponse()
            raw = reply.read(limit + 1)
            if len(raw) > limit or reply.length not in (None, 0):
                raise FactsUnavailable("invalid_size")
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                raise FactsUnavailable("invalid_response") from None
            if reply.status in (400, 409, 413) or (route == "tasks" and reply.status == 404):
                raise FactsQueryError(reply.status, value)
            if reply.status != 200:
                raise FactsUnavailable("absent" if value.get("reason") == "absent" else "unavailable")
            return value
        except (OSError, http.client.HTTPException):
            raise FactsUnavailable("unreachable") from None
        finally:
            timer.cancel()
            connection.close()

    def context(self, task_id=None, trace_id=None, **paging):
        params = identifiers(task_id, trace_id)
        try:
            return self.get("task-context", {**params, **paging})
        except FactsUnavailable:
            return {"schema_version":1, "scope":{"task_id":task_id, "trace_id":trace_id},
                    "task":None, "related_tasks":[], "related_tasks_has_more":False,
                    "task_available":False, "event_window":None, "events":[], "events_has_more":False,
                    "next_after":None, "notes":["任务账本暂不可读，当前终态需要恢复连接后核对。"]}

    def event_batch(self, route, history):
        # The archive owns one high-water mark per physical source, including across restarts.
        source = self.sources.get(route)
        if source is None:
            source = self.get(route, {"limit":0}).get("source_id")
        for _ in range(2):
            if not isinstance(source, str) or not source:
                raise FactsUnavailable("invalid_source")
            self.sources[route] = source
            cursor = history.source_cursor(source)
            try:
                page = self.get(route, {"source_id":source, "after":cursor})
            except FactsQueryError as exc:
                if exc.status != 409 or exc.details.get("code") != "facts.source_changed":
                    raise FactsUnavailable(str(exc)) from exc
                source = exc.details.get("source_id")
                continue
            if page.get("source_id") != source or not isinstance(page.get("events"), list):
                raise FactsUnavailable("invalid_page")
            previous = cursor
            for row in page["events"]:
                if not isinstance(row, dict) or type(row.get("seq")) is not int or row["seq"] <= previous:
                    raise FactsUnavailable("invalid_sequence")
                previous = row["seq"]
            return page
        raise FactsUnavailable("source_changed")
