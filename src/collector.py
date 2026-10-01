#!/usr/bin/env python3
"""nuc-console collector: runs as root (Windows: SYSTEM), writes the container, network and boot state as JSON.

The renderer (unprivileged user) only reads this JSON: the Docker socket is equivalent to root
and must not live in the process that owns the tty.
Linux sections run the tools below; macOS and Windows sections live in collect_darwin.py and collect_windows.py and write
the same shapes, plus "os" so the renderer knows which firewall model the verdicts come from.
"""
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import nuc_config  # noqa: E402  (same directory)

CFG = nuc_config.load()
OFF = {k for k, v in CFG["features"].items() if not v}
LINUX, MACOS, WINDOWS, OS_NAME = nuc_config.LINUX, nuc_config.MACOS, nuc_config.WINDOWS, nuc_config.OS_NAME
if WINDOWS:
    import collect_windows as cwin
    import winapi
elif MACOS:
    import collect_darwin as cmac
# collected section -> feature that enables it (disabled = like a missing tool: no error, no alarm)
NET_FEATURE = {"listeners": "exposure", "serve": "exposure", "ts_peers": "tailscale", "ufw": "firewall",
               "docker_user": "firewall", "iptables": "firewall", "drops": "firewall", "dbs": "databases",
               "f2b": "fail2ban", "firewall": "firewall"}
BOOT_FEATURE = {"analyze": ("boot",), "blame": ("boot",), "failed": ("boot",), "enabled": ("boot",), "journal": ("boot",),
                "containers": ("boot", "containers"), "docker_df": ("docker_disk",)}
# Linux-only sections: on macOS/Windows they are "not available on this OS" (no error, no alarm)
LINUX_ONLY_NET = ("ufw", "docker_user", "iptables", "drops", "f2b")
UNSUPPORTED_BOOT = {"darwin": ("analyze", "blame", "journal"), "windows": ("blame",)}.get(OS_NAME, ())
OUT = os.path.join(nuc_config.RUN_DIR, "containers.json")
OUT_NET = os.path.join(nuc_config.RUN_DIR, "net.json")
OUT_BOOT = os.path.join(nuc_config.RUN_DIR, "boot.json")
INTERVAL_S = 10
NET_INTERVAL_S = 30
BOOT_INTERVAL_S = 300  # boot does not change: every 5 minutes is enough
if WINDOWS:  # only directories that need Administrator rights to write to
    _root, _pf = os.environ.get("SystemRoot") or r"C:\Windows", os.environ.get("ProgramFiles") or r"C:\Program Files"
    SBIN = os.pathsep.join((os.path.join(_root, "System32", "WindowsPowerShell", "v1.0"), os.path.join(_root, "System32"),
                            os.path.join(_pf, "Docker", "Docker", "resources", "bin"), os.path.join(_pf, "Tailscale")))
elif MACOS:  # Apple's tools first: a same-named binary in /usr/local/bin can never shadow lsof, pfctl & co.
    SBIN = ("/usr/sbin:/usr/bin:/sbin:/bin:/usr/libexec/ApplicationFirewall:/usr/local/bin:/opt/homebrew/bin"
            ":/Applications/Docker.app/Contents/Resources/bin:/Applications/Tailscale.app/Contents/MacOS")
else:
    SBIN = "/usr/sbin:/usr/bin:/sbin:/bin"
APPLE_SYSTEM = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/libexec/", "/System/")  # SIP-protected: safe to run as root
# processes that listen on behalf of containers: docker-proxy (Linux), the Docker Desktop / OrbStack / Rancher backends
DOCKER_PROXIES = {"docker-proxy", "com.docker.backend", "com.docker.vpnkit", "vpnkit", "vpnkit-bridge", "com.docker.proxy",
                  "OrbStack Helper", "limactl", "rancher-desktop"}  # not wslrelay: it forwards any WSL port, not only containers
