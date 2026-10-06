#!/usr/bin/env python3
"""Open every page of the web shell in a real headless Chrome / Chromium and fail on what the static tests cannot see.

    python3 tools/browser_check.py [--chrome PATH] [--out DIR] [--only TEXT] [--quick] [--keep-going]

It starts `src/web.py --demo` on a free loopback port (nothing real is read, nothing is downloaded), then loads each page of the matrix:
every view (overview, map as a tree and as a graph, cpu, health, ai, settings, the layout editor) in every theme and every density
(`?ui=1.t<d|l|h|a>.d<w|k|c>`), with the scripts on and the dumped DOM taken after they ran, plus every view with the scripts off (the
page must then be complete by itself), plus the wall page (`ui=1.dw&kiosk=1`). A page fails when

  * the browser logs a Content-Security-Policy or Trusted Types violation ("Refused to ...", "TrustedHTML" ...), a script error, an
    uncaught exception or a failed load of one of the page's own files (the log of `--enable-logging=stderr`, parsed by `parse_log`);
  * the DOM it dumps lacks a landmark (the top bar, `main`, the key figures, the view's own block: `missing_landmarks`).

The live app (`/app`, src/appjs.py) is loaded too, each of its screens in every theme (scripts on: it draws the page, so it has no
script-free variant; `live=0`, so that its stream does not keep the page from ever settling), and then its **parity** is checked: for
each screen a page that has the app draw the document of tests/golden.py's frozen world next to the markup htmlview.py draws of the
very same components (`parity_page`); after the script ran, the two must be the same markup (`parity_problems`). `--no-parity` skips it.

With `--out DIR` it also writes one screenshot per page. Exit status 0 when every page passed, 1 otherwise, 2 when no browser
or no server could be started. Python standard library only; the browser is found from `--chrome`, `$CHROME` or the usual names and
paths on Linux, macOS and Windows."""
import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

VIEWS = ("overview", "map", "map-graph", "cpu", "health", "ai", "settings", "telegram", "layout")
APP_VIEWS = ("overview", "cpu", "health", "map", "ai")  # the live app's screens (/app)
THEMES = (("auto", "a"), ("dark", "d"), ("light", "l"), ("high-contrast", "h"))
DENSITIES = (("wall", "w"), ("desk", "k"), ("compact", "c"))
# the graph is paused: its script reloads the page when the refresh is due, and a reload inside --virtual-time-budget keeps some Chrome
# builds (154 on the CI runner) from ever finishing the dump; paused, the same script runs (drawing, physics) and only the reload is off
VIEW_QUERY = {"overview": "", "map": "&view=map", "map-graph": "&view=map&as=graph&pause=1", "cpu": "&view=cpu", "health": "&view=health",
              "ai": "&view=ai", "settings": "&view=settings", "telegram": "&view=telegram", "layout": "&edit=1"}
WINDOW = "1440,900"
VIRTUAL_TIME_MS = 4000  # the scripts fetch, poll and replace blocks: let the page run this long (virtual time, so it does not wait)

CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "chromium-headless-shell")
CHROME_PATHS = (
    "/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/chromium", "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# What a page must hold, whatever its theme and density. "any" = one of the markers is enough.
COMMON_LANDMARKS = (("the top bar", ('<header class="topbar"',)), ("<main>", ("<main ", "<main>")),
                    ("the screens' navigation", ('<nav class="tabs"',)), ("the footer", ("<footer",)))
VIEW_LANDMARKS = {
    "overview": (("the key figures", ('class="kpis"',)), ("a card", ('<article class="card',))),
    "map": (("the map", ('class="scr mapv"',)),),
    "map-graph": (("the graph", ('id="gv"',)), ("the graph's drawing", ("<svg",))),
    "cpu": (("the CPU screen", ('class="scr scr-cpu"',)),),
    "health": (("the findings", ('class="hv"',)),),
    "ai": (("the AI screen", ('class="scr av"',)),),
    "settings": (("the settings", ('class="settings"',)),),
    "telegram": (("the Telegram page", ('id="tg"',)), ("its pairing form", ('action="/telegram/pair"',))),
    "layout": (("the layout editor", ("data-edit",)), ("a card", ('<article class="card',))),
    "wall": (("the key figures", ('class="kpis"',)), ("a card", ('<article class="card',))),
    # the live app: what its script drew from the documents (the server sends an empty <main id="app">)
    "app-overview": (("the key figures it drew", ('role="listitem"',)), ("a card it drew", ('<article class="card',))),
    "app-cpu": (("the CPU screen it drew", ('class="scr scr-cpu"',)),),
    "app-health": (("the findings it drew", ('class="hv"',)),),
    "app-map": (("the map it drew", ('class="scr mapv"',)),),
    "app-ai": (("the AI screen it drew", ('class="scr av"',)),),
}
# With the scripts off the page is the server's markup alone: the same landmarks.

_VIOLATION = (
    (re.compile(r"Refused to (apply|execute|load|connect|frame|navigate|send|create|evaluate|run)", re.I), "CSP"),
    (re.compile(r"Content[- ]Security[- ]Policy", re.I), "CSP"),
    (re.compile(r"Trusted ?Types|TrustedHTML|TrustedScript|TrustedScriptURL|require-trusted-types", re.I), "Trusted Types"),
)
_CONSOLE = re.compile(r"CONSOLE[^\]]*\]\s*(.*)$")
_LOG_PREFIX = re.compile(r"^\[\d+:\d+:\d{4}/\d{6}(?:\.\d+)?:(?P<level>[A-Z]+):(?P<src>[^\]]*)\]\s*(?P<msg>.*)$")
_JS_ERROR = re.compile(r"Uncaught|SyntaxError|ReferenceError|TypeError|RangeError|EvalError|Unhandled (promise )?rejection|Failed to load resource|"
                       r"net::ERR_|Failed to fetch", re.I)


# ------------------------------------------------------------------------------------------------------------- the matrix

class Page(object):
    """One load: a name, its path, and how it is loaded (js on or off) and how it is looked at (the landmarks, a screenshot)."""
    __slots__ = ("name", "path", "js", "kind")

    def __init__(self, name, path, js, kind):
        self.name, self.path, self.js, self.kind = name, path, js, kind


def ui_string(theme_code, density_code):
    return "1.t%s.d%s" % (theme_code, density_code)


def page_path(view, theme_code, density_code, kiosk=False):
    """The shell's address for a view in a theme and a density, e.g. /?app=1&ui=1.td.dc&view=cpu (prefs.py: ?ui= is one URL's own preferences)."""
    path = "/?app=1&ui=" + ui_string(theme_code, density_code) + VIEW_QUERY[view]
    return path + ("&kiosk=1" if kiosk else "")


def app_path(view, theme_code, density_code):
    """The live app's address for a screen in a theme and a density, without its stream: /app?ui=1.td.dk&live=0&view=cpu."""
    return "/app?ui=" + ui_string(theme_code, density_code) + "&live=0" + ("" if view == "overview" else "&view=" + view)


def build_matrix(quick=False):
    """-> the list of Page to load. Scripts on: every view x theme x density, and the wall page (kiosk) in every theme; scripts off: every
    view x theme in the desk density, and the wall page. quick: the default theme and density only, plus the dark/compact pair."""
    themes = (THEMES[0], THEMES[1]) if quick else THEMES
    densities = (DENSITIES[1],) if quick else DENSITIES
    pages = []
    for view in VIEWS:
        for theme, tc in themes:
            for density, dc in densities:
                pages.append(Page("%s/%s/%s/js" % (view, theme, density), page_path(view, tc, dc), True, view))
    for theme, tc in themes:
        pages.append(Page("wall/%s/js" % theme, page_path("overview", tc, "w", kiosk=True), True, "wall"))
    for view in VIEWS:
        for theme, tc in themes[:1] if quick else themes:
            pages.append(Page("%s/%s/desk/nojs" % (view, theme), page_path(view, tc, "k"), False, view))
    pages.append(Page("wall/auto/nojs", page_path("overview", "a", "w", kiosk=True), False, "wall"))
    for view in APP_VIEWS:  # the live app: scripts on only (it is the script that draws the page)
        for theme, tc in themes:
            pages.append(Page("app-%s/%s/desk/js" % (view, theme), app_path(view, tc, "k"), True, "app-" + view))
        if not quick:
            pages.append(Page("app-%s/dark/compact/js" % view, app_path(view, "d", "c"), True, "app-" + view))
    return pages


# ----------------------------------------------------------------------------------------------- what a page is checked for

def parse_log(text):
    """Chrome's stderr (--enable-logging=stderr --v=0) -> [(kind, message)] of what makes a page fail: CSP and Trusted Types violations,
    script errors, uncaught exceptions, a failed load. Console lines look like
    [1234:1234:0102/030405.678:INFO:CONSOLE(12)] "Refused to execute inline script ...", source: http://127.0.0.1:1/ (12)
    Anything else the browser logs (GPU, D-Bus, sandbox ...) is its own noise and dropped, unless it names the page's policy."""
    found = []
    for line in text.splitlines():
        m = _CONSOLE.search(line)
        msg = m.group(1).strip() if m else line.strip()
        kind = None
        for rx, name in _VIOLATION:
            if rx.search(msg):
                kind = name
                break
        if kind is None and m:
            kind = "error" if _JS_ERROR.search(msg) else None
        if kind is None and m and "favicon" not in msg:
            lp = _LOG_PREFIX.match(line)
            kind = "console error" if lp and lp.group("level") == "ERROR" else None
        if kind and "favicon" not in msg:
            found.append((kind, msg[:300]))
    return found


def missing_landmarks(dom, kind):
    """The dumped DOM of a page of `kind` (a view name or "wall") -> the names of the landmarks it lacks (none: complete)."""
    if not dom or "<html" not in dom.lower():
        return ["a document"]
    missing = []
    for name, markers in COMMON_LANDMARKS + VIEW_LANDMARKS.get(kind, ()):
        if not any(marker in dom for marker in markers):
            missing.append(name)
    return missing


# ---------------------------------------------------------------------------------------------------- finding and running Chrome

def find_chrome(explicit=None, environ=None):
    """--chrome, $CHROME, then the usual names on PATH and the usual install paths. -> a path, or None."""
    environ = os.environ if environ is None else environ
    for cand in (explicit, environ.get("CHROME")):
        if cand:
            return cand if os.path.isfile(cand) else shutil.which(cand)
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    return None


def chrome_args(chrome, profile, root=None):
    """The arguments every run shares. A root user (a container) needs --no-sandbox: Chrome refuses to sandbox as root."""
    root = (hasattr(os, "geteuid") and os.geteuid() == 0) if root is None else root
    args = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--hide-scrollbars", "--mute-audio",
            "--user-data-dir=" + profile, "--window-size=" + WINDOW, "--enable-logging=stderr", "--v=0",
            "--virtual-time-budget=%d" % VIRTUAL_TIME_MS]
    if root:
        args.append("--no-sandbox")
    return args


