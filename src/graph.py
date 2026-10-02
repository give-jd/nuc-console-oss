"""nuc-console MAP: who can reach what on this machine, and what is behind it. The model only: no colours, no I/O.

Nodes are the zones traffic comes from (Internet, LAN, tailnet, this machine), the ports that let it in, the containers and
host processes behind them, the peers seen connected, compose stacks, failed units and declared web apps that are down.
Every edge says how sure it is: 'bind' (structure: a zone reaches a port, a port belongs to a process), 'seen' (a live
connection was observed), 'declared' (compose depends_on, an env var naming the host, a systemd dependency), 'possible'
(same docker network, nothing else). Missing data is said (G["notes"]), never drawn as "no link".

The console (render.py) and the web view (web.py) draw the same rows: rows() walks the graph as a tree whose open branches
come from a State, and every row has a stable key (a hash of its path from the root) that survives refreshes and fits in a URL.
"""
import hashlib
import ipaddress
import re
import time

import exposure  # the exposure model: what is reachable from where

EV_RANK = {"seen": 3, "declared": 2, "possible": 1, "bind": 0}
STATE_RANK = {"err": 0, "down": 1, "warn": 2, "unknown": 3, "info": 4, "ok": 5}
BAD = ("err", "down", "warn")
# (id, label, what it shows, exposure group of exposure.group_of)
ROOTS = (("root:internet", "INTERNET", "reachable from the Internet (Tailscale Funnel)", "INTERNET"),
         ("root:lan", "LAN", "open on the LAN (and on the tailnet)", "LAN"),
         ("root:tailnet", "TAILNET", "reachable from the tailnet only", "TAILNET"),
         ("root:local", "LOCAL", "this machine only", "LOCALE"),
         ("root:impact", "IMPACT", "what is down or failing, and what depends on it", None),
         ("root:stacks", "STACKS", "compose projects and their containers", None),
         ("root:outbound", "OUTBOUND", "connections this machine opens to other hosts", None))
GROUP_ROOT = {g: rid for rid, _, _, g in ROOTS if g}
CTX = {"root:impact": "rev", "root:outbound": "ext"}  # how a root's subtree is walked; the others follow dependencies
LEGEND = "━━► seen  ╌╌► declared  ┄┄► same network  ◄━━ connected to it"
ARROW = {("out", "seen"): "━━►", ("out", "declared"): "╌╌►", ("out", "possible"): "┄┄►",
         ("in", "seen"): "◄━━", ("in", "declared"): "◄╌╌", ("in", "possible"): "◄┄┄", ("in", "bind"): "◄──"}
EV_WORD = {"seen": "seen", "declared": "declared", "possible": "same network, no evidence", "bind": ""}
KIND_NAME = {"root": "view", "port": "entry port", "ct": "container", "proc": "host process", "ext": "remote address",
             "stack": "compose project", "unit": "service", "webapp": "web app"}
REACH_TEXT = {"INTERNET": "the Internet (Tailscale Funnel)", "LAN": "the LAN and the tailnet", "TAILNET": "the tailnet only",
              "LOCALE": "this machine only"}
CELL_TEXT = {0: "no", 1: "open", 2: "filtered by source", 3: "unknown (treated as open)"}
MAX_DEPTH = 12  # a tree deeper than this is a loop the cycle check missed, or noise
DATA_HOPS = 6   # how far "this entry leads to a database" looks
TS4, TS6 = ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")
CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(s):
    """External data (container names, process names) never carries control characters to a console."""
    return CTRL.sub("?", str(s))


def ago(sec):
    sec = max(0, int(sec))
    return ("now" if sec < 60 else f"{sec // 60}m ago" if sec < 3600 else f"{sec // 3600}h ago" if sec < 172800
            else f"{sec // 86400}d ago")


def path_key(path):
    """Stable id of a row: the same path from the same root gives the same key at every refresh (and in every URL)."""
    return hashlib.sha1("\x1f".join(path).encode("utf-8", "replace")).hexdigest()[:10]


class State(object):
    """Which branches are open. Roots are open unless shut; with `all` every branch is open unless shut."""

    def __init__(self, open=(), all=False, shut=(), only=False):  # noqa: A002 - the names the URLs use
        self.open, self.all, self.shut, self.only = set(open), bool(all), set(shut), bool(only)

    def is_open(self, key, root=False):
        return key not in self.shut and (root or self.all or key in self.open)

    def copy(self):
        return State(self.open, self.all, self.shut, self.only)

    def toggle(self, row):
        if row["open"]:
            self.open.discard(row["key"])
            if self.all or row["depth"] == 0:
                self.shut.add(row["key"])
        else:
            self.shut.discard(row["key"])
            if not self.all and row["depth"]:
                self.open.add(row["key"])

    def toggled(self, row):
        st = self.copy()
        st.toggle(row)
        return st

    def expand_all(self):
        self.all, self.open, self.shut = True, set(), set()

    def collapse_all(self):
        self.all, self.open, self.shut = False, set(), set()


class Rows(list):
    truncated = False  # True when rows() stopped at its limit


# ---- building the graph ------------------------------------------------------------------------------------------------

