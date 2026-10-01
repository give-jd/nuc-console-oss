"""History: the SQLite store, the aggregation, the template function, the event readers of every OS (fixtures) and the thread.

Everything here runs on any OS (fixtures, fake `run`, temporary files); the real system calls are exercised in
tests/test_platforms.py (OnWindows, OnMacOS) and in OnLinux below. Addresses are documentation ranges; secrets are built at
runtime so that no real-looking secret is committed.
"""
import contextlib
import gc
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import collect_darwin as cmac  # noqa: E402
import collect_windows as cwin  # noqa: E402
import collector  # noqa: E402
import history  # noqa: E402
import nuc_config  # noqa: E402

DAY = 86400
T0 = 1_790_000_000 - 1_790_000_000 % 3600  # an exact hour, September 2026


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nuc-history-")
        self.path = os.path.join(self.dir, "history.db")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def store(self, now=None):
        s = history.Store(self.path, now=lambda: now if now is not None else T0 + 100)
        self.addCleanup(s.close)
        return s

    def rows(self, sql, *args):
        c = sqlite3.connect(self.path)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()


def dump_text(path):
    """Every text value of every table, to prove what is NOT in the database."""
    c = sqlite3.connect(path)
    try:
        out = []
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in c.execute("SELECT * FROM %s" % t):
                out.extend(str(v) for v in row if isinstance(v, str))
        return "\n".join(out)
    finally:
        c.close()


# ---- schema, migration, file --------------------------------------------------------------------------------------------------

