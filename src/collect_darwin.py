"""Collector sections for macOS (the collector runs as root from a LaunchDaemon).

Sockets: `lsof` (root sees every process); program paths: `ps`; firewall: the Application Firewall (`socketfilterfw`) and
`pfctl`; services: `launchctl` and the plists in /Library/LaunchDaemons. Every function takes `run` (collector.run) so the
parsers and the verdicts are tested with fixtures on any OS.

The macOS Application Firewall works per program, not per port:
  - off: every listener is reachable from the network ('nofw');
  - "block all incoming": only essential system services (DHCP, Bonjour, IPsec) are reachable;
  - on: a program listed in the firewall is allowed or blocked as listed; Apple's own programs are allowed when
    "automatically allow built-in software" is on; any other unlisted program is 'unknown' (macOS asks the user, or allows
    it automatically when it is signed and "downloaded signed software" is on: not verified here) = treated as open.
pf (the packet filter) is off by default; when it is on with rules of its own those rules are not interpreted, so a verdict
that would say "reachable" becomes 'unknown'.
"""
import os
import plistlib
import re

APPLE_DIRS = ("/System/", "/usr/libexec/", "/usr/sbin/", "/usr/bin/", "/sbin/", "/bin/")
ESSENTIAL = {"mDNSResponder", "configd", "racoon", "launchd"}  # allowed even with "block all incoming"
SFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
DYNAMIC_UDP = 49152  # client sockets (browsers, FaceTime, QUIC) from here up: not services


def parse_lsof(text, proto):
    """`lsof -nP +c 0 -F pcn` -> [{'proto','addr','port','pid','proc'}]; connected UDP sockets ('->') are skipped."""
    out, pid, cmd, seen = [], 0, "", set()
    for ln in text.splitlines():
        tag, val = ln[:1], ln[1:]
        if tag == "p":
            pid = int(val) if val.isdigit() else 0
        elif tag == "c":
            cmd = val
        elif tag == "n" and "->" not in val:
            addr, _, port = val.rpartition(":")
            if not port.isdigit():
                continue
            addr = addr.strip("[]").split("%")[0]
            key = (proto, addr, int(port), pid)
            if key not in seen:
                seen.add(key)
                out.append({"proto": proto, "addr": "*" if addr in ("*", "") else addr, "port": int(port), "pid": pid, "proc": cmd})
    return out


def parse_lsof_established(text):
    """`lsof -nP +c 0 -iTCP -sTCP:ESTABLISHED -F pcn` -> [{'l_addr','l_port','p_addr','p_port','proc'}]."""
    out, cmd = [], ""
    for ln in text.splitlines():
        tag, val = ln[:1], ln[1:]
        if tag == "c":
            cmd = val
        elif tag == "n" and "->" in val:
            local, _, peer = val.partition("->")
            la, _, lp = local.rpartition(":")
            pa, _, pp = peer.rpartition(":")
            if lp.isdigit() and pp.isdigit():
                out.append({"l_addr": la.strip("[]"), "l_port": int(lp), "p_addr": pa.strip("[]"), "p_port": int(pp), "proc": cmd})
    return out


def parse_ps(text):
    """`ps -axo pid=,comm=` -> {pid: program path} (comm is the full path of the executable on macOS)."""
    out = {}
    for ln in text.splitlines():
        pid, _, comm = ln.strip().partition(" ")
        if pid.isdigit():
            out[int(pid)] = comm.strip()
    return out


def parse_alf(state, blockall, allowsigned, stealth, apps):
    """socketfilterfw outputs -> {'state': 0 off | 1 on | 2 block all, 'block_all', 'builtin', 'downloaded', 'stealth', 'apps'}."""
    m = re.search(r"State = (\d)", state)
    st = int(m.group(1)) if m else (0 if "disabled" in state.lower() else 1 if "enabled" in state.lower() else None)
    if st is None:
        raise ValueError("firewall state unreadable: " + state.strip()[:60])
    low = allowsigned.lower()
    return {"state": st, "block_all": st == 2 or _on(blockall),
            "builtin": bool(re.search(r"built-in signed software enabled", low)),
            "downloaded": bool(re.search(r"downloaded signed software enabled", low)),
            "stealth": _on(stealth), "apps": parse_alf_apps(apps)}


