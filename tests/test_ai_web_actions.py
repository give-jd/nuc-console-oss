"""What the AI page and the AI screen DO (src/aiweb.py, and its two front ends in src/web.py and src/render.py): the engine's jobs (download
with progress, cancel, never twice, delete, start and stop of the model server, the chat), the on/off switch and web.json, the lock, and
the demo; then the POST endpoints of the web view (CSRF, Origin, token, body, redirect), the page and its forms, and the console keys.

Hermetic: a fake loopback server stands in for GitHub (the server's archive, allow_loopback_http), the fake Ollama of fakeollama.py for the
model server (it pulls from its fake registry), the fake OpenAI-compatible server of test_advisor.py for the chat, a temporary AI folder; no test
waits for the clock (events and joins). Platform-aware: the tests that start the model server are for Unix (/bin/sh, process groups, signals); on
Windows, where the engine starts ollama.exe, the fake one is Python source that Base.popen runs with this interpreter. macOS runners are arm64,
and a temporary folder's parents are not always readable by others.
"""
import contextlib
import hashlib
import html
import http.client
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import re
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import advisor  # noqa: E402
import aiollama  # noqa: E402
import aiweb  # noqa: E402
import aisetup  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ansi  # noqa: E402
import web  # noqa: E402
import webhttp  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402
import test_advisor as ta  # noqa: E402  (the fake model server, the history and the report of that file)
import test_aisetup as tas  # noqa: E402  (the fake download server, the fake advice of aihw)
import fakeollama as fo  # noqa: E402  (the fake model server)

POSIX = os.name == "posix"
unix_only = unittest.skipUnless(POSIX, "the model server started and stopped on Unix: /bin/sh, process groups, signals")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    n = s.getsockname()[1]
    s.close()
    return n


class Base(unittest.TestCase):
    """An engine on a temporary AI folder with a fake catalog (the server's build and two models), the fake download server, and the fake
    Ollama that the engine starts (it pulls the two models from its fake registry)."""

    SIZES = {"tiny": 2_600_000, "other": 30_000}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "home", "ai")   # = aisetup.work_dir() with the NUC_CONSOLE_HOME below: the engine's folder is the catalog's and web.json's
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": os.path.join(self.tmp, "home"), "NUC_CONSOLE_CONFIG": os.path.join(self.tmp, "config.ini")})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = nuc_config.load("/nonexistent")
        self.archive = fo.runtime_archive()
        self.dl = tas.Server()
        self.httpd = self.dl.__enter__()
        self.addCleanup(self.dl.__exit__)
        self.httpd.files = {"/" + self.archive[0]: self.archive[1]}
        self.runtime = fo.fake_runtime(self.dl.base, self.archive)[0]
        self.models = [fo.fake_model(name, size, rank, approx_mb=3, ram_mb=100) for rank, (name, size) in enumerate(self.SIZES.items(), 1)]
        self.hw = {}
        self.popens = []
        port = mock.patch.object(aisetup, "DEFAULT_PORT", free_port())  # the servers of these tests start where no other test run (or program) is
        port.start()
        self.addCleanup(port.stop)
        self.eng = aiweb.Engine(directory=self.d, runtime=self.runtime, models=self.models, allow_loopback_http=True, cfg_fn=lambda: self.cfg,
                                hw_fn=lambda: self.hw, popen=self.popen, state_fn=lambda: None)  # no state of the machine unless a test gives one
        self.eng.start_wait, self.eng.stop_grace, self.eng.poll_s, self.eng.answer_wait, self.eng.load_wait = 0.2, 0.5, 0.05, 30, 30
        self.addCleanup(self.eng.shutdown)
        self.control()

    def control(self, **kw):
        """What the fake Ollama can pull (the two models) and how it misbehaves (fakeollama.py)."""
        fo.write_control(self.d, registry=fo.registry_for(self.models), **kw)

    def popen(self, argv, **kw):
        self.popens.append((argv, kw))
        if not POSIX and argv and argv[0].lower().endswith("ollama.exe"):
            argv = [sys.executable] + list(argv)  # the fake ollama.exe is Python source (fakeollama.py): this interpreter runs it
        return subprocess.Popen(argv, **kw)

    def m(self, name="tiny"):
        return next(m for m in self.models if m["id"] == name)

    def install(self, names=("tiny", "other")):
        fo.install_runtime(self.d, self.runtime)
        for n in names:
            fo.install_model(self.d, self.m(n))
        self.control()

    def installed(self, name="tiny"):
        return aisetup.model_ready(self.d, self.m(name))

    def pulls(self):
        return [r["body"]["model"] for r in fo.requests_seen(self.d) if r["path"] == "/api/pull"]

    def job(self):
        if not self.eng.wait(30):
            self.fail("the job did not end: %s" % self.where())
        return self.eng.snapshot()["job"]

    def where(self):
        """What a job that does not end is doing: its phase, the server's last lines and what its endpoint answers."""
        info = self.eng.child_info or {}
        pid = info.get("pid")
        snap = self.eng.snapshot()
        job, srv = snap["job"] or {}, snap["server"]
        probe = aisetup.probe(srv["endpoint"], 2) if srv["endpoint"] else None
        return "phase %r, step %r, note %r; server running %s, pid %s, endpoint %r, alive %s; it wrote: %s; the endpoint: %r" % (
            job.get("phase"), job.get("step"), job.get("note"), srv["running"], pid, srv["endpoint"], bool(pid and POSIX and alive(pid)),
            " | ".join(info.get("tail") or []) or "nothing", probe)

    def notice(self):
        return self.eng.snapshot()["notice"]["text"]

    def wait_phase(self, phase):
        """Until the job that runs is in this phase (bounded: short waits on an event, not a guess of how long it takes)."""
        gate = threading.Event()
        for _ in range(1200):
            job = self.eng.snapshot()["job"]
            if job and job["phase"] == phase or job and job["state"] != "running":
                break
            gate.wait(0.01)
        return self.eng.snapshot()["job"]

    def requests(self):
        return [p for p, _r in self.httpd.requests]


# ------------------------------------------------------------------------------------------------------------- the one action

@unix_only
class UseModel(Base):
    """Choosing a model does everything: fetch the server if it is missing, start it, pull the model, load it, make it the advisor's, turn it on."""

    def use(self, name="tiny"):
        ok, text = self.eng.use_model(name)
        self.assertTrue(ok, text)
        return self.job()

    def answering(self):
        srv = self.eng.snapshot()["server"]
        ok, ids, why = aisetup.probe(srv["endpoint"], 2)
        self.assertTrue(ok, why)
        return srv, ids

    def test_one_choice_downloads_starts_pulls_loads_and_turns_the_advisor_on(self):
        job = self.use("tiny")
        self.assertEqual((job["state"], job["error"]), ("done", ""), self.where())
        self.assertIn("tiny is in use", job["note"])
        self.assertTrue(self.installed("tiny"))
        self.assertEqual(self.requests(), ["/" + self.archive[0]], "the server's build, once")
        self.assertEqual(self.pulls(), ["tiny:test"], "the model, through the server")
        srv, ids = self.answering()
        self.assertEqual((srv["running"], srv["model"], ids), (True, "tiny", ["tiny"]), "it answers when the job is done")
        loads = [r["body"] for r in fo.requests_seen(self.d) if r["path"] == "/api/generate"]
        self.assertEqual(loads, [{"model": "tiny"}], "loaded once, before the job says it is done")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"enabled": True, "model": "tiny", "endpoint": srv["endpoint"]})
        snap = self.eng.snapshot()
        self.assertEqual(snap["switch"], {"on": True, "by": "web", "locked": False, "can_off": True})
        self.assertEqual(snap["state"][0], "running")
        self.assertIn("tiny runs here and answers at %s" % srv["endpoint"], snap["state"][1])
        self.assertEqual(self.eng.ecfg()["ai"]["model"], "tiny")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "config.ini")))
        argv, kw = self.popens[0]
        self.assertEqual(argv[-2:], [aisetup.runtime_path(self.d, self.runtime), "serve"])
        self.assertEqual(kw["env"]["OLLAMA_HOST"], "127.0.0.1:%d" % aisetup.port_of(srv["endpoint"]))
        self.assertEqual(kw["env"]["OLLAMA_MODELS"], os.path.join(self.d, "models"))
        self.assertEqual(kw["env"]["HOME"], os.path.join(self.d, "home"), "its key goes to the AI folder: the web account has no home")

    def test_the_progress_is_the_jobs_while_it_runs(self):
        phases = []
        self.eng.progress_hook = lambda job: phases.append((job["step"], job["phase"], aiweb.job_text(job)))
        self.use("tiny")
        self.assertTrue(any(step == "runtime" for step, _p, _t in phases))
        self.assertTrue(any("downloading the model" in text for _s, _p, text in phases))
        self.assertTrue(any(p == "verifying" and s == "tiny" for s, p, _t in phases), "Ollama's own check of the layers")

    def test_the_same_model_again_is_not_fetched_or_started_again(self):
        self.use("tiny")
        pid, n = self.eng.snapshot()["server"]["pid"], len(self.httpd.requests)
        job = self.use("tiny")
        self.assertEqual(job["state"], "done")
        self.assertEqual(self.eng.snapshot()["server"]["pid"], pid, "the server runs: nothing to start")
        self.assertEqual((len(self.httpd.requests), self.pulls()), (n, ["tiny:test"]))
        self.assertEqual(len(self.popens), 1)

    def test_another_model_is_loaded_by_the_same_server(self):
        self.use("tiny")
        first = self.eng.snapshot()["server"]["pid"]
        self.use("other")
        srv, ids = self.answering()
        self.assertEqual(srv["model"], "other")
        self.assertEqual(sorted(ids), ["other", "tiny"], "the server serves every installed model")
        self.assertEqual(srv["pid"], first, "one server: it loads the other model (and unloads the first, one at a time)")
        self.assertEqual(len(self.popens), 1)
        self.assertEqual(advisor.read_web_state(self.eng.web_path())["model"], "other")
        self.assertEqual(self.requests(), ["/" + self.archive[0]], "the server is fetched once")
        self.assertEqual(self.pulls(), ["tiny:test", "other:test"])

    def test_a_download_that_fails_leaves_the_advisor_as_it_was(self):
        self.httpd.files.clear()
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("HTTP 404", job["error"])
        self.assertEqual(self.popens, [], "no server without its build")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {})
        self.assertEqual(self.eng.snapshot()["state"][0], "off")

    def test_a_pull_that_fails_says_why_and_stops_the_server_it_started(self):
        self.control(pull_error="pull model manifest: file does not exist")
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("the model server says: pull model manifest: file does not exist", job["error"])
        self.assertFalse(self.installed("tiny"))
        self.assertTrue(self.eng.child_info["gone"].wait(30), "a server without a model would only hold memory")
        self.assertNotIn("enabled", advisor.read_web_state(self.eng.web_path()))
        self.assertEqual(self.eng.snapshot()["state"][0], "off")
        self.assertTrue(aisetup.runtime_ready(self.d, self.runtime), "the server stays installed: the next try only pulls")

    def test_a_server_that_dies_while_it_loads_fails_the_job_and_the_advisor_stays_off(self):
        self.install()
        self.control(load_die=True)
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("stopped while it loaded the model (exit status 4)", job["error"])
        self.assertIn("out of memory", job["error"])
        st = advisor.read_web_state(self.eng.web_path())
        self.assertNotIn("enabled", st)
        self.assertNotIn("endpoint", st, "nobody answers there")
        self.assertEqual(self.eng.snapshot()["state"][0], "off")

    def test_a_model_the_server_cannot_load_is_said(self):
        self.install()
        self.control(load_error="model requires more system memory (12 GiB) than is available (4 GiB)")
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("tiny could not be loaded: the model server says: model requires more system memory", job["error"])
        self.assertTrue(self.eng.child_info["gone"].wait(30))
        self.assertEqual(self.eng.snapshot()["state"][0], "off")

    def test_cancel_while_the_model_loads_stops_the_server_it_started(self):
        self.install()
        self.control(load_hold=True)
        self.assertTrue(self.eng.use_model("tiny")[0])
        job = self.wait_phase("loading")
        self.assertEqual(job["phase"], "loading")
        info = self.eng.child_info
        self.assertTrue(self.eng.cancel()[0])
        job = self.job()
        self.assertEqual(job["state"], "cancelled")
        self.assertTrue(info["gone"].wait(30))
        self.assertFalse(alive(info["pid"]))
        self.assertFalse(self.eng.snapshot()["server"]["running"])
        self.assertNotIn("enabled", advisor.read_web_state(self.eng.web_path()))

    def test_a_server_that_never_answers_is_stopped_and_said(self):
        self.install()
        self.control(mute=True)
        self.eng.answer_wait = 1.0
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("did not answer within 1 s", job["error"])
        self.assertIn("loading", job["error"], "its last words")
        self.assertFalse(self.eng.snapshot()["server"]["running"])

    def test_turn_off_stops_the_server_and_the_advisor_and_turn_on_brings_the_same_model_back_without_a_download(self):
        self.use("tiny")
        info, n = self.eng.child_info, len(self.httpd.requests)
        self.assertTrue(self.eng.turn_off()[0])
        self.assertTrue(info["gone"].wait(30))
        snap = self.eng.snapshot()
        self.assertEqual((snap["switch"]["on"], snap["server"]["running"], snap["state"][0]), (False, False, "off"))
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"enabled": False, "model": "tiny"}, "what it was is remembered, the switch is off")
        ok, text = self.eng.turn_on()
        self.assertTrue(ok, text)
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.eng.snapshot()["state"][0], "running")
        self.assertEqual(self.eng.snapshot()["server"]["model"], "tiny")
        self.assertEqual((len(self.httpd.requests), self.pulls()), (n, ["tiny:test"]), "nothing fetched again")

    def test_a_model_that_will_not_work_here_is_refused_before_anything_is_fetched(self):
        hw = tas.hw_of(8192, 5500)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            self.eng.hw_fn = lambda: hw
            self.eng.models[0] = dict(self.m("tiny"), approx_mb=30000, ram_mb=31000)
            job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("will not work on this machine", job["error"])
        self.assertEqual((self.httpd.requests, self.popens), ([], []))

    def test_what_turn_on_will_use_and_what_it_asks_first(self):
        hw = tas.hw_of(65536, 60000)
        self.eng.hw_fn = lambda: hw
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            ch = self.eng.choice()
            total = len(self.archive[1]) + self.SIZES["tiny"]
            self.assertEqual(ch, {"model": None, "by": "", "recommended": "tiny", "target": "tiny", "size": total, "installed": False})
            ok, text = self.eng.turn_on()
            self.assertFalse(ok)
            self.assertIn("no model is chosen yet", text)
            self.assertEqual(self.httpd.requests, [], "nothing is fetched on a guess: the page or the screen asks about the recommended one first")
            self.cfg["ai"]["model"] = "other"   # config.ini names one
            ch = self.eng.choice()
            self.assertEqual((ch["model"], ch["by"], ch["target"]), ("other", "config", "other"))
            self.eng._web(lambda st: st.update(model="tiny"))   # the page's choice wins
            ch = self.eng.choice()
            self.assertEqual((ch["model"], ch["by"]), ("tiny", "page"))
            self.cfg["ai"]["model"] = "qwen3:8b"   # a name only that server knows: not one of the catalog
            self.eng._web(lambda st: st.pop("model"))
            self.assertEqual(self.eng.choice()["model"], None)
            self.install(("tiny",))
            self.eng._web(lambda st: st.update(model="tiny"))
            ch = self.eng.choice()
            self.assertEqual((ch["installed"], ch["size"]), (True, 0))
            self.assertTrue(self.eng.turn_on()[0])
            self.assertEqual(self.job()["state"], "done")
            self.assertEqual((self.requests(), self.pulls()), ([], []), "installed: not one download")

    def test_the_size_to_fetch_counts_what_is_partly_there_and_says_unknown_when_not_pinned(self):
        part = aisetup.archive_path(self.d, self.runtime) + ".part"
        os.makedirs(os.path.dirname(part))
        tas.put(part, b"p" * 1000)
        self.assertEqual(self.eng._missing_bytes(self.d, self.m("tiny")), len(self.archive[1]) - 1000 + self.SIZES["tiny"])
        self.assertIsNone(self.eng._missing_bytes(self.d, dict(self.m("tiny"), ollama=None)))
        self.assertIsNone(self.eng._missing_bytes(self.d, self.m("tiny")) if False else None)
        self.eng.runtime = dict(self.runtime, assets={})
        self.assertIsNone(self.eng._missing_bytes(self.d, self.m("tiny")), "no build for this system")


