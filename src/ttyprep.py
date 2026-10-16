"""Console font and screen blanking of the dashboard's tty (Linux), run by nuc-console.service before the dashboard starts (ExecStartPre=-+,
as root: setfont needs it). Reads [console] of config.ini. Both settings are off by default. Every step is skipped, never an error, when its
tool, its font or the tty is not there: the dashboard starts anyway. Running it twice leaves the same state; blank_minutes back to 0 takes the timer it set before back (docs/CONFIGURATION.md)."""
import os
import shutil
import subprocess
import sys

import nuc_config


def run(cmd, say, stdin=subprocess.DEVNULL):
    """Runs one command (stdin: setterm --blank poke acts on the terminal it is given as input); False (and a line for the journal) when it is missing or fails. Never raises."""
    exe = shutil.which(cmd[0])
    if not exe:
        say(f"nuc-console: {cmd[0]} not found: skipped")
        return False
    try:
        r = subprocess.run([exe] + cmd[1:], stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        say(f"nuc-console: {cmd[0]}: {e}")
        return False
    if r.returncode:
        say(f"nuc-console: {cmd[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.returncode == 0


def open_tty(tty):
    return os.open(tty, os.O_RDONLY | os.O_NOCTTY | getattr(os, "O_NONBLOCK", 0))


def poke(tty, say):
    """setterm --blank poke: unblank the screen. setterm does the ioctl on its standard input, so the tty is given as stdin (--file is only where
    it writes its escape sequences)."""
    try:
        fd = open_tty(tty)
    except OSError as e:
        say(f"nuc-console: {tty}: {e}")
        return False
    try:
        return run(["setterm", "--term", "linux", "--blank", "poke"], say, stdin=fd)
    finally:
        os.close(fd)


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
    mark = os.path.join(nuc_config.RUN_DIR, "ttyprep-blank")  # in /run: gone at reboot, when the kernel's own timer is back too
    if cfg["blank_minutes"]:  # --powerdown too: the monitor goes to sleep, not only black. Any key (or the next start) wakes it
        m = str(cfg["blank_minutes"])
        if run(["setterm", "--term", "linux", "--blank", m, "--powerdown", m, "--file", tty], say):
            poke(tty, say)  # wake it now: a screen already black comes back
        try:
            open(mark, "w").close()
        except OSError:
            pass
    elif os.path.exists(mark):  # 0 after a number: the timer set on an earlier start stays in the kernel until it is told otherwise
        if run(["setterm", "--term", "linux", "--blank", "0", "--powerdown", "0", "--file", tty], say):
            poke(tty, say)
        try:
            os.remove(mark)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
