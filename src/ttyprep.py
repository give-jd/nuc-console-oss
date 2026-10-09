"""Console font and screen blanking of the dashboard's tty (Linux), run by nuc-console.service before the dashboard starts (ExecStartPre=-+,
as root: setfont needs it). Reads [console] of config.ini. Both settings are off by default. Every step is skipped, never an error, when its
tool, its font or the tty is not there: the dashboard starts anyway. Running it twice leaves the same state (docs/CONFIGURATION.md)."""
import os
import shutil
import subprocess
import sys

import nuc_config


def run(cmd, say):
    """Runs one command; False (and a line for the journal) when it is missing or fails. Never raises."""
    exe = shutil.which(cmd[0])
    if not exe:
        say(f"nuc-console: {cmd[0]} not found: skipped")
        return False
    try:
        r = subprocess.run([exe] + cmd[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        say(f"nuc-console: {cmd[0]}: {e}")
        return False
    if r.returncode:
        say(f"nuc-console: {cmd[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.returncode == 0


def main(env=None, say=None):
    env = os.environ if env is None else env
    say = say or (lambda line: print(line, file=sys.stderr))
    if not nuc_config.LINUX:
        return 0
    cfg = nuc_config.load(warn=say)["console"]
    vt = env.get("NUC_CONSOLE_VT", "1")
    if not (vt.isascii() and vt.isdigit() and 1 <= int(vt) <= 63):
        say(f"nuc-console: NUC_CONSOLE_VT={vt!r} is not a terminal: nothing to set")
        return 0
    tty = "/dev/tty" + vt
    if cfg["font"]:
        run(["setfont", "-C", tty, cfg["font"]], say)
    if cfg["blank_minutes"]:  # --powerdown too: the monitor goes to sleep, not only black. Any key (or the next start) wakes it
        m = str(cfg["blank_minutes"])
        run(["setterm", "--term", "linux", "--blank", m, "--powerdown", m, "--file", tty], say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
