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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

VIEWS = ("overview", "map", "map-graph", "cpu", "health", "ai", "settings", "layout")
THEMES = (("auto", "a"), ("dark", "d"), ("light", "l"), ("high-contrast", "h"))
DENSITIES = (("wall", "w"), ("desk", "k"), ("compact", "c"))
# the graph is paused: its script reloads the page when the refresh is due, and a reload inside --virtual-time-budget keeps some Chrome
# builds (154 on the CI runner) from ever finishing the dump; paused, the same script runs (drawing, physics) and only the reload is off
VIEW_QUERY = {"overview": "", "map": "&view=map", "map-graph": "&view=map&as=graph&pause=1", "cpu": "&view=cpu", "health": "&view=health",
              "ai": "&view=ai", "settings": "&view=settings", "layout": "&edit=1"}
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
    "layout": (("the layout editor", ("data-edit",)), ("a card", ('<article class="card',))),
    "wall": (("the key figures", ('class="kpis"',)), ("a card", ('<article class="card',))),
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
