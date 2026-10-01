"""Synthetic data for `render.py --once --demo` (screenshots, trying the dashboard without root, docs).

Invented: hostnames, users, containers and addresses (documentation ranges). CPU, RAM, temperature, uptime and root-disk
figures are the real ones of the machine running it (read from /proc and /sys); nothing is written and no real state is read.
"""
import time

LAN = "192.168.0"    # private range, generic (a public TEST-NET would be flagged as "not local")
EXT = "198.51.100"   # TEST-NET-2
TS = "100.64.0"      # CGNAT range used by Tailscale
UFW_TEXT = f"""Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    {LAN}.0/24
Anywhere on tailscale0     ALLOW IN    Anywhere
41641/udp                  ALLOW IN    Anywhere
"""


def _ct(name, project, state="running", status="Up 3 days", ports=(), mem=200 * 2 ** 20):
    return {"name": name, "status": status, "state": state, "project": project, "ports": list(ports), "mem": mem}


def _db(name, kind, project, port, scope, active=(), ext=()):
    return {"name": name, "kind": kind, "image": kind + ":16", "project": project, "state": "running",
            "ports": [{"p": port, "c": f"{port}/tcp", "s": scope}], "host_net": False, "nets": [project + "_default"],
            "active": list(active), "usano": list(active), "stessa_rete": [], "host_clients": [],
            "external": [{"ip": ip, "last": time.time() - 60} for ip in ext], "ext_source": "netns"}


def _link(name, project, service, image, nets, listen, ports=(), depends=(), env=(), db=None, state="running", health="",
          exit_code=None, restarts=0):
    return {"name": name, "image": image, "project": project, "service": service, "state": state, "health": health,
            "exit": exit_code, "restarts": restarts, "restart": "unless-stopped", "host_net": False, "nets": dict(nets),
            "ports": list(ports), "listen": list(listen), "listen_src": "netns" if state == "running" else "image",
            "depends_on": list(depends), "env_refs": list(env), "db": db}


def _links(now):
    """net["links"] (the MAP): what each container is, and who was seen talking to whom (see collector.link_items)."""
    sh, bl = "shop_default", "blog_default"
    edge = lambda a, b, port, n=1, ago=0: {"from": a, "to": b, "port": port, "n": n, "last": now - ago}  # noqa: E731
    return {"since": now - 3600, "conn_source": "netns", "errors": [], "containers": [
        _link("shop-web-1", "shop", "web", "example/shop-web:1.4", {sh: "172.18.0.2"}, [80], [{"p": 8080, "c": "80/tcp", "s": "*"}],
              depends=["shop-api-1"], env=["shop-api-1"], health="healthy"),
        _link("shop-api-1", "shop", "api", "example/shop-api:1.4", {sh: "172.18.0.3"}, [3000],
              depends=["shop-db-1"], env=["shop-db-1", "cache-1"], health="healthy"),
        _link("shop-db-1", "shop", "db", "postgres:16", {sh: "172.18.0.4"}, [5432], [{"p": 5432, "c": "5432/tcp", "s": "*"}], db="postgres"),
        _link("worker-1", "shop", "worker", "example/shop-worker:1.4", {sh: ""}, [], depends=["shop-db-1"], env=["shop-db-1"],
              state="exited", exit_code=137, restarts=3),
        _link("blog-app-1", "blog", "app", "example/blog:2.0", {bl: "172.19.0.2"}, [8080], [{"p": 8081, "c": "8080/tcp", "s": "lo"}],
              depends=["blog-db-1"], env=["blog-db-1"]),
        _link("blog-db-1", "blog", "db", "postgres:16", {bl: "172.19.0.3"}, [5432], [{"p": 5433, "c": "5432/tcp", "s": "lo"}], db="postgres"),
        _link("cache-1", "", "", "redis:7", {sh: "172.18.0.9"}, [6379], [{"p": 6379, "c": "6379/tcp", "s": "lo"}], db="redis")],
        "conns": [
        edge("ct:shop-web-1", "ct:shop-api-1", 3000, 4), edge("ct:shop-api-1", "ct:shop-db-1", 5432, 6),
        edge("ct:blog-app-1", "ct:blog-db-1", 5432, 2), edge("ext:" + LAN + ".50", "ct:shop-db-1", 5432),
        edge("ext:" + TS + ".2", "ct:shop-web-1", 8080), edge("ext:" + LAN + ".20", "proc:sshd", 22),
        edge("proc:tailscaled", "proc:node", 5678), edge("ct:shop-api-1", "ext:" + EXT + ".25", 443, 2),
        edge("proc:node", "ext:" + EXT + ".80", 443, 0, 1500)]}


