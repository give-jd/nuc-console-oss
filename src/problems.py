"""nuc-console problems: what is wrong with this machine, in words, and what was accepted as known.

`problems_raw` turns the readings (the network and container state, the boot state, the thermal figures, the port baseline) into
(severity, text, id) problems; `problems()` leaves out the ones the owner accepted (`accepted.json`, tied to the severity and the text) and
`ProblemList` carries the rest with what only the web has room for. `CATALOG` says what each id is, why it matters and how to fix it, in the
words of this OS (`CMD`, `ACCEPT_CMD`, `PROBLEMS_CMD`: the commands the advice names). The port baseline is read and written here too.
`status_pill` is the header's verdict. `render.py` owns the three that need a frame's worth of data (`current_problem_records`,
`accept_problem`, `print_problems`) and the Telegram switch (`TELEGRAM_ON`, which depends on --demo). Stdlib only."""
import json
import os
import re
import sys
import time
from fnmatch import fnmatchcase

import cards  # same directory: the card registry (a problem belongs to a card)
import history
import hostdata
import nuc_config
import ui
from exposure import (SENSITIVE, baseline_diff, expose_apply, expose_over, expose_over_items, expose_unmatched, exposure_keys,
                      exposure_partial, exposure_rows, group_of, os_of)
from ui import fmt_ago, plural, safe

WINDOWS, MACOS = nuc_config.WINDOWS, nuc_config.MACOS
CFG = nuc_config.current()
TELEGRAM_ON = lambda: False  # noqa: E731 - render.py replaces it: in the demo the notifier is on by config.ini alone, and --demo is render's flag

# the commands the advice on screen refers to, in the words of this OS
if WINDOWS:
    ACCEPT_CMD = "nuc-console-accept"  # from an administrator prompt
    CMD = {"restart": "Start-ScheduledTask -TaskPath \\nuc-console\\ -TaskName collector (administrator PowerShell)",
           "logs": r"%ProgramData%\nuc-console\logs\collector.log", "apply": "run install-windows.cmd again (it keeps config.ini)"}
elif MACOS:
    ACCEPT_CMD = "sudo nuc-console-accept"
    CMD = {"restart": "sudo launchctl kickstart -k system/com.nuc-console.collector", "logs": "/var/log/nuc-console/collector.log",
           "apply": "run sudo ./install.sh again (it keeps config.ini)"}
else:
    ACCEPT_CMD = "sudo nuc-console-accept"
    CMD = {"restart": "sudo systemctl restart nuc-console-collector", "logs": "journalctl -u nuc-console-collector",
           "apply": "sudo systemctl restart nuc-console nuc-console-collector nuc-console-web"}  # apply: config.ini, everything that reads it
PROBLEMS_CMD = "nuc-console-problems"
if nuc_config.PORTABLE:  # run.sh / run.cmd: no nuc-console-accept on the PATH, no service to restart: the advice says what exists
    ACCEPT_CMD = "run.cmd -Accept" if WINDOWS else "./run.sh --accept"
    PROBLEMS_CMD = "run.cmd -Problems" if WINDOWS else "./run.sh --problems"
    CMD = {"restart": "quit it (Ctrl+C) and start it again", "logs": os.path.join(nuc_config.BASE_DIR, "logs", "collector.log"),
           "apply": "quit the app (or run.sh / run.cmd) and start it again"}


def load_baseline(path=None):
    """valid dict | None if missing | 'corrotta' (corrupt) if it exists but is unreadable (not 'missing': must be flagged)."""
    path = path or hostdata.BASELINE
    if not os.path.exists(path):
        return None
    d = hostdata.load_json(path)
    return d if isinstance(d, dict) and isinstance(d.get("ports"), dict) else "corrotta"


