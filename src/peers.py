"""The fleet client: polls the peers of [hosts] and writes what it saw into a cache (stdlib only, Python 3.8+; no web.py, no render.py).

This is the ONE place where nuc-console fetches from the network on behalf of a configured peer, and it follows foreign input: what a
peer answers is untrusted data. The rules (docs/SECURITY.md, "The peer client"), each with its test in tests/test_peers.py:

  - targets: only the entries of [hosts]; the URL is parsed strictly (parse_url) and the address is judged (classify): link-local (the cloud
    metadata address included), multicast, unspecified, reserved and broadcast are never reached; loopback only through the internal
    `allow_loopback` of the tests, never from the configuration; private, CGNAT and ULA ranges are allowed; a public address only over https.
    Every address of a name is judged and the connection goes to the CHECKED address (no second resolution: no DNS rebinding).
  - transport: plain http only to Tailscale ranges unless the peer says insecure=yes (the result then carries the flag); https verifies the
    chain and the name with the system's CAs, or the SHA-256 of the leaf certificate (pin=) INSTEAD of the chain, never a fallback.
  - no redirect, no proxy from the environment, no keep-alive, four fixed headers, 3 s per socket operation and 5 s for the whole request
    (a watchdog closes the socket), a body capped while it is read, a JSON content type, status 200 and nothing else; a remote body is never
    echoed: failures are words of a fixed list (REASONS).
  - the token is read by a hardened reader (a regular file, not a link, 0600, owned by this account or root, small) and goes nowhere but the
    Authorization header of that peer: not into the cache, an error, a log or an argument.
  - what comes back is parsed in a try, shape- and depth-checked, copied through a whitelist of fields with types and lengths, and every
    string passes ui.safe before it enters the cache. A peer that cannot be reached, trusted or read is never shown as ok.

The cache (docs/WEB.md, "Fleet cache"): one JSON file per peer plus fleet.json in a folder the poller owns (nuc_config.peers_cache_dir()),
written atomically; read_cache() is how a consumer reads it back (the folder is another account's: no link, owner, mode, size, types).

    python3 peers.py list | test NAME | once       (bin/nuc-console-fleet)
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import random
import re
import socket
import ssl
import stat
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nuc_config  # noqa: E402
import ui  # noqa: E402

API_KNOWN = 1  # the web API version this client reads; a peer with a bigger one is "incompatible"
USER_AGENT = "nuc-console-peers/1"
SUMMARY_PATH, VIEW_PATH = "/api/v1/summary", "/api/v1/"
VIEWS = ("overview", "cpu", "health", "map", "events")  # the documents the 'views' scope fetches, as opaque data
STATES = ("ok", "warn", "err", "down", "unknown", "info", "unreachable", "unauthorized", "incompatible", "untrusted", "stale")
SUMMARY_STATES = ("ok", "warn", "err", "down", "unknown", "info")
# which is worse, for the fleet's pill: an unreachable or untrusted peer outranks a quiet "warn" and is never green
RANK = {"ok": 0, "info": 0, "warn": 1, "unknown": 2, "stale": 2, "incompatible": 2, "unauthorized": 3, "untrusted": 3, "unreachable": 4,
        "err": 4, "down": 5}
FAILED = ("unreachable", "unauthorized", "untrusted", "incompatible")  # these raise the backoff of the peer
SCOPES = nuc_config.HOST_SCOPES
INTERVAL, INTERVAL_MIN = 15, 5
BACKOFF_FIRST, BACKOFF_MAX = 5, 300
WORKERS = 4
STALE_AGE_S = 120  # a summary whose own `at` is older than this (by the peer's clock) is "stale"
SKEW_WARN_S = 60
CACHE_MAX = 12 * 1024 * 1024  # a peer file: a summary and up to five 2 MB documents
FLEET_MAX = 64 * 1024
CACHE_STALE_S = 120  # a consumer treats a file older than this as stale (the poller writes every 15 s)
TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{16,256}")
POSIX = os.name == "posix"

# every failure is one of these words; the text for a person is here, never a remote or a system message
REASONS = {
    "bad-url": "the URL in [hosts] is not acceptable (http or https, no user, query or fragment, a plain name or IP address)",
    "resolve": "the name does not resolve",
    "blocked-address": "the address is not one nuc-console may connect to (link-local, loopback, multicast, reserved or unspecified)",
    "plain-http": "plain http is only allowed to Tailscale addresses: use https, or set insecure=yes for this peer",
    "token-file": "the token file is missing, unsafe (not 0600, a link, not yours) or not a token",
    "connect": "cannot connect",
    "timeout": "no answer in time",
    "tls": "the TLS handshake failed",
    "tls-verify": "the certificate is not trusted for this name (no CA chain or a wrong name); use https with a valid certificate or pin=sha256:",
    "pin-mismatch": "the certificate is not the pinned one",
    "redirect": "the peer answered with a redirect, which is never followed",
    "unauthorized": "the peer refused the token (401/403)",
    "not-found": "the peer has no such page (an old version, or not nuc-console)",
    "http-status": "the peer answered with an error status",
    "encoding": "the peer sent a compressed or transformed body",
    "content-type": "the answer is not application/json",
    "too-large": "the answer is larger than allowed",
    "truncated": "the answer ended before the length the peer announced",
    "bad-json": "the answer is not valid JSON",
    "bad-shape": "the answer is not a nuc-console document",
    "api-newer": "the peer speaks a newer API than this client understands",
    "identity-changed": "this is not the host that answered first (host_id changed): check the address, then remove it from the known file",
    "cache-old": "the poller has not written for a while",
    "internal": "internal error",
}


class PeerError(Exception):
    """A failed poll: `state` is the peer state it gives, `reason` one of REASONS."""

    def __init__(self, state, reason):
        Exception.__init__(self, reason)
        self.state, self.reason = state, reason


class Limits(object):
    """The time and size limits of one request. The defaults are the rules; a test passes smaller ones to keep itself fast."""

    def __init__(self, op=3.0, total=5.0, summary_bytes=256 * 1024, view_bytes=2 * 1024 * 1024):
        self.op, self.total, self.summary_bytes, self.view_bytes = op, total, summary_bytes, view_bytes


# ---- Text from a peer --------------------------------------------------------------------------------------------------------------------
_ODD = frozenset(("Cf", "Zl", "Zp", "Co", "Cs", "Cn"))  # format controls (bidi overrides), line and paragraph separators, unassigned
_SECRET_KEY = re.compile(r"csrf|token|secret|passw|cookie|authorization|api[_-]?key", re.I)


def clean(value, n):
    """A string from a peer: cut to n characters, no control character (ui.safe), no invisible format or separator character."""
    s = ui.safe(str(value)[:n])
    return "".join("?" if unicodedata.category(c) in _ODD else c for c in s)


def _no_constant(_name):
    raise ValueError("NaN and Infinity are not JSON")


def _int_text(text):
    if len(text) > 20:  # int() of a long run of digits is quadratic on older Pythons
        raise ValueError("number too long")
    return int(text)


def loads(body):
    """The JSON of bytes a peer sent -> the value; ValueError for anything that is not strictly that (bad bytes, nesting too deep, NaN)."""
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_no_constant, parse_int=_int_text)
    except (ValueError, RecursionError, MemoryError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ValueError("not json") from None


def tree(v, depth=0, limit=40):
    """A copy of a JSON value that is safe to keep and show: strings cleaned, keys that look like a CSRF value or a token dropped, floats
    finite, nothing nested deeper than `limit` (ValueError)."""
    if depth > limit:
        raise ValueError("too deep")
    if isinstance(v, dict):
        return {clean(k, 120): tree(x, depth + 1, limit) for k, x in v.items() if isinstance(k, str) and not _SECRET_KEY.search(k)}
    if isinstance(v, list):
        return [tree(x, depth + 1, limit) for x in v]
    if isinstance(v, str):
        return clean(v, 2000)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return v if v is None or isinstance(v, (bool, int)) else None


# ---- The URL and the address ---------------------------------------------------------------------------------------------------------------
_QUAD = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PATH = re.compile(r"/[A-Za-z0-9._~/-]*")
_PORT = re.compile(r"[0-9]{1,5}")
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
# addresses of cloud metadata services outside link-local (AWS IPv6, Alibaba): reached from a private or CGNAT range otherwise
NAT64, SIX2FOUR = ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("2002::/16")
METADATA = tuple(ipaddress.ip_network(n) for n in ("fd00:ec2::/32", "100.100.100.200/32"))


class Target(object):
    """A parsed [hosts] URL: scheme, host (lower case; an IP literal as its text), port, ip (an ipaddress object or None for a name), the
    path prefix ('' or '/x'), and the Host header and the text to show (no user info by construction)."""
    __slots__ = ("scheme", "host", "port", "ip", "prefix", "hostline", "display")

    def __init__(self, scheme, host, port, ip, prefix):
        self.scheme, self.host, self.port, self.ip, self.prefix = scheme, host, port, ip, prefix
        shown = "[%s]" % host if ip is not None and ip.version == 6 else host
        default = 443 if scheme == "https" else 80
        self.hostline = shown if port == default else "%s:%d" % (shown, port)
        self.display = "%s://%s%s" % (scheme, self.hostline, prefix)


def reach(ip):
    """The address to connect to: an IPv4-mapped one is the IPv4 address (the others are connected as they are, judged by what they embed)."""
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip


def norm(ip):
    """The address a connection would really reach: an IPv4-mapped or NAT64 or 6to4 IPv6 address is judged as the IPv4 address inside it."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip in SIX2FOUR:
            return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return ip


