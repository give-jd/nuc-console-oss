#!/usr/bin/env python3
"""nuc-console web view: the dashboard screen in a browser. READ-ONLY, opt-in, stdlib only.

Off unless `[web] enabled = yes` in config.ini. Runs as the unprivileged user, reads the same state as the tty
renderer and serves one HTML page (no JavaScript, except the one fixed script of the MAP's graph view, pinned by its hash
in the Content-Security-Policy). It has no write path: GET only, no forms, no API.
Binding to anything but loopback requires a token (fail closed). See docs/WEB.md.
"""
import base64
import hashlib
import hmac
import html
import http.server
import ipaddress
import math
import os
import re
import socket
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode, urlsplit

import graph
import graphjs
import graphlayout
import nuc_config
import render
from htmlview import CPU_CSS, CSS, GRAPH_CSS, MAP_CSS, fit_css, sgr_class, to_html  # noqa: F401 - to_html is part of this module's interface (tests, tools)

MIN_TOKEN = 16
TOKEN_OK = re.compile(r"[A-Za-z0-9._~-]{16,}")   # cookie- and URL-safe
MAX_CONN = 32        # simultaneous connections; more are dropped
DEADLINE_S = 15      # total time a single request may take (slowloris)
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
ZOOMS = (50, 67, 75, 90, 100, 110, 125, 150, 175, 200)  # text size steps (%), like a browser's: few values, bounded cache
CACHE_MAX = 64       # rendered pages kept: map URLs have unbounded combinations, the least recently used goes first
CACHE_CHARS = 32 * 2 ** 20  # and at most this much HTML: a map page that repeats many real row keys weighs megabytes
KEY = re.compile(r"[0-9a-f]{10}")  # a map row (graph.path_key): the only thing a map URL names
PID = re.compile(r"[0-9]{1,10}")   # a process (cpu view): ASCII digits only, at most MAX_PID
MAX_PID = 2 ** 32 - 1              # a Windows pid is 32 bits
NUM = re.compile(r"[0-9]{1,6}")    # a number in a URL: '²'.isdigit() is true but int('²') raises, so does int() of 5000 digits
MAX_KEYS = 300       # open= / shut= keys taken from a URL, each
MAP_ROWS = 1000      # rows on one map page: a bigger tree is unreadable in a browser anyway (close branches, problems only)
UNIVERSE = 3000      # rows of the fully open map, to drop keys that name nothing
STATE_CLASS = {"err": "r", "down": "r", "warn": "y", "unknown": "y", "ok": "g", "info": "n"}  # htmlview.PALETTE (n: plain text)
STATE_MARK = {"down": "✖ ", "unknown": "? "}  # a symbol besides the colour
EV_CLASS = {"seen": "w B", "declared": "n", "possible": "d", "bind": "d"}  # evidence: seen bright, declared plain, possible dim
EV_OF = {glyph: ev for (_, ev), glyph in graph.ARROW.items()}
LEVEL_CLASS = {"err": "r", "warn": "y", "ok": "g"}
# the MAP's graph view (?view=map&as=graph): the same graph as circles and lines
GRAPH_NODES = 400    # nodes drawn on one graph page: beyond, the most relevant ones, and a note says how to see the others
GZOOMS = (50, 67, 80, 100, 125, 150, 200, 250, 300)  # z=: the drawing's size in % of the window (no script needed)
ZONES = tuple(rid for rid, _, _, group in graph.ROOTS if group)  # INTERNET, LAN, TAILNET, LOCAL: the hubs (IMPACT etc. are views)
GBAD = ("err", "down", "warn", "unknown")  # needs attention, in the graph view (the colours: red, yellow)
KINDS = ("root", "port", "ct", "proc", "ext", "stack", "unit", "webapp")
STATES = ("ok", "warn", "err", "down", "unknown", "info")
ARROWS = ("seen", "declared", "possible")  # drawn with an arrowhead; 'reach' (zone -> port) and 'bind' (port -> owner) are structure
CAP_RANK = {"root": 2, "port": 3, "ct": 4, "proc": 5, "unit": 6, "webapp": 6, "stack": 7, "ext": 8}  # kept first when too many
LAYOUTS_MAX = 16     # layouts kept (they are pure: the same nodes and edges give the same positions, whatever graph they come from)
MARGIN = 70          # around the drawing: a label is centred under its circle
LABEL_MAX = 24       # characters of a label under its circle (the full name is in its tooltip and the details)
GRAPH_SCRIPT = graphjs.SCRIPT  # sent as is, never built from request data


def script_csp(script):
    """CSP that lets exactly this inline script run (by its SHA-256), and nothing else."""
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    return f"{CSP}; script-src 'sha256-{digest}'"


GRAPH_CSP = script_csp(GRAPH_SCRIPT) if GRAPH_SCRIPT else CSP  # computed once: the script is a constant


class Page(str):
    """A rendered page and the Content-Security-Policy it is served with (only the graph page carries a script)."""
    csp = CSP


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

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=(), csp=CSP):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Content-Security-Policy", csp), ("X-Frame-Options", "DENY")) + tuple(extra):
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
        page = srv.page(**view_params(q))
        self._send(200, page.encode(), "text/html; charset=utf-8", csp=getattr(page, "csp", CSP))

    def _no(self):
        self._send(405, b"read-only\n", extra=(("Allow", "GET"),))
    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _no