def accept_baseline(if_missing=False, path=None, now=None):
    """Saves the current set of exposed ports as 'expected'. As root: sudo nuc-console-accept.

    Refuses a stale or partial state: it would bless as normal what could not be measured.
    """
    path, now = path or hostdata.BASELINE, now or time.time()
    if if_missing and isinstance(load_baseline(path), dict):
        print("baseline already present: left untouched")
        return 0
    net, cont = hostdata.load_json(hostdata.NET_STATE), hostdata.load_containers()
    if (not isinstance(net, dict) or now - net.get("ts", 0) > cards.NET_STALE_S or exposure_partial(net)
            or cont is None or now - cont.get("ts", 0) > hostdata.STALE_S):
        print("network/container state missing, stale or incomplete: baseline not created", file=sys.stderr)
        return 1
    cur = exposure_keys(net, cont)
    if cur is None:
        print("port list unavailable: baseline not created", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": now, "ports": cur}, f, indent=1)
        f.flush()
        os.fsync(f.fileno())  # a power cut must not leave an empty file
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    print(f"baseline written: {len(cur)} exposed ports in {path}")
    return 0


TELEGRAM_DOWN_S, TELEGRAM_FAILING_S = 300, 600  # notify.py rewrites status.json every 30 s: older than 5 min = not running; 10 min of failed sends = say so


TELEGRAM_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")  # a bot token must never reach the screen, whoever put it in a message


def telegram_status(path=None):
    """notify.py's status.json (counters and the last error, never a secret): a dict; None if missing or broken; False if this user may
    not read it (on Windows only administrators and the service accounts can open the notifier's folder, where the token is)."""
    try:
        with open(path or os.path.join(nuc_config.NOTIFY_DIR, "status.json"), encoding="utf-8") as f:
            d = json.load(f)
    except PermissionError:
        return False
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def telegram_state(now, path=None):
    """The notifier as status.json shows it: ("ok" | "unreadable" | "unpaired" | "down" | "failing", how long and why, for "failing")."""
    tg = telegram_status(path)
    if tg is False:
        return "unreadable", ""  # cannot look: neither a problem nor a proof that all is well
    if tg and tg.get("paired") is False:
        return "unpaired", ""  # its last word, even if it has stopped since (it exits at once when there is no chat)
    ts, since = (tg or {}).get("ts"), (tg or {}).get("failing_since")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or now - ts > TELEGRAM_DOWN_S:
        return "down", ""
    if not isinstance(since, bool) and isinstance(since, (int, float)) and now - since > TELEGRAM_FAILING_S:
        err = TELEGRAM_TOKEN.sub("<token>", safe(tg.get("last_error") or ""))[:60].strip()
        return "failing", fmt_ago(now - since) + (": " + err if err else "")
    return "ok", ""


def ssh_exposed(net, cont):
    """True when sshd listens where the exposure data says it is reachable from the Internet (a Funnel) or further than [expose] allows.
    No exposure data (net missing, no port list) is False: the rule then stays at the lower severity."""
    if not isinstance(net, dict) or net.get("listeners") is None:
        return False
    rows = exposure_rows(net, cont)
    if CFG["expose"]:
        rows = expose_apply(rows, net, cont)
    return any(r["proto"] == "tcp" and (r["port"] == 22 or r["name"] == "sshd") and (group_of(r) == "INTERNET" or expose_over(r)) for r in rows)


FP_NAMES = {}  # (problem id, text) -> every name behind a service-down / cert-* item (the text shows three): what accepting it is tied to


def _names(pid, text, names, out, sev):
    FP_NAMES[(pid, text)] = sorted(set(names))
    out.append((sev, text, pid))


def host_checks(boot, net, cont, now):
    """What boot.json's services, sshd and certs sections say, as (severity, text, id): a unit that must run and does not, the sshd settings
    that weaken a login, a certificate near its end. Names and counts only; a section the collector did not write (Windows/macOS, not
    configured, sshd absent) says nothing, unless the config asks for it on a system that has it (then watch-config says it did not run).
    A malformed value is skipped, never an error."""
    out, more = [], []
    FP_NAMES.clear()
    rows = [x for x in boot.get("services") or () if isinstance(x, dict) and isinstance(x.get("unit"), str)] if isinstance(boot.get("services"), list) else []
    skipped = [x for x in rows if x.get("state") == "not-checked"]
    down = [x for x in rows if x.get("state") not in ("active", "activating", "reloading", "not-checked")]
    if down:
        text = ("service not running: " + ", ".join(f"{safe(x['unit'])[:40]} ({safe(str(x.get('state')))[:12]})" for x in down[:3])
                + (f" … +{len(down) - 3}" if len(down) > 3 else ""))
        _names("service-down", text, [x["unit"] for x in down], out, 2)
    if skipped:
        more.append(plural(len(skipped), "unit") + " not checked this pass (time limit): " + ", ".join(safe(x["unit"])[:40] for x in skipped[:3]))
    local = boot.get("os") in (None, "linux")
    if (CFG.get("services") or {}).get("must_run") and local:  # asked for, and nothing came back: not "all fine"
        if "services" in (boot.get("errors") or {}):
            more.append("[services] must_run is set but the check failed")
        elif not isinstance(boot.get("services"), list):
            more.append("[services] must_run is set but systemctl is not available / the check did not run")
    ssh = boot.get("sshd") if isinstance(boot.get("sshd"), dict) else {}
    if ssh.get("permitrootlogin") == "yes":
        out.append((1, "sshd allows root login (PermitRootLogin yes)", "ssh-root-login"))
    if ssh.get("passwordauthentication") == "yes" or ssh.get("kbdinteractiveauthentication") == "yes":
        wide = ssh_exposed(net, cont)
        what = ("sshd accepts passwords (PasswordAuthentication yes)" if ssh.get("passwordauthentication") == "yes"
                else "sshd accepts keyboard-interactive/PAM password login (KbdInteractiveAuthentication yes)")
        out.append((2 if wide else 1, what + (", and port 22 reaches the Internet or beyond config.ini" if wide else ""), "ssh-password-auth"))
    if ssh.get("permitemptypasswords") == "yes":
        out.append((2, "sshd accepts empty passwords (PermitEmptyPasswords yes)", "ssh-empty-passwords"))
    warn = (CFG.get("tls") or {}).get("warn_days", 21)
    soon, gone, lost, unchecked = [], [], [], []
    certs = boot.get("certs")
    for c in certs if isinstance(certs, list) else ():
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            continue
        exp, name = c.get("exp"), safe(c["name"])[:32]
        if c.get("why") == "not-checked":
            unchecked.append(name)
        elif isinstance(exp, bool) or not isinstance(exp, (int, float)):
            lost.append(name)
        elif exp <= now:
            gone.append((name, f"{name} ({int((now - exp) // 86400)} d ago)"))
        elif int((exp - now) // 86400) < warn:
            soon.append((name, f"{name} ({int((exp - now) // 86400)} d)"))
    for pid, sev, head, items in (("cert-expired", 2, "TLS certificate expired: ", gone), ("cert-expiring", 1, "TLS certificate expires soon: ", soon),
                                  ("cert-unreadable", 1, "TLS certificate not read: ", [(n, n) for n in lost])):
        if items:
            _names(pid, head + ", ".join(t for _, t in items[:3]) + (f" … +{len(items) - 3}" if len(items) > 3 else ""), [n for n, _ in items], out, sev)
    if unchecked:
        more.append(plural(len(unchecked), "certificate") + " not checked this pass (time limit): " + ", ".join(unchecked[:3]))
    if (CFG.get("tls") or {}).get("watch"):
        if "certs" in (boot.get("errors") or {}):
            more.append("[tls] watch is set but the check failed")
        elif not isinstance(certs, list):
            more.append("[tls] watch is set but the check did not run")
    for m in more:
        out.append((1, m, "watch-config"))
    return out


INODE_WARN, INODE_ERR = 90.0, 97.0  # % of a filesystem's inodes used


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def hygiene_problems(boot, now):
    """What boot.json's host-hygiene sections say (collector.collect_boot): [(severity, text, id)]. A section that is missing, broken or odd
    (absent tool, unsupported OS, [backups] empty) says nothing: it is neither a problem nor a proof that all is well."""
    out = []
    bk = [b for b in boot.get("backups") or () if isinstance(b, dict) and isinstance(b.get("name"), str)] if isinstance(boot.get("backups"), list) else []
    gone = [b for b in bk if not b.get("matches") or num(b.get("newest")) is None]
    old = []
    for b in bk:
        if b in gone:
            continue
        age = (now - b["newest"]) / 86400
        if age > (num(b.get("max_age_days")) or 2):
            old.append((safe(b["name"])[:30], int(age)))
    if gone:
        out.append((2, plural(len(gone), "backup") + " found nothing: " + ", ".join(safe(b["name"])[:30] for b in gone[:3]), "backup-missing"))
    if old:
        out.append((1, plural(len(old), "backup") + " older than allowed: " + ", ".join(f"{n} ({d} d)" for n, d in old[:3]), "backup-stale"))
    rb = boot.get("reboot")
    if isinstance(rb, dict):
        if rb.get("flag") is True:
            out.append((1, "reboot required: the system asked for one (/run/reboot-required)", "reboot-required"))
        elif rb.get("kernel_old") is True and isinstance(rb.get("running"), str) and isinstance(rb.get("newest"), str):
            out.append((1, f"reboot required: running kernel {safe(rb['running'])[:40]}, {safe(rb['newest'])[:40]} installed", "reboot-required"))
    up = boot.get("updates")
    if isinstance(up, dict) and isinstance(num(up.get("security")), int) and up["security"] > 0:
        out.append((1, plural(up["security"], "security update") + " pending" + (f" ({num(up.get('total'))} upgrades in all)" if num(up.get("total")) else ""), "updates-pending"))
    ck = boot.get("clock")
    if isinstance(ck, dict) and ck.get("synced") is False:
        out.append((1, "clock not synchronised (NTP service " + ("on" if ck.get("ntp") else "off") + ")", "clock-unsynced"))
    ino = [(num(r.get("pct")), safe(r.get("mount"))[:30]) for r in boot.get("inodes") or () if isinstance(r, dict) and isinstance(r.get("mount"), str)] \
        if isinstance(boot.get("inodes"), list) else []
    ino = sorted(((p, m) for p, m in ino if p is not None and p >= INODE_WARN), reverse=True)
    if ino:
        out.append((2 if ino[0][0] >= INODE_ERR else 1, "inodes nearly used up: " + ", ".join(f"{m} ({p:.0f}%)" for p, m in ino[:3]), "disk-inodes"))
    return out


NOT_BACK_WINDOW_S = 1200  # containers-not-back looks at the first 20 minutes after a boot, then it is just a stopped container


def restart_problems(boot, now):
    """The two restart-policy alarms, from the boot collector's `containers` (the RUNNING ones, with their policy) and `containers_before`
    (the names that ran with a policy before the last reboot; collector.remember_running). Names only; an absent or unreadable
    section gives nothing: unknown is not an alarm here, and not a green either."""
    cs, out = boot.get("containers"), []
    if not isinstance(cs, list) or not hostdata.on("containers"):
        return out
    ignore = (CFG.get("containers") or {}).get("restart_ignore") or []
    cs = [c for c in cs if isinstance(c, dict) and isinstance(c.get("name"), str)]
    no = [c["name"] for c in cs if (c.get("restart") or "no") == "no" and not any(fnmatchcase(c["name"].lower(), g) for g in ignore)]
    if no:
        out.append((1, plural(len(no), "running container") + " will not restart after a reboot: " + ", ".join(safe(n) for n in no[:3])
                    + (" …" if len(no) > 3 else ""), "container-no-restart"))
    btime = boot.get("btime")
    if isinstance(btime, (int, float)) and not isinstance(btime, bool) and 0 <= now - btime < NOT_BACK_WINDOW_S:
        up = {c["name"] for c in cs}
        gone = [n for n in boot.get("containers_before") or () if isinstance(n, str) and n not in up]
        if gone:
            out.append((2, plural(len(gone), "container") + " running before the reboot did not come back: "
                        + ", ".join(safe(n) for n in gone[:3]) + (" …" if len(gone) > 3 else ""), "containers-not-back"))
    return out


def kernel_problems(k, now):
    """boot['kernlog'] (collector.parse_kernlog: {class: {n, last, ...}}; absent where the OS has no kernel log) -> problems. A class
    leaves the list when its newest line is older than its window (history.KERNEL_WINDOW_S); the OOM killer stays for the boot."""
    out = []
    if not isinstance(k, dict):
        return out

    def get(cls):
        e = k.get(cls)
        if not isinstance(e, dict) or not isinstance(e.get("n"), int) or isinstance(e.get("n"), bool) or e["n"] < 1:
            return None
        last = e.get("last")
        win = history.KERNEL_WINDOW_S.get(cls)
        if not isinstance(last, (int, float)) or isinstance(last, bool) or (win is not None and now - last > win):
            return None
        return True
    # no age in the texts ("3 min" -> "1 h" would be a new problem to the fingerprint) and, for the two that cannot be accepted, no count
    # either (notify.py keys those by their text): the count and the age of the newest line are in boot.json's kernlog
    g = k.get("display") if get("display") else None
    if g:
        out.append((2, f"{safe(g.get('driver') or 'gpu')[:16]} graphics driver stuck: {plural(g['n'], 'error')} in the kernel log", "display-stuck"))
    if get("disk"):
        out.append((2, "disk I/O errors in the kernel log (last 24 h)", "disk-io-errors"))
    if get("memory"):
        out.append((2, "memory (ECC/MCE) errors in the kernel log (last 24 h)", "memory-errors"))
    g = k.get("nic") if get("nic") else None
    if g and g["n"] >= history.NIC_MIN:
        out.append((1, f"network adapter reset or link down {g['n']} times in 24 h", "nic-resets"))
    g = k.get("oom") if get("oom") else None
    if g:
        names = ", ".join(safe(p)[:30] for p in (g.get("procs") or [])[:3] if isinstance(p, str))
        out.append((1, f"kernel killed {g['n']} process{'' if g['n'] == 1 else 'es'} for memory this boot" + (f" ({names})" if names else ""), "oom-killed"))
    return out


def problems_raw(net, cont, now=None, boot=False, thermal=None, baseline=False):
    """Every anomaly, by decreasing severity: [(3=port change | 2=error | 1=warning, text, problem id)]. Ids are stable."""
    now, out = now or time.time(), []
    hidden = [f for f in nuc_config.SECURITY_FEATURES if "features." + f in (CFG.get("overlaid") or {}) and not CFG["features"].get(f, True)]
    if hidden:  # a person at the settings page switched a security section off: config.ini has it on, and nothing may hide that
        out.append((2, "%s switched off from the settings page (config.ini has it on)" % ", ".join(hidden), "feature-hidden"))
    if CFG.get("config_error"):  # config.ini exists but could not be read: nothing in it ([expose], [webapps]) is applied
        out.append((2, "config.ini unreadable: defaults in use ([expose] and [webapps] not applied)", "config-unreadable"))
    bad = (CFG.get("alerts") or {}).get("ignored") or []
    if bad:  # [alerts] mute named something that cannot be muted: said on the screen too, never accepted in silence
        out.append((1, "[alerts] mute: " + ", ".join(f"'{safe(x)[:40]}'" for x in bad[:3]) + (" … " if len(bad) > 3 else " ")
                    + "cannot be muted: ignored", "mute-ignored"))
    bad = CFG.get("watch_warnings") or []
    if bad:  # [services] / [tls] asked for something that is not done (a bad unit name, a public host): the collector skips it, and the screen says so
        out.append((1, "config.ini [services]/[tls]: " + "; ".join(safe(x)[:60] for x in bad[:2]) + (f" … +{len(bad) - 2}" if len(bad) > 2 else ""), "watch-config"))
    if not hostdata.on("containers"):
        pass
    elif cont is None:
        out.append((2, "container collector not running", "collector-containers"))
    else:
        if now - cont.get("ts", 0) > hostdata.STALE_S:
            out.append((1, f"container state stale ({int(now - cont.get('ts', 0))} s old)", "stale-containers"))
        down = [ct for ct in cont["containers"] if ct["state"] != "running"]
        sick = [ct for ct in cont["containers"] if "unhealthy" in ct["status"] or "Restarting" in ct["status"]]
        if sick:
            out.append((2, plural(len(sick), "unhealthy container"), "unhealthy-container"))
        if down:
            out.append((1, plural(len(down), "container") + " exited with an error", "container-exited"))
    if thermal and hostdata.on("thermal"):
        for label, key in (("CPU", "cpu"), ("NVMe", "nvme")):
            if key in thermal:
                t, mx = thermal[key]
                if t >= ui.THERMAL_ERR * mx:
                    out.append((2, f"{label} at {t:.0f}°C: above the {ui.THERMAL_ERR * mx:.0f}°C threshold", "thermal"))
                elif t >= ui.THERMAL_WARN * mx:
                    out.append((1, f"{label} at {t:.0f}°C: above the {ui.THERMAL_WARN * mx:.0f}°C threshold", "thermal"))
        if thermal.get("recent"):
            out.append((1, f"CPU thermal throttling: {thermal['recent']} events in the last minute", "throttling"))
        psi, limit = thermal.get("psi"), (CFG.get("thresholds") or {}).get("memory_pressure_pct", 10)
        if isinstance(psi, (int, float)) and not isinstance(psi, bool) and psi > limit:  # no PSI on this OS/kernel: None, no problem
            out.append((1, f"memory pressure {psi:.1f} % (processes waited for memory; the limit is {limit} %)", "memory-pressure"))
    if not hostdata.on("boot"):
        pass
    elif boot is None:
        out.append((1, "boot collector not running", "collector-boot"))
    elif boot:
        lbl = cards.boot_labels(boot)
        if boot.get("failed"):
            out.append((2, plural(len(boot["failed"]), lbl["failed_one"]) + ": " + ", ".join(safe(u) for u in boot["failed"][:3]), "failed-units"))
        if boot.get("journal") and boot["journal"]["err"]:
            out.append((1, plural(boot["journal"]["err"], "error") + lbl["journal_in"], "journal-errors"))
        out.extend(host_checks(boot, net, cont, now))
        out.extend(hygiene_problems(boot, now))
        out += restart_problems(boot, now)
        out.extend(kernel_problems(boot.get("kernlog"), now))
    if net is None:
        out.append((2, "network collector not running", "collector-net"))
        return sorted(out, key=lambda x: -x[0])
    if now - net.get("ts", 0) > cards.NET_STALE_S:
        out.append((1, f"network data stale ({int(now - net.get('ts', 0))} s old)", "stale-net"))
    if net.get("errors"):
        out.append((1, "network sections not collected: " + ", ".join(net["errors"]), "net-sections"))
    ufw, fw = net.get("ufw"), net.get("firewall")
    if os_of(net) != "linux":  # the OS firewall (Windows Firewall, macOS Application Firewall) instead of ufw
        name = (fw or {}).get("name") or "firewall"
        if fw is None and cards.is_disabled(net, "firewall"):
            pass
        elif fw is None:
            out.append((2, "firewall state unreadable: LAN exposure unknown", "firewall-unreadable"))
        else:
            if fw.get("off"):
                where = f" on the {', '.join(fw['off'])} network" if fw.get("kind") == "windows" else ""
                out.append((2, f"{name} off{where}: listening services are reachable from the LAN", "firewall-off"))
            if fw.get("policy"):
                out.append((1, f"{name} has rules from Group Policy: they are not read", "firewall-policy"))
    elif ufw is None and cards.is_disabled(net, "ufw"):
        pass  # firewall switched off in config.ini: the user's choice, not an alarm
    elif ufw is None and cards.is_absent(net, "ufw"):
        out.append((1, "ufw not installed: LAN filtering cannot be verified", "ufw-missing"))
    elif ufw is None:
        out.append((2, "ufw unreadable", "ufw-unreadable"))
    elif not ufw["active"]:
        out.append((2, "ufw off: no LAN filtering for non-Docker services", "ufw-off"))
    if net.get("listeners") is not None and baseline == "corrotta":
        out.append((2, f"port baseline unreadable: regenerate it with {ACCEPT_CMD}", "baseline-unreadable"))
    elif net.get("listeners") is not None and baseline is None:
        out.append((1, f"port baseline missing: create it with {ACCEPT_CMD}", "baseline-missing"))
    elif isinstance(baseline, dict) and net.get("listeners") is not None:
        if exposure_partial(net):  # partial data: a comparison would raise false alarms (or hide real ones)
            out.append((1, "port comparison suspended: network sections unreadable", "port-compare-suspended"))
        else:
            new, gone, changed = baseline_diff(exposure_keys(net, cont) or {}, baseline)
            for k, v in list(new.items())[:3]:
                port, _, group = k.partition(":")
                out.append((3, f"NEW exposed port: {port} {group.lower()} ({safe(v['name'])[:24]})", "port-new"))
            if len(new) > 3:
                out.append((3, f"… and {len(new) - 3} more new exposed ports", "port-new"))
            for k, why in list(changed.items())[:3]:
                out.append((3, f"CHANGED {k.partition(':')[0]}: {why}", "port-changed"))
            if gone:
                out.append((1, plural(len(gone), "port") + f" no longer exposed: if intended, {ACCEPT_CMD}", "port-gone"))
    if net.get("listeners") is not None:
        rows = exposure_rows(net, cont)
        pub = [r for r in rows if r["net"] == 1]
        expected = {p for ports in CFG["webapps"].values() for p in ports if p not in SENSITIVE}  # declared web apps: intended exposure
        bypass = [r for r in rows if r["bad_note"] and r["note"].startswith("docker") and r["lan"] == 1
                  and not (r["proto"] == "tcp" and r["port"] in expected)]
        dbs = [r for r in rows if r["warn"]]
        if dbs:
            out.append((2, f"{len(dbs)} DB/broker open on LAN", "db-open-lan"))
        if bypass:
            out.append((1, plural(len(bypass), "Docker port") + " bypassing ufw (DOCKER-USER empty)", "docker-bypass"))
        if pub:
            out.append((1, plural(len(pub), "service") + f" public on the Internet (Funnel :{pub[0]['port']})", "funnel-public"))
        if CFG["expose"]:  # [expose] only adds alarms: the ones above stay as they are, declared or not
            over = expose_over_items(expose_apply(rows, net, cont))
            if over:
                out.append((2, plural(len(over), "service") + (" reaches" if len(over) == 1 else " reach") + " beyond config.ini: " + ", ".join(over), "over-exposed"))
            ok = (isinstance(cont, dict) and not any(cont.get(k) for k in ("error", "absent", "disabled")) and isinstance(boot, dict)
                  and not {"links", "dbs"} & set(net.get("errors") or ()))  # with a source of names missing every name looks wrong
            lost = expose_unmatched(net, cont, boot) if ok else []
            if lost:
                out.append((1, "[expose] " + ", ".join(f"'{safe(k)}'" for k in lost) + (" matches" if len(lost) == 1 else " match") + " no service", "expose-unmatched"))
    if TELEGRAM_ON():  # notify.py (docs/TELEGRAM.md): only then its status.json is read; switched off = nothing to say
        state, why = telegram_state(now)
        if state == "unpaired":
            out.append((1, "Telegram notifications on, but not paired", "telegram-unpaired"))
        elif state == "down":
            out.append((1, "Telegram notifier not running", "telegram-failing"))
        elif state == "failing":
            out.append((1, f"Telegram notifications failing for {why}", "telegram-failing"))
    return sorted(out, key=lambda x: -x[0])


ACCEPTED_PATH = os.environ.get("NUC_CONSOLE_ACCEPTED", os.path.join(nuc_config.LIB_DIR, "accepted.json"))

# id -> (what it is, why it matters, how to handle it). Shown by `nuc-console-problems`.
CATALOG = {
    "collector-containers": ("Container collector not running", "no container data", "sudo systemctl status nuc-console-collector; journalctl -u nuc-console-collector"),
    "collector-net": ("Network collector not running", "no exposure/firewall data", "sudo systemctl restart nuc-console-collector"),
    "collector-boot": ("Boot collector not running", "no boot data", "sudo systemctl restart nuc-console-collector"),
    "stale-containers": ("Container state is old", "the collector stopped updating", "sudo systemctl restart nuc-console-collector"),
    "stale-net": ("Network data is old", "the collector stopped updating", "sudo systemctl restart nuc-console-collector"),
    "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete", "journalctl -u nuc-console-collector; the section name is in the message"),
    "unhealthy-container": ("Container unhealthy or restarting", "the service may be down or degraded", "docker ps; docker logs <name>; fix the healthcheck or the app. A wrong healthcheck path is the usual cause"),
    "container-no-restart": ("Running container will not restart after a reboot", "its restart policy is no: after a reboot or a Docker restart it stays down and nothing says so",
                             "docker update --restart unless-stopped <name> (compose: restart: unless-stopped); a one-shot or test container on purpose: list it in [containers] restart_ignore"),
    "containers-not-back": ("Containers did not come back after the reboot", "they were running before it with a restart policy and are not running now: a service is down",
                            "docker ps -a; docker logs <name>; docker start <name>; if you stopped one on purpose, it clears 20 minutes after the boot"),
    "container-exited": ("Container exited with an error", "a service that should run is down", "docker ps -a; docker logs <name>; docker start <name> or remove it if obsolete"),
    "thermal": ("Temperature above the threshold", "throttling and hardware wear", "check airflow/dust; sensors; reduce load"),
    "throttling": ("CPU thermal throttling", "the CPU is slowing itself down", "check cooling; look at the thermal bars"),
    "failed-units": ("Failed systemd units", "a service that should run is down", "systemctl --failed; journalctl -u <unit>; systemctl reset-failed once handled"),
    "service-down": ("A service that must run is not running", "failed-units only sees units that FAILED: one that was stopped, never started or does not exist (fail2ban, sshd) is invisible there; "
                     "[services] must_run names the ones you expect",
                     "systemctl status <unit>; journalctl -u <unit>; sudo systemctl enable --now <unit>, or take it off [services] must_run if it is no longer expected"),
    "ssh-root-login": ("sshd allows root to log in", "one password or key guessed or leaked is a full takeover, with no user name to find first",
                       "PermitRootLogin prohibit-password (or no) in /etc/ssh/sshd_config, then sudo systemctl reload ssh; or accept it with a reason if it is intended"),
    "ssh-password-auth": ("sshd accepts passwords", "passwords can be guessed, and the Internet tries them all day: keys cannot be; with port 22 reachable from the Internet or beyond [expose] it is an error",
                          "PasswordAuthentication no in /etc/ssh/sshd_config once your key works, then sudo systemctl reload ssh (sshd -T shows the global values: a Match block overrides them for its users or addresses, so read those too; KbdInteractiveAuthentication no closes the PAM password route); or accept it with a reason if the LAN is trusted"),
    "ssh-empty-passwords": ("sshd accepts empty passwords", "an account with no password can log in over SSH",
                            "PermitEmptyPasswords no in /etc/ssh/sshd_config, then sudo systemctl reload ssh; passwd -l <user> for an account that should not log in"),
    "cert-expiring": ("A TLS certificate is about to expire", "when it does, browsers and clients refuse the service",
                      "renew it (your ACME client, your CA, or the device's own page), then restart or reload the service that serves it; [tls] warn_days sets how early this speaks"),
    "cert-expired": ("A TLS certificate has expired", "browsers and clients already refuse the service",
                     "renew it now (your ACME client, your CA, or the device's own page), then restart or reload the service that serves it"),
    "cert-unreadable": ("A watched TLS certificate could not be read", "its expiry is unknown, so nothing is being watched there",
                        "check the host:port answers TLS (or the file is a PEM certificate, at most 64 KB) from this machine; fix or remove the entry under [tls] watch"),
    "watch-config": ("[services] or [tls] in config.ini asks for something that is not done", "a unit name that is not valid, a host that is not on a private network (nuc-console connects nowhere else) or a duplicate is skipped",
                     "fix the line under [services] / [tls] in config.ini (the message says which); restart the services after fixing it"),
    "backup-stale": ("A backup is older than allowed", "the newest copy is old: the job that makes it may have stopped, and a restore would lose that much",
                     "look at the job that writes it (cron, systemd timer, backup tool) and its log; the limit is the number after the path under [backups] in config.ini"),
    "backup-missing": ("A backup was not found", "nothing matches the path under [backups]: the backup never ran, the disk is not mounted, or the path is wrong",
                       "ls the path; mount the backup disk; fix the path in config.ini (absolute, no ~) and restart nuc-console-collector"),
    "reboot-required": ("Reboot required", "a kernel or library update is installed but not running yet: the old, possibly vulnerable one still is",
                        "reboot when it suits you (sudo reboot); cat /run/reboot-required.pkgs lists the packages"),
    "updates-pending": ("Security updates pending", "known vulnerabilities are fixed in packages that are not installed yet",
                        "sudo apt update && apt list --upgradable; sudo apt upgrade (or enable unattended-upgrades)"),
    "clock-unsynced": ("Clock not synchronised", "logs, certificates, tokens and backups depend on the time being right",
                       "timedatectl status; sudo timedatectl set-ntp true; check chrony or systemd-timesyncd and the network"),
    "disk-inodes": ("Inodes nearly used up", "no new file can be created when they run out, though the disk shows free space",
                    "df -i; find the folder with millions of small files (sessions, caches, mail queues, container layers) and clean it"),
    "journal-errors": ("Errors in this boot's journal", "usually noise (docker veth races, firmware ACPI), sometimes a real fault", "journalctl -b -p err -o short | sort | uniq -c | sort -rn | head; accept it if it is known noise"),
    "ufw-off": ("ufw is off", "no filtering for non-Docker services on the LAN", "sudo scripts/enable-ufw.sh (LAN=<your subnet>) - keeps a rollback timer"),
    "ufw-missing": ("ufw not installed", "LAN filtering cannot be verified", "install ufw, or accept this if you use nftables/firewalld"),
    "ufw-unreadable": ("ufw status unreadable", "firewall state unknown", "sudo ufw status verbose; journalctl -u nuc-console-collector"),
    "docker-bypass": ("Docker ports bypass ufw", "a port published on 0.0.0.0 is reachable from the LAN whatever ufw says", "publish on 127.0.0.1 (compose: \"127.0.0.1:PORT:PORT\") or declare the app under [webapps] in config.ini if the exposure is intended; or add a DOCKER-USER rule"),
    "db-open-lan": ("Database/broker open on the LAN", "data services should not be reachable from the network", "publish the DB on 127.0.0.1 (scripts/rebind-all-dbs.sh) or stop it if unused"),
    "funnel-public": ("Service public on the Internet (Tailscale Funnel)", "anyone on the Internet can reach it", "tailscale funnel status; turn it off if not needed: tailscale funnel --https=PORT off"),
    "over-exposed": ("Service reaches further than config.ini says", "you declared under [expose] how far it should be reachable, and it is reachable from more places",
                     "bind it to 127.0.0.1 (or to the interface you meant), close the port in the firewall, or turn the Funnel off; if the wider reach is intended, say so under [expose] in config.ini"),
    "expose-unmatched": ("[expose] name matches no service", "a name that matches nothing (a typo, or a service that was removed) guards nothing",
                         "fix the name under [expose] in config.ini (container, compose service or project, process, unit, database, [webapps] name) or remove the line; ports are never checked"),
    "feature-hidden": ("A section was switched off from the settings page", "exposure, firewall, fail2ban, databases or tailscale is collected and alarmed no more, so a change there would pass unseen; config.ini has it on and the page's choice is kept in settings.ini",
                       "Reset to config.ini on the settings page, or delete settings.ini (the AI folder); set [web] settings_actions = no to stop the page from doing it"),
    "config-unreadable": ("config.ini unreadable", "the file exists but could not be read, so the defaults are in use: [expose] and [webapps] are not applied and nothing is checked against them",
                          "check config.ini for a key starting with ':' (write ports as 8080) or a section without a header; the exact error is in the service logs / stderr; restart the services after fixing it"),
    "mute-ignored": ("[alerts] mute names an alarm that cannot be muted", "the alarm stays on: security alarms and unknown names are never muted, whatever the file says",
                     "remove the name from [alerts] mute in config.ini; the mutable ones are listed in docs/CONFIGURATION.md; restart the services after fixing it"),
    "baseline-missing": ("Port baseline missing", "new ports cannot be detected", "sudo nuc-console-accept"),
    "baseline-unreadable": ("Port baseline unreadable", "new ports cannot be detected", "sudo nuc-console-accept"),
    "port-compare-suspended": ("Port comparison suspended", "network sections were unreadable", "see net-sections"),
    "port-new": ("New exposed port", "something started listening where it did not before", "identify it (ss -ltnp); if intended: sudo nuc-console-accept; if not, stop it"),
    "port-changed": ("Exposed port changed", "a different service or a weaker filter on a known port", "check what changed; if intended: sudo nuc-console-accept"),
    "port-gone": ("Port no longer exposed", "a service you expected is gone", "if intended: sudo nuc-console-accept"),
}


CATALOG.update({  # the kernel's own log (Linux): graphics, disk, memory, network adapter, the OOM killer; memory pressure (PSI)
    "display-stuck": ("Graphics driver stuck", "the kernel's graphics driver stopped answering: the monitor can stay black while every service looks fine",
                      "do not replug the HDMI/DisplayPort cable and do not force a connector 'detect' (that can trigger the hang); press a key or switch "
                      "console (Ctrl+Alt+F2, then back), else reboot (sudo systemctl reboot); if it comes back, see the driver's vendor bug tracker for a kernel parameter"),
    "disk-io-errors": ("Disk I/O errors in the kernel log", "a failing disk, cable or filesystem: data may be unreadable or lost",
                       "journalctl -k -p err | grep -iE 'nvme|ata|I/O error|EXT4-fs|XFS|BTRFS'; sudo smartctl -a /dev/<disk>; back up now; check the cable; replace the disk it names"),
    "memory-errors": ("Memory (ECC/MCE) errors in the kernel log", "failing RAM or CPU: crashes and silent data corruption",
                      "journalctl -k | grep -iE 'edac|mce|hardware error'; memtest86+ (a full pass, from boot); reseat or replace the module it names"),
    "nic-resets": ("Network adapter resets or link drops", "an unstable cable, switch port, driver or power setting: connections drop",
                   "journalctl -k | grep -iE 'reset adapter|link is down'; try another cable or port; check the switch; ethtool -S <interface>; a driver or firmware update"),
    "oom-killed": ("The kernel killed a process for memory", "the machine ran out of memory this boot: a service died without a word",
                   "journalctl -k | grep -i 'killed process'; free -h; find the app that grew (the HEALTH screen); give it a limit it respects or add RAM"),
    "memory-pressure": ("Memory pressure", "processes are waiting for memory (PSI): the machine is slow and heading for the OOM killer",
                        "cat /proc/pressure/memory; free -h; ps aux --sort=-rss | head; stop or limit the biggest app; add RAM or swap. The limit is [thresholds] memory_pressure_pct"),
})
CATALOG.update({  # macOS/Windows: the OS firewall in place of ufw (the advice is per OS, below)
    "firewall-off": ("Firewall off", "every listening service is reachable from the network", "turn the firewall on"),
    "firewall-unreadable": ("Firewall state unreadable", "LAN exposure cannot be judged: ports are shown as unknown", f"{CMD['logs']}"),
    "firewall-policy": ("Firewall rules from Group Policy", "rules set by policy are not in the local store: those ports are shown as unknown",
                        "Get-NetFirewallRule -PolicyStore ActiveStore lists the effective rules"),
})
# the same problems, explained with the commands of this OS
OS_CATALOG = {
    "windows": {
        "collector-containers": ("Container collector not running", "no container data",
                                 r"Get-ScheduledTask -TaskPath \nuc-console\ ; log: " + CMD["logs"]),
        "collector-net": ("Network collector not running", "no exposure/firewall data", CMD["restart"]),
        "collector-boot": ("Boot collector not running", "no boot data", CMD["restart"]),
        "stale-containers": ("Container state is old", "the collector stopped updating", CMD["restart"]),
        "stale-net": ("Network data is old", "the collector stopped updating", CMD["restart"]),
        "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete",
                         CMD["logs"] + "; the section name is in the message"),
        "failed-units": ("Automatic services stopped with an error", "a service that should run is down",
                         "Get-Service <name>; Event Viewer > Windows Logs > System says why; Start-Service <name>"),
        "journal-errors": ("Errors in the System event log since boot", "usually noise (DCOM permissions, drivers), sometimes a real fault",
                           "Event Viewer > Windows Logs > System; accept it if it is known noise"),
        "db-open-lan": ("Database/broker open on the LAN", "data services should not be reachable from the network",
                        "publish the DB on 127.0.0.1 (compose: \"127.0.0.1:5432:5432\") or stop it if unused"),
        "firewall-off": ("Windows Firewall off", "every listening service is reachable from that network",
                         "Windows Security > Firewall & network protection: turn it on; or, as administrator: Set-NetFirewallProfile -All -Enabled True"),
        "baseline-missing": ("Port baseline missing", "new ports cannot be detected", "nuc-console-accept (administrator prompt)"),
        "baseline-unreadable": ("Port baseline unreadable", "new ports cannot be detected", "nuc-console-accept (administrator prompt)"),
        "port-new": ("New exposed port", "something started listening where it did not before",
                     "identify it (Get-NetTCPConnection -State Listen); if intended: nuc-console-accept (administrator); if not, stop it"),
        "port-changed": ("Exposed port changed", "a different service or a weaker filter on a known port",
                         "check what changed; if intended: nuc-console-accept (administrator)"),
        "port-gone": ("Port no longer exposed", "a service you expected is gone", "if intended: nuc-console-accept (administrator)"),
    },
    "darwin": {
        "collector-containers": ("Container collector not running", "no container data",
                                 "sudo launchctl print system/com.nuc-console.collector; log: " + CMD["logs"]),
        "collector-net": ("Network collector not running", "no exposure/firewall data", CMD["restart"]),
        "collector-boot": ("Boot collector not running", "no boot data", CMD["restart"]),
        "stale-containers": ("Container state is old", "the collector stopped updating", CMD["restart"]),
        "stale-net": ("Network data is old", "the collector stopped updating", CMD["restart"]),
        "net-sections": ("Some network sections could not be collected", "the exposure picture may be incomplete",
                         CMD["logs"] + "; the section name is in the message"),
        "failed-units": ("Launch daemons that exited with an error", "a service that should run is down",
                         "sudo launchctl print system/<label>; its log is named in the plist (StandardErrorPath)"),
        "firewall-off": ("macOS firewall off", "every listening service is reachable from the network",
                         "System Settings > Network > Firewall: turn it on (or: sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate on)"),
        "port-new": ("New exposed port", "something started listening where it did not before",
                     "identify it (sudo lsof -nP -iTCP -sTCP:LISTEN); if intended: sudo nuc-console-accept; if not, stop it"),
    },
}
CATALOG.update({  # notify.py (Telegram, docs/TELEGRAM.md); only when it is on (config.ini, or the web view's Telegram page)
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "the web view's Telegram page (settings), or sudo nuc-console-telegram --setup (the bot token from @BotFather, your @username), "
                          "then tap the link it shows and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "sudo nuc-console-telegram --status; sudo nuc-console-telegram --test; journalctl -u nuc-console-notify; "
                         "sudo systemctl restart nuc-console-notify"),
})
OS_CATALOG["windows"].update({
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "the web view's Telegram page (settings), or nuc-console-telegram.cmd --setup (administrator prompt: the bot token from "
                          "@BotFather, your @username), then tap the link it shows and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "nuc-console-telegram.cmd --status; nuc-console-telegram.cmd --test; log: %ProgramData%\\nuc-console\\logs\\notify.log; "
                         "restart: nuc-console-telegram.cmd --on (administrator prompt)"),
})
OS_CATALOG["darwin"].update({
    "telegram-unpaired": ("Telegram notifications on, but not paired", "no alert can reach your phone: this machine does not know your chat",
                          "the web view's Telegram page (settings), or sudo nuc-console-telegram --setup (the bot token from @BotFather, your @username), "
                          "then tap the link it shows and press Start"),
    "telegram-failing": ("Telegram notifier not running or failing", "new problems are not reaching your phone",
                         "sudo nuc-console-telegram --status; sudo nuc-console-telegram --test; log: /var/log/nuc-console/notify.log; "
                         "restart: sudo launchctl kickstart -k system/com.nuc-console.notify"),
})
BASE_CATALOG = dict(CATALOG)  # the advice in the words of an installed Linux, before this OS's and the portable run's (tests/golden.py renders with it)
CATALOG.update(OS_CATALOG.get(nuc_config.OS_NAME, {}))
if nuc_config.PORTABLE:
    CATALOG = {k: (t, w, re.sub(r"(?:sudo )?nuc-console-accept(?: \(administrator prompt\))?", ACCEPT_CMD, a)) for k, (t, w, a) in CATALOG.items()}
    # the collector is part of the run, not a service: no systemctl, launchctl or scheduled task, and its log is a file
    _again = CMD["restart"] + "; log: " + CMD["logs"]
    CATALOG.update({k: (CATALOG[k][0], CATALOG[k][1], a) for k, a in {
        "collector-containers": _again, "collector-net": _again, "collector-boot": _again,
        "stale-containers": _again, "stale-net": _again,
        "net-sections": CMD["logs"] + "; the section name is in the message",
        "ufw-unreadable": "sudo ufw status verbose; log: " + CMD["logs"],
        # the Telegram notifier starts with the run (the desktop app too): the web view's Telegram page pairs and tests it, its log is a file
        "telegram-unpaired": "pair it on the web view's Telegram page (settings > Phone alerts), or [telegram] enabled = no",
        "telegram-failing": "the web view's Telegram page says why and sends a test (settings > Phone alerts); log: "
                            + os.path.join(nuc_config.BASE_DIR, "logs", "notify.log"),
    }.items()})