def classify(ip, allow_loopback=False):
    """-> 'forbidden' | 'loopback' | 'tailnet' | 'private' | 'public'. What an address EMBEDS (NAT64, 6to4) can only make it forbidden; the
    range it is in is the range of the address the socket goes to (only ::ffff:a.b.c.d is that IPv4 address), so an IPv6 address that
    embeds a Tailscale IPv4 address is 'public': https only."""
    if _kind(norm(ip), allow_loopback) == "forbidden":
        return "forbidden"
    actual = reach(ip)
    if actual.version == 6 and any(actual in n for n in (NAT64, SIX2FOUR)):
        return "public"
    return _kind(actual, allow_loopback)


def _kind(ip, allow_loopback):
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or getattr(ip, "is_site_local", False) or ip == ipaddress.IPv4Address("255.255.255.255"):
        return "forbidden"
    if ip.is_loopback:  # before is_reserved: ::1 is inside the reserved ::/8
        return "loopback" if allow_loopback else "forbidden"
    if ip.is_reserved or any(ip.version == n.version and ip in n for n in METADATA):
        return "forbidden"
    if any(ip.version == n.version and ip in n for n in TAILNET):
        return "tailnet"
    if any(ip.version == n.version and ip in n for n in PRIVATE):
        return "private"
    if ip.is_private or not ip.is_global:  # documentation, benchmarking, shared, site-local, 0.0.0.0/8, ...: no service of a person
        return "forbidden"
    return "public"


def parse_url(url):
    """-> Target. ValueError for anything but http(s)://HOST[:PORT][/prefix] with a DNS name or an IP literal in its plain spelling."""
    if not isinstance(url, str) or not 8 <= len(url) <= 300 or any(ord(c) <= 0x20 or ord(c) >= 0x7F or c in "\\\"'`?#%" for c in url):
        raise ValueError("url")
    scheme, sep, rest = url.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("http", "https"):
        raise ValueError("scheme")
    netloc, slash, path = rest.partition("/")
    path = slash + path
    if not netloc or "@" in netloc:
        raise ValueError("userinfo")
    if netloc.startswith("["):  # an IPv6 literal
        close = netloc.find("]")
        if close < 0:
            raise ValueError("host")
        host, tail = netloc[1:close], netloc[close + 1:]
        if tail and not tail.startswith(":"):
            raise ValueError("host")
        port_text = tail[1:] if tail else ""
        if "%" in host or ":" not in host:
            raise ValueError("host")
        ip = ipaddress.IPv6Address(host)  # ValueError
        host = ip.compressed
    else:
        host, colon, port_text = netloc.partition(":")
        if colon and not port_text:
            raise ValueError("port")
        host = host.lower()
        if host.endswith("."):
            host = host[:-1]
        ip = None
        if re.fullmatch(r"[0-9.]+", host):  # only the dotted quad: 2130706433, 127.1 and 0177.0.0.1 are other spellings of an address
            if not _QUAD.fullmatch(host) or any(int(x) > 255 for x in host.split(".")):
                raise ValueError("host")
            ip = ipaddress.IPv4Address(host)
        else:
            labels = host.split(".")
            if len(host) > 253 or not all(_LABEL.fullmatch(x) for x in labels) or labels[-1].isdigit() or re.fullmatch(r"0x[0-9a-f]*", labels[-1]):
                raise ValueError("host")
    if port_text:
        if not _PORT.fullmatch(port_text) or not 1 <= int(port_text) <= 65535:
            raise ValueError("port")
        port = int(port_text)
    else:
        port = 443 if scheme == "https" else 80
    prefix = path.rstrip("/")
    if prefix and (not _PATH.fullmatch(prefix) or "//" in prefix or ".." in prefix.split("/")):
        raise ValueError("path")
    return Target(scheme, host, port, ip, prefix)


