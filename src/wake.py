"""Wakes a blanked Linux text console when a NEW problem appears ([console] wake_on_problem, docs/CONFIGURATION.md). The renderer runs as the
unprivileged user that owns the console as its controlling terminal (StandardInput=tty), and the kernel lets that owner use TIOCLINUX
TIOCL_UNBLANKSCREEN without CAP_SYS_ADMIN: no root helper, no write to /sys/class/graphics. Anywhere else (macOS, Windows, a pty, a terminal that is
not the Linux console) it does nothing."""
import os
import time

import nuc_config

try:
    import fcntl
    import termios
except ImportError:  # Windows
    fcntl = termios = None

TIOCL_UNBLANKSCREEN = 4
MIN_GAP_S = 30.0  # at most one wake in this time: a flapping alarm must not keep the monitor lit


def is_vt(fd, env=None):
    """True for a Linux virtual console: Linux, a terminal, TERM=linux (what systemd sets for a tty unit)."""
    env = os.environ if env is None else env
    try:
        return bool(nuc_config.LINUX and fcntl and fd is not None and fd >= 0 and os.isatty(fd) and env.get("TERM") == "linux")
    except OSError:
        return False


def unblank(fd):
    """The ioctl; True when the kernel took it. Any OSError (not the owner, not a VT) is swallowed."""
    try:
        fcntl.ioctl(fd, termios.TIOCLINUX, bytes([TIOCL_UNBLANKSCREEN]))
        return True
    except (OSError, AttributeError, TypeError):
        return False


class Waker:
    """feed(problems) once per frame -> True when it woke the screen. A problem is new when its id was not in the previous frame of this process,
    or its severity went up; accepted and muted ones are not in the list given (problems.problems() leaves them out). The first frame only
    remembers: everything is new at start. A list without ids (a screen that failed to draw) changes nothing."""

    def __init__(self, enabled, fd, clock=time.monotonic, env=None):
        self.enabled = bool(enabled) and is_vt(fd, env)
        self.fd, self.clock = fd, clock
        self.prev, self.last = None, None

    def feed(self, pb):
        pids = getattr(pb, "pids", None)
        if not self.enabled or pids is None:
            return False
        cur = {}
        for pid, item in zip(pids, pb):
            cur[pid] = max(cur.get(pid, 0), item[0])
        prev, self.prev = self.prev, cur
        if prev is None or not any(sev >= 1 and sev > prev.get(pid, 0) for pid, sev in cur.items()):
            return False
        now = self.clock()
        if self.last is not None and now - self.last < MIN_GAP_S:
            return False  # ponytail: the event is dropped, not queued; the next new problem wakes it
        self.last = now
        return unblank(self.fd)
