"""The first-party scripts of the new web shell (src/webjs.py): the shared static rules (tests/jsrules.py) applied to every one,
the rules themselves shown to bite, the fragment allowlists the refresh script enforces equal to the Python constants, the hashes
and assets stable and right, and the DOM contract in the module docstring complete.
It runs without a browser (node, when installed, only checks the syntax); the behaviour was checked in a real Chromium against a
fixture page that follows the DOM contract, under a CSP with the scripts' hashes, `require-trusted-types-for 'script'` and
`connect-src 'self'`: refresh (only changed cards replaced, focus, <details> and scroll kept, 304, backoff, stale and 401 banners,
hidden tab, pause, typing, refused fragments), keys, preferences, copy, and the layout editor (drag, keyboard, save, draft)."""
import base64
import hashlib
import importlib
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
import graphjs  # noqa: E402
import jsrules  # noqa: E402
import nuc_config  # noqa: E402
import prefs  # noqa: E402
import webjs  # noqa: E402

NAMES = ("refresh", "keys", "prefs", "builder")


def inject(src, line, times=1):
    """The script with a statement added just inside the IIFE (it keeps the script's shape, so only the rule at hand can object)."""
    end = src.rindex("\n})();")
    return src[:end] + "".join("\n  " + line for _ in range(times)) + src[end:]


def flagged(name, src, label):
    return any(label in p for p in jsrules.check(name, src))


class Rules(unittest.TestCase):
    def test_the_four_scripts_and_their_policies(self):
        self.assertEqual(tuple(webjs.SCRIPTS), NAMES)
        self.assertEqual(sorted(jsrules.POLICIES), sorted(NAMES + ("graph", "app")))  # app: src/appjs.py, tested in test_appjs.py
        for name, js in (("refresh", webjs.REFRESH_JS), ("keys", webjs.KEYS_JS), ("prefs", webjs.PREFS_JS), ("builder", webjs.BUILDER_JS)):
            self.assertIs(webjs.SCRIPTS[name], js)

    def test_every_script_obeys_every_rule(self):
        for name, js in webjs.SCRIPTS.items():
            for rule in jsrules.RULES:
                with self.subTest(script=name, rule=rule.__name__):
                    self.assertEqual(rule(js, jsrules.POLICIES[name]), [])
            self.assertEqual(jsrules.check(name, js), [], name)

    def test_the_rules_take_the_graph_script_too(self):
        self.assertEqual(jsrules.check("graph", graphjs.SCRIPT), [])

    def test_sizes(self):
        for name, js in webjs.SCRIPTS.items():
            self.assertLess(len(js.encode("ascii")), jsrules.POLICIES[name].max_bytes, name)
        # the four together stay small: what a page that needs all of them inlines
        self.assertLess(sum(len(js) for js in webjs.SCRIPTS.values()), 31 * 1024)

    def test_there_is_one_door_to_the_network(self):
        for name in ("refresh", "prefs", "builder"):
            js = webjs.SCRIPTS[name]
            self.assertEqual(js.count("fetch("), 1, name)
            self.assertIn(webjs._LOAD, js)
            self.assertTrue(jsrules.CANON_LOAD.search(js), name)
        self.assertNotIn("fetch", webjs.KEYS_JS)
        # what the door lets through: "/?..." only
        self.assertIn('!url.startsWith("/?")', webjs._LOAD)
        self.assertIn('credentials: "same-origin"', webjs._LOAD)
        self.assertIn('redirect: "error"', webjs._LOAD)

    def test_no_script_touches_the_cookie(self):
        for name, js in webjs.SCRIPTS.items():
            self.assertIsNone(re.search(r"\.cookie\b|cookieStore", js), name)  # the cookie is HttpOnly: nothing here reads or writes one


