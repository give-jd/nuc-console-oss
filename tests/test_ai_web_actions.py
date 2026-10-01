"""What the AI page and the AI screen DO (src/aiweb.py, and its two front ends in src/web.py and src/render.py): the engine's jobs (download
with progress, cancel, never twice, delete, start and stop of the model server, the chat), the on/off switch and web.json, the lock, and
the demo; then the POST endpoints of the web view (CSRF, Origin, token, body, redirect), the page and its forms, and the console keys.

Hermetic: a fake loopback server stands in for GitHub and Hugging Face (allow_loopback_http), a fake runtime script for llamafile, the
fake OpenAI-compatible server of test_advisor.py for the model, a temporary AI folder; no test waits for the clock (events and joins).
Platform-aware: Windows has no POSIX runtime script (the start and stop tests are for Unix), macOS runners are arm64, and a temporary
folder's parents are not always readable by others.
"""
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import advisor  # noqa: E402
import aiweb  # noqa: E402
import aisetup  # noqa: E402
import nuc_config  # noqa: E402
import test_advisor as ta  # noqa: E402  (the fake model server, the history and the report of that file)
import test_aisetup as tas  # noqa: E402  (the fake download server, the fake advice of aihw)

POSIX = os.name == "posix"
RUNTIME_NAME = "llamafile-0.0"
unix_only = unittest.skipUnless(POSIX, "the fake runtime is a Unix script (Windows runs a real .exe)")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def runtime_script(kind="serve"):
    """The bytes of a fake llamafile for `/bin/sh runtime --server --host H --port N -m FILE -a ID ...`: serve = answers GET /v1/models on that port
    until it is stopped; die = writes a line and exits; stubborn = ignores SIGTERM."""
    serve = ("import http.server, json, sys\\n"
             "a = sys.argv[1:]\\nport = int(a[a.index(\"--port\") + 1])\\nmodel = a[a.index(\"-a\") + 1]\\n"
             "print(\"fake runtime up on\", port, flush=True)\\n"
             "class H(http.server.BaseHTTPRequestHandler):\\n"
             "    def log_message(self, *x): pass\\n"
             "    def do_GET(self):\\n"
             "        b = json.dumps({\"data\": [{\"id\": model}]}).encode()\\n"
             "        self.send_response(200); self.send_header(\"Content-Length\", str(len(b))); self.end_headers(); self.wfile.write(b)\\n"
             "http.server.ThreadingHTTPServer((\"127.0.0.1\", port), H).serve_forever()\\n")
    stubborn = ("import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\nprint(\"up\", flush=True)\\n"
                "while True:\\n    time.sleep(0.05)\\n")
    if kind == "die":
        return b"#!/bin/sh\necho 'boom: unknown flag --server' >&2\nexit 3\n"
    if kind == "dies-loading":
        return b"#!/bin/sh\necho 'loading the model...'\nsleep 0.6\necho 'out of memory' >&2\nexit 4\n"
    if kind == "mute":  # loads for ever: it never answers
        return ("#!/bin/sh\nexec '%s' -c 'import time\nprint(\"loading\", flush=True)\nwhile True:\n    time.sleep(0.05)\n'\n" % sys.executable).encode()
    code = (stubborn if kind == "stubborn" else serve).replace("\\n", "\n")
    return ("#!/bin/sh\nexec '%s' -c '%s' \"$@\"\n" % (sys.executable, code.replace("'", "'\"'\"'"))).encode()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    n = s.getsockname()[1]
    s.close()
    return n


