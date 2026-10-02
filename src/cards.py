"""The cards of the overview and the KPIs of the row above them (stdlib only, Python 3.8+).

One card per overview section (the ids are nuc_config.SECTIONS), one KPI per prefs.KPI_IDS. A card is built by a function registered
here with register(); build() asks it for a ui.Card at a detail level k and remembers the answer for the frame, so the layout engine
(render.page_overview), which tries every level and lifts the caps one section after the other, never builds the same card twice.
The KPIs are figures read off the same Ctx; a source that is missing makes the KPI 'unknown' ('?'), never fine.

Nothing here draws, reads a file or knows the size of a screen (a card's builder is told its width through Caps). Every section is a
native card below (@native: data to components, drawn by ansi.card_lines and htmlview.html); render.py registers them in the order of the
screen. A card made of ui.Raw lines (raw_card) is still drawn by both renderers.

How a problem becomes the state of a card (PROBLEM_CARDS: the problem ids of render.problems_raw, and the cards they belong to):

  collector-containers  stale-containers  unhealthy-container  container-exited      containers
  collector-boot  failed-units  journal-errors                                       boot
  thermal  throttling                                                                system
  collector-net  stale-net  net-sections                                             exposure firewall webapps databases tailscale
  db-open-lan                                                                        databases exposure
  docker-bypass                                                                      firewall exposure
  funnel-public                                                                      tailscale exposure
  over-exposed  expose-unmatched                                                     exposure
  port-new  port-changed  port-gone  port-compare-suspended                          exposure
  baseline-missing  baseline-unreadable                                              exposure
  firewall-off  firewall-policy  firewall-unreadable  ufw-missing  ufw-off  ufw-unreadable      firewall
  config-unreadable                                                                  exposure webapps (and attention)
  telegram-unpaired  telegram-failing                                                (attention only)

The attention card belongs to every problem. The state of a card is the worst of its problems (severity 3 and 2: 'err', 1: 'warn'); a
card whose source is missing is 'unknown' whatever the problems say (the problem says why: the collector is not running); a card with
nothing to show on purpose (docker not installed, no filesystems) is 'info'; else 'ok'. A problem list that does not say which problem is
which (a plain list of (severity, text)) leaves every card but attention 'unknown' as soon as it is not empty: nothing is invented.
"""
import re
import time

import nuc_config
import prefs
import ui
from exposure import (GROUPS, SENSITIVE, bind_scope, expose_apply, expose_note, expose_over_items, exposure_rows, group_of, is_private_addr, os_of,
                      webapp_rows)
from ui import Bar, Card, Col, Flow, Grid, Group, Head, Indent, Kpi, Line, More, Msg, NoteTable, Only, Raw, Row, Span, Spark, Table, Timeline, Wrap


# ---- Caps: what a card may show -----------------------------------------------------------------------------------------------

class Caps(object):
    """What the layout lets a card show. width: the columns the card is drawn in; full: nothing is capped (the detail pages, the web's
    full view); expand: the sections whose caps are lifted because the free space allows it; trunc: where a card says which sections
    hid items ('... +N more'), the Details pages show them in full. The layout (render.page_overview) owns the two sets and shares them
    with every Caps it makes."""
    __slots__ = ("width", "full", "expand", "trunc")

    def __init__(self, width=0, full=False, expand=None, trunc=None):
        self.width, self.full = width, bool(full)
        self.expand = set() if expand is None else expand  # not copied: the layout passes its own sets
        self.trunc = set() if trunc is None else trunc

    def opened(self, section):
        """True when `section` shows everything: the full view, or its caps are lifted."""
        return self.full or section in self.expand

    def lim(self, seq, n, section):
        """seq[:n] unless the section is open; remembers that it hid items (in this Caps' trunc)."""
        if self.opened(section) or len(seq) <= n:
            return seq
        self.trunc.add(section)
        return seq[:n]

    def key(self, id):
        """What of the caps changes a card's lines: the memo's key."""
        return (self.full, id in self.expand, self.width)


# ---- Ctx: what a frame is built from ------------------------------------------------------------------------------------------

class Ctx(object):
    """The data of one frame, as the builders read it: s (the Sampler's reading), cont / net / boot (the collectors' state: None = not
    running), problems (a render.ProblemList: (severity, text) with .pids, the id of each, and .accepted), cfg (nuc_config.current()),
    prefs (prefs.effective()), now, baseline, new (the new exposed ports), health (render.health_data(): {"report"}) and ai
    (render.ai_status()): the last two only where a KPI wants them. memo: what was built or worked out in this frame."""
    __slots__ = ("s", "cont", "net", "boot", "problems", "cfg", "prefs", "now", "baseline", "new", "health", "ai", "memo")

    def __init__(self, s=None, cont=None, net=None, boot=None, problems=None, cfg=None, prefs=None, now=None, baseline=None, new=None,
                 health=None, ai=None):
        self.s, self.cont, self.net, self.boot, self.problems = s, cont, net, boot, problems
        self.cfg = nuc_config.current() if cfg is None else cfg
        self.prefs, self.now, self.baseline, self.new, self.health, self.ai = prefs, now, baseline, new or {}, health, ai
        self.memo = {}

    def once(self, key, fn):
        """fn() the first time `key` is asked in this frame, the same answer after."""
        try:
            return self.memo[key]
        except KeyError:
            val = self.memo[key] = fn()
            return val

    def problem_ids(self):
        """[(severity, problem id or None)] of the problems of this frame."""
        pb = self.problems or []
        pids = getattr(pb, "pids", None)
        if pids is None or len(pids) != len(pb):
            return [(sev, None) for sev, _ in pb]
        return [(item[0], pid) for item, pid in zip(pb, pids)]


# ---- the problems a card answers for ------------------------------------------------------------------------------------------

_NET_CARDS = ("exposure", "firewall", "webapps", "databases", "tailscale")
PROBLEM_CARDS = {
    "collector-containers": ("containers",), "stale-containers": ("containers",), "unhealthy-container": ("containers",),
    "container-exited": ("containers",),
    "collector-boot": ("boot",), "failed-units": ("boot",), "journal-errors": ("boot",),
    "thermal": ("system",), "throttling": ("system",),
    "collector-net": _NET_CARDS, "stale-net": _NET_CARDS, "net-sections": _NET_CARDS,
    "db-open-lan": ("databases", "exposure"), "docker-bypass": ("firewall", "exposure"), "funnel-public": ("tailscale", "exposure"),
    "over-exposed": ("exposure",), "expose-unmatched": ("exposure",),
    "port-new": ("exposure",), "port-changed": ("exposure",), "port-gone": ("exposure",), "port-compare-suspended": ("exposure",),
    "baseline-missing": ("exposure",), "baseline-unreadable": ("exposure",),
    "firewall-off": ("firewall",), "firewall-policy": ("firewall",), "firewall-unreadable": ("firewall",),
    "ufw-missing": ("firewall",), "ufw-off": ("firewall",), "ufw-unreadable": ("firewall",),
    "config-unreadable": ("exposure", "webapps"),
    "telegram-unpaired": (), "telegram-failing": (),
}
_SEV_STATE = {3: "err", 2: "err", 1: "warn"}  # a port change (3) is as urgent as an error (2)


def problem_state(id, ctx):
    """The state the problems of this frame give card `id` ('ok' when none belongs to it)."""
    items = ctx.problem_ids()
    if id == "attention":
        return ui.worst(_SEV_STATE.get(sev, "warn") for sev, _ in items)
    if any(pid is None for _, pid in items):
        return "unknown"  # the list does not say which problem is which
    return ui.worst(_SEV_STATE.get(sev, "warn") for sev, pid in items if id in PROBLEM_CARDS.get(pid, ()))


def _has(d, key):
    return isinstance(d, dict) and d.get(key) is not None


def _readable(id, ctx):
    """False when the source of the card is missing: it is drawn 'unknown'."""
    s, net = ctx.s if isinstance(ctx.s, dict) else {}, ctx.net
    if id in ("exposure",):
        return _has(net, "listeners")
    if id in ("firewall", "webapps"):
        return isinstance(net, dict)
    if id == "databases":
        return _has(net, "dbs")
    if id == "tailscale":
        return _has(net, "ts_peers")
    if id == "containers":
        return isinstance(ctx.cont, dict) and (bool(ctx.cont.get("absent")) or isinstance(ctx.cont.get("containers"), list))
    if id == "boot":
        return isinstance(ctx.boot, dict)
    if id == "docker_disk":
        return _has(ctx.boot, "docker_df")
    if id == "system":
        return bool(s) and not (s.get("mem") is None and s.get("disk_root") is None and not s.get("cpu"))
    if id == "network_traffic":
        return s.get("net") is not None
    if id == "sessions":
        return s.get("sessions") is not None
    if id == "disks":
        return s.get("fs") is not None
    return True


def _quiet(id, ctx):
    """True when the card has nothing to show on purpose (not an error, not a warning)."""
    s = ctx.s if isinstance(ctx.s, dict) else {}
    if id == "containers":
        return isinstance(ctx.cont, dict) and bool(ctx.cont.get("absent"))
    if id == "network_traffic":
        return not s.get("net")
    if id == "disks":
        return not s.get("fs")
    return False


def card_state(id, ctx):
    """The state of card `id` in this frame (see the top of this module)."""
    if id == "attention":
        return problem_state(id, ctx)
    if not _readable(id, ctx):
        return "unknown"
    st = problem_state(id, ctx)
    return "info" if st == "ok" and _quiet(id, ctx) else st


# ---- the registry of cards ----------------------------------------------------------------------------------------------------

class Entry(object):
    __slots__ = ("id", "title", "feature", "builder")

    def __init__(self, id, title, feature, builder):
        self.id, self.title, self.feature, self.builder = id, title, feature, builder


CARDS = {}  # id -> Entry, in registration order


def register(id, title, feature, builder):
    """Registers the builder of card `id`: builder(ctx, k, caps) -> ui.Card. feature: True (always) or the [features] name that switches
    the card off in config.ini. A second registration of an id replaces the first (a module may be imported under two names)."""
    CARDS[id] = Entry(id, title, feature, builder)
    return builder


