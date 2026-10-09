"""The advice the screens show: the shared latest-advice file (src/advisor.py) and the collector's daily digest (src/collector.py).

No model and no network beyond the fake OpenAI-compatible server of test_advisor on 127.0.0.1, no real clock (the digest thread
takes a fake one) and no real child process except one that is refused at once ([ai] off). Other operating systems are exercised
through the `plat`/`env` arguments and mocks, so everything runs on every OS.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor  # noqa: E402
import collector  # noqa: E402
import health  # noqa: E402
import history  # noqa: E402
import nuc_config  # noqa: E402
import test_advisor as ta  # noqa: E402  (the fake model server, the report and the history of that file)

HOUR = 3600
T0 = 1900000000          # a fixed clock for the digest thread


def entry(**kw):
    """A valid entry of the file, as the writer makes it."""
    e = {"text": "Free space on / [disk-full:/] and look at [oom:postgres].", "model": "tiny-model", "at": int(time.time()) - 5 * HOUR,
         "cites": ["disk-full:/", "oom:postgres"], "period": 7, "report_at": int(time.time()) - 5 * HOUR,
         "findings_ids": ["oom:postgres", "cpu-hog:chromium", "disk-full:/"]}
    e.update(kw)
    return e


def raw_file(periods=None, daily=None, v=1):
    d = {"v": v, "periods": periods if periods is not None else {"7": entry()}}
    if daily is not None:
        d["daily"] = daily
    return d


def write_raw(path, obj, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8"))
    if os.name == "posix":
        os.chmod(path, mode)


class Files(ta.Base):
    """ta.Base (fake server, config, NUC_CONSOLE_HOME = <tmp>/cache) plus the shared file, which lives in that directory."""

    def setUp(self):
        ta.Base.setUp(self)
        self.path = os.path.join(self.tmp, "cache", "advice.json")

    def stored(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


# ------------------------------------------------------------------------------------------------------------ where it lives

class Paths(unittest.TestCase):
    def test_next_to_the_history_on_every_os(self):
        for plat in ("linux", "darwin"):
            self.assertEqual(advisor.store_path(plat, {}), "/var/lib/nuc-console/advice.json", plat)
        self.assertEqual(advisor.store_path("win32", {"ProgramData": r"D:\PD"}), r"D:\PD\nuc-console\lib\advice.json")
        self.assertEqual(advisor.store_path("win32", {}), r"C:\ProgramData\nuc-console\lib\advice.json")

    def test_portable_mode_wins_everywhere(self):
        self.assertEqual(advisor.store_path("linux", {"NUC_CONSOLE_HOME": "/opt/h", "ProgramData": "X"}), "/opt/h/advice.json")
        self.assertEqual(advisor.store_path("darwin", {"NUC_CONSOLE_HOME": "/opt/h"}), "/opt/h/advice.json")
        self.assertEqual(advisor.store_path("win32", {"NUC_CONSOLE_HOME": r"E:\h", "ProgramData": "X"}), r"E:\h\advice.json")

    def test_the_default_is_the_directory_of_history_db(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("NUC_CONSOLE_HOME", None)
            self.assertEqual(advisor.store_path(), os.path.join(nuc_config.LIB_DIR, "advice.json"))
            self.assertEqual(os.path.dirname(advisor.store_path()), os.path.dirname(history.PATH))
            here = "win32" if nuc_config.WINDOWS else "darwin" if nuc_config.MACOS else "linux"
            self.assertEqual(advisor.store_path(), advisor.store_path(here, os.environ))  # the mirrored paths do not drift

    def test_who_may_write_it(self):
        with mock.patch.dict(os.environ):
            os.environ["NUC_CONSOLE_HOME"] = "/anywhere"
            self.assertTrue(advisor.store_writable())  # portable mode: the user's own directory
            os.environ.pop("NUC_CONSOLE_HOME")
            if hasattr(os, "geteuid"):
                for euid, want in ((0, True), (1000, False)):
                    with mock.patch.object(os, "geteuid", return_value=euid):
                        self.assertEqual(advisor.store_writable(), want)
                        self.assertEqual(advisor.is_admin(), want)
        with mock.patch.object(advisor.nuc_config, "WINDOWS", True), mock.patch.dict(os.environ):
            os.environ.pop("NUC_CONSOLE_HOME", None)
            for answer, want in ((1, True), (0, False)):
                fake = types.SimpleNamespace(windll=types.SimpleNamespace(shell32=types.SimpleNamespace(IsUserAnAdmin=lambda a=answer: a)))
                with mock.patch.dict(sys.modules, {"ctypes": fake}):
                    self.assertEqual(advisor.store_writable(), want)
            with mock.patch.dict(sys.modules, {"ctypes": types.SimpleNamespace()}):  # no windll: not an administrator
                self.assertFalse(advisor.is_admin())


# ------------------------------------------------------------------------------------------------------ writing and reading

class Store(Files):
    def result(self, **kw):
        r = {"text": "Look at [oom:postgres].", "model": "tiny-model", "at": int(time.time()), "cites": ["oom:postgres"]}
        r.update(kw)
        return r

    def test_roundtrip_and_shape(self):
        report = ta.make_report()
        self.assertTrue(advisor.save_shared(self.result(), report, 7))
        data = self.stored()
        self.assertEqual(set(data), {"v", "periods"})
        self.assertEqual(data["v"], 1)
        self.assertEqual(set(data["periods"]), {"7"})
        e = data["periods"]["7"]
        self.assertEqual(set(e), {"text", "model", "at", "cites", "period", "report_at", "findings_ids"})
        self.assertEqual((e["text"], e["model"], e["cites"], e["period"]), ("Look at [oom:postgres].", "tiny-model", ["oom:postgres"], 7))
        self.assertEqual(e["report_at"], report["period"]["to"])
        self.assertEqual(e["findings_ids"], ["oom:postgres", "cpu-hog:chromium", "disk-full:/"])
        self.assertEqual(advisor.read_store(), {"periods": {7: e}, "daily": None})

    def test_a_cited_id_is_always_among_the_ids(self):
        report = ta.make_report(extra=300)
        self.assertTrue(advisor.save_shared(self.result(cites=["log-noisy:u299", "oom:postgres"]), report, 7))
        e = advisor.read_store()["periods"][7]
        self.assertEqual(len(e["findings_ids"]), advisor.MAX_IDS + 1)
        self.assertEqual(e["cites"], ["log-noisy:u299", "oom:postgres"])

    def test_mode_and_no_leftovers(self):
        advisor.save_shared(self.result(), ta.make_report(), 7)
        advisor.save_shared(self.result(), ta.make_report(), 30)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)  # the readers are other users
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["advice.json"])

    def test_the_directory_is_created(self):
        deep = os.path.join(self.tmp, "a", "b", "advice.json")
        self.assertTrue(advisor.save_shared(self.result(), ta.make_report(), 1, path=deep))
        self.assertIn(1, advisor.read_store(deep)["periods"])

    def test_atomic_replace(self):
        advisor.save_shared(self.result(text="old"), ta.make_report(), 7)
        calls, real = [], os.replace

        def spy(src, dst):
            with open(dst, encoding="utf-8") as f:  # at this moment readers still get the old, complete file
                calls.append((src, dst, json.load(f)["periods"]["7"]["text"]))
            real(src, dst)
        with mock.patch.object(os, "replace", spy):
            advisor.save_shared(self.result(text="new"), ta.make_report(), 7)
        (src, dst, before), = calls
        self.assertEqual((dst, before, os.path.dirname(src)), (self.path, "old", os.path.dirname(self.path)))  # same folder: a rename
        self.assertEqual(advisor.read_store()["periods"][7]["text"], "new")

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_one(self):
        advisor.save_shared(self.result(text="old"), ta.make_report(), 7)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                advisor.save_shared(self.result(text="new"), ta.make_report(), 7)
        self.assertEqual(advisor.read_store()["periods"][7]["text"], "old")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["advice.json"])
        with mock.patch.object(json, "dump", side_effect=TypeError("bug")):
            with self.assertRaises(TypeError):
                advisor.save_shared(self.result(text="new"), ta.make_report(), 7)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["advice.json"])

    def test_windows_retries_while_a_reader_has_the_file_open(self):
        real, fails = os.replace, []

        def flaky(src, dst):
            if len(fails) < 3:
                fails.append(1)
                raise PermissionError("in use")
            real(src, dst)
        with mock.patch.object(advisor.nuc_config, "WINDOWS", True), mock.patch.object(os, "replace", flaky), \
                mock.patch.object(advisor.time, "sleep"):
            self.assertTrue(advisor.save_shared(self.result(), ta.make_report(), 7))
        self.assertEqual(len(fails), 3)
        with mock.patch.object(os, "replace", side_effect=PermissionError("denied")):  # not Windows: a real refusal
            with self.assertRaises(PermissionError):
                advisor.save_shared(self.result(), ta.make_report(), 7)

    def test_unwritable_place_is_an_oserror(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        with self.assertRaises(OSError):
            advisor.save_shared(self.result(), ta.make_report(), 7, path=os.path.join(blocker, "advice.json"))

    def test_periods_are_independent_and_a_cached_answer_never_replaces_a_newer_one(self):
        now = int(time.time())
        advisor.save_shared(self.result(text="seven", at=now), ta.make_report(), 7)
        advisor.save_shared(self.result(text="thirty", at=now - 10), ta.make_report(), 30)
        self.assertFalse(advisor.save_shared(self.result(text="older", at=now - 100), ta.make_report(), 7))
        self.assertTrue(advisor.save_shared(self.result(text="newer", at=now + 1), ta.make_report(), 7))
        got = advisor.read_store()["periods"]
        self.assertEqual({k: v["text"] for k, v in got.items()}, {7: "newer", 30: "thirty"})
        for days in (0, 3, 14, 400, "7", None):
            with self.assertRaises(ValueError):
                advisor.save_shared(self.result(), ta.make_report(), days)

    def test_the_daily_mark_and_the_entries_keep_each_other(self):
        advisor.mark_daily(T0, None)
        self.assertEqual(advisor.read_store(), {"periods": {}, "daily": {"at": T0, "ok": None}})
        advisor.save_shared(self.result(), ta.make_report(), 7)
        advisor.mark_daily(T0 + 5, True)
        got = advisor.read_store()
        self.assertEqual(got["daily"], {"at": T0 + 5, "ok": True})
        self.assertIn(7, got["periods"])

    # -- the reader trusts nothing

    def test_missing_and_damaged_files_are_empty(self):
        empty = {"periods": {}, "daily": None}
        self.assertEqual(advisor.read_store(self.path), empty)
        for junk in (b"", b"not json", b"[]", b"5", b"null", b'{"v": 2, "periods": {}}', b'{"v": true, "periods": {}}', b'{"v": "1"}',
                     b'{"v": 1, "periods": [1]}', b'{"v": 1, "periods": 5}', b"\xff\xfe\x00bad utf-8", b'{"v": 1, "periods": {"7": ',
                     b'{"v": 1.0, "periods": {}}'):
            write_raw(self.path, junk)
            got = advisor.read_store(self.path)
            self.assertEqual(got["periods"], {}, junk)
            self.assertIsNone(got["daily"], junk)

    def test_an_invalid_entry_is_dropped_alone(self):
        bad = ({"text": 5}, entry(text=""), entry(text=" \x1b[31m\x07 "), entry(model=5), entry(at="1"), entry(at=True), entry(at=0),
               entry(report_at=-1), entry(report_at=None), entry(period=30), entry(period=True), entry(cites="x"), entry(findings_ids=None),
               entry(cites=[5]), entry(findings_ids=["a", 7]), "text", None, [entry()])
        for i, e in enumerate(bad):
            write_raw(self.path, raw_file({"7": e, "30": entry(period=30)}))
            got = advisor.read_store(self.path)["periods"]
            self.assertEqual(list(got), [30], (i, e))  # the good neighbour survives

    def test_a_float_anywhere_makes_the_whole_file_foreign(self):
        write_raw(self.path, raw_file({"7": entry(at=1.5), "30": entry(period=30)}))  # this module writes integers only
        self.assertEqual(advisor.read_store(self.path)["periods"], {})

    def test_only_the_periods_of_the_screen(self):
        write_raw(self.path, raw_file({"1": entry(period=1), "3": entry(period=3), "7": entry(), "30": entry(period=30), "07": entry(),
                                       "x": entry(), "400": entry(period=400)}))
        self.assertEqual(sorted(advisor.read_store(self.path)["periods"]), [1, 7, 30])

    def test_the_text_is_cleaned_again_and_capped(self):
        evil = "\x1b[31mred\x1b[0m \x1b]0;title\x07 bell\x07 nul\x00 bidi\u202e zero\u200b <think>secret</think>ok [oom:postgres] " + "word " * 3000
        write_raw(self.path, raw_file({"7": entry(text=evil, model="m\x1b[1m" + "x" * 500)}))
        e = advisor.read_store(self.path)["periods"][7]
        for bad in ("\x1b", "\x07", "\x00", "\u202e", "\u200b", "secret", "title"):
            self.assertNotIn(bad, e["text"] + e["model"])
        self.assertLessEqual(len(e["text"]), advisor.MAX_TEXT)
        self.assertLessEqual(len(e["model"]), 80)
        self.assertTrue(e["text"].startswith("red"))

    def test_cites_must_be_among_the_findings_ids_and_ids_are_bounded(self):
        write_raw(self.path, raw_file({"7": entry(cites=["oom:postgres", "forged:id", "oom:postgres", "x" * 500],
                                                  findings_ids=["oom:postgres", "\x1b[31mcpu-hog:chromium", "i" * 500] + ["f%d" % i for i in range(999)])}))
        e = advisor.read_store(self.path)["periods"][7]
        self.assertEqual(e["cites"], ["oom:postgres"])
        self.assertEqual(len(e["findings_ids"]), advisor.MAX_IDS + advisor.MAX_CITES)
        self.assertTrue(all(len(i) <= 120 and "\x1b" not in i for i in e["findings_ids"]))

    def test_size_cap_and_numbers_and_nesting(self):
        write_raw(self.path, raw_file({"7": entry(text="x" * (advisor.STORE_MAX_BYTES + 10))}))
        self.assertEqual(advisor.read_store(self.path)["periods"], {})                # bigger than any file this module writes
        write_raw(self.path, b'{"v": 1, "periods": {"7": {"at": ' + b"9" * 250000 + b"}}}")
        self.assertEqual(advisor.read_store(self.path)["periods"], {})                # 250 000 digits: refused, not computed
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"1.5"):
            write_raw(self.path, b'{"v": 1, "periods": {}, "daily": {"at": ' + number + b"}}")
            self.assertIsNone(advisor.read_store(self.path)["daily"], number)
        write_raw(self.path, b"[" * 100000 + b"]" * 100000)                           # nesting: an error, not a RecursionError
        self.assertEqual(advisor.read_store(self.path), {"periods": {}, "daily": None})
        write_raw(self.path, raw_file({"7": entry(text="a" * 3000)}))
        self.assertEqual(len(advisor.read_store(self.path)["periods"][7]["text"]), 3000)

    def test_a_directory_or_a_fifo_is_not_read(self):
        os.makedirs(self.path)
        self.assertEqual(advisor.read_store(self.path), {"periods": {}, "daily": None})
        os.rmdir(self.path)
        if not hasattr(os, "mkfifo"):
            return
        os.mkfifo(self.path)
        box = []
        t = threading.Thread(target=lambda: box.append(advisor.read_store(self.path)), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "reading a FIFO blocked")
        self.assertEqual(box, [{"periods": {}, "daily": None}])

    def test_the_owner_and_the_mode_are_checked(self):
        st = lambda uid, mode: types.SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode)  # noqa: E731
        if hasattr(os, "geteuid"):
            self.assertTrue(advisor._owner_ok(st(0, 0o644), 1000))        # root's, read by a user
            self.assertTrue(advisor._owner_ok(st(1000, 0o600), 1000))     # the reader's own
            self.assertFalse(advisor._owner_ok(st(1001, 0o644), 1000))    # someone else's
            self.assertFalse(advisor._owner_ok(st(0, 0o666), 1000))       # writable by everybody
            self.assertFalse(advisor._owner_ok(st(0, 0o664), 1000))       # writable by a group
            write_raw(self.path, raw_file())
            self.assertEqual(list(advisor.read_store(self.path)["periods"]), [7])
            with mock.patch.object(advisor, "_owner_ok", return_value=False):
                self.assertEqual(advisor.read_store(self.path)["periods"], {})
            write_raw(self.path, raw_file(), mode=0o666)
            self.assertEqual(advisor.read_store(self.path)["periods"], {})  # writable by everybody: not trusted
        with mock.patch.object(advisor, "os", types.SimpleNamespace()):   # Windows has no owner to look at: the folder's ACL decides
            self.assertTrue(advisor._owner_ok(st(5, 0o666)))

    def test_the_daily_mark_is_validated(self):
        for d in ({"at": T0, "ok": True}, {"at": T0, "ok": False}, {"at": T0, "ok": None}, {"at": T0}):
            write_raw(self.path, raw_file(daily=d))
            self.assertEqual(advisor.read_store(self.path)["daily"], {"at": T0, "ok": d.get("ok")})
        for d in ({"at": "x", "ok": True}, {"at": 0, "ok": True}, {"at": True, "ok": True}, {"at": T0, "ok": "yes"}, {"ok": True}, [T0], 5):
            write_raw(self.path, raw_file(daily=d))
            self.assertIsNone(advisor.read_store(self.path)["daily"], d)


# ------------------------------------------------------------------------------------------------------- what a screen gets

class Shared(Files):
    def put(self, **kw):
        write_raw(self.path, raw_file({"7": entry(**kw)}))

    def test_a_fresh_entry(self):
        self.put(at=int(time.time()) - 5 * HOUR)
        res = advisor.shared_advice(ta.make_report())
        self.assertEqual(set(res), {"text", "model", "at", "cites", "shared", "period", "stale_s"})
        self.assertEqual((res["shared"], res["period"], res["model"]), (True, 7, "tiny-model"))
        self.assertLessEqual(abs(res["stale_s"] - 5 * HOUR), 5)
        self.assertEqual(res["cites"], ["disk-full:/", "oom:postgres"])
        self.assertEqual(res["text"], entry()["text"])

    def test_only_a_recent_one(self):
        now = ta.NOW
        for age, shown in ((0, True), (35 * HOUR, True), (36 * HOUR, True), (36 * HOUR + 1, False), (40 * HOUR, False), (7 * 86400, False),
                           (-60, True), (-advisor.SKEW, True), (-advisor.SKEW - 1, False), (-86400, False)):
            self.put(at=now - age)
            res = advisor.shared_advice(ta.make_report(), now=now)
            self.assertEqual(res is not None, shown, age)
            if res:
                self.assertGreaterEqual(res["stale_s"], 0)
                self.assertEqual(res["stale_s"], max(0, age))

    def test_only_for_its_own_period(self):
        self.put()
        for days in (1, 30, 3, 14):
            r = ta.make_report()
            r["period"]["days"] = days
            self.assertIsNone(advisor.shared_advice(r), days)
        write_raw(self.path, raw_file({"1": entry(period=1, text="one day"), "7": entry(text="a week")}))
        for days, text in ((1, "one day"), (7, "a week")):
            r = ta.make_report()
            r["period"]["days"] = days
            self.assertEqual(advisor.shared_advice(r)["text"], text)
        for r in (None, {}, [], "x", {"period": None}, {"period": {"days": True}}, {"period": {"days": "7"}}, {"period": {"days": 7.0}},
                  {"period": 7}, {"period": {}}):
            self.assertIsNone(advisor.shared_advice(r), r)

    def test_a_citation_of_a_finding_that_is_gone_is_dropped(self):
        self.put()
        r = ta.make_report()
        r["findings"] = [f for f in r["findings"] if f["id"] != "disk-full:/"]
        res = advisor.shared_advice(r)
        self.assertEqual(res["cites"], ["oom:postgres"])
        self.assertIn("[disk-full:/]", res["text"])               # the text is what the model wrote; only the links are filtered
        r["findings"] = []
        self.assertEqual(advisor.shared_advice(r)["cites"], [])   # no finding left: the advice stays, nothing is linked
        for findings in (None, "x", 5, [None, 5, {"id": 1}, {"id": ""}]):
            r["findings"] = findings
            self.assertEqual(advisor.shared_advice(r)["cites"], [], findings)
        r["findings"] = [{"id": "\x1b[31moom:postgres"}]            # ids are cleaned the same way when the file is written and here
        self.assertEqual(advisor.shared_advice(r)["cites"], ["oom:postgres"])

    def test_a_damaged_file_is_no_advice(self):
        for junk in (b"", b"{", b"[]"):
            write_raw(self.path, junk)
            self.assertIsNone(advisor.shared_advice(ta.make_report()))

    # -- try_advise: what the screens call

    def test_a_screen_gets_the_shared_advice_without_touching_the_model(self):
        self.put()
        res = advisor.try_advise(ta.make_report(), self.cfg, cached_only=True)
        self.assertEqual((res["shared"], res["period"], res["text"]), (True, 7, entry()["text"]))
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(advisor.try_advise(ta.make_report(extra=3), self.cfg, cached_only=True)["shared"], True)  # another report: still it

    def test_the_exact_cached_answer_comes_first(self):
        self.put()
        self.srv.queue.append(ta.completion("Exact [oom:postgres]."))
        made = advisor.advise(ta.make_report(), self.cfg)
        res = advisor.try_advise(ta.make_report(), self.cfg, cached_only=True)
        self.assertEqual(res, made)
        self.assertNotIn("shared", res)
        self.assertEqual(set(res), {"text", "model", "at", "cites"})  # the shape of today
        self.assertEqual(len(self.srv.posts()), 1)

    def test_nothing_to_show_stays_none(self):
        self.assertIsNone(advisor.try_advise(ta.make_report(), self.cfg, cached_only=True))
        self.put(at=int(time.time()) - 40 * HOUR)
        self.assertIsNone(advisor.try_advise(ta.make_report(), self.cfg, cached_only=True))
        self.put()
        self.assertIsNone(advisor.try_advise(ta.make_report(), self.config(enabled=False), cached_only=True))  # [ai] off: nothing at all
        self.assertEqual(self.srv.requests, [])

    def test_a_generation_never_serves_the_shared_one(self):
        self.put()
        self.srv.queue.append(ta.completion("Fresh [oom:postgres]."))
        res = advisor.try_advise(ta.make_report(), self.cfg)
        self.assertEqual(res["text"], "Fresh [oom:postgres].")
        self.assertNotIn("shared", res)
        self.assertEqual(len(self.srv.posts()), 1)
        self.assertEqual(advisor.try_advise(ta.make_report(), self.config(enabled=False))["busy"], False)  # an error keeps its shape

    def test_the_screen_call_does_not_raise_on_a_hostile_file(self):
        write_raw(self.path, b"[" * 100000)
        self.assertIsNone(advisor.try_advise(ta.make_report(), self.cfg, cached_only=True))
        for r in (None, {}, [], {"findings": 5, "period": {"days": 7}}):
            advisor.try_advise(r, self.cfg, cached_only=True)


class Screens(unittest.TestCase):
    RES = {"text": "Free space on / first [disk-full:/].", "model": "tiny-model", "at": ta.NOW, "cites": ["disk-full:/"],
           "shared": True, "period": 7, "stale_s": 5 * HOUR + 120}

    def test_age_in_words(self):
        for s, want in ((0, "just now"), (59, "just now"), (60, "1 min ago"), (3599, "59 min ago"), (3600, "1 h ago"), (5 * HOUR + 120, "5 h ago"),
                        (36 * HOUR, "36 h ago"), (47 * HOUR, "47 h ago"), (48 * HOUR, "2 days ago"), (-5, "just now")):
            self.assertEqual(advisor.ago(s), want, s)

    def test_lines_say_when_it_was_generated(self):
        out = advisor.lines(self.RES, 100)
        self.assertEqual(out[0], "ADVICE (AI, tiny-model, generated 5 h ago) — check before acting")
        self.assertEqual(out[-1], "cites: [disk-full:/]")
        self.assertTrue(all(len(x) <= 40 for x in advisor.lines(self.RES, 40)))
        # a result that was generated for exactly this report, or by the CLI: as before
        plain = dict(self.RES)
        for k in ("shared", "period", "stale_s"):
            del plain[k]
        self.assertEqual(advisor.lines(plain, 100)[0], "ADVICE (AI, tiny-model) — check before acting")
        self.assertEqual(advisor.lines(dict(self.RES, stale_s=None), 100)[0], "ADVICE (AI, tiny-model) — check before acting")
        self.assertEqual(advisor.lines(dict(self.RES, stale_s="5"), 100)[0], "ADVICE (AI, tiny-model) — check before acting")
        self.assertEqual(advisor.lines(dict(self.RES, stale_s=True), 100)[0], "ADVICE (AI, tiny-model) — check before acting")
        self.assertIn("generated just now", advisor.lines(dict(self.RES, stale_s=3), 100)[0])

    def test_html_says_it_too_and_stays_escaped(self):
        out = advisor.html(self.RES)
        self.assertTrue(out.startswith('<div class="advice advice-shared"><p class="advice-head">ADVICE (AI, tiny-model, generated 5 h ago)'))
        self.assertTrue(out.endswith("</div>"))
        self.assertIn("cites: [disk-full:/]", out)
        evil = dict(self.RES, text="<script>x</script>", model="<img onerror=1>", cites=["<i>"])
        got = advisor.html(evil)
        for tag in ("<script", "<img", "<i>"):
            self.assertNotIn(tag, got)
        self.assertEqual(advisor.html(dict(self.RES, stale_s=None)).count("advice-shared"), 0)
        plain = {k: v for k, v in self.RES.items() if k not in ("shared", "period", "stale_s")}
        self.assertTrue(advisor.html(plain).startswith('<div class="advice"><p class="advice-head">ADVICE (AI, tiny-model) —'))


# ----------------------------------------------------------------------------------------------------------------- the command

class Cli(Files):
    def setUp(self):
        Files.setUp(self)
        with open(os.environ["NUC_CONSOLE_CONFIG"], "w", encoding="utf-8") as f:
            f.write("[ai]\nenabled = yes\nendpoint = %s\nmodel = tiny-model\ntimeout_s = 20\n" % self.srv.url)
        nice = mock.patch.object(os, "nice", create=True)
        for p in (mock.patch.object(history, "open_ro", lambda *a, **k: ta.ro(self.db_path)),
                  mock.patch.object(health, "report", lambda conn, now=None, days=7, cores=None: ta.make_report()), nice):
            p.start()
            self.addCleanup(p.stop)
        self.nice = os.nice  # the mock: the digest asks for nice 10 and the tests must not change the priority of the test run

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = advisor.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_advise_as_root_feeds_the_screens(self):
        self.srv.queue.append(ta.completion("Check memory [oom:postgres]."))
        code, out, err = self.run_main("advise")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Check memory", out)
        e = advisor.read_store()["periods"][7]
        self.assertEqual((e["text"], e["cites"], e["model"]), ("Check memory [oom:postgres].", ["oom:postgres"], "tiny-model"))
        self.assertEqual(e["findings_ids"], ["oom:postgres", "cpu-hog:chromium", "disk-full:/"])
        res = advisor.try_advise(ta.make_report(extra=2), self.cfg, cached_only=True)  # the screen, a minute later, another report
        self.assertEqual((res["shared"], res["text"]), (True, "Check memory [oom:postgres]."))
        self.assertLessEqual(res["stale_s"], 5)
        self.nice.assert_not_called()  # a plain advise is not a background job

    def test_the_periods_of_the_screen_only(self):
        self.srv.default = ta.completion("Look [oom:postgres].")
        for days, kept in (("3", False), ("30", True), ("1", True), ("7", True), ("15", False)):
            advisor._reset_limits()
            self.assertEqual(self.run_main("advise", "--days", days)[0], 0, days)
            self.assertEqual(int(days) in advisor.read_store()["periods"], kept, days)

    def test_without_the_right_to_write_there_it_is_the_cache_as_today(self):
        self.srv.queue.append(ta.completion("Mine [oom:postgres]."))
        with mock.patch.object(advisor, "store_writable", return_value=False):
            self.assertEqual(self.run_main("advise")[0], 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "cache", "advisor-cache.json")))
        self.assertEqual(advisor.advise(ta.make_report(), self.cfg, cached_only=True)["text"], "Mine [oom:postgres].")

    def test_a_cached_answer_does_not_replace_a_newer_advice(self):
        self.srv.queue.append(ta.completion("From the cache [oom:postgres]."))
        self.assertEqual(self.run_main("advise")[0], 0)
        newer = int(time.time()) + 100
        advisor.save_shared({"text": "Newer", "model": "m", "at": newer, "cites": []}, ta.make_report(), 7)
        self.assertEqual(self.run_main("advise")[0], 0)  # served from the cache: older than what the file holds
        self.assertEqual(advisor.read_store()["periods"][7]["text"], "Newer")

    def test_store_always_asks_the_model_and_runs_at_low_priority(self):
        self.srv.default = ta.completion("Digest [oom:postgres].")
        code, out, err = self.run_main("advise", "--store", "--period", "7")
        self.assertEqual((code, err), (0, ""))
        self.nice.assert_called_once_with(10)
        advisor._reset_limits()
        self.assertEqual(self.run_main("advise", "--period", "7", "--store")[0], 0)  # the same report: the cache is not used
        self.assertEqual(len(self.srv.posts()), 2)
        self.assertEqual(self.run_main("advise")[0], 0)  # and an ordinary call is served by the cache the digest filled
        self.assertEqual(len(self.srv.posts()), 2)
        self.assertEqual(advisor.read_store()["periods"][7]["text"], "Digest [oom:postgres].")

    def test_store_needs_the_right_to_write_there(self):
        with mock.patch.object(advisor, "store_writable", return_value=False):
            code, out, err = self.run_main("advise", "--store")
        self.assertEqual(code, 1)
        self.assertIn("run it as root", err)
        self.assertIn(advisor.store_path(), err)
        self.assertEqual(self.srv.requests, [])  # refused before the model is asked

    def test_store_arguments(self):
        for argv in (("--store", "--period", "3"), ("--store", "--days", "30", "--period", "7"), ("--store", "--store"), ("--period",),
                     ("--period", "x"), ("--period", "0"), ("--period", "31"), ("--store", "now"), ("--days", "7", "--days", "7"),
                     ("--period", "٣"), ("--store", "--days", "99")):
            code, out, err = self.run_main("advise", *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage: advisor.py", out)
        self.assertEqual(self.srv.requests, [])
        self.srv.default = ta.completion("ok [oom:postgres]")
        for argv in (("--store", "--period", "30"), ("--period", "1", "--store"), ("--days", "7"), ("--period", "7")):
            advisor._reset_limits()
            self.assertEqual(self.run_main("advise", *argv)[0], 0, argv)

    def test_a_failed_write_is_the_answer_of_store_and_a_warning_otherwise(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        self.srv.default = ta.completion("ok [oom:postgres]")
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": os.path.join(blocker, "sub")}):
            code, out, err = self.run_main("advise", "--store")
            self.assertEqual(code, 1)
            self.assertIn("cannot write the shared advice", err)
            advisor._reset_limits()
            code, out, err = self.run_main("advise")
            self.assertEqual(code, 0)
            self.assertIn("ok [oom:postgres]", out)
            self.assertIn("cannot write the shared advice", err)

    def test_the_other_failures_keep_their_exit_codes_and_store_nothing(self):
        with open(os.environ["NUC_CONSOLE_CONFIG"], "w", encoding="utf-8") as f:
            f.write("[ai]\nenabled = no\n")
        self.assertEqual(self.run_main("advise", "--store")[0], 3)
        with open(os.environ["NUC_CONSOLE_CONFIG"], "w", encoding="utf-8") as f:
            f.write("[ai]\nenabled = yes\nendpoint = http://127.0.0.1:%d/v1\nmodel = tiny-model\n" % ta.free_port())
        self.assertEqual(self.run_main("advise", "--store")[0], 1)
        with mock.patch.object(history, "open_ro", lambda *a, **k: None):
            self.assertEqual(self.run_main("advise", "--store")[0], 4)
        self.assertFalse(os.path.exists(self.path))


# ------------------------------------------------------------------------------------------------------ the collector's side

class Quiet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": os.path.join(self.tmp, "state"),
                                           "NUC_CONSOLE_CONFIG": os.path.join(self.tmp, "absent.ini"), "PYTHONDONTWRITEBYTECODE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.path = os.path.join(self.tmp, "state", "advice.json")


def cfg(enabled=True, daily=True, timeout_s=120, **kw):
    d = {"enabled": enabled, "daily": daily, "timeout_s": timeout_s, "endpoint": "http://127.0.0.1:1/v1", "model": "tiny-model"}
    d.update(kw)
    return {"ai": d}


class Switch(Quiet):
    def loops(self, config, off=()):
        with mock.patch.object(collector, "CFG", config), mock.patch.object(collector, "OFF", set(off)):
            return collector.loops()

    def test_on_only_when_asked_for(self):
        self.assertIn(collector.ai_daily_loop, self.loops(cfg()))
        self.assertNotIn(collector.ai_daily_loop, self.loops(cfg(daily=False)))
        self.assertNotIn(collector.ai_daily_loop, self.loops(cfg(enabled=False)))
        self.assertNotIn(collector.ai_daily_loop, self.loops(cfg(enabled=False, daily=False)))
        self.assertNotIn(collector.ai_daily_loop, self.loops({}))
        self.assertNotIn(collector.ai_daily_loop, self.loops({"ai": None}))

    def test_the_history_it_reads_must_be_on(self):
        self.assertNotIn(collector.ai_daily_loop, self.loops(cfg(), off=("health",)))
        self.assertIn(collector.ai_daily_loop, self.loops(cfg(), off=("cpu", "map", "boot", "docker_disk")))
        self.assertIn(collector.history_loop, self.loops(cfg()))  # next to the history thread, not instead of it

    def test_off_by_default(self):
        default = nuc_config.load(os.path.join(self.tmp, "absent.ini"))
        self.assertEqual((default["ai"]["enabled"], default["ai"]["daily"]), (False, False))
        self.assertNotIn(collector.ai_daily_loop, self.loops(default))
        ini = os.path.join(self.tmp, "c.ini")
        with open(ini, "w") as f:
            f.write("[ai]\nenabled = yes\ndaily = yes\n")
        self.assertIn(collector.ai_daily_loop, self.loops(nuc_config.load(ini)))

    def run_main(self, config, argv):
        started = []

        class FakeThread(object):
            def __init__(self, target=None, **kw):
                started.append(target)

            def start(self):
                pass

        class Stop(Exception):
            pass

        def stop(*a):
            raise Stop()
        with mock.patch.object(collector, "CFG", config), mock.patch.object(collector, "OFF", set()), \
                mock.patch.object(collector, "threading", types.SimpleNamespace(Thread=FakeThread)), \
                mock.patch.object(nuc_config, "RUN_DIR", os.path.join(self.tmp, "run")), \
                mock.patch.object(collector, "collect", return_value={}), mock.patch.object(collector, "write_atomic"), \
                mock.patch.object(collector, "collect_net", return_value={}), mock.patch.object(collector, "collect_boot", return_value={}), \
                mock.patch.object(collector, "collect_sensors", return_value={}), \
                mock.patch.object(collector.time, "sleep", side_effect=stop), mock.patch.object(sys, "argv", list(argv)), \
                mock.patch.object(history, "Store", side_effect=AssertionError("the database was opened")), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                collector.main()
            except Stop:
                pass
        return started

    def test_main_starts_its_own_thread(self):
        started = self.run_main(cfg(), ("collector.py",))
        self.assertEqual(started.count(collector.ai_daily_loop), 1)
        self.assertEqual(started.count(collector.net_loop), 1)  # a thread of its own: the others are not waiting for it
        self.assertNotIn(collector.ai_daily_loop, self.run_main(cfg(daily=False), ("collector.py",)))

    def test_once_never_runs_it(self):
        self.assertEqual(self.run_main(cfg(), ("collector.py", "--once")), [])
        with mock.patch.object(collector, "ai_digest", side_effect=AssertionError("ran")), \
                mock.patch.object(collector, "collect", return_value={}), mock.patch.object(collector, "collect_net", return_value={}), \
                mock.patch.object(collector, "collect_boot", return_value={}), mock.patch.object(collector, "CFG", cfg()):
            collector.once()


class Clock(object):
    """A clock that only moves when the thread sleeps."""

    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def wall(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Daily(Quiet):
    def loop(self, clock, runs, outcomes=((True, "stored"),), until=None, **kw):
        """Runs the thread until `until` runs happened; every run is recorded with the time it began."""
        outs = iter(outcomes)
        last = [outcomes[-1]]

        def run(config):
            runs.append(clock.t)
            try:
                last[0] = next(outs)
            except StopIteration:
                pass
            if isinstance(last[0], BaseException):
                raise last[0]
            return last[0]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            collector.ai_daily_loop(cfg(), sleep=clock.sleep, wall=clock.wall, run=run, stop=lambda: len(runs) >= (until or 1), **kw)
        return out.getvalue(), err.getvalue()

    def test_the_first_digest_comes_after_ten_minutes_then_every_day(self):
        clock, runs = Clock(), []
        self.loop(clock, runs, until=3)
        self.assertEqual(runs, [T0 + 600, T0 + 600 + 86400, T0 + 600 + 2 * 86400])
        self.assertEqual(clock.slept[0], 600)
        self.assertTrue(all(1 <= s <= 3600 for s in clock.slept))  # it looks at the clock at least hourly: a step or an edit is seen

    def test_the_mark_is_kept_in_the_shared_file(self):
        clock, runs = Clock(), []
        out, err = self.loop(clock, runs)
        self.assertEqual(advisor.read_store()["daily"], {"at": T0 + 600, "ok": True})
        self.assertIn("ai digest: stored", out)
        self.assertEqual(err, "")

    def test_the_mark_is_written_before_the_run_so_a_crash_does_not_repeat_it(self):
        clock, runs, seen = Clock(), [], []

        def run(config):
            seen.append(advisor.read_store()["daily"])
            return True, "ok"
        with contextlib.redirect_stdout(io.StringIO()):
            collector.ai_daily_loop(cfg(), sleep=clock.sleep, wall=clock.wall, run=run, stop=lambda: bool(seen))
        self.assertEqual(seen, [{"at": T0 + 600, "ok": None}])

    def test_a_restart_does_not_run_it_again(self):
        advisor.mark_daily(T0 - HOUR, True)                       # the previous collector ran it an hour ago
        clock, runs = Clock(), []
        self.loop(clock, runs)
        self.assertEqual(runs, [T0 - HOUR + 86400])               # 23 hours from now, not 10 minutes
        for at, ok in ((T0 - 2 * HOUR, False), (T0 - 100, None)):  # a failed or an interrupted one: the next day as well
            advisor.mark_daily(at, ok)
            clock, runs = Clock(), []
            self.loop(clock, runs)
            self.assertEqual(runs, [at + 86400], at)

    def test_an_old_mark_means_ten_minutes_after_the_start(self):
        for at in (T0 - 86400, T0 - 30 * HOUR, T0 - 400 * 86400):
            advisor.mark_daily(at, True)
            clock, runs = Clock(), []
            self.loop(clock, runs)
            self.assertEqual(runs, [T0 + 600], at)

    def test_a_mark_from_the_future_counts_for_nothing(self):
        advisor.mark_daily(T0 + 10 * 86400, True)                 # the clock was wrong when it was written
        clock, runs = Clock(), []
        self.loop(clock, runs)
        self.assertEqual(runs, [T0 + 600])
        with open(self.path, "w") as f:                           # a damaged file: as if there were none
            f.write("{")
        clock, runs = Clock(), []
        self.loop(clock, runs)
        self.assertEqual(runs, [T0 + 600])

    def test_the_clock_going_back_does_not_stop_it_for_good(self):
        clock, runs = Clock(), []
        real, jumped = clock.sleep, []

        def sleep(s):
            real(s)
            if runs and not jumped:                               # after the first digest: a year back
                jumped.append(1)
                clock.t -= 365 * 86400
        clock.sleep = sleep
        self.loop(clock, runs, until=2)
        self.assertEqual(len(runs), 2)
        self.assertGreaterEqual(runs[1] - (runs[0] - 365 * 86400), 86400)  # a whole day after the step, not hours
        self.assertLess(runs[1] - (runs[0] - 365 * 86400), 86400 + 7200)

    def test_a_failure_is_printed_once_and_retried_the_next_day(self):
        clock, runs = Clock(), []
        out, err = self.loop(clock, runs, outcomes=((False, "exit 1: no server on 127.0.0.1:11434"), RuntimeError("boom"), (True, "stored")),
                             until=3)
        self.assertEqual(runs, [T0 + 600, T0 + 600 + 86400, T0 + 600 + 2 * 86400])  # never sooner than a day
        self.assertEqual(err.count("ai digest failed:"), 2)
        self.assertEqual(err.count("no server on 127.0.0.1:11434"), 1)
        self.assertEqual(err.count("boom"), 1)
        self.assertEqual(out.count("ai digest:"), 1)
        self.assertEqual(advisor.read_store()["daily"], {"at": T0 + 600 + 2 * 86400, "ok": True})
        clock, runs = Clock(), []
        self.loop(clock, runs, outcomes=((False, "x"),))
        self.assertEqual(advisor.read_store()["daily"]["ok"], False)

    def test_it_runs_once_a_day_even_when_the_schedule_cannot_be_kept(self):
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        clock, runs = Clock(), []
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": os.path.join(blocker, "sub")}):
            out, err = self.loop(clock, runs, until=3)
        self.assertEqual(runs, [T0 + 600, T0 + 600 + 86400, T0 + 600 + 2 * 86400])  # no spin, no stop
        self.assertEqual(err.count("cannot keep the schedule"), 1)                  # said once

    def test_stopped_before_the_time_it_runs_nothing(self):
        clock, runs = Clock(), []
        collector.ai_daily_loop(cfg(), sleep=clock.sleep, wall=clock.wall, run=lambda c: runs.append(1), stop=lambda: True)
        self.assertEqual((runs, clock.slept), ([], []))
        self.assertFalse(os.path.exists(self.path))

    def test_it_keeps_the_advice_the_digest_wrote(self):
        advisor.save_shared({"text": "kept [oom:postgres]", "model": "m", "at": int(time.time()), "cites": ["oom:postgres"]}, ta.make_report(), 7)
        clock, runs = Clock(), []
        self.loop(clock, runs)
        got = advisor.read_store()
        self.assertEqual(got["periods"][7]["text"], "kept [oom:postgres]")
        self.assertEqual(got["daily"]["ok"], True)


class Child(Quiet):
    """ai_digest(): the child process of the digest."""

    def call(self, result=None, exc=None, config=None, **kw):
        seen = {}

        def run(argv, **k):
            seen["argv"], seen["kw"] = argv, k
            if exc:
                raise exc
            return result
        with mock.patch.object(collector, "WINDOWS", kw.get("windows", False)):
            out = collector.ai_digest(config or cfg(), run=run)
        return out, seen

    def done(self, rc, out=""):
        return subprocess.CompletedProcess([], rc, stdout=out)

    def test_a_fixed_command_line_and_a_time_limit(self):
        (ok, msg), seen = self.call(self.done(0))
        self.assertTrue(ok)
        self.assertEqual(seen["argv"], [sys.executable, os.path.join(os.path.abspath(SRC), "advisor.py"), "advise", "--store", "--period", "7"])
        self.assertEqual(seen["argv"], collector.ai_digest_argv())
        k = seen["kw"]
        self.assertEqual(k["timeout"], 120 + collector.AI_MARGIN_S)           # [ai] timeout_s plus a margin
        self.assertEqual((k["stdin"], k["stdout"], k["stderr"]), (subprocess.DEVNULL, subprocess.PIPE, subprocess.STDOUT))
        for forbidden in ("shell", "preexec_fn", "env", "cwd"):               # no shell; preexec_fn is unsafe with threads; the child lowers its own nice
            self.assertNotIn(forbidden, k)
        self.assertNotIn("creationflags", k)

    def test_the_limit_follows_the_config_within_bounds(self):
        for timeout_s, want in ((10, 10), (300, 300), (600, 600), (5, 10), (9999, 600), ("45", 45), (None, 120), ("x", 120)):
            _, seen = self.call(self.done(0), config=cfg(timeout_s=timeout_s))
            self.assertEqual(seen["kw"]["timeout"], want + collector.AI_MARGIN_S, timeout_s)

    def test_windows_starts_it_below_normal_without_a_window(self):
        _, seen = self.call(self.done(0), windows=True)
        self.assertEqual(seen["kw"]["creationflags"], 0x4000 | 0x08000000)

    def test_results(self):
        self.assertEqual(self.call(self.done(0, "ADVICE\nlong text\n"))[0], (True, "advice for the last 7 days stored"))
        ok, msg = self.call(self.done(1, "\nnuc-console-ask: no server on 127.0.0.1:11434: start Ollama\n\n"))[0]
        self.assertEqual((ok, msg), (False, "exit 1: nuc-console-ask: no server on 127.0.0.1:11434: start Ollama"))
        self.assertEqual(self.call(self.done(3, ""))[0], (False, "exit 3"))
        self.assertEqual(self.call(self.done(2, None))[0], (False, "exit 2"))
        msg = self.call(self.done(1, "caf\u00e9 \x1b[31mred\x07 " + "x" * 500))[0][1]
        self.assertTrue(all(0x20 <= ord(c) < 0x7f for c in msg), msg)         # printable: it goes to a log
        self.assertLessEqual(len(msg), 230)

    def test_a_child_that_does_not_end_or_does_not_start(self):
        ok, msg = self.call(exc=subprocess.TimeoutExpired("x", 240))[0]
        self.assertEqual((ok, msg), (False, "no answer within 240 s: stopped"))
        for exc in (FileNotFoundError("no python"), PermissionError("denied"), OSError("fork failed")):
            ok, msg = self.call(exc=exc)[0]
            self.assertFalse(ok)
            self.assertIn("cannot run the advisor", msg)

    def test_the_real_child_is_refused_at_once_when_the_ai_is_off(self):
        # a real process with the real command line: it must be understood (not "usage"), and [ai] is off here
        ok, msg = collector.ai_digest(cfg(timeout_s=10))
        self.assertFalse(ok)
        self.assertIn("exit 3", msg)
        self.assertIn("enabled = no", msg)
        self.assertFalse(os.path.exists(self.path))


class EndToEnd(ta.Base):
    """The thread, the child's command line and the screens' call: the model is the fake server, the child runs in this process."""

    def test_a_day_of_the_collector_and_the_screen_that_reads_it(self):
        with open(os.environ["NUC_CONSOLE_CONFIG"], "w", encoding="utf-8") as f:
            f.write("[ai]\nenabled = yes\ndaily = yes\nendpoint = %s\nmodel = tiny-model\ntimeout_s = 20\n" % self.srv.url)
        config = nuc_config.load()
        self.assertTrue(collector.ai_daily_on(config, off=()))
        self.assertFalse(collector.ai_daily_on(config, off=("health",)))
        self.srv.queue.append(ta.completion("Fix the OOM first [oom:postgres].\nThen [disk-full:/]."))
        child = []

        def run(argv, **kw):  # the child process of the digest, here in-process
            child.append((argv, kw))
            code = advisor.main(argv[2:])
            return subprocess.CompletedProcess(argv, code, stdout="")
        patches = (mock.patch.object(history, "open_ro", lambda *a, **k: ta.ro(self.db_path)),
                   mock.patch.object(health, "report", lambda conn, now=None, days=7, cores=None: ta.make_report()),
                   mock.patch.object(os, "nice", create=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        clock = Clock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            collector.ai_daily_loop(config, sleep=clock.sleep, wall=clock.wall, run=lambda c: collector.ai_digest(c, run=run),
                                    stop=lambda: len(child) >= 1)
        self.assertEqual(len(child), 1)
        self.assertEqual(len(self.srv.posts()), 1)
        self.assertIn("ai digest: advice for the last 7 days stored", out.getvalue())
        # the screen: unprivileged, another report a minute later (one more finding), no way to the model
        screen = ta.make_report(extra=1)
        screen["findings"] = [f for f in screen["findings"] if f["id"] != "disk-full:/"] + [{"id": "new:one", "level": "info", "title": "n"}]
        res = advisor.try_advise(screen, config, cached_only=True)
        self.assertEqual(res["text"], "Fix the OOM first [oom:postgres].\nThen [disk-full:/].")
        self.assertEqual(res["cites"], ["oom:postgres"])                                    # [disk-full:/] is gone: not linked
        self.assertEqual(advisor.lines(res, 100)[0], "ADVICE (AI, tiny-model, generated just now) — check before acting")
        self.assertEqual(len(self.srv.posts()), 1)
        # the next day: a second digest, a new answer replaces the old one
        self.srv.queue.append(ta.completion("A day later [oom:postgres]."))
        child.clear()
        advisor._reset_limits()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            collector.ai_daily_loop(config, sleep=clock.sleep, wall=clock.wall, run=lambda c: collector.ai_digest(c, run=run),
                                    stop=lambda: len(child) >= 1)
        self.assertEqual(advisor.read_store()["periods"][7]["text"], "A day later [oom:postgres].")


if __name__ == "__main__":
    unittest.main()
