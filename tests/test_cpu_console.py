"""The console CPU screen (render.py): the data behind it (sensors.json merged into the sampler's temperatures), the grid of CPUs, the
temperatures, the process table, the keys, `--view cpu` and its options, every frame inside its screen, the CPU among the rotating
pages, the feature switch, hostile names, unknown values and missing data.

Hermetic like tests/test_map_console.py: the demo data at a clock that stands still, a fake terminal for the main loop (no TTY), fake
producers where the samplers matter (no /proc, no ps), no host state, on Linux, macOS and Windows alike. Every module global a test
changes is put back in tearDown.
"""
import contextlib
import io
import json
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402

NOW = 1_790_000_000
SIZES = ((79, 24), (120, 33), (200, 50), (226, 50))       # console sizes (--cols --rows): the layout gets cols - 1, as in once()
OSES = (None, "windows", "darwin")
ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)               # built at runtime: the file itself stays plain text
SGR = re.compile(r"\x1b\[[0-9;]*m")                        # the only escape sequences the renderer puts inside a line
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LINE = "\x1b[K\r\n"                                        # frame(): every line but the last ends with erase-to-end + CRLF
RENDER_GLOBALS = ("DEMO", "DEMO_OS", "MODE", "WINDOWS", "MACOS", "ACCEPTED_PATH", "SENSORS", "time", "os", "sys", "signal", "shutil",
                  "socket", "termios", "tty", "Sampler", "read_keys", "snapshot", "page_overview", "cpuinfo", "procs", "load_json", "cpu_screen", "cpu_slide", "slides", "write_text_atomic")
CELL = re.compile(r"(?<![\d.])(\d{1,3})([PE]?) [█▒░]+ +(\d+)%( \d\.\d\dG)?( +\d+°C)?")
EVIL = "x" + ESC + "[2J" + "y" + ESC + "]0;owned" + BEL + "z" + CSI8 + "31m\r\n<b>&amp;"


class Proxy(object):
    """A module as render (or demo) sees it, with a few attributes replaced: the real module is never touched."""

    def __init__(self, real, **over):
        self._real = real
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Stop(Exception):
    """Ends render.main()'s endless loop from inside a fake keyboard read."""


class FakeSampler(object):
    """No /proc, no hostinfo: what Sampler.sample() returns on a machine with nothing to say."""

    def sample(self):
        return {"cpu": {}, "thermal": {}, "net": {}, "sessions": None, "fs": None}


def overview_stub(*a, **kw):
    return ["   overview page (stub)"]


class CpuCase(unittest.TestCase):
    """The demo at NOW, the cpu feature on and out of the rotation, nothing read from the host; all of it undone after."""

    def setUp(self):
        self.saved = {k: getattr(render, k) for k in RENDER_GLOBALS}
        self.saved_demo = {k: getattr(demo, k) for k in ("time", "cpu_sample", "proc_sample", "sensors")}
        cfg = render.CFG
        self.saved_cfg = (dict(cfg["features"]), cfg["webapps"], cfg["map_in_rotation"], cfg["cpu_in_rotation"])
        self.saved_expose = cfg["expose"]
        self.tmp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.on_sleep = self.pass_time
        clock = Proxy(time, time=lambda: self.now, sleep=lambda sec: self.on_sleep(sec),
                      strftime=lambda fmt, t=None: time.strftime(fmt, time.gmtime(self.now) if t is None else t))
        render.time = demo.time = clock
        render.socket = Proxy(render.socket, gethostname=lambda: "test-host")  # demo_defaults() renames it on the proxy only
        render.ACCEPTED_PATH = os.path.join(self.tmp.name, "accepted.json")     # missing: nothing accepted, whatever the host has
        render.SENSORS = os.path.join(self.tmp.name, "sensors.json")            # missing unless a test writes it
        render.DEMO, render.DEMO_OS = True, None
        cfg["features"]["cpu"], cfg["cpu_in_rotation"] = True, False

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(render, k, v)
        for k, v in self.saved_demo.items():
            setattr(demo, k, v)
        features, render.CFG["webapps"], render.CFG["map_in_rotation"], render.CFG["cpu_in_rotation"] = self.saved_cfg
        render.CFG["expose"] = self.saved_expose
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        self.tmp.cleanup()

    def pass_time(self, sec):
        self.now += sec

    def feed(self, data=(None, None, None)):
        """render.snapshot(): the dashboard state (none by default: the CPU screen does not need it, only its header pill does)."""
        render.snapshot = lambda w: dict(cont=data[0], net=data[1], boot=data[2], baseline=None)

    def data(self, os_name=None, change=None):
        """What the CPU screen shows at NOW on the demo machine of that OS, read through the real CpuFeed (which merges sensors.json)."""
        render.DEMO, render.DEMO_OS = True, os_name
        d = render.CpuFeed().read()
        if change:
            change(d)
        return d

    def procs(self, d, **where):
        return next(p for p in d["procs"]["procs"] if all(p[k] == v for k, v in where.items()))

    def check_frame(self, screen, w, h, what=""):
        """Exactly h lines, none wider than w once the colours are stripped, and no control character or escape sequence but
        the renderer's own colours and line ends. -> the lines, ANSI stripped."""
        rows = screen.split(LINE)
        self.assertEqual(len(rows), h, what)
        out = [render.ANSI.sub("", x) for x in rows]
        for x in out:
            self.assertLessEqual(len(x), w, (what, x))
        self.assertIsNone(CONTROL.search(SGR.sub("", "".join(rows))), what)
        return out

    def once(self, opts, cols, rows):
        """`render.py --once --view cpu OPTS --cols cols --rows rows`, without printing: (frame, its lines ANSI stripped)."""
        s = render.cpu_once(["render.py", "--once", "--view", "cpu"] + list(opts), cols - 1, rows)
        return s, self.check_frame(s, cols - 1, rows, (opts, cols, rows, render.DEMO_OS))

    def producers(self, cpu_os=None, boom=()):
        """Fake cpuinfo/procs modules that count what is made and sampled (the demo data underneath). -> the counters."""
        log = dict(cpu_made=0, cpu_sampled=0, proc_made=0, proc_sampled=0)

        class CpuSampler(object):
            def __init__(self):
                if "cpu_init" in boom:
                    raise OSError("no /proc")
                log["cpu_made"] += 1

            def sample(self):
                log["cpu_sampled"] += 1
                if "cpu" in boom:
                    raise ValueError("bad stat " + ESC + "[2J")
                return demo.cpu_sample(cpu_os, render.time.time())

        class ProcSampler(object):
            def __init__(self):
                if "proc_init" in boom:
                    raise OSError("no /proc")
                log["proc_made"] += 1

            def sample(self):
                log["proc_sampled"] += 1
                if "proc" in boom:
                    raise ValueError("bad status")
                return demo.proc_sample(cpu_os, render.time.time())
        render.DEMO = False
        render.cpuinfo = types.SimpleNamespace(CpuSampler=CpuSampler)
        render.procs = types.SimpleNamespace(ProcSampler=ProcSampler)
        return log


def grid_cells(lines):
    """{cpu id: (tag, busy %, 'x.xxG' or None, °C or None)} read back from the screen."""
    out = {}
    for line in lines:
        for m in CELL.finditer(line):
            out[int(m.group(1))] = (m.group(2).strip(), int(m.group(3)), m.group(4).strip() if m.group(4) else None,
                                    int(m.group(5).strip()[:-2]) if m.group(5) else None)
    return out


def process_lines(lines):
    """The process rows of a screen, as (pid, text after the pid): everything between the column heads and the footer."""
    at = next(i for i, x in enumerate(lines) if re.match(r"\s+PID[▼▲ ]", x))
    return [x for x in lines[at + 1:-1] if re.match(r"\s+\d+ ", x)]


