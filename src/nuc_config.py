"""Configuration shared by collector and renderer (stdlib only, Python 3.8+).

File: $NUC_CONSOLE_CONFIG, else /etc/nuc-console/config.ini (Windows: %ProgramData%\\nuc-console\\config.ini;
portable run: $NUC_CONSOLE_HOME/config.ini, with the state and the baseline in that folder too).
A missing file means defaults (everything on).
A broken file never stops the dashboard: the problem goes to stderr and defaults apply for the bad keys.
"""
import configparser
import json
import os
import re
import stat
import sys
import threading

WINDOWS, MACOS = sys.platform == "win32", sys.platform == "darwin"
LINUX = not (WINDOWS or MACOS)
OS_NAME = "windows" if WINDOWS else "darwin" if MACOS else "linux"  # written in the state files: the renderer reads it
VERSION = "2.4.0"  # this release: the release workflow refuses a tag that does not match it, nuc-console-update compares it
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
TELEGRAM_WEB = "web.json"  # in NOTIFY_DIR: what the web view's Telegram page chose (on/off, the paired @username); written by the notifier

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


def load(path=None, warn=None, overlay=True):
    """-> {"features": {name: bool}, "mode": str, "rotate_seconds": int, ...}. warn: what to do with each complaint about the file (a line of
    text): printed to stderr unless given (the settings page collects them to refuse a value before it writes it).
    overlay: what an installation's settings page chose (settings.ini, "The overlay" below) is laid over the file before it is read, when
    the file is this process's config.ini (True; a portable run has none); a path = that overlay file; False = config.ini alone."""
    path = path or os.environ.get("NUC_CONSOLE_CONFIG", DEFAULT_PATH)
    say = warn or _report(lambda line: print(line, file=sys.stderr))
    cfg = {"features": {f: True for f in FEATURES}, "mode": "overview", "rotate_seconds": 15, "refresh_seconds": 2, "columns": 0, "rows": 0, "spacing": 1, "details": True, "overview_seconds": 45, "map_in_rotation": False, "cpu_in_rotation": False, "health_in_rotation": False, "sections": list(SECTIONS), "webapps": {},
           "web": {"enabled": False, "bind": "127.0.0.1", "port": 8787, "token_file": "", "columns": 200, "rows": 60,
                   "refresh_seconds": 2, "allowed_hosts": [], "settings_actions": True},
           "display": {"browser": "auto", "mode": "browser", "zoom": 100},
           "ai": {"enabled": False, "endpoint": "http://127.0.0.1:11434/v1", "model": "", "allow_remote": False, "timeout_s": 120,
                  "daily": False, "gpu": "auto", "web_actions": True},
           "console": {"font": "", "blank_minutes": 0}}
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
    cfg["expose"] = {}  # [expose]: key -> the widest reach intended (the group names of exposure.GROUPS); here so the early returns have it
    cfg["overlaid"] = {}  # "section.key" -> value, for each key the overlay set (the settings page marks them)
    cfg["config_error"] = ""  # set when a file that exists cannot be read: the defaults are in use and render says so (config-unreadable)
    cfg["telegram"] = {"enabled": False, "username": "", "detail": "titles", "resolved": True, "web_actions": True}  # notify.py; the bot token is never here
    cfg["ui"] = {"web": "app", "sections": list(SECTIONS)}  # [ui] (prefs.parse_ui): the web flag, the section order and only the keys the file sets
    try:
        if not cp.read(path, encoding="utf-8-sig"):  # UTF-8 on every OS (Windows would assume cp1252); Notepad may add a BOM
            if os.path.exists(path):  # read() ignores a file it cannot open: that is not "no config.ini"
                cfg["config_error"] = "cannot open " + path[-150:]
            return cfg
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        say(f"nuc-console: cannot read {path}: {e}")
        cfg["config_error"] = str(e)[:200]
        return cfg
    ov = overlay_for(path, overlay)
    if ov:  # a lock in config.ini (read before anything is laid over it) says whether the page's choices count at all
        try:
            locked = not cp.getboolean("web", "settings_actions", fallback=True)
        except ValueError:
            locked = False
        if not locked:
            cfg["overlaid"] = lay_overlay(cp, ov, say)
    for key in cp["features"] if cp.has_section("features") else ():
        if key not in FEATURES:
            say(f"nuc-console: {path}: unknown feature '{key}' ignored")
            continue
        try:
            cfg["features"][key] = cp.getboolean("features", key)
        except ValueError:
            say(f"nuc-console: {path}: [features] {key} is not a boolean: kept on")
    if cp.has_section("dashboard"):
        mode = cp.get("dashboard", "mode", fallback="overview").strip().lower()
        if mode in MODES:
            cfg["mode"] = mode
        else:
            say(f"nuc-console: {path}: [dashboard] mode must be one of {MODES}")
        for key, lo, hi in (("rotate_seconds", 3, 600), ("columns", 40, 500), ("rows", 10, 200), ("spacing", 0, 1), ("overview_seconds", 10, 600)):
            try:
                v = cp.getint("dashboard", key, fallback=cfg[key])
                cfg[key] = 0 if key != "rotate_seconds" and v == 0 else max(lo, min(hi, v))  # 0 = automatic
            except ValueError:
                say(f"nuc-console: {path}: [dashboard] {key} must be an integer")
    refresh = lambda sec: max(REFRESH_MIN, min(REFRESH_MAX, cp.getint(sec, "refresh_seconds")))  # noqa: E731
    dash_refresh, web_refresh = False, None
    if cp.has_section("dashboard") and cp.has_option("dashboard", "refresh_seconds"):
        try:
            cfg["refresh_seconds"], dash_refresh = refresh("dashboard"), True
        except ValueError:
            say(f"nuc-console: {path}: [dashboard] refresh_seconds must be an integer (1-10)")
    for key in ("details", "map_in_rotation", "cpu_in_rotation", "health_in_rotation"):
        if cp.has_section("dashboard") and cp.has_option("dashboard", key):
            try:
                cfg[key] = cp.getboolean("dashboard", key)
            except ValueError:
                say(f"nuc-console: {path}: [dashboard] {key} is not a boolean: kept {'on' if cfg[key] else 'off'}")
    if cp.has_section("dashboard") and cp.has_option("dashboard", "sections"):
        asked = [x.strip().lower() for x in cp.get("dashboard", "sections").split(",") if x.strip()]
        for x in asked:
            if x not in SECTIONS:
                say(f"nuc-console: {path}: [dashboard] sections: unknown name '{x}' ignored (known: {', '.join(SECTIONS)})")
        order = [x for i, x in enumerate(asked) if x in SECTIONS and x not in asked[:i]]
        cfg["sections"] = order + [x for x in SECTIONS if x not in order]  # sections you forget keep their default place at the end
    if cp.has_section("webapps"):  # name = port[, port...]: web apps you expect to be reachable (and running)
        for name in cp["webapps"]:
            try:
                ports = [int(x) for x in cp.get("webapps", name).replace(";", ",").split(",") if x.strip()]
            except ValueError:
                say(f"nuc-console: {path}: [webapps] {name}: ports must be integers")
                continue
            if ports and all(0 < p < 65536 for p in ports):
                cfg["webapps"][name] = ports
            else:
                say(f"nuc-console: {path}: [webapps] {name}: invalid port list")
    if cp.has_section("expose"):  # name or port = local|tailnet|lan|internet: the widest reach you intend (more is an ATTENTION problem)
        for key in [k for k in cp.options("expose") if k not in cp.defaults()]:  # a [DEFAULT] key belongs to every section: not a service
            word = cp.get("expose", key).strip().lower()
            try:
                expose_port(key)
            except ValueError:
                say(f"nuc-console: {path}: [expose] {key}: not a valid port (use 8080 or 8080/udp)")
                continue
            if word in EXPOSE_WORDS:
                cfg["expose"][key] = EXPOSE_WORDS[word]
            else:
                say(f"nuc-console: {path}: [expose] {key}: '{word}' is not local, tailnet, lan or internet")
    if cp.has_section("web"):
        w = cfg["web"]
        try:
            w["enabled"] = cp.getboolean("web", "enabled", fallback=False)
        except ValueError:
            say(f"nuc-console: {path}: [web] enabled is not a boolean: kept off")
        try:
            w["settings_actions"] = cp.getboolean("web", "settings_actions", fallback=True)  # no = the settings page changes nothing (a lock)
        except ValueError:
            say(f"nuc-console: {path}: [web] settings_actions is not a boolean: kept on")
        w["bind"] = cp.get("web", "bind", fallback=w["bind"]).strip() or w["bind"]
        w["token_file"] = cp.get("web", "token_file", fallback="").strip()
        w["allowed_hosts"] = [h.strip().lower() for h in cp.get("web", "allowed_hosts", fallback="").split(",") if h.strip()]
        for key, lo, hi in (("port", 1, 65535), ("columns", 60, 300), ("rows", 20, 120)):
            try:
                w[key] = max(lo, min(hi, cp.getint("web", key, fallback=w[key])))
            except ValueError:
                say(f"nuc-console: {path}: [web] {key} must be an integer")
        if cp.has_option("web", "refresh_seconds"):  # the old place of the setting: used while [dashboard] has none
            try:
                web_refresh = refresh("web")
            except ValueError:
                say(f"nuc-console: {path}: [web] refresh_seconds must be an integer (1-10)")
    # one refresh for every screen and page; [web] refresh_seconds (older config files) only for the web pages, as before
    cfg["web"]["refresh_seconds"] = cfg["refresh_seconds"] if dash_refresh or web_refresh is None else web_refresh
    if cp.has_section("telegram"):  # notify.py: ATTENTION changes sent to one Telegram user (docs/TELEGRAM.md)
        t = cfg["telegram"]
        for key in ("enabled", "resolved", "web_actions"):  # web_actions: the web view's Telegram page may pair, switch and test (no = it only shows)
            try:
                t[key] = cp.getboolean("telegram", key, fallback=t[key])
            except ValueError:
                say(f"nuc-console: {path}: [telegram] {key} is not a boolean: kept {'on' if t[key] else 'off'}")
        user = cp.get("telegram", "username", fallback="").strip().lstrip("@").lower()
        if user and not (5 <= len(user) <= 32 and user.isascii() and all(ch.isalnum() or ch == "_" for ch in user)):
            say(f"nuc-console: {path}: [telegram] username must be a Telegram @username (5-32 letters, digits, _)")
        else:
            t["username"] = user
        detail = cp.get("telegram", "detail", fallback=t["detail"]).strip().lower()
        if detail in TELEGRAM_DETAILS:
            t["detail"] = detail
        else:
            say(f"nuc-console: {path}: [telegram] detail must be titles or full: kept {t['detail']}")
    if cp.has_section("display"):  # Windows/macOS: the dashboard in a browser tab or a full-screen window
        b = cp.get("display", "browser", fallback="auto").strip()
        cfg["display"]["browser"] = b if b.lower() not in ("auto", "none", "") else (b.lower() or "auto")
        mode = cp.get("display", "mode", fallback="browser").strip().lower()
        if mode in DISPLAY_MODES:
            cfg["display"]["mode"] = DISPLAY_MODES[mode]
        else:
            say(f"nuc-console: {path}: [display] mode must be browser, fullscreen or none: kept browser")
        try:
            cfg["display"]["zoom"] = max(50, min(200, cp.getint("display", "zoom", fallback=100)))
        except ValueError:
            say(f"nuc-console: {path}: [display] zoom must be an integer (percent)")
    if cp.has_section("ai"):  # the optional local model of the HEALTH screen (docs/HEALTH.md): off unless asked for
        ai = cfg["ai"]
        for key in ("enabled", "allow_remote", "daily", "web_actions"):  # web_actions: the AI screens may download, start and ask (docs/AI.md); no = read-only
            try:
                ai[key] = cp.getboolean("ai", key, fallback=ai[key])
            except ValueError:
                say(f"nuc-console: {path}: [ai] {key} is not a boolean: kept {'on' if ai[key] else 'off'}")
        ai["endpoint"] = cp.get("ai", "endpoint", fallback=ai["endpoint"]).strip() or ai["endpoint"]
        ai["model"] = cp.get("ai", "model", fallback="").strip()
        gpu = cp.get("ai", "gpu", fallback="").strip().lower()  # nuc-console-ai serve: use the GPU when the model fits there (auto) or never (no)
        if gpu in ("auto", "yes", "on", "true", "1"):
            ai["gpu"] = "auto"
        elif gpu in ("no", "off", "false", "0", "none", "cpu"):
            ai["gpu"] = "no"
        elif gpu:
            say(f"nuc-console: {path}: [ai] gpu must be auto or no: kept auto")
        try:
            ai["timeout_s"] = max(10, min(600, cp.getint("ai", "timeout_s", fallback=ai["timeout_s"])))
        except ValueError:
            say(f"nuc-console: {path}: [ai] timeout_s must be an integer (10-600)")
    if cp.has_section("console"):  # Linux text console, read by ttyprep.py before the dashboard starts: both off unless asked for
        font = cp.get("console", "font", fallback="").strip()
        if re.fullmatch(r"[A-Za-z0-9_.+-]*", font):  # a font name or file name for setfont: no path, no spaces
            cfg["console"]["font"] = font
        else:
            say(f"nuc-console: {path}: [console] font must be a font name such as Lat15-TerminusBold32x16: kept off")
        try:
            minutes = cp.getint("console", "blank_minutes", fallback=0)
            if not 0 <= minutes <= 60:
                raise ValueError
            cfg["console"]["blank_minutes"] = minutes
        except ValueError:
            say(f"nuc-console: {path}: [console] blank_minutes must be an integer (0-60): kept off")
    try:  # the preferences of the new interface (prefs.py, docs/CONFIGURATION.md): a bad value costs that key only, and nothing here stops the dashboard
        import prefs  # here, not at the top: prefs reads SECTIONS from this module
        keys = {k: cp.get("ui", k) for k in cp.options("ui") if k not in cp.defaults()} if cp.has_section("ui") else {}  # a [DEFAULT] key is not ours
        cfg["ui"], warnings = prefs.parse_ui(keys, cfg["sections"])
        for w in warnings:
            say(f"nuc-console: {path}: {w}")
    except Exception as e:  # noqa: BLE001  (a bug in the optional interface settings must never take the collector or the screen down)
        say(f"nuc-console: {path}: [ui] ignored: {e}")
    return cfg


