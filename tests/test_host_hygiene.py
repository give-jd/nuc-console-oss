"""Host hygiene alarms: [backups], reboot, updates, clock, inodes (collector sections of boot.json, judged by problems.hygiene_problems).

Hermetic: the files are built in a temporary folder, the commands are faked (collector.run), the clock is passed in."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import collector  # noqa: E402
import golden  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
ROOT = os.path.abspath(os.sep)  # "/" on POSIX, "C:\\" on Windows (where "/srv" is not absolute since Python 3.13)


def at(*parts):
    return os.path.join(ROOT, *parts)


def load(text):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        with open(path, "w") as f:
            f.write(text)
        said = []
        return nuc_config.load(path, warn=said.append, overlay=False)["backups"], said


class Config(unittest.TestCase):
    def test_a_line_and_its_age(self):
        nightly, photos, comma = at("srv", "b", "db-*.gz"), at("srv", "photos"), at("srv", "a,b", "dump")
        got, said = load(f"[backups]\nnightly = {nightly}, 3\nphotos = {photos}\nweird = {comma}\nwith-age = {comma}, 5\n")
        self.assertEqual(said, [])
        self.assertEqual(got["nightly"], {"path": nightly, "max_age_days": 3})
        self.assertEqual(got["photos"], {"path": photos, "max_age_days": 2}, "the default age")
        self.assertEqual(got["weird"]["path"], comma, "a comma that is not followed by a number belongs to the path")
        self.assertEqual(got["with-age"], {"path": comma, "max_age_days": 5})

    def test_what_is_refused_is_said_and_skipped(self):
        up, old, ok = at("srv", "..", "etc", "x"), at("srv", "x"), at("srv", "ok")
        got, said = load(f"[backups]\nrel = backups/db\ntilde = ~/db\nup = {up}\nold = {old}, 0\nok = {ok}\n")
        self.assertEqual(list(got), ["ok"])
        self.assertEqual(len(said), 4)
        self.assertTrue(all("[backups]" in s for s in said))

    def test_no_section_no_backups(self):
        self.assertEqual(load("[ui]\ntheme = dark\n")[0], {})
        self.assertEqual(nuc_config.load("/nonexistent")["backups"], {})

    def test_the_shipped_file_has_it_commented(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "config.ini")) as f:
            text = f.read()
        self.assertIn("# [backups]", text)
        self.assertEqual(nuc_config.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "config.ini"),
                                         warn=lambda s: None, overlay=False)["backups"], {})


    def test_the_migration_keeps_the_section(self):
        import confmigrate
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(root, "config", "config.ini")) as f:
            dist = f.read()
        path = at("srv", "b", "db-*.gz")
        new = confmigrate.build(f"[features]\nboot = no\n[backups]\ndb = {path}, 3\n", dist)
        new = new[0] if isinstance(new, tuple) else new
        self.assertEqual(load(new)[0], {"db": {"path": path, "max_age_days": 3}})


class Newest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.realpath(self.tmp.name)

    def touch(self, rel, mtime, size=0):
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        os.utime(path, (mtime, mtime))
        return path

    def test_no_match(self):
        self.assertEqual(collector.newest_match(os.path.join(self.d, "db-*.gz")), (None, None, 0))
        self.assertEqual(collector.newest_match(os.path.join(self.d, "absent", "db-*.gz")), (None, None, 0))

    def test_the_newest_of_a_glob_with_its_size(self):
        self.touch("db-1.gz", NOW - 5 * DAY, 10)
        self.touch("db-2.gz", NOW - DAY, 20)
        self.touch("other.txt", NOW, 99)
        self.assertEqual(collector.newest_match(os.path.join(self.d, "db-*.gz")), (NOW - DAY, 20, 2))

    def test_a_path_without_a_glob_and_a_folder(self):
        self.touch("one.tar", NOW - 3 * DAY, 7)
        self.assertEqual(collector.newest_match(os.path.join(self.d, "one.tar")), (NOW - 3 * DAY, 7, 1))
        os.utime(self.d, (NOW - 2 * DAY, NOW - 2 * DAY))
        self.assertEqual(collector.newest_match(self.d)[0], NOW - 2 * DAY, "a folder counts as itself")

    def test_a_link_out_of_the_base_is_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = os.path.join(outside.name, "f.gz")
        with open(secret, "w") as f:
            f.write("x")
        os.utime(secret, (NOW, NOW))
        self.touch("in/db-1.gz", NOW - 4 * DAY)
        os.symlink(outside.name, os.path.join(self.d, "in", "evil"))        # a folder link that leads out
        os.symlink(outside.name, os.path.join(self.d, "evil"))
        got = collector.newest_match(os.path.join(self.d, "*", "*.gz"))     # the wildcard folder 'evil' is a link to the outside
        self.assertEqual(got, (NOW - 4 * DAY, 0, 1))

    def test_a_link_among_the_matches_counts_as_itself(self):
        target = self.touch("target.dat", NOW - 10 * DAY)
        link = os.path.join(self.d, "latest.gz")
        os.symlink(target, link)
        if os.utime in os.supports_follow_symlinks:
            os.utime(link, (NOW - DAY, NOW - DAY), follow_symlinks=False)
        else:  # Windows: a link's own date cannot be set, only read
            self.assertNotEqual(int(os.lstat(link).st_mtime), NOW - 10 * DAY)
        self.assertEqual(collector.newest_match(link)[0], int(os.lstat(link).st_mtime), "the link's own date, not its target's")
        self.assertNotEqual(collector.newest_match(link)[0], NOW - 10 * DAY)

    def test_collect_backups(self):
        self.touch("db-1.gz", NOW - DAY, 5)
        cfg = {"backups": {"a": {"path": os.path.join(self.d, "db-*.gz"), "max_age_days": 2},
                           "b": {"path": os.path.join(self.d, "nope-*"), "max_age_days": 7}}}
        self.assertEqual(collector.collect_backups(cfg), [
            {"name": "a", "newest": NOW - DAY, "size": 5, "matches": 1, "max_age_days": 2},
            {"name": "b", "newest": None, "size": None, "matches": 0, "max_age_days": 7}])
        with self.assertRaises(collector.Absent):
            collector.collect_backups({"backups": {}})


class Reboot(unittest.TestCase):
    def test_version_order_is_numeric(self):
        k = collector.kernel_key
        self.assertEqual(k("7.0.0-34-generic"), ((7, 0, 0, 34), "generic"))
        self.assertEqual(k("vmlinuz-7.0.0-38-generic"), ((7, 0, 0, 38), "generic"))
        self.assertGreater(k("6.8.0-100-generic")[0], k("6.8.0-9-generic")[0], "100 is after 9, not before")

    def run_reboot(self, running, boot, flag=False):
        with mock.patch("os.uname", create=True, return_value=mock.Mock(release=running)), mock.patch("os.listdir", return_value=boot), \
                mock.patch("os.path.exists", return_value=flag):
            return collector.collect_reboot()

    def test_an_older_running_kernel(self):
        got = self.run_reboot("7.0.0-34-generic", ["vmlinuz-7.0.0-34-generic", "vmlinuz-7.0.0-38-generic", "config-7.0.0-38-generic", "vmlinuz.old"])
        self.assertEqual((got["newest"], got["kernel_old"], got["flag"]), ("7.0.0-38-generic", True, False))
        self.assertEqual([p[0] for p in problems.hygiene_problems({"reboot": got}, NOW)], [1])

    def test_the_running_kernel_is_the_newest(self):
        got = self.run_reboot("7.0.0-38-generic", ["vmlinuz-7.0.0-34-generic", "vmlinuz-7.0.0-38-generic"])
        self.assertFalse(got["kernel_old"])
        self.assertEqual(problems.hygiene_problems({"reboot": got}, NOW), [])

    def test_a_kernel_newer_than_the_installed_ones_and_another_flavour_are_no_reason(self):
        self.assertFalse(self.run_reboot("7.0.0-40-generic", ["vmlinuz-7.0.0-38-generic"])["kernel_old"])
        self.assertFalse(self.run_reboot("7.0.0-34-generic", ["vmlinuz-7.0.0-34-generic", "vmlinuz-7.0.0-99-lowlatency"])["kernel_old"])

    def test_boot_unreadable_says_nothing_but_the_flag_counts(self):
        with mock.patch("os.uname", create=True, return_value=mock.Mock(release="7.0.0-34-generic")), mock.patch("os.listdir", side_effect=OSError), \
                mock.patch("os.path.exists", return_value=True):
            got = collector.collect_reboot()
        self.assertEqual((got["newest"], got["kernel_old"], got["flag"]), (None, False, True))
        self.assertEqual(problems.hygiene_problems({"reboot": got}, NOW)[0][2], "reboot-required")


APT = """NOTE: This is only a simulation!
Inst libc6 [2.39-0ubuntu8.3] (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst vim [2:9.1.0016-1ubuntu7.4] (2:9.1.0016-1ubuntu7.5 Ubuntu:24.04/noble-updates [amd64])
Inst newpkg (1.0 Ubuntu:24.04/noble-updates [amd64])
Conf libc6 (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Remv oldpkg [1.0]
"""
DEBIAN = "Inst openssl [3.0.11-1] (3.0.14-1~deb12u1 Debian-Security:12/stable-security [amd64])\n"


class Updates(unittest.TestCase):
    def setUp(self):
        collector._UPDATES.update(at=0.0, value=None, error=None)
        self.addCleanup(collector._UPDATES.update, at=0.0, value=None, error=None)

    def test_parse(self):
        self.assertEqual(collector.parse_apt_sim(APT), {"total": 3, "security": 1})
        self.assertEqual(collector.parse_apt_sim(DEBIAN), {"total": 1, "security": 1})
        self.assertEqual(collector.parse_apt_sim("Inst vim [1] (2 Ubuntu:24.04/noble-updates [amd64])\n"), {"total": 1, "security": 0})
        self.assertEqual(collector.parse_apt_sim(""), {"total": 0, "security": 0})

    def test_no_package_name_is_kept(self):
        self.assertNotIn("libc6", repr(collector.parse_apt_sim(APT)))

    def test_run_once_an_hour_with_a_fixed_command(self):
        fake = mock.Mock(return_value=(0, APT, ""))
        with mock.patch.object(collector, "run", fake), mock.patch.object(collector, "OS_NAME", "linux"):
            first = collector.collect_updates(NOW)
            collector.collect_updates(NOW + 3599)
            self.assertEqual(fake.call_count, 1)
            collector.collect_updates(NOW + 3600)
            self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args[0], ("env", "LC_ALL=C", "apt-get", "-s", "dist-upgrade"))
        self.assertEqual((first["security"], first["total"], first["at"]), (1, 3, NOW))

    def test_no_apt_is_absent_and_a_failure_is_kept_for_the_hour(self):
        with mock.patch.object(collector, "run", return_value=(127, "", "env: 'apt-get': No such file or directory")), \
                mock.patch.object(collector, "OS_NAME", "linux"):
            with self.assertRaises(collector.Absent):
                collector.collect_updates(NOW)
        collector._UPDATES.update(at=0.0, value=None, error=None)
        fake = mock.Mock(return_value=(100, "", "E: Could not get lock"))
        with mock.patch.object(collector, "run", fake), mock.patch.object(collector, "OS_NAME", "linux"):
            for t in (NOW, NOW + 600):
                with self.assertRaises(RuntimeError):
                    collector.collect_updates(t)
        self.assertEqual(fake.call_count, 1)

    def test_the_problem(self):
        pb = problems.hygiene_problems({"updates": {"security": 3, "total": 14}}, NOW)
        self.assertEqual([(s, i) for s, _, i in pb], [(1, "updates-pending")])
        self.assertIn("3 security updates pending", pb[0][1])
        self.assertEqual(problems.hygiene_problems({"updates": {"security": 0, "total": 14}}, NOW), [])
        self.assertEqual(problems.hygiene_problems({"updates": "junk"}, NOW), [])


class Clock(unittest.TestCase):
    def ask(self, result):
        fake = mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)
        with mock.patch.object(collector, "run", fake):
            return collector.collect_clock(), fake

    def test_synced_and_not(self):
        got, fake = self.ask((0, "NTP=yes\nNTPSynchronized=yes\n", ""))
        self.assertEqual(got, {"synced": True, "ntp": True})
        self.assertEqual(fake.call_args[0], ("timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"))
        got, _ = self.ask((0, "NTPSynchronized=no\nNTP=no\n", ""))
        pb = problems.hygiene_problems({"clock": got}, NOW)
        self.assertEqual([(s, i) for s, _, i in pb], [(1, "clock-unsynced")])
        self.assertIn("off", pb[0][1])
        self.assertEqual(problems.hygiene_problems({"clock": {"synced": True, "ntp": False}}, NOW), [])

    def test_unknown_is_no_problem(self):
        for result in ((1, "", "Failed to connect to bus"), (0, "garbage\n", ""), (0, "NTPSynchronized=maybe\n", ""), (None, "", "timeout")):
            with self.assertRaises(collector.Absent, msg=result):
                self.ask(result)
        with self.assertRaises(collector.Absent):
            self.ask(collector.Absent("timedatectl"))
        self.assertEqual(problems.hygiene_problems({"clock": {"synced": None}}, NOW), [])


class Inodes(unittest.TestCase):
    def read(self, table):
        def statvfs(mount):
            files, free = table[mount]
            return mock.Mock(f_files=files, f_ffree=free)
        with mock.patch.object(collector, "disk_rows", return_value=[{"mount": m} for m in table]), mock.patch("os.statvfs", create=True, side_effect=statvfs):
            return collector.collect_inodes()

    def test_percent_and_the_filesystems_without_a_count(self):
        got = self.read({"/": (1000, 900), "/data": (1000, 20), "/btrfs": (0, 0)})
        self.assertEqual(got, [{"mount": "/", "pct": 10.0}, {"mount": "/data", "pct": 98.0}])

    def test_thresholds(self):
        def sev(p):
            return [s for s, _, _ in problems.hygiene_problems({"inodes": [{"mount": "/data", "pct": p}]}, NOW)]
        self.assertEqual([sev(89.9), sev(90.0), sev(96.9), sev(97.0), sev(100.0)], [[], [1], [1], [2], [2]])

    def test_the_worst_first_and_junk(self):
        pb = problems.hygiene_problems({"inodes": [{"mount": "/a", "pct": 91.0}, {"mount": "/b", "pct": 99.0}, {"mount": "/c", "pct": 10.0}, {"pct": 99}, "x"]}, NOW)
        self.assertEqual(len(pb), 1)
        self.assertEqual((pb[0][0], pb[0][2]), (2, "disk-inodes"))
        self.assertLess(pb[0][1].index("/b"), pb[0][1].index("/a"))
        self.assertNotIn("/c", pb[0][1])


class Backups(unittest.TestCase):
    def one(self, **kw):
        row = dict({"name": "nightly", "newest": NOW - DAY, "size": 1, "matches": 1, "max_age_days": 2}, **kw)
        return problems.hygiene_problems({"backups": [row]}, NOW)

    def test_fresh_stale_missing(self):
        self.assertEqual(self.one(), [])
        self.assertEqual(self.one(newest=NOW - 2 * DAY), [], "exactly at the limit is still fine")
        pb = self.one(newest=NOW - 3 * DAY)
        self.assertEqual([(s, i) for s, _, i in pb], [(1, "backup-stale")])
        self.assertIn("nightly (3 d)", pb[0][1])
        self.assertEqual([(s, i) for s, _, i in self.one(newest=NOW - 3 * DAY, max_age_days=7)], [])
        pb = self.one(newest=None, size=None, matches=0)
        self.assertEqual([(s, i) for s, _, i in pb], [(2, "backup-missing")])

    def test_both_at_once_and_no_section(self):
        pb = problems.hygiene_problems({"backups": [{"name": "a", "newest": None, "matches": 0, "max_age_days": 2},
                                                    {"name": "b", "newest": NOW - 9 * DAY, "matches": 1, "max_age_days": 2}]}, NOW)
        self.assertEqual(sorted(i for _, _, i in pb), ["backup-missing", "backup-stale"])
        for boot in ({}, {"backups": None}, {"backups": []}, {"backups": "x"}, {"backups": [{"newest": 1}]}):
            self.assertEqual(problems.hygiene_problems(boot, NOW), [], boot)


class Wiring(unittest.TestCase):
    def setUp(self):
        world = golden.FrozenWorld(cfg={"alerts": {"mute": ["reboot-required", "updates-pending"], "ignored": []}})
        world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)

    def test_through_problems_and_the_mute_list(self):
        boot = {"ts": NOW, "errors": {}, "reboot": {"flag": True}, "updates": {"security": 1, "total": 1}, "clock": {"synced": False, "ntp": True}}
        pb = problems.problems(None, None, now=NOW, boot=boot)
        self.assertIn("clock-unsynced", pb.pids)
        self.assertNotIn("reboot-required", pb.pids, "operational: it can be muted")
        self.assertIn("updates-pending", pb.pids, "security: a mute is ignored")
        self.assertEqual(pb.muted, 1)
        for pid in ("backup-stale", "backup-missing", "reboot-required", "updates-pending", "clock-unsynced", "disk-inodes"):
            self.assertIn(pid, problems.CATALOG)
        self.assertEqual(sorted(set(nuc_config.MUTABLE_ALERTS) & {"backup-stale", "backup-missing", "reboot-required", "updates-pending",
                                                                   "clock-unsynced", "disk-inodes"}), ["disk-inodes", "reboot-required"])


if __name__ == "__main__":
    unittest.main()
