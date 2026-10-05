"""What the Telegram page of the web view does besides showing: pair this machine with a bot of your own, switch the alerts on and off,
send a test. One engine per process (the web view's); web.py turns a form into a call and the snapshot into the page.

  pair     your bot's token and your @username: getMe checks the token and names the bot, a one-time code makes the link
           https://t.me/<bot>?start=<code>, and notify.pair() (the code `nuc-console-telegram --setup` uses) waits, in a thread of its own,
           for that Start from that @username; Cancel stops it. The pairing is then handed to the notifier and forgotten here.
  on, off  requests without a secret; test: the notifier sends a test message to the paired chat.

The token goes one way. This account (the web view's) cannot read the notifier's folder: it leaves a request in NOTIFY_DIR/inbox, where it
may create files and do nothing else (Linux: the folder is 2730 and the web unit is in its group; Windows: create only), and the notifier,
another account, reads and deletes it, keeps the token where only it can read it, and says what it did in status.json (the request's id,
ok, a sentence). The token is in this process only while a pairing runs, in that thread. One thing at a time; nothing here blocks a page.
`[telegram] web_actions = no` refuses everything. In a portable run (the desktop app is one) the notifier is started beside the web view, as
the same account, in data/notify: the same requests, the same answers.
--demo: the same flow simulated in memory (nothing is sent, nothing is written).
Standard library only, Python 3.8+.
"""
import json
import os
import re
import secrets
import threading
import time

import notify
import nuc_config

ANSWER_S = 60       # the notifier looks at its inbox every notify.INBOX_S: no answer after this long means it is not listening
NOTICE_S = 300      # the answer to the last action stays on the page this long
POLL_S = 10         # the longest wait of one getUpdates while pairing: Cancel takes effect within it
DEMO_START_S = 6.0  # the demo: somebody presses Start this long after the link appears
DEMO_BOT = "demo_nuc_console_bot"
LOCKED = "locked by config.ini ([telegram] web_actions = no): this page only shows; the command line still works"
RESTART_PORTABLE = "quit nuc-console and start it again: the notifier starts with it (its log: logs/notify.log)"
START_CMD = {"linux": "sudo systemctl restart nuc-console-notify", "darwin": "sudo launchctl kickstart -k system/com.nuc-console.notify",
             "windows": "Start-ScheduledTask -TaskPath \\nuc-console\\ -TaskName notify (administrator PowerShell)"}
CLI = {"linux": "sudo nuc-console-telegram", "darwin": "sudo nuc-console-telegram", "windows": "nuc-console-telegram.cmd (administrator prompt)"}
RUNNING = ("checking", "waiting", "handing")  # a pairing in progress


