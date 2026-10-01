#!/usr/bin/env python3
"""Reads the values to pin in tools/python-pins.json from the python-build-standalone release on GitHub (stdlib only, Python 3.8+).
Needs network access: the python-pins job of .github/workflows/ai-pins.yml runs it, and a maintainer pastes what it prints.

    python3 tools/python_pins.py [--series 3.13] [--release TAG] [--json FILE] [--summary FILE] [--download DIR] [--pins FILE]

  * takes the latest release of astral-sh/python-build-standalone (or --release TAG) and lists its `install_only_stripped`
    assets of the four targets (x86_64 and aarch64 Linux, aarch64 and x86_64 macOS) for the newest stable 3.13.x that all four
    have (3.12.x if there is none; --series X.Y chooses one): file name, size, SHA-256;
  * the SHA-256 comes from the release's SHA256SUMS; the asset's own digest (the GitHub API) must agree with it when there is one:
    a disagreement, or a file with no SHA-256 at all, is an error, never a guess;
  * prints them, and the JSON for tools/python-pins.json (also to --json FILE), and appends a table and that JSON to --summary FILE
    (default: $GITHUB_STEP_SUMMARY, the job summary of a workflow);
  * --download DIR also downloads the four tarballs, checks size and SHA-256 of the bytes against what the release says, and
    reads each one the way tools/build_release.py does (python/ folder, links, exec bits): what the build would refuse is reported now;
  * when --pins FILE (default tools/python-pins.json) is already filled, the pinned files are checked against the release that
    holds them (size, SHA-256): the exit code is 1 on any difference, and it says whether a newer Python is available.
It changes no file of the repository (--json and --summary write where you say), and sends GITHUB_TOKEN / GH_TOKEN (when set) to
api.github.com only: the downloads are public.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_release as br  # noqa: E402

REPO = "astral-sh/python-build-standalone"
API = "https://api.github.com/repos/%s" % REPO
SERIES = ("3.13", "3.12")  # the newest stable one that has all four targets
PAGE = 100
MAX_PAGES = 60  # a release holds a thousand assets or more
NAME = re.compile(r"cpython-(\d+)\.(\d+)\.(\d+)\+(\d{8})-(.+)-%s\.tar\.gz" % br.STANDALONE_FLAVOUR)  # a pre-release (3.14.0rc1) has no match
COMMENT = (  # the "_comment" of tools/python-pins.json (a test keeps the two equal)
    "The Python that the Linux and macOS archives carry: python-build-standalone (github.com/astral-sh/python-build-standalone), "
    "the install_only_stripped build, one tarball per target. null = NOT PINNED YET: tools/build_release.py refuses to build while "
    "any value is null, and so does the release workflow. Never type these values from memory: run the python-pins job of "
    ".github/workflows/ai-pins.yml (or python3 tools/python_pins.py on a machine with network access) and paste the JSON it prints "
    "over this file.")


class PinsError(Exception):
    pass


def https_only(url):
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname:
        raise PinsError("refusing a URL that is not HTTPS: %r" % (url,))
    return url


class Github:
    """The few requests this needs; `opener` is urllib.request.urlopen (a test passes a stand-in)."""

    def __init__(self, token=None, opener=urllib.request.urlopen):
        self.token, self.opener = token, opener

    def request(self, url, accept, token=False):
        headers = {"Accept": accept, "User-Agent": "nuc-console-python-pins", "X-GitHub-Api-Version": "2022-11-28"}
        if token and self.token:
            headers["Authorization"] = "Bearer " + self.token
        return urllib.request.Request(https_only(url), headers=headers)

    def get(self, url, accept="application/octet-stream", token=False):
        try:
            with self.opener(self.request(url, accept, token), timeout=120) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as e:
            raise PinsError("%s: %s" % (url, e))

    def json(self, url):
        try:
            return json.loads(self.get(url, "application/vnd.github+json", token=True).decode("utf-8"))
        except ValueError as e:
            raise PinsError("%s: not JSON (%s)" % (url, e))

    def download(self, url, dest):
        """-> (size, sha256) of what was written to dest."""
        h, size = hashlib.sha256(), 0
        try:
            with self.opener(self.request(url, "application/octet-stream"), timeout=300) as resp, open(dest, "wb") as f:
                for block in iter(lambda: resp.read(1 << 20), b""):
                    h.update(block)
                    size += len(block)
                    f.write(block)
        except (urllib.error.URLError, OSError) as e:
            raise PinsError("%s: %s" % (url, e))
        return size, h.hexdigest()


def release_assets(gh, release):
    """-> (tag, [asset dicts]) of the latest release, or of the release called `release`; every page of its asset list."""
    meta = gh.json("%s/releases/%s" % (API, "tags/" + quote(release, safe="") if release else "latest"))
    if not isinstance(meta, dict) or not isinstance(meta.get("tag_name"), str) or not isinstance(meta.get("id"), int):
        raise PinsError("the release answer has no tag_name and id")
    assets = []
    for page in range(1, MAX_PAGES + 1):
        chunk = gh.json("%s/releases/%d/assets?per_page=%d&page=%d" % (API, meta["id"], PAGE, page))
        if not isinstance(chunk, list):
            raise PinsError("the asset list is not a list")
        assets += [a for a in chunk if isinstance(a, dict)]
        if len(chunk) < PAGE:
            return meta["tag_name"], assets
    raise PinsError("more than %d assets: not read" % (PAGE * MAX_PAGES))


def parse_sums(text):
    """'<64 hex>  name' (or ' *name') lines of a SHA256SUMS file -> {name: hex}; a name listed twice with different hashes is refused."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(\S.*?)\s*", line)
        if not m:
            continue
        digest, name = m.group(1).lower(), m.group(2)
        if out.get(name, digest) != digest:
            raise PinsError("SHA256SUMS lists %s twice with different hashes" % name)
        out[name] = digest
    return out


