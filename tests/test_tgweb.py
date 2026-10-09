"""Tests for tgweb.py and the Telegram page of the web view: pairing from the browser, the requests handed to the notifier, the forms' rules.

No network and no real Telegram: a fake transport answers getMe and getUpdates (the Start carries the code the engine put in its link), the
clock and the sleeps are fake, and the notifier's folder is a temporary one. The notifier's side is the real notify.Service, so a test can
follow a pairing from the form to the token file. Tokens and chat ids are fake values built at runtime.
"""
import contextlib
import http.client
import io
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
_TMP = tempfile.mkdtemp(prefix="nuc-tgweb-test-")
os.environ.setdefault("NUC_CONSOLE_CONFIG", os.path.join(_TMP, "config.ini"))
import notify  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import tgweb  # noqa: E402
import web  # noqa: E402

SECRET = "Kp7" * 12  # the secret half of the fake token: it must never show up on a page, in a file of the web view or in its memory
TOKEN = "123456789:" + SECRET
CHAT = 5550001
POSIX = sys.platform != "win32"


class Clock:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Telegram:
    """getMe names the bot; getUpdates brings the Start of `user` with the code of the engine's link (press=False: nothing comes)."""

    def __init__(self, eng_ref, clock, user="bob_99", press=True, me=None):
        self.eng_ref, self.clock, self.user, self.press, self.calls = eng_ref, clock, user, press, []
        self.hold = False  # nobody presses, and the wait is real: until Cancel (at most 5 s a call)
        self.me = me or {"ok": True, "result": {"username": "my_bot"}}

    def __call__(self, method, payload):
        self.calls.append(method)
        if method == "getMe":
            if isinstance(self.me, Exception):
                raise self.me
            return self.me
        if method == "getUpdates":
            if self.hold and payload.get("timeout"):
                self.eng_ref[0]._stop.wait(5)
                return {"ok": True, "result": []}
            self.clock.t += payload.get("timeout", 0)
            code = re.search(r"start=([A-Za-z0-9_-]+)", self.eng_ref[0].link() or "")
            if self.press and code and payload.get("timeout"):
                msg = {"message_id": 1, "date": 0, "text": "/start " + code.group(1), "chat": {"id": CHAT, "type": "private"},
                       "from": {"id": CHAT, "is_bot": False, "username": self.user}}
                return {"ok": True, "result": [{"update_id": 7, "message": msg}]}
            return {"ok": True, "result": []}
        return {"ok": True, "result": {"message_id": 1}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "notify")
        os.makedirs(notify.inbox_dir(self.dir))
        os.makedirs(notify.secret_dir(self.dir), exist_ok=True)
        self.cfg = nuc_config.load("/nonexistent")
        self.clock = Clock()
        ref = [None]
        self.tg = Telegram(ref, self.clock)
        self.eng = tgweb.Engine(notify_dir=self.dir, make_transport=lambda token: self.tg, cfg_fn=lambda: self.cfg, clock=self.clock,
                                sleep=self.clock.sleep, wait=120)
        ref[0] = self.eng
        for p in (mock.patch.object(notify, "NOTIFY_DIR", self.dir), mock.patch.object(nuc_config, "NOTIFY_DIR", self.dir),
                  mock.patch.object(nuc_config, "PORTABLE", "")):
            p.start()
            self.addCleanup(p.stop)
        self.notifier_alive()

    def notifier_alive(self, listening=True, **kw):
        """The status.json a listening notifier writes every cycle."""
        st = notify.status_dict(self.clock(), kw.get("enabled", False), kw.get("paired", False), kw.get("username"), None, None, None, listening=listening)
        notify.write_status(self.dir, st)

    def inbox(self):
        return sorted(os.listdir(notify.inbox_dir(self.dir)))

    def requests(self):
        out = []
        for n in self.inbox():
            with open(os.path.join(notify.inbox_dir(self.dir), n), encoding="utf-8") as f:
                out.append(json.load(f))
        return out

    def notifier_takes_them(self):
        """The real service's side: it reads and does the requests (its transport is a fake that only sends)."""
        svc = notify.Service(self.dir, self.clock, self.clock.sleep, records=lambda: [], make_transport=lambda token: self.tg, log=lambda text: None)
        svc.listening = True
        svc.requests(self.cfg)
        svc.look(nuc_config.telegram(self.cfg, self.dir))

    def paired(self):
        self.assertTrue(self.eng.pair(TOKEN, "@Bob_99")[0])
        self.assertTrue(self.eng.wait())