def judge(scheme, classes, insecure=False):
    """-> (ok, reason, plain): can a request over `scheme` go to addresses of these classes? plain: it goes in the clear to somewhere that
    is not a Tailscale range (only with insecure=yes) so that the result can raise the alarm."""
    if not classes or "forbidden" in classes:
        return False, "blocked-address", False
    if scheme == "https":
        return True, "", False
    if all(c in ("tailnet", "loopback") for c in classes):
        return True, "", False
    if insecure and "public" not in classes:  # insecure=yes opens http to the private ranges; a public address is https or nothing
        return True, "", True
    return False, "plain-http", False


# ---- The token ------------------------------------------------------------------------------------------------------------------------------
def read_secret(path, limit=512):
    """The token in the file at `path`. PeerError(unauthorized, token-file) for anything unsafe or odd; the message never holds the path or the
    content. POSIX: a regular file opened without following a link, no group/other permission bits, owned by root or by this account, in a
    folder that group/others cannot write to."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except (OSError, ValueError, TypeError):
        raise PeerError("unauthorized", "token-file") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise PeerError("unauthorized", "token-file")
        if POSIX:
            if st.st_mode & 0o077 or st.st_uid not in (0, os.geteuid()):
                raise PeerError("unauthorized", "token-file")
            if os.stat(os.path.dirname(os.path.abspath(path))).st_mode & 0o022:
                raise PeerError("unauthorized", "token-file")
        data = os.read(fd, limit + 1)
    except OSError:
        raise PeerError("unauthorized", "token-file") from None
    finally:
        os.close(fd)
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise PeerError("unauthorized", "token-file") from None
    if len(data) > limit or not TOKEN_RE.fullmatch(token):
        raise PeerError("unauthorized", "token-file")
    return token


# ---- One request ----------------------------------------------------------------------------------------------------------------------------
_LOOKUPS = threading.BoundedSemaphore(4)  # at most four name lookups outstanding at once (a stuck getaddrinfo cannot be interrupted)


def _resolve(host, port, resolver, timeout):
    """Every address of `host` (text), in the order the system gives them. A resolver that hangs costs `timeout`, not more (a thread)."""
    out = {}
    if not _LOOKUPS.acquire(False):  # four lookups are already stuck: another thread would only pile up behind a dead resolver
        raise PeerError("unreachable", "resolve")

    def work():
        try:
            out["r"] = [r for r in (resolver or _system_resolver)(host, port)]
        except Exception:  # noqa: BLE001  (any resolver failure is "does not resolve")
            out["r"] = None
        finally:
            _LOOKUPS.release()
    th = threading.Thread(target=work, daemon=True)
    try:
        th.start()
    except RuntimeError:
        _LOOKUPS.release()
        raise PeerError("unreachable", "resolve") from None
    th.join(max(0.0, timeout))
    if th.is_alive() or not out.get("r"):
        raise PeerError("unreachable", "timeout" if th.is_alive() else "resolve")
    return out["r"]


def _system_resolver(host, port):
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _connect(ip, port, timeout):
    """A connected socket to the address `ip` (an ipaddress object, already judged). The one place that opens a connection."""
    s = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((str(ip), port))
    except BaseException:
        s.close()
        raise
    return s


def scrub_env():
    """OpenSSL reads SSL_CERT_FILE and SSL_CERT_DIR for the system's CAs (ssl.create_default_context cannot be told not to): a service that
    follows the network must not take its trust anchors from its environment. Called by main() before any context exists."""
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        os.environ.pop(name, None)


def _context(pin, cafile):
    if pin:  # the leaf certificate's hash is checked INSTEAD of the chain and the name (after the handshake)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context(cafile=cafile)
    if hasattr(ssl, "TLSVersion"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Reply(object):
    __slots__ = ("body", "date", "plain")

    def __init__(self, body, date, plain):
        self.body, self.date, self.plain = body, date, plain


def get(t, path, token, cap, limits=None, pin="", insecure=False, allow_loopback=False, resolver=None, cafile=None, clock=time.monotonic,
        info=None):
    """GET prefix+path from the peer `t` (a Target) -> Reply(body bytes, the peer's Date as epoch seconds or None, plain). Raises PeerError.
    allow_loopback, resolver, cafile and clock are the tests' (never fed from the configuration). `info` gets "plain": True as soon as the
    request is allowed to go in the clear to a non-Tailscale address."""
    lim = limits or Limits()
    end = clock() + lim.total
    if t.ip is not None:
        ips = [t.ip]
    else:
        ips = []
        for text in _resolve(t.host, t.port, resolver, min(lim.op, end - clock())):
            try:
                ips.append(ipaddress.ip_address(text) if "%" not in str(text) else None)
            except ValueError:
                ips.append(None)
        if None in ips:
            raise PeerError("unreachable", "blocked-address")
    ips = ips[:8]
    ok, why, plain = judge(t.scheme, [classify(i, allow_loopback) for i in ips], insecure)
    if not ok:
        raise PeerError("unreachable", why)
    if info is not None and plain:
        info["plain"] = True
    holder = {"sock": None, "dead": False}

    def kill():  # the deadline: the socket is shut down under whoever is blocked on it
        holder["dead"] = True
        s = holder["sock"]
        if s is not None:
            for fn in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    fn()
                except (OSError, ValueError):
                    pass
    watchdog = threading.Timer(max(0.0, end - clock()), kill)
    watchdog.daemon = True
    watchdog.start()
    conn = None
    try:
        sock = None
        for ip in ips:
            left = end - clock()
            if left <= 0:
                raise PeerError("unreachable", "timeout")
            try:
                sock = _connect(reach(ip), t.port, min(lim.op, left))  # the checked address, not the name
                break
            except (OSError, ValueError):
                continue
        if sock is None:
            raise PeerError("unreachable", "timeout" if holder["dead"] or clock() >= end else "connect")
        holder["sock"] = sock
        if t.scheme == "https":
            sock.settimeout(max(0.01, min(lim.op, end - clock())))
            try:
                sock = _context(pin, cafile).wrap_socket(sock, server_hostname=t.host, do_handshake_on_connect=False)
                holder["sock"] = sock
                sock.do_handshake()
                if pin:
                    der = sock.getpeercert(binary_form=True) or b""
                    if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin.split(":", 1)[1].lower()):
                        raise PeerError("untrusted", "pin-mismatch")
            except ssl.SSLCertVerificationError:
                raise PeerError("untrusted", "tls-verify") from None
            except (ssl.SSLError, OSError, ValueError) as e:
                timed = holder["dead"] or clock() >= end or isinstance(e, socket.timeout) or "timed out" in str(e)
                raise PeerError("unreachable", "timeout" if timed else "tls") from None
        conn = http.client.HTTPConnection("peer", t.port)
        conn.sock = sock  # http.client speaks over our socket: it never connects, never reads a proxy from the environment
        sock.settimeout(max(0.01, min(lim.op, end - clock())))
        conn.putrequest("GET", t.prefix + path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", t.hostline)
        if token:
            conn.putheader("Authorization", "Bearer " + token)
        conn.putheader("Accept", "application/json")
        conn.putheader("User-Agent", USER_AGENT)
        conn.endheaders()
        resp = conn.getresponse()
        status = resp.status
        if 300 <= status < 400:
            raise PeerError("unreachable", "redirect")
        if status in (401, 403):
            raise PeerError("unauthorized", "unauthorized")
        if status == 404:
            raise PeerError("incompatible", "not-found")
        if status != 200:
            raise PeerError("unreachable", "http-status")
        if resp.getheader("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
            raise PeerError("unreachable", "encoding")
        if (resp.getheader("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise PeerError("unreachable", "content-type")
        length = resp.getheader("Content-Length")
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,15}", length.strip()):
                raise PeerError("unreachable", "bad-shape")
            if int(length) > cap:
                raise PeerError("unreachable", "too-large")
        chunks, got = [], 0
        while True:
            left = end - clock()
            if left <= 0:
                raise PeerError("unreachable", "timeout")
            sock.settimeout(max(0.01, min(lim.op, left)))
            data = resp.read(min(16384, cap + 1 - got))  # whatever Content-Length or the chunks say, never more than the cap
            if not data:
                break
            chunks.append(data)
            got += len(data)
            if got > cap:
                raise PeerError("unreachable", "too-large")
        if length is not None and not resp.chunked and got < int(length):
            raise PeerError("unreachable", "truncated")  # fewer bytes than announced: not the document the peer meant
        date = None
        try:
            date = parsedate_to_datetime(resp.getheader("Date") or "").timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
        return Reply(b"".join(chunks), date, bool(plain))
    except PeerError as e:
        if holder["dead"] and e.reason != "timeout":  # cut short by the deadline: a truncated header or body says nothing else
            raise PeerError("unreachable", "timeout") from None
        raise
    except (socket.timeout, TimeoutError):
        raise PeerError("unreachable", "timeout") from None
    except http.client.HTTPException:
        raise PeerError("unreachable", "timeout" if holder["dead"] or clock() >= end else "bad-shape") from None
    except (OSError, ValueError, ssl.SSLError):
        raise PeerError("unreachable", "timeout" if holder["dead"] or clock() >= end else "connect") from None
    finally:
        watchdog.cancel()
        s = holder["sock"]
        for obj in (conn, s):
            try:
                if obj is not None:
                    obj.close()
            except (OSError, ValueError):
                pass


# ---- What a peer says ------------------------------------------------------------------------------------------------------------------------
_HEX16 = re.compile(r"[0-9a-f]{16}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_~-]{0,39}")
_OS = re.compile(r"[a-z0-9-]{1,16}")


def _int(v, hi=10 ** 7):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None


def flat_summary(d):
    """The whitelist: a dict of candidate fields -> only the fields that have the right type and size, cleaned. Used on a peer's answer (after
    the shape of the 2.x summary is flattened) and again on a cache file by read_cache."""
    out = {}
    if not isinstance(d, dict):
        return out
    for key, n in (("host", 120), ("status_text", 120)):
        if isinstance(d.get(key), str) and d[key].strip():
            out[key] = clean(d[key], n)
    if isinstance(d.get("host_id"), str) and _HEX16.fullmatch(d["host_id"]):
        out["host_id"] = d["host_id"]
    if isinstance(d.get("version"), str) and _VERSION.fullmatch(d["version"]):
        out["version"] = d["version"]
    if isinstance(d.get("os"), str) and _OS.fullmatch(d["os"]):
        out["os"] = d["os"]
    if _int(d.get("api"), 10 ** 6) is not None:
        out["api"] = d["api"]
    if d.get("state") in SUMMARY_STATES:
        out["state"] = d["state"]
    if _int(d.get("problems")) is not None:
        out["problems"] = d["problems"]
    if isinstance(d.get("counts"), dict):
        counts = {k: d["counts"][k] for k in ("err", "warn", "unknown", "info") if _int(d["counts"].get(k)) is not None}
        if counts:
            out["counts"] = counts
    if isinstance(d.get("worst"), list):
        out["worst"] = [clean(x, 120) for x in d["worst"] if isinstance(x, str)][:3]
    if isinstance(d.get("stale"), bool):
        out["stale"] = d["stale"]
    at = d.get("at")
    if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and 0 <= at <= 4102444800:
        out["at"] = at
    return out


def parse_summary(body):
    """The bytes of a peer's /api/v1/summary -> the whitelisted fields (flat_summary). PeerError for JSON that is not, a document that is not
    a summary, or an API newer than API_KNOWN (state incompatible: only api and version are kept, nothing else is interpreted)."""
    try:
        doc = loads(body)
    except ValueError:
        raise PeerError("unreachable", "bad-json") from None
    if not isinstance(doc, dict) or doc.get("view") != "summary" or _int(doc.get("api"), 10 ** 6) is None:
        raise PeerError("unreachable", "bad-shape")
    if doc["api"] > API_KNOWN:
        raise PeerError("incompatible", "api-newer")
    status = doc.get("status")
    cand = dict(doc)
    if isinstance(status, dict):
        cand["state"], cand["status_text"] = status.get("state"), status.get("text")
    elif isinstance(status, str):
        cand["state"] = status
    cand["state"] = cand.get("state") if cand.get("state") in SUMMARY_STATES else None
    return flat_summary(cand)


def parse_view(name, body):
    """The bytes of a peer's document `name` -> the document, opaque but checked: a JSON object whose `view` is `name` and whose `api` is one
    this client reads, nested at most 40 deep, strings cleaned, keys named like csrf or token removed. PeerError otherwise."""
    try:
        doc = loads(body)
        if not isinstance(doc, dict) or doc.get("view") != name or _int(doc.get("api"), 10 ** 6) is None or doc["api"] > API_KNOWN:
            raise PeerError("unreachable", "bad-shape")
        return tree(doc)
    except ValueError:
        raise PeerError("unreachable", "bad-json") from None


# ---- One poll ----------------------------------------------------------------------------------------------------------------------------
def new_entry(peer, now):
    try:
        shown = parse_url(peer["url"]).display
    except ValueError:
        shown = ""
    return {"v": 1, "name": peer["name"], "url_display": shown, "ts": int(now), "state": "unknown", "error": "", "summary": {}, "views": {},
            "scope": peer.get("scope", "summary"), "warn": [], "insecure": False, "identity": "ok", "last_ok": None, "skew_s": None,
            "age_s": None}


def poll_peer(peer, prev=None, wall=time.time, **net):
    """One poll of one peer (a dict of nuc_config's [hosts]) -> its cache entry. Never raises. `prev` is the previous entry: when the peer
    is unreachable its last summary and documents are kept, with their age (last_ok), as the plan wants. **net: limits, allow_loopback,
    resolver, cafile, clock (get)."""
    entry = new_entry(peer, wall())
    info = {"plain": False}
    try:
        try:
            t = parse_url(peer["url"])
        except ValueError:
            raise PeerError("unreachable", "bad-url") from None
        if peer.get("pin") and t.scheme != "https":
            raise PeerError("unreachable", "bad-url")  # a pin belongs to a certificate: there is none on http
        token = read_secret(peer["token"])
        lim = net.get("limits") or Limits()
        kw = dict(net, pin=peer.get("pin", ""), insecure=bool(peer.get("insecure")), info=info)
        kw.pop("limits", None)
        reply = get(t, SUMMARY_PATH, token, lim.summary_bytes, limits=lim, **kw)
        received = wall()
        summary = parse_summary(reply.body)
        entry["summary"] = summary
        entry["last_ok"] = int(received)
        if reply.date is not None and abs(reply.date - received) < 10 * 365 * 86400:
            entry["skew_s"] = int(round(reply.date - received))
            if "at" in summary:
                entry["age_s"] = max(0, int(reply.date - summary["at"]))  # the peer's own clock against the peer's own clock
        if entry["skew_s"] is not None and abs(entry["skew_s"]) > SKEW_WARN_S:
            entry["warn"].append("clock-skew")
        if "host_id" not in summary or "counts" not in summary:
            entry["warn"].append("legacy-peer")
        if peer.get("scope") == "full":
            entry["warn"].append("full-scope-token")
        state = summary.get("state", "unknown")
        if summary.get("stale") is True or (entry["age_s"] is not None and entry["age_s"] > STALE_AGE_S):
            state = "stale"
        entry["state"] = state
        if peer.get("scope") in ("views", "full") and state != "stale":
            for name in VIEWS:  # the first one that fails ends the round: a slow peer costs one timeout, not five
                try:
                    entry["views"][name] = parse_view(name, get(t, VIEW_PATH + name, token, lim.view_bytes, limits=lim, **kw).body)
                except PeerError as e:
                    entry["warn"].append("view-failed:" + name)
                    if e.reason in ("timeout", "connect", "unauthorized"):
                        break
    except PeerError as e:
        entry["state"], entry["error"] = e.state, e.reason
        if e.state == "unreachable" and prev and isinstance(prev.get("summary"), dict):
            entry["summary"], entry["views"], entry["last_ok"] = prev["summary"], prev.get("views") or {}, prev.get("last_ok")
            entry["warn"].append("showing-old-data")
    except Exception:  # noqa: BLE001  (a bug must not take the poller down, nor leak a message)
        entry["state"], entry["error"] = "unreachable", "internal"
    entry["insecure"] = bool(info["plain"])
    if info["plain"]:
        entry["warn"].append("insecure-transport")
    return entry


class Known(object):
    """host_id of each peer, remembered at the first contact (like an SSH known_hosts): the file `known` in the state folder
    (nuc_config.peers_state_dir(), which survives a reboot, unlike the cache)."""
    NAME = "known"

    def __init__(self, folder):
        self.path = os.path.join(folder, self.NAME)
        self.ids = {}
        obj = read_json(self.path, 65536)
        if isinstance(obj, dict) and obj.get("v") == 1 and isinstance(obj.get("ids"), dict):
            self.ids = {k: v for k, v in obj["ids"].items() if nuc_config.HOST_NAME_RE.fullmatch(str(k)) and isinstance(v, str) and _HEX16.fullmatch(v)}

    def check(self, entry, remember=True):
        """Sets entry["identity"] ('ok' | 'changed') and, when it changed, makes the peer untrusted and drops its data. Returns True when `ids` changed."""
        seen = entry["summary"].get("host_id") if entry["state"] not in FAILED else None
        old = self.ids.get(entry["name"])
        if old and entry["state"] not in FAILED and seen != old:  # another id, or none any more: not the host that answered first
            entry.update(identity="changed", state="untrusted", error="identity-changed", summary={}, views={}, last_ok=None, age_s=None)
            return False
        if seen and not old and remember:
            self.ids[entry["name"]] = seen
            return True
        return False

    def forget(self, name):
        """The only way to accept a replaced machine: True when something was remembered."""
        return self.ids.pop(name, None) is not None

    def save(self, folder):
        os.makedirs(folder, 0o755, exist_ok=True)
        write_json(folder, self.NAME, {"v": 1, "ids": self.ids})


# ---- The cache ---------------------------------------------------------------------------------------------------------------------------
NAME_FILE = re.compile(r"([a-z0-9][a-z0-9-]{0,31})\.json")


def write_json(folder, name, obj, limit=None):
    """folder/name atomically (a file aside, then os.replace), mode 0644, never through a link. ValueError when it would be too big."""
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(data) > (CACHE_MAX if limit is None else limit):
        raise ValueError("too big")
    tmp = os.path.join(folder, ".%s.tmp" % name)
    try:
        os.remove(tmp)
    except OSError:  # nothing there, or not ours to remove: the exclusive open below decides
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if POSIX:
            os.chmod(tmp, 0o644)  # the umask must not decide
        os.replace(tmp, os.path.join(folder, name))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, limit):
    """The JSON in a file another account may have written -> value, or None: a regular file opened without following a link, no larger than
    `limit`, not writable by group/others, owned by root, this account or the folder's owner, in a folder others cannot write to."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except (OSError, ValueError, TypeError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        if POSIX:
            dst = os.stat(os.path.dirname(os.path.abspath(path)))
            if st.st_mode & 0o022 or dst.st_mode & 0o022 or st.st_uid not in (0, os.geteuid(), dst.st_uid):
                return None
        chunks, got = [], 0
        while got <= limit:
            part = os.read(fd, 65536)
            if not part:
                break
            chunks.append(part)
            got += len(part)
        if got > limit:
            return None
        return loads(b"".join(chunks))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def fleet_doc(entries, now):
    worst = max((e["state"] for e in entries), key=lambda s: RANK.get(s, 2), default="unknown")
    return {"v": 1, "ts": int(now), "worst": worst, "peers": [{"name": e["name"], "state": e["state"]} for e in entries]}


OWNER = ".owner"  # the marker of a cache folder this poller made or was given empty


def own_folder(folder):
    """Makes sure `folder` is a real directory (not a link) that this poller may fill and clean: owned by this account (POSIX; Windows relies
    on the installer's ACLs), and either carrying our marker or empty. OSError('cache folder is not ours') and nothing touched otherwise."""
    if not os.path.lexists(folder):
        os.makedirs(folder, 0o755)
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode) or (POSIX and st.st_uid != os.geteuid()):
        raise OSError("cache folder is not ours")
    marker = os.path.join(folder, OWNER)
    if not os.path.lexists(marker):
        if os.listdir(folder):
            raise OSError("cache folder is not ours")
        write_json(folder, OWNER, {"v": 1})
    elif read_json(marker, 1024) != {"v": 1}:
        raise OSError("cache folder is not ours")


