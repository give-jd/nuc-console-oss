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
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode, urlsplit

import graph
import nuc_config
import render
from htmlview import CPU_CSS, CSS, MAP_CSS, fit_css, sgr_class, to_html  # noqa: F401 - to_html is part of this module's interface (tests, tools)

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
        self._send(200, srv.page(**view_params(q)).encode(), "text/html; charset=utf-8")

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
            "sort": one("sort") if view == "cpu" and one("sort") in render.CPU_SORTS[1:] else ""}


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
        pause, st, sel = state.get("pause", False), map_state(state), state.get("sel", "")
        try:
            G, pb = self.map_graph(r)
            prune(G, st)
            mhere = map_here(here, st, sel, pause)
            body = map_body(G, pb, st, sel, mhere, socket.gethostname())
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: map render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            mhere, body = map_here(here, st, sel, pause), "<pre>render error (see the service log)</pre>"
        anchor = f"#r-{sel}" if sel else ""  # the footer's links keep the selected row in sight
        link = lambda text, **kw: f'<a href="{html.escape(page_url(mhere, **kw) + anchor)}">{text}</a>'  # noqa: E731
        bar = [dash, link("expand all", **state_params(graph.State(all=True, only=st.only))),
               link("collapse all", **state_params(graph.State(only=st.only))),
               link("all paths", only=False) if st.only else link("problems only", only=True),
               "paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        return doc(body, bar, refresh=not pause)


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


def map_body(G, pb, st, sel, here, host):
    """Header, legend, notes, the tree (one row per line, every row a link) and the details of the selected row, as HTML.
    Everything from the graph is escaped: labels are container and process names."""
    esc = html.escape
    text, code = render.status_pill(pb)
    out = [f'<div class="hd {sgr_class(code)}"><span> {esc(host)} │ MAP │ {time.strftime("%H:%M:%S")}</span><span>{esc(text)} </span></div>']
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


def details_panel(G, nid, st, sel, here):
    """Everything graph.details knows about the selected node, as a table; 'close' keeps the page on the row."""
    esc, trs = html.escape, []
    for i, (label, value, level) in enumerate(graph.details(G, nid)):
        top, indent = ' class="top"' if i == 0 else "", ' class="in"' if label.startswith("  ") else ""
        trs.append(f'<tr{top}><th{indent}>{esc(label.strip())}</th><td class="{LEVEL_CLASS.get(level, "")}">{esc(str(value))}</td></tr>')
    return (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span>'
            f'<a href="{esc(map_url(here, st, "", sel))}">close ✕</a></div><table>{"".join(trs)}</table></aside>')


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