class Engine(object):
    """The Telegram work of one process. Every action returns (ok, text) and leaves the text as the notice the page shows; none blocks.
    Tests give the folder, a fake transport, the clock and the sleep; the demo gives demo=True."""

    def __init__(self, demo=False, notify_dir=None, make_transport=None, cfg_fn=None, clock=None, sleep=None, wait=None, poll=POLL_S):
        self.demo, self._dir, self.cfg_fn = demo, notify_dir, cfg_fn
        self.clock = clock or (lambda: time.time())  # looked up at each call: tests/golden.py freezes this module's clock
        self.sleep = sleep or (lambda sec: time.sleep(sec))
        self.make = make_transport or notify.HttpsTransport
        self.wait_s, self.poll = notify.PAIR_S if wait is None else wait, poll
        self.demo_start = DEMO_START_S
        self.lock = threading.RLock()
        self.version = 0     # +1 at every change the page shows
        self.job = None      # the pairing: {"state", "username", "bot", "link", "until", "text"}; never the token
        self.pending = None  # a request the notifier has not answered yet: {"id", "action", "at"}
        self.notice = None   # {"ok", "text", "at"}
        self._stop, self._thread = threading.Event(), None
        self.dstate = {"enabled": False, "paired": False, "username": "", "last_sent_ts": None}  # the demo's notifier, in memory

    # ---------------------------------------------------------------------------------------------------------------- places and state
    @property
    def directory(self):
        return self._dir or nuc_config.NOTIFY_DIR

    def cfg(self):
        return self.cfg_fn() if self.cfg_fn else _BIND["cfg"]() if "cfg" in _BIND else nuc_config.current()

    def os_name(self):
        return "linux" if self.demo else nuc_config.OS_NAME  # the demo speaks the words of an installed Linux, on every OS (tests/golden.py)

    def locked(self):
        return not self.cfg()["telegram"].get("web_actions", True)

    def portable(self):
        return bool(nuc_config.PORTABLE) and not self.demo

    def start_hint(self):
        """How to start the notifier when it does not answer: the service's command, or in a portable run quitting and starting it again."""
        return RESTART_PORTABLE if self.portable() else START_CMD[self.os_name()]

    def settings(self):
        """[telegram] in force (nuc_config.telegram): enabled, by ("config" | "web" | ""), username, detail, resolved, web_actions."""
        if self.demo:
            d = self.dstate
            return dict(self.cfg()["telegram"], enabled=d["enabled"], by="web" if d["enabled"] else "", username=d["username"])
        return nuc_config.telegram(self.cfg(), self.directory)

    def status(self):
        """The notifier's status.json (no secret in it), {} when there is none or this account may not read it."""
        if self.demo:
            d = self.dstate
            return {"ts": int(self.clock()), "enabled": d["enabled"], "paired": d["paired"], "username": d["username"] or None,
                    "last_sent_ts": d["last_sent_ts"], "last_error": None, "failing_since": None, "by": "web" if d["enabled"] else None,
                    "listening": True, "request": None}
        return notify.read_status(self.directory) or {}

    def listening(self, st=None):
        """The notifier runs and takes this page's requests: its status is fresh and says so."""
        st = self.status() if st is None else st
        ts = st.get("ts")
        fresh = isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 <= self.clock() - ts < notify.STALE_S
        return bool(fresh and st.get("listening") is True)

    def busy(self):
        return bool(self.pending or (self.job and self.job["state"] in RUNNING))

    def _changed(self):
        self.version += 1

    def _say(self, ok, text):
        with self.lock:
            self.notice = {"ok": bool(ok), "text": text, "at": self.clock()}
            self._changed()
        return bool(ok), text

    def _refusal(self, need_notifier=True):
        """Why nothing can be asked now, or None."""
        if self.locked():
            return LOCKED
        if self.busy():
            return "wait: the last action is not finished"
        if need_notifier and not self.listening():
            return "the notifier service does not take this page's requests now: start it (%s), then try again" % self.start_hint()
        return None

    # ------------------------------------------------------------------------------------------------------------------------- actions
    def pair(self, token, username):
        """Starts a pairing: the token is checked, the link shown, Start awaited (in a thread). -> (ok, text)."""
        why = self._refusal()
        if why:
            return self._say(False, why)
        token, user = (token or "").strip(), (username or "").strip().lstrip("@").lower()
        if not notify.TOKEN_RE.fullmatch(token):
            return self._say(False, "that is not a bot token (it looks like 123456789:AAH...): copy it again from @BotFather")
        if not notify.USER_RE.fullmatch(user):
            return self._say(False, "a Telegram @username has 5 to 32 letters, digits or _ (Telegram: Settings > Username)")
        with self.lock:
            self._stop.clear()
            self.job = {"state": "checking", "username": user, "bot": "", "link": "", "until": 0, "text": ""}
            self.notice = None
            self._changed()
            work = self._demo_pair if self.demo else self._pair
            self._thread = threading.Thread(target=work, args=(token, user), name="telegram-pairing", daemon=True)
            self._thread.start()
        return True, "checking the token"

    def cancel(self):
        """Stops the pairing that waits (within POLL_S): nothing is paired."""
        with self.lock:
            if not (self.job and self.job["state"] in ("checking", "waiting")):
                return False, "nothing to cancel"
            self._stop.set()
            self.job.update(state="cancelled", text="cancelled: nothing was paired", link="")
        return self._say(True, "cancelled: nothing was paired")

    def request(self, action):
        """on, off or test: a request for the notifier. -> (ok, text)."""
        if action not in ("on", "off", "test"):
            return False, "unknown action"
        why = self._refusal()
        if why:
            return self._say(False, why)
        if self.demo:
            return self._demo_request(action)
        try:
            self._send(action)
        except OSError as e:
            return self._say(False, self._cannot(e))
        return True, "asked the notifier"

    def link(self):
        """The t.me link of the pairing that waits for Start, else "" (the page's /?view=telegram&open=1 redirects to it)."""
        with self.lock:
            return self.job["link"] if self.job and self.job["state"] == "waiting" else ""

    def wait(self, timeout=30):
        """Tests: until the pairing thread has ended."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return not (t and t.is_alive())

    # ------------------------------------------------------------------------------------------------------------------- the pairing
    def _pair(self, token, user):
        tg = None
        try:
            tg = self.make(token)
            me = notify.call(tg, "getMe", {}, self.sleep)
            bot = me.get("username") if isinstance(me, dict) else None
            if not (isinstance(bot, str) and notify.BOT_RE.fullmatch(bot)):
                return self._fail("Telegram did not name the bot: is this the token of a bot?")
            code = secrets.token_urlsafe(16)  # only A-Za-z0-9_-, what a /start link may carry
            with self.lock:
                if self._stop.is_set():
                    return None
                self.job.update(state="waiting", bot=bot, link="https://t.me/%s?start=%s" % (bot, code), until=self.clock() + self.wait_s)
                self._changed()
            chat = notify.pair(tg, code, user, self.clock, self.sleep, self.wait_s, self.poll, self._stop.is_set)
            if self._stop.is_set():
                return None
            if chat is None:
                return self._fail("nobody pressed Start in time: nothing was paired (try again: a new link)")
            with self.lock:
                if self._stop.is_set():  # cancelled while the Start arrived: nothing is handed over
                    return None
                self.job.update(state="handing", link="", text="")
                self._changed()
            self._send("pair", token=token, chat_id=chat["chat_id"], username=user, bot=bot)
        except notify.TelegramError as e:  # its text holds no token
            self._fail("Telegram: %s%s" % (e, " (a bot with a webhook cannot be paired: remove it with @BotFather)" if e.status == 409 else ""))
        except OSError as e:
            self._fail(self._cannot(e))
        except Exception as e:  # noqa: BLE001 - a thread that dies says so on the page, never with the token
            self._fail(notify.clean(notify.redact("internal error: %s: %s" % (type(e).__name__, e), token), 160))
        finally:
            token = tg = None  # noqa: F841 - nothing of it stays in this process
        return None

    def _fail(self, text):
        with self.lock:
            if self.job and self.job["state"] != "cancelled":
                self.job.update(state="failed", text=text, link="")
                self._say(False, text)

    def _cannot(self, e):
        where = notify.inbox_dir(self.directory)
        fix = "quit nuc-console and start it again: the notifier makes that folder" if self.portable() else "the installer makes that folder; run it again"
        return "cannot leave the request in %s (%s): %s" % (where, e.strerror or type(e).__name__, fix)

    def _send(self, action, **fields):
        """One request in the inbox: r-<ms>-<random>.json, created here (never replaced), 0640 (Linux: the folder's group is the
        notifier's). Raises OSError."""
        rid = "%013d-%s" % (int(self.clock() * 1000), secrets.token_hex(4))
        data = dict({"v": 1, "id": rid, "action": action, "ts": int(self.clock())}, **fields)
        path = os.path.join(notify.inbox_dir(self.directory), "r-%s.json" % rid)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), 0o640)  # the unit's umask must not take the group's read away
            f.write(json.dumps(data))
            f.flush()
            os.fsync(f.fileno())
        with self.lock:
            self.pending = {"id": rid, "action": action, "at": self.clock()}
            self._changed()
        return rid

    def _resolve(self):
        """The notifier's answer to the pending request (status.json), or its silence after ANSWER_S: the notice, and the pairing's end."""
        with self.lock:
            p = self.pending
            if not p:
                return
            req = self.status().get("request")
            if isinstance(req, dict) and req.get("id") == p["id"]:
                ok, text = req.get("ok") is True, notify.clean(req.get("said") or ("done" if req.get("ok") else "refused"), 200)
            elif self.clock() - p["at"] > ANSWER_S:
                ok, text = False, "the notifier did not answer: is it running? (%s)" % self.start_hint()
            else:
                return
            self.pending = None
            if p["action"] == "pair" and self.job and self.job["state"] == "handing":
                self.job.update(state="done" if ok else "failed", text=text)
            self._say(ok, text)

    # ------------------------------------------------------------------------------------------------------------------------ the page
    def snapshot(self):
        """What the page shows: plain values, no secret."""
        self._resolve()
        with self.lock:
            st = self.status()
            note = self.notice if self.notice and self.clock() - self.notice["at"] < NOTICE_S else None
            return {"locked": self.locked(), "portable": self.portable(), "start": self.start_hint(), "demo": self.demo, "os": self.os_name(),
                    "settings": self.settings(), "status": st, "listening": self.listening(st), "job": dict(self.job) if self.job else None,
                    "pending": dict(self.pending) if self.pending else None, "notice": dict(note) if note else None, "busy": self.busy(),
                    "now": self.clock()}

    # ------------------------------------------------------------------------------------------------------------------------ the demo
    def _demo_pair(self, token, user):
        self.sleep(0.3)
        code = secrets.token_urlsafe(16)
        with self.lock:
            if self._stop.is_set():
                return
            self.job.update(state="waiting", bot=DEMO_BOT, link="https://t.me/%s?start=%s" % (DEMO_BOT, code), until=self.clock() + self.wait_s)
            self._changed()
        if self._stop.wait(self.demo_start):
            return
        with self.lock:
            self.dstate.update(enabled=True, paired=True, username=user)
            text = "paired with @%s: a greeting was sent to that chat (demo: nothing was sent)" % user
            self.job.update(state="done", text=text, link="")
            self._say(True, text)

    def _demo_request(self, action):
        d = self.dstate
        if action == "test":
            if not d["paired"]:
                return self._say(False, "not paired: pair it first")
            d["last_sent_ts"] = int(self.clock())
            return self._say(True, "test message sent to @%s (demo: nothing was sent)" % d["username"])
        d["enabled"] = action == "on"
        return self._say(True, action + ("" if d["paired"] or action == "off" else ", but not paired yet"))