class Data(CpuCase):
    """cpu_merge, cpu_data and CpuFeed: what the producers hand over, what the screen gets."""

    def sensors(self, os_name, ts=None, **over):
        s = demo.sensors(os_name, NOW)
        s["ts"] = NOW - 5 if ts is None else ts
        s["cpu"].update(over)
        return s

    def test_sensors_fill_what_the_sampler_could_not_read_on_windows_and_macos(self):
        for os_name, want in (("windows", ("LibreHardwareMonitor", 8, "Core #3")), ("darwin", ("smctemp", 0, "CPU die (smctemp)"))):
            render.DEMO_OS = os_name
            d = self.data(os_name)
            t = d["cpu"]["temps"]
            self.assertEqual((t["source"], len(t["cores"])), want[:2], os_name)
            self.assertIn(want[2], [s["label"] for s in t["sensors"]])
            self.assertIsInstance(t["package"], float)
            self.assertEqual(sorted(t["cores"]), list(range(1, 9)) if os_name == "windows" else [])   # JSON's string keys became numbers
            self.assertTrue(all(isinstance(k, int) for k in t["cores"]))
        d = self.data("darwin")
        self.assertEqual(d["extra"]["pressure"], "Moderate")
        self.assertEqual([x["name"] for x in d["extra"]["clusters"]], ["E-Cluster", "P-Cluster"])
        self.assertEqual(d["extra"]["notes"], [])

    def test_the_samplers_own_readings_are_kept(self):
        cpu = demo.cpu_sample("windows", NOW)
        cpu["temps"].update(package=50.0, cores={1: 40.0}, source="acpitz", sensors=[{"label": "x", "c": 1.0}])
        render.DEMO_OS = "windows"
        got, extra = render.cpu_merge(cpu, self.sensors("windows"), NOW)
        t = got["temps"]
        self.assertEqual((t["package"], t["source"], t["sensors"]), (50.0, "acpitz", [{"label": "x", "c": 1.0}]))
        self.assertEqual(sorted(t["cores"]), list(range(1, 9)))               # a core the sampler had keeps its value, the others come
        self.assertEqual(t["cores"][1], 40.0)
        self.assertEqual(cpu["temps"]["cores"], {1: 40.0})                      # the sampler's own dict is not modified

    def test_linux_never_reads_sensors_json(self):
        with open(render.SENSORS, "w") as f:
            json.dump(self.sensors("windows"), f)
        render.DEMO, render.DEMO_OS = False, None
        render.WINDOWS = render.MACOS = False
        got, extra = render.cpu_merge({"temps": {"package": None, "cores": {}}}, render.load_json(render.SENSORS), NOW)
        self.assertIsNone(got["temps"]["package"])
        self.assertEqual(extra["notes"], [])

    def test_a_stale_missing_or_broken_sensors_json_is_ignored_and_says_so(self):
        render.DEMO_OS = "windows"
        cpu = demo.cpu_sample("windows", NOW)
        for sens, note in ((self.sensors("windows", ts=NOW - 61), "stale (61 s old)"), (self.sensors("windows", ts=NOW - 7200), "stale (2 h old)"),
                           ({"os": "windows", "cpu": {"package": 50.0}}, "stale"), (None, "no sensors.json"), ([1], "no sensors.json"),
                           ("text", "no sensors.json")):
            got, extra = render.cpu_merge(cpu, sens, NOW)
            self.assertIsNone(got["temps"]["package"], note)
            self.assertEqual(got["temps"]["cores"], {}, note)
            self.assertEqual(len(extra["notes"]), 1, note)
            self.assertEqual(extra["notes"][0][0], "warn")
            self.assertIn(note, extra["notes"][0][1])
        got, extra = render.cpu_merge(cpu, self.sensors("windows", ts=NOW - 60), NOW)  # exactly the limit: still read
        self.assertIsNotNone(got["temps"]["package"])

    def test_numbers_that_are_not_numbers_are_dropped(self):
        render.DEMO_OS = "windows"
        nan, inf = float("nan"), float("inf")
        s = self.sensors("windows", package=nan, cores={"1": True, "2": "hot", "3": inf, "x": 5.0, "4": 44.5, "5": None, "²": 3.0},
                         sensors=[{"label": "a", "c": nan}, {"label": "b", "c": "1"}, {"label": "c", "c": 33.0}, "junk", None],
                         pressure=5, clusters=[1, {"name": "E-Cluster"}], source=7)
        got, extra = render.cpu_merge(demo.cpu_sample("windows", NOW), s, NOW)
        t = got["temps"]
        self.assertEqual((t["package"], t["cores"], [x["label"] for x in t["sensors"]], t["source"]), (None, {4: 44.5}, ["c"], None))
        self.assertEqual((extra["pressure"], extra["clusters"]), (None, [{"name": "E-Cluster"}]))
        for bad in (True, "3", None, nan, inf, [], {}):
            self.assertIsNone(render.num(bad), bad)
        self.assertEqual((render.num(3), render.num(2.5), render.num(0)), (3.0, 2.5, 0.0))
        self.assertEqual(render.idict({"1": "a", 2: "b", "x": "c", None: "d", "²": "e"}), {1: "a", 2: "b"})

    def test_collector_errors_are_notes(self):
        render.DEMO_OS = "darwin"
        s = self.sensors("darwin")
        s["errors"] = {"powermetrics": "needs root " + ESC + "[2J", "smctemp": "x", "third": "y"}
        got, extra = render.cpu_merge(demo.cpu_sample("darwin", NOW), s, NOW)
        self.assertEqual([x[0] for x in extra["notes"]], ["info", "info"])      # the first two
        self.assertIn("powermetrics: needs root", extra["notes"][0][1])

    def test_a_sampler_that_raises_leaves_its_part_empty_and_a_note(self):
        for boom, note in (("cpu", "cpu sampler failed: ValueError"), ("proc", "process sampler failed: ValueError"),
                           ("cpu_init", "cpu sampler: OSError"), ("proc_init", "process sampler: OSError")):
            log = self.producers(boom=(boom,))
            d = render.CpuFeed().read()
            notes = [x[1] for x in d["extra"]["notes"]]
            self.assertIn(note, notes, boom)
            self.assertNotIn(ESC, "".join(notes))
            if boom.startswith("cpu"):
                self.assertEqual(d["cpu"]["usage"] if "usage" in d["cpu"] else {}, {}, boom)
                self.assertTrue(d["procs"]["procs"], boom)
            else:
                self.assertEqual(d["procs"]["procs"], [], boom)
                self.assertTrue(d["cpu"]["usage"]["cores"], boom)
            for cols, rows in SIZES:                                             # and the screen still draws
                body = render.cpu_view(d, cols - 1, rows - 2)[0]
                self.check_frame(render.frame(("CPU", 1, 1, body), 0, 1, cols - 1, rows, []), cols - 1, rows, (boom, cols))

    def test_garbage_from_a_producer_never_crashes_the_screen(self):
        garbage = [None, [], "text", 5, {"usage": 5, "temps": [], "freq": "x", "kinds": 3, "cache": [], "rates": None, "load": "abc", "throttle": 1},
                   {"usage": {"total": "x", "cores": [None, {"id": "a"}, {"id": 1, "busy": "x"}, {"id": 2, "busy": float("nan")}, 7]},
                    "temps": {"cores": [1], "sensors": [None, 1, {"c": "x"}], "package": "hot"}, "load": [1], "kinds": {"P": "x", "E": None}}]
        for g in garbage:
            render.DEMO = False
            render.cpuinfo = types.SimpleNamespace(CpuSampler=lambda g=g: types.SimpleNamespace(sample=lambda: g))
            render.procs = types.SimpleNamespace(ProcSampler=lambda g=g: types.SimpleNamespace(sample=lambda: g))
            d = render.CpuFeed().read()
            for cols, rows in SIZES:
                body = render.cpu_view(d, cols - 1, rows - 2, details=True, cur=1)[0]
                self.check_frame(render.frame(("CPU", 1, 1, body), 0, 1, cols - 1, rows, []), cols - 1, rows, (g, cols))
        render.procs = types.SimpleNamespace(ProcSampler=lambda: types.SimpleNamespace(sample=lambda: {"procs": [None, {"pid": "a"}, {"pid": True},
                                                                                                         {"pid": 5, "name": None}, 3], "total": []}))
        d = render.CpuFeed().read()
        self.assertEqual([p["pid"] for p in d["procs"]["procs"]], [5])
        body = "\n".join(render.ANSI.sub("", x) for x in render.cpu_view(d, 119, 31)[0])
        self.assertRegex(body, r"\s+5 \?\s+.* \?\n?")                              # a nameless process: '?'

    def test_the_feed_reads_once_per_max_age_and_again_when_the_demo_changes(self):
        log = self.producers()
        feed = render.CpuFeed(max_age=2)
        first = feed.read()
        self.assertIs(feed.read(), first)
        self.now += 1.9
        self.assertIs(feed.read(), first)
        self.assertEqual((log["cpu_sampled"], log["proc_sampled"]), (1, 1))
        self.now += 0.2
        self.assertIsNot(feed.read(), first)
        self.assertEqual((log["cpu_made"], log["proc_made"], log["cpu_sampled"], log["proc_sampled"]), (1, 1, 2, 2))  # the samplers are kept
        self.now -= 3600                                                           # the clock went back: a new reading, not a stale one
        self.assertEqual(feed.read()["at"], self.now)
        plain = render.CpuFeed()
        plain.read(), plain.read()
        self.assertEqual(log["cpu_sampled"], 5)                                    # max_age 0: every read samples
        render.DEMO = True
        fed = render.CpuFeed(max_age=60)
        windows = fed.read()
        render.DEMO_OS = "windows"
        self.assertIsNot(fed.read(), windows)
        self.assertIn("AMD", fed.read()["cpu"]["model"])

    def test_the_first_read_settles_so_the_screen_has_a_cpu_percentage(self):
        log = self.producers()
        waited = []
        self.on_sleep = lambda sec: waited.append(sec)
        render.CpuFeed(settle=0.4).read()
        self.assertEqual((waited, log["proc_sampled"]), ([0.4], 2))
        render.CpuFeed().read()
        self.assertEqual(waited, [0.4])


