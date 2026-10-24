"""Integrity of the host, Linux only (stdlib only, Python 3.8+): what the root collector reads and how it is compared.

Two things live here, both pure functions over plain reads (a `root` argument stands for "/", so a test can build a small file system):

  - the baseline sections `users`, `keys`, `cron`, `units`: who can log in or become root, a hash of each account's authorized_keys, a hash of
    every cron file, the enabled systemd unit files. Only names, counts and sha256 digests are kept, never the content of a file (a key, a
    command line) or a comment. `baseline_sections` is what `nuc-console-accept` stores, `baseline_problems` what is compared with it.
  - the SMART health of the physical disks: the parsers of `smartctl -j` and `nvme smart-log -o json`, and `smart_problems`.

The sections are written in net.json by collector.collect_net (each fails alone); the baseline file stays tolerant: a section that is not in
it is "not tracked yet" (one quiet note, `baseline-incomplete`), never an alarm. Unknown keys of the file are ignored."""
import errno
import hashlib
import json
import os
import re
import stat

from ui import safe

SECTIONS = ("users", "keys", "cron", "units")
SECTION_SEV = {"users": 2, "keys": 2, "cron": 1, "units": 1}
SECTION_ID = {s: s + "-changed" for s in SECTIONS}
BASELINE_VERSION = 2  # 1 = ports only (no "v" in the file)
ADMIN_GROUPS = ("sudo", "wheel", "admin", "docker")
MAX_FILE = 4 * 2 ** 20  # a file bigger than that is hashed by its first 4 MiB (and says so nowhere: it is a fingerprint)
MAX_ITEMS = 500
UNREADABLE = "unreadable"


def _p(root, path):
    return os.path.join(root, path.lstrip("/"))


def _owner_ok(file_uid, user_uid):
    return file_uid in (0, user_uid)


