"""nuc-console: the Hosts screen (key 6) and the screens of another host (docs/DESIGN.md, "Remote documents" and "The Hosts screen").

What the console keeps while these screens are open, what each key does (ui.KEYMAP, scopes hosts and remote), and the reading of the poller's
cache: the list of hosts comes from `peers.read_cache(views=False)` ranked by webhosts (the web page's own ranking), the documents of the host
being looked at from `peers.read_views` (size-capped, hardened, foreign input), decoded by remotedoc (never raises) and kept until the cache
file changes. The renderer opens no socket and starts no thread or process for any of it. Drawing is render.py's: it owns the frame, and the
first line of a remote host's screen is always render.remote_header.
"""
import os
import time

import ansi
import nuc_config
import remotedoc
import ui
from ui import Col, Group, Msg, Row, Span, Table, Title

ME = ""  # the id of the line that stands for this machine ("this host"); a configured peer's name can never be empty
KEY_VIEW = {"1": "overview", "2": "map", "3": "cpu", "4": "health", "e": "events"}  # what the keys choose on a remote host
VIEW_ORDER = ("overview", "map", "cpu", "health", "events")  # Tab goes through them in this order
VIEW_WORD = {"overview": "overview", "map": "map", "cpu": "CPU", "health": "health", "events": "events"}
NO_AI = "AI, Telegram and settings are never shown for another host"
STATE_TONE = {"ok": "ok", "info": "info", "warn": "warn", "stale": "unknown", "unknown": "unknown", "err": "err", "down": "err", "unreachable": "err",
              "unauthorized": "err", "untrusted": "err", "incompatible": "err"}  # the colour of a state word; its symbol is webhosts.LOOK's


class HostsView(object):
    """The Hosts list and, once a host is opened, that host's screens: mode ('list' | 'remote'), the cursor of the list (cur: the host's id, ME for
    this machine; idx and top are where it stays when it is gone), the host and its view shown (name, view), how far its body is scrolled,
    a note (the last key that could not be obeyed), the local screen to go back to (origin) with the views it had open (stash: map, cpu, health,
    ai, events: put back as they were, so the local frame is the one that was left), and when it was opened and last touched."""

    def __init__(self, origin="overview", stash=(None, None, None, None, None), now=None):
        self.mode, self.cur, self.idx, self.top, self.rows = "list", ME, 0, 0, 10
        self.name, self.view, self.scroll, self.room, self.note = "", "", 0, 10, ""  # view "": the one it opens on (start_view)
        self.origin, self.stash = origin, stash
        self.opened = self.touched = time.time() if now is None else now
        self.sig, self.raw, self.docs, self.body = None, {}, {}, None  # the cache file's stamp, its documents, the decoded ones, the drawn body

    @property
    def scope(self):
        return "remote" if self.mode == "remote" else "hosts"


# ---- the list ------------------------------------------------------------------------------------------------------------------------
def build(hosts, folder, now, cache=None, name="this host"):
    """(the hosts document of webhosts.build, [the rows worst first]): the web page's states and order, from [hosts] and the cache (files
    only). The caller reads the cache (cache=...), or it is read here with views=False. name: what this machine goes by on the screens
    ([host] name, else the host name: the header's own), shown on the line 'this host'."""
    import webhosts  # here: the console process loads the web view's helpers only when there are hosts to list
    me = {"id": ME, "name": name, "version": nuc_config.VERSION, "host_id": "", "os": ""}
    doc = webhosts.build(hosts, folder, me, now, cache)
    return doc, webhosts.order(doc["hosts"])


def entry_of(cache, name):
    e = (cache or {}).get("peers", {}).get(name)
    return e if isinstance(e, dict) else None


def views_of(entry):
    """The views a cached entry holds a document of, in the order Tab goes through them."""
    index = entry.get("view_index") if isinstance(entry, dict) else None
    return [v for v in VIEW_ORDER if isinstance(index, dict) and v in index]


def why_not(entry, name, view):
    """Why a view cannot be shown, in words (nothing the peer wrote is in them): it is the body of the screen, or the footer's note."""
    if entry is None:
        return f"no data from {ui.safe(name)[:40]} yet (the poller has not reached it)"
    if entry.get("scope") == "summary":
        return "not available for this peer: its token has scope summary"
    if not entry.get("view_index"):
        return f"not available for this peer yet: the poller holds no document of {ui.safe(name)[:40]} (it fetches them after the summary)"
    return f"not available for this peer: it served no {VIEW_WORD.get(view, 'such')} document"