def _node(G, nid, kind, label, sub="", state="info"):
    n = G["nodes"].get(nid)
    if n is None:
        n = G["nodes"][nid] = {"id": nid, "kind": kind, "label": safe(label), "sub": safe(sub), "state": state,
                               "facts": [], "findings": []}
    return n


def _fact(n, label, value):
    if value not in (None, "", []):
        n["facts"].append((label, safe(value)))


def _find(n, level, text):
    if (level, text) not in n["findings"]:
        n["findings"].append((level, safe(text)))


def _worse(n, state):
    if STATE_RANK[state] < STATE_RANK[n["state"]]:
        n["state"] = state


def _edge(G, src, dst, ev, port=None, n=0, last=None, why=""):
    """One edge per (src, dst): the strongest evidence wins, the others stay in 'why'."""
    if src == dst:
        return None
    e = G["_pair"].get((src, dst))
    if e is None:
        e = G["_pair"][(src, dst)] = {"src": src, "dst": dst, "ev": ev, "port": port, "n": n or 0, "last": last, "why": []}
        G["edges"].append(e)
        G["out"].setdefault(src, []).append(e)
        G["inc"].setdefault(dst, []).append(e)
    else:
        if EV_RANK[ev] > EV_RANK[e["ev"]]:
            e["ev"] = ev
            e["port"] = port if port is not None else e["port"]
        elif e["port"] is None:
            e["port"] = port
        if ev == "seen":
            e["n"] = max(e["n"], n or 0)
            e["last"] = max(e["last"] or 0, last or 0) or None
    if why and why not in e["why"]:
        e["why"].append(safe(why))
    return e


def ext_class(ip, peers=None):
    """(where the address is, tailnet peer name): 'Internet', 'LAN', 'tailnet', 'this machine' or '?'."""
    try:
        a = ipaddress.ip_address(str(ip).strip("[]").split("%")[0])
    except ValueError:
        return "?", ""
    a = getattr(a, "ipv4_mapped", None) or a
    if (a.version == 4 and a in TS4) or (a.version == 6 and a in TS6):
        return "tailnet", (peers or {}).get(str(a), "")
    if a.is_loopback:
        return "this machine", ""
    if exposure.is_private_addr(str(a)):
        return "LAN", ""
    return "Internet", ""


def _ensure(G, nid, peers):
    """A node named by an edge of the collector: an address, a client process, a container not in the lists."""
    if nid in G["nodes"]:
        return G["nodes"][nid]
    kind, _, name = nid.partition(":")
    if kind == "ext":
        zone, peer = ext_class(name, peers)
        n = _node(G, nid, "ext", name, " · ".join(x for x in (zone, peer) if x))
        n["zone"] = zone
        _fact(n, "address", name)
        _fact(n, "where", zone)
        _fact(n, "tailnet peer", peer)
        return n
    if kind == "proc":
        n = _node(G, nid, "proc", name, "host process")
        _fact(n, "listens", "nothing (a client only)")
        return n
    n = _node(G, nid, "ct", name, "container", "unknown")
    _fact(n, "state", "not in the container list (stopped, or removed)")
    return n


def _ct_state(state, status, health):
    if state == "running":
        if health == "unhealthy" or "unhealthy" in status:
            return "err"
        if "Restarting" in status or health == "starting":
            return "warn"
        return "ok"
    if state in ("restarting", "paused"):
        return "warn"
    if state in ("exited", "dead"):
        return "down"
    if state == "created":
        return "info"
    return "unknown"


def _containers(G, cont, net, links):
    for ct in (cont or {}).get("containers") or []:
        if isinstance(ct, dict) and ct.get("name"):
            _node(G, "ct:" + ct["name"], "ct", ct["name"])["_ct"] = ct
    for x in (links or {}).get("containers") or []:
        if isinstance(x, dict) and x.get("name"):
            _node(G, "ct:" + x["name"], "ct", x["name"])["_ln"] = x
    dbk = {it.get("name"): it.get("kind") for it in (((net or {}).get("dbs") or {}).get("items") or []) if isinstance(it, dict)}
    for n in [n for n in G["nodes"].values() if n["kind"] == "ct"]:
        c, ln = n.get("_ct") or {}, n.get("_ln") or {}
        status, state = c.get("status") or "", ln.get("state") or c.get("state") or ""
        health = ln.get("health") or ("unhealthy" if "unhealthy" in status else "healthy" if "(healthy)" in status else "")
        n["db"] = ln.get("db") or dbk.get(n["label"])
        n["project"] = ln.get("project") or c.get("project") or ""
        n["state"] = _ct_state(state, status, health)
        n["sub"] = safe(" · ".join(x for x in (n["db"] or "container", n["project"]) if x))
        _fact(n, "kind", f"database ({n['db']})" if n["db"] else "container")
        _fact(n, "image", ln.get("image"))
        _fact(n, "compose", " / ".join(x for x in (n["project"], ln.get("service")) if x))
        _fact(n, "state", status or (state + (f" (exit code {ln['exit']})" if ln.get("exit") not in (None, 0) else "")))
        _fact(n, "health", health)
        if ln.get("restarts"):
            _fact(n, "restarts", ln["restarts"])
        _fact(n, "restart policy", ln.get("restart"))
        _fact(n, "networks", ", ".join(f"{k} {v}".strip() for k, v in sorted((ln.get("nets") or {}).items())))
        if ln.get("host_net"):
            _fact(n, "network", "host network: shares the host's ports")
        pub = ln.get("ports") or c.get("ports") or []
        _fact(n, "published", ", ".join(_fmt_pub(p) for p in pub if isinstance(p, dict)))
        if ln.get("listen"):
            src = " (seen in its network namespace)" if ln.get("listen_src") == "netns" else " (declared by the image)"
            _fact(n, "listens inside", ", ".join(str(p) for p in ln["listen"]) + src)
        if c.get("mem"):
            _fact(n, "memory", f"{c['mem'] / 2 ** 20:.0f} MiB")
        if n["state"] == "err":
            _find(n, "err", "health check failing")
        elif n["state"] == "down":
            code = ln.get("exit")
            _find(n, "err", f"exited with code {code}" if code not in (None, 0) else "not running")
        elif n["state"] == "warn":
            _find(n, "warn", "restarting" if "Restarting" in status or state == "restarting" else state or "starting")
        if ln.get("restarts", 0) >= 3:
            _find(n, "warn", f"restarted {ln['restarts']} times")


