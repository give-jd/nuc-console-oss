"""Preferences of the interface: how the screens look and which cards they show (stdlib only, Python 3.8+).

Pure: nothing here reads a file, the environment or the network; the config.ini dict, the cookie and the URL value are passed in.
The new web shell (web.py) renders with it; the console tab bar will.

Fields (a field that is not set falls through to the next source):
  theme      auto | dark | light | high-contrast         cookie code t<a|d|l|h>
  density    wall | desk | compact                       d<w|k|c>
  start_view overview | map | cpu | health | ai          v<o|m|c|h|a>
  preset     default | security | server | desktop        p<n|s|v|d>   (a preset is a layout + KPIs + hidden cards)
  order      severity | fixed                            o<s|f>
  kpis       up to 8 KPI ids, in order                   k<kpi2>(_<kpi2>)*
  layout     the visible cards in order, width 1-4       l<card2>[1-4][x](_...)*   (x = hidden)
  hidden     the cards not shown                         (the x items of l)

Sources, strongest first: ?ui= (one URL) > cookie nuc_ui > config.ini [ui] > preset > built-in default.

One string for the cookie, ?ui= and localStorage:  "1" *("." field)
  - the whole value is  1(\\.[a-z][a-z0-9_]{1,120}){0,8}  and at most 256 bytes (ASCII, cookie-safe: no ; , space = " \\);
  - a wrong version, a wrong shape or non-ASCII ignores the whole value; an invalid field is ignored and the others are kept;
  - a field given twice: the last valid one wins;
  - k and l are strict: one unknown code, a width other than 1-4 or more than 8 KPIs invalidates that field; a repeat inside it
    (a KPI twice, a card twice) counts once, the first mention wins;
  - dump_cookie() writes the canonical form (fields in the order t d v p o k l, visible cards first, hidden ones in the order of
    nuc_config.SECTIONS), so equal preferences give an equal string (a cache key).
Example:  1.tl.dw.os.kpb_in_cp_rm.lat2_ex2_wa1_fw1_sy1_ct2_dbx

The layout editor of the web shell (`?edit=1`) changes only l (layout and hidden): move, resize, hide, show and reset are pure functions here
(`move`, `resize`, `hide`, `show`, `reset`; `apply_edit` applies one step, the field `e<op><card2>`, to a cookie).
"""
import re

import nuc_config

THEMES = ("auto", "dark", "light", "high-contrast")
DENSITIES = ("wall", "desk", "compact")
VIEWS = ("overview", "map", "cpu", "health", "ai")
PRESETS = ("default", "security", "server", "desktop")
ORDERS = ("severity", "fixed")
WEB_MODES = ("classic", "app")  # [ui] web only: which web interface is served (config.ini, never the cookie)
KPI_IDS = ("problems", "internet", "lan", "beyond", "db_lan", "firewall", "cpu", "ram", "disk", "temp", "load", "containers",
           "unhealthy", "failed_units", "ssh", "tailnet", "rx", "tx", "uptime", "health", "ai")
CARDS = tuple(nuc_config.SECTIONS)  # the card ids: the overview sections
MAX_KPIS = 8
MAX_WIDTH = 4  # a card is 1-4 columns wide on the web
COOKIE_MAX = 256
COOKIE_NAME = "nuc_ui"
COOKIE_VERSION = "1"

THEME_CODES = {"auto": "a", "dark": "d", "light": "l", "high-contrast": "h"}
DENSITY_CODES = {"wall": "w", "desk": "k", "compact": "c"}
VIEW_CODES = {"overview": "o", "map": "m", "cpu": "c", "health": "h", "ai": "a"}
PRESET_CODES = {"default": "n", "security": "s", "server": "v", "desktop": "d"}
ORDER_CODES = {"severity": "s", "fixed": "f"}
KPI_CODES = dict(zip(KPI_IDS, ("pb", "in", "la", "be", "dl", "fw", "cp", "rm", "dk", "tp", "ld", "ct", "uh", "fu", "sh", "tn", "rx", "tx",
                               "up", "hl", "ai")))
CARD_CODES = dict(zip(CARDS, ("at", "ex", "wa", "fw", "sy", "ct", "db", "bo", "nt", "se", "ts", "dd", "di")))

DEFAULTS = {"theme": "auto", "density": "desk", "start_view": "overview", "preset": "default", "order": "severity"}
FIELDS = ("theme", "density", "start_view", "preset", "order", "kpis", "layout", "hidden")
SOURCES = ("url", "browser", "config.ini", "preset", "default")  # where a value of effective() comes from
UI_KEYS = ("web",) + FIELDS  # the keys of config.ini [ui]

