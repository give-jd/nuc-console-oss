"""Tests for notify.py, the Telegram notifier: no network, no real Telegram.

A fake transport records every call the code makes (method names included: the service must never read updates); the clock and
the sleeps are fake too. Tokens and chat ids are fake values built at runtime. The files live in a temporary folder.
"""
import ast
import contextlib
import io
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
_TMP = tempfile.mkdtemp(prefix="nuc-notify-test-")
os.environ["NUC_CONSOLE_CONFIG"] = os.path.join(_TMP, "config.ini")  # hermetic: never the host's config.ini or notifier folder
os.environ["NUC_CONSOLE_NOTIFY_DIR"] = os.path.join(_TMP, "notify")
import demo  # noqa: E402
import nuc_config  # noqa: E402
import notify  # noqa: E402
import render  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
REAL_SERVICE_CONTROL = notify.service_control  # Base replaces it in every test: this is the real one
SECRET = "Zq9" * 12  # the secret half of the fake token: it must never show up anywhere
TOKEN = "123456789:" + SECRET
CHAT_ID = 4242424242
POSIX = sys.platform != "win32"
CFG = {"enabled": True, "username": "alice", "detail": "titles", "resolved": True}
SEV = {3: "port-change", 2: "error", 1: "warning"}


def slurp(*path):
    with open(os.path.join(*path), encoding="utf-8") as f:
        return f.read()


def rec(pid, sev=1, text=None, accepted=False):
    """A problem record as render.problem_records() makes it."""
    title = render.CATALOG.get(pid, (pid,))[0]
    text = text or title
    return {"id": pid, "severity": SEV[sev], "text": text, "accepted": accepted, "reason": "", "title": title, "why": "",
            "fix": "", "fingerprint": render.fingerprint(sev, text, pid), "acceptable": pid not in render.NOT_ACCEPTABLE}


def port(n):
    return rec("port-new", 3, f"NEW exposed port: {n} lan (app{n})")


def temp(c, key="thermal", sev=1):
    return rec(key, sev, f"CPU at {c}°C: above the 72°C threshold")


def update(uid, text, user="alice", kind="private", chat_id=777, from_id=None, is_bot=False):
    """A getUpdates entry."""
    return {"update_id": uid, "message": {"message_id": uid, "date": 0, "text": text, "chat": {"id": chat_id, "type": kind},
                                          "from": {"id": chat_id if from_id is None else from_id, "is_bot": is_bot, "username": user}}}


