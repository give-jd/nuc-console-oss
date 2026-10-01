"""Synthetic data for `render.py --once --demo` (screenshots, trying the dashboard without root, docs).

Invented: hostnames, users, containers and addresses (documentation ranges). CPU, RAM, temperature, uptime and root-disk
figures are the real ones of the machine running it (read from /proc and /sys); nothing is written and no real state is read.
"""
import random
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


# ---- HEALTH: a report as health.report() returns it (docs/DESIGN.md "## Health"), for --demo and the tests ------------------------------

HEALTH_VARIANTS = ("", "little", "none")  # "": a machine with 16 days of history; little: 5 hours; none: the report of an empty history
HISTORY_DAYS = 16                         # how long the demo machine has been recorded: 30 days of report cover only these 16


def _fix(os_name, linux, darwin, windows):
    return {"darwin": darwin, "windows": windows}.get(os_name, linux)


def _series(name, n, base, spread=0.5, have=None):
    """n values around base (a fixed shape per name: the demo is the same at every run); the first n - have are None (not recorded yet)."""
    rnd = random.Random(sum(map(ord, name)))
    have = n if have is None else min(n, have)
    return [None] * (n - have) + [round(base * (1 - spread / 2 + spread * rnd.random()), 1) for _ in range(have)]


def _growing(name, n, start, slope, have):
    """RSS (MB) that grows by `slope` MB per step from `start`, with a little noise (a leak): None before the first record."""
    rnd = random.Random(sum(map(ord, name)))
    have = min(n, have)
    return [None] * (n - have) + [round(start + slope * i + 20 * rnd.random(), 1) for i in range(have)]


def _ev(subject, n, last, scale):
    return {"subject": subject, "n": max(1, round(n * scale)), "last": last}


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _finding(rule, subject, level, title, text, facts, fix):
    return {"id": "%s:%s" % (rule, subject or "host"), "level": level, "title": title, "text": text, "fix": fix, "facts": facts, "subject": subject}


def _times(n):
    return "1 time" if n == 1 else "%d times" % n