# (field, cookie letter, allowed values -> cookie code), in the canonical order of the cookie
_SCALARS = (("theme", "t", THEME_CODES), ("density", "d", DENSITY_CODES), ("start_view", "v", VIEW_CODES),
            ("preset", "p", PRESET_CODES), ("order", "o", ORDER_CODES))
_BY_LETTER = {letter: (field, {code: value for value, code in codes.items()}) for field, letter, codes in _SCALARS}
_KPI_BY_CODE = {code: kpi for kpi, code in KPI_CODES.items()}
_CARD_BY_CODE = {code: card for card, code in CARD_CODES.items()}
_COOKIE_RE = re.compile(r"1(\.[a-z][a-z0-9_]{1,120}){0,8}")
_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{1,120}")
_ITEM_RE = re.compile(r"([a-z]{2})([1-4])?(x)?")


def _cards(text):
    """'attention:2 exposure:2 webapps' -> (('attention', 2), ('exposure', 2), ('webapps', 1)); the data of the presets."""
    return tuple((t.partition(":")[0], int(t.partition(":")[2] or 1)) for t in text.split())


_WIDE = ("attention", "exposure")  # the default preset: these two cards are 2 columns wide, the rest follow the section order
_PRESET_DATA = {
    "default": {"kpis": "problems internet lan beyond cpu ram disk temp", "hidden": ""},  # layout: the section order (see preset_prefs)
    "security": {"layout": "attention:2 exposure:2 firewall webapps databases sessions tailscale containers system boot disks "
                           "network_traffic docker_disk",
                 "kpis": "problems internet lan beyond containers health", "hidden": ""},
    "server": {"layout": "attention:2 system:2 containers databases disks docker_disk boot exposure:2 webapps firewall network_traffic "
                         "sessions tailscale",
               "kpis": "problems cpu ram disk temp containers load health", "hidden": ""},
    "desktop": {"layout": "attention:2 system:2 disks network_traffic sessions exposure:2 firewall boot",
                "kpis": "problems cpu ram disk temp ai", "hidden": "containers databases docker_disk webapps tailscale"},
}


def _hidden_order(ids):
    """Hidden cards as a list in the order of the sections, once each: which cards are hidden is a set, so its order is canonical."""
    want = set(ids)
    return [c for c in CARDS if c in want]


def _section_order(sections):
    """[dashboard] sections as the dashboard reads it: the known names once each, the ones left out appended in their default place."""
    out = []
    for s in sections if isinstance(sections, (list, tuple)) else ():
        if isinstance(s, str) and s in CARDS and s not in out:
            out.append(s)
    return out + [c for c in CARDS if c not in out]


def preset_prefs(name, sections=None):
    """{"layout": [(card, width)], "kpis": [kpi], "hidden": [card]} of a preset. The default preset lists the cards in the order of
    `sections` ([dashboard] sections, backward compatible; the built-in order when None); the other presets bring their own order."""
    data = _PRESET_DATA[name if name in _PRESET_DATA else "default"]
    if name == "default" or name not in _PRESET_DATA:
        layout = [(c, 2 if c in _WIDE else 1) for c in _section_order(sections)]
    else:
        layout = list(_cards(data["layout"]))
    hidden = _hidden_order(data["hidden"].split())
    return {"layout": [(c, w) for c, w in layout if c not in hidden], "kpis": data["kpis"].split(), "hidden": hidden}


# ---------------------------------------------------------------- normalising (a partial prefs dict -> only valid, canonical parts)

def _item(it):
    """A layout item as (card, width) or None: 'sessions', ('sessions', 2) or ['sessions', 2]; the width is held to 1-4."""
    if isinstance(it, str):
        cid, w = it, 1
    elif isinstance(it, (list, tuple)) and len(it) == 2:
        cid, w = it
    else:
        return None
    if not isinstance(cid, str) or cid not in CARD_CODES:
        return None
    w = w if isinstance(w, int) and not isinstance(w, bool) else 1
    return cid, max(1, min(MAX_WIDTH, w))


