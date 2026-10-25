"""The alarms about the peers (problems.peer_state / peer_rows, ids peer-down, fleet-poller-down, peer-insecure, peer-identity): thresholds
with an injected clock, alert=yes against no, a poller that stopped, accepting by name, hostile cache entries, and where they are NOT seen
(the root collector's problems_raw and so its history). The cache is the one the poller writes (tests/fleetfix.py); nothing else is read."""
import os
import shutil
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import cards  # noqa: E402
import fleetfix  # noqa: E402
import notify  # noqa: E402
import nuc_config  # noqa: E402
import peers  # noqa: E402
import problems  # noqa: E402

NOW = 1_800_000_000
PIDS = ("peer-down", "fleet-poller-down", "peer-insecure", "peer-identity")


def down(name, fails=3, since=100, state="unreachable", **kw):
    """A cache entry of a peer that has failed `fails` polls in a row, the last good answer `since` seconds before NOW."""
    return fleetfix.entry(name, NOW, state=state, age=since, fails=fails, error="connect", **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nuc-peer-alarms-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def hosts(self, *names, **kw):
        return [fleetfix.peer(n, **kw) for n in names]

    def state(self, hosts, entries, poller=True, now=NOW):
        fleetfix.write(self.dir, entries, NOW, poller=poller)
        return problems.peer_state(now=now, hosts=hosts, folder=self.dir)

    def rows(self, hosts, entries, **kw):
        ps = self.state(hosts, entries, **kw)
        return ps, problems.peer_rows(ps)

    def ids(self, rows):
        return [r[2] for r in rows]


class Down(Base):
    def test_two_rounds_are_no_alarm_three_with_a_minute_are(self):
        h = self.hosts("nas", alert=True)
        self.assertEqual(self.rows(h, [down("nas", fails=2, since=500)])[1], [])
        self.assertEqual(self.rows(h, [down("nas", fails=3, since=59)])[1], [])  # three rounds but not a minute yet
        ps, rows = self.rows(h, [down("nas", fails=3, since=60)])
        self.assertEqual(rows, [(2, "host not answering: nas (unreachable)", "peer-down")])
        self.assertEqual(ps["down"], [("nas", "unreachable")])

    def test_recovery_clears_it(self):
        h = self.hosts("nas", alert=True)
        self.assertEqual(self.ids(self.rows(h, [down("nas", fails=9, since=900)])[1]), ["peer-down"])
        self.assertEqual(self.rows(h, [fleetfix.entry("nas", NOW, fails=0)])[1], [])

    def test_alert_no_is_never_down(self):
        h = self.hosts("nas")  # alert defaults to no
        self.assertEqual(self.rows(h, [down("nas", fails=50, since=5000)])[1], [])

    def test_a_peer_never_reached_is_down_after_three_rounds(self):
        h = self.hosts("nas", alert=True)
        e = down("nas", fails=3)
        e["last_ok"] = None
        self.assertEqual(self.ids(self.rows(h, [e])[1]), ["peer-down"])
        e["fails"] = 2
        self.assertEqual(self.rows(h, [e])[1], [])

    def test_every_failed_state_counts_and_a_good_one_never_does(self):
        h = self.hosts("nas", alert=True)
        for st in ("unreachable", "unauthorized", "untrusted", "incompatible"):
            rows = self.rows(h, [down("nas", state=st)])[1]
            lead = "host not answering" if st == "unreachable" else "peer not readable"  # silent, or answering but refused (token, identity, version)
            self.assertEqual(rows, [(2, "%s: nas (%s)" % (lead, st), "peer-down")], st)
        for st in ("ok", "warn", "err", "down", "stale", "unknown"):  # a peer that answers, even with bad news, is not down
            self.assertEqual(self.rows(h, [fleetfix.entry("nas", NOW, state=st, fails=7)])[1], [], st)

    def test_not_polled_yet_says_nothing(self):
        self.assertEqual(self.rows(self.hosts("nas", alert=True), [fleetfix.entry("other", NOW)])[1], [])

    def test_three_names_and_the_rest_counted(self):
        names = ("a1", "b2", "c3", "d4", "e5")
        h = self.hosts(*names, alert=True)
        rows = self.rows(h, [down(n) for n in names])[1]
        self.assertEqual(rows, [(2, "host not answering: a1 (unreachable), b2 (unreachable), c3 (unreachable) … +2", "peer-down")])
        mixed = self.rows(self.hosts("a1", "b2", alert=True), [down("a1"), down("b2", state="untrusted")])[1]
        self.assertEqual(mixed, [(2, "host not answering or not readable: a1 (unreachable), b2 (untrusted)", "peer-down")])

    def test_the_sixty_seconds_apply_to_every_failed_state(self):
        h = self.hosts("nas", alert=True)
        for st in ("unauthorized", "untrusted", "incompatible", "unreachable"):
            self.assertEqual(self.rows(h, [down("nas", state=st, since=30)])[1], [], st)

    def test_a_last_ok_in_the_future_is_not_known_so_three_rounds_decide(self):
        h = self.hosts("nas", alert=True)
        e = down("nas", since=0)
        e["last_ok"] = NOW + 3600  # the poller host's clock stepped back
        self.assertEqual(self.ids(self.rows(h, [e])[1]), ["peer-down"])
        e["fails"] = 2
        self.assertEqual(self.rows(h, [e])[1], [])
        e["fails"], e["last_ok"] = 3, NOW + 30  # within the tolerance: a young answer still holds the alarm back
        self.assertEqual(self.rows(h, [e])[1], [])

    def test_a_stopped_poller_is_a_note_not_peer_down(self):
        h = self.hosts("nas", alert=True)
        for stale_ts in (NOW - 600, None):  # fleet.json old, and none at all
            shutil.rmtree(self.dir)
            os.makedirs(self.dir)
            fleetfix.write(self.dir, [down("nas")], NOW - 600 if stale_ts else NOW, poller=stale_ts is not None)
            ps = problems.peer_state(now=NOW, hosts=h, folder=self.dir)
            self.assertEqual(ps["poller"], "stale" if stale_ts else "absent")
            rows = problems.peer_rows(ps)
            self.assertEqual(self.ids(rows), ["fleet-poller-down"])
            self.assertEqual(rows[0][0], 1)

    def test_a_stopped_poller_with_nobody_watched_is_quiet(self):
        self.assertEqual(self.rows(self.hosts("nas"), [down("nas")], poller=False)[1], [])

    def test_a_poller_stamped_in_the_future_is_not_running(self):
        fleetfix.write(self.dir, [down("nas")], NOW + 3600)
        self.assertEqual(problems.peer_state(now=NOW, hosts=self.hosts("nas", alert=True), folder=self.dir)["poller"], "stale")

    def test_no_hosts_no_state(self):
        self.assertIsNone(problems.peer_state(now=NOW, hosts=[], folder=self.dir))
        self.assertEqual(problems.peer_rows(None), [])


class Insecure(Base):
    def test_only_a_configured_insecure_peer_reached_in_clear(self):
        h = [fleetfix.peer("lan", insecure=True), fleetfix.peer("ts"), fleetfix.peer("cfg-no", insecure=False)]
        entries = [fleetfix.entry("lan", NOW, insecure=True), fleetfix.entry("ts", NOW, insecure=True), fleetfix.entry("cfg-no", NOW, insecure=True)]
        ps, rows = self.rows(h, entries)
        self.assertEqual(rows, [(1, "token sent in clear over http: lan", "peer-insecure")])  # independent of alert=yes; the others have no insecure=yes
        self.assertEqual(self.rows([fleetfix.peer("lan", insecure=True)], [fleetfix.entry("lan", NOW, insecure=False)])[1], [])


class Identity(Base):
    def test_a_changed_host_id_is_its_own_alarm_and_not_peer_down(self):
        h = self.hosts("nas", alert=True)
        e = down("nas", state="untrusted", identity="changed")
        rows = self.rows(h, [e])[1]
        self.assertEqual(rows, [(2, "not the host remembered: nas", "peer-identity")])


class Hostile(Base):
    def test_foreign_values_never_become_text_or_alarms(self):
        h = self.hosts("nas", alert=True)
        for bad in ("3", True, -1, 3.5, None, [3], {"x": 1}):
            ps = problems.peer_state(now=NOW, hosts=h, folder=self.dir, cache={"fleet": {"ts": NOW}, "peers": {"nas": {"state": "unreachable", "fails": bad, "last_ok": None}}})
            self.assertEqual(ps["down"], [], repr(bad))
        for bad in ("x", True, [1]):  # a last_ok that is no number cannot hold the alarm back or push it out: it counts as 'never'
            ps = problems.peer_state(now=NOW, hosts=h, folder=self.dir, cache={"fleet": {"ts": NOW}, "peers": {"nas": {"state": "unreachable", "fails": 3, "last_ok": bad}}})
            self.assertEqual(ps["down"], [("nas", "unreachable")], repr(bad))
        for cache in ({"fleet": {"ts": "now"}, "peers": {"nas": {"state": "unreachable", "fails": 3}}}, {"fleet": "x", "peers": []}, {"peers": {"nas": 3}}, {}, {"fleet": {"ts": NOW}, "peers": None}):
            self.assertIsInstance(problems.peer_state(now=NOW, hosts=h, folder=self.dir, cache=cache), dict)  # never raises

    def test_a_state_word_outside_the_list_is_not_down_and_the_text_uses_fixed_words(self):
        rows = problems.peer_rows({"poller": "running", "watch": 1, "down": [("nas", "<script>hostile text</script>")], "insecure": [], "identity": []})
        self.assertEqual(rows, [(2, "host not answering: nas (unreachable)", "peer-down")])

    def test_names_come_from_the_configuration_only(self):
        h = self.hosts("nas", alert=True)
        ps, rows = self.rows(h, [down("nas"), down("ghost"), down("evil-1")])
        self.assertEqual(ps["down"], [("nas", "unreachable")])
        junk = [{"name": "NAS\nx", "alert": True}, {"name": 3}, "x", {"name": "ok1", "alert": True}]
        self.assertEqual(problems.peer_state(now=NOW, hosts=junk, folder=self.dir, cache={"fleet": {"ts": NOW}, "peers": {}})["watch"], 1)

    def test_a_broken_cache_file_is_skipped(self):
        fleetfix.write(self.dir, [down("nas")], NOW)
        with open(os.path.join(self.dir, "nas.json"), "w") as f:
            f.write("{not json")
        ps = problems.peer_state(now=NOW, hosts=self.hosts("nas", alert=True), folder=self.dir)
        self.assertEqual(ps["down"], [])


class Accepting(Base):
    def fp(self, rows):
        (sev, text, pid), = [r for r in rows if r[2] == "peer-down"]
        return problems.fingerprint(sev, text, pid)

    def test_accepting_one_name_does_not_hide_the_next(self):
        h = self.hosts("nas", "router", alert=True)
        accepted = self.fp(self.rows(h, [down("nas"), fleetfix.entry("router", NOW)])[1])
        self.assertTrue(accepted.startswith("2|names:"))
        again = self.fp(self.rows(h, [down("nas"), fleetfix.entry("router", NOW)])[1])
        self.assertTrue(problems.fp_matches(accepted, again))
        both = self.fp(self.rows(h, [down("nas"), down("router")])[1])
        self.assertFalse(problems.fp_matches(accepted, both))  # router going down later is a new problem
        self.assertTrue(problems.fp_matches(both, again))  # one coming back does not bring the alarm back

    def test_the_layer_that_builds_the_list_honours_accept_and_mute(self):
        h = self.hosts("nas", "router", alert=True)
        fleetfix.write(self.dir, [down("nas"), down("router")], NOW)
        ps = problems.peer_state(now=NOW, hosts=h, folder=self.dir)
        net, cont = {"ts": NOW, "errors": {}}, {"ts": NOW, "containers": []}
        pb = problems.problems(net, cont, now=NOW, peers=ps)
        self.assertIn("peer-down", pb.pids)
        i = pb.pids.index("peer-down")
        self.assertEqual(pb[i][0], 2)
        self.assertEqual(pb.info[i][3], 'sudo nuc-console-accept --problem peer-down --reason "..."' if not nuc_config.PORTABLE else pb.info[i][3])
        acc = {"peer-down": {"fp": self.fp(problems.peer_rows(ps)), "reason": "r"}}
        with mock.patch.object(problems, "load_accepted", return_value=acc):
            self.assertNotIn("peer-down", problems.problems(net, cont, now=NOW, peers=ps).pids)
        with mock.patch.dict(problems.CFG, {"alerts": {"mute": ["peer-down"], "ignored": []}}):
            m = problems.problems(net, cont, now=NOW, peers=ps)
        self.assertNotIn("peer-down", m.pids)
        self.assertEqual(m.muted, 1)

    def test_identity_and_insecure_cannot_be_muted_and_are_acceptable(self):
        for pid in ("peer-insecure", "peer-identity"):
            self.assertNotIn(pid, nuc_config.MUTABLE_ALERTS)
            self.assertNotIn(pid, problems.NOT_ACCEPTABLE)
        for pid in ("peer-down", "fleet-poller-down"):
            self.assertIn(pid, nuc_config.MUTABLE_ALERTS)
        self.assertEqual(problems.muted_ids() & {"peer-insecure", "peer-identity"}, set())


class Layers(Base):
    """Who sees them: the unprivileged layer (problems(), problem_records()) reads the cache; problems_raw (the root collector) does not."""

    def test_the_collector_path_never_sees_them_and_the_screens_do(self):
        import time
        entry = down("nas")
        entry["last_ok"] = int(time.time()) - 300  # the real clock is used by the layer
        fleetfix.write(self.dir, [entry], time.time())
        net, cont = {"ts": NOW, "errors": {}}, {"ts": NOW, "containers": []}
        with mock.patch.dict(problems.CFG, {"hosts": self.hosts("nas", alert=True)}), mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": self.dir}):
            raw = problems.problems_raw(net, cont)
            recs = problems.problem_records(net, cont)
            pb = problems.problems(net, cont)
            safe = problems.safe_problems(net, cont)
        self.assertFalse([r for r in raw if r[2] in PIDS])
        self.assertEqual([r["id"] for r in recs if r["id"] in PIDS], ["peer-down"])
        self.assertIn("peer-down", pb.pids)
        self.assertIn("peer-down", safe.pids)

    def test_the_notifier_announces_it_with_the_title_and_the_names_only_with_full_detail(self):
        import time
        entry = down("nas")
        entry["last_ok"] = int(time.time()) - 300
        fleetfix.write(self.dir, [entry], time.time())
        net, cont = {"ts": NOW, "errors": {}}, {"ts": NOW, "containers": []}
        with mock.patch.dict(problems.CFG, {"hosts": self.hosts("nas", alert=True)}), mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": self.dir}):
            recs = problems.problem_records(net, cont)
        keys, seen = notify.collect(recs)
        self.assertEqual([r["id"] for r in seen.values() if r["id"] in PIDS], ["peer-down"])
        keys = {k: v for k, v in keys.items() if k.startswith("peer-down")}  # (the host's own ufw-unreadable is not the point)
        text = notify.format_message("host", notify.pick(keys, seen), (), "titles", started=sum(keys.values()))
        self.assertIn(problems.BASE_CATALOG["peer-down"][0], text)
        self.assertNotIn("nas", text)
        full = notify.format_message("host", notify.pick(keys, seen), (), "full", started=sum(keys.values()))
        self.assertIn("nas (unreachable)", full)

    def test_without_hosts_nothing_changes(self):
        with mock.patch.dict(problems.CFG, {"hosts": []}):
            self.assertIsNone(problems.peer_state())
            self.assertFalse([r for r in problems.problem_records({"ts": NOW, "errors": {}}, {"ts": NOW, "containers": []}) if r["id"] in PIDS])


class Heartbeat(Base):
    """A REAL Poller on a fake clock, peers failing for good: the poller is running, and the cache says so."""

    def poller(self, hosts, poll=None, **kw):
        self.t, self.m = [float(NOW)], [0.0]
        state = tempfile.mkdtemp(prefix="nuc-peer-alarms-state-")
        self.addCleanup(shutil.rmtree, state, True)
        p = peers.Poller(hosts, self.dir, poll=poll or peers.poll_peer, clock=lambda: self.m[0], wall=lambda: self.t[0], jitter=lambda: 1.0, state=state, workers=1, **kw)
        self.addCleanup(p.pool.shutdown, False)
        return p

    def tick(self, p):
        self.t[0] += 1
        self.m[0] += 1
        p.step()
        for f in list(p.running.values()):
            f.exception()  # the poll is fast: wait for it, the next step harvests

    def test_all_peers_dead_for_a_quarter_hour_peer_down_stays_and_the_poller_never_looks_dead(self):
        h = self.hosts("nas", alert=True)  # the token file does not exist: every round fails at once, no network
        fleetfix.write(self.dir, [fleetfix.entry("nas", NOW, age=0)], NOW)  # it was fine at the start
        p = self.poller(h)
        seen = set()
        for sec in range(1, 1000):
            self.tick(p)
            ps = problems.peer_state(now=self.t[0], hosts=h, folder=self.dir)
            names = {r[2] for r in problems.peer_rows(ps)}
            self.assertEqual(ps["poller"], "running", sec)
            self.assertNotIn("fleet-poller-down", names, sec)
            if sec < 60:
                self.assertNotIn("peer-down", names, sec)  # the 60 s are counted from the last good answer, in every failed state
            if sec > 80:
                self.assertIn("peer-down", names, sec)
            seen |= names
        self.assertEqual(seen, {"peer-down"})

    def test_a_stopped_poller_turns_into_the_note_after_two_minutes(self):
        h = self.hosts("nas", alert=True)
        fleetfix.write(self.dir, [fleetfix.entry("nas", NOW, age=0)], NOW)
        p = self.poller(h)
        for _ in range(200):
            self.tick(p)
        last = self.t[0]
        ps = problems.peer_state(now=last + 100, hosts=h, folder=self.dir)
        self.assertEqual((ps["poller"], self.ids(problems.peer_rows(ps))), ("running", ["peer-down"]))
        ps = problems.peer_state(now=last + 130, hosts=h, folder=self.dir)  # no more steps: the heartbeat stops
        self.assertEqual((ps["poller"], self.ids(problems.peer_rows(ps))), ("stale", ["fleet-poller-down"]))

    def test_the_heartbeat_is_written_every_fifteen_seconds_and_not_more(self):
        p = self.poller(self.hosts("nas", alert=True))
        fleetfix.write(self.dir, [fleetfix.entry("nas", NOW, age=0)], NOW)
        stamps = set()
        for _ in range(400):
            self.tick(p)
            stamps.add(peers.read_cache(self.dir, now=self.t[0])["fleet"]["ts"])
        self.assertLessEqual(max(b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])), peers.HEARTBEAT_S + 1)

    def test_a_poll_that_raises_carries_the_count_on(self):
        h = self.hosts("nas", alert=True)
        fleetfix.write(self.dir, [down("nas", fails=4)], NOW)

        def boom(*a, **k):
            raise RuntimeError("secret detail")
        p = self.poller(h, poll=boom)
        (e,) = p.run_once()
        self.assertEqual((e["state"], e["error"], e["fails"]), ("unreachable", "internal", 5))
        self.assertEqual(e["last_ok"], int(NOW - 100))
        self.assertEqual(peers.read_cache(self.dir, now=self.t[0])["peers"]["nas"]["fails"], 5)

    def test_last_ok_survives_every_failed_state(self):
        e = peers.poll_peer(fleetfix.peer("x", token="/nonexistent/x.token"), fleetfix.entry("x", NOW, age=500), wall=lambda: NOW)
        self.assertEqual((e["state"], e["last_ok"]), ("unauthorized", NOW - 500))