def enabled(id, cfg=None):
    """False when the card's feature is switched off in config.ini ([features]); a card that is not registered is not enabled."""
    entry = CARDS.get(id)
    if entry is None:
        return False
    cfg = nuc_config.current() if cfg is None else cfg
    return entry.feature is True or bool(cfg["features"].get(entry.feature, True))


def ids(cfg=None):
    """The ids of the cards that are registered and not switched off, in registration order."""
    return [i for i in CARDS if enabled(i, cfg)]


def raw_card(id, ctx, lines, note=""):
    """A Card whose body is the lines the console has always drawn for the section (ui.Raw), with the state of this frame."""
    return Card(id, CARDS[id].title, note, card_state(id, ctx), [Raw(lines)])


def build(id, ctx, k, caps):
    """The Card `id` at detail level k, within caps. Remembered for the frame by (id, k, whether its caps are lifted, full, width), and
    the sections it hid items of are told to caps.trunc again when it is not built but remembered."""
    key = ("card", id, k) + caps.key(id)
    hit = ctx.memo.get(key)
    if hit is None:
        before = set(caps.trunc)
        caps.trunc.clear()
        try:
            card = CARDS[id].builder(ctx, k, caps)
        finally:
            added = frozenset(caps.trunc)
            caps.trunc.update(before)
        card.truncated = id in added
        hit = ctx.memo[key] = (card, added)
    caps.trunc.update(hit[1])
    return hit[0]


# ---- the cards built of components --------------------------------------------------------------------------------------------
# A native card is data -> components: the builder reads the Ctx, honours the detail level k and the caps (caps.lim, caps.opened) and
# returns a ui.Card whose body is Span / Line / Msg / Table / Wrap / More..., with no colour and no column arithmetic beyond the widths
# of the columns. The console draws it with ansi.card_lines (title, then body), the web with htmlview.html. NATIVE holds them: the
# registration order of CARDS is the sections' (render.py registers each id in turn, from here when the card is native, else a Raw one).

NET_STALE_S = 120    # the net collector's data older than this is stale (seconds)
BOOT_STALE_S = 900   # the boot collector's
NATIVE = {}          # id -> (title, feature, builder): what register() takes


def native(id, title, feature):
    """Decorator: a card builder fn(ctx, k, caps) -> ui.Card. The decorated function is the builder as it is (an error in it is for the
    caller); what is registered is the same builder wrapped so that a broken one becomes the card's title and an 'err' message, never an
    empty screen."""
    def deco(fn):
        def guarded(ctx, k, caps):
            try:
                return fn(ctx, k, caps)
            except Exception as e:  # noqa: BLE001 - a broken card must not empty the screen
                return Card(id, title, "", card_state(id, ctx), [Msg("err", ui.safe(repr(e))[:60])])
        NATIVE[id] = (title, feature, guarded)
        return fn
    return deco


def _now(ctx):
    return ctx.now if ui.num(ctx.now) is not None else time.time()


def is_absent(d, key):
    """True if the collector recorded that the tool for that section is not installed (not an error)."""
    return isinstance(d, dict) and key in (d.get("absent") or [])


def is_disabled(d, key):
    """True if the section was switched off in config.ini (the collector skipped it on purpose)."""
    return isinstance(d, dict) and key in (d.get("disabled") or [])


def unavail(d, key, prefix="unavailable"):
    """The message of a section without data: 'not installed' (info) if the tool is missing, 'unavailable: error' if it is broken."""
    if is_disabled(d, key):
        return Msg("info", "disabled in config.ini")
    if isinstance(d, dict) and key in (d.get("unsupported") or []):
        return Msg("info", "not available on this OS")
    if isinstance(d, dict) and key in (d.get("notes") or {}):
        return Msg("info", ui.safe(d["notes"][key])[:70])
    if is_absent(d, key):
        return Msg("info", "not installed on this machine")
    return Msg("warn", f"{prefix}: " + ui.safe(((d or {}).get("errors") or {}).get(key, "collector needs updating"))[:60])


def _frac(part, whole):
    """part/whole, or None when either is not a number or the whole is nothing: a bar that could not be read is '?'."""
    part, whole = ui.num(part), ui.num(whole)
    return part / whole if part is not None and whole else None


def _native_card(id, ctx, note, body, hidden=None):
    """The Card: the title of the registry, the state of the frame. hidden: the More the card shows (kept on the card for a page that
    links to all of it)."""
    return Card(id, NATIVE[id][0], note, card_state(id, ctx), body, more=hidden)


@native("disks", "DISKS", "disks")
def disks_card(ctx, k, caps):
    s = ctx.s if isinstance(ctx.s, dict) else {}
    fs = s.get("fs")
    if not fs:
        return _native_card("disks", ctx, "", [Msg("info" if fs == [] else "warn", "no filesystems" if fs == [] else "unavailable")])
    bw = max(8, min(30, caps.width - 42))
    shown = caps.lim(fs, 5, "disks")
    rows = [Row([Span(ui.safe(f["mount"])[:14]), Bar(_frac(f["used"], f["total"]), f"{ui.human(f['used'])}/{ui.human(f['total'])}", w=bw)])
            for f in shown]
    body = [Table([Col("mount", "Mount", w=14), Col("usage", "Used", num=True)], rows)]
    more = More(len(fs) - len(shown), "more") if len(fs) > len(shown) else None
    return _native_card("disks", ctx, "", body + ([more] if more else []), more)


@native("docker_disk", "DOCKER · DISK", "docker_disk")
def docker_disk_card(ctx, k, caps):
    boot, now = ctx.boot, _now(ctx)
    note = f"stale data ({ui.fmt_ago(now - boot['ts'])} old)" if boot and now - boot.get("ts", now) > BOOT_STALE_S else ""
    df = (boot or {}).get("docker_df")
    if df is None:
        return _native_card("docker_disk", ctx, note, [unavail(boot, "docker_df")])
    body = []
    if df["rows"]:
        cols = [Col("type", "Type", w=14, gap=0), Col("count", "Count", "r", num=True, w=4, wprio=3), Col("active", "In use", gap=2, num=True, wprio=2),
                Col("size", "Size", num=True, w=9), Col("unused", "Unused", wprio=1)]
        body.append(Table(cols, [Row([r["type"], str(r["count"]), f"({r['active']} in use)", r["size"], f"unused {r['reclaimable']}"])
                                 for r in df["rows"]]))
    dang = df.get("dangling_images")
    if dang is not None:
        body.append(Line([Span(f" dangling images: {dang['count']} ({ui.human(dang['bytes']) if dang['bytes'] else '0B'}): safe to prune",
                               "warn" if dang["bytes"] else "muted")]))
    if df.get("volumes_unused"):
        anon = df.get("volumes_unused_anonymous") or 0
        body.append(Line([Span(f" {df['volumes_unused']} unused volumes ({anon} anonymous): may hold data, check before pruning", "warn")]))
    body.append(Line([Span(" unused = no container uses it; tagged images can be re-pulled", "muted")]))
    return _native_card("docker_disk", ctx, note, body)


@native("sessions", "SESSIONS", "sessions")
def sessions_card(ctx, k, caps):
    s = ctx.s if isinstance(ctx.s, dict) else {}
    sess = s.get("sessions")
    if sess is None:
        return _native_card("sessions", ctx, "", [Msg("warn", "unavailable")])
    remote = [ip for ip in sess["ssh"] if not is_private_addr(ip)]
    body = [Line([f" {ui.plural(len(sess['local']), 'user session')}   ssh: ",
                  Span(f"{len(sess['ssh'])} connected", "err" if remote else "ok") if sess["ssh"] else Span("none", "muted")])]
    shown = caps.lim(sess["ssh"], 4, "sessions")
    for ip in shown:
        far = ip in remote
        body.append(Line([" ", Span("✖" if far else "●", "err" if far else "ok"), f" ssh from {ip}  ",
                          Span("address NOT local or Tailscale", "err") if far else Span("LAN or Tailscale", "muted")]))
    more = More(len(sess["ssh"]) - len(shown), "more ssh clients") if len(sess["ssh"]) > len(shown) else None
    if more:
        body.append(more)
    for key, label in (("rdp", "remote desktop"), ("vnc", "screen sharing")):  # Windows RDP, macOS Screen Sharing
        peers = sess.get(key) or []
        if peers:
            far = any(not is_private_addr(ip) for ip in peers)
            body.append(Line([" ", Span("✖" if far else "●", "err" if far else "ok"), f" {label} from {', '.join(peers[:3])}",
                              Span("  address NOT local or Tailscale", "err") if far else Span("  LAN or Tailscale", "muted")]))
    ttys = sorted({x["tty"] for x in sess["local"] if x["tty"]})
    if ttys:
        body.append(Wrap(ttys, sep=" ", max_lines=None if caps.opened("sessions") else 1, indent=1))
    return _native_card("sessions", ctx, "", body, more)


@native("tailscale", "TAILSCALE", "tailscale")
def tailscale_card(ctx, k, caps):
    net = ctx.net
    ts = (net or {}).get("ts_peers")
    if ts is None:
        return _native_card("tailscale", ctx, "", [unavail(net, "ts_peers")])
    peers, me = ts["peers"], ts["self"]
    online, now = sum(p["online"] for p in peers), _now(ctx)
    stale = now - net.get("ts", now) > NET_STALE_S
    note = (f"{ui.safe(me['name'])} · {online}/{len(peers)} nodes online" + (" · exit node" if me["exit_option"] else "")
            + (f" · stale data ({ui.fmt_ago(now - net['ts'])} old)" if stale else ""))
    shown = caps.lim(peers, 8, "tailscale")
    rows = []
    for p in shown:
        if p["online"]:
            state = [Span("online ", "ok"), Span("direct" if p["direct"] else f"via relay {p['relay']}", "muted")]
        else:
            state = [Span("offline · " + (f"seen {ui.fmt_ago(now - p['last_seen'])} ago" if p["last_seen"] else "never seen"), "muted")]
        if p["exit"]:
            state.append(Span("  exit node in use", "warn"))
        rows.append(Row([Span("●" if p["online"] else "○", "ok" if p["online"] else "muted"), ui.safe(p["name"])[:18], ui.safe(p["os"])[:8],
                         Line(state)]))
    cols = [Col("dot", "", w=1), Col("name", "Node", w=18), Col("os", "OS", prio=2, w=8), Col("state", "State")]
    more = More(len(peers) - len(shown), "nodes") if len(peers) > len(shown) else None
    return _native_card("tailscale", ctx, note, [Table(cols, rows)] + ([more] if more else []), more)


