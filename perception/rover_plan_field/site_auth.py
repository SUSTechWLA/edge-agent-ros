import os
import base64
import binascii
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import secrets
import re
from html import escape
import time
from threading import Lock
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ADDRESS = ip_network("192.168.0.0/23")
AUTH = Path("/access/accounts.json")
SITE = os.environ["PLAN_PUBLIC_HOST"]
SESSIONS = {}
SESSION_LOCK = Lock()
LOGIN = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 Jetson 观测台</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#101a22;color:#e6f1f4;font:16px system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}
main{padding:30px;border:1px solid #38535e;border-radius:14px;width:min(412px,94vw);background:#182833}
h1{font-size:24px;margin:0 0 24px}label{display:block;margin:16px 0 6px}
input,button{width:100%;font:inherit;padding:11px;border:1px solid #55727e;border-radius:7px}
input{background:#12202a;color:#fff}button{margin-top:20px;background:#7de2d2;color:#13272a;border:0;font-weight:700;cursor:pointer}p{color:#ffd8af}
</style><main><h1>Jetson 观测台</h1>{message}
<form method="post" action="/login"><input type="hidden" name="return_to" value="{return_to}">
<label for="name">账号</label><input id="name" name="username" autocomplete="username" required>
<label for="key">访问密钥</label><input id="key" name="password" type="password" autocomplete="off" required>
<button type="submit">登录观测台</button>
</form></main></html>"""

def return_path(path):
    url=urlsplit(path)
    if url.scheme or url.netloc or url.path not in ("/","/plan-observe.html"):
        return "/"
    fields=parse_qs(url.query,max_num_fields=16)
    allowed={}
    for key,pattern in (("task",r"[\w.-]{1,96}"),("scene",r"rgbd|example|field"),("capture",r"[0-9a-f]{64}")):
        value=fields.get(key,[""])[0]
        if re.fullmatch(pattern,value):
            allowed[key]=value
    return "/plan-observe.html?"+urlencode(allowed) if allowed else "/"


class Gateway(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(8)
        super().setup()

    def log_message(self, *_):
        pass

    def reply(self, status, raw, mime="application/json", challenge=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Jetson Console", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(raw)

    def accounts(self):
        try:
            return json.loads(AUTH.read_bytes())
        except (ValueError, OSError):
            return {}

    def check(self, username, password):
        record = self.accounts().get(username, {})
        proof = hashlib.sha256(password.encode()).hexdigest()
        return record if hmac.compare_digest(proof, record.get("sha256", "")) else None

    def role(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "edge_plan_session" in cookie:
                with SESSION_LOCK:
                    record = SESSIONS.get(cookie["edge_plan_session"].value)
                if record:
                    username, proof, expires = record
                    account = self.accounts().get(username, {})
                    if expires > time.monotonic() and hmac.compare_digest(proof, account.get("sha256", "")):
                        return account.get("role")
            scheme, token = self.headers.get("Authorization", "").split(" ", 1)
            if scheme.lower() == "basic" and len(token) <= 512:
                username, password = base64.b64decode(token, validate=True).decode().split(":", 1)
                return (self.check(username, password) or {}).get("role")
        except (ValueError, TypeError, binascii.Error, CookieError):
            pass
        return None

    def login_page(self, denied=False, target=None):
        page = LOGIN.replace("{message}", "<p>账号或访问密钥不正确。</p>" if denied else "")
        try:
            dest=return_path(target if target is not None else self.path)
        except ValueError:
            dest="/"
        page=page.replace("{return_to}",escape(dest,quote=True))
        self.reply(401 if denied else 200, page.encode(), "text/html; charset=utf-8")

    def login(self):
        if self.command == "GET":
            return self.login_page()
        if self.headers.get("Origin") not in (None, "http://" + SITE):
            return self.reply(403, b'{"error":"invalid login origin"}')
        lengths = self.headers.get_all("Content-Length", [])
        length = lengths[0] if len(lengths) == 1 else ""
        if (not length.isdecimal() or len(length) > 4 or not 0 < int(length) <= 1024
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
            return self.reply(400, b'{"error":"invalid login"}')
        fields = parse_qs(self.rfile.read(int(length)).decode("ascii", errors="replace"))
        try:
            dest=return_path(fields.get("return_to",["/"])[0])
        except ValueError:
            dest="/"
        username = fields.get("username", [""])[0]
        account = self.check(username, fields.get("password", [""])[0])
        if not account or account.get("role") not in ("viewer", "operator"):
            return self.login_page(True,dest)
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with SESSION_LOCK:
            for key, record in list(SESSIONS.items()):
                if record[2] <= now:
                    del SESSIONS[key]
            if len(SESSIONS) >= 256:
                del SESSIONS[next(iter(SESSIONS))]
            SESSIONS[token] = (username, account["sha256"], now + 28800)
        self.send_response(303)
        self.send_header("Location", dest)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", "edge_plan_session=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800")
        self.send_header("Content-Length", "0")
        self.end_headers()
