"""The "Execute" button of five ATTENTION problems ([web] fix_actions, docs/WEB.md#the-execute-button).

No root process is added. The web account calls `systemctl <verb> <unit>` itself, and polkit decides: a rule (systemd/50-nuc-console-fix.rules,
generated from TABLE by polkit_rule()) lets exactly that account manage exactly these units with exactly these verbs, and nothing else. This
module is the second lock, not the only one: the request carries a problem id and nothing more, the id is looked up in TABLE (the one place that
says what can run), the problem must be active right now (the caller passes the live list: a client is never believed), the same unit is not
touched twice within RATE_S seconds, and only one run goes at a time. Linux only. A refusal says why in a few fixed words; the output of
systemctl never reaches a page.

python3 fixactions.py --enabled   exit 0 when config.ini has [web] fix_actions = yes (install.sh: install or remove the polkit rule)
python3 fixactions.py --rule      print the polkit rule (the committed file is this text; a test compares them)"""

import shutil
import subprocess
import sys
import threading
import time

import nuc_config

WEB_USER = "nuc-console"  # the account of nuc-console-web.service: the only one the polkit rule names
RATE_S = 60  # the same unit is not run again within this many seconds, whichever problem asked
RUN_TIMEOUT_S = 20
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}  # systemctl is looked up in this PATH, not the caller's
# problem id -> (verb, unit, label of the button). Each id exists in problems.BASE_CATALOG and its `fix` text names the same command (a test).
TABLE = {
    "collector-net": ("restart", "nuc-console-collector.service", "Restart nuc-console-collector"),
    "collector-boot": ("restart", "nuc-console-collector.service", "Restart nuc-console-collector"),
    "stale-containers": ("restart", "nuc-console-collector.service", "Restart nuc-console-collector"),
    "stale-net": ("restart", "nuc-console-collector.service", "Restart nuc-console-collector"),
    "fleet-poller-down": ("start", "nuc-console-peers.service", "Start nuc-console-peers"),
}
DONE = {"restart": "restarted", "start": "started"}
DENIED = ("access denied", "interactive authentication required")  # what systemctl says when polkit says no

_lock = threading.Lock()  # one run at a time
_last = {}  # unit -> the time of its last attempt (guarded by _lock)


def available(cfg):
    """Linux, and [web] fix_actions = yes (a bool True: nothing else counts)."""
    return bool(nuc_config.LINUX and ((cfg or {}).get("web") or {}).get("fix_actions") is True)


def item(pid, cfg):
    """What the data of a problem carries: {"label", "id"} when the button may be drawn for it, else None. The caller lists only active problems."""
    if pid in TABLE and available(cfg):
        return {"label": TABLE[pid][2], "id": pid}
    return None


def _log(pid, what):
    print("nuc-console web: fix %s: %s" % (pid, what), file=sys.stderr)


def run(pid, active, cfg, now=None):
    """Run the fix of problem `pid` -> (ok, note). `active`: the ids of the problems on the page now (the caller recomputes them; never the client's
    word). Every refusal comes before anything is run, in this order: feature off, not Linux, id not in TABLE, problem not active, a run in
    progress, the unit run less than RATE_S ago. One line per attempt in the journal: the validated id, the fixed argv, the outcome."""
    if not (((cfg or {}).get("web") or {}).get("fix_actions") is True):
        _log("?", "refused(off)")
        return False, "Execute is off ([web] fix_actions in config.ini)"
    if not nuc_config.LINUX:
        _log("?", "refused(not linux)")
        return False, "Execute is only available on Linux"
    if not isinstance(pid, str) or pid not in TABLE:
        _log("?", "refused(unknown id)")
        return False, "this problem has no Execute"
    verb, unit, _label = TABLE[pid]
    cmd = "systemctl %s %s" % (verb, unit)
    if pid not in active:
        _log(pid, "%s: refused(not active)" % cmd)
        return False, "this problem is not active any more"
    if not _lock.acquire(blocking=False):
        _log(pid, "%s: refused(busy)" % cmd)
        return False, "another fix is running: wait a moment"
    try:
        t = time.monotonic() if now is None else now
        if unit in _last and 0 <= t - _last[unit] < RATE_S:
            _log(pid, "%s: refused(rate limit)" % cmd)
            return False, "%s was run less than a minute ago: wait" % unit
        _last[unit] = t
        exe = shutil.which("systemctl", path=ENV["PATH"])
        if not exe:
            _log(pid, "%s: failed(no systemctl)" % cmd)
            return False, "failed: systemctl was not found"
        try:
            r = subprocess.run([exe, "--no-ask-password", verb, unit], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=RUN_TIMEOUT_S, env=dict(ENV))
        except subprocess.TimeoutExpired:
            _log(pid, "%s: failed(timeout)" % cmd)
            return False, "failed: systemctl did not answer in %d s" % RUN_TIMEOUT_S
        except OSError:
            _log(pid, "%s: failed(not run)" % cmd)
            return False, "failed: systemctl could not be run"
        if r.returncode == 0:
            _log(pid, "%s: ok" % cmd)
            return True, "%s %s" % (unit, DONE[verb])
        err = (r.stderr or b"")[:2000].decode("utf-8", "replace").lower()  # looked at, never shown
        _log(pid, "%s: failed(rc %d)" % (cmd, r.returncode))
        if any(w in err for w in DENIED):
            return False, "not permitted: the polkit rule is not installed (docs/INSTALL.md)"
        return False, "failed (see the service log)"
    finally:
        _lock.release()


def polkit_rule():
    """The text of systemd/50-nuc-console-fix.rules: the (unit, verb) pairs of TABLE, spelled out, for WEB_USER; no pattern, no wildcard. Any other
    action, unit, verb or user falls through (the rule returns nothing), so the default policy applies."""
    pairs = sorted({(unit, verb) for verb, unit, _l in TABLE.values()})
    tests = " ||\n".join('         (action.lookup("unit") == "%s" && action.lookup("verb") == "%s")' % p for p in pairs)
    return ("// nuc-console: the web account may run the fixes of the Execute button, and nothing else (docs/INSTALL.md, SECURITY.md).\n"
            "// Installed by install.sh only while [web] fix_actions = yes. Generated from src/fixactions.py TABLE: do not edit.\n"
            "polkit.addRule(function(action, subject) {\n"
            '    if (action.id == "org.freedesktop.systemd1.manage-units" &&\n'
            '        subject.user == "%s" &&\n'
            "        (\n%s)) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n" % (WEB_USER, tests))


def main(argv):
    if "--rule" in argv:
        sys.stdout.write(polkit_rule())
        return 0
    if "--enabled" in argv:  # install.sh: anything uncertain (unreadable file, crash) is "off": the rule is then removed
        try:
            return 0 if available_in_file() else 1
        except Exception:  # noqa: BLE001
            return 1
    print(__doc__)
    return 2


def available_in_file():
    """[web] fix_actions in the config.ini of this installation, read alone (no overlay), on Linux."""
    cfg = nuc_config.load(overlay=False, warn=lambda line: None)
    return not cfg.get("config_error") and available(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