def _clean(d):
    """The valid part of a partial prefs dict, canonical; never raises. An empty layout or kpis is dropped (it says nothing);
    an empty hidden stays (it says 'nothing is hidden'). A card both in layout and hidden is hidden."""
    out = {}
    if not isinstance(d, dict):
        return out
    for field, _, codes in _SCALARS:
        v = d.get(field)
        if isinstance(v, str) and v in codes:
            out[field] = v
    ks = d.get("kpis")
    if isinstance(ks, (list, tuple)):
        ids = []
        for k in ks:
            if isinstance(k, str) and k in KPI_CODES and k not in ids:
                ids.append(k)
        if ids:
            out["kpis"] = ids[:MAX_KPIS]
    hidden = d.get("hidden")
    if isinstance(hidden, (list, tuple)):
        out["hidden"] = _hidden_order(c for c in hidden if isinstance(c, str) and c in CARD_CODES)
    layout = d.get("layout")
    if isinstance(layout, (list, tuple)):
        items, seen = [], set(out.get("hidden", ()))
        for it in layout:
            it = _item(it)
            if it and it[0] not in seen:
                seen.add(it[0])
                items.append(it)
        if items:
            out["layout"] = items
    return out


# ---------------------------------------------------------------- the cookie / ?ui= / localStorage string

def _parse_field(seg):
    """One field segment ('tl', 'kpb_in', 'lat2_dbx') -> the prefs it sets ({} when invalid)."""
    letter, body = seg[0], seg[1:]
    if letter in _BY_LETTER:
        field, values = _BY_LETTER[letter]
        return {field: values[body]} if body in values else {}
    if letter == "k":
        ids = []
        for part in body.split("_"):
            kpi = _KPI_BY_CODE.get(part)
            if kpi is None:
                return {}
            if kpi not in ids:
                ids.append(kpi)
        return {"kpis": ids} if len(ids) <= MAX_KPIS else {}
    if letter == "l":
        layout, hidden, seen = [], [], set()
        for part in body.split("_"):
            m = _ITEM_RE.fullmatch(part)
            card = _CARD_BY_CODE.get(m.group(1)) if m else None
            if card is None:
                return {}
            if card in seen:
                continue
            seen.add(card)
            if m.group(3):
                hidden.append(card)
            else:
                layout.append((card, int(m.group(2) or 1)))
        out = {"hidden": _hidden_order(hidden)}
        if layout:
            out["layout"] = layout
        return out
    return {}


def parse_cookie(s):
    """A cookie / ?ui= / localStorage string -> the partial prefs it holds; {} when it is invalid (never raises)."""
    if not isinstance(s, str) or len(s) > COOKIE_MAX or not s.isascii() or not _COOKIE_RE.fullmatch(s):
        return {}
    out = {}
    for seg in s.split(".")[1:]:
        out.update(_parse_field(seg))  # last valid one wins
    return out


def dump_cookie(prefs):
    """Partial prefs -> the canonical string ('1' for none): the same preferences always give the same string."""
    p = _clean(prefs)
    segs = [letter + codes[p[field]] for field, letter, codes in _SCALARS if field in p]
    if "kpis" in p:
        segs.append("k" + "_".join(KPI_CODES[k] for k in p["kpis"]))
    items = [CARD_CODES[c] + str(w) for c, w in p.get("layout", ())] + [CARD_CODES[c] + "x" for c in p.get("hidden", ())]
    if items:
        segs.append("l" + "_".join(items))
    return ".".join([COOKIE_VERSION] + segs)


def apply_set(current, field):
    """The cookie after a `?set=<field>` link: the field in the same syntax replaces that field of `current` ('tl', 'dw', 'kpb_in',
    'lat2_ex2_dbx'); 'reset' clears everything ('1': the caller may then delete the cookie). Always a canonical string; an invalid
    field changes nothing. Choosing a preset (`pn`...) also drops the cookie's own kpis, layout and hidden, so that the preset shows
    as it is (theme, density, start view and order stay)."""
    prefs = parse_cookie(current)
    if field == "reset":
        return dump_cookie({})
    new = _parse_field(field) if isinstance(field, str) and _FIELD_RE.fullmatch(field) else {}
    if new:
        prefs.update(new)
        if "preset" in new:
            for k in ("kpis", "layout", "hidden"):
                prefs.pop(k, None)
    return dump_cookie(prefs)


# ---------------------------------------------------------------- the layout editor (pure: a layout in, a layout out)
#
# A layout here is {"layout": [(card, width)], "hidden": [card]}: the cards that show, in order, and the ones that are off. The editor's
# links are `?set=e<op><card2>` (one step: the server applies it to the layout in force and stores the whole result) and `?set=ereset`
# (drop the layout and the hidden list: the preset shows again); the editor's script sends the whole layout (`?set=l...`) instead.

