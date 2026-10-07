#!/usr/bin/env python3
"""nuc-console web view: the dashboard screen in a browser. Read-only, opt-in, stdlib only; the AI page has buttons.

Two interfaces: the classic one (the screen as text, the default) and the shell (`[ui] web = app` or `?app=1`: a top bar, key figures and cards, with
the Map, CPU, Health and AI pages inside the same frame; /s/app.<sha8>.css is its style sheet, `/?set=` writes the appearance cookie). Neither runs a
script of its own here; the shell's scripts are a later step.

Off unless `[web] enabled = yes` in config.ini. Runs as the unprivileged user, reads the same state as the tty
renderer and serves one HTML page (no JavaScript, except the one fixed script of the MAP's graph view, pinned by its hash
in the Content-Security-Policy). GET only, except the forms of the AI page (`/?view=ai`): POST /ai/<action>, form-encoded, answered
with a redirect (src/aiweb.py does the work in the background), and those of the Telegram page (`/?view=telegram`): POST /telegram/<action>
(src/tgweb.py: pair, cancel, on, off, test). Those forms are guarded: the same access as viewing (loopback, or the token), a CSRF token,
Origin/Referer/Sec-Fetch-Site checks, a 4 KB body, ids checked against the catalog, `[ai] web_actions = no` / `[telegram] web_actions = no` to lock.
Binding to anything but loopback requires a token (fail closed). The data API (`/api/v1/...`, src/webapi.py) gives what the screens show as JSON,
and their changes as Server-Sent Events (`/api/v1/stream`), behind the same checks; it changes nothing. See docs/WEB.md.
"""
import base64
import hashlib
import html
import http.server
import json
import os
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
import appjs
import ansi  # same directory: the console's drawing of a card, kept in the shell's cards for now
import cards
import confedit
import exposure
import graph
import graphjs
import graphlayout
import htmlview
import nuc_config
import prefs
import render
import screens
import tgweb
import ui
import webapi
import webcss
import webjs
from webhttp import API_PATH, CSP, TG_ACTIONS, BadRequest, Handler, check_bind, is_loopback, page_csp, read_token
from webmap import (canvas, graph_body, graph_doc, graph_here, graph_select, graph_url, map_body, map_here, map_native, map_nodes, map_state,
                    mode_switch, prune, state_params, tree_url, zoom_steps)
from webpages import (AiUi, ai_asked, ai_body, ai_native, ai_nodes, ai_prompt_node, health_body, health_native, health_nodes, sel_index,
                      telegram_html)
from weburl import APP_PATH, APP_VIEWS, HERE_KEYS, NUM, VIEW_KEYS, ZOOMS, app_url, choice, map_mode, page_url, view_params
from htmlview import AI_CSS, CPU_CSS, CSS, HEALTH_CSS, MAP_CSS, fit_css, to_html  # noqa: F401 - to_html is part of this module's interface (tests, tools)

MAX_CONN = 32        # simultaneous connections; more are dropped
STREAMS_MAX = 8      # streams open at once (each holds a connection and a thread; the app's tabs hold one each): more get 503
STREAM_S = 300       # a stream ends after this long (the request deadline does not apply to it); the client reconnects by itself
KEEPALIVE_S = 15     # a comment line when nothing changed for this long: proxies and clients see the stream is alive
CACHE_MAX = 64       # rendered pages kept: map URLs have unbounded combinations, the least recently used goes first
CACHE_CHARS = 32 * 2 ** 20  # and at most this much HTML: a map page that repeats many real row keys weighs megabytes
HEALTH_COLS = 140     # the health page's default width in columns: wider than that scrolls sideways on a laptop (cols=200 asks for the 3-column layout)
LAYOUTS_MAX = 16     # layouts kept (they are pure: the same nodes and edges give the same positions, whatever graph they come from)
MARGIN = 70          # around the drawing: a label is centred under its circle
GRAPH_SCRIPT = graphjs.SCRIPT  # sent as is, never built from request data


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
FEATURE_WORDS = confedit.FEATURE_WORDS  # the settings page's [features] switches: (key, title, what it is)
CONFIG_NOTE_S = 300  # how long the settings page shows what its last save of config.ini did
SOURCE_WORDS = {"url": "from this URL (?ui=)", "browser": "from this browser", "config.ini": "from config.ini", "preset": "from the preset", "default": "default"}


def set_url(field, back):
    """/?set=<field>&back=<the view's query>: the link that stores one preference and comes back (Handler._set)."""
    return "/?" + urlencode([("set", field), ("back", back)])


