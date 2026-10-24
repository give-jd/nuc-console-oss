"""/metrics of the web view: the exposition is valid (a strict parser below), unknown figures are left out, labels are escaped, and the route
has the API's access rules (token off loopback, Host guard, GET/HEAD only) and a switch ([web] metrics = no: 404)."""
import math
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import threading  # noqa: E402
import unittest  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import cards  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webmetrics  # noqa: E402
from test_web import TOKEN, get_any as get, serve  # noqa: E402
import test_webapi  # noqa: E402

NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
SAMPLE = re.compile(r"(%s)(?:\{((?:%s=\"(?:[^\"\\\n]|\\[\\\"n])*\",?)*)\})? (\S+)" % (NAME, "[a-zA-Z_][a-zA-Z0-9_]*"))
LABEL = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=\"((?:[^\"\\\n]|\\[\\\"n])*)\"")


def parse(text):
    """{(name, labels tuple): float}, strictly: HELP and TYPE once per family and before its samples, no duplicate series, finite numbers,
    one trailing newline, nothing else."""
    assert text.endswith("\n") and not text.endswith("\n\n"), "one final newline"
    helped, typed, out = set(), {}, {}
    for line in text[:-1].split("\n"):
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in helped and re.fullmatch(NAME, name), line
            helped.add(name)
        elif line.startswith("# TYPE "):
            _h, _t, name, kind = line.split(" ")
            assert name not in typed and kind in ("gauge", "counter") and name in helped, line
            typed[name] = kind
        else:
            m = SAMPLE.fullmatch(line)
            assert m, "not a sample: %r" % line
            name, labels, value = m.groups()
            assert name in typed, "sample before its TYPE: " + line
            tags = tuple(LABEL.findall(labels or ""))
            assert (name, tags) not in out, "duplicate series: " + line
            assert math.isfinite(float(value)), line
            out[(name, tags)] = float(value)
    return out


def ctx(**kw):
    base = dict(s={}, cont=None, net=None, boot=None, problems=None, cfg=render.CFG, now=1000.0, baseline=None)
    base.update(kw)
    return cards.Ctx(**base)


def pbl(items, accepted=0, muted=0):
    pb = problems.ProblemList([(sev, "text") for sev, _ in items])
    pb.pids, pb.accepted, pb.muted = [pid for _s, pid in items], accepted, muted
    return pb


class Format(unittest.TestCase):
    def test_nothing_known_gives_only_the_fixed_series(self):
        got = parse(webmetrics.render(ctx(), "1.2.3").decode())
        self.assertEqual(sorted(n for n, _ in got), ["nuc_console_build_info", "nuc_console_up"])
        self.assertEqual(got[("nuc_console_build_info", (("version", "1.2.3"),))], 1)

    def test_unknown_is_omitted_never_zero(self):
        s = {"mem": {"MemTotal": 0}, "load": [], "uptime": None, "fs": None, "thermal": {}, "net": {}, "cpu": {}}
        text = webmetrics.render(ctx(s=s, cont={"absent": True}), "1").decode()
        for name in ("memory", "load", "uptime", "filesystem", "temperature", "network", "containers", "cpu_usage", "problems", "firewall", "exposed"):
            self.assertNotIn("nuc_console_" + name, text)

    def test_the_figures(self):
        s = {"cpu": {"cpu0": 0.5, "cpu1": 0.25}, "load": ["0.5", "1", "2"], "mem": {"MemTotal": 1000, "MemAvailable": 400}, "uptime": 90,
             "fs": [{"mount": "/", "used": 10, "total": 100}], "thermal": {"cpu": (50, 100), "nvme": (61, 80)},
             "net": {"eth0": {"rx_tot": 5, "tx_tot": 7}}}
        cont = {"ts": 990, "containers": [{"state": "running", "status": "Up"}, {"state": "running", "status": "Up (unhealthy)"},
                                          {"state": "exited", "status": "Exited"}]}
        c = ctx(s=s, cont=cont, net={"ts": 900}, problems=pbl([(2, "a"), (1, "b"), (1, "b")], accepted=2, muted=1))
        got = {k: v for k, v in parse(webmetrics.render(c, "1").decode()).items()}
        g = lambda n, **lab: got[("nuc_console_" + n, tuple(sorted(lab.items())))]  # noqa: E731
        self.assertEqual((g("cpu_usage_ratio"), g("load15"), g("memory_used_bytes"), g("uptime_seconds"), g("temperature_celsius")), (0.375, 2, 600, 90, 61))
        self.assertEqual((g("filesystem_used_bytes", mount="/"), g("network_receive_bytes_total", interface="eth0")), (10, 5))
        self.assertEqual((g("containers", state="running"), g("containers", state="unhealthy"), g("containers", state="exited")), (2, 1, 1))
        self.assertEqual((g("problems", severity="error"), g("problems", severity="warning"), g("problems", severity="change")), (1, 2, 0))
        self.assertEqual((g("problem_active", id="a"), g("problems_muted"), g("problems_accepted")), (1, 1, 2))
        self.assertEqual((g("collector_age_seconds", collector="network"), g("collector_age_seconds", collector="containers")), (100, 10))

    def test_hostile_labels_are_escaped(self):
        evil = 'a"b\\c\nd'
        c = ctx(s={"fs": [{"mount": evil, "used": 1, "total": 2}]}, problems=pbl([(1, evil)]))
        text = webmetrics.render(c, evil).decode()
        got = parse(text)  # a raw quote or newline would not parse
        self.assertIn('mount="a\\"b\\\\c?d"', text)  # ui.safe: the API's own treatment of a newline
        self.assertIn('id="a\\"b\\\\c\\nd"', text)
        self.assertEqual(len([k for k in got if k[0] == "nuc_console_filesystem_used_bytes"]), 1)

    def test_problem_ids_are_capped_and_not_repeated(self):
        c = ctx(problems=pbl([(1, "p%d" % i) for i in range(300)] + [(1, "p1")]))
        n = [k for k in parse(webmetrics.render(c, "1").decode()) if k[0] == "nuc_console_problem_active"]
        self.assertEqual(len(n), webmetrics.PROBLEM_IDS_MAX)


