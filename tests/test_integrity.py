"""The extended baseline (users, keys, cron, units) and the SMART health of the disks (src/integrity.py, collector.collect_smart).

A small file system is built in a temporary folder and passed as `root`; the commands are a fake `run`. Fixtures in tests/fixtures are shaped like
the real output of smartctl -j and nvme-cli; the values are made up."""
import contextlib
import io
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import time
import unittest

import cards  # noqa: E402
import collector  # noqa: E402
import hostdata  # noqa: E402
import integrity  # noqa: E402
import problems  # noqa: E402
import render  # noqa: E402,F401  (registers the cards)
import ui  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ACCEPT = problems.ACCEPT_CMD


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def put(root, path, text, mode="w"):
    full = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, mode) as f:
        f.write(text)
    return full


def build(root, extra_user=None, keys="ssh-ed25519 AAAA1 a@x\n# c\n\nssh-ed25519 AAAA2 b@x\n", cron="0 3 * * * root backup\n", unit="[Service]\nExecStart=/bin/true\n"):
    users = ["root:x:0:0:root:/root:/bin/bash", "daemon:x:1:1::/usr/sbin:/usr/sbin/nologin", "alice:x:1000:1000::/home/alice:/bin/bash",
             "bob:x:1001:1001::/home/bob:/bin/bash", "svc:x:998:998::/srv:/bin/false"]
    if extra_user:
        users.append(extra_user)
    put(root, "/etc/passwd", "\n".join(users) + "\n")
    put(root, "/etc/group", "sudo:x:27:alice\ndocker:x:998:svc\nusers:x:100:bob\n")
    put(root, "/home/alice/.ssh/authorized_keys", keys)
    put(root, "/etc/crontab", "SHELL=/bin/sh\n")
    put(root, "/etc/cron.d/backup", cron)
    put(root, "/var/spool/cron/crontabs/alice", "@daily true\n")
    put(root, "/etc/systemd/system/app.service", unit)
    put(root, "/etc/systemd/system/multi-user.target.wants/ssh.service", "")  # a name under *.wants
    return root


def snap(root):
    return {s: f(root) for s, f in integrity.COLLECT.items()}


def net_with(sections, **kw):
    return dict({"ts": time.time(), "errors": {}, "absent": [], "ufw": None, "listeners": []}, **sections, **kw)


