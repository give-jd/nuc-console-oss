"""MAP collector: net.json 'links' (who is connected to whom), listeners' systemd unit, tailnet IPs, boot 'deps'.

Pure functions with fixtures (ss -tanH, docker inspect, compose labels, /proc cgroup, systemctl show), plus collect_net and
collect_boot driven by a fake `run`, so everything runs on Linux, macOS and Windows alike.
Addresses are documentation ranges (192.0.2.0/24 this host, 198.51.100.0/24 and 203.0.113.0/24 outside, 192.168.0.x LAN,
100.64.0.0/10 tailnet, 2001:db8::/32 IPv6, 172.18.x docker); fake secrets are built at runtime.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import collect_windows as cwin  # noqa: E402
import collector  # noqa: E402

FAKE_PW = "not-a-" + "real-password"
FAKE_URL = "post" + "gres://user:" + FAKE_PW + "@db:5432/app"
GW = "172.18.0.1"
HOST = "192.0.2.10"


def ct(name, image, nets=None, pid=0, project="shop", service="", env=(), ports=None, expose=(), depends="", state="running",
       exit_code=0, mode="", health=None, restart="unless-stopped", restarts=0):
    """A `docker inspect` entry with only the fields the collector reads."""
    up = state == "running"
    labels = {k: v for k, v in (("com.docker.compose.project", project), ("com.docker.compose.service", service),
                                ("com.docker.compose.depends_on", depends)) if v}
    st = {"Status": state, "Running": up, "Pid": pid if up else 0, "ExitCode": exit_code}
    if health:
        st["Health"] = {"Status": health}
    networks = {n: {"Aliases": [service, "0123456789ab"] if service else [], "IPAddress": ip if up else "", "Gateway": gw}
                for n, (ip, gw) in (nets or {}).items()}
    return {"Name": "/" + name, "State": st, "RestartCount": restarts,
            "Config": {"Image": image, "Env": list(env), "ExposedPorts": {e: {} for e in expose}, "Labels": labels},
            "HostConfig": {"NetworkMode": mode or "default", "PortBindings": {}, "RestartPolicy": {"Name": restart}},
            "NetworkSettings": {"Ports": ports or {}, "Networks": networks}}


SHOP = {"shop_default": ("", GW)}
INSPECT = [
    ct("shop-web-1", "example/shop-web:1.4", {"shop_default": ("172.18.0.3", GW)}, pid=101, service="web", health="healthy",
       env=["API_URL=http://api:3000/v1", "THEME=dark"], depends="api:service_started:false", expose=("80/tcp",),
       ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}, {"HostIp": "::", "HostPort": "8080"}]}),
    ct("shop-api-1", "example/shop-api:2", {"shop_default": ("172.18.0.4", GW)}, pid=102, service="api",
       env=["DATABASE_URL=" + FAKE_URL, "REDIS_HOST=cache", "PASSWORD=" + FAKE_PW],
       depends="db:service_healthy:false,cache:service_started:true", expose=("3000/tcp",)),
    ct("shop-db-1", "postgres:16", {"shop_default": ("172.18.0.5", GW)}, pid=103, service="db", expose=("5432/tcp",)),
    ct("shop-worker-1", "example/shop-worker:2", SHOP, service="worker", state="exited", exit_code=137, restarts=4,
       expose=("9100/tcp",), env=["DB_HOST=db"]),
    ct("solo", "example/solo:1", {"bridge": ("172.17.0.2", "172.17.0.1")}, pid=104, project="", restart="",
       expose=("8000/tcp", "53/udp")),
    ct("hostnet", "example/agent:1", {"host": ("", "")}, pid=105, project="", mode="host", expose=("9999/tcp",)),
    ct("sidecar", "example/proxy:1", {}, pid=106, project="", mode="container:shop-api-1", expose=("15000/tcp",)),
]

NETNS = {  # pid -> `ss -tanH` inside that container
    101: """LISTEN 0      4096         0.0.0.0:80          0.0.0.0:*
LISTEN 0      4096            [::]:80             [::]:*
LISTEN 0      511        127.0.0.1:9000        0.0.0.0:*
ESTAB  0      0         172.18.0.3:80       172.18.0.1:51234
ESTAB  0      0         172.18.0.3:80       172.18.0.1:45000
ESTAB  0      0   [::ffff:172.18.0.3]:80  [::ffff:203.0.113.9]:40000
ESTAB  0      0         172.18.0.3:41000    172.18.0.4:3000
ESTAB  0      0         172.18.0.3:41001    172.18.0.4:3000
TIME-WAIT 0   0         172.18.0.3:41002    172.18.0.4:3000
ESTAB  0      0          127.0.0.1:9000      127.0.0.1:45500
ESTAB  0      0          127.0.0.1:45500     127.0.0.1:9000
""",
    102: """LISTEN 0      511                *:3000              *:*
