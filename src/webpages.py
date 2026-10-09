"""nuc-console web view: the HTML of the pages made before the shell, and the models the shell takes from them: Telegram, HEALTH and AI.

The Telegram page (`telegram_html`), the HEALTH page (`health_nodes`, `health_native`, `health_body`) and the AI page (`ai_nodes`, `ai_native`,
`ai_body` and its forms: `AiUi`, `ai_row_html`, `ai_panel_html`, `ai_control_html`, `ai_chat_html`, `ai_manage_html`). Moved from web.py unchanged.
Functions of the data and of the links they are given; the Server of web.py calls them and owns the caches, the engines and the CSRF token."""
import html
import sys
import time
from urllib.parse import urlsplit

import advisor
import aisetup
import ansi
import htmlview
import render
import screens
import tgweb
import ui
from htmlview import sgr_class, to_html
from ui import dd, hclean, hnum
from weburl import LEVEL_CLASS, page_url

PILL_CLASS = {"err": "r", "warn": "y", "info": "d"}  # htmlview.HEALTH_CSS


def telegram_html(snap, csrf, back, here):
    """The Telegram page (?view=telegram): are the alerts on and do they reach anyone, the buttons (on/off, test), what the notifier says, and the
    pairing: the steps and the form (bot token, @username), or the link to press Start on while it waits. Every button is a form that posts to
    /telegram/<action> with the CSRF token and the view to come back to; a locked page, a portable run and a notifier that does not listen
    have none. snap: tgweb.Engine.snapshot(); here: the page's parameters."""
    esc = html.escape
    t, st, job, os_name = snap["settings"], snap["status"], snap["job"], snap["os"]
    running = bool(job and job["state"] in tgweb.RUNNING)
    can = not snap["locked"] and snap["listening"]
    hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v, quote=True)}">' for k, v in (("csrf", csrf), ("back", back)))

    def button(action, label, cls="", title="", disabled=False):
        if not can:
            return ""
        return (f'<form class="f" method="post" action="/telegram/{action}">{hidden}<button class="bt {cls}" type="submit"'
                + (f' title="{esc(title, quote=True)}"' if title else "") + (" disabled" if disabled else "") + f">{esc(label)}</button></form>")
    cmd = lambda text: f'<code class="cmd">{esc(text)}</code>'  # noqa: E731
    state, line = tgweb.state_of(snap)
    pill, cls = {"on": ("ON", "g"), "off": ("OFF", "d"), "unpaired": ("NOT PAIRED", "y"), "failing": ("FAILING", "r"), "down": ("DOWN", "r")}[state]
    busy = snap["busy"]
    acts = []
    if t["by"] == "config":
        acts.append('<span class="d">on by config.ini ([telegram] enabled = yes): switch it off there</span>')
    elif t["enabled"]:
        acts.append(button("off", "Switch off", "off", "no more alerts; the pairing stays (the chat is told)", busy))
    elif st.get("paired") or t["username"]:
        acts.append(button("on", "Switch on", "on", "the ATTENTION changes go to the paired chat again", busy))
    if st.get("paired"):
        acts.append(button("test", "Send a test", "", "a test message to the paired chat", busy))
    out = ['<div class="settings av" id="tg"><section class="sec" aria-labelledby="sec-tg"><h3 class="sech" id="sec-tg">Telegram alerts</h3>',
           '<p class="hintl">New and resolved ATTENTION problems of this machine on your phone, through a Telegram bot of your own. '
           'The machine only sends: it never reads your messages and opens no port.</p>',
           f'<div class="controls"><span class="pl {cls} big">{pill}</span><span class="ci">{esc(line)}</span>{"".join(acts)}</div>']
    if snap["pending"]:
        out.append('<div class="ask"><p>asked the notifier: waiting for its answer…</p></div>')
    note = snap["notice"]
    if note:
        out.append(f'<div class="ctl"><div class="note {"ok" if note["ok"] else "bad"}" role="status">{esc(note["text"])}</div></div>')
    if snap["locked"]:
        out.append(f'<p class="d">{esc(tgweb.LOCKED)}' + ("" if snap["portable"] else f': {cmd(tgweb.CLI[os_name] + " --setup")}') + "</p>")
    elif not snap["listening"]:
        out.append('<p class="d">the notifier service does not take this page\'s requests now (it is not running, or it started before they were '
                   'allowed): ' + (esc(snap["start"]) if snap["portable"] else f'start it with {cmd(snap["start"])}') + ', then reload this page</p>')
    ts = lambda v: (time.strftime("%Y-%m-%d %H:%M", time.localtime(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else "never")  # noqa: E731
    rows = [("sends to", "@" + esc(t["username"]) if t["username"] and st.get("paired") else "nobody yet: pair it below"),
            ("switched on", {"config": "in config.ini", "web": "on this page"}.get(t["by"], "no")),
            ("notifier", "running, takes this page's requests" if snap["listening"] else "running" if tgweb._fresh(st, snap["now"]) else "not running"),
            ("last sent", esc(ts(st.get("last_sent_ts")))),
            ("last error", esc(notify_clean(st.get("last_error"))) if st.get("last_error") else "none"),
            ("content", ("titles only" if t["detail"] == "titles" else "titles and the problems' text (names, ports)")
             + f' ([telegram] detail = {esc(t["detail"])}), resolved problems {"too" if t["resolved"] else "not told"}')]
    out.append('<dl class="about">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl></section>")
    out.append('<section class="sec" aria-labelledby="sec-tgp" id="tg-pair"><h3 class="sech" id="sec-tgp">'
               + ("Pair again" if st.get("paired") else "Pair it with your Telegram") + "</h3>")
    if running:
        out.append(f'<div class="controls"><span class="pl c big">PAIRING</span><span class="ci">{esc(tgweb.job_text(job, snap["now"]))}</span>'
                   + (button("cancel", "Cancel", "stop", "nothing is paired") if job["state"] in ("checking", "waiting") else "") + "</div>")
        if job["state"] == "waiting":
            open_href = esc(page_url(dict(here, view="telegram", open="1")))
            out.append(f'<div class="ask"><p>On the phone where you use Telegram as <b>@{esc(job["username"])}</b>, open this link and press <b>Start</b>:</p>'
                       f'<div class="ask-b"><a class="bt on" href="{open_href}">Open in Telegram</a></div></div>'
                       f'<p class="hintl">Or type it in the phone\'s browser: {cmd(job["link"])}. Only a Start from @{esc(job["username"])} with this code pairs; '
                       "this page sees it by itself.</p>")
    elif can and not busy:
        cli = tgweb.CLI[os_name] + " --setup"
        out.append('<ol class="hintl">'
                   '<li>In Telegram open <b>@BotFather</b>, send <b>/newbot</b>, choose a name and a username ending in <i>bot</i>, and copy the '
                   '<b>token</b> it gives you. It is a password: whoever has it can write as your bot.</li>'
                   '<li>Paste it below with your own <b>@username</b> (Telegram: Settings &gt; Username): only that person can pair.</li>'
                   '<li>Press Pair, then open the link this page shows and press <b>Start</b>.</li>'
                   '<li>Once it says <i>paired</i>, press <b>Send a test</b> at the top of this page: the message reaches your phone in a few seconds.</li></ol>'
                   f'<form class="f" method="post" action="/telegram/pair">{hidden}'
                   '<div class="fs"><label class="lab" for="tg-token">Bot token</label>'
                   '<input class="q" id="tg-token" type="password" name="token" maxlength="100" autocomplete="off" required placeholder="123456789:AA…"></div>'
                   '<div class="fs"><label class="lab" for="tg-user">Your Telegram @username</label>'
                   f'<input class="q" id="tg-user" type="text" name="username" maxlength="33" autocomplete="off" required placeholder="@your_name" value="{esc(("@" + t["username"]) if t["username"] else "", quote=True)}"></div>'
                   '<button class="bt on" type="submit">Pair</button></form>'
                   + ('<p class="hintl">The token goes to the notifier, which keeps it in its own folder, readable only by your account. '
                      if snap["portable"] else '<p class="hintl">The token goes to the notifier service, which keeps it where this web view cannot read it again. ')
                   + ("A new pairing replaces the old one, and the chat paired now is told. " if st.get("paired") else "")
                   + ("" if snap["portable"] else f"The same on the machine: {cmd(cli)}.") + "</p>")
    elif not can and not snap["portable"]:
        out.append(f'<p class="hintl">On the machine: {cmd(tgweb.CLI[os_name] + " --setup")} (docs/TELEGRAM.md).</p>')
    out.append("</section></div>")
    return "".join(out)


def notify_clean(text):
    """A line of the notifier's status for the page: one line, no bot token, short."""
    return render.TELEGRAM_TOKEN.sub("<token>", ui.safe(str(text)))[:160]


def sel_index(data, sel):
    return next((i for i, f in enumerate(screens.health_findings(data["report"])) if f["id"] == sel), 0)


def health_extra_html(report):
    """The ADVICE block under the findings list: the advisor's CACHED answer (render.health_advice: never a generation on a request path),
    as the advisor's own escaped HTML, or "" when the advisor is off. The console calls render.health_extra_lines(report, w) at the same place."""
    on_, res = render.health_advice(report)
    if not on_:
        return ""
    try:
        import advisor
        return advisor.html(res) if res else ('<div class="advice"><p class="advice-head">ADVICE (AI) — none yet</p><p>'
                                              f'{html.escape(render.ADVICE_NONE)}</p></div>')
    except Exception:  # noqa: BLE001 - a broken advisor is no advice
        return ""


def health_advice_node(report):
    """The ADVICE block of the Health page as a ui.Advice (health_extra_html's twin: the advisor's CACHED answer, never a generation on a request
    path), or None when the advisor is off. A broken advisor is no advice."""
    on_, res = render.health_advice(report)
    if not on_:
        return None
    try:
        if not res:
            return ui.Advice("ADVICE (AI) \u2014 none yet", [[render.ADVICE_NONE]], kind="none")
        p = advisor.parts(res)
        return ui.Advice(p["head"], p["paras"], p["notes"], p["kind"]) if p else None
    except Exception:  # noqa: BLE001
        return None


def health_nodes(smp, days, sel, here):
    """The Health screen's components for the web (screens.health_model): the period as links, the findings and the details of `sel`, the ADVICE
    block, the sections of tables. here: the page's parameters (the links of the periods change only the period). The page and the data API draw these."""
    data, _pb = render.health_state(smp, days)
    fl = screens.health_findings(data["report"])
    hv = screens.HealthView(days)
    hv.cur = sel
    advice = health_advice_node(data["report"]) if data["report"] is not None else None
    href = lambda d: page_url(here, period=d if d != 7 else 0)  # noqa: E731
    return screens.health_model(data, hv, fl, advice, href, time.time())


def health_native(smp, days, sel, here):
    """The Health screen of the shell, drawn from components (screens.health_model): the period as links with their keys, the findings with their
    details, the ADVICE block, the sections of tables. here: the page's parameters; the links of the periods change only the period."""
    try:
        return '<div class="hv">' + "".join(htmlview.html(n) for n in health_nodes(smp, days, sel, here)) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: health render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the health screen could not be drawn (see the service log)</p>'


def ai_chat_nodes(snap, prompt_url=None, now=None):
    """The chat of the engine's snapshot as ui.Qa nodes, oldest first: the question, and the answer as a ui.Advice (advisor.parts: escaped by the renderer,
    cleaned and capped by the advisor) or, while the model is still writing it, what it is doing and for how long. A question that failed shows why.
    Each says when it was answered and in how long, and links to its whole prompt (prompt_url(id) -> the page that shows it; None: no link)."""
    out, now = [], time.time() if now is None else now
    for e in snap["chat"]["history"]:
        p = advisor.parts(e["res"] if e["res"] else {"error": e["error"] or "no answer"})
        when = time.strftime("%d %b %H:%M", time.localtime(e["at"])) if e.get("at") else ""
        note = " · ".join(x for x in (when, "in " + screens.ai_secs(e["took"]) if e.get("took") is not None else "") if x)
        link = prompt_url(e["id"]) if prompt_url and e.get("prompt_n") and e.get("id") else None
        out.append(ui.Qa(e["kind"], e["q"], ui.Advice(p["head"], p["paras"], p["notes"], p["kind"]) if p else None, key=e.get("id") or "", note=note,
                         prompt_href=link))
    pend = snap["chat"]["pending"]
    if pend:
        took = screens.ai_secs(now - pend["started"]) if pend.get("started") else ""
        wait = "%s%s (a small model on a slow CPU may need a minute; this page updates by itself)" % (pend.get("step") or "the model is writing the answer",
                                                                                                        " · " + took if took else "")
        out.append(ui.Qa(pend["kind"], pend["q"], None, True, key=pend.get("id") or "", wait=wait))
    return out


def ai_asked(rows, sel, confirm, prompt):
    """The question a page asks first and the prompt it shows, as far as they apply (confirm=delete: sel is installed, on: a model is selected,
    delete-all: something is installed, clear: the chat has something; prompt: an exchange of the chat with a prompt) -> (confirm, prompt)."""
    hist = None
    if confirm == "clear" or prompt:
        try:
            hist = render.ai_engine().snapshot()["chat"]["history"]
        except Exception:  # noqa: BLE001 - no engine: nothing to clear or to show
            hist = []
    if confirm == "delete" and not any(m["id"] == sel and m["installed"] for m in rows) or confirm == "on" and not sel \
            or confirm == "delete-all" and not any(m["installed"] for m in rows) or confirm == "clear" and not hist:
        confirm = ""
    if prompt and not any(e.get("id") == prompt and e.get("prompt_n") for e in hist or []):
        prompt = ""
    return confirm, prompt


def ai_prompt_node(eng, prompt, ahere):
    """The ui.Prompt of the exchange `prompt` (an id the chat has: ai_asked checked it), closed by a link to the page without it; None for ''."""
    if not prompt:
        return None
    msgs, e = eng.prompt_of(prompt)
    if not e or not msgs:
        return None
    return ui.Prompt(("advice: " if e["kind"] == "advise" else "you: ") + e["q"], [(m["role"], m["text"]) for m in msgs], page_url(ahere, prompt=""))


def ai_nodes(srv, ahere, sel, confirm, snap, eng, prompt=""):
    """The AI screen's components for the web (screens.ai_model): the switch and its progress, the model's state, the chat (and the whole prompt of
    one exchange, when prompt names it), the hardware and the status, the models with their buttons (ui.Action: each posts to /ai/* with the CSRF
    token and the page to come back to; none when the page is locked), the details. ahere: the page's parameters. The page and the data API draw these."""
    data, _pb = render.ai_state(srv.smp)
    rows = screens.ai_rows(data["cat"])
    locked = snap["locked"]
    there = dict(ahere, prompt=prompt)
    acts = None if locked else screens.AiActs(srv.csrf, urlsplit(page_url(there)).query)
    links = screens.AiLinks(lambda mid: page_url(there, sel="" if mid == sel else mid), page_url(there, sel=""), page_url(there))
    chat = ai_chat_nodes(snap, lambda eid: page_url(ahere, prompt="" if eid == prompt else eid) + "#prompt")
    return screens.ai_model(data, render.ai_status(), snap, None if locked else eng.choice(), rows, sel, "" if locked else confirm, acts, links,
                            chat, ai_prompt_node(eng, prompt, ahere))


def ai_native(srv, ahere, sel, confirm, snap, eng, prompt=""):
    """The AI screen of the shell, drawn from components (screens.ai_model, the model the console draws from too): the switch and its progress, the
    chat, the hardware and the status, the models as a table with their buttons, the details, what can be deleted. Every button is a form that posts
    to /ai/* with the CSRF token and the page to come back to, as on the classic page; a locked page has none. ahere: the page's parameters."""
    try:
        return '<div class="scr av">' + "".join(htmlview.html(n) for n in ai_nodes(srv, ahere, sel, confirm, snap, eng, prompt)) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: ai render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the AI screen could not be drawn (see the service log)</p>'


class AiUi(object):
    """What the AI page needs to draw its forms: the engine's snapshot and choice (None: locked, no forms), the catalog, the rows, the selected model, the
    question asked first, the CSRF token, the view to come back to. Every form is a button that posts to /ai/<action>."""

    def __init__(self, snap, choice, cat, rows, sel, confirm, csrf, back, here):
        self.snap, self.ch, self.cat, self.rows, self.sel, self.confirm, self.csrf, self.back, self.here = snap, choice, cat or {}, rows, sel, confirm, csrf, back, here
        self.locked = snap["locked"]
        self.working = bool(snap["job"] and snap["job"]["state"] == "running")  # a job runs: its buttons wait (the engine would say "busy")
        self.prompt = None  # the ui.Prompt of the exchange whose prompt the page shows

    def form(self, action, label, fields=(), cls="", title="", disabled=False):
        """One button: a form that posts to /ai/<action> with the CSRF token and the view to come back to (ids and numbers only in `fields`)."""
        if self.locked:
            return ""
        esc = lambda v: html.escape(str(v), quote=True)  # noqa: E731
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in (("csrf", self.csrf), ("back", self.back)) + tuple(fields))
        return (f'<form class="f" method="post" action="/ai/{action}">{hidden}<button class="bt {cls}" type="submit"'
                + (f' title="{esc(title)}"' if title else "") + (" disabled" if disabled else "") + f'>{html.escape(label)}</button></form>')

    def row(self, i):
        return next((r for r in self.rows if r["id"] == i), None)


def ai_use_cell(r, ui):
    """The button that does everything for a model (use this model), or the reason there is none."""
    esc = html.escape
    if not r["pinned"]:
        return '<span class="d">not pinned yet</span>'
    if r["verdict"] == "no":
        return f'<span class="d" title="{esc(r["why"], quote=True)}">too big</span>'
    if r["active"] and ui.snap["state"][0] in ("running", "on"):
        return '<span class="g">● in use</span>'
    return ui.form("use", "use this model", (("model", r["id"]),), "use", "download it if needed, start it, turn AI on", ui.working)


def ai_row_action(r, ui):
    """The last cell of a model's row."""
    return "" if ui is None or ui.locked else f'<td class="ac">{ai_use_cell(r, ui)}</td>'


def ai_row_html(r, i, sel, here, ui=None):
    """One model of the table: its marks, its name (a link: the details), the columns of the console, the pill with its symbol, and the button."""
    esc, cur = html.escape, r["id"] == sel
    label, _, cls, _ = screens.AI_VERDICT.get(r["verdict"], screens.AI_UNKNOWN)
    marks = "".join(f'<span class="{col}" title="{what}">{sym}</span>' if on else " "
                    for sym, col, what, on in (("★", "y", "recommended", r["rec"]), ("✓", "g", "installed", r["installed"]), ("●", "c", "active", r["active"])))
    on, no = ' class="sel"' if cur else "", ' class="no"' if r["verdict"] == "no" else ""  # the selected row, a model that is too big (dim)
    return (f'<tr{on} id="m-{i}"><td class="mk">{marks}</td>'
            f'<td{no}><a class="lb" href="{esc(page_url(here, sel="" if cur else r["id"]) + f"#m-{i}")}">{esc(r["name"])}</a></td>'
            f'<td class="pm">{esc(r["params"])}</td><td class="r sz">{esc(screens.ai_mb(r["size_mb"]))}</td><td class="r">{esc(screens.ai_mb(r["need_mb"]))}</td>'
            f'<td><span class="pl {cls}">{esc(label)}</span></td><td>{esc(screens.ai_tok(r["tok"]))}</td><td class="nn"><div>{esc(r["notes"])}</div></td>'
            + ai_row_action(r, ui) + "</tr>")


def ai_panel_html(r, here, i, windows, ui=None):
    """The details of a model: what the console's pane says, as a table (the commands selectable in one click), what can be done with it
    (use it; delete its files: asked about first), and a link that closes it."""
    esc = html.escape
    title, items = screens.ai_details(r, windows)
    trs = [f'<tr class="top"><th>model</th><td>{esc(title)}</td></tr>']
    for label, value, kind in items:
        cell = (f'<span class="pl {screens.AI_VERDICT[kind][2]}">{esc(value)}</span>' if kind in screens.AI_VERDICT else f'<code class="cmd">{esc(value)}</code>' if kind == "cmd"
                else f'<span class="{"y" if kind == "warn" else "d"}">{esc(value)}</span>' if kind in ("warn", "dim") else esc(value))
        trs.append(f"<tr><th>{esc(label)}</th><td>{cell}</td></tr>")
    if ui is not None and not ui.locked:
        acts = ai_use_cell(r, ui)
        if r["installed"]:
            acts += " " + ui.form("delete", "delete its files", (("model", r["id"]),), "del", "asks first", ui.working)
        if acts:
            trs.append(f"<tr><th>do</th><td>{acts}</td></tr>")
    return (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span><a href="{esc(page_url(here, sel="") + f"#m-{i}")}">close ✕</a></div>'
            f'<table>{"".join(trs)}</table></aside>')


def ai_control_html(ui):
    """The top of the page: is the AI on, what is it doing (a download with its progress, the model loading), the button (turn it on, turn it off, cancel),
    the folder the models go to, the answer to the last click, and the question a click asked first (confirm=)."""
    esc, snap, ch = html.escape, ui.snap, ui.ch
    state, text = snap["state"]
    pill, cls = {"off": ("OFF", "d"), "working": ("WORKING", "c"), "running": ("ON", "g"), "on": ("ON", "g"), "error": ("ERROR", "r")}[state]
    out = ['<section class="ctl" id="ctl"><div class="row">' + f'<span class="pl {cls} big">{pill}</span><span class="st">{esc(text)}</span>']
    if ui.locked:
        out.append('<span class="d">locked by config.ini ([ai] web_actions = no): this page only shows</span>')
    elif state == "working":
        out.append(ui.form("cancel", "Cancel", (), "stop", "stop it: what was fetched is kept"))
    elif snap["switch"]["by"] == "config":
        out.append('<span class="d">on by config.ini ([ai] enabled = yes): turn it off there</span>')
    elif snap["switch"]["on"]:
        out.append(ui.form("off", "Turn AI off", (), "off", "stop the model server and turn the advisor off"))
    else:
        out.append(ui.form("on", "Turn AI on", (), "on", "set up the model and start it"))
    out.append("</div>")
    job = snap["job"]
    if state == "working":
        out.append('<div class="row"><progress max="100" value="%d"></progress> %s</div>' % (job["pct"], "%d%%" % job["pct"]) if job["phase"] == "downloading" and job["total"]
                   else '<div class="row"><progress max="100"></progress></div>')
    out += [htmlview.html(n) for n in screens.ai_model_lines(snap, None if ui.locked else screens.AiActs(ui.csrf, ui.back))]
    if not ui.locked and state == "off" and ch:
        t = ui.row(ch["target"])
        name, size = esc(t["name"]) if t else "", ch["size"]
        todo = "installed here" if ch["installed"] else ("%s to download" % esc(screens.ai_mb((size or 0) / 2 ** 20)) if size else "not downloadable yet")
        if ch["model"] and t:
            out.append(f'<div class="d">turning it on uses <strong>{name}</strong> ({"chosen on this page" if ch["by"] == "page" else "[ai] model in config.ini"}; {todo})</div>')
        elif t:
            out.append(f'<div class="d">no model is chosen yet: turning it on asks about the recommended one, <strong>{name}</strong> ({todo})</div>')
        else:
            out.append('<div class="d">no model fits this machine comfortably: choose one from the list below</div>')
    cat = ui.cat
    if cat.get("dir"):
        out.append('<div class="row dir">models are downloaded to <code class="cmd">%s</code> · %s</div>'
                   % (esc(hclean(cat["dir"], 200)), esc(aisetup.space_text(cat.get("space")))))
    note = snap["notice"]
    if note:
        out.append('<div class="note %s">%s</div>' % ("ok" if note["ok"] else "bad", esc(note["text"])))
    if ui.confirm and not ui.locked:
        t = ui.row(ui.sel)
        if ui.confirm == "on" and t:
            what = "Turn AI on with <strong>%s</strong>? %s" % (esc(t["name"]), "It is installed here." if ch and ch["installed"] else
                   "It is not here yet: <strong>%s</strong> to download (the SHA-256 is checked), then the model server starts on this machine and the advisor is turned on."
                   % esc(screens.ai_mb(((ch or {}).get("size") or 0) / 2 ** 20)))
            yes = ui.form("on", "Yes, turn it on", (("model", ui.sel), ("confirm", "yes")), "on")
        elif ui.confirm == "delete" and t:
            what = "Delete the files of <strong>%s</strong> (%s)? You can download it again later." % (esc(t["name"]), esc(screens.ai_mb(t["size_mb"])))
            yes = ui.form("delete", "Yes, delete", (("model", ui.sel), ("confirm", "yes")), "off")
        elif ui.confirm == "clear":
            n = len(snap["chat"]["history"])
            what = "Clear the chat? Its %d question%s and answer%s, and the prompts they were sent, are deleted for good." % (n, "s" * (n != 1), "s" * (n != 1))
            yes = ui.form("clear", "Yes, clear the chat", (("confirm", "yes"),), "off")
        elif ui.confirm == "delete-all":
            what = "Delete the runtime and every downloaded model (%s)? You can download them again later." % esc(aisetup.fmt_size((cat.get("space") or {}).get("used")) if (cat.get("space") or {}).get("used") else "nothing")
            yes = ui.form("delete-all", "Yes, delete everything", (("confirm", "yes"),), "off")
        else:
            what = yes = ""
        if what:
            out.append(f'<div class="cf" id="confirm"><span>{what}</span> {yes} <a class="bt" href="{esc(page_url(ui.here))}">No</a></div>')
    out.append("</section>")
    return "".join(out)


def ai_chat_html(ui):
    """The chat under the switch: the questions and answers of this process (newest last), the question box, and 'advice now'. The model's text goes
    through advisor.html() (escaped, cleaned, capped); a question is escaped here. The box works when the AI is on and its server is not still starting."""
    esc, snap = html.escape, ui.snap
    chat, ready = snap["chat"], snap["switch"]["on"] and snap["state"][0] != "working"
    out = ['<section class="chat" id="chat"><div class="hs">CHAT · ask the model about this machine (it sees its state now and reads its history; AI, check before acting)</div>']
    for e in chat["history"]:
        res = advisor.html(e["res"]) if e["res"] else advisor.html({"error": e["error"] or "no answer"})
        link = (f' <a href="{esc(page_url(ui.here, prompt=e["id"]) + "#prompt")}">the prompt it was sent</a>' if e.get("prompt_n") and e.get("id") else "")
        out.append(f'<div class="qa"><p class="q"><strong>{"advice" if e["kind"] == "advise" else "you"}:</strong> {esc(e["q"])}</p>{res}'
                   + (f'<p class="d">{link}</p>' if link else "") + "</div>")
    if chat["pending"]:
        out.append(f'<div class="qa"><p class="q"><strong>{"advice" if chat["pending"]["kind"] == "advise" else "you"}:</strong> {esc(chat["pending"]["q"])}</p>'
                   f'<p class="d">{esc(chat["pending"].get("step") or "the model is writing the answer")} (a small model on a slow CPU may need a minute; '
                   'this page reloads by itself)</p></div>')
    if ui.prompt is not None:
        out.append(htmlview.html(ui.prompt))
    if not ui.locked:
        dis = "" if ready and not chat["busy"] else " disabled"
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v, quote=True)}">' for k, v in (("csrf", ui.csrf), ("back", ui.back)))
        out.append(f'<form class="f ask" id="ask" method="post" action="/ai/ask">{hidden}<input class="q" type="text" name="q" maxlength="500" size="60" '
                   f'placeholder="ask: why is the disk filling up?" autocomplete="off"> <button class="bt on" type="submit"{dis}>Ask</button></form>')
        out.append('<div class="adv">advice now: ' + " ".join(ui.form("advise", label, (("days", d),), "", "", not ready or bool(chat["busy"]))
                                                             for d, label in ((1, "last 24 h"), (7, "last 7 days"), (30, "last 30 days")))
                   + (" · " + ui.form("clear", "Clear chat", (), "", "delete the questions, the answers and their prompts (asks first)") if chat["history"] else "")
                   + "</div>")
        if not ready:
            out.append('<p class="d">%s</p>' % ("the model is still starting: the box wakes up when it answers" if snap["state"][0] == "working" else "turn AI on to ask"))
    out.append("</section>")
    return "".join(out)