MUTED_HINT = "[alerts] mute in config.ini"  # where a muted alarm is switched back on (mute is not accept: no reason, no root, no accepted.json)
NOT_ACCEPTABLE = {"port-new", "port-changed", "port-gone", "feature-hidden", "disk-io-errors", "memory-errors"}  # port changes are handled by the baseline: sudo nuc-console-accept
COUNT_MATTERS = {"container-no-restart", "containers-not-back", "db-open-lan", "docker-bypass", "funnel-public", "unhealthy-container", "container-exited", "failed-units", "service-down"}
COUNT_MATTERS.add("over-exposed")  # which services go beyond [expose] matters, not only how many: a new one is a new problem
COUNT_MATTERS.add("expose-unmatched")  # a new typo is a new problem: an accepted one must not hide it


def fp_matches(accepted, current):
    """An accepted fingerprint still covers the current one: equal, or (service-down, cert-*) the same severity and every name now failing
    was already in the accepted list, so one leaving does not bring the alarm back and a new one does."""
    if accepted == current:
        return True
    if accepted.partition("|")[0] != current.partition("|")[0]:
        return False
    a, c = accepted.partition("|")[2], current.partition("|")[2]
    return a.startswith("names:") and c.startswith("names:") and set(c[6:].split("\n")) <= set(a[6:].split("\n"))


