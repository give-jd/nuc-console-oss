"""Every key of config.ini that a person sets, for the settings page (src/web.py): what it does, the values it takes, when a change applies,
and the checks a value from the page passes before it is written (stdlib only, Python 3.8+).

nuc_config.load() stays the judge of what a value means: save() writes the new file aside, reads it back with load() and keeps it only when
load() has nothing new to say about the section being saved. tests/test_confedit.py checks that this schema, config/config.ini and load() name
the same keys, and that every value the page can send reads back as itself. Nothing here imports web.py or render.py.
"""
import re

import nuc_config
import prefs

NOW, START, NOTIFIER, INSTALLED, LOCK = "now", "start", "notifier", "installed", "lock"
APPLIES = {  # when a change applies, as the page says it
    NOW: "applies at once",
    START: "applies at the next start",
    NOTIFIER: "the notifier reads it within 30 seconds",
    INSTALLED: "used by an installation only, not by the desktop app or a portable run",
    LOCK: "a lock: only config.ini changes it, never this page",
}
BOOL, INT, CHOICE, TEXT, LIST, MAP = "bool", "int", "choice", "text", "list", "map"
NOTHING = "none"  # [ui] hidden: the word the page uses for `hidden =` (nothing hidden), which is not the same as no line at all
MAX_TEXT = 300    # characters of one value
MAX_ITEMS = 64    # lines of [webapps] or [expose]
GROUP_WORDS = {"LOCALE": "local", "TAILNET": "tailnet", "LAN": "lan", "INTERNET": "internet"}  # [expose]: load()'s groups -> the file's words
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.@+-]{0,63}")      # a [webapps] or [expose] name, as configparser stores it (lower case)
PORT_KEY_RE = re.compile(r"[0-9]{1,5}(/(tcp|udp))?")        # an [expose] port
UNSAFE = re.compile(r"[\x00-\x1f\x7f#;]")                    # a newline breaks the file, # and ; start a comment there


class Key(object):
    """One key: its section and name, its kind (BOOL, INT, CHOICE, TEXT, LIST), what it does (what), its default as the file writes it,
    when a change applies, and the values it takes: choices (CHOICE, LIST), lo..hi (INT; zero: 0 is allowed too and means automatic), a
    pattern (TEXT, a LIST's items). unset: an empty value removes the line ([ui]: the browser, the preset or the default decides then)."""
    __slots__ = ("section", "name", "kind", "default", "applies", "what", "choices", "lo", "hi", "zero", "pattern", "unset")

    def __init__(self, section, name, kind, default, applies, what, choices=(), lo=None, hi=None, zero=False, pattern=None, unset=False):
        self.section, self.name, self.kind, self.default, self.applies, self.what = section, name, kind, default, applies, what
        self.choices, self.lo, self.hi, self.zero, self.pattern, self.unset = tuple(choices), lo, hi, zero, pattern, unset

    def editable(self, overlay=False):
        """The page may change it: not a lock, and not a key that only an installation reads (the page writes a portable run's file).
        overlay (an installation): only the keys nuc_config.OVERLAY_ALLOW lists, and never a lock."""
        if overlay:
            return self.applies != LOCK and nuc_config.overlay_allowed(self.section, self.name)
        return self.applies not in (LOCK, INSTALLED)

    def values(self):
        """The values it takes, in words, for the page."""
        if self.kind == BOOL:
            return "yes or no"
        if self.kind == INT:
            return "%s%d-%d" % ("0 (automatic) or " if self.zero else "", self.lo, self.hi)
        if self.kind == CHOICE:
            return " | ".join(self.choices) + (" (or empty: not set)" if self.unset else "")
        if self.kind == LIST and self.choices:
            return "comma-separated: " + ", ".join(self.choices)
        return ""


def _k(section, rows):
    return tuple(Key(section, *row[:5], **(row[5] if len(row) > 5 else {})) for row in rows)


