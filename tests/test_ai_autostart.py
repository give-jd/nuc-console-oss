"""The model server after a restart of the web service (src/aiweb.py service_tick / autostart_plan / write_status), the status file the web view
writes for the unprivileged side (advisor.write_ai_status / read_ai_status), the `ai-down` alarm (problems.ai_state) and the AI page's one line.

Hermetic: a temporary AI folder, an Engine whose start_server and probe are fakes, an injected clock; no server is started and nothing is probed.
Platform-neutral: no POSIX file modes or links are required (those checks skip where the system has none).
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = os.path.join(os.path.abspath(os.sep), "nonexistent", "nuc-console.ini")
import advisor  # noqa: E402
import aisetup  # noqa: E402
import aiweb  # noqa: E402
import confedit  # noqa: E402
import nuc_config  # noqa: E402
import problems  # noqa: E402
import screens  # noqa: E402

T0 = 1_800_000_000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "ai")
        os.makedirs(self.d)
        self.cfg = nuc_config.load(os.path.join(self.tmp, "none.ini"))
        self.answering = False
        self.starts = []
        self.models = [{"id": "tiny"}, {"id": "other"}]
        self.eng = aiweb.Engine(directory=self.d, models=self.models, runtime={}, cfg_fn=lambda: self.cfg, hw_fn=lambda: {}, state_fn=lambda: None)
        self.eng.answers_fn = lambda endpoint: self.answering
        self.eng.autostart_delay, self.eng.autostart_retry = 5.0, 60.0
        self.eng.start_server = self.fake_start
        self.job_outcome = "running"
        self.ready = mock.patch.multiple(aisetup, runtime_ready=lambda d, rt=None, **kw: True, model_ready=lambda d, m, **kw: True,
                                         assess_model=lambda m, hw, *a: {"verdict": "ok", "why": ""})
        self.ready.start()
        self.addCleanup(self.ready.stop)
        self.err = io.StringIO()
        mock.patch.object(sys, "stderr", self.err).start()
        self.addCleanup(mock.patch.stopall)

    def fake_start(self, model_id=None, auto=False):
        self.starts.append(model_id)
        self.eng.job = {"kind": "start", "model": model_id or "", "state": self.job_outcome, "phase": "", "step": "", "done": 0, "total": 0, "rate": 0.0,
                        "error": "boom" if self.job_outcome == "failed" else "", "note": "", "started": T0, "ended": None, "mark": (T0, 0), "phase_at": T0}
        return True, "started"

    def web(self, **kw):
        advisor.write_web_state(lambda st: st.update(kw), self.eng.web_path())

    def tick(self, dt):
        self.eng.service_tick(T0 + dt, T0 + dt)


class AutostartTest(Base):
    def test_it_starts_the_chosen_model_once_after_the_delay(self):
        self.web(enabled=True, model="tiny")
        self.tick(0)
        self.tick(4)
        self.assertEqual(self.starts, [], "the web view serves first")
        self.assertTrue(self.eng.snapshot()["autostart"], "the page can say it is about to start")
        self.job_outcome = "done"
        self.tick(5)
        self.assertEqual(self.starts, ["tiny"])
        self.tick(6)
        self.tick(500)
        self.assertEqual(self.starts, ["tiny"], "one attempt per process start")
        self.assertFalse(self.eng.snapshot()["autostart"])

    def test_nothing_is_started_when_it_is_not_wanted(self):
        cases = [("a server answers", lambda: setattr(self, "answering", True)),
                 ("the AI is off", lambda: self.web(enabled=False)),
                 ("no model chosen", lambda: self.web(model="")),
                 ("autostart = no", lambda: self.cfg["ai"].update(autostart=False)),
                 ("web_actions = no", lambda: self.cfg["ai"].update(web_actions=False)),
                 ("a remote endpoint", lambda: self.cfg["ai"].update(endpoint="http://192.0.2.7:11434/v1"))]
        for name, setup in cases:
            with self.subTest(name):
                self.eng._svc, self.eng._user_stopped = None, False
                self.cfg["ai"].update(autostart=True, web_actions=True, endpoint="http://127.0.0.1:11434/v1")
                self.answering = False
                self.web(enabled=True, model="tiny")
                setup()
                self.eng.service_tick(T0, T0)
                self.eng.service_tick(T0 + 10, T0 + 10)
                self.assertEqual(self.starts, [], name)

    def test_the_default_is_yes_and_the_web_json_of_another_state_is_not_needed(self):
        self.assertIs(self.cfg["ai"]["autostart"], True)

    def test_not_again_after_the_user_turned_it_off_or_stopped_the_server(self):
        self.web(enabled=True, model="tiny")
        self.eng.stop_server()
        self.tick(0)
        self.tick(10)
        self.assertEqual(self.starts, [])
        self.eng._svc, self.eng._user_stopped = None, False
        self.eng.turn_off()
        self.web(enabled=True, model="tiny")  # turned on again by hand, after the stop of this run
        self.tick(0)
        self.tick(10)
        self.assertEqual(self.starts, [], "turn AI off in this process: no automatic start before the next process start")

    def test_one_retry_after_a_minute_and_no_more(self):
        self.web(enabled=True, model="tiny")
        self.job_outcome = "failed"
        self.tick(0)
        self.tick(5)
        self.assertEqual(len(self.starts), 1)
        self.tick(6)  # the job is seen to have failed
        self.assertIn("failed: boom", self.eng.notice["text"], "shown as the page's last note")
        self.assertIn("autostart failed: boom", self.err.getvalue())
        self.tick(65)
        self.assertEqual(len(self.starts), 1, "60 s after the failure, not before")
        self.tick(67)
        self.assertEqual(len(self.starts), 2)
        self.tick(68)
        self.tick(1000)
        self.assertEqual(len(self.starts), 2, "no restart loop")

    def test_a_refused_start_is_a_failure_too_and_nothing_raises(self):
        self.web(enabled=True, model="tiny")
        self.eng.start_server = lambda model_id=None, auto=False: (self.starts.append(model_id) or (False, "busy: downloading"))
        self.tick(0)
        self.tick(5)
        self.tick(66)
        self.tick(200)
        self.assertEqual(len(self.starts), 2)
        with mock.patch.object(self.eng, "autostart_plan", side_effect=RuntimeError("bug")):
            self.eng._svc = None
            self.tick(0)
            self.tick(5)
        self.assertIn("unexpected error RuntimeError", self.eng.notice["text"])

    def test_a_cancelled_start_is_not_retried(self):
        self.web(enabled=True, model="tiny")
        self.job_outcome = "cancelled"
        self.tick(0)
        self.tick(5)
        self.tick(6)
        self.tick(100)
        self.assertEqual(len(self.starts), 1)

    def test_what_is_missing_is_said_not_started(self):
        self.web(enabled=True, model="tiny")
        for name, patch, word in (("runtime", {"runtime_ready": lambda d, rt=None, **kw: False}, "model server is not installed"),
                                  ("model", {"model_ready": lambda d, m, **kw: False}, "tiny is not installed"),
                                  ("verdict", {"assess_model": lambda m, hw, *a: {"verdict": "no", "why": "too big"}}, "will not work on this machine: too big")):
            with self.subTest(name), mock.patch.multiple(aisetup, **patch):
                self.eng._svc, self.eng.notice = None, None
                self.tick(0)
                self.tick(5)
                self.tick(100)
                self.assertEqual(self.starts, [])
                self.assertIn(word, self.eng.notice["text"])
                self.assertFalse(self.eng.notice["ok"])
                self.assertIn(word, self.err.getvalue())

    def test_a_model_the_catalog_does_not_list_is_said(self):
        self.web(enabled=True, model="zzz")
        self.tick(0)
        self.tick(5)
        self.assertEqual(self.starts, [])
        self.assertIn("does not list", self.eng.notice["text"])

    def test_the_demo_and_a_second_call_begin_nothing(self):
        self.assertFalse(aiweb.Engine(demo=True).service_begin())
        with mock.patch("threading.Thread") as th:
            self.assertTrue(self.eng.service_begin())
            self.assertFalse(self.eng.service_begin())
            self.assertEqual(th.call_count, 1)

    def test_the_console_does_not_begin_a_service(self):
        import inspect
        import render
        src = inspect.getsource(render)
        self.assertNotIn("service_begin", src, "only web.py starts a server by itself")
        self.assertIn("service_begin", inspect.getsource(sys.modules["web"] if "web" in sys.modules else __import__("web")))


class StatusFileTest(Base):
    def path(self):
        return os.path.join(self.d, advisor.AI_STATUS_FILE)

    def test_it_says_on_answering_and_since_when(self):
        self.web(enabled=True, model="tiny")
        self.cfg["ai"].update(model="tiny", enabled=True)
        self.tick(0)  # the service starts: since = now
        got = advisor.read_ai_status(self.path())
        self.assertEqual(got, {"ts": int(T0), "since": int(T0), "down_for": 0, "enabled": True, "answering": False, "model": "tiny", "reason": "starting"})
        self.tick(40)
        got = advisor.read_ai_status(self.path())
        self.assertEqual((got["down_for"], got["since"]), (40, int(T0)), "still not answering: the same state, 40 s long")
        self.answering = True
        self.tick(80)
        got = advisor.read_ai_status(self.path())
        self.assertEqual((got["answering"], got["down_for"], got["ts"]), (True, 0, int(T0 + 80)))
        self.tick(85)
        self.assertEqual(advisor.read_ai_status(self.path())["ts"], int(T0 + 80), "refreshed every 30 s, not at every tick")

    def test_off_or_no_model_is_not_enabled(self):
        self.eng.write_status(100.0, T0)
        self.assertFalse(advisor.read_ai_status(self.path())["enabled"])

    def test_a_folder_that_cannot_be_written_is_logged_once_and_never_raises(self):
        self.eng._dir = os.path.join(self.d, "nope", "deeper")
        with mock.patch.object(advisor, "write_ai_status", side_effect=PermissionError(13, "denied")):
            self.assertIsNone(self.eng.write_status(100.0, T0))
            self.assertIsNone(self.eng.write_status(130.0, T0 + 30))
        self.assertEqual(self.err.getvalue().count("cannot write ai-status.json"), 1)

    @unittest.skipIf(os.name != "posix", "POSIX file modes")
    def test_the_file_is_world_readable(self):
        self.eng.write_status(100.0, T0)
        self.assertEqual(os.stat(self.path()).st_mode & 0o777, 0o644)
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")], [])

    def put(self, text, raw=False):
        with open(self.path(), "wb") as f:
            f.write(text if raw else text.encode())

    def test_hostile_content_is_nothing(self):
        good = {"v": 1, "ts": 5, "since": 4, "down_for": 3, "enabled": True, "answering": False, "model": "tiny", "reason": "unknown"}
        self.put(json.dumps(good))
        self.assertEqual(advisor.read_ai_status(self.path())["model"], "tiny")
        bad = [dict(good, v=2), dict(good, v=True), dict(good, ts=5.5), dict(good, ts=True), dict(good, ts="5"), dict(good, since=-1), dict(good, down_for=-1), dict(good, down_for=10 ** 9), dict(good, down_for=1.5), dict(good, reason="because"), dict(good, reason=None), dict(good, enabled=1),
               dict(good, answering="no"), [good], "x"]
        for b in bad:
            with self.subTest(b):
                self.put(json.dumps(b))
                self.assertIsNone(advisor.read_ai_status(self.path()))
        for text in ("", "{", "NaN", '{"v":1,"ts":1e3}', "[" * 100000, '{"v":1,"ts":' + "9" * 40 + "}", "\xff"):
            with self.subTest(text[:12]):
                self.put(text, raw=False) if text != "\xff" else self.put(b"\xff\xfe", raw=True)
                self.assertIsNone(advisor.read_ai_status(self.path()))
        self.assertEqual(advisor.read_ai_status(self.path() + ".missing"), None)
        self.put(json.dumps(dict(good, model="a b<script>")))
        self.assertEqual(advisor.read_ai_status(self.path())["model"], "", "a name that is not a model name is dropped")

    def test_too_big_is_refused(self):
        self.put(json.dumps({"v": 1, "ts": 5, "since": 4, "down_for": 3, "reason": "unknown", "enabled": True, "answering": False, "model": "x", "pad": "p" * 5000}))
        self.assertIsNone(advisor.read_ai_status(self.path()))

    def test_a_link_is_not_followed(self):
        target = os.path.join(self.tmp, "target.json")
        with open(target, "w") as f:
            json.dump({"v": 1, "ts": 5, "since": 4, "down_for": 3, "enabled": True, "answering": False, "reason": "unknown"}, f)
        try:
            os.symlink(target, self.path())
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("no symbolic links here")
        self.assertIsNone(advisor.read_ai_status(self.path()))

    def test_a_folder_in_its_place_is_nothing(self):
        os.makedirs(self.path())
        self.assertIsNone(advisor.read_ai_status(self.path()))


class AlarmTest(Base):
    def status(self, **kw):
        st = dict(ts=T0, down_for=1000, enabled=True, answering=False, model="qwen3-8b", reason="unknown")
        st.update(kw)
        advisor.write_ai_status(st["enabled"], st["answering"], st["down_for"], st["model"], st["ts"], os.path.join(self.d, advisor.AI_STATUS_FILE), st["reason"])

    def state(self, now=T0):
        return problems.ai_state(now, os.path.join(self.d, advisor.AI_STATUS_FILE))

    def test_down_only_after_two_minutes_of_not_answering(self):
        self.status(down_for=120)
        self.assertEqual(self.state()[:2], ("ok", "qwen3-8b"), "120 s is still the grace")
        self.status(down_for=121)
        self.assertEqual(self.state()[:2], ("down", "qwen3-8b"))

    def test_answering_clears_it(self):
        self.status(answering=True)
        self.assertEqual(self.state()[0], "ok")

    def test_off_missing_stale_or_from_the_future_say_nothing(self):
        self.status(enabled=False)
        self.assertEqual(self.state()[0], "unknown")
        os.unlink(os.path.join(self.d, advisor.AI_STATUS_FILE))
        self.assertEqual(self.state()[:2], ("unknown", ""), "no file: no alarm, no crash")
        self.status()
        self.assertEqual(self.state(T0 + 180)[0], "down")
        self.assertEqual(self.state(T0 + 181)[0], "unknown", "the web view is not running: stale after 3 minutes")
        self.assertEqual(self.state(T0 - 61)[0], "unknown", "a clock that went backwards")

    def raw(self, now=T0):
        with mock.patch.object(advisor, "ai_status_path", return_value=os.path.join(self.d, advisor.AI_STATUS_FILE)):
            net = {"ts": now, "errors": {}, "listeners": [], "serve": []}
            return [x for x in problems.problems_raw(net, {"ts": now, "containers": []}, now=now) if x[2] == "ai-down"]

    def test_the_problem_is_raised_and_cleared_in_problems_raw(self):
        self.status()
        got = self.raw()
        self.assertEqual(got, [(1, "AI is on (qwen3-8b) but its model server is not running: turn AI off and on again on the AI page", "ai-down")])
        self.status(answering=True)
        self.assertEqual(self.raw(), [])
        self.status()
        with mock.patch.dict(problems.CFG["features"], {"ai": False}):
            self.assertEqual(self.raw(), [], "with [features] ai = no there is no AI to report on")

    def test_a_hostile_model_name_never_reaches_the_text(self):
        with open(os.path.join(self.d, advisor.AI_STATUS_FILE), "w") as f:
            json.dump({"v": 1, "ts": int(T0), "since": 1, "down_for": 999, "enabled": True, "answering": False, "model": "\x1b[31m<b>", "reason": "unknown"}, f)
        self.assertEqual(self.raw()[0][1], "AI is on but its model server is not running: turn AI off and on again on the AI page")

    def test_the_catalog_and_the_rules(self):
        title, why, fix = problems.CATALOG["ai-down"]
        self.assertIn("nuc-console-ai serve --install-service", fix)
        self.assertIn("ai-down", nuc_config.MUTABLE_ALERTS, "operational: a muted AI hides no attack surface")
        self.assertNotIn("ai-down", problems.NOT_ACCEPTABLE)
        self.assertIn("ai-down", problems.OS_CATALOG["windows"])


class ReviewTest(Base):
    def test_a_turn_off_during_the_probe_wins(self):  # race: the plan said start, the user said stop while it probed
        self.web(enabled=True, model="tiny")

        def probe(endpoint):
            self.eng.turn_off()
            return False
        self.eng.answers_fn = probe
        self.tick(0)
        self.tick(5)
        self.assertEqual(self.starts, [])

    def test_an_automatic_job_that_meets_the_stop_before_it_spawns_is_cancelled(self):
        job = {"auto": True}
        self.eng._user_stopped = True
        with self.assertRaises(aisetup.Cancelled):
            self.eng._work_start(job, None)

    def test_the_end_of_the_service_starts_nothing_and_ends_what_is_under_way(self):
        self.web(enabled=True, model="tiny")
        self.tick(0)
        self.eng.shutdown()
        self.tick(5)
        self.tick(100)
        self.assertEqual(self.starts, [], "once stopping, no child is spawned")
        self.eng._svc_stop.clear()
        self.eng._svc = None
        self.answering = False
        self.eng.answers_fn = lambda e: self.eng._svc_stop.set() or False  # the service ends during the probe
        self.tick(0)
        self.tick(5)
        self.assertEqual(self.starts, [])

    def test_a_skip_does_not_spend_an_attempt(self):
        self.web(enabled=True, model="tiny")
        self.answering = True
        self.tick(0)
        self.tick(5)
        self.assertEqual(self.eng._svc["tries"], 0)
        self.answering = False
        self.job_outcome = "failed"
        self.eng._svc["next"] = T0 + 6
        self.tick(6)
        self.tick(7)
        self.tick(70)
        self.assertEqual(len(self.starts), 2, "the first real attempt, and the one retry")

    def test_the_ai_feature_off_stops_the_start_and_the_status(self):
        self.web(enabled=True, model="tiny")
        self.cfg["features"]["ai"] = False
        self.assertFalse(self.eng.service_begin())
        self.assertEqual(self.eng.autostart_plan()[0], "skip")
        self.assertIsNone(self.eng.write_status(100.0, T0))
        self.tick(0)
        self.tick(10)
        self.assertEqual(self.starts, [])
        self.assertFalse(os.path.exists(os.path.join(self.d, advisor.AI_STATUS_FILE)))

    def test_any_http_answer_is_an_answer_even_a_401(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertTrue(aisetup.answers("http://127.0.0.1:%d/v1" % srv.server_address[1], 2))
        self.assertFalse(aisetup.probe("http://127.0.0.1:%d/v1" % srv.server_address[1], 2)[0], "the advisor's own probe is stricter: that is why")
        dead = srv.server_address[1]
        srv.shutdown()
        srv.server_close()
        self.assertFalse(aisetup.answers("http://127.0.0.1:%d/v1" % dead, 1))
        self.assertFalse(aisetup.answers("file:///etc/passwd", 1))

    def test_an_endpoint_on_another_machine_is_not_an_ai_down(self):
        self.cfg["ai"].update(enabled=True, model="tiny", endpoint="http://192.0.2.7:11434/v1")
        got = self.eng.write_status(100.0, T0)
        self.assertFalse(got["enabled"], "autostart cannot fix it, and the AI KPI already says down")

    def test_each_reason_has_its_own_words(self):
        for reason, word in (("no-runtime", "server is not installed: download a model"), ("no-model", "model is not installed: download it"),
                             ("refused", "will not work on this machine"), ("failed", "could not be started"),
                             ("starting", "turn AI off and on again"), ("unknown", "turn AI off and on again")):
            with self.subTest(reason):
                d = os.path.join(self.tmp, reason)
                os.makedirs(d)
                path = os.path.join(d, advisor.AI_STATUS_FILE)
                advisor.write_ai_status(True, False, 500, "tiny", T0, path, reason)
                with mock.patch.object(advisor, "ai_status_path", return_value=path):
                    net = {"ts": T0, "errors": {}, "listeners": [], "serve": []}
                    got = [x for x in problems.problems_raw(net, {"ts": T0, "containers": []}, now=T0) if x[2] == "ai-down"]
                self.assertEqual(len(got), 1)
                self.assertIn(word, got[0][1])
                self.assertEqual(got[0][0], 1)

    def test_the_engine_names_why_it_did_not_start(self):
        self.web(enabled=True, model="tiny")
        for patch, reason in (({"runtime_ready": lambda d, rt=None, **kw: False}, "no-runtime"), ({"model_ready": lambda d, m, **kw: False}, "no-model"),
                              ({"assess_model": lambda m, hw, *a: {"verdict": "no", "why": "x"}}, "refused")):
            with self.subTest(reason), mock.patch.multiple(aisetup, **patch):
                self.eng._svc, self.eng._reason = None, ""
                self.tick(0)
                self.tick(5)
                self.assertEqual(self.eng.write_status(100.0, T0)["reason"], reason)
        self.eng._svc, self.eng._reason = None, ""
        self.job_outcome = "failed"
        self.tick(0)
        self.tick(5)
        self.tick(6)
        self.assertEqual(self.eng.write_status(100.0, T0)["reason"], "starting", "a retry is pending")
        self.tick(67)
        self.tick(68)
        self.assertEqual(self.eng.write_status(100.0, T0)["reason"], "failed")

    def test_a_clock_that_steps_fires_nothing(self):
        self.web(enabled=True, model="tiny")
        self.cfg["ai"].update(enabled=True, model="tiny")
        self.eng.autostart_delay = 10 ** 6
        self.eng.service_tick(1000.0, T0)
        self.eng.service_tick(1040.0, T0 + 10 ** 5)  # the wall clock jumped a day; 40 s went by
        got = advisor.read_ai_status(os.path.join(self.d, advisor.AI_STATUS_FILE))
        self.assertEqual(got["down_for"], 40, "counted by the monotonic clock, not by the day that passed")
        self.eng.status_every = 1
        self.eng.service_tick(1041.0, T0 + 10 ** 5 + 1)
        self.assertEqual(advisor.read_ai_status(os.path.join(self.d, advisor.AI_STATUS_FILE))["down_for"], 41)
        self.assertEqual(problems.ai_state(T0 + 10 ** 5 + 1, os.path.join(self.d, advisor.AI_STATUS_FILE))[0], "ok", "41 s is within the grace")

    def test_a_fifo_and_a_link_are_not_read(self):
        path = os.path.join(self.d, advisor.AI_STATUS_FILE)
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this system")
        os.mkfifo(path)
        self.assertIsNone(advisor.read_ai_status(path), "returns at once, does not wait for a writer")


class ConfigTest(unittest.TestCase):
    def test_the_key_parses_and_the_settings_page_knows_it(self):
        for text, want in (("", True), ("autostart = yes", True), ("autostart = no", False), ("autostart = maybe", True)):
            with self.subTest(text):
                fd, path = tempfile.mkstemp(suffix=".ini")
                os.close(fd)
                try:
                    with open(path, "w") as f:
                        f.write("[ai]\n" + text + "\n")
                    with contextlib.redirect_stderr(io.StringIO()):
                        cfg = nuc_config.load(path)
                    self.assertIs(cfg["ai"]["autostart"], want)
                finally:
                    os.unlink(path)
        key = next(k for k in confedit.KEYS if (k.section, k.name) == ("ai", "autostart"))
        self.assertEqual(key.default, "yes")


class PageTest(unittest.TestCase):
    def snap(self, **kw):
        s = {"state": ("on", "on: the advisor asks http://127.0.0.1:8080/v1 (tiny)"), "locked": False, "job": None, "chat": {"pending": None},
             "switch": {"on": True, "by": "web", "can_off": True}, "model": {"state": "down", "endpoint": "http://127.0.0.1:8080/v1", "model": "tiny"},
             "autostart": False}
        s.update(kw)
        return s

    def words(self, snap):
        nodes = screens.ai_control_nodes(snap, None, None, [], "", "", screens.AiActs("tok", "view=ai"), None)
        return [(getattr(x, "label", None) or getattr(x, "text", "")) for n in nodes for x in getattr(n, "items", [])]

    def test_on_and_not_running_is_one_statement(self):
        w = self.words(self.snap())
        self.assertIn("AI is on but the model server is not running: turn AI off and on again to start it", w)
        self.assertFalse([x for x in w if "nothing answers" in x or x == "on: the advisor asks http://127.0.0.1:8080/v1 (tiny)"], w)
        self.assertEqual(sum(1 for x in w if x == "✖ NOT RUNNING"), 1, "one badge, not an ON above a NOT RUNNING")
        self.assertNotIn("● ON", w)
        self.assertIn("AI is on but the model server is not running: starting it", self.words(self.snap(autostart=True)))

    def test_other_states_keep_their_lines(self):
        w = self.words(self.snap(model={"state": "ready", "model": "tiny", "row": {"size_mb": 1, "vram_mb": 1}}))
        self.assertIn("● ON", w)
        self.assertFalse(screens.ai_not_running(self.snap(state=("off", "off"), model={"state": "down"})))
        self.assertFalse(screens.ai_not_running(self.snap(state=("running", "x"))))


if __name__ == "__main__":
    unittest.main()