VIEWS_SUFFIX = ".views.json"  # NAME.views.json: the documents of a peer, apart from NAME.json (summary entry + index): the cheap read stays cheap


def _remove_regular(path):
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            os.remove(path)
    except OSError:
        pass


def write_entry(folder, e):
    """One peer's two files: NAME.views.json (the documents, written first, or removed when there are none) and NAME.json (the summary entry
    and `view_index` {document: {size, ts}}, never the documents). Too big with the documents -> without them (the summary still counts).
    -> the view index written."""
    name, views, warn = e["name"], e.get("views") or {}, list(e["warn"])
    index = {}
    if views:
        try:
            write_json(folder, name + VIEWS_SUFFIX, {"v": 1, "views": views})
            index = {k: {"size": len(json.dumps(v, separators=(",", ":"))), "ts": int(e["ts"])} for k, v in views.items()}
        except ValueError:
            warn.append("views-dropped")
    if not index:
        _remove_regular(os.path.join(folder, name + VIEWS_SUFFIX))
    body = {k: v for k, v in e.items() if k not in ("views", "view_index")}
    write_json(folder, name + ".json", dict(body, warn=warn, view_index=index))
    return index


def write_cache(folder, entries, now, keep=None, files=True):
    """Writes one file per entry and fleet.json; removes the file of a peer that fleet.json listed before and `keep` (the names configured)
    no longer has, and nothing else: foreign files in the folder are never touched (OSError, and nothing written, if the folder is not ours)."""
    own_folder(folder)
    before = read_json(os.path.join(folder, "fleet.json"), FLEET_MAX)
    listed = [p.get("name") for p in before.get("peers", []) if isinstance(p, dict)] if isinstance(before, dict) and isinstance(before.get("peers"), list) else []
    for e in entries if files else ():
        write_entry(folder, e)
    for name in listed:
        if keep is not None and isinstance(name, str) and name not in keep and nuc_config.HOST_NAME_RE.fullmatch(name):
            _remove_regular(os.path.join(folder, name + ".json"))
            _remove_regular(os.path.join(folder, name + VIEWS_SUFFIX))
    write_json(folder, "fleet.json", fleet_doc(entries, now), FLEET_MAX)


