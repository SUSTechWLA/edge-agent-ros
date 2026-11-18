"""Bound the wall time and bytes of one direct request to a configured local service."""
from concurrent.futures import Future
from http.client import HTTPConnection, HTTPSConnection
import socket
import threading
from urllib.parse import urlsplit


class TransportError(ValueError):
    def __init__(self, reason, *, prefix=b""):
        super().__init__(reason)
        self.reason, self.prefix = reason, prefix


# A DNS resolver may return late even after the caller stops waiting.
_slots = threading.BoundedSemaphore(4)


def read(url, body, limit, timeout, *, capture_prefix=False):
    target = urlsplit(url)
    if target.scheme not in ("http","https") or not target.hostname or target.username is not None or target.fragment:
        raise TransportError("endpoint_invalid")
    connection_type = HTTPSConnection if target.scheme=="https" else HTTPConnection
    connection = connection_type(target.hostname,target.port,timeout=timeout)
    path = target.path or "/"
    if target.query:
        path += "?"+target.query
    if not _slots.acquire(blocking=False):
        raise TransportError("transport_busy")
    result,abandoned,sockets = Future(),threading.Event(),[]

    def receive():
        try:
            connection.connect()
            sockets.append(connection.sock)
            if abandoned.is_set():
                return
            connection.request("GET" if body is None else "POST",path,body=body,
                               headers={"Content-Type":"application/json"} if body is not None else {})
            with connection.getresponse() as reply:
                if reply.status!=200:
                    raise TransportError("endpoint_status")
                declared = reply.getheader("Content-Length")
                if declared is not None:
                    try:
                        declared = int(declared)
                    except ValueError as exc:
                        raise TransportError("invalid_length") from exc
                    if declared<0 or declared>limit and not capture_prefix:
                        raise TransportError("reply_size")
                raw = reply.read(limit+1)
                if len(raw)>limit:
                    raise TransportError("reply_size",prefix=raw if capture_prefix else b"")
                if declared is not None and len(raw)!=declared:
                    raise TransportError("incomplete_reply")
                result.set_result(raw)
        except Exception as exc:
            result.set_exception(exc)
        finally:
            connection.close()
            _slots.release()

    try:
        threading.Thread(target=receive,daemon=True,name="bounded-http-read").start()
    except RuntimeError as exc:
        _slots.release()
        raise TransportError("transport_busy") from exc
    try:
        return result.result(timeout)
    except TimeoutError as exc:
        abandoned.set()
        if sockets and sockets[0] is not None:
            try:
                sockets[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise TransportError("deadline") from exc
