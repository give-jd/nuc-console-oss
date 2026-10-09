"""The cards of ATTENTION, EXPOSURE, FIREWALL and WEB APPS, built of components (cards.py): the console's text at several detail levels, what
the web has more room for (the problems' ids, why and fix, the accepted ones, the whole names), and the HTML they make.

The console's text for them is held byte for byte by the golden files and by the comparison with the old drawing code (tests/test_golden.py);
here the lines the demo machine gives at each level are written out, the variants (a new port, an [expose] note, a macOS or Windows firewall,
many rules and apps) are held on what they must say, and the web side is checked for structure and for escaping.
"""
import copy
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golden  # noqa: E402
import ansi  # noqa: E402
import cards  # noqa: E402
import demo  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import problems  # noqa: E402
import cardlines  # noqa: E402
import exposure  # noqa: E402
import ui  # noqa: E402

HOSTILE = "<script>alert(1)</script>"
FOUR = ("attention", "exposure", "firewall", "webapps")


def plain(lines):
    return [ansi.ANSI.sub("", x) for x in lines]


def rule(title, w=100, note=""):
    """The title line of a card, as the console writes it, without its colours."""
    return plain([ansi.section(title, w, note)])[0]


class Base(unittest.TestCase):
    """A frozen world (the clock, the default configuration, nothing accepted) and the demo machine's frame."""

    accepted = ()
    cfg = None

    def setUp(self):
        self.world = golden.FrozenWorld(cfg=self.cfg, accepted=self.accepted)
        self.world.__enter__()
        self.addCleanup(self.world.__exit__, None, None, None)
        render.DEMO = True

    def frame(self, os_name=None, tweak=None, **kw):
        """(ctx, net) of the demo machine; tweak(cont, net) changes the data before the problems are worked out."""
        cont, net, boot, base = (copy.deepcopy(x) for x in demo.snapshot(golden.NOW, os_name))
        if tweak:
            tweak(cont, net, base)
        pb = problems.safe_problems(net, cont, golden.NOW, boot=boot, baseline=base)
        args = dict(s=demo.sampler_data(os_name, golden.NOW), cont=cont, net=net, boot=boot, problems=pb, cfg=render.CFG, now=golden.NOW,
                    baseline=base, new=exposure.new_ports(net, cont, base))
        args.update(kw)
        return cards.Ctx(**args)

    def card(self, id, ctx, k=0, width=100, full=False, expand=()):
        return cards.build(id, ctx, k, cards.Caps(width, full, set(expand), set()))

    def lines(self, id, ctx, k=0, width=100, **kw):
        return plain(ansi.card_lines(self.card(id, ctx, k, width, **kw), width)[0])


def nodes(card, kind):
    """The components of a kind in the body of a card, looking into the Details too."""
    out = []
    for part in card.body:
        if isinstance(part, kind):
            out.append(part)
        if isinstance(part, ui.Details):
            out += [x for x in part.body if isinstance(x, kind)]
    return out


# ---- the console's text ----------------------------------------------------------------------------------------------------------------