def fingerprint(sev, text, pid):
    """What was accepted: severity + text. For exposure/health items the exact text (one more is a new problem); for noisy
    counters (journal errors, temperatures) the digits are ignored, so 118 -> 120 stays accepted but a worse severity does not."""
    if (pid, text) in FP_NAMES:  # the text shows three names: the tie is to all of them
        return f"{sev}|names:" + "\n".join(FP_NAMES[(pid, text)])
    return f"{sev}|" + (text if pid in COUNT_MATTERS else re.sub(r"\d+", "#", text))


class ProblemList(list):
    """List of (severity, text) with .accepted = how many known items were left out (shown under ATTENTION) and .pids = the problem id
    of each item, in the same order (None when the list was not built by problems(): cards.Ctx then cannot tell which card a problem is
    about). What only the web has room for, None in a list that problems() did not build: .info = (title, why, fix, accept command) of each
    item, in the same order (the catalog's words for this OS and this install); .known = what was accepted, [{id, text, reason, ts}] (as
    many as .accepted); .cmds = the commands the advice refers to: {problems, accept, forget}."""
    accepted = 0
    muted = 0  # [alerts] mute: how many alarms the person silenced (only MUTABLE_ALERTS can be), shown under ATTENTION as a count
    muted_known = None  # [{id, text}] of them, as many as .muted
    pids = None
    info = None
    known = None

    @property
    def cmds(self):
        return {"problems": PROBLEMS_CMD, "accept": ACCEPT_CMD, "forget": ACCEPT_CMD + " --forget"}