def valid_entry(obj, name):
    """The entry of a cache file as a consumer may use it, rebuilt field by field, or None. The file is foreign input."""
    if not isinstance(obj, dict) or obj.get("v") != 1 or obj.get("name") != name or obj.get("state") not in STATES:
        return None
    if obj.get("scope") not in SCOPES or obj.get("identity") not in ("ok", "changed") or not isinstance(obj.get("insecure"), bool):
        return None
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 0 <= ts <= 4102444800:
        return None
    err = obj.get("error")
    if not isinstance(err, str) or not (err == "" or err in REASONS):
        return None
    warn = obj.get("warn")
    if not isinstance(warn, list) or len(warn) > 32:
        return None
    out = {"v": 1, "name": name, "url_display": clean(obj.get("url_display") if isinstance(obj.get("url_display"), str) else "", 300),
           "ts": ts, "state": obj["state"], "error": err, "scope": obj["scope"], "identity": obj["identity"], "insecure": obj["insecure"],
           "warn": [clean(w, 40) for w in warn if isinstance(w, str)], "summary": flat_summary(obj.get("summary")),
           "last_ok": _int(obj.get("last_ok"), 4102444800), "skew_s": obj["skew_s"] if isinstance(obj.get("skew_s"), int) and not isinstance(obj.get("skew_s"), bool) else None,
           "age_s": _int(obj.get("age_s"), 10 ** 9), "views": {}, "view_index": {}}
    index = obj.get("view_index")
    if isinstance(index, dict):
        for vname in VIEWS:
            v = index.get(vname)
            if isinstance(v, dict) and _int(v.get("size"), CACHE_MAX) is not None and _int(v.get("ts"), 4102444800) is not None:
                out["view_index"][vname] = {"size": _int(v["size"], CACHE_MAX), "ts": _int(v["ts"], 4102444800)}
    return out


