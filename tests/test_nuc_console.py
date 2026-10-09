import collections
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the test helpers (cardlines.py)
import collector  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import problems  # noqa: E402
import hostdata  # noqa: E402
import cardlines  # noqa: E402
import ui  # noqa: E402
import exposure  # noqa: E402
import cards  # noqa: E402
import ansi  # noqa: E402

import re
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
render.MODE = "rotate"  # page tests assume the 3-page rotation; overview tests pass mode= explicitly
render.CFG["spacing"] = 0  # legacy layout tests count lines: spacing has its own tests
render.CFG["details"] = False  # legacy tests expect one overview slide; detail pages have their own tests

CONT = {"ts": time.time(), "containers": [
    {"name": "web-1", "status": "Up 3 days", "state": "running", "project": "shop",
     "ports": [{"p": 8080, "s": "*"}, {"p": 5432, "s": "lo"}], "mem": 300 * 2**20},
    {"name": "db-1", "status": "Up 3 days (unhealthy)", "state": "running", "project": "shop",
     "ports": [], "mem": None},
    {"name": "solo", "status": "Up 1 hour", "state": "running", "project": "", "ports": [], "mem": 2**30},
]}


# fake values built at runtime (no literal URL with credentials in the source)
FAKE_PW = "not-a-" + "real-password"
FAKE_URL = "post" + "gres://user:" + FAKE_PW + "@db:5432/app"
UFW = collector.parse_ufw("""Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    192.168.0.0/24
9011/tcp                   ALLOW IN    Anywhere
8000:8010/tcp              ALLOW IN    Anywhere
Anywhere on tailscale0     ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
""")
NET = {"ts": time.time(), "errors": {}, "ufw": UFW, "docker_user": [],
       "f2b": {"jails": [{"name": "sshd", "banned": 2, "ips": ["203.0.113.7"]}]},
       "drops": {"n": 5, "src": [["198.51.100.9", 5]], "dpt": [["23", 5]]},
       "serve": [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True}],
       "listeners": [
           {"proto": "tcp", "addr": "0.0.0.0", "port": 22, "proc": "sshd"},
           {"proto": "tcp", "addr": "::", "port": 22, "proc": "sshd"},
           {"proto": "tcp", "addr": "0.0.0.0", "port": 5432, "proc": "docker-proxy"},
           {"proto": "tcp", "addr": "0.0.0.0", "port": 9011, "proc": "java"},
           {"proto": "tcp", "addr": "0.0.0.0", "port": 7777, "proc": "foo"},
           {"proto": "tcp", "addr": "127.0.0.1", "port": 8443, "proc": "docker-proxy"},
           {"proto": "tcp", "addr": "127.0.0.1", "port": 55432, "proc": "docker-proxy"},
           {"proto": "tcp", "addr": "100.64.0.1", "port": 8443, "proc": "tailscaled"},
           {"proto": "tcp", "addr": "100.64.0.1", "port": 8444, "proc": "tailscaled"}]}
ROWS = {(r["port"], r["proto"], r["loc"], r["ts"]): r for r in exposure.exposure_rows(NET, CONT)}


BOOT = {"ts": time.time(), "errors": {}, "kernel": "7.0", "btime": int(time.time()) - 86400,
        "analyze": {"parts": {"firmware": 6.7, "loader": 6.5, "kernel": 0.8, "initrd": 1.3, "userspace": 16.0}, "total": 31.3},
        "blame": [{"unit": f"svc{i}.service", "s": 20.0 - i} for i in range(12)], "failed": [],
        "enabled": [{"unit": f"s{i}.service", "state": "active" if i % 3 else "inactive"} for i in range(60)],
        "journal": {"err": 3, "warn": 5, "capped": False,
                    "top": [{"id": f"id{i}", "n": 9 - i, "pr": 3, "last": "msg\x1b[2J"} for i in range(8)]},
        "containers": [{"name": "a", "started": int(time.time()) - 86390, "restart": "unless-stopped"},
                       {"name": "b", "started": int(time.time()) - 100, "restart": "no"}]}


