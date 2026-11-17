"""Independent HTTP tools for public sessions and bounded task evidence (SDD-010)."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"agent"))
from module_journal import ModuleJournal
from module_events import identifier
from tracing import sanitized
from evidence.public_source import EvidenceError, PublicSource, remaining
from evidence.query import COLUMNS, dump, run_sql, schema


VERSION = "1"
MAX_RESULT_BYTES = 12288
MAX_BODY_BYTES = 32768
DEADLINE_SECONDS = 4
TOOLS = {"read_session","query_evidence","evidence_schema"}


def bounded_id(value, name):
    try:
        identifier(value,name)
    except ValueError:
        raise EvidenceError("evidence.invalid_request",f"{name} 必须是 1 至 240 字符的有效标识。") from None
    return value


def error_receipt(op, query_id, error):
    op = op if isinstance(op,str) and len(op)<=240 else None
    return {"operation_id":op,"state":"BLOCKED","error":{"code":error.code,"message":str(error),
            "details":{"query_id":query_id,"origin":"evidence.query"}},
            "diagnostic":{"code":error.code,"origin":"evidence.query","query_id":query_id}}


def fit_evidence(value, reserve=512):
    """Keep complete JSON cells. Explain output clipping instead of altering measurements."""
    # Leave room for receipt metadata and the longest permitted operation ID.
    ceiling = MAX_RESULT_BYTES-reserve
    while len(dump(value).encode())>ceiling:
        value["truncated"] = True
        if value.get("rows"):
            value["rows"].pop()
            value["markdown"] = "查询输出超过文本预算，已减少返回行。请缩小 SELECT 字段或 WHERE 范围。"
            continue
        if value.get("refs"):
            value["refs"].pop()
            continue
        if len(value.get("markdown",""))>400:
            value["markdown"] = value["markdown"][:len(value["markdown"])//2]+"\n\n…内容已截断，请打开 Console 会话继续阅读。"
            continue
        raise EvidenceError("evidence.result_budget","结果元数据超过预算，请缩小查询。")
    return value


class EvidenceProvider:
    def __init__(self, source, journal_path):
        self.source = source
        self.journal = ModuleJournal(journal_path)
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.active = set()
        with self.journal.db:
            self.journal.db.execute("""CREATE TABLE IF NOT EXISTS evidence_requests(
                operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, query_id TEXT NOT NULL, receipt TEXT)""")

    def close(self):
        with self.lock:
            self.journal.close()

    def context(self, body, target, query_id):
        trace = body.get("trace") or {}
        if not isinstance(trace,dict):
            raise EvidenceError("evidence.invalid_request","trace 必须是对象。")
        result = {"module":"evidence.query","layer":"evidence",
                  "trace_id":bounded_id(trace.get("trace_id") or query_id,"trace_id")}
        for field in ("task_id","parent_event_id","call_id"):
            if trace.get(field) is not None:
                result[field] = bounded_id(trace[field],field)
        if "task_id" not in result and target:
            result["task_id"] = target
        return result

    def work(self, tool, arguments, deadline):
        if not isinstance(arguments,dict):
            raise EvidenceError("evidence.invalid_request","arguments 必须是对象。")
        if tool=="evidence_schema":
            if arguments:
                raise EvidenceError("evidence.invalid_request","evidence_schema 不接受参数。")
            return schema()
        task_id = bounded_id(arguments.get("task_id"),"task_id")
        if tool=="read_session":
            if set(arguments)!={"task_id"}:
                raise EvidenceError("evidence.invalid_request","read_session 仅接受 task_id。")
            return sanitized(self.source.session(task_id,deadline))
        if set(arguments)!={"task_id","sql","after_seq","limit","archive_id"}:
            raise EvidenceError("evidence.invalid_request","query_evidence 需要 task_id、sql、after_seq、limit、archive_id。")
        after, limit, archive = arguments["after_seq"],arguments["limit"],arguments["archive_id"]
        if type(after) not in (int,float) or not 0<=after<=2**53-1 or int(after)!=after:
            raise EvidenceError("evidence.invalid_request","after_seq 必须是非负安全整数。")
        if type(limit) not in (int,float) or not 1<=limit<=50 or int(limit)!=limit:
            raise EvidenceError("evidence.invalid_request","limit 必须是 1 至 50 的整数。")
        if not isinstance(archive,str) or len(archive)>240 or after>0 and not archive:
            raise EvidenceError("evidence.invalid_request","续读必须提供首次返回的 archive_id；首次可用空字符串。")
        # Validate the query before any source IO, including harmless schema errors.
        params = {"task_id":task_id,"after_seq":int(after),"limit":int(limit)}
        run_sql(arguments["sql"],[],params,int(limit),deadline,sanitized)
        events,coverage = self.source.events(task_id,int(after),archive,deadline)
        columns,rows,clipped = run_sql(arguments["sql"],events,params,int(limit),deadline,sanitized)
        if any(len(column)>120 for column in columns):
            raise EvidenceError("evidence.result_budget","结果列名过长，请使用简短别名。")
        try:
            dump(rows)
        except (ValueError,TypeError):
            raise EvidenceError("evidence.invalid_result","SQL 结果必须为有限数值或文本。") from None
        by_seq = {event["seq"]:event for event in events}
        refs = []
        if "seq" in columns:
            index = columns.index("seq")
            for row in rows:
                event = by_seq.get(row[index]) if type(row[index]) is int else None
                if event:
                    refs.append({key:event.get(key) for key in ("seq","event_id","kind","code")})
        markdown = (f"本次载入任务 `{task_id}` 的 {len(events)} 条公开事件，查询返回 {len(rows)} 行。\n\n"
                    "结果只描述本次加载范围；`rows` 为查询值，`refs` 为可回查的归档编号。"
                    +(" 仍有后续事件，请按 coverage.next_after 续读。" if coverage["has_more"] else ""))
        return {"scope":{"task_id":task_id},"columns":columns,"rows":rows,"refs":refs,
                "coverage":coverage,"truncated":clipped,"markdown":markdown}

    def invoke(self, tool, body):
        op = body.get("operation_id") if isinstance(body,dict) else None
        query_id = uuid.uuid4().hex
        if tool not in TOOLS:
            return 200,error_receipt(op,query_id,EvidenceError("evidence.unknown_tool","未知证据工具。"))
        try:
            bounded_id(op,"operation_id")
            if body.get("version")!=VERSION:
                raise EvidenceError("evidence.version_mismatch","证据插件只支持版本 1。")
            arguments = body.get("arguments")
            target = arguments.get("task_id") if isinstance(arguments,dict) else None
            if target is not None:
                bounded_id(target,"task_id")
            # Store a digest, never the SQL text, user literals, or model request body.
            digest = hashlib.sha256(dump({"tool":tool,"arguments":arguments,"trace":body.get("trace")}).encode()).hexdigest()
            context = self.context(body,target,query_id)
        except (EvidenceError,ValueError,TypeError,RecursionError) as exc:
            error = exc if isinstance(exc,EvidenceError) else EvidenceError("evidence.invalid_request","无效的查询请求。")
            return error.status,error_receipt(op,query_id,error)
        if not self.slots.acquire(blocking=False):
            return 503,error_receipt(op,query_id,EvidenceError("evidence.busy","证据插件正在处理其他查询。",503))
        claimed = False
        try:
            with self.lock:
                if op in self.active:
                    raise EvidenceError("evidence.busy","同一操作正在查询，请稍后按原 ID 重试。",503)
                row = self.journal.db.execute("SELECT digest,query_id,receipt FROM evidence_requests WHERE operation_id=?",(op,)).fetchone()
                if row:
                    if row[0]!=digest:
                        raise EvidenceError("evidence.operation_conflict","同一 operation_id 已绑定另一查询；修正查询请使用新 ID。")
                    query_id = row[1]
                    if row[2]:
                        return 200,json.loads(row[2])
                self.active.add(op)
                claimed = True
                context = self.context(body,target,query_id)
                data = {"query_id":query_id,"tool":tool,"request_sha256":digest,"target_task_id":target}
                with self.journal.db:
                    self.journal.db.execute("INSERT OR IGNORE INTO evidence_requests VALUES(?,?,?,NULL)",(op,digest,query_id))
                    requested = self.journal.emit({**context,"operation_id":op,"kind":"evidence.query_requested",
                        "code":"evidence.requested","data":data})
            started = time.monotonic()
            status, failure = 200,None
            try:
                evidence = sanitized(self.work(tool,arguments,started+DEADLINE_SECONDS))
                remaining(started+DEADLINE_SECONDS)
                evidence["query_id"] = query_id
                reserve = len(dump({"operation_id":op,"state":"SUCCEEDED","evidence":{}}).encode())+8
                evidence = fit_evidence(evidence,reserve)
                result = {"operation_id":op,"state":"SUCCEEDED","evidence":evidence}
            except EvidenceError as exc:
                failure = exc
                status,result = exc.status,error_receipt(op,query_id,exc)
            except (ValueError,TypeError,KeyError,RecursionError):
                failure = EvidenceError("evidence.source_invalid","证据格式不符合已发布契约。",503)
                status,result = failure.status,error_receipt(op,query_id,failure)
            with self.lock,self.journal.db:
                self.journal.emit({**context,"parent_event_id":requested,"operation_id":op,
                    "kind":"evidence.query_failed" if failure else "evidence.query_completed",
                    "code":failure.code if failure else "evidence.completed","level":"warn" if failure else "info",
                    "data":{**data,"duration_ms":round((time.monotonic()-started)*1000,2),
                            "rows":len(result.get("evidence",{}).get("rows",[])),
                            "coverage":result.get("evidence",{}).get("coverage",{}),
                            "truncated":result.get("evidence",{}).get("truncated",False)}})
                # Transient transport failures remain retryable under the same operation ID.
                if status==200:
                    self.journal.db.execute("UPDATE evidence_requests SET receipt=? WHERE operation_id=?",(dump(result),op))
            return status,result
        except EvidenceError as exc:
            return exc.status,error_receipt(op,query_id,exc)
        except sqlite3.Error:
            return 503,error_receipt(op,query_id,EvidenceError("evidence.audit_unavailable","查询审计无法持久保存，未交付查询结果。",503))
        finally:
            if claimed:
                with self.lock:
                    self.active.discard(op)
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def __init__(self,*args,provider,**kwargs):
        self.provider = provider
        super().__init__(*args,**kwargs)

    def log_message(self,*args):
        # URLs can contain client identifiers; structured journal is the audit source.
        pass

    def reply(self,status,payload):
        raw = dump(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if urlsplit(self.path).path=="/schema":
            self.reply(200,schema())
        else:
            self.reply(404,{"error":{"code":"evidence.not_found","message":"unknown endpoint"}})

    def do_POST(self):
        url = urlsplit(self.path)
        aliases = {"/session":"read_session","/query":"query_evidence","/schema":"evidence_schema"}
        tool = aliases.get(url.path)
        if url.path=="/invoke":
            tool = parse_qs(url.query).get("tool",[None])[0]
        if tool not in TOOLS:
            self.reply(404,{"error":{"code":"evidence.unknown_tool","message":"unknown evidence endpoint"}})
            return
        try:
            length = int(self.headers.get("Content-Length","0"))
            if (not 0<length<=MAX_BODY_BYTES or self.headers.get("Transfer-Encoding")
                    or self.headers.get_content_type()!="application/json"):
                raise ValueError()
            self.connection.settimeout(2)
            raw = self.rfile.read(length)
            if len(raw)!=length:
                raise ValueError()
            body = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            status,result = self.provider.invoke(tool,body)
        except (ValueError,UnicodeError,TimeoutError,RecursionError):
            self.close_connection = True
            self.reply(400,{"error":{"code":"evidence.invalid_request","message":"需要完整的有界 JSON 请求。"}})
            return
        try:
            self.reply(status,result)
        except (BrokenPipeError,ConnectionResetError):
            pass


def main():
    source = PublicSource(os.getenv("EDGE_EVIDENCE_SOURCE","http://console:8080"))
    provider = EvidenceProvider(source,os.getenv("EDGE_EVIDENCE_JOURNAL","/var/lib/edge-evidence/module-events.sqlite"))
    try:
        with ThreadingHTTPServer((os.getenv("EDGE_EVIDENCE_BIND","0.0.0.0"),int(os.getenv("EDGE_EVIDENCE_PORT","8125"))),
                                 partial(Handler,provider=provider)) as server:
            server.serve_forever()
    finally:
        provider.close()


if __name__=="__main__":
    main()