def read_views(folder, name):
    """The documents of one peer from NAME.views.json (foreign input, read like every cache file) -> {document: tree}, {} when there are none."""
    out = {}
    if not NAME_FILE.fullmatch(name + ".json"):
        return out
    obj = read_json(os.path.join(folder, name + VIEWS_SUFFIX), CACHE_MAX)
    views = obj.get("views") if isinstance(obj, dict) and obj.get("v") == 1 else None
    if isinstance(views, dict):
        for vname in VIEWS:
            doc = views.get(vname)
            if isinstance(doc, dict) and doc.get("view") == vname:
                try:
                    out[vname] = tree(doc)
                except ValueError:
                    pass
    return out


def read_cache(folder, now=None, views=False):
    """The cache as a consumer reads it -> {"peers": {name: entry}, "fleet": {...} | None, "age_s": seconds since fleet.json or None,
    "skipped": n}. Foreign input: files that are links, not regular, too big, writable by others, owned by someone else, not JSON or with a
    wrong type are skipped (and counted); an entry older than CACHE_STALE_S is 'stale' (error cache-old) unless it already is worse. The
    documents of `views` are only returned (and sanitised again) when asked for. Never raises."""
    now = time.time() if now is None else now
    out = {"peers": {}, "fleet": None, "age_s": None, "skipped": 0}
    try:
        names = sorted(os.listdir(folder))
    except (OSError, ValueError):
        return out
    for fn in names:
        m = NAME_FILE.fullmatch(fn)
        if not m:
            continue
        if m.group(1) == "fleet":
            doc = read_json(os.path.join(folder, fn), FLEET_MAX)
            if (isinstance(doc, dict) and doc.get("v") == 1 and doc.get("worst") in STATES and isinstance(doc.get("ts"), (int, float))
                    and not isinstance(doc.get("ts"), bool)):
                out["fleet"] = {"ts": doc["ts"], "worst": doc["worst"]}
                out["age_s"] = max(0, int(now - doc["ts"]))
            else:
                out["skipped"] += 1
            continue
        entry = read_entry(folder, m.group(1), now, views)
        if entry is None:
            out["skipped"] += 1
            continue
        out["peers"][m.group(1)] = entry
    return out


def read_entry(folder, name, now=None, views=False):
    """ONE peer's file of the cache as read_cache returns it, or None (missing, foreign, malformed)."""
    now = time.time() if now is None else now
    raw = read_json(os.path.join(folder, name + ".json"), CACHE_MAX)
    entry = valid_entry(raw, name) if raw is not None else None
    if entry is None:
        return None
    if views:
        entry["views"] = read_views(folder, name) if entry["view_index"] else {}
    else:
        del entry["views"]  # present only when asked for
    if now - entry["ts"] > CACHE_STALE_S and entry["state"] in ("ok", "info", "warn", "unknown"):
        entry["state"], entry["error"] = "stale", "cache-old"
    return entry


