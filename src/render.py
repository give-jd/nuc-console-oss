#!/usr/bin/env python3
"""nuc-console renderer: full-screen ANSI dashboard on tty1. Stdlib only, no privileges.

On macOS and Windows there is no text console to take over: `--kiosk` writes the same screen as an HTML file and shows it
in a full-screen browser (see kiosk()); host metrics come from hostinfo.py instead of /proc.
"""
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import cards  # same directory: the card registry and the KPI model
import ansi  # same directory: the console's drawing of the components
import cpuinfo  # same directory: the CPU screen's producers
import graph  # same directory: the MAP model
import hostdata  # same directory: what is read from the machine and the collector's files
import display  # same directory: the browser behind --kiosk and --open
import nuc_config
import prefs  # same directory: [ui], the console's theme, density, order and KPIs
import events  # same directory: the EVENTS screen (the timeline of the history)
import hostsview  # same directory: the Hosts screen (key 6): its view-model and keys (peers.py, the cache reader, is loaded when it is used)
import wake  # same directory: wakes a blanked console on a new problem
import problems  # same directory: the problem list, its advice and what was accepted
import procs
import remotedoc  # same directory: a remote host's view documents, read back into components
import ui
import screens  # same directory: the full screens' view-models and the components of each screen
from ansi import ANSI, bar, c, cc, clip, columns, kv, msg, msg_wrap, pad, section, vlen
from ui import dd, dget, fmt_ago, hclean, hnum, human, idict, num, plural, safe
from screens import (CPU_SORT_KEYS, CPU_SORT_SHORT, CPU_SORTS, HEALTH_DAYS, HEALTH_NONE, MAP_IDLE_S, AiView, HealthView, MapView,
                     ai_key, ai_mb, ai_rows, ai_select, ai_sync, cpu_key, cpu_rows, cpu_select, cpu_sync, health_findings, health_key,
                     health_nothing, health_select, health_sync, map_key, map_layout, map_select, map_sync, map_view)
from exposure import expose_apply, exposure_rows, new_ports

try:  # POSIX terminals only: on Windows the keys come from msvcrt
    import termios
    import tty
except ImportError:
    termios = tty = None
LINUX, WINDOWS, MACOS = nuc_config.LINUX, nuc_config.WINDOWS, nuc_config.MACOS
if not LINUX:
    import hostinfo

CFG = nuc_config.current()  # the process's one configuration dict (tests and --demo change it in place)
# names that moved out of this file and that src/web.py still reads as `render.X` (web.py is split on its own branch): `__getattr__` answers
# them from the module they live in, at the time of the read. Delete a line when web.py imports the name from there. Nothing else re-exports.
WEB_COMPAT = {"KIOSK_HINT": "display", "Sampler": "hostdata", "CMD": "problems", "safe_problems": "problems", "status_pill": "problems",
              "telegram_state": "problems"}


def __getattr__(name):
    mod = WEB_COMPAT.get(name)
    if mod is None:
        raise AttributeError(f"module 'render' has no attribute {name!r}")
    return getattr(sys.modules[mod], name)


MODE = os.environ.get("NUC_CONSOLE_MODE") or CFG["mode"]  # overview = a single screen, no rotation


def fleet_on():
    """The Hosts screen (key 6): [hosts] has peers and [features] fleet is not no. Off, nothing of it exists: no key, no help line."""
    return bool(CFG.get("hosts")) and bool(CFG["features"].get("fleet", True))


def on(feature):
    """Section enabled in config.ini (default: yes). ui.CONSOLE_FLEET is not a switch of the file: it is fleet_on()."""
    return fleet_on() if feature == ui.CONSOLE_FLEET else CFG["features"].get(feature, True)

ROTATE_S, REFRESH_S, HOLD_S = CFG["rotate_seconds"], CFG["refresh_seconds"], 60  # REFRESH_S: 1-10 s, config.ini


def reload_config():
    """config.ini read again into CFG, in place: every module holds this very dict and its parts, so they all see what the settings page just
    wrote; the pace of the rotation with it. A file that cannot be read changes nothing (the page wrote one load() read back)."""
    global MODE, ROTATE_S, REFRESH_S
    new = nuc_config.load()
    if new.get("config_error"):
        return False
    for k, v in new.items():
        old = CFG.get(k)
        if isinstance(old, dict) and isinstance(v, dict):
            old.clear()
            old.update(v)
        elif isinstance(old, list) and isinstance(v, list):
            old[:] = v
        else:
            CFG[k] = v
    MODE = os.environ.get("NUC_CONSOLE_MODE") or CFG["mode"]
    ROTATE_S, REFRESH_S = CFG["rotate_seconds"], CFG["refresh_seconds"]
    return True


WIDE = 200  # from this width up: containers in 2 columns, exposure and firewall side by side
PAGES = tuple(n for n, ok in (("System", True), ("Network & firewall", on("exposure") or on("firewall")),
                              ("Boot", on("boot"))) if ok)


def wrap_items(items, w, indent=6, sep="  ·  ", max_lines=None):
    """Compact list over several lines without splitting items; beyond max_lines the last line ends with '… +N'."""
    rows, cur = [], []
    for it in items:
        if cur and indent + vlen(sep.join(cur)) + len(sep) + vlen(it) > w:
            rows.append(cur)
            cur = []
        cur.append(it)
    rows.append(cur)
    rows = [r for r in rows if r]
    if not max_lines or len(rows) <= max_lines:
        return [" " * indent + sep.join(r) for r in rows]
    rows, hidden = rows[:max_lines], sum(len(r) for r in rows[max_lines:])
    while len(rows[-1]) > 1 and indent + vlen(sep.join(rows[-1])) + 8 > w:  # 8 = "  … +NNN"
        rows[-1].pop()
        hidden += 1
    return [" " * indent + sep.join(r) for r in rows[:-1]] + [" " * indent + sep.join(rows[-1]) + c(90, f"  … +{hidden}")]


def unavail_msg(d, key, prefix="unavailable"):
    """Line for a section without data: 'not installed' (info) if the tool is missing, 'unavailable: error' if it is broken."""
    m = cards.unavail(d, key, prefix)
    return msg(m.level, m.text)


def _lines_of(parts, w):
    """The console's lines of a list of components (ansi.render), and whether the drawing cut something."""
    lines, hid = [], False
    for part in parts:
        got, h = ansi.render(part, w)
        lines += got
        hid = hid or h
    return lines, hid


def thermal_lines(th, bw, maxw=None):
    """Temperatures with bar and thresholds (RAM style) + throttling time. Scale and thresholds come from the sensor (cards.thermal_parts)."""
    return _lines_of(cards.thermal_parts(th, bw, maxw), 0)[0]