EDIT_OPS = {"u": "earlier", "d": "later", "s": "narrower", "g": "wider", "h": "hide", "w": "show"}
_EDIT_RE = re.compile(r"e([udsghw])([a-z]{2})|(ereset)")


def layout_of(prefs, available=None):
    """The layout as the editor works on it, from preferences (usually effective()'s): the cards that show (visible_cards: the ones the
    layout leaves out come last, 1 wide) and the hidden ones (all of them, even a card whose feature is off now: it stays hidden)."""
    p = _clean(prefs)
    return {"layout": visible_cards(p, available), "hidden": list(p.get("hidden", ()))}


def _lay(layout):
    c = _clean(layout)
    return [list(i) for i in c.get("layout", ())], list(c.get("hidden", ()))


def _out(items, hidden):
    return {"layout": [tuple(i) for i in items], "hidden": hidden}


def move(layout, card, delta):
    """The card `delta` places earlier (< 0) or later (> 0) among the cards that show; at the end of the list it stays; a card that is not
    shown or unknown changes nothing."""
    items, hidden = _lay(layout)
    ids = [c for c, _ in items]
    if card in ids and isinstance(delta, int) and not isinstance(delta, bool):
        i = ids.index(card)
        items.insert(max(0, min(len(items) - 1, i + delta)), items.pop(i))
    return _out(items, hidden)


def resize(layout, card, delta):
    """The card `delta` columns wider (or narrower), held to 1-4; a card that is not shown or unknown changes nothing."""
    items, hidden = _lay(layout)
    for it in items:
        if it[0] == card and isinstance(delta, int) and not isinstance(delta, bool):
            it[1] = max(1, min(MAX_WIDTH, it[1] + delta))
    return _out(items, hidden)


def hide(layout, card):
    """The card out of the shown ones into the hidden ones (a card that is not shown, or unknown, changes nothing)."""
    items, hidden = _lay(layout)
    if any(c == card for c, _ in items):
        items = [i for i in items if i[0] != card]
        hidden = _hidden_order(hidden + [card])
    return _out(items, hidden)


def show(layout, card):
    """A hidden card back, as the last one of the shown, 1 wide (a card that is not hidden, or unknown, changes nothing)."""
    items, hidden = _lay(layout)
    if card in hidden:
        hidden = [c for c in hidden if c != card]
        items.append([card, 1])
    return _out(items, hidden)


def reset(layout=None):
    """No layout of one's own: nothing stored, the preset's cards show again."""
    return {}


def edit_parts(field):
    """`?set=` field of the editor -> (op letter, card id), ("r", None) for `ereset`, None when it is not one (or names no card)."""
    m = _EDIT_RE.fullmatch(field) if isinstance(field, str) else None
    if not m:
        return None
    if m.group(3):
        return ("r", None)
    card = _CARD_BY_CODE.get(m.group(2))
    return (m.group(1), card) if card else None


def edit_field(op, card):
    """The `?set=` field of one editor step: op is a key of EDIT_OPS, card a card id."""
    return "e" + op + CARD_CODES[card]


def apply_edit(current, field, cfg_ui=None, available=None):
    """The cookie after an editor step: only the layout and the hidden list change, the rest is kept. The step is applied to the layout in
    force (the cookie's, else config.ini's, else the preset's), so that the first step of a new editor fixes the whole layout as it was.
    A field that is not a step, or names a card that does not show (unknown, hidden, its feature off), changes nothing: the canonical
    `current` comes back. The result is never longer than COOKIE_MAX (else `current` is kept)."""
    prefs = parse_cookie(current)
    step = edit_parts(field)
    if step is None:
        return dump_cookie(prefs)
    op, card = step
    if op == "r":
        prefs.pop("layout", None)
        prefs.pop("hidden", None)
        return dump_cookie(prefs)
    eff, _src = effective(cfg_ui, current)
    lay = layout_of(eff, available)
    new = _clean({"u": lambda: move(lay, card, -1), "d": lambda: move(lay, card, 1), "s": lambda: resize(lay, card, -1),
                  "g": lambda: resize(lay, card, 1), "h": lambda: hide(lay, card), "w": lambda: show(lay, card)}[op]())
    if new.get("layout", []) == lay["layout"] and new.get("hidden", []) == lay["hidden"]:
        return dump_cookie(prefs)  # nothing moved (the first card up, a card at its widest, an unknown card): do not fix the layout either
    prefs.pop("layout", None)
    if "layout" in new:
        prefs["layout"] = new["layout"]
    prefs["hidden"] = new.get("hidden", [])  # an empty list is stored too: it says 'nothing is hidden', whatever the preset hides
    out = dump_cookie(prefs)
    return out if len(out) <= COOKIE_MAX else dump_cookie(parse_cookie(current))


