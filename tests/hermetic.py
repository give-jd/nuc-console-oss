"""The host's state, kept out of the tests.

Imported first by every test module (before a src module reads the environment or a path): the suite then gives the same result on a
machine that runs nuc-console (a baseline, accepted problems, a collector writing in /run, a config.ini) as on a clean one.

  - the environment: every NUC_CONSOLE_* variable that names a file is pointed at a folder that does not exist, and what changes how
    a screen is drawn (COLUMNS, LINES, NO_COLOR, XDG_*) is removed; HOME is an empty temporary folder;
  - a tripwire on open(), os.stat(), os.listdir(), os.scandir() and sqlite3.connect(): a path inside the folders the installed
    console owns (nuc_config.ETC_DIR, RUN_DIR, LIB_DIR and the Telegram folder) raises HostStateRead and is recorded in VIOLATIONS.
    HostStateRead is a BaseException, so that a `try: ... except OSError` (or `except Exception`) in the code under test cannot
    turn the read into "no file". A test that needs a file there points the path at a temporary folder instead (patch.object(render,
    "ACCEPTED_PATH", ...), mock.patch.object(nuc_config, "LIB_DIR", ...)); test_zz_host_state.py proves the guard and fails if a read
    was swallowed anyway (it sorts last, so that it runs after every other module).

The same tripwire stops the commands that ask the machine (HOST_COMMANDS: ss, docker, journalctl, loginctl...). Not guarded: /proc and /sys
(the readings the screens draw from the machine; no test asserts on them). The tests that want the real ones say so (test_cpuinfo.RealHost, test_procs.test_real_proc).
"""
import atexit
import builtins
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

SCRATCH = tempfile.mkdtemp(prefix="nuc-hermetic-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
ABSENT = os.path.join(SCRATCH, "absent")  # nothing is ever created here

for _k in ("COLUMNS", "LINES", "NO_COLOR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "NUC_CONSOLE_HOME", "NUC_CONSOLE_MODE",
           "NUC_CONSOLE_WEB"):
    os.environ.pop(_k, None)
os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(SCRATCH, "home")
for _k, _f in (("CONFIG", "config.ini"), ("BASELINE", "baseline.json"), ("ACCEPTED", "accepted.json"), ("NET", "net.json"),
               ("STATE", "containers.json"), ("BOOT", "boot.json"), ("SENSORS", "sensors.json")):
    os.environ["NUC_CONSOLE_" + _k] = os.path.join(ABSENT, _f)
os.environ["NUC_CONSOLE_NOTIFY_DIR"] = os.path.join(ABSENT, "notify")

import nuc_config  # noqa: E402  (after the environment: it reads NUC_CONSOLE_HOME and NUC_CONSOLE_NOTIFY_DIR when imported)

# what the installed console owns: where nuc_config says it lives on this OS (the temporary folder of a portable run is not the host)
HOST_DIRS = tuple(os.path.normcase(os.path.abspath(d)) for d in (nuc_config.ETC_DIR, nuc_config.RUN_DIR, nuc_config.LIB_DIR)
                  if not os.path.abspath(d).startswith(SCRATCH)) + (
    ("/var/lib/nuc-console", "/var/run/nuc-console", "/run/nuc-console", "/etc/nuc-console", "/var/lib/nuc-console-notify")
    if nuc_config.LINUX else ())

# aisetup.default_dir() names the administrator's folder (euid 0: /var/lib/nuc-console/ai) as a literal, and find_dir()/work_dir() look in
# it for a stamp file to see what was installed with sudo: on a machine that has AI set up the screens would show that. For the host's own
# OS (plat=None) that folder is an empty one; a test that names a platform gets the real literal back, which is what it asserts on.
import aisetup  # noqa: E402
_default_dir = aisetup.default_dir


def _hermetic_default_dir(plat=None, env=None, euid=None, home=None):
    d = _default_dir(plat, env, euid, home)
    return os.path.join(ABSENT, "ai") if plat is None and os.path.normcase(d) in [os.path.join(h, "ai") for h in HOST_DIRS] else d


aisetup.default_dir = _hermetic_default_dir

# the same for history.open_ro(): the screens and pages open the history of the machine (history.PATH) when nobody says which one.
import history  # noqa: E402
_open_ro = history.open_ro


def _hermetic_open_ro(path=None):
    return _open_ro(path or os.path.join(ABSENT, "history.db"))


history.open_ro = _hermetic_open_ro

# render.snapshot() starts a background thread that runs loginctl and ss for the SESSIONS card: the card would show whoever is logged in
# on the machine. The thread gets an empty answer instead (render.read_sessions itself is untouched: a test that fakes subprocess.run
# and calls it, or that gives cached() another function, gets what it asked for).
import render  # noqa: E402
_cached, _read_sessions = render.cached, render.read_sessions


def _hermetic_cached(key, ttl, fn):
    return _cached(key, ttl, (lambda: {"local": [], "ssh": []}) if fn is _read_sessions else fn)


render.cached = _hermetic_cached
VIOLATIONS = []


class HostStateRead(BaseException):
    """A test (or the code under test) touched the host's nuc-console state: inject the path instead."""


def _who():
    """The innermost frame outside this file and the innermost one in tests/: where the read comes from."""
    import traceback
    frames = [f for f in traceback.extract_stack() if os.path.basename(f.filename) != "hermetic.py"]
    tests = [f for f in frames if os.path.normpath(os.path.dirname(f.filename)) == HERE]
    srcs = [f for f in frames if os.path.normpath(os.path.dirname(f.filename)) == os.path.normpath(SRC)]
    return " <- ".join("%s:%s %s" % (os.path.basename(f.filename), f.lineno, f.name) for f in (srcs[-1:] + tests[-1:]))


def _guard(kind, path):
    try:
        p = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))
    except (TypeError, ValueError):  # a file descriptor, a closed object: not a path
        return
    if any(p == d or p.startswith(d + os.sep) for d in HOST_DIRS):
        who = _who()
        VIOLATIONS.append("%s %s (from %s)" % (kind, p, who))
        raise HostStateRead("%s of the host's state: %s, from %s (point the path at a temporary folder)" % (kind, p, who))


