import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import collector  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402

import re
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
render.MODE = "rotate"  # page tests assume the 3-page rotation; overview tests pass mode= explicitly

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
ROWS = {(r["port"], r["proto"], r["loc"], r["ts"]): r for r in render.exposure_rows(NET, CONT)}


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
        s = render.c(31, "abcdef")
        self.assertEqual(render.vlen(render.clip(s, 3)), 3)

    def test_containers_grouped_and_box_aligned(self):
        lines = render.page_container(CONT, 100)
        text = "\n".join(lines)
        self.assertIn("shop (2)", text)
        self.assertIn("(standalone) (1)", text)
        self.assertIn("*8080 lo:5432", text)
        box = [x for x in lines if x[0] in "┌│└"]
        self.assertTrue(all(render.vlen(x) == 100 for x in box), "right borders misaligned")

    def test_unhealthy_is_red(self):
        row = next(x for x in render.page_container(CONT, 100) if "db-1" in x)
        self.assertIn("\x1b[31m●", row)

    def test_stale_and_missing_state(self):
        old = dict(CONT, ts=time.time() - 500)
        self.assertIn("stale data", "\n".join(render.page_container(old, 100)))
        self.assertIn("collector not running", render.page_container(None, 100)[0])

    def test_frame_fits_screen_and_paginates(self):
        smp = render.Sampler()
        time.sleep(0.2)
        w, h = 100, 20
        sl = render.slides(smp.sample(), CONT, NET, w, h - 2)
        self.assertGreater(len(sl), 2, "with 18 usable lines the System page must be split")
        for i, s in enumerate(sl):
            f = render.frame(s, i, len(sl), w, h).split("\x1b[K\r\n")
            self.assertEqual(len(f), h)
            self.assertTrue(all(render.vlen(r) <= w for r in f))

    def test_escape_sequences_from_untrusted_data_are_neutralised(self):
        evil = dict(CONT, containers=[dict(CONT["containers"][0], project="a\x1bcb\nc", name="n\x1b[2Jx")])
        text = "\n".join(render.page_container(evil, 100))
        self.assertNotIn("\x1bc", text)
        self.assertNotIn("\x1b[2J", text)
        self.assertEqual(render.safe("a\x1b[31m\n"), "a?[31m?")

    def test_exited_container_is_red(self):
        dead = dict(CONT, containers=[dict(CONT["containers"][0], state="exited", status="Exited (137) 1h ago")])
        row = next(x for x in render.page_container(dead, 100) if "web-1" in x)
        self.assertIn("\x1b[31m●", row)

    def test_specific_ip_bind_is_shown(self):
        self.assertEqual(render.fmt_ports([{"p": 9, "s": "100.64.0.3"}, {"p": 1, "s": "lo"}]), "100.64.0.3:9 lo:1")

    def test_broken_state_file_does_not_crash_frame(self):
        smp = render.Sampler()
        broken = {"ts": time.time(), "containers": [{}]}
        sl = render.slides(smp.sample(), broken, NET, 100, 30)
        self.assertIn("error on page System", "\n".join("\n".join(x[3]) for x in sl))

    def test_load_containers_rejects_wrong_shape(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([1, 2], f)
        self.assertIsNone(render.load_containers(f.name))
        os.unlink(f.name)

    def test_fw_verdicts(self):
        v = render.fw_verdict
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
        row = next(r for r in render.exposure_rows(net, CONT) if r["port"] == 5432)
        self.assertEqual((row["lan"], row["warn"]), (2, False))

    def test_page_rete_layouts(self):
        narrow = render.page_rete(NET, CONT, 120)
        wide = render.page_rete(NET, CONT, 240)
        self.assertLess(len(wide), len(narrow))                     # side by side: fewer lines
        self.assertTrue(all(render.vlen(x) <= 240 for x in wide))
        self.assertIn("DOCKER-USER empty", "\n".join(narrow))
        self.assertIn("network collector not running", "\n".join(render.page_rete(None, CONT, 120)))
        self.assertIn("stale", "\n".join(render.page_rete(dict(NET, ts=1), CONT, 120)))

    def test_wide_containers_two_columns(self):
        one, two = render.containers_block(CONT, 100), render.containers_block(CONT, 240)
        self.assertLess(len(two), len(one))
        self.assertTrue(all(render.vlen(x) <= 240 for x in two))

    def ufw(self, *rules, default="deny (incoming), allow (outgoing), disabled (routed)"):
        rows = [dict(zip(("to", "action", "from"), r)) for r in rules]
        return {"active": True, "default": default, "logging": "on (low)", "rules": rows}

    def test_fw_never_blocked_when_rule_not_understood(self):
        """Review finding 1: a rule that cannot be read must give 'unknown', never 'blocked'."""
        v = render.fw_verdict
        for to in ("Nginx Full", "10.0.0.5 80"):
            self.assertEqual(v(80, "tcp", self.ufw((to, "ALLOW IN", "Anywhere")))[0], "unknown", to)
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp on eth0", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(443, "tcp", self.ufw(("80,443/tcp", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(9999, "tcp", self.ufw(("Anywhere on eth0", "ALLOW IN", "Anywhere")))[0], "open")
        self.assertEqual(v(22, "tcp", self.ufw(("22 (v6)", "ALLOW IN", "2001:db8::/32 (v6)")))[0], "unknown")
        self.assertEqual(v(22, "tcp", self.ufw(("Anywhere", "ALLOW IN", "Anywhere 53")))[0], "unknown")

    def test_fw_deny_order_and_scope(self):
        v = render.fw_verdict
        deny_first = self.ufw(("22/tcp", "DENY IN", "10.0.0.0/8"), ("22/tcp", "ALLOW IN", "Anywhere"))
        self.assertEqual(v(22, "tcp", deny_first)[0], "filtered")   # open except 10.0.0.0/8
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp", "DENY IN", "Anywhere"), ("22/tcp", "ALLOW IN", "Anywhere")))[0],
                         "blocked")                                  # first match wins
        self.assertEqual(v(22, "tcp", self.ufw(("22/tcp", "ALLOW FWD", "Anywhere")))[0], "blocked")  # route, not inbound
        self.assertEqual(v(22, "udp", self.ufw(("22/tcp", "ALLOW IN", "Anywhere")))[0], "blocked")
        self.assertEqual(v(80, "tcp", self.ufw(("80", "ALLOW IN", "Anywhere"), default="allow (incoming)"))[0], "open")

    def test_docker_verdict(self):
        d = render.docker_verdict
        self.assertEqual(d(None)[0], 3)
        self.assertEqual(d([])[0], 1)
        self.assertEqual(d(["-A DOCKER-USER -j DROP"])[0], 2)                       # blanket DROP: certain
        self.assertEqual(d(["-A DOCKER-USER -p tcp -m tcp --dport 80 -j DROP"])[0], 3)  # container port: unknown
        self.assertEqual(d(["-A DOCKER-USER -j ufw-user-forward"])[0], 3)          # external chain
        self.assertEqual(d(["-A DOCKER-USER -s 203.0.113.1 -j ACCEPT"])[0], 1)         # no DROP: still open

    def test_userland_proxy_off_published_port_still_listed(self):
        net = dict(NET, listeners=[])                                              # ss sees no socket
        ports = {r["port"] for r in render.exposure_rows(net, CONT)}
        self.assertIn(8080, ports)

    def test_funnel_tcp_forward_foreground_and_failure(self):
        sv = collector.parse_serve('{"TCP": {"8022": {"TCPForward": "127.0.0.1:22"}}, "AllowFunnel": {"host.example.ts.net:8022": true},'
                                   ' "Foreground": {"x": {"Web": {"host.example.ts.net:9000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}},'
                                   ' "AllowFunnel": {"host.example.ts.net:9000": true}}}}')
        self.assertEqual({(x["port"], x["funnel"]) for x in sv}, {(8022, True), (9000, True)})
        net = dict(NET, serve=None, errors={"serve": "tailscale failed"})
        row = next(r for r in render.exposure_rows(net, CONT) if r["port"] == 8444)
        self.assertEqual(row["net"], 3)                                             # Funnel unknown: "?", not "no"

    def test_listeners_missing_is_not_shown_as_zero(self):
        text = "\n".join(render.page_rete(dict(NET, listeners=None, errors={"listeners": "ss failed"}), CONT, 120))
        self.assertIn("EXPOSURE unavailable", text)

    def test_ufw_logging_off_is_stated(self):
        text = "\n".join(render.firewall_block(dict(NET, ufw=dict(UFW, logging="off")), 120))
        self.assertIn("ufw logging off", text)

    def test_problems_summary_and_pill(self):
        pb = render.problems(NET, CONT)
        self.assertEqual(pb[0][0], 2)                                               # errors before warnings
        self.assertTrue(any("DB/broker open on LAN" in t for _, t in pb))
        self.assertTrue(any("Funnel" in t for _, t in pb))
        self.assertIn("PROBLEMS", render.status_pill(pb)[0])
        self.assertEqual(render.status_pill([]), ("✔ ALL OK", "1;7"))
        self.assertTrue(any("network collector not running" in t for _, t in render.problems(None, CONT)))
        off = render.problems(dict(NET, ufw=dict(UFW, active=False)), CONT)
        self.assertTrue(any("ufw off" in t for _, t in off))

    def test_header_shows_status_and_fits(self):
        smp = render.Sampler()
        sl = render.slides(smp.sample(), CONT, NET, 100, 30)
        f = render.frame(sl[0], 0, len(sl), 100, 32, render.problems(NET, CONT)).split("\x1b[K\r\n")
        self.assertIn("PROBLEMS", f[0])
        self.assertLessEqual(render.vlen(f[0]), 100)

    def test_boot_page_fits_one_screen_at_common_sizes(self):
        for w, h in ((100, 30), (120, 33), (160, 40), (240, 67)):
            lines = render.page_boot(BOOT, w - 1, h - 2)
            self.assertLessEqual(len(lines), h - 2, f"{w}x{h}: the Boot page must fit on one screen")
            self.assertTrue(all(render.vlen(x) <= w - 1 for x in lines), f"{w}x{h}: line wider than the screen")

    def test_boot_page_content_and_untrusted_text(self):
        raw = "\n".join(render.page_boot(BOOT, 199, 60))
        text = render.ANSI.sub("", raw)
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
            self.assertTrue(all(render.vlen(x) <= w for x in lines))
            shown = sum(x.count("item") for x in lines)
            self.assertIn(f"+{40 - shown}", render.ANSI.sub("", lines[-1]))     # no item disappears without being counted
        self.assertEqual(len(render.wrap_items(items[:3], 80, indent=5, max_lines=2)), 1)

    def test_boot_problems(self):
        pb = render.problems(NET, CONT, boot=dict(BOOT, failed=["x.service"]))
        self.assertTrue(any("1 failed systemd unit: x.service" in t for _, t in pb))
        self.assertTrue(any("3 errors in this boot's journal" in t for _, t in render.problems(NET, CONT, boot=BOOT)))
        self.assertTrue(any("boot collector not running" in t for _, t in render.problems(NET, CONT, boot=None)))
        self.assertFalse(any("boot" in t for _, t in render.problems(NET, CONT)))    # default: no boot check

    def test_thermal_lines_thresholds_from_sensor_max(self):
        th = {"cpu": (52.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "throttle": 10, "recent": 0,
              "clk": (2.6, 4.9)}
        text = render.ANSI.sub("", "\n".join(render.thermal_lines(th, 30)))
        self.assertIn("52°C/105°C   limits 84/94°C", text)                         # 80% and 90% of the sensor maximum
        self.assertIn("33°C/86°C   limits 69/77°C", text)
        self.assertIn("7.8 min total since boot", text)
        self.assertIn("none in the last minute", text)
        hot = render.ANSI.sub("", "\n".join(render.thermal_lines(dict(th, recent=12), 30)))
        self.assertIn("THROTTLING now (+12 events/min)", hot)
        self.assertIn("measuring", render.ANSI.sub("", "\n".join(render.thermal_lines(dict(th, recent=None), 30))))
        self.assertEqual(render.thermal_lines({}, 30), [])                          # no sensors: no line

    def test_thermal_problems(self):
        base = {"cpu": (50.0, 105.0), "throttle_s": 1.0, "recent": 0}
        self.assertFalse(any("°C" in t or "throttling" in t for _, t in render.problems(NET, CONT, thermal=base)))
        warn = render.problems(NET, CONT, thermal=dict(base, cpu=(85.0, 105.0)))
        self.assertTrue(any(sev == 1 and "CPU at 85°C: above the 84°C threshold" in t for sev, t in warn))
        err = render.problems(NET, CONT, thermal=dict(base, cpu=(95.0, 105.0)))
        self.assertTrue(any(sev == 2 and "CPU at 95°C: above the 94°C threshold" in t for sev, t in err))
        thr = render.problems(NET, CONT, thermal=dict(base, recent=7))
        self.assertTrue(any("thermal throttling: 7 events" in t for _, t in thr))

    def test_baseline_alarms(self):
        keys = render.exposure_keys(NET, CONT)
        self.assertIn("8443/t:TAILNET", keys)                                       # fixed tailscaled port: tracked
        self.assertIn("8444/t:INTERNET", keys)
        self.assertFalse(any(k.startswith("55432/") for k in keys))                 # local only: excluded
        eph = dict(NET, listeners=NET["listeners"] + [{"proto": "tcp", "addr": "100.64.0.1", "port": 50949, "proc": "tailscaled"}])
        self.assertNotIn("50949/t:TAILNET", render.exposure_keys(eph, CONT))        # ephemeral tailscaled port: excluded
        base = {"ts": 1, "ports": dict(keys)}
        self.assertFalse(any(sev == 3 for sev, _ in render.problems(NET, CONT, baseline=base)))   # same: no alarm
        del base["ports"]["5432/t:LAN"]                                             # one more LAN port than expected
        pb = render.problems(NET, CONT, baseline=base)
        self.assertEqual(pb[0][0], 3)                                               # alarms go on top
        self.assertIn("NEW exposed port: 5432/t lan", pb[0][1])
        self.assertIn("EXPOSED PORTS CHANGED", render.status_pill(pb)[0])
        base["ports"]["99/t:LAN"] = "old"                                           # an expected port is gone
        self.assertTrue(any("1 port no longer exposed" in t for sev, t in render.problems(NET, CONT, baseline=base) if sev == 1))
        self.assertTrue(any("port baseline missing" in t for _, t in render.problems(NET, CONT, baseline=None)))
        self.assertFalse(any("baseline" in t for _, t in render.problems(NET, CONT)))   # default: no check

    def test_accept_baseline_roundtrip(self):
        import contextlib
        import io
        import tempfile
        d = tempfile.mkdtemp()
        net, state, bl = os.path.join(d, "net.json"), os.path.join(d, "c.json"), os.path.join(d, "sub", "baseline.json")
        import json
        json.dump(NET, open(net, "w"))
        json.dump(CONT, open(state, "w"))
        old = (render.NET_STATE, render.STATE)
        render.NET_STATE, render.STATE = net, state
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(render.accept_baseline(path=bl), 0)
                mtime = os.path.getmtime(bl)
                self.assertEqual(render.accept_baseline(if_missing=True, path=bl), 0)     # already present: left untouched
            self.assertEqual(os.path.getmtime(bl), mtime)
            self.assertEqual(render.baseline_diff(render.exposure_keys(NET, CONT), render.load_baseline(bl)), ({}, {}, {}))
            os.unlink(net)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(render.accept_baseline(path=os.path.join(d, "b2.json")), 1)  # unknown state: does not write
            self.assertFalse(os.path.exists(os.path.join(d, "b2.json")))
        finally:
            render.NET_STATE, render.STATE = old

    def test_baseline_detects_service_and_filter_changes(self):
        """Review: a change of service or rule on the same port used to raise no alarm."""
        keys = render.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items()}}
        self.assertEqual(render.baseline_diff(keys, base)[2], {})
        evil = {k: dict(v) for k, v in keys.items()}
        evil["22/t:LAN"]["name"] = "evil"                                            # another process on port 22
        pb = render.problems(dict(NET, listeners=[dict(x, proc="evil") if x["port"] == 22 else x for x in NET["listeners"]]),
                             CONT, baseline=base)
        self.assertTrue(any(sev == 3 and "CHANGED 22/t: service sshd → evil" in t for sev, t in pb))
        base["ports"]["22/t:LAN"]["lan"] = 2                                         # expected: filtered by source
        keys["22/t:LAN"]["lan"] = 1                                                  # now open to all
        self.assertIn("now open to the whole LAN", render.baseline_diff(keys, base)[2]["22/t:LAN"])
        keys["22/t:LAN"]["lan"] = 2                                                  # stricter or equal: no alarm
        self.assertEqual(render.baseline_diff(keys, base)[2], {})
        # CHANGED marker in the table
        self.assertEqual(set(render.new_ports(NET, CONT, {"ts": 1, "ports": {"22/t:LAN": {"name": "x", "lan": 2}}}).values()),
                         {"NEW", "CHANGED"})

    def test_baseline_suspended_on_partial_data_and_corrupt_file(self):
        keys = render.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items() if k != "9011/t:LAN"}}
        partial = dict(NET, errors={"ufw": "boom"})
        pb = render.problems(partial, CONT, baseline=base)
        self.assertFalse(any(sev == 3 for sev, _ in pb), "partial data: no false red alarms")
        self.assertTrue(any("port comparison suspended" in t for _, t in pb))
        self.assertEqual(render.new_ports(partial, CONT, base), {})
        self.assertTrue(any(sev == 2 and "port baseline unreadable" in t for sev, t in render.problems(NET, CONT, baseline="corrotta")))
        import tempfile
        d = tempfile.mkdtemp()
        self.assertIsNone(render.load_baseline(os.path.join(d, "nope.json")))       # missing
        bad = os.path.join(d, "bad.json")
        open(bad, "w").write("{truncated")
        self.assertEqual(render.load_baseline(bad), "corrotta")                     # present but broken: not 'missing'
        self.assertEqual(render.new_ports(NET, CONT, "corrotta"), {})               # and does not crash the callers

    def test_accept_refuses_stale_or_partial_state_and_exit_code(self):
        import json
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        net, state = os.path.join(d, "net.json"), os.path.join(d, "c.json")
        json.dump(CONT, open(state, "w"))
        old = (render.NET_STATE, render.STATE)
        render.NET_STATE, render.STATE = net, state
        try:
            import contextlib
            import io
            for label, data in (("stale", dict(NET, ts=time.time() - 9999)), ("with errors", dict(NET, errors={"ufw": "x"}))):
                json.dump(data, open(net, "w"))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(render.accept_baseline(path=os.path.join(d, "b.json")), 1, label)
                self.assertFalse(os.path.exists(os.path.join(d, "b.json")), label)
        finally:
            render.NET_STATE, render.STATE = old
        env = dict(os.environ, NUC_CONSOLE_NET="/nonexistent", NUC_CONSOLE_STATE="/nonexistent",
                   NUC_CONSOLE_BASELINE=os.path.join(d, "x", "b.json"))
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "render.py"), "--accept"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)                                            # the failure is visible to install.sh

    def test_tailscaled_ephemeral_ports_excluded_even_with_ufw_off(self):
        off = dict(NET, ufw=dict(UFW, active=False), listeners=NET["listeners"] + [
            {"proto": "udp", "addr": "0.0.0.0", "port": 50001, "proc": "tailscaled"},
            {"proto": "udp", "addr": "0.0.0.0", "port": 41641, "proc": "tailscaled"}])
        keys = render.exposure_keys(off, CONT)
        self.assertNotIn("50001/u:LAN", keys)                                        # ephemeral: excluded in any group
        self.assertIn("41641/u:LAN", keys)                                           # fixed: tracked

    def test_thermal_hotplug_and_narrow_line(self):
        smp = render.Sampler()
        now = time.monotonic()
        smp.hist.append((now - 30, 1000))
        orig = render.read_thermal
        render.read_thermal = lambda: {"cpu": (50.0, 105.0), "throttle": 900, "throttle_s": 1.0, "clk": (2.6, 4.9)}
        try:
            self.assertIsNone(smp.sample()["thermal"]["recent"])                     # CPU offline: falling counter, never negative
        finally:
            render.read_thermal = orig
        th = {"cpu": (50.0, 105.0), "clk": (2.6, 4.9)}
        for mw in (40, 60, 90):
            self.assertTrue(all(render.vlen(x) <= mw or mw < 40 for x in render.thermal_lines(th, 10, mw)))
        self.assertIn("clock", "\n".join(render.thermal_lines(th, 10, 120)))
        self.assertNotIn("clock", "\n".join(render.thermal_lines(th, 10, 55)))      # when narrow the clock goes first

    def test_overview_fits_small_console_and_never_wider(self):
        smp = render.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "recent": 0, "clk": (2.6, 4.9)}
        for w, h in ((79, 24), (80, 25), (100, 30), (119, 32)):
            sl = render.slides(sm, CONT, NET, w - 1, h - 2, BOOT, baseline=False, mode="overview")
            self.assertEqual(len(sl), 1, f"{w}x{h}: single screen")
            self.assertTrue(all(render.vlen(x) <= w - 1 for x in sl[0][3]), f"{w}x{h}: line wider than the screen")

    def test_overview_broken_block_title_uses_width(self):
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {}}
        lines = render.page_overview(sm, CONT, dict(NET, ufw="x", docker_user=5), BOOT, 100, 60)
        self.assertTrue(all(render.vlen(x) <= 100 for x in lines))

    def test_overview_fits_one_screen(self):
        smp = render.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        for w, h in ((100, 30), (120, 33), (160, 40), (240, 67)):
            sl = render.slides(sm, CONT, NET, w - 1, h - 2, BOOT, baseline=False, mode="overview")
            self.assertEqual(len(sl), 1, f"{w}x{h}: the single screen must be just one")
            self.assertLessEqual(len(sl[0][3]), h - 2)
            self.assertTrue(all(render.vlen(x) <= w - 1 for x in sl[0][3]), f"{w}x{h}: line wider than the screen")
            f = render.frame(sl[0], 0, 1, w - 1, h, render.problems(NET, CONT, boot=BOOT)).split("\x1b[K\r\n")
            self.assertEqual(len(f), h)
            self.assertIn("single screen", f[-1])
            self.assertIn(f"console {w}x{h}", f[-1])

    def test_overview_content_and_new_marker(self):
        sm = {"cpu": {"cpu0": 0.1}, "thermal": {"cpu": (50.0, 105.0)}}
        keys = render.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: v for k, v in keys.items() if k != "9011/t:LAN"}}
        text = render.ANSI.sub("", "\n".join(render.page_overview(sm, CONT, NET, BOOT, 200, 60, baseline=base)))
        for word in ("ATTENTION", "EXPOSURE", "FIREWALL", "SYSTEM", "CONTAINER", "BOOT", "NEW exposed port: 9011"):
            self.assertIn(word, text)
        self.assertIn("NEW ", text)                                                 # marker on the new port
        broken = "\n".join(render.page_overview(sm, CONT, dict(NET, ufw="x"), BOOT, 120, 60))
        self.assertIn("SYSTEM", render.ANSI.sub("", broken))                        # malformed state: the screen stays alive
        self.assertEqual(render.safe_problems(dict(NET, ufw="x"), CONT)[0][0], 2)   # and the header does not raise
        self.assertEqual(render.safe_problems(5, CONT)[0][0], 2)

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
        text = render.ANSI.sub("", "\n".join(render.stack_lines(cont, 100, 3)))
        self.assertIn("shop", text)
        self.assertIn("1/2 running", text)                                         # unhealthy does not count as running
        self.assertIn("● api", text)
        self.assertIn("✖ db", text)
        self.assertIn("(no stack)", text)
        self.assertEqual(render.short_name("shop-api-1", "shop"), "api")
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
        out = render.ANSI.sub("", "\n".join(render.ov_database(dict(NET, dbs=dbs), cont, 120, 0)))
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
        self.assertIn("unavailable", "\n".join(render.ov_database(dict(NET), cont, 120, 0)))       # old collector
        compact = render.ANSI.sub("", "\n".join(render.ov_database(dict(NET, dbs=dbs), cont, 120, 2)))
        self.assertNotIn("in use now", compact)                                    # compact levels: one line per database

    def test_overview_uses_available_space_and_shrinks(self):
        smp = render.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "throttle_s": 469.0, "recent": 0}
        dbs = {"since": time.time(), "items": []}
        big = render.page_overview(sm, CONT, dict(NET, dbs=dbs), BOOT, 239, 65)
        small = render.page_overview(sm, CONT, dict(NET, dbs=dbs), BOOT, 119, 31)
        text = render.ANSI.sub("", "\n".join(big))
        for word in ("HEAVIEST CONTAINERS", "DATABASE", "SLOWEST UNITS", "UFW INBOUND RULES", "PORT"):
            self.assertIn(word, text)                                              # 240x67: full tables, every section
        self.assertGreater(len(big), len(small))
        self.assertLessEqual(len(big), 65)
        self.assertLessEqual(len(small), 31)
        self.assertNotIn("HEAVIEST CONTAINERS", render.ANSI.sub("", "\n".join(small)))
        self.assertTrue(all(render.vlen(x) <= 239 for x in big))

    def test_database_lines_wrap_at_intermediate_width(self):
        """Review: at 199 columns long lists stayed on one line and were silently cut."""
        many = [f"long-service-number-{i}-1" for i in range(30)]
        dbs = {"since": time.time() - 3600, "items": [
            {"name": "shop-db-1", "kind": "elasticsearch", "project": "shop", "host_net": False,
             "ports": [{"p": 9200, "c": "9200/tcp", "s": "*"}, {"p": 9300, "c": "9300/tcp", "s": "*"}],
             "active": many, "usano": many, "stessa_rete": many, "host_clients": [], "ext_source": "netns",
             "external": [{"ip": f"198.51.100.{i}", "last": time.time()} for i in range(1, 40)]}]}
        for w in (79, 119, 199, 300):
            lines = render.ov_database(dict(NET, dbs=dbs), CONT, w, 0)
            self.assertTrue(all(render.vlen(x) <= w for x in lines), f"w={w}: line wider than the screen")
        wide = "\n".join(render.ANSI.sub("", x) for x in render.ov_database(dict(NET, dbs=dbs), CONT, 199, 0))
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
        self.assertEqual(render.fw_verdict(22, "tcp", real)[0], "open")
        self.assertEqual(render.fw_verdict(9999, "tcp", real)[0], "blocked")         # the tailscale0 rule does not concern the LAN
        text = render.ANSI.sub("", "\n".join(render.firewall_block(dict(NET, ufw=real), 120)))
        self.assertIn("UFW INBOUND RULES (4)", text)                                 # IPv4 inbound only
        self.assertIn("2 outbound", text)
        self.assertIn("2 mirrored IPv6", text)
        self.assertNotIn("172.16.0.0/12", text)
        # with dozens of pre-existing rules the single screen at 240x67 must stay at the rich level
        big = dict(real, rules=real["rules"] + [{"to": f"{3000 + i}/tcp", "action": "ALLOW IN", "from": "192.168.0.0/24"} for i in range(20)]
                   + [{"to": f"{3000 + i}/tcp (v6)", "action": "ALLOW IN", "from": "Anywhere (v6)"} for i in range(20)]
                   + [{"to": f"{i}/tcp", "action": "ALLOW OUT", "from": "Anywhere"} for i in range(20)])
        smp = render.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm["thermal"] = {"cpu": (52.0, 105.0), "throttle_s": 1.0, "recent": 0}
        page = render.page_overview(sm, CONT, dict(NET, ufw=big, dbs={"since": time.time(), "items": []}), BOOT, 239, 65)
        text2 = render.ANSI.sub("", "\n".join(page))
        self.assertIn("UFW INBOUND RULES", text2)                                    # rich level: full tables, not the summary
        self.assertIn("PORT", text2)
        self.assertLessEqual(len(page), 65)
        capped = render.ANSI.sub("", "\n".join(render.firewall_block(dict(NET, ufw=big), 120, 3)))
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
        self.assertEqual(render.parse_netdev(dev), {"eth0": (5000, 7000), "docker0": (9, 8)})   # no lo, veth, container bridges
        sess = render.parse_sessions("c1 1000 alice - 123 user pts/0 yes 2h\n7 1000 alice - 5 user - no -\n",
                                     "0 0 192.168.0.10:22 192.168.0.5:50000\n0 0 192.168.0.10:22 203.0.113.9:40000\n0 0 [fd00::1]:22 [fd00::2]:1\n")
        self.assertEqual(sess["local"], [{"user": "alice", "tty": "pts/0"}, {"user": "alice", "tty": ""}])
        self.assertEqual(sess["ssh"], ["192.168.0.5", "203.0.113.9", "fd00::2"])
        self.assertTrue(render.is_private_addr("192.168.0.5") and render.is_private_addr("100.64.0.2"))   # LAN and Tailscale
        self.assertFalse(render.is_private_addr("203.0.113.9"))                      # documentation range: NOT local
        self.assertTrue(render.is_private_addr("::ffff:192.168.0.5") and render.is_private_addr("fd7a:115c:a1e0::1"))
        self.assertFalse(render.is_private_addr("2a0d:3341::1"))
        mounts = "/dev/nvme0n1p2 / ext4 rw 0 0\n/dev/nvme0n1p2 /var/lib/foo ext4 rw 0 0\n/dev/loop3 /snap/x squashfs ro 0 0\ntmpfs /run tmpfs rw 0 0\n/dev/nvme0n1p1 /boot/efi vfat rw 0 0\noverlay /var/lib/docker/overlay2/x overlay rw 0 0\n"
        self.assertEqual(render.parse_mounts(mounts), [("/", "ext4"), ("/boot/efi", "vfat")])   # once per device, real ones only
        self.assertEqual(render.fmt_rate(0), "0 B/s")
        self.assertEqual(render.fmt_rate(12_300), "12.3 kB/s")
        self.assertEqual(render.fmt_rate(4_500_000), "4.5 MB/s")
        sp = render.ANSI.sub("", render.sparkline([0, 100, 5000], 6))
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
        text = lambda lines: render.ANSI.sub("", "\n".join(lines))
        t = text(render.ov_traffico(s, 78, 0))
        self.assertIn("eth0", t)
        self.assertIn("12.3 kB/s", t)
        self.assertIn("1.0G", t)
        for w in (78, 118):
            self.assertTrue(all(render.vlen(x) <= w for x in render.ov_traffico(s, w, 0)), f"traffic too wide at {w}")
        ses = text(render.ov_sessioni(s, 78, 0))
        self.assertIn("ssh from 203.0.113.9  address NOT local or Tailscale", ses)     # ssh from the Internet: highlighted
        self.assertIn("ssh from 192.168.0.5  LAN or Tailscale", ses)
        self.assertIn("pts/0", ses)
        red = "\n".join(render.ov_sessioni(s, 78, 0))
        self.assertIn("\x1b[31m2 connected", red)                                       # with an external ssh the count is red
        self.assertIn("none", text(render.ov_sessioni({"sessions": {"local": [], "ssh": []}}, 78, 0)))
        net = dict(NET, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": True},
                                  "peers": [{"name": "pc", "os": "windows", "online": True, "last_seen": None, "direct": True, "relay": "fra", "exit": False},
                                            {"name": "phone", "os": "android", "online": False, "last_seen": now - 7200, "direct": False, "relay": "", "exit": False},
                                            {"name": "new", "os": "linux", "online": False, "last_seen": None, "direct": False, "relay": "", "exit": True}]})
        ts = text(render.ov_tailscale(net, 78, 0))
        self.assertIn("nuc · 1/3 nodes online · exit node", ts)
        self.assertIn("online direct", ts)
        self.assertIn("offline · seen 2 h ago", ts)
        self.assertIn("offline · never seen", ts)
        self.assertIn("unavailable", text(render.ov_tailscale(NET, 78, 0)))              # old collector: says so
        boot = dict(BOOT, docker_df={"rows": [{"type": "Images", "count": "152", "active": "18", "size": "101.8GB", "reclaimable": "60GB (59%)"}],
                                     "volumes_unused": 151})
        dk = text(render.ov_docker(boot, 90, 0))
        self.assertIn("reclaimable 60GB (59%)", dk)
        self.assertIn("151 volumes not used by any container", dk)
        self.assertIn("\x1b[33m60GB (59%)", "\n".join(render.ov_docker(boot, 90, 0)))     # >= 50% reclaimable: yellow
        self.assertIn("unavailable", text(render.ov_docker(BOOT, 90, 0)))
        self.assertIn("1.0G/4.0G", text(render.ov_dischi(s, 78, 0)))

    def test_pack_first_fit(self):
        mk = lambda n, tag: (lambda cw: [f"{tag}{i}" for i in range(n)])
        out = render.pack([mk(3, "a"), mk(3, "b"), mk(2, "c")], 2, 10, 23, 6, [""])
        out = [render.ANSI.sub("", x) for x in out]                                  # columns() ends every line with a reset
        left = [l[:10].strip() for l in out]
        self.assertEqual(left[:6], ["a0", "a1", "a2", "", "c0", "c1"])              # a stays on top; c (small) fills the gap below
        right = [l[13:].strip() for l in out]
        self.assertEqual(right[:3], ["b0", "b1", "b2"])                              # b did not fit below a: it goes to the next column
        self.assertIsNone(render.pack([mk(9, "x")], 3, 10, 36, 4, [""]))            # a block taller than the column: no room
        one = render.pack([mk(2, "a"), mk(2, "b")], 1, 10, 10, 10, [""])
        self.assertEqual(one, ["a0", "a1", "", "b0", "b1"])                         # one column: stacked with an empty line

    def test_three_columns_at_240_and_new_sections_only_when_room(self):
        smp = render.Sampler()
        time.sleep(0.2)
        sm = smp.sample()
        sm.update(thermal={"cpu": (52.0, 105.0), "throttle_s": 1.0, "recent": 0},
                  net={"eth0": {"rx": 1, "tx": 1, "rx_tot": 5, "tx_tot": 5, "hist_rx": [1], "hist_tx": [1]}},
                  sessions={"local": [], "ssh": []}, fs=[{"mount": "/", "used": 1, "total": 4}])
        net = dict(NET, dbs={"since": time.time(), "items": []}, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": False}, "peers": []})
        big = render.page_overview(sm, CONT, net, dict(BOOT, docker_df={"rows": [], "volumes_unused": 0}), 239, 65)
        text = render.ANSI.sub("", "\n".join(big))
        for title in ("NETWORK TRAFFIC", "SESSIONS", "TAILSCALE", "DOCKER · DISK", "DISKS", "DATABASE", "EXPOSURE", "FIREWALL"):
            self.assertIn(title, text)
        # three columns: section titles appear at three distinct horizontal positions on the same line
        first = render.ANSI.sub("", big[0])
        self.assertGreaterEqual(first.count("──"), 3)
        self.assertTrue(all(render.vlen(x) <= 239 for x in big))
        self.assertLessEqual(len(big), 65)
        small = render.ANSI.sub("", "\n".join(render.page_overview(sm, CONT, net, BOOT, 119, 31)))
        self.assertNotIn("NETWORK TRAFFIC", small)                                   # small console: essentials only
        self.assertNotIn("DISKS", small)

    def test_review10_ts_peers_error_does_not_disable_port_alarms(self):
        keys = render.exposure_keys(NET, CONT)
        base = {"ts": 1, "ports": {k: dict(v) for k, v in keys.items() if k != "9011/t:LAN"}}
        cosmetic = dict(NET, errors={"ts_peers": "tailscale failed", "f2b": "x", "dbs": "y"})
        self.assertFalse(render.exposure_partial(cosmetic))
        self.assertIn("9011/t:LAN", render.new_ports(cosmetic, CONT, base))              # the alarm stays on
        pb = render.problems(cosmetic, CONT, baseline=base)
        self.assertTrue(any(sev == 3 and "NEW exposed port: 9011" in t for sev, t in pb))
        self.assertTrue(any("network sections not collected" in t for _, t in pb))        # but the error still shows
        critical = dict(NET, errors={"ufw": "boom"})
        self.assertTrue(render.exposure_partial(critical))
        self.assertEqual(render.new_ports(critical, CONT, base), {})                     # partial critical data: comparison suspended
        self.assertTrue(any("port comparison suspended" in t for _, t in render.problems(critical, CONT, baseline=base)))
        import contextlib
        import io
        import tempfile
        d = tempfile.mkdtemp()
        net, state = os.path.join(d, "net.json"), os.path.join(d, "c.json")
        json.dump(dict(NET, errors={"ts_peers": "x"}), open(net, "w"))
        json.dump(CONT, open(state, "w"))
        old = (render.NET_STATE, render.STATE)
        render.NET_STATE, render.STATE = net, state
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(render.accept_baseline(path=os.path.join(d, "b.json")), 0)   # the baseline is created even with ts_peers broken
        finally:
            render.NET_STATE, render.STATE = old

    def test_review10_untrusted_docker_text_is_sanitised(self):
        boot = dict(BOOT, docker_df={"rows": [{"type": "Im\x1b[2Jages", "count": "1\x1b[2J", "active": "2\x1b[2J", "size": "3\x1b[2J",
                                                "reclaimable": "\x1bcx (60%)"}], "volumes_unused": 0})
        raw = "\n".join(render.ov_docker(boot, 100, 0))
        self.assertNotIn("\x1b[2J", raw)
        self.assertNotIn("\x1bc", raw)

    def test_review10_sessions(self):
        sess = render.parse_sessions("c1 1000 alice - 123 user pts/0 yes 2h\n7 1000 alice - 5 manager - no -\n", "")
        self.assertEqual(sess["local"], [{"user": "alice", "tty": "pts/0"}])           # 'manager' is not a session
        self.assertIn("1 user session ", render.ANSI.sub("", "\n".join(render.ov_sessioni({"sessions": sess}, 78, 0))))

        class Boom:
            returncode = 1
            stdout = ""
        orig = render.subprocess.run
        render.subprocess.run = lambda *a, **k: Boom()
        try:
            with self.assertRaises(RuntimeError):                                          # failed command: never "none"
                render.read_sessions()
        finally:
            render.subprocess.run = orig
        self.assertIn("unavailable", render.ANSI.sub("", "\n".join(render.ov_sessioni({"sessions": None}, 78, 0))))

    def test_review10_cached_runs_in_background_and_survives_errors(self):
        import threading
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return {"ok": 1}
        t0 = time.monotonic()
        self.assertIsNone(render.cached("test-slow", 60, slow))                         # does not wait for the slow read
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(started.wait(2))
        release.set()
        for _ in range(50):
            if render.cached("test-slow", 60, slow) is not None:
                break
            time.sleep(0.05)
        self.assertEqual(render.cached("test-slow", 60, slow), {"ok": 1})

        def bad():
            raise OSError("stuck mount")
        render.cached("test-bad", 60, bad)
        time.sleep(0.2)
        self.assertIsNone(render.cached("test-bad", 60, bad))                            # error: None, the thread does not die

    def test_review10_docker_df_and_freshness_and_cleanup(self):
        self.assertEqual(collector.parse_docker_df("5\n[1]\nnull\n"), [])              # valid JSON but not an object
        self.assertEqual(render.fmt_ago(-30), "0 s")                                     # no "-30 s"
        old_net = dict(NET, ts=time.time() - 1000, ts_peers={"self": {"name": "nuc", "online": True, "exit_option": False},
                                                              "peers": [{"name": f"p{i}", "os": "x", "online": False, "last_seen": None,
                                                                         "direct": False, "relay": "", "exit": False} for i in range(11)]})
        txt = render.ANSI.sub("", "\n".join(render.ov_tailscale(old_net, 90, 0)))
        self.assertIn("stale data (", txt)
        self.assertIn("… +3 nodes", txt)                                                  # nodes past the eighth are counted
        stale_boot = dict(BOOT, ts=time.time() - 5000, docker_df={"rows": [], "volumes_unused": 0})
        self.assertIn("stale data (", render.ANSI.sub("", "\n".join(render.ov_docker(stale_boot, 90, 0))))
        smp = render.Sampler()
        smp.net_hist["gone0"] = (render.collections.deque([1]), render.collections.deque([1]))
        smp.sample()
        self.assertNotIn("gone0", smp.net_hist)                                          # interface gone: history dropped
        self.assertEqual(render.parse_netdev("h1\nh2\n  eth0: x y z\n"), {})            # line with too few fields: ignored

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
        text = lambda lines: render.ANSI.sub("", "\n".join(lines))
        pb = render.problems(net, cont, boot=boot)
        self.assertTrue(any(sev == 1 and "ufw not installed" in t for sev, t in pb))    # warning, not error
        self.assertFalse(any(sev >= 2 for sev, _ in pb), "no error/alarm just because tools are missing")
        # but with no known firewall a listening database is still flagged (fail-open): absence does not reassure
        risky = dict(net, listeners=net["listeners"] + [{"proto": "tcp", "addr": "0.0.0.0", "port": 5432, "proc": "postgres"}])
        self.assertTrue(any(sev == 2 and "DB/broker open on LAN" in t for sev, t in render.problems(risky, cont, boot=boot)))
        self.assertNotIn("network sections not collected", text([t for _, t in pb]))
        fw = text(render.firewall_block(net, 100))
        self.assertIn("ufw not installed", fw)
        self.assertRegex(fw, r"iptables\s+not installed")
        self.assertNotIn("unreadable", fw)
        self.assertIn("docker not installed", text(render.ov_container(cont, 90, 0)))
        self.assertIn("docker not installed", text(render.containers_block(cont, 90)))
        self.assertIn("not installed", text(render.ov_database(net, cont, 90, 0)))
        self.assertIn("not installed", text(render.ov_tailscale(net, 90, 0)))
        self.assertIn("not installed", text(render.ov_docker(boot, 90, 0)))
        for blk in (render.boot_block_lente(boot, 90, 3), render.boot_block_fallite(boot, 90), render.boot_block_servizi(boot, 90, 2),
                    render.boot_block_container(boot, 90, 2, time.time()), render.boot_block_journal(boot, 90, 3),
                    render.boot_block_avvio(boot, 100.0, 90)):
            self.assertIn("not installed", text(blk))
        # a real fault stays an error, clearly distinct from "not installed"
        broken = dict(net, ufw=None, absent=[], errors={"ufw": "boom"})
        self.assertTrue(any(sev == 2 and "ufw unreadable" in t for sev, t in render.problems(broken, cont, boot=boot)))
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
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
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

    def test_shipped_config_parses_and_lists_every_feature(self):
        path = os.path.join(os.path.dirname(__file__), "..", "config", "config.ini")
        cfg = nuc_config.load(path)
        self.assertTrue(all(cfg["features"].values()))
        text = open(path).read()
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
        self.assertFalse([t for _, t in render.problems(net, cont, boot=boot, baseline=base) if "KeyError" in t])

    def test_collector_skips_disabled_sections(self):
        calls = []
        orig_run, orig_off = collector.run, collector.OFF
        collector.run = lambda name, *a, **k: calls.append(name) or (0, "", "")
        collector.OFF = {"firewall", "fail2ban", "tailscale", "exposure", "databases"}
        try:
            d = collector.collect_net()
        finally:
            collector.run, collector.OFF = orig_run, orig_off
        for tool in ("ufw", "iptables", "tailscale", "ss", "docker", "journalctl"):
            self.assertNotIn(tool, calls)
        self.assertEqual(set(d["disabled"]), {"listeners", "serve", "ts_peers", "ufw", "docker_user", "iptables", "drops", "dbs", "f2b"})
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
            pb = render.problems(net, None, boot=False)
            self.assertFalse([t for _, t in pb if "ufw" in t or "container" in t], pb)
            body = "\n".join("\n".join(x[3]) for x in render.slides(
                {"cpu": {"cpu0": 0.1}, "thermal": {}, "net": {}, "sessions": None, "fs": None}, None, net, 120, 40, None, False, mode="overview"))
            for title in ("FIREWALL", "CONTAINER", "BOOT", "DISKS"):
                self.assertNotIn(title, ANSI_RE.sub("", body))
            self.assertIn("EXPOSURE", ANSI_RE.sub("", body))
        finally:
            render.CFG["features"].clear()
            render.CFG["features"].update(orig)


if __name__ == "__main__":
    unittest.main()
