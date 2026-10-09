"""Where the dashboard gets the host's own figures (RAM, root disk, uptime, load): from the Sampler, once per sample, and from nowhere else.

The SYSTEM block of the overview and the System page of the rotation draw what `Sampler.sample()` hands them; they never call /proc,
statvfs or hostinfo themselves. So `--demo` can replace the figures (tests/test_demo_host.py), and a figure the host cannot give is
drawn as `?`. The /proc files are fake here (a dict of path -> text behind `open`), the Windows and macOS readers are fake `hostinfo`
functions: every test runs the same on Linux, macOS and Windows.
"""
import io
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # the test helpers (cardlines.py)
import hostinfo  # noqa: E402
import render  # noqa: E402
import cardlines  # noqa: E402
import cards  # noqa: E402
import ansi  # noqa: E402

GIB = 2 ** 30
STAT = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0 0 0\nintr 1 2 3\n"
NETDEV = ("Inter-|   Receive                                                |  Transmit\n"
          " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
          "  eth9: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n")
MEMINFO = ("MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    6144000 kB\nBuffers:           10000 kB\n"
           "Cached:          2048000 kB\nSwapTotal:       2097152 kB\nSwapFree:        2097000 kB\nHugePages_Total:       0\n")
FILES = {"/proc/stat": STAT, "/proc/net/dev": NETDEV, "/proc/meminfo": MEMINFO, "/proc/loadavg": "0.82 0.64 0.51 1/456 12345\n",
         "/proc/uptime": "93784.50 400000.00\n"}
real_open = open


class Statvfs(object):
    """os.statvfs("/"): 480 blocks of 1 GiB, 300 of them free (180 GiB used)."""
    f_frsize, f_blocks, f_bfree = GIB, 480, 300


def text(lines):
    return ansi.ANSI.sub("", "\n".join(lines))