def _fmt_pub(p):
    scope = {"*": "*", "lo": "127.0.0.1"}.get(p.get("s"), p.get("s") or "*")
    return f"{scope}:{p.get('p') or '?'}" + (f" → {p['c']}" if p.get("c") else "")


def _hosts(G, net):
    """Host processes that listen (docker's own proxies are not services: the container behind them is)."""
    for ln in (net or {}).get("listeners") or []:
        proc = ln.get("proc") or ""
        if not proc or proc in exposure.DOCKER_PROXIES:
            continue
        n = _node(G, "proc:" + proc, "proc", proc, ln.get("unit") or "host process", "ok")
        n.setdefault("_listen", [])
        where = f"{ln.get('addr')}:{ln.get('port')}/{ln.get('proto')}"
        if where not in n["_listen"]:
            n["_listen"].append(where)
        if ln.get("unit") and ("unit", ln["unit"]) not in n["facts"]:
            _fact(n, "unit", ln["unit"])
    for n in G["nodes"].values():
        if n["kind"] == "proc" and n.get("_listen"):
            _fact(n, "listens", ", ".join(n["_listen"]))


def _ports(G, net, cont, baseline, peers, expose=None, webapps=None):
    if net is None or net.get("listeners") is None:
        return
    rows = exposure.expose_apply(exposure.exposure_rows(net, cont), net, cont, expose or {}, webapps or {})  # [expose]: r["want"] where a key matches
    new = exposure.new_ports(net, cont, baseline) if isinstance(baseline, dict) else {}
    serve = exposure.serve_by_port(net)
    for r, (owners, _) in zip(rows, exposure.row_owners(net, cont, rows)):
        group = exposure.group_of(r)
        nid = f"port:{r['port']}/{r['proto']}@{group.lower()}"
        svs = serve.get(r["port"], []) if r["name"].startswith(("funnel ", "serve ")) else []
        state ="err" if r["warn"] else "warn" if (r["bad_note"] or r["net"] == 1 or r["lan"] == 3) else "info"
        n = _node(G, nid, "port", f":{r['port']}/{r['proto']}", r["note"], state)
        n.update(zone=group, owners=owners, port=r["port"], proto=r["proto"])
        _fact(n, "reachable from", REACH_TEXT[group])
        _fact(n, "service", r["name"])
        for sv in svs:
            _fact(n, "tailscale " + ("funnel" if sv.get("funnel") else "serve"), f"{sv.get('path', '')} → {sv.get('target', '')}")
        _fact(n, "LAN", CELL_TEXT.get(r["lan"], "?"))
        _fact(n, "tailnet", CELL_TEXT.get(r["ts"], "?"))
        _fact(n, "Internet", {0: "no", 1: "PUBLIC (Funnel)", 3: "unknown"}.get(r["net"], "?"))
        _fact(n, "firewall", r["note"])
        if r.get("want"):
            _fact(n, "declared reach", f"{exposure.EXPOSE_LABEL[r['want']]} (config.ini [expose])")
            if exposure.expose_over(r):
                _find(n, "err", f"declared {exposure.EXPOSE_LABEL[r['want']]} in config.ini, reachable from {REACH_TEXT[group]}")
                _worse(n, "err")
        if r["warn"]:
            _find(n, "err", "database/broker open on the LAN")
        if r["net"] == 1:
            _find(n, "warn", "public on the Internet (Tailscale Funnel)")
        if r["bad_note"] and r["net"] != 1:
            _find(n, "warn", r["note"])
        if r["lan"] == 3 and not r["bad_note"]:
            _find(n, "warn", "firewall verdict unknown: treated as open")
        key = f"{r['port']}/{r['proto'][0]}:{group}"
        if key in new:
            _find(n, "err", "NEW since the accepted baseline" if new[key] == "NEW" else "CHANGED since the accepted baseline")
            _worse(n, "err")
        if not owners:
            _fact(n, "behind it", "unknown (the process could not be read)")
        _edge(G, GROUP_ROOT[group], nid, "bind")
        for o in owners:
            if o not in G["nodes"]:
                _ensure(G, o, peers)
            _edge(G, nid, o, "bind")