def ai_manage_html(ui):
    """The small 'manage' area at the bottom: the downloaded files, and the button that deletes all of them (asked about first)."""
    cat = ui.cat
    used = (cat.get("space") or {}).get("used")
    if ui.locked or not (used or any(r["installed"] for r in ui.rows)):
        return ""
    return ('<div class="mg">manage: %s the runtime and every model (%s)</div>'
            % (ui.form("delete-all", "delete everything", (), "del", "asks first", ui.working), html.escape(aisetup.fmt_size(used) if used else "nothing")))


def console_lines(nodes, w):
    """The console's lines of a list of components (the classic pages show them in a <pre>)."""
    return ansi.render(ui.Group(nodes), w)[0]


def console_line(node, w):
    """The first console line of a component."""
    return ansi.render(node, w)[0][0]


def ai_body(data, st, pb, rows, sel, here, cols, host, ui=None):
    """Header, title, the AI switch and the chat, HARDWARE, the models (every one a link; the selected one's details beside or under the table), the
    legend, STATUS and the manage area, as HTML. The lines are the console's (screens.ai_*, drawn by ansi), cleaned by hclean() and escaped by to_html()/html.escape().
    ui: the forms (AiUi); None draws the page without them."""
    esc, cat = html.escape, data["cat"]
    text, code = render.status_pill(pb)
    out = [f'<div class="hd {sgr_class(code)}"><span> {esc(host)} │ AI │ {time.strftime("%H:%M:%S")}</span><span>{esc(text)} </span></div>',
           f'<pre class="ht">{to_html(console_line(screens.ai_title(rows if cat is not None else None, cols), cols))}</pre>']
    if ui is not None:
        out += [ai_control_html(ui), ai_chat_html(ui)]
    if cat is None:  # the catalog could not be read
        return "".join(out) + f'<p class="hn {"r" if data.get("err") else ""}">{esc(hclean(data["msg"]))}</p>'
    ids, windows = {m["id"] for m in rows}, dd(cat.get("hw")).get("os") == "windows"
    out.append(f'<pre class="ht">{to_html(chr(10).join(console_lines(screens.ai_hw_nodes(cat.get("hw"), cols, 0), cols)))}</pre>')
    i = next((j for j, m in enumerate(rows) if m["id"] == sel), None)
    panel = ai_panel_html(rows[i], here, i, windows, ui) if i is not None else ""
    act = ui is not None and not ui.locked
    table = ('<div class="mw" id="models"><table class="mt"><thead><tr><th></th><th>model</th><th class="pm">params</th><th class="r sz">size</th><th class="r">needs</th><th>verdict</th><th>est tok/s</th>'
             '<th class="nn">notes</th>' + ("<th></th>" if act else "") + '</tr></thead><tbody>'
             + "".join(ai_row_html(m, j, sel, here, ui) for j, m in enumerate(rows)) + "</tbody></table></div>"
             if rows else '<div class="nt">the catalog lists no model</div>')
    out.append(f'<div class="hs">MODELS · best first</div><main class="mp{" two" if panel else ""}"><div class="tree">{table}'
               f'<pre class="ht">{to_html(console_line(screens.ai_legend(cols), cols))}</pre></div>{panel}</main>')
    out.append(f'<pre class="ht">{to_html(chr(10).join(console_lines(screens.ai_status_nodes(st, cat, ids, cols, 0), cols)))}</pre>')
    if ui is not None:
        out.append(ai_manage_html(ui))
    return "".join(out)


