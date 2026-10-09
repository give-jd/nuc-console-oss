"""Static checks of the graph view's one script (src/graphjs.py SCRIPT): it is inlined into a page under a CSP that
allows only its SHA-256, so what matters here is that it can do nothing beyond what the module promises (no markup built
from data, no network, nothing loaded, no navigation but the reload and a node's own link), that it cannot break out of its
<script> element, that it touches only the DOM contract's ids, classes and attributes, and that the hash is stable.
The rules themselves are shared with the web shell's scripts (tests/jsrules.py, applied to those in tests/test_webjs.py); what
is left here is what only this script has: its own contract (jsrules.POLICIES["graph"]) and its geometry helpers.
It runs without a browser; the behaviour (drag, zoom, pan, refresh, storage) was checked in a real one."""
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graphjs  # noqa: E402
import jsrules  # noqa: E402

S = graphjs.SCRIPT
P = jsrules.POLICIES["graph"]

# the DOM contract (web.py's graph page), and what the script may add to it: jsrules.POLICIES["graph"]
IDS = P.ids
READ_SELECTORS = P.selectors
READ_ATTRS = P.attrs_read
WRITE_ATTRS = P.attrs_write
CLASSES = P.classes_set  # set by the script: documented at its top for the server's CSS
STYLES = P.styles
strings = jsrules.strings


def problems(*rules):
    """What the shared rules find in the script, from the rule groups named (empty: it obeys them)."""
    return [p for rule in rules for p in rule(S, P)]


class Shared(unittest.TestCase):
    def test_obeys_every_shared_rule(self):
        self.assertEqual(jsrules.check("graph", S), [])


class Forbidden(unittest.TestCase):
    def test_no_markup_from_data(self):
        self.assertEqual(problems(jsrules.rule_global, jsrules.rule_caps), [])
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "insertAdjacentElement", "document.write", "createElement",
                    "createElementNS", "createContextualFragment", "DOMParser", "appendChild", "insertBefore", "cloneNode",
                    "textContent", "innerText", "srcdoc", "importNode"):
            self.assertNotIn(api, S, api)

    def test_no_code_from_strings(self):
        self.assertEqual(problems(jsrules.rule_global), [])
        self.assertIsNone(re.search(r"\beval\b", S))
        self.assertIsNone(re.search(r"\bFunction\b", S))  # new Function(...), Function(...), the constructor's other doors
        self.assertIsNone(re.search(r"\b(setTimeout|setInterval|requestAnimationFrame)\(\s*[\"'`]", S))
        self.assertIsNone(re.search(r"\bimport\b", S))  # import(...) and import statements
        self.assertNotIn("importScripts", S)
        self.assertNotIn("Worker", S)
        self.assertNotIn("javascript:", S.lower())

    def test_no_network_and_nothing_loaded(self):
        self.assertEqual(problems(jsrules.rule_global, jsrules.rule_caps, jsrules.rule_network), [])
        for api in ("fetch", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "navigator", "Image(", "Audio(",
                    "postMessage", "BroadcastChannel", "SharedWorker", "serviceWorker", "caches", "indexedDB", "localStorage",
                    "document.cookie", "window.open", "window.name", "document.domain", "opener", "<link", "<img", "@import"):
            self.assertNotIn(api, S, api)
        self.assertIsNone(re.search(r"https?:|ftp:|wss?:|//\s*[a-z0-9.-]+\.(com|org|net|io)\b", S, re.I))
        self.assertIsNone(re.search(r"\.(src|srcset|action|data)\s*=[^=]", S))

    def test_navigation_is_only_reload_and_a_node_link(self):
        self.assertEqual(problems(jsrules.rule_navigation), [])
        self.assertEqual(strings(r"\blocation\.(\w+)", S), {"reload", "assign"})
        self.assertEqual(S.count("location.assign("), 1)
        self.assertIn("location.assign(href)", S)  # href: the node's own link, read from the page
        self.assertIsNone(re.search(r"\bhistory\b", S))
        self.assertIsNone(re.search(r"\blocation\s*=[^=]", S))
        self.assertIsNone(re.search(r"\.href\s*=[^=]", S))

    def test_storage_only_in_try(self):
        self.assertEqual(problems(jsrules.rule_storage), [])
        lines = [ln for ln in S.splitlines() if "sessionStorage" in ln and not ln.lstrip().startswith("//")]
        self.assertEqual(len(lines), 2)  # save and restore, nothing else
        for ln in lines:
            self.assertIn("try {", ln)
        self.assertIn("catch", S)
        self.assertEqual(strings(r"sessionStorage\.(\w+)", S), {"setItem", "getItem"})


