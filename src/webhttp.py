"""nuc-console web view: the HTTP side. The security of the view and the request handler that applies it.

What is here, moved from web.py without a line of its logic changed: the bind and token checks (`is_loopback`, `read_token`, `check_bind`), the
DNS-rebinding guard (`host_ok`) and the Origin / Referer comparison (`same_host`), the Content-Security-Policy (`page_csp`, `CSP`), the limits of a
request (`DEADLINE_S`, `POST_MAX`), the table of the forms that may be posted (`POST_AREAS`), and `Handler`: GET (the pages, the data API, its stream,
the style sheets), POST (the forms of the AI, Telegram and settings pages, behind the token, the CSRF token, Origin / Referer / Sec-Fetch-Site and
a 4 KB body). The pages themselves are built by the `Server` of web.py, which the handler reaches as `self.server`; this module does not import it."""
import hashlib
import hmac
import http.server
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import advisor
import appjs
import cards
import nuc_config
import prefs
import render
import tgweb
import webapi
import webcss
import webjs
from weburl import APP_PATH, NUM, app_url, view_params, view_url

MIN_TOKEN = 16
TOKEN_OK = re.compile(r"[A-Za-z0-9._~-]{16,}")   # cookie- and URL-safe


DEADLINE_S = 15      # total time a single request may take (slowloris)
API_PATH = "/api/v1"  # the data API: /api/v1 (what there is), /api/v1/<view> (JSON), /api/v1/stream?view=<view> (Server-Sent Events)


RETRY_MS = 3000      # what a stream tells the client to wait before it reconnects
ASSET_FILES = {"%s.%s.css" % (name, sha[:8]): (body, ctype) for name, (body, ctype, sha) in webcss.ASSETS.items()}  # /s/<name>.<sha8>.<ext>
ASSET_CACHE = "private, max-age=31536000, immutable"  # the name carries the hash: a changed sheet is another URL
UI_COOKIE_AGE = 31536000  # the appearance cookie lives a year
AI_ACTIONS = ("on", "off", "use", "cancel", "delete", "delete-all", "ask", "advise", "clear", "load")  # POST /ai/<action>
TG_ACTIONS = ("pair", "cancel", "on", "off", "test")  # POST /telegram/<action>
SETTINGS_ACTIONS = ("feature", "config")  # POST /settings/<action>: a portable run's config.ini (the desktop app is one): a switch, a section
POST_AREAS = {"ai": AI_ACTIONS, "telegram": TG_ACTIONS, "settings": SETTINGS_ACTIONS}
POST_MAX = 4096      # bytes of a form: a question is 500 characters, everything else is an id


def page_csp(scripts=(), shell=False, forms=False):
    """The Content-Security-Policy of one page, composed from what the page carries: the one place a policy is written.
    scripts: the texts of the inline scripts the page has; script-src lists exactly their SHA-256 and nothing else, and a page without
    one has no script-src (default-src 'none' then forbids every script). connect-src 'self' only where a script talks to this server
    (refresh, preferences, layout editor); where the refresh script is, Trusted Types: its one policy, nuc-frag, is the only way to hand
    markup to the parser. shell: the page loads /s/app.<sha8>.css and has no inline style at all (no <style>, no style= attribute: style-src 'self'). forms: the page's forms post to this server (the AI page) and nowhere else."""
    scripts = [s for s in scripts if s]
    if appjs.APP_JS in scripts:
        forms = True  # its screens' buttons are forms (posted by the app, or by the browser when it does not run)
    parts = ["default-src 'none'", "style-src " + ("'self'" if shell else "'unsafe-inline'"), "base-uri 'none'",
             "form-action " + ("'self'" if forms else "'none'"), "frame-ancestors 'none'"]
    if scripts:
        parts.append("script-src " + " ".join(dict.fromkeys(webjs.csp_source(s) for s in scripts)))
    if any(s in (webjs.REFRESH_JS, webjs.PREFS_JS, webjs.BUILDER_JS, appjs.APP_JS) for s in scripts):
        parts.append("connect-src 'self'")
    if webjs.REFRESH_JS in scripts:
        parts += ["require-trusted-types-for 'script'", "trusted-types nuc-frag"]
    elif appjs.APP_JS in scripts:  # the app builds its elements one by one and never hands markup to the parser: no policy at all
        parts += ["require-trusted-types-for 'script'", "trusted-types 'none'"]
    return "; ".join(parts)


