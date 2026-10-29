"""The Execute control on the page (docs/WEB.md#the-execute-button): its markup on the script-free page (htmlview.py) and in the live app (appjs.py),
the status line, what the server hands each of them, and that a remote host's view draws none of it."""
import json
import socket
import os
import re
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import unittest
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import appjs  # noqa: E402
import cards  # noqa: E402
import htmlview  # noqa: E402
import nuc_config  # noqa: E402
import remotedoc  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import web  # noqa: E402
import webapi  # noqa: E402

RUN = {"label": "Restart nuc-console-collector", "id": "stale-net"}
INFO = {"run": lambda pid: RUN if pid == "stale-net" else None, "csrf": "tok&<1>", "back": "view=cpu&sort=mem", "note": None}
PROB = ui.Problem("warn", "data is old", id="stale-net", title="t", why="w", fix="sudo systemctl restart nuc-console-collector")
OTHER = ui.Problem("warn", "port opened", id="db-open-lan", fix="close it")


class Tags(HTMLParser):
    def __init__(self, text):
        HTMLParser.__init__(self)
        self.found = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))

    def all(self, tag):
        return [a for t, a in self.found if t == tag]


class Markup(unittest.TestCase):
    def test_a_problem_with_run_gets_a_disclosure_and_a_form(self):
        with htmlview.executing(INFO):
            out = htmlview.html(PROB)
        tags = Tags(out)
        self.assertEqual([a.get("class") for a in tags.all("details")], ["fix", "exec"])
        form, = tags.all("form")
        self.assertEqual((form["method"], form["action"]), ("post", "/fix/run"))
        fields = {a["name"]: a["value"] for a in tags.all("input")}
        self.assertEqual(fields, {"id": "stale-net", "csrf": "tok&<1>", "back": "view=cpu&sort=mem"})
        self.assertIn("<summary>Execute</summary>", out)
        self.assertIn("Confirm: Restart nuc-console-collector</button>", out)
        self.assertIn("as the web account, allowed by a polkit rule", out)
        self.assertNotIn("tok&<1>", out)  # escaped in the attribute
        self.assertEqual([a["type"] for a in tags.all("button")], ["submit"])

    def test_nothing_is_drawn_without_run(self):
        with htmlview.executing(INFO):
            self.assertNotIn("exec", htmlview.html(OTHER))
        for info in (None, dict(INFO, run=lambda pid: None)):
            with htmlview.executing(info):
                out = htmlview.html(PROB)
            self.assertNotIn("/fix/run", out)
            self.assertNotIn("Execute", out)
        self.assertNotIn("/fix/run", htmlview.html(PROB))  # outside any context too
        self.assertNotIn("exec", htmlview.html(ui.Problem("warn", "x")))

    def test_the_context_is_restored(self):
        with htmlview.executing(INFO):
            with htmlview.executing(None):
                self.assertNotIn("/fix/run", htmlview.html(PROB))
            self.assertIn("/fix/run", htmlview.html(PROB))
        self.assertNotIn("/fix/run", htmlview.html(PROB))

    def test_the_status_line(self):
        self.assertEqual(htmlview.exec_note(), "")
        with htmlview.executing(INFO):
            self.assertEqual(htmlview.exec_note(), "")
        for ok, sym, lv in ((True, "✔", "lv-ok"), (False, "✖", "lv-err")):
            with htmlview.executing(dict(INFO, note={"ok": ok, "text": "done <b>"})):
                out = htmlview.exec_note()
            tag, a = Tags(out).found[0]
            self.assertEqual((tag, a["role"], a["aria-live"]), ("p", "status", "polite"))
            self.assertIn(lv, a["class"].split())
            self.assertIn('<span class="sym">%s</span> done &lt;b&gt;</p>' % sym, out)  # the symbol AND the words, escaped

    def test_no_inline_handler_or_style(self):
        with htmlview.executing(dict(INFO, note={"ok": True, "text": "x"})):
            out = htmlview.exec_note() + htmlview.html(PROB)
        self.assertIsNone(re.search(r"\son\w+=|style=|<script", out))


class Script(unittest.TestCase):
    """appjs.py draws the same markup (the parity of the two is checked in a real browser by tools/browser_check.py); here, its source."""

    def test_the_script_draws_the_same_parts(self):
        js = appjs.APP_JS
        for part in ('"/fix/run"', 'class: "exec"', 'el("summary", {}, "Execute")', 'method: "post"', 'hid("id", r.id)', 'hid("csrf", ex.csrf)',
                     'hid("back", ex.back)', 'hid("to", "app")', '"Confirm: " + tx(r.label)', 'type: "submit"', "This runs \\u201c",
                     "as the web account, allowed by a polkit rule.", '"aria-live": "polite"', 'role: "status"', 'cls("msg", "notice", "fixnote", "lv-" + lv)'):
            self.assertIn(part, js)
        self.assertIsNone(re.search(r"\bon[a-z]+\s*[:=]\s*[\"']|\bstyle\b\s*:", js.split("function runner")[1].split("function accepted")[0]))

    def test_the_text_of_the_two_drawings_is_the_same(self):
        with htmlview.executing(INFO):
            out = htmlview.html(PROB)
        for text in ("This runs “Restart nuc-console-collector” on this machine, as the web account, allowed by a polkit rule.",
                     "Confirm: Restart nuc-console-collector", "Execute"):
            self.assertIn(text, out)
        self.assertIn(json.loads('"\\u201c"'), "“")

    def test_the_run_comes_only_from_the_overview_in_the_attention_card(self):
        js = appjs.APP_JS
        self.assertIn('c.id === "attention" ? exAll : null', js)
        self.assertIn("cfg.exec && typeof cfg.exec.csrf", js)


