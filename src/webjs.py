"""nuc-console web view: the first-party scripts of the new shell, as Python string constants.

Four small scripts, each a strict-mode IIFE with no globals and no libraries, ASCII only. The shell inlines them (one <script>
each) under a Content-Security-Policy that lists their SHA-256 (csp_source), or serves them from /s/<name>.<sha8>.js (ASSETS);
either way no other script can run. The page works without any of them: every control is a link or a form. What they may do is
fixed by tests/jsrules.py (no markup built from data, no eval, no cookie, no connection but this server's "/?..." pages, ...)
and tests/test_webjs.py runs those rules on every script.

    REFRESH_JS   partial refresh: polls the page's fragment and swaps in the cards whose revision changed (~12 KB)
    KEYS_JS      keyboard: clicks the links the server marked with data-key, moves over the rows of a list (~3 KB)
    PREFS_JS     preferences: theme and density without a reload, the browser-side copy, the "Copy" button (~4 KB)
    BUILDER_JS   layout editor, only on ?edit=1 pages: drag, resize, hide, with a keyboard alternative, saved at every change (~12 KB)

The scripts only ever GET: they never send a form. A form POST (the AI screen's actions) stays a native form.

Use: an inline `<script>` holding the text of a constant, and its csp_source(...) in the page's script-src (inline is the default:
Safari and Firefox honour a hash source reliably only for inline scripts). Or `<script src=asset_path(name) integrity="sha256-...">`
with the bytes of ASSETS[name] served at that URL (immutable: the name carries the first 8 digits of the hash). The page's CSP
also needs `connect-src 'self'` for REFRESH_JS, PREFS_JS and BUILDER_JS, and, with `require-trusted-types-for 'script'`,
`trusted-types nuc-frag` (the one policy REFRESH_JS creates). KEYS_JS needs neither.

THE DOM CONTRACT (what the server renders and the scripts read)
==============================================================

Everything a script looks up is a literal selector, id or attribute name listed here (tests/jsrules.py checks both ways).

<html>
    data-density="wall|desk|compact"   the density; PREFS_JS changes it, REFRESH_JS reads it
    data-kiosk                         present in kiosk mode (with density wall, REFRESH_JS rotates the page)
    data-theme="..."                   the theme; PREFS_JS changes it (the value must match [a-z-]{1,16})
    data-prefs-src="cookie|config|url" where the effective preferences came from ("config": no nuc_ui cookie was sent)
    data-prefs="<string>"              optional: the canonical preferences string (cookie grammar, [A-Za-z0-9_.~:,-]{1,256})
    classes set by the scripts, for the server's CSS: stale (the numbers are old), paused (refresh paused by the toggle)

<main data-refresh="2" data-frag="/?view=overview&frag=1" data-rotate="20" data-paused="1">
    data-refresh   seconds between polls (1..3600); absent or 0: no polling
    data-frag      the URL to poll, must start with "/?" (the page's own URL plus frag=1); anything else is refused
    data-rotate    wall density with kiosk only: seconds between one-screen scrolls (>= 3); the page also reloads every ten minutes (the burn-in shift) and keeps its scroll position across that reload (sessionStorage key nuc-wall-y, read back only after a reload)
    data-paused    "1" when the page starts paused (REFRESH_JS keeps it in step when the toggle is used); the page then still has
                   data-refresh and data-frag, so that the toggle can resume it. Without data-refresh the toggle is left to the server
    data-edit      (main.grid[data-edit]) the layout editor page: REFRESH_JS does nothing there, BUILDER_JS starts

Blocks: every card is an element with data-card="<id>" and data-rev="<revision>" (the revision changes when, and only when, the
    card's HTML changes). Ids are unique: the 13 card ids of nuc_config.SECTIONS plus "__top" (the top bar: host, status pill, tabs,
    clock), "__kpis" (the KPI row) and "__view" (the body and toolbar of the Map, CPU, Health and AI pages: one block). A fragment holds the same blocks in the same order as the page; if the set or the order
    differs (a card appeared, went or moved) REFRESH_JS reloads the page. Inside a block:
    data-k="<key>"      a stable key (links, rows, details): focus and the open state of <details> survive a refresh by it
    data-row            a row of a list: KEYS_JS moves the focus over them and Enter follows the link of the focused row
    <details data-k>    its open state is kept as the reader left it
                        (the Health screen: each finding is div.fd[data-k="f-<id>"] holding details[data-k]; the period links are a[data-key="d|w|m"])
                        (the AI screen, div.scr.av: every button is a form posting to /ai/* with its hidden csrf and back fields, and carries data-key from the
                        keymap: e on/off, c cancel, u use the selected model, x delete its files, X delete all, y yes and n no (a link) to a question, Escape
                        closes the details; the question box is input[name="q"], which REFRESH_JS never replaces while it has focus or text)
    data-kpi="<id>" data-depth="<n>" data-max-lines="<n>"   what the components (htmlview.html) say of a KPI, a tree row and a list; for the
                        server's CSS only (the scripts never read them, a fragment may carry them)
    data-problem="<id>" the stable id of a problem in ATTENTION (a problem, or one accepted as known); for the server's CSS only
    class="card sN stateX rN"      (the overview's cards) rN: the grid rows the card spans (htmlview.est_rows, the server's estimate). REFRESH_JS
                        replaces it with the measured span (article.card[data-card]: its height in rows of main.grid's grid-auto-rows, r2..r200) after
                        load, each swap and a resize, only where the grid packs densely (not in one column, not on ?card=, not in the editor)
    data-state="ok|warn|err|unknown"   for the server's CSS only (the scripts never read it, a fragment may carry it)
    Ids in a fragment must not reuse the ids of this contract (stale, live, help, export). `name` is only allowed on form controls.

Server-rendered controls the scripts click or intercept:
    a[data-key="X"], button[data-key="X"]   data-key holds KeyboardEvent.key values (case matters: "Z", "?", "Escape",
                                            "ArrowLeft", "1"), several separated by one space ("? Escape"). A key press clicks the
                                            first visible, enabled match: the keymap is the server's, not the script's.
                                            Scope: while #help is the :target only the elements inside #help react, otherwise the
                                            rest of the page (the closed #help is not displayed). The AI screen's buttons are
                                            ordinary form buttons with data-key: a click on them is the reader's own submit.
    #help                                   the key overlay, shown with :target (a[href="#help"] opens it, a[href="#"] closes it)
    [data-pause]                            the pause toggle (a link to the paused URL, or a button; data-key="Z"). REFRESH_JS
                                            flips its state, sets aria-pressed on every [data-pause] and cancels the navigation
    #stale                                  an empty element (role=status, hidden): REFRESH_JS fills it with "stale since HH:MM:SS",
                                            "session expired" or "updating the page" and shows it
    a[data-set]                             a preference link, href="/?set=..." (a plain click is intercepted, the rest navigates);
                                            data-theme="dark" and/or data-density="wall" say what it switches to; aria-current marks
                                            the chosen one of its group
    [data-copy="#export"]                   a button: copies the text of #export (the [ui] snippet, an element with id "export");
                                            data-done / data-fail are optional labels it shows for a moment
    main.grid[data-edit] > article.card[data-card]   the editor's cards, nothing else in the grid; width classes s1..s4 (exactly one)
    [data-drag]      the drag handle in a card: it moves the card (the server's CSS gives it touch-action: none, and shows it only on a
                     card that has tabindex, that is, once BUILDER_JS runs)
    [data-size]      the resize handle in a card: dragged sideways, it sets the width (snapped to 1-4 quarters of the grid)
    [data-earlier] [data-later] [data-shrink] [data-grow] [data-hide]   links in a card: one place earlier or later, narrower, wider,
                     hide/show. Each is an href="/?set=e<step>..." of the server (the editor without the script); BUILDER_JS takes a
                     plain click, changes the page and sends the whole layout instead. They carry no data-set.
    class off        a card that is hidden in the layout: it stays in the grid, dimmed, so it can be shown again
    class drag       set on the card being dragged;  data-grab: set on a card grabbed with the keyboard (KEYS_JS stands aside)
    [data-title]     optional: the card's name for the screen reader (else data-card is used)
    #live            the editor's aria-live region
    cards get tabindex=0 from BUILDER_JS; the server's CSS marks them with :focus-visible, .drag and [data-grab]

THE FRAGMENT CONTRACT (REFRESH_JS)
==================================

    GET <page url>&frag=1  (what main[data-frag] says)
        200  Content-Type: text/html, X-Nuc-Fragment: 1, ETag: "...", body: only the blocks above (no <html>, no <head>, no
             text between them but whitespace). Only the tags in FRAG_TAGS and the attributes in FRAG_ATTRS are accepted; href
             must start with "/?" or "#", action with "/" (not "//"), method is get or post, there are no on* attributes and no
             style. Anything else makes the page reload.
        304  when If-None-Match carries the current ETag.
        401  the session (token cookie) is gone: the page says "session expired" and stops polling.
        other status, other type, a missing X-Nuc-Fragment, a redirect: a failed poll (backoff, "stale since ...").
    GET /?set=<prefs string>&frag=1  (PREFS_JS, BUILDER_JS)
        204  Set-Cookie: nuc_ui=... (HttpOnly: no script can read or write it), and optionally X-Nuc-Prefs: <canonical string>
             that PREFS_JS keeps in localStorage["nuc-ui"]. Anything but 204 is a failure.
        Layout strings (BUILDER_JS) use the cookie grammar l<card2><1-4>[x]_... , the codes are CARD_CODES. Each change of the editor
        is one such request; BUILDER_JS sends them one after the other and puts the page back to the last layout the server
        confirmed (204) when one fails.

Browser storage: localStorage "nuc-ui" (PREFS_JS: the preferences string) and "nuc-ui-sync" (when it last re-sent it), always
inside try/catch. BUILDER_JS keeps nothing in the browser: the cookie is the layout. document.cookie is never touched.
"""
import base64
import hashlib