SECTIONS = (  # (section, title, what it is for): the order of the page
    ("dashboard", "Console and rotation",
     "The text console and the pages that take turns on a monitor with no keyboard (rotation, Details pages); the pace of every screen. "
     "A console that runs reads config.ini again when it starts."),
    ("ui", "Look and layout: everyone's defaults",
     "What the console and every browser start with. A browser's own choices (Appearance, above) and ?ui= in a link come first; a key "
     "that is not set falls to the preset, then to the built-in default."),
    ("display", "Display at login (macOS and Windows installations)",
     "How an installed nuc-console shows itself when you log in. The desktop app is its own window: of this section it uses zoom only."),
    ("console", "Linux text console (an installation's service)",
     "The font and the screen blanking of the monitor the dashboard draws on, applied when nuc-console.service starts; both off by default."),
    ("web", "Web view (an installation's service)",
     "The optional web service of an installation, the dashboard's only network listener. The desktop app and a portable run always "
     "serve 127.0.0.1 on a port of their own, without a token: they use the grid size and allowed_hosts only."),
    ("telegram", "Telegram alerts",
     "New and resolved ATTENTION problems on your phone, through a bot of your own. The pairing (the bot token) is done on the Telegram "
     "page: the token is never in config.ini."),
    ("ai", "Local AI",
     "A small model on this machine that turns the HEALTH findings into advice and answers questions about the machine. It only reads and "
     "suggests: it never runs anything."),
    ("webapps", "Web apps you expect",
     "One per line, name = port[, port...]: shown in WEB APPS as active, or DOWN when nothing listens there. A port listed here is an "
     "intended exposure: it no longer counts as a Docker port bypassing ufw."),
    ("expose", "Intended reach of services",
     "One per line, name or port = local | tailnet | lan | internet: the widest reach you intend. When the real reach is wider, ATTENTION "
     "raises over-exposed (the other alarms are never silenced by it). A name is a container, a compose service or project, a process, a "
     "systemd unit, a database name or kind, or a [webapps] name; a port is 8080 or 8080/udp. Several lines on one service: the most "
     "restrictive wins."),
)
TITLES = {name: title for name, title, _ in SECTIONS}

FEATURE_WORDS = (  # [features], the settings page's switches, screens first: (key, title, what it is); every nuc_config.FEATURES once
    ("map", "Map screen", "who reaches what, and what is behind it"),
    ("cpu", "CPU screen", "per-core load, temperatures, the processes"),
    ("health", "Health screen", "which apps cause trouble over time; the collector keeps history.db"),
    ("ai", "AI screen", "the local model: what fits this machine, the chat"),
    ("exposure", "Exposure", "listening ports by scope, port alarms, Funnel"),
    ("firewall", "Firewall", "the firewall's rules and recent drops"),
    ("fail2ban", "fail2ban", "jails and bans, inside Firewall"),
    ("containers", "Containers", "Docker containers and their health"),
    ("databases", "Databases", "database and broker ports, who connects"),
    ("webapps", "Web apps", "declared apps and the web listeners found"),
    ("tailscale", "Tailscale", "the tailnet's peers"),
    ("boot", "Boot", "boot time, slowest and failed units, journal errors"),
    ("docker_disk", "Docker disk", "what Docker keeps on the disk"),
    ("network_traffic", "Network traffic", "per-interface throughput"),
    ("sessions", "Sessions", "logged-in users and SSH sessions"),
    ("disks", "Disks", "mounted filesystems"),
    ("thermal", "Thermal", "CPU and NVMe temperatures, throttling"),
)

