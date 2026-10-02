"""One set of static rules for every first-party script of the web view (src/graphjs.py, src/webjs.py).

The scripts are inlined into pages whose Content-Security-Policy lists their SHA-256 (or served from a hashed URL), so what
matters is what they can do once they run: build no markup from data, run no code from strings, open no connection but this
server's own "/?..." pages, load nothing, keep nothing but a few documented keys, navigate nowhere but the reload and a link
the server wrote, touch no global, and read and write only the ids, classes, attributes and selectors the DOM contract names
(the module docstring of src/webjs.py; the top of src/graphjs.py). This file is those rules as a checker, with no browser:

    jsrules.check("refresh", source)          -> a list of problems, empty when the script obeys every rule
    jsrules.POLICIES[name]                    -> what one script is allowed (names: graph, refresh, keys, prefs, builder)
    jsrules.rule_*(source, policy)            -> one rule group, for a test that names it

Three kinds of rule:
  * GLOBAL bans: no script may contain these, anywhere (comments included: a comment that names a banned API is rewritten);
  * CAPS: an API a few scripts need (fetch, DOMParser, localStorage...), and which ones may use it, how often and how;
  * the CONTRACT of each script: its literal ids, selectors, attributes, classes, events and styles, which must match its
    POLICY exactly (an allowance nobody uses is a hole nobody notices) and may only be string literals.

The behaviour was checked in a real browser (see tests/test_webjs.py for what is checked here, and why a rule exists).
Run `python3 tests/jsrules.py` to print the problems of every script.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))


def strings(pattern, text):
    """The set of the first group of every match of pattern in text."""
    return set(re.findall(pattern, text))


def code_lines(src):
    """The lines that are not whole-line comments."""
    return [ln for ln in src.splitlines() if not ln.lstrip().startswith("//")]


EVERYTHING = ("ids", "selectors", "attrs_read", "attrs_write", "classes_set", "classes_read", "events", "styles")


class Policy(object):
    """What one script may touch. Sets are the literal names the script uses; every one must be used (see exact)."""

    def __init__(self, name, max_bytes, max_lines, ids=(), selectors=(), attrs_read=(), attrs_write=(), classes_set=(),
                 classes_read=(), events=(), styles=(), location=(), assign_calls=0, storage=None, exact=EVERYTHING,
                 literal_attrs=True, dynamic_setattr=(), ban_extra=()):
        self.name = name
        self.max_bytes = max_bytes              # the script must be smaller than this
        self.max_lines = max_lines
        self.ids = set(ids)                     # getElementById("...")
        self.selectors = set(selectors)         # querySelector / querySelectorAll / closest / matches ("...")
        self.attrs_read = set(attrs_read)       # getAttribute / hasAttribute ("...")
        self.attrs_write = set(attrs_write)     # setAttribute / removeAttribute ("...")
        self.classes_set = set(classes_set)     # classList.add / remove / toggle ("..."): documented at the top of the script
        self.classes_read = set(classes_read)   # classList.contains ("...")
        self.events = set(events)               # addEventListener ("...")
        self.styles = set(styles)               # .style.<name>
        self.location = set(location)           # location.<name>
        self.assign_calls = assign_calls        # how many location.assign(...) calls (the argument is read from the DOM)
        self.storage = storage or {}            # {"localStorage": (lines mentioning it, {methods})}: each line inside try
        self.exact = set(exact)                 # which of the sets above must be equal to what the script uses (else: a subset)
        self.literal_attrs = literal_attrs      # attribute names are string literals (graph: a geometry helper takes a name)
        self.dynamic_setattr = tuple(dynamic_setattr)   # the only lines where setAttribute takes a name that is not a literal
        self.ban_extra = tuple(ban_extra)       # more regex bans for this script only


# ---- the global bans: (label, regex). Whole text, comments included. -----------------------------------------------------------
GLOBAL_BANS = [
    # markup from data
    ("innerHTML", r"innerHTML"), ("outerHTML", r"outerHTML"), ("insertAdjacent", r"insertAdjacent(?:HTML|Text|Element)"),
    ("document.write", r"document\s*\.\s*write"), ("createElement", r"createElement"), ("createContextualFragment", r"createContextualFragment"),
    ("innerText", r"innerText"), ("srcdoc", r"srcdoc"), ("execCommand", r"execCommand"),
    ("appendChild and old node calls", r"\b(?:appendChild|insertBefore|cloneNode|removeChild|replaceChild|replaceChildren|prepend)\b"),
    ("Element.remove()", r"\.remove\(\)"),
    # code from strings, modules, workers
    ("eval", r"\beval\b"), ("Function", r"\bFunction\b"), ("timer with a string", r"\b(?:setTimeout|setInterval|requestAnimationFrame)\(\s*[\"'`]"),
    ("import", r"\bimport\b"), ("importScripts", r"importScripts"), ("Worker", r"Worker"), ("javascript:", r"(?i)javascript:"),
    # network and anything loaded
    ("WebSocket", r"WebSocket"), ("EventSource", r"EventSource"), ("XMLHttpRequest", r"XMLHttpRequest"), ("sendBeacon", r"sendBeacon"),
    ("postMessage", r"postMessage"), ("BroadcastChannel", r"BroadcastChannel"), ("WebTransport", r"WebTransport"),
    ("RTCPeerConnection", r"RTCPeerConnection"), ("serviceWorker", r"(?i)serviceWorker"), ("caches", r"\bcaches\b"),
    ("indexedDB", r"indexedDB"), ("cookieStore", r"cookieStore"), ("document.cookie", r"document\s*\.\s*cookie"), ("window.open", r"window\s*\.\s*open\b"),
    ("window.name", r"window\s*\.\s*name\b"), ("document.domain", r"document\s*\.\s*domain"), ("opener", r"opener"),
    ("<link", r"<link"), ("<img", r"<img"), ("@import", r"@import"), ("Image(", r"\bImage\("), ("Audio(", r"\bAudio\("),
    ("a URL", r"(?i)https?:|ftp:|wss?:|//\s*[a-z0-9.-]+\.(?:com|org|net|io)\b"),
    ("a property that loads or sends (src, srcset, action, data, formAction, cookie, domain, name = ...)",
     r"\.(?:src|srcset|action|formAction|data|cookie|domain|name)\s*=[^=]"),
    ("an event handler property (.onclick = ...)", r"\.on[a-z]+\s*=[^=]"),
    # navigation
    ("history", r"\bhistory\b"), ("location = ...", r"\blocation\s*=[^=]"), (".href = ...", r"\.href\s*=[^=]"),
    # styling and the page's own strings through other doors
    ("className", r"\bclassName\b"), ("dataset", r"\bdataset\b"), ("style.cssText", r"style\s*\.\s*cssText"), ("styleSheets", r"styleSheets"),
    ("getElementsBy", r"getElementsBy"),
    # globals and window properties
    ("globalThis", r"\bglobalThis\b"), ("bracket access on a global", r"\b(?:window|self|top|parent)\s*\["),
    ("assignment to a global (window.x = ...)", r"(?m)^\s*(?:window|self|top|parent)\.\w+\s*=[^=]"),
    ("assignment to this.x", r"\bthis\.\w+\s*=[^=]"), ("var", r"\bvar\b"),
]

# ---- the caps: (label, regex, how many times at most, the scripts that may use it). A script not listed may not match at all. ---
CAPS = [
    ("fetch", r"\bfetch\b", 1, {"refresh", "prefs", "builder"}),            # and only as the one canonical helper, see rule_network
    ("DOMParser", r"\bDOMParser\b", 1, {"refresh"}),
    ("parseFromString", r"\bparseFromString\(", 1, {"refresh"}),
    ("createPolicy", r"\bcreatePolicy\(", 1, {"refresh"}),
    ("importNode", r"\bimportNode\b", 1, {"refresh"}),
    ("replaceWith", r"\.replaceWith\(", 1, {"refresh"}),
    ("before/after", r"\.(?:before|after)\(", 4, {"builder"}),
    ("append", r"\.append\(", 3, {"builder"}),
    ("textContent", r"\btextContent\b", 99, {"refresh", "prefs", "builder"}),
    ("localStorage", r"\blocalStorage\b", 99, {"prefs", "builder"}),
    ("sessionStorage", r"\bsessionStorage\b", 99, {"graph"}),
    ("navigator", r"\bnavigator\b", 2, {"prefs"}),
    ("clipboard.writeText", r"\bclipboard\.writeText\(", 1, {"prefs"}),
    ("trustedTypes", r"\btrustedTypes\b", 2, {"refresh"}),
]

# the one door to the network, the same text in refresh, prefs and builder (src/webjs.py _LOAD)
CANON_LOAD = re.compile(
    r'  const load = \(url, etag, signal\) => \{[^\n]*\n'
    r'    if \(typeof url !== "string" \|\| !url\.startsWith\("/\?"\)\) return Promise\.reject\(new Error\("refused"\)\);\n'
    r'    return fetch\(url, \{credentials: "same-origin", cache: "no-store", redirect: "error", '
    r'headers: etag \? \{"If-None-Match": etag\} : \{\}, signal\}\);\n  \};')

REFRESH_ATTRS_READ = {"data-edit", "data-frag", "data-refresh", "data-rotate", "data-paused", "data-k", "data-card", "data-rev",
                      "data-density", "data-kiosk"}

POLICIES = {
    # src/graphjs.py: the MAP's graph view. Its DOM contract is web.py's graph page.
    "graph": Policy(
        "graph", 16 * 1024, 300, ids={"gv", "gsvg", "gvp"},
        selectors={"a.n[data-k]", "line.e[data-a][data-b]", "a.n", "circle", "text"},
        attrs_read={"data-k", "data-a", "data-b", "data-state", "data-refresh", "data-paused", "cx", "cy", "r", "x", "y", "href",
                    "marker-end"},
        attrs_write={"transform", "cx", "cy", "x", "y", "x1", "y1", "x2", "y2"},
        classes_set={"js", "drag", "hov", "hv", "pin"},
        events={"DOMContentLoaded", "pointerdown", "pointermove", "pointerup", "pointercancel", "click", "dblclick", "dragstart",
                "wheel", "gesturestart", "gesturechange", "pointerover", "pointerout", "focusin", "focusout", "keydown", "pagehide",
                "visibilitychange"},
        styles={"touchAction", "userSelect", "webkitUserSelect"}, location={"reload", "assign"}, assign_calls=1,
        storage={"sessionStorage": (2, {"setItem", "getItem"})}, exact=("ids", "classes_set", "styles"),
        literal_attrs=False, dynamic_setattr=("const put = (el, name, v) => el.setAttribute(name, v.toFixed(1));",),
        ban_extra=(("removeAttribute", r"removeAttribute"),)),
    # the web shell's scripts (src/webjs.py; the DOM contract is its docstring)
    "refresh": Policy(
        "refresh", 11 * 1024, 160, ids={"stale"},
        selectors={"main", "summary", "[data-pause]", "[data-k]", "[data-card]", "details[data-k]", "input, textarea", "form"},
        attrs_read=REFRESH_ATTRS_READ, attrs_write={"data-paused", "aria-pressed"}, classes_set={"stale", "paused"},
        events={"DOMContentLoaded", "click", "visibilitychange", "online", "pointerdown", "keydown", "wheel"},
        location={"reload"}),
    "keys": Policy(
        "keys", 3584, 60, selectors={"a[data-key], button[data-key]", "[data-row]", "[data-grab]", "#help:target", "a[href]"},
        attrs_read={"data-key", "aria-disabled", "tabindex"}, events={"DOMContentLoaded", "keydown"}),
    "prefs": Policy(
        "prefs", 4864, 70, ids={"export"}, selectors={"a[data-set]", "[data-copy]"},
        attrs_read={"data-theme", "data-density", "data-copy", "data-done", "data-fail", "data-prefs", "data-prefs-src", "href"},
        attrs_write={"data-theme", "data-density", "aria-current"}, events={"DOMContentLoaded", "click"},
        location={"reload", "assign"}, assign_calls=1, storage={"localStorage": (2, {"getItem", "setItem"})}),
    "builder": Policy(
        "builder", 10752, 170, ids={"live"},
        selectors={"main.grid[data-edit]", "article.card[data-card]", "[data-hide]", "[data-drag]",
                   "[data-grow], [data-shrink], [data-hide], [data-save], [data-reset]"},
        attrs_read={"data-card", "data-title", "data-done", "data-save", "data-reset", "data-grow", "data-shrink"},
        attrs_write={"data-grab", "aria-pressed"}, classes_set={"s1", "s2", "s3", "s4", "off", "drag"},
        classes_read={"s2", "s3", "s4", "off"},
        events={"DOMContentLoaded", "click", "pointerdown", "pointermove", "pointerup", "pointercancel", "keydown", "focusout"},
        location={"assign"}, assign_calls=1, storage={"localStorage": (3, {"getItem", "setItem", "removeItem"})}),
}


# ---- the rules ------------------------------------------------------------------------------------------------------------------
def rule_global(src, pol):
    """Nothing banned anywhere in the text (markup from data, code from strings, network, navigation, globals)."""
    out = ["banned: %s" % label for label, rx in GLOBAL_BANS if re.search(rx, src)]
    out += ["banned here: %s" % label for label, rx in pol.ban_extra if re.search(rx, src)]
    return out


def rule_caps(src, pol):
    """APIs only some scripts need: not elsewhere, and no more often than the table says."""
    out = []
    for label, rx, most, who in CAPS:
        n = len(re.findall(rx, src))
        if n and pol.name not in who:
            out.append("%s is not allowed in %s" % (label, pol.name))
        elif n > most:
            out.append("%s %d times (at most %d)" % (label, n, most))
    return out


def rule_network(src, pol):
    """fetch only through the one canonical helper (same-origin "/?..." URLs, no redirect, no cache); navigator only for the clipboard."""
    out = []
    if re.search(r"\bfetch\b", src):
        if len(re.findall(r"\bfetch\b", src)) != 1 or not CANON_LOAD.search(src):
            out.append("fetch is not the one canonical load() helper")
        if len(re.findall(r"\bload\(", src)) < 1:
            out.append("load() is defined but never called")
    for m in re.finditer(r"\bnavigator\b(?!\.clipboard\b)", src):
        out.append("navigator other than navigator.clipboard")
    return out


def rule_navigation(src, pol):
    """location: only the members the script is allowed; assign() takes an href or data-done read from the page."""
    out = []
    members = strings(r"\blocation\.(\w+)", src)
    if members != pol.location:
        out.append("location members %s, expected %s" % (sorted(members), sorted(pol.location)))
    calls = re.findall(r"\blocation\.assign\(([^)]*)\)", src)
    if len(calls) != pol.assign_calls:
        out.append("location.assign called %d times, expected %d" % (len(calls), pol.assign_calls))
    for arg in calls:
        if not re.fullmatch(r"\w+", arg) or not re.search(r"\b%s\s*=\s*[^;,]*getAttribute\(\"(?:href|data-done)\"\)" % re.escape(arg), src):
            out.append("location.assign(%s): not an href read from the page" % arg)
    return out


def rule_storage(src, pol):
    """Browser storage only where the policy says, every line inside try/catch, and only the named methods."""
    out = []
    for api in ("localStorage", "sessionStorage"):
        lines = [ln for ln in code_lines(src) if api in ln]
        want = pol.storage.get(api)
        if want is None:
            out += ["%s is not allowed in %s" % (api, pol.name)] * bool(lines)
            continue
        if len(lines) != want[0]:
            out.append("%s on %d lines, expected %d" % (api, len(lines), want[0]))
        out += ["%s outside try/catch: %s" % (api, ln.strip()) for ln in lines if "try {" not in ln or "catch" not in ln]
        if strings(r"%s\.(\w+)" % api, src) != want[1]:
            out.append("%s methods %s" % (api, sorted(strings(r"%s\.(\w+)" % api, src))))
    return out


def rule_shape(src, pol):
    """Cannot end its <script> element, ASCII, small, a strict IIFE with nothing at the top level, no globals, documented classes."""
    out = []
    low = src.lower()
    out += ["cannot contain %s" % bad for bad in ("</script", "<script", "<!--", "-->", "]]>", "\x00") if bad in low]
    try:
        src.encode("ascii")
    except UnicodeEncodeError:
        out.append("not ASCII")
    if "\r" in src:
        out.append("CR in the text")
    if len(src.encode("utf-8")) >= pol.max_bytes:
        out.append("%d bytes, the limit is %d" % (len(src.encode("utf-8")), pol.max_bytes))
    if len(src.splitlines()) > pol.max_lines:
        out.append("%d lines, the limit is %d" % (len(src.splitlines()), pol.max_lines))
    code = "\n".join(code_lines(src))
    if not code.startswith("(function main() {\n  \"use strict\";\n"):
        out.append("does not start with the strict IIFE: %r" % code[:60])
    if not code.rstrip().endswith("})();"):
        out.append("does not end with })();")
    if code.count("\n(function"):
        out.append("more than one top-level function")
    for ln in code.splitlines()[1:-1]:
        if ln.strip() and not ln.startswith(" "):
            out.append("something at the top level: %s" % ln)
    for ln in code.splitlines():
        if re.match(r"\s*(var|window\.\w+\s*=|this\.\w+\s*=)", ln):
            out.append("var or a global: %s" % ln)
    if "(function" in src:
        head = src[:src.index("(function")]
        if not head.startswith("//"):
            out.append("starts without a comment that says what it is")
        for cls in sorted(pol.classes_set):
            if not re.search(r"^//.*[ #.]%s\b" % re.escape(cls), head, re.M):
                out.append("class %s is set but not documented at the top" % cls)
    return out


def _literal_only(src, funcs, what):
    """A problem when a call of one of the functions has a first argument that is not a string literal."""
    return ["%s: only string literals" % what] if re.search(r"\b(?:%s)\((?!\s*\")" % funcs, src) else []


def _contract(field, used, pol):
    """What the script uses of one kind (ids, selectors...) against the policy: never more, and for exact fields never less."""
    allowed, out = getattr(pol, field), []
    if used - allowed:
        out.append("%s not in the contract of %s: %s" % (field, pol.name, sorted(used - allowed)))
    if field in pol.exact and allowed - used:
        out.append("%s allowed for %s but never used: %s" % (field, pol.name, sorted(allowed - used)))
    return out


def rule_contract(src, pol):
    """The ids, selectors, attributes, classes, events and styles the script touches are exactly its policy, all literals."""
    out = []
    out += _literal_only(src, "getElementById", "an id")
    out += _literal_only(src, "querySelectorAll|querySelector|closest|matches", "a selector")
    out += _literal_only(src, "addEventListener", "an event name")
    out += _literal_only(src, r"classList\.(?:toggle|add|remove|contains)", "a class name")
    if pol.literal_attrs:
        out += _literal_only(src, "getAttribute|hasAttribute|removeAttribute", "an attribute name")
    out += _contract("ids", strings(r"getElementById\(\"([^\"]*)\"\)", src), pol)
    out += _contract("selectors", strings(r"(?:querySelectorAll|querySelector|closest|matches)\(\"([^\"]*)\"\)", src), pol)
    out += _contract("attrs_read", strings(r"\b(?:getAttribute|hasAttribute)\(\"([^\"]*)\"\)", src), pol)
    out += _contract("attrs_write", strings(r"\bsetAttribute\(\"([^\"]*)\"", src) | strings(r"\bremoveAttribute\(\"([^\"]*)\"\)", src), pol)
    out += _contract("classes_set", strings(r"classList\.(?:toggle|add|remove)\(\"([^\"]*)\"", src), pol)
    out += _contract("classes_read", strings(r"classList\.contains\(\"([^\"]*)\"", src), pol)
    out += _contract("events", strings(r"addEventListener\(\"([^\"]*)\"", src), pol)
    out += _contract("styles", strings(r"\.style\.(\w+)", src), pol)
    # setAttribute with a name that is not a literal: only the policy's own helper lines
    for ln in src.splitlines():
        if re.search(r"setAttribute\((?!\s*\")", ln) and ln.strip() not in pol.dynamic_setattr:
            out.append("setAttribute with a computed name: %s" % ln.strip())
    return out


RULES = [rule_global, rule_caps, rule_network, rule_navigation, rule_storage, rule_shape, rule_contract]


def check(name, src, policy=None):
    """Every problem the script has under the rules of its policy (policy: the one of that name unless given)."""
    pol = policy or POLICIES[name]
    out = []
    for rule in RULES:
        out += ["%s: %s" % (rule.__name__[5:], p) for p in rule(src, pol)]
    return out


def main():
    import graphjs
    import webjs
    sources = dict(webjs.SCRIPTS, graph=graphjs.SCRIPT)
    bad = 0
    for name in sorted(sources):
        problems = check(name, sources[name])
        bad += len(problems)
        print("%-8s %6d bytes  %s" % (name, len(sources[name].encode("utf-8")), "ok" if not problems else "%d problem(s)" % len(problems)))
        for p in problems:
            print("   -", p)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