class Schema(TmpDir):
    TABLES = {"meta", "app_hour", "host_hour", "disk_day", "boots", "events", "log_day"}

    def test_creation(self):
        self.store()
        names = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue(self.TABLES <= names, names)
        meta = dict(self.rows("SELECT key, value FROM meta"))
        self.assertEqual(meta["schema_version"], str(history.SCHEMA_VERSION))
        self.assertEqual(meta["os"], nuc_config.OS_NAME)
        self.assertEqual(meta["created"], str(T0 + 100))
        cols = {r[1] for r in self.rows("PRAGMA table_info(host_hour)")}
        self.assertEqual(cols, {"hour", "cpu_avg", "cpu_max", "mem_avg", "mem_max", "swap_max", "temp_avg", "temp_max", "temp_high",
                                "throttle", "load_max", "samples"})
        self.assertEqual({r[1] for r in self.rows("PRAGMA table_info(events)")},
                         {"id", "ts", "kind", "subject", "detail", "source", "n"})
        self.assertEqual({r[1] for r in self.rows("PRAGMA table_info(log_day)")},
                         {"day", "source", "unit", "template", "n", "first", "last"})

    def test_wal_and_busy_timeout(self):
        s = self.store()
        self.assertEqual(s.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertGreaterEqual(s.conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    @unittest.skipUnless(os.name == "posix", "POSIX modes")
    def test_files_are_readable_by_everybody(self):
        old = os.umask(0o077)  # a service manager may start the collector with a strict umask
        try:
            s = self.store()
            s.add_hours({(T0 // 3600, "x"): history.app_row([(1.0, 10, 1)])}, {})
        finally:
            os.umask(old)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                self.assertEqual(stat.S_IMODE(os.stat(self.path + suffix).st_mode), 0o644, suffix)

    def test_migration_is_idempotent_and_keeps_data(self):
        s = self.store()
        s.add_events([{"ts": T0, "kind": "crash", "subject": "app", "detail": "d", "source": "t", "n": 2}])
        created = s.meta("created")
        s.close()
        for _ in range(3):
            s2 = self.store(now=T0 + 99999)
            self.assertEqual(s2.meta("created"), created)  # not rewritten
            self.assertEqual(s2.meta("schema_version"), str(history.SCHEMA_VERSION))
            s2.close()
        self.assertEqual(self.rows("SELECT subject, n FROM events"), [("app", 2)])
        conn = sqlite3.connect(self.path, isolation_level=None)
        history.migrate(conn)
        history.migrate(conn)
        conn.close()

    def test_old_database_without_a_version_is_adopted(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, subject TEXT, detail TEXT, source TEXT, n INTEGER DEFAULT 1)")
        conn.execute("INSERT INTO events(ts, kind, subject) VALUES (1, 'crash', 'old')")
        conn.commit()
        conn.close()
        self.store()
        self.assertEqual(self.rows("SELECT subject FROM events"), [("old",)])
        self.assertEqual(dict(self.rows("SELECT key, value FROM meta"))["schema_version"], "1")

    def test_stepwise_migration(self):
        s = self.store()
        s.close()
        ran = []
        with mock.patch.object(history, "SCHEMA_VERSION", 3), \
                mock.patch.object(history, "MIGRATIONS", {1: lambda c: ran.append(1), 2: lambda c: ran.append(2)}):
            s = self.store()
            self.assertEqual(s.meta("schema_version"), "3")
            s.close()
            self.assertEqual(ran, [1, 2])
            s = self.store()  # again: nothing to do
            self.assertEqual(ran, [1, 2])

    def test_a_newer_schema_is_refused_not_damaged(self):
        s = self.store()
        s.set_meta("schema_version", "99")
        s.close()
        with self.assertRaises(RuntimeError):
            history.Store(self.path)
        self.assertEqual(dict(self.rows("SELECT key, value FROM meta"))["schema_version"], "99")

    def test_a_damaged_file_is_set_aside_and_replaced(self):
        with open(self.path, "wb") as f:
            f.write(b"this is not a database" * 100)
        s = self.store()
        s.add_events([{"ts": T0, "kind": "crash", "subject": "a", "detail": "d", "source": "t"}])
        self.assertTrue([n for n in os.listdir(self.dir) if ".corrupt-" in n])
        self.assertEqual(self.rows("SELECT subject FROM events"), [("a",)])

    def test_open_ro(self):
        self.assertIsNone(history.open_ro(self.path))  # no history yet
        s = self.store()
        s.add_events([{"ts": T0, "kind": "oom", "subject": "java", "detail": "d", "source": "t"}])
        ro = history.open_ro(self.path)
        self.assertEqual(ro.execute("SELECT subject FROM events").fetchall(), [("java",)])
        self.assertEqual(history.meta_get(ro, "schema_version"), "1")
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO events(ts) VALUES (1)")  # never writes
        s.add_events([{"ts": T0 + 1, "kind": "oom", "subject": "go", "detail": "d", "source": "t"}])  # the writer is not blocked
        self.assertEqual(len(ro.execute("SELECT * FROM events").fetchall()), 2)
        ro.close()
        s.close()
        ro = history.open_ro(self.path)  # after a clean close
        self.assertEqual(len(ro.execute("SELECT * FROM events").fetchall()), 2)
        ro.close()

    def test_open_ro_of_something_else_is_none(self):
        with open(self.path, "wb") as f:
            f.write(b"x" * 500)
        self.assertIsNone(history.open_ro(self.path))


# ---- hourly rows: the upsert and the maths ------------------------------------------------------------------------------------

class Hours(TmpDir):
    def test_two_flushes_in_the_same_hour_add_up(self):
        s = self.store()
        h = T0 // 3600
        w = history.Window()
        w.add(T0 + 60, {"nginx": [2.0, 100, 2], "idle": [0.0, 10, 1]}, {"cpu": 10.0, "mem": 50.0, "temp": 60.0, "temp_high": 90.0, "throttle": 2, "load": 1.0})
        w.add(T0 + 120, {"nginx": [4.0, 300, 3]}, {"cpu": 30.0, "mem": 60.0, "swap": 5.0, "temp": 70.0, "throttle": 0, "load": 2.5})
        a, hst = w.rows()
        s.add_hours(a, hst)
        w2 = history.Window()
        w2.add(T0 + 1800, {"nginx": [6.0, 200, 5]}, {"cpu": 80.0, "mem": 40.0, "swap": 20.0, "temp": 80.0, "temp_high": 95.0, "throttle": 3, "load": 0.5})
        a, hst = w2.rows()
        s.add_hours(a, hst)
        ng = self.rows("SELECT cpu_s, rss_max, rss_avg, procs_max, samples FROM app_hour WHERE app = 'nginx' AND hour = ?", h)
        self.assertEqual(ng, [(12.0, 300, 200, 5, 3)])  # 2+4+6 s; max of 100/300/200; mean of the three; 5 processes at most
        self.assertEqual(self.rows("SELECT samples, cpu_s FROM app_hour WHERE app = 'idle'"), [(1, 0.0)])
        host = self.rows("SELECT cpu_avg, cpu_max, mem_avg, mem_max, swap_max, temp_avg, temp_max, temp_high, throttle, load_max, samples"
                         " FROM host_hour WHERE hour = ?", h)
        self.assertEqual(host, [(40.0, 80.0, 50.0, 60.0, 20.0, 70.0, 80.0, 95.0, 5, 2.5, 3)])

    def test_merge_functions_ignore_what_was_never_measured(self):
        old = {"cpu_avg": None, "cpu_max": None, "mem_avg": 10.0, "mem_max": 10.0, "swap_max": None, "temp_avg": None, "temp_max": None,
               "temp_high": None, "throttle": None, "load_max": None, "samples": 4}
        new = dict(old, cpu_avg=50.0, cpu_max=60.0, mem_avg=20.0, mem_max=30.0, samples=1)
        m = history.merge_host(old, new)
        self.assertEqual((m["cpu_avg"], m["cpu_max"]), (50.0, 60.0))
        self.assertEqual(m["mem_avg"], 12.0)
        self.assertIsNone(m["temp_avg"])
        self.assertIsNone(m["throttle"])
        self.assertEqual(m["samples"], 5)

    def test_samples_of_two_hours_make_two_rows(self):
        w = history.Window()
        w.add(T0 + 3590, {"a": [1.0, 5, 1]}, {"cpu": 1.0})
        w.add(T0 + 3650, {"a": [1.0, 5, 1]}, {"cpu": 3.0})
        a, h = w.rows()
        self.assertEqual(sorted(k[0] for k in a), [T0 // 3600, T0 // 3600 + 1])
        self.assertEqual(sorted(h), [T0 // 3600, T0 // 3600 + 1])

    def test_an_empty_sample_is_dropped(self):
        w = history.Window()
        w.add(T0, {}, {"cpu": None, "mem": None})
        self.assertEqual(w.rows(), ({}, {}))
        w.add(T0, {}, {"mem": 1.0})
        self.assertEqual(len(w.rows()[1]), 1)

    def test_the_window_is_bounded_while_the_database_is_down(self):
        w = history.Window()
        for i in range(history.MAX_PENDING + 50):
            w.add(T0 + i * 60, {"a": [1.0, 1, 1]}, {})
        self.assertEqual(len(w.snaps), history.MAX_PENDING)

    def test_cap_keeps_the_top_apps_and_the_big_ones_and_sums_the_rest(self):
        w = history.Window()
        for i in (0, 1):
            apps = {"app%03d" % n: [float(n), 1000 * (n + 1), 1] for n in range(300)}   # 300 apps: cpu_s = n
            apps["fat"] = [0.0, 150 * 1024 * 1024, 1]                                    # no CPU, but 150 MB: kept
            w.add(T0 + 60 * i, apps, {})
        a, _ = w.rows()
        names = {k[1] for k in a}
        self.assertIn("app299", names)
        self.assertIn("fat", names)
        self.assertIn(history.OTHER, names)
        self.assertNotIn("app000", names)
        self.assertEqual(len(names), history.APP_TOP + 1 + 1)  # the top, the fat one, (other)
        other = a[(T0 // 3600, history.OTHER)]
        small = [n for n in range(300) if "app%03d" % n not in names]
        self.assertEqual(other["cpu_s"], round(2.0 * sum(small), 3))
        self.assertEqual(other["rss_max"], sum(1000 * (n + 1) for n in small))
        self.assertEqual(other["procs_max"], len(small))
        self.assertEqual(other["samples"], 2)
        total = sum(r["cpu_s"] for r in a.values())
        self.assertEqual(total, round(2.0 * (sum(range(300))), 3))  # nothing is lost by the cap

    def test_the_cap_holds_over_the_whole_hour_not_only_per_flush(self):
        s = self.store(T0 + 600)
        h = T0 // 3600
        for flush in range(3):  # three flushes of the same hour, each with its own 150 apps: 450 in all
            rows = {(h, "app-%d-%03d" % (flush, i)): history.app_row([(float(1 + i + 10 * flush), 1000, 1)]) for i in range(150)}
            s.add_hours(rows, {})
        s.add_hours({(h, "big"): history.app_row([(0.0, 300 * 1024 * 1024, 1)])}, {})
        got = {r[0]: r for r in self.rows("SELECT app, cpu_s, rss_max FROM app_hour WHERE hour = ?", h)}
        self.assertEqual(len(got), history.APP_TOP + 1 + 1)  # the top ones, the big one, and (other)
        self.assertIn("big", got)
        self.assertAlmostEqual(sum(r[1] for r in got.values()), sum(1 + i + 10 * f for f in range(3) for i in range(150)))
        self.assertGreaterEqual(got[history.OTHER][2], 100 * 1000)  # what (some of) the small ones held together

    def test_names_and_subjects_are_sanitised(self):
        self.assertEqual(history.app_name("kworker/u16:3-events"), "kworker")
        self.assertEqual(history.app_name("ksoftirqd/12"), "ksoftirqd")
        self.assertEqual(history.app_name("kworker/R-rcu_gp"), "kworker")
        self.assertEqual(history.app_name("Web Content"), "Web Content")
        self.assertEqual(history.app_name("a/b"), "a/b")
        self.assertEqual(history.app_name(""), "(unnamed)")
        evil = "x\x1b[31m\u202eevil\x00" + "y" * 200
        c = history.clean(evil)
        self.assertLessEqual(len(c), 64)
        self.assertTrue(c.isprintable())
        self.assertNotIn("\x1b", c)
        self.assertEqual(history.clean("a\nb\tc"), "a b c")


class ProcAccounting(unittest.TestCase):
    def proc(self, pid, name, t, start=1000.0, mem=1024, cpu=None):
        return {"pid": pid, "name": name, "time": t, "start": start, "mem": mem, "cpu": cpu}

    def test_cpu_seconds_are_the_difference_of_cpu_time(self):
        a = history.ProcAccount(ncpu=4)
        first = a.update([self.proc(1, "x", 10.0), self.proc(2, "x", 5.0), self.proc(3, "y", None, mem=None)], 100.0, 5000.0)
        self.assertEqual(first["x"], [0.0, 2048, 2])  # nothing to compare with yet; memory and count are known
        self.assertEqual(first["y"], [0.0, 0, 1])
        got = a.update([self.proc(1, "x", 40.0), self.proc(2, "x", 5.5), self.proc(3, "y", None, mem=None)], 160.0, 5060.0)
        self.assertEqual(got["x"][0], 30.5)
        self.assertEqual(got["y"][0], 0.0)

    def test_a_new_process_had_used_nothing_before(self):
        a = history.ProcAccount(ncpu=4)
        a.update([self.proc(1, "x", 1.0)], 100.0, 5000.0)
        got = a.update([self.proc(1, "x", 1.0), self.proc(9, "job", 12.0, start=5030.0)], 160.0, 5060.0)
        self.assertEqual(got["job"][0], 12.0)

    def test_a_reused_pid_is_a_new_process(self):
        a = history.ProcAccount(ncpu=4)
        a.update([self.proc(1, "old", 500.0, start=1000.0)], 100.0, 5000.0)
        got = a.update([self.proc(1, "new", 3.0, start=5040.0)], 160.0, 5060.0)
        self.assertEqual(got["new"][0], 3.0)
        self.assertNotIn("old", got)

    def test_without_cpu_time_the_percentage_is_used(self):
        a = history.ProcAccount(ncpu=4)
        a.update([self.proc(1, "x", None, cpu=None)], 100.0, 5000.0)
        got = a.update([self.proc(1, "x", None, cpu=50.0)], 160.0, 5060.0)
        self.assertEqual(got["x"][0], 30.0)  # 50 % of a core for 60 s

    def test_a_bogus_counter_cannot_invent_cpu_time(self):
        a = history.ProcAccount(ncpu=2)
        a.update([self.proc(1, "x", 1.0)], 100.0, 5000.0)
        got = a.update([self.proc(1, "x", 10 ** 9)], 160.0, 5060.0)
        self.assertLessEqual(got["x"][0], 120.0)

    def test_macos_start_times_move_by_a_second(self):
        a = history.ProcAccount(ncpu=4)
        a.update([self.proc(1, "x", 10.0, start=1000.0)], 100.0, 5000.0)
        got = a.update([self.proc(1, "x", 12.0, start=1001.0)], 160.0, 5060.0)
        self.assertEqual(got["x"][0], 2.0)

    def test_garbage_rows_do_not_raise(self):
        a = history.ProcAccount()
        a.update([{"pid": 1, "name": None, "time": "x", "mem": "y"}, {}, {"name": "ok", "pid": 2, "time": 1.0}], 1.0, 1.0)


class HostFields(unittest.TestCase):
    def test_temperature_package_then_cores_then_sensors(self):
        self.assertEqual(history.pick_temp({"package": 61.5, "cores": {0: 70.0}, "high": 100.0}), (61.5, 100.0))
        self.assertEqual(history.pick_temp({"package": None, "cores": {"0": 55.0, "1": 66.0}}), (66.0, None))
        self.assertEqual(history.pick_temp({"sensors": [{"label": "a", "c": 40.0}, {"label": "b", "c": 45.5}]}), (45.5, None))
        self.assertEqual(history.pick_temp({}), (None, None))

    def test_sensors_json_only_when_fresh_and_only_as_a_fallback(self):
        # integer keys of sensors.json arrive as strings
        s = {"ts": 1000, "os": "windows", "cpu": {"package": None, "cores": {"0": 50.0, "1": 58.0}, "sensors": []}}
        self.assertEqual(history.pick_temp({}, s, now=1060), (58.0, None))
        self.assertEqual(history.pick_temp({}, s, now=1000 + 3600), (None, None))  # stale
        self.assertEqual(history.pick_temp({"package": 40.0}, s, now=1060), (40.0, None))  # cpuinfo first
        self.assertEqual(history.pick_temp({}, {"ts": 1000, "cpu": None}, now=1000), (None, None))
        self.assertEqual(history.pick_temp({}, "junk", now=1000), (None, None))

    def test_throttle_and_memory(self):
        self.assertEqual(history.throttle_count({"package": 12, "cores": {0: 5}}), 12)
        self.assertEqual(history.throttle_count({"package": None, "cores": {"0": 5, "1": 6}}), 11)
        self.assertIsNone(history.throttle_count({"package": None, "cores": {}}))
        self.assertIsNone(history.throttle_count(None))
        mem, swap = history.mem_percent({"MemTotal": 1000, "MemAvailable": 250, "SwapTotal": 200, "SwapFree": 150})
        self.assertEqual((mem, swap), (75.0, 25.0))
        self.assertEqual(history.mem_percent({"MemTotal": 1000, "MemAvailable": 250, "SwapTotal": 0, "SwapFree": 0}), (75.0, None))
        self.assertEqual(history.mem_percent({}), (None, None))
        info = history.parse_meminfo("MemTotal:       16384 kB\nMemAvailable:    8192 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\nOther: 5 kB\n")
        self.assertEqual(info, {"MemTotal": 16384 * 1024, "MemAvailable": 8192 * 1024, "SwapTotal": 0, "SwapFree": 0})

    def test_mounts_are_real_filesystems_once_per_device(self):
        text = ("/dev/sda1 / ext4 rw 0 0\n/dev/sda1 /bind ext4 rw 0 0\ntmpfs /run tmpfs rw 0 0\n/dev/sdb1 /data xfs rw 0 0\n"
                "/dev/sdc1 /var/lib/docker/overlay2 ext4 rw 0 0\n/dev/sdd1 /mnt/my\\040disk ext4 rw 0 0\nproc /proc proc rw 0 0\n")
        self.assertEqual(history.parse_mounts(text), ["/", "/data", "/mnt/my disk"])


# ---- retention, size ---------------------------------------------------------------------------------------------------------

class Retention(TmpDir):
    def test_prune_removes_what_is_old_and_keeps_the_rest(self):
        now = T0 + 500 * DAY
        s = self.store(now)
        c = s.conn
        for days, keep in ((29, True), (31, False)):
            c.execute("INSERT INTO app_hour VALUES (?, 'a', 1, 1, 1, 1, 1)", ((now - days * DAY) // 3600,))
            c.execute("INSERT INTO host_hour(hour, samples) VALUES (?, 1)", ((now - days * DAY) // 3600,))
            c.execute("INSERT INTO log_day VALUES (?, 'journal', 'u', 't', 1, 1, 1)", ((now - days * DAY) // DAY,))
        for days in (89, 91):
            c.execute("INSERT INTO events(ts, kind, subject, n) VALUES (?, 'crash', ?, 1)", (now - days * DAY, "d%d" % days))
        for days in (399, 401):
            c.execute("INSERT INTO disk_day VALUES (?, '/', 1, 2)", ((now - days * DAY) // DAY,))
            c.execute("INSERT INTO boots VALUES (?, 30.0, 'k')", (now - days * DAY,))
        gone = s.prune(now)
        self.assertEqual(gone, {"app_hour": 1, "host_hour": 1, "log_day": 1, "events": 1, "disk_day": 1, "boots": 1})
        self.assertEqual(self.rows("SELECT subject FROM events"), [("d89",)])
        self.assertEqual(len(self.rows("SELECT * FROM app_hour")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM disk_day")), 1)
        self.assertEqual(s.prune(now), {k: 0 for k in gone})  # again: nothing

    def test_vacuum_at_most_weekly(self):
        s = self.store(T0)
        self.assertTrue(s.vacuum_if_due(T0))
        self.assertFalse(s.vacuum_if_due(T0 + 6 * DAY))
        self.assertTrue(s.vacuum_if_due(T0 + 8 * DAY))
        self.assertEqual(s.meta("last_vacuum"), str(T0 + 8 * DAY))

    def test_events_older_than_their_retention_are_not_stored(self):
        s = self.store(T0 + 200 * DAY)
        s.add_events([{"ts": T0, "kind": "crash", "subject": "old", "detail": "d", "source": "t"},
                      {"ts": T0 + 199 * DAY, "kind": "crash", "subject": "new", "detail": "d", "source": "t"},
                      {"ts": T0 + 199 * DAY, "kind": "nonsense", "subject": "x", "detail": "d", "source": "t"}])
        self.assertEqual(self.rows("SELECT subject FROM events"), [("new",)])

    def test_thirty_days_of_two_hundred_apps_stay_small(self):
        s = self.store(T0 + 31 * DAY)
        names = ["application-number-%03d.exe" % i for i in range(200)]
        t0 = time.time()
        for hour in range(30 * 24):
            apps = {n: history.app_row([(1.5 + i % 7, 40_000_000 + i * 1000, 1 + i % 4), (0.5, 41_000_000, 2)]) for i, n in enumerate(names)}
            s.add_hours({(T0 // 3600 + hour, n): r for n, r in apps.items()},
                        {T0 // 3600 + hour: history.host_row([{"cpu": 12.5, "mem": 50.0, "swap": 1.0, "load": 0.5, "temp": 55.0, "throttle": 0}])})
        took = time.time() - t0
        s.close()
        size = sum(os.path.getsize(self.path + x) for x in ("", "-wal", "-shm") if os.path.exists(self.path + x))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM app_hour"), [(200 * 30 * 24,)])
        self.assertLess(size, 25 * 1024 * 1024, "history.db is %.1f MB" % (size / 1e6))
        self.assertLess(took, 120)


class Logs(TmpDir):
    def test_log_templates_count_per_day_and_unit(self):
        s = self.store()
        day = T0 // DAY
        s.add_events([], [{"ts": T0, "source": "journal", "unit": "a.service", "template": "failed <n>"},
                          {"ts": T0 + 50, "source": "journal", "unit": "a.service", "template": "failed <n>"},
                          {"ts": T0 + 10, "source": "journal", "unit": "b.service", "template": "failed <n>"}])
        s.add_events([], [{"ts": T0 + 5, "source": "journal", "unit": "a.service", "template": "failed <n>"}])
        rows = self.rows('SELECT unit, n, "first", "last" FROM log_day WHERE day = ? ORDER BY unit', day)
        self.assertEqual(rows, [("a.service", 3, T0, T0 + 50), ("b.service", 1, T0 + 10, T0 + 10)])

    def test_a_flood_of_distinct_templates_is_capped(self):
        s = self.store()
        with mock.patch.object(history, "LOG_DAY_ROWS", 5):
            s.add_events([], [{"ts": T0, "source": "journal", "unit": "u", "template": "t%d" % i} for i in range(20)])
            s.add_events([], [{"ts": T0, "source": "journal", "unit": "u", "template": "t0"}])  # known ones still count
        self.assertEqual(self.rows("SELECT COUNT(*) FROM log_day"), [(5,)])
        self.assertEqual(self.rows("SELECT n FROM log_day WHERE template = 't0'"), [(2,)])

    def test_events_and_cursors_are_one_transaction(self):
        s = self.store()
        bad = [{"ts": T0, "kind": "crash", "subject": "a", "detail": "d", "source": "t"}, {"kind": "crash"}]  # the second one breaks
        with self.assertRaises(Exception):
            s.add_events(bad, [], {"journal_cursor": "s=1"})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM events"), [(0,)])
        self.assertIsNone(s.meta("journal_cursor"))
        s.add_events(bad[:1], [], {"journal_cursor": "s=1"})  # the store is usable afterwards
        self.assertEqual(s.meta("journal_cursor"), "s=1")


# ---- template() ---------------------------------------------------------------------------------------------------------------

class Template(unittest.TestCase):
    def secrets(self):  # built at runtime: nothing here looks like a real secret in the repository
        rnd = lambda n, alphabet: "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(n))  # noqa: E731
        return {"pw": "Pa" + "ss" + "w0rd" + "!9", "token": rnd(40, "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"),
                "b64": rnd(64, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") + "==",
                "jwt": "eyJ" + rnd(20, "abcdefghijklmnop") + "." + "eyJ" + rnd(30, "qrstuvwxyz0123") + "." + rnd(43, "ABCDEFGHIJ0123456789_-"),
                "uuid": "123e4567-e89b-12d3-a456-426614174000", "hex": "deadbeefcafe0123", "user": "alice", "key": "hunter2"}

    def gone(self, tpl, *needles):
        for n in needles:
            self.assertNotIn(n, tpl, "%r survived in %r" % (n, tpl))

    def test_key_value_pairs(self):
        s = self.secrets()
        for text in ("login failed password=%(pw)s user=%(user)s" % s, "connect api_key=%(token)s retry=3" % s,
                     'auth token="%(pw)s" done' % s, "secret:%(key)s next" % s, "Password: %(pw)s" % s,
                     "the password is %(key)s" % s, "--password %(key)s --user=%(user)s" % s, "x PASSWORD=%(pw)s;" % s):
            t = history.template(text)
            self.gone(t, s["pw"], s["token"], s["key"], "alice")
            self.assertNotRegex(t, r"\d", t)
        self.assertEqual(history.template("rhost= user=bob uid=1000"), "rhost=<v> user=<v> uid=<v>")

    def test_a_secret_said_after_its_name(self):
        self.assertEqual(history.template("login failed user bob pass s3cr3t!"), "login failed user <str> pass <v>")
        self.assertEqual(history.template("passwd Abc!2345 rejected"), "passwd <v> rejected")
        self.assertEqual(history.template("secret 1234 leaked"), "secret <v> leaked")
        # plain words stay: they are the program's own wording
        self.assertEqual(history.template("password required"), "password required")
        self.assertEqual(history.template("token expired"), "token expired")

    def test_urls_with_credentials(self):
        s = self.secrets()
        t = history.template("fetch https://%(user)s:%(pw)s@repo.example.com:8443/a/b?token=%(token)s&x=1 failed" % s)
        self.assertEqual(t, "fetch <url> failed")
        self.gone(history.template("postgres://%(user)s:%(pw)s@db.example.com/app" % s), s["pw"], "alice", "db.example.com")
        self.gone(history.template("ftp://203.0.113.9/file and http://[2001:db8::1]:80/x"), "203", "2001", "file")

    def test_emails_addresses_and_hosts_of_people(self):
        t = history.template("mail to alice@example.com and root@localhost bounced")
        self.assertEqual(t, "mail to <email> and <email> bounced")
        self.gone(history.template("Accepted publickey for bob from 192.0.2.44 port 51234"), "192", "51234")
        # unit instance names are not addresses
        self.assertEqual(history.template("getty@tty1.service: start"), "getty@tty<n>.service: start")

    def test_ipv4_ipv6(self):
        for ip in ("192.0.2.17", "198.51.100.200", "203.0.113.5:8080", "10.0.0.0/8", "2001:db8::1", "2001:db8:0:0:0:0:0:2",
                   "fe80::1ff:fe23:4567:890a", "::1", "[2001:db8::2]:443", "::ffff:192.0.2.1", "fe80::1%eth0"):
            t = history.template("peer %s closed" % ip)
            self.assertEqual(t.split(" ")[0], "peer", t)
            self.assertNotRegex(t, r"[0-9a-f]:|\.\d|db8", t)
        self.assertEqual(history.template("at 12:34:56 done"), "at <n> done")  # a time is not an address

    def test_hex_uuid_paths(self):
        s = self.secrets()
        t = history.template("object %(hex)s id %(uuid)s mac aa:bb:cc:dd:ee:ff addr 0xffff8800deadbeef" % s)
        self.assertEqual(t, "object <hex> id <uuid> mac <hex> addr <hex>")
        for text in ("open /home/alice/.ssh/id_rsa failed", "cannot read /var/lib/app/data.db: denied", "C:\\Users\\alice\\secret.txt missing",
                     "\\\\server\\share\\dir\\f.txt", "load ~/.config/app/token", "see usr/lib/x86_64-linux-gnu/libfoo.so", "(/etc/shadow)"):
            t = history.template(text)
            self.gone(t, "alice", "shadow", "secret", "id_rsa", "server", "token", "libfoo", "data.db")
        self.assertEqual(history.template("and/or I/O error"), "and/or I/O error")

    def test_quoted_strings(self):
        s = self.secrets()
        t = history.template("""user "%(pw)s" typed 'two words here' and `quoted' then "unterminated %(key)s""" % s)
        self.gone(t, s["pw"], "two words", "quoted", s["key"], "unterminated")
        self.assertEqual(history.template("can't open file 'foo bar'"), "can't open file <str>")

    def test_tokens_and_long_base64(self):
        s = self.secrets()
        for tok in (s["jwt"], s["token"], s["b64"], "ghp_" + s["token"], "AKIA" + s["token"][:16].upper()):
            t = history.template("Authorization failed for %s end" % tok)
            self.gone(t, tok, tok[:12])
        t = history.template("Authorization: Bearer %(token)s" % s)
        self.gone(t, s["token"])
        self.gone(history.template("Basic dXNlcjpwYXNzd29yZA== sent"), "dXNl")
        self.assertEqual(history.template("systemd-networkd-wait-online started"), "systemd-networkd-wait-online started")  # words stay

    def test_user_names_after_the_word_user(self):
        self.gone(history.template("Invalid user mallory from 192.0.2.5"), "mallory")
        self.gone(history.template("session opened for user alice(uid=1000) by (uid=0)"), "alice")

    def test_numbers_and_shape(self):
        self.assertEqual(history.template("Out of memory: Killed process 4242 (java) total-vm:812kB"),
                         "Out of memory: Killed process <n> (java) total-vm:<v>")
        self.assertEqual(history.template("kernel 5.15.0-91-generic booted in 3.5 s"), "kernel <n>-generic booted in <n> s")
        self.assertEqual(history.template("nginx.service: Failed with result 'exit-code'."), "nginx.service: Failed with result <str>.")
        self.assertEqual(history.template(""), "")
        self.assertEqual(history.template(None), "")
        self.assertEqual(history.template(12345), "<n>")

    def test_one_line_and_length(self):
        self.assertEqual(history.template("first line 1\nsecond line password=x\nthird"), "first line <n>")
        self.assertEqual(history.template("\n\nreal line"), "real line")
        long = history.template("word " * 400)
        self.assertLessEqual(len(long), history.TEMPLATE_MAX)
        cut = history.template("a" * 150 + " 123456")  # the cut never leaves half a placeholder
        self.assertNotRegex(cut, r"<[^>]*$")
        weird = history.template("a\x1b[31m\u202e\x00b")
        self.assertTrue(weird.isprintable())

    def test_idempotent_and_digit_free(self):
        for text in ("Failed password for root from 192.0.2.1 port 22 ssh2", "usb 1-1: device descriptor read/64, error -71",
                     "EXT4-fs (sda1): error count since last fsck: 3", "segfault at 0 ip 00007f1234 sp 00007ffe error 4 in libc.so.6[7f12+2000]"):
            t = history.template(text)
            self.assertEqual(history.template(t), t)
            self.assertNotRegex(t, r"\d")

    def test_never_raises_and_is_fast(self):
        nasty = ("::::::::" * 500) + ("\\" * 500) + ("a/" * 500) + ("\"'`" * 300) + ("1.2.3." * 300) + ("-" * 4000)
        t0 = time.time()
        for text in (nasty, nasty[::-1], "A" * 100000, " " * 5000, "=" * 3000, "a=" * 2000, "<" * 3000):
            self.assertIsInstance(history.template(text), str)
        self.assertLess(time.time() - t0, 5)


# ---- Linux: the journal --------------------------------------------------------------------------------------------------------

def jl(msg, pr=3, ident="kernel", ts=1_790_000_000, cursor=None, **fields):
    d = {"MESSAGE": msg, "PRIORITY": str(pr), "SYSLOG_IDENTIFIER": ident, "__REALTIME_TIMESTAMP": str(int(ts * 1e6)),
         "__CURSOR": cursor or "s=0a;i=%x;b=0b;m=1;t=1;x=1" % ts}
    d.update(fields)
    return json.dumps(d)


class Journal(unittest.TestCase):
    def parse(self, lines, source="journal"):
        return history.parse_journal_events("\n".join(lines) + "\n", source, now=1_790_000_500)

    def one(self, got, kind):
        rows = [e for e in got["events"] if e["kind"] == kind]
        self.assertEqual(len(rows), 1, got["events"])
        return rows[0]

    def test_oom_kill(self):
        got = self.parse([jl("Out of memory: Killed process 4242 (java) total-vm:8123456kB, anon-rss:7000000kB, UID:1000", 3, _TRANSPORT="kernel"),
                          jl("Memory cgroup out of memory: Killed process 77 (Web Content) total-vm:1kB", 3, _TRANSPORT="kernel")])
        subjects = sorted(e["subject"] for e in got["events"] if e["kind"] == "oom")
        self.assertEqual(subjects, ["Web Content", "java"])
        self.assertEqual(got["events"][0]["source"], "journal")

    def test_oom_message_from_a_user_process_is_not_the_kernel(self):
        got = self.parse([jl("Out of memory: Killed process 1 (x)", 3, ident="myapp", _TRANSPORT="journal")])
        self.assertEqual([e for e in got["events"] if e["kind"] == "oom"], [])

    def test_segfault_and_traps(self):
        got = self.parse([jl("myapp[1234]: segfault at 0 ip 000055d0c0a1b2c3 sp 00007ffd error 4 in myapp[55d0c0a1a000+2000]", 6, _TRANSPORT="kernel"),
                          jl("traps: other app[99] general protection fault ip:7f12 sp:7ffe error:0 in libc.so.6[7f00+1000]", 6, _TRANSPORT="kernel"),
                          jl("traps: third[5] trap invalid opcode ip:1 sp:2 error:0", 6, _TRANSPORT="kernel")], "kernel")
        self.assertEqual(sorted((e["kind"], e["subject"]) for e in got["events"]),
                         [("crash", "myapp"), ("crash", "other app"), ("crash", "third")])
        self.assertEqual(got["logs"], [])  # the kernel source never feeds the log templates

    def test_service_failed(self):
        got = self.parse([jl("nginx.service: Failed with result 'exit-code'.", 4, ident="systemd", _SYSTEMD_UNIT="init.scope"),
                          jl("nginx.service: Failed with result 'exit-code'.", 4, ident="systemd"),
                          jl("db.service: Failed with result 'a-new-and-odd-result'.", 4, ident="systemd")])
        rows = {e["subject"]: e for e in got["events"] if e["kind"] == "service_failed"}
        self.assertEqual(rows["nginx.service"]["n"], 2)
        self.assertEqual(rows["nginx.service"]["detail"], "failed with result exit-code")
        self.assertEqual(rows["db.service"]["detail"], "failed with result other")  # a fixed vocabulary: never text from the log
        self.assertEqual(got["logs"], [])  # warnings are events at most, not log templates

    def test_main_process_exit(self):
        got = self.parse([jl("api.service: Main process exited, code=dumped, status=11/SEGV", 5, ident="systemd"),
                          jl("job.service: Main process exited, code=killed, status=9/KILL", 5, ident="systemd"),
                          jl("job2.service: Main process exited, code=exited, status=1/FAILURE", 5, ident="systemd"),
                          jl("ok.service: Main process exited, code=exited, status=0/SUCCESS", 5, ident="systemd")])
        kinds = sorted((e["kind"], e["subject"], e["detail"]) for e in got["events"])
        self.assertEqual(kinds, [("crash", "api.service", "main process dumped core (SEGV)"),
                                 ("exit_error", "job.service", "main process killed (KILL)"),
                                 ("exit_error", "job2.service", "main process exited with status 1")])

    def test_coredump_and_the_doubles(self):
        core = jl("Process 4321 (worker) of user 1000 dumped core.\n\nStack trace of thread 4321:\n#0 0x00007f in foo", 2,
                  ident="systemd-coredump", COREDUMP_EXE="/usr/lib/app/worker", COREDUMP_COMM="worker")
        got = self.parse([core, jl("worker[4321]: segfault at 0 ip 0000 sp 0000 error 4", 6, _TRANSPORT="kernel")])
        self.assertEqual(len([e for e in got["events"] if e["kind"] == "crash"]), 2)  # one reader sees both ...
        crash = self.one({"events": history.dedupe_crashes(got["events"])}, "crash")  # ... the pass keeps one
        self.assertEqual((crash["subject"], crash["detail"]), ("worker", "core dumped"))
        self.assertEqual(got["logs"][0]["unit"], "systemd-coredump")
        self.assertNotRegex(got["logs"][0]["template"], r"\d")
        # the same crash seen by three readers is one crash
        k = self.parse([jl("worker[4321]: segfault at 0 ip 0000 sp 0000 error 4", 6, _TRANSPORT="kernel")], "kernel")["events"]
        m = self.parse([jl("worker.service: Main process exited, code=dumped, status=11/SEGV", 5, ident="systemd")])["events"]
        c = self.parse([core])["events"]
        both = history.dedupe_crashes(history.merge_events(k + m + c))
        self.assertEqual([(e["subject"], e["detail"], e["n"]) for e in both if e["kind"] == "crash"], [("worker", "core dumped", 1)])
        # two segfaults, one core dump: one is left
        two = history.dedupe_crashes(history.merge_events([dict(k[0], n=1), dict(k[0], n=1, ts=k[0]["ts"] + 1), c[0]]))
        self.assertEqual(sorted((e["detail"], e["n"]) for e in two), [("core dumped", 1), ("segmentation fault", 1)])
        # a crash of another app is never absorbed
        other = history.dedupe_crashes(history.merge_events(k + [dict(c[0], subject="another")]))
        self.assertEqual(len([e for e in other if e["kind"] == "crash"]), 2)

    def test_ssh_failures_are_counted_never_identified(self):
        lines = [jl("Failed password for invalid user admin from 192.0.2.44 port 40222 ssh2", 6, ident="sshd"),
                 jl("Failed password for root from 198.51.100.7 port 40223 ssh2", 6, ident="sshd-session"),
                 jl("Invalid user mallory from 203.0.113.9 port 22", 6, ident="sshd"),
                 jl("Accepted publickey for carol from 192.0.2.50 port 22 ssh2", 6, ident="sshd"),
                 jl("Failed password for dave from 192.0.2.44 port 1 ssh2", 6, ident="notsshd")]
        got = self.parse(lines, "sshd")
        self.assertEqual([(e["kind"], e["subject"], e["n"]) for e in got["events"]], [("login_fail", "sshd", 3)])
        text = json.dumps(got)
        for secret in ("192.0.2", "198.51.100", "203.0.113", "admin", "mallory", "root", "dave", "carol"):
            self.assertNotIn(secret, text)
        # and the journal source does not count them a second time
        self.assertEqual(self.parse(lines, "journal")["events"], [])

    def test_log_templates_priority_3_and_worse(self):
        got = self.parse([jl("Out of memory: Killed process 1 (a)", 3, _TRANSPORT="kernel"),
                          jl("disk error on sda1 sector 1234", 3, ident="smartd", _SYSTEMD_UNIT="smartd.service"),
                          jl("warning only", 4, ident="x"), jl("password=" + "hunter2" + " rejected", 2, ident="app"),
                          jl([104, 105], 3, ident="bin")])
        self.assertEqual([(g["unit"], g["template"]) for g in got["logs"]],
                         [("kernel", "Out of memory: Killed process <n> (a)"), ("smartd.service", "disk error on sda<n> sector <n>"),
                          ("app", "password=<v> rejected"), ("bin", "hi")])
        self.assertNotIn("hunter2", json.dumps(got))

    def test_cursor_timestamp_cap_and_garbage(self):
        lines = [jl("a", 3, cursor="s=1;i=1;b=2;m=3;t=4;x=5", ts=1_790_000_001), "not json", "[1, 2]", "",
                 jl("b", 3, cursor="s=1;i=2;b=2;m=3;t=4;x=5", ts=1_790_000_002),
                 json.dumps({"MESSAGE": "no timestamp", "PRIORITY": "3", "__CURSOR": "bad cursor with spaces"})]
        got = self.parse(lines)
        self.assertEqual(got["read"], 3)
        self.assertEqual(got["cursor"], "s=1;i=2;b=2;m=3;t=4;x=5")  # the bad one is not taken
        capped = self.parse(lines, cap=1) if False else history.parse_journal_events("\n".join(lines), "journal", cap=1)
        self.assertTrue(capped["capped"])
        self.assertEqual(capped["cursor"], "s=1;i=1;b=2;m=3;t=4;x=5")
        self.assertEqual(capped["ts"], 1_790_000_001)
        empty = self.parse([])
        self.assertEqual((empty["events"], empty["cursor"], empty["capped"]), ([], None, False))

    def test_hostile_names_are_sanitised(self):
        got = self.parse([jl("Out of memory: Killed process 4242 (evil\x1b[31m%s) total-vm:1kB" % ("x" * 300), 3, _TRANSPORT="kernel")])
        subject = got["events"][0]["subject"]
        self.assertLessEqual(len(subject), 64)
        self.assertNotIn("\x1b", subject)


# ---- Docker --------------------------------------------------------------------------------------------------------------------

def dstate(restarts=0, oom=False, exit=0, status="running", started="2026-09-30T10:00:00Z"):
    return {"restarts": restarts, "oom": oom, "exit": exit, "status": status, "started": started}


class DockerState(unittest.TestCase):
    def kinds(self, prev, cur):
        return sorted((e["kind"], e["subject"], e["n"]) for e in history.docker_events(prev, cur, T0))

    def test_parse(self):
        text = ("/web|2|false|0|running|2026-09-30T10:00:00.123Z\n/db|0|true|137|exited|2026-09-29T08:00:00Z\n"
                "garbage\n/x|a|b|c|d|e\n/we\x1bird|0|false|0|running|t\n")
        got = history.parse_docker_state(text)
        self.assertEqual(got["web"], dstate(2, False, 0, "running", "2026-09-30T10:00:00.123Z"))
        self.assertTrue(got["db"]["oom"] and got["db"]["exit"] == 137)
        self.assertEqual(len(got), 3)
        self.assertNotIn("\x1b", "".join(got))

    def test_first_look_is_only_a_baseline(self):
        self.assertEqual(self.kinds(None, {"a": dstate(5, True, 137, "exited")}), [])

    def test_restart_counter(self):
        self.assertEqual(self.kinds({"a": dstate(1)}, {"a": dstate(4)}), [("restart", "a", 3)])
        self.assertEqual(self.kinds({"a": dstate(4)}, {"a": dstate(4)}), [])
        self.assertEqual(self.kinds({"a": dstate(4)}, {"a": dstate(0)}), [])  # the container was recreated: not a restart

    def test_a_manual_restart_is_one_restart_a_start_is_none(self):
        self.assertEqual(self.kinds({"a": dstate()}, {"a": dstate(started="2026-09-30T11:00:00Z")}), [("restart", "a", 1)])
        stopped = dstate(status="exited", started="2026-09-30T10:00:00Z")
        self.assertEqual(self.kinds({"a": stopped}, {"a": dstate(started="2026-09-30T11:00:00Z")}), [])

    def test_oom_is_reported_once(self):
        was = {"a": dstate()}
        now = {"a": dstate(oom=True, exit=137, status="exited")}
        self.assertEqual(self.kinds(was, now), [("oom", "a", 1)])  # not also an exit_error: one incident
        self.assertEqual(self.kinds(now, now), [])
        again = {"a": dstate(oom=True, exit=137, status="exited", started="2026-09-30T12:00:00Z")}
        self.assertEqual(self.kinds(now, again), [("oom", "a", 1)])  # killed again after a restart

    def test_exit_code_newly_not_zero(self):
        self.assertEqual(self.kinds({"a": dstate()}, {"a": dstate(exit=1, status="exited")}), [("exit_error", "a", 1)])
        self.assertEqual(self.kinds({"a": dstate(exit=1, status="exited")}, {"a": dstate(exit=1, status="exited")}), [])
        self.assertEqual(self.kinds({"a": dstate()}, {"a": dstate(exit=0, status="exited")}), [])  # a clean stop
        self.assertEqual(self.kinds({"a": dstate(exit=1, status="exited")}, {"a": dstate(exit=2, status="exited")}), [("exit_error", "a", 1)])
        self.assertEqual(self.kinds({}, {"new": dstate(exit=3, status="exited")}), [("exit_error", "new", 1)])

    def test_the_command_is_fixed_and_ids_are_checked(self):
        self.assertIn("{{.State.OOMKilled}}", history.DOCKER_FORMAT)
        self.assertTrue(history.CONTAINER_ID.match("a" * 64))
        self.assertFalse(history.CONTAINER_ID.match("--privileged"))
        self.assertFalse(history.CONTAINER_ID.match("abc"))


# ---- Windows: the Event Log --------------------------------------------------------------------------------------------------

def ev(eid, prov, rec, level=2, arg=None, t=1_790_000_000, **extra):
    d = {"Id": eid, "ProviderName": prov, "LogName": "System", "RecordId": rec, "TimeCreated": t, "Level": level, "Arg0": arg}
    d.update(extra)
    return d


class WindowsEvents(unittest.TestCase):
    def data(self, system, application=(), max_system=None, errors=None):
        return {"logs": {"System": {"max": max_system or max([e["RecordId"] for e in system] or [0]), "events": list(system)},
                         "Application": {"max": 900, "events": list(application)}}, "errors": errors or {}}

    def test_the_ids_that_matter(self):
        loc = "Le service Spouleur d'impression s'est arrêté de manière inattendue. C:\\Users\\alice\\x"  # localized text: ignored
        got = cwin.parse_events(self.data(
            [ev(7031, "Service Control Manager", 11, 2, "Print Spooler", Message=loc),
             ev(7034, "Service Control Manager", 12, 2, "Audio Service"),
             ev(7000, "Service Control Manager", 13, 2, "Foo Service"), ev(7001, "Service Control Manager", 14, 2, "Bar"),
             ev(7009, "Service Control Manager", 15, 2, "Baz"), ev(7023, "Service Control Manager", 16, 2, "Qux"),
             ev(7024, "Service Control Manager", 17, 2, "Quux"),
             ev(41, "Microsoft-Windows-Kernel-Power", 18, 1), ev(17, "Microsoft-Windows-WHEA-Logger", 19, 3),
             ev(2004, "Microsoft-Windows-Resource-Exhaustion-Detector", 20, 3),
             ev(7036, "Service Control Manager", 21, 3, "Not Interesting")],
            [ev(1000, "Application Error", 800, 2, "contoso.exe", LogName="Application"),
             ev(1002, "Application Hang", 801, 2, "slow.exe", LogName="Application"),
             ev(1000, "Some Other Source", 802, 2, "x", LogName="Application")]))
        by = sorted((e["kind"], e["subject"]) for e in got["events"])
        self.assertEqual(by, sorted([("crash", "Print Spooler"), ("crash", "Audio Service"), ("service_failed", "Foo Service"),
                                     ("service_failed", "Bar"), ("service_failed", "Baz"), ("service_failed", "Qux"),
                                     ("service_failed", "Quux"), ("unexpected_shutdown", "system"), ("hw_error", "WHEA"),
                                     ("oom", "low virtual memory"), ("crash", "contoso.exe"), ("hang", "slow.exe")]))
        self.assertEqual({e["source"] for e in got["events"]}, {"eventlog"})
        self.assertEqual(got["cursors"], {"System": 21, "Application": 900})
        # Level 1-2 events are also log templates "<Provider> <Id>"
        tmpl = sorted((g["unit"], g["template"]) for g in got["logs"])
        self.assertIn(("Service Control Manager", "Service Control Manager 7031"), tmpl)
        self.assertIn(("Microsoft-Windows-Kernel-Power", "Microsoft-Windows-Kernel-Power 41"), tmpl)
        self.assertNotIn(("Microsoft-Windows-WHEA-Logger", "Microsoft-Windows-WHEA-Logger 17"), tmpl)  # a warning
        text = json.dumps(got)
        self.assertNotIn("arrêté", text)
        self.assertNotIn("alice", text)

    def test_one_event_alone_and_an_empty_log(self):
        d = {"logs": {"System": {"max": 5, "events": ev(41, "Microsoft-Windows-Kernel-Power", 5, 1)},  # ConvertTo-Json on one item
                      "Application": {"max": 0, "events": []}}, "errors": {}}
        got = cwin.parse_events(d)
        self.assertEqual([e["kind"] for e in got["events"]], ["unexpected_shutdown"])
        self.assertEqual(got["cursors"], {"System": 5, "Application": 0})

    def test_a_log_that_failed_keeps_its_cursor(self):
        got = cwin.parse_events(self.data([ev(41, "Microsoft-Windows-Kernel-Power", 5, 1)], errors={"Application": "UnauthorizedAccess"}))
        self.assertEqual(list(got["cursors"]), ["System"])
        self.assertEqual(list(got["errors"]), ["Application"])
        self.assertEqual(len(got["events"]), 1)

    def test_garbage_does_not_raise(self):
        for junk in (None, [], "x", {"logs": None}, {"logs": {"System": "x"}}, {"logs": {"System": {"max": "zz", "events": [1, None]}}},
                     {"logs": {"System": {"max": 1, "events": [{"Id": "x"}, {"Id": 5}, {"ProviderName": 1}]}}}):
            got = cwin.parse_events(junk)
            self.assertEqual(set(got), {"events", "logs", "cursors", "errors"})

    def test_hostile_names(self):
        got = cwin.parse_events(self.data([ev(1000, "Application Error", 1, 2, "evil\x1b[31m" + "x" * 200)]))
        self.assertLessEqual(len(got["events"][0]["subject"]), 64)
        self.assertNotIn("\x1b", got["events"][0]["subject"])

    def test_the_script_is_fixed_text_plus_integers(self):
        base = cwin.events_script({})
        self.assertIn("$cur = @{ System = 0; Application = 0 }", base)
        s = cwin.events_script({"System": 123, "Application": "456"}, 600)
        self.assertIn("System = 123; Application = 456", s)
        self.assertIn("$first = 600", s)
        self.assertEqual(s.replace("System = 123; Application = 456", "System = 0; Application = 0").replace("$first = 600", "$first = 3600"), base)
        for evil in ("1; calc", "x", None, -5):
            try:
                cwin.events_script({"System": evil})
            except ValueError:
                continue  # refused: also fine
        self.assertIn("System = 0;", cwin.events_script({"System": -5}))
        self.assertNotIn("Message", cwin.PS_EVENTS_BODY.replace("-Message", ""))  # the message text is never read
        self.assertIn("-MaxEvents 2000", cwin.PS_EVENTS_BODY)

    @unittest.skipUnless(shutil.which("pwsh") or (sys.platform == "win32" and shutil.which("powershell")), "PowerShell")
    def test_the_script_parses(self):
        exe = shutil.which("pwsh") or shutil.which("powershell")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "events.ps1")
            with open(p, "w", encoding="utf-8") as f:
                f.write(cwin.events_script({"System": 7}))
            cmd = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$t, [ref]$e); "
                   "if ($e.Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 1 }" % p.replace("'", "''"))
            r = __import__("subprocess").run([exe, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ---- macOS: crash reports ----------------------------------------------------------------------------------------------------

class MacReports(TmpDir):
    def report(self, name, head, mtime, sub=""):
        d = os.path.join(self.dir, "Library", "Logs", "DiagnosticReports", sub)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(head if isinstance(head, bytes) else head.encode())
        os.utime(p, (mtime, mtime))
        return p

    def dirs(self):
        return (os.path.join(self.dir, "Library", "Logs", "DiagnosticReports"),)

    def test_headers(self):
        P = cmac.parse_report_head
        self.assertEqual(P(b'{"app_name":"Safari","timestamp":"2026-09-30 10:00:00.00 +0200","bug_type":"309","os_version":"macOS 15"}\n{"body":1}',
                           "Safari-2026-09-30-100000.ips"), ("crash", "Safari", "crash report (bug_type 309)"))
        self.assertEqual(P(b'{"app_name":"Spinny","bug_type":"298"}\n', "Spinny.ips")[:2], ("hang", "Spinny"))
        self.assertEqual(P(b'{"app_name":"Spinny","bug_type":"211"}\n', "Spinny.ips")[:2], ("hang", "Spinny"))
        self.assertEqual(P(b'{"bug_type":"298","name":"JetsamEvent","os_version":"macOS 15"}\n', "JetsamEvent-2026-09-30-100000.ips"),
                         ("oom", "memory pressure", "Jetsam report"))
        self.assertEqual(P(b'Process:               Old App [1234]\nPath: /Applications/Old.app/x\n', "Old App_2020.crash")[:2], ("crash", "Old App"))
        self.assertEqual(P(b'Date/Time: x\nProcess:  Frozen [9]\n', "Frozen_2020.hang")[:2], ("hang", "Frozen"))
        self.assertIsNone(P(b'{"app_name":"x","bug_type":"385"}\n', "x.ips"))  # not a trouble report
        self.assertIsNone(P(b"", "x.ips"))
        self.assertIsNone(P(b"\xff\xfe\x00garbage", "x.crash"))
        self.assertIsNone(P(b"{broken json", "x.ips"))

    def test_scan_cursor_cap_and_links(self):
        now = 1_790_000_000
        for i in range(5):
            self.report("App%d-2026.ips" % i, '{"app_name":"App%d","bug_type":"309"}\n{}' % i, now - 1000 + i)
        self.report("Old.ips", '{"app_name":"Old","bug_type":"309"}\n', now - 10 ** 6)
        self.report("notes.txt", "hello", now)
        self.report("Retired.crash", "Process: Deep [1]\n", now, sub="Retired")  # subdirectories are not scanned
        secret = os.path.join(self.dir, "secret")
        with open(secret, "w") as f:
            f.write('{"app_name":"FromLink","bug_type":"309"}\n')
        if hasattr(os, "symlink"):
            try:
                os.symlink(secret, os.path.join(self.dirs()[0], "Link.ips"))
            except (OSError, NotImplementedError):
                pass
        evs, newest = cmac.diag_events(now - 2000, cap=3, dirs=self.dirs(), now=now)
        self.assertEqual([e["subject"] for e in evs], ["Old", "App0", "App1", "App2"][1:])  # oldest first, cap 3, Old is before the cursor
        self.assertEqual(newest, now - 1000 + 2)
        evs2, newest2 = cmac.diag_events(newest, cap=3, dirs=self.dirs(), now=now)
        self.assertEqual([e["subject"] for e in evs2], ["App3", "App4"])
        self.assertEqual(newest2, now - 1000 + 4)
        self.assertEqual(cmac.diag_events(newest2, dirs=self.dirs(), now=now), ([], newest2))
        self.assertNotIn("FromLink", json.dumps(evs + evs2))

    def test_only_the_head_is_read(self):
        p = self.report("Big.ips", '{"app_name":"Big","bug_type":"309"}\n' + "x" * 5_000_000, 1_790_000_000)
        self.assertGreater(os.path.getsize(p), 5_000_000)
        evs, _ = cmac.diag_events(0, dirs=self.dirs(), now=1_790_000_100)
        self.assertEqual([e["subject"] for e in evs], ["Big"])

    def test_a_missing_directory_is_nothing(self):
        self.assertEqual(cmac.diag_events(0, dirs=(os.path.join(self.dir, "nope"), "/nonexistent/*/x"), now=1), ([], 0))

    def test_failed_daemons_are_new_failures_only(self):
        self.assertEqual(cmac.new_failed_daemons(None, ["a.b"], T0), [])  # the first look
        got = cmac.new_failed_daemons(["a.b"], ["a.b", "c.d"], T0)
        self.assertEqual([(e["kind"], e["subject"], e["source"]) for e in got], [("service_failed", "c.d", "launchd")])
        self.assertEqual(cmac.new_failed_daemons(["a.b"], [], T0), [])


# ---- the collector's history job ---------------------------------------------------------------------------------------------

class FakeProcs(object):
    def __init__(self, *rounds):
        self.rounds = list(rounds)

    def sample(self):
        return {"procs": self.rounds.pop(0) if self.rounds else [], "total": {}, "notes": []}


class FakeCpu(object):
    def __init__(self, *rounds):
        self.rounds = list(rounds)

    def sample(self):
        return self.rounds.pop(0) if self.rounds else {}


def cpu_sample(busy, temp=None, high=None, load=None, throttle=None):
    return {"usage": {"total": {"busy": busy}}, "load": load, "temps": {"package": temp, "cores": {}, "high": high},
            "throttle": {"package": throttle, "cores": {}}}


class Clock(object):
    def __init__(self, start=T0 + 30):
        self.t = float(start)

    def wall(self):
        return self.t

    def mono(self):
        return self.t - T0

    def tick(self, s=60):
        self.t += s


def P(pid, name, t, mem=1000, start=100.0):
    return {"pid": pid, "name": name, "time": t, "mem": mem, "start": start, "cpu": None}


class JobCase(TmpDir):
    def job(self, procs=None, cpu=None, clock=None):
        clock = clock or Clock()
        store = history.Store(self.path, now=clock.wall)
        self.addCleanup(store.close)
        job = collector.HistoryJob(store, procs or FakeProcs(), cpu or FakeCpu(), wall=clock.wall, mono=clock.mono)
        return job, clock, store


class JobSampling(JobCase):
    def test_minutes_become_hour_rows(self):
        procs = FakeProcs([P(1, "nginx", 10.0), P(2, "nginx", 1.0), P(3, "python3", 5.0, mem=50_000_000)],
                          [P(1, "nginx", 40.0), P(2, "nginx", 1.0), P(3, "python3", 5.5, mem=60_000_000)],
                          [P(1, "nginx", 70.0), P(3, "python3", 6.5, mem=40_000_000)])
        cpu = FakeCpu(cpu_sample(10.0, 50.0, 90.0, [0.5, 0, 0], 3), cpu_sample(30.0, 60.0, 90.0, [1.5, 0, 0], 5),
                      cpu_sample(20.0, 55.0, 90.0, [0.1, 0, 0], 5))
        with mock.patch.object(collector, "hist_meminfo", return_value={"MemTotal": 1000, "MemAvailable": 400, "SwapTotal": 100, "SwapFree": 90}):
            job, clock, store = self.job(procs, cpu)
            for _ in range(3):
                job.sample()
                clock.tick()
            job.flush()
        h = T0 // 3600
        rows = {r[0]: r[1:] for r in self.rows("SELECT app, cpu_s, rss_max, rss_avg, procs_max, samples FROM app_hour WHERE hour = ?", h)}
        self.assertEqual(rows["nginx"], (60.0, 2000, 1667, 2, 3))   # (40-10)+(1-1) then (70-40) s of CPU; rss 2000, 2000, 1000
        self.assertEqual(rows["python3"], (1.5, 60_000_000, 50_000_000, 1, 3))
        host = self.rows("SELECT cpu_avg, cpu_max, mem_avg, swap_max, temp_max, temp_high, throttle, load_max, samples FROM host_hour")
        self.assertEqual(host, [(20.0, 30.0, 60.0, 10.0, 60.0, 90.0, 2, 1.5, 3)])  # throttle: 5-3 and 5-5; the first reading has no delta
        self.assertEqual(json.loads(store.meta("last_errors")), {})
        self.assertEqual(self.rows("SELECT * FROM events"), [])  # sampling never writes events

    def test_empty_samplers_leave_no_rows_and_no_errors(self):
        with mock.patch.object(collector, "hist_meminfo", side_effect=OSError("no meminfo")), \
                mock.patch.object(os, "getloadavg", side_effect=OSError("no load")):
            job, clock, store = self.job()  # like the stubs: nothing to report
            job.sample()
            job.flush()
        self.assertEqual(self.rows("SELECT * FROM app_hour"), [])
        self.assertEqual(self.rows("SELECT * FROM host_hour"), [])
        self.assertIn("memory", json.loads(store.meta("last_errors")) if store.meta("last_errors") else {"memory": 1})

    def test_a_failing_sampler_costs_only_its_part(self):
        class Boom(object):
            def sample(self):
                raise RuntimeError("boom")
        with mock.patch.object(collector, "hist_meminfo", return_value={"MemTotal": 100, "MemAvailable": 50}):
            job, clock, store = self.job(Boom(), FakeCpu(cpu_sample(12.0)))
            job.sample()
            job.flush()
        self.assertEqual(self.rows("SELECT cpu_avg, mem_avg FROM host_hour"), [(12.0, 50.0)])
        self.assertIn("procs", job.errors)

    def test_failed_flush_keeps_the_samples(self):
        with mock.patch.object(collector, "hist_meminfo", return_value={"MemTotal": 100, "MemAvailable": 50}):
            job, clock, store = self.job(FakeProcs([P(1, "a", 1.0)]), FakeCpu())
            job.sample()
            with mock.patch.object(store, "add_hours", side_effect=sqlite3.OperationalError("disk full")):
                with self.assertRaises(sqlite3.OperationalError):
                    job.flush()
            self.assertEqual(len(job.window.snaps), 1)
            job.flush()
        self.assertEqual(len(self.rows("SELECT * FROM app_hour")), 1)

    def test_temperature_from_sensors_json_when_fresh(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(nuc_config, "RUN_DIR", d), \
                mock.patch.object(collector, "hist_meminfo", return_value={}):
            job, clock, store = self.job(FakeProcs(), FakeCpu(cpu_sample(5.0)))
            self.assertIsNone(job.host_fields(cpu_sample(5.0), clock.wall())["temp"])  # no file: absent, not an error
            with open(os.path.join(d, "sensors.json"), "w") as f:
                json.dump({"ts": clock.wall() - 5, "os": "windows", "cpu": {"package": None, "cores": {"0": 61.0, "1": 66.5}, "sensors": []}}, f)
            self.assertEqual(job.host_fields(cpu_sample(5.0), clock.wall())["temp"], 66.5)
            self.assertEqual(job.host_fields(cpu_sample(5.0, temp=40.0), clock.wall())["temp"], 40.0)
            with open(os.path.join(d, "sensors.json"), "w") as f:
                f.write("{not json")
            self.assertIsNone(job.host_fields(cpu_sample(5.0), clock.wall())["temp"])


class JobEvents(JobCase):
    """The event sources with a fake `run`: which command lines are used, what is stored, where the cursors are."""

    def fake_run(self, answers, log):
        def run(name, *args, timeout=15):
            log.append((name,) + args)
            for key, value in answers.items():
                if key(name, args):
                    return value(args) if callable(value) else value
            raise collector.Absent(name)
        return run

    def test_linux_journal_end_to_end(self):
        calls = []
        j1 = "\n".join([jl("Out of memory: Killed process 4242 (java) total-vm:1kB", 3, _TRANSPORT="kernel", ts=T0 + 10, cursor="s=a;i=1;b=b;m=1;t=1;x=1"),
                        jl("nginx.service: Failed with result 'exit-code'.", 4, ident="systemd", ts=T0 + 20, cursor="s=a;i=2;b=b;m=1;t=1;x=1"),
                        jl("disk failure on /dev/sda1 for user alice", 3, ident="smartd", ts=T0 + 30, cursor="s=a;i=3;b=b;m=1;t=1;x=1")])
        j2 = jl("worker[1]: segfault at 0 ip 0 sp 0 error 4", 6, _TRANSPORT="kernel", ts=T0 + 40, cursor="s=a;i=4;b=b;m=1;t=1;x=1")
        j3 = "\n".join([jl("Failed password for invalid user admin from 192.0.2.44 port 1 ssh2", 6, ident="sshd", ts=T0 + 50, cursor="s=a;i=5;b=b;m=1;t=1;x=1"),
                        jl("Invalid user mallory from 198.51.100.9 port 2", 6, ident="sshd", ts=T0 + 60, cursor="s=a;i=6;b=b;m=1;t=1;x=1")])

        def journal(args):
            return (0, {"-p": j1, "_TRANSPORT=kernel": j2, "-t": j3}[next(a for a in args if a in ("-p", "_TRANSPORT=kernel", "-t"))], "")
        answers = {lambda n, a: n == "journalctl": journal}
        job, clock, store = self.job()
        with mock.patch.object(collector, "run", self.fake_run(answers, calls)):
            job.src_journal_commit = None
            evs, logs, meta = job.src_journal()
            store.add_events(evs, logs, meta)
        self.assertEqual(len(calls), 3)
        for c in calls:  # fixed command lines: no shell, the first look takes the last hour
            self.assertEqual(c[0], "journalctl")
            self.assertEqual(c[1:3], ("-o", "json"))
            self.assertIn("--no-pager", c)
            self.assertIn("--since=-1h", c)
            self.assertTrue(any(a.startswith("--output-fields=") for a in c))
        self.assertEqual(sorted((e[0], e[1]) for e in self.rows("SELECT kind, subject FROM events")),
                         [("crash", "worker"), ("login_fail", "sshd"), ("oom", "java"), ("service_failed", "nginx.service")])
        self.assertEqual(self.rows("SELECT n FROM events WHERE kind = 'login_fail'"), [(2,)])
        self.assertEqual({k: v for k, v in meta.items() if k.endswith("_cursor")},
                         {"journal_cursor": "s=a;i=3;b=b;m=1;t=1;x=1", "kernel_cursor": "s=a;i=4;b=b;m=1;t=1;x=1", "sshd_cursor": "s=a;i=6;b=b;m=1;t=1;x=1"})
        text = dump_text(self.path)
        for secret in ("192.0.2.44", "198.51.100.9", "admin", "mallory", "alice"):
            self.assertNotIn(secret, text)
        logs = self.rows("SELECT unit, template FROM log_day ORDER BY unit")
        self.assertEqual(logs, [("kernel", "Out of memory: Killed process <n> (java) total-vm:<v>"),
                                ("smartd", "disk failure on <path> for user <str>")])
        # the second pass goes on from the cursors
        calls.clear()
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "journalctl": (0, "", "")}, calls)):
            evs, logs, meta = job.src_journal()
        self.assertEqual(sorted(c[-1] for c in calls), ["--after-cursor=s=a;i=3;b=b;m=1;t=1;x=1", "--after-cursor=s=a;i=4;b=b;m=1;t=1;x=1",
                                                        "--after-cursor=s=a;i=6;b=b;m=1;t=1;x=1"])
        self.assertEqual((evs, logs, meta), ([], [], {}))

    def test_a_lost_cursor_falls_back_to_the_last_time(self):
        job, clock, store = self.job()
        store.set_meta("journal_cursor", "s=gone;i=1;b=b;m=1;t=1;x=1")
        store.set_meta("journal_ts", "1790000000")
        store.set_meta("kernel_cursor", "bad cursor")  # not even a cursor: the first look
        calls = []

        def journal(args):
            if any(a.startswith("--after-cursor=") for a in args):
                return 1, "", "Failed to seek to cursor: Invalid argument"
            return 0, "", ""
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "journalctl": journal}, calls)):
            job.src_journal()
        self.assertTrue(any(c[-1] == "--since=@1790000000" for c in calls), calls)
        self.assertTrue(any(c[-1] == "--since=-1h" and "_TRANSPORT=kernel" in c for c in calls), calls)

    def test_a_journal_that_fails_is_reported_not_raised(self):
        job, clock, store = self.job()
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: True: (1, "", "No journal files were found.")}, [])):
            self.assertEqual(job.src_journal(), ([], [], {}))
        self.assertIn("journal", job.errors)

    def test_a_backlog_is_read_in_rounds(self):
        job, clock, store = self.job()
        lines = [jl("x", 3, ts=T0 + i, cursor="s=a;i=%x;b=b;m=1;t=1;x=1" % i) for i in range(1, 8)]
        calls = []

        def journal(args):
            cur = next((a for a in args if a.startswith("--after-cursor=")), None)
            start = int(cur.split("i=")[1].split(";")[0], 16) if cur else 0
            return 0, "\n".join(lines[start:]), ""
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "journalctl": journal}, calls)), \
                mock.patch.object(history, "JOURNAL_CAP", 3), mock.patch.object(history, "JOURNAL_SOURCES", history.JOURNAL_SOURCES[:1]):
            evs, logs, meta = job.src_journal()
        self.assertEqual(len(logs), 7)
        self.assertEqual(meta["journal_cursor"], "s=a;i=7;b=b;m=1;t=1;x=1")
        self.assertEqual(len(calls), 3)

    def test_docker_end_to_end(self):
        calls = []
        ids = "a" * 64 + "\n" + "b" * 64 + "\n--evil\n"
        state = {"v": "/web|0|false|0|running|2026-09-30T10:00:00Z\n/db|0|false|0|running|2026-09-30T10:00:00Z\n"}

        def docker(args):
            return (0, ids, "") if args[0] == "ps" else (0, state["v"], "")
        job, clock, store = self.job()
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "docker": docker}, calls)):
            evs, logs, meta = job.src_docker()
            self.assertEqual(evs, [])  # the baseline
            store.add_events(evs, logs, meta)
            state["v"] = "/web|3|false|0|running|2026-09-30T10:05:00Z\n/db|0|true|137|exited|2026-09-30T10:00:00Z\n"
            evs, logs, meta = job.src_docker()
            store.add_events(evs, logs, meta)
            evs, logs, meta = job.src_docker()
            self.assertEqual((evs, meta), ([], {}))  # nothing changed: nothing written
        self.assertEqual(sorted(self.rows("SELECT kind, subject, n, source FROM events")),
                         [("oom", "db", 1, "docker"), ("restart", "web", 3, "docker")])
        self.assertEqual(calls[0], ("docker", "ps", "-a", "-q", "--no-trunc"))
        self.assertEqual(calls[1][:4], ("docker", "inspect", "--format", history.DOCKER_FORMAT))
        self.assertEqual(calls[1][4:], ("a" * 64, "b" * 64))  # the id that is not an id was never passed

    def test_windows_end_to_end(self):
        calls = []
        out = json.dumps({"logs": {"System": {"max": 77, "events": [ev(41, "Microsoft-Windows-Kernel-Power", 77, 1, t=T0 + 5)]},
                                   "Application": {"max": 12, "events": [ev(1000, "Application Error", 12, 2, "app.exe", t=T0 + 6, LogName="Application")]}},
                          "errors": {}})
        job, clock, store = self.job()
        store.set_meta("eventlog_System", "70")
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "powershell": (0, out, "")}, calls)):
            evs, logs, meta = job.src_eventlog()
            store.add_events(evs, logs, meta)
        self.assertEqual(calls[0][:6], ("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"))
        import base64
        script = base64.b64decode(calls[0][6]).decode("utf-16-le")
        self.assertIn("$cur = @{ System = 70; Application = 0 }", script)
        self.assertEqual(sorted(self.rows("SELECT kind, subject FROM events")), [("crash", "app.exe"), ("unexpected_shutdown", "system")])
        self.assertEqual((store.meta("eventlog_System"), store.meta("eventlog_Application")), ("77", "12"))

    def test_macos_end_to_end(self):
        job, clock, store = self.job()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "App-1.ips")
            with open(p, "w") as f:
                f.write('{"app_name":"App","bug_type":"309"}\n')
            os.utime(p, (T0 + 5, T0 + 5))
            with mock.patch.object(cmac, "DIAG_DIRS", (d,)), mock.patch.object(cmac.diag_events, "__defaults__", (cmac.DIAG_CAP, (d,), None)):
                evs, logs, meta = job.src_diag()
        store.add_events(evs, logs, meta)
        self.assertEqual(self.rows("SELECT kind, subject, source FROM events"), [("crash", "App", "diag")])
        self.assertEqual(float(store.meta("diag_mtime")), T0 + 5)

        def launchctl(args):
            return 0, "PID\tStatus\tLabel\n-\t78\tcom.example.broken\n123\t0\tcom.example.fine\n", ""
        with mock.patch.object(collector, "run", self.fake_run({lambda n, a: n == "launchctl": launchctl}, [])), \
                mock.patch.object(cmac, "daemons", lambda run: ([], ["com.example.broken"])):
            evs, logs, meta = job.src_launchd()
            self.assertEqual(evs, [])
            store.add_events(evs, logs, meta)
            with mock.patch.object(cmac, "daemons", lambda run: ([], ["com.example.broken", "com.example.new"])):
                evs, _, _ = job.src_launchd()
        self.assertEqual([(e["kind"], e["subject"]) for e in evs], [("service_failed", "com.example.new")])

    def test_each_source_fails_alone_and_a_missing_tool_is_not_an_error(self):
        job, clock, store = self.job()

        def boom():
            raise RuntimeError("boom")

        def absent():
            raise collector.Absent("tool")
        ok = ([{"ts": T0, "kind": "crash", "subject": "a", "detail": "d", "source": "t", "n": 1}], [], {"x": "1"})
        with mock.patch.object(job, "sources", lambda: [("one", boom), ("two", absent), ("three", lambda: ok)]):
            job.events()
        self.assertEqual(self.rows("SELECT subject FROM events"), [("a",)])
        self.assertEqual(list(json.loads(store.meta("last_errors"))), ["one"])
        self.assertEqual(store.meta("x"), "1")

    def test_boot_time_is_recorded_once_per_boot(self):
        job, clock, store = self.job()
        with tempfile.TemporaryDirectory() as d:
            boot = os.path.join(d, "boot.json")
            with mock.patch.object(collector, "OUT_BOOT", boot):
                job.boot()  # no file yet
                with open(boot, "w") as f:
                    json.dump({"btime": T0 - 1000, "kernel": "6.1.0-test", "analyze": None}, f)
                job.boot()  # not finished booting: no analyze
                self.assertEqual(self.rows("SELECT * FROM boots"), [])
                with open(boot, "w") as f:
                    json.dump({"btime": T0 - 1000, "kernel": "6.1.0-test", "analyze": {"total": 31.5, "parts": {}}}, f)
                job.boot()
                job.boot()
                with open(boot, "w") as f:
                    f.write("{broken")
                job.boot()
        self.assertEqual(self.rows("SELECT boot, total_s, kernel FROM boots"), [(T0 - 1000, 31.5, "6.1.0-test")])

    def test_daily_disks_prune_and_vacuum_once_a_day(self):
        clock = Clock(T0 + 30)
        job, clock, store = self.job(clock=clock)
        store.conn.execute("INSERT INTO events(ts, kind, subject, n) VALUES (?, 'crash', 'ancient', 1)", (clock.wall() - 100 * DAY,))
        rows = [{"mount": "/", "used": 40, "total": 100}, {"mount": "/data", "used": 5, "total": 10}]
        with mock.patch.object(collector, "disk_rows", return_value=rows) as dr:
            job.daily()
            job.daily()  # the same day: nothing
            self.assertEqual(dr.call_count, 1)
            self.assertEqual(sorted(self.rows("SELECT mount, used, total FROM disk_day")), [("/", 40, 100), ("/data", 5, 10)])
            self.assertEqual(self.rows("SELECT * FROM events"), [])  # pruned
            self.assertIsNotNone(store.meta("last_vacuum"))
            clock.tick(DAY)
            job.daily()
            self.assertEqual(dr.call_count, 2)
        # a disk reading that fails does not stop the retention
        clock.tick(DAY)
        with mock.patch.object(collector, "disk_rows", side_effect=OSError("hung mount")):
            job.daily()
        self.assertIn("disks", job.errors)
        self.assertEqual(store.meta("last_daily"), str(int(clock.wall() // DAY)))

    def test_disk_rows_and_the_timeout_helper(self):
        rows = collector.disk_rows() if collector.LINUX else []
        for r in rows:
            self.assertTrue(0 <= r["used"] <= r["total"] and r["total"] > 0, r)
        self.assertEqual(collector.call_with_timeout(lambda: 5, 5), 5)
        with self.assertRaises(KeyError):
            collector.call_with_timeout(lambda: {}["x"], 5)
        gate = threading.Event()
        self.addCleanup(gate.set)
        with self.assertRaises(TimeoutError):
            collector.call_with_timeout(gate.wait, 0.2)


# ---- the thread --------------------------------------------------------------------------------------------------------------

class Loop(TmpDir):
    def run_loop(self, make, rounds):
        sleeps = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            collector.history_loop(make=make, sleep=sleeps.append, stop=lambda: len(sleeps) >= rounds, mono=lambda: 0.0)
        return sleeps, err.getvalue()

    def test_the_loop_survives_exceptions_in_every_place(self):
        steps = []

        class Job(object):
            def step(self):
                steps.append(1)
                if len(steps) in (1, 2, 4):
                    raise RuntimeError("step %d broke" % len(steps))
        made = []

        def make():
            made.append(1)
            if len(made) == 1:
                raise OSError("cannot open the database")  # the store cannot be opened at first
            return Job()
        sleeps, err = self.run_loop(make, 6)
        self.assertEqual(len(sleeps), 6)  # never died, always slept
        self.assertEqual(len(steps), 5)
        self.assertEqual(len(made), 2)  # the job is made again until it exists, then kept
        self.assertIn("cannot open the database", err)
        self.assertEqual(err.count("RuntimeError('step 1 broke')"), 1)
        self.assertTrue(all(1.0 <= s <= collector.HIST_SAMPLE_S for s in sleeps))

    def test_the_same_failure_is_printed_once(self):
        class Job(object):
            def step(self):
                raise ValueError("same")
        sleeps, err = self.run_loop(lambda: Job(), 5)
        self.assertEqual(err.count("same"), 1)
        self.assertEqual(len(sleeps), 5)

    def test_the_real_job_in_the_real_loop(self):
        db = os.path.join(self.dir, "lib", "history.db")
        gc.collect()  # what other tests left to the garbage collector warns now, not inside the stderr captured below
        with mock.patch.object(history, "PATH", db), mock.patch.object(collector.HistoryJob, "sources", lambda self: []):
            sleeps, err = self.run_loop(None, 2)
        self.assertEqual(err, "")
        self.assertTrue(os.path.exists(db))
        s = sqlite3.connect(db)
        self.assertEqual(s.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone(), ("1",))
        s.close()

    def test_a_database_that_cannot_be_written_does_not_kill_the_loop(self):
        blocker = os.path.join(self.dir, "file")
        with open(blocker, "w") as f:
            f.write("x")
        with mock.patch.object(history, "PATH", os.path.join(blocker, "history.db")):  # a directory that is a file
            sleeps, err = self.run_loop(None, 3)
        self.assertEqual(len(sleeps), 3)
        self.assertIn("history:", err)


class FeatureSwitch(TmpDir):
    """[features] health = no: no thread, no file. --once never touches the database either."""

    class Stop(Exception):
        pass

    def run_main(self, off, argv=("collector.py",)):
        started = []

        class FakeThread(object):
            def __init__(self, target=None, name=None, daemon=None, **kw):
                started.append(target)

            def start(self):
                pass
        db = os.path.join(self.dir, "lib", "history.db")
        stop = self.Stop
        with mock.patch.object(collector, "OFF", set(off)), mock.patch.object(history, "PATH", db), \
                mock.patch.object(collector, "threading", types.SimpleNamespace(Thread=FakeThread)), \
                mock.patch.object(nuc_config, "RUN_DIR", os.path.join(self.dir, "run")), \
                mock.patch.object(collector, "collect", return_value={}), mock.patch.object(collector, "write_atomic"), \
                mock.patch.object(collector, "collect_net", return_value={}), mock.patch.object(collector, "collect_boot", return_value={}), \
                mock.patch.object(collector.time, "sleep", side_effect=stop), mock.patch.object(sys, "argv", list(argv)), \
                mock.patch.object(history, "Store", side_effect=AssertionError("the database was opened")), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                collector.main()
            except stop:
                pass
        self.assertFalse(os.path.exists(db))
        self.assertFalse(os.path.exists(os.path.dirname(db)))
        return started

    def test_on_by_default(self):
        self.assertIn("health", nuc_config.FEATURES)
        self.assertIn(collector.history_loop, self.run_main(off=()))

    def test_off(self):
        self.assertNotIn(collector.history_loop, self.run_main(off=("health",)))

    def test_other_features_do_not_matter(self):
        self.assertIn(collector.history_loop, self.run_main(off=("cpu", "map", "boot", "docker_disk")))

    def test_once_does_not_touch_the_database(self):
        started = self.run_main(off=(), argv=("collector.py", "--once"))
        self.assertEqual(started, [])

    def test_the_config_switch_is_read(self):
        cfg = os.path.join(self.dir, "c.ini")
        with open(cfg, "w") as f:
            f.write("[features]\nhealth = no\n")
        self.assertFalse(nuc_config.load(cfg)["features"]["health"])
        self.assertTrue(nuc_config.load("/nonexistent")["features"]["health"])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux")
class OnLinux(JobCase):
    """The real sources of this machine: results may be empty (no journal, no Docker), a crash is never allowed."""

    def test_real_journal_and_docker_readers(self):
        job, clock, store = self.job()
        for name, fn in job.sources():
            try:
                evs, logs, meta = fn()
            except collector.Absent:
                continue
            except Exception as e:  # noqa: BLE001 - the job would record it; here it must at least be a clean one
                self.assertIsInstance(e, (RuntimeError, OSError), repr(e))
                continue
            store.add_events(evs, logs, meta)
        job.events()
        self.assertIsInstance(json.loads(store.meta("last_errors") or "{}"), dict)

    def test_real_step_runs_twice(self):
        job = collector.HistoryJob(history.Store(self.path), wall=time.time, mono=time.monotonic)
        self.addCleanup(job.store.close)
        job.step()
        job.step()
        job.flush()
        self.assertEqual(job.store.meta("schema_version"), "1")


if __name__ == "__main__":
    unittest.main()