class Pairing(Base):
    def test_a_pairing_goes_from_the_form_to_the_notifier_and_the_token_is_not_kept_here(self):
        ok, _ = self.eng.pair(TOKEN, " @Bob_99 ")
        self.assertTrue(ok)
        self.assertTrue(self.eng.wait())
        self.assertEqual(self.tg.calls[:2], ["getMe", "getUpdates"])
        (req,) = self.requests()
        self.assertEqual((req["action"], req["token"], req["chat_id"], req["username"], req["bot"]), ("pair", TOKEN, CHAT, "bob_99", "my_bot"))
        if POSIX:
            (name,) = self.inbox()
            self.assertEqual(os.stat(os.path.join(notify.inbox_dir(self.dir), name)).st_mode & 0o777, 0o640)  # the notifier reads it as its group
        snap = self.eng.snapshot()
        self.assertEqual((snap["job"]["state"], snap["busy"], snap["pending"]["action"]), ("handing", True, "pair"))
        for value in list(vars(self.eng).values()) + [snap]:
            self.assertNotIn(SECRET, repr(value))
        self.notifier_takes_them()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(notify.read_token(self.dir), TOKEN)
        self.assertEqual(notify.read_chat(self.dir)["chat_id"], CHAT)
        snap = self.eng.snapshot()
        self.assertEqual((snap["job"]["state"], snap["busy"], snap["pending"]), ("done", False, None))
        self.assertTrue(snap["notice"]["ok"])
        self.assertIn("paired with @bob_99", snap["notice"]["text"])
        self.assertEqual(tgweb.state_of(snap)[0], "on")
        self.assertNotIn(SECRET, repr(snap))

    def test_the_link_carries_a_one_time_code_and_the_redirect_only_goes_there(self):
        self.tg.hold = True
        self.eng.pair(TOKEN, "bob_99")
        link = ""
        for _ in range(200):  # the thread puts the link in as soon as getMe answered
            link = self.eng.link()
            if link:
                break
            threading.Event().wait(0.01)
        self.assertRegex(link, r"^https://t\.me/my_bot\?start=[A-Za-z0-9_-]{16,64}$")
        self.assertTrue(tgweb.BOT_LINK.fullmatch(link))
        self.eng.cancel()
        self.eng.wait()

    def test_cancel_pairs_nothing_and_writes_nothing(self):
        self.tg.hold = True
        self.eng.pair(TOKEN, "bob_99")
        for _ in range(200):
            if self.eng.link():
                break
            threading.Event().wait(0.01)
        ok, text = self.eng.cancel()
        self.assertTrue(ok)
        self.assertTrue(self.eng.wait())
        snap = self.eng.snapshot()
        self.assertEqual((snap["job"]["state"], snap["busy"], self.inbox()), ("cancelled", False, []))
        self.assertIn("nothing was paired", snap["notice"]["text"])
        self.assertEqual(self.eng.cancel()[0], False)

    def test_nobody_pressing_start_in_time_says_so(self):
        self.tg.press = False
        self.eng.pair(TOKEN, "bob_99")
        self.assertTrue(self.eng.wait())
        snap = self.eng.snapshot()
        self.assertEqual((snap["job"]["state"], self.inbox()), ("failed", []))
        self.assertIn("nobody pressed Start in time", snap["notice"]["text"])

    def test_somebody_else_pressing_start_does_not_pair(self):
        self.tg.user = "mallory"
        self.eng.pair(TOKEN, "bob_99")
        self.assertTrue(self.eng.wait())
        self.assertEqual((self.eng.snapshot()["job"]["state"], self.inbox()), ("failed", []))

    def test_a_wrong_token_is_telegrams_word_without_the_token(self):
        self.tg.me = notify.TelegramError("HTTP 401: Unauthorized", status=401)
        self.eng.pair(TOKEN, "bob_99")
        self.assertTrue(self.eng.wait())
        snap = self.eng.snapshot()
        self.assertEqual(snap["job"]["state"], "failed")
        self.assertIn("HTTP 401", snap["notice"]["text"])
        self.assertNotIn(SECRET, repr(snap))
        self.tg.me = {"ok": True, "result": {"username": "no way"}}
        self.eng.pair(TOKEN, "bob_99")
        self.eng.wait()
        self.assertIn("did not name the bot", self.eng.snapshot()["notice"]["text"])

    def test_what_cannot_be_a_token_or_a_username_starts_nothing(self):
        for token, user, why in ((SECRET, "bob_99", "not a bot token"), (TOKEN, "bob", "5 to 32"), (TOKEN, "bob-99", "5 to 32"), ("", "", "not a bot token")):
            with self.subTest(user=user):
                ok, text = self.eng.pair(token, user)
                self.assertFalse(ok)
                self.assertIn(why, text)
                self.assertIsNone(self.eng.job)
                self.assertNotIn(SECRET, text)
        self.assertEqual(self.tg.calls, [])

    def test_refusals_locked_portable_busy_and_a_notifier_that_does_not_listen(self):
        self.cfg["telegram"]["web_actions"] = False
        self.assertIn("web_actions = no", self.eng.pair(TOKEN, "bob_99")[1])
        self.assertIn("web_actions = no", self.eng.request("test")[1])
        self.cfg["telegram"]["web_actions"] = True
        with mock.patch.object(nuc_config, "PORTABLE", "/x"):  # a portable run (the desktop app): its notifier is started with it, nothing refused
            self.notifier_alive(listening=False)
            self.assertIn("quit nuc-console and start it again", self.eng.request("on")[1])
        self.notifier_alive(listening=False)
        self.assertIn("does not take this page's requests", self.eng.pair(TOKEN, "bob_99")[1])
        self.notifier_alive()
        self.clock.t += notify.STALE_S + 1  # its status is old: it is not running
        self.assertIn("does not take this page's requests", self.eng.request("on")[1])
        self.notifier_alive()
        self.assertTrue(self.eng.request("on")[0])
        self.assertIn("not finished", self.eng.request("off")[1])
        self.assertIn("not finished", self.eng.pair(TOKEN, "bob_99")[1])
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.tg.calls, [])