# What a fragment may contain (REFRESH_JS walks the parsed fragment and reloads the page on anything else). Lowercase names as the
# HTML parser reports them; the SVG ones are the primitives of the bars and sparklines. No script, style, link, meta, img, iframe,
# object, embed, template, base, canvas, math, foreignObject, use, image: nothing that loads, runs or restyles.
FRAG_TAGS = (
    "a", "abbr", "article", "aside", "b", "br", "button", "caption", "circle", "code", "col", "colgroup", "dd", "details", "div", "dl", "dt", "em",
    "figure", "footer", "form", "g", "h2", "h3", "h4", "header", "hr", "i", "input", "kbd", "label", "li", "line", "meter", "nav", "ol",
    "option", "p", "path", "polyline", "pre", "progress", "rect", "section", "select", "small", "span", "strong", "sub", "summary",
    "sup", "svg", "table", "tbody", "td", "text", "textarea", "tfoot", "th", "thead", "time", "title", "tr", "u", "ul",
)
# Attribute names, case as the parser reports them (viewBox). No on*, no style, no src/srcset/srcdoc/formaction/target/rel/ping.
# Values: href starts with "/?" or "#", action with "/" and not "//", method is get or post (REFRESH_JS checks).
FRAG_ATTRS = (
    "action", "aria-controls", "aria-disabled", "aria-current", "aria-describedby", "aria-expanded", "aria-hidden", "aria-label", "aria-labelledby",
    "aria-live", "aria-pressed", "aria-sort", "aria-valuemax", "aria-valuemin", "aria-valuenow", "autocomplete", "checked", "class",
    "colspan", "cx", "cy", "d", "data-card", "data-copy", "data-density", "data-depth", "data-done", "data-fail", "data-k", "data-key", "data-kpi",
    "data-max-lines", "data-pause", "data-problem", "data-rev", "data-row", "data-set", "data-state", "data-theme", "datetime", "disabled", "fill", "for", "height", "high", "href",
    "id", "lang", "low", "max", "maxlength", "method", "min", "name", "open", "optimum", "placeholder", "points",
    "preserveAspectRatio", "r", "readonly", "required", "role", "rowspan", "rx", "ry", "scope", "selected", "size", "stroke",
    "stroke-linecap", "stroke-linejoin", "stroke-width", "tabindex", "title", "transform", "type", "value", "viewBox", "width",
    "x", "x1", "x2", "y", "y1", "y2",
)
# The two-letter codes of the cookie's layout grammar (nuc_config.SECTIONS; tests/test_webjs.py keeps them in step).
CARD_CODES = (
    ("attention", "at"), ("exposure", "ex"), ("webapps", "wa"), ("firewall", "fw"), ("system", "sy"), ("containers", "ct"),
    ("databases", "db"), ("boot", "bo"), ("network_traffic", "nt"), ("sessions", "se"), ("tailscale", "ts"), ("docker_disk", "dd"),
    ("disks", "di"),
)

