"""Bounded SQLite SELECT over one task's public HTTP event window, never owner databases."""
import json
import re
import sqlite3
import time
from contextlib import closing

from edge_contracts.evidence_client import EvidenceError, remaining


COLUMNS = ("seq","utc","kind","level","source","session_id","mission_id","operation_id",
           "detail","cause_seq","source_id","source_seq","event_id","trace_id","parent_event_id",
           "task_id","module","layer","code","call_id","trigger_kind","trigger_id","trigger_source","data_json")
FUNCTIONS = {"count","sum","total","avg","min","max","coalesce","ifnull","nullif","length",
             "substr","substring","lower","upper","trim","ltrim","rtrim","abs","round","typeof",
             "replace","instr","json_extract","json_type","json_valid","json_array_length","like","glob","->","->>"}
MAX_SQL_BYTES = 16384
MAX_VM_STEPS = 200000
MAX_CELL_BYTES = 2*1024*1024


def dump(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":"))


def schema():
    return {"schema_version":1,"table":"events","columns":list(COLUMNS),
            "allowed_functions":sorted(FUNCTIONS),
            "scope":"表已限于当前任务及 Console 显式关联的委托；底层事件 task_id 可以为空。"
                    "不要再次 WHERE task_id=... 排除真实委托；没有私有任务库或宿主文件。",
            "limits":{"source_events":500,"result_rows":50,"result_bytes":12288,"deadline_seconds":4,
                      "sql_bytes":MAX_SQL_BYTES,"sql_vm_steps":MAX_VM_STEPS},
            "parameters":{"task_id":"string","sql":"string","after_seq":"number (integer ≥ 0)",
                          "limit":"number (integer 1..50)","archive_id":"string; first page empty, continuation required"},
            "sql":"单条 SELECT / WITH；只读 events；允许聚合、关联、json_extract(data_json, '$.path') 和 JSON -> / ->>。"
                  "可用绑定参数 :task_id / :after_seq / :limit。禁止写入、ATTACH、PRAGMA、文件和扩展函数。",
            "discovery":"先 SELECT kind,count(*) FROM events GROUP BY kind，核对真实事件名称；再选一条 data_json 看实际字段。"
                        "不得猜测事件枚举或 JSON 路径；NULL 只表示当前路径没有值。",
            "examples":["SELECT kind,count(*) FROM events GROUP BY kind",
                        "SELECT seq,code,detail FROM events WHERE level IN ('warn','error') ORDER BY seq",
                        "SELECT seq,json_extract(data_json,'$.payload.diagnostic') AS diagnosis FROM events WHERE level IN ('warn','error')",
                        "SELECT seq,json_extract(data_json,'$.payload.trace.diagnosis') AS diagnosis FROM events WHERE kind LIKE '%error%'",
                        "SELECT child.seq,child.parent_event_id,parent.detail FROM events child LEFT JOIN events parent ON parent.event_id=child.parent_event_id"],
            "continuation":"after_seq=0, archive_id='' 首次读取；has_more 时以 coverage.next_after 与 archive_id 续读。"
                           "输出截断时可缩小 SELECT 字段或用 WHERE seq 继续本窗口；next_after 只负责源事件分页。",
            "markdown":"先 read_session(task_id) 阅读任务会话，再 query_evidence 查询证据。"
                       "查询成功表示读到了公开记录，不表示机器人任务成功。窗口缺少父事件时应回查同任务早期窗口。"}


def run_sql(sql, events, parameters, limit, deadline, sanitize):
    if not isinstance(sql,str) or not 0<len(sql.encode("utf-8"))<=MAX_SQL_BYTES:
        raise EvidenceError("evidence.invalid_sql", "sql 必须是 1 到 16384 字节的单条 SELECT 文本。")
    if not re.match(r"\s*(SELECT|WITH)\b",sql,re.I):
        raise EvidenceError("evidence.sql_denied", "只允许 SELECT 或 WITH 开头的单条只读查询。")
    remaining(deadline)
    with closing(sqlite3.connect(":memory:")) as db:
        definitions = [name+(" INTEGER" if name in ("seq","cause_seq","source_seq") else " TEXT") for name in COLUMNS]
        db.execute("CREATE TABLE events ("+",".join(definitions)+")")
        normalized = []
        for event in events:
            item = {name:event.get(name) for name in COLUMNS if name!="data_json"}
            item["data_json"] = dump(sanitize(event.get("data",{})))
            item = sanitize(item)
            # Keep received fields intact. A large model record must not prevent
            # a SELECT that only needs a neighboring brake distance or COUNT(*).
            # PublicSource bounds each HTTP page; output clipping happens later.
            if any(value is not None and not isinstance(value,(str,int,float)) for value in item.values()):
                raise EvidenceError("evidence.source_invalid", "公开事件字段类型异常。",503)
            normalized.append(tuple(item[name] for name in COLUMNS))
        db.executemany("INSERT INTO events VALUES ("+",".join("?" for _ in COLUMNS)+")",normalized)
        db.commit()
        db.execute("PRAGMA query_only=ON")
        for category,value in ((sqlite3.SQLITE_LIMIT_LENGTH,MAX_CELL_BYTES),(sqlite3.SQLITE_LIMIT_SQL_LENGTH,MAX_SQL_BYTES),
                               (sqlite3.SQLITE_LIMIT_COLUMN,64),(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,64),
                               (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,10),(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,64),
                               (sqlite3.SQLITE_LIMIT_ATTACHED,0)):
            db.setlimit(category,value)
        denied = False
        def authorize(action, arg1, arg2, database, origin):
            nonlocal denied
            allowed = (action == sqlite3.SQLITE_SELECT
                       or action == sqlite3.SQLITE_READ and arg1=="events"
                       and (database=="main" and arg2 in COLUMNS or database is None and arg2=="")
                       or action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FUNCTIONS
                       or action == sqlite3.SQLITE_RECURSIVE)
            if not allowed:
                denied = True
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        steps = 0
        exhausted = False
        def progress():
            nonlocal steps, exhausted
            steps += 1000
            exhausted = steps>MAX_VM_STEPS or time.monotonic()>=deadline
            return int(exhausted)
        db.set_progress_handler(progress,1000)
        try:
            cursor = db.execute(sql,parameters)
            columns = [column[0] for column in cursor.description]
            rows = []
            for row in cursor:
                remaining(deadline)
                if len(rows)==limit:
                    return columns,rows,True
                if any(isinstance(cell,bytes) for cell in row):
                    raise EvidenceError("evidence.sql_denied", "结果仅支持 JSON 标量，不支持 BLOB。")
                rows.append(list(row))
            return columns,rows,False
        except sqlite3.Error as exc:
            if exhausted:
                raise EvidenceError("evidence.query_budget", "SQL 超出时间或指令预算，请简化查询。") from None
            if denied:
                raise EvidenceError("evidence.sql_denied", "查询访问了未公开的表、语句或函数。") from None
            if isinstance(exc,sqlite3.DataError):
                raise EvidenceError("evidence.result_budget", "SQL 结果字段超过大小预算，请只选择需要的字段。") from None
            # SQLite errors may echo submitted SQL/literals; do not copy them to audit or caller.
            raise EvidenceError("evidence.invalid_sql", "SQL 语法、字段或绑定参数不正确；请使用 evidence_schema 核对。") from None
