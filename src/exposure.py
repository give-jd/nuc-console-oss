"""The exposure model (stdlib only, Python 3.8+): what is reachable on this machine, and from where.

From the collector's state (net.json, containers.json) to rows of ports with a verdict per way in (this machine, the LAN, the
tailnet, the Internet), the declared reach of [expose] against the real one, the web apps, and the baseline comparison. Console,
web and MAP all draw from these rows; nothing here draws, reads a file or knows the size of a screen. The words in a row's note
are what both surfaces show.

[expose], [webapps] and the [features] switches are read through nuc_config.current(), the one configuration dict of the process.
"""
import ipaddress
import re

import nuc_config
from ui import safe


def _on(feature):
    """Section enabled in config.ini (default: yes): render.on()'s answer, from the same dict."""
    return nuc_config.current()["features"].get(feature, True)


TS4, TS6 = ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")
PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
                                                  "100.64.0.0/10", "::1/128", "fe80::/10", "fc00::/7")]


def is_private_addr(addr):
    """LAN, loopback, link-local, Tailscale (100.64/10, fd7a::/48 in fc00::/7). Explicit list: Python's `is_private`
    also includes documentation ranges (e.g. 203.0.113.0/24), which are not local at all."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return any(ip in n for n in PRIVATE_NETS)


# network sections the exposure classification depends on: if one is missing, the baseline comparison is not reliable.
# The others (Tailscale, fail2ban, drops, databases...) are secondary: an error there must not silence the port alarms.
EXPOSURE_SECTIONS = frozenset(("listeners", "ufw", "docker_user", "serve", "firewall"))
# processes that listen on behalf of containers: docker-proxy (Linux), the Docker Desktop / OrbStack / Rancher backends
DOCKER_PROXIES = {"docker-proxy", "com.docker.backend", "com.docker.vpnkit", "vpnkit", "vpnkit-bridge", "com.docker.proxy",
                  "OrbStack Helper", "limactl", "rancher-desktop"}  # not wslrelay: it forwards any WSL port, not only containers


def os_of(d):
    """Which OS wrote a state file: 'linux' for files written before the field existed."""
    return (d or {}).get("os") or "linux"


def exposure_partial(net):
    return bool(set((net or {}).get("errors") or {}) & EXPOSURE_SECTIONS)


# typical database/broker ports: exposed to the LAN they are the case to flag
SENSITIVE = {3306, 5432, 5433, 5447, 5984, 6379, 6381, 9200, 27017, 1883, 9001, 18086, 8086}


def bind_scope(addr):
    """Where a connection can come from, looking only at the bind address."""
    if addr in ("*", "", "0.0.0.0", "::"):
        return "wild"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "lan"
    return "lo" if ip.is_loopback else "ts" if ip in TS4 or ip in TS6 else "lan"


def rule_match(to, port, proto):
    """(result, interface). Result: True/False, 'all' (Anywhere) or None = rule that cannot be interpreted."""
    to = to.replace(" (v6)", "").strip()
    iface = ""
    m = re.search(r"\s+on\s+(\S+)$", to)
    if m:
        iface, to = m.group(1), to[:m.start()].strip()
    spec, _, pr = to.partition("/")
    if pr and pr != proto:
        return False, iface
    if spec == "Anywhere":
        return "all", iface
    if spec == "OpenSSH":  # ufw's default profile: 22/tcp
        return port == 22 and proto == "tcp", iface
    if re.fullmatch(r"[\d,:]+", spec):  # 22, 80,443, 8000:8010
        for item in spec.split(","):
            lo, _, hi = item.partition(":")
            if lo.isdigit() and int(lo) <= port <= int(hi or lo):
                return True, iface
        return False, iface
    return None, iface  # application profiles, destinations with an IP...


def fw_verdict(port, proto, ufw):
    """(state, note). State: open | filtered | blocked | nofw | unknown.

    Safety rule: whatever cannot be interpreted is 'unknown' (treated as exposed), never 'blocked'.
    First match wins, as in ufw. Rules for the tailscale* interface concern the tailnet, not the LAN.
    ponytail: reads `ufw status verbose`, not the real iptables rules; rules written outside ufw are not seen.
    """
    if ufw is None:
        return "unknown", "ufw n/a"
    if not ufw.get("active"):
        return "nofw", "ufw off"
    d = ufw.get("default", "")
    if "deny (incoming)" not in d and "reject (incoming)" not in d:
        return "open", "default allow"
    allow_src, deny_src, unknown, v6_allow = [], [], False, False
    for r in ufw.get("rules", []):
        act = r["action"]
        allow, deny = act.startswith(("ALLOW", "LIMIT")), act.startswith(("DENY", "REJECT"))
        if not (allow or deny) or "OUT" in act or "FWD" in act:
            continue
        m, iface = rule_match(r["to"], port, proto)
        if m is False or iface.startswith("tailscale"):
            continue
        if m is None:
            unknown = True
            continue
        src = r["from"].replace(" (v6)", "")
        if re.search(r"\s\d", src):  # source with a port ("Anywhere 53"): not interpreted
            unknown = True
            continue
        if "(v6)" in r["to"] + r["from"]:
            v6_allow = v6_allow or allow
            continue
        if src.startswith("Anywhere"):
            if allow:
                return ("filtered", "open except " + ", ".join(deny_src)[:24]) if deny_src else ("open", "open")
            if allow_src:
                break
            return "blocked", "blocked (deny)"
        (allow_src if allow else deny_src).append(src)
    if allow_src:
        return "filtered", "only " + ", ".join(dict.fromkeys(allow_src))[:30]
    if unknown:
        return "unknown", "rule not understood"
    if v6_allow:
        return "unknown", "IPv6 rule only"
    return "blocked", "blocked"


def docker_verdict(du):
    """(cell, note, red_note) for a port published by Docker.

    Rules in DOCKER-USER see the *container* port (DNAT already done), not the published one: a rule with
    --dport cannot be attributed to the port shown, hence 'unknown'. Only a DROP/REJECT without --dport is certain.
    """
    if du is None:
        return 3, "docker: DOCKER-USER n/a", False
    if not du:
        return 1, "docker: bypasses ufw", True
    blanket, unclear = False, False
    for rule in du:
        target = rule.rpartition("-j ")[2].strip()
        if target.startswith(("DROP", "REJECT")):
            if "--dport" in rule or "--dports" in rule:
                unclear = True
            else:
                blanket = True
        elif target not in ("ACCEPT", "RETURN"):
            unclear = True  # jump to an external chain (e.g. ufw-user-forward)
    if blanket:
        return 2, f"docker: DROP in DOCKER-USER ({len(du)})", False
    if unclear:
        return 3, "docker: per-port/chain rules, check by hand", False
    return 1, f"docker: DOCKER-USER without DROP ({len(du)})", True


CELL = {"open": 1, "nofw": 1, "filtered": 2, "blocked": 0, "unknown": 3}
# UDP discovery ports that every browser or OS component binds at the same time (macOS/Windows): one stable name, or the
# "service" of the port would flip between chrome and msedge at every pass and raise CHANGED alarms
SHARED_UDP = {5353: "mDNS", 5355: "LLMNR", 1900: "SSDP", 3702: "WS-Discovery", 137: "NetBIOS", 138: "NetBIOS"}
EXPOSED_RANK = {"open": 4, "nofw": 4, "unknown": 3, "filtered": 2, "blocked": 1}


def exposure_rows(net, cont):
    """One row per (port, proto, bind class): 8443 on loopback and 8443 on Tailscale are different services.

    ponytail: the TS column assumes tailscaled has its `ts-input` rules (accept everything from tailscale0
    before ufw): a reachable bind is open to tailnet peers regardless of ufw. Check with
    `iptables -S ts-input`; holds only if Tailscale's netfilter mode is on (default).
    The container name is looked up by port only (two containers on the same port with different IPs: the first wins).
    """
    ls, ufw, du = net.get("listeners") or [], net.get("ufw"), net.get("docker_user")
    # macOS/Windows: the collector already judged each socket against the firewall (it works per program there); the same
    # firewall filters the Tailscale interface too, and Docker Desktop's port proxy is an ordinary program behind it
    native = os_of(net) != "linux"
    serve_err = "serve" in (net.get("errors") or {})
    serve = {x["port"]: x for x in net.get("serve") or []}
    funnel_ports = {x["port"] for x in net.get("serve") or [] if x["funnel"]}
    published = {}
    for ct in (cont or {}).get("containers", []):
        if ct["state"] == "running":
            for p in ct["ports"]:
                if isinstance(p["p"], int):
                    published.setdefault((p["p"]), ct["name"])
    rows = {}
    for l in ls:
        sc = bind_scope(l["addr"])
        r = rows.setdefault((l["port"], l["proto"], sc), {"proc": "", "fw": None, "procs": set()})
        r["proc"] = r["proc"] or l["proc"]
        if l["proc"]:
            r["procs"].add(l["proc"])
        v = l.get("fw")
        if v and v[0] in CELL and (r["fw"] is None or EXPOSED_RANK[v[0]] > EXPOSED_RANK[r["fw"][0]]):
            r["fw"] = v  # IPv4 and IPv6 sockets of one port: the most exposed verdict counts
    # ports published by Docker with no listening socket (userland-proxy disabled): hidden from ss
    have = {(port, proto) for (port, proto, _sc) in rows}
    for ct in (cont or {}).get("containers", []):
        if ct["state"] != "running":
            continue
        for p in ct["ports"]:
            if isinstance(p["p"], int) and (p["p"], "tcp") not in have:
                sc = "lo" if p["s"] == "lo" else "wild" if p["s"] == "*" else bind_scope(p["s"])
                rows.setdefault((p["p"], "tcp", sc), {"proc": "docker-proxy", "fw": None, "procs": set()})
    out = []
    for (port, proto, sc), r in rows.items():
        via_docker = r["proc"] in DOCKER_PROXIES or (not r["proc"] and port in published)
        name = published.get(port, "container?") if via_docker else (r["proc"] or "?")
        if native and not via_docker and proto == "udp" and port in SHARED_UDP:
            name = f"{SHARED_UDP[port]} ({', '.join(sorted(r['procs'])) or '?'})"
        elif native and not via_docker and len(r["procs"]) > 1:  # several programs on one port: the same name whatever the order
            name = ", ".join(sorted(r["procs"]))
        sv = serve.get(port) if sc == "ts" else None
        if sv:
            name = f"{'funnel' if sv['funnel'] else 'serve'} {sv['path']} → " + sv["target"].replace("http://", "")
        lan_cell, note, bad = 0, "", False
        ts_cell = 1 if sc in ("wild", "ts") else 0
        if native and sc in ("wild", "lan", "ts"):
            state, fnote = r["fw"] or ("unknown", "firewall n/a")
            if sc == "ts":
                ts_cell, note = CELL[state], "tailnet only" + ("" if state in ("open", "nofw") else " · " + fnote)
            else:
                lan_cell, note, bad = CELL[state], fnote, state in ("unknown", "nofw")
                ts_cell = CELL[state] if sc == "wild" else 0
        elif sc in ("wild", "lan"):
            if via_docker:
                lan_cell, note, bad = docker_verdict(du)
            else:
                state, fnote = fw_verdict(port, proto, ufw)
                lan_cell = CELL[state]
                note = "LAN " + fnote if not fnote.startswith("ufw") else fnote
                bad = state in ("unknown", "nofw")
        elif sc == "ts":
            note = "tailnet only"
        net_cell = 1 if port in funnel_ports and sc in ("ts", "wild") else 3 if serve_err and sc == "ts" else 0
        if net_cell == 1:
            note, bad = "FUNNEL: public", True
        out.append({"port": port, "proto": proto, "name": safe(name), "loc": 0 if sc == "ts" else 1,
                    "lan": lan_cell, "ts": ts_cell, "net": net_cell,
                    "note": safe(note), "bad_note": bad, "warn": port in SENSITIVE and lan_cell in (1, 3)})
    out.sort(key=lambda x: (-(x["net"] == 1), -(x["lan"] in (1, 3)), -x["ts"], -x["warn"], x["port"], x["proto"]))
    return out


def exposure_keys(net, cont):
    """{'22/t:LAN': {'name': service, 'lan': filter state}} of ports reachable from outside only; None if unknown.

    Comparing service and filter state too (open/filtered) avoids missing a rule change or a change of
    the process listening on the same port. tailscaled's ephemeral ports (>= 32768, except the fixed
    41641) are excluded: they change at every start and would raise false alarms.
    ponytail: does not tell TCP/UDP apart for Docker-published ports, and the exact name 'tailscaled' is trusted.
    """
    if net is None or net.get("listeners") is None:
        return None
    # macOS/Windows: a shared discovery port is that protocol, whichever browser happens to hold it now
    stable = lambda r: SHARED_UDP[r["port"]] if os_of(net) != "linux" and r["proto"] == "udp" and r["port"] in SHARED_UDP else r["name"]  # noqa: E731
    return {f"{r['port']}/{r['proto'][0]}:{group_of(r)}": {"name": stable(r), "lan": r["lan"]}
            for r in exposure_rows(net, cont)
            if group_of(r) != "LOCALE" and not (r["name"] == "tailscaled" and r["port"] >= 32768 and r["port"] != 41641)}


def name_change(old, new, width=20):
    """'old → new' starting where the two names start to differ (a plain cut at 20 chars showed identical prefixes)."""
    old, new = safe(old), safe(new)
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    start = max(0, p - 6)  # a little context before the first difference
    lead = "…" if start else ""
    return f"{lead}{old[start:start + width]} → {lead}{new[start:start + width]}"


def baseline_diff(cur, base):
    """(new, gone, changed) against the accepted baseline; 'changed' = same port/group but another service,
    or a LAN filter that went from 'by source' to 'open to all'."""
    old = base["ports"]
    val = lambda v: v if isinstance(v, dict) else {"name": v, "lan": None}
    new = {k: v for k, v in cur.items() if k not in old}
    gone = {k: val(v) for k, v in old.items() if k not in cur}
    changed = {}
    # a shared discovery port now named by its protocol (macOS/Windows): whatever program a baseline recorded there is the same
    renamed = lambda k, new: new in SHARED_UDP.values() and "/u:" in k  # noqa: E731
    for k, v in cur.items():
        if k in old:
            o = val(old[k])
            if o["name"] != v["name"] and _on("containers") and not renamed(k, v["name"]):  # containers off: names can't be resolved
                changed[k] = "service " + name_change(o["name"], v["name"])
            elif o["lan"] == 2 and v["lan"] in (1, 3) and _on("firewall"):  # firewall off: verdict unknown, not a rule change
                changed[k] = "was filtered by source, now open to the whole LAN"
    return new, gone, changed


def new_ports(net, cont, baseline):
    """{key: 'NEW'|'CHANGED'}; empty if it cannot be computed or data is partial (never an exception)."""
    try:
        if isinstance(baseline, dict) and net and net.get("listeners") is not None and not exposure_partial(net):
            new, _, changed = baseline_diff(exposure_keys(net, cont) or {}, baseline)
            return {**{k: "NEW" for k in new}, **{k: "CHANGED" for k in changed}}
    except Exception:  # noqa: BLE001
        pass
    return {}


GROUPS = (("INTERNET", "Reachable from the Internet (Tailscale Funnel)"),
          ("LAN", "Open on the LAN (and on Tailscale)"),
          ("TAILNET", "Tailnet only"),
          ("LOCALE", "This machine only"))  # keys are stored in baseline.json: never rename them


def group_of(r):
    return "INTERNET" if r["net"] == 1 else "LAN" if r["lan"] in (1, 2, 3) else "TAILNET" if r["ts"] else "LOCALE"


INFRA_PROCS = {"sshd", "tailscaled", "systemd-resolve", "systemd-resolved", "cupsd", "avahi-daemon", "chronyd", "rpcbind", "dnsmasq", "named",
               # Windows and macOS system services that listen on their own (not web apps)
               "System", "svchost", "lsass", "wininit", "services", "spoolsv", "launchd", "mDNSResponder", "rapportd", "ControlCenter",
               "sharingd", "remoted", "configd", "netbiosd", "Tailscale", "tailscale-ipn"}
REACH_ORDER = ("INTERNET", "LAN", "TAILNET", "LOCALE")


# ---- [expose]: the widest reach you intend for a service, against the reach it has --------------------------------------------
EXPOSE_LABEL = {"INTERNET": "Internet", "LAN": "LAN", "TAILNET": "tailnet", "LOCALE": "local"}  # a reach in the words of [expose]


def expose_policy(policy=None):
    """[(key, (port, proto) or None, group)] of [expose]: keys lowercased; a reach that does not exist or a port that cannot exist is left out."""
    out = []
    for k, v in (nuc_config.current()["expose"] if policy is None else policy).items():
        k = str(k).lower()
        try:
            if v in REACH_ORDER:
                out.append((k, nuc_config.expose_port(k), v))
        except ValueError:
            pass  # nuc_config said so when it read the file
    return out


def _ct_names(name, project="", service=""):
    """The names a container answers to in [expose]: its own, without a replica number (shop-db-1 -> shop-db), its compose project and service."""
    out = {name, re.sub(r"[-_]\d+$", "", name), project, service}
    if project and service:
        out |= {f"{project}-{service}", f"{project}_{service}"}
    return {x.lower() for x in out if x}


def _unit_names(unit):
    u = str(unit).lower()
    return {u, u[:-len(".service")]} if u.endswith(".service") else {u}


def expose_cts(net, cont):
    """{container name: the names it answers to}: every container listed (stopped ones too) and every database (its kind too)."""
    cts = {}

    def add(name, project="", service="", *more):
        cts[name] = cts.get(name, set()) | _ct_names(name, project, service) | set(more)

    for ct in (cont or {}).get("containers") or []:
        if isinstance(ct, dict) and ct.get("name"):
            add(ct["name"], ct.get("project") or "")
    for ln in ((net or {}).get("links") or {}).get("containers") or []:
        if isinstance(ln, dict) and ln.get("name"):
            add(ln["name"], ln.get("project") or "", ln.get("service") or "")
    for it in ((net or {}).get("dbs") or {}).get("items") or []:
        if isinstance(it, dict) and it.get("name"):
            add(it["name"], it.get("project") or "", "", *([str(it["kind"]).lower()] if it.get("kind") else []))
    return cts


def expose_apply(rows, net, cont, policy=None, webapps=None):
    """Puts the [expose] verdict on each exposure row (in place): r["want"] = the widest reach intended for it (a group name),
    r["key"] = the config key that says so. Rows no key matches get nothing (and with no [expose] at all nothing is touched).

    A key is a port (8080, 8080/udp) or a name: the row's service, the container behind it (its name without the replica number,
    compose project and service, database name and kind), the process and unit that listen, a [webapps] name. Behind a
    Funnel/Serve row it is what listens on the backend (row_owners: the map and this check agree on who is behind a row).
    Several keys on one row: the most restrictive reach wins."""
    pol = expose_policy(policy)
    if not pol or not rows or (net or {}).get("listeners") is None:
        return rows
    webapps = nuc_config.current()["webapps"] if webapps is None else webapps
    cts = expose_cts(net, cont)
    for r, (owners, ports) in zip(rows, row_owners(net, cont, rows)):
        names = set() if r["name"] in ("?", "container?") or r["name"].startswith(("funnel ", "serve ")) else {r["name"].lower()}
        for o in owners:
            kind, _, who = o.partition(":")
            if kind == "ct":
                names |= cts.get(who) or _ct_names(who)
            elif kind == "proc":  # its unit is the one that listens on one of this row's ports (another service may run the same program)
                names.add(who.lower())
                for ln in net["listeners"]:
                    if ln.get("proc") == who and ln.get("port") in ports and ln.get("unit"):
                        names |= _unit_names(ln["unit"])
        if r["proto"] == "tcp":
            names |= {n.lower() for n, ps in webapps.items() if ports & set(ps)}
        hit = [(REACH_ORDER.index(g), -i, k) for i, (k, pk, g) in enumerate(pol) if (((pk[1] == r["proto"] == "tcp" and pk[0] in ports) or pk == (r["port"], r["proto"])) if pk else k in names)]
        r["want"], r["key"] = (REACH_ORDER[max(hit)[0]], max(hit)[2]) if hit else (None, None)  # the highest index is the narrowest reach
    return rows


def _expose_reach(r):
    return "INTERNET" if r["net"] == 3 else group_of(r)  # Funnel status unreadable (net 3): unknown is treated as open


def expose_over(r):
    """True when an exposure row (after expose_apply) reaches further than [expose] says. A row no key matches never does."""
    return bool(r.get("want")) and REACH_ORDER.index(_expose_reach(r)) < REACH_ORDER.index(r["want"])


def expose_note(r):
    """The [expose] marker of an exposure row: ('beyond config.ini: local', True), ('expected: LAN', False), None if no key matches."""
    if not r.get("want"):
        return None
    return (f"beyond config.ini: {EXPOSE_LABEL[r['want']]}", True) if expose_over(r) else (f"expected: {EXPOSE_LABEL[r['want']]}", False)


def expose_over_items(rows):
    """['shop-db :5432 LAN > local', ...]: what reaches further than [expose] says, widest first, once per service and port."""
    items = {}
    for r in rows:
        if expose_over(r):
            pk = nuc_config.expose_port(r["key"])
            udp = "/udp" if r["proto"] == "udp" else ""
            who = f"port {pk[0]}" + ("/udp" if pk[1] == "udp" else "") if pk else r["key"]  # a port key is named by itself: the process behind it can change
            at = "" if pk == (r["port"], r["proto"]) else f" :{r['port']}{udp}"  # (a port key that follows a Funnel to its backend: the row's own port too)
            items.setdefault((REACH_ORDER.index(_expose_reach(r)), who, r["port"], r["proto"]),
                             f"{safe(who)}{at} {EXPOSE_LABEL[_expose_reach(r)]} > {EXPOSE_LABEL[r['want']]}")
    return [items[k] for k in sorted(items)]


def expose_unmatched(net, cont, boot=None, policy=None, webapps=None):
    """The [expose] names that match nothing this machine knows (a typo guards nothing). Port keys are never listed: a port nobody
    listens on is fine. Known: containers (stopped ones too), compose projects and services, the processes and units that listen,
    databases (name, kind), [webapps], the units enabled at boot."""
    webapps = nuc_config.current()["webapps"] if webapps is None else webapps
    known = {n.lower() for n in webapps}
    for names in expose_cts(net, cont).values():
        known |= names
    for ln in (net or {}).get("listeners") or []:
        known |= {str(ln.get("proc") or "").lower()} | _unit_names(ln.get("unit") or "")
    boot = boot if isinstance(boot, dict) else {}
    known |= {n for u in boot.get("enabled") or [] if isinstance(u, dict) for n in _unit_names(u.get("unit") or "")}
    known |= {n for u in boot.get("failed") or [] for n in _unit_names(u)}
    return [k for k, pk, _ in expose_policy(policy) if pk is None and k not in known]


def webapp_rows(net, cont):
    """Web apps: the ones you declared under [webapps] (up or down) and the listeners found on their own.

    -> [{name, ports, state: 'up'|'down', reach: INTERNET|LAN|TAILNET|LOCALE|None, expected: bool}] sorted for display."""
    rows = exposure_rows(net, cont) if net and net.get("listeners") is not None else []
    db_names = {it["name"] for it in ((net or {}).get("dbs") or {}).get("items", [])}
    by_port = {}
    for r in rows:
        by_port.setdefault(r["port"], []).append(r)
    widest = lambda rs: min((group_of(r) for r in rs), key=REACH_ORDER.index) if rs else None
    out, used = [], set()
    for name, ports in nuc_config.current()["webapps"].items():
        hit = [r for p in ports for r in by_port.get(p, [])]
        out.append({"name": name, "ports": list(ports), "state": "up" if hit else "down", "reach": widest(hit), "expected": True})
        used |= set(ports)
    found = {}
    desktop = os_of(net) != "linux"  # macOS/Windows desktops: dozens of apps listen on 127.0.0.1, list only what is reachable
    for r in rows:
        if desktop and group_of(r) == "LOCALE":
            continue
        if (r["port"] in used or r["port"] in SENSITIVE or r["port"] == 22 or r["proto"] != "tcp" or r["name"] in INFRA_PROCS
                or r["name"].startswith("svchost/") or r["name"] in db_names or r["name"] == "?"):
            continue
        found.setdefault(r["name"], []).append(r)
    for name, rs in found.items():
        if name.startswith(("funnel ", "serve ")):
            name = " ".join(name.split()[:2])  # 'funnel /webhook → 127.0.0.1:…' -> 'funnel /webhook'
        out.append({"name": name, "ports": sorted({r["port"] for r in rs}), "state": "up", "reach": widest(rs), "expected": False})
    rank = lambda x: (not x["expected"], x["state"] != "up", REACH_ORDER.index(x["reach"]) if x["reach"] else 9, x["name"])
    return sorted(out, key=rank)


# ---- who answers behind a row: the container that publishes the port, the process that listens, what a Serve/Funnel proxies to ----

TARGET = re.compile(r"^(?:([a-z][a-z0-9+.-]*)://)?(\[[^\]]*\]|[^/:?#]*)(?::(\d+))?(?:[/?#]|$)", re.I)  # scheme, host, port


def _pub_scope(p):
    """The bind scope (bind_scope) of a port published by Docker, read as exposure_rows reads it."""
    s = p.get("s") or "*"
    return "lo" if s == "lo" else "wild" if s == "*" else bind_scope(s)


def _scopes(r, taken=()):
    """The bind scopes an exposure row comes from: exposure_rows keeps one row per (port, proto, scope), and where the row landed
    tells which. 'This machine only' is loopback, or a bind the firewall blocks: what the port's other rows have not taken."""
    if not r["loc"]:
        return {"ts"}
    group = group_of(r)
    if group != "LOCALE":
        return {"wild", "lan"} if group == "LAN" else {"wild"}
    return {"lo", "lan", "wild"}.difference(taken)


