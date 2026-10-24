"""nuc-console web view: the Telegram, HEALTH, EVENTS and AI pages.

The Telegram page (`telegram_html`), the HEALTH page (`health_nodes`, `health_native`), the EVENTS page (`events_nodes`, `events_native`) and the AI page (`ai_nodes`, `ai_native`).
Functions of the data and of the links they are given; the Server of web.py calls them and owns the caches, the engines and the CSRF token."""
import html
import sys
import time
from urllib.parse import urlsplit

import advisor
import events
import htmlview
import render
import screens
import tgweb
import ui
from weburl import page_url

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


def health_advice_node(report):
    """The ADVICE block of the Health page as a ui.Advice (the advisor's CACHED answer, never a generation on a request
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


def events_here(params, here, pause=False):
    """The page's parameters for the EVENTS view of params (events.parse_query's): the period, the kind, the pause and the size/refresh ones of `here`."""
    return dict({"view": "events", "age": params["age"] if params["age"] in ("1h", "7d") else "", "kind": params["kind"], "pause": pause}, **here)


def events_nodes(params, here):
    """The EVENTS screen's components for the web (events.events_nodes): the period, the kinds and the downloads as links, the table of events.
    params: events.parse_query's answer (the page passes the default period); here: the page's parameters, which the links change one at a time.
    The page and the data API draw these."""
    data = render.events_data(params)
    href = lambda age=None, kind=None, format=None: page_url(  # noqa: E731
        here, **({"age": "" if age == "24h" else age} if age is not None else {}), **({"kind": kind} if kind is not None else {}),
        **({"format": format} if format else {}))
    return events.events_nodes(data, params["age"], params["kind"], href)


def events_native(params, here):
    """The EVENTS page of the shell, drawn from components. here: the page's parameters."""
    try:
        return '<div class="hv ev">' + "".join(htmlview.html(n) for n in events_nodes(params, here)) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: events render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the events page could not be drawn (see the service log)</p>'


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
    to /ai/* with the CSRF token and the page to come back to, a locked page has none. ahere: the page's parameters."""
    try:
        return '<div class="scr av">' + "".join(htmlview.html(n) for n in ai_nodes(srv, ahere, sel, confirm, snap, eng, prompt)) + "</div>"
    except Exception as e:  # noqa: BLE001 - a broken state must not take the page down
        print("nuc-console web: ai render error:", repr(e)[:200], file=sys.stderr)  # detail to the journal, not to the page
        return '<p class="sm">the AI screen could not be drawn (see the service log)</p>'