_YESNO = ("yes", "no")
KEYS = (
    tuple(Key("features", key, BOOL, "yes", NOW, "%s: %s. Off: not drawn, no alarm, not collected." % (title, what))
          for key, title, what in FEATURE_WORDS)
    + _k("dashboard", (
        ("sections", LIST, ", ".join(nuc_config.SECTIONS), NOW,
         "The order of the overview's sections, first to last (the console, the classic web page, and the web shell's default preset). "
         "A section left out keeps its place at the end; one switched off under Screens and sections is not drawn.",
         {"choices": nuc_config.SECTIONS}),
        ("mode", CHOICE, "overview", START,
         "The console: overview = one screen, no keyboard needed; rotate = three pages that take turns (the arrow keys move through them).",
         {"choices": nuc_config.MODES}),
        ("rotate_seconds", INT, "15", NOW,
         "Seconds each page stays when pages take turns (rotate, the Details pages, a full-screen window); on the web shell's wall page, "
         "the seconds between two scrolls.", {"lo": 3, "hi": 600}),
        ("refresh_seconds", INT, "2", NOW,
         "Seconds between two redraws of every screen and page (a page's - / + links change it there for a while). Faster: livelier "
         "bars, a little more CPU.", {"lo": nuc_config.REFRESH_MIN, "hi": nuc_config.REFRESH_MAX}),
        ("columns", INT, "0", START,
         "The console's layout width in characters; 0 = the real console's. Set it when the monitor shows less than the console believes "
         "it has: the layout is never larger than the real console.", {"lo": 40, "hi": 500, "zero": True}),
        ("rows", INT, "0", START, "The console's layout height in lines; 0 = the real console's.", {"lo": 10, "hi": 200, "zero": True}),
        ("spacing", INT, "1", NOW,
         "1 = a blank line under each section title (more air, a few more rows); 0 = compact. The console and the classic web page.",
         {"lo": 0, "hi": 1}),
        ("details", BOOL, "yes", NOW,
         "yes: a monitor with no keyboard turns to Details pages that show in full what the overview cut (… +N more), then comes back; "
         "no: the overview alone, never rotating."),
        ("overview_seconds", INT, "45", NOW, "How long the overview stays before the Details pages (details = yes).", {"lo": 10, "hi": 600}),
        ("map_in_rotation", BOOL, "no", NOW, "yes: the MAP screen, opened as far as it fits, joins the pages that take turns."),
        ("cpu_in_rotation", BOOL, "no", NOW, "yes: the CPU screen joins the pages that take turns."),
        ("health_in_rotation", BOOL, "no", NOW, "yes: the HEALTH screen joins the pages that take turns."),
    ))
    + _k("ui", (
        ("web", CHOICE, "app", NOW,
         "The web interface served: app = the shell of cards (this page); classic = the older text pages, kept for one release.",
         {"choices": prefs.WEB_MODES[::-1]}),
        ("theme", CHOICE, "auto", NOW,
         "auto follows the system's light or dark mode; dark; light; high-contrast: the strongest colours.",
         {"choices": prefs.THEMES, "unset": True}),
        ("density", CHOICE, "desk", NOW,
         "wall: big type for a monitor across the room, small print hidden, lists cut to 3 lines; desk: the default; compact: more on "
         "one screen.", {"choices": prefs.DENSITIES, "unset": True}),
        ("start_view", CHOICE, "overview", NOW, "The screen the console and the web view open at.", {"choices": prefs.VIEWS, "unset": True}),
        ("preset", CHOICE, "default", NOW,
         "A ready-made layout, key figures and hidden cards: default; security (exposure and the firewall first); server (the system and "
         "the containers first); desktop (no containers, databases or web apps). layout, kpis and hidden below override it.",
         {"choices": prefs.PRESETS, "unset": True}),
        ("order", CHOICE, "severity", NOW,
         "severity: a card that needs attention moves up; fixed: the cards keep the layout's order. On the console the cards keep their "
         "order unless this says severity.", {"choices": prefs.ORDERS, "unset": True}),
        ("kpis", LIST, "", NOW,
         "Up to %d key figures for the top row, in order, of: %s. Empty: the preset's." % (prefs.MAX_KPIS, ", ".join(prefs.KPI_IDS)),
         {"pattern": re.compile(r"[a-z_]{2,20}"), "unset": True}),
        ("layout", LIST, "", NOW,
         "The cards that show, in order: name or name:width (1-%d columns on the web), of: %s. Cards left out of layout and hidden are "
         "added at the end. Empty: the preset's." % (prefs.MAX_WIDTH, ", ".join(prefs.CARDS)),
         {"pattern": re.compile(r"[a-z_]{2,20}(:[0-9])?"), "unset": True}),
        ("hidden", LIST, "", NOW,
         "The cards that do not show (same names). %s = nothing hidden; empty: the preset's." % NOTHING,
         {"pattern": re.compile(r"[a-z_]{2,20}(:[0-9])?"), "unset": True}),
    ))
    + _k("display", (
        ("mode", CHOICE, "browser", INSTALLED,
         "browser: at every login it opens in a normal window of your browser; fullscreen: full screen, the overview and the Details "
         "pages taking turns (Alt+F4 / Cmd+Q closes it); none: it never opens by itself (a machine without a monitor). The installers "
         "apply it.", {"choices": ("browser", "fullscreen", "none")}),
        ("zoom", INT, "100", NOW,
         "Text size of the web pages in percent (the A- / A+ links change it while you look). Bigger: fewer columns.", {"lo": 50, "hi": 200}),
        ("browser", TEXT, "auto", INSTALLED,
         "The browser of the full-screen window: auto = Edge, then Chrome, then Firefox (Windows); Chrome, Edge, Brave, Chromium, else "
         "Safari (macOS); or the full path of a Chromium-based browser.", {"pattern": re.compile(r"[^\x00-\x1f\x7f#;]{1,%d}" % MAX_TEXT)}),
    ))
    + _k("console", (
        ("font", TEXT, "", INSTALLED,
         "A font for setfont, e.g. Lat15-TerminusBold32x16 (big text on a full-HD monitor). Empty: the font stays as it is.",
         {"pattern": re.compile(r"[A-Za-z0-9_.+-]{0,64}")}),
        ("blank_minutes", INT, "0", INSTALLED,
         "Minutes without a key before the screen goes black and the monitor sleeps; 0 = never.", {"lo": 0, "hi": 60}),
    ))
    + _k("web", (
        ("enabled", BOOL, "no", INSTALLED, "yes: an installation runs the web view as a service (off by default)."),
        ("bind", TEXT, "127.0.0.1", INSTALLED,
         "The address it listens on. 127.0.0.1 = this machine only: publish it with tailscale serve or a reverse proxy. Any other address "
         "needs token_file, or the service refuses to start.", {"pattern": re.compile(r"[0-9A-Za-z.:%_-]{1,64}")}),
        ("port", INT, "8787", INSTALLED, "The TCP port it listens on (by default: http://127.0.0.1:8787).", {"lo": 1, "hi": 65535}),
        ("token_file", TEXT, "", INSTALLED,
         "A file with a secret of 16 characters or more (mode 0600, readable by the nuc-console user): every request must carry it. "
         "Never write the token itself in config.ini.", {"pattern": re.compile(r"[^\x00-\x1f\x7f#;]{0,%d}" % MAX_TEXT)}),
        ("allowed_hosts", TEXT, "", START,
         "Extra names in the Host header accepted when no token is set, comma-separated (a guard against DNS rebinding): localhost, "
         "127.0.0.1, the bind address, the hostname and *.ts.net always are.",
         {"pattern": re.compile(r"([a-z0-9.-]{1,253}(, *[a-z0-9.-]{1,253})*)?")}),
        ("settings_actions", BOOL, "yes", LOCK,
         "yes: the settings page of an installation may change the presentation keys (what config.ini says wins for every other key); no: it "
         "only shows, and what it chose before counts for nothing (an administrator's lock)."),
        ("columns", INT, "200", NOW, "Width of the classic web pages' grid in characters (a page can ask ?cols=).", {"lo": 60, "hi": 300}),
        ("rows", INT, "60", NOW, "Height of that grid in lines.", {"lo": 20, "hi": 120}),
    ))
    + _k("telegram", (
        ("enabled", BOOL, "no", NOTIFIER,
         "yes: the notifier sends. The Telegram page can switch it on too, and cannot switch off a yes written here."),
        ("username", TEXT, "", NOTIFIER,
         "Your Telegram @username (5-32 letters, digits, _): who may receive the messages. The pairing writes it.",
         {"pattern": re.compile(r"(@?[A-Za-z0-9_]{5,32})?")}),
        ("detail", CHOICE, "titles", NOTIFIER,
         "titles: only a problem's title leaves this machine; full: its text too (container names, ports), which then goes through "
         "Telegram's servers.", {"choices": nuc_config.TELEGRAM_DETAILS}),
        ("resolved", BOOL, "yes", NOTIFIER, "yes: a message also when a problem is gone."),
        ("web_actions", BOOL, "yes", LOCK,
         "yes: the Telegram page may pair, switch on and off and send a test; no: it only shows (an administrator's lock)."),
    ))
    + _k("ai", (
        ("enabled", BOOL, "no", NOW,
         "yes: the advisor is on, with the model below. The AI page's on and off do the same without this file; a yes here wins."),
        ("endpoint", TEXT, "http://127.0.0.1:11434/v1", NOW,
         "The OpenAI-compatible server the advisor asks (Ollama, llama.cpp server, LM Studio): on this machine, unless allow_remote = yes.",
         {"pattern": re.compile(r"https?://[^\s\x00-\x1f\x7f#;]{1,%d}" % MAX_TEXT)}),
        ("model", TEXT, "", NOW,
         "The model's name as the server lists it, or a catalog id such as qwen3-8b. Empty: the server's first.",
         {"pattern": re.compile(r"[A-Za-z0-9._:/@+-]{0,200}")}),
        ("gpu", CHOICE, "auto", START,
         "auto: the model server nuc-console starts uses the GPU when the model fits there, all of it or some layers; no: it runs on the "
         "CPU. Applies when the server starts again (AI off, then on).", {"choices": ("auto", "no")}),
        ("allow_remote", BOOL, "no", LOCK,
         "yes: the endpoint may be on another machine, and the machine's history goes there with each question."),
        ("timeout_s", INT, "120", NOW, "Seconds an answer may take.", {"lo": 10, "hi": 600}),
        ("web_actions", BOOL, "yes", LOCK,
         "yes: the AI page and the AI screen may set a model up, switch the AI on and off, delete models and ask; no: they only show (an "
         "administrator's lock)."),
        ("daily", BOOL, "no", START,
         "yes: the collector asks for one digest of the last 7 days a day (at low priority), shown on the HEALTH screen. It needs "
         "enabled = yes and the Health screen."),
    ))
)
BY_SECTION = {}
for _key in KEYS:
    BY_SECTION.setdefault(_key.section, []).append(_key)
