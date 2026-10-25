"""The Hosts page and /api/v1/hosts (docs/WEB.md, "Hosts"): the fleet as the poller's cache says it. Hermetic: a temporary cache folder written with
the poller's own writer, the real handler on a loopback port, the demo data. The web process must never touch the network: a test makes that fail."""
import html
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import fleetfix  # noqa: E402
import nuc_config  # noqa: E402
import peers  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402
import webcss  # noqa: E402
import webhosts  # noqa: E402
import webjs  # noqa: E402

VIEWER = "v" * 24
PEER_TOKEN = "p" * 40
POSIX = os.name == "posix"
NAMES = ("alpha", "bravo", "charlie", "delta", "echo")


def call(srv, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    out = (r.status, {k.title(): v for k, v in r.getheaders()}, r.read().decode("utf-8", "replace"))
    c.close()
    return out


def bearer(tok=VIEWER):
    return {"Authorization": "Bearer " + tok}


class Fleet(unittest.TestCase):
    """A server with five configured peers and a cache folder the tests fill."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cache = os.path.join(cls.dir, "cache")
        os.makedirs(cls.cache)
        cls.tokens = os.path.join(cls.dir, "peer.tokens")
        fd = os.open(cls.tokens, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("watcher summary %s\n" % PEER_TOKEN)
        cls.env = mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": cls.cache})
        cls.env.start()
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2, peer_tokens_file=cls.tokens)
        cls.srv = web.Server(("127.0.0.1", 0), cfg, VIEWER, demo=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.saved = (render.CFG.get("hosts"), dict(render.CFG["features"]), render.CFG["expose"])
        cls.hosts = [fleetfix.peer(n) for n in NAMES]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.env.stop()
        render.DEMO = False
        render.CFG["hosts"], render.CFG["expose"] = cls.saved[0], cls.saved[2]
        render.CFG["features"].clear()
        render.CFG["features"].update(cls.saved[1])
        __import__("shutil").rmtree(cls.dir, True)

    def setUp(self):
        render.CFG["hosts"] = list(self.hosts)
        render.CFG["features"]["fleet"] = True
        self.clear()
        self.now = time.time()
        self.fill(self.mixed())

    def clear(self):
        for fn in os.listdir(self.cache):
            os.remove(os.path.join(self.cache, fn))
        self.srv.cache.clear()

    def mixed(self):
        n = self.now
        return [fleetfix.entry("alpha", n),
                fleetfix.entry("bravo", n, "warn", counts=(0, 2, 0), worst=("Disk almost full", "A container exited")),
                fleetfix.entry("charlie", n, "unreachable", age=300, error="connect", warn=["showing-old-data"]),
                fleetfix.entry("delta", n, "ok", scope="full", skew_s=90, insecure=True),
                fleetfix.entry("echo", n, "ok", identity="changed", error="identity-changed")]

    def fill(self, entries, poller=True):
        self.clear()
        fleetfix.write(self.cache, entries, self.now, poller)
        self.srv.cache.clear()

    def api(self, path="/api/v1/hosts", **h):
        return call(self.srv, path, dict(bearer(), **h))

    def doc(self):
        st, _h, body = self.api()
        self.assertEqual(st, 200, body)
        return json.loads(body)

    def page(self, query="view=hosts", **h):
        st, headers, body = call(self.srv, "/?" + query, dict(bearer(), **h))
        return st, headers, html.unescape(body) if st == 200 else body

    # ---- the document ---------------------------------------------------------------------------------------------------------------------

    def test_the_document_has_the_schema_and_strict_types(self):
        d = self.doc()
        self.assertEqual((d["api"], d["view"]), (1, "hosts"))
        self.assertEqual(set(d["self"]), {"name", "host_id", "version", "os"})
        self.assertTrue(all(isinstance(d["self"][k], str) for k in d["self"]))
        self.assertEqual([h["id"] for h in d["hosts"]], list(NAMES))
        keys = {"id", "name", "url", "state", "status", "counts", "worst", "age_s", "skew_s", "version", "api", "scope", "insecure", "identity", "error"}
        for h in d["hosts"]:
            self.assertEqual(set(h), keys)
            for k in ("id", "name", "url", "state", "status", "version", "scope", "identity", "error"):
                self.assertIsInstance(h[k], str, (h["id"], k))
            self.assertIsInstance(h["insecure"], bool)
            self.assertIsInstance(h["worst"], list)
            self.assertLessEqual(len(h["worst"]), 3)
            self.assertIn(h["state"], peers.STATES)
            for k in ("age_s", "skew_s", "api"):
                self.assertTrue(h[k] is None or (isinstance(h[k], int) and not isinstance(h[k], bool)), (h["id"], k))
            self.assertTrue(h["counts"] is None or set(h["counts"]) == {"err", "warn", "unknown"})
        self.assertEqual(set(d["fleet"]), {"state", "poller"})
        self.assertEqual(d["fleet"]["poller"], "running")

    def test_the_document_has_what_the_cache_says_and_nothing_secret(self):
        d = self.doc()
        by = {h["id"]: h for h in d["hosts"]}
        self.assertEqual(by["alpha"]["state"], "ok")
        self.assertEqual(by["alpha"]["url"], "https://alpha.example.net")
        self.assertEqual(by["alpha"]["name"], "alpha-box")
        self.assertEqual(by["bravo"]["counts"], {"err": 0, "warn": 2, "unknown": 0})
        self.assertEqual(by["bravo"]["worst"], ["Disk almost full", "A container exited"])
        self.assertEqual((by["charlie"]["state"], by["charlie"]["error"], by["charlie"]["age_s"]), ("unreachable", "connect", 300))
        self.assertEqual(by["delta"]["scope"], "full")
        self.assertEqual(by["delta"]["skew_s"], 90)
        self.assertTrue(by["delta"]["insecure"])
        self.assertEqual((by["echo"]["state"], by["echo"]["identity"]), ("untrusted", "changed"))
        text = json.dumps(d)
        for secret in ("/nonexistent", ".token", "token", "pin", "sha256", PEER_TOKEN, VIEWER, "csrf", self.cache):
            self.assertNotIn(secret, text.replace('"scope"', ""), secret)

    def test_the_fleet_state_is_the_worst_and_never_ok_for_old_or_unreachable(self):
        self.assertEqual(self.doc()["fleet"]["state"], "unreachable")
        self.fill([fleetfix.entry("alpha", self.now), fleetfix.entry("bravo", self.now, "warn", counts=(0, 1, 0))])
        self.assertEqual(self.doc()["fleet"]["state"], "warn")
        self.fill([fleetfix.entry("alpha", self.now), fleetfix.entry("bravo", self.now, "stale")])
        self.assertEqual(self.doc()["fleet"]["state"], "stale")
        self.fill([fleetfix.entry(n, self.now) for n in NAMES])
        self.assertEqual(self.doc()["fleet"]["state"], "ok")
        self.fill([fleetfix.entry(n, self.now - 4000, ts=self.now - 4000) for n in NAMES], poller=False)
        d = self.doc()
        self.assertEqual({h["state"] for h in d["hosts"]}, {"stale"}, "an entry nobody refreshed is stale, not ok")
        self.assertNotEqual(d["fleet"]["state"], "ok")

    def test_the_ranking_puts_failures_over_warnings_over_stale_over_ok(self):
        t = webhosts.tier
        for s in ("unreachable", "untrusted", "unauthorized", "incompatible", "err", "down"):
            self.assertEqual(t(s), 3, s)
        self.assertEqual((t("warn"), t("stale"), t("unknown"), t("ok")), (2, 1, 1, 0))
        self.assertEqual(t("something-new"), 1, "a state nobody knows is not fine")
        self.assertGreater(webhosts.severity("unreachable"), webhosts.severity("warn"))
        self.assertGreater(webhosts.severity("warn"), webhosts.severity("stale"))
        self.assertGreater(webhosts.severity("stale"), webhosts.severity("ok"))

    def test_the_poller_is_running_stale_or_absent(self):
        self.assertEqual(self.doc()["fleet"]["poller"], "running")
        self.fill([fleetfix.entry(n, self.now) for n in NAMES], poller=False)
        d = self.doc()
        self.assertEqual(d["fleet"]["poller"], "absent")
        self.assertNotEqual(d["fleet"]["state"], "ok", "no poller, no knowledge")
        self.clear()
        self.fill([fleetfix.entry(n, self.now) for n in NAMES])
        old = os.path.join(self.cache, "fleet.json")
        with open(old) as f:
            doc = json.load(f)
        doc["ts"] = self.now - 1000
        os.remove(old)
        peers.write_json(self.cache, "fleet.json", doc, peers.FLEET_MAX)
        self.srv.cache.clear()
        d = self.doc()
        self.assertEqual(d["fleet"]["poller"], "stale")
        self.assertNotEqual(d["fleet"]["state"], "ok")

    def test_a_peer_with_no_file_is_unknown_with_a_reason(self):
        self.clear()
        d = self.doc()
        self.assertEqual({h["state"] for h in d["hosts"]}, {"unknown"})
        self.assertEqual({h["error"] for h in d["hosts"]}, {"no-data"})
        self.assertEqual(d["fleet"]["poller"], "absent")

    def test_hostile_cache_contents_are_types_not_trouble(self):
        n = self.now
        bad = fleetfix.entry("alpha", n)
        bad["summary"]["host"] = "<img src=x onerror=alert(2)>\x1b[31mred\x1b]0;title\x07 " + "x" * 5000
        bad["summary"]["status_text"] = "a\nb\x9b2Jc"
        bad["summary"]["worst"] = ["\x1b[2Jcleared"] * 9 + [5, None]
        bad["summary"]["counts"] = {"err": "7", "warn": True, "unknown": -1}
        bad["summary"]["version"] = "<script>alert(1)</script>"
        bad["url_display"] = "https://\x1b[0m" + "u" * 5000
        weird = fleetfix.entry("bravo", n)
        weird["state"] = "on-fire"           # not a state: the file is skipped
        wrong = fleetfix.entry("charlie", n)
        wrong["ts"] = "yesterday"
        missing = fleetfix.entry("delta", n)
        del missing["summary"]
        fleetfix.write(self.cache, [bad], n)
        for e in (weird, wrong, missing):
            peers.write_json(self.cache, e["name"] + ".json", e)
        with open(os.path.join(self.cache, "echo.json"), "w") as f:
            f.write("{" * 50000)
        self.srv.cache.clear()
        st, _h, body = self.api()
        self.assertEqual(st, 200)
        for bytes_ in ("\x1b", "\x07", "\x9b", "<script"):
            self.assertNotIn(bytes_, body)
        d = json.loads(body)
        by = {h["id"]: h for h in d["hosts"]}
        self.assertLessEqual(len(by["alpha"]["name"]), 120)
        self.assertEqual(by["alpha"]["url"], "https://alpha.example.net", "the address shown is the configured one, rebuilt: not the cache's text")
        self.assertIsNone(by["alpha"]["counts"], "counts of the wrong type are not counts")
        self.assertEqual(by["alpha"]["version"], "")
        for gone in ("bravo", "charlie", "echo"):
            self.assertEqual((by[gone]["state"], by[gone]["error"]), ("unknown", "no-data"), gone)
        self.assertEqual(by["delta"]["state"], "unknown", "an 'ok' with nothing behind it is not ok")
        page = call(self.srv, "/?view=hosts", bearer())[2]
        for bytes_ in ("\x1b", "\x07", "\x9b", "alert(1)", "<img"):
            self.assertNotIn(bytes_, page)

    def test_an_unknown_peer_in_the_cache_is_ignored(self):
        self.fill(self.mixed() + [fleetfix.entry("zulu", self.now, "err", counts=(9, 0, 0))])
        d = self.doc()
        self.assertEqual([h["id"] for h in d["hosts"]], list(NAMES))
        self.assertNotIn("zulu", json.dumps(d))

    # ---- access, headers, ETag --------------------------------------------------------------------------------------------------------------

    def test_the_document_has_the_checks_of_the_other_views(self):
        self.assertEqual(call(self.srv, "/api/v1/hosts")[0], 401, "no token")
        self.assertEqual(call(self.srv, "/api/v1/hosts", {"Authorization": "Bearer nope"})[0], 401)
        st, h, body = self.api()
        self.assertEqual((st, h["Cache-Control"], h["Content-Type"]), (200, "no-store", "application/json; charset=utf-8"))
        self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(self.api(**{"Sec-Fetch-Site": "cross-site"})[0], 403, "a page of another site cannot read it")
        etag = h["Etag"]
        self.assertEqual(json.loads(body)["rev"], etag.strip('"'))
        st, _h, body = self.api(**{"If-None-Match": etag})
        self.assertEqual((st, body), (304, ""))
        self.assertEqual(call(self.srv, "/api/v1/hosts", bearer(), "HEAD")[0], 200)
        st, _h, idx = self.api("/api/v1")
        self.assertIn("hosts", json.loads(idx)["views"])

    def test_the_rev_does_not_move_while_nothing_does(self):
        a = self.doc()["rev"]
        time.sleep(0.2)
        self.srv.cache.clear()
        self.assertEqual(self.doc()["rev"], a)

    def test_the_host_guard_applies_without_a_token(self):
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
        srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(call(srv, "/api/v1/hosts", {"Host": "evil.example"})[0], 421)
            self.assertEqual(call(srv, "/api/v1/hosts", {"Host": "localhost"})[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()
            render.DEMO = True

    def test_a_peer_token_is_refused_the_document_the_page_and_the_redirect(self):
        for path in ("/api/v1/hosts", "/api/v1/stream?view=hosts", "/?view=hosts", "/?view=hosts&go=alpha", "/?view=hosts&host=alpha"):
            st, h, body = call(self.srv, path, bearer(PEER_TOKEN))
            self.assertEqual(st, 403, path)
            self.assertNotIn("Location", h)
            self.assertNotIn("csrf", body.lower())
        self.assertIn(call(self.srv, "/api/v1/summary", bearer(PEER_TOKEN))[0], (200,))

    def test_a_cookie_does_not_make_a_peer_token_a_viewer(self):
        self.assertEqual(call(self.srv, "/api/v1/hosts", {"Cookie": "nuc_token=" + PEER_TOKEN})[0], 401)
        self.assertEqual(call(self.srv, "/api/v1/hosts?token=" + PEER_TOKEN)[0], 401)

    def test_the_stream_carries_the_document(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/api/v1/stream?view=hosts", headers=bearer())
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        got = b""
        while b'"view":"hosts"' not in got:
            got += r.fp.readline()
        c.close()
        self.assertIn(b'"view":"hosts"', got)

    # ---- off ------------------------------------------------------------------------------------------------------------------------------

    def off(self, how):
        if how == "empty":
            render.CFG["hosts"] = []
        else:
            render.CFG["features"]["fleet"] = False
        self.srv.cache.clear()

    def test_off_everything_is_as_without_the_fleet(self):
        _st, base, _b = call(self.srv, "/?view=nothing", bearer())
        for how in ("empty", "feature"):
            self.off(how)
            st, _h, body = self.api()
            self.assertEqual(st, 404, how)
            self.assertIn("no such view", body)
            self.assertNotIn("hosts", body.split("one of")[1])
            self.assertNotIn("hosts", json.loads(self.api("/api/v1")[2])["views"])
            self.assertEqual(self.api("/api/v1/stream?view=hosts")[0], 404)
            self.assertEqual(call(self.srv, "/api/v1/hosts", {"Authorization": "Bearer " + PEER_TOKEN})[0], 403)
            for query in ("view=hosts", "view=hosts&host=alpha", "view=hosts&go=alpha"):
                st, h, page = self.page(query)
                self.assertEqual(st, 200, (how, query))
                self.assertNotIn("Location", h)
                self.assertNotIn("fl-grid", page)
                self.assertNotIn("Hosts</a>", page)
                self.assertNotIn('data-key="6"', page)
                self.assertNotIn('class="host" href', page)
                self.assertNotIn('id="sec-hosts"', page)
            self.assertEqual(self.page("view=settings")[2].count("sec-hosts"), 0)
            self.assertEqual(self.page("view=hosts")[2].split("<main")[1].split("</main>")[0],
                             self.page("")[2].split("<main")[1].split("</main>")[0], "view=hosts is the overview")
        del base

    def test_the_overview_is_unchanged_while_off_and_has_the_tab_and_link_while_on(self):
        on = self.page("")[2]
        self.assertIn('data-key="6"', on)
        self.assertIn('<a class="host" href="/?view=hosts"', on)
        self.off("empty")
        off = self.page("")[2]
        self.assertNotIn('data-key="6"', off)
        self.assertIn('<span class="host">', off)

    # ---- the review's findings --------------------------------------------------------------------------------------------------------------

    def test_a_future_stamp_is_not_fresh(self):
        """ts or last_ok ahead of this clock by more than 60 s: stale with the error word clock, a banner, the fleet follows; 30 s is tolerated."""
        n = self.now
        for ahead, fresh in ((30 * 86400, False), (61, False), (30, True)):
            self.fill([fleetfix.entry("alpha", n, ts=n + ahead)] + [fleetfix.entry(x, n) for x in NAMES[1:]])
            d = self.doc()
            a = d["hosts"][0]
            self.assertEqual((a["state"], a["error"]) == ("ok", ""), fresh, (ahead, a))
            if not fresh:
                self.assertEqual((a["state"], a["error"]), ("stale", "clock"))
                self.assertNotEqual(d["fleet"]["state"], "ok")
                self.assertIn("poller's clock is ahead", self.page()[2])
            else:
                self.assertNotIn("poller's clock is ahead", self.page()[2])
        self.fill([fleetfix.entry("alpha", n, last_ok=int(n) + 3600)] + [fleetfix.entry(x, n) for x in NAMES[1:]])
        self.assertEqual(self.doc()["hosts"][0]["error"], "clock")

    def test_a_fleet_stamped_in_the_future_is_a_stale_poller(self):
        self.fill([fleetfix.entry(x, self.now) for x in NAMES])
        path = os.path.join(self.cache, "fleet.json")
        for ahead, want in ((30 * 86400, "stale"), (61, "stale"), (30, "running")):
            with open(path) as f:
                doc = json.load(f)
            doc["ts"] = self.now + ahead
            os.remove(path)
            peers.write_json(self.cache, "fleet.json", doc, peers.FLEET_MAX)
            self.srv.cache.clear()
            d = self.doc()
            self.assertEqual(d["fleet"]["poller"], want, ahead)
            if want == "stale":
                self.assertNotEqual(d["fleet"]["state"], "ok")

    def test_a_good_state_with_an_error_is_not_known(self):
        self.fill([fleetfix.entry("alpha", self.now, "ok", error="connect")] + [fleetfix.entry(x, self.now) for x in NAMES[1:]])
        self.assertEqual(self.doc()["hosts"][0]["state"], "unknown")

    def test_the_settings_row_of_the_fleet_says_when_there_are_no_hosts(self):
        self.assertIn("\u2714 on</span>", re.search(r'data-feature="fleet".*?</li>', self.page("view=settings")[2], re.S).group(0))
        render.CFG["hosts"] = []
        self.srv.cache.clear()
        row = re.search(r'data-feature="fleet".*?</li>', self.page("view=settings")[2], re.S).group(0)
        self.assertIn("on (no [hosts])", row)

    # ---- the page -------------------------------------------------------------------------------------------------------------------------

    def test_the_page_orders_the_cards_worst_first_with_symbol_and_word(self):
        st, h, page = self.page()
        self.assertEqual(st, 200)
        ids = re.findall(r'<article class="fl-card[^>]*aria-label="([^"]*)"', page)
        self.assertEqual(len(ids), 5)
        states = re.findall(r'<article class="fl-card[^>]*data-state="([^"]*)"', page)
        self.assertEqual([webhosts.tier(s) for s in states], sorted((webhosts.tier(s) for s in states), reverse=True))
        self.assertEqual(states[-1], "ok", "ok last")
        self.assertIn('<span class="chip st-down">✖ unreachable</span>', page)
        self.assertIn('<span class="chip st-warn">! warn</span>', page)
        self.assertIn('<span class="chip st-ok">✔ ok</span>', page)
        self.assertIn("Disk almost full", page)
        self.assertIn("2 warn", page)
        self.assertIn("connection: cannot connect", page)
        self.assertIn("ago", page)
        fleet = re.search(r'<section class="fl-head".*?</section>', page, re.S).group(0)
        self.assertIn("unreachable", fleet)
        self.assertNotIn("st-ok", fleet)
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_every_card_has_an_open_on_link_to_the_go_redirect(self):
        page = self.page()[2]
        for name in ("alpha-box", "bravo-box"):
            self.assertIn("Open on %s</a>" % name, page)
        links = re.findall(r'href="(/\?view=hosts&go=[^"]*)"', page)
        self.assertEqual(sorted(links), sorted("/?view=hosts&go=" + n for n in NAMES))
        self.assertNotRegex(page, r'href="https?:')

    def test_the_detail_card(self):
        st, _h, page = self.page("view=hosts&host=bravo")
        self.assertEqual(st, 200)
        self.assertIn("fl-detail", page)
        self.assertIn("nuc-console-fleet test bravo", page)
        self.assertIn("Open on bravo-box", page)
        self.assertNotIn("charlie-box", page)
        self.assertIn("all hosts", page)
        for query in ("view=hosts&host=nope", "view=hosts&host=" + "a" * 90, "view=hosts&host=../x", "view=hosts&host=%00"):
            page = self.page(query)[2]
            self.assertIn("fl-grid", page, query)
            self.assertNotIn("fl-detail", page, query)

    def test_banners(self):
        page = self.page()[2]
        self.assertIn("read through a token with full access", page)
        self.assertIn("delta-box: its clock differs from this machine's by 90 s", page)
        self.assertIn("nuc-console-fleet forget echo", page)
        self.assertIn("identity changed", page)
        self.assertIn("plain http on a local network", page)
        self.assertNotIn("poller is not running", page)
        self.fill([fleetfix.entry(n, self.now) for n in NAMES], poller=False)
        page = self.page()[2]
        self.assertIn("The peer poller is not running", page)
        self.assertIn("nuc-console-peers", page)
        self.assertIn("peers.py serve", page)
        self.fill([fleetfix.entry(n, self.now) for n in NAMES])
        page = self.page()[2]
        for gone in ("full access", "plain http", "differs from this machine", "identity changed", "poller is not running"):
            self.assertNotIn(gone, page)

    def test_duplicate_names_are_said(self):
        self.fill([fleetfix.entry("alpha", self.now, host="twin"), fleetfix.entry("bravo", self.now, host="TWIN"), fleetfix.entry("charlie", self.now),
                   fleetfix.entry("delta", self.now), fleetfix.entry("echo", self.now)])
        self.assertRegex(self.page()[2], r"(?i)Two machines are called twin")
        self.fill([fleetfix.entry(n, self.now) for n in NAMES])
        self.assertNotIn("Two machines are called", self.page()[2])

    def test_the_page_has_no_style_no_new_script_and_the_same_policy(self):
        st, h, page = self.page()
        self.assertNotIn("style=", page)
        self.assertNotIn("<style", page)
        csp = h["Content-Security-Policy"]
        _st, h2, _p = self.page("view=cpu")
        self.assertEqual(csp, h2["Content-Security-Policy"], "the same scripts, the same policy as the other pages")
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http", csp)
        self.assertNotIn("unsafe", csp)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
        self.assertEqual(set(scripts), {webjs.REFRESH_JS, webjs.KEYS_JS, webjs.PREFS_JS})
        for k in ("Access-Control-Allow-Origin", "Access-Control-Allow-Credentials"):
            self.assertNotIn(k, h)
        self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin")

    def test_the_page_uses_only_what_a_fragment_may_have(self):
        for query in ("view=hosts", "view=hosts&host=charlie"):
            unit = self
            frag = call(self.srv, "/?%s&frag=1" % query, bearer())
            self.assertEqual(frag[0], 200)
            self.assertIn("fl-card", frag[2])

            class Walk(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    unit.assertIn(tag, webjs.FRAG_TAGS, tag)
                    for k, v in attrs:
                        unit.assertIn(k, {x.lower() for x in webjs.FRAG_ATTRS}, (tag, k))
                        if k == "href":
                            unit.assertTrue(v.startswith(("/?", "#")), v)
            Walk().feed(frag[2])

    def test_the_fragment_refreshes_with_the_data(self):
        a = call(self.srv, "/?view=hosts&frag=1", bearer())
        self.assertIn("Etag", a[1])
        self.fill([fleetfix.entry(n, self.now, "err", counts=(3, 0, 0)) for n in NAMES])
        b = call(self.srv, "/?view=hosts&frag=1", bearer())
        self.assertNotEqual(a[1]["Etag"], b[1]["Etag"])
        self.assertIn("3 err", b[2])

    def test_the_settings_page_lists_the_hosts_read_only(self):
        st, _h, page = self.page("view=settings")
        sec = re.search(r'<section class="sec" id="hosts".*?</section>', page, re.S).group(0)
        for n in NAMES:
            self.assertIn(n, sec)
        self.assertIn("nuc-console-fleet test alpha", sec)
        self.assertIn("token summary", sec)
        self.assertIn("token full", sec)
        self.assertNotIn("<form", sec)
        self.assertNotIn("<input", sec)
        self.assertNotIn("/nonexistent", sec)
        self.assertNotIn(".token", sec)

    # ---- go -------------------------------------------------------------------------------------------------------------------------------

    def test_go_redirects_to_the_configured_address_only(self):
        st, h, body = self.page("view=hosts&go=alpha")
        self.assertEqual((st, h["Location"]), (302, "https://alpha.example.net/"))
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertEqual(h["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Set-Cookie", h)
        self.assertEqual(body, "")
        render.CFG["hosts"] = [fleetfix.peer("alpha", url="http://100.64.1.7:8787/base/"), fleetfix.peer("bravo", url="https://[fd7a:115c:a1e0::1]:9/")]
        self.assertEqual(self.page("view=hosts&go=alpha")[1]["Location"], "http://100.64.1.7:8787/base/")
        self.assertEqual(self.page("view=hosts&go=bravo")[1]["Location"], "https://[fd7a:115c:a1e0::1]:9/")

    def test_go_matrix(self):
        for go in ("nope", "ALPHA", "alpha ", "http://evil.example", "//evil.example", "https://alpha.example.net", "..", "../alpha", "alpha/..", "alpha%0d%0aX: y",
                   "a" * 33, "a" * 5000, "", "-alpha", "alpha.json", "fleet", "%00", "alpha;evil", "alpha\\evil", "alpha@evil.example"):
            st, h, _b = self.page("view=hosts&go=" + urllib.parse.quote(go, safe=":/@;\\"))
            self.assertEqual(st, 200 if go == "" else 404, repr(go))  # go= with nothing is no go: the page
            self.assertNotIn("Location", h, repr(go))
        st, h, _b = self.page("view=hosts&go=alpha&go=evil")
        self.assertEqual(h["Location"], "https://alpha.example.net/", "the first, checked")
        st, h, _b = self.page("view=hosts&go=evil&go=alpha")
        self.assertEqual(st, 404)

    def test_go_follows_the_access_rules_and_does_not_work_across_sites(self):
        self.assertEqual(call(self.srv, "/?view=hosts&go=alpha")[0], 401)
        st, h, _b = self.page("view=hosts&go=alpha", **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((st, h["Location"]), (303, "/?view=hosts"))
        for ok in ("same-origin", "none"):
            self.assertEqual(self.page("view=hosts&go=alpha", **{"Sec-Fetch-Site": ok})[0], 302)

    def test_go_is_not_a_redirect_for_a_bad_configured_url(self):
        render.CFG["hosts"] = [fleetfix.peer("alpha", url="javascript:alert(1)"), fleetfix.peer("bravo", url="https://user:pw@h.example/"),
                               fleetfix.peer("charlie", url="ftp://h.example/"), fleetfix.peer("delta", url="https://h.example/?q=1")]
        for n in ("alpha", "bravo", "charlie", "delta"):
            self.assertEqual(self.page("view=hosts&go=" + n)[0], 404, n)
        self.assertIsNone(webhosts.go_target(render.CFG["hosts"], None))

    # ---- no network, and the mobile rules -------------------------------------------------------------------------------------------------

    def test_the_web_process_opens_no_connection_to_anybody(self):
        """The tests' own client is in this process, so the sockets cannot be forbidden: the client's functions are, and the module that draws the
        page does not import anything that connects."""
        def refuse(*a, **k):
            raise AssertionError("the web process talked to a peer")
        import ast
        with open(webhosts.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imported & {"socket", "ssl", "http", "urllib", "subprocess", "threading", "asyncio", "select", "selectors"}, set())
        with mock.patch.object(peers, "get", refuse), mock.patch.object(peers, "poll_peer", refuse), mock.patch.object(peers, "_connect", refuse), \
                mock.patch.object(peers, "_resolve", refuse), mock.patch.object(peers, "read_secret", refuse), mock.patch.object(peers, "Poller", refuse):
            self.srv.cache.clear()
            self.assertEqual(self.api()[0], 200)
            self.assertEqual(self.page()[0], 200)
            self.assertEqual(self.page("view=hosts&host=alpha")[0], 200)
            self.assertEqual(self.page("view=settings")[0], 200)
            self.assertEqual(self.page("view=hosts&go=alpha")[0], 302)

    def test_the_stylesheet_is_mobile_first(self):
        css = webcss.CSS
        grid = re.search(r"\.fl-grid\{([^}]*)\}", css).group(1)
        self.assertIn("grid-template-columns:minmax(0,1fr)", grid, "one column by default")
        self.assertIn("@container app (min-width:34em){.fl-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}", css)
        self.assertIn("@container app (min-width:64em){.fl-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}", css)
        self.assertRegex(css, r"\.fl-act \.btn\{min-height:44px")
        self.assertRegex(css, r"\.fl-act \.lnk\{[^}]*min-height:44px")
        self.assertRegex(css, r"a\.host\{[^}]*min-height:44px")
        self.assertIn("@container app (max-width:33.9em){.fl-act .btn{flex:1 1 100%}}", css)
        for rule in (".fl-card p{margin:0;min-width:0;overflow-wrap:anywhere}", ".fl-top h3{"):
            self.assertIn(rule, css)
        self.assertIn("overflow-wrap:anywhere", re.search(r"\.fl-top h3\{([^}]*)\}", css).group(0))
        hosts = css[css.index(".fl{display:grid"):css.index(".mapv>*{")]
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", hosts), [], "tokens only")

    def test_the_new_switch_is_everywhere(self):
        import confedit
        self.assertIn("fleet", nuc_config.FEATURES)
        self.assertIn("fleet", [k for k, _t, _w in confedit.FEATURE_WORDS])
        self.assertTrue(nuc_config.load("/nonexistent")["features"]["fleet"])
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(root, "config", "config.ini"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^fleet\s+= yes")
        with open(os.path.join(root, "docs", "CONFIGURATION.md"), encoding="utf-8") as f:
            self.assertIn("`fleet`", f.read())


class Pure(unittest.TestCase):
    def test_quant(self):
        self.assertEqual([webhosts.quant(x) for x in (0, 4, 5, 12, 59, 60, 119, 120, 3601, -5)], [0, 0, 5, 10, 55, 60, 60, 120, 3600, 0])

    def test_build_without_a_cache_folder(self):
        d = webhosts.build([fleetfix.peer("a")], os.path.join(tempfile.gettempdir(), "no-such-folder-" + "x" * 8), {"name": "me"}, 1000.0)
        self.assertEqual((d["hosts"][0]["state"], d["fleet"]["poller"]), ("unknown", "absent"))

    def test_go_target(self):
        hosts = [fleetfix.peer("a", url="https://a.example:8443/x")]
        self.assertEqual(webhosts.go_target(hosts, "a"), "https://a.example:8443/x/")
        for bad in ("b", "", None, 5, "A", "a/", "a" * 40):
            self.assertIsNone(webhosts.go_target(hosts, bad), repr(bad))


if __name__ == "__main__":
    unittest.main()