class Server(unittest.TestCase):
    def setUp(self):
        saved = (render.CFG["expose"], render.CFG["webapps"], socket.gethostname, render.DEMO)  # a demo server changes them for good
        self.addCleanup(lambda: (render.CFG.__setitem__("expose", saved[0]), render.CFG.__setitem__("webapps", saved[1]),
                                 setattr(socket, "gethostname", saved[2]), setattr(render, "DEMO", saved[3])))
        self.srv = web.Server(("127.0.0.1", 0), dict(nuc_config.load()["web"], refresh_seconds=2), "v" * 24, demo=True)
        self.addCleanup(self.srv.server_close)
        web_cfg = dict(render.CFG["web"])
        self.addCleanup(lambda: render.CFG.__setitem__("web", web_cfg))
        mock.patch.object(nuc_config, "LINUX", True).start()
        self.addCleanup(mock.patch.stopall)

    def on(self, yes=True, token=True):
        """[web] fix_actions, with a token_file: the feature needs both (a key on without a token file draws nothing)."""
        render.CFG["web"] = dict(render.CFG["web"], fix_actions=yes, token_file="/etc/nuc-console/web.token" if token else "")

    def test_exec_info_only_when_the_feature_is_on(self):
        self.on(False)
        self.assertIsNone(self.srv.exec_info({}))
        self.on(True, token=False)
        self.assertIsNone(self.srv.exec_info({}), "the key on without a token file: nothing is drawn")
        self.on()
        info = self.srv.exec_info({"view": "cpu"})
        self.assertEqual((info["csrf"], info["back"], info["note"]), (self.srv.csrf, "view=cpu", None))
        self.assertEqual(info["run"]("stale-net")["id"], "stale-net")
        self.assertIsNone(info["run"]("db-open-lan"))

    def card(self):
        body = [PROB, OTHER]
        with mock.patch.object(cards, "build", return_value=ui.Card("attention", "ATTENTION", "", "warn", body)):
            return self.srv.shell_card(None, "attention", 1, 2, {}, False)[1]

    def test_the_attention_card_of_the_page_has_one_execute_form_and_the_note(self):
        self.on()
        self.srv.fix_note = {"ok": False, "text": "not permitted", "at": time.time()}
        out = self.card()
        self.assertEqual(out.count('action="/fix/run"'), 1)
        self.assertEqual(out.count('class="exec"'), 1)
        self.assertIn(self.srv.csrf, out)
        self.assertLess(out.index("fixnote"), out.index("data-problem"))  # the status line is at the top of the list
        self.assertIn("not permitted", out)
        self.on(False)
        out = self.card()
        for word in ("/fix/run", "exec", "fixnote", "Execute"):
            self.assertNotIn(word, out)

    def test_other_cards_get_none(self):
        self.on()
        with mock.patch.object(cards, "build", return_value=ui.Card("exposure", "EXPOSURE", "", "warn", [PROB])):
            out = self.srv.shell_card(None, "exposure", 1, 2, {}, False)[1]
        self.assertNotIn("/fix/run", out)

    def test_the_token_is_in_the_page_config_never_in_a_data_document(self):
        self.on()
        page = self.srv.app_page("", {})
        conf = json.loads(re.search(r'id="cfg">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(conf["exec"], {"csrf": self.srv.csrf})
        self.assertNotIn(b'"csrf"', bytes(self.srv.api("overview", {})))
        self.on(False)
        page = self.srv.app_page("", {})
        self.assertNotIn("exec", json.loads(re.search(r'id="cfg">(.*?)</script>', page, re.S).group(1)))


class Remote(unittest.TestCase):
    def test_a_remote_view_draws_no_execute_even_if_the_document_carries_it(self):
        card = ui.Card("attention", "ATTENTION", "", "warn", [PROB])
        body = {"host": "far", "status": {"state": "warn", "text": "x"}, "stale": False, "kpis": [], "cards": [card], "layout": [{"id": "attention", "w": 1}]}
        doc = json.loads(bytes(webapi.document("overview", body, 1.0, console=True)))
        doc["cards"][0]["body"][0]["run"] = RUN
        doc["problems"] = {"items": [{"id": "stale-net", "run": RUN}], "fix_note": {"ok": True, "text": "done"}}
        d = remotedoc.decode(doc)
        self.assertTrue(d.ok)
        self.assertFalse(hasattr(d, "problems"))
        node = d.cards[0].body[0]
        self.assertIsInstance(node, ui.Problem)
        self.assertFalse(hasattr(node, "run"))
        out = "".join(htmlview.html(c) for c in d.cards)
        self.assertIn("stale-net", out)
        for word in ("/fix/run", "exec", "fixnote", "Execute"):
            self.assertNotIn(word, out)


if __name__ == "__main__":
    unittest.main()