class Shape(unittest.TestCase):
    def test_cannot_end_its_script_element(self):
        low = S.lower()
        for bad in ("</script", "<script", "<!--", "-->", "]]>"):
            self.assertNotIn(bad, low, bad)
        self.assertNotIn("\x00", S)

    def test_ascii_and_size(self):
        S.encode("ascii")
        self.assertNotIn("\r", S)
        self.assertLess(len(S.encode("utf-8")), 16 * 1024)
        self.assertLessEqual(len(S.splitlines()), 300)
        self.assertEqual([p for p in jsrules.rule_shape(S, P) if "bytes" in p or "lines" in p or "ASCII" in p], [])

    def test_strict_iife_no_globals(self):
        self.assertEqual(jsrules.rule_shape(S, P), [])
        code = "\n".join(ln for ln in S.splitlines() if not ln.startswith("//"))
        self.assertTrue(code.startswith("(function main() {\n  \"use strict\";\n"), code[:60])
        self.assertTrue(code.rstrip().endswith("})();"))
        self.assertEqual(code.count("\n(function"), 0)
        # nothing at the top level but the IIFE: every other line is indented or closes it
        for ln in code.splitlines()[1:-1]:
            self.assertTrue(ln.startswith(" ") or not ln.strip(), ln)
        for ln in code.splitlines():
            self.assertIsNone(re.match(r"\s*(var|window\.\w+\s*=|this\.\w+\s*=)", ln), ln)  # let/const only
        self.assertEqual(strings(r"\bvar\b", S), set())

    def test_documented_at_the_top(self):
        head = S[:S.index("(function")]
        self.assertTrue(head.startswith("//"))
        for cls in CLASSES:
            self.assertIsNotNone(re.search(r"^//.*[ #.]%s\b" % cls, head, re.M), cls)


class Contract(unittest.TestCase):
    def test_shared_contract_rule(self):
        self.assertEqual(problems(jsrules.rule_contract), [])

    def test_ids(self):
        self.assertEqual(strings(r"getElementById\(\"([^\"]*)\"\)", S), IDS)
        self.assertNotIn("getElementsBy", S)

    def test_selectors(self):
        used = strings(r"(?:querySelectorAll|querySelector|closest|matches)\(\"([^\"]*)\"\)", S)
        self.assertTrue(used)
        self.assertLessEqual(used, READ_SELECTORS)
        self.assertIsNone(re.search(r"(querySelectorAll|querySelector|closest)\([^\"]", S))  # only literal selectors

    def test_attributes(self):
        read = strings(r"(?:getAttribute|hasAttribute)\(\"([^\"]*)\"\)", S)
        self.assertLessEqual(read, READ_ATTRS)
        for must in ("data-k", "data-a", "data-b", "data-state", "data-refresh", "data-paused", "marker-end"):
            self.assertIn(must, read)
        # num(el, "cx"), put(el, "x1", v): the helpers' calls name the geometry attributes
        geo = strings(r"\b(?:num|put)\([^,()]+(?:\([^)]*\))?,\s*\"([^\"]*)\"", S)
        self.assertLessEqual(geo, {"cx", "cy", "r", "x", "y", "x1", "y1", "x2", "y2"})
        wrote = strings(r"setAttribute\(\"([^\"]*)\"", S) | {a for a in geo if a != "r"}
        self.assertLessEqual(wrote, WRITE_ATTRS)
        self.assertIn("transform", wrote)
        dyn = [ln for ln in S.splitlines() if re.search(r"setAttribute\([^\"]", ln)]  # only the geometry helper takes a name
        self.assertEqual(len(dyn), 1, dyn)
        self.assertIn("const put = (el, name, v) => el.setAttribute(name, v.toFixed(1));", dyn[0])
        self.assertEqual([ln.strip() for ln in dyn], list(P.dynamic_setattr))
        self.assertNotIn("removeAttribute", S)
        self.assertNotIn("dataset", S)

    def test_classes(self):
        used = strings(r"classList\.(?:toggle|add|remove|contains)\(\"([^\"]*)\"", S)
        self.assertTrue(used)
        self.assertLessEqual(used, CLASSES)
        self.assertEqual(used, CLASSES)  # all of them are really used, so the header's list is the whole list
        self.assertIsNone(re.search(r"classList\.(?:toggle|add|remove|contains)\([^\"]", S))
        self.assertNotIn("className", S)

    def test_styles_and_nothing_else(self):
        self.assertEqual(strings(r"\.style\.(\w+)", S), STYLES)
        self.assertNotIn("style.cssText", S)
        self.assertNotIn("styleSheets", S)

    def test_reads_the_pages_own_state_only(self):
        self.assertIn('"nuc-graph:" + (gv.getAttribute("data-state") || "")', S)
        self.assertIn("pagehide", S)
        self.assertIn("document.hidden", S)


class Hash(unittest.TestCase):
    def test_known_value(self):
        # sha256("abc") from FIPS 180-2, in base64
        self.assertEqual(graphjs.csp_source("abc"), "'sha256-ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0='")
        self.assertEqual(graphjs.csp_source(""), "'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='")

    def test_is_the_hash_of_the_utf8_bytes(self):
        want = "'sha256-%s'" % base64.b64encode(hashlib.sha256(S.encode("utf-8")).digest()).decode("ascii")
        self.assertEqual(graphjs.csp_source(), want)
        self.assertEqual(graphjs.csp_source(S), want)
        self.assertRegex(want, r"^'sha256-[A-Za-z0-9+/]{43}='$")
        # UTF-8, not the platform's encoding, and not the str's repr
        self.assertEqual(graphjs.csp_source("é"), "'sha256-%s'" % base64.b64encode(hashlib.sha256(b"\xc3\xa9").digest()).decode())

    def test_stable_and_sensitive(self):
        self.assertEqual(graphjs.csp_source(), graphjs.csp_source())
        self.assertNotEqual(graphjs.csp_source(S), graphjs.csp_source(S + " "))
        self.assertNotEqual(graphjs.csp_source(S), graphjs.csp_source(S.replace("use strict", "use  strict")))


@unittest.skipUnless(shutil.which("node"), "node is not installed (an optional syntax check)")
class Syntax(unittest.TestCase):
    def test_parses(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "graph.js")
            with open(p, "w", newline="\n") as f:
                f.write(S)
            r = subprocess.run([shutil.which("node"), "--check", p], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