def _wrap(mod, name):
    real = getattr(mod, name)
    if getattr(real, "_hermetic", False):
        return

    def guarded(path, *a, **k):
        _guard(name, path)
        return real(path, *a, **k)
    guarded._hermetic = True
    setattr(mod, name, guarded)


# the commands the collector and the renderer run to ask the machine (listeners, sessions, containers, journal, firewall): an answer from
# the host, not from the fixture. The test that wants one fakes subprocess itself (that is above this wrapper and never reaches it).
HOST_COMMANDS = () if not nuc_config.LINUX else {"ss", "netstat", "loginctl", "who", "docker", "journalctl", "systemctl", "ufw", "iptables", "nft", "tailscale",
                 "fail2ban-client", "lsof", "last", "sensors", "smartctl"}
_Popen_init = subprocess.Popen.__init__


def _popen_init(self, args, *a, **k):
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    if argv and os.path.basename(os.fsdecode(argv[0])) in HOST_COMMANDS:
        who = _who()
        VIOLATIONS.append("run %s (from %s)" % (os.fsdecode(argv[0]), who))
        raise HostStateRead("run of %s asks the host, from %s: fake the command in the test" % (os.fsdecode(argv[0]), who))
    return _Popen_init(self, args, *a, **k)


subprocess.Popen.__init__ = _popen_init

# os.path.exists() and friends: on Windows with Python 3.12+ they are C functions that never go through os.stat()
for _mod, _name in ((builtins, "open"), (os, "stat"), (os, "listdir"), (os, "scandir"), (os, "open"), (sqlite3, "connect"),
                    (os.path, "exists"), (os.path, "lexists"), (os.path, "isfile"), (os.path, "isdir"), (os.path, "islink")):
    _wrap(_mod, _name)

# macOS and Windows: the system tools hostinfo runs (vm_stat, sysctl, netstat, mount, who...) answer for the machine. They are "not there"
# (hostinfo then says "unavailable", as it does for a missing tool), except for the tests that exist to try the real ones (OnMacOS, OnWindows
# in test_platforms), which switch REAL_TOOLS on for their duration.
REAL_TOOLS = False
if not nuc_config.LINUX:
    import hostinfo  # noqa: E402
    _hostinfo_run = hostinfo._run

    def _hermetic_hostinfo_run(*args, **kwargs):
        if not REAL_TOOLS:
            raise FileNotFoundError(args[0])
        return _hostinfo_run(*args, **kwargs)
    hostinfo._run = _hermetic_hostinfo_run