def view_params(q):
    """Query string -> page() arguments, clamped and rounded to a few distinct values (bounded cache and CPU).

    cols/rows: the layout grid (rows 0 = config); zoom: text size in % (0 = [display] zoom); fit=1: the text fills the window width (and the
    height, when rows is given), so a bigger zoom means fewer columns, re-laid out; full=1: every Details page;
    rotate=1: overview and Details pages take turns like on the console; kiosk=1: the footer says how to close the window;
    refresh: seconds between two reloads (1-10; default [dashboard] refresh_seconds).
    view=map: the MAP, whose state is all in the URL: open=/shut= the branches opened/closed by hand (row keys joined by '.'),
    all=1 everything open, sel= the row whose details are shown, only=1 problems only, pause=1 no reload.
    as=graph: the MAP drawn as a graph (anything else: the tree), with stacks=1 the compose projects, ext=0 no remote
    addresses, local=1|2 only the selected node and its neighbours within 1 or 2 hops, z= the drawing's size in % (GZOOMS).
    view=cpu: the CPU screen: sort=mem|time|pid|user (default cpu), sel= the pid whose details are shown (only digits; dropped when
    no such process)."""
    one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    num = lambda k: int(one(k)) if NUM.fullmatch(one(k)) else 0  # noqa: E731
    cols, rows, zoom = num("cols"), num("rows"), num("zoom")
    view = one("view") if one("view") in ("map", "cpu") else ""
    sel = one("sel") if view == "map" and KEY.fullmatch(one("sel")) else \
        str(int(one("sel"))) if view == "cpu" and PID.fullmatch(one("sel")) and int(one("sel")) <= MAX_PID else ""  # '007' is pid 7: one URL
    return {"cols": max(60, min(300, (cols + 10) // 20 * 20)) if cols else 0,
            "rows": max(20, min(120, (rows + 2) // 4 * 4)) if rows else 0,
            "zoom": min(ZOOMS, key=lambda z: abs(z - zoom)) if zoom else 0, "fit": one("fit") == "1", "full": one("full") == "1",
            "rotate": one("rotate") == "1", "kiosk": one("kiosk") == "1",
            "refresh": max(nuc_config.REFRESH_MIN, min(nuc_config.REFRESH_MAX, num("refresh"))) if num("refresh") else 0,  # 0 = config
            "view": view, "open": map_keys(one("open")), "shut": map_keys(one("shut")),
            "all": one("all") == "1", "sel": sel, "only": one("only") == "1", "pause": one("pause") == "1",
            "as": "graph" if one("as") == "graph" else "", "stacks": one("stacks") == "1",
            "ext": "0" if one("ext") == "0" else "",  # remote addresses are shown unless ext=0 (the only value written)
            "local": int(one("local")) if one("local") in ("1", "2") else 0,
            "z": gzoom(num("z")),
            "sort": one("sort") if view == "cpu" and one("sort") in render.CPU_SORTS[1:] else ""}


def gzoom(z):
    """z= -> the nearest step of GZOOMS; 100 (the default) and nothing are 0, so one view has one URL."""
    z = min(GZOOMS, key=lambda g: abs(g - z)) if z else 0
    return 0 if z == 100 else z


def map_mode(state):
    """The map parameters of one mode only: the tree's open/shut/all mean nothing to the graph, the graph's filters nothing to
    the tree (no URL or cache key carries them there), local nothing without a selected node."""
    s = dict(state)
    if s.get("as") == "graph":
        s.update(open=(), shut=(), all=False, z=gzoom(s.get("z") or 0))
        s["local"] = s.get("local") if s.get("local") in (1, 2) and s.get("sel") else 0
    else:
        s.update({"as": ""}, stacks=False, ext="", local=0, z=0)
    return s


def map_keys(raw):
    """'k1.k2...' -> the valid row keys, deduplicated and sorted (equal views share one URL and one cache entry), at most MAX_KEYS."""
    return tuple(sorted({k for k in raw.split(".") if KEY.fullmatch(k)})[:MAX_KEYS])


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not nuc_config.WINDOWS  # on Windows SO_REUSEADDR lets another program bind the same port

    def __init__(self, addr, cfg, token="", demo=False, zoom=100):
        self.cfg, self.token, self.zoom = cfg, token, zoom
        self.allowed = {"localhost", "127.0.0.1", "::1", addr[0].lower(), socket.gethostname().lower()} | set(cfg.get("allowed_hosts", []))
        self.slots = threading.BoundedSemaphore(MAX_CONN)
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        render.DEMO = demo
        self.smp = render.Sampler()
        self.cpu_feed = render.CpuFeed(settle=0.4)  # the CPU page's samplers (processes cost CPU): made at the first request, one for all viewers
        self.smp.sample()  # starts the background reads (sessions, disks): the first page must not say "unavailable"
        self.lock = threading.RLock()  # reentrant: a map page, built under it, takes the map's graph from the same cache
        self.cache = OrderedDict()  # view -> (time, page): a burst of requests renders once; at most CACHE_MAX views
        self.layouts = OrderedDict()  # (size, nodes, edges) -> positions of the graph view: at most LAYOUTS_MAX
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

    def cached(self, key, ttl, build):
        """The page of this view if rendered less than ttl seconds ago, else build() it (one render at a time)."""
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                self.cache.move_to_end(key)
                return hit[1]
            page = build()
            self.cache[key] = (time.time(), page)
            self.cache.move_to_end(key)
            while len(self.cache) > CACHE_MAX or (len(self.cache) > 1 and self.cache_chars() > CACHE_CHARS):
                self.cache.popitem(last=False)
            return page

    def cache_chars(self):
        """Characters of the cached pages (the cached map graph is not a page: it counts 0)."""
        return sum(len(v) for _, v in self.cache.values() if isinstance(v, (str, bytes)))

    def page(self, cols=0, full=False, zoom=0, rows=0, fit=False, rotate=False, kiosk=False, refresh=0, view="", **state):
        """zoom/refresh 0 = the configured ones: only what the viewer changed is written in the links.
        view='map': the MAP page, state = its open/shut/all/sel/only/pause parameters (see view_params)."""
        r = refresh or self.cfg["refresh_seconds"]
        here = {"cols": cols, "rows": rows, "zoom": zoom, "fit": fit, "full": full, "rotate": rotate, "kiosk": kiosk, "refresh": refresh}
        zoom = zoom or min(ZOOMS, key=lambda z: abs(z - self.zoom))
        self.cpu_feed.max_age = r  # processes are read at most once per refresh interval, however many viewers and pages ask
        if view == "cpu":
            key = ("cpu", zoom, r) + tuple(here.items()) + (state.get("sort", ""), state.get("sel", ""))
            return self.cached(key, r / 2, lambda: self.cpu_page(here, zoom, r, state.get("sort", "") or "cpu", state.get("sel", "")))
        if view == "map":
            state = map_mode(state)
            key = ("map", zoom, r) + tuple(here.items()) + tuple(sorted(state.items()))
            return self.cached(key, r / 2, lambda: self.map_page(here, state, zoom, r))
        return self.cached((cols, rows, zoom, fit, full, rotate, kiosk, r), r / 2, lambda: self.dashboard(here, zoom, r))

    def grid(self, here, zoom):
        """(columns, rows, CSS) of a screen page: the text fills the window (fit) or is a fixed grid whose font grows with the zoom."""
        cols, rows, fit = (here[k] for k in ("cols", "rows", "fit"))
        base_cols, base_rows = cols or self.cfg["columns"], rows or self.cfg["rows"]
        if fit:  # the text fills the window: a bigger zoom = fewer columns and rows, re-laid out like a smaller console
            gcols, grows = max(60, round(base_cols * 100 / zoom)), max(16, round(base_rows * 100 / zoom))
            return gcols, grows, fit_css(gcols, grows if rows else 0)
        return base_cols, base_rows, "body{font-size:%.1fpx}" % (14 * zoom / 100)  # like the browser's own zoom (Ctrl +/-)

    def dashboard(self, here, zoom, r):
        rows, fit, full, rotate, kiosk = (here[k] for k in ("rows", "fit", "full", "rotate", "kiosk"))
        gcols, grows, style = self.grid(here, zoom)
        try:
            if full:  # overview + every detail page: nothing hidden behind "… +N more"
                body = "</pre><hr><pre>".join(to_html(f) for f in render.render_screens(self.smp, gcols, grows, mode="overview", keys=False, page=True,
                                                                                      cpu_feed=self.cpu_feed))
            else:
                # all the columns ("wide" 200 = the 2-column layout); fit without rows = a normal browser window: the page
                # scrolls, so it shows every section in full at any text size (full screen fits instead, and rotates)
                screen, _ = render.render_screen(self.smp, gcols, grows, mode="overview", at=time.time() if rotate else None,
                                                 keys=False, page=True, scroll=fit and not rows, cpu_feed=self.cpu_feed)
                body = to_html(screen)
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = "render error (see the service log)"
        link = lambda text, **kw: f'<a href="{html.escape(page_url(here, **kw))}">{text}</a>'  # noqa: E731
        views = [link("compact", cols=100), link("wide", cols=200), link("overview", full=False) if full else link("full details", full=True)]
        for name in ("map", "cpu"):
            if render.CFG["features"].get(name, True):
                views.append(f'<a href="{html.escape(page_url(dict({"view": name}, **here)))}">{name}</a>')
        page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta http-equiv="refresh" content="{r}"><title>{html.escape(socket.gethostname())} · nuc-console</title>'
                f'<style>{CSS}{style}</style><pre>{body}</pre><footer>text {self.sizes(link, zoom)} · refresh every {self.every(link, r)} · '
                f'{" · ".join(views)} · read-only · {time.strftime("%H:%M:%S")}' + (f" · {html.escape(render.KIOSK_HINT)}" if kiosk else "")
                + '</footer></html>')
        return page

    @staticmethod
    def sizes(link, zoom):
        i = ZOOMS.index(zoom)
        return (link("A−", zoom=ZOOMS[i - 1]) if i else "A−") + f" {zoom}% " + (link("A+", zoom=ZOOMS[i + 1]) if i + 1 < len(ZOOMS) else "A+")

    @staticmethod
    def every(link, r):
        lo, hi = nuc_config.REFRESH_MIN, nuc_config.REFRESH_MAX  # − = more often, + = less often
        return (link("−", refresh=r - 1) if r > lo else "−") + f" {r}s " + (link("+", refresh=r + 1) if r < hi else "+")

    def cpu_page(self, here, zoom, r, sort, sel):
        """The CPU screen as a page: the console's own screen through the ANSI path, its process rows links (sel= shows that process's
        details), the sort as links in the bottom bar. The processes are read once per refresh interval, whoever asks (cpu_feed)."""
        esc = html.escape

        def doc(body, foot, style="", refresh=True):  # the host name after cpu_problems(): --demo names the host there
            return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    + (f'<meta http-equiv="refresh" content="{r}">' if refresh else "")
                    + f'<title>{esc(socket.gethostname())} · cpu · nuc-console</title><style>{CSS}{CPU_CSS}{style}</style>{body}'
                    f'<footer>{" · ".join(foot)}</footer></html>')
        dash = f'<a href="{esc(page_url(here))}">dashboard</a>'  # back to the normal view, same size and refresh
        if not render.CFG["features"].get("cpu", True):
            return doc("<p>CPU screen disabled in config.ini (<b>[features] cpu = no</b>)</p>", [dash, "read-only"], refresh=False)
        gcols, grows, style = self.grid(here, zoom)
        chere = {"view": "cpu", "sort": "" if sort == "cpu" else sort, "sel": sel}
        chere.update(here)
        try:
            d = self.cpu_feed.read()
            sel = sel if sel and int(sel) in {p["pid"] for p in d["procs"]["procs"]} else ""  # no such process (any more): dropped
            chere["sel"] = sel
            screen, rows = render.cpu_web(d, render.cpu_problems(self.smp), gcols, grows, sort, int(sel) if sel else None, here["fit"] and not here["rows"])
            lines = [to_html(x) for x in screen.split("\x1b[K\r\n")]
            for i, pid in rows:  # a process row is a link: its details, or (the one open) back to the table
                lines[i] = f'<a class="pr" title="details" href="{esc(page_url(chere, sel="" if str(pid) == sel else str(pid)))}">{lines[i]}</a>'
            body = f"<pre>{chr(10).join(lines)}</pre>"
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: cpu render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = "<pre>render error (see the service log)</pre>"
        link = lambda text, **kw: f'<a href="{esc(page_url(chere, **kw))}">{text}</a>'  # noqa: E731
        sorts = " ".join(f"<b>{name}</b>" if key == sort else link(name, sort="" if key == "cpu" else key)
                         for key, name in (("cpu", "cpu"), ("mem", "mem"), ("time", "time"), ("pid", "pid"), ("user", "user")))
        bar = [dash] + ([f'<a href="{esc(page_url(dict({"view": "map"}, **here)))}">map</a>'] if render.CFG["features"].get("map", True) else [])
        bar += ["sort " + sorts, link("close details", sel="") if sel else "a process: its details", "text " + self.sizes(link, zoom),
                "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(esc(render.KIOSK_HINT))
        return doc(body, bar, style)

    def map_graph(self, r):
        """render.map_graph() for as long as a page lives (r/2), shared by every map view: a new sel/open/shut walks the
        tree again, it does not rebuild the graph (bounded CPU)."""
        return self.cached(("map graph",), r / 2, lambda: render.map_graph(self.smp))

    def map_page(self, here, state, zoom, r):
        """The MAP as real HTML (rows of links, not a screen): the URL holds the whole view, so the reload keeps it."""
        def doc(body, foot, refresh=True):  # the host name after map_graph(): --demo names the host there
            return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    + (f'<meta http-equiv="refresh" content="{r}">' if refresh else "")
                    + f'<title>{html.escape(socket.gethostname())} · map · nuc-console</title><style>{CSS}{MAP_CSS}'
                    + "body{font-size:%.1fpx}" % (14 * zoom / 100) + f'</style>{body}<footer>{" · ".join(foot)}</footer></html>')
        dash = f'<a href="{html.escape(page_url(here))}">dashboard</a>'  # back to the normal view, same size and refresh
        if not render.CFG["features"].get("map", True):
            return doc("<p>map disabled in config.ini (<b>[features] map = no</b>)</p>", [dash, "read-only"], refresh=False)
        if state.get("as") == "graph":
            return self.graph_page(here, state, zoom, r, dash)
        pause, st, sel = state.get("pause", False), map_state(state), state.get("sel", "")
        found = {}
        try:
            G, pb = self.map_graph(r)
            prune(G, st)
            mhere = map_here(here, st, sel, pause)
            body = map_body(G, pb, st, sel, mhere, socket.gethostname(), found)
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: map render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            mhere, body, found = map_here(here, st, sel, pause), "<pre>render error (see the service log)</pre>", {}
        anchor = f"#r-{sel}" if sel else ""  # the footer's links keep the selected row in sight
        link = lambda text, **kw: f'<a href="{html.escape(page_url(mhere, **kw) + anchor)}">{text}</a>'  # noqa: E731
        bar = [dash, mode_switch(None, graph_url(found, st.only, pause, here)),
               link("expand all", **state_params(graph.State(all=True, only=st.only))),
               link("collapse all", **state_params(graph.State(only=st.only))),
               link("all paths", only=False) if st.only else link("problems only", only=True),
               "paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        return doc(body, bar, refresh=not pause)

    def graph_page(self, here, state, zoom, r, dash):
        """The MAP as a graph: circles (nodes) and lines (edges) in an SVG, every node a link that selects it, the details
        of the selected one beside it. It works without a script; with GRAPH_SCRIPT (pinned by its hash in this page's
        CSP, the only page that has one) nodes can be dragged and the drawing zoomed and panned, and the script reloads
        the page itself: the meta refresh is then for browsers without scripts only."""
        pause, only, stacks = state.get("pause", False), state.get("only", False), state.get("stacks", False)
        ext, z, sel, local = state.get("ext", ""), state.get("z", 0), state.get("sel", ""), state.get("local", 0)
        script = ""
        try:
            G, pb = self.map_graph(r)
            v = graph_select(G, sel, local, only, stacks, ext != "0")
            if v["sel"] is None:  # the URL names no node drawn here (it changed, or a hand-made URL): dropped
                sel, local = "", 0
            gh = graph_here(here, sel, local, only, stacks, ext, z, pause)
            ids = sorted(v["ids"])
            w, h = canvas(len(ids))
            pos = self.layout(ids, sorted({(e["src"], e["dst"]) for e in v["edges"]}), w, h)
            body = graph_body(G, pb, v, pos, w, h, gh, r, socket.gethostname())
            tree = tree_url(G, v["sel"], only, pause, here)
            script = GRAPH_SCRIPT
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: graph render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            gh = graph_here(here, "", 0, only, stacks, ext, z, pause)
            body, tree = "<pre>render error (see the service log)</pre>", page_url(map_here(here, graph.State(only=only), "", pause))
        link = lambda text, **kw: f'<a href="{html.escape(page_url(gh, **kw))}">{text}</a>'  # noqa: E731
        url = lambda **kw: page_url(gh, **kw)  # noqa: E731
        bar = [dash, mode_switch(tree, None), link("all nodes", only=False) if only else link("problems only", only=True),
               "remote addresses " + choice([("on", url(ext="") if ext else None), ("off", None if ext else url(ext="0"))]),
               "stacks " + choice([("on", None if stacks else url(stacks=True)), ("off", url(stacks=False) if stacks else None)]),
               "zoom " + zoom_steps(link, z), "paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        page = Page(graph_doc(r, zoom, z, body, bar, not pause, script))
        if script:
            page.csp = GRAPH_CSP
        return page

    def layout(self, ids, pairs, w, h):
        """graphlayout.layout() of these nodes and edges on a w x h drawing, computed once: it is pure (the same nodes and
        edges give the same positions), so the graph rebuilt at every refresh reuses it while its shape stays the same."""
        key = hashlib.sha1(repr((w, h, ids, pairs)).encode("utf-8", "replace")).hexdigest()
        with self.lock:
            pos = self.layouts.get(key)
            if pos is None:
                pos = self.layouts[key] = graphlayout.layout(ids, pairs, w, h, MARGIN)
            self.layouts.move_to_end(key)
            while len(self.layouts) > LAYOUTS_MAX:
                self.layouts.popitem(last=False)
            return pos


def map_state(state):
    """URL parameters -> graph.State. With everything open the hand-opened list means nothing: dropped (one URL per view)."""
    st = graph.State(open=state.get("open", ()), all=state.get("all", False), shut=state.get("shut", ()), only=state.get("only", False))
    if st.all:
        st.open = set()
    return st


def state_params(st):
    return {"open": ".".join(sorted(st.open)), "shut": ".".join(sorted(st.shut)), "all": st.all, "only": st.only}


def map_here(here, st, sel, pause):
    """The map view's URL parameters, map ones first, in a fixed order: the same view is always the same URL."""
    p = {"view": "map"}
    p.update(state_params(st), sel=sel, pause=pause)
    p.update(here)
    return p


def universe(G):
    """The rows of the fully open map (at most UNIVERSE), walked once per graph (the server keeps one for r/2)."""
    if "_web_universe" not in G:
        G["_web_universe"] = graph.rows(G, graph.State(all=True), limit=UNIVERSE)
    return G["_web_universe"]


def prune(G, st):
    """Keys that name no row of the map (it changed, or a hand-made URL) are dropped: every link repeats them.
    On a map too big to walk whole, the keys of the rows this view walks are kept too: no other key changes the page."""
    if not (st.open or st.shut):
        return
    every = universe(G)
    keys = {r["key"] for r in every if r["kids"]}
    if every.truncated:
        keys |= {r["key"] for r in graph.rows(G, st, limit=MAP_ROWS) if r["kids"]}
    st.open &= keys
    st.shut &= keys


def map_head(pb, host):
    """The MAP's header line: host, time, and the problems pill coloured as on the console."""
    text, code = render.status_pill(pb)
    return (f'<div class="hd {sgr_class(code)}"><span> {html.escape(host)} │ MAP │ {time.strftime("%H:%M:%S")}</span>'
            f'<span>{html.escape(text)} </span></div>')


def map_body(G, pb, st, sel, here, host, found=None):
    """Header, legend, notes, the tree (one row per line, every row a link) and the details of the selected row, as HTML.
    Everything from the graph is escaped: labels are container and process names. found: gets the selected row's node."""
    esc = html.escape
    out = [map_head(pb, host)]
    legend = []
    for item in graph.LEGEND.split("  "):
        glyph, _, word = item.partition(" ")
        legend.append(f'<span class="{EV_CLASS.get(EV_OF.get(glyph), "")}">{esc(glyph)}</span> {esc(word)}' if glyph in EV_OF else esc(item))
    n = graph.counts(G)
    out.append(f'<div class="lg">{n["nodes"]} nodes · {n["edges"]} links · '
               + (f'<span class="r">{n["problems"]} need attention</span>' if n["problems"] else "nothing needs attention")
               + f' │ {"  ".join(legend)}  <span class="r">✖ down</span>  <span class="y">? unknown</span>  ▸ ▾ open/close · a name: its details</div>')
    out += [f'<div class="nt">· {esc(x)}</div>' for x in G.get("notes") or []]
    rs = graph.rows(G, st, limit=MAP_ROWS)
    tree = [map_row(G, row, st, sel, here) for row in rs]
    if rs.truncated:
        tree.append(f'<div class="nt">… more than {MAP_ROWS} rows: close some branches, or show problems only</div>')
    if not rs:
        tree.append('<div class="nt">nothing to show yet' + (": see the notes above" if G.get("notes") else "") + '</div>')
    panel = ""
    if sel:
        i = graph.find(rs, sel)
        row = rs[i] if i is not None else None
        if row is None:  # its branch is closed: still the same row of the fully open map
            every = universe(G)
            j = graph.find(every, sel)
            row = every[j] if j is not None else None
        panel = details_panel(G, row["node"] if row else None, st, sel, here)
        if found is not None and row:
            found.update(node=row["node"], kind=G["nodes"].get(row["node"], {}).get("kind"))
    out.append(f'<main class="mp{" two" if panel else ""}"><div class="tree">{"".join(tree)}</div>{panel}</main>')
    return "".join(out)


def map_url(here, st, sel, anchor):
    return page_url(here, sel=sel, **state_params(st)) + (f"#r-{anchor}" if anchor else "")


def map_row(G, row, st, sel, here):
    """One row: tree glyphs, the toggle link (▸ opens, ▾ closes, ↻ already on this path), the arrow coloured by evidence,
    the name (a link that selects it) coloured by state, the port's owner in bold, the qualifier dim, the worst finding."""
    esc, k = html.escape, row["key"]
    p = graph.parts(G, row)
    if row["kids"] and not row["cycle"]:
        tip = "close" if row["open"] else f"open: {row['kids']} below"
        out = [f'<a class="tg" title="{tip}" href="{esc(map_url(here, st.toggled(row), sel, k))}">{esc(p["toggle"])}</a>']
    else:
        tip = "already shown above on this path" if row["cycle"] else "nothing below"
        out = [f'<span class="tg d" title="{tip}">{esc(p["toggle"])}</span>']
    if p["arrow"]:
        out.append(f'<span class="{EV_CLASS.get(p.get("ev"), "")}">{esc(p["arrow"])}</span> ')
    label = STATE_MARK.get(p["state"], "") + p["label"]
    out.append(f'<a class="lb {STATE_CLASS.get(p["state"], "n")}" href="{esc(map_url(here, st, k, k))}">{esc(label)}</a>')
    if p.get("port"):
        out.append(f' <span class="d">{esc(p["port"])}</span>')
    if p.get("owner"):
        out.append(f' <b class="{"y" if p["owner"] == "?" else "n"}">{esc(p["owner"])}</b>')
    if p.get("sub"):
        out.append(f'  <span class="d">{esc(p["sub"])}</span>')
    if p.get("note"):
        lv = next((lv for lv, t in G["nodes"][row["node"]].get("findings") or [] if t == p["note"]), "")  # else: when it was seen
        out.append(f'  <span class="{LEVEL_CLASS.get(lv, "d") if lv != "ok" else "d"}">{esc(p["note"])}</span>')
    if k == sel:
        out.append(' <a class="dl" href="#details">details ↓</a>')
    return (f'<div class="ro{" sel" if k == sel else ""}" id="r-{esc(k)}"><span class="tr">{esc(p["tree"])}</span>'
            f'<span class="bd">{"".join(out)}</span></div>')


def details_panel(G, nid, st, sel, here, close=None, tools=""):
    """Everything graph.details knows about the selected node, as a table; 'close' keeps the page on the row.
    The graph view gives its own close URL, and the links to the node's neighbourhood (tools: HTML)."""
    esc, trs = html.escape, []
    for i, (label, value, level) in enumerate(graph.details(G, nid)):
        top, indent = ' class="top"' if i == 0 else "", ' class="in"' if label.startswith("  ") else ""
        trs.append(f'<tr{top}><th{indent}>{esc(label.strip())}</th><td class="{LEVEL_CLASS.get(level, "")}">{esc(str(value))}</td></tr>')
    close = map_url(here, st, "", sel) if close is None else close
    return (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span>{tools}'
            f'<a href="{esc(close)}">close ✕</a></div><table>{"".join(trs)}</table></aside>')


# ---- the graph view of the MAP ------------------------------------------------------------------------------------------

def choice(opts):
    """A small segmented switch: [(text, URL)], the current option's URL None (highlighted, not a link)."""
    return ('<span class="mv">' + "".join(f"<b>{t}</b>" if u is None else f'<a href="{html.escape(u)}">{t}</a>' for t, u in opts)
            + "</span>")


def mode_switch(tree, graph_):
    """'tree | graph' on the MAP page: the URL of the other mode, None for the current one."""
    return choice([("tree", tree), ("graph", graph_)])


def zoom_steps(link, z):
    """'− 100% +' for the drawing's size (z=)."""
    cur = z or 100
    i = GZOOMS.index(cur)
    return ((link("−", z=gzoom(GZOOMS[i - 1])) if i else "−") + f" {cur}% "
            + (link("+", z=gzoom(GZOOMS[i + 1])) if i + 1 < len(GZOOMS) else "+"))


def graph_here(here, sel="", local=0, only=False, stacks=False, ext="", z=0, pause=False):
    """The graph view's URL parameters in a fixed order (one URL per view), sel last: a node's link is the view plus its key."""
    p = {"view": "map", "as": "graph", "only": only, "stacks": stacks, "ext": ext, "z": z, "pause": pause}
    p.update(here)
    p.update(local=local, sel=sel)
    return p


def graph_url(found, only, pause, here):
    """The graph view of a tree view: the selected row's node selected (a project turns the stacks on), same filter and pace."""
    nid, kind = found.get("node"), found.get("kind")
    sel = graph.path_key((nid,)) if nid and (kind != "root" or nid in ZONES) else ""
    return page_url(graph_here(here, sel, 0, only, bool(sel) and kind == "stack", "", 0, pause))


def tree_url(G, nid, only, pause, here):
    """The tree view of a graph view: the selected node's first row in the fully open tree, selected, its branch opened."""
    st = graph.State(only=only)
    every = universe(G) if nid is not None else []
    j = next((i for i, row in enumerate(every) if row["node"] == nid), None)
    if j is None:
        return page_url(map_here(here, st, "", pause))
    depth = every[j]["depth"]
    for row in reversed(every[:j]):  # its ancestors: the nearest row above it at each smaller depth (roots are open anyway)
        if depth == 0:
            break
        if row["depth"] < depth:
            depth = row["depth"]
            if depth:
                st.open.add(row["key"])
    key = every[j]["key"]
    return page_url(map_here(here, st, key, pause)) + "#r-" + key


def reachable(edges, start, back=False):
    """start and every node a path of these edges leads to from it (back: every node from which a path leads to it)."""
    nxt = {}
    for e in edges:
        a, b = (e["dst"], e["src"]) if back else (e["src"], e["dst"])
        nxt.setdefault(a, []).append(b)
    out, todo = set(start), list(start)
    while todo:
        for x in nxt.get(todo.pop(), ()):
            if x not in out:
                out.add(x)
                todo.append(x)
    return out


def problems(edges, ids, bad):
    """'Problems only': the nodes that need attention, and those on a path from a zone to one of them (as the tree's
    problems only: a client address connected to a front door is not on that path)."""
    return bad | (reachable(edges, [z for z in ZONES if z in ids]) & reachable(edges, bad, back=True))


def around(edges, start, hops):
    """start and the nodes at most `hops` edges away from it, whichever way the edges point (Obsidian's local graph)."""
    near = {}
    for e in edges:
        near.setdefault(e["src"], set()).add(e["dst"])
        near.setdefault(e["dst"], set()).add(e["src"])
    out, frontier = {start}, [start]
    for _ in range(hops):
        nxt = [y for x in frontier for y in sorted(near.get(x, ())) if y not in out]
        out.update(nxt)
        frontier = nxt
    return out


def graph_select(G, sel="", local=0, only=False, stacks=False, ext=True):
    """What the graph view draws: {ids, edges, keys {id: key}, sel (id or None), bad (ids), hidden (count)}.

    Every node but the IMPACT/STACKS/OUTBOUND views (their nodes are drawn anyway), compose projects with stacks, remote
    addresses unless ext is false; only: problems() (what needs attention and the paths from a zone to it); local=1|2 with
    a selected node: it and its neighbours within that many hops. At most GRAPH_NODES: the selected node, the problems and
    their paths, zones, ports, containers, processes, then remote addresses seen last. sel: the key of a node that passes
    the filters, else None. The edges are every edge of the graph between two drawn nodes."""
    nodes, keys, taken = G["nodes"], {}, set()
    for nid in sorted(nodes):
        kind = nodes[nid]["kind"]
        if (kind == "root" and nid not in ZONES) or (kind == "stack" and not stacks) or (kind == "ext" and not ext):
            continue
        k = graph.path_key((nid,))
        if k not in taken:  # two ids with one key (40 bits): the first one only, so that a key names one node
            taken.add(k)
            keys[nid] = k
    ids = set(keys)
    within = lambda ids: [e for e in G["edges"] if e["src"] in ids and e["dst"] in ids]  # noqa: E731
    edges = within(ids)
    bad = {x for x in ids if nodes[x]["state"] in GBAD}
    if only:
        ids = problems(edges, ids, bad)
        edges = within(ids)
    cur = next((x for x in sorted(ids) if keys[x] == sel), None) if sel else None
    if cur is not None and local in (1, 2):
        ids = around(edges, cur, local)
        edges = within(ids)
    hidden = max(0, len(ids) - GRAPH_NODES)
    if hidden:
        lead = problems(edges, ids, bad & ids)
        last = {}  # remote address -> when it was last seen connected
        for e in edges:
            t = G["now"] if e.get("n") else (e.get("last") or 0)
            for x in (e["src"], e["dst"]):
                if nodes[x]["kind"] == "ext":
                    last[x] = max(last.get(x, 0), t)
        ids = set(sorted(ids, key=lambda x: (x != cur, x not in lead, CAP_RANK.get(nodes[x]["kind"], 9), -last.get(x, 0), x))[:GRAPH_NODES])
        edges = within(ids)
    return {"ids": ids, "edges": edges, "keys": {x: keys[x] for x in ids}, "sel": cur, "bad": bad & ids, "hidden": hidden}


def canvas(n):
    """The drawing's size in SVG units for n nodes: 1000 x 700 up to 60, growing toward 2000 x 1400 at GRAPH_NODES."""
    w = 1000 if n <= 60 else min(2000, 1000 + (n - 60) * 1000 // (GRAPH_NODES - 60))
    w = int(round(w / 50.0)) * 50
    return w, int(w * 0.7)


def f1(v):
    """A coordinate: a plain decimal, at most one digit after the point."""
    s = "%.1f" % v
    return s[:-2] if s.endswith(".0") else s


def spot(p, w, h):
    """A layout position, kept inside the drawing (room for the label under the circle); the centre if it is not a number."""
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        x, y = w / 2.0, h / 2.0
    if not (math.isfinite(x) and math.isfinite(y)):
        x, y = w / 2.0, h / 2.0
    return min(max(x, 20.0), w - 20.0), min(max(y, 20.0), h - 34.0)


MARKERS = "".join(f'<marker id="ah-{ev}" class="mk {ev}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" '
                  f'markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0L10,5L0,10z"/></marker>' for ev in ARROWS)
ARROW_GAP = 4    # between an arrowhead's tip and the circle it points at (graphjs.SCRIPT keeps the same when a node moves)


def graph_svg(G, v, pos, w, h, z, gh):
    """The SVG of the drawing, per the DOM contract with graphjs.SCRIPT: edges first (endpoints on the circles' edges),
    then nodes; every node a link selecting it; sel / nb (its neighbours, its edges) / dim (the others) when one is
    selected. Every text from the graph is escaped (quotes too) and its control characters replaced."""
    nodes, keys, cur, bad, esc = G["nodes"], v["keys"], v["sel"], v["bad"], html.escape
    deg = {}
    for e in v["edges"]:
        for x in (e["src"], e["dst"]):
            deg[x] = deg.get(x, 0) + 1
    rad = {x: 18.0 if nodes[x]["kind"] == "root" else min(14.0, 6 + 1.6 * math.sqrt(deg.get(x, 0))) for x in v["ids"]}
    xy = {x: spot(pos.get(x), w, h) for x in v["ids"]}
    near = {e["dst"] if e["src"] == cur else e["src"] for e in v["edges"] if cur in (e["src"], e["dst"])} - {cur} if cur else set()
    lines = []
    for e in sorted(v["edges"], key=lambda e: (cur in (e["src"], e["dst"]), e["dst"] in bad, graph.EV_RANK.get(e["ev"], 0),
                                               keys[e["src"]], keys[e["dst"]])):
        a, b = e["src"], e["dst"]
        ev = "reach" if e["ev"] == "bind" and a in ZONES else e["ev"] if e["ev"] in ARROWS else "bind"
        (x1, y1), (x2, y2) = xy[a], xy[b]
        ra, rb, d = rad[a], rad[b] + (ARROW_GAP if ev in ARROWS else 0), math.hypot(x2 - x1, y2 - y1)
        if d > ra + rb + 1:  # else the circles touch: centre to centre, under them
            ux, uy = (x2 - x1) / d, (y2 - y1) / d
            x1, y1, x2, y2 = x1 + ux * ra, y1 + uy * ra, x2 - ux * rb, y2 - uy * rb
        cls = "e " + ev + (" bad" if (a in bad and b in bad) or (e["ev"] in ("seen", "declared") and b in bad) else "") \
            + (" nb" if cur in (a, b) else "")
        lines.append(f'<line class="{cls}" data-a="{keys[a]}" data-b="{keys[b]}" x1="{f1(x1)}" y1="{f1(y1)}" x2="{f1(x2)}" '
                     f'y2="{f1(y2)}"' + (f' marker-end="url(#ah-{ev})"' if ev in ARROWS else "") + "/>")
    texts = {}
    for x in v["ids"]:
        label = graph.safe(nodes[x]["label"])
        state = nodes[x]["state"] if nodes[x]["state"] in STATES else "unknown"
        texts[x] = STATE_MARK.get(state, "") + (label if len(label) <= LABEL_MAX else label[:LABEL_MAX - 1] + "…")
    spots = place_labels(sorted(v["ids"], key=lambda x: (nodes[x]["kind"] != "root", -deg.get(x, 0), keys[x])), xy, rad, texts)
    circles = []
    for x in sorted(v["ids"], key=lambda x: (x == cur, x in near, x in bad, nodes[x]["kind"] == "root", keys[x])):
        n, k, (cx, cy), r = nodes[x], keys[x], xy[x], rad[x]
        state = n["state"] if n["state"] in STATES else "unknown"
        kind = n["kind"] if n["kind"] in KINDS else "other"
        label, sub = graph.safe(n["label"]), graph.safe(n.get("sub") or "")
        lx, ly, anchor = spots[x]
        cls = f"n {state} k-{kind}" + (" sel" if x == cur else " nb" if x in near else " dim" if cur else "")
        circles.append(f'<a class="{cls}" id="n-{k}" data-k="{k}" href="{esc(page_url(gh, sel=k))}">'
                       f'<circle cx="{f1(cx)}" cy="{f1(cy)}" r="{f1(r)}"/><text class="lb{anchor}" x="{f1(lx)}" y="{f1(ly)}">'
                       f'{esc(texts[x])}</text><title>{esc(label + (" · " + sub if sub else ""))}</title></a>')
    zz = z or 100
    return (f'<svg id="gsvg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{round(w * zz / 100)}" '
            f'height="{round(h * zz / 100)}" data-w="{w}" data-h="{h}"><defs>{MARKERS}</defs><g id="gvp">'
            f'<g class="ge">{"".join(lines)}</g><g class="gn">{"".join(circles)}</g></g></svg>')


LABEL_CHAR_W, LABEL_H = 6.4, 12.0  # 11px sans-serif (GRAPH_CSS): average character width and line height, in SVG units


def place_labels(order, xy, rad, texts):
    """{id: (x, y, class suffix)}: each label under its circle (the default), else above, right or left of it, whichever
    first overlaps no circle and no label placed before it (order: the most important nodes first); the least bad when all
    do. Deterministic, so a refresh does not move labels around. The script keeps each label's offset from its circle."""
    boxes = [(cx - rad[x], cy - rad[x], cx + rad[x], cy + rad[x]) for x, (cx, cy) in xy.items()]
    out = []

    def overlap(b):
        return sum(max(0.0, min(b[2], o[2]) - max(b[0], o[0])) * max(0.0, min(b[3], o[3]) - max(b[1], o[1]))
                   for o in boxes + out)
    spots = {}
    for x in order:
        (cx, cy), r, w = xy[x], rad[x], len(texts[x]) * LABEL_CHAR_W
        cands = ((cx, cy + r + 12, "", (cx - w / 2, cy + r + 2, cx + w / 2, cy + r + 2 + LABEL_H)),      # below
                 (cx, cy - r - 5, "", (cx - w / 2, cy - r - 4 - LABEL_H, cx + w / 2, cy - r - 4)),        # above
                 (cx + r + 4, cy + 4, " ls", (cx + r + 4, cy - LABEL_H / 2, cx + r + 4 + w, cy + LABEL_H / 2)),  # right
                 (cx - r - 4, cy + 4, " le", (cx - r - 4 - w, cy - LABEL_H / 2, cx - r - 4, cy + LABEL_H / 2)))  # left
        own = (cx - r, cy - r, cx + r, cy + r)
        boxes.remove(own)  # a label never collides with its own circle
        best = min(cands, key=lambda c: (overlap(c[3]) > 0, overlap(c[3]), cands.index(c)))
        boxes.append(own)
        spots[x] = best[:3]
        out.append(best[3])
    return spots


def legend_line(ev, text):
    return (f'<svg class="lk" viewBox="0 0 30 10" width="30" height="10" aria-hidden="true"><line class="ll {ev}" x1="1" y1="5" '
            f'x2="{27 if ev in ARROWS else 29}" y2="5"' + (f' marker-end="url(#ah-{ev})"' if ev in ARROWS else "") + f'/></svg> {text}')


def legend_dot(state, text):
    return (f'<svg class="lk" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"><g class="ln {state}">'
            f'<circle cx="7" cy="7" r="5"/></g></svg> {text}')


GRAPH_LEGEND = ("  ".join([legend_line("seen", "seen"), legend_line("declared", "declared"), legend_line("possible", "same network"),
                           legend_line("reach", "zone → port → service"), legend_line("reach bad", "leads to a problem")])
                + " │ " + "  ".join([legend_dot("ok", "ok"), legend_dot("warn", "attention"), legend_dot("err", "failing"),
                                     legend_dot("down", "✖ down"), legend_dot("unknown", "? unknown"), legend_dot("info", "info"),
                                     legend_dot("info ext", "remote address")]))


def graph_body(G, pb, v, pos, w, h, gh, r, host):
    """Header, legend, notes, the drawing and the details of the selected node, as HTML."""
    esc, n = html.escape, graph.counts(G)
    out = [map_head(pb, host)]
    links = sum(e["ev"] in ARROWS for e in v["edges"])
    out.append(f'<div class="lg">{len(v["ids"])} nodes · {links} links · '
               + (f'<span class="r">{n["problems"]} need attention</span>' if n["problems"] else "nothing needs attention")
               + f' │ {GRAPH_LEGEND} · a node: its details' + (' <a class="dl" href="#details">details ↓</a>' if v["sel"] else "") + "</div>")
    out += [f'<div class="nt">· {esc(graph.safe(x))}</div>' for x in G.get("notes") or []]
    if v["hidden"]:
        out.append(f'<div class="nt">+{v["hidden"]} more not drawn: show problems only, or select a node and its local graph</div>')
    if not v["ids"]:
        out.append('<div class="nt">' + ("nothing needs attention" if gh["only"] else "nothing to show yet"
                                         + (": see the notes above" if G.get("notes") else "")) + "</div>")
    panel = ""
    if v["sel"]:
        local = gh["local"]
        tools = choice([("local 1", None if local == 1 else page_url(gh, local=1)), ("local 2", None if local == 2 else page_url(gh, local=2)),
                        ("whole graph", page_url(gh, local=0) if local else None)])
        panel = details_panel(G, v["sel"], None, gh["sel"], gh, close=page_url(gh, local=0, sel=""), tools=tools)
    paused = "1" if gh["pause"] else "0"
    out.append(f'<main class="mp{" two" if panel else ""}"><div class="gv" id="gv" data-refresh="{r}" data-paused="{paused}" '
               f'data-state="{esc(page_url(gh, sel=""))}">{graph_svg(G, v, pos, w, h, gh["z"], gh)}</div>{panel}</main>')
    return "".join(out)


def graph_doc(r, zoom, z, body, foot, refresh, script):
    """The graph page: with a script, the meta refresh only for browsers that run none (the script reloads the page itself);
    the drawing fills the column at z=100 (no scrolling), z% of it otherwise (the box scrolls)."""
    meta = f'<meta http-equiv="refresh" content="{r}">'
    size = f"#gsvg{{width:{z}%}}" if z else "#gsvg{width:100%;max-height:calc(100vh - 150px)}"
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            + ((f"<noscript>{meta}</noscript>" if script else meta) if refresh else "")
            + f'<title>{html.escape(socket.gethostname())} · map · nuc-console</title><style>{CSS}{MAP_CSS}{GRAPH_CSS}'
            + "body{font-size:%.1fpx}" % (14 * zoom / 100) + size + f'</style>{body}<footer>{" · ".join(foot)}</footer>'
            + (f"<script>{script}</script>" if script else "") + "</html>")


def page_url(params, **change):
    """The current view with some parameters changed (only the ones that differ from the defaults are written)."""
    p = dict(params, **change)
    return "/?" + urlencode([(k, "1" if v is True else v) for k, v in p.items() if v not in (0, False, None, "")])


def main(argv):
    if "--log" in argv[:-1]:  # Windows scheduled task: no journal
        nuc_config.log_to(argv[argv.index("--log") + 1])
    full_cfg = nuc_config.load()
    cfg = full_cfg["web"]
    demo = "--demo" in argv
    if "--enabled" in argv:  # used by install.sh
        return 0 if cfg["enabled"] else 1
    if "--local" in argv and not cfg["enabled"]:
        # macOS/Windows display: the dashboard for this machine's own browser, on loopback only, whatever [web] bind says
        cfg = dict(cfg, enabled=True, bind="127.0.0.1", token_file="")
    if not cfg["enabled"] and not demo:
        print("nuc-console web view is disabled ([web] enabled = no in config.ini)")
        return 0
    try:
        token = read_token(cfg["token_file"])
        check_bind(cfg["bind"], token)
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else cfg["port"]
        srv = Server((cfg["bind"], port), cfg, token, demo, zoom=full_cfg["display"]["zoom"])
        if demo and "--demo-os" in argv[:-1]:  # --demo as the macOS/Windows collector writes it (windows|darwin)
            render.DEMO_OS = argv[argv.index("--demo-os") + 1]
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
