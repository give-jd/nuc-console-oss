"""nuc-console web view: the address of a page. What the query string may say (`view_params`), the few values each parameter can take, and
the URLs the pages write for themselves (`page_url`, `view_url`, `app_url`); the one small HTML helper the pages share, `choice`.

A leaf of the web view: it imports no other module of it (web.py, webhttp.py, webmap.py, webpages.py import this one), so a URL is
written in one place. Pure functions; nothing here reads the host."""
import html
import re
from urllib.parse import urlencode

import aiweb
import nuc_config
import prefs
import screens
import webcss

APP_PATH = "/app"     # the live app (src/appjs.py): every screen drawn in the browser from the data API, kept up to date by its stream


AI_CONFIRMS = ("on", "delete", "delete-all", "clear")  # the question a page asks before it does that (?view=ai&confirm=...)


ZOOMS = (50, 67, 75, 90, 100, 110, 125, 150, 175, 200)  # text size steps (%), like a browser's: few values, bounded cache


KEY = re.compile(r"[0-9a-f]{10}")  # a map row (graph.path_key): the only thing a map URL names
PID = re.compile(r"[0-9]{1,10}")   # a process (cpu view): ASCII digits only, at most MAX_PID
MAX_PID = 2 ** 32 - 1              # a Windows pid is 32 bits
NUM = re.compile(r"[0-9]{1,6}")    # a number in a URL: '²'.isdigit() is true but int('²') raises, so does int() of 5000 digits
MAX_KEYS = 300       # open= / shut= keys taken from a URL, each


HEALTH_SEL_MAX = 200  # characters of a finding id taken from a URL (an id is "<rule>:<subject>", the subject is capped at 64)


AI_SEL_MAX = screens.AI_ID_MAX + 1  # characters of a model id taken from a URL: one more than an id has, so that a longer text never equals one


GZOOMS = webcss.GZOOMS  # z=: the drawing's size in % of the window (no script needed)


