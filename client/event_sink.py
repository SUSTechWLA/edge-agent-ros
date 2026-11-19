"""Optional read-only event consumer. Import archive rows and checkpoint in one transaction."""
import argparse
from datetime import datetime, timezone
from http.client import HTTPException, IncompleteRead
import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
if __package__:
    from . import bounded_http
else:
    import bounded_http


MAX_PAGE_BYTES = 8 * 1024 * 1024
PAGE_TIMEOUT_SECONDS = 10
EXACT_FILTERS = {"kind","module","code","source","level","trigger_kind","event_id","operation_id",
                 "mission_id","session_id","layer","call_id","parent_event_id","trigger_id","trigger_source"}


class SinkError(URLError):
    """A safely classified subscription failure; callers can retry the same checkpoint."""

    def __init__(self,code,*,seq=None):
        super().__init__(code)
        self.seq=seq


def ordinal(value):
    return type(value) is int and 0 <= value < 2**63


def archive_identity(page):
    identity = page.get("archive_id") if isinstance(page,dict) else None
    if not isinstance(identity,str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}",identity):
        raise SinkError("subscription.invalid_archive")
    return identity


def canonical(row):
    return json.dumps(row,ensure_ascii=True,sort_keys=True,allow_nan=False,separators=(",",":"))