def _owners(net, cont, port, proto, scopes=None):
    """Who answers on a host port: the container that publishes it, else the processes listening on it. Only those bound
    where the entry is (scopes); when no socket of the port is bound there, the scope cannot be told and all are kept."""
    pub = [(ct["name"], _pub_scope(p)) for ct in (cont or {}).get("containers") or [] if ct.get("state") == "running"
           for p in ct.get("ports") or [] if p.get("p") == port]
    ls = [(ln.get("proc") or "", bind_scope(ln.get("addr"))) for ln in (net or {}).get("listeners") or []
          if ln.get("port") == port and ln.get("proto") == proto]
    if scopes is not None and any(sc in scopes for _, sc in pub + ls):
        pub, ls = [x for x in pub if x[1] in scopes], [x for x in ls if x[1] in scopes]
    if pub:
        return ["ct:" + pub[0][0]]
    procs = []
    for proc, _ in ls:
        if proc and proc not in DOCKER_PROXIES and "proc:" + proc not in procs:
            procs.append("proc:" + proc)
    return procs


def _target(t):
    """(host, port) a Serve/Funnel handler proxies to; None for a file, a text, or a target without a port to tell."""
    m = TARGET.match(str(t or ""))
    if not m or not m.group(2):
        return None
    scheme, host, port = m.groups()
    if port is None and not scheme:
        return None
    return host.strip("[]").lower(), int(port) if port else 443 if scheme.lower().startswith("https") else 80


