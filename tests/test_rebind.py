"""Tests for scripts/rebind-db-localhost.py with a fake Docker: no real command is run."""
import copy
import importlib.util
import io
import json
import os
import contextlib
import shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tarfile
import tempfile
import types
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("rebind", os.path.join(HERE, "..", "scripts", "rebind-db-localhost.py"))
rebind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebind)

IMG = {"Id": "sha256:aaa", "Config": {"Cmd": ["postgres"], "Entrypoint": ["docker-entrypoint.sh"], "Healthcheck": None,
                                      "WorkingDir": "", "Labels": {"org.x": "1"}}}
PG = {"Image": "sha256:aaa", "Name": "/pg",
      "Config": {"Image": "postgres:16", "Env": ["POSTGRES_PASSWORD=SECRET", "POSTGRES_USER=u", "PGDATA=/data/x"],
                 "Cmd": ["postgres"], "Entrypoint": ["docker-entrypoint.sh"], "Healthcheck": None, "User": "", "WorkingDir": "",
                 "Labels": {"org.x": "1"}},
      "HostConfig": {"NetworkMode": "bridge", "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                     "PortBindings": {"5432/tcp": [{"HostIp": "", "HostPort": "5432"}]}, "ShmSize": 67108864,
                     "LogConfig": {"Type": "json-file", "Config": {}}, "Binds": ["vol123456789012:/var/lib/postgresql/data"]},
      "Mounts": [{"Type": "volume", "Name": "vol123456789012", "Destination": "/var/lib/postgresql/data", "RW": True}]}


class FakeDocker:
    """Minimal Docker state + command log; `fail` = step at which to simulate an error."""

    def __init__(self, backup_dir, fail=None, after="3 tables", binding="5432/tcp -> 127.0.0.1:5432", inspect_override=None):
        self.calls, self.fail, self.after, self.binding = [], fail, after, binding
        self.containers = {"pg": "running"}
        self.spec = copy.deepcopy(PG)
        if inspect_override:
            inspect_override(self.spec)
        self.probes = 0
        self.backup_dir = backup_dir

    def r(self, out="", rc=0, err=""):
        return types.SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    def __call__(self, *args, check=True, **kw):
        self.calls.append(args)
        a = args
        if self.fail in ("stop", "rename") and a[:2] == ("docker", self.fail):
            raise RuntimeError(f"simulated: {a[1]} failed")
        if a[:2] == ("docker", "inspect") and len(a) == 3:
            return self.r(json.dumps([self.spec]))
        if a[:3] == ("docker", "image", "inspect"):
            return self.r(json.dumps([IMG]))
        if a[:2] == ("docker", "inspect"):  # -f {{.State.Status}} NAME
            return self.r(self.containers.get(a[-1], ""))
        if a[:2] == ("docker", "exec"):
            if "pg_isready" in a:
                return self.r(rc=0)
            self.probes += 1
            return self.r("3 tables" if self.probes == 1 else self.after)  # 1st probe = before, 2nd = after
        if a[:2] == ("docker", "stop"):
            self.containers["pg"] = "exited"
        elif a[:2] == ("docker", "start"):
            self.containers[a[2]] = "running"
        elif a[:2] == ("docker", "rename"):
            self.containers[a[3]] = self.containers.pop(a[2])
        elif a[:2] == ("docker", "rm"):
            if a[2] == "-f":
                self.containers.pop(a[3], None)
            else:
                self.containers.pop(a[2], None)
        elif a[:2] == ("docker", "port"):
            return self.r(self.binding)
        elif a[:2] == ("docker", "run") and "du" in a:
            return self.r("100\t/data")
        elif a[:2] == ("docker", "run") and "--rm" in a:  # backup
            if self.fail == "backup":
                raise RuntimeError("simulated: tar in the container failed")
            tgz = a[-1].split("/backup/")[1].split(" ")[0]
            with tarfile.open(os.path.join(self.backup_dir, tgz), "w:gz") as t:
                for n, data in (("./PG_VERSION", b"16\n"), ("./base/1", b"x" * 400)):
                    ti = tarfile.TarInfo(n)
                    ti.size = len(data)
                    t.addfile(ti, io.BytesIO(data))
        elif a[:2] == ("docker", "run") and "-d" in a:
            if self.fail == "run":
                raise RuntimeError("simulated: docker run failed (port in use)")
            self.containers[a[a.index("--name") + 1]] = "running"
        elif a[0] == "tar":
            with tarfile.open(a[2]) as t:
                return self.r("\n".join(t.getnames()))
        return self.r()