def _health_linux(now, days):
    scale = days / 7.0
    last = lambda h: int(now - h * 3600)  # noqa: E731
    when = lambda h: _when(last(h))  # noqa: E731
    span = "the last 24 hours" if days == 1 else "the last %d days" % days
    oom_n, restarts, hot_h = max(1, round(3 * scale)), max(9, round(31 * scale)), max(2, round(14 * scale))
    hog_h, logins, noisy = max(6, round(31 * scale)), round(1204 * scale), round(18420 * scale)
    f = _finding
    found = [  # (the shortest period whose rules can say it, the finding)
        (1, f("oom", "shop-worker-1", "err", "Out of memory: shop-worker-1",
              "shop-worker-1 was hit by an out-of-memory event %s in %s (last %s)." % (_times(oom_n), span, when(2.5)),
              {"n": oom_n, "last": last(2.5)},
              "journalctl -k | grep -i 'killed process'; free -h; give it a limit it respects (docker update --memory 2g <name> or MemoryMax= in a "
              "drop-in) and raise it if the app really needs more; add RAM or swap; look for a leak (mem-leak)")),
        (1, f("restart-loop", "shop-worker-1", "warn", "Restarting: shop-worker-1",
              "shop-worker-1 restarted 9 times within 24 hours (%d in total) and exited with an error 3 times in %s (last %s)."
              % (restarts, span, when(2.4)),
              {"max_restarts_24h": 9, "restarts": restarts, "exit_errors": 3, "last": last(2.4)},
              "docker ps -a; docker inspect <name> (State.ExitCode, State.OOMKilled); docker logs --tail 100 <name>; systemctl status <unit>; fix the "
              "cause; a restart back-off (RestartSec= in the unit) keeps a crash loop from burning CPU")),
        (1, f("disk-full", "/data", "warn", "Disk filling up: /data", "/data is 83% full, growing 4.1 GB/day: full in about 12 days.",
              {"used_gb": 1660.4, "total_gb": 2000.0, "growth_gb_day": 4.1, "fit_days": 30, "used_pct": 83.0, "days_to_full": 12.4},
              "df -h; du -xh --max-depth=1 / 2>/dev/null | sort -rh | head; docker system df (docker image prune, docker builder prune); "
              "journalctl --vacuum-size=500M; apt clean")),
        (3, f("mem-leak", "node", "warn", "Memory keeps growing: node",
              "node grew about 180 MB/day over 5 days (from 0.9 GB to 1.8 GB, r2 0.93), a leak is possible.",
              {"slope_mb_day": 180.2, "days": 5, "start_mb": 920, "now_mb": 1820, "r2": 0.93},
              "ps -eo pid,rss,comm --sort=-rss | head; docker stats --no-stream; stopgap: systemctl restart <unit> / docker restart <name>; cap it so "
              "it cannot starve the rest: docker update --memory 1g --memory-swap 1g <name> or MemoryMax=1G in a drop-in; then update it or report the leak")),
        (1, f("cpu-hog", "chrome", "warn", "Keeps the CPU busy: chrome",
              "chrome used over 80%% of one core for %d hours in %s (peak 143%%, busiest at %s)." % (hog_h, span, when(27)),
              {"hours_over_80pct_core": hog_h, "hours_over_half_cores": 0, "cores": 4, "avg_pct": 62.3, "peak_pct": 143.0,
               "cpu_s": round(375000 * scale), "peak_hour": last(27)},
              "top (or htop) shows it now; docker stats / systemctl status <unit>; cap it: docker update --cpus 2 <name> or CPUQuota=200% in a "
              "systemd drop-in (systemctl edit <unit>); check its log for a loop")),
        (1, f("thermal", None, "warn", "Running hot",
              "%d hours at or above the temperature limit in %s (about 2.0 h/day, max 91 C); busiest then: chrome, node, shop-worker-1." % (hot_h, span),
              {"hours_hot": hot_h, "hours_per_day": 2.0, "max_c": 91.0, "apps": "chrome, node, shop-worker-1"},
              "sensors; clean the dust, check the fans and the airflow, move the box off the heat; reduce the load of the apps listed")),
        (3, f("login-fail", "sshd", "warn", "Failed logins: sshd",
              "412 failed logins on 2026-09-28 against a median of 18 a day; %d in %s." % (logins, span),
              {"peak_day": 412, "peak_date": "2026-09-28", "median_day": 18, "total": logins, "days_over": 2},
              "journalctl -u ssh -u sshd | grep -i fail | tail; fail2ban-client status sshd; sudo ufw limit 22/tcp; PasswordAuthentication no in "
              "sshd_config; do not expose port 22 to the Internet (use a VPN or Tailscale)")),
        (1, f("log-noisy", "dockerd", "info", "Noisy log: dockerd",
              "dockerd logged one message %d times in %s (about 2631 a day): \"level=<v> msg=<str> error=<str>\"." % (noisy, span),
              {"n": noisy, "per_day": 2631, "source": "journal", "template": "level=<v> msg=<str> error=<str>"},
              "journalctl -u <unit> -n 50 -p warning; docker logs --tail 50 <name>; fix what it complains about or lower its log level; "
              "LogRateLimitIntervalSec in journald.conf as a stopgap")),
        (3, f("log-new", None, "info", "New log messages",
              "4 log messages appeared for the first time in the last 24 hours, most often from kernel (412 times).",
              {"new_templates": 4, "top_unit": "kernel", "top_n": 412, "top_template": "nvme nvme<n>: I/O <n> QID <n> timeout, aborting"},
              "journalctl -p warning --since '24 hours ago' | tail -50; new messages after an update or a config change are normal, otherwise look "
              "at the unit that writes them")),
        (7, f("boot-regression", None, "info", "Slower boot", "The last boot took 58 s, 2.5x the median of the 7 boots before (23 s).",
              {"last_s": 58.4, "median_s": 23.0, "ratio": 2.54, "boots": 7},
              "systemd-analyze blame | head; systemd-analyze critical-chain; disable what is new and slow (systemctl disable <unit>)")),
    ]
    findings = [x for d, x in found if days >= d]
    # app, share of CPU time, average % of one core, busiest hour (hours ago), RSS average and maximum (GB), RSS trend (MB/day)
    names = [("chrome", .31, 62.3, 27, 2.4, 3.9, 14.5), ("node", .14, 28.0, 51, 1.3, 1.9, 180.2), ("dockerd", .06, 11.8, 5, .3, .4, None),
             ("postgres", .05, 9.7, 77, .9, 1.1, 3.1), ("python3", .04, 7.2, 33, .2, .5, None), ("shop-worker-1", .03, 5.9, 2, .7, 2.0, None)]
    n = days if days > 1 else 24
    have = min(n, HISTORY_DAYS if days > 1 else 24)
    top_cpu = [{"app": a, "cpu_s": round(1210000 * scale * sh, 1), "share": sh, "avg_pct": avg, "peak_hour": last(pk),
                "series": _series("cpu" + a, n, avg, 0.9, have)} for a, sh, avg, pk, _, _, _ in names]
    top_mem = [{"app": a, "rss_max": int(mx * 2 ** 30), "rss_avg": int(av * 2 ** 30), "trend_mb_day": tr if days >= 3 else None,
                "series": _series("mem" + a, n, av * 1024, 0.25, have) if tr is None or days == 1 else
                _growing("mem" + a, n, av * 1024 - tr * have / 2, tr, have)}
               for a, _, _, _, av, mx, tr in sorted(names, key=lambda x: -x[4])]
    events = {"oom": [_ev("shop-worker-1", 3, last(2.5), scale)],
              "crash": [_ev("chrome", 4, last(30), scale), _ev("snapd", 1, last(90), scale)],
              "restart": [_ev("shop-worker-1", 31, last(2.4), scale)],
              "exit_error": [_ev("shop-worker-1", 3, last(2.4), scale)],
              "service_failed": [_ev("snapd.service", 2, last(90), scale)],
              "throttle": [_ev("host", 6, last(27), scale)],
              "login_fail": [_ev("sshd", 1204, last(1.2), scale)]}
    logs = [("dockerd", "level=<v> msg=<str> error=<str>", 18420, False), ("kernel", "nvme nvme<n>: I/O <n> QID <n> timeout, aborting", 412, True),
            ("sshd", "Failed password for <str> from <ip> port <n> ssh2", 1204, False),
            ("systemd-resolved", "Using degraded feature set UDP instead of UDP+EDNS0 for DNS server <ip>.", 260, False),
            ("snapd", "cannot refresh snap <str>: <str>", 38, True), ("cron", "(<str>) CMD (<path>)", 2016, False)]
    logs = sorted(({"source": "journal", "unit": u, "template": t, "n": max(1, round(k * scale)), "new": nw} for u, t, k, nw in logs),
                  key=lambda r: -r["n"])
    disks = [{"mount": "/data", "used_pct": 83.0, "days_to_full": 12.4}, {"mount": "/", "used_pct": 37.5, "days_to_full": None},
             {"mount": "/boot/efi", "used_pct": 6.0, "days_to_full": None}]
    thermal = {"hours_hot": hot_h, "max": 91.0, "apps_when_hot": [
        {"app": "chrome", "cpu_s": round(41000 * scale, 1), "share": 0.38}, {"app": "node", "cpu_s": round(18000 * scale, 1), "share": 0.17},
        {"app": "shop-worker-1", "cpu_s": round(9000 * scale, 1), "share": 0.08}]}
    boots = [{"boot": int(now) - (8 - i) * 86400 * 2, "total_s": t} for i, t in enumerate((21.8, 22.5, 23.1, 21.9, 24.6, 22.2, 23.0, 58.4))]
    notes = ["memory trends need a period of at least 3 days"] if days < 3 else []
    return findings, top_cpu, top_mem, events, logs, disks, thermal, boots, notes