@native("network_traffic", "NETWORK TRAFFIC", "network_traffic")
def network_traffic_card(ctx, k, caps):
    s = ctx.s if isinstance(ctx.s, dict) else {}
    nets, note = s.get("net"), "↓ received · ↑ sent"
    if not nets:
        return _native_card("network_traffic", ctx, note, [Msg("info", "no interfaces")])
    sw = 12 if caps.width >= 100 else 8  # a shorter sparkline in a narrow column: the line must not be cut
    ranked = sorted(nets.items(), key=lambda kv: -(kv[1]["rx_tot"] + kv[1]["tx_tot"]))
    shown = caps.lim(ranked, 5, "network_traffic")
    rows = []
    for name, v in shown:
        rows.append(Row([ui.safe(name)[:11], Line([Span("↓", "ok"), " ", ui.fmt_rate(v["rx"])]), Spark(v["hist_rx"], sw),
                         Line([Span("↑", "accent"), " ", ui.fmt_rate(v["tx"])]), Spark(v["hist_tx"], sw),
                         Span(f" ↓{ui.human(v['rx_tot'])} ↑{ui.human(v['tx_tot'])}", "muted")]))
    cols = [Col("iface", "Interface", w=11), Col("rx", "Received", num=True, w=12, gap=0), Col("rx_hist", "", gap=1, wprio=3),
            Col("tx", "Sent", num=True, w=12, gap=0, wprio=1), Col("tx_hist", "", gap=0, wprio=3), Col("total", "Total", prio=0, wprio=2)]
    more = More(len(nets) - len(shown), "more") if len(nets) > len(shown) else None
    return _native_card("network_traffic", ctx, note, [Table(cols, rows)] + ([more] if more else []), more)


# ---- system, containers, databases and boot ------------------------------------------------------------------------------------
# (PR 14.) The same lines the console has always drawn for these sections, as components: the Bars carry their values, the cores are a
# Grid of Bars, a container stack is a Group of a header and a Wrap of chips (a Span with the state's tone and symbol), the databases a
# NoteTable (a row each, the 'in use now' and 'external clients' facts under it as a Flow), the boot a Timeline and Tables. The figures
# that more than one of them read (up/load, RAM, the container's name) are here.

BOOT_LABELS = {  # the same BOOT blocks speak of systemd units, Windows services or launchd daemons depending on who wrote boot.json
    "linux": {"failed_one": "failed systemd unit", "failed_short": "failed unit", "journal_in": " in this boot's journal",
              "failed_title": "FAILED UNITS", "enabled_title": "SERVICES ENABLED AT BOOT", "journal_title": "BOOT JOURNAL",
              "journal_short": "journal", "kernel": "kernel "},
    "windows": {"failed_one": "failed service", "failed_short": "failed service", "journal_in": " in the System event log since boot",
                "failed_title": "FAILED SERVICES", "enabled_title": "AUTOMATIC SERVICES", "journal_title": "SYSTEM EVENT LOG",
                "journal_short": "events", "kernel": ""},
    "darwin": {"failed_one": "failed launch daemon", "failed_short": "failed daemon", "journal_in": " in the system log",
               "failed_title": "FAILED LAUNCH DAEMONS", "enabled_title": "LAUNCH DAEMONS (third-party)", "journal_title": "SYSTEM LOG",
               "journal_short": "log", "kernel": ""},
}


_BLOCKS = "▁▂▃▄▅▆▇█"  # one character per core, by load (the console's sparkline glyphs)


def boot_labels(b):
    return BOOT_LABELS.get(os_of(b), BOOT_LABELS["linux"])


def unsupported(d, key):
    """The collector of this OS has no such section (e.g. systemd-analyze blame on Windows): leave the block out."""
    return isinstance(d, dict) and key in (d.get("unsupported") or [])


def fmt_up(sec):
    """'5d 0h', or '?' when the uptime is not known."""
    return "?" if sec is None else ui.fmt_dur(sec)


def fmt_load(load):
    """The three load averages, '?' when they could not be read; '' (nothing to say) where the OS has none ([])."""
    return "" if load == [] else "?" if load is None else " ".join(load)


def up_load_note(up, load):
    """'up 5d 0h · load 0.82 0.64 0.51': ? for what could not be read (None), no load at all where the OS has none ([])."""
    return f"up {fmt_up(up)}" + (f" · load {fmt_load(load)}" if load != [] else "")


def ram_figures(m):
    """(used, total) bytes of the RAM from a Sampler's "mem": None when it does not say (an old kernel without MemAvailable, no data)."""
    try:
        total, avail = m["MemTotal"], m["MemAvailable"]
        return (total - avail, total) if total else None
    except (KeyError, TypeError):
        return None


def disk_figures(d):
    """(used, total, label) of a Sampler's "disk_root": None when it is missing or empty."""
    try:
        used, total, label = d
        return (used, total, label) if total else None
    except (TypeError, ValueError):
        return None


def short_name(name, project=""):
    """'ethibid-api-1' -> 'api' (without the stack prefix and the replica index)."""
    n = ui.safe(name)
    if project and n.startswith(project + "-"):
        n = n[len(project) + 1:]
    return re.sub(r"-\d+$", "", n)


def ct_ok(ct):
    return ct["state"] == "running" and "unhealthy" not in ct["status"] and "Restarting" not in ct["status"]


def fmt_db_port(p):
    if p["c"] == "host":
        return "host network"
    pre = "lo:" if p["s"] == "lo" else "*" if p["s"] == "*" else p["s"] + ":"
    return f"{pre}{p['p'] or '?'}" + ("/u" if p["c"].endswith("/udp") else "")


def _fits(lead, items, sep, w):
    """The items (Spans / Lines) that fit in w columns after lead, joined by sep: trailing ones are dropped, one is always kept."""
    items = list(items)
    while len(items) > 1 and len(lead + sep.join(x.text for x in items)) > w:
        items.pop()
    return items


def _joined(lead, items, sep):
    spans = [lead] if lead else []
    for n, x in enumerate(items):
        spans += ([sep] if n else []) + (x.spans if isinstance(x, Line) else [x])
    return Line(spans)


def thermal_parts(th, bw, maxw=None):
    """Temperatures with a Bar and its limits (RAM style) + the throttling time, as Lines. Scale and thresholds come from the sensor. When
    the line is wider than maxw the clock goes first, then the limits. A temperature that is not a number is '?'."""
    out = []
    for label, key in (("TEMP", "cpu"), ("NVMe", "nvme")):
        if key not in th:
            continue
        try:
            t, mx = (ui.num(x) for x in th[key])
        except (TypeError, ValueError):
            t = mx = None
        if t is None or not mx:  # not a reading: '?', never fine (and no limits to work out)
            out.append(Line([f" {label:<5} ", Span("?", "unknown")]))
            continue
        val = f"{t:.0f}°C/{mx:.0f}°C"
        clk = f"   clock {th['clk'][0]:.1f}/{th['clk'][1]:.1f} GHz" if key == "cpu" and th.get("clk") else ""
        limits = f"   limits {ui.THERMAL_WARN * mx:.0f}/{ui.THERMAL_ERR * mx:.0f}°C"
        head = 1 + 5 + 1 + bw + 1 + len(val)
        extra = next((x for x in (limits + clk, limits) if maxw is None or head + len(x) <= maxw), "")
        out.append(Line([f" {label:<5} ", Bar(_frac(t, mx), val, ui.THERMAL_WARN, ui.THERMAL_ERR, bw)] + ([extra] if extra else [])))
    if th.get("throttle_s") is not None:
        rec = th.get("recent")
        state = (Span(f"✖ THROTTLING now (+{rec} events/min)", "err") if rec else
                 Span("✔ none in the last minute", "ok") if rec == 0 else Span("measuring", "muted"))
        out.append(Line([" ", Span("throt", "muted"), f" {ui.fmt_min(th['throttle_s'])} total since boot   ", state]))
    return out


