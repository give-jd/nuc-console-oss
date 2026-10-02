"""What the AI page and the AI screen DO (src/aiweb.py, and its two front ends in src/web.py and src/render.py): the engine's jobs (download
with progress, cancel, never twice, delete, start and stop of the model server, the chat), the on/off switch and web.json, the lock, and
the demo; then the POST endpoints of the web view (CSRF, Origin, token, body, redirect), the page and its forms, and the console keys.

Hermetic: a fake loopback server stands in for GitHub and Hugging Face (allow_loopback_http), a fake runtime script for llamafile, the
fake OpenAI-compatible server of test_advisor.py for the model, a temporary AI folder; no test waits for the clock (events and joins).
Platform-aware: the start and stop tests are for Unix (/bin/sh, process groups, signals); on Windows, where the engine starts the runtime as an
.exe, the fake one is Python source that Base.popen runs with this interpreter. macOS runners are arm64, and a temporary folder's parents are not
always readable by others.
"""
import contextlib
import hashlib
import html
import http.client
import io
import json
import os
import shutil
import signal
import socket
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
import aiweb  # noqa: E402
import aisetup  # noqa: E402
import nuc_config  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ansi  # noqa: E402
import web  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from webtest import classic_default  # noqa: E402
import test_advisor as ta  # noqa: E402  (the fake model server, the history and the report of that file)
import test_aisetup as tas  # noqa: E402  (the fake download server, the fake advice of aihw)

