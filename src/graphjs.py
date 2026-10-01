"""nuc-console graph view: the one script of the web view, on the graph page of the MAP only.

The page works without it (every node is a link); with it, nodes can be dragged, the view zoomed and panned. It reads the
SVG the server drew, never builds HTML from data, opens no connection and loads nothing: web.py sends it inline with its
SHA-256 in the Content-Security-Policy, so no other script can run. Its only navigations are the ones the page makes anyway:
the periodic reload (it replaces the <meta refresh>, which the server puts in <noscript>) and following a node's link.

What it keeps (sessionStorage, this tab only, key "nuc-graph:" + #gv's data-state): the view's zoom and pan and where the
nodes moved by hand are. The class names it sets, for the server's CSS, are listed at the top of SCRIPT.
"""
import base64
import hashlib

SCRIPT = r"""// nuc-console graph view (src/graphjs.py): drag the nodes, zoom and pan the MAP's graph. It reads the SVG the server
// drew and changes only coordinates, a transform and class names: no markup, no network, nothing loaded.
// Classes it sets, for the server's CSS:
//   #gv.js       the script runs
//   #gsvg.drag   a node drag, a pan or a pinch is in progress (cursor)
//   #gsvg.hov    a node is hovered or focused: dim what is not .hv
//   .hv          on a.n and line.e: the hovered or focused node, its neighbours and the edges touching it
//   a.n.pin      a node placed by hand: it keeps its place (double-click it to let it go)
// Mouse: drag a node or the background, wheel or pinch to zoom, double-click the background to reset the view.
// Keys: + and - zoom, 0 resets the view, the arrows pan.
(function main() {
  "use strict";
  if (!document.getElementById("gvp") && document.readyState === "loading") { document.addEventListener("DOMContentLoaded", main); return; }
  const gv = document.getElementById("gv"), svg = document.getElementById("gsvg"), vp = document.getElementById("gvp");
  if (!gv || !svg || !vp || !svg.getScreenCTM) return;
  const MIN_S = 0.2, MAX_S = 5, CLICK_PX = 4, QUIET_MS = 8000, KEY = "nuc-graph:" + (gv.getAttribute("data-state") || "");
  const ARROWS = new Map([["ArrowLeft", [60, 0]], ["ArrowRight", [-60, 0]], ["ArrowUp", [0, 60]], ["ArrowDown", [0, -60]]]);
  const nodes = [], edges = [], byKey = new Map(), byEl = new Map(), ptrs = new Map();
  const period = Number(gv.getAttribute("data-refresh")) * 1000, due = Date.now() + Math.max(period, 1000);
  const live = gv.getAttribute("data-paused") !== "1" && period > 0;
  let s = 1, tx = 0, ty = 0;         // the view: translate(tx,ty) scale(s) on #gvp, in the viewBox's units
  let alpha = 0, raf = 0, cell = 1;  // the simulation: its heat, its animation frame, the size of its grid's cells
  let drag = null, swallow = 0, hov = null, clickT = 0, gs = 1, last = 0, timer = 0;
  const num = (el, name) => parseFloat(el.getAttribute(name));
  const put = (el, name, v) => el.setAttribute(name, v.toFixed(1));
  const ok = (v, lo = -1e5, hi = 1e5) => typeof v === "number" && v >= lo && v <= hi;
  const touch = () => { last = Date.now(); };
  const nodeOf = t => (t && t.closest && byEl.get(t.closest("a.n"))) || null;
  const mode = on => svg.classList.toggle("drag", on);

  svg.querySelectorAll("a.n[data-k]").forEach(el => {
    const c = el.querySelector("circle"), k = el.getAttribute("data-k");
    if (!c || byKey.has(k)) return;
    const x = num(c, "cx"), y = num(c, "cy"), r = num(c, "r") || 0;
    let t = el.querySelector("text");
    const ox = t ? num(t, "x") - x : 0, oy = t ? num(t, "y") - y : 0;  // the label keeps its place under the circle
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (!Number.isFinite(ox) || !Number.isFinite(oy)) t = null;
    const n = {i: nodes.length, k, el, c, t, ox, oy, r, x, y, x0: x, y0: y, px: x, py: y, vx: 0, vy: 0,
      pin: false, wk: -1, deg: 0, adj: [], sp: 2 * r + 30};  // sp: the room it wants around itself; wk: see wake()
    nodes.push(n); byKey.set(k, n); byEl.set(el, n);
  });
  svg.querySelectorAll("line.e[data-a][data-b]").forEach(el => {
    const a = byKey.get(el.getAttribute("data-a")), b = byKey.get(el.getAttribute("data-b"));
    if (!a || !b || a === b) return;
    const e = {el, a, b, gap: b.r + (el.hasAttribute("marker-end") ? 4 : 0)};  // as the server drew it: room for an arrowhead
    edges.push(e); a.adj.push(e); b.adj.push(e); a.deg++; b.deg++;
  });

  function near(fn) {  // fn(a, b, dx, dy) for the pairs of nodes in the same or adjacent cells of a grid (cell >= every sp)
    const grid = new Map(), at = (gx, gy) => gx + ":" + gy;
    for (const n of nodes) {
      n.gx = Math.floor(n.x / cell); n.gy = Math.floor(n.y / cell);
      if (!grid.has(at(n.gx, n.gy))) grid.set(at(n.gx, n.gy), []);
      grid.get(at(n.gx, n.gy)).push(n);
    }
    for (const a of nodes) for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
      for (const b of grid.get(at(a.gx + i, a.gy + j)) || []) if (b.i > a.i) fn(a, b, b.x - a.x, b.y - a.y);
    }
  }
  function rest() {  // what is on the page is the state of rest: springs at their length, no more room than there is
    for (const e of edges) {
      e.len = Math.hypot(e.b.x - e.a.x, e.b.y - e.a.y);
      e.k = 1 / Math.min(e.a.deg, e.b.deg); e.bias = e.a.deg / (e.a.deg + e.b.deg);
    }
    for (const n of nodes) cell = Math.max(cell, n.sp);
    near((a, b, dx, dy) => { const d = Math.hypot(dx, dy); a.sp = Math.min(a.sp, d); b.sp = Math.min(b.sp, d); });
  }
  function tick() {  // one frame: springs along the edges, room between the nodes, friction; still nodes stay put
    const still = n => n.pin || n.wk < 0;
    for (const e of edges) {
      const a = e.a, b = e.b, w = still(a) ? 1 : still(b) ? 0 : e.bias;
      if (still(a) && still(b)) continue;
      let dx = b.x + b.vx - a.x - a.vx, dy = b.y + b.vy - a.y - a.vy;
      const d = Math.hypot(dx, dy) || 1, f = (d - e.len) / d * alpha * e.k;
      dx *= f; dy *= f;
      b.vx -= dx * w; b.vy -= dy * w; a.vx += dx * (1 - w); a.vy += dy * (1 - w);
    }
    near((a, b, dx, dy) => {
      const c = (a.sp + b.sp) / 2, d2 = dx * dx + dy * dy, w = still(a) ? 1 : still(b) ? 0 : 0.5;
      if (d2 >= c * c || (still(a) && still(b))) return;
      if (!d2) dx = 0.1;
      const d = Math.sqrt(d2) || 0.1, f = (c - d) / d * alpha * 0.3;
      b.vx += dx * f * w; b.vy += dy * f * w; a.vx -= dx * f * (1 - w); a.vy -= dy * f * (1 - w);
    });
    for (const n of nodes) {
      if (still(n)) { n.vx = n.vy = 0; continue; }
      n.vx *= 0.6; n.vy *= 0.6; n.x += n.vx; n.y += n.vy;
    }
    draw();
    alpha *= 0.955;  // at rest again in about a second and a half
    raf = alpha > 0.02 ? requestAnimationFrame(tick) : 0;
    if (!raf) { for (const n of nodes) n.wk = -1; save(); }
  }
  function wake(n) {  // n and the nodes up to 2 edges away move (wk: edges left); the rest of the graph holds them in place
    for (const todo = [[n, 2]]; todo.length;) {
      const [m, h] = todo.pop();
      if (m.wk >= h) continue;
      m.wk = h;
      if (h) for (const e of m.adj) todo.push([e.a === m ? e.b : e.a, h - 1]);
    }
    alpha = 1;
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function draw() {  // only what moved: circles, labels, and the lines touching them
    const moved = new Set();
    for (const n of nodes) {
      if (Math.abs(n.x - n.px) + Math.abs(n.y - n.py) < 0.05) continue;
      n.px = n.x; n.py = n.y; moved.add(n);
      put(n.c, "cx", n.x); put(n.c, "cy", n.y);
      if (n.t) { put(n.t, "x", n.x + n.ox); put(n.t, "y", n.y + n.oy); }
    }
    for (const e of edges) {
      if (!moved.has(e.a) && !moved.has(e.b)) continue;
      const a = e.a, b = e.b, dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy), u = d > a.r + e.gap ? 1 / d : 0;
      put(e.el, "x1", a.x + dx * u * a.r); put(e.el, "y1", a.y + dy * u * a.r);  // from circle to circle, as the server does
      put(e.el, "x2", b.x - dx * u * e.gap); put(e.el, "y2", b.y - dy * u * e.gap);
    }
  }

  function view() { vp.setAttribute("transform", "translate(" + tx.toFixed(2) + "," + ty.toFixed(2) + ") scale(" + s.toFixed(4) + ")"); }
  function ctm() { const m = svg.getScreenCTM(); return m && m.a && m.d ? m : null; }  // viewBox units -> screen pixels
  function toWorld(x, y) { const m = ctm(); return m && [((x - m.e) / m.a - tx) / s, ((y - m.f) / m.d - ty) / s]; }
  function zoomAt(x, y, f) {  // the point under (x, y) stays there
    const m = ctm(), ns = Math.min(MAX_S, Math.max(MIN_S, s * f));
    if (!m) return;
    const u = (x - m.e) / m.a, v = (y - m.f) / m.d;
    tx = u - (u - tx) * ns / s; ty = v - (v - ty) * ns / s; s = ns; view();
  }
  function panBy(dx, dy) { const m = ctm(); if (m) { tx += dx / m.a; ty += dy / m.d; view(); } }
  function reset() { s = 1; tx = ty = 0; view(); }
  function box() {  // the part of the graph's box that is on the screen
    const b = gv.getBoundingClientRect();
    return [Math.max(b.left, 0), Math.max(b.top, 0), Math.min(b.right, innerWidth), Math.min(b.bottom, innerHeight)];
  }
  function reveal(n) {  // a node reached with the keyboard comes into sight
    const m = ctm(), b = box();
    if (!m) return;
    const x = (n.x * s + tx) * m.a + m.e, y = (n.y * s + ty) * m.d + m.f;
    if (x < b[0] || x > b[2] || y < b[1] || y > b[3]) panBy((b[0] + b[2]) / 2 - x, (b[1] + b[3]) / 2 - y);
  }
  function pin(n, on) { n.pin = on; n.el.classList.toggle("pin", on); }
  function hover(n) {  // n, its neighbours and its edges get .hv, the svg .hov; null clears
    if (n === hov) return;
    for (const [h, on] of [[hov, false], [n, true]]) {  // the old one off, then the new one on: they may share neighbours
      if (!h) continue;
      h.el.classList.toggle("hv", on);
      for (const e of h.adj) for (const el of [e.el, e.a.el, e.b.el]) el.classList.toggle("hv", on);
    }
    hov = n; svg.classList.toggle("hov", !!n);
  }

  function save() {  // the view, and the nodes that are not drawn where the server put them (pinned, or moved along)
    const p = {};
    for (const n of nodes) if (n.pin || n.px !== n.x0 || n.py !== n.y0) p[n.k] = [+n.px.toFixed(1), +n.py.toFixed(1), n.pin ? 1 : 0];
    try { sessionStorage.setItem(KEY, JSON.stringify({v: [s, tx, ty], p})); } catch (err) { /* storage off or full */ }
  }
  function restore() {  // only for nodes still on the page, only sane numbers
    let st = null;
    try { st = JSON.parse(sessionStorage.getItem(KEY)); } catch (err) { return; }
    const v = st && st.v, p = st && st.p;
    if (Array.isArray(v) && ok(v[0], MIN_S, MAX_S) && ok(v[1]) && ok(v[2])) [s, tx, ty] = v;
    if (p && typeof p === "object") {
      for (const k of Object.keys(p)) {
        const n = byKey.get(k), q = p[k];
        if (n && Array.isArray(q) && ok(q[0]) && ok(q[1])) { n.x = q[0]; n.y = q[1]; pin(n, q[2] === 1); }
      }
    }
  }
  function plan() {  // the page's refresh: when due, but not during an interaction nor within 8 s of one, never while hidden
    clearTimeout(timer);
    if (!live || document.hidden) return;
    const now = Date.now(), at = Math.max(due, last + QUIET_MS, drag || raf ? now + 1000 : 0);
    if (at > now) timer = setTimeout(plan, at - now);
    else { save(); location.reload(); }
  }

  function mid() { const [p, q] = ptrs.values(); return {x: (p.x + q.x) / 2, y: (p.y + q.y) / 2, d: Math.hypot(p.x - q.x, p.y - q.y)}; }
  function start(id, x, y, n, moved) {  // a press: on a node it drags the node, elsewhere it pans the view
    const w = n && toWorld(x, y);
    drag = {id, x, y, tx, ty, moved, n: w ? n : null, ox: w ? n.x - w[0] : 0, oy: w ? n.y - w[1] : 0};
  }
  svg.addEventListener("pointerdown", e => {
    if (e.button > 0) return;  // the main button, a finger, a pen
    if (e.isPrimary) ptrs.clear();
    ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
    swallow = 0; touch();
    if (ptrs.size === 1) start(e.pointerId, e.clientX, e.clientY, nodeOf(e.target), false);
    else if (ptrs.size === 2) { drag = {pinch: mid(), moved: true}; mode(true); }
  });
  window.addEventListener("pointermove", e => {
    const p = ptrs.get(e.pointerId);
    if (!p || !drag) return;
    p.x = e.clientX; p.y = e.clientY; touch();
    if (drag.pinch) {  // two fingers: the midpoint pans, the distance zooms
      const g = mid(), o = drag.pinch;
      panBy(g.x - o.x, g.y - o.y);
      if (o.d > 0 && g.d > 0) zoomAt(g.x, g.y, g.d / o.d);
      drag.pinch = g;
    } else if (e.pointerId === drag.id && (drag.moved || Math.hypot(p.x - drag.x, p.y - drag.y) >= CLICK_PX)) {
      if (!drag.moved) {  // it is no longer a click
        drag.moved = true; mode(true);
        try { svg.setPointerCapture(e.pointerId); } catch (err) { /* the pointer is gone */ }
      }
      const w = toWorld(p.x, p.y), m = ctm();
      if (drag.n && w) { drag.n.x = w[0] + drag.ox; drag.n.y = w[1] + drag.oy; pin(drag.n, true); wake(drag.n); }
      if (!drag.n && m) { tx = drag.tx + (p.x - drag.x) / m.a; ty = drag.ty + (p.y - drag.y) / m.d; view(); }
    }
  });
  function up(e) {
    if (!ptrs.delete(e.pointerId) || !drag) return;
    touch();
    if (ptrs.size === 1 && drag.pinch) { const [[id, p]] = ptrs; start(id, p.x, p.y, null, true); }  // one finger left: it pans
    if (ptrs.size) return;
    if (drag.moved) {
      swallow = Date.now() + 500;  // the click that ends a drag does not follow the link
      if (drag.n) wake(drag.n);
      hover(e.pointerType === "mouse" ? nodeOf(document.elementFromPoint(e.clientX, e.clientY)) : null);
    }
    drag = null; mode(false);
  }
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
  window.addEventListener("click", e => {
    if (Date.now() < swallow) { swallow = 0; e.preventDefault(); e.stopPropagation(); return; }
    const n = nodeOf(e.target), href = n && n.el.getAttribute("href");
    if (!href || !n.pin || e.detail < 1 || e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault(); clearTimeout(clickT);  // a pinned node waits for a second click: a double-click lets it go
    if (e.detail > 1) { pin(n, false); wake(n); save(); } else clickT = setTimeout(() => { save(); location.assign(href); }, 300);
  }, true);
  svg.addEventListener("dblclick", e => { if (!nodeOf(e.target)) { e.preventDefault(); reset(); } });
  svg.addEventListener("dragstart", e => e.preventDefault());
  svg.addEventListener("wheel", e => {  // the wheel, and a trackpad's pinch (ctrl + wheel)
    const d = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1);
    e.preventDefault(); touch();
    zoomAt(e.clientX, e.clientY, Math.exp(-Math.max(-200, Math.min(200, d)) * (e.ctrlKey ? 0.01 : 0.002)));
  }, {passive: false});
  svg.addEventListener("gesturestart", e => { e.preventDefault(); gs = 1; });
  svg.addEventListener("gesturechange", e => {  // Safari's trackpad pinch (a touch screen's goes through the pointers)
    e.preventDefault();
    if (!(e.scale > 0)) return;
    if (!ptrs.size) { touch(); zoomAt(e.clientX, e.clientY, e.scale / gs); }
    gs = e.scale;
  });
  svg.addEventListener("pointerover", e => { const n = nodeOf(e.target); if (n && !(drag && drag.moved)) { hover(n); touch(); } });
  svg.addEventListener("pointerout", e => { if (hov && !(drag && drag.moved) && !hov.el.contains(e.relatedTarget)) hover(null); });
  svg.addEventListener("focusin", e => { const n = nodeOf(e.target); if (n) { hover(n); touch(); if (!drag) reveal(n); } });
  svg.addEventListener("focusout", () => hover(null));
  document.addEventListener("keydown", e => {  // only when nothing else wants the keys: focus on the page or in the graph
    const t = e.target, k = e.key, zoomBy = f => { const b = box(); zoomAt((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, f); };
    if (e.ctrlKey || e.metaKey || e.altKey || !t || !(t === document.body || t === document.documentElement || gv.contains(t))) return;
    if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName) || t.isContentEditable) return;
    if (k === "+" || k === "=") zoomBy(1.25);
    else if (k === "-" || k === "_") zoomBy(0.8);
    else if (k === "0") reset();
    else if (ARROWS.has(k)) panBy(ARROWS.get(k)[0], ARROWS.get(k)[1]);
    else return;
    e.preventDefault(); touch();
  });
  window.addEventListener("pagehide", save);
  document.addEventListener("visibilitychange", () => { if (document.hidden) save(); else plan(); });

  rest(); restore(); draw(); view();  // the springs' lengths and room are the server's, whatever was restored
  gv.classList.add("js");
  svg.style.touchAction = "none";  // fingers drag and pinch the graph, not the page
  svg.style.userSelect = svg.style.webkitUserSelect = "none";
  plan();
})();
"""


def csp_source(script=SCRIPT):
    """The script's hash as a CSP source ("'sha256-...'"): the SHA-256 of its UTF-8 bytes, as the browser computes it."""
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