class ConsoleTests(Base):
    def test_they_are_native_cards_in_the_order_of_the_sections(self):
        ctx = self.frame()
        for id in FOUR:
            self.assertIn(id, cards.NATIVE)
            card = self.card(id, ctx)
            self.assertFalse(any(isinstance(p, ui.Raw) for p in card.body), id)
            self.assertEqual(card.title, cards.CARDS[id].title)
        self.assertEqual(list(cards.CARDS), list(render.nuc_config.SECTIONS))

    def test_attention_at_the_levels(self):
        ctx = self.frame()
        full = [rule("ATTENTION"),
                "   ✖ 1 DB/broker open on LAN",
                "   ! 1 container exited with an error",
                "   ! 2 errors in this boot's journal",
                "   ! 2 Docker ports bypassing ufw (DOCKER-USER empty)",
                "   ! 1 service public on the Internet (Funnel :8444)"]
        for k in (-2, 0, 1):
            self.assertEqual(self.lines("attention", ctx, k), full, k)  # six at the roomy levels
        self.assertEqual(self.lines("attention", ctx, 2), full[:5] + ["   … +1 more"])
        self.assertEqual(self.lines("attention", ctx, 3), full[:4] + ["   … +2 more"])
        self.assertEqual(self.lines("attention", ctx, 3, expand=("attention",)), full)  # the caps lifted
        self.assertTrue(self.card("attention", ctx, 3).truncated)
        self.assertFalse(self.card("attention", ctx, 0).truncated)
        self.assertEqual(self.card("attention", ctx, 3).more, ui.More(2, "more", indent=3))

    def test_attention_nothing_wrong_and_the_accepted_line(self):
        ctx = self.frame(problems=problems.ProblemList())
        self.assertEqual(self.lines("attention", ctx), [rule("ATTENTION"), "   ✔ no problems detected"])
        pb = problems.ProblemList([(1, "w")])
        pb.accepted = 2
        self.assertEqual(self.lines("attention", self.frame(problems=pb))[1:], ["   ! w", "   · 2 accepted as known (nuc-console-problems)"])
        none = problems.ProblemList()
        none.accepted = 1
        self.assertEqual(self.lines("attention", self.frame(problems=none))[1:], ["   ✔ no problems detected", "   · 1 accepted as known (nuc-console-problems)"])

    def test_attention_wraps_a_long_problem_at_its_commas(self):
        pb = problems.ProblemList([(2, "3 services reach beyond config.ini: " + ", ".join(f"service{i} :{8000 + i} LAN > local" for i in range(5)))])
        lines = self.lines("attention", self.frame(problems=pb), width=60)
        self.assertGreater(len(lines), 3)
        self.assertTrue(lines[1].startswith("   ✖ 3 services") and lines[2].startswith("     service"))
        self.assertEqual(lines[1:], plain(ansi.msg_wrap("err", pb[0][1], 60)))  # the console's own wrapping

    def test_exposure_compact_levels(self):
        ctx = self.frame()
        want = [rule("EXPOSURE"),
                " Internet 1   LAN 3   tailnet only 0   local only 4   ⚠ 1 DB/broker on LAN",
                " ● 8444/t funnel /webhook → 127.0.0.1:5678/webhook  public on the Internet",
                " ⚠5432 shop-db-1  ·  8080 shop-web-1  ·  22 sshd"]
        for k in (0, 1, 3):
            self.assertEqual(self.lines("exposure", ctx, k), want, k)

    def test_exposure_the_whole_matrix(self):
        ctx = self.frame()
        want = [rule("EXPOSURE"), "",
                "   Internet 1    LAN 3    Tailscale 4    Local 4        ⚠ 1 DB/broker open on LAN",
                "   ● open   ◐ filtered by source   ? unknown (treated as open)   · no", "",
                "   PORT     SERVICE                               LOC   LAN    TS   NET    NOTE", "",
                "   Reachable from the Internet (Tailscale Funnel)  (1)",
                "    8444/t  funnel /webhook → 127.0.0.1:5678/we   ·     ·     ●     ●    FUNNEL: public", "",
                "   Open on the LAN (and on Tailscale)  (3)",
                "    5432/t ⚠shop-db-1                             ●     ●     ●     ·    docker: bypasses ufw",
                "    8080/t  shop-web-1                            ●     ●     ●     ·    docker: bypasses ufw",
                "      22/t  sshd                                  ●     ◐     ●     ·    LAN only 192.168.0.0/24", "",
                "   This machine only  (4)",
                "     5433 blog-db-1  ·  5678 node  ·  6379 cache-1  ·  8081 blog-app-1"]
        self.assertEqual(self.lines("exposure", ctx, -2), want)
        self.assertEqual(self.lines("exposure", ctx, -1), want)
        self.assertEqual(plain(render.exposure_block(ctx.net, ctx.cont, 100)), want)  # the Network page's block is the same

    def test_exposure_narrow_shortens_the_name_and_the_note_never_a_cell(self):
        ctx = self.frame()
        lines = self.lines("exposure", ctx, -2, width=60)
        self.assertTrue(all(len(x) <= 60 for x in lines))
        row = next(x for x in lines if "5432/t" in x)
        self.assertTrue(row.endswith("●     ●     ●     ·    docker: by…"), row)  # the note ends with an ellipsis, never mid-word silently
        self.assertTrue(row.startswith("    5432/t ⚠shop-db-1 "), row)
        self.assertIn("docker: bypasses ufw", "\n".join(self.lines("exposure", ctx, -2, width=80)))

    def test_exposure_a_new_port_and_an_expose_note(self):
        def tweak(cont, net, base):
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 8888, "proc": "jupyter-lab"})
            net["ufw"]["rules"].append({"to": "8888/tcp", "action": "ALLOW IN", "from": "192.168.0.0/24"})
        ctx = self.frame(tweak=tweak)
        self.assertEqual(ctx.new.get("8888/t:LAN"), "NEW")
        compact = self.lines("exposure", ctx, 0)
        self.assertIn(" NEW 8888 jupyter-lab  ·  ⚠5432 shop-db-1  ·  8080 shop-web-1  ·  22 sshd", compact[3])  # new ones first
        full = self.lines("exposure", ctx, -2)
        self.assertTrue(any("8888/t" in x and "NEW " in x for x in full))

    def test_exposure_beyond_expose(self):
        with mock.patch.dict(render.CFG["expose"], {"shop-db-1": "LOCALE"}, clear=False):
            ctx = self.frame()
            compact = self.lines("exposure", ctx, 0)
            full = self.lines("exposure", ctx, -2)
        self.assertTrue(any("beyond config.ini: local" in x for x in compact), compact)
        self.assertTrue(any("5432/t" in x and "beyond config.ini: local" in x for x in full), full)

    def test_exposure_unavailable(self):
        for net in (None, {"listeners": None}):
            ctx = self.frame()
            ctx.net = net
            self.assertEqual(self.lines("exposure", ctx, 0)[1:], ["   ✖ unavailable"])
            self.assertEqual(self.lines("exposure", ctx, -2)[1:], ["   ✖ unavailable"])

    def test_firewall_at_the_levels(self):
        ctx = self.frame()
        compact = [rule("FIREWALL"),
                   "   ✔ ufw active",
                   "   ! DOCKER-USER empty: ports published by containers bypass ufw",
                   "   INPUT DROP · FORWARD DROP   ts-input ✔   fail2ban sshd:2   drop 1h 41"]
        for k in (0, 2, 3):
            self.assertEqual(self.lines("firewall", ctx, k), compact, k)
        full = [rule("FIREWALL"), "", compact[1], compact[2], "",
                "   ufw          in deny  ·  out allow  ·  fwd deny   log: on (low)",
                "   iptables     INPUT DROP (5 rules)   FORWARD DROP (0 rules)",
                "   tailscale    ts-input accepts tailscale0",
                "   fail2ban     sshd: 2 ban  198.51.100.7 198.51.100.9",
                "   drop 1h      41",
                "     ports      23×25  445×16",
                "     sources    198.51.100.9×30  198.51.100.44×11", "",
                "   UFW INBOUND RULES (3)",
                "   TO                          ACTION        FROM",
                "   22/tcp                      ALLOW IN      192.168.0.0/24",
                "   Anywhere on tailscale0      ALLOW IN      Anywhere",
                "   41641/udp                   ALLOW IN      Anywhere"]
        self.assertEqual(self.lines("firewall", ctx, -2), full)
        part = self.lines("firewall", ctx, -1)  # the intermediate level: the rules that open a port to the world first
        self.assertEqual(part[:-3], full[:-3])
        self.assertEqual(sorted(part[-3:]), sorted(full[-3:]))
        self.assertEqual(part[-3], full[-1])
        self.assertEqual(plain(render.firewall_block(ctx.net, 100)), full)

    def test_firewall_caps_the_rules_at_the_intermediate_level_most_exposed_first(self):
        def tweak(cont, net, base):
            net["ufw"]["rules"] = [{"to": f"{9000 + i}/tcp", "action": "ALLOW IN", "from": "192.168.0.0/24"} for i in range(14)] \
                + [{"to": "443/tcp", "action": "ALLOW IN", "from": "Anywhere"}, {"to": "Anywhere", "action": "ALLOW OUT", "from": "Anywhere"},
                   {"to": "80/tcp (v6)", "action": "ALLOW IN", "from": "Anywhere (v6)"}]
        ctx = self.frame(tweak=tweak)
        part = self.lines("firewall", ctx, -1)
        rules = [x for x in part if re.match(r"   \d+/tcp", x)]
        self.assertEqual(len(rules), 10)
        self.assertTrue(rules[0].startswith("   443/tcp"), "the one open to the world comes first")
        self.assertEqual(part[-1], "   … +5 rules")
        self.assertIn("   UFW INBOUND RULES (15)   + hidden: 1 outbound, 1 mirrored IPv6", part)
        self.assertTrue(self.card("firewall", ctx, -1).truncated)
        every = self.lines("firewall", ctx, -2)  # the full view: all of them, in their own order
        self.assertEqual(len([x for x in every if re.match(r"   \d+/tcp", x)]), 15)
        self.assertNotIn("… +", every[-1])
        self.assertEqual(len([x for x in self.lines("firewall", ctx, -1, expand=("firewall",)) if re.match(r"   \d+/tcp", x)]), 15)

    def test_firewall_when_it_is_off_unreadable_or_not_installed(self):
        def off(cont, net, base):
            net["ufw"]["active"] = False
            net["docker_user"] = None
            net["errors"] = {"docker_user": "no perm"}
        lines = self.lines("firewall", self.frame(tweak=off), 0)
        self.assertEqual(lines[1], "   ✖ ufw OFF: no LAN filtering for non-Docker services")
        self.assertEqual(lines[2], "   ✖ DOCKER-USER unreadable: no perm")

        def gone(cont, net, base):
            net["ufw"] = None
            net["absent"] = ["ufw"]
        self.assertEqual(self.lines("firewall", self.frame(tweak=gone), 0)[1], "   · ufw not installed: LAN filtering cannot be verified from here (nft/firewalld?)")

        def broken(cont, net, base):
            net["ufw"] = None
            net["errors"] = {"ufw": "permission denied"}
        self.assertEqual(self.lines("firewall", self.frame(tweak=broken), 0)[1], "   ✖ ufw unreadable: permission denied")
        self.assertEqual(self.lines("firewall", self.frame(net=None), 0)[1:], ["   ✖ network collector not running"])

    def test_firewall_on_windows_and_macos(self):
        win = self.frame("windows")
        self.assertEqual(self.lines("firewall", win, 0)[1:], ["   ✔ Windows Firewall on (active: Private)", "   4 listening ports let in"])
        full = self.lines("firewall", win, -2)
        self.assertIn("   Private      on   inbound block   ← active", full)
        self.assertIn("   WHAT LETS PORTS IN (3)", full)
        self.assertIn('   rule "Docker Desktop Backend" (Private)     5432/t, 8080/t', full)
        mac = self.frame("darwin")
        self.assertEqual(self.lines("firewall", mac, 0)[1:], ["   ✔ macOS firewall on · stealth", "   5 listening ports let in"])
        self.assertIn("   pf           off", self.lines("firewall", mac, -2))

        def off(cont, net, base):
            net["firewall"] = dict(net["firewall"], off=["Public"])
        self.assertEqual(self.lines("firewall", self.frame("windows", off), 0)[1], "   ✖ Windows Firewall OFF on Public: no filtering there")
        row = next(x for x in ansi.card_lines(self.card("firewall", self.frame("windows", off)), 100)[0] if "OFF" in x)
        self.assertIn("\x1b[1;31mWindows Firewall OFF\x1b[0m", row)  # the part that is the point stays bold red

        def disabled(cont, net, base):
            net["firewall"], net["disabled"] = None, ["firewall"]
        self.assertEqual(self.lines("firewall", self.frame("windows", disabled), 0)[1], "   · firewall check disabled in config.ini")

    def test_webapps_levels_the_cap_and_the_note(self):
        ctx = self.frame()
        want = [rule("WEB APPS", 100, "4 active"),
                " ● funnel /webhook        8444         Internet      ",
                " ● shop-web-1             8080         LAN+tailnet   ",
                " ● blog-app-1             8081         local only    ",
                " ● node                   5678         local only    "]
        for k in (-2, 0, 3):
            self.assertEqual(self.lines("webapps", ctx, k), want, k)
        self.assertEqual(self.card("webapps", ctx).note, "4 active")

    def test_webapps_many_declared_apps_are_capped_by_the_level(self):
        names = {f"app{i:02d}": [9000 + i] for i in range(15)}
        with mock.patch.dict(render.CFG["webapps"], names, clear=True):
            ctx = self.frame()
            rows = lambda k, **kw: [x for x in self.lines("webapps", ctx, k, **kw) if x[:2] in (" ●", " ○")]  # noqa: E731
            self.assertEqual(len(rows(0)), 12)
            self.assertEqual(len(rows(2)), 6)
            self.assertEqual(len(rows(3)), 4)
            self.assertEqual(self.lines("webapps", ctx, 3)[-1], " … +" + str(len(exposure.webapp_rows(ctx.net, ctx.cont)) - 4) + " more")
            self.assertEqual(len(rows(3, expand=("webapps",))), len(exposure.webapp_rows(ctx.net, ctx.cont)))
            self.assertEqual(len(rows(3, full=True)), len(exposure.webapp_rows(ctx.net, ctx.cont)))
            self.assertTrue(self.card("webapps", ctx, 3).truncated)
            note = self.card("webapps", ctx).note
            self.assertRegex(note, r"^\d+ active · 15 down \(expected\)$")
            down = [x for x in self.lines("webapps", ctx, -2) if "DOWN (expected)" in x]
            self.assertTrue(down and "○" in down[0] and "not listening" in down[0])

    def test_webapps_says_why_it_has_nothing(self):
        ctx = self.frame()
        ctx.net = None
        self.assertEqual(self.lines("webapps", ctx)[1:], ["   ✖ network collector not running"])
        ctx = self.frame(net={"ts": golden.NOW, "listeners": None, "errors": {"listeners": "ss failed"}})
        self.assertEqual(self.lines("webapps", ctx)[1:], ["   ! unavailable: ss failed"])
        ctx = self.frame(net={"ts": golden.NOW, "listeners": []}, cont={"ts": golden.NOW, "containers": []})
        self.assertEqual(self.lines("webapps", ctx)[1:], ["   · no web apps found (declare the ones you expect under [webapps] in config.ini)"])

    def test_a_card_whose_data_is_broken_is_its_title_and_a_message(self):
        ctx = self.frame()
        ctx.net = {"listeners": [{"port": "x"}]}
        for id in ("exposure", "webapps"):
            lines = ansi.card_lines(self.card(id, ctx), 80)[0]
            self.assertEqual(lines[0], ansi.section(cards.CARDS[id].title, 80), id)
            self.assertEqual(len(lines), 2, id)
            self.assertIn("✖", lines[1])


