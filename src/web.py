#!/usr/bin/env python3
"""nuc-console web view: the dashboard screen in a browser. READ-ONLY, opt-in, stdlib only.

Off unless `[web] enabled = yes` in config.ini. Runs as the unprivileged user, reads the same state as the tty
renderer and serves one HTML page (no JavaScript). It has no write path: GET only, no forms, no API.
Binding to anything but loopback requires a token (fail closed). See docs/WEB.md.
"""
import hmac
import html
import http.server
import ipaddress
import os
import re
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import nuc_config
import render
from htmlview import CSS, to_html  # noqa: F401 - to_html is part of this module's interface (tests, tools)

MIN_TOKEN = 16
TOKEN_OK = re.compile(r"[A-Za-z0-9._~-]{16,}")   # cookie- and URL-safe
MAX_CONN = 32        # simultaneous connections; more are dropped
DEADLINE_S = 15      # total time a single request may take (slowloris)
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def is_loopback(bind):
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def read_token(path):
    """Token from a file (never from config.ini, which is world-readable). Returns '' if no file is configured."""
    if not path:
        return ""
    with open(path) as f:
        st = os.fstat(f.fileno())
        if not nuc_config.WINDOWS:  # Windows has no mode bits: install-windows.ps1 protects the folder with an ACL instead
            if st.st_mode & 0o077:
                raise ValueError(f"{path} must not be readable by group/others (chmod 600)")
            if st.st_uid not in (0, os.geteuid()):
                raise ValueError(f"{path} must be owned by root or by the service user")
        tok = f.read().strip()
    if not TOKEN_OK.fullmatch(tok):
        raise ValueError(f"token in {path} must be {MIN_TOKEN}+ characters from A-Z a-z 0-9 . _ ~ -")
    return tok


def check_bind(bind, token):
    """Fail closed: a non-loopback listener without a token would publish the topology of the machine."""
    if not is_loopback(bind) and not token:
        raise ValueError(f"refusing to listen on {bind} without a token: set [web] token_file, or bind to 127.0.0.1 "
                         "and publish with `tailscale serve`")


def host_ok(host_header, allowed):
    """DNS-rebinding guard: only expected Host names are served (a rebinding page arrives with the attacker's name)."""
    h = (host_header or "").strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return h in allowed or h.endswith(".ts.net")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "nuc-console"
    sys_version = ""
    timeout = 10

    def handle(self):  # timeout above is per recv(): add a total deadline so a trickling client can't hold a thread
        t = threading.Timer(DEADLINE_S, self._kill)
        t.daemon = True
        t.start()
        try:
            super().handle()
        finally:
            t.cancel()

    def _kill(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def log_message(self, *a):  # no access log: URLs may carry a token
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Content-Security-Policy", CSP), ("X-Frame-Options", "DENY")) + tuple(extra):
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _token_from(self, query):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nuc_token":
                return v
        return (query.get("token") or [""])[0]

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        srv = self.server
        if u.path == "/healthz":
            return self._send(200, b"ok\n")
        if u.path != "/":
            return self._send(404, b"not found\n")
        if not srv.token and not host_ok(self.headers.get("Host"), srv.allowed):
            return self._send(421, b"misdirected request: add this name to [web] allowed_hosts\n")
        if srv.token:
            given = self._token_from(q)
            if not hmac.compare_digest(given.encode(), srv.token.encode()):
                return self._send(401, b"unauthorized\n", extra=(("WWW-Authenticate", 'Bearer realm="nuc-console"'),))
            if "token" in q:  # move the token out of the URL (history, logs, referrers) into a cookie
                return self._send(302, extra=(("Location", "/"), ("Set-Cookie",
                                  f"nuc_token={given}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")))
        try:
            cols = int((q.get("cols") or [0])[0])
        except ValueError:
            cols = 0
        cols = max(60, min(300, (cols + 10) // 20 * 20)) if cols else srv.cfg["columns"]  # few distinct sizes: bounded cache and CPU
        self._send(200, srv.page(cols, (q.get("full") or [""])[0] == "1").encode(), "text/html; charset=utf-8")

    def _no(self):
        self._send(405, b"read-only\n", extra=(("Allow", "GET"),))
    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _no


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not nuc_config.WINDOWS  # on Windows SO_REUSEADDR lets another program bind the same port

    def __init__(self, addr, cfg, token="", demo=False):
        self.cfg, self.token = cfg, token
        self.allowed = {"localhost", "127.0.0.1", "::1", addr[0].lower(), socket.gethostname().lower()} | set(cfg.get("allowed_hosts", []))
        self.slots = threading.BoundedSemaphore(MAX_CONN)
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        render.DEMO = demo
        self.smp = render.Sampler()
        self.lock = threading.Lock()
        self.cache = {}  # cols -> (time, page): a burst of requests renders once
        super().__init__(addr, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):  # too many connections: drop instead of queueing threads
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def page(self, cols, full=False):
        with self.lock:
            hit = self.cache.get((cols, full))
            if hit and time.time() - hit[0] < self.cfg["refresh_seconds"] / 2:
                return hit[1]
            try:
                if full:  # overview + every detail page: nothing hidden behind "… +N more"
                    body = "</pre><hr><pre>".join(to_html(f) for f in render.render_screens(self.smp, cols - 1, self.cfg["rows"], mode="overview"))
                else:
                    screen, _ = render.render_screen(self.smp, cols - 1, self.cfg["rows"], mode="overview")
                    body = to_html(screen)
            except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
                print("nuc-console web: render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
                body = "render error (see the service log)"
            r = self.cfg["refresh_seconds"]
            page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<meta http-equiv="refresh" content="{r}"><title>{html.escape(socket.gethostname())} · nuc-console</title>'
                    f'<style>{CSS}</style><pre>{body}</pre><footer>read-only · {time.strftime("%H:%M:%S")} · refreshes every {r}s · '
                    f'<a href="/?cols=100">compact</a> · <a href="/?cols=200">wide</a> · <a href="/?full=1">full details</a></footer></html>')
            self.cache[(cols, full)] = (time.time(), page)
            return page


def main(argv):
    if "--log" in argv[:-1]:  # Windows scheduled task: no journal
        nuc_config.log_to(argv[argv.index("--log") + 1])
    cfg = nuc_config.load()["web"]
    demo = "--demo" in argv
    if "--enabled" in argv:  # used by install.sh
        return 0 if cfg["enabled"] else 1
    if not cfg["enabled"] and not demo:
        print("nuc-console web view is disabled ([web] enabled = no in config.ini)")
        return 0
    try:
        token = read_token(cfg["token_file"])
        check_bind(cfg["bind"], token)
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else cfg["port"]
        srv = Server((cfg["bind"], port), cfg, token, demo)
    except (OSError, ValueError, IndexError) as e:
        print("nuc-console web:", e, file=sys.stderr)
        return 2
    print(f"nuc-console web view on http://{'[' + cfg['bind'] + ']' if ':' in cfg['bind'] else cfg['bind']}:{srv.server_address[1]} (read-only{', token required' if token else ''})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
