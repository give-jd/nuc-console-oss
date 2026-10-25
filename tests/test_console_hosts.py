"""The console side of the multi-host stack: key 6 (the Hosts list), another host's screens drawn from the poller's cache, the idle return, and what
must never happen (a socket, local data under a remote header, a remote view in the rotation, the key with the feature off).

The cache is a temporary folder written by the poller's own writer (tests/fleetfix.py, peers.write_cache); the documents are the REAL ones a demo
host serves (web.Server.api in tests/golden.FrozenWorld), so that what is drawn here is what a peer would really send. The loop runs on a fake
terminal with an injected clock (tests/test_keymap.py's run_main), no sleep, no host state, no network."""
import copy
import json
import os
import re
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import ansi  # noqa: E402
import fleetfix  # noqa: E402
import golden  # noqa: E402
import hostsview  # noqa: E402
import nuc_config  # noqa: E402
import peers  # noqa: E402
import remotedoc  # noqa: E402
import render  # noqa: E402
import ui  # noqa: E402
import webhosts  # noqa: E402
from test_keymap import Dispatch  # noqa: E402
from test_map_console import LINE, NOW, MapCase  # noqa: E402

VIEWS = ("overview", "map", "cpu", "health", "events")
NAMES = ("alpha", "bravo", "charlie", "delta", "echo", "golf", "hotel", "foxtrot")
UP, DOWN, PGUP, PGDN, HOME, END = b"\x1b[A", b"\x1b[B", b"\x1b[5~", b"\x1b[6~", b"\x1b[H", b"\x1b[F"
SGR = re.compile(r"\x1b\[[0-9;]*m")
LOCAL_TABS = re.compile(r"│ \[?1[ ·]")  # the tab bar of the local header: a remote host's frame never has it
_REAL = {}


def real_docs():
    """{view: the document} a demo host serves, as the poller would have cached it (parsed JSON); the host's name is the peer's, so that a
    line of this machine under a remote header could not pass for it."""
    if not _REAL:
        with golden.FrozenWorld() as world:
            srv = world.server
            for v in VIEWS:
                _REAL[v] = json.loads(bytes(srv.api(v, {})))
    docs = copy.deepcopy(_REAL)
    docs["overview"]["host"] = "alpha-box"
    return docs


def entries(now=NOW):
    """The cache: alpha (ok, all five documents), bravo (warn, all but events), charlie (unreachable, old data, no documents), delta (scope summary),
    echo (not the host that answered first), golf (an old reading), hotel (hostile text); foxtrot is configured and has no entry."""
    docs = real_docs()
    alpha = fleetfix.entry("alpha", now, scope="views", views=docs)
    bravo = fleetfix.entry("bravo", now, "warn", counts=(0, 2, 0), worst=("Disk almost full",), scope="views", views={k: v for k, v in docs.items() if k != "events"})
    charlie = fleetfix.entry("charlie", now, "unreachable", age=300, error="connect", warn=["showing-old-data"])
    delta = fleetfix.entry("delta", now)
    echo = fleetfix.entry("echo", now, identity="changed", error="identity-changed")
    golf = fleetfix.entry("golf", now, age=400, ts=now - 400, scope="views", views={"overview": docs["overview"]})
    hotel = fleetfix.entry("hotel", now, scope="views", views={"overview": hostile(docs["overview"])})
    hotel["summary"]["host"] = "evil\x1b[2J\x9b31mhost"
    return [alpha, bravo, charlie, delta, echo, golf, hotel]


def hostile(doc):
    """The overview with an escape sequence, a C1 control, a bidi override and a huge string in the text of every card."""
    doc = copy.deepcopy(doc)
    for card in doc["cards"]:
        card["title"] = "T\x1b[2J\x9b31m‮EVIL" + "A" * 100000
        card["note"] = "\x1b]0;pwned\x07" + "B" * 100000
    doc["host"] = "h\x1b[31m" + "C" * 100000
    return doc


