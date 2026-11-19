"""Only public Console GET endpoints are available to this provider."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit


class EvidenceError(Exception):
    def __init__(self, code, message, status=200):
        super().__init__(message)
        self.code, self.status = code, status


def remaining(deadline):
    value = deadline-time.monotonic()
    if value <= 0:
        raise EvidenceError("evidence.deadline", "查询超出时间预算，请缩小范围后重试。")
    return value


class PublicSource:
    MAX_BODY = 2*1024*1024
    MAX_EVENTS = 500

    def __init__(self, base):
        self.url = urlsplit(base)
        if (self.url.scheme not in ("http", "https") or not self.url.hostname
                or self.url.username or self.url.password or self.url.query or self.url.fragment):
            raise ValueError("EDGE_EVIDENCE_SOURCE must be a fixed HTTP(S) URL without credentials")

    def get(self, path, params, deadline):
        connection = response = None
        timer = None
        expired = threading.Event()
        try:
            cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
            connection = cls(self.url.hostname,self.url.port,timeout=min(1.5,remaining(deadline)))
            def interrupt():
                expired.set()
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            # Socket timeouts limit gaps between reads; an absolute watchdog
            # also ends a peer that drips response headers forever.
            timer = threading.Timer(remaining(deadline),interrupt)
            timer.daemon = True
            timer.start()
            target = self.url.path.rstrip("/")+path+"?"+urlencode(params)
            connection.request("GET",target,headers={"Accept":"application/json"})
            response = connection.getresponse()
            # No redirect, writable method, caller-supplied host or database path.
            if response.status == 404:
                raise EvidenceError("evidence.task_not_found", "公共接口中没有找到该任务或证据。")
            if response.status != 200:
                raise EvidenceError("evidence.source_unavailable", "公共证据接口暂不可用。",503)
            chunks, total = [], 0
            while total<=self.MAX_BODY:
                remaining(deadline)
                # read1 returns after one socket read, so a slow trickle cannot
                # keep a whole-body read alive indefinitely by resetting its timeout.
                chunk = response.read1(min(65536,self.MAX_BODY+1-total))
                if not chunk:
                    if response.length not in (None,0):
                        raise EvidenceError("evidence.source_unavailable", "公共证据响应在完整传输前中断。",503)
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            remaining(deadline)
            if len(raw)>self.MAX_BODY:
                raise EvidenceError("evidence.source_budget", "公共证据页超过读取预算，未返回不完整数据。")
            value = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value,dict):
                raise ValueError()
            return value
        except EvidenceError:
            raise
        except (OSError,http.client.HTTPException):
            if expired.is_set():
                raise EvidenceError("evidence.deadline", "公共证据读取超出本次查询时间预算。") from None
            raise EvidenceError("evidence.source_unavailable", "公共证据接口连接中断或超时。",503) from None
        except (ValueError,UnicodeError,RecursionError):
            raise EvidenceError("evidence.source_invalid", "公共证据接口未返回有效的 JSON 对象。",503) from None
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def session(self, task_id, deadline):
        data = self.get("/api/session",{"task_id":task_id},deadline)
        scope, coverage, messages = data.get("scope"),data.get("coverage"),data.get("messages",[])
        if (not isinstance(data.get("markdown"),str) or not isinstance(data.get("coverage"),dict)
                or not isinstance(scope,dict) or scope.get("task_id") != task_id
                or not isinstance(messages,list)
                or scope.get("trace_id") is not None and not isinstance(scope["trace_id"],str)):
            raise EvidenceError("evidence.source_invalid", "会话投影缺少匹配的任务范围或覆盖说明。",503)
        refs = []
        seen = set()
        for message in messages:
            if not isinstance(message,dict) or not isinstance(message.get("refs",[]),list):
                raise EvidenceError("evidence.source_invalid", "会话消息或证据引用结构异常。",503)
            for ref in message.get("refs",[]):
                if not isinstance(ref,dict) or type(ref.get("seq")) is not int or ref["seq"]<=0:
                    raise EvidenceError("evidence.source_invalid", "会话证据引用缺少有效归档序号。",503)
                if ref["seq"] not in seen:
                    refs.append({key:ref.get(key) for key in ("seq","event_id","kind","code")})
                    seen.add(ref["seq"])
        public_coverage = {key:coverage[key] for key in ("archive_id","through_seq","archived_events","selected_events",
                          "collapsed_events","truncated","task_available","notes","pruned_through_seq") if key in coverage}
        return {"scope":{"task_id":task_id,"trace_id":scope.get("trace_id")},
                "markdown":data["markdown"],"coverage":public_coverage,
                "refs":refs[:40],"truncated":bool(data["coverage"].get("truncated")) or len(refs)>40}

    def events(self, task_id, after_seq, archive_id, deadline):
        loaded, cursor, cutoff, identity, has_more, pruned = [], after_seq, None, None, False, 0
        while len(loaded)<self.MAX_EVENTS:
            query = {"task_id":task_id,"after":cursor,"limit":min(100,self.MAX_EVENTS-len(loaded))}
            if cutoff is not None:
                query["before"] = cutoff+1
            page = self.get("/api/events",query,deadline)
            current = page.get("archive_id")
            if not isinstance(current,str) or not current or type(page.get("cursor")) is not int:
                raise EvidenceError("evidence.source_invalid", "归档缺少身份或截止游标。",503)
            page_pruned = page.get("pruned_through_seq",0)
            if type(page_pruned) is not int or page_pruned<0:
                raise EvidenceError("evidence.source_invalid", "归档清理位置无效。",503)
            pruned = max(pruned,page_pruned)
            if (archive_id and current!=archive_id) or (identity and current!=identity):
                raise EvidenceError("evidence.archive_changed", "归档身份已变化，请使用 after_seq=0 重新读取。")
            if identity is None:
                identity, cutoff = current,page["cursor"]
                if cursor>cutoff:
                    raise EvidenceError("evidence.cursor_ahead", "游标超过当前归档，请核对归档身份。")
            rows = page.get("events")
            if not isinstance(rows,list) or type(page.get("has_more")) is not bool or len(rows)>query["limit"]:
                raise EvidenceError("evidence.source_invalid", "归档页结构不符合公共接口契约。",503)
            for row in rows:
                if (not isinstance(row,dict) or type(row.get("seq")) is not int
                        or not cursor<row["seq"]<=cutoff):
                    raise EvidenceError("evidence.source_invalid", "归档页游标不递增或超过固定截止位置。",503)
                cursor = row["seq"]
                loaded.append(row)
            has_more = page["has_more"]
            if not has_more:
                break
            if not rows:
                raise EvidenceError("evidence.source_invalid", "归档声明还有数据但未提供继续读取位置。",503)
        coverage = {"archive_id":identity,"task_id":task_id,"after_seq":after_seq,
                    "through_seq":cutoff,"loaded_events":len(loaded),"has_more":has_more,
                    "next_after":cursor if has_more else None,
                    "pruned_through_seq":pruned,"retention_gap_possible":after_seq<pruned,
                    "notes":["SQL 只作用于本次加载窗口；聚合数量不能代替整个任务总量。",
                             "范围由 Console task_id 查询及其已记录的委托关联确定；缺失事件不会被补造。"]}
        if after_seq<pruned:
            coverage["notes"].append("归档在请求范围内已执行历史清理；不能据此证明任务的原始记录完整。")
        return loaded,coverage