def _conns(G, links, peers):
    for e in (links or {}).get("conns") or []:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("from") or ""), str(e.get("to") or "")
        if not a.startswith(("ct:", "proc:", "ext:")) or not b.startswith(("ct:", "proc:", "ext:")):
            continue
        _ensure(G, a, peers)
        _ensure(G, b, peers)
        when = "now" if e.get("n") else ago(G["now"] - (e.get("last") or 0))
        port = e.get("port") if isinstance(e.get("port"), int) else None
        _edge(G, a, b, "seen", port, e.get("n") or 0, e.get("last"), f"connection seen{f' on :{port}' if port else ''} ({when})")


def _declared(G, links):
    by_net = {}
    for x in (links or {}).get("containers") or []:
        if not isinstance(x, dict) or not x.get("name"):
            continue
        src = "ct:" + x["name"]
        for d in x.get("depends_on") or []:
            _ensure(G, "ct:" + d, {})
            _edge(G, src, "ct:" + d, "declared", why="compose depends_on")
        for d in x.get("env_refs") or []:
            _ensure(G, "ct:" + d, {})
            _edge(G, src, "ct:" + d, "declared", why="named in its environment")
        for net_name in x.get("nets") or {}:
            if net_name not in ("bridge", "host", "none"):
                by_net.setdefault(net_name, set()).add(src)
    for net_name, members in sorted(by_net.items()):  # a database on a shared network: anyone there could use it
        for db in sorted(m for m in members if G["nodes"][m].get("db")):
            for o in sorted(members - {db}):
                if not G["nodes"][o].get("db") and (o, db) not in G["_pair"]:
                    _edge(G, o, db, "possible", why=f"same network {net_name}")


def _dbs(G, net, peers):
    """The DATABASE section's evidence (it exists without the map section too)."""
    for it in ((net or {}).get("dbs") or {}).get("items") or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        db = "ct:" + it["name"]
        n = _ensure(G, db, peers)
        n["db"] = n.get("db") or it.get("kind")
        for o in it.get("active") or []:
            _edge(G, _ensure(G, "ct:" + o, peers)["id"], db, "seen", n=1, last=G["now"], why="connected now (seen inside the database)")
        for o in it.get("usano") or []:
            _edge(G, _ensure(G, "ct:" + o, peers)["id"], db, "declared", why="named in its environment")
        for o in it.get("stessa_rete") or []:
            if ("ct:" + o, db) not in G["_pair"]:
                _edge(G, _ensure(G, "ct:" + o, peers)["id"], db, "possible", why="same network")
        for p in it.get("host_clients") or []:
            _edge(G, _ensure(G, "proc:" + p, peers)["id"], db, "seen", n=1, last=G["now"], why="host process connected")
        for x in it.get("external") or []:
            if isinstance(x, dict) and x.get("ip"):
                fresh = G["now"] - (x.get("last") or 0) < 120
                _edge(G, _ensure(G, "ext:" + x["ip"], peers)["id"], db, "seen", n=1 if fresh else 0, last=x.get("last"),
                      why="external client of the database")
        if it.get("ext_source") == "n/d":
            _fact(n, "external clients", "not detectable here")


def _webapps(G, webapps, known):
    """known: the listening ports were read (without them a declared web app is unknown, never down)."""
    for name, ports in sorted((webapps or {}).items()):
        hit = [n for n in G["nodes"].values() if n["kind"] == "port" and n.get("port") in ports]
        for n in hit:
            _fact(n, "web app", f"{name} (declared in config.ini)")
        if not hit and not known:
            n = _node(G, "webapp:" + name, "webapp", name, "web app · listening ports unknown", "unknown")
            _fact(n, "declared ports", ", ".join(str(p) for p in ports))
            _fact(n, "listening", "unknown (no listening ports from the network collector)")
        elif not hit:
            n = _node(G, "webapp:" + name, "webapp", name, "web app · expected on :" + ",".join(str(p) for p in ports), "down")
            _fact(n, "declared ports", ", ".join(str(p) for p in ports))
            _find(n, "warn", "declared in [webapps] but nothing listens on " + ", ".join(f":{p}" for p in ports))


def _units(G, boot):
    if not isinstance(boot, dict):
        return
    what = {"windows": "failed service", "darwin": "failed launch daemon"}.get(boot.get("os") or "linux", "failed unit")
    deps = boot.get("deps") if isinstance(boot.get("deps"), dict) else {}
    failed = boot.get("failed") or []
    for u in failed:  # all of them first: a failed unit that needs another failed one is down, not just "needs" it
        _find(_node(G, "unit:" + str(u), "unit", u, what, "down"), "err", "failed")
    for u in failed:
        n = G["nodes"]["unit:" + str(u)]
        for d in deps.get(u) or []:
            m = _node(G, "unit:" + str(d), "unit", d, "needs " + safe(u), "unknown")
            _fact(m, "needs", u)
            _edge(G, m["id"], n["id"], "declared", why="systemd dependency" if what == "failed unit" else "service dependency")
        if u in deps and not deps[u]:  # no key: the list could not be read (or is past the first 20), which is not "none"
            _fact(n, "needed by", "nothing that is installed")
        elif u not in deps:
            _fact(n, "needed by", "unknown")