# ---- The poller ------------------------------------------------------------------------------------------------------------------------
class Poller(object):
    """Polls the peers, at most WORKERS at a time, each on its own schedule: INTERVAL after a success, an exponential backoff (5 s to 5 min,
    with jitter) after a failure. A dead peer holds one worker for its timeout; the others are polled on time. Not a thread of its own:
    step(now) does what is due, run_once() everything, run_forever() steps until `stop` is set."""

    def __init__(self, peers, folder, interval=INTERVAL, poll=poll_peer, clock=time.monotonic, wall=time.time,
                 jitter=lambda: random.uniform(0.8, 1.2), workers=WORKERS, state=None, **net):
        self.peers, self.folder = peers, folder
        self.state = state or nuc_config.peers_state_dir()  # where `known` lives: it survives a reboot, the cache does not
        self.reported = set()
        self.interval = max(INTERVAL_MIN, interval)
        self.poll, self.clock, self.wall, self.jitter, self.net = poll, clock, wall, jitter, net
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.next = {p["name"]: 0.0 for p in peers}
        self.fails = {p["name"]: 0 for p in peers}
        self.running = {}
        self.entries = {}
        self.known = Known(self.state)

    def delay(self, name, entry):
        if entry["state"] in FAILED:
            self.fails[name] += 1
            return min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** (self.fails[name] - 1)) * self.jitter()
        self.fails[name] = 0
        return self.interval * self.jitter()

    def harvest(self):
        done = [n for n, f in self.running.items() if f.done()]
        for name in done:
            fut = self.running.pop(name)
            try:
                entry = fut.result()
            except Exception:  # noqa: BLE001
                entry = new_entry(next(p for p in self.peers if p["name"] == name), self.wall())
                entry["state"], entry["error"] = "unreachable", "internal"
            if self.known.check(entry):
                try:
                    self.known.save(self.state)
                except OSError:
                    pass
            self.persist(entry)
            self.entries[name] = entry
            self.next[name] = self.clock() + self.delay(name, entry)
        return bool(done)

    def persist(self, entry):
        """The peer's file with its documents goes to the cache now; the entry kept in memory has only the names of the documents (16 peers x
        five documents parsed are tens of MB, and nobody needs them here: last() reads one file when a poll fails)."""
        try:
            own_folder(self.folder)
            index = write_entry(self.folder, entry)
        except OSError as e:
            self.complain(e)
            index = {k: {"size": 0, "ts": int(entry["ts"])} for k in entry["views"]}
        entry["view_index"], entry["views"] = index, {}

    def step(self, now=None):
        """Collect the polls that finished, start the ones due; -> True when something was written."""
        now = self.clock() if now is None else now
        changed = self.harvest()
        for p in self.peers:
            name = p["name"]
            if name not in self.running and self.next[name] <= now:
                self.running[name] = self.pool.submit(self.poll, p, self.last(name), self.wall, **self.net)
        if changed:
            try:
                self.write()
            except OSError as e:  # a full disk or a folder that is not ours must not stop the polling: say it once per kind
                self.complain(e)
        return changed

    def complain(self, e):
        kind = e.errno or str(e)
        if kind not in self.reported:
            self.reported.add(kind)
            print("nuc-console-peers: cannot write the cache: %s" % (os.strerror(e.errno) if e.errno else e), file=sys.stderr)

    def last(self, name):
        return read_entry(self.folder, name, views=True) if nuc_config.HOST_NAME_RE.fullmatch(name) else None

    def write(self):
        write_cache(self.folder, [self.entries[p["name"]] for p in self.peers if p["name"] in self.entries], self.wall(),
                    keep={p["name"] for p in self.peers}, files=False)

    def run_once(self):
        """Every peer, now; -> the entries in the order of the configuration."""
        for p in self.peers:
            self.running[p["name"]] = self.pool.submit(self.poll, p, self.last(p["name"]), self.wall, **self.net)
        for fut in list(self.running.values()):
            fut.exception()  # waits
        self.harvest()
        self.write()
        return [self.entries[p["name"]] for p in self.peers]

    def run_forever(self, stop, tick=1.0):
        while not stop.is_set():
            self.step()
            stop.wait(tick)
        self.pool.shutdown(wait=False)


# ---- The command (bin/nuc-console-fleet) ------------------------------------------------------------------------------------------------
def say_entry(e, out):
    words = REASONS.get(e["error"], "")
    out("%s  %s  %s" % (e["name"], e["state"].upper(), e["url_display"] or "(invalid URL)"))
    if words:
        out("  why: %s" % words)
    s = e["summary"]
    bits = ["%s %s" % (k, s[k]) for k in ("host", "version", "os", "api", "host_id") if k in s]
    if bits:
        out("  peer: " + ", ".join(bits))
    if s.get("counts"):
        out("  counts: " + ", ".join("%s %d" % kv for kv in sorted(s["counts"].items())))
    for w in s.get("worst", []):
        out("  - " + w)
    if e.get("skew_s") is not None:
        out("  clock: the peer differs by %+d s" % e["skew_s"])
    if e["warn"]:
        out("  notes: " + ", ".join(e["warn"]))
    if e["insecure"]:
        out("  WARNING: plain http outside Tailscale (insecure=yes): the token travels in the clear")
    if e["identity"] == "changed":
        out("  WARNING: the host_id changed since the first contact")
    if e.get("view_index") or e["views"]:
        out("  documents: " + ", ".join(sorted(e.get("view_index") or e["views"])))