# ---- what the web has more room for ------------------------------------------------------------------------------------------------------

class AttentionExtrasTests(Base):
    accepted = (("docker-bypass", 1, "2 Docker ports bypassing ufw (DOCKER-USER empty)"),)

    def test_every_problem_has_its_id_why_fix_and_accept_command(self):
        ctx = self.frame()
        probs = nodes(self.card("attention", ctx, -2), ui.Problem)
        self.assertEqual([p.id for p in probs], ["db-open-lan", "container-exited", "journal-errors", "funnel-public"])
        by = {p.id: p for p in probs}
        db = by["db-open-lan"]
        self.assertEqual(db.level, "err")
        self.assertEqual((db.title, db.why), (problems.CATALOG["db-open-lan"][0], problems.CATALOG["db-open-lan"][1]))
        self.assertEqual(db.fix, problems.CATALOG["db-open-lan"][2])
        self.assertEqual(db.accept, 'sudo nuc-console-accept --problem db-open-lan --reason "..."')
        self.assertEqual(by["journal-errors"].fix, problems.CATALOG["journal-errors"][2])
        for p in probs:
            self.assertTrue(p.id and p.title and p.why and p.fix and p.accept, p.id)

    def test_what_the_console_draws_is_unchanged_by_them(self):
        ctx = self.frame()
        with_extras = self.lines("attention", ctx, -2)
        bare = problems.ProblemList(list(ctx.problems))  # no ids, no why, no fix
        bare.accepted = ctx.problems.accepted
        self.assertEqual(with_extras, self.lines("attention", self.frame(problems=bare), -2))

    def test_the_accepted_ones_are_a_details_with_their_reason_and_the_forget_command(self):
        ctx = self.frame()
        self.assertEqual(ctx.problems.accepted, 1)
        card = self.card("attention", ctx, -2)
        det = [p for p in card.body if isinstance(p, ui.Details)]
        self.assertEqual(len(det), 1)
        self.assertEqual(ansi.render(det[0].summary, 60)[0], ["1 accepted"])
        (acc,) = nodes(card, ui.Accepted)
        self.assertEqual((acc.id, acc.reason, acc.undo), ("docker-bypass", "known", "sudo nuc-console-accept --forget docker-bypass"))
        self.assertEqual(acc.text, "2 Docker ports bypassing ufw (DOCKER-USER empty)")
        self.assertEqual(self.lines("attention", ctx, -2)[-1], "   · 1 accepted as known (nuc-console-problems)")  # the console: the one line
        out = htmlview.html(card)
        self.assertIn('<details class="dt"><summary>1 accepted</summary>', out)
        self.assertIn('<code class="pid">docker-bypass</code>', out)
        self.assertIn("reason: “known”", out)
        self.assertIn('undo: <code class="cmd">sudo nuc-console-accept --forget docker-bypass</code>', out)

    def test_the_commands_are_the_ones_of_the_install_and_of_the_portable_run(self):
        ctx = self.frame()
        hints = {h.label: h.cmd for h in nodes(self.card("attention", ctx, -2), ui.Hint)}
        self.assertEqual(hints, {"list with why and fix": "nuc-console-problems",
                                 "accept a known one": 'sudo nuc-console-accept --problem <id> --reason "..."'})
        with mock.patch.object(problems, "ACCEPT_CMD", "./run.sh --accept"), mock.patch.object(problems, "PROBLEMS_CMD", "./run.sh --problems"):
            ctx = self.frame()
            card = self.card("attention", ctx, -2)
            hints = {h.label: h.cmd for h in nodes(card, ui.Hint)}
            self.assertEqual(hints["list with why and fix"], "./run.sh --problems")
            self.assertEqual(hints["accept a known one"], './run.sh --accept --problem <id> --reason "..."')
            self.assertEqual(nodes(card, ui.Problem)[0].accept, './run.sh --accept --problem db-open-lan --reason "..."')
            self.assertEqual(nodes(card, ui.Accepted)[0].undo, "./run.sh --accept --forget docker-bypass")
            self.assertEqual(self.lines("attention", ctx, -2)[-1], "   · 1 accepted as known (./run.sh --problems)")
            out = htmlview.html(card)
            self.assertIn('<code class="cmd">./run.sh --problems</code>', out)
            self.assertIn('./run.sh --accept --forget docker-bypass', out)

    def test_a_port_change_is_accepted_with_the_baseline_not_with_a_reason(self):
        def tweak(cont, net, base):
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 8888, "proc": "jupyter-lab"})
        ctx = self.frame(tweak=tweak)
        card = self.card("attention", ctx, -2)
        new = next(p for p in nodes(card, ui.Problem) if p.id == "port-new")
        self.assertEqual(new.level, "err")
        self.assertEqual(new.accept, "")
        self.assertIn("nuc-console-accept", new.fix)
        hints = {h.label: h.cmd for h in nodes(card, ui.Hint)}
        self.assertEqual(hints["a port change is accepted with the baseline"], "sudo nuc-console-accept")
        self.assertIn('<code class="pid" title="New exposed port">port-new</code>', htmlview.html(card))

    def test_a_list_that_problems_did_not_build_has_no_extras_and_invents_none(self):
        pb = problems.ProblemList([(2, "x"), (1, "y")])
        card = self.card("attention", self.frame(problems=pb), -2)
        for p in nodes(card, ui.Problem):
            self.assertEqual((p.id, p.title, p.why, p.fix, p.accept), ("", "", "", "", ""))
        plain_list = [(2, "x")]
        card = self.card("attention", self.frame(problems=plain_list), -2)
        self.assertEqual(nodes(card, ui.Hint), [])
        self.assertEqual(self.lines("attention", self.frame(problems=plain_list))[1:], ["   ✖ x"])
        self.assertNotIn("<code", htmlview.html(card))

    def test_the_problems_html(self):
        out = htmlview.html(self.card("attention", self.frame(), -2))
        self.assertEqual(len(re.findall(r'<div class="msg prob lv-(?:err|warn)" data-problem="[a-z-]+">', out)), 4)
        self.assertIn('<code class="pid" title="Database/broker open on the LAN">db-open-lan</code></span><span class="d why">data services should not be reachable from the network</span>', out)
        self.assertIn('<details class="fix" data-k="fix-db-open-lan"><summary>fix</summary>', out)
        self.assertIn('<span class="d how">fix: <code class="cmd">publish the DB on 127.0.0.1 (scripts/rebind-all-dbs.sh) or stop it if unused</code>', out)
        self.assertIn('accept if known: <code class="cmd">sudo nuc-console-accept --problem db-open-lan --reason &quot;...&quot;</code>', out)
        self.assertIn('<p class="hint d">list with why and fix: <code class="cmd">nuc-console-problems</code></p>', out)
        self.assertEqual(len(re.findall("<article", out)), 1)
        self.assertNotIn("\x1b", out)