class Collector(unittest.TestCase):
    def test_boot_parsers(self):
        an = collector.parse_analyze("Startup finished in 6.710s (firmware) + 769ms (kernel) + 1min 2.5s (userspace) = 1min 10s\n")
        self.assertEqual(an["parts"], {"firmware": 6.71, "kernel": 0.769, "userspace": 62.5})
        self.assertEqual(an["total"], 70.0)
        bl = collector.parse_blame("1min 2.5s slow.service\n1.6s dev-x.device\n300ms fast.service\n")
        self.assertEqual([x["unit"] for x in bl], ["slow.service", "fast.service"])   # .device excluded
        self.assertEqual(bl[0]["s"], 62.5)
        self.assertEqual(collector.parse_failed("● bad.service loaded failed failed Bad\nother.service loaded failed failed X\n"),
                         ["bad.service", "other.service"])
        self.assertEqual(collector.parse_failed(""), [])
        j = collector.parse_journal('{"PRIORITY":"3","SYSLOG_IDENTIFIER":"k","MESSAGE":"boom"}\n'
                                    '{"PRIORITY":"4","SYSLOG_IDENTIFIER":"k","MESSAGE":[1,2]}\nnot json\n')
        self.assertEqual((j["err"], j["warn"], j["top"][0]["n"], j["top"][0]["pr"]), (1, 1, 2, 3))
        self.assertEqual(j["top"][0]["last"], "<binary message>")               # non-UTF-8 MESSAGE: list of bytes
        self.assertEqual(collector.parse_iso("2026-09-29T10:00:00.5Z"), 1790676000.5)
    def test_parse_ss_and_ufw_and_serve(self):
        ss = collector.parse_ss('tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
                                'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n'
                                'tcp LISTEN 0 511 *:8088 *:* users:(("node",pid=2,fd=18))\n'
                                'tcp LISTEN 0 4096 [fd7a:115c:a1e0::d73b:332c]:443 [::]:*')
        self.assertEqual([(x["addr"], x["port"], x["proc"]) for x in ss],
                         [("0.0.0.0", 22, "sshd"), ("127.0.0.53", 53, ""), ("*", 8088, "node"),
                          ("fd7a:115c:a1e0::d73b:332c", 443, "")])
        self.assertEqual(len(UFW["rules"]), 5)
        self.assertEqual(UFW["rules"][3]["to"], "Anywhere on tailscale0")
        sv = collector.parse_serve('{"Web": {"host.example.ts.net:8444": {"Handlers": {"/webhook": {"Proxy": "http://127.0.0.1:5678"}}}},'
                                   ' "AllowFunnel": {"host.example.ts.net:8444": true}}')
        self.assertEqual(sv, [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678", "funnel": True}])
        self.assertEqual(collector.parse_serve("{}"), [])

    def test_docker_user_ignores_only_bare_return(self):
        self.assertEqual(collector.parse_docker_user("-N DOCKER-USER\n-A DOCKER-USER -j RETURN"), [])
        self.assertEqual(len(collector.parse_docker_user("-A DOCKER-USER -s 203.0.113.7 -j DROP\n-A DOCKER-USER -j RETURN")), 1)
        self.assertEqual(len(collector.parse_docker_user("-A DOCKER-USER -s 10.0.0.0/8 -j RETURN")), 1)  # RETURN with a match: a real rule

    def test_parse_iptables(self):
        d = collector.parse_iptables("-P INPUT ACCEPT\n-P FORWARD DROP\n-A INPUT -j ts-input\n"
                                     "-A ts-input -i tailscale0 -j ACCEPT")
        self.assertEqual((d["policy"]["INPUT"], d["count"]["INPUT"], d["ts_input"]), ("ACCEPT", 1, True))
        self.assertFalse(collector.parse_iptables("-P INPUT DROP")["ts_input"])

    def test_parse_drops(self):
        d = collector.parse_drops("x UFW BLOCK SRC=198.51.100.9 DST=1 DPT=23\nx UFW BLOCK SRC=198.51.100.9 DPT=445\nnoise")
        self.assertEqual((d["n"], d["src"], d["dpt"][0][1]), (2, [("198.51.100.9", 2)], 1))

    def test_parse_ports(self):
        got = collector.parse_ports("0.0.0.0:8180->8080/tcp, [::]:8180->8080/tcp, 127.0.0.1:1->2/tcp, 80/tcp")
        self.assertEqual(got, [{"p": 8180, "s": "*"}, {"p": 1, "s": "lo"}])

    def test_parse_ports_scopes(self):
        got = collector.parse_ports("[::1]:80->80/tcp, 100.64.0.3:9->9/tcp, 8000-8010->8000-8010/tcp, [::]:7->7/tcp")
        self.assertEqual(got, [{"p": 80, "s": "lo"}, {"p": 9, "s": "100.64.0.3"},
                               {"p": "8000-8010", "s": "*"}, {"p": 7, "s": "*"}])

    def test_project_label_not_matched_inside_other_value(self):
        self.assertIsNone(collector.PROJECT.search("x=com.docker.compose.project=evil"))
        self.assertEqual(collector.PROJECT.search("a=b,com.docker.compose.project=shop").group(1), "shop")

    def test_exit_zero_filtered_exit_error_kept(self):
        self.assertTrue(collector.EXIT_OK.match("Exited (0) 2 weeks ago"))
        self.assertFalse(collector.EXIT_OK.match("Exited (137) 46 hours ago"))

    def test_parse_ports_empty(self):
        self.assertEqual(collector.parse_ports(""), [])


class Render(unittest.TestCase):
    def test_clip_keeps_ansi_and_width(self):
        s = ansi.c(31, "abcdef")
        self.assertEqual(ansi.vlen(ansi.clip(s, 3)), 3)

    def test_containers_grouped_and_box_aligned(self):
        lines = render.containers_block(CONT, 100)
        text = "\n".join(lines)
        self.assertIn("shop (2)", text)
        self.assertIn("(standalone) (1)", text)
        self.assertIn("*8080 lo:5432", text)
        box = [x for x in lines if x[0] in "┌│└"]
        self.assertTrue(all(ansi.vlen(x) == 100 for x in box), "right borders misaligned")

    def test_unhealthy_is_red(self):
        row = next(x for x in render.containers_block(CONT, 100) if "db-1" in x)
        self.assertIn("\x1b[31m●", row)

    def test_stale_and_missing_state(self):
        old = dict(CONT, ts=time.time() - 500)
        self.assertIn("stale data", "\n".join(render.containers_block(old, 100)))
        self.assertIn("collector not running", render.containers_block(None, 100)[0])

    def test_frame_fits_screen_and_paginates(self):
        smp = hostdata.Sampler()
        time.sleep(0.2)
        w, h = 100, 20
        sl = render.slides(smp.sample(), CONT, NET, w, h - 2)
        self.assertGreater(len(sl), 2, "with 18 usable lines the System page must be split")
        for i, s in enumerate(sl):
            f = render.frame(s, i, len(sl), w, h).split("\x1b[K\r\n")
            self.assertEqual(len(f), h)
            self.assertTrue(all(ansi.vlen(r) <= w for r in f))

    def test_escape_sequences_from_untrusted_data_are_neutralised(self):
        evil = dict(CONT, containers=[dict(CONT["containers"][0], project="a\x1bcb\nc", name="n\x1b[2Jx")])
        text = "\n".join(render.containers_block(evil, 100))
        self.assertNotIn("\x1bc", text)
        self.assertNotIn("\x1b[2J", text)
        self.assertEqual(ui.safe("a\x1b[31m\n"), "a?[31m?")

    def test_exited_container_is_red(self):
        dead = dict(CONT, containers=[dict(CONT["containers"][0], state="exited", status="Exited (137) 1h ago")])
        row = next(x for x in render.containers_block(dead, 100) if "web-1" in x)
        self.assertIn("\x1b[31m●", row)

    def test_specific_ip_bind_is_shown(self):
        self.assertEqual(render.fmt_ports([{"p": 9, "s": "100.64.0.3"}, {"p": 1, "s": "lo"}]), "100.64.0.3:9 lo:1")

    def test_broken_state_file_does_not_crash_frame(self):
        smp = hostdata.Sampler()
        broken = {"ts": time.time(), "containers": [{}]}
        sl = render.slides(smp.sample(), broken, NET, 100, 30)
        self.assertIn("error on page System", "\n".join("\n".join(x[3]) for x in sl))

    def test_load_containers_rejects_wrong_shape(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([1, 2], f)
        self.assertIsNone(hostdata.load_containers(f.name))
        os.unlink(f.name)

    def test_fw_verdicts(self):
        v = exposure.fw_verdict
        self.assertEqual(v(22, "tcp", UFW)[0], "filtered")          # LAN only; the v6 Anywhere rule is ignored
        self.assertEqual(v(9011, "tcp", UFW)[0], "open")
        self.assertEqual(v(8005, "tcp", UFW)[0], "open")            # range 8000:8010
        self.assertEqual(v(7777, "tcp", UFW)[0], "blocked")         # default deny with no rule
        self.assertEqual(v(22, "tcp", dict(UFW, active=False))[0], "nofw")
        self.assertEqual(v(7777, "tcp", None)[0], "unknown")        # missing data: never "blocked"

    def test_exposure_classification(self):
        self.assertEqual(ROWS[(5432, "tcp", 1, 1)]["lan"], 1)       # docker without DOCKER-USER: bypasses ufw
        self.assertIn("bypasses", ROWS[(5432, "tcp", 1, 1)]["note"])
        self.assertTrue(ROWS[(5432, "tcp", 1, 1)]["warn"])          # DB open on the LAN
        self.assertEqual(ROWS[(22, "tcp", 1, 1)]["lan"], 2)         # filtered by source
        self.assertEqual(ROWS[(7777, "tcp", 1, 1)]["lan"], 0)       # blocked by ufw, but on TS (ts-input)
        self.assertEqual(ROWS[(8443, "tcp", 1, 0)]["name"], "container?")  # loopback: local, not on TS
        self.assertEqual(ROWS[(8443, "tcp", 1, 0)]["ts"], 0)
        self.assertEqual(ROWS[(8443, "tcp", 0, 1)]["lan"], 0)       # same port on Tailscale: separate row
        f = ROWS[(8444, "tcp", 0, 1)]
        self.assertTrue(f["net"] and f["bad_note"])                 # Funnel = Internet

    def test_docker_user_rules_change_verdict(self):
        net = dict(NET, docker_user=["-A DOCKER-USER -j DROP"])
        row = next(r for r in exposure.exposure_rows(net, CONT) if r["port"] == 5432)
        self.assertEqual((row["lan"], row["warn"]), (2, False))

    def test_page_rete_layouts(self):
        narrow = render.page_rete(NET, CONT, 120)
        wide = render.page_rete(NET, CONT, 240)
        self.assertLess(len(wide), len(narrow))                     # side by side: fewer lines
        self.assertTrue(all(ansi.vlen(x) <= 240 for x in wide))
        self.assertIn("DOCKER-USER empty", "\n".join(narrow))
        self.assertIn("network collector not running", "\n".join(render.page_rete(None, CONT, 120)))
        self.assertIn("stale", "\n".join(render.page_rete(dict(NET, ts=1), CONT, 120)))

    def test_wide_containers_two_columns(self):
        one, two = render.containers_block(CONT, 100), render.containers_block(CONT, 240)
        self.assertLess(len(two), len(one))
        self.assertTrue(all(ansi.vlen(x) <= 240 for x in two))

    def ufw(self, *rules, default="deny (incoming), allow (outgoing), disabled (routed)"):
        rows = [dict(zip(("to", "action", "from"), r)) for r in rules]
        return {"active": True, "default": default, "logging": "on (low)", "rules": rows}

    def test_fw_never_blocked_when_rule_not_understood(self):
        """Review finding 1: a rule that cannot be read must give 'unknown', never 'blocked'."""
        v = exposure.fw_verdict
        for to in ("Nginx Full", "10.0.0.5 80"):
            self.assertEqual(v(80, "tcp", self.ufw((to, "ALLOW IN", "Anywhere")))[0], "unknown", to)
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp on eth0", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(443, "tcp", self.ufw(("80,443/tcp", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(9999, "tcp", self.ufw(("Anywhere on eth0", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(22, "tcp", self.ufw(("22 (v6)", "ALLOW IN", "2001:db8::/32 (v6)")))[0], "unknown")
        self.assertEqual(v(22, "tcp", self.ufw(("Anywhere", "ALLOW IN", "Anywhere 53")))[0], "unknown")

    def test_fw_deny_order_and_scope(self):
        v = exposure.fw_verdict
        deny_first = self.ufw(("22/tcp", "DENY IN", "10.0.0.0/8"), ("22/tcp", "ALLOW IN", "Anywhere"))
        self.assertEqual(v(22, "tcp", deny_first)[0], "filtered")   # open except 10.0.0.0/8
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp", "DENY IN", "Anywhere"), ("22/tcp", "ALLOW IN", "Anywhere")))[0],
                         "blocked")                                  # first match wins
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp", "ALLOW FWD", "Anywhere")))[0], "blocked")  # route, not inbound
        self.assertEqual(v(22, "udp", self.ufw(("22/tcp", "ALLOW IN", "Anywhere")))[0], "blocked")
        self.assertEqual(v(80, "tcp", self.ufw(("80", "ALLOW IN", "Anywhere"), default="allow (incoming)"))[0], "open")

    def test_docker_verdict(self):
        d = exposure.docker_verdict
        self.assertEqual(d(None)[0], 3)
        self.assertEqual(d([])[0], 1)
        self.assertEqual(d(["-A DOCKER-USER -j DROP"])[0], 2)                       # blanket DROP: certain
        self.assertEqual(d(["-A DOCKER-USER -p tcp -m tcp --dport 80 -j DROP"])[0], 3)  # container port: unknown
        self.assertEqual(d(["-A DOCKER-USER -j ufw-user-forward"])[0], 3)          # external chain
        self.assertEqual(d(["-A DOCKER-USER -s 203.0.113.1 -j ACCEPT"])[0], 1)         # no DROP: still open

    def test_userland_proxy_off_published_port_still_listed(self):
        net = dict(NET, listeners=[])                                              # ss sees no socket
        ports = {r["port"] for r in exposure.exposure_rows(net, CONT)}
        self.assertIn(8080, ports)

    def test_funnel_tcp_forward_foreground_and_failure(self):
        sv = collector.parse_serve('{"TCP": {"8022": {"TCPForward": "127.0.0.1:22"}}, "AllowFunnel": {"host.example.ts.net:8022": true},'
                                   ' "Foreground": {"x": {"Web": {"host.example.ts.net:9000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}},'
                                   ' "AllowFunnel": {"host.example.ts.net:9000": true}}}}')
        self.assertEqual({(x["port"], x["funnel"]) for x in sv}, {(8022, True), (9000, True)})
        net = dict(NET, serve=None, errors={"serve": "tailscale failed"})
        row = next(r for r in exposure.exposure_rows(net, CONT) if r["port"] == 8444)
        self.assertEqual(row["net"], 3)                                             # Funnel unknown: "?", not "no"

    def test_listeners_missing_is_not_shown_as_zero(self):
        text = "\n".join(render.page_rete(dict(NET, listeners=None, errors={"listeners": "ss failed"}), CONT, 120))
        self.assertIn("EXPOSURE unavailable", text)

    def test_ufw_logging_off_is_stated(self):
        text = "\n".join(render.firewall_block(dict(NET, ufw=dict(UFW, logging="off")), 120))
        self.assertIn("ufw logging off", text)

    def test_problems_summary_and_pill(self):
        pb = problems.problems(NET, CONT)
        self.assertEqual(pb[0][0], 2)                                               # errors before warnings
        self.assertTrue(any("DB/broker open on LAN" in t for _, t in pb))
        self.assertTrue(any("Funnel" in t for _, t in pb))
        self.assertIn("PROBLEMS", problems.status_pill(pb)[0])
        self.assertEqual(problems.status_pill([]), ("✔ ALL OK", "1;7"))
        self.assertTrue(any("network collector not running" in t for _, t in problems.problems(None, CONT)))
        off = problems.problems(dict(NET, ufw=dict(UFW, active=False)), CONT)
        self.assertTrue(any("ufw off" in t for _, t in off))

    def test_header_shows_status_and_fits(self):
        smp = hostdata.Sampler()
        sl = render.slides(smp.sample(), CONT, NET, 100, 30)
        f = render.frame(sl[0], 0, len(sl), 100, 32, problems.problems(NET, CONT)).split("\x1b[K\r\n")
        self.assertIn("PROBLEMS", f[0])
        self.assertLessEqual(ansi.vlen(f[0]), 100)

    def test_boot_page_fits_one_screen_at_common_sizes(self):
        for w, h in ((100, 30), (120, 33), (160, 40), (240, 67)):
            lines = render.page_boot(BOOT, w - 1, h - 2)
            self.assertLessEqual(len(lines), h - 2, f"{w}x{h}: the Boot page must fit on one screen")
            self.assertTrue(all(ansi.vlen(x) <= w - 1 for x in lines), f"{w}x{h}: line wider than the screen")

    def test_boot_page_content_and_untrusted_text(self):
        raw = "\n".join(render.page_boot(BOOT, 199, 60))
        text = ansi.ANSI.sub("", raw)
        self.assertIn("boot finished in 31.3s", text)
        self.assertIn("1 started at boot", text)                                   # 'a' yes, 'b' started by hand
        self.assertIn("won't restart on reboot", text)
        self.assertNotIn("\x1b[2J", raw)                                           # journal message sanitised
        self.assertIn("boot collector not running", "\n".join(render.page_boot(None, 100, 30)))

    def test_wrap_items_never_exceeds_width_and_counts_hidden(self):
        items = [f"item{i}" for i in range(40)]
        for w in (40, 80, 120):
            lines = render.wrap_items(items, w, indent=5, max_lines=2)
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(ansi.vlen(x) <= w for x in lines))
            shown = sum(x.count("item") for x in lines)
            self.assertIn(f"+{40 - shown}", ansi.ANSI.sub("", lines[-1]))     # no item disappears without being counted
        self.assertEqual(len(render.wrap_items(items[:3], 80, indent=5, max_lines=2)), 1)

    def test_boot_problems(self):
        pb = problems.problems(NET, CONT, boot=dict(BOOT, failed=["x.service"]))
        self.assertTrue(any("1 failed systemd unit: x.service" in t for _, t in pb))
        self.assertTrue(any("3 errors in this boot's journal" in t for _, t in problems.problems(NET, CONT, boot=BOOT)))
        self.assertTrue(any("boot collector not running" in t for _, t in problems.problems(NET, CONT, boot=None)))
        self.assertFalse(any("boot" in t for _, t in problems.problems(NET, CONT)))    # default: no boot check

    def test_thermal_lines_thresholds_from_sensor_max(self):
        th = {"cpu": (52.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "throttle": 10, "recent": 0,
              "clk": (2.6, 4.9)}
        text = ansi.ANSI.sub("", "\n".join(render.thermal_lines(th, 30)))
        self.assertIn("52°C/105°C   limits 84/94°C", text)                         # 80% and 90% of the sensor maximum
        self.assertIn("33°C/86°C   limits 69/77°C", text)
        self.assertIn("7.8 min total since boot", text)
        self.assertIn("none in the last minute", text)
        hot = ansi.ANSI.sub("", "\n".join(render.thermal_lines(dict(th, recent=12), 30)))
        self.assertIn("THROTTLING now (+12 events/min)", hot)
        self.assertIn("measuring", ansi.ANSI.sub("", "\n".join(render.thermal_lines(dict(th, recent=None), 30))))
        self.assertEqual(render.thermal_lines({}, 30), [])                          # no sensors: no line

    def test_thermal_problems(self):
        base = {"cpu": (50.0, 105.0), "throttle_s": 1.0, "recent": 0}
        self.assertFalse(any("°C" in t or "throttling" in t for _, t in problems.problems(NET, CONT, thermal=base)))
        warn = problems.problems(NET, CONT, thermal=dict(base, cpu=(85.0, 105.0)))
        self.assertTrue(any(sev == 1 and "CPU at 85°C: above the 84°C threshold" in t for sev, t in warn))
        err = problems.problems(NET, CONT, thermal=dict(base, cpu=(95.0, 105.0)))
        self.assertTrue(any(sev == 2 and "CPU at 95°C: above the 94°C threshold" in t for sev, t in err))
        thr = problems.problems(NET, CONT, thermal=dict(base, recent=7))
        self.assertTrue(any("thermal throttling: 7 events" in t for _, t in thr))

    def test_baseline_alarms(self):
        keys = exposure.exposure_keys(NET, CONT)
        self.assertIn("8443/t:TAILNET", keys)                                       # fixed tailscaled port: tracked
        self.assertIn("8444/t:INTERNET", keys)
        self.assertFalse(any(k.startswith("55432/") for k in keys))                 # local only: excluded
        eph = dict(NET, listeners=NET["listeners"] + [{"proto": "tcp", "addr": "100.64.0.1", "port": 50949, "proc": "tailscaled"}])
        self.assertNotIn("50949/t:TAILNET", exposure.exposure_keys(eph, CONT))        # ephemeral tailscaled port: excluded
        base = {"ts": 1, "ports": dict(keys)}
        self.assertFalse(any(sev == 3 for sev, _ in problems.problems(NET, CONT, baseline=base)))   # same: no alarm
        del base["ports"]["5432/t:LAN"]                                             # one more LAN port than expected
        pb = problems.problems(NET, CONT, baseline=base)
        self.assertEqual(pb[0][0], 3)                                               # alarms go on top
        self.assertIn("NEW exposed port: 5432/t lan", pb[0][1])
        self.assertIn("EXPOSED PORTS CHANGED", problems.status_pill(pb)[0])
        base["ports"]["99/t:LAN"] = "old"                                           # an expected port is gone
        self.assertTrue(any("1 port no longer exposed" in t for sev, t in problems.problems(NET, CONT, baseline=base) if sev == 1))
        self.assertTrue(any("port baseline missing" in t for _, t in problems.problems(NET, CONT, baseline=None)))
        self.assertFalse(any("baseline" in t for _, t in problems.problems(NET, CONT)))   # default: no check

    def test_accept_baseline_roundtrip(self):
        import contextlib
        import io
        import tempfile
        d = tempfile.mkdtemp()
        net, state, bl = os.path.join(d, "net.json"), os.path.join(d, "c.json"), os.path.join(d, "sub", "baseline.json")
        import json
        json.dump(dict(NET, ts=time.time()), open(net, "w"))   # fresh now: the fixtures date from the import, minutes ago on a slow runner
        json.dump(dict(CONT, ts=time.time()), open(state, "w"))
        old = (hostdata.NET_STATE, hostdata.STATE)
        hostdata.NET_STATE, hostdata.STATE = net, state
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(problems.accept_baseline(path=bl), 0)
                mtime = os.path.getmtime(bl)
                self.assertEqual(problems.accept_baseline(if_missing=True, path=bl), 0)     # already present: left untouched
            self.assertEqual(os.path.getmtime(bl), mtime)
            self.assertEqual(exposure.baseline_diff(exposure.exposure_keys(NET, CONT), problems.load_baseline(bl)), ({}, {}, {}))
            os.unlink(net)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(problems.accept_baseline(path=os.path.join(d, "b2.json")), 1)  # unknown state: does not write
            self.assertFalse(os.path.exists(os.path.join(d, "b2.json")))
        finally:
            hostdata.NET_STATE, hostdata.STATE = old

    def test_baseline_detects_service_and_filter_changes(self):
        """Review: a change of service or rule on the same port used to raise no alarm."""
        keys = exposure.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items()}}
        self.assertEqual(exposure.baseline_diff(keys, base)[2], {})
        evil = {k: dict(v) for k, v in keys.items()}
        evil["22/t:LAN"]["name"] = "evil"                                            # another process on port 22
        pb = problems.problems(dict(NET, listeners=[dict(x, proc="evil") if x["port"] == 22 else x for x in NET["listeners"]]),
                             CONT, baseline=base)
        self.assertTrue(any(sev == 3 and "CHANGED 22/t: service sshd → evil" in t for sev, t in pb))
        base["ports"]["22/t:LAN"]["lan"] = 2                                         # expected: filtered by source
        keys["22/t:LAN"]["lan"] = 1                                                  # now open to all
        self.assertIn("now open to the whole LAN", exposure.baseline_diff(keys, base)[2]["22/t:LAN"])
        keys["22/t:LAN"]["lan"] = 2                                                  # stricter or equal: no alarm
        self.assertEqual(exposure.baseline_diff(keys, base)[2], {})
        # CHANGED marker in the table
        self.assertEqual(set(exposure.new_ports(NET, CONT, {"ts": 1, "ports": {"22/t:LAN": {"name": "x", "lan": 2}}}).values()),
                         {"NEW", "CHANGED"})

    def test_baseline_suspended_on_partial_data_and_corrupt_file(self):
        keys = exposure.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items() if k != "9011/t:LAN"}}
        partial = dict(NET, errors={"ufw": "boom"})
        pb = problems.problems(partial, CONT, baseline=base)
        self.assertFalse(any(sev == 3 for sev, _ in pb), "partial data: no false red alarms")
        self.assertTrue(any("port comparison suspended" in t for _, t in pb))
        self.assertEqual(exposure.new_ports(partial, CONT, base), {})
        self.assertTrue(any(sev == 2 and "port baseline unreadable" in t for sev, t in problems.problems(NET, CONT, baseline="corrotta")))
        import tempfile
        d = tempfile.mkdtemp()
        self.assertIsNone(problems.load_baseline(os.path.join(d, "nope.json")))       # missing
        bad = os.path.join(d, "bad.json")
        open(bad, "w").write("{truncated")
        self.assertEqual(problems.load_baseline(bad), "corrotta")                     # present but broken: not 'missing'
        self.assertEqual(exposure.new_ports(NET, CONT, "corrotta"), {})               # and does not crash the callers

    def test_accept_refuses_stale_or_partial_state_and_exit_code(self):
        import json
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        net, state = os.path.join(d, "net.json"), os.path.join(d, "c.json")
        json.dump(CONT, open(state, "w"))
        old = (hostdata.NET_STATE, hostdata.STATE)
        hostdata.NET_STATE, hostdata.STATE = net, state
        try:
            import contextlib
            import io
            for label, data in (("stale", dict(NET, ts=time.time() - 9999)), ("with errors", dict(NET, errors={"ufw": "x"}))):
                json.dump(data, open(net, "w"))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(problems.accept_baseline(path=os.path.join(d, "b.json")), 1, label)
                self.assertFalse(os.path.exists(os.path.join(d, "b.json")), label)
        finally:
            hostdata.NET_STATE, hostdata.STATE = old
        env = dict(os.environ, NUC_CONSOLE_NET="/nonexistent", NUC_CONSOLE_STATE="/nonexistent",
                   NUC_CONSOLE_BASELINE=os.path.join(d, "x", "b.json"))
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "render.py"), "--accept"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)                                            # the failure is visible to install.sh

    def test_tailscaled_ephemeral_ports_excluded_even_with_ufw_off(self):
        off = dict(NET, ufw=dict(UFW, active=False), listeners=NET["listeners"] + [
            {"proto": "udp", "addr": "0.0.0.0", "port": 50001, "proc": "tailscaled"},
            {"proto": "udp", "addr": "0.0.0.0", "port": 41641, "proc": "tailscaled"}])
        keys = exposure.exposure_keys(off, CONT)
        self.assertNotIn("50001/u:LAN", keys)                                        # ephemeral: excluded in any group
        self.assertIn("41641/u:LAN", keys)                                           # fixed: tracked

    def test_thermal_hotplug_and_narrow_line(self):
        smp = hostdata.Sampler()
        now = time.monotonic()
        smp.hist.append((now - 30, 1000))
        orig = hostdata.read_thermal
        hostdata.read_thermal = lambda: {"cpu": (50.0, 105.0), "throttle": 900, "throttle_s": 1.0, "clk": (2.6, 4.9)}
        try:
            self.assertIsNone(smp.sample()["thermal"]["recent"])                     # CPU offline: falling counter, never negative
        finally:
            hostdata.read_thermal = orig
        th = {"cpu": (50.0, 105.0), "clk": (2.6, 4.9)}
        for mw in (40, 60, 90):
            self.assertTrue(all(ansi.vlen(x) <= mw or mw < 40 for x in render.thermal_lines(th, 10, mw)))
        self.assertIn("clock", "\n".join(render.thermal_lines(th, 10, 120)))
        self.assertNotIn("clock", "\n".join(render.thermal_lines(th, 10, 55)))      # when narrow the clock goes first

    def test_overview_fits_small_console_and_never_wider(self):
        smp = hostdata.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "recent": 0, "clk": (2.6, 4.9)}
        for w, h in ((79, 24), (80, 25), (100, 30), (119, 32)):
            sl = render.slides(sm, CONT, NET, w - 1, h - 2, BOOT, baseline=False, mode="overview")
            self.assertEqual(len(sl), 1, f"{w}x{h}: single screen")
            self.assertTrue(all(ansi.vlen(x) <= w - 1 for x in sl[0][3]), f"{w}x{h}: line wider than the screen")

    def test_overview_broken_block_title_uses_width(self):
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}}
        lines = render.page_overview(sm, CONT, dict(NET, ufw="x", docker_user=5), BOOT, 100, 60)
        self.assertTrue(all(ansi.vlen(x) <= 100 for x in lines))

    def test_overview_fits_one_screen(self):
        smp = hostdata.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        for w, h in ((100, 30), (120, 33), (160, 40), (240, 67)):
            sl = render.slides(sm, CONT, NET, w - 1, h - 2, BOOT, baseline=False, mode="overview")
            self.assertEqual(len(sl), 1, f"{w}x{h}: the single screen must be just one")
            self.assertLessEqual(len(sl[0][3]), h - 2)
            self.assertTrue(all(ansi.vlen(x) <= w - 1 for x in sl[0][3]), f"{w}x{h}: line wider than the screen")
            f = render.frame(sl[0], 0, 1, w - 1, h, problems.problems(NET, CONT, boot=BOOT)).split("\x1b[K\r\n")
            self.assertEqual(len(f), h)
            self.assertIn("single screen", f[-1])
            self.assertIn(f"console {w}x{h}", f[-1])

    def test_overview_content_and_new_marker(self):
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {"cpu": (50.0, 105.0)}}
        keys = exposure.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: v for k, v in keys.items() if k != "9011/t:LAN"}}
        text = ansi.ANSI.sub("", "\n".join(render.page_overview(sm, CONT, NET, BOOT, 200, 60, baseline=base)))
        for word in ("ATTENTION", "EXPOSURE", "FIREWALL", "SYSTEM", "CONTAINER", "BOOT", "NEW exposed port: 9011"):
            self.assertIn(word, text)
        self.assertIn("NEW ", text)                                                 # marker on the new port
        broken = "\n".join(render.page_overview(sm, CONT, dict(NET, ufw="x"), BOOT, 120, 60))
        self.assertIn("SYSTEM", ansi.ANSI.sub("", broken))                        # malformed state: the screen stays alive
        self.assertEqual(problems.safe_problems(dict(NET, ufw="x"), CONT)[0][0], 2)   # and the header does not raise
        self.assertEqual(problems.safe_problems(5, CONT)[0][0], 2)

    def test_collector_db_items_and_privacy(self):
        def ct(name, image, nets, env=(), ports=None, project="", service="", pid=100, ip=None, expose=("5432/tcp",)):
            return {"Name": "/" + name, "State": {"Status": "running", "Pid": pid},
                    "Config": {"Image": image, "Env": list(env), "ExposedPorts": {e: {} for e in expose},
                               "Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service}},
                    "HostConfig": {"PortBindings": {}, "NetworkMode": "default"},
                    "NetworkSettings": {"Ports": ports or {},
                                        "Networks": {n: {"Aliases": [service or name, "abc123def456"],
                                                         "IPAddress": ip or "172.20.0.9", "Gateway": "172.20.0.1"} for n in nets}}}
        insp = [ct("shop-db-1", "postgres:16", ["shop_net"], project="shop", service="db", ip="172.20.0.2",
                   ports={"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}, {"HostIp": "::", "HostPort": "5432"}]}),
                ct("shop-api-1", "myapp:1", ["shop_net"], env=["DATABASE_URL=" + FAKE_URL], project="shop", ip="172.20.0.3"),
                ct("shop-web-1", "nginx", ["shop_net"], env=["FOO=bar", "DB_TYPE=postgres", "OTHER=" + "post" + "gres://otherhost/x"],
                   project="shop", ip="172.20.0.4"),
                ct("shop-job-1", "prodrigestivill/postgres-backup-local:16", ["shop_net"], env=["POSTGRES_HOST=db"],
                   project="shop", ip="172.20.0.5"),
                ct("cache", "redis:7", ["bridge"], ports={"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6379"}]},
                   pid=200, ip="172.17.0.2", expose=("6379/tcp",)),
                ct("rand", "timescale/timescaledb:2", ["bridge"], ports={"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "49153"}]},
                   pid=300, ip="172.17.0.3"),
                ct("host-db", "mongo:7", ["host"], pid=400)]
        insp[-1]["HostConfig"]["NetworkMode"] = "host"
        host_conns = [{"l_addr": "127.0.0.1", "l_port": 42000, "p_addr": "127.0.0.1", "p_port": 6379, "proc": "node"},
                      {"l_addr": "10.9.9.9", "l_port": 43000, "p_addr": "203.0.113.5", "p_port": 6379, "proc": "cloudclient"}]
        ns = {100: [{"l_addr": "172.20.0.2", "l_port": 5432, "p_addr": "172.20.0.3", "p_port": 5000, "proc": ""},   # container api
                    {"l_addr": "172.20.0.2", "l_port": 5432, "p_addr": "192.168.0.55", "p_port": 5001, "proc": ""},  # external LAN client
                    {"l_addr": "172.20.0.2", "l_port": 5432, "p_addr": "172.20.0.1", "p_port": 5002, "proc": ""}],   # from the gateway = host
              200: []}
        collector.EXTERNAL_SEEN.clear()
        out = collector.db_items(insp, host_conns, lambda pid: ns.get(pid), {"127.0.0.1", "192.168.0.10"}, now=1000.0)
        by = {i["name"]: i for i in out["items"]}
        self.assertEqual(set(by), {"shop-db-1", "cache", "rand", "host-db"})          # backup/app are not DBs; timescaledb is
        db = by["shop-db-1"]
        self.assertEqual(db["kind"], "postgres")
        self.assertEqual(db["active"], ["shop-api-1"])                                # runtime evidence: connected now, from the namespace
        self.assertEqual(db["usano"], ["shop-api-1", "shop-job-1"])                   # env: URL with host 'db' and POSTGRES_HOST=db
        self.assertEqual(db["stessa_rete"], ["shop-web-1"])                           # DB_TYPE=postgres and otherhost do NOT count
        self.assertEqual(db["ports"], [{"p": 5432, "c": "5432/tcp", "s": "*"}])       # v4+v6 of the same binding: once
        self.assertEqual(db["external"], [{"ip": "192.168.0.55", "last": 1000.0}])    # seen inside the namespace: DNAT does not hide it
        self.assertEqual(db["ext_source"], "netns")
        self.assertEqual(by["cache"]["host_clients"], ["node"])                       # local peers only: 'cloudclient' excluded
        self.assertEqual(by["cache"]["ext_source"], "netns")
        self.assertEqual(by["rand"]["ports"], [{"p": 49153, "c": "5432/tcp", "s": "*"}])   # random port read from NetworkSettings
        self.assertEqual(by["rand"]["ext_source"], "n/d")                              # unreadable namespace: never "none seen"
        self.assertTrue(by["host-db"]["host_net"])
        self.assertEqual(by["host-db"]["ext_source"], "n/d")
        self.assertNotIn(FAKE_PW, json.dumps(out))                                   # env values never leave
        self.assertTrue(collector.is_local("::ffff:127.0.0.1", set()))                # IPv4-mapped
        self.assertTrue(collector.is_local("192.168.0.10", {"192.168.0.10"}))
        self.assertFalse(collector.is_local("192.168.0.55", {"192.168.0.10"}))
        self.assertEqual(collector.parse_established("0 0 127.0.0.1:41000 127.0.0.1:5432 users:((\"psql\",pid=1,fd=3))\nnoise"),
                         [{"l_addr": "127.0.0.1", "l_port": 41000, "p_addr": "127.0.0.1", "p_port": 5432, "proc": "psql"}])

    def test_db_kind_and_env_uses(self):
        for img, kind in (("postgres:16", "postgres"), ("pgvector/pgvector:pg16", "postgres"), ("eclipse-mosquitto:2.0", "mosquitto"),
                          ("timescale/timescaledb:2", "postgres"), ("bitnami/postgresql:16", "postgres"), ("docker.io/library/redis@sha256:ab", "redis"),
                          ("registry.example/mirror/mysql:8", "mysql"), ("quay.io/keydb/keydb", "keydb")):
            self.assertEqual(collector.db_kind(img), kind, img)
        for img in ("postgrest/postgrest", "mongo-express", "redis-commander", "redisinsight", "prodrigestivill/postgres-backup-local:16",
                    "myorg/postgres-exporter", "pgadmin-admin", "sha256:abcdef", "nginx", "myproj-redis-worker"):
            self.assertIsNone(collector.db_kind(img), img)
        al = {"db", "shop-db-1"}
        u = collector.env_uses
        self.assertTrue(u(al, "DATABASE_URL", "post" + "gres://u:p%40ss@db:5432/x"))
        self.assertTrue(u(al, "JDBC", "jdbc:" + "postgresql://db:5432/x"))
        self.assertTrue(u(al, "DB_HOST", "db"))
        self.assertTrue(u(al, "PGHOST", "db:5432"))
        self.assertFalse(u(al, "DB_TYPE", "db"))                                       # not a host
        self.assertFalse(u({"postgres"}, "URL", "post" + "gres://otherhost/x"))            # the scheme is not the host
        self.assertFalse(u(al, "DATA_DIR", "/var/lib/db"))

    def test_external_seen_is_bounded(self):
        collector.EXTERNAL_SEEN.clear()
        def ct(name, pid):
            return {"Name": "/" + name, "State": {"Status": "running", "Pid": pid},
                    "Config": {"Image": "redis:7", "Env": [], "ExposedPorts": {"6379/tcp": {}}, "Labels": {}},
                    "HostConfig": {"PortBindings": {}}, "NetworkSettings": {"Ports": {}, "Networks": {"bridge": {"IPAddress": "172.17.0.2"}}}}
        many = [{"l_addr": "x", "l_port": 6379, "p_addr": f"198.51.100.{i}", "p_port": 1, "proc": ""} for i in range(1, 60)]
        out = collector.db_items([ct("r", 1)], [], lambda pid: many, set(), now=5000.0)
        self.assertEqual(len(out["items"][0]["external"]), collector.EXTERNAL_MAX_IPS)  # cap on remembered IPs
        old = collector.db_items([ct("r", 1)], [], lambda pid: [], set(), now=5000.0 + collector.EXTERNAL_TTL_S + 1)
        self.assertEqual(old["items"][0]["external"], [])                              # expired after 24 h
        collector.db_items([], [], lambda pid: [], set())
        self.assertNotIn("r", collector.EXTERNAL_SEEN)                                 # container gone: no orphan key

    def test_stack_detail_and_database_section(self):
        cont = {"ts": time.time(), "containers": [
            {"name": "shop-api-1", "status": "Up 1h", "state": "running", "project": "shop", "ports": [], "mem": 2**20 * 50},
            {"name": "shop-db-1", "status": "Up 1h (unhealthy)", "state": "running", "project": "shop", "ports": [], "mem": None},
            {"name": "solo", "status": "Exited (1)", "state": "exited", "project": "", "ports": [], "mem": None}]}
        text = ansi.ANSI.sub("", "\n".join(cardlines.stack_lines(cont, 100, 3)))
        self.assertIn("shop", text)
        self.assertIn("1/2 running", text)                                         # unhealthy does not count as running
        self.assertIn("● api", text)
        self.assertIn("✖ db", text)
        self.assertIn("(no stack)", text)
        self.assertEqual(cards.short_name("shop-api-1", "shop"), "api")
        dbs = {"since": time.time() - 7200, "items": [
            {"name": "shop-db-1", "kind": "postgres", "project": "shop", "host_net": False,
             "ports": [{"p": 5432, "c": "5432/tcp", "s": "*"}], "active": ["shop-api-1"], "usano": ["shop-api-1", "shop-job-1"],
             "stessa_rete": ["shop-web-1"], "host_clients": [], "external": [], "ext_source": "netns"},
            {"name": "cache", "kind": "redis", "project": "", "host_net": False, "ports": [{"p": 6379, "c": "6379/tcp", "s": "lo"}],
             "active": [], "usano": [], "stessa_rete": [], "host_clients": ["node"], "ext_source": "netns",
             "external": [{"ip": "192.168.0.55", "last": time.time()}]},
            {"name": "blind", "kind": "mongo", "project": "", "host_net": True, "ports": [{"p": 0, "c": "host", "s": "*"}],
             "active": [], "usano": [], "stessa_rete": [], "host_clients": [], "external": [], "ext_source": "n/d"},
            {"name": "old", "kind": "redis", "project": "", "host_net": False, "ports": [{"p": 6380, "c": "6379/udp", "s": "lo"}],
             "active": [], "usano": [], "stessa_rete": [], "host_clients": [], "ext_source": "netns",
             "external": [{"ip": "203.0.113.7", "last": time.time() - 4000}]}]}
        out = ansi.ANSI.sub("", "\n".join(cardlines.ov_database(dict(NET, dbs=dbs), cont, 120, 0)))
        self.assertIn("4 running · 2 exposed", out)                                # postgres on * and mongo on the host network
        self.assertIn("  exposed", out)
        self.assertIn("in use now: api", out)                                      # runtime evidence
        self.assertIn("declared by job", out)                                      # declared only (env): without api, already 'in use'
        self.assertNotIn("declared by api", out)
        self.assertIn("host network", out)
        self.assertIn("external clients: not detectable", out)                     # never a false reassurance
        self.assertIn("last external client 203.0.113.7 67 min ago", out)         # old external: do not say 'none'
        self.assertIn("*5432", out)
        self.assertIn("lo:6380/u", out)
        self.assertIn("same network: web", out)
        self.assertIn("host processes: node", out)
        self.assertIn("external clients now: 192.168.0.55", out)                       # recent external: highlighted
        self.assertIn("no external client seen in 2 h", out)
        self.assertIn("unavailable", "\n".join(cardlines.ov_database(dict(NET), cont, 120, 0)))       # old collector
        compact = ansi.ANSI.sub("", "\n".join(cardlines.ov_database(dict(NET, dbs=dbs), cont, 120, 2)))
        self.assertNotIn("in use now", compact)                                    # compact levels: one line per database

    def test_overview_uses_available_space_and_shrinks(self):
        smp = hostdata.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "throttle_s": 469.0, "recent": 0}
        dbs = {"since": time.time(), "items": []}
        big = render.page_overview(sm, CONT, dict(NET, dbs=dbs), BOOT, 239, 65)
        small = render.page_overview(sm, CONT, dict(NET, dbs=dbs), BOOT, 119, 31)
        text = ansi.ANSI.sub("", "\n".join(big))
        for word in ("HEAVIEST CONTAINERS", "DATABASE", "SLOWEST UNITS", "UFW INBOUND RULES", "PORT"):
            self.assertIn(word, text)                                              # 240x67: full tables, every section
        self.assertGreater(len(big), len(small))
        self.assertLessEqual(len(big), 65)
        self.assertLessEqual(len(small), 31)
        self.assertNotIn("HEAVIEST CONTAINERS", ansi.ANSI.sub("", "\n".join(small)))
        self.assertTrue(all(ansi.vlen(x) <= 239 for x in big))

    def test_database_lines_wrap_at_intermediate_width(self):
        """Review: at 199 columns long lists stayed on one line and were silently cut."""
        many = [f"long-service-number-{i}-1" for i in range(30)]
        dbs = {"since": time.time() - 3600, "items": [
            {"name": "shop-db-1", "kind": "elasticsearch", "project": "shop", "host_net": False,
             "ports": [{"p": 9200, "c": "9200/tcp", "s": "*"}, {"p": 9300, "c": "9300/tcp", "s": "*"}],
             "active": many, "usano": many, "stessa_rete": many, "host_clients": [], "ext_source": "netns",
             "external": [{"ip": f"198.51.100.{i}", "last": time.time()} for i in range(1, 40)]}]}
        for w in (79, 119, 199, 300):
            lines = cardlines.ov_database(dict(NET, dbs=dbs), CONT, w, 0)
            self.assertTrue(all(ansi.vlen(x) <= w for x in lines), f"w={w}: line wider than the screen")
        wide = "\n".join(ansi.ANSI.sub("", x) for x in cardlines.ov_database(dict(NET, dbs=dbs), CONT, 199, 0))
        self.assertIn("elasticsearch", wide)                                        # long type: the column adapts
        self.assertGreater(wide.count("\n"), 3)                                     # and wraps instead of truncating

    def test_real_ufw_output_parsing_and_compact_rule_table(self):
        """Real NUC data: a long 'To' column with no double spaces shifted the fields; IPv6/OUT rules bloated the table."""
        real = collector.parse_ufw("""Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
8501/tcp                   ALLOW IN    192.168.0.0/24
Anywhere on tailscale0     ALLOW IN    Anywhere
41641/udp                  ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
Anywhere (v6) on tailscale0 ALLOW IN    Anywhere (v6)            # tailnet
22/tcp                     ALLOW OUT   Anywhere
172.16.0.0/12              ALLOW OUT   Anywhere
""")
        self.assertEqual(len(real["rules"]), 8)
        v6 = real["rules"][5]
        self.assertEqual((v6["to"], v6["action"], v6["from"]), ("Anywhere (v6) on tailscale0", "ALLOW IN", "Anywhere (v6)"))  # comment stripped
        self.assertEqual(real["rules"][6]["action"], "ALLOW OUT")
        self.assertEqual(exposure.fw_verdict(22, "tcp", real)[0], "open")
        self.assertEqual(exposure.fw_verdict(9999, "tcp", real)[0], "blocked")         # the tailscale0 rule does not concern the LAN
        text = ansi.ANSI.sub("", "\n".join(render.firewall_block(dict(NET, ufw=real), 120)))
        self.assertIn("UFW INBOUND RULES (4)", text)                                 # IPv4 inbound only
        self.assertIn("2 outbound", text)
        self.assertIn("2 mirrored IPv6", text)
        self.assertNotIn("172.16.0.0/12", text)
        # with dozens of pre-existing rules the single screen at 240x67 must stay at the rich level
        big = dict(real, rules=real["rules"] + [{"to": f"{3000 + i}/tcp", "action": "ALLOW IN", "from": "192.168.0.0/24"} for i in range(20)]
                   + [{"to": f"{3000 + i}/tcp (v6)", "action": "ALLOW IN", "from": "Anywhere (v6)"} for i in range(20)]
                   + [{"to": f"{i}/tcp", "action": "ALLOW OUT", "from": "Anywhere"} for i in range(20)])
        smp = hostdata.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "throttle_s": 1.0, "recent": 0}
        page = render.page_overview(sm, CONT, dict(NET, ufw=big, dbs={"since": time.time(), "items": []}), BOOT, 239, 65)
        text2 = ansi.ANSI.sub("", "\n".join(page))
        self.assertIn("UFW INBOUND RULES", text2)                                    # rich level: full tables, not the summary
        self.assertIn("PORT", text2)
        self.assertLessEqual(len(page), 65)
        capped = ansi.ANSI.sub("", "\n".join(render.firewall_block(dict(NET, ufw=big), 120, 3)))
        self.assertEqual(capped.count("ALLOW IN"), 3)                                # cap respected
        self.assertIn("… +21 rules", capped)                                         # extra rules are counted, not silently cut
        self.assertIn("22/tcp", capped.split("UFW INBOUND RULES")[1].split("\n")[2])  # the rule open to all comes first

    def test_ufw_block_log_lines_are_not_counted_as_errors(self):
        j = collector.parse_journal('{"PRIORITY":"3","SYSLOG_IDENTIFIER":"kernel","MESSAGE":"[UFW BLOCK] IN=eth0 SRC=203.0.113.7"}\n'
                                    '{"PRIORITY":"3","SYSLOG_IDENTIFIER":"sshd","MESSAGE":"real error"}\n')
        self.assertEqual((j["err"], j["warn"]), (1, 0))
        self.assertEqual([t["id"] for t in j["top"]], ["sshd"])

    def test_new_data_parsers(self):
        dev = ("Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
               "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n  eth0: 5000 10 0 0 0 0 0 0 7000 12 0 0 0 0 0 0\n"
               " veth1: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\nbr-abc: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\ndocker0: 9 1 0 0 0 0 0 0 8 1 0 0 0 0 0 0\n")
        self.assertEqual(hostdata.parse_netdev(dev), {"eth0": (5000, 7000), "docker0": (9, 8)})   # no lo, veth, container bridges
        sess = hostdata.parse_sessions("c1 1000 alice - 123 user pts/0 yes 2h\n7 1000 alice - 5 user - no -\n",
                                     "0 0 192.168.0.10:22 192.168.0.5:50000\n0 0 192.168.0.10:22 203.0.113.9:40000\n0 0 [fd00::1]:22 [fd00::2]:1\n")
        self.assertEqual(sess["local"], [{"user": "alice", "tty": "pts/0"}, {"user": "alice", "tty": ""}])
        self.assertEqual(sess["ssh"], ["192.168.0.5", "203.0.113.9", "fd00::2"])
        self.assertTrue(exposure.is_private_addr("192.168.0.5") and exposure.is_private_addr("100.64.0.2"))   # LAN and Tailscale
        self.assertFalse(exposure.is_private_addr("203.0.113.9"))                      # documentation range: NOT local
        self.assertTrue(exposure.is_private_addr("::ffff:192.168.0.5") and exposure.is_private_addr("fd7a:115c:a1e0::1"))
        self.assertFalse(exposure.is_private_addr("2a0d:3341::1"))
        mounts = "/dev/nvme0n1p2 / ext4 rw 0 0\n/dev/nvme0n1p2 /var/lib/foo ext4 rw 0 0\n/dev/loop3 /snap/x squashfs ro 0 0\ntmpfs /run tmpfs rw 0 0\n/dev/nvme0n1p1 /boot/efi vfat rw 0 0\noverlay /var/lib/docker/overlay2/x overlay rw 0 0\n"
        self.assertEqual(hostdata.parse_mounts(mounts), [("/", "ext4"), ("/boot/efi", "vfat")])   # once per device, real ones only
        self.assertEqual(ui.fmt_rate(0), "0 B/s")
        self.assertEqual(ui.fmt_rate(12_300), "12.3 kB/s")
        self.assertEqual(ui.fmt_rate(4_500_000), "4.5 MB/s")
        sp = ansi.ANSI.sub("", ansi.sparkline([0, 100, 5000], 6))
        self.assertEqual((len(sp), sp[-1]), (6, "█"))                                # always 'width' columns, the last value is the highest
        peers = collector.parse_ts_peers(json.dumps({"Self": {"HostName": "nuc", "Online": True, "ExitNodeOption": True},
            "Peer": {"a": {"HostName": "pc", "OS": "windows", "Online": True, "CurAddr": "203.0.113.7:41641", "Relay": "fra", "LastSeen": "2026-09-30T10:00:00Z"},
                     "b": {"HostName": "old", "OS": "iOS", "Online": False, "LastSeen": "0001-01-01T00:00:00Z"}}}))
        self.assertEqual([(p["name"], p["online"], p["direct"], p["last_seen"] is None) for p in peers["peers"]],
                         [("pc", True, True, False), ("old", False, False, True)])       # online first, 'never seen' = None
        self.assertTrue(peers["self"]["exit_option"])
        df = collector.parse_docker_df('{"Type":"Images","TotalCount":"5","Active":"2","Size":"1GB","Reclaimable":"600MB (60%)"}\nnoise\n')
        self.assertEqual(df, [{"type": "Images", "count": "5", "active": "2", "size": "1GB", "reclaimable": "600MB (60%)"}])

    def test_new_blocks_content(self):
        now = time.time()
        s = {"net": {"eth0": {"rx": 12_300, "tx": 500, "rx_tot": 2**30, "tx_tot": 2**20, "hist_rx": [0, 5000, 12300], "hist_tx": [0, 0, 500]}},
             "sessions": {"local": [{"user": "u", "tty": "pts/0"}], "ssh": ["192.168.0.5", "203.0.113.9"]},
             "fs": [{"mount": "/", "used": 2**30, "total": 2**32}]}
        text = lambda lines: ansi.ANSI.sub("", "\n".join(lines))
        t = text(cardlines.ov_traffico(s, 78, 0))
        self.assertIn("eth0", t)
        self.assertIn("12.3 kB/s", t)
        self.assertIn("1.0G", t)
        for w in (78, 118):
            self.assertTrue(all(ansi.vlen(x) <= w for x in cardlines.ov_traffico(s, w, 0)), f"traffic too wide at {w}")
        ses = text(cardlines.ov_sessioni(s, 78, 0))
        self.assertIn("ssh from 203.0.113.9  address NOT local or Tailscale", ses)     # ssh from the Internet: highlighted
        self.assertIn("ssh from 192.168.0.5  LAN or Tailscale", ses)
        self.assertIn("pts/0", ses)
        red = "\n".join(cardlines.ov_sessioni(s, 78, 0))
        self.assertIn("\x1b[31m2 connected", red)                                       # with an external ssh the count is red
        self.assertIn("none", text(cardlines.ov_sessioni({"sessions": {"local": [], "ssh": []}}, 78, 0)))
        net = dict(NET, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": True},
                                  "peers": [{"name": "pc", "os": "windows", "online": True, "last_seen": None, "direct": True, "relay": "fra", "exit": False},
                                            {"name": "phone", "os": "android", "online": False, "last_seen": now - 7200, "direct": False, "relay": "", "exit": False},
                                            {"name": "new", "os": "linux", "online": False, "last_seen": None, "direct": False, "relay": "", "exit": True}]})
        ts = text(cardlines.ov_tailscale(net, 78, 0))
        self.assertIn("nuc · 1/3 nodes online · exit node", ts)
        self.assertIn("online direct", ts)
        self.assertIn("offline · seen 2 h ago", ts)
        self.assertIn("offline · never seen", ts)
        self.assertIn("unavailable", text(cardlines.ov_tailscale(NET, 78, 0)))              # old collector: says so
        boot = dict(BOOT, docker_df={"rows": [{"type": "Images", "count": "152", "active": "18", "size": "101.8GB", "reclaimable": "60GB (59%)"}],
                                     "volumes_unused": 151})
        dk = text(cardlines.ov_docker(boot, 90, 0))
        self.assertIn("unused 60GB (59%)", dk)
        self.assertIn("151 unused volumes", dk)
        self.assertIn("may hold data", dk)
        self.assertIn("unavailable", text(cardlines.ov_docker(BOOT, 90, 0)))
        self.assertIn("1.0G/4.0G", text(cardlines.ov_dischi(s, 78, 0)))

    def test_pack_keeps_the_requested_order(self):
        mk = lambda n, tag: (lambda cw: [f"{tag}{i}" for i in range(n)])
        out = render.pack([mk(3, "a"), mk(3, "b"), mk(2, "c")], 2, 10, 23, 6, [""])
        out = [ansi.ANSI.sub("", x) for x in out]                                  # columns() ends every line with a reset
        left = [l[:10].strip() for l in out]
        self.assertEqual(left[:4], ["a0", "a1", "a2", ""])                           # a on top; the small c does NOT back-fill the gap below
        right = [l[13:].strip() for l in out]
        self.assertEqual(right[:6], ["b0", "b1", "b2", "", "c0", "c1"])         # b did not fit below a: next column, then c after b
        self.assertIsNone(render.pack([mk(9, "x")], 3, 10, 36, 4, [""]))            # a block taller than the column: no room
        one = render.pack([mk(2, "a"), mk(2, "b")], 1, 10, 10, 10, [""])
        self.assertEqual(one, ["a0", "a1", "", "b0", "b1"])                         # one column: stacked with an empty line

    def test_three_columns_at_240_and_new_sections_only_when_room(self):
        smp = hostdata.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm.update(thermal={"cpu": (52.0, 105.0), "throttle_s": 1.0, "recent": 0},
                  net={"eth0": {"rx": 1, "tx": 1, "rx_tot": 5, "tx_tot": 5, "hist_rx": [1], "hist_tx": [1]}},
                  sessions={"local": [], "ssh": []}, fs=[{"mount": "/", "used": 1, "total": 4}])
        net = dict(NET, dbs={"since": time.time(), "items": []}, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": False}, "peers": []})
        big = render.page_overview(sm, CONT, net, dict(BOOT, docker_df={"rows": [], "volumes_unused": 0}), 239, 65)
        text = ansi.ANSI.sub("", "\n".join(big))
        for title in ("NETWORK TRAFFIC", "SESSIONS", "TAILSCALE", "DOCKER · DISK", "DISKS", "DATABASE", "EXPOSURE", "FIREWALL"):
            self.assertIn(title, text)
        # three columns: section titles appear at three distinct horizontal positions on the same line
        first = ansi.ANSI.sub("", big[0])
        self.assertGreaterEqual(first.count("──"), 3)
        self.assertTrue(all(ansi.vlen(x) <= 239 for x in big))
        self.assertLessEqual(len(big), 65)
        small = ansi.ANSI.sub("", "\n".join(render.page_overview(sm, CONT, net, BOOT, 119, 31)))
        self.assertNotIn("NETWORK TRAFFIC", small)                                   # small console: essentials only
        self.assertNotIn("DISKS", small)

    def test_review10_ts_peers_error_does_not_disable_port_alarms(self):
        keys = exposure.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items() if k != "9011/t:LAN"}}
        cosmetic = dict(NET, errors={"ts_peers": "tailscale failed", "f2b": "x", "dbs": "y"})
        self.assertFalse(exposure.exposure_partial(cosmetic))
        self.assertIn("9011/t:LAN", exposure.new_ports(cosmetic, CONT, base))              # the alarm stays on
        pb = problems.problems(cosmetic, CONT, baseline=base)
        self.assertTrue(any(sev == 3 and "NEW exposed port: 9011" in t for sev, t in pb))
        self.assertTrue(any("network sections not collected" in t for _, t in pb))        # but the error still shows
        critical = dict(NET, errors={"ufw": "boom"})
        self.assertTrue(exposure.exposure_partial(critical))
        self.assertEqual(exposure.new_ports(critical, CONT, base), {})                     # partial critical data: comparison suspended
        self.assertTrue(any("port comparison suspended" in t for _, t in problems.problems(critical, CONT, baseline=base)))
        import contextlib
        import io
        import tempfile
        d = tempfile.mkdtemp()
        net, state = os.path.join(d, "net.json"), os.path.join(d, "c.json")
        json.dump(dict(NET, ts=time.time(), errors={"ts_peers": "x"}), open(net, "w"))
        json.dump(dict(CONT, ts=time.time()), open(state, "w"))
        old = (hostdata.NET_STATE, hostdata.STATE)
        hostdata.NET_STATE, hostdata.STATE = net, state
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(problems.accept_baseline(path=os.path.join(d, "b.json")), 0)   # the baseline is created even with ts_peers broken
        finally:
            hostdata.NET_STATE, hostdata.STATE = old

    def test_review10_untrusted_docker_text_is_sanitised(self):
        boot = dict(BOOT, docker_df={"rows": [{"type": "Im\x1b[2Jages", "count": "1\x1b[2J", "active": "2\x1b[2J", "size": "3\x1b[2J",
                                                "reclaimable": "\x1bcx (60%)"}], "volumes_unused": 0})
        raw = "\n".join(cardlines.ov_docker(boot, 100, 0))
        self.assertNotIn("\x1b[2J", raw)
        self.assertNotIn("\x1bc", raw)

    @unittest.skipUnless(nuc_config.LINUX, "loginctl + ss: the Linux session source (macOS/Windows: tests/test_platforms.py)")
    def test_review10_sessions(self):
        sess = hostdata.parse_sessions("c1 1000 alice - 123 user pts/0 yes 2h\n7 1000 alice - 5 manager - no -\n", "")
        self.assertEqual(sess["local"], [{"user": "alice", "tty": "pts/0"}])           # 'manager' is not a session
        self.assertIn("1 user session ", ansi.ANSI.sub("", "\n".join(cardlines.ov_sessioni({"sessions": sess}, 78, 0))))

        class Boom:
            returncode = 1
            stdout = ""
        orig = render.subprocess.run
        render.subprocess.run = lambda *a, **k: Boom()
        try:
            with self.assertRaises(RuntimeError):                                          # failed command: never "none"
                hostdata.read_sessions()
        finally:
            render.subprocess.run = orig
        self.assertIn("unavailable", ansi.ANSI.sub("", "\n".join(cardlines.ov_sessioni({"sessions": None}, 78, 0))))

    def test_review10_cached_runs_in_background_and_survives_errors(self):
        import threading
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return {"ok": 1}
        t0 = time.monotonic()
        self.assertIsNone(hostdata.cached("test-slow", 60, slow))                         # does not wait for the slow read
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(started.wait(2))
        release.set()
        for _ in range(50):
            if hostdata.cached("test-slow", 60, slow) is not None:
                break
            time.sleep(0.05)
        self.assertEqual(hostdata.cached("test-slow", 60, slow), {"ok": 1})

        def bad():
            raise OSError("stuck mount")
        hostdata.cached("test-bad", 60, bad)
        time.sleep(0.2)
        self.assertIsNone(hostdata.cached("test-bad", 60, bad))                            # error: None, the thread does not die

    def test_review10_docker_df_and_freshness_and_cleanup(self):
        self.assertEqual(collector.parse_docker_df("5\n[1]\nnull\n"), [])              # valid JSON but not an object
        self.assertEqual(ui.fmt_ago(-30), "0 s")                                     # no "-30 s"
        old_net = dict(NET, ts=time.time() - 1000, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": False},
                                                              "peers": [{"name": f"p{i}", "os": "x", "online": False, "last_seen": None,
                                                                         "direct": False, "relay": "", "exit": False} for i in range(11)]})
        txt = ansi.ANSI.sub("", "\n".join(cardlines.ov_tailscale(old_net, 90, 0)))
        self.assertIn("stale data (", txt)
        self.assertIn("… +3 nodes", txt)                                                  # nodes past the eighth are counted
        stale_boot = dict(BOOT, ts=time.time() - 5000, docker_df={"rows": [], "volumes_unused": 0})
        self.assertIn("stale data (", ansi.ANSI.sub("", "\n".join(cardlines.ov_docker(stale_boot, 90, 0))))
        smp = hostdata.Sampler()
        smp.net_hist["gone0"] = (collections.deque([1]), collections.deque([1]))
        smp.sample()
        self.assertNotIn("gone0", smp.net_hist)                                          # interface gone: history dropped
        self.assertEqual(hostdata.parse_netdev("h1\nh2\n  eth0: x y z\n"), {})            # line with too few fields: ignored

    @unittest.skipUnless(nuc_config.LINUX, "the Linux tool set; macOS/Windows read sockets natively (tests/test_platforms.py)")
    def test_portability_missing_tools_are_absent_not_errors(self):
        """On a server without Docker, ufw, Tailscale, fail2ban, systemd...: no errors, no crashes, no alarms."""
        orig = collector.SBIN
        collector.SBIN = "/path/that/does/not/exist"                                  # no binary can be found
        try:
            with self.assertRaises(collector.Absent):
                collector.run("docker", "ps")
            self.assertEqual(collector.collect()["containers"], [])
            self.assertTrue(collector.collect().get("absent"))                          # docker missing: marked, not an error
            self.assertIsNone(collector.container_conns(1))                             # nsenter missing: no data, no crash
            net = collector.collect_net()
            boot = collector.collect_boot()
        finally:
            collector.SBIN = orig
        self.assertEqual(net["errors"], {})                                             # NO errors
        self.assertEqual(boot["errors"], {})
        for k in ("listeners", "serve", "ufw", "docker_user", "iptables", "f2b", "drops", "dbs", "ts_peers"):
            self.assertIn(k, net["absent"], k)
        for k in ("analyze", "blame", "failed", "enabled", "journal", "containers", "docker_df"):
            self.assertIn(k, boot["absent"], k)

    def test_portability_renderer_with_absent_tools(self):
        net = {"ts": time.time(), "errors": {}, "absent": ["ufw", "iptables", "f2b", "drops", "docker_user", "dbs", "ts_peers"],
               "listeners": [{"proto": "tcp", "addr": "0.0.0.0", "port": 22, "proc": "sshd"},
                             {"proto": "tcp", "addr": "0.0.0.0", "port": 9011, "proc": "java"}],
               "serve": [], "docker_user": None, "ufw": None}
        boot = {"ts": time.time(), "errors": {}, "absent": ["analyze", "blame", "failed", "enabled", "journal", "containers", "docker_df"],
                "kernel": "6.1", "btime": int(time.time()) - 100}
        cont = {"ts": time.time(), "containers": [], "absent": True}
        text = lambda lines: ansi.ANSI.sub("", "\n".join(lines))
        pb = problems.problems(net, cont, boot=boot)
        self.assertTrue(any(sev == 1 and "ufw not installed" in t for sev, t in pb))    # warning, not error
        self.assertFalse(any(sev >= 2 for sev, _ in pb), "no error/alarm just because tools are missing")
        # but with no known firewall a listening database is still flagged (fail-open): absence does not reassure
        risky = dict(net, listeners=net["listeners"] + [{"proto": "tcp", "addr": "0.0.0.0", "port": 5432, "proc": "postgres"}])
        self.assertTrue(any(sev == 2 and "DB/broker open on LAN" in t for sev, t in problems.problems(risky, cont, boot=boot)))
        self.assertNotIn("network sections not collected", text([t for _, t in pb]))
        fw = text(render.firewall_block(net, 100))
        self.assertIn("ufw not installed", fw)
        self.assertRegex(fw, r"iptables\s+not installed")
        self.assertNotIn("unreadable", fw)
        self.assertIn("docker not installed", text(cardlines.ov_container(cont, 90, 0)))
        self.assertIn("docker not installed", text(render.containers_block(cont, 90)))
        self.assertIn("not installed", text(cardlines.ov_database(net, cont, 90, 0)))
        self.assertIn("not installed", text(cardlines.ov_tailscale(net, 90, 0)))
        self.assertIn("not installed", text(cardlines.ov_docker(boot, 90, 0)))
        for blk in (render.boot_block_lente(boot, 90, 3), render.boot_block_fallite(boot, 90), render.boot_block_servizi(boot, 90, 2),
                    render.boot_block_container(boot, 90, 2, time.time()), render.boot_block_journal(boot, 90, 3),
                    render.boot_block_avvio(boot, 100.0, 90)):
            self.assertIn("not installed", text(blk))
        # a real fault stays an error, clearly distinct from "not installed"
        broken = dict(net, ufw=None, absent=[], errors={"ufw": "boom"})
        self.assertTrue(any(sev == 2 and "ufw unreadable" in t for sev, t in problems.problems(broken, cont, boot=boot)))
        # and the whole single screen draws without exceptions
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}}
        lines = render.page_overview(sm, cont, net, boot, 200, 60)
        self.assertGreater(len(lines), 10)
        self.assertNotIn("Traceback", text(lines))

    def test_first_slide_of(self):
        sl = [("System", 1, 2, []), ("System", 2, 2, []), (render.PAGES[1], 1, 1, [])]
        self.assertEqual(render.first_slide_of(sl, 1), 2)