GLOBAL_SAMPLES = {
    "innerHTML": "el.innerHTML = x;", "outerHTML": "el.outerHTML = x;", "insertAdjacent": 'el.insertAdjacentHTML("beforeend", x);',
    "document.write": "document.write(x);", "createElement": 'document.createElement("div");',
    "createContextualFragment": "r.createContextualFragment(x);", "innerText": "el.innerText = x;", "srcdoc": "f.srcdoc = x;",
    "execCommand": 'document.execCommand("insertHTML", false, x);', "appendChild and old node calls": "el.appendChild(n);",
    "Element.remove()": "el.remove();", "eval": "eval(x);", "Function": "const f = new Function(x);",
    "timer with a string": 'setTimeout("x()", 1);', "import": 'import("x");', "importScripts": 'importScripts("x");',
    "Worker": "const w = new Worker(x);", "javascript:": 'const u = "javascript:void(0)";', "WebSocket": "const s = new WebSocket(x);",
    "EventSource": "const s = new EventSource(x);", "XMLHttpRequest": "const r = new XMLHttpRequest();", "sendBeacon": "n.sendBeacon(x);",
    "postMessage": "w.postMessage(x);", "BroadcastChannel": "const b = new BroadcastChannel(x);", "WebTransport": "const t = new WebTransport(x);",
    "RTCPeerConnection": "const t = new RTCPeerConnection();", "serviceWorker": "n.serviceWorker.register(x);", "caches": "caches.open(x);",
    "indexedDB": "indexedDB.open(x);", "cookieStore": "cookieStore.get(x);", "document.cookie": "const c = document.cookie;", "window.open": "window.open(x);",
    "window.name": "const n = window.name;", "document.domain": "const d = document.domain;", "opener": "const o = window.opener;",
    "<link": 'const l = "<link rel=x>";', "<img": 'const i = "<img src=x>";', "@import": 'const c = "@import x";',
    "Image(": "const i = new Image(1, 1);", "Audio(": "const a = new Audio(x);", "a URL": 'const u = "https://example.com/";',
    "a property that loads or sends (src, srcset, action, data, formAction, cookie, domain, name = ...)": "img.src = x;",
    "an event handler property (.onclick = ...)": "el.onclick = f;", "history": "history.pushState(1, 2);", "location = ...": "location = x;",
    ".href = ...": "a.href = x;", "className": "el.className = x;", "dataset": "el.dataset.x = 1;", "style.cssText": "el.style.cssText = x;",
    "setAttribute style (an inline style the CSP refuses)": "el.setAttribute(\"style\", x);",
    "styleSheets": "const s = document.styleSheets;", "getElementsBy": 'document.getElementsByTagName("a");',
    "globalThis": "globalThis.x = 1;", "bracket access on a global": 'window["x"] = 1;',
    "assignment to a global (window.x = ...)": "window.x = 1;", "assignment to this.x": "this.x = 1;", "var": "var x = 1;",
}

CAP_SAMPLES = {
    "fetch": 'fetch("/?x");', "DOMParser": "const p = new DOMParser();", "parseFromString": 'const d = p.parseFromString(x, "text/html");',
    "createPolicy": 'const t = tt.createPolicy("x", {});', "importNode": "const n = document.importNode(x, true);",
    "replaceWith": "a.replaceWith(b);", "before/after": "a.before(b);", "append": "a.append(b);", "textContent": 'a.textContent = "x";',
    "localStorage": 'const v = localStorage.getItem("a");', "sessionStorage": 'const v = sessionStorage.getItem("a");',
    "navigator": "const l = navigator.language;", "clipboard.writeText": "navigator.clipboard.writeText(x);",
    "trustedTypes": "const t = window.trustedTypes;",
    "createElement": 'const e = document.createElement("p");', "createElementNS": 'const e = document.createElementNS(ns, "svg");',
    "EventSource": 'const s = new EventSource("/api/v1/stream?view=cpu");', "history": 'history.pushState(null, "", "/app");',
}


