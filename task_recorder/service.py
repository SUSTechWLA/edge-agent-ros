"""Optional per-task evidence recorder. Only this process owns its SQLite archive."""
import argparse
import base64
import binascii
import hashlib
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
MEDIA = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,8}\Z")
BODY_LIMIT = 12 * 1024 * 1024
TERMINAL = {"SUCCEEDED","FAILED","BLOCKED","CANCELED","RECOVERY_REQUIRED","INTERRUPTED","STOPPED","COMPLETE","PARTIAL"}


class Rejected(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status=status


def need(ok, status, detail):
    if not ok:
        raise Rejected(status,detail)


def packed(obj):
    return json.dumps(obj,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":"))


class Archive:
    def __init__(self, root, budget=256*1024**2):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.guard=(self.root/".owner").open("a+b")
        if os.name=="nt":
            import msvcrt
            self.guard.seek(0)
            msvcrt.locking(self.guard.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(self.guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.budget=budget
        self.live_maps=None
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.root/"recordings.sqlite",check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,task TEXT NOT NULL,sources TEXT NOT NULL,
          status TEXT NOT NULL,task_status TEXT,reason TEXT,started INTEGER NOT NULL,ended INTEGER,bytes INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS by_task ON sessions(task,started);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,rid TEXT NOT NULL,eid TEXT NOT NULL,kind TEXT NOT NULL,
          stamp INTEGER NOT NULL,rx INTEGER NOT NULL,body TEXT NOT NULL,filename TEXT,blob BLOB,digest TEXT NOT NULL,
          UNIQUE(rid,eid),UNIQUE(rid,filename));
        CREATE INDEX IF NOT EXISTS by_stream ON events(rid,kind,seq);
        CREATE TABLE IF NOT EXISTS admission(singleton INTEGER PRIMARY KEY CHECK(singleton=1),draining INTEGER NOT NULL);
        INSERT OR IGNORE INTO admission VALUES(1,0);
        """)
        with self.db:
            self.db.execute("UPDATE sessions SET status='INTERRUPTED',ended=?,reason='recorder restarted during capture' WHERE status='ACTIVE'",(time.time_ns(),))

    def close(self):
        self.db.close()
        self.guard.close()

    def lifecycle(self,draining=None):
        with self.lock,self.db:
            if draining is not None:
                self.db.execute("UPDATE admission SET draining=? WHERE singleton=1",(int(draining),))
            return {"draining":bool(self.db.execute("SELECT draining FROM admission").fetchone()[0]),
                    "active_count":self.db.execute("SELECT count(*) FROM sessions WHERE status='ACTIVE'").fetchone()[0]}

    def session(self,rid):
        need(isinstance(rid,str) and IDENT.fullmatch(rid),400,"invalid recording_id")
        row=self.db.execute("SELECT * FROM sessions WHERE id=?",(rid,)).fetchone()
        need(row,404,"recording not found")
        return row

    def summary(self,rid):
        with self.lock:
            row=self.session(rid)
            streams=self.db.execute("SELECT kind,count(*) AS n,max(stamp) AS t,max(rx) AS rx FROM events WHERE rid=? GROUP BY kind",(rid,)).fetchall()
            counts={s["kind"]:s["n"] for s in streams}
            sources=json.loads(row["sources"])
            return dict(recording_id=rid,task_id=row["task"],sources=sources,status=row["status"],task_status=row["task_status"],
                        reason=row["reason"],started_ns=row["started"],ended_ns=row["ended"],bytes=row["bytes"],
                        counts=counts,latest={s["kind"]:s["t"] for s in streams},
                        source_last_receive_ns={s["kind"]:s["rx"] for s in streams},missing_sources=[k for k in sources if k not in counts])

    def start(self,obj):
        rid,task,sources=(obj.get(k) for k in ("recording_id","task_id","sources"))
        need(isinstance(rid,str) and IDENT.fullmatch(rid) and not rid.lower().startswith("recordings.sqlite"),
             400,"invalid recording_id")
        need(isinstance(task,str) and 0<len(task)<=200,400,"invalid task_id")
        need(isinstance(sources,list) and 0<len(sources)<=32 and all(isinstance(k,str) and IDENT.fullmatch(k) for k in sources)
             and len(set(sources))==len(sources),400,"sources must be distinct names")
        sources=packed(sorted(sources))
        with self.lock,self.db:
            old=self.db.execute("SELECT task,sources FROM sessions WHERE id=?",(rid,)).fetchone()
            if old:
                need(tuple(old)==(task,sources),409,"recording already bound")
            else:
                need(not self.db.execute("SELECT draining FROM admission").fetchone()[0],409,"recorder draining")
                self.db.execute("INSERT INTO sessions(id,task,sources,status,started) VALUES(?,?,?,'ACTIVE',?)",(rid,task,sources,time.time_ns()))
            return self.summary(rid)

    def append(self,rid,obj):
        with self.lock:
            row=self.session(rid)
            need(row["task"]==obj.get("task_id"),409,"wrong task")
            kind,eid,ts=obj.get("kind"),obj.get("event_id"),obj.get("stamp_ns")
            need(isinstance(eid,str) and 0<len(eid)<=256 and not any(ord(c)<32 for c in eid),400,"invalid event_id")
            need(kind in json.loads(row["sources"]) and type(ts) is int and 0<ts<2**63,400,"invalid source or timestamp")
            body=dict(obj)
            media=body.pop("media",None)
            filename=blob=None
            if media is not None:
                need(isinstance(media,dict),400,"invalid media")
                filename=media.get("name")
                need(isinstance(filename,str) and len(filename)<=160 and MEDIA.fullmatch(filename),400,"invalid media name")
                try:
                    blob=base64.b64decode(media["base64"],validate=True)
                except (KeyError,ValueError,TypeError,binascii.Error):
                    raise Rejected(400,"invalid media payload")
                need(0<len(blob)<=8*1024**2,413,"invalid media size")
                body["file"]=filename
                body["sha256"]=hashlib.sha256(blob).hexdigest()
            else:
                need("file" not in body,400,"file requires attached media")
            data=packed(body)
            checksum=hashlib.sha256(data.encode()).hexdigest()
            old=self.db.execute("SELECT seq,digest FROM events WHERE rid=? AND eid=?",(rid,eid)).fetchone()
            if old:
                need(old["digest"]==checksum,409,"event id reused with different content")
                return {"seq":old["seq"],"duplicate":True}
            need(row["status"]=="ACTIVE",409,"recording is closed")
            n=len(data.encode())+len(blob or b"")
            if row["bytes"]+n>self.budget:
                with self.db:
                    self.db.execute("UPDATE sessions SET status='LIMIT_REACHED',ended=?,reason='per-recording byte limit' WHERE id=?",(time.time_ns(),rid))
                raise Rejected(507,"recording byte limit reached")
            try:
                with self.db:
                    cur=self.db.execute("INSERT INTO events(rid,eid,kind,stamp,rx,body,filename,blob,digest) VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid,eid,kind,ts,time.time_ns(),data,filename,blob,checksum))
                    self.db.execute("UPDATE sessions SET bytes=bytes+? WHERE id=?",(n,rid))
            except sqlite3.IntegrityError:
                raise Rejected(409,"media path already exists")
            return {"seq":cur.lastrowid,"duplicate":False}

    def events(self,rid,after,limit):
        with self.lock:
            self.session(rid)
            need(0<=after<2**63 and 1<=limit<=256,400,"invalid cursor or limit")
            rows=self.db.execute("SELECT seq,rx,body FROM events WHERE rid=? AND seq>? ORDER BY seq LIMIT ?",(rid,after,limit))
            return [dict(json.loads(r["body"]),seq=r["seq"],received_ns=r["rx"]) for r in rows]

    def map_page(self,rid):
        with self.lock:
            self.session(rid)
        return Path(__file__).with_name("live_map.html").read_bytes()

    def map_view(self,rid,segment,previous_etag):
        with self.lock:
            row=self.session(rid)
            if self.live_maps is None:
                from .live_map import LiveMaps
                self.live_maps=LiveMaps(self.root/"recordings.sqlite")
            fields=dict(recording_id=rid,task_id=row["task"],status=row["status"],sources=json.loads(row["sources"]))
        etag,view=self.live_maps.view(rid,segment,status=fields["status"],previous_etag=previous_etag)
        return etag, ({**fields,**view} if view is not None else None)

    def finish(self,rid,obj):
        with self.lock:
            row=self.session(rid)
            status,reason=obj.get("status"),obj.get("reason","")
            need(row["task"]==obj.get("task_id"),409,"wrong task")
            need(isinstance(status,str) and status in TERMINAL and isinstance(reason,str) and len(reason)<=1000,400,"invalid finish")
            if row["status"]=="ACTIVE":
                with self.db:
                    self.db.execute("UPDATE sessions SET status='FINISHED',task_status=?,ended=?,reason=? WHERE id=?",(status,time.time_ns(),reason,rid))
            elif row["status"]=="FINISHED":
                need((row["task_status"],row["reason"])==(status,reason),409,"different terminal receipt")
        return self.export(rid)

    def export(self,rid):
        # Closed sessions are immutable. Use a separate read connection so exporting
        # video bytes never holds the writer lock for another recording.
        summary=self.summary(rid)
        need(summary["status"]!="ACTIVE",409,"finish before exporting")
        folder=self.root/rid/"media"/"mission"
        folder.mkdir(parents=True,exist_ok=True)
        tmp=folder/("."+uuid.uuid4().hex+".jsonl")
        db=sqlite3.connect((self.root/"recordings.sqlite").resolve().as_uri()+"?mode=ro",uri=True)
        try:
            previous={}
            timing={}
            with tmp.open("x",encoding="utf8",newline="\n") as stream:
                for seq,rx,body,name,blob in db.execute("SELECT seq,rx,body,filename,blob FROM events WHERE rid=? ORDER BY seq",(rid,)):
                    record=json.loads(body)
                    kind,ts=record["kind"],record["stamp_ns"]
                    stat=timing.setdefault(kind,dict(first_source_ns=ts,last_source_ns=ts,
                                      max_source_gap_ns=0,max_receive_gap_ns=0,source_rewinds=0))
                    if kind in previous:
                        last,received=previous[kind]
                        stat["max_source_gap_ns"]=max(stat["max_source_gap_ns"],ts-last)
                        stat["max_receive_gap_ns"]=max(stat["max_receive_gap_ns"],rx-received)
                        stat["source_rewinds"]+=int(ts<last)
                    previous[kind]=ts,rx
                    stat["last_source_ns"]=ts
                    if name:
                        dest=folder/name
                        dest.parent.mkdir(exist_ok=True)
                        item=dest.with_name("."+uuid.uuid4().hex)
                        item.write_bytes(blob)
                        os.replace(item,dest)
                    stream.write(packed(dict(record,seq=seq,received_ns=rx))+"\n")
            os.replace(tmp,folder/"timeline.jsonl")
            summary["stream_timing"]=timing
            manifest=self.root/rid/("."+uuid.uuid4().hex)
            manifest.write_text(packed(summary)+"\n","utf8")
            os.replace(manifest,self.root/rid/"recording.json")
        finally:
            db.close()
            tmp.unlink(missing_ok=True)
        return summary


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def handle_api(self,post):
        db=self.server.archive
        etag=None
        try:
            url=urlsplit(self.path)
            parts=url.path.strip("/").split("/")
            if post:
                size=int(self.headers.get("Content-Length","0"))
                need(0<size<=BODY_LIMIT,413,"invalid body size")
                obj=json.loads(self.rfile.read(size))
                need(isinstance(obj,dict),400,"expected JSON object")
            if post and parts==["recordings","start"]: result=db.start(obj)
            elif parts[0]=="recordings" and len(parts)==3 and post and parts[2]=="events": result=db.append(parts[1],obj)
            elif parts[0]=="recordings" and len(parts)==3 and post and parts[2]=="finish": result=db.finish(parts[1],obj)
            elif parts[0]=="recordings" and len(parts)==3 and post and parts[2]=="export": result=db.export(parts[1])
            elif not post and parts==["health"]: result={"service":"task-recorder","version":"1",**db.lifecycle()}
            elif post and parts in (["admin","drain"],["admin","enable"]):
                need(ipaddress.ip_address(self.client_address[0]).is_loopback,403,"local service admin only")
                result=db.lifecycle(parts[1]=="drain")
            elif parts[0]=="recordings" and len(parts)==2 and not post: result=db.summary(parts[1])
            elif parts[0]=="recordings" and len(parts)==3 and parts[2]=="events" and not post:
                q=parse_qs(url.query)
                result={"events":db.events(parts[1],int(q.get("after",[0])[0]),int(q.get("limit",[100])[0]))}
            elif not post and len(parts)==3 and parts[0]=="recordings" and parts[2]=="global-map":
                q=parse_qs(url.query,keep_blank_values=True)
                need(set(q)<= {"segment"} and len(q.get("segment",[]))<=1,400,"invalid map query")
                etag,result=db.map_view(parts[1],int(q["segment"][0]) if "segment" in q else None,
                                        self.headers.get("If-None-Match"))
                if result is None:
                    self.send_response(304)
                    self.send_header("ETag",etag)
                    self.send_header("Cache-Control","private, no-cache")
                    self.end_headers()
                    return
            elif not post and len(parts)==3 and parts[0]=="recordings" and parts[2]=="map-view":
                raw=db.map_page(parts[1])
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(raw)))
                self.send_header("Cache-Control","no-store")
                self.send_header("X-Content-Type-Options","nosniff")
                self.send_header("Content-Security-Policy","default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(raw)
                return
            elif parts==["recordings"] and not post:
                task=parse_qs(url.query).get("task_id",[""])[0]
                with db.lock:
                    result={"recordings":[r[0] for r in db.db.execute("SELECT id FROM sessions WHERE task=? ORDER BY started DESC LIMIT 256",(task,))]}
            else: raise Rejected(404,"unknown recorder resource")
            code=200
        except Rejected as exc:
            code,result=exc.status,{"error":str(exc)}
        except (ValueError,TypeError,OverflowError,UnicodeError):
            code,result=400,{"error":"invalid request"}
        except (OSError,sqlite3.Error) as exc:
            code,result=503,{"error":"archive unavailable","detail":str(exc)}
        raw=packed(result).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        if etag:
            self.send_header("ETag",etag)
            self.send_header("Cache-Control","private, no-cache")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self): self.handle_api(False)
    def do_POST(self): self.handle_api(True)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--state",type=Path,required=True)
    cli.add_argument("--bind",default="127.0.0.1")
    cli.add_argument("--port",type=int,default=8188)
    cli.add_argument("--max-mib",type=int,default=256)
    args=cli.parse_args()
    if not 1<=args.max_mib<=8192: cli.error("max-mib must be in [1,8192]")
    with ThreadingHTTPServer((args.bind,args.port),Handler) as server:
        server.archive=Archive(args.state,args.max_mib*1024**2)
        try: server.serve_forever()
        finally: server.archive.close()