def counts_text(c):
    if c is None:
        return "?"
    parts = [f"{c[k]} {k}" if k != "unknown" else f"{c[k]} ?" for k in ("err", "warn", "unknown") if c[k]]
    return " ".join(parts) if parts else "none"


def state_cell(r):
    import webhosts
    look = webhosts.LOOK.get(r["state"], "unknown")
    return Span(ui.SYMBOLS[look] + " " + r["state"], STATE_TONE.get(r["state"], "unknown"))


def _cut(text, n):
    return text if len(text) <= n else text[:max(n - 1, 0)] + "…"


def list_lines(doc, rows, hs, cache, w, h):
    """The Hosts list as console lines (at most h, none wider than w): the title, this machine first, then the peers worst first with the
    cursor's row in reverse; a line under the title when the poller is not running. Moves hs.idx/cur/top and sets hs.rows (the page)."""
    import webhosts
    poller = doc["fleet"]["poller"]
    lines = [r["id"] for r in rows]
    ids = [ME] + lines
    i = ids.index(hs.cur) if hs.cur in ids else None
    hs.idx = i if i is not None else max(0, min(hs.idx, len(ids) - 1))
    hs.cur = ids[hs.idx]
    n = len(rows)
    out = [Title("HOSTS", [Span("this host and %d other%s" % (n, "" if n == 1 else "s"), "muted"), Span("poller " + poller, "muted")])]
    if poller != "running":
        out.append(Msg("warn", "the peer poller is not running: the other hosts' data is old or missing (sudo systemctl start nuc-console-peers)"))
    notes = len(out)
    room = max(1, h - notes - 1 - 1)  # the head row, the cursor never leaves the page; the title and the notes
    hs.rows = room
    hs.top = ansi.scroll(hs.top, hs.idx, len(ids), room)
    nw = max(8, min(20, max([len(x) for x in ids] + [len(doc["self"]["name"])]), (w - 40) // 3))
    cols = [Col("state", "STATE", w=14, gap=1), Col("name", "HOST", w=nw, gap=1), Col("problems", "PROBLEMS", w=16, gap=1, prio=3),
            Col("age", "DATA", w=8, gap=1, prio=2), Col("version", "VERSION", w=8, gap=1, prio=1), Col("scope", "TOKEN", w=8, gap=1, prio=4),
            Col("views", "VIEWS", prio=5)]
    body = []
    me = doc["self"]
    for j in range(hs.top, min(len(ids), hs.top + room)):
        tone = "sel" if j == hs.idx else None
        if j == 0:
            cells = [Span("· this host", "muted"), Span(_cut(me["name"], nw)), Span(""), Span("live", "muted"), Span(_cut(me["version"], 8)), Span("-", "muted"),
                     Span("all", "muted")]
        else:
            r = rows[j - 1]
            e = entry_of(cache, r["id"])
            cells = [state_cell(r), Span(_cut(r["id"], nw), full=r["id"]), Span(counts_text(r["counts"]), webhosts_tone(r)),
                     Span("never" if r["age_s"] is None else ui.fmt_ago(r["age_s"]), "muted"), Span(_cut(r["version"] or "?", 8)),
                     Span(r["scope"], "muted"), Span(f"{len(views_of(e))}/{len(VIEW_ORDER)}", "muted")]
        body.append(Row(cells, tone))
    out.append(Table(cols, body, head=True, indent=1, fill=True, head_tone="accent_strong", solid=w, clip=w))
    return [ansi.clip(x, w) for x in ansi.render(Group(out), w)[0][:h]]


def webhosts_tone(r):
    c = r["counts"]
    return "unknown" if c is None else "err" if c["err"] else "warn" if c["warn"] or c["unknown"] else "ok"


def list_footer_pos(hs, n):
    return f"host {hs.idx + 1}/{n + 1}"


def hosts_key(hs, key, ids):
    """One key on the Hosts list (ui.KEYMAP, scope hosts); ids: [ME] + the peers' ids in the order drawn. Returns 'back' (Esc, q), 'local' (Enter
    on this machine: back to the local screen), 'open' (a peer was chosen: hs.name is it) or '' (only the cursor moved)."""
    act = ui.action("hosts", key)
    if act == "back":
        return "back"
    if act == "pick":
        if hs.cur == ME or hs.cur not in ids:
            return "local"
        hs.mode, hs.name, hs.scroll, hs.note, hs.view = "remote", hs.cur, 0, "", ""
        hs.sig, hs.raw, hs.docs, hs.body = None, {}, {}, None
        return "open"
    if not ids or act not in ("move", "page"):
        return ""
    i = ids.index(hs.cur) if hs.cur in ids else hs.idx
    page = max(1, hs.rows - 1)
    i = {"up": i - 1, "k": i - 1, "down": i + 1, "j": i + 1, "pgup": i - page, "pgdn": i + page, "home": 0, "end": len(ids) - 1}.get(key, i)
    hs.idx = max(0, min(i, len(ids) - 1))
    hs.cur = ids[hs.idx]
    return ""


# ---- one remote host ------------------------------------------------------------------------------------------------------------------
def start_view(entry):
    """The view a host opens on: its overview when the cache holds it, else the first it holds, else the overview (the screen says why not)."""
    have = views_of(entry)
    return "overview" if not have or "overview" in have else have[0]


def remote_key(hs, key, entry):
    """One key on a remote host's screen (ui.KEYMAP, scope remote). Returns 'back' (Esc, q: to the list), '' (done here) or None (not a key of
    this screen: the help, r and Z are the console's own). 1 2 3 4 e choose the host's view when the cache holds it (else hs.note says why,
    the view stays); Tab/Shift+Tab go to the next/previous view it holds; the others scroll the body (hs.scroll is clamped by the drawing).
    5 and 6 are nothing here: AI and the list are not views of another host."""
    hs.note = ""
    if key in ("5", "6"):
        hs.note = NO_AI if key == "5" else "6 opens the list: Esc goes back to it"
        return ""
    act = ui.action("remote", key, lambda f: f != ui.CONSOLE_FLEET)
    if act in ("help", "redraw", "pause"):
        return None
    if act == "back":
        return "back"
    if act == "rview":
        want = KEY_VIEW[key]
        if want in views_of(entry):
            hs.view, hs.scroll = want, 0
        else:
            hs.note = why_not(entry, hs.name, want)
        return ""
    if act == "rnext":
        have = views_of(entry)
        if have:
            i = have.index(hs.view) if hs.view in have else -1 if key == "tab" else 0
            hs.view, hs.scroll = have[(i + (1 if key == "tab" else -1)) % len(have)], 0
        else:
            hs.note = why_not(entry, hs.name, hs.view)
        return ""
    if act == "scroll":
        page = max(1, hs.room - 1)
        hs.scroll = max(0, {"up": hs.scroll - 1, "k": hs.scroll - 1, "down": hs.scroll + 1, "j": hs.scroll + 1, "pgup": hs.scroll - page,
                            "pgdn": hs.scroll + page, "home": 0, "end": 10 ** 9}.get(key, hs.scroll))
    return ""


def read_remote(hs, folder, entry, reader=None, sig=None):
    """Brings hs.raw up to the cache: the documents of the host in hs.name are read again only when its views file changed (its mtime and size;
    the read itself is peers.read_views: size-capped, no links, owner checked) and decoded only when asked for (document). reader/sig: the demo's
    own source, a function name -> documents and the stamp it goes by. Never raises."""
    if not views_of(entry):
        hs.sig, hs.raw, hs.docs, hs.body = None, {}, {}, None
        return
    import peers  # here: the console loads the poller's reader only when a host is opened
    if sig is None:
        try:
            st = os.stat(os.path.join(folder, hs.name + peers.VIEWS_SUFFIX))
            sig = (hs.name, st.st_mtime_ns, st.st_size, entry.get("ts"))
        except (OSError, ValueError):
            sig = None
    if sig is not None and sig == hs.sig:
        return
    raw = (reader or (lambda name: peers.read_views(folder, name)))(hs.name)
    hs.raw, hs.docs, hs.body = raw, {}, None
    hs.sig = sig if raw else None  # nothing read although the index lists documents: look again at the next tick


def document(hs, view):
    """The decoded document of `view` of the host being looked at (a remotedoc.Document), or None when the cache has none of it."""
    if view not in hs.raw:
        return None
    d = hs.docs.get(view)
    if d is None:
        d = hs.docs[view] = remotedoc.decode(hs.raw[view])
    return d


def idle(hs, now, seconds):
    """Has the keyboard been left alone long enough (seconds; 0 = never) for the screen to go back to the local one."""
    return bool(seconds) and now - hs.touched > seconds
