#!/usr/bin/env python3
"""nuc-console web view: the dashboard screen in a browser. Read-only, opt-in, stdlib only; the AI page has buttons.

Two interfaces: the classic one (the screen as text, the default) and the shell (`[ui] web = app` or `?app=1`: a top bar, key figures and cards, with
the Map, CPU, Health and AI pages inside the same frame; /s/app.<sha8>.css is its style sheet, `/?set=` writes the appearance cookie). Neither runs a
script of its own here; the shell's scripts are a later step.

Off unless `[web] enabled = yes` in config.ini. Runs as the unprivileged user, reads the same state as the tty
renderer and serves one HTML page (no JavaScript, except the one fixed script of the MAP's graph view, pinned by its hash
in the Content-Security-Policy). GET only, except the forms of the AI page (`/?view=ai`): POST /ai/<action>, form-encoded, answered
with a redirect (src/aiweb.py does the work in the background). Those forms are guarded: the same access as viewing (loopback, or the
token), a CSRF token, Origin/Referer/Sec-Fetch-Site checks, a 4 KB body, ids checked against the catalog, `[ai] web_actions = no` to lock.
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
import secrets
import signal
import socket
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode, urlsplit

import advisor
import aiweb
import aisetup
import ansi  # same directory: the console's drawing of a card, kept in the shell's cards for now
import cards
import exposure
import graph
import graphjs
import graphlayout
import htmlview
import nuc_config
import prefs
import render
import screens
import ui
import webcss
import webjs
from ui import dd, hclean, hnum
from htmlview import AI_CSS, CPU_CSS, CSS, GRAPH_CSS, HEALTH_CSS, MAP_CSS, fit_css, sgr_class, to_html  # noqa: F401 - to_html is part of this module's interface (tests, tools)

MIN_TOKEN = 16
TOKEN_OK = re.compile(r"[A-Za-z0-9._~-]{16,}")   # cookie- and URL-safe
MAX_CONN = 32        # simultaneous connections; more are dropped
DEADLINE_S = 15      # total time a single request may take (slowloris)
ASSET_FILES = {"%s.%s.css" % (name, sha[:8]): (body, ctype) for name, (body, ctype, sha) in webcss.ASSETS.items()}  # /s/<name>.<sha8>.<ext>
ASSET_CACHE = "private, max-age=31536000, immutable"  # the name carries the hash: a changed sheet is another URL
UI_COOKIE_AGE = 31536000  # the appearance cookie lives a year
AI_ACTIONS = ("on", "off", "use", "cancel", "delete", "delete-all", "ask", "advise")  # POST /ai/<action>
AI_CONFIRMS = ("on", "delete", "delete-all")  # the question a page asks before it does that (?view=ai&confirm=...)
POST_MAX = 4096      # bytes of a form: a question is 500 characters, everything else is an id
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
HEALTH_SEL_MAX = 200  # characters of a finding id taken from a URL (an id is "<rule>:<subject>", the subject is capped at 64)
HEALTH_COLS = 140     # the health page's default width in columns: wider than that scrolls sideways on a laptop (cols=200 asks for the 3-column layout)
PILL_CLASS = {"err": "r", "warn": "y", "info": "d"}  # htmlview.HEALTH_CSS
AI_SEL_MAX = screens.AI_ID_MAX + 1  # characters of a model id taken from a URL: one more than an id has, so that a longer text never equals one
# the MAP's graph view (?view=map&as=graph): the same graph as circles and lines
GRAPH_NODES = 400    # nodes drawn on one graph page: beyond, the most relevant ones, and a note says how to see the others
GZOOMS = webcss.GZOOMS  # z=: the drawing's size in % of the window (no script needed)
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


def page_csp(scripts=(), shell=False, forms=False):
    """The Content-Security-Policy of one page, composed from what the page carries: the one place a policy is written.
    scripts: the texts of the inline scripts the page has; script-src lists exactly their SHA-256 and nothing else, and a page without
    one has no script-src (default-src 'none' then forbids every script). connect-src 'self' only where a script talks to this server
    (refresh, preferences, layout editor); where the refresh script is, Trusted Types: its one policy, nuc-frag, is the only way to hand
    markup to the parser. shell: the page loads /s/app.<sha8>.css and has no inline style at all (no <style>, no style= attribute: style-src 'self'). forms: the page's forms post to this server (the AI page) and nowhere else."""
    scripts = [s for s in scripts if s]
    parts = ["default-src 'none'", "style-src " + ("'self'" if shell else "'unsafe-inline'"), "base-uri 'none'",
             "form-action " + ("'self'" if forms else "'none'"), "frame-ancestors 'none'"]
    if scripts:
        parts.append("script-src " + " ".join(dict.fromkeys(webjs.csp_source(s) for s in scripts)))
    if any(s in (webjs.REFRESH_JS, webjs.PREFS_JS, webjs.BUILDER_JS) for s in scripts):
        parts.append("connect-src 'self'")
    if webjs.REFRESH_JS in scripts:
        parts += ["require-trusted-types-for 'script'", "trusted-types nuc-frag"]
    return "; ".join(parts)


CSP = page_csp()  # the classic pages: no script, no connection, no form


class Page(str):
    """A rendered page and what it is served with: the Content-Security-Policy (the graph page carries a script, the AI page may post a
    form) and the Referrer-Policy (the AI page sends its own address to itself, so that the browser's Origin on a post is the real one)."""
    csp = CSP
    referrer = "no-referrer"
    blocks = None  # a shell page: its blocks (top bar, key figures, cards, the view), what ?frag=1 serves; a classic page has none


class View(object):
    """What a page gives the shell instead of a document: its body, its own controls (the toolbar above it), the URL parameters that make the view
    (the footer's links change one of them), whether it reloads (and how soon, when `wait` says), its script and a class for <html> (the style sheet sizes the page by it), and whether it
    has forms (the CSP then allows them to post here). grid: the body is the overview's cards; legacy: it is one of the pages made before the
    shell (their classes are styled in webcss.py); bar: a block above the grid (the layout editor's controls)."""
    __slots__ = ("body", "tools", "here", "live", "script", "forms", "wait", "cls", "grid", "legacy", "bar")

    def __init__(self, body, tools=(), here=None, live=True, script="", forms=False, wait=0, cls="", grid=False, legacy=True, bar=""):
        self.body, self.tools, self.here, self.live, self.script, self.forms = body, list(tools), here, live, script, forms
        self.wait, self.cls, self.grid, self.legacy, self.bar = wait, cls, grid, legacy, bar


TAB_TITLES = {name: title for name, _feature, title in ui.SCREENS}
THEMES = tuple((name, name.replace("-", " "), prefs.THEME_CODES[name]) for name in prefs.THEMES)  # (value, the word, the code of ?set=t<code>)
DENSITIES = tuple((name, name, prefs.DENSITY_CODES[name]) for name in prefs.DENSITIES)
DETAIL_K = {"wall": 1, "desk": -2, "compact": 0}  # the detail level a card is drawn at (render.page_overview's k: -2 is the richest)
CARD_COLS = {1: 42, 2: 90, 3: 138, 4: 186}  # the width in columns the console's text of a card is laid out for, by the card's width in the grid
WEB_COLS = 186  # what a native card is built for: the widest layout, no cut in a name; the style sheet shrinks it (priority columns, wrapping)
WALL_ROWS = 3  # the wall density (a screen seen from afar) shows this many rows of a list; the rest is a "+N more"


def wall_seconds(rotate):
    """Seconds between two one-screen scrolls of the shell's wall page: rotate=N of the URL, else [dashboard] rotate_seconds."""
    return rotate if isinstance(rotate, int) and not isinstance(rotate, bool) else int(render.CFG["rotate_seconds"])


def wall_trim(body):
    """The body of a native card for the wall: a table or a list of more than WALL_ROWS rows is cut there and says how many it left out (a
    More that follows it is added to, so a card never says it twice). The same list when nothing is cut."""
    out, i = [], 0
    while i < len(body):
        part, left = body[i], 0
        if isinstance(part, ui.Table) and len(part.rows) > WALL_ROWS:
            left = len(part.rows) - WALL_ROWS
            part = ui.Table(part.cols, part.rows[:WALL_ROWS], None if part.groups is None else [g for g in part.groups if g[1] < WALL_ROWS], part.head)
        elif isinstance(part, ui.Wrap) and len(part.items) > WALL_ROWS and not part.flat:
            left = len(part.items) - WALL_ROWS
            part = ui.Wrap(part.items[:WALL_ROWS], part.sep, part.max_lines, part.indent, part.lead)
        out.append(part)
        i += 1
        if left:
            nxt = body[i] if i < len(body) else None
            if isinstance(nxt, ui.More):
                out.append(ui.More(left + nxt.n, nxt.what, nxt.href))
                i += 1
            else:
                out.append(ui.More(left, "more"))
    return out if len(out) != len(body) or any(x is not y for x, y in zip(out, body)) else body


STATE_RANK = {"err": 0, "down": 0, "warn": 1, "unknown": 2, "ok": 3, "info": 4}  # by severity: what needs you first
KPI_CARD = {"problems": "attention", "internet": "exposure", "lan": "exposure", "beyond": "exposure", "db_lan": "exposure", "firewall": "firewall",
            "cpu": "system", "ram": "system", "temp": "system", "load": "system", "uptime": "system", "disk": "disks", "containers": "containers",
            "unhealthy": "containers", "failed_units": "boot", "ssh": "sessions", "tailnet": "tailscale", "rx": "network_traffic", "tx": "network_traffic"}
KPI_VIEW = {"health": "health", "ai": "ai"}  # the key figures that open a screen
STALE_PROBLEMS = ("collector-containers", "stale-containers", "collector-net", "stale-net", "collector-boot")  # a collector that is not running
SOURCE_WORDS = {"url": "from this URL (?ui=)", "browser": "from this browser", "config.ini": "from config.ini", "preset": "from the preset", "default": "default"}


def set_url(field, back):
    """/?set=<field>&back=<the view's query>: the link that stores one preference and comes back (Handler._set)."""
    return "/?" + urlencode([("set", field), ("back", back)])


def set_link(field, back, text, current, **data):
    """A preference as a link (the chosen one is the plain, current text); data: the data-* attributes that say what it switches to."""
    if current:
        return f'<span class="lnk" aria-current="true">{html.escape(text)}</span>'
    attrs = "".join(f' data-{k}="{html.escape(v)}"' for k, v in data.items())
    return f'<a class="lnk" data-set{attrs} href="{html.escape(set_url(field, back))}">{html.escape(text)}</a>'


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


