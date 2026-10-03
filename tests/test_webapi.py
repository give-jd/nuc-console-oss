"""The data API of the web view: webapi.py's documents and events (pure), and /api/v1 on a real demo server (the same access rules as the pages,
JSON, ETag, the stream)."""
import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import cards  # noqa: E402
import prefs  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import web  # noqa: E402
import webapi  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_web import TOKEN, get_any as get, raw, serve  # noqa: E402

_SAVED = {}


def setUpModule():  # the demo servers' render.demo_defaults() renames the host and declares [webapps] and [expose] for good: put them back
    _SAVED.update(host=socket.gethostname, webapps=render.CFG["webapps"], expose=render.CFG["expose"], demo=render.DEMO)


def tearDownModule():
    socket.gethostname, render.CFG["webapps"], render.CFG["expose"], render.DEMO = _SAVED["host"], _SAVED["webapps"], _SAVED["expose"], _SAVED["demo"]


def stop(srv):
    srv.shutdown()
    srv.server_close()


def body_of(wire):
    head, _, body = wire.partition("\r\n\r\n")
    return head, body


def events(body):
    """The messages of a stream's body: [(id, data dict)], in order."""
    out = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
        if "data" in lines:
            out.append((lines.get("id"), json.loads(lines["data"])))
    return out


class Data(unittest.TestCase):
    def test_a_component_is_its_type_and_its_fields(self):
        self.assertEqual(webapi.data(ui.Span("8 GB", tone="warn", bold=True)),
                         {"type": "Span", "text": "8 GB", "tone": "warn", "bold": True, "mono": False, "full": None})
        card = webapi.data(ui.Card("system", "SYSTEM", "", "ok", [ui.Line([ui.Span("up 3 days")])], size=2))
        self.assertEqual(card["type"], "Card")
        self.assertEqual(card["body"][0]["spans"][0]["text"], "up 3 days")
        self.assertEqual(card["size"], 2)

    def test_inherited_fields_are_carried(self):
        """A Notice has no slots of its own: its level and text are Msg's (its _fields() would say nothing)."""
        self.assertEqual(webapi.data(ui.Notice("warn", "stale data")), {"type": "Notice", "level": "warn", "text": "stale data"})
        p = webapi.data(ui.Problem("err", "db open", "db-open-lan", "Database open", "why", "fix", "accept"))
        self.assertEqual((p["level"], p["text"], p["id"], p["fix"]), ("err", "db open", "db-open-lan", "fix"))

    def test_what_only_the_console_draws_is_left_out(self):
        col = webapi.data(ui.Col("name", "NAME", w=20, gap=2, clip=True))
        for k in ("w", "gap", "clip"):
            self.assertNotIn(k, col)
        self.assertEqual((col["key"], col["label"]), ("name", "NAME"))
        table = webapi.data(ui.Table([ui.Col("a", "A")], [ui.Row(["1"])], indent=4, fill=True))
        self.assertNotIn("indent", table)
        self.assertNotIn("fill", table)
        self.assertEqual(table["rows"][0]["cells"][0]["text"], "1")

    def test_only_and_cap_are_drawn_as_the_web_draws_them(self):
        nodes = [ui.Only("console", [ui.Msg("info", "console line")]), ui.Only("web", [ui.Msg("info", "web 1"), ui.Msg("info", "web 2")]),
                 ui.Cap([ui.Msg("info", "capped")], 1, "lines")]
        self.assertEqual([n["text"] for n in webapi.data(nodes)], ["web 1", "web 2", "capped"])

    def test_cols_are_their_children_without_the_consoles_widths(self):
        cols = webapi.data(ui.Cols([(ui.Msg("info", "left"), 40), (ui.Only("web", [ui.Msg("info", "right")]), None)], gap=3, h=10))
        self.assertEqual(cols, {"type": "Cols", "children": [{"type": "Msg", "level": "info", "text": "left"},
                                                              {"type": "Msg", "level": "info", "text": "right"}]})

    def test_a_bar_says_its_state(self):
        self.assertEqual(webapi.data(ui.Bar(0.95, "95%", 0.7, 0.9))["state"], "err")
        self.assertEqual(webapi.data(ui.Bar(None, "?"))["state"], "unknown")

    def test_raw_lines_lose_their_colours_and_control_characters(self):
        self.assertEqual(webapi.data(ui.Raw(["\x1b[31mred\x1b[0m", "bell\x07"])), {"type": "Raw", "lines": ["red", "bell?"]})

    def test_values(self):
        self.assertEqual(webapi.data({"a": (1, 2.5), 3: float("nan"), "inf": float("inf"), "none": None}),
                         {"a": [1, 2.5], "3": None, "inf": None, "none": None})
        class Thing(object):
            def __str__(self):
                return "a thing"
        self.assertEqual(webapi.data(Thing()), "a thing")

    def test_every_component_class_has_its_fields(self):
        for name in dir(ui):
            cls = getattr(ui, name)
            if isinstance(cls, type) and issubclass(cls, ui.Component) and cls is not ui.Component:
                own = [n for k in cls.__mro__ for n in getattr(k, "__slots__", ())]
                self.assertEqual(set(webapi.fields(cls)), set(own) - webapi.CONSOLE_ONLY, name)