KEY = {(k.section, k.name): k for k in KEYS}
MAPS = ("webapps", "expose")  # sections of free names: a text of name = value lines


class Refused(ValueError):
    """What the page sent cannot be written: the reasons, one per value (nothing was written)."""

    def __init__(self, reasons):
        ValueError.__init__(self, "; ".join(reasons))
        self.reasons = list(reasons)


def value(cfg, key):
    """The value of a key in force (cfg: nuc_config.load()), as config.ini writes it: the page shows it and compares what it receives with it.
    A [ui] key the file does not set is ""; [ui] hidden set to nothing is NOTHING."""
    sec, name = key.section, key.name
    if sec == "features":
        v = cfg["features"].get(name, True)
    elif sec == "dashboard":
        v = cfg.get(name)
    elif sec == "ui":
        ui = cfg.get("ui") or {}
        v = ui.get(name)
        if v is None:
            return ""
        if name == "layout":
            v = [c if w == 1 else "%s:%d" % (c, w) for c, w in v]
        elif name == "hidden":
            hw = ui.get("hidden_w") or {}
            v = [c if c not in hw else "%s:%d" % (c, hw[c]) for c in v] or NOTHING
    else:
        v = (cfg.get(sec) or {}).get(name)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def map_text(cfg, section):
    """[webapps] or [expose] in force as the page's text: one `name = value` line each."""
    if section == "webapps":
        return "\n".join("%s = %s" % (n, ", ".join(str(p) for p in ports)) for n, ports in sorted((cfg.get("webapps") or {}).items()))
    return "\n".join("%s = %s" % (n, GROUP_WORDS.get(g, g.lower())) for n, g in sorted((cfg.get("expose") or {}).items()))