PROJECT = re.compile(r"(?:^|,)com\.docker\.compose\.project=([^,]+)")
PORT = re.compile(r"^(?:(\[[^\]]+\]|[\d.]+):)?(\d+(?:-\d+)?)->")
DUR = re.compile(r"(\d+(?:\.\d+)?)(us|ms|min|s|h)")
DUR_S = {"us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600}
BLAME = re.compile(r"^\s*((?:\d+(?:\.\d+)?(?:us|ms|min|s|h)\s?)+)\s+(\S+)\s*$")
# The database type is recognised from the image's *repository name* (not the whole string: 'postgrest' or
# 'mongo-express' are not databases). Images identified only by 'sha256:' cannot be recognised.
DB_NAME = re.compile(r"^(?:eclipse-|bitnami-|percona-|apache-)?(postgres|postgresql|pgvector|timescaledb|postgis|mysql|mariadb|mongo|"
                     r"redis|valkey|keydb|dragonfly|couchdb|influxdb|mosquitto|elasticsearch|opensearch|memcached|rabbitmq|"
                     r"clickhouse|cassandra|neo4j|qdrant|meilisearch)(?:-(?:alpine|server|stack|community|enterprise|oss|ce|slim))?$", re.I)
DB_SKIP = re.compile(r"(backup|exporter|admin|proxy|express|commander|insight|-ui$|-gui$|migrat)", re.I)
KIND_MAP = {"postgresql": "postgres", "pgvector": "postgres", "timescaledb": "postgres", "postgis": "postgres"}
HOSTKEY = re.compile(r"(HOST|ADDR|SERVER|ENDPOINT|URL|URI|DSN|CONN)", re.I)
URL_HOST = re.compile(r"://(?:[^/@\s]*@)?([^:/?#\s,;]+)")
EXTERNAL_TTL_S, EXTERNAL_MAX_IPS = 86400, 20  # the memory of external IPs does not grow without bound
SINCE = time.time()
EXTERNAL_SEEN = {}  # DB name -> {ip: last time seen}: the memory lasts as long as the collector process
EXIT_OK = re.compile(r"^Exited \(0\)")
PROC = re.compile(r'\("([^"]+)"')
DROP_SRC, DROP_DPT = re.compile(r"SRC=(\S+)"), re.compile(r"DPT=(\d+)")


def parse_ports(ports):
    """Published ports: scope '*' (all interfaces), 'lo' (loopback) or the specific bind IP.

    '0.0.0.0:8180->8080/tcp, 127.0.0.1:1->2/tcp, 100.1.2.3:9->9/tcp' ->
    [{'p': 8180, 's': '*'}, {'p': 1, 's': 'lo'}, {'p': 9, 's': '100.1.2.3'}]
    """
    seen, out = set(), []
    for item in (ports or "").split(", "):
        m = PORT.match(item)
        if not m:
            continue  # port only exposed (80/tcp), not published
        host, port = m.groups()
        host = (host or "0.0.0.0").strip("[]")
        scope = "*" if host in ("0.0.0.0", "::") else "lo" if host.startswith("127.") or host == "::1" else host
        p = int(port) if port.isdigit() else port
        if (p, scope) not in seen:
            seen.add((p, scope))
            out.append({"p": p, "s": scope})
    return out


def mem_bytes(cid):
    # cgroup v2 with the systemd driver; docker stats costs >2 s per call, this file ~0
    for path in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current",
                 f"/sys/fs/cgroup/docker/{cid}/memory.current"):
        try:
            with open(path) as f:
                return int(f.read())
        except OSError:
            continue
    return None


MEM_MIB = {"B": 1, "KiB": 2 ** 10, "MiB": 2 ** 20, "GiB": 2 ** 30, "TiB": 2 ** 40, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9}


