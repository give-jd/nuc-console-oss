"""nuc-console web view: the Hosts page, its data document and the settings section (docs/WEB.md, "Hosts").

The web process NEVER talks to another host. Everything here is a pure function of three things: the peers configured under [hosts], what the
poller (peers.py, its own account and its own tokens) last wrote into its cache folder, and the clock. `peers.read_cache` reads those files
as foreign input (links, owners, sizes and types are checked there); nothing here opens a socket, resolves a name or reads a token. Switching
to another host is a navigation: `go_target` gives the address of a configured peer (never one taken from a request), the browser opens that
host's own origin, and the other host serves its own pages with its own token.

A peer that cannot be reached, is not the host that answered first, refuses the token, speaks a newer API or has old data is never shown as
ok: `severity` ranks unreachable / untrusted / unauthorized / incompatible / err above warn, above stale / unknown, above ok, and an
unknown word ranks with the unknowns.
"""
import html
import re

import htmlview
import nuc_config
import peers
import ui
from weburl import page_url

TOP = ("down", "err", "unreachable", "untrusted", "unauthorized", "incompatible")  # the states that need you first
LOW = ("stale", "unknown")  # not known to be fine
# the look of a state: the style class and symbol of the nearest of the six states a card has (a symbol besides the colour, always)
LOOK = {"ok": "ok", "info": "info", "warn": "warn", "stale": "unknown", "unknown": "unknown", "err": "err", "down": "down",
        "unreachable": "down", "unauthorized": "err", "untrusted": "err", "incompatible": "err"}
NO_DATA = "no-data"
CLOCK = "clock"
TEXT = dict(peers.REASONS, **{NO_DATA: "the poller has no data about this host yet",
                              CLOCK: "the poller's clock is ahead of this machine's: its data cannot be trusted to be fresh"})
SKEW_S = peers.SKEW_WARN_S  # a clock this far from ours is said
POLLER_FIX = "sudo systemctl start nuc-console-peers"  # a portable run: python3 src/peers.py serve
STATE_RE = re.compile(r"[a-z]{2,16}")


def tier(state):
    """3: needs you; 2: warn; 1: stale or unknown (old or no data); 0: ok. Whatever is not known is 1, never 0."""
    return 3 if state in TOP else 2 if state == "warn" else 0 if state in ("ok", "info") else 1


def severity(state):
    return (tier(state), peers.RANK.get(state, 2))


def quant(sec):
    """An age in coarse steps (5 s under a minute, a minute above), so that the document's rev moves only when something does."""
    sec = max(0, int(sec))
    return sec // 5 * 5 if sec < 60 else sec // 60 * 60


def _s(x, n=120):
    return ui.safe(x)[:n] if isinstance(x, str) else ""


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def host_doc(peer, entry, now):
    """One host of the document, from the configured peer and its cache entry (None: the poller has not written it)."""
    e = entry or {}
    s = e.get("summary") if isinstance(e.get("summary"), dict) else {}
    state = e.get("state") if e else "unknown"
    identity = e.get("identity") if e.get("identity") in ("ok", "changed") else "ok"
    if identity == "changed":
        state = "untrusted"  # not the host that answered first: nothing it says is taken as true
    if state not in peers.STATES or (state in ("ok", "info", "warn") and "state" not in s):
        state = "unknown"  # a word the poller does not use, or a good one with no summary behind it: not known
    err = e.get("error") if e.get("error") in TEXT else ""
    if state in ("ok", "info", "warn") and (err or e.get("error")):
        state = "unknown"  # the poller never writes a good state with an error: not known
    if not entry:
        err = NO_DATA
    elif any(isinstance(e.get(k), (int, float)) and not isinstance(e.get(k), bool) and e[k] - now > SKEW_S for k in ("ts", "last_ok")):
        if tier(state) < 3:
            state = "stale"  # stamped in the future (a clock stepped back, a leftover file): that is not fresh data
        err = CLOCK
    counts = s.get("counts")
    counts = {k: _int(counts.get(k)) or 0 for k in ("err", "warn", "unknown")} if isinstance(counts, dict) else None
    last = _int(e.get("last_ok")) if e else None
    try:
        shown = peers.parse_url(peer.get("url")).display
    except ValueError:
        shown = ""
    return {"id": peer["name"], "name": _s(s.get("host")) or peer["name"], "url": _s(shown, 300), "state": state, "status": _s(s.get("status_text")),
            "counts": counts, "worst": [_s(t) for t in (s.get("worst") if isinstance(s.get("worst"), list) else [])[:3]],
            "age_s": quant(now - last) if last is not None else None, "skew_s": _int(e.get("skew_s")),
            "version": _s(s.get("version"), 40), "api": _int(s.get("api")), "scope": e.get("scope") if e.get("scope") in peers.SCOPES else peer.get("scope", "summary"),
            "insecure": bool(e.get("insecure")), "identity": identity, "error": err}