def same_host(value, host):
    """An Origin or a Referer ('http://host:port[/path]'): is that the host:port the request was addressed to? 'null', another port, another
    name, a user name in front, anything that is not http(s): no."""
    try:
        u = urlsplit(value or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and u.netloc.lower() == (host or "").strip().lower()


class BadRequest(ValueError):
    """A form that is not one this page makes (an id the catalog has not, a number that is not one): 400."""


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

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=(), csp=CSP, referrer="no-referrer", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if code not in (204, 304):  # these have no body, and no length either
            self.send_header("Content-Length", str(len(body)))
        for k, v in (("Cache-Control", cache), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", referrer),
                     ("Content-Security-Policy", csp), ("X-Frame-Options", "DENY")) + tuple(extra):
            self.send_header(k, v)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")  # no other site can embed or read this
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.end_headers()
        if body and self.command != "HEAD":  # HEAD: the headers of the GET (Content-Length included), no body
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

    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _set(self, q):
        """/?set=<field>&back=<view>: one appearance preference into the nuc_ui cookie (validated by prefs.apply_set, nothing else is stored), then
        a redirect to the view `back` names, rebuilt from its validated parameters (never the text given: no open redirect). set=reset clears it."""
        field = (q.get("set") or [""])[0]
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):  # a link on another site does not change this one's look
            return self._send(403, b"a link of another site cannot change the preferences\n")
        whole = field.startswith(prefs.COOKIE_VERSION + ".") or field == prefs.COOKIE_VERSION  # PREFS_JS sends back the string it was given: all of it
        step = prefs.edit_parts(field) is not None  # one step of the layout editor (e<op><card>, ereset)
        if whole and (not prefs.parse_cookie(field) and field != prefs.COOKIE_VERSION):
            return self._send(400, b"not a preference\n")
        if not whole and not step and field != "reset" and ("." in field or not prefs.parse_cookie("1." + field)):
            return self._send(400, b"not a preference\n")
        current = self._cookie(prefs.COOKIE_NAME)
        if step:  # only the layout changes; the step is applied to the layout in force (cookie, else config.ini, else the preset)
            value = prefs.apply_edit(current, field, render.CFG.get("ui"), [c for c in prefs.CARDS if cards.enabled(c, render.CFG)])
        else:
            value = prefs.dump_cookie(prefs.parse_cookie(field)) if whole else prefs.apply_set(current, field)
        try:
            back = parse_qs((q.get("back") or [""])[0][:400], max_num_fields=40)
        except ValueError:
            back = {}
        keep = (f"{prefs.COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={UI_COOKIE_AGE}" if value != prefs.COOKIE_VERSION
                else f"{prefs.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")  # nothing left to remember: the cookie goes
        if (q.get("frag") or [""])[0] == "1":  # a script's request: no redirect, the page changes itself; X-Nuc-Prefs is what it keeps in the browser
            return self._send(204, extra=(("Set-Cookie", keep), ("X-Nuc-Prefs", value)))
        self._send(302, extra=(("Location", view_url(view_params(back))), ("Set-Cookie", keep)))

    def _asset(self, path):
        """/s/<name>.<sha8>.css: the shell's style sheet, immutable (an unknown name or hash: 404)."""
        hit = ASSET_FILES.get(path[3:])
        if hit is None:
            return self._send(404, b"not found\n")
        self._send(200, hit[0], hit[1], cache=ASSET_CACHE)

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        srv = self.server
        if u.path == "/healthz":
            return self._send(200, b"ok\n")
        if u.path.startswith("/ai/") and u.path[4:] in AI_ACTIONS:
            return self._send(405, b"post only\n", extra=(("Allow", "POST"),))
        if u.path != "/" and not u.path.startswith("/s/"):
            return self._send(404, b"not found\n")
        if not srv.token and not host_ok(self.headers.get("Host"), srv.allowed):
            return self._send(421, b"misdirected request: add this name to [web] allowed_hosts\n")
        if srv.token:
            given = self._token_from(q)
            if not hmac.compare_digest(given.encode(), srv.token.encode()):
                return self._send(401, b"unauthorized\n", extra=(("WWW-Authenticate", 'Bearer realm="nuc-console"'),))
            if "token" in q and u.path == "/":  # move the token out of the URL (history, logs, referrers) into a cookie; the view stays (validated parameters only)
                return self._send(302, extra=(("Location", view_url(view_params(q))), ("Set-Cookie",
                                  f"nuc_token={given}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")))
        if u.path != "/":
            return self._asset(u.path)
        if "set" in q:
            return self._set(q)
        page = srv.page(cookie=self._cookie(prefs.COOKIE_NAME), **view_params(q))
        if (q.get("frag") or [""])[0] == "1" and getattr(page, "blocks", None) is not None:  # a classic page has no blocks: it is served whole, as always
            return self._fragment(page.blocks)
        self._send(200, page.encode(), "text/html; charset=utf-8", csp=getattr(page, "csp", CSP), referrer=getattr(page, "referrer", "no-referrer"),
                   extra=(("Vary", "Cookie"),))

    def _fragment(self, blocks):
        """?frag=1 on a shell page: only its blocks (what the refresh script swaps in), with an ETag of them: the same tag in If-None-Match is a 304."""
        tag = '"%s"' % hashlib.sha256(blocks.encode("utf-8")).hexdigest()[:20]
        extra = (("ETag", tag), ("X-Nuc-Fragment", "1"), ("Vary", "Cookie"))
        sent = [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]
        if tag in sent or "*" in sent or "W/" + tag in sent:
            return self._send(304, extra=extra)
        self._send(200, blocks.encode(), "text/html; charset=utf-8", extra=extra)

    def do_POST(self):  # noqa: N802 - the forms of the AI page, and nothing else
        u = urlsplit(self.path)
        if not u.path.startswith("/ai/") or u.query:
            return self._no()
        act, srv = u.path[4:], self.server
        if act not in AI_ACTIONS:
            return self._send(404, b"not found\n")
        # the same access as viewing: no token = loopback and a known Host name; a token = that token (constant time)
        if not srv.token and not host_ok(self.headers.get("Host"), srv.allowed):
            return self._send(421, b"misdirected request: add this name to [web] allowed_hosts\n")
        if srv.token and not hmac.compare_digest(self._token_from({}).encode(), srv.token.encode()):
            return self._send(401, b"unauthorized\n", extra=(("WWW-Authenticate", 'Bearer realm="nuc-console"'),))
        if not render.CFG["features"].get("ai", True):
            return self._send(404, b"the AI screen is off ([features] ai = no)\n")
        if not advisor.web_actions_on(render.CFG):
            return self._send(403, b"locked by config.ini ([ai] web_actions = no)\n")
        # a browser says where a form came from: only this page, on this host and port (a page of another site, or of another port, is no one's click)
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return self._send(403, b"a form of another site is refused\n")
        for name in ("Origin", "Referer"):
            if self.headers.get(name) is not None and not same_host(self.headers.get(name), self.headers.get("Host")):
                return self._send(403, b"a form of another site is refused\n")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._send(415, b"a form is application/x-www-form-urlencoded\n")
        n = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or n is None or not NUM.fullmatch(n):
            return self._send(400, b"a form has a Content-Length\n")
        if int(n) > POST_MAX:
            return self._send(413, b"too big\n")
        body = self.rfile.read(int(n))
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (ValueError, UnicodeDecodeError):
            return self._send(400, b"not a form\n")
        if not hmac.compare_digest(((form.get("csrf") or [""])[0]).encode(), srv.csrf.encode()):
            return self._send(403, b"the form is not from this page (reload the page and try again)\n")
        try:
            where = srv.ai_action(act, form)
        except BadRequest as e:
            return self._send(400, ("%s\n" % e).encode())
        self._send(303, extra=(("Location", where),), referrer="same-origin")  # Post/Redirect/Get: a reload never posts again

    def _no(self):
        self._send(405, b"read-only\n", extra=(("Allow", "GET, HEAD"),))
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no
    do_HEAD = do_GET  # the same checks and headers as GET: _send writes no body for HEAD


