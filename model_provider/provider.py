"""Ollama-compatible Model Adapter for Edge Agent ROS."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from module_journal import ModuleJournal
from author_tool import writer_lock
from structured import current_schema, matches

MODEL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]{0,127}")
KEEP_ALIVE = re.compile(r"-1|0|[1-9][0-9]{0,3}[smh]")
MAX_BODY = 131072
TIMEOUT = 75


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def unique(pairs):
    value = {}
    for k,v in pairs:
        if k in value:
            raise ValueError("duplicate")
        value[k] = v
    return value


def loads(raw):
    data = json.loads(raw, object_pairs_hook=unique)
    encoded(data)  # Reject infinite numbers and unpaired surrogates before external calls.
    return data


class Fault(Exception):
    def __init__(self, reason, status=400, upstream_status=None):
        self.reason, self.status, self.upstream_status = reason, status, upstream_status
        self.receipt = {}


def read_routes(directory):
    try:
        with (Path(directory) / "routes.json").open("rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError("size")
        data = loads(raw)
        if not isinstance(data,dict) or set(data)!={"schema_version","routes"} or type(data["schema_version"])!=int or data["schema_version"]!=1:
            raise ValueError("version")
        routes = data["routes"]
        if not isinstance(routes,dict) or len(routes)>64:
            raise ValueError("routes")
        for name, spec in routes.items():
            if (not MODEL_NAME.fullmatch(name) or not isinstance(spec,dict) or set(spec)!={"provider","model"}
                    or not isinstance(spec["model"],str) or not MODEL_NAME.fullmatch(spec["model"])
                    or spec["provider"] not in ("ollama","deepseek")
                    or (spec["provider"]=="deepseek") != name.startswith("deepseek-")):
                raise ValueError("route")
        return routes, digest(data)
    except (OSError,ValueError,TypeError,RecursionError) as ex:
        raise Fault("config_invalid",503) from ex


def messages_for(path, body):
    if path == "/api/generate":
        if not isinstance(body.get("prompt"),str) or not isinstance(body.get("system",""),str):
            raise Fault("prompt_invalid")
        system = [{"role":"system","content":body["system"]}] if body.get("system") else []
        return system + [{"role":"user","content":body["prompt"]}]
    messages = body.get("messages")
    if (not isinstance(messages,list) or not 1<=len(messages)<=64
            or any(not isinstance(m,dict) or set(m)!={"role","content"}
                   or m["role"] not in ("system","user","assistant") or not isinstance(m["content"],str)
                   for m in messages)):
        raise Fault("messages_invalid")
    return messages


def validate(path, body):
    if path not in ("/api/chat","/api/generate"):
        raise Fault("not_found",404)
    if not isinstance(body,dict):
        raise Fault("request_invalid")
    allowed = {"messages"} if path=="/api/chat" else {"prompt","system"}
    if set(body)-{"model","stream","format","options","think","keep_alive"}-allowed or body.get("stream",False) is not False:
        raise Fault("request_unsupported")
    if not isinstance(body.get("model"),str) or not MODEL_NAME.fullmatch(body["model"]):
        raise Fault("model_invalid")
    if "think" in body and type(body["think"]) is not bool:
        raise Fault("options_invalid")
    if "keep_alive" in body:
        keep = body["keep_alive"]
        if not ((type(keep) is int and -1 <= keep <= 86400) or (isinstance(keep,str) and KEEP_ALIVE.fullmatch(keep))):
            raise Fault("options_invalid")
    fmt = body.get("format")
    if fmt is not None and fmt!="json" and not isinstance(fmt,dict):
        raise Fault("format_invalid")
    opts = body.get("options",{})
    if not isinstance(opts,dict) or set(opts)-{"temperature","num_ctx","num_predict","num_thread"}:
        raise Fault("options_invalid")
    for name,low,high,types in (("temperature",0,2,(int,float)),("num_ctx",128,65536,(int,)),
                                ("num_predict",1,8192,(int,)),("num_thread",1,64,(int,))):
        if name in opts and (type(opts[name]) not in types or not low<=opts[name]<=high):
            raise Fault("options_invalid")
    return messages_for(path, body)


def cloud_payload(body, model, messages):
    fmt, opts = body.get("format"), body.get("options",{})
    thinking = "enabled" if body.get("think") is True else "disabled"
    payload = {"model":model,"messages":messages,"stream":False,"thinking":{"type":thinking},
               "temperature":opts.get("temperature",0),"max_tokens":opts.get("num_predict",2048)}
    if fmt is not None:
        if isinstance(fmt,dict) and (fmt.get("type","object")!="object" or not current_schema(fmt)):
            raise Fault("format_unsupported")
        instruction = "Return one JSON object only."
        if isinstance(fmt,dict):
            instruction += " Follow this JSON Schema exactly: " + encoded(fmt).decode()
        # Keep the caller's system message first and preserve the role order.
        if messages[0]["role"]=="system":
            payload["messages"] = [{**messages[0],"content":messages[0]["content"]+"\n"+instruction},*messages[1:]]
        else:
            payload["messages"] = [{"role":"system","content":instruction},*messages]
        payload["response_format"] = {"type":"json_object"}
    return payload


def contact(provider, path, body):
    # A hard wall deadline also covers DNS and drip-fed replies on Windows and Linux.
    # The helper's argv, stdout failures and stderr carry no request or credentials.
    try:
        result = subprocess.run([sys.executable,"-B",str(Path(__file__).with_name("upstream.py"))],
                                input=encoded({"provider":provider,"path":path,"body":body}),
                                capture_output=True, timeout=TIMEOUT, check=True)
        exchange = loads(result.stdout)
    except subprocess.TimeoutExpired as ex:
        raise Fault("timeout",504) from ex
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError) as ex:
        raise Fault("transport_failed",502) from ex
    if "error" in exchange:
        reason = exchange["error"]
        status = {"rate_limit":429,"timeout":504,"key_unavailable":503,"local_url_invalid":503}.get(reason,502)
        raise Fault(reason,status,exchange.get("upstream_status"))
    return exchange["reply"]


def normalized_reply(provider, path, body, reply):
    try:
        if provider=="deepseek":
            first = reply["choices"][0]
            reason, content = first["finish_reason"], first["message"]["content"]
            usage = reply.get("usage",{})
            metrics = {"prompt_eval_count":usage.get("prompt_tokens"),"eval_count":usage.get("completion_tokens")}
        else:
            if reply.get("done") is not True:
                raise ValueError("incomplete")
            reason = reply.get("done_reason","stop")
            content = reply["message"]["content"] if path=="/api/chat" else reply["response"]
            metrics = {k:reply.get(k) for k in ("prompt_eval_count","eval_count")}
        if reason not in ("length","stop") or not isinstance(content,str):
            raise ValueError("finish")
        if reason=="length":
            # Old clients may parse content without inspecting done_reason.
            content = ""
        elif not content.strip():
            raise Fault("empty_reply",502)
        elif body.get("format") is not None:
            value = loads(content)
            if provider=="deepseek" and not isinstance(value,dict):
                raise ValueError("object")
            if provider=="deepseek" and isinstance(body["format"],dict) and not matches(value,body["format"],[65536]):
                raise Fault("schema_mismatch",502)
        output = {"model":body["model"],"done":True,"done_reason":reason,
                  **{k:v for k,v in metrics.items() if type(v) is int and v>=0}}
        if path=="/api/chat":
            output["message"] = {"role":"assistant","content":content}
        else:
            output["response"] = content
        return output
    except (KeyError, IndexError, ValueError, TypeError, AttributeError, RecursionError) as ex:
        raise Fault("reply_invalid",502) from ex


class Adapter:
    def __init__(self, routes, journal):
        self.routes, self.journal = routes, journal
        self.lock = threading.Lock()
        with journal.db:
            journal.db.execute("CREATE TABLE IF NOT EXISTS model_provider_pending("
                               "call_id TEXT PRIMARY KEY, parent TEXT NOT NULL, meta TEXT NOT NULL)")
        for call,parent,meta in journal.db.execute("SELECT call_id,parent,meta FROM model_provider_pending").fetchall():
            self.emit(call,"failed",{**loads(meta),"reason":"outcome_unknown"},parent)

    def emit(self, call, stage, data, parent=None):
        with self.lock, self.journal.db:
            event_id = self.journal.emit({"module":"model.provider","kind":"model.provider."+stage,
                    "code":"model."+data.get("reason",stage),"trace_id":call,"call_id":call,
                    "parent_event_id":parent,"level":"warn" if stage=="failed" else "info","data":data})
            if stage=="requested":
                self.journal.db.execute("INSERT INTO model_provider_pending VALUES(?,?,?)",
                                        (call,event_id,encoded(data).decode()))
            elif parent:
                self.journal.db.execute("DELETE FROM model_provider_pending WHERE call_id=?",(call,))
            return event_id

    def invoke(self, path, body, transport=contact):
        call, parent = uuid.uuid4().hex, None
        meta = {"request_sha256":digest(body),"protocol":path.removeprefix("/api/")}
        started = time.monotonic()
        try:
            messages = validate(path, body)
            routes, version = read_routes(self.routes)
            alias = body["model"]
            if alias not in routes:
                raise Fault("route_missing",404)
            provider, model = routes[alias]["provider"], routes[alias]["model"]
            meta.update(route_sha256=version, alias=alias, provider=provider, model=model)
            payload = (cloud_payload(body, model, messages) if provider=="deepseek"
                       else {**body, "model":model, "stream":False})
            parent = self.emit(call,"requested",meta)
            result = normalized_reply(provider,path,body,transport(provider,path,payload))
            completed = self.emit(call,"completed",{**meta,"done_reason":result["done_reason"],
                      "elapsed_ms":round((time.monotonic()-started)*1000),
                      **{k:result[k] for k in ("eval_count","prompt_eval_count") if k in result}},parent)
            result["adapter"] = {"call_id":call,"event_id":completed,"route_sha256":version,"upstream_model":model}
            return result
        except Fault as ex:
            info = {**meta,"reason":ex.reason,"elapsed_ms":round((time.monotonic()-started)*1000)}
            if ex.upstream_status is not None:
                info["upstream_status"]=ex.upstream_status
            failed = self.emit(call,"failed",info,parent)
            ex.receipt = {"call_id":call,"event_id":failed}
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self, status, value):
        wire = encoded(value)
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def do_GET(self):
        try:
            if self.path not in ("/health","/api/tags"):
                raise Fault("not_found",404)
            routes, version = read_routes(self.server.adapter.routes)
            result = ({"status":"ready","route_sha256":version} if self.path=="/health"
                      else {"models":[{"name":name,"model":name} for name in sorted(routes)]})
        except Fault as ex:
            return self.reply(ex.status,{"error":"model."+ex.reason})
        self.reply(200,result)

    def do_POST(self):
        parsed = False
        try:
            if self.path not in ("/api/chat","/api/generate"):
                raise Fault("not_found",404)
            lengths = self.headers.get_all("Content-Length",[])
            if len(lengths)!=1 or self.headers.get("Transfer-Encoding") is not None:
                raise Fault("request_invalid")
            size = int(lengths[0])
            if size > MAX_BODY:
                raise Fault("request_too_large",413)
            if size<=0 or self.headers.get_content_type()!="application/json":
                raise Fault("request_invalid")
            raw = self.rfile.read(size)
            if len(raw)!=size:
                raise Fault("request_invalid")
            body = loads(raw)
            parsed = True
            result = self.server.adapter.invoke(self.path,body)
        except (Fault,ValueError,TypeError,RecursionError,sqlite3.Error) as error:
            fault = error if isinstance(error,Fault) else Fault("request_invalid")
            if isinstance(error,sqlite3.Error):
                fault = Fault("journal_unavailable",503)
            elif not parsed and self.path in ("/api/chat","/api/generate"):
                try:
                    self.server.adapter.emit(uuid.uuid4().hex,"failed",{"reason":fault.reason})
                except (sqlite3.Error,OSError):
                    fault = Fault("journal_unavailable",503)
            return self.reply(fault.status,{"error":"model."+fault.reason,**fault.receipt})
        self.reply(200,result)


class Server(ThreadingHTTPServer):
    def __init__(self,address,adapter):
        self.slots = threading.BoundedSemaphore(4)
        self.adapter = adapter
        super().__init__(address,Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request,address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(TIMEOUT+5,expire)
        try:
            request.settimeout(10)
            timer.start()
            super().process_request_thread(request,address)
        finally:
            timer.cancel()
            self.slots.release()

    def handle_error(self, request, address):
        # Socket disconnects and timeouts may happen while reading transport headers.
        if not isinstance(sys.exc_info()[1],(OSError,TimeoutError)):
            super().handle_error(request,address)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes",type=Path,default=Path("/routes"))
    parser.add_argument("--journal",type=Path,default=Path("/journal/module-events.sqlite"))
    parser.add_argument("--bind",default=os.getenv("EDGE_MODEL_BIND","0.0.0.0"))
    parser.add_argument("--port",type=int,default=8134)
    args = parser.parse_args()
    # Reconciliation has a single owner per source volume, including overlapping restarts.
    with writer_lock(args.journal.parent):
        journal = ModuleJournal(args.journal)
        try:
            with Server((args.bind,args.port),Adapter(args.routes,journal)) as server:
                server.serve_forever()
        finally:
            journal.close()
