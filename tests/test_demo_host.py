"""`--demo` shows nothing of the machine it runs on: the same screen on every machine, a function of the clock only.

The screenshots of the README, the tests and anyone trying the dashboard see the demo, so it must not leak the developer's RAM, disk,
uptime, load, core count, temperatures, traffic, sessions or mounts (it did: the SYSTEM block and the System page read /proc). Here two very
different fake hosts (a 2-core box with 1 GiB and a 64-core one with 512 GiB, with their own users, mounts, interfaces and sensors) run
the same demo at the same fake time: every screen must come out byte for byte the same. Nothing here reads the real host.
"""
import contextlib
import io
import os
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import demo  # noqa: E402
import render  # noqa: E402

NOW = 1_790_000_000
GIB = 2 ** 30
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))
OSES = (None, "windows", "darwin")


class Proxy(object):
    """A module as render (or demo) sees it, with a few attributes replaced: the real module is never touched."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Host(object):
    """One fake machine: what every reader of the host would say on it."""

    def __init__(self, name, cores, ram_gib, disk_gib, up_s, load, thermal, user, mount, iface):
        self.name, self.cores, self.ram, self.disk, self.up, self.load = name, cores, ram_gib * GIB, disk_gib * GIB, up_s, load
        self.thermal, self.user, self.mount, self.iface = thermal, user, mount, iface

    def mem(self):
        return {"MemTotal": self.ram, "MemAvailable": self.ram // 10, "Cached": self.ram // 5, "SwapTotal": self.ram // 2, "SwapFree": 0}


SMALL = Host("small-box", 2, 1, 8, 61.0, ["9.99", "9.50", "9.00"], {"cpu": (99.0, 100.0), "nvme": (91.0, 85.85), "throttle": 77, "throttle_s": 5000.0,
                                                                  "clk": (0.8, 1.0), "recent": 9}, "mallory", "/srv/secret", "wlan9")
BIG = Host("big-iron", 64, 512, 20000, 400 * 86400.0, None, {"throttle": None, "throttle_s": None, "clk": None}, "root", "D:", "bond0")


class DemoHost(unittest.TestCase):
    """The demo with the readers of the host behind a fake machine; the fake time stands still (the clock is the one thing the demo uses)."""

    def setUp(self):
        self.saved = {k: getattr(render, k) for k in ("DEMO", "DEMO_OS", "DEMO_HEALTH", "MODE", "time", "socket", "Sampler")}
        self.saved_demo_time = demo.time
        cfg = render.CFG
        self.saved_cfg = (dict(cfg["features"]), dict(cfg["webapps"]), dict(cfg["expose"]), cfg["map_in_rotation"], cfg["cpu_in_rotation"],
                          cfg["health_in_rotation"], cfg["details"], cfg["spacing"])
        self.saved_caches = dict(render._AI), dict(render._HEALTH)
        render.MODE = "overview"  # (tests/test_nuc_console.py leaves it at "rotate")
        clock = Proxy(time, time=lambda: NOW, strftime=lambda fmt, t=None: time.strftime(fmt, time.gmtime(NOW) if t is None else t))
        render.time = demo.time = clock
        cfg["details"], cfg["spacing"] = True, 1
        cfg["map_in_rotation"] = cfg["cpu_in_rotation"] = cfg["health_in_rotation"] = True   # the slides of the rotation too
        for f in ("map", "cpu", "health", "ai"):
            cfg["features"][f] = True

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(render, k, v)
        demo.time = self.saved_demo_time
        cfg = render.CFG
        features, webapps, expose, cfg["map_in_rotation"], cfg["cpu_in_rotation"], cfg["health_in_rotation"], cfg["details"], cfg["spacing"] = self.saved_cfg
        cfg["features"].clear()
        cfg["features"].update(features)
        cfg["webapps"], cfg["expose"] = webapps, expose
        for cache, saved in ((render._AI, self.saved_caches[0]), (render._HEALTH, self.saved_caches[1])):
            cache.clear()
            cache.update(saved)

    @contextlib.contextmanager
    def on(self, host):
        """Every low-level reader of the host (/proc through the module's own readers, os, shutil, the sensors, the sessions and mounts, the
        host name) answers as `host`. The counters move on each read, like a real machine's."""
        ticks = {"n": 0}

        def cpu():
            ticks["n"] += 1
            busy = 100 * ticks["n"]
            return {f"cpu{i}": (busy * (i + 1) // 3, busy * 4) for i in range(host.cores)}

        def net():
            ticks["n"] += 1
            return {host.iface: (10 ** 9 * ticks["n"], 10 ** 6 * ticks["n"])}
        statvfs = types_ns(f_frsize=4096, f_blocks=host.disk // 4096, f_bfree=host.disk // 8192)
        with contextlib.ExitStack() as st:
            for target, value in (("meminfo", host.mem), ("loadavg", lambda: host.load), ("uptime_s", lambda: host.up),
                                  ("root_disk", lambda: (host.disk // 2, host.disk, host.mount)), ("read_thermal", lambda: dict(host.thermal)),
                                  ("read_sessions", lambda: {"local": [{"user": host.user, "tty": "pts/9"}], "ssh": ["203.0.113.99"]}),
                                  ("read_filesystems", lambda: [{"mount": host.mount, "used": 1, "total": host.disk}]),
                                  ("cached", lambda key, ttl, fn: fn())):
                st.enter_context(mock.patch.object(render, target, value))
            st.enter_context(mock.patch.object(render.Sampler, "_cpu", staticmethod(cpu)))
            st.enter_context(mock.patch.object(render.Sampler, "_netdev", staticmethod(net)))
            st.enter_context(mock.patch("os.cpu_count", lambda: host.cores))
            st.enter_context(mock.patch("os.getloadavg", lambda: (9.0, 9.0, 9.0), create=True))
            st.enter_context(mock.patch("os.statvfs", lambda path: statvfs, create=True))
            st.enter_context(mock.patch("shutil.disk_usage", lambda path: (host.disk, host.disk // 2, host.disk // 2)))
            st.enter_context(mock.patch.object(render, "socket", Proxy(render.socket, gethostname=lambda: host.name)))
            render._AI.clear()
            render._HEALTH.clear()
            render.CFG["webapps"], render.CFG["expose"] = {}, {}  # demo_defaults() declares its own, whatever the host's config.ini says
            yield

    def once(self, argv):
        """`render.py --once --demo ARGV`: what it prints, ANSI codes kept (the screenshots keep them)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["--demo", "--color"] + list(argv))
        return out.getvalue()

    def on_both(self, fn):
        """fn() on SMALL, then on BIG: (what it gave on each)."""
        with self.on(SMALL):
            a = fn()
        with self.on(BIG):
            b = fn()
        return a, b

    def assertSame(self, fn, what=""):
        a, b = self.on_both(fn)
        self.assertEqual(a, b, what)
        return a


def types_ns(**kw):
    return type("StatvfsResult", (object,), kw)


def system_block(out):
    """The lines of the overview's SYSTEM block (header to the line before the next section's), ANSI stripped."""
    lines = [render.ANSI.sub("", x) for x in out.split("\n")]
    i = next(k for k, x in enumerate(lines) if x.startswith("── SYSTEM"))
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("── ")), len(lines))
    return [x.rstrip() for x in lines[i:j]]


class TheOverview(DemoHost):
    def test_the_overview_is_the_same_on_two_very_different_hosts(self):
        for cols, rows in ((120, 33), (226, 50)):
            out = self.assertSame(lambda: self.once(["--cols", str(cols), "--rows", str(rows)]), f"{cols}x{rows}")
            self.assertIn("SYSTEM", out)

    def test_it_shows_the_demo_machine_not_the_host(self):
        with self.on(SMALL):
            wide = render.ANSI.sub("", self.once(["--cols", "226", "--rows", "50"]))        # 3 columns: the blocks sit side by side
            small = system_block(self.once(["--cols", "120", "--rows", "33"]))
        self.assertRegex(small[0], r"── SYSTEM ─+  up 5d 0h · load 0\.82 0\.64 0\.51$")
        for want in ("9.6G/15.7G   cache 1.2G", "180.0G/480.0G", "78°C/100°C", "44°C/85°C", "23%   4 cores", "  0 ", " 18%", "  3 ", " 24%"):
            self.assertIn(want, wide)
        for want in ("9.6G/15.7G   cache 1.2G", "180.0G/480.0G", "78°C/100°C", "23%   core "):  # the compact levels: one character per core
            self.assertIn(want, "\n".join(small))
        for leak in ("small-box", "mallory", "99°C", "91°C", "9.99", " 1.0G", "wlan9", "/srv/secret"):
            self.assertNotIn(leak, wide + "\n".join(small))

    def test_every_size_and_os_of_the_overview_rotation_and_details(self):
        def everything():
            out = []
            for os_name in OSES:
                for cols, rows in SIZES:
                    for mode in ("overview", "rotate"):
                        render.DEMO, render.DEMO_OS, render.MODE = True, os_name, mode
                        out += render.render_screens(None, cols - 1, rows, mode=mode)   # every slide: overview, Details, Map, CPU, Health
                        out.append(render.render_screen(None, cols - 1, rows, mode=mode, scroll=True)[0])    # the web's page that scrolls
            return out
        a, b = self.on_both(everything)
        self.assertEqual(len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            self.assertEqual(x, y, f"screen {i}")

    def test_the_system_page_of_the_rotation(self):
        for os_name in OSES:
            for cols, rows in ((120, 33), (226, 50)):
                def page():
                    render.DEMO, render.DEMO_OS = True, os_name
                    return render.render_screens(None, cols - 1, rows, mode="rotate")[0]
                out = render.ANSI.sub("", self.assertSame(page, f"{os_name} {cols}x{rows}"))
                self.assertIn("up 5d 0h", out)
                self.assertIn("RAM", out)

    def test_the_windows_demo_has_no_load_average_and_the_others_have_one(self):
        with self.on(SMALL):
            for os_name, load in ((None, True), ("windows", False), ("darwin", True)):
                head = system_block(self.once(["--demo-os", os_name or "linux", "--cols", "120", "--rows", "40"]))[0]
                self.assertEqual("load" in head, load, (os_name, head))


class TheOtherScreens(DemoHost):
    """CPU, MAP, HEALTH and AI had demo producers already: they must not take anything from the host either (core counts, RAM...)."""

    def test_each_screen_is_the_same_on_two_hosts(self):
        for os_name in OSES:
            for view, extra in (("cpu", []), ("cpu", ["--details", "--select", "node"]), ("map", []), ("health", []), ("health", ["--period", "1"]),
                                ("ai", []), ("ai", ["--select", "qwen3-8b", "--details"])):
                for cols, rows in ((120, 33), (226, 50)):
                    argv = ["--view", view, "--cols", str(cols), "--rows", str(rows)] + extra + (["--demo-os", os_name] if os_name else [])
                    out = self.assertSame(lambda: self.once(argv), " ".join(argv))
                    self.assertGreater(len(out), 500, argv)

    def test_a_real_sampler_given_to_the_demo_is_ignored(self):
        """The web view always owns a real Sampler: under --demo what it reads (a hot CPU, 64 cores) must not reach the page or the header."""
        for os_name in OSES:
            render.DEMO, render.DEMO_OS = True, os_name
            with self.on(SMALL):
                smp = render.Sampler()
                smp.sample()
                self.assertGreater(render.read_thermal()["recent"], 0)      # the host really is throwing problems
                pills = [list(render.cpu_problems(smp)), list(render.map_graph(smp)[1]), list(render.health_state(smp, 7)[1]),
                         list(render.ai_state(smp)[1])]
                with_smp = render.render_screen(smp, 119, 33, mode="overview")
            with self.on(BIG):
                without = render.render_screen(None, 119, 33, mode="overview")
                none = [list(render.cpu_problems(None)), list(render.map_graph(None)[1]), list(render.health_state(None, 7)[1]),
                        list(render.ai_state(None)[1])]
            self.assertEqual(pills, none, os_name)
            self.assertEqual(with_smp, without, os_name)
            self.assertFalse([t for pb in pills for _, t in pb if "°C" in t or "throttling" in t], os_name)

    def test_the_demo_reads_nothing_from_the_host(self):
        def boom(*a, **kw):
            raise AssertionError("the demo read the host")
        with self.on(SMALL), mock.patch.multiple(render, meminfo=boom, loadavg=boom, uptime_s=boom, root_disk=boom, read_thermal=boom,
                                                 read_sessions=boom, read_filesystems=boom, Sampler=boom), \
                mock.patch("os.cpu_count", boom), mock.patch.object(render.cpuinfo, "CpuSampler", boom), mock.patch.object(render.procs, "ProcSampler", boom):
            for argv in ([], ["--demo-os", "windows"], ["--demo-os", "darwin"], ["--view", "cpu"], ["--view", "map"], ["--view", "health"], ["--view", "ai"]):
                self.assertGreater(len(self.once(argv + ["--cols", "120", "--rows", "33"])), 500, argv)


class TheSamplerOfTheDemo(unittest.TestCase):
    def test_it_has_every_key_of_a_real_sample_and_nothing_else(self):
        real = FakeSampler().sample()
        for os_name in OSES:
            sm = demo.sampler_data(os_name, NOW)
            self.assertEqual(set(sm), set(real), os_name)
            self.assertEqual(sorted(sm["cpu"]), [f"cpu{i}" for i in range(len(sm["cpu"]))])
            self.assertTrue(all(isinstance(sm[k], dict) for k in ("cpu", "thermal", "net", "mem")))
            self.assertTrue(all(0.0 <= v <= 1.0 for v in sm["cpu"].values()))

    def test_the_figures_of_the_linux_demo(self):
        sm = demo.sampler_data(None, NOW)
        self.assertAlmostEqual(sum(sm["cpu"].values()) / len(sm["cpu"]), 0.23)
        self.assertEqual(sorted(sm["cpu"].values()), [0.09, 0.18, 0.24, 0.41])
        self.assertEqual((render.human(sm["mem"]["MemTotal"] - sm["mem"]["MemAvailable"]), render.human(sm["mem"]["MemTotal"]),
                          render.human(sm["mem"]["Cached"])), ("9.6G", "15.7G", "1.2G"))
        self.assertEqual(sm["disk_root"], (180 * GIB, 480 * GIB, "/"))
        self.assertEqual(render.up_load_note(sm["uptime"], sm["load"]), "up 5d 0h · load 0.82 0.64 0.51")

    def test_each_os_has_its_own_machine(self):
        win, mac = demo.sampler_data("windows", NOW), demo.sampler_data("darwin", NOW)
        self.assertEqual(win["load"], [])                       # Windows: no load average
        self.assertEqual(len(mac["load"]), 3)
        self.assertEqual((win["disk_root"][2], mac["disk_root"][2]), ("C:", "/"))
        self.assertEqual(win["mem"]["SwapTotal"], 0)             # as winapi.memory() says
        self.assertEqual(list(win["net"]), ["Ethernet"])
        self.assertEqual(list(mac["net"]), ["en0"])
        self.assertEqual(win["thermal"].get("cpu"), None)        # the temperatures of those two are the collector's (sensors.json)
        self.assertEqual(sorted(demo.sampler_data(None, NOW)["thermal"]), ["clk", "cpu", "nvme", "recent", "throttle", "throttle_s"])

    def test_only_the_traffic_moves_with_the_clock(self):
        a, b, c = demo.sampler_data(None, NOW), demo.sampler_data(None, NOW), demo.sampler_data(None, NOW + 60)
        self.assertEqual(a, b)
        self.assertEqual({k: v for k, v in a.items() if k != "net"}, {k: v for k, v in c.items() if k != "net"})
        self.assertNotEqual(a["net"], c["net"])
        n = a["net"]["eth0"]
        self.assertEqual((len(n["hist_rx"]), len(n["hist_tx"])), (render.NET_HIST, render.NET_HIST))
        self.assertEqual((n["rx"], n["tx"]), (n["hist_rx"][-1], n["hist_tx"][-1]))
        self.assertTrue(all(v >= 0 for v in n["hist_rx"] + n["hist_tx"]))

    def test_it_does_not_depend_on_the_host(self):
        with mock.patch("os.cpu_count", lambda: 64), mock.patch("os.statvfs", create=True, side_effect=AssertionError), \
                mock.patch("builtins.open", side_effect=AssertionError("read a file")):
            a = demo.sampler_data("darwin", NOW)
        self.assertEqual(a, demo.sampler_data("darwin", NOW))

    def test_the_uptime_is_the_boot_the_boot_section_reports(self):
        cont, net, boot, base = demo.snapshot(NOW)
        self.assertEqual(NOW - boot["btime"], demo.UP_DEMO)
        self.assertEqual(demo.sampler_data(None, NOW)["uptime"], demo.UP_DEMO)
        for os_name in ("windows", "darwin"):
            self.assertEqual(NOW - demo.snapshot(NOW, os_name)[2]["btime"], demo.sampler_data(os_name, NOW)["uptime"])


class FakeSampler(object):
    """A real Sampler.sample() on a machine that says nothing: the keys the demo has to have (a field the Sampler gains must be invented there too)."""

    def sample(self):
        none = lambda: None  # noqa: E731
        with mock.patch.multiple(render, cached=lambda key, ttl, fn: None, read_thermal=lambda: {"throttle": None, "throttle_s": None, "clk": None}, meminfo=none, loadavg=none, uptime_s=none,
                                 root_disk=none), \
                mock.patch.object(render.Sampler, "_cpu", staticmethod(lambda: {})), mock.patch.object(render.Sampler, "_netdev", staticmethod(lambda: {})):
            return render.Sampler().sample()


if __name__ == "__main__":
    unittest.main()