class RulesBite(unittest.TestCase):
    """A rule that never fires is worse than none: every ban, every cap and every contract line is shown to catch its case."""

    def test_every_global_ban_has_a_sample_and_catches_it_in_every_script(self):
        self.assertEqual(sorted(GLOBAL_SAMPLES), sorted(label for label, _ in jsrules.GLOBAL_BANS))
        sources = dict(webjs.SCRIPTS, graph=graphjs.SCRIPT)
        for label, sample in GLOBAL_SAMPLES.items():
            for name, src in sources.items():
                with self.subTest(ban=label, script=name):
                    self.assertTrue(flagged(name, inject(src, sample), "banned: " + label), jsrules.check(name, inject(src, sample)))

    def test_every_cap_is_refused_where_it_is_not_allowed_and_counted_where_it_is(self):
        self.assertEqual(sorted(CAP_SAMPLES), sorted(label for label, _, _, _ in jsrules.CAPS))
        sources = dict(webjs.SCRIPTS, graph=graphjs.SCRIPT)
        for label, rx, most, who in jsrules.CAPS:
            for name, src in sources.items():
                with self.subTest(cap=label, script=name):
                    if name not in who:
                        self.assertTrue(flagged(name, inject(src, CAP_SAMPLES[label]), "not allowed in " + name), label)
                    elif (most.get(name, 0) if isinstance(most, dict) else most) < 99:
                        n = most.get(name, 0) if isinstance(most, dict) else most
                        self.assertTrue(flagged(name, inject(src, CAP_SAMPLES[label], times=n + 1), label), label)
        # and the table matches who really uses what
        for name, src in sources.items():
            for label, rx, most, who in jsrules.CAPS:
                if re.search(rx, src):
                    self.assertIn(name, who, (name, label))

    def test_fetch_must_be_the_canonical_helper(self):
        for name in ("refresh", "prefs", "builder"):
            src = webjs.SCRIPTS[name]
            self.assertTrue(flagged(name, src.replace('!url.startsWith("/?")', '!url.startsWith("/")'), "canonical"), name)
            self.assertTrue(flagged(name, src.replace('redirect: "error"', 'redirect: "follow"'), "canonical"), name)
            self.assertTrue(flagged(name, src.replace('credentials: "same-origin", ', ""), "canonical"), name)
            self.assertTrue(flagged(name, inject(src, 'fetch(x);'), "fetch"), name)

    def test_navigation_and_storage_rules(self):
        for name in NAMES:
            src = webjs.SCRIPTS[name]
            self.assertTrue(flagged(name, inject(src, "location.assign(x);"), "navigation"), name)
            self.assertTrue(flagged(name, inject(src, 'location.replace("/");'), "navigation"), name)
            self.assertTrue(flagged(name, inject(src, 'const q = localStorage.getItem("q");'), "storage"), name)
        # a stored value read outside try/catch
        src = webjs.PREFS_JS.replace("try { return localStorage.getItem(k); } catch (err) { return null; }", "return localStorage.getItem(k);")
        self.assertTrue(flagged("prefs", src, "outside try/catch"))
        # location.assign must take an href or data-done read from the page
        src = webjs.PREFS_JS.replace('const a = e.target.closest("a[data-set]"), href = a && a.getAttribute("href")', 'const a = e.target.closest("a[data-set]"), href = "/?x"')
        self.assertTrue(flagged("prefs", src, "not an href read from the page"))

    def test_the_contract_is_exact_literals(self):
        for name in NAMES:
            src = webjs.SCRIPTS[name]
            for label, sample in (("ids", 'document.getElementById("zzz");'), ("selectors", 'document.querySelector("zzz");'),
                                  ("attrs_read", 'a.getAttribute("data-zzz");'), ("attrs_write", 'a.setAttribute("onclick", "x");'),
                                  ("classes_set", 'a.classList.add("zzz");'), ("events", 'a.addEventListener("zzz", f);'),
                                  ("styles", 'a.style.color = "red";')):
                with self.subTest(script=name, field=label):
                    self.assertTrue(flagged(name, inject(src, sample), label + " not in the contract"), jsrules.check(name, inject(src, sample)))
            for sample, what in (("document.getElementById(x);", "an id"), ("document.querySelector(x);", "a selector"),
                                 ("a.addEventListener(x, f);", "an event name"), ("a.classList.add(x);", "a class name"),
                                 ("a.getAttribute(x);", "an attribute name"), ("a.setAttribute(x, 1);", "setAttribute with a computed name")):
                with self.subTest(script=name, sample=sample):
                    self.assertTrue(flagged(name, inject(src, sample), what), jsrules.check(name, inject(src, sample)))

    def test_an_allowance_nobody_uses_is_a_problem(self):
        for name in NAMES:
            pol = jsrules.POLICIES[name]
            src = webjs.SCRIPTS[name]
            for field, value in (("ids", "zzz"), ("selectors", "zzz"), ("attrs_read", "data-zzz"), ("events", "zzz")):
                with self.subTest(script=name, field=field):
                    saved = getattr(pol, field)
                    setattr(pol, field, saved | {value})
                    try:
                        self.assertTrue(flagged(name, src, "never used"))
                    finally:
                        setattr(pol, field, saved)

    def test_shape_rules(self):
        for name in NAMES:
            src = webjs.SCRIPTS[name]
            self.assertTrue(flagged(name, src.replace('"use strict";', "", 1), "strict IIFE"), name)
            self.assertTrue(flagged(name, src + "\nfoo();\n", "does not end"), name)
            self.assertTrue(flagged(name, src.replace("\n})();", "\n})();\nfoo();"), "does not end"), name)
            self.assertTrue(flagged(name, inject(src, 'const s = "é";'), "not ASCII"), name)
            self.assertTrue(flagged(name, inject(src, 'const s = "</script>";'), "cannot contain"), name)
            self.assertTrue(flagged(name, inject(src, "// " + "x" * 20000), "bytes"), name)
            self.assertTrue(flagged(name, inject(src, "// x\n  const a = 1;", times=400), "lines"), name)
            self.assertTrue(flagged(name, src.replace("\r", "") + "\r", "CR"), name)
            head, rest = src[:src.index("(function")], src[src.index("(function"):]
            self.assertTrue(flagged(name, rest, "starts without a comment"), name)
            for cls in jsrules.POLICIES[name].classes_set:
                self.assertTrue(flagged(name, re.sub(r"\b%s\b" % cls, "zzzz", head) + rest, "class %s is set but not documented" % cls), (name, cls))
            self.assertTrue(flagged(name, "\n".join(["const top = 1;", src]), "strict IIFE"), name)

    def test_a_script_cannot_hide_a_banned_word_in_a_comment(self):
        # the bans read the whole text: a comment that names an API is rewritten, so nothing can sit next to the code unseen
        self.assertTrue(flagged("keys", inject(webjs.KEYS_JS, "// innerHTML"), "banned: innerHTML"))