def custom_layout(source):
    """True when the layout in force is the reader's own (the cookie's or ?ui=): the cards then keep their order (order = fixed) instead of
    moving by severity. `source` is effective()'s second value."""
    return source.get("layout") in ("url", "browser")


# ---------------------------------------------------------------- config.ini [ui]

def _tokens(text):
    """'attention : 2, exposure:2; webapps' -> ['attention:2', 'exposure:2', 'webapps'] (commas, semicolons or blanks, lower case)."""
    return [t for t in re.split(r"[,;\s]+", re.sub(r"\s*:\s*", ":", text.strip().lower())) if t]


def _show(text):
    return text[:40].replace("\n", " ")


def parse_ui(section, sections_default=None):
    """config.ini [ui] (a dict of the section's keys) -> (ui, warnings). Never raises: a bad value is a warning and that key is not set.

    ui always has "web" (classic | app) and "sections" ([dashboard] sections, completed), and then only the keys the file sets and
    that are valid: theme, density, start_view, preset, order (str), kpis (ids), layout [(card, width)] and hidden [card].
    Unknown names are dropped with a warning; a blank value sets nothing, except `hidden =` (nothing is hidden)."""
    warns = []
    ui = {"web": "classic", "sections": _section_order(sections_default)}
    sec = {}
    for k, v in (section.items() if isinstance(section, dict) else ()):
        sec[str(k).strip().lower()] = "" if v is None else str(v)
    for key in sec:
        if key not in UI_KEYS:
            warns.append(f"[ui] unknown key '{_show(key)}' ignored (known: {', '.join(UI_KEYS)})")
    for key, allowed in (("web", WEB_MODES), ("theme", THEMES), ("density", DENSITIES), ("start_view", VIEWS),
                         ("preset", PRESETS), ("order", ORDERS)):
        v = sec.get(key, "").strip().lower()
        if not v:
            continue
        if v in allowed:
            ui[key] = v
        else:
            warns.append(f"[ui] {key} must be one of {', '.join(allowed)}: '{_show(v)}' ignored")
    if "kpis" in sec:
        asked, ids = _tokens(sec["kpis"]), []
        for t in asked:
            if t not in KPI_CODES:
                warns.append(f"[ui] kpis: unknown name '{_show(t)}' ignored (known: {', '.join(KPI_IDS)})")
            elif t in ids:
                warns.append(f"[ui] kpis: '{t}' is listed twice: once")
            else:
                ids.append(t)
        if len(ids) > MAX_KPIS:
            warns.append(f"[ui] kpis: at most {MAX_KPIS}, the first {MAX_KPIS} are used")
            ids = ids[:MAX_KPIS]
        if ids:
            ui["kpis"] = ids
        elif asked:
            warns.append("[ui] kpis: no valid name, the preset's list is used")
    hidden = None
    if "hidden" in sec:
        hidden = []
        for t in _tokens(sec["hidden"]):
            if t not in CARD_CODES:
                warns.append(f"[ui] hidden: unknown card '{_show(t)}' ignored (known: {', '.join(CARDS)})")
            else:
                hidden.append(t)
        ui["hidden"] = hidden = _hidden_order(hidden)
    if "layout" in sec:
        asked, items, seen, hid = _tokens(sec["layout"]), [], set(), set(hidden or ())
        for t in asked:
            name, colon, w = t.partition(":")
            if name not in CARD_CODES:
                warns.append(f"[ui] layout: unknown card '{_show(name)}' ignored (known: {', '.join(CARDS)})")
                continue
            if name in hid:
                warns.append(f"[ui] layout: '{name}' is also in hidden: hidden")
                continue
            if name in seen:
                warns.append(f"[ui] layout: '{name}' is listed twice: the first counts")
                continue
            seen.add(name)
            if not colon:
                width = 1
            elif w.isascii() and w.isdigit():
                width = max(1, min(MAX_WIDTH, int(w)))
                if str(width) != w:
                    warns.append(f"[ui] layout: {name}:{_show(w)} is not a width of 1-{MAX_WIDTH}: {width} is used")
            else:
                width = 1
                warns.append(f"[ui] layout: {name}:{_show(w)} is not a width of 1-{MAX_WIDTH}: 1 is used")
            items.append((name, width))
        if items:
            ui["layout"] = items
        elif asked:
            warns.append("[ui] layout: no valid card, the preset's layout is used")
    return ui, warns