class Screens(CpuCase):
    """The frames: every size, every demo OS, what each block shows."""

    OPTIONS = ([], ["--sort", "mem"], ["--sort", "time"], ["--sort", "pid"], ["--sort", "user"], ["--sort", "bogus"], ["--select", "ffmpeg"],
               ["--select", "ffmpeg", "--details"], ["--details"], ["--select", "kworker", "--details", "--sort", "pid"],
               ["--select", "no such process", "--details"], ["--sort"], ["--select"])

    def test_every_frame_fills_its_screen_and_nothing_more(self):
        for os_name in OSES:
            render.DEMO_OS = os_name
            for cols, rows in SIZES:
                for opts in self.OPTIONS:
                    with self.subTest(os=os_name, size=(cols, rows), opts=opts):
                        s, lines = self.once(opts, cols, rows)
                        self.assertIn("test-host │ CPU │", lines[0].replace("demo-host", "test-host"))
                        self.assertIn("── CPU ", lines[1])
                        self.assertRegex(lines[-1], r"\d+/\d+")
                        self.assertIn("PROCESSES", "\n".join(lines))
                        self.assertTrue(process_lines(lines), "no process row")

    def test_the_linux_demo_shows_the_machine_the_cpus_the_temperatures_and_the_processes(self):
        d = self.data()
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        for want in ("12th Gen Intel(R) Core(TM) i7-1260P", "sockets 1", "cores 12", "threads 16", "P 8 + E 8 threads", "arch x86_64",
                     "L1d 448K  L1i 640K  L2 9M  L3 18M", "governor powersave/intel_pstate", "clock 400-4700 MHz (base 2100)", "up 3d 4h",
                     "load 2.41 1.98 1.75", "ctxt 18", "intr 6.", "running 4", "blocked 1"):
            self.assertIn(want, txt)
        cells = grid_cells(lines)
        self.assertEqual(sorted(cells), list(range(16)))
        self.assertEqual({cells[i][0] for i in range(8)}, {"P"})
        self.assertEqual({cells[i][0] for i in range(8, 16)}, {"E"})
        for i, (tag, busy, ghz, temp) in cells.items():                           # each CPU: its busy %, clock and the temperature of its own core
            row = d["cpu"]["usage"]["cores"][i]
            self.assertEqual(busy, round(row["busy"]), i)
            self.assertEqual(ghz, f"{d['cpu']['freq']['cur'][i] / 1000:4.2f}G".strip(), i)
            self.assertEqual(temp, round(d["cpu"]["temps"]["cores"][d["cpu"]["core_of"][i]]), i)
        self.assertEqual((cells[4][3], cells[5][3]), (cells[4][3], cells[4][3]))     # CPU 4 and 5 are one core: one temperature
        self.assertGreater(cells[4][3], 90)
        self.assertIn("ALL ", txt)
        self.assertRegex(txt, r"user \d+\.\d  sys \d+\.\d  nice \d+\.\d  iowait \d+\.\d  irq \d+\.\d  steal 0\.0  idle \d+\.\d")
        self.assertRegex(txt, r"PKG +█+░* \d+°C/100°C +high 100°C +crit 100°C")
        self.assertRegex(txt, r"hottest core 8 9\d°C \(cpu 4,5\)")
        self.assertRegex(txt, r"throttled ! 1204 events, 4\.2 min in all; cores 8: 812, 4: 37")
        self.assertIn("source coretemp", txt)
        self.assertIn("Package id 0", txt)
        self.assertIn("Core 23", txt)                                               # every sensor, at this width
        self.assertRegex(txt, r"41 total · 3 running · \d+ threads · 2 unreadable · by CPU%")
        self.assertRegex(txt, r"PID USER +S +NI +THR +CPU%▼ +MEM% +RSS +TIME NAME")
        rows = process_lines(lines)
        tail = process_lines(self.once(["--sort", "pid", "--select", "3301"], 200, 50)[1])           # the end of the list
        self.assertIn("ffmpeg", rows[0])                                            # sorted by CPU%: the busiest first
        self.assertRegex(rows[0], r"^\s+2210 alice +R +10 +18 +\d+\.\d +\d+\.\d +640M +2:32:00 ffmpeg")
        cpu = [float(re.search(r"\s(\d+\.\d)\s+\d+\.\d\s+\S+\s+\S+ \S", x).group(1)) for x in rows]
        self.assertEqual(cpu, sorted(cpu, reverse=True))
        self.assertGreaterEqual(len(rows), 30)                                      # most of the demo's 40 processes fit at 200x50
        self.assertIn("1/40", lines[-1])
        self.assertTrue(any(re.match(r"\s+2477 1000 +Z ", x) for x in tail))        # the zombie
        self.assertTrue(any(re.match(r"\s+2501 \? +\? +\? +\? +\? +\? +\? +\? gvfsd-fuse", x) for x in tail))  # the unreadable one: '?'

    def test_windows_has_no_state_nice_or_load_and_its_temperatures_come_from_the_collector(self):
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        render.DEMO_OS = "windows"
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        self.assertIn("AMD Ryzen 7 5800X", txt)
        self.assertIn("load ?", txt)
        self.assertIn("running ?", txt)
        self.assertNotRegex(txt, r"PID▼? USER +S ")
        self.assertRegex(txt, r"PID USER +THR +CPU%▼")                             # S and NI left out: unknown for every process here
        self.assertIn("source LibreHardwareMonitor", txt)
        self.assertRegex(txt, r"hottest core 3 90°C \(cpu 4,5\)")                    # the collector's core 3 = logical CPUs 4 and 5
        cells = grid_cells(lines)
        self.assertEqual((cells[0][3], cells[1][3], cells[4][3], cells[5][3], cells[15][3]), (66, 66, 90, 90, 65))
        self.assertEqual({v[0] for v in cells.values()}, {""})                      # no P/E on this CPU
        self.assertIn("throttled ?", txt)
        self.assertRegex(txt, r"\n\s+4 \? +214 +1\.\d +\? +\? +\? System")           # the protected process: '?' where it was not readable
        self.assertRegex(txt, r"iowait \?  irq \?  steal \?")
        self.assertIn("Core #8", txt)

    def test_the_cpu_to_core_map_is_never_a_guess(self):
        d = self.data("windows")
        ids = list(range(16))
        self.assertEqual(render.cpu_core_of(d, ids), {i: 1 + i // 2 for i in ids})  # adjacent threads, cores in sensor order: said in the docstring
        d["cpu"]["cores"] = 6                                                       # 8 sensors for 6 cores: not the same cores
        self.assertEqual(render.cpu_core_of(d, ids), {})
        d["cpu"]["cores"] = 8
        d["cpu"]["temps"]["cores"].pop(3)                                           # a core without a sensor
        self.assertEqual(render.cpu_core_of(d, ids), {})
        d["cpu"]["core_of"] = {i: 1 + i // 2 for i in ids}                          # the sampler says which core each CPU is on
        self.assertEqual(render.cpu_core_of(d, ids), {i: 1 + i // 2 for i in ids if 1 + i // 2 != 3})
        self.assertNotIn(4, render.cpu_core_temps(d, ids))
        lin = self.data()
        self.assertEqual(render.cpu_core_of(lin, list(range(16)))[9], 16 + 1)       # Linux: core ids as sysfs names them (the demo's map)

    def test_macos_shows_pressure_clusters_and_apple_cores(self):
        render.DEMO_OS = "darwin"
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        self.assertIn("Apple M2", txt)
        self.assertIn("P 4 + E 4 cores", txt)
        self.assertRegex(txt, r"thermal pressure Moderate +· +E-Cluster 2064 MHz 38% active +· +P-Cluster 3204 MHz 62% active")
        cells = grid_cells(lines)
        self.assertEqual([cells[i][0] for i in range(8)], ["E"] * 4 + ["P"] * 4)    # the tags from the clusters' cpus (JSON string keys)
        self.assertEqual([cells[i][2] for i in (0, 3, 4, 7)], ["2.06G", "1.94G", "3.24G", "3.20G"])
        self.assertTrue(all(c[3] is None for c in cells.values()))                  # no per-core sensor on this Mac: no temperature column
        self.assertFalse([x for x in lines if CELL.search(x) and "°C" in x])
        self.assertIn("hottest core ?", txt)
        self.assertIn("source smctemp", txt)
        self.assertRegex(txt, r"PKG +6\d°C +high \? +crit \?")                      # no limit known: no bar, '?'
        self.assertIn("ctxt ?/s", txt)
        self.assertRegex(txt, r"\n\s+1021 alice +U ")                              # the macOS uninterruptible state
        self.assertRegex("\n".join(self.once(["--select", "1400"], 200, 50)[1]), r"\n\s+1400 \? +S +\? +\? +\? +\? +\? +\? XprotectService")

    def test_columns_are_dropped_from_the_right_and_the_name_stays(self):
        d = self.data()
        seen = []
        for w in (78, 66, 60, 50, 40, 30, 20):
            lines = [render.ANSI.sub("", x) for x in render.cpu_view(d, w, 40)[0]]
            head = next(x for x in lines if re.match(r"\s+PID[▼▲ ]", x))
            self.assertTrue(head.rstrip().endswith("NAME"), (w, head))
            self.assertTrue(all(len(x) <= w for x in lines), w)
            seen.append([x.strip("▼▲") for x in head.split()[:-1]])
        self.assertEqual(seen[0], ["PID", "USER", "S", "NI", "THR", "CPU%", "MEM%", "RSS", "TIME"])
        self.assertEqual(seen[1], ["PID", "USER", "S", "NI", "THR", "CPU%", "MEM%", "RSS"])
        for a, b in zip(seen, seen[1:]):
            self.assertEqual(a[:len(b)], b)                                          # always from the right, never from the middle
        self.assertTrue(len(seen[-1]) < 6)

    def test_the_grid_has_two_to_four_columns_by_width(self):
        d = self.data()
        widths = {}
        for cols, rows in SIZES:
            lines = [render.ANSI.sub("", x) for x in render.cpu_view(d, cols - 1, rows - 2)[0]]
            widths[cols] = max(len(CELL.findall(x)) for x in lines)
        self.assertEqual(widths, {79: 2, 120: 3, 200: 4, 226: 4})

    def test_the_height_is_shared_and_what_does_not_fit_is_counted(self):
        d = self.data()
        full = [render.ANSI.sub("", x) for x in render.cpu_view(d, 119, 200)[0]]
        self.assertEqual(len(process_lines(full + ["x"])), 40)                      # plenty of room: everything, and no padding
        for h in range(4, 40):
            body = [render.ANSI.sub("", x) for x in render.cpu_view(d, 78, h)[0]]
            self.assertLessEqual(len(body), h, h)
        small = [render.ANSI.sub("", x) for x in render.cpu_view(d, 78, 22)[0]]
        self.assertRegex("\n".join(small), r"… \+\d+ more processes")                # no cursor (the rotation slide): the last line says it
        self.assertRegex("\n".join(small), r"… \+\d+ more lines|… \+\d+ more CPUs|TEMPERATURES")
        n = len(grid_cells(small))
        self.assertTrue(re.search(r"… \+%d more CPUs" % (16 - n), "\n".join(small)) or n == 16, (n, small))   # the CPUs left out are counted

    def test_unknown_values_are_question_marks_and_never_zero(self):
        def blank(d):
            d["cpu"].update(model=None, sockets=None, cores=None, kinds={}, cache={}, uptime=None, load=None,
                            rates={"ctxt": None, "intr": None, "running": None, "blocked": None})
            d["cpu"]["freq"] = {"cur": {}, "min": None, "max": None, "base": None, "governor": None, "driver": None}
            d["cpu"]["usage"]["total"] = None
            for r in d["cpu"]["usage"]["cores"]:
                r.update(busy=None, user=None, system=None, iowait=None)
            d["cpu"]["temps"] = {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None}
            d["cpu"]["throttle"] = {"package": None, "cores": {}, "package_s": None}
            d["procs"]["total"].update(running=None)
            d["procs"]["procs"] = [{"pid": 7, "ppid": None, "user": None, "name": "mystery", "state": None, "threads": None, "nice": None, "prio": None,
                                    "cpu": None, "mem": None, "mem_pct": None, "time": None, "start": None}]
        d = self.data(change=blank)
        render.DEMO = True
        demo.cpu_sample = lambda os_name=None, now=None: d["cpu"]
        demo.proc_sample = lambda os_name=None, now=None: d["procs"]
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        for want in ("sockets ?", "cores ?", "cache ?", "governor ?", "clock ?", "up ?", "load ?", "ctxt ?/s", "intr ?/s", "running ?", "blocked ?",
                     "PKG   ?", "hottest core ?", "throttled ?", "source ?"):
            self.assertIn(want, txt)
        self.assertEqual(len(re.findall(r"\s\d+ +[█▒░]+ +\?", txt)), 16)           # 16 CPUs, busy '?': never 0%
        self.assertNotIn("0%", txt.replace("100%", ""))
        self.assertRegex("\n".join(process_lines(lines)), r"\s+7 +\? +\? +\? +\? +\? mystery")
        self.assertIn("CPU% ?: measuring", txt)
        s, lines = self.once(["--details"], 200, 50)
        pane = "\n".join(lines)
        for want in ("parent    ?", "user      ?", "state     ?", "threads   ?", "nice      ?", "priority  ?", "CPU       ?", "memory    ?",
                     "CPU time  ?", "started   ?"):
            self.assertIn(want, pane)
        self.assertIn("── PROCESS 7 ", pane)

    def test_empty_data_says_what_is_missing_at_every_size(self):
        for cpu_os in (None, "windows", "darwin"):
            render.DEMO = False
            render.WINDOWS, render.MACOS = cpu_os == "windows", cpu_os == "darwin"
            render.cpuinfo = types.SimpleNamespace(CpuSampler=lambda: types.SimpleNamespace(sample=lambda: {
                "threads": 4, "arch": "x86_64", "usage": {"total": None, "cores": []}, "notes": ["not implemented yet"]}))
            render.procs = types.SimpleNamespace(ProcSampler=lambda: types.SimpleNamespace(sample=lambda: {
                "procs": [], "total": {"count": 0, "running": None, "threads": None, "unreadable": 0}, "notes": ["not implemented yet"]}))
            render.snapshot = lambda w: dict(cont=None, net=None, boot=None, baseline=None)
            render.Sampler = FakeSampler
            for cols, rows in SIZES:
                with self.subTest(os=cpu_os, size=(cols, rows)):
                    s, lines = self.once(["--details", "--select", "x"], cols, rows)
                    txt = "\n".join(lines)
                    self.assertIn("per-CPU usage: ? (the sampler gave none)", txt)
                    self.assertIn("processes: ? (the sampler gave none)", txt)
                    self.assertEqual(txt.count("not implemented yet"), 1)             # said once, not once per producer
                    self.assertIn("threads 4", txt)
                    self.assertIn("0/0", lines[-1])
                    self.assertNotIn("DETAILS", txt)
                    self.assertNotIn("PROCESS ", txt.replace("PROCESSES", ""))
                    if cpu_os:
                        self.assertIn("no sensors.json", txt)
            lines = self.once([], 120, 33)[1]
            self.assertIn("│ CPU │", lines[0])
        render.WINDOWS = render.MACOS = False

    def test_no_collector_file_and_a_stale_one_are_said_on_the_screen(self):
        render.DEMO, render.WINDOWS = False, True
        self.producers("windows")
        render.DEMO = False
        self.feed()
        render.Sampler = FakeSampler
        s, lines = self.once([], 200, 50)
        self.assertIn("temperatures: ? (no sensors.json: the collector is not running, or [features] cpu = no)", "\n".join(lines))
        with open(render.SENSORS, "w") as f:
            json.dump(demo.sensors("windows", NOW - 300), f)
        s, lines = self.once([], 200, 50)
        self.assertIn("sensors.json is stale (5 min old)", "\n".join(lines))
        with open(render.SENSORS, "w") as f:
            json.dump(demo.sensors("windows", NOW), f)
        s, lines = self.once([], 200, 50)
        txt = "\n".join(lines)
        self.assertNotIn("sensors.json", txt)
        self.assertIn("source LibreHardwareMonitor", txt)
        with open(render.SENSORS, "w") as f:
            f.write("{not json")
        self.assertIn("no sensors.json", "\n".join(self.once([], 200, 50)[1]))        # unreadable = absent


class Sorting(CpuCase):
    def test_each_sort_orders_by_its_column_with_unknowns_last_and_ties_by_pid(self):
        for os_name in OSES:
            d = self.data(os_name)
            pl = d["procs"]["procs"]
            for sort, field, rev in (("cpu", "cpu", True), ("mem", "mem", True), ("time", "time", True), ("pid", "pid", False)):
                rows = render.cpu_rows(pl, sort)
                self.assertEqual(sorted(p["pid"] for p in rows), sorted(p["pid"] for p in pl), (os_name, sort))
                known = [p for p in rows if p[field] is not None]
                self.assertEqual(rows[:len(known)], known, (os_name, sort))                    # unknowns after all the known ones
                vals = [p[field] for p in known]
                self.assertEqual(vals, sorted(vals, reverse=rev), (os_name, sort))
                unknown = [p["pid"] for p in rows[len(known):]]
                self.assertEqual(unknown, sorted(unknown), (os_name, sort))
            users = render.cpu_rows(pl, "user")
            names = [p["user"] for p in users if p["user"] is not None]
            self.assertEqual(names, sorted(names, key=str.lower), os_name)
            self.assertTrue(all(p["user"] is None for p in users[len(names):]))
        tie = [{"pid": 9, "cpu": 1.0}, {"pid": 3, "cpu": 1.0}, {"pid": 5, "cpu": 2.0}, {"pid": 4, "cpu": None}, {"pid": 1, "cpu": None}]
        self.assertEqual([p["pid"] for p in render.cpu_rows(tie, "cpu")], [5, 3, 9, 1, 4])
        self.assertEqual([p["pid"] for p in render.cpu_rows(tie, "bogus")], [5, 3, 9, 1, 4])  # an unknown name sorts by cpu

    def test_the_command_line_sorts_and_selects(self):
        d = self.data()
        first = lambda opts: re.match(r"\s+(\d+) ", process_lines(self.once(opts, 200, 50)[1])[0]).group(1)  # noqa: E731
        self.assertEqual(first([]), "2210")
        self.assertEqual(first(["--sort", "cpu"]), "2210")
        self.assertEqual(first(["--sort", "mem"]), "2350")                                       # java, 2.2G
        self.assertEqual(first(["--sort", "time"]), "2350")                                      # 50210 s of CPU time
        self.assertEqual(first(["--sort", "pid"]), "1")
        users = [re.match(r"\s+\d+ (\S+)", x).group(1) for x in process_lines(self.once(["--sort", "user"], 200, 50)[1])]
        self.assertEqual(users, sorted(users, key=str.lower))                                    # by user, A to Z (999 before alice)
        self.assertEqual(users[0], "1000")
        self.assertEqual(first(["--sort", "bogus"]), "2210")
        self.assertEqual(first(["--sort"]), "2210")
        s, lines = self.once(["--sort", "mem"], 200, 50)
        self.assertIn("by memory", "\n".join(lines))
        self.assertIn("sort:", lines[-1])
        marked = [x for x in s.split(LINE)[-1:] if ESC + "[1;7mM mem" in x]
        self.assertTrue(marked, "the sort in use is drawn reversed in the footer")
        for opts, pid in ((["--select", "ffmpeg"], 2210), (["--select", "FFMPEG"], 2210), (["--select", "postgres"], 1590), (["--select", "2900"], 2900),
                          (["--select", "kworker/4"], 288)):
            s, lines = self.once(opts, 200, 50)
            reverse = [render.ANSI.sub("", x) for x in s.split(LINE) if ESC + "[7m" in x]
            self.assertEqual(len(reverse), 1, opts)
            self.assertRegex(reverse[0], r"^\s+%d " % pid)                                         # the cursor row, highlighted
        s, lines = self.once(["--select", "no such process"], 200, 50)
        self.assertRegex([render.ANSI.sub("", x) for x in s.split(LINE) if ESC + "[7m" in x][0], r"^\s+2210 ")  # not found: the first row
        self.assertIn("1/40", lines[-1])


class Keys(CpuCase):
    """cpu_key on the demo's processes: the cursor, the sort, the details pane, leaving."""

    def press(self, cv, keys, d=None, page=10):
        """Keys as the console loop hands them to cpu_key (the rows sorted again and the cursor synced after 'rows').
        -> (the rows afterwards, what cpu_key returned for each key). After every key the cursor is on one of the rows."""
        d = d or self.data()
        rows = render.cpu_rows(d["procs"]["procs"], cv.sort)
        render.cpu_sync(cv, rows)
        acts = []
        for k in keys:
            act = render.cpu_key(cv, k, rows, page)
            if act == "rows":
                rows = render.cpu_rows(d["procs"]["procs"], cv.sort)
                render.cpu_sync(cv, rows)
            acts.append(act)
            if rows:
                self.assertTrue(0 <= cv.idx < len(rows), (k, cv.idx))
                self.assertEqual(cv.cur, rows[cv.idx]["pid"], k)
        return rows, acts

    def test_up_and_down_move_and_stop_at_the_ends(self):
        cv = render.CpuView(now=NOW)
        rows, acts = self.press(cv, ["up", "k"])
        self.assertEqual((cv.idx, acts), (0, ["", ""]))
        rows, acts = self.press(cv, ["down", "j", "down"])
        self.assertEqual((cv.idx, acts, cv.cur), (3, ["", "", ""], rows[3]["pid"]))
        n = len(rows)
        rows, _ = self.press(cv, ["down"] * (n + 5))
        self.assertEqual(cv.idx, n - 1)
        rows, _ = self.press(cv, ["up"])
        self.assertEqual(cv.idx, n - 2)

    def test_page_keys_move_a_page_and_stop_at_the_ends(self):
        cv, seen = render.CpuView(now=NOW), []
        n = len(self.data()["procs"]["procs"])
        for k in ("pgdn", "pgdn", "pgup", "end", "pgdn", "pgup", "home", "pgup"):
            self.press(cv, [k], page=7)
            seen.append(cv.idx)
        self.assertEqual(seen, [7, 14, 7, n - 1, n - 1, n - 8, 0, 0])

    def test_the_sort_keys_are_htops_letters_in_either_case(self):
        cv = render.CpuView(now=NOW)
        for key, sort in (("P", "cpu"), ("M", "mem"), ("T", "time"), ("N", "pid"), ("U", "user"), ("m", "mem"), ("p", "cpu"), ("t", "time"),
                          ("n", "pid"), ("u", "user")):
            rows, acts = self.press(cv, [key])
            self.assertEqual((acts, cv.sort), (["rows"], sort), key)
            self.assertEqual([p["pid"] for p in rows], [p["pid"] for p in render.cpu_rows(self.data()["procs"]["procs"], sort)], key)

    def test_the_cursor_follows_its_process_through_a_new_sort(self):
        cv = render.CpuView(now=NOW)
        self.press(cv, ["down", "down"])
        pid = cv.cur
        rows, _ = self.press(cv, ["n"])
        self.assertEqual((cv.cur, rows[cv.idx]["pid"]), (pid, pid))
        self.assertEqual(cv.idx, [p["pid"] for p in rows].index(pid))
        rows, _ = self.press(cv, ["m", "p"])
        self.assertEqual(cv.cur, pid)

    def test_enter_and_space_toggle_the_details_of_the_process_under_the_cursor(self):
        cv, d = render.CpuView(now=NOW), self.data()
        self.assertFalse(cv.details)
        rows, acts = self.press(cv, ["enter"], d)
        self.assertEqual((acts, cv.details), ([""], True))
        for pid in (2210, 1610, 2477, 2501):
            cv.cur = pid
            s = render.cpu_screen(d, [], cv, 119, 33)[0]
            txt = "\n".join(self.check_frame(s, 119, 33, pid))
            self.assertIn(f"── PROCESS {pid} ", txt)                                           # the pane follows the cursor
        rows, acts = self.press(cv, ["space"], d)
        self.assertEqual((acts, cv.details), ([""], False))
        self.assertNotIn("── PROCESS ", render.ANSI.sub("", render.cpu_screen(d, [], cv, 119, 33)[0]))

    def test_esc_q_leave_and_change_nothing(self):
        cv = render.CpuView(now=NOW)
        self.press(cv, ["down", "m"])
        before = (cv.idx, cv.cur, cv.details, cv.sort)
        for k in ("esc", "q"):
            rows, acts = self.press(cv, [k])
            self.assertEqual(acts, ["back"], k)
            self.assertEqual((cv.idx, cv.cur, cv.details, cv.sort), before, k)
        for k in ("c", "C", "tab", "3", "?", "r", "Z"):  # c is the overview's letter now, the others are the dispatcher's
            rows, acts = self.press(cv, [k])
            self.assertEqual(acts, [""], k)
            self.assertEqual((cv.idx, cv.cur, cv.details, cv.sort), before, k)
        self.press(cv, ["enter"])
        self.assertEqual(self.press(cv, ["esc", "esc"])[1], ["", "back"])  # the details pane closes first

    def test_other_keys_do_nothing_and_no_rows_is_not_an_error(self):
        cv = render.CpuView(now=NOW)
        self.press(cv, ["down", "down"])
        before = (cv.idx, cv.cur, cv.sort, cv.details)
        rows, acts = self.press(cv, ["x", "1", "", "f1", "PGDN", "tab", "left", "right", "e"])
        self.assertEqual((acts, (cv.idx, cv.cur, cv.sort, cv.details)), ([""] * 9, before))
        empty = {"cpu": self.data()["cpu"], "procs": {"procs": [], "total": {}}, "extra": {"notes": [], "pressure": None, "clusters": []}, "at": NOW}
        cv = render.CpuView(now=NOW)
        keys = ["up", "down", "pgup", "pgdn", "home", "end", "j", "k", "x"]
        rows, acts = self.press(cv, keys, empty)
        self.assertEqual((rows, acts, cv.idx, cv.cur), ([], [""] * len(keys), 0, None))
        self.assertEqual(self.press(cv, ["enter", "u", "esc", "esc"], empty)[1], ["", "rows", "", "back"])

    def test_the_cursor_stays_in_place_when_its_process_disappears(self):
        d = self.data()
        cv = render.CpuView(now=NOW)
        rows, _ = self.press(cv, ["down", "down", "down"], d)
        idx, pid = cv.idx, cv.cur
        d["procs"]["procs"] = [p for p in d["procs"]["procs"] if p["pid"] != pid]
        rows = render.cpu_rows(d["procs"]["procs"], "cpu")
        self.assertEqual(render.cpu_sync(cv, rows), idx)                                       # the same place, another process
        self.assertEqual(cv.cur, rows[idx]["pid"])
        d["procs"]["procs"] = d["procs"]["procs"][:2]
        rows = render.cpu_rows(d["procs"]["procs"], "cpu")
        self.assertEqual(render.cpu_sync(cv, rows), 1)                                         # fewer rows than before: the last one

    def test_the_selected_row_is_always_on_screen(self):
        d = self.data()
        n = len(d["procs"]["procs"])
        for cols, rows in SIZES:
            for details in (False, True):
                cv = render.CpuView(now=NOW)
                cv.details = details
                for step in list(range(n)) + list(range(n, 0, -1)):
                    self.press(cv, ["down" if step < n else "up"], d)
                    s, vis = render.cpu_screen(d, [], cv, cols - 1, rows)
                    chosen = [render.ANSI.sub("", x) for x in s.split(LINE) if ESC + "[7m" in x]
                    self.assertEqual(len(chosen), 1, (cols, rows, details, step, cv.idx))
                    self.assertRegex(chosen[0], r"^\s+%d " % cv.cur)

    def test_scrolling_keeps_the_row_away_from_the_edges(self):
        for n, rows_, i in ((40, 10, 20), (40, 10, 0), (40, 10, 39), (5, 10, 4), (40, 3, 20), (40, 1, 20), (40, 0, 20)):
            top = render.map_scroll(0, i, n, rows_)
            self.assertTrue(0 <= top <= max(0, n - rows_), (n, rows_, i, top))
            if rows_:
                self.assertTrue(top <= i < top + rows_, (n, rows_, i, top))


class Details(CpuCase):
    def pane(self, pid, cols=120, rows=33, os_name=None):
        render.DEMO_OS = os_name
        s, lines = self.once(["--select", str(pid), "--details"], cols, rows)
        return lines

    def test_every_field_of_the_contract_is_in_the_pane(self):
        d = self.data()
        p = self.procs(d, pid=2210)
        txt = "\n".join(self.pane(2210, 200, 50))
        pane = txt[txt.index("── PROCESS 2210"):]
        for want in ("ffmpeg", "parent    1  systemd", "user      alice", "state     R  running", "threads   18", "nice      10", "priority  30",
                     "CPU       ", "(100 % = one core)", "memory    3.9 %  640M resident", "CPU time  2:32:00", "children  0"):
            self.assertIn(want, pane)
        when = re.search(r"started   (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \((\d+) h ago\)", pane)
        self.assertTrue(when, pane)
        self.assertEqual(when.group(2), "7")                                                    # started 24320 s before the reading
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p["start"])), when.group(1))

    def test_the_parent_is_named_and_the_children_are_counted(self):
        kids = sum(1 for p in self.data()["procs"]["procs"] if p["ppid"] == 1)
        self.assertGreater(kids, 10)
        self.assertIn(f"children  {kids}", "\n".join(self.pane(1, 200, 50)))                  # systemd's
        txt = "\n".join(self.pane(1467, 200, 50))
        self.assertIn("parent    1  systemd", txt.replace("1  systemd", "1  systemd"))
        txt = "\n".join(self.pane(1520, 200, 50))
        self.assertIn("parent    1467  containerd-shim", txt)
        self.assertIn("children  3", txt)
        txt = "\n".join(self.pane(2477, 200, 50))
        self.assertIn("state     Z  zombie", txt)
        self.assertIn("parent    1610  node", txt)                                           # the zombie's parent is the one that must collect it
        txt = "\n".join(self.pane(2501, 200, 50))
        self.assertIn("parent    ?", txt)                                                    # unreadable: nothing known

    def test_a_parent_that_is_not_in_the_list_is_said(self):
        d = self.data(change=lambda d: d["procs"]["procs"].append({"pid": 5555, "ppid": 4444, "name": "orphan", "user": "x", "state": "S"}))
        demo.proc_sample = lambda os_name=None, now=None: d["procs"]
        self.assertIn("parent    4444  ? (not in the list)", "\n".join(self.pane(5555, 200, 50)))

    def test_the_pane_is_beside_the_table_when_wide_and_below_it_when_not(self):
        for cols, rows in SIZES:
            lines = self.pane(2210, cols, rows)
            at = next(i for i, x in enumerate(lines) if "── PROCESS 2210" in x)
            if cols - 1 >= render.CPU_PANE_W:
                self.assertTrue(all(x[x.index(" │ ") + 1] == "│" for x in lines[at:-1] if " │ " in x), cols)
                self.assertGreater(lines[at].index("── PROCESS 2210"), 80, cols)              # beside: the table is on its left
                self.assertTrue(re.match(r"── PROCESSES ", lines[at]), cols)
            else:
                self.assertTrue(lines[at].startswith("── PROCESS 2210"), cols)               # below: its own title line
                self.assertGreater(at, 10, cols)
            txt = "\n".join(lines[at:])
            self.assertIn("parent", txt, cols)
            self.assertIn("started", txt, cols)
            self.assertIn("children", txt, cols)

    def test_the_table_keeps_enough_rows_with_the_pane_open_on_a_small_screen(self):
        s, lines = self.once(["--select", "ffmpeg", "--details"], 79, 24)
        rows = process_lines(lines[:next(i for i, x in enumerate(lines) if "── PROCESS 2210" in x)] + ["x"])
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(sum(1 for x in lines if "── PROCESS 2210" in x), 1)

    def test_long_values_wrap_or_are_cut_never_overflow(self):
        d = self.data(change=lambda d: d["procs"]["procs"].append(
            {"pid": 777, "ppid": 1, "name": "n" * 64, "user": "u" * 80, "state": "S", "threads": 1, "nice": 0, "prio": 20, "cpu": 99.0, "mem": 5 * 2 ** 40,
             "mem_pct": 12345.0, "time": 10 ** 9, "start": NOW - 10 ** 9}))
        demo.proc_sample = lambda os_name=None, now=None: d["procs"]
        for cols, rows in SIZES:
            lines = self.pane(777, cols, rows)
            self.assertIn("── PROCESS 777", "\n".join(lines))


class Rotation(CpuCase):
    """[dashboard] cpu_in_rotation: the CPU as one of the rotating pages (a monitor nobody types on)."""

    def setUp(self):
        super().setUp()
        render.page_overview = overview_stub                                                    # the overview reads the host: not the subject here
        self.cont, self.net, self.boot, self.base = demo.snapshot(now=NOW)

    def slides(self, w, body_h, **kw):
        return render.slides(FakeSampler().sample(), self.cont, self.net, w, body_h, self.boot, self.base, mode="overview", **kw)

    def test_the_cpu_joins_the_rotation_when_asked(self):
        self.assertEqual([x[0] for x in self.slides(119, 31)], ["Overview"])
        render.CFG["cpu_in_rotation"] = True
        for cols, rows in SIZES:
            w, body_h = cols - 1, rows - 2
            sl = self.slides(w, body_h)
            self.assertEqual([x[0] for x in sl], ["Overview", "CPU"], cols)
            name, part, parts, body = sl[-1]
            self.assertEqual((part, parts), (1, 1))
            self.assertLessEqual(len(body), body_h)
            self.assertFalse([x for x in body if len(render.ANSI.sub("", x)) > w], cols)
            self.assertFalse([x for x in body if ESC + "[7m" in x], cols)                       # no cursor on a monitor without a keyboard
            txt = "\n".join(render.ANSI.sub("", x) for x in body)
            for want in ("── CPU ", "ALL ", "TEMPERATURES", "── PROCESSES", "ffmpeg"):
                self.assertIn(want, txt, cols)
            self.assertEqual(sorted(grid_cells(txt.split("\n"))), list(range(16)) if (cols, rows) != (79, 24) else sorted(grid_cells(txt.split("\n"))))
            frame = render.frame(sl[-1], 1, len(sl), w, rows, [], keys=False)
            self.check_frame(frame, w, rows, cols)
            self.assertIn("│ CPU │", render.ANSI.sub("", frame).split("\r\n")[0])
        sl = self.slides(119, 31)
        self.assertEqual(render.pick_slide(sl, sum(render.slide_seconds(x, len(sl)) for x in sl) - 1), 1)   # its turn comes
        self.assertEqual([x[0] for x in self.slides(119, 31, scroll=True)], ["Overview"])        # the scrolling web page: one page only
        render.CFG["map_in_rotation"] = True
        self.assertEqual([x[0] for x in self.slides(119, 31)], ["Overview", "Map", "CPU"])

    def test_the_slide_shows_the_top_processes_that_fit_and_counts_the_rest(self):
        render.CFG["cpu_in_rotation"] = True
        body = self.slides(78, 22)[-1][3]
        txt = "\n".join(render.ANSI.sub("", x) for x in body)
        self.assertRegex(txt, r"… \+\d+ more processes")
        rows = process_lines(txt.split("\n") + ["x"])
        self.assertIn("ffmpeg", rows[0])
        big = "\n".join(render.ANSI.sub("", x) for x in self.slides(199, 90)[-1][3])
        self.assertNotIn("more processes", big)
        self.assertEqual(len(process_lines(big.split("\n") + ["x"])), 40)

    def test_no_slide_when_off_or_when_the_feature_is_disabled(self):
        render.CFG["cpu_in_rotation"] = False
        self.assertNotIn("CPU", [x[0] for x in self.slides(199, 48)])
        render.CFG["cpu_in_rotation"] = True
        render.CFG["features"]["cpu"] = False
        self.assertNotIn("CPU", [x[0] for x in self.slides(199, 48)])
        frame = render.frame(self.slides(199, 48)[0], 0, 1, 199, 50, [], keys=True)
        self.assertNotIn("c: cpu", render.ANSI.sub("", frame))                                   # and the footer does not offer it

    def test_the_lazy_slide_is_empty_until_it_is_the_one_shown(self):
        render.CFG["cpu_in_rotation"] = True
        log = self.producers()
        self.feed()
        sl = self.slides(119, 31, cpu_lazy=True)
        self.assertEqual([x[0] for x in sl], ["Overview", "CPU"])
        self.assertEqual((log["cpu_made"], log["proc_made"], log["proc_sampled"]), (0, 0, 0))   # not shown: nothing sampled, nothing created
        self.assertNotIn("PROCESSES", "\n".join(sl[1][3]))
        feed = render.CpuFeed()
        render.fill_cpu(sl, 0, 119, 31, feed)
        self.assertEqual(log["proc_sampled"], 0)                                                 # the overview is the one shown: still nothing
        render.fill_cpu(sl, 1, 119, 31, feed)
        self.assertEqual((log["cpu_made"], log["proc_made"], log["proc_sampled"]), (1, 1, 1))
        self.assertIn("PROCESSES", "\n".join(render.ANSI.sub("", x) for x in sl[1][3]))

    def test_a_broken_cpu_slide_is_an_error_line_not_a_crash(self):
        render.CFG["cpu_in_rotation"] = True

        def broken(*a, **kw):
            raise ValueError("bad state " + ESC + "[2J")
        render.cpu_slide = broken
        sl = self.slides(119, 31)
        line = sl[-1][3][0]
        self.assertEqual(sl[-1][0], "CPU")
        self.assertIn("error on page CPU", render.ANSI.sub("", line))
        self.assertNotIn(ESC + "[2J", line)
        lazy = self.slides(119, 31, cpu_lazy=True)
        render.fill_cpu(lazy, 1, 119, 31, render.CpuFeed())
        self.assertIn("error on page CPU", render.ANSI.sub("", lazy[1][3][0]))

    def test_config_cpu_in_rotation_yes_no_and_not_a_boolean(self):
        path = os.path.join(self.tmp.name, "config.ini")
        got = {}
        for value in ("yes", "no", "on", "maybe"):
            with open(path, "w", encoding="utf-8") as f:
                f.write("[dashboard]\ncpu_in_rotation = " + value + "\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got[value] = (nuc_config.load(path)["cpu_in_rotation"], err.getvalue())
        self.assertEqual((got["yes"], got["no"], got["on"]), ((True, ""), (False, ""), (True, "")))
        self.assertFalse(got["maybe"][0])
        self.assertIn("[dashboard] cpu_in_rotation is not a boolean: kept off", got["maybe"][1])
        with open(path, "w", encoding="utf-8") as f:
            f.write("[dashboard]\ncpu_in_rotation = yes\n[features]\ncpu = no\n")
        cfg = nuc_config.load(path)
        self.assertEqual((cfg["cpu_in_rotation"], cfg["features"]["cpu"]), (True, False))

    def test_render_screen_reads_the_processes_only_for_the_slide_it_shows(self):
        render.CFG["cpu_in_rotation"] = True
        log = self.producers()
        self.feed((self.cont, self.net, self.boot))
        saved = render.slides
        render.slides = lambda *a, **kw: saved(*a, **dict(kw, mode="overview"))
        smp = FakeSampler()
        smp.sample = lambda: {"cpu": {}, "thermal": {}, "net": {}, "sessions": None, "fs": None}
        for n, want in ((0, 0), (1, 1)):
            out, count = render.render_screen(smp, 119, 33, n=n)
            self.assertEqual(log["proc_sampled"] >= 1, bool(want), n)
        out, _ = render.render_screen(smp, 119, 33, at=0)
        self.assertNotIn("PROCESSES", render.ANSI.sub("", out))

    def test_the_kiosk_page_reads_the_processes_only_while_the_cpu_slide_is_shown(self):
        render.CFG["cpu_in_rotation"] = True
        log = self.producers()
        self.feed((self.cont, self.net, self.boot))
        render.Sampler, render.MODE = FakeSampler, "overview"
        pages, steps = [], [40, 10, 5, 8, 40]                                                   # frames at 0 s, 40 s, 50 s, 55 s, 63 s, 103 s of the rotation
        render.write_text_atomic = lambda path, text: pages.append((text, dict(log)))

        def sleep(sec):
            if not steps:
                raise Stop()
            self.now += steps.pop(0)
        self.on_sleep = sleep
        with self.assertRaises(Stop), contextlib.redirect_stderr(io.StringIO()):
            render.kiosk_file(["render.py", "--no-browser", "--html", os.path.join(self.tmp.name, "k.html")], self.tmp.name, 120, 33)
        shows = [" │ CPU │ " in text for text, _ in pages]
        self.assertEqual(shows, [False, False, True, True, False, False])
        self.assertEqual([x["proc_sampled"] for _, x in pages][:2], [0, 0])                     # nobody looks at the CPU: nothing is made or read
        self.assertEqual(pages[3][1]["proc_made"], 1)                                            # one pair for the stay on the CPU slide
        self.assertEqual(pages[5][1]["proc_made"], 1)                                            # and nothing new for the overview afterwards
        self.assertEqual(pages[5][1]["proc_sampled"], pages[3][1]["proc_sampled"])
        self.assertEqual(pages[3][1]["proc_sampled"], 2)


class Footer(CpuCase):
    def test_the_overview_offers_the_cpu_screen_when_there_is_a_keyboard_and_the_feature_is_on(self):
        slide = ("Overview", 1, 1, ["x"])
        on = render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True))
        self.assertIn("1-5: screens", on)                                                         # 3 is the CPU screen
        self.assertNotIn("screens", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=False)))   # the web page: no keys
        self.assertIn("1 2 4 5: screens", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True, cpukey=False)))
        self.assertIn("1 3: screens", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=False, cpukey=True)))
        render.CFG["features"]["cpu"] = False
        self.assertIn("1 2 4 5: screens", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True)))
        render.CFG["features"]["cpu"] = True
        render.CFG["features"]["map"] = False
        self.assertIn("1 3-5: screens", render.ANSI.sub("", render.frame(slide, 0, 1, 119, 33, [], keys=True)))
        two = render.ANSI.sub("", render.frame(("Overview", 1, 2, ["x"]), 0, 2, 79, 24, [], keys=True)).split("\r\n")[-1]
        self.assertIn("1 3-5: screens", two)                                                      # and it fits the narrowest footer
        self.assertLessEqual(len(two), 79)

    def test_the_cpu_footer_gives_up_keys_from_the_least_needed_when_narrow(self):
        cv = render.CpuView(now=NOW)
        seen = []
        for w in (200, 100, 78, 60, 40, 24, 12):
            f = render.ANSI.sub("", render.cpu_footer(cv, 40, w))
            self.assertLessEqual(len(f), w, w)
            seen.append(f)
        self.assertIn("PgUp/PgDn/Home/End: page", seen[0])
        self.assertIn("Esc: back", seen[0])
        self.assertIn("?: help", seen[0])
        self.assertIn("sort: P cpu  M mem  T time  N pid  U user", seen[0])
        self.assertIn("Esc: back", seen[2])                                                     # the way back is the last thing to go
        self.assertIn("P cpu", seen[2])
        self.assertNotIn("PgUp", seen[2])
        cv.details = True
        self.assertIn("Enter: hide details", render.ANSI.sub("", render.cpu_footer(cv, 40, 200)))
        cv.cur, cv.idx = 5, 5
        self.assertIn("row 6/40", render.ANSI.sub("", render.cpu_footer(cv, 40, 140)))
        self.assertIn("0/0", render.ANSI.sub("", render.cpu_footer(cv, 0, 140)))


