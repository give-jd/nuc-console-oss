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


def _ufw():
    import collector
    d = collector.parse_ufw(UFW_TEXT)
    d["raw"] = "Status: active"
    return d


def snapshot(now=None):
    """-> (containers, net, boot, baseline) in the same shape the collector writes."""
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
    lst = lambda addr, port, proc: {"proto": "tcp", "addr": addr, "port": port, "proc": proc}
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
           "ts_peers": {"self": {"name": "demo-host", "online": True, "exit_option": False}, "peers": [
               {"name": "laptop", "os": "linux", "online": True, "last_seen": None, "direct": True, "relay": "", "exit": False, "exit_option": False},
               {"name": "phone", "os": "android", "online": False, "last_seen": now - 7200, "direct": False, "relay": "fra", "exit": False, "exit_option": False}]}}
    boot = {"ts": now, "errors": {}, "absent": [], "kernel": "6.8.0-demo", "btime": int(now) - 5 * 86400,
            "analyze": {"parts": {"firmware": 5.1, "loader": 2.0, "kernel": 1.2, "initrd": 1.1, "userspace": 12.4}, "total": 21.8},
            "blame": [{"unit": u, "s": s} for u, s in (("docker.service", 6.2), ("snapd.service", 4.8), ("cloud-init.service", 3.9))],
            "failed": [], "enabled": [{"unit": f"svc{i}.service", "state": "active"} for i in range(24)],
            "journal": {"err": 2, "warn": 9, "capped": False, "top": [{"id": "kernel", "n": 4, "pr": 4, "last": "example warning"}]},
            "containers": [{"name": "shop-web-1", "started": int(now) - 5 * 86400 + 40, "restart": "unless-stopped"},
                           {"name": "cache-1", "started": int(now) - 3600, "restart": "no"}],
            "docker_df": {"rows": [{"type": "Images", "count": "14", "active": "6", "size": "5.2GB", "reclaimable": "2.1GB (40%)"},
                                   {"type": "Containers", "count": "7", "active": "6", "size": "180MB", "reclaimable": "0B (0%)"},
                                   {"type": "Local Volumes", "count": "5", "active": "5", "size": "1.4GB", "reclaimable": "0B (0%)"}],
                          "volumes_unused": 12, "volumes_unused_anonymous": 9, "dangling_images": {"count": 0, "bytes": 0}}}
    import render
    base = {"ts": now, "ports": render.exposure_keys(net, cont)}  # baseline = current exposure: no "new port" alarm
    return cont, net, boot, base


def sampler_data(real):
    """Replace machine-specific parts of a real Sampler.sample() result (users, mounts, interface names)."""
    real = dict(real)
    real["sessions"] = {"local": [{"user": "alice", "tty": "tty1"}], "ssh": [LAN + ".20"]}
    real["fs"] = [{"mount": "/", "used": 180 * 2 ** 30, "total": 480 * 2 ** 30}, {"mount": "/data", "used": 1200 * 2 ** 30, "total": 2000 * 2 ** 30}]
    real["net"] = {"eth0": dict(next(iter(real["net"].values()), {"rx": 0, "tx": 0, "rx_tot": 0, "tx_tot": 0, "hist_rx": [0], "hist_tx": [0]}))}
    return real