def export_ini(prefs):
    """Preferences (usually the first value of effective()) -> the text of a config.ini [ui] block, what the settings page's Export
    button gives. parse_ui() reads it back to the same preferences. `hidden =` is always written: blank means nothing is hidden."""
    p = _clean(prefs)
    lines = ["[ui]"]
    if isinstance(prefs, dict) and prefs.get("web") in WEB_MODES:
        lines.append(f"web = {prefs['web']}")
    for field, _, _ in _SCALARS:
        if field in p:
            lines.append(f"{field} = {p[field]}")
    if "kpis" in p:
        lines.append("kpis = " + ", ".join(p["kpis"]))
    if "layout" in p:
        lines.append("layout = " + ", ".join(c if w == 1 else f"{c}:{w}" for c, w in p["layout"]))
    if "hidden" in p:
        lines.append(("hidden = " + ", ".join(p["hidden"])).rstrip())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the preferences in force

def _as_prefs(value):
    """A cookie / ?ui= string or an already parsed dict -> clean partial prefs."""
    return _clean(parse_cookie(value) if isinstance(value, str) else value)


def effective(cfg_ui=None, cookie=None, oneshot=None):
    """-> (prefs, source). cfg_ui is cfg["ui"] (parse_ui), cookie the nuc_ui value and oneshot the ?ui= value (strings or parsed dicts).

    prefs has the 8 FIELDS: strings, kpis [id], layout [(card, width)] (the cards that show, in order, as stated: visible_cards() adds
    the cards it leaves out and drops the ones that are off) and hidden [card] (never in layout). source[field] is "url", "browser",
    "config.ini", "preset" or "default": the first of ?ui=, cookie and [ui] that sets the field, else the preset of the moment
    (chosen: "preset"; nobody chose one: "default"; the layout of the default preset follows [dashboard] sections: "config.ini").
    A card that the layout of a stronger source names is not hidden by a weaker source's hidden list."""
    layers = (("url", _as_prefs(oneshot)), ("browser", _as_prefs(cookie)), ("config.ini", _clean(cfg_ui)))
    prefs, source = {}, {}
    for field, _, _ in _SCALARS:
        for name, layer in layers:
            if field in layer:
                prefs[field], source[field] = layer[field], name
                break
        else:
            prefs[field], source[field] = DEFAULTS[field], "default"
    sections = _section_order(cfg_ui.get("sections")) if isinstance(cfg_ui, dict) else list(CARDS)
    base = preset_prefs(prefs["preset"], sections)
    from_preset = "default" if source["preset"] == "default" else "preset"
    for field in ("kpis", "layout", "hidden"):
        for name, layer in layers:
            if field in layer:
                prefs[field], source[field] = list(layer[field]), name
                break
        else:
            prefs[field], source[field] = list(base[field]), from_preset
            if field == "layout" and prefs["preset"] == "default" and sections != list(CARDS):
                source[field] = "config.ini"  # [dashboard] sections
    rank = {"url": 0, "browser": 1, "config.ini": 2, "preset": 3, "default": 3}
    if rank[source["hidden"]] > rank[source["layout"]]:  # a weaker hidden list never hides a card that a stronger layout names
        named = {c for c, _ in prefs["layout"]}
        prefs["hidden"] = [c for c in prefs["hidden"] if c not in named]
    prefs["layout"] = [(c, w) for c, w in prefs["layout"] if c not in prefs["hidden"]]  # layout = the cards that show, in order
    return prefs, source


def visible_cards(prefs, available=None):
    """[(card, width)] to draw, in order: the layout's cards that are available and not hidden, then the available cards that the layout
    and hidden do not mention (new cards show up after an upgrade), each 1 wide. `available`: the card ids that exist and whose feature
    is on, in their default order (nuc_config.SECTIONS when None); anything else in the layout is dropped. Hidden cards never show."""
    p = _clean(prefs)
    avail = []
    for c in CARDS if available is None else available:
        if isinstance(c, str) and c in CARD_CODES and c not in avail:
            avail.append(c)
    hidden = set(p.get("hidden", ()))
    out, seen = [], set()
    for c, w in p.get("layout", ()):
        if c in avail and c not in hidden and c not in seen:
            seen.add(c)
            out.append((c, w))
    return out + [(c, 1) for c in avail if c not in hidden and c not in seen]
