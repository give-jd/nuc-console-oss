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
        if not stat.S_ISREG(st.st_mode) or (nofollow_uid is not None and st.st_uid not in (0, nofollow_uid)):
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
    """[(name, uid, home)] of /etc/passwd; lines that are not 7 fields are skipped."""
    out = []
    for ln in (text or "").splitlines():
        f = ln.split(":")
        if len(f) >= 6 and f[0] and f[2].isdigit() and not ln.startswith("#"):
            out.append((f[0][:64], int(f[2]), f[5]))
    return out


def parse_group(text):
    """{group: [member names]} of /etc/group."""
    out = {}
    for ln in (text or "").splitlines():
        f = ln.split(":")
        if len(f) >= 4 and f[0] and not ln.startswith("#"):
            out[f[0]] = [m for m in f[3].split(",") if m]
    return out


def accounts(root="/"):
    """[(name, uid, home)] of the accounts that matter: uid 0, uid >= 1000, and the members of sudo, wheel, admin and docker."""
    pw = _read(_p(root, "/etc/passwd"))
    if pw is None or pw == UNREADABLE:
        raise OSError("/etc/passwd unreadable")
    grp = _read(_p(root, "/etc/group"))
    groups = parse_group(grp.decode("utf-8", "replace") if isinstance(grp, bytes) else "")
    admins = {m for g in ADMIN_GROUPS for m in groups.get(g, ())}
    rows = parse_passwd(pw.decode("utf-8", "replace"))
    return sorted((n, u, h) for n, u, h in rows if u == 0 or u >= 1000 or n in admins)[:MAX_ITEMS]


def users(root="/"):
    return sorted({n for n, _, _ in accounts(root)})


def keys(root="/"):
    """{account: {"sha256", "n"}} of ~/.ssh/authorized_keys, for the accounts that have one (n = lines that hold a key, not blank or comment)."""
    out = {}
    for name, uid, home in accounts(root):
        if not home.startswith("/") or name in out:
            continue
        data = _read(_p(root, home + "/.ssh/authorized_keys"), nofollow_uid=uid)
        if data is None:
            continue
        n = 0 if data == UNREADABLE else sum(1 for ln in data.splitlines() if ln.strip() and not ln.lstrip().startswith(b"#"))
        out[name] = {"sha256": _sha(data), "n": n}
    return out


def _listdir(path):
    try:
        return sorted(os.listdir(path))[:MAX_ITEMS]
    except OSError:
        return []


def cron(root="/"):
    """{file: sha256}: /etc/crontab, each /etc/cron.d/* file, and 'spool:<user>' for each per-user crontab."""
    out = {}
    for label, path in [("/etc/crontab", _p(root, "/etc/crontab"))] + [
            ("/etc/cron.d/" + n, _p(root, "/etc/cron.d/" + n)) for n in _listdir(_p(root, "/etc/cron.d"))] + [
            ("spool:" + n, _p(root, "/var/spool/cron/crontabs/" + n)) for n in _listdir(_p(root, "/var/spool/cron/crontabs"))]:
        data = _read(path)
        if data is not None:
            out[safe(label)[:80]] = _sha(data)
    return out


UNIT = re.compile(r"^[A-Za-z0-9:_.@\\-]+\.(service|timer|socket|path|mount|target)$")


def units(root="/"):
    """{unit name: sha256 or ""}: the units enabled by hand under /etc/systemd/system (the entries of its *.wants and *.requires folders), and
    the unit files that live there (a regular file, not a vendor unit): those are hashed, a link has "" (its target is the vendor's)."""
    base, out = _p(root, "/etc/systemd/system"), {}
    for n in _listdir(base):
        full = os.path.join(base, n)
        if n.endswith((".wants", ".requires")) and os.path.isdir(full):
            for u in _listdir(full):
                if UNIT.match(u):
                    out.setdefault(u, "")
        elif UNIT.match(n) and os.path.isfile(full) and not os.path.islink(full):
            data = _read(full)
            if data is not None:
                out[n] = _sha(data)
    return out


COLLECT = {"users": users, "keys": keys, "cron": cron, "units": units}


# ---- the baseline ----------------------------------------------------------------------------------------------------------

def _valid(sec, v):
    """A section of baseline.json is usable only in its own shape; anything else counts as not there."""
    if sec == "users":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if sec == "keys":
        return isinstance(v, dict) and all(isinstance(x, dict) and isinstance(x.get("sha256"), str) and isinstance(x.get("n"), int) for x in v.values())
    return isinstance(v, dict) and all(isinstance(x, str) for x in v.values())


def baseline_sections(net):
    """{section: data} of net.json that nuc-console-accept stores next to the ports (the ones this OS collected without an error)."""
    return {s: net[s] for s in SECTIONS if isinstance(net, dict) and net.get(s) is not None and _valid(s, net[s])}


def _names(items, cap=3):
    items = [safe(str(x))[:40] for x in items]
    return ", ".join(items[:cap]) + (f" (+{len(items) - cap})" if len(items) > cap else "")


def diff(sec, cur, base):
    """What changed in a section, in words, without any content; "" when nothing."""
    if sec == "users":
        add, rem = sorted(set(cur) - set(base)), sorted(set(base) - set(cur))
        parts = ([f"added {_names(add)}"] if add else []) + ([f"removed {_names(rem)}"] if rem else [])
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
    out, missing = [], []
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
        d = diff(sec, cur, old)
        if d:
            out.append((SECTION_SEV[sec], f"{LABEL[sec]} changed: {d}", SECTION_ID[sec]))
    if missing:
        out.append((1, f"baseline predates {', '.join(missing)}: run {accept_cmd} to include them (not tracked until then)", "baseline-incomplete"))
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