def _stacks(G):
    for n in [n for n in G["nodes"].values() if n["kind"] == "ct" and (n.get("_ct") or n.get("_ln"))]:
        proj = n.get("project") or ""
        s = _node(G, "stack:" + proj, "stack", proj or "standalone", "compose project" if proj else "containers without a project", "ok")
        _edge(G, s["id"], n["id"], "bind")
        if n["state"] in BAD:
            _worse(s, "warn" if n["state"] == "warn" else "err")
    for s in [n for n in G["nodes"].values() if n["kind"] == "stack"]:
        members = [e["dst"] for e in G["out"].get(s["id"], [])]
        _fact(s, "containers", f"{len(members)} ({sum(G['nodes'][m]['state'] == 'ok' for m in members)} healthy)")


def _bfs(G, start, follow, hops):
    """{node: parent} of everything reachable from `start` in `hops` steps through edges `follow` accepts."""
    parent, frontier = {start: None}, [start]
    for _ in range(hops):
        nxt = []
        for x in frontier:
            for e in follow(x):
                y = e["dst"] if e["src"] == x else e["src"]
                if y not in parent:
                    parent[y] = x
                    nxt.append(y)
        frontier = nxt
    return parent


def _via(G, parent, end, start, back=False):
    """The labels between start and end of a _bfs; back: listed from end toward start (a dependent, then what it uses)."""
    chain, x = [], parent.get(end)
    while x is not None and x != start:
        chain.append(G["nodes"][x]["label"])
        x = parent.get(x)
    return " → ".join(chain if back else reversed(chain))


def _findings(G):
    nodes = G["nodes"]
    fwd = lambda x: [e for e in G["out"].get(x, []) if e["ev"] in ("seen", "declared") and not e["dst"].startswith("ext:")]  # noqa: E731
    rev = lambda x: [e for e in G["inc"].get(x, []) if e["ev"] in ("seen", "declared") and e["src"].startswith(("ct:", "proc:", "unit:"))]  # noqa: E731
    for p in [n for n in nodes.values() if n["kind"] == "port" and n["zone"] != "LOCALE"]:
        for o in p["owners"]:
            if nodes[o].get("db") or o.startswith("ext:"):  # another host behind a Serve port: what it uses is not seen
                continue
            parent = _bfs(G, o, fwd, DATA_HOPS)
            data = [x for x in parent if x != o and nodes[x].get("db")]
            for d in sorted(data):
                via = _via(G, parent, d, None)
                level = "warn" if p["zone"] == "INTERNET" else "info"
                _find(p, level, f"leads to data: {nodes[d]['label']} ({nodes[d]['db']}) via {via}")
                if level == "warn":
                    _worse(p, "warn")
        nums = _port_numbers(G, p)
        for o in p["owners"]:  # a public address connected to a port that is meant for the LAN: the LAN is the Internet here
            for e in G["inc"].get(o, []):  # through this port (an unknown one may be it): not another port of the same owner
                if (e["src"].startswith("ext:") and nodes[e["src"]].get("zone") == "Internet" and p["zone"] in ("LAN", "TAILNET")
                        and (e["port"] is None or e["port"] in nums)):
                    _find(p, "warn", f"a public address was connected ({nodes[e['src']]['label']}): reachable from the Internet?")
                    _worse(p, "warn")
    for n in [n for n in nodes.values() if n["kind"] == "ct" and n.get("db")]:
        lan = [e for e in G["inc"].get(n["id"], []) if e["src"].startswith("ext:") and nodes[e["src"]].get("zone") in ("LAN", "Internet")]
        if lan:
            _find(n, "err", "clients from outside connect to it directly: " + ", ".join(nodes[e["src"]]["label"] for e in lan[:3]))
            _worse(n, "warn")  # healthy, but exposed: attention, not "down"
    for bad in [n for n in nodes.values() if n["state"] in ("down", "err") and n["kind"] in ("ct", "unit")]:
        parent = _bfs(G, bad["id"], rev, DATA_HOPS)
        users = [x for x in parent if x != bad["id"]]
        if users:
            _find(bad, "err", f"{len(users)} depend on it: " + ", ".join(nodes[x]["label"] for x in sorted(users)[:4])
                  + (" …" if len(users) > 4 else ""))
        word = "down" if bad["state"] == "down" else "failing"
        for x in users:
            via = _via(G, parent, x, bad["id"], back=True)
            _find(nodes[x], "warn", f"depends on {bad['label']}, which is {word}" + (f" (via {via})" if via else ""))
            if nodes[x]["state"] in ("ok", "info", "unknown"):
                nodes[x]["state"] = "warn"
        for x in [bad["id"]] + users:  # and the entries in front of them: the zone reaches what is down through them
            for e in G["inc"].get(x, []):
                if e["ev"] == "bind" and e["src"].startswith("port:"):
                    via = " → ".join(v for v in (nodes[x]["label"], _via(G, parent, x, bad["id"], back=True)) if v)
                    _find(nodes[e["src"]], "warn", f"{bad['label']} behind it is {word}" if x == bad["id"]
                          else f"leads to {bad['label']}, which is {word} (via {via})")
                    _worse(nodes[e["src"]], "warn")


