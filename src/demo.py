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


# ---- the CPU screen (render.py --view cpu): the three producers' data as they would read on three invented machines ---------
# Linux: an Intel hybrid laptop (i7-1260P, 4 P-cores with two threads each + 8 E-cores). Windows: an AMD 8-core desktop.
# macOS: an Apple M2. The figures move a little from one second to the next (the web page is alive), the same at the same time.

UP_LINUX, UP_WINDOWS, UP_DARWIN = 3 * 86400 + 4 * 3600 + 12 * 60, 26 * 3600 + 33 * 60, 2 * 86400 + 6 * 3600
MiB = 2 ** 20


def _wob(now, i, span):
    """A deterministic wobble in -span..span: the same at the same second, a little different two seconds later."""
    return ((int(now // 2) * 7919 + i * 104729) % (2 * span + 1)) - span


def _usage(now, busy, iowait=None):
    """usage{} from a busy % per logical CPU (user ~2/3, system ~1/4, the rest nice/irq); iowait {cpu: %} where it is known."""
    cores = []
    for i, b in enumerate(busy):
        b = max(0.5, min(100.0, b + _wob(now, i, 3)))
        io = (iowait or {}).get(i, 0.0) if iowait is not None else None
        cores.append({"id": i, "busy": round(b, 1), "user": round(b * 0.68, 1), "system": round(b * 0.27, 1), "iowait": io})
    n = len(cores)
    avg = lambda k: round(sum(x[k] or 0 for x in cores) / n, 1)  # noqa: E731
    busy_t, io_t = avg("busy"), (avg("iowait") if iowait is not None else None)
    total = {"busy": busy_t, "user": avg("user"), "system": avg("system"), "nice": round(busy_t * 0.03, 1), "iowait": io_t,
             "irq": round(busy_t * 0.02, 1) if iowait is not None else None, "steal": 0.0 if iowait is not None else None,
             "idle": round(100 - busy_t - (io_t or 0), 1)}
    return {"total": total, "cores": cores}


def _cpu_linux(now):
    busy = [38, 14, 27, 9, 97, 44, 21, 8, 31, 17, 12, 9, 23, 6, 15, 5]
    ghz = [3.92, 3.88, 3.41, 3.40, 4.38, 4.37, 2.95, 2.96, 2.81, 2.77, 2.12, 1.98, 3.05, 2.40, 1.86, 2.20]
    core_of = {i: (i // 2) * 4 for i in range(8)}
    core_of.update({8 + i: 16 + i for i in range(8)})
    hot = {0: 63, 4: 66, 8: 94, 12: 68, 16: 57, 17: 58, 18: 55, 19: 59, 20: 56, 21: 58, 22: 54, 23: 57}
    cores = {k: float(v + _wob(now, k, 1)) for k, v in hot.items()}
    pkg = 78.0 + _wob(now, 99, 1)
    return {"model": "12th Gen Intel(R) Core(TM) i7-1260P", "vendor": "GenuineIntel", "arch": "x86_64", "sockets": 1, "cores": 12,
            "threads": 16, "kinds": {"P": list(range(8)), "E": list(range(8, 16))}, "core_of": core_of,
            "cache": {"L1d": 448 * 1024, "L1i": 640 * 1024, "L2": 9 * MiB, "L3": 18 * MiB},
            "freq": {"cur": {i: round(g * 1000 + _wob(now, i, 40)) for i, g in enumerate(ghz)}, "min": 400, "max": 4700, "base": 2100,
                     "governor": "powersave", "driver": "intel_pstate"},
            "usage": _usage(now, busy, {2: 6.0, 9: 3.0}), "load": [2.41, 1.98, 1.75],
            "rates": {"ctxt": 18342 + 37 * _wob(now, 1, 9), "intr": 6127 + 11 * _wob(now, 2, 9), "running": 4, "blocked": 1},
            "uptime": UP_LINUX + now % 60,
            "temps": {"package": pkg, "cores": cores, "high": 100.0, "crit": 100.0, "source": "coretemp",
                      "sensors": [{"label": "Package id 0", "c": pkg, "high": 100.0, "crit": 100.0}]
                      + [{"label": f"Core {k}", "c": v, "high": 100.0, "crit": 100.0} for k, v in sorted(cores.items())]},
            "throttle": {"package": 1204, "cores": {8: 812, 4: 37}, "package_s": 252.4}, "notes": []}


def _cpu_windows(now):
    busy = [22, 9, 61, 12, 18, 7, 33, 10, 15, 6, 41, 8, 12, 5, 19, 7]
    ghz = [4.42, 4.40, 4.61, 4.58, 3.80, 3.80, 4.48, 4.45, 3.80, 3.80, 4.52, 4.50, 3.80, 3.80, 4.21, 4.20]
    return {"model": "AMD Ryzen 7 5800X 8-Core Processor", "vendor": "AuthenticAMD", "arch": "AMD64", "sockets": 1, "cores": 8,
            "threads": 16, "kinds": {}, "cache": {"L1d": 256 * 1024, "L1i": 256 * 1024, "L2": 4 * MiB, "L3": 32 * MiB},
            "freq": {"cur": {i: round(g * 1000 + _wob(now, i, 30)) for i, g in enumerate(ghz)}, "min": None, "max": 3801, "base": 3801,
                     "governor": None, "driver": None},
            "usage": _usage(now, busy), "load": None,
            "rates": {"ctxt": 24311 + 53 * _wob(now, 3, 9), "intr": 9877 + 17 * _wob(now, 4, 9), "running": None, "blocked": None},
            "uptime": UP_WINDOWS + now % 60,
            "temps": {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None},  # the collector's (sensors.json)
            "throttle": {"package": None, "cores": {}, "package_s": None}, "notes": []}


def _cpu_darwin(now):
    busy = [41, 37, 29, 22, 88, 52, 34, 17]
    return {"model": "Apple M2", "vendor": "Apple", "arch": "arm64", "sockets": 1, "cores": 8, "threads": 8, "kinds": {"P": 4, "E": 4},
            "cache": {"L1d": 128 * 1024, "L1i": 192 * 1024, "L2": 16 * MiB},
            "freq": {"cur": {}, "min": None, "max": None, "base": None, "governor": None, "driver": None},
            "usage": _usage(now, busy, iowait=None), "load": [3.12, 2.87, 2.40],
            "rates": {"ctxt": None, "intr": None, "running": None, "blocked": None}, "uptime": UP_DARWIN + now % 60,
            "temps": {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None},
            "throttle": {"package": None, "cores": {}, "package_s": None}, "notes": []}


def cpu_sample(os_name=None, now=None):
    """What cpuinfo.CpuSampler.sample() returns on the demo machine of that OS (see the contract in docs/DESIGN.md).
    `core_of` ({logical CPU: physical core id}) is the topology the per-CPU temperatures need: Linux only here."""
    now = now or time.time()
    return {"windows": _cpu_windows, "darwin": _cpu_darwin}.get(os_name, _cpu_linux)(now)


def sensors(os_name=None, now=None):
    """The collector's sensors.json on macOS/Windows (Linux has none: the renderer reads sysfs), as it comes out of json.load:
    the per-core keys are strings."""
    now = now or time.time()
    if os_name == "windows":
        cores = {1: 66.5, 2: 68.0, 3: 89.5, 4: 70.25, 5: 64.75, 6: 67.0, 7: 71.5, 8: 65.0}
        cpu = {"package": 74.5 + _wob(now, 7, 1), "cores": {str(k): v for k, v in cores.items()},
               "sensors": [{"label": "Core (Tctl/Tdie)", "c": 74.5}, {"label": "CCD1 (Tdie)", "c": 69.25}]
               + [{"label": f"Core #{k}", "c": v} for k, v in cores.items()],
               "source": "LibreHardwareMonitor", "pressure": None, "clusters": []}
        return {"ts": now - 4, "os": "windows", "cpu": cpu, "errors": {}, "absent": ["OpenHardwareMonitor"]}
    if os_name == "darwin":
        cpu = {"package": 61.5 + _wob(now, 8, 1), "cores": {}, "sensors": [{"label": "CPU die (smctemp)", "c": 61.5}],
               "source": "smctemp", "pressure": "Moderate",
               "clusters": [{"name": "E-Cluster", "mhz": 2064.0, "active": 38.2, "cpus": {str(i): 2064.0 - 40 * i for i in range(4)}},
                            {"name": "P-Cluster", "mhz": 3204.0, "active": 61.5, "cpus": {str(i): 3204.0 + (36 if i == 4 else 0) for i in range(4, 8)}}]}
        return {"ts": now - 6, "os": "darwin", "cpu": cpu, "errors": {}, "absent": ["osx-cpu-temp"]}
    return None


# pid, ppid, user, name, state, threads, nice, CPU %, RSS MiB, CPU time s, started (s after boot); None = could not be read
_PROCS_LINUX = (
    (1, 0, "root", "systemd", "S", 1, 0, 0.1, 13, 412.3, 1), (2, 0, "root", "kthreadd", "S", 1, 0, 0.0, 0, 0.4, 1),
    (14, 2, "root", "ksoftirqd/0", "S", 1, 0, 0.3, 0, 98.1, 1), (15, 2, "root", "rcu_preempt", "I", 1, 0, 0.7, 0, 210.6, 1),
    (213, 2, "root", "kworker/u32:3-events_unbound", "I", 1, 0, 0.4, 0, 51.2, 8000),
    (288, 2, "root", "kworker/4:1H-kblockd", "D", 1, -20, 0.2, 0, 12.9, 40),
    (412, 1, "root", "systemd-journal", "S", 1, 0, 0.2, 48, 133.0, 3), (440, 1, "root", "systemd-udevd", "S", 1, 0, 0.0, 11, 9.4, 3),
    (612, 1, "systemd-resolve", "systemd-resolve", "S", 1, 0, 0.1, 14, 41.7, 5), (701, 1, "root", "cron", "S", 1, 0, 0.0, 3, 6.1, 6),
    (733, 1, "root", "tailscaled", "S", 14, 0, 1.2, 46, 1820.4, 6), (745, 1, "root", "sshd", "S", 1, 0, 0.0, 9, 2.1, 6),
    (812, 1, "root", "containerd", "S", 18, 0, 0.8, 52, 3010.2, 7), (901, 1, "root", "dockerd", "S", 24, 0, 1.5, 98, 4402.7, 8),
    (1020, 1, "root", "fail2ban-server", "S", 4, 0, 0.2, 31, 640.0, 9), (1102, 1, "root", "agetty", "S", 1, 0, 0.0, 2, 0.0, 9),
    (1180, 1, "nuc-console", "python3", "S", 3, 0, 1.9, 38, 2211.8, 10),
    (1401, 1, "root", "containerd-shim", "S", 12, 0, 0.1, 13, 88.0, 30), (1433, 1, "root", "containerd-shim", "S", 12, 0, 0.1, 14, 91.2, 30),
    (1467, 1, "root", "containerd-shim", "S", 11, 0, 0.1, 12, 79.5, 31), (1502, 1, "root", "containerd-shim", "S", 11, 0, 0.0, 12, 70.3, 31),
    (1520, 1467, "999", "postgres", "S", 1, 0, 0.3, 58, 301.0, 32), (1588, 1520, "999", "postgres", "S", 1, 0, 0.1, 22, 64.0, 32),
    (1589, 1520, "999", "postgres", "S", 1, 0, 0.2, 19, 88.5, 32), (1590, 1520, "999", "postgres", "R", 1, 0, 21.7, 140, 2410.0, 32),
    (1610, 1433, "1000", "node", "R", 11, 0, 96.4, 412, 18233.0, 33), (2477, 1610, "1000", "sh", "Z", 1, 0, 0.0, 0, 0.1, 9000),
    (1702, 1401, "root", "nginx", "S", 1, 0, 0.0, 8, 3.2, 33), (1703, 1702, "www-data", "nginx", "S", 1, 0, 0.4, 11, 210.4, 33),
    (2290, 1502, "999", "redis-server", "S", 5, 0, 0.6, 9, 520.1, 34),
    (2350, 1, "alice", "java", "S", 52, 0, 63.8, 2300, 50210.0, 600),
    (2210, 1, "alice", "ffmpeg", "R", 18, 10, 287.5, 640, 9120.5, 250000),
    (2501, None, None, "gvfsd-fuse", None, None, None, None, None, None, None),
    (2600, 1, "root", "snapd", "S", 21, 0, 0.1, 38, 120.0, 12), (2700, 1, "root", "unattended-upgr", "S", 2, 0, 0.0, 26, 4.4, 14),
    (2800, 1, "root", "smartd", "S", 1, 0, 0.0, 6, 1.2, 14), (2900, 1, "alice", "rsync", "D", 1, 0, 12.4, 9, 300.0, 270000),
    (3120, 745, "alice", "sshd", "S", 1, 0, 0.0, 7, 0.4, 272000), (3122, 3120, "alice", "bash", "S", 1, 0, 0.0, 5, 0.2, 272000),
    (3301, 3122, "alice", "htop", "S", 1, 0, 1.1, 5, 3.2, 273000))
_PROCS_WINDOWS = (
    (4, 0, None, "System", None, 214, None, 1.6, None, None, 0), (108, 4, None, "Registry", None, 4, None, None, None, None, 0),
    (412, 4, None, "smss.exe", None, 2, None, None, None, None, 1), (588, 572, None, "csrss.exe", None, 12, None, None, None, None, 3),
    (680, 572, "SYSTEM", "wininit.exe", None, 1, None, 0.0, 6, 0.3, 3), (760, 680, "SYSTEM", "services.exe", None, 9, None, 0.2, 11, 92.1, 3),
    (784, 680, "SYSTEM", "lsass.exe", None, 10, None, 0.1, 21, 61.0, 3),
    (912, 760, "SYSTEM", "svchost.exe", None, 18, None, 0.3, 29, 210.5, 4), (980, 760, "NETWORK SERVICE", "svchost.exe", None, 12, None, 0.1, 14, 80.2, 4),
    (1044, 760, "LOCAL SERVICE", "svchost.exe", None, 7, None, 0.0, 9, 12.4, 4), (1188, 760, "SYSTEM", "svchost.exe", None, 22, None, 1.4, 61, 1404.0, 4),
    (1342, 760, "LOCAL SERVICE", "svchost.exe", None, 5, None, 0.0, 7, 3.1, 5), (1560, 760, "SYSTEM", "spoolsv.exe", None, 11, None, 0.0, 13, 8.8, 6),
    (2011, 760, "SYSTEM", "MsMpEng.exe", None, 41, None, 34.2, 288, 3310.0, 8), (2240, 760, "SYSTEM", "sshd.exe", None, 3, None, 0.0, 8, 1.2, 9),
    (2318, 760, "SYSTEM", "tailscaled.exe", None, 19, None, 0.9, 51, 702.0, 9),
    (2402, 760, "SYSTEM", "com.docker.service", None, 14, None, 0.2, 38, 140.4, 10),
    (3104, 3088, "alice", "explorer.exe", None, 78, None, 1.2, 160, 980.6, 40), (3360, 3104, "alice", "OneDrive.exe", None, 31, None, 0.1, 90, 77.0, 60),
    (3720, 3104, "alice", "Docker Desktop.exe", None, 40, None, 0.8, 210, 300.2, 70),
    (3801, 760, "SYSTEM", "com.docker.backend.exe", None, 26, None, 2.1, 120, 801.0, 75),
    (3990, 4, "SYSTEM", "vmmem", None, 16, None, 112.6, 3800, 42010.0, 80),
    (4120, 3104, "alice", "chrome.exe", None, 44, None, 6.4, 380, 2800.0, 300), (4188, 4120, "alice", "chrome.exe", None, 19, None, 44.9, 520, 3105.5, 300),
    (4201, 4120, "alice", "chrome.exe", None, 12, None, 0.4, 140, 98.0, 310), (4230, 4120, "alice", "chrome.exe", None, 15, None, 2.2, 210, 401.0, 320),
    (4412, 3104, "alice", "Code.exe", None, 36, None, 3.9, 410, 1520.0, 900), (4460, 4412, "alice", "Code.exe", None, 22, None, 1.1, 260, 610.3, 900),
    (5012, 3104, "alice", "WindowsTerminal.exe", None, 21, None, 0.3, 95, 41.2, 1200),
    (5100, 5012, "alice", "pwsh.exe", None, 18, None, 0.0, 88, 12.0, 1200),
    (5240, 1560, "SYSTEM", "pythonw.exe", None, 4, None, 1.8, 41, 1690.0, 15), (5300, 760, "LOCAL SERVICE", "pythonw.exe", None, 6, None, 0.6, 37, 520.4, 15),
    (6020, 3104, "alice", "Teams.exe", None, 52, None, 9.7, 620, 5402.0, 2000), (6111, 3104, "alice", "Spotify.exe", None, 33, None, 2.6, 240, 1802.0, 3000),
    (7004, 912, "SYSTEM", "SearchIndexer.exe", None, 17, None, 18.4, 110, 2501.0, 5000),
    (7280, 912, "alice", "RuntimeBroker.exe", None, 8, None, 0.0, 24, 2.2, 6000),
    (8888, 3104, "alice", "ffmpeg.exe", None, 16, None, 241.3, 520, 6020.0, 90000))
_PROCS_DARWIN = (
    (0, None, "root", "kernel_task", "R", 412, 0, 14.2, 18, 30120.0, 0), (1, 0, "root", "launchd", "S", 4, 0, 0.3, 20, 1620.0, 0),
    (98, 1, "root", "logd", "S", 6, 0, 0.6, 14, 940.0, 2), (102, 1, "root", "UserEventAgent", "S", 3, 0, 0.0, 8, 11.0, 2),
    (106, 1, "root", "configd", "S", 9, 0, 0.1, 10, 220.0, 2), (112, 1, "root", "powerd", "S", 3, 0, 0.0, 4, 31.0, 2),
    (118, 1, "_windowserver", "WindowServer", "S", 21, -20, 17.8, 310, 21011.0, 4),
    (130, 1, "root", "mds", "S", 11, 0, 0.4, 41, 1404.0, 4), (161, 1, "_spotlight", "mds_stores", "S", 5, 5, 6.7, 96, 3120.0, 5),
    (170, 1, "root", "airportd", "S", 7, 0, 0.0, 9, 120.0, 5), (176, 1, "_coreaudiod", "coreaudiod", "S", 11, -10, 1.4, 22, 640.2, 5),
    (201, 1, "root", "tailscaled", "S", 15, 0, 0.8, 44, 980.0, 6), (240, 1, "root", "sshd", "S", 1, 0, 0.0, 6, 0.6, 6),
    (305, 1, "root", "python3", "S", 3, 0, 1.6, 35, 1702.0, 7),
    (511, 1, "alice", "Finder", "S", 8, 0, 0.1, 98, 66.0, 60), (514, 1, "alice", "Dock", "S", 4, 0, 0.0, 61, 31.0, 60),
    (520, 1, "alice", "ControlCenter", "S", 7, 0, 0.2, 55, 42.0, 60), (602, 1, "alice", "Safari", "S", 18, 0, 3.1, 240, 1840.0, 300),
    (640, 1, "alice", "com.apple.WebKit.WebContent", "S", 12, 0, 42.6, 690, 3600.0, 310),
    (641, 1, "alice", "com.apple.WebKit.Networking", "S", 9, 0, 0.7, 70, 210.0, 310),
    (702, 1, "alice", "Docker Desktop", "S", 31, 0, 0.9, 180, 400.0, 400), (731, 702, "alice", "com.docker.backend", "S", 22, 0, 1.8, 140, 900.0, 405),
    (760, 731, "alice", "com.apple.Virtualization.VirtualMachine", "R", 14, 0, 85.3, 2900, 30400.0, 410),
    (812, 1, "alice", "Terminal", "S", 6, 0, 0.4, 72, 64.0, 900), (815, 812, "alice", "login", "S", 2, 0, 0.0, 4, 0.1, 900),
    (816, 815, "alice", "zsh", "S", 1, 0, 0.0, 5, 0.4, 900), (901, 816, "alice", "node", "R", 9, 0, 97.8, 380, 12011.0, 5000),
    (902, 901, "alice", "node", "Z", 1, 0, 0.0, 0, 0.2, 9000), (950, 1, "alice", "Music", "S", 13, 0, 1.2, 130, 320.0, 7000),
    (990, 1, "alice", "Code Helper (Renderer)", "S", 17, 0, 4.4, 330, 940.0, 8000), (991, 1, "alice", "Code Helper", "S", 11, 0, 0.6, 90, 120.0, 8000),
    (1021, 1, "alice", "rsync", "U", 1, 0, 8.9, 9, 310.0, 150000), (1100, 1, "alice", "Slack Helper", "S", 14, 0, 2.2, 240, 610.0, 9000),
    (1201, 1, "root", "softwareupdated", "S", 4, 0, 0.0, 28, 12.0, 20000), (1302, 1, "alice", "Spotlight", "S", 6, 0, 0.1, 44, 22.0, 20000),
    (1400, 1, None, "XprotectService", "S", None, None, None, None, None, None),
    (1501, 816, "alice", "ffmpeg", "R", 12, 10, 236.0, 410, 4020.0, 200000))
_RAM = {"linux": 16 * 2 ** 30, "windows": 32 * 2 ** 30, "darwin": 16 * 2 ** 30}


def proc_sample(os_name=None, now=None):
    """What procs.ProcSampler.sample() returns on the demo machine: ~40 processes, a few busy ones, a zombie (Linux, macOS), some
    the sampler could not read (fields None: on Windows the protected ones, like a real non-administrator view)."""
    now = now or time.time()
    os_name = os_name if os_name in ("windows", "darwin") else "linux"
    table = {"windows": _PROCS_WINDOWS, "darwin": _PROCS_DARWIN}.get(os_name, _PROCS_LINUX)
    boot = now - {"linux": UP_LINUX, "windows": UP_WINDOWS, "darwin": UP_DARWIN}[os_name]
    prio = {"linux": lambda ni: 20 + ni, "windows": lambda ni: 8, "darwin": lambda ni: 31 - ni}[os_name]
    procs = []
    for i, (pid, ppid, user, name, state, thr, nice, cpu, rss, tm, start) in enumerate(table):
        if cpu is not None and cpu >= 1:
            cpu = round(cpu * (1 + _wob(now, i, 4) / 100.0), 1)
        procs.append({"pid": pid, "ppid": ppid, "user": user, "name": name, "state": state, "threads": thr, "nice": nice,
                      "prio": prio(nice or 0) if thr is not None else None, "cpu": cpu,
                      "mem": rss * MiB if rss is not None else None,
                      "mem_pct": round(rss * MiB * 100.0 / _RAM[os_name], 1) if rss is not None else None,
                      "time": tm, "start": boot + start if start is not None else None})
    unreadable = sum(1 for p in procs if p["cpu"] is None and p["mem"] is None)
    total = {"count": len(procs) + 1, "running": sum(p["state"] == "R" for p in procs) if os_name != "windows" else None,
             "threads": sum(p["threads"] or 0 for p in procs) + 37, "unreadable": unreadable + 1}  # +1: one we could not even list
    return {"procs": procs, "total": total, "notes": []}
