"""The first-run wizard: what the web view shows, how to set it up, and the marker that says it was seen.

Three things live here and nothing else:
  - the marker `setup.json` (state(), mark()): in the folder of the AI and overlay files, or the portable run's `ai` folder. It says the guide was
    finished or skipped. Until it exists the web shell offers a banner ("New here?");
    a marker that cannot be read counts as absent, one that cannot be written costs only a banner that comes back. It holds no setting.
  - the facts (facts()): what is on now, read from the configuration in force. The wizard explains; it never shows a state the data does not support.
  - the words: the five steps as HTML (step_html(), used by web.py). The wizard is the web view's and the desktop app's; the text console has none.
Writing a setting is not done here: step 4's forms post to the settings page's own actions (/settings/feature, /settings/config), behind the same guard
(token, CSRF, Origin, the allowlist nuc_config.OVERLAY_ALLOW). Pure functions; stdlib only."""
import html
import json
import os
import stat
import time

import cards
import nuc_config
import prefs
import ui

MARKER_FILE = "setup.json"
MARKER_MAX = 2048  # bytes; a bigger file counts for nothing
MARKS = ("finished", "skipped")
STEPS = ("What you see", "The screens", "On and off", "Set it up", "Where things live")
SETUP_PATH = "/setup"
DOCS = "https://github.com/give-jd/nuc-console-oss/blob/main/docs/"


# ---- the marker ---------------------------------------------------------------------------------------------------------------------


def marker_path():
    """$NUC_CONSOLE_SETUP, else setup.json next to the overlay (an installation: the AI folder the web view's account owns), else in the portable
    run's own `ai` folder."""
    if os.environ.get("NUC_CONSOLE_SETUP"):
        return os.environ["NUC_CONSOLE_SETUP"]
    ov = nuc_config.overlay_path()
    return os.path.join(os.path.dirname(ov) if ov else os.path.join(os.path.abspath(nuc_config.PORTABLE), "ai"), MARKER_FILE)


def state(path=None):
    """"" while the guide was never finished or skipped; else which one (MARKS). A link, a big file, a file that is not JSON: absent."""
    path = path or marker_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))  # a FIFO planted here must not block a page
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MARKER_MAX:
                return ""
            with os.fdopen(os.dup(fd), encoding="utf-8") as f:
                how = (json.load(f) or {}).get("how")
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError):
        return ""
    return how if how in MARKS else ""


def mark(how, path=None, now=None):
    """Write the marker (aside, never through a link, then put in place). True when written; False when the folder will not take it: the guide
    then simply offers itself again."""
    path = path or marker_path()
    if how not in MARKS:
        raise ValueError("not a mark: %r" % (how,))
    folder = os.path.dirname(os.path.abspath(path))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        if nuc_config.PORTABLE and not os.path.lexists(folder):  # a portable run's own folder; an installation's is the installer's
            os.makedirs(folder, exist_ok=True)
        if os.path.islink(folder) or not os.path.isdir(folder):
            return False
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"how": how, "at": int(now if now is not None else time.time()), "version": nuc_config.VERSION}, f)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def clamp(step):
    """A step number from a URL: 1-5, anything else the nearest end (a word is the first)."""
    try:
        return max(1, min(len(STEPS), int(step)))
    except (TypeError, ValueError):
        return 1


# ---- the facts ----------------------------------------------------------------------------------------------------------------------


def ai_cost():
    """('about 1.4 GB', '1.1-18.6 GB') the model server's and the models' download sizes as the pins say them; ("", "") when they cannot be told."""
    try:
        import aisetup
        gb = lambda n: "%.1f" % (n / 1e9)  # noqa: E731
        srv = [a["size"] for a in aisetup.RUNTIME["assets"].values() if a.get("size")]
        mods = [m["size"] for m in aisetup.MODELS if m.get("size")]
        return (("%s-%s GB" % (gb(min(srv)), gb(max(srv)))) if srv else "", ("%s-%s GB" % (gb(min(mods)), gb(max(mods)))) if mods else "")
    except Exception:  # noqa: BLE001 - a figure the pins do not give is left out, never guessed
        return "", ""


def facts(cfg, token=None, serving=False):
    """What is on now, from the configuration in force `cfg`. token: the running web view's token (settings_mode takes it as the page does), None to read [web] token_file; serving: the answer goes out through the web
    view (so the web view is on, whatever the file says: it is the one reading this)."""
    feats = cfg.get("features") or {}
    web = cfg.get("web") or {}
    mode, why = nuc_config.settings_mode(token=token)
    return {
        "off": [k for k in nuc_config.FEATURES if not feats.get(k, True)],
        "on": [k for k in nuc_config.FEATURES if feats.get(k, True)],
        "web": bool(web.get("enabled")) or serving, "port": web.get("port"), "bind": web.get("bind"),
        "token": bool(token or web.get("token_file")),
        "ai": bool(feats.get("ai", True)), "ai_on": bool((cfg.get("ai") or {}).get("enabled")),
        "ai_remote": bool((cfg.get("ai") or {}).get("allow_remote")),
        "telegram": bool((cfg.get("telegram") or {}).get("enabled")), "detail": (cfg.get("telegram") or {}).get("detail", "titles"),
        "overlay_opt_in": bool(web.get("settings_actions")), "mode": mode, "why": why,
        "portable": bool(nuc_config.PORTABLE),
    }


