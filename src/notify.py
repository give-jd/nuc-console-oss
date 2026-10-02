#!/usr/bin/env python3
"""nuc-console notifier: the ATTENTION changes of this machine, as Telegram messages to one person. Stdlib only.

THE MACHINE SENDS, NOTHING ELSE. What you can audit in this file:
- the only network traffic is outbound HTTPS to api.telegram.org (a constant: no URL, proxy or webhook setting, no redirect
  followed, no listening socket anywhere);
- the service calls sendMessage (and getMe, to see that the bot is alive) and NEVER getUpdates: it reads no message at all, so
  nothing anyone writes to the bot can make this machine do anything;
- `--setup` is the only code that reads updates, once, to pair: it accepts ONE message, `/start <random code>` sent from the
  configured @username in a private chat, and silently ignores everything else;
- the bot token lives in NOTIFY_DIR/token (0600, owned by the service user), never in config.ini, a log, status.json or an error.
Off unless `[telegram] enabled = yes` in config.ini. The service is `notify.py` with no argument; `nuc-console-telegram` is the
command line (`--help`).
"""
import collections
import getpass
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import nuc_config
import render
import ui

API_HOST = "api.telegram.org"  # the only host this program talks to
METHODS = ("getMe", "getUpdates", "sendMessage")  # the only API methods it can call; the service uses two (see SendOnly)
TIMEOUT_S = 10  # per request (long polling adds its own wait)
CYCLE_S = 30  # the service looks at the problems this often
STALE_S = 120  # status.json older than this: the notifier is not running
MAX_PER_HOUR = 20  # messages; changes beyond it are held back and sent together as soon as there is room
MAX_TEXT = 4000  # Telegram refuses more than 4096 characters
MAX_LINES = 30  # changes listed in one message: the rest is counted
MAX_REPLY = 1 << 20  # bytes read from one reply
PAIR_S = 600  # --setup waits this long for the Start button
POLL_S = 25  # long polling during --setup
RETRY_PAUSE = (2, 5)  # seconds between the attempts of one send
ATTEMPTS = 3  # attempts per send and cycle; after a failed cycle the next one waits (see Notifier.fail)
MAC_CAFILE = "/etc/ssl/cert.pem"  # macOS: python.org's Python ships without certificates until "Install Certificates" is run
NOTIFY_DIR = nuc_config.NOTIFY_DIR
TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
USER_RE = re.compile(r"[A-Za-z0-9_]{5,32}")  # a Telegram @username, without the @ (fullmatch)
BOT_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
TOKEN_ANY = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
BIDI = "\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff"  # invisible and direction-changing characters: they could disguise a line
LINE_CHARS = re.compile("[" + BIDI + "]")
TEXT_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f" + BIDI + "]")  # everything but the newline between lines
SEV_RANK = {"port-change": 3, "error": 2, "warning": 1}
LOUD = ("port-change", "error")  # these get the ‼ marker
LABEL = {"NEW": "NEW  ", "OK": "OK   ", "OPEN": "OPEN ", "CHANGED": "CHANGED "}
TEXT_KEYED = render.NOT_ACCEPTABLE | render.COUNT_MATTERS  # problems for which one more or another item is a new problem
OWN = "telegram-"  # the ids of the notifier's own problems (the dashboard shows them): never announced, it would only talk to itself
POSIX = not nuc_config.WINDOWS
SERVICE = {  # fixed command lines (no shell); the names the installers create
    "linux": {"restart": [["systemctl", "restart", "nuc-console-notify"]], "stop": [["systemctl", "stop", "nuc-console-notify"]]},
    "darwin": {"restart": [["launchctl", "kickstart", "-k", "system/com.nuc-console.notify"]],
               "stop": [["launchctl", "kill", "TERM", "system/com.nuc-console.notify"]]},
    "windows": {"restart": [["schtasks", "/End", "/TN", "\\nuc-console\\notify"], ["schtasks", "/Run", "/TN", "\\nuc-console\\notify"]],
                "stop": [["schtasks", "/End", "/TN", "\\nuc-console\\notify"]]},  # restart: End fails when it is not running: fine
}
HELP = """nuc-console-telegram: the ATTENTION changes of this machine, sent to you on Telegram.
The machine only sends. It never reads your messages and listens on no port.

  nuc-console-telegram --setup      pair: your own bot's token and your @username, then press Start in Telegram *
  nuc-console-telegram --test       send a test message
  nuc-console-telegram --status     is it on, paired, running, what went wrong (--json for scripts; no secrets in it)
  nuc-console-telegram --on         turn it on again (starts the service) *
  nuc-console-telegram --off        turn it off (stops the service; the pairing stays) *
  nuc-console-telegram --forget     delete the token and the pairing, turn it off *
  nuc-console-telegram --preview    print the message the current problems would send (nothing is sent; --demo: invented data)
  nuc-console-telegram --enabled    exit code 0 when [telegram] enabled = yes (for the installers)
  notify.py [--log FILE]            the service itself (no argument), with its output in a file (Windows, macOS)
* needs root (Windows: an administrator prompt)
Settings: [telegram] in config.ini (enabled, username, detail = titles|full, resolved). The token is never in config.ini.
"""
INTRO = """nuc-console will send you the ATTENTION changes of this machine on Telegram. It only sends: it never reads your messages.
You need a bot of your own (free): open @BotFather in Telegram, send /newbot, and keep the token it gives you.
And your @username (Telegram: Settings > Username): only that person can pair with the bot."""


# ---- text: nothing that leaves the machine, or reaches a log, may carry a control character or a secret ------------------------