ESTAB  0      0         172.18.0.4:3000     172.18.0.3:41000
ESTAB  0      0         172.18.0.4:3000     172.18.0.3:41001
ESTAB  0      0         172.18.0.4:42000    172.18.0.5:5432
ESTAB  0      0         172.18.0.4:42001  198.51.100.20:443
ESTAB  0      0         172.18.0.4:42002    172.18.0.1:6379
SYN-SENT 0    1         172.18.0.4:42003  198.51.100.21:443
""",
    103: """LISTEN 0      244          0.0.0.0:5432        0.0.0.0:*
LISTEN 0      244             [::]:5432           [::]:*
ESTAB  0      0         172.18.0.5:5432     172.18.0.4:42000
ESTAB  0      0         172.18.0.5:5432   192.168.0.55:5001
""",
    104: None,  # unreadable namespace
}

LISTENERS = [{"proto": "tcp", "addr": "0.0.0.0", "port": 22, "proc": "sshd"},
             {"proto": "tcp", "addr": "::", "port": 22, "proc": "sshd"},
             {"proto": "tcp", "addr": "0.0.0.0", "port": 8080, "proc": "docker-proxy"},
             {"proto": "tcp", "addr": "0.0.0.0", "port": 6379, "proc": "redis-server"},
             {"proto": "udp", "addr": "0.0.0.0", "port": 53, "proc": "dnsmasq"}]


def est(la, lp, pa, pp, proc):
    return {"l_addr": la, "l_port": lp, "p_addr": pa, "p_port": pp, "proc": proc}


HOST_CONNS = [
    est(HOST, 22, "203.0.113.50", 50000, "sshd"),                     # inbound from the Internet
    est("::ffff:" + HOST, 22, "::ffff:198.51.100.30", 50001, "sshd"),  # same, on a dual-stack socket
    est("127.0.0.1", 43000, "127.0.0.1", 8080, "curl"),               # a host client to a published port
    est("127.0.0.1", 8080, "127.0.0.1", 43000, "docker-proxy"),       # ...its server end: counted once, from the client
    est(GW, 51234, "172.18.0.3", 80, "docker-proxy"),                 # the proxy's hop into the container: not a client
    est(GW, 6379, "172.18.0.4", 42002, "redis-server"),               # the api's connection, seen from the host too
    est("127.0.0.1", 44000, "127.0.0.1", 6379, "nginx"),              # host process -> host process
    est("127.0.0.1", 6379, "127.0.0.1", 44000, "redis-server"),
    est(GW, 45000, "172.18.0.3", 80, "nginx"),                        # host process straight to a container IP
    est(HOST, 51000, "198.51.100.7", 6379, "redis-server"),           # a service talking out
    est(HOST, 52000, "203.0.113.80", 443, "firefox"),                 # a desktop app talking out: noise, not kept
]


def edges(out):
    return {(c["from"], c["to"], c["port"]): c["n"] for c in out["conns"]}


class Parsers(unittest.TestCase):
    def test_ss_all_listen_and_established_rows(self):
        got = collector.parse_ss_all(NETNS[101] + "garbage\n\nLISTEN 0 1 [2001:db8::3]:8443 [::]:*\n"
                                     "ESTAB 0 0 [2001:db8::3]:8443 [2001:db8::99%eth0]:5555\n"
                                     "tcp ESTAB 0 0 172.18.0.3:80 203.0.113.1:1 users:((\"nginx\",pid=7,fd=3))\n")
        self.assertEqual(sorted({(x["addr"], x["port"]) for x in got["listen"]}),
                         [("0.0.0.0", 80), ("127.0.0.1", 9000), ("2001:db8::3", 8443), ("::", 80)])
        e = {(x["l_addr"], x["l_port"], x["p_addr"], x["p_port"]) for x in got["estab"]}
        self.assertIn(("172.18.0.3", 80, "203.0.113.9", 40000), e)              # IPv4-mapped spelled as IPv4
        self.assertIn(("2001:db8::3", 8443, "2001:db8::99", 5555), e)           # brackets and scope dropped
        self.assertNotIn(41002, {x["l_port"] for x in got["estab"]})              # TIME-WAIT is not a connection
        self.assertEqual(got["estab"][-1]["proc"], "nginx")                       # Netid column tolerated
        self.assertEqual(collector.parse_ss_all(""), {"listen": [], "estab": []})
        old = collector.parse_ss_all("LISTEN 0 128 :::22 :::*\nESTAB 0 0 ::ffff:172.18.0.3:22 ::ffff:198.51.100.2:6000\n")
        self.assertEqual((old["listen"][0]["addr"], old["estab"][0]["p_addr"]), ("::", "198.51.100.2"))   # old ss: no brackets

    def test_container_conns_reuses_one_reading(self):
        calls = []
        fn = lambda pid: calls.append(pid) or collector.parse_ss_all(NETNS[103])  # noqa: E731
        self.assertEqual(len(collector.container_conns(103, fn)), 2)
        self.assertIsNone(collector.container_conns(1, lambda pid: None))
        self.assertEqual(calls, [103])

    def test_depends_on_label(self):
        self.assertEqual(collector.parse_depends_on("db:service_healthy:false,cache:service_started:true"), ["db", "cache"])
        self.assertEqual(collector.parse_depends_on("db"), ["db"])                 # older compose: the name alone
        self.assertEqual(collector.parse_depends_on(" db : x , db:service_started,,"), ["db"])
        self.assertEqual(collector.parse_depends_on(""), [])
        self.assertEqual(collector.parse_depends_on(None), [])

    def test_unit_of_cgroup_v1_and_v2(self):
        u = collector.unit_of_cgroup
        self.assertEqual(u("0::/system.slice/ssh.service\n"), "ssh.service")
        self.assertEqual(u("12:pids:/system.slice/ssh.service\n4:memory:/system.slice/ssh.service\n"
                           "1:name=systemd:/system.slice/ssh.service\n0::/system.slice/ssh.service\n"), "ssh.service")
        self.assertEqual(u("4:memory:/system.slice/docker.service\n1:name=systemd:/system.slice/nginx.service\n"), "nginx.service")
        self.assertEqual(u("0::/system.slice/system-getty.slice/getty@tty1.service"), "getty@tty1.service")
        self.assertEqual(u("0::/user.slice/user-1000.slice/user@1000.service/app.slice/syncthing.service"), "syncthing.service")
        self.assertIsNone(u("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox-1234.scope"))
        self.assertIsNone(u("0::/user.slice/user-1000.slice/session-3.scope"))     # a login session: no service
        self.assertIsNone(u("0::/system.slice/docker-0123456789abcdef.scope"))    # a container's process
        self.assertIsNone(u("0::/docker/0123456789abcdef"))                       # cgroupfs driver
        self.assertEqual(u("0::/system.slice/containerd.service/sub"), "containerd.service")   # delegated sub-cgroup
        self.assertIsNone(u(""))
        self.assertIsNone(u("garbage"))
        self.assertIsNone(collector.unit_of_pid("not-a-pid"))

    def test_parse_ss_keeps_the_pid(self):
        rows = collector.parse_ss('tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=812,fd=3),("sshd",pid=900,fd=3))\n'
                                  "tcp LISTEN 0 4096 [::]:80 [::]:*\n")
        self.assertEqual([(r["proc"], r["pid"]) for r in rows], [("sshd", 812), ("", None)])

    def test_reverse_deps(self):
        text = ("RequiredBy=dbus.service\nRequisiteOf=app.service\nWantedBy=multi-user.target systemd-logind.service\n"
                "PartOf=x.target\nBoundBy=vpn.service\nUpheldBy=y.service\nNoise=x.service\n")
        # hard dependencies only: a failed service multi-user.target merely wants does not break the target
        self.assertEqual(collector.parse_reverse_deps(text), ["app.service", "dbus.service", "vpn.service"])
        self.assertEqual(collector.parse_reverse_deps("RequiredBy=\nWantedBy=multi-user.target\n"), [])
        self.assertEqual(collector.parse_reverse_deps(""), [])

    def test_tailnet_ips(self):
        d = collector.parse_ts_peers(json.dumps({"Self": {"HostName": "nuc", "Online": True, "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"]},
                                                 "Peer": {"a": {"HostName": "pc", "Online": True, "TailscaleIPs": ["100.64.0.7"]},
                                                          "b": {"HostName": "old", "Online": False}}}))
        self.assertEqual(d["self"]["ips"], ["100.64.0.1", "fd7a:115c:a1e0::1"])
        self.assertEqual({p["name"]: p["ips"] for p in d["peers"]}, {"pc": ["100.64.0.7"], "old": []})


class LinkItems(unittest.TestCase):
    def setUp(self):
        collector.LINKS_SEEN.clear()
        self.entered = []

    def netns(self, pid):
        self.entered.append(pid)
        text = NETNS.get(pid)
        return None if text is None else collector.parse_ss_all(text)

    def build(self, now=1000.0, inspected=INSPECT, host=HOST_CONNS, netns=True, listeners=LISTENERS):
        return collector.link_items(inspected, self.netns if netns else None, host, listeners, {HOST, GW, "172.17.0.1"}, now=now)

    def test_edges_from_namespaces_and_host_sockets(self):
        out = self.build()
        self.assertEqual(edges(out), {
            ("ext:203.0.113.9", "ct:shop-web-1", 80): 1,       # outside client seen inside: DNAT does not hide it
            ("ct:shop-web-1", "ct:shop-api-1", 3000): 2,       # two connections, each seen from both namespaces: counted once
            ("ct:shop-api-1", "ct:shop-db-1", 5432): 1,
            ("ct:shop-api-1", "ext:198.51.100.20", 443): 1,    # outbound; SYN-SENT not counted
            ("ct:shop-api-1", "proc:redis-server", 6379): 1,   # to a host service via the gateway, also seen by the host: once
            ("ext:192.168.0.55", "ct:shop-db-1", 5432): 1,
            ("ext:203.0.113.50", "proc:sshd", 22): 1,
            ("ext:198.51.100.30", "proc:sshd", 22): 1,         # IPv4-mapped peer spelled as IPv4
            ("proc:curl", "ct:shop-web-1", 8080): 1,           # published port -> the container, not docker-proxy
            ("proc:nginx", "proc:redis-server", 6379): 1,
            ("proc:nginx", "ct:shop-web-1", 80): 1,
            ("proc:redis-server", "ext:198.51.100.7", 6379): 1,
        })
        ids = {e for k in edges(out) for e in k[:2]}
        self.assertFalse([i for i in ids if "172.18.0.1" in i or "docker-proxy" in i or "firefox" in i or "127.0.0.1" in i])
        self.assertEqual(out["conn_source"], "netns")
        self.assertEqual(out["errors"], ["1 of 4 container namespaces unreadable"])
        self.assertEqual(out["since"], collector.SINCE)
        self.assertEqual(sorted(self.entered), [101, 102, 103, 104])   # never host-network, shared-namespace or stopped ones
        self.assertTrue(all(isinstance(c["last"], float) and isinstance(c["port"], int) for c in out["conns"]))

    def test_containers(self):
        by = {c["name"]: c for c in self.build()["containers"]}
        self.assertEqual(set(by), {"shop-web-1", "shop-api-1", "shop-db-1", "shop-worker-1", "solo", "hostnet", "sidecar"})
        web = by["shop-web-1"]
        self.assertEqual(web, {"name": "shop-web-1", "image": "example/shop-web:1.4", "project": "shop", "service": "web",
                               "state": "running", "health": "healthy", "exit": None, "restarts": 0, "restart": "unless-stopped",
                               "host_net": False, "nets": {"shop_default": "172.18.0.3"}, "ports": [{"p": 8080, "c": "80/tcp", "s": "*"}],
                               "listen": [80], "listen_src": "netns", "depends_on": ["shop-api-1"], "env_refs": ["shop-api-1"], "db": None})
        api = by["shop-api-1"]
        self.assertEqual(api["depends_on"], ["shop-db-1", "cache"])    # resolved within the project; unknown service kept as is
        self.assertEqual(api["env_refs"], ["shop-db-1"])               # host 'db' in DATABASE_URL; REDIS_HOST=cache: no such container
        self.assertEqual(api["listen"], [3000])
        self.assertEqual(by["shop-db-1"]["db"], "postgres")
        w = by["shop-worker-1"]                                        # exited with an error: no namespace, details from inspect
        self.assertEqual((w["state"], w["exit"], w["restarts"], w["listen"], w["listen_src"], w["nets"]),
                         ("exited", 137, 4, [9100], "image", {"shop_default": ""}))
        self.assertEqual(w["env_refs"], ["shop-db-1"])                 # DB_HOST=db: same network, by compose service name
        solo = by["solo"]
        self.assertEqual((solo["listen"], solo["listen_src"], solo["nets"], solo["restart"], solo["project"]),
                         ([8000], "image", {"bridge": "172.17.0.2"}, "", ""))   # namespace unreadable: TCP ports of the image
        self.assertEqual((by["hostnet"]["host_net"], by["hostnet"]["ports"], by["hostnet"]["nets"]), (True, [], {}))
        self.assertEqual(by["sidecar"]["listen_src"], "image")

    def test_values_never_stored(self):
        text = json.dumps(self.build())
        self.assertNotIn(FAKE_PW, text)
        self.assertNotIn("dark", text)
        self.assertNotIn("/v1", text)

    def test_unreadable_namespaces_mean_host_side_only(self):
        out = collector.link_items(INSPECT, lambda pid: None, HOST_CONNS, LISTENERS, {HOST, GW}, now=1000.0)
        self.assertEqual(out["conn_source"], "host")
        self.assertIn("nsenter needs root", out["errors"][0])
        self.assertTrue(all(c["listen_src"] == "image" for c in out["containers"]))
        self.assertEqual(edges(out)[("ct:shop-api-1", "proc:redis-server", 6379)], 1)   # the host's side still shows it
        self.assertNotIn(("ct:shop-web-1", "ct:shop-api-1", 3000), edges(out))

    def test_host_only_mode_docker_desktop(self):
        """macOS/Windows: no namespaces; the Docker Desktop backend listens for the published ports."""
        insp = [ct("app", "example/app:1", {"app_default": ("172.18.0.2", GW)}, pid=1, project="app", service="app",
                   expose=("80/tcp",), ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}),
                ct("cache", "redis:7", {"app_default": ("172.18.0.3", GW)}, pid=2, project="app", service="cache",
                   ports={"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6379"}]})]
        listeners = [{"proto": "tcp", "addr": "*", "port": 8080, "proc": "com.docker.backend"},
                     {"proto": "tcp", "addr": "127.0.0.1", "port": 6379, "proc": "com.docker.backend"},
                     {"proto": "tcp", "addr": "*", "port": 5000, "proc": "ControlCenter"}]
        host = [est("192.0.2.5", 8080, "192.168.0.20", 50000, "com.docker.backend"),   # a LAN client of the app
                est("127.0.0.1", 50001, "127.0.0.1", 6379, "Code Helper"),             # local client of the cache
                est("127.0.0.1", 6379, "127.0.0.1", 50001, "com.docker.backend"),
                est("192.0.2.5", 50002, "203.0.113.4", 443, "com.docker.backend"),    # the backend's own traffic: not a client
                est("192.0.2.5", 50003, "203.0.113.5", 443, "Safari"),                # desktop noise
                est("192.0.2.5", 5000, "192.168.0.21", 50004, "ControlCenter")]       # AirPlay receiver: inbound to a process
        out = collector.link_items(insp, None, host, listeners, {"192.0.2.5"}, now=50.0)
        self.assertEqual(out["conn_source"], "host")
        self.assertEqual(out["errors"], [])
        self.assertEqual(edges(out), {("ext:192.168.0.20", "ct:app", 8080): 1, ("proc:Code Helper", "ct:cache", 6379): 1,
                                      ("ext:192.168.0.21", "proc:ControlCenter", 5000): 1})
        self.assertEqual([(c["name"], c["listen"], c["listen_src"]) for c in out["containers"]],
                         [("app", [80], "image"), ("cache", [], "image")])

    def test_without_docker_or_listeners(self):
        out = collector.link_items(None, None, HOST_CONNS, None, {HOST}, now=10.0)
        self.assertEqual(out["containers"], [])
        self.assertIn("listening ports unknown", out["errors"][0])
        self.assertEqual(edges(out), {})                                 # no listener known: nothing to classify, no guess
        out = collector.link_items(None, None, None, LISTENERS, {HOST}, now=11.0)
        self.assertEqual((out["containers"], out["conns"], out["errors"]), ([], [], []))

    def test_memory_24h_and_containers_gone(self):
        self.build(now=1000.0)
        later = self.build(now=2000.0, host=[], netns=False)              # nothing seen now: remembered, n = 0
        self.assertEqual(edges(later)[("ct:shop-web-1", "ct:shop-api-1", 3000)], 0)
        self.assertEqual({c["last"] for c in later["conns"]}, {1000.0})
        kept = collector.link_items(None, None, None, LISTENERS, set(), now=3000.0)   # Docker unreadable: container edges kept
        self.assertIn(("ct:shop-api-1", "ct:shop-db-1", 5432), edges(kept))
        done = collector.link_items([x for x in INSPECT if x["Name"] != "/shop-web-1"], None, [], LISTENERS, {HOST}, now=3500.0,
                                    names={"shop-web-1"})                 # Exited (0): not inspected, still listed by docker ps -a
        self.assertNotIn("shop-web-1", [c["name"] for c in done["containers"]])
        self.assertIn(("ct:shop-web-1", "ct:shop-api-1", 3000), edges(done))   # a finished job is remembered, not removed
        gone = self.build(now=4000.0, inspected=[x for x in INSPECT if x["Name"] != "/shop-db-1"], host=[], netns=False)
        self.assertFalse([k for k in edges(gone) if "ct:shop-db-1" in k])  # container removed: its edges forgotten
        self.assertIn(("ct:shop-web-1", "ct:shop-api-1", 3000), edges(gone))
        expired = self.build(now=1000.0 + collector.LINKS_TTL_S + 1, host=[], netns=False)
        self.assertEqual(expired["conns"], [])

    def test_caps_on_outside_peers_and_edges(self):
        seen = {}
        now_edges = {("ext:198.51.100.%d" % i, "ct:web", 80): {i} for i in range(1, 31)}
        for i in range(1, 31):
            seen[("ext:198.51.100.%d" % i, "ct:web", 80)] = 100.0 + i      # older peers first
        out = collector.remember_links({}, None, 200.0, seen=seen)
        self.assertEqual(len(out), collector.LINKS_MAX_EXT)
        self.assertEqual(min(c["last"] for c in out), 111.0)                # the 10 oldest dropped
        out = collector.remember_links(now_edges, None, 300.0, seen={})
        self.assertEqual(len(out), collector.LINKS_MAX_EXT)                # also within one pass
        big = {}
        for i in range(500):
            big[("ct:a%d" % i, "ct:b", 80)] = {i}
        for i in range(10):
            big[("ct:a%d" % i, "ext:203.0.113.%d" % i, 443)] = {i}
        out = collector.remember_links(big, None, 400.0, seen={})
        self.assertEqual(len(out), collector.LINKS_MAX)
        self.assertFalse([c for c in out if c["to"].startswith("ext:")])   # same age: outside peers go first


class CollectNet(unittest.TestCase):
    """collect_net with a fake `run`: one docker inspect, one host socket list and one nsenter per container per pass."""

    def setUp(self):
        self.saved = {k: getattr(collector, k) for k in ("LINUX", "MACOS", "WINDOWS", "OS_NAME", "run", "OFF", "unit_of_pid",
                                                         "UNSUPPORTED_BOOT", "boot_time")}
        self.saved_which, self.saved_cwin = collector.shutil.which, getattr(collector, "cwin", None)
        collector.LINKS_SEEN.clear()
        collector.EXTERNAL_SEEN.clear()
        self.calls = []
        # a finished one-shot job; 'other/alias' is a legacy link's alias, not a container
        self.job = ("idjob", "Exited (0) 3 hours ago", "nightly-backup-1,shop-api-1/backup")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(collector, k, v)
        collector.shutil.which = self.saved_which
        if self.saved_cwin is None and hasattr(collector, "cwin"):
            del collector.cwin
        elif self.saved_cwin is not None:
            collector.cwin = self.saved_cwin

    def fake_run(self, name, *args, timeout=15):
        self.calls.append((name,) + args)
        absent = {"tailscale", "ufw", "iptables", "fail2ban-client", "journalctl"}
        if name in absent:
            raise collector.Absent(name)
        if (name, args[:2]) == ("docker", ("ps", "-a")):
            rows = [("id%d" % i, "Up 2 hours", x["Name"][1:]) for i, x in enumerate(INSPECT)] + ([self.job] if self.job else [])
            return 0, "".join("\t".join(r) + "\n" for r in rows), ""
        if (name, args[:1]) == ("docker", ("inspect",)):
            self.assertNotIn("idjob", args)                                       # finished one-shot jobs are not inspected
            return 0, json.dumps(INSPECT), ""
        if name == "nsenter":
            text = NETNS.get(int(args[1]))
            return (0, text, "") if text is not None else (1, "", "nsenter: Permission denied")
        if name == "ss" and args[0] == "-tnpH":
            return 0, "".join(f"0 0 {c['l_addr']}:{c['l_port']} {c['p_addr']}:{c['p_port']} users:((\"{c['proc']}\",pid=9,fd=3))\n"
                              for c in HOST_CONNS if ":" not in c["l_addr"]), ""
        if name == "ss" and args[0] == "-tulnpH":
            return 0, ('tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=812,fd=3))\n'
                       'tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=3000,fd=4))\n'
                       'tcp LISTEN 0 128 0.0.0.0:6379 0.0.0.0:* users:(("redis-server",pid=3100,fd=6))\n'), ""
        if name == "hostname":
            return 0, f"{HOST} {GW} 172.17.0.1\n", ""
        return 0, "", ""

    def as_linux(self):
        collector.LINUX, collector.MACOS, collector.WINDOWS, collector.OS_NAME = True, False, False, "linux"
        collector.UNSUPPORTED_BOOT = ()
        collector.OFF = set()
        collector.run = self.fake_run
        collector.shutil.which = lambda name, path=None: "/usr/bin/" + name
        collector.unit_of_pid = lambda pid: {812: "ssh.service", 3000: "docker.service"}.get(pid)

    def test_linux_pass_shares_commands_between_databases_and_map(self):
        self.as_linux()
        d = collector.collect_net()
        self.assertEqual(d["errors"], {})
        count = lambda pred: sum(1 for c in self.calls if pred(c))  # noqa: E731
        self.assertEqual(count(lambda c: c[:2] == ("docker", "inspect")), 1)
        self.assertEqual(count(lambda c: c[:2] == ("ss", "-tnpH")), 1)
        self.assertEqual(sorted(int(c[2]) for c in self.calls if c[0] == "nsenter"), [101, 102, 103, 104])   # db 103: once
        self.assertTrue(all(c[3:] == ("-n", "ss", "-tanH") for c in self.calls if c[0] == "nsenter"))
        db = d["dbs"]["items"]
        self.assertEqual([(i["name"], i["active"], i["ext_source"]) for i in db], [("shop-db-1", ["shop-api-1"], "netns")])  # running only
        links = d["links"]
        self.assertEqual(links["conn_source"], "netns")
        self.assertIn(("ct:shop-web-1", "ct:shop-api-1", 3000), edges(links))
        self.assertIn(("ext:203.0.113.50", "proc:sshd", 22), edges(links))
        self.assertIn("shop-worker-1", [c["name"] for c in links["containers"]])
        ls = {x["port"]: x for x in d["listeners"]}
        self.assertEqual((ls[22].get("unit"), ls[8080].get("unit"), ls[6379].get("unit")), ("ssh.service", "docker.service", None))
        self.assertFalse([x for x in d["listeners"] if "pid" in x])                # the pid never reaches net.json

    def test_docker_installed_but_not_running_is_a_note_not_an_error(self):
        self.as_linux()
        real = collector.run
        for err in ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
                    "failed to connect to the docker API at npipe:////./pipe/docker_engine; check if the path is correct"):
            collector.LINKS_SEEN.clear()
            collector.run = lambda name, *a, **k: (1, "", err) if name == "docker" else real(name, *a, **k)
            d = collector.collect_net()
            self.assertNotIn("dbs", d["errors"], err)
            self.assertIn("dbs", d["absent"])
            self.assertEqual(d["notes"]["dbs"], "Docker is installed but its engine is not running")
        collector.run = lambda name, *a, **k: (None, "", "TimeoutExpired(['docker', 'ps'], 15)") if name == "docker" else real(name, *a, **k)
        d = collector.collect_net()
        self.assertNotIn("dbs", d["errors"])                                         # an engine still starting: a note too
        self.assertEqual(d["notes"]["dbs"], "Docker did not answer in time (its engine may be starting)")
        collector.run = lambda name, *a, **k: (1, "", "permission denied") if name == "docker" else real(name, *a, **k)
        self.assertIn("dbs", collector.collect_net()["errors"])                     # any other failure is still an error

    def test_a_nightly_job_is_remembered_until_it_is_removed(self):
        """Exited (0): off the container lists, but its connections stay on the map for 24 h, until `docker rm`."""
        self.as_linux()
        job = ("ct:nightly-backup-1", "ct:shop-db-1", 5432)
        collector.LINKS_SEEN[job] = time.time() - 3600                           # seen while it ran, an hour ago
        d = collector.collect_net()
        self.assertIn(job, edges(d["links"]))
        self.assertNotIn("nightly-backup-1", [c["name"] for c in d["links"]["containers"]])
        self.assertEqual([i["name"] for i in d["dbs"]["items"]], ["shop-db-1"])    # the databases: running containers only
        self.job = None                                                          # removed: no longer listed
        self.assertNotIn(job, edges(collector.collect_net()["links"]))

    def test_map_off_runs_nothing_for_it(self):
        self.as_linux()
        collector.OFF = {"map", "databases"}
        d = collector.collect_net()
        self.assertIn("links", d["disabled"])
        self.assertFalse([c for c in self.calls if c[0] in ("docker", "nsenter", "hostname") or c[:2] == ("ss", "-tnpH")])

    def test_docker_missing_still_maps_the_host(self):
        self.as_linux()
        run = self.fake_run
        collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name)) if name == "docker" else run(name, *a, **k)
        d = collector.collect_net()
        self.assertIn("dbs", d["absent"])
        self.assertNotIn("links", d["absent"])
        self.assertEqual(d["links"]["containers"], [])
        self.assertIn(("ext:203.0.113.50", "proc:sshd", 22), edges(d["links"]))
        self.assertFalse([c for c in self.calls if c[0] == "nsenter"])

    def test_nothing_installed_is_absent_not_an_error(self):
        self.as_linux()
        collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name))
        d = collector.collect_net()
        self.assertIn("links", d["absent"])
        self.assertEqual(d["errors"], {})

    def test_nsenter_missing_is_a_note(self):
        self.as_linux()
        collector.shutil.which = lambda name, path=None: None if name == "nsenter" else "/usr/bin/" + name
        d = collector.collect_net()
        self.assertEqual(d["links"]["conn_source"], "host")
        self.assertIn("nsenter not installed", d["links"]["errors"][0])

    def test_windows_host_only(self):
        collector.LINUX, collector.MACOS, collector.WINDOWS, collector.OS_NAME = False, False, True, "windows"
        collector.OFF = {"firewall"}
        collector.run = self.fake_run
        insp = [ct("app", "example/app:1", {"app_default": ("172.18.0.2", GW)}, pid=1, project="app", service="app",
                   ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]})]
        run = self.fake_run
        collector.run = lambda name, *a, **k: (0, json.dumps(insp), "") if a[:1] == ("inspect",) else run(name, *a, **k)

        class FakeWin:
            listeners = staticmethod(lambda fw: [{"proto": "tcp", "addr": "0.0.0.0", "port": 8080, "proc": "com.docker.backend", "fw": None}])
            established = staticmethod(lambda: [est("192.0.2.5", 8080, "192.168.0.20", 50000, "com.docker.backend")])
            local_addrs = staticmethod(lambda: {"192.0.2.5"})
        collector.cwin = FakeWin
        d = collector.collect_net()
        self.assertEqual(d["links"]["conn_source"], "host")
        self.assertEqual(edges(d["links"]), {("ext:192.168.0.20", "ct:app", 8080): 1})
        self.assertFalse([c for c in self.calls if c[0] == "nsenter"])

    def test_linux_boot_deps_per_unit(self):
        self.as_linux()
        collector.boot_time = lambda: 1700000000

        def run(name, *args, timeout=15):
            self.calls.append((name,) + args)
            if name == "docker":
                raise collector.Absent(name)
            if args[:1] == ("--failed",):
                return 0, "● bad.service loaded failed failed Bad\n-.mount loaded failed failed Root\nbroken.service loaded failed failed X\n", ""
            if args[:1] == ("show",):
                unit = args[-1]
                self.assertEqual(args[-2], "--")                                          # a unit is never read as an option
                self.assertEqual([a for a in args if a.endswith(("By", "Of"))], ["RequiredBy", "RequisiteOf", "BoundBy"])
                if unit == "broken.service":
                    return 1, "", "Failed to get properties"
                return 0, {"bad.service": "RequiredBy=app.service\nWantedBy=multi-user.target\nUpheldBy=\n",
                           "-.mount": "RequiredBy=local-fs.target\n"}[unit], ""
            return 0, "", ""
        collector.run = run
        b = collector.collect_boot()
        self.assertEqual(b["deps"], {"bad.service": ["app.service"], "-.mount": ["local-fs.target"]})   # not WantedBy
        self.assertNotIn("deps", b["errors"])
        collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name))
        b = collector.collect_boot()
        self.assertIn("deps", b["absent"])                                                # no failed list: deps unknown, not an error
        self.assertEqual(b["errors"], {})


class WindowsAndMacBoot(unittest.TestCase):
    def test_dependent_services(self):
        self.assertIn("DependentServices", cwin.PS_BOOT)
        data = {"boot": 1700000000, "perf": None, "events": [],
                "services": [{"n": "Spooler", "s": "Stopped", "e": 1067}, {"n": "Dns", "s": "Stopped", "e": 5},
                             {"n": "Gone", "s": "Stopped", "e": 2}, {"n": "Run", "s": "Running", "e": 0}],
                "deps": {"Spooler": ["Fax", "PrintNotify"], "Dns": "Dhcp", "Run": ["X"], "Big": ["S%d" % i for i in range(30)]}}
        b = cwin.boot_sections(data)
        self.assertEqual(b["deps"], {"Spooler": ["Fax", "PrintNotify"], "Dns": ["Dhcp"]})   # 'Gone' unreadable: no key (unknown)
        data["deps"]["Gone"] = ["S%d" % i for i in range(30)]
        self.assertEqual(len(cwin.boot_sections(data)["deps"]["Gone"]), 20)
        self.assertEqual(cwin.boot_sections(dict(data, deps=None))["deps"], {})

    def test_macos_has_no_deps(self):
        saved = {k: getattr(collector, k) for k in ("LINUX", "MACOS", "WINDOWS", "OS_NAME", "UNSUPPORTED_BOOT", "run", "boot_time")}
        saved_cmac = getattr(collector, "cmac", None)

        class FakeMac:
            daemons = staticmethod(lambda run: ([], ["com.example.bad"]))
        try:
            collector.LINUX, collector.MACOS, collector.WINDOWS, collector.OS_NAME = False, True, False, "darwin"
            collector.UNSUPPORTED_BOOT = ("analyze", "blame", "journal", "deps")   # what the module sets on macOS
            collector.boot_time = lambda: 1700000000
            collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name))
            collector.cmac = FakeMac
            b = collector.collect_boot()
        finally:
            for k, v in saved.items():
                setattr(collector, k, v)
            if saved_cmac is None:
                del collector.cmac
            else:
                collector.cmac = saved_cmac
        self.assertEqual((b["failed"], b["errors"]), (["com.example.bad"], {}))
        self.assertNotIn("deps", b)                                                       # unsupported: absent, never "none"
        self.assertIn("deps", b["unsupported"])


if __name__ == "__main__":
    unittest.main()