def yn(on, yes="on", no="off"):
    return yes if on else no


def kpi_names(cfg):
    """The key figures in force, by label (the preset's, or [ui] kpis); [] when the row is switched off."""
    eff = prefs.effective(cfg.get("ui"), "", "")[0]
    return [cards.KPI_LABELS.get(k, k) for k in eff["kpis"]]


# ---- the web steps ------------------------------------------------------------------------------------------------------------------


def esc(x):
    return html.escape(str(x), quote=True)


def link(step):
    return "%s?step=%d" % (SETUP_PATH, step)


def code(text):
    return '<code class="cmd">%s</code>' % esc(text)


def row(label, value, note=""):
    return '<li><span class="wz-k">%s</span><span class="wz-v">%s</span>%s</li>' % (label, value, '<span class="wz-n sm">%s</span>' % note if note else "")


def flag(on, yes="on", no="off"):
    """An on/off word with a symbol that is not the green tick: a switch is not a verdict."""
    return '<b class="wz-%s">%s %s</b>' % ("on" if on else "off", "●" if on else "○", yn(on, yes, no))


def step1(cfg, f, x):
    states = {"ok": "checked and fine", "warn": "needs a look; nothing is broken yet", "err": "wrong now", "down": "something that should run does not",
              "unknown": "could not be read: it is never counted as fine", "info": "a note, nothing to do"}
    legend = "".join('<li><span class="sym s-%s" aria-hidden="true">%s</span> <b>%s</b> <span class="sm">%s</span></li>'
                     % (s, esc(ui.SYMBOLS[s]), s, esc(states[s])) for s in ui.STATES)
    ks = kpi_names(cfg)
    row_now = (", ".join(esc(k) for k in ks) if ks else "none: <code>[ui] kpis = none</code> hides the row and the cards take the space")
    return ('<p>The overview is a row of <b>key figures</b> on top and, below, one <b>card</b> per section. Every figure and card has a state, and a state is a '
            'symbol and a word first; the colour is the third cue, never the only one.</p>'
            f'<h4 class="wz-h">The states</h4><ul class="wz-legend">{legend}</ul>'
            '<h4 class="wz-h">The key figures</h4>'
            f'<p>The row in force on this machine: {row_now}. A figure links to its card; one that cannot be read shows <b>?</b>, never a tick.</p>'
            '<h4 class="wz-h">ATTENTION</h4>'
            '<p>The first card lists what needs you, the worst first. Each item has a stable id, a reason and a fix: <code>nuc-console-problems</code> prints them. '
            'A known item can be <i>accepted</i> (root, with a reason: dimmed, not counted) or, for an operational alarm only, <i>muted</i> (<code>[alerts] mute</code>). '
            'Security alarms can be neither hidden nor muted.</p>')


def step2(cfg, f, x):
    on = set(f["on"])
    rows = []
    for i, (name, feat, title) in enumerate(ui.SCREENS):
        live = not feat or feat in on
        href = {"overview": "/", "map": "/?view=map", "cpu": "/?view=cpu", "health": "/?view=health", "ai": "/?view=ai"}[name]
        what = {"overview": "the key figures and the cards: what is wrong, what is exposed, what the machine does",
                "map": "who reaches what, and what is behind it",
                "cpu": "per-core load, temperatures and the processes",
                "health": "which apps cause trouble over time (needs a few days of history)",
                "ai": "what local model fits this machine, and a chat with it"}[name]
        rows.append(row('<kbd>%d</kbd> <a class="lnk" href="%s">%s</a>' % (i + 1, esc(href), esc(title)), esc(what),
                        "on" if live else "off: [features] %s = no" % feat))
    rows.append(row('<a class="lnk" href="/?view=settings">Settings</a>', "theme, density, the key figures, the sections, config.ini", "web: the gear in the top bar"))
    rows.append(row('<a class="lnk" href="/?view=telegram">Telegram</a>', "alerts on your phone, through a bot of your own", "off unless you pair it"))
    rows.append(row('<a class="lnk" href="/?ui=1.dw&amp;kiosk=1">Wall</a>', "big type for a monitor across the room; lists cut to three rows", "the wall density"))
    return ('<p>Keys <kbd>1</kbd>-<kbd>5</kbd> open the screens on the console, the tabs do on the web; <kbd>?</kbd> shows the keys, <kbd>Esc</kbd> goes back. '
            'A screen switched off in <code>[features]</code> has no key and no tab.</p>'
            f'<ul class="wz-list">{"".join(rows)}</ul>')


