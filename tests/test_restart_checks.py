"""Containers that will not come back after a reboot: container-no-restart (a running container with restart policy `no`) and
containers-not-back (it ran with a policy before the reboot and is not running now). The record behind the second one is
collector.remember_running; a reboot is simulated with the boot time."""
import hermetic  # noqa: F401  (first: keeps the host's state out of the suite)
import json
import os
import tempfile
import unittest
from unittest import mock

import collector
import demo
import problems

NOW = 1_800_000_000


def boot(containers, up=86400, before=None):
    d = {"ts": NOW, "errors": {}, "btime": NOW - up, "containers": containers}
    if before is not None:
        d["containers_before"] = before
    return d


def ct(name, restart="unless-stopped"):
    return {"name": name, "started": NOW - 100, "restart": restart}


def found(b, **cfg):
    with mock.patch.dict(problems.CFG["containers"], cfg):
        return {pid: (sev, text) for sev, text, pid in problems.problems_raw(None, None, NOW, boot=b) if pid in ("container-no-restart", "containers-not-back")}


class NoRestart(unittest.TestCase):
    def test_a_running_container_with_policy_no_is_flagged_with_names_and_count(self):
        got = found(boot([ct("a-1", "no"), ct("b-1", "no"), ct("c-1", ""), ct("d-1", "no"), ct("ok-1")]))
        self.assertEqual(set(got), {"container-no-restart"})
        sev, text = got["container-no-restart"]
        self.assertEqual(sev, 1)
        self.assertIn("4 running containers", text)
        self.assertIn("a-1, b-1, c-1", text)
        self.assertNotIn("d-1", text)  # three names at most, then the count says the rest

    def test_the_ignore_list_takes_names_and_globs(self):
        b = boot([ct("job-1", "no"), ct("shop-test-7", "no"), ct("Dev-Test-2", "no"), ct("real-1", "no")])
        got = found(b, restart_ignore=["job-1", "*-test-*"])
        self.assertIn("1 running container ", got["container-no-restart"][1])
        self.assertIn("real-1", got["container-no-restart"][1])
        self.assertEqual(found(b, restart_ignore=["job-1", "*-test-*", "real-*"]), {})

    def test_policies_that_restart_are_not_flagged_and_unknown_is_not_an_alarm(self):
        self.assertEqual(found(boot([ct("a", "always"), ct("b", "unless-stopped"), ct("c", "on-failure")])), {})
        self.assertEqual(found({"ts": NOW, "btime": NOW - 5}), {})  # no docker section: nothing to say
        self.assertEqual(found(boot([ct("a", "no")], up=60), ), {"container-no-restart": found(boot([ct("a", "no")]))["container-no-restart"]})

    def test_config_parses_globs_lower_case(self):
        import nuc_config
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.ini")
            with open(p, "w") as f:
                f.write("[containers]\nrestart_ignore = Job-1, *-TEST-*; x\n")
            self.assertEqual(nuc_config.load(p, overlay=False)["containers"]["restart_ignore"], ["job-1", "*-test-*", "x"])
        self.assertEqual(nuc_config.load(os.path.join(tempfile.gettempdir(), "nuc-absent.ini"), overlay=False)["containers"]["restart_ignore"], [])