def page_sistema(s, w, cont=None):
    m, load, up, disk = s.get("mem"), s.get("load"), s.get("uptime"), cards.disk_figures(s.get("disk_root"))  # the Sampler's: nothing is read here
    lines = [f" up {cards.fmt_up(up)}" + (f"   load {cards.fmt_load(load)}" if load != [] else ""), ""]
    bw = max(10, min(60, w - 40))
    ram, swap = cards.ram_figures(m), hostdata.swap_figures(m)
    lines.append(f" RAM   {bar(ram[0] / ram[1], bw)} {human(ram[0])}/{human(ram[1])}  cache {human(m.get('Cached'))}" if ram
                 else f" RAM   {c(33, '?')}")
    if swap:
        lines.append(f" SWAP  {bar(swap[0] / swap[1], bw)} {human(swap[0])}/{human(swap[1])}")
    lines.append(f" DISK  {bar(disk[0] / disk[1], bw)} {human(disk[0])}/{human(disk[1])}  {safe(disk[2])}" if disk
                 else f" DISK  {c(33, '?')}")
    if on("thermal"):
        lines += thermal_lines(s.get("thermal") or {}, bw)
    lines.append("")
    cores = sorted(s["cpu"].items(), key=lambda kv: int(kv[0][3:]))
    cw = 26
    ncol = max(1, (w - 1) // cw)
    cells = [f" {k[3:]:>2} {bar(v, 12)} {v * 100:3.0f}%" for k, v in cores]
    for i in range(0, len(cells), ncol):
        lines.append("".join(pad(x, cw) for x in cells[i:i + ncol]))
    lines.append("")
    return lines + (containers_block(cont if cont is not None else hostdata.load_containers(), w) if on("containers") else [])


def fmt_ports(ports):
    # '*' = all interfaces, 'lo:' = loopback, 'IP:' = bound to a specific address
    return " ".join(f"{'*' if p['s'] == '*' else p['s'] + ':' if p['s'] != 'lo' else 'lo:'}{p['p']}"
                    for p in ports)


def container_box(proj, cts, w):
    inner = w - 2
    title = f"─ {proj[:60]} ({len(cts)}) "
    lines = ["┌" + title + "─" * max(0, inner - len(title)) + "┐"]
    for ct in sorted(cts, key=lambda x: x["name"]):
        st = ct["status"]
        ok = "unhealthy" not in st and "Restarting" not in st and ct["state"] == "running"
        dot = c(32, "●") if ok and "starting" not in st else c(33 if ok else 31, "●")
        row = (f" {dot} {pad(safe(ct['name'])[:36], 37)}{pad(safe(st)[:22], 23)}"
               f"{pad(human(ct['mem']), 7)} {fmt_ports(ct['ports'])}")
        lines.append("│" + pad(clip(row, inner), inner) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def containers_block(data, w, now=None):
    now = now or time.time()
    if data is None:
        return [c(31, " collector not running: no state in " + hostdata.STATE)]
    lines = []
    if data.get("absent"):
        return [msg("info", "docker not installed on this machine")]
    if data.get("error"):
        lines.append(c(31, " docker: " + safe(data["error"])))
    age = now - data.get("ts", 0)
    if age > hostdata.STALE_S:
        lines.append(c(33, f" stale data ({int(age)} s old): collector stopped?"))
    groups = {}
    for ct in data["containers"]:
        groups.setdefault(safe(ct["project"]) or "(standalone)", []).append(ct)
    total_mem = sum(ct["mem"] or 0 for ct in data["containers"])
    lines.append(f" {plural(len(data['containers']), 'container')}   RAM {human(total_mem)}   "
                 f"ports: * = all interfaces, lo: = local only")
    ncol = 2 if w >= WIDE else 1
    cw = (w - 2 * (ncol - 1)) // ncol
    boxes = [container_box(proj, groups[proj], cw) for proj in sorted(groups)]
    if ncol == 1:
        return lines + [ln for b in boxes for ln in b]
    # columns in reading order: the first fills up to half of the total lines
    half, cols, acc = sum(len(b) for b in boxes) / 2, [[], []], 0
    for b in boxes:
        cols[0 if acc + len(b) / 2 < half else 1].extend(b)
        acc += len(b)
    return lines + columns([(cols[0], cw), (cols[1], cw)], w)


NAMEW = 36
NCOL3 = 225  # from this width the single screen uses three columns
BOOT_WINDOW_S = 900  # a container started within 15 min of boot "started with the boot"


def boot_block_avvio(b, up, w):
    return [section("BOOT", w)] + _lines_of(cards.boot_start_parts(b, up, w), w)[0]


def boot_block_lente(b, w, k):
    return _lines_of(cards.boot_slowest_parts(b, cards.Caps(w), k), w)[0]


def boot_block_fallite(b, w):
    lines = [section(cards.boot_labels(b)["failed_title"], w), ""]
    f = b.get("failed")
    if f is None:
        return lines + [unavail_msg(b, "failed")]
    return lines + ([msg("err", safe(u)) for u in f] if f else [msg("ok", "none")])


def boot_block_servizi(b, w, k):
    lines = [section(cards.boot_labels(b)["enabled_title"], w), ""]
    en = b.get("enabled")
    if en is None:
        return lines + [unavail_msg(b, "enabled")]
    act = [e for e in en if e["state"] == "active"]
    off = [e for e in en if e["state"] != "active"]
    lines.append(f"   {len(en)} enabled   {c(32, f'{len(act)} active')}   {len(off)} inactive (often one-shots already done)")
    lines += wrap_items([c(90, e["unit"].replace(".service", "")) for e in act], w, indent=5, max_lines=k)
    fail = [e for e in off if e["state"] == "failed"]
    if fail:
        lines += [msg("err", safe(e["unit"]) + " failed") for e in fail]
    return lines


def boot_block_container(b, w, k, now):
    lines = [section("CONTAINERS & REBOOT", w), ""]
    cs = b.get("containers")
    if cs is None:
        return lines + [unavail_msg(b, "containers")]
    bt = b.get("btime", 0)
    at_boot = [x for x in cs if x["restart"] != "no" and x["started"] - bt <= BOOT_WINDOW_S]
    manual = [x for x in cs if x["restart"] == "no"]
    lines.append(f"   {c(32, str(len(at_boot)))} started at boot (restart policy)   "
                 f"{c(33, str(len(manual))) if manual else 0} started by hand: won't restart on reboot")
    if manual:
        lines += wrap_items([safe(x["name"]) for x in manual], w, indent=5, max_lines=k)
    return lines


def boot_block_journal(b, w, k):
    return _lines_of(cards.boot_journal_parts(b, cards.Caps(w), k, w), w)[0]


def tight(lines):
    """Compact mode: removes the empty line right after each section title."""
    out = []
    for i, ln in enumerate(lines):
        if ln == "" and i and ANSI.sub("", lines[i - 1]).startswith("──"):
            continue
        out.append(ln)
    return out


def page_boot(b, w, body_h, now=None):
    now = now or time.time()
    if b is None:
        return ["", msg("err", "boot collector not running: no state in " + hostdata.BOOT_STATE)]
    head = [msg("warn", f"boot data stale ({int(now - b.get('ts', 0))} s old)"), ""] if now - b.get("ts", 0) > cards.BOOT_STALE_S else []
    up = now - b.get("btime", now)
    wide = w >= WIDE
    # a single page: if it does not fit the height the lists shrink (k), then it splits like the others
    # macOS/Windows: the blocks their collector has no data for are left out, not shown empty
    slow = (lambda bw, k: []) if cards.unsupported(b, "blame") else (lambda bw, k: boot_block_lente(b, bw, k) + [""])
    jour = (lambda bw, k: []) if cards.unsupported(b, "journal") else (lambda bw, k: [""] + boot_block_journal(b, bw, k))
    for k in (10, 8, 6, 4, 3):
        if wide:
            lw, rw = int(w * 0.5), w - int(w * 0.5) - 3
            left = boot_block_avvio(b, up, lw) + [""] + slow(lw, k) + boot_block_fallite(b, lw)
            right = (boot_block_servizi(b, rw, k // 2 + 1) + [""] + boot_block_container(b, rw, k // 2, now)
                     + jour(rw, k))
            lines = head + columns([(left, lw), (right, rw)], w, gap=3)
        else:
            lines = (head + boot_block_avvio(b, up, w) + [""] + slow(w, k)
                     + boot_block_fallite(b, w) + [""] + boot_block_servizi(b, w, k // 3 + 1) + [""]
                     + boot_block_container(b, w, k // 3, now) + jour(w, k))
        if len(lines) <= body_h:
            return lines
    return tight(lines) if len(tight(lines)) <= body_h else lines


def telegram_on():
    """The notifier is on: config.ini's [telegram] enabled, or the web view's Telegram page turned it on (nuc_config.telegram). The demo: config only."""
    return bool(CFG["telegram"]["enabled"] if DEMO else nuc_config.telegram(CFG)["enabled"])


problems.TELEGRAM_ON = telegram_on


def current_problem_records():
    smp = hostdata.Sampler()
    time.sleep(0.5)
    st, sm = snapshot(200), smp.sample()
    return problems.problem_records(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])


def accept_problem(pid, reason="", forget=False, path=None, now=None, records=None):
    """Mark what you see now as known (dimmed, counted under ATTENTION) or forget it. As root:
    sudo nuc-console-accept --problem ID --reason ... The acceptance is tied to the current severity and text: a worse or
    different situation shows up again."""
    path = path or problems.ACCEPTED_PATH
    if pid not in problems.CATALOG:
        print("unknown problem id; known: " + ", ".join(sorted(problems.CATALOG)), file=sys.stderr)
        return 2
    cur = problems.load_accepted(path)
    if forget:
        cur.pop(pid, None)
    else:
        if pid in problems.NOT_ACCEPTABLE:
            print(f"port changes are accepted with the baseline: {problems.ACCEPT_CMD} (no --problem)", file=sys.stderr)
            return 2
        reason = ui.CTRL.sub(" ", reason).strip()
        if not reason:
            print(f"--reason is required: write why this is acceptable (it is shown in `{problems.PROBLEMS_CMD}`)", file=sys.stderr)
            return 2
        recs = current_problem_records() if records is None else records
        rec = next((r for r in recs if r["id"] == pid), None)
        if rec is None:
            print(f"{pid} is not a current problem: nothing to accept", file=sys.stderr)
            return 2
        cur[pid] = {"reason": reason[:200], "ts": now or time.time(), "fp": rec["fingerprint"]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(("forgot " if forget else "accepted ") + pid)
    return 0


def print_problems(argv):
    """`render.py --problems [--json]`: every current anomaly, with why it matters and how to fix it (read-only)."""
    recs = current_problem_records()
    if "--json" in argv:
        print(json.dumps(recs, indent=1, ensure_ascii=False))
        return 0
    if not recs:
        print("no problems")
        return 0
    live, muted = [r for r in recs if not r.get("muted")], [r for r in recs if r.get("muted")]
    print(f"{len(live)} problems ({sum(r['accepted'] for r in live)} accepted" + (f", {len(muted)} muted" if muted else "") + ")\n")
    for r in live:
        print(f"[{r['severity']}] {r['id']}" + ("   (ACCEPTED: " + safe(r["reason"]) + ")" if r["accepted"] else ""))
        print(f"    {safe(r['text'])}")
        if r["why"]:
            print(f"    why: {r['why']}")
        if r["fix"]:
            print(f"    fix: {r['fix']}")
        if r["acceptable"]:
            print(f"    accept if known:  {problems.ACCEPT_CMD} --problem {r['id']} --reason \"...\"")
        else:
            print(f"    port changes are accepted with the baseline: {problems.ACCEPT_CMD}")
    if muted:  # silenced by [alerts] mute: still collected and listed here, off the screen's list, the problems KPI and Telegram
        print(f"muted ({len(muted)}, by {problems.MUTED_HINT}):")
        for r in muted:
            print(f"  [{r['severity']}] {r['id']}: {safe(r['text'])}")
    return 0


def _block(card_id, title, body, w):
    """The console lines of a card's body (components), drawn under its title."""
    return ansi.card_lines(ui.Card(card_id, title, "", "ok", body), w)[0]


def exposure_block(net, cont, w, new=None):
    """The whole exposure matrix (the Network page, and the overview when there is room)."""
    ctx = cards.Ctx(net=net, cont=cont, new=new, cfg=CFG)
    rows = expose_apply(exposure_rows(net, cont), net, cont)
    return _block("exposure", "EXPOSURE", cards.exposure_full(ctx, cards.Caps(w), rows), w)


def firewall_block(net, w, max_rules=None):
    """The whole firewall block: status, configuration, rules (max_rules: how many, None for all of them)."""
    return _block("firewall", "FIREWALL", cards.firewall_full(net, cards.Caps(w), max_rules), w)


def page_rete(net, cont, w, now=None, baseline=False):
    if net is None:
        return ["", msg("err", "network collector not running: no state in " + hostdata.NET_STATE)]
    now = now or time.time()
    pb = problems.safe_problems(net, cont, now, baseline=baseline)
    new = new_ports(net, cont, baseline)
    if not on("exposure"):  # section switched off in config.ini: draw only the other one
        return [section("ATTENTION", w), ""] + [x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] + [""] + firewall_block(net, w)
    if not on("firewall"):
        return [section("ATTENTION", w), ""] + [x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] + [""] + exposure_block(net, cont, w, new)
    head = [section("ATTENTION", w), ""] + ([x for sev, t in pb[:6] for x in msg_wrap("err" if sev >= 2 else "warn", t, w)] if pb
                                              else [msg("ok", "no problems detected")]) + [""]

    if net.get("listeners") is None:  # without the port list "LAN 0" would look like "nothing exposed"
        if cards.is_absent(net, "listeners"):
            return head + ["", msg("warn", "EXPOSURE unavailable: `ss` is missing (iproute2 package)")] + [""] + firewall_block(net, w)
        return head + ["", msg("err", "EXPOSURE unavailable: "
                                 + safe((net.get("errors") or {}).get("listeners", "?")))] \
            + [""] + firewall_block(net, w)
    if w >= WIDE:
        ew = int(w * 0.62)
        return head + columns([(exposure_block(net, cont, ew, new), ew), (firewall_block(net, w - ew - 3), w - ew - 3)],
                              w, gap=3)
    return head + exposure_block(net, cont, w, new) + ["", ""] + firewall_block(net, w)


# The overview's sections are cards (cards.py), registered in the order of the screen; every builder is in cards.py (cards.NATIVE).
for _id in ("attention", "exposure", "webapps", "firewall", "system", "containers", "databases", "boot", "network_traffic", "sessions",
            "tailscale", "docker_disk", "disks"):
    cards.register(_id, *cards.NATIVE[_id][:2], cards.NATIVE[_id][2])


def pack(blocks, ncol, cw, w, body_h, gap):
    """Fills the columns in the given order, left to right, top to bottom: a block goes in the current column, or in the
    next one if it does not fit; earlier columns are never back-filled, so the order on screen is the order requested
    (first-fit used to move sections around whenever a line more or less changed). None if one does not fit anywhere."""
    cols = [[] for _ in range(ncol)]
    ci = 0
    for fn in blocks:
        lines = fn(cw)
        if gap and spacing_on() and len(lines) > 1 and lines[1] != "":  # a little air under each section title
            lines = [lines[0], ""] + lines[1:]
        while ci < ncol:
            col = cols[ci]
            need = len(lines) + (len(gap) if col else 0)
            if len(col) + need <= body_h:
                col.extend((gap if col else []) + lines)
                break
            ci += 1
        else:
            return None
    return columns([(col, cw) for col in cols], w, gap=3) if ncol > 1 else cols[0]


def even_columns(pre, ncol, cw, w):
    """The blocks `pre` (lines each, in a fixed order) in ncol columns of cw, as even as possible: the shortest column height that holds
    every one of them. The scrolling page's layout, and a remote host's overview."""
    blocks = [(lambda c_, lines=lines: lines) for lines in pre]
    lo, hi = max(len(x) + 2 for x in pre), sum(len(x) + 2 for x in pre)
    while lo < hi:
        mid = (lo + hi) // 2
        if pack(blocks, ncol, cw, w, mid, [""]) is None:
            lo = mid + 1
        else:
            hi = mid
    return pack(blocks, ncol, cw, w, hi, [""])


def page_overview(s, cont, net, boot, w, body_h, pb=None, baseline=False, now=None, details=None, scroll=False):
    """Everything on one screen. If it does not fit, details shrink (k = 0..3); at the last level no empty lines.

    `details`: pass a list to receive the detail pages (sections that hid items, shown in full), see slides().
    `scroll`: a browser page that scrolls (body_h is ignored): every section and every item at the richest level, nothing cut,
    in columns as even as possible. A bigger text (fewer columns) then means a longer page, never less content."""
    now = time.time() if now is None else now  # one clock for the frame: the problems and every card's ages and uptimes
    pb = problems.safe_problems(net, cont, now, boot=boot, thermal=s.get("thermal"), baseline=baseline) if pb is None else pb
    new = new_ports(net, cont, baseline)

    ctx = cards.Ctx(s=s, cont=cont, net=net, boot=boot, problems=pb, cfg=CFG, now=now, baseline=baseline, new=new)  # this frame's data and memo
    lifted, trunc = set(), set()  # the sections whose caps are lifted because the free space allows it; the ones that hid items ("… +N more")
    caps_at = lambda width, full=False: cards.Caps(width, full, lifted, trunc)  # noqa: E731 - what the registry asks for

    ncol = 3 if w >= NCOL3 else 2 if w >= WIDE else 1
    cw = (w - 3 * (ncol - 1)) // ncol

    def card_block(n, k, c_, full=False):
        """The lines of card n at level k in a column c_ wide: its title and body drawn by ansi.card_lines,
        remembered for the frame by what changes them; a card that hid items tells the Details pages."""
        caps = caps_at(c_, full)
        card = cards.build(n, ctx, k, caps)
        lines, hid = ctx.once(("lines", n, k) + caps.key(n), lambda: ansi.card_lines(card, c_))
        if hid:
            trunc.add(n)
        return list(lines)

    def make_cand(k, full=False):
        """(card id, block) per section at detail level k: a section switched off in config.ini does not appear. The card comes from the
        registry (cards.build), which remembers it for this frame: the levels and expand() ask for the same card again and again."""
        have = {"attention", "exposure", "firewall", "system", "containers"}
        if k < 4:
            have |= {"databases", "boot", "webapps"}
        if k <= 3 and (w >= WIDE or scroll):  # wide consoles (or a page that scrolls): the detail sections stay at every level
            have |= {"network_traffic", "sessions", "tailscale", "docker_disk", "disks"}
        # the order is fixed (config.ini [dashboard] sections, or [ui] layout / order: card_order), never decided by which block happens to fit where
        def lines_of(n, c_):
            return mark_title(card_block(n, k, c_, full), cards.build(n, ctx, k, caps_at(c_, full)).state)  # the card's state in front of its title, when it is not fine
        return [(n, lambda c_, n=n: lines_of(n, c_)) for n in card_order(ctx, CFG["sections"]) if n in have and cards.enabled(n, CFG)]

    def detail_pages(hid):
        """The sections that hid items ("… +N more"), built in full at the richest level and laid out page by page."""
        blocks = [fn(cw) for n, fn in make_cand(-2, True) if n in hid]
        pages, cols, ci = [], [[] for _ in range(ncol)], 0
        flush = lambda: pages.append(columns([(col, cw) for col in cols], w, gap=3) if ncol > 1 else list(cols[0]))
        for lines in blocks:
            for chunk in [lines[i:i + body_h] for i in range(0, len(lines), body_h)]:
                while True:
                    col = cols[ci]
                    if len(col) + len(chunk) + (1 if col else 0) <= body_h:
                        col.extend(([""] if col else []) + chunk)
                        break
                    if ci + 1 < ncol:
                        ci += 1
                    else:
                        flush()
                        cols, ci = [[] for _ in range(ncol)], 0
        if any(cols):
            flush()
        return pages

    if scroll:
        return even_columns([fn(cw) for _, fn in make_cand(-2, True)], ncol, cw, w)

    # first detail is removed keeping the empty lines between blocks; only at the very end are those removed too
    # from the richest (k=-2, full tables) to the most compact; on very small consoles the last level drops BOOT and DATABASE
    levels = ((-2, True), (-1, True), (0, True), (1, True), (2, True), (3, True), (3, False), (4, False))
    if ui_cfg().get("density") == "wall":  # a wall display is read from afar: it starts at level 0, without the two richest levels (-2, -1)
        levels = levels[2:]
    for i, (k, spaced) in enumerate(levels):
        trunc.clear()  # only what the level that is finally shown hides counts
        blocks = [fn for _, fn in make_cand(k)]
        last = i == len(levels) - 1
        lines = pack(blocks, ncol, cw, w, 10 ** 6 if last else body_h, [""] if spaced else [])  # last level: no limit (the page splits)
        if lines is not None:
            if not last:
                # the caps ("… +N more") are not about space: lift each one if the layout still fits, so a free corner of the
                # screen is used before anything is pushed to the Details pages. Section order = priority. If something stays
                # cut, try once more without the air under the titles: complete content beats spacing.
                def expand(first_lines, gap):
                    ls = first_lines
                    try:
                        for name in [n for n, _ in make_cand(k) if n in set(trunc)]:
                            lifted.add(name)
                            trunc.clear()
                            try_lines = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, gap)
                            if try_lines is None:
                                lifted.discard(name)
                            else:
                                ls = try_lines
                        trunc.clear()
                        ls = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, gap) or ls
                        return ls, set(trunc)
                    finally:
                        lifted.clear()
                        trunc.clear()
                lines, left = expand(lines, [""] if spaced else [])
                if left:  # still cut: complete content beats air. First without the air under the titles, then without the blank lines between sections
                    for tight_spacing, tight_gap in (([(0, [""] if spaced else [])] if spacing_on() else []) + ([(0, [])] if spaced else [])):
                        saved_spacing = CFG["spacing"]
                        CFG["spacing"] = tight_spacing
                        try:
                            trunc.clear()
                            tight = pack([fn for _, fn in make_cand(k)], ncol, cw, w, body_h, tight_gap)
                            tight_lines, tight_left = expand(tight, tight_gap) if tight is not None else (None, left)
                        finally:
                            CFG["spacing"] = saved_spacing
                        if tight_lines is not None and len(tight_left) < len(left):
                            lines, left = tight_lines, tight_left
                        if not left:
                            break
                trunc.clear()
                trunc.update(left)
            if details is not None and CFG["details"] and trunc:
                details.extend(detail_pages(set(trunc)))
            return lines  # the last level has a 10**6 limit: we always return here


def slides(s, cont, net, w, body_h, boot=None, baseline=False, mode=None, scroll=False, cpu_feed=None, cpu_lazy=False):
    """Each page is split into chunks body_h tall: (page name, index, total, lines). scroll: one page, any height, nothing cut.
    cpu_feed: where the CPU slide ([dashboard] cpu_in_rotation) reads; cpu_lazy: leave it empty (see fill_cpu)."""
    det = []
    if scroll:
        return [("Overview", 1, 1, page_overview(s, cont, net, boot, w, body_h, baseline=baseline, scroll=True))]
    if (mode or MODE) == "overview":
        pages = (("Overview", lambda: page_overview(s, cont, net, boot, w, body_h, baseline=baseline, details=det)),)
    else:
        every = {"System": lambda: page_sistema(s, w, cont),
                 "Network & firewall": lambda: page_rete(net, cont, w, baseline=baseline),
                 "Boot": lambda: page_boot(boot, w, body_h)}
        pages = tuple((n, every[n]) for n in PAGES)
    out = []
    for name, fn in pages:
        try:
            lines = fn()
        except Exception as e:  # a broken page must not bring the process down (crash-loop = black tty1)
            lines = [c(31, f" error on page {name}: {safe(repr(e))[:w - 20]}")]
        chunks = [lines[i:i + body_h] for i in range(0, len(lines), body_h)] or [[]]
        out += [(name, i + 1, len(chunks), ch) for i, ch in enumerate(chunks)]
    out += [("Details", i + 1, len(det), pg) for i, pg in enumerate(det)]  # full content of what the overview cut ("… +N more")
    if CFG.get("map_in_rotation") and on("map"):  # a monitor without a keyboard sees the Map too, opened as far as it fits
        try:
            lines = map_slide(cont, net, boot, baseline, w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page Map: {safe(repr(e))[:w - 20]}")]
        out.append(("Map", 1, 1, lines))
    if CFG.get("cpu_in_rotation") and on("cpu"):  # and the CPU: the processor and the top processes, for a monitor without a keyboard
        try:
            lines = [c(90, " CPU: shown when its turn comes")] if cpu_lazy else cpu_slide(cpu_feed or CpuFeed(), w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page CPU: {safe(repr(e))[:w - 20]}")]
        out.append(("CPU", 1, 1, lines))
    if CFG.get("health_in_rotation") and on("health"):  # likewise the Health screen: the findings that fit and the top apps
        try:
            lines = health_slide(w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the pages above
            lines = [c(31, f" error on page Health: {safe(repr(e))[:w - 20]}")]
        out.append(("Health", 1, 1, lines))
    return out


def slide_seconds(slide, n):
    """How long a slide stays: the overview longer when it is followed by detail pages (nobody can press a key on that monitor)."""
    return CFG["overview_seconds"] if slide[0] == "Overview" and n > 1 else ROTATE_S


def pick_slide(sl, t):
    """Index of the slide shown t seconds after the start, cycling through all of them."""
    durs = [slide_seconds(x, len(sl)) for x in sl]
    t %= sum(durs)
    for i, d in enumerate(durs):
        if t < d:
            return i
        t -= d
    return 0


# ---- [ui] on the console: the theme, the density, the cards' order, the tab bar and the KPI line ---------------------------------
# config.ini [ui] (prefs.parse_ui, in CFG["ui"]) is the only source here: a console has no cookie and no URL. Without it nothing below changes
# what the console draws except the header's tab bar, the KPI line (on a tall screen) and the state symbol on a section's title.

UI_THEMES = {"light": "light", "high-contrast": "hc"}  # [ui] theme -> ui.ANSI_THEMES (auto and dark: the default one)
KPI_MIN_ROWS = 30  # the KPI line shows from this many rows up, or whenever [ui] kpis is set (and never with kpis = none)
TAB_SHORT = {"overview": "Ov", "map": "Map", "cpu": "CPU", "health": "Hlth", "ai": "AI"}  # the tab bar when the line is narrow
KPI_TOKEN = {"ok": "ok", "warn": "warn", "err": "err", "down": "err", "unknown": "unknown", "info": "info"}
TITLE_STATES = ("warn", "err", "down", "unknown")  # the states a section's title says besides the colour (ok and info are quiet)
LAYOUT_KEYS = ("layout", "hidden", "order", "preset")  # [ui] keys that take the order of the cards from prefs (else [dashboard] sections)


def ui_cfg():
    return CFG.get("ui") or {}


def theme_name():
    """The ANSI theme the frame is written in: NO_COLOR (set and not empty) is always mono, else [ui] theme."""
    if os.environ.get("NO_COLOR"):
        return "mono"
    return UI_THEMES.get(ui_cfg().get("theme"), "default")


def themed(screen):
    """The frame in the console's theme (the screens are drawn in the default one)."""
    return ansi.retheme(screen, theme_name())


def spacing_on():
    """Empty lines under the section titles: [dashboard] spacing, and never in the compact density."""
    return bool(CFG["spacing"]) and ui_cfg().get("density") != "compact"


def kpi_on(h, page=False):
    """Is there a KPI line on a screen h rows tall? A browser page (page=True) has its own."""
    ks = ui_cfg().get("kpis")
    return not page and ks != [] and (h >= KPI_MIN_ROWS or bool(ks))  # [ui] kpis = none: no line, and its row goes to the body


def body_rows(h, page=False):
    """The rows a screen's body has: the frame less the header, the footer and the KPI line."""
    return h - 2 - (1 if kpi_on(h, page) else 0)


def make_ctx(st, sm, pb, now=None):
    """The cards.Ctx of the KPI line from what a screen has read (snapshot(), the sampler's reading, the header's problems)."""
    ids = prefs.effective(ui_cfg())[0]["kpis"]
    net, cont, base = st["net"], st["cont"], st["baseline"]
    ctx = cards.Ctx(s=sm, cont=cont, net=net, boot=st["boot"], problems=pb, cfg=CFG, now=time.time() if now is None else now, baseline=base,
                    new=new_ports(net, cont, base))
    for field, ask in (("health", lambda: health_data(7)), ("ai", ai_status)):  # only the KPIs that read them cost a read
        if field in ids:
            try:
                setattr(ctx, field, ask())
            except Exception:  # noqa: BLE001 - a source that fails is an unknown KPI, never a broken screen
                pass
    return ctx


KEEP = {}  # what the screens last read (st, sm: map_graph, cpu_problems, health_state, ai_state) and the Ctx made of it (kept_ctx)


def keep(st, sm):
    """Remembers what a screen has read, for the KPI line of its frame."""
    KEEP.update(st=st, sm=sm)


def kept_ctx(pb):
    """The cards.Ctx of the KPI line of a screen that has no Ctx of its own: made of what was last read (keep) and the header's problems
    pb, once for each reading. Nothing read: only the problems are known, the other KPIs are '?'."""
    st, sm = KEEP.get("st"), KEEP.get("sm")
    if st is None:
        return cards.Ctx(problems=pb or [], cfg=CFG)
    hit = KEEP.get("ctx")
    if hit is None or hit[0] is not st or hit[1] is not sm or hit[2] is not pb:
        hit = KEEP["ctx"] = (st, sm, pb, make_ctx(st, sm, pb))
    return hit[3]


def kpi_line(ctx, w):
    """The KPI line: symbol, label and value of each KPI of [ui] (kpis or the preset's), the last ones dropped until it fits w columns."""
    return kpi_line_of(cards.kpis(ctx, prefs.effective(ui_cfg())[0]["kpis"]), w)


def kpi_line_of(kpis, w):
    """kpi_line() of the figures themselves (ui.Kpi): a remote host's come from its document."""
    items = []
    for k in kpis:
        col, val = ui.sgr(KPI_TOKEN[k.state]), k.value + k.unit
        items.append((len(f"{k.symbol} {k.label} {val}"),
                      cc(col, k.symbol) + " " + c(ui.sgr("muted"), k.label) + " " + (val if k.state in ("ok", "info") else cc(col, val))))
    while len(items) > 1 and 1 + sum(n for n, _ in items) + 3 * (len(items) - 1) > w:
        items.pop()
    return clip(" " + "   ".join(t for _, t in items), w)


def card_order(ctx, base):
    """The overview's cards in the order to draw them: [dashboard] sections (`base`) unless [ui] says layout, hidden, preset or order.
    Then it is prefs' layout without the hidden cards, and with `order = severity` the cards with the worst state first: attention
    stays where the layout puts it (first), the cards of one state keep their order. The order is a function of the states, so a card
    moves only when its own state changes (or another one's does): the same states are the same order, frame after frame."""
    ui_ = ui_cfg()
    if not any(k in ui_ for k in LAYOUT_KEYS):
        return list(base)
    ids = [n for n, _w in prefs.visible_cards(prefs.effective(ui_)[0], base)]
    if ui_.get("order") != "severity":
        return ids
    states = ctx.once("card_states", lambda: {n: cards.card_state(n, ctx) for n in ids})
    rank = {n: -ui._SEVERITY[states[n]] for n in ids}
    first = ids[:1] if ids[:1] == ["attention"] else []
    return first + sorted(ids[len(first):], key=lambda n: rank[n])  # sorted() is stable


def mark_title(lines, state):
    """The card's lines with its state in front of the title ('── ✖ EXPOSURE ──'), the rule shortened by as much: width does not change.
    Quiet states (ok, info) and a first line that is not a section title are left alone."""
    if state not in TITLE_STATES or not lines:
        return lines
    strong, accent = ui.sgr("accent_strong"), ui.sgr("accent")
    head, line = f"\x1b[{strong}m ", lines[0]
    at = line.find(head)
    if at < 0:
        return lines
    line = line[:at] + head + "\x1b[0m" + c(ui.sgr(KPI_TOKEN[state]), ui.SYMBOLS[state]) + head + line[at + len(head):]
    fill = f"\x1b[{accent}m" + "─" * 4  # the rule after the title: 2 columns shorter, for the symbol and its blank
    cut = line.find(fill)
    if cut >= 0:
        line = line[:cut + len(fill) - 4] + line[cut + len(fill) - 2:]
    return [line] + lines[1:]


PAUSED = False  # Z: the redraw is paused, the header says so


def tab_bar(cur, shown, short, rev_on, rev_off):
    """The screens' tabs: '[1 Overview]  2 Map  3 CPU  4 Health  5 AI', or '[1·Ov] 2·Map 3·CPU 4·Hlth 5·AI' when short. The current one has
    brackets and is in reverse (rev_on / rev_off: the sequences, which depend on the bar being in reverse itself): never colour alone.
    A screen whose feature is off is left out; the digits are the screens', they do not move."""
    out = []
    for i, (name, feat, title) in enumerate(ui.SCREENS):
        if name != cur and (short is None or feat and not shown(feat)):  # short None: only the current tab, for a very narrow console
            continue
        label = f"{i + 1}·{TAB_SHORT[name]}" if short is not False else f"{i + 1} {title}"
        out.append(f"{rev_on}[{label}]{rev_off}" if name == cur else label)
    return (" " if short is not False else "  ").join(out)


def console_head(name, part, parts, w, text, code, shift, shown):
    """The header line of the console: ' host │ [1 Overview]  2 Map  3 CPU  4 Health  5 AI │ HH:MM:SS … ✖ N PROBLEMS' on the pill's colour.
    The tabs have a short form when the long one leaves no room for the host name. A page of the overview that is not 'Overview'
    (System, Boot, Details...) says which, after the tabs (the footer says which screen of how many)."""
    cur, title = ("", "") if name in ("Events", "Hosts") else next(((n, t) for n, _f, t in ui.SCREENS if t == name), ("overview", "Overview"))  # Events has no tab: none is current
    bar_rev = theme_name() == "mono" or "7" in code.split(";")  # the bar is in reverse already: the current tab is the one that is not
    rev_on, rev_off = ("\x1b[27m", "\x1b[7m") if bar_rev else ("\x1b[7m", "\x1b[27m")
    extra = f" │ {name}" + (f" {part}/{parts}" if parts > 1 else "") if name != title else ""  # the footer counts the screens
    tail = extra + f" │ {time.strftime('%H:%M:%S')}" + (" │ paused" if PAUSED else "") + " "
    host = nuc_config.host_label(CFG, socket.gethostname)
    for short in (False, True, None):
        pre = shift + " "
        mid = " │ " + tab_bar(cur, shown, short, rev_on, rev_off) + tail
        room = w - len(text) - 2 - vlen(pre + mid)
        if room >= min(len(host), 14) or short is None:  # a form is chosen when the host name keeps 14 columns (all of it, if it is shorter)
            break
    if len(host) > room:  # a long host name (macOS: 'xyz-…-ABCD.local') must never push the status off the screen
        host = host[:max(room - 1, 1)] + "…"
    left = pre + host + mid
    return clip(c(code, pad(left, max(vlen(left), w - len(text) - 2)) + text + "  "), w)


def frame(slide, idx, n, w, h, pb=None, keys=True, hint="", page=False, mapkey=None, foot=None, cpukey=None, healthkey=None, aikey=None,
          ctx=None):
    """page=True: a browser page, where all w columns are usable (the Linux console needs w = its width - 1).
    mapkey / cpukey / healthkey / aikey: say that `2` opens the Map, `3` the CPU screen, `4` the Health screen, `5` the AI screen
    (default: when keys are); foot: a footer of its own, else the overview's, made from ui.KEYMAP.
    The console's frame has the tab bar on top and, from KPI_MIN_ROWS rows (or with [ui] kpis), the KPI line of ctx (a cards.Ctx; without
    one, of what the screens last read: kept_ctx); its body is body_rows(h) tall. A browser page keeps the plain header and has no KPI line."""
    name, part, parts, body = slide
    text, code = problems.status_pill(pb or [])
    shift = " " * (int(time.time() // 600) % 3)  # every 10 min shift the header
    flags = {"map": mapkey, "cpu": cpukey, "health": healthkey, "ai": aikey}
    shown = lambda f: bool(keys if flags.get(f) is None else flags[f]) and on(f)  # noqa: E731
    if page:
        tail = f" │ {name}" + (f" {part}/{parts}" if parts > 1 else "") + f" │ {time.strftime('%H:%M:%S')}" + (" │ paused" if PAUSED else "")
        host, room = nuc_config.host_label(CFG, socket.gethostname), w - len(text) - 2 - len(shift) - 1 - len(tail)
        if len(host) > room:  # a long host name (macOS: 'xyz-…-ABCD.local') must never push the status off the screen
            host = host[:max(room - 1, 1)] + "…"
        left = shift + f" {host}" + tail
        head = clip(c(code, pad(left, max(len(left), w - len(text) - 2)) + text + "  "), w)
    else:
        head = console_head(name, part, parts, w, text, code, shift, shown)
    size = f"{w}x{h}" if page else f"{w + 1}x{h}"
    if foot is None:
        foot = c(90, overview_footer(name, idx, n, w, size, keys, hint, shown))
    foot = clip(foot, w)
    rows = [head]
    if kpi_on(h, page):
        rows.append(kpi_line(ctx if ctx is not None else kept_ctx(pb), w))
    rows += [clip(x, w) for x in body[:max(0, h - 1 - len(rows))]]
    rows += [""] * (h - 1 - len(rows)) + [foot]
    return "\x1b[K\r\n".join(rows[:h])  # \x1b[K: clears what is left of the previous frame


def overview_footer(name, idx, n, w, size, keys, hint, shown):
    """The footer of the rotating pages, plain text: where we are, the keys of the overview (ui.KEYMAP), then the console size and the
    hint, which go first when it is narrow. keys=False (a browser page): no keys. shown(feature): is that screen's digit offered?"""
    lead = " single screen   " if n == 1 else f" screen {idx + 1}/{n}   "
    items = []
    if keys or any(shown(f) for f in ("map", "cpu", "health", "ai")):
        items = ui.footer_items("overview", shown, dyn={"back": ("q: quit", None) if nuc_config.PORTABLE else None,  # Esc does nothing here
                                                        "slide-prev": None if n == 1 else ("←→: slide", None)})
    if n > 1 and name == "Details":
        items.append((97, "details: everything the overview cut ('… +N more')", ""))
    items.append((99, f"console {size}", None))
    if hint:
        items.append((98, hint, ""))
    return ui.fit(lead, items, w)


def first_slide_of(sl, page_idx):
    return next((i for i, s in enumerate(sl) if s[0] == PAGES[page_idx]), 0)


# ---- a REMOTE host's screens: its documents (src/remotedoc.py) drawn by the code that draws the local ones ---------------------------------
# Pure functions: the document is already read (a peers.py cache entry), nothing here reads the host, a file or the clock. The frame is the
# local one (header, KPI line, body, footer) with two differences nobody can miss: the header is the remote marker, never the local pill, and a
# banner (remote_banner) says when the data is old, the transport is not safe or the host is not the one that answered first.

REMOTE_CODE = "1;7;35"  # the remote header: bold, reverse video and magenta: no local header is ever drawn so
REMOTE_BANNER = {  # the cache entry's state -> (banner colour, what it says)
    "stale": ("banner_warn", "! STALE: the last reading is old; what follows may no longer be true"),
    "unreachable": ("banner_err", "\u2716 UNREACHABLE: this host does not answer; what follows is its last reading"),
    "unauthorized": ("banner_err", "\u2716 UNAUTHORIZED: this host refuses our token; what follows is its last reading"),
    "untrusted": ("banner_err", "\u2716 UNTRUSTED: the certificate or the identity of this host does not match; do not rely on this"),
    "incompatible": ("banner_warn", "! INCOMPATIBLE: this host speaks a newer web API; update this console"),
    "down": ("banner_err", "\u2716 DOWN: this host reports itself down"),
}


def remote_age(age_s):
    """How old a reading is, short: 12s, 3m, 5h, 2d ('?' when it is not known)."""
    if not isinstance(age_s, (int, float)) or isinstance(age_s, bool) or age_s != age_s or age_s < 0:
        return "?"
    s = int(min(age_s, 10 ** 9))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def remote_header(name, age_s, w=0):
    """The header line while a remote host is shown: '⇄ name (remote 12s)' in reverse video, bold and magenta, w columns wide (0: as long as
    it is). The local header is ' host │ [1 Overview] ... │ HH:MM:SS ... status' on the status pill's colour: the two are never alike."""
    text = f" \u21c4 {remotedoc.clean(str(name)[:40])} (remote {remote_age(age_s)}) "
    return c(REMOTE_CODE, pad(text[:w], w) if w else text)


def remote_banner(entry, w):
    """The banners (lines, each w columns wide) a peers.py cache entry asks for, the worst first: the host is not the one that answered first
    (identity changed), the state (stale, unreachable ...), the transport is plain http outside Tailscale (insecure). Nothing the peer wrote
    is in them. An entry that is not a dict gives the 'untrusted' one."""
    e = entry if isinstance(entry, dict) and isinstance(entry.get("state"), str) else {"state": "untrusted"}
    out = []
    if e.get("identity") not in (None, "ok"):
        out.append(("banner_err", "\u2716 IDENTITY CHANGED: this is not the host that answered first; check the address"))
    if e.get("state") in REMOTE_BANNER:
        out.append(REMOTE_BANNER[e["state"]])
    if e.get("insecure") is True:
        out.append(("banner_warn", "! INSECURE: plain http outside Tailscale; anyone on the path can read or change this"))
    return [clip(c(ui.sgr(tok), pad(" " + words, w)), w) for tok, words in out]


def remote_overview(d, w):
    """The overview of a decoded remote document as lines, nothing cut: every card at the richest level in columns as even as possible, as the
    scrolling page draws the local one (page_overview(scroll=True)). The order is the document's layout when it has one, else its cards'."""
    by_id = {}
    for card in d.cards:
        by_id.setdefault(card.id, card)
    order = [by_id[i] for i, _w in d.layout if i in by_id]
    cards_ = order or d.cards
    ncol = 3 if w >= NCOL3 else 2 if w >= WIDE else 1
    cw = (w - 3 * (ncol - 1)) // ncol
    pre = []
    for card in cards_:
        try:
            pre.append(mark_title(ansi.card_lines(card, cw)[0], card.state))
        except Exception:  # noqa: BLE001 - a card that cannot be drawn is a line, the others stay
            pre.append([msg("warn", "a card of the remote host could not be drawn")])
    return even_columns(pre, ncol, cw, w) if pre else [msg("info", "the remote host sent no cards")]


def remote_body(doc, w):
    """The body of a remote host's screen as lines, nothing cut (the pager's text; remote_view cuts each to w as frame() does). doc: a dict (the JSON of a view document)
    or a remotedoc.Document. Never raises: a document that cannot be read, is too new or is not a remote view is one line saying so."""
    d = doc if isinstance(doc, remotedoc.Document) else remotedoc.decode(doc)
    try:
        if d.ok and d.view == "overview":
            lines = remote_overview(d, w)
        else:
            lines = []
            for n in d.nodes:
                try:
                    lines += ansi.render(n, w)[0]
                except Exception:  # noqa: BLE001
                    lines.append(msg("warn", "an element of the remote host could not be drawn"))
                if len(lines) > remotedoc.MAX_LINES:
                    break
    except Exception:  # noqa: BLE001
        lines = [msg("err", "the remote host's view could not be drawn")]
    if d.cut:
        lines.append(msg("warn", "the remote document is bigger than this console reads: part of it is left out"))
    return lines[:remotedoc.MAX_LINES]


def remote_view(host, doc, w, h, age_s=None, kpis=None, foot=None, banner=(), top=0, body=None, state=None):
    """A remote host's screen as exactly h lines (each w columns wide, no raw escape sequence of the data): the remote header, the banners
    (remote_banner), the KPI line when the frame has one (kpis: ui.Kpi of the host, default the document's own), the body from line `top`
    on (the pager: remote_body has all of it) and the footer (default: where the body is; a function (top, shown, total, room) -> text is
    called with where the body ended up). The local frame's rows otherwise: the same code draws them, so a remote overview looks like a local
    one. body: lines already drawn (remote_body of the same document and width; doc may then be None: a host with nothing to show), state:
    a dict that gets top (clamped), room and total, for a pager."""
    w = max(1, w if isinstance(w, int) and not isinstance(w, bool) else 1)
    h = max(1, h if isinstance(h, int) and not isinstance(h, bool) else 1)
    top = top if isinstance(top, int) and not isinstance(top, bool) else 0
    d = None if doc is None and body is not None else doc if isinstance(doc, remotedoc.Document) else remotedoc.decode(doc)
    body = remote_body(d, w) if body is None else body
    if h == 1:  # no room for more than the marker: a frame without it would be unsafe
        return [remote_header(host, age_s, w)]
    rows = ([remote_header(host, age_s, w)] + [b if vlen(b) <= w else clip(b, w) for b in banner if isinstance(b, str)])[:h - 1]
    figures = (d.kpis if d is not None else []) if kpis is None else kpis
    if kpi_on(h) and figures and len(rows) < h - 1:
        rows.append(kpi_line_of(figures, w))
    room = max(0, h - 1 - len(rows))
    top = max(0, min(top, max(0, len(body) - room)))
    shown = body[top:top + room]
    rows += [clip(x, w) for x in shown]
    if state is not None:
        state.update(top=top, room=room, total=len(body))
    if callable(foot):
        foot = foot(top, len(shown), len(body), room)
    if not isinstance(foot, str):
        foot = c(90, clip(f" remote host: lines {top + 1}-{top + len(shown)} of {len(body)}" if body else " remote host: nothing to show", w))
    rows += [""] * (h - 1 - len(rows)) + [foot if vlen(foot) <= w else clip(foot, w)]
    return rows[:h]


# ---- the Hosts screen (key 6) and the screens of another host (src/hostsview.py; docs/DESIGN.md, "The Hosts screen") ------------------------
# The renderer reads the poller's cache folder (files, checked as foreign input by peers.py) and nothing else: no socket, no thread, no process.
# A remote host's frame is remote_view's: its first line is remote_header, and nothing of this machine is drawn under it.

def hosts_state(now):
    """(cache, the hosts document, the rows worst first, the demo's reader of documents or None, the local problems): one tick of the Hosts screens.
    The cache is peers.read_cache(views=False); in the demo with [hosts] set and no poller's cache, demo.fleet makes one."""
    import hostsview
    import peers
    folder = nuc_config.peers_cache_dir()
    cache, reader = peers.read_cache(folder, now=now, views=False), None
    if DEMO and not cache["peers"]:
        import demo
        cache, docs = demo.fleet(CFG.get("hosts") or [], now)
        reader = lambda name: docs.get(name, {})  # noqa: E731
    doc, rows = hostsview.build(CFG.get("hosts") or [], folder, now, cache, nuc_config.host_label(CFG, socket.gethostname))
    st = snapshot(0)
    sm = host_sample(None) if DEMO else host_sample(hostdata.Sampler())
    keep(st, sm)
    if DEMO:
        demo_defaults()
    pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])
    return cache, doc, rows, reader, pb


def hosts_foot(hs, lead, w, skip=()):
    return clip(c(90 if not hs.note else 33, ui.footer(hs.scope, lead, w, on, skip=skip)), w)


def hosts_list_screen(hs, state, w, h):
    """The Hosts list as one frame: the local header (it is this machine's list), the rows, the keys."""
    import hostsview
    cache, doc, rows, _reader, pb = state
    body = hostsview.list_lines(doc, rows, hs, cache, w, body_rows(h))
    return frame(("Hosts", 1, 1, body), 0, 1, w, h, pb, foot=hosts_foot(hs, f" {hostsview.list_footer_pos(hs, len(rows))}   ", w))


def remote_screen(hs, state, w, h, now):
    """The screen of the host hs.name as exactly h lines: remote_header first, the banners its cache entry asks for, then its document of hs.view
    (the pager's window of it), or a plain line saying why there is none. Moves hs.view, hs.scroll and hs.room."""
    import hostsview
    cache, _doc, _rows, reader, _pb = state
    name = hs.name
    entry = hostsview.entry_of(cache, name)
    hostsview.read_remote(hs, nuc_config.peers_cache_dir(), entry, reader, ("demo", name) if reader else None)
    have = hostsview.views_of(entry)
    if not hs.view:
        hs.view = hostsview.start_view(entry)
    d = hostsview.document(hs, hs.view)
    banner = remote_banner(entry, w) if entry else []
    last = entry.get("last_ok") if entry else None
    age = now - (last if isinstance(last, (int, float)) and not isinstance(last, bool) else entry.get("ts", now)) if entry else None
    if d is None:
        words = (hostsview.why_not(entry, name, hs.view) if hs.view not in have
                 else "the cached document could not be read (the poller will write it again)")
        lines = [msg("info", words)] + ([msg("info", "give that peer's [hosts] line scope=views, with a token the peer accepts for it (docs/WEB.md)")]
                                         if entry and entry.get("scope") == "summary" and not have else [])
    else:
        key = (hs.view, w, id(d))
        if hs.body is None or hs.body[0] != key:
            hs.body = (key, remote_body(d, w))
        lines = hs.body[1]
    st = {}

    def foot(top, shown, total, room):
        where = f" {hostsview.VIEW_WORD.get(hs.view, hs.view)} · lines {top + 1}-{top + shown} of {total}   " if d is not None and total else " "
        text = ui.footer("remote", where if not hs.note else f" {hs.note}   ", w, on)
        return clip(c(33 if hs.note else 90, text), w)
    rows = remote_view(name, d, w, h, age_s=age, banner=banner, top=hs.scroll, body=lines, state=st, foot=foot)
    hs.scroll, hs.room = st.get("top", 0), max(1, st.get("room", 1))
    return rows


def hosts_screen(hs, state, w, h, now):
    """The frame of the Hosts screens: the list (mode 'list') or one remote host's view (mode 'remote')."""
    if hs.mode == "remote":
        return "\x1b[K\r\n".join(remote_screen(hs, state, w, h, now))
    return hosts_list_screen(hs, state, w, h)


def hosts_once(argv, w, h):
    """`--once --view hosts` (the list; --select NAME puts the cursor on a host) and `--once --host NAME [--view overview|map|cpu|health|events]
    [--scroll N]` (one remote host's screen), as the console draws them (tests, screenshots). None: nothing to draw (it says why)."""
    import hostsview
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    if not fleet_on():
        print("the Hosts screen is off: [hosts] is empty or [features] fleet = no in config.ini", file=sys.stderr)
        return None
    now = time.time()
    hs = hostsview.HostsView(now=now)
    host, view = opt("--host"), opt("--view")
    if host:
        if host not in [p["name"] for p in CFG.get("hosts") or []]:
            print("--host: not a host of [hosts]", file=sys.stderr)
            return None
        if view and view not in hostsview.VIEW_ORDER:
            print("--view: a remote host has overview, map, cpu, health and events", file=sys.stderr)
            return None
        hs.mode, hs.name, hs.view = "remote", host, view
        hs.scroll = int(opt("--scroll")) if opt("--scroll").isdigit() else 0
    elif opt("--select"):
        hs.cur = opt("--select")
    state = hosts_state(now)
    return hosts_screen(hs, state, w, h, now)


DEMO = False  # --demo: synthetic data (src/demo.py) instead of the real state
DEMO_OS = None  # --demo-os windows|darwin: the demo as the macOS/Windows collector would write it


def snapshot(w):
    """State read from disk, as the main loop sees it."""
    if DEMO:
        import demo
        cont, net, boot, base = demo.snapshot(os_name=DEMO_OS)
        return dict(cont=cont, net=net, boot=boot, baseline=base)
    return dict(cont=hostdata.load_containers(), net=hostdata.load_json(hostdata.NET_STATE), boot=hostdata.load_json(hostdata.BOOT_STATE), baseline=problems.load_baseline())


def host_sample(smp):
    """The Sampler's reading for a screen. Under --demo it is the demo machine's (demo.sampler_data), whatever sampler is given (the web view
    always has a real one) and without calling it: nothing of the machine running the demo is read or shown. Without a sampler: no figures
    ({"thermal": {}}: the screens that only judge the header's problems)."""
    if DEMO:
        import demo
        return demo.sampler_data(DEMO_OS)
    return smp.sample() if smp else {"thermal": {}}


def demo_defaults():
    """--demo: the host name and the [webapps] the screenshots show (one up, one expected-but-down)."""
    socket.gethostname = lambda: "demo-host"
    if not CFG["webapps"]:
        CFG["webapps"] = {"shop-web": [8080], "admin-console": [9443]}
    if not CFG["expose"]:  # one service within its reach, two beyond it (the Funnel's backend is the process 'node': a unit on Linux only)
        CFG["expose"] = {"shop-web": "LAN", "shop-db": "LOCALE", ("n8n" if DEMO_OS in (None, "linux") else "node"): "TAILNET"}


def map_graph(smp=None):
    """(MAP graph, header problems) of the current state: shared by the console's Map screen and the web view's map page."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
    if DEMO:
        demo_defaults()
    G = graph.build(st["cont"], st["net"], st["boot"], CFG["webapps"], baseline=st["baseline"], expose=CFG["expose"])
    return G, problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


# ---- MAP screen: graph.py's tree drawn on the console, moved through with the keyboard ------------------------------------
# The screen's model (MapView, its keys, the tree, the details, the layout) is in screens.py as components that ansi.render draws; what is
# here is what needs this process (the footer's enabled keys, the frame, the producers).


def map_footer(mv, n, w, truncated=False):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    hide, only = mv.details, mv.st.only
    pos = f"{mv.idx + 1}/{n}{'+' if truncated else ''}" if n else "0/0"
    dyn = {"details": ("Enter: " + ("hide details" if hide else "details"), "Enter: " + ("hide" if hide else "details")),
           "problems": ("p: " + ("all paths" if only else "problems only"), "p: " + ("all" if only else "problems"))}
    return clip(c(90, ui.footer("map", f" row {pos}   ", w, on, dyn)), w)


def map_screen(G, pb, mv, w, h):
    """(the interactive Map as one frame: header, body, key help; the rows it shows): the live loop and --once."""
    rs = graph.rows(G, mv.st)
    map_sync(mv, rs)
    body, mv.top = map_view(G, rs, w, body_rows(h), mv.cur, mv.details, mv.top, mv.st.only)
    return frame(("Map", 1, 1, body), 0, 1, w, h, pb, foot=map_footer(mv, len(rs), w, getattr(rs, "truncated", False))), rs


def map_slide(cont, net, boot, baseline, w, body_h):
    """The Map among the rotating pages ([dashboard] map_in_rotation): no cursor, opened level by level while it fits."""
    G = graph.build(cont, net, boot, CFG["webapps"], baseline=baseline, expose=CFG["expose"])
    return map_view(G, graph.rows(G, graph.State(open=graph.fit_open(G, map_layout(G, w, body_h)[1]))), w, body_h)[0]


def map_once(argv, w, h):
    """`--once --view map`: the Map screen as the console draws it (tests, README screenshots).
    --expand all: every branch open (e); --expand fit: opened level by level while it fits the screen, as the rotation
    slide does; --expand N: the same for at most N tree rows; none: only the roots open, as when `m` opens it.
    --select TEXT: the cursor on the first row whose name contains TEXT (any case), opening the branches above it;
    --details: the details pane of the selected row (Enter); --only: problems only (p)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    G, pb = map_graph(None if DEMO else hostdata.Sampler())
    mv = MapView()
    mv.st.only, mv.details = "--only" in argv, "--details" in argv
    exp = opt("--expand")
    if exp == "all":
        mv.st.expand_all()
    elif exp == "fit" or exp.isdigit():
        mv.st.open = graph.fit_open(G, int(exp) if exp.isdigit() else map_layout(G, w, body_rows(h), mv.details)[1])
    if opt("--select"):
        map_select(G, mv, opt("--select"))
    return map_screen(G, pb, mv, w, h)[0]


# ---- CPU screen: the processor, every logical CPU, the temperatures and the processes (htop, plus temperatures) ------------
# Data: cpuinfo.CpuSampler and procs.ProcSampler (the contract is in docs/DESIGN.md), plus sensors.json on macOS/Windows.
# Everything a producer hands over is data: numbers go through num(), text through safe(), a missing value is drawn as "?".

SENSORS = os.environ.get("NUC_CONSOLE_SENSORS", os.path.join(nuc_config.RUN_DIR, "sensors.json"))  # written by the macOS/Windows collector
SENSORS_STALE_S = 60  # an older sensors.json is not "now": it is ignored, and the screen says so
CPU_IDLE_S = MAP_IDLE_S  # left alone this long, the CPU screen gives the monitor back to the rotation


def cpu_os():
    """Whose machine the CPU screen describes: the demo's OS under --demo, else this one."""
    if DEMO:
        return DEMO_OS if DEMO_OS in ("windows", "darwin") else "linux"
    return "windows" if WINDOWS else "darwin" if MACOS else "linux"


def cpu_merge(cpu, sens, now):
    """(cpu, extra): the sampler's data with the collector's temperatures filled in where the sampler had none (macOS/Windows; on
    Linux the sampler reads sysfs itself), and extra = {pressure, clusters, notes: [(level, text)]} from sensors.json. A sensors.json
    older than SENSORS_STALE_S is ignored with a note: its numbers are not the temperatures of now."""
    cpu = dict(cpu)
    temps = dict(dd(cpu.get("temps")))
    temps["cores"] = {k: v for k, v in idict(temps.get("cores")).items() if num(v) is not None}
    extra = {"pressure": None, "clusters": [], "notes": []}
    if cpu_os() != "linux":
        ts = num(dget(sens, "ts"))
        if not isinstance(sens, dict):
            extra["notes"].append(("warn", "temperatures: ? (no sensors.json: the collector is not running, or [features] cpu = no)"))
        elif ts is None or now - ts > SENSORS_STALE_S:
            age = "" if ts is None else f" ({fmt_ago(now - ts)} old)"
            extra["notes"].append(("warn", f"temperatures: ? (sensors.json is stale{age}: the collector stopped writing it)"))
        else:
            sc = dd(sens.get("cpu"))
            if num(temps.get("package")) is None and num(sc.get("package")) is not None:
                temps["package"] = float(sc["package"])
            for k, v in idict(sc.get("cores")).items():
                if num(v) is not None and k not in temps["cores"]:
                    temps["cores"][k] = float(v)
            if not temps.get("sensors"):
                temps["sensors"] = [{"label": x.get("label"), "c": num(x.get("c")), "high": None, "crit": None}
                                    for x in sc.get("sensors") or [] if isinstance(x, dict) and num(x.get("c")) is not None]
            if not temps.get("source") and isinstance(sc.get("source"), str):
                temps["source"] = sc["source"]
            if isinstance(sc.get("pressure"), str):
                extra["pressure"] = sc["pressure"]
            extra["clusters"] = [x for x in sc.get("clusters") or [] if isinstance(x, dict)]
            errors = dd(sens.get("errors"))
            extra["notes"] += [("info", f"{k}: {v}") for k, v in list(errors.items())[:2]]
    cpu["temps"] = temps
    return cpu, extra


def cpu_data(feed, settle=0.0):
    """One reading of everything the screen shows: {cpu, procs, extra, at}. A sampler that raises leaves its part empty and a note."""
    now, notes = time.time(), []
    if DEMO:
        import demo
        raw_cpu, raw_pr, sens = demo.cpu_sample(DEMO_OS, now), demo.proc_sample(DEMO_OS, now), demo.sensors(DEMO_OS, now)
    else:
        if feed.cs is None:  # created on the first read: process sampling costs CPU, only a screen that is shown pays for it
            for attr, what, make in (("cs", "cpu", lambda: cpuinfo.CpuSampler()), ("ps", "process", lambda: procs.ProcSampler())):
                try:
                    setattr(feed, attr, make())
                except Exception as e:  # noqa: BLE001 - a broken producer leaves its half of the screen empty
                    setattr(feed, attr, False)
                    notes.append(safe(f"{what} sampler: {type(e).__name__}"))
            if settle and feed.ps:  # a first reading, so that the next one has a CPU% to show
                try:
                    feed.ps.sample()
                    time.sleep(settle)
                except Exception:  # noqa: BLE001
                    pass

        def read(src, what):
            try:
                r = src.sample() if src else None
            except Exception as e:  # noqa: BLE001
                notes.append(safe(f"{what} sampler failed: {type(e).__name__}"))
                r = None
            return r if isinstance(r, dict) else {}
        raw_cpu, raw_pr = read(feed.cs, "cpu"), read(feed.ps, "process")
        sens = hostdata.load_json(SENSORS) if cpu_os() != "linux" else None
    cpu, extra = cpu_merge(raw_cpu, sens, now)
    pl = [p for p in (raw_pr.get("procs") if isinstance(raw_pr.get("procs"), list) else []) if isinstance(p, dict) and isinstance(p.get("pid"), int) and not isinstance(p["pid"], bool)]
    total = dd(raw_pr.get("total"))
    for x in notes + list(cpu.get("notes") or []) + list(raw_pr.get("notes") or []):  # what the producers could not read, once each
        if isinstance(x, str) and ("info", safe(x)) not in extra["notes"]:
            extra["notes"].append(("info", safe(x)))
    return {"cpu": cpu, "procs": {"procs": pl, "total": total}, "extra": extra, "at": now}


class CpuFeed(object):
    """The CPU screen's data source: one CpuSampler and one ProcSampler, created at the first read (process sampling costs CPU: only a
    screen that is shown owns one). read() hands the last reading back while it is younger than max_age (the web page: one sampling
    per refresh interval, whoever asks). settle: seconds between a first process reading and the one shown, so that a one-off
    screen or a page already has a CPU% (the console instead shows "measuring" for a second)."""

    def __init__(self, settle=0.0, max_age=0.0):
        self.cs = self.ps = None
        self.at, self.data, self.demo, self.settle, self.max_age = 0.0, None, None, settle, max_age

    def read(self):
        now = time.time()
        if self.data is None or self.demo != (DEMO, DEMO_OS) or not 0 <= now - self.at < self.max_age:
            self.demo = (DEMO, DEMO_OS)
            self.data, self.at = cpu_data(self, self.settle), now
        return self.data


class CpuView(screens.CpuView):
    """The interactive CPU screen's state (screens.CpuView, with the data source of this process)."""

    def __init__(self, now=None):
        screens.CpuView.__init__(self, CpuFeed(), now or time.time())


# -- what is known about each logical CPU

_TOPO = {}


def cpu_topology(ids):
    """{logical CPU: physical core id} from sysfs (Linux): the key the sampler's per-core temperatures are filed under."""
    if DEMO or cpu_os() != "linux" or not LINUX:
        return {}
    key = tuple(ids)
    if key not in _TOPO:
        got = {i: hostdata.read_file(f"/sys/devices/system/cpu/cpu{i}/topology/core_id") for i in ids}
        _TOPO[key] = {i: int(v) for i, v in got.items() if v and v.lstrip("-").isdigit()}
    return _TOPO[key]


def cpu_ctx(full=False):
    """What the CPU screen needs of this process (screens.CpuCtx): whose machine it describes, the topology, whether nothing is cut (full),
    the clock."""
    return screens.CpuCtx(cpu_os(), cpu_topology, full, time)


def cpu_view(d, w, h, sort="cpu", cur=None, details=False, top=0):
    """(the CPU screen's body: at most h lines, none wider than w; the first process shown; the processes in order; how many are
    visible; [(line, pid)] of the process rows). cur: the pid under the cursor (None: no cursor, the table is cut at the bottom);
    details: the cursor's process in a pane (beside the table from CPU_PANE_W columns on, below it otherwise). screens.cpu_view makes
    the components; this draws them."""
    sc = screens.cpu_view(d, cpu_ctx(), w, h, sort, cur, details, top)
    lines = [x for node in sc.nodes for x in ansi.render(node, w)[0]]
    return [clip(x, w) for x in lines[:h]], sc.top, sc.rows, sc.vis, sc.pids


def cpu_footer(cv, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    sorts = "  ".join(f"{k.upper()} {CPU_SORT_SHORT[v]}" for k, v in CPU_SORT_KEYS.items())
    dyn = {"sort": ("sort: " + sorts, sorts), "details": ("Enter: " + ("hide details" if cv.details else "details"), "Enter: details")}
    text = ui.footer("cpu", f" row {cv.idx + 1}/{n}   " if n else " row 0/0   ", w, on, dyn)
    here = f"{next(k for k, v in CPU_SORT_KEYS.items() if v == cv.sort).upper()} {CPU_SORT_SHORT[cv.sort]}"
    return clip(c(90, text.replace(here, "\x1b[0m" + c("1;7", here) + "\x1b[90m", 1)), w)  # the sort in use, reversed


def cpu_screen(d, pb, cv, w, h):
    """(the interactive CPU screen as one frame: header, body, key help; the processes in order): the live loop and --once."""
    rows = cpu_rows(d["procs"]["procs"], cv.sort)
    cpu_sync(cv, rows)
    body, cv.top, _, vis, _ = cpu_view(d, w, body_rows(h), cv.sort, cv.cur, cv.details, cv.top)
    cv.page = max(1, vis - 1)
    return frame(("CPU", 1, 1, body), 0, 1, w, h, pb, foot=cpu_footer(cv, len(rows), w)), rows


def cpu_slide(feed, w, body_h):
    """The CPU among the rotating pages ([dashboard] cpu_in_rotation): header, CPUs, temperatures, the top processes that fit."""
    return cpu_view(feed.read(), w, body_h)[0]


def fill_cpu(sl, idx, w, body_h, feed):
    """The CPU slide is built empty by slides(lazy): its samplers cost CPU, so only the slide on screen gets its data."""
    if sl[idx][0] == "CPU":
        try:
            lines = cpu_slide(feed, w, body_h)
        except Exception as e:  # noqa: BLE001 - same rule as the other pages
            lines = [c(31, f" error on page CPU: {safe(repr(e))[:w - 20]}")]
        sl[idx] = ("CPU", 1, 1, lines)


def cpu_problems(smp=None):
    """The header's problems of the current state: the CPU screen has no graph of its own to take them from."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
    if DEMO:
        demo_defaults()
    return problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def cpu_once(argv, w, h):
    """`--once --view cpu`: the CPU screen as the console draws it (tests, screenshots). --sort cpu|mem|time|pid|user (anything else:
    cpu); --select NAME: the cursor on the first process whose name contains NAME (any case) or whose pid it is; --details: the
    details pane of the process under the cursor (Enter)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    d = CpuFeed(settle=0.5).read()
    cv = CpuView()
    cv.sort = opt("--sort") if opt("--sort") in CPU_SORTS else "cpu"
    cv.details = "--details" in argv
    if opt("--select"):
        cpu_select(cpu_rows(d["procs"]["procs"], cv.sort), cv, opt("--select"))
    return cpu_screen(d, cpu_problems(None if DEMO else hostdata.Sampler()), cv, w, h)[0]


def render_screen(smp, w, h, mode=None, n=0, at=None, keys=True, page=False, scroll=False, cpu_feed=None):
    """One frame as an ANSI string and the number of slides: used by --once and by the web view (web.py).
    at = a time: the slide shown at that moment of the rotation (overview, then Details pages), as on the console."""
    st, sm = snapshot(w), host_sample(smp)
    if DEMO:
        demo_defaults()
    pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    ctx = make_ctx(st, sm, pb) if kpi_on(h, page) else None  # the KPI line (a console; a browser page has its own)
    sl = slides(sm, st["cont"], st["net"], w, body_rows(h, page), st["boot"], st["baseline"], mode=mode, scroll=scroll, cpu_lazy=True)
    if scroll:  # the page is as tall as its content (header + body + footer)
        h = len(sl[0][3]) + 2
    n = pick_slide(sl, at) if at is not None else n
    fill_cpu(sl, n % len(sl), w, body_rows(h, page), cpu_feed or CpuFeed(settle=0.5))  # only the slide shown reads the processes
    return frame(sl[n % len(sl)], n % len(sl), len(sl), w, h, pb, keys=keys, page=page, ctx=ctx), len(sl)


def render_screens(smp, w, h, mode=None, keys=True, page=False, cpu_feed=None):
    """Every slide (overview + detail pages) as ANSI frames: the web "full details" view."""
    st, sm = snapshot(w), host_sample(smp)
    if DEMO:
        demo_defaults()
    sl = slides(sm, st["cont"], st["net"], w, body_rows(h, page), st["boot"], st["baseline"], mode=mode, cpu_feed=cpu_feed or CpuFeed(settle=0.5))
    pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
    ctx = make_ctx(st, sm, pb) if kpi_on(h, page) else None
    return [frame(x, i, len(sl), w, h, pb, keys=keys, page=page, ctx=ctx) for i, x in enumerate(sl)]


def utf8_stdout():
    """Windows pipes default to the ANSI code page: the bars and symbols must not crash `--once > file` or `| tool`."""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if sys.stdout is not None and enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def once(argv):
    global DEMO, DEMO_OS, DEMO_HEALTH
    DEMO = "--demo" in argv
    DEMO_OS = argv[argv.index("--demo-os") + 1] if "--demo-os" in argv[:-1] else None
    DEMO_HEALTH = argv[argv.index("--demo-health") + 1] if "--demo-health" in argv[:-1] else ""
    arg = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d
    w, h, n = arg("--cols", 120) - 1, arg("--rows", 33), arg("--slide", 0)
    view = argv[argv.index("--view") + 1] if "--view" in argv[:-1] else ""
    if view == "start":  # the screen the console opens at: [ui] start_view (the overview when it is not set)
        view = ui_cfg().get("start_view", "overview")
        view = "" if view == "overview" else view
    if view == "hosts" or "--host" in argv:  # the Hosts list, or one remote host's screen (see hosts_once)
        out = hosts_once(argv, w, h)
        if out is None:
            return 2
    elif view == "map":  # the Map screen (see map_once for its options)
        if not on("map"):
            print("the map is off: [features] map = no in config.ini", file=sys.stderr)
            return 2
        out = map_once(argv, w, h)
    elif view == "cpu":  # the CPU screen (see cpu_once)
        if not on("cpu"):
            print("the CPU screen is off: [features] cpu = no in config.ini", file=sys.stderr)
            return 2
        out = cpu_once(argv, w, h)
    elif view == "health":  # the Health screen (see health_once for its options)
        if not on("health"):
            print("the health screen is off: [features] health = no in config.ini", file=sys.stderr)
            return 2
        out = health_once(argv, w, h)
        if out is None:
            return 2
    elif view == "events":  # the EVENTS screen (see events_once for its options)
        out = events_once(argv, w, h)
        if out is None:
            return 2
    elif view == "ai":  # the AI screen (see ai_once for its options)
        if not on("ai"):
            print("the AI screen is off: [features] ai = no in config.ini", file=sys.stderr)
            return 2
        out = ai_once(argv, w, h)
    else:
        smp = None if DEMO else hostdata.Sampler()  # the demo reads nothing from this machine (render_screen)
        if smp:
            smp.sample()  # starts the background reads (sessions, disks): they have the half second below to arrive
            time.sleep(0.5)
        out, _ = render_screen(smp, w, h, n=n)
    print(themed(out) if "--color" in argv else ANSI.sub("", out))  # --color keeps the ANSI codes (used by tools/ansi2svg.py)


# ---- HEALTH screen: health.py's report (the history over days and weeks) on the console, moved through with the keyboard ------

# The view-model of the screen is in screens.py; this file reads the report and runs the loop.
HEALTH_TTL = 60          # the report is computed at most this often per period, whatever the number of keys or requests
HEALTH_IDLE_S = 600      # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
DEMO_HEALTH = ""         # --demo-health little|none: the demo with 5 hours of history, or none (demo.HEALTH_VARIANTS)
_HEALTH, _HEALTH_LOCK = {}, threading.Lock()


def health_series(conn, rep):
    """The top_cpu / top_mem rows get "series": CPU seconds / mean RSS of the app per hour (24 h) or per day (longer), oldest first, None
    where nothing was recorded. health.report() has no series: the screen asks app_hour (read-only) itself, once per report. Rows that
    already carry one (the demo, a later report()) are left alone. A history that cannot be read costs the sparklines, nothing else."""
    try:
        per_hour, days = rep["period"]["days"] <= 1, int(rep["period"]["days"])
        rows = [x for k in ("top_cpu", "top_mem") for x in rep.get(k) or [] if isinstance(x, dict) and "series" not in x]
        names = sorted({x["app"] for x in rows})
        if not names:
            return
        h1 = int(rep["period"]["to"] // 3600)
        n = 24 if per_hour else days
        unit = 1 if per_hour else 24
        first = (h1 if per_hour else h1 // 24) - n + 1  # the bucket of the oldest value
        cpu, mem = {}, {}
        for app, b, secs, rss in conn.execute(
                "SELECT app, hour / ?, SUM(cpu_s), AVG(rss_avg) FROM app_hour WHERE hour >= ? AND hour <= ? AND app IN (%s) GROUP BY app, hour / ?"
                % ",".join("?" * len(names)), (unit, first * unit, h1) + tuple(names) + (unit,)):
            if 0 <= b - first < n:
                cpu.setdefault(app, [None] * n)[b - first], mem.setdefault(app, [None] * n)[b - first] = secs, rss
        for x in rep.get("top_cpu") or []:
            if isinstance(x, dict) and "series" not in x:
                x["series"] = cpu.get(x["app"], [])
        for x in rep.get("top_mem") or []:
            if isinstance(x, dict) and "series" not in x:
                x["series"] = [None if v is None else v / 2 ** 20 for v in mem.get(x["app"], [])]  # MB
    except Exception:  # noqa: BLE001 - sparklines are a nicety
        pass


def health_build(days, now):
    """{"report": dict | None, "msg": why there is none, "err": bool, "at": now}: the demo, or the history opened read-only."""
    if DEMO:
        import demo
        return {"report": demo.health_report(DEMO_OS, days, now, variant=DEMO_HEALTH), "msg": "", "err": False, "at": now}
    try:
        import health  # a missing or broken module costs this screen, never the dashboard
        import history
        conn = history.open_ro()
        if conn is None:
            return {"report": None, "msg": HEALTH_NONE, "err": False, "at": now}
        try:
            rep = health.report(conn, days=days)
            health_series(conn, rep)
        finally:
            conn.close()
        return {"report": rep, "msg": "", "err": False, "at": now}
    except Exception as e:  # noqa: BLE001 - say so, and keep the dashboard
        return {"report": None, "msg": "the history could not be read: " + safe(repr(e))[:100], "err": True, "at": now}


def health_data(days):
    """health_build() of the last `days` days (1, 7 or 30), at most once per HEALTH_TTL per period: the console and every web request
    share it, so a key or a page never reads the history. A failure is kept for the same time (no retry on every key)."""
    days = days if days in HEALTH_DAYS else 7
    src = (bool(DEMO), DEMO_OS, DEMO_HEALTH)  # where it was read: the demo's report is never the history's, nor one demo machine's another's
    with _HEALTH_LOCK:
        now = time.time()
        hit = _HEALTH.get(days)
        if hit is None or hit.get("src", src) != src or not 0 <= now - hit["at"] < HEALTH_TTL:
            hit = _HEALTH[days] = dict(health_build(days, now), src=src)
        return hit


ADVICE_TTL = 30          # the advisor's cached answer is looked up at most this often per report: a frame must not read a file
ADVICE_LINES = 6         # lines the HEALTH screen has room for under the findings
ADVICE_NONE = "no advice yet: nuc-console-ask --advise asks the local model (this screen never does, it only shows an answer that is there)"
_ADVICE, _ADVICE_LOCK = {}, threading.Lock()


def health_advice(report):
    """(on, the advisor's answer for this report): on = [ai] enabled and its endpoint allowed; the answer is the CACHED one (None: nothing
    cached yet, or {"error"}). It never asks the model: a key or a page must not wait for a generation (nuc-console-ask --advise does that).
    Remembered for ADVICE_TTL per report object. Never raises: a broken advisor is no advice."""
    with _ADVICE_LOCK:
        now, hit = time.time(), _ADVICE.get("hit")
        if hit is not None and hit[0] is report and 0 <= now - hit[1] < ADVICE_TTL:
            return hit[2]
        try:
            import advisor
            cfg = ai_cfg()
            res = (True, advisor.try_advise(report, cfg, cached_only=True)) if advisor.available(cfg)[0] else (False, None)
        except Exception:  # noqa: BLE001
            res = (False, None)
        _ADVICE["hit"] = (report, now, res)
        return res


def health_extra_lines(report, w):
    """The ADVICE block under the findings: the advisor's cached answer (see health_advice), as at most ADVICE_LINES ANSI lines none wider
    than w, or [] when the advisor is off. The web page calls web.health_extra_html(report) at the same place."""
    on_, res = health_advice(report)
    if not on_:
        return []
    try:
        import advisor
        lines = advisor.lines(res, max(20, w - 2)) if res else textwrap.wrap(ADVICE_NONE, max(20, w - 2))
        text = [" " + hclean(x) for x in lines]
    except Exception:  # noqa: BLE001
        return []
    if not res:
        return [c(90, x) for x in text[:ADVICE_LINES]]
    cut = len(text) > ADVICE_LINES
    if cut:
        text = text[:ADVICE_LINES - 1] + [f" … +{len(text) - ADVICE_LINES + 1} more lines: nuc-console-ask --advise"]
    return [c(90, text[0])] + text[1:-1 if cut else None] + ([c(90, text[-1])] if cut else [])


def hansi(line):
    """A line from the advisor hook: its colours (SGR) stay, every other escape sequence and control character becomes '?'."""
    return "".join(x if re.fullmatch(r"\x1b\[[0-9;]*m", x) else hclean(x) for x in re.split(r"(\x1b\[[0-9;]*m)", str(line)))


def health_find_row(f, w):
    """A finding on one line (not cut to w): level pill, title, the text as far as it fits."""
    return ansi.finding_text(screens.health_finding(f, False, False, False), w)


def health_title(R, w, days, fl, selector=True):
    """The title line of the screen at w columns (screens.health_title drawn by ansi)."""
    return ansi.render(screens.health_title(R, days, fl, selector), w)[0][0]


def health_tables(R, w, h=None):
    """The sections under the findings as lines: the fullest level that fits h lines (None: everything, the web page scrolls)."""
    return screens.health_tables_lines(R, w, h, time.time())


def health_advice_node(R, w):
    """The ADVICE block for the console: the advisor's lines (health_extra_lines, cut and cleaned) in a ui.Advice, None when there are none."""
    lines = [clip(hansi(x), w) for x in health_extra_lines(R, w)[:6]]
    return ui.Advice(lines=lines) if lines else None


def health_body(data, hv, fl, w, h):
    """The Health screen's body: at most h lines, none wider than w (screens.health_lines draws the components the screen is made of)."""
    return screens.health_lines(data, hv, fl, w, h, health_advice_node, time.time())


def health_footer(hv, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    pos = f"finding {hv.idx + 1}/{n}" if n else "no findings"
    dyn = {"details": ("Enter: " + ("hide details" if hv.details else "details"), "Enter: " + ("hide" if hv.details else "details"))}
    return clip(c(90, ui.footer("health", f" {pos}   ", w, on, dyn)), w)


def health_screen(data, pb, hv, w, h):
    """(the interactive Health screen as one frame: header, body, key help; its findings): the live loop and --once."""
    fl = health_findings(data["report"])
    health_sync(hv, fl)
    body = health_body(data, hv, fl, w, body_rows(h))
    return frame(("Health", 1, 1, body), 0, 1, w, h, pb, foot=health_footer(hv, len(fl), w)), fl


def health_slide(w, body_h):
    """The Health screen among the rotating pages ([dashboard] health_in_rotation): no cursor; the findings that fit (the rest counted),
    then the top CPU and memory users."""
    data = health_data(7)
    R = data["report"]
    if R is None:
        return ([section("HEALTH", w)] + ansi.render(ui.Group(screens._msg("err" if data.get("err") else "info", data["msg"], w)), w)[0])[:body_h]
    fl = health_findings(R)
    now = time.time()
    out = [health_title(R, w, 7, fl, selector=False)]
    notes = [hclean(x) for x in R.get("notes") or [] if isinstance(x, str) and (R.get("coverage") or {}).get("since")]
    if notes and body_h >= 10:  # "collecting: 5 hours so far": a monitor nobody types on must not look conclusive
        out.append(clip(c(90, "  · " + notes[0]), w))
    avail = body_h - len(out)
    if not hnum((R.get("coverage") or {}).get("hours")):
        return (out + ansi.render(ui.Group(screens._msg("info", "no data in this period" if (R.get("coverage") or {}).get("since") else HEALTH_NONE, w)), w)[0])[:body_h]
    ncol, apps = (2 if w >= 110 else 1), []
    cw, days = ((w - 3) // 2 if ncol == 2 else w), int(hnum((R.get("period") or {}).get("days"), 7))
    for k in (0, 1, 2, 3):
        cols = [ansi.render(ui.Group(f(R, cw, k, days, now)), cw)[0] for f in (screens.hb_cpu, screens.hb_mem)]
        cand = columns([(x, cw) for x in cols], w, gap=3) if ncol == 2 else cols[0] + cols[1]
        if len(cand) <= avail - 3:  # the findings keep at least a title and two rows
            apps = cand
            break
    rows = max(0, avail - len(apps) - 1)
    out.append(section("FINDINGS", w))
    if not fl:
        out += ansi.render(ui.Group(screens._msg("info", health_nothing(R), w)), w)[0]
    elif rows:
        out += [clip(health_find_row(f, w), w) for f in fl[:rows if len(fl) <= rows else rows - 1]]
        if len(fl) > rows:
            out.append(c(90, f"   … +{len(fl) - rows + 1} more findings"))
    return [clip(x, w) for x in (out + apps)[:body_h]]


def health_state(smp, days):
    """(the cached report data, the header's problems): shared by the console loop and --once."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
    if DEMO:
        demo_defaults()
    return health_data(days), problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def health_once(argv, w, h):
    """`--once --view health`: the Health screen as the console draws it (tests, screenshots). --period 1|7|30 (days, default 7),
    --select TEXT: the cursor on the first finding whose id or title contains TEXT, --details: its details pane (Enter).
    --demo-health little|none: the demo with 5 hours of history, or with none. None: nothing to draw (the error is printed)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    if opt("--period") not in ("", "1", "7", "30"):
        print("--period must be 1, 7 or 30 (days)", file=sys.stderr)
        return None
    hv = HealthView(int(opt("--period") or 7))
    hv.details = "--details" in argv
    data, pb = health_state(None if DEMO else hostdata.Sampler(), hv.days)
    if opt("--select"):
        health_select(health_findings(data["report"]), hv, opt("--select"))
    return health_screen(data, pb, hv, w, h)[0]


# ---- EVENTS screen: events.py's timeline (the history's events table) on the console, moved through with the keyboard -----------

EVENTS_TTL = 5  # the events are read at most this often per request, whatever the number of keys or pages
_EVENTS, _EVENTS_LOCK = {}, threading.Lock()


def events_data(params):
    """events.load() of params (events.parse_query's), at most once per EVENTS_TTL for the same ones. [features] health = no: the history is off
    and the answer says so. The demo: demo.events_answer (--demo-health little|none for a young or an empty history)."""
    key = (bool(DEMO), DEMO_OS, DEMO_HEALTH, on("health")) + tuple(sorted(params.items()))
    with _EVENTS_LOCK:
        now = time.time()
        hit = _EVENTS.get(key)
        if hit is None or not 0 <= now - hit["at"] < EVENTS_TTL:
            if not on("health"):
                hit = events.load(params, now, False)
            elif DEMO:
                import demo
                hit = demo.events_answer(DEMO_OS, params, now, variant=DEMO_HEALTH)
            else:
                hit = events.load(params, now)
            _EVENTS[key] = hit
            while len(_EVENTS) > 16:
                _EVENTS.pop(next(iter(_EVENTS)))
        return hit


def events_state(smp, ev):
    """(the events for the view's period and kind, the header's problems): shared by the console loop and --once."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
    if DEMO:
        demo_defaults()
    return events_data(ev.params()), problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def events_footer(ev, n, w):
    """Where the cursor is, and the keys (ui.KEYMAP): in short words when the screen is narrow, then the least needed go first."""
    dyn = {"details": ("Enter: " + ("hide details" if ev.details else "details"), "Enter: " + ("hide" if ev.details else "details"))}
    return clip(c(90, ui.footer("events", f" {events.footer_pos(ev, n)}   ", w, on, dyn)), w)


def events_screen(data, pb, ev, w, h):
    """The interactive EVENTS screen as one frame (header, body, key help): the live loop and --once."""
    events.events_sync(ev, data["rows"])
    body = events.events_lines(data, ev, w, body_rows(h))
    return frame(("Events", 1, 1, body), 0, 1, w, h, pb, foot=events_footer(ev, len(data["rows"]), w))


def events_once(argv, w, h):
    """`--once --view events`: the EVENTS screen as the console draws it (tests, screenshots). --age 1h|24h|7d (default 24h), --kind KIND (one of
    history.KINDS), --select N: the cursor on the Nth newest event (1 = the first), --details: its details pane. None: nothing to draw."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    try:
        got = events.parse_query({k: [v] for k, v in (("age", opt("--age")), ("kind", opt("--kind"))) if v})
    except events.QueryError as e:
        print("--view events: " + str(e), file=sys.stderr)
        return None
    ev = events.EventsView(got["age"] or events.DEFAULT_AGE, got["kind"])
    ev.details = "--details" in argv
    data, pb = events_state(None if DEMO else hostdata.Sampler(), ev)
    if opt("--select").isdigit() and data["rows"]:
        ev.idx = max(0, min(int(opt("--select")) - 1, len(data["rows"]) - 1))
        ev.cur = data["rows"][ev.idx]["id"]
    return events_screen(data, pb, ev, w, h)


# ---- AI screen: what this machine can run, and which local model to choose (aisetup.catalog(): the console, --once, the web page) ---
# Data: aisetup.catalog() (docs/AI.md): {hw, dir, runtime, recommended, active, models: [{..., assess, installed, pinned, commands}]}.
# Everything in it is data (a model's name comes from a catalog file, a GPU's from a driver): text goes through hclean(), numbers through
# num(), a value that is missing is drawn as "?". The screen runs nothing and downloads nothing: it shows the commands to type.

# The screen's view-model is in screens.py (the AI block: the model rows, the view and its keys, every piece drawn as components); what is
# here reads the world (the catalog, the engine, the probe of the model server) and runs the live loop.


def ai_body(data, st, av, rows, w, h):
    return screens.ai_lines(data, st, av, rows, w, h)


def ai_footer(av, n, w, snap=None):
    """The footer of the AI screen as one line (screens.ai_footer drawn)."""
    return ansi.render(screens.ai_footer(av, n, w, snap, on), w)[0][0]


AI_IDLE_S = 600          # the screen left alone this long gives the monitor back to the rotation (nobody may be at the keyboard)
AI_TTL = 10              # the catalog is read at most this often, whatever the number of keys or requests
AI_PROBE_TTL = 60        # does the model server answer? looked at most this often ...
AI_PROBE_TIMEOUT = 1.0   # ... for at most this long ...
AI_PROBE_STUCK = 30      # ... in a thread of its own (a key or a page never waits for it; one stuck this long is given up)
AI_NONE = "the model catalog could not be read"
_AI, _AI_LOCK = {}, threading.Lock()
_AIPROBE, _AIPROBE_LOCK = {"res": None, "at": 0.0, "key": None, "thread": None, "started": 0.0}, threading.Lock()


def ai_build(now):
    """{"cat": dict | None, "msg": why there is none, "err": bool, "at": now}: the demo's machine, or aisetup.catalog() (it reads the
    hardware and the files of the AI directory, never the network)."""
    if DEMO:
        return {"cat": ai_engine().demo_catalog(DEMO_OS), "msg": "", "err": False, "at": now}  # the invented machine, as the simulated actions left it
    try:
        import aisetup  # a missing or broken module costs this screen, never the dashboard
        cat = aisetup.catalog()  # the folder the buttons work in (aisetup.work_dir()), the active model as the AI page chose it
        if not isinstance(cat, dict):
            raise TypeError("catalog() gave no dict")
        return {"cat": cat, "msg": "", "err": False, "at": now}
    except Exception as e:  # noqa: BLE001 - say so, and keep the dashboard
        return {"cat": None, "msg": AI_NONE + ": " + safe(repr(e))[:100], "err": True, "at": now}


def ai_data():
    """ai_build() at most once per AI_TTL: the console and every web request share it, so a key or a page does not read the hardware
    files again. A failure is kept for the same time (no retry on every key)."""
    with _AI_LOCK:
        now, key = time.time(), (DEMO, DEMO_OS)
        hit = _AI.get("hit")
        ver = ai_version()
        if hit is None or hit["key"] != key or not 0 <= now - hit["at"] < AI_TTL or hit.get("ver", ver) != ver:  # a job moved: read it again
            hit = _AI["hit"] = dict(ai_build(now), key=key, ver=ver)
        return hit


def ai_report(days):
    """The HEALTH report of the last `days` days, from the history the screens share (health_data): what "advice now" is asked about."""
    import advisor
    data = health_data(days)
    if data.get("report") is None:
        raise advisor.NoHistory(str(data.get("msg") or "no history yet"))
    return data["report"]


def ai_engine():
    """The engine of this process (src/aiweb.py), told where this program's settings and HEALTH report are (it must not import render: it may be
    __main__): the real one, or while --demo is on the demo's: the invented machines act on an engine of their own, in memory (nothing is
    downloaded, started or written)."""
    import aiweb
    aiweb.bind(cfg=lambda: CFG, report=ai_report)
    eng = aiweb.engine()
    return eng if eng.demo == bool(DEMO) else aiweb.configure(demo=bool(DEMO))


def ai_version():
    """The engine's change counter (a download ended, a server started...): the catalog is read again when it moves. 0 without an engine."""
    try:
        import aiweb
        return aiweb.version()
    except Exception:  # noqa: BLE001
        return 0


def ai_cfg():
    """The advisor's settings as the AI page and screen left them: config.ini with web.json laid over it (advisor.effective_cfg)."""
    try:
        import advisor
        import aiweb
        return advisor.effective_cfg(CFG, aiweb.engine().web_path())
    except Exception:  # noqa: BLE001 - a broken module is config.ini's word
        return CFG


def ai_probe_run(ai):
    """One look at the model server of [ai] endpoint: {"state": "answering" | "down", "msg", "models"}. Never raises."""
    try:
        import advisor
        info = advisor.endpoint_info(ai.get("endpoint"), bool(ai.get("allow_remote")))
        models = advisor.list_models(info, AI_PROBE_TIMEOUT)
        return {"state": "answering", "msg": "", "models": [hclean(x, 80) for x in models[:20]]}
    except Exception as e:  # noqa: BLE001 - an AdvisorError says what to do about it; anything else is only named
        return {"state": "down", "msg": hclean(str(e) if hasattr(e, "exit_code") else type(e).__name__, 160), "models": []}


def ai_probe_work(ai, key):
    res = ai_probe_run(ai)
    with _AIPROBE_LOCK:
        _AIPROBE.update(res=res, at=time.time(), key=key)


def ai_probe(wait=0.0):
    """Does the model server answer? The last probe, or None while there is none yet. A probe older than AI_PROBE_TTL is made again in a thread
    of its own and the older answer stays until the new one is in: a key or a page never waits for the network (wait: --once, which may).
    Nothing is asked while [ai] enabled = no."""
    ai = (CFG if DEMO else ai_cfg()).get("ai") or {}
    if not ai.get("enabled"):
        return {"state": "off", "msg": "[ai] enabled = no in config.ini", "models": []}
    key = (str(ai.get("endpoint")), bool(ai.get("allow_remote")))
    with _AIPROBE_LOCK:
        now, st = time.time(), _AIPROBE
        t = st["thread"]
        if (st["key"] != key or not 0 <= now - st["at"] < AI_PROBE_TTL) and (t is None or not t.is_alive() or now - st["started"] > AI_PROBE_STUCK):
            t = st["thread"] = threading.Thread(target=ai_probe_work, args=(dict(ai), key), daemon=True)
            st["started"] = now
            t.start()
    if wait and t is not None:
        t.join(wait)
    with _AIPROBE_LOCK:
        return st["res"] if st["key"] == key else None


def ai_status(wait=0.0):
    """What the STATUS section reads: {enabled, endpoint, model, probe}: [ai] as config.ini has it, and the last probe of its server."""
    if DEMO:
        eng = ai_engine()
        st = eng.demo_status(DEMO_OS)
        st["snap"] = eng.snapshot()
        st["switch"] = st["snap"]["switch"]
        return st
    ai = ai_cfg().get("ai") or {}
    out = {"enabled": bool(ai.get("enabled")), "endpoint": hclean(ai.get("endpoint"), 120), "model": hclean(ai.get("model"), 80), "probe": ai_probe(wait)}
    try:
        out["snap"] = ai_engine().snapshot()
        out["switch"] = out["snap"]["switch"]
    except Exception:  # noqa: BLE001
        pass
    return out


def ai_state(smp):
    """(the catalog's data, the header's problems): shared by the console loop, --once and the web page."""
    st = snapshot(0)
    sm = host_sample(smp)
    keep(st, sm)
    if DEMO:
        demo_defaults()
    return ai_data(), problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm.get("thermal"), baseline=st["baseline"])


def ai_do(av, act, rows):
    """What a key of the AI screen asked (ai_key): done through the engine, which works in the background (a download, the server, the answers
    come back as its snapshot: ai_work_lines draws them), or a question first (av.confirm, answered with y). A line only this screen says
    goes in av.msg. Never raises: an engine that cannot start is a line."""
    av.msg = None
    try:
        import aiweb
        eng = ai_engine()
        if act == "yes":
            kind, mid, _q = av.confirm or (None, None, None)
            av.confirm = None
            return {"on": lambda: eng.turn_on(mid), "delete": lambda: eng.delete(mid), "delete-all": eng.delete_all}.get(kind, lambda: None)()
        if eng.locked():
            av.msg = ("err", aiweb.LOCKED)
            return None
        row = rows[ai_sync(av, rows)] if rows else None
        if act == "cancel":
            return eng.cancel()
        if act == "toggle":
            snap = eng.snapshot()
            if snap["state"][0] == "working":
                return eng.cancel()
            if snap["switch"]["on"]:
                return eng.turn_off()
            ch = eng.choice()
            if ch["model"] is None and ch["recommended"]:  # nothing chosen yet: ask about the recommended one first, naming its size
                t = next((r for r in rows if r["id"] == ch["recommended"]), None)
                todo = "installed here" if ch["installed"] else (f"{ai_mb((ch['size'] or 0) / 2 ** 20)} to download" if ch["size"] else "not downloadable yet")
                av.confirm = ("on", ch["recommended"], f"Turn AI on with {t['name'] if t else ch['recommended']} ({todo})?")
                return None
            return eng.turn_on()
        if row is None:
            av.msg = ("warn", "no model is selected")
            return None
        if act == "use":
            return eng.use_model(row["id"])
        if act == "delete":
            if not row["installed"]:
                av.msg = ("warn", f"{row['name']} is not installed: nothing to delete")
                return None
            av.confirm = ("delete", row["id"], f"Delete the files of {row['name']} ({ai_mb(row['size_mb'])})?")
        elif act == "delete-all":
            used = dd(dd(ai_data()["cat"]).get("space")).get("used")
            if not used and not any(r["installed"] for r in rows):
                av.msg = ("warn", "nothing is downloaded: nothing to delete")
                return None
            av.confirm = ("delete-all", None, f"Delete the runtime and every downloaded model ({screens.ai_size(used)})?")
    except Exception as e:  # noqa: BLE001 - the screen goes on
        av.msg = ("err", "the AI engine could not do that: " + hclean(repr(e), 120))
    return None


def ai_busy():
    """Something is running (a download, the server starting, an answer): the screen looks again every second. False without an engine."""
    try:
        return bool(ai_engine().snapshot()["busy"])
    except Exception:  # noqa: BLE001
        return False


def ai_screen(data, pb, av, w, h, wait=0.0):
    """(the interactive AI screen as one frame: header, body, key help; its model rows): the live loop and --once."""
    rows = ai_rows(data["cat"])
    ai_sync(av, rows)
    st = ai_status(wait)
    body = ai_body(data, st, av, rows, w, body_rows(h))
    return frame(("AI", 1, 1, body), 0, 1, w, h, pb, foot=ai_footer(av, len(rows), w, st.get("snap"))), rows


def ai_once(argv, w, h):
    """`--once --view ai`: the AI screen as the console draws it (tests, screenshots). --select TEXT: the cursor on the first model whose id or
    name contains TEXT (any case), --details: its details (Enter). --demo, with --demo-os windows|darwin: three invented machines (src/demo.py)."""
    opt = lambda k: argv[argv.index(k) + 1] if k in argv[:-1] else ""  # noqa: E731
    av = AiView()
    av.details = "--details" in argv
    data, pb = ai_state(None if DEMO else hostdata.Sampler())
    if opt("--select"):
        ai_select(ai_rows(data["cat"]), av, opt("--select"))
    return ai_screen(data, pb, av, w, h, wait=AI_PROBE_TIMEOUT + 0.5)[0]


# ---- kiosk: macOS/Windows have no text console to take over, the monitor shows the screen in a full-screen browser -----


def open_in_browser(argv):
    """`render.py --open`: the dashboard in a normal window of the default browser ([display] mode = browser, at every login).

    The page is the local web view (127.0.0.1, started by the installer at boot): at login it may need a few seconds more. With a
    token in [web], the URL carries it when this user can read the token file (the web view moves it into a cookie and keeps the view);
    otherwise the dashboard is the page written to a file, as `--kiosk` does."""
    base = display.user_dir()
    os.makedirs(base, exist_ok=True)
    if sys.stderr is None or "--log" in argv[:-1]:  # pythonw / launched at logon: no console to write to
        nuc_config.log_to(argv[argv.index("--log") + 1] if "--log" in argv[:-1] else os.path.join(base, "display.log"))
    url, token = display.dashboard_url(), ""
    if CFG["web"]["token_file"]:
        token, why = display.read_web_token(CFG["web"]["token_file"])
        if not token:
            print(f"[web] token_file is set but {why}: the dashboard is shown from a page written to a file instead", file=sys.stderr, flush=True)
            return kiosk_file(argv, base, *display.kiosk_grid())
    if not display.web_up(CFG["web"]["port"], 60):
        print(f"the web view does not answer on 127.0.0.1:{CFG['web']['port']}: dashboard not opened", file=sys.stderr, flush=True)
        return 1
    print(f"open -> {url}" + (" (with the token of [web] token_file)" if token else ""), file=sys.stderr, flush=True)  # never the token itself
    if token:
        url += ("&" if "?" in url else "?") + "token=" + token
    if WINDOWS:
        os.startfile(url)  # the default browser, as this user
    elif MACOS:
        subprocess.run(["/usr/bin/open", url], timeout=30)
    else:
        import webbrowser
        webbrowser.open(url)
    return 0


def kiosk(argv):
    """`render.py --kiosk`: the dashboard full screen, for the monitor of a Mac or a Windows PC (the display at login).

    It opens the local web view (127.0.0.1, started by the installer) in a full-screen browser window: overview and Details
    pages take turns, A- / A+ change the text size. Without the web view (a Linux desktop, or a token in [web]) it falls
    back on a page written to a file every 2 s (`--file` forces it; `--html FILE`, `--no-browser`)."""
    base = display.user_dir()
    os.makedirs(base, exist_ok=True)
    if sys.stderr is None or "--log" in argv[:-1]:  # pythonw / launched at logon: no console to write to
        nuc_config.log_to(argv[argv.index("--log") + 1] if "--log" in argv[:-1] else os.path.join(base, "display.log"))
    cols, rows = display.kiosk_grid()
    web = CFG["web"]
    if "--file" not in argv and not web["token_file"] and display.web_up(web["port"], 60):
        url = display.dashboard_url(fullscreen=True)
        exe = display.find_browser()
        cmd = display.kiosk_command(exe, url, base)
        print(f"kiosk -> {url}", file=sys.stderr, flush=True)
        if "--no-browser" not in argv and cmd:
            display.launch(cmd)
            return 0
        if "--no-browser" not in argv and display.default_browser(exe, url):
            return 0
        print("no browser started: open " + url, file=sys.stderr, flush=True)
        return 0 if "--no-browser" in argv else 1
    return kiosk_file(argv, base, cols, rows)


def kiosk_file(argv, base, cols, rows):
    """The fallback: the dashboard as an HTML file rewritten every 2 s (no network at all). Ends when the browser closes."""
    import htmlview
    zoom = CFG["display"]["zoom"]
    cols, rows = max(60, round(cols * 100 / zoom)), max(16, round(rows * 100 / zoom))  # bigger text = a smaller grid
    path = argv[argv.index("--html") + 1] if "--html" in argv[:-1] else os.path.join(base, "display.html")
    w, h = cols, rows  # a browser page: every column is usable (no Linux console last-column quirk)
    print(f"kiosk {cols}x{rows} -> {path}", file=sys.stderr, flush=True)
    smp, t0, browser, started, cmd, cpu_feed = hostdata.Sampler(), time.time(), None, 0.0, None, None
    while True:
        try:
            st, sm = snapshot(w), smp.sample()
            sl = slides(sm, st["cont"], st["net"], w, h - 2, st["boot"], st["baseline"], cpu_lazy=True)
            idx = pick_slide(sl, time.time() - t0) % len(sl)
            if sl[idx][0] == "CPU":  # its samplers run only while it is on screen (see main)
                cpu_feed = cpu_feed or CpuFeed()
                fill_cpu(sl, idx, w, h - 2, cpu_feed)
            else:
                cpu_feed = None
            screen = frame(sl[idx], idx, len(sl), w, h,
                           problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"]),
                           keys=False, hint=display.KIOSK_HINT, page=True)
            display.write_text_atomic(path, htmlview.kiosk_page(screen, cols, rows, REFRESH_S, nuc_config.host_label(CFG, socket.gethostname)))
        except Exception as e:  # noqa: BLE001 - a broken frame must not close the kiosk: the next one may be fine
            print("kiosk frame error:", repr(e)[:200], file=sys.stderr, flush=True)
        if browser is None and "--no-browser" not in argv:
            import pathlib
            exe = display.find_browser()
            cmd = display.kiosk_command(exe, pathlib.Path(path).resolve().as_uri(), base)
            if cmd:
                browser, started = display.launch(cmd), time.time()
            else:
                browser = False
                if not display.default_browser(exe, path):
                    print("no browser found: open " + path + " yourself, or set [display] browser in config.ini", file=sys.stderr, flush=True)
        # the viewer closed the window (Alt+F4): stop. A browser that quits at once handed the page to a running one: keep going
        if browser and cmd[0] != "/usr/bin/open" and browser.poll() is not None and time.time() - started > 10:
            return 0
        time.sleep(REFRESH_S)


# ---- keys: the same names on every OS (up down left right pgup pgdn home end tab btab enter esc space, or the character) ---

KEY_CHAR = {"\t": "tab", "\r": "enter", "\n": "enter", "\x1b": "esc", " ": "space"}
CSI_KEY = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "btab"}
TILDE_KEY = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn"}  # Linux VT, xterm, rxvt
WIN_SCAN = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "I": "pgup", "Q": "pgdn", "\x0f": "btab"}
ESC_SEQ = re.compile(r"\x1b(\[\[|\[|O)([0-9;]*)([A-Za-z~])")  # CSI / SS3 (+ modifiers); '\x1b[[A' = F1 on the Linux VT
ESC_TAIL = re.compile(rb"\x1b(\[\[?[0-9;]*|O)?$")  # a read that ends inside a sequence (or on a lone Esc)


def decode_keys(raw):
    """Key names in what a POSIX terminal sent: one read may hold several keys. Unknown sequences (F keys) are dropped."""
    s = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\x1b" and s[i + 1:i + 2] in ("[", "O"):
            m = ESC_SEQ.match(s, i)
            if m:
                intro, args, fin = m.groups()
                name = "" if intro == "[[" else TILDE_KEY.get(args.split(";")[0], "") if fin == "~" else CSI_KEY.get(fin, "")
                out += [name] if name else []
            i = m.end() if m else i + 2  # cut short: dropped, never read as Esc + letters
            continue
        if ch == "\r" and s[i + 1:i + 2] == "\n":
            i += 1
            continue
        name = KEY_CHAR.get(ch) or (ch if ch.isprintable() else "")
        out += [name] if name else []
        i += 1
    return out


def read_keys(fd, wait):
    """Keys typed on the POSIX terminal fd within `wait` seconds: [] if none, None when the terminal is gone."""
    if not select.select([fd], [], [], max(0.0, wait))[0]:
        return []
    raw = os.read(fd, 64)
    if not raw:
        return None
    for _ in range(4):  # an escape sequence cut in two by the read, or a lone Esc: the rest comes within milliseconds
        if not ESC_TAIL.search(raw) or not select.select([fd], [], [], 0.05)[0]:
            break
        more = os.read(fd, 64)
        if not more:
            break
        raw += more
    return decode_keys(raw)


def win_key(ch, nxt=""):
    """The name of a key read with msvcrt.getwch(): arrows & co. are two reads, '\\x00' or '\\xe0' then a scan code."""
    if ch in ("\x00", "\xe0") and nxt:
        return WIN_SCAN.get(nxt, "")
    if ch == "\x00":
        return ""
    return KEY_CHAR.get(ch) or (ch if ch.isprintable() else "")


def windows_key(timeout):
    """The name of a key typed in a Windows console within `timeout` seconds, or '' (msvcrt has no select)."""
    import msvcrt
    end = time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            # a prefix is followed at once by its scan code; '\xe0' alone is a letter ('à' on an Italian keyboard)
            nxt = msvcrt.getwch() if ch == "\x00" or (ch == "\xe0" and msvcrt.kbhit()) else ""
            return win_key(ch, nxt)
        if time.monotonic() >= end:
            return ""
        time.sleep(0.05)


HELP_W = 64  # the help box's widest size (columns), inside the frame


def new_view(name, opened):
    """(mv, cv, hv, av): the screen `name` (map, cpu, health, ai) open, the others None. (The EVENTS screen is never a start view.)"""
    v = {"map": MapView, "cpu": CpuView, "health": HealthView, "ai": AiView}[name]()
    v.opened = v.touched = opened
    return tuple(v if n == name else None for n in ("map", "cpu", "health", "ai"))


def help_box(scope, w, h, enabled, portable=None, paused=False):
    """The `?` overlay's box: the screen's own keys and the global ones from ui.KEYMAP, as many as fit in a frame w x h (the header and the
    footer stay visible): the last ones of the table go first and the box says how many are left. Lines of equal width, ANSI allowed."""
    groups = ui.help_rows(scope, enabled, nuc_config.PORTABLE if portable is None else portable, paused)
    bw = max(20, min(HELP_W, w - 2))
    lw = min(max(len(k) for _t, items in groups for k, _v in items) + 2, bw // 2)
    inner = bw - 4
    room = max(1, h - 5)  # the frame's header and footer, the box's two borders, the line that says how to close it
    keep = [list(items) for _t, items in groups]
    dropped = 0

    def size(sep):
        shown = [g for g in keep if g]
        return sum(1 + len(g) for g in shown) + (len(shown) - 1 if sep and shown else 0) + (1 if dropped else 0)
    sep = size(True) <= room  # a blank line between the groups when there is room
    while size(sep) > room and any(keep):
        next(g for g in reversed(keep) if g).pop()
        dropped += 1
    lines = []
    for (title, _items), g in zip(groups, keep):
        if g:
            lines += ([""] if lines and sep else []) + [c(ui.sgr("accent_strong"), title)]
            lines += [c(ui.sgr("strong"), pad(k[:lw - 2], lw)) + v[:inner - lw] for k, v in g]
    if dropped:
        lines.append(c(ui.sgr("muted"), f"+{dropped} more"))
    lines.append(c(ui.sgr("muted"), "any key closes this help"))
    top = c(ui.sgr("accent"), "┌─ ") + c(ui.sgr("accent_strong"), "Keys") + c(ui.sgr("accent"), " " + "─" * (bw - 9) + "┐")
    box = [c(ui.sgr("accent"), "│") + " " + pad(clip(x, inner), inner) + " " + c(ui.sgr("accent"), "│") for x in lines]
    return [top] + box + [c(ui.sgr("accent"), "└" + "─" * (bw - 2) + "┘")]


def help_overlay(screen, scope, w, h, enabled, paused=False):
    """The frame (as frame() returns it) with the help box over its middle."""
    lines = screen.split("\x1b[K\r\n")
    return "\x1b[K\r\n".join(ansi.overlay(lines, help_box(scope, w, h, enabled, paused=paused), w))


def main(argv):
    global PAUSED
    utf8_stdout()
    if "--problems" in argv:
        return print_problems(argv)
    if "--accept" in argv and ("--problem" in argv or "--forget" in argv):
        flag = "--problem" if "--problem" in argv else "--forget"
        opt = lambda k: argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else ""
        return accept_problem(opt(flag), opt("--reason"), forget=flag == "--forget")
    if "--accept" in argv:
        return problems.accept_baseline(if_missing="--if-missing" in argv)
    if "--demo" in argv and "--once" not in argv:
        print("--demo only works together with --once", file=sys.stderr)
        return 2
    if "--once" in argv:
        return once(argv)
    if "--kiosk" in argv:
        return kiosk(argv)
    if "--open" in argv:
        return open_in_browser(argv)
    smp = hostdata.Sampler()
    fd = sys.stdin.fileno() if sys.stdin else -1
    old = termios.tcgetattr(fd) if termios and fd >= 0 and os.isatty(fd) else None
    win_keys = WINDOWS and fd >= 0 and os.isatty(fd)
    if WINDOWS:
        import winapi
        winapi.enable_vt()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    out = sys.stdout
    try:
        if old:
            tty.setcbreak(fd)
        out.write("\x1b[?25l\x1b[2J")
        t0, hold_until, held, size = time.time(), 0, 0, None
        map_ok = on("map") and bool(old or win_keys)  # the Map needs a keyboard: without one only map_in_rotation shows it
        cpu_ok = on("cpu") and bool(old or win_keys)  # so does the CPU screen (cpu_in_rotation is for a monitor without one)
        health_ok = on("health") and bool(old or win_keys)  # and the Health screen (health_in_rotation without one)
        ai_ok = on("ai") and bool(old or win_keys)  # and the AI screen (it is not part of the rotation: nobody chooses a model from a monitor)
        kctx = None  # the overview's cards.Ctx (the KPI line); the other screens' is made of what they read (kept_ctx)
        mv, G, pb, fresh, rs = None, None, [], 0.0, []  # the Map while it is shown, its graph and problems, next data refresh
        err, last_pb = None, []  # why the Map or Health could not be drawn (until the next refresh); the rotation's last problems
        cv, cpu_d, cpu_fresh, cpu_rows_, rot_feed = None, None, 0.0, [], None  # the CPU screen, its data and rows; the rotation slide's feed
        hv, hd, hl = None, None, []  # the Health screen while it is shown, its report data and findings
        av, ad, al = None, None, []  # the AI screen while it is shown, its catalog data and model rows
        ev, ed = None, None  # the EVENTS screen while it is shown, and its rows
        hs, hstate = None, None  # the Hosts screens (key 6: the list, or another host's views) while shown, and what the cache last said
        events_ok = bool(old or win_keys)  # it needs a keyboard too, and no feature: with the history off it says so
        avail = {"map": map_ok, "cpu": cpu_ok, "health": health_ok, "ai": ai_ok, "events": events_ok}  # the screens the digits (and Tab) can open
        enabled = lambda f: fleet_on() if f == ui.CONSOLE_FLEET else avail.get(f, True)  # noqa: E731
        waker = wake.Waker(CFG["console"]["wake_on_problem"] and not DEMO, fd)  # a new problem wakes a blanked Linux console (wake.py)
        sl, idx, ov_cache = [], 0, None  # the overview's slides and the one shown; what a paused overview keeps showing
        paused, pause_t, pre_hold, pause_ov = False, 0.0, 0, False  # Z: the redraw is paused (since when; the hold it interrupted)
        dirty, help_open = True, False  # a frame is due even when paused; the `?` overlay is shown
        start = ui_cfg().get("start_view", "overview")  # [ui] start_view: the screen at the start, and where an idle one goes back to
        start = start if start != "overview" and avail.get(start) else "overview"  # a screen needs its keyboard (and its feature on)
        if start != "overview":
            mv, cv, hv, av = new_view(start, time.time())

        def close_hosts():
            """Back from the Hosts screens to the local screen they were opened from, as it was left (its view objects are put back)."""
            nonlocal mv, cv, hv, av, ev, hs, t0, ov_cache, fresh, cpu_fresh, err, cpu_d
            gone, hs = hs, None
            mv, cv, hv, av, ev = gone.stash
            stamp = time.time()
            for v in (mv, cv, hv, av, ev):
                if v is not None:
                    v.touched = stamp
            if gone.origin == "overview":  # the rotation was paused all the time: it goes on where it was
                t0 += stamp - gone.opened
                ov_cache = None
            fresh = cpu_fresh = 0.0
            err, cpu_d = None, None
            out.write("\x1b[2J")

        def resume():
            """Z again (or another screen): the redraw goes on; a paused overview carries on with the slide it was at."""
            nonlocal paused, t0, hold_until
            if paused and pause_ov:
                gap = time.time() - pause_t
                t0, hold_until = t0 + gap, pre_hold + gap
            paused = False
        while True:
            w, h = shutil.get_terminal_size((120, 33))
            # config.ini [dashboard] columns/rows: layout size forced smaller than the real console (never larger: it would run off-screen)
            w, h = min(w, CFG["columns"] or w), min(h, CFG["rows"] or h)
            if (w, h) != size:  # the real console size ends up in the journal: journalctl -u nuc-console
                print(f"console {w}x{h} mode={MODE}", file=sys.stderr, flush=True)
                out.write("\x1b[2J")
                size, dirty = (w, h), True
            w -= 1  # the Linux VT keeps the cursor on the last column: \x1b[K there would erase the last character
            now = time.time()
            was_open = any(x is not None for x in (mv, cv, hv, av, ev, hs))
            if hs is not None and hostsview.idle(hs, now, CFG["console"]["remote_idle_seconds"]):  # nobody at the keyboard: never leave a display on another host
                close_hosts()
                dirty = True
            if mv is not None and now - mv.touched > MAP_IDLE_S:  # nobody at the keyboard: the monitor goes back to the rotation
                t0, mv, paused, dirty = t0 + now - mv.opened, None, False, True
                out.write("\x1b[2J")
            if cv is not None and now - cv.touched > CPU_IDLE_S:  # same for the CPU screen
                t0, cv, paused, dirty = t0 + now - cv.opened, None, False, True
                out.write("\x1b[2J")
            if hv is not None and now - hv.touched > HEALTH_IDLE_S:  # and the Health screen
                t0, hv, paused, dirty = t0 + now - hv.opened, None, False, True
                out.write("\x1b[2J")
            if av is not None and now - av.touched > AI_IDLE_S:  # and the AI screen
                t0, av, paused, dirty = t0 + now - av.opened, None, False, True
                out.write("\x1b[2J")
            if ev is not None and now - ev.touched > events.IDLE_S:  # and the EVENTS screen
                t0, ev, paused, dirty = t0 + now - ev.opened, None, False, True
                out.write("\x1b[2J")
            if start != "overview" and was_open and mv is cv is hv is av is ev is hs is None:  # an idle screen: back to the start view, not the rotation
                mv, cv, hv, av = new_view(start, now)
                fresh = cpu_fresh = 0.0
                err, cpu_d = None, None
            PAUSED = paused
            if mv is not None:  # the Map: new data every REFRESH_S, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + REFRESH_S, None  # set first: after a failure keys redraw the error, never postpone the retry
                        G, pb = map_graph(smp)
                    if err is None:
                        screen, rs = map_screen(G, pb, mv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken map must not take the console down; Esc still goes back
                    G, rs, err = None, [], e
                    pb = last_pb + [(2, "the map could not be built")]  # its problems are unknown: never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("Map", 1, 1, [c(31, f" error on the map: {safe(repr(err))[:w - 20]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            elif cv is not None:  # the CPU screen: new data every REFRESH_S (the first one after a second: a CPU% needs two readings)
                try:
                    if now >= cpu_fresh and not paused:
                        cpu_fresh = now + (min(1.0, REFRESH_S) if cpu_d is None else REFRESH_S)
                        st, sm = snapshot(w), smp.sample()  # the header's status pill stays true while the screen is open
                        last_pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
                        keep(st, sm)
                        cpu_d = cv.feed.read()
                    screen, cpu_rows_ = cpu_screen(cpu_d, last_pb, cv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; c/Esc still go back
                    cpu_rows_ = []
                    screen = frame(("CPU", 1, 1, [c(31, f" error on the CPU screen: {safe(repr(e))[:w - 26]}")]), 0, 1, w, h,
                                   last_pb + [(2, "the CPU screen could not be drawn")], foot=c(90, " Esc: back   1-5: screens"))
                wait = cpu_fresh - time.time()
            elif hv is not None:  # the Health screen: the report comes from its one-minute cache, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + REFRESH_S, None
                        hd, pb = health_state(smp, hv.days)
                    if err is None:
                        screen, hl = health_screen(hd, pb, hv, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    hd, hl, err = None, [], e
                    pb = last_pb + [(2, "the health screen could not be built")]  # never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("Health", 1, 1, [c(31, f" error on the health screen: {safe(repr(err))[:w - 30]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            elif av is not None:  # the AI screen: the catalog comes from its short cache, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + (1.0 if ai_busy() else REFRESH_S), None  # while something runs (a download) it looks again every second
                        ad, pb = ai_state(smp)
                    if err is None:
                        screen, al = ai_screen(ad, pb, av, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    ad, al, err = None, [], e
                    pb = last_pb + [(2, "the AI screen could not be built")]  # never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("AI", 1, 1, [c(31, f" error on the AI screen: {safe(repr(err))[:w - 28]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            elif hs is not None:  # the Hosts list or another host's views: the cache is read at every refresh, a new frame at every key
                try:
                    if hstate is None or (now >= fresh and not paused):
                        fresh = now + REFRESH_S
                        hstate = hosts_state(now)
                        last_pb = hstate[4]
                    screen = hosts_screen(hs, hstate, w, h, now)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    words = f"error on the Hosts screen: {safe(repr(e))[:w - 30]}"
                    screen = ("\x1b[K\r\n".join(remote_view(hs.name, None, w, h, body=[msg("err", words)])) if hs.mode == "remote"
                              else frame(("Hosts", 1, 1, [c(31, " " + words)]), 0, 1, w, h, last_pb, foot=c(90, " Esc: back")))
                wait = fresh - time.time()
            elif ev is not None:  # the EVENTS screen: the rows come from a short cache, a new frame at every key
                try:
                    if now >= fresh and not paused:
                        fresh, err = now + REFRESH_S, None
                        ed, pb = events_state(smp, ev)
                    if err is None:
                        screen = events_screen(ed, pb, ev, w, h)
                except Exception as e:  # noqa: BLE001 - a broken screen must not take the console down; Esc still goes back
                    ed, err = None, e
                    pb = last_pb + [(2, "the events screen could not be built")]  # never a reassuring "ALL OK"
                if err is not None:
                    screen = frame(("Events", 1, 1, [c(31, f" error on the events screen: {safe(repr(err))[:w - 30]}")]), 0, 1, w, h, pb,
                                   foot=c(90, " Esc: back   1-5: screens"))
                wait = fresh - time.time()
            else:
                if paused and ov_cache is not None:  # the slide and the data it was paused on
                    sl, last_pb, kctx = ov_cache
                    idx = held % len(sl)
                else:
                    st, sm = snapshot(w), smp.sample()
                    sl = slides(sm, st["cont"], st["net"], w, body_rows(h), st["boot"], st["baseline"], cpu_lazy=True)
                    idx = (held if now < hold_until else pick_slide(sl, now - t0)) % len(sl)
                    if sl[idx][0] == "CPU":  # its samplers run only while it is on screen
                        rot_feed = rot_feed or CpuFeed()
                        fill_cpu(sl, idx, w, body_rows(h), rot_feed)
                    else:
                        rot_feed = None
                    last_pb = problems.safe_problems(st["net"], st["cont"], boot=st["boot"], thermal=sm["thermal"], baseline=st["baseline"])
                    kctx = make_ctx(st, sm, last_pb) if kpi_on(h) else None
                    ov_cache = (sl, last_pb, kctx)
                screen = frame(sl[idx], idx, len(sl), w, h, last_pb, keys=bool(old or win_keys), mapkey=map_ok, cpukey=cpu_ok, healthkey=health_ok,
                               aikey=ai_ok, ctx=kctx)  # no keyboard (a monitor): no keys are offered
                wait = REFRESH_S
            waker.feed(pb if (mv is not None or hv is not None or av is not None) else last_pb)
            scope = "map" if mv is not None else "cpu" if cv is not None else "health" if hv is not None else "ai" if av is not None else "events" if ev is not None else hs.scope if hs is not None else "overview"
            if help_open:
                screen = help_overlay(screen, scope, w, h, enabled, paused)
            if paused:  # nothing new to draw until a key: the frame, the clock and the data stay as they are
                wait = REFRESH_S
            if dirty or not paused:
                out.write("\x1b[H" + themed(screen))
                out.flush()
                dirty = False
            keys = []
            if old:
                keys = read_keys(fd, wait)
                if keys is None:  # tty closed/hangup: select always fires, without a pause it would be a busy loop
                    time.sleep(REFRESH_S)
                    continue
            elif win_keys:
                keys = [k for k in [windows_key(wait)] if k]
            else:
                time.sleep(max(0.0, wait))
            for k in keys:
                dirty = True
                if help_open:  # any key closes the help, and does nothing else
                    help_open = False
                    continue
                view = mv if mv is not None else cv if cv is not None else hv if hv is not None else av if av is not None else ev if ev is not None else hs
                if view is not None:
                    view.touched = time.time()
                if hs is not None and hs.mode == "remote":  # another host's screen: its keys first (the digits and Tab choose ITS views)
                    res = hostsview.remote_key(hs, k, hostsview.entry_of(hstate[0], hs.name) if hstate else None)
                    if res == "back":
                        hs.mode, hs.note, hs.body = "list", "", None
                        out.write("\x1b[2J")
                    if res is not None:
                        continue
                asking = av is not None and bool(av.confirm)  # a question waits: any key answers it (y: yes), as it always did
                act = "" if asking else ui.action(scope, k, enabled)
                tgt = None  # the screen to go to
                if act == "screen":
                    tgt = dict(ui.screen_keys(enabled)).get(k)  # a screen that is off: nothing happens
                elif act in ("next", "prev"):
                    order = [n for _d, n in ui.screen_keys(enabled)]
                    tgt = order[(order.index(scope) + (1 if act == "next" else -1)) % len(order)] if scope in order else None
                elif act.startswith("open-"):  # the overview's letters m c h a
                    tgt = act[5:] if enabled(act[5:]) else None
                elif act == "help":
                    help_open = True
                elif act == "redraw":
                    out.write("\x1b[2J")
                    if not paused:  # new data too
                        fresh, cpu_fresh, ov_cache = 0.0, 0.0, None
                elif act == "pause":
                    if paused:
                        resume()
                        fresh, cpu_fresh, ov_cache = 0.0, 0.0, None
                    else:
                        paused, pause_t, pause_ov = True, time.time(), scope == "overview"
                        if pause_ov:  # this slide stays until the redraw goes on
                            pre_hold, held, hold_until = hold_until, idx, float("inf")
                elif act == "hosts":  # 6: the Hosts list, from any local screen (only while [hosts] is set and [features] fleet is on)
                    if hs is None:
                        hs = hostsview.HostsView(scope, (mv, cv, hv, av, ev), view.opened if view is not None else time.time())
                        resume()
                        mv = cv = hv = av = ev = None
                        cpu_d, hstate, fresh = None, None, 0.0
                        out.write("\x1b[2J")
                        break
                elif hs is not None:  # the list: Esc/q back to the local screen, Enter on a host opens its screens, on this machine goes back too
                    res = hostsview.hosts_key(hs, k, [hostsview.ME] + [r["id"] for r in hstate[2]] if hstate else [hostsview.ME])
                    if res in ("back", "local"):
                        close_hosts()
                        break
                    if res == "open":
                        out.write("\x1b[2J")
                elif scope == "overview":
                    if act == "back" and k == "q" and nuc_config.PORTABLE:  # run.sh in a terminal: q quits (on the monitor of an install it must not)
                        return 0
                    if act in ("slide-prev", "slide-next") and sl:  # held like a digit used to: HOLD_S
                        held = (idx + (1 if act == "slide-next" else -1)) % len(sl)
                        idx = held
                        hold_until = hold_until if paused else time.time() + HOLD_S
                        out.write("\x1b[2J")
                elif cv is not None:
                    res = cpu_key(cv, k, cpu_rows_, cv.page)
                    if res == "back":
                        tgt = "overview"
                    elif res == "rows" and cpu_d is not None:  # the next key of the same read moves on the new order
                        cpu_rows_ = cpu_rows(cpu_d["procs"]["procs"], cv.sort)
                        cpu_sync(cv, cpu_rows_)
                elif hv is not None:
                    res = health_key(hv, k, hl)
                    if res == "back":
                        tgt = "overview"
                    elif res == "period":  # asked again (the report of that period may be cached)
                        fresh = 0.0
                elif ev is not None:
                    res = events.events_key(ev, k, ed["rows"] if ed else [])
                    if res == "back":
                        tgt = "overview"
                    elif res == "filter":  # another period or kind: asked again
                        fresh = 0.0
                elif av is not None:
                    res = ai_key(av, k, al)
                    if res == "back":
                        tgt = "overview"
                    elif res:  # a key that acts: the engine does it in the background, the screen shows what it says
                        ai_do(av, res, al)
                        fresh = 0.0
                elif mv is not None:
                    res = map_key(mv, k, rs, max(1, map_layout(G, w, body_rows(h), mv.details)[1] - 1) if G else 10)
                    if res == "back":
                        tgt = "overview"
                    elif res == "rows" and G is not None:  # the next key of the same read moves on the new tree
                        rs = graph.rows(G, mv.st)
                        map_sync(mv, rs)
                if tgt == "overview" and scope == "overview" and sl:  # 1 on the overview: its first slide
                    held = next((i for i, x in enumerate(sl) if x[0] == "Overview"), 0)
                    idx = held
                    hold_until = hold_until if paused else time.time() + HOLD_S
                    out.write("\x1b[2J")
                elif tgt is not None and tgt != scope and (tgt == "overview" or avail.get(tgt)):
                    opened = view.opened if view is not None else time.time()  # the rotation stays paused all the time on the screens
                    resume()
                    mv = cv = hv = av = ev = hs = None
                    cpu_d = None
                    if tgt == "overview":
                        t0 += time.time() - opened  # the rotation was paused: it goes on where it was
                        ov_cache = None
                    elif tgt == "map":
                        mv, fresh, err = MapView(), 0.0, None
                    elif tgt == "cpu":
                        cv, cpu_fresh, rot_feed = CpuView(), 0.0, None
                    elif tgt == "health":
                        hv, fresh, err = HealthView(), 0.0, None
                    elif tgt == "events":
                        ev, fresh, err = events.EventsView(), 0.0, None
                    else:
                        av, fresh, err = AiView(), 0.0, None
                    nv = mv if mv is not None else cv if cv is not None else hv if hv is not None else av if av is not None else ev
                    if nv is not None:
                        nv.opened = nv.touched = opened
                    out.write("\x1b[2J")
                    break
    finally:
        PAUSED = False
        out.write("\x1b[?25h\x1b[0m")
        out.flush()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:  # a model server this screen started ends with it (nothing happens when it started none)
            import aiweb
            aiweb.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))  # --accept must be able to fail: install.sh and the user's script check the exit code
    except KeyboardInterrupt:  # Ctrl+C in a terminal (run.sh): the terminal is already restored, no traceback
        sys.exit(130)
    except PermissionError as e:  # --accept as a normal user: say what to do instead of a traceback
        print(f"permission denied: {e.filename or e}: run it as " + ("administrator" if WINDOWS else "root (sudo)"), file=sys.stderr)
        sys.exit(1)