def health_body(data, pb, days, sel, here, cols, host):
    """Header, title, notes, the findings (every one a link; the selected one's details beside or under them) and the sections, as HTML.
    Everything from the report goes through hclean() (control, format and wide characters out) and html.escape()."""
    esc, R = html.escape, data["report"]
    text, code = render.status_pill(pb)
    out = [f'<div class="hd {sgr_class(code)}"><span> {esc(host)} │ HEALTH │ {time.strftime("%H:%M:%S")}</span><span>{esc(text)} </span></div>']
    fl = screens.health_findings(R)
    title = console_line(screens.health_title(R, days, fl, False), cols)
    if R is None:
        return "".join(out) + f'<pre class="ht">{to_html(title)}</pre><p class="hn {"r" if data.get("err") else ""}">{esc(hclean(data["msg"]))}</p>'
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str)]
    if not (R.get("coverage") or {}).get("since"):
        notes = [x for x in notes if x != "no history yet"]
    out.append(f'<pre class="ht">{to_html(title)}</pre>' + "".join(f'<div class="nt">· {esc(x)}</div>' for x in notes))
    if not hnum((R.get("coverage") or {}).get("hours")):
        return "".join(out) + f'<p class="hn">{esc("no data in this period" if (R.get("coverage") or {}).get("since") else screens.HEALTH_NONE)}</p>'
    rows = []
    for i, f in enumerate(fl):
        level, ttl, txt, _, _ = screens.health_details(f)
        label = screens.LEVEL_PILL[level][0].strip()
        cur = f["id"] == sel
        rows.append(f'<div class="ro{" sel" if cur else ""}" id="f-{i}"><span class="tr"><span class="pl {PILL_CLASS[level]}">{esc(label)}</span> </span>'
                    f'<span class="bd"><a class="lb {LEVEL_CLASS.get(level, "n")}" href="{esc(page_url(here, sel="" if cur else f["id"]) + f"#f-{i}")}">'
                    f'{esc(ttl)}</a>  <span class="d">{esc(txt)}</span>' + (' <a class="dl" href="#details">details ↓</a>' if cur else "") + '</span></div>')
    if not fl:
        rows.append(f'<div class="nt">{esc(screens.health_nothing(R))}</div>')
    panel = ""
    cur = next((f for f in fl if f["id"] == sel), None)
    if cur:
        level, ttl, txt, facts, fix = screens.health_details(cur)
        trs = [f'<tr class="top"><th>finding</th><td class="{LEVEL_CLASS.get(level, "")}">{esc(ttl)}</td></tr>', f"<tr><th>what</th><td>{esc(txt)}</td></tr>"]
        trs += ([f'<tr><th>facts</th><td></td></tr>'] if facts else []) + [f'<tr><th class="in">{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in facts]
        trs.append(f"<tr><th>fix</th><td>{esc(fix)}</td></tr>")
        panel = (f'<aside class="dp" id="details"><div class="dh"><span>DETAILS</span><a href="{esc(page_url(here, sel="") + f"#f-{sel_index(data, sel)}")}">'
                 f'close ✕</a></div><table>{"".join(trs)}</table></aside>')
    out.append(f'<div class="hs">FINDINGS</div><main class="mp{" two" if panel else ""}"><div class="tree">{"".join(rows)}{health_extra_html(R)}</div>{panel}</main>')
    if R.get("coverage"):
        out.append(f'<pre class="ht">{to_html(chr(10).join(screens.health_tables_lines(R, cols, None, time.time())))}</pre>')
    return "".join(out)