def clean(s, limit=0):
    """One line of plain text: control and direction characters out, white space collapsed, optionally cut."""
    s = " ".join(LINE_CHARS.sub("", ui.CTRL.sub(" ", str(s))).split())
    return s[:limit] if limit else s


def redact(text, *hide):
    """The text without the bot token (urllib puts it, inside `bot<token>`, in the URL of every error) or anything else in `hide`."""
    s = str(text)
    for h in hide:
        if h and len(str(h)) >= 4:
            s = s.replace(str(h), "<redacted>")
    return TOKEN_ANY.sub("<redacted>", BOT_URL.sub("bot<redacted>", s))


def hostname():
    return clean(socket.gethostname(), 60) or "this machine"


class TelegramError(Exception):
    """A call that failed. The text is already redacted: it is safe to print, log and store."""

    def __init__(self, message, status=None, retry_after=None):
        Exception.__init__(self, message)
        self.status, self.retry_after = status, retry_after

    @property
    def retryable(self):
        """Worth trying again: no answer at all, too many requests, or Telegram's own trouble. A 400/401/403 is not going to change."""
        return self.status is None or self.status == 429 or self.status >= 500


# ---- files: atomic, private, and read back only if nobody else could have written them --------------------------------------

def write_atomic(path, text, mode, owner=None, sleep=time.sleep):
    """tmp + fsync + chmod + rename. The temporary file is created exclusively with the final mode (a secret is never readable
    by others, not even for a moment, and a link planted at its name is not followed)."""
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)  # the umask may have taken bits away
            else:
                os.chmod(tmp, mode)
            if owner is not None and hasattr(os, "fchown"):  # root (--setup): the files belong to the service user
                os.fchown(f.fileno(), owner[0], owner[1])
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: somebody is reading the previous file right now
                if POSIX or attempt == 19:
                    raise
                sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def owner_of(d):
    """(uid, gid) the files of folder d get when root writes them, else None (the service writes as itself)."""
    if POSIX and os.geteuid() == 0:
        st = os.stat(d)
        return st.st_uid, st.st_gid
    return None


def put(d, name, text, mode):
    write_atomic(os.path.join(d, name), text, mode, owner_of(d))


