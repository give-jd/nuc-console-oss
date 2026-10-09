"""`nuc-console-config migrate`: brings an installed config.ini to the layout the release ships (config.ini.dist, next to it) without losing a
setting (stdlib only, Python 3.8+).

The installers never overwrite config.ini, so an old installation keeps the long, commented file it started with. The new file is the shipped
one (config.ini.dist) with, on top of it, every key the person set to something other than the default (nuc_config.edit_lines puts the value in
place), their [webapps], [expose] and [ui] lines as they wrote them, and any key or section the new layout does not know under a `# legacy:`
line: nothing is dropped silently. Comments on the lines the person wrote stay; the full-line comments of the old layout are not carried
(the backup has them). The new file is written aside and read back with nuc_config.load(): when the configuration in force would differ in
any section, the old file stays. The old one is copied to config.ini.bak-YYYYMMDD-HHMMSS first. No privilege of its own: it writes where the
person can, like confedit.save. tests/test_confmigrate.py.
"""
import argparse
import configparser
import difflib
import os
import re
import stat
import sys
import tempfile
import time

import nuc_config

LAYOUT_RE = re.compile(r"^#\s*config-layout:\s*(\d+)\s*$", re.M)  # the line of config.ini.dist that says which layout it is
KEY_RE = re.compile(r"^([^\s=:#;\[][^=:]*?)\s*[=:]")
COMMENT_RE = re.compile(r"\s+([#;].*)$")
UI_DEFAULT_RE = re.compile(r"^#\s*([a-z_]+)\s*=")  # [ui] keys are listed commented out in the dist: a key set there is meant
KEEP = ("ui", "webapps", "expose")  # carried as the person wrote them: [ui] counts only the keys that are set, the others are free-form lines
LEGACY = "# legacy: not in the current layout, kept as it was"


def _parse(text):
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), strict=False)
    cp.read_string(text)
    return cp


def _header(ln):
    s = ln.strip()
    return s[1:s.index("]")].strip().lower() if s.startswith("[") and "]" in s else None


def _scan(lines):
    """{(section, key): the line as written} (the last one wins, as in configparser) and [(section, key)] in file order."""
    raw, order, sec = {}, [], None
    for ln in lines:
        h = _header(ln)
        if h is not None:
            sec = h
            continue
        m = KEY_RE.match(ln)
        if sec is not None and m:
            k = (sec, m.group(1).strip().lower())
            if k not in raw:
                order.append(k)
            raw[k] = ln.rstrip()
    return raw, order


def layout(text):
    m = LAYOUT_RE.search(text)
    return m.group(1) if m else None


def _insert(lines, section, new):
    """`new` lines after the last non-empty line of [section]; the section at the end when the file has none."""
    at, inside = None, False
    for i, ln in enumerate(lines):
        h = _header(ln)
        if h is not None:
            inside = h == section
        if inside and ln.strip():
            at = i + 1
    if at is None:
        lines += ["", "[%s]" % section] + new
    else:
        lines[at:at] = new


def _unstick(lines, section, key):
    """The inline comment of the shipped line of a key goes: the person's own takes its place."""
    sec = None
    for i, ln in enumerate(lines):
        h = _header(ln)
        if h is not None:
            sec = h
        elif sec == section and not ln.lstrip().startswith(("#", ";")):
            m = KEY_RE.match(ln)
            if m and m.group(1).strip().lower() == key:
                lines[i] = COMMENT_RE.sub("", ln)


def build(old, dist):
    """-> (the new file's text, how many full-line comments of the old file it does not carry)."""
    ocp, dcp = _parse(old), _parse(dist)
    lines = dist.splitlines()
    raw, _ = _scan(old.splitlines())
    ui_keys = {m.group(1) for m in map(UI_DEFAULT_RE.match, lines) if m}
    items, extra, fresh = {}, {}, {}
    for sec in ocp.sections():
        sec_l = sec.lower()
        for key in ocp.options(sec):
            r = raw.get((sec_l, key))
            line = r if r is not None else "%s = %s" % (key, ocp.get(sec, key).replace("\n", " "))
            known = dcp.has_section(sec) and dcp.has_option(sec, key)
            ui_known = key in ui_keys
            if sec_l in KEEP:
                out = [line] if sec_l != "ui" or ui_known else [LEGACY, line]
                extra.setdefault(sec_l, []).extend(out)
            elif known:
                v = ocp.get(sec, key).strip()
                if v == dcp.get(sec, key).strip():
                    continue
                m = COMMENT_RE.search(line.split("=", 1)[-1]) if r is not None else None
                if m:
                    _unstick(lines, sec_l, key)
                items.setdefault(sec_l, []).append((key, v + ("  " + m.group(1) if m else "")))
            elif dcp.has_section(sec):
                extra.setdefault(sec_l, []).extend([LEGACY, line])
            else:
                fresh.setdefault(sec_l, []).extend([LEGACY, line])
    for sec, its in items.items():
        lines = nuc_config.edit_lines(lines, sec, its)
    for sec, new in extra.items():
        _insert(lines, sec, new)
    for sec, new in fresh.items():
        lines += ["", "[%s]" % sec] + new
    shipped = {ln.strip() for ln in dist.splitlines()}
    lost = sum(1 for ln in old.splitlines() if ln.strip().startswith(("#", ";")) and ln.strip() not in shipped)
    return "\n".join(lines) + "\n", lost


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def needs_migration(path):
    """True when config.ini at `path` is not in the layout of the config.ini.dist next to it (a file that cannot be judged: False)."""
    try:
        old, dist = _read(path), _read(path + ".dist")
        if layout(dist) is not None:
            return layout(old) != layout(dist)
        return build(old, dist)[0] != old
    except (OSError, UnicodeDecodeError, configparser.Error):
        return False