@native("system", "SYSTEM", True)
def system_card(ctx, k, caps):
    s = ctx.s if isinstance(ctx.s, dict) else {}
    m, disk, w = s.get("mem"), disk_figures(s.get("disk_root")), caps.width
    bw = max(8, min(40, w - 52))
    ram = ram_figures(m)
    body = [Line([" RAM   ", Bar(_frac(ram[0], ram[1]), f"{ui.human(ram[0])}/{ui.human(ram[1])}   cache {ui.human(m.get('Cached'))}", w=bw)])
            if ram else Line([" RAM   ", Span("?", "unknown")]),
            Line([" DISK  ", Bar(_frac(disk[0], disk[1]), f"{ui.human(disk[0])}/{ui.human(disk[1])}", w=bw)])
            if disk else Line([" DISK  ", Span("?", "unknown")])]
    if ctx.cfg["features"].get("thermal", True):  # (the sampler leaves it empty when the feature is off; the demo's is not, so the switch is honoured here too)
        body += thermal_parts(s.get("thermal") or {}, bw, w)
    cores = [v for _, v in sorted(s["cpu"].items(), key=lambda kv: int(kv[0][3:]))]
    if cores:
        mean = sum(cores) / len(cores)
        if k <= 2:  # one bar per core, in columns: the normal look; only the tiny-console levels (k>=3) compress to one character per core
            body.append(Line([" CPU   ", Bar(mean, f"{mean * 100:.0f}%   {len(cores)} threads", w=bw)]))
            body.append(Grid([Line([f" {i:>2} ", Bar(v, f"{v * 100:3.0f}%", w=9)]) for i, v in enumerate(cores)], 20, max(1, min(6, (w - 1) // 20))))
        else:
            spark = [Span(_BLOCKS[min(7, int(v * 8))], "ok" if v < 0.7 else "warn" if v < 0.9 else "err") for v in cores]
            body.append(Line([" CPU   ", Bar(mean, f"{mean * 100:.0f}%", w=bw), "   core "] + spark))
    cont = ctx.cont
    if k <= -2 and cont:
        top = caps.lim(sorted((ct for ct in cont["containers"] if ct["mem"]), key=lambda ct: -ct["mem"]), 5, "system")
        if top:
            mx = top[0]["mem"]
            body += [Line([]), Line([Span(" HEAVIEST CONTAINERS (RAM)", "strong")]),
                     Table([Col("name", "Container", w=35, gap=0), Col("mem", "RAM", num=True, w=7, gap=0), Col("share", "", wprio=2)],
                           [Row([ui.safe(ct["name"])[:34], ui.human(ct["mem"]), Bar(ct["mem"] / mx, "", 2, 2, 20)]) for ct in top])]
    return _native_card("system", ctx, up_load_note(s.get("uptime"), s.get("load")), body)


def stack_parts(cont, cap, caps):
    """For each stack: a Group of a header with counts and RAM, then its services as chips (a Wrap) with a status mark."""
    groups = {}
    for ct in cont["containers"]:
        groups.setdefault(ui.safe(ct["project"]), []).append(ct)
    out = []
    for proj in sorted(groups, key=lambda x: (x == "", x)):
        cts = sorted(groups[proj], key=lambda x: x["name"])
        bad = sum(not ct_ok(ct) for ct in cts)
        head = Line([" ", Span("▸", "accent_strong"), " ", Span(f"{(proj or '(no stack)')[:26]:<27}", "strong"),
                     f"{len(cts) - bad}/{len(cts)} running   RAM {ui.human(sum(ct['mem'] or 0 for ct in cts))}"]
                    + ([Span(f"   ✖ {bad}", "err")] if bad else []))
        chips = [Line([Span("●", "ok") if ct_ok(ct) else Span("✖", "err"), " " + short_name(ct["name"], proj)]) for ct in cts]
        out.append(Group([head, Wrap(chips, sep="   ", max_lines=None if caps.opened("containers") else cap, indent=5)]))
    return out


@native("containers", "CONTAINER", "containers")
def containers_card(ctx, k, caps):
    cont = ctx.cont
    if cont is None:
        return _native_card("containers", ctx, "", [Msg("err", "container collector not running")])
    if cont.get("absent"):
        return _native_card("containers", ctx, "", [Msg("info", "docker not installed on this machine")])
    cs = cont.get("containers")
    if not isinstance(cs, list):
        return _native_card("containers", ctx, "", [Msg("warn", "unavailable")])
    down = [x for x in cs if x["state"] != "running"]
    sick = [x for x in cs if x["state"] == "running" and not ct_ok(x)]
    ok = len(cs) - len(down) - len(sick)
    body = [Line([f" {ui.plural(len(cs), 'container')}   RAM {ui.human(sum(x['mem'] or 0 for x in cs))}   ", Span("●", "ok"), f" {ok} ok"]
                 + (["   ", Span(f"✖ {len(down)} stopped", "err")] if down else []) + (["   ", Span(f"✖ {len(sick)} unhealthy", "err")] if sick else []))]
    if k <= 2:  # detail: which stacks and which services are running
        return _native_card("containers", ctx, "", body + stack_parts(cont, {-2: 4, -1: 3, 0: 3, 1: 2, 2: 1}[k], caps))
    groups = {}
    for x in cs:
        name = ui.safe(x["project"]) or "(standalone)"
        groups[name] = groups.get(name, 0) + 1
    if k < 4:
        body.append(Wrap([f"{g} {n}" for g, n in sorted(groups.items())], max_lines=None if caps.opened("containers") else 1, indent=1))
    for x in (sick + down)[:max(0, 4 - k)]:
        body.append(Line([" ", Span("✖", "err"), f" {ui.safe(x['name'])[:36]:<37}", Span(ui.safe(x["status"])[:30], "muted")]))
    return _native_card("containers", ctx, "", body)


@native("databases", "DATABASE", "databases")
def databases_card(ctx, k, caps):
    net, w = ctx.net, caps.width
    if net is None:
        return _native_card("databases", ctx, "", [Msg("err", "network collector not running")])
    dbs = net.get("dbs")
    if dbs is None:
        return _native_card("databases", ctx, "", [unavail(net, "dbs")])
    items = dbs["items"]
    if not items:
        return _native_card("databases", ctx, "", [Msg("info", "no databases running")])
    exposed = lambda it: it["host_net"] or any(p["s"] != "lo" for p in it["ports"])  # noqa: E731
    open_n = sum(bool(exposed(it)) for it in items)
    note = f"{len(items)} running" + (f" · {open_n} exposed" if open_n else "")
    now = _now(ctx)
    kw = min(14, max(len(ui.safe(it["kind"])) for it in items) + 1)
    txt = [" ".join(fmt_db_port(p) for p in it["ports"]) or "docker net only" for it in items]
    pw = min(28, max(len(t) for t in txt) + 2)
    rows, notes = [], []
    for it, ports in zip(items, txt):
        ex = bool(exposed(it))
        rows.append(Row([Span("●", "err" if ex else "ok"), Span(f"{ui.safe(it['kind']):<{kw}}", "strong"), ui.safe(it["name"])[:30], ports,
                         Span("exposed", "err") if ex else Span("local only", "muted")]))
        if k > 1:
            notes.append([])
            continue
        proj = ui.safe(it["project"])
        names = lambda lst: ", ".join(short_name(n, proj) for n in lst)  # noqa: E731
        who = []
        if it["active"]:
            who.append(Line([Span("in use now: ", "ok"), names(it["active"])]))
        declared = [n for n in it["usano"] if n not in it["active"]]
        if declared:
            who.append(Span("declared by " + names(declared)))
        if it["stessa_rete"]:
            who.append(Span("same network: " + names(it["stessa_rete"]), "muted"))
        if it["host_clients"]:
            who.append(Span("host processes: " + ", ".join(ui.safe(x) for x in it["host_clients"])))
        if not who:
            who.append(Line([Span("no known service", "warn"), Span(" (bridge: cannot tell)", "muted")]))
        ext = it["external"]
        if ext and now - ext[0]["last"] < 900:
            who.append(Span("external clients now: " + ", ".join(ui.safe(e["ip"]) for e in ext if now - e["last"] < 900), "err"))
        elif ext:
            who.append(Span(f"last external client {ui.safe(ext[0]['ip'])} {ui.fmt_ago(now - ext[0]['last'])} ago", "warn"))
        elif it["ext_source"] == "netns":
            who.append(Span(f"no external client seen in {ui.fmt_ago(now - dbs['since'])}", "muted"))
        else:
            who.append(Span("external clients: not detectable", "warn"))
        notes.append([Flow(who, Span("→", "muted"), 8, "   ", w - 14)])  # wrapped, never silently cut
    cols = [Col("dot", "", w=1), Col("kind", "Kind", gap=0), Col("name", "Name", w=31, gap=0), Col("ports", "Ports", num=True, w=pw, gap=0, wprio=2),
            Col("reach", "Reach")]
    return _native_card("databases", ctx, note, [NoteTable(cols, rows, notes)])


def boot_start_parts(b, up, w):
    """The start of a boot: what it took (the Timeline of its stages and its legend), without the title line (the card has it)."""
    an, lbl = b.get("analyze"), boot_labels(b)
    out = [Line([])]
    if an is None:
        head = [Line([f"   {ui.safe(b.get('kernel', '?'))}   up {ui.fmt_dur(up)}"])] if os_of(b) != "linux" else []
        return out + head + [unavail(b, "analyze", "boot times unavailable")]
    total = max(an["total"], 0.001)
    out.append(Line(["   boot finished in ", Span(ui.fmt_s(total), "strong"), f"   {lbl['kernel']}{ui.safe(b.get('kernel', '?'))}   up {ui.fmt_dur(up)}"]))
    return out + [Timeline(list(an["parts"].items()), total, max(20, min(w - 8, 80)))]


def boot_slowest_parts(b, caps, k):
    """The slowest units: a Head, then a Table of unit, a Bar of its share of the slowest and its time."""
    out = [Head("SLOWEST UNITS", "activation time: not all of them block boot"), Line([])]
    bl = b.get("blame")
    if bl is None:
        return out + [unavail(b, "blame")]
    top = caps.lim(bl, k, "boot")
    mx = max((x["s"] for x in top), default=1) or 1
    rows = [Row([ui.safe(x["unit"])[:38], Bar(max(1, round(20 * x["s"] / mx)) / 20, "", tone="accent", w=20),
                 Span(ui.fmt_s(x["s"]), "warn" if x["s"] >= 5 else None)]) for x in top]
    return out + [Indent([Table([Col("unit", "Unit", w=39, gap=0), Col("share", "", gap=2, wprio=2), Col("time", "Time", "r", num=True)], rows)], 2)]


def boot_journal_parts(b, caps, k, w):
    """The boot journal: errors and warnings counted, then its most frequent entries."""
    j = b.get("journal")
    out = [Head(boot_labels(b)["journal_title"], "warning and worse"), Line([])]
    if j is None:
        return out + [unavail(b, "journal")]
    cap = " (last 500)" if j.get("capped") else ""
    out.append(Line(["   ", Span(str(j["err"]), "err") if j["err"] else "0", " errors   ", Span(str(j["warn"]), "warn") if j["warn"] else "0",
                     f" warning{cap}"]))
    room = max(20, w - 45)
    rows = [Row([Span("●", "err" if e["pr"] <= 3 else "warn"), ui.safe(e["id"])[:26], f"{e['n']}×", Span(ui.safe(e["last"])[:room], "muted")])
            for e in caps.lim(j["top"], k, "boot")]
    return out + [Indent([Table([Col("dot", "", w=1), Col("id", "Entry", w=27, gap=0), Col("n", "Count", "r", num=True, w=5, gap=2),
                                 Col("last", "Last", wprio=1)], rows)], 2)]


@native("boot", "BOOT", "boot")
def boot_card(ctx, k, caps):
    b, w = ctx.boot, caps.width
    if k <= -2 and b is not None:  # the full boot detail only if there really is room to spare
        up = _now(ctx) - b.get("btime", time.time())
        body = boot_start_parts(b, up, w)
        for key, blk in (("blame", lambda: boot_slowest_parts(b, caps, 5)), ("journal", lambda: boot_journal_parts(b, caps, 4, w))):
            if not unsupported(b, key):  # macOS/Windows: no empty "not available" blocks
                body += [Line([])] + blk()
        return _native_card("boot", ctx, "", body)
    if b is None:
        return _native_card("boot", ctx, "", [Msg("warn", "boot collector not running")])
    an, j, failed, lbl = b.get("analyze"), b.get("journal"), b.get("failed"), boot_labels(b)
    bits = []
    if an:
        bits.append(Line(["finished in ", Span(ui.fmt_s(an["total"]), "strong")]))
    if failed is not None:
        bits.append(Span(f"✖ {ui.plural(len(failed), lbl['failed_short'])}", "err") if failed else Span(f"✔ 0 {lbl['failed_short']}s", "ok"))
    if j:
        bits.append(Line([f"{lbl['journal_short']} ", Span(str(j["err"]), "err") if j["err"] else "0", " err · ",
                          Span(str(j["warn"]), "warn") if j["warn"] else "0", " warn"]))
    body = [_joined(" ", _fits(" ", bits, "   ", w), "   ")]
    if b.get("blame") and k < 3:
        top = [f"{x['unit'].replace('.service', '')} {ui.fmt_s(x['s'])}" for x in b["blame"][:max(1, 3 - k)]]
        top = _fits(" slowest: ", [Span(x) for x in top], "  ·  ", w)
        body.append(Line([" slowest: ", Span("  ·  ".join(x.text for x in top), "muted")]))
    return _native_card("boot", ctx, "", body)


# ---- ATTENTION, EXPOSURE, FIREWALL, WEB APPS ----------------------------------------------------------------------------------
# What the console draws of these four is the same as ever, byte for byte; what the web has more room for rides on fields the console does
# not draw: Problem (id, why, fix, accept), Details.brief (the accepted ones), Span.full (a name the console had to cut), Hint.

NAMEW = 36                                   # the widest service name of the exposure matrix
ADVICE_PROBLEMS_CMD = "nuc-console-problems"  # a problem list that did not come from render.problems() has no commands of its own
LEGEND = "   ● open   ◐ filtered by source   ? unknown (treated as open)   · no"
REACH_LABEL = {"INTERNET": "Internet", "LAN": "LAN+tailnet", "TAILNET": "tailnet", "LOCALE": "local only"}
REACH_TONE = {"INTERNET": "err", "LAN": "warn", "TAILNET": None, "LOCALE": "muted"}


def _ago(now, ts):
    """'3 h ago', or '' when ts is not a time."""
    ts = ui.num(ts)
    return "" if ts is None else ui.fmt_ago(now - ts) + " ago"


@native("attention", "ATTENTION", True)
def attention_card(ctx, k, caps):
    pb = ctx.problems if ctx.problems is not None else []
    shown = caps.lim(pb, max(3, 6 - k), "attention")
    info, pids, cmds = getattr(pb, "info", None), getattr(pb, "pids", None), getattr(pb, "cmds", None) or {}
    explained, named = info is not None and len(info) == len(pb), pids is not None and len(pids) == len(pb)
    body, probs = [], []
    for i, item in enumerate(shown):
        title, why, fix, accept = info[i] if explained else ("", "", "", "")
        probs.append(ui.Problem("err" if item[0] >= 2 else "warn", item[1], pids[i] if named else "", title, why, fix, accept))
    body += probs or [Msg("ok", "no problems detected")]
    more = More(len(pb) - len(shown), "more", indent=3) if len(pb) > len(shown) else None
    if more:
        body.append(more)
    acc = getattr(pb, "accepted", 0)
    if acc:
        now, forget = _now(ctx), cmds.get("forget", "")
        known = [ui.Accepted(x["text"], x["id"], x["reason"], _ago(now, x.get("ts")), f"{forget} {x['id']}" if forget else "")
                 for x in getattr(pb, "known", None) or []]
        body.append(ui.Details(f"{acc} accepted", known, brief=Line([Span(f"   · {acc} accepted as known ({cmds.get('problems') or ADVICE_PROBLEMS_CMD})",
                                                                          "muted")])))
    if cmds:
        body.append(ui.Hint("list with why and fix", cmds["problems"]))
        if any(p.accept for p in probs):
            body.append(ui.Hint("accept a known one", f'{cmds["accept"]} --problem <id> --reason "..."'))
        if any(p.id and not p.accept for p in probs):
            body.append(ui.Hint("a port change is accepted with the baseline", cmds["accept"]))
    return _native_card("attention", ctx, "", body, more)


# -- exposure

def _cell(v, warn=False, net=False, loc=False):
    """One cell of the matrix: 0 closed, 1 open, 2 filtered by source, 3 unknown (warn: a database; net: the Internet column; loc: the
    'this machine' column, where open is not an alarm)."""
    if loc:
        return Span("●", "neutral") if v else Span("·", "muted")
    if v == 3:
        return Span("?", "unknown")
    if net:
        return Span("●", "err_strong") if v else Span("·", "muted")
    if v == 1:
        return Span("●", "err" if warn else "warn")
    return Span("◐", "accent") if v == 2 else Span("·", "muted")


def exposure_full(ctx, caps, rows):
    """The whole matrix, for a card with room (k < 0) and the Network page: the counts, the legend, then the rows of the groups that have
    any (a Table with its groups) and the ports that are only local as a compact list."""
    w, new = caps.width, ctx.new or {}
    by = {g: [r for r in rows if group_of(r) == g] for g, _ in GROUPS}
    titles = dict(GROUPS)
    warn, ni, nl = sum(r["warn"] for r in rows), len(by["INTERNET"]), len(by["LAN"])
    count = Line(["   Internet ", Span(str(ni), "err_strong") if ni else "0", "    LAN ", Span(str(nl), "warn") if nl else "0",
                  f"    Tailscale {sum(r['ts'] == 1 for r in rows)}    Local {len(by['LOCALE'])}"])
    alert = f"⚠ {warn} DB/broker open on LAN"
    if not warn or len(count.text) + 8 + len(alert) <= w:
        summary = [Line(count.spans + ["        "] + ([Span(alert, "err")] if warn else []))]
    else:
        summary = [count, Line(["   ", Span(alert, "err")])]
    # the console draws the counts and the legend as padded text; the web, where spaces collapse, as a row of counters and a list of symbols
    sym = [("●", "err_strong", "open"), ("◐", "accent", "filtered by source"), ("?", "unknown", "unknown (treated as open)"), ("·", "muted", "no")]
    counters = [Line(["Internet ", Span(str(ni), "err_strong", bold=True) if ni else "0"]), Line(["LAN ", Span(str(nl), "warn", bold=True) if nl else "0"]),
                f"Tailscale {sum(r['ts'] == 1 for r in rows)}", f"Local {len(by['LOCALE'])}"] + ([Span(alert, "err")] if warn else [])
    body = [Only("console", [Line()] + summary + [Line([Span(LEGEND, "muted")], clip=w), Line()]),
            Only("web", [Wrap(counters, sep="    ", indent=3, flat=True), Wrap([Line([Span(a, b), " " + c]) for a, b, c in sym], sep="   ", indent=3, flat=True)])]
    nw = max(12, min(NAMEW, w - 57))  # narrow column (3 columns): the name gets shorter, notes and cells stay visible
    declared_ports = {p for ps in ctx.cfg["webapps"].values() for p in ps if p not in SENSITIVE}
    trows, groups = [], []
    for g, title in GROUPS:
        if g == "LOCALE" or not by[g]:
            continue
        groups.append((title, len(trows)))
        for r in by[g]:
            tag = new.get(f"{r['port']}/{r['proto'][0]}:{g}")
            declared = r["proto"] == "tcp" and r["port"] in declared_ports
            room = w - (37 + nw) - (len(tag) + 1 if tag else 0)  # never clip mid-word: end with an ellipsis
            base = ("declared: " + r["note"].replace("docker: bypasses ufw", "docker")) if declared and r["bad_note"] else r["note"]
            xn = expose_note(r)  # [expose]: beyond what config.ini says (red, instead of the note) or within it (grey, before the note)
            if xn:
                base = xn[0] if xn[1] else xn[0] + (" · " + base if base else "")
            text = base if len(base) <= room else base[:max(room - 1, 0)] + "…"
            note = Span(text, "err" if (xn and xn[1]) or (r["bad_note"] and not declared) else "muted", full=base)
            trows.append(Row([Span(f"{r['port']:>5}/{r['proto'][0]}", full=f"{r['port']}/{r['proto']}"),
                              Line([Span("⚠", "err") if r["warn"] else " ", Span(r["name"][:nw - 1], full=r["name"])]),
                              _cell(r["loc"], loc=True), _cell(r["lan"], r["warn"]), _cell(r["ts"], r["warn"]), _cell(r["net"], net=True),
                              Line([Span(tag + " ", "err_strong"), note]) if tag else note],
                             key=f"{r['port']}/{r['proto'][0]}:{g}"))
    head = "   " + "PORT".ljust(9) + "SERVICE".ljust(nw + 1) + "".join(x.center(6) for x in ("LOC", "LAN", "TS", "NET")) + "  NOTE"
    cols = [Col("port", "Port", num=True, w=8, gap=0), Col("service", "Service", w=nw + 1, gap=0), Col("loc", "Loc", "c", w=6, gap=0, wprio=3),
            Col("lan", "LAN", "c", w=6, gap=0), Col("ts", "TS", "c", w=6, gap=0), Col("net", "Net", "c", w=6), Col("note", "Note", wprio=2)]
    body.append(Table(cols, trows, groups or None, indent=3, head_line=head, titled=True))
    if by["LOCALE"]:  # exception-based: local is not a risk, compact list
        body += [Line(), Line([Span("   " + titles["LOCALE"], bold=True), Span(f"  ({len(by['LOCALE'])})", "muted")]),
                 Wrap([f"{r['port']} {r['name']}" for r in by["LOCALE"]], sep="  ·  ", indent=5)]
    return body


@native("exposure", "EXPOSURE", "exposure")
def exposure_card(ctx, k, caps):
    rows = _rows(ctx)
    if rows is None:
        return _native_card("exposure", ctx, "", [Msg("err", "unavailable")])
    if k < 0:
        return _native_card("exposure", ctx, "", exposure_full(ctx, caps, rows))
    w, new = caps.width, ctx.new or {}
    by = {g: [r for r in rows if group_of(r) == g] for g, _ in GROUPS}
    warn, ni = sum(r["warn"] for r in rows), len(by["INTERNET"])
    body = [Line([" Internet ", Span(str(ni), "err_strong") if ni else "0",
                  f"   LAN {len(by['LAN'])}   tailnet only {len(by['TAILNET'])}   local only {len(by['LOCALE'])}"]
                 + (["   ", Span(f"⚠ {warn} DB/broker on LAN", "err")] if warn else []))]
    for r in caps.lim(by["INTERNET"], 2, "exposure"):
        tag, xn = new.get(f"{r['port']}/{r['proto'][0]}:INTERNET"), expose_note(r)
        line = Line([" ", Span("●", "err_strong"), " "] + ([Span(tag + " ", "err_strong")] if tag else [])
                    + [Span(f"{r['port']}/{r['proto'][0]}", full=f"{r['port']}/{r['proto']}"), " ", Span(r["name"][:40], full=r["name"]), "  ",
                       Span("public on the Internet", "err")])
        if xn:  # [expose]: after the line when it fits, else below it
            mark = Span(xn[0], "err" if xn[1] else "muted")
            if len(line.text) + 2 + len(xn[0]) <= w:
                line = Line(line.spans + ["  ", mark])
            else:
                body.append(line)
                line = Line(["   ", mark])
        body.append(line)
    items = []
    for r in by["LAN"]:
        tag, xn = new.get(f"{r['port']}/{r['proto'][0]}:LAN"), expose_note(r)
        label, full = f"{r['port']} {r['name'][:22]}", f"{r['port']} {r['name']}"
        items.append((0 if tag or (xn and xn[1]) else 1,
                      Line(([Span(tag + " ", "err_strong")] if tag else []) + [Span("⚠" + label, "err", full="⚠" + full) if r["warn"] else Span(label, full=full)]
                           + ([" ", Span(xn[0], "err" if xn[1] else "muted")] if xn else []))))
    # new/changed items first: they must not end up behind the '… +N'
    if items:
        body.append(Wrap([t for _, t in sorted(items, key=lambda x: x[0])], sep="  ·  ", indent=1,
                         max_lines=None if caps.opened("exposure") else max(1, 3 - min(k, 2))))
    return _native_card("exposure", ctx, "", body)


# -- firewall

def short_default(text):
    """'deny (incoming), allow (outgoing), deny (routed)' -> 'in deny · out allow · fwd deny' (fits a 3-column layout)."""
    names = {"incoming": "in", "outgoing": "out", "routed": "fwd"}
    found = re.findall(r"(\w+) \((incoming|outgoing|routed)\)", str(text))
    return ui.safe("  ·  ".join(f"{names[d]} {a}" for a, d in found)) if found else ui.safe(text)


def _fw_native_status(net):
    """macOS/Windows: the status line of the OS firewall (Windows Firewall per network profile, macOS Application Firewall)."""
    fw, err = net.get("firewall"), net.get("errors") or {}
    if fw is None and is_disabled(net, "firewall"):
        return [Msg("info", "firewall check disabled in config.ini")]
    if fw is None:
        return [Msg("err", "firewall unreadable: " + ui.safe(err.get("firewall", "?"))[:70])]
    name = ui.safe(fw.get("name") or "firewall")
    if fw.get("kind") == "windows":
        active = [n for n, p in (fw.get("profiles") or {}).items() if p.get("active")]
        line = (ui.RichMsg("err", [Span(f"{name} OFF", "err_strong"), f" on {ui.safe(', '.join(fw['off']))}: no filtering there"]) if fw.get("off")
                else Msg("ok", f"{name} on" + (f" (active: {ui.safe(', '.join(active))})" if active else "")))
        return [line] + ([Msg("warn", "rules from Group Policy are not read: those ports show ?")] if fw.get("policy") else [])
    if fw.get("state") == 0:
        return [ui.RichMsg("err", [Span(f"{name} OFF", "err_strong"), ": every listening program is reachable from the LAN"])]
    return [Msg("ok", f"{name} " + ("blocking all incoming" if fw.get("block_all") else "on") + (" · stealth" if fw.get("stealth") else ""))]


def fw_status(net):
    """The two most important status lines: ufw and DOCKER-USER (macOS/Windows: the OS firewall)."""
    if os_of(net) != "linux":
        return _fw_native_status(net)
    err = net.get("errors") or {}
    ufw, du = net.get("ufw"), net.get("docker_user")
    out = []
    if ufw is None and is_absent(net, "ufw"):
        out.append(Msg("info", "ufw not installed: LAN filtering cannot be verified from here (nft/firewalld?)"))
    elif ufw is None:
        out.append(Msg("err", "ufw unreadable: " + ui.safe(err.get("ufw", "?"))[:80]))
    elif not ufw["active"]:
        out.append(ui.RichMsg("err", [Span("ufw OFF", "err_strong"), ": no LAN filtering for non-Docker services"]))
    else:
        out.append(Msg("ok", "ufw active"))
    if du is None and (is_absent(net, "docker_user") or is_absent(net, "iptables")):
        pass  # no iptables/Docker: the DOCKER-USER chain does not exist, no line to show
    elif du is None:
        out.append(Msg("err", "DOCKER-USER unreadable: " + ui.safe(err.get("docker_user", "?"))[:70]))
    elif not du:
        out.append(Msg("warn", "DOCKER-USER empty: ports published by containers bypass ufw"))
    else:
        out.append(Msg("ok", f"DOCKER-USER: {ui.plural(len(du), 'rule')}"))
    return out


def fw_native_details(net, caps, max_rules=None):
    """macOS/Windows FIREWALL body: the configuration in a few lines, then which rule or setting opens each listening port."""
    w, fw, pairs, body = caps.width, net.get("firewall"), [], []
    if fw and fw.get("kind") == "windows":
        for name, p in (fw.get("profiles") or {}).items():
            pairs.append((name, Line([Span("OFF", "err") if not p.get("enabled", True) else "on",
                                      f"   inbound {'allow' if p.get('inbound') == 1 else 'block'}" + ("   block all" if p.get("block_all") else "")]
                                     + ([Span("   ← active", "accent")] if p.get("active") else []))))
        if fw.get("networks"):
            pairs.append(("networks", "  ".join(f"{ui.safe(n['alias'])}: {ui.safe(n['category'])}" for n in fw["networks"])))
        pairs.append(("rules", f"{fw.get('allow_rules', 0)} allow · {fw.get('block_rules', 0)} block (enabled, inbound)"))
    elif fw:
        pf = fw.get("pf")
        pairs.append(("signed apps", f"built-in {'allowed' if fw.get('builtin') else 'asked'} · downloaded {'allowed' if fw.get('downloaded') else 'asked'}"))
        pairs.append(("app rules", f"{fw.get('apps_allowed', 0)} allowed · {fw.get('apps_blocked', 0)} blocked"))
        pairs.append(("pf", "unreadable" if pf is None else
                      (f"on, {ui.plural(pf.get('rules', 0), 'rule')} of its own (not interpreted)" if pf.get("enabled") else "off")))
    if pairs:
        body.append(ui.KV(pairs))
    opened = {}
    for lst in net.get("listeners") or []:
        st, note = (lst.get("fw") or ["", ""])[:2]
        if st in ("open", "nofw") and bind_scope(lst["addr"]) != "lo":
            opened.setdefault(note, set()).add((lst["port"], lst["proto"][0]))
    if opened:
        rows = sorted(opened.items(), key=lambda kv_: min(kv_[1]))
        shown = rows if max_rules is None else caps.lim(rows, max_rules, "firewall")
        trows = []
        for note, ports in shown:
            plist = ", ".join(f"{p}/{x}" for p, x in sorted(ports))
            trows.append(Row([Span(ui.safe(note)[:42], full=ui.safe(note)), plist]))
        body += [Line(), Line([Span(f"   WHAT LETS PORTS IN ({len(rows)})", bold=True)]),
                 Table([Col("rule", "Rule / setting", w=44, gap=0), Col("ports", "Ports", clip=max(10, w - 48))], trows, indent=3,
                       head_line="   " + "RULE / SETTING".ljust(44) + "PORTS")]
        if len(shown) < len(rows):
            body.append(More(len(rows) - len(shown), "more", indent=3))
    return body


def firewall_full(net, caps, max_rules=None):
    """The whole firewall block: the status first, then the configuration and the rules (max_rules: how many, None for all of them)."""
    err = net.get("errors") or {}
    ufw, du, ipt = net.get("ufw"), net.get("docker_user"), net.get("iptables")
    body = [Line()] + fw_status(net) + [Line()]  # the status before any detail
    if os_of(net) != "linux":
        return body + fw_native_details(net, caps, max_rules)
    pairs = []
    if ufw is not None:
        pairs.append(("ufw", Span(f"{short_default(ufw['default'])}   log: {ui.safe(ufw['logging'])}") if ufw["active"]
                      else Span("off (no rules in force)", "err")))
    if ipt:
        pol, cnt = ipt["policy"], ipt["count"]
        pairs.append(("iptables", "   ".join(f"{k} {pol.get(k, '?')} ({ui.plural(cnt.get(k, 0), 'rule')})" for k in ("INPUT", "FORWARD"))))
        pairs.append(("tailscale", Span("ts-input accepts tailscale0", "ok") if ipt["ts_input"] else Span("ts-input rule not found: TS may not be open", "warn")))
    elif is_absent(net, "iptables"):
        pairs.append(("iptables", Span("not installed", "muted")))
    elif "iptables" in err:
        pairs.append(("iptables", Span("n/a: " + ui.safe(err["iptables"])[:70], "err")))
    f2b = net.get("f2b")
    if f2b is None and is_absent(net, "f2b"):
        pass  # fail2ban not installed: no line
    elif f2b is None or f2b.get("error"):
        pairs.append(("fail2ban", Span("n/a: " + ui.safe((f2b or {}).get("error") or err.get("f2b", "?"))[:70], "err")))
    else:
        for j in f2b["jails"]:
            pairs.append(("fail2ban", f"{ui.safe(j['name'])}: {j['banned']} ban  {ui.safe(' '.join(j['ips']))}"))
    dr = net.get("drops")
    if ufw is not None and ufw.get("logging", "").startswith("off"):
        pairs.append(("drop 1h", Span("ufw logging off: blocks not logged", "warn")))
    elif dr is None and is_absent(net, "drops"):
        pass  # no journalctl: no drop count
    elif dr is None:
        pairs.append(("drop 1h", Span("n/a " + ui.safe(err.get("drops", "")), "warn")))
    else:
        pairs.append(("drop 1h", str(dr["n"])))
        if dr["dpt"]:
            pairs.append(("  ports", "  ".join(f"{ui.safe(k)}×{v}" for k, v in dr["dpt"])))
            pairs.append(("  sources", "  ".join(f"{ui.safe(k)}×{v}" for k, v in dr["src"])))
    if pairs:
        body.append(ui.KV(pairs))
    if ufw and ufw["rules"]:
        # IPv4 inbound only: IPv6 rules mirror them and outbound ones do not filter (default allow): keeping
        # them all would take the table to dozens of lines and drop the single screen to the compact level
        v6 = lambda r: "(v6)" in r["to"] + r["from"]  # noqa: E731
        rules_in = [r for r in ufw["rules"] if "OUT" not in r["action"] and "FWD" not in r["action"] and not v6(r)]
        n_out = sum("OUT" in r["action"] for r in ufw["rules"])
        n_v6 = sum(v6(r) and "OUT" not in r["action"] and "FWD" not in r["action"] for r in ufw["rules"])
        hidden = ", ".join(x for x in (f"{n_out} outbound" if n_out else "", f"{n_v6} mirrored IPv6" if n_v6 else "") if x)
        body += [Line(), Line([Span(f"   UFW INBOUND RULES ({len(rules_in)})", bold=True)] + ([Span(f"   + hidden: {hidden}", "muted")] if hidden else []))]
        open_all = lambda r: (r["action"].startswith(("ALLOW", "LIMIT")) and r["from"].startswith("Anywhere")  # noqa: E731
                              and not r["to"].startswith("Anywhere"))  # exposes the port to the world: must be seen first
        if max_rules is None or caps.opened("firewall"):
            shown = rules_in
        else:
            shown = caps.lim(sorted(rules_in, key=lambda r: not open_all(r)), max_rules, "firewall")
        trows = [Row([Span(ui.safe(r["to"])[:26], full=ui.safe(r["to"])), Span(ui.safe(r["action"])[:12], full=ui.safe(r["action"])), ui.safe(r["from"])],
                     tone="warn" if open_all(r) else None) for r in shown]  # no cap: all of them, and the page splits by itself if they do not fit
        body.append(Table([Col("to", "To", w=28, gap=0), Col("action", "Action", num=True, w=14, gap=0), Col("from", "From", wprio=1)], trows, indent=3,
                          head_line="   " + "TO".ljust(28) + "ACTION".ljust(14) + "FROM", fill=True))
        if len(shown) < len(rules_in):
            body.append(More(len(rules_in) - len(shown), "rule" if len(rules_in) - len(shown) == 1 else "rules", indent=3))
    return body


@native("firewall", "FIREWALL", "firewall")
def firewall_card(ctx, k, caps):
    net = ctx.net
    if net is None:
        return _native_card("firewall", ctx, "", [Msg("err", "network collector not running")])
    if k < 0:  # enough room: with the rule list (capped at the intermediate level, most exposed first)
        return _native_card("firewall", ctx, "", firewall_full(net, caps, None if (k <= -2 or caps.opened("firewall")) else 10))
    body = fw_status(net)
    ipt, f2b, dr = net.get("iptables"), net.get("f2b"), net.get("drops")
    bits = []
    if os_of(net) != "linux" and net.get("firewall"):
        let_in = {(x["port"], x["proto"]) for x in net.get("listeners") or []
                  if (x.get("fw") or [""])[0] in ("open", "nofw") and bind_scope(x["addr"]) != "lo"}
        bits.append(f"{ui.plural(len(let_in), 'listening port')} let in")
    if ipt:
        bits.append("INPUT " + ipt["policy"].get("INPUT", "?") + " · FORWARD " + ipt["policy"].get("FORWARD", "?"))
        bits.append(Span("ts-input ✔", "ok") if ipt["ts_input"] else Span("ts-input ?", "warn"))
    if f2b and not f2b.get("error"):
        bits.append("fail2ban " + " ".join(f"{ui.safe(j['name'])}:{j['banned']}" for j in f2b["jails"]))
    if dr is not None:
        bits.append(f"drop 1h {dr['n']}")
    if bits:
        body.append(Wrap(bits, sep="   ", indent=3))
    return _native_card("firewall", ctx, "", body)


# -- web apps

@native("webapps", "WEB APPS", "webapps")
def webapps_card(ctx, k, caps):
    net, w = ctx.net, caps.width
    if net is None:
        return _native_card("webapps", ctx, "", [Msg("err", "network collector not running")])
    rows = webapp_rows(net, ctx.cont)
    if net.get("listeners") is None and not rows:
        return _native_card("webapps", ctx, "", [unavail(net, "listeners")])
    if not rows:
        return _native_card("webapps", ctx, "", [Msg("info", "no web apps found (declare the ones you expect under [webapps] in config.ini)")])
    up, down = sum(r["state"] == "up" for r in rows), sum(r["state"] == "down" for r in rows)
    note = f"{up} active" + (f" · {down} down (expected)" if down else "")
    declared = bool(ctx.cfg["webapps"])
    nw = max(8, min(22, w - 47))
    shown = caps.lim(rows, 12 if k <= 0 else 6 if k <= 2 else 4, "webapps")
    trows = []
    for r in shown:
        ports = ",".join(str(p) for p in r["ports"][:3]) + ("…" if len(r["ports"]) > 3 else "")
        label = REACH_LABEL.get(r["reach"], "?")
        if r["state"] == "down":
            mark, reach, flag = Span("○", "warn"), Span("not listening", "muted"), Span("DOWN (expected)", "warn")
        else:
            mark = Span("●", "ok" if r["expected"] or not declared else "warn")
            reach = Span(label, REACH_TONE.get(r["reach"]))
            flag = Span("not declared", "muted") if declared and not r["expected"] else Span("")
        trows.append(Row([mark, Span(ui.safe(r["name"])[:nw], full=ui.safe(r["name"])), ports, reach, flag], key=r["name"]))
    cols = [Col("mark", "", w=1), Col("name", "Web app", w=nw + 1, gap=0), Col("ports", "Ports", num=True, w=13, gap=0, wprio=2), Col("reach", "Reach", w=14, gap=0),
            Col("flag", "", wprio=1)]
    more = More(len(rows) - len(shown), "more", indent=1) if len(rows) > len(shown) else None
    return _native_card("webapps", ctx, note, [Table(cols, trows)] + ([more] if more else []), more)


# ---- the KPIs -----------------------------------------------------------------------------------------------------------------

KPI_LABELS = {"problems": "Problems", "internet": "Internet", "lan": "LAN", "beyond": "Beyond", "db_lan": "DB on LAN", "firewall": "Firewall",
              "cpu": "CPU", "ram": "RAM", "disk": "Disk", "temp": "Temp", "load": "Load", "containers": "Containers",
              "unhealthy": "Unhealthy", "failed_units": "Failed units", "ssh": "SSH", "tailnet": "Tailnet", "rx": "Rx", "tx": "Tx",
              "uptime": "Uptime", "health": "Health", "ai": "AI"}
KPIS = {}  # id -> builder(ctx) -> ui.Kpi, in the order of prefs.KPI_IDS


def kpi(id):
    """Decorator: registers the builder of KPI `id`."""
    def deco(fn):
        KPIS[id] = fn
        return fn
    return deco


def _k(id, value, unit, state, hint="", spark=None):
    return Kpi(id, KPI_LABELS[id], value, unit, state, None, hint, spark)


def _unk(id, hint=""):
    """The KPI of a source that is missing: '?', never fine."""
    return Kpi(id, KPI_LABELS[id], "?", "", "unknown", None, hint)


def _level(frac, warn=0.7, err=0.9):
    return "ok" if frac < warn else "warn" if frac < err else "err"


def _rows(ctx):
    """The exposure rows (with [expose] applied) of this frame, or None when the network state has no listeners."""
    if not _has(ctx.net, "listeners"):
        return None
    return ctx.once("exposure_rows", lambda: expose_apply(exposure_rows(ctx.net, ctx.cont), ctx.net, ctx.cont))


def _count(ctx, group):
    rows = _rows(ctx)
    return None if rows is None else [r for r in rows if group_of(r) == group]


def _listed(d, field, key):
    """True if the collector put `key` in d[field] (absent: the tool is not installed; disabled: switched off in config.ini)."""
    return isinstance(d, dict) and key in (d.get(field) or [])


@kpi("problems")
def _kpi_problems(ctx):
    pb = ctx.problems
    if pb is None:
        return _unk("problems")
    sev = max((s for s, _ in pb), default=0)
    acc = getattr(pb, "accepted", 0)
    return _k("problems", str(len(pb)), "", "err" if sev >= 2 else "warn" if pb else "ok", f"{acc} accepted as known" if acc else "")


@kpi("internet")
def _kpi_internet(ctx):
    rows = _count(ctx, "INTERNET")
    return _unk("internet") if rows is None else _k("internet", str(len(rows)), "", "err" if rows else "ok", "public on the Internet")


@kpi("lan")
def _kpi_lan(ctx):
    rows = _count(ctx, "LAN")
    return _unk("lan") if rows is None else _k("lan", str(len(rows)), "", "warn" if any(r["warn"] for r in rows) else "ok", "reachable from the LAN")


@kpi("beyond")
def _kpi_beyond(ctx):
    rows = _rows(ctx)
    if rows is None:
        return _unk("beyond")
    if not ctx.cfg.get("expose"):
        return _k("beyond", "-", "", "info", "no [expose] in config.ini")
    n = len(expose_over_items(rows))
    return _k("beyond", str(n), "", "err" if n else "ok", "beyond [expose]")


@kpi("db_lan")
def _kpi_db_lan(ctx):
    rows = _rows(ctx)
    if rows is None:
        return _unk("db_lan")
    n = sum(1 for r in rows if r["warn"])
    return _k("db_lan", str(n), "", "err" if n else "ok", "databases and brokers open on the LAN")


@kpi("firewall")
def _kpi_firewall(ctx):
    net = ctx.net
    if not isinstance(net, dict):
        return _unk("firewall")
    if os_of(net) != "linux":
        fw = net.get("firewall")
        if fw is None:
            return _k("firewall", "-", "", "info", "switched off in config.ini") if _listed(net, "disabled", "firewall") else _unk("firewall")
        return _k("firewall", "off", "", "err", "the OS firewall is off") if fw.get("off") else _k("firewall", "on", "", "ok")
    ufw = net.get("ufw")
    if ufw is None:
        if _listed(net, "disabled", "ufw"):
            return _k("firewall", "-", "", "info", "switched off in config.ini")
        if _listed(net, "absent", "ufw"):
            return _k("firewall", "none", "", "warn", "ufw is not installed")
        return _unk("firewall", "ufw unreadable")
    return _k("firewall", "on", "", "ok", "ufw") if ufw.get("active") else _k("firewall", "off", "", "err", "ufw is off")


def _cores(ctx):
    cpu = (ctx.s or {}).get("cpu") if isinstance(ctx.s, dict) else None
    vals = [v for v in (ui.num(x) for x in (cpu or {}).values())] if isinstance(cpu, dict) else []
    return [v for v in vals if v is not None]


@kpi("cpu")
def _kpi_cpu(ctx):
    cores = _cores(ctx)
    if not cores:
        return _unk("cpu")
    mean = sum(cores) / len(cores)
    return _k("cpu", f"{mean * 100:.0f}", "%", _level(mean), ui.plural(len(cores), "thread"))


@kpi("ram")
def _kpi_ram(ctx):
    m = (ctx.s or {}).get("mem") if isinstance(ctx.s, dict) else None
    total, avail = (ui.num(m.get("MemTotal")), ui.num(m.get("MemAvailable"))) if isinstance(m, dict) else (None, None)
    if not total or avail is None:
        return _unk("ram")
    used = max(total - avail, 0.0)
    return _k("ram", f"{used / total * 100:.0f}", "%", _level(used / total), f"{ui.human(used)}/{ui.human(total)}")


@kpi("disk")
def _kpi_disk(ctx):
    d = (ctx.s or {}).get("disk_root") if isinstance(ctx.s, dict) else None
    try:
        used, total, label = d
        used, total = ui.num(used), ui.num(total)
    except (TypeError, ValueError):
        return _unk("disk")
    if used is None or not total:
        return _unk("disk")
    return _k("disk", f"{used / total * 100:.0f}", "%", _level(used / total), f"{ui.human(used)}/{ui.human(total)} {ui.safe(label)}")


@kpi("temp")
def _kpi_temp(ctx):
    th = (ctx.s or {}).get("thermal") if isinstance(ctx.s, dict) else None
    best = None
    for key in ("cpu", "nvme"):
        try:
            t, mx = (ui.num(x) for x in th[key])
        except (TypeError, KeyError, ValueError):
            continue
        if t is not None and mx and (best is None or t / mx > best[0] / best[1]):
            best = (t, mx)
    if best is None:
        return _unk("temp")
    t, mx = best
    return _k("temp", f"{t:.0f}", "°C", _level(t / mx, ui.THERMAL_WARN, ui.THERMAL_ERR), f"limit {mx:.0f}°C")


@kpi("load")
def _kpi_load(ctx):
    load = (ctx.s or {}).get("load") if isinstance(ctx.s, dict) else None
    if load == []:
        return _unk("load", "this OS has no load average")
    try:
        l1, l5, l15 = (float(x) for x in load)
    except (TypeError, ValueError):
        return _unk("load")
    if not all(x == x and abs(x) != float("inf") for x in (l1, l5, l15)):
        return _unk("load")
    ratio = l1 / max(1, len(_cores(ctx)))  # per core: 1.0 is a core's worth of work
    return _k("load", f"{l1:.2f}", "", _level(ratio, 1.0, 2.0), f"1/5/15 min: {l1:.2f} {l5:.2f} {l15:.2f}")


def _sick(ct):
    return "unhealthy" in ct["status"] or "Restarting" in ct["status"]  # as render.problems_raw counts them


@kpi("containers")
def _kpi_containers(ctx):
    cont = ctx.cont
    if not isinstance(cont, dict):
        return _unk("containers")
    if cont.get("absent"):
        return _k("containers", "-", "", "info", "docker is not installed")
    cs = cont.get("containers")
    if not isinstance(cs, list):
        return _unk("containers")
    running = sum(1 for x in cs if x["state"] == "running")
    sick = any(_sick(x) for x in cs)
    return _k("containers", str(running), f"/{len(cs)}", "err" if sick else "warn" if running < len(cs) else "ok", "running")


@kpi("unhealthy")
def _kpi_unhealthy(ctx):
    cont = ctx.cont
    if not isinstance(cont, dict):
        return _unk("unhealthy")
    if cont.get("absent"):
        return _k("unhealthy", "-", "", "info", "docker is not installed")
    if not isinstance(cont.get("containers"), list):
        return _unk("unhealthy")
    n = sum(1 for x in cont["containers"] if _sick(x))
    return _k("unhealthy", str(n), "", "err" if n else "ok", "unhealthy or restarting")


@kpi("failed_units")
def _kpi_failed(ctx):
    failed = ctx.boot.get("failed") if isinstance(ctx.boot, dict) else None
    if not isinstance(failed, list):
        return _unk("failed_units")
    return _k("failed_units", str(len(failed)), "", "err" if failed else "ok", ", ".join(ui.safe(u) for u in failed[:3]))


@kpi("ssh")
def _kpi_ssh(ctx):
    sess = (ctx.s or {}).get("sessions") if isinstance(ctx.s, dict) else None
    if not isinstance(sess, dict) or not isinstance(sess.get("ssh"), list):
        return _unk("ssh")
    far = [ip for ip in sess["ssh"] if not is_private_addr(ip)]
    return _k("ssh", str(len(sess["ssh"])), "", "err" if far else "ok", f"{len(far)} from outside the LAN and the tailnet" if far else "connected")


@kpi("tailnet")
def _kpi_tailnet(ctx):
    net = ctx.net
    ts = net.get("ts_peers") if isinstance(net, dict) else None
    if ts is None:
        if _listed(net, "absent", "ts_peers"):
            return _k("tailnet", "-", "", "info", "tailscale is not installed")
        return _unk("tailnet")
    try:
        peers = ts["peers"]
        online = sum(1 for p in peers if p["online"])
    except (KeyError, TypeError):
        return _unk("tailnet")
    return _k("tailnet", str(online), f"/{len(peers)}", "ok", "nodes online")


def _traffic(ctx, key):
    nets = (ctx.s or {}).get("net") if isinstance(ctx.s, dict) else None
    if not isinstance(nets, dict) or not nets:
        return None
    try:
        total = sum(ui.num(v[key]) or 0.0 for v in nets.values())
        top = max(nets.values(), key=lambda v: (ui.num(v["rx_tot"]) or 0.0) + (ui.num(v["tx_tot"]) or 0.0))
        return total, list(top["hist_" + key])
    except (KeyError, TypeError, ValueError):
        return None


def _rate_kpi(id, ctx):
    got = _traffic(ctx, id)
    if got is None:
        return _unk(id)
    value, _, unit = ui.fmt_rate(got[0]).partition(" ")
    return _k(id, value, unit, "info", "received" if id == "rx" else "sent", got[1])


@kpi("rx")
def _kpi_rx(ctx):
    return _rate_kpi("rx", ctx)


@kpi("tx")
def _kpi_tx(ctx):
    return _rate_kpi("tx", ctx)


@kpi("uptime")
def _kpi_uptime(ctx):
    up = ui.num((ctx.s or {}).get("uptime")) if isinstance(ctx.s, dict) else None
    return _unk("uptime") if up is None else _k("uptime", ui.fmt_dur(up), "", "info")


@kpi("health")
def _kpi_health(ctx):
    rep = ctx.health.get("report") if isinstance(ctx.health, dict) else None
    findings = rep.get("findings") if isinstance(rep, dict) else None
    if not isinstance(findings, list):
        return _unk("health")
    err = sum(1 for f in findings if isinstance(f, dict) and f.get("level") == "err")
    warn = sum(1 for f in findings if isinstance(f, dict) and f.get("level") == "warn")
    return _k("health", str(err + warn), "", "err" if err else "warn" if warn else "ok", f"{err} err · {warn} warn")


@kpi("ai")
def _kpi_ai(ctx):
    st = ctx.ai
    if not isinstance(st, dict) or "enabled" not in st:
        return _unk("ai")
    if not st["enabled"]:
        return _k("ai", "off", "", "info", "the advisor is off")
    probe = st.get("probe")
    answer = probe.get("state") if isinstance(probe, dict) else None
    if answer == "answering":
        return _k("ai", "on", "", "ok", "the model server answers")
    if answer == "down":
        return _k("ai", "down", "", "down", "the model server does not answer")
    return _unk("ai", "checking the model server")


def kpis(ctx, ids=None):
    """The Kpi of each id, in order (the default preset's when ids is None; an id that is not a KPI is skipped). A builder that fails
    is 'unknown': a broken source never takes the row down and never reads fine."""
    out = []
    for id in prefs.preset_prefs("default")["kpis"] if ids is None else ids:
        fn = KPIS.get(id)
        if fn is None:
            continue
        try:
            out.append(fn(ctx))
        except Exception:  # noqa: BLE001 - what a producer hands over is data, not a promise
            out.append(_unk(id))
    return out