def _roots(G):
    nodes = G["nodes"]
    order = lambda ids: sorted(ids, key=lambda x: (STATE_RANK[nodes[x]["state"]], nodes[x].get("port") or 0, nodes[x]["label"]))  # noqa: E731
    kids = {rid: [] for rid, _, _, _ in ROOTS}
    for rid, _, _, group in ROOTS:
        if group:
            kids[rid] = order(e["dst"] for e in G["out"].get(rid, []))
    kids["root:impact"] = order(n["id"] for n in nodes.values() if n["kind"] in ("ct", "unit", "webapp") and n["state"] in ("down", "err"))
    kids["root:stacks"] = sorted((n["id"] for n in nodes.values() if n["kind"] == "stack"), key=lambda x: (x == "stack:", x))
    kids["root:outbound"] = order({e["src"] for e in G["edges"] if e["dst"].startswith("ext:") and e["ev"] != "bind"
                                   and not e["src"].startswith("ext:")})  # 'bind': a Serve port's backend on another host
    for rid, label, what, _ in ROOTS:
        if not kids[rid]:
            continue
        n = _node(G, rid, "root", label, what, "info")
        G["_kids"][rid] = kids[rid]
        worst = min((nodes[k]["state"] for k in kids[rid]), key=STATE_RANK.get)
        if worst in BAD and rid not in ("root:stacks", "root:outbound"):
            n["state"] = "warn" if worst == "warn" else "err"
        _fact(n, "shows", what)
        _fact(n, "items", len(kids[rid]))
        G["roots"].append(rid)


def _notes(G, net, links, cont):
    if net is None:
        G["notes"].append("network collector not running: no ports, no connections")
        return
    if links is None:
        if "links" in (net.get("disabled") or []):
            G["notes"].append("map = no in config.ini: connections are not collected")
        elif "links" in (net.get("errors") or {}):
            G["notes"].append("connections unreadable: " + safe(net["errors"]["links"])[:80])
        elif "links" in (net.get("absent") or []):
            G["notes"].append("connections not collected: neither docker nor ss/lsof is installed")
        else:
            G["notes"].append("connections not collected: restart the collector after the upgrade")
    else:
        errors = [str(x) for x in links.get("errors") or []]
        if links.get("conn_source") == "host" and (links.get("containers") or (cont or {}).get("containers")):
            if exposure.os_of(net) != "linux":
                G["notes"].append("containers' own connections are not visible on this OS (Docker Desktop VM): declared and "
                                  "same-network links only")
            elif not any("namespace" in x or "nsenter" in x for x in errors):  # else the collector's own note says it, and why
                G["notes"].append("container namespaces unreadable: container links seen from the host only")
        for x in errors:
            G["notes"].append(safe(x)[:100])
        since = links.get("since")
        if since:
            G["notes"].append(f"connections sampled every 30 s, remembered 24 h (watching since {ago(G['now'] - since).replace(' ago', '')})")
    if exposure.exposure_partial(net):
        G["notes"].append("some network sections unreadable: reachability may be incomplete")
    if cont is None:
        G["notes"].append("container collector not running")


def build(cont, net, boot=None, webapps=None, now=None, baseline=None, expose=None):
    """The graph of one snapshot. Never raises on missing or partial data: what is missing goes to G["notes"]."""
    G = {"nodes": {}, "edges": [], "out": {}, "inc": {}, "notes": [], "roots": [], "_pair": {}, "_kids": {}, "_bad": {},
         "now": now or time.time()}
    net = net if isinstance(net, dict) else None
    cont = cont if isinstance(cont, dict) else None
    links = (net or {}).get("links") if isinstance((net or {}).get("links"), dict) else None
    ts = (net or {}).get("ts_peers") or {}
    peers = {ip: safe(p.get("name", "")) for p in ([ts.get("self") or {}] + list(ts.get("peers") or [])) if isinstance(p, dict)
             for ip in p.get("ips") or []}
    _containers(G, cont, net, links)
    _hosts(G, net)
    _ports(G, net, cont, baseline, peers, expose, webapps)
    _conns(G, links, peers)
    _declared(G, links)
    _dbs(G, net, peers)
    _webapps(G, webapps, net is not None and net.get("listeners") is not None)
    _units(G, boot)
    _findings(G)
    _stacks(G)  # after the findings: a project is as yellow as its members turned
    _roots(G)
    _notes(G, net, links, cont)
    return G


# ---- walking it as a tree ------------------------------------------------------------------------------------------------