class ExposureWebTests(Base):
    def test_the_matrix_is_a_table_with_groups_and_glyph_cells(self):
        ctx = self.frame()
        card = self.card("exposure", ctx, -2)
        (table,) = [p for p in card.body if isinstance(p, ui.Table)]
        self.assertEqual([c.key for c in table.cols], ["port", "service", "loc", "lan", "ts", "net", "note"])
        self.assertEqual([g for g, _ in table.groups], ["Reachable from the Internet (Tailscale Funnel)", "Open on the LAN (and on Tailscale)"])
        self.assertEqual([i for _, i in table.groups], [0, 1])
        self.assertEqual([r.key for r in table.rows], ["8444/t:INTERNET", "5432/t:LAN", "8080/t:LAN", "22/t:LAN"])
        glyphs = lambda r: [(c.text, c.tone) for c in r.cells[2:6]]  # noqa: E731
        self.assertEqual(glyphs(table.rows[0]), [("·", "muted"), ("·", "muted"), ("●", "warn"), ("●", "err_strong")])
        self.assertEqual(glyphs(table.rows[1]), [("●", "neutral"), ("●", "err"), ("●", "err"), ("·", "muted")])  # a database: red, not amber
        self.assertEqual(glyphs(table.rows[3]), [("●", "neutral"), ("◐", "accent"), ("●", "warn"), ("·", "muted")])  # filtered by source
        out = htmlview.html(card)
        self.assertIn('<tr class="grp"><th colspan="7" scope="rowgroup">Reachable from the Internet (Tailscale Funnel) <span class="n">(1)</span></th></tr>', out)
        self.assertIn('<tr class="grp"><th colspan="7" scope="rowgroup">Open on the LAN (and on Tailscale) <span class="n">(3)</span></th></tr>', out)
        self.assertIn('<tr data-key="5432/t:LAN">', out)
        self.assertIn('<td class="c"><span class="t-err">●</span></td>', out)
        self.assertIn('<td class="c"><span class="t-accent">◐</span></td>', out)
        self.assertIn('<th class="c" scope="col">LAN</th>', out)
        self.assertIn("5432/tcp", out)  # the web has the room for the whole protocol, the console writes /t
        self.assertNotIn("   PORT  ", out)  # the console's header line is not drawn
        self.assertIn("This machine only", out)
        self.assertIn("5433 blog-db-1", out)
        self.assertIn("</span> unknown (treated as open)</li>", out)  # the legend, a list of symbols the web lays out

    def test_the_note_is_whole_on_the_web_and_cut_on_the_console(self):
        def tweak(cont, net, base):
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 9090, "proc": "a-rather-long-process-name-that-needs-the-room"})
            net["ufw"]["rules"].append({"to": "9090/tcp", "action": "ALLOW IN", "from": "192.168.0.0/24"})
        ctx = self.frame(tweak=tweak)
        table = next(p for p in self.card("exposure", ctx, -2, width=80).body if isinstance(p, ui.Table))
        row = next(r for r in table.rows if r.key == "9090/t:LAN")
        svc = row.cells[1].spans[1]
        self.assertLess(len(svc.text), len(svc.full))
        self.assertEqual(svc.full, "a-rather-long-process-name-that-needs-the-room")
        out = htmlview.html(self.card("exposure", ctx, -2, width=80))
        self.assertIn("a-rather-long-process-name-that-needs-the-room", out)

    def test_the_expose_notes_are_in_the_table(self):
        with mock.patch.dict(render.CFG["expose"], {"shop-db-1": "LOCALE", "shop-web-1": "LAN"}, clear=False):
            card = self.card("exposure", self.frame(), -2)
        table = next(p for p in card.body if isinstance(p, ui.Table))
        notes = {r.key: r.cells[6] for r in table.rows}
        db, web = notes["5432/t:LAN"], notes["8080/t:LAN"]
        self.assertEqual((db.text, db.tone), ("beyond config.ini: local", "err"))
        self.assertEqual(web.full, "expected: LAN · docker: bypasses ufw")  # (the console's text is the one cut to the room)

    def test_hostile_names_are_escaped(self):
        def tweak(cont, net, base):
            cont["containers"].append({"name": HOSTILE, "status": "Up", "state": "running", "project": "", "ports": [{"p": 9090, "s": "*"}], "mem": 1})
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 9090, "proc": "docker-proxy"})
        ctx = self.frame(tweak=tweak)
        for k in (-2, 0):
            out = htmlview.html(self.card("exposure", ctx, k, width=200))
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out, k)
            self.assertNotIn("<script", out, k)
            self.assertNotIn("alert(1)</", out, k)