class Fragment(unittest.TestCase):
    """The allowlists REFRESH_JS enforces are the Python tuples, written into the script when the module is imported."""

    def js_lists(self):
        m = re.search(r'new Set\("([^"]*)"\.split\(" "\)\), FRAG_ATTRS = new Set\("([^"]*)"\.split\(" "\)\)', webjs.REFRESH_JS)
        self.assertIsNotNone(m)
        self.assertEqual(webjs.REFRESH_JS.count("new Set("), 2)
        return m.group(1).split(" "), m.group(2).split(" ")

    def test_js_equals_python(self):
        tags, attrs = self.js_lists()
        self.assertEqual(tags, list(webjs.FRAG_TAGS))
        self.assertEqual(attrs, list(webjs.FRAG_ATTRS))
        self.assertNotIn("@", webjs.REFRESH_JS)

    def test_the_tuples_are_clean(self):
        for tup in (webjs.FRAG_TAGS, webjs.FRAG_ATTRS):
            self.assertEqual(len(tup), len(set(tup)), "duplicates")
            for item in tup:
                self.assertRegex(item, r"^[A-Za-z][A-Za-z0-9-]*$")  # nothing that could break the space-joined list or the string around it
        for tag in webjs.FRAG_TAGS:
            self.assertEqual(tag, tag.lower(), tag)  # the parser reports tags in lower case
        for tag in ("script", "style", "link", "meta", "base", "iframe", "frame", "frameset", "object", "embed", "applet", "template",
                    "img", "image", "audio", "video", "source", "track", "picture", "canvas", "math", "foreignobject", "use", "noscript",
                    "html", "head", "body", "title_", "slot", "dialog", "portal", "svg_", "a_"):
            self.assertNotIn(tag, webjs.FRAG_TAGS, tag)
        for attr in webjs.FRAG_ATTRS:
            self.assertFalse(attr.lower().startswith("on"), attr)  # no event handlers
        for attr in ("style", "src", "srcset", "srcdoc", "formaction", "target", "rel", "ping", "download", "poster", "data", "xlink:href",
                     "http-equiv", "content", "background", "longdesc", "usemap", "is", "slot", "form", "list", "autofocus", "accesskey"):
            self.assertNotIn(attr, webjs.FRAG_ATTRS, attr)
        # what the contract needs of a fragment
        for tag in ("article", "section", "header", "div", "span", "a", "table", "details", "summary", "svg", "path", "form", "button"):
            self.assertIn(tag, webjs.FRAG_TAGS)
        for attr in ("class", "id", "href", "role", "data-card", "data-rev", "data-k", "data-key", "data-pause", "data-row", "data-state", "viewBox",
                     "open", "tabindex", "title", "aria-label"):
            self.assertIn(attr, webjs.FRAG_ATTRS)

    def test_value_rules_are_in_the_script(self):
        js = webjs.REFRESH_JS
        for needle in ('/^(on|style$)/.test(a.name)', 'a.name === "href" && !(v.startsWith("/?") || v.startsWith("#"))',
                       'a.name === "action" && !/^\\/[^\\/\\\\]/.test(v)', 'a.name === "method" && !/^(get|post)$/i.test(v)',
                       'a.name === "name" && !/^(input|select|textarea|button)$/.test(n.localName)',
                       "n instanceof HTMLElement || n instanceof SVGElement", "doc.head.childNodes.length",
                       'r.headers.get("X-Nuc-Fragment") !== "1"', "r.status === 401", "r.status !== 304", 'createPolicy("nuc-frag"',
                       'Math.pow(2, fails - 1)', "Math.min(60000", "location.reload()"):
            self.assertIn(needle, js)
        self.assertEqual(js.count('createPolicy("nuc-frag"'), 1)

    def test_card_codes(self):
        m = re.search(r'new Map\("([^"]*)"\.split\(" "\)\.map\(p => p\.split\(":"\)\)\)', webjs.BUILDER_JS)
        self.assertIsNotNone(m)
        self.assertEqual([tuple(p.split(":")) for p in m.group(1).split(" ")], list(webjs.CARD_CODES))
        codes = [c for _, c in webjs.CARD_CODES]
        self.assertEqual(len(codes), len(set(codes)))
        for _, c in webjs.CARD_CODES:
            self.assertRegex(c, r"^[a-z]{2}$")
        # the codes the cookie grammar was written with, and the cards nuc_config knows
        self.assertEqual(dict(webjs.CARD_CODES), {"attention": "at", "exposure": "ex", "webapps": "wa", "firewall": "fw", "system": "sy",
                                                   "containers": "ct", "databases": "db", "boot": "bo", "network_traffic": "nt",
                                                   "sessions": "se", "tailscale": "ts", "docker_disk": "dd", "disks": "di"})
        self.assertEqual([c for c, _ in webjs.CARD_CODES], list(nuc_config.SECTIONS))
        self.assertEqual(dict(webjs.CARD_CODES), prefs.CARD_CODES)  # the grammar of the server's cookie
        # a layout the editor writes is one the server reads back as it was
        layout = "l" + "_".join(c + str(1 + i % 4) + ("x" if i % 5 == 4 else "") for i, (_, c) in enumerate(webjs.CARD_CODES))
        got = prefs.parse_cookie("1." + layout)
        self.assertEqual(len(got["layout"]) + len(got["hidden"]), len(webjs.CARD_CODES))
        self.assertEqual(prefs.apply_set("1", layout), prefs.dump_cookie(got))


