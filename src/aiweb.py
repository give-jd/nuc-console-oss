"""What the AI page of the web view and the AI screen of the console do besides showing: one engine per process, shared by both.

  use a model  the one action the user needs: download the model server if it is missing (Ollama, the build of this system: aisetup.download():
              HTTPS only, the pinned size and SHA-256, resume, never fetched twice; then unpacked), start it, pull the model through it if it is
              missing (with progress; Ollama checks every layer), load it, make it the advisor's model and turn the advisor on. "Turn AI on" is the
              same with the model chosen before (or the recommended one, which the page and the screen ask about first); "turn AI off" stops the
              server and turns the advisor off
  delete      one model, or everything (the server and every model); cancel stops a download, a pull or a start (what was fetched is kept)
  server      the model server is a CHILD of this process (aisetup.serve_argv and serve_env: 127.0.0.1 only, low priority, the models in the AI
              folder, the GPU unless [ai] gpu = no); it serves every installed model, keeps the one in use loaded, ends with the process, and there
              is only ever one
  chat        a question (advisor.ask, with the machine's state now: the program that owns the process binds it) or "advice now"
              (advisor.advise), answered in the background; the last few are kept in memory until the chat is cleared
  switch      on/off, the model and the endpoint of the server started here: web.json in the AI folder (advisor.effective_cfg lays it
              over config.ini, which these programs never write)
  usage       what the model uses now (the AI page's MODEL USAGE): the CPU and memory of the model server's processes, the models it has
              loaded and where (Ollama's /api/ps: in the GPU's memory or in RAM), how busy the GPU is (aihw.gpu_load). Measured in a thread of
              its own every USAGE_S, only while the AI is on and a page has asked for the snapshot within USAGE_IDLE_S

One job at a time (use, download, delete, start), plus one answer being written; both run in threads of their own, so a request or a key never
waits for them. The state is in memory (snapshot() is what the pages read) and the AI folder is the one the commands use
(aisetup.work_dir()): a lock file there keeps the web view and the console from fetching or deleting at once. `[ai] web_actions = no`
refuses everything. --demo: the same flow, simulated, no file is written and nothing is downloaded or started.
Standard library only, Python 3.8+.
"""
import atexit
import collections
import http.client
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import advisor
import aihw
import aiollama
import aisetup
import nuc_config

HISTORY_MAX = 10          # questions and advice kept (memory only)
NOTICE_S = 120            # a notice (the result of the last action) stays on the page this long
LOCK_FILE = "job.lock"    # in the AI folder: a download or a delete is running, in this process or in the other one (web view / console)
LOCK_STALE_S = 90         # a lock nobody has touched this long is a dead process's; a live job touches it every HEARTBEAT_S
HEARTBEAT_S = 10
START_WAIT_S = 2.0        # a server that is still there this long after it was started has not failed at once
ANSWER_WAIT_S = 60.0      # ... and answers within this long (Windows may scan a new executable first)
LOAD_WAIT_S = 900.0       # then it loads the model: the job waits this long (a 19 GB model on a slow disk takes minutes)
POLL_S = 1.0              # ... looking every second
STOP_GRACE_S = 10         # asked to end (SIGTERM), then killed
TAIL_LINES = 12           # the last lines the server wrote, kept to say why it stopped
DEMO_STEPS, DEMO_STEP_S = 16, 0.15   # a simulated download: this many steps of this many seconds, per file
DEMO_FREE = 412 * 10 ** 9
USAGE_S = 3.0             # the model's usage is measured this often ...
USAGE_IDLE_S = 30.0       # ... while a page has asked for the snapshot within this long (nobody looking: nothing is measured)
SERVER_NAMES = ("ollama", "ollama.exe", "ollama_llama_server", "ollama_llama_se", "ollama_llama_server.exe")  # a server this process did not start

VERB = {"use": "setting up", "download": "downloading", "delete": "deleting", "delete-all": "deleting everything", "start": "starting the server"}
DEMO_RUNTIME = {"linux": "linux-amd64", "windows": "windows-amd64", "darwin": "darwin"}  # the build each demo machine would download
LOCKED = "locked by config.ini ([ai] web_actions = no): the page and the screen only show"
ASKING = "asking the model: the answer appears below (a small model on a slow CPU may need a minute)"


