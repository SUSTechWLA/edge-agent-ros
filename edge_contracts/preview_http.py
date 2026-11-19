"""Bounded HTTP transport for read-only image preview endpoints."""
from http.server import ThreadingHTTPServer
import json
import sys
import threading


def json_reply(status,obj):
    return status,"application/json",json.dumps(obj,allow_nan=False).encode(),{}


class PreviewHTTP(ThreadingHTTPServer):
    def __init__(self,address,node,handler):
        self.node=node
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(address,handler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(3)
        try:
            super().process_request(request,address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self,request,address):
        def expire():
            try:
                request.shutdown(2)
            except OSError:
                pass
        limit=threading.Timer(5,expire)
        try:
            limit.start()
            super().process_request_thread(request,address)
        finally:
            limit.cancel()
            self.slots.release()

    def handle_error(self,request,address):
        if not isinstance(sys.exc_info()[1],OSError):
            super().handle_error(request,address)