@unittest.skipIf(sys.platform == "win32", "scripts/ are Linux/macOS helpers (POSIX paths and permissions)")
class Rebind(unittest.TestCase):
    def run_main(self, **kw):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = FakeDocker(d, **kw)
        old = rebind.sh
        rebind.sh = fake
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = rebind.main("pg", "5432", "5432", d)
        finally:
            rebind.sh = old
        return rc, fake, d

    def test_success_path(self):
        rc, f, d = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(f.containers, {"pg": "running"})                           # new one running, old one removed
        joined = [" ".join(c) for c in f.calls]
        pos = lambda prefix: next(i for i, x in enumerate(joined) if x.startswith(prefix))
        self.assertLess(pos("docker stop"), pos("docker rename pg pg-old-rebind"))
        self.assertLess(pos("docker rename pg pg-old-rebind"), pos("docker run -d --name pg"))
        self.assertLess(pos("docker run -d --name pg"), pos("docker rm pg-old-rebind"))
        run = next(c for c in f.calls if c[:3] == ("docker", "run", "-d"))
        self.assertIn("127.0.0.1:5432:5432", run)                                   # loopback only
        self.assertNotIn("SECRET", " ".join(" ".join(c) for c in f.calls))           # no password in the commands
        (tgz,) = os.listdir(d)
        self.assertTrue(tgz.startswith("pg-vol123456789-"))

    def test_refuses_when_it_cannot_recreate_faithfully(self):
        def cmd(s): s["Config"]["Cmd"] = ["postgres", "-c", "max_connections=500"]
        def net(s): s["HostConfig"]["NetworkMode"] = "mynet"
        def mem(s): s["HostConfig"]["Memory"] = 2**30
        def bind(s): s["HostConfig"]["Binds"] = ["/srv/data:/var/lib/postgresql/data"]
        for label, fn in (("cmd", cmd), ("network", net), ("limits", mem), ("host bind", bind)):
            rc, f, _ = self.run_main(inspect_override=fn)
            self.assertEqual(rc, 2, label)
            self.assertEqual(f.containers, {"pg": "running"}, label)                 # nothing touched
            self.assertFalse(any(c[1] in ("stop", "rename", "rm") for c in f.calls if c[0] == "docker" and len(c) > 1
                                 and c[1] != "run"), label)

    def test_refuses_relative_backup_dir_and_moved_tag(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rebind.main("pg", "5432", "5432", "relative/dir"), 2)
        rc, f, _ = self.run_main(inspect_override=lambda s: s.update(Image="sha256:bbb"))   # the tag now points to another image
        self.assertEqual(rc, 2)
        self.assertEqual(f.containers, {"pg": "running"})

    def test_every_failure_after_stop_restores_original(self):
        for point in ("backup", "run"):
            rc, f, _ = self.run_main(fail=point)
            self.assertEqual(rc, 1, point)
            self.assertEqual(f.containers, {"pg": "running"}, f"{point}: the original container must be running again")
        rc, f, _ = self.run_main(after="0 tables")                                  # different data after recreation
        self.assertEqual((rc, f.containers), (1, {"pg": "running"}))
        rc, f, _ = self.run_main(binding="5432/tcp -> 0.0.0.0:5432")                # binding not loopback only
        self.assertEqual((rc, f.containers), (1, {"pg": "running"}))
        rc, f, d = self.run_main(fail="run")
        self.assertEqual(len(os.listdir(d)), 1)                                      # the backup stays available


if __name__ == "__main__":
    unittest.main()