class Base(unittest.TestCase):
    """An engine on a temporary AI folder with a fake catalog (a runtime and two models), the fake download server and the fake model server."""

    MODEL_BYTES = {"tiny": tas.blob(2_600_000, b"tiny"), "other": tas.blob(30_000, b"other")}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.d = os.path.join(self.tmp, "ai")
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_HOME": os.path.join(self.tmp, "home"), "NUC_CONSOLE_CONFIG": os.path.join(self.tmp, "config.ini")})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = nuc_config.load("/nonexistent")
        self.rt_bytes = runtime_script("serve")
        self.runtime, _m = tas.fake_manifest("http://127.0.0.1:1", self.rt_bytes, b"x")
        self.models = []
        for name, data in self.MODEL_BYTES.items():
            m = tas.fake_manifest("http://127.0.0.1:1", self.rt_bytes, data, name)[1]
            self.models.append(dict(m, rank=len(self.models) + 1, approx_mb=3, ram_mb=100))
        self.dl = tas.Server()
        self.httpd = self.dl.__enter__()
        self.addCleanup(self.dl.__exit__)
        self.httpd.files = {"/" + RUNTIME_NAME: self.rt_bytes, "/tiny.gguf": self.MODEL_BYTES["tiny"], "/other.gguf": self.MODEL_BYTES["other"]}
        patch = mock.patch.object(aisetup, "model_url", lambda m: self.dl.base + "/" + m["file"])
        patch.start()
        self.addCleanup(patch.stop)
        self.runtime = dict(self.runtime, url=self.dl.base + "/" + RUNTIME_NAME)
        self.hw = {}
        self.popens = []
        port = mock.patch.object(aisetup, "DEFAULT_PORT", free_port())  # the servers of these tests start where no other test run (or program) is
        port.start()
        self.addCleanup(port.stop)
        self.eng = aiweb.Engine(directory=self.d, runtime=self.runtime, models=self.models, allow_loopback_http=True, cfg_fn=lambda: self.cfg,
                                hw_fn=lambda: self.hw, popen=self.popen)
        self.eng.start_wait, self.eng.stop_grace, self.eng.poll_s = 0.2, 0.5, 0.05
        self.addCleanup(self.eng.shutdown)

    def popen(self, argv, **kw):
        self.popens.append((argv, kw))
        return subprocess.Popen(argv, **kw)

    def m(self, name="tiny"):
        return next(m for m in self.models if m["id"] == name)

    def install(self, names=("tiny", "other"), script=None):
        aisetup.ensure_dirs(self.d)
        rt = aisetup.runtime_path(self.d, self.runtime)
        tas.put(rt, script if script is not None else self.rt_bytes)
        aisetup.record(self.d, rt, sha(script if script is not None else self.rt_bytes))
        if script is not None:  # the pin is of the script that runs
            self.runtime.update(sha256=sha(script), size=len(script))
        for n in names:
            tas.put(aisetup.model_path(self.d, self.m(n)), self.MODEL_BYTES[n])
            aisetup.record(self.d, aisetup.model_path(self.d, self.m(n)), self.m(n)["sha256"])

    def installed(self, name="tiny"):
        return aisetup.is_verified(self.d, aisetup.model_path(self.d, self.m(name)), self.m(name))

    def job(self):
        self.assertTrue(self.eng.wait(30), "the job did not end")
        return self.eng.snapshot()["job"]

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
    """Choosing a model does everything: fetch what is missing, start the server, wait until it answers, make it the advisor's, turn it on."""

    def use(self, name="tiny"):
        ok, text = self.eng.use_model(name)
        self.assertTrue(ok, text)
        return self.job()

    def answering(self):
        srv = self.eng.snapshot()["server"]
        ok, ids, why = aisetup.probe(srv["endpoint"], 2)
        self.assertTrue(ok, why)
        return srv, ids

    def test_one_choice_downloads_starts_waits_and_turns_the_advisor_on(self):
        job = self.use("tiny")
        self.assertEqual((job["state"], job["error"]), ("done", ""))
        self.assertIn("tiny is in use", job["note"])
        self.assertTrue(self.installed("tiny"))
        self.assertEqual(self.requests(), ["/" + RUNTIME_NAME, "/tiny.gguf"])
        srv, ids = self.answering()
        self.assertEqual((srv["running"], srv["model"], ids), (True, "tiny", ["tiny"]), "it answers when the job is done")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"enabled": True, "model": "tiny", "endpoint": srv["endpoint"]})
        snap = self.eng.snapshot()
        self.assertEqual(snap["switch"], {"on": True, "by": "web", "locked": False})
        self.assertEqual(snap["state"][0], "running")
        self.assertIn("tiny runs here and answers at %s" % srv["endpoint"], snap["state"][1])
        self.assertEqual(self.eng.ecfg()["ai"]["model"], "tiny")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "config.ini")))

    def test_the_progress_is_the_jobs_while_it_runs(self):
        phases = []
        self.eng.progress_hook = lambda job: phases.append((job["step"], job["phase"], aiweb.job_text(job)))
        self.use("tiny")
        self.assertTrue(any(step == "runtime" for step, _p, _t in phases))
        self.assertTrue(any("downloading the model" in text for _s, _p, text in phases))
        self.assertTrue(any(p == "verifying" for _s, p, _t in phases))

    def test_the_same_model_again_is_not_fetched_or_started_again(self):
        self.use("tiny")
        pid, n = self.eng.snapshot()["server"]["pid"], len(self.httpd.requests)
        job = self.use("tiny")
        self.assertEqual(job["state"], "done")
        self.assertEqual(self.eng.snapshot()["server"]["pid"], pid, "the server runs: nothing to start")
        self.assertEqual(len(self.httpd.requests), n)
        self.assertEqual(len(self.popens), 1)

    def test_another_model_replaces_the_server_one_at_a_time(self):
        self.use("tiny")
        first = self.eng.snapshot()["server"]["pid"]
        self.use("other")
        srv, ids = self.answering()
        self.assertEqual((srv["model"], ids), ("other", ["other"]))
        self.assertNotEqual(srv["pid"], first)
        self.assertFalse(alive(first), "the first server is stopped")
        self.assertEqual(len(self.popens), 2)
        self.assertEqual(advisor.read_web_state(self.eng.web_path())["model"], "other")
        self.assertEqual(self.requests(), ["/" + RUNTIME_NAME, "/tiny.gguf", "/other.gguf"], "the runtime is fetched once")

    def test_a_download_that_fails_leaves_the_advisor_as_it_was(self):
        self.httpd.files.pop("/tiny.gguf")
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("HTTP 404", job["error"])
        self.assertEqual(self.popens, [], "no server for a model that is not there")
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {})
        self.assertEqual(self.eng.snapshot()["state"][0], "off")

    def test_a_server_that_dies_while_it_loads_fails_the_job_and_the_advisor_stays_off(self):
        self.install(script=runtime_script("dies-loading"))
        self.eng.start_wait = 0.0   # not at once: while it loads
        job = self.use("tiny")
        self.assertEqual(job["state"], "failed")
        self.assertIn("stopped while it loaded the model (exit status 4)", job["error"])
        self.assertIn("out of memory", job["error"])
        st = advisor.read_web_state(self.eng.web_path())
        self.assertNotIn("enabled", st)
        self.assertNotIn("endpoint", st, "nobody answers there")
        self.assertEqual(self.eng.snapshot()["state"][0], "off")

    def test_cancel_while_the_model_loads_stops_the_server_it_started(self):
        self.install(script=runtime_script("mute"))
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
        self.assertEqual(len(self.httpd.requests), n, "nothing fetched again")

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
            total = len(self.rt_bytes) + len(self.MODEL_BYTES["tiny"])
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
            self.assertEqual(self.requests(), [], "installed: not one request")

    def test_the_size_to_fetch_counts_what_is_partly_there_and_says_unknown_when_not_pinned(self):
        part = aisetup.model_path(self.d, self.m("tiny")) + ".part"
        os.makedirs(os.path.dirname(part))
        tas.put(part, b"p" * 1000)
        self.assertEqual(self.eng._missing_bytes(self.d, self.m("tiny")), len(self.rt_bytes) + len(self.MODEL_BYTES["tiny"]) - 1000)
        self.assertIsNone(self.eng._missing_bytes(self.d, dict(self.m("tiny"), sha256=None)))