# ---------------------------------------------------------------------------------------------------------------------- download

@unix_only
class Download(Base):
    def test_download_runtime_and_model_with_progress_and_the_checks_of_the_command(self):
        seen = []
        self.eng.progress_hook = lambda job: seen.append((job["step"], job["done"], job["total"], job["phase"]))
        ok, text = self.eng.download("tiny")
        self.assertTrue(ok, text)
        self.assertIn("downloading tiny", text)
        job = self.job()
        self.assertEqual((job["state"], job["error"]), ("done", ""), self.where())
        self.assertEqual(job["pct"], 100)
        total = len(self.archive[1]) + self.SIZES["tiny"]
        self.assertEqual((job["done"], job["total"]), (total, total))
        self.assertEqual([s for s, *_ in seen][0], "runtime", "the server first")
        self.assertEqual(seen[-1][0], "tiny")
        dones = [d for _s, d, _t, _p in seen]
        self.assertEqual(dones, sorted(dones), "progress only goes forward")
        self.assertGreater(len([x for x in seen if x[0] == "tiny"]), 2, "the model is several chunks: there is progress to show")
        self.assertEqual({t for s_, _d, t, _p in seen if s_ == "tiny"}, {total})
        self.assertIn("verifying", {p for s_, *_x, p in seen if s_ == "runtime"}, "after the last byte the SHA-256 of the archive is checked")
        self.assertIn("verifying", {p for s_, *_x, p in seen if s_ == "tiny"}, "and Ollama checks the layers")
        self.assertTrue(self.installed("tiny"))
        self.assertTrue(aisetup.runtime_ready(self.d, self.runtime, rehash=True))
        self.assertTrue(aisetup.model_ready(self.d, self.m(), rehash=True))
        self.assertIn("tiny is installed", self.notice())
        if POSIX:
            self.assertEqual(os.stat(aisetup.runtime_path(self.d, self.runtime)).st_mode & 0o777, 0o755)
        self.assertEqual(self.requests(), ["/" + self.archive[0]])
        self.assertEqual(self.pulls(), ["tiny:test"])
        self.assertTrue(self.eng.child_info["gone"].wait(30), "the server started for the download is stopped after it")
        self.assertEqual(self.eng.snapshot()["state"][0], "off")
        self.assertNotIn("endpoint", advisor.read_web_state(self.eng.web_path()))

    def test_a_download_goes_through_the_server_that_runs(self):
        self.install(("tiny",))
        self.eng.start_server("tiny")
        self.assertEqual(self.job()["state"], "done")
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.download("other")
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.pulls(), ["other:test"])
        self.assertEqual((self.eng.snapshot()["server"]["pid"], len(self.popens)), (pid, 1), "the same server, still running")

    def test_a_file_that_is_there_is_never_fetched_again(self):
        self.eng.download("tiny")
        self.job()
        n = len(self.httpd.requests)
        ok, _t = self.eng.download("tiny")
        self.assertTrue(ok)
        job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("already installed", job["note"])
        self.assertEqual((len(self.httpd.requests), self.pulls()), (n, ["tiny:test"]), "not one request")
        self.assertEqual(len(self.popens), 1, "not even a server started to look")
        self.eng.download("other")  # a second model: only its own pull, the server is there
        self.job()
        self.assertEqual((self.requests()[n:], self.pulls()), ([], ["tiny:test", "other:test"]))

    def test_one_job_at_a_time(self):
        reached, release = threading.Event(), threading.Event()

        def hook(job):
            reached.set()
            release.wait(30)
        self.eng.progress_hook = hook
        self.assertTrue(self.eng.download("tiny")[0])
        self.assertTrue(reached.wait(30))
        try:
            for call in (lambda: self.eng.download("other"), self.eng.delete_all, lambda: self.eng.delete("tiny"), lambda: self.eng.start_server("tiny")):
                ok, text = call()
                self.assertFalse(ok)
                self.assertIn("busy: downloading tiny", text)
            self.assertEqual(self.eng.snapshot()["job"]["state"], "running")
        finally:
            release.set()
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.pulls(), ["tiny:test"])

    def test_cancel_of_the_servers_archive_keeps_what_was_fetched_and_the_next_download_goes_on_from_there(self):
        big = fo.runtime_archive(fo.runtime_exe() + b"\n#" + tas.blob(3_000_000, b"pad").hex().encode())
        self.httpd.files = {"/" + big[0]: big[1]}
        self.eng.runtime = fo.fake_runtime(self.dl.base, big)[0]
        self.eng.progress_hook = lambda job: self.eng.cancel() if job["step"] == "runtime" and job["done"] > 0 else None
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "cancelled")
        self.assertIn("kept", job["note"])
        part = aisetup.archive_path(self.d, self.eng.runtime) + ".part"
        size = os.path.getsize(part)
        self.assertTrue(0 < size < len(big[1]), size)
        self.assertIn("cancelled", self.notice())
        self.eng.progress_hook = None
        self.eng.download("tiny")
        self.assertEqual(self.job()["state"], "done", self.where())
        self.assertIn(("/" + big[0], "bytes=%d-" % size), self.httpd.requests)
        self.assertTrue(self.installed("tiny"))

    def test_cancel_of_a_pull_keeps_what_was_pulled_and_stops_the_server_started_for_it(self):
        self.install(())
        self.control(pull_hold=True)
        self.eng.progress_hook = lambda job: self.eng.cancel() if job["step"] == "tiny" and job["done"] > 0 else None
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "cancelled", self.where())
        self.assertFalse(self.installed("tiny"))
        partial = [f for f in os.listdir(os.path.join(self.d, "models", "blobs")) if f.endswith("-partial")]
        self.assertEqual(len(partial), 1, "what was pulled stays: the server resumes it")
        self.assertTrue(self.eng.child_info["gone"].wait(30))
        self.eng.progress_hook = None
        self.control()
        self.eng.download("tiny")
        self.assertEqual(self.job()["state"], "done")
        self.assertTrue(self.installed("tiny"))

    def test_cancel_without_a_job_and_of_a_job_that_is_not_a_download(self):
        self.assertEqual(self.eng.cancel(), (False, "nothing to cancel"))
        self.install()
        reached, release = threading.Event(), threading.Event()
        real = aisetup.remove_files
        with mock.patch.object(aisetup, "remove_files", side_effect=lambda *a: (reached.set(), release.wait(30), real(*a))[2]):
            self.eng.delete("other")
            self.assertTrue(reached.wait(30))
            ok, text = self.eng.cancel()
            release.set()
        self.assertFalse(ok)
        self.assertIn("cannot be cancelled", text)
        self.assertEqual(self.job()["state"], "done")

    def test_failures_say_why_and_leave_nothing_installed(self):
        self.httpd.files.clear()
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("HTTP 404", job["error"])
        self.assertIn("failed", self.notice())
        self.assertFalse(self.installed("tiny"))
        self.httpd.files["/" + self.archive[0]] = bytes(len(self.archive[1]))   # the right size, the wrong bytes
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("SHA-256 is", job["error"])
        arch = aisetup.archive_path(self.d, self.runtime)
        self.assertFalse(os.path.exists(arch))
        self.assertFalse(os.path.exists(arch + ".part"), "a wrong hash deletes the download")
        self.assertFalse(os.path.exists(aiollama.runtime_dir(self.d, self.runtime)), "nothing unpacked")
        self.assertEqual(self.popens, [])

    def test_an_archive_that_holds_a_way_out_is_refused(self):
        import tarfile
        import io as _io
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("../../escape")
            info.size = 1
            t.addfile(info, _io.BytesIO(b"x"))
        bad = ("ollama-bad.tgz" if POSIX else "ollama-bad.zip", buf.getvalue())
        if not POSIX:
            import zipfile
            buf = _io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("../../escape", "x")
            bad = ("ollama-bad.zip", buf.getvalue())
        self.httpd.files = {"/" + bad[0]: bad[1]}
        self.eng.runtime = fo.fake_runtime(self.dl.base, bad)[0]
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("unsafe name", job["error"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "escape")))
        self.assertFalse(os.path.exists(aiollama.runtime_dir(self.d, self.eng.runtime) + ".part"))

    def test_the_server_never_gets_a_bad_wish(self):
        n = len(self.httpd.requests)
        self.assertEqual(self.eng.download("../../etc/passwd")[0], False)
        self.assertIn("unknown model", self.notice())
        unpinned = dict(self.m("other"), id="loose", ollama=None)
        self.eng.models.append(unpinned)
        self.eng.download("loose")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("not pinned", job["error"])
        self.assertEqual((len(self.httpd.requests), self.pulls()), (n, []))

    def test_not_enough_disk_stops_before_the_network(self):
        with mock.patch.object(aisetup, "free_bytes", return_value=1000):
            self.eng.download("tiny")
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("not enough free disk space", job["error"])
        self.assertEqual(self.httpd.requests, [])

    def test_a_model_that_will_not_work_here_is_refused_and_the_command_with_force_is_named(self):
        hw = tas.hw_of(8192, 5500)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            self.eng.hw_fn = lambda: hw
            self.eng.models[0] = dict(self.m("tiny"), approx_mb=30000, ram_mb=31000)
            self.eng.download("tiny")
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("will not work on this machine", job["error"])
        self.assertIn("--force", job["error"])
        self.assertIn(aisetup.commands_for("tiny")["install"], job["error"])
        self.assertEqual(self.httpd.requests, [])

    def test_a_slow_model_is_installed_with_a_warning(self):
        hw = tas.hw_of(8192, 5500)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            self.eng.hw_fn = lambda: hw
            self.eng.models[1] = dict(self.m("other"), approx_mb=5500, ram_mb=6000)
            self.eng.download("other")
            job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("slow down", job["note"] + self.notice())

    def test_an_account_that_may_not_write_is_told_the_command_of_the_administrator(self):
        with mock.patch.object(aisetup, "ensure_dirs", side_effect=PermissionError(13, "Permission denied", self.d)):
            self.eng.download("tiny")
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("may not write", job["error"])
        self.assertIn(aisetup.commands_for("tiny")["install"], job["error"])
        self.assertEqual(self.httpd.requests, [])

    def test_the_lock_keeps_the_other_process_out_and_a_dead_ones_is_taken_over(self):
        aisetup.ensure_dirs(self.d)
        lock = os.path.join(self.d, aiweb.LOCK_FILE)
        tas.put(lock, json.dumps({"what": "downloading qwen3-8b", "pid": 1}))
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("another nuc-console process is downloading qwen3-8b", job["error"])
        self.assertEqual(self.httpd.requests, [])
        self.assertTrue(os.path.exists(lock), "the other one's lock stays")
        old = time.time() - aiweb.LOCK_STALE_S - 60
        os.utime(lock, (old, old))
        self.eng.download("tiny")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(os.path.exists(lock), "ours is gone with the job")

    def test_the_lock_is_released_when_the_job_fails_too(self):
        self.httpd.files.clear()
        self.eng.download("tiny")
        self.assertEqual(self.job()["state"], "failed")
        self.assertFalse(os.path.exists(os.path.join(self.d, aiweb.LOCK_FILE)))

    def test_snapshot_numbers_and_words(self):
        job = {"kind": "download", "model": "qwen3-8b", "state": "running", "phase": "downloading", "step": "qwen3-8b", "done": 2_100_000_000,
               "total": 5_000_000_000, "rate": 12_300_000.0}
        self.assertEqual(aiweb.job_text(job), "qwen3-8b: downloading the model, 42%, 2.1 GB of 5.0 GB, 12.3 MB/s, about 4 min left")
        self.assertEqual(aiweb.job_text(job, False), "downloading qwen3-8b")
        self.assertEqual(aiweb.job_text(dict(job, step="runtime")), "qwen3-8b: downloading the runtime, 42%, 2.1 GB of 5.0 GB, 12.3 MB/s, about 4 min left")
        self.assertEqual(aiweb.job_text(dict(job, phase="verifying")), "qwen3-8b: checking the SHA-256 of the model")
        self.assertEqual(aiweb.job_text(dict(job, step="runtime", phase="unpacking")), "qwen3-8b: unpacking the runtime")
        self.assertEqual(aiweb.job_text(dict(job, kind="use", phase="starting")), "qwen3-8b: starting the model server")
        self.assertIn("loading the model", aiweb.job_text(dict(job, kind="use", phase="loading")))
        self.assertEqual(aiweb.job_text(dict(job, total=0, done=0)), "qwen3-8b: checking what is there")
        self.assertEqual(aiweb.job_text(dict(job, kind="use", state="done")), "setting up qwen3-8b done")
        self.assertEqual(aiweb.job_text(dict(job, state="failed")), "downloading qwen3-8b failed")
        self.assertEqual(aiweb.job_text(dict(job, state="cancelled")), "downloading qwen3-8b cancelled")
        self.assertEqual(aiweb.job_text(dict(job, kind="delete-all", model="", state="running")), "deleting everything")
        self.assertEqual(aiweb.job_text(None), "")
        self.assertEqual([aiweb.minutes(x) for x in (0, 40, 89, 240, 5000, 9000)], ["1 s", "40 s", "89 s", "4 min", "83 min", "2 h 30 min"])
        self.assertIsNone(self.eng.snapshot()["job"])
        self.assertEqual(self.eng.snapshot()["version"], self.eng.version)


# ----------------------------------------------------------------------------------------------------------------------- delete

class Delete(Base):
    def test_one_model_and_the_layers_only_it_used(self):
        self.install()
        self.eng._web(lambda st: st.update(model="tiny"))
        ok, _t = self.eng.delete("tiny")
        self.assertTrue(ok)
        job = self.job()
        self.assertEqual(job["state"], "done", job["error"])
        self.assertIn("deleted tiny, ", job["note"])
        self.assertIn("freed", job["note"])
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m("tiny"))))
        self.assertTrue(self.installed("other"), "the others stay, with the layers they share")
        self.assertTrue(aisetup.runtime_ready(self.d, self.runtime))
        self.assertNotIn("model", advisor.read_web_state(self.eng.web_path()), "the model the page had chosen is gone")
        self.assertFalse(os.path.exists(os.path.join(self.d, aiweb.LOCK_FILE)))
        self.assertEqual(self.popens, [], "no server is started to delete")

    def test_everything_the_server_the_models_and_its_home(self):
        self.install()
        os.makedirs(os.path.join(self.d, "home", ".ollama"))
        tas.put(os.path.join(self.d, "home", ".ollama", "id_ed25519"), b"x")
        tas.put(os.path.join(self.d, "notes.txt"), "not ours")
        self.eng._web(lambda st: st.update(model="other", enabled=True))
        self.eng.delete_all()
        job = self.job()
        self.assertEqual(job["state"], "done", job["error"])
        self.assertIn("deleted the server and every model", job["note"])
        for m in self.models:
            self.assertFalse(os.path.exists(aisetup.model_path(self.d, m)))
        self.assertFalse(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertFalse(os.path.exists(os.path.join(self.d, "home")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "notes.txt")), "only the folders of the server and the models")
        st = advisor.read_web_state(self.eng.web_path())
        self.assertEqual(st, {"enabled": True}, "the switch stays, the model is gone")
        self.assertEqual(aisetup.dir_space(self.d)["used"], 0)

    def test_nothing_to_delete(self):
        self.eng.delete("tiny")
        job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("nothing to delete", job["note"])

    def test_a_job_that_ends_at_once_has_the_last_word_in_the_notice(self):
        seen, real = [], self.eng._result
        self.eng._result = lambda ok, text: (seen.append(text), real(ok, text))[1]
        for _ in range(60):
            seen.clear()
            self.eng.delete("tiny")                      # nothing there: it is over before start() has returned to the caller
            self.assertTrue(self.eng.wait(30))
            self.assertEqual(len(seen), 2)
            self.assertTrue(seen[0].startswith("started: "), seen)
            self.assertIn("nothing to delete", seen[1])
            self.assertIn("nothing to delete", self.notice())
        self.eng._result = real

    def test_files_this_account_cannot_delete_name_the_command_to_run_instead(self):
        self.install()
        real = os.unlink
        tiny_blobs = {d for d, _s in aiollama.read_manifest(aisetup.model_path(self.d, self.m("tiny")))}

        def unlink(p, *a):
            if any(os.path.basename(p) == d.replace(":", "-") for d in tiny_blobs) or p.endswith(os.path.join("tiny", "latest")):
                raise PermissionError(13, "Permission denied", p)
            return real(p, *a)
        with mock.patch.object(aiollama.os, "unlink", side_effect=unlink):
            self.eng.delete("tiny")
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("latest", job["error"])
        self.assertIn("belong to another account", job["error"])
        self.assertIn(aisetup.commands_for("tiny")["remove"], job["error"])
        self.assertTrue(self.installed("tiny"), "still there, and still known as installed")
        real_rmtree = shutil.rmtree

        def rmtree(path, ignore_errors=False, onerror=None):
            if path.endswith("models") and onerror:
                onerror(os.unlink, os.path.join(path, "blobs", "x"), (PermissionError, PermissionError(13, "Permission denied"), None))
                return
            return real_rmtree(path, ignore_errors=ignore_errors, onerror=onerror)
        with mock.patch.object(aisetup.shutil, "rmtree", side_effect=rmtree):
            self.eng.delete_all()
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("nuc-console-ai remove", job["error"])
        self.assertFalse(os.path.exists(aisetup.runtime_path(self.d, self.runtime)), "what could be deleted was")

    @unix_only
    def test_the_model_the_server_runs_is_deleted_through_it_and_the_server_and_the_advisor_stop(self):
        self.install()
        self.eng.use_model("tiny")
        self.assertEqual(self.job()["state"], "done")
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.delete("tiny")
        self.assertEqual(self.job()["state"], "done")
        seen = [(r["path"], r["body"]) for r in fo.requests_seen(self.d)][-2:]
        self.assertEqual(seen, [("/api/generate", {"model": "tiny", "keep_alive": 0}), ("/api/delete", {"model": "tiny"})],
                         "unloaded first (Windows cannot delete a file in use), then deleted by the server")
        self.assertFalse(self.installed("tiny"))
        self.assertFalse(alive(pid), "it answered with that model: it is stopped too")
        self.assertEqual(self.eng.snapshot()["state"][0], "off")
        st = advisor.read_web_state(self.eng.web_path())
        self.assertNotIn("enabled", st, "nothing answers with that model any more: the advisor is off")
        self.assertNotIn("model", st)

    @unix_only
    def test_another_model_than_the_one_in_use_is_deleted_through_the_server_that_stays(self):
        self.install()
        self.eng.use_model("tiny")
        self.assertEqual(self.job()["state"], "done")
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.delete("other")
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(fo.requests_seen(self.d)[-1]["path"], "/api/delete")
        self.assertFalse(self.installed("other"))
        snap = self.eng.snapshot()
        self.assertEqual((snap["server"]["pid"], snap["server"]["model"], snap["state"][0]), (pid, "tiny", "running"))

    @unix_only
    def test_everything_stops_the_server_first(self):
        self.install()
        self.eng.use_model("tiny")
        self.assertEqual(self.job()["state"], "done")
        info = self.eng.child_info
        self.eng.delete_all()
        self.assertEqual(self.job()["state"], "done")
        self.assertTrue(info["gone"].wait(30))
        self.assertFalse(alive(info["pid"]))
        self.assertFalse(aisetup.runtime_ready(self.d, self.runtime))


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)  # a zombie of ours is not alive
    except ChildProcessError:
        return True