def main(argv=None, out=print, cfg=None, net=None):
    scrub_env()
    net = net or {}  # the tests': limits, allow_loopback, resolver, cafile (never from the command line or the configuration)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--needed"]:  # for the installers: exit 0 when [hosts] has a peer (start the service), 1 when it has none
        try:  # 0 a peer is configured, 1 none, 2 the answer is unknown (unreadable config, crash): the callers leave things as they are
            c = cfg or nuc_config.load()
            if c.get("config_error"):
                print("nuc-console-peers: config unreadable", file=sys.stderr)
                return 2
            return 0 if c["hosts"] else 1
        except Exception:  # noqa: BLE001
            print("nuc-console-peers: config unreadable", file=sys.stderr)
            return 2
    if argv[:1] == ["serve"] and (len(argv) == 1 or (len(argv) == 3 and argv[1] == "--log")):  # the service; --log FILE: Windows, no console
        if len(argv) == 3:
            try:
                _log_to(argv[2])
            except OSError:
                print("nuc-console-peers: cannot open the log", file=sys.stderr)
                return 1
        return serve_main(net)  # allow_loopback and the rest only through `net`: not reachable from the command line
    usage = "usage: nuc-console-fleet list | test NAME | once | forget NAME  (the service: peers.py serve [--log FILE])"
    cfg = cfg or nuc_config.load()
    peers = cfg["hosts"]
    folder, state = nuc_config.peers_cache_dir(), nuc_config.peers_state_dir()
    if not argv or argv[0] not in ("list", "test", "once", "forget") or (argv[0] in ("test", "forget")) != (len(argv) == 2) or \
            (argv[0] in ("list", "once") and len(argv) != 1):
        out(usage)
        return 2
    if argv[0] == "forget":  # the only way to accept a replaced machine; works for a peer no longer in [hosts] too
        if not nuc_config.HOST_NAME_RE.fullmatch(argv[1]):
            out("not a peer name: %s" % re.sub(r"[^a-z0-9-]", "?", argv[1][:32]))
            return 2
        known = Known(state)
        if not known.forget(argv[1]):
            out("%s: no identity is remembered" % argv[1])
            return 0
        try:
            known.save(state)
        except OSError as err:
            out("cannot write the state in %s: %s" % (state, os.strerror(err.errno) if err.errno else "error"))
            return 1
        out("%s: identity forgotten; the next poll remembers whoever answers first" % argv[1])
        return 0
    if not peers:
        out("no peers: add some under [hosts] in config.ini (docs/CONFIGURATION.md)")
        return 0 if argv[0] == "list" else 1
    if argv[0] == "list":
        cached = read_cache(folder)
        for p in peers:
            e = cached["peers"].get(p["name"])
            try:
                shown = parse_url(p["url"]).display
            except ValueError:
                shown = "(invalid URL)"
            out("%s  %s  scope=%s%s%s  last: %s" % (p["name"], shown, p["scope"], " alert" if p["alert"] else "", " insecure" if p["insecure"] else "",
                                                       (e["state"] + ("" if not e["error"] else " (" + e["error"] + ")")) if e else "no cache"))
        if cached["fleet"] is None:
            out("no fleet.json in %s: the poller has not run (nuc-console-fleet once)" % folder)
        return 0
    if argv[0] == "test":
        peer = next((p for p in peers if p["name"] == argv[1]), None)
        if peer is None:
            out("no such peer: %s (see: nuc-console-fleet list)" % re.sub(r"[^a-z0-9-]", "?", argv[1][:32]))
            return 2
        e = poll_peer(peer, **net)
        Known(state).check(e, remember=False)
        say_entry(e, out)
        return 0 if e["state"] in SUMMARY_STATES + ("stale",) else 1
    poller = Poller(peers, folder, state=state, **net)
    try:
        entries = poller.run_once()
    except OSError as err:
        out("cannot write the cache in %s: %s" % (folder, os.strerror(err.errno) if err.errno else err))
        return 1
    finally:
        poller.pool.shutdown(wait=False)
    for e in entries:
        say_entry(e, out)
    return 0


# ---- The service (systemd unit nuc-console-peers, run.sh / run.ps1) ---------------------------------------------------------------------
def _cfg_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _log(kind, seen):
    """One line per kind, once: a word of a closed list, never an exception text, a path, a URL or a token."""
    if kind not in seen:
        seen.add(kind)
        print("nuc-console-peers: %s" % kind, file=sys.stderr, flush=True)


def _log_to(path, max_bytes=1 << 20):
    """stdout and stderr to `path` (Windows has no journal): the folder is made only if its parent exists, a link in the way is refused
    (O_NOFOLLOW; Windows has none and its folder is the installer's), a new file is 0600, a log over 1 MB is kept once as .1."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.mkdir(folder, 0o700)  # not makedirs: a missing parent is an error, not something to create
    try:
        if os.path.getsize(path) > max_bytes and not os.path.islink(path):
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    sys.stdout = sys.stderr = os.fdopen(fd, "a", buffering=1, encoding="utf-8", errors="replace")


def _close(poller):
    """Drops the polls not started yet (Future.cancel: Python 3.8 has no shutdown(cancel_futures)) and lets the one in flight end by itself."""
    for fut in list(getattr(poller, "running", {}).values()):
        fut.cancel()
    poller.pool.shutdown(wait=False)


def serve(stop, reload_flag, load=None, path=None, tick=1.0, make=None, **net):
    """The foreground loop of the service: one Poller for the peers of [hosts], rebuilt when config.ini changes (mtime/size) or when
    `reload_flag` (SIGHUP) is set; no peers = no Poller and no work, only the wait (an idle service costs a stat per tick). Whatever one
    round raises is logged once per kind and the loop goes on; `stop` (SIGTERM) ends it. -> 0."""
    load = load or (lambda: nuc_config.load(overlay=False, warn=lambda _line: None))
    
    path = path or nuc_config.config_path()
    make = make or Poller
    folder, state = nuc_config.peers_cache_dir(), nuc_config.peers_state_dir()
    seen, poller, current, stamp = set(), None, None, "unset"  # ponytail: a loader that raises is retried every tick (stamp not moved)
    while not stop.is_set():
        try:
            now_stamp = _cfg_stamp(path)
            if reload_flag.is_set() or now_stamp != stamp:
                reload_flag.clear()
                cfg = load()
                if cfg.get("config_error"):  # the file cannot be read or parsed now: keep what runs, cache included, and look again next tick
                    _log("config unreadable", seen)
                    hosts = current
                else:
                    stamp = now_stamp
                    hosts = cfg.get("hosts") or []
                if hosts != current:
                    if poller is not None:
                        _close(poller)
                    poller, current = None, hosts
                    if hosts:
                        poller = make(hosts, folder, state=state, **net)
                    else:
                        _log("idle: no peers in [hosts]", seen)
                        try:  # what the cache says of peers that are gone must not outlive them
                            write_cache(folder, [], time.time(), keep=set())
                        except OSError:
                            pass
            if poller is not None:
                poller.step()
        except Exception:  # noqa: BLE001  one bad round (or a broken file) must not stop the service
            _log("round failed", seen)
        stop.wait(tick)
    if poller is not None:
        _close(poller)  # a poll in flight ends by its own 5 s limit: the process leaves within ~10 s
    return 0


def serve_main(net=None, tick=1.0):
    """`peers.py serve`: signals (SIGTERM/SIGINT stop; SIGHUP, where the OS has it, reloads [hosts])."""
    import signal
    scrub_env()
    stop, reload_flag = threading.Event(), threading.Event()
    for name, fn in (("SIGTERM", lambda *_: stop.set()), ("SIGINT", lambda *_: stop.set()), ("SIGBREAK", lambda *_: stop.set()),
                     ("SIGHUP", lambda *_: reload_flag.set())):
        sig = getattr(signal, name, None)  # SIGHUP and friends do not exist on Windows
        if sig is not None:
            try:
                signal.signal(sig, fn)
            except (OSError, ValueError):
                pass
    return serve(stop, reload_flag, tick=tick, **(net or {}))


if __name__ == "__main__":
    sys.exit(main())