def select(assets, tag, series):
    """-> (python 'X.Y.Z', {archive suffix: asset}) for the newest X.Y.Z of `series` ('3.13') that has an install_only_stripped
    asset for every target and the date of `tag`; None when there is none. Pre-releases (3.14.0rc1) never match."""
    found = {}
    for a in assets:
        m = NAME.fullmatch(a.get("name") or "")
        if not m or m.group(4) != tag or "%s.%s" % m.group(1, 2) != series:
            continue
        for suffix, triple in br.STANDALONE_TARGETS.items():
            if m.group(5) == triple:
                found.setdefault("%s.%s.%s" % m.group(1, 2, 3), {})[suffix] = a
    complete = [v for v, per in found.items() if len(per) == len(br.STANDALONE_TARGETS)]
    if not complete:
        return None
    best = max(complete, key=lambda v: tuple(int(x) for x in v.split(".")))
    return best, found[best]


def sha256_of(asset, sums):
    """-> (hex, where it comes from): SHA256SUMS first; the asset's digest ('sha256:...') must agree with it, and is used alone when
    the release has no SHA256SUMS entry for the file."""
    name = asset["name"]
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        digest = digest[len("sha256:"):].lower()
    else:
        digest = None
    listed = sums.get(name)
    if listed and digest and listed != digest:
        raise PinsError("%s: SHA256SUMS says %s, the asset digest says %s: not pinned" % (name, listed, digest))
    if not (listed or digest):
        raise PinsError("%s: no SHA-256 in SHA256SUMS or in the asset digest: not pinned" % name)
    if not re.fullmatch(r"[0-9a-f]{64}", listed or digest):
        raise PinsError("%s: not a SHA-256: %r" % (name, listed or digest))
    return (listed or digest), ("SHA256SUMS + asset digest" if listed and digest else "SHA256SUMS" if listed else "asset digest")


def release_sums(gh, assets):
    """{file name: SHA-256} from the SHA256SUMS asset of the release ({} when it has none: the asset digests are used then)."""
    asset = next((a for a in assets if a.get("name") == "SHA256SUMS"), None)
    if not asset:
        return {}
    return parse_sums(gh.get(https_only(asset.get("browser_download_url"))).decode("utf-8", "replace"))