# ---------------------------------------------------------------------------------------------------------------- switch and use

class Switch(Base):
    def test_on_and_off_in_web_json_and_the_switch_says_who_decides(self):
        self.assertEqual(self.eng.snapshot()["switch"], {"on": False, "by": "", "locked": False, "can_off": True})
        self.assertTrue(self.eng.set_enabled(True)[0])
        self.assertEqual(self.eng.snapshot()["switch"], {"on": True, "by": "web", "locked": False, "can_off": True})
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"enabled": True})
        self.assertTrue(self.eng.ecfg()["ai"]["enabled"])
        self.assertFalse(self.cfg["ai"]["enabled"], "config.ini's own is not touched")
        self.assertTrue(self.eng.set_enabled(False)[0])
        self.assertEqual(self.eng.snapshot()["switch"]["on"], False)
        self.assertFalse(self.eng.ecfg()["ai"]["enabled"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "config.ini")), "config.ini is never written")

    def test_config_ini_yes_cannot_be_turned_off_from_here(self):
        self.cfg["ai"]["enabled"] = True
        self.assertEqual(self.eng.snapshot()["switch"], {"on": True, "by": "config", "locked": False, "can_off": False})
        ok, text = self.eng.set_enabled(False)
        self.assertFalse(ok)
        self.assertIn("on by config.ini", text)
        self.assertTrue(self.eng.set_enabled(True)[0], "turning on what is on is harmless")

    def test_where_this_process_may_write_config_ini_off_turns_off_what_config_ini_turned_on(self):
        self.cfg["ai"]["enabled"] = True
        wrote = []

        def off():
            wrote.append(True)
            self.cfg["ai"]["enabled"] = False
            return True, ""
        self.eng.cfg_off_fn, self.eng.cfg_writable_fn = off, lambda: True
        self.assertEqual(self.eng.snapshot()["switch"], {"on": True, "by": "config", "locked": False, "can_off": True})
        ok, text = self.eng.turn_off()
        self.assertTrue(ok, text)
        self.assertEqual(wrote, [True])
        self.assertEqual(self.eng.snapshot()["switch"]["on"], False)
        self.eng.cfg_off_fn = lambda: (False, "config.ini cannot be written by this account")
        self.cfg["ai"]["enabled"] = True
        ok, text = self.eng.set_enabled(False)
        self.assertFalse(ok)
        self.assertIn("(config.ini cannot be written by this account)", text)

    def test_config_ini_is_written_only_in_a_portable_run_and_only_its_key(self):
        path = os.path.join(self.tmp, "config.ini")  # NUC_CONSOLE_CONFIG
        with open(path, "w", encoding="utf-8") as f:
            f.write("[ai]\n# the advisor\nenabled = yes\nmodel = tiny\n")
        with mock.patch.object(nuc_config, "PORTABLE", False):
            self.assertFalse(aiweb.default_cfg_writable())
            self.assertFalse(aiweb.default_cfg_off()[0])
        with open(path, encoding="utf-8") as f:
            self.assertIn("enabled = yes", f.read(), "an installation's config.ini is the administrator's")
        cur = {"ai": {"enabled": True}}
        with mock.patch.object(nuc_config, "PORTABLE", True), mock.patch.object(nuc_config, "current", return_value=cur), \
                mock.patch.dict(aiweb._BIND, {"cfg": lambda: cur}):
            self.assertTrue(aiweb.default_cfg_writable())
            self.assertEqual(aiweb.default_cfg_off(), (True, ""))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, "[ai]\n# the advisor\nenabled = no\nmodel = tiny\n")
        self.assertFalse(cur["ai"]["enabled"], "the process reads it at once")

    def test_the_lock_refuses_every_action_and_changes_nothing(self):
        self.install()
        self.cfg["ai"]["web_actions"] = False
        n = len(self.httpd.requests)
        for call in (lambda: self.eng.set_enabled(True), lambda: self.eng.turn_on("tiny"), self.eng.turn_on, lambda: self.eng.use_model("tiny"), self.eng.turn_off,
                     lambda: self.eng.download("other"), self.eng.cancel, lambda: self.eng.delete("tiny"), self.eng.delete_all, self.eng.start_server,
                     self.eng.stop_server, lambda: self.eng.ask("why?"), lambda: self.eng.advise(7), self.eng.load_model, self.eng.clear_chat):
            ok, text = call()
            self.assertFalse(ok)
            self.assertIn("locked by config.ini ([ai] web_actions = no)", text)
        self.assertTrue(self.eng.snapshot()["locked"])
        self.assertIsNone(self.eng.snapshot()["job"])
        self.assertEqual(len(self.httpd.requests), n)
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {})
        self.assertTrue(self.installed("tiny"))

    def test_an_unwritable_folder_is_said_not_raised(self):
        with mock.patch.object(advisor, "write_web_state", side_effect=PermissionError(13, "Permission denied")):
            ok, text = self.eng.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("may not change it", text)


# ------------------------------------------------------------------------------------------------------------------------ server

