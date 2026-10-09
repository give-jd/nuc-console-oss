"""tools/python_pins.py: what the python-pins job of the ai-pins workflow reads from the python-build-standalone release.

Nothing here touches the network: the GitHub API, the SHA256SUMS asset and the tarballs are served by a stand-in.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import unittest
import urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_release as br  # noqa: E402
import python_pins as pp  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_release import fake_python_tree, fake_tarball  # noqa: E402

TAG = "20991231"
TARGETS = list(br.STANDALONE_TARGETS.values())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def name(python, target, tag=TAG, flavour="install_only_stripped"):
    return "cpython-%s+%s-%s-%s.tar.gz" % (python, tag, target, flavour)


class FakeGithub:
    """Stands in for pp.Github: serves a release (tag, id) with `assets` (dicts name, size, digest), SHA256SUMS and the tarballs."""

    def __init__(self, assets, tarballs=None, sums=True, page=pp.PAGE, tag=TAG, rid=7):
        self.assets, self.tarballs, self.page, self.tag, self.rid = assets, tarballs or {}, page, tag, rid
        self.sums = "".join("%s  %s\n" % (sha(self.tarballs[a["name"]]) if a["name"] in self.tarballs else a.get("sha", "0" * 64), a["name"])
                            for a in assets) if sums else None
        self.requests = []

    def json(self, url):
        self.requests.append(url)
        if url.endswith("/releases/latest") or "/releases/tags/" in url:
            return {"tag_name": self.tag, "id": self.rid}
        m = re.search(r"/releases/%d/assets\?per_page=(\d+)&page=(\d+)" % self.rid, url)
        if not m:
            raise pp.PinsError("unexpected request " + url)
        per, page = int(m.group(1)), int(m.group(2))
        listing = [dict(a, browser_download_url="https://github.com/%s/releases/download/%s/%s" % (pp.REPO, self.tag, a["name"])) for a in self.assets]
        if self.sums is not None:
            listing.append({"name": "SHA256SUMS", "size": len(self.sums),
                            "browser_download_url": "https://github.com/%s/releases/download/%s/SHA256SUMS" % (pp.REPO, self.tag)})
        return listing[(page - 1) * per:page * per]

    def get(self, url, accept="", token=False):
        self.requests.append(url)
        if url.endswith("/%s/SHA256SUMS" % self.tag):
            return self.sums.encode()
        raise pp.PinsError("unexpected download " + url)

    def download(self, url, dest):
        self.requests.append(url)
        data = self.tarballs[url.rsplit("/", 1)[-1].replace("%2B", "+")]
        with open(dest, "wb") as f:
            f.write(data)
        return len(data), sha(data)


class Router:
    """Two releases: the latest, and an older one that a pin file may name."""

    def __init__(self, latest, old):
        self.latest, self.old = latest, old

    def pick(self, url):
        if "/releases/tags/%s" % self.old.tag in url or "/releases/%d/" % self.old.rid in url or "/%s/" % self.old.tag in url:
            return self.old
        return self.latest

    def json(self, url):
        return self.pick(url).json(url)

    def get(self, url, accept="", token=False):
        return self.pick(url).get(url, accept, token)


def release_assets(pythons=("3.13.5",), tarballs=None, tag=TAG, **kw):
    """Assets of a release: the four targets of each Python, and noise (other flavours, other targets, a pre-release)."""
    assets, files = [], dict(tarballs or {})
    for python in pythons:
        for target in TARGETS:
            n = name(python, target, tag)
            files.setdefault(n, fake_tarball(fake_python_tree(target, "3.99.1")))
            assets.append({"name": n, "size": len(files[n])})
    for noise in (name("3.13.5", TARGETS[0], flavour="install_only"), name("3.13.5", TARGETS[0], flavour="full"),
                  name("3.13.5", "x86_64-unknown-linux-musl"), name("3.13.5", "x86_64-pc-windows-msvc"), name("3.14.0rc2", TARGETS[0]),
                  name("3.13.9", TARGETS[0], tag="20990101"), "cpython-3.13.5+%s-%s-install_only_stripped.tar.gz.sha256" % (TAG, TARGETS[0])):
        assets.append({"name": noise, "size": 1})
    return FakeGithub(assets, files, tag=tag, rid=7 if tag == TAG else 8, **kw)


class Selection(unittest.TestCase):
    def test_the_newest_stable_313_with_all_four_targets(self):
        gh = release_assets(("3.13.2", "3.13.10", "3.13.5", "3.12.9"))
        found = pp.collect(gh)
        self.assertEqual((found["python"], found["release"], found["note"]), ("3.13.10", TAG, ""))  # as numbers: 10 > 5
        self.assertEqual([r["suffix"] for r in found["rows"]], list(br.STANDALONE_TARGETS))
        for r in found["rows"]:
            self.assertEqual(r["file"], name("3.13.10", br.STANDALONE_TARGETS[r["suffix"]]))
            self.assertRegex(r["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(r["source"], "SHA256SUMS")
            self.assertEqual(r["url"], "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (TAG, r["file"].replace("+", "%2B")))

    def test_a_python_that_lacks_a_target_is_not_chosen_and_312_is_the_fallback(self):
        gh = release_assets(("3.13.5", "3.12.9"))
        gh.assets = [a for a in gh.assets if not ("3.13.5" in a["name"] and "aarch64-apple-darwin" in a["name"])]  # 3.13.5 has no Apple silicon build
        found = pp.collect(FakeGithub(gh.assets, gh.tarballs))
        self.assertEqual((found["python"], found["note"]), ("3.12.9", "Python 3.13 has no complete set in release %s: using 3.12" % TAG))
        with self.assertRaises(pp.PinsError):
            pp.collect(FakeGithub(gh.assets, gh.tarballs), None, ("3.13",))  # --series 3.13 asks for 3.13 only

    def test_nothing_suitable_is_an_error(self):
        with self.assertRaisesRegex(pp.PinsError, "no install_only_stripped Python 3.13 or 3.12"):
            pp.collect(FakeGithub([{"name": name("3.11.9", TARGETS[0]), "size": 1}]))
        with self.assertRaisesRegex(pp.PinsError, "Python 3.9"):
            pp.collect(release_assets(("3.13.5",)), None, ("3.9",))

    def test_every_page_of_the_asset_list_is_read(self):
        gh = release_assets(("3.13.5",), page=2)
        gh.page = 2
        found = pp.collect(gh)
        self.assertEqual(found["python"], "3.13.5")
        pages = [r for r in gh.requests if "/assets?" in r]
        self.assertGreaterEqual(len(pages), 1)
        # a short list ends the paging; a full page asks for the next one
        many = FakeGithub([{"name": "other-%d.tar.gz" % i, "size": 1} for i in range(pp.PAGE + 5)] +
                          [{"name": name("3.13.5", t), "size": 3} for t in TARGETS])
        found = pp.collect(many)
        self.assertEqual(len([r for r in many.requests if "/assets?" in r]), 2)
        self.assertEqual(found["python"], "3.13.5")


class Hashes(unittest.TestCase):
    def test_the_sums_file_is_read_as_sha256sum_writes_it(self):
        text = "%s  a.tar.gz\n%s *b.tar.gz\nnot a hash line\n\n" % ("a" * 64, "B" * 64)
        self.assertEqual(pp.parse_sums(text), {"a.tar.gz": "a" * 64, "b.tar.gz": "b" * 64})
        with self.assertRaises(pp.PinsError):
            pp.parse_sums("%s  f\n%s  f\n" % ("a" * 64, "b" * 64))

    def test_the_asset_digest_must_agree_with_the_sums(self):
        a = {"name": "f", "size": 1, "digest": "sha256:" + "a" * 64}
        self.assertEqual(pp.sha256_of(a, {"f": "a" * 64}), ("a" * 64, "SHA256SUMS + asset digest"))
        self.assertEqual(pp.sha256_of(a, {}), ("a" * 64, "asset digest"))  # no sums entry: the digest alone
        self.assertEqual(pp.sha256_of({"name": "f"}, {"f": "c" * 64}), ("c" * 64, "SHA256SUMS"))
        with self.assertRaisesRegex(pp.PinsError, "SHA256SUMS says .* the asset digest says"):
            pp.sha256_of(a, {"f": "b" * 64})
        with self.assertRaisesRegex(pp.PinsError, "no SHA-256"):
            pp.sha256_of({"name": "f"}, {})  # never a guess
        with self.assertRaisesRegex(pp.PinsError, "not a SHA-256"):
            pp.sha256_of({"name": "f", "digest": "sha256:xyz"}, {})

    def test_a_release_without_a_hash_for_a_file_is_not_pinned(self):
        gh = release_assets(("3.13.5",), sums=False)
        with self.assertRaisesRegex(pp.PinsError, "no SHA-256"):
            pp.collect(gh)
        for a in gh.assets:
            a["digest"] = "sha256:" + sha(b"x")
        self.assertEqual(pp.collect(gh)["rows"][0]["source"], "asset digest")


class Output(unittest.TestCase):
    def found(self):
        return pp.collect(release_assets(("3.13.5",)))

    def test_the_json_is_what_the_build_reads_back(self):
        found = self.found()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "python-pins.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(pp.pin_json(found))
            python, release, pins = br.read_python_pins(path)  # the same checks the build makes
        self.assertEqual((python, release), ("3.13.5", TAG))
        for r in found["rows"]:
            self.assertEqual(pins[r["suffix"]], (r["file"], r["sha256"], r["size"]))
        self.assertEqual(json.loads(pp.pin_json(found))["_comment"], pp.COMMENT)
        self.assertTrue(pp.pin_json(found).endswith("}\n"))

    def test_the_text_and_the_summary_carry_file_size_hash_and_the_json(self):
        found = self.found()
        for text in (pp.render_text(found), pp.render_markdown(found)):
            for r in found["rows"]:
                self.assertIn(r["file"], text)
                self.assertIn(r["sha256"], text)
                self.assertIn(str(r["size"]), text)
            self.assertIn('"python": "3.13.5"', text)
        self.assertIn("| linux-x86_64 |", pp.render_markdown(found))
        self.assertIn("```json", pp.render_markdown(found))
        self.assertIn("tools/python-pins.json", pp.render_text(found))

    def test_the_comment_is_the_one_of_the_pin_file(self):
        with open(os.path.join(ROOT, "tools", "python-pins.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["_comment"], pp.COMMENT)


class CheckPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-pins-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gh = release_assets(("3.13.5",))
        self.latest = pp.collect(self.gh)
        self.path = os.path.join(self.tmp, "python-pins.json")
        self.log = []

    def write(self, found=None, **change):
        doc = pp.pin_document(found or self.latest)
        for k, v in change.items():
            if k.startswith("t_"):
                doc["targets"]["linux-arm64"][k[2:]] = v
            else:
                doc[k] = v
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def check(self):
        return pp.check_pins(self.gh, self.path, self.latest, self.log.append)

    def test_a_pin_file_that_is_the_release_is_ok(self):
        self.write()
        self.assertTrue(self.check())
        self.assertEqual(len([m for m in self.log if m.strip().startswith("ok ")]), 4)

    def test_a_different_hash_or_size_is_reported_and_fails(self):
        self.write(t_sha256="f" * 64)
        self.assertFalse(self.check())
        self.assertTrue([m for m in self.log if "DIFFERENT linux-arm64" in m])
        self.log.clear()
        self.write(t_size=self.latest["rows"][1]["size"] + 1)
        self.assertFalse(self.check())

    def test_the_placeholders_are_only_said_to_be_placeholders(self):
        self.path = os.path.join(ROOT, "tools", "python-pins.json")
        if not all(r is None for r in (json.load(open(self.path, encoding="utf-8"))["python"],)):
            self.skipTest("pinned")
        self.assertTrue(self.check())
        self.assertIn("not pinned yet", self.log[0])

    def test_an_older_pinned_release_is_looked_up_in_that_release_and_a_newer_python_is_mentioned(self):
        old = release_assets(("3.13.2",), tag="20990101")
        self.gh = Router(self.gh, old)
        self.write(pp.collect(old))  # pinned: Python 3.13.2 of the release 20990101; the latest is 3.13.5 of 20991231
        self.assertTrue(self.check())
        self.assertTrue([m for m in self.log if "a newer Python is available: 3.13.5 (release %s); pinned: 3.13.2" % TAG in m], self.log)
        self.assertEqual(len([m for m in self.log if m.strip().startswith("ok ")]), 4)
        self.log.clear()
        self.write(pp.collect(old), t_sha256="e" * 64)  # a hash that is not the one of that release
        self.assertFalse(self.check())
        self.assertTrue([m for m in self.log if "DIFFERENT linux-arm64" in m])
        self.log.clear()
        doc = pp.pin_document(pp.collect(old))
        doc["release"], doc["python"] = "20980101", "3.13.2"  # a release that does not have those files
        for t in doc["targets"].values():
            t["file"] = t["file"].replace("20990101", "20980101")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.assertFalse(self.check())
        self.assertTrue([m for m in self.log if "is not an asset of release 20980101" in m])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nuc-pins-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = []

    def run_main(self, gh, *args):
        return pp.main(["--pins", os.path.join(self.tmp, "none.json")] + list(args), gh=gh, log=self.log.append)

    def test_it_prints_writes_the_json_and_appends_to_the_summary(self):
        summary, js = os.path.join(self.tmp, "summary.md"), os.path.join(self.tmp, "pins.json")
        self.assertEqual(self.run_main(release_assets(("3.13.5",)), "--summary", summary, "--json", js), 0)
        with open(summary, encoding="utf-8") as f:
            self.assertIn("python-build-standalone %s, Python 3.13.5" % TAG, f.read())
        self.assertEqual(br.read_python_pins(js)[0], "3.13.5")
        self.assertIn("Paste this over tools/python-pins.json", "\n".join(self.log))

    def test_download_checks_the_bytes_and_reads_each_tarball_like_the_build(self):
        gh = release_assets(("3.13.5",))
        self.assertEqual(self.run_main(gh, "--download", os.path.join(self.tmp, "dl"), "--summary", ""), 0, self.log)
        self.assertEqual(len([m for m in self.log if "downloaded, size and SHA-256 are the release's" in m]), 4)
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "dl"))), 4)
        # a tarball whose bytes are not what the release says: reported, exit 1
        bad = release_assets(("3.13.5",))
        first = bad.assets[0]["name"]
        bad.tarballs[first] = bad.tarballs[first] + b"x"  # the sums were computed from the old bytes
        bad.sums = bad.sums  # unchanged on purpose
        self.log.clear()
        self.assertEqual(self.run_main(bad, "--download", os.path.join(self.tmp, "dl2"), "--summary", ""), 1)
        self.assertTrue([m for m in self.log if "PROBLEM" in m])
        # a tarball the build would refuse (no python/bin/python3): reported now, not at release time
        worse = release_assets(("3.13.5",), tarballs={name("3.13.5", TARGETS[2]): fake_tarball([("python", "dir", None, 0o755), ("python/x", "file", b"x", 0o644)])})
        self.log.clear()
        self.assertEqual(self.run_main(worse, "--download", os.path.join(self.tmp, "dl3"), "--summary", ""), 1)
        self.assertTrue([m for m in self.log if "no python/bin/python3" in m])

    def test_a_network_failure_is_an_error_not_a_traceback(self):
        class Down(FakeGithub):
            def json(self, url):
                raise pp.PinsError("%s: <urlopen error [Errno -2] Name or service not known>" % url)
        self.assertEqual(self.run_main(Down([]), "--summary", ""), 1)
        self.assertIn("python_pins: error:", self.log[-1])

    def test_only_https_and_the_token_only_to_the_api(self):
        with self.assertRaises(pp.PinsError):
            pp.https_only("http://github.com/x")
        seen = []

        class Opener:
            def __call__(self, req, timeout=None):
                seen.append(req)
                raise urllib.error.URLError("stop")
        gh = pp.Github("secret-token", Opener())
        for fn, args in ((gh.json, ("https://api.github.com/x",)), (gh.get, ("https://github.com/x/file",))):
            with self.assertRaises(pp.PinsError):
                fn(*args)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret-token")  # the API
        self.assertIsNone(seen[1].get_header("Authorization"))  # a download: public, the token is not sent to it


if __name__ == "__main__":
    unittest.main()