def _port_numbers(G, n):
    """The ports a client of this entry may have used: the host port, and the container port it is published to."""
    nums = {n.get("port")}
    for o in n.get("owners") or []:
        for p in (G["nodes"][o].get("_ln") or {}).get("ports") or []:
            c = str(p.get("c", "")).split("/")[0] if isinstance(p, dict) else ""  # malformed entries: skipped, never a crash
            if c and p.get("p") == n.get("port") and re.fullmatch(r"[0-9]{1,5}", c):
                nums.add(int(c))
    return nums


def _edge_order(G, e, other):
    n = G["nodes"][other]
    return (-EV_RANK[e["ev"]], other.startswith("ext:"), STATE_RANK[n["state"]], n["label"])


def children(G, nid, ctx="fwd"):
    """[(edge or None, child id, 'out'|'in')] in display order. ctx: 'fwd' dependencies, 'rev' dependents, 'ext' outbound."""
    if nid in G["_kids"]:
        return [(None, k, "out") for k in G["_kids"][nid]]
    n = G["nodes"].get(nid)
    if n is None or n["kind"] == "ext":  # a remote address is where a path ends: what it talks to is not seen from here
        return []
    if ctx == "rev":
        inc = [e for e in G["inc"].get(nid, []) if (e["ev"] in ("seen", "declared") and e["src"].startswith(("ct:", "proc:", "unit:")))
               or (e["ev"] == "bind" and e["src"].startswith("port:"))]
        return [(e, e["src"], "in") for e in sorted(inc, key=lambda e: _edge_order(G, e, e["src"]))]
    if ctx == "ext":
        return [(e, e["dst"], "out") for e in sorted((e for e in G["out"].get(nid, []) if e["dst"].startswith("ext:")),
                                                     key=lambda e: _edge_order(G, e, e["dst"]))]
    if n["kind"] == "port":
        nums, out, seen = _port_numbers(G, n), [], set()
        for o in n.get("owners") or []:
            for e in sorted(G["inc"].get(o, []), key=lambda e: _edge_order(G, e, e["src"])):
                if e["src"].startswith("ext:") and (e["port"] in nums or e["port"] is None) and e["src"] not in seen:
                    seen.add(e["src"])
                    out.append((e, e["src"], "in"))
        for o in n.get("owners") or []:
            for item in children(G, o, ctx):
                if item[1] not in seen:
                    seen.add(item[1])
                    out.append(item)
        return out
    if n["kind"] == "stack":
        return [(None, e["dst"], "out") for e in sorted(G["out"].get(nid, []), key=lambda e: _edge_order(G, e, e["dst"]))]
    return [(e, e["dst"], "out") for e in sorted(G["out"].get(nid, []), key=lambda e: _edge_order(G, e, e["dst"])) if e["ev"] != "bind"]


def _bad_below(G, nid, ctx):
    """True if this node, or anything under it in this walk, needs attention ('problems only'). Worked out once per walk,
    backwards from the nodes that need attention to everything that reaches them: neither a cycle nor the order the nodes
    are asked in can hide a path."""
    if ctx not in G["_bad"]:
        up, todo = {}, []
        for x, n in G["nodes"].items():
            for _, k, _ in children(G, x, ctx):
                up.setdefault(k, set()).add(x)
            if n.get("state") in BAD or any(lv in ("err", "warn") for lv, _ in n.get("findings") or []):
                todo.append(x)
        bad = G["_bad"][ctx] = set(todo)
        while todo:
            for x in up.get(todo.pop(), ()):
                if x not in bad:
                    bad.add(x)
                    todo.append(x)
    return nid in G["_bad"][ctx]


def rows(G, st=None, limit=3000):
    """The visible rows, depth first: {key, depth, node, edge, dir, open, kids, cycle, last, rails}."""
    st, out = st or State(), Rows()

    def walk(nid, path, depth, edge, way, last, rails, ctx):
        if len(out) >= limit:
            out.truncated = True
            return
        # a port's children are its owners': the owners are on the path too, even if their id is not in it (nor in the key)
        cycle = nid in path[:-1] or any(nid in (G["nodes"].get(p, {}).get("owners") or ()) for p in path[:-1])
        kids = [] if cycle or depth >= MAX_DEPTH else children(G, nid, ctx)
        if st.only:
            kids = [k for k in kids if _bad_below(G, k[1], ctx)]
        key = path_key(path)
        is_open = bool(kids) and st.is_open(key, depth == 0)
        out.append({"key": key, "depth": depth, "node": nid, "edge": edge, "dir": way, "open": is_open, "kids": len(kids),
                    "cycle": cycle, "last": last, "rails": rails})
        if is_open:
            below = rails + (not last,) if depth else ()
            for i, (e, child, d) in enumerate(kids):
                walk(child, path + (child,), depth + 1, e, d, i == len(kids) - 1, below, ctx)

    for rid in G["roots"]:
        ctx = CTX.get(rid, "fwd")
        if not st.only or _bad_below(G, rid, ctx):
            walk(rid, (rid,), 0, None, "out", True, (), ctx)
    return out