def poller_state(cache, now):
    """running | stale | absent, from fleet.json (the poller writes it at every round)."""
    if cache.get("fleet") is None:
        return "absent"
    ts = cache["fleet"].get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not -SKEW_S <= now - ts <= peers.CACHE_STALE_S:
        return "stale"  # old, or stamped in the future beyond the tolerance
    return "running"


def build(hosts, folder, me, now, cache=None):
    """The hosts document: {"self": {...}, "hosts": [...], "fleet": {"state", "poller"}}, in the order of [hosts]. Only the cache (views=False)."""
    cache = cache if cache is not None else peers.read_cache(folder, now=now, views=False)
    rows = [host_doc(p, cache["peers"].get(p["name"]), now) for p in hosts]
    poller = poller_state(cache, now)
    states = [r["state"] for r in rows] + ([] if poller == "running" else ["stale"])  # a poller that does not run: nothing is known to be fine
    return {"self": me, "hosts": rows, "fleet": {"state": max(states, key=severity, default="unknown"), "poller": poller}}


def go_target(hosts, ident):
    """The address 'Open on <name>' leads to: the configured peer's URL, rebuilt from its parsed parts (scheme, host, port, path; no user info,
    query or fragment), with a trailing slash. None for an id that is not a configured one (or whose URL does not parse): never a URL from
    the request."""
    if not isinstance(ident, str) or not nuc_config.HOST_NAME_RE.fullmatch(ident):
        return None
    for p in hosts:
        if p.get("name") == ident:
            try:
                return peers.parse_url(p.get("url")).display + "/"
            except ValueError:
                return None
    return None


def order(rows):
    """Worst first, then by name."""
    return sorted(rows, key=lambda r: (-severity(r["state"])[0], -severity(r["state"])[1], r["name"].lower(), r["id"]))


def duplicates(doc):
    """The display names that two machines (this one included) go by: they cannot be told apart on a screen."""
    seen, dup = {}, []
    for name in [doc["self"]["name"]] + [r["name"] for r in doc["hosts"]]:
        seen[name.lower()] = seen.get(name.lower(), 0) + 1
        if seen[name.lower()] == 2:
            dup.append(name)
    return dup


def badge(doc):
    """The tab's badge: the symbol of the fleet's state and how many hosts need a look; '' when everything is fine."""
    state = doc["fleet"]["state"]
    if tier(state) == 0:
        return ""
    n = sum(1 for r in doc["hosts"] if tier(r["state"]) > 0) or 1
    return '<span class="s-%s">%s%d</span>' % (LOOK.get(state, "unknown"), html.escape(ui.SYMBOLS[LOOK.get(state, "unknown")]), n)


# ---- the page -------------------------------------------------------------------------------------------------------------------------
esc = html.escape


def chip(state):
    look = LOOK.get(state, "unknown")
    return f'<span class="chip st-{look}">{esc(ui.SYMBOLS[look])} {esc(state)}</span>'


def ago(age):
    return "never" if age is None else ui.fmt_ago(age) + " ago"


def go_url(ident):
    return page_url({"view": "hosts", "go": ident})


def counts_html(c):
    if c is None:
        return '<span class="s-unknown"><span class="sym unknown" aria-hidden="true">?</span> problems not reported</span>'
    return " ".join((f'<span class="s-err"><span class="sym" aria-hidden="true">{ui.SYMBOLS["err"]}</span> {c["err"]} err</span>',
                     f'<span class="s-warn"><span class="sym" aria-hidden="true">{ui.SYMBOLS["warn"]}</span> {c["warn"]} warn</span>')
                    + ((f'<span class="s-unknown"><span class="sym unknown" aria-hidden="true">?</span> {c["unknown"]} unknown</span>',) if c["unknown"] else ()))


def open_link(r, cls="btn pri"):
    return f'<a class="{cls}" href="{esc(go_url(r["id"]))}">Open on {esc(r["name"])}</a>'


def card(r, here):
    """One host: a card with the state, the name, the version, the counts, the age, the connection, the worst titles and the way there."""
    worst = "".join(f"<li>{esc(t)}</li>" for t in r["worst"])
    conn = TEXT.get(r["error"], "") if r["error"] else ""
    link = conn or "answering"
    return (f'<article class="fl-card st-{LOOK.get(r["state"], "unknown")}" data-state="{esc(r["state"])}" aria-label="{esc(r["name"])}: {esc(r["state"])}">'
            f'<div class="fl-top"><h3>{esc(r["name"])}</h3>{chip(r["state"])}</div>'
            f'<p class="sm fl-sub">{esc(r["version"] or "version ?")}{" · " + esc(r["url"]) if r["url"] else ""}</p>'
            f'<p class="fl-count">{counts_html(r["counts"])}</p>'
            f'<p class="sm">data {esc(ago(r["age_s"]))} · connection: {esc(link)}</p>'
            + (f'<ul class="fl-worst">{worst}</ul>' if worst else "")
            + f'<div class="fl-act">{open_link(r)}<a class="lnk" href="{esc(page_url(dict(here, view="hosts", host=r["id"])))}">details</a></div></article>')