def run_chrome(args, timeout=90):
    """-> (returncode, stdout, stderr); a run that hangs is killed and reported as rc -1."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", "timeout after %d s" % timeout


def check_page(chrome, base, page, profile, out=None):
    """Load one page: -> (list of problems as strings, seconds). `base` is the demo server, or for a page without scripts the proxy that
    strips them (see NoScriptProxy); either way the DOM is the browser's own (--dump-dom) and its log is parsed for violations."""
    t0 = time.time()
    url = base + page.path
    problems = []
    rc, dom, err = run_chrome(chrome_args(chrome, profile) + ["--dump-dom", url])
    if rc != 0:
        problems.append("browser exited with %s: %s" % (rc, err.strip().splitlines()[-1][:200] if err.strip() else "no output"))
    logs = err
    if out:
        shot = os.path.join(out, page.name.replace("/", "_") + ".png")
        rc2, _o, err2 = run_chrome(chrome_args(chrome, profile) + ["--screenshot=" + shot, url])
        if rc2 != 0 or not os.path.isfile(shot):
            problems.append("no screenshot (rc %s)" % rc2)
        logs += "\n" + err2
    for kind, msg in parse_log(logs):
        problems.append("%s: %s" % (kind, msg))
    for name in missing_landmarks(dom, page.kind):
        problems.append("no %s in the DOM" % name)
    return problems, time.time() - t0


_SCRIPT = re.compile(r"<script\b.*?</script\s*>", re.I | re.S)


def strip_scripts(markup):
    """The markup without its <script> elements: the page a browser with scripts switched off has to work with."""
    return _SCRIPT.sub("", markup)


