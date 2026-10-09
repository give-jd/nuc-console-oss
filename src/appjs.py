"""nuc-console web view: the script of the live app (/app), as a Python string constant (the installers copy only src/*.py).

The app is one page that draws every screen from the data API (/api/v1, src/webapi.py) and keeps it up to date from its stream:
no reload, no poll, the page changes in place. Like the shell's scripts (src/webjs.py) it is first-party, a strict-mode IIFE with no
globals and no library, ASCII only, inlined into a page whose Content-Security-Policy lists its SHA-256; tests/jsrules.py (policy
"app") fixes what it may do. Unlike them it builds the page itself, so it is the one script that may create elements: with
createElement / createElementNS from a fixed list of tags and setAttribute from a fixed list of attributes, text only as text
(append of a string, textContent), never markup (no innerHTML, no DOMParser) and never a URL it did not check.

    APP_JS   the app (~40 KB): the router, the stream, the drawing of the components, the morph, the forms of the AI screen

THE DRAWING
===========

draw(node) is htmlview.html(node) in the browser: the same elements, attributes, classes and text for every component of src/ui.py
(tests/test_appjs.py compares the two in a headless Chrome, view by view, on the documents of tests/golden.py's frozen world). The
overview's cards and key figures are the shell's (htmlview.card_article, htmlview.kpi_tile). A change of htmlview.py is a change
here too.

THE PAGE (web.py app_page)
==========================

<script type="application/json" id="cfg">   {"refresh", "views", "layout": [[card, width]...], "order", "custom", "kpis": [...],
                                             "kpi_card": {...}, "kpi_view": {...}, "titles": {...}}: what the page was served with
<script type="application/json" id="doc">   the first documents ({view: document}), so that the first paint needs no request
<header class="topbar">     the shell's top bar: .status (the pill), nav.tabs a (aria-current), time.clock
<div id="kpis">              the key figures (the overview's, or the summary's on the other screens)
<div id="stale">             the banner of a stream that broke ("stale since HH:MM:SS")
<main id="app">              the screen: class "grid" (the overview's cards) or "view" (cpu, health, map, ai)
[data-pause]                 the pause toggle: the stream closes while paused

THE NETWORK
===========

    GET  /api/v1/<view>?<params>                     call(): when the stream cannot be opened (503: too many), every refresh interval
    GET  /api/v1/stream?view=<view>&view=summary...  stream(): Server-Sent Events, one document per change
    POST /ai/<action>, /telegram/<action>             call(url, form): the forms of the screens (CSRF token in their hidden fields), with
                                                      Accept: application/json; the answer is {"to": the address the page goes to next},
                                                      followed in the page when the app draws it (a question asked first: ?confirm=), never
                                                      a redirect; the stream brings the change

A link to "/?..." that the app draws (overview, cpu, health, map as a tree, ai, with the parameters those screens take) is followed in
the page (history.pushState to "/app?..."); any other link (settings, Telegram, the graph, a card in full) leaves the app for the shell.
Another screen starts at its top; the same screen with other parameters keeps the page where it was (a model selected, the prompt of an
answer shown), and brings what it opened into sight when it is off screen (#prompt, section.spec)."""
import base64
import hashlib

# The elements and attributes the app creates (draw: htmlview.html's; the frame: the shell's). Anything else throws.
HTML_TAGS = ("a", "article", "aside", "b", "br", "button", "code", "col", "colgroup", "dd", "details", "div", "dl", "dt", "figure", "form",
             "h2", "h3", "header", "input", "kbd", "li", "p", "pre", "section", "span", "strong", "summary", "table", "tbody", "td", "th", "thead", "tr",
             "ul")
SVG_TAGS = ("svg", "rect", "polyline")
ATTRS = ("action", "aria-current", "aria-disabled", "aria-hidden", "aria-label", "aria-sort", "autocomplete", "class", "colspan", "data-card",
         "data-depth", "data-k", "data-key", "data-kpi", "data-max-lines", "data-problem", "data-row", "data-state", "data-truncated", "disabled",
         "fill", "height", "href", "id", "maxlength", "method", "name", "open", "placeholder", "points", "preserveAspectRatio", "role", "scope",
         "title", "type", "value", "viewBox", "width", "x", "y")