def kind(lines):
    """'remote' when the frame's first line is a remote header, 'local' when it is the local one (tab bar), else '?'."""
    first = lines[0]
    return "remote" if first.startswith(" ⇄ ") else "local" if LOCAL_TABS.search(first) else "?"


class Fleet(MapCase):
    """A cache folder, [hosts] with the eight peers, and the loop of the console on a fake terminal."""
    run_main = Dispatch.run_main

    def setUp(self):
        super().setUp()
        self.saved_stub = {k: getattr(render, k) for k in ("slides", "events_state", "map_graph")}
        self.saved_portable = nuc_config.PORTABLE
        self.addCleanup(lambda: setattr(nuc_config, "PORTABLE", self.saved_portable))
        self.addCleanup(lambda: [setattr(render, k, v) for k, v in self.saved_stub.items()])
        self.addCleanup(setattr, render, "PAUSED", False)
        self.folder = os.path.join(self.tmp.name, "peers-cache")
        fleetfix.write(self.folder, entries(), NOW)
        env = mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": self.folder})
        env.start()
        self.addCleanup(env.stop)
        cfg = render.CFG
        self.saved_hosts, self.saved_console = cfg["hosts"], dict(cfg["console"])
        self.addCleanup(self.restore_cfg)
        cfg["hosts"] = [fleetfix.peer(n) for n in NAMES]
        cfg["features"]["fleet"] = True
        cfg["console"]["remote_idle_seconds"] = 60
        render.demo_defaults()  # the host's name and the demo's [webapps], once: the first frame of a loop is the same as the next ones

    def restore_cfg(self):
        render.CFG["hosts"] = self.saved_hosts
        render.CFG["console"].clear()
        render.CFG["console"].update(self.saved_console)
        render.CFG["features"].pop("fleet", None)

    def ids(self):
        """[ME] + the peers' ids in the order the list draws them (worst first)."""
        state = render.hosts_state(NOW)
        return [hostsview.ME] + [r["id"] for r in state[2]]

    def to(self, name):
        """The keys that open the host `name` from the list (the cursor starts on this machine)."""
        return [b"j"] * self.ids().index(name) + [b"\r"]

    def once(self, argv, cols=80, rows=24):
        out = render.hosts_once(["render.py", "--once", "--demo"] + list(argv), cols - 1, rows)
        self.assertIsNotNone(out)
        return self.check_frame(out, cols - 1, rows, argv), out


class KeySix(Fleet):
    def test_the_key_is_inert_and_absent_from_the_help_without_the_fleet(self):
        for why in ("no hosts", "feature off"):
            render.CFG["hosts"] = [] if why == "no hosts" else render.CFG["hosts"]
            render.CFG["features"]["fleet"] = why != "feature off"
            self.assertFalse(render.fleet_on(), why)
            frames = self.run_main([b"6", b"?"])
            self.assertEqual([kind(f) for f in frames], ["local"] * 3, why)
            self.assertNotIn("HOSTS", "\n".join(frames[1]))
            help_text = "\n".join(frames[2])
            self.assertIn("Keys", help_text)
            self.assertNotIn("the other machines", help_text, why)
            self.assertNotIn("6: hosts", "\n".join(frames[0]), why)
            self.assertIsNone(render.hosts_once(["render.py", "--once", "--demo", "--view", "hosts"], 79, 24), why)

    def test_the_key_opens_the_list_from_every_screen_and_is_in_the_footer_and_the_help(self):
        frames = self.run_main([b"6", b"\x1b", b"2", b"6", b"\x1b", b"?"])
        self.assertEqual([kind(f) for f in frames], ["local"] * 7)
        self.assertIn("HOSTS", "\n".join(frames[1]))
        self.assertIn("6: hosts", frames[0][-1])
        self.assertIn("the other machines", "\n".join(frames[6]))
        self.assertIn("HOSTS", "\n".join(frames[4]))

    def test_the_table_names_every_key(self):
        self.assertEqual(ui.action("overview", "6", render.on), "hosts")
        self.assertEqual(ui.action("overview", "6"), "")  # no `enabled`: the console's key is off, whatever else is on
        self.assertEqual(ui.action("hosts", "j"), "move")
        self.assertEqual(ui.action("hosts", "\r"), "")
        self.assertEqual(ui.action("hosts", "enter"), "pick")
        for k, act in (("1", "rview"), ("e", "rview"), ("tab", "rnext"), ("btab", "rnext"), ("j", "scroll"), ("end", "scroll"), ("esc", "back"), ("q", "back")):
            self.assertEqual(ui.action("remote", k), act, k)
        words = {r.action for r in ui.rows("remote")}
        self.assertFalse(words & {"screen", "next", "prev", "hosts"}, "a remote host has no local screens")
        groups = dict(ui.help_rows("remote", render.on))
        text = " ".join(v for items in groups.values() for _k, v in items)
        self.assertNotIn("Overview, Map", text)
        self.assertIn("scroll", text)

    def test_the_web_help_does_not_get_the_consoles_key(self):
        shown = [k for _t, items in ui.help_rows("global", lambda f: f != ui.CONSOLE_FLEET) for k, _v in items]
        self.assertNotIn("6", shown)


