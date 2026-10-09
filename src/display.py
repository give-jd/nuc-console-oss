"""nuc-console kiosk plumbing: the browser behind `render.py --kiosk` and `--open` (macOS/Windows have no text console to take over, the monitor
shows the dashboard in a full-screen browser). Where the per-user files go, which browser, the command that starts it, whether the local web
view answers, the URL to open. The entry points (`render.kiosk`, `render.open_in_browser`, `render.kiosk_file`) stay in render.py: the file
fallback draws frames. Stdlib only."""
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import nuc_config

LINUX, WINDOWS, MACOS = nuc_config.LINUX, nuc_config.WINDOWS, nuc_config.MACOS
if not LINUX:
    import hostinfo
CFG = nuc_config.current()


def user_dir():
    """Per-user folder for the kiosk page and the browser profile (the kiosk runs as the logged-in user)."""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif MACOS:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "nuc-console")


def kiosk_grid():
    """(columns, rows) of the kiosk screen: [dashboard] columns/rows if set, else from the monitor's aspect ratio.

    The page scales its font so the grid fills the screen; 64 rows of text, as many columns as the shape allows
    (16:9 -> 237 columns, the 3-column layout; 16:10 -> 213 columns, 2 columns)."""
    rows = CFG["rows"] or 64
    if CFG["columns"]:
        return CFG["columns"], rows
    size = hostinfo.screen_size() if not LINUX else None
    aspect = size[0] / size[1] if size else 16 / 9
    return max(100, min(400, round(rows * aspect * 1.25 / 0.6))), rows


BROWSERS = {
    "windows": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles%\Mozilla Firefox\firefox.exe", r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
                r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"],  # Firefox last: it cannot start full screen (see browser_command)
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "/Applications/Chromium.app/Contents/MacOS/Chromium"],
    "linux": ["chromium", "chromium-browser", "google-chrome", "microsoft-edge", "brave-browser", "firefox"],
}


def find_browser(choice=None):
    """Path of the browser that will show the kiosk: [display] browser in config.ini, else the first one installed."""
    choice = choice or CFG["display"]["browser"]
    if choice == "none":
        return None
    if choice != "auto":
        return choice
    for cand in BROWSERS[nuc_config.OS_NAME]:
        path = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), cand)
        found = shutil.which(path) if LINUX else (path if os.path.isfile(path) else None)
        if found:
            return found
    return ""  # none found: macOS falls back on Safari through `open`


# shown in the kiosk's footer: the page has nothing to click, the keyboard is the way out
KIOSK_HINT = "Cmd+Q closes · Ctrl+Cmd+F leaves full screen" if MACOS else "Alt+F4 closes · F11 leaves full screen"


def browser_command(exe, url, profile):
    """A full-screen app window (no tabs, no address bar), no first-run pages, a profile of its own (never the user's tabs
    and logins). Not the browsers' locked "kiosk" mode: that one swallows Alt+F4 & co. and the screen could not be closed."""
    if exe == "" and MACOS:
        return ["/usr/bin/open", "-a", "Safari", url]  # Safari has no full-screen flag: Ctrl+Cmd+F once
    if not exe:
        return None
    if "firefox" in os.path.basename(exe).lower():
        return [exe, "--new-window", url]  # Firefox: only its locked kiosk mode starts full screen; F11 instead
    return [exe, "--app=" + url, "--start-fullscreen", "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--noerrdialogs", "--user-data-dir=" + profile]


def kiosk_command(exe, target, base):
    """browser_command() for the kiosk, and the log line of a browser that cannot start full screen (Firefox)."""
    cmd = browser_command(exe, target, os.path.join(base, "browser"))
    if cmd and "firefox" in os.path.basename(exe).lower():
        print("Firefox does not start full screen: press F11 in its window", file=sys.stderr, flush=True)
    return cmd


def default_browser(exe, target):
    """Windows, none of BROWSERS found (find_browser() said ""; not `[display] browser = none`): the default browser, in a normal window
    (no flag can make it full screen: F11). True once opened."""
    if exe != "" or not WINDOWS:
        return False
    try:
        os.startfile(target)  # as this user, like open_in_browser
    except OSError as e:
        print("no supported browser found for full screen, and the default browser did not open:", repr(e)[:200], file=sys.stderr, flush=True)
        return False
    print("no supported browser found for full screen: opened the default browser (F11 for full screen)", file=sys.stderr, flush=True)
    return True


def launch(cmd):
    """Starts the browser, detached from our console (its output is not ours to show)."""
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_text_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # no CRLF translation on Windows
        f.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:  # Windows: the browser is reading the previous frame right now
            time.sleep(0.05)
    return False


def web_up(port, wait):
    """True once the local web view answers; at login it may still be starting (it starts at boot)."""
    end = time.time() + wait
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                conn.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                if b" 200 " in conn.recv(64):
                    return True
        except OSError:
            pass
        if time.time() >= end:
            return False
        time.sleep(2)


def dashboard_url(fullscreen=False, cols=0, rows=0):
    """The page the display opens. [ui] web = classic: the classic page (fit to the window; full screen: sized to the grid, the pages taking
    turns, the kiosk footer). [ui] web = app: the shell (app=1); full screen: its wall display (ui=1.dw: wall density; kiosk=1: scrolls by
    itself, says how to close the window). Both take the token the caller appends."""
    app = (CFG.get("ui") or {}).get("web") == "app"
    if app:
        query = {"app": 1, "ui": "1.dw", "kiosk": 1} if fullscreen else {"app": 1}
    else:
        query = {"fit": 1, "cols": cols, "rows": rows, "rotate": 1, "kiosk": 1} if fullscreen else {"fit": 1}
    from urllib.parse import urlencode
    return f"http://127.0.0.1:{CFG['web']['port']}/?" + urlencode(query)


def read_web_token(path):
    """(the token in [web] token_file, "") if this user can read it, else ("", why). The text never holds the token."""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read(512).strip()
    except (OSError, ValueError) as e:  # not there, not allowed (the service user's 0600 file), not text
        return "", f"{path} cannot be read by this user ({e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__})"
    if not re.fullmatch(r"[A-Za-z0-9._~-]{16,}", token):  # what the web view accepts (src/webhttp.py TOKEN_OK): safe in a URL, too
        return "", f"{path} does not hold a token the web view accepts"
    return token, ""