class FakeProc(unittest.TestCase):
    """The Linux readers behind a fake /proc: `self.files` maps a path to its text, or to the exception that reading it raises;
    `self.reads` lists the paths opened; `self.statvfs` is what statvfs("/") gives (or the exception it raises)."""

    def setUp(self):
        self.files, self.reads, self.statvfs = dict(FILES), [], Statvfs
        self.saved = (render.LINUX, render.cached, render.read_thermal)
        render.LINUX = True                           # the /proc readers, whatever the OS the tests run on
        render.cached = lambda key, ttl, fn: None     # no background thread (loginctl, statvfs of every mount): not what is tested
        render.read_thermal = lambda: {"throttle": None, "throttle_s": None, "clk": None}
        for p in (mock.patch("builtins.open", self.fake_open), mock.patch("os.statvfs", self.fake_statvfs, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        render.LINUX, render.cached, render.read_thermal = self.saved

    def fake_open(self, path, *a, **kw):
        if path in self.files:
            self.reads.append(path)
            if isinstance(self.files[path], Exception):
                raise self.files[path]
            return io.StringIO(self.files[path])
        return real_open(path, *a, **kw)

    def fake_statvfs(self, path):
        self.reads.append("statvfs " + path)
        if isinstance(self.statvfs, Exception):
            raise self.statvfs
        return self.statvfs

    def sample(self):
        return render.Sampler().sample()


class SamplerHostFigures(FakeProc):
    def test_linux_figures_come_from_proc_and_statvfs(self):
        sm = self.sample()
        self.assertEqual(sm["mem"], {"MemTotal": 16384000 * 1024, "MemFree": 1000000 * 1024, "MemAvailable": 6144000 * 1024, "Buffers": 10000 * 1024,
                                     "Cached": 2048000 * 1024, "SwapTotal": 2097152 * 1024, "SwapFree": 2097000 * 1024, "HugePages_Total": 0})
        self.assertEqual(sm["uptime"], 93784.5)
        self.assertEqual(sm["load"], ["0.82", "0.64", "0.51"])
        self.assertEqual(sm["disk_root"], (180 * GIB, 480 * GIB, "/"))
        self.assertEqual(sorted(sm["cpu"]), ["cpu0", "cpu1"])  # the other figures are still there

    def test_each_figure_is_read_once_per_sample_and_never_while_drawing(self):
        smp = render.Sampler()
        del self.reads[:]
        sm = smp.sample()
        for path in ("/proc/meminfo", "/proc/loadavg", "/proc/uptime", "statvfs /"):
            self.assertEqual(self.reads.count(path), 1, path)
        del self.reads[:]
        for k in (-2, 0, 3):
            cardlines.ov_sistema(sm, 119, k)
        render.page_sistema(sm, 119, cont=None)
        self.assertEqual(self.reads, [], "a block read the host while it was drawing: the sample holds the figures")

        def boom(*a):
            raise AssertionError("a block called a reader of the host")
        with mock.patch.multiple(render, meminfo=boom, loadavg=boom, uptime_s=boom, root_disk=boom, read_thermal=boom):
            self.assertIn("RAM", text(cardlines.ov_sistema(sm, 119, 0)))
            self.assertIn("RAM", text(render.page_sistema(sm, 119, cont=None)))

    def test_an_unreadable_figure_is_none_and_the_others_are_kept(self):
        self.files.update({"/proc/meminfo": OSError(13, "denied"), "/proc/uptime": "garbage", "/proc/loadavg": "garbage"})
        self.statvfs = OSError(5, "I/O error")
        sm = self.sample()
        self.assertEqual((sm["mem"], sm["uptime"], sm["load"], sm["disk_root"]), (None, None, None, None))
        self.assertEqual(sorted(sm["cpu"]), ["cpu0", "cpu1"], "one unreadable file must not take the rest of the sample")
        self.files.update({"/proc/meminfo": MEMINFO, "/proc/uptime": "5.00 1.00", "/proc/loadavg": ""})
        self.statvfs = Statvfs
        sm = self.sample()
        self.assertEqual((sm["mem"]["MemTotal"], sm["uptime"], sm["load"], sm["disk_root"][1]), (16384000 * 1024, 5.0, None, 480 * GIB))

    def test_a_meminfo_that_is_not_meminfo_is_unknown_not_a_crash(self):
        for junk in ("nonsense\n", "MemTotal: lots kB\n", "MemTotal: 1: 2 kB\n"):
            self.files["/proc/meminfo"] = junk
            self.assertIsNone(self.sample()["mem"], junk)

    def test_a_load_average_that_exists_but_is_unreadable_is_none_not_nothing(self):
        for junk in (OSError(5, "I/O error"), "", "0.5\n"):
            self.files["/proc/loadavg"] = junk
            self.assertIsNone(self.sample()["load"], junk)


class DrawnFromTheSample(FakeProc):
    """What the two blocks say for a sample: the figures, in the format they had when they read the host themselves."""

    def test_system_block(self):
        lines = text(cardlines.ov_sistema(self.sample(), 119, 0)).split("\n")
        self.assertTrue(lines[0].startswith("── SYSTEM "))
        self.assertTrue(lines[0].endswith("up 1d 2h · load 0.82 0.64 0.51"), lines[0])
        self.assertRegex(lines[1], r"^ RAM   █+░+ 9\.8G/15\.6G   cache 2\.0G$")
        self.assertRegex(lines[2], r"^ DISK  █+░+ 180\.0G/480\.0G$")

    def test_system_page(self):
        lines = text(render.page_sistema(self.sample(), 119, cont=None)).split("\n")
        self.assertEqual(lines[0], " up 1d 2h   load 0.82 0.64 0.51")
        self.assertRegex(lines[2], r"^ RAM   █+░+ 9\.8G/15\.6G  cache 2\.0G$")
        self.assertRegex(lines[3], r"^ SWAP  █*░+ 0M/2\.0G$")
        self.assertRegex(lines[4], r"^ DISK  █+░+ 180\.0G/480\.0G  /$")

    def test_no_swap_no_swap_line_and_no_load_where_the_os_has_none(self):
        sm = dict(self.sample(), mem={"MemTotal": 8 * GIB, "MemAvailable": 2 * GIB, "Cached": GIB, "SwapTotal": 0, "SwapFree": 0}, load=[],
                  disk_root=(10 * GIB, 100 * GIB, "C:"))
        page = text(render.page_sistema(sm, 119, cont=None))
        self.assertNotIn("SWAP", page)
        self.assertNotIn("load", page)
        self.assertIn("  C:", page)
        block = text(cardlines.ov_sistema(sm, 119, 0))
        self.assertNotIn("load", block)
        self.assertNotIn("?", block)

    def test_what_cannot_be_read_is_a_question_mark(self):
        sm = dict(self.sample(), mem=None, disk_root=None, uptime=None, load=None)
        block = text(cardlines.ov_sistema(sm, 119, 0)).split("\n")
        self.assertTrue(block[0].endswith("up ? · load ?"), block[0])
        self.assertEqual((block[1], block[2]), (" RAM   ?", " DISK  ?"))
        page = text(render.page_sistema(sm, 119, cont=None)).split("\n")
        self.assertEqual((page[0], page[2], page[3]), (" up ?   load ?", " RAM   ?", " DISK  ?"))

    def test_a_sample_without_the_figures_says_so_too(self):
        for sm in ({"cpu": {"cpu0": 0.5}, "thermal": {}},                                     # the fakes of the other tests: nothing to say
                   dict(self.sample(), mem={"MemTotal": 0, "MemAvailable": 0}, disk_root=(1, 0, "/")),   # nothing to divide by
                   dict(self.sample(), mem={"MemTotal": GIB}, disk_root="garbage"),            # no MemAvailable (kernel < 3.14)
                   dict(self.sample(), mem={}, disk_root=())):
            block = text(cardlines.ov_sistema(sm, 119, 0)).split("\n")
            self.assertEqual((block[1], block[2]), (" RAM   ?", " DISK  ?"), sm)
            page = text(render.page_sistema(sm, 119, cont=None)).split("\n")
            self.assertEqual((page[2], page[3]), (" RAM   ?", " DISK  ?"), sm)

    def test_unreadable_figures_leave_the_rest_of_the_block(self):
        sm = dict(self.sample(), mem=None, cpu={"cpu0": 0.5, "cpu1": 0.25})
        block = text(cardlines.ov_sistema(sm, 119, 0))
        self.assertIn("CPU   ", block)
        self.assertIn("2 threads", block)
        self.assertNotIn("error", block.lower())

    def test_up_load_note(self):
        self.assertEqual(cards.up_load_note(5 * 86400, ["0.1", "0.2", "0.3"]), "up 5d 0h · load 0.1 0.2 0.3")
        self.assertEqual(cards.up_load_note(3600 + 120, []), "up 1h 2m")
        self.assertEqual(cards.up_load_note(None, None), "up ? · load ?")
        self.assertEqual(cards.up_load_note(None, []), "up ?")


class OtherOperatingSystems(unittest.TestCase):
    """Windows and macOS: the same figures through hostinfo (fake here), whatever the OS the tests run on."""

    def setUp(self):
        self.saved = (render.LINUX, render.cached, render.read_thermal)
        render.LINUX = False
        render.cached = lambda key, ttl, fn: None
        render.read_thermal = lambda: {"throttle": None, "throttle_s": None, "clk": None}
        for p in (mock.patch.object(render.Sampler, "_cpu", staticmethod(lambda: {})),
                  mock.patch.object(render.Sampler, "_netdev", staticmethod(lambda: {})),
                  mock.patch.object(render, "hostinfo", hostinfo, create=True)):  # render imports it only where it is not Linux
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        render.LINUX, render.cached, render.read_thermal = self.saved

    def test_the_figures_come_from_hostinfo(self):
        mem = {"MemTotal": 32 * GIB, "MemAvailable": 20 * GIB, "Cached": 4 * GIB, "SwapTotal": 0, "SwapFree": 0}
        with mock.patch.multiple(hostinfo, meminfo=lambda: mem, loadavg=lambda: None, uptime=lambda: 4000.0,
                                 root_disk=lambda: (100 * GIB, 500 * GIB, "C:")):
            sm = render.Sampler().sample()
        self.assertEqual((sm["mem"], sm["uptime"], sm["load"], sm["disk_root"]), (mem, 4000.0, [], (100 * GIB, 500 * GIB, "C:")))
        block = text(cardlines.ov_sistema(sm, 119, 0))
        self.assertIn("up 1h 6m", block.split("\n")[0])
        self.assertNotIn("load", block)
        with mock.patch.multiple(hostinfo, meminfo=lambda: mem, loadavg=lambda: ["1.50", "1.25", "1.00"],
                                 uptime=lambda: 4000.0, root_disk=lambda: (1, 2, "/")):
            self.assertEqual(render.Sampler().sample()["load"], ["1.50", "1.25", "1.00"])

    def test_a_failing_api_is_unknown(self):
        def fail():
            raise OSError("GlobalMemoryStatusEx failed")
        with mock.patch.multiple(hostinfo, meminfo=fail, loadavg=fail, uptime=fail, root_disk=fail):
            sm = render.Sampler().sample()
        self.assertEqual((sm["mem"], sm["uptime"], sm["load"], sm["disk_root"]), (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