class Documents(unittest.TestCase):
    def test_the_rev_is_the_data_not_the_time(self):
        a, b = webapi.document("cpu", {"x": 1}, 100.0), webapi.document("cpu", {"x": 1}, 200.0)
        self.assertEqual(a.rev, b.rev)
        self.assertNotEqual(a.rev, webapi.document("cpu", {"x": 2}, 100.0).rev)
        d = json.loads(a)
        self.assertEqual((d["api"], d["view"], d["rev"], d["at"], d["x"]), (webapi.VERSION, "cpu", a.rev, 100.0, 1))
        self.assertTrue(webapi.EVENT_ID.fullmatch(a.rev))

    def test_an_event_is_one_data_line(self):
        doc = webapi.document("ai", {"text": "two\nlines\r and   a separator"}, 1.0)
        ev = webapi.event(doc)
        self.assertTrue(ev.endswith(b"\n\n"))
        lines = ev[:-2].split(b"\n")
        self.assertEqual(lines[0], b"id: " + doc.rev.encode())
        self.assertEqual(len(lines), 2, "the JSON escapes CR and LF: one data line")
        self.assertEqual(json.loads(lines[1][len(b"data: "):].decode())["text"], "two\nlines\r and   a separator")
        self.assertNotIn(b"\r", ev)
        self.assertEqual(webapi.comment("keepalive"), b": keepalive\n\n")
        self.assertEqual(webapi.retry(3000), b"retry: 3000\n\n")

    def test_problems(self):
        pb = render.ProblemList([(2, "1 DB open on LAN"), (1, "journal errors"), (3, "port changed")])
        pb.pids = ["db-open-lan", "journal-errors", "port-changed"]
        pb.info = [("Database open", "why", "fix", "accept db"), None, ("Port changed", "w", "f", "")]
        pb.accepted, pb.known = 1, [{"id": "thermal", "text": "hot", "reason": "fan", "ts": 1}]
        out = webapi.problems(pb)
        self.assertEqual([(i["id"], i["state"]) for i in out["items"]], [("db-open-lan", "err"), ("journal-errors", "warn"), ("port-changed", "err")])
        self.assertEqual(out["items"][0]["cards"], ["databases", "exposure"])
        self.assertEqual(out["items"][0]["fix"], "fix")
        self.assertNotIn("fix", out["items"][1])
        self.assertEqual((out["accepted"], out["known"][0]["id"]), (1, "thermal"))
        plain = webapi.problems([(2, "something")])
        self.assertEqual(plain["items"], [{"id": None, "state": "err", "severity": 2, "text": "something", "cards": []}])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = serve()

    @classmethod
    def tearDownClass(cls):
        stop(cls.srv)

    def doc(self, path, headers=None):
        st, h, body = get(self.srv, path, headers=headers)
        self.assertEqual(st, 200, path)
        self.assertEqual(h["Content-Type"], "application/json; charset=utf-8")
        return h, json.loads(body)

    def test_the_index_says_what_there_is(self):
        _h, d = self.doc("/api/v1")
        self.assertEqual(d["api"], webapi.VERSION)
        self.assertEqual(d["views"], list(webapi.VIEWS))
        with mock.patch.dict(render.CFG["features"], {"cpu": False}):
            self.assertNotIn("cpu", self.doc("/api/v1/")[1]["views"])

    def test_every_view_is_a_document_with_the_page_headers(self):
        for view in webapi.VIEWS:
            h, d = self.doc("/api/v1/" + view)
            self.assertEqual((d["api"], d["view"]), (webapi.VERSION, view))
            self.assertEqual(h["ETag"], '"%s"' % d["rev"])
            self.assertEqual(h["Cache-Control"], "no-store")
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'none'", h["Content-Security-Policy"])
            self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin")

    def test_the_overview(self):
        _h, d = self.doc("/api/v1/overview")
        on = [c for c in prefs.CARDS if cards.enabled(c, render.CFG)]
        self.assertEqual([c["id"] for c in d["cards"]], on)
        self.assertTrue(all(c["type"] == "Card" and c["state"] in ui.STATES for c in d["cards"]))
        self.assertEqual([k["id"] for k in d["kpis"]], list(prefs.KPI_IDS))
        self.assertTrue({x["id"] for x in d["layout"]} <= set(on))
        self.assertIn(d["status"]["state"], ("ok", "warn", "err"))
        self.assertTrue(d["problems"]["items"], "the demo has problems")
        self.assertTrue(all(p["state"] in ("err", "warn") for p in d["problems"]["items"]))

    def test_the_summary_is_what_every_page_shows_above_its_screen(self):
        _h, d = self.doc("/api/v1/summary")
        self.assertEqual(set(d) - {"api", "view", "rev", "at"}, {"host", "status", "kpis", "stale", "restart", "problems", "badges"})
        self.assertEqual([k["id"] for k in d["kpis"]], list(prefs.KPI_IDS))
        self.assertIsInstance(d["stale"], bool)
        self.assertGreater(d["problems"], 0, "the demo has problems")
        self.assertIn("map", d["badges"])
        self.assertEqual(len(d["badges"]["health"]), 2, "errors and warnings")
        _h, o = self.doc("/api/v1/overview")
        self.assertEqual(o["badges"], d["badges"])
        self.assertEqual(o["status"], d["status"])

    def test_the_views_take_the_pages_parameters_checked(self):
        self.assertEqual(self.doc("/api/v1/cpu?sort=mem&sel=999999999")[1]["sort"], "mem")
        self.assertEqual(self.doc("/api/v1/cpu?sel=999999999")[1]["sel"], "", "no such process: dropped")
        self.assertEqual(self.doc("/api/v1/cpu?sort=bogus")[1]["sort"], "cpu")
        self.assertEqual(self.doc("/api/v1/health?period=30")[1]["period"], 30)
        self.assertEqual(self.doc("/api/v1/health?period=5&sel=nothing")[1]["period"], 7)
        self.assertEqual(self.doc("/api/v1/map?sel=0123456789")[1]["sel"], "0123456789")
        self.assertEqual(self.doc("/api/v1/ai?sel=no-such-model&confirm=delete-all")[1]["sel"], "")

    def test_the_ai_and_telegram_documents_carry_what_their_buttons_need_and_no_secret(self):
        _h, ai = self.doc("/api/v1/ai")
        self.assertEqual(ai["csrf"], self.srv.csrf)
        text = json.dumps(ai)
        self.assertIn('"type": "Action"', text)
        self.assertIn(self.srv.csrf, text)
        _h, tg = self.doc("/api/v1/telegram")
        self.assertEqual(tg["csrf"], self.srv.csrf)
        self.assertIn("/telegram/pair", tg["actions"])
        self.assertNotIn("now", tg["engine"])
        self.assertNotIn("token", json.dumps(tg).lower().replace("csrf", ""))
        with mock.patch.dict(render.CFG["ai"], {"web_actions": False}):
            with self.srv.lock:
                self.srv.cache.clear()  # the document of a second ago is kept for a second
            locked = self.doc("/api/v1/ai")[1]
            self.assertIsNone(locked["csrf"], "locked: no buttons, no token")
            self.assertNotIn('"Action"', json.dumps(locked))
        with self.srv.lock:
            self.srv.cache.clear()

    def test_a_document_that_did_not_change_is_304(self):
        h, _d = self.doc("/api/v1/telegram")
        st, h2, body = get(self.srv, "/api/v1/telegram", headers={"If-None-Match": h["ETag"]})
        self.assertEqual((st, h2["ETag"], body), (304, h["ETag"], ""))
        self.assertEqual(get(self.srv, "/api/v1/telegram", headers={"If-None-Match": '"0000000000000000"'})[0], 200)

    def test_head_is_get_without_the_body(self):
        wire = raw(self.srv, "/api/v1/telegram", [("Host", "localhost")], "HEAD")
        head, body = body_of(wire)
        self.assertIn(" 200 ", head.split("\r\n")[0])
        self.assertIn("Content-Length:", head)
        self.assertEqual(body, "")

    def test_unknown_views_and_off_features_are_404(self):
        for path in ("/api/v1/nope", "/api/v1/cpu/x", "/api/v2/overview", "/api", "/api/state"):
            self.assertEqual(get(self.srv, path)[0], 404, path)
        with mock.patch.dict(render.CFG["features"], {"health": False}):
            st, _h, body = get(self.srv, "/api/v1/health")
            self.assertEqual(st, 404)
            self.assertIn("[features] health = no", json.loads(body)["error"])

    def test_read_only(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            st, h, _b = get(self.srv, "/api/v1/overview", m)
            self.assertEqual((st, h["Allow"]), (405, "GET, HEAD"), m)

    def test_another_site_cannot_read_it(self):
        for site, code in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)):
            self.assertEqual(get(self.srv, "/api/v1/telegram", headers={"Sec-Fetch-Site": site})[0], code, site)
        self.assertEqual(get(self.srv, "/api/v1/stream?view=telegram", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_the_host_guard(self):
        self.assertIn(" 421 ", raw(self.srv, "/api/v1/overview", [("Host", "evil.example.com")]).split("\r\n")[0] + " ")
        self.assertIn(" 421 ", raw(self.srv, "/api/v1/stream", [("Host", "evil.example.com")]).split("\r\n")[0] + " ")


class Token(unittest.TestCase):
    def test_the_token_is_asked_as_for_a_page(self):
        srv = serve(TOKEN)
        try:
            self.assertEqual(get(srv, "/api/v1/overview")[0], 401)
            self.assertEqual(get(srv, "/api/v1/stream")[0], 401)
            self.assertEqual(get(srv, "/api/v1/overview", headers={"Authorization": "Bearer " + "x" * 24})[0], 401)
            self.assertEqual(get(srv, "/api/v1/telegram", headers={"Authorization": "Bearer " + TOKEN})[0], 200)
            self.assertEqual(get(srv, "/api/v1/telegram", headers={"Cookie": "nuc_token=" + TOKEN})[0], 200)
            st, h, _b = get(srv, "/api/v1/telegram?token=" + TOKEN)
            self.assertEqual(st, 200, "no redirect: a script asked, not a person")
            self.assertNotIn("Set-Cookie", h)
        finally:
            stop(srv)


class Stream(unittest.TestCase):
    def setUp(self):
        self.srv = serve()
        self.srv.stream_s, self.srv.keepalive_s, self.srv.stream_tick = 1.2, 0.3, 0.1
        self.addCleanup(stop, self.srv)

    def open(self, path="/api/v1/stream?view=telegram", headers=(), method="GET"):
        t0 = time.monotonic()
        wire = raw(self.srv, path, [("Host", "localhost")] + list(headers), method)
        return time.monotonic() - t0, body_of(wire)

    def test_the_first_event_is_the_document_then_it_ends(self):
        took, (head, body) = self.open()
        self.assertIn(" 200 ", head.split("\r\n")[0])
        self.assertIn("Content-Type: text/event-stream; charset=utf-8", head)
        self.assertNotIn("Content-Length", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertTrue(body.startswith("retry: 3000\n\n"))
        evs = events(body)
        self.assertTrue(evs)
        eid, d = evs[0]
        self.assertEqual((d["view"], d["rev"]), ("telegram", eid))
        self.assertGreaterEqual(took, 1.0, "it lasted stream_s")

    def test_the_request_deadline_does_not_cut_a_stream(self):
        get(self.srv, "/api/v1/telegram")  # built once: the stream starts at once, well within the deadline of its request
        with mock.patch.object(web, "DEADLINE_S", 0.5):
            took, (_head, body) = self.open()
        self.assertGreaterEqual(took, 1.0)
        self.assertIn(": keepalive", body, "written after the deadline of a request")

    def test_last_event_id_skips_what_the_client_has(self):
        rev = json.loads(get(self.srv, "/api/v1/telegram")[2])["rev"]
        _t, (_head, body) = self.open(headers=[("Last-Event-ID", rev)])
        self.assertEqual(events(body), [], "nothing changed: nothing sent")
        self.assertIn(": keepalive", body)
        _t, (_head, body) = self.open(headers=[("Last-Event-ID", "not-a-rev")])
        self.assertEqual(len(events(body)), 1, "an id that is not a rev is ignored")

    def test_a_view_and_its_parameters(self):
        _t, (_head, body) = self.open("/api/v1/stream?view=cpu&sort=mem")
        self.assertEqual(events(body)[0][1]["sort"], "mem")
        _t, (_head, body) = self.open("/api/v1/stream")
        self.assertEqual(events(body)[0][1]["view"], "overview", "the default view")
        self.assertEqual(get(self.srv, "/api/v1/stream?view=nope")[0], 404)
        with mock.patch.dict(render.CFG["features"], {"map": False}):
            self.assertEqual(get(self.srv, "/api/v1/stream?view=map")[0], 404)

    def test_one_stream_carries_a_screen_and_the_summary(self):
        _t, (_head, body) = self.open("/api/v1/stream?view=cpu&view=summary&sort=mem")
        got = {d["view"]: d for _id, d in events(body)}
        self.assertEqual(set(got), {"cpu", "summary"})
        self.assertEqual(got["cpu"]["sort"], "mem")
        rev = json.loads(get(self.srv, "/api/v1/telegram")[2])["rev"]
        _t, (_head, body) = self.open("/api/v1/stream?view=telegram&view=telegram", [("Last-Event-ID", rev)])
        self.assertEqual(events(body), [], "one view, named twice: its id is honoured")
        _t, (_head, body) = self.open("/api/v1/stream?view=telegram&view=summary", [("Last-Event-ID", rev)])
        self.assertEqual({d["view"] for _id, d in events(body)}, {"telegram", "summary"}, "an id is one view's rev: with two, both are sent")
        st, _h, body = get(self.srv, "/api/v1/stream?view=cpu&view=map&view=health&view=ai")
        self.assertEqual(st, 400)
        self.assertIn("at most 3 views", json.loads(body)["error"])
        self.assertEqual(get(self.srv, "/api/v1/stream?view=cpu&view=nope")[0], 404)

    def test_head_is_the_headers_only(self):
        took, (head, body) = self.open(method="HEAD")
        self.assertIn("text/event-stream", head)
        self.assertEqual(body, "")
        self.assertLess(took, 1.0)

    def test_streams_are_capped(self):
        for _ in range(web.STREAMS_MAX):
            self.assertTrue(self.srv.streams.acquire(blocking=False))
        try:
            st, h, body = get(self.srv, "/api/v1/stream?view=telegram")
            self.assertEqual((st, h["Retry-After"]), (503, "10"))
            self.assertIn("too many streams", json.loads(body)["error"])
        finally:
            for _ in range(web.STREAMS_MAX):
                self.srv.streams.release()

    def test_a_client_that_goes_away_frees_its_stream(self):
        self.srv.stream_s = 30
        c = socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5)
        c.sendall(b"GET /api/v1/stream?view=overview HTTP/1.0\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"200", c.recv(100))
        c.close()
        deadline = time.monotonic() + 10
        while self.free() < web.STREAMS_MAX:  # the next write fails and the slot comes back
            if time.monotonic() > deadline:
                self.fail("the stream of a closed connection still holds its slot")
            time.sleep(0.05)

    def free(self):
        n = 0
        while n < web.STREAMS_MAX and self.srv.streams.acquire(blocking=False):
            n += 1
        for _ in range(n):
            self.srv.streams.release()
        return n


if __name__ == "__main__":
    unittest.main()