# The one door to the network, the same text in every script that fetches: only this server's own "/?..." pages, same origin,
# never a redirect, never cached. The URL comes from the page (data-frag, a link's href, data-done) or is built here from a
# checked string; the check below is what makes that safe. tests/jsrules.py demands exactly this text and exactly one fetch.
_LOAD = r"""  const load = (url, etag, signal) => {  // the only door to the network: this server's "/?..." pages
    if (typeof url !== "string" || !url.startsWith("/?")) return Promise.reject(new Error("refused"));
    return fetch(url, {credentials: "same-origin", cache: "no-store", redirect: "error", headers: etag ? {"If-None-Match": etag} : {}, signal});
  };"""

_REFRESH = r"""// nuc-console web view: partial refresh (src/webjs.py REFRESH_JS). It polls main[data-frag] for the page's cards as an HTML
// fragment and replaces the cards whose data-rev changed. The fragment is parsed inertly and every node and attribute is checked
// against two allowlists before anything is taken over; a fragment that fails the check reloads the page. It waits while the tab is
// hidden, while paused and while you type or select; a failed poll backs off (2 s to 60 s) and says so: numbers never look fresh.
// Classes it sets on <html>, for the server's CSS:
//   stale    the numbers on the page are old (the #stale banner says since when)
//   paused   the refresh is paused with the [data-pause] toggle
// It also trues up the grid's row spans: the server estimates each overview card's height (class rN), the script measures it and sets
// the exact span, after load, after every swap and on resize (debounced); the classes r2..r200 are the server's CSS. The class
// attribute is rewritten as a whole (the class names are computed, and classList is only used with literal names here).
// In wall density with kiosk on it also scrolls one screen every main[data-rotate] seconds, and the page it reloads every ten minutes
// (and when it cannot take a fragment in) comes back at the same scroll position (sessionStorage "nuc-wall-y", this tab only).
(function main() {
  "use strict";
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const FRAG_TAGS = new Set("@FRAG_TAGS@".split(" ")), FRAG_ATTRS = new Set("@FRAG_ATTRS@".split(" "));
  const root = document.documentElement, page = document.querySelector("main"), banner = document.getElementById("stale");
  if (!page || page.hasAttribute("data-edit")) return;
  const url = page.getAttribute("data-frag") || "", secs = Number(page.getAttribute("data-refresh"));
  const period = url && secs >= 1 ? Math.min(secs, 3600) * 1000 : 0, spin = Number(page.getAttribute("data-rotate")) * 1000;
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
  const opened = new Map();  // <details> the reader opened or closed by hand: data-k -> open
  let etag = "", fails = 0, lastOk = Date.now(), touched = 0, timer = 0, busy = false, dead = false, policy = null;
  let paused = page.getAttribute("data-paused") === "1";
@LOAD@
  try { policy = window.trustedTypes ? window.trustedTypes.createPolicy("nuc-frag", {createHTML: s => s}) : null; } catch (err) { policy = null; }
  const pad = n => (n < 10 ? "0" : "") + n;
  const clock = ms => { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
  const kiosk = () => root.getAttribute("data-density") === "wall" && root.hasAttribute("data-kiosk");
  const plain = e => !(e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey);

  function show(text) {  // the banner under the top bar; "" takes it down
    root.classList.toggle("stale", !!text);
    if (banner) { banner.textContent = text; banner.hidden = !text; }
  }
  const late = () => show("stale since " + clock(lastOk));
  function plan(ms) { clearTimeout(timer); if (!dead && period) timer = setTimeout(tick, ms); }
  function reload() {  // a page that cannot take the fragment in as it is: load it whole, at most once in 15 seconds
    dead = true;
    show("updating the page");
    setTimeout(again, Math.max(0, 15000 - performance.now()));
  }
  function again() {  // a reload that keeps a kiosk wall where it was scrolled to (the next page restores it)
    try { if (kiosk()) sessionStorage.setItem("nuc-wall-y", String(Math.round(window.scrollY))); } catch (err) { /* storage off */ }
    location.reload();
  }
  function editing() {  // the reader is working on the page: a form has the focus, an input holds text, text is selected
    const a = document.activeElement, sel = window.getSelection();
    if (a && (a.closest("form") || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return true;
    if (sel && !sel.isCollapsed && !kiosk()) return true;
    for (const f of document.querySelectorAll("input, textarea")) {
      if (!/^(hidden|submit|button|reset|image|checkbox|radio)$/.test(f.type) && f.value !== f.defaultValue) return true;
    }
    return false;
  }

  function clean(n) {  // true when the node and everything below it is on the allowlists
    if (n.nodeType === 3) return true;
    if (n.nodeType !== 1 || !FRAG_TAGS.has(n.localName) || !(n instanceof HTMLElement || n instanceof SVGElement)) return false;
    for (const a of n.attributes) {
      const v = a.value;
      if (!FRAG_ATTRS.has(a.name) || /^(on|style$)/.test(a.name)) return false;
      if (a.name === "href" && !(v.startsWith("/?") || v.startsWith("#"))) return false;
      if (a.name === "action" && !/^\/[^\/\\]/.test(v)) return false;
      if (a.name === "method" && !/^(get|post)$/i.test(v)) return false;
      if (a.name === "name" && !/^(input|select|textarea|button)$/.test(n.localName)) return false;
    }
    for (const c of n.childNodes) if (!clean(c)) return false;
    return true;
  }
  function parse(text) {  // the fragment's blocks, or null when anything in it is off the allowlists
    const doc = new DOMParser().parseFromString(policy ? policy.createHTML(text) : text, "text/html"), out = [];
    if (doc.head.childNodes.length || doc.documentElement.attributes.length || doc.body.attributes.length) return null;
    for (const n of doc.body.childNodes) {
      if (n.nodeType === 3 && !n.data.trim()) continue;
      if (n.nodeType !== 1 || !n.hasAttribute("data-card") || !clean(n)) return null;
      out.push(n);
    }
    return out.length && out.length <= 200 ? out : null;
  }
  function put(old, node) {  // old gives way to a copy of node; the focus and the open <details> stay as the reader had them
    const a = document.activeElement, f = a && old.contains(a) ? a.closest("[data-k]") : null, key = f && f.getAttribute("data-k");
    const fresh = document.importNode(node, true);
    for (const d of fresh.querySelectorAll("details[data-k]")) {
      const k = d.getAttribute("data-k");
      if (opened.has(k)) d.open = opened.get(k);
    }
    old.replaceWith(fresh);
    if (key) for (const el of fresh.querySelectorAll("[data-k]")) if (el.getAttribute("data-k") === key) { el.focus({preventScroll: true}); break; }
  }
  function swap(blocks) {  // false when the page has other cards than the fragment: that is a new page, not an update
    const live = Array.from(document.querySelectorAll("[data-card]")), ids = list => list.map(el => el.getAttribute("data-card")).join(" ");
    if (ids(live) !== ids(blocks)) return false;
    const x = window.scrollX, y = window.scrollY;
    blocks.forEach((b, i) => { if (b.getAttribute("data-rev") !== live[i].getAttribute("data-rev")) put(live[i], b); });
    if (window.scrollX !== x || window.scrollY !== y) window.scrollTo(x, y);
    return true;
  }

  function fit() {  // the grid's dense packing with the measured heights: each card spans the rows its own height needs (no hole, no overlap)
    const g = getComputedStyle(page);
    if (!/dense/.test(g.gridAutoFlow) || url.indexOf("card=") >= 0) return;  // one column (rows are auto), or one card in full
    const hit = /([0-9.]+)px/.exec(g.gridAutoRows), unit = hit ? Number(hit[1]) : parseFloat(g.fontSize) * 0.5, todo = [];
    if (!(unit > 1)) return;
    for (const c of document.querySelectorAll("article.card[data-card]")) {  // measure all first, then write: one layout, not one per card
      const names = (c.getAttribute("class") || "").split(/\s+/).filter(n => n), keep = names.filter(n => !/^r[0-9]+$/.test(n));
      const h = c.getBoundingClientRect().height + (parseFloat(getComputedStyle(c).marginBottom) || 0);
      const want = Math.min(200, Math.ceil(h / unit - 0.05)), next = want > 1 ? keep.concat("r" + want) : keep;
      if (h > 0 && next.join(" ") !== names.join(" ")) todo.push([c, next]);
    }
    for (const [c, next] of todo) c.setAttribute("class", next.join(" "));
  }
  async function poll() {
    const ctl = new AbortController(), kill = setTimeout(() => ctl.abort(), 10000);
    busy = true;
    try {
      const r = await load(url, etag, ctl.signal);
      if (r.status === 401) { dead = true; show("session expired"); return; }
      if (r.status !== 304) {
        if (r.status !== 200 || r.headers.get("X-Nuc-Fragment") !== "1" || !/^text\/html\b/i.test(r.headers.get("Content-Type") || "")) throw new Error("not a fragment");
        const text = await r.text(), blocks = text.length < 4e6 ? parse(text) : null;
        if (editing()) { plan(period); return; }  // the reader started typing meanwhile: not now
        if (!blocks || !swap(blocks)) { reload(); return; }
        fit();
        etag = r.headers.get("ETag") || "";
      }
      fails = 0; lastOk = Date.now(); show("");
      plan(period);
    } catch (err) {
      fails++; late();
      plan(Math.min(60000, Math.max(period, 2000) * Math.pow(2, fails - 1)));
    } finally { clearTimeout(kill); busy = false; }
  }
  function tick() {
    if (document.hidden || dead || paused) return;  // visibilitychange and the toggle start it again
    if (busy) { plan(period); return; }
    if (editing()) {
      if (Date.now() - lastOk > Math.max(30000, 5 * period)) late();  // a long pause for typing is still old data
      plan(period);
    } else poll();
  }
  function turn() {  // kiosk: one screen further down, back to the top at the end, unless someone is at the page
    setTimeout(turn, spin);
    const room = root.scrollHeight - window.innerHeight;
    if (!kiosk() || document.hidden || paused || Date.now() - touched < 3 * spin || room < 8) return;
    window.scrollTo({top: window.scrollY >= room - 8 ? 0 : Math.min(room, window.scrollY + window.innerHeight * 0.9), behavior: calm.matches ? "auto" : "smooth"});
  }

  document.addEventListener("click", e => {
    const s = e.target.closest("summary"), d = s && s.parentElement, k = d && d.getAttribute("data-k");
    if (k) opened.set(k, !d.open);  // before the click's own effect: what the reader is about to make of it
    const b = e.target.closest("[data-pause]");
    if (!b || !plain(e) || !period) return;  // a page that does not poll (a graph reloads itself) leaves the link to the server
    e.preventDefault();
    paused = !paused;
    root.classList.toggle("paused", paused);
    page.setAttribute("data-paused", paused ? "1" : "0");
    for (const t of document.querySelectorAll("[data-pause]")) t.setAttribute("aria-pressed", String(paused));
    if (!paused) plan(0);
  }, true);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) plan(0); });
  window.addEventListener("online", () => plan(0));
  window.addEventListener("pointerdown", () => { touched = Date.now(); }, true);
  window.addEventListener("keydown", () => { touched = Date.now(); }, true);
  window.addEventListener("wheel", () => { touched = Date.now(); }, {capture: true, passive: true});
  root.classList.toggle("paused", paused);
  let shaken = 0;
  window.addEventListener("resize", () => { clearTimeout(shaken); shaken = setTimeout(fit, 150); });
  window.addEventListener("load", fit);
  fit();
  if (!paused) plan(period);
  if (spin >= 3000) {  // a wall: scroll, and start the page again every ten minutes, so that the server's burn-in shift (its shift-N class) moves
    setTimeout(turn, spin);
    setTimeout(again, 600000 - Date.now() % 600000 + 2000);
    const nav = performance.getEntriesByType("navigation")[0];  // only a reload gives the place back: a fresh visit starts at the top
    let y = 0;
    try { y = nav && nav.type === "reload" && kiosk() ? Number(sessionStorage.getItem("nuc-wall-y")) || 0 : 0; } catch (err) { y = 0; }
    if (y > 0) window.scrollTo({top: y, behavior: "instant"});
  }
})();
"""

