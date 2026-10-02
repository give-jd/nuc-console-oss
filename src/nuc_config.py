"""Configuration shared by collector and renderer (stdlib only, Python 3.8+).

File: $NUC_CONSOLE_CONFIG, else /etc/nuc-console/config.ini (Windows: %ProgramData%\\nuc-console\\config.ini;
portable run: $NUC_CONSOLE_HOME/config.ini, with the state and the baseline in that folder too).
A missing file means defaults (everything on).
A broken file never stops the dashboard: the problem goes to stderr and defaults apply for the bad keys.
"""
import configparser
import os
import sys
import threading

WINDOWS, MACOS = sys.platform == "win32", sys.platform == "darwin"
LINUX = not (WINDOWS or MACOS)
OS_NAME = "windows" if WINDOWS else "darwin" if MACOS else "linux"  # written in the state files: the renderer reads it
VERSION = "1.5.0"  # this release: the release workflow refuses a tag that does not match it, nuc-console-update compares it
PORTABLE = os.environ.get("NUC_CONSOLE_HOME", "")  # portable run (run.sh / run.cmd): config, state and baseline in that folder
if PORTABLE:
    BASE_DIR = os.path.abspath(PORTABLE)
    ETC_DIR, RUN_DIR, LIB_DIR = BASE_DIR, os.path.join(BASE_DIR, "run"), os.path.join(BASE_DIR, "lib")
elif WINDOWS:  # one root for config, state and logs; install-windows.ps1 restricts writing to SYSTEM and Administrators
    BASE_DIR = os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "nuc-console")
    ETC_DIR, RUN_DIR, LIB_DIR = BASE_DIR, os.path.join(BASE_DIR, "run"), os.path.join(BASE_DIR, "lib")
else:  # macOS uses the Linux paths, except the runtime directory (no /run there)
    ETC_DIR, RUN_DIR, LIB_DIR = "/etc/nuc-console", "/var/run/nuc-console" if MACOS else "/run/nuc-console", "/var/lib/nuc-console"
DEFAULT_PATH = os.path.join(ETC_DIR, "config.ini")
FEATURES = ("containers", "databases", "exposure", "webapps", "firewall", "fail2ban", "tailscale", "boot", "docker_disk",
            "network_traffic", "sessions", "disks", "thermal", "map", "cpu", "health", "ai")
MODES = ("overview", "rotate")
REFRESH_MIN, REFRESH_MAX = 1, 10  # seconds between two redraws ([dashboard] refresh_seconds): every screen and page
# macOS/Windows: how the dashboard is shown ([display] mode). 'kiosk' is accepted for the full-screen window.
DISPLAY_MODES = {"browser": "browser", "fullscreen": "fullscreen", "kiosk": "fullscreen", "none": "none", "no": "none", "off": "none"}
# Fixed on-screen order of the overview sections (most important first: what needs action, then security posture,
# then resources, workloads, history, then detail panels). Overridable with [dashboard] sections.
SECTIONS = ("attention", "exposure", "webapps", "firewall", "system", "containers", "databases", "boot", "network_traffic", "sessions",
            "tailscale", "docker_disk", "disks")
# notify.py (Telegram): its own folder, owned by the unprivileged service user: bot token, paired chat (0600), status.json (0644)
NOTIFY_DIR = os.environ.get("NUC_CONSOLE_NOTIFY_DIR") or (os.path.join(BASE_DIR, "notify") if WINDOWS or PORTABLE else "/var/lib/nuc-console-notify")  # portable: under data/
TELEGRAM_DETAILS = ("titles", "full")  # titles: only the problem's title leaves the machine; full: its text too (names, ports)

# [expose]: the words for a reach, and the group names exposure.py uses for it (exposure.GROUPS); synonyms are accepted
EXPOSE_WORDS = {"local": "LOCALE", "localhost": "LOCALE", "loopback": "LOCALE", "tailnet": "TAILNET", "tailscale": "TAILNET",
                "lan": "LAN", "internet": "INTERNET", "public": "INTERNET"}


def expose_port(key):
    """(port, proto) of an [expose] key that is a port (8080, 8080/udp); None when it is a name. ValueError: a port that cannot exist."""
    head, _, proto = key.partition("/")
    if not (head.isascii() and head.isdigit()):
        return None
    if not 0 < int(head) < 65536 or proto not in ("", "tcp", "udp"):
        raise ValueError(key)
    return int(head), proto or "tcp"