class Listing(Fleet):
    def test_this_host_first_then_the_peers_worst_first_in_the_webs_order(self):
        state = render.hosts_state(NOW)
        doc, rows = state[1], state[2]
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in webhosts.order(doc["hosts"])])
        sev = [webhosts.severity(r["state"]) for r in rows]
        self.assertEqual(sev, sorted(sev, reverse=True), "worst first")
        self.assertEqual({r["id"] for r in rows}, set(NAMES))
        self.assertEqual(webhosts.tier(next(r for r in rows if r["id"] == "foxtrot")["state"]), 1, "no entry: not known, never green")
        for cols, nrows in ((80, 24), (120, 33)):
            lines, _raw = self.once(["--view", "hosts"], cols, nrows)
            body = [x for x in lines if x.strip()]
            self.assertTrue(LOCAL_TABS.search(lines[0]), "the list is this machine's screen: the local header")
            first = next(x for x in body if x.lstrip().startswith("· this host"))
            self.assertLess(body.index(first), min(i for i, x in enumerate(body) if "alpha" in x))
            order = [n for x in body for n in NAMES if re.search(r"\b%s\b" % n, x)]
            self.assertEqual(order, [r["id"] for r in rows][:len(order)])
            self.assertIn("this host", first)
            self.assertIn(nuc_config.host_label(render.CFG, render.socket.gethostname), first)

    def test_this_host_is_named_by_host_name_when_set(self):
        render.CFG["host"] = dict(render.CFG.get("host") or {}, name="wall-nuc")
        self.addCleanup(lambda: render.CFG["host"].pop("name", None))
        lines, _ = self.once(["--view", "hosts"], 120, 33)
        self.assertIn("wall-nuc", next(x for x in lines if x.lstrip().startswith("· this host")))

    def test_every_state_has_a_symbol_and_a_word(self):
        lines, _ = self.once(["--view", "hosts"], 120, 33)
        text = "\n".join(lines)
        for word in ("unreachable", "untrusted", "warn", "ok", "stale", "unknown"):
            self.assertRegex(text, r"[✔!✖?·] " + word)

    def test_the_cursor_row_is_in_reverse_and_follows_the_keys(self):
        frames = self.run_main([b"6", b"j", b"j", b"k", END, HOME])
        pos = [re.search(r"host (\d+)/(\d+)", f[-1]).group(1) for f in frames[1:]]
        self.assertEqual(pos, ["1", "2", "3", "2", "9", "1"])
        self.assertEqual(re.search(r"host \d+/(\d+)", frames[1][-1]).group(1), "9")  # this host and eight peers
        _lines, raw = self.once(["--view", "hosts", "--select", "echo"], 120, 33)
        shown = [x for x in raw.split(LINE) if "\x1b[7m" in x]
        self.assertTrue(any("echo" in ansi.ANSI.sub("", x) for x in shown))

    def test_small_and_tall_consoles_and_a_long_list(self):
        for cols, rows in ((80, 24), (120, 33), (60, 12), (40, 8)):
            self.once(["--view", "hosts"], cols, rows)
        render.CFG["hosts"] = [fleetfix.peer("h%02d" % i) for i in range(16)]
        frames = self.run_main([b"6", END, PGUP, HOME], cols=80, rows=12)
        self.assertIn("host 17/17", frames[2][-1])
        self.assertIn("host 1/17", frames[4][-1])

    def test_the_demo_shows_the_list_and_a_remote_view_without_a_poller(self):
        empty = os.path.join(self.tmp.name, "nothing")
        with mock.patch.dict(os.environ, {"NUC_CONSOLE_PEERS_CACHE": empty}):
            lines, _ = self.once(["--view", "hosts"], 120, 33)
            self.assertIn("this host", "\n".join(lines))
            self.assertIn("alpha", "\n".join(lines))
            remote, _ = self.once(["--host", "alpha"], 120, 33)
            self.assertTrue(remote[0].startswith(" ⇄ alpha (remote "))
            self.assertIn("SYSTEM", "\n".join(remote))