def row_of(asset, sums, tag, suffix="", target=""):
    digest, source = sha256_of(asset, sums)
    if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
        raise PinsError("%s: no size" % asset["name"])
    return {"suffix": suffix, "target": target, "file": asset["name"], "size": asset["size"], "sha256": digest, "source": source,
            "url": br.STANDALONE_URL.format(release=tag, file=quote(asset["name"]))}


def collect(gh, release=None, series=SERIES):
    """-> {"python", "release", "note", "rows": [{suffix, target, file, size, sha256, source, url}]} for the newest stable Python."""
    tag, assets = release_assets(gh, release)
    chosen, used = None, None
    for s in series:
        chosen, used = select(assets, tag, s), s
        if chosen:
            break
    if not chosen:
        raise PinsError("release %s has no install_only_stripped Python %s for all of %s" % (
            tag, " or ".join(series), ", ".join(br.STANDALONE_TARGETS.values())))
    python, per = chosen
    note = "" if used == series[0] else "Python %s has no complete set in release %s: using %s" % (series[0], tag, used)
    sums = release_sums(gh, assets)
    rows = [row_of(per[suffix], sums, tag, suffix, triple) for suffix, triple in br.STANDALONE_TARGETS.items()]
    return {"python": python, "release": tag, "note": note, "rows": rows}


def pin_document(found):
    """The content of tools/python-pins.json for what was found (read_python_pins reads it back)."""
    return {"_comment": COMMENT, "python": found["python"], "release": found["release"],
            "targets": {r["suffix"]: {"target": r["target"], "file": r["file"], "sha256": r["sha256"], "size": r["size"]} for r in found["rows"]}}


def pin_json(found):
    """Compact enough to read, one target per line, like the file in the repository."""
    doc = pin_document(found)
    lines = ["{", '  "_comment": %s,' % json.dumps(doc["_comment"]), '  "python": %s,' % json.dumps(doc["python"]),
             '  "release": %s,' % json.dumps(doc["release"]), '  "targets": {']
    items = list(doc["targets"].items())
    for i, (suffix, t) in enumerate(items):
        lines.append('    %s: %s%s' % (json.dumps(suffix), json.dumps(t), "," if i < len(items) - 1 else ""))
    return "\n".join(lines + ["  }", "}"]) + "\n"


def render_text(found):
    out = ["python-build-standalone release %s: Python %s (%s)" % (found["release"], found["python"], br.STANDALONE_FLAVOUR)]
    if found["note"]:
        out.append("note: " + found["note"])
    for r in found["rows"]:
        out.append("  %-13s %s" % (r["suffix"], r["file"]))
        out.append("  %-13s size %d  sha256 %s  (%s)" % ("", r["size"], r["sha256"], r["source"]))
        out.append("  %-13s %s" % ("", r["url"]))
    out += ["", "Paste this over tools/python-pins.json:", "", pin_json(found).rstrip("\n")]
    return "\n".join(out) + "\n"


def render_markdown(found):
    out = ["## python-build-standalone %s, Python %s" % (found["release"], found["python"]), ""]
    if found["note"]:
        out += ["> " + found["note"], ""]
    out += ["| Archive | File | Size | SHA-256 | From |", "|---|---|---|---|---|"]
    for r in found["rows"]:
        out.append("| %s | `%s` | %d | `%s` | %s |" % (r["suffix"], r["file"], r["size"], r["sha256"], r["source"]))
    out += ["", "Paste over `tools/python-pins.json`:", "", "```json", pin_json(found).rstrip("\n"), "```", ""]
    return "\n".join(out)