def load(path=None):
    """-> {"features": {name: bool}, "mode": str, "rotate_seconds": int}"""
    path = path or os.environ.get("NUC_CONSOLE_CONFIG", DEFAULT_PATH)
    cfg = {"features": {f: True for f in FEATURES}, "mode": "overview", "rotate_seconds": 15, "refresh_seconds": 2, "columns": 0, "rows": 0, "spacing": 1, "details": True, "overview_seconds": 45, "map_in_rotation": False, "cpu_in_rotation": False, "health_in_rotation": False, "sections": list(SECTIONS), "webapps": {},
           "web": {"enabled": False, "bind": "127.0.0.1", "port": 8787, "token_file": "", "columns": 200, "rows": 60,
                   "refresh_seconds": 2, "allowed_hosts": []},
           "display": {"browser": "auto", "mode": "browser", "zoom": 100},
           "ai": {"enabled": False, "endpoint": "http://127.0.0.1:11434/v1", "model": "", "allow_remote": False, "timeout_s": 120,
                  "daily": False, "gpu": "auto", "web_actions": True}}
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
    cfg["expose"] = {}  # [expose]: key -> the widest reach intended (the group names of exposure.GROUPS); here so the early returns have it
    cfg["config_error"] = ""  # set when a file that exists cannot be read: the defaults are in use and render says so (config-unreadable)
    cfg["telegram"] = {"enabled": False, "username": "", "detail": "titles", "resolved": True}  # notify.py; the bot token is never here
    cfg["ui"] = {"web": "classic", "sections": list(SECTIONS)}  # [ui] (prefs.parse_ui): the web flag, the section order and only the keys the file sets
    try:
        if not cp.read(path, encoding="utf-8-sig"):  # UTF-8 on every OS (Windows would assume cp1252); Notepad may add a BOM
            if os.path.exists(path):  # read() ignores a file it cannot open: that is not "no config.ini"
                cfg["config_error"] = "cannot open " + path[-150:]
            return cfg
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        print(f"nuc-console: cannot read {path}: {e}", file=sys.stderr)
        cfg["config_error"] = str(e)[:200]
        return cfg
    for key in cp["features"] if cp.has_section("features") else ():
        if key not in FEATURES:
            print(f"nuc-console: {path}: unknown feature '{key}' ignored", file=sys.stderr)
            continue
        try:
            cfg["features"][key] = cp.getboolean("features", key)
        except ValueError:
            print(f"nuc-console: {path}: [features] {key} is not a boolean: kept on", file=sys.stderr)
    if cp.has_section("dashboard"):
        mode = cp.get("dashboard", "mode", fallback="overview").strip().lower()
        if mode in MODES:
            cfg["mode"] = mode
        else:
            print(f"nuc-console: {path}: [dashboard] mode must be one of {MODES}", file=sys.stderr)
        for key, lo, hi in (("rotate_seconds", 3, 600), ("columns", 40, 500), ("rows", 10, 200), ("spacing", 0, 1), ("overview_seconds", 10, 600)):
            try:
                v = cp.getint("dashboard", key, fallback=cfg[key])
                cfg[key] = 0 if key != "rotate_seconds" and v == 0 else max(lo, min(hi, v))  # 0 = automatic
            except ValueError:
                print(f"nuc-console: {path}: [dashboard] {key} must be an integer", file=sys.stderr)
    refresh = lambda sec: max(REFRESH_MIN, min(REFRESH_MAX, cp.getint(sec, "refresh_seconds")))  # noqa: E731
    dash_refresh, web_refresh = False, None
    if cp.has_section("dashboard") and cp.has_option("dashboard", "refresh_seconds"):
        try:
            cfg["refresh_seconds"], dash_refresh = refresh("dashboard"), True
        except ValueError:
            print(f"nuc-console: {path}: [dashboard] refresh_seconds must be an integer (1-10)", file=sys.stderr)
    for key in ("details", "map_in_rotation", "cpu_in_rotation", "health_in_rotation"):
        if cp.has_section("dashboard") and cp.has_option("dashboard", key):
            try:
                cfg[key] = cp.getboolean("dashboard", key)
            except ValueError:
                print(f"nuc-console: {path}: [dashboard] {key} is not a boolean: kept {'on' if cfg[key] else 'off'}", file=sys.stderr)
    if cp.has_section("dashboard") and cp.has_option("dashboard", "sections"):
        asked = [x.strip().lower() for x in cp.get("dashboard", "sections").split(",") if x.strip()]
        for x in asked:
            if x not in SECTIONS:
                print(f"nuc-console: {path}: [dashboard] sections: unknown name '{x}' ignored (known: {', '.join(SECTIONS)})", file=sys.stderr)
        order = [x for i, x in enumerate(asked) if x in SECTIONS and x not in asked[:i]]
        cfg["sections"] = order + [x for x in SECTIONS if x not in order]  # sections you forget keep their default place at the end
    if cp.has_section("webapps"):  # name = port[, port...]: web apps you expect to be reachable (and running)
        for name in cp["webapps"]:
            try:
                ports = [int(x) for x in cp.get("webapps", name).replace(";", ",").split(",") if x.strip()]
            except ValueError:
                print(f"nuc-console: {path}: [webapps] {name}: ports must be integers", file=sys.stderr)
                continue
            if ports and all(0 < p < 65536 for p in ports):
                cfg["webapps"][name] = ports
            else:
                print(f"nuc-console: {path}: [webapps] {name}: invalid port list", file=sys.stderr)
    if cp.has_section("expose"):  # name or port = local|tailnet|lan|internet: the widest reach you intend (more is an ATTENTION problem)
        for key in [k for k in cp.options("expose") if k not in cp.defaults()]:  # a [DEFAULT] key belongs to every section: not a service
            word = cp.get("expose", key).strip().lower()
            try:
                expose_port(key)
            except ValueError:
                print(f"nuc-console: {path}: [expose] {key}: not a valid port (use 8080 or 8080/udp)", file=sys.stderr)
                continue
            if word in EXPOSE_WORDS:
                cfg["expose"][key] = EXPOSE_WORDS[word]
            else:
                print(f"nuc-console: {path}: [expose] {key}: '{word}' is not local, tailnet, lan or internet", file=sys.stderr)
    if cp.has_section("web"):
        w = cfg["web"]
        try:
            w["enabled"] = cp.getboolean("web", "enabled", fallback=False)
        except ValueError:
            print(f"nuc-console: {path}: [web] enabled is not a boolean: kept off", file=sys.stderr)
        w["bind"] = cp.get("web", "bind", fallback=w["bind"]).strip() or w["bind"]
        w["token_file"] = cp.get("web", "token_file", fallback="").strip()
        w["allowed_hosts"] = [h.strip().lower() for h in cp.get("web", "allowed_hosts", fallback="").split(",") if h.strip()]
        for key, lo, hi in (("port", 1, 65535), ("columns", 60, 300), ("rows", 20, 120)):
            try:
                w[key] = max(lo, min(hi, cp.getint("web", key, fallback=w[key])))
            except ValueError:
                print(f"nuc-console: {path}: [web] {key} must be an integer", file=sys.stderr)
        if cp.has_option("web", "refresh_seconds"):  # the old place of the setting: used while [dashboard] has none
            try:
                web_refresh = refresh("web")
            except ValueError:
                print(f"nuc-console: {path}: [web] refresh_seconds must be an integer (1-10)", file=sys.stderr)
    # one refresh for every screen and page; [web] refresh_seconds (older config files) only for the web pages, as before
    cfg["web"]["refresh_seconds"] = cfg["refresh_seconds"] if dash_refresh or web_refresh is None else web_refresh
    if cp.has_section("telegram"):  # notify.py: ATTENTION changes sent to one Telegram user (docs/TELEGRAM.md)
        t = cfg["telegram"]
        for key in ("enabled", "resolved"):
            try:
                t[key] = cp.getboolean("telegram", key, fallback=t[key])
            except ValueError:
                print(f"nuc-console: {path}: [telegram] {key} is not a boolean: kept {'on' if t[key] else 'off'}", file=sys.stderr)
        user = cp.get("telegram", "username", fallback="").strip().lstrip("@").lower()
        if user and not (5 <= len(user) <= 32 and user.isascii() and all(ch.isalnum() or ch == "_" for ch in user)):
            print(f"nuc-console: {path}: [telegram] username must be a Telegram @username (5-32 letters, digits, _)", file=sys.stderr)
        else:
            t["username"] = user
        detail = cp.get("telegram", "detail", fallback=t["detail"]).strip().lower()
        if detail in TELEGRAM_DETAILS:
            t["detail"] = detail
        else:
            print(f"nuc-console: {path}: [telegram] detail must be titles or full: kept {t['detail']}", file=sys.stderr)
    if cp.has_section("display"):  # Windows/macOS: the dashboard in a browser tab or a full-screen window
        b = cp.get("display", "browser", fallback="auto").strip()
        cfg["display"]["browser"] = b if b.lower() not in ("auto", "none", "") else (b.lower() or "auto")
        mode = cp.get("display", "mode", fallback="browser").strip().lower()
        if mode in DISPLAY_MODES:
            cfg["display"]["mode"] = DISPLAY_MODES[mode]
        else:
            print(f"nuc-console: {path}: [display] mode must be browser, fullscreen or none: kept browser", file=sys.stderr)
        try:
            cfg["display"]["zoom"] = max(50, min(200, cp.getint("display", "zoom", fallback=100)))
        except ValueError:
            print(f"nuc-console: {path}: [display] zoom must be an integer (percent)", file=sys.stderr)
    if cp.has_section("ai"):  # the optional local model of the HEALTH screen (docs/HEALTH.md): off unless asked for
        ai = cfg["ai"]
        for key in ("enabled", "allow_remote", "daily", "web_actions"):  # web_actions: the AI screens may download, start and ask (docs/AI.md); no = read-only
            try:
                ai[key] = cp.getboolean("ai", key, fallback=ai[key])
            except ValueError:
                print(f"nuc-console: {path}: [ai] {key} is not a boolean: kept {'on' if ai[key] else 'off'}", file=sys.stderr)
        ai["endpoint"] = cp.get("ai", "endpoint", fallback=ai["endpoint"]).strip() or ai["endpoint"]
        ai["model"] = cp.get("ai", "model", fallback="").strip()
        gpu = cp.get("ai", "gpu", fallback="").strip().lower()  # nuc-console-ai serve: use the GPU when the model fits there (auto) or never (no)
        if gpu in ("auto", "yes", "on", "true", "1"):
            ai["gpu"] = "auto"
        elif gpu in ("no", "off", "false", "0", "none", "cpu"):
            ai["gpu"] = "no"
        elif gpu:
            print(f"nuc-console: {path}: [ai] gpu must be auto or no: kept auto", file=sys.stderr)
        try:
            ai["timeout_s"] = max(10, min(600, cp.getint("ai", "timeout_s", fallback=ai["timeout_s"])))
        except ValueError:
            print(f"nuc-console: {path}: [ai] timeout_s must be an integer (10-600)", file=sys.stderr)
    try:  # the preferences of the new interface (prefs.py, docs/CONFIGURATION.md): a bad value costs that key only, and nothing here stops the dashboard
        import prefs  # here, not at the top: prefs reads SECTIONS from this module
        keys = {k: cp.get("ui", k) for k in cp.options("ui") if k not in cp.defaults()} if cp.has_section("ui") else {}  # a [DEFAULT] key is not ours
        cfg["ui"], warnings = prefs.parse_ui(keys, cfg["sections"])
        for w in warnings:
            print(f"nuc-console: {path}: {w}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001  (a bug in the optional interface settings must never take the collector or the screen down)
        print(f"nuc-console: {path}: [ui] ignored: {e}", file=sys.stderr)
    return cfg


_CURRENT, _CURRENT_LOCK = None, threading.Lock()


def current():
    """The configuration of this process: read once, by the first caller, and the very same dict at every call.
    Modules read it through here (render.CFG is this object); a test or a --demo run changes it in place and every module sees it.
    Code that needs the file read again (notify.py's cycle, the AI installer after it wrote a key) calls load()."""
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = load()
        return _CURRENT


def set_key(path, section, key, value):
    """Writes `key = value` in [section] of an ini file, keeping every comment and every other line; adds what is missing.
    Used by the installers' --display option: the admin's other edits are never touched."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    header = lambda ln: ln.strip()[1:ln.strip().index("]")].strip().lower() if ln.strip().startswith("[") and "]" in ln else None  # noqa: E731
    is_key = lambda ln: ln.split("=", 1)[0].strip().lower() == key.lower() and "=" in ln and not ln.lstrip().startswith(("#", ";"))  # noqa: E731
    out, inside, done = [], False, False
    for ln in lines:
        h = header(ln)
        if h is not None:
            if inside and not done:  # leaving the section without the key: add it after its last non-empty line
                at = len(out)
                while at and not out[at - 1].strip():
                    at -= 1
                out.insert(at, f"{key} = {value}")
                done = True
            inside = h == section.lower()
        elif inside and not done and is_key(ln):
            ln, done = f"{key} = {value}", True
        out.append(ln)
    if inside and not done:
        out.append(f"{key} = {value}")
    elif not done:
        out += ["", f"[{section}]", f"{key} = {value}"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":  # for the installers: --get SECTION KEY (the value in force) | --set FILE SECTION KEY VALUE
    if sys.argv[1:2] == ["--get"] and len(sys.argv) == 4:
        print(load()[sys.argv[2]][sys.argv[3]])
    elif sys.argv[1:2] == ["--set"] and len(sys.argv) == 6:
        set_key(*sys.argv[2:6])
    else:
        sys.exit("usage: nuc_config.py --get SECTION KEY | --set FILE SECTION KEY VALUE")


def log_to(path, max_bytes=1 << 20):
    """Send stdout/stderr to a log file (Windows scheduled tasks have no journal). Rotated once at start when over 1 MB."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass
    f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = f
    return f