def step3(cfg, f, x):
    srv, mods = ai_cost()
    web_note = ("%s:%s%s" % (f["bind"], f["port"], ", with a token" if f["token"] else ", no token (other accounts of this machine can open it)")
                if f["web"] else "off: nothing listens")
    rows = [
        row("Web view", flag(f["web"]), esc(web_note + ". The only listener of the console; read-only except the buttons you see.")),
        row("AI model", flag(f["ai_on"]) + ("" if f["ai"] else " <span class='sm'>(AI screen off)</span>"),
            esc("Off until you turn it on. Local only: the model runs on this machine and is asked at 127.0.0.1"
                + (" (allow_remote is on: check [ai] endpoint)" if f["ai_remote"] else "")
                + ". Costs a download: the model server %s plus a model of %s, and the RAM or GPU to run it." % (srv or "of its pinned size", mods or "its pinned size"))),
        row("Telegram", flag(f["telegram"]),
            esc("Off until you pair a bot. Leaves the machine: the host name and the problem titles"
                + (", and the detail (container names, ports) because detail = full" if f["detail"] == "full" else " (detail = titles: nothing more)") + ", over HTTPS to api.telegram.org.")),
        row("Settings overlay", flag(f["mode"] == "overlay", "available", "not available") if not f["portable"] else flag(f["mode"] == "file", "writes config.ini", "read-only"),
            esc("An installation's config.ini is the administrator's. The settings page can change presentation only if config.ini has "
                "[web] settings_actions = yes and [web] token_file." if not f["portable"] else "A portable run or the desktop app: config.ini is your own file.")),
    ]
    off = f["off"]
    return ('<p>The console reads this machine and sends nothing anywhere unless you switch a piece on. Where it stands now:</p>'
            f'<ul class="wz-list">{"".join(rows)}</ul>'
            '<h4 class="wz-h">What runs as root</h4>'
            '<p>The collector reads the ports, the firewall, the containers and the journal as root (SYSTEM on Windows) and writes files the unprivileged screens read. '
            'Every section you switch off in <code>[features]</code> is <b>not drawn, raises no alarm and is not collected</b>: the less you ask, the less runs as root. '
            + (f'Switched off now: <b>{esc(", ".join(off))}</b>.' if off else 'Nothing is switched off now: every section is collected.')
            + '</p>')


def step4(cfg, f, x):
    """x: 'features' and 'ui' (the forms, built by web.py from the settings page's own code), 'note' (what the last save did), 'path', 'apply'."""
    note = '<div class="cfgn %s" role="status">%s</div>' % ("ok" if x["note"]["ok"] else "bad", "<br>".join(esc(t) for t in x["note"]["lines"])) if x.get("note") else ""
    if f["mode"]:
        how = ('This is a portable run: the choices below are written to %s, the file of your own account, the way the settings page does.' % code(x["path"])
               if f["mode"] == "file" else
               'This installation has the settings overlay: %s is the administrator\'s and stays as it is. What you choose below is kept in %s, '
               'marked <i>set from this page</i>, and can be reset. A section can be switched <b>off</b> here; only %s can switch one on again.'
               % (code(x["path"]), code(x["overlay"]), code("config.ini")))
        body = (f'<p>{how}</p>{note}<h4 class="wz-h">Which sections to show</h4>{x["features"]}'
                f'<h4 class="wz-h">Key figures, preset, theme and density</h4>{x["ui"]}')
    else:
        edit = "sudoedit " + x["path"] if "sudo" in str(x["apply"]).split() else "open %s as administrator" % x["path"]  # the apply command says which family of words
        body = (f'<p class="wz-why"><b>Why there is no form here:</b> {esc(f["why"])}. '
                f'{code("config.ini")} is the administrator\'s on an installation, and the wizard never writes it.</p>'
                f'<p>To choose, edit the file and apply it:</p><pre class="wz-pre" tabindex="0">{esc(edit)}</pre>'
                '<p>and put the lines you want in it:</p>'
                '<pre class="wz-pre" tabindex="0">[features]\n# yes or no; "no" = not drawn, no alarm, not collected\n'
                'map = yes\ncpu = yes\nhealth = yes\nai = yes\n\n[ui]\npreset = default      # default | security | server | desktop\n'
                'kpis = problems, cpu, ram, disk   # up to 8 of the figures, or: none (no row)\ntheme = auto          # auto | dark | light | high-contrast\n'
                'density = desk        # wall | desk | compact</pre>'
                f'<p>then: {code(x["apply"])}</p>'
                '<p class="sm">To let this page do it for you next time, add <code>settings_actions = yes</code> and <code>token_file = &lt;a file&gt;</code> under '
                '<code>[web]</code>: the overlay is opt-in, and it needs a token.</p>')
    return body