def _ufw():
    import collector
    d = collector.parse_ufw(UFW_TEXT)
    d["raw"] = "Status: active"
    return d


def snapshot(now=None, os_name=None):
    """-> (containers, net, boot, baseline) in the same shape the collector writes; os_name 'windows'/'darwin' = that collector."""
    now = now or time.time()
    cont = {"ts": now, "containers": [
        _ct("shop-web-1", "shop", ports=[{"p": 8080, "s": "*"}], mem=310 * 2 ** 20),
        _ct("shop-api-1", "shop", mem=520 * 2 ** 20),
        _ct("shop-db-1", "shop", ports=[{"p": 5432, "s": "*"}], mem=410 * 2 ** 20),
        _ct("blog-app-1", "blog", ports=[{"p": 8081, "s": "lo"}], mem=150 * 2 ** 20),
        _ct("blog-db-1", "blog", ports=[{"p": 5433, "s": "lo"}], mem=260 * 2 ** 20),
        _ct("cache-1", "", ports=[{"p": 6379, "s": "lo"}], mem=30 * 2 ** 20),
        _ct("worker-1", "shop", state="exited", status="Exited (137) 2 hours ago", mem=None),
    ]}
    units = {"sshd": "ssh.service", "node": "n8n.service", "tailscaled": "tailscaled.service"}
    lst = lambda addr, port, proc: dict({"proto": "tcp", "addr": addr, "port": port, "proc": proc},  # noqa: E731
                                        **({"unit": units[proc]} if proc in units else {}))
    net = {"ts": now, "errors": {}, "absent": [], "ufw": _ufw(), "docker_user": [],
           "iptables": {"policy": {"INPUT": "DROP", "FORWARD": "DROP"}, "count": {"INPUT": 5}, "ts_input": True},
           "f2b": {"jails": [{"name": "sshd", "banned": 2, "ips": [EXT + ".7", EXT + ".9"]}]},
           "drops": {"n": 41, "src": [[EXT + ".9", 30], [EXT + ".44", 11]], "dpt": [["23", 25], ["445", 16]]},
           "serve": [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True}],
           "listeners": [lst("0.0.0.0", 22, "sshd"), lst("0.0.0.0", 8080, "docker-proxy"), lst("0.0.0.0", 5432, "docker-proxy"),
                         lst("127.0.0.1", 8081, "docker-proxy"), lst("127.0.0.1", 5433, "docker-proxy"),
                         lst("127.0.0.1", 6379, "docker-proxy"), lst("127.0.0.1", 5678, "node"),
                         lst(TS + ".1", 8444, "tailscaled")],
           "dbs": {"since": now - 3600, "items": [
               _db("shop-db-1", "postgres", "shop", 5432, "*", active=["shop-api-1"], ext=[LAN + ".50"]),
               _db("blog-db-1", "postgres", "blog", 5433, "lo", active=["blog-app-1"]),
               _db("cache-1", "redis", "", 6379, "lo")]},
           "ts_peers": {"self": {"name": "demo-host", "online": True, "exit_option": False, "ips": [TS + ".1"]}, "peers": [
               {"name": "laptop", "os": "linux", "online": True, "last_seen": None, "direct": True, "relay": "", "exit": False, "exit_option": False,
                "ips": [TS + ".2"]},
               {"name": "phone", "os": "android", "online": False, "last_seen": now - 7200, "direct": False, "relay": "fra", "exit": False, "exit_option": False,
                "ips": [TS + ".3"]}]},
           "links": _links(now)}
    boot = {"ts": now, "errors": {}, "absent": [], "kernel": "6.8.0-demo", "btime": int(now) - 5 * 86400,
            "analyze": {"parts": {"firmware": 5.1, "loader": 2.0, "kernel": 1.2, "initrd": 1.1, "userspace": 12.4}, "total": 21.8},
            "blame": [{"unit": u, "s": s} for u, s in (("docker.service", 6.2), ("snapd.service", 4.8), ("cloud-init.service", 3.9))],
            "failed": [], "deps": {}, "enabled": [{"unit": f"svc{i}.service", "state": "active"} for i in range(24)],
            "journal": {"err": 2, "warn": 9, "capped": False, "top": [{"id": "kernel", "n": 4, "pr": 4, "last": "example warning"}]},
            "containers": [{"name": "shop-web-1", "started": int(now) - 5 * 86400 + 40, "restart": "unless-stopped"},
                           {"name": "cache-1", "started": int(now) - 3600, "restart": "no"}],
            "docker_df": {"rows": [{"type": "Images", "count": "14", "active": "6", "size": "5.2GB", "reclaimable": "2.1GB (40%)"},
                                   {"type": "Containers", "count": "7", "active": "6", "size": "180MB", "reclaimable": "0B (0%)"},
                                   {"type": "Local Volumes", "count": "5", "active": "5", "size": "1.4GB", "reclaimable": "0B (0%)"}],
                          "volumes_unused": 12, "volumes_unused_anonymous": 9, "dangling_images": {"count": 0, "bytes": 0}}}
    if os_name in ("windows", "darwin"):
        net, boot = _native(net, boot, os_name, now)
    import render
    base = {"ts": now, "ports": render.exposure_keys(net, cont)}  # baseline = current exposure: no "new port" alarm
    return cont, net, boot, base


