"""Configuration shared by collector and renderer (stdlib only, Python 3.8+).

File: $NUC_CONSOLE_CONFIG, else /etc/nuc-console/config.ini (Windows: %ProgramData%\\nuc-console\\config.ini).
A missing file means defaults (everything on).
A broken file never stops the dashboard: the problem goes to stderr and defaults apply for the bad keys.
"""
import configparser
import os
import sys

WINDOWS, MACOS = sys.platform == "win32", sys.platform == "darwin"
LINUX = not (WINDOWS or MACOS)
OS_NAME = "windows" if WINDOWS else "darwin" if MACOS else "linux"  # written in the state files: the renderer reads it
if WINDOWS:  # one root for config, state and logs; install-windows.ps1 restricts writing to SYSTEM and Administrators
    BASE_DIR = os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "nuc-console")
    ETC_DIR, RUN_DIR, LIB_DIR = BASE_DIR, os.path.join(BASE_DIR, "run"), os.path.join(BASE_DIR, "lib")
else:  # macOS uses the Linux paths, except the runtime directory (no /run there)
    ETC_DIR, RUN_DIR, LIB_DIR = "/etc/nuc-console", "/var/run/nuc-console" if MACOS else "/run/nuc-console", "/var/lib/nuc-console"
DEFAULT_PATH = os.path.join(ETC_DIR, "config.ini")
FEATURES = ("containers", "databases", "exposure", "webapps", "firewall", "fail2ban", "tailscale", "boot", "docker_disk",
            "network_traffic", "sessions", "disks", "thermal")
MODES = ("overview", "rotate")
# Fixed on-screen order of the overview sections (most important first: what needs action, then security posture,
# then resources, workloads, history, then detail panels). Overridable with [dashboard] sections.
SECTIONS = ("attention", "exposure", "webapps", "firewall", "system", "containers", "databases", "boot", "network_traffic", "sessions",
            "tailscale", "docker_disk", "disks")


def load(path=None):
    """-> {"features": {name: bool}, "mode": str, "rotate_seconds": int}"""
    path = path or os.environ.get("NUC_CONSOLE_CONFIG", DEFAULT_PATH)
    cfg = {"features": {f: True for f in FEATURES}, "mode": "overview", "rotate_seconds": 15, "columns": 0, "rows": 0, "spacing": 1, "details": True, "overview_seconds": 45, "sections": list(SECTIONS), "webapps": {},
           "web": {"enabled": False, "bind": "127.0.0.1", "port": 8787, "token_file": "", "columns": 200, "rows": 60,
                   "refresh_seconds": 5, "allowed_hosts": []},
           "display": {"browser": "auto"}}
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        if not cp.read(path, encoding="utf-8-sig"):  # UTF-8 on every OS (Windows would assume cp1252); Notepad may add a BOM
            return cfg
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        print(f"nuc-console: cannot read {path}: {e}", file=sys.stderr)
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
    if cp.has_section("dashboard") and cp.has_option("dashboard", "details"):
        try:
            cfg["details"] = cp.getboolean("dashboard", "details")
        except ValueError:
            print(f"nuc-console: {path}: [dashboard] details is not a boolean: kept on", file=sys.stderr)
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
    if cp.has_section("web"):
        w = cfg["web"]
        try:
            w["enabled"] = cp.getboolean("web", "enabled", fallback=False)
        except ValueError:
            print(f"nuc-console: {path}: [web] enabled is not a boolean: kept off", file=sys.stderr)
        w["bind"] = cp.get("web", "bind", fallback=w["bind"]).strip() or w["bind"]
        w["token_file"] = cp.get("web", "token_file", fallback="").strip()
        w["allowed_hosts"] = [h.strip().lower() for h in cp.get("web", "allowed_hosts", fallback="").split(",") if h.strip()]
        for key, lo, hi in (("port", 1, 65535), ("columns", 60, 300), ("rows", 20, 120), ("refresh_seconds", 2, 300)):
            try:
                w[key] = max(lo, min(hi, cp.getint("web", key, fallback=w[key])))
            except ValueError:
                print(f"nuc-console: {path}: [web] {key} must be an integer", file=sys.stderr)
    if cp.has_section("display"):  # Windows/macOS: the monitor shows the screen in a full-screen browser (render.py --kiosk)
        b = cp.get("display", "browser", fallback="auto").strip()
        cfg["display"]["browser"] = b if b.lower() not in ("auto", "none", "") else (b.lower() or "auto")
    return cfg


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