def _health_windows(now, days):
    scale = days / 7.0
    last = lambda h: int(now - h * 3600)  # noqa: E731
    when = lambda h: _when(last(h))  # noqa: E731
    span = "the last 24 hours" if days == 1 else "the last %d days" % days
    crashes, dcom = max(3, round(4 * scale)), round(2311 * scale)
    f = _finding
    findings = [
        f("unexpected-shutdown", None, "err", "Unexpected shutdown",
          "The machine shut down unexpectedly 1 time in %s (last %s)." % (span, when(14)), {"n": 1, "last": last(14)},
          "Event Viewer > Windows Logs > System: Kernel-Power 41 and the BugCheck 1001 after it; check the power supply, the temperature and "
          "the drivers; analyse C:\\Windows\\Minidump with WinDbg"),
        f("crash-loop", "contoso-sync.exe", "warn", "Crashing: contoso-sync.exe",
          "contoso-sync.exe crashed %s in %s (last %s)." % (_times(crashes), span, when(3)),
          {"n": crashes, "crashes": crashes, "hangs": 0, "last": last(3)},
          "Event Viewer > Windows Logs > Application: Application Error / Application Hang name the faulting module; update or reinstall it; "
          "sfc /scannow if it is a system component"),
        f("crash-loop", "Spooler", "warn", "Crashing: Spooler", "Spooler crashed 3 times in %s (last %s)." % (span, when(60)),
          {"n": 3, "crashes": 3, "hangs": 0, "last": last(60)},
          "Get-Service <name>; Event Viewer > Windows Logs > System (Service Control Manager 7031) says why it stopped; services.msc > Recovery: "
          "restart with a delay"),
        f("cpu-share", "MsMpEng.exe", "info", "Biggest CPU user: MsMpEng.exe",
          "MsMpEng.exe used 27%% of all CPU time in %s (average 17%% of one core, busiest at %s)." % (span, when(31)),
          {"share_pct": 27.0, "avg_pct": 17.1, "cpu_s": round(167000 * scale), "peak_hour": last(31)},
          "expected? then nothing to do. Else: Task Manager > Details shows what it does"),
        f("log-noisy", "System", "info", "Noisy log: System",
          "System logged one message %d times in %s (about 330 a day): \"Microsoft-Windows-DistributedCOM 10016\"." % (dcom, span),
          {"n": dcom, "per_day": 330, "source": "System", "template": "Microsoft-Windows-DistributedCOM 10016"},
          "Event Viewer > Windows Logs: filter by the source; fix its cause"),
    ]
    if days == 1:  # one day: the service crash of two days ago is not in the period
        findings = [x for x in findings if x["id"] != "crash-loop:Spooler"]
    names = [("MsMpEng.exe", .27, 17.1, 31, .4, .6, None), ("chrome.exe", .18, 11.4, 12, 2.1, 3.2, 20.5), ("Code.exe", .09, 5.7, 6, 1.2, 1.9, None),
             ("SearchIndexer.exe", .07, 4.4, 44, .1, .2, None), ("svchost.exe", .05, 3.1, 8, .3, .4, None)]
    n = days if days > 1 else 24
    have = min(n, HISTORY_DAYS if days > 1 else 24)
    top_cpu = [{"app": a, "cpu_s": round(620000 * scale * sh, 1), "share": sh, "avg_pct": avg, "peak_hour": last(pk),
                "series": _series("cpu" + a, n, avg, 0.9, have)} for a, sh, avg, pk, _, _, _ in names]
    top_mem = [{"app": a, "rss_max": int(mx * 2 ** 30), "rss_avg": int(av * 2 ** 30), "trend_mb_day": tr if days >= 3 else None,
                "series": _series("mem" + a, n, av * 1024, 0.25, have)} for a, _, _, _, av, mx, tr in sorted(names, key=lambda x: -x[4])[:4]]
    events = {"crash": [_ev("contoso-sync.exe", 4, last(3), scale), _ev("Spooler", 3, last(60), scale)],
              "hang": [_ev("explorer.exe", 1, last(100), scale)],
              "unexpected_shutdown": [{"subject": "host", "n": 1, "last": last(14)}],
              "service_failed": [_ev("WSearch", 2, last(70), scale)]}
    if days == 1:
        events = {"crash": [_ev("contoso-sync.exe", 4, last(3), scale)], "unexpected_shutdown": [{"subject": "host", "n": 1, "last": last(14)}]}
    logs = [{"source": "System", "unit": "", "template": "Microsoft-Windows-DistributedCOM 10016", "n": dcom, "new": False},
            {"source": "System", "unit": "", "template": "Service Control Manager 7031", "n": max(1, round(3 * scale)), "new": False},
            {"source": "Application", "unit": "", "template": "Application Error 1000", "n": max(1, round(4 * scale)), "new": False},
            {"source": "System", "unit": "", "template": "Microsoft-Windows-Kernel-Power 41", "n": 1, "new": True}]
    disks = [{"mount": "C:", "used_pct": 72.4, "days_to_full": None}, {"mount": "D:", "used_pct": 54.1, "days_to_full": None}]
    thermal = {"hours_hot": 0, "max": None, "apps_when_hot": []}  # no temperature sensor file: the screen must say "no data", not "cool"
    boots = [{"boot": int(now) - (6 - i) * 86400 * 2, "total_s": t} for i, t in enumerate((23.3, 24.1, 22.8, 25.0, 23.9, 26.2))]
    notes = ["memory trends need a period of at least 3 days"] if days < 3 else []
    return findings, top_cpu, top_mem, events, logs, disks, thermal, boots, notes