def check(key, raw):
    """A value from the page -> the text config.ini gets. Refused with the reason when it is not one the key takes."""
    v = (raw or "").strip()
    if len(v) > MAX_TEXT:
        raise Refused(["%s: at most %d characters" % (key.name, MAX_TEXT)])
    if UNSAFE.search(v):
        raise Refused(["%s: one line, without # or ; (they start a comment in config.ini)" % key.name])
    if not v and key.unset:
        return ""
    if key.kind == BOOL:
        v = v.lower()
        if v not in _YESNO:
            raise Refused(["%s: yes or no" % key.name])
    elif key.kind == CHOICE:
        v = v.lower()
        if v not in key.choices:
            raise Refused(["%s: one of %s" % (key.name, ", ".join(key.choices))])
    elif key.kind == INT:
        n = int(v) if v.isascii() and v.isdigit() and len(v) < 7 else None
        if n is None or not (key.lo <= n <= key.hi or (key.zero and n == 0)):
            raise Refused(["%s: a whole number, %s" % (key.name, key.values())])
        v = str(n)
    elif key.kind == LIST:
        items = [t.strip().lower() for t in v.split(",") if t.strip()]
        if key.section == "ui" and key.name == "hidden" and items == [NOTHING]:
            return NOTHING
        bad = [t for t in items if (t not in key.choices if key.choices else not key.pattern.fullmatch(t))]
        if bad:
            raise Refused(["%s: unknown name %s" % (key.name, bad[0][:40])])
        v = ", ".join(items)
    elif key.name == "allowed_hosts" and not key.pattern.fullmatch(v.lower()):
        raise Refused(["%s: host names (letters, digits, . and -), comma-separated" % key.name])
    elif key.name == "allowed_hosts":
        v = ", ".join(h.strip().lower() for h in v.split(",") if h.strip())
    elif key.pattern is not None and not key.pattern.fullmatch(v):
        raise Refused(["%s: %s" % (key.name, "not a value it takes" if v else "it cannot be empty")])
    if key.section == "telegram" and key.name == "username":
        v = v.lstrip("@").lower()
    return v