def check_pins(gh, path, latest, log=print):
    """Checks a filled pin file against the release that holds its files. -> True when every pinned file is as pinned (False: a
    difference, or not checkable). A file that is not pinned yet (null) is only said so."""
    try:
        python, release, pins = br.read_python_pins(path)
    except br.ReleaseError as e:
        if "not pinned yet" in str(e):
            log("pin file %s: not pinned yet (every value is null): paste the JSON above" % path)
            return True
        log("pin file %s: %s" % (path, e))
        return False
    log("pin file %s: Python %s, release %s" % (path, python, release))
    if release == latest["release"]:
        rows = {r["file"]: r for r in latest["rows"]}
    else:  # an older release than the latest: the pinned files are looked up there
        tag, assets = release_assets(gh, release)
        sums = release_sums(gh, assets)
        by_name = {a.get("name"): a for a in assets}
        rows = {}
        for file, _digest, _size in pins.values():
            if file in by_name:
                rows[file] = row_of(by_name[file], sums, tag)
    ok = True
    for suffix, (file, digest, size) in pins.items():
        r = rows.get(file)
        if not r:
            log("  DIFFERENT %s: %s is not an asset of release %s" % (suffix, file, release))
            ok = False
        elif r["sha256"] != digest or r["size"] != size:
            log("  DIFFERENT %s: the release has size %d sha256 %s, the pin has size %d sha256 %s" % (suffix, r["size"], r["sha256"], size, digest))
            ok = False
        else:
            log("  ok %s: %s (size and SHA-256 are the release's)" % (suffix, file))
    if ok and (python, release) != (latest["python"], latest["release"]):
        log("a newer Python is available: %s (release %s); pinned: %s (release %s)" % (latest["python"], latest["release"], python, release))
    return ok


def download_and_inspect(gh, found, folder, log=print):
    """Downloads the four tarballs into folder, compares size and SHA-256 with the release and reads them like the build does."""
    os.makedirs(folder, exist_ok=True)
    ok = True
    for r in found["rows"]:
        dest = os.path.join(folder, r["file"])
        try:
            size, digest = gh.download(r["url"], dest)
            if (size, digest) != (r["size"], r["sha256"]):
                raise PinsError("the download has size %d sha256 %s, the release says size %d sha256 %s" % (size, digest, r["size"], r["sha256"]))
            with open(dest, "rb") as f:
                entries = br.standalone_python(f.read(), "nuc-console-X", source=r["file"])
        except (PinsError, br.ReleaseError, OSError) as e:
            log("  PROBLEM %s: %s" % (r["suffix"], e))
            ok = False
            continue
        files = sum(1 for e in entries if isinstance(e.data, bytes))
        links = sum(1 for e in entries if isinstance(e.data, br.Link))
        folders = sum(1 for e in entries if e.data is None)
        log("  ok %s: downloaded, size and SHA-256 are the release's; python/ holds %d files, %d folders, %d links (%d bytes unpacked)"
            % (r["suffix"], files, folders, links, sum(len(e.data) for e in entries if isinstance(e.data, bytes))))
    return ok


def main(argv=None, gh=None, log=print):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--series", help="X.Y: only this Python (default: the newest 3.13.x, else 3.12.x)")
    p.add_argument("--release", metavar="TAG", help="a release tag of python-build-standalone (default: the latest release)")
    p.add_argument("--json", metavar="FILE", help="also write the JSON for tools/python-pins.json here")
    p.add_argument("--summary", metavar="FILE", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                   help="append a Markdown table and the JSON here (default: $GITHUB_STEP_SUMMARY)")
    p.add_argument("--download", metavar="DIR", help="download the four tarballs into DIR and check them")
    p.add_argument("--pins", metavar="FILE", default=os.path.join(br.ROOT, br.PINS_FILE), help="the pin file to check (default: tools/python-pins.json)")
    a = p.parse_args(argv)
    gh = gh or Github(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    try:
        found = collect(gh, a.release, (a.series,) if a.series else SERIES)
    except PinsError as e:
        log("python_pins: error: %s" % e)
        return 1
    log(render_text(found))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(pin_json(found))
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(render_markdown(found))
    ok = True
    if os.path.exists(a.pins):
        try:
            ok = check_pins(gh, a.pins, found, log)
        except PinsError as e:
            log("python_pins: error: %s" % e)
            ok = False
    if a.download:
        log("downloading the tarballs into %s" % a.download)
        ok = download_and_inspect(gh, found, a.download, log) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