_APP = r"""// nuc-console web view: the live app (src/appjs.py APP_JS). It draws the screen from the data API's documents and keeps it up to date from
// the stream: each new document is drawn and morphed into the page, so what did not change stays as it was (focus, open details, scroll).
// Links to the screens it draws are followed in the page; forms are posted in the background and the stream brings their effect.
// Classes it sets, for the server's CSS:
//   stale    the numbers on the page are old (the #stale banner says since when)
//   paused   the stream is closed with the [data-pause] toggle
//   busy     (on main) a screen is asked for and not drawn yet
//   chg      (on a key figure) its value just changed
(function main() {
  "use strict";
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const root = document.documentElement, app = document.getElementById("app"), kpiBox = document.getElementById("kpis");
  const banner = document.getElementById("stale"), cfgNode = document.getElementById("cfg"), docNode = document.getElementById("doc");
  if (!app || !kpiBox || !cfgNode) return;
  const live = !app.hasAttribute("data-static");
  const SVGNS = "http://www.w3.org/2000/svg";
  const HTML_TAGS = new Set("@HTML_TAGS@".split(" ")), SVG_TAGS = new Set("@SVG_TAGS@".split(" ")), ATTRS = new Set("@ATTRS@".split(" "));
  const SYM = {ok: "\u2714", warn: "!", err: "\u2716", down: "\u2716", unknown: "?", info: "\u00b7"};
  const TAG = {err: "\u2716 ERR", warn: "! WARN", info: "\u00b7 INFO"};
  const BADGE = {ok: "ok", warn: "warn", err: "err", accent: "accent", muted: ""};
  const RANK = {err: 0, down: 0, warn: 1, unknown: 2, ok: 3, info: 4};
  const VIEWS = new Set(["", "cpu", "health", "map", "ai"]);
  const KEEP = {"": [], cpu: ["sort", "sel"], health: ["period", "sel"], map: ["open", "shut", "all", "sel", "only"], ai: ["sel", "confirm", "prompt"]};
  const SAFE_HREF = /^(?:\/(?!\/)|[?#]|https?:\/\/)[^\s\\"'<>]*$/, SAFE_PATH = /^\/[a-z][a-z0-9\/-]*$/, CTRL = /[\u0000-\u001f\u007f-\u009f]/g;
  let cfg = {};
  try { cfg = JSON.parse(cfgNode.textContent || "{}"); } catch (err) { return; }
  const period = Math.max(1, Math.min(Number(cfg.refresh) || 2, 3600)) * 1000;

  // ---- the elements: only these tags and attributes, text only as text ------------------------------------------------------------
  function add(n, kids) {
    for (const k of kids) {
      if (Array.isArray(k)) add(n, k);
      else if (k !== null && k !== undefined && k !== "") n.append(k);
    }
    return n;
  }
  function dress(n, attrs, kids) {
    for (const name of Object.keys(attrs || {})) {
      const v = attrs[name];
      if (v === null || v === undefined || v === false) continue;
      if (!ATTRS.has(name)) throw new Error("attribute " + name);
      n.setAttribute(name, v === true ? "" : String(v));
    }
    return add(n, kids);
  }
  function el(tag, attrs, ...kids) {
    if (!HTML_TAGS.has(tag)) throw new Error("tag " + tag);
    return dress(document.createElement(tag), attrs, kids);
  }
  function sv(tag, attrs, ...kids) {
    if (!SVG_TAGS.has(tag)) throw new Error("tag " + tag);
    return dress(document.createElementNS(SVGNS, tag), attrs, kids);
  }
  const say = v => (v === null || v === undefined ? "None" : v === true ? "True" : v === false ? "False" : String(v));  // Python's str()
  const tx = v => say(v).replace(CTRL, "?");  // ui.safe(): no control character reaches the page
  const cls = (...names) => {  // htmlview._cls: lowercase, '_' as '-', anything but a-z 0-9 '-' as '-'
    const out = names.filter(n => n).map(n => String(n).toLowerCase().replace(/_/g, "-").replace(/[^a-z0-9-]/g, "-"));
    return out.length ? out.join(" ") : null;
  };
  const href = u => (u && SAFE_HREF.test(String(u)) ? String(u) : null);
  const kind = x => (x && typeof x === "object" && typeof x.type === "string" ? x.type : "");
  function fix(x, d) {  // Python's format(x, ".Nf"): toFixed rounds an exact tie up, Python to the even digit
    const s = x.toFixed(d), m = Math.pow(10, d), t = x * 2 * m;
    if (!(Number.isInteger(t) && t % 2 !== 0 && t / (2 * m) === x)) return s;
    const n = (t - 1) / 2;
    return ((n % 2 === 0 ? n : n + 1) / m).toFixed(d);
  }
  const g1 = x => String(Number(fix(x, 1)));  // format(round(x, 1), "g") for the values of a bar
  function trim(list) {  // the markup of a list of nodes with str.strip() applied to it: the text at both ends loses its blanks
    const out = list.flat(Infinity).filter(n => n !== null && n !== undefined && n !== "");
    while (out.length && typeof out[0] === "string" && !out[0].trim()) out.shift();
    while (out.length && typeof out[out.length - 1] === "string" && !out[out.length - 1].trim()) out.pop();
    if (out.length && typeof out[0] === "string") out[0] = out[0].replace(/^\s+/, "");
    if (out.length && typeof out[out.length - 1] === "string") out[out.length - 1] = out[out.length - 1].replace(/\s+$/, "");
    return out;
  }

  // ---- the components (htmlview.html) --------------------------------------------------------------------------------------------
  function span(s) {
    const names = cls(s.tone ? "t-" + s.tone : "", s.bold ? "b" : "", s.mono ? "mono" : "");
    const text = tx(s.full === null || s.full === undefined ? s.text : s.full);
    return names ? el("span", {class: names}, text) : text;
  }
  function bar(b, value) {
    const val = b.value_text && value ? el("span", {class: "n"}, tx(b.value_text)) : null;
    if (b.busy) {
      return [sv("svg", {class: cls("bar", "busy", b.tone ? "t-" + b.tone : ""), viewBox: "0 0 100 8", width: 100, height: 8, role: "progressbar",
        "aria-label": "in progress", preserveAspectRatio: "none"}, sv("rect", {class: "bg", x: 0, y: 0, width: 100, height: 8}),
        sv("rect", {class: "fg", x: 0, y: 0, width: 35, height: 8})), val];
    }
    if (b.frac === null || b.frac === undefined) return [el("span", {class: "st-unknown", role: "img", "aria-label": "unknown"}, "?")];
    return [sv("svg", {class: cls("bar", "st-" + b.state, b.tone ? "t-" + b.tone : ""), viewBox: "0 0 100 8", width: 100, height: 8, role: "img",
      "aria-label": fix(b.frac * 100, 0) + "%", preserveAspectRatio: "none"}, sv("rect", {class: "bg", x: 0, y: 0, width: 100, height: 8}),
      sv("rect", {class: "fg", x: 0, y: 0, width: g1(b.frac * 100), height: 8})), val];
  }
  function spark(sp) {
    const vals = sp.values || [];
    if (!vals.length) return sv("svg", {class: "spark", viewBox: "0 0 80 16", width: 80, height: 16, role: "img", "aria-label": "no data"});
    const top = Math.max(Math.max(...vals), sp.floor, 1e-9), step = 80 / Math.max(vals.length - 1, 1);
    const pts = vals.map((v, i) => fix(i * step, 1) + "," + fix(16 - 1 - v / top * (16 - 2), 1)).join(" ");
    return sv("svg", {class: "spark", viewBox: "0 0 80 16", width: 80, height: 16, role: "img", "aria-label": "trend"}, sv("polyline", {fill: "none", points: pts}));
  }
  function meter(m) {
    let x = 0;
    const rects = [], said = [];
    for (const [pct, part] of m.parts || []) {
      const w = Math.min(pct, 100 - x);
      if (w > 0) rects.push(sv("rect", {class: cls("m-" + part), x: fix(x, 1), y: 0, width: fix(w, 1), height: 8}));
      x += Math.max(w, 0);
      said.push(part + " " + fix(pct, 0) + "%");
    }
    return sv("svg", {class: "meter", viewBox: "0 0 100 8", width: 100, height: 8, role: "img", "aria-label": tx(said.join(", ") || "idle"),
      preserveAspectRatio: "none"}, sv("rect", {class: "bg", x: 0, y: 0, width: 100, height: 8}), rects);
  }
  function series(sr) {
    const vals = sr.values || [], width = Math.max(4 * vals.length - 1, 1), nums = vals.filter(v => v !== null), top = nums.length ? Math.max(...nums) : 0;
    const bars = [];
    vals.forEach((v, i) => {
      if (v === null) return;
      const h = top > 0 ? Math.max(1, v / top * (16 - 2)) : 1;
      bars.push(sv("rect", {x: 4 * i, y: fix(16 - h, 1), width: 3, height: fix(h, 1)}));
    });
    return sv("svg", {class: cls("series", sr.tone ? "t-" + sr.tone : ""), viewBox: "0 0 " + width + " 16", width: width, height: 16, role: "img",
      "aria-label": bars.length ? "trend" : "no data", preserveAspectRatio: "none"}, bars);
  }
  function inline(x) {
    switch (kind(x)) {
      case "Span": return [span(x)];
      case "Line": return (x.spans || []).map(inline);
      case "Bar": return bar(x, true);
      case "Spark": return [spark(x)];
      case "Meter": return [meter(x)];
      case "Series": return [series(x)];
      case "Badge": return [badge(x)];
      case "Action": return [action(x)];
    }
    return [el("span", {class: "st-unknown"}, "?")];
  }
  const wprio = c => { const p = c.wprio === null || c.wprio === undefined ? c.prio : c.wprio; return p ? "p" + p : ""; };
  function barline(line) {
    const parts = line.spans || [], at = parts.findIndex(x => kind(x) === "Bar");
    if (at < 0) return null;
    const b = parts[at], label = trim(parts.slice(0, at).map(inline));
    if (b.frac === null || b.frac === undefined) return [el("span", {class: "lb"}, label), inline(b)];
    const vt = (b.value_text || "").trim(), cut = vt.indexOf("  "), value = cut < 0 ? vt : vt.slice(0, cut), rest = cut < 0 ? "" : vt.slice(cut + 2);
    const extra = trim([tx(rest.trim()), " ", parts.slice(at + 1).map(inline)]);
    return [el("span", {class: "lb"}, label), bar(b, false), el("span", {class: "n"}, tx(value)), extra.length ? el("span", {class: "bx"}, extra) : null];
  }
  function th(c) {
    const link = href(c.href), label = link ? el("a", {href: link, "data-key": c.hkey || null}, tx(c.label)) : tx(c.label);
    return el("th", {class: cls(c.align !== "l" ? c.align : "", c.num ? "n" : "", wprio(c), c.sort ? "sorted" : ""),
      "aria-sort": {desc: "descending", asc: "ascending"}[c.sort] || null, scope: "col"}, label);
  }
  function table(t, notes) {
    const cols = t.cols || [], rows = t.rows || [], marks = new Map((t.groups || []).map(([label, i]) => [i, label]));
    const ends = Array.from(marks.keys()).sort((a, b) => a - b).concat([rows.length]), body = [];
    rows.forEach((r, i) => {
      if (marks.has(i)) {
        const count = t.titled ? [" ", el("span", {class: "n"}, "(" + (ends[ends.indexOf(i) + 1] - i) + ")")] : null;
        body.push(el("tr", {class: "grp"}, el("th", {colspan: cols.length, scope: "rowgroup"}, tx(marks.get(i)), count)));
      }
      const link = href(r.href), cells = [];
      for (let j = 0; j < Math.min(cols.length, (r.cells || []).length); j++) {
        const c = cols[j], inner = inline(r.cells[j]);
        cells.push(el("td", {class: cls(c.align !== "l" ? c.align : "", c.num ? "n" : "", wprio(c))}, j === 0 && link ? el("a", {href: link}, inner) : inner));
      }
      body.push(el("tr", {class: cls(r.tone ? "t-" + r.tone : ""), "data-key": r.key === null || r.key === undefined ? null : tx(r.key),
        "data-row": !!link, "aria-current": r.tone === "sel" ? "true" : null}, cells));
      if (notes && notes[i] && notes[i].length) body.push(el("tr", {class: "sub"}, el("td", {colspan: cols.length}, notes[i].map(draw))));
    });
    return el("table", {class: "tbl"}, el("colgroup", {}, cols.map(c => el("col", {class: cls("c-" + c.key)}))),
      el("thead", {}, el("tr", {}, cols.map(th))), el("tbody", {}, body));
  }
  function problem(p) {
    const chip = p.id ? [" ", el("code", {class: "pid", title: tx(p.title)}, tx(p.id))] : null;
    const why = p.id && p.why ? el("span", {class: "d why"}, tx(p.why)) : null;
    const how = [p.fix ? el("span", {class: "d how"}, "fix: ", el("code", {class: "cmd"}, tx(p.fix))) : null,
      p.accept ? el("span", {class: "d accept"}, "accept if known: ", el("code", {class: "cmd"}, tx(p.accept))) : null].filter(n => n);
    const fx = how.length ? el("details", {class: "fix", "data-k": "fix-" + tx(p.id)}, el("summary", {}, "fix"), how) : null;
    return el("div", {class: cls("msg", "prob", "lv-" + p.level), "data-problem": tx(p.id)}, el("span", {class: "sym"}, SYM[p.level]),
      el("span", {class: "pr"}, tx(p.text), chip), why, fx);
  }
  function accepted(a) {
    return el("div", {class: "msg known lv-info", "data-problem": tx(a.id)}, el("span", {class: "sym"}, SYM.info),
      el("span", {class: "pr"}, tx(a.text) + " ", el("code", {class: "pid"}, tx(a.id))),
      el("span", {class: "d why"}, "reason: \u201c" + tx(a.reason) + "\u201d", a.when ? " \u00b7 accepted " + tx(a.when) : null,
        a.undo ? [" \u00b7 undo: ", el("code", {class: "cmd"}, tx(a.undo))] : null));
  }
  function seg(label, options) {
    return el("div", {class: "seg", role: "group", "aria-label": say(label)}, options.map(([text, key, chosen, to]) => {
      if (chosen) return el("span", {"aria-current": "true"}, say(text));
      const link = href(to);
      if (link === null) return el("span", {"aria-disabled": "true"}, say(text));
      return el("a", {href: link, "data-key": key ? tx(key) : null, title: key ? "key " + tx(key) : null}, say(text));
    }));
  }
  function legend(lg) {
    return el("ul", {class: "legend lgd"}, (lg.items || []).map(([glyph, word, tone]) => el("li", {}, el("span", {class: cls(tone ? "t-" + tone : "")}, tx(glyph)), " " + tx(word))));
  }
  function branch(b) {
    const d = Math.max(0, Math.min(b.depth, 12)), k = tx(b.key), link = href(b.href), tog = href(b.mark_href);
    const mark = tog ? el("a", {class: "tg", href: tog, title: tx(b.tip), "data-k": "t-" + k}, inline(b.mark)) : el("span", {class: "tg d", title: tx(b.tip)}, inline(b.mark));
    const inner = [el("span", {class: "sym"}, SYM[b.state]), " ", inline(b.body)], cur = b.cursor ? "true" : null;
    const row = link ? el("a", {class: "oa", href: link, "data-row": true, "data-k": "o-" + k, "aria-current": cur}, inner) : el("span", {class: "oa", "aria-current": cur}, inner);
    return [el("li", {class: cls("ob", "st-" + b.state, b.cursor ? "sel" : ""), "data-depth": d}, mark, row),
      b.after !== null && b.after !== undefined ? el("li", {class: "ob-d"}, draw(b.after)) : null];
  }
  function pane(p) {
    const facts = p.facts && p.facts.length ? [el("dt", {}, "facts"), el("dd", {}, el("ul", {class: "facts"}, p.facts.map(([k, v]) => el("li", {}, el("span", {class: "k"}, tx(k)), " ", el("b", {}, tx(v))))))] : null;
    return el("dl", {class: cls("pane", "lv-" + p.level)}, el("dt", {}, "what"), el("dd", {}, tx(p.what)), facts, el("dt", {}, "fix"), el("dd", {}, tx(p.fix)));
  }
  function finding(f) {
    const key = "f-" + tx(f.id);
    const summary = el("summary", {}, el("span", {class: "tag " + tx(f.level)}, TAG[f.level]), " ", el("strong", {class: "ft"}, tx(f.title)), " ", el("span", {class: "fx"}, tx(f.text)));
    return el("div", {class: cls("fd", "lv-" + f.level), "data-k": key}, el("details", {"data-k": key + "-d", open: !!f.open}, summary, f.detail ? pane(f.detail) : null));
  }
  function advice(a) {
    if (!(a.head || (a.paras || []).length || (a.notes || []).length)) return null;
    const paras = (a.paras || []).map(p => el("p", {}, p.map((x, i) => (i ? [el("br", {}), tx(x)] : tx(x)))));
    return el("div", {class: cls("advice", a.kind !== "advice" ? "advice-" + a.kind : "")}, el("p", {class: "advice-head"}, tx(a.head)), paras,
      (a.notes || []).map(([k, t]) => el("p", {class: cls("advice-" + k)}, tx(t))));
  }
  function timeline(tl) {
    let x = 0;
    const rects = (tl.parts || []).map(([name, v]) => {
      const w = Math.min(Math.max(v / tl.total * 100, 0), 100 - x), r = sv("rect", {class: cls("tp", "tp-" + name), x: fix(x, 1), y: 0, width: fix(w, 1), height: 8});
      x += w;
      return r;
    });
    return el("figure", {class: "timeline"}, sv("svg", {class: "tl", viewBox: "0 0 100 8", width: 100, height: 8, role: "img", "aria-label": "boot timeline",
      preserveAspectRatio: "none"}, rects), el("ul", {class: "legend"}, (tl.parts || []).map(([name, v]) => el("li", {class: cls("tp-" + name)},
      el("span", {class: "sw"}), " " + tx(name) + " ", el("span", {class: "n"}, fix(v, 1) + "s")))));
  }
  function badge(b) { return el("span", {class: cls("tag", BADGE[b.tone])}, tx(b.text)); }
  function action(a) {
    if (!SAFE_PATH.test(String(a.action))) return null;
    const ask = a.ask ? [el("input", {class: "q", type: "text", name: tx(a.ask[0]), maxlength: a.ask[2], placeholder: tx(a.ask[1]), "aria-label": tx(a.ask[1]),
      autocomplete: "off"}), " "] : null;
    return el("form", {class: "f", method: "post", action: a.action}, (a.fields || []).map(([k, v]) => el("input", {type: "hidden", name: tx(k), value: tx(v)})), ask,
      el("button", {class: cls("bt", a.tone ? "bt-" + a.tone : ""), type: "submit", title: a.title ? tx(a.title) : null, "data-key": a.key ? tx(a.key) : null,
        disabled: !!a.disabled}, tx(a.label)));
  }
  function spec(sp) {
    const rows = (sp.items || []).map(([label, text, tone, whole]) => [el("dt", {}, tx(label)),
      el("dd", {}, whole ? el("code", {class: "cmd"}, tx(text)) : tone ? el("span", {class: cls("t-" + tone)}, tx(text)) : tx(text))]);
    const acts = sp.actions && sp.actions.length ? [el("dt", {}, "do"), el("dd", {class: "do"}, sp.actions.map(action))] : null;
    return el("section", {class: "spec"}, el("h3", {class: "sub"}, "DETAILS"), el("p", {class: "spec-t"}, tx(sp.title)), el("dl", {class: "spec-dl"}, rows, acts));
  }
  function qa(q) {
    const wait = q.pending && !q.answer ? el("p", {class: "pend"}, tx(q.wait)) : null, link = href(q.prompt_href);
    const foot = [q.note ? el("span", {class: "d"}, tx(q.note)) : null, q.note && link ? " \u00b7 " : null, link ? el("a", {href: link}, "the prompt it was sent") : null];
    return el("div", {class: "qa", "data-k": q.key ? "qa-" + tx(q.key) : null}, el("p", {class: "q"}, el("strong", {}, q.kind === "advice" ? "advice:" : "you:"), " " + tx(q.q)),
      q.answer ? draw(q.answer) : null, wait, q.note || link ? el("p", {class: "qa-f"}, foot) : null);
  }
  function prompt(p) {  // htmlview._prompt_html: every message whole, its line breaks kept
    const msgs = p.messages || [], n = t => Array.from(say(t)).length, close = href(p.close_href);
    const block = t => say(t).split("\n").map(x => x.replace(CTRL, "?")).join("\n");
    return el("section", {class: "prompt", id: "prompt"}, el("h3", {class: "sub"}, "PROMPT ", el("span", {class: "note"}, "the whole request the model was sent, as it read it: "
      + msgs.length + " parts, " + msgs.reduce((a, [, t]) => a + n(t), 0) + " characters")), el("p", {class: "pm-q"}, el("span", {}, "for: " + tx(p.title)),
      close ? el("a", {class: "pm-x", href: close}, "close \u2715") : null), msgs.map(([role, t]) => el("div", {class: "pm"}, el("p", {class: "pm-r"},
      el("span", {class: "tag"}, tx(role)), " ", el("span", {class: "d"}, n(t) + " characters")), el("pre", {class: "pm-t"}, block(t)))));
  }
  function title(t) {
    const segs = (t.seg ? [t.seg] : []).concat(t.segs || []);
    return el("header", {class: "st"}, el("h2", {}, tx(t.label)), el("p", {class: "bits"}, (t.parts || []).map(p => el("span", {class: "bit"}, inline(p)))),
      t.legend ? legend(t.legend) : null, segs.map(sg => seg(sg.label, sg.options || [])));
  }
  function kpi(k) {
    return el("div", {class: cls("kpi", "st-" + k.state), "data-kpi": tx(k.id), title: tx(k.hint)}, el("span", {class: "sym"}, tx(k.symbol)),
      el("span", {class: "lbl"}, tx(k.label)), el("span", {class: "n"}, tx(k.value)), el("span", {class: "unit"}, tx(k.unit)), k.spark ? spark(k.spark) : null);
  }
  function props(p) {
    const close = href(p.close);
    return el("aside", {class: "props"}, el("div", {class: "ph"}, el("h3", {class: "sub"}, tx(p.title)), close ? el("a", {href: close}, "close \u2715") : null),
      el("dl", {}, (p.items || []).map(([raw, v, lv], i) => [el("dt", {class: cls(String(raw).startsWith("  ") ? "in" : "")}, tx(String(raw).trim())),
        el("dd", {class: cls(lv ? "lv-" + lv : "", i === 0 ? "top" : "")}, tx(v))])));
  }
  function draw(x) {
    const n = kind(x);
    switch (n) {
      case "Raw": return el("pre", {class: "raw"}, (x.lines || []).join("\n"));
      case "Card": return el("article", {class: cls("card", "st-" + x.state, "s" + x.size), id: "card-" + tx(x.id), "data-card": tx(x.id), "data-truncated": !!x.truncated},
        el("header", {}, el("h2", {}, tx(x.title)), x.note ? el("span", {class: "note"}, tx(x.note)) : null, el("span", {class: cls("state", "st-" + x.state)}, SYM[x.state] + " " + x.state)),
        (x.body || []).map(draw));
      case "Line": if (!(x.spans || []).length) return null; if (barline(x)) return el("p", {class: "ln bl"}, barline(x)); return el("p", {class: "ln"}, inline(x));
      case "Span": case "Bar": case "Spark": case "Meter": return el("p", {class: "ln"}, inline(x));
      case "Problem": return problem(x);
      case "Accepted": return accepted(x);
      case "Msg": case "Notice": case "RichMsg":
        return el("p", {class: cls("msg", n === "Notice" ? "notice" : "", "lv-" + x.level), role: n === "Notice" ? "status" : null}, el("span", {class: "sym"}, SYM[x.level]),
          " ", n === "RichMsg" ? inline(x.rich) : tx(x.text));
      case "Hint": return el("p", {class: "hint d"}, tx(x.label) + ": ", el("code", {class: "cmd"}, tx(x.cmd)));
      case "KV": return el("dl", {class: "kv"}, (x.pairs || []).map(([k, v]) => [el("dt", {}, tx(k)), el("dd", {}, inline(v))]));
      case "Table": return table(x, null);
      case "NoteTable": return table({cols: x.cols, "rows": x.rows, groups: null, titled: false}, x.notes || []);
      case "Wrap": return el("ul", {class: x.flat ? "wrap flat" : "wrap", "data-max-lines": x.max_lines ? Math.trunc(x.max_lines) : null},
        x.lead !== null && x.lead !== undefined ? el("li", {class: "lead"}, inline(x.lead)) : null, (x.items || []).map(i => el("li", {}, inline(i))));
      case "More": return el("details", {class: "more"}, el("summary", {}, "\u2026 +" + x.n + " " + tx(x.what)), href(x.href) ? el("p", {}, el("a", {href: href(x.href)}, "show all")) : null);
      case "Group": return el("section", {class: "grp"}, x.title ? el("h3", {}, tx(x.title)) : null, (x.children || []).map(draw));
      case "Pill": return el("span", {class: cls("pill", "st-" + x.state)}, SYM[x.state] + " " + tx(x.text));
      case "Kpi": return kpi(x);
      case "Tree": return el("ul", {class: "tree"}, (x.rows || []).map(([d, item, st]) => el("li", {class: cls("st-" + st), "data-depth": Math.trunc(d)}, el("span", {class: "sym"}, SYM[st]), " ", inline(item))));
      case "Details": return el("details", {class: "dt", open: !!x.open}, el("summary", {}, inline(x.summary)), (x.body || []).map(draw));
      case "Badge": return el("p", {class: "ln"}, badge(x));
      case "Action": return action(x);
      case "Controls": return el("div", {class: "controls"}, (x.items || []).map(i => el("div", {class: "ci"}, inline(i))));
      case "Question": return el("div", {class: "ask", role: "group", "aria-label": "question"}, el("p", {}, tx(x.text)), el("div", {class: "ask-b"},
        x.yes ? action(x.yes) : null, href(x.no_href) ? el("a", {class: "btn", href: href(x.no_href), "data-key": "n"}, "No") : null));
      case "Spec": return spec(x);
      case "Qa": return qa(x);
      case "Prompt": return prompt(x);
      case "Log": return el("div", {class: "log", role: "log", "aria-label": tx(x.label)}, el("div", {class: "log-in"}, (x.children || []).map(draw)));
      case "Title": return title(x);
      case "Seg": return seg(x.label, x.options || []);
      case "Legend": return legend(x);
      case "Outline": return el("ul", {class: "ol"}, (x.rows || []).map(branch));
      case "Props": return props(x);
      case "Series": return series(x);
      case "Cols": return el("div", {class: "cols"}, (x.children || []).map(c => el("div", {class: "col"}, draw(c))));
      case "Pane": return pane(x);
      case "Finding": return finding(x);
      case "Advice": return advice(x);
      case "Head": return el("h3", {class: "sub"}, tx(x.title), x.note ? [" ", el("span", {class: "note"}, tx(x.note))] : null);
      case "Indent": return el("div", {class: "ind"}, (x.children || []).map(draw));
      case "Grid": return el("ul", {class: "grid"}, (x.items || []).map(i => (kind(i) === "Line" && barline(i) ? el("li", {class: "bl"}, barline(i)) : el("li", {}, inline(i)))));
      case "Timeline": return timeline(x);
      case "Flow": return el("p", {class: "flow"}, x.lead !== null && x.lead !== undefined ? [el("span", {class: "lead"}, inline(x.lead)), " "] : null,
        (x.items || []).map(i => el("span", {class: "fi"}, inline(i))));
      case "Cap": return (x.children || []).map(draw);
      case "Split": return el("div", {class: "split"}, el("div", {class: "sp-l"}, (x.left || []).map(draw)), el("div", {class: "sp-r"}, (x.right || []).map(draw)));
      case "Only": return x.surface === "web" ? (x.children || []).map(draw) : null;
      case "Tiles": return el("div", {class: "kpis tiles"}, (x.items || []).map(draw));
    }
    return el("span", {class: "st-unknown"}, "?");
  }

  // ---- the shell's frame: key figures and cards (htmlview.kpi_tile, htmlview.card_article) ---------------------------------------------
  function sparkTile(values) {
    const vals = (values || []).map(Number).slice(-60);
    if (vals.length < 2) return null;
    const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
    const pts = vals.map((v, i) => fix(i * 100 / (vals.length - 1), 1) + "," + fix(22 - (v - lo) * 20 / span, 1)).join(" ");
    return sv("svg", {class: "spk", viewBox: "0 0 100 24", preserveAspectRatio: "none", "aria-hidden": "true"}, sv("polyline", {points: pts}));
  }
  function tile(k, to) {
    const sym = k.state === "unknown" ? null : el("span", {class: "sym s-" + say(k.state), "aria-hidden": "true"}, SYM[k.state] || "?");
    const sp = k.spark ? sparkTile(k.spark.values) : null;
    const value = el("span", {class: "kv"}, sym, say(k.value), k.unit ? el("span", {class: "u"}, say(k.unit)) : null);
    return el(to ? "a" : "div", {class: "kpi st-" + say(k.state), role: "listitem", "data-state": say(k.state), href: to || null}, el("span", {class: "kl"}, say(k.label)),
      sp ? el("span", {class: "kline"}, value, sp) : value, k.hint ? el("span", {class: "ks"}, say(k.hint)) : null);
  }
  function badgeOf(view, b) {  // what a tab says (web.py shell_tabs): the Map's problems, the Health findings, the AI
    const v = b[view];
    if (view === "map" && v) return String(v);
    if (view === "health" && Array.isArray(v) && (v[0] || v[1])) {
      return [v[0] ? el("span", {class: "s-err"}, "\u2716" + v[0]) : null, v[0] && v[1] ? " " : null, v[1] ? el("span", {class: "s-warn"}, "!" + v[1]) : null];
    }
    if (view === "ai" && v) return v === "on" ? [el("span", {class: "s-ok"}, "\u25cf"), " on"] : v === "down" ? [el("span", {class: "s-err"}, "\u2716"), " down"] : el("span", {class: "s-unknown"}, "?");
    return null;
  }
  function tabs(b) {  // htmlview.tab, for every screen: the app's own links
    return (cfg.tabs || []).map(([key, view, label]) => {
      const v = view === "overview" ? "" : view, said = badgeOf(view, b || {});
      return el("a", {href: v ? "/app?view=" + v : "/app", "data-key": key, "aria-current": route && route.view === v ? "page" : null}, el("kbd", {}, say(key)), say(label),
        said ? [" ", el("span", {class: "badge"}, said)] : null);
    });
  }
  function chip(state) { return el("span", {class: "chip st-" + say(state)}, (SYM[state] || "?") + " " + say(state)); }
  function card(c, size) {
    return el("article", {class: "card s" + size + " st-" + say(c.state), "data-card": say(c.id), id: "c-" + say(c.id), "data-state": say(c.state)},
      el("div", {class: "ch"}, el("h3", {}, say(c.title)), c.note ? el("span", {class: "note"}, say(c.note)) : null, chip(c.state)),
      el("div", {class: "cb"}, (c.body || []).map(draw)));
  }

  // ---- the screens --------------------------------------------------------------------------------------------------------------
  const docs = {}, docFor = {};  // the documents known, and the parameters of the screen each was drawn for
  let route = parse(location.search), paused = false, reveal = "";
  function parse(search) {  // "/app?view=cpu&sort=mem" -> {view, params}: the parameters that screen takes, nothing else
    const q = new URLSearchParams(search || ""), view = q.get("view") || "";
    const params = new URLSearchParams();
    if (!VIEWS.has(view)) return null;
    for (const k of KEEP[view]) if (q.get(k) !== null && q.get(k) !== "") params.set(k, q.get(k));
    return {view: view, params: params};
  }
  const name = r => r.view || "overview";
  const stay = new URLSearchParams();  // what the page was opened with and every address of the app keeps: ?ui= (this URL's preferences), live=0
  for (const k of ["ui", "live"]) { const v = new URLSearchParams(location.search).get(k); if (v) stay.set(k, v); }
  const appUrl = r => "/app" + ((s => (s ? "?" + s : ""))([r.view ? "view=" + r.view : "", r.params.toString(), stay.toString()].filter(x => x).join("&")));
  const streamUrl = r => "/api/v1/stream?view=" + name(r) + (r.view ? "&view=summary" : "") + (r.params.toString() ? "&" + r.params.toString() : "");
  const docUrl = (r, v) => "/api/v1/" + v + (v !== "summary" && r.params.toString() ? "?" + r.params.toString() : "");
  function kpiLink(id) {
    if (cfg.kpi_view && cfg.kpi_view[id] && (cfg.views || []).indexOf(cfg.kpi_view[id]) >= 0) return "/app?view=" + cfg.kpi_view[id];
    const c = cfg.kpi_card && cfg.kpi_card[id];
    if (!c || !(cfg.layout || []).some(([cid]) => cid === c)) return null;
    return (route && route.view ? "/app" : "") + "#c-" + c;
  }
  function kpis(doc) {
    const want = cfg.kpis || [], by = new Map((doc.kpis || []).map(k => [k.id, k]));
    const tiles = want.filter(id => by.has(id)).map(id => tile(by.get(id), kpiLink(id)));
    const note = doc.stale ?
      el("div", {class: "stale-banner", role: "status"}, el("span", {class: "sym"}, "!"), " ", el("b", {}, "some of the data is old or missing: a collector is not running?"),
        " ", el("span", {class: "sm"}, "restart it: ", el("code", {class: "cmd"}, say(doc.restart || "")))) : null;
    return [note, tiles.length ? el("div", {class: "kpis", role: "list", "aria-label": "Key figures"}, tiles) : null];  // kpis = none: no row, no space
  }
  function overview(doc) {
    const by = new Map((doc.cards || []).map(c => [c.id, c]));
    let list = (cfg.layout || []).filter(([cid]) => by.has(cid)).map(([cid, w]) => [by.get(cid), w]);
    if (cfg.order === "severity" && !cfg.custom) list = list.map((x, i) => [x, i]).sort((a, b) => ((RANK[a[0][0].state] ?? 3) - (RANK[b[0][0].state] ?? 3)) || a[1] - b[1]).map(x => x[0]);
    return list.map(([c, w]) => card(c, w));
  }
  const BOX = {cpu: "scr scr-cpu", health: "hv", map: "scr mapv", ai: "scr av"};
  function screen(doc, view) { return [el("div", {class: BOX[view]}, (doc.nodes || []).map(draw))]; }

  // ---- the morph: the page becomes the fresh drawing, node by node; what is the same stays the same node -------------------------------
  const opened = new Map();  // <details data-k> the reader opened or closed by hand: data-k -> open
  const keyOf = n => (n.nodeType === 1 ? n.getAttribute("data-k") || n.getAttribute("data-card") || n.getAttribute("data-problem") : null);
  function morph(old, fresh) {
    if (old.nodeType !== fresh.nodeType || old.nodeName !== fresh.nodeName || keyOf(old) !== keyOf(fresh)) { old.replaceWith(fresh); return fresh; }
    if (old.nodeType !== 1) { if (old.nodeValue !== fresh.nodeValue) old.nodeValue = fresh.nodeValue; return old; }
    const held = old.localName === "details" && opened.has(keyOf(old)), typing = old.localName === "input" && old.type === "text" && (old === document.activeElement || old.value);
    for (const a of Array.from(old.attributes)) if (!fresh.hasAttribute(a.name) && !(held && a.name === "open")) old.removeAttribute(a.name);
    for (const a of Array.from(fresh.attributes)) {
      if (held && a.name === "open") continue;
      if (old.getAttribute(a.name) !== a.value) old.setAttribute(a.name, a.value);
    }
    if (held && old.open !== opened.get(keyOf(old))) old.open = opened.get(keyOf(old));
    if (!typing) kids(old, fresh);
    return old;
  }
  function kids(old, fresh) {
    const next = Array.from(fresh.childNodes), byKey = new Map();
    for (const n of Array.from(old.childNodes)) { const k = keyOf(n); if (k) byKey.set(k, n); }
    let cur = old.firstChild;
    for (const n of next) {
      const k = keyOf(n);
      let match = null;
      if (k && byKey.has(k)) { match = byKey.get(k); byKey.delete(k); }
      else if (!k && cur && !keyOf(cur) && cur.nodeType === n.nodeType && cur.nodeName === n.nodeName) match = cur;
      if (match) {
        if (match !== cur) { if (cur) cur.before(match); else old.append(match); }
        cur = morph(match, n).nextSibling;
      } else if (cur) cur.before(n);
      else old.append(n);
    }
    while (cur) { const after = cur.nextSibling; cur.replaceWith(); cur = after; }
  }
  function into(box, nodes) {  // box's children become nodes, morphed
    const fresh = el("div", {}, nodes);
    kids(box, fresh);
  }
  function flash(before) {  // the key figures whose value changed: class chg for a moment
    for (const t of kpiBox.querySelectorAll("[data-state]")) {
      const k = t.querySelector(".kv"), was = before.get(t.getAttribute("href") || t.querySelector(".kl").textContent);
      if (k && was !== undefined && was !== k.textContent) { t.classList.add("chg"); setTimeout(() => t.classList.remove("chg"), 1200); }
    }
  }
  function fit() {  // the overview's dense packing with the measured heights (REFRESH_JS's): each card spans the rows its height needs
    if (app.getAttribute("class") !== "grid") return;
    const g = getComputedStyle(app);
    if (!/dense/.test(g.gridAutoFlow)) return;
    const hit = /([0-9.]+)px/.exec(g.gridAutoRows), unit = hit ? Number(hit[1]) : parseFloat(g.fontSize) * 0.5, todo = [];
    if (!(unit > 1)) return;
    for (const c of app.querySelectorAll("article.card[data-card]")) {
      const names = (c.getAttribute("class") || "").split(/\s+/).filter(n => n), keep = names.filter(n => !/^r[0-9]+$/.test(n));
      const h = c.getBoundingClientRect().height + (parseFloat(getComputedStyle(c).marginBottom) || 0);
      const want = Math.min(200, Math.ceil(h / unit - 0.05)), next = want > 1 ? keep.concat("r" + want) : keep;
      if (h > 0 && next.join(" ") !== names.join(" ")) todo.push([c, next]);
    }
    for (const [c, next] of todo) c.setAttribute("class", next.join(" "));
  }
  function paint() {  // the page as the documents say: the key figures, the status, the screen
    if (!route) return;
    const view = name(route), doc = docs[view], top = route.view ? docs.summary : docs.overview;
    if (top) {
      const before = new Map(Array.from(kpiBox.querySelectorAll("[data-state]")).map(t => [t.getAttribute("href") || t.querySelector(".kl").textContent, (t.querySelector(".kv") || t).textContent]));
      into(kpiBox, kpis(top));
      flash(before);
      const pill = document.querySelector(".status");
      if (pill && top.status) { pill.setAttribute("class", "status " + say(top.status.state)); pill.textContent = say(top.status.text); }
      const host = document.querySelector(".host"), nav = document.querySelector("nav.tabs");
      if (host && top.host) host.textContent = say(top.host);
      if (nav) into(nav, tabs(top.badges));
      document.title = say(top.host || "") + " \u00b7 " + (route.view || "overview") + " \u00b7 nuc-console";
    }
    if (!doc) { app.classList.add("busy"); return; }
    app.classList.remove("busy");
    const grid = view === "overview";
    if (app.getAttribute("class") !== (grid ? "grid" : "view")) app.setAttribute("class", grid ? "grid" : "view");
    into(app, grid ? overview(doc) : screen(doc, view));
    const shown = reveal === "prompt" ? document.getElementById("prompt") : reveal ? app.querySelector("section.spec") : null;
    reveal = "";
    if (shown) {  // what the click opened, in sight: whole when it fits, else from its top
      const box = shown.getBoundingClientRect(), room = window.innerHeight;
      if (box.top < 0 || box.bottom > room) shown.scrollIntoView({block: box.height > room ? "start" : "nearest"});
    }
    const clock = document.querySelector("time.clock"), upd = document.querySelector("footer .upd");
    if (doc.at) {
      const d = new Date(doc.at * 1000), p = n => (n < 10 ? "0" : "") + n, hms = p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
      if (clock) clock.textContent = hms;
      if (upd) upd.textContent = "updated " + hms;
    }
    fit();
  }

  // ---- the network: the stream, the documents, the forms ---------------------------------------------------------------------------
  const call = (url, form) => {  // the only door to the network: this server's data API, and the forms of its screens
    if (typeof url !== "string" || !(form ? /^\/(ai|telegram)\/[a-z-]+$/.test(url) : url.startsWith("/api/v1/"))) return Promise.reject(new Error("refused"));
    return fetch(url, form ? {method: "POST", body: form, credentials: "same-origin", cache: "no-store", redirect: "manual", headers: {"Accept": "application/json"}} : {credentials: "same-origin", cache: "no-store", redirect: "error"});
  };
  const stream = url => {  // the one stream: this server's data API
    if (typeof url !== "string" || !url.startsWith("/api/v1/stream?")) throw new Error("refused");
    return new EventSource(url);
  };
  let source = null, sourceUrl = "", timer = 0, lastOk = Date.now(), quiet = 0;
  const pad = n => (n < 10 ? "0" : "") + n;
  const clockOf = ms => { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
  function show(text) {
    root.classList.toggle("stale", !!text);
    if (banner) { banner.textContent = text; banner.hidden = !text; }
  }
  function take(doc) {
    if (!doc || typeof doc !== "object" || typeof doc.view !== "string") return;
    docs[doc.view] = doc;
    docFor[doc.view] = route ? route.params.toString() : "";
    lastOk = Date.now();
    clearTimeout(quiet);
    show("");
    paint();
  }
  function close() { if (source) source.close(); source = null; sourceUrl = ""; clearTimeout(timer); }
  function listen() {  // the stream of the screen in view (and of the summary): reopened when the screen or its parameters change
    if (!route || paused || document.hidden) { close(); return; }
    if (!live) { close(); poll(false); return; }  // a snapshot (live=0): the documents once, no stream
    const url = streamUrl(route);
    if (source && sourceUrl === url) return;
    close();
    sourceUrl = url;
    try { source = stream(url); } catch (err) { source = null; poll(); return; }
    const mine = source;
    mine.addEventListener("message", e => { if (mine === source) { try { take(JSON.parse(e.data)); } catch (err) { /* not a document */ } } });
    mine.addEventListener("open", () => { if (mine === source) { clearTimeout(quiet); show(""); } });
    mine.addEventListener("error", () => {
      if (mine !== source) return;
      if (mine.readyState === 2) { source = null; poll(); return; }  // refused (too many streams, gone): ask for the documents instead
      clearTimeout(quiet);
      quiet = setTimeout(() => show("stale since " + clockOf(lastOk)), 10000);  // it reconnects by itself: old data only if it takes long
    });
  }
  async function poll(again) {  // the documents (every refresh interval while the stream cannot be had, unless again is false)
    clearTimeout(timer);
    if (!route || paused || document.hidden || source) return;
    const want = route.view ? [name(route), "summary"] : ["overview"];
    try {
      for (const v of want) {
        const r = await call(docUrl(route, v));
        if (r.status === 401) { show("session expired"); return; }
        if (r.status !== 200) throw new Error("not a document");
        take(await r.json());
      }
    } catch (err) { show("stale since " + clockOf(lastOk)); }
    if (again !== false) timer = setTimeout(() => { if (!source) listen(); }, period);
  }
  function go(r, push, hash) {  // a screen of the app: drawn from what is known, then from its stream. Another screen starts at its top; the same
    // screen with other parameters (a model selected, a question asked first) stays where it was, and what it opened is brought into sight
    const moved = !route || route.view !== r.view;
    route = r;
    reveal = hash === "prompt" ? "prompt" : !moved && r.params.get("sel") ? "spec" : "";
    if (push) history.pushState(null, "", appUrl(r));
    for (const k of Object.keys(docs)) if (k !== "summary" && k !== "overview" && (k !== name(r) || docFor[k] !== r.params.toString())) delete docs[k];
    paint();
    listen();
    if (push && moved) window.scrollTo(0, 0);
  }
  function follow(to) {  // an address of a screen the app draws ("/?..." or "/app?..."): drawn in the page -> true; anything else -> false
    const [path, hash] = String(to).split("#");
    if (!(path.startsWith("/?") || path.startsWith("/app?") || path === "/app")) return false;
    const q = new URLSearchParams(path.replace(/^\/(app)?\?/, "").replace(/^\/app$/, "")), view = q.get("view") || "";
    for (const k of q.keys()) if (k !== "view" && KEEP[view] && KEEP[view].indexOf(k) < 0 && !(k === "period" && q.get(k) === "0") && q.get(k) !== "") return false;  // a parameter only the shell knows
    const r = parse(q.toString());
    if (!r) return false;
    if (appUrl(r) !== appUrl(route) || hash) go(r, true, hash);
    return true;
  }

  document.addEventListener("click", e => {
    const s = e.target.closest("summary"), d = s && s.parentElement, k = d && d.getAttribute("data-k");
    if (k) opened.set(k, !d.open);
    if (e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const p = e.target.closest("[data-pause]");
    if (p) {
      e.preventDefault();
      paused = !paused;
      root.classList.toggle("paused", paused);
      for (const t of document.querySelectorAll("[data-pause]")) t.setAttribute("aria-pressed", String(paused));
      if (paused) close(); else listen();
      return;
    }
    const a = e.target.closest("a[href]");
    if (a && follow(a.getAttribute("href") || "")) e.preventDefault();
  }, true);
  document.addEventListener("submit", e => {  // a form of a screen: posted in the background, its effect comes with the stream
    const f = e.target.closest("form"), to = f ? f.getAttribute("action") || "" : "";
    if (!f || !app.contains(f) || !/^\/(ai|telegram)\/[a-z-]+$/.test(to)) return;
    e.preventDefault();
    const body = new URLSearchParams(new FormData(f)), b = f.querySelector("button");
    if (b) b.setAttribute("disabled", "");
    call(to, body).then(r => {
      if (!(r.type === "opaqueredirect" || r.ok)) return r.text().then(t => show(t.trim().slice(0, 200) || "refused"));
      const q = f.querySelector("input.q");
      if (q) q.value = "";
      return r.type === "opaqueredirect" ? null : r.json().then(j => { if (j && typeof j.to === "string") follow(j.to); });  // where the page goes next: a question to answer first
    }).catch(() => show("the action could not be sent")).finally(() => { if (b) b.removeAttribute("disabled"); listen(); });
  }, true);
  window.addEventListener("popstate", () => { const r = parse(location.search); if (r) go(r, false); });
  document.addEventListener("visibilitychange", () => listen());
  let shaken = 0;
  window.addEventListener("resize", () => { clearTimeout(shaken); shaken = setTimeout(fit, 150); });
  window.addEventListener("load", fit);

  if (!route) route = {view: "", params: new URLSearchParams()};
  try {
    const first = JSON.parse((docNode && docNode.textContent) || "{}");
    for (const k of Object.keys(first)) if (first[k] && typeof first[k] === "object") docs[k] = first[k];
  } catch (err) { /* the stream brings them */ }
  for (const k of Object.keys(docs)) docFor[k] = route.params.toString();
  history.replaceState(null, "", appUrl(route));
  paint();
  if (live) listen();
})();
"""

APP_JS = (_APP.replace("@HTML_TAGS@", " ".join(HTML_TAGS)).replace("@SVG_TAGS@", " ".join(SVG_TAGS)).replace("@ATTRS@", " ".join(ATTRS)))
SCRIPTS = {"app": APP_JS}


def csp_source(text):
    """The script-src source of an inline script: 'sha256-<base64 of the SHA-256 of its UTF-8 text>'."""
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")