def parse_map(section, text):
    """The page's text of [webapps] or [expose] -> {name: value as config.ini writes it}. Blank lines are skipped; anything else that is not
    `name = value` with a name and a value that section takes is refused, all the reasons at once."""
    out, why = {}, []
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]
    if len(lines) > MAX_ITEMS:
        raise Refused(["at most %d lines" % MAX_ITEMS])
    for ln in lines:
        name, eq, v = ln.partition("=")
        name, v = name.strip().lower(), v.strip()
        show = ln[:60]
        if not eq or not name or not v:
            why.append("%r: write name = value" % show)
            continue
        if UNSAFE.search(name + v.replace(";", "," if section == "webapps" else ";")) or len(ln) > MAX_TEXT:
            why.append("%r: one line, without # or ;" % show)
            continue
        if section == "expose" and PORT_KEY_RE.fullmatch(name):
            try:
                nuc_config.expose_port(name)
            except ValueError:
                why.append("%r: a port is 1-65535, 8080 or 8080/udp" % show)
                continue
        elif not NAME_RE.fullmatch(name):
            why.append("%r: a name is letters, digits and . _ - @ + (up to 64), starting with a letter or a digit" % show)
            continue
        if name in out:
            why.append("%r: %s is there twice" % (show, name))
            continue
        if section == "webapps":
            ports = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
            if not ports or not all(p.isascii() and p.isdigit() and 0 < int(p) < 65536 for p in ports):
                why.append("%r: ports are numbers from 1 to 65535, comma-separated" % show)
                continue
            out[name] = ", ".join(str(int(p)) for p in ports)
        else:
            word = v.lower()
            if word not in nuc_config.EXPOSE_WORDS:
                why.append("%r: the reach is local, tailnet, lan or internet" % show)
                continue
            out[name] = GROUP_WORDS[nuc_config.EXPOSE_WORDS[word]]
    if why:
        raise Refused(why)
    return out


def changes(cfg, section, form):
    """What a post of one section asks to change, against cfg (the configuration in force): (items [(key, value)], drop [key]). form: the
    request's {field: [values]}. Only the keys the page may change are read; a field that is missing changes nothing. Refused with every
    reason at once: then nothing is written."""
    one = lambda name: (form.get(name) or [None])[0]  # noqa: E731
    items, drop, why = [], [], []
    if section in MAPS:
        text = one("text")
        if text is None:
            return [], []
        try:
            new = parse_map(section, text)
        except Refused as e:
            raise Refused(["[%s] %s" % (section, r) for r in e.reasons])
        old = dict(ln.split(" = ", 1) for ln in map_text(cfg, section).split("\n") if ln)
        items = [(n, v) for n, v in sorted(new.items()) if old.get(n) != v]
        drop = sorted(n for n in old if n not in new)
        return items, drop
    if section not in BY_SECTION or section == "features":
        raise Refused(["no such section: %s" % section[:40]])
    for key in BY_SECTION[section]:
        raw = one(key.name)
        if raw is None or not key.editable():
            continue
        try:
            v = check(key, raw)
        except Refused as e:
            why.extend("[%s] %s" % (section, r) for r in e.reasons)
            continue
        if v == value(cfg, key):
            continue
        if v == "" and key.unset:
            drop.append(key.name)
        else:
            items.append((key.name, "" if v == NOTHING else v))
    if why:
        raise Refused(why)
    return items, drop