class Hashes(unittest.TestCase):
    def test_known_values(self):
        # sha256("abc") from FIPS 180-2, in base64
        self.assertEqual(webjs.sha256_b64("abc"), "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=")
        self.assertEqual(webjs.csp_source("abc"), "'sha256-ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0='")
        self.assertEqual(webjs.csp_source(""), "'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='")
        # UTF-8, not the platform's encoding
        self.assertEqual(webjs.csp_source("é"), "'sha256-%s'" % base64.b64encode(hashlib.sha256(b"\xc3\xa9").digest()).decode())

    def test_the_hash_of_every_script_is_the_hash_of_its_bytes(self):
        for name, js in webjs.SCRIPTS.items():
            want = "'sha256-%s'" % base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode("ascii")
            self.assertEqual(webjs.csp_source(js), want, name)
            self.assertRegex(want, r"^'sha256-[A-Za-z0-9+/]{43}='$")
        self.assertEqual(len({webjs.csp_source(js) for js in webjs.SCRIPTS.values()}), 4)

    def test_sensitive(self):
        js = webjs.REFRESH_JS
        self.assertNotEqual(webjs.csp_source(js), webjs.csp_source(js + " "))
        self.assertNotEqual(webjs.csp_source(js), webjs.csp_source(js.replace("use strict", "use  strict")))

    def test_assets(self):
        self.assertEqual(tuple(webjs.ASSETS), NAMES)
        seen = set()
        for name, (body, ctype, hexdigest) in webjs.ASSETS.items():
            self.assertIsInstance(body, bytes)
            self.assertEqual(body, webjs.SCRIPTS[name].encode("ascii"))
            self.assertEqual(hexdigest, hashlib.sha256(body).hexdigest())
            self.assertEqual(ctype, "text/javascript; charset=utf-8")
            self.assertRegex(hexdigest, r"^[0-9a-f]{64}$")
            # the URL carries 8 digits of the hash: a changed script is a new URL, an unchanged one keeps its old one
            self.assertEqual(webjs.asset_path(name), "/s/%s.%s.js" % (name, hexdigest[:8]))
            self.assertRegex(webjs.asset_path(name), r"^/s/[a-z]+\.[0-9a-f]{8}\.js$")
            seen.add(hexdigest[:8])
            # the CSP source of the inline script and the asset's hash are the same digest
            self.assertEqual(webjs.csp_source(webjs.SCRIPTS[name]), "'sha256-%s'" % base64.b64encode(bytes.fromhex(hexdigest)).decode())
        self.assertEqual(len(seen), 4)

    def test_stable_across_imports_and_builds(self):
        before = {n: (js, webjs.csp_source(js), webjs.ASSETS[n][2]) for n, js in webjs.SCRIPTS.items()}
        # building again from the templates gives the same text, byte for byte
        for name, template in (("refresh", webjs._REFRESH), ("keys", webjs._KEYS), ("prefs", webjs._PREFS), ("builder", webjs._BUILDER)):
            self.assertEqual(webjs._inject(template), webjs.SCRIPTS[name], name)
        reloaded = importlib.reload(webjs)
        try:
            for n, js in reloaded.SCRIPTS.items():
                self.assertEqual((js, reloaded.csp_source(js), reloaded.ASSETS[n][2]), before[n], n)
        finally:
            importlib.reload(webjs)

    def test_a_tuple_change_changes_the_hash(self):
        saved = webjs.FRAG_TAGS
        try:
            webjs.FRAG_TAGS = saved + ("q",)
            self.assertNotEqual(webjs._inject(webjs._REFRESH), webjs.REFRESH_JS)
            self.assertEqual(webjs._inject(webjs._KEYS), webjs.KEYS_JS)  # the others do not read the tuples
        finally:
            webjs.FRAG_TAGS = saved