def view_params(q):
    """Query string -> page() arguments, clamped and rounded to a few distinct values (bounded cache and CPU).

    cols/rows: the layout grid (rows 0 = config); zoom: text size in % (0 = [display] zoom); fit=1: the text fills the window width (and the
    height, when rows is given), so a bigger zoom means fewer columns, re-laid out; full=1: every Details page;
    rotate=1: overview and Details pages take turns like on the console; kiosk=1: the footer says how to close the window;
    rotate=N (3-600): the same, and on the shell's wall page (kiosk=1 with the wall density) the seconds between two one-screen scrolls
    (default [dashboard] rotate_seconds);
    refresh: seconds between two reloads (1-10; default [dashboard] refresh_seconds).
    view=map: the MAP, whose state is all in the URL: open=/shut= the branches opened/closed by hand (row keys joined by '.'),
    all=1 everything open, sel= the row whose details are shown, only=1 problems only, pause=1 no reload.
    as=graph: the MAP drawn as a graph (anything else: the tree), with stacks=1 the compose projects, ext=0 no remote
    addresses, local=1|2 only the selected node and its neighbours within 1 or 2 hops, z= the drawing's size in % (GZOOMS).
    view=cpu: the CPU screen: sort=mem|time|pid|user (default cpu), sel= the pid whose details are shown (only digits; dropped when
    no such process).
    view=health: the HEALTH page: period=1|7|30 (days, default 7), sel= the id of the finding whose details are shown (page() drops one the
    report does not have), pause=1 no reload.
    view=ai: the AI page: sel= the id of the model whose details are shown (page() drops one the catalog does not have), pause=1 no reload,
    confirm=on|delete|delete-all the question the page asks first (page() drops one that does not apply; on and delete are about sel).
    edit=1: the layout editor (the shell's overview in edit mode; with app=0 or another view it is dropped).
    The shell: app=1 (0: the classic page, whatever [ui] web says), ui=<the preferences string> for this URL only (prefs.parse_cookie: an invalid one
    is dropped), view=settings (the settings page), card=<id> (one card of the overview in full), pause=1 (the overview too)."""
    one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    num = lambda k: int(one(k)) if NUM.fullmatch(one(k)) else 0  # noqa: E731
    cols, rows, zoom = num("cols"), num("rows"), num("zoom")
    view = one("view") if one("view") in ("map", "cpu", "health", "ai", "settings") else ""
    edit = not view and one("edit") == "1" and one("app") != "0"  # the layout editor: the shell's overview in edit mode (the classic page has none)
    sel = one("sel") if view == "map" and KEY.fullmatch(one("sel")) else \
        one("sel")[:HEALTH_SEL_MAX] if view == "health" else one("sel")[:AI_SEL_MAX] if view == "ai" else \
        str(int(one("sel"))) if view == "cpu" and PID.fullmatch(one("sel")) and int(one("sel")) <= MAX_PID else ""  # '007' is pid 7: one URL
    return {"cols": max(60, min(300, (cols + 10) // 20 * 20)) if cols else 0,
            "rows": max(20, min(120, (rows + 2) // 4 * 4)) if rows else 0,
            "zoom": min(ZOOMS, key=lambda z: abs(z - zoom)) if zoom else 0, "fit": one("fit") == "1", "full": one("full") == "1",
            "rotate": True if one("rotate") == "1" else num("rotate") if 3 <= num("rotate") <= 600 else False,
            "kiosk": one("kiosk") == "1",
            "refresh": max(nuc_config.REFRESH_MIN, min(nuc_config.REFRESH_MAX, num("refresh"))) if num("refresh") else 0,  # 0 = config
            "app": one("app") if one("app") in ("0", "1") else "", "ui": ui_oneshot(one("ui")),
            "card": one("card") if not view and one("card") in prefs.CARDS and not edit else "", "edit": edit,
            "view": view, "open": map_keys(one("open")), "shut": map_keys(one("shut")),
            "all": one("all") == "1", "sel": sel, "only": one("only") == "1", "pause": one("pause") == "1",
            "as": "graph" if one("as") == "graph" else "", "stacks": one("stacks") == "1",
            "ext": "0" if one("ext") == "0" else "",  # remote addresses are shown unless ext=0 (the only value written)
            "local": int(one("local")) if one("local") in ("1", "2") else 0,
            "z": gzoom(num("z")),
            "sort": one("sort") if view == "cpu" and one("sort") in screens.CPU_SORTS[1:] else "",
            **({"period": {"1": 1, "7": 7, "30": 30}.get(one("period"), 0)} if view == "health" else {}),
            **({"confirm": one("confirm") if one("confirm") in AI_CONFIRMS else ""} if view == "ai" else {})}


HERE_KEYS = ("cols", "rows", "zoom", "fit", "full", "rotate", "kiosk", "refresh", "app", "ui")  # the size, refresh and interface parameters every view has
VIEW_KEYS = {"map": ("open", "shut", "all", "sel", "only", "pause", "as", "stacks", "ext", "local", "z"), "cpu": ("sort", "sel"),
             "health": ("period", "sel", "pause"), "ai": ("sel", "pause"), "": ("card", "edit")}  # and what each view reads besides (settings: nothing)


def ui_oneshot(raw):
    """?ui= -> the canonical preferences string it holds ('' when invalid or empty): it is part of the URL, so of the cache key."""
    got = prefs.parse_cookie(raw)
    return prefs.dump_cookie(got) if got else ""


def view_url(p):
    """The address of a view as view_params() read it, for the redirect that follows ?token=: only validated values of the parameters that
    view reads (an unknown one, `token` included, is not echoed), the defaults left out, like the links of the pages."""
    p = map_mode(p) if p["view"] == "map" else p
    p = {k: p[k] for k in ("view",) + HERE_KEYS + VIEW_KEYS.get(p["view"], ()) + (("pause",) if p["app"] == "1" and p["view"] == "" else ())}  # the shell's overview pauses too
    p.update({k: ".".join(p[k]) for k in ("open", "shut") if k in p})  # row keys: 'k1.k2', like the pages' own links
    return page_url(p).rstrip("?")  # nothing left: "/"


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
        self.csrf = secrets.token_urlsafe(24)  # in every form of the AI page; a page of another site cannot read it
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        render.DEMO = demo
        aiweb.configure(demo=demo)  # the engine of the AI page: the real one, or the demo's (simulated); render.ai_engine() tells it where the settings are
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

    def page(self, cols=0, full=False, zoom=0, rows=0, fit=False, rotate=False, kiosk=False, refresh=0, view="", app="", ui="", card="", edit=False, cookie="", **state):
        """zoom/refresh 0 = the configured ones: only what the viewer changed is written in the links.
        view='map': the MAP page, state = its open/shut/all/sel/only/pause parameters (see view_params).
        The shell (uses_shell) draws the same views in its own frame; cookie is the nuc_ui cookie the request came with (prefs.effective checks it)."""
        r = refresh or self.cfg["refresh_seconds"]
        here = {"cols": cols, "rows": rows, "zoom": zoom, "fit": fit, "full": full, "rotate": rotate, "kiosk": kiosk, "refresh": refresh, "app": app, "ui": ui}
        zoom = zoom or min(ZOOMS, key=lambda z: abs(z - self.zoom))
        self.cpu_feed.max_age = r  # processes are read at most once per refresh interval, however many viewers and pages ask
        shell = self.uses_shell(view, app, card, edit)
        pause = bool(state.get("pause")) and not edit  # the editor does not reload by itself: a page that moves under your hand is no editor
        if shell:
            eff, src = prefs.effective(render.CFG.get("ui"), cookie, "" if edit else ui)  # the editor edits the browser's layout, not a ?ui= one
            cookie = prefs.dump_cookie(prefs.parse_cookie(cookie))  # what the browser holds, as a valid string ('1' when nothing)
            norm = {"": {"pause": pause, "edit": edit}, "settings": {}}.get(view, {})
        else:
            norm = {}

        def serve(key, ttl, build):
            """The classic page build(False), or the shell's page of the same view (build(True) is its View); one render at a time, kept ttl seconds."""
            if not shell:
                return self.cached(key, ttl, lambda: build(False))
            skey = ("shell", view, card, zoom, r, prefs.dump_cookie(eff), cookie, prefs.custom_layout(src, eff["order"])) + tuple(here.items()) + tuple(sorted(norm.items()))
            return self.cached(skey, ttl, lambda: self.shell_render(view, build, here, zoom, r, eff, cookie, pause, card, edit,
                                                                     prefs.custom_layout(src, eff["order"])))
        if view == "cpu":
            norm = {"sort": state.get("sort", ""), "sel": state.get("sel", ""), "pause": pause}
            key = ("cpu", zoom, r) + tuple(here.items()) + (state.get("sort", ""), state.get("sel", ""))
            return serve(key, r / 2, lambda sh: self.cpu_page(here, zoom, r, state.get("sort", "") or "cpu", state.get("sel", ""), sh))
        if view == "map":
            state = map_mode(state)
            norm = dict(state)
            key = ("map", zoom, r) + tuple(here.items()) + tuple(sorted(state.items()))
            return serve(key, r / 2, lambda sh: self.map_page(here, state, zoom, r, sh))
        if view == "health":  # the period and the selected finding are checked here, so that the cache key holds only values that exist
            days = state.get("period") if state.get("period") in screens.HEALTH_DAYS else 7
            on = render.CFG["features"].get("health", True)
            ids = {f["id"] for f in screens.health_findings(render.health_data(days)["report"])} if on else set()  # one report a minute per period
            sel, pause = state.get("sel", "") if state.get("sel", "") in ids else "", bool(state.get("pause"))
            norm = {"days": days, "sel": sel, "pause": pause}
            key = ("health", zoom, r, days, sel, pause) + tuple(here.items())
            return serve(key, r / 2, lambda sh: self.health_page(here, days, sel, pause, zoom, r, sh))
        if view == "ai":  # the selected model, and the question asked first, are checked here too: the cache key holds only what exists
            rows = screens.ai_rows(render.ai_data()["cat"]) if render.CFG["features"].get("ai", True) else []  # one catalog per AI_TTL
            ids = {m["id"] for m in rows}
            sel, pause = state.get("sel", "") if state.get("sel", "") in ids else "", bool(state.get("pause"))
            confirm = state.get("confirm", "")
            if confirm == "delete" and not any(m["id"] == sel and m["installed"] for m in rows) or confirm == "on" and not sel \
                    or confirm == "delete-all" and not any(m["installed"] for m in rows):
                confirm = ""
            norm = {"sel": sel, "pause": pause, "confirm": confirm, "version": aiweb.version()}
            key = ("ai", zoom, r, sel, pause, confirm, aiweb.version()) + tuple(here.items())  # a job that ends, a server that starts: a new page
            return serve(key, min(r / 2, 1.0), lambda sh: self.ai_page(here, sel, pause, confirm, zoom, r, sh))  # a job's progress moves: never older than a second
        return serve((cols, rows, zoom, fit, full, rotate, kiosk, r), r / 2, lambda sh: self.dashboard(here, zoom, r))

    # ---- the shell: `[ui] web = app` or ?app=1 --------------------------------------------------------------------------------------

    def uses_shell(self, view, app, card, edit=False):
        """The shell is the page for ?app=1, for the settings, a single card and the layout editor (they exist only there), and for every page
        when [ui] web = app (?app=0 asks for the classic page anyway)."""
        return view == "settings" or bool(card) or edit or app == "1" or (app != "0" and (render.CFG.get("ui") or {}).get("web") == "app")

    def shell_frame(self, r):
        """The data of a shell page (cards.Ctx), read once per half refresh interval whatever the number of viewers."""
        return self.cached(("shell frame", r), r / 2, self.read_frame)

    def read_frame(self):
        st, sm = render.snapshot(0), render.host_sample(self.smp)
        if render.DEMO:
            render.demo_defaults()
        pb = render.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])
        feats, health, ai = render.CFG["features"], None, None
        try:
            health = render.health_data(7) if feats.get("health", True) else None
            ai = render.ai_status() if feats.get("ai", True) else None
        except Exception as e:  # noqa: BLE001 - a KPI whose source fails is '?', the page stays
            print("nuc-console web: shell data error:", repr(e)[:200], file=sys.stderr)
        return cards.Ctx(s=sm, cont=st["cont"], net=st["net"], boot=st["boot"], problems=pb, cfg=render.CFG, now=time.time(), baseline=st["baseline"],
                         new=exposure.new_ports(st["net"], st["cont"], st["baseline"]), health=health, ai=ai)

    def shell_render(self, view, build, here, zoom, r, eff, cookie, pause, card, edit=False, custom=False):
        """One page of the shell: the top bar and the key figures (blocks the page is refreshed by), the view, the footer and the help. `build(True)`
        gives the View of a Map, CPU, Health or AI page (the page's own builder, which then returns its body and its controls, not a document).
        edit: the overview as the layout editor (no refresh, its own script); custom: the layout in force is the reader's own (the cards keep their order)."""
        ctx = self.shell_frame(r)
        feats = lambda f: render.CFG["features"].get(f, True)  # noqa: E731
        visible = [c for c, _w in prefs.visible_cards(eff, [c for c in prefs.CARDS if cards.enabled(c, render.CFG)])]
        if view == "settings":
            v = self.settings_view(here, eff, cookie, r)
        elif view == "" and edit:
            v = self.edit_view(here, eff, ctx)
        elif view == "":
            v = self.overview_view(here, eff, ctx, r, pause, card, visible, custom)
        else:
            v = build(True)
        vhere = dict(v.here) if v.here is not None else dict({"view": view}, **here)
        pause = bool(vhere.get("pause", pause))
        if pause:
            vhere["pause"] = True
        host = socket.gethostname()
        link = lambda text, **kw: f'<a href="{html.escape(page_url(vhere, **kw))}">{text}</a>'  # noqa: E731
        back = urlsplit(page_url(vhere)).query
        setlink = lambda field, text, cur, **data: set_link(field, back, text, cur, **data)  # noqa: E731
        clock = time.strftime("%H:%M:%S")
        # the key figures, each a link to the card (or the view) it is about
        here_home = dict(here)
        tiles = []
        for k in cards.kpis(ctx, eff["kpis"]):
            if k.id in KPI_VIEW:
                href = page_url(dict(here_home, view=KPI_VIEW[k.id])) if feats(KPI_VIEW[k.id]) else ""
            elif KPI_CARD.get(k.id) in visible:
                href = ("" if view == "" and not card else page_url(here_home)) + "#c-" + KPI_CARD[k.id]
            else:
                href = ""
            tiles.append(htmlview.kpi_tile(k, href))
        stale = {pid for _s, pid in ctx.problem_ids()} & set(STALE_PROBLEMS)
        note = htmlview.banner("!", "some of the data is old or missing: a collector is not running?",
                               f'restart it: <code class="cmd">{html.escape(render.CMD.get("restart", ""))}</code>') if stale else ""
        top = htmlview.topbar(host, *self.shell_pill(ctx.problems), self.shell_tabs(ctx, here, view, r, feats), clock, "#help",
                              page_url(dict(here, view="settings")), view == "settings")
        kpis = "" if view == "settings" else htmlview.kpis_block(tiles, note)
        # the page
        r_live = int(v.wait or r) if v.live and not pause else 0  # the meta refresh (inside <noscript>: with the scripts, they poll)
        tools = "".join(f'<span class="tl">{t}</span>' for t in v.tools)
        wall = bool(here["kiosk"]) and eff["density"] == "wall" and view != "settings" and not edit  # a screen on a wall: it scrolls by itself
        attrs = f' data-rotate="{wall_seconds(here["rotate"])}"' if wall else ""
        if (v.live or pause) and view != "settings" and not v.script:  # a graph reloads itself with its own script; a paused page can be resumed by it
            attrs += f' data-refresh="{int(v.wait or r)}" data-frag="{html.escape(page_url(vhere, frag="1"))}"'
        if pause:
            attrs += ' data-paused="1"'
        if edit:
            attrs += " data-edit"
        if v.grid:
            main = v.bar + f'<main class="grid"{attrs}>{v.body}</main>'
            blocks = v.body
        else:
            inner = v.body.replace("<main ", "<div ").replace("</main>", "</div>") if v.legacy else v.body  # one <main> a page: the old bodies' became <div>
            inner = (f'<div class="toolbar">{tools}</div>' if tools else "") + inner
            if view != "settings" and not v.script:  # the view is one block of the page, so that the poll can replace it
                inner = htmlview.block("div", "__view", "vb", inner)
                blocks = inner
            else:
                blocks = ""
            main = f'<main class="view{" legacy" if v.legacy else ""}"{attrs}>{inner}</main>'
        groups = ["refresh every " + self.every(link, r) if view != "settings" and not edit and not wall else "",
                  f'<a class="lnk" data-pause data-key="Z" aria-pressed="{"true" if pause else "false"}" href="'
                  + html.escape(page_url(vhere, pause=not pause)) + f'">{"resume" if pause else "pause"}</a>' if view != "settings" and not edit else "",
                  link("Done", edit=False) if edit else link("Edit layout", edit=True) if view == "" and not card else "",
                  "text " + self.sizes(link, zoom),
                  '<span class="grp">theme: ' + " ".join(setlink("t" + code, label, eff["theme"] == name, theme=name) for name, label, code in THEMES) + "</span>",
                  '<span class="grp">density: ' + " ".join(setlink("d" + code, label, eff["density"] == name, density=name) for name, label, code in DENSITIES) + "</span>",
                  "read-only · AI actions" if render.CFG["features"].get("ai", True) and advisor.web_actions_on(render.CFG) else "read-only"]
        if wall:  # nobody clicks on a wall: no pause, no layout editor, no size, theme or density links; the way out is the keyboard
            groups = [f"refreshed every {r} s", "read-only"]
        if here["kiosk"]:
            groups.append(html.escape(render.KIOSK_HINT))
        scope = view if view in ("map", "cpu", "health", "ai") else "global" if view == "settings" else "overview"
        groups_help = (ui.help_rows("editor", feats) + ui.help_rows("global", feats, bool(nuc_config.PORTABLE))) if edit \
            else ui.help_rows(scope, feats, bool(nuc_config.PORTABLE), pause)
        helps = htmlview.help_dialog(groups_help)
        body = top + kpis + '<div id="stale" class="stale-banner" role="status" hidden></div>' + main + htmlview.foot([g for g in groups if g], clock) + helps
        name = "layout" if edit else {"": "overview", "settings": "settings"}.get(view, view)
        scripts = [webjs.KEYS_JS, webjs.PREFS_JS, webjs.BUILDER_JS] if edit else \
            [webjs.REFRESH_JS, webjs.KEYS_JS, webjs.PREFS_JS] + ([v.script] if v.script else [])
        page = Page(htmlview.shell_doc(f"{host} · {name} · nuc-console", webcss.asset_path("app"), body, eff["theme"], eff["density"], zoom,
                                       int(time.time() // 600) % 3, here["kiosk"], "url" if here["ui"] else "cookie" if cookie != prefs.COOKIE_VERSION else "config",
                                       cookie if not here["ui"] and cookie != prefs.COOKIE_VERSION else "", r_live, v.cls, scripts, pause))
        page.csp = page_csp(scripts, shell=True, forms=v.forms)
        page.referrer = "same-origin" if v.forms else "no-referrer"
        page.blocks = None if edit else top + kpis + blocks  # the editor has no refresh script: ?frag=1 gets the whole page, as a classic page does
        return page

    @staticmethod
    def shell_pill(pb):
        """(state class, text) of the status pill: always there, the symbol besides the colour."""
        text, _code = render.status_pill(pb or [])
        state = "ok" if not pb else "err" if any(sev >= 2 for sev, _ in pb) else "warn"
        return state, text

    def shell_tabs(self, ctx, here, view, r, feats):
        """The tabs: a link per screen with its key, a badge where there is something to say (the Map's problems, the Health findings, the AI)."""
        badges = {}
        try:
            if feats("map") and view != "settings":
                n = graph.counts(self.map_graph(r)[0])["problems"]
                badges["map"] = str(n) if n else ""
            rep = ((ctx.health or {}).get("report") or {}).get("findings") if feats("health") else None
            if isinstance(rep, list):
                err, warn = (sum(1 for f in rep if isinstance(f, dict) and f.get("level") == lv) for lv in ("err", "warn"))
                badges["health"] = " ".join(x for x in (f'<span class="s-err">✖{err}</span>' if err else "", f'<span class="s-warn">!{warn}</span>' if warn else "") if x)
            ai = ctx.ai if feats("ai") and isinstance(ctx.ai, dict) else {}
            if ai.get("enabled"):
                answer = (ai.get("probe") or {}).get("state") if isinstance(ai.get("probe"), dict) else None
                badges["ai"] = '<span class="s-ok">●</span> on' if answer == "answering" else '<span class="s-err">✖</span> down' if answer == "down" else '<span class="s-unknown">?</span>'
        except Exception as e:  # noqa: BLE001 - a badge is a nicety: the tab stays
            print("nuc-console web: badge error:", repr(e)[:200], file=sys.stderr)
        out = []
        for digit, name in ui.screen_keys(feats):
            href = page_url(dict(here, view=name)) if name != "overview" else page_url(here)
            out.append(htmlview.tab(digit, TAB_TITLES[name], href, view == ("" if name == "overview" else name), badges.get(name, "")))
        return out

    def overview_view(self, here, eff, ctx, r, pause, only, visible, custom=False):
        """The overview: the cards in the order the preferences give (by severity, or fixed), each the body the console draws for it (the
        components come later), in a grid. only: one card in full (?card=). custom: the layout is the reader's own (the editor's): the cards
        stay where they put them, whatever the order preference says."""
        k = DETAIL_K.get(eff["density"], -2)
        if only:
            order = [(only, 4)] if cards.enabled(only, render.CFG) else []
        else:
            order = prefs.visible_cards(eff, [c for c in prefs.CARDS if cards.enabled(c, render.CFG)])
        built = [self.shell_card(ctx, cid, w, k, here, bool(only)) for cid, w in order]
        if eff["order"] == "severity" and not only and not custom:
            built.sort(key=lambda x: STATE_RANK.get(x[0], 3))  # stable: equal states keep the layout's order
        body = "".join(x[1] for x in built)
        vhere = dict(here, pause=pause, card=only)
        if only:
            body = body or '<p class="sm">this card is not shown: its feature is off in config.ini ([features])</p>'
        return View(body, ['<a href="%s">&larr; overview</a>' % html.escape(page_url(here))] if only else [], vhere, True, grid=True, legacy=False)

    def edit_view(self, here, eff, ctx):
        """The layout editor: the overview's cards in the order of the layout (never by severity), each with its own buttons (earlier, later,
        narrower, wider, hide: plain links, `?set=e<step>`), then the hidden cards, each with a button to show it. Every control is a link:
        without JavaScript a click is a step on the server that comes back here; BUILDER_JS turns the same links into changes of the page."""
        k = DETAIL_K.get(eff["density"], -2)
        vhere = dict(here, edit=True)
        back = urlsplit(page_url(vhere)).query
        esc = html.escape
        lay = prefs.layout_of(eff, [c for c in prefs.CARDS if cards.enabled(c, render.CFG)])
        title = lambda cid: cards.CARDS[cid].title if cid in cards.CARDS else cid  # noqa: E731

        def controls(cid, hidden=False):
            def link(op, glyph, what, attr):
                return (f'<a class="eb" {attr} href="{esc(set_url(prefs.edit_field(op, cid), back))}" aria-label="{esc(what)}: {esc(title(cid))}" '
                        f'title="{esc(what)}">{glyph}</a>')
            show = (f'<a class="eb eb-hide" data-hide href="{esc(set_url(prefs.edit_field("w" if hidden else "h", cid), back))}" '
                    f'aria-label="{esc(title(cid))}: hide or show" title="Hide or show"><span class="t-hide">\u2715</span><span class="t-show">show</span></a>')
            return ('<div class="ectl" role="group" aria-label="Layout of ' + esc(title(cid)) + '"><span class="grip" data-drag aria-hidden="true" title="Drag to move">'
                    '\u283f</span>' + link("u", "\u2191", "Earlier", "data-earlier") + link("d", "\u2193", "Later", "data-later")
                    + link("s", "\u2212", "Narrower", "data-shrink") + '<span class="wv" aria-hidden="true"></span>' + link("g", "+", "Wider", "data-grow")
                    + show + '<span class="grip rz" data-size aria-hidden="true" title="Drag to resize">\u2194</span></div>')
        built = [self.shell_card(ctx, cid, w, k, here, False, controls(cid))[1] for cid, w in lay["layout"]]
        for cid in lay["hidden"]:
            if cards.enabled(cid, render.CFG):  # built like the others (showing it with the script needs its body); the style sheet hides the body
                built.append(self.shell_card(ctx, cid, lay.get("hidden_w", {}).get(cid, 1), k, here, False, controls(cid, True), True)[1])
        done, reset = page_url(here), set_url("ereset", back)
        bar = ('<section class="ebar" aria-label="Layout editor"><div class="eh"><h2>Edit layout</h2>'
               f'<a class="lnk" href="{esc(done)}">Done</a><a class="lnk" data-set href="{esc(reset)}">Reset layout</a></div>'
               '<p class="hintl">Each card has its own buttons: \u2191 \u2193 move it, \u2212 + make it narrower or wider (1 to 4 columns), '
               '\u2715 hides it, and a hidden card has a <b>show</b> button. With JavaScript you can also drag a card by its \u283f handle, drag its '
               'right edge to resize it, or focus a card and press Space, then the arrow keys, + and \u2212, x and Esc (<a href="#help">help</a>). '
               'Every change is saved at once.</p>'
               '<p class="hintl">While a layout is in force (yours or config.ini\'s) the cards stay in this order (the order is fixed), instead of moving by '
               'severity, unless the order is set to severity. <b>Reset layout</b> brings the preset\'s back.</p><p id="live" class="sr-only" role="status" aria-live="polite"></p></section>')
        return View("".join(built), [], vhere, False, grid=True, legacy=False, bar=bar)

    def shell_card(self, ctx, cid, size, k, here, full, tools="", off=False):
        """(state, the article) of one card: the lines the console has always drawn for it (cards.build), colours as spans. A card that fails is
        shown unknown, with the reason in the log. tools: the editor's buttons of the card (the body is then not clickable); off: the editor lists it as hidden."""
        try:
            width = WEB_COLS
            card = cards.build(cid, ctx, k if not full else -2, cards.Caps(width, bool(full)))
            if card.body and all(isinstance(x, ui.Raw) for x in card.body):  # not built of components yet: the console's text, colours as spans
                width = CARD_COLS.get(size, 104)
                card = cards.build(cid, ctx, k if not full else -2, cards.Caps(width, bool(full)))
                lines, cut = ansi.card_lines(card, width)
                note, inner = htmlview.ansi_card(lines)
            else:  # built of components: the web draws them itself, whatever the card is
                parts = wall_trim(card.body) if k == DETAIL_K["wall"] and not full else card.body
                note, inner, cut = "", "".join(htmlview.html(x) for x in parts), card.truncated or parts is not card.body
            wall = k == DETAIL_K["wall"]  # nobody can click on a wall: no link to the whole card
            more = f'<a href="{html.escape(page_url(dict(here, card=cid)))}">… the whole card</a>' if cut and not full and not wall else ""
            rows = 0 if tools or full else htmlview.est_rows(inner + (f"<p>{more}</p>" if more else ""), size, card.note or note, wall)  # the editor's cards and a card on its own do not pack
            return card.state, htmlview.card_article(cid, card.title, card.note or note, card.state, size, inner, more, tools, off, rows)
        except Exception as e:  # noqa: BLE001 - a broken card must not take the page down
            print("nuc-console web: card %s error: %r" % (cid, e), file=sys.stderr)
            entry = cards.CARDS.get(cid)
            return "unknown", htmlview.card_article(cid, entry.title if entry else cid, "", "unknown", size, '<p class="sm">this card could not be drawn (see the service log)</p>', "", tools, off)

    def settings_view(self, here, eff, cookie, r):
        """The settings page: the appearance (every choice a link: /?set=), what to export, and what this machine is (read-only)."""
        vhere = dict(here, view="settings")
        back = urlsplit(page_url(vhere)).query
        esc = html.escape
        href = lambda field: esc(set_url(field, back))  # noqa: E731
        _, source = prefs.effective(render.CFG.get("ui"), cookie if cookie != prefs.COOKIE_VERSION else "", here["ui"])
        where = lambda f: f'<span class="src sm">{esc(SOURCE_WORDS[source[f]])}</span>'  # noqa: E731

        def group(title, field, options, hint=""):
            opts = [(text, set_url(code, back), eff[field] == value, " data-set" + "".join(f' data-{k}="{esc(v)}"' for k, v in data.items()))
                    for text, code, value, data in options]
            return (f'<div class="fs"><span class="lab">{esc(title)} {where(field)}</span>' + htmlview.seg(title, opts)
                    + (f'<p class="hintl">{esc(hint)}</p>' if hint else "") + "</div>")
        theme = group("Theme", "theme", [(label.capitalize(), "t" + code, name, {"theme": name}) for name, label, code in THEMES])
        dens = group("Density", "density", [(label.capitalize(), "d" + code, name, {"density": name}) for name, label, code in DENSITIES],
                     "Wall hides the small print and cuts long lists to 3 lines; compact fits more.")
        preset = group("Preset", "preset", [(name.capitalize(), "p" + prefs.PRESET_CODES[name], name, {}) for name in prefs.PRESETS],
                       "A preset is a layout, a set of key figures and the cards it hides; choosing one drops your own layout and key figures.")
        order = group("Order", "order", [("By severity", "o" + prefs.ORDER_CODES["severity"], "severity", {}), ("Fixed", "o" + prefs.ORDER_CODES["fixed"], "fixed", {})],
                      "By severity: what needs you comes first. Fixed: the layout's order, nothing moves. A layout of your own (from this browser, "
                      "the link or config.ini) means fixed, unless you choose By severity here."
                      + (" You have a layout, so for now the cards stay in its order." if prefs.custom_layout(source, eff["order"]) else ""))
        start = group("Start view", "start_view", [(TAB_TITLES[n], "v" + prefs.VIEW_CODES[n], n, {}) for n in prefs.VIEWS])
        cur, boxes = list(eff["kpis"]), []
        for kid in prefs.KPI_IDS:
            label, on = esc(cards.KPI_LABELS.get(kid, kid)), kid in cur
            new = [x for x in cur if x != kid] if on else cur + [kid]
            ok = bool(new) and len(new) <= prefs.MAX_KPIS
            inner = f'<span class="box" aria-hidden="true">{"✔" if on else "+"}</span> {label}'
            link_ = (f'<a href="{href("k" + "_".join(prefs.KPI_CODES[x] for x in new))}" data-set>{inner}</a>' if ok
                     else f'<span aria-disabled="true">{inner}</span>')
            boxes.append(f'<li class="{"on" if on else "off"}">{link_}' + (f'<span class="o">{cur.index(kid) + 1}</span>' if on else "") + "</li>")
        kpis = (f'<div class="fs"><span class="lab">Key figures (at most {prefs.MAX_KPIS}, shown in the order chosen) {where("kpis")}</span>'
                f'<ul class="kchk">{"".join(boxes)}</ul></div>')
        edit_href = esc(page_url(dict(here, view="", edit=True)))
        layout = (f'<div class="fs"><span class="lab">Layout {where("layout")}</span><div><a class="lnk" href="{edit_href}">Edit layout</a></div>'
                  '<p class="hintl">Move, resize and hide the cards of the overview. It is kept in this browser; a preset or Reset layout brings the preset\'s back.</p></div>')
        export = prefs.export_ini(dict(eff, web="app"))
        appearance = (f'<section class="sec" aria-labelledby="sec-app"><h3 class="sech" id="sec-app">Appearance</h3>{theme}{dens}{preset}{layout}{order}{start}{kpis}'
                      f'<div class="fs expo"><span class="lab" id="exp-lab">Export</span><pre id="export" tabindex="0" aria-labelledby="exp-lab">{esc(export)}</pre>'
                      f'<button type="button" class="lnk" data-copy="#export" data-done="Copied" data-fail="Selected: press Ctrl+C">Copy</button>'
                      f'<code class="ck-v" id="cookie-v">{esc(cookie)}</code>'
                      f'<p class="hintl">Saved in this browser (the cookie above). To make it everyone\'s default, paste the block into config.ini.</p></div>'
                      f'<div class="fs"><a class="lnk" data-set href="{href("reset")}">Reset to the defaults</a></div></section>')
        return View(f'<div class="settings">{appearance}{self.about_html()}</div>', [], vhere, False, legacy=False)

    def about_html(self):
        """'About this machine': what the machine is and how it is set up, read-only (this page changes nothing on it)."""
        esc, cfg = html.escape, render.CFG
        code = lambda t: f'<code class="cmd">{esc(t)}</code>'  # noqa: E731
        portable = nuc_config.PORTABLE
        conf = os.environ.get("NUC_CONSOLE_CONFIG") or (os.path.join(os.path.abspath(portable), "config.ini") if portable else nuc_config.DEFAULT_PATH)
        upd = "bin/nuc-console-update" if portable else "nuc-console-update"
        rows = [("version", f"<b>nuc-console {esc(nuc_config.VERSION)}</b> <span class=\"sm\">to update, run {code(upd)} yourself: nothing here goes online</span>"),
                ("mode", ("portable run: config, state and baseline are in " + code(os.path.abspath(portable))) if portable
                 else "installed on this machine"),
                ("config", code(conf) + " " + ('<span class="s-err">unreadable: %s</span>' % esc(cfg["config_error"]) if cfg.get("config_error")
                                               else '<span class="s-ok">read, no errors</span>' if os.path.exists(conf)
                                               else '<span class="sm">no file: the defaults are in use</span>'))]
        bind = self.server_address[0]
        loop = is_loopback(bind)
        rows.append(("web access", ("loopback only (" + esc(bind) + ")" if loop else esc(bind)) + (", a token is required" if self.token else
                                                                                               ", no token" if loop else "")))
        d = cfg.get("display") or {}
        rows.append(("display", f'mode {esc(d.get("mode", "browser"))} · zoom {int(self.zoom)}%'))
        rows.append(("Telegram", esc(self.telegram_text())))
        locked = not advisor.web_actions_on(cfg)
        rows.append(("AI buttons", ('<span class="s-warn">locked by the admin</span> ([ai] web_actions = no): the AI page only shows' if locked else
                                    "allowed ([ai] web_actions = yes): the AI page can switch the AI on and off, ask and delete models")
                     if cfg["features"].get("ai", True) else "the AI screen is off ([features] ai = no)"))
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
        return (f'<section class="sec" aria-labelledby="sec-about"><h3 class="sech" id="sec-about">About this machine</h3>'
                f'<p class="sm">Read-only: this page changes nothing on the machine.</p><dl class="about">{dl}</dl></section>')

    @staticmethod
    def telegram_text():
        """The notifier as the settings say it: off, or what its status.json shows (render.telegram_state)."""
        if not render.CFG["telegram"]["enabled"]:
            return "off ([telegram] enabled = no)"
        if nuc_config.PORTABLE:
            return "on in config.ini, but a portable run sends no message: install nuc-console (docs/TELEGRAM.md)"
        state, why = render.telegram_state(time.time())
        return {"ok": "on, paired and sending", "unreadable": "on; this account cannot read its status",
                "unpaired": "on, but not paired: run nuc-console-telegram --setup", "down": "on, but the notifier is not running"}.get(
            state, "failing for " + why)

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
        for name in ("map", "cpu", "health", "ai"):
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

    def cpu_native(self, here, r, sort, sel):
        """The CPU screen of the shell: the screen's components (screens.cpu_view, the model the console draws too) as HTML, no <pre>: key
        figures, the CPUs, the temperatures and the process table, whose column heads are the sort links (data-key: the keymap's letters) and
        whose rows are links (sel= shows that process's details beside it, the selected row closes them)."""
        esc = html.escape
        chere = {"view": "cpu", "sort": "" if sort == "cpu" else sort, "sel": sel}
        chere.update(here)
        try:
            d = self.cpu_feed.read()
            sel = sel if sel and int(sel) in {p["pid"] for p in d["procs"]["procs"]} else ""  # no such process (any more): dropped
            chere["sel"] = sel
            links = screens.CpuLinks(lambda by: page_url(chere, sort="" if by == "cpu" else by), lambda pid: page_url(chere, sel="" if str(pid) == sel else str(pid)))
            sc = screens.cpu_view(d, render.cpu_ctx(False), screens.WEB_W, 10 ** 4, sort, int(sel) if sel else None, bool(sel), 0, links)
            body = '<div class="scr scr-cpu">' + "".join(htmlview.html(node) for node in sc.nodes) + "</div>"
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: cpu render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = '<p class="sm">render error (see the service log)</p>'
        tools = [f'<a href="{esc(page_url(chere, sel=""))}">close details</a>'] if sel else []  # no sel: the rows are links, nothing to say
        return View(body, tools, chere, True, legacy=False)

    def cpu_page(self, here, zoom, r, sort, sel, shell=False):
        """The CPU screen as a page: the console's own screen through the ANSI path, its process rows links (sel= shows that process's
        details), the sort as links in the bottom bar. The processes are read once per refresh interval, whoever asks (cpu_feed). In the
        shell (shell=True) it is built of components instead (cpu_native)."""
        esc = html.escape

        def doc(body, foot, style="", refresh=True, tools=(), vhere=None):  # the host name after cpu_problems(): --demo names the host there
            if shell:
                return View(body, tools, vhere, refresh)
            return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    + (f'<meta http-equiv="refresh" content="{r}">' if refresh else "")
                    + f'<title>{esc(socket.gethostname())} · cpu · nuc-console</title><style>{CSS}{CPU_CSS}{style}</style>{body}'
                    f'<footer>{" · ".join(foot)}</footer></html>')
        dash = f'<a href="{esc(page_url(here))}">dashboard</a>'  # back to the normal view, same size and refresh
        if not render.CFG["features"].get("cpu", True):
            return doc("<p>CPU screen disabled in config.ini (<b>[features] cpu = no</b>)</p>", [dash, "read-only"], refresh=False)
        if shell:
            return self.cpu_native(here, r, sort, sel)
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
        tools = ["sort " + sorts, link("close details", sel="") if sel else "a process: its details"]
        bar = [dash] + ([f'<a href="{esc(page_url(dict({"view": "map"}, **here)))}">map</a>'] if render.CFG["features"].get("map", True) else [])
        bar += tools + ["text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(esc(render.KIOSK_HINT))
        return doc(body, bar, style, tools=tools, vhere=chere)

    def map_graph(self, r):
        """render.map_graph() for as long as a page lives (r/2), shared by every map view: a new sel/open/shut walks the
        tree again, it does not rebuild the graph (bounded CPU)."""
        return self.cached(("map graph",), r / 2, lambda: render.map_graph(self.smp))

    def map_page(self, here, state, zoom, r, shell=False):
        """The MAP as real HTML (rows of links, not a screen): the URL holds the whole view, so the reload keeps it."""
        def doc(body, foot, refresh=True, tools=(), vhere=None):  # the host name after map_graph(): --demo names the host there
            if shell:
                return View(body, tools, vhere, refresh)
            return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    + (f'<meta http-equiv="refresh" content="{r}">' if refresh else "")
                    + f'<title>{html.escape(socket.gethostname())} · map · nuc-console</title><style>{CSS}{MAP_CSS}'
                    + "body{font-size:%.1fpx}" % (14 * zoom / 100) + f'</style>{body}<footer>{" · ".join(foot)}</footer></html>')
        dash = f'<a href="{html.escape(page_url(here))}">dashboard</a>'  # back to the normal view, same size and refresh
        if not render.CFG["features"].get("map", True):
            return doc("<p>map disabled in config.ini (<b>[features] map = no</b>)</p>", [dash, "read-only"], refresh=False)
        if state.get("as") == "graph":
            return self.graph_page(here, state, zoom, r, dash, shell)
        pause, st, sel = state.get("pause", False), map_state(state), state.get("sel", "")
        found = {}
        try:
            G, pb = self.map_graph(r)
            prune(G, st)
            mhere = map_here(here, st, sel, pause)
            if shell:  # the shell draws the screen itself: the components of screens.py, as HTML (the classic page below is unchanged)
                body = map_native(G, st, sel, mhere, here, pause, found)
                return View(body, [], mhere, not pause, legacy=False)
            body = map_body(G, pb, st, sel, mhere, socket.gethostname(), found)
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: map render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            mhere, body, found = map_here(here, st, sel, pause), "<pre>render error (see the service log)</pre>", {}
        anchor = f"#r-{sel}" if sel else ""  # the footer's links keep the selected row in sight
        link = lambda text, **kw: f'<a href="{html.escape(page_url(mhere, **kw) + anchor)}">{text}</a>'  # noqa: E731
        tools = [mode_switch(None, graph_url(found, st.only, pause, here)),
                 link("expand all", **state_params(graph.State(all=True, only=st.only))),
                 link("collapse all", **state_params(graph.State(only=st.only))),
                 link("all paths", only=False) if st.only else link("problems only", only=True)]
        bar = [dash] + tools + ["paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        return doc(body, bar, refresh=not pause, tools=tools, vhere=mhere)

    def graph_page(self, here, state, zoom, r, dash, shell=False):
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
        tools = [mode_switch(tree, None), link("all nodes", only=False) if only else link("problems only", only=True),
                 "remote addresses " + choice([("on", url(ext="") if ext else None), ("off", None if ext else url(ext="0"))]),
                 "stacks " + choice([("on", None if stacks else url(stacks=True)), ("off", url(stacks=False) if stacks else None)]),
                 "zoom " + zoom_steps(link, z)]
        bar = [dash] + tools + ["paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        if shell:  # the drawing's size is a class of <html> (gzNNN) the style sheet sizes: no inline style (View.cls)
            return View(body, tools, gh, not pause, script=script, cls=f"gz{z}" if z else "gzfit")
        page = Page(graph_doc(r, zoom, z, body, bar, not pause, script))
        if script:
            page.csp = page_csp([script])
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


    def health_page(self, here, days, sel, pause, zoom, r, shell=False):
        """The HEALTH page: the console screen's parts as HTML. The findings are links (sel=<id>) whose details show beside or under the
        list; the sections of tables are the console's, drawn at the page's width. The URL holds the whole view, so the reload keeps it."""
        def doc(body, foot, refresh=True, tools=(), vhere=None):
            if shell:
                return View(body, tools, vhere, refresh)
            return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    + (f'<meta http-equiv="refresh" content="{r}">' if refresh else "")
                    + f'<title>{html.escape(socket.gethostname())} · health · nuc-console</title><style>{CSS}{MAP_CSS}{HEALTH_CSS}'
                    + "body{font-size:%.1fpx}" % (14 * zoom / 100) + f'</style>{body}<footer>{" · ".join(foot)}</footer></html>')
        dash = f'<a href="{html.escape(page_url(here))}">dashboard</a>'
        if not render.CFG["features"].get("health", True):
            return doc("<p>health disabled in config.ini (<b>[features] health = no</b>)</p>", [dash, "read-only"], refresh=False)
        hhere = dict({"view": "health", "period": days if days != 7 else 0, "sel": sel, "pause": pause}, **here)
        if shell:  # the shell draws the screen itself: the components of screens.py, as HTML
            return View(health_native(self.smp, days, sel, hhere), [], hhere, not pause, legacy=False)
        cols, data = here["cols"] or min(self.cfg["columns"], HEALTH_COLS), {"report": None}
        try:
            data, pb = render.health_state(self.smp, days)
            body = health_body(data, pb, days, sel, hhere, cols, socket.gethostname())
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: health render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = "<pre>render error (see the service log)</pre>"
        anchor = f"#f-{sel_index(data, sel)}" if sel else ""  # the footer's links keep the selected finding in sight
        link = lambda text, **kw: f'<a href="{html.escape(page_url(hhere, **kw) + anchor)}">{text}</a>'  # noqa: E731
        periods = " ".join(f"<b>{lab}</b>" if d == days else link(lab, period=d if d != 7 else 0) for d, lab in ((1, "24h"), (7, "7d"), (30, "30d")))
        tools = ["period " + periods, link("compact", cols=100), link("wide", cols=200)]
        bar = [dash] + tools + ["paused " + link("live", pause=False) if pause else link("pause", pause=True),
               "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only", time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        return doc(body, bar, refresh=not pause, tools=tools, vhere=hhere)

    def ai_page(self, here, sel, pause, confirm, zoom, r, shell=False):
        """The AI page: the AI switch and what it is doing at the top, the chat under it (the model answers as soon as the server does), then what
        this machine can run: the models as a table (a button per model: use it), their details, the hardware and the status. Forms post to /ai/*
        (see do_POST); the page reloads by itself only while something runs, so that a question being typed is not lost. The URL holds the whole
        view, so a reload keeps it."""
        eng = render.ai_engine()
        snap = eng.snapshot()
        live = snap["busy"] or snap["locked"]   # idle with forms: no reload (a locked page has none: it reloads as the others do)

        def doc(body, foot, refresh=True, tools=(), vhere=None):
            if shell:  # the shell reloads (a job runs: soon) only while something runs; a page with forms is not reloaded under a question being typed
                return View(body, tools, vhere, bool(refresh and live), forms=not snap["locked"], wait=2 if snap["busy"] else 0)
            meta = f'<meta http-equiv="refresh" content="{2 if snap["busy"] else r}">' if refresh and live else ""
            page = Page('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        + meta + f'<title>{html.escape(socket.gethostname())} · ai · nuc-console</title><style>{CSS}{MAP_CSS}{HEALTH_CSS}{AI_CSS}'
                        + "body{font-size:%.1fpx}" % (14 * zoom / 100) + f'</style>{body}<footer>{" · ".join(foot)}</footer></html>')
            page.csp, page.referrer = (CSP, "no-referrer") if snap["locked"] else (page_csp(forms=True), "same-origin")
            return page
        dash = f'<a href="{html.escape(page_url(here))}">dashboard</a>'
        if not render.CFG["features"].get("ai", True):
            return doc("<p>AI screen disabled in config.ini (<b>[features] ai = no</b>)</p>", [dash, "read-only"], refresh=False)
        ahere = dict({"view": "ai", "sel": sel, "pause": pause}, **here)
        if shell:  # the shell draws the screen itself: the components of screens.py, as HTML (the forms are the ones of the classic page)
            return View(ai_native(self, ahere, sel, confirm, snap, eng), [], ahere, bool(not pause and live), forms=not snap["locked"], wait=2 if snap["busy"] else 0,
                        legacy=False)
        cols, rows = here["cols"] or min(self.cfg["columns"], HEALTH_COLS), []
        try:
            data, pb = render.ai_state(self.smp)
            rows = screens.ai_rows(data["cat"])
            ui = AiUi(snap, eng.choice() if not snap["locked"] else None, data["cat"], rows, sel, confirm, self.csrf, urlsplit(page_url(ahere)).query, ahere)
            body = ai_body(data, render.ai_status(), pb, rows, sel, ahere, cols, socket.gethostname(), ui)
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: ai render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = "<pre>render error (see the service log)</pre>"
        anchor = f"#m-{next((i for i, m in enumerate(rows) if m['id'] == sel), 0)}" if sel else ""  # the footer's links keep the selected model in sight
        link = lambda text, **kw: f'<a href="{html.escape(page_url(ahere, **kw) + anchor)}">{text}</a>'  # noqa: E731
        tools = [link("compact", cols=100), link("wide", cols=200)]
        if snap["locked"]:  # the page as it was: read-only, and it reloads like the others
            bar = [dash] + tools + ["paused " + link("live", pause=False) if pause else link("pause", pause=True),
                   "text " + self.sizes(link, zoom), "refresh every " + self.every(link, r), "read-only (locked by config.ini)", time.strftime("%H:%M:%S")]
        else:  # forms: no reload while idle, so that a question being typed is not lost; it reloads by itself while a job or an answer runs
            tools.append(link("reload"))
            bar = [dash, tools[0], tools[1], "text " + self.sizes(link, zoom),
                   "paused " + link("live", pause=False) if pause else "reloads by itself while something runs · " + tools[2], time.strftime("%H:%M:%S")]
        if here["kiosk"]:
            bar.append(html.escape(render.KIOSK_HINT))
        return doc(body, bar, refresh=not pause, tools=tools, vhere=ahere)

    def ai_action(self, act, form):
        """What a post of the AI page asked, done through the engine (in the background), and the address to go to next (Post/Redirect/Get).
        A model id is checked against the engine's catalog, a number against its list, a text is cleaned and cut by the engine; nothing of the
        request reaches a path or a command line."""
        eng = render.ai_engine()
        one = lambda k, n=200: ((form.get(k) or [""])[0])[:n]  # noqa: E731
        model, yes = one("model", 80), one("confirm", 8) == "yes"
        if model and model not in {m["id"] for m in eng.models}:
            raise BadRequest("unknown model")
        sel = confirm = anchor = ""
        if act == "on":  # the model chosen before; the recommended one only after the page has asked
            ch = eng.choice()
            if yes and model:
                eng.turn_on(model)
                sel = model
            elif ch["model"] is None and ch["recommended"]:
                confirm, sel = "on", ch["recommended"]
            else:
                eng.turn_on()
        elif act == "off":
            eng.turn_off()
        elif act == "cancel":
            eng.cancel()
        elif act in ("use", "delete"):
            if not model:
                raise BadRequest("a model is needed")
            sel = model
            if act == "use":
                eng.use_model(model)
            elif yes:
                eng.delete(model)
            else:
                confirm = "delete"
        elif act == "delete-all":
            if yes:
                eng.delete_all()
            else:
                confirm = "delete-all"
        elif act == "ask":
            eng.ask(one("q", 600))
            anchor = "#ask"
        elif act == "advise":
            if not NUM.fullmatch(one("days", 3)):
                raise BadRequest("a number of days is needed")
            eng.advise(int(one("days", 3)))
            anchor = "#ask"
        with self.lock:
            self.cache.clear()  # the page that comes next shows what this did
        back = view_params(parse_qs(one("back", 400)))
        here = {k: back[k] for k in HERE_KEYS}
        return page_url(dict({"view": "ai", "sel": sel or (back.get("sel") if back.get("view") == "ai" else ""), "confirm": confirm}, **here)) + anchor


def sel_index(data, sel):
    return next((i for i, f in enumerate(screens.health_findings(data["report"])) if f["id"] == sel), 0)


def health_extra_html(report):
    """The ADVICE block under the findings list: the advisor's CACHED answer (render.health_advice: never a generation on a request path),
    as the advisor's own escaped HTML, or "" when the advisor is off. The console calls render.health_extra_lines(report, w) at the same place."""
    on_, res = render.health_advice(report)
    if not on_:
        return ""
    try:
        import advisor
        return advisor.html(res) if res else ('<div class="advice"><p class="advice-head">ADVICE (AI) — none yet</p><p>'
                                              f'{html.escape(render.ADVICE_NONE)}</p></div>')
    except Exception:  # noqa: BLE001 - a broken advisor is no advice
        return ""


def health_advice_node(report):
    """The ADVICE block of the Health page as a ui.Advice (health_extra_html's twin: the advisor's CACHED answer, never a generation on a request
    path), or None when the advisor is off. A broken advisor is no advice."""
    on_, res = render.health_advice(report)
    if not on_:
        return None
    try:
        if not res:
            return ui.Advice("ADVICE (AI) \u2014 none yet", [[render.ADVICE_NONE]], kind="none")
        p = advisor.parts(res)
        return ui.Advice(p["head"], p["paras"], p["notes"], p["kind"]) if p else None
    except Exception:  # noqa: BLE001
        return None


def health_native(smp, days, sel, here):
    """The Health screen of the shell, drawn from components (screens.health_model): the period as links with their keys, the findings with their
    details, the ADVICE block, the sections of tables. here: the page's parameters; the links of the periods change only the period."""
    try:
        data, _pb = render.health_state(smp, days)
        fl = screens.health_findings(data["report"])
        hv = screens.HealthView(days)
        hv.cur = sel
        advice = health_advice_node(data["report"]) if data["report"] is not None else None
        href = lambda d: page_url(here, period=d if d != 7 else 0)  # noqa: E731
        nodes = screens.health_model(data, hv, fl, advice, href, time.time())
        return '<div class="hv">' + "".join(htmlview.html(n) for n in nodes) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: health render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the health screen could not be drawn (see the service log)</p>'


def ai_chat_nodes(snap):
    """The chat of the engine's snapshot as ui.Qa nodes, oldest first: the question, and the answer as a ui.Advice (advisor.parts: escaped by the renderer,
    cleaned and capped by the advisor) or, while the model is still writing it, the waiting line. A question that failed shows why."""
    out = []
    for e in snap["chat"]["history"]:
        p = advisor.parts(e["res"] if e["res"] else {"error": e["error"] or "no answer"})
        out.append(ui.Qa(e["kind"], e["q"], ui.Advice(p["head"], p["paras"], p["notes"], p["kind"]) if p else None))
    pend = snap["chat"]["pending"]
    return out + ([ui.Qa(pend["kind"], pend["q"], None, True)] if pend else [])


def ai_native(srv, ahere, sel, confirm, snap, eng):
    """The AI screen of the shell, drawn from components (screens.ai_model, the model the console draws from too): the switch and its progress, the
    chat, the hardware and the status, the models as a table with their buttons, the details, what can be deleted. Every button is a form that posts
    to /ai/* with the CSRF token and the page to come back to, as on the classic page; a locked page has none. ahere: the page's parameters."""
    try:
        data, _pb = render.ai_state(srv.smp)
        rows = screens.ai_rows(data["cat"])
        locked = snap["locked"]
        acts = None if locked else screens.AiActs(srv.csrf, urlsplit(page_url(ahere)).query)
        links = screens.AiLinks(lambda mid: page_url(ahere, sel="" if mid == sel else mid), page_url(ahere, sel=""), page_url(ahere))
        nodes = screens.ai_model(data, render.ai_status(), snap, None if locked else eng.choice(), rows, sel, "" if locked else confirm, acts, links,
                                 ai_chat_nodes(snap))
        return '<div class="scr av">' + "".join(htmlview.html(n) for n in nodes) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: ai render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the AI screen could not be drawn (see the service log)</p>'


class AiUi(object):
    """What the AI page needs to draw its forms: the engine's snapshot and choice (None: locked, no forms), the catalog, the rows, the selected model, the
    question asked first, the CSRF token, the view to come back to. Every form is a button that posts to /ai/<action>."""

    def __init__(self, snap, choice, cat, rows, sel, confirm, csrf, back, here):
        self.snap, self.ch, self.cat, self.rows, self.sel, self.confirm, self.csrf, self.back, self.here = snap, choice, cat or {}, rows, sel, confirm, csrf, back, here
        self.locked = snap["locked"]
        self.working = bool(snap["job"] and snap["job"]["state"] == "running")  # a job runs: its buttons wait (the engine would say "busy")

    def form(self, action, label, fields=(), cls="", title="", disabled=False):
        """One button: a form that posts to /ai/<action> with the CSRF token and the view to come back to (ids and numbers only in `fields`)."""
        if self.locked:
            return ""
        esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in (("csrf", self.csrf), ("back", self.back)) + tuple(fields))
        return (f'<form class="f" method="post" action="/ai/{action}">{hidden}<button class="bt {cls}" type="submit"'
                + (f' title="{esc(title)}"' if title else "") + (" disabled" if disabled else "") + f'>{html.escape(label)}</button></form>')

    def row(self, i):
        return next((r for r in self.rows if r["id"] == i), None)


def ai_use_cell(r, ui):
    """The button that does everything for a model (use this model), or the reason there is none."""
    esc = html.escape
    if not r["pinned"]:
        return '<span class="d">not pinned yet</span>'
    if r["verdict"] == "no":
        return f'<span class="d" title="{esc(r["why"], quote=True)}">too big</span>'
    if r["active"] and ui.snap["state"][0] in ("running", "on"):
        return '<span class="g">● in use</span>'
    return ui.form("use", "use this model", (("model", r["id"]),), "use", "download it if needed, start it, turn AI on", ui.working)


def ai_row_action(r, ui):
    """The last cell of a model's row."""
    return "" if ui is None or ui.locked else f'<td class="ac">{ai_use_cell(r, ui)}</td>'


def ai_row_html(r, i, sel, here, ui=None):
    """One model of the table: its marks, its name (a link: the details), the columns of the console, the pill with its symbol, and the button."""
    esc, cur = html.escape, r["id"] == sel
    label, _, cls, _ = screens.AI_VERDICT.get(r["verdict"], screens.AI_UNKNOWN)
    marks = "".join(f'<span class="{col}" title="{what}">{sym}</span>' if on else " "
                    for sym, col, what, on in (("★", "y", "recommended", r["rec"]), ("✓", "g", "installed", r["installed"]), ("●", "c", "active", r["active"])))
    on, no = ' class="sel"' if cur else "", ' class="no"' if r["verdict"] == "no" else ""  # the selected row, a model that is too big (dim)
    return (f'<tr{on} id="m-{i}"><td class="mk">{marks}</td>'
            f'<td{no}><a class="lb" href="{esc(page_url(here, sel="" if cur else r["id"]) + f"#m-{i}")}">{esc(r["name"])}</a></td>'
            f'<td class="pm">{esc(r["params"])}</td><td class="r sz">{esc(screens.ai_mb(r["size_mb"]))}</td><td class="r">{esc(screens.ai_mb(r["need_mb"]))}</td>'
            f'<td><span class="pl {cls}">{esc(label)}</span></td><td>{esc(screens.ai_tok(r["tok"]))}</td><td class="nn"><div>{esc(r["notes"])}</div></td>'
            + ai_row_action(r, ui) + "</tr>")


def ai_panel_html(r, here, i, windows, ui=None):
    """The details of a model: what the console's pane says, as a table (the commands selectable in one click), what can be done with it
    (use it; delete its files: asked about first), and a link that closes it."""
    esc = html.escape
    title, items = screens.ai_details(r, windows)
    trs = [f'<tr class="top"><th>model</th><td>{esc(title)}</td></tr>']
    for label, value, kind in items:
        cell = (f'<span class="pl {screens.AI_VERDICT[kind][2]}">{esc(value)}</span>' if kind in screens.AI_VERDICT else f'<code class="cmd">{esc(value)}</code>' if kind == "cmd"
                else f'<span class="{"y" if kind == "warn" else "d"}">{esc(value)}</span>' if kind in ("warn", "dim") else esc(value))
        trs.append(f"<tr><th>{esc(label)}</th><td>{cell}</td></tr>")
    if ui is not None and not ui.locked:
        acts = ai_use_cell(r, ui)
        if r["installed"]:
            acts += " " + ui.form("delete", "delete its files", (("model", r["id"]),), "del", "asks first", ui.working)
        if acts:
            trs.append(f"<tr><th>do</th><td>{acts}</td></tr>")
    return (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span><a href="{esc(page_url(here, sel="") + f"#m-{i}")}">close ✕</a></div>'
            f'<table>{"".join(trs)}</table></aside>')


def ai_control_html(ui):
    """The top of the page: is the AI on, what is it doing (a download with its progress, the model loading), the button (turn it on, turn it off, cancel),
    the folder the models go to, the answer to the last click, and the question a click asked first (confirm=)."""
    esc, snap, ch = html.escape, ui.snap, ui.ch
    state, text = snap["state"]
    pill, cls = {"off": ("OFF", "d"), "working": ("WORKING", "c"), "running": ("ON", "g"), "on": ("ON", "g"), "error": ("ERROR", "r")}[state]
    out = ['<section class="ctl" id="ctl"><div class="row">' + f'<span class="pl {cls} big">{pill}</span><span class="st">{esc(text)}</span>']
    if ui.locked:
        out.append('<span class="d">locked by config.ini ([ai] web_actions = no): this page only shows</span>')
    elif state == "working":
        out.append(ui.form("cancel", "Cancel", (), "stop", "stop it: what was fetched is kept"))
    elif snap["switch"]["by"] == "config":
        out.append('<span class="d">on by config.ini ([ai] enabled = yes): turn it off there</span>')
    elif snap["switch"]["on"]:
        out.append(ui.form("off", "Turn AI off", (), "off", "stop the model server and turn the advisor off"))
    else:
        out.append(ui.form("on", "Turn AI on", (), "on", "set up the model and start it"))
    out.append("</div>")
    job = snap["job"]
    if state == "working":
        out.append('<div class="row"><progress max="100" value="%d"></progress> %s</div>' % (job["pct"], "%d%%" % job["pct"]) if job["phase"] == "downloading" and job["total"]
                   else '<div class="row"><progress max="100"></progress></div>')
    if not ui.locked and state == "off" and ch:
        t = ui.row(ch["target"])
        name, size = esc(t["name"]) if t else "", ch["size"]
        todo = "installed here" if ch["installed"] else ("%s to download" % esc(screens.ai_mb((size or 0) / 2 ** 20)) if size else "not downloadable yet")
        if ch["model"] and t:
            out.append(f'<div class="d">turning it on uses <strong>{name}</strong> ({"chosen on this page" if ch["by"] == "page" else "[ai] model in config.ini"}; {todo})</div>')
        elif t:
            out.append(f'<div class="d">no model is chosen yet: turning it on asks about the recommended one, <strong>{name}</strong> ({todo})</div>')
        else:
            out.append('<div class="d">no model fits this machine comfortably: choose one from the list below</div>')
    cat = ui.cat
    if cat.get("dir"):
        out.append('<div class="row dir">models are downloaded to <code class="cmd">%s</code> · %s</div>'
                   % (esc(hclean(cat["dir"], 200)), esc(aisetup.space_text(cat.get("space")))))
    note = snap["notice"]
    if note:
        out.append('<div class="note %s">%s</div>' % ("ok" if note["ok"] else "bad", esc(note["text"])))
    if ui.confirm and not ui.locked:
        t = ui.row(ui.sel)
        if ui.confirm == "on" and t:
            what = "Turn AI on with <strong>%s</strong>? %s" % (esc(t["name"]), "It is installed here." if ch and ch["installed"] else
                   "It is not here yet: <strong>%s</strong> to download (the SHA-256 is checked), then the model server starts on this machine and the advisor is turned on."
                   % esc(screens.ai_mb(((ch or {}).get("size") or 0) / 2 ** 20)))
            yes = ui.form("on", "Yes, turn it on", (("model", ui.sel), ("confirm", "yes")), "on")
        elif ui.confirm == "delete" and t:
            what = "Delete the files of <strong>%s</strong> (%s)? You can download it again later." % (esc(t["name"]), esc(screens.ai_mb(t["size_mb"])))
            yes = ui.form("delete", "Yes, delete", (("model", ui.sel), ("confirm", "yes")), "off")
        elif ui.confirm == "delete-all":
            what = "Delete the runtime and every downloaded model (%s)? You can download them again later." % esc(aisetup.fmt_size((cat.get("space") or {}).get("used")) if (cat.get("space") or {}).get("used") else "nothing")
            yes = ui.form("delete-all", "Yes, delete everything", (("confirm", "yes"),), "off")
        else:
            what = yes = ""
        if what:
            out.append(f'<div class="cf" id="confirm"><span>{what}</span> {yes} <a class="bt" href="{esc(page_url(ui.here))}">No</a></div>')
    out.append("</section>")
    return "".join(out)


def ai_chat_html(ui):
    """The chat under the switch: the questions and answers of this process (newest last), the question box, and 'advice now'. The model's text goes
    through advisor.html() (escaped, cleaned, capped); a question is escaped here. The box works when the AI is on and its server is not still starting."""
    esc, snap = html.escape, ui.snap
    chat, ready = snap["chat"], snap["switch"]["on"] and snap["state"][0] != "working"
    out = ['<section class="chat" id="chat"><div class="hs">CHAT · ask the model about this machine (it reads this machine\'s history; AI, check before acting)</div>']
    for e in chat["history"]:
        res = advisor.html(e["res"]) if e["res"] else advisor.html({"error": e["error"] or "no answer"})
        out.append(f'<div class="qa"><p class="q"><strong>{"advice" if e["kind"] == "advise" else "you"}:</strong> {esc(e["q"])}</p>{res}</div>')
    if chat["pending"]:
        out.append(f'<div class="qa"><p class="q"><strong>{"advice" if chat["pending"]["kind"] == "advise" else "you"}:</strong> {esc(chat["pending"]["q"])}</p>'
                   '<p class="d">the model is writing the answer (a small model on a slow CPU may need a minute; this page reloads by itself)</p></div>')
    if not ui.locked:
        dis = "" if ready and not chat["busy"] else " disabled"
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v, quote=True)}">' for k, v in (("csrf", ui.csrf), ("back", ui.back)))
        out.append(f'<form class="f ask" id="ask" method="post" action="/ai/ask">{hidden}<input class="q" type="text" name="q" maxlength="500" size="60" '
                   f'placeholder="ask: why is the disk filling up?" autocomplete="off"{dis}> <button class="bt on" type="submit"{dis}>Ask</button></form>')
        out.append('<div class="adv">advice now: ' + " ".join(ui.form("advise", label, (("days", d),), "", "", not ready or bool(chat["busy"]))
                                                             for d, label in ((1, "last 24 h"), (7, "last 7 days"), (30, "last 30 days"))) + "</div>")
        if not ready:
            out.append('<p class="d">%s</p>' % ("the model is still starting: the box wakes up when it answers" if snap["state"][0] == "working" else "turn AI on to ask"))
    out.append("</section>")
    return "".join(out)


def ai_manage_html(ui):
    """The small 'manage' area at the bottom: the downloaded files, and the button that deletes all of them (asked about first)."""
    cat = ui.cat
    used = (cat.get("space") or {}).get("used")
    if ui.locked or not (used or any(r["installed"] for r in ui.rows)):
        return ""
    return ('<div class="mg">manage: %s the runtime and every model (%s)</div>'
            % (ui.form("delete-all", "delete everything", (), "del", "asks first", ui.working), html.escape(aisetup.fmt_size(used) if used else "nothing")))


def console_lines(nodes, w):
    """The console's lines of a list of components (the classic pages show them in a <pre>)."""
    return ansi.render(ui.Group(nodes), w)[0]


def console_line(node, w):
    """The first console line of a component."""
    return ansi.render(node, w)[0][0]


def ai_body(data, st, pb, rows, sel, here, cols, host, ui=None):
    """Header, title, the AI switch and the chat, HARDWARE, the models (every one a link; the selected one's details beside or under the table), the
    legend, STATUS and the manage area, as HTML. The lines are the console's (screens.ai_*, drawn by ansi), cleaned by hclean() and escaped by to_html()/html.escape().
    ui: the forms (AiUi); None draws the page without them."""
    esc, cat = html.escape, data["cat"]
    text, code = render.status_pill(pb)
    out = [f'<div class="hd {sgr_class(code)}"><span> {esc(host)} │ AI │ {time.strftime("%H:%M:%S")}</span><span>{esc(text)} </span></div>',
           f'<pre class="ht">{to_html(console_line(screens.ai_title(rows if cat is not None else None, cols), cols))}</pre>']
    if ui is not None:
        out += [ai_control_html(ui), ai_chat_html(ui)]
    if cat is None:  # the catalog could not be read
        return "".join(out) + f'<p class="hn {"r" if data.get("err") else ""}">{esc(hclean(data["msg"]))}</p>'
    ids, windows = {m["id"] for m in rows}, dd(cat.get("hw")).get("os") == "windows"
    out.append(f'<pre class="ht">{to_html(chr(10).join(console_lines(screens.ai_hw_nodes(cat.get("hw"), cols, 0), cols)))}</pre>')
    i = next((j for j, m in enumerate(rows) if m["id"] == sel), None)
    panel = ai_panel_html(rows[i], here, i, windows, ui) if i is not None else ""
    act = ui is not None and not ui.locked
    table = ('<div class="mw" id="models"><table class="mt"><thead><tr><th></th><th>model</th><th class="pm">params</th><th class="r sz">size</th><th class="r">needs</th><th>verdict</th><th>est tok/s</th>'
             '<th class="nn">notes</th>' + ("<th></th>" if act else "") + '</tr></thead><tbody>'
             + "".join(ai_row_html(m, j, sel, here, ui) for j, m in enumerate(rows)) + "</tbody></table></div>"
             if rows else '<div class="nt">the catalog lists no model</div>')
    out.append(f'<div class="hs">MODELS · best first</div><main class="mp{" two" if panel else ""}"><div class="tree">{table}'
               f'<pre class="ht">{to_html(console_line(screens.ai_legend(cols), cols))}</pre></div>{panel}</main>')
    out.append(f'<pre class="ht">{to_html(chr(10).join(console_lines(screens.ai_status_nodes(st, cat, ids, cols, 0), cols)))}</pre>')
    if ui is not None:
        out.append(ai_manage_html(ui))
    return "".join(out)


def health_body(data, pb, days, sel, here, cols, host):
    """Header, title, notes, the findings (every one a link; the selected one's details beside or under them) and the sections, as HTML.
    Everything from the report goes through hclean() (control, format and wide characters out) and html.escape()."""
    esc, R = html.escape, data["report"]
    text, code = render.status_pill(pb)
    out = [f'<div class="hd {sgr_class(code)}"><span> {esc(host)} │ HEALTH │ {time.strftime("%H:%M:%S")}</span><span>{esc(text)} </span></div>']
    fl = screens.health_findings(R)
    title = console_line(screens.health_title(R, days, fl, False), cols)
    if R is None:
        return "".join(out) + f'<pre class="ht">{to_html(title)}</pre><p class="hn {"r" if data.get("err") else ""}">{esc(hclean(data["msg"]))}</p>'
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str)]
    if not (R.get("coverage") or {}).get("since"):
        notes = [x for x in notes if x != "no history yet"]
    out.append(f'<pre class="ht">{to_html(title)}</pre>' + "".join(f'<div class="nt">· {esc(x)}</div>' for x in notes))
    if not hnum((R.get("coverage") or {}).get("hours")):
        return "".join(out) + f'<p class="hn">{esc("no data in this period" if (R.get("coverage") or {}).get("since") else screens.HEALTH_NONE)}</p>'
    rows = []
    for i, f in enumerate(fl):
        level, ttl, txt, _, _ = screens.health_details(f)
        label = screens.LEVEL_PILL[level][0].strip()
        cur = f["id"] == sel
        rows.append(f'<div class="ro{" sel" if cur else ""}" id="f-{i}"><span class="tr"><span class="pl {PILL_CLASS[level]}">{esc(label)}</span> </span>'
                    f'<span class="bd"><a class="lb {LEVEL_CLASS.get(level, "n")}" href="{esc(page_url(here, sel="" if cur else f["id"]) + f"#f-{i}")}">'
                    f'{esc(ttl)}</a>  <span class="d">{esc(txt)}</span>' + (' <a class="dl" href="#details">details ↓</a>' if cur else "") + '</span></div>')
    if not fl:
        rows.append(f'<div class="nt">{esc(screens.health_nothing(R))}</div>')
    panel = ""
    cur = next((f for f in fl if f["id"] == sel), None)
    if cur:
        level, ttl, txt, facts, fix = screens.health_details(cur)
        trs = [f'<tr class="top"><th>finding</th><td class="{LEVEL_CLASS.get(level, "")}">{esc(ttl)}</td></tr>', f"<tr><th>what</th><td>{esc(txt)}</td></tr>"]
        trs += ([f'<tr><th>facts</th><td></td></tr>'] if facts else []) + [f'<tr><th class="in">{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in facts]
        trs.append(f"<tr><th>fix</th><td>{esc(fix)}</td></tr>")
        panel = (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span><a href="{esc(page_url(here, sel="") + f"#f-{sel_index(data, sel)}")}">'
                 f'close ✕</a></div><table>{"".join(trs)}</table></aside>')
    out.append(f'<div class="hs">FINDINGS</div><main class="mp{" two" if panel else ""}"><div class="tree">{"".join(rows)}{health_extra_html(R)}</div>{panel}</main>')
    if R.get("coverage"):
        out.append(f'<pre class="ht">{to_html(chr(10).join(screens.health_tables_lines(R, cols, None, time.time())))}</pre>')
    return "".join(out)


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


def map_native(G, st, sel, here, base, pause, found=None):
    """The Map screen of the shell, drawn from components (screens.map_web, the model the console draws too): the title with its choices (expand
    all, collapse all, problems only: links with their keys, and the graph view), the tree as a list whose rows are links (a row selects it, its
    mark opens or closes the branch) and the details of the selected row beside it. here: the map's URL parameters, base: the page's own (the
    graph view keeps those). found: gets the selected row's node, as map_body does."""
    rs = graph.rows(G, st, limit=MAP_ROWS)
    row = None
    if sel:
        i = graph.find(rs, sel)
        row = rs[i] if i is not None else None
        if row is None:  # its branch is closed: still the same row of the fully open map
            every = universe(G)
            j = graph.find(every, sel)
            row = every[j] if j is not None else None
    nid = row["node"] if row else None
    mine = {} if found is None else found
    if row:
        mine.update(node=nid, kind=G["nodes"].get(nid, {}).get("kind"))
    state = lambda **kw: page_url(here, **kw)  # noqa: E731
    links = screens.MapLinks(
        lambda r: map_url(here, st, "" if r["key"] == sel else r["key"], ""),
        lambda r: map_url(here, st.toggled(r), sel, ""),
        map_url(here, st, "", ""),
        state(**state_params(graph.State(all=True, only=st.only))),
        state(**state_params(graph.State(only=st.only))),
        lambda only: state(only=only),
        graph_url(mine, st.only, pause, base))
    try:
        nodes = screens.map_web(G, rs, st, sel, nid, links, MAP_ROWS, rs.truncated)
        return '<div class="scr mapv">' + "".join(htmlview.html(n) for n in nodes) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: map render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the map could not be drawn (see the service log)</p>'


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
    if nuc_config.PORTABLE or ("--local" in argv and not cfg["enabled"]):
        # macOS/Windows display: the dashboard for this machine's own browser, on loopback only, whatever [web] bind says.
        # A portable run (run.sh / run.ps1, NUC_CONSOLE_HOME) is always like that: nothing listens beyond 127.0.0.1, no token
        cfg = dict(cfg, enabled=True, bind="127.0.0.1", token_file="")
    if not cfg["enabled"] and not demo:
        print("nuc-console web view is disabled ([web] enabled = no in config.ini)")
        return 0
    try:
        token = read_token(cfg["token_file"])
        check_bind(cfg["bind"], token)
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else cfg["port"]
        if demo:  # --demo-os windows|darwin: the demo as that OS's collector writes it; --demo-health little|none: the HEALTH page of a young history
            render.DEMO_OS = argv[argv.index("--demo-os") + 1] if "--demo-os" in argv[:-1] else None
            render.DEMO_HEALTH = argv[argv.index("--demo-health") + 1] if "--demo-health" in argv[:-1] else ""
        srv = Server((cfg["bind"], port), cfg, token, demo, zoom=full_cfg["display"]["zoom"])
        if demo and "--demo-os" in argv[:-1]:  # --demo as the macOS/Windows collector writes it (windows|darwin)
            render.DEMO_OS = argv[argv.index("--demo-os") + 1]
    except (OSError, ValueError, IndexError) as e:
        print("nuc-console web:", e, file=sys.stderr)
        return 2
    acts = render.CFG["features"].get("ai", True) and advisor.web_actions_on(render.CFG)
    print(f"nuc-console web view on http://{'[' + cfg['bind'] + ']' if ':' in cfg['bind'] else cfg['bind']}:{srv.server_address[1]} "
          f"({'read-only except the buttons of the AI page' if acts else 'read-only'}{', token required' if token else ''})", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # a stop is an exit: the model server this process started ends with it (aiweb.shutdown)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        aiweb.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