@unix_only
class Server(Base):
    def start(self, name="tiny"):
        ok, text = self.eng.start_server(name)
        self.assertTrue(ok, text)
        return self.job()

    def test_load_it_now_loads_the_model_of_the_server_started_here(self):
        self.assertEqual(self.eng.load_model(), (False, "no model server with a model runs here: turn AI on first"))
        self.install()
        self.start()
        before = len([r for r in fo.requests_seen(self.d) if r["path"] == "/api/generate"])
        ok, text = self.eng.load_model()
        self.assertTrue(ok, text)
        job = self.job()
        self.assertEqual((job["kind"], job["state"], job["error"], job["note"]), ("load", "done", "", "tiny is loaded: it answers at once"))
        loads = [r["body"] for r in fo.requests_seen(self.d) if r["path"] == "/api/generate"]
        self.assertEqual(loads[before:], [{"model": "tiny"}])

    def test_start_runs_the_server_as_a_child_on_loopback_loads_the_model_and_stop_ends_it(self):
        self.install()
        job = self.start()
        self.assertEqual((job["state"], job["error"]), ("done", ""))
        srv = self.eng.snapshot()["server"]
        self.assertTrue(srv["running"])
        self.assertEqual(srv["model"], "tiny")
        self.assertTrue(alive(srv["pid"]))
        port = aisetup.port_of(srv["endpoint"])
        self.assertEqual(srv["endpoint"], "http://127.0.0.1:%d/v1" % port)
        ok, ids, why = aisetup.probe(srv["endpoint"], 2)
        self.assertEqual((ok, sorted(ids)), (True, ["other", "tiny"]), "it serves every installed model: %s" % why)
        self.assertEqual([r["body"] for r in fo.requests_seen(self.d) if r["path"] == "/api/generate"], [{"model": "tiny"}], "tiny loaded")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"model": "tiny", "endpoint": srv["endpoint"]})
        eff = self.eng.ecfg()["ai"]
        self.assertEqual((eff["model"], eff["endpoint"]), ("tiny", srv["endpoint"]))
        # how it was started: `ollama serve`, 127.0.0.1 only, a small environment, its own process group, low priority
        argv, kw = self.popens[0]
        self.assertEqual(argv[-2:], [aisetup.runtime_path(self.d, self.runtime), "serve"])
        if len(argv) > 2:
            self.assertEqual((os.path.basename(argv[0]), argv[1:3]), ("nice", ["-n", "10"]), "low priority, like `serve`")
        self.assertTrue(kw["start_new_session"])
        env = kw["env"]
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:%d" % port)
        self.assertEqual(env["OLLAMA_MODELS"], os.path.join(self.d, "models"))
        self.assertEqual(env["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual({k for k in env if not k.startswith("OLLAMA_")}, {"PATH", "HOME", "TMPDIR", "LANG"})
        self.assertEqual(env["HOME"], os.path.join(self.d, "home"))
        self.assertNotIn("NUC_CONSOLE_HOME", env, "this process's environment is not the server's")
        self.assertFalse(set(aiollama.GPU_HIDE) & set(env), "the GPU is Ollama's to use")
        info = self.eng.child_info
        ok, text = self.eng.stop_server()
        self.assertTrue(ok, text)
        self.assertTrue(info["gone"].wait(30))
        self.assertFalse(self.eng.snapshot()["server"]["running"])
        self.assertFalse(alive(srv["pid"]))
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"model": "tiny"}, "the endpoint was this server's: gone with it")
        self.assertFalse(aisetup.probe(srv["endpoint"], 1)[0])

    def test_only_one_child_and_only_one_a_second_start_is_refused(self):
        self.install()
        self.start("tiny")
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.start_server("other")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("already running", job["error"])
        self.assertEqual(self.eng.snapshot()["server"]["pid"], pid)
        self.assertEqual(len(self.popens), 1)

    def test_start_without_a_model_or_with_one_that_is_not_installed_says_so(self):
        self.eng.start_server("tiny")
        self.assertIn("download a model first", self.job()["error"])
        self.install(("other",))
        self.eng.start_server("tiny")
        self.assertIn("not installed", self.job()["error"])
        self.assertEqual(self.popens, [])

    def test_start_without_a_name_takes_the_model_the_page_chose(self):
        self.install()
        self.eng._web(lambda st: st.update(model="other"))
        self.start(None)
        self.assertEqual(self.eng.snapshot()["server"]["model"], "other")

    def test_a_server_that_ends_at_once_is_a_failed_job_with_its_last_line(self):
        self.install()
        self.control(die="boom: unknown flag --models")
        job = self.start()
        self.assertEqual(job["state"], "failed")
        self.assertIn("stopped at once (exit status 3)", job["error"])
        self.assertIn("boom: unknown flag", job["error"])
        self.assertFalse(self.eng.snapshot()["server"]["running"])
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {}, "nothing was chosen for a server that never was")

    def test_a_server_that_ends_later_leaves_its_exit_and_its_last_lines(self):
        self.install()
        self.eng.start_wait = 0.1
        self.start()
        srv = self.eng.snapshot()["server"]
        os.killpg(srv["pid"], 9)
        info = self.eng.child_info
        self.assertTrue(info["gone"].wait(30))
        snap = self.eng.snapshot()["server"]
        self.assertFalse(snap["running"])
        self.assertEqual(snap["exit"]["model"], "tiny")
        self.assertEqual(snap["exit"]["code"], -9)
        self.assertEqual(advisor.read_web_state(self.eng.web_path()).get("endpoint"), None, "nobody answers there now")

    def test_a_server_that_ignores_the_request_to_end_is_killed(self):
        self.install()
        self.control(stubborn=True)
        self.start()
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.stop_grace = 0.2
        info = self.eng.child_info
        self.assertTrue(self.eng.stop_server()[0])
        self.assertTrue(info["gone"].wait(30))
        self.assertFalse(alive(pid))

    def test_stop_without_a_child_says_so(self):
        ok, text = self.eng.stop_server()
        self.assertFalse(ok)
        self.assertIn("no model server was started here", text)

    def test_the_server_ends_with_the_process(self):
        self.install()
        self.start()
        pid = self.eng.snapshot()["server"]["pid"]
        aiweb.set_engine(self.eng)
        self.addCleanup(aiweb.set_engine, None)
        aiweb.shutdown()
        self.assertFalse(alive(pid))
        self.assertTrue(self.eng._atexit, "and an exit handler is there for the day the process ends without anyone asking")

    def test_turning_the_advisor_off_stops_the_server_started_here(self):
        self.install()
        self.eng.set_enabled(True)
        self.start()
        info = self.eng.child_info
        pid = info["pid"]
        ok, text = self.eng.turn_off()
        self.assertTrue(ok)
        self.assertIn("being stopped", text)
        self.assertTrue(info["gone"].wait(30))
        self.assertFalse(alive(pid))

    def test_a_server_already_answering_at_the_endpoint_the_page_holds_is_not_started_twice(self):
        self.install()
        srv = ta.Fake()
        self.addCleanup(srv.stop)
        self.eng._web(lambda st: st.update(endpoint=srv.url))
        self.eng.start_server("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("already answers", job["error"])
        self.assertEqual(self.popens, [])

    def test_the_port_is_a_free_one(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        taken = holder.getsockname()[1]
        self.addCleanup(holder.close)
        with mock.patch.object(aisetup, "DEFAULT_PORT", taken):
            port = aisetup.pick_port(None)
        self.assertNotEqual(port, taken)
        self.assertTrue(taken < port < taken + aisetup.PORT_TRIES)
        with mock.patch.object(aisetup, "DEFAULT_PORT", taken), mock.patch.object(aisetup, "PORT_TRIES", 1):
            self.assertRaises(aisetup.SetupError, aisetup.pick_port, None)

    def test_config_gpu_no_hides_the_gpus_from_the_server(self):
        self.install()
        self.cfg["ai"]["gpu"] = "no"
        self.start()
        env = self.popens[0][1]["env"]
        for k in aiollama.GPU_HIDE:
            self.assertEqual(env[k], "-1", k)
        self.assertEqual(env["OLLAMA_HOST"].split(":")[0], "127.0.0.1", "whatever the plan, this machine only")
        self.assertEqual(self.eng.snapshot()["server"]["where"], "CPU only")


# ------------------------------------------------------------------------------------------------------------------------- chat

class Chat(Base):
    def setUp(self):
        Base.setUp(self)
        self.model = ta.Fake()
        self.addCleanup(self.model.stop)
        self.db = os.path.join(self.tmp, "history.db")
        ta.make_db(self.db).close()
        for p in (mock.patch.object(advisor, "MIN_INTERVAL", 0.0),):
            p.start()
            self.addCleanup(p.stop)
        advisor._reset_limits()
        advisor._MEM.clear()
        advisor._TOOLS_OK.clear()
        self.eng.history_fn = lambda: ta.ro(self.db)
        self.eng.report_fn = lambda days: ta.make_report()
        self.cfg["ai"].update(enabled=True, endpoint=self.model.url, model="tiny-model", timeout_s=20)

    def chat(self):
        self.assertTrue(self.eng.wait_chat(30), "the answer did not come")
        return self.eng.snapshot()["chat"]

    def test_a_question_is_answered_in_the_background_and_kept(self):
        self.model.queue += [ta.tool_calls(("events", {"kind": "crash"})), ta.completion("chromium crashed most.")]
        ok, text = self.eng.ask("which app crashes?")
        self.assertTrue(ok, text)
        chat = self.chat()
        self.assertFalse(chat["busy"])
        e = chat["history"][-1]
        self.assertEqual((e["kind"], e["q"], e["error"]), ("ask", "which app crashes?", ""))
        self.assertEqual(e["res"]["text"], "chromium crashed most.")
        self.assertEqual(e["res"]["tools_used"], ["events"])
        self.assertEqual(self.model.posts()[0]["body"]["messages"][1]["content"], "which app crashes?")

    def test_asking_the_model_is_said_only_while_it_is_asked(self):
        release = threading.Event()
        self.model.queue.append({"body": ta.completion("done."), "wait": release})
        ok, text = self.eng.ask("is it up?")
        self.assertEqual((ok, text), (True, aiweb.ASKING))
        self.assertEqual(self.eng.snapshot()["notice"]["text"], aiweb.ASKING)
        release.set()
        self.chat()
        self.assertIsNone(self.eng.snapshot()["notice"], "the answer is on the page: the notice must not go on saying it is being asked")

    def test_a_notice_that_is_not_the_question_is_kept_when_the_answer_comes(self):
        release = threading.Event()
        self.model.queue.append({"body": ta.completion("done."), "wait": release})
        self.eng.ask("is it up?")
        self.eng._result(False, "something else happened")
        release.set()
        self.chat()
        self.assertEqual(self.eng.snapshot()["notice"]["text"], "something else happened")

    def test_the_request_never_waits_for_the_model_and_the_answer_is_written_by_another_thread(self):
        release, threads = threading.Event(), []
        self.model.queue.append({"body": ta.completion("late"), "wait": release})
        real = advisor.ask

        def ask(*a, **k):
            threads.append(threading.current_thread())
            return real(*a, **k)
        with mock.patch.object(advisor, "ask", side_effect=ask):
            ok, _t = self.eng.ask("slow one")
            self.assertTrue(ok)
            snap = self.eng.snapshot()
            self.assertTrue(snap["chat"]["busy"], "returned at once, the answer is still being written")
            self.assertEqual(snap["chat"]["pending"]["q"], "slow one")
            self.assertTrue(snap["busy"])
            ok, text = self.eng.ask("another")
            self.assertFalse(ok)
            self.assertIn("busy", text)
            release.set()
            chat = self.chat()
        self.assertNotEqual(threads[0], threading.current_thread())
        self.assertEqual(chat["history"][-1]["res"]["text"], "late")
        self.assertEqual(len(chat["history"]), 1, "the refused one was never asked")

    def test_the_last_ones_are_kept_oldest_first(self):
        self.eng.history_max = 10
        for i in range(12):
            self.model.queue.append(ta.completion("a%d" % i))
            advisor._reset_limits()
            self.assertTrue(self.eng.ask("q%d" % i)[0])
            self.chat()
        h = self.eng.snapshot()["chat"]["history"]
        self.assertEqual([e["q"] for e in h], ["q%d" % i for i in range(2, 12)])

    def test_the_question_is_limited_to_500_characters_and_cleaned(self):
        ok, text = self.eng.ask("x" * 501)
        self.assertFalse(ok)
        self.assertIn("500 characters at most", text)
        self.assertFalse(self.eng.ask("   ")[0])
        self.assertFalse(self.eng.ask("\x1b[2J\x07")[0], "nothing but control characters is no question")
        self.model.queue.append(ta.completion("ok"))
        self.assertTrue(self.eng.ask("a" * 500)[0])
        self.chat()
        self.assertEqual(self.eng.snapshot()["chat"]["history"][-1]["q"], "a" * 500)
        self.model.queue.append(ta.completion("ok"))
        advisor._reset_limits()
        self.assertTrue(self.eng.ask("why\x1b[31m is it\x07 slow\n?")[0])
        self.chat()
        self.assertNotIn("\x1b", self.eng.snapshot()["chat"]["history"][-1]["q"])
        self.assertEqual(len(self.model.posts()), 2)

    def test_no_answer_is_an_entry_that_says_why(self):
        self.cfg["ai"]["enabled"] = False
        self.eng.ask("is it on?")
        e = self.chat()["history"][-1]
        self.assertEqual(e["res"], None)
        self.assertIn("enabled = no", e["error"])
        self.cfg["ai"].update(enabled=True, endpoint="http://127.0.0.1:%d/v1" % free_port())
        self.eng.ask("is it up?")
        self.assertIn("no server on", self.chat()["history"][-1]["error"])
        self.cfg["ai"]["endpoint"] = self.model.url
        self.eng.history_fn = lambda: (_ for _ in ()).throw(advisor.NoHistory("no history yet"))
        self.eng.ask("any history?")
        self.assertIn("no history yet", self.chat()["history"][-1]["error"])

    def test_the_page_can_turn_it_on_and_the_question_is_asked(self):
        self.cfg["ai"].update(enabled=False, endpoint="http://127.0.0.1:1/v1", model="")
        self.eng._web(lambda st: st.update(enabled=True, model="tiny-model", endpoint=self.model.url))
        self.model.queue.append(ta.completion("on"))
        self.eng.ask("hello?")
        self.assertEqual(self.chat()["history"][-1]["res"]["text"], "on")

    def test_advice_now_is_a_fresh_answer_and_is_stored_for_the_screens_when_this_account_may(self):
        self.model.queue += [ta.completion("Check memory [oom:postgres]."), ta.completion("Second opinion.")]
        self.assertTrue(self.eng.advise(7)[0])
        e = self.chat()["history"][-1]
        self.assertEqual((e["kind"], e["q"], e["error"]), ("advise", "advice on the last 7 days", ""))
        self.assertEqual(e["res"]["cites"], ["oom:postgres"])
        self.assertTrue(e["res"]["stored"], "NUC_CONSOLE_HOME is set: the shared advice is this account's to write")
        stored = advisor.read_store()["periods"][7]
        self.assertEqual(stored["text"], "Check memory [oom:postgres].")
        advisor._reset_limits()
        self.eng.advise(7)
        self.assertEqual(self.chat()["history"][-1]["res"]["text"], "Second opinion.", "'now' means a new answer, not the cached one")
        self.assertEqual(len(self.model.posts()), 2)
        self.assertEqual(self.eng.advise(1)[0], True)
        self.chat()
        self.assertEqual(self.eng.snapshot()["chat"]["history"][-1]["q"], "advice on the last day")

    def test_advice_for_other_periods_is_refused_and_a_store_that_cannot_be_written_is_not_an_error(self):
        for days in (0, 3, 31, "7", None):
            ok, text = self.eng.advise(days)
            self.assertFalse(ok)
            self.assertIn("1, 7, 30", text)
        self.assertEqual(self.model.requests, [])
        self.model.queue.append(ta.completion("fine"))
        with mock.patch.object(advisor, "save_shared", side_effect=OSError("read-only")):
            self.eng.advise(30)
            e = self.chat()["history"][-1]
        self.assertEqual((e["res"]["text"], e["res"]["stored"], e["error"]), ("fine", False, ""))

    def test_advice_when_there_is_no_history(self):
        self.eng.report_fn = lambda days: (_ for _ in ()).throw(advisor.NoHistory("no history yet: the collector writes it"))
        self.eng.advise(7)
        self.assertIn("no history yet", self.chat()["history"][-1]["error"])

    STATE = {"host": "host-x", "status": "2 PROBLEMS", "problems": [{"level": "err", "text": "disk / is 97% full", "id": "disk-full"}],
             "figures": ["CPU: 6 % (ok; 16 threads)"], "top_cpu": [{"name": "ffmpeg", "cpu_pct": 280.0, "mem_mb": 640.0}]}

    def test_a_question_comes_with_the_state_of_the_machine_now_as_data(self):
        self.eng.state_fn = lambda: dict(self.STATE)
        self.model.queue.append(ta.completion("/ is almost full."))
        self.assertTrue(self.eng.ask("why is it slow?")[0])
        e = self.chat()["history"][-1]
        self.assertEqual((e["res"]["text"], e["res"]["state"], e["error"]), ("/ is almost full.", True, ""))
        msgs = self.model.posts()[0]["body"]["messages"]
        self.assertNotIn("disk / is 97% full", msgs[0]["content"], "the state is data: never in the instructions")
        said = msgs[1]["content"]
        self.assertTrue(said.startswith(advisor.STATE_PREAMBLE))
        self.assertEqual(json.loads(said[len(advisor.STATE_PREAMBLE):].split("\n\nQuestion: ")[0]), self.STATE)
        self.assertTrue(said.endswith("\n\nQuestion: why is it slow?"))
        self.assertIn(advisor.STATE_ONLY, advisor.parts(e["res"])["notes"][0][1])

    def test_without_a_history_the_state_still_answers_and_the_tools_say_there_is_none(self):
        self.eng.state_fn = lambda: dict(self.STATE)
        self.eng.history_fn = lambda: (_ for _ in ()).throw(advisor.NoHistory("no history yet"))
        self.model.queue += [ta.tool_calls(("events", {"kind": "crash"})), ta.completion("Nothing crashed that I can see; / is almost full.")]
        self.eng.ask("what crashed?")
        e = self.chat()["history"][-1]
        self.assertEqual(e["error"], "")
        self.assertIn("almost full", e["res"]["text"])
        tool = [m for m in self.model.posts()[1]["body"]["messages"] if m["role"] == "tool"][0]
        self.assertIn("there is no history yet", tool["content"])

    def test_a_state_that_cannot_be_read_is_left_out(self):
        self.eng.state_fn = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        self.model.queue.append(ta.completion("fine"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.eng.ask("is it up?")
            e = self.chat()["history"][-1]
        self.assertEqual((e["res"]["text"], e["res"]["state"]), ("fine", False))
        self.assertEqual(self.model.posts()[0]["body"]["messages"][1]["content"], "is it up?")
        self.assertIn("machine state error", err.getvalue())

    def test_clear_forgets_the_chat_and_keeps_an_answer_being_written(self):
        self.model.queue.append(ta.completion("one"))
        self.eng.ask("first?")
        self.chat()
        self.assertEqual(self.eng.clear_chat(), (True, "the chat is cleared"))
        self.assertEqual(self.eng.snapshot()["chat"]["history"], [])
        self.assertEqual(self.eng.clear_chat(), (True, "the chat was already empty"))
        release = threading.Event()
        self.model.queue.append({"body": ta.completion("two"), "wait": release})
        advisor._reset_limits()
        self.eng.ask("second?")
        self.eng.clear_chat()
        self.assertEqual(self.eng.snapshot()["chat"]["pending"]["q"], "second?")
        release.set()
        self.assertEqual([x["q"] for x in self.chat()["history"]], ["second?"])

    def test_clear_is_refused_when_the_page_is_locked(self):
        self.eng.history.append({"kind": "ask", "q": "kept?", "res": None, "error": "x", "at": 0})
        self.cfg["ai"]["web_actions"] = False
        self.assertEqual(self.eng.clear_chat(), (False, aiweb.LOCKED))
        self.assertEqual(len(self.eng.snapshot()["chat"]["history"]), 1)

    def test_the_chat_is_kept_in_the_ai_folder_and_read_again_after_a_restart(self):
        self.model.queue += [ta.tool_calls(("events", {"kind": "crash"})), ta.completion("chromium crashed most.")]
        self.eng.ask("which app crashes?")
        e = self.chat()["history"][-1]
        path = os.path.join(self.d, aiweb.CHAT_FILE)
        self.assertTrue(os.path.isfile(path))
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, "the questions, the answers and the machine's state: this account's only")
        again = aiweb.Engine(directory=self.d, models=self.models, cfg_fn=lambda: self.cfg, state_fn=lambda: None)
        self.addCleanup(again.shutdown)
        h = again.snapshot()["chat"]["history"]
        self.assertEqual([(x["id"], x["q"], x["res"]["text"], x["res"]["tools_used"]) for x in h],
                         [(e["id"], "which app crashes?", "chromium crashed most.", ["events"])])
        self.assertEqual(again.prompt_of(e["id"])[0], self.eng.prompt_of(e["id"])[0])
        self.assertEqual(again.clear_chat(), (True, "the chat is cleared"))
        self.assertFalse(os.path.exists(path), "cleared: no file is left")

    def test_a_folder_that_cannot_be_written_keeps_the_chat_in_memory(self):
        self.model.queue.append(ta.completion("fine"))
        with mock.patch.object(aiweb, "write_chat", side_effect=PermissionError(13, "Permission denied")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.eng.ask("is it up?")
            self.assertEqual(self.chat()["history"][-1]["res"]["text"], "fine")
        self.assertIn("kept in memory only", err.getvalue())

    def test_each_answer_keeps_the_whole_prompt_the_model_was_sent(self):
        self.eng.state_fn = lambda: dict(self.STATE)
        self.model.queue += [ta.tool_calls(("events", {"kind": "crash"})), ta.completion("chromium crashed most.")]
        self.eng.ask("which app crashes?")
        e = self.chat()["history"][-1]
        self.assertNotIn("prompt", e, "the snapshot says how many parts there are; a page asks for the prompt itself")
        msgs, entry = self.eng.prompt_of(e["id"])
        self.assertEqual(e["prompt_n"], len(msgs))
        self.assertEqual(entry["q"], "which app crashes?")
        self.assertEqual([m["role"] for m in msgs], ["request", "system", "tools", "user", "assistant", "tool"])
        sent = self.model.posts()[-1]["body"]["messages"]  # the last request holds the whole conversation
        self.assertEqual([msgs[1]["text"], msgs[3]["text"], msgs[5]["text"]], [sent[0]["content"], sent[1]["content"], sent[3]["content"]])
        self.assertIn(advisor.STATE_PREAMBLE, msgs[3]["text"])
        self.assertIn('calls events({"kind": "crash"})', msgs[4]["text"])
        self.assertIn("model tiny-model", msgs[0]["text"])
        self.assertIn("events(", msgs[2]["text"], "the tools it was offered, by name and arguments")
        self.assertEqual(self.eng.prompt_of("000000000000"), ([], None))

    def test_an_answer_that_failed_keeps_the_prompt_it_was_sent(self):
        self.model.queue.append(ta.completion(""))
        self.eng.ask("is it up?")
        e = self.chat()["history"][-1]
        self.assertIsNone(e["res"])
        self.assertTrue(e["error"])
        msgs, _e = self.eng.prompt_of(e["id"])
        self.assertEqual([m["role"] for m in msgs][-1], "user")
        self.assertEqual(msgs[-1]["text"], "is it up?")

    def test_advice_keeps_its_prompt_too(self):
        self.model.queue.append(ta.completion("Check memory."))
        self.eng.advise(7)
        e = self.chat()["history"][-1]
        msgs, _e = self.eng.prompt_of(e["id"])
        self.assertEqual([m["role"] for m in msgs], ["request", "system", "user"])
        self.assertEqual(msgs[2]["text"], self.model.posts()[-1]["body"]["messages"][1]["content"])

    def test_what_the_answer_is_doing_is_said_while_it_is_written(self):
        release, step = threading.Event(), ""
        self.model.queue += [ta.tool_calls(("events", {"kind": "crash"})), {"body": ta.completion("done."), "wait": release}]
        self.eng.ask("which app crashes?")
        gate = threading.Event()
        for _ in range(1000):
            step = (self.eng.snapshot()["chat"]["pending"] or {}).get("step", "")
            if "again" in step:
                break
            gate.wait(0.01)
        self.assertEqual(step, "asking the model again, with what events returned")
        release.set()
        e = self.chat()["history"][-1]
        self.assertEqual(e["res"]["text"], "done.")
        self.assertIsInstance(e["took"], int)

    def test_a_bug_ends_the_answer_not_the_thread(self):
        with mock.patch.object(advisor, "ask", side_effect=RuntimeError("secret detail")), contextlib.redirect_stderr(io.StringIO()):
            self.eng.ask("boom?")
            e = self.chat()["history"][-1]
        self.assertIn("unexpected error: RuntimeError", e["error"])
        self.assertNotIn("secret detail", e["error"], "the detail goes to the log, not to the page")
        self.assertFalse(self.eng.snapshot()["chat"]["busy"])


# ------------------------------------------------------------------------------------------------------------------- the chat file

class ChatFile(unittest.TestCase):
    """CHAT_FILE: what is written is read back cleaned, and a file that is not what write_chat() writes is not read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, aiweb.CHAT_FILE)

    ENTRY = {"kind": "ask", "q": "why\x1b[31m?", "error": "", "at": 1700000000.7, "took": 3.2,
             "res": {"text": "ok\x07", "tools_used": ["events"], "model": "m", "calls": [{"tool": "events", "args": {"days": 1.5}, "ok": True}], "state": True},
             "prompt": [{"role": "user", "text": "a\nb\x1b[2J"}, {"role": "evil", "text": "x"}, "junk"]}

    def test_what_is_written_is_read_back_cleaned(self):
        aiweb.write_chat(self.path, [self.ENTRY, {"kind": "nope", "q": "x"}])
        got = aiweb.read_chat(self.path)
        self.assertEqual(len(got), 1, "what is not an exchange is left out")
        g = got[0]
        self.assertEqual((g["q"], g["res"]["text"], g["at"], g["took"], g["error"]), ("why?", "ok", 1700000000, 3, ""))
        self.assertEqual(g["prompt"], [{"role": "user", "text": "a\nb"}, {"role": "?", "text": "x"}])
        self.assertEqual(g["res"]["calls"], [{"tool": "events", "ok": True, "args": '{"days": 1.5}'}], "a float is text: the file is read without floats")
        self.assertRegex(g["id"], "^[0-9a-f]{12}$")
        self.assertEqual(aiweb.read_chat(self.path), got, "the same file reads the same")

    def test_a_file_that_is_not_this_modules_is_not_read(self):
        for raw in (b"not json", b'{"v": 2, "chat": []}', b"[1]", b'{"v": 1, "chat": {}}', b'{"v": 1, "chat": [{"at": 1.5}]}'):
            with open(self.path, "wb") as f:
                f.write(raw)
            self.assertEqual(aiweb.read_chat(self.path), [], raw)
        aiweb.write_chat(self.path, [self.ENTRY])
        with mock.patch.object(aiweb, "CHAT_MAX_BYTES", 100):
            self.assertEqual(aiweb.read_chat(self.path), [], "bigger than this module writes")
        if POSIX:
            os.chmod(self.path, 0o666)
            self.assertEqual(aiweb.read_chat(self.path), [], "others may write it: nothing in it is trusted")
        self.assertEqual(aiweb.read_chat(os.path.join(self.tmp, "none")), [])

    def test_no_exchange_no_file_and_the_oldest_go_first_when_it_is_too_big(self):
        aiweb.write_chat(self.path, [])
        self.assertFalse(os.path.exists(self.path))
        many = [dict(self.ENTRY, q="q%d" % i, res=dict(self.ENTRY["res"], text="x" * 400)) for i in range(10)]
        with mock.patch.object(aiweb, "CHAT_MAX_BYTES", 3000):
            aiweb.write_chat(self.path, many)
            got = aiweb.read_chat(self.path)
        self.assertLess(len(got), 10)
        self.assertEqual(got[-1]["q"], "q9", "the newest stay")


# ---------------------------------------------------------------------------------------------------------------- the model's state

class ModelState(unittest.TestCase):
    """aiweb.model_state(): what the line under the switch says the model is doing, from the snapshot alone."""

    CAT = [{"id": "qwen3-8b", "ollama": "qwen3:8b"}]

    def snap(self, **kw):
        s = {"job": None, "server": {"running": True, "model": "qwen3-8b", "endpoint": "http://127.0.0.1:9/v1"}, "switch": {"on": True}, "locked": False,
             "ai": {"model": "qwen3-8b", "endpoint": "http://127.0.0.1:9/v1"}, "usage": {"models": [], "answers": True}}
        s.update(kw)
        return s

    def state(self, **kw):
        return aiweb.model_state(self.snap(**kw), self.CAT, now=1000.0)

    def test_each_state(self):
        job = {"state": "running", "kind": "use", "model": "qwen3-8b", "phase": "loading", "phase_at": 990.0, "started": 900.0}
        self.assertEqual((self.state(job=job)["state"], self.state(job=job)["since"]), ("loading", 990.0))
        self.assertEqual(self.state(job=dict(job, phase="starting"))["state"], "starting")
        self.assertEqual(self.state(job=dict(job, phase="downloading"))["state"], "fetching")
        self.assertEqual(self.state(job=dict(job, kind="delete"))["state"], "unloaded", "a delete is not about loading")
        self.assertEqual(self.state(job=dict(job, kind="download", phase="downloading"))["state"], "unloaded", "nor is a download of another model")
        self.assertEqual(self.state(switch={"on": False}, server={"running": False})["state"], "off")
        row = {"name": "qwen3-8b:latest", "size_mb": 5000.0, "vram_mb": 5000.0, "ctx": 4096}
        got = self.state(usage={"models": [row], "answers": True})
        self.assertEqual((got["state"], got["row"], got["load"]), ("ready", row, False))
        self.assertEqual(self.state(usage={"models": [dict(row, name="qwen3:8b")]})["state"], "ready", "the library's name is the same model")
        got = self.state(usage={"models": [dict(row, name="other:latest")]})
        self.assertEqual((got["state"], got["others"], got["load"]), ("unloaded", ["other:latest"], True))
        self.assertTrue(self.state()["load"], "the server runs here without it: it can be loaded now")
        self.assertFalse(self.state(locked=True)["load"])
        self.assertFalse(self.state(server={"running": False, "model": ""})["load"], "a server started elsewhere is not this page's to load")
        self.assertEqual(self.state(usage={"models": None, "answers": False})["state"], "down")
        self.assertEqual(self.state(usage={"models": None, "answers": None})["state"], "unknown")
        self.assertEqual(self.state(usage={"measuring": True})["state"], "checking")
        self.assertEqual(self.state(usage=None)["state"], "unknown", "nobody measured: never a reassuring 'loaded'")
        got = self.state(server={"running": False}, ai={"model": "", "endpoint": "http://127.0.0.1:11434/v1"}, usage={"models": [row]})
        self.assertEqual((got["state"], got["model"]), ("ready", "qwen3-8b:latest"), "config.ini names no model: the server's first one answers")


# ------------------------------------------------------------------------------------------------------------------------ usage

class FakeProbe(object):
    """Stands for aiweb.UsageProbe: what it returns is the test's, and it counts the readings."""

    def __init__(self, procs=(), loaded=None, gpu=None, threads=8, ram=16384):
        self.p, self.l, self.g, self.t, self.r, self.calls, self.seen = list(procs), loaded, gpu, threads, ram, 0, threading.Event()

    def procs(self):
        self.calls += 1
        self.seen.set()
        return list(self.p)

    def loaded(self, endpoint):
        self.endpoint = endpoint
        return self.l

    def gpu(self):
        return self.g

    def threads(self):
        return self.t

    def ram_mb(self):
        return self.r


PROCS = [{"pid": 10, "ppid": 1, "name": "ollama", "cpu": 2.0, "mem": 100 * 2 ** 20}, {"pid": 11, "ppid": 10, "name": "ollama", "cpu": 398.0, "mem": 3000 * 2 ** 20},
         {"pid": 12, "ppid": 11, "name": "helper", "cpu": None, "mem": None}, {"pid": 20, "ppid": 1, "name": "bash", "cpu": 50.0, "mem": 5 * 2 ** 20}]


class Usage(unittest.TestCase):
    def test_the_processes_of_the_server_this_process_started_and_their_children(self):
        u = aiweb.usage_of(PROCS, 10, [{"name": "qwen3:4b", "size_mb": 3000.0, "vram_mb": 0.0, "ctx": 4096}], {"gpus": [], "note": "none"}, 16, 32000, at=5)
        self.assertEqual(u["procs"], {"n": 3, "cpu_pct": 400.0, "share_pct": 25.0, "mem_mb": 3100.0, "ram_mb": 32000, "threads": 16})
        self.assertEqual((u["at"], u["models"][0]["name"], u["gpus"], u["gpu_note"]), (5, "qwen3:4b", [], "none"))

    def test_a_server_started_elsewhere_is_found_by_its_name_and_nothing_seen_is_none(self):
        u = aiweb.usage_of(PROCS, None)
        self.assertEqual((u["procs"]["n"], u["procs"]["cpu_pct"], u["procs"]["share_pct"]), (3, 400.0, None))
        self.assertIsNone(u["models"], "not asked: unknown, not 'none loaded'")
        self.assertIsNone(u["gpus"])
        self.assertIsNone(aiweb.usage_of([PROCS[3]], 999)["procs"])
        u = aiweb.usage_of([{"pid": 1, "ppid": 0, "name": "OLLAMA.EXE", "cpu": None, "mem": None}], None, threads=4)
        self.assertEqual(u["procs"], {"n": 1, "cpu_pct": None, "share_pct": None, "mem_mb": None, "ram_mb": None, "threads": 4})
        self.assertIsNone(aiweb.usage_of(["x", {"pid": True}, {"pid": "1"}], None)["procs"])

    def test_the_models_ollama_has_loaded(self):
        self.assertEqual(aiweb.parse_ps({"models": [{"name": "qwen3:4b", "size": 3 * 2 ** 30, "size_vram": 2 ** 30, "context_length": 4096},
                                                    {"model": "x\x1b[31m", "size": -1, "size_vram": True}, "junk"]}),
                         [{"name": "qwen3:4b", "size_mb": 3072.0, "vram_mb": 1024.0, "ctx": 4096}, {"name": "x", "size_mb": None, "vram_mb": None, "ctx": None}])  # no escape reaches a page
        for bad in (None, [], {"models": None}, {"error": "x"}):
            self.assertIsNone(aiweb.parse_ps(bad))
        self.assertEqual(aiweb.parse_ps({"models": []}), [])

    def test_the_probe_asks_only_a_loopback_server_and_nothing_answering_is_unknown(self):
        with mock.patch.object(aiollama.Api, "call", side_effect=AssertionError("never another machine")):
            for ep in ("http://192.0.2.1:11434/v1", "https://127.0.0.1:1/v1", "", None):
                self.assertIsNone(aiweb.UsageProbe.loaded(ep))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(OSError, msg="nothing answers: the caller says the server is down"):
                aiweb.UsageProbe.loaded("http://127.0.0.1:%d/v1" % free_port())
            eng = aiweb.Engine(directory=tempfile.mkdtemp(), cfg_fn=lambda: dict(nuc_config.load("/nonexistent"), ai=dict(
                nuc_config.load("/nonexistent")["ai"], endpoint="http://127.0.0.1:%d/v1" % free_port())), state_fn=lambda: None)
            self.addCleanup(shutil.rmtree, eng.directory, True)
            u = eng.measure_usage()
        self.assertEqual((u["models"], u["answers"]), (None, False))
        self.assertEqual(err.getvalue(), "", "a server that is not up yet is not an error in the log")

    def engine(self, probe, on=True):
        cfg = nuc_config.load("/nonexistent")
        cfg["ai"].update(enabled=on, endpoint="http://127.0.0.1:11434/v1")
        eng = aiweb.Engine(directory=tempfile.mkdtemp(), cfg_fn=lambda: cfg, probe=probe, state_fn=lambda: None)
        self.addCleanup(shutil.rmtree, eng.directory, True)
        self.addCleanup(eng.shutdown)
        eng.usage_s = 0.01
        return eng

    def test_it_is_measured_in_the_background_only_while_the_ai_is_on_and_a_page_asks(self):
        probe = FakeProbe(PROCS, [], {"gpus": [{"name": "GPU", "busy_pct": 40.0, "used_mb": 1.0, "total_mb": 8.0}], "source": "x", "note": ""})
        off = self.engine(probe, on=False)
        self.assertIsNone(off.snapshot(usage=True)["usage"], "the AI is off: nothing to measure")
        self.assertIsNone(off._usage_thread)
        eng = self.engine(probe)
        self.assertIsNone(eng.snapshot()["usage"], "a screen that does not show it never starts the sampler")
        self.assertIsNone(eng._usage_thread)
        first = eng.snapshot(usage=True)["usage"]
        self.assertTrue(first == {"running": False, "measuring": True} or "procs" in first)
        self.assertTrue(probe.seen.wait(10))
        gate = threading.Event()
        for _ in range(500):
            if eng.usage:
                break
            gate.wait(0.01)
        u = eng.snapshot(usage=True)["usage"]
        self.assertEqual((u["running"], u["procs"]["cpu_pct"], u["models"], u["gpus"][0]["busy_pct"]), (False, 400.0, [], 40.0))  # the ollama processes and their child, not bash
        self.assertEqual(probe.endpoint, "http://127.0.0.1:11434/v1", "no server of its own: the endpoint of [ai]")
        with mock.patch.object(aiweb, "USAGE_IDLE_S", 0.0):  # nobody asks any more: the sampler stops and forgets
            eng._usage_thread.join(10)
        self.assertFalse(eng._usage_thread.is_alive())
        self.assertIsNone(eng.usage)

    def test_a_probe_that_fails_is_unknown_not_an_error(self):
        probe = FakeProbe()
        probe.procs = lambda: (_ for _ in ()).throw(OSError("no /proc"))
        probe.gpu = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        with contextlib.redirect_stderr(io.StringIO()):
            u = self.engine(probe).measure_usage()
        self.assertEqual((u["procs"], u["gpus"], u["models"]), (None, None, None))

    def test_the_page_draws_it_and_what_cannot_be_read_is_a_question_mark(self):
        snap = {"usage": dict(aiweb.usage_of(PROCS, 10, [{"name": "qwen3:4b", "size_mb": 3000.0, "vram_mb": 1000.0, "ctx": 4096}],
                                             {"gpus": [{"name": "RTX", "busy_pct": None, "used_mb": 900.0, "total_mb": 8192.0}], "note": ""}, 16, 32000), running=True)}
        text = " ".join(n.__class__.__name__ for n in screens.ai_usage_nodes(snap))
        self.assertEqual(text, "Head KV")
        kv = screens.ai_usage_nodes(snap)[1]
        labels = [k for k, _v in kv.pairs]
        self.assertEqual(labels, ["model", "CPU", "RAM", "GPU"])
        import htmlview
        html_text = "".join(htmlview.html(n) for n in screens.ai_usage_nodes(snap))
        self.assertIn("1000 MB in the GPU&#x27;s memory, 2.0 GB in RAM", html_text)
        self.assertIn("25%", html_text)
        self.assertIn('<span class="st-unknown" role="img" aria-label="unknown">?</span><span class="t-muted"> busy', html_text,
                      "a GPU whose load is unknown is '?', never an empty bar")
        self.assertIn("the AI is off", htmlview.html(screens.ai_usage_nodes({"usage": None})[1]))
        self.assertIn("measuring", htmlview.html(screens.ai_usage_nodes({"usage": {"running": True, "measuring": True}})[1]))
        none = screens.ai_usage_nodes({"usage": {"running": True, "procs": None, "models": [], "gpus": [], "gpu_note": "no GPU here"}})
        html_text = htmlview.html(none[1])
        for words in ("none loaded now", "no process of the model server", "no GPU here"):
            self.assertIn(words, html_text)


# ------------------------------------------------------------------------------------------------------------------------- demo

class Demo(unittest.TestCase):
    def setUp(self):
        self.eng = aiweb.Engine(demo=True)
        self.eng.demo_step = 0.0
        self.eng._demo_os = "windows"      # a machine with nothing installed
        for target, why in ((mock.patch.object(aisetup, "download", side_effect=AssertionError("the demo downloads nothing")), "download"),
                            (mock.patch.object(advisor, "write_web_state", side_effect=AssertionError("the demo writes no file")), "write"),
                            (mock.patch.object(subprocess, "Popen", side_effect=AssertionError("the demo starts nothing")), "start")):
            target.start()
            self.addCleanup(target.stop)
        self.cat = lambda: self.eng.demo_catalog("windows")

    def installed(self):
        return sorted(m["id"] for m in self.cat()["models"] if m["installed"])

    def test_the_model_usage_is_invented_from_the_demo_machine_and_nothing_is_measured(self):
        self.assertIsNone(self.eng.snapshot(usage=True)["usage"], "no server on this demo machine")
        eng = aiweb.Engine(demo=True)
        eng._demo_os = "linux"  # the NVIDIA box, its server up
        with mock.patch.object(aiweb.UsageProbe, "procs", side_effect=AssertionError("the demo measures nothing")):
            u = eng.snapshot(usage=True)["usage"]
            self.assertIsNone(eng.snapshot()["usage"])
        self.assertTrue(u["demo"] and u["running"])
        self.assertEqual(u["models"][0]["vram_mb"], u["models"][0]["size_mb"], "it fits the GPU: all of it there")
        self.assertIn("NVIDIA", u["gpus"][0]["name"])
        self.assertEqual(u["procs"]["n"], 2)
        self.assertIsNone(eng._usage_thread)
        with mock.patch.object(aiweb, "time", mock.Mock(time=lambda: 1_790_000_000.0, sleep=time.sleep)):
            self.assertEqual(eng.snapshot(usage=True)["usage"], eng.snapshot(usage=True)["usage"], "a clock that stands still: the same figures")

    def test_the_chat_box_and_its_clear_button_are_there_only_with_something_in_it(self):
        acts = screens.AiActs("t", "view=ai")
        snap = self.eng.snapshot()
        nodes = screens.ai_chat_nodes(snap, [], acts)[0].children
        self.assertFalse([n for n in nodes if isinstance(n, screens.ui.Log)])
        self.assertNotIn("/ai/clear", repr([getattr(x, "action", "") for n in nodes if isinstance(n, screens.ui.Controls) for x in n.items]))
        self.eng.history.append({"kind": "ask", "q": "q", "res": None, "error": "e", "at": 0})
        snap = self.eng.snapshot()
        nodes = screens.ai_chat_nodes(snap, [screens.ui.Qa("you", "q")], acts)[0].children
        self.assertEqual(len([n for n in nodes if isinstance(n, screens.ui.Log)]), 1)
        clear = [x for n in nodes if isinstance(n, screens.ui.Controls) for x in n.items if getattr(x, "action", "") == "/ai/clear"]
        self.assertEqual((len(clear), clear[0].label, clear[0].disabled), (1, "Clear chat", False))

    def test_one_choice_downloads_starts_and_turns_the_advisor_on_in_memory(self):
        self.assertEqual(self.installed(), [])
        self.assertFalse(self.cat()["runtime"]["installed"])
        self.assertEqual(self.eng.snapshot()["state"][0], "off")
        seen = []
        self.eng.progress_hook = lambda job: seen.append(aiweb.job_text(job))
        self.assertTrue(self.eng.use_model("qwen3-4b")[0])
        self.assertTrue(self.eng.wait(30))
        job = self.eng.snapshot()["job"]
        self.assertEqual((job["state"], job["error"]), ("done", ""))
        self.assertIn("in use", job["note"])
        self.assertEqual(len(seen), 2 * aiweb.DEMO_STEPS, "runtime and model: two sets of steps")
        self.assertIn("downloading the runtime", seen[0])
        self.assertIn("downloading the model", seen[-1])
        self.assertEqual(self.installed(), ["qwen3-4b"])
        self.assertTrue(self.cat()["runtime"]["installed"])
        self.assertEqual(self.cat()["active"], "qwen3-4b")
        sp = self.cat()["space"]
        self.assertGreater(sp["used"], 2_000_000_000)
        self.assertEqual(sp["free"], aiweb.DEMO_FREE)
        snap = self.eng.snapshot()
        self.assertEqual(snap["switch"], {"on": True, "by": "web", "locked": False, "can_off": True})
        self.assertEqual((snap["server"]["running"], snap["server"]["model"]), (True, "qwen3-4b"))
        self.assertEqual(snap["state"][0], "running")
        st = self.eng.demo_status("windows")
        self.assertEqual((st["enabled"], st["model"], st["probe"]["state"], st["endpoint"]), (True, "qwen3-4b", "answering", snap["server"]["endpoint"]))
        n = len(seen)
        self.eng.use_model("qwen3-4b")
        self.eng.wait(30)
        self.assertEqual(len(seen), n, "never twice, in the demo too")

    def test_cancel_stops_the_simulated_download(self):
        self.eng.demo_step = 0.01
        self.eng.progress_hook = lambda job: self.eng.cancel() if job["done"] * 10 >= job["total"] else None
        self.eng.use_model("qwen3-4b")
        self.eng.wait(30)
        self.assertEqual(self.eng.snapshot()["job"]["state"], "cancelled")
        self.assertEqual(self.installed(), [])
        self.assertEqual(self.eng.snapshot()["switch"]["on"], False)

    def test_turn_off_delete_and_the_choice(self):
        ch = self.eng.choice()
        self.assertEqual((ch["model"], ch["by"], ch["installed"]), (None, "", False))
        self.assertEqual(ch["recommended"], ch["target"])
        self.assertGreater(ch["size"], 2_000_000_000, "the runtime and the model still to fetch")
        self.assertFalse(self.eng.turn_on()[0], "nothing chosen: the page or the screen asks about the recommended one")
        self.assertTrue(self.eng.turn_on(ch["recommended"])[0])
        self.eng.wait(30)
        self.assertEqual(self.eng.choice()["model"], ch["recommended"])
        self.assertEqual(self.eng.choice()["by"], "page")
        self.assertEqual(self.eng.choice()["size"], 0)
        self.assertTrue(self.eng.turn_off()[0])
        snap = self.eng.snapshot()
        self.assertEqual((snap["switch"]["on"], snap["server"]["running"], snap["state"][0]), (False, False, "off"))
        self.assertFalse(self.eng.demo_status("windows")["enabled"])
        self.assertTrue(self.eng.turn_on()[0], "the chosen one this time")
        self.eng.wait(30)
        self.assertEqual(self.eng.snapshot()["state"][0], "running")
        self.eng.delete(ch["recommended"])
        self.eng.wait(30)
        self.assertEqual(self.installed(), [])
        snap = self.eng.snapshot()
        self.assertEqual((snap["server"]["running"], snap["switch"]["on"]), (False, False), "the server it asked is gone")
        self.eng.use_model("qwen3-4b")
        self.eng.wait(30)
        self.eng.delete_all()
        self.eng.wait(30)
        self.assertEqual(self.installed(), [])
        self.assertFalse(self.cat()["runtime"]["installed"])

    def test_questions_and_advice_are_canned_and_marked_as_a_demo(self):
        self.assertTrue(self.eng.ask("why is it slow?")[0])
        self.eng.wait_chat(30)
        e = self.eng.snapshot()["chat"]["history"][-1]
        self.assertEqual(e["res"]["model"], "demo")
        self.assertIn("why is it slow?", e["res"]["text"])
        self.eng.advise(7)
        self.eng.wait_chat(30)
        self.assertIn("Demo advice for the last 7 days", self.eng.snapshot()["chat"]["history"][-1]["res"]["text"])

    def test_the_lock_applies_to_the_demo_too(self):
        eng = aiweb.Engine(demo=True, cfg_fn=lambda: {"ai": {"web_actions": False}})
        self.assertEqual(eng.use_model("qwen3-4b"), (False, aiweb.LOCKED))
        self.assertEqual(eng.turn_off(), (False, aiweb.LOCKED))


# ----------------------------------------------------------------------------------------------------------------------- the web page

TOKEN = "t" * 24
ESC, BEL = chr(27), chr(7)


class WebBase(Base):
    """The web view on a loopback port, with this test's engine and catalog behind its AI page: the real page, the real POST handler."""

    token = ""

    def setUp(self):
        Base.setUp(self)
        self.saved = (dict(render.CFG["features"]), dict(render.CFG["ai"]), render.DEMO, render.DEMO_OS, aiweb.set_engine(None), dict(aiweb._BIND))
        self.addCleanup(self.restore)
        render.DEMO, render.DEMO_OS = False, None
        render.CFG["features"]["ai"] = True
        render.CFG["ai"].clear()
        render.CFG["ai"].update(self.cfg["ai"])
        render._AI.clear()
        render._AIPROBE.update(res=None, at=0.0, key=None, thread=None, started=0.0)
        self.cfg = render.CFG                                     # the page and the engine read one config: a test changes render.CFG["ai"]
        self.eng.cfg_fn = lambda: render.CFG
        for target, value in ((aisetup, {"MODELS": self.models, "RUNTIME": self.runtime, "_hardware": lambda: self.hw}),):
            for name, val in value.items():
                p = mock.patch.object(target, name, val)
                p.start()
                self.addCleanup(p.stop)
        cfg = dict(nuc_config.load()["web"], refresh_seconds=2)
        self.srv = web.Server(("127.0.0.1", 0), cfg, self.token, demo=False)
        aiweb.set_engine(self.eng)                               # the server made its own: this test's is the one behind the page
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.port = self.srv.server_address[1]

    def stop_server(self):
        self.srv.shutdown()
        self.srv.server_close()

    def restore(self):
        probe = render._AIPROBE.get("thread")
        if probe is not None:
            probe.join(10)                                      # a probe this test started must not write its answer into the next test's state
        features, ai, render.DEMO, render.DEMO_OS, _old, bind = self.saved
        render.CFG["features"].clear()
        render.CFG["features"].update(features)
        render.CFG["ai"].clear()
        render.CFG["ai"].update(ai)
        aiweb.set_engine(None)
        aiweb._BIND.clear()
        aiweb._BIND.update(bind)
        render._AI.clear()
        render._AIPROBE.update(res=None, at=0.0, key=None, thread=None, started=0.0)

    # ---- http
    @classic_default()
    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        return r.status, {k.title(): v for k, v in r.getheaders()}, data

    def get(self, path="/?view=ai", headers=None):
        return self.request("GET", path, None, headers)

    def page(self, path="/?view=ai"):
        st, _h, body = self.get(path)
        self.assertEqual(st, 200, path)
        self.assertNotIn("render error", body)
        return body

    def post(self, action, fields=None, csrf=True, headers=None, path=None, body=None):
        """A browser's form post (same origin) -> (status, headers, body); csrf=True puts this server's token in, a string another one."""
        data = dict(fields or {})
        if csrf:
            data["csrf"] = self.srv.csrf if csrf is True else csrf
        h = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % self.port}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        return self.request("POST", path or "/ai/" + action, urlencode(data) if body is None else body, h)

    def raw(self, text):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(text.encode("latin-1"))
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        s.close()
        return out.decode("utf-8", "replace")

    def forms(self, body):
        """[(action, {name: value})] of the forms of a page (the hidden fields; a button's own are not there)."""
        out = []
        for m in re.finditer(r'<form[^>]*action="/ai/([a-z-]+)">(.*?)</form>', body, re.S):
            out.append((m.group(1), {n: html.unescape(v) for n, v in re.findall(r'<input type="hidden" name="([a-z]+)" value="([^"]*)">', m.group(2))}))
        return out

    def form(self, body, action, **has):
        for a, f in self.forms(body):
            if a == action and all(f.get(k) == v for k, v in has.items()):
                return f
        return None

    def go(self, action, fields=None, **kw):
        """Click: post the form with the page's own token, expect the redirect, return where it goes."""
        st, h, body = self.post(action, fields, **kw)
        self.assertEqual(st, 303, "%s: %s" % (action, body))
        self.assertEqual(body, "")
        return h["Location"]


@unix_only
class WebUse(WebBase):
    """The page end to end, with the fake download server, the fake runtime and the fake model server behind it."""

    def test_the_page_has_the_switch_the_chat_the_models_and_every_form_carries_the_token(self):
        body = self.page()
        self.assertIn('<span class="pl d big">OFF</span>', body)
        self.assertEqual(self.form(body, "on")["csrf"], self.srv.csrf, "the token the server checks is the one in the page")
        for action, f in self.forms(body):
            self.assertEqual(f["csrf"], self.srv.csrf, action)
            self.assertEqual(f["back"], "view=ai", action)
        uses = {f["model"] for a, f in self.forms(body) if a == "use"}
        self.assertEqual(uses, {"tiny", "other"}, "one button per model")
        self.assertRegex(body, r'<input class="q" type="text" name="q" maxlength="500"[^>]*autocomplete="off"> <button class="bt on" type="submit" disabled>Ask')
        # the box takes a question at any time; the button wakes up when the AI is on
        self.assertIn("turn AI on to ask", body)
        self.assertIn("models are downloaded to", body)
        self.assertIn(html.escape(self.d), body)
        self.assertIn("nothing downloaded", body)
        self.assertNotIn("<script", body.lower())

    def test_one_click_on_a_model_sets_it_up_and_the_page_shows_it_working_and_then_on(self):
        reached, release = threading.Event(), threading.Event()
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.archive[1]) else None
        where = self.go("use", {"model": "tiny", "back": "view=ai"})
        self.assertEqual(where, "/?view=ai&sel=tiny")
        self.assertTrue(reached.wait(30))
        body = self.page(where)
        self.assertIn('<span class="pl c big">WORKING</span>', body)
        self.assertRegex(body, r'<progress max="100" value="\d+"></progress> \d+%')
        self.assertIn("tiny: downloading the model", body)
        self.assertIn('<meta http-equiv="refresh" content="2">', body, "while a job runs the page reloads by itself every 2 s")
        self.assertIsNotNone(self.form(body, "cancel"), "and the button is Cancel")
        self.assertIsNone(self.form(body, "on"))
        self.assertRegex(body, r'<button class="bt use" type="submit"[^>]* disabled>use this model</button>', "the other buttons wait")
        release.set()
        self.assertEqual(self.job()["state"], "done")
        body = self.page(where)
        self.assertIn('<span class="pl g big">ON</span>', body)
        self.assertIn("tiny runs here and answers at http://127.0.0.1:", body)
        self.assertIsNotNone(self.form(body, "off"))
        self.assertNotIn('http-equiv="refresh"', body, "idle again: no reload")
        self.assertIn('<span class="g">● in use</span>', body)
        self.assertNotRegex(body, r'<input class="q"[^>]* disabled>', "usable now: the server answers")
        self.assertIn("tiny is in use", body)
        self.assertIn("<code class=\"cmd\">%s</code>" % html.escape(self.d), body)

    def test_the_chat_answers_and_what_the_model_writes_is_inert(self):
        self.go("use", {"model": "tiny"})
        self.assertEqual(self.job()["state"], "done")
        model = ta.Fake()
        self.addCleanup(model.stop)
        self.cfg["ai"].update(endpoint=model.url, model="tiny-model", timeout_s=20)
        self.eng._web(lambda st: st.update(endpoint=model.url, model="tiny-model"))   # the page's model server is the fake one from here
        db = os.path.join(self.tmp, "history.db")
        ta.make_db(db).close()
        self.eng.history_fn = lambda: ta.ro(db)
        advisor._reset_limits()
        hostile = '<script>alert(1)</script> "><img src=x onerror=alert(1)> ' + ESC + "[2J" + BEL + " <b>bold</b>"
        model.queue.append(ta.completion(hostile))
        with mock.patch.object(advisor, "MIN_INTERVAL", 0.0):
            where = self.go("ask", {"q": '<i>why</i> "slow"?\x07'})
            self.assertTrue(where.endswith("#ask"))
            self.assertTrue(self.eng.wait_chat(30))
        body = self.page()
        self.assertIn("&lt;i&gt;why&lt;/i&gt; &quot;slow&quot;?", body, "the question is escaped")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body, "so is what the model wrote")
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("<img", body.lower())
        self.assertNotIn("<b>bold", body)
        self.assertIsNone(re.search(r"<[^>]*\son[a-z]+=", body))
        self.assertNotIn(ESC, body)
        self.assertNotIn(BEL, body)
        self.assertIn("ANSWER (AI, tiny-model)", body)
        self.assertIn("check before acting", body)
        # newest last: the second answer is after the first
        model.queue.append(ta.completion("second answer"))
        advisor._reset_limits()
        with mock.patch.object(advisor, "MIN_INTERVAL", 0.0):
            self.go("ask", {"q": "and now?"})
            self.eng.wait_chat(30)
        body = self.page()
        self.assertLess(body.index("&lt;i&gt;why"), body.index("and now?"))
        self.assertLess(body.index("&lt;script&gt;alert"), body.index("second answer"))
        self.assertLess(body.index("second answer"), body.index('id="ask"'), "the box is after the answers")

    def test_advice_now_and_its_errors(self):
        self.go("use", {"model": "tiny"})
        self.job()
        model = ta.Fake()
        self.addCleanup(model.stop)
        self.cfg["ai"].update(endpoint=model.url, model="tiny-model", timeout_s=20)
        self.eng._web(lambda st: st.update(endpoint=model.url, model="tiny-model"))
        self.eng.report_fn = lambda days: ta.make_report()
        model.queue.append(ta.completion("Check memory [oom:postgres]."))
        advisor._reset_limits()
        with mock.patch.object(advisor, "MIN_INTERVAL", 0.0):
            self.assertEqual(self.go("advise", {"days": "7"}), "/?view=ai#ask")
            self.eng.wait_chat(30)
        body = self.page()
        self.assertIn("advice on the last 7 days", body)
        self.assertIn("ADVICE (AI, tiny-model)", body)
        self.assertIn("cites: [oom:postgres]", body)
        for days in ("x", "", "-1", "²", "99999999"):
            st, _h, text = self.post("advise", {"days": days})
            self.assertIn(st, (303, 400), days)
        st, _h, text = self.post("advise", {"days": "x"})
        self.assertEqual((st, text.strip()), (400, "a number of days is needed"))
        self.go("advise", {"days": "5"})
        self.assertIn("advice is for the last 1, 7, 30 days", self.get()[2].replace("&quot;", '"'))

    def test_turn_off_and_on_again_and_the_buttons_follow(self):
        self.go("use", {"model": "tiny"})
        self.job()
        info = self.eng.child_info
        self.go("off")
        self.assertTrue(info["gone"].wait(30))
        body = self.page()
        self.assertIn('<span class="pl d big">OFF</span>', body)
        self.assertIsNotNone(self.form(body, "on"))
        self.assertIn("turning it on uses <strong>Tiny test model</strong> (chosen on this page; installed here)", body)
        n = len(self.httpd.requests)
        self.go("on")
        self.assertEqual(self.job()["state"], "done")
        self.assertIn('<span class="pl g big">ON</span>', self.page())
        self.assertEqual(len(self.httpd.requests), n, "nothing fetched again")

    def test_turn_on_with_nothing_chosen_asks_first_about_the_recommended_model_and_its_size(self):
        hw = tas.hw_of(65536, 60000)
        self.hw.update(hw)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            body = self.page()
            self.assertIn("no model is chosen yet: turning it on asks about the recommended one, <strong>Tiny test model</strong>", body)
            where = self.go("on")
            self.assertEqual(where, "/?view=ai&sel=tiny&confirm=on")
            self.assertEqual(self.httpd.requests, [], "nothing is fetched before the answer")
            body = self.page(where)
            self.assertIn('<div class="cf" id="confirm">', body)
            self.assertIn("Turn AI on with <strong>Tiny test model</strong>?", body)
            self.assertIn("to download (the SHA-256 is checked)", body)
            yes = self.form(body, "on", confirm="yes")
            self.assertEqual((yes["model"], yes["csrf"]), ("tiny", self.srv.csrf))
            self.assertIn('<a class="bt" href="/?view=ai&amp;sel=tiny">No</a>', body)
            self.assertEqual(self.go("on", yes), "/?view=ai&sel=tiny")
            self.assertEqual(self.job()["state"], "done")
        self.assertEqual((self.requests(), self.pulls()), (["/" + self.archive[0]], ["tiny:test"]))
        self.assertIn('<span class="pl g big">ON</span>', self.page())

    def test_without_a_recommendation_there_is_nothing_to_ask_the_notice_says_so(self):
        where = self.go("on")
        self.assertEqual(where, "/?view=ai")
        body = self.page(where)
        self.assertIn("no model fits this machine comfortably: choose one from the list below", body)
        self.assertIn("no model is chosen yet", body)
        self.assertEqual(self.httpd.requests, [])

    def test_delete_asks_first_and_then_deletes_one_model_or_everything(self):
        self.install()
        where = self.go("delete", {"model": "tiny", "back": "view=ai&zoom=125"})
        self.assertEqual(where, "/?view=ai&sel=tiny&confirm=delete&zoom=125")
        self.assertTrue(self.installed("tiny"), "asked, not done")
        body = self.page(where)
        self.assertIn("Delete the files of <strong>Tiny test model</strong>", body)
        yes = self.form(body, "delete", confirm="yes")
        self.assertEqual(yes["model"], "tiny")
        self.assertEqual(self.go("delete", yes), "/?view=ai&sel=tiny&zoom=125", "the page's own view comes back, the question does not")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(self.installed("tiny"))
        self.assertTrue(self.installed("other"))
        self.assertNotIn('id="confirm"', self.page("/?view=ai&sel=tiny&confirm=delete"), "nothing there to delete: no question")
        where = self.go("delete-all")
        self.assertEqual(where, "/?view=ai&confirm=delete-all")
        self.assertIn("Delete the runtime and every downloaded model", self.page(where))
        self.assertTrue(self.installed("other"))
        self.assertEqual(self.go("delete-all", {"confirm": "yes"}), "/?view=ai")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(self.installed("other"))
        self.assertFalse(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertNotIn('id="confirm"', self.page(self.go("delete-all")), "nothing left: the question is not asked")

    def test_cancel_over_http(self):
        reached, release = threading.Event(), threading.Event()
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.archive[1]) else None
        self.go("use", {"model": "tiny"})
        self.assertTrue(reached.wait(30))
        self.go("cancel")
        release.set()
        self.assertEqual(self.job()["state"], "cancelled")
        self.assertIn("cancelled", self.page())
        self.assertFalse(self.installed("tiny"))

    def test_a_failure_is_shown_in_red_with_its_reason(self):
        self.httpd.files.clear()
        self.go("use", {"model": "tiny"})
        self.job()
        body = self.page()
        self.assertRegex(body, r'<div class="note bad">[^<]*HTTP 404')
        self.assertIn('<span class="pl d big">OFF</span>', body)


class WebSecurity(WebBase):
    """Who may post, from where, how much, and what happens to what they send."""

    def state(self):
        return (self.eng.snapshot()["job"], advisor.read_web_state(self.eng.web_path()), self.httpd.requests[:])

    def assertRefused(self, resp, code, why=None):
        st, _h, body = resp
        self.assertEqual(st, code, body)
        if why:
            self.assertIn(why, body)
        self.assertIsNone(self.eng.snapshot()["job"], "nothing was started")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {}, "nothing was written")
        self.assertEqual(self.httpd.requests, [], "nothing was fetched")

    def test_clear_needs_the_token_and_forgets_the_chat_the_page_draws_in_its_box(self):
        self.eng.history.append({"kind": "ask", "q": "why <so> slow?", "res": {"text": "busy", "tools_used": [], "model": "m", "calls": [], "state": True},
                                 "error": "", "at": 0})
        body = self.page("/?app=1&view=ai")
        self.assertIn('<div class="log" role="log" aria-label="chat"><div class="log-in"><div class="qa">', body)
        self.assertIn("why &lt;so&gt; slow?", body)
        self.assertIn('action="/ai/clear"', body)
        self.assertIn(advisor.STATE_ONLY.replace("'", "&#x27;"), body)
        self.assertEqual(self.post("clear", csrf=False)[0], 403)
        self.assertEqual(self.post("clear", headers={"Origin": "http://203.0.113.9"})[0], 403)
        self.assertEqual(len(self.eng.snapshot()["chat"]["history"]), 1, "a refused post changes nothing")
        where = self.go("clear")
        self.assertEqual(where, "/?view=ai&confirm=clear#ask", "the chat goes for good: the page asks first")
        self.assertEqual(len(self.eng.snapshot()["chat"]["history"]), 1)
        body = self.page("/?app=1&view=ai&confirm=clear")
        self.assertIn("Clear the chat? Its 1 question and answer, and the prompts they were sent, are deleted for good.", body)
        self.assertIsNotNone(self.form(body, "clear", confirm="yes"))
        self.assertTrue(self.go("clear", {"confirm": "yes"}).endswith("#ask"))
        self.assertEqual(self.eng.snapshot()["chat"]["history"], [])
        self.assertEqual(self.notice(), "the chat is cleared")
        body = self.page("/?app=1&view=ai")
        self.assertNotIn('class="log"', body)
        self.assertNotIn('action="/ai/clear"', body, "nothing to clear: no button")

    def test_the_live_app_is_told_where_to_go_next_instead_of_a_redirect(self):
        st, h, body = self.post("delete", {"model": "tiny"}, headers={"Accept": "application/json"})
        self.assertEqual((st, h["Content-Type"]), (200, "application/json; charset=utf-8"))
        self.assertEqual(json.loads(body), {"to": "/?view=ai&sel=tiny&confirm=delete"}, "the question it asks first, which the app shows")
        st, h, _b = self.post("delete", {"model": "tiny"})
        self.assertEqual((st, h["Location"]), (303, "/?view=ai&sel=tiny&confirm=delete"), "a form without the script: the redirect, as always")
        self.assertEqual(self.post("delete", {"model": "tiny"}, csrf=False, headers={"Accept": "application/json"})[0], 403, "the same checks")

    def test_the_whole_prompt_of_an_answer_is_shown_on_request_and_never_in_the_engines_state(self):
        e = aiweb.chat_entry({"kind": "ask", "q": "why <slow>?", "at": 0, "res": {"text": "busy", "tools_used": [], "model": "m", "calls": [], "state": True},
                              "prompt": [{"role": "system", "text": "rules <b>"}, {"role": "user", "text": "line one\nline two"}]})
        self.eng.history.append(e)
        body = self.page("/?app=1&view=ai")
        self.assertIn('<a href="/?view=ai&amp;app=1&amp;prompt=%s#prompt">the prompt it was sent</a>' % e["id"], body)
        self.assertNotIn("rules &lt;b&gt;", body)
        body = self.page("/?app=1&view=ai&prompt=%s" % e["id"])
        self.assertIn('<section class="prompt" id="prompt">', body)
        self.assertIn('<pre class="pm-t">line one\nline two</pre>', body)
        self.assertIn("rules &lt;b&gt;", body)
        self.assertIn("for: you: why &lt;slow&gt;?", body)
        self.assertNotIn('class="prompt"', self.page("/?app=1&view=ai&prompt=000000000000"), "an exchange the chat does not have: nothing")
        self.assertNotIn('class="prompt"', self.page("/?app=1&view=ai&prompt=../../etc"), "not an id: dropped")
        st, _h, doc = self.get("/api/v1/ai?prompt=%s" % e["id"])
        d = json.loads(doc)
        self.assertEqual((st, d["prompt"]), (200, e["id"]))
        self.assertNotIn("rules", json.dumps(d["engine"]), "the engine's state carries no prompt: only the nodes of the one asked for")
        self.assertIn("line one\\nline two", doc)
        self.assertIn("rules &lt;b&gt;", self.page("/?app=0&view=ai&prompt=%s" % e["id"]), "the classic page shows it too")

    def test_clear_asks_only_when_there_is_a_chat_and_load_is_a_post_like_the_others(self):
        self.assertNotIn("Clear the chat?", self.page("/?app=1&view=ai&confirm=clear"))
        with mock.patch.object(self.eng, "load_model", return_value=(True, "x")) as load:
            self.assertEqual(self.post("load", csrf=False)[0], 403)
            self.assertEqual(self.go("load"), "/?view=ai")
        load.assert_called_once_with()

    def test_the_server_gives_the_engine_the_machine_as_its_pages_show_it(self):
        self.assertEqual(aiweb._BIND["state"], self.srv.machine_state)
        st = self.srv.machine_state()
        self.assertEqual(st["host"], advisor.clean_line(socket.gethostname(), 64))
        self.assertIn(st["os"], ("linux", "darwin", "windows"))
        self.assertTrue(any(f.startswith("CPU: ") for f in st["figures"]))
        self.assertLessEqual(len(advisor.state_text(st)), advisor.MAX_STATE_CHARS)

    def test_the_token_of_the_page_is_per_process_and_random(self):
        self.assertGreaterEqual(len(self.srv.csrf), 24)
        other = web.Server(("127.0.0.1", 0), dict(nuc_config.load()["web"]), "", demo=False)
        self.addCleanup(other.server_close)
        self.assertNotEqual(other.csrf, self.srv.csrf)
        aiweb.set_engine(self.eng)

    def test_a_post_without_the_token_or_with_another_is_refused_and_the_compare_is_constant_time(self):
        self.assertRefused(self.post("use", {"model": "tiny"}, csrf=False), 403, "not from this page")
        self.assertRefused(self.post("use", {"model": "tiny"}, csrf="x" * 32), 403)
        self.assertRefused(self.post("use", {"model": "tiny"}, csrf=""), 403)
        self.assertRefused(self.post("use", {"model": "tiny"}, csrf=self.srv.csrf + "x"), 403)
        self.assertRefused(self.post("use", {"model": "tiny"}, csrf="é" * 5), 403)
        with mock.patch.object(webhttp.hmac, "compare_digest", wraps=webhttp.hmac.compare_digest) as cmp:
            self.post("use", {"model": "tiny"}, csrf="y" * 32)
        self.assertTrue(any(c.args[1] == self.srv.csrf.encode() for c in cmp.call_args_list), "hmac.compare_digest")

    def test_a_form_of_another_site_is_refused_by_origin_referer_or_fetch_metadata(self):
        for h in ({"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1:1"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1"},
                  {"Origin": "http://127.0.0.1:%d.evil.example" % self.port}, {"Origin": "http://user@127.0.0.1:%d" % self.port},
                  {"Origin": "ftp://127.0.0.1:%d" % self.port}, {"Origin": ""}, {"Origin": None, "Referer": "http://evil.example/ai"},
                  {"Origin": None, "Referer": "http://localhost:%d/" % self.port}, {"Origin": None, "Referer": "javascript:alert(1)"},
                  {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "bogus"},
                  {"Origin": "http://127.0.0.1:%d" % self.port, "Referer": "http://evil.example/"}, {"Origin": "http://evil.example", "Referer": "http://127.0.0.1:%d/" % self.port}):
            with self.subTest(h):
                self.assertRefused(self.post("use", {"model": "tiny"}, headers=h), 403, "another site")

    def test_the_forms_of_this_page_are_accepted_whatever_the_browser_sends(self):
        for h in ({}, {"Origin": None}, {"Origin": None, "Referer": "http://127.0.0.1:%d/?view=ai&sel=tiny" % self.port},
                  {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {"Origin": "HTTP://127.0.0.1:%d" % self.port},
                  {"Origin": "https://127.0.0.1:%d" % self.port}):  # (https: a proxy that terminates TLS and keeps the Host)
            with self.subTest(h):
                st, _h, body = self.post("on", headers=h)
                self.assertEqual(st, 303, body)

    def test_body_and_headers_of_a_post(self):
        self.assertRefused(self.post("use", {"model": "tiny"}, headers={"Content-Type": "application/json"}), 415)
        self.assertRefused(self.post("use", {"model": "tiny"}, headers={"Content-Type": "text/plain"}), 415)
        self.assertRefused(self.post("use", {"model": "tiny"}, headers={"Content-Type": None}), 415)
        too_big = urlencode({"csrf": self.srv.csrf, "model": "tiny", "pad": "x" * webhttp.POST_MAX})
        self.assertRefused(self.post("use", body=too_big), 413)
        just = urlencode({"csrf": self.srv.csrf, "pad": "x"})
        just += "&p=" + "y" * (webhttp.POST_MAX - len(just) - 3)
        self.assertEqual(len(just), webhttp.POST_MAX)
        self.assertRefused(self.post("use", body=just + "z"), 413)
        many = urlencode([("csrf", self.srv.csrf)] + [("k%d" % i, "v") for i in range(40)])
        self.assertRefused(self.post("use", body=many), 400)
        self.assertRefused(self.post("use", body=b"csrf=%s&model=\xff\xfe" % self.srv.csrf.encode()), 400, "not a form")
        self.assertEqual(self.post("cancel", body=just)[0], 303, "4 KB is allowed")
        st, _h, _b = self.post("cancel", headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        self.assertEqual(st, 303)

    def test_raw_requests_with_no_length_a_wrong_one_or_chunked(self):
        head = "POST /ai/off HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\nContent-Type: application/x-www-form-urlencoded\r\n" % (self.port, self.port)
        for extra, want in (("\r\n", "400"), ("Content-Length: abc\r\n\r\n", "400"), ("Content-Length: -5\r\n\r\n", "400"), ("Content-Length: ²\r\n\r\n", "400"),
                            ("Content-Length: 99999999999\r\n\r\n", "400"), ("Content-Length: 5000\r\n\r\n" + "x" * 5000, "413"),
                            ("Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n", "400")):
            with self.subTest(extra[:40]):
                self.assertTrue(self.raw(head + extra).startswith("HTTP/1.0 " + want), extra[:40])
        self.assertIsNone(self.eng.snapshot()["job"])

    def test_only_the_known_actions_post_and_only_on_ai_paths(self):
        self.assertEqual(self.post("x", path="/")[0], 405, "everything else stays GET-only")
        self.assertEqual(self.post("x", path="/healthz")[0], 405)
        self.assertEqual(self.post("x", path="/ai")[0], 405)
        self.assertEqual(self.post("x", path="/ai/on?x=1")[0], 405, "no query on a post")
        self.assertEqual(self.post("x", path="/ai/")[0], 404)
        self.assertEqual(self.post("x", path="/ai/nope")[0], 404)
        self.assertEqual(self.post("x", path="/ai/on/")[0], 404)
        self.assertEqual(self.post("x", path="/ai/../on")[0], 404)
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            st, h, _b = self.request(m, "/ai/on")
            self.assertEqual((st, h["Allow"]), (405, "GET, HEAD"), m)
        for m in ("GET", "HEAD"):  # HEAD is answered like GET
            st, h, _b = self.request(m, "/ai/on")
            self.assertEqual((st, h["Allow"]), (405, "POST"), "a GET of an action says what it takes: " + m)
        self.assertEqual(self.request("GET", "/ai/nope")[0], 404)
        self.assertEqual(self.get("/ai/use")[0], 405)

    def test_a_model_id_is_the_catalogs_or_nothing_a_number_is_a_number(self):
        for model in ("../../etc/passwd", "tiny; rm -rf /", "TINY", "tiny\n", "", "tiny" + "x" * 100, "$(id)", "\x00", "other/../tiny"):
            for action in ("use", "delete"):
                with self.subTest(model=model, action=action):
                    st, _h, body = self.post(action, {"model": model})
                    self.assertEqual(st, 400, body)
                    self.assertIn(body.strip(), ("unknown model", "a model is needed"))
        self.assertEqual(self.post("on", {"model": "../x", "confirm": "yes"})[0], 400)
        self.assertEqual(self.state()[0], None)
        self.assertEqual(self.httpd.requests, [])

    def test_the_redirect_is_always_a_page_of_ours_whatever_back_says(self):
        for back in ("http://evil.example/", "//evil.example/x", "view=map&open=aaaaaaaaaa&all=1", "view=ai&sel=%0d%0aSet-Cookie:x=1", "\r\nX: y", "x" * 300,
                     "view=ai&cols=999999&zoom=-3&refresh=%C2%B2"):
            with self.subTest(back=back[:30]):
                st, h, _b = self.post("off", {"back": back})
                self.assertEqual(st, 303)
                where = h["Location"]
                self.assertTrue(where.startswith("/?view=ai") or where == "/?", where)
                self.assertNotIn("\r", where)
                self.assertNotIn("\n", where)
                self.assertNotIn("evil", where)
        st, h, _b = self.post("off", {"back": "view=ai&cols=100&zoom=125&sel=tiny&pause=1"})
        self.assertEqual(h["Location"], "/?view=ai&sel=tiny&cols=100&zoom=125", "the view comes back, a pause does not (the progress must show)")

    def test_the_lock_refuses_the_post_the_page_shows_no_form_and_nothing_changes(self):
        render.CFG["ai"]["web_actions"] = False
        self.assertRefused(self.post("on"), 403, "locked by config.ini ([ai] web_actions = no)")
        self.assertRefused(self.post("use", {"model": "tiny"}), 403)
        self.assertRefused(self.post("ask", {"q": "hi"}), 403)
        self.assertRefused(self.post("delete-all", {"confirm": "yes"}), 403)
        self.assertRefused(self.post("on", csrf=False), 403)
        body = self.page()
        self.assertNotIn("<form", body)
        self.assertIn("locked by config.ini ([ai] web_actions = no)", body)

    def test_the_feature_off_the_post_is_404(self):
        render.CFG["features"]["ai"] = False
        self.assertRefused(self.post("on"), 404)

    def test_no_token_needs_a_known_host_name_like_the_pages(self):
        self.assertRefused(self.post("on", headers={"Host": "evil.example", "Origin": "http://evil.example"}), 421)
        st, _h, _b = self.post("on", headers={"Host": "localhost:%d" % self.port, "Origin": "http://localhost:%d" % self.port})
        self.assertEqual(st, 303)

    def test_a_page_is_never_cached_between_viewers_and_the_post_clears_what_was_cached(self):
        before = self.page()
        self.assertIn('<span class="pl d big">OFF</span>', before)
        self.go("use", {"model": "other"})
        self.job()
        self.assertIn('<span class="pl g big">ON</span>', self.page(), "the page after a post shows what it did, not the cached one")


class WebToken(WebBase):
    token = TOKEN

    def test_the_token_is_needed_for_a_post_as_for_a_view_and_the_csrf_token_besides(self):
        st, _h, _b = self.post("off")
        self.assertEqual(st, 401, "no token")
        self.assertEqual(self.post("off", headers={"Cookie": "nuc_token=" + "x" * 24})[0], 401)
        self.assertEqual(self.post("off", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.post("off", headers={"Cookie": "nuc_token=" + TOKEN}, csrf=False)[0], 403, "the token of the page too")
        self.assertEqual(self.post("off", headers={"Cookie": "nuc_token=" + TOKEN})[0], 303)
        self.assertEqual(self.post("off", headers={"Authorization": "Bearer " + TOKEN})[0], 303)
        self.assertEqual(self.post("off", headers={"Authorization": "Bearer " + TOKEN}, path="/ai/off?token=" + TOKEN)[0], 405, "never in the URL")
        self.assertEqual(self.get("/?view=ai")[0], 401)
        st, _h, body = self.get("/?view=ai", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(st, 200)
        self.assertIn(self.srv.csrf, body)

    def test_the_host_name_is_not_what_lets_a_token_post_in_but_the_token(self):
        h = {"Host": "other.example:%d" % self.port, "Origin": "http://other.example:%d" % self.port, "Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.post("off", headers=h)[0], 303)


class WebHttpHeaders(WebBase):
    def test_the_responses_carry_the_policy_of_the_page_that_made_them(self):
        st, h, _b = self.get()
        self.assertEqual((h["Content-Security-Policy"], h["Referrer-Policy"]), (webhttp.page_csp(forms=True), "same-origin"))
        st, h, _b = self.post("off")
        self.assertEqual((st, h["Referrer-Policy"], h["Cache-Control"]), (303, "same-origin", "no-store"))
        self.assertIn("form-action 'none'", h["Content-Security-Policy"], "a redirect has no form: the strict one")
        self.assertEqual(self.get("/?view=health")[1]["Content-Security-Policy"], webhttp.CSP, "every other page keeps form-action 'none'")
        self.assertEqual(self.get("/")[1]["Content-Security-Policy"], webhttp.CSP)
        self.assertEqual(self.get("/")[1]["Referrer-Policy"], "no-referrer")
        self.assertIn("form-action 'none'", webhttp.CSP)

    def test_the_page_reloads_only_while_something_runs_or_when_locked(self):
        self.assertNotIn("http-equiv", self.page())
        self.eng.pending = {"kind": "ask", "q": "slow?", "started": time.time()}   # an answer is being written
        self.srv.cache.clear()
        body = self.page()
        self.assertIn('<meta http-equiv="refresh" content="2">', body)
        self.assertIn("slow?", body)
        self.assertIn("the model is writing the answer", body)
        self.eng.pending = None
        self.srv.cache.clear()
        render.CFG["ai"]["web_actions"] = False
        self.assertIn('<meta http-equiv="refresh" content="2">', self.page(), "locked: it reloads at the refresh interval, as the other pages")
        self.srv.cache.clear()
        self.eng.pending = {"kind": "ask", "q": "slow?", "started": time.time()}
        self.assertIn('content="2"', self.page("/?view=ai&pause=1") + 'content="2"')
        self.assertNotIn("http-equiv", self.page("/?view=ai&pause=1"), "paused: never")


# ------------------------------------------------------------------------------------------------------------------- the console

@unix_only
class ConsoleKeys(WebBase):
    """The AI screen's keys on a real engine (the fake download server, runtime and catalog behind it): e, u, x, X, c and the questions they ask."""

    def view(self):
        render._AI.clear()
        data = render.ai_data()
        rows = screens.ai_rows(data["cat"])
        av = screens.AiView()
        screens.ai_sync(av, rows)
        return data, av, rows

    def press(self, av, rows, key):
        """What main() does with a key: ai_key, then ai_do for an action."""
        act = screens.ai_key(av, key, rows)
        if act and act != "back":
            render.ai_do(av, act, rows)
        return act

    def screen(self, av, data, cols=120, rows=33):
        s, _r = render.ai_screen(data, [], av, cols - 1, rows)
        return ansi.ANSI.sub("", s)

    def select(self, av, rows, name):
        av.idx = next(i for i, r in enumerate(rows) if r["id"] == name)
        av.cur = name

    def test_u_uses_the_selected_model_with_everything_it_takes(self):
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        self.assertEqual(self.press(av, rows, "u"), "use")
        self.assertEqual(self.job()["state"], "done")
        self.assertTrue(self.installed("tiny"))
        snap = self.eng.snapshot()
        self.assertEqual((snap["state"][0], snap["server"]["model"], snap["switch"]["on"]), ("running", "tiny", True))
        data, av, rows = self.view()
        txt = self.screen(av, data, cols=240)  # wide: macOS's temporary folders are long, and the line would be cut before "free on that disk"
        self.assertIn("● ON", txt)
        self.assertIn("tiny runs here and answers at http://127.0.0.1:", txt)
        self.assertIn("tiny is in use", txt, "the answer to the key is on the screen")
        self.assertIn("folder %s" % self.d, txt)
        self.assertRegex(txt, r"folder .* · \d+ MB downloaded · [\d.]+ [GM]B free on that disk")

    def test_e_with_a_chosen_model_turns_on_and_off_without_asking(self):
        self.eng._web(lambda st: st.update(model="other"))
        data, av, rows = self.view()
        self.press(av, rows, "e")
        self.assertIsNone(av.confirm)
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.eng.snapshot()["server"]["model"], "other")
        info = self.eng.child_info
        self.press(av, rows, "e")
        self.assertTrue(info["gone"].wait(30))
        self.assertEqual(self.eng.snapshot()["state"][0], "off")
        data, av, rows = self.view()
        self.assertIn("○ OFF", self.screen(av, data))
        self.assertIn("AI is off", self.screen(av, data))

    def test_e_with_nothing_chosen_asks_about_the_recommended_model_and_its_size_first(self):
        hw = tas.hw_of(65536, 60000)
        self.hw.update(hw)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            data, av, rows = self.view()
            self.press(av, rows, "e")
            self.assertEqual(av.confirm[:2], ("on", "tiny"))
            total = len(self.archive[1]) + self.SIZES["tiny"]
            self.assertEqual(av.confirm[2], "Turn AI on with Tiny test model (%s to download)?" % screens.ai_mb(total / 2 ** 20))
            self.assertIn("Turn AI on with Tiny test model", self.screen(av, data).splitlines()[-1])
            self.assertEqual(self.httpd.requests, [], "nothing is fetched before the y")
            self.assertEqual(self.press(av, rows, "n"), "")
            self.assertIsNone(av.confirm)
            self.assertIsNone(self.eng.snapshot()["job"])
            self.press(av, rows, "e")
            self.assertEqual(self.press(av, rows, "y"), "yes")
            self.assertIsNone(av.confirm)
            self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.eng.snapshot()["state"][0], "running")
        self.assertEqual((self.requests(), self.pulls()), (["/" + self.archive[0]], ["tiny:test"]))

    def test_the_screen_shows_the_progress_and_c_cancels(self):
        reached, release = threading.Event(), threading.Event()
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.archive[1]) else None
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        self.press(av, rows, "u")
        self.assertTrue(reached.wait(30))
        txt = self.screen(av, data)
        self.assertIn("◐ WORKING", txt)
        self.assertRegex(txt, r"tiny: downloading the model, \d+%, [\d.]+ [MK]B of [\d.]+ MB")
        self.assertRegex(txt, r"[█░]{14}")
        self.assertIn("(c: cancel)", txt)
        self.assertIn("c: cancel", txt.splitlines()[-1])
        self.press(av, rows, "c")
        release.set()
        self.assertEqual(self.job()["state"], "cancelled")
        data, av, rows = self.view()
        txt = self.screen(av, data)
        self.assertIn("○ OFF", txt)
        self.assertIn("cancelled", txt)

    def test_x_and_X_ask_first_and_y_deletes(self):
        self.install()
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        self.press(av, rows, "x")
        self.assertEqual(av.confirm[:2], ("delete", "tiny"))
        self.assertTrue(av.confirm[2].startswith("Delete the files of Tiny test model ("))
        self.assertTrue(self.installed("tiny"), "asked, not done")
        self.press(av, rows, "z")
        self.assertIsNone(av.confirm)
        self.assertTrue(self.installed("tiny"))
        self.press(av, rows, "x")
        self.press(av, rows, "y")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(self.installed("tiny"))
        self.assertTrue(self.installed("other"))
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        self.press(av, rows, "x")
        self.assertIsNone(av.confirm)
        self.assertEqual(av.msg[0], "warn")
        self.assertIn("not installed: nothing to delete", self.screen(av, data))
        self.press(av, rows, "X")
        self.assertEqual(av.confirm[:2], ("delete-all", None))
        self.assertTrue(av.confirm[2].startswith("Delete the runtime and every downloaded model ("))
        self.press(av, rows, "y")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(self.installed("other"))
        data, av, rows = self.view()
        self.press(av, rows, "X")
        self.assertIsNone(av.confirm)
        self.assertEqual(av.msg, ("warn", "nothing is downloaded: nothing to delete"))

    def test_a_failure_is_a_line_and_the_screen_goes_on(self):
        self.httpd.files.clear()
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        self.press(av, rows, "u")
        self.assertEqual(self.job()["state"], "failed")
        data, av, rows = self.view()
        self.assertRegex(self.screen(av, data), r"setting up tiny failed: .*HTTP 404")

    def test_locked_by_config_ini_a_key_is_a_line_and_nothing_else(self):
        data, av, rows = self.view()
        self.select(av, rows, "tiny")
        render.CFG["ai"]["web_actions"] = False
        for key in "euxXc":
            self.press(av, rows, key)
            self.assertEqual(av.msg, ("err", aiweb.LOCKED), key)
            self.assertIsNone(av.confirm)
        self.assertIsNone(self.eng.snapshot()["job"])
        self.assertEqual(self.httpd.requests, [])
        data, av, rows = self.view()
        self.assertNotIn("e: AI on/off", self.screen(av, data, 200))
        self.assertIn("locked by config.ini ([ai] web_actions = no): this screen only shows", self.screen(av, data, 200, 40))

    def test_ai_do_never_raises(self):
        data, av, rows = self.view()
        with mock.patch.object(render, "ai_engine", side_effect=RuntimeError("boom")):
            render.ai_do(av, "toggle", rows)
        self.assertEqual(av.msg[0], "err")
        self.assertIn("could not do that", av.msg[1])
        render.ai_do(av, "use", [])
        render.ai_do(av, "yes", rows)


class Module(unittest.TestCase):
    def test_the_engine_is_one_per_process_and_the_version_needs_none(self):
        old = aiweb.set_engine(None)
        self.addCleanup(aiweb.set_engine, old)
        self.assertEqual(aiweb.version(), 0, "asking the version makes no engine")
        self.assertIsNone(aiweb._ENGINE)
        eng = aiweb.engine()
        self.assertIs(aiweb.engine(), eng)
        eng.version += 1
        self.assertEqual(aiweb.version(), 1)
        aiweb.shutdown()
        demo = aiweb.configure(demo=True, os_name="darwin")
        self.assertIs(aiweb.engine(), demo)
        self.assertTrue(demo.demo)
        self.assertEqual(demo._demo_os, "darwin")

    def test_bound_settings_and_report(self):
        old = dict(aiweb._BIND)
        self.addCleanup(lambda: (aiweb._BIND.clear(), aiweb._BIND.update(old)))
        aiweb._BIND.clear()
        self.assertEqual(aiweb.default_cfg()["ai"]["web_actions"], True, "without a program's own: config.ini")
        aiweb.bind(cfg=lambda: {"ai": {"x": 1}}, report=lambda days: {"days": days})
        self.assertEqual(aiweb.default_cfg(), {"ai": {"x": 1}})
        self.assertEqual(aiweb.default_report(30), {"days": 30})

    def test_the_engine_does_not_import_the_screens(self):
        with open(os.path.join(HERE, "..", "src", "aiweb.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotRegex(src, r"^\s*(import|from) (render|web)\b", "render may be __main__: importing it from here would run it twice")


if __name__ == "__main__":
    unittest.main()