# ------------------------------------------------------------------------------------------------------------------------- the engine
_ENGINE, _ENGINE_LOCK, _BIND = None, threading.Lock(), {}


def bind(cfg=None):
    """The web view tells the engine where its settings are (render.CFG)."""
    if cfg is not None:
        _BIND["cfg"] = cfg


def engine():
    """The engine of this process (made at the first use)."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
        return _ENGINE


def set_engine(eng):
    """Another engine for this process (the demo's, a test's). -> the one that was there, or None."""
    global _ENGINE
    with _ENGINE_LOCK:
        old, _ENGINE = _ENGINE, eng
        return old


def configure(demo=False):
    """The web view's start: its engine is the demo's (simulated) or the real one."""
    eng = Engine(demo=bool(demo))
    set_engine(eng)
    return eng


def version():
    """The engine's change counter (0 without an engine): the page is drawn again when it moves."""
    return _ENGINE.version if _ENGINE is not None else 0


# ------------------------------------------------------------------------------------------------------------------------------ words

def state_of(snap):
    """('on' | 'off' | 'unpaired' | 'failing' | 'down', one plain line): are the alerts on, and do they reach anyone?"""
    t, st = snap["settings"], snap["status"]
    if not t["enabled"]:
        return "off", "off: no alert leaves this machine"
    if st.get("paired") is False or not t["username"]:
        return "unpaired", "on, but not paired: no alert can reach a phone yet"
    if not _fresh(st, snap["now"]):
        return "down", "on, but the notifier is not running"
    if st.get("failing_since"):
        return "failing", "on, but sending fails: " + notify.clean(notify.redact(st.get("last_error") or "no answer"), 120)
    return "on", "on: the ATTENTION changes go to @%s" % t["username"]


def _fresh(st, now):
    ts = st.get("ts")
    return isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 <= now - ts < notify.STALE_S


def job_text(job, now):
    """One plain line about the pairing."""
    if not job:
        return ""
    s = job["state"]
    if s == "checking":
        return "checking the token with Telegram…"
    if s == "waiting":
        left = max(0, int(job["until"] - now))
        return "waiting for Start from @%s (%s left)" % (job["username"], ("%d min" % ((left + 59) // 60)) if left >= 60 else "%d s" % left)
    if s == "handing":
        return "Start received from @%s: handing the pairing to the notifier…" % job["username"]
    return job["text"] or s


BOT_LINK = re.compile(r"https://t\.me/[A-Za-z0-9_]{3,64}\?start=[A-Za-z0-9_-]{16,64}")  # the only address the page's redirect may go to
