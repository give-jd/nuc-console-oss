"""A fake nuc-console peer for the tests of the fleet client (test_peers.py): an http.server on 127.0.0.1, port 0, that answers like a
peer's web API and misbehaves on request. Nothing leaves the machine.

    peer = FakePeer(token="x" * 20)           # answers only a request that carries "Authorization: Bearer <token>"
    peer.start()                              # peer.port; peer.stop() at the end (addCleanup)
    peer.mode = "slow-headers"                # for every path; peer.modes["/api/v1/cpu"] = "..." for one
    peer.requests                             # [(path, {header: value}), ...] what arrived; peer.sent: bytes written in the last answer

Modes: normal, old (a 2.5.x summary: no host_id, counts, worst, version, os), newer (api 2), stale, down, redirect-301/302/307 (to peer.target),
unauthorized (401), forbidden (403), notfound, error500, html (wrong content type), gzip, empty, not-json, nested (100k levels), bigstring (a
10 MB string), hostile (escape sequences and wrong types in the fields), types (wrong types only), huge-length (Content-Length of 10 GB),
infinite-chunked, slow-headers, slow-body (one byte at a time), silent (reads the request, never answers), huge-header.
TLS: FakePeer(tls=True) serves tests/fixtures/peer-test.pem (a self-signed certificate for peer.test, localhost and 127.0.0.1; its key is
published on purpose and protects nothing).
"""
import json
import os
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PEM = os.path.join(HERE, "fixtures", "peer-test.pem")
HOST_ID = "0123456789abcdef"


def summary(**over):
    doc = {"api": 1, "view": "summary", "rev": "r1", "at": time.time(), "host": "nuc-b", "host_id": HOST_ID, "version": "2.6.0", "os": "linux",
           "status": {"state": "ok", "text": "ALL OK"}, "counts": {"err": 0, "warn": 0, "unknown": 0}, "worst": [], "stale": False,
           "problems": 0, "kpis": [], "badges": {}, "restart": "sudo systemctl restart nuc-console"}
    doc.update(over)
    return doc


def old_summary():
    """What 2.5.2 answers: no host_id, version, os, counts or worst."""
    doc = summary()
    for k in ("host_id", "version", "os", "counts", "worst"):
        del doc[k]
    return doc


