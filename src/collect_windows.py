"""Collector sections for Windows (the collector runs as SYSTEM from a scheduled task).

Sockets come from the IP helper API (winapi.py); firewall, services and the event log from one PowerShell call each,
returning JSON (ConvertTo-Json: property names are English on every Windows language, unlike `netstat`/`netsh` text).
The Windows Firewall verdict for each listening port is computed here, because it depends on the program behind the
socket (allow rules are usually per program), which only a SYSTEM process can see.

Rules of the evaluation (Windows Firewall semantics, simplified on the safe side):
  - a profile that is off lets everything in; "block all incoming" blocks everything; default inbound "allow" lets in;
  - otherwise an enabled inbound rule must match protocol, port, program and service; a matching BLOCK rule wins
    over any ALLOW rule; RemoteAddresses '*' or 'LocalSubnet' means open to the LAN, a list of addresses = filtered;
  - anything that cannot be matched with certainty (port keywords like RPC, a rule bound to an interface type or a local
    address, running services unreadable) makes the verdict 'unknown' = treated as open, never 'blocked';
  - with rules coming from Group Policy (not visible in the local store) a 'blocked' becomes 'unknown';
  - several active profiles (one per network): the most exposed verdict wins.
"""
import base64
import json
import ntpath
import os
import re
import socket
import sys
import time

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import winapi

PROFILES = ((1, "Domain"), (2, "Private"), (4, "Public"))
EXPOSED = {"open": 4, "nofw": 4, "unknown": 3, "filtered": 2, "blocked": 1}  # merge: the most exposed verdict wins
PORT_KEYWORDS = {"rpc-epmap": [135], "iphttps": [443], "iphttpsin": [443]}  # the others (RPC, Teredo, Ply2Disc...) = unknown
DYNAMIC_UDP = 49152  # UDP sockets from here up are client sockets (browsers, Teams, QUIC): not services, and they churn
# Rule groups that Windows enforces only on one special interface, although their stored conditions say "any interface":
# Wi-Fi Direct (WlanSvc applies them to WFD links: they allow the kernel process 'System' on every TCP/UDP port, so if they
# applied to the LAN, SMB would be open on every public network, which it is not by default) and Teredo (tunnel only).
IFACE_ONLY_GROUPS = {"@wlansvc.dll,-36864", "@wlansvc.dll,-36865", "@firewallapi.dll,-32752"}
# "Wi-Fi Direct Network Discovery" (spooler, scanner, dashost on any port): meant for Wi-Fi Direct links too, but without the
# same proof: 'unknown', with the rule's name in the note, rather than a guess either way
IFACE_UNSURE_GROUPS = {"@firewallapi.dll,-36851"}

PS_PRELUDE = "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [Text.Encoding]::UTF8\n"

PS_FIREWALL = PS_PRELUDE + r"""
$p = New-Object -ComObject HNetCfg.FwPolicy2
$prof = @{}
foreach ($t in 1, 2, 4) { $prof["$t"] = @{ enabled = [bool]$p.FirewallEnabled($t); inbound = [int]$p.DefaultInboundAction($t); block_all = [bool]$p.BlockAllInboundTraffic($t) } }
$rules = @(foreach ($r in $p.Rules) { if ($r.Direction -eq 1 -and $r.Enabled) { @{ n = [string]$r.Name; a = [int]$r.Action; pr = [int]$r.Protocol; lp = [string]$r.LocalPorts; app = [string]$r.ApplicationName; svc = [string]$r.ServiceName; pf = [int]$r.Profiles; ra = [string]$r.RemoteAddresses; la = [string]$r.LocalAddresses; it = [string]$r.InterfaceTypes; ifs = [string]$r.Interfaces; pkg = [string]$r.LocalAppPackageId; owner = [string]$r.LocalUserOwner; g = [string]$r.Grouping; auth = [string]$r.LocalUserAuthorizedList + [string]$r.RemoteUserAuthorizedList + [string]$r.RemoteMachineAuthorizedList; sf = [int]$r.SecureFlags } } })
$svc = @(Get-CimInstance Win32_Service -Filter "State='Running'" | ForEach-Object { @{ n = [string]$_.Name; pid = [int]$_.ProcessId } })
$nets = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { @{ alias = [string]$_.InterfaceAlias; cat = [string]$_.NetworkCategory } })
@{ current = [int]$p.CurrentProfileTypes; profiles = $prof; rules = $rules; services = $svc; networks = $nets; policy = [bool](Test-Path 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsFirewall') } | ConvertTo-Json -Depth 4 -Compress
"""