class NotBack(unittest.TestCase):
    def test_before_the_reboot_running_and_now_missing_is_flagged_inside_the_window_only(self):
        b = boot([ct("kept")], up=300, before=["kept", "web-1", "db-1", "q-1", "w-1"])
        sev, text = found(b)["containers-not-back"]
        self.assertEqual(sev, 2)
        self.assertIn("4 containers", text)
        self.assertIn("web-1, db-1, q-1", text)
        self.assertEqual(found(dict(b, btime=NOW - 1300)), {})  # past 20 minutes it is just a stopped container
        self.assertEqual(found(dict(b, btime=NOW - 86400)), {})

    def test_it_clears_when_they_run_again(self):
        self.assertEqual(found(boot([ct("web-1"), ct("db-1")], up=300, before=["web-1", "db-1"])), {})

    def test_stopped_by_hand_before_the_reboot_is_not_flagged(self):
        # `hand-1` has policy unless-stopped but was stopped by hand: it was not in the running record, so it is not in `before`
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "running.json")
            collector.remember_running(["web-1"], NOW - 86400, path, )  # last look before the reboot: only web-1 ran
            before = collector.remember_running(["web-1"], NOW - 100, path)  # first look after it
            self.assertEqual(before, ["web-1"])
            self.assertEqual(found(boot([ct("web-1")], up=100, before=before)), {})

    def test_the_record_survives_a_simulated_reboot_and_holds_names_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "running.json")
            self.assertEqual(collector.remember_running(["web-1", "db-1"], NOW - 86400, path), [])  # first ever look: no past
            self.assertEqual(collector.remember_running(["web-1", "db-1"], NOW - 86399, path), [])   # same boot (btime jitter)
            # reboot: Docker has not started the containers yet
            self.assertEqual(collector.remember_running([], NOW - 120, path), ["db-1", "web-1"])
            # still the same boot: web-1 is back, the memory of `before` is kept and `running` is the current set
            self.assertEqual(collector.remember_running(["web-1"], NOW - 119, path), ["db-1", "web-1"])
            with open(path) as f:
                rec = json.load(f)
            self.assertEqual(sorted(rec), ["before", "btime", "running"])
            self.assertEqual(rec["running"], ["web-1"])
            self.assertEqual(rec["btime"], NOW - 120)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o644))
            b = boot([ct("web-1")], up=120, before=collector.remember_running(["web-1"], NOW - 118, path))
            self.assertEqual(list(found(b)), ["containers-not-back"])
            self.assertIn("db-1", found(b)["containers-not-back"][1])

    def test_an_unreadable_record_costs_the_check_not_the_section(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "running.json")
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(collector.remember_running(["a"], NOW, path), [])
            self.assertEqual(collector.remember_running(["a"], NOW, os.path.join(path, "x", "y")), [])  # cannot be written: no error


class Collect(unittest.TestCase):
    def run_boot(self, docker):
        def fake_run(name, *a, **k):
            if name == "docker":
                return docker(a)
            raise collector.Absent(name)
        with mock.patch.object(collector, "run", fake_run), mock.patch.object(collector, "boot_time", lambda: NOW - 50), \
                mock.patch.object(collector, "RUNNING_RECORD", os.path.join(tempfile.mkdtemp(dir=hermetic.SCRATCH), "running.json")):
            return collector.collect_boot()

    def test_no_docker_means_no_section_and_no_error(self):
        b = self.run_boot(lambda a: (_ for _ in ()).throw(collector.Absent("docker")))
        self.assertIn("containers", b["absent"])
        self.assertNotIn("containers", b["errors"])
        self.assertNotIn("containers_before", b)
        self.assertEqual(found(b), {})

    def test_policy_and_before_come_out_of_docker(self):
        def docker(a):
            if a[:2] == ("ps", "-q"):
                return 0, "id1\nid2\n", ""
            if a[:1] == ("inspect",):
                return 0, "/web-1|2026-10-20T10:00:00Z|unless-stopped\n/tmp-1|2026-10-20T10:00:00Z|\n", ""
            return 1, "", "no"
        b = self.run_boot(docker)
        self.assertEqual([(c["name"], c["restart"]) for c in b["containers"]], [("web-1", "unless-stopped"), ("tmp-1", "no")])
        self.assertEqual(b["containers_before"], [])
        self.assertEqual(list(found(dict(b, ts=NOW))), ["container-no-restart"])


class Demo(unittest.TestCase):
    def test_every_demo_os_has_the_no_restart_container_and_nothing_about_a_reboot(self):
        for os_name in (None, "windows", "darwin"):
            b = demo.snapshot(NOW, os_name)[2] if os_name else demo.snapshot(NOW)[2]
            got = {pid: t for _s, t, pid in problems.problems_raw(None, None, NOW, boot=b) if pid.startswith("container")}
            self.assertEqual(list(got), ["container-no-restart"], os_name)
            self.assertIn("cache-1", got["container-no-restart"])


if __name__ == "__main__":
    unittest.main()