def _read(path, nofollow_uid=None):
    """Bytes of a regular file; None if it is missing or not a regular file; UNREADABLE if it cannot be read. nofollow_uid (a user's own file,
    which that user could point anywhere with a symlink): never follows a link and only accepts a file owned by that user or root."""
    try:
        fd = os.open(path, os.O_RDONLY | (getattr(os, "O_NOFOLLOW", 0) if nofollow_uid is not None else 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        return None if e.errno == errno.ELOOP else UNREADABLE  # a link where a user's own file should be: not followed, not hashed
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (nofollow_uid is not None and not _owner_ok(st.st_uid, nofollow_uid)):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(MAX_FILE)
    except OSError:
        return UNREADABLE
    finally:
        os.close(fd)


def _sha(data):
    return data if data == UNREADABLE else hashlib.sha256(data).hexdigest()


def parse_passwd(text):
    """[(name, uid, home, shell)] of /etc/passwd; lines with fewer than 6 fields are skipped (a missing shell is '')."""
    out = []
    for ln in (text or "").splitlines():
        f = ln.split(":")
        if len(f) >= 6 and f[0] and f[2].isdigit() and not ln.startswith("#"):
            out.append((f[0][:64], int(f[2]), f[5], (f[6] if len(f) > 6 else "").strip()[:60]))
    return out


def parse_group(text):
    """{group: [member names]} of /etc/group."""
    out = {}
    for ln in (text or "").splitlines():
        f = ln.split(":")
        if len(f) >= 4 and f[0] and not ln.startswith("#"):
            out[f[0]] = [m for m in f[3].split(",") if m]
    return out


NO_LOGIN = ("nologin", "false", "sync", "shutdown", "halt")


def real_shell(shell):
    """A shell a person can log in with: not nologin, false, sync, shutdown or halt (an empty field means /bin/sh)."""
    return os.path.basename(shell) not in NO_LOGIN


def passwd(root="/"):
    """(all rows of /etc/passwd as (name, uid, home, shell), {group: members} of /etc/group); OSError when /etc/passwd cannot be read."""
    pw = _read(_p(root, "/etc/passwd"))
    if pw is None or pw == UNREADABLE:
        raise OSError("/etc/passwd unreadable")
    grp = _read(_p(root, "/etc/group"))
    return parse_passwd(pw.decode("utf-8", "replace")), parse_group(grp.decode("utf-8", "replace") if isinstance(grp, bytes) else "")


def users(root="/"):
    """{name: {"uid", "shell", "groups"}} of the accounts that matter: uid 0, uid >= 1000, any with a login shell, any member of
    sudo, wheel, admin or docker; groups = the ones of those four it is in (sorted)."""
    rows, groups = passwd(root)
    out = {}
    for n, uid, _, shell in rows:
        mine = sorted(g for g in ADMIN_GROUPS if n in groups.get(g, ()))
        if uid == 0 or uid >= 1000 or mine or real_shell(shell):
            out.setdefault(n, {"uid": uid, "shell": shell, "groups": mine})
    return dict(sorted(out.items())[:MAX_ITEMS])


def keys(root="/"):
    """{account: {"sha256", "n"}} of ~/.ssh/authorized_keys of EVERY account of /etc/passwd whose home has one (root included); n = lines
    that hold a key, not blank or comment."""
    out = {}
    for name, uid, home, _ in passwd(root)[0]:
        if not home.startswith("/") or name in out:
            continue
        data = _read(_p(root, home + "/.ssh/authorized_keys"), nofollow_uid=uid)
        if data is None:
            continue
        n = 0 if data == UNREADABLE else sum(1 for ln in data.splitlines() if ln.strip() and not ln.lstrip().startswith(b"#"))
        out[name] = {"sha256": _sha(data), "n": n}
    return dict(sorted(out.items())[:MAX_ITEMS])


def _listdir(path):
    try:
        return sorted(os.listdir(path))[:MAX_ITEMS]
    except OSError:
        return []


def cron(root="/"):
    """{file: sha256}: /etc/crontab, /etc/anacrontab, each file of /etc/cron.d and of cron.hourly/daily/weekly/monthly, and 'spool:<user>' /
    'spool-cron:<user>' for each per-user crontab (/var/spool/cron/crontabs, /var/spool/cron)."""
    todo = [("/etc/crontab", "/etc/crontab"), ("/etc/anacrontab", "/etc/anacrontab")]
    for d in ("/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly"):
        todo += [(d + "/" + n, d + "/" + n) for n in _listdir(_p(root, d))]
    for tag, d in (("spool:", "/var/spool/cron/crontabs"), ("spool-cron:", "/var/spool/cron")):
        todo += [(tag + n, d + "/" + n) for n in _listdir(_p(root, d))]
    out = {}
    for label, path in todo:
        data = _read(_p(root, path))  # a folder (crontabs inside /var/spool/cron) is not a regular file: skipped
        if data is not None:
            out[safe(label)[:80]] = _sha(data)
    return out


UNIT = re.compile(r"^[A-Za-z0-9:_.@\\-]+\.(service|timer|socket|path|mount|target)$")


def _link(path):
    return "link:" + safe(os.readlink(path))[:120]


def units(root="/"):
    """{label: value} under /etc/systemd/system: the units enabled by hand (entries of *.wants / *.requires), the unit files that live there and
    the drop-ins (`name.service.d/override.conf`). A regular file is its sha256; a link is 'link:<target path>' (the target is never read or
    followed: a relink shows, the vendor's file is not hashed)."""
    base, out = _p(root, "/etc/systemd/system"), {}

    def add(label, full):
        try:
            if os.path.islink(full):
                out.setdefault(label, _link(full))
            elif os.path.isfile(full):
                data = _read(full)
                if data is not None:
                    out.setdefault(label, _sha(data))
        except OSError:
            out.setdefault(label, UNREADABLE)

    for n in _listdir(base):
        full = os.path.join(base, n)
        if n.endswith((".wants", ".requires")) and os.path.isdir(full) and not os.path.islink(full):
            for u in _listdir(full):
                if UNIT.match(u):
                    add(u, os.path.join(full, u))
        elif n.endswith(".d") and UNIT.match(n[:-2]) and os.path.isdir(full) and not os.path.islink(full):
            for f in _listdir(full):
                if f.endswith(".conf") and UNIT.match("x" + f[:-5] + ".service"):
                    add(n + "/" + f, os.path.join(full, f))
        elif UNIT.match(n):
            add(n, full)
    return out


COLLECT = {"users": users, "keys": keys, "cron": cron, "units": units}


# ---- the baseline ----------------------------------------------------------------------------------------------------------

def _valid(sec, v):
    """A section of baseline.json is usable only in its own shape; anything else counts as not there."""
    if sec == "users":  # a list of names is the older shape (names only)
        return (isinstance(v, list) and all(isinstance(x, str) for x in v)) or (isinstance(v, dict) and all(
            isinstance(x, dict) and isinstance(x.get("uid"), int) and isinstance(x.get("shell"), str) and isinstance(x.get("groups"), list)
            and all(isinstance(g, str) for g in x["groups"]) for x in v.values()))
    if sec == "keys":
        return isinstance(v, dict) and all(isinstance(x, dict) and isinstance(x.get("sha256"), str) and isinstance(x.get("n"), int) for x in v.values())
    return isinstance(v, dict) and all(isinstance(x, str) for x in v.values())


def baseline_sections(net):
    """{section: data} of net.json that nuc-console-accept stores next to the ports (the ones this OS collected without an error)."""
    return {s: net[s] for s in SECTIONS if isinstance(net, dict) and net.get(s) is not None and _valid(s, net[s])}


def _names(items, cap=3):
    items = [safe(str(x))[:40] for x in items]
    return ", ".join(items[:cap]) + (f" (+{len(items) - cap})" if len(items) > cap else "")


def _users_diff(cur, base):
    """Names added and removed, and, when both sides know them, 'alice uid 1000->0', 'carol shell /bin/bash->/bin/false', 'bob joined sudo'."""
    asd = lambda v: v if isinstance(v, dict) else {n: None for n in v}  # noqa: E731 - the older shape is a list of names
    cur, base = asd(cur), asd(base)
    parts, add, rem = [], sorted(set(cur) - set(base)), sorted(set(base) - set(cur))
    for n in sorted(set(cur) & set(base)):
        c, o = cur[n], base[n]
        if not isinstance(c, dict) or not isinstance(o, dict):
            continue
        n = safe(n)[:40]
        if c["uid"] != o["uid"]:
            parts.append(f"{n} uid {o['uid']}->{c['uid']}")
        if c["shell"] != o["shell"]:
            parts.append(f"{n} shell {safe(o['shell'])[:30] or '(none)'}->{safe(c['shell'])[:30] or '(none)'}")
        parts += [f"{n} joined {safe(g)[:20]}" for g in sorted(set(c["groups"]) - set(o["groups"]))]
        parts += [f"{n} left {safe(g)[:20]}" for g in sorted(set(o["groups"]) - set(c["groups"]))]
    parts = ([f"added {_names(add)}"] if add else []) + ([f"removed {_names(rem)}"] if rem else []) + parts
    return "; ".join(parts[:6]) + (f"; +{len(parts) - 6} more" if len(parts) > 6 else "")


def diff(sec, cur, base):
    """What changed in a section, in words, without any content; "" when nothing."""
    if sec == "users":
        return _users_diff(cur, base)
    else:
        add, rem = sorted(set(cur) - set(base)), sorted(set(base) - set(cur))
        if sec == "keys":
            mod = sorted(k for k in cur if k in base and cur[k] != base[k])
            what = lambda k: f"{k} ({base[k]['n']}->{cur[k]['n']} keys)" if cur[k]["n"] != base[k]["n"] else k  # noqa: E731
            mod = [what(k) for k in mod]
        else:
            mod = sorted(k for k in cur if k in base and cur[k] != base[k])
        parts = ([f"added {_names(add)}"] if add else []) + ([f"removed {_names(rem)}"] if rem else []) + ([f"changed {_names(mod)}"] if mod else [])
    return "; ".join(parts)


LABEL = {"users": "login/sudo accounts", "keys": "authorized_keys", "cron": "cron", "units": "enabled systemd units"}


def baseline_problems(net, base, accept_cmd):
    """[(severity, text, id)] against a baseline dict: the changes of each section, and one note for the sections the baseline does not have."""
    out, missing, names_only = [], [], False
    if not isinstance(net, dict):
        return out
    for sec in SECTIONS:
        cur = net.get(sec)
        if cur is None or not _valid(sec, cur):
            continue  # not collected (another OS, off, an error that net-sections already says): nothing to compare
        old = base.get(sec) if isinstance(base, dict) else None
        if old is None or not _valid(sec, old):
            missing.append(sec)
            continue
        names_only = names_only or (sec == "users" and isinstance(old, list) and isinstance(cur, dict))  # compared by name until re-accepted
        d = diff(sec, cur, old)
        if d:
            out.append((SECTION_SEV[sec], f"{LABEL[sec]} changed: {d}", SECTION_ID[sec]))
    if missing or names_only:
        text = (f"baseline predates {', '.join(missing)}" if missing else "baseline lists account names only")
        text += f": run {accept_cmd} to include " + ("them (not tracked until then)" if missing else "uid, shell and groups")
        out.append((1, text, "baseline-incomplete"))
    return out


# ---- SMART -----------------------------------------------------------------------------------------------------------------

DEVICE = re.compile(r"^(nvme[0-9]+n[0-9]+|sd[a-z]+)$")  # never loop, ram, dm-, zram; validated before it is part of an argv


def block_devices(root="/"):
    """The physical block devices of /sys/block, sorted; removable media (a USB stick) is left out."""
    out = []
    for n in _listdir(_p(root, "/sys/block")):
        if DEVICE.match(n):
            rem = _read(_p(root, f"/sys/block/{n}/removable"))
            if rem is None or rem == UNREADABLE or rem.strip() != b"1":
                out.append(n)
    return out


def sysfs_model(dev, root="/"):
    raw = _read(_p(root, f"/sys/block/{dev}/device/model"))
    return " ".join(raw.decode("utf-8", "replace").split())[:40] if isinstance(raw, bytes) else ""


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _entry(dev, model, health, wear, spare, media, crit, poh, realloc):
    return {"dev": dev, "model": safe(model or "")[:40], "health": health, "wear": _int(wear), "spare": _int(spare), "media": _int(media),
            "crit": _int(crit), "poh": _int(poh), "realloc": _int(realloc)}


def parse_smartctl(dev, text, model=""):
    """`smartctl -j -H -A` -> the figures kept, or None when it read nothing (no permission, an unknown USB bridge, a virtual disk)."""
    try:
        d = json.loads(text)
    except (ValueError, RecursionError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    ss = d.get("smart_status") if isinstance(d.get("smart_status"), dict) else {}
    log = d.get("nvme_smart_health_information_log") if isinstance(d.get("nvme_smart_health_information_log"), dict) else {}
    table = ((d.get("ata_smart_attributes") or {}).get("table")) if isinstance(d.get("ata_smart_attributes"), dict) else None
    attrs = {a.get("id"): a for a in table if isinstance(a, dict)} if isinstance(table, list) else {}
    if not isinstance(ss.get("passed"), bool) and not log and not attrs:
        return None
    health = None if not isinstance(ss.get("passed"), bool) else ("PASSED" if ss["passed"] else "FAILED")
    raw = lambda i: (attrs.get(i, {}).get("raw") or {}).get("value") if isinstance(attrs.get(i), dict) else None  # noqa: E731
    wear = log.get("percentage_used")
    for i in (231, 233):  # SATA SSD: normalized value = life left, 100 -> 0
        v = (attrs.get(i) or {}).get("value")
        if wear is None and _int(v) is not None and v <= 100:
            wear = 100 - v
    poh = log.get("power_on_hours", (d.get("power_on_time") or {}).get("hours") if isinstance(d.get("power_on_time"), dict) else None)
    return _entry(dev, d.get("model_name") or model, health, wear, log.get("available_spare"), log.get("media_errors"),
                  log.get("critical_warning"), poh, raw(5))


def parse_nvme(dev, text, model=""):
    """`nvme smart-log -o json` -> the same figures (the controller's critical_warning is its verdict: 0 = PASSED)."""
    try:
        d = json.loads(text)
    except (ValueError, RecursionError, TypeError):
        return None
    if not isinstance(d, dict) or _int(d.get("critical_warning")) is None:
        return None
    return _entry(dev, model, "PASSED" if d["critical_warning"] == 0 else "FAILED",
                  d.get("percent_used", d.get("percentage_used")), d.get("avail_spare", d.get("available_spare")), d.get("media_errors"),
                  d["critical_warning"], d.get("power_on_hours"), None)


def smart_problems(smart):
    """[(severity, text, id)] from the list the collector wrote under net['smart'] (anything else: nothing, the card says '?')."""
    if not isinstance(smart, list):
        return []
    rows = [r for r in smart if isinstance(r, dict) and isinstance(r.get("dev"), str)]
    out = []
    failed = [r["dev"] for r in rows if r.get("health") == "FAILED" or (_int(r.get("crit")) or 0) != 0]
    if failed:
        out.append((2, "SMART health failed: " + _names(failed), "disk-smart-failed"))
    worn = [(r["dev"], r["wear"]) for r in rows if _int(r.get("wear")) is not None and r["wear"] >= 80]
    if worn:
        top = max(w for _, w in worn)
        out.append((2 if top >= 95 else 1, "disk wear: " + _names([f"{d} {w} %" for d, w in worn]), "disk-wear"))
    bad = [r for r in rows if (_int(r.get("media")) or 0) > 0 or (_int(r.get("realloc")) or 0) > 0]
    if bad:
        grown = any(r.get("grown") for r in bad)
        out.append((2 if grown else 1, "disk media errors" + (" growing" if grown else "") + ": " + _names([r["dev"] for r in bad]), "disk-media-errors"))
    return out