class Requests(Base):
    def test_on_off_and_test_are_requests_without_a_secret_and_the_answer_is_the_notice(self):
        self.paired()
        self.notifier_takes_them()
        self.eng.snapshot()
        self.notifier_alive()
        self.assertTrue(self.eng.request("test")[0])
        (req,) = self.requests()
        self.assertEqual(set(req), {"v", "id", "action", "ts"})
        self.assertTrue(self.eng.snapshot()["busy"])
        self.notifier_takes_them()
        snap = self.eng.snapshot()
        self.assertEqual((snap["busy"], snap["notice"]["ok"], snap["notice"]["text"]), (False, True, "test message sent to @bob_99"))
        self.eng.request("off")
        self.notifier_takes_them()
        snap = self.eng.snapshot()
        self.assertEqual((snap["notice"]["text"], snap["settings"]["enabled"]), ("off", False))
        self.eng.request("on")
        self.notifier_takes_them()
        self.assertEqual(self.eng.snapshot()["settings"]["by"], "web")
        self.assertEqual(self.eng.request("dance"), (False, "unknown action"))

    def test_a_notifier_that_does_not_answer_is_said_after_a_minute(self):
        self.eng.request("on")
        self.clock.t += tgweb.ANSWER_S - 1
        self.assertTrue(self.eng.snapshot()["busy"])
        self.clock.t += 2
        snap = self.eng.snapshot()
        self.assertFalse(snap["busy"])
        self.assertIn("did not answer", snap["notice"]["text"])

    def test_an_inbox_that_cannot_be_written_is_said(self):
        os.rmdir(notify.inbox_dir(self.dir))
        ok, text = self.eng.request("on")
        self.assertFalse(ok)
        self.assertIn("run it again", text)
        self.assertFalse(self.eng.snapshot()["busy"])


class Demo(unittest.TestCase):
    def test_the_demo_pairs_switches_and_tests_in_memory(self):
        clock = Clock()
        eng = tgweb.Engine(demo=True, cfg_fn=lambda: nuc_config.load("/nonexistent"), clock=clock, sleep=lambda s: None)
        eng.demo_start = 0.01
        self.assertEqual(tgweb.state_of(eng.snapshot())[0], "off")
        self.assertFalse(eng.request("test")[0])
        self.assertTrue(eng.pair(TOKEN, "demo_user")[0])
        self.assertTrue(eng.wait())
        snap = eng.snapshot()
        self.assertEqual((snap["job"]["state"], tgweb.state_of(snap)[0]), ("done", "on"))
        self.assertIn("demo", snap["notice"]["text"])
        self.assertTrue(eng.request("test")[0])
        eng.request("off")
        self.assertEqual(tgweb.state_of(eng.snapshot())[0], "off")
        self.assertNotIn(SECRET, repr(vars(eng)))


