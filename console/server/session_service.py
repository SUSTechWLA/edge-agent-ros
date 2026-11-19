"""Thin, bounded consumer of the replaceable session.read runtime service."""
from concurrent.futures import Future
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import threading
from urllib.parse import urlsplit
import uuid

sys.path.append(str(Path(__file__).resolve().parents[2] / "agent"))
from runtime_plugins import PluginUnavailable, ServiceSnapshot
import soft_client
from task_data import identifiers
from tracing import clean_text


TIMEOUT = 8
MAX_REPLY_BYTES = 256 * 1024
_SLOTS = threading.BoundedSemaphore(4)


class SessionUnavailable(Exception):
    def __init__(self, code, message, status=503, details=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = details


def _pin():
    try:
        directory = ServiceSnapshot(Path(os.getenv("EDGE_PLUGIN_DIR", "/etc/edge/plugins"))/"runtime")
        if soft_client.enabled():
            view = soft_client.request("session_pin")
            if view["claimed"]:
                item = view["service"]
                if item is None:
                    raise SessionUnavailable("session.draining","会话提供者当前不可用。")
                legacy, pin = item["legacy"], item["pin"]
                if directory.owners.get("session.read"):
                    old = directory.pin("session.read")
                    if not legacy or any(old[k] != legacy[v] for k,v in (
                            ("plugin_id","id"),("plugin_version","version"),("endpoint","endpoint"))):
                        raise SessionUnavailable("session.service_conflict","旧服务与当前许可不一致。")
                return {"service":"session.read","plugin_id":pin["plugin_id"],
                        "plugin_version":legacy["version"] if legacy else pin["version"],
                        "binding":pin["binding"].removeprefix("sha256:"),"soft_pin":pin}
        return directory.pin("session.read")
    except soft_client.SoftError as error:
        raise SessionUnavailable("session.transport",str(error)) from error
    except PluginUnavailable as error:
        code = "session." + error.code.removeprefix("plugin.")
        messages = {"session.service_missing":"会话插件未安装；仍可查看任务目录和原始事件。",
                    "session.service_conflict":"多个插件同时提供会话服务，请修复注册冲突。",
                    "session.draining":"会话插件正在退出，暂不接收新请求。"}
        raise SessionUnavailable(code, messages.get(code,"会话插件依赖暂不可用，请检查服务目录。")) from error
    except OSError as error:
        raise SessionUnavailable("session.registry_unavailable","会话服务目录暂不可读。") from error


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON number")
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite JSON number")
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid, parse_float=number)