def _local(host, own):
    """True if a Serve target is this machine: localhost, loopback, or one of its own addresses."""
    if host == "localhost" or host in own:
        return True
    try:
        a = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return a.is_loopback or a.is_unspecified


def serve_by_port(net):
    """{port: [handler]} of Tailscale Serve/Funnel: a port can have several handlers (paths), each with a backend of its own."""
    serve = {}
    for x in net.get("serve") or []:
        if isinstance(x, dict):
            serve.setdefault(x.get("port"), []).append(x)
    return serve


def row_owners(net, cont, rows):
    """[(owners, ports)] of each exposure row. Owners: the nodes ('ct:<container>', 'proc:<process>', 'ext:<host>') that answer
    behind it: the container that publishes the port, else the processes listening on it, else (Serve/Funnel) what listens on
    the backend. Ports: the local ports it stands for (its own, and the backend's behind a Serve/Funnel).
    The map draws the owners; the [expose] check (expose_apply) names a service by them (the one behind a Funnel is the one declared)."""
    serve = serve_by_port(net)
    taken = {}  # (port, proto) -> the bind scopes of its rows that are not 'this machine only'
    for r in rows:
        if group_of(r) != "LOCALE":
            taken.setdefault((r["port"], r["proto"]), set()).update(_scopes(r))
    me = (net.get("ts_peers") or {}).get("self")
    own = {str(ln.get("addr")) for ln in net.get("listeners") or []}  # this machine: where it listens, its tailnet addresses
    own.update((me.get("ips") or []) if isinstance(me, dict) else [])
    out = []
    for r in rows:
        svs = serve.get(r["port"], []) if r["name"].startswith(("funnel ", "serve ")) else []
        scopes = _scopes(r, taken.get((r["port"], r["proto"]), ()))
        owners = [] if svs else _owners(net, cont, r["port"], r["proto"], scopes)
        ports = {r["port"]}
        for sv in svs:  # what listens on a local target; another host is a remote address, not what listens on its port here
            t = _target(sv.get("target"))
            if t and _local(t[0], own):
                found = _owners(net, cont, t[1], "tcp", {"wild", "lo" if t[0] == "localhost" else bind_scope(t[0])})
                ports.add(t[1])
            else:
                found = ["ext:" + t[0]] if t else []
            owners += [o for o in found if o not in owners]
        out.append((owners, ports))
    return out