class Config(unittest.TestCase):
    def _load(self, text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8") as f:  # Windows would write cp1252
            f.write(text)
        try:
            return nuc_config.load(f.name)
        finally:
            os.unlink(f.name)

    def test_missing_file_means_everything_on(self):
        cfg = nuc_config.load("/nonexistent/config.ini")
        self.assertTrue(all(cfg["features"].values()))
        self.assertEqual(set(cfg["features"]), set(nuc_config.FEATURES))
        self.assertEqual((cfg["mode"], cfg["rotate_seconds"]), ("overview", 15))

    def test_switch_off_and_bad_values_fall_back_safely(self):
        cfg = self._load("[features]\nfirewall = no\nthermal = off\nboot = banana\nnope = yes\n"
                         "[dashboard]\nmode = weird\nrotate_seconds = 9999\n")
        self.assertFalse(cfg["features"]["firewall"])
        self.assertFalse(cfg["features"]["thermal"])
        self.assertTrue(cfg["features"]["boot"])      # not a boolean: stays on
        self.assertNotIn("nope", cfg["features"])
        self.assertEqual((cfg["mode"], cfg["rotate_seconds"]), ("overview", 600))

    def test_shipped_config_really_switches_off_with_inline_comments(self):
        text = open(os.path.join(os.path.dirname(__file__), "..", "config", "config.ini")).read()
        for f in nuc_config.FEATURES:
            import re as _re
            flipped = _re.sub(r"(?m)^(%s\s*=\s*)yes" % f, r"\1no", text)
            self.assertNotEqual(flipped, text, f)
            self.assertFalse(self._load(flipped)["features"][f], f)

    def test_size_override_and_clamp(self):
        cfg = self._load("[dashboard]\ncolumns = 235\nrows = 3\n")
        self.assertEqual((cfg["columns"], cfg["rows"]), (235, 10))
        self.assertEqual(self._load("[dashboard]\ncolumns = 0\n")["columns"], 0)

    def test_funnel_follows_exposure_not_tailscale(self):
        self.assertEqual(collector.NET_FEATURE["serve"], "exposure")

    def test_garbage_file_does_not_crash(self):
        self.assertTrue(all(self._load("this is not ini\n\x00")["features"].values()))

    def _load_err(self, text):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = self._load(text)
        return cfg, err.getvalue()

    def test_expose_defaults_to_nothing(self):
        self.assertEqual(nuc_config.load("/nonexistent/config.ini")["expose"], {})   # missing file: the early return has it too
        self.assertEqual(self._load("[features]\nmap = no\n")["expose"], {})

    def test_expose_values_synonyms_and_case(self):
        cfg, err = self._load_err("[expose]\nShop-DB = LOCAL\nn8n = Tailnet\nts = tailscale\nlo = localhost\nlo2 = loopback\n"
                                  "web = lan\nopen = Internet\npub = public\n8080 = lan\n53/udp = LAN\n")
        self.assertEqual(cfg["expose"], {"shop-db": "LOCALE", "n8n": "TAILNET", "ts": "TAILNET", "lo": "LOCALE", "lo2": "LOCALE",
                                         "web": "LAN", "open": "INTERNET", "pub": "INTERNET", "8080": "LAN", "53/udp": "LAN"})
        self.assertEqual(err, "")
        self.assertTrue(set(cfg["expose"].values()) <= {g for g, _ in exposure.GROUPS})      # render's group names

    def test_expose_bad_value_or_port_skips_only_that_key(self):
        cfg, err = self._load_err("[expose]\nok = local\nbad = everywhere\nempty =\n0 = lan\n70000 = lan\n8080/sctp = lan\n"
                                  "99999/udp = lan\n443 = tailnet\n2fauth = lan\n")
        self.assertEqual(cfg["expose"], {"ok": "LOCALE", "443": "TAILNET", "2fauth": "LAN"})   # '2fauth' is a name, not a port
        for key in ("bad", "empty", "0", "70000", "8080/sctp", "99999/udp"):
            self.assertIn(f"[expose] {key}:", err)
        self.assertEqual(err.count("nuc-console: "), 6)
        self.assertNotIn("[expose] ok:", err)

    def test_expose_key_starting_with_colon_makes_the_whole_file_fall_back(self):
        cfg, err = self._load_err("[features]\nmap = no\n[expose]\n:8080 = lan\n")   # configparser refuses it: defaults, said on stderr
        self.assertEqual(cfg["expose"], {})
        self.assertTrue(cfg["features"]["map"])
        self.assertIn("cannot read", err)

    def test_expose_duplicate_key_after_lowercasing_does_not_drop_the_file(self):
        cfg, err = self._load_err("[features]\nmap = no\n[expose]\nShop-DB = local\nshop-db = lan\n")   # last wins, the rest of the file is kept
        self.assertEqual(cfg["expose"], {"shop-db": "LAN"})
        self.assertFalse(cfg["features"]["map"])
        self.assertEqual((cfg["config_error"], err), ("", ""))

    def test_a_file_that_cannot_be_read_is_flagged(self):
        self.assertEqual(self._load("[features]\nmap = no\n")["config_error"], "")
        self.assertEqual(nuc_config.load("/nonexistent/config.ini")["config_error"], "")           # no file is not an error
        cfg, _ = self._load_err("[expose]\n:8080 = lan\n")
        self.assertIn(":8080", cfg["config_error"])
        self.assertLessEqual(len(cfg["config_error"]), 200)
        cfg, _ = self._load_err("this is not ini\n")
        self.assertTrue(cfg["config_error"])
        import tempfile
        with tempfile.TemporaryDirectory() as d:                                                   # exists, cannot be opened as a file
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                cfg = nuc_config.load(d)
            self.assertTrue(cfg["config_error"])
            self.assertEqual(cfg["expose"], {})
            bad = os.path.join(d, "config.ini")
            with open(bad, "wb") as f:
                f.write(b"[expose]\nshop = lan\n\xff\xfe = lan\n")                                  # not UTF-8
            with contextlib.redirect_stderr(io.StringIO()):
                cfg = nuc_config.load(bad)
            self.assertTrue(cfg["config_error"])

    def test_expose_keys_of_the_default_section_are_not_services(self):
        cfg, _ = self._load_err("[DEFAULT]\nshop-db = lan\n8080 = lan\n[expose]\nn8n = local\n[webapps]\nblog = 8081\n")
        self.assertEqual(cfg["expose"], {"n8n": "LOCALE"})

    def test_expose_port_keys(self):
        self.assertEqual(nuc_config.expose_port("8080"), (8080, "tcp"))
        self.assertEqual(nuc_config.expose_port("8080/udp"), (8080, "udp"))
        self.assertEqual(nuc_config.expose_port("8080/tcp"), (8080, "tcp"))
        self.assertIsNone(nuc_config.expose_port("shop-db"))
        self.assertIsNone(nuc_config.expose_port("3proxy"))
        self.assertIsNone(nuc_config.expose_port("n8n"))
        for bad in ("0", "65536", "8080/x", "8080/udp/x"):
            self.assertRaises(ValueError, nuc_config.expose_port, bad)

    def test_configuration_reference_documents_every_key(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "docs", "CONFIGURATION.md"), encoding="utf-8") as f:
            doc = f.read()
        keys = list(nuc_config.FEATURES) + ["mode", "sections", "columns", "rows", "spacing", "details", "overview_seconds",
                                            "rotate_seconds", "enabled", "bind", "port", "token_file", "allowed_hosts", "refresh_seconds",
                                            "browser", "zoom", "username", "detail", "resolved"]
        self.assertEqual([k for k in keys if "`%s`" % k not in doc], [])
        for name in nuc_config.SECTIONS:
            self.assertIn(name, doc)

    def test_expose_is_documented_and_the_shipped_example_parses(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "docs", "CONFIGURATION.md"), encoding="utf-8") as f:
            self.assertIn("## `[expose]`", f.read())
        with open(os.path.join(root, "docs", "DESIGN.md"), encoding="utf-8") as f:
            self.assertNotIn("Expected vs actual", f.read().split("## Not yet applied")[1].split("\n## ")[0])   # applied: it has its own section
        with open(os.path.join(root, "config", "config.ini"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("[expose]", text)
        self.assertEqual(self._load(text)["expose"], {})                                       # shipped as a commented example
        example = re.sub(r"(?m)^# (shop-db|n8n|8080)(\s*=)", r"\1\2", text)
        self.assertNotEqual(example, text)
        self.assertEqual(self._load(example)["expose"], {"shop-db": "LOCALE", "n8n": "TAILNET", "8080": "LAN"})

    def test_shipped_config_parses_and_lists_every_feature(self):
        path = os.path.join(os.path.dirname(__file__), "..", "config", "config.ini")
        cfg = nuc_config.load(path)
        self.assertTrue(all(cfg["features"].values()))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("[display]", text)
        for f in nuc_config.FEATURES:
            self.assertIn(f + " ", text, f"feature {f} undocumented in config/config.ini")

    def test_demo_renders_at_every_width_without_errors(self):
        import demo
        cont, net, boot, base = demo.snapshot()
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": {"local": [], "ssh": []}, "fs": []}
        for w in (79, 119, 199, 225):
            body = ANSI_RE.sub("", "\n".join("\n".join(x[3]) for x in render.slides(sm, cont, net, w, 40, boot, base, mode="overview")))
            self.assertNotIn("KeyError", body)
            self.assertNotIn("error on page", body)
            self.assertIn("EXPOSURE", body)
        self.assertFalse([t for _, t in problems.problems(net, cont, boot=boot, baseline=base) if "KeyError" in t])

    def test_collector_skips_disabled_sections(self):
        calls = []
        orig_run, orig_off = collector.run, collector.OFF
        collector.run = lambda name, *a, **k: calls.append(name) or (0, "", "")
        collector.OFF = {"firewall", "fail2ban", "tailscale", "exposure", "databases", "map"}
        try:
            d = collector.collect_net()
        finally:
            collector.run, collector.OFF = orig_run, orig_off
        for tool in ("ufw", "iptables", "tailscale", "ss", "docker", "journalctl", "powershell", "socketfilterfw", "lsof"):
            self.assertNotIn(tool, calls)
        expected = {"listeners", "serve", "ts_peers", "ufw", "docker_user", "iptables", "drops", "dbs", "f2b", "links"}
        self.assertEqual(set(d["disabled"]), expected if nuc_config.LINUX else expected | {"firewall"})
        self.assertEqual(d["errors"], {})

    def test_collector_containers_off_never_calls_docker(self):
        orig_run, orig_off = collector.run, collector.OFF
        collector.run = lambda *a, **k: self.fail("docker must not run")
        collector.OFF = {"containers"}
        try:
            self.assertTrue(collector.collect()["disabled"])
        finally:
            collector.run, collector.OFF = orig_run, orig_off

    def test_renderer_hides_disabled_blocks_and_raises_no_alarm(self):
        orig = render.CFG["features"].copy()
        net = {"ts": time.time(), "errors": {}, "absent": ["ufw"], "disabled": ["ufw"], "listeners": []}
        try:
            render.CFG["features"].update(firewall=False, boot=False, thermal=False, disks=False, containers=False)
            pb = problems.problems(net, None, boot=False)
            self.assertFalse([t for _, t in pb if "ufw" in t or "container" in t], pb)
            body = "\n".join("\n".join(x[3]) for x in render.slides(
                {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": None, "fs": None}, None, net, 120, 40, None, False, mode="overview"))
            for title in ("FIREWALL", "CONTAINER", "BOOT", "DISKS"):
                self.assertNotIn(title, ANSI_RE.sub("", body))
            self.assertIn("EXPOSURE", ANSI_RE.sub("", body))
        finally:
            render.CFG["features"].clear()
            render.CFG["features"].update(orig)



class SectionOrder(unittest.TestCase):
    def _cfg(self, text):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write(text)
        try:
            return nuc_config.load(f.name)
        finally:
            os.unlink(f.name)

    def test_default_order_and_overrides(self):
        self.assertEqual(nuc_config.load("/nonexistent")["sections"], list(nuc_config.SECTIONS))
        cfg = self._cfg("[dashboard]\nsections = system, bogus, exposure, system\n")
        self.assertEqual(cfg["sections"][:2], ["system", "exposure"])            # asked first, duplicates and unknown dropped
        self.assertEqual(sorted(cfg["sections"]), sorted(nuc_config.SECTIONS))   # forgotten ones are appended, none lost

    def test_overview_follows_the_configured_order(self):
        import demo
        cont, net, boot, base = demo.snapshot()
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": {"local": [], "ssh": []}, "fs": []}
        saved = list(render.CFG["sections"])
        try:
            for order in (["attention", "exposure", "firewall", "system", "containers"],
                          ["system", "containers", "firewall", "exposure", "attention"]):
                render.CFG["sections"] = order + [x for x in nuc_config.SECTIONS if x not in order]
                txt = ansi.ANSI.sub("", "\n".join("\n".join(x[3]) for x in render.slides(sm, cont, net, 118, 60, boot, base, mode="overview")))
                pos = [txt.index(t) for t in ("ATTENTION", "EXPOSURE", "FIREWALL", "SYSTEM", "CONTAINER")]
                names = ["attention", "exposure", "firewall", "system", "containers"]
                self.assertEqual([names[i] for i in sorted(range(5), key=lambda i: pos[i])], order)
        finally:
            render.CFG["sections"] = saved

    def test_per_core_bars_always_exploded_on_normal_levels(self):
        sm = {"cpu": {f"cpu{i}": 0.3 for i in range(14)}, "thermal": {}, "net": {}, "sessions": None, "fs": None}
        for k in (-2, -1, 0, 1, 2):   # exploded at every normal level
            self.assertTrue(any(" 13 " in ansi.ANSI.sub("", l) for l in cardlines.ov_sistema(sm, 76, k)), k)
        tiny = "\n".join(ansi.ANSI.sub("", l) for l in cardlines.ov_sistema(sm, 76, 3))
        self.assertNotIn(" 13 ", tiny)                                                # only the tiny-console levels compress
        self.assertIn("core ", tiny)


class DetailPages(unittest.TestCase):
    """What the overview cuts ("… +N more") is shown in full on rotating detail pages: that monitor has no keyboard."""

    def setUp(self):
        self.saved = (dict(render.CFG["webapps"]), render.CFG["details"])
        render.CFG["details"] = True
        render.CFG["webapps"] = {"app%02d" % i: [9000 + i] for i in range(20)}
        self.sm = {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": {"local": [], "ssh": []}, "fs": []}

    def tearDown(self):
        render.CFG["webapps"], render.CFG["details"] = self.saved

    def _slides(self, w, h):
        return render.slides(self.sm, CONT, NET, w, h - 2, BOOT, False, mode="overview")

    def test_truncated_sections_get_detail_pages_with_everything(self):
        sl = self._slides(118, 33)
        names = [x[0] for x in sl]
        self.assertEqual(names[0], "Overview")
        self.assertIn("Details", names)
        overview = ansi.ANSI.sub("", "\n".join(sl[0][3]))
        details = ansi.ANSI.sub("", "\n".join("\n".join(x[3]) for x in sl if x[0] == "Details"))
        self.assertIn("more", overview)
        self.assertNotIn("app19", overview)
        for i in range(20):
            self.assertIn("app%02d" % i, details)
        self.assertNotIn("… +", details)                                   # nothing is hidden on the detail pages
        for x in sl:
            self.assertLessEqual(len(x[3]), 31)                             # every page fits the screen

    def test_no_detail_pages_when_nothing_is_cut_or_when_switched_off(self):
        render.CFG["webapps"] = {}
        self.assertEqual([x[0] for x in render.slides(self.sm, {"ts": time.time(), "containers": []}, dict(NET, listeners=[]), 226, 60, None, False, mode="overview")].count("Details"), 0)
        render.CFG["webapps"] = {"app%02d" % i: [9000 + i] for i in range(20)}
        render.CFG["details"] = False
        self.assertNotIn("Details", [x[0] for x in self._slides(118, 33)])

    def test_the_details_pages_only_hold_what_the_final_level_cut(self):
        def details(n):
            render.CFG["webapps"] = {"app%03d" % i: [9000 + i] for i in range(n)}
            pages = []
            render.page_overview(self.sm, CONT, NET, BOOT, 226, 60, details=pages)
            return ansi.ANSI.sub("", "\n".join("\n".join(p) for p in pages))
        self.assertNotIn("app001", details(2))                              # 2 declared apps plus the ones found fit the 12-row cap: nothing to detail for them
        self.assertIn("app079", details(80))                                # 80 apps cannot fit: the Details pages will show them

    def test_free_space_lifts_the_caps_before_anything_goes_to_details(self):
        render.CFG["webapps"] = {"app%02d" % i: [9000 + i] for i in range(15)}       # over the 12-row cap, but there is room at 226x60
        sl = render.slides(self.sm, CONT, NET, 226, 58, BOOT, False, mode="overview")
        txt = ansi.ANSI.sub("", "\n".join(sl[0][3]))
        self.assertTrue(all("app%02d" % i in txt for i in range(15)), "caps must lift when the layout still fits")
        self.assertEqual([x[0] for x in sl].count("Details"), 0)

    def test_rotation_gives_the_overview_longer_and_cycles(self):
        sl = [("Overview", 1, 1, []), ("Details", 1, 2, []), ("Details", 2, 2, [])]
        o, r = render.CFG["overview_seconds"], render.ROTATE_S
        self.assertEqual([render.pick_slide(sl, t) for t in (0, o - 1, o, o + r - 1, o + r, o + 2 * r - 1, o + 2 * r)], [0, 0, 1, 1, 2, 2, 0])
        one = [("Overview", 1, 1, [])]
        self.assertEqual(render.pick_slide(one, 12345), 0)

    def test_config_details_and_overview_seconds(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[dashboard]\ndetails = no\noverview_seconds = 5\n")
        try:
            cfg = nuc_config.load(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual((cfg["details"], cfg["overview_seconds"]), (False, 10))   # clamped to 10-600
        self.assertEqual((nuc_config.load("/nonexistent")["details"], nuc_config.load("/nonexistent")["overview_seconds"]), (True, 45))


class WebAppsAndProblems(unittest.TestCase):
    def setUp(self):
        self.saved = dict(render.CFG["webapps"])
        self.acc = problems.ACCEPTED_PATH

    def tearDown(self):
        render.CFG["webapps"] = self.saved
        problems.ACCEPTED_PATH = self.acc

    def test_declared_up_declared_down_and_discovered(self):
        render.CFG["webapps"] = {"shop": [8080], "admin": [9443]}
        rows = {r["name"]: r for r in exposure.webapp_rows(NET, CONT)}
        self.assertEqual((rows["shop"]["state"], rows["shop"]["expected"]), ("up", True))
        self.assertEqual((rows["admin"]["state"], rows["admin"]["reach"]), ("down", None))   # expected but nothing listens
        self.assertIn("LAN", {r["reach"] for r in rows.values() if r["state"] == "up"})
        self.assertFalse([r for r in rows.values() if r["ports"] and set(r["ports"]) & exposure.SENSITIVE])  # databases are not web apps
        txt = "\n".join(ansi.ANSI.sub("", l) for l in cardlines.ov_webapp(NET, CONT, 80, 0))
        self.assertIn("DOWN (expected)", txt)
        self.assertIn("1 down (expected)", txt)

    def test_declared_webapp_is_not_a_docker_bypass_problem(self):
        pb = lambda: [t for _, t, pid in problems.problems_raw(NET, CONT) if pid == "docker-bypass"]
        render.CFG["webapps"] = {}
        before = pb()
        self.assertTrue(before, "fixture must have a docker bypass")
        render.CFG["webapps"] = {"everything": [r["port"] for r in exposure.exposure_rows(NET, CONT)]}
        left = pb()
        self.assertEqual(len(left), 1)                       # only the database port 5432 still counts: a declaration cannot mask a DB
        self.assertIn("1 Docker port", left[0])

    def test_every_problem_id_is_in_the_catalog(self):
        import re as _re
        with open(problems.__file__, encoding="utf-8") as f:
            src = f.read()
        ids = set(_re.findall(r'out\.append\(\(\d, .*?, "([a-z-]+)"\)\)', src))
        self.assertTrue(ids)
        self.assertFalse(ids - set(problems.CATALOG), ids - set(problems.CATALOG))
        for pid, (title, why, fix) in problems.CATALOG.items():
            self.assertTrue(title and fix, pid)

    def _boot(self, n):
        return dict(BOOT, journal={"err": n, "warn": 1, "capped": False, "top": []})

    def test_accept_hides_counts_and_shows_the_count(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            problems.ACCEPTED_PATH = os.path.join(d, "accepted.json")
            recs = lambda n: problems.problem_records(NET, CONT, boot=self._boot(n))
            self.assertEqual(render.accept_problem("journal-errors", "", records=recs(118)), 2)        # reason required
            self.assertEqual(render.accept_problem("nope", "x", records=recs(118)), 2)                  # unknown id
            self.assertEqual(render.accept_problem("db-open-lan", "x", records=[]), 2)                  # not a current problem
            texts = lambda n: [t for _, t in problems.problems(NET, CONT, boot=self._boot(n))]
            self.assertTrue(any("118 errors" in t for t in texts(118)))
            self.assertEqual(render.accept_problem("journal-errors", "docker veth noise\x1b[2J", records=recs(118)), 0)
            self.assertFalse(any("errors in this boot" in t for t in texts(118)))
            self.assertFalse(any("errors in this boot" in t for t in texts(120)))                      # noisy counter: digits ignored
            pb = problems.problems(NET, CONT, boot=self._boot(118))
            self.assertEqual(pb.accepted, 1)
            shown = "\n".join(ansi.ANSI.sub("", l) for l in render.page_overview(
                {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": None, "fs": None}, CONT, NET, self._boot(118), 118, 40, pb=pb))
            self.assertIn("1 accepted as known", shown)                                                # never silent
            rec = [r for r in recs(118) if r["id"] == "journal-errors"][0]
            self.assertTrue(rec["accepted"] and "\x1b" not in rec["reason"] and rec["fix"])
            self.assertEqual(render.accept_problem("journal-errors", forget=True), 0)
            self.assertTrue(any("errors in this boot" in t for t in texts(118)))
            if os.name == "posix":  # Windows has no mode bits: the folder ACL protects the file
                self.assertEqual(os.stat(problems.ACCEPTED_PATH).st_mode & 0o777, 0o644)

    def test_acceptance_does_not_hide_a_worse_situation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            problems.ACCEPTED_PATH = os.path.join(d, "accepted.json")
            render.CFG["webapps"] = {}
            recs = problems.problem_records(NET, CONT)
            self.assertEqual(render.accept_problem("docker-bypass", "intended", records=recs), 0)
            self.assertFalse([1 for _, t, pid in problems.problems_raw(NET, CONT) if pid == "docker-bypass" and t in [x for _, x in problems.problems(NET, CONT)]])
            worse = dict(NET, listeners=NET["listeners"] + [{"proto": "tcp", "addr": "0.0.0.0", "port": 7778, "proc": "docker-proxy"}])
            cont2 = dict(CONT, containers=CONT["containers"] + [{"name": "x-1", "status": "Up", "state": "running", "project": "",
                                                              "ports": [{"p": 7778, "s": "*"}], "mem": 1}])
            self.assertTrue(any("Docker port" in t for _, t in problems.problems(worse, cont2)),
                            "one more bypassing port must be a new, visible problem")
            # thermal: accepting a warning must not hide a critical temperature
            warn = problems.problem_records(NET, CONT, thermal={"cpu": (88.0, 105.0), "throttle": None})
            self.assertEqual(render.accept_problem("thermal", "hot room", records=warn), 0)
            crit = problems.problems(NET, CONT, thermal={"cpu": (100.0, 105.0), "throttle": None})
            self.assertTrue(any(sev == 2 for sev, t in crit if "CPU at" in t))

    def test_port_changes_cannot_be_accepted_as_problems(self):
        self.assertEqual(render.accept_problem("port-new", "x", records=[{"id": "port-new", "fingerprint": "3|x"}]), 2)

    def test_broken_or_hostile_accepted_files_hide_nothing_and_never_crash(self):
        import tempfile
        for payload in ("{not json", "[" * 100000, '[1,2]', '{"journal-errors": {"reason": 5, "fp": "1|x"}}',
                        '{"journal-errors": {"reason": "ok"}}', '{"journal-errors": "x"}', '{"port-new": {"reason": "a", "fp": "3|x"}}'):
            with tempfile.NamedTemporaryFile("w", delete=False) as f:
                f.write(payload)
            try:
                self.assertEqual(problems.load_accepted(f.name), {}, payload[:30])
            finally:
                os.unlink(f.name)

    def test_problems_command_prints_advice_and_json(self):
        import contextlib
        import io
        import json as _json
        import tempfile
        problems.ACCEPTED_PATH = os.path.join(tempfile.mkdtemp(), "accepted.json")
        orig = render.current_problem_records
        render.current_problem_records = lambda: problems.problem_records(NET, CONT, boot=self._boot(118))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(render.print_problems([]), 0)
            txt = out.getvalue()
            self.assertIn("journal-errors", txt)
            self.assertIn("fix:", txt)
            self.assertIn("accept if known", txt)
            self.assertIn("port changes are accepted with the baseline", ansi.ANSI.sub("", "port changes are accepted with the baseline")) 
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                render.print_problems(["--json"])
            data = _json.loads(out.getvalue())
            self.assertTrue(all({"id", "severity", "text", "accepted", "fix", "fingerprint"} <= set(r) for r in data))
        finally:
            render.current_problem_records = orig

    def test_main_parses_accept_problem_and_forget(self):
        calls = []
        orig = render.accept_problem
        render.accept_problem = lambda pid, reason="", forget=False, **kw: calls.append((pid, reason, forget)) or 0
        try:
            render.main(["render.py", "--accept", "--problem", "docker-bypass", "--reason", "because"])
            render.main(["render.py", "--accept", "--forget", "docker-bypass"])
            render.main(["render.py", "--accept", "--problem", "--reason", "x"])
        finally:
            render.accept_problem = orig
        self.assertEqual(calls[0], ("docker-bypass", "because", False))
        self.assertEqual(calls[1], ("docker-bypass", "", True))
        self.assertEqual(calls[2][0], "--reason")        # malformed: the id is refused later as unknown

    def test_declared_database_port_is_not_masked_and_exposure_panel_agrees(self):
        render.CFG["webapps"] = {"db": [5432], "shop": [8080]}
        rows = exposure.exposure_rows(NET, CONT)
        bypass = [r for r in rows if r["bad_note"] and r["note"].startswith("docker") and r["lan"] == 1]
        pb = [t for _, t, pid in problems.problems_raw(NET, CONT) if pid == "docker-bypass"]
        self.assertTrue(pb, "the database port 5432 must still count")
        panel = "\n".join(ansi.ANSI.sub("", l) for l in render.exposure_block(NET, CONT, 100))
        self.assertIn("declared:", panel)                                                          # 8080 is labelled, not alarmed

    def test_docker_df_parsers(self):
        self.assertEqual(collector.parse_size("1.68GB"), 1_680_000_000)
        self.assertEqual(collector.parse_size("0B"), 0)
        self.assertEqual(collector.parse_size("garbage"), 0)
        self.assertTrue(collector.ANON_VOLUME.match("a" * 64))
        self.assertFalse(collector.ANON_VOLUME.match("my_named_volume"))

    def test_spacing_adds_air_only_on_spaced_levels(self):
        blocks = [lambda cw: ["TITLE", "a", "b"]]
        saved = render.CFG["spacing"]
        try:
            render.CFG["spacing"] = 1
            self.assertEqual([ansi.ANSI.sub("", x).strip() for x in render.pack(blocks, 1, 10, 10, 20, [""])], ["TITLE", "", "a", "b"])
            self.assertEqual(render.pack(blocks, 1, 10, 10, 20, []), ["TITLE", "a", "b"])   # unspaced (tiny) levels stay compact
            render.CFG["spacing"] = 0
            self.assertEqual(render.pack(blocks, 1, 10, 10, 20, [""]), ["TITLE", "a", "b"])
        finally:
            render.CFG["spacing"] = saved

    def test_sections_stay_even_when_empty(self):
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": {"local": [], "ssh": []}, "fs": []}
        net = {"ts": time.time(), "errors": {}, "absent": [], "listeners": [], "ufw": None, "docker_user": [], "dbs": {"items": []}}
        txt = ansi.ANSI.sub("", "\n".join("\n".join(x[3]) for x in render.slides(sm, {"ts": time.time(), "containers": []}, net, 226, 60, None, False, mode="overview")))
        for title in ("WEB APPS", "SESSIONS", "NETWORK TRAFFIC", "DISKS", "DATABASE", "CONTAINER", "TAILSCALE", "DOCKER"):
            self.assertIn(title, txt)


class ExposeVsDeclared(unittest.TestCase):
    """[expose]: the widest reach you intend, against the one the dashboard computes (ATTENTION, EXPOSURE matrix)."""

    @staticmethod
    def _ct(name, project, ports=(), state="running"):
        return {"name": name, "status": "Up 3 days" if state == "running" else "Exited (0) 2 hours ago", "state": state,
                "project": project, "ports": list(ports), "mem": 1}

    @staticmethod
    def _ls(addr, port, proc, unit=None, proto="tcp"):
        return dict({"proto": proto, "addr": addr, "port": port, "proc": proc}, **({"unit": unit} if unit else {}))

    def setUp(self):
        self.saved = (render.CFG["expose"], dict(render.CFG["webapps"]), problems.ACCEPTED_PATH)
        render.CFG["expose"], render.CFG["webapps"] = {}, {}
        ct, ls = self._ct, self._ls
        self.cont = {"ts": time.time(), "containers": [
            ct("shop-db-1", "shop", [{"p": 5432, "s": "*"}]), ct("shop-web-1", "shop", [{"p": 8080, "s": "*"}]),
            ct("blog-db-1", "blog", [{"p": 5433, "s": "lo"}]), ct("old-job-1", "shop", state="exited")]}
        self.net = dict(NET, listeners=[
            ls("0.0.0.0", 22, "sshd", "ssh.service"), ls("0.0.0.0", 5432, "docker-proxy"), ls("0.0.0.0", 8080, "docker-proxy"),
            ls("127.0.0.1", 5433, "docker-proxy"), ls("127.0.0.1", 5678, "node", "n8n.service"), ls("127.0.0.1", 3000, "node", "other.service"),
            ls("100.64.0.1", 8444, "tailscaled", "tailscaled.service")],
            links={"containers": [{"name": "shop-db-1", "project": "shop", "service": "db"},
                                  {"name": "shop-web-1", "project": "shop", "service": "web"},
                                  {"name": "blog-db-1", "project": "blog", "service": "db"}]},
            dbs={"items": [{"name": "shop-db-1", "kind": "postgres", "project": "shop"},
                           {"name": "blog-db-1", "kind": "postgres", "project": "blog"}]})

    def tearDown(self):
        render.CFG["expose"], render.CFG["webapps"], problems.ACCEPTED_PATH = self.saved

    def rows(self, expose, net=None, cont=None):
        render.CFG["expose"] = expose
        return {(r["port"], r["proto"]): r for r in exposure.expose_apply(exposure.exposure_rows(net or self.net, cont or self.cont), net or self.net,
                                                                       cont or self.cont)}

    def problem(self, pid, net=None, cont=None):
        return [t for _, t, i in problems.problems_raw(net or self.net, cont or self.cont, boot={}) if i == pid]

    def test_no_policy_touches_nothing(self):
        rows = exposure.exposure_rows(self.net, self.cont)
        self.assertEqual(exposure.expose_apply(rows, self.net, self.cont), rows)
        self.assertFalse([r for r in rows if "want" in r])
        self.assertEqual([i for _, _, i in problems.problems_raw(self.net, self.cont) if i in ("over-exposed", "expose-unmatched")], [])

    def test_over_and_within(self):
        rows = self.rows({"shop-db": "LOCALE", "shop-web": "LAN", "blog-db": "LOCALE"})
        self.assertTrue(exposure.expose_over(rows[5432, "tcp"]))                                 # LAN, meant local
        self.assertFalse(exposure.expose_over(rows[8080, "tcp"]))                                # LAN, meant LAN
        self.assertFalse(exposure.expose_over(rows[5433, "tcp"]))                                # local, meant local
        self.assertIsNone(rows[22, "tcp"]["want"])                                             # nothing declared for sshd
        self.assertFalse(exposure.expose_over(rows[22, "tcp"]))                                  # ...so never over
        self.assertEqual(self.problem("over-exposed"), ["1 service reaches beyond config.ini: shop-db :5432 LAN > local"])
        self.assertEqual(self.rows({"shop-db": "INTERNET"})[5432, "tcp"]["want"], "INTERNET")  # the widest word: nothing is beyond it
        self.assertEqual(self.problem("over-exposed"), [])

    def test_the_problem_is_an_error_listing_every_service_widest_first(self):
        render.CFG["expose"] = {"shop-db": "LOCALE", "n8n": "TAILNET", "shop-web": "TAILNET"}
        self.net["serve"] = [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True}]
        found = [(sev, t) for sev, t, i in problems.problems_raw(self.net, self.cont) if i == "over-exposed"]
        self.assertEqual(found, [(2, "3 services reach beyond config.ini: n8n :8444 Internet > tailnet, "
                                     "shop-db :5432 LAN > local, shop-web :8080 LAN > tailnet")])

    def test_most_restrictive_declaration_wins(self):
        for expose in ({"shop-db": "LAN", "postgres": "LOCALE", "5432": "TAILNET"}, {"5432": "TAILNET", "postgres": "LOCALE", "shop-db": "LAN"}):
            r = self.rows(expose)[5432, "tcp"]
            self.assertEqual((r["want"], r["key"]), ("LOCALE", "postgres"))                    # whatever the order in the file
        r = self.rows({"shop-db": "TAILNET", "5432": "TAILNET"})[5432, "tcp"]
        self.assertEqual(r["key"], "shop-db")                                                  # same reach: the first key

    def test_unknown_counts_as_open(self):
        net = dict(self.net, docker_user=None)                                                # DOCKER-USER unreadable: the verdict is '?'
        rows = self.rows({"shop-web": "TAILNET"}, net)
        self.assertEqual(rows[8080, "tcp"]["lan"], 3)
        self.assertTrue(exposure.expose_over(rows[8080, "tcp"]))
        self.assertTrue(self.problem("over-exposed", net))

    def test_port_keys(self):
        net = dict(self.net, listeners=self.net["listeners"] + [self._ls("0.0.0.0", 8080, "dnsmasq", proto="udp")])
        rows = self.rows({"8080": "LOCALE"}, net)
        self.assertEqual((rows[8080, "tcp"]["want"], rows[8080, "udp"]["want"]), ("LOCALE", None))   # a bare port is tcp
        rows = self.rows({"8080/udp": "LOCALE", "22/tcp": "LAN"}, net)
        self.assertEqual((rows[8080, "tcp"]["want"], rows[8080, "udp"]["want"], rows[22, "tcp"]["want"]), (None, "LOCALE", "LAN"))
        self.assertEqual(self.problem("over-exposed", net), ["1 service reaches beyond config.ini: port 8080/udp tailnet > local"])

    def test_container_replica_project_service_and_database_names(self):
        for key, ports in (("shop-db", {5432}), ("shop-db-1", {5432}), ("db", {5432, 5433}), ("shop", {5432, 8080}),
                           ("postgres", {5432, 5433}), ("blog", {5433}), ("sshd", {22}), ("ssh", {22}), ("ssh.service", {22}),
                           ("shop_db", {5432}), ("shop-web", {8080}), ("nothing", set())):
            got = {p for (p, _), r in self.rows({key: "LOCALE"}).items() if r["want"]}
            self.assertEqual(got, ports, key)
        cont = dict(self.cont, containers=[self._ct("shop_db_1", "shop", [{"p": 5432, "s": "*"}])])  # compose v1 names
        self.assertTrue(self.rows({"shop_db": "LOCALE"}, self.net, cont)[5432, "tcp"]["want"])

    def test_webapp_name_matches_its_ports(self):
        render.CFG["webapps"] = {"storefront": [8080, 9443]}
        self.assertEqual({p for (p, _), r in self.rows({"storefront": "LOCALE"}).items() if r["want"]}, {8080})

    def test_funnel_is_declared_by_the_service_behind_it(self):
        self.net["serve"] = [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True}]
        for key in ("n8n", "n8n.service", "node"):                                             # the unit and the process behind the backend port
            r = self.rows({key: "TAILNET"})[8444, "tcp"]
            self.assertEqual((r["want"], exposure.group_of(r), exposure.expose_over(r)), ("TAILNET", "INTERNET", True), key)
        self.assertIsNone(self.rows({"other": "LOCALE"})[8444, "tcp"]["want"])                 # the other node program is not behind it
        self.assertIsNone(self.rows({"tailscaled": "LOCALE"})[8444, "tcp"]["want"])            # nor is tailscaled, the process that holds the port
        render.CFG["expose"] = {"n8n": "TAILNET"}
        self.assertIn("n8n :8444 Internet > tailnet", self.problem("over-exposed")[0])
        self.net["serve"][0]["target"] = "http://10.0.0.9:5678/"                               # a backend on another host: not this machine's n8n
        self.assertIsNone(self.rows({"n8n": "TAILNET"})[8444, "tcp"]["want"])

    def test_funnel_to_a_container_and_to_a_webapp(self):
        self.net["serve"] = [{"port": 8444, "path": "/", "target": "http://127.0.0.1:8080", "funnel": True}]
        self.assertTrue(self.rows({"shop-web": "TAILNET"})[8444, "tcp"]["want"])
        render.CFG["webapps"] = {"hook": [5678]}
        self.net["serve"][0]["target"] = "http://localhost:5678"
        self.assertEqual(self.rows({"hook": "LOCALE"})[8444, "tcp"]["want"], "LOCALE")

    def test_unmatched_names_are_listed_and_port_keys_never(self):
        render.CFG["webapps"] = {"storefront": [8080]}
        render.CFG["expose"] = {"postgress": "LOCALE", "shop-db": "LOCALE", "old-job": "LOCALE", "postgres": "LOCALE", "n8n": "LOCALE",
                                "storefront": "LOCALE", "9": "LOCALE", "4444/udp": "LOCALE", "db": "LOCALE", "ssh": "LOCALE", "n8nn": "LAN"}
        self.assertEqual(exposure.expose_unmatched(self.net, self.cont), ["postgress", "n8nn"])  # a stopped container and a DB kind are known names
        self.assertEqual(self.problem("expose-unmatched"), ["[expose] 'postgress', 'n8nn' match no service"])
        render.CFG["expose"] = {"postgress": "LOCALE", "8888": "LAN"}
        found = [(sev, t) for sev, t, i in problems.problems_raw(self.net, self.cont, boot={}) if i == "expose-unmatched"]
        self.assertEqual(found, [(1, "[expose] 'postgress' matches no service")])
        render.CFG["expose"] = {"8888": "LAN", "53/udp": "LOCALE"}                             # nothing listens there: fine
        self.assertEqual(self.problem("expose-unmatched"), [])
        boot = {"enabled": [{"unit": "nginx.service", "state": "inactive"}], "failed": ["backup.service"]}
        self.assertEqual(exposure.expose_unmatched(self.net, self.cont, boot, {"nginx": "LOCALE", "backup": "LOCALE", "nope": "LAN"}), ["nope"])

    def test_unmatched_is_not_said_while_the_container_list_is_missing(self):
        render.CFG["expose"] = {"shop-db": "LOCALE"}
        self.assertEqual(self.problem("expose-unmatched", self.net, None), [])                 # collector-containers says that already
        self.assertEqual(exposure.expose_unmatched(self.net, self.cont), [])

    def test_nothing_is_said_without_the_listeners(self):
        render.CFG["expose"] = {"shop-db": "LOCALE"}
        net = dict(self.net, listeners=None)
        self.assertEqual([i for _, _, i in problems.problems_raw(net, self.cont) if i in ("over-exposed", "expose-unmatched")], [])

    def test_other_alarms_are_never_silenced(self):
        before = [(t, i) for _, t, i in problems.problems_raw(self.net, self.cont) if i in ("db-open-lan", "docker-bypass")]
        self.assertEqual({i for _, i in before}, {"db-open-lan", "docker-bypass"})
        render.CFG["expose"] = {"shop-db": "LAN", "shop-web": "LAN", "5432": "INTERNET"}       # everything within reach
        after = [(t, i) for _, t, i in problems.problems_raw(self.net, self.cont) if i in ("db-open-lan", "docker-bypass")]
        self.assertEqual(after, before)
        self.assertEqual(self.problem("over-exposed"), [])

    def test_accepting_over_exposed_does_not_hide_a_new_service(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            problems.ACCEPTED_PATH = os.path.join(d, "accepted.json")
            render.CFG["expose"] = {"shop-db": "LOCALE", "shop-web": "LAN", "blog-db": "LOCALE"}
            self.assertEqual(render.accept_problem("over-exposed", "known", records=problems.problem_records(self.net, self.cont)), 0)
            self.assertFalse([t for _, t in problems.problems(self.net, self.cont) if "beyond config.ini" in t])      # accepted: dimmed
            self.assertEqual(problems.problems(self.net, self.cont).accepted, 1)
            render.CFG["expose"]["shop-web"] = "LOCALE"                                        # a second service now goes beyond
            shown = [t for _, t in problems.problems(self.net, self.cont) if "beyond config.ini" in t]
            self.assertEqual(len(shown), 1, "the accepted problem must not hide a worse one")
            self.assertIn("2 services", shown[0])
            render.CFG["expose"].update({"shop-web": "LAN", "shop-db": "TAILNET"})              # same count, a different port: also new
            self.assertEqual(len([t for _, t in problems.problems(self.net, self.cont) if "beyond config.ini" in t]), 1)
            render.CFG["expose"]["shop-db"] = "LOCALE"                                         # back to what was accepted
            self.assertFalse([t for _, t in problems.problems(self.net, self.cont) if "beyond config.ini" in t])

    def test_fingerprint_keeps_the_numbers_of_the_expose_problems(self):
        self.assertIn("over-exposed", problems.COUNT_MATTERS)
        a, b = "2|1 service reaches beyond config.ini: x :5432 LAN > local", "2|1 service reaches beyond config.ini: x :5433 LAN > local"
        self.assertNotEqual(problems.fingerprint(2, a[2:], "over-exposed"), problems.fingerprint(2, b[2:], "over-exposed"))
        self.assertIn("expose-unmatched", problems.COUNT_MATTERS)
        self.assertNotEqual(problems.fingerprint(1, "[expose] 'a1' matches no service", "expose-unmatched"),
                            problems.fingerprint(1, "[expose] 'a2' matches no service", "expose-unmatched"))  # a new typo is not the accepted one
        self.assertEqual(problems.fingerprint(1, "x 1 y", "net-sections"), problems.fingerprint(1, "x 2 y", "net-sections"))  # the noisy ones still ignore digits

    def test_both_problems_are_catalogued_and_in_the_json(self):
        import contextlib
        import io
        import json as _json
        render.CFG["expose"] = {"shop-db": "LOCALE", "postgress": "LOCALE"}
        for pid in ("over-exposed", "expose-unmatched"):
            title, why, fix = problems.CATALOG[pid]
            self.assertTrue(title and why and fix, pid)
            self.assertNotIn(pid, problems.NOT_ACCEPTABLE)
        orig = render.current_problem_records
        render.current_problem_records = lambda: problems.problem_records(self.net, self.cont, boot={})
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                render.print_problems(["--json"])
        finally:
            render.current_problem_records = orig
        recs = {r["id"]: r for r in _json.loads(out.getvalue())}
        self.assertEqual((recs["over-exposed"]["severity"], recs["expose-unmatched"]["severity"]), ("error", "warning"))
        self.assertTrue(recs["over-exposed"]["acceptable"] and recs["over-exposed"]["fix"])

    def test_port_key_follows_a_funnel_to_its_backend(self):
        self.net["serve"] = [{"port": 8444, "path": "/", "target": "http://127.0.0.1:3000", "funnel": True}]
        rows = self.rows({"3000": "LOCALE"})
        self.assertEqual((rows[3000, "tcp"]["want"], rows[8444, "tcp"]["want"]), ("LOCALE", "LOCALE"))
        self.assertFalse(exposure.expose_over(rows[3000, "tcp"]))                                # the listener is local
        self.assertTrue(exposure.expose_over(rows[8444, "tcp"]))                                 # the Funnel publishes it
        self.assertEqual(self.problem("over-exposed"), ["1 service reaches beyond config.ini: port 3000 :8444 Internet > local"])
        self.assertEqual(self.rows({"3000/tcp": "LOCALE"})[8444, "tcp"]["want"], "LOCALE")
        self.assertIsNone(self.rows({"3000/udp": "LOCALE"})[8444, "tcp"]["want"])              # a udp key has no backend to follow
        self.assertIsNone(self.rows({"3001": "LOCALE"})[8444, "tcp"]["want"])

    def test_port_key_label_does_not_depend_on_the_process_name(self):
        rows = list(self.rows({"8080": "LOCALE"}).values())
        items = exposure.expose_over_items(rows)
        self.assertEqual(items, ["port 8080 LAN > local"])
        for r in rows:
            r["name"] = "haproxy"                                                              # the process behind a port can flip between cycles
        self.assertEqual(exposure.expose_over_items(rows), items)                                # ...the text, and what was accepted, must not
        self.assertEqual(problems.fingerprint(2, items[0], "over-exposed"), "2|port 8080 LAN > local")

    def test_unknown_funnel_status_counts_as_internet(self):
        net = dict(self.net, serve=[], errors={"serve": "tailscale serve status failed"})     # the tailnet-only row 8444: Funnel unknown
        r = self.rows({"8444": "TAILNET"}, net)[8444, "tcp"]
        self.assertEqual((r["net"], exposure.group_of(r)), (3, "TAILNET"))                       # group_of is untouched
        self.assertTrue(exposure.expose_over(r))
        self.assertEqual(self.problem("over-exposed", net), ["1 service reaches beyond config.ini: port 8444 Internet > tailnet"])
        r = self.rows({"8444": "TAILNET"}, dict(self.net, serve=[]))[8444, "tcp"]              # Funnel known to be off: within
        self.assertEqual(r["net"], 0)
        self.assertFalse(exposure.expose_over(r))
        self.assertEqual(self.problem("over-exposed", dict(self.net, serve=[])), [])

    def test_unmatched_is_not_said_from_degraded_data(self):
        render.CFG["expose"] = {"nope": "LOCALE"}
        want = ["[expose] 'nope' matches no service"]
        self.assertEqual(self.problem("expose-unmatched"), want)
        for label, net, cont, boot in (("containers error", self.net, dict(self.cont, error="docker: boom"), {}),
                                       ("containers absent", self.net, dict(self.cont, absent=True), {}),
                                       ("containers disabled", self.net, dict(self.cont, disabled=True), {}),
                                       ("links unreadable", dict(self.net, errors={"links": "boom"}), self.cont, {}),
                                       ("dbs unreadable", dict(self.net, errors={"dbs": "boom"}), self.cont, {}),
                                       ("no boot data", self.net, self.cont, None),
                                       ("no boot argument", self.net, self.cont, False)):
            with self.subTest(label):
                self.assertEqual([t for _, t, i in problems.problems_raw(net, cont, boot=boot) if i == "expose-unmatched"], [])
        net = dict(self.net, errors={"serve": "boom"})                                         # another section: the names are all there
        self.assertEqual(self.problem("expose-unmatched", net), want)
        self.assertFalse([t for _, t in problems.safe_problems(self.net, self.cont) if "match no service" in t or "matches no service" in t])  # page_rete

    def test_a_broken_config_file_is_an_error(self):
        saved = render.CFG.get("config_error")
        try:
            render.CFG["config_error"] = ""
            self.assertEqual([i for _, _, i in problems.problems_raw(self.net, self.cont) if i == "config-unreadable"], [])
            render.CFG["config_error"] = "Source contains parsing errors"
            found = [(sev, t) for sev, t, i in problems.problems_raw(self.net, self.cont) if i == "config-unreadable"]
            self.assertEqual(found, [(2, "config.ini unreadable: defaults in use ([expose] and [webapps] not applied)")])
            self.assertTrue(all(problems.CATALOG["config-unreadable"]))
            self.assertNotIn("config-unreadable", problems.NOT_ACCEPTABLE)
        finally:
            if saved is None:
                render.CFG.pop("config_error", None)
            else:
                render.CFG["config_error"] = saved

    def test_the_matrix_says_beyond_and_expected(self):
        render.CFG["expose"] = {"shop-db": "LOCALE", "shop-web": "LAN"}
        plain = lambda lines: [ansi.ANSI.sub("", ln) for ln in lines]
        panel = plain(render.exposure_block(self.net, self.cont, 120))
        db = next(ln for ln in panel if "shop-db-1" in ln)
        web = next(ln for ln in panel if "shop-web-1" in ln)
        self.assertIn("beyond config.ini: local", db)
        self.assertNotIn("bypasses ufw", db)                                                   # it wins over the other notes
        self.assertIn("expected: LAN", web)
        self.assertIn("docker: bypasses ufw", web)                                             # within reach: the firewall note stays, after the marker
        self.assertFalse([ln for ln in panel if "sshd" in ln and ("expected" in ln or "beyond" in ln)])
        red = next(ln for ln in render.exposure_block(self.net, self.cont, 120) if "shop-db-1" in ln)
        self.assertIn("\x1b[31mbeyond config.ini: local", red)
        render.CFG["expose"] = {}
        self.assertFalse([ln for ln in plain(render.exposure_block(self.net, self.cont, 120)) if "expected:" in ln or "beyond config.ini" in ln])

    def test_the_matrix_fits_narrow_columns(self):
        render.CFG["expose"] = {"shop-db": "LOCALE", "shop-web": "LAN"}
        for w in (59, 79, 99):
            for ln in render.exposure_block(self.net, self.cont, w):
                self.assertLessEqual(ansi.vlen(ln), w, ln)

    def test_the_compact_overview_carries_the_marker(self):
        render.CFG["expose"] = {"shop-db": "LOCALE", "shop-web": "LAN", "n8n": "TAILNET"}
        self.net["serve"] = [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True}]
        for w in (80, 100, 120):
            lines = [ansi.ANSI.sub("", ln) for ln in cardlines.ov_esposizione(self.net, self.cont, w, 1)]
            text = "\n".join(lines)
            self.assertIn("beyond config.ini: tailnet", text)                                  # the Funnel line
            self.assertIn("beyond config.ini: local", text)
            self.assertIn("expected: LAN", text)
            self.assertFalse([ln for ln in lines if len(ln) > w], w)
            self.assertLess(text.index("shop-db-1"), text.index("shop-web-1"))                 # what goes beyond comes first (the list may be cut)

    def test_a_long_attention_line_is_continued_not_cut(self):
        text = "5 services reach beyond config.ini: " + ", ".join(f"service-{i} :{5000 + i} LAN > local" for i in range(5))
        lines = ansi.msg_wrap("err", text, 73)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(ansi.vlen(ln) <= 73 for ln in lines), lines)
        self.assertEqual(ansi.ANSI.sub("", " ".join(x.strip() for x in lines)).replace("✖ ", "", 1), text)
        self.assertEqual(len(ansi.msg_wrap("err", "short", 73)), 1)

    def test_no_wrapped_line_is_wider_than_w(self):
        for w in range(30, 130):
            for step in range(1, 12):
                parts = [f"svc{'x' * ((i * step) % 9)}-{i} :{5000 + i}/udp LAN > local" for i in range(14)]
                parts[0] = "7 services reach beyond config.ini: " + parts[0]
                if max(map(len, parts)) > w - 6:
                    continue                                                                   # one item wider than the line cannot be wrapped
                for line in ansi.msg_wrap("err", ", ".join(parts), w):
                    self.assertLessEqual(ansi.vlen(line), w, (w, step, line))


class ChangedMessage(unittest.TestCase):
    def test_name_change_shows_the_difference_not_the_common_prefix(self):
        self.assertEqual(exposure.name_change("sshd", "evil"), "sshd → evil")
        out = exposure.name_change("serve / → 127.0.0.1:8501", "serve / → 127.0.0.1:9000")
        self.assertIn("8501", out)
        self.assertIn("9000", out)
        self.assertLessEqual(len(out), 2 * 21 + 3)
        old, new = out.split(" → ")
        self.assertNotEqual(old, new)


class TelegramProblems(unittest.TestCase):
    """telegram-unpaired / telegram-failing come from the notifier's status.json (docs/TELEGRAM.md) and only when [telegram] is on."""

    def setUp(self):
        import tempfile
        self.saved = dict(render.CFG["telegram"]), nuc_config.NOTIFY_DIR
        self.tmp = tempfile.TemporaryDirectory()
        nuc_config.NOTIFY_DIR = self.tmp.name
        render.CFG["telegram"]["enabled"] = True

    def tearDown(self):
        render.CFG["telegram"].clear()
        render.CFG["telegram"].update(self.saved[0])
        nuc_config.NOTIFY_DIR = self.saved[1]
        self.tmp.cleanup()

    def status(self, **kw):
        d = {"ts": time.time(), "enabled": True, "paired": True, "username": "someone", "last_sent_ts": None, "last_error": None,
             "failing_since": None}
        d.update(kw)
        with open(os.path.join(self.tmp.name, "status.json"), "w") as f:
            json.dump(d, f)

    def found(self):
        return [(sev, t, pid) for sev, t, pid in problems.problems_raw(NET, CONT) if pid.startswith("telegram-")]

    def test_nothing_is_read_or_said_when_switched_off(self):
        render.CFG["telegram"]["enabled"] = False
        orig = problems.telegram_status
        problems.telegram_status = lambda *a: self.fail("status.json read although [telegram] is off")
        try:
            self.assertEqual(self.found(), [])           # no status.json at all
        finally:
            problems.telegram_status = orig
        self.status(paired=False, ts=time.time() - 9999, failing_since=time.time() - 9999)
        self.assertEqual(self.found(), [])

    def test_healthy_notifier_says_nothing(self):
        self.status(last_sent_ts=time.time() - 5)
        self.assertEqual(self.found(), [])
        self.status(failing_since=time.time() - 120, last_error="HTTP 502")      # a blip of two minutes is not worth a line
        self.assertEqual(self.found(), [])

    def test_enabled_but_not_paired(self):
        self.status(paired=False)
        self.assertEqual(self.found(), [(1, "Telegram notifications on, but not paired", "telegram-unpaired")])
        self.status(paired=False, ts=time.time() - 9999)   # it exits at once when there is no chat: its last word still counts
        self.assertEqual([pid for _, _, pid in self.found()], ["telegram-unpaired"])

    def test_not_running(self):
        self.assertEqual(self.found(), [(1, "Telegram notifier not running", "telegram-failing")])    # no status.json
        self.status(ts=time.time() - 400)
        self.assertEqual(self.found(), [(1, "Telegram notifier not running", "telegram-failing")])
        self.status(ts=time.time() - 200)                                                           # 30 s cycle: 200 s is still alive
        self.assertEqual(self.found(), [])
        for junk in ("[1, 2]", "not json", "", '{"ts": true}', '{"ts": "now"}', '{"paired": true}'):
            with open(os.path.join(self.tmp.name, "status.json"), "w") as f:
                f.write(junk)
            self.assertEqual([pid for _, _, pid in self.found()], ["telegram-failing"], junk)

    def test_failing_for_ten_minutes_says_how_long_and_why(self):
        self.status(failing_since=time.time() - 12 * 60, last_error="HTTP 401 Unauthorized")
        self.assertEqual(self.found(), [(1, "Telegram notifications failing for 12 min: HTTP 401 Unauthorized", "telegram-failing")])
        self.status(failing_since=time.time() - 3 * 3600, last_error=None)
        self.assertEqual(self.found(), [(1, "Telegram notifications failing for 3 h", "telegram-failing")])

    def test_the_error_is_short_clean_and_never_a_token(self):
        token = "123456789:" + "AbC_dE-" * 6
        self.status(failing_since=time.time() - 700, last_error="\x1b[31mred\nHTTP 404 for https://api.telegram.org/bot" + token + "/sendMessage " + "x" * 200)
        (_, text, pid), = self.found()
        self.assertEqual(pid, "telegram-failing")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)
        self.assertNotIn(token, text)
        self.assertNotIn("AbC_dE", text)
        self.assertLess(len(text), 130)

    def test_a_folder_this_user_cannot_open_is_neither_ok_nor_a_problem(self):
        from unittest import mock
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
            self.assertEqual(problems.telegram_state(time.time())[0], "unreadable")
        self.assertEqual(problems.telegram_state(time.time())[0], "down")                             # missing is not the same thing

    def test_catalog_has_both_ids_for_every_os_and_the_fingerprint_ignores_the_minutes(self):
        for pid in ("telegram-unpaired", "telegram-failing"):
            self.assertIn(pid, problems.CATALOG)
            self.assertIn("nuc-console-telegram", problems.CATALOG[pid][2])
            for os_name in ("windows", "darwin"):
                self.assertIn("nuc-console-telegram", problems.OS_CATALOG[os_name][pid][2])
        self.assertIn("nuc-console-telegram.cmd --setup", problems.OS_CATALOG["windows"]["telegram-unpaired"][2])
        self.assertNotIn("telegram-failing", problems.COUNT_MATTERS)
        self.assertEqual(problems.fingerprint(1, "Telegram notifications failing for 12 min: x", "telegram-failing"),
                         problems.fingerprint(1, "Telegram notifications failing for 45 min: x", "telegram-failing"))

    def test_shipped_config_leaves_it_off_and_documents_it(self):
        path = os.path.join(os.path.dirname(__file__), "..", "config", "config.ini")
        self.assertFalse(nuc_config.load(path)["telegram"]["enabled"])
        with open(path, encoding="utf-8") as f:
            self.assertIn("[telegram]", f.read())


class NoClipping(unittest.TestCase):
    """No block may produce a line wider than its column: the 3-column layout used to cut words ("DB/broker ope")."""

    def test_no_line_wider_than_its_column_with_demo_data(self):
        import demo
        orig, wide = render.pack, []

        def spy(blocks, ncol, cw, w, body_h, gap):
            wide.extend((ansi.ANSI.sub("", ln).strip(), cw) for fn in blocks for ln in fn(cw) if ansi.vlen(ln) > cw)
            return orig(blocks, ncol, cw, w, body_h, gap)
        cont, net, boot, base = demo.snapshot()
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": {"local": [], "ssh": []}, "fs": []}
        render.pack = spy
        saved, saved_expose = dict(render.CFG["webapps"]), render.CFG["expose"]
        render.CFG["webapps"] = {"shop-web": [8080], "an-expected-app-with-a-long-name": [9443]}  # one up, one DOWN row
        render.CFG["expose"] = {"shop-web": "LAN", "shop-db": "LOCALE", "n8n": "TAILNET"}  # one within, two beyond: the markers and the long ATTENTION line
        try:
            for w, h in ((118, 33), (199, 50), (200, 40), (224, 50), (225, 50), (234, 60), (239, 67)):
                render.slides(sm, cont, net, w, h - 2, boot, base, mode="overview")
        finally:
            render.pack = orig
            render.CFG["webapps"], render.CFG["expose"] = saved, saved_expose
        self.assertEqual(wide, [])

    def test_fit_join_drops_trailing_items(self):
        self.assertEqual(ansi.fit_join(["aaaa", "bbbb", "cccc"], "  ", 14, " x: "), " x: aaaa  bbbb")
        self.assertEqual(ansi.fit_join(["a" * 50], "  ", 10), "a" * 50)   # one item is always kept (clipped by the column)

    def test_short_ufw_default(self):
        self.assertEqual(cards.short_default("deny (incoming), allow (outgoing), deny (routed)"), "in deny  ·  out allow  ·  fwd deny")
        self.assertEqual(cards.short_default("weird"), "weird")


if __name__ == "__main__":
    unittest.main()