class Demo(Base):
    def test_demo_screens_never_mix_in_the_real_peers(self):
        import render
        h = self.hosts("nas", alert=True)
        fleetfix.write(self.dir, [down("nas")], NOW)
        self.assertIsNotNone(problems.peer_state(now=NOW, hosts=h, folder=self.dir))
        with mock.patch.object(render, "DEMO", True):
            self.assertIsNone(problems.peer_state(now=NOW, hosts=h, folder=self.dir))
            with mock.patch.dict(problems.CFG, {"hosts": h}), mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": self.dir}):
                self.assertFalse([r for r in problems.problem_records({"ts": NOW, "errors": {}}, {"ts": NOW, "containers": []}) if r["id"] in PIDS])


class Catalog(unittest.TestCase):
    def test_each_id_is_explained_and_belongs_to_no_card(self):
        for pid in PIDS:
            title, why, fix = problems.BASE_CATALOG[pid]
            self.assertTrue(title and why and fix, pid)
            self.assertEqual(cards.PROBLEM_CARDS[pid], (), pid)
        self.assertIn("sudo -u nuc-console-peers nuc-console-fleet forget NAME", problems.BASE_CATALOG["peer-identity"][2])

    def test_the_cache_carries_the_rounds(self):
        e = peers.poll_peer(fleetfix.peer("x", url="https://127.0.0.1:1/", token="/nonexistent/x.token"), None, wall=lambda: NOW)
        self.assertIn(e["state"], peers.FAILED)  # the token file is missing: a failed round, whatever its word
        self.assertEqual(e["fails"], 1)
        e2 = peers.poll_peer(fleetfix.peer("x", url="https://127.0.0.1:1/", token="/nonexistent/x.token"), e, wall=lambda: NOW)
        self.assertEqual(e2["fails"], 2)
        self.assertEqual(peers.valid_entry(e2, "x")["fails"], 2)
        e2["fails"] = "7"
        self.assertEqual(peers.valid_entry(e2, "x")["fails"], 0)


if __name__ == "__main__":
    unittest.main()