def _health_darwin(now, days):
    scale = days / 7.0
    last = lambda h: int(now - h * 3600)  # noqa: E731
    when = lambda h: _when(last(h))  # noqa: E731
    span = "the last 24 hours" if days == 1 else "the last %d days" % days
    oom_n, crashes = max(1, round(2 * scale)), max(3, round(5 * scale))
    f = _finding
    findings = [
        f("oom", "Google Chrome Helper", "err", "Out of memory: Google Chrome Helper",
          "Google Chrome Helper was hit by an out-of-memory event %s in %s (last %s)." % (_times(oom_n), span, when(15)),
          {"n": oom_n, "last": last(15)},
          "Console > Crash Reports: JetsamEvent-*.ips lists the memory of every process; quit the biggest apps; add RAM"),
        f("crash-loop", "photolibraryd", "warn", "Crashing: photolibraryd",
          "photolibraryd crashed %s in %s (last %s)." % (_times(crashes), span, when(7)),
          {"n": crashes, "crashes": crashes, "hangs": 0, "last": last(7)},
          "Console > Crash Reports (~/Library/Logs/DiagnosticReports, /Library/Logs/DiagnosticReports): open the newest report of the app; "
          "update or reinstall it"),
        f("cpu-share", "mds_stores", "info", "Biggest CPU user: mds_stores",
          "mds_stores used 28%% of all CPU time in %s (average 9%% of one core, busiest at %s)." % (span, when(55)),
          {"share_pct": 28.0, "avg_pct": 9.3, "cpu_s": round(42000 * scale), "peak_hour": last(55)},
          "expected? then nothing to do. Else: Activity Monitor > CPU shows what it does"),
    ]
    names = [("mds_stores", .28, 9.3, 55, .3, .5, None), ("Google Chrome Helper", .21, 7.0, 15, 1.8, 2.9, 11.0), ("WindowServer", .09, 3.1, 9, .5, .6, None),
             ("kernel_task", .08, 2.7, 80, .1, .1, None), ("photolibraryd", .06, 2.0, 7, .4, .7, None)]
    n = days if days > 1 else 24
    have = min(n, HISTORY_DAYS if days > 1 else 24)
    top_cpu = [{"app": a, "cpu_s": round(150000 * scale * sh, 1), "share": sh, "avg_pct": avg, "peak_hour": last(pk),
                "series": _series("cpu" + a, n, avg, 0.9, have)} for a, sh, avg, pk, _, _, _ in names]
    top_mem = [{"app": a, "rss_max": int(mx * 2 ** 30), "rss_avg": int(av * 2 ** 30), "trend_mb_day": tr if days >= 3 else None,
                "series": _series("mem" + a, n, av * 1024, 0.25, have)} for a, _, _, _, av, mx, tr in sorted(names, key=lambda x: -x[4])[:4]]
    events = {"oom": [_ev("Google Chrome Helper", 2, last(15), scale)], "crash": [_ev("photolibraryd", 5, last(7), scale)],
              "hang": [_ev("Finder", 1, last(120), scale)]}
    disks = [{"mount": "/", "used_pct": 61.3, "days_to_full": None}, {"mount": "/Volumes/Data", "used_pct": 46.8, "days_to_full": None}]
    thermal = {"hours_hot": 0, "max": 74.0, "apps_when_hot": []}
    notes = ["memory trends need a period of at least 3 days"] if days < 3 else []
    return findings, top_cpu, top_mem, events, [], disks, thermal, [], notes  # macOS: no log history, no boot time


