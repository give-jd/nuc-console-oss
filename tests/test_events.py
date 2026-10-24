"""The EVENTS timeline: the recording of the problems' lifecycle (history.problem_events, collector.HistoryJob.problems), the request
(events.parse_query), the data (events.load on a real history.db in a temporary folder), the CSV (injection), the console screen at
80x24 and 120x33 (keys, filters, details, no history, history off, empty), and the web: the page, /api/v1/events (strict parameters, ETag,
token), the downloads and the style sheet's phone rules.

Hermetic: a temporary history.db, the demo's events (src/demo.py), a real demo server on a loopback port; nothing of the host is read.
"""
import os
import re
import socket
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import collector  # noqa: E402
import demo  # noqa: E402
import events  # noqa: E402
import history  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import webcss  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_web import TOKEN, get_any as get, raw, serve  # noqa: E402

NOW = 1_790_000_000
LINE = "\x1b[K\r\n"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_SAVED = {}


def setUpModule():  # the demo screens and servers (render.demo_defaults) rename the host and declare [webapps] and [expose] for good: put them back
    _SAVED.update(host=socket.gethostname, webapps=render.CFG["webapps"], expose=render.CFG["expose"], demo=render.DEMO)


def tearDownModule():
    socket.gethostname, render.CFG["webapps"], render.CFG["expose"], render.DEMO = _SAVED["host"], _SAVED["webapps"], _SAVED["expose"], _SAVED["demo"]


def stop(srv):
    srv.shutdown()
    srv.server_close()


def ev(ts, kind="crash", subject="nginx", detail="segmentation fault", n=1):
    return {"ts": ts, "kind": kind, "subject": subject, "detail": detail, "source": "journal", "n": n}


class Recording(unittest.TestCase):
    """history.problem_events: opened, cleared, the baseline, the de-bounce and the little it keeps."""

    def test_the_first_look_is_only_the_baseline(self):
        self.assertEqual(history.problem_events(None, {"thermal": "warning"}, NOW), [])

    def test_a_problem_that_appears_and_one_that_clears(self):
        got = history.problem_events({"thermal": "warning"}, {"port-new": "port-change"}, NOW)
        self.assertEqual([(e["kind"], e["subject"], e["detail"], e["ts"]) for e in got],
                         [("problem", "port-new", "opened (port-change)", NOW), ("problem", "thermal", "cleared (warning)", NOW)])
        self.assertEqual(history.problem_events({"thermal": "warning"}, {"thermal": "warning"}, NOW), [])  # no change, no event

    def test_flapping_inside_a_minute_is_one_event_per_state(self):
        recent = {}
        out = []
        for t, (prev, cur) in enumerate([({}, {"x-y": "error"}), ({"x-y": "error"}, {}), ({}, {"x-y": "error"}), ({"x-y": "error"}, {}),
                                         ({}, {"x-y": "error"})]):
            out += history.problem_events(prev, cur, NOW + t * 10, recent)
        self.assertEqual([e["detail"] for e in out], ["opened (error)", "cleared (error)"])      # the repeats within 60 s collapse
        later = history.problem_events({}, {"x-y": "error"}, NOW + 70, recent)                      # the window is over
        self.assertEqual([e["detail"] for e in later], ["opened (error)"])

    def test_the_debounce_does_not_grow(self):
        recent = {}
        history.problem_events({}, {"a-b": "error"}, NOW, recent)
        history.problem_events({}, {}, NOW + 10 * history.PROBLEM_DEBOUNCE_S + 1, recent)
        self.assertEqual(recent, {})

    def test_only_an_id_and_a_severity_are_kept(self):
        got = history.problem_events({}, {"ok-id": "error", "has space": "error", "192.0.2.1:22": "error", "x" * 80: "error"}, NOW)
        self.assertEqual([e["subject"] for e in got], ["ok-id"])                                   # an id that is not one is not recorded
        got = history.problem_events({}, {"ok-id": "bad\x1b[2Jsev"}, NOW)
        self.assertNotIn("\x1b", got[0]["detail"])

    def test_the_kind_is_known_to_the_store(self):
        self.assertIn("problem", history.KINDS)
        self.assertIn("problem", events.KINDS)