def _on(text):
    """'... set to enabled.' / 'stealth mode is on' -> True; 'Block all DISABLED!' / '... is off' -> False."""
    t = text.lower()
    return not re.search(r"\b(disabled|off)\b", t) and bool(re.search(r"\b(enabled|on)\b", t))


def parse_alf_apps(text):
    """`socketfilterfw --listapps` -> [{'path', 'allow'}]."""
    out, cur = [], None
    for ln in text.splitlines():
        m = re.match(r"^\s*\d+\s*:\s*(\S.*?)\s*$", ln)
        if m:
            cur = {"path": m.group(1), "allow": None}
            out.append(cur)
        elif cur is not None and cur["allow"] is None:
            if re.search(r"allow incoming", ln, re.I):
                cur["allow"] = True
            elif re.search(r"block incoming", ln, re.I):
                cur["allow"] = False
    return [a for a in out if a["allow"] is not None]


def parse_pf(info, rules):
    """`pfctl -s info` + `pfctl -s rules` -> {'enabled', 'rules': own rules (Apple's anchors excluded)}."""
    enabled = bool(re.search(r"^Status:\s*Enabled", info, re.M))
    own = [ln for ln in rules.splitlines() if ln.strip() and "com.apple" not in ln]
    return {"enabled": enabled, "rules": len(own)}


def alf_verdict(alf, pf, path, proc):
    """(state, note) for one listening program. See the module docstring for the rules."""
    if alf is None:
        return "unknown", "firewall unreadable"
    st = alf["state"]
    if st == 0:
        v = ("nofw", "macOS firewall off")
    elif alf.get("block_all") or st == 2:
        v = ("open", "essential service") if proc in ESSENTIAL else ("blocked", "block all incoming")
    else:
        listed = next((a for a in alf.get("apps") or [] if path and (path == a["path"] or path.startswith(a["path"].rstrip("/") + "/"))), None)
        if listed:
            v = ("open", "allowed in the firewall") if listed["allow"] else ("blocked", "blocked in the firewall")
        elif proc in ESSENTIAL or (path.startswith(APPLE_DIRS) and alf.get("builtin")):
            v = ("open", "built-in, allowed")
        elif not path:
            v = ("unknown", "program unknown")
        else:
            v = ("unknown", "not listed: macOS decides")
    if pf and pf.get("enabled") and pf.get("rules") and v[0] in ("open", "nofw"):
        return "unknown", "pf rules not interpreted"
    return v


def fw_summary(alf, pf):
    apps = (alf or {}).get("apps") or []
    return {"kind": "darwin", "name": "macOS firewall", "state": (alf or {}).get("state"),
            "off": ["Application Firewall"] if alf and alf["state"] == 0 else [],
            "block_all": bool((alf or {}).get("block_all")), "stealth": bool((alf or {}).get("stealth")),
            "builtin": bool((alf or {}).get("builtin")), "downloaded": bool((alf or {}).get("downloaded")),
            "apps_allowed": sum(a["allow"] for a in apps), "apps_blocked": sum(not a["allow"] for a in apps), "pf": pf}


# ---- sections (run = collector.run) ------------------------------------------------------------------------------------

def _ok(rc, err, what, allowed=(0,)):
    if rc not in allowed:
        raise RuntimeError(err or f"{what} failed")


def firewall(run):
    outs = []
    for flag in ("--getglobalstate", "--getblockall", "--getallowsigned", "--getstealthmode", "--listapps"):
        rc, out, err = run("socketfilterfw", flag)
        _ok(rc, err, "socketfilterfw " + flag)
        outs.append(out)
    alf = parse_alf(*outs)
    try:
        rc, info, _ = run("pfctl", "-s", "info")
        rc2, rules, _ = run("pfctl", "-s", "rules")
        pf = parse_pf(info, rules) if rc == 0 and rc2 == 0 else None
    except Exception:  # noqa: BLE001 - pf is secondary: the Application Firewall is what macOS uses
        pf = None
    return alf, pf