def _post(endpoint, request):
    if not _SLOTS.acquire(blocking=False):
        raise SessionUnavailable("session.busy","会话服务并发请求已达上限，请稍后重试。")
    destination = urlsplit(endpoint)
    cls = HTTPSConnection if destination.scheme == "https" else HTTPConnection
    conn = cls(destination.hostname, destination.port, timeout=TIMEOUT)
    future, abandoned, sockets = Future(), threading.Event(), []

    def send():
        try:
            conn.connect()
            sockets.append(conn.sock)
            if abandoned.is_set():
                return
            conn.request("POST", destination.path or "/", body=json.dumps(request).encode(),
                         headers={"Content-Type":"application/json"})
            response = conn.getresponse()
            with response:
                length = response.getheader("Content-Length")
                if length is not None and not 0 <= int(length) <= MAX_REPLY_BYTES:
                    raise ValueError("invalid response size")
                raw = response.read(MAX_REPLY_BYTES+1)
                if len(raw)>MAX_REPLY_BYTES or length is not None and int(length)!=len(raw):
                    raise ValueError("invalid response size")
                future.set_result((response.status,_decode(raw)))
        except Exception as error:
            future.set_exception(error)
        finally:
            conn.close()
            _SLOTS.release()

    try:
        threading.Thread(target=send,daemon=True,name="session-http").start()
    except RuntimeError as error:
        _SLOTS.release()
        raise SessionUnavailable("session.busy","会话请求线程暂不可用。") from error
    try:
        return future.result(timeout=TIMEOUT)
    except TimeoutError as error:
        abandoned.set()
        if sockets:
            try:
                sockets[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise SessionUnavailable("session.timeout","会话插件读取超时，请稍后重试。") from error
    except (OSError, HTTPException) as error:
        raise SessionUnavailable("session.transport","会话插件连接失败；原始事件仍可读取。") from error
    except (ValueError, RecursionError) as error:
        raise SessionUnavailable("session.response_invalid","会话插件返回的数据格式或大小不符合契约。") from error


def _document(value, scope):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"]!=1:
        raise ValueError("invalid document")
    actual = value.get("scope")
    if not isinstance(actual, dict) or any(actual.get(key)!=identity for key,identity in scope.items()):
        raise ValueError("response scope mismatch")
    for name in ("task_id", "trace_id"):
        if actual.get(name) is not None:
            identifiers(**{name:actual[name]})
    task = value.get("task")
    if not isinstance(task,dict) or task.get("id")!=actual.get("task_id") or not isinstance(task.get("state"),str):
        raise ValueError("invalid task identity")
    if not isinstance(value.get("headline"),str) or not isinstance(value.get("markdown"),str):
        raise ValueError("invalid text")
    messages, related, coverage = value.get("messages"), value.get("related_tasks"), value.get("coverage")
    if not isinstance(messages,list) or len(messages)>64 or not isinstance(related,list) or len(related)>100:
        raise ValueError("invalid collection")
    if type(value.get("related_tasks_has_more")) is not bool or not isinstance(coverage,dict):
        raise ValueError("invalid coverage")
    for message in messages:
        if (not isinstance(message,dict) or message.get("role") not in ("user","agent","tool","system")
                or not all(isinstance(message.get(k),str) for k in ("title","text","source"))):
            raise ValueError("invalid message")
        _refs(message.get("refs"))
    for item in related:
        if not isinstance(item,dict) or not all(isinstance(item.get(k),str) for k in ("id","text","state")):
            raise ValueError("invalid related task")
    diagnosis = value.get("diagnosis")
    if diagnosis is not None:
        if not isinstance(diagnosis,dict) or type(diagnosis.get("historical")) is not bool:
            raise ValueError("invalid diagnosis")
        _refs(diagnosis.get("refs"))
    for key in ("through_seq","archived_events","selected_events","collapsed_events","public_payload_bytes"):
        if type(coverage.get(key)) is not int or coverage[key]<0:
            raise ValueError("invalid coverage counter")
    if (not isinstance(coverage.get("notes"),list) or not all(isinstance(n,str) for n in coverage["notes"])
            or type(coverage.get("truncated")) is not bool or type(coverage.get("task_available")) is not bool):
        raise ValueError("invalid coverage fields")
    if not isinstance(value.get("links"),dict) or not all(isinstance(value["links"].get(k),str)
                                                         for k in ("events","export","markdown")):
        raise ValueError("invalid links")
    return value


def _refs(refs):
    if not isinstance(refs,list) or len(refs)>500:
        raise ValueError("invalid references")
    for reference in refs:
        if not isinstance(reference,dict) or type(reference.get("seq")) is not int or reference["seq"]<=0:
            raise ValueError("invalid event reference")


def _read(binding, metadata, scope):
    wire = {**metadata,**scope}
    if "soft_pin" in binding:
        try:
            status, reply = soft_client.request("session",pin=binding["soft_pin"],wire=wire)
        except soft_client.SoftError as error:
            raise SessionUnavailable("session.transport",str(error)) from error
    else:
        status, reply = _post(binding["endpoint"], wire)
    try:
        current = _pin()
    except SessionUnavailable as error:
        raise SessionUnavailable("session.binding_changed","读取期间会话插件已退出或更换，请重新读取。") from error
    if any(current[key]!=binding[key] for key in ("plugin_id","plugin_version","binding")):
        raise SessionUnavailable("session.binding_changed","读取期间会话插件版本已变化，请重新读取。")
    try:
        if not isinstance(reply,dict) or any(reply.get(key)!=value for key,value in metadata.items()):
            raise ValueError("provider metadata mismatch")
        if type(reply.get("schema_version")) is not int:
            raise ValueError("invalid schema type")
        if status==404:
            if reply.get("code")!="session.not_found":
                raise ValueError("invalid missing response")
            raise SessionUnavailable("session.not_found","未找到任务或归档证据。",404)
        if status!=200:
            code,error = reply.get("code"),reply.get("error")
            if (not isinstance(code,str) or not re.fullmatch(r"session\.[a-zA-Z][a-zA-Z0-9_.-]{0,79}",code)
                    or not isinstance(error,dict) or error.get("code")!=code
                    or not isinstance(error.get("message"),str)):
                raise ValueError("invalid error receipt")
            raise SessionUnavailable(code,clean_text(error["message"])[:1200])
        document = _document(reply.get("document"),scope)
    except (ValueError,TypeError,RecursionError) as error:
        raise SessionUnavailable("session.response_invalid","会话插件响应的身份、范围或格式不符合契约。") from error
    document["service"] = {key:binding[key] for key in ("service","plugin_id","plugin_version","binding")}
    document["request_id"] = metadata["request_id"]
    document["markdown"] += ("\n会话提供者 "+binding["plugin_id"]+" @ "+binding["plugin_version"]+
                              " · binding "+binding["binding"]+"\n")
    document["coverage"]["summary_characters"] = len(document["markdown"])
    document["coverage"]["summary_bytes"] = len(document["markdown"].encode("utf-8"))
    return document


def read_session(task_id=None, trace_id=None):
    scope = identifiers(task_id, trace_id)
    binding = _pin()
    metadata = {"schema_version":1,"request_id":uuid.uuid4().hex,
                **{key:binding[key] for key in ("plugin_id","plugin_version","binding")}}
    try:
        return _read(binding,metadata,scope)
    except SessionUnavailable as error:
        error.details = {**{key:value for key,value in metadata.items() if key!="schema_version"},"origin":"session.read"}
        raise