class Job(unittest.TestCase):
    """collector.HistoryJob.problems: the minute step, with a fake clock and a fake reader of the problems."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.db")
        self.now, self.mono_t, self.cur = NOW, 1000.0, {}
        self.store = history.Store(self.path, now=lambda: self.now)
        self.job = collector.HistoryJob(self.store, wall=lambda: self.now, mono=lambda: self.mono_t, read_problems=lambda: self.cur)
        self.job.t_start = 0.0                                                                     # the grace period is over

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def rows(self):
        c = history.open_ro(self.path)
        try:
            return c.execute("SELECT ts, kind, subject, detail, source FROM events ORDER BY id").fetchall()
        finally:
            c.close()

    def minute(self, cur, secs=60):
        self.now += secs
        self.cur = cur
        self.job.problems()

    def test_lifecycle_across_minutes_and_a_restart(self):
        self.minute({"thermal": "warning"})                         # first look: baseline only
        self.assertEqual(self.rows(), [])
        self.minute({"thermal": "warning", "port-new": "port-change"})
        self.minute({"port-new": "port-change"})
        self.assertEqual([r[1:4] for r in self.rows()],
                         [("problem", "port-new", "opened (port-change)"), ("problem", "thermal", "cleared (warning)")])
        job2 = collector.HistoryJob(self.store, wall=lambda: self.now, mono=lambda: self.mono_t, read_problems=lambda: {})
        job2.t_start = 0.0
        self.now += 60
        job2.problems()                                              # a new process compares with what meta says was open
        self.assertEqual(self.rows()[-1][1:4], ("problem", "port-new", "cleared (port-change)"))
        self.assertEqual(len(self.rows()), 3)

    def test_the_grace_period_after_start(self):
        self.job.t_start = self.mono_t - 10
        self.minute({"thermal": "warning"})
        self.assertIsNone(history.json_meta(self.store, "problems_open"))                          # nothing was even read

    def test_a_failing_reader_is_an_error_note_not_an_exception(self):
        def boom():
            raise RuntimeError("no state")
        self.job.read_problems = boom
        self.job.problems()
        self.assertIn("problems", self.job.errors)

    def test_no_text_of_a_problem_reaches_the_database(self):
        self.minute({})
        self.minute({"port-new": "port-change"})
        blob = json.dumps(self.rows())
        self.assertNotRegex(blob, r"\d+\.\d+\.\d+\.\d+")
        self.assertEqual(set(r[4] for r in self.rows()), {"problems"})


class Query(unittest.TestCase):
    def q(self, **kw):
        return events.parse_query({k: v if isinstance(v, list) else [v] for k, v in kw.items()})

    def test_defaults(self):
        self.assertEqual(self.q(), {"limit": events.LIMIT_DEFAULT, "since": 0, "kind": "", "age": "24h"})
        self.assertEqual(self.q(since="0")["age"], "")                                            # since=0: everything kept

    def test_valid_values(self):
        got = self.q(limit="500", since="1790000000", kind="oom")
        self.assertEqual((got["limit"], got["since"], got["kind"]), (500, 1790000000, "oom"))
        self.assertEqual(self.q(age="1h", limit="1")["age"], "1h")

    def test_malformed_values_are_refused(self):
        for bad in ({"limit": "0"}, {"limit": "501"}, {"limit": "-1"}, {"limit": "abc"}, {"limit": ""}, {"limit": "1e2"}, {"limit": "٣"},
                    {"limit": "10000"}, {"since": "-5"}, {"since": "x"}, {"since": ""}, {"since": "1.5"}, {"since": "99999999999"},
                    {"age": "2h"}, {"age": ""}, {"age": "24H"}, {"kind": "nope"}, {"kind": ""}, {"kind": "crash;DROP"},
                    {"kind": ["crash", "oom"]}, {"limit": ["1", "2"]}, {"age": "1h", "since": "5"}):
            with self.assertRaises(events.QueryError, msg=bad):
                self.q(**bad)

    def test_the_message_never_echoes_the_value(self):
        try:
            self.q(kind="<script>alert(1)</script>")
        except events.QueryError as e:
            self.assertNotIn("script", str(e))


class Data(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.db")
        self.store = history.Store(self.path, now=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def p(self, **kw):
        return dict(events.parse_query({k: [v] for k, v in kw.items()}))

    def test_newest_first_filters_and_the_cap(self):
        self.store.add_events([ev(NOW - 100 * i, "crash" if i % 2 else "oom", "app%d" % i) for i in range(1, 8)])
        got = events.load(self.p(), NOW, path=self.path)
        self.assertEqual((got["state"], got["total"], [r["subject"] for r in got["rows"]][:3]), ("ok", 7, ["app1", "app2", "app3"]))
        got = events.load(self.p(kind="oom", limit="2"), NOW, path=self.path)
        self.assertEqual(([r["subject"] for r in got["rows"]], got["total"]), (["app2", "app4"], 3))
        got = events.load(self.p(since=str(NOW - 250)), NOW, path=self.path)
        self.assertEqual(len(got["rows"]), 2)
        self.assertEqual(len(events.load(self.p(age="1h"), NOW, path=self.path)["rows"]), 7)

    def test_age_is_taken_from_the_clock(self):
        self.store.add_events([ev(NOW - 30 * 60, subject="recent"), ev(NOW - 2 * 3600, subject="older")])
        self.assertEqual([r["subject"] for r in events.load(self.p(age="1h"), NOW, path=self.path)["rows"]], ["recent"])
        self.assertEqual(len(events.load(self.p(age="24h"), NOW, path=self.path)["rows"]), 2)

    def test_a_row_is_clean_and_complete(self):
        self.store.add_events([ev(NOW - 5, "problem", "port-new", "opened (port-change)", 2)])
        r = events.load(self.p(), NOW, path=self.path)["rows"][0]
        self.assertEqual((r["kind"], r["subject"], r["detail"], r["n"], r["ts"]), ("problem", "port-new", "opened (port-change)", 2, NOW - 5))

    def test_history_off_none_empty_and_unreadable(self):
        off = events.load(self.p(), NOW, enabled=False, path=self.path)
        self.assertEqual((off["state"], off["rows"], off["msg"]), ("off", [], events.OFF))
        empty = events.load(self.p(), NOW, path=self.path)
        self.assertEqual((empty["state"], empty["rows"], empty["total"]), ("ok", [], 0))          # a history with no events: ok, and says so
        none = events.load(self.p(), NOW, path=os.path.join(self.tmp.name, "absent.db"))
        self.assertEqual((none["state"], none["msg"]), ("none", events.NO_HISTORY))
        bad = os.path.join(self.tmp.name, "bad.db")
        c = sqlite3.connect(bad)
        c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        c.commit()
        c.close()
        err = events.load(self.p(), NOW, path=bad)                                                # no events table
        self.assertEqual(err["state"], "error")
        self.assertIn("could not be read", err["msg"])


class Csv(unittest.TestCase):
    def test_formula_cells_are_quoted_and_the_rest_is_untouched(self):
        rows = [dict(id=1, ts=NOW, kind="crash", subject=s, detail=d, source="journal", n=3)
                for s, d in (("=HYPERLINK(\"x\")", "+1"), ("-2", "@SUM(A1)"), ("\t=1", "\r=2"), ("plain", "a,b \"q\""))]
        out = list(csv.reader(io.StringIO(events.csv_text(rows))))
        self.assertEqual(out[0], ["time_utc", "ts", "kind", "subject", "detail", "count", "source"])
        self.assertEqual([r[3:5] for r in out[1:]], [["'=HYPERLINK(\"x\")", "'+1"], ["'-2", "'@SUM(A1)"], ["?=1", "?=2"], ["plain", 'a,b "q"']])  # ui.safe turns a tab into ?
        self.assertEqual(out[1][1], str(NOW))                                                     # numbers are numbers
        self.assertTrue(out[1][0].startswith("2026-09-"))

    def test_the_cell_helper(self):
        for s in ("=a", "+a", "-a", "@a"):
            self.assertEqual(events.csv_cell(s), "'" + s)
        self.assertEqual(events.csv_cell("a=b"), "a=b")
        self.assertEqual(events.csv_cell(5), 5)
        self.assertEqual(events.csv_cell("\x1b[2J"), ui.safe("\x1b[2J"))


class Console(unittest.TestCase):
    """The screen at 80x24 and 120x33 (--once --view events), its keys, and the three ways it has nothing to list."""

    def setUp(self):
        self.saved = (render.DEMO, render.DEMO_OS, render.DEMO_HEALTH, render.CFG["features"].get("health"), dict(render._EVENTS))
        render.DEMO, render.DEMO_OS, render.DEMO_HEALTH = True, None, ""
        render.CFG["features"]["health"] = True
        render._EVENTS.clear()

    def tearDown(self):
        render.DEMO, render.DEMO_OS, render.DEMO_HEALTH, health, cache = self.saved
        render.CFG["features"]["health"] = True if health is None else health
        render._EVENTS.clear()
        render._EVENTS.update(cache)

    def screen(self, cols, rows, *opts):
        s = render.events_once(["render.py", "--once", "--view", "events"] + list(opts), cols - 1, rows)
        lines = [ANSI.sub("", x) for x in s.split(LINE)]
        self.assertEqual(len(lines), rows)
        for x in lines:
            self.assertLessEqual(len(x), cols - 1, x)
        return lines

    def test_the_timeline_fits_both_sizes(self):
        for cols, rows in ((80, 24), (120, 33)):
            lines = self.screen(cols, rows)
            text = "\n".join(lines)
            self.assertIn("EVENTS", text)
            self.assertIn("last 24 hours", text)
            self.assertIn("opened (warning)", text)
            self.assertRegex(text, r"\d\d-\d\d \d\d:\d\d:\d\d")                                  # local time
            self.assertIn("unhealthy-container", text)
            self.assertIn("f: kind", lines[-1])

    def test_the_help_lists_the_screen_and_the_overview_opens_it(self):
        keys = {k for _t, items in ui.help_rows("events") for k, _v in items}
        self.assertIn("h/d/w", keys)
        self.assertIn("f", keys)
        self.assertTrue(any(k == "e" for _t, items in ui.help_rows("overview") for k, _v in items))
        self.assertEqual(ui.action("overview", "e"), "open-events")
        self.assertEqual([ui.action("events", k) for k in events.AGE_KEYS], ["age"] * 3)
        self.assertEqual(tuple(k for r in ui.KEYMAP if r.scope == "events" and r.action == "age" for k in r.keys), events.AGE_KEYS)
        self.assertEqual([n for _d, n in ui.screen_keys()], ["overview", "map", "cpu", "health", "ai"])  # 1-5 are as they were

    def test_period_and_kind_filter(self):
        one = "\n".join(self.screen(120, 33, "--age", "1h"))
        self.assertIn("last hour", one)
        self.assertIn("thermal", one)
        self.assertNotIn("Xorg", one)
        kind = "\n".join(self.screen(120, 33, "--kind", "oom"))
        self.assertIn("kind: out of memory", kind)
        self.assertNotIn("sshd", kind)
        self.assertIsNone(render.events_once(["x", "--age", "9h"], 119, 33))

    def test_keys(self):
        d = demo.events_answer(None, events.parse_query({}), NOW)
        rows = d["rows"]
        v = events.EventsView(now=NOW)
        v.rows = 5
        self.assertEqual(events.events_key(v, "down", rows), "")
        self.assertEqual((v.idx, v.cur), (1, rows[1]["id"]))
        events.events_key(v, "end", rows)
        self.assertEqual(v.idx, len(rows) - 1)
        events.events_key(v, "down", rows)
        self.assertEqual(v.idx, len(rows) - 1)                                                   # stops at the end
        events.events_key(v, "pgup", rows)
        self.assertEqual(v.idx, len(rows) - 5)
        events.events_key(v, "home", rows)
        self.assertEqual(v.idx, 0)
        self.assertEqual(events.events_key(v, "w", rows), "filter")
        self.assertEqual(v.age, "7d")
        self.assertEqual(events.events_key(v, "w", rows), "")                                    # the same period: nothing to read again
        self.assertEqual(events.events_key(v, "f", rows), "filter")
        self.assertEqual(v.kind, "problem")
        for _ in range(len(events.KIND_CYCLE) - 1):
            events.events_key(v, "f", rows)
        self.assertEqual(v.kind, "")                                                             # all kinds again
        events.events_key(v, "enter", rows)
        self.assertTrue(v.details)
        self.assertEqual(events.events_key(v, "esc", rows), "")                                  # closes the pane first
        self.assertEqual(events.events_key(v, "esc", rows), "back")
        self.assertEqual(events.events_key(v, "q", []), "back")
        self.assertEqual(events.events_key(v, "down", []), "")                                   # an empty list: keys do nothing

    def test_the_cursor_follows_its_event_when_the_list_changes(self):
        rows = demo.events_answer(None, events.parse_query({}), NOW)["rows"]
        v = events.EventsView(now=NOW)
        v.cur, v.idx = rows[3]["id"], 3
        events.events_sync(v, rows[1:])
        self.assertEqual(v.idx, 2)
        events.events_sync(v, rows[5:])                                                          # it is gone: the same index, clamped
        self.assertEqual(v.cur, rows[5:][min(2, len(rows[5:]) - 1)]["id"])

    def test_details_and_scrolling_stay_in_the_frame(self):
        for sel in ("1", "7", "15"):
            lines = self.screen(80, 24, "--select", sel, "--details")
            self.assertTrue(any("DETAILS" in x for x in lines), sel)
        small = self.screen(80, 12, "--select", "15", "--details")
        self.assertEqual(len(small), 12)

    def test_no_history_history_off_and_nothing_in_the_period(self):
        render.DEMO_HEALTH = "none"
        render._EVENTS.clear()
        text = "\n".join(self.screen(80, 24))
        self.assertIn("no history yet", text)
        render.DEMO_HEALTH = ""
        render.CFG["features"]["health"] = False
        render._EVENTS.clear()
        text = "\n".join(self.screen(80, 24))
        self.assertIn("history is off", text)
        self.assertIn("[features] health = no", text)
        self.assertNotIn("Xorg", text)
        render.CFG["features"]["health"] = True
        render._EVENTS.clear()
        text = "\n".join(self.screen(120, 33, "--kind", "hang"))                                  # the demo has no hang
        self.assertIn("no events in this period", text)

    def test_hostile_names_cannot_write_escape_sequences(self):
        evil = dict(id=1, ts=NOW, kind="crash", subject="\x1b]0;pwn\x07<b>x\x1b[2J", detail="\x9b31mred", source="journal", n=1)
        data = events.answer("ok", events.parse_query({}), NOW, [evil], 1)
        v = events.EventsView(now=NOW)
        out = events.events_lines(data, v, 79, 24)
        for line in out:
            self.assertNotIn("\x1b]", line)
            self.assertNotIn("\x9b", line)
            self.assertNotIn("\x07", line)
            self.assertNotIn("\x1b[2J", line)


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()
        cls.token_srv = serve(TOKEN)

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)
        stop(cls.token_srv)

    def test_the_page(self):
        code, head, page = get(self.srv, "/?view=events")
        self.assertEqual(code, 200)
        self.assertIn('class="hv ev"', page)
        self.assertIn("EVENTS", page)
        self.assertIn("unhealthy-container", page)
        self.assertIn('href="/?view=events&amp;format=csv"', page)
        self.assertIn('href="/?view=events&amp;format=json"', page)
        self.assertIn('href="/?view=events&amp;age=1h"', page)
        self.assertIn('aria-label="kind"', page)
        self.assertEqual(page.count("<main "), 1)
        self.assertNotIn("<script>alert", page)

    def test_the_filters_are_links_and_clamped(self):
        _c, _h, page = get(self.srv, "/?view=events&age=7d&kind=oom")
        self.assertIn("last 7 days", page)
        self.assertIn("kernel OOM killer", page)
        self.assertNotIn("Xorg", page)
        _c, _h, page = get(self.srv, "/?view=events&age=%3Cx%3E&kind=bogus")                       # a page clamps what it does not know
        self.assertIn("last 24 hours", page)
        self.assertNotIn("bogus", page)

    def test_the_footer_links_to_the_page_and_the_app_draws_it(self):
        _c, _h, page = get(self.srv, "/?view=cpu")
        self.assertIn('href="/?view=events"', page)
        code, _h, app = get(self.srv, "/app?view=events&live=0")
        self.assertEqual(code, 200)
        self.assertIn('"events"', app)
        self.assertIn('"nodes"', app)

    def test_the_api_document_and_its_etag(self):
        code, head, body = get(self.srv, "/api/v1/events?limit=3")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertEqual((doc["view"], doc["state"], doc["count"], doc["limit"]), ("events", "ok", 3, 3))
        self.assertGreater(doc["total"], 3)
        self.assertEqual(set(doc["events"][0]), {"id", "ts", "kind", "subject", "detail", "source", "n", "time"})
        self.assertGreaterEqual(doc["events"][0]["ts"], doc["events"][1]["ts"])
        self.assertEqual(doc["nodes"][0]["type"], "Title")
        tag = head["ETag"]
        code, _h, _b = get(self.srv, "/api/v1/events?limit=3", headers={"If-None-Match": tag})
        self.assertEqual(code, 304)
        self.assertIn("events", json.loads(get(self.srv, "/api/v1")[2])["views"])

    def test_the_api_filters(self):
        doc = json.loads(get(self.srv, "/api/v1/events?kind=oom")[2])
        self.assertEqual({e["kind"] for e in doc["events"]}, {"oom"})
        doc = json.loads(get(self.srv, "/api/v1/events?age=1h")[2])
        self.assertTrue(doc["events"] and all(e["ts"] >= doc["since"] for e in doc["events"]))
        doc = json.loads(get(self.srv, "/api/v1/events?limit=500")[2])
        self.assertLessEqual(len(doc["events"]), 500)

    def test_the_api_refuses_malformed_parameters(self):
        for q in ("limit=0", "limit=501", "limit=", "limit=x", "limit=1&limit=2", "since=-1", "since=", "since=1&age=1h", "age=", "age=3h", "kind=",
                  "kind=nope", "kind=%3Cscript%3E", "limit=%00"):
            code, _h, body = get(self.srv, "/api/v1/events?" + q)
            self.assertEqual(code, 400, q)
            self.assertIn("error", json.loads(body), q)
            self.assertNotIn("script", body, q)
        code, _h, _b = get(self.srv, "/api/v1/stream?view=events&limit=0")
        self.assertEqual(code, 400)

    def test_the_api_has_the_access_rules_of_the_other_views(self):
        self.assertEqual(get(self.token_srv, "/api/v1/events")[0], 401)
        self.assertEqual(get(self.token_srv, "/api/v1/events", headers={"Authorization": "Bearer " + TOKEN})[0], 200)
        self.assertEqual(get(self.token_srv, "/?view=events&format=csv")[0], 401)
        self.assertEqual(get(self.token_srv, "/?view=events&format=csv", headers={"Authorization": "Bearer " + TOKEN})[0], 200)
        self.assertEqual(get(self.srv, "/api/v1/events", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(get(self.srv, "/api/v1/events", headers={"Host": "evil.example"})[0], 421)

    def test_the_csv_and_json_downloads(self):
        code, head, body = get(self.srv, "/?view=events&format=csv")
        self.assertEqual(code, 200)
        self.assertTrue(head["Content-Type"].startswith("text/csv"))
        self.assertIn('attachment; filename="nuc-console-events.csv"', head["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][:4], ["time_utc", "ts", "kind", "subject"])
        self.assertGreater(len(rows), 5)
        code, head, body = get(self.srv, "/?view=events&format=json&kind=oom")
        self.assertEqual(code, 200)
        self.assertIn("filename=\"nuc-console-events.json\"", head["Content-Disposition"])
        self.assertEqual({e["kind"] for e in json.loads(body)["events"]}, {"oom"})
        for bad in ("format=xml", "format=", "format=csv&format=json", "format=csv&limit=0", "format=csv&kind=x", "format=csv&age=2h"):
            self.assertEqual(get(self.srv, "/?view=events&" + bad)[0], 400, bad)

    def test_a_download_quotes_formulas(self):
        evil = dict(id=1, ts=NOW, kind="crash", subject="=cmd|' /C calc'!A0", detail="@x", source="journal", n=1)
        data = events.answer("ok", events.parse_query({}), NOW, [evil], 1)
        with mock.patch.object(render, "events_data", lambda params: data):
            _c, _h, body = get(self.srv, "/?view=events&format=csv")
            self.assertIn("'=cmd|", body)
            self.assertIn("'@x", body)
            _c, _h, page = get(self.srv, "/?view=events&kind=throttle")                       # a key of its own: the page is cached for a second
            self.assertIn("=cmd|", page)
            self.assertNotIn("<script", page.split('class="hv ev"')[1].split("</table>")[0])

    def test_nothing_to_download_is_not_an_empty_file(self):
        with mock.patch.object(render, "events_data", lambda params: events.load(params, NOW, enabled=False)):
            code, _h, body = get(self.srv, "/?view=events&format=csv")
            self.assertEqual(code, 404)
            self.assertIn("history is off", body)
            doc = json.loads(get(self.srv, "/api/v1/events")[2])
            self.assertEqual((doc["state"], doc["events"]), ("off", []))
            self.assertIn("history is off", get(self.srv, "/?view=events&kind=hang")[2])

    def test_a_phone_gets_stacked_rows(self):
        self.assertIn("@media (max-width:600px){", webcss.EVENTS_VIEW)
        self.assertIn(".ev table.tbl tr", webcss.EVENTS_VIEW)
        self.assertIn(".ev table.tbl,.ev table.tbl tbody,.ev table.tbl tr,.ev table.tbl td{display:block", webcss.EVENTS_VIEW)
        self.assertIn(webcss.EVENTS_VIEW, webcss.CSS)

    def test_the_raw_wire_has_the_download_headers(self):
        head = raw(self.srv, "/?view=events&format=csv", headers=(("Host", "127.0.0.1"),))
        self.assertIn("Content-Disposition: attachment", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)


if __name__ == "__main__":
    unittest.main()