def fit_open(G, max_rows):
    """Keys to open so that the tree fills at most max_rows: level by level, while it fits (the rotation slide)."""
    st = State()
    for depth in range(1, MAX_DEPTH):
        cand = [r["key"] for r in rows(G, st) if r["depth"] == depth and r["kids"] and not r["open"]]
        if not cand:
            break
        trial = State(open=st.open | set(cand))
        if len(rows(G, trial, limit=max_rows + 1)) <= max_rows:
            st = trial
            continue
        for key in cand:  # the whole level does not fit: open what does, top first, and stop there
            trial = State(open=st.open | {key})
            if len(rows(G, trial, limit=max_rows + 1)) <= max_rows:
                st = trial
        break
    return st.open


def find(rs, key):
    return next((i for i, r in enumerate(rs) if r["key"] == key), None)


def _edge_text(G, e, other, way):
    n = G["nodes"][other]
    port = f" :{e['port']}" if e.get("port") else ""
    when = ""
    if e["ev"] == "seen":
        when = " now" if e.get("n") else (" " + ago(G["now"] - e["last"]) if e.get("last") else "")
    why = "; ".join(w for w in e["why"] if not w.startswith("connection seen"))
    return (f"{n['label']}{port}  {EV_WORD[e['ev']]}{when}" + (f"  ({why})" if why else "")
            + (f"  [{n['sub']}]" if n["sub"] else ""))


def note_of(G, row):
    """The short text at the end of a row: the worst finding, else when the connection was seen."""
    n, e = G["nodes"][row["node"]], row["edge"]
    worst = sorted((f for f in n["findings"] if f[0] in ("err", "warn")), key=lambda f: f[0] != "err")
    if worst:
        return worst[0][1]
    if e and e["ev"] == "seen":
        return "now" if e.get("n") else ago(G["now"] - e["last"]) if e.get("last") else ""
    return ""


def parts(G, row):
    """Everything a renderer needs to draw one row, as plain text pieces (the renderer adds colours or links)."""
    n, e = G["nodes"][row["node"]], row["edge"]
    tree = "" if not row["depth"] else "".join("│  " if r else "   " for r in row["rails"]) + ("└─ " if row["last"] else "├─ ")
    toggle = "↻" if row["cycle"] else ("▾" if row["open"] else "▸") if row["kids"] else "·"
    arrow = ARROW.get((row["dir"], e["ev"]), "") if e else ""
    port = f":{e['port']}" if e and e.get("port") and e["ev"] != "bind" and n["kind"] != "port" else ""
    owner = ", ".join(G["nodes"][o]["label"] for o in n.get("owners") or []) or ("?" if n["kind"] == "port" else "")
    note = note_of(G, row)
    return {"tree": tree, "toggle": toggle, "arrow": arrow, "label": n["label"], "owner": owner, "sub": "" if n["sub"] == note else n["sub"],
            "state": n["state"], "note": note, "port": port, "kind": n["kind"], "ev": e["ev"] if e else ""}


def details(G, nid):
    """Everything known about a node: [(label, value, level)], level err/warn/ok/info or ''."""
    n = G["nodes"].get(nid)
    if n is None:
        return [("gone", "no longer on the map (it changed since the last refresh)", "warn")]
    lvl = {"err": "err", "down": "err", "warn": "warn", "ok": "ok", "unknown": "warn"}.get(n["state"], "info")
    out = [(KIND_NAME.get(n["kind"], n["kind"]), n["label"] + (f"  ·  {n['sub']}" if n["sub"] else ""), lvl)]
    out += [(k, v, "") for k, v in n["facts"]]
    out += [("!" if lv in ("err", "warn") else "·", t, lv) for lv, t in n["findings"]]
    targets = [nid] + list(n.get("owners") or [])
    for t in targets:
        tn = G["nodes"].get(t)
        if t != nid and tn:
            out.append(("behind it", f"{tn['label']}  ·  {tn['sub']}", {"err": "err", "down": "err", "warn": "warn"}.get(tn["state"], "")))
            out += [("  " + k, v, "") for k, v in tn["facts"]]
            out += [("  !" if lv in ("err", "warn") else "  ·", txt, lv) for lv, txt in tn["findings"]]
        inc = sorted((e for e in G["inc"].get(t, []) if e["ev"] != "bind"), key=lambda e: _edge_order(G, e, e["src"]))
        outs = sorted((e for e in G["out"].get(t, []) if e["ev"] != "bind"), key=lambda e: _edge_order(G, e, e["dst"]))
        for e in inc:
            out.append(("◄ from", _edge_text(G, e, e["src"], "in"), ""))
        for e in outs:
            out.append(("► to", _edge_text(G, e, e["dst"], "out"), ""))
    ports = [e["src"] for e in G["inc"].get(nid, []) if e["ev"] == "bind" and e["src"].startswith("port:")]
    for p in ports:
        pn = G["nodes"][p]
        out.append(("exposed as", f"{pn['label']} ({REACH_TEXT.get(pn.get('zone'), '?')})", "warn" if pn["state"] in BAD else ""))
    return out


def counts(G):
    real = [n for n in G["nodes"].values() if n["kind"] != "root"]
    return {"nodes": len(real), "edges": sum(e["ev"] != "bind" for e in G["edges"]),
            "problems": sum(n["state"] in BAD for n in real)}