class Words(unittest.TestCase):
    def snap(self, enabled=True, paired=True, ts=0, failing=None, by="web", portable=False):
        return {"settings": {"enabled": enabled, "username": "bob_99", "by": by}, "status": {"paired": paired, "ts": ts, "failing_since": failing,
                                                                                         "last_error": "HTTP 401: bot123456789:" + SECRET},
                "portable": portable, "listening": True, "now": 10}

    def test_the_state_line(self):
        self.assertEqual(tgweb.state_of(self.snap())[0], "on")
        self.assertEqual(tgweb.state_of(self.snap(enabled=False))[0], "off")
        self.assertEqual(tgweb.state_of(self.snap(paired=False))[0], "unpaired")
        self.assertEqual(tgweb.state_of(self.snap(ts=-1000))[0], "down")
        state, line = tgweb.state_of(self.snap(failing=1))
        self.assertEqual(state, "failing")
        self.assertNotIn(SECRET, line)
        self.assertEqual(tgweb.state_of(self.snap(portable=True))[0], "on", "a portable run sends too (the desktop app)")

    def test_the_job_line(self):
        self.assertEqual(tgweb.job_text({"state": "waiting", "username": "bob_99", "until": 130, "text": ""}, 10), "waiting for Start from @bob_99 (2 min left)")
        self.assertEqual(tgweb.job_text({"state": "waiting", "username": "bob_99", "until": 40, "text": ""}, 10), "waiting for Start from @bob_99 (30 s left)")
        self.assertEqual(tgweb.job_text(None, 0), "")