class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_webapi.setUpModule()
        cls.open, cls.locked = serve(), serve(TOKEN)
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2, metrics=False)
        cls.off = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
        threading.Thread(target=cls.off.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.open, cls.locked, cls.off):
            test_webapi.stop(s)
        test_webapi.tearDownModule()

    def test_default_is_on_and_valid(self):
        self.assertTrue(nuc_config.load()["web"]["metrics"])
        st, h, body = get(self.open, "/metrics")
        self.assertEqual((st, h["Content-Type"]), (200, "text/plain; version=0.0.4; charset=utf-8"))
        self.assertEqual(h["Cache-Control"], "no-store")
        got = parse(body)
        self.assertIn(("nuc_console_up", ()), got)
        self.assertLess(len(body), 64 * 1024)

    def test_head_is_get_without_the_body(self):
        st, h, body = get(self.open, "/metrics")
        hst, hh, hbody = get(self.open, "/metrics", "HEAD")
        self.assertEqual((hst, hbody), (st, ""))
        self.assertEqual(int(hh["Content-Length"]), len(body.encode()))

    def test_only_get_and_head(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(get(self.open, "/metrics", m)[0], 405, m)

    def test_off_loopback_style_token_rules_as_the_api(self):
        for path in ("/metrics", "/api/v1/overview"):
            self.assertEqual(get(self.locked, path)[0], 401, path)
            self.assertEqual(get(self.locked, path, headers={"Authorization": "Bearer nope"})[0], 401, path)
            self.assertEqual(get(self.locked, path, "HEAD")[0], 401, path)
            self.assertEqual(get(self.locked, path, headers={"Authorization": "Bearer " + TOKEN})[0], 200, path)
            self.assertEqual(get(self.locked, path, headers={"Authorization": "Bearer " + TOKEN, "Sec-Fetch-Site": "cross-site"})[0], 403, path)
        self.assertEqual(get(self.locked, "/metrics?token=" + TOKEN)[0], 200)
        self.assertEqual(get(self.open, "/metrics", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(get(self.open, "/api/v1", headers={"Host": "evil.example"})[0], 421)

    def test_switched_off_is_a_404_before_anything(self):
        self.assertEqual(get(self.off, "/metrics")[0], 404)
        self.assertEqual(get(self.off, "/api/v1")[0], 200)

    def test_it_exposes_nothing_the_api_does_not(self):
        import json
        api = json.loads(get(self.open, "/api/v1/overview")[2])
        ids = {p["id"] for p in api["problems"]["items"]}
        got = parse(get(self.open, "/metrics")[2])
        self.assertLessEqual({dict(t)["id"] for n, t in got if n == "nuc_console_problem_active"}, ids)


if __name__ == "__main__":
    unittest.main()