POSIX = os.name == "posix"
RUNTIME_NAME = "llamafile-0.0"
unix_only = unittest.skipUnless(POSIX, "start and stop on Unix: /bin/sh, process groups, signals")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def runtime_script(kind="serve"):
    """The bytes of a fake llamafile for `/bin/sh runtime --server --host H --port N -m FILE -a ID ...`: serve = answers GET /v1/models on that port
    until it is stopped; die = writes a line and exits; stubborn = ignores SIGTERM. Windows starts `runtime.exe --server ...`: serve and stubborn
    are then the Python source itself, which Base.popen hands to this interpreter (a script cannot be an .exe)."""
    serve = ("import faulthandler, http.server, json, signal, socketserver, sys\\nif hasattr(signal, \"SIGUSR1\"): faulthandler.register(signal.SIGUSR1)\\n"  # Unix: where it is (Base.where)
             "a = sys.argv[1:]\\nport = int(a[a.index(\"--port\") + 1])\\nmodel = a[a.index(\"-a\") + 1]\\n"
             "print(\"fake runtime up on\", port, flush=True)\\n"
             "class H(http.server.BaseHTTPRequestHandler):\\n"
             "    def log_message(self, *x): pass\\n"
             "    def do_GET(self):\\n"
             "        b = json.dumps({\"data\": [{\"id\": model}]}).encode()\\n"
             "        self.send_response(200); self.send_header(\"Content-Length\", str(len(b))); self.end_headers(); self.wfile.write(b)\\n"
             "class S(http.server.ThreadingHTTPServer):\\n"
             "    def server_bind(self): socketserver.TCPServer.server_bind(self)\\n"  # not HTTPServer's: its socket.getfqdn() hangs on macOS runners
             "s = S((\"127.0.0.1\", port), H)\\nprint(\"listening\", flush=True)\\ns.serve_forever()\\n")
    stubborn = ("import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\nprint(\"up\", flush=True)\\n"
                "while True:\\n    time.sleep(0.05)\\n")
    if kind == "die":
        return b"#!/bin/sh\necho 'boom: unknown flag --server' >&2\nexit 3\n"
    if kind == "dies-loading":
        return b"#!/bin/sh\necho 'loading the model...'\nsleep 0.6\necho 'out of memory' >&2\nexit 4\n"
    if kind == "mute":  # loads for ever: it never answers
        return ("#!/bin/sh\nexec '%s' -c 'import time\nprint(\"loading\", flush=True)\nwhile True:\n    time.sleep(0.05)\n'\n" % sys.executable).encode()
    code = (stubborn if kind == "stubborn" else serve).replace("\\n", "\n")
    if not POSIX:
        return code.encode()
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
        self.d = os.path.join(self.tmp, "home", "ai")   # = aisetup.work_dir() with the NUC_CONSOLE_HOME below: the engine's folder is the catalog's and web.json's
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
        if not POSIX and argv and argv[0] == aisetup.runtime_path(self.d, self.runtime):
            argv = [sys.executable] + list(argv)  # the fake runtime.exe is Python source (runtime_script): this interpreter runs it
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
        if not self.eng.wait(30):
            self.fail("the job did not end: %s" % self.where())
        return self.eng.snapshot()["job"]

    def where(self):
        """What a job that does not end is doing: its phase, the server's last lines (the fake runtime adds where it is, on SIGUSR1) and what
        its endpoint answers."""
        info = self.eng.child_info or {}
        pid = info.get("pid")
        if POSIX and pid and alive(pid):
            try:
                os.kill(pid, signal.SIGUSR1)
            except OSError:
                pass
            threading.Event().wait(1)
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
            self.skipTest("alive() is POSIX's: on Windows os.kill(pid, 0) sends a Ctrl+C")
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
        with mock.patch.object(advisor, "ask", side_effect=RuntimeError("secret detail")), contextlib.redirect_stderr(io.StringIO()):
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
        self.assertRegex(body, r'<input class="q" type="text" name="q" maxlength="500"[^>]* disabled>')   # the box wakes up when the AI is on
        self.assertIn("turn AI on to ask", body)
        self.assertIn("models are downloaded to", body)
        self.assertIn(html.escape(self.d), body)
        self.assertIn("nothing downloaded", body)
        self.assertNotIn("<script", body.lower())

    def test_one_click_on_a_model_sets_it_up_and_the_page_shows_it_working_and_then_on(self):
        reached, release = threading.Event(), threading.Event()
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.rt_bytes) else None
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
        self.assertEqual(self.requests(), ["/" + RUNTIME_NAME, "/tiny.gguf"])
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
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.rt_bytes) else None
        self.go("use", {"model": "tiny"})
        self.assertTrue(reached.wait(30))
        self.go("cancel")
        release.set()
        self.assertEqual(self.job()["state"], "cancelled")
        self.assertIn("cancelled", self.page())
        self.assertFalse(self.installed("tiny"))

    def test_a_failure_is_shown_in_red_with_its_reason(self):
        self.httpd.files.pop("/tiny.gguf")
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
        with mock.patch.object(web.hmac, "compare_digest", wraps=web.hmac.compare_digest) as cmp:
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
        too_big = urlencode({"csrf": self.srv.csrf, "model": "tiny", "pad": "x" * web.POST_MAX})
        self.assertRefused(self.post("use", body=too_big), 413)
        just = urlencode({"csrf": self.srv.csrf, "pad": "x"})
        just += "&p=" + "y" * (web.POST_MAX - len(just) - 3)
        self.assertEqual(len(just), web.POST_MAX)
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
        self.assertEqual((h["Content-Security-Policy"], h["Referrer-Policy"]), (web.page_csp(forms=True), "same-origin"))
        st, h, _b = self.post("off")
        self.assertEqual((st, h["Referrer-Policy"], h["Cache-Control"]), (303, "same-origin", "no-store"))
        self.assertIn("form-action 'none'", h["Content-Security-Policy"], "a redirect has no form: the strict one")
        self.assertEqual(self.get("/?view=health")[1]["Content-Security-Policy"], web.CSP, "every other page keeps form-action 'none'")
        self.assertEqual(self.get("/")[1]["Content-Security-Policy"], web.CSP)
        self.assertEqual(self.get("/")[1]["Referrer-Policy"], "no-referrer")
        self.assertIn("form-action 'none'", web.CSP)

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
            total = len(self.rt_bytes) + len(self.MODEL_BYTES["tiny"])
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
        self.assertEqual(self.requests(), ["/" + RUNTIME_NAME, "/tiny.gguf"])

    def test_the_screen_shows_the_progress_and_c_cancels(self):
        reached, release = threading.Event(), threading.Event()
        self.eng.progress_hook = lambda job: (reached.set(), release.wait(30)) if job["step"] == "tiny" and job["done"] > len(self.rt_bytes) else None
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
        self.httpd.files.pop("/tiny.gguf")
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
