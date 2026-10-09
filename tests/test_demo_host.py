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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import demo  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import cards  # noqa: E402
import ansi  # noqa: E402

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
    lines = [ansi.ANSI.sub("", x) for x in out.split("\n")]
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
            wide = ansi.ANSI.sub("", self.once(["--cols", "226", "--rows", "50"]))        # 3 columns: the blocks sit side by side
            small = system_block(self.once(["--cols", "120", "--rows", "33"]))
        self.assertRegex(small[0], r"── SYSTEM ─+  up 5d 0h · load 0\.82 0\.64 0\.51$")
        for want in ("10.2G/31.2G   cache 4.8G", "180.0G/480.0G", "72°C/100°C", "44°C/85°C", "6%   16 threads"):
            self.assertIn(want, wide)
        self.assertRegex(wide, r" 4 █+░+ +62%")                                            # the busy thread of the demo machine
        for want in ("10.2G/31.2G   cache 4.8G", "180.0G/480.0G", "72°C/100°C", "6%   core "):  # the compact levels: one character per core
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
                out = ansi.ANSI.sub("", self.assertSame(page, f"{os_name} {cols}x{rows}"))
                self.assertIn({None: "up 5d 0h", "windows": "up 1d 2h", "darwin": "up 2d 6h"}[os_name], out)
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
        now = time.time()  # two renders a second apart show a different age of the same stale state: one instant for both
        with mock.patch("time.time", return_value=now):
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
            self.assertEqual(sorted(sm["cpu"], key=lambda k: int(k[3:])), [f"cpu{i}" for i in range(len(sm["cpu"]))])
            self.assertTrue(all(isinstance(sm[k], dict) for k in ("cpu", "thermal", "net", "mem")))
            self.assertTrue(all(0.0 <= v <= 1.0 for v in sm["cpu"].values()))

    def test_the_figures_of_the_linux_demo(self):
        sm = demo.sampler_data(None, NOW)
        self.assertEqual(len(sm["cpu"]), 16)                                                 # 8 cores, 16 threads
        self.assertAlmostEqual(sum(sm["cpu"].values()) / len(sm["cpu"]), 0.0625)
        self.assertEqual(max(sm["cpu"].values()), 0.62)
        self.assertEqual((ui.human(sm["mem"]["MemTotal"] - sm["mem"]["MemAvailable"]), ui.human(sm["mem"]["MemTotal"]),
                          ui.human(sm["mem"]["Cached"])), ("10.2G", "31.2G", "4.8G"))
        self.assertEqual(sm["disk_root"], (180 * GIB, 480 * GIB, "/"))
        self.assertEqual(cards.up_load_note(sm["uptime"], sm["load"]), "up 5d 0h · load 0.82 0.64 0.51")

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
        self.assertEqual(demo.sampler_data(None, NOW)["thermal"]["cpu"], (72.0, 100.0))        # the CPU screen's package, the sensor's own maximum

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
        for os_name in OSES:
            self.assertEqual(NOW - demo.snapshot(NOW, os_name)[2]["btime"], demo.sampler_data(os_name, NOW)["uptime"], os_name)
            self.assertEqual(demo.sampler_data(os_name, NOW)["uptime"], demo.machine(os_name)["up"], os_name)


def facts(text, **patterns):
    """{name: the groups of the first match of its regex in `text` (ANSI stripped), None if it does not match}."""
    text = ansi.ANSI.sub("", text)
    out = {}
    for name, rx in patterns.items():
        m = re.search(rx, text, re.M)
        out[name] = m.groups() if m else None
    return out