def load_accepted(path=None):
    """{problem id: {"reason", "fp", ...}} accepted as known. A missing or broken file means nothing is accepted: it never hides by accident."""
    try:
        with open(path or ACCEPTED_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if k in CATALOG and k not in NOT_ACCEPTABLE and isinstance(v, dict)
            and isinstance(v.get("fp"), str) and isinstance(v.get("reason", ""), str)}


def muted_ids():
    """The ids [alerts] mute silences: what nuc_config.load kept of it, held once more to MUTABLE_ALERTS (a security alarm is never one of them,
    whoever filled CFG)."""
    return set((CFG.get("alerts") or {}).get("mute") or ()) & set(nuc_config.MUTABLE_ALERTS)


def problems(*a, **kw):
    """Anomalies to show, by decreasing severity: ProblemList of (3|2|1, text), without the ones you accepted. Empty = all ok."""
    acc = load_accepted()
    out = ProblemList()
    out.pids, out.info, out.known, out.muted_known = [], [], [], []
    mute = muted_ids()
    for sev, text, pid in problems_raw(*a, **kw):
        if pid in mute:
            out.muted += 1
            out.muted_known.append({"id": pid, "text": text})
        elif pid in acc and fp_matches(acc[pid]["fp"], fingerprint(sev, text, pid)):
            out.accepted += 1
            out.known.append({"id": pid, "text": text, "reason": acc[pid].get("reason", ""), "ts": acc[pid].get("ts")})
        else:
            title, why, fix = CATALOG.get(pid, (pid, "", ""))
            out.append((sev, text))
            out.pids.append(pid)
            out.info.append((title, why, fix, "" if pid in NOT_ACCEPTABLE else f'{ACCEPT_CMD} --problem {pid} --reason "..."'))
    return out