def view(name, **over):
    doc = {"api": 1, "view": name, "rev": "v1", "at": time.time(), "csrf": "must-not-survive", "nodes": [{"title": "x", "token": "t", "n": 1}]}
    doc.update(over)
    return doc


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def date_time_string(self, timestamp=None):
        return BaseHTTPRequestHandler.date_time_string(self, time.time() + self.server.peer.date_offset)

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (OSError, ssl.SSLError):
            self.close_connection = True

    def do_GET(self):
        peer = self.server.peer
        peer.requests.append((self.path, {k: v for k, v in self.headers.items()}))
        peer.sent = 0
        self.close_connection = True
        mode = peer.modes.get(self.path, peer.mode)
        if peer.token is not None and self.headers.get("Authorization") != "Bearer " + peer.token:
            return self.answer(401, b'{"error":"token"}')
        getattr(self, "m_" + mode.replace("-", "_"))()

    # -- helpers
    def answer(self, status, body, ctype="application/json", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.server.peer.sent += len(body)

    def doc(self):
        peer = self.server.peer
        name = self.path.rsplit("/", 1)[-1]
        if name in peer.docs:
            return peer.docs[name]
        return summary() if name == "summary" else view(name)

    def m_normal(self):
        self.answer(200, json.dumps(self.doc()).encode())

    def m_old(self):
        self.answer(200, json.dumps(old_summary() if self.path.endswith("summary") else self.doc()).encode())

    def m_newer(self):
        self.answer(200, json.dumps(dict(self.doc(), api=2, surprise={"a": [1]})).encode())

    def m_stale(self):
        self.answer(200, json.dumps(dict(self.doc(), stale=True)).encode())

    def m_down(self):
        self.answer(200, json.dumps(dict(self.doc(), status={"state": "down", "text": "x"})).encode())

    def redirect(self, code):
        self.answer(code, b"", extra=(("Location", self.server.peer.target),))

    def m_redirect_301(self):
        self.redirect(301)

    def m_redirect_302(self):
        self.redirect(302)

    def m_redirect_307(self):
        self.redirect(307)

    def m_unauthorized(self):
        self.answer(401, b"<html>secret remote text</html>", "text/html")

    def m_forbidden(self):
        self.answer(403, b"secret remote text")

    def m_notfound(self):
        self.answer(404, b"secret remote text")

    def m_error500(self):
        self.answer(500, b"secret remote text")

    def m_html(self):
        self.answer(200, b"<html>ok</html>", "text/html")

    def m_gzip(self):
        self.answer(200, b"\x1f\x8b", extra=(("Content-Encoding", "gzip"),))

    def m_empty(self):
        self.answer(200, b"")

    def m_not_json(self):
        self.answer(200, b"{not json")

    def m_nested(self):
        self.answer(200, b"[" * 100000 + b"]" * 100000)

    def m_bigstring(self):
        self.answer(200, b'{"view":"summary","api":1,"host":"' + b"A" * (10 * 1024 * 1024) + b'"}')

    def m_hostile(self):
        esc = "\x1b[31mred\x1b[0m\x9b2J\x07bell\nline\u202eRTL"
        self.answer(200, json.dumps(summary(host=esc, version=esc, os=esc, host_id=esc, status={"state": "ok", "text": esc},
                                           worst=[esc, esc, esc, esc, 5, None], counts={"err": 1, "warn": "x", "evil": 1})).encode())

    def m_types(self):
        self.answer(200, json.dumps({"api": "1", "view": "summary", "host": 5, "host_id": 1, "version": [], "os": {}, "status": 7,
                                     "counts": "many", "worst": "none", "stale": "yes", "at": "now"}).encode())

    def m_types2(self):
        self.answer(200, json.dumps(summary(status={"state": "evil"}, counts={"err": -5, "warn": True, "unknown": 10 ** 12}, worst={"a": 1},
                                           stale=1, at=1e308)).encode())

    def m_huge_length(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(10 * 1024 ** 3))
        self.end_headers()
        self.wfile.write(b'{"view":"summary"')
        self.wfile.flush()
        self.server.peer.stop_evt.wait(1.5)

    def m_infinite_chunked(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = b"%x\r\n" % 1024 + b" " * 1024 + b"\r\n"
        while not self.server.peer.stop_evt.is_set():
            self.wfile.write(chunk)
            self.wfile.flush()
            self.server.peer.sent += 1024

    def m_slow_headers(self):
        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        while not self.server.peer.stop_evt.is_set():
            self.wfile.write(b"a")
            self.wfile.flush()
            self.server.peer.stop_evt.wait(0.05)

    def m_slow_body(self):
        body = json.dumps(summary()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for i in range(len(body)):
            if self.server.peer.stop_evt.is_set():
                return
            self.wfile.write(body[i:i + 1])
            self.wfile.flush()
            self.server.peer.stop_evt.wait(0.05)

    def m_silent(self):
        self.server.peer.stop_evt.wait(30)

    def m_huge_header(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Pad", "a" * 70000)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that goes away (or refuses the certificate) is the point of many tests

    def get_request(self):
        sock, addr = self.socket.accept()
        if self.peer.ctx is not None:
            sock = self.peer.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)  # the handshake happens in the handler's thread
        return sock, addr


class FakePeer(object):
    def __init__(self, token=None, tls=False, mode="normal"):
        self.token, self.mode, self.modes, self.docs = token, mode, {}, {}
        self.requests, self.sent, self.date_offset, self.target = [], 0, 0, "http://127.0.0.1:9/never"
        self.stop_evt = threading.Event()
        self.ctx = None
        if tls:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(PEM)
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.peer = self
        self.port = self.server.server_address[1]
        self.thread = None

    @property
    def url(self):
        return "%s://127.0.0.1:%d" % ("https" if self.ctx else "http", self.port)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_evt.set()
        self.server.shutdown()
        self.server.server_close()