class NoScriptProxy(object):
    """A second loopback server that forwards every GET to the demo server and removes the scripts from the HTML it returns (same status,
    same Content-Security-Policy and other headers). Chrome cannot dump a DOM with its script engine off, and this keeps the browser's
    own rendering, style sheets and log for the pages that must work without any script."""

    def __init__(self, upstream):
        import http.server
        import threading
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    r = urllib.request.urlopen(outer.upstream + self.path, timeout=20)
                    code, headers, body = r.getcode(), r.headers, r.read()
                except urllib.error.HTTPError as e:
                    code, headers, body = e.code, e.headers, e.read()
                if "text/html" in (headers.get("Content-Type") or ""):
                    body = strip_scripts(body.decode("utf-8", "replace")).encode("utf-8")
                self.send_response(code)
                for k, v in headers.items():
                    if k.lower() not in ("content-length", "connection", "transfer-encoding", "server", "date"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.upstream = upstream
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ------------------------------------------------------------------------------------------------------- the app's parity

_REV = re.compile(r' data-rev="[^"]*"')
_ROWS = re.compile(r'(<article class="card s\d st-[a-z]+) r\d+"')
_REGION = {"app": re.compile(r'<main id="app"[^>]*>(.*)</main><!--END-APP-->', re.S),
           "expect": re.compile(r'<div id="expect">(.*)</div><!--END-EXPECT-->', re.S),
           "kpis": re.compile(r'<div class="kpiblock" id="kpis">(.*)</div><!--END-KPIS-->', re.S),
           "expect-kpis": re.compile(r'<div id="expect-kpis">(.*)</div><!--END-EXPECT-KPIS-->', re.S)}


def parity_page(view):
    """-> (the page, its Content-Security-Policy) for one screen of the live app: web.Server.app_page of tests/golden.py's frozen world (the
    app draws the document embedded in it, no stream), with markers after the regions the app draws (#kpis, main#app) and, before </body>,
    what htmlview.py draws of the same components: #expect (the screen) and #expect-kpis (the key figures, on the overview)."""
    for d in (os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")):
        if d not in sys.path:
            sys.path.insert(0, d)
    import golden
    import htmlview
    import prefs
    import render
    import web
    shown = golden.CHAT[0]["id"]  # the AI screen with its chat (the log the app draws too) and the whole prompt of its first answer
    with golden.FrozenWorld(chat=golden.CHAT if view == "ai" else ()) as world:
        srv = world.server
        srv.cache.clear()
        page = srv.app_page("", {"live": ["0"]} if view == "overview" else dict({"view": [view], "live": ["0"]}, **({"prompt": [shown]} if view == "ai" else {})))
        r = srv.cfg["refresh_seconds"]
        here = {k: v for k, v in web.view_params({}).items() if k in web.HERE_KEYS}
        kpis = ""
        if view == "overview":
            body = srv.api_overview(r)
            eff, _src = prefs.effective(render.CFG.get("ui"), "", "")
            layout = [(cid, w) for cid, w in prefs.visible_cards(eff, [c for c in prefs.CARDS if web.cards.enabled(c, render.CFG)])]
            by = {c.id: c for c in body["cards"]}
            order = [(by[cid], w) for cid, w in layout if cid in by]
            if eff["order"] == "severity":
                order.sort(key=lambda x: web.STATE_RANK.get(x[0].state, 3))
            screen = "".join(htmlview.card_article(c.id, c.title, c.note, c.state, w, "".join(htmlview.html(x) for x in c.body)) for c, w in order)
            ids = {cid for cid, _w in layout}
            byk = {k.id: k for k in body["kpis"]}
            tiles = []
            for kid in eff["kpis"]:
                if kid not in byk:
                    continue
                if kid in web.KPI_VIEW and render.CFG["features"].get(web.KPI_VIEW[kid], True):
                    to = "/app?view=" + web.KPI_VIEW[kid]
                else:
                    to = "#c-" + web.KPI_CARD[kid] if web.KPI_CARD.get(kid) in ids else ""
                tiles.append(htmlview.kpi_tile(byk[kid], to))
            note = htmlview.banner("!", "some of the data is old or missing: a collector is not running?",
                                   'restart it: <code class="cmd">%s</code>' % htmlview.esc(render.CMD.get("restart", ""))) if body["stale"] else ""
            kpis = note + '<div class="kpis" role="list" aria-label="Key figures">' + "".join(tiles) + "</div>"
        else:
            if view == "cpu":
                nodes = srv.api_cpu(here, "cpu", "")["nodes"]
            elif view == "health":
                nodes = web.health_nodes(srv.smp, 7, "", dict({"view": "health", "period": 0, "sel": "", "pause": False}, **here))
            elif view == "map":
                p = web.view_params({"view": ["map"]})
                nodes = srv.api_map(here, web.map_mode({k: p[k] for k in web.VIEW_KEYS["map"] if k not in ("as", "pause")}), r)["nodes"]
            else:
                nodes = srv.api_ai(here, "", "", shown)["nodes"]
            box = {"cpu": "scr scr-cpu", "health": "hv", "map": "scr mapv", "ai": "scr av"}[view]
            screen = '<div class="%s">' % box + "".join(htmlview.html(n) for n in nodes) + "</div>"
    html_text = str(page).replace('<div class="kpiblock" id="kpis"></div>', '<div class="kpiblock" id="kpis"></div><!--END-KPIS-->', 1)
    html_text = html_text.replace("</main>", "</main><!--END-APP-->", 1)
    expect = ('<div id="expect-kpis">' + _REV.sub("", kpis) + '</div><!--END-EXPECT-KPIS--><div id="expect">' + _REV.sub("", screen)
              + "</div><!--END-EXPECT-->")
    return html_text.replace("</body>", expect + "</body>", 1), page.csp


def parity_problems(dom, view):
    """What differs, in the DOM the browser dumped, between what the app drew and what htmlview.py draws: [] when the same. The grid rows
    the app measured for each card (class rN) are not compared: the server's page estimates them instead."""
    got = {k: (rx.search(dom).group(1) if rx.search(dom) else None) for k, rx in _REGION.items()}
    out = ["no %s region in the DOM" % k for k, v in got.items() if v is None]
    if out:
        return out
    pairs = [("the screen", _ROWS.sub(r'\1"', got["app"]), got["expect"])]
    if view == "overview":
        pairs.append(("the key figures", got["kpis"], got["expect-kpis"]))
    for what, a, b in pairs:
        if a != b:
            at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            out.append("%s differs at %d: app %r / htmlview %r" % (what, at, a[max(0, at - 80):at + 80], b[max(0, at - 80):at + 80]))
    return out


def check_parity(chrome, profile, views=APP_VIEWS):
    """-> {view: problems}: each parity page served on a loopback port of its own (with its CSP), dumped by the browser, compared."""
    import http.server
    import threading
    out = {}
    for view in views:
        text, csp = parity_page(view)
        body = text.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200 if urllib.parse.urlsplit(self.path).path == "/app" else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", csp)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/app%s" % (httpd.server_address[1], "" if view == "overview" else "?view=" + view)
            rc, dom, err = run_chrome(chrome_args(chrome, profile) + ["--dump-dom", url])
            problems = ["browser exited with %s" % rc] if rc != 0 else []
            problems += ["%s: %s" % (k, m) for k, m in parse_log(err)]
            out[view] = problems + parity_problems(dom, view)
        finally:
            httpd.shutdown()
            httpd.server_close()
    return out


# ------------------------------------------------------------------------------------------------------------ the server

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(port):
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "web.py"), "--demo", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        if proc.poll() is not None:
            return proc, None
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            return proc, base
        except Exception:
            time.sleep(0.1)
    return proc, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", help="the Chrome / Chromium to run (default: $CHROME, then the usual names and paths)")
    ap.add_argument("--out", help="write one screenshot per page into this folder")
    ap.add_argument("--only", help="only the pages whose name contains this text (e.g. 'cpu/dark')")
    ap.add_argument("--quick", action="store_true", help="fewer themes and densities (a smoke run)")
    ap.add_argument("--keep-going", action="store_true", help="accepted for clarity: every page is always checked, the status says whether all passed")
    ap.add_argument("--port", type=int, default=0, help="the port of the demo server (default: a free one)")
    ap.add_argument("--list", action="store_true", help="print the pages and stop")
    ap.add_argument("--no-parity", action="store_true", help="do not compare what the live app draws with what htmlview.py draws")
    a = ap.parse_args(argv)
    pages = build_matrix(a.quick)
    if a.only:
        pages = [p for p in pages if a.only in p.name]
    if a.list:
        for p in pages:
            print(p.name, p.path)
        return 0
    chrome = find_chrome(a.chrome)
    if not chrome:
        print("browser_check: no Chrome or Chromium found (use --chrome PATH or set CHROME)", file=sys.stderr)
        return 2
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    proc, base = start_server(a.port or free_port())
    profile = tempfile.mkdtemp(prefix="nuc-browser-")
    failed = 0
    noscript = None
    try:
        if base is None:
            print("browser_check: the demo server did not start: " + (proc.stderr.read().decode("utf-8", "replace")[-500:] if proc.poll() is not None else "timeout"),
                  file=sys.stderr)
            return 2
        noscript = NoScriptProxy(base)
        print("browser_check: %s, %d pages, %s" % (chrome, len(pages), base))
        for p in pages:
            problems, secs = check_page(chrome, base if p.js else noscript.base, p, profile, a.out)
            print("%s  %-34s %4.1fs" % ("FAIL" if problems else "ok  ", p.name, secs))
            for line in problems[:8]:
                print("      " + line)
            if len(problems) > 8:
                print("      ... %d more" % (len(problems) - 8))
            failed += bool(problems)
        if not a.no_parity and not a.only:
            for view, problems in check_parity(chrome, profile).items():
                print("%s  %-34s" % ("FAIL" if problems else "ok  ", "app parity: " + view))
                for line in problems[:4]:
                    print("      " + line[:600])
                failed += bool(problems)
    finally:
        if noscript:
            noscript.close()
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    print("browser_check: %d pages, %d failed" % (len(pages), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