class Remote(Fleet):
    def test_each_of_the_five_documents_is_the_hosts_body_under_the_remote_header(self):
        docs = real_docs()
        for cols, rows in ((80, 24), (120, 33)):
            w = cols - 1
            for view in VIEWS:
                with self.subTest(view=view, size=(cols, rows)):
                    lines, raw = self.once(["--host", "alpha", "--view", view], cols, rows)
                    self.assertEqual(raw.split(LINE)[0], render.remote_header("alpha", 12, w))
                    self.assertEqual(sum(1 for x in lines if x.startswith(" ⇄ ")), 1)
                    self.assertFalse(any(LOCAL_TABS.search(x) for x in lines), "no local header under the remote one")
                    body = render.remote_body(docs[view], w)
                    got = raw.split(LINE)[1:rows - 1]
                    self.assertEqual(got, [ansi.clip(x, w) for x in body[:rows - 2]] + [""] * max(0, rows - 2 - len(body)))
                    self.assertIn(hostsview.VIEW_WORD[view] + " · lines", lines[-1])

    def test_the_header_lines_in_the_three_demo_os(self):
        want = "\x1b[1;7;35m ⇄ alpha (remote 12s)" + " " * (119 - len(" ⇄ alpha (remote 12s)")) + "\x1b[0m"
        local = {}
        for os_name in ("linux", "windows", "darwin"):
            with golden.FrozenWorld(cfg={"hosts": [fleetfix.peer("alpha")]}, fleet=lambda now: entries(now)[:1]) as world:
                got = world.once(["--host", "alpha", "--demo-os", os_name, "--cols", "120", "--rows", "33"]).split(LINE)
                self.assertEqual(got[0], want, os_name)
                self.assertTrue(got[0].startswith("\x1b[1;7;35m"), os_name)
                local[os_name] = world.once(["--demo-os", os_name, "--cols", "120", "--rows", "33"]).split(LINE)[0]
                self.assertNotEqual(local[os_name], want)
                self.assertNotIn("⇄", local[os_name])

    def test_missing_documents_say_why(self):
        none, _ = self.once(["--host", "foxtrot"])
        self.assertIn("no data from foxtrot yet (the poller has not reached it)", " ".join(none))
        summary, _ = self.once(["--host", "delta"])
        self.assertIn("its token has scope summary", " ".join(summary))
        events, _ = self.once(["--host", "bravo", "--view", "events"])
        self.assertIn("not available for this peer", " ".join(events))
        for lines in (none, summary, events):
            self.assertTrue(lines[0].startswith(" ⇄ "))
        self.assertFalse(any(LOCAL_TABS.search(x) for x in none + summary + events))

    def test_banners_for_an_old_reading_a_dead_peer_and_a_changed_identity(self):
        old, _ = self.once(["--host", "golf"], 120, 33)
        self.assertIn("STALE", old[1])
        self.assertIn("remote 6m", old[0])
        dead, _ = self.once(["--host", "charlie"], 120, 33)
        self.assertIn("UNREACHABLE", dead[1])
        who, _ = self.once(["--host", "echo"], 120, 33)
        self.assertIn("IDENTITY CHANGED", who[1])

    def test_a_hostile_cache_is_drawn_as_text_only(self):
        for cols, rows in ((80, 24), (120, 33)):
            lines, raw = self.once(["--host", "hotel"], cols, rows)
            self.assertNotIn("\x1b", SGR.sub("", raw).replace("\x1b[K", "").replace("\r\n", ""))
            self.assertFalse(re.search(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f‪-‮]", "".join(lines)))
            self.assertTrue(lines[0].startswith(" ⇄ hotel"))
            self.assertIn("EVIL", "".join(lines), "the hostile text was read (as plain text), not dropped with the whole document")
        lst, raw = self.once(["--view", "hosts"], 120, 33)
        self.assertNotIn("\x1b", SGR.sub("", raw).replace("\x1b[K", "").replace("\r\n", ""))

    def test_the_keys_choose_the_remote_view_and_say_when_a_view_is_missing(self):
        docs = real_docs()
        w = 79
        keys = [b"6"] + self.to("bravo")
        n = len(keys)
        frames = self.run_main(keys + [b"2", b"3", b"e", b"4", b"1"])
        self.assertEqual([kind(f) for f in frames[n - 1:n + 1]], ["local", "remote"])
        after = frames[n:]
        for f, v in zip(after, ("overview", "map", "cpu", "cpu", "health", "overview")):
            self.assertIn(hostsview.VIEW_WORD[v] + " · lines" if f is not after[3] else "not available", f[-1])
        self.assertIn("not available for this peer: it served no events document", after[3][-1])
        self.assertEqual(after[3][1:-1], after[2][1:-1], "a view the peer does not hold leaves the screen as it is")
        shown = render.remote_body(docs["map"], w)
        self.assertEqual(after[1][1:4], [ansi.ANSI.sub("", ansi.clip(x, w)) for x in shown[:3]])

    def test_a_summary_peer_has_no_view_to_choose(self):
        keys = [b"6"] + self.to("delta")
        n = len(keys)
        frames = self.run_main(keys + [b"2", b"4", b"5"])
        self.assertTrue(all(kind(f) == "remote" for f in frames[n:]))
        self.assertIn("scope summary", " ".join(frames[n]))
        self.assertIn("its token has scope summary", frames[n + 1][-1])
        self.assertIn("never shown for another host", frames[n + 3][-1])

    def test_tab_goes_round_the_views_the_peer_holds(self):
        keys = [b"6"] + self.to("bravo")
        n = len(keys)
        frames = self.run_main(keys + [b"\t"] * 5 + [b"\x1b[Z"])
        titles = [re.search(r"(overview|map|CPU|health|events) · lines", f[-1]) for f in frames[n:]]
        self.assertEqual([m.group(1) if m else None for m in titles], ["overview", "map", "CPU", "health", "overview", "map", "overview"])

    def test_the_pager_scrolls_the_tall_body(self):
        w = 79
        body = render.remote_body(real_docs()["overview"], w)
        self.assertGreater(len(body), 60)
        keys = [b"6"] + self.to("alpha")
        n = len(keys)
        frames = self.run_main(keys + [b"j", b"j", PGDN, END, b"k", HOME])
        tops = [int(re.search(r"lines (\d+)-", f[-1]).group(1)) for f in frames[n:]]
        room = 24 - 2
        self.assertEqual(tops, [1, 2, 3, 3 + room - 1, len(body) - room + 1, len(body) - room, 1])
        for f in frames[n:]:
            self.assertEqual(kind(f), "remote")
        self.assertEqual(frames[n + 3][1], ansi.ANSI.sub("", ansi.clip(body[23], w)))

    def test_esc_and_q_go_back_to_the_list_and_again_to_the_local_screen(self):
        for back in (b"\x1b", b"q"):
            keys = [b"6"] + self.to("alpha")
            n = len(keys)
            frames = self.run_main(keys + [back, back])
            self.assertEqual([kind(f) for f in frames[n - 1:]], ["local", "remote", "local", "local"])
            self.assertIn("HOSTS", "\n".join(frames[n + 1]))
            self.assertNotIn("HOSTS", "\n".join(frames[n + 2]))

    def test_the_help_of_a_remote_screen_names_its_keys(self):
        keys = [b"6"] + self.to("alpha")
        n = len(keys)
        frames = self.run_main(keys + [b"?", b"x"])
        text = "\n".join(frames[n + 1])
        self.assertIn("Keys", text)
        self.assertIn("next/previous view", text)
        self.assertNotIn("Overview, Map", text)
        self.assertEqual(kind(frames[n + 2]), "remote")

    def test_no_key_of_a_local_screen_leaves_a_remote_host(self):
        keys = [b"6"] + self.to("alpha")
        n = len(keys)
        frames = self.run_main(keys + [b"5", b"6", b"m", b"c", b"h", b"a", b"r", b"Z", b"Z"])
        self.assertTrue(all(kind(f) == "remote" for f in frames[n:]))


class Idle(Fleet):
    def test_the_remote_view_returns_to_the_local_screen_after_the_idle_time(self):
        keys = [b"6"] + self.to("alpha")
        n = len(keys)
        frames = self.run_main(keys + [30, b"j", 40, 61, 1])
        self.assertEqual([kind(f) for f in frames[n:]], ["remote", "remote", "remote", "remote", "local", "local"])
        self.assertEqual(frames[n + 4][1:], frames[0][1:], "back on the screen it came from")

    def test_idle_zero_never_returns(self):
        render.CFG["console"]["remote_idle_seconds"] = 0
        keys = [b"6"] + self.to("alpha")
        frames = self.run_main(keys + [100000, 100000])
        self.assertEqual([kind(f) for f in frames[len(keys):]], ["remote"] * 3)

    def test_the_list_returns_too_and_the_local_screen_is_the_exact_one_left(self):
        frames = self.run_main([b"2", b"j", b"j", b"6", 59, 2, 1])
        self.assertEqual([kind(f) for f in frames], ["local"] * 8)
        self.assertIn("HOSTS", "\n".join(frames[4]))
        self.assertIn("HOSTS", "\n".join(frames[5]))
        self.assertNotIn("HOSTS", "\n".join(frames[6]))
        self.assertEqual(frames[6][1:], frames[3][1:], "the Map with its cursor where it was")

    def test_switching_back_restores_the_exact_local_frame(self):
        for first in ([], [b"2", b"j", b"j", b"l"], [b"e"]):
            with self.subTest(first=first):
                before = self.run_main(first + [b"6"])
                after = self.run_main(first + [b"6"] + self.to("alpha") + [b"\x1b", b"\x1b"])
                self.assertEqual(after[-1], before[len(first)], "back where it was")
                self.assertEqual(after[0], before[0])

    def test_a_digit_in_the_list_goes_to_that_local_screen(self):
        frames = self.run_main([b"6", b"3", b"6", b"1"])
        self.assertEqual([kind(f) for f in frames], ["local"] * 5)
        self.assertIn("CPU", frames[2][0])
        self.assertNotIn("HOSTS", "\n".join(frames[4]))


class Never(Fleet):
    def test_the_rotation_never_has_the_hosts_or_a_remote_view(self):
        st = render.snapshot(79)
        sm = render.host_sample(None)
        render.demo_defaults()
        for cfg in ({}, {"map_in_rotation": True, "cpu_in_rotation": True, "health_in_rotation": True}):
            for k, v in cfg.items():
                self.addCleanup(render.CFG.__setitem__, k, render.CFG[k])
                render.CFG[k] = v
            names = [s[0] for s in render.slides(sm, st["cont"], st["net"], 79, 20, st["boot"], st["baseline"])]
            self.assertFalse([n for n in names if "ost" in n or "emote" in n], names)
        frames = self.run_main([20] * 12)
        self.assertEqual([kind(f) for f in frames], ["local"] * 13)
        self.assertNotIn("⇄", "\n".join("\n".join(f) for f in frames))
        with open(os.path.join(os.path.dirname(os.path.abspath(render.__file__)), "render.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def slides("):src.index("def slide_seconds(")]
        self.assertNotIn("hostsview", body)

    def test_no_socket_no_thread_no_process_anywhere_on_the_path(self):
        def boom(*a, **kw):
            raise AssertionError("a socket was opened")
        threads = threading.active_count()
        with mock.patch("socket.socket", boom), mock.patch("socket.create_connection", boom), mock.patch("socket.getaddrinfo", boom), \
                mock.patch("subprocess.Popen", boom):
            for argv in [["--view", "hosts"], ["--view", "hosts", "--select", "charlie"]] + [["--host", n, "--view", v] for n in NAMES for v in VIEWS]:
                self.once(argv)
            keys = [b"6"] + self.to("alpha") + [b"2", b"3", b"4", b"e", b"\t", END, b"\x1b", b"\x1b"]
            frames = self.run_main(keys)
        self.assertEqual(len(frames), len(keys) + 1)
        self.assertEqual(threading.active_count(), threads)

    def test_a_remote_frame_is_never_local_data_and_a_local_frame_never_remote_data(self):
        frames = self.run_main([b"6"] + self.to("alpha") + [b"2", b"\t", b"\x1b", b"\x1b", b"3"])
        for f in frames:
            k = kind(f)
            self.assertIn(k, ("local", "remote"), f[0])
            if k == "remote":
                self.assertEqual(sum(1 for x in f if x.startswith(" ⇄ ")), 1)
                self.assertFalse(any(LOCAL_TABS.search(x) or "PROBLEM" in x or "ALL OK" in x for x in f), "no local pill, no tab bar under a remote header")
            else:
                self.assertFalse(any(x.startswith(" ⇄ ") for x in f))
                self.assertNotIn("alpha-box", "\n".join(f))

    def test_a_broken_cache_does_not_take_the_console_down(self):
        with mock.patch.object(render, "hosts_state", side_effect=RuntimeError("boom")):
            frames = self.run_main([b"6", b"\x1b"])
        self.assertIn("error on the Hosts screen", "\n".join(frames[1]))
        self.assertNotIn("Hosts screen", "\n".join(frames[2]))


class Config(unittest.TestCase):
    def test_remote_idle_seconds_default_range_and_complaint(self):
        self.assertEqual(nuc_config.load("/nonexistent/x.ini", overlay=False)["console"]["remote_idle_seconds"], 60)
        for text, want, complaint in (("30", 30, False), ("0", 0, False), ("86400", 86400, False), ("-1", 60, True), ("86401", 60, True), ("soon", 60, True)):
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.ini")
                with open(path, "w") as f:
                    f.write("[console]\nremote_idle_seconds = %s\n" % text)
                said = []
                cfg = nuc_config.load(path, warn=said.append, overlay=False)
                self.assertEqual(cfg["console"]["remote_idle_seconds"], want, text)
                self.assertEqual(bool(said), complaint, text)


if __name__ == "__main__":
    unittest.main()