_KEYS = r"""// nuc-console web view: keyboard (src/webjs.py KEYS_JS). A key press clicks the link or button the server marked with data-key:
// the keymap is the server's, this script has none. In a list, the arrows, j and k, PageUp and PageDown, Home and End move the
// focus over the elements marked data-row and Enter follows the focused row's link. Keys with Ctrl, Alt or Meta, keys typed into
// a field and keys while a card is grabbed in the layout editor are left alone. It sets no classes and keeps nothing.
(function main() {
  "use strict";
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const STEP = new Map([["ArrowDown", 1], ["j", 1], ["ArrowUp", -1], ["k", -1], ["PageDown", 2], ["PageUp", -2]]);
  const shown = el => el.getClientRects().length > 0;
  const typing = t => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

  function press(key, scope) {  // click the first visible, enabled element that asks for this key
    for (const el of scope.querySelectorAll("a[data-key], button[data-key]")) {
      if (!el.getAttribute("data-key").split(" ").includes(key) || el.disabled || el.getAttribute("aria-disabled") === "true" || !shown(el)) continue;
      el.click();
      return true;
    }
    return false;
  }
  function move(key) {  // the focus to another row of the list
    const rows = Array.from(document.querySelectorAll("[data-row]")).filter(shown), n = rows.length;
    if (!n) return false;
    const a = document.activeElement, cur = rows.indexOf(a && a.closest("[data-row]")), step = STEP.get(key) || 0;
    const page = Math.max(1, Math.floor(window.innerHeight / (rows[0].getBoundingClientRect().height || 24)) - 1);
    let i = cur < 0 ? (step > 0 || key === "Home" ? 0 : n - 1) : cur + (Math.abs(step) > 1 ? Math.sign(step) * page : step);
    if (key === "Home") i = 0;
    if (key === "End") i = n - 1;
    const row = rows[Math.max(0, Math.min(n - 1, i))];
    if (!row.hasAttribute("tabindex") && !/^(A|BUTTON)$/.test(row.tagName)) row.tabIndex = -1;  // so that it can take the focus
    row.focus();
    return true;
  }
  function follow() {  // Enter on a row that is not itself a link: the link inside it
    const a = document.activeElement, row = a && a.closest("[data-row]");
    if (!row || /^(A|BUTTON)$/.test(a.tagName)) return false;
    const link = row.querySelector("a[href]");
    if (link) link.click();
    return !!link;
  }

  document.addEventListener("keydown", e => {
    if (e.defaultPrevented || e.ctrlKey || e.altKey || e.metaKey || e.isComposing || typing(e.target) || document.querySelector("[data-grab]")) return;
    const k = e.key, help = document.querySelector("#help:target");
    let hit = false;
    if (!help && (STEP.has(k) || k === "Home" || k === "End")) hit = move(k);
    else if (!help && k === "Enter") hit = follow();
    if (!hit && !e.repeat) hit = press(k, help || document);
    if (hit) e.preventDefault();
  });
})();
"""

