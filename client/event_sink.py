"""Optional read-only event consumer. Import archive rows and checkpoint in one transaction."""
import argparse
from http.client import IncompleteRead
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


class Sink:
    def __init__(self,path,url,filters):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.url=url.rstrip("/")
        self.filters=filters
        self.key=json.dumps(filters,sort_keys=True)
        self.archive=None
        self.db.executescript("""
           PRAGMA journal_mode=WAL;
           PRAGMA synchronous=FULL;
           CREATE TABLE IF NOT EXISTS events(archive_id TEXT,seq INTEGER,event TEXT NOT NULL,
               PRIMARY KEY(archive_id,seq));
           CREATE TABLE IF NOT EXISTS checkpoints(archive_id TEXT,selector TEXT,cursor INTEGER NOT NULL,
               PRIMARY KEY(archive_id,selector));
           CREATE TABLE IF NOT EXISTS retention_notices(archive_id TEXT,selector TEXT,after_seq INTEGER,
               pruned_through_seq INTEGER,PRIMARY KEY(archive_id,selector,pruned_through_seq));
        """)

    def page(self,after,limit):
        query=urlencode({**self.filters,"after":after,"limit":limit})
        with urlopen(self.url+"/api/events?"+query,timeout=10) as response:
            return json.load(response)

    def copy(self):
        if not self.archive:
            self.archive=self.page(0,1)["archive_id"]
        stored=self.db.execute("SELECT cursor FROM checkpoints WHERE archive_id=? AND selector=?",
                               (self.archive,self.key)).fetchone()
        after=stored[0] if stored else 0
        page=self.page(after,32)
        if page["archive_id"]!=self.archive:
            self.archive=page["archive_id"]
            return True
        rows=page["events"]
        next_cursor=rows[-1]["seq"] if page["has_more"] else page["cursor"]
        if next_cursor<after:
            raise ValueError("source checkpoint moved backwards in the same archive")
        if stored and next_cursor==after and not rows:
            return False
        with self.db:
            if after < page["pruned_through_seq"]:
                self.db.execute("INSERT OR IGNORE INTO retention_notices VALUES(?,?,?,?)",
                                (self.archive,self.key,after,page["pruned_through_seq"]))
            self.db.executemany("INSERT OR IGNORE INTO events VALUES(?,?,?)",
                [(self.archive,row["seq"],json.dumps(row,ensure_ascii=False)) for row in rows])
            self.db.execute("""INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(archive_id,selector)
                   DO UPDATE SET cursor=max(cursor,excluded.cursor)""",(self.archive,self.key,next_cursor))
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