def set_link(field, back, text, current, **data):
    """A preference as a link; data: the data-* attributes that say what it switches to. The chosen one is a link too, marked aria-current
    (drawn as plain text): PREFS_JS switches the theme and the density in place and moves the mark, so every choice must stay clickable."""
    attrs = "".join(f' data-{k}="{html.escape(v)}"' for k, v in data.items()) + (' aria-current="true"' if current else "")
    return f'<a class="lnk" data-set{attrs} href="{html.escape(set_url(field, back))}">{html.escape(text)}</a>'


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not nuc_config.WINDOWS  # on Windows SO_REUSEADDR lets another program bind the same port

    def __init__(self, addr, cfg, token="", demo=False, zoom=100):
        self.cfg, self.token, self.zoom = cfg, token, zoom
        self.allowed = {"localhost", "127.0.0.1", "::1", addr[0].lower(), socket.gethostname().lower()} | set(cfg.get("allowed_hosts", []))
        self.slots = threading.BoundedSemaphore(MAX_CONN)
        self.streams = threading.BoundedSemaphore(STREAMS_MAX)  # /api/v1/stream: each holds one of the slots above for minutes
        self.stream_s, self.keepalive_s, self.stream_tick = STREAM_S, KEEPALIVE_S, 0  # (tick 0: the refresh interval; the tests shorten them)
        self.csrf = secrets.token_urlsafe(24)  # in every form of the AI page; a page of another site cannot read it
        self.config_note = None  # the settings page's last save of config.ini: {at, section, ok, lines, form} (settings_action)
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        render.DEMO = demo
        aiweb.configure(demo=demo)  # the engine of the AI page: the real one, or the demo's (simulated); render.ai_engine() tells it where the settings are
        aiweb.bind(state=self.machine_state)  # ... and the chat's model is given the machine as these pages show it
        tgweb.configure(demo=demo)  # the engine of the Telegram page (pairing, requests to the notifier), the same way
        tgweb.bind(cfg=lambda: render.CFG)
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
            confirm, prompt = ai_asked(rows, sel, state.get("confirm", ""), state.get("prompt", ""))
            norm = {"sel": sel, "pause": pause, "confirm": confirm, "prompt": prompt, "version": aiweb.version()}
            key = ("ai", zoom, r, sel, pause, confirm, prompt, aiweb.version()) + tuple(here.items())  # a job that ends, a server that starts: a new page
            return serve(key, min(r / 2, 1.0), lambda sh: self.ai_page(here, sel, pause, confirm, zoom, r, sh, prompt))  # a job's progress moves: never older than a second
        if view == "telegram":  # the shell only (uses_shell); a pairing that waits, an answer of the notifier: a new page
            norm = {"version": tgweb.version()}
            return serve(("telegram", zoom, r, tgweb.version()) + tuple(here.items()), min(r / 2, 1.0), lambda sh: self.telegram_page(here))
        return serve((cols, rows, zoom, fit, full, rotate, kiosk, r), r / 2, lambda sh: self.dashboard(here, zoom, r))

    # ---- the live app: /app (src/appjs.py draws the screens from the data API) -------------------------------------------------------

    def app_page(self, cookie, q):
        """The live app's page: the shell's frame (top bar, key figures, footer, help) around an empty <main id="app">, the settings the app
        needs (#cfg: the refresh, the screens on, the layout and order of the cards and the key figures the preferences give) and the first
        documents (#doc), so that the first paint needs no request; then APP_JS draws and listens, KEYS_JS and PREFS_JS do what they do on the
        shell. Without scripts the page says so and links to the shell. live=0: the page as it is now, no stream (data-static: a snapshot, a
        screenshot, the browser check)."""
        p = view_params(q)
        feats = lambda f: render.CFG["features"].get(f, True)  # noqa: E731
        view = p["view"] if p["view"] in APP_VIEWS and feats(p["view"]) else ""
        eff, src = prefs.effective(render.CFG.get("ui"), cookie, p["ui"])
        cookie = prefs.dump_cookie(prefs.parse_cookie(cookie))  # what the browser holds, as a valid string ('1' when nothing), as the shell reads it
        on = [c for c in prefs.CARDS if cards.enabled(c, render.CFG)]
        r = self.cfg["refresh_seconds"]
        conf = {"refresh": r, "views": [v for v in APP_VIEWS if feats(v)], "layout": [[c, w] for c, w in prefs.visible_cards(eff, on)],
                "order": eff["order"], "custom": prefs.custom_layout(src, eff["order"]), "kpis": list(eff["kpis"]), "kpi_card": KPI_CARD,
                "kpi_view": KPI_VIEW, "tabs": [[digit, name, TAB_TITLES[name]] for digit, name in ui.screen_keys(feats)]}
        qq = parse_qs(urlsplit(app_url(dict(p, view=view))).query)
        first = {}
        for name in ([view, "summary"] if view else ["overview"]):
            try:
                doc = self.api(name, qq)
                if doc is not None:
                    first[name] = json.loads(doc)
            except Exception as e:  # noqa: BLE001 - the stream brings it
                print("nuc-console web: app %s error: %r" % (name, e), file=sys.stderr)
        top = first.get("summary") or first.get("overview") or {}
        state, text = (top.get("status") or {}).get("state", "unknown"), (top.get("status") or {}).get("text", "?")
        host = socket.gethostname()
        tabs = [htmlview.tab(digit, TAB_TITLES[name], APP_PATH if name == "overview" else APP_PATH + "?view=" + name, (name if name != "overview" else "") == view)
                for digit, name in ui.screen_keys(feats)]
        here = app_url(dict(p, view=view))
        top_bar = htmlview.topbar(host, state, text, tabs, time.strftime("%H:%M:%S"), "#help", page_url({"view": "settings"}), False)
        back = urlsplit(page_url({"view": view} if view else {})).query
        setlink = lambda field, label, cur, **data: set_link(field, back, label, cur, **data)  # noqa: E731
        groups = ['<span class="live">live</span>',
                  f'<a class="lnk" data-pause data-key="Z" aria-pressed="false" href="{html.escape(here)}">pause</a>',
                  f'<a class="lnk" href="{html.escape(page_url({"view": view} if view else {}) if view else "/")}">classic pages</a>',
                  '<span class="grp">theme: ' + " ".join(setlink("t" + code, label, eff["theme"] == name, theme=name) for name, label, code in THEMES) + "</span>",
                  '<span class="grp">density: ' + " ".join(setlink("d" + code, label, eff["density"] == name, density=name) for name, label, code in DENSITIES) + "</span>"]
        data = lambda obj, ident: ('<script type="application/json" id="%s">' % ident) + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).replace(
            "<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + "</script>"  # noqa: E731 - nothing in it can end the element
        body = (top_bar + '<div class="kpiblock" id="kpis"></div><div id="stale" class="stale-banner" role="status" hidden></div>'
                + f'<main id="app" class="{"view" if view else "grid"}"{" data-static" if (q.get("live") or [""])[0] == "0" else ""}>'
                + '<noscript><p class="msg lv-info"><span class="sym">\u00b7</span> the live app needs JavaScript: the <a href="/">classic pages</a> do not.</p></noscript>'
                + "</main>" + htmlview.foot(groups, time.strftime("%H:%M:%S"))
                + htmlview.help_dialog(ui.help_rows(view or "overview", feats, bool(nuc_config.PORTABLE), False))
                + data(conf, "cfg") + data(first, "doc"))
        scripts = [appjs.APP_JS, webjs.KEYS_JS, webjs.PREFS_JS]
        page = Page(htmlview.shell_doc(f"{host} · {view or 'overview'} · nuc-console", webcss.asset_path("app"), body, eff["theme"], eff["density"], 100,
                                       int(time.time() // 600) % 3, False, "url" if p["ui"] else "cookie" if cookie != prefs.COOKIE_VERSION else "config",
                                       cookie if not p["ui"] and cookie != prefs.COOKIE_VERSION else "", 0, "app", scripts))
        page.csp = page_csp(scripts, shell=True)
        return page

    # ---- the data API: /api/v1 (webapi.py turns the components into JSON) -----------------------------------------------------------

    def api_index(self):
        """GET /api/v1: what there is."""
        return {"api": webapi.VERSION, "version": nuc_config.VERSION, "refresh": self.cfg["refresh_seconds"],
                "views": [v for v in webapi.VIEWS if v in ("overview", "telegram", "summary") or render.CFG["features"].get(v, True)],
                "documents": API_PATH + "/<view>", "stream": API_PATH + "/stream?view=<view>"}

    def api(self, name, q):
        """The document of the view `name` (webapi.VIEWS) for the request's parameters (the page's: view_params checks and clamps them), as a
        webapi.Doc; None when the view's feature is off. Built at most once per half refresh interval for the same parameters, whoever asks (the
        AI's and the Telegram page's at most once a second: a job's progress moves), like a page."""
        p = view_params(dict(q, view=["" if name == "overview" else name]))
        here = {k: p[k] for k in HERE_KEYS}
        r = self.cfg["refresh_seconds"]
        feats = render.CFG["features"]
        if name in ("cpu", "health", "map", "ai") and not feats.get(name, True):
            return None
        if name == "overview":
            return self.cached(("api", name), r / 2, lambda: webapi.document(name, self.api_overview(r), time.time()))
        if name == "summary":
            return self.cached(("api", name), r / 2, lambda: webapi.document(name, self.api_summary(r), time.time()))
        if name == "cpu":
            self.cpu_feed.max_age = r
            sort, sel = p["sort"] or "cpu", p["sel"]
            return self.cached(("api", name, sort, sel), r / 2, lambda: webapi.document(name, self.api_cpu(here, sort, sel), time.time()))
        if name == "health":
            days = p.get("period") if p.get("period") in screens.HEALTH_DAYS else 7
            ids = {f["id"] for f in screens.health_findings(render.health_data(days)["report"])}
            sel = p["sel"] if p["sel"] in ids else ""
            hhere = dict({"view": "health", "period": days if days != 7 else 0, "sel": sel, "pause": False}, **here)
            return self.cached(("api", name, days, sel), r / 2, lambda: webapi.document(name, {"period": days, "sel": sel,
                               "nodes": health_nodes(self.smp, days, sel, hhere)}, time.time()))
        if name == "map":
            state = map_mode({k: p[k] for k in VIEW_KEYS["map"] if k not in ("as", "pause")})
            key = ("api", name) + tuple(sorted((k, tuple(sorted(v)) if isinstance(v, (set, frozenset, list, tuple)) else v) for k, v in state.items()))
            return self.cached(key, r / 2, lambda: webapi.document(name, self.api_map(here, state, r), time.time()))
        if name == "ai":
            rows = screens.ai_rows(render.ai_data()["cat"])
            sel = p["sel"] if p["sel"] in {m["id"] for m in rows} else ""
            confirm, prompt = ai_asked(rows, sel, p.get("confirm", ""), p.get("prompt", ""))
            return self.cached(("api", name, sel, confirm, prompt, aiweb.version()), min(r / 2, 1.0),
                               lambda: webapi.document(name, self.api_ai(here, sel, confirm, prompt), time.time()))
        return self.cached(("api", name, tgweb.version()), min(r / 2, 1.0), lambda: webapi.document(name, self.api_telegram(), time.time()))

    def api_overview(self, r):
        """The overview's fields: the status pill, the key figures, every card whose feature is on (built in full: nothing hidden), the layout
        config.ini gives them (which, in which order, how wide) and the problems."""
        ctx = self.shell_frame(r)
        eff, _src = prefs.effective(render.CFG.get("ui"), "", "")
        on = [c for c in prefs.CARDS if cards.enabled(c, render.CFG)]
        built = []
        for cid in on:
            try:
                built.append(cards.build(cid, ctx, -2, cards.Caps(WEB_COLS, True)))
            except Exception as e:  # noqa: BLE001 - a broken card is unknown, the others stay
                print("nuc-console web: card %s error: %r" % (cid, e), file=sys.stderr)
                entry = cards.CARDS.get(cid)
                built.append(ui.Card(cid, entry.title if entry else cid, "could not be drawn (see the service log)", "unknown", []))
        state, text = self.shell_pill(ctx.problems)
        stale = bool({pid for _s, pid in ctx.problem_ids()} & set(STALE_PROBLEMS))
        return {"host": socket.gethostname(), "status": {"state": state, "text": text}, "stale": stale, "restart": render.CMD.get("restart", ""),
                "kpis": cards.kpis(ctx, prefs.KPI_IDS), "cards": built,
                "badges": self.tab_badges(ctx, r, lambda f: render.CFG["features"].get(f, True)),
                "layout": [{"id": cid, "w": w} for cid, w in prefs.visible_cards(eff, on)], "problems": webapi.problems(ctx.problems)}

    def api_summary(self, r):
        """What every page shows above its view: the host, the status pill, every key figure, whether a collector's data is old or missing
        (the banner of the pages) and how many problems there are."""
        ctx = self.shell_frame(r)
        state, text = self.shell_pill(ctx.problems)
        stale = bool({pid for _s, pid in ctx.problem_ids()} & set(STALE_PROBLEMS))
        return {"host": socket.gethostname(), "status": {"state": state, "text": text}, "kpis": cards.kpis(ctx, prefs.KPI_IDS),
                "stale": stale, "restart": render.CMD.get("restart", ""), "problems": len(ctx.problems or []),
                "badges": self.tab_badges(ctx, r, lambda f: render.CFG["features"].get(f, True))}

    def machine_state(self):
        """The machine as the pages show it now, for the AI's chat (advisor.machine_state: the model reads it with a question): the status, the
        problems, every key figure, the busiest processes (names only) and the HEALTH findings. Read from the frame the pages share; the
        processes from the CPU page's sampler, at most once per refresh interval, only while the CPU screen is on."""
        r = self.cfg["refresh_seconds"]
        ctx = self.shell_frame(r)
        _state, text = self.shell_pill(ctx.problems)
        pb = [{"level": "err" if sev >= 2 else "warn" if sev >= 1 else "info", "text": words, "id": pid or ""}
              for (sev, words), (_s, pid) in zip(ctx.problems or [], ctx.problem_ids())]
        figures = [{"label": k.label, "value": k.value, "unit": k.unit, "state": k.state, "hint": k.hint} for k in cards.kpis(ctx, prefs.KPI_IDS)]
        rows = []
        if render.CFG["features"].get("cpu", True):
            with self.lock:  # the CPU page reads it under the same lock
                rows = ((self.cpu_feed.read() or {}).get("procs") or {}).get("procs") or []
        report = (ctx.health or {}).get("report") if isinstance(ctx.health, dict) else None
        findings = report.get("findings") if isinstance(report, dict) and isinstance(report.get("findings"), list) else []
        return advisor.machine_state(socket.gethostname(), render.cpu_os(), time.time(), text, pb, figures, rows, findings)

    def api_cpu(self, here, sort, sel):
        nodes, sel = self.cpu_nodes(dict({"view": "cpu", "sort": "" if sort == "cpu" else sort, "sel": sel}, **here), sort, sel)
        return {"sort": sort, "sel": sel, "nodes": nodes}

    def api_map(self, here, state, r):
        """The Map as a tree (the graph view's drawing is not data: its nodes and edges are the tree's rows and details)."""
        st, sel = map_state(state), state.get("sel", "")
        G, _pb = self.map_graph(r)
        prune(G, st)
        found = {}
        nodes = map_nodes(G, st, sel, map_here(here, st, sel, False), here, False, found)
        return {"sel": sel, "node": found.get("node"), "counts": graph.counts(G), "nodes": nodes}

    def api_ai(self, here, sel, confirm, prompt=""):
        """The AI screen's fields: its components (their ui.Action buttons carry the CSRF token: a client posts them as they are), the engine's state
        (the chat's prompts are not in it: prompt=<id> puts the one of that exchange in the nodes), and the CSRF token (none when [ai] web_actions = no
        locks the page)."""
        eng = render.ai_engine()
        snap = eng.snapshot(usage=True)
        nodes = ai_nodes(self, dict({"view": "ai", "sel": sel, "pause": False}, **here), sel, confirm, snap, eng, prompt)
        return {"sel": sel, "confirm": "" if snap["locked"] else confirm, "prompt": prompt, "engine": snap, "csrf": None if snap["locked"] else self.csrf,
                "nodes": nodes}

    def api_telegram(self):
        """The Telegram page's fields: the engine's state (never the bot's token) and, when its buttons work, the CSRF token and their actions. Less
        the clock readings that change on every read (the engine's now, the time of the notifier's status file: `listening` says what it means), so
        that the rev changes only with the state."""
        snap = tgweb.engine().snapshot()
        forms = not snap["locked"] and snap["listening"]
        snap = dict(snap, status=dict(snap["status"] or {}))
        snap.pop("now", None)
        snap["status"].pop("ts", None)
        return {"engine": snap, "csrf": self.csrf if forms else None, "actions": ["/telegram/" + a for a in TG_ACTIONS] if forms else []}

    # ---- the shell: `[ui] web = app` or ?app=1 --------------------------------------------------------------------------------------

    def uses_shell(self, view, app, card, edit=False):
        """The shell is the page for ?app=1, for the settings, the Telegram page, a single card and the layout editor (they exist only there), and for every page
        when [ui] web = app (?app=0 asks for the classic page anyway)."""
        return view in ("settings", "telegram") or bool(card) or edit or app == "1" or (app != "0" and (render.CFG.get("ui") or {}).get("web") == "app")

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
                              page_url(dict(here, view="settings")), view in ("settings", "telegram"))
        kpis = "" if view in ("settings", "telegram") else htmlview.kpis_block(tiles, note)
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
        quiet = view in ("settings", "telegram")  # pages to change things on: no refresh or pause links (the Telegram page reloads by itself only while it waits)
        groups = ["refresh every " + self.every(link, r) if not quiet and not edit and not wall else "",
                  f'<a class="lnk" data-pause data-key="Z" aria-pressed="{"true" if pause else "false"}" href="'
                  + html.escape(page_url(vhere, pause=not pause)) + f'">{"resume" if pause else "pause"}</a>' if not quiet and not edit else "",
                  link("Done", edit=False) if edit else link("Edit layout", edit=True) if view == "" and not card else "",
                  f'<a class="lnk" href="{html.escape(app_url(dict(vhere, view=view if view in APP_VIEWS else "")))}">live app</a>'
                  if not quiet and not edit and not wall and not (view == "map" and vhere.get("as") == "graph") else "",
                  "text " + self.sizes(link, zoom),
                  '<span class="grp">theme: ' + " ".join(setlink("t" + code, label, eff["theme"] == name, theme=name) for name, label, code in THEMES) + "</span>",
                  '<span class="grp">density: ' + " ".join(setlink("d" + code, label, eff["density"] == name, density=name) for name, label, code in DENSITIES) + "</span>",
                  "Telegram setup" if view == "telegram" else
                  "read-only · AI actions" if render.CFG["features"].get("ai", True) and advisor.web_actions_on(render.CFG) else "read-only"]
        if wall:  # nobody clicks on a wall: no pause, no layout editor, no size, theme or density links; the way out is the keyboard
            groups = [f"refreshed every {r} s", "read-only"]
        if here["kiosk"]:
            groups.append(html.escape(render.KIOSK_HINT))
        scope = view if view in ("map", "cpu", "health", "ai") else "global" if view in ("settings", "telegram") else "overview"
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

    def tab_badges(self, ctx, r, feats, graph_too=True):
        """What the tabs have to say, as data: {"map": the Map's problems, "health": [errors, warnings] of the findings, "ai": "on", "down" or
        "unknown" while the AI is enabled}; a screen with nothing to say has no entry. graph_too: count the Map's problems (it builds the graph)."""
        out = {}
        try:
            if feats("map") and graph_too:
                out["map"] = graph.counts(self.map_graph(r)[0])["problems"]
            rep = ((ctx.health or {}).get("report") or {}).get("findings") if feats("health") else None
            if isinstance(rep, list):
                out["health"] = [sum(1 for f in rep if isinstance(f, dict) and f.get("level") == lv) for lv in ("err", "warn")]
            ai = ctx.ai if feats("ai") and isinstance(ctx.ai, dict) else {}
            if ai.get("enabled"):
                answer = (ai.get("probe") or {}).get("state") if isinstance(ai.get("probe"), dict) else None
                out["ai"] = "on" if answer == "answering" else "down" if answer == "down" else "unknown"
        except Exception as e:  # noqa: BLE001 - a badge is a nicety: the tab stays
            print("nuc-console web: badge error:", repr(e)[:200], file=sys.stderr)
        return out

    def shell_tabs(self, ctx, here, view, r, feats):
        """The tabs: a link per screen with its key, a badge where there is something to say (the Map's problems, the Health findings, the AI)."""
        data, badges = self.tab_badges(ctx, r, feats, view not in ("settings", "telegram")), {}
        if "map" in data:
            badges["map"] = str(data["map"]) if data["map"] else ""
        if "health" in data:
            err, warn = data["health"]
            badges["health"] = " ".join(x for x in (f'<span class="s-err">✖{err}</span>' if err else "", f'<span class="s-warn">!{warn}</span>' if warn else "") if x)
        if "ai" in data:
            badges["ai"] = {"on": '<span class="s-ok">●</span> on', "down": '<span class="s-err">✖</span> down'}.get(data["ai"], '<span class="s-unknown">?</span>')
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
            live = field in ("theme", "density")  # switched in place by PREFS_JS: the chosen one stays a link, to come back to it
            return (f'<div class="fs"><span class="lab">{esc(title)} {where(field)}</span>' + htmlview.seg(title, opts, live)
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
            why = f"all {prefs.MAX_KPIS} places are taken: untick one first" if new else "at least one key figure stays"
            link_ = (f'<a href="{href("k" + "_".join(prefs.KPI_CODES[x] for x in new))}" data-set>{inner}</a>' if ok
                     else f'<span aria-disabled="true" title="{esc(why)}">{inner}</span>')
            boxes.append(f'<li class="{"on" if on else "off"}">{link_}' + (f'<span class="o">{cur.index(kid) + 1}</span>' if on else "") + "</li>")
        full = len(cur) >= prefs.MAX_KPIS  # a ninth cannot be added: say so, or the + of the others looks broken
        kpis = (f'<div class="fs"><span class="lab">Key figures (at most {prefs.MAX_KPIS}, shown in the order chosen) {where("kpis")}</span>'
                + (f'<p class="hintl" id="kpi-full">All {prefs.MAX_KPIS} places are taken: untick one (✔) to make room, then add another (+).</p>'
                   if full else "") + f'<ul class="kchk">{"".join(boxes)}</ul></div>')
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
        feats, forms = self.features_html(back)
        return View(f'<div class="settings">{appearance}{feats}{self.config_html(back)}{self.alerts_html(here)}{self.about_html(here)}</div>', [],
                    vhere, False, forms=forms, legacy=False)

    def features_html(self, back):
        """(the 'Screens and sections' block of the settings page, whether it has forms): every [features] switch with its state in words and a
        symbol; in a portable run (the desktop app) a button that turns it on or off (a form posting to /settings/feature with the CSRF token),
        in an installation the switches as they are and how to change them (config.ini is the administrator's: the page never writes it)."""
        esc, feats = html.escape, render.CFG["features"]
        ok, why = nuc_config.features_writable()
        path = nuc_config.config_path()
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v, quote=True)}">' for k, v in (("csrf", self.csrf), ("back", back)))
        rows = []
        for key, title, what in FEATURE_WORDS:
            on = bool(feats.get(key, True))
            state = f'<span class="fs-st {"on" if on else "off"}">{"✔ on" if on else "○ off"}</span>'
            ctl = (f'<form class="f" method="post" action="/settings/feature">{hidden}<input type="hidden" name="name" value="{key}">'
                   f'<input type="hidden" name="on" value="{"no" if on else "yes"}"><button class="btn{"" if on else " pri"}" type="submit">'
                   f'{"Turn off" if on else "Turn on"}</button></form>' if ok else "")
            rows.append(f'<li class="{"on" if on else "off"}" data-feature="{key}"><span class="ft"><b>{esc(title)}</b> <span class="sm">{esc(what)}</span></span>'
                        f'{state}{ctl}</li>')
        if ok:
            note = (f'Saved in <code class="cmd">{esc(path)}</code> as you click (the rest of the file stays as it is): the screens follow at once, '
                    'the collector within 10 seconds. A section that is off is not drawn, raises no alarm and is not collected.')
        else:
            restart = render.CMD.get("restart") or "restart nuc-console"
            note = (f'Read-only here ({esc(why)}): edit <b>[features]</b> in <code class="cmd">{esc(path)}</code>, then '
                    f'<code class="cmd">{esc(restart)}</code>.')
        html_ = (f'<section class="sec" id="features" aria-labelledby="sec-feat"><h3 class="sech" id="sec-feat">Screens and sections</h3>'
                 f'<p class="hintl">{note}</p><ul class="feats">{"".join(rows)}</ul></section>')
        return html_, ok

    def config_html(self, back):
        """The settings page's 'config.ini' block: every key of every section (confedit.KEYS) with its value in force, what it does, the values
        it takes, its default and when a change applies. In a portable run (the desktop app) each section is a form posting to
        /settings/config with the CSRF token; the locks and the keys only an installation reads are shown, not offered. In an installation
        the same, read-only, with the file to edit and how to restart. A section opens by itself after a save, with what the save did."""
        esc, cfg = html.escape, render.CFG
        ok, why = nuc_config.features_writable()
        path = nuc_config.config_path()
        note = self.config_note if self.config_note and time.time() - self.config_note["at"] < CONFIG_NOTE_S else None
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v, quote=True)}">' for k, v in (("csrf", self.csrf), ("back", back)))
        blocks = []
        for sec, title, what in confedit.SECTIONS:
            mine = note if note and note["section"] == sec else None
            typed = mine["form"] if mine and not mine["ok"] else {}  # a refused post: the page shows what was typed, to fix it
            if sec in confedit.MAPS:
                text = typed.get("text", confedit.map_text(cfg, sec))
                body = (f'<textarea class="cmap" id="f-{sec}" name="text" rows="{max(3, min(12, text.count(chr(10)) + 2))}" maxlength="1200" '
                        f'spellcheck="false" autocomplete="off" aria-label="[{sec}]">{esc(text)}</textarea>' if ok
                        else f'<pre class="cmap">{esc(text)}</pre>' if text else '<p class="hintl">Nothing in this section.</p>')
                body = f'<div class="ck wide">{body}</div>'
            else:
                body = "".join(self.config_row(key, typed.get(key.name), ok) for key in confedit.BY_SECTION[sec])
            if mine:
                body = (f'<div class="cfgn {"ok" if mine["ok"] else "bad"}" role="status">'
                        + "<br>".join(esc(x) for x in mine["lines"]) + "</div>" + body)
            if ok:
                body = (f'<form class="cfgf" method="post" action="/settings/config">{hidden}<input type="hidden" name="section" value="{sec}">'
                        f'{body}<div class="cfgb"><button class="btn pri" type="submit">Save [{sec}]</button></div></form>')
            blocks.append(f'<details class="cfgs" id="cfg-{sec}"{" open" if mine else ""}><summary><code>[{sec}]</code> {esc(title)}</summary>'
                          f'<p class="hintl">{esc(what)}</p>{body}</details>')
        if ok:
            head = (f'Every key of <code class="cmd">{esc(path)}</code>, section by section: <b>Save</b> writes the keys of that section you changed, '
                    'and nothing else of the file (its comments stay). A value is checked before it is written: one the dashboard would not '
                    'take is refused, and the file stays as it was. [features] is <a class="lnk" href="#features">Screens and sections</a> above.')
        else:
            head = (f'What <code class="cmd">{esc(path)}</code> sets, key by key, and what each one does. Read-only here ({esc(why)}): edit the file, '
                    f'then apply it: <code class="cmd">{esc(render.CMD.get("apply") or render.CMD["restart"])}</code>. [features] is <a class="lnk" href="#features">Screens '
                    'and sections</a> above.')
        return (f'<section class="sec" id="config" aria-labelledby="sec-cfg"><h3 class="sech" id="sec-cfg">config.ini</h3>'
                f'<p class="hintl">{head}</p><div class="cfgl">{"".join(blocks)}</div></section>')

    @staticmethod
    def config_row(key, typed, ok):
        """One key of the config.ini block: its name, its value (a control when the page may change it), and what it does, the values it takes,
        its default and when a change applies."""
        esc, cur = html.escape, confedit.value(render.CFG, key)
        v = cur if typed is None else typed
        fid = f"f-{key.section}-{key.name}"
        dflt = "default " + key.default if key.default and key.default != ", ".join(key.choices) else "default: all, in this order" if key.default else ""
        when = confedit.APPLIES[key.applies] if ok or key.applies in (confedit.LOCK, confedit.NOTIFIER) else ""  # installed: all at the restart
        meta = [x for x in (key.values(), dflt, when) if x]
        hint = f'<p class="hintl">{esc(key.what)}' + (f' <span class="sm">{esc(" · ".join(meta))}</span>' if meta else "") + "</p>"
        if not (ok and key.editable()):
            shown = f'<code class="cv">{esc(cur)}</code>' if cur else '<span class="sm">not set</span>'
            return f'<div class="ck cko" id="k-{key.section}-{key.name}"><span class="cn"><code>{key.name}</code></span>{shown}{hint}</div>'
        if key.kind in (confedit.BOOL, confedit.CHOICE):
            opts = list(confedit._YESNO if key.kind == confedit.BOOL else key.choices)
            if key.unset:
                opts.insert(0, "")
            if v not in opts:  # a value the file has that the page does not list (kiosk): shown as it is, kept unless changed
                opts.insert(0, v)
            ctl = (f'<select id="{fid}" name="{key.name}">' + "".join(
                f'<option value="{esc(o, quote=True)}"{" selected" if o == v else ""}>{esc(o or "not set (default " + key.default + ")")}</option>'
                for o in opts) + "</select>")
        elif key.kind == confedit.INT:
            lo = 0 if key.zero else key.lo
            ctl = f'<input id="{fid}" name="{key.name}" type="number" min="{lo}" max="{key.hi}" step="1" value="{esc(v, quote=True)}" required>'
        else:
            ctl = (f'<input id="{fid}" name="{key.name}" type="text" maxlength="{confedit.MAX_TEXT}" spellcheck="false" autocomplete="off" '
                   f'value="{esc(v, quote=True)}"' + (f' placeholder="{esc(key.default, quote=True)}"' if key.default else "") + ">")
        return f'<div class="ck" id="k-{key.section}-{key.name}"><label class="cn" for="{fid}"><code>{key.name}</code></label>{ctl}{hint}</div>'

    def alerts_html(self, here):
        """The settings page's 'Phone alerts' block: are the Telegram alerts on and do they reach a phone (the Telegram page's state, in words and
        a pill), what the setup takes, and the way to the Telegram page, where the steps, the pairing and the test are."""
        esc = html.escape
        try:
            snap = tgweb.engine().snapshot()
            state, line = tgweb.state_of(snap)
        except Exception as e:  # noqa: BLE001 - the block says it cannot tell, the page stays
            print("nuc-console web: telegram state error: %r" % (e,), file=sys.stderr)
            state, line = "down", "the notifier's state cannot be read"
        pill, cls = {"on": ("ON", "g"), "off": ("OFF", "d"), "unpaired": ("NOT PAIRED", "y"), "failing": ("FAILING", "r"), "down": ("DOWN", "r")}[state]
        go = esc(page_url(dict(here, view="telegram")) + "#tg")
        label = "Telegram alerts" if state == "on" else "Set up Telegram alerts" if state in ("off", "unpaired") else "Check the Telegram alerts"
        return ('<section class="sec" id="alerts" aria-labelledby="sec-alerts"><h3 class="sech" id="sec-alerts">Phone alerts</h3>'
                '<p class="hintl">New and resolved ATTENTION problems of this machine on your phone, through a Telegram bot of your own (free, about '
                'two minutes): create the bot with @BotFather, pair it with your @username, press Start, send a test. The Telegram page walks you '
                'through it. The machine only sends: it never reads your messages.</p>'
                f'<div class="fs"><span><span class="pl {cls} big">{pill}</span> {esc(line)}</span>'
                f'<div><a class="btn pri" href="{go}">{esc(label)}</a></div></div></section>')

    def about_html(self, here=None):
        """'About this machine': what the machine is and how it is set up, read-only (this page changes nothing on it)."""
        esc, cfg, here = html.escape, render.CFG, here or {}
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
        rows.append(("Telegram", esc(self.telegram_text()) + ' · <a class="lnk" href="%s">Telegram page</a>' % esc(page_url(dict(here, view="telegram")))))
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
        if not render.telegram_on():
            return "off ([telegram] enabled = no)"
        state, why = render.telegram_state(time.time())
        return {"ok": "on, paired and sending", "unreadable": "on; this account cannot read its status",
                "unpaired": "on, but not paired: pair it on the Telegram page", "down": "on, but the notifier is not running"}.get(
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
            nodes, sel = self.cpu_nodes(chere, sort, sel)
            chere["sel"] = sel
            body = '<div class="scr scr-cpu">' + "".join(htmlview.html(node) for node in nodes) + "</div>"
        except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
            print("nuc-console web: cpu render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
            body = '<p class="sm">render error (see the service log)</p>'
        tools = [f'<a href="{esc(page_url(chere, sel=""))}">close details</a>'] if sel else []  # no sel: the rows are links, nothing to say
        return View(body, tools, chere, True, legacy=False)

    def cpu_nodes(self, chere, sort, sel):
        """(the CPU screen's components for the web, the selected pid if that process still exists else ''): screens.cpu_view at the web's size,
        its column heads sort links and its rows selection links, made from chere (the page's parameters). The page and the data API draw these."""
        d = self.cpu_feed.read()
        sel = sel if sel and int(sel) in {p["pid"] for p in d["procs"]["procs"]} else ""  # no such process (any more): dropped
        chere = dict(chere, sel=sel)
        links = screens.CpuLinks(lambda by: page_url(chere, sort="" if by == "cpu" else by), lambda pid: page_url(chere, sel="" if str(pid) == sel else str(pid)))
        return screens.cpu_view(d, render.cpu_ctx(False), screens.WEB_W, 10 ** 4, sort, int(sel) if sel else None, bool(sel), 0, links).nodes, sel

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

    def ai_page(self, here, sel, pause, confirm, zoom, r, shell=False, prompt=""):
        """The AI page: the AI switch and what it is doing at the top, the chat under it (the model answers as soon as the server does), then what
        this machine can run: the models as a table (a button per model: use it), their details, the hardware and the status. Forms post to /ai/*
        (see do_POST); the page reloads by itself only while something runs, so that a question being typed is not lost. The URL holds the whole
        view, so a reload keeps it."""
        eng = render.ai_engine()
        snap = eng.snapshot(usage=True)
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
            return View(ai_native(self, ahere, sel, confirm, snap, eng, prompt), [], ahere, bool(not pause and live), forms=not snap["locked"],
                        wait=2 if snap["busy"] else 0, legacy=False)
        cols, rows = here["cols"] or min(self.cfg["columns"], HEALTH_COLS), []
        try:
            data, pb = render.ai_state(self.smp)
            rows = screens.ai_rows(data["cat"])
            ui = AiUi(snap, eng.choice() if not snap["locked"] else None, data["cat"], rows, sel, confirm, self.csrf, urlsplit(page_url(ahere)).query, ahere)
            ui.prompt = ai_prompt_node(eng, prompt, ahere)
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

    def telegram_page(self, here):
        return telegram_view(self, here)

    def tg_action(self, act, form):
        """What a post of the Telegram page asked, done through the engine (a pairing runs in the background), and the address to go to next. The
        token and the @username are checked by the engine and never echoed; the answer is the page's notice."""
        eng = tgweb.engine()
        one = lambda k, n=200: ((form.get(k) or [""])[0])[:n]  # noqa: E731
        if act == "pair":
            eng.pair(one("token", 120), one("username", 40))
        elif act == "cancel":
            eng.cancel()
        else:
            eng.request(act)
        with self.lock:
            self.cache.clear()  # the page that comes next shows what this did
        back = view_params(parse_qs(one("back", 400)))
        return page_url(dict({"view": "telegram"}, **{k: back[k] for k in HERE_KEYS})) + "#tg"

    def settings_action(self, act, form):
        """A [features] switch of the settings page (a portable run: do_POST checked that config.ini is this account's): the key is one of
        nuc_config.FEATURES and the value yes or no, nothing else of the request is written. config.ini keeps every other line; the screens
        follow at once, the collector within a cycle. -> the settings page again."""
        one = lambda k, n=40: ((form.get(k) or [""])[0])[:n]  # noqa: E731
        if act == "config":
            return self.config_action(form, one("section", 20), view_params(parse_qs(one("back", 400))))
        name, on = one("name"), one("on", 3)
        if act != "feature" or name not in nuc_config.FEATURES or on not in ("yes", "no"):
            raise BadRequest("a feature of [features] and yes or no are needed")
        nuc_config.set_feature(name, on == "yes")
        if name == "ai" and on == "no":  # the AI screen goes: the model server this process started goes too (no page would be left to stop it)
            render.ai_engine().stop_server()
        with self.lock:
            self.cache.clear()  # what the pages show changes with it
        back = view_params(parse_qs(one("back", 400)))
        return page_url(dict({"view": "settings"}, **{k: back[k] for k in HERE_KEYS})) + "#features"

    def config_action(self, form, section, back):
        """A section of the settings page's config.ini block (a portable run: do_POST checked that the file is this account's): the keys the page
        may change are checked (confedit.save: what load() would not take is refused, and nothing is written), the file keeps every other line,
        and this process reads it again at once (render.reload_config, and the web view's own grid, pace and zoom). A refused post comes back
        with the reasons and what was typed. -> the settings page, at that section."""
        if section not in confedit.TITLES:
            raise BadRequest("a section of config.ini is needed")
        typed = {k: v[0][:3000] for k, v in form.items() if k not in ("csrf", "back", "section") and v}
        try:
            names = confedit.save(nuc_config.config_path(), section, form)
        except confedit.Refused as e:
            note = dict(ok=False, lines=["Not saved, nothing changed:"] + [r[:300] for r in e.reasons[:12]], form=typed)
        else:
            if names:
                render.reload_config()
                web = render.CFG["web"]
                for k in ("columns", "rows", "refresh_seconds"):  # what this server copied at its start; the rest of [web] is read at the next one
                    self.cfg[k] = web[k]
                self.zoom = render.CFG["display"]["zoom"]
                lines = ["Saved in config.ini: " + ", ".join(names) + "."]
                for code, keys in confedit.applies(section, names).items():
                    lines.append("%s: %s." % (", ".join(keys), confedit.APPLIES[code]))
                if confedit.START in confedit.applies(section, names):
                    lines.append("To start again: " + (render.CMD.get("apply") or render.CMD["restart"]) + ".")
            else:
                lines = ["Nothing changed: every value is the one config.ini already has."]
            note = dict(ok=True, lines=lines, form={})
        self.config_note = dict(note, at=time.time(), section=section)
        with self.lock:
            self.cache.clear()  # what the pages show changes with it
        return page_url(dict({"view": "settings"}, **{k: back[k] for k in HERE_KEYS})) + "#cfg-" + section

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
        elif act == "clear":  # the chat goes for good: asked first
            if yes:
                eng.clear_chat()
            else:
                confirm = "clear"
            anchor = "#ask"
        elif act == "load":
            eng.load_model()
        with self.lock:
            self.cache.clear()  # the page that comes next shows what this did
        back = view_params(parse_qs(one("back", 400)))
        here = {k: back[k] for k in HERE_KEYS}
        ai = back.get("view") == "ai"
        return page_url(dict({"view": "ai", "sel": sel or (back.get("sel") if ai else ""), "confirm": confirm,
                              "prompt": back.get("prompt", "") if ai and act != "clear" else ""}, **here)) + anchor


def telegram_view(srv, here):
    """The View of the Telegram page for the shell: it reloads (every 2 s) only while something runs (a pairing, an answer awaited), so that a token
    being typed is not lost; it has forms unless it is locked, a portable run, or the notifier does not listen."""
    there = dict(here, view="telegram")
    try:
        snap = tgweb.engine().snapshot()
        body = telegram_html(snap, srv.csrf, urlsplit(page_url(there)).query, there)
        forms = not snap["locked"] and snap["listening"]
        busy = snap["busy"]
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: telegram render error:", repr(e)[:200], file=sys.stderr)
        body, forms, busy = '<p class="sm">the Telegram page could not be drawn (see the service log)</p>', False, False
    return View(body, [], there, busy, forms=forms, wait=2 if busy else 0, legacy=False)


# ---- the graph view of the MAP ------------------------------------------------------------------------------------------


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
