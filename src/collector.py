#!/usr/bin/env python3
"""nuc-console collector: runs as root (Windows: SYSTEM), writes the container, network and boot state as JSON.

The renderer (unprivileged user) only reads this JSON: the Docker socket is equivalent to root
and must not live in the process that owns the tty.
Linux sections run the tools below; macOS and Windows sections live in collect_darwin.py and collect_windows.py and write
the same shapes, plus "os" so the renderer knows which firewall model the verdicts come from. On macOS and Windows it also
writes the CPU temperature (sensors.json): only root/SYSTEM can read it there; on Linux the renderer reads sysfs itself.
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

import history  # noqa: E402  (same directory)
import nuc_config  # noqa: E402

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
               "f2b": "fail2ban", "firewall": "firewall", "links": "map"}
BOOT_FEATURE = {"analyze": ("boot",), "blame": ("boot",), "failed": ("boot",), "deps": ("boot",), "enabled": ("boot",), "journal": ("boot",),
                "containers": ("boot", "containers"), "docker_df": ("docker_disk",)}
# Linux-only sections: on macOS/Windows they are "not available on this OS" (no error, no alarm)
LINUX_ONLY_NET = ("ufw", "docker_user", "iptables", "drops", "f2b")
UNSUPPORTED_BOOT = {"darwin": ("analyze", "blame", "journal", "deps"), "windows": ("blame",)}.get(OS_NAME, ())
OUT = os.path.join(nuc_config.RUN_DIR, "containers.json")
OUT_NET = os.path.join(nuc_config.RUN_DIR, "net.json")
OUT_BOOT = os.path.join(nuc_config.RUN_DIR, "boot.json")
OUT_SENSORS = os.path.join(nuc_config.RUN_DIR, "sensors.json")
INTERVAL_S = 10
NET_INTERVAL_S = 30
BOOT_INTERVAL_S = 300  # boot does not change: every 5 minutes is enough
SENSORS_INTERVAL_S = 30 if WINDOWS else 10  # Windows starts PowerShell for each reading: not more often than that
TEMP_RANGE_C = (-20, 150)  # a CPU sensor outside it is broken, not hot or cold
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
PID = re.compile(r"\bpid=(\d+)")
LINKS_TTL_S, LINKS_MAX, LINKS_MAX_EXT = 86400, 400, 20  # the memory of connections: 24 h, 400 edges, 20 outside peers per node
LINKS_SEEN = {}  # (from, to, port) -> last time seen: like EXTERNAL_SEEN, it lasts as long as the collector process
WILDCARD = ("0.0.0.0", "::", "*", "")
# the hard reverse dependencies: the units that Require=, Requisite= or BindsTo= it, i.e. what breaks if it is down.
# Not WantedBy (a target that only wants it keeps going), PartOf (the other way round) or UpheldBy (it only restarts it)
REVERSE_DEPS = ("RequiredBy", "RequisiteOf", "BoundBy")
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
    """ss -tulnpH -> [{'proto','addr','port','proc','pid'}]; '*' and '::' stay wildcards. pid (None if not shown) is for
    the collector only: it names the systemd unit, then it is dropped (it changes at every restart)."""
    out = []
    for line in text.splitlines():
        p = line.split(None, 6)
        if len(p) < 5:
            continue
        addr, _, port = p[4].rpartition(":")
        if not port.isdigit():
            continue
        m = PROC.search(p[6]) if len(p) > 6 else None
        pid = PID.search(p[6]) if len(p) > 6 else None
        out.append({"proto": p[0], "addr": addr.strip("[]").split("%")[0],
                    "port": int(port), "proc": m.group(1) if m else "", "pid": int(pid.group(1)) if pid else None})
    return out


def unit_of_cgroup(text):
    """/proc/PID/cgroup -> 'ssh.service': the systemd service the process runs in; None in a session or container scope.

    cgroup v2 '0::/system.slice/ssh.service'; v1 the 'name=systemd' line. The leaf decides: from the innermost cgroup up,
    the first '.service' wins and a '.scope'/'.slice' ends the search (a desktop app is not its user manager's service)."""
    paths = []
    for ln in text.splitlines():
        _, _, rest = ln.partition(":")
        ctrl, _, path = rest.partition(":")
        if path:  # the systemd hierarchy first
            paths.insert(0 if ctrl in ("", "name=systemd") else len(paths), path)
    for path in paths[:1]:
        for part in reversed([x for x in path.split("/") if x]):
            if part.endswith(".service"):
                return part
            if part.endswith((".scope", ".slice")):
                return None
    return None


def unit_of_pid(pid):
    try:
        with open(f"/proc/{int(pid)}/cgroup") as f:
            return unit_of_cgroup(f.read())
    except (OSError, ValueError):
        return None


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
    """tailscale status --json -> {'self': {...}, 'peers': [{'name','os','online','last_seen','direct','relay','exit','ips'}]}.

    'direct' = there is a direct address (CurAddr); otherwise traffic goes through the relay. 'last_seen' is None if never seen.
    'ips' (TailscaleIPs) lets the map name a tailnet client seen connected.
    """
    d = json.loads(text)

    def seen(v):
        if not v or v.startswith("0001-"):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def ips(p):
        return [x for x in p.get("TailscaleIPs") or [] if isinstance(x, str)]

    me = d.get("Self") or {}
    peers = [{"name": p.get("HostName", "?"), "os": p.get("OS", ""), "online": bool(p.get("Online")),
              "last_seen": seen(p.get("LastSeen")), "direct": bool(p.get("CurAddr")), "relay": p.get("Relay", ""),
              "exit": bool(p.get("ExitNode")), "exit_option": bool(p.get("ExitNodeOption")), "ips": ips(p)}
             for p in (d.get("Peer") or {}).values()]
    return {"self": {"name": me.get("HostName", "?"), "online": bool(me.get("Online")), "exit_option": bool(me.get("ExitNodeOption")),
                     "ips": ips(me)},
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


def parse_reverse_deps(text):
    """`systemctl show -p RequiredBy -p RequisiteOf -p BoundBy UNIT` -> the units that need it (first level only), sorted.

    The hard dependencies of `systemctl list-dependencies --reverse`, machine-readable: its --plain output is flat on
    recent systemd (every level at the same indent), so the first level could not be told from the targets it expands.
    WantedBy is not read: one failed service wanted by multi-user.target does not break the target."""
    out = set()
    for ln in text.splitlines():
        key, _, val = ln.partition("=")
        if key.strip() in REVERSE_DEPS:
            out.update(val.split())
    return sorted(out)


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

    def deps():
        """What each failed unit leaves without its dependency. One call per unit: one failure costs only its own key."""
        if not isinstance(d.get("failed"), list):
            raise Absent("failed units unknown")
        out = {}
        for unit in d["failed"][:20]:
            props = [a for k in REVERSE_DEPS for a in ("-p", k)]
            try:
                rc, text, _ = run("systemctl", "show", "--no-pager", *props, "--", unit)  # '--': '-.mount' is a unit, not an option
            except Exception:  # noqa: BLE001 - per unit: the others still answer
                continue
            if rc == 0:
                out[unit] = parse_reverse_deps(text)[:20]
        return out

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
    if not LINUX:  # same sections, native sources; 'blame' (and on macOS 'analyze', 'journal', 'deps') stay unsupported
        analyze, failed, deps, enabled, journal = (from_native(k) for k in ("analyze", "failed", "deps", "enabled", "journal"))
    for key, fn in (("analyze", analyze), ("blame", blame), ("failed", failed), ("deps", deps), ("enabled", enabled),
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
        return re.split(r"[:/]", v, maxsplit=1)[0].lower() in aliases
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


def norm_ip(addr):
    """'[::ffff:192.0.2.1]' -> '192.0.2.1', 'fe80::1%eth0' -> 'fe80::1': one spelling per address, so the two ends of a
    connection (the host's sockets, the container's) and the docker inspect IPs compare equal. Not an IP: kept as is."""
    a = str(addr or "").strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return a
    return str(getattr(ip, "ipv4_mapped", None) or ip)


def parse_ss_all(text):
    """`ss -tanH` (every state) -> {'listen': [{'addr','port'}], 'estab': [{'l_addr','l_port','p_addr','p_port','proc'}]}.

    One call per container namespace gives both the ports it listens on and its connections (addresses normalised)."""
    listen, estab = [], []
    for ln in text.splitlines():
        p = ln.split()
        if p and p[0].startswith("tcp"):  # a Netid column (several socket types asked): not ours, but harmless
            p = p[1:]
        if len(p) < 5:
            continue
        la, _, lp = p[3].rpartition(":")
        pa, _, pp = p[4].rpartition(":")
        if not lp.isdigit():
            continue
        if p[0] == "LISTEN":
            listen.append({"addr": norm_ip(la), "port": int(lp)})
        elif p[0] in ("ESTAB", "ESTABLISHED") and pp.isdigit():
            m = PROC.search(" ".join(p[5:]))
            estab.append({"l_addr": norm_ip(la), "l_port": int(lp), "p_addr": norm_ip(pa), "p_port": int(pp),
                          "proc": m.group(1) if m else ""})
    return {"listen": listen, "estab": estab}


def container_ss(pid):
    """Every TCP socket *inside* the container's network namespace (parse_ss_all): external clients show up with their
    real IP (from the host, Docker's DNAT hides them from ss). Needs root. None if unreadable."""
    try:
        rc, out, _ = run("nsenter", "-t", str(pid), "-n", "ss", "-tanH", timeout=5)
    except Absent:
        return None
    return parse_ss_all(out) if rc == 0 else None


def container_conns(pid, ss_fn=None):
    """Established connections inside the container's namespace; ss_fn = the per-pass memo of container_ss (one nsenter
    per container serves the databases and the map). None if unreadable."""
    rows = (ss_fn or container_ss)(pid)
    return None if rows is None else rows["estab"]


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


def aliases_of(name, d):
    """Names other containers can reach it by: container name, compose service, network aliases and DNS names on the
    user networks (not the 12-hex short ids: they would match any hex word)."""
    nets = d["NetworkSettings"].get("Networks") or {}
    out = {a.lower() for n in nets if n not in ("bridge", "host", "none")
           for a in (nets[n].get("Aliases") or []) + (nets[n].get("DNSNames") or []) if not re.fullmatch(r"[0-9a-f]{12}", a)}
    out |= {name.lower(), ((d["Config"].get("Labels") or {}).get("com.docker.compose.service") or name).lower()}
    out.discard("")
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
        aliases = aliases_of(name, d)
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


def parse_depends_on(label):
    """Compose label com.docker.compose.depends_on 'db:service_healthy:false,cache:service_started:true' -> ['db', 'cache']."""
    out = []
    for item in (label or "").split(","):
        svc = item.split(":", 1)[0].strip()
        if svc and svc not in out:
            out.append(svc)
    return out


def remember_links(now_edges, names, now, seen=None):
    """Adds this pass's edges {(from, to, port): {connection ids}} to the memory and returns every edge remembered.

    Bounded: 24 h, at most LINKS_MAX_EXT outside peers per node and LINKS_MAX edges in all (the oldest go first, outside
    peers before containers and processes); edges of a container that is gone are forgotten (names None = Docker
    unreadable this pass: nothing is forgotten for that)."""
    seen = LINKS_SEEN if seen is None else seen
    n_of = lambda k: len(now_edges.get(k) or ())  # noqa: E731
    for k in now_edges:
        seen[k] = now
    for k, t in list(seen.items()):
        if now - t > LINKS_TTL_S or (names is not None and any(e.startswith("ct:") and e[3:] not in names for e in k[:2])):
            del seen[k]
    peers = {}  # node -> {outside peer: (last, n)}
    for k, t in seen.items():
        for node, peer in ((k[0], k[1]), (k[1], k[0])):
            if peer.startswith("ext:") and not node.startswith("ext:"):
                mine = peers.setdefault(node, {})
                mine[peer] = max(mine.get(peer, (0, 0)), (t, n_of(k)))
    keep = {node: set(sorted(m, key=lambda p: (m[p], p), reverse=True)[:LINKS_MAX_EXT]) for node, m in peers.items()}
    for a, b, p in list(seen):
        if (b.startswith("ext:") and b not in keep.get(a, ())) or (a.startswith("ext:") and a not in keep.get(b, ())):
            del seen[(a, b, p)]
    rank = lambda k: (seen[k], not (k[0].startswith("ext:") or k[1].startswith("ext:")), n_of(k), k)  # noqa: E731
    for k in sorted(seen, key=rank, reverse=True)[LINKS_MAX:]:
        del seen[k]
    return [{"from": a, "to": b, "port": p, "n": n_of((a, b, p)), "last": seen[(a, b, p)]} for a, b, p in sorted(seen)]


def link_items(inspected, netns_fn, host_conns, listeners, local_addrs, now=None, names=None):
    """The MAP's data: every container (state, compose wiring, ports) and who is connected to whom.

    Nodes: 'ct:<container>', 'proc:<host process>', 'ext:<ip>' (anything that is neither this host nor a container).
    - inspected: docker inspect of the containers (running + exited with an error); None = Docker absent or unreadable;
    - names: every container Docker lists, Exited (0) included: a finished one-shot job is not on the map's list, but
      its connections are forgotten only when it is removed (or after 24 h), so a nightly job still shows the next morning;
    - netns_fn(pid) -> parse_ss_all() of a container's namespace, None if unreadable; netns_fn None = host sockets only
      (macOS/Windows: the containers live in Docker Desktop's VM);
    - host_conns: the host's established sockets with their process (None = unreadable); listeners: net['listeners'].
    Connections are counted once even when both ends are seen (two containers' namespaces, or a container's and the
    host's), aggregated by (from, to, server port) and remembered for 24 h (remember_links).
    Only names, ports and addresses are kept: never env values, command lines or payloads."""
    now = now or time.time()
    by_name = {d["Name"].lstrip("/"): d for d in inspected or []}
    running = {n for n, d in by_name.items() if (d.get("State") or {}).get("Running")}
    nets_of = {n: (d.get("NetworkSettings") or {}).get("Networks") or {} for n, d in by_name.items()}
    ip_owner, gateways = {}, set()
    for n, nets in nets_of.items():
        for net in nets.values():
            for k in ("IPAddress", "GlobalIPv6Address"):
                if net.get(k) and n in running:  # a stopped container has no address (and its old one may be reused)
                    ip_owner[norm_ip(net[k])] = n
            gateways.update(norm_ip(net[k]) for k in ("Gateway", "IPv6Gateway") if net.get(k))
    # the host's addresses (hostname -I lists the bridge gateways too); a macvlan network's gateway is the router, not us
    host_addrs = {norm_ip(a) for a in local_addrs or ()} or set(gateways)
    by_port = {}  # TCP listening port -> [(address, process)]
    for x in listeners or []:
        if x.get("proto") == "tcp" and isinstance(x.get("port"), int):
            by_port.setdefault(x["port"], []).append((norm_ip(x.get("addr")), x.get("proc") or ""))
    services = {p for ls in by_port.values() for _, p in ls if p}
    published = {}  # host port -> [(scope, container)]
    for n in sorted(running):
        if (by_name[n].get("HostConfig") or {}).get("NetworkMode") != "host":
            for x in ports_of(by_name[n]):
                if x["p"] and str(x["c"]).endswith("/tcp"):
                    published.setdefault(x["p"], []).append((x["s"], n))

    def server_proc(port, addr):
        """The process listening where a connection to addr:port lands; None if nothing listens there ('' = unknown)."""
        ls = by_port.get(port) or ()
        return next((p for a, p in ls if a == addr), next((p for a, p in ls if a in WILDCARD), None))

    def target(port, addr):
        """Node behind addr:port of this host: the container that publishes it, else the listening process."""
        cands = published.get(port) or ()
        owner = next((n for s, n in cands if s == addr or (s == "lo" and is_local(addr, ()))), None) \
            or next((n for s, n in cands if s == "*"), None)
        if owner:
            return "ct:" + owner
        p = server_proc(port, addr)
        return "proc:" + p if p else None

    now_edges = {}

    def add(frm, to, port, c):
        if frm != to and port:
            now_edges.setdefault((frm, to, port), set()).add(frozenset(((c["l_addr"], c["l_port"]), (c["p_addr"], c["p_port"]))))

    by_service = {}  # (compose project, service) -> container names
    for n, d in by_name.items():
        lb = (d.get("Config") or {}).get("Labels") or {}
        by_service.setdefault((lb.get("com.docker.compose.project") or "", lb.get("com.docker.compose.service") or ""), []).append(n)
    aliases = {n: aliases_of(n, d) for n, d in by_name.items()}
    user_nets = {n: {k for k in nets if k not in ("bridge", "host", "none")} for n, nets in nets_of.items()}
    containers, ns_ok, ns_bad = [], 0, 0
    for name, d in sorted(by_name.items()):
        st, cfg, hc = d.get("State") or {}, d.get("Config") or {}, d.get("HostConfig") or {}
        labels, mode, up = cfg.get("Labels") or {}, hc.get("NetworkMode") or "", name in running
        host_net = mode == "host"
        rows = None
        # 'container:X' shares X's namespace: reading it again would give X's connections to this one
        if netns_fn and up and st.get("Pid") and not host_net and not mode.startswith("container:"):
            rows = netns_fn(st["Pid"])
            ns_ok, ns_bad = ns_ok + (rows is not None), ns_bad + (rows is None)
        if rows is not None:
            listen_all = {x["port"] for x in rows["listen"]}
            listen = sorted({x["port"] for x in rows["listen"] if not is_local(x["addr"], ())})  # loopback: itself only
        else:
            listen = sorted({int(k.split("/")[0]) for k in cfg.get("ExposedPorts") or {}
                             if str(k).endswith("/tcp") and k.split("/")[0].isdigit()})
            listen_all = set(listen)
        me = "ct:" + name
        for c in (rows or {}).get("estab") or ():
            c = dict(c, l_addr=norm_ip(c["l_addr"]), p_addr=norm_ip(c["p_addr"]))
            peer = c["p_addr"]
            other = ip_owner.get(peer)
            if other == name or is_local(peer, ()):  # loopback inside a container is the container itself
                continue
            if c["l_port"] in listen_all:  # someone connected to it
                if other:
                    add("ct:" + other, me, c["l_port"], c)
                elif not (peer in gateways or is_local(peer, host_addrs)):  # from the host side: the host's sockets say who
                    add("ext:" + peer, me, c["l_port"], c)
            elif other:
                add(me, "ct:" + other, c["p_port"], c)
            elif is_local(peer, host_addrs):  # to a service of the host (or a port another container publishes)
                t = target(c["p_port"], peer)
                if t:
                    add(me, t, c["p_port"], c)
            else:
                add(me, "ext:" + peer, c["p_port"], c)
        project = labels.get("com.docker.compose.project") or ""
        depends = []
        for svc in parse_depends_on(labels.get("com.docker.compose.depends_on")):
            for x in sorted(by_service.get((project, svc)) or [svc]) if project else [svc]:
                if x not in depends:
                    depends.append(x)
        env = [e.partition("=") for e in cfg.get("Env") or [] if "=" in e]
        env_refs = sorted(o for o in by_name if o != name and user_nets[name] & user_nets[o]
                          and any(env_uses(aliases[o], k, v) for k, _, v in env))
        containers.append({"name": name, "image": cfg.get("Image") or "", "project": project,
                           "service": labels.get("com.docker.compose.service") or "", "state": st.get("Status") or "",
                           "health": (st.get("Health") or {}).get("Status") or "", "exit": None if up else st.get("ExitCode"),
                           "restarts": d.get("RestartCount") or 0, "restart": (hc.get("RestartPolicy") or {}).get("Name") or "",
                           "host_net": host_net, "nets": {k: v.get("IPAddress") or "" for k, v in nets_of[name].items() if k not in ("host", "none")},
                           "ports": [] if host_net else ports_of(d), "listen": listen, "listen_src": "netns" if rows is not None else "image",
                           "depends_on": depends, "env_refs": env_refs, "db": db_kind(cfg.get("Image") or "")})
    for c in host_conns or ():
        c = dict(c, l_addr=norm_ip(c["l_addr"]), p_addr=norm_ip(c["p_addr"]))
        la, pa, proc = c["l_addr"], c["p_addr"], c.get("proc") or ""
        other = ip_owner.get(pa)
        peer_local = not other and is_local(pa, host_addrs)
        if server_proc(c["l_port"], la) is not None:  # the server end: someone connected to a port of this host
            t = target(c["l_port"], la)
            if t and not peer_local:  # a local client is counted from its own socket, below
                add("ct:" + other if other else "ext:" + pa, t, c["l_port"], c)
        elif proc and proc not in DOCKER_PROXIES:  # the client end; a proxy's own connections are its containers' traffic
            if other:
                add("proc:" + proc, "ct:" + other, c["p_port"], c)
            elif peer_local:
                t = target(c["p_port"], pa)
                if t:
                    add("proc:" + proc, t, c["p_port"], c)
            elif proc in services:  # only services: a desktop's hundreds of browser connections are noise
                add("proc:" + proc, "ext:" + pa, c["p_port"], c)
    errors = []
    if netns_fn and ns_bad:
        errors.append(f"{ns_bad} of {ns_ok + ns_bad} container namespaces unreadable" if ns_ok else
                      "container namespaces unreadable (nsenter needs root): container links seen from the host only")
    if listeners is None and host_conns is not None:
        errors.append("listening ports unknown: host connections only partly classified")
    return {"since": SINCE, "conn_source": "netns" if netns_fn and (ns_ok or not ns_bad) else "host", "containers": containers,
            "conns": remember_links(now_edges, set(by_name) | set(names or ()) if inspected is not None else None, now),
            "errors": errors}


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
            rows, units = parse_ss(out), {}
            for x in rows:
                pid = x.pop("pid", None)  # changes at every restart: only the unit it names is kept
                if pid and pid not in units:
                    units[pid] = unit_of_pid(pid)
                if pid and units[pid]:
                    x["unit"] = units[pid]
            return rows
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

    memo = {}  # one docker inspect, one host socket list, one nsenter per container per pass: dbs and links share them

    def once(key, fn):
        if key not in memo:
            try:
                memo[key] = fn()
            except Exception as e:  # noqa: BLE001 - remembered: every section of this pass sees the same failure
                memo[key] = e
        if isinstance(memo[key], Exception):
            raise memo[key]
        return memo[key]

    def inspect():
        """docker inspect of the running containers and of those that exited with an error, and the names of every
        container listed. Exited (0) = a finished one-shot job: not inspected (dropped like in collect()) but named, so
        the map forgets its connections only once it is removed. The map shows both, the databases only the running ones."""
        rc, out, err = run("docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Status}}\t{{.Names}}")
        if rc != 0:
            raise RuntimeError(err or "docker ps failed")
        rows = [ln.split("\t", 2) + ["", ""] for ln in out.splitlines()]
        ids = [r[0] for r in rows if r[0].strip() and not EXIT_OK.match(r[1])]
        names = {n for r in rows for n in r[2].strip().split(",") if n and "/" not in n}  # 'other/alias': a legacy link, not a name
        if not ids:
            return [], names
        # a container may vanish between ps and inspect: rc != 0 but the output for the others is valid
        rc, insp, err = run("docker", "inspect", *ids)
        if rc != 0 and not insp.strip().startswith("["):
            raise RuntimeError(err or "docker inspect failed")
        return json.loads(insp or "[]"), names

    def host_conns():
        if WINDOWS:
            return cwin.established()
        if MACOS:
            return cmac.established(run)
        rc, est, err = run("ss", "-tnpH", "state", "established")
        if rc != 0:
            raise RuntimeError(err or "ss failed")
        return parse_established(est)

    def host_addrs():
        if WINDOWS:
            return cwin.local_addrs()
        if MACOS:
            return cmac.local_addrs(run)
        try:
            _, hostip, _ = run("hostname", "-I")
        except Absent:
            hostip = ""
        return set(hostip.split())

    def netns(pid):
        return once(("netns", pid), lambda: container_ss(pid))

    def dbs():
        running = [x for x in once("docker", inspect)[0] if (x.get("State") or {}).get("Running")]
        if not LINUX:  # Docker Desktop: the containers live in a VM, their namespaces are out of reach (no 'who connects')
            return db_items(running, once("est", host_conns), None, once("addrs", host_addrs))
        return db_items(running, once("est", host_conns), lambda pid: container_conns(pid, netns), once("addrs", host_addrs))

    def links():
        """Fails softly: without Docker the host's own connections still make a map, and the reverse; each missing
        source is a note in links['errors']. Absent only when neither Docker nor the host sockets exist."""
        notes, insp, names, est, absent = [], None, None, None, []
        try:
            insp, names = once("docker", inspect)
        except Absent:
            absent.append("docker")
        except Exception as e:  # noqa: BLE001
            notes.append("docker unreadable: " + str(e)[:100])
        try:
            est = once("est", host_conns)
        except Absent as e:
            absent.append(str(e))
            notes.append(f"{e} not installed: host connections not seen")
        except Exception as e:  # noqa: BLE001
            notes.append("host connections unreadable: " + (str(e) or repr(e))[:100])
        if len(absent) == 2:
            raise Absent("docker and " + absent[1])
        try:
            addrs = once("addrs", host_addrs)
        except Exception:  # noqa: BLE001 - without them only loopback counts as this host
            addrs = set()
        netns_fn = netns if LINUX and shutil.which("nsenter", path=SBIN) else None
        if LINUX and not netns_fn and insp:
            notes.append("nsenter not installed: container links seen from the host only")
        out = link_items(insp, netns_fn, est, d.get("listeners"), addrs, names=names)
        out["errors"] = notes + out["errors"]
        return out

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
    section("links", links)  # after listeners and dbs: it reads the first and reuses what the second ran
    section("ts_peers", ts_peers)
    section("f2b", f2b_jails)
    section("drops", drops)
    return d


def sane_c(value):
    """A plausible CPU temperature (°C, 0.1 precision), else None. About 0 °C is what tools print when they read nothing
    (osx-cpu-temp on Apple Silicon, a dummy ACPI zone at 273.2 K): no reading, not a cold CPU."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return round(float(value), 1) if TEMP_RANGE_C[0] <= value <= TEMP_RANGE_C[1] and abs(value) >= 0.5 else None


def cpu_temps(source, readings, errors):
    """One source's readings [{'label', 'c', 'role': 'package' | 'core' | None, 'core': n}] -> sensors.json's
    {'package', 'cores', 'sensors', 'source'}, or None when no reading is plausible. Implausible readings are dropped,
    with a note in errors[source]."""
    out, bad = {"package": None, "cores": {}, "sensors": [], "source": source}, []
    for r in readings:
        c = sane_c(r.get("c"))
        if c is None:
            bad.append(f"{r.get('label')}: {r.get('c')}")
            continue
        out["sensors"].append({"label": str(r.get("label"))[:40], "c": c})
        if r.get("role") == "package" and out["package"] is None:
            out["package"] = c
        elif r.get("role") == "core" and isinstance(r.get("core"), int):
            out["cores"].setdefault(r["core"], c)
    if bad:
        errors[source] = ("no or implausible value, dropped: " + ", ".join(bad))[:120]
    return out if out["sensors"] else None


def collect_sensors():
    """sensors.json: the CPU temperature on macOS and Windows. Every source fails on its own; a tool that is not installed
    is 'absent', never an error; what cannot be read stays None.

    macOS: Apple's powermetrics as root (Intel: CPU die temperature; every Mac: thermal pressure; Apple Silicon: cluster
    frequency and residency, no temperature), then, only when it gives no temperature, the optional smctemp or
    osx-cpu-temp as their owner (never root). Windows: one PowerShell call (WMI): LibreHardwareMonitor, else
    OpenHardwareMonitor, else the ACPI thermal zones."""
    d = {"ts": time.time(), "os": OS_NAME, "errors": {}, "absent": [],
         "cpu": {"package": None, "cores": {}, "sensors": [], "source": None, "pressure": None, "clusters": []}}
    if "cpu" in OFF:  # main() does not even start the thread: this is for a direct call
        d["disabled"] = True
        return d

    def read(name, fn):
        try:
            return fn()
        except Absent:
            d["absent"].append(name)
        except Exception as e:  # noqa: BLE001 - one broken source leaves the others
            d["errors"][name] = (str(e) or repr(e))[:120]
        return None

    def use(source, readings):
        temps = cpu_temps(source, readings, d["errors"])
        if temps:
            d["cpu"].update(temps)
        return bool(temps)

    if MACOS:
        pm = read("powermetrics", lambda: cmac.powermetrics(run, platform.machine())) or {}
        d["cpu"].update(pressure=pm.get("pressure"), clusters=pm.get("clusters") or [])
        if not use("powermetrics", [{"label": "CPU die", "c": pm["die"], "role": "package"}] if pm.get("die") is not None else []):
            for tool in cmac.TEMP_TOOLS:
                c = read(tool, lambda tool=tool: cmac.temp_tool(run, tool))
                if c is not None and use(tool, [{"label": f"CPU ({tool})", "c": c, "role": "package"}]):
                    break
    elif WINDOWS:
        for src, status, readings in read("powershell", lambda: cwin.sensor_sources(cwin.powershell(run, cwin.PS_SENSORS, timeout=30))) or ():
            if status == "absent":
                d["absent"].append(src)
            elif status:
                d["errors"][src] = status
            elif use(src, readings):
                break
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


# ---- history ([features] health): the SQLite store (history.py), filled by a thread of its own ----------------------------
# Every minute: processes and host figures into memory; every 5 minutes: flushed to the hour rows (one transaction), and the
# events since the last look (journal / Event Log / crash reports, Docker) with their cursors; once a day: disks, retention.
# Each source fails alone (the error goes to meta last_errors), and nothing here may stop the thread.
HIST_SAMPLE_S, HIST_FLUSH_S, HIST_EVENTS_S = 60, 300, 300
HIST_CALL_S = 25  # a stuck network mount must not stop the history


def call_with_timeout(fn, seconds):
    """fn() in a helper thread; TimeoutError if it does not return in time (the thread is abandoned: statvfs on a dead mount
    can block for minutes)."""
    box = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box["e"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError("no answer in %d s" % seconds)
    if "e" in box:
        raise box["e"]
    return box["v"]


def hist_meminfo():
    """{'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'} in bytes, on any OS."""
    if LINUX:
        with open("/proc/meminfo") as f:
            return history.parse_meminfo(f.read())
    import hostinfo
    return hostinfo.meminfo()


def hist_sensors():
    """sensors.json (macOS/Windows collector thread) or None: absent, unreadable or not there yet."""
    try:
        with open(os.path.join(nuc_config.RUN_DIR, "sensors.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def disk_rows():
    """[{'mount', 'used', 'total'}] of every real filesystem (the same filter as the DISKS section)."""
    if LINUX:
        with open("/proc/mounts") as f:
            mounts = history.parse_mounts(f.read())
        rows = []
        for mp in mounts:
            try:
                st = os.statvfs(mp)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            if total:
                rows.append({"mount": mp, "used": (st.f_blocks - st.f_bfree) * st.f_frsize, "total": total})
        return rows
    import hostinfo
    return hostinfo.filesystems()


class HistoryJob(object):
    """One step per minute of what the history thread does. `procs` and `cpu` are the renderer's samplers (procs.py,
    cpuinfo.py): created here, in the collector's own thread, and only read by it."""

    def __init__(self, store, procs=None, cpu=None, wall=time.time, mono=time.monotonic):
        self.store, self.procs, self.cpu, self.wall, self.mono = store, procs, cpu, wall, mono
        self.account, self.window = history.ProcAccount(), history.Window()
        self.errors = {}          # source -> short text of its last failure (meta last_errors)
        self.t_flush, self.t_events = mono(), None
        self.throttle_prev = None
        self.boot_seen = None
        self.cores = None
        self.samplers_tried = procs is not None and cpu is not None

    # -- bookkeeping --

    def fail(self, name, e):
        self.errors[name] = (e if isinstance(e, str) else repr(e))[:120]

    def make_samplers(self):
        if self.samplers_tried:
            return
        self.samplers_tried = True
        try:
            import procs as procs_mod
            self.procs = self.procs or procs_mod.ProcSampler()
        except Exception as e:  # noqa: BLE001
            self.fail("procs", e)
        try:
            import cpuinfo
            self.cpu = self.cpu or cpuinfo.CpuSampler()
        except Exception as e:  # noqa: BLE001
            self.fail("cpu", e)

    def step(self):
        """Runs what is due; every source on its own, so one failure leaves the others."""
        self.make_samplers()
        self.sample()
        if self.mono() - self.t_flush >= HIST_FLUSH_S:
            self.flush()
        if self.t_events is None or self.mono() - self.t_events >= HIST_EVENTS_S:
            self.events()

    # -- every minute --

    def host_fields(self, c, now):
        h = {"cpu": None, "mem": None, "swap": None, "load": None, "temp": None, "temp_high": None, "throttle": None}
        c = c if isinstance(c, dict) else {}
        threads = c.get("threads")
        if isinstance(threads, int) and threads > 0 and threads != self.cores:  # cpuinfo knows the logical CPUs (online ones)
            self.cores = threads
            self.store.set_cores(threads)
        busy = ((c.get("usage") or {}).get("total") or {}).get("busy")
        if isinstance(busy, (int, float)):
            h["cpu"] = float(busy)
        load = c.get("load")
        if isinstance(load, (list, tuple)) and load and isinstance(load[0], (int, float)):
            h["load"] = float(load[0])
        elif load is None and hasattr(os, "getloadavg"):
            try:
                h["load"] = os.getloadavg()[0]
            except OSError:
                pass
        try:
            h["mem"], h["swap"] = history.mem_percent(hist_meminfo())
            self.errors.pop("memory", None)
        except Exception as e:  # noqa: BLE001
            self.fail("memory", e)
        temps = c.get("temps") if isinstance(c.get("temps"), dict) else {}
        h["temp"], h["temp_high"] = history.pick_temp(temps)
        if h["temp"] is None:
            h["temp"], _ = history.pick_temp({}, hist_sensors(), now)
        count = history.throttle_count(c.get("throttle"))
        if count is not None:
            if self.throttle_prev is not None and count >= self.throttle_prev:
                h["throttle"] = count - self.throttle_prev  # the counter restarts at boot: a drop is not a negative number
            self.throttle_prev = count
        return h

    def sample(self):
        now, mono = self.wall(), self.mono()
        apps, c = {}, None
        if self.procs is not None:
            try:
                apps = self.account.update(self.procs.sample().get("procs") or [], mono, now)
                self.errors.pop("procs", None)
            except Exception as e:  # noqa: BLE001
                self.fail("procs", e)
        if self.cpu is not None:
            try:
                c = self.cpu.sample()
                self.errors.pop("cpu", None)
            except Exception as e:  # noqa: BLE001
                self.fail("cpu", e)
        self.window.add(now, apps, self.host_fields(c, now))

    def flush(self):
        """The samples so far -> app_hour / host_hour, one transaction. A failed one keeps them for the next try."""
        app_rows, host_rows = self.window.rows()
        if app_rows or host_rows:
            self.store.add_hours(app_rows, host_rows, {"last_errors": json.dumps(self.errors, sort_keys=True)})
        self.window.clear()
        self.t_flush = self.mono()

    # -- every 5 minutes --

    def sources(self):
        out = []
        if LINUX:
            out.append(("journal", self.src_journal))
        elif MACOS:
            out += [("diag", self.src_diag), ("launchd", self.src_launchd)]
        elif WINDOWS:
            out.append(("eventlog", self.src_eventlog))
        if "containers" not in OFF:
            out.append(("docker", self.src_docker))
        return out

    def events(self):
        """The events and log templates since the last look, then what is due once a day. Cursors move only together with the
        rows they cover (one transaction per source)."""
        for name, fn in self.sources():
            self.errors.pop(name, None)
            try:
                evs, logs, meta = fn()
                self.store.add_events(evs, logs, meta)
            except Absent:
                continue  # the tool is not installed: not an error
            except Exception as e:  # noqa: BLE001
                self.fail(name, e)
        try:
            self.boot()
            self.errors.pop("boot", None)
        except Exception as e:  # noqa: BLE001
            self.fail("boot", e)
        try:
            self.daily()
        except Exception as e:  # noqa: BLE001
            self.fail("daily", e)
        self.t_events = self.mono()
        try:
            self.store.set_meta("last_errors", json.dumps(self.errors, sort_keys=True))
        except Exception as e:  # noqa: BLE001
            print("history last_errors:", repr(e), file=sys.stderr)

    def src_journal(self):
        """journalctl, three fixed command lines (history.JOURNAL_SOURCES), each after its own cursor; the first look takes the
        last hour. A cursor the journal no longer knows (vacuumed): from the time of the last entry instead."""
        events, logs, meta, bad = [], [], {}, []
        for name, extra in history.JOURNAL_SOURCES:
            ckey, tkey = name + "_cursor", name + "_ts"
            cursor, rounds = self.store.meta(ckey), 0
            if cursor and not history.CURSOR.match(cursor):
                cursor = None
            while rounds < 4:  # a pass reads JOURNAL_CAP entries at most; a backlog is read in a few rounds
                rounds += 1
                base = ["-o", "json", "--no-pager", "--output-fields=" + history.JOURNAL_FIELDS] + list(extra)
                where = ["--after-cursor=" + cursor] if cursor else ["--since=-1h"]
                rc, out, err = run("journalctl", *(base + where), timeout=60)
                if rc == 1 and not out.strip() and not err:
                    rc = 0  # older systemd: "no entries" is exit status 1, with nothing said; a lost cursor says so on stderr
                if rc != 0 and cursor:
                    ts = (meta.get(tkey) or self.store.meta(tkey) or "")
                    rc, out, err = run("journalctl", *(base + ["--since=@" + ts if ts.isdigit() else "--since=-1h"]), timeout=60)
                if rc != 0:
                    bad.append("%s: %s" % (name, err or "rc=%s" % rc))
                    break
                got = history.parse_journal_events(out, name, now=self.wall())
                events += got["events"]
                logs += got["logs"]
                if got["cursor"]:
                    cursor = meta[ckey] = got["cursor"]
                    meta[tkey] = str(got["ts"])
                if not got["capped"]:
                    break
        if bad:
            self.fail("journal", "; ".join(bad))
        return history.dedupe_crashes(history.merge_events(events)), logs, meta

    def src_docker(self):
        """Restarts, OOM kills and failed exits of containers: what changed in `docker inspect` since the last look (the
        container list of collect() has no restart counter). The first look is only the baseline."""
        rc, out, err = run("docker", "ps", "-a", "-q", "--no-trunc", timeout=20)
        if rc != 0:
            raise RuntimeError(err or "docker ps failed")
        ids = [i for i in out.split() if history.CONTAINER_ID.match(i)][:300]
        cur = {}
        if ids:
            rc, out, err = run("docker", "inspect", "--format", history.DOCKER_FORMAT, *ids, timeout=30)
            if rc != 0:
                raise RuntimeError(err or "docker inspect failed")
            cur = history.parse_docker_state(out)
        prev = history.json_meta(self.store, "docker_state")
        evs = history.docker_events(prev if isinstance(prev, dict) else None, cur, self.wall())
        text = json.dumps(cur, sort_keys=True, separators=(",", ":"))
        return evs, [], {"docker_state": text} if text != self.store.meta("docker_state") else {}

    def src_eventlog(self):
        """Windows: Warning, Error and Critical events of System and Application since the last RecordId (collect_windows)."""
        import collect_windows as cw
        cur = {log: int(self.store.meta("eventlog_" + log) or 0) for log in ("System", "Application")}
        got = cw.parse_events(cw.powershell(run, cw.events_script(cur), timeout=90), self.wall())
        if got["errors"]:
            self.fail("eventlog", "; ".join("%s: %s" % kv for kv in sorted(got["errors"].items())))
        return got["events"], got["logs"], {"eventlog_" + log: str(rec) for log, rec in got["cursors"].items()}

    def src_diag(self):
        """macOS: crash, hang and Jetsam reports newer than the last one read (collect_darwin)."""
        import collect_darwin as cm
        now = self.wall()
        try:
            since = float(self.store.meta("diag_mtime") or 0)
        except ValueError:
            since = 0.0
        evs, newest = cm.diag_events(since or now - cm.DIAG_FIRST_S, now=now)
        return evs, [], {"diag_mtime": repr(float(newest))}

    def src_launchd(self):
        """macOS: third-party launch daemons that exited with an error since the last look."""
        import collect_darwin as cm
        failed = cm.daemons(run)[1]
        prev = history.json_meta(self.store, "launchd_failed")
        return (cm.new_failed_daemons(prev if isinstance(prev, list) else None, failed, self.wall()), [],
                {"launchd_failed": json.dumps(sorted(failed))})

    def boot(self):
        """The boot time of the current boot, when boot.json (the boot thread) has it: each boot once."""
        try:
            with open(OUT_BOOT, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        total = (d.get("analyze") or {}).get("total") if isinstance(d, dict) and isinstance(d.get("analyze"), dict) else None
        if isinstance(total, (int, float)) and isinstance(d.get("btime"), (int, float)) and "%s:%s" % (d["btime"], total) != self.boot_seen:
            self.store.put_boot(d["btime"], total, d.get("kernel"))
            self.boot_seen = "%s:%s" % (d["btime"], total)

    # -- once a day --

    def daily(self):
        now = self.wall()
        day = int(now // 86400)
        if self.store.meta("last_daily") == str(day):
            return
        try:
            self.store.put_disks(day, call_with_timeout(disk_rows, HIST_CALL_S))
            self.errors.pop("disks", None)
        except Exception as e:  # noqa: BLE001
            self.fail("disks", e)
        self.store.prune(now)
        self.store.vacuum_if_due(now)
        self.store.set_meta("last_daily", str(day))


def history_loop(make=None, sleep=time.sleep, stop=None, mono=time.monotonic):
    """The history thread. It must never die: whatever goes wrong (a full disk, a locked file, a bug) is printed once and tried
    again a minute later; the database is opened here, not at import, so `--once` and a disabled feature never create it."""
    job, last = None, None
    try:
        while not (stop and stop()):
            t0 = mono()
            try:
                if job is None:
                    job = make() if make else HistoryJob(history.Store())
                job.step()
                last = None
            except Exception as e:  # noqa: BLE001
                msg = repr(e)[:200]
                if msg != last:  # the same failure every minute would fill the log
                    print("history:", msg, file=sys.stderr)
                    last = msg
            sleep(max(1.0, HIST_SAMPLE_S - (mono() - t0)))
    finally:  # stopped (tests): the file is closed, not left to the garbage collector
        store = getattr(job, "store", None)
        if store is not None and hasattr(store, "close"):
            store.close()


# ---- AI digest ([ai] enabled = yes and daily = yes): the advice the screens show, generated once a day --------------------------
# The screens run unprivileged and cannot read the advisor's cache of root, so the collector (root/SYSTEM) writes the 7-day advice
# once a day into the shared advice file (advisor.py "the shared advice", next to history.db). The model is slow and its client
# must not weigh on the machine: a child process (`advisor.py advise --store --period 7`, fixed command line, low priority, time
# limit) does the work and this thread only waits for it, so no other collector thread is ever held up. First digest ~10 minutes
# after the start, then every 24 hours; the last attempt is kept in the shared file so that a restart does not run another one.
AI_FIRST_S, AI_EVERY_S, AI_CHECK_S = 600, 86400, 3600  # the clock is looked at least hourly: a clock step or an edit is noticed
AI_MARGIN_S, AI_PERIOD_DAYS = 120, 7                    # the child may take [ai] timeout_s plus the model listing and the report


def ai_daily_on(cfg=None, off=None):
    """The digest needs the model ([ai] enabled), the switch ([ai] daily) and the history it reads ([features] health)."""
    ai = (CFG if cfg is None else cfg).get("ai") or {}
    return bool(ai.get("enabled") and ai.get("daily")) and "health" not in (OFF if off is None else off)


def ai_digest_argv():
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "advisor.py"), "advise", "--store", "--period",
            str(AI_PERIOD_DAYS)]


def ai_digest(cfg=None, run=None):
    """One digest in a child process -> (ok, one line for the log). Never raises. The child lowers its own niceness (POSIX:
    preexec_fn is not safe in a process with threads); on Windows it starts at BELOW_NORMAL priority without a window."""
    run = run or subprocess.run
    try:
        limit = float(((CFG if cfg is None else cfg).get("ai") or {}).get("timeout_s", 120))
    except (TypeError, ValueError):
        limit = 120.0
    limit = int(max(10.0, min(600.0, limit))) + AI_MARGIN_S
    extra = {}
    if WINDOWS:
        extra["creationflags"] = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    try:
        r = run(ai_digest_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=limit,
                encoding="utf-8", errors="replace", **extra)
    except subprocess.TimeoutExpired:
        return False, "no answer within %d s: stopped" % limit
    except Exception as e:  # noqa: BLE001 - no python, no such file...
        return False, "cannot run the advisor: %s" % repr(e)[:150]
    if r.returncode == 0:
        return True, "advice for the last %d days stored" % AI_PERIOD_DAYS
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    tail = re.sub(r"[^\x20-\x7e]", "?", lines[-1])[:200] if lines else ""  # printable: it goes to a log
    return False, "exit %s%s" % (r.returncode, ": " + tail if tail else "")


def ai_daily_loop(cfg=None, sleep=time.sleep, wall=time.time, run=None, stop=None):
    """The digest thread. A failure (the model is down, a timeout, a full disk) is printed once and the digest is tried again the
    next day, never in a loop; the thread itself never dies. sleep/wall/run/stop: the tests' clock and child."""
    import advisor  # only here: --once and a collector without [ai] daily never load it
    run = run or ai_digest
    started, last, seen = wall(), None, None
    mark = advisor.read_store().get("daily")  # the previous collector's last attempt
    if mark and mark["at"] <= started + advisor.SKEW:  # one from the future: the clock was wrong, it counts for nothing
        last = mark["at"]

    def note(at, ok):
        nonlocal seen
        try:
            advisor.mark_daily(at, ok)
        except Exception as e:  # noqa: BLE001 - the digest still runs once a day: `last` is kept in memory too
            msg = repr(e)[:200]
            if msg != seen:
                print("ai digest: cannot keep the schedule:", msg, file=sys.stderr)
                seen = msg
    while not (stop and stop()):
        now = wall()
        if started > now + advisor.SKEW:  # the clock went back (a wrong one corrected): counted from now on
            started = now
        if last is not None and last > now + advisor.SKEW:
            last = now
        due = max(started + AI_FIRST_S, last + AI_EVERY_S if last is not None else 0)
        if now < due:
            sleep(max(1.0, min(due - now, AI_CHECK_S)))
            continue
        last = now
        note(now, None)
        try:
            ok, msg = run(cfg)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, repr(e)[:200]
        note(now, bool(ok))
        print("ai digest:" if ok else "ai digest failed:", msg, file=sys.stdout if ok else sys.stderr)


def sensors_loop():
    while True:
        try:
            write_atomic(collect_sensors(), OUT_SENSORS)
        except Exception as e:  # noqa: BLE001 - the thread must not die: the file goes stale and the renderer flags it
            print("collect_sensors:", repr(e), file=sys.stderr)
        time.sleep(SENSORS_INTERVAL_S)


def sensors_on():
    """sensors.json is written on macOS and Windows only (Linux: the renderer reads sysfs), with [features] cpu on."""
    return (MACOS or WINDOWS) and "cpu" not in OFF


def loops():
    """The background loops main() starts next to the container loop."""
    out = [] if {"boot", "docker_disk"} <= OFF else [boot_loop]
    out.append(net_loop)  # separate thread: slow ufw/iptables/fail2ban must not stop the container refresh
    if sensors_on():
        out.append(sensors_loop)  # powermetrics takes ~1 s, PowerShell longer: never in another loop's way
    if "health" not in OFF:
        out.append(history_loop)  # the history: its own thread and file (history.db), never touched by --once
    if ai_daily_on():
        out.append(ai_daily_loop)  # the daily AI digest: its own thread, the model runs in a low-priority child process
    return out


def once():
    """`--once`: every state file's content in one document ("sensors" on macOS and Windows only)."""
    out = {"containers": collect(), "net": collect_net(), "boot": None if {"boot", "docker_disk"} <= OFF else collect_boot()}
    if MACOS or WINDOWS:
        out["sensors"] = collect_sensors() if sensors_on() else None
    return out


def main():
    if "--log" in sys.argv[:-1]:  # Windows scheduled task: no journal, the log goes to %ProgramData%\nuc-console\logs
        nuc_config.log_to(sys.argv[sys.argv.index("--log") + 1])
    if WINDOWS:
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # never look for docker.exe & co. in the working directory
    if "--once" in sys.argv:
        print(json.dumps(once(), indent=1))
        return
    os.makedirs(nuc_config.RUN_DIR, exist_ok=True)  # systemd creates it (RuntimeDirectory); launchd and Task Scheduler do not
    for fn in loops():
        threading.Thread(target=fn, daemon=True).start()
    while True:
        try:
            data = collect()
        except Exception as e:  # docker down or unexpected output: the state carries the error, the loop goes on
            data = {"ts": time.time(), "error": repr(e)[:200], "containers": []}
        write_atomic(data)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