class Shape(unittest.TestCase):
    def test_ascii_and_plain(self):
        for name, js in webjs.SCRIPTS.items():
            js.encode("ascii")
            self.assertNotIn("\r", js)
            self.assertNotIn("\t", js, name)
            self.assertTrue(js.endswith("})();\n"), name)
            self.assertNotIn("@LOAD@", js)
            self.assertNotRegex(js, r"@[A-Z_]+@")
            self.assertEqual(js.count("\n(function main() {\n  \"use strict\";\n"), 1, name)

    def test_each_script_waits_for_the_page(self):
        for name, js in webjs.SCRIPTS.items():
            self.assertIn('if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }', js, name)

    def test_the_header_says_what_it_is(self):
        for name, js in webjs.SCRIPTS.items():
            self.assertTrue(js.startswith("// nuc-console web view: "), name)
            self.assertIn("src/webjs.py", js.split("(function main", 1)[0], name)


class DomContract(unittest.TestCase):
    """The module docstring is the contract the server's author reads: everything the scripts look up is in it."""

    doc = webjs.__doc__

    def test_every_name_a_script_uses_is_documented(self):
        for name in NAMES:
            pol = jsrules.POLICIES[name]
            for attr in pol.attrs_read | pol.attrs_write:
                if attr.startswith("data-"):
                    self.assertIn(attr, self.doc, (name, attr))
            for sel in pol.selectors:
                for token in re.findall(r"data-[a-z-]+", sel):
                    self.assertIn(token, self.doc, (name, sel))
            for i in pol.ids:
                self.assertIn("#" + i, self.doc, (name, i))
            for cls in pol.classes_set | pol.classes_read:
                if re.fullmatch(r"s[1-4]", cls):
                    self.assertIn("s1..s4", self.doc)
                else:
                    self.assertIn(cls, self.doc, (name, cls))

    def test_every_data_attribute_in_a_script_is_documented(self):
        for name, js in webjs.SCRIPTS.items():
            for token in set(re.findall(r"\bdata-[a-z]+(?:-[a-z]+)*", js)):
                self.assertIn(token, self.doc, (name, token))

    def test_the_fragment_contract_is_documented(self):
        for needle in ("X-Nuc-Fragment", "ETag", "If-None-Match", "304", "401", "204", "Set-Cookie", "X-Nuc-Prefs", "&frag=1",
                       "FRAG_TAGS", "FRAG_ATTRS", "CARD_CODES", "data-rev", "__top", "__kpis", "localStorage", "nuc-ui"):
            self.assertIn(needle, self.doc, needle)

    def test_the_names_are_in_the_scripts_and_the_docs_say_the_same(self):
        self.assertIn('"nuc-ui"', webjs.PREFS_JS)
        self.assertIn('"nuc-ui-sync"', webjs.PREFS_JS)
        self.assertNotIn("localStorage", webjs.BUILDER_JS)  # the editor keeps nothing in the browser: the cookie is the layout
        self.assertIn("X-Nuc-Prefs", webjs.PREFS_JS)
        self.assertIn("X-Nuc-Fragment", webjs.REFRESH_JS)