class WebPage(Base):
    """The page and its forms through the real request handler, on a loopback port."""

    def setUp(self):
        Base.setUp(self)
        saved = (dict(render.CFG["telegram"]), render.DEMO, tgweb.set_engine(None), dict(tgweb._BIND))
        render.DEMO = False
        self.srv = web.Server(("127.0.0.1", 0), dict(nuc_config.load("/nonexistent")["web"], refresh_seconds=2), "", demo=False)
        tgweb.set_engine(self.eng)  # the server made its own: this test's is the one behind the page
        self.cfg = render.CFG  # the page and the engine read one config
        render.CFG["telegram"]["web_actions"] = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

        def undo():
            self.srv.shutdown()
            self.srv.server_close()
            render.CFG["telegram"].clear()
            render.CFG["telegram"].update(saved[0])
            render.DEMO = saved[1]
            tgweb.set_engine(saved[2])
            tgweb._BIND.clear()
            tgweb._BIND.update(saved[3])
        self.addCleanup(undo)
        self.port = self.srv.server_address[1]

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, {k.title(): v for k, v in r.getheaders()}, data

    def post(self, action, fields=None, csrf=True, headers=None):
        data = dict(fields or {}, back="view=telegram")
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % self.port}
        h.update(headers or {})
        return self.request("POST", "/telegram/" + action, urlencode(data), {k: v for k, v in h.items() if v is not None})

    def page(self):
        st, h, body = self.request("GET", "/?view=telegram")
        self.assertEqual(st, 200)
        return h, body

    def test_the_page_has_the_form_with_the_token_of_this_process_and_may_post_here_only(self):
        h, body = self.page()
        self.assertIn('action="/telegram/pair"', body)
        self.assertIn('name="csrf" value="%s"' % self.srv.csrf, body)
        self.assertIn('type="password" name="token"', body)
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["Referrer-Policy"], "same-origin")

    def test_a_pairing_through_the_form_and_the_token_never_comes_back(self):
        st, h, _ = self.post("pair", {"token": TOKEN, "username": "@bob_99"})
        self.assertEqual((st, h["Location"]), (303, "/?view=telegram#tg"))
        self.assertTrue(self.eng.wait())
        self.notifier_takes_them()
        _h, body = self.page()
        self.assertIn("paired with @bob_99", body)
        self.assertNotIn(SECRET, body)
        self.assertEqual(notify.read_token(self.dir), TOKEN)

    def test_without_the_csrf_token_or_from_another_site_nothing_starts(self):
        for resp in (self.post("pair", {"token": TOKEN, "username": "bob_99"}, csrf=False),
                     self.post("pair", {"token": TOKEN, "username": "bob_99"}, csrf="x" * 32),
                     self.post("pair", {"token": TOKEN, "username": "bob_99"}, headers={"Origin": "http://evil.example"}),
                     self.post("test", headers={"Sec-Fetch-Site": "cross-site"})):
            self.assertEqual(resp[0], 403, resp[2])
        self.assertEqual((self.eng.job, self.tg.calls, self.inbox()), (None, [], []))

    def test_locked_the_post_is_403_and_the_page_has_no_form(self):
        render.CFG["telegram"]["web_actions"] = False
        st, _h, body = self.post("pair", {"token": TOKEN, "username": "bob_99"})
        self.assertEqual(st, 403)
        self.assertIn("web_actions = no", body)
        h, body = self.page()
        self.assertNotIn("<form", body)
        self.assertIn("form-action 'none'", h["Content-Security-Policy"])
        self.assertEqual((self.eng.job, self.inbox()), (None, []))

    def test_only_the_known_actions_and_never_a_get(self):
        self.assertEqual(self.post("dance")[0], 404)
        self.assertEqual(self.request("GET", "/telegram/pair")[0], 405)
        self.assertEqual(self.request("POST", "/telegram", "x", {"Content-Type": "application/x-www-form-urlencoded"})[0], 405)
        self.assertEqual(self.request("POST", "/telegram/on?x=1", "x", {"Content-Type": "application/x-www-form-urlencoded"})[0], 405)

    def test_open_redirects_to_the_link_that_waits_and_nowhere_else(self):
        st, h, _ = self.request("GET", "/?view=telegram&open=1")
        self.assertEqual((st, h["Location"]), (303, "/?view=telegram"))
        self.tg.hold = True
        self.post("pair", {"token": TOKEN, "username": "bob_99"})
        for _ in range(200):
            if self.eng.link():
                break
            threading.Event().wait(0.01)
        st, h, _ = self.request("GET", "/?view=telegram&open=1")
        self.assertEqual((st, h["Location"]), (302, self.eng.link()))
        st, h, _ = self.request("GET", "/?view=telegram&open=1", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 303)
        _h, body = self.page()
        self.assertIn("data-refresh", body)  # it waits: the page reloads by itself
        view = body[body.index('data-card="__view"'):body.index("</main>")]
        for href in re.findall(r'href="([^"]*)"', view):  # the refresh script keeps only links of this server
            self.assertTrue(href.startswith("/?") or href.startswith("#"), href)
        self.post("cancel")
        self.eng.wait()

    def test_the_settings_page_links_to_it(self):
        st, _h, body = self.request("GET", "/?view=settings")
        self.assertIn('href="/?view=telegram"', body)

    def test_the_settings_page_says_whether_the_alerts_reach_a_phone_and_leads_to_the_setup(self):
        _st, _h, body = self.request("GET", "/?view=settings")
        block = body[body.index('<section class="sec" id="alerts"'):]
        block = block[:block.index("</section>")]
        self.assertIn('<span class="pl d big">OFF</span>', block)
        self.assertIn('href="/?view=telegram#tg">Set up Telegram alerts</a>', block)
        for step in ("@BotFather", "@username", "Start", "send a test"):
            self.assertIn(step, block)
        self.notifier_alive(enabled=True, paired=True, username="bob_99")
        self.cfg["telegram"].update(enabled=True, username="bob_99")
        self.srv.cache.clear()
        _st, _h, body = self.request("GET", "/?view=settings")
        self.assertIn('<span class="pl g big">ON</span> on: the ATTENTION changes go to @bob_99', body)
        self.assertIn(">Telegram alerts</a>", body)

    def test_a_portable_run_the_desktop_app_pairs_and_tests_from_the_page_too(self):
        with mock.patch.object(nuc_config, "PORTABLE", os.path.dirname(self.dir)):
            h, body = self.page()
            self.assertIn('action="/telegram/pair"', body)
            self.assertIn("readable only by your account", body)
            self.assertNotIn("nuc-console-telegram", body, "no command of an installation")
            self.assertIn("press <b>Send a test</b>", body)
            st, _h, _ = self.post("pair", {"token": TOKEN, "username": "@bob_99"})
            self.assertEqual(st, 303)
            self.assertTrue(self.eng.wait())
            self.notifier_takes_them()
            self.assertEqual(notify.read_token(self.dir), TOKEN)
            self.clock.t += notify.STALE_S + 1  # the notifier is gone: the page says how to bring it back in a portable run
            _h, body = self.page()
            self.assertIn("quit nuc-console and start it again: the notifier starts with it", body)


if __name__ == "__main__":
    unittest.main()