_PREFS = r"""// nuc-console web view: preferences (src/webjs.py PREFS_JS). A click on a[data-set] (href="/?set=...") is sent to the server in
// the background (it answers 204 and sets its HttpOnly cookie) and the theme and density the link names (data-theme, data-density)
// go on <html> at once, without a reload; if that fails the link is followed as it is. The preferences string the server sends
// back is kept in localStorage["nuc-ui"], and sent again, once, when the server found no cookie. A [data-copy="#export"] button
// copies the text of #export. It never touches the cookie. It sets no classes.
(function main() {
  "use strict";
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const KEY = "nuc-ui", SYNC = "nuc-ui-sync", SAFE = /^[A-Za-z0-9_.~:,-]{1,256}$/, WORD = /^[a-z-]{1,16}$/;
  const root = document.documentElement;
@LOAD@
  const read = k => { try { return localStorage.getItem(k); } catch (err) { return null; } };
  const write = (k, v) => { try { localStorage.setItem(k, v); } catch (err) { /* storage off or full */ } };
  const plain = e => !(e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey);
  const mark = (b, on) => { if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current"); };

  function apply(a) {  // what the link switches to, on the page now; a preference the page does not show needs the server's redraw
    const th = a.getAttribute("data-theme"), de = a.getAttribute("data-density");
    if (th === null && de === null) { location.reload(); return; }
    if (th !== null && WORD.test(th)) root.setAttribute("data-theme", th);
    if (de !== null && WORD.test(de)) root.setAttribute("data-density", de);
    for (const b of document.querySelectorAll("a[data-set]")) {
      if (th !== null && b.hasAttribute("data-theme")) mark(b, b.getAttribute("data-theme") === th);
      if (de !== null && b.hasAttribute("data-density")) mark(b, b.getAttribute("data-density") === de);
    }
  }
  function copy(b, src) {  // the text of src to the clipboard, within the click; where that is not allowed, select it
    const done = b.getAttribute("data-done"), fail = b.getAttribute("data-fail"), was = b.textContent;
    const say = t => { if (t) { b.textContent = t; setTimeout(() => { b.textContent = was; }, 1800); } };
    const pick = () => { const r = document.createRange(), s = window.getSelection(); r.selectNodeContents(src); s.removeAllRanges(); s.addRange(r); say(fail); };
    if (!navigator.clipboard) pick();
    else navigator.clipboard.writeText(src.textContent).then(() => say(done), pick);
  }

  document.addEventListener("click", e => {
    const a = e.target.closest("a[data-set]"), href = a && a.getAttribute("href"), c = e.target.closest("[data-copy]");
    if (c && c.getAttribute("data-copy") === "#export" && document.getElementById("export")) { e.preventDefault(); copy(c, document.getElementById("export")); return; }
    if (!a || !plain(e) || !href || !href.startsWith("/?set=") || href.indexOf("#") >= 0) return;
    e.preventDefault();
    load(href + "&frag=1").then(r => {
      if (r.status !== 204) throw new Error("status " + r.status);
      const p = r.headers.get("X-Nuc-Prefs");
      if (p && SAFE.test(p)) write(KEY, p);
      apply(a);
    }).catch(() => location.assign(href));  // without the script this is what the link does anyway
  });

  const mine = root.getAttribute("data-prefs"), kept = read(KEY);
  if (root.getAttribute("data-prefs-src") === "cookie" && mine && SAFE.test(mine)) write(KEY, mine);
  else if (root.getAttribute("data-prefs-src") === "config" && kept && kept !== "1" && SAFE.test(kept) && Date.now() - (Number(read(SYNC)) || 0) > 300000) {
    write(SYNC, String(Date.now()));  // once in a while, so that a browser that refuses the cookie is not sent in circles
    load("/?set=" + kept + "&frag=1").then(r => { if (r.status === 204) location.reload(); }).catch(() => {});
  }
})();
"""