# ---------------------------------------------------------------------------------------------------------------------- download

class Download(Base):
    def test_download_runtime_and_model_with_progress_and_the_checks_of_the_command(self):
        seen = []
        self.eng.progress_hook = lambda job: seen.append((job["step"], job["done"], job["total"], job["phase"]))
        ok, text = self.eng.download("tiny")
        self.assertTrue(ok, text)
        self.assertIn("downloading tiny", text)
        job = self.job()
        self.assertEqual((job["state"], job["error"]), ("done", ""))
        self.assertEqual(job["pct"], 100)
        total = len(self.rt_bytes) + len(self.MODEL_BYTES["tiny"])
        self.assertEqual((job["done"], job["total"]), (total, total))
        self.assertEqual([s for s, *_ in seen][0], "runtime", "the runtime first")
        self.assertEqual(seen[-1][0], "tiny")
        dones = [d for _s, d, _t, _p in seen]
        self.assertEqual(dones, sorted(dones), "progress only goes forward")
        self.assertGreater(len(seen), 2, "the model is several chunks: there is progress to show")
        self.assertTrue({t for _s, _d, t, _p in seen} == {total})
        self.assertIn("verifying", {p for *_x, p in seen}, "after the last byte the SHA-256 is checked")
        self.assertTrue(self.installed("tiny"))
        self.assertTrue(aisetup.is_verified(self.d, aisetup.runtime_path(self.d, self.runtime), self.runtime))
        self.assertEqual(aisetup.sha256_file(aisetup.model_path(self.d, self.m())), self.m()["sha256"])
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m()) + ".part"))
        self.assertIn("tiny is installed", self.notice())
        if POSIX:
            self.assertEqual(os.stat(aisetup.runtime_path(self.d, self.runtime)).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(aisetup.model_path(self.d, self.m())).st_mode & 0o777, 0o644)
        self.assertEqual(self.requests(), ["/" + RUNTIME_NAME, "/tiny.gguf"])

    def test_a_file_that_is_there_and_verified_is_never_fetched_again(self):
        self.eng.download("tiny")
        self.job()
        n = len(self.httpd.requests)
        ok, _t = self.eng.download("tiny")
        self.assertTrue(ok)
        job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("already installed", job["note"])
        self.assertEqual(len(self.httpd.requests), n, "not one request")
        self.eng.download("other")  # a second model: only its own file, the runtime is there
        self.job()
        self.assertEqual(self.requests()[n:], ["/other.gguf"])

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
        self.assertEqual(self.requests().count("/tiny.gguf"), 1)

    def test_cancel_keeps_what_was_fetched_and_the_next_download_goes_on_from_there(self):
        self.eng.progress_hook = lambda job: self.eng.cancel() if job["step"] == "tiny" and job["done"] > len(self.rt_bytes) else None
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "cancelled")
        self.assertIn("kept", job["note"])
        self.assertFalse(self.installed("tiny"))
        part = aisetup.model_path(self.d, self.m()) + ".part"
        size = os.path.getsize(part)
        self.assertTrue(0 < size < len(self.MODEL_BYTES["tiny"]), size)
        self.assertTrue(aisetup.is_verified(self.d, aisetup.runtime_path(self.d, self.runtime), self.runtime), "the runtime was done before")
        self.assertIn("cancelled", self.notice())
        self.eng.progress_hook = None
        self.eng.download("tiny")
        self.assertEqual(self.job()["state"], "done")
        self.assertEqual(self.httpd.requests[-1], ("/tiny.gguf", "bytes=%d-" % size))
        self.assertTrue(self.installed("tiny"))

    def test_cancel_without_a_job_and_of_a_job_that_is_not_a_download(self):
        self.assertEqual(self.eng.cancel(), (False, "nothing to cancel"))
        self.install()
        reached, release = threading.Event(), threading.Event()
        real = aisetup.forget
        with mock.patch.object(aisetup, "forget", side_effect=lambda *a: (reached.set(), release.wait(30), real(*a))[2]):
            self.eng.delete("other")
            self.assertTrue(reached.wait(30))
            ok, text = self.eng.cancel()
            release.set()
        self.assertFalse(ok)
        self.assertIn("cannot be cancelled", text)
        self.assertEqual(self.job()["state"], "done")

    def test_failures_say_why_and_leave_nothing_installed(self):
        self.httpd.files.pop("/tiny.gguf")
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("HTTP 404", job["error"])
        self.assertIn("failed", self.notice())
        self.assertFalse(self.installed("tiny"))
        self.httpd.files["/tiny.gguf"] = bytes(len(self.MODEL_BYTES["tiny"]))   # the right size, the wrong bytes
        self.eng.download("tiny")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("SHA-256 is", job["error"])
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m())))
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m()) + ".part"), "a wrong hash deletes the download")

    def test_the_server_never_gets_a_bad_wish(self):
        n = len(self.httpd.requests)
        self.assertEqual(self.eng.download("../../etc/passwd")[0], False)
        self.assertIn("unknown model", self.notice())
        unpinned = dict(self.m("other"), id="loose", sha256=None)
        self.eng.models.append(unpinned)
        self.eng.download("loose")
        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("not pinned", job["error"])
        self.assertEqual(len(self.httpd.requests), n)

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
        self.httpd.files.pop("/" + RUNTIME_NAME)
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
    def test_one_model_with_its_partial_download_and_its_stamp(self):
        self.install()
        part = aisetup.model_path(self.d, self.m("tiny")) + ".part"
        tas.put(part, b"p" * 100)
        self.eng._web(lambda st: st.update(model="tiny"))
        ok, _t = self.eng.delete("tiny")
        self.assertTrue(ok)
        job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("deleted 2 files", job["note"])
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m("tiny"))))
        self.assertFalse(os.path.exists(part))
        self.assertNotIn("tiny.gguf", aisetup.load_stamp(self.d))
        self.assertTrue(self.installed("other"), "the others stay")
        self.assertTrue(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertNotIn("model", advisor.read_web_state(self.eng.web_path()), "the model the page had chosen is gone")
        self.assertFalse(os.path.exists(os.path.join(self.d, aiweb.LOCK_FILE)))

    def test_everything_the_runtime_the_models_and_the_loader_it_unpacked(self):
        self.install()
        os.makedirs(os.path.join(self.d, "home", ".llamafile"))
        tas.put(os.path.join(self.d, "home", ".llamafile", "x"), b"x")
        tas.put(os.path.join(self.d, "notes.txt"), "not ours")
        self.eng._web(lambda st: st.update(model="other", enabled=True))
        self.eng.delete_all()
        job = self.job()
        self.assertEqual(job["state"], "done")
        for m in self.models:
            self.assertFalse(os.path.exists(aisetup.model_path(self.d, m)))
        self.assertFalse(os.path.exists(aisetup.runtime_path(self.d, self.runtime)))
        self.assertFalse(os.path.exists(os.path.join(self.d, "home")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "notes.txt")), "only what the catalog put there")
        st = advisor.read_web_state(self.eng.web_path())
        self.assertEqual(st, {"enabled": True}, "the switch stays, the model is gone")
        self.assertEqual(aisetup.dir_space(self.d)["used"], 0)

    def test_nothing_to_delete(self):
        self.eng.delete("tiny")
        job = self.job()
        self.assertEqual(job["state"], "done")
        self.assertIn("nothing to delete", job["note"])

    def test_files_this_account_cannot_delete_name_the_command_to_run_instead(self):
        self.install()
        real = os.unlink

        def unlink(p, *a):
            if p.endswith("tiny.gguf"):
                raise PermissionError(13, "Permission denied", p)
            return real(p, *a)
        with mock.patch.object(aiweb.os, "unlink", side_effect=unlink):
            self.eng.delete("tiny")
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("tiny.gguf", job["error"])
        self.assertIn("belong to another account", job["error"])
        self.assertIn(aisetup.commands_for("tiny")["remove"], job["error"])
        self.assertTrue(self.installed("tiny"), "still there, and still known as installed")
        with mock.patch.object(aiweb.os, "unlink", side_effect=unlink):
            self.eng.delete_all()
            job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("nuc-console-ai remove", job["error"])
        self.assertFalse(os.path.exists(aisetup.model_path(self.d, self.m("other"))), "what could be deleted was")

    def test_a_model_the_server_started_here_runs_is_not_deleted_under_it(self):
        if not POSIX:
            self.skipTest("the fake runtime is a Unix script")
        self.install()
        self.eng.start_server("tiny")
        self.assertEqual(self.job()["state"], "done")
        pid = self.eng.snapshot()["server"]["pid"]
        self.eng.delete("tiny")
        self.assertEqual(self.job()["state"], "done")
        self.assertFalse(self.eng.snapshot()["server"]["running"])
        self.assertFalse(alive(pid))


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
        self.assertEqual(self.eng.snapshot()["switch"], {"on": False, "by": "", "locked": False})
        self.assertTrue(self.eng.set_enabled(True)[0])
        self.assertEqual(self.eng.snapshot()["switch"], {"on": True, "by": "web", "locked": False})
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"enabled": True})
        self.assertTrue(self.eng.ecfg()["ai"]["enabled"])
        self.assertFalse(self.cfg["ai"]["enabled"], "config.ini's own is not touched")
        self.assertTrue(self.eng.set_enabled(False)[0])
        self.assertEqual(self.eng.snapshot()["switch"]["on"], False)
        self.assertFalse(self.eng.ecfg()["ai"]["enabled"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "config.ini")), "config.ini is never written")

    def test_config_ini_yes_cannot_be_turned_off_from_here(self):
        self.cfg["ai"]["enabled"] = True
        self.assertEqual(self.eng.snapshot()["switch"], {"on": True, "by": "config", "locked": False})
        ok, text = self.eng.set_enabled(False)
        self.assertFalse(ok)
        self.assertIn("on by config.ini", text)
        self.assertTrue(self.eng.set_enabled(True)[0], "turning on what is on is harmless")

    def test_the_lock_refuses_every_action_and_changes_nothing(self):
        self.install()
        self.cfg["ai"]["web_actions"] = False
        n = len(self.httpd.requests)
        for call in (lambda: self.eng.set_enabled(True), lambda: self.eng.turn_on("tiny"), self.eng.turn_on, lambda: self.eng.use_model("tiny"), self.eng.turn_off,
                     lambda: self.eng.download("other"), self.eng.cancel, lambda: self.eng.delete("tiny"), self.eng.delete_all, self.eng.start_server,
                     self.eng.stop_server, lambda: self.eng.ask("why?"), lambda: self.eng.advise(7)):
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

    def test_start_runs_the_server_as_a_child_on_loopback_and_stop_ends_it(self):
        self.install()
        job = self.start()
        self.assertEqual((job["state"], job["error"]), ("done", ""))
        srv = self.eng.snapshot()["server"]
        self.assertTrue(srv["running"])
        self.assertEqual(srv["model"], "tiny")
        self.assertTrue(alive(srv["pid"]))
        port = int(srv["endpoint"].rsplit(":", 1)[1].split("/")[0])
        self.assertEqual(srv["endpoint"], "http://127.0.0.1:%d/v1" % port)
        ok, ids, why = aisetup.probe(srv["endpoint"], 2)
        self.assertEqual((ok, ids), (True, ["tiny"]), "the job ends when the server answers: %s" % why)
        self.assertEqual(advisor.read_web_state(self.eng.web_path()), {"model": "tiny", "endpoint": srv["endpoint"]})
        eff = self.eng.ecfg()["ai"]
        self.assertEqual((eff["model"], eff["endpoint"]), ("tiny", srv["endpoint"]))
        # how it was started: the command line of `serve`, 127.0.0.1 only, a small environment, its own process group, low priority
        argv, kw = self.popens[0]
        sh = argv.index("/bin/sh")
        self.assertIn(sh, (0, 3))
        if sh:
            self.assertEqual((os.path.basename(argv[0]), argv[1:3]), ("nice", ["-n", "10"]), "low priority, like `serve`")
        self.assertEqual(argv[argv.index("/bin/sh") + 1], aisetup.runtime_path(self.d, self.runtime))
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("-m") + 1], aisetup.model_path(self.d, self.m()))
        self.assertEqual(argv[argv.index("-a") + 1], "tiny")
        self.assertEqual(argv[argv.index("--gpu") + 1], "disable", "no hardware reading: CPU only")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(set(kw["env"]), {"PATH", "HOME", "TMPDIR", "LANG"})
        self.assertEqual(kw["env"]["HOME"], os.path.join(self.d, "home"))
        self.assertNotIn("NUC_CONSOLE_HOME", kw["env"], "this process's environment is not the server's")
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
        self.install(script=runtime_script("die"))
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
        self.install(script=runtime_script("stubborn"))
        self.eng.start_wait, self.eng.load_wait = 0.1, 0.2   # it never answers: the job gives up, the server stays
        job = self.start()
        self.assertIn("has not answered", job["error"])
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
            port = self.eng._pick_port(None)
        self.assertNotEqual(port, taken)
        self.assertTrue(taken < port < taken + aiweb.PORT_TRIES)
        with mock.patch.object(aisetup, "DEFAULT_PORT", taken), mock.patch.object(aiweb, "PORT_TRIES", 1):
            self.assertRaises(aisetup.SetupError, self.eng._pick_port, None)

    def test_the_gpu_plan_of_serve_decides_the_layers(self):
        self.install()
        hw = tas.hw_of(65536, 60000, [tas.gpu_of("RTX", 24576, 22000)])
        self.eng.hw_fn = lambda: hw
        self.eng.models[0] = dict(self.m("tiny"), approx_mb=2000, ram_mb=2800, layers=32)
        with mock.patch.dict(sys.modules, {"aihw": tas.FakeAihw(hw)}):
            self.start()
            argv = self.popens[0][0]
            self.assertEqual(argv[argv.index("--gpu") + 1], "auto")
            self.assertEqual(argv[argv.index("-ngl") + 1], str(aisetup.ALL_LAYERS))
            self.eng.stop_server()
            self.eng.child_info["gone"].wait(30)
            self.cfg["ai"]["gpu"] = "no"
            self.start()
            argv = self.popens[1][0]
            self.assertEqual(argv[argv.index("--gpu") + 1], "disable", "[ai] gpu = no")
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1", "whatever the plan, this machine only")


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

    def test_the_last_ten_are_kept_oldest_first(self):
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

    def test_a_bug_ends_the_answer_not_the_thread(self):
        with mock.patch.object(advisor, "ask", side_effect=RuntimeError("secret detail")), contextlib.redirect_stderr(__import__("io").StringIO()):
            self.eng.ask("boom?")
            e = self.chat()["history"][-1]
        self.assertIn("unexpected error: RuntimeError", e["error"])
        self.assertNotIn("secret detail", e["error"], "the detail goes to the log, not to the page")
        self.assertFalse(self.eng.snapshot()["chat"]["busy"])


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
        self.assertEqual(snap["switch"], {"on": True, "by": "web", "locked": False})
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