def parse_mem_usage(text):
    """docker stats MemUsage '12.5MiB / 7.66GiB' -> bytes used (None if unreadable)."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B|kB)\b", str(text))
    return int(float(m.group(1)) * MEM_MIB[m.group(2)]) if m and m.group(2) in MEM_MIB else None


_STATS = {"ts": 0, "mem": {}}


def docker_mem():
    """{container id: bytes} from `docker stats` (macOS/Windows: the containers live in a VM, no cgroup files here).
    It costs ~2 s per call: refreshed every 30 s, not at every 10 s container pass."""
    if time.time() - _STATS["ts"] > 30:
        rc, out, _ = run("docker", "stats", "--no-stream", "--no-trunc", "--format", "{{.ID}}\t{{.MemUsage}}", timeout=30)
        if rc == 0:
            _STATS["mem"] = {i: parse_mem_usage(u) for i, _, u in (ln.partition("\t") for ln in out.splitlines()) if i}
        _STATS["ts"] = time.time()
    return _STATS["mem"]


def collect():
    if "containers" in OFF:
        return {"ts": time.time(), "containers": [], "disabled": True}
    # -a: containers that exited with an error (137, 255...) are the signal we need; Exited (0) = one-shot job, dropped
    try:
        rc, out, err = run("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}", timeout=20)
    except Absent:
        return {"ts": time.time(), "containers": [], "absent": True}  # no Docker on this machine: not an error
    if rc != 0:
        return {"ts": time.time(), "error": err[:200] or "docker not responding", "containers": []}
    rows, bad = [], 0
    try:
        mem_of = mem_bytes if LINUX else docker_mem().get
    except Exception:  # noqa: BLE001 - memory is a detail: without it the list still shows (RAM '-')
        mem_of = lambda _cid: None  # noqa: E731
    for line in out.splitlines():
        try:
            d = json.loads(line)
            if EXIT_OK.match(d["Status"]):
                continue
            m = PROJECT.search(d.get("Labels", ""))
            rows.append({"name": d["Names"], "status": d["Status"], "state": d["State"],
                         "project": m.group(1) if m else "", "ports": parse_ports(d.get("Ports")),
                         "mem": mem_of(d["ID"])})
        except (ValueError, KeyError):
            bad += 1  # one unreadable line must not empty the whole list
    out = {"ts": time.time(), "containers": rows}
    if bad:
        out["error"] = f"{bad} unreadable docker line" + ("" if bad == 1 else "s")
    return out


class Absent(Exception):
    """The tool is not installed: on another machine that is normal, not a fault (no error, no alarm)."""


def mac_identity(exe):
    """macOS: None = run as root (Apple's SIP-protected tools); else the user a third-party tool must run as.

    docker, tailscale... live in user-writable places (/usr/local/bin, apps dragged to /Applications): run as root, a
    replaced binary would be root code. They run as their owner, or as the user at the console when root owns them
    (Docker Desktop and the Tailscale app run in that user's session anyway)."""
    if os.geteuid() != 0:
        return None
    real = os.path.realpath(exe)
    if real.startswith(APPLE_SYSTEM):
        return None
    uid = os.stat(real).st_uid or os.stat("/dev/console").st_uid
    if uid == 0:
        raise RuntimeError(f"{os.path.basename(real)}: nobody logged in at the console, not run as root")
    import pwd
    return pwd.getpwuid(uid)


def _as_user(pw):
    env = {"PATH": SBIN, "HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "TMPDIR": "/tmp", "LANG": "C"}
    if sys.version_info >= (3, 9):
        return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": [], "env": env}

    def drop():  # Python 3.8: no user= argument
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
    return {"preexec_fn": drop, "env": env}


def run(name, *args, timeout=15):
    """(rc, stdout, stderr); rc=None on timeout. Raises Absent if the binary does not exist."""
    exe = shutil.which(name, path=SBIN)
    if not exe:
        raise Absent(name)
    kw = {}
    if MACOS:
        pw = mac_identity(exe)
        if pw:
            kw = _as_user(pw)
    elif WINDOWS:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        r = subprocess.run([exe, *args], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", repr(e)[:120]
    return r.returncode, r.stdout, r.stderr.strip()[:120]


def parse_ss(text):
    """ss -tulnpH -> [{'proto','addr','port','proc'}]; '*' and '::' stay wildcards."""
    out = []
    for line in text.splitlines():
        p = line.split(None, 6)
        if len(p) < 5:
            continue
        addr, _, port = p[4].rpartition(":")
        if not port.isdigit():
            continue
        m = PROC.search(p[6]) if len(p) > 6 else None
        out.append({"proto": p[0], "addr": addr.strip("[]").split("%")[0],
                    "port": int(port), "proc": m.group(1) if m else ""})
    return out


def parse_serve(text):
    """tailscale serve status --json -> [{'port','path','target','funnel'}].

    Reads Web (http/https), TCP with TCPForward and Foreground sessions; does not read Services (VIP service).
    """
    def entries(d):
        funnel = {k for k, v in (d.get("AllowFunnel") or {}).items() if v}
        out = []
        for hostport, web in (d.get("Web") or {}).items():
            port = hostport.rpartition(":")[2]
            for path, h in (web.get("Handlers") or {}).items():
                out.append({"port": int(port) if port.isdigit() else 443, "path": path,
                            "target": h.get("Proxy") or h.get("Path") or "", "funnel": hostport in funnel})
        for port, cfg in (d.get("TCP") or {}).items():
            if cfg.get("TCPForward") and port.isdigit():
                out.append({"port": int(port), "path": "tcp", "target": cfg["TCPForward"],
                            "funnel": any(k.rpartition(":")[2] == port for k in funnel)})
        return out

    d = json.loads(text or "{}")
    out = entries(d)
    for fg in (d.get("Foreground") or {}).values():
        out += entries(fg or {})
    return out


UFW_RULE = re.compile(r"^(?P<to>.+?)\s+(?P<action>(?:ALLOW|DENY|REJECT|LIMIT)(?: (?:IN|OUT|FWD))?)\s+(?P<from>.*?)(?:\s+#.*)?$")


def parse_ufw(text):
    d = {"active": False, "default": "", "logging": "", "rules": []}
    in_rules = False
    for line in text.splitlines():
        if line.startswith("Status:"):
            d["active"] = line.split(":", 1)[1].strip() == "active"
        elif line.startswith("Logging:"):
            d["logging"] = line.split(":", 1)[1].strip()
        elif line.startswith("Default:"):
            d["default"] = line.split(":", 1)[1].strip()
        elif line.startswith("--"):
            in_rules = True
        elif in_rules and line.strip():
            # do not split by columns (2+ spaces): a long 'To' column (e.g. 'Anywhere (v6) on tailscale0') leaves no
            # double spaces and would shift every field. Recognise the action and take what is before and after it.
            m = UFW_RULE.match(line.strip())
            if m:
                d["rules"].append({"to": m.group("to"), "action": m.group("action"), "from": m.group("from")})
    return d


def parse_docker_user(text):
    """Rules in DOCKER-USER apart from the bare RETURN that Docker inserts by itself.

    A RETURN with conditions (-s ... -j RETURN) is a real rule and stays: it may precede a DROP.
    """
    return [ln for ln in text.splitlines() if ln.startswith("-A DOCKER-USER") and ln != "-A DOCKER-USER -j RETURN"]


def parse_iptables(text):
    """iptables -S -> policy per chain, rules per chain, and whether tailscaled accepts tailscale0 (ts-input)."""
    policy, count, ts_input = {}, {}, False
    for ln in text.splitlines():
        p = ln.split()
        if ln.startswith("-P ") and len(p) >= 3:
            policy[p[1]] = p[2]
        elif ln.startswith("-A ") and len(p) >= 2:
            count[p[1]] = count.get(p[1], 0) + 1
            ts_input = ts_input or (p[1] == "ts-input" and "tailscale0" in ln and ln.endswith("ACCEPT"))
    return {"policy": policy, "count": count, "ts_input": ts_input}


def parse_ts_peers(text):
    """tailscale status --json -> {'self': {...}, 'peers': [{'name','os','online','last_seen','direct','relay','exit'}]}.

    'direct' = there is a direct address (CurAddr); otherwise traffic goes through the relay. 'last_seen' is None if never seen.
    """
    d = json.loads(text)

    def seen(v):
        if not v or v.startswith("0001-"):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    me = d.get("Self") or {}
    peers = [{"name": p.get("HostName", "?"), "os": p.get("OS", ""), "online": bool(p.get("Online")),
              "last_seen": seen(p.get("LastSeen")), "direct": bool(p.get("CurAddr")), "relay": p.get("Relay", ""),
              "exit": bool(p.get("ExitNode")), "exit_option": bool(p.get("ExitNodeOption"))} for p in (d.get("Peer") or {}).values()]
    return {"self": {"name": me.get("HostName", "?"), "online": bool(me.get("Online")), "exit_option": bool(me.get("ExitNodeOption"))},
            "peers": sorted(peers, key=lambda x: (not x["online"], x["name"].lower()))}


SIZE_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def parse_size(text):
    """'1.68GB' -> bytes (docker prints decimal units); 0 if it cannot be read."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKMGT]?B)\s*", str(text))
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)]) if m and m.group(2) in SIZE_UNITS else 0


ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")


def parse_docker_df(text):
    """docker system df --format '{{json .}}' -> [{'type','count','active','size','reclaimable'}] (strings as docker gives them)."""
    out = []
    for ln in text.splitlines():
        try:
            d = json.loads(ln)
            out.append({"type": d["Type"], "count": d["TotalCount"], "active": d["Active"], "size": d["Size"],
                        "reclaimable": d["Reclaimable"]})
        except (ValueError, KeyError, TypeError, AttributeError):  # non-JSON lines, missing fields, or JSON that is not an object
            continue
    return out


def parse_drops(text):
    srcs, ports = {}, {}
    n = 0
    for line in text.splitlines():
        if "UFW BLOCK" not in line:
            continue
        n += 1
        for rx, acc in ((DROP_SRC, srcs), (DROP_DPT, ports)):
            m = rx.search(line)
            if m:
                acc[m.group(1)] = acc.get(m.group(1), 0) + 1
    top = lambda d: sorted(d.items(), key=lambda kv: -kv[1])[:5]
    return {"n": n, "src": top(srcs), "dpt": top(ports)}


def f2b_jails():
    rc, out, err = run("fail2ban-client", "status")
    if rc != 0:
        return {"error": err or "fail2ban not responding", "jails": []}
    m = re.search(r"Jail list:\s*(.*)", out)
    jails = []
    for j in [x.strip() for x in (m.group(1) if m else "").split(",") if x.strip()]:
        rc, o, _ = run("fail2ban-client", "status", j)
        n = re.search(r"Currently banned:\s*(\d+)", o)
        ips = re.search(r"Banned IP list:\s*(.*)", o)
        jails.append({"name": j, "banned": int(n.group(1)) if n else 0,
                      "ips": (ips.group(1).split() if ips else [])[:5]})
    return {"jails": jails}


def parse_dur(text):
    """'1min 5.2s' -> 65.2 (systemd-analyze format)."""
    return sum(float(v) * DUR_S[u] for v, u in DUR.findall(text))


def parse_analyze(text):
    """'Startup finished in 6.7s (firmware) + ... = 31.2s' -> {'parts': {...}, 'total': s}"""
    line = next((ln for ln in text.splitlines() if ln.startswith("Startup finished")), "")
    parts = {name: parse_dur(t) for t, name in re.findall(r"((?:[\d.]+(?:us|ms|min|s|h)\s*)+)\((\w+)\)", line)}
    m = re.search(r"=\s*(.+)$", line)
    return {"parts": parts, "total": parse_dur(m.group(1)) if m else sum(parts.values())}


def parse_blame(text, n=12):
    rows = [(parse_dur(m.group(1)), m.group(2)) for ln in text.splitlines() if (m := BLAME.match(ln))]
    # .device units are waiting for hardware, not work done by a service: noise in this ranking
    return [{"unit": u, "s": round(sec, 3)} for sec, u in rows if not u.endswith(".device")][:n]


def parse_failed(text):
    return [ln.replace("●", "").split()[0] for ln in text.splitlines() if ln.replace("●", "").strip()]


def parse_journal(text, cap=500):
    """journalctl -o json (warning and worse) -> counts and the noisiest identifiers."""
    idents, err, warn, n = {}, 0, 0, 0
    for ln in text.splitlines():
        try:
            d = json.loads(ln)
            pr = int(d.get("PRIORITY", 4))
        except (ValueError, TypeError):
            continue
        msg = d.get("MESSAGE")
        if isinstance(msg, str) and "[UFW " in msg:
            continue  # packets blocked by ufw are firewall logs (counted separately as 'drop'), not system errors
        n += 1
        err, warn = (err + 1, warn) if pr <= 3 else (err, warn + 1)
        e = idents.setdefault(d.get("SYSLOG_IDENTIFIER", "?"), {"n": 0, "pr": 7, "last": ""})
        e["n"] += 1
        e["pr"] = min(e["pr"], pr)
        e["last"] = msg[:120] if isinstance(msg, str) else "<binary message>"  # journald: non-UTF8 = list of bytes
    top = sorted(({"id": k, **v} for k, v in idents.items()), key=lambda x: (x["pr"], -x["n"]))[:10]
    return {"err": err, "warn": warn, "capped": n >= cap, "top": top}


def parse_iso(text):
    """Docker StartedAt '2026-09-29T10:00:00.123456789Z' -> epoch."""
    base, _, frac = text.rstrip("Z").partition(".")
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + (float("0." + frac[:9]) if frac else 0)


def os_release():
    """What the BOOT section prints next to the boot time: the kernel on Linux, the OS version elsewhere."""
    if MACOS:
        return f"macOS {platform.mac_ver()[0]}"
    if WINDOWS:
        return f"Windows {platform.release()} ({platform.version()})"
    return platform.release()


class NotRecorded(Exception):
    """The OS keeps no record of it this time (e.g. Windows boot time without its diagnostics event): info, not an error."""


def boot_time():
    if MACOS:
        rc, out, err = run("sysctl", "-n", "kern.boottime")
        if rc != 0:
            raise RuntimeError(err or "sysctl failed")
        m = re.search(r"sec = (\d+)", out)
        return int(m.group(1)) if m else int(time.time())
    if WINDOWS:
        return int(time.time() - winapi.uptime())
    with open("/proc/stat") as f:
        return next(int(ln.split()[1]) for ln in f if ln.startswith("btime"))


def collect_boot():
    d = {"ts": time.time(), "errors": {}, "absent": [], "kernel": os_release(), "os": OS_NAME}

    def section(key, fn):
        if OFF.intersection(BOOT_FEATURE.get(key, ())):
            d["absent"].append(key)
            d.setdefault("disabled", []).append(key)
            return
        if key in UNSUPPORTED_BOOT:
            d["absent"].append(key)
            d.setdefault("unsupported", []).append(key)
            return
        try:
            d[key] = fn()
        except Absent:
            d["absent"].append(key)
        except NotRecorded as e:
            d["absent"].append(key)
            d.setdefault("notes", {})[key] = str(e)
        except Exception as e:  # noqa: BLE001 - a broken section does not empty the others
            d["errors"][key] = repr(e)[:120]

    native = {}  # the macOS/Windows boot data comes from one call shared by several sections

    def from_native(key):
        def fn():
            if "data" not in native:
                try:
                    native["data"] = (cwin.boot_sections(cwin.powershell(run, cwin.PS_BOOT, timeout=90)) if WINDOWS
                                      else dict(zip(("enabled", "failed"), cmac.daemons(run))))
                except Exception as e:  # noqa: BLE001 - remembered: every section of this pass reports the same failure
                    native["data"] = e
            if isinstance(native["data"], Exception):
                raise native["data"]
            if native["data"].get(key) is None:
                raise NotRecorded("not recorded for this boot (Diagnostics-Performance log)")
            return native["data"][key]
        return fn

    def need(rc_ok, err, what):
        if not rc_ok:
            raise RuntimeError(err or f"{what} failed")

    def analyze():
        rc, out, err = run("systemd-analyze")
        need(rc == 0, err, "systemd-analyze")
        return parse_analyze(out)

    def blame():
        rc, out, err = run("systemd-analyze", "blame", "--no-pager")
        need(rc == 0, err, "systemd-analyze blame")
        return parse_blame(out)

    def failed():
        rc, out, err = run("systemctl", "--failed", "--no-legend", "--plain")
        need(rc == 0, err, "systemctl --failed")
        return parse_failed(out)

    def enabled():
        rc, out, err = run("systemctl", "list-unit-files", "--type=service", "--state=enabled", "--no-legend", "--plain")
        need(rc == 0, err, "systemctl list-unit-files")
        names = [ln.split()[0] for ln in out.splitlines() if ln.strip() and "@." not in ln.split()[0]]
        _, states, _ = run("systemctl", "is-active", *names)  # rc != 0 if any is inactive: the output is still valid
        return [{"unit": u, "state": st} for u, st in zip(names, states.split())]

    def journal():
        rc, out, err = run("journalctl", "-b", "-p", "warning", "-o", "json", "-n", "500", "--no-pager",
                           "--output-fields=SYSLOG_IDENTIFIER,MESSAGE,PRIORITY", timeout=30)
        need(rc in (0, 1), err, "journalctl")
        return parse_journal(out)

    def containers():
        rc, out, err = run("docker", "ps", "-q")
        need(rc == 0, err, "docker ps")
        ids = out.split()
        if not ids:
            return []
        rc, out, err = run("docker", "inspect", "--format",
                           "{{.Name}}|{{.State.StartedAt}}|{{.HostConfig.RestartPolicy.Name}}", *ids)
        need(rc == 0, err, "docker inspect")
        rows = []
        for ln in out.splitlines():
            name, started, policy = ln.split("|")
            rows.append({"name": name.lstrip("/"), "started": parse_iso(started), "restart": policy or "no"})
        return rows

    def docker_df():
        rc, out, err = run("docker", "system", "df", "--format", "{{json .}}", timeout=60)
        need(rc == 0, err, "docker system df")
        rows = parse_docker_df(out)
        rc, dangling, _ = run("docker", "volume", "ls", "-q", "-f", "dangling=true", timeout=30)
        unused = dangling.split() if rc == 0 else None
        rc, dimg, _ = run("docker", "image", "ls", "-f", "dangling=true", "--format", "{{.Size}}", timeout=30)
        return {"rows": rows, "volumes_unused": len(unused) if unused is not None else None,
                "volumes_unused_anonymous": sum(bool(ANON_VOLUME.match(v)) for v in unused) if unused is not None else None,
                "dangling_images": {"count": len(dimg.split()), "bytes": sum(parse_size(x) for x in dimg.split())} if rc == 0 else None}

    d["btime"] = boot_time()
    if not LINUX:  # same sections, native sources; 'blame' (and on macOS 'analyze', 'journal') stay unsupported
        analyze, failed, enabled, journal = (from_native(k) for k in ("analyze", "failed", "enabled", "journal"))
    for key, fn in (("analyze", analyze), ("blame", blame), ("failed", failed), ("enabled", enabled),
                    ("journal", journal), ("containers", containers), ("docker_df", docker_df)):
        section(key, fn)
    return d


def db_kind(image):
    """'pgvector/pgvector:pg16' -> 'postgres'; None if it is not a database (or is an accessory of one: backup, admin...)."""
    base = image.rsplit("/", 1)[-1].split(":")[0].split("@")[0]
    m = DB_NAME.match(base)
    if not m or DB_SKIP.search(base):
        return None
    return KIND_MAP.get(m.group(1).lower(), m.group(1).lower())


def env_uses(aliases, key, value):
    """True if the environment variable points at the database: host of a URL, or a *HOST/*ADDR/*URL... variable with
    the alias. The type (DB_TYPE=postgres) or the scheme (postgres://otherhost) do not count. The value is never stored."""
    v = value.strip()
    if "://" in v:
        return any(h.lower() in aliases for h in URL_HOST.findall(v))
    if HOSTKEY.search(key):
        return re.split(r"[:/]", v, 1)[0].lower() in aliases
    return False


def parse_established(text):
    """ss -tnH [-p] state established -> [{'l_addr','l_port','p_addr','p_port','proc'}]"""
    out = []
    for ln in text.splitlines():
        p = ln.split(None, 4)
        if len(p) < 4:
            continue
        la, _, lp = p[2].rpartition(":")
        pa, _, pp = p[3].rpartition(":")
        if lp.isdigit() and pp.isdigit():
            m = PROC.search(p[4]) if len(p) > 4 else None
            out.append({"l_addr": la.strip("[]"), "l_port": int(lp), "p_addr": pa.strip("[]"), "p_port": int(pp),
                        "proc": m.group(1) if m else ""})
    return out


def container_conns(pid):
    """Established connections seen *inside* the container's network namespace: external clients show up with their real
    IP (from the host, Docker's DNAT hides them from ss). Needs root. None if unreadable."""
    try:
        rc, out, _ = run("nsenter", "-t", str(pid), "-n", "ss", "-tnH", "state", "established", timeout=5)
    except Absent:
        return None
    return parse_established(out) if rc == 0 else None


def is_local(addr, local_addrs):
    try:
        ip = ipaddress.ip_address(addr.strip("[]").split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_loopback or str(ip) in local_addrs


def ports_of(d):
    """*Real* published ports (NetworkSettings.Ports: includes -P and random ports), not only the requested ones."""
    binds = {k: v for k, v in (d["NetworkSettings"].get("Ports") or {}).items() if v} \
        or {k: v for k, v in (d["HostConfig"].get("PortBindings") or {}).items() if v}
    seen, out = set(), []
    for cport, bl in binds.items():
        for b in bl or []:
            ip, hp = b.get("HostIp") or "0.0.0.0", b.get("HostPort") or ""
            scope = "lo" if ip.startswith("127.") or ip == "::1" else "*" if ip in ("0.0.0.0", "::") else ip
            if (cport, hp, scope) not in seen:  # v4 and v6 of the same binding count once
                seen.add((cport, hp, scope))
                out.append({"p": int(hp) if hp.isdigit() else 0, "c": cport, "s": scope})
    return out


def db_items(inspected, host_conns, conn_fn=None, local_addrs=(), now=None):
    """List of running databases: ports, who uses them (runtime evidence + evidence from env) and external clients.

    - 'active': containers connected *now*, seen inside the database's namespace (conn_fn) and recognised by IP;
    - 'usano' ("use it"): the service name appears as a host in one of their environment variables (presence only, never values);
    - 'stessa_rete' ("same network"): reachable but no evidence. For containers on the default bridge only, nothing more is known;
    - 'external': non-local IPs seen connected (sampled every 30 s: shorter connections are not seen);
      'ext_source' is 'netns' if detectable, otherwise 'n/d' (host network or unreadable namespace): no false reassurance.
    """
    now = now or time.time()
    by_name = {d["Name"].lstrip("/"): d for d in inspected}
    ip_owner, gateways = {}, set()
    for n, d in by_name.items():
        for net in d["NetworkSettings"]["Networks"].values():
            if net.get("IPAddress"):
                ip_owner[net["IPAddress"]] = n
            if net.get("Gateway"):
                gateways.add(net["Gateway"])
    items = []
    for name, d in sorted(by_name.items()):
        kind = db_kind(d["Config"]["Image"])
        if not kind:
            continue
        labels = d["Config"].get("Labels") or {}
        host_net = d["HostConfig"].get("NetworkMode") == "host"
        ports = [{"p": 0, "c": "host", "s": "*"}] if host_net else ports_of(d)
        nets = {n for n in d["NetworkSettings"]["Networks"] if n not in ("bridge", "host", "none")}
        aliases = {a.lower() for n in nets for a in (d["NetworkSettings"]["Networks"][n].get("Aliases") or [])
                   if not re.fullmatch(r"[0-9a-f]{12}", a)} | {name.lower()}
        aliases.add(labels.get("com.docker.compose.service", name).lower())
        aliases.discard("")
        peers = [o for o, od in by_name.items()
                 if o != name and nets & set(od["NetworkSettings"]["Networks"]) and not db_kind(od["Config"]["Image"])]
        usano = sorted(o for o in peers if any(env_uses(aliases, e.split("=", 1)[0], e.split("=", 1)[-1])
                                               for e in by_name[o]["Config"].get("Env") or [] if "=" in e))
        cports = {int(c.split("/")[0]) for c in list(d["Config"].get("ExposedPorts") or {}) + [x["c"] for x in ports]
                  if c.split("/")[0].isdigit()}
        conns = conn_fn(d["State"].get("Pid")) if conn_fn and not host_net and d["State"].get("Pid") else None
        active, ext = set(), EXTERNAL_SEEN.setdefault(name, {})
        for c in conns or []:
            if c["l_port"] not in cports:
                continue
            peer = c["p_addr"]
            if peer in ip_owner:
                active.add(ip_owner[peer])
            elif peer in gateways or is_local(peer, local_addrs):
                continue  # from the bridge gateway = a host process (via docker-proxy): see host_clients below
            else:
                ext[peer] = now
        hp = {x["p"] for x in ports if x["p"]}
        host_clients = sorted({c["proc"] for c in host_conns
                               if c["p_port"] in hp and c["proc"] and c["proc"] not in DOCKER_PROXIES
                               and is_local(c["p_addr"], local_addrs)})
        for ip in [i for i, t in ext.items() if now - t > EXTERNAL_TTL_S]:
            del ext[ip]
        for ip in sorted(ext, key=ext.get)[:-EXTERNAL_MAX_IPS]:
            del ext[ip]
        items.append({"name": name, "kind": kind, "image": d["Config"]["Image"],
                      "project": labels.get("com.docker.compose.project", ""), "state": d["State"]["Status"],
                      "ports": ports, "host_net": host_net, "nets": sorted(nets), "active": sorted(active),
                      "usano": usano, "stessa_rete": sorted(set(peers) - set(usano) - active), "host_clients": host_clients,
                      "external": [{"ip": ip, "last": t} for ip, t in sorted(ext.items(), key=lambda kv: -kv[1])],
                      "ext_source": "netns" if conns is not None else "n/d"})
    for gone in set(EXTERNAL_SEEN) - {i["name"] for i in items}:
        del EXTERNAL_SEEN[gone]  # containers gone: no orphan keys
    return {"since": SINCE, "items": items}


def collect_net():
    """Each section fails on its own: a broken command does not empty the others."""
    d = {"ts": time.time(), "errors": {}, "absent": [], "os": OS_NAME}

    def section(key, fn):
        if NET_FEATURE.get(key) in OFF:
            d["absent"].append(key)
            d.setdefault("disabled", []).append(key)
            return
        if not LINUX and key in LINUX_ONLY_NET:
            d["absent"].append(key)
            d.setdefault("unsupported", []).append(key)
            return
        try:
            d[key] = fn()
        except Absent:
            d["absent"].append(key)  # tool not installed: the renderer says so, but it is neither an error nor an alarm
        except Exception as e:  # noqa: BLE001 - any unexpected output ends up in errors, not in a crash
            d["errors"][key] = repr(e)[:120]

    fw = {}  # macOS/Windows: the firewall state the listener verdicts are computed from

    def firewall():
        if WINDOWS:
            fw["win"] = cwin.powershell(run, cwin.PS_FIREWALL)
            return cwin.fw_summary(fw["win"])
        fw["alf"], fw["pf"] = cmac.firewall(run)
        return cmac.fw_summary(fw["alf"], fw["pf"])

    def listeners():
        if WINDOWS:
            out = cwin.listeners(fw.get("win"))
        elif MACOS:
            out = cmac.listeners(run, fw.get("alf"), fw.get("pf"))
        else:
            rc, out, err = run("ss", "-tulnpH")
            if rc != 0:
                raise RuntimeError(err or "ss failed")
            return parse_ss(out)
        if "firewall" in OFF:  # nothing was read: say so, never a verdict
            for x in out:
                x["fw"] = ["unknown", "firewall check off in config.ini"]
        return out

    def serve():
        rc, out, err = run("tailscale", "serve", "status", "--json")
        if rc != 0:
            raise RuntimeError(err or "tailscale failed")
        return parse_serve(out)

    def ufw():
        rc, out, err = run("ufw", "status", "verbose")
        if rc != 0:
            raise RuntimeError(err or "ufw failed")
        d = parse_ufw(out)
        d["raw"] = out[:200]  # to see at a glance whether the parser read it right
        return d

    def iptables():
        rc, out, err = run("iptables", "-S")
        if rc != 0:
            raise RuntimeError(err or "iptables failed")
        return parse_iptables(out)

    def docker_user():
        rc, out, err = run("iptables", "-S", "DOCKER-USER")
        if rc != 0:
            raise RuntimeError(err or "iptables failed")
        return parse_docker_user(out)

    def drops():
        # rc 1 = no lines found: not an error
        rc, out, err = run("journalctl", "-k", "--since", "-1h", "-o", "cat", "-g", "UFW BLOCK",
                           "-n", "5000", "--no-pager", timeout=20)
        if rc not in (0, 1):
            raise RuntimeError(err or "journalctl failed")
        return parse_drops(out)

    def dbs():
        rc, out, err = run("docker", "ps", "-q")
        if rc != 0:
            raise RuntimeError(err or "docker ps failed")
        ids = out.split()
        insp = "[]"
        if ids:
            # a container may vanish between ps and inspect: rc != 0 but the output for the others is valid
            rc, insp, err = run("docker", "inspect", *ids)
            if rc != 0 and not insp.strip().startswith("["):
                raise RuntimeError(err or "docker inspect failed")
        if WINDOWS:  # Docker Desktop: the containers live in a VM, their namespaces are out of reach (no 'who connects')
            return db_items(json.loads(insp or "[]"), cwin.established(), None, cwin.local_addrs())
        if MACOS:
            return db_items(json.loads(insp or "[]"), cmac.established(run), None, cmac.local_addrs(run))
        rc, est, err = run("ss", "-tnpH", "state", "established")
        if rc != 0:
            raise RuntimeError(err or "ss failed")
        try:
            _, hostip, _ = run("hostname", "-I")
        except Absent:
            hostip = ""
        return db_items(json.loads(insp or "[]"), parse_established(est), container_conns, set(hostip.split()))

    def ts_peers():
        rc, out, err = run("tailscale", "status", "--json")
        if rc != 0:
            raise RuntimeError(err or "tailscale failed")
        return parse_ts_peers(out)

    if not LINUX:
        section("firewall", firewall)  # before the listeners: their verdicts depend on it
    section("listeners", listeners)
    section("serve", serve)
    section("ufw", ufw)
    section("docker_user", docker_user)
    section("iptables", iptables)
    section("dbs", dbs)
    section("ts_peers", ts_peers)
    section("f2b", f2b_jails)
    section("drops", drops)
    return d


def write_atomic(data, path=OUT):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: the renderer has the file open this very moment; it closes it in milliseconds
            if not WINDOWS or attempt == 19:
                raise
            time.sleep(0.05)


def net_loop():
    while True:
        try:
            write_atomic(collect_net(), OUT_NET)
        except Exception as e:  # noqa: BLE001 - the thread must not die: the file goes stale and the renderer flags it
            print("collect_net:", repr(e), file=sys.stderr)
        time.sleep(NET_INTERVAL_S)


def boot_loop():
    while True:
        try:
            write_atomic(collect_boot(), OUT_BOOT)
        except Exception as e:  # noqa: BLE001
            print("collect_boot:", repr(e), file=sys.stderr)
        time.sleep(BOOT_INTERVAL_S)


def main():
    if "--log" in sys.argv[:-1]:  # Windows scheduled task: no journal, the log goes to %ProgramData%\nuc-console\logs
        nuc_config.log_to(sys.argv[sys.argv.index("--log") + 1])
    if WINDOWS:
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # never look for docker.exe & co. in the working directory
    if "--once" in sys.argv:
        print(json.dumps({"containers": collect(), "net": collect_net(), "boot": None if {"boot", "docker_disk"} <= OFF else collect_boot()}, indent=1))
        return
    os.makedirs(nuc_config.RUN_DIR, exist_ok=True)  # systemd creates it (RuntimeDirectory); launchd and Task Scheduler do not
    if not {"boot", "docker_disk"} <= OFF:
        threading.Thread(target=boot_loop, daemon=True).start()
    # separate thread: slow ufw/iptables/fail2ban must not stop the container refresh
    threading.Thread(target=net_loop, daemon=True).start()
    while True:
        try:
            data = collect()
        except Exception as e:  # docker down or unexpected output: the state carries the error, the loop goes on
            data = {"ts": time.time(), "error": repr(e)[:200], "containers": []}
        write_atomic(data)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