_BUILDER = r"""// nuc-console web view: layout editor (src/webjs.py BUILDER_JS), only on the ?edit=1 page: main.grid[data-edit]. Cards are
// reordered by moving the ones already there; no markup is made. Pointer: drag a card by its [data-drag] handle to move it, drag its
// [data-size] handle sideways to resize it (it snaps to 1-4 columns). The links [data-earlier], [data-later], [data-shrink],
// [data-grow] and [data-hide] do the same one step at a time. Keyboard: Space on a card grabs or drops it, arrows move it, + and -
// resize, x hides or shows, Escape puts it back. #live announces each step ("exposure: position 3 of 12, width 2").
// Every change is saved at once: the whole layout (cookie grammar l<card2><1-4>[x]_...) goes to the server as
// GET /?set=...&frag=1; when that fails, the page goes back to the last layout the server kept. Without this script the same
// controls are links to the server's own steps.
// Classes it sets, for the server's CSS:
//   s1 s2 s3 s4   the card's width      off    the card is hidden in the layout (it stays in the grid, dimmed)
//   drag          the card being dragged (data-grab marks a card grabbed with the keyboard)
(function main() {
  "use strict";
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const grid = document.querySelector("main.grid[data-edit]");
  if (!grid) return;
  const CODES = new Map("@CODES@".split(" ").map(p => p.split(":"))), live = document.getElementById("live");
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
@LOAD@
  const plain = e => !(e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey);
  const cards = () => Array.from(grid.querySelectorAll("article.card[data-card]"));
  const code = c => CODES.get(c.getAttribute("data-card"));
  const width = c => c.classList.contains("s4") ? 4 : c.classList.contains("s3") ? 3 : c.classList.contains("s2") ? 2 : 1;
  const setWidth = (c, n) => { c.classList.toggle("s1", n === 1); c.classList.toggle("s2", n === 2); c.classList.toggle("s3", n === 3); c.classList.toggle("s4", n === 4); };
  const inside = (r, x, y) => x >= r.left && x < r.right && y >= r.top && y < r.bottom;
  let drag = null, grab = null;  // a pointer drag {id, c, h, s, over, x, y, size}; a keyboard grab {c, s}

  function layout() { return "l" + cards().filter(code).map(c => code(c) + width(c) + (c.classList.contains("off") ? "x" : "")).join("_"); }
  const snap = () => cards().map(c => ({c, w: width(c), off: c.classList.contains("off")}));
  function sync(c) { const h = c.querySelector("[data-hide]"); if (h) h.setAttribute("aria-pressed", String(c.classList.contains("off"))); }
  function restore(s) { for (const o of s) { grid.append(o.c); setWidth(o.c, o.w); o.c.classList.toggle("off", o.off); sync(o.c); } }
  function arrange(str) {  // the layout string, on the page; false when it is not one
    if (!/^l[a-z]{2}[1-4]x?(_[a-z]{2}[1-4]x?)*$/.test(str)) return false;
    const by = new Map(cards().map(c => [code(c), c])), done = new Set();
    for (const part of str.slice(1).split("_")) {
      const c = by.get(part.slice(0, 2));
      if (!c || done.has(c)) continue;
      done.add(c); grid.append(c); setWidth(c, Number(part[2])); c.classList.toggle("off", part.length > 3); sync(c);
    }
    return true;
  }
  let sent = layout(), kept = sent, queue = Promise.resolve();  // the layout last sent, the one the server confirmed, the requests in turn
  function say(c, extra) {  // "exposure: position 3 of 12, width 2"
    const all = cards(), name = (c.getAttribute("data-title") || c.getAttribute("data-card")).replace(/_/g, " ");
    if (live) live.textContent = name + ": position " + (all.indexOf(c) + 1) + " of " + all.length + ", width " + width(c) + (c.classList.contains("off") ? ", hidden" : "") + (extra ? ". " + extra : "");
  }
  function commit(c, extra) {  // tell the reader, and the server when the layout is not the one it has
    say(c, extra);
    if (layout() === sent) return;
    sent = layout();
    queue = queue.then(() => {
      const str = sent;  // the latest: a request that failed before this one has put the page back
      return load("/?set=" + str + "&frag=1").then(r => {
        if (r.status !== 204) throw new Error("status " + r.status);
        kept = str;
      });
    }).catch(() => {
      arrange(kept); sent = kept;
      if (live) live.textContent = "Saving failed; the layout is back as the server has it";
    });
  }
  function resize(c, d) { setWidth(c, Math.max(1, Math.min(4, width(c) + d))); commit(c); }
  function flip(c) { c.classList.toggle("off"); sync(c); commit(c); }
  function nudge(c, d) {  // one place earlier or later
    const all = cards(), o = all[all.indexOf(c) + d];
    if (!o) { say(c); return; }
    if (d < 0) o.before(c); else o.after(c);
    c.focus({preventScroll: true});  // moving a node drops its focus
    c.scrollIntoView({block: "nearest", behavior: calm.matches ? "auto" : "smooth"});
    commit(c);
  }
  function release(extra) { const c = grab.c; grab = null; c.removeAttribute("data-grab"); say(c, extra); }

  document.addEventListener("click", e => {
    const t = e.target.closest("[data-earlier], [data-later], [data-grow], [data-shrink], [data-hide]"), c = t && t.closest("article.card[data-card]");
    if (!t || !c || !plain(e)) return;
    e.preventDefault();  // without the script these are links to the server's own step
    if (t.hasAttribute("data-earlier")) nudge(c, -1);
    else if (t.hasAttribute("data-later")) nudge(c, 1);
    else if (t.hasAttribute("data-grow")) resize(c, 1);
    else if (t.hasAttribute("data-shrink")) resize(c, -1);
    else flip(c);
    t.focus({preventScroll: true});
  });
  function hold() {  // the handle keeps the pointer even outside the window; moving a node in the page drops that, so ask again
    try { drag.h.setPointerCapture(drag.id); } catch (err) { /* the pointer is gone */ }
  }
  document.addEventListener("pointerdown", e => {
    const h = e.target.closest("[data-drag], [data-size]"), c = h && h.closest("article.card[data-card]");
    if (!c || e.button > 0 || drag || grab) return;
    drag = {id: e.pointerId, c, h, s: snap(), over: null, x: e.clientX, y: e.clientY, size: h.hasAttribute("data-size")};
    hold();
    c.classList.add("drag");
    e.preventDefault();
  });
  document.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    const x = e.clientX, y = e.clientY;
    if (drag.size) {  // the card ends where the pointer is, to the nearest quarter of the grid: 1 to 4 columns
      const n = Math.max(1, Math.min(4, Math.round((x - drag.c.getBoundingClientRect().left) / (grid.getBoundingClientRect().width / 4))));
      if (n !== width(drag.c)) setWidth(drag.c, n);
      return;
    }
    if (y < 48 || y > window.innerHeight - 48) window.scrollBy(0, y < 48 ? -24 : 24);
    const hit = cards().find(c => c !== drag.c && c.getClientRects().length && inside(c.getBoundingClientRect(), x, y));
    if (!hit) { drag.over = null; return; }
    if (hit === drag.over || Math.hypot(x - drag.x, y - drag.y) < 8) return;  // once per card entered, and not for a tremor
    drag.over = hit; drag.x = x; drag.y = y;
    if (hit.compareDocumentPosition(drag.c) & Node.DOCUMENT_POSITION_FOLLOWING) hit.before(drag.c); else hit.after(drag.c);
    hold();
  });
  function end(e, ok) {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    d.c.classList.remove("drag");
    if (ok) commit(d.c); else { restore(d.s); say(d.c); }
  }
  document.addEventListener("pointerup", e => end(e, true));
  document.addEventListener("pointercancel", e => end(e, false));
  document.addEventListener("keydown", e => {
    if (e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
    const k = e.key;
    if (drag) {  // a pointer drag in progress: Escape puts everything back, other keys wait
      if (k === "Escape") { restore(drag.s); drag.c.classList.remove("drag"); say(drag.c, "Cancelled"); drag = null; e.preventDefault(); }
      return;
    }
    if (!grab) {
      if (k === " " && e.target.matches("article.card[data-card]")) {
        grab = {c: e.target, s: snap()};
        grab.c.setAttribute("data-grab", "1");
        say(grab.c, "Grabbed. Arrows move, plus and minus resize, x hides or shows, space drops, escape cancels");
        e.preventDefault();
      }
      return;
    }
    const c = grab.c;
    if (k === "Escape") { restore(grab.s); grab = null; c.removeAttribute("data-grab"); c.focus({preventScroll: true}); commit(c, "Cancelled"); }
    else if (k === " " || k === "Enter") release("Dropped");
    else if (k === "ArrowLeft" || k === "ArrowUp") nudge(c, -1);
    else if (k === "ArrowRight" || k === "ArrowDown") nudge(c, 1);
    else if (k === "+" || k === "=") resize(c, 1);
    else if (k === "-" || k === "_") resize(c, -1);
    else if (k === "x") flip(c);
    else return;
    e.preventDefault();
  });
  document.addEventListener("focusout", e => {  // leaving a grabbed card drops it (a move gives the focus back at once)
    if (grab && e.target === grab.c) setTimeout(() => { if (grab && document.activeElement !== grab.c) release("Dropped"); }, 0);
  });

  for (const c of cards()) { c.tabIndex = 0; sync(c); }
})();
"""