class FirewallAndWebAppsWebTests(Base):
    def test_the_firewall_is_messages_and_key_values(self):
        card = self.card("firewall", self.frame(), -2)
        kinds = [type(p).__name__ for p in card.body]
        self.assertEqual(kinds, ["Line", "Msg", "Msg", "Line", "KV", "Line", "Line", "Table"])  # (a blank line is for the console)
        self.assertEqual(kinds.count("KV"), 1)
        (kv,) = [p for p in card.body if isinstance(p, ui.KV)]
        self.assertEqual([k for k, _ in kv.pairs], ["ufw", "iptables", "tailscale", "fail2ban", "drop 1h", "  ports", "  sources"])
        out = htmlview.html(card)
        self.assertIn('<p class="msg lv-ok"><span class="sym">✔</span> ufw active</p>', out)
        self.assertIn('<p class="msg lv-warn"><span class="sym">!</span> DOCKER-USER empty: ports published by containers bypass ufw</p>', out)
        self.assertIn("<dt>iptables</dt><dd>INPUT DROP (5 rules)   FORWARD DROP (0 rules)</dd>", out)
        self.assertIn('<dt>tailscale</dt><dd><span class="t-ok">ts-input accepts tailscale0</span></dd>', out)
        self.assertIn('<th scope="col">To</th>', out)
        self.assertIn("22/tcp", out)

    def test_a_rule_that_opens_a_port_to_the_world_is_a_warm_row(self):
        def tweak(cont, net, base):
            net["ufw"]["rules"].append({"to": "443/tcp", "action": "ALLOW IN", "from": "Anywhere"})
        card = self.card("firewall", self.frame(tweak=tweak), -2)
        table = next(p for p in card.body if isinstance(p, ui.Table))
        warm = [r for r in table.rows if r.tone == "warn"]
        self.assertEqual([r.cells[0].text for r in warm], ["41641/udp", "443/tcp"])  # (the demo has one: the world may reach it)
        self.assertIn('<tr class="t-warn">', htmlview.html(card))

    def test_the_macos_and_windows_firewall_are_key_values_and_a_table(self):
        card = self.card("firewall", self.frame("windows"), -2)
        (kv,) = [p for p in card.body if isinstance(p, ui.KV)]
        self.assertEqual([k for k, _ in kv.pairs], ["Domain", "Private", "Public", "networks", "rules"])
        out = htmlview.html(card)
        self.assertIn('<td>rule &quot;Docker Desktop Backend&quot; (Private)</td>', out)
        self.assertIn("5432/t, 8080/t", out)

    def test_webapps_are_a_table(self):
        ctx = self.frame()
        card = self.card("webapps", ctx, 0)
        (table,) = [p for p in card.body if isinstance(p, ui.Table)]
        self.assertEqual([c.label for c in table.cols], ["", "Web app", "Ports", "Reach", ""])
        self.assertEqual([r.key for r in table.rows], ["funnel /webhook", "shop-web-1", "blog-app-1", "node"])
        out = htmlview.html(card)
        self.assertIn('<th scope="col">Web app</th>', out)
        self.assertIn('<tr data-key="shop-web-1">', out)
        self.assertIn('<span class="t-warn">LAN+tailnet</span>', out)
        self.assertIn('<span class="t-err">Internet</span>', out)
        self.assertIn("<span class=\"t-ok\">●</span>", out)
        self.assertEqual(htmlview.html(card).count("<tr "), 4)

    def test_webapps_show_the_whole_name_and_escape_it(self):
        def tweak(cont, net, base):
            cont["containers"].append({"name": HOSTILE, "status": "Up", "state": "running", "project": "", "ports": [{"p": 9090, "s": "*"}], "mem": 1})
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 9090, "proc": "docker-proxy"})
        decl = {"declared-" + "x" * 40: [9091], HOSTILE + "decl": [9092]}
        with mock.patch.dict(render.CFG["webapps"], decl, clear=True):
            ctx = self.frame(tweak=tweak)
            out = htmlview.html(self.card("webapps", ctx, 0, width=60))
            console = "\n".join(plain(ansi.card_lines(self.card("webapps", ctx, 0, width=60), 60)[0]))
        self.assertIn("declared-" + "x" * 40, out)         # the web has the room
        self.assertNotIn("declared-" + "x" * 40, console)  # the console had not
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)
        self.assertNotIn("<script", out)
        self.assertIn("DOWN (expected)", out)

    def test_every_card_is_html_without_leftovers_and_in_every_state(self):
        def hostile(cont, net, base):
            cont["containers"].append({"name": HOSTILE, "status": "Up", "state": "running", "project": "", "ports": [{"p": 9090, "s": "*"}], "mem": 1})
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 9090, "proc": "docker-proxy"})
            net["f2b"] = {"jails": [{"name": HOSTILE, "banned": 1, "ips": [HOSTILE]}]}
            if net.get("ufw"):
                net["ufw"]["rules"].append({"to": HOSTILE, "action": "ALLOW IN", "from": "Anywhere"})
        for os_name in (None, "windows", "darwin"):
            for k in (-2, -1, 0, 3):
                ctx = self.frame(os_name, hostile)
                for id in FOUR:
                    out = htmlview.html(self.card(id, ctx, k, width=120))
                    self.assertIn(f'data-card="{id}"', out)
                    self.assertNotIn("\x1b", out)
                    self.assertNotIn("<script", out, (os_name, k, id))
                    self.assertNotIn("<pre", out)
                    self.assertEqual(len(re.findall("<article", out)), len(re.findall("</article>", out)))
                    self.assertEqual(len(re.findall("<table", out)), len(re.findall("</table>", out)))
                    self.assertEqual(len(re.findall("<details", out)), len(re.findall("</details>", out)))
                    self.assertEqual(out.count('"') % 2, 0)


class WrapperTests(Base):
    """The functions render.py has always had are thin: the card drawn at the same level."""

    def test_ov_functions_are_the_cards(self):
        ctx = self.frame()
        for k in (-2, -1, 0, 3):
            for id, lines in (("attention", cardlines.ov_attention(ctx.problems, 100, k)), ("exposure", cardlines.ov_esposizione(ctx.net, ctx.cont, 100, k, ctx.new)),
                              ("firewall", cardlines.ov_firewall(ctx.net, 100, k)), ("webapps", cardlines.ov_webapp(ctx.net, ctx.cont, 100, k))):
                self.assertEqual(plain(lines), self.lines(id, ctx, k), (id, k))

    def test_the_overview_is_made_of_these_cards(self):
        ctx = self.frame()
        shown = plain(render.page_overview(ctx.s, ctx.cont, ctx.net, ctx.boot, 200, 60, baseline=ctx.baseline, now=golden.NOW))
        for title in ("ATTENTION", "EXPOSURE", "FIREWALL", "WEB APPS"):
            self.assertTrue(any(title in x for x in shown), title)
        self.assertTrue(json.dumps(shown))


if __name__ == "__main__":
    unittest.main()