def _native(net, boot, os_name, now):
    """The same machine as seen by the macOS/Windows collector: OS firewall verdicts per socket, no ufw/iptables/fail2ban."""
    win = os_name == "windows"
    proxy = "com.docker.backend"
    allow = (lambda r: ["open", f'rule "{r}" (Private)']) if win else (lambda r: ["open", r])
    lst = lambda addr, port, proc, fw, proto="tcp": {"proto": proto, "addr": addr, "port": port, "proc": proc, "fw": fw}
    common = [lst("0.0.0.0", 8080, proxy, allow("Docker Desktop Backend") if win else ["unknown", "not listed: macOS decides"]),
              lst("0.0.0.0", 5432, proxy, allow("Docker Desktop Backend") if win else ["unknown", "not listed: macOS decides"]),
              lst("127.0.0.1", 8081, proxy, ["open", "local"]), lst("127.0.0.1", 5433, proxy, ["open", "local"]),
              lst("127.0.0.1", 6379, proxy, ["open", "local"]), lst("127.0.0.1", 5678, "node", ["open", "local"]),
              lst(TS + ".1", 8444, "tailscaled", allow("Tailscale") if win else ["open", "allowed in the firewall"])]
    if win:
        own = [lst("0.0.0.0", 22, "sshd", allow("OpenSSH SSH Server (sshd)")),
               lst("0.0.0.0", 445, "System", ["blocked", "default block (Private)"]),
               lst("0.0.0.0", 135, "svchost/RpcSs", ["blocked", "default block (Private)"]),
               lst("0.0.0.0", 3389, "svchost/TermService", ["filtered", f"only {LAN}.0/24"]),
               lst("0.0.0.0", 7680, "svchost/DoSvc", ["unknown", "rule not understood (Private)"])]
        fw = {"kind": "windows", "name": "Windows Firewall", "off": [], "policy": False, "allow_rules": 87, "block_rules": 3,
              "networks": [{"alias": "Ethernet", "category": "Private"}],
              "profiles": {n: {"enabled": True, "inbound": 0, "block_all": False, "active": n == "Private"}
                           for n in ("Domain", "Private", "Public")}}
        boot = dict(boot, analyze={"parts": {"main path": 14.2, "post boot": 9.1}, "total": 23.3}, kernel="Windows 11 (10.0.26100)",
                    enabled=[{"unit": f"Service{i}", "state": "active" if i % 6 else "inactive"} for i in range(40)],
                    journal={"err": 3, "warn": 12, "capped": False, "top": [
                        {"id": "Microsoft-Windows-DistributedCOM", "n": 9, "pr": 4, "last": "example DCOM warning"},
                        {"id": "Service Control Manager", "n": 3, "pr": 3, "last": "example service error"}]},
                    unsupported=["blame"], absent=["blame"])
        boot.pop("blame", None)
    else:
        own = [lst("*", 22, "launchd", ["open", "built-in, allowed"]), lst("*", 5000, "ControlCenter", ["open", "built-in, allowed"]),
               lst("*", 7000, "ControlCenter", ["open", "built-in, allowed"]),
               lst("*", 5353, "mDNSResponder", ["open", "essential service"], proto="udp")]
        fw = {"kind": "darwin", "name": "macOS firewall", "state": 1, "off": [], "block_all": False, "stealth": True, "builtin": True,
              "downloaded": True, "apps_allowed": 3, "apps_blocked": 1, "pf": {"enabled": False, "rules": 0}}
        boot = dict(boot, kernel="macOS 15.6", enabled=[{"unit": f"com.example.daemon{i}", "state": "active"} for i in range(6)],
                    unsupported=["analyze", "blame", "journal"], absent=["analyze", "blame", "journal"])
        for k in ("analyze", "blame", "journal"):
            boot.pop(k, None)
    gone = ("ufw", "docker_user", "iptables", "f2b", "drops")
    net = {k: v for k, v in net.items() if k not in gone}
    net.update(os=os_name, firewall=fw, listeners=own + common, absent=list(gone), unsupported=list(gone))
    if not win:
        boot.pop("deps", None)  # macOS: launchd has no dependency graph to read
    for it in net["dbs"]["items"]:
        it["ext_source"] = "n/d"  # Docker Desktop: the containers' namespaces are inside a VM
        it["external"], it["active"] = [], []  # nor who connects from the other containers (collector.db_items without conn_fn)
    links = dict(net["links"], conn_source="host")  # only the host's sockets: container-to-container traffic is in the VM
    links["containers"] = [dict(x, listen_src="image") for x in links["containers"]]
    links["conns"] = [e for e in links["conns"] if not e["from"].startswith("ct:")]  # what containers open is NATed by the VM
    net["links"] = links
    return net, dict(boot, os=os_name, ts=now)