def view_params(q):
    """Query string -> page() arguments, clamped and rounded to a few distinct values (bounded cache and CPU).

    cols/rows: the layout grid (rows 0 = config); zoom: text size in % (0 = [display] zoom); fit=1: the text fills the window width (and the
    height, when rows is given), so a bigger zoom means fewer columns, re-laid out; full=1: every Details page;
    rotate=1: overview and Details pages take turns like on the console; kiosk=1: the footer says how to close the window;
    rotate=N (3-600): the same, and on the shell's wall page (kiosk=1 with the wall density) the seconds between two one-screen scrolls
    (default [dashboard] rotate_seconds);
    refresh: seconds between two reloads (1-10; default [dashboard] refresh_seconds).
    view=map: the MAP, whose state is all in the URL: open=/shut= the branches opened/closed by hand (row keys joined by '.'),
    all=1 everything open, sel= the row whose details are shown, only=1 problems only, pause=1 no reload.
    as=graph: the MAP drawn as a graph (anything else: the tree), with stacks=1 the compose projects, ext=0 no remote
    addresses, local=1|2 only the selected node and its neighbours within 1 or 2 hops, z= the drawing's size in % (GZOOMS).
    view=cpu: the CPU screen: sort=mem|time|pid|user (default cpu), sel= the pid whose details are shown (only digits; dropped when
    no such process).
    view=health: the HEALTH page: period=1|7|30 (days, default 7), sel= the id of the finding whose details are shown (page() drops one the
    report does not have), pause=1 no reload.
    view=ai: the AI page: sel= the id of the model whose details are shown (page() drops one the catalog does not have), pause=1 no reload,
    confirm=on|delete|delete-all|clear the question the page asks first (page() drops one that does not apply; on and delete are about sel),
    prompt= the id of the chat's exchange whose whole prompt is shown (page() drops one the chat does not have).
    view=telegram: the Telegram page (pair this machine with your bot, switch the alerts, test); open=1 there: a redirect to the t.me link of the
    pairing that waits (Handler._tg_open).
    edit=1: the layout editor (the shell's overview in edit mode; with app=0 or another view it is dropped).
    The shell: app=1 (0: the classic page, whatever [ui] web says), ui=<the preferences string> for this URL only (prefs.parse_cookie: an invalid one
    is dropped), view=settings (the settings page), card=<id> (one card of the overview in full), pause=1 (the overview too)."""
    one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
    num = lambda k: int(one(k)) if NUM.fullmatch(one(k)) else 0  # noqa: E731
    cols, rows, zoom = num("cols"), num("rows"), num("zoom")
    view = one("view") if one("view") in ("map", "cpu", "health", "ai", "settings", "telegram") else ""
    edit = not view and one("edit") == "1" and one("app") != "0"  # the layout editor: the shell's overview in edit mode (the classic page has none)
    sel = one("sel") if view == "map" and KEY.fullmatch(one("sel")) else \
        one("sel")[:HEALTH_SEL_MAX] if view == "health" else one("sel")[:AI_SEL_MAX] if view == "ai" else \
        str(int(one("sel"))) if view == "cpu" and PID.fullmatch(one("sel")) and int(one("sel")) <= MAX_PID else ""  # '007' is pid 7: one URL
    return {"cols": max(60, min(300, (cols + 10) // 20 * 20)) if cols else 0,
            "rows": max(20, min(120, (rows + 2) // 4 * 4)) if rows else 0,
            "zoom": min(ZOOMS, key=lambda z: abs(z - zoom)) if zoom else 0, "fit": one("fit") == "1", "full": one("full") == "1",
            "rotate": True if one("rotate") == "1" else num("rotate") if 3 <= num("rotate") <= 600 else False,
            "kiosk": one("kiosk") == "1",
            "refresh": max(nuc_config.REFRESH_MIN, min(nuc_config.REFRESH_MAX, num("refresh"))) if num("refresh") else 0,  # 0 = config
            "app": one("app") if one("app") in ("0", "1") else "", "ui": ui_oneshot(one("ui")),
            "card": one("card") if not view and one("card") in prefs.CARDS and not edit else "", "edit": edit,
            "view": view, "open": map_keys(one("open")), "shut": map_keys(one("shut")),
            "all": one("all") == "1", "sel": sel, "only": one("only") == "1", "pause": one("pause") == "1",
            "as": "graph" if one("as") == "graph" else "", "stacks": one("stacks") == "1",
            "ext": "0" if one("ext") == "0" else "",  # remote addresses are shown unless ext=0 (the only value written)
            "local": int(one("local")) if one("local") in ("1", "2") else 0,
            "z": gzoom(num("z")),
            "sort": one("sort") if view == "cpu" and one("sort") in screens.CPU_SORTS[1:] else "",
            **({"period": {"1": 1, "7": 7, "30": 30}.get(one("period"), 0)} if view == "health" else {}),
            **({"confirm": one("confirm") if one("confirm") in AI_CONFIRMS else "",
                "prompt": one("prompt") if aiweb.CHAT_ID.fullmatch(one("prompt")) else ""} if view == "ai" else {})}


APP_VIEWS = ("cpu", "health", "map", "ai")  # the screens the live app draws besides the overview (settings, Telegram, the graph: the shell's)
HERE_KEYS = ("cols", "rows", "zoom", "fit", "full", "rotate", "kiosk", "refresh", "app", "ui")  # the size, refresh and interface parameters every view has
VIEW_KEYS = {"map": ("open", "shut", "all", "sel", "only", "pause", "as", "stacks", "ext", "local", "z"), "cpu": ("sort", "sel"),
             "health": ("period", "sel", "pause"), "ai": ("sel", "pause"), "": ("card", "edit")}  # and what each view reads besides (settings, telegram: nothing)


def ui_oneshot(raw):
    """?ui= -> the canonical preferences string it holds ('' when invalid or empty): it is part of the URL, so of the cache key."""
    got = prefs.parse_cookie(raw)
    return prefs.dump_cookie(got) if got else ""


def view_url(p):
    """The address of a view as view_params() read it, for the redirect that follows ?token=: only validated values of the parameters that
    view reads (an unknown one, `token` included, is not echoed), the defaults left out, like the links of the pages."""
    p = map_mode(p) if p["view"] == "map" else p
    p = {k: p[k] for k in ("view",) + HERE_KEYS + VIEW_KEYS.get(p["view"], ()) + (("pause",) if p["app"] == "1" and p["view"] == "" else ())}  # the shell's overview pauses too
    p.update({k: ".".join(p[k]) for k in ("open", "shut") if k in p})  # row keys: 'k1.k2', like the pages' own links
    return page_url(p).rstrip("?")  # nothing left: "/"


def app_url(p):
    """The address of a screen of the live app for view_params() p: the screen and the parameters it takes, the defaults left out."""
    view = p["view"] if p["view"] in APP_VIEWS else ""
    keep = {"cpu": ("sort", "sel"), "health": ("period", "sel"), "map": ("open", "shut", "all", "sel", "only"), "ai": ("sel", "confirm", "prompt")}.get(view, ())
    q = {"view": view}
    q.update((k, ".".join(p[k]) if k in ("open", "shut") and not isinstance(p[k], str) else p[k]) for k in keep if k in p)
    return APP_PATH + ("?" + urlencode([(k, "1" if v is True else v) for k, v in q.items() if v not in (0, False, None, "")]) if any(
        v not in (0, False, None, "") for v in q.values()) else "")


def gzoom(z):
    """z= -> the nearest step of GZOOMS; 100 (the default) and nothing are 0, so one view has one URL."""
    z = min(GZOOMS, key=lambda g: abs(g - z)) if z else 0
    return 0 if z == 100 else z


def map_mode(state):
    """The map parameters of one mode only: the tree's open/shut/all mean nothing to the graph, the graph's filters nothing to
    the tree (no URL or cache key carries them there), local nothing without a selected node."""
    s = dict(state)
    if s.get("as") == "graph":
        s.update(open=(), shut=(), all=False, z=gzoom(s.get("z") or 0))
        s["local"] = s.get("local") if s.get("local") in (1, 2) and s.get("sel") else 0
    else:
        s.update({"as": ""}, stacks=False, ext="", local=0, z=0)
    return s


def map_keys(raw):
    """'k1.k2...' -> the valid row keys, deduplicated and sorted (equal views share one URL and one cache entry), at most MAX_KEYS."""
    return tuple(sorted({k for k in raw.split(".") if KEY.fullmatch(k)})[:MAX_KEYS])


def choice(opts):
    """A small segmented switch: [(text, URL)], the current option's URL None (highlighted, not a link)."""
    return ('<span class="mv">' + "".join(f"<b>{t}</b>" if u is None else f'<a href="{html.escape(u)}">{t}</a>' for t, u in opts)
            + "</span>")


def page_url(params, **change):
    """The current view with some parameters changed (only the ones that differ from the defaults are written)."""
    p = dict(params, **change)
    return "/?" + urlencode([(k, "1" if v is True else v) for k, v in p.items() if v not in (0, False, None, "")])