@unittest.skipUnless(shutil.which("node"), "node is not installed (an optional syntax check)")
class Syntax(unittest.TestCase):
    def test_every_script_parses(self):
        with tempfile.TemporaryDirectory() as d:
            for name, js in webjs.SCRIPTS.items():
                p = os.path.join(d, name + ".js")
                with open(p, "w", newline="\n") as f:
                    f.write(js)
                r = subprocess.run([shutil.which("node"), "--check", p], capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, "%s: %s" % (name, r.stderr))


class BuilderRules(unittest.TestCase):
    """What the layout editor's script may and may not do, beyond the rules every script obeys (the browser check is in the work log)."""

    def test_it_obeys_every_rule(self):
        self.assertEqual(jsrules.check("builder", webjs.BUILDER_JS), [])

    def test_it_saves_with_the_one_door_and_keeps_nothing_in_the_browser(self):
        js = webjs.BUILDER_JS
        self.assertEqual(js.count('load("/?set=" + str + "&frag=1")'), 1)
        for banned in ("localStorage", "sessionStorage", "location", "document.cookie", "navigator"):
            self.assertNotIn(banned, js)
        self.assertEqual(jsrules.POLICIES["builder"].location, set())
        self.assertEqual(jsrules.POLICIES["builder"].storage, {})

    def test_it_moves_nodes_the_server_made_and_makes_none(self):
        js = webjs.BUILDER_JS
        for banned in ("createElement", "innerHTML", "cloneNode", "appendChild", "insertAdjacent", "importNode", "DOMParser"):
            self.assertNotIn(banned, js)
        self.assertEqual(len(re.findall(r"\.(?:before|after)\(", js)), 4)  # one place earlier or later, and the drag
        self.assertEqual(len(re.findall(r"\.append\(", js)), 2)

    def test_a_rule_breaker_is_caught(self):
        for line, label in (('document.body.innerHTML = "x";', "innerHTML"), ('localStorage.setItem("a", "b");', "localStorage"),
                            ('document.createElement("a");', "createElement"), ('location.assign("/?x");', "location"),
                            ('fetch("/?x");', "fetch"), ('eval("1");', "eval"), ('const q = document.querySelector("#other");', "selectors"),
                            ('document.cookie;', "document.cookie")):
            self.assertTrue(flagged("builder", inject(webjs.BUILDER_JS, line), label), line)

    def test_the_layout_it_writes_is_the_one_the_server_reads(self):
        js = webjs.BUILDER_JS
        self.assertIn('"l" + cards().filter(code).map(c => code(c) + width(c) + (c.classList.contains("off") ? "x" : "")).join("_")', js)
        self.assertIn("/^l[a-z]{2}[1-4]x?(_[a-z]{2}[1-4]x?)*$/", js)  # what it takes back after a failed save: the same grammar
        # the whole of the editor's output is a field the server accepts, whatever the order or the widths
        rnd = random.Random(3)
        for _ in range(50):
            ids = list(prefs.CARDS)
            rnd.shuffle(ids)
            field = "l" + "_".join(prefs.CARD_CODES[c] + str(rnd.randint(1, 4)) + ("x" if rnd.random() < 0.3 else "") for c in ids)
            self.assertEqual(prefs.apply_set("1", field), prefs.dump_cookie(prefs.parse_cookie("1." + field)))
            self.assertLessEqual(len(prefs.apply_set("1.tl.dw.vo.pv.os.kpb_in_la_be_dl_fw_cp_rm", field)), prefs.COOKIE_MAX)


if __name__ == "__main__":
    unittest.main()