def listeners(run, alf, pf):
    rows = []
    for proto, args in (("tcp", ("-iTCP", "-sTCP:LISTEN")), ("udp", ("-iUDP",))):
        rc, out, err = run("lsof", "-nP", "+c", "0", *args, "-F", "pcn")
        _ok(rc, err, "lsof", allowed=(0, 1))  # 1 = nothing found
        rows += [r for r in parse_lsof(out, proto) if proto == "tcp" or r["port"] < DYNAMIC_UDP]
    rc, out, err = run("ps", "-axo", "pid=,comm=")
    paths = parse_ps(out) if rc == 0 else {}
    out, seen = [], set()
    for r in rows:
        key = (r["proto"], r["addr"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        path = paths.get(r["pid"], "")
        out.append({"proto": r["proto"], "addr": r["addr"], "port": r["port"], "proc": r["proc"] or os.path.basename(path),
                    "fw": list(alf_verdict(alf, pf, path, r["proc"]))})
    return out


def established(run):
    rc, out, err = run("lsof", "-nP", "+c", "0", "-iTCP", "-sTCP:ESTABLISHED", "-F", "pcn")
    _ok(rc, err, "lsof", allowed=(0, 1))
    return parse_lsof_established(out)


def parse_ifconfig_addrs(text):
    return {m.split("%")[0] for m in re.findall(r"^\s*inet6?\s+(\S+)", text, re.M)}


def local_addrs(run):
    try:
        rc, out, _ = run("ifconfig")
    except Exception:  # noqa: BLE001
        return set()
    return parse_ifconfig_addrs(out) if rc == 0 else set()


def parse_launchctl(text):
    """`launchctl list` -> {label: (pid or None, last exit status or None)}."""
    out = {}
    for ln in text.splitlines()[1:]:
        f = ln.split("\t") if "\t" in ln else ln.split()
        if len(f) >= 3:
            pid = int(f[0]) if f[0].lstrip("-").isdigit() and f[0] != "-" else None
            st = int(f[1]) if f[1].lstrip("-").isdigit() else None
            out[f[2].strip()] = (pid, st)
    return out


def failed_status(st):
    """A non-zero exit, or a signal other than the ones of a normal stop (SIGKILL/SIGTERM at shutdown or by launchd)."""
    return st is not None and st != 0 and st not in (-9, -15)


def daemons(run, plist_dir="/Library/LaunchDaemons"):
    """Third-party launch daemons (Apple's own are hundreds and managed by the OS) -> (enabled, failed)."""
    rc, out, err = run("launchctl", "list")
    _ok(rc, err, "launchctl list")
    jobs = parse_launchctl(out)
    enabled = []
    for fn in sorted(os.listdir(plist_dir)) if os.path.isdir(plist_dir) else []:
        if not fn.endswith(".plist"):
            continue
        try:
            with open(os.path.join(plist_dir, fn), "rb") as f:
                pl = plistlib.load(f)
        except Exception:  # noqa: BLE001 - one unreadable plist must not hide the others
            continue
        label = pl.get("Label") or fn[:-6]
        if pl.get("Disabled"):
            continue
        pid, st = jobs.get(label, (None, None))
        state = "active" if pid else "failed" if failed_status(st) else "inactive"
        enabled.append({"unit": label, "state": state})
    failed = sorted(lbl for lbl, (pid, st) in jobs.items() if not pid and failed_status(st) and not lbl.startswith("com.apple."))
    return enabled, failed


# ---- CPU sensors (sensors.json) ----------------------------------------------------------------------------------------

PM_DIE = re.compile(r"^CPU die temperature:\s*(-?\d+(?:\.\d+)?)\s*C\b")
PM_PRESSURE = re.compile(r"^Current pressure level:\s*([A-Za-z]+)")
PM_CLUSTER = re.compile(r"^([A-Za-z0-9]+-Cluster) HW active (frequency|residency):\s*(\d+(?:\.\d+)?)\s*(?:MHz|%)")
PM_CPU = re.compile(r"^CPU (\d+) frequency:\s*(\d+(?:\.\d+)?)\s*MHz")
PRESSURES = ("Nominal", "Moderate", "Heavy", "Trapping", "Sleeping")
# Optional third-party tools for the CPU temperature where powermetrics has none (Apple Silicon). collector.run runs them
# as the user who owns them, never as root, like docker and tailscale: they read the SMC without privileges.
TEMP_TOOLS = {"smctemp": ("-c",), "osx-cpu-temp": ()}
TOOL_TEMP = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:[^\w\s]?\s*C)?\s*$")  # 52.3 | 52.3°C (° maybe mis-decoded)


def pm_samplers(machine):
    """The powermetrics sampler lists to try, best first (an unknown sampler makes it fail; smc exists on Intel only).

    Intel: smc (CPU die temperature) + thermal (pressure). Apple Silicon: thermal + cpu_power (cluster frequency and
    residency). 'x86_64' may also be an Intel Python under Rosetta on Apple Silicon: the Apple Silicon list comes next."""
    if machine.lower().startswith(("arm", "aarch")):
        return (("thermal", "cpu_power"), ("cpu_power",), ("thermal",))
    return (("smc", "thermal"), ("thermal", "cpu_power"), ("thermal",))


def parse_powermetrics(text):
    """powermetrics text -> {'die': C | None, 'pressure': str | None, 'clusters': [{'name', 'mhz', 'active', 'cpus'}]}.

    Intel (smc): 'CPU die temperature: 45.69 C'. Every Mac (thermal): 'Current pressure level: Nominal'. Apple Silicon
    (cpu_power): 'E-Cluster HW active frequency: 1020 MHz' and '... HW active residency:  12.34% (...)' per cluster (P0-,
    P1-Cluster on Max/Ultra), each followed by 'CPU 4 frequency: 2064 MHz' for its CPUs ('cpus': {cpu id: MHz}).
    Apple's text, English on every system; what does not match stays None or missing, never a guess."""
    die = pressure = cur = None
    clusters = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        m = PM_DIE.match(ln)
        if m:
            die = float(m.group(1)) if die is None else die
            continue
        m = PM_PRESSURE.match(ln)
        if m:
            word = m.group(1).capitalize()
            pressure = pressure or (word if word in PRESSURES else m.group(1)[:20])
            continue
        m = PM_CLUSTER.match(ln)
        if m:
            cur = next((c for c in clusters if c["name"] == m.group(1)), None)
            if cur is None:
                cur = {"name": m.group(1), "mhz": None, "active": None, "cpus": {}}
                clusters.append(cur)
            cur["mhz" if m.group(2) == "frequency" else "active"] = float(m.group(3))
            continue
        m = PM_CPU.match(ln)
        if m and cur is not None:
            cur["cpus"][int(m.group(1))] = float(m.group(2))
    return {"die": die, "pressure": pressure, "clusters": clusters}


def powermetrics(run, machine):
    """One ~1 s sample of Apple's /usr/bin/powermetrics (root) -> parse_powermetrics(). When a sampler list is refused
    (unknown sampler on this Mac) the next, smaller one is tried; a timeout or 'not root' ends the attempts."""
    err = ""
    for samplers in pm_samplers(machine):
        rc, out, err = run("powermetrics", "-n", "1", "-i", "1000", "--samplers", ",".join(samplers), timeout=10)
        if rc == 0:
            return parse_powermetrics(out)
        if rc is None or "superuser" in err.lower():
            break
    raise RuntimeError(err or "powermetrics failed")


def parse_tool_temp(text):
    """`smctemp -c` ('52.3') or `osx-cpu-temp` ('52.3°C') -> 52.3; None if that is not what it printed (°F, text)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    m = TOOL_TEMP.match(lines[0]) if len(lines) == 1 else None
    return float(m.group(1)) if m else None


def temp_tool(run, name):
    """The CPU temperature one optional tool prints (TEMP_TOOLS); collector.run raises Absent if it is not installed."""
    rc, out, err = run(name, *TEMP_TOOLS[name], timeout=10)
    if rc != 0:
        raise RuntimeError(err or f"{name} failed (rc={rc})")
    c = parse_tool_temp(out)
    if c is None:
        raise ValueError("unreadable output: " + repr(out.strip()[:40]))
    return c
