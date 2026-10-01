"""The web AI page (web.py ?view=ai): the link, the models as links that carry the view, sel validation, the catalog read once per ttl, the
feature switch, escaping, the Windows and macOS demos, no JavaScript, and the advisor's cached advice on the HEALTH page.

Hermetic: the demo machines (src/demo.py) through a real server on a loopback port; no aisetup, no hardware and no network is read; every module
global a test changes is put back in tearDown.
"""
import html
import http.client
import os
import re
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import advisor  # noqa: E402
import demo  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import web  # noqa: E402

ESC, BEL, CSI8 = chr(27), chr(7), chr(0x9b)
EVIL = '<script>alert(1)</script>"onmouseover=alert(1) \'x ' + ESC + "[2J" + ESC + "]0;pwn" + BEL + CSI8
ROW = re.compile(r'<tr( class="sel")? id="m-(\d+)">(.*?)</tr>')
PILL = re.compile(r'<span class="pl (\w)">([^<]*)</span>')


def serve():
    cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
    srv = web.Server(("127.0.0.1", 0), cfg, "", demo=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def get(srv, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), body


def rows_of(page):
    """[(index, selected, href of the name link, name, params, verdict label, verdict class, marks)] of the models table of an AI page."""
    out = []
    for sel, i, inner in ROW.findall(page):
        m = re.search(r'<td class="mk">(.*?)</td><td[^>]*><a class="lb" href="([^"]*)">([^<]*)</a></td><td class="pm">([^<]*)</td>', inner)
        p = PILL.search(inner)
        marks = "".join(re.findall(r">([★✓●])<", m.group(1)))
        out.append((int(i), bool(sel), html.unescape(m.group(2)), html.unescape(m.group(3)), html.unescape(m.group(4)), html.unescape(p.group(2)), p.group(1), marks))
    return out


def link(page, text):
    m = re.search(r'<a href="([^"]*)">' + re.escape(text) + "</a>", page)
    return html.unescape(m.group(1)) if m else None


def params(url):
    return web.view_params(web.parse_qs(web.urlsplit(url).query))


def plain(page):
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<style>.*?</style>", "", page, flags=re.S)))


def model(mid, verdict="gpu", name=None, rank=1, **kw):
    a = {"verdict": verdict, "where": "GPU", "need_mb": 5000, "gpu_layers": 36, "tok_s": [20, 40], "why": "why " + mid}
    m = {"id": mid, "name": name or mid, "license": "Apache-2.0", "params_b": 8.0, "quant": "Q4_K_M", "layers": 36, "ctx_max": 32768, "rank": rank,
         "approx_mb": 4400, "notes": "note", "assess": a, "installed": False, "pinned": True,
         "commands": {"install": "sudo nuc-console-ai setup " + mid, "use": "sudo nuc-console-ai use " + mid, "remove": "sudo nuc-console-ai remove " + mid}}
    m.update(kw)
    return m


class AiPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host, cls.webapps, cls.expose = socket.gethostname, render.CFG["webapps"], render.CFG["expose"]  # demo_defaults() changes them for good: put them back
        cls.srv = serve()
        cls.saved = (dict(render.CFG["features"]), render.DEMO_OS, render.ai_build, render.ai_status, dict(render.CFG["ai"]), web.health_extra_html)

    @classmethod
    def tearDownClass(cls):
        render.DEMO = False
        socket.gethostname, render.CFG["webapps"], render.CFG["expose"] = cls.host, cls.webapps, cls.expose
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        render.DEMO, render.DEMO_OS = True, None
        render.CFG["features"]["ai"] = True
        render._AI.clear()
        self.srv.cache.clear()
        self.builds = []                                                     # the (demo, os) of every ai_build()
        real = render.ai_build
        render.ai_build = lambda now: (self.builds.append(render.DEMO_OS), real(now))[1]

    def tearDown(self):
        features, render.DEMO_OS, render.ai_build, render.ai_status, ai, web.health_extra_html = self.saved
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        render.CFG["ai"].clear()
        render.CFG["ai"].update(ai)
        render._AI.clear()
        render._ADVICE.clear()
        render._HEALTH.clear()

    def page(self, path):
        st, h, body = get(self.srv, path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body, path)
        return body

    def seed(self, cat, status=None):
        render._AI["hit"] = {"cat": cat, "msg": "", "err": False, "at": time.time(), "key": (render.DEMO, render.DEMO_OS)}
        if status is not None:
            render.ai_status = lambda wait=0.0: status

    # ---- the link ---------------------------------------------------------------------------------------------------------------------

    def test_ai_link_in_the_bottom_bar_with_and_without_the_feature(self):
        dash = self.page("/")
        self.assertEqual(link(dash, "ai"), "/?view=ai")
        self.assertEqual(link(dash, "health"), "/?view=health")              # next to it, after it
        self.assertLess(dash.index(">map</a>"), dash.index(">cpu</a>"))
        self.assertLess(dash.index(">health</a>"), dash.index(">ai</a>"))
        self.assertLess(dash.index(">ai</a>"), dash.index("read-only"))
        self.assertEqual(link(self.page("/?cols=100&zoom=125"), "ai"), "/?view=ai&cols=100&zoom=125")   # the size is kept
        render.CFG["features"]["ai"] = False
        self.srv.cache.clear()
        off = self.page("/")
        self.assertIsNone(link(off, "ai"))
        self.assertNotIn("view=ai", off)
        self.assertIn(">health</a>", off)                                    # the others stay
        self.assertEqual(self.builds, [])                                    # the dashboard never reads the hardware

    def test_feature_off_the_page_says_so_and_reads_nothing(self):
        render.CFG["features"]["ai"] = False
        st, h, off = get(self.srv, "/?view=ai&sel=qwen3-4b")
        self.assertEqual(st, 200)
        self.assertIn("AI screen disabled in config.ini", off)
        self.assertIn("[features] ai = no", off)
        self.assertNotIn("<table", off)
        self.assertNotIn("http-equiv", off)                                  # nothing to reload
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(link(off, "dashboard"), "/?")
        self.assertEqual(self.builds, [])

    # ---- the page ---------------------------------------------------------------------------------------------------------------------

    def test_page_has_header_title_hardware_models_status_and_is_locked_down(self):
        st, h, body = get(self.srv, "/?view=ai")
        self.assertEqual(st, 200)
        csp = h["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)                                  # unchanged: no script on this page
        self.assertEqual(csp, web.CSP)
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"]), ("no-store", "nosniff"))
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<form", body.lower())
        self.assertNotIn("<input", body.lower())
        self.assertIsNone(re.search(r"\son[a-z]+=", body))                   # no inline event handler
        self.assertIn("demo-host │ AI", body)
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        self.assertIn("<title>demo-host · ai · nuc-console</title>", body)
        txt = plain(body)
        self.assertIn("what this machine can run · 12 models · ✔ 10 fit  ◐ 2 gpu+cpu", txt)
        for word in ("── HARDWARE ", "AMD Ryzen 7 5800X", "21.0 GB free of 31.2 GB", "NVIDIA GeForce RTX 3060 · CUDA", "── STATUS ", "✔ answering · 1 model: qwen3-4b",
                     "● qwen3-4b  in the catalog", "MODELS · best first", "recommended", "installed", "tok/s: rough estimate"):
            self.assertIn(word, txt)
        rs = rows_of(body)
        self.assertEqual(len(rs), 12)
        self.assertEqual([r[3] for r in rs][:4], ["OpenAI gpt-oss 20B", "Qwen3 30B-A3B", "Phi-4 14B", "Qwen3 14B"])    # best first
        self.assertEqual(len({r[0] for r in rs}), 12)                        # one anchor per model
        for i, sel, href, name, par, label, cls, marks in rs:                # the browser stays on the clicked row; the id is in the URL
            self.assertTrue(href.endswith("#m-%d" % i), href)
            self.assertIn(params(href)["sel"], {r["id"] for r in render.ai_rows(render.ai_data()["cat"])})
        self.assertEqual({r[5] for r in rs}, {"◐ GPU+CPU", "✔ FITS GPU"})    # a symbol besides the colour
        self.assertEqual([r[7] for r in rs if r[7]], ["★", "✓●", "✓"])      # recommended, installed and active, installed
        self.assertNotIn("DETAILS", txt)                                     # nothing selected: no panel
        self.assertEqual(self.builds, [None])

    def test_the_verdict_pills_of_every_demo_machine(self):
        render.DEMO_OS = "windows"
        body = self.page("/?view=ai")
        seen = {(m.group(1), m.group(2)) for m in PILL.finditer(body)}
        self.assertEqual(seen, {("y", "! SLOW"), ("r", "✖ TOO BIG"), ("c", "◐ GPU+CPU"), ("g", "✔ FITS GPU")})
        for cls in ("g", "c", "y", "r"):
            self.assertIn(".pl.%s{" % cls, body)                             # every pill has its colour in the page's style
        self.assertIn('<td class="no"><a', body)                             # a model that is too big is dimmed

    def test_footer_links_keep_the_view(self):
        body = self.page("/?view=ai&sel=phi-4&zoom=125&refresh=5&cols=100")
        self.assertEqual(link(body, "dashboard"), "/?cols=100&zoom=125&refresh=5")
        p = params(link(body, "pause"))
        self.assertEqual((p["view"], p["sel"], p["zoom"], p["refresh"], p["cols"], p["pause"]), ("ai", "phi-4", 125, 5, 100, True))
        self.assertTrue(link(body, "pause").endswith("#m-2"))                # the selected model stays in sight
        self.assertEqual(params(link(body, "compact"))["cols"], 100)
        self.assertEqual(params(link(body, "wide"))["cols"], 200)
        self.assertEqual(params(link(body, "A+"))["zoom"], 150)
        self.assertEqual(params(link(body, "+"))["refresh"], 6)
        self.assertIn("read-only", body)
        self.assertIsNone(link(body, "map"))                                 # the bar of this page, not the dashboard's

    def test_hardware_follows_the_width_asked(self):
        def width(page):
            pre = re.findall(r'<pre class="ht">(.*?)</pre>', page, re.S)[1]
            return max(len(x) for x in plain(pre).split("\n"))
        self.assertGreater(width(self.page("/?view=ai&cols=200")), width(self.page("/?view=ai&cols=100")))
        self.assertLessEqual(width(self.page("/?view=ai&cols=100")), 100)

    def test_selecting_a_model_shows_its_details_and_commands_beside_or_under_the_table(self):
        body = self.page("/?view=ai")
        first = rows_of(body)[4]                                             # qwen3-8b
        href = first[2].split("#")[0]
        sel = self.page(href)
        rs = rows_of(sel)
        self.assertEqual([r[1] for r in rs].count(True), 1)
        self.assertTrue(rs[4][1])
        self.assertIn('<aside class="dp" id="details">', sel)
        self.assertIn('<main class="mp two">', sel)
        panel = sel[sel.index('<aside class="dp"'):sel.index("</aside>")]
        txt = plain(panel)
        for word in ("Qwen3 8B", "✔ FITS GPU", "needs 5.8 GB, the NVIDIA GeForce RTX 3060 has 11.0 GB free: all on the GPU", "about 31-51 tokens/s (a rough estimate, not a promise)",
                     "all 36 layers on the GPU", "8.2B parameters · Q4_K_M · context up to 40960 tokens", "Apache-2.0", "thinking mode: /no_think turns it off", "not installed"):
            self.assertIn(word, txt)
        self.assertIn('<code class="cmd">sudo nuc-console-ai setup qwen3-8b</code>', panel)   # the exact command, selectable in one click
        self.assertNotIn("nuc-console-ai remove", panel)                     # not installed: nothing to remove
        close = re.search(r'<a href="([^"]*)">close ✕</a>', panel)
        self.assertEqual(params(html.unescape(close.group(1)))["sel"], "")
        self.assertTrue(html.unescape(close.group(1)).endswith("#m-4"))
        again = [r for r in rs if r[1]][0][2]                                # the selected name link toggles it off
        self.assertEqual(params(again)["sel"], "")
        self.assertEqual(self.page(again.split("#")[0]).count("<aside"), 0)
        self.assertIn("nuc-console-ai remove qwen3-4b", plain(self.page("/?view=ai&sel=qwen3-4b&cols=100")))      # installed: remove (and it is the active one)
        self.assertNotIn("nuc-console-ai use qwen3-4b", plain(self.page("/?view=ai&sel=qwen3-4b")))
        self.assertIn("not pinned yet: this build cannot download it", plain(self.page("/?view=ai&sel=gpt-oss-20b")))   # no command for what cannot be downloaded
        self.assertNotIn("setup gpt-oss-20b", self.page("/?view=ai&sel=gpt-oss-20b"))
        self.assertEqual(self.builds, [None])                                # the catalog was asked for once

    def test_every_model_of_every_demo_opens(self):
        for os_name in (None, "windows", "darwin"):
            render.DEMO_OS = os_name
            render._AI.clear()
            self.srv.cache.clear()
            ids = [r["id"] for r in render.ai_rows(render.ai_data()["cat"])]
            self.assertEqual(len(ids), 12)
            for mid in ids:
                with self.subTest(os=os_name, model=mid):
                    body = self.page("/?view=ai&sel=" + quote(mid, safe=""))
                    self.assertIn('<aside class="dp"', body)
                    self.assertEqual(len([r for r in rows_of(body) if r[1]]), 1)

    def test_windows_and_macos_demos(self):
        render.DEMO_OS = "windows"
        win = self.page("/?view=ai&sel=qwen3-8b")
        txt = plain(win)
        for word in ("Intel Core i7-10750H", "NVIDIA GeForce GTX 1650", "4.0 GB (free: ?)", "Intel(R) UHD Graphics · Vulkan", "unified memory: it shares the RAM",
                     "nvidia-smi not found", "· off  ([ai] enabled = no in config.ini)", "! not installed", "run the commands in an administrator prompt",
                     "nuc-console-ai setup qwen3-8b"):
            self.assertIn(word, txt)
        self.assertNotIn("sudo", txt)
        render.DEMO_OS = "darwin"
        render._AI.clear()
        self.srv.cache.clear()
        mac = plain(self.page("/?view=ai&sel=qwen3-8b"))
        for word in ("Apple M2 · 8 cores · NEON", "Apple M2 (10-core GPU) · Metal", "unified memory: it shares the RAM", "✖ not answering · no server on 127.0.0.1:8080",
                     "sudo nuc-console-ai remove qwen3-8b", "✓ installed · ● active: [ai] model"):
            self.assertIn(word, mac)
        self.assertNotIn("VRAM", mac)
        self.assertNotIn("administrator prompt", mac)

    def test_a_catalog_that_could_not_be_read_is_a_message(self):
        render._AI["hit"] = {"cat": None, "msg": "the model catalog could not be read: ImportError('x')", "err": True, "at": time.time(), "key": (True, None)}
        body = self.page("/?view=ai")
        self.assertIn("the model catalog could not be read: ImportError(&#x27;x&#x27;)", body)
        self.assertIn('<p class="hn r">', body)
        self.assertEqual(rows_of(body), [])
        self.assertNotIn("0 models", plain(body))
        self.assertIn('<meta http-equiv="refresh"', body)                    # it may be there at the next reload

    def test_an_empty_catalog_says_so(self):
        self.seed({"hw": demo.ai_catalog(None)["hw"], "models": []})
        body = self.page("/?view=ai")
        self.assertIn("the catalog lists no model", body)
        self.assertEqual(rows_of(body), [])

    # ---- parameters -------------------------------------------------------------------------------------------------------------------

    def test_invalid_parameters_are_dropped(self):
        q = lambda s: web.view_params(web.parse_qs(s))  # noqa: E731
        self.assertEqual(q("view=ai")["view"], "ai")
        self.assertEqual(q("view=ai&sel=qwen3-4b")["sel"], "qwen3-4b")       # any text: the page checks it against the catalog
        self.assertEqual(len(q("view=ai&sel=" + "a" * 5000)["sel"]), web.AI_SEL_MAX)
        self.assertEqual(web.AI_SEL_MAX, render.AI_ID_MAX + 1)               # one more than an id has: a longer text never equals one
        self.assertEqual(q("view=map&sel=qwen3-4b")["sel"], "")              # the map's sel stays ten hex digits
        self.assertEqual(q("view=cpu&sel=qwen3-4b")["sel"], "")
        self.assertEqual(q("sel=qwen3-4b")["sel"], "")
        self.assertEqual((q("view=AI")["view"], q("view=ai")["view"], q("view=aii")["view"]), ("", "ai", ""))
        self.assertNotIn("period", q("view=ai&period=30"))                   # not the health page's
        self.assertTrue(q("view=ai&pause=1")["pause"])
        for path in ("/?view=ai&sel=" + "a" * 5000, "/?view=ai&sel=%00&period=%C2%B2", "/?view=ai&sel=<script>&zoom=%C2%B2&refresh=-1", "/?view=ai&all=1&open=x&shut=y&only=1"):
            body = self.page(path)
            self.assertNotIn("<script", body.lower())
            self.assertEqual(body.count("<aside"), 0, path)                  # no such model: no panel

    def test_a_selection_that_is_not_in_the_catalog_is_dropped(self):
        for sel in ("nope", "qwen3-4", "qwen3-4b ", "QWEN3-4B", "qwen3-4b\n", "qwen3-4b\x00", "../etc/passwd", "x" * 300, "<script>alert(1)</script>"):
            body = self.page("/?view=ai&sel=" + quote(sel))
            self.assertEqual(body.count("<aside"), 0, repr(sel))
            self.assertEqual([r for r in rows_of(body) if r[1]], [], repr(sel))
            self.assertNotIn("alert(1)", body)                               # and never repeated in a link
            self.assertNotIn("../etc", body)
            self.assertNotIn("x" * 50, body)
        self.assertEqual(self.page("/?view=ai&sel=qwen3-4b").count("<aside"), 1)

    def test_pause_stops_the_reload(self):
        body = self.page("/?view=ai&pause=1")
        self.assertNotIn("http-equiv", body)
        self.assertIn("paused", body)
        self.assertEqual(params(link(body, "live"))["pause"], False)
        self.assertNotIn("pause=", link(body, "live"))

    # ---- the catalog cache ------------------------------------------------------------------------------------------------------------

    def test_the_catalog_is_read_once_per_ttl_whatever_the_requests(self):
        for i in range(30):
            self.page("/?view=ai")
            self.page("/?view=ai&sel=%s" % ("qwen3-4b", "phi-4", "nope")[i % 3])
            self.page("/?view=ai&zoom=%d&refresh=%d&cols=%d" % (50 + 25 * (i % 3), 1 + i % 10, 100 + 20 * (i % 4)))
            self.srv.cache.clear()                                           # not even the page cache helps: only the catalog's own
        self.assertEqual(self.builds, [None])
        render._AI["hit"]["at"] -= render.AI_TTL + 1                         # a few seconds later
        self.page("/?view=ai")
        self.assertEqual(self.builds, [None, None])
        self.page("/")                                                       # the dashboard does not ask for it
        self.assertEqual(self.builds, [None, None])

    def test_concurrent_requests_share_one_computation(self):
        slow = []
        inner = render.ai_build

        def slow_build(now):
            slow.append(1)
            time.sleep(0.2)
            return inner(now)
        render.ai_build = slow_build
        out = []
        threads = [threading.Thread(target=lambda: out.append(get(self.srv, "/?view=ai&cols=%d" % (100 + 20 * i))[0])) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(out, [200] * 6)
        self.assertEqual(len(slow), 1)

    def test_the_page_cache_holds_only_values_that_exist(self):
        for i in range(web.CACHE_MAX * 3):
            self.page("/?view=ai&sel=junk%d&pause=%d" % (i, i % 2))
        keys = [k for k in self.srv.cache if k[0] == "ai"]
        self.assertLessEqual(len(self.srv.cache), web.CACHE_MAX)
        self.assertLessEqual(len(keys), 2)                                   # paused or not: a junk selection is the empty one
        self.assertNotIn("junk", "".join(str(k) for k in self.srv.cache))
        first = self.srv.page(**web.view_params(web.parse_qs("view=ai")))
        self.assertIs(self.srv.page(**web.view_params(web.parse_qs("view=ai"))), first)    # within r/2: the same render

    # ---- hostile data -----------------------------------------------------------------------------------------------------------------

    def hostile(self):
        e = EVIL
        cat = demo.ai_catalog(None)
        ms = cat["models"]
        ms[0].update(id="x<y>&z" + e, name="Evil " + e, notes=e, license=e, quant=e)
        ms[0]["assess"].update(why="why " + e, where=e)
        ms[1]["commands"] = {"install": 'sudo evil "quote" <b>' + e, "use": e, "remove": e}
        ms[1]["installed"], ms[1]["pinned"] = False, True
        ms[2]["assess"]["verdict"] = e
        ms.insert(3, "not a dict")
        ms.insert(4, {"id": e + "2", "name": None, "assess": 5, "commands": "x"})
        cat["hw"] = {"os": e, "arch": e, "cpu": {"model": e}, "ram": {"total_mb": 1000, "available_mb": 100}, "gpus": [{"name": e, "vram_mb": 100, "backend": e}], "notes": [e]}
        cat["runtime"], cat["dir"], cat["active"] = {"installed": True, "version": e}, e, ms[0]["id"]
        return cat, {"enabled": True, "endpoint": e, "model": e, "probe": {"state": "down", "msg": e, "models": [e]}}

    def test_html_and_control_characters_in_the_catalog_are_inert(self):
        cat, status = self.hostile()
        self.seed(cat, status)
        evil_id = render.ai_rows(cat)[0]["id"]
        for path in ("/?view=ai", "/?view=ai&cols=100", "/?view=ai&cols=200", "/?view=ai&sel=" + quote(evil_id, safe=""), "/?view=ai&sel=qwen3-30b-a3b"):
            body = self.page(path)
            self.assertEqual(re.findall(r"<script|<img|<iframe|<svg|<form|<object|<embed|<b>", body, re.I), [], path)
            self.assertIsNone(re.search(r"<[^>]*\son[a-z]+=", body), path)                 # no event handler inside any tag
            self.assertNotIn(ESC, body)
            self.assertNotIn(BEL, body)
            self.assertNotIn(CSI8, body)
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)     # shown, as text
        body = self.page("/?view=ai&sel=" + quote(evil_id, safe=""))
        self.assertIn("?[2J?]0;pwn?", plain(body))                           # the escape sequence is visible text, not an escape
        self.assertEqual(body.count("<aside"), 1)
        self.assertEqual(len([r for r in rows_of(body) if r[1]]), 1)
        for href in re.findall(r'href="([^"]*)"', body):                     # every link is one of ours, the id percent-encoded
            self.assertTrue(href.startswith("/?") or href.startswith("#"), href)
            self.assertNotIn("<", html.unescape(href))
            self.assertNotIn('"', html.unescape(href))
        cmd = self.page("/?view=ai&sel=qwen3-30b-a3b")                       # a hostile command is text in a code element, quotes and all
        self.assertIn('sudo evil &quot;quote&quot; &lt;b&gt;', cmd)

    def test_a_hostile_message_is_escaped_too(self):
        render._AI["hit"] = {"cat": None, "msg": "the catalog " + EVIL + " could not be read", "err": True, "at": time.time(), "key": (True, None)}
        body = self.page("/?view=ai")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn(ESC, body)
        self.assertIn("&lt;script&gt;", body)

    def test_a_failing_state_read_is_an_error_line_not_a_crash(self):
        real = render.ai_state

        def broken(smp):
            raise ValueError("state unreadable " + ESC + "[2J")
        render.ai_state = broken
        try:
            st, _, body = get(self.srv, "/?view=ai&sel=qwen3-4b")
        finally:
            render.ai_state = real
        self.assertEqual(st, 200)
        self.assertIn("render error (see the service log)", body)
        self.assertNotIn("state unreadable", body)                           # the detail goes to the log, not to the page
        self.assertNotIn(ESC, body)

    def test_a_catalog_of_the_wrong_shape_is_a_page_not_a_crash(self):
        for bad in ({}, {"hw": None, "models": None}, {"hw": {"gpus": "no", "notes": "no", "cpu": 3, "ram": []}, "runtime": 5},
                    {"models": [None, 3, {"id": "a", "assess": {"tok_s": [1]}, "commands": 5}]}):
            self.seed(bad)
            self.srv.cache.clear()
            st, _, body = get(self.srv, "/?view=ai")
            self.assertEqual(st, 200)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("render error", body)

    # ---- the advisor on the HEALTH page -----------------------------------------------------------------------------------------------

    RESULT = {"text": "1. restart shop-worker-1 with a memory limit [oom:shop-worker-1]\n2. look at <b>/data</b>", "model": "qwen3-4b", "at": 1,
              "cites": ["oom:shop-worker-1"]}

    def advisor_on(self, result):
        render.CFG["ai"].update(enabled=True, endpoint="http://127.0.0.1:11434/v1")
        calls = []

        def try_advise(report, cfg, cached_only=False):
            calls.append(cached_only)
            return result
        p1, p2 = mock.patch.object(advisor, "try_advise", try_advise), mock.patch.object(advisor, "available", lambda cfg: (True, ""))
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        render._ADVICE.clear()
        render._HEALTH.clear()
        self.srv.cache.clear()
        return calls

    def test_the_health_page_has_no_advice_while_the_advisor_is_off(self):
        self.assertEqual(web.health_extra_html(demo.health_report(None, 7)), "")
        self.assertNotIn("ADVICE", self.page("/?view=health"))

    def test_the_cached_advice_goes_under_the_findings_and_over_the_sections_and_is_escaped(self):
        calls = self.advisor_on(dict(self.RESULT))
        body = self.page("/?view=health")
        self.assertIn('<div class="advice"><p class="advice-head">ADVICE (AI, qwen3-4b) — check before acting</p>', body)
        self.assertIn("2. look at &lt;b&gt;/data&lt;/b&gt;", body)           # the model's words, as text
        self.assertNotIn("<b>/data</b>", body)
        self.assertLess(body.index("Slower boot"), body.index('class="advice"'))          # after the findings ...
        self.assertLess(body.index('class="advice"'), body.index("TOP CPU"))               # ... before the sections
        self.assertIn("[oom:shop-worker-1]", body)
        self.assertEqual(set(calls), {True})                                 # cached_only: a page never waits for a generation
        self.assertLessEqual(len(calls), 1)                                  # looked up once for this report
        self.assertIn(".advice{", body)                                      # and it has its style

    def test_nothing_cached_yet_the_page_says_how_to_ask(self):
        self.advisor_on(None)
        body = self.page("/?view=health")
        self.assertIn("ADVICE (AI) — none yet", body)
        self.assertIn("nuc-console-ask --advise asks the local model", body)

    def test_a_hostile_answer_and_a_broken_advisor_on_the_health_page(self):
        self.advisor_on(dict(self.RESULT, text=EVIL, model=EVIL, cites=[EVIL]))
        body = self.page("/?view=health")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn(ESC, body)
        self.assertNotIn(BEL, body)
        with mock.patch.object(advisor, "try_advise", side_effect=RuntimeError("boom")):
            render._ADVICE.clear()
            render._HEALTH.clear()
            self.srv.cache.clear()
            body = self.page("/?view=health")
        self.assertNotIn("ADVICE", body)                                     # no advice, and the page is the page


if __name__ == "__main__":
    unittest.main()