PS_BOOT = PS_PRELUDE + r"""
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$svc = @(Get-CimInstance Win32_Service -Filter "StartMode='Auto'" | ForEach-Object { @{ n = [string]$_.Name; s = [string]$_.State; e = [int]$_.ExitCode } })
$ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2, 3; StartTime = $boot } -MaxEvents 500 -ErrorAction SilentlyContinue | ForEach-Object { @{ p = [string]$_.ProviderName; l = [int]$_.Level; m = ([string]$_.Message -split "`r?`n")[0] } })
$perf = $null
try {
  $e = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 100 } -MaxEvents 1 -ErrorAction Stop
  if ($e.TimeCreated -gt $boot) { $x = [xml]$e.ToXml(); $d = @{}; foreach ($n in $x.Event.EventData.Data) { $d[$n.Name] = $n.'#text' }; $perf = @{ total = [int64]$d['BootTime']; main = [int64]$d['MainPathBootTime']; post = [int64]$d['BootPostBootTime'] } }
} catch { $perf = $null }
$dep = @{}
foreach ($s in $svc) { if ($s.s -eq 'Stopped' -and $s.e -ne 0 -and $s.e -ne 1077) { try { $dep[$s.n] = @((Get-Service -Name $s.n -ErrorAction Stop).DependentServices | Select-Object -First 20 | ForEach-Object { [string]$_.Name }) } catch { } } }
@{ boot = ([DateTimeOffset]$boot).ToUnixTimeSeconds(); services = $svc; events = $ev; perf = $perf; deps = $dep } | ConvertTo-Json -Depth 4 -Compress
"""