def _inject(js):
    """The script with the allowlists, the card codes and the network door written into it: the same input gives the same text."""
    js = js.replace("@LOAD@", _LOAD)
    js = js.replace("@FRAG_TAGS@", " ".join(FRAG_TAGS))
    js = js.replace("@FRAG_ATTRS@", " ".join(FRAG_ATTRS))
    js = js.replace("@CODES@", " ".join("%s:%s" % p for p in CARD_CODES))
    return js


REFRESH_JS = _inject(_REFRESH)
KEYS_JS = _inject(_KEYS)
PREFS_JS = _inject(_PREFS)
BUILDER_JS = _inject(_BUILDER)

SCRIPTS = {"refresh": REFRESH_JS, "keys": KEYS_JS, "prefs": PREFS_JS, "builder": BUILDER_JS}
ASSET_TYPE = "text/javascript; charset=utf-8"


def sha256_b64(text):
    """The SHA-256 of the text's UTF-8 bytes in base64: what a CSP hash source carries."""
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def csp_source(js):
    """The script's hash as a CSP source ("'sha256-...'"), as the browser computes it over the inline script's text."""
    return "'sha256-%s'" % sha256_b64(js)


# name -> (the script's bytes, content type, SHA-256 in hex): served at /s/<name>.<sha8>.js, immutable (the name changes with the text)
ASSETS = {name: (js.encode("ascii"), ASSET_TYPE, hashlib.sha256(js.encode("ascii")).hexdigest()) for name, js in SCRIPTS.items()}


def asset_path(name):
    """The URL an asset is served at: the first 8 hex digits of its SHA-256 are in the name, so a changed script is a new URL."""
    return "/s/%s.%s.js" % (name, ASSETS[name][2][:8])