def telegram_web(d=None):
    """The valid keys of NOTIFY_DIR/web.json, what the web view's Telegram page chose: {"enabled": bool, "username": str}. The notifier writes it
    (0644) when the page asks; {} when there is none, or when it could have been written by someone else: not a regular file, too big, writable by
    group or others, owned by neither root nor the owner of the folder (the notifier's account). Never raises."""
    path = os.path.join(d or NOTIFY_DIR, TELEGRAM_WEB)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
                return {}
            if not WINDOWS and (st.st_mode & 0o022 or st.st_uid not in (0, os.stat(os.path.dirname(path)).st_uid)):
                return {}
            data = json.loads(f.read(4097).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    out = {}
    if isinstance(data, dict) and data.get("v") == 1:
        if isinstance(data.get("enabled"), bool):
            out["enabled"] = data["enabled"]
        user = data.get("username")
        if isinstance(user, str) and 5 <= len(user) <= 32 and user.isascii() and all(ch.isalnum() or ch == "_" for ch in user):
            out["username"] = user.lower()
    return out


def telegram(cfg, d=None):
    """[telegram] as it is in force: config.ini with what the web view's Telegram page chose (web.json) laid over it. On when config.ini says
    enabled = yes OR the page turned it on (config's yes cannot be turned off from the page); the @username the page paired in place of
    config.ini's. "by" says where "on" comes from: "config", "web" or "". `[telegram] web_actions = no`: config.ini alone. A portable run (the
    desktop app is one) is the same: its notifier is started beside its web view, as the same account."""
    t = dict(cfg["telegram"], by="config" if cfg["telegram"]["enabled"] else "")
    if not t.get("web_actions", True):
        return t
    web = telegram_web(d)
    if web.get("username"):
        t["username"] = web["username"]
    if web.get("enabled") and not t["enabled"]:
        t["enabled"], t["by"] = True, "web"
    return t


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


_SET_LOCK = threading.Lock()  # two threads of one process (two clicks on the settings page) never write the file at once


def _new_value(old, value):
    """The line `old` (`key = value  # comment`) with only its value changed: the key as it was written, and the comment after the value
    kept in its column when there is room."""
    at = old.index("=") + 1
    line = (old[:at].rstrip() + " " + value).rstrip()
    m = re.search(r"\s+([#;].*)$", old[at:])
    if not m:
        return line
    col = at + m.start(1)
    return (line.ljust(col) if len(line) < col else line + "  ") + m.group(1)


def set_key(path, section, key, value):
    """Writes `key = value` in [section] of an ini file, keeping every comment (the one after the old value too) and every other line; adds
    what is missing. The file keeps its permissions. Used by the installers' --display option, the AI setup, the notifier and the settings
    page of a portable run: the admin's other edits are never touched."""
    set_keys(path, section, [(key, value)])


def set_keys(path, section, items, drop=(), check=None):
    """set_key for several keys of one section at once (items: [(key, value)]), and the keys in `drop` removed (their line, not the comments
    around it), in one write. check(tmp_path): called with the new file before it takes the place of the old one; an exception from it (the
    settings page refuses a value that load() complains about) leaves the old file as it was."""
    with _SET_LOCK:
        try:
            with open(path, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            lines, mode = [], None
        out = edit_lines(lines, section, items, drop)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        try:
            if mode is not None:
                os.chmod(tmp, mode)
            if check is not None:
                check(tmp)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def edit_lines(lines, section, items, drop=()):
    """The lines of an ini file with [section]'s keys set (items: [(key, value)], in that order) and the keys of `drop` removed: a key that is
    there gets its new value in place (_new_value: its comment stays), one that is not is added after the section's last non-empty line, and a
    section that is not there is added at the end. Names compare without case, like configparser; a commented-out line is not a key."""
    want = [(k, v) for k, v in items]
    value = {k.lower(): v for k, v in want}
    found, gone = set(), {k.lower() for k in drop}
    sec = section.lower()
    header = lambda ln: ln.strip()[1:ln.strip().index("]")].strip().lower() if ln.strip().startswith("[") and "]" in ln else None  # noqa: E731
    name = lambda ln: ln.split("=", 1)[0].strip().lower() if "=" in ln and not ln.lstrip().startswith(("#", ";")) else None  # noqa: E731
    out, inside, seen = [], False, False

    def rest():  # the keys not found: after the section's last non-empty line
        at = len(out)
        while at and not out[at - 1].strip():
            at -= 1
        out[at:at] = [("%s = %s" % (k, v)).rstrip() for k, v in want if k.lower() not in found]
        found.update(value)
    for ln in lines:
        h = header(ln)
        if h is not None:
            if inside:
                rest()
            inside = h == sec
            seen = seen or inside
        elif inside and name(ln) is not None:
            key = name(ln)
            if key in gone:
                continue
            if key in value:  # every line of it: configparser keeps the last one
                ln = _new_value(ln, value[key])
                found.add(key)
        out.append(ln)
    if inside:
        rest()
    elif not seen and want:
        out += ["", "[%s]" % section] + [("%s = %s" % (k, v)).rstrip() for k, v in want]
    return out


# ---- The overlay: what the settings page of an INSTALLATION changes ------------------------------------------------------------------
# config.ini is root's and the web view runs as an unprivileged account (a deliberate boundary), so the page never writes it. Like the AI and
# Telegram pages it writes a file of its own, settings.ini, in the AI folder (the one folder the installers give that account), and load() lays
# it over config.ini. It is UNTRUSTED input to every reader, root's collector included: only OVERLAY_ALLOW keys count, [features] only to
# switch OFF (what root runs can shrink that way, never grow), any other key is ignored and reported. `rm` of the file resets everything.
OVERLAY_FILE = "settings.ini"
OVERLAY_MAX = 16384  # bytes; a bigger file counts for nothing
OVERLAY_ALLOW = (("features", "*"), ("dashboard", "*"), ("ui", "*"), ("display", "zoom"))  # presentation and non-security switches, nothing else
_REPORTED = set()


def _report(emit):
    """A complaint about the overlay is printed once per process: the collector reads it every cycle and must not flood the journal."""
    def say(line):
        if line not in _REPORTED and len(_REPORTED) < 200:
            _REPORTED.add(line)
            emit(line)
    return say


def overlay_allowed(section, key):
    return (section, "*") in OVERLAY_ALLOW or (section, key) in OVERLAY_ALLOW


def overlay_path():
    """The overlay file of an installation: $NUC_CONSOLE_SETTINGS, else settings.ini in the system-wide AI folder (Linux /var/lib/nuc-console/ai,
    macOS /Library/Application Support/nuc-console/ai, Windows ProgramData's), which the installers hand to the web view's account. "" in a
    portable run: its config.ini is the account's own file and is written directly."""
    if PORTABLE:
        return ""
    if os.environ.get("NUC_CONSOLE_SETTINGS"):
        return os.environ["NUC_CONSOLE_SETTINGS"]
    d = os.path.join(BASE_DIR, "ai") if WINDOWS else "/Library/Application Support/nuc-console/ai" if MACOS else os.path.join(LIB_DIR, "ai")
    return os.path.join(d, OVERLAY_FILE)


def overlay_for(path, overlay=True):
    """The overlay file `load(path, overlay=...)` reads, or "": only for this process's own config.ini unless a path is given."""
    if isinstance(overlay, str):
        return overlay
    if overlay is not True or PORTABLE:
        return ""
    try:
        same = os.path.abspath(path) == os.path.abspath(config_path())
    except (TypeError, ValueError):
        same = False
    return overlay_path() if same else ""


def read_overlay(path=None, warn=None):
    """-> {"section.key": value} the overlay holds, whatever it holds: {} when there is none or it cannot be trusted (not a regular file, not
    opened through a link, too big, writable by group or others, owned by neither root nor the owner of its folder). Never raises. Keys outside
    OVERLAY_ALLOW are dropped (warn says so); [features] only keeps the ones that switch OFF."""
    path = overlay_path() if path is None else path
    say = warn or (lambda line: None)
    if not path:
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > OVERLAY_MAX:
                say(f"nuc-console: {path}: not a regular file of at most {OVERLAY_MAX} bytes: ignored")
                return {}
            if not WINDOWS and (st.st_mode & 0o022 or st.st_uid not in (0, os.stat(os.path.dirname(os.path.abspath(path))).st_uid)):
                say(f"nuc-console: {path}: writable by others or owned by someone else: ignored")
                return {}
            text = f.read(OVERLAY_MAX + 1).decode("utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):  # a link (O_NOFOLLOW), a permission, bad bytes
        say(f"nuc-console: {path}: cannot be read as a settings file: ignored")
        return {}
    cp = configparser.ConfigParser(interpolation=None, strict=False, default_section="\0default")  # a [DEFAULT] is a section like any other
    try:
        cp.read_string(text)
    except (configparser.Error, RecursionError):
        say(f"nuc-console: {path}: not a valid ini file: ignored")
        return {}
    out = {}
    for sec in cp.sections():
        for key in cp.options(sec):
            val = cp.get(sec, key, raw=True).strip()
            name = f"{sec.lower()}.{key}"
            if not overlay_allowed(sec.lower(), key) or len(val) > 300:
                say(f"nuc-console: {path}: {name} is not for the settings page: ignored")
            elif sec.lower() == "features" and (key not in FEATURES or val.lower() != "no"):
                say(f"nuc-console: {path}: {name} can only switch a feature off here: ignored")
            else:
                out[name] = val
    return out


def lay_overlay(cp, path, say):
    """The overlay's values set in `cp` (a ConfigParser that has read config.ini); -> {"section.key": value} laid over."""
    laid = {}
    for name, val in read_overlay(path, say).items():
        sec, _, key = name.partition(".")
        if not cp.has_section(sec):
            cp.add_section(sec)
        cp.set(sec, key, val)
        laid[name] = val
    return laid


def _write_overlay(values, path, check=None):
    """settings.ini with `values` ({"section.key": value}): written aside (created new, mode 0640, never through a link), checked, then put in
    place with os.replace. Nothing in it: the file is removed. Raises OSError (not writable, a link in the way) or what check raises."""
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.islink(folder) or not os.path.isdir(folder):
        raise OSError("the settings folder %s is not a plain folder" % folder)
    if not values:
        if check is not None:
            check("")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    lines, last = ["# nuc-console: what the web view's settings page chose. config.ini wins for the keys it locks; `rm` this file to undo it all.", ""], None
    for name in sorted(values):
        sec, _, key = name.partition(".")
        if sec != last:
            lines += ([""] if last else []) + ["[%s]" % sec]
            last = sec
        lines.append(("%s = %s" % (key, values[name])).rstrip())
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > OVERLAY_MAX:
        raise OSError("too many settings")
    tmp = path + ".tmp"
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o640)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if WINDOWS:  # no mode bits: SYSTEM and Administrators write, the web view's account too, everyone else reads; fail closed
            icacls = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "icacls.exe")
            who = (os.environ.get("USERDOMAIN", "") + "\\" if os.environ.get("USERDOMAIN") else "") + os.environ.get("USERNAME", "")
            grants = ["*S-1-5-18:F", "*S-1-5-32-544:F", "*S-1-5-32-545:R"] + ([who + ":M"] if who else [])
            import subprocess
            if subprocess.run([icacls, tmp, "/inheritance:r", "/grant:r"] + grants, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                raise OSError("could not restrict %s (icacls)" % tmp)
        if check is not None:
            check(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def update_overlay(put=None, drop=(), path=None, check=None):
    """Change the overlay: `put` ({"section.key": value}) set, `drop` (names, or "*" for all) removed. A name outside OVERLAY_ALLOW is a
    ValueError, a [features] value that is not "no" too. check(tmp_path) as in set_keys: it sees the new file before it takes the old one's place."""
    path = path or overlay_path()
    if not path:
        raise OSError("this run has no settings overlay")
    put = put or {}
    for name, val in put.items():
        sec, _, key = name.partition(".")
        if not overlay_allowed(sec, key) or (sec == "features" and (key not in FEATURES or val != "no")):
            raise ValueError("not for the settings page: %s" % name[:60])
    with _SET_LOCK:
        cur = read_overlay(path)
        for name in ([n for n in cur] if "*" in drop else drop):
            cur.pop(name, None)
        cur.update(put)
        _write_overlay(cur, path, check)


def config_path():
    """The config.ini this process reads: $NUC_CONSOLE_CONFIG, else the default (a portable run: the one in its folder)."""
    return os.environ.get("NUC_CONSOLE_CONFIG", DEFAULT_PATH)


def features_writable(path=None):
    """(True, "") when the settings page may switch [features]: a portable run (the desktop app is one), whose config.ini is this account's own
    file in its own folder; else (False, why). An installation's config.ini is the administrator's, and the web view never writes it."""
    path = path or config_path()
    if not PORTABLE:
        return False, "installed: config.ini is the administrator's"
    folder = os.path.dirname(os.path.abspath(path))
    if not os.access(folder, os.W_OK) or (os.path.exists(path) and not os.access(path, os.W_OK)):
        return False, "config.ini cannot be written by this account"
    return True, ""


def settings_mode(path=None):
    """How the settings page may change things here: ("file", "") a portable run's own config.ini; ("overlay", "") an installation, through the
    overlay (settings.ini) when its folder is this account's to write and config.ini does not lock it ([web] settings_actions); else ("", why)."""
    if features_writable(path)[0]:
        return "file", ""
    ov = overlay_path()
    if not ov:
        return "", "config.ini cannot be written by this account"
    if not current()["web"].get("settings_actions", True):
        return "", "locked by config.ini ([web] settings_actions = no)"
    folder = os.path.dirname(ov)
    if os.path.islink(folder) or not os.path.isdir(folder) or (not WINDOWS and not os.access(folder, os.W_OK | os.X_OK)):
        return "", "the settings folder is not writable by this account"
    return "overlay", ""


def set_feature(name, on, path=None):
    """[features] name = yes | no in this process's config.ini (set_key: the rest of the file as it was), and in current() at once. The collector
    reads it again within a cycle (collector.reload_features). ValueError for a name that is not a feature. In an installation (settings_mode
    "overlay") off goes in the overlay and on removes that entry; on when config.ini says no is a ValueError: the page can only switch off."""
    if name not in FEATURES:
        raise ValueError("not a feature: %r" % (name,))
    if settings_mode(path)[0] == "overlay":
        if on and not load(path or config_path(), overlay=False)["features"][name]:
            raise ValueError("config.ini switches %s off: only the administrator can switch it on" % name)
        update_overlay({} if on else {"features." + name: "no"}, ["features." + name] if on else ())
        current()["features"][name] = bool(on)
        current()["overlaid"].pop("features." + name, None)
        if not on:
            current()["overlaid"]["features." + name] = "no"
        return
    set_key(path or config_path(), "features", name, "yes" if on else "no")
    current()["features"][name] = bool(on)


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