def problem_records(*a, **kw):
    """Every anomaly with its analysis, for `nuc-console-problems`: [{id, severity, text, accepted, muted, reason, title, why, fix, fingerprint}]."""
    acc, mute = load_accepted(), muted_ids()
    out = []
    for sev, text, pid in problems_raw(*a, **kw):
        title, why, fix = CATALOG.get(pid, (pid, "", ""))
        fp = fingerprint(sev, text, pid)
        out.append({"id": pid, "severity": {3: "port-change", 2: "error", 1: "warning"}.get(sev, "warning"), "text": text,
                    "accepted": pid not in mute and pid in acc and fp_matches(acc[pid]["fp"], fp), "muted": pid in mute, "reason": (acc.get(pid) or {}).get("reason", ""), "title": title,
                    "why": why, "fix": fix, "fingerprint": fp, "acceptable": pid not in NOT_ACCEPTABLE})
    return out


def safe_problems(*a, **kw):
    """problems() that never raises: a malformed state must not send the main loop into a crash-loop."""
    try:
        return problems(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return [(2, "unparseable state: " + safe(repr(e))[:60])]


def status_pill(pb):
    """(text, colour code) for the header: always visible, with a symbol besides the colour."""
    if not pb:
        return "✔ ALL OK", ui.sgr("banner_ok")
    if any(sev == 3 for sev, _ in pb):
        return "✖ EXPOSED PORTS CHANGED", ui.sgr("banner_err")
    n_err = sum(1 for sev, _ in pb if sev == 2)
    return (f"✖ {len(pb)} PROBLEMS", ui.sgr("banner_err")) if n_err else (f"! {len(pb)} WARNINGS", ui.sgr("banner_warn"))