class OneMachine(DemoHost):
    """The overview, the CPU screen, the AI screen, BOOT and HEALTH of one demo OS describe the same machine: cores, memory, uptime, load,
    boot time, temperature and disks (they used to invent one machine each: 4 cores here, 16 threads there, a 21.8 s boot next to a 58 s one)."""

    def screens(self, os_name):
        argv = ["--demo-os", os_name] if os_name != "linux" else []
        with self.on(SMALL):
            overview = self.once(argv + ["--cols", "226", "--rows", "50"])
            cpu = self.once(argv + ["--view", "cpu", "--cols", "200", "--rows", "50"])
            ai = self.once(argv + ["--view", "ai", "--cols", "200", "--rows", "50"])
            health = self.once(argv + ["--view", "health", "--cols", "226", "--rows", "50"])
        return overview, cpu, ai, health

    def test_every_screen_says_the_same_about_the_machine(self):
        for os_name in ("linux", "windows", "darwin"):
            m = demo.MACHINES[os_name]
            overview, cpu, ai, health = self.screens(os_name)
            up = "up %dd %dh" % (m["up"] // 86400, m["up"] % 86400 // 3600)
            load = "%.2f %.2f %.2f" % m["load"] if m["load"] else None
            threads, cores = m["cpu"]["threads"], m["cpu"]["cores"]
            ram_gib = "%.1f" % (m["ram_mib"] / 1024.0)
            o = facts(overview, up=r"SYSTEM ─+\s+(up \d+d \d+h)", load=r"SYSTEM ─+\s+up \d+d \d+h · load (\d\.\d\d \d\.\d\d \d\.\d\d)",
                      ram=r"RAM +[█░]+ (\d+\.\d)G/(\d+\.\d)G", cpus=r"CPU +[█░]+ +\d+% +(\d+) threads", boot=r"finished in (\d+\.\d)s")
            c = facts(cpu, up=r"(up \d+d \d+h)", load=r"load (\d\.\d\d \d\.\d\d \d\.\d\d)", cores=r"cores (\d+)  ·", threads=r"threads (\d+)")
            a = facts(ai, cpu=r"CPU +(.+?) · (\d+) cores(?: / (\d+) threads)? · ", ram=r"RAM +[█░]+ +\d+\.\d GB free of (\d+\.\d) GB",
                      gpu=r"GPU +(\S.*?) · (?:CUDA|Metal)")
            h = facts(health, boot=r"last (\d+) s · median (\d+) s · (\d+) boots?",
                      slower=r"took (\d+) s, ([\d.]+)x the median of the (\d+) boots before \((\d+) s\)")
            what = os_name
            self.assertEqual(o["up"], (up,), what)                                          # uptime: the SYSTEM block ...
            self.assertEqual(c["up"], (up,), what)                                          # ... and the CPU screen
            self.assertEqual(o["load"], (load,) if load else None, what)                    # load: the same three numbers
            self.assertEqual(c["load"], (load,) if load else None, what)
            self.assertEqual(o["ram"][1], ram_gib, what)                                     # RAM: the SYSTEM block and the AI screen
            self.assertEqual(a["ram"], (ram_gib,), what)
            self.assertAlmostEqual(float(o["ram"][1]) - float(o["ram"][0]), m["avail_mib"] / 1024.0, delta=0.06, msg=what)   # used + free = total
            self.assertEqual(int(o["cpus"][0]), threads, what)                              # logical CPUs: the per-core bars,
            self.assertEqual((int(c["cores"][0]), int(c["threads"][0])), (cores, threads), what)   # the CPU screen,
            self.assertEqual(int(a["cpu"][1]), cores, what)                                 # and the AI screen
            if threads != cores:
                self.assertEqual(int(a["cpu"][2]), threads, what)
            self.assertIn(m["cpu"]["model"].split(" 8-Core")[0], cpu)
            self.assertIn(a["gpu"][0], [g["name"] for g in m["gpus"]], what)
            if m["boot"]:                                                                    # BOOT and HEALTH: the same boot
                self.assertEqual(float(o["boot"][0]), m["boot"]["total"], what)
                self.assertEqual(int(h["boot"][0]), round(m["boot"]["total"]), what)
                self.assertEqual(int(h["boot"][1]), round(m["boot"]["median"]), what)
                self.assertEqual(int(h["boot"][2]), len(m["boot"]["before"]) + 1, what)
            else:
                self.assertIsNone(o["boot"], what)
                self.assertIn("no boot times recorded", ansi.ANSI.sub("", health), what)
            self.assertEqual(h["slower"] is not None, os_name == "linux", what)           # only the Linux demo story has the slow boot
            if h["slower"]:
                self.assertEqual((int(h["slower"][0]), int(h["slower"][3])), (58, 23))      # 58 s, 2.5x the median of 23 s
                self.assertEqual(float(h["slower"][1]), round(m["boot"]["total"] / m["boot"]["median"], 1))
                self.assertEqual(int(h["slower"][2]), 7)

    def test_the_temperatures_the_thermal_history_and_the_disks_agree(self):
        lin = demo.machine("linux")
        overview, cpu, ai, health = self.screens("linux")
        self.assertIn("72°C/100°C", ansi.ANSI.sub("", overview))                           # the TEMP bar ...
        self.assertRegex(ansi.ANSI.sub("", cpu), r"PKG +█+░* 72°C/100°C +high 90°C +crit 100°C")   # ... and the CPU screen's package, with its limits
        rep = demo.health_report(None, 7, NOW)
        self.assertGreaterEqual(rep["thermal"]["max"], lin["temp"]["high"])                  # the hot hours were over the 90 C limit, now it is at 72
        self.assertGreater(rep["thermal"]["max"], lin["temp"]["cpu"])
        self.assertIn("(%.0f C)" % lin["temp"]["high"], [f for f in rep["findings"] if f["id"] == "thermal:host"][0]["text"])
        for os_name in ("linux", "windows", "darwin"):                                       # the disks of HEALTH are the DISKS block's
            fs = {x["mount"]: x for x in demo.sampler_data(os_name, NOW)["fs"]}
            rows = {d["mount"]: d for d in demo.health_report(os_name, 7, NOW)["disks"]}
            self.assertTrue(set(fs) <= set(rows), os_name)
            for mount, x in fs.items():
                self.assertAlmostEqual(rows[mount]["used_pct"], x["used"] * 100.0 / x["total"], delta=0.06, msg=(os_name, mount))
        rep = demo.health_report(None, 7, NOW)
        data = [d for d in rep["disks"] if d["mount"] == "/data"][0]
        full = [f for f in rep["findings"] if f["id"] == "disk-full:/data"][0]
        left = (300.0 - 249.0) / full["facts"]["growth_gb_day"]                            # the days to full follow from the figures
        self.assertAlmostEqual(data["days_to_full"], left, delta=0.1)
        self.assertEqual(full["facts"]["days_to_full"], data["days_to_full"])
        self.assertIn("83%", full["text"])

    def test_the_boot_timeline_adds_up_and_ends_where_the_uptime_begins(self):
        for os_name in ("linux", "windows"):
            m = demo.machine(os_name)
            boot = demo.snapshot(NOW, os_name)[2]
            self.assertAlmostEqual(sum(boot["analyze"]["parts"].values()), boot["analyze"]["total"], places=1, msg=os_name)
            self.assertEqual(boot["analyze"]["total"], m["boot"]["total"], os_name)
            rows = demo.health_report(os_name, 7, NOW)["boots"]
            self.assertEqual((rows[-1]["total_s"], rows[-1]["boot"]), (m["boot"]["total"], NOW - m["up"]), os_name)   # the last boot is this one
        blame = [x["s"] for x in demo.snapshot(NOW)[2]["blame"]]
        self.assertEqual(blame, sorted(blame, reverse=True))                                 # slowest first
        self.assertLess(max(blame), demo.machine()["boot"]["total"])                         # no unit slower than the whole boot
        before = sorted(demo.machine()["boot"]["before"])
        self.assertEqual(before[len(before) // 2], demo.machine()["boot"]["median"])

    def test_the_processes_use_the_machines_memory_and_boot(self):
        for os_name in ("linux", "windows", "darwin"):
            m = demo.machine(os_name)
            sample = demo.proc_sample(os_name, NOW)
            top = max((p for p in sample["procs"] if p["mem"]), key=lambda p: p["mem"])
            self.assertAlmostEqual(top["mem_pct"], top["mem"] * 100.0 / (m["ram_mib"] * demo.MiB), delta=0.06, msg=os_name)
            started = [p["start"] for p in sample["procs"] if p["start"] is not None]
            self.assertTrue(all(NOW - m["up"] <= t <= NOW for t in started), os_name)        # nothing started before the machine booted


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