def notice(path):
    sudo = "" if nuc_config.WINDOWS else "sudo "
    return "config.ini is in the old layout: run `%snuc-console-config migrate`" % sudo if needs_migration(path) else ""


def _differs(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def migrate(path, dry_run=False, yes=False, out=print, ask=input, load=nuc_config.load):
    """The command. -> exit code: 0 done, nothing to do or dry run; 1 refused or failed (the message says why; config.ini is as it was)."""
    if os.path.islink(path):
        out("refused: %s is a symbolic link (migrate the file it points to, by its own path)" % path)
        return 1
    try:
        old, dist = _read(path), _read(path + ".dist")
        with open(path, "rb") as f:
            old_bytes = f.read()
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError as e:
        out("nothing to migrate: %s does not exist" % e.filename)
        return 1
    except (OSError, UnicodeDecodeError) as e:
        out("cannot read %s: %s" % (path, e))
        return 1
    before = load(path, warn=lambda _: None)
    if before.get("config_error"):
        out("refused: config.ini cannot be read (%s): fix it by hand first" % before["config_error"][:120])
        return 1
    if layout(dist) is not None and layout(old) == layout(dist):
        out("already current: %s" % path)
        return 0
    try:
        new, lost = build(old, dist)
    except configparser.Error as e:
        out("refused: %s" % str(e)[:150])
        return 1
    if new == old:
        out("already current: %s" % path)
        return 0
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "config.ini", "config.ini (migrated)"))
    if dry_run:
        out(diff.rstrip("\n"))
        out("dry run: nothing was changed")
        return 0
    tmp = None
    bak = None
    try:
        # mkstemp: a name nobody can predict, created O_EXCL at 0600 (a planted link under a fixed name would be written through)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".config.ini.")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        after = load(tmp, warn=lambda _: None)
        bad = _differs(before, after)
        if after.get("config_error") or bad:
            out("refused: the migrated file would change the configuration (%s): config.ini is as it was" % (", ".join(bad) or "unreadable"))
            return 1
        if not yes:
            out(diff.rstrip("\n"))
            try:
                if ask("Replace %s with this (a copy is kept as .bak-*)? [y/N] " % path).strip().lower() not in ("y", "yes"):
                    out("cancelled: nothing was changed")
                    return 1
            except EOFError:
                out("cancelled: nothing was changed (no answer; --yes does not ask)")
                return 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1, 100):  # two runs in the same second: -2, -3 ...
            bak = "%s.bak-%s%s" % (path, stamp, "" if n == 1 else "-%d" % n)
            try:
                fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # the old file may name a token file: nobody else reads the copy
                break
            except FileExistsError:
                continue
        else:
            out("cannot write: too many backups of the same second")
            return 1
        with os.fdopen(fd, "wb") as f:
            f.write(old_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if hasattr(os, "chown"):  # the file keeps its owner and group (run with sudo, it would be root's otherwise)
            try:
                st = os.stat(path)
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        if os.path.islink(path):
            out("refused: %s became a symbolic link" % path)
            return 1
        os.replace(tmp, path)
        tmp = None
        if hasattr(os, "O_DIRECTORY"):  # the rename itself survives a power cut
            try:
                dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    except OSError as e:
        out("cannot write: %s (the same rights as editing config.ini are needed: sudo on Linux and macOS, an administrator prompt on Windows)" % e)
        return 1
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    out("migrated: %s (old file kept as %s)" % (path, os.path.basename(bak)))
    if lost:
        out("%d comment line(s) of the old layout were not carried: they are in the copy" % lost)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nuc-console-config", description="Maintenance of config.ini.")
    sub = ap.add_subparsers(dest="cmd")
    mg = sub.add_parser("migrate", help="bring config.ini to the layout of config.ini.dist, keeping every setting")
    mg.add_argument("--dry-run", action="store_true", help="print the diff, change nothing")
    mg.add_argument("--yes", "-y", action="store_true", help="do not ask")
    mg.add_argument("--config", help="the config.ini (default: the installed one)")
    nt = sub.add_parser("notice", help="print one line when config.ini is in the old layout (used by nuc-console-update)")
    nt.add_argument("--config")
    a = ap.parse_args(argv)
    if a.cmd is None:
        ap.print_help()
        return 2
    path = a.config or nuc_config.config_path()
    if a.cmd == "notice":
        line = notice(path)
        if line:
            print(line)
        return 0
    return migrate(path, a.dry_run, a.yes)


if __name__ == "__main__":
    sys.exit(main())