class MainLoop(CpuCase):
    """render.main() on a fake terminal: `c` opens the CPU screen, its keys move and sort it, c/Esc/q go back, an idle screen gives
    the monitor back, the samplers run only while it is shown."""

    def run_main(self, script, keyboard=True, cols=80, rows=24):
        """script: what each keyboard read returns (raw bytes), a number of seconds that pass without a key, or (seconds, bytes):
        a key typed that long after the frame. -> (the frames drawn, ANSI stripped, each a list of lines; what tcsetattr got back)."""
        out, err, restored, todo = io.StringIO(), io.StringIO(), [], list(script)
        self.waits = []

        def next_read(fd=None, wait=None):
            if not todo:
                raise Stop()
            x = todo.pop(0)
            self.waits.append(wait)
            if isinstance(x, tuple):
                self.now += x[0]
                x = x[1]
            if isinstance(x, bytes):
                return render.decode_keys(x)
            self.now += x
            return []
        render.sys = Proxy(sys, stdin=types.SimpleNamespace(fileno=lambda: 5), stdout=out, stderr=err)
        render.os = Proxy(os, isatty=lambda fd: keyboard)
        render.termios = types.SimpleNamespace(tcgetattr=lambda fd: ["attrs", fd], TCSADRAIN=1,
                                               tcsetattr=lambda fd, when, attrs: restored.append(attrs))
        render.tty = types.SimpleNamespace(setcbreak=lambda fd: None)
        render.signal = Proxy(signal, signal=lambda *a: None)
        render.shutil = Proxy(shutil, get_terminal_size=lambda fallback=None: os.terminal_size((cols, rows)))
        render.WINDOWS, render.MODE, render.Sampler = False, "overview", FakeSampler
        render.read_keys, render.page_overview = next_read, overview_stub
        self.on_sleep = lambda sec: next_read()                                                  # no keyboard: the loop sleeps between frames
        with self.assertRaises(Stop):
            render.main(["render.py"])
        text = out.getvalue()
        self.assertTrue(text.endswith("\x1b[?25h\x1b[0m"))                                       # the cursor is given back
        frames = []
        for chunk in text[:-len("\x1b[?25h\x1b[0m")].split("\x1b[H")[1:]:
            chunk = chunk[:-len("\x1b[2J")] if chunk.endswith("\x1b[2J") else chunk
            frames.append(self.check_frame(chunk, cols - 1, rows, len(frames)))
        return frames, restored

    @staticmethod
    def is_cpu(frame):
        return " │ CPU │ " in frame[0] and "── CPU " in frame[1]

    @staticmethod
    def row(frame):
        return int(re.search(r"(\d+)/(\d+)", frame[-1]).group(1))

    def setUp(self):
        super().setUp()
        self.feed()

    def test_c_opens_the_cpu_screen_its_keys_move_and_sort_it_c_and_esc_go_back(self):
        frames, restored = self.run_main([b"c", b"\x1b[B\x1bOB", b"\x1b[B", b"\r", b"\x1b[6~", b"\x1b[H", b"M", b"1", b"c", b"\x1b"])
        self.assertEqual([self.is_cpu(f) for f in frames], [False, True, True, True, True, True, True, True, False, True, False])
        self.assertIn("1-5: screens", frames[0][-1])                                              # a keyboard: the footer offers the screens (3 is this one)
        self.assertEqual(self.row(frames[1]), 1)
        self.assertEqual(self.row(frames[2]), 3)                                                  # ↓ (CSI) ↓ (SS3)
        self.assertEqual(self.row(frames[3]), 4)
        self.assertNotIn("── PROCESS ", "\n".join(frames[3]))
        self.assertIn("── PROCESS ", "\n".join(frames[4]))                                       # Enter
        self.assertGreater(self.row(frames[5]), 4)                                                # PgDn
        self.assertEqual(self.row(frames[6]), 1)                                                  # Home
        self.assertIn("by memory", "\n".join(frames[7]))                                          # M
        self.assertNotIn("CPU", frames[8][0])                                                     # 1: back to the dashboard
        self.assertIn("overview page (stub)", "\n".join(frames[8]))
        self.assertIn("by CPU%", "\n".join(frames[9]))                                            # a new screen: the defaults again
        self.assertNotIn("── PROCESS ", "\n".join(frames[9]))
        self.assertEqual(restored, [["attrs", 5]])                                                # the terminal's settings are put back

    def test_q_goes_back_too_and_the_rotation_goes_on_where_it_was(self):
        render.CFG["cpu_in_rotation"] = False
        frames, _ = self.run_main([b"c", 3, b"q", 1])
        self.assertEqual([self.is_cpu(f) for f in frames], [False, True, True, False, False])

    def test_an_idle_cpu_screen_gives_the_monitor_back_to_the_rotation(self):
        idle = render.CPU_IDLE_S
        frames, _ = self.run_main([b"c", idle - 1, b"\x1b[B", idle - 1, 2])
        self.assertEqual([self.is_cpu(f) for f in frames], [False, True, True, True, True, False])
        self.assertEqual(self.row(frames[4]), 2)                                                  # a key restarts the count

    def test_the_data_is_read_every_refresh_and_a_key_only_redraws(self):
        log = self.producers()
        render.DEMO = False
        self.feed()
        frames, _ = self.run_main([b"c", b"\x1b[B", b"\x1b[B", render.REFRESH_S, render.REFRESH_S])
        self.assertTrue(all(self.is_cpu(f) for f in frames[1:]))
        self.assertEqual((log["cpu_made"], log["proc_made"]), (1, 1))                             # created when the screen opened
        self.assertEqual(log["proc_sampled"], 3)                                                  # 1st frame, then each refresh: not each key
        self.assertEqual(log["cpu_sampled"], 3)
        self.assertEqual(self.waits[:2], [render.REFRESH_S, min(1.0, render.REFRESH_S)])           # the first reading is soon followed by another

    def test_processes_are_not_sampled_before_the_screen_opens_or_after_it_closes(self):
        log = self.producers()
        render.DEMO = False
        self.feed()
        self.run_main([2, 2, b"c", 2, b"1", 2, 2, 2])
        made_while_open = (log["cpu_made"], log["proc_made"])
        self.assertEqual(made_while_open, (1, 1))
        self.assertEqual(log["proc_sampled"], 2)                                                  # the frame it opened with, one refresh later
        log2 = self.producers()
        self.run_main([2, 2, 2])
        self.assertEqual((log2["cpu_made"], log2["proc_made"], log2["proc_sampled"]), (0, 0, 0))   # the dashboard alone never touches them
        log3 = self.producers()
        self.run_main([b"c", b"1", 2, 2])
        self.assertEqual(log3["proc_made"], 1)
        self.run_main([b"c", b"1", b"c", b"1"])
        self.assertEqual(log3["proc_made"], 3)                                                    # each opening: fresh samplers, no old averages

    def test_without_a_keyboard_the_cpu_screen_is_not_offered(self):
        frames, _ = self.run_main([2, 2], keyboard=False)
        self.assertEqual(len(frames), 3)
        self.assertFalse([f for f in frames if self.is_cpu(f) or "screens" in f[-1]])

    def test_feature_off_c_does_nothing(self):
        render.CFG["features"]["cpu"] = False
        frames, _ = self.run_main([b"c", b"3", b"m"])
        self.assertFalse([f for f in frames if self.is_cpu(f)])
        self.assertNotIn("1-5: screens", frames[0][-1])
        self.assertIn("1 2 4 5: screens", frames[0][-1])
        self.assertEqual(len(frames), 4)

    def test_the_cpu_slide_runs_its_samplers_only_while_it_is_the_one_on_screen(self):
        render.CFG["cpu_in_rotation"] = True
        log = self.producers()
        render.DEMO = False
        self.feed((None, None, None))
        frames, _ = self.run_main([0, render.CFG["overview_seconds"] - 1, 0, 1, 1, 1])
        shown = [f for f in frames if " │ CPU │ " in f[0]]
        self.assertTrue(shown)
        self.assertEqual(log["proc_sampled"], len(shown))                                          # one reading per CPU frame, none for the others
        self.assertEqual(log["proc_made"], 1)

    def test_a_broken_cpu_screen_is_an_error_frame_esc_still_works_and_it_retries(self):
        log = self.producers(boom=("cpu",))
        render.DEMO = False
        self.feed()
        calls = []
        real = render.cpu_screen

        def broken(d, pb, cv, w, h):
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("bad state " + ESC + "[2J")
            return real(d, pb, cv, w, h)
        render.cpu_screen = broken
        frames, _ = self.run_main([b"c", b"x", b"x", b"1"])
        self.assertIn("error on the CPU screen", "\n".join(frames[1]))
        self.assertNotIn(ESC, "".join("".join(f) for f in frames))
        before = int(re.search(r"(\d+) (PROBLEMS|WARNINGS)", frames[0][0]).group(1))
        self.assertIn(f"{before + 1} PROBLEMS", frames[1][0])                                      # one more: it could not be drawn
        self.assertTrue(self.is_cpu(frames[3]))                                                    # drawn again once it works
        self.assertFalse(self.is_cpu(frames[-1]))                                                  # and 1 still went back

    def test_the_status_pill_is_the_dashboards_while_the_cpu_screen_is_open(self):
        data = demo.snapshot(now=NOW)
        self.feed(data[:3])
        frames, _ = self.run_main([b"c", 1])
        self.assertRegex(frames[0][0], r"PROBLEMS|WARNINGS|ALL OK|EXPOSED")
        self.assertEqual(frames[1][0][-20:].strip(), frames[0][0][-20:].strip())

    def test_the_command_line_prints_the_cpu_screen_for_each_demo_os(self):
        for os_name, note in (("windows", "LibreHardwareMonitor"), ("darwin", "thermal pressure Moderate"), ("", "coretemp")):
            argv = ["render.py", "--once", "--demo", "--view", "cpu", "--cols", "120", "--rows", "33", "--sort", "mem"]
            argv += ["--demo-os", os_name] if os_name else []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertIsNone(render.once(argv))
            self.assertEqual(render.DEMO_OS, os_name or None)
            text = out.getvalue()
            self.assertTrue(text.endswith("\n"))
            lines = text[:-1].split("\r\n")
            self.assertEqual(len(lines), 33, os_name)
            self.assertFalse([x for x in lines if len(x) > 119], os_name)
            self.assertNotIn(ESC, text)                                                           # without --color: plain text
            self.assertIn(note, text, os_name)
            self.assertIn("by memory", text, os_name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render.once(["render.py", "--once", "--demo", "--view", "cpu", "--color", "--cols", "120", "--rows", "33"])
        self.assertIn(ESC + "[", out.getvalue())                                                  # --color keeps them (tools/ansi2svg.py)

    def test_the_command_line_refuses_the_cpu_screen_when_the_feature_is_off(self):
        render.CFG["features"]["cpu"] = False
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.assertEqual(render.once(["render.py", "--once", "--demo", "--view", "cpu"]), 2)
        self.assertIn("[features] cpu = no", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class Hostile(CpuCase):
    """Names come from processes, sensors and the collector: never a raw escape sequence on the console."""

    RAW = (ESC + "[2J", ESC + "]0", BEL, CSI8, "\t")
    SHOWN = "x?[2Jy?]0;owned?z?31m??<b>&amp;"

    def poison(self):
        def procs_(os_name=None, now=None):
            r = real_proc(os_name, now)
            for p in r["procs"][:8]:
                p["name"] = EVIL
                p["user"] = EVIL
            r["procs"].append({"pid": 31337, "ppid": r["procs"][0]["pid"], "user": EVIL, "name": EVIL, "state": ESC + "[2J", "threads": 1, "nice": 0,
                               "prio": 20, "cpu": 99.9, "mem": 100, "mem_pct": 0.1, "time": 5.0, "start": NOW - 5})
            r["notes"] = ["note " + EVIL]
            return r

        def cpu_(os_name=None, now=None):
            r = real_cpu(os_name, now)
            r["model"], r["vendor"], r["arch"] = EVIL, EVIL, EVIL
            r["freq"].update(governor=EVIL, driver=EVIL)
            r["temps"].update(source=EVIL, sensors=[{"label": EVIL, "c": 50.0, "high": None, "crit": None}])
            r["notes"] = ["cpu note " + EVIL]
            return r

        def sens(os_name=None, now=None):
            r = real_sens(os_name, now)
            if r:
                r["cpu"].update(source=EVIL, pressure=EVIL, sensors=[{"label": EVIL, "c": 50.0}],
                                clusters=[{"name": EVIL, "mhz": 1000.0, "active": 5.0, "cpus": {"0": 1.0}}])
                r["errors"] = {EVIL: EVIL}
            return r
        real_proc, real_cpu, real_sens = demo.proc_sample, demo.cpu_sample, demo.sensors
        demo.proc_sample, demo.cpu_sample, demo.sensors = procs_, cpu_, sens

    def test_control_characters_in_names_never_reach_the_console(self):
        self.poison()
        for os_name in OSES:
            render.DEMO_OS = os_name
            for cols, rows in SIZES:
                for opts in ([], ["--select", "31337", "--details"], ["--sort", "user", "--details"], ["--sort", "pid", "--select", "x?[2J", "--details"]):
                    with self.subTest(os=os_name, size=(cols, rows), opts=opts):
                        s, lines = self.once(opts, cols, rows)                                    # check_frame: only SGR and CRLF survive
                        for x in self.RAW:
                            self.assertNotIn(x, s)
                        self.assertEqual(s.count("\r"), rows - 1)
                        self.assertEqual(s.count("\n"), rows - 1)
        render.DEMO_OS = None
        s, lines = self.once(["--select", "31337", "--details", "--sort", "pid"], 226, 50)
        txt = "\n".join(lines)
        self.assertGreaterEqual(txt.count(self.SHOWN), 3)                                         # drawn, '?' for every control character
        self.assertIn("note " + self.SHOWN, txt)
        self.assertIn("cpu note " + self.SHOWN, txt)
        self.assertIn("── CPU ", txt)
        self.assertIn(self.SHOWN, lines[1])                                                       # the model
        self.assertIn("state     ?", txt)                                                         # a state that is no letter: its first character
        for os_name in ("windows", "darwin"):
            render.DEMO_OS = os_name
            txt = "\n".join(self.once(["--details"], 226, 50)[1])
            self.assertIn(self.SHOWN, txt)
            self.assertIn("source " + self.SHOWN, txt)

    def test_the_rotation_slide_draws_the_same_names_clean(self):
        self.poison()
        render.CFG["cpu_in_rotation"] = True
        render.page_overview = overview_stub
        cont, net, boot, base = demo.snapshot(now=NOW)
        body = render.slides(FakeSampler().sample(), cont, net, 225, 48, boot, base, mode="overview")[-1][3]
        frame = render.frame(("CPU", 1, 2, body), 1, 2, 225, 50, [], keys=False)
        self.check_frame(frame, 225, 50)
        self.assertIn(self.SHOWN, render.ANSI.sub("", frame))

    def test_the_renderer_cleans_what_it_is_handed_whatever_the_producer_did(self):
        d = self.data()
        for p in d["procs"]["procs"][:5]:
            p["name"], p["user"] = EVIL, EVIL
        d["procs"]["procs"][0]["state"] = "R" + ESC
        d["cpu"]["kinds"] = {"P": [0, 1], "E": [2]}
        for cols, rows in SIZES:
            body = render.cpu_view(d, cols - 1, rows - 2, cur=d["procs"]["procs"][0]["pid"], details=True)[0]
            frame = render.frame(("CPU", 1, 1, body), 0, 1, cols - 1, rows, [])
            self.check_frame(frame, cols - 1, rows, cols)
            self.assertNotIn(ESC + "[2J", frame)


if __name__ == "__main__":
    unittest.main()