class Sink:
    def __init__(self,path,url,filters):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.url=url.rstrip("/")
        self.filters=filters
        self.key=json.dumps(filters,sort_keys=True)
        self.archive=None
        self.journal=None
        self.db.executescript("""
           PRAGMA journal_mode=WAL;
           PRAGMA synchronous=FULL;
           CREATE TABLE IF NOT EXISTS events(archive_id TEXT,seq INTEGER,event TEXT NOT NULL,
               PRIMARY KEY(archive_id,seq));
           CREATE INDEX IF NOT EXISTS sink_journal_id ON events(archive_id,json_extract(event,'$.event_id'))
               WHERE json_valid(event) AND json_extract(event,'$.source') GLOB 'journal:*';
           CREATE TABLE IF NOT EXISTS checkpoints(archive_id TEXT,selector TEXT,cursor INTEGER NOT NULL,
               PRIMARY KEY(archive_id,selector));
           CREATE TABLE IF NOT EXISTS retention_notices(archive_id TEXT,selector TEXT,after_seq INTEGER,
               pruned_through_seq INTEGER,PRIMARY KEY(archive_id,selector,pruned_through_seq));
           CREATE TABLE IF NOT EXISTS subscription_state(selector TEXT PRIMARY KEY, archive_id TEXT, trace_id TEXT NOT NULL);
           CREATE TABLE IF NOT EXISTS subscription_notices(id INTEGER PRIMARY KEY, selector TEXT NOT NULL,
               event_id TEXT UNIQUE NOT NULL, trace_id TEXT NOT NULL, parent_event_id TEXT, utc TEXT NOT NULL,
               kind TEXT NOT NULL, data TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
           CREATE INDEX IF NOT EXISTS subscription_notice_stream ON subscription_notices(selector,kind,id);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO subscription_state VALUES(?,NULL,?)",
                            (self.key, "subscription-" + uuid.uuid4().hex))

    def bind_journal(self,journal,module):
        """Publish this selector's transitions to the consumer's own ModuleJournal."""
        self.journal = journal
        self.module = module
        self.flush_notices()

    def notice(self,kind,trace,*,parent=None,**data):
        self.db.execute("""INSERT INTO subscription_notices(selector,event_id,trace_id,parent_event_id,utc,kind,data)
                           VALUES(?,?,?,?,?,?,?)""",(self.key,uuid.uuid4().hex,trace,parent,
                           datetime.now(timezone.utc).isoformat(),kind,canonical(data)))

    def health(self,trace,archive,after,reason=None,seq=None):
        prev = self.db.execute("""SELECT event_id,kind,data FROM subscription_notices WHERE selector=? AND kind IN
            ('subscription.blocked','subscription.restored') ORDER BY id DESC LIMIT 1""",(self.key,)).fetchone()
        kind = "subscription.blocked" if reason else "subscription.restored"
        data = {"reason":reason,"archive_id":archive,"after_seq":after,"failed_seq":seq}
        if (not prev and not reason) or (prev and prev[1]==kind and (not reason or json.loads(prev[2])==data)):
            return
        self.notice(kind,trace,parent=prev[0] if prev else None,**data)

    def flush_notices(self):
        if self.journal is None:
            return
        for seq,eid,trace,parent,utc,kind,data in self.db.execute("""SELECT id,event_id,trace_id,parent_event_id,utc,kind,data
                FROM subscription_notices WHERE selector=? AND delivered=0 ORDER BY id""",(self.key,)):
            with self.journal.db:
                self.journal.emit({"module":self.module,"event_id":eid,"trace_id":trace,"parent_event_id":parent,
                    "utc":utc,"kind":kind,"code":kind,"level":"info" if kind=="subscription.restored" else "warn",
                    "data":json.loads(data)},key="subscription/"+eid)
            with self.db:
                self.db.execute("UPDATE subscription_notices SET delivered=1 WHERE id=?",(seq,))

    def page(self,after,limit):
        query=urlencode({**self.filters,"after":after,"limit":limit})
        try:
            raw = bounded_http.read(self.url+"/api/events?"+query,None,MAX_PAGE_BYTES,PAGE_TIMEOUT_SECONDS)
        except bounded_http.TransportError as exc:
            code = {"reply_size":"page_too_large","incomplete_reply":"incomplete_page",
                    "deadline":"deadline"}.get(exc.reason,"source_unavailable")
            raise SinkError("subscription."+code) from exc
        def invalid_number(_):
            raise ValueError("nonfinite")
        return json.loads(raw,parse_constant=invalid_number)

    def validate(self,page,after,limit):
        archive_identity(page)
        if (not ordinal(page.get("cursor")) or not ordinal(page.get("pruned_through_seq"))
                or page["pruned_through_seq"]>page["cursor"] or type(page.get("has_more")) is not bool
                or not isinstance(page.get("events"),list) or len(page["events"])>limit
                or page["has_more"] and len(page["events"])!=limit):
            raise SinkError("subscription.invalid_page")
        previous=after
        for row in page["events"]:
            seq=row.get("seq") if isinstance(row,dict) else None
            if (not ordinal(seq) or not previous<seq<=page["cursor"]
                    or not all(isinstance(row.get(k),str) and row[k] for k in ("source","kind"))):
                raise SinkError("subscription.invalid_row")
            if any(row.get(k)!=self.filters[k] for k in EXACT_FILTERS.intersection(self.filters)):
                raise SinkError("subscription.selector_mismatch",seq=seq)
            cause = row.get("cause_seq")
            if cause is not None and not (ordinal(cause) and 0 < cause <= page["cursor"] and cause != seq):
                raise SinkError("subscription.invalid_row",seq=seq)
            previous=seq
        if page["has_more"] and previous>=page["cursor"]:
            raise SinkError("subscription.invalid_page")

    def store_row(self,archive,row):
        raw=canonical(row)
        if row["source"].startswith("journal:"):
            eid=row.get("event_id")
            if not isinstance(eid,str) or not re.fullmatch(r"[\w./:@-]{1,240}",eid):
                raise SinkError("subscription.invalid_row",seq=row["seq"])
            collision=self.db.execute("""SELECT 1 FROM events WHERE json_valid(event)
                AND json_extract(event,'$.source') GLOB 'journal:*' AND archive_id=?
                AND json_extract(event,'$.event_id')=? AND seq<>? LIMIT 1""",(archive,eid,row["seq"])).fetchone()
            if collision:
                raise SinkError("subscription.event_conflict",seq=row["seq"])
        inserted=self.db.execute("INSERT INTO events VALUES(?,?,?) ON CONFLICT(archive_id,seq) DO NOTHING",
                                 (archive,row["seq"],raw))
        if inserted.rowcount:
            return
        old=json.loads(self.db.execute("SELECT event FROM events WHERE archive_id=? AND seq=?",
                                       (archive,row["seq"])).fetchone()[0])
        if canonical(old)==raw:
            return
        # The archive can resolve a stable parent ID to its sequence after the child was copied.
        if row.get("parent_event_id") and old.get("cause_seq") is None and row.get("cause_seq") is not None:
            if canonical(dict(old,cause_seq=row["cause_seq"]))==raw:
                self.db.execute("UPDATE events SET event=? WHERE archive_id=? AND seq=?",(raw,archive,row["seq"]))
                return
        raise SinkError("subscription.event_conflict",seq=row["seq"])

    def copy(self):
        self.flush_notices()
        archive,trace=self.db.execute("SELECT archive_id,trace_id FROM subscription_state WHERE selector=?",
                                      (self.key,)).fetchone()
        after=0
        try:
            if archive is None:
                first=self.page(0,1)
                archive=archive_identity(first)
                self.validate(first,0,1)
            stored=self.db.execute("SELECT cursor FROM checkpoints WHERE archive_id=? AND selector=?",
                                   (archive,self.key)).fetchone()
            after=stored[0] if stored else 0
            page=self.page(after,32)
            self.validate(page,after,32)
            current=archive_identity(page)
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                actual=self.db.execute("SELECT archive_id FROM subscription_state WHERE selector=?",(self.key,)).fetchone()[0]
                if actual is not None and actual!=archive:
                    return True  # A concurrent reader already switched generations.
                if actual!=current:
                    self.db.execute("UPDATE subscription_state SET archive_id=? WHERE selector=?",(current,self.key))
                self.archive=current
                if archive!=current:
                    self.notice("subscription.archive_changed",trace,archive_id=current,previous_archive_id=archive,
                                after_seq=after,reason="archive_changed")
                    more=True
                else:
                    more=self.store_page(page,trace,archive,after)
                    self.health(trace,archive,after)
        except (URLError,OSError,HTTPException,ValueError,RecursionError) as exc:
            if isinstance(exc,SinkError):
                reason=exc.reason
            elif isinstance(exc,(ValueError,RecursionError)):
                reason="subscription.invalid_json"
            else:
                reason="subscription.source_unavailable"
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.health(trace,archive,after,reason,getattr(exc,"seq",None))
            self.flush_notices()
            raise SinkError(reason) from exc
        self.flush_notices()
        return more

    def store_page(self,page,trace,archive,after):
        rows=page["events"]
        next_cursor=rows[-1]["seq"] if page["has_more"] else page["cursor"]
        if next_cursor<after:
            raise SinkError("subscription.cursor_regressed")
        if after < page["pruned_through_seq"]:
            inserted=self.db.execute("INSERT OR IGNORE INTO retention_notices VALUES(?,?,?,?)",
                                     (archive,self.key,after,page["pruned_through_seq"]))
            if inserted.rowcount:
                self.notice("subscription.retention",trace,archive_id=archive,after_seq=after,
                            pruned_through_seq=page["pruned_through_seq"],reason="retention_crossed")
        for row in rows:
            self.store_row(archive,row)
        if next_cursor!=after or not after:
            self.db.execute("""INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(archive_id,selector)
                   DO UPDATE SET cursor=excluded.cursor WHERE checkpoints.cursor<excluded.cursor""",
                   (archive,self.key,next_cursor))
        return page["has_more"]

    def close(self):
        self.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,required=True,help="private destination SQLite file")
    parser.add_argument("--url",default="http://127.0.0.1:8765")
    parser.add_argument("--once",action="store_true",help="catch up, then exit")
    parser.add_argument("--export",action="store_true",help="write locally copied events to stdout as NDJSON")
    for key in ("module","code","kind","level","trigger_kind"):
        parser.add_argument("--"+key.replace("_","-"))
    args=parser.parse_args()
    filters={key:getattr(args,key) for key in ("module","code","kind","level","trigger_kind") if getattr(args,key)}
    if args.export:
        with sqlite3.connect(args.db.resolve().as_uri()+"?mode=ro",uri=True) as db:
            for archive,raw in db.execute("SELECT archive_id,event FROM events ORDER BY archive_id,seq"):
                print(json.dumps({**json.loads(raw),"archive_id":archive},ensure_ascii=False))
        return
    sink=Sink(args.db,args.url,filters)
    backoff=1
    try:
        while True:
            try:
                more=sink.copy()
                backoff=1
            except (URLError,TimeoutError,ConnectionError,IncompleteRead,json.JSONDecodeError,UnicodeDecodeError) as error:
                if args.once:
                    raise
                print(f"event source unavailable ({type(error).__name__}); retry in {backoff}s",
                      file=sys.stderr,flush=True)
                time.sleep(backoff)
                backoff=min(30,backoff*2)
                continue
            if not more:
                if args.once:
                    return
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sink.close()


if __name__=="__main__":
    main()