def sampler_data(real, os_name=None):
    """Replace machine-specific parts of a real Sampler.sample() result (users, mounts, interface names)."""
    real = dict(real)
    if os_name == "windows":
        real["sessions"] = {"local": [{"user": "alice", "tty": "Console"}], "ssh": [], "rdp": [LAN + ".20"]}
        real["fs"] = [{"mount": "C:", "used": 180 * 2 ** 30, "total": 480 * 2 ** 30}, {"mount": "D:", "used": 1200 * 2 ** 30, "total": 2000 * 2 ** 30}]
        real["net"] = {"Ethernet": dict(next(iter(real["net"].values()), {"rx": 0, "tx": 0, "rx_tot": 0, "tx_tot": 0, "hist_rx": [0], "hist_tx": [0]}))}
        return real
    if os_name == "darwin":
        real["sessions"] = {"local": [{"user": "alice", "tty": "console"}], "ssh": [LAN + ".20"], "vnc": []}
        real["fs"] = [{"mount": "/", "used": 180 * 2 ** 30, "total": 480 * 2 ** 30}, {"mount": "/Volumes/Data", "used": 1200 * 2 ** 30, "total": 2000 * 2 ** 30}]
        real["net"] = {"en0": dict(next(iter(real["net"].values()), {"rx": 0, "tx": 0, "rx_tot": 0, "tx_tot": 0, "hist_rx": [0], "hist_tx": [0]}))}
        return real
    real["sessions"] = {"local": [{"user": "alice", "tty": "tty1"}], "ssh": [LAN + ".20"]}
    real["fs"] = [{"mount": "/", "used": 180 * 2 ** 30, "total": 480 * 2 ** 30}, {"mount": "/data", "used": 1200 * 2 ** 30, "total": 2000 * 2 ** 30}]
    real["net"] = {"eth0": dict(next(iter(real["net"].values()), {"rx": 0, "tx": 0, "rx_tot": 0, "tx_tot": 0, "hist_rx": [0], "hist_tx": [0]}))}
    return real