def powershell(run, script, timeout=60):
    """Runs a fixed script (never user input) and returns its JSON. -EncodedCommand: no quoting, no execution policy."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    rc, out, err = run("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc,
                       timeout=timeout)
    if rc != 0:
        raise RuntimeError(err or f"powershell rc={rc}")
    return json.loads(out.lstrip("\ufeff").strip() or "null")


# ---- Windows Firewall evaluation (pure, tested with fixtures) -----------------------------------------------------------

def expand(path, env):
    """'%SystemRoot%\\system32\\svchost.exe' -> 'c:\\windows\\system32\\svchost.exe' (lower case, backslashes)."""
    p = re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1).upper(), m.group(0)), path or "")
    return ntpath.normpath(p.replace("/", "\\")).lower() if p else ""


def port_match(spec, port):
    """True/False, or None when the rule names ports by keyword (RPC, Teredo...) that cannot be resolved here."""
    spec = (spec or "").strip()
    if spec in ("", "*"):
        return True
    unknown = False
    for tok in (t.strip().lower() for t in spec.split(",") if t.strip()):
        lo, _, hi = tok.partition("-")
        if lo.isdigit() and (hi.isdigit() or not hi):
            if int(lo) <= port <= int(hi or lo):
                return True
        elif tok in PORT_KEYWORDS:
            if port in PORT_KEYWORDS[tok]:
                return True
        else:
            unknown = True
    return None if unknown else False


def rule_match(r, l, svc_pids, env):
    """Does an enabled inbound rule apply to listener l ({'proto','port','pid','path','token'})? True/False/None (= cannot tell).

    Rules with an owner or an app package are the ones Windows creates for Store apps: they apply only inside that app's
    AppContainer, never to a desktop program or a service (the package SID is not always readable, so it is not compared)."""
    pr = r.get("pr", 256)
    if pr != 256 and pr != {"tcp": 6, "udp": 17}.get(l["proto"]):
        return False
    if (r.get("g") or "").lower() in IFACE_ONLY_GROUPS:
        return False
    if (r.get("g") or "").lower() in IFACE_UNSURE_GROUPS:
        return None if rule_match(dict(r, g=""), l, svc_pids, env) is not False else False
    if r.get("pkg") or r.get("owner"):
        tok = l.get("token")
        if tok is None:  # token unreadable (protected process): a program outside the Store app folders is not a Store app
            path = (l.get("path") or "").lower()
            return None if not path or "\\windowsapps\\" in path or "\\systemapps\\" in path else False
        if not tok.get("appcontainer") or (r.get("owner") and r["owner"] != tok.get("user")):
            return False
        return None  # an AppContainer of that user: maybe this very app, maybe another one
    verdicts = [port_match(r.get("lp"), l["port"]) if pr != 256 else True]
    app = (r.get("app") or "").strip()
    if app and app != "*":
        if app.lower() == "system":
            verdicts.append(l.get("pid") == 4)
        elif not l.get("path"):
            verdicts.append(None)
        else:
            verdicts.append(expand(app, env) == expand(l["path"], env))
    svc = (r.get("svc") or "").strip()
    if svc:
        if svc == "*" or not svc_pids:  # any service / the list of running services could not be read
            verdicts.append(None)
        else:  # a service that is not running has no process: its rule cannot be about this socket
            verdicts.append(l.get("pid") in (svc_pids.get(svc.lower()) or ()))
    # bound to a local address, an interface (type), authenticated peers or IPsec: cannot be resolved for "the LAN"
    if (r.get("la") or "*") != "*" or (r.get("it") or "All") != "All" or r.get("ifs") or r.get("auth") or r.get("sf"):
        verdicts.append(None)
    if False in verdicts:
        return False
    return None if None in verdicts else True


def profile_verdict(prof, bit, name, rules, l, svc_pids, env):
    if not prof.get("enabled", True):
        return "nofw", f"firewall off ({name})"
    if prof.get("block_all"):
        return "blocked", f"block all incoming ({name})"
    if prof.get("inbound") == 1:
        return "open", f"default allow ({name})"
    blocked, allow_any, allow_some, block_some, unknown = None, None, [], [], None
    for r in rules:
        pf = r.get("pf", 0x7FFFFFFF)
        if not pf & bit:
            continue
        m = rule_match(r, l, svc_pids, env)
        if m is False:
            continue
        if m is None:
            if r.get("a") == 1 and unknown is None:  # an unreadable ALLOW might open it; an unreadable BLOCK cannot make it worse
                unknown = r
            continue
        everyone = (r.get("ra") or "*") in ("*", "LocalSubnet")
        if r.get("a") == 0:
            if everyone:
                blocked = blocked or r
            else:
                block_some.append(r["ra"])
        elif everyone:
            allow_any = allow_any or r
        else:
            allow_some.append(r["ra"])
    if blocked:
        return "blocked", f"rule \"{blocked.get('n', '?')}\" blocks it ({name})"
    if allow_any:
        if block_some:
            return "filtered", "open except " + ", ".join(block_some)[:24]
        return "open", f"rule \"{allow_any.get('n', '?')}\" ({name})"
    if allow_some:
        return "filtered", "only " + ", ".join(dict.fromkeys(allow_some))[:30]
    if unknown:
        return "unknown", f"rule \"{unknown.get('n', '?')}\" not understood ({name})"
    return "blocked", f"default block ({name})"


def fw_verdict(fw, l, svc_pids, env=None):
    """(state, note) for one listener, over every active profile (the most exposed wins)."""
    env = env if env is not None else {k.upper(): v for k, v in os.environ.items()}
    if not fw:
        return "unknown", "firewall unreadable"
    current = fw.get("current") or 0
    active = [(b, n) for b, n in PROFILES if current & b] or list(PROFILES)  # unknown network profile: consider all
    best = None
    for bit, name in active:
        v = profile_verdict((fw.get("profiles") or {}).get(str(bit), {}), bit, name, fw.get("rules") or [], l, svc_pids, env)
        if best is None or EXPOSED[v[0]] > EXPOSED[best[0]]:
            best = v
    if fw.get("policy") and best[0] in ("blocked", "filtered"):
        return "unknown", "group policy rules not read"
    return best


def service_pids(fw):
    out = {}
    for s in (fw or {}).get("services") or []:
        out.setdefault(str(s.get("n", "")).lower(), set()).add(s.get("pid"))
    return out


def fw_summary(fw):
    """What the FIREWALL section shows: per-profile state, active profiles and networks, rule counts, policy."""
    current = fw.get("current") or 0
    profiles = {name: dict((fw.get("profiles") or {}).get(str(bit), {}), active=bool(current & bit)) for bit, name in PROFILES}
    rules = fw.get("rules") or []
    return {"kind": "windows", "name": "Windows Firewall", "profiles": profiles,
            "off": [n for n, p in profiles.items() if p.get("active") and not p.get("enabled", True)],
            "networks": [{"alias": x.get("alias", "?"), "category": x.get("cat", "?")} for x in fw.get("networks") or []],
            "allow_rules": sum(r.get("a") == 1 for r in rules), "block_rules": sum(r.get("a") == 0 for r in rules),
            "policy": bool(fw.get("policy"))}


# ---- sections ----------------------------------------------------------------------------------------------------------

def proc_name(path, pid, svc_by_pid):
    """'C:\\...\\svchost.exe' + the service it hosts -> 'svchost/Dnscache'; 'C:\\...\\sshd.exe' -> 'sshd'."""
    if pid == 4:
        return "System"
    base = ntpath.basename(path or "")
    base = base[:-4] if base.lower().endswith(".exe") else base
    if base.lower() == "svchost" and svc_by_pid.get(pid):
        return f"svchost/{sorted(svc_by_pid[pid])[0]}"
    return base


def listeners_from(tcp, udp, images, fw, env=None, tokens=None):
    """Raw socket tables -> [{'proto','addr','port','proc','fw'}] like the Linux `ss` rows, plus the firewall verdict."""
    tokens = tokens or {}
    svc_pids = service_pids(fw)
    svc_by_pid = {}
    for name, pids in svc_pids.items():
        for p in pids:
            svc_by_pid.setdefault(p, set()).add(name)
    out, seen = [], set()
    socks = [("tcp", s) for s in tcp if s["state"] == 2] + [("udp", s) for s in udp if s["port"] < DYNAMIC_UDP]
    for proto, s in socks:
        key = (proto, s["addr"], s["port"])
        if key in seen:
            continue
        seen.add(key)
        path = images.get(s["pid"], "")
        lst = {"proto": proto, "addr": s["addr"], "port": s["port"], "pid": s["pid"], "path": path, "token": tokens.get(s["pid"])}
        state, note = fw_verdict(fw, lst, svc_pids, env) if fw else ("unknown", "firewall unreadable")
        out.append({"proto": proto, "addr": s["addr"], "port": s["port"], "proc": proc_name(path, s["pid"], svc_by_pid),
                    "fw": [state, note]})
    return out


def images_of(pids):
    return {p: winapi.process_image(p) for p in set(pids)}


def listeners(fw):
    tcp, udp = winapi.tcp_table(), winapi.udp_table()
    pids = {s["pid"] for s in tcp + udp}
    return listeners_from(tcp, udp, images_of(pids), fw, tokens={p: winapi.process_token(p) for p in pids})


def established():
    """ESTABLISHED TCP connections with the owning process: [{'l_addr','l_port','p_addr','p_port','proc'}]."""
    rows = [s for s in winapi.tcp_table() if s["state"] == winapi.TCP_ESTABLISHED]
    images = images_of([s["pid"] for s in rows])
    return [{"l_addr": s["addr"], "l_port": s["port"], "p_addr": s["raddr"], "p_port": s["rport"],
             "proc": proc_name(images.get(s["pid"], ""), s["pid"], {})} for s in rows]


def local_addrs():
    try:
        return {ai[4][0].split("%")[0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return set()


def journal_from_events(events, cap=500):
    """System event log entries (Level 1 critical, 2 error, 3 warning) -> the shape of the Linux journal summary."""
    idents, err, warn = {}, 0, 0
    for e in events or []:
        pr = {1: 2, 2: 3}.get(e.get("l"), 4)  # syslog priorities: 2 crit, 3 err, 4 warning
        err, warn = (err + 1, warn) if pr <= 3 else (err, warn + 1)
        x = idents.setdefault(e.get("p") or "?", {"n": 0, "pr": 7, "last": ""})
        x["n"] += 1
        x["pr"] = min(x["pr"], pr)
        x["last"] = x["last"] or str(e.get("m") or "")[:120]  # newest first: keep the most recent message
    top = sorted(({"id": k, **v} for k, v in idents.items()), key=lambda x: (x["pr"], -x["n"]))[:10]
    return {"err": err, "warn": warn, "capped": len(events or []) >= cap, "top": top}


def boot_sections(data):
    """PowerShell boot JSON -> {'btime', 'analyze', 'failed', 'deps', 'enabled', 'journal'} (analyze None when not recorded).

    deps: failed service -> the services that depend on it (DependentServices); a service whose list could not be read
    has no key (unknown, not "none")."""
    svcs = data.get("services") or []
    # Automatic services that stopped with an error. 1077 = never started since boot (trigger/delayed start): not a failure.
    failed = sorted(s["n"] for s in svcs if s.get("s") == "Stopped" and s.get("e") not in (0, 1077, None))
    enabled = [{"unit": s["n"], "state": "active" if s.get("s") == "Running" else "failed" if s["n"] in failed else "inactive"}
               for s in sorted(svcs, key=lambda s: s["n"].lower())]
    perf = data.get("perf")
    analyze = None
    if perf and perf.get("total"):
        parts = {"main path": perf.get("main", 0) / 1000, "post boot": perf.get("post", 0) / 1000}
        analyze = {"parts": {k: v for k, v in parts.items() if v > 0}, "total": perf["total"] / 1000}
    raw = data.get("deps") if isinstance(data.get("deps"), dict) else {}
    deps = {n: ([v] if isinstance(v, str) else [str(x) for x in v or []])[:20] for n, v in raw.items() if n in failed}
    return {"btime": int(data.get("boot") or time.time()), "analyze": analyze, "failed": failed, "deps": deps, "enabled": enabled,
            "journal": journal_from_events(data.get("events"))}