def banners(doc):
    """[(symbol, bold text, small HTML)]: what needs a word besides the cards."""
    out, code = [], lambda t: f'<code class="cmd">{esc(t)}</code>'  # noqa: E731
    poller = doc["fleet"]["poller"]
    if poller != "running":
        out.append(("!", "The peer poller is not running: what is shown about the other hosts is old or missing." if poller == "stale"
                    else "The peer poller is not running: there is no data about the other hosts.",
                    f"start it: {code(POLLER_FIX)} (a portable run: {code('python3 src/peers.py serve')})"))
    for r in doc["hosts"]:
        n = r["name"]
        if r["error"] == CLOCK:
            out.append(("!", f"{n}: the poller's clock is ahead of this machine's.", "its data is not taken as fresh; check the time on the machine that runs the poller"))
        if r["identity"] != "ok":
            out.append(("✖", f"{n}: this is not the host that answered first (its identity changed).",
                        f"check the address in [hosts], then {code('nuc-console-fleet forget ' + r['id'])}"))
        if r["scope"] == "full":
            out.append(("!", f"{n}: read through a token with full access (a 2.5.x server has no read-only peer token).",
                        "update that host, then give it a peer token (docs/WEB.md, peer tokens)"))
        if r["skew_s"] is not None and abs(r["skew_s"]) > SKEW_S:
            out.append(("!", f"{n}: its clock differs from this machine's by {abs(r['skew_s'])} s.", "certificates, one-time codes and logs are out of step"))
        if r["insecure"]:
            out.append(("!", f"{n}: reached over plain http on a local network: the token travels unencrypted.", "use https, or a Tailscale address"))
    for name in duplicates(doc):
        out.append(("!", f"Two machines are called {name}.", "give each its own name: <code class=\"cmd\">[host] name</code> in config.ini"))
    return out


def banners_html(doc):
    return "".join(htmlview.banner(sym, bold, small) for sym, bold, small in banners(doc))


def page_html(doc, here, host=""):
    """The Hosts page: the fleet's state, the banners, one card per host worst first; with host=<id> (a configured one) that host in detail."""
    rows = {r["id"]: r for r in doc["hosts"]}
    fleet, n = doc["fleet"], len(doc["hosts"])
    me = doc["self"]
    head = (f'<section class="fl-head" aria-label="Fleet">{chip(fleet["state"])} <b>{n} host{"" if n == 1 else "s"}</b>'
            f'<span class="sm">this machine: {esc(me["name"])}{" " + esc(me["version"]) if me["version"] else ""} · poller {esc(fleet["poller"])}</span></section>')
    if host in rows:
        return '<div class="fl">' + head + banners_html(doc) + detail(rows[host], here) + "</div>"
    return '<div class="fl">' + head + banners_html(doc) + '<div class="fl-grid">' + "".join(card(r, here) for r in order(doc["hosts"])) + "</div></div>"


def detail(r, here):
    conn = TEXT.get(r["error"], "") if r["error"] else "answering"
    rows = (("state", chip(r["state"])), ("status", esc(r["status"] or "?")), ("problems", counts_html(r["counts"])),
            ("worst", "<br>".join(esc(t) for t in r["worst"]) or '<span class="sm">none reported</span>'), ("version", esc(r["version"] or "?")),
            ("API", esc(str(r["api"]) if r["api"] is not None else "?")), ("address", esc(r["url"] or "?")), ("token", esc(r["scope"])),
            ("data", esc(ago(r["age_s"]))), ("clock", "?" if r["skew_s"] is None else esc("%+d s" % r["skew_s"])),
            ("connection", esc(conn)), ("test it", f'<code class="cmd">nuc-console-fleet test {esc(r["id"])}</code>'))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return (f'<article class="fl-card st-{LOOK.get(r["state"], "unknown")} fl-detail" data-state="{esc(r["state"])}"><div class="fl-top"><h3>{esc(r["name"])}</h3></div>'
            f'<dl class="about">{dl}</dl><div class="fl-act">{open_link(r)}<a class="lnk" href="{esc(page_url(dict(here, view="hosts")))}">all hosts</a></div></article>')


def settings_html(hosts, doc):
    """The settings page's section: the configured peers and what the poller last saw, read-only (the file is the administrator's)."""
    by = {r["id"]: r for r in doc["hosts"]}
    items = []
    for p in hosts:
        r = by.get(p["name"])
        items.append((esc(p["name"]), f'{esc(r["url"] or "(invalid URL)")} · token {esc(r["scope"])}{" · pinned" if p.get("pin") else ""}'
                      f' · last: {chip(r["state"])} {esc(ago(r["age_s"]))}<br>pair or test: <code class="cmd">nuc-console-fleet test {esc(p["name"])}</code>'))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in items)
    return ('<section class="sec" id="hosts" aria-labelledby="sec-hosts"><h3 class="sech" id="sec-hosts">Hosts</h3>'
            '<p class="hintl">The other nuc-console hosts this one watches ([hosts] in config.ini, the administrator\'s: this page never writes it, '
            f'and no token is shown). Poller: {esc(doc["fleet"]["poller"])}.</p><dl class="about">{dl}</dl></section>')