class Clock:
    """Fake time: sleeping only moves the clock."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Fake:
    """transport(method, payload) -> reply. Records every call; `script` entries (replies or exceptions) are used first."""

    def __init__(self, script=()):
        self.calls, self.script = [], list(script)

    def __call__(self, method, payload):
        self.calls.append((method, json.loads(json.dumps(payload))))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return {"ok": True, "result": {"username": "my_bot"} if method == "getMe" else [] if method == "getUpdates" else {"message_id": 1}}

    @property
    def methods(self):
        return [m for m, _ in self.calls]

    @property
    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def down(status=None, text="connection failed", retry_after=None):
    return notify.TelegramError(text, status=status, retry_after=retry_after)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.cfg_path = os.path.join(tmp.name, "notify"), os.path.join(tmp.name, "config.ini")
        os.mkdir(self.dir, 0o700)
        self.config(enabled="yes", username="@alice")
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_CONFIG": self.cfg_path, "NUC_CONSOLE_NOTIFY_DIR": self.dir})
        env.start()
        self.addCleanup(env.stop)
        for what in (mock.patch.object(notify, "NOTIFY_DIR", self.dir), mock.patch.object(notify, "service_control", return_value=(True, ""))):
            what.start()
            self.addCleanup(what.stop)

    def config(self, **kw):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write("[telegram]\n" + "".join(f"{k} = {v}\n" for k, v in kw.items()))

    def pair_files(self, user="alice"):
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        notify.put(self.dir, "chat.json", json.dumps({"chat_id": CHAT_ID, "username": user, "bot": "my_bot", "paired_ts": 1}), 0o600)

    def notifier(self, script=(), clock=None):
        clock, fake = clock or Clock(), Fake(script)
        nt = notify.Notifier(fake, CHAT_ID, self.dir, host="myhost", clock=clock, sleep=clock.sleep, log=lambda text: None)
        return nt, fake, clock

    def run_cycles(self, nt, clock, *rounds, cfg=CFG):
        """One cycle per list of records, 30 s apart."""
        for records in rounds:
            nt.cycle(records, cfg)
            clock.t += notify.CYCLE_S

    def status(self):
        with open(os.path.join(self.dir, "status.json"), encoding="utf-8") as f:
            return json.load(f)


# ---- what is "the same problem" ---------------------------------------------------------------------------------------------

class Keys(unittest.TestCase):
    def test_port_changes_and_counted_problems_are_told_apart_by_their_text(self):
        self.assertNotEqual(notify.problem_key(port(8080)), notify.problem_key(port(9090)))
        self.assertEqual(notify.problem_key(port(8080)), notify.problem_key(port(8080)))
        a, b = rec("db-open-lan", 2, "1 DB/broker open on LAN"), rec("db-open-lan", 2, "2 DB/broker open on LAN")
        self.assertNotEqual(notify.problem_key(a), notify.problem_key(b))

    def test_numbers_that_change_every_cycle_are_not_a_change(self):
        self.assertEqual(notify.problem_key(temp(85)), notify.problem_key(temp(86)))
        j1, j2 = rec("journal-errors", 1, "118 errors in this boot's journal"), rec("journal-errors", 1, "120 errors in this boot's journal")
        self.assertEqual(notify.problem_key(j1), notify.problem_key(j2))
        t1 = rec("throttling", 1, "CPU thermal throttling: 3 events in the last minute")
        t2 = rec("throttling", 1, "CPU thermal throttling: 9 events in the last minute")
        self.assertEqual(notify.problem_key(t1), notify.problem_key(t2))

    def test_a_worse_severity_is_a_new_problem(self):
        self.assertNotEqual(notify.problem_key(temp(85, sev=1)), notify.problem_key(temp(95, sev=2)))

    def test_ids_repeat_so_the_state_is_a_multiset_and_accepted_ones_do_not_count(self):
        keys, recs = notify.collect([port(80), port(80), port(81), rec("ufw-off", 2), rec("journal-errors", 1, "5 errors", accepted=True)])
        self.assertEqual(keys[notify.problem_key(port(80))], 2)
        self.assertEqual(sum(keys.values()), 4)
        self.assertEqual(set(recs), set(keys))

    def test_the_problems_of_the_real_list_have_the_same_keys_cycle_after_cycle(self):
        def keys():
            cont, net, boot, base = demo.snapshot()
            return notify.collect(render.problem_records(net, cont, boot=boot, baseline=base))[0]
        first = keys()
        self.assertTrue(first)
        self.assertEqual(first, keys())


class Debouncing(unittest.TestCase):
    def test_a_problem_counts_when_seen_in_two_consecutive_cycles(self):
        d = notify.Debounce()
        self.assertEqual(d.update(Counter({"a": 1})), Counter())
        self.assertEqual(d.update(Counter({"a": 1})), Counter({"a": 1}))

    def test_it_is_resolved_when_absent_in_two_consecutive_cycles(self):
        d = notify.Debounce(Counter({"a": 1}))
        self.assertEqual(d.update(Counter()), Counter({"a": 1}))
        self.assertEqual(d.update(Counter()), Counter())

    def test_a_flapping_problem_never_counts(self):
        d = notify.Debounce()
        for n in (1, 0, 1, 0, 1, 0):
            self.assertEqual(d.update(Counter({"a": n})), Counter())

    def test_a_count_change_is_confirmed_like_the_rest(self):
        d = notify.Debounce(Counter({"a": 1}))
        self.assertEqual(d.update(Counter({"a": 2})), Counter({"a": 1}))
        self.assertEqual(d.update(Counter({"a": 2})), Counter({"a": 2}))

    def test_what_was_told_before_a_restart_is_still_standing(self):
        d = notify.Debounce(Counter({"a": 1}))
        self.assertEqual(d.update(Counter({"a": 1})), Counter({"a": 1}))

    def test_diff_is_a_multiset_difference(self):
        new, gone = notify.diff(Counter({"a": 2, "b": 1}), Counter({"a": 1, "c": 1}))
        self.assertEqual((new, gone), (Counter({"a": 1, "b": 1}), Counter({"c": 1})))


# ---- the text of a message ---------------------------------------------------------------------------------------------------

class Messages(unittest.TestCase):
    def items(self, *records):
        keys, recs = notify.collect(records)
        return notify.pick(keys, recs)

    def test_titles_only_by_default_no_names_no_ports(self):
        text = notify.format_message("myhost", self.items(port(8080), rec("db-open-lan", 2, "1 DB/broker open on LAN (shop-db-1)")))
        self.assertEqual(text.splitlines()[0], "nuc-console · myhost")
        self.assertIn("NEW  ‼ New exposed port", text)
        self.assertIn("NEW  ‼ Database/broker open on the LAN", text)
        for secret in ("8080", "app8080", "shop-db-1", "lan ("):
            self.assertNotIn(secret, text)

    def test_full_adds_the_text_after_the_title(self):
        text = notify.format_message("myhost", self.items(port(8080)), detail="full")
        self.assertIn("NEW  ‼ New exposed port: NEW exposed port: 8080 lan (app8080)", text)

    def test_the_worst_comes_first_and_a_warning_has_no_marker(self):
        lines = notify.format_message("h", self.items(rec("docker-bypass", 1), rec("ufw-off", 2), port(1))).splitlines()[1:]
        self.assertEqual(lines, ["NEW  ‼ New exposed port", "NEW  ‼ ufw is off", "NEW  Docker ports bypass ufw"])

    def test_repeated_lines_are_counted(self):
        text = notify.format_message("h", self.items(port(1), port(2), port(3)))
        self.assertEqual(text.splitlines()[1:], ["NEW  ‼ New exposed port (x3)"])

    def test_resolved_lines(self):
        gone = notify.pick(Counter({"ufw-off|x": 1, "db-open-lan|1 DB/broker open on LAN": 1}), {})  # after a restart: only the keys are left
        text = notify.format_message("h", [], gone, detail="full")
        self.assertEqual(text.splitlines()[1:], ["OK   Database/broker open on the LAN: 1 DB/broker open on LAN", "OK   ufw is off"])
        # a resolved problem is not an alarm any more: no marker, even when its record is still known
        keys, recs = notify.collect([rec("ufw-off", 2)])
        self.assertEqual(notify.format_message("h", [], notify.pick(keys, recs)).splitlines()[1:], ["OK   ufw is off"])

    def test_a_problem_whose_count_changed_is_one_line_not_a_new_and_a_resolved_one(self):
        old = [("db-open-lan|1 DB/broker open on LAN", None)]
        text = notify.format_message("h", self.items(rec("db-open-lan", 2, "2 DB/broker open on LAN")), old, detail="full")
        self.assertEqual(text.splitlines()[1:], ["CHANGED ‼ Database/broker open on the LAN: 2 DB/broker open on LAN"])

    def test_first_run_summary(self):
        self.assertEqual(notify.format_message("h", self.items(), started=0).splitlines()[1], "Monitoring started: no open problems")
        text = notify.format_message("h", self.items(rec("ufw-off", 2), rec("docker-bypass")), started=2)
        self.assertEqual(text.splitlines()[1:], ["Monitoring started: 2 open problems", "OPEN ‼ ufw is off", "OPEN Docker ports bypass ufw"])
        self.assertIn("1 open problem", notify.format_message("h", self.items(rec("ufw-off")), started=1))

    def test_hostname_and_texts_are_one_clean_line(self):
        evil = "ho\x1b[31mst\nname‮​"
        self.assertEqual(notify.clean(evil), "ho [31mst name")
        with mock.patch.object(socket, "gethostname", return_value=evil):
            self.assertNotIn("\x1b", notify.hostname())
        text = notify.format_message("h", self.items(rec("port-new", 3, "NEW\x07 exposed\r\nport:\x9b 1‮")), detail="full")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertFalse(notify.TEXT_CHARS.search(text.replace("\n", "")))

    def test_every_control_character_render_strips_is_stripped_too(self):
        for code in list(range(0, 32)) + list(range(127, 160)):
            self.assertNotIn(chr(code), notify.clean("a" + chr(code) + "b"))

    def test_long_lists_and_long_texts_are_cut_to_fit(self):
        many = self.items(*[rec("port-new", 3, f"NEW exposed port: {n}") for n in range(100)])
        text = notify.format_message("h", many, detail="full")
        self.assertEqual(len(text.splitlines()), 1 + notify.MAX_LINES + 1)
        self.assertTrue(text.endswith("... and 70 more"))
        wide = self.items(*[rec("port-new", 3, f"NEW exposed port: {n} " + "x" * 400) for n in range(100)])
        text = notify.format_message("h", wide, detail="full")  # 300 characters a line at most, 4000 in all
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 340)
        self.assertLessEqual(len(text), notify.MAX_TEXT)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(notify.message_payload(1, "a" * 9000)["text"]), 4096)

    def test_payload_is_plain_text_without_a_preview(self):
        p = notify.message_payload(CHAT_ID, "line 1\nline\x1b2")
        self.assertEqual(p, {"chat_id": CHAT_ID, "text": "line 1\nline 2", "disable_web_page_preview": True})
        self.assertNotIn("parse_mode", p)


# ---- the loop's decisions ----------------------------------------------------------------------------------------------------

class Cycles(Base):
    def test_first_run_sends_one_summary_not_a_line_per_problem(self):
        nt, fake, clock = self.notifier()
        probs = [rec("ufw-off", 2), port(1), port(2)]
        self.run_cycles(nt, clock, probs)
        self.assertEqual(fake.calls, [])  # one cycle: nothing is confirmed yet
        self.run_cycles(nt, clock, probs)
        self.assertEqual(len(fake.texts), 1)
        self.assertEqual(fake.texts[0].splitlines()[1], "Monitoring started: 3 open problems")
        self.assertNotIn("NEW", fake.texts[0])
        self.run_cycles(nt, clock, probs, probs, probs)
        self.assertEqual(len(fake.texts), 1)

    def test_first_run_with_nothing_wrong_says_so(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])
        self.assertEqual(fake.texts[0].splitlines()[1], "Monitoring started: no open problems")

    def test_a_new_problem_is_told_once_after_two_cycles_and_its_end_too(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])
        n = len(fake.texts)
        self.run_cycles(nt, clock, [rec("ufw-off", 2)])
        self.assertEqual(len(fake.texts), n)  # seen once: not yet
        self.run_cycles(nt, clock, [rec("ufw-off", 2)], [rec("ufw-off", 2)], [rec("ufw-off", 2)])
        self.assertEqual(fake.texts[n:], ["nuc-console · myhost\nNEW  ‼ ufw is off"])
        self.run_cycles(nt, clock, [], [], [])
        self.assertEqual(fake.texts[n + 1:], ["nuc-console · myhost\nOK   ufw is off"])
        self.assertEqual(len(fake.texts), n + 2)

    def test_resolved_no_keeps_the_ok_lines_out(self):
        nt, fake, clock = self.notifier()
        cfg = dict(CFG, resolved=False)
        self.run_cycles(nt, clock, [rec("ufw-off", 2)], [rec("ufw-off", 2)], [], [], [], cfg=cfg)
        self.assertEqual(len(fake.texts), 1)  # the summary only
        self.assertEqual(nt.sent, Counter())  # and the gone problem is forgotten: it will be news if it comes back
        self.run_cycles(nt, clock, [rec("ufw-off", 2)], [rec("ufw-off", 2)], cfg=cfg)
        self.assertEqual(len(fake.texts), 2)

    def test_numbers_that_change_every_cycle_send_nothing(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [temp(80)], [temp(81)])
        self.assertEqual(len(fake.texts), 1)  # the first run's summary
        # a journal problem appears (news, once) while temperatures and error counts move all the time
        self.run_cycles(nt, clock, *[[temp(70 + i), rec("journal-errors", 1, f"{100 + i} errors in this boot's journal")] for i in range(12)])
        self.assertEqual(len(fake.texts), 2)
        self.assertEqual(fake.texts[1].splitlines()[1:], ["NEW  " + render.CATALOG["journal-errors"][0]])   # the title of this OS

    def test_the_notifiers_own_problems_are_never_announced(self):
        nt, fake, clock = self.notifier()
        own = [rec("telegram-failing", 2, "Telegram messages fail since 12:00"), rec("telegram-unpaired", 1, "Telegram is on but not paired")]
        self.run_cycles(nt, clock, own, own)
        self.assertEqual(fake.texts[0].splitlines()[1], "Monitoring started: no open problems")
        self.run_cycles(nt, clock, own + [rec("ufw-off", 2)], own + [rec("ufw-off", 2)], own, own)
        self.assertEqual(fake.texts[1:], ["nuc-console · myhost\nNEW  ‼ ufw is off", "nuc-console · myhost\nOK   ufw is off"])
        for text in fake.texts:
            self.assertNotIn("Telegram", text)
        self.assertEqual(notify.collect(own), (Counter(), {}))

    def test_a_flap_of_one_cycle_sends_nothing(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [], [rec("ufw-off", 2)], [], [], [])
        self.assertEqual(len(fake.texts), 1)

    def test_several_changes_make_one_message(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [rec("ufw-off", 2)], [rec("ufw-off", 2)])
        probs = [rec("docker-bypass"), port(1)]
        self.run_cycles(nt, clock, probs, probs, probs)
        self.assertEqual(len(fake.texts), 2)
        self.assertEqual(fake.texts[1].splitlines()[1:], ["NEW  ‼ New exposed port", "NEW  Docker ports bypass ufw", "OK   ufw is off"])

    def test_what_was_told_survives_a_restart(self):
        nt, fake, clock = self.notifier()
        probs = [rec("ufw-off", 2), port(1)]
        self.run_cycles(nt, clock, probs, probs)
        keys = notify.load_sent(self.dir)["keys"]
        self.assertEqual(keys, notify.collect(probs)[0])
        nt2, fake2, clock2 = self.notifier(clock=clock)  # the service restarted
        self.run_cycles(nt2, clock2, probs, probs, probs)
        self.assertEqual(fake2.calls, [])  # nothing again
        self.run_cycles(nt2, clock2, probs + [rec("docker-bypass")], probs + [rec("docker-bypass")])
        self.assertEqual(fake2.texts, ["nuc-console · myhost\nNEW  Docker ports bypass ufw"])
        nt3, fake3, clock3 = self.notifier(clock=clock)  # and again: only what changed after the last message
        self.run_cycles(nt3, clock3, probs, probs)
        self.assertEqual(fake3.texts, ["nuc-console · myhost\nOK   Docker ports bypass ufw"])

    def test_a_missing_or_broken_sent_file_means_a_first_run(self):
        with open(os.path.join(self.dir, "sent.json"), "w") as f:
            f.write("{not json")
        os.chmod(os.path.join(self.dir, "sent.json"), 0o600)
        self.assertIsNone(notify.load_sent(self.dir)["keys"])
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])
        self.assertIn("Monitoring started", fake.texts[0])
        self.assertEqual(notify.load_sent(self.dir)["keys"], Counter())

    def test_sent_file_is_private(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])
        if POSIX:
            self.assertEqual(os.stat(os.path.join(self.dir, "sent.json")).st_mode & 0o777, 0o600)

    def test_at_most_20_messages_an_hour_the_rest_goes_out_together(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])  # message 1
        probs = []
        for n in range(24):  # a new problem every minute
            probs = probs + [port(8000 + n)]
            self.run_cycles(nt, clock, probs, probs)
        self.assertEqual(len(fake.texts), 20)
        self.assertTrue(nt.blocked)
        self.assertEqual(Counter(m for m in fake.methods), Counter(sendMessage=20))
        clock.t += 3600  # the hour is over
        self.run_cycles(nt, clock, probs)
        self.assertEqual(len(fake.texts), 21)
        last = fake.texts[-1]
        self.assertIn("held back by the limit of 20 messages an hour", last)
        self.assertIn("New exposed port (x5)", last)  # 24 problems, 19 of them were told one by one
        self.assertFalse(nt.blocked)
        self.run_cycles(nt, clock, probs, probs)
        self.assertEqual(len(fake.texts), 21)

    def test_rate_room(self):
        self.assertTrue(notify.rate_room([0.0] * 19, 10.0))
        self.assertFalse(notify.rate_room([0.0] * 20, 10.0))
        self.assertTrue(notify.rate_room([0.0] * 20, 3600.0))

    def test_a_failed_send_is_retried_later_and_nothing_is_lost_or_doubled(self):
        nt, fake, clock = self.notifier(script=[down(502, "HTTP 502: Bad Gateway")] * 3)
        self.run_cycles(nt, clock, [], [])
        self.assertEqual(fake.methods, ["sendMessage"] * 3)  # one cycle: three attempts, with pauses
        self.assertIsNotNone(nt.failing_since)
        self.assertIsNone(nt.sent)  # still the first run: the summary was not delivered
        self.assertEqual(nt.error, "HTTP 502: Bad Gateway")
        self.run_cycles(nt, clock, [])  # backing off: no new attempt yet
        self.assertEqual(len(fake.calls), 3)
        clock.t += 300
        self.run_cycles(nt, clock, [])
        self.assertEqual(len(fake.texts), 4)  # the 4th attempt got through
        self.assertEqual((nt.error, nt.failing_since), (None, None))
        self.assertIn("Monitoring started", fake.texts[-1])
        self.run_cycles(nt, clock, [], [], [])
        self.assertEqual(len(fake.texts), 4)

    def test_a_refusal_is_not_retried_within_the_cycle(self):
        nt, fake, clock = self.notifier(script=[down(403, "HTTP 403: Forbidden: bot was blocked by the user")])
        self.run_cycles(nt, clock, [], [])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(nt.error, "HTTP 403: Forbidden: bot was blocked by the user")

    def test_while_failing_with_nothing_to_say_it_asks_who_the_bot_is_until_it_answers(self):
        nt, fake, clock = self.notifier()
        self.run_cycles(nt, clock, [], [])
        self.assertEqual(len(fake.texts), 1)
        nt.fail(down(None, "URLError: no route"), clock())
        clock.t += 120
        fake.script = [down(None, "URLError: still no route")]
        self.run_cycles(nt, clock, [])
        self.assertEqual(fake.methods[-1], "getMe")
        self.assertIsNotNone(nt.failing_since)
        clock.t += 600
        self.run_cycles(nt, clock, [])
        self.assertEqual(fake.methods[-2:], ["getMe", "getMe"])
        self.assertEqual((nt.error, nt.failing_since), (None, None))
        self.assertEqual(len(fake.texts), 1)

    def test_retries_pause_as_telegram_asks(self):
        fake, clock = Fake([down(429, "HTTP 429: Too Many Requests", retry_after=7)]), Clock()
        self.assertEqual(notify.call(fake, "sendMessage", {}, clock.sleep), {"message_id": 1})
        self.assertEqual((len(fake.calls), clock.t - 1_000_000.0), (2, 7))
        fake = Fake([down(None), down(None), down(None), down(None)])
        with self.assertRaises(notify.TelegramError):
            notify.call(fake, "sendMessage", {}, Clock().sleep)
        self.assertEqual(len(fake.calls), notify.ATTEMPTS)
        fake = Fake([{"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}])
        with self.assertRaises(notify.TelegramError) as ctx:
            notify.call(fake, "sendMessage", {}, Clock().sleep)
        self.assertEqual((ctx.exception.status, len(fake.calls)), (400, 1))

    def test_the_error_kept_for_the_dashboard_has_no_token_and_no_chat_id(self):
        nt, fake, clock = self.notifier(script=[down(400, f"HTTP 400: chat {CHAT_ID} not found on https://api.telegram.org/bot{TOKEN}/sendMessage")])
        self.run_cycles(nt, clock, [], [])
        for secret in (SECRET, TOKEN, str(CHAT_ID)):
            self.assertNotIn(secret, nt.error)


# ---- the service -------------------------------------------------------------------------------------------------------------

class Service(Base):
    def serve(self, rounds, fake=None, cycles=None, clock=None):
        clock, fake = clock or Clock(), fake or Fake()
        seq = iter(rounds)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = notify.serve(self.dir, clock=clock, sleep=clock.sleep, records=lambda: next(seq), transport=fake, cycles=cycles or len(rounds))
        return rc, fake, out.getvalue()

    def test_the_service_never_reads_updates(self):
        self.pair_files()
        probs = [[], [], [rec("ufw-off", 2)], [rec("ufw-off", 2)], [], [], [port(1)], [port(1)]]
        rc, fake, _ = self.serve(probs, fake=Fake([down(500), down(500), down(500)]))
        self.assertEqual(rc, 0)
        self.assertTrue(fake.calls)
        self.assertLessEqual(set(fake.methods), {"sendMessage", "getMe"})
        self.assertNotIn("getUpdates", fake.methods)

    def test_its_transport_cannot_even_ask_for_updates(self):
        inner = Fake()
        send_only = notify.SendOnly(inner)
        with self.assertRaises(ValueError):
            send_only("getUpdates", {})
        self.assertEqual(inner.calls, [])
        for method in ("sendMessage", "getMe"):
            send_only(method, {})
        self.assertEqual(inner.methods, ["sendMessage", "getMe"])

    def test_getupdates_exists_only_in_the_pairing_code(self):
        tree = ast.parse(slurp(ROOT, "src", "notify.py"))
        users = set()
        for node in tree.body:
            if any(isinstance(n, ast.Constant) and n.value == "getUpdates" for n in ast.walk(node)):
                users.add(node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else node.targets[0].id)
        self.assertEqual(users, {"pair", "METHODS"})  # the list of the methods the transport knows, and the pairing

    def test_sending_goes_to_the_paired_chat_in_plain_text(self):
        self.pair_files()
        rc, fake, out = self.serve([[], []])
        (method, payload), = fake.calls
        self.assertEqual((method, payload["chat_id"], payload["disable_web_page_preview"]), ("sendMessage", CHAT_ID, True))
        self.assertNotIn("parse_mode", payload)
        self.assertNotIn(SECRET, out)

    def test_off_means_exit_0_at_once_without_any_network(self):
        self.config(enabled="no", username="@alice")
        self.pair_files()
        with mock.patch.object(notify, "HttpsTransport", side_effect=AssertionError("no network when off")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(notify.main(["notify.py"]), 0)
        self.assertIs(self.status()["enabled"], False)
        self.assertIn("off", out.getvalue())

    def test_not_paired_means_exit_0_and_says_why_in_the_status(self):
        for setup in (lambda: None, lambda: notify.put(self.dir, "token", TOKEN + "\n", 0o600)):
            setup()
            with mock.patch.object(notify, "HttpsTransport", side_effect=AssertionError("no network without a pairing")):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(notify.serve(self.dir, records=lambda: [], cycles=1), 0)
            st = self.status()
            self.assertEqual((st["enabled"], st["paired"]), (True, False))
            self.assertIn("--setup", st["last_error"])

    def test_a_username_other_than_the_paired_one_is_not_sent_to(self):
        self.pair_files(user="mallory")
        with mock.patch.object(notify, "HttpsTransport", side_effect=AssertionError("must not send")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(notify.serve(self.dir, cycles=1), 0)
        self.assertIs(self.status()["paired"], False)

    def test_status_json_has_the_contract_fields_and_no_secret(self):
        self.pair_files()
        err = down(401, f"HTTP 401: Unauthorized (bot{TOKEN}, chat {CHAT_ID})")
        rc, fake, out = self.serve([[], [], [rec("ufw-off", 2)], [rec("ufw-off", 2)]], fake=Fake([{"ok": True, "result": {}}, err, err, err]))
        raw = slurp(self.dir, "status.json")
        for secret in (SECRET, TOKEN, str(CHAT_ID)):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, out)
        st = json.loads(raw)
        self.assertEqual(set(st), {"ts", "enabled", "paired", "username", "last_sent_ts", "last_error", "failing_since"})
        self.assertEqual((st["enabled"], st["paired"], st["username"]), (True, True, "alice"))
        self.assertIsInstance(st["last_sent_ts"], int)
        self.assertIn("HTTP 401", st["last_error"])
        self.assertIsInstance(st["failing_since"], int)
        if POSIX:
            self.assertEqual(os.stat(os.path.join(self.dir, "status.json")).st_mode & 0o777, 0o644)

    def test_status_ts_moves_every_cycle(self):
        self.pair_files()
        clock = Clock()
        self.serve([[]], clock=clock)
        first = self.status()["ts"]
        clock.t += 5
        self.serve([[]], clock=clock)
        self.assertEqual(self.status()["ts"], first + 5)

    def test_an_error_in_the_problem_list_is_a_status_line_not_a_crash(self):
        self.pair_files()
        calls = []

        def records():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError(f"state file broken near bot{TOKEN}")
            return []
        clock, fake, out = Clock(), Fake(), io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(notify.serve(self.dir, clock=clock, sleep=clock.sleep, records=records, transport=fake, cycles=2), 0)
        self.assertIn("cannot list the problems", self.status()["last_error"])
        self.assertNotIn(SECRET, self.status()["last_error"])
        with contextlib.redirect_stdout(out):
            notify.serve(self.dir, clock=clock, sleep=clock.sleep, records=lambda: [], transport=fake, cycles=1)
        self.assertIsNone(self.status()["last_error"])

    def test_a_bug_while_deciding_is_a_status_line_too(self):
        self.pair_files()
        clock, fake, out = Clock(), Fake(), io.StringIO()
        with mock.patch.object(notify.Notifier, "cycle", side_effect=KeyError(f"bot{TOKEN}")), contextlib.redirect_stdout(out):
            rc = notify.serve(self.dir, clock=clock, sleep=clock.sleep, records=lambda: [], transport=fake, cycles=3)
        self.assertEqual(rc, 0)
        self.assertIn("internal error: KeyError", self.status()["last_error"])
        self.assertNotIn(SECRET, self.status()["last_error"])

    def test_switching_off_in_the_config_ends_the_service_with_exit_0(self):
        self.pair_files()
        calls = []

        def records():
            calls.append(1)
            if len(calls) == 3:
                self.config(enabled="no", username="@alice")
            return []
        clock = Clock()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = notify.serve(self.dir, clock=clock, sleep=clock.sleep, records=records, transport=Fake(), cycles=10)
        self.assertEqual((rc, len(calls)), (0, 3))
        self.assertIs(self.status()["enabled"], False)

    def test_enabled_exit_codes(self):
        self.assertEqual(notify.main(["notify.py", "--enabled"]), 0)
        self.config(enabled="no", username="@alice")
        self.assertEqual(notify.main(["notify.py", "--enabled"]), 1)
        self.config()
        self.assertEqual(notify.main(["notify.py", "--enabled"]), 1)

    def test_the_throttling_history_is_kept_here_without_render_sampler(self):
        reads, mono = iter([{"throttle": 10}, {"throttle": 10}, {"throttle": 14}, {"throttle": 14}]), iter([0, 30, 60, 90])
        hist = notify.ThermalHistory(read=lambda: dict(next(reads)), mono=lambda: next(mono))
        self.assertEqual([hist().get("recent") for _ in range(4)], [None, 0, 4, 4])
        with mock.patch.object(render.Sampler, "sample", side_effect=AssertionError("Sampler starts background threads")):
            with mock.patch.object(render, "snapshot", return_value={"net": None, "cont": None, "boot": None, "baseline": None}):
                recs = notify.current_records(lambda: {})
        self.assertTrue(recs)  # the collectors are not running: that is a problem too


# ---- the connection ----------------------------------------------------------------------------------------------------------

class Opener:
    """What the transport opens URLs with, faked: records the request, answers or raises."""

    def __init__(self, result=None, error=None):
        self.requests, self.result, self.error = [], result, error

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.result if self.result is not None else b'{"ok": true, "result": {"message_id": 5}}')


class Connection(unittest.TestCase):
    URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    def test_only_https_to_the_telegram_host_with_a_json_post(self):
        op = Opener()
        reply = notify.HttpsTransport(TOKEN, opener=op)("sendMessage", {"chat_id": 1, "text": "hi", "disable_web_page_preview": True})
        self.assertEqual(reply, {"ok": True, "result": {"message_id": 5}})
        (req, timeout), = op.requests
        self.assertEqual((req.full_url, req.get_method(), timeout), (self.URL, "POST", notify.TIMEOUT_S))
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data), {"chat_id": 1, "text": "hi", "disable_web_page_preview": True})
        self.assertEqual(notify.API_HOST, "api.telegram.org")

    def test_long_polling_waits_longer_than_the_plain_timeout(self):
        op = Opener(b'{"ok": true, "result": []}')
        notify.HttpsTransport(TOKEN, opener=op)("getUpdates", {"timeout": 25})
        self.assertEqual(op.requests[0][1], 25 + notify.TIMEOUT_S)

    def test_other_methods_do_not_exist(self):
        op = Opener()
        with self.assertRaises(ValueError):
            notify.HttpsTransport(TOKEN, opener=op)("setWebhook", {"url": "https://example.com"})
        self.assertEqual(op.requests, [])

    def test_the_token_never_comes_out_of_an_error(self):
        errors = [urllib.error.URLError(f"cannot reach {self.URL}"), OSError(f"[Errno 111] {self.URL}"), ValueError(f"bad url {self.URL}"),
                  ssl.SSLError(f"handshake with bot{TOKEN} failed"), RuntimeError(TOKEN),
                  urllib.error.HTTPError(self.URL, 401, "Unauthorized", {}, io.BytesIO(json.dumps(
                      {"ok": False, "error_code": 401, "description": f"Unauthorized {self.URL}"}).encode())),
                  urllib.error.HTTPError(self.URL, 502, f"Bad Gateway {TOKEN}", {}, io.BytesIO(b"<html>not json " + TOKEN.encode() + b"</html>"))]
        for err in errors:
            with self.assertRaises(notify.TelegramError) as ctx:
                notify.HttpsTransport(TOKEN, opener=Opener(error=err))("sendMessage", {"chat_id": 1, "text": "x"})
            shown = "\n".join((str(ctx.exception), repr(ctx.exception), repr(ctx.exception.args)))
            self.assertNotIn(SECRET, shown, repr(err))
            self.assertNotIn("123456789", shown, repr(err))
            self.assertIsNone(ctx.exception.__cause__)
            self.assertTrue(ctx.exception.__suppress_context__)
        self.assertNotIn(SECRET, repr(notify.HttpsTransport(TOKEN, opener=Opener())))

    def test_http_errors_carry_their_status_and_telegrams_explanation(self):
        body = json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7", "parameters": {"retry_after": 7}})
        err = urllib.error.HTTPError(self.URL, 429, "Too Many Requests", {}, io.BytesIO(body.encode()))
        with self.assertRaises(notify.TelegramError) as ctx:
            notify.HttpsTransport(TOKEN, opener=Opener(error=err))("sendMessage", {})
        e = ctx.exception
        self.assertEqual((e.status, e.retry_after, e.retryable), (429, 7, True))
        self.assertEqual(str(e), "HTTP 429: Too Many Requests: retry after 7")
        err = urllib.error.HTTPError(self.URL, 403, "Forbidden", {}, io.BytesIO(b"{}"))
        with self.assertRaises(notify.TelegramError) as ctx:
            notify.HttpsTransport(TOKEN, opener=Opener(error=err))("sendMessage", {})
        self.assertEqual((ctx.exception.status, ctx.exception.retryable, str(ctx.exception)), (403, False, "HTTP 403: Forbidden"))

    def test_a_reply_that_is_not_json_is_an_error(self):
        for body in (b"<html>", b"[1, 2]", b"\xff\xfe"):
            with self.assertRaises(notify.TelegramError):
                notify.HttpsTransport(TOKEN, opener=Opener(body))("getMe", {})

    def test_redact(self):
        text = f"a bot{TOKEN} b {TOKEN} c {CHAT_ID} d bot99999:abc_DEF-ghi e"
        out = notify.redact(text, TOKEN, CHAT_ID)
        for secret in (SECRET, TOKEN, str(CHAT_ID), "abc_DEF"):
            self.assertNotIn(secret, out)
        self.assertIn("bot<redacted>", notify.redact(f"https://api.telegram.org/bot{TOKEN}/getMe"))
        self.assertEqual(notify.redact("nothing to hide: 42"), "nothing to hide: 42")

    def test_the_opener_has_no_proxy_no_redirect_and_no_other_scheme(self):
        handlers = notify.make_opener(notify.tls_context()).handlers
        names = {type(h).__name__ for h in handlers}
        self.assertEqual(names, {"HTTPSHandler", "HTTPDefaultErrorHandler", "HTTPErrorProcessor"})

    def test_the_certificate_and_the_host_name_are_checked(self):
        ctx = notify.tls_context()
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_the_source_has_no_listening_socket_no_webhook_no_other_host(self):
        src = slurp(ROOT, "src", "notify.py")
        for word in (".listen(", ".bind(", "http.server", "socketserver", "setWebhook", "http://", "ProxyHandler", "shell=True"):
            self.assertNotIn(word, src)
        self.assertEqual(sorted(set(re.findall(r"https://([^/\s\"'{]+|\{API_HOST\})", src))), ["t.me", "{API_HOST}"])

    def test_the_source_is_python_3_8(self):
        ast.parse(slurp(ROOT, "src", "notify.py"), feature_version=(3, 8))


# ---- the token and the files that hold it ------------------------------------------------------------------------------------

class Token(Base):
    def test_format(self):
        ok = ["123456789:" + "A" * 35, "12345:" + "a_-" * 10, "1234567890:" + "Z" * 30]
        bad = ["", "123:" + "A" * 35, "123456789:" + "A" * 29, "123456789:" + "A" * 34 + "!", "x23456789:" + "A" * 35, "123456789" + "A" * 35,
               " 123456789:" + "A" * 35, "123456789:" + "A" * 35 + "\n", "bot123456789:" + "A" * 35]
        for t in ok:
            self.assertTrue(notify.TOKEN_RE.fullmatch(t), t)
        for t in bad:
            self.assertFalse(notify.TOKEN_RE.fullmatch(t), repr(t))

    def test_the_username_format(self):
        for ok in ("alice", "Alice_99", "a" * 32):
            self.assertTrue(notify.USER_RE.fullmatch(ok))
        for bad in ("", "abcd", "a" * 33, "@alice", "ali ce", "alicé", "ali-ce", "alice\n"):
            self.assertFalse(notify.USER_RE.fullmatch(bad), repr(bad))

    def test_reads_a_good_token_file(self):
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        self.assertEqual(notify.read_token(self.dir), TOKEN)

    def test_a_file_that_is_not_a_token_is_refused_without_quoting_it(self):
        notify.put(self.dir, "token", "hunter2-" + SECRET + "\n", 0o600)
        with self.assertRaises(ValueError) as ctx:
            notify.read_token(self.dir)
        self.assertNotIn(SECRET, str(ctx.exception))

    @unittest.skipUnless(POSIX, "permission bits")
    def test_group_and_other_permissions_are_refused(self):
        path = os.path.join(self.dir, "token")
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        for mode in (0o640, 0o604, 0o644, 0o660, 0o666, 0o601):
            os.chmod(path, mode)
            with self.assertRaises(ValueError, msg=oct(mode)):
                notify.read_token(self.dir)
        os.chmod(path, 0o400)
        self.assertEqual(notify.read_token(self.dir), TOKEN)

    @unittest.skipUnless(POSIX, "permission bits")
    def test_a_folder_others_can_write_in_is_refused(self):
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        os.chmod(self.dir, 0o777)
        with self.assertRaises(ValueError):
            notify.read_token(self.dir)
        os.chmod(self.dir, 0o711)
        self.assertEqual(notify.read_token(self.dir), TOKEN)

    @unittest.skipUnless(POSIX, "owners")
    def test_a_file_of_a_stranger_is_refused(self):
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        try:
            os.chown(os.path.join(self.dir, "token"), os.geteuid() + 4242, -1)
        except PermissionError:
            self.skipTest("cannot give a file to another user (not root)")
        with self.assertRaises(ValueError):
            notify.read_token(self.dir)

    @unittest.skipUnless(POSIX, "links")
    def test_a_link_is_not_followed(self):
        real = os.path.join(self.dir, "elsewhere")
        notify.put(self.dir, "elsewhere", TOKEN + "\n", 0o600)
        os.symlink(real, os.path.join(self.dir, "token"))
        with self.assertRaises(OSError):
            notify.read_token(self.dir)

    @unittest.skipUnless(POSIX, "links")
    def test_writing_does_not_follow_a_link_planted_at_the_temporary_name(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.symlink(victim, os.path.join(self.dir, "token.tmp"))
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        self.assertEqual(slurp(victim), "keep")
        self.assertEqual(notify.read_token(self.dir), TOKEN)

    def test_credentials_problems_are_sentences_without_secrets(self):
        self.assertIn("--setup", notify.load_credentials(self.dir, "alice")[2])
        notify.put(self.dir, "token", TOKEN + "\n", 0o600)
        self.assertIn("not paired", notify.load_credentials(self.dir, "alice")[2])
        self.pair_files()
        token, chat, problem = notify.load_credentials(self.dir, "alice")
        self.assertEqual((token, chat["chat_id"], problem), (TOKEN, CHAT_ID, None))
        self.assertIn("not the paired one", notify.load_credentials(self.dir, "bob")[2])
        self.assertIn("empty", notify.load_credentials(self.dir, "")[2])
        notify.put(self.dir, "chat.json", json.dumps({"chat_id": "1", "username": "alice"}), 0o600)
        self.assertIn("not valid", notify.load_credentials(self.dir, "alice")[2])

    def test_atomic_writes_leave_no_temporary_file_and_replace(self):
        notify.put(self.dir, "x.json", "one", 0o600)
        notify.put(self.dir, "x.json", "two", 0o600)
        self.assertEqual(sorted(os.listdir(self.dir)), ["x.json"])
        self.assertEqual(slurp(self.dir, "x.json"), "two")
        if POSIX:
            self.assertEqual(os.stat(os.path.join(self.dir, "x.json")).st_mode & 0o777, 0o600)


# ---- pairing -----------------------------------------------------------------------------------------------------------------

class Pairing(unittest.TestCase):
    CODE = "Kq8_-Zx1"

    def polls(self, *batches):
        """A transport that answers the getUpdates calls with these batches, then with nothing; every call moves the clock."""
        clock, queue = Clock(), list(batches)

        class Poll(Fake):
            def __call__(self, method, payload):
                Fake.__call__(self, method, payload)  # records it
                clock.t += payload.get("timeout", 0)
                return {"ok": True, "result": queue.pop(0) if queue else []}
        return Poll(), clock

    def test_only_the_right_code_from_the_right_person_in_a_private_chat_pairs(self):
        wrong = [update(1, "/start " + self.CODE + "x"), update(2, "/start"), update(3, "hello"), update(4, "/start " + self.CODE, user="mallory"),
                 update(5, "/start " + self.CODE, user=None), update(6, "/start " + self.CODE, kind="group", chat_id=-1001, from_id=777),
                 update(7, "/start " + self.CODE, kind="supergroup", chat_id=-1002, from_id=777), update(8, "/start " + self.CODE, kind="channel"),
                 update(9, "/start " + self.CODE, is_bot=True), update(10, "/start  " + self.CODE), update(11, " /start " + self.CODE),
                 update(12, "/start " + self.CODE, from_id=778), {"update_id": 13, "edited_message": {}}, {"update_id": 14}, "junk", None]
        fake, clock = self.polls(wrong, [update(20, "/start " + self.CODE, user="Alice", chat_id=777)])
        self.assertEqual(notify.pair(fake, self.CODE, "alice", clock, clock.sleep), {"chat_id": 777, "username": "alice"})

    def test_everything_else_is_ignored_until_the_time_is_up(self):
        fake, clock = self.polls([update(1, "/start " + self.CODE, user="mallory")], [update(2, "/start nope")])
        self.assertIsNone(notify.pair(fake, self.CODE, "alice", clock, clock.sleep, wait=100))
        self.assertGreaterEqual(clock.t - 1_000_000.0, 100)

    def test_the_offset_advances_and_the_consumed_updates_are_confirmed(self):
        fake, clock = self.polls([update(5, "x"), update(7, "y")], [update(9, "/start " + self.CODE)])
        self.assertIsNotNone(notify.pair(fake, self.CODE, "alice", clock, clock.sleep))
        polls = [p for m, p in fake.calls]
        self.assertEqual({m for m, _ in fake.calls}, {"getUpdates"})
        self.assertNotIn("offset", polls[0])
        self.assertEqual(polls[1]["offset"], 8)
        self.assertEqual(polls[-1], {"offset": 10, "timeout": 0, "limit": 1})  # the confirmation
        self.assertTrue(all(p["allowed_updates"] == ["message"] for p in polls[:-1]))
        self.assertTrue(all(p["timeout"] <= notify.POLL_S for p in polls))

    def test_a_webhook_or_a_bad_token_stops_it_with_a_clear_error(self):
        clock = Clock()
        fake = Fake([down(409, "HTTP 409: Conflict: can't use getUpdates method while webhook is active")])
        with self.assertRaises(notify.TelegramError) as ctx:
            notify.pair(fake, self.CODE, "alice", clock, clock.sleep)
        self.assertEqual(ctx.exception.status, 409)
        fake = Fake([down(None)] * 5)
        with self.assertRaises(notify.TelegramError):
            notify.pair(fake, self.CODE, "alice", clock, clock.sleep)

    def test_a_dropped_connection_is_tried_again(self):
        fake, clock = Fake([down(None)]), Clock()
        fake.script.append({"ok": True, "result": [update(1, "/start " + self.CODE)]})
        self.assertEqual(notify.pair(fake, self.CODE, "alice", clock, clock.sleep)["chat_id"], 777)


class FakeTerm:
    def __init__(self, secret=TOKEN, answers=("alice",)):
        self.lines, self.answers, self.token = [], list(answers), secret

    def say(self, text=""):
        self.lines.append(text)

    def ask(self, prompt):
        self.lines.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def secret(self, prompt):
        self.lines.append(prompt)
        return self.token

    @property
    def text(self):
        return "\n".join(self.lines)


class Setup(Base):
    def run_setup(self, term=None, press=True, user="alice", script=(), wait=60):
        """--setup with a fake Telegram: the person presses Start (the code is read from the link the program printed)."""
        term = term or FakeTerm()
        clock = Clock()

        class Telegram(Fake):
            def __call__(me, method, payload):
                r = Fake.__call__(me, method, payload)
                if method == "getUpdates":
                    clock.t += payload.get("timeout", 0)
                    code = re.search(r"start=([A-Za-z0-9_-]+)", term.text)
                    if press and code and payload.get("timeout"):
                        return {"ok": True, "result": [update(1, "/start " + code.group(1), user=user)]}
                    return {"ok": True, "result": []}
                return r
        fake = Telegram(script)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = notify.setup(term, lambda token: fake, self.dir, clock=clock, sleep=clock.sleep, wait=wait)
        return rc, fake, term

    def test_it_pairs_one_username_and_turns_the_notifier_on(self):
        with open(self.cfg_path, "w") as f:
            f.write("# my settings\n[features]\nmap = no\n\n[telegram]\nenabled = no\ndetail = full\n")
        rc, fake, term = self.run_setup()
        self.assertEqual(rc, 0, term.text)
        self.assertEqual(fake.methods, ["getMe", "getUpdates", "getUpdates", "sendMessage"])
        self.assertEqual(notify.read_token(self.dir), TOKEN)
        chat = notify.read_chat(self.dir)
        self.assertEqual((chat["chat_id"], chat["username"], chat["bot"]), (777, "alice", "my_bot"))
        self.assertEqual(set(chat), {"chat_id", "username", "bot", "paired_ts"})
        cfg = slurp(self.cfg_path)
        self.assertIn("# my settings", cfg)
        self.assertIn("map = no", cfg)
        self.assertEqual(nuc_config.load()["telegram"], {"enabled": True, "username": "alice", "detail": "full", "resolved": True})
        self.assertIn("username = @alice", cfg)
        (method, payload), = [c for c in fake.calls if c[0] == "sendMessage"]
        self.assertEqual(payload["chat_id"], 777)
        self.assertIn("paired. You will receive the ATTENTION changes of this machine; it never reads your messages.", payload["text"])
        notify.service_control.assert_called_once_with("restart")
        self.assertIn("bot @my_bot found", term.text)
        self.assertIn("https://t.me/my_bot?start=", term.text)
        self.assertIn("/setjoingroups", term.text)
        if POSIX:
            for name in ("token", "chat.json"):
                self.assertEqual(os.stat(os.path.join(self.dir, name)).st_mode & 0o777, 0o600)

    def test_the_token_is_never_printed_and_the_code_is_url_safe(self):
        rc, fake, term = self.run_setup()
        self.assertNotIn(SECRET, term.text)
        self.assertNotIn("Zq9Zq9", term.text)
        (code,) = re.findall(r"start=(\S+)", term.text)
        self.assertRegex(code, r"^[A-Za-z0-9_-]{16,64}$")

    def test_the_username_defaults_to_the_one_in_config_ini(self):
        rc, fake, term = self.run_setup(FakeTerm(answers=[""]))
        self.assertEqual(rc, 0, term.text)
        self.assertEqual(notify.read_chat(self.dir)["username"], "alice")
        rc, fake, term = self.run_setup(FakeTerm(answers=["@Bob_99"]), user="bob_99")
        self.assertEqual((rc, notify.read_chat(self.dir)["username"]), (0, "bob_99"))
        self.assertIn("username = @bob_99", slurp(self.cfg_path))

    def test_somebody_else_pressing_start_does_not_pair(self):
        rc, fake, term = self.run_setup(user="mallory", wait=60)
        self.assertEqual(rc, 1)
        self.assertIn("nothing was paired", term.text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "chat.json")))
        self.assertNotIn("sendMessage", fake.methods)
        self.assertIn("enabled = yes", slurp(self.cfg_path))  # untouched: it was already yes
        self.assertIn("username = @alice", slurp(self.cfg_path))

    def test_nobody_pressing_start_leaves_the_config_alone(self):
        self.config(enabled="no")
        rc, fake, term = self.run_setup(press=False, wait=60)
        self.assertEqual(rc, 1)
        self.assertEqual(slurp(self.cfg_path), "[telegram]\nenabled = no\n")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "token")))
        notify.service_control.assert_not_called()

    def test_a_new_pairing_forgets_the_old_chat_first(self):
        self.pair_files(user="old_user")
        rc, fake, term = self.run_setup(press=False, wait=30)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "chat.json")))

    def test_a_wrong_token_is_stopped_before_anything_is_written(self):
        rc, fake, term = self.run_setup(FakeTerm(secret="not a token"))
        self.assertEqual((rc, fake.calls), (2, []))
        rc, fake, term = self.run_setup(script=[{"ok": False, "error_code": 401, "description": "Unauthorized"}])
        self.assertEqual((rc, fake.methods), (1, ["getMe"]))
        self.assertIn("Unauthorized", term.text)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_username_that_cannot_be_one_is_refused(self):
        for bad in ("abc", "no spaces here", "a" * 40, "ali-ce"):
            rc, fake, term = self.run_setup(FakeTerm(answers=[bad]))
            self.assertEqual((rc, fake.methods), (2, ["getMe"]))
            self.assertEqual(os.listdir(self.dir), [])

    def test_it_needs_root_or_an_administrator(self):
        env = {k: v for k, v in os.environ.items() if k != "NUC_CONSOLE_NOTIFY_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(notify, "is_admin", return_value=False):
            rc, fake, term = self.run_setup()
        self.assertEqual((rc, fake.calls, os.listdir(self.dir)), (2, [], []))
        self.assertIn("root" if POSIX else "administrator", term.text)

    def test_a_config_that_cannot_be_written_is_said_not_hidden(self):
        with mock.patch.object(nuc_config, "set_key", side_effect=PermissionError("denied")):
            rc, fake, term = self.run_setup()
        self.assertEqual(rc, 1)
        self.assertIn("Set it by hand: [telegram] enabled = yes", term.text)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "chat.json")))

    def test_a_service_that_cannot_be_started_is_reported_not_fatal(self):
        notify.service_control.return_value = (False, "Unit nuc-console-notify.service not found")
        rc, fake, term = self.run_setup()
        self.assertEqual(rc, 0)
        self.assertIn("could not restart the notifier service (Unit nuc-console-notify.service not found)", term.text)

    def test_piped_input_is_read_line_by_line_and_the_token_is_not_echoed_on_a_terminal(self):
        term = notify.Terminal()
        with mock.patch.object(sys, "stdin", io.StringIO(TOKEN + "\nalice\n")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual((term.secret("t: "), term.ask("u: "), term.ask("more: ")), (TOKEN, "alice", ""))
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.object(sys, "stdin", tty), mock.patch.object(notify.getpass, "getpass", return_value=TOKEN) as gp:
            self.assertEqual(term.secret("t: "), TOKEN)
        gp.assert_called_once()


# ---- the other commands ------------------------------------------------------------------------------------------------------

class Commands(Base):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = notify.main(["notify.py", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_help(self):
        rc, out, _ = self.run_main("--help")
        self.assertEqual(rc, 0)
        for flag in ("--setup", "--test", "--status", "--on", "--off", "--forget", "--preview", "--enabled"):
            self.assertIn(flag, out)
        self.assertIn("never reads your messages", out)

    def test_nonsense_arguments_print_the_help_and_exit_2(self):
        for args in (("--nope",), ("--setup", "--test"), ("--json",), ("--demo",), ("--status", "--demo"), ("--demo-os",), ("--preview", "--demo-os", "plan9"),
                     ("stray",)):
            rc, out, err = self.run_main(*args)
            self.assertEqual((rc, out), (2, ""), args)
            self.assertIn("--setup", err)

    def test_on_and_off_edit_the_config_and_drive_the_service(self):
        self.config(enabled="no", username="@alice", detail="full")
        self.pair_files()
        self.assertEqual(self.run_main("--on")[0], 0)
        self.assertTrue(nuc_config.load()["telegram"]["enabled"])
        notify.service_control.assert_called_with("restart")
        self.assertEqual(nuc_config.load()["telegram"]["detail"], "full")
        self.assertEqual(self.run_main("--off")[0], 0)
        self.assertFalse(nuc_config.load()["telegram"]["enabled"])
        notify.service_control.assert_called_with("stop")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "token")))  # --off keeps the pairing

    def test_on_without_a_pairing_says_so(self):
        rc, out, _ = self.run_main("--on")
        self.assertEqual(rc, 0)
        self.assertIn("not paired yet", out)

    def test_forget_deletes_the_token_and_the_pairing_and_turns_it_off(self):
        self.pair_files()
        notify.put(self.dir, "sent.json", '{"keys": {}}', 0o600)
        rc, out, _ = self.run_main("--forget")
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(self.dir), ["status.json"])
        self.assertFalse(nuc_config.load()["telegram"]["enabled"])
        notify.service_control.assert_called_with("stop")
        st = self.status()
        self.assertEqual((st["enabled"], st["paired"]), (False, False))
        self.assertEqual(self.run_main("--forget")[0], 0)  # nothing left: still fine

    def test_these_need_root(self):
        env = {k: v for k, v in os.environ.items() if k != "NUC_CONSOLE_NOTIFY_DIR"}
        self.pair_files()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(notify, "is_admin", return_value=False):
            for flag in ("--on", "--off", "--forget", "--setup"):
                self.assertEqual(self.run_main(flag)[0], 2, flag)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "token")))
        self.assertTrue(nuc_config.load()["telegram"]["enabled"])
        notify.service_control.assert_not_called()

    def test_test_message(self):
        self.pair_files()
        fake = Fake()
        term = FakeTerm()
        self.assertEqual(notify.send_test(term, lambda token: fake, self.dir), 0)
        (method, payload), = fake.calls
        self.assertEqual((method, payload["chat_id"]), ("sendMessage", CHAT_ID))
        self.assertIn("test message", payload["text"])
        self.assertIn("@alice", term.text)
        fake = Fake([down(403, "HTTP 403: Forbidden")])
        term = FakeTerm()
        self.assertEqual(notify.send_test(term, lambda token: fake, self.dir), 1)
        self.assertIn("HTTP 403", term.text)
        self.assertEqual(notify.send_test(FakeTerm(), lambda token: Fake(), os.path.join(self.dir, "nowhere")), 1)

    def test_status_json_has_no_secret(self):
        self.pair_files()
        clock = Clock(1_000_000.0)
        with mock.patch("time.time", clock):
            notify.write_status(self.dir, notify.status_dict(clock(), True, True, "alice", 999_000, "HTTP 502: Bad Gateway", 999_500))
            rc, out, _ = self.run_main("--status", "--json")
        self.assertEqual(rc, 0)
        for secret in (SECRET, TOKEN, str(CHAT_ID)):
            self.assertNotIn(secret, out)
        d = json.loads(out)
        self.assertEqual((d["enabled"], d["username"], d["paired"], d["running"], d["bot"]), (True, "alice", True, True, "my_bot"))
        self.assertEqual((d["last_sent_ts"], d["last_error"], d["failing_since"]), (999_000, "HTTP 502: Bad Gateway", 999_500))

    def test_status_in_words(self):
        self.pair_files()
        now = 2_000_000.0
        notify.write_status(self.dir, notify.status_dict(now - 20, True, True, "alice", now - 3600, "HTTP 502: Bad Gateway", now - 600))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.time", Clock(now)):
            self.assertEqual(notify.main(["notify.py", "--status"]), 0)
        text = out.getvalue()
        for line in ("Telegram notifications: on", "username:    @alice", "paired:      yes (bot @my_bot)", "running (status 20 s old)", "(60 min ago)",
                     "last error:  HTTP 502: Bad Gateway (failing since"):
            self.assertIn(line, text)
        for secret in (SECRET, str(CHAT_ID)):
            self.assertNotIn(secret, text)
        notify.write_status(self.dir, notify.status_dict(now - 20, True, False, "alice", None, "not paired: run nuc-console-telegram --setup", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.time", Clock(now)):
            notify.main(["notify.py", "--status"])
        self.assertIn("paired:      no: run nuc-console-telegram --setup", out.getvalue())

    def test_status_says_not_running_when_the_status_is_old(self):
        notify.write_status(self.dir, notify.status_dict(1000, True, True, "alice", None, None, None))
        d = json.loads(self.run_main("--status", "--json")[1])
        self.assertIs(d["running"], False)
        rc, out, _ = self.run_main("--status")
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)
        self.assertIn("@alice", out)

    def test_status_with_nothing_there_does_not_fail(self):
        os.rmdir(self.dir)
        rc, out, _ = self.run_main("--status")
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)
        d = json.loads(self.run_main("--status", "--json")[1])
        self.assertEqual((d["paired"], d["ts"], d["running"]), (None, None, False))

    def test_preview_prints_the_message_and_sends_nothing(self):
        with mock.patch.object(notify, "HttpsTransport", side_effect=AssertionError("preview never sends")), \
                mock.patch.object(render, "DEMO", False), mock.patch.object(render, "DEMO_OS", None), mock.patch.dict(render.CFG, clear=False), \
                mock.patch.object(socket, "gethostname", socket.gethostname), mock.patch.object(render, "read_thermal", return_value={}):
            rc, out, _ = self.run_main("--preview", "--demo")
            self.assertEqual(rc, 0)
            self.assertIn("nuc-console · demo-host", out)
            self.assertIn("NEW  ‼ Database/broker open on the LAN", out)
            self.assertNotIn("DB/broker open on LAN", out)  # the text of the problem is not in the titles
            self.config(enabled="yes", username="@alice", detail="full")
            rc, out, _ = self.run_main("--preview", "--demo")
            self.assertIn("Database/broker open on the LAN: 1 DB/broker open on LAN", out)

    def test_service_control_uses_fixed_command_lines(self):
        for os_name, table in notify.SERVICE.items():
            for action, steps in table.items():
                for argv in steps:
                    self.assertTrue(all(isinstance(a, str) for a in argv))
        self.assertEqual(notify.SERVICE["linux"]["restart"], [["systemctl", "restart", "nuc-console-notify"]])
        self.assertEqual(notify.SERVICE["linux"]["stop"], [["systemctl", "stop", "nuc-console-notify"]])
        self.assertEqual(notify.SERVICE["darwin"]["restart"], [["launchctl", "kickstart", "-k", "system/com.nuc-console.notify"]])
        self.assertEqual(notify.SERVICE["darwin"]["stop"], [["launchctl", "kill", "TERM", "system/com.nuc-console.notify"]])
        self.assertEqual(notify.SERVICE["windows"]["restart"][-1], ["schtasks", "/Run", "/TN", "\\nuc-console\\notify"])
        self.assertEqual(notify.SERVICE["windows"]["stop"], [["schtasks", "/End", "/TN", "\\nuc-console\\notify"]])

    def test_service_control_runs_without_a_shell_and_reports_failures(self):
        ran = []

        def fake_run(argv, **kw):
            ran.append((argv, kw))
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(nuc_config, "OS_NAME", "linux"), mock.patch.object(notify, "POSIX", True):
            with mock.patch.object(notify.subprocess, "run", fake_run):
                self.assertEqual(REAL_SERVICE_CONTROL("restart"), (True, ""))
            self.assertEqual(ran[0][0], ["systemctl", "restart", "nuc-console-notify"])
            self.assertNotIn("shell", ran[0][1])
            self.assertEqual(ran[0][1]["env"]["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
            with mock.patch.object(notify.subprocess, "run", side_effect=FileNotFoundError("systemctl")):
                ok, why = REAL_SERVICE_CONTROL("stop")
            self.assertFalse(ok)
            self.assertIn("systemctl", why)
            with mock.patch.object(notify.subprocess, "run", return_value=mock.Mock(returncode=5, stdout="", stderr="Unit not found\n")):
                self.assertEqual(REAL_SERVICE_CONTROL("stop"), (False, "Unit not found"))


# ---- the commands in bin/ ----------------------------------------------------------------------------------------------------

class Wrappers(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8", newline="") as f:
            return f.read()

    def test_the_shell_wrapper_runs_the_installed_code_with_a_clean_environment(self):
        s = self.read("bin/nuc-console-telegram")
        self.assertTrue(s.startswith("#!/bin/sh\n"))
        self.assertIn("exec env -u NUC_CONSOLE_CONFIG -u NUC_CONSOLE_NOTIFY_DIR", s)
        for var in ("NUC_CONSOLE_BASELINE", "NUC_CONSOLE_NET", "NUC_CONSOLE_STATE", "NUC_CONSOLE_BOOT", "NUC_CONSOLE_ACCEPTED"):
            self.assertIn(f"-u {var}", s)
        self.assertIn('/usr/bin/python3 /opt/nuc-console/notify.py "$@"', s)  # the two paths install-macos.sh rewrites
        self.assertNotIn("\r", s)
        if POSIX:
            self.assertTrue(os.access(os.path.join(ROOT, "bin", "nuc-console-telegram"), os.X_OK))

    def test_the_windows_wrapper_clears_the_redirect_variables(self):
        s = self.read("bin/nuc-console-telegram.cmd")
        for var in ("NUC_CONSOLE_CONFIG", "NUC_CONSOLE_NOTIFY_DIR", "NUC_CONSOLE_BASELINE", "NUC_CONSOLE_NET", "NUC_CONSOLE_STATE", "NUC_CONSOLE_BOOT",
                    "NUC_CONSOLE_ACCEPTED"):
            self.assertIn(f"set {var}=", s)
        self.assertIn(r'"%~dp0..\python\python.exe" -B "%~dp0..\app\notify.py" %*', s)
        self.assertIn("exit /b %errorlevel%", s)

    def test_line_endings_are_pinned(self):
        attrs = self.read(".gitattributes")
        self.assertIn("bin/nuc-console-telegram text eol=lf", attrs)
        self.assertIn("*.cmd text eol=crlf", attrs)


class SecretsFolder(unittest.TestCase):
    """The token and the chat are where the web view's account cannot read them (docs/TELEGRAM.md, SECURITY.md)."""

    def test_windows_keeps_the_secrets_in_private_and_status_beside_it(self):
        with mock.patch.object(nuc_config, "WINDOWS", True):
            self.assertEqual(notify.secret_dir(os.path.join("X", "notify")), os.path.join("X", "notify", "private"))
        with mock.patch.object(nuc_config, "WINDOWS", False):
            self.assertEqual(notify.secret_dir(os.path.join("X", "notify")), os.path.join("X", "notify"))

    def test_linux_service_user_is_its_own(self):
        src = open(notify.__file__, encoding="utf-8").read()
        self.assertIn('"nuc-console-notify"', src)  # not "nuc-console", the web view's user


if __name__ == "__main__":
    unittest.main()