def read_private(path, limit=65536):
    """Text of a file that holds a secret. POSIX, like web.read_token: no group/other permission bits, and the owner is root, this
    user or the owner of the folder (the service user: root reads what --setup wrote for it); the folder is not writable by others."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if POSIX:
            if st.st_mode & 0o077:
                raise ValueError(f"{path} must not be readable by group/others (chmod 600)")
            dst = os.stat(os.path.dirname(path) or ".")
            if st.st_uid not in (0, os.geteuid(), dst.st_uid):
                raise ValueError(f"{path} must be owned by root or by the service user")
            if dst.st_mode & 0o022:
                raise ValueError(f"the folder of {path} must not be writable by group/others")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} is too large")
    return data.decode("utf-8")


def secret_dir(d):
    """Where the token, the chat and sent.json live. Windows: NOTIFY_DIR\\private, which only SYSTEM, Administrators and the
    notifier (NETWORK SERVICE) can open; the web view (LOCAL SERVICE) reads status.json in NOTIFY_DIR, never a secret.
    Linux/macOS: NOTIFY_DIR itself (0711: files 0600 of the notifier's own user)."""
    return os.path.join(d, "private") if nuc_config.WINDOWS else d


def read_token(d):
    """The bot token in folder d, validated. Raises OSError / ValueError (its text never holds the token)."""
    tok = read_private(os.path.join(secret_dir(d), "token"), 256).strip()
    if not TOKEN_RE.fullmatch(tok):
        raise ValueError("the token file does not hold a bot token (123456789:AA...)")
    return tok


def read_chat(d):
    """{"chat_id": int, "username": str, "bot": str, "paired_ts": int} from folder d. Raises OSError / ValueError."""
    try:
        chat = json.loads(read_private(os.path.join(secret_dir(d), "chat.json")))
    except (RecursionError, UnicodeDecodeError):
        raise ValueError("chat.json is not readable") from None
    cid, user = (chat.get("chat_id"), chat.get("username")) if isinstance(chat, dict) else (None, None)
    if not (isinstance(cid, int) and not isinstance(cid, bool) and isinstance(user, str) and USER_RE.fullmatch(user)):
        raise ValueError("chat.json is not valid")
    return chat


def load_credentials(d, username):
    """-> (token, chat, problem). problem is None, or a sentence on why the notifier cannot send (it goes to status.json)."""
    try:
        token = read_token(d)
    except FileNotFoundError:
        return None, None, "no bot token: run nuc-console-telegram --setup"
    except PermissionError:
        return None, None, "the bot token is private: run this as root (or as the service user)"
    except (OSError, ValueError) as e:
        return None, None, clean(redact(f"the token cannot be used: {e}"), 160)
    try:
        chat = read_chat(d)
    except FileNotFoundError:
        return None, None, "not paired: run nuc-console-telegram --setup"
    except PermissionError:
        return None, None, "the pairing is private: run this as root (or as the service user)"
    except (OSError, ValueError) as e:
        return None, None, clean(redact(f"the pairing cannot be used: {e}"), 160)
    if not username:
        return None, None, "[telegram] username is empty in config.ini: run nuc-console-telegram --setup"
    if chat["username"].lower() != username:  # the config names someone else than the pairing: never send to the old person
        return None, None, "[telegram] username is not the paired one: run nuc-console-telegram --setup"
    return token, chat, None


def status_dict(now, enabled, paired, username, last_sent, error, failing_since):
    """status.json: read by the dashboard (world-readable), so no token, no chat id, no message text."""
    return {"ts": int(now), "enabled": bool(enabled), "paired": bool(paired), "username": username or None,
            "last_sent_ts": last_sent, "last_error": (clean(error, 160) or None) if error else None, "failing_since": failing_since}


def write_status(d, data):
    """Best effort: a folder that cannot be written (not installed, portable run) must not stop the notifier."""
    try:
        put(d, "status.json", json.dumps(data, indent=1) + "\n", 0o644)
        return True
    except OSError:
        return False


def load_sent(d):
    """{"keys": Counter | None, "stamps": [send times], "last_sent": ts | None}. None keys: a first run (no file, or a broken one)."""
    out = {"keys": None, "stamps": [], "last_sent": None}
    try:
        data = json.loads(read_private(os.path.join(secret_dir(d), "sent.json")))
        keys = data["keys"]
        if not all(isinstance(k, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0 for k, n in keys.items()):
            raise ValueError("keys")
        out["keys"] = collections.Counter(keys)
        out["stamps"] = [t for t in data.get("sent_ts", []) if isinstance(t, (int, float)) and not isinstance(t, bool)]
        last = data.get("last_sent_ts")
        out["last_sent"] = last if isinstance(last, (int, float)) and not isinstance(last, bool) else None
    except (OSError, ValueError, KeyError, AttributeError, TypeError, RecursionError):
        pass
    return out


# ---- the connection: HTTPS to one host, nothing else --------------------------------------------------------------------------

def tls_context():
    def make(cafile=None):
        ctx = ssl.create_default_context(cafile=cafile)  # verifies the certificate and the host name
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
    ctx = make()
    if nuc_config.MACOS and not ctx.cert_store_stats().get("x509_ca") and os.path.isfile(MAC_CAFILE):
        ctx = make(MAC_CAFILE)
    return ctx


def make_opener(ctx):
    """Plain HTTPS and the error handling, no more: no proxy taken from the environment, no redirect followed, no other scheme."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class HttpsTransport:
    """transport(method, payload) -> the decoded reply (a dict). Raises only TelegramError, whose text holds no token."""

    def __init__(self, token, opener=None):
        self._token = token
        self._opener = opener or make_opener(tls_context())

    def __repr__(self):
        return "HttpsTransport()"  # never the token

    def __call__(self, method, payload):
        if method not in METHODS:
            raise ValueError("method not allowed: " + clean(method, 30))
        url = f"https://{API_HOST}/bot{self._token}/{method}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "nuc-console-notify"})
        try:
            with self._opener.open(req, timeout=TIMEOUT_S + int(payload.get("timeout", 0))) as r:
                body = r.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as e:
            err = self._http_error(e)
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
            raise err from None
        except Exception as e:  # noqa: BLE001 - URLError, socket and TLS errors, a malformed reply: all become one error without the URL
            raise TelegramError(self._hide(f"{type(e).__name__}: {e}")) from None
        return self._decode(body)

    def _hide(self, text):
        return clean(redact(text, self._token), 200)

    def _decode(self, body):
        try:
            reply = json.loads(body[:MAX_REPLY].decode("utf-8"))
        except (ValueError, RecursionError):
            raise TelegramError("Telegram's reply is not JSON") from None
        if len(body) > MAX_REPLY or not isinstance(reply, dict):
            raise TelegramError("Telegram's reply is not what was expected")
        return reply

    def _http_error(self, e):
        try:
            body = json.loads(e.read(MAX_REPLY).decode("utf-8"))
        except Exception:  # noqa: BLE001 - the body is only an explanation
            body = None
        body = body if isinstance(body, dict) else {}
        params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry = params.get("retry_after")
        retry = retry if isinstance(retry, (int, float)) and not isinstance(retry, bool) else None
        return TelegramError(self._hide(f"HTTP {e.code}: {body.get('description') or e.reason or ''}").rstrip(": "), status=e.code, retry_after=retry)


class SendOnly:
    """The service's view of the transport: it can send and ask who the bot is. getUpdates (reading) does not exist for it."""
    ALLOWED = ("sendMessage", "getMe")

    def __init__(self, inner):
        self._inner = inner

    def __call__(self, method, payload):
        if method not in self.ALLOWED:
            raise ValueError("the notifier service may not call " + clean(method, 30))
        return self._inner(method, payload)


def check(reply):
    """A reply's `result`, or the TelegramError of a refusal."""
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        code = reply.get("error_code") if isinstance(reply, dict) else None
        text = clean(redact((reply.get("description") if isinstance(reply, dict) else "") or "unexpected reply"), 160)
        raise TelegramError(text, status=code if isinstance(code, int) and not isinstance(code, bool) else None)
    return reply.get("result")


def call(transport, method, payload, sleep=time.sleep, attempts=ATTEMPTS):
    """transport(method, payload) -> result. What can pass (no answer, 429, 5xx) is tried again, with a pause, a few times at most."""
    for n in range(max(1, attempts)):
        try:
            return check(transport(method, payload))
        except TelegramError as e:
            if n + 1 >= attempts or not e.retryable:
                raise
            sleep(min(e.retry_after or RETRY_PAUSE[min(n, len(RETRY_PAUSE) - 1)], 30))


def message_payload(chat_id, text):
    """Plain text: no parse_mode, so nothing in it is ever interpreted (no markup, no link built from data)."""
    return {"chat_id": chat_id, "text": TEXT_CHARS.sub(" ", text)[:MAX_TEXT], "disable_web_page_preview": True}


# ---- what changed: pure functions ---------------------------------------------------------------------------------------------

def problem_key(rec):
    """What makes two problems 'the same'. Port changes and counted problems: their exact text (one more is a new problem).
    The others: the fingerprint, in which the digits are ignored (temperatures and counters change every cycle)."""
    pid = rec["id"]
    return pid + "|" + (rec["text"] if pid in TEXT_KEYED else rec["fingerprint"])


def collect(records):
    """-> (Counter of key -> how many, {key: record}) of the problems nobody accepted. Ids repeat (several new ports): a multiset.
    The notifier's own problems (telegram-unpaired, telegram-failing) are not among them."""
    keys, recs = collections.Counter(), {}
    for r in records:
        if not r.get("accepted") and not r["id"].startswith(OWN):
            k = problem_key(r)
            keys[k] += 1
            recs[k] = r
    return keys, recs


class Debounce:
    """A change counts when it is seen in two consecutive cycles (a service restarting, a flapping port: no message)."""

    def __init__(self, start=None):
        self.stable = collections.Counter(start or ())
        self.prev = collections.Counter(start or ())
        self.cycles = 0

    def update(self, cur):
        for k in set(cur) | set(self.prev) | set(self.stable):
            if cur[k] == self.prev[k]:
                self.stable[k] = cur[k]
        self.prev = collections.Counter(cur)
        self.stable = +self.stable  # drops the zeros
        self.cycles += 1
        return collections.Counter(self.stable)


def diff(stable, sent):
    """-> (new, gone): what is confirmed now and was not told, what was told and is confirmed gone."""
    return stable - sent, sent - stable


def title_of(key):
    pid = key.split("|", 1)[0]
    return render.CATALOG[pid][0] if pid in render.CATALOG else clean(pid, 40)


def detail_of(key, rec):
    """The text of the problem (names, ports): only shown with [telegram] detail = full."""
    if rec is not None:
        return rec["text"]
    pid, _, rest = key.partition("|")
    return rest if pid in TEXT_KEYED else ""


def pick(counter, recs):
    """[(key, record | None)] one per count, the worst first (a record is None for a problem that is gone: only its key is left)."""
    out = []
    for key, n in counter.items():
        out += [(key, recs.get(key))] * n
    return sorted(out, key=lambda kr: (-SEV_RANK.get(kr[1]["severity"], 1) if kr[1] else 0, title_of(kr[0]), kr[0]))


def format_message(host, new, gone=(), detail="titles", started=None, held=False):
    """The text of one message. new / gone: [(key, record | None)]. started: the number of open problems of a first run.
    Titles only (the catalogue's own words) unless detail = full, which adds the problem's text."""
    lines = ["nuc-console · " + host]
    if started is not None:
        lines.append("Monitoring started: " + ("no open problems" if not started else ui.plural(started, "open problem")))
    if held:
        lines.append(f"Changes held back by the limit of {MAX_PER_HOUR} messages an hour:")
    new, gone, changed = list(new), list(gone), []
    for item in list(new):  # a problem whose count or text changed has an old key and a new one with the same title: one line
        old = next((g for g in gone if title_of(g[0]) == title_of(item[0])), None)
        if old is not None and started is None:
            new.remove(item)
            gone.remove(old)
            changed.append(item)
    shown = collections.OrderedDict()
    for label, items in (("OPEN" if started is not None else "NEW", new), ("CHANGED", changed), ("OK", gone)):
        for key, rec in items:
            text = LABEL[label] + ("‼ " if rec and label != "OK" and rec["severity"] in LOUD else "") + title_of(key)
            more = clean(detail_of(key, rec), 300) if detail == "full" else ""
            text += ": " + more if more else ""
            shown[text] = shown.get(text, 0) + 1
    body = [t + (f" (x{n})" if n > 1 else "") for t, n in shown.items()]
    lines += body[:MAX_LINES] + ([f"... and {len(body) - MAX_LINES} more"] if len(body) > MAX_LINES else [])
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def rate_room(stamps, now, limit=MAX_PER_HOUR, window=3600):
    """True when one more message fits in the last hour."""
    return sum(1 for t in stamps if now - t < window) < limit


# ---- the state that survives from one cycle to the next -----------------------------------------------------------------------

class ThermalHistory:
    """render.read_thermal() plus the throttling history that render.Sampler keeps (Sampler itself starts background threads that
    run loginctl, ss and statvfs: not for this process)."""

    def __init__(self, read=None, mono=time.monotonic):
        self.read, self.mono, self.hist = read or render.read_thermal, mono, collections.deque(maxlen=64)

    def __call__(self):
        th, now = self.read(), self.mono()
        if th.get("throttle") is not None:
            self.hist.append((now, th["throttle"]))
            old = next(((t, n) for t, n in self.hist if now - t <= render.THROTTLE_WINDOW_S), None)
            rec = th["throttle"] - old[1] if old and now - old[0] >= 10 else None  # events in the last minute; None while too short
            th["recent"] = rec if rec is None or rec >= 0 else None  # a falling sum: a CPU went offline
        return th


def current_records(thermal):
    """The problems of this moment, as `nuc-console-problems` sees them (the same functions)."""
    st = render.snapshot(200)
    try:
        th = thermal() if render.on("thermal") else {}
    except Exception:  # noqa: BLE001 - an unreadable sensor must not hide the other problems
        th = {}
    return render.problem_records(st["net"], st["cont"], boot=st["boot"], thermal=th, baseline=st["baseline"])


class Notifier:
    """Debounce, what the person was told (sent.json), the hourly limit and the failure state: one cycle at a time."""

    def __init__(self, transport, chat_id, d=None, host=None, clock=time.time, sleep=time.sleep, log=None):
        self.transport, self.chat_id, self.dir = transport, chat_id, d or NOTIFY_DIR
        self.host, self.clock, self.sleep = host or hostname(), clock, sleep
        self.log = log or (lambda text: print(text, flush=True))
        saved = load_sent(self.dir)
        self.sent, self.stamps, self.last_sent = saved["keys"], saved["stamps"], saved["last_sent"]
        self.debounce = Debounce(self.sent)  # what the person was told counts as already seen: a restart sends nothing again
        self.recs = {}
        self.blocked = False  # the hourly limit held a message back
        self.error = self.failing_since = self.problem = None  # why sending fails and since when; why the problems are unknown
        self.fails, self.not_before = 0, 0.0

    def cycle(self, records, cfg):
        now = self.clock()
        keys, seen = collect(records)
        self.recs.update(seen)
        stable = self.debounce.update(keys)
        if self.sent is None:  # the first run: one summary of what is open, no line per problem
            if self.debounce.cycles >= 2 and self.may_send(now):
                self.send(format_message(self.host, pick(stable, self.recs), (), cfg["detail"], started=sum(stable.values())), stable, now)
        else:
            new, gone = diff(stable, self.sent)
            told = gone if cfg["resolved"] else collections.Counter()
            if new or told:
                if self.may_send(now):
                    text = format_message(self.host, pick(new, self.recs), pick(told, self.recs), cfg["detail"], held=self.blocked)
                    self.send(text, stable, now)
            elif gone:  # gone problems the person does not want to hear about: just forget them
                self.sent = collections.Counter(stable)
                self.save()
            else:
                self.probe(now)
        for k in [k for k in self.recs if k not in keys and k not in stable and k not in (self.sent or ())]:
            del self.recs[k]

    def may_send(self, now):
        if now < self.not_before:  # still backing off after a failure
            return False
        self.stamps = [t for t in self.stamps if now - t < 3600]
        if not rate_room(self.stamps, now):
            self.blocked = True  # the changes stay unsent: they pile up, and go out together when there is room
            return False
        return True

    def send(self, text, target, now):
        try:
            call(self.transport, "sendMessage", message_payload(self.chat_id, text), self.sleep)
        except TelegramError as e:
            self.fail(e, now)
            return
        self.stamps.append(now)
        self.sent, self.last_sent, self.blocked = collections.Counter(target), int(now), False
        self.succeeded()
        self.save()
        self.log(f"sent a message ({len(text.splitlines()) - 1} lines)")

    def probe(self, now):
        """Nothing to tell while sending failed: ask Telegram who the bot is, so a recovery is noticed and the failure state ends."""
        if self.failing_since is None or now < self.not_before:
            return
        try:
            call(self.transport, "getMe", {}, self.sleep, attempts=1)
        except TelegramError as e:
            self.fail(e, now)
        else:
            self.succeeded()

    def fail(self, e, now):
        text = clean(redact(e, str(self.chat_id)), 160)
        if text != self.error:
            self.log("sending failed: " + text)
        self.error, self.fails = text, self.fails + 1
        self.failing_since = self.failing_since or int(now)
        self.not_before = now + min(300, 30 * 2 ** self.fails)  # 60 s, 2 min, 4 min, 5 min, 5 min...

    def succeeded(self):
        if self.error:
            self.log("sending works again")
        self.error = self.failing_since = None
        self.fails, self.not_before = 0, 0.0

    def save(self):
        data = {"keys": dict(self.sent), "sent_ts": [int(t) for t in self.stamps], "last_sent_ts": self.last_sent}
        try:
            put(secret_dir(self.dir), "sent.json", json.dumps(data) + "\n", 0o600)
        except OSError as e:
            self.log("cannot write sent.json: " + clean(redact(e), 120))

    def status(self, cfg, paired=True):
        return status_dict(self.clock(), True, paired, cfg["username"], self.last_sent, self.problem or self.error, self.failing_since)


def serve(d=None, clock=time.time, sleep=time.sleep, records=None, transport=None, cycles=None):
    """The service. Returns the exit code: 0 also when there is nothing to do (off, not paired), so no supervisor restarts it in a loop."""
    d = d or NOTIFY_DIR
    cfg = nuc_config.load()["telegram"]
    if not cfg["enabled"]:
        write_status(d, status_dict(clock(), False, False, cfg["username"], None, None, None))
        print("Telegram notifications are off ([telegram] enabled = no in config.ini)", flush=True)
        return 0
    token, chat, problem = load_credentials(d, cfg["username"])
    if problem:
        write_status(d, status_dict(clock(), True, False, cfg["username"], None, problem, None))
        print(problem, flush=True)
        return 0
    nt = Notifier(SendOnly(transport or HttpsTransport(token)), chat["chat_id"], d, clock=clock, sleep=sleep)
    thermal = ThermalHistory()
    records = records or (lambda: current_records(thermal))
    print(f"nuc-console notify: sending to @{chat['username']} (detail = {cfg['detail']}), looking every {CYCLE_S} s", flush=True)
    done = 0
    try:
        while True:
            cfg = nuc_config.load()["telegram"]  # the settings are read again every cycle
            if not cfg["enabled"] or cfg["username"] != chat["username"].lower():
                write_status(d, status_dict(clock(), cfg["enabled"], False, cfg["username"], nt.last_sent, None, None))
                print("Telegram notifications stopped: switched off or paired with someone else", flush=True)
                return 0
            try:
                recs, nt.problem = records(), None
            except Exception as e:  # noqa: BLE001 - an unexpected state is a line in status.json, never a crash loop
                nt.problem = clean(redact(f"cannot list the problems: {type(e).__name__}: {e}", str(chat["chat_id"])), 160)
            else:
                try:
                    nt.cycle(recs, cfg)
                except Exception as e:  # noqa: BLE001 - same here
                    nt.problem = clean(redact(f"internal error: {type(e).__name__}: {e}", str(chat["chat_id"])), 160)
            write_status(d, nt.status(cfg))
            done += 1
            if cycles is not None and done >= cycles:
                return 0
            sleep(CYCLE_S)
    except KeyboardInterrupt:
        return 0


# ---- pairing (--setup): the only place that reads updates ---------------------------------------------------------------------

def match_start(update, code, username):
    """{"chat_id", "username"} when update is `/start <code>` typed by @username in a private chat with the bot; else None."""
    msg = update.get("message") if isinstance(update, dict) else None
    chat, frm = (msg.get("chat"), msg.get("from")) if isinstance(msg, dict) else (None, None)
    if not (isinstance(chat, dict) and isinstance(frm, dict)) or chat.get("type") != "private" or frm.get("is_bot"):
        return None
    text, name, cid = msg.get("text"), frm.get("username"), chat.get("id")
    if not (isinstance(text, str) and isinstance(name, str) and isinstance(cid, int) and not isinstance(cid, bool)):
        return None
    if not hmac.compare_digest(text.encode("utf-8"), ("/start " + code).encode("utf-8")) or name.lower() != username or frm.get("id") != cid:
        return None
    return {"chat_id": cid, "username": username}


def pair(transport, code, username, clock=time.time, sleep=time.sleep, wait=PAIR_S):
    """Long polling until the right message arrives -> {"chat_id", "username"}, or None after `wait` seconds. Everything else that
    is written to the bot is ignored without a trace. The updates read are confirmed at the end, so they are not delivered again."""
    deadline, offset, errors, found = clock() + wait, None, 0, None
    try:
        while found is None and deadline - clock() > 0:
            payload = {"timeout": int(max(1, min(POLL_S, deadline - clock()))), "allowed_updates": ["message"], "limit": 20}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = call(transport, "getUpdates", payload, sleep, attempts=1)
            except TelegramError as e:
                errors += 1
                if not e.retryable or errors >= 5:
                    raise
                sleep(3)
                continue
            errors = 0
            for u in updates if isinstance(updates, list) else ():
                uid = u.get("update_id") if isinstance(u, dict) else None
                if isinstance(uid, int) and not isinstance(uid, bool):
                    offset = max(offset or 0, uid + 1)
                found = found or match_start(u, code, username)
    finally:
        if offset is not None:
            try:
                call(transport, "getUpdates", {"offset": offset, "timeout": 0, "limit": 1}, sleep, attempts=1)
            except TelegramError:
                pass
    return found


class Terminal:
    """The person at the keyboard. Piped input (automation) is read line by line; a token typed on a terminal is not echoed."""

    def say(self, text=""):
        print(text, flush=True)

    def ask(self, prompt):
        if sys.stdin.isatty():
            return input(prompt)
        print(prompt, end="", flush=True)
        return sys.stdin.readline().rstrip("\r\n")

    def secret(self, prompt):
        return getpass.getpass(prompt) if sys.stdin.isatty() else self.ask(prompt)


def is_admin():
    if nuc_config.WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def need_admin(flag):
    """None when allowed, else what to tell the person. NUC_CONSOLE_NOTIFY_DIR (tests, a try-out in a folder of your own) lifts it."""
    if is_admin() or os.environ.get("NUC_CONSOLE_NOTIFY_DIR"):
        return None
    how = "run it from an administrator prompt" if nuc_config.WINDOWS else f"run it as root: sudo nuc-console-telegram {flag}"
    return f"{flag} changes the machine's settings: {how}"


def config_path():
    return os.environ.get("NUC_CONSOLE_CONFIG", nuc_config.DEFAULT_PATH)


def set_config(term, **values):
    """[telegram] key = value in config.ini, keeping every other line. -> True when written."""
    path = config_path()
    try:
        for key, value in values.items():
            nuc_config.set_key(path, "telegram", key, value)
    except OSError as e:
        term.say(f"cannot write {path}: {clean(e, 120)}. Set it by hand: [telegram] " + ", ".join(f"{k} = {v}" for k, v in values.items()))
        return False
    return True


def service_user():
    """(uid, gid) of the unprivileged service user the installer creates, or None."""
    try:
        import pwd
        pw = pwd.getpwnam("_nuc-console" if nuc_config.MACOS else "nuc-console-notify")  # Linux: its own user, not the web view's
        return pw.pw_uid, pw.pw_gid
    except (ImportError, KeyError):
        return None


def prepare_dir(d):
    """The folder exists: 0711 (others reach status.json, they cannot list the secrets), owned by the service user."""
    created = not os.path.isdir(d)  # a folder the installer made keeps its mode and owner
    os.makedirs(secret_dir(d), exist_ok=True)  # Windows: the installer made both, with their own ACLs
    if created and POSIX:
        os.chmod(d, 0o711)
        user = service_user() if os.geteuid() == 0 else None
        if user:
            os.chown(d, *user)


def service_control(action):
    """Starts, restarts or stops the notifier service with a fixed command line. -> (ok, why not). Never fatal for the caller."""
    env = dict(os.environ, PATH="/usr/sbin:/usr/bin:/sbin:/bin") if POSIX else None  # a fixed PATH: the commands are looked up there
    ok, why = False, ""
    for argv in SERVICE[nuc_config.OS_NAME][action]:
        if not POSIX:  # the system folder, not whatever the current directory holds
            argv = [os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", argv[0] + ".exe")] + argv[1:]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
            ok, why = r.returncode == 0, clean(r.stderr or r.stdout, 160)
        except (OSError, subprocess.SubprocessError) as e:
            ok, why = False, clean(e, 160)
    return ok, why


def apply_service(term, action):
    ok, why = service_control(action)
    if ok:
        term.say("the notifier service was " + ("stopped" if action == "stop" else "(re)started"))
    else:
        term.say(f"could not {action} the notifier service ({why or 'no answer'}): is it installed? Run it by hand: python3 notify.py")
    return ok


def setup(term, make_transport, d=None, clock=time.time, sleep=time.sleep, wait=PAIR_S):
    """--setup: token, @username, Start in Telegram. -> exit code."""
    d, token = d or NOTIFY_DIR, ""
    refusal = need_admin("--setup")
    if refusal:
        term.say(refusal)
        return 2
    term.say(INTRO)
    term.say()
    try:
        token = term.secret("Bot token (not shown): ").strip()
        if not TOKEN_RE.fullmatch(token):
            term.say("that is not a bot token (it looks like 123456789:AAH...): copy it again from @BotFather")
            return 2
        tg = make_transport(token)
        me = call(tg, "getMe", {}, sleep)
        bot = me.get("username") if isinstance(me, dict) else None
        if not (isinstance(bot, str) and re.fullmatch(r"[A-Za-z0-9_]{3,64}", bot)):
            term.say("Telegram did not name the bot: is this the token of a bot?")
            return 1
        term.say(f"bot @{bot} found")
        default = nuc_config.load()["telegram"]["username"]
        user = (term.ask(f"Your Telegram @username [{'@' + default if default else ''}]: ").strip().lstrip("@") or default).lower()
        if not USER_RE.fullmatch(user):
            term.say("a Telegram @username has 5 to 32 letters, digits or _")
            return 2
        prepare_dir(d)
        try:
            os.remove(os.path.join(secret_dir(d), "chat.json"))  # a new token, a new pairing: the old chat belongs to the old bot
        except FileNotFoundError:
            pass
        put(secret_dir(d), "token", token + "\n", 0o600)
        code = secrets.token_urlsafe(16)  # only A-Za-z0-9_-, what a /start link may carry
        term.say()
        term.say("Open this link on your phone and press Start:")
        term.say(f"  https://t.me/{bot}?start={code}")
        term.say(f"Waiting up to {ui.plural(max(1, wait // 60), 'minute')} for it (Ctrl+C to cancel)...")
        chat = pair(tg, code, user, clock, sleep, wait)
        if chat is None:
            term.say("Nobody pressed Start in time: nothing was paired. The token is saved: run --setup again.")
            return 1
        chat.update(bot=bot, paired_ts=int(clock()))
        put(secret_dir(d), "chat.json", json.dumps(chat) + "\n", 0o600)
    except TelegramError as e:
        term.say("Telegram: " + str(e) + (" (a bot with a webhook cannot be paired: remove it with @BotFather)" if e.status == 409 else ""))
        return 1
    except (KeyboardInterrupt, EOFError):
        term.say("\ncancelled")
        return 1
    except OSError as e:
        term.say(f"cannot write in {d}: {clean(redact(e, token), 120)}")
        return 1
    term.say(f"paired with @{user}")
    written = set_config(term, enabled="yes", username="@" + user)
    try:
        call(tg, "sendMessage", message_payload(chat["chat_id"], f"nuc-console {hostname()}: paired. You will receive the ATTENTION changes of "
                                                                 "this machine; it never reads your messages."), sleep)
    except TelegramError as e:
        term.say("paired, but the greeting could not be sent: " + str(e))
    apply_service(term, "restart")
    term.say("Tip: in @BotFather send /setjoingroups, pick your bot and choose Disable, so nobody can add it to a group.")
    term.say("Try it: nuc-console-telegram --test")
    return 0 if written else 1


# ---- the other commands -------------------------------------------------------------------------------------------------------

def read_status(d):
    try:
        with open(os.path.join(d, "status.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def status_report(d, now=None):
    """What --status shows (no secret in it): the settings in force, status.json, and whether the notifier looks alive."""
    now, cfg, st = now or time.time(), nuc_config.load()["telegram"], read_status(d)
    bot = None
    try:
        bot = read_chat(d).get("bot")
    except (OSError, ValueError):
        pass  # the pairing is only readable by root and the service user
    ts = (st or {}).get("ts")
    alive = isinstance(ts, (int, float)) and 0 <= now - ts < STALE_S
    st = st or {}
    return {"enabled": cfg["enabled"], "username": cfg["username"] or None, "detail": cfg["detail"], "resolved": cfg["resolved"],
            "paired": bool(st["paired"]) if "paired" in st else None, "bot": bot if isinstance(bot, str) else None, "running": alive,
            "ts": ts, "last_sent_ts": st.get("last_sent_ts"), "last_error": (clean(st["last_error"], 160) or None) if st.get("last_error") else None,
            "failing_since": st.get("failing_since")}


def ago(sec):
    sec = int(max(0, sec))
    return f"{sec} s" if sec < 120 else f"{sec // 60} min" if sec < 7200 else f"{sec // 3600} h" if sec < 172800 else f"{sec // 86400} d"


def print_status(d, as_json=False, now=None):
    now = now or time.time()
    s = status_report(d, now)
    if as_json:
        print(json.dumps(s, indent=1))
        return 0

    def when(ts):
        return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} ({ago(now - ts)} ago)" if isinstance(ts, (int, float)) else "never"
    print("Telegram notifications: " + ("on" if s["enabled"] else "off"))
    print(f"  username:    {'@' + s['username'] if s['username'] else '(not set)'}")
    if s["paired"] is None:
        paired = "unknown (the notifier has not written its status yet)"
    else:
        paired = ("yes" + (f" (bot @{s['bot']})" if s["bot"] else "")) if s["paired"] else "no: run nuc-console-telegram --setup"
    print("  paired:      " + paired)
    print(f"  content:     {s['detail']} ({'the titles of the problems' if s['detail'] == 'titles' else 'titles and the problems text'}), "
          f"resolved {'yes' if s['resolved'] else 'no'}")
    if s["running"]:
        notifier = f"running (status {ago(now - s['ts'])} old)"
    else:
        notifier = "not running" + (f" (last status {ago(now - s['ts'])} ago)" if isinstance(s["ts"], (int, float)) else "")
    print("  notifier:    " + notifier)
    print(f"  last sent:   {when(s['last_sent_ts'])}")
    print(f"  last error:  {s['last_error'] or 'none'}" + (f" (failing since {when(s['failing_since'])})" if s["failing_since"] else ""))
    return 0


def send_test(term, make_transport, d=None, sleep=time.sleep):
    d = d or NOTIFY_DIR
    token, chat, problem = load_credentials(d, nuc_config.load()["telegram"]["username"])
    if problem:
        term.say(problem)
        return 1
    try:
        call(make_transport(token), "sendMessage", message_payload(chat["chat_id"], f"nuc-console {hostname()}: test message. If you read "
                                                                                    "this, the notifications work."), sleep)
    except TelegramError as e:
        term.say("not sent: " + str(e))
        return 1
    term.say(f"test message sent to @{chat['username']}")
    return 0


def switch(term, on, d=None):
    """--on / --off: enabled = yes|no in config.ini, and the service follows."""
    refusal = need_admin("--on" if on else "--off")
    if refusal:
        term.say(refusal)
        return 2
    if not set_config(term, enabled="yes" if on else "no"):
        return 1
    term.say("Telegram notifications are " + ("on" if on else "off"))
    if on and load_credentials(d or NOTIFY_DIR, nuc_config.load()["telegram"]["username"])[2]:
        term.say("not paired yet: run nuc-console-telegram --setup")
    apply_service(term, "restart" if on else "stop")
    return 0


def forget(term, d=None):
    """--forget: no token, no pairing, off."""
    d = d or NOTIFY_DIR
    refusal = need_admin("--forget")
    if refusal:
        term.say(refusal)
        return 2
    apply_service(term, "stop")  # first: a running notifier would write its files again
    failed = False
    for name in ("token", "chat.json", "sent.json"):
        try:
            os.remove(os.path.join(secret_dir(d), name))
        except FileNotFoundError:
            pass
        except OSError as e:
            term.say(f"cannot delete {name}: {clean(e, 120)}")
            failed = True
    cfg = nuc_config.load()["telegram"]
    if not set_config(term, enabled="no"):
        failed = True
    write_status(d, status_dict(time.time(), False, False, cfg["username"], None, None, None))
    term.say("token and pairing deleted, notifications off. To undo the pairing on Telegram: /revoke in @BotFather, or delete the bot.")
    return 1 if failed else 0


def preview(opts):
    """--preview [--demo [--demo-os windows|darwin]]: the message the problems of this moment would send. Nothing is sent."""
    if opts["demo"]:
        render.DEMO, render.DEMO_OS = True, opts["demo_os"]
        render.demo_defaults()
    cfg = nuc_config.load()["telegram"]
    keys, recs = collect(current_records(ThermalHistory()))
    print(format_message(hostname(), pick(keys, recs), (), cfg["detail"]))
    print(f"\n(preview: nothing was sent; detail = {cfg['detail']})")
    return 0


FLAGS = ("--setup", "--test", "--status", "--on", "--off", "--forget", "--preview", "--enabled")


def parse(args):
    """-> (command | None, {"json", "demo", "demo_os"}), or None when the arguments make no sense."""
    cmds, opts, i = [], {"json": False, "demo": False, "demo_os": None}, 0
    while i < len(args):
        a = args[i]
        if a in FLAGS:
            cmds.append(a)
        elif a in ("--json", "--demo"):
            opts[a[2:]] = True
        elif a == "--demo-os" and i + 1 < len(args) and args[i + 1] in ("windows", "darwin"):
            opts["demo_os"] = args[i + 1]
            i += 1
        else:
            return None
        i += 1
    if len(cmds) > 1 or (opts["json"] and cmds != ["--status"]) or ((opts["demo"] or opts["demo_os"]) and cmds != ["--preview"]):
        return None
    return (cmds[0] if cmds else None), opts


def main(argv):
    args = list(argv[1:])
    if "--log" in args[:-1]:  # Windows / macOS: no journal
        i = args.index("--log")
        nuc_config.log_to(args[i + 1])
        del args[i:i + 2]
    render.utf8_stdout()
    if "--help" in args or "-h" in args:
        print(HELP, end="")
        return 0
    parsed = parse(args)
    if parsed is None:
        print(HELP, end="", file=sys.stderr)
        return 2
    cmd, opts = parsed
    term = Terminal()
    if cmd == "--enabled":  # for the installers
        return 0 if nuc_config.load()["telegram"]["enabled"] else 1
    if cmd == "--status":
        return print_status(NOTIFY_DIR, opts["json"])
    if cmd == "--preview":
        return preview(opts)
    transport = lambda token: HttpsTransport(token)  # noqa: E731
    if cmd == "--setup":
        return setup(term, transport)
    if cmd == "--test":
        return send_test(term, transport)
    if cmd in ("--on", "--off"):
        return switch(term, cmd == "--on")
    if cmd == "--forget":
        return forget(term)
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