CSP = page_csp()  # the classic pages: no script, no connection, no form


def is_loopback(bind):
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def read_token(path):
    """Token from a file (never from config.ini, which is world-readable). Returns '' if no file is configured."""
    if not path:
        return ""
    with open(path) as f:
        st = os.fstat(f.fileno())
        if not nuc_config.WINDOWS:  # Windows has no mode bits: install-windows.ps1 protects the folder with an ACL instead
            if st.st_mode & 0o077:
                raise ValueError(f"{path} must not be readable by group/others (chmod 600)")
            if st.st_uid not in (0, os.geteuid()):
                raise ValueError(f"{path} must be owned by root or by the service user")
        tok = f.read().strip()
    if not TOKEN_OK.fullmatch(tok):
        raise ValueError(f"token in {path} must be {MIN_TOKEN}+ characters from A-Z a-z 0-9 . _ ~ -")
    return tok


LOCAL_TOKEN = "web.token"  # in the data folder of a portable run / the desktop app: the access token of the loopback view


def local_token(path):
    """The access token of a portable run's loopback view (run.sh, run.ps1, the desktop app), where no [web] token_file is meant to exist:
    one per data folder, created here the first time (secrets.token_urlsafe, 0600, owner only; Windows: an ACL for this user), read back
    by the next runs (the browser and the desktop app hold it as a cookie) and by the launchers of the same account. Delete the file for a
    new one. A file that is not a good token, or that others can read, is an error (read_token): it is not replaced behind your back."""
    try:
        return read_token(path)
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another start of this folder won the race
        return read_token(path)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    if nuc_config.WINDOWS:  # no mode bits: only this user (and SYSTEM) may read it
        icacls = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "icacls.exe")
        who = (os.environ.get("USERDOMAIN", "") + "\\" if os.environ.get("USERDOMAIN") else "") + os.environ.get("USERNAME", "")
        done = who and subprocess.run([icacls, path, "/inheritance:r", "/grant:r", who + ":F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not done:  # fail closed: a token others may read protects nothing
            os.remove(path)
            raise ValueError(f"could not restrict {path} to your account (icacls)")
    return token


def check_bind(bind, token):
    """Fail closed: a non-loopback listener without a token would publish the topology of the machine."""
    if not is_loopback(bind) and not token:
        raise ValueError(f"refusing to listen on {bind} without a token: set [web] token_file, or bind to 127.0.0.1 "
                         "and publish with `tailscale serve`")


def same_host(value, host):
    """An Origin or a Referer ('http://host:port[/path]'): is that the host:port the request was addressed to? 'null', another port, another
    name, a user name in front, anything that is not http(s): no."""
    try:
        u = urlsplit(value or "")
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and u.netloc.lower() == (host or "").strip().lower()


class BadRequest(ValueError):
    """A form that is not one this page makes (an id the catalog has not, a number that is not one): 400."""


def host_ok(host_header, allowed):
    """DNS-rebinding guard: only expected Host names are served (a rebinding page arrives with the attacker's name)."""
    h = (host_header or "").strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return h in allowed or h.endswith(".ts.net")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "nuc-console"
    sys_version = ""
    timeout = 10

    def handle(self):  # timeout above is per recv(): add a total deadline so a trickling client can't hold a thread
        t = self._deadline = threading.Timer(DEADLINE_S, self._kill)  # a stream (/api/v1/stream) cancels it and keeps its own end
        t.daemon = True
        t.start()
        try:
            super().handle()
        finally:
            t.cancel()

    def _kill(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def log_message(self, *a):  # no access log: URLs may carry a token
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=(), csp=CSP, referrer="no-referrer", cache="no-store"):
        self._head(code, ctype, None if code in (204, 304) else len(body), extra, csp, referrer, cache)  # 204, 304: no body, and no length either
        if body and self.command != "HEAD":  # HEAD: the headers of the GET (Content-Length included), no body
            self.wfile.write(body)

    def _head(self, code, ctype, length, extra=(), csp=CSP, referrer="no-referrer", cache="no-store"):
        """The status line and the headers every answer has (length None: no Content-Length, a stream's or a 204's)."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (("Cache-Control", cache), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", referrer),
                     ("Content-Security-Policy", csp), ("X-Frame-Options", "DENY")) + tuple(extra):
            self.send_header(k, v)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")  # no other site can embed or read this
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.end_headers()

    def _token_from(self, query):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nuc_token":
                return v
        return (query.get("token") or [""])[0]

    def _secure(self):
        """'; Secure' when the browser reached this server over HTTPS (a proxy in front says so: tailscale serve and others send
        X-Forwarded-Proto: https), else ''. Never on plain http, and loopback is plain http: a browser drops a Secure cookie that comes over
        http, and the dashboard would ask for the token on every click. The header can only make a cookie stricter, so it needs no trusted-peer list."""
        return "; Secure" if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https" else ""

    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _set(self, q):
        """/?set=<field>&back=<view>: one appearance preference into the nuc_ui cookie (validated by prefs.apply_set, nothing else is stored), then
        a redirect to the view `back` names, rebuilt from its validated parameters (never the text given: no open redirect). set=reset clears it."""
        field = (q.get("set") or [""])[0]
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):  # a link on another site does not change this one's look
            return self._send(403, b"a link of another site cannot change the preferences\n")
        whole = field.startswith(prefs.COOKIE_VERSION + ".") or field == prefs.COOKIE_VERSION  # PREFS_JS sends back the string it was given: all of it
        step = prefs.edit_parts(field) is not None  # one step of the layout editor (e<op><card>, ereset)
        if whole and (not prefs.parse_cookie(field) and field != prefs.COOKIE_VERSION):
            return self._send(400, b"not a preference\n")
        if not whole and not step and field != "reset" and ("." in field or not prefs.parse_cookie("1." + field)):
            return self._send(400, b"not a preference\n")
        current = self._cookie(prefs.COOKIE_NAME)
        if step:  # only the layout changes; the step is applied to the layout in force (cookie, else config.ini, else the preset)
            value = prefs.apply_edit(current, field, render.CFG.get("ui"), [c for c in prefs.CARDS if cards.enabled(c, render.CFG)])
        else:
            value = prefs.dump_cookie(prefs.parse_cookie(field)) if whole else prefs.apply_set(current, field)
        try:
            back = parse_qs((q.get("back") or [""])[0][:400], max_num_fields=40)
        except ValueError:
            back = {}
        keep = (f"{prefs.COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={UI_COOKIE_AGE}" if value != prefs.COOKIE_VERSION
                else f"{prefs.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0") + self._secure()  # nothing left to remember: the cookie goes
        if (q.get("frag") or [""])[0] == "1":  # a script's request: no redirect, the page changes itself; X-Nuc-Prefs is what it keeps in the browser
            return self._send(204, extra=(("Set-Cookie", keep), ("X-Nuc-Prefs", value)))
        self._send(302, extra=(("Location", view_url(view_params(back))), ("Set-Cookie", keep)))

    def _asset(self, path):
        """/s/<name>.<sha8>.css: the shell's style sheet, immutable (an unknown name or hash: 404)."""
        hit = ASSET_FILES.get(path[3:])
        if hit is None:
            return self._send(404, b"not found\n")
        self._send(200, hit[0], hit[1], cache=ASSET_CACHE)

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        srv = self.server
        if u.path == "/healthz":
            return self._send(200, b"ok\n")
        area, _sep, act = u.path[1:].partition("/")
        if act in POST_AREAS.get(area, ()):
            return self._send(405, b"post only\n", extra=(("Allow", "POST"),))
        api = u.path == API_PATH or u.path.startswith(API_PATH + "/")
        if u.path not in ("/", APP_PATH) and not u.path.startswith("/s/") and not api:
            return self._send(404, b"not found\n")
        if not srv.token and not host_ok(self.headers.get("Host"), srv.allowed):
            return self._send(421, b"misdirected request: add this name to [web] allowed_hosts\n")
        if srv.token:
            given = self._token_from(q)
            if not hmac.compare_digest(given.encode(), srv.token.encode()):
                return self._send(401, b"unauthorized\n", extra=(("WWW-Authenticate", 'Bearer realm="nuc-console"'),))
            if "token" in q and u.path in ("/", APP_PATH):  # move the token out of the URL (history, logs, referrers) into a cookie; the view stays (validated parameters only)
                where = view_url(view_params(q)) if u.path == "/" else app_url(view_params(q))
                return self._send(302, extra=(("Location", where), ("Set-Cookie",
                                  f"nuc_token={given}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000" + self._secure())))
        if api:
            return self._api(u.path[len(API_PATH):].strip("/"), q)
        if u.path == APP_PATH:
            page = srv.app_page(self._cookie(prefs.COOKIE_NAME), q)
            return self._send(200, page.encode(), "text/html; charset=utf-8", csp=page.csp, referrer="same-origin", extra=(("Vary", "Cookie"),))
        if u.path != "/":
            return self._asset(u.path)
        if "set" in q:
            return self._set(q)
        if (q.get("view") or [""])[0] == "telegram" and (q.get("open") or [""])[0] == "1":
            return self._tg_open()
        page = srv.page(cookie=self._cookie(prefs.COOKIE_NAME), **view_params(q))
        if (q.get("frag") or [""])[0] == "1" and getattr(page, "blocks", None) is not None:  # a classic page has no blocks: it is served whole, as always
            return self._fragment(page.blocks)
        self._send(200, page.encode(), "text/html; charset=utf-8", csp=getattr(page, "csp", CSP), referrer=getattr(page, "referrer", "no-referrer"),
                   extra=(("Vary", "Cookie"),))

    def _json(self, code, obj, extra=()):
        self._send(code, webapi.dump(obj), webapi.JSON_TYPE, extra=extra)

    def _api(self, name, q):
        """The data API, after the checks every request has (Host or token): /api/v1 (what there is), /api/v1/<view> (its document, with an ETag:
        304 when If-None-Match has it), /api/v1/stream (_stream). Read-only. A request a browser marks as coming from another site is refused (no
        page of another site reads this, and none can make it work for it either)."""
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return self._json(403, {"error": "a page of another site cannot read this server's data"})
        srv = self.server
        if name == "":
            return self._json(200, srv.api_index())
        if name == "stream":
            return self._stream(q)
        if name not in webapi.VIEWS:
            return self._json(404, {"error": "no such view: one of " + ", ".join(webapi.VIEWS)})
        try:
            doc = srv.api(name, q)
        except Exception as e:  # noqa: BLE001 - a broken state must not take the server down
            print("nuc-console web: api %s error: %r" % (name, e), file=sys.stderr)  # detail to the journal, not to the answer
            return self._json(500, {"error": "the data could not be read (see the service log)"})
        if doc is None:
            return self._json(404, {"error": "this screen is off in config.ini ([features] %s = no)" % name})
        tag = '"%s"' % doc.rev
        if tag in [t.strip() for t in self.headers.get("If-None-Match", "").split(",")]:
            return self._send(304, extra=(("ETag", tag),))
        self._send(200, doc, webapi.JSON_TYPE, extra=(("ETag", tag),))

    def _stream(self, q):
        """/api/v1/stream?view=<view> (and that view's parameters): Server-Sent Events. Each event is the view's document, sent when its rev changes
        (the first at once, unless Last-Event-ID is already its rev); a comment every KEEPALIVE_S when nothing changed. view may be given up to
        STREAM_VIEWS_MAX times (a page: its view and the summary): each event is then one of them (its "view" says which). A stream holds a
        connection and a thread: STREAMS_MAX at once (more: 503), and it ends after STREAM_S, the request deadline lifted (the client reconnects
        after RETRY_MS, with the last id). A client that stops reading is dropped by the socket's timeout."""
        srv = self.server
        names = []
        for v in q.get("view") or ["overview"]:
            if (v or "overview") not in names:
                names.append(v or "overview")
        if len(names) > webapi.STREAM_VIEWS_MAX:
            return self._json(400, {"error": "a stream carries at most %d views" % webapi.STREAM_VIEWS_MAX})
        bad = [v for v in names if v not in webapi.VIEWS]
        if bad:
            return self._json(404, {"error": "no such view: one of " + ", ".join(webapi.VIEWS)})
        try:
            off = [v for v in names if srv.api(v, q) is None]
        except Exception as e:  # noqa: BLE001
            print("nuc-console web: api %s error: %r" % (names, e), file=sys.stderr)
            return self._json(500, {"error": "the data could not be read (see the service log)"})
        if off:
            return self._json(404, {"error": "this screen is off in config.ini ([features] %s = no)" % off[0]})
        if not srv.streams.acquire(blocking=False):
            return self._json(503, {"error": "too many streams are open: try again later"}, extra=(("Retry-After", "10"),))
        try:
            self._deadline.cancel()
            self._head(200, webapi.STREAM_TYPE, None)
            if self.command == "HEAD":
                return
            given = self.headers.get("Last-Event-ID", "").strip()
            last = {names[0]: given} if len(names) == 1 and webapi.EVENT_ID.fullmatch(given) else {}  # an id is one view's rev
            tick = srv.stream_tick or (1.0 if {"ai", "telegram"} & set(names) else srv.cfg["refresh_seconds"])  # a job's progress moves every second
            self.wfile.write(webapi.retry(RETRY_MS))
            start = quiet = time.monotonic()
            while True:
                now = time.monotonic()
                for name in names:
                    try:
                        doc = srv.api(name, q)
                    except Exception as e:  # noqa: BLE001 - one failed read: the stream waits for the next
                        print("nuc-console web: api %s error: %r" % (name, e), file=sys.stderr)
                        doc = None
                    if doc is not None and doc.rev != last.get(name):
                        self.wfile.write(webapi.event(doc))
                        last[name], quiet = doc.rev, now
                if now - quiet >= srv.keepalive_s:
                    self.wfile.write(webapi.comment("keepalive"))
                    quiet = now
                if now - start >= srv.stream_s:
                    return
                time.sleep(max(0.01, min(tick, srv.stream_s - (now - start))))
        except OSError:  # the client went away, or stopped reading (the socket's timeout)
            return
        finally:
            srv.streams.release()

    def _tg_open(self):
        """/?view=telegram&open=1: the t.me link of the pairing that waits for Start (a link on the page that the refresh can keep: it points
        here). The address is the engine's own (bot name and code checked, never from the request); nothing else: back to the page."""
        site = self.headers.get("Sec-Fetch-Site")
        link = tgweb.engine().link() if site in (None, "same-origin", "none") else ""
        if link and tgweb.BOT_LINK.fullmatch(link):
            return self._send(302, extra=(("Location", link),))
        self._send(303, extra=(("Location", "/?view=telegram"),))

    def _fragment(self, blocks):
        """?frag=1 on a shell page: only its blocks (what the refresh script swaps in), with an ETag of them: the same tag in If-None-Match is a 304."""
        tag = '"%s"' % hashlib.sha256(blocks.encode("utf-8")).hexdigest()[:20]
        extra = (("ETag", tag), ("X-Nuc-Fragment", "1"), ("Vary", "Cookie"))
        sent = [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]
        if tag in sent or "*" in sent or "W/" + tag in sent:
            return self._send(304, extra=extra)
        self._send(200, blocks.encode(), "text/html; charset=utf-8", extra=extra)

    def do_POST(self):  # noqa: N802 - the forms of the AI page, of the Telegram page and of a portable run's settings, and nothing else
        u = urlsplit(self.path)
        area, sep, act = u.path[1:].partition("/")
        if not sep or area not in POST_AREAS or u.query:
            return self._no()
        srv = self.server
        if act not in POST_AREAS[area]:
            return self._send(404, b"not found\n")
        # the same access as viewing: no token = loopback and a known Host name; a token = that token (constant time)
        if not srv.token and not host_ok(self.headers.get("Host"), srv.allowed):
            return self._send(421, b"misdirected request: add this name to [web] allowed_hosts\n")
        if srv.token and not hmac.compare_digest(self._token_from({}).encode(), srv.token.encode()):
            return self._send(401, b"unauthorized\n", extra=(("WWW-Authenticate", 'Bearer realm="nuc-console"'),))
        if area == "ai" and not render.CFG["features"].get("ai", True):
            return self._send(404, b"the AI screen is off ([features] ai = no)\n")
        if area == "ai" and not advisor.web_actions_on(render.CFG):
            return self._send(403, b"locked by config.ini ([ai] web_actions = no)\n")
        if area == "telegram" and not render.CFG["telegram"].get("web_actions", True):
            return self._send(403, b"locked by config.ini ([telegram] web_actions = no)\n")
        if area == "settings" and not nuc_config.features_writable()[0]:
            return self._send(403, ("config.ini is not this page's to write (%s): edit it, then restart\n" % nuc_config.features_writable()[1]).encode())
        # a browser says where a form came from: only this page, on this host and port (a page of another site, or of another port, is no one's click)
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return self._send(403, b"a form of another site is refused\n")
        for name in ("Origin", "Referer"):
            if self.headers.get(name) is not None and not same_host(self.headers.get(name), self.headers.get("Host")):
                return self._send(403, b"a form of another site is refused\n")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._send(415, b"a form is application/x-www-form-urlencoded\n")
        n = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or n is None or not NUM.fullmatch(n):
            return self._send(400, b"a form has a Content-Length\n")
        if int(n) > POST_MAX:
            return self._send(413, b"too big\n")
        body = self.rfile.read(int(n))
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (ValueError, UnicodeDecodeError):
            return self._send(400, b"not a form\n")
        if not hmac.compare_digest(((form.get("csrf") or [""])[0]).encode(), srv.csrf.encode()):
            return self._send(403, b"the form is not from this page (reload the page and try again)\n")
        try:
            where = srv.ai_action(act, form) if area == "ai" else srv.tg_action(act, form) if area == "telegram" else srv.settings_action(act, form)
        except BadRequest as e:
            return self._send(400, ("%s\n" % e).encode())
        except OSError as e:  # the settings: config.ini could not be written (a full disk, a file made read-only meanwhile)
            print("nuc-console web: config.ini not written: %r" % (e,), file=sys.stderr)
            return self._send(500, b"config.ini could not be written (see the log)\n")
        if (self.headers.get("Accept") or "").split(",")[0].strip().lower() == webapi.JSON_TYPE.split(";")[0]:
            return self._json(200, {"to": where})  # the live app's form, sent by its script: where the page goes next (a question to ask first...)
        self._send(303, extra=(("Location", where),), referrer="same-origin")  # Post/Redirect/Get: a reload never posts again

    def _no(self):
        self._send(405, b"read-only\n", extra=(("Allow", "GET, HEAD"),))
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no
    do_HEAD = do_GET  # the same checks and headers as GET: _send writes no body for HEAD
