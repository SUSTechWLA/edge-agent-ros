"""Default session.read service, independently replaceable through runtime plugins."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"agent"))
from module_journal import ModuleJournal
from module_events import IDENTIFIER
from tracing import sanitized
from evidence.public_source import EvidenceError, remaining
from sessions_plugin.source import SessionSource
from sessions_plugin.projection import project


MAX_REQUEST = 16384
MAX_RESPONSE = 131072
DEADLINE = 6


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()


class SessionProvider:
    def __init__(self, source, journal_path, plugin_id="session.default",version="1"):
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}",plugin_id) or not isinstance(version,str) or not 1<=len(version)<=64:
            raise ValueError("invalid Session plugin identity")
        self.source,self.plugin_id,self.version = source,plugin_id,version
        self.journal = ModuleJournal(journal_path)
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)

    def close(self):
        with self.lock:
            self.journal.close()

    def metadata(self, request):
        return {key:request.get(key) for key in ("schema_version","request_id","plugin_id","plugin_version","binding")}

    def validate(self, request):
        if not isinstance(request,dict):
            raise EvidenceError("session.invalid_request","请求必须为 JSON 对象。",400)
        expected = {"schema_version","request_id","plugin_id","plugin_version","binding"}
        scopes = set(request)&{"task_id","trace_id"}
        if (len(scopes)!=1 or set(request)!=expected|scopes or type(request.get("schema_version")) is not int
                or request["schema_version"]!=1
                or not isinstance(request.get("request_id"),str) or not IDENTIFIER.fullmatch(request["request_id"])
                or not isinstance(request.get("binding"),str) or not re.fullmatch(r"[0-9a-f]{64}",request["binding"])):
            raise EvidenceError("session.invalid_request","需要协议版本 1、有效请求编号、绑定摘要以及一个 task_id 或 trace_id。",400)
        if request["plugin_id"]!=self.plugin_id or request["plugin_version"]!=self.version:
            raise EvidenceError("session.identity_mismatch","请求的 Session 插件身份或版本不可用。",400)
        name = next(iter(scopes))
        if not isinstance(request[name],str) or not 1<=len(request[name])<=512:
            raise EvidenceError("session.invalid_request","任务或调用链标识必须为 1 至 512 字符。",400)
        return {name:request[name]}

    def error(self, metadata, error):
        code = error.code.replace("evidence.","session.",1)
        return {**metadata,"code":code,"error":{"code":code,"message":str(error)}}

    def invoke(self, request):
        metadata = self.metadata(request) if isinstance(request,dict) else {}
        try:
            scope = self.validate(request)
        except EvidenceError as error:
            return error.status,self.error(metadata,error)
        if not self.slots.acquire(blocking=False):
            return 503,self.error(metadata,EvidenceError("session.busy","会话插件正在处理其他读取，请稍后重试。",503))
        started = time.monotonic()
        common = {"module":self.plugin_id,"trace_id":request["request_id"],"layer":"session"}
        if "task_id" in scope and IDENTIFIER.fullmatch(scope["task_id"]):
            common["task_id"] = scope["task_id"]
        data = {"request_id":request["request_id"],"plugin_id":self.plugin_id,"plugin_version":self.version,
                "binding":request["binding"],"requested_scope":scope,
                "request_sha256":hashlib.sha256(encoded(request)).hexdigest()}
        try:
            with self.lock,self.journal.db:
                parent = self.journal.emit({**common,"kind":"session.query_requested","code":"session.requested","data":data})
            status, error, document = 200,None,None
            try:
                context = sanitized(self.source.context(scope,started+DEADLINE))
                events,coverage = self.source.collect(context,started+DEADLINE)
                # Audit records describe this reader, not evidence that the
                # requested task exists. The public loading window excludes
                # query audit prefixes before pagination (unlike total count).
                if not context.get("task") and not coverage["loaded_events"]:
                    if context.get("notes"):
                        raise EvidenceError("session.source_unavailable","任务来源暂不可读，无法确认该会话。",503)
                    raise EvidenceError("session.not_found","没有找到该任务或调用链的公开记录。",404)
                document = project(context,sanitized(events),coverage)
                remaining(started+DEADLINE)
                result = {**metadata,"document":document}
                if len(encoded(result))>MAX_RESPONSE:
                    raise EvidenceError("session.result_budget","会话投影超出响应预算，未交付不完整文档。",503)
            except EvidenceError as failure:
                error = failure
                status = failure.status if failure.status in (400,404) else 503
                result = self.error(metadata,failure)
            except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
                error = EvidenceError("session.source_invalid","公开事实无法按当前 Session 契约投影。",503)
                status,result = 503,self.error(metadata,error)
            with self.lock,self.journal.db:
                completed = {**common,"parent_event_id":parent,"kind":"session.query_failed" if error else "session.query_completed",
                             "code":result["code"] if error else "session.completed","level":"warn" if error else "info",
                             "data":{**data,"duration_ms":round((time.monotonic()-started)*1000,2),
                                     "coverage":document["coverage"] if document is not None else {}}}
                if document and document["scope"].get("task_id") and IDENTIFIER.fullmatch(document["scope"]["task_id"]):
                    completed["task_id"] = document["scope"]["task_id"]
                self.journal.emit(completed)
            return status,result
        except sqlite3.Error:
            return 503,self.error(metadata,EvidenceError("session.audit_unavailable","会话查询审计暂不可保存，未交付投影结果。",503))
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def __init__(self,*args,provider,**kwargs):
        self.provider = provider
        super().__init__(*args,**kwargs)

    def log_message(self,*args):
        pass

    def respond(self,status,body):
        raw = encoded(body)
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.path=="/health":
            self.respond(200,{"plugin_id":self.provider.plugin_id,"plugin_version":self.provider.version,"service":"session.read"})
        else:
            self.respond(404,{"error":{"code":"session.not_found","message":"unknown endpoint"}})

    def do_POST(self):
        if self.path!="/session":
            self.respond(404,{"error":{"code":"session.not_found","message":"unknown endpoint"}})
            return
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0<length<=MAX_REQUEST or self.headers.get("Transfer-Encoding") or self.headers.get_content_type()!="application/json":
                raise ValueError()
            self.connection.settimeout(2)
            raw = self.rfile.read(length)
            if len(raw)!=length:
                raise ValueError()
            request = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError,UnicodeError,TimeoutError,RecursionError):
            self.close_connection = True
            self.respond(400,{"error":{"code":"session.invalid_request","message":"需要完整的有界 JSON 请求。"}})
            return
        status,body = self.provider.invoke(request)
        self.respond(status,body)


def main():
    provider = SessionProvider(SessionSource(os.getenv("EDGE_SESSION_SOURCE","http://console:8080")),
                               os.getenv("EDGE_SESSION_JOURNAL","/var/lib/edge-session/session.default/module-events.sqlite"),
                               os.getenv("EDGE_SESSION_PLUGIN_ID","session.default"),os.getenv("EDGE_SESSION_PLUGIN_VERSION","1"))
    try:
        with ThreadingHTTPServer((os.getenv("EDGE_SESSION_BIND","0.0.0.0"),int(os.getenv("EDGE_SESSION_PORT","8126"))),
                                 partial(Handler,provider=provider)) as server:
            server.serve_forever()
    finally:
        provider.close()


if __name__=="__main__":
    main()