class Engine(object):
    """The work of one process. Every public action returns (ok, text) and leaves the same text as the notice the pages show; none of them
    blocks (a job or an answer runs in its own thread). Tests give the folder, the catalog and the hooks; the demo gives demo=True."""

    def __init__(self, demo=False, directory=None, runtime=None, models=None, allow_loopback_http=False, cfg_fn=None, hw_fn=None,
                 report_fn=None, history_fn=None, popen=subprocess.Popen, state_fn=None, probe=None):
        self.demo, self._dir, self.allow_loopback_http, self.popen = demo, directory, allow_loopback_http, popen
        self.runtime = runtime if runtime is not None else aisetup.RUNTIME
        self.models = models if models is not None else aisetup.MODELS
        self.cfg_fn, self.hw_fn, self.report_fn, self.history_fn = cfg_fn or default_cfg, hw_fn or aisetup._hardware, report_fn or default_report, \
            history_fn or advisor._open_history
        self.state_fn = state_fn or default_state
        self.probe = probe or UsageProbe()     # what the usage is read with (tests: a fake)
        self.usage, self._usage_seen, self._usage_thread, self._usage_stop = None, 0.0, None, threading.Event()
        self.usage_s = USAGE_S
        self.start_wait, self.stop_grace, self.demo_step = START_WAIT_S, STOP_GRACE_S, DEMO_STEP_S  # tests shorten them
        self.load_wait, self.answer_wait, self.poll_s = LOAD_WAIT_S, ANSWER_WAIT_S, POLL_S
        self.lock = threading.RLock()
        self.version = 0                       # +1 at every change a page may show: the catalog is read again
        self.job = self.notice = self.pending = None
        self.history = collections.deque(maxlen=HISTORY_MAX)
        self.progress_hook = None              # tests: called (with the job) at every chunk of a download
        self._cancel, self._job_thread, self._chat_thread = threading.Event(), None, None
        self.child = self.child_info = self.last_exit = None
        self.stop_lock = threading.Lock()
        self._lock_path, self._beat, self._atexit, self._windows_job = None, None, False, None
        self._demo_os, self.demo_state, self._demo_booted = None, {}, False

    # ---------------------------------------------------------------------------------------------------------------- places and settings
    @property
    def directory(self):
        return self._dir or aisetup.work_dir()

    def web_path(self):
        return os.path.join(self.directory, advisor.WEB_FILE)

    def cfg(self):
        return self.cfg_fn()

    def ecfg(self):
        """The advisor's config as the page and the advisor see it: config.ini with web.json laid over it (the demo's: with its own, in memory)."""
        if self.demo:
            cfg = self.cfg()
            ai, web = dict(cfg.get("ai") or {}), self._dstate().get("web") or {}
            if web.get("enabled"):
                ai["enabled"] = True
            ai.update({k: web[k] for k in ("model", "endpoint") if web.get(k)})
            return dict(cfg, ai=ai)
        return advisor.effective_cfg(self.cfg(), self.web_path())

    def locked(self):
        return not advisor.web_actions_on(self.cfg())

    def _result(self, ok, text):
        with self.lock:
            self.notice = {"ok": bool(ok), "text": text, "at": time.time()}
            self.version += 1
        return bool(ok), text

    def _refuse(self):
        return self._result(False, LOCKED) if self.locked() else None

    def _model(self, model_id):
        for m in self.models:
            if m["id"] == model_id:
                return m
        raise aisetup.SetupError("unknown model '%s'" % aisetup.safe(model_id, 40))

    def _web(self, change):
        """Change web.json; (the demo keeps it in memory). Raises aisetup.SetupError with something to say when the folder cannot be written."""
        if self.demo:
            change(self._dstate().setdefault("web", {}))
            return
        try:
            advisor.write_web_state(change, self.web_path())
        except OSError as e:
            raise aisetup.SetupError("cannot write %s (%s): this account may not change it" % (aisetup.safe(self.directory, 120), e.strerror or e))

    # --------------------------------------------------------------------------------------------------------------------- the switch
    def set_enabled(self, on):
        """Turn the advisor on or off for this page and screen (web.json), nothing else. config.ini saying yes cannot be overruled from here."""
        blocked = self._refuse()
        if blocked:
            return blocked
        if not on and (self.cfg().get("ai") or {}).get("enabled"):
            return self._result(False, "the advisor is on by config.ini ([ai] enabled = yes): set it to no there to turn it off")
        try:
            self._web(lambda st: st.update(enabled=bool(on)))
        except aisetup.SetupError as e:
            return self._result(False, str(e))
        return self._result(True, "the advisor is " + ("on" if on else "off"))

    def turn_off(self):
        """AI off: the advisor is turned off and the model server this process started is stopped."""
        ok, text = self.set_enabled(False)
        if not ok:
            return ok, text
        if self.demo:
            self._dstate()["server"] = None
            return self._result(True, "AI is off")
        return self._result(True, "AI is off" + ("; the model server is being stopped" if self._stop_child_async() else ""))

    def turn_on(self, model_id=None):
        """AI on: the model chosen before (the page's, else config.ini's) is set up and used; model_id names another one (the recommended one,
        after the page or the screen asked). Without either there is nothing to turn on: choice() says what to ask."""
        blocked = self._refuse()
        if blocked:
            return blocked
        if model_id is None:
            model_id = self.choice()["model"]
            if model_id is None:
                return self._result(False, "no model is chosen yet: choose one in the list, or let it use the recommended one")
        return self.use_model(model_id)

    def use_model(self, model_id):
        """The one action: this model, set up and running, and the advisor on. What is missing is downloaded (with progress, verified),
        the server is started (another model's is stopped first) and the answer waited for; then web.json says model and on."""
        try:
            m = self._model(model_id)
        except aisetup.SetupError as e:
            return self._result(False, str(e))
        return self._start_job("use", m["id"], lambda job: (self._demo_use if self.demo else self._work_use)(job, m))

    def choice(self):
        """What 'turn AI on' will use: {"model": the chosen one (web.json's, else config.ini's, if the catalog has it) or None, "by": "page" |
        "config" | "", "recommended": the id the hardware advice names or None, "target": the model that would be set up (the chosen one, else
        the recommended one), "size": bytes still to download for it (None: not pinned), "installed": it is there already}."""
        ai = self.ecfg()["ai"]
        chosen = next((m for m in self.models if m["id"] == ai.get("model")), None)
        web = (self._dstate().get("web") if self.demo else advisor.read_web_state(self.web_path())) or {}
        rec = self._recommended()
        target = chosen or next((m for m in self.models if m["id"] == rec), None)
        size, installed = None, False
        if target is not None:
            d = self.directory
            if self.demo:
                st = self._dstate()
                installed = target["id"] in st["installed"] and st["runtime"]
                size = 0 if installed else int(target.get("approx_mb", 0) * 10 ** 6) + (0 if st["runtime"] else self._demo_runtime_size())
            else:
                installed = target in aisetup.installed_models(d, self.models, self.runtime)
                size = 0 if installed else self._missing_bytes(d, target)
        return {"model": chosen["id"] if chosen else None, "by": ("page" if web.get("model") == chosen["id"] else "config") if chosen else "",
                "recommended": rec, "target": target["id"] if target else None, "size": size, "installed": installed}

    def _missing_bytes(self, d, m):
        """Bytes still to download for the server and the model (what is partly there counts for what it has); None when something is not pinned."""
        rt = self.runtime
        if aisetup.missing_pins(rt, False) or aisetup.missing_pins(m, True):
            return None
        total = 0
        if not aisetup.runtime_ready(d, rt):
            a, p = aiollama.runtime_asset(rt), aisetup.archive_path(d, rt)
            if not aisetup.is_verified(d, p, a["sha256"], a["size"]):
                total += max(0, a["size"] - (os.path.getsize(p + ".part") if os.path.exists(p + ".part") else 0))
        if not aisetup.model_ready(d, m):
            total += aisetup.model_bytes(m)
        return total

    def _recommended(self):
        if self.demo:
            import demo
            self._dstate()
            return demo.ai_catalog(self._demo_os)["recommended"]
        return aisetup.recommend_id(self.models, self.hw_fn())

    # ------------------------------------------------------------------------------------------------------------------------ the jobs
    def _start_job(self, kind, model_id, work):
        blocked = self._refuse()
        if blocked:
            return blocked
        with self.lock:
            if self.job and self.job["state"] == "running":
                return self._result(False, "busy: %s. Wait for it, or cancel it" % job_text(self.job, False))
            now = time.time()
            self.job = {"kind": kind, "model": model_id or "", "state": "running", "phase": "starting", "step": "", "done": 0, "total": 0, "rate": 0.0,
                        "error": "", "note": "", "started": now, "ended": None, "mark": (now, 0)}
            self._cancel = threading.Event()
            job = self.job
            self._job_thread = threading.Thread(target=self._run_job, args=(job, work), daemon=True)
            ok = self._result(True, "started: %s" % job_text(job, False))  # before the thread runs: a job that ends at once must have the last word
            self._job_thread.start()
        return ok

    def _run_job(self, job, work):
        state, error = "done", ""
        try:
            work(job)
        except aisetup.Cancelled:
            state, job["note"] = "cancelled", "cancelled: what was fetched is kept, download it again to go on from there"
        except aisetup.SetupError as e:
            state, error = "failed", aisetup.safe(e, 400)
        except PermissionError as e:
            state, error = "failed", "this account may not write to %s (%s)%s" % (aisetup.safe(e.filename or self.directory, 120), e.strerror or "permission denied",
                                                                              self._elsewhere(job))
        except OSError as e:
            state, error = "failed", aisetup.safe(e.strerror or e, 200)
        except Exception as e:  # noqa: BLE001 - a bug must end the job, not the thread's silence
            print("nuc-console ai: job error: %r" % (e,), file=sys.stderr)
            state, error = "failed", "unexpected error: %s (see the service log)" % type(e).__name__
        finally:
            self._unclaim()
            with self.lock:
                job.update(state=state, error=error, ended=time.time(), phase="")
                self.version += 1
        text = job_text(job, False)
        self._result(state != "failed", error and "%s: %s" % (text, error) or (job["note"] or text))

    def _elsewhere(self, job):
        """What to type instead when this account may not do it: the command of the administrator."""
        kind, model = job["kind"], job["model"]
        if kind == "download" and model:
            return ". Run: " + self._cmd("install", model)
        if kind == "delete" and model:
            return ". Run: " + self._cmd("remove", model)
        if kind == "delete-all":
            return ". Run: " + ("nuc-console-ai remove (in an administrator prompt)" if aisetup._is_win() else "sudo nuc-console-ai remove")
        return ""

    @staticmethod
    def _cmd(what, model_id):
        return aisetup.commands_for(model_id)[what]

    def wait(self, timeout=30):
        """Until the job that is running ends (tests, and --once). True when there is none (any more)."""
        t = self._job_thread
        if t is not None:
            t.join(timeout)
        return not (self.job and self.job["state"] == "running")

    def cancel(self):
        """Stop the download or the start that is running: what was fetched stays, the next download resumes it; a server that was
        starting is stopped."""
        blocked = self._refuse()
        if blocked:
            return blocked
        with self.lock:
            if not (self.job and self.job["state"] == "running"):
                return self._result(False, "nothing to cancel")
            if self.job["kind"] not in ("download", "use", "start"):
                return self._result(False, "%s cannot be cancelled: it ends in a moment" % job_text(self.job, False))
            self._cancel.set()
        return self._result(True, "cancelling: what was fetched is kept")

    # claim: the lock file of a download or a delete. It also finds out early that this account may not write the folder.
    def _claim(self, what):
        d = self.directory
        aisetup.ensure_dirs(d)
        path = os.path.join(d, LOCK_FILE)
        for _ in range(3):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - os.stat(path).st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_S:  # its process is gone
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    continue
                try:
                    with open(path, encoding="utf-8") as f:
                        other = aisetup.safe(json.load(f).get("what"), 80)
                except (OSError, ValueError, AttributeError):
                    other = "working"
                raise aisetup.SetupError("another nuc-console process is %s in %s: wait until it is done" % (other, d))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"what": what, "pid": os.getpid(), "at": int(time.time())}, f)
            stop = threading.Event()

            def beat():
                while not stop.wait(HEARTBEAT_S):
                    try:
                        os.utime(path, None)
                    except OSError:
                        return
            self._lock_path, self._beat = path, stop
            threading.Thread(target=beat, daemon=True).start()
            return
        raise aisetup.SetupError("cannot take the lock %s" % path)

    def _unclaim(self):
        path, stop = self._lock_path, self._beat
        self._lock_path = self._beat = None
        if stop is not None:
            stop.set()
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -------------------------------------------------------------------------------------------------------------------------- download
    def download(self, model_id):
        """Fetch the runtime (if it is not there) and the model: pinned, verified, never twice."""
        try:
            m = self._model(model_id)
        except aisetup.SetupError as e:
            return self._result(False, str(e))
        return self._start_job("download", m["id"], lambda job: (self._demo_download if self.demo else self._work_download)(job, m))

    def _progress(self, job, base, total):
        """The callback of aisetup.download(): bytes of this file `done` out of `size`; base: bytes of the files before it; total: of the job."""
        def cb(done, size):
            self._tick(job, base + done, total, "verifying" if done >= size else "downloading")
        return cb

    def _tick(self, job, done, total, phase):
        """Progress: the job's bytes done out of total, its phase, the speed over the last second or more; the lock is touched, the hook called."""
        now = time.time()
        with self.lock:
            job["done"], job["total"], job["phase"] = done, total, phase
            t0, d0 = job["mark"]
            if now - t0 >= 1.0:
                job["rate"], job["mark"] = (job["done"] - d0) / (now - t0), (now, job["done"])
        if self._lock_path:
            self._touch_lock()
        hook = self.progress_hook
        if hook:
            hook(job)

    def _touch_lock(self):
        try:
            os.utime(self._lock_path, None)
        except (OSError, TypeError):
            pass

    def _work_download(self, job, m):
        self._check_fit(job, m)
        if aisetup.runtime_ready(self.directory, self.runtime) and aisetup.model_ready(self.directory, m):
            self._note(job, "%s is already installed: nothing to download" % m["id"])
            return
        self._claim("downloading " + m["id"])
        try:
            self._fetch_runtime(job, m)
            api, mine, started = self._server_for_files(job)
            try:
                self._pull(job, m, api)
            finally:
                if started:
                    self._stop_child()  # it was started for the download only
        finally:
            self._unclaim()
        self._note(job, "%s is installed: %s" % (m["id"], aisetup.fmt_size(aiollama.model_bytes(aiollama.models_dir(self.directory), m["id"]))))

    @staticmethod
    def _note(job, text):
        """What the job says when it is done, with its warning (a model that will slow the PC down) after it."""
        job["note"] = text + ("; " + job["warn"] if job.get("warn") else "")

    def _check_fit(self, job, m):
        """Refuse what cannot be downloaded (not pinned) or will not work on this machine; warn about what will slow it down."""
        for name, entry, is_m in (("the server", self.runtime, False), (m["id"], m, True)):
            if aisetup.missing_pins(entry, is_m):
                raise aisetup.SetupError("%s is not pinned in this build (%s): it cannot be downloaded" % (name, ", ".join(aisetup.missing_pins(entry, is_m))))
        a = aisetup.assess_model(m, self.hw_fn())
        if a and a["verdict"] == "no":
            raise aisetup.SetupError("%s will not work on this machine: %s. To install it anyway: %s --force"
                                     % (m["id"], aisetup.safe(a["why"], 160), self._cmd("install", m["id"])))
        if a and a["verdict"] == "slow":
            job["warn"] = "warning: the PC will slow down while %s runs" % m["id"]

    def _fetch_runtime(self, job, m):
        """The server's build, if it is not unpacked yet: its archive (pinned, verified, never fetched twice), then unpacked. The disk check
        counts the model too. -> False when it was there already."""
        d, rt = self.directory, self.runtime
        need = (aisetup.runtime_bytes(d, rt) or 0) + (0 if aisetup.model_ready(d, m) else aisetup.model_bytes(m))
        free = aisetup.free_bytes(d)
        if need and free < need + aisetup.DISK_MARGIN:
            raise aisetup.SetupError("not enough free disk space in %s: %s needed, %s free" % (d, aisetup.fmt_size(need + aisetup.DISK_MARGIN), aisetup.fmt_size(free)))
        if aisetup.runtime_ready(d, rt):
            return False
        a, arch = aiollama.runtime_asset(rt), aisetup.archive_path(d, rt)
        if not aisetup.is_verified(d, arch, a["sha256"], a["size"]):  # by the stamp: a verified archive is never fetched again
            later = 0 if aisetup.model_ready(d, m) else aisetup.model_bytes(m)
            with self.lock:
                job["total"], job["step"], job["phase"] = a["size"] + later, "runtime", "downloading"
            aisetup.download(a["url"], arch, a["sha256"], a["size"], allow_loopback_http=self.allow_loopback_http,
                             progress=self._progress(job, 0, a["size"] + later), cancel=self._cancel.is_set)
            aisetup.record(d, arch, a["sha256"])
            with self.lock:
                job["done"] = a["size"]
        with self.lock:
            job["step"], job["phase"] = "runtime", "unpacking"
        aisetup.unpack_runtime(d, rt, cancel=self._cancel.is_set)
        return True

    def _pull(self, job, m, api):
        """Pull the model through the server if it is not installed (progress: the layers Ollama reports, after what the job fetched before)."""
        d = self.directory
        if aisetup.model_ready(d, m):
            return False
        with self.lock:
            base = job["done"] if job.get("step") == "runtime" else 0
            job["step"], job["phase"] = m["id"], "downloading"
            if not job["total"] or job["total"] < base:
                job["total"] = base + aisetup.model_bytes(m)
        est = aisetup.model_bytes(m)

        def cb(done, total, status):  # Ollama's words: "pulling <digest>", then "verifying sha256 digest", "writing manifest", "success"
            self._tick(job, base + done, base + max(total, est), "verifying" if status.startswith(("verifying", "writing", "success")) else "downloading")
        aisetup.install_model(api, d, m, cb, self._cancel.is_set, insecure=self.allow_loopback_http)
        with self.lock:
            job["done"] = job["total"]
        return True

    # ---------------------------------------------------------------------------------------------------------------------------- delete
    def delete(self, model_id):
        """Delete one model's files (the partial download too). The page asks first; this is the doing."""
        try:
            m = self._model(model_id)
        except aisetup.SetupError as e:
            return self._result(False, str(e))
        return self._start_job("delete", m["id"], lambda job: self._work_delete(job, m))

    def delete_all(self):
        """Delete the server and every model."""
        return self._start_job("delete-all", "", lambda job: self._work_delete(job, None))

    def _work_delete(self, job, m):
        d, runtime = self.directory, self.runtime
        if self.demo:
            return self._demo_delete(job, m)
        mine = self._server_model()
        served = bool(mine and (m is None or mine == m["id"]))
        plan = aisetup.removal_plan(d, m, self.models)
        if not plan:
            job["note"] = "nothing to delete in %s" % d
            return
        if m is None:
            self._stop_child()  # the server runs from these files
        self._claim("deleting")
        if m is not None and self._child_alive():  # the server knows the model: it forgets it and deletes what only it used
            api = aiollama.Api(aiollama.base_of(self.child_info["endpoint"]))
            api.unload(m["id"])  # a loaded model's files cannot be deleted on Windows
            freed, stuck = (plan[0][2], []) if api.delete(m["id"]) else aisetup.remove_files(d, m, runtime)
            if served:
                self._stop_child()  # it answered with that model: with the advisor off nothing asks it any more
        else:
            freed, stuck = aisetup.remove_files(d, m, runtime)
        if m is None:
            shutil.rmtree(os.path.join(d, "home"), ignore_errors=True)  # the server's key and its log
        chosen = self._web_model()
        if (chosen and (m is None or chosen == m["id"])) or served:  # the model the page chose is gone, or the one the server answered with
            def forget(st):
                if chosen and (m is None or chosen == m["id"]):
                    st.pop("model", None)
                if served:
                    st.pop("enabled", None)  # nothing answers with it any more: the advisor is off (config.ini's yes, if any, stays)
            try:
                self._web(forget)
            except aisetup.SetupError:
                pass
        if stuck:
            raise aisetup.SetupError("could not delete %s: %s%s. The files belong to another account (installed with sudo?), or a server uses them%s"
                                     % (", ".join(n for n, _w in stuck[:3]), stuck[0][1], " and more" if len(stuck) > 3 else "", self._elsewhere(job)))
        job["note"] = "deleted %s, %s freed" % (m["id"] if m else "the server and every model", aisetup.fmt_size(freed))

    def _web_model(self):
        return advisor.read_web_state(self.web_path()).get("model")

    # ----------------------------------------------------------------------------------------------------------------------- the server
    def _child_alive(self):
        return self.child is not None and self.child.poll() is None

    def _server_model(self):
        """The model the server started here answers with (loaded), or None (no server, or none loaded)."""
        with self.lock:
            return (self.child_info["model"] or None) if self._child_alive() else None

    def start_server(self, model_id=None):
        """Start the model server as a child of this process: 127.0.0.1 only, low priority. One at a time; it ends with this process."""
        try:
            m = self._model(model_id) if model_id else None
        except aisetup.SetupError as e:
            return self._result(False, str(e))
        return self._start_job("start", m["id"] if m else "", lambda job: (self._demo_start if self.demo else self._work_start)(job, m))

    def _work_start(self, job, wanted):
        d = self.directory
        have = aisetup.installed_models(d, self.models, self.runtime)
        if not have:
            raise aisetup.SetupError("no server and model installed in %s: download a model first" % d)
        if wanted is not None:
            if wanted not in have:
                raise aisetup.SetupError("%s is not installed in %s: download it first" % (wanted["id"], d))
            m = wanted
        else:
            chosen = self.ecfg()["ai"].get("model")
            m = next((x for x in have if x["id"] == chosen), None) or next((x for x in have if x["id"] == aisetup.DEFAULT_MODEL), have[0])
        if self._child_alive():
            raise aisetup.SetupError("the model server started here is already running (%s, pid %d): stop it first"
                                     % (self.child_info["model"] or "no model loaded", self.child.pid))
        try:
            self._serve(job, m)
        except BaseException:
            self._stop_child()
            raise
        self._web(lambda st: st.update(model=m["id"]))

    def _work_use(self, job, m):
        """What the user asked for when they chose a model: it is there, it runs, the advisor asks it."""
        self._check_fit(job, m)
        had_server = self._child_alive()
        try:
            self._claim("setting up " + m["id"])
            try:
                self._fetch_runtime(job, m)
                if not aisetup.model_ready(self.directory, m):
                    api, _mine, _started = self._server_for_files(job, own=True)
                    self._pull(job, m, api)
            finally:
                self._unclaim()
            with self.lock:
                same = self._child_alive() and self.child_info["model"] == m["id"]
            if not same:
                self._serve(job, m)
        except BaseException:
            if not had_server:
                self._stop_child()  # a server started for this and left without a model would only hold memory
            raise
        self._web(lambda st: st.update(enabled=True, model=m["id"]))
        self._note(job, "%s is in use: the advisor is on and asks it" % m["id"])

    def _server_for_files(self, job, own=False):
        """A server to pull or delete through -> (api, mine, started): the one started here; else, unless `own`, the one the other nuc-console
        process started (web.json's endpoint, when an Ollama answers there: the same folder); else one started now (started=True)."""
        if self._child_alive():
            return aiollama.Api(aiollama.base_of(self.child_info["endpoint"])), True, False
        old = advisor.read_web_state(self.web_path()).get("endpoint")
        if old and aisetup.is_loopback_endpoint(old):
            other = aiollama.Api(aiollama.base_of(old))
            if other.version(1.0):
                if own:
                    raise aisetup.SetupError("a model server already answers at %s (started from the other nuc-console process): use it, or stop it there" % old)
                return other, False, False
        info = self._spawn_server(job)
        return aiollama.Api(aiollama.base_of(info["endpoint"])), True, True

    def _serve(self, job, m):
        """`m` loaded by the server started here (started now if there is none), so that the first answer is quick."""
        hw = self.hw_fn()
        a = aisetup.assess_model(m, hw)
        if a and a["verdict"] == "no":
            raise aisetup.SetupError("%s will not work on this machine: %s" % (m["id"], aisetup.safe(a["why"], 160)))
        if not self._child_alive():
            self._server_for_files(job, own=True)
        with self.lock:
            info = self.child_info
            job["phase"] = "loading"
        try:
            aiollama.Api(aiollama.base_of(info["endpoint"])).load(m["id"], cancel=self._cancel.is_set, timeout=self.load_wait)
        except aisetup.Cancelled:
            self._stop_child()
            raise
        except aisetup.SetupError as e:
            if info["gone"].wait(self.poll_s * 2):
                raise aisetup.SetupError("the model server stopped while it loaded the model (exit status %s): %s"
                                         % (info["code"], aisetup.safe(" / ".join(info["tail"]) or "it wrote nothing", 240)))
            raise aisetup.SetupError("%s could not be loaded: %s" % (m["id"], aisetup.safe(e, 300)))
        with self.lock:
            info["model"] = m["id"]
            self.version += 1
        job["note"] = "the model server runs: %s on %s, %s" % (m["id"], info["endpoint"], info["where"])

    def _spawn_server(self, job):
        """Start the server as a child of this process, see that it does not die at once, wait until it answers. -> its info."""
        d, runtime = self.directory, self.runtime
        with self.lock:
            if self._child_alive():
                raise aisetup.SetupError("the model server started here is already running (pid %d): stop it first" % self.child.pid)
            job["phase"] = "starting"
        if not aisetup.runtime_ready(d, runtime):
            raise aisetup.SetupError("the model server is not installed in %s: download a model first" % d)
        old = advisor.read_web_state(self.web_path()).get("endpoint")
        if old and aisetup.probe(old, 1)[0]:
            raise aisetup.SetupError("a model server already answers at %s (started from the other nuc-console process): use it, or stop it there" % old)
        gpu, why = aisetup.gpu_plan(self.cfg()["ai"].get("gpu", "auto"))
        port = aisetup.pick_port(aisetup.port_of(old) if old else None)
        home = os.path.join(d, "home")
        os.makedirs(home, exist_ok=True)
        proc = self._spawn(aisetup.serve_argv(d, runtime), aisetup.serve_env(d, port, aisetup.DEFAULT_CTX, gpu, home), home)
        info = {"pid": proc.pid, "model": "", "port": port, "endpoint": aisetup.endpoint_for(port), "since": time.time(), "tail": collections.deque(maxlen=TAIL_LINES),
                "stopping": False, "gone": threading.Event(), "code": None, "where": aisetup.gpu_text(gpu), "why": why}
        with self.lock:
            self.child, self.child_info, self.last_exit = proc, info, None
        threading.Thread(target=self._watch, args=(proc, info), daemon=True).start()
        if not self._atexit:
            self._atexit = True
            atexit.register(self.shutdown)
        if info["gone"].wait(self.start_wait):  # it ended at once: say why
            raise aisetup.SetupError("the model server stopped at once (exit status %s): %s" % (info["code"], aisetup.safe(" / ".join(info["tail"]) or "it wrote nothing", 240)))
        api, deadline = aiollama.Api(aiollama.base_of(info["endpoint"])), time.monotonic() + self.answer_wait
        while not api.version(1.0):
            if self._cancel.is_set():
                self._stop_child()
                raise aisetup.Cancelled()
            if info["gone"].wait(self.poll_s):
                raise aisetup.SetupError("the model server stopped as it started (exit status %s): %s"
                                         % (info["code"], aisetup.safe(" / ".join(info["tail"]) or "it wrote nothing", 240)))
            if time.monotonic() > deadline:
                self._stop_child()
                raise aisetup.SetupError("the model server did not answer within %d s: %s" % (self.answer_wait, aisetup.safe(" / ".join(info["tail"]) or "it wrote nothing", 240)))
        self._web(lambda st: st.update(endpoint=info["endpoint"]))
        return info

    def _spawn(self, argv, env, home):
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=home)
        if nuc_config.WINDOWS:
            kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) \
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            kw["start_new_session"] = True  # its own process group: stopping it ends the server's runners too
            nice = shutil.which("nice", path=aisetup.UNIX_PATH)
            if nice:
                argv = [nice, "-n", "10"] + list(argv)  # low priority, like `nuc-console-ai serve`
        try:
            proc = self.popen(argv, **kw)
        except OSError as e:
            raise aisetup.SetupError("cannot start the model server: %s" % aisetup.safe(e.strerror or e, 160))
        if nuc_config.WINDOWS:
            self._into_job(proc)
        return proc

    def _into_job(self, proc):
        """Windows: the child goes into a job object that kills it when this process ends, however it ends (a task that is stopped is not
        asked: it is terminated). Best effort: without it stop_server() still ends the child."""
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

            class Basic(ctypes.Structure):
                _fields_ = [("user_time", ctypes.c_int64), ("job_user_time", ctypes.c_int64), ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                            ("max_ws", ctypes.c_size_t), ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                            ("sched", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("basic", Basic), ("io", Counters), ("proc_mem", ctypes.c_size_t), ("job_mem", ctypes.c_size_t), ("peak_proc", ctypes.c_size_t),
                            ("peak_job", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            if self._windows_job is None:
                job = k32.CreateJobObjectW(None, None)
                info = Extended()
                info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9: JobObjectExtendedLimitInformation
                    return
                self._windows_job = job
            k32.AssignProcessToJobObject(self._windows_job, int(proc._handle))
        except Exception:  # noqa: BLE001
            pass

    def _watch(self, proc, info):
        """Reads what the server writes (the last lines are kept) until it ends, then says it ended."""
        try:
            for raw in iter(proc.stdout.readline, b""):
                info["tail"].append(aisetup.safe(raw.decode("utf-8", "replace").strip(), 200))
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdout.close()
            except (OSError, ValueError, AttributeError):
                pass
        code = info["code"] = proc.wait()
        with self.lock:
            if self.child is proc:
                self.child = None
                if not info["stopping"]:
                    self.last_exit = {"model": info["model"], "code": code, "tail": list(info["tail"])[-3:], "at": time.time()}
            self.version += 1
        try:  # the endpoint web.json holds was this server's: nobody answers there now
            self._web(lambda st: st.pop("endpoint", None) if st.get("endpoint") == info["endpoint"] else None)
        except aisetup.SetupError:
            pass
        info["gone"].set()

    def stop_server(self):
        """Stop the server this process started (not one it did not start). Returns at once; the stopping takes a moment."""
        blocked = self._refuse()
        if blocked:
            return blocked
        if self.demo:
            had = self._dstate().get("server")
            self.demo_state["server"] = None
            return self._result(bool(had), "the model server was stopped" if had else "no model server was started here")
        if not self._stop_child_async():
            return self._result(False, "no model server was started here (one that was started by hand or as a service is stopped the way it was started)")
        return self._result(True, "stopping the model server")

    def _stop_child_async(self):
        with self.lock:
            if not self._child_alive():
                return False
        threading.Thread(target=self._stop_child, daemon=True).start()
        return True

    def _stop_child(self):
        """End the child and its group: asked (SIGTERM), then killed after stop_grace seconds; Windows: its tree is ended (the server's runner
        is a process of its own, which terminating the server alone would leave behind)."""
        with self.stop_lock:
            with self.lock:
                proc, info = self.child, self.child_info
            if proc is None or proc.poll() is not None:
                return
            info["stopping"] = True
            try:
                if nuc_config.WINDOWS:
                    tree = subprocess.run([aisetup.tool("taskkill", "win32"), "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL).returncode == 0
                    if not tree:
                        proc.terminate()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError, aisetup.SetupError):
                pass
            try:
                proc.wait(self.stop_grace)
            except subprocess.TimeoutExpired:
                try:
                    if nuc_config.WINDOWS:
                        subprocess.run([aisetup.tool("taskkill", "win32"), "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError, aisetup.SetupError):
                    pass
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            info["gone"].wait(2)
        with self.lock:
            self.version += 1

    def stop_usage(self):
        self._usage_stop.set()

    def shutdown(self):
        """The process is ending: the server it started ends with it, and a download in the way is stopped."""
        self._cancel.set()
        self._usage_stop.set()
        self._stop_child()
        self._unclaim()

    # ------------------------------------------------------------------------------------------------------------------------- the chat
    def ask(self, text):
        """A question about the machine's history, answered by the model in the background (advisor.ask: read-only queries)."""
        blocked = self._refuse()
        if blocked:
            return blocked
        q = advisor.clean_line(text, advisor.MAX_QUESTION + 1)
        if not q:
            return self._result(False, "type a question first")
        if len(q) > advisor.MAX_QUESTION:
            return self._result(False, "the question is too long: %d characters at most" % advisor.MAX_QUESTION)
        return self._chat("ask", q, lambda: self._demo_answer(q) if self.demo else self._ask(q))

    def advise(self, days):
        """Advice on the last 1, 7 or 30 days of the HEALTH findings, written now (not the stored one)."""
        blocked = self._refuse()
        if blocked:
            return blocked
        if days not in advisor.STORE_PERIODS:
            return self._result(False, "advice is for the last %s days" % ", ".join(str(x) for x in advisor.STORE_PERIODS))
        return self._chat("advise", "advice on the last %s" % ("day" if days == 1 else "%d days" % days), lambda: self._demo_advice(days) if self.demo else self._advise(days))

    def _chat(self, kind, label, work):
        with self.lock:
            if self.pending:
                return self._result(False, "busy: an answer is being written; wait for it")
            self.pending = {"kind": kind, "q": label, "started": time.time()}
            self._chat_thread = threading.Thread(target=self._chat_run, args=(kind, label, work), daemon=True)
            ok = self._result(True, ASKING)
            self._chat_thread.start()
        return ok

    def _chat_run(self, kind, label, work):
        res, error = None, ""
        try:
            res = work()
        except advisor.AdvisorError as e:
            error = advisor.clean_line(str(e), 300)
        except Exception as e:  # noqa: BLE001
            print("nuc-console ai: chat error: %r" % (e,), file=sys.stderr)
            error = "unexpected error: %s (see the service log)" % type(e).__name__
        finally:
            with self.lock:
                self.history.append({"kind": kind, "q": label, "res": res, "error": error, "at": time.time()})
                self.pending = None
                if self.notice and self.notice["text"] == ASKING:  # the answer is on the page: "asking the model" is no longer true
                    self.notice = None
                self.version += 1

    def clear_chat(self):
        """Forget the questions and answers kept so far (an answer still being written stays, and shows when it is done)."""
        blocked = self._refuse()
        if blocked:
            return blocked
        with self.lock:
            n = len(self.history)
            self.history.clear()
        return self._result(True, "the chat is cleared" if n else "the chat was already empty")

    def state(self):
        """The machine's state now (advisor.machine_state(), from the program that owns this process: bind(state=...)), or None when it cannot
        be read: a question is then answered from the history alone."""
        try:
            st = self.state_fn()
        except Exception as e:  # noqa: BLE001 - the state is a help: a question without it is still a question
            print("nuc-console ai: machine state error: %r" % (e,), file=sys.stderr)
            return None
        return st if isinstance(st, dict) and st else None

    def wait_chat(self, timeout=30):
        t = self._chat_thread
        if t is not None:
            t.join(timeout)
        return self.pending is None

    def _ask(self, q):
        cfg = self.ecfg()
        ok, why = advisor.available(cfg)
        if not ok:
            raise advisor.Disabled(why)
        state = self.state()
        try:
            conn = self.history_fn()
        except advisor.NoHistory:
            if state is None:
                raise
            conn = None  # no history yet (a new install, the HEALTH screen off): the state now still answers, and the tools say there is none
        try:
            return advisor.ask(q, conn, cfg, state=state)
        finally:
            if conn is not None:
                conn.close()

    def _advise(self, days):
        cfg = self.ecfg()
        ok, why = advisor.available(cfg)
        if not ok:
            raise advisor.Disabled(why)
        report = self.report_fn(days)
        res = dict(advisor.advise(report, cfg, fresh=True))
        res["stored"] = False
        if advisor.store_writable():  # the shared advice the screens show: only where this account may write it
            try:
                res["stored"] = bool(advisor.save_shared(res, report, days))
            except OSError:
                pass
        return res

    # ------------------------------------------------------------------------------------------------------------------------ the pages
    def snapshot(self, usage=False):
        """What a page or a screen shows, as plain data (copies): the job, the server, the chat, the last notice, the switch; with usage, also what
        the model uses now (the page that shows it asks: that keeps the sampler going; the others never start it)."""
        with self.lock:
            job = dict(self.job) if self.job else None
            if job:
                job.pop("mark", None)
                job["pct"] = min(100, job["done"] * 100 // job["total"]) if job["total"] else 0
                job["eta"] = int((job["total"] - job["done"]) / job["rate"]) if job["rate"] > 0 and job["total"] > job["done"] else None
            running = bool(job and job["state"] == "running")
            server = self._server_view()
            chat = {"busy": bool(self.pending), "pending": dict(self.pending) if self.pending else None, "history": list(self.history)}
            notice = dict(self.notice) if self.notice and time.time() - self.notice["at"] < NOTICE_S else None
            ai = self.ecfg()["ai"] if not self.demo else {}
            snap = {"job": job, "server": server, "chat": chat, "notice": notice, "busy": running or chat["busy"] or server["starting"] or server["stopping"],
                    "version": self.version, "locked": self.locked(), "switch": self.switch(), "demo": self.demo,
                    "ai": {"endpoint": ai.get("endpoint") or "", "model": ai.get("model") or ""}}
            if self.demo:
                web = self._dstate().get("web") or {}
                snap["ai"] = {"endpoint": server["endpoint"], "model": web.get("model") or self.demo_state["model"]}
            snap["state"] = state_of(snap)
            snap["usage"] = None if not usage else self._demo_usage(server) if self.demo else self._usage_now(server, snap["switch"])
            return snap

    # ------------------------------------------------------------------------------------------------------------------------ the usage
    def _usage_now(self, server, switch):
        """The last measurement (a copy), and the sampler kept going while the AI is on or its server runs; None when there is nothing to measure."""
        if not (server["running"] or (switch or {}).get("on")):
            self.usage = None
            return None
        self._usage_seen = time.time()
        t = self._usage_thread
        if t is None or not t.is_alive():
            self._usage_stop.clear()
            self._usage_thread = threading.Thread(target=self._usage_loop, daemon=True)
            self._usage_thread.start()
        return dict(self.usage, running=bool(server["running"])) if self.usage else {"running": bool(server["running"]), "measuring": True}

    def _usage_loop(self):
        while not self._usage_stop.is_set() and time.time() - self._usage_seen < USAGE_IDLE_S:
            u = self.measure_usage()
            with self.lock:
                self.usage = u
            self._usage_stop.wait(self.usage_s)
        with self.lock:
            self.usage = None  # nobody looks: what was measured goes stale, and is measured again when a page comes back

    def measure_usage(self):
        """One measurement (usage_of): the server's processes, its models, the GPUs. Never raises: what fails is unknown."""
        with self.lock:
            root = self.child_info["pid"] if self._child_alive() and self.child_info else None
            endpoint = (self.child_info or {}).get("endpoint") if root else ""
        if not endpoint:
            try:
                endpoint = self.ecfg()["ai"].get("endpoint") or ""
            except Exception:  # noqa: BLE001
                endpoint = ""
        p = self.probe
        return usage_of(_try(p.procs, []), root, _try(lambda: p.loaded(endpoint), None), _try(p.gpu, None), p.threads(), _try(p.ram_mb, None))

    def switch(self):
        if self.demo:
            st = self._dstate()
            web = st.get("web") or {}
            on = bool(web["enabled"]) if "enabled" in web else bool(st["enabled"])
            return {"on": on, "by": "web" if on else "", "locked": self.locked()}
        return advisor.web_switch(self.cfg(), self.web_path())

    def _server_view(self):
        starting = bool(self.job and self.job["state"] == "running" and self.job["kind"] == "start")
        if self.demo:
            s = self._dstate().get("server")
            return {"running": bool(s), "starting": starting, "stopping": False, "pid": s["pid"] if s else None, "model": s["model"] if s else "",
                    "endpoint": s["endpoint"] if s else "", "since": s["since"] if s else None, "where": "demo", "exit": None, "tail": []}
        alive = self._child_alive()
        info = self.child_info if alive else None
        return {"running": alive, "starting": starting, "stopping": bool(alive and info["stopping"]), "pid": info["pid"] if info else None,
                "model": info["model"] if info else "", "endpoint": info["endpoint"] if info else "", "since": info["since"] if info else None,
                "where": info["where"] if info else "", "exit": dict(self.last_exit) if self.last_exit and not alive else None,
                "tail": list(info["tail"]) if info else []}

    # ------------------------------------------------------------------------------------------------------------------------------ demo
    def _dstate(self):
        self._demo_boot(self._demo_os)
        return self.demo_state

    def _demo_boot(self, os_name):
        """The demo machine's state at the start, and again when the demo changes machine."""
        if self._demo_os != os_name or not self._demo_booted:
            self._demo_booted = True
            import demo
            st = demo._AI_STATE[os_name if os_name in demo.AI_OSES else "linux"]
            self._demo_os = os_name
            self.demo_state = {"installed": set(st["installed"]), "runtime": bool(st["runtime"]["installed"]), "enabled": st["enabled"],
                               "model": st["active"] or "", "server": None, "web": {}, "start_probe": dict(st["probe"])}
            if st["enabled"] and st["probe"]["state"] == "answering" and st["active"]:  # that machine has its server up, started from this page
                self.demo_state["server"] = {"pid": 4242, "model": st["active"], "endpoint": aisetup.endpoint_for(aisetup.DEFAULT_PORT), "since": time.time() - 3600}
                self.demo_state["web"] = {"enabled": True, "model": st["active"]}

    def _demo_runtime_size(self):
        """What the demo machine's build of the server weighs (the pinned size of that file)."""
        a = (aisetup.RUNTIME.get("assets") or {}).get(DEMO_RUNTIME.get(self._demo_os or "linux", "linux-amd64")) or {}
        return a.get("size") or 0

    def _demo_has(self, model_id):
        return model_id in self._dstate()["installed"]

    def demo_catalog(self, os_name):
        """demo.ai_catalog() as this engine's simulated actions have left it: what is installed, the active model, the runtime, the space."""
        import demo
        self._demo_boot(os_name)
        cat, st = demo.ai_catalog(os_name), self.demo_state
        sizes = {m["id"]: int(m["approx_mb"] * 10 ** 6) for m in cat["models"]}
        for m in cat["models"]:
            m["installed"] = m["id"] in st["installed"]
        cat["runtime"]["installed"] = st["runtime"]
        cat["active"] = (st.get("web") or {}).get("model") or st["model"] or None
        cat["space"] = {"used": sum(sizes[i] for i in st["installed"] if i in sizes) + (self._demo_runtime_size() if st["runtime"] else 0), "free": DEMO_FREE}
        return cat

    def demo_status(self, os_name):
        """demo.ai_status() with the simulated switch, model and server."""
        import demo
        self._demo_boot(os_name)
        out, st = demo.ai_status(os_name), self.demo_state
        web, srv = st.get("web") or {}, st.get("server")
        out["enabled"] = bool(web["enabled"]) if "enabled" in web else bool(out["enabled"])
        out["model"] = web.get("model") or out["model"]
        if srv:
            out.update(endpoint=srv["endpoint"], probe={"state": "answering", "msg": "", "models": [srv["model"]]})
        return out

    def _demo_sleep_step(self):
        if self._cancel.wait(self.demo_step):
            raise aisetup.Cancelled()

    def _demo_fetch(self, job, m):
        """The simulated download of what is missing -> False when it was all there."""
        st = self._dstate()
        todo = [("runtime", self._demo_runtime_size())] * (not st["runtime"]) + [(m["id"], int(m["approx_mb"] * 10 ** 6))] * (m["id"] not in st["installed"])
        if not todo:
            return False
        total, base = sum(s_ for _n, s_ in todo), 0
        with self.lock:
            job["total"], job["phase"] = total, "downloading"
        for name, size in todo:
            with self.lock:
                job["step"] = name
            for i in range(1, DEMO_STEPS + 1):
                self._demo_sleep_step()
                with self.lock:
                    job.update(done=base + size * i // DEMO_STEPS, rate=size / (DEMO_STEPS * max(self.demo_step, 0.01)))
                hook = self.progress_hook
                if hook:
                    hook(job)
            base += size
            if name == "runtime":
                st["runtime"] = True
            else:
                st["installed"].add(name)
        return True

    def _demo_download(self, job, m):
        job["note"] = ("%s is installed (demo: nothing was downloaded)" if self._demo_fetch(job, m) else "%s is already installed: nothing to download") % m["id"]

    def _demo_use(self, job, m):
        st = self._dstate()
        self._demo_fetch(job, m)
        if not (st.get("server") and st["server"]["model"] == m["id"]):
            st["server"] = None
            for phase, steps in (("starting", 1), ("loading", 4)):
                with self.lock:
                    job["phase"] = phase
                for _ in range(steps):
                    self._demo_sleep_step()
            st["server"] = {"pid": 4242, "model": m["id"], "endpoint": aisetup.endpoint_for(aisetup.DEFAULT_PORT), "since": time.time()}
        st.setdefault("web", {}).update(enabled=True, model=m["id"])
        job["note"] = "%s is in use (demo: nothing was downloaded or started)" % m["id"]

    def _demo_delete(self, job, m):
        st = self._dstate()
        self._demo_sleep_step()
        web = st.setdefault("web", {})
        served = bool(st.get("server") and (m is None or st["server"]["model"] == m["id"]))
        if m is None:
            st["installed"].clear()
            st["runtime"] = False
        else:
            st["installed"].discard(m["id"])
        if served:
            st["server"] = None
            web.pop("enabled", None)  # nothing answers any more: the advisor is off, as in the real thing
        if m is None or web.get("model") == m["id"]:
            web.pop("model", None)
        job["note"] = "deleted (demo: no file existed)"

    def _demo_start(self, job, wanted):
        st = self._dstate()
        if st.get("server"):
            raise aisetup.SetupError("the model server started here is already running: stop it first")
        have = [x for x in self.models if x["id"] in st["installed"]]
        if not have or not st["runtime"]:
            raise aisetup.SetupError("no runtime and model installed: download a model first")
        m = wanted or next((x for x in have if x["id"] == (st.get("web") or {}).get("model") or st["model"]), have[0])
        if m["id"] not in st["installed"]:
            raise aisetup.SetupError("%s is not installed: download it first" % m["id"])
        self._demo_sleep_step()
        st["server"] = {"pid": 4242, "model": m["id"], "endpoint": aisetup.endpoint_for(aisetup.DEFAULT_PORT), "since": time.time()}
        st.setdefault("web", {}).update(model=m["id"])
        job["note"] = "the model server runs (demo: nothing was started)"

    def _demo_answer(self, q):
        time.sleep(self.demo_step * 4)
        st = self.state() or {}
        seen = ("It was given this machine's state now: %s, %d problem%s, %d key figures, the busiest processes (%s)."
                % (st.get("status") or "?", len(st.get("problems") or []), "" if len(st.get("problems") or []) == 1 else "s", len(st.get("figures") or []),
                   ", ".join(p.get("name", "?") for p in (st.get("top_cpu") or [])[:3]) or "none read") if st else "No state of the machine could be read.")
        return {"text": "Demo answer. A real answer comes from the local model, built from this machine's state now and read-only queries on its "
                        "history.\n\n%s\n\nYou asked: %s" % (seen, q), "tools_used": ["top_apps"], "model": "demo", "calls": [], "state": bool(st)}

    def _demo_usage(self, server):
        """What the demo machine's model would use: invented, from the demo's catalog (its model and its GPU) and the clock, nothing measured."""
        if not server["running"]:
            return None
        import demo
        cat, now = demo.ai_catalog(self._demo_os), time.time()
        m = next((x for x in cat["models"] if x["id"] == server["model"]), None)
        hw = cat.get("hw") or {}
        gpus = [g for g in hw.get("gpus") or [] if isinstance(g, dict)]
        size = int(m["approx_mb"] * 1.15) if m else 3000
        dedicated = [g for g in gpus if not g.get("unified") and g.get("vram_mb")]
        unified = bool(gpus) and not dedicated
        vram = size if dedicated or unified else 0
        wave = abs(math.sin(now / 9.0))
        threads = (hw.get("cpu") or {}).get("threads") or 8
        cpu = (12.0 + 30.0 * wave) if vram else (180.0 + 300.0 * wave)
        gpu_rows = [{"name": g.get("name") or "GPU", "busy_pct": round(25.0 + 50.0 * wave), "used_mb": float(size + 600),
                     "total_mb": float(g["vram_mb"]) if not g.get("unified") and g.get("vram_mb") else None} for g in gpus[:1]]
        procs = [{"pid": 4242, "ppid": 1, "name": "ollama", "cpu": 0.4, "mem": 90 * 2 ** 20},
                 {"pid": 4243, "ppid": 4242, "name": "ollama", "cpu": cpu, "mem": (size - vram + 600) * 2 ** 20 if not unified else 700 * 2 ** 20}]
        u = usage_of(procs, 4242, [{"name": aiollama_name(m) if m else server["model"], "size_mb": float(size), "vram_mb": float(vram), "ctx": aisetup.DEFAULT_CTX}],
                     {"gpus": gpu_rows, "source": "demo" if gpu_rows else None, "note": "" if gpu_rows else "no GPU whose load can be read"},
                     threads, (hw.get("ram") or {}).get("total_mb"))
        return dict(u, running=True, demo=True)

    def _demo_advice(self, days):
        time.sleep(self.demo_step * 4)
        return {"text": "Demo advice for the last %d day%s. A real one comes from the local model and the HEALTH findings; the one you see here is not." % (days, "" if days == 1 else "s"),
                "model": "demo", "at": int(time.time()), "cites": [], "stored": False}


# ------------------------------------------------------------------------------------------------------------------------------ usage

def _try(fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a measurement that fails is unknown, never an error on the page
        print("nuc-console ai: usage: %r" % (e,), file=sys.stderr)
        return default


def aiollama_name(m):
    """The name Ollama lists a catalog model under (its `ollama` field), for the demo's /api/ps."""
    return str(m.get("ollama") or m.get("id") or "?")


class UsageProbe(object):
    """What the usage is read with: the processes (one procs.ProcSampler kept from one reading to the next, so that the second has a CPU%),
    the models the server has loaded (Ollama's /api/ps, on a loopback http endpoint only), the GPUs (aihw.gpu_load), the logical CPUs and the
    RAM (aihw.cached). Tests replace it."""

    def __init__(self):
        self._ps = None

    def procs(self):
        if self._ps is None:
            import procs
            self._ps = procs.ProcSampler()
        got = self._ps.sample()
        return got.get("procs") or [] if isinstance(got, dict) else []

    @staticmethod
    def loaded(endpoint):
        if not endpoint or not advisor.WEB_ENDPOINT.fullmatch(endpoint):  # http://127.0.0.1|localhost|[::1]:port only: never another machine
            return None
        try:
            status, obj = aiollama.Api(aiollama.base_of(endpoint), timeout=2.0).call("GET", "/api/ps", timeout=2.0)
        except (OSError, aiollama.SetupError, ValueError, http.client.HTTPException):  # nothing answers (yet): unknown, quietly
            return None
        return parse_ps(obj) if status == 200 else None

    @staticmethod
    def gpu():
        return aihw.gpu_load()

    @staticmethod
    def threads():
        return os.cpu_count() or None

    @staticmethod
    def ram_mb():
        return (aihw.cached().get("ram") or {}).get("total_mb")


def parse_ps(obj):
    """Ollama's /api/ps answer -> [{"name", "size_mb", "vram_mb", "ctx"}]: each model loaded now, what it takes in all and in the GPU's memory,
    and its context in tokens (None where the server does not say). None when the answer is not that."""
    if not isinstance(obj, dict) or not isinstance(obj.get("models"), list):
        return None
    out = []
    for m in obj["models"][:8]:
        if not isinstance(m, dict):
            continue
        n = lambda k: m.get(k) if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool) and m.get(k) >= 0 else None  # noqa: E731
        out.append({"name": advisor.clean_line(m.get("name") or m.get("model") or "?", 80), "size_mb": round(n("size") / 2.0 ** 20, 1) if n("size") is not None else None,
                    "vram_mb": round(n("size_vram") / 2.0 ** 20, 1) if n("size_vram") is not None else None, "ctx": n("context_length")})
    return out


def usage_of(procs, root=None, models=None, gpu=None, threads=None, ram_mb=None, at=None):
    """What the model uses, from one reading: procs (procs.ProcSampler's list), root (the pid of the server this process started; None: the
    processes named like a model server, SERVER_NAMES), models (parse_ps; None: unknown), gpu (aihw.gpu_load), threads (logical CPUs) and
    ram_mb (the RAM). -> {"at", "procs": {"n", "cpu_pct" (% of one core), "share_pct" (% of the machine), "mem_mb", "ram_mb", "threads"} or None
    when no process of it is seen, "models", "gpus", "gpu_note"}. A value that cannot be read is None. Pure."""
    rows = [p for p in procs or [] if isinstance(p, dict) and isinstance(p.get("pid"), int) and not isinstance(p.get("pid"), bool)]
    kids = collections.defaultdict(list)
    for p in rows:
        if isinstance(p.get("ppid"), int):
            kids[p["ppid"]].append(p["pid"])
    by = {p["pid"]: p for p in rows}
    todo = [root] if root in by else [p["pid"] for p in rows if str(p.get("name") or "").lower() in SERVER_NAMES]
    mine = set()
    while todo:
        pid = todo.pop()
        if pid not in mine:
            mine.add(pid)
            todo.extend(kids.get(pid, ()))
    got = [by[pid] for pid in mine]
    real = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0  # noqa: E731
    cpus, mems = [p["cpu"] for p in got if real(p.get("cpu"))], [p["mem"] for p in got if real(p.get("mem"))]
    cpu = round(sum(cpus), 1) if cpus else None
    pr = {"n": len(got), "cpu_pct": cpu, "share_pct": round(cpu / threads, 1) if cpu is not None and real(threads) and threads else None,
          "mem_mb": round(sum(mems) / 2.0 ** 20, 1) if mems else None, "ram_mb": ram_mb if real(ram_mb) else None, "threads": threads if real(threads) else None}
    g = gpu if isinstance(gpu, dict) else {}
    return {"at": time.time() if at is None else at, "procs": pr if got else None, "models": models,
            "gpus": [x for x in g.get("gpus") or [] if isinstance(x, dict)] if gpu is not None else None, "gpu_note": str(g.get("note") or "")}


# ------------------------------------------------------------------------------------------------------------------------------ module

_ENGINE, _ENGINE_LOCK = None, threading.Lock()


_BIND = {}


def bind(cfg=None, report=None, state=None):
    """The program that owns this process tells the engine where its settings, the HEALTH report and the machine's state now are: the web view
    (render.CFG, render.health_data, the state its pages show) and the console (its own, as __main__: importing render from here would run it
    a second time)."""
    _BIND.update({k: v for k, v in (("cfg", cfg), ("report", report), ("state", state)) if v is not None})


def default_state():
    """The machine's state now, as bind(state=...) gives it (advisor.machine_state()); None when nobody bound one."""
    return _BIND["state"]() if "state" in _BIND else None


def default_cfg():
    """nuc_config's config as the program that owns this process loaded it (bind()), else read from config.ini."""
    return _BIND["cfg"]() if "cfg" in _BIND else nuc_config.load()


def default_report(days):
    """The HEALTH report of the last `days` days (bind(): the one the screens share, from their cache), else read from the history:
    AdvisorError (NoHistory) when there is none."""
    if "report" in _BIND:
        return _BIND["report"](days)
    import health
    conn = advisor._open_history()
    try:
        return health.report(conn, days=days)
    finally:
        conn.close()


def engine():
    """The engine of this process (made at the first use)."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
        return _ENGINE


def set_engine(eng):
    """Another engine for this process (the demo's, a test's); the old one is not stopped. -> the one that was there, or None."""
    global _ENGINE
    with _ENGINE_LOCK:
        old, _ENGINE = _ENGINE, eng
        return old


def configure(demo=False, os_name=None):
    """The web view's start: its engine is the demo's (simulated, as that machine: linux, windows or darwin) or the real one."""
    eng = Engine(demo=bool(demo))
    eng._demo_os = os_name
    set_engine(eng)
    return eng


def version():
    """The engine's change counter, without making an engine (0 when there is none): the catalog is read again when it moves."""
    return _ENGINE.version if _ENGINE is not None else 0


def shutdown():
    """What a program does as it ends: the model server it started ends too."""
    eng = _ENGINE
    if eng is not None:
        eng.shutdown()


# ------------------------------------------------------------------------------------------------------------------------------ words

def job_text(job, progress=True):
    """One plain line about a job: 'setting up qwen3-8b', with progress=True 'qwen3-8b: downloading the model, 42%, 2.1 GB of 5.0 GB, 12.3 MB/s,
    about 4 min left'. A job that ended says how."""
    if not job:
        return ""
    kind, model, state = job["kind"], job["model"], job["state"]
    what = "%s%s" % (VERB.get(kind, kind), " " + model if model else "")
    if state in ("failed", "cancelled", "done"):
        return "%s %s" % (what, state)
    if not progress or kind not in ("use", "download", "start"):
        return what
    step, phase, total, done = job.get("step"), job.get("phase"), job.get("total") or 0, job.get("done") or 0
    part = "runtime" if step == "runtime" else "model"
    if phase == "verifying":
        body = "checking the SHA-256 of the %s" % part
    elif phase == "unpacking":
        body = "unpacking the runtime"
    elif phase == "starting":
        body = "starting the model server"
    elif phase == "loading":
        body = "loading the model: it answers in a minute or two"
    elif total:
        bits = ["%d%%" % min(100, done * 100 // total), "%s of %s" % (aisetup.fmt_size(done) if done else "0 MB", aisetup.fmt_size(total))]
        if job.get("rate", 0) > 0:
            bits.append("%.1f MB/s" % (job["rate"] / 1e6))
            if total > done:
                bits.append("about %s left" % minutes((total - done) / job["rate"]))
        body = "downloading the %s, %s" % (part, ", ".join(bits))
    else:
        body = "checking what is there"
    return "%s: %s" % (model or "model server", body)


def state_of(snap):
    """('off' | 'working' | 'running' | 'on' | 'error', one plain line): is the AI on, and what is it doing? The pages and the screen put it at the
    top. working: a job runs (download, start); running: the advisor is on and the server this process started is up; on: it is on and asks a
    server that was not started here (config.ini's, yours); error: the server started here ended by itself."""
    job, srv, on = snap["job"], snap["server"], snap["switch"]["on"]
    if job and job["state"] == "running" and job["kind"] in ("use", "download", "start"):
        return "working", job_text(job)
    if on and srv["running"]:
        return "running", "on: %s runs here and answers at %s" % (srv["model"] or "the model server", srv["endpoint"])
    if on and srv.get("exit"):
        e = srv["exit"]
        return "error", "the model server stopped (exit status %s): %s" % (e["code"], " / ".join(e["tail"]) or "it wrote nothing")
    if on:
        return "on", "on: the advisor asks %s%s" % (snap["ai"]["endpoint"], " (%s)" % snap["ai"]["model"] if snap["ai"]["model"] else "")
    return "off", "off" + ("; the model server started here still runs" if srv["running"] else "")


def minutes(seconds):
    """'40 s', '4 min', '2 h 5 min'."""
    s = max(1, int(seconds))
    return "%d s" % s if s < 90 else "%d min" % round(s / 60.0) if s < 5400 else "%d h %d min" % (s // 3600, s % 3600 // 60)