def step5(cfg, f, x):
    rows = [row("config.ini", code(x["path"]), "administrator's on an installation; your own in a portable run"),
            row("Settings overlay", code(x["overlay"]) if x["overlay"] else "none in a portable run", "what the settings page changed; delete the file to undo it"),
            row("This guide's marker", code(x["marker"]), "written when you finish or skip; delete it to see the banner again")]
    cmds = [("nuc-console-problems", "every current ATTENTION item with its reason and its fix"),
            ("sudo nuc-console-accept --problem &lt;id&gt; --reason \"...\"", "mark a known item as accepted (root, with a reason)"),
            ("sudo nuc-console-config migrate --dry-run", "bring an old config.ini to this release's layout, keeping your settings")]
    cl = "".join('<li><span class="wz-k"><code class="cmd">%s</code></span><span class="wz-v">%s</span></li>' % (c, esc(t)) for c, t in cmds)
    docs = " &middot; ".join('<a class="lnk" href="%s%s">%s</a>' % (DOCS, p, t) for p, t in (
        ("WEB.md", "the web view"), ("CONFIGURATION.md", "every key"), ("TELEGRAM.md", "Telegram"), ("AI.md", "the AI model"), ("HARDENING.md", "hardening")))
    who = ('<ul class="wz-list"><li><span class="wz-k">The administrator</span><span class="wz-v">config.ini: the web view, Telegram, AI, [expose], the locks</span></li>'
           '<li><span class="wz-k">This page, if opted in</span><span class="wz-v">presentation only: sections (off), [dashboard], [ui], zoom</span></li>'
           '<li><span class="wz-k">The Telegram and AI pages</span><span class="wz-v">their own folders only; <code>web_actions = no</code> locks them</span></li></ul>')
    return (f'<h4 class="wz-h">Files</h4><ul class="wz-list">{"".join(rows)}</ul>'
            f'<h4 class="wz-h">Commands</h4><ul class="wz-list">{cl}</ul>'
            f'<h4 class="wz-h">Who can change what</h4>{who}'
            f'<h4 class="wz-h">Documentation</h4><p>{docs}</p>')


BUILDERS = (step1, step2, step3, step4, step5)


def step_html(n, cfg, f, x, csrf, back=""):
    """Step n (1-5) as the body of the page: the heading, the step, the navigation (plain links), and the two forms that write the marker."""
    n = clamp(n)
    dots = "".join('<li aria-current="step"><b>%d. %s</b></li>' % (i, esc(t)) if i == n else
                   '<li><a class="lnk" href="%s">%d. %s</a></li>' % (link(i), i, esc(t)) for i, t in enumerate(STEPS, 1))
    hid = '<input type="hidden" name="csrf" value="%s">' % esc(csrf)
    prev = '<a class="btn" href="%s">Back</a>' % link(n - 1) if n > 1 else ""
    nxt = ('<a class="btn pri" href="%s">Next</a>' % link(n + 1)) if n < len(STEPS) else (
        '<form class="f" method="post" action="/setup/finish">%s<button class="btn pri" type="submit">Finish</button></form>' % hid)
    skip = '<form class="f" method="post" action="/setup/skip">%s<button class="btn" type="submit">Skip the guide</button></form>' % hid
    return ('<div class="settings wz"><section class="sec" aria-labelledby="sec-wz">'
            f'<h3 class="sech" id="sec-wz">Setup guide &middot; step {n} of {len(STEPS)}: {esc(STEPS[n - 1])}</h3>'
            f'<nav aria-label="Steps"><ol class="wz-steps">{dots}</ol></nav>'
            f'{BUILDERS[n - 1](cfg, f, x)}'
            f'<div class="wz-nav">{prev}{nxt}{skip}</div>'
            '<p class="hintl">Every step can be skipped and the guide is always at <a class="lnk" href="/setup?step=1">/setup</a>, '
            'in the settings page and in the footer. It changes nothing by itself.</p></section></div>')


def banner_html(csrf, to=""):
    """The dismissible banner of the shell: a link into the tour and a form that skips it (POST /setup/skip, the CSRF token)."""
    return ('<div class="stale-banner wz-banner" role="region" aria-label="Setup guide"><span class="sym">·</span> <b>New here?</b> '
            f'<a class="lnk" href="{link(1)}">Take the 2-minute tour</a>'
            f'<form class="f" method="post" action="/setup/skip"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            + (f'<input type="hidden" name="to" value="{esc(to)}">' if to else "")
            + '<button class="btn" type="submit">Dismiss</button></form></div>')