class Sections(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = build(self.d.name)
        self.addCleanup(self.d.cleanup)

    def test_what_is_collected(self):
        s = snap(self.root)
        self.assertEqual(s["users"], ["alice", "bob", "root", "svc"])  # uid 0, uid >= 1000, a member of docker; not daemon
        self.assertEqual(s["keys"]["alice"]["n"], 2)  # the comment and the blank line do not count
        self.assertEqual(len(s["keys"]["alice"]["sha256"]), 64)
        self.assertEqual(set(s["cron"]), {"/etc/crontab", "/etc/cron.d/backup", "spool:alice"})
        self.assertEqual(set(s["units"]), {"app.service", "ssh.service"})
        self.assertEqual(s["units"]["ssh.service"], "")  # enabled by a link: no hash
        self.assertEqual(len(s["units"]["app.service"]), 64)

    def test_nothing_of_a_file_is_kept(self):
        blob = json.dumps(snap(self.root))
        for secret in ("AAAA1", "a@x", "0 3 *", "ExecStart", "SHELL", "@daily"):
            self.assertNotIn(secret, blob)

    def test_users_added_and_removed(self):
        base = snap(self.root)
        build(self.d.name, extra_user="mallory:x:0:0::/root:/bin/sh")
        build(self.d.name, extra_user="carol:x:1002:1002::/home/carol:/bin/bash")
        cur = snap(self.root)
        pr = integrity.baseline_problems(net_with(cur), base, ACCEPT)
        self.assertEqual([(s, i) for s, _, i in pr], [(2, "users-changed")])
        self.assertIn("added carol", pr[0][1])
        pr = integrity.baseline_problems(net_with(base), cur, ACCEPT)
        self.assertIn("removed carol", pr[0][1])

    def test_keys_changed_added_removed(self):
        base = snap(self.root)
        put(self.d.name, "/home/alice/.ssh/authorized_keys", "ssh-ed25519 AAAA1 a@x\n")
        pr = integrity.baseline_problems(net_with(snap(self.root)), base, ACCEPT)
        self.assertEqual([(s, i) for s, _, i in pr], [(2, "keys-changed")])
        self.assertIn("alice (2->1 keys)", pr[0][1])
        put(self.d.name, "/home/alice/.ssh/authorized_keys", "ssh-ed25519 AAAA9 z@x\nssh-ed25519 AAAA8 y@x\n")  # same count, other content
        pr = integrity.baseline_problems(net_with(snap(self.root)), base, ACCEPT)
        self.assertIn("changed alice", pr[0][1])
        self.assertNotIn("AAAA", pr[0][1])
        os.unlink(os.path.join(self.d.name, "home/alice/.ssh/authorized_keys"))
        pr = integrity.baseline_problems(net_with(snap(self.root)), base, ACCEPT)
        self.assertIn("removed alice", pr[0][1])

    def test_cron_changed_added_removed(self):
        base = snap(self.root)
        put(self.d.name, "/etc/cron.d/backup", "* * * * * root evil\n")
        put(self.d.name, "/etc/cron.d/new", "x\n")
        os.unlink(os.path.join(self.d.name, "var/spool/cron/crontabs/alice"))
        pr = integrity.baseline_problems(net_with(snap(self.root)), base, ACCEPT)
        self.assertEqual([(s, i) for s, _, i in pr], [(1, "cron-changed")])
        for part in ("added /etc/cron.d/new", "removed spool:alice", "changed /etc/cron.d/backup"):
            self.assertIn(part, pr[0][1])
        self.assertNotIn("evil", pr[0][1])

    def test_units_changed_added_removed(self):
        base = snap(self.root)
        put(self.d.name, "/etc/systemd/system/app.service", "[Service]\nExecStart=/bin/false\n")
        put(self.d.name, "/etc/systemd/system/timers.target.wants/new.timer", "")
        os.unlink(os.path.join(self.d.name, "etc/systemd/system/multi-user.target.wants/ssh.service"))
        pr = integrity.baseline_problems(net_with(snap(self.root)), base, ACCEPT)
        self.assertEqual([(s, i) for s, _, i in pr], [(1, "units-changed")])
        for part in ("added new.timer", "removed ssh.service", "changed app.service"):
            self.assertIn(part, pr[0][1])

    def test_no_change_no_problem(self):
        s = snap(self.root)
        self.assertEqual(integrity.baseline_problems(net_with(s), s, ACCEPT), [])

    def test_a_key_file_that_is_a_link_is_not_followed(self):
        os.unlink(os.path.join(self.d.name, "home/alice/.ssh/authorized_keys"))
        put(self.d.name, "/elsewhere", "secret\n")
        os.symlink(os.path.join(self.d.name, "elsewhere"), os.path.join(self.d.name, "home/alice/.ssh/authorized_keys"))
        self.assertNotIn("alice", integrity.keys(self.d.name))

    def test_unreadable_passwd_is_an_error_of_the_section(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(OSError):
                integrity.users(empty)
            self.assertEqual(integrity.cron(empty), {})  # nothing there: an empty section, not a failure


class Compatibility(unittest.TestCase):
    """An installation that updates has a baseline with the ports only: nothing alarms, one quiet note says what to do."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.cur = snap(build(self.d.name))

    def test_old_baseline_gives_one_note_and_no_alarm(self):
        pr = integrity.baseline_problems(net_with(self.cur), {"ts": 1, "ports": {}}, ACCEPT)
        self.assertEqual(len(pr), 1)
        sev, text, pid = pr[0]
        self.assertEqual((sev, pid), (1, "baseline-incomplete"))
        self.assertIn(ACCEPT, text)
        for s in integrity.SECTIONS:
            self.assertIn(s, text)
        self.assertNotIn("baseline-incomplete", problems.NOT_ACCEPTABLE)  # acceptable and quiet
        self.assertNotIn("baseline-incomplete", __import__("nuc_config").MUTABLE_ALERTS)

    def test_one_missing_section_is_not_tracked_and_the_others_are(self):
        base = dict({"ports": {}}, **{k: v for k, v in self.cur.items() if k != "cron"})
        net = net_with(dict(self.cur, cron={"/etc/crontab": "0" * 64}, users=["alice", "root", "svc", "eve"]))
        pr = integrity.baseline_problems(net, base, ACCEPT)
        self.assertEqual(sorted(i for _, _, i in pr), ["baseline-incomplete", "users-changed"])
        self.assertIn("cron", [t for _, t, i in pr if i == "baseline-incomplete"][0])

    def test_wrong_shape_counts_as_missing_not_as_a_crash(self):
        base = {"ports": {}, "users": 5, "keys": [1], "cron": {"a": 1}, "units": None, "future": {"x": 1}}  # unknown keys are ignored
        pr = integrity.baseline_problems(net_with(self.cur), base, ACCEPT)
        self.assertEqual([i for _, _, i in pr], ["baseline-incomplete"])

    def test_sections_not_collected_are_not_compared(self):
        self.assertEqual(integrity.baseline_problems(net_with({}), {"ports": {}}, ACCEPT), [])  # macOS/Windows, or a section that failed
        self.assertEqual(integrity.baseline_problems(None, {"ports": {}}, ACCEPT), [])

    def test_problems_raw_wires_it(self):
        base = {"ts": 1, "ports": {}}
        pr = problems.problems_raw(net_with(self.cur), None, baseline=base)
        self.assertIn("baseline-incomplete", [i for _, _, i in pr])
        self.assertFalse(any(i.endswith("-changed") for _, _, i in pr))
        pr = problems.problems_raw(net_with(self.cur), None, baseline=None)  # no baseline at all: the existing note, no new ones
        self.assertNotIn("baseline-incomplete", [i for _, _, i in pr])
        pr = problems.problems_raw(net_with(self.cur), None, baseline="corrotta")
        self.assertIn("baseline-unreadable", [i for _, _, i in pr])

    def test_integrity_changes_cannot_be_accepted_one_by_one(self):
        for pid in ("users-changed", "keys-changed", "cron-changed", "units-changed"):
            self.assertIn(pid, problems.NOT_ACCEPTABLE)
            self.assertIn(pid, problems.CATALOG)


class AcceptFlow(unittest.TestCase):
    def run_accept(self, net, **kw):
        d = tempfile.mkdtemp()
        n, c, bl = os.path.join(d, "net.json"), os.path.join(d, "c.json"), os.path.join(d, "sub", "baseline.json")
        json.dump(dict(net, ts=time.time()), open(n, "w"))
        json.dump({"ts": time.time(), "containers": []}, open(c, "w"))
        old = (hostdata.NET_STATE, hostdata.STATE)
        hostdata.NET_STATE, hostdata.STATE = n, c
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = problems.accept_baseline(path=bl, **kw)
        finally:
            hostdata.NET_STATE, hostdata.STATE = old
        return rc, bl, out.getvalue()

    def test_linux_baseline_has_the_sections_and_a_version(self):
        with tempfile.TemporaryDirectory() as d:
            cur = snap(build(d))
        rc, bl, out = self.run_accept(net_with(cur))
        self.assertEqual(rc, 0)
        data = json.load(open(bl))
        self.assertEqual(data["v"], integrity.BASELINE_VERSION)
        for s in integrity.SECTIONS:
            self.assertEqual(data[s], cur[s])
        self.assertIn("users, keys, cron, units", out)
        self.assertEqual(oct(os.stat(bl).st_mode & 0o777), "0o644")
        self.assertFalse(os.path.exists(bl + ".tmp"))
        self.assertEqual(integrity.baseline_problems(net_with(cur), problems.load_baseline(bl), ACCEPT), [])  # accepted: quiet

    def test_other_os_baseline_is_as_before(self):
        rc, bl, out = self.run_accept(net_with({}, os="windows"))
        self.assertEqual(rc, 0)
        self.assertEqual(set(json.load(open(bl))), {"ts", "ports"})

    def test_a_failed_section_is_left_out_not_blessed(self):
        with tempfile.TemporaryDirectory() as d:
            cur = snap(build(d))
        cur.pop("cron")
        rc, bl, _ = self.run_accept(net_with(cur, errors={"cron": "x"}))
        self.assertEqual(rc, 0)
        self.assertNotIn("cron", json.load(open(bl)))

    def test_corrupt_baseline_file(self):
        d = tempfile.mkdtemp()
        bl = os.path.join(d, "b.json")
        open(bl, "w").write("{not json")
        self.assertEqual(problems.load_baseline(bl), "corrotta")
        open(bl, "w").write(json.dumps({"ports": {}, "users": "x", "v": 99}))  # a newer or odd file still loads: the odd section is "missing"
        self.assertIsInstance(problems.load_baseline(bl), dict)


SMARTCTL_RUN = {"smartctl": ("nvme", fixture("smartctl_nvme.json")), }


class Smart(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        for n, rem in (("nvme0n1", "0"), ("sda", "0"), ("sdb", "1"), ("loop0", "0"), ("zram0", "0"), ("dm-0", "0"), ("nvme0n1p1", "0"), ("sda1", "0")):
            put(self.d.name, f"/sys/block/{n}/removable", rem + "\n")
        put(self.d.name, "/sys/block/nvme0n1/device/model", "Example   NVMe SSD 1TB \n")
        collector._SMART.update(t=None, val=None, media={})
        self.calls = []

    def fake(self, outputs, absent=()):
        def run(name, *args, timeout=15):
            self.calls.append((name,) + args)
            self.assertEqual(timeout, collector.SMART_TIMEOUT_S)
            if name in absent:
                raise collector.Absent(name)
            return outputs[name](args[-1].rsplit("/", 1)[1]) + ("",)
        return run

    def collect(self, outputs, absent=(), now=0.0):
        old = collector.run
        collector.run = self.fake(outputs, absent)
        try:
            return collector.collect_smart(now, self.d.name)
        finally:
            collector.run = old

    def test_only_physical_non_removable_devices(self):
        self.assertEqual(integrity.block_devices(self.d.name), ["nvme0n1", "sda"])  # no loop, zram, dm-, partitions, USB stick
        self.assertEqual([bool(integrity.DEVICE.match(x)) for x in ("nvme0n1", "sdab", "sda1", "../sda", "sda;x", "nvme0", "loop0")],
                         [True, True, False, False, False, False, False])

    def test_parsers_on_real_shapes(self):
        a = integrity.parse_smartctl("nvme0n1", fixture("smartctl_nvme.json"))
        self.assertEqual((a["health"], a["wear"], a["spare"], a["media"], a["crit"], a["poh"], a["model"]),
                         ("PASSED", 3, 100, 0, 0, 4321, "Example NVMe SSD 1TB"))
        b = integrity.parse_smartctl("sda", fixture("smartctl_sata.json"))
        self.assertEqual((b["health"], b["wear"], b["realloc"], b["poh"]), ("PASSED", 12, 8, 21000))
        c = integrity.parse_nvme("nvme0n1", fixture("nvme_smartlog.json"), "M")
        self.assertEqual((c["health"], c["wear"], c["crit"], c["media"], c["poh"]), ("FAILED", 96, 4, 5, 30000))
        self.assertIsNone(integrity.parse_smartctl("nvme0n1", fixture("smartctl_nopermission.json")))  # no sudo
        for bad in ("", "[]", "null", "{", '{"smart_status": 3}'):
            self.assertIsNone(integrity.parse_smartctl("sda", bad))
            self.assertIsNone(integrity.parse_nvme("sda", bad))

    def test_smartctl_with_a_fixed_argv_and_a_mask_exit_status(self):
        outs = {"smartctl": lambda dev: (4, fixture("smartctl_nvme.json" if dev.startswith("nvme") else "smartctl_sata.json"))}  # rc is a bit mask
        rows = self.collect(outs)
        self.assertEqual([r["dev"] for r in rows], ["nvme0n1", "sda"])
        self.assertEqual(self.calls[0], ("smartctl", "-j", "-H", "-A", "/dev/nvme0n1"))
        self.assertEqual(rows[0]["model"], "Example NVMe SSD 1TB")
        self.assertEqual(rows[1]["realloc"], 8)

    def test_cached_for_half_an_hour(self):
        outs = {"smartctl": lambda dev: (0, fixture("smartctl_nvme.json"))}
        self.collect(outs, now=100.0)
        n = len(self.calls)
        self.collect(outs, now=100.0 + collector.SMART_TTL_S - 1)
        self.assertEqual(len(self.calls), n)
        self.collect(outs, now=100.0 + collector.SMART_TTL_S + 1)
        self.assertGreater(len(self.calls), n)

    def test_nvme_cli_when_there_is_no_smartctl(self):
        outs = {"nvme": lambda dev: (0, fixture("nvme_smartlog.json"))}
        rows = self.collect(outs, absent=("smartctl",))
        self.assertEqual([c for c in self.calls if c[0] == "nvme"][0], ("nvme", "smart-log", "-o", "json", "/dev/nvme0n1"))
        self.assertEqual(rows[0]["health"], "FAILED")
        self.assertEqual(rows[1]["health"], None)  # the SATA disk: nothing can read it

    def test_no_tool_is_absent_not_an_error(self):
        with self.assertRaises(collector.Absent):
            self.collect({}, absent=("smartctl", "nvme"))

    def test_no_sudo_or_a_usb_bridge_is_a_question_mark(self):
        outs = {"smartctl": lambda dev: (2, fixture("smartctl_nopermission.json")), "nvme": lambda dev: (1, "")}
        rows = self.collect(outs)
        self.assertEqual([r["health"] for r in rows], [None, None])
        self.assertEqual(integrity.smart_problems(rows), [])  # unknown is shown, not alarmed

    def test_errors_grown_since_the_previous_sample(self):
        sample = json.loads(fixture("smartctl_nvme.json"))
        def outs(media):
            sample["nvme_smart_health_information_log"]["media_errors"] = media
            return {"smartctl": lambda dev: (0, json.dumps(sample)) if dev.startswith("nvme") else (0, fixture("smartctl_sata.json"))}
        self.assertFalse(self.collect(outs(2), now=0.0)[0]["grown"])  # the first look: no previous sample
        self.assertFalse(self.collect(outs(2), now=2000.0)[0]["grown"])
        self.assertTrue(self.collect(outs(3), now=4000.0)[0]["grown"])

    def row(self, **kw):
        return dict({"dev": "nvme0n1", "health": "PASSED", "wear": 3, "media": 0, "crit": 0, "realloc": None, "grown": False}, **kw)

    def test_problems(self):
        ids = lambda *rows: [(s, i) for s, _, i in integrity.smart_problems(list(rows))]  # noqa: E731
        self.assertEqual(ids(self.row()), [])
        self.assertEqual(ids(self.row(health="FAILED")), [(2, "disk-smart-failed")])
        self.assertEqual(ids(self.row(crit=4)), [(2, "disk-smart-failed")])
        self.assertEqual(ids(self.row(wear=79)), [])
        self.assertEqual(ids(self.row(wear=80)), [(1, "disk-wear")])
        self.assertEqual(ids(self.row(wear=94)), [(1, "disk-wear")])
        self.assertEqual(ids(self.row(wear=95)), [(2, "disk-wear")])
        self.assertEqual(ids(self.row(media=3)), [(1, "disk-media-errors")])
        self.assertEqual(ids(self.row(media=3, grown=True)), [(2, "disk-media-errors")])
        self.assertEqual(ids(self.row(dev="sda", media=None, realloc=8)), [(1, "disk-media-errors")])
        self.assertEqual(ids(self.row(dev="sda", media=None, realloc=8, grown=True)), [(2, "disk-media-errors")])
        self.assertEqual(integrity.smart_problems(None), [])
        self.assertEqual(integrity.smart_problems("x"), [])
        self.assertEqual(integrity.smart_problems([None, 3, {"dev": 5}]), [])

    def test_not_mutable(self):
        mutable = __import__("nuc_config").MUTABLE_ALERTS
        for pid in ("disk-smart-failed", "disk-wear", "disk-media-errors", "users-changed", "keys-changed", "cron-changed", "units-changed"):
            self.assertNotIn(pid, mutable)
            self.assertIn(pid, cards.PROBLEM_CARDS)

    def test_problems_raw_has_them_and_other_os_has_none(self):
        net = net_with({"smart": [self.row(wear=97)]})
        self.assertIn("disk-wear", [i for _, _, i in problems.problems_raw(net, None)])
        self.assertEqual([i for _, _, i in problems.problems_raw(net_with({}, os="windows"), None) if i.startswith("disk-")], [])

    def test_collect_net_marks_the_sections_unsupported_off_linux(self):
        for key in integrity.SECTIONS + ("smart",):
            self.assertIn(key, collector.LINUX_ONLY_NET)
            self.assertIn(key, collector.NET_FEATURE)


class DisksCard(unittest.TestCase):
    def ctx(self, smart):
        return cards.Ctx(s={"fs": [{"mount": "/", "used": 2 ** 30, "total": 2 ** 31}]}, net=net_with({} if smart is None else {"smart": smart}),
                         problems=problems.ProblemList(), cfg=None, now=time.time())

    def test_one_line_with_a_fragment_per_device_that_fits_the_width(self):
        rows = [{"dev": "nvme0n1", "health": "PASSED", "wear": 7}, {"dev": "sda", "health": "FAILED", "wear": 100},
                {"dev": "nvme10n1", "health": None, "wear": None}, {"dev": "sdb", "health": "PASSED", "wear": 85}]
        lines = cards.smart_lines(self.ctx(rows).net, cards.Caps(80))
        self.assertEqual([ln.text for ln in lines], [" SMART  nvme0n1 ok 7%  sda FAILED 100%  nvme10n1 ?  sdb ok 85%"])
        narrow = cards.smart_lines(self.ctx(rows).net, cards.Caps(40))[0].text
        self.assertLessEqual(len(narrow), 40)
        self.assertTrue(narrow.endswith("+3"), narrow)  # what did not fit is counted, not dropped silently
        card = cards.build("disks", self.ctx(rows), 0, cards.Caps(60))
        self.assertEqual(len([b for b in card.body if isinstance(b, ui.Line)]), 1)

    def test_nothing_without_smart(self):
        for smart in (None, [], "x"):
            self.assertEqual(cards.smart_lines(self.ctx(smart).net, cards.Caps(60)), [])
        self.assertEqual(cards.smart_lines(None, cards.Caps(60)), [])


if __name__ == "__main__":
    unittest.main()