def save(path, section, form, load=nuc_config.load):
    """Writes what a post of one section changed into config.ini at `path` (nuc_config.set_keys: every other line and comment as it was) and
    returns the names it changed ([] when nothing did): a value is compared with the file's own, as load() reads it. The new file is read with
    load() before it takes the old one's place: a complaint about this section that the old file did not have refuses the whole post, and the
    old file stays."""
    before = []
    now = load(path, warn=before.append)
    if now.get("config_error"):
        raise Refused(["config.ini cannot be read (%s): fix it by hand first" % now["config_error"][:120]])
    items, drop = changes(now, section, form)
    if not items and not drop:
        return []
    tag = "[%s]" % section
    before = {w.split(": ", 2)[-1] for w in before}

    def judge(tmp):
        said = []
        if load(tmp, warn=said.append).get("config_error"):
            raise Refused(["config.ini would not be readable any more"])
        new = [w.split(": ", 2)[-1] for w in said if tag in w]
        new = [w for w in new if w not in before]
        if new:
            raise Refused(new)
    nuc_config.set_keys(path, section, items, drop, check=judge)
    return [k for k, _ in items] + list(drop)


def applies(section, names):
    """{APPLIES code: [names]} for the notice after a save, in the order of APPLIES."""
    out = {}
    for name in names:
        key = KEY.get((section, name))
        out.setdefault(key.applies if key else NOW, []).append(name)
    return {code: out[code] for code in APPLIES if code in out}


def save_overlay(path, section, form, overlay=None, load=nuc_config.load):
    """The installation's save: what a post of one section changed goes into the overlay (nuc_config.update_overlay), never into config.ini at
    `path`. Only keys of nuc_config.OVERLAY_ALLOW: a post that names any other key of the section, or a section with none, is refused whole.
    A value equal to config.ini's is not kept (the overlay holds only what differs), an empty [ui] value drops its entry. The new file is read
    back with load() like save() does. Returns the names changed."""
    overlay = overlay or nuc_config.overlay_path()
    if section not in BY_SECTION or section in ("features",) + MAPS or not any(k.editable(True) for k in BY_SECTION[section]):
        raise Refused(["[%s] is the administrator's: only config.ini changes it" % section[:40]])
    one = lambda name: (form.get(name) or [None])[0]  # noqa: E731
    base = load(path, overlay=False)
    if base.get("config_error"):
        raise Refused(["config.ini cannot be read (%s): fix it by hand first" % base["config_error"][:120]])
    cur, why, put, drop = nuc_config.read_overlay(overlay), [], {}, []
    for key in BY_SECTION[section]:
        raw = one(key.name)
        if raw is None:
            continue
        if not key.editable(True):
            why.append("[%s] %s: only config.ini changes it" % (section, key.name))
            continue
        try:
            v = check(key, raw)
        except Refused as e:
            why.extend("[%s] %s" % (section, r) for r in e.reasons)
            continue
        name = "%s.%s" % (section, key.name)
        if v == value(base, key) or (v == "" and key.unset):
            if name in cur:
                drop.append(name)
        elif cur.get(name) != ("" if v == NOTHING else v):
            put[name] = "" if v == NOTHING else v
    if why:
        raise Refused(why)
    if not put and not drop:
        return []
    tag = "[%s]" % section
    before = []
    load(path, warn=before.append, overlay=overlay)
    before = {w.split(": ", 2)[-1] for w in before}

    def judge(tmp):
        said = []
        if load(path, warn=said.append, overlay=tmp or False).get("config_error"):
            raise Refused(["config.ini would not be readable any more"])
        new = [w for w in (w.split(": ", 2)[-1] for w in said if tag in w) if w not in before]
        if new:
            raise Refused(new)
    nuc_config.update_overlay(put, drop, overlay, judge)
    return [n.partition(".")[2] for n in list(put) + drop]