def health_report(os_name=None, days=7, now=None, variant=""):
    """The dict health.report() returns, for a demo machine of this OS (linux, windows, darwin) and a period of 1, 7 or 30 days.
    Findings, counts and series follow the period like the real rules do (memory trends need 3 days). variant: "little" = 5 hours
    of data (the report says it is still collecting), "none" = the report of an empty history. Extra keys: the rows of top_cpu and
    top_mem carry "series" (CPU seconds / mean RSS per hour for 24 h, per day otherwise; None before the first record), which the
    screen otherwise reads from the history itself."""
    now = int(now or time.time())
    days = days if days in (1, 7, 30) else 7
    period = {"from": now - days * 86400, "to": now, "days": days}
    if variant == "none":
        return {"period": period, "coverage": {"hours": 0, "since": None}, "findings": [], "top_cpu": [], "top_mem": [], "events": {}, "logs": [],
                "disks": [], "thermal": {"hours_hot": 0, "max": None, "apps_when_hot": []}, "boots": [], "notes": ["no history yet"]}
    hours = 5 if variant == "little" else min(days * 24, HISTORY_DAYS * 24)
    since = (now // 3600 - (5 if variant == "little" else HISTORY_DAYS * 24)) * 3600
    build = {"windows": _health_windows, "darwin": _health_darwin}.get(os_name, _health_linux)
    findings, top_cpu, top_mem, events, logs, disks, thermal, boots, notes = build(now, days)
    if variant == "little":  # what the rules say with less than a day: the ones that need days stay silent
        keep = ("restart-loop", "oom", "cpu-share", "crash-loop", "unexpected-shutdown")
        findings = [x for x in findings if x["id"].split(":")[0] in keep][:2]
        top_cpu, top_mem, logs = [dict(x, series=x["series"][-5:]) for x in top_cpu[:3]], [dict(x, trend_mb_day=None, series=x["series"][-5:])
                                                                                          for x in top_mem[:3]], logs[:2]
        events = {k: v for k, v in events.items() if k in ("restart", "oom", "crash")}
        disks, boots = [dict(d, days_to_full=None) for d in disks], boots[-1:]
        thermal = {"hours_hot": 0, "max": thermal["max"] and 61.0, "apps_when_hot": []}
        notes = ["collecting: 5 hours so far; trends need 24 hours of data"]
    return {"period": period, "coverage": {"hours": hours, "since": since}, "findings": findings, "top_cpu": top_cpu, "top_mem": top_mem,
            "events": events, "logs": logs, "disks": disks, "thermal": thermal, "boots": boots, "notes": notes}


# ---- AI: three invented machines and the catalog aisetup.catalog() would hand the AI screen for each ------------------------------
# A Linux box with a 12 GB NVIDIA card, a Windows laptop (16 GB RAM, a 4 GB card, nothing installed yet) and an M2 with 16 GB of unified
# memory. The models are the catalog's candidates with approximate sizes; the verdicts come from _ai_assess(), a small copy of the rules of
# aihw.assess() (the demo must not depend on that module and must give the same screen at every run).

AI_OSES = ("linux", "windows", "darwin")
_AI_MODELS = (  # id, name, licence, params_b, active_b, quant, layers, ctx_max, rank, approx_mb, notes, pinned
    ("gpt-oss-20b", "OpenAI gpt-oss 20B", "Apache-2.0", 21.0, 3.6, "MXFP4", 24, 131072, 1, 12100, "fast for its size (MoE, 3.6B active); reasoning effort is a setting", False),
    ("qwen3-30b-a3b", "Qwen3 30B-A3B", "Apache-2.0", 30.5, 3.3, "Q4_K_M", 48, 40960, 2, 18600, "fast on CPU (MoE: only 3.3B parameters work per token)", True),
    ("phi-4", "Phi-4 14B", "MIT", 14.7, None, "Q4_K_M", 40, 16384, 3, 9100, "strong at reasoning and code; short context (16k)", True),
    ("qwen3-14b", "Qwen3 14B", "Apache-2.0", 14.8, None, "Q4_K_M", 40, 40960, 4, 9000, "thinking mode: /no_think turns it off", True),
    ("qwen3-8b", "Qwen3 8B", "Apache-2.0", 8.2, None, "Q4_K_M", 36, 40960, 5, 5000, "thinking mode: /no_think turns it off", True),
    ("granite-3.3-8b", "IBM Granite 3.3 8B", "Apache-2.0", 8.2, None, "Q4_K_M", 40, 131072, 6, 5000, "long context (128k); tuned for business tasks", True),
    ("phi-4-mini", "Phi-4-mini 3.8B", "MIT", 3.8, None, "Q4_K_M", 32, 131072, 7, 2500, "small and good at reasoning; long context", True),
    ("qwen3-4b", "Qwen3 4B", "Apache-2.0", 4.0, None, "Q4_K_M", 36, 40960, 8, 2500, "the default of nuc-console-ai: good advice in about 4 GB", True),
    ("smollm3-3b", "SmolLM3 3B", "Apache-2.0", 3.1, None, "Q4_K_M", 36, 65536, 9, 1900, "small, multilingual, long context", True),
    ("granite-3.3-2b", "IBM Granite 3.3 2B", "Apache-2.0", 2.5, None, "Q4_K_M", 40, 131072, 10, 1600, "very small; fine for short advice", True),
    ("qwen3-1.7b", "Qwen3 1.7B", "Apache-2.0", 1.7, None, "Q4_K_M", 28, 40960, 11, 1100, "runs anywhere; thinking mode: /no_think", True),
    ("qwen3-0.6b", "Qwen3 0.6B", "Apache-2.0", 0.6, None, "Q4_K_M", 28, 40960, 12, 400, "a toy: too small to give reliable advice", True),
)
_AI_MACHINES = {
    "linux": {"os": "linux", "arch": "x86_64",
              "cpu": {"model": "AMD Ryzen 7 5800X 8-Core Processor", "cores": 8, "threads": 16, "flags": ["avx2"]},
              "ram": {"total_mb": 31923, "available_mb": 21540},
              "gpus": [{"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3060", "vram_mb": 12288, "vram_free_mb": 11264, "unified": False,
                        "backend": "cuda", "source": "nvidia-smi"}],
              "notes": []},
    "windows": {"os": "windows", "arch": "x86_64",
                "cpu": {"model": "Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz", "cores": 6, "threads": 12, "flags": ["avx2"]},
                "ram": {"total_mb": 16176, "available_mb": 7410},
                "gpus": [{"vendor": "nvidia", "name": "NVIDIA GeForce GTX 1650", "vram_mb": 4096, "vram_free_mb": None, "unified": False,
                          "backend": "cuda", "source": "registry"},
                         {"vendor": "intel", "name": "Intel(R) UHD Graphics", "vram_mb": None, "vram_free_mb": None, "unified": True,
                          "backend": "vulkan", "source": "registry"}],
                "notes": ["nvidia-smi not found: the free video memory could not be read"]},
    "darwin": {"os": "darwin", "arch": "arm64",
               "cpu": {"model": "Apple M2", "cores": 8, "threads": 8, "flags": ["neon"]},
               "ram": {"total_mb": 16384, "available_mb": 9830},
               "gpus": [{"vendor": "apple", "name": "Apple M2 (10-core GPU)", "vram_mb": None, "vram_free_mb": None, "unified": True,
                         "backend": "metal", "source": "sysctl"}],
               "notes": []},
}
_AI_STATE = {  # per machine: what is installed, the active model, the runtime, the advisor's [ai] settings and whether its server answers
    "linux": {"installed": ("qwen3-4b", "qwen3-1.7b"), "active": "qwen3-4b", "runtime": {"installed": True, "version": "0.9.3"},
              "dir": "/var/lib/nuc-console-ai", "enabled": True, "endpoint": "http://127.0.0.1:11434/v1",
              "probe": {"state": "answering", "msg": "", "models": ["qwen3-4b"]}},
    "windows": {"installed": (), "active": None, "runtime": {"installed": False, "version": ""},
                "dir": r"C:\ProgramData\nuc-console-ai", "enabled": False, "endpoint": "http://127.0.0.1:11434/v1",
                "probe": {"state": "off", "msg": "[ai] enabled = no in config.ini", "models": []}},
    "darwin": {"installed": ("qwen3-8b",), "active": "qwen3-8b", "runtime": {"installed": True, "version": "0.9.3"},
               "dir": "/usr/local/var/nuc-console-ai", "enabled": True, "endpoint": "http://127.0.0.1:8080/v1",
               "probe": {"state": "down", "msg": "no server on 127.0.0.1:8080: start Ollama or run nuc-console-ai serve", "models": []}},
}
_AI_CPU_GBS, _AI_GPU_GBS = (20.0, 40.0), {"nvidia": (150.0, 250.0), "apple": (60.0, 90.0), "amd": (150.0, 250.0)}  # effective memory bandwidth, GB/s


def _ai_gb(mb):
    return "%.1f GB" % (mb / 1024.0)


def _ai_assess(m, hw):
    """{verdict, where, need_mb, gpu_layers, tok_s, why} of a model on a machine, by the rules of aihw.assess(): weights + KV cache + 300 MB;
    gpu = fits the free video memory with 10% to spare (Apple silicon: at most 65% of the RAM and no more than is free), partial = some layers
    on the card and the rest comfortably in RAM, ram = CPU only and at most half of the RAM, slow = fits up to 85% of the RAM, no = too big."""
    need = int(m["approx_mb"] + m["layers"] * 17 + 300)
    total, free = hw["ram"]["total_mb"], hw["ram"]["available_mb"]
    gpu = next((g for g in hw["gpus"] if g["backend"] in ("cuda", "rocm", "metal") and (g["unified"] or g["vram_mb"])), None)
    active = m["active_b"] or m["params_b"]
    gb = m["approx_mb"] / 1024.0 * active / m["params_b"]  # bytes read per token: the active parameters only (MoE)
    cpu_t = tuple(gb / x for x in reversed(_AI_CPU_GBS))   # seconds per token at the fast and at the slow end
    rate = lambda t: [max(1, int(round(1 / t[1]))), max(1, int(round(1 / t[0])))]  # noqa: E731  # [slow end, fast end] tokens/s

    def out(verdict, where, layers, tok, why):
        return {"verdict": verdict, "where": where, "need_mb": need, "gpu_layers": layers, "tok_s": tok, "why": why}
    if gpu and gpu["unified"] and need <= 0.65 * total and need <= free:
        bw = _AI_GPU_GBS.get(gpu["vendor"], _AI_CPU_GBS)
        return out("gpu", "GPU", m["layers"], rate(tuple(gb / x for x in reversed(bw))),
                   "needs %s, the %s has %s of unified memory free: all on the GPU" % (_ai_gb(need), gpu["name"], _ai_gb(free)))
    if gpu and not gpu["unified"]:
        vram = gpu["vram_free_mb"] if gpu["vram_free_mb"] is not None else int(gpu["vram_mb"] * 0.85)
        bw = _AI_GPU_GBS.get(gpu["vendor"], _AI_CPU_GBS)
        if need * 1.1 <= vram:
            return out("gpu", "GPU", m["layers"], rate(tuple(gb / x for x in reversed(bw))),
                       "needs %s, the %s has %s free: all on the GPU" % (_ai_gb(need), gpu["name"], _ai_gb(vram)))
        on_gpu = int(m["layers"] * max(0, vram - 300) / need)
        rest = need - need * on_gpu / m["layers"]
        if on_gpu >= max(2, m["layers"] // 8) and rest <= 0.5 * total and rest <= free - 1024:
            share = on_gpu / float(m["layers"])
            t = tuple(gb * share / g + gb * (1 - share) / c for g, c in zip(reversed(bw), reversed(_AI_CPU_GBS)))
            return out("partial", "GPU+CPU", on_gpu, rate(t), "needs %s, the %s has %s free: %d of %d layers on the GPU, the rest (%s) in RAM"
                       % (_ai_gb(need), gpu["name"], _ai_gb(vram), on_gpu, m["layers"], _ai_gb(rest)))
    if need <= 0.5 * total and need <= free - 1024:
        return out("ram", "CPU", 0, rate(cpu_t), "needs %s, the RAM has %s free of %s: fits comfortably (CPU only)" % (_ai_gb(need), _ai_gb(free), _ai_gb(total)))
    if need <= 0.85 * total:
        shared = bool(gpu and gpu["unified"])  # Apple silicon: the GPU works on the same memory, slow or not
        return out("slow", "GPU" if shared else "CPU", m["layers"] if shared else 0,
                   rate(tuple(gb / x for x in reversed(_AI_GPU_GBS.get(gpu["vendor"], _AI_CPU_GBS)))) if shared else rate(cpu_t),
                   "needs %s of %s of RAM (%s free now): it runs, but the PC will slow down (swapping, other programs squeezed)"
                   % (_ai_gb(need), _ai_gb(total), _ai_gb(free)))
    return out("no", "-", 0, None, "needs %s, more than the %s of RAM: it will not work" % (_ai_gb(need), _ai_gb(total)))


def ai_catalog(os_name=None):
    """The dict aisetup.catalog() returns (docs/AI.md), for a demo machine of this OS (linux, windows, darwin)."""
    os_name = os_name if os_name in AI_OSES else "linux"
    hw, st = _AI_MACHINES[os_name], _AI_STATE[os_name]
    sudo = "" if os_name == "windows" else "sudo "
    models = []
    for mid, name, lic, params, active, quant, layers, ctx, rank, mb, notes, pinned in _AI_MODELS:
        m = {"id": mid, "name": name, "license": lic, "params_b": params, "quant": quant, "layers": layers, "ctx_max": ctx, "rank": rank,
             "approx_mb": mb, "notes": notes}
        if active:
            m["active_b"] = active
        m.update(assess=_ai_assess(dict(m, active_b=active), hw), installed=mid in st["installed"], pinned=pinned,
                 commands={"install": "%snuc-console-ai setup %s" % (sudo, mid), "use": "%snuc-console-ai use %s" % (sudo, mid),
                           "remove": "%snuc-console-ai remove %s" % (sudo, mid)})
        models.append(m)
    best = [m for m in models if m["assess"]["verdict"] in ("gpu", "ram")] or [m for m in models if m["assess"]["verdict"] == "partial"] \
        or sorted((m for m in models if m["assess"]["verdict"] != "no"), key=lambda m: m["approx_mb"])[:1]
    return {"hw": hw, "dir": st["dir"], "runtime": dict(st["runtime"]), "recommended": min(best, key=lambda m: m["rank"])["id"] if best else None,
            "active": st["active"], "models": models}


def ai_status(os_name=None):
    """What the STATUS section reads: [ai] as that machine's config has it and the last probe of its model server."""
    st = _AI_STATE[os_name if os_name in AI_OSES else "linux"]
    return {"enabled": st["enabled"], "endpoint": st["endpoint"], "model": st["active"] or "", "probe": dict(st["probe"])}
