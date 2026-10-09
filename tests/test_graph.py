import copy
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import demo  # noqa: E402
import graph  # noqa: E402
import render  # noqa: E402,F401 - graph reads the exposure rules from it

WEBAPPS = {"shop-web": [8080], "admin-console": [9443]}


def demo_graph(os_name=None, change=None, webapps=WEBAPPS):
    cont, net, boot, base = demo.snapshot(now=1_790_000_000, os_name=os_name)
    if change:
        change(cont, net, boot)
    return graph.build(cont, net, boot, webapps, now=1_790_000_000, baseline=base)


def labels(G, st=None):
    return [(r["depth"], G["nodes"][r["node"]]["label"]) for r in graph.rows(G, st or graph.State(all=True))]


def edge(G, src, dst):
    return G["_pair"].get((src, dst))


class Model(unittest.TestCase):
    def test_roots_in_order_and_only_the_non_empty_ones(self):
        G = demo_graph()
        self.assertEqual(G["roots"], ["root:internet", "root:lan", "root:local", "root:impact", "root:stacks", "root:outbound"])
        self.assertNotIn("root:tailnet", G["roots"])  # the demo has nothing reachable from the tailnet only

    def test_ports_land_in_their_zone_with_their_owner(self):
        G = demo_graph()
        lan = G["_kids"]["root:lan"]
        self.assertEqual(lan[0], "port:5432/tcp@lan")  # the worst first: a database open on the LAN
        self.assertEqual(G["nodes"]["port:5432/tcp@lan"]["state"], "err")
        self.assertIn(("err", "database/broker open on the LAN"), G["nodes"]["port:5432/tcp@lan"]["findings"])
        self.assertEqual(G["nodes"]["port:8080/tcp@lan"]["owners"], ["ct:shop-web-1"])
        self.assertEqual(G["nodes"]["port:22/tcp@lan"]["owners"], ["proc:sshd"])
        self.assertEqual(G["nodes"]["proc:sshd"]["sub"], "ssh.service")

    def test_funnel_entry_belongs_to_the_process_behind_the_serve_target(self):
        G = demo_graph()
        p = G["nodes"]["port:8444/tcp@internet"]
        self.assertEqual(p["owners"], ["proc:node"])
        self.assertIn(("warn", "public on the Internet (Tailscale Funnel)"), p["findings"])
        self.assertTrue(any(k == "tailscale funnel" and "5678" in v for k, v in p["facts"]))

    def test_ports_say_the_reach_declared_for_them_and_flag_the_excess(self):
        cont, net, boot, base = demo.snapshot(now=1_790_000_000)
        expose = {"shop-db": "LOCALE", "shop-web": "LAN", "n8n": "TAILNET"}
        plain = graph.build(cont, net, boot, WEBAPPS, now=1_790_000_000, baseline=base)
        G = graph.build(cont, net, boot, WEBAPPS, now=1_790_000_000, baseline=base, expose=expose)
        self.assertFalse([n for n in plain["nodes"].values() if any(k == "declared reach" for k, _ in n["facts"])])  # no [expose]: nothing said
        db, web, hook = (G["nodes"][i] for i in ("port:5432/tcp@lan", "port:8080/tcp@lan", "port:8444/tcp@internet"))
        self.assertIn(("declared reach", "local (config.ini [expose])"), db["facts"])
        self.assertIn(("err", "declared local in config.ini, reachable from the LAN and the tailnet"), db["findings"])
        self.assertIn(("declared reach", "LAN (config.ini [expose])"), web["facts"])
        self.assertFalse([1 for lv, t in web["findings"] if "declared" in t])                                          # within its reach: a fact only
        self.assertEqual(web["state"], plain["nodes"]["port:8080/tcp@lan"]["state"])
        self.assertIn(("declared reach", "tailnet (config.ini [expose])"), hook["facts"])                             # n8n: the unit behind the Funnel
        self.assertIn(("err", "declared tailnet in config.ini, reachable from the Internet (Tailscale Funnel)"), hook["findings"])
        self.assertEqual((plain["nodes"]["port:8444/tcp@internet"]["state"], hook["state"]), ("warn", "err"))
        self.assertFalse([k for k, _ in G["nodes"]["port:22/tcp@lan"]["facts"] if k == "declared reach"])           # nothing declared for sshd

    def test_evidence_is_merged_per_pair_and_the_strongest_wins(self):
        G = demo_graph()
        e = edge(G, "ct:shop-api-1", "ct:shop-db-1")
        self.assertEqual((e["ev"], e["port"]), ("seen", 5432))
        self.assertIn("compose depends_on", e["why"])
        self.assertIn("named in its environment", e["why"])
        self.assertEqual(edge(G, "ct:shop-api-1", "ct:cache-1")["ev"], "declared")
        self.assertEqual(edge(G, "ct:worker-1", "ct:shop-db-1")["ev"], "declared")

    def test_same_network_links_only_toward_databases(self):
        G = demo_graph()
        self.assertEqual(edge(G, "ct:shop-web-1", "ct:shop-db-1")["ev"], "possible")
        self.assertEqual(edge(G, "ct:shop-web-1", "ct:cache-1")["ev"], "possible")
        self.assertIsNone(edge(G, "ct:shop-db-1", "ct:shop-web-1"))
        self.assertIsNone(edge(G, "ct:shop-api-1", "ct:shop-web-1"))  # same network, but not a database: no edge

    def test_tailnet_clients_are_named_after_the_peer(self):
        G = demo_graph()
        n = G["nodes"]["ext:100.64.0.2"]
        self.assertEqual(n["sub"], "tailnet · laptop")
        self.assertEqual(graph.ext_class("203.0.113.9")[0], "Internet")
        self.assertEqual(graph.ext_class("192.168.0.50")[0], "LAN")
        self.assertEqual(graph.ext_class("not an ip")[0], "?")
        self.assertEqual(graph.ext_class("::ffff:192.168.0.7")[0], "LAN")

    def test_a_lan_client_on_a_database_is_flagged_on_the_database(self):
        G = demo_graph()
        db = G["nodes"]["ct:shop-db-1"]
        self.assertTrue(any(lv == "err" and "192.168.0.50" in t for lv, t in db["findings"]))
        self.assertEqual(db["state"], "warn")  # healthy but exposed: attention, not down

    def test_entry_that_leads_to_data_says_through_what(self):
        G = demo_graph()
        f = [t for _, t in G["nodes"]["port:8080/tcp@lan"]["findings"] if t.startswith("leads to data: shop-db-1")]
        self.assertEqual(f, ["leads to data: shop-db-1 (postgres) via shop-web-1 → shop-api-1"])

    def test_a_down_dependency_turns_its_users_yellow_up_the_chain(self):
        def stop_db(cont, net, boot):
            for x in net["links"]["containers"]:
                if x["name"] == "shop-db-1":
                    x.update(state="exited", exit=1)
            for x in cont["containers"]:
                if x["name"] == "shop-db-1":
                    x.update(state="exited", status="Exited (1) 1 minute ago")
        G = demo_graph(change=stop_db)
        self.assertEqual(G["nodes"]["ct:shop-db-1"]["state"], "down")
        self.assertEqual(G["nodes"]["ct:shop-api-1"]["state"], "warn")
        self.assertIn(("warn", "depends on shop-db-1, which is down (via shop-api-1)"), G["nodes"]["ct:shop-web-1"]["findings"])
        self.assertNotEqual(G["nodes"]["ct:blog-app-1"]["state"], "warn")  # another stack: untouched
        self.assertIn("ct:shop-db-1", G["_kids"]["root:impact"])
        rows = labels(G, graph.State(all=True))
        i = rows.index((1, "shop-db-1"))
        below = [lbl for d, lbl in rows[i + 1:i + 6] if d > 1]
        self.assertIn("shop-api-1", below)  # IMPACT walks the dependents
        self.assertIn(":8080/tcp", below)    # up to the entry the LAN uses to reach them

    def test_failed_units_and_what_needs_them(self):
        def fail(cont, net, boot):
            boot.update(failed=["nfs-server.service"], deps={"nfs-server.service": ["backup.service"]})
        G = demo_graph(change=fail)
        self.assertIn("unit:nfs-server.service", G["_kids"]["root:impact"])
        self.assertEqual(edge(G, "unit:backup.service", "unit:nfs-server.service")["ev"], "declared")
        self.assertIn(("warn", "depends on nfs-server.service, which is down"), G["nodes"]["unit:backup.service"]["findings"])

    def test_declared_web_app_with_nothing_listening_is_in_impact(self):
        G = demo_graph()
        self.assertIn("webapp:admin-console", G["_kids"]["root:impact"])
        self.assertEqual(G["nodes"]["webapp:admin-console"]["state"], "down")
        self.assertTrue(any(k == "web app" for k, _ in G["nodes"]["port:8080/tcp@lan"]["facts"]))

    def test_new_port_against_the_baseline_is_an_error_on_the_entry(self):
        cont, net, boot, base = demo.snapshot(now=1_790_000_000)
        base = copy.deepcopy(base)
        del base["ports"]["8080/t:LAN"]
        G = graph.build(cont, net, boot, WEBAPPS, now=1_790_000_000, baseline=base)
        self.assertIn(("err", "NEW since the accepted baseline"), G["nodes"]["port:8080/tcp@lan"]["findings"])

    def test_a_public_client_on_a_lan_port_is_suspicious(self):
        def public(cont, net, boot):
            net["links"]["conns"].append({"from": "ext:203.0.113.9", "to": "proc:sshd", "port": 22, "n": 1, "last": 1_790_000_000})
        G = demo_graph(change=public)
        self.assertTrue(any("203.0.113.9" in t for _, t in G["nodes"]["port:22/tcp@lan"]["findings"]))

    def test_desktop_os_says_container_links_are_not_visible(self):
        for os_name in ("windows", "darwin"):
            G = demo_graph(os_name)
            self.assertTrue(any("Docker Desktop" in n for n in G["notes"]), G["notes"])
            self.assertIsNone(edge(G, "ct:shop-web-1", "ct:shop-api-1") and
                              edge(G, "ct:shop-web-1", "ct:shop-api-1")["ev"] == "seen" or None)

    def test_missing_data_is_said_never_raises(self):
        for cont, net, boot in ((None, None, None), ({}, {}, {}), ({"containers": "x"}, {"listeners": None}, "x"),
                                ({"containers": [{"bad": 1}]}, {"links": {"conns": [{"from": "ct:a"}, None, "x"]}}, None)):
            G = graph.build(cont, net, boot, None)
            graph.rows(G, graph.State(all=True))
            graph.counts(G)
        self.assertIn("network collector not running: no ports, no connections", graph.build(None, None)["notes"])
        net = demo.snapshot()[1]
        del net["links"]
        self.assertTrue(any("restart the collector" in x for x in graph.build(None, net)["notes"]))
        net["disabled"] = ["links"]
        self.assertTrue(any("map = no" in x for x in graph.build(None, net)["notes"]))
        self.assertEqual(graph.rows(graph.build(None, None)), [])

    def test_names_from_the_data_lose_their_control_characters(self):
        def hostile(cont, net, boot):
            net["links"]["conns"].append({"from": "proc:evil\x1b[2J\x07", "to": "ct:shop-web-1", "port": 80, "n": 1})
        G = demo_graph(change=hostile)
        n = G["nodes"]["proc:evil\x1b[2J\x07"]
        self.assertFalse(re.search(r"[\x00-\x1f]", n["label"]))

    def test_a_down_database_turns_the_entries_in_front_of_it_yellow(self):
        def change(name, **how):
            def fn(cont, net, boot):
                for x in net["links"]["containers"] + cont["containers"]:
                    if x["name"] == name:
                        x.update(how)
            return fn
        G = demo_graph(change=change("blog-db-1", state="exited", status="Exited (1) 1 minute ago", exit=1))
        p = G["nodes"]["port:8081/tcp@locale"]
        self.assertEqual(p["state"], "warn")
        self.assertIn(("warn", "leads to blog-db-1, which is down (via blog-app-1)"), p["findings"])
        self.assertEqual(G["nodes"]["root:local"]["state"], "warn")  # all the way up to the zone
        G = demo_graph("windows", change=change("shop-db-1", state="exited", status="Exited (1) 1 minute ago", exit=1))
        p = G["nodes"]["port:8080/tcp@lan"]
        self.assertEqual(p["state"], "warn")
        self.assertIn(("warn", "leads to shop-db-1, which is down (via shop-web-1 → shop-api-1)"), p["findings"])
        G = demo_graph("windows", change=change("blog-app-1", status="Up 1 hour (unhealthy)", health="unhealthy"))
        self.assertIn(("warn", "blog-app-1 behind it is failing"), G["nodes"]["port:8081/tcp@locale"]["findings"])
        self.assertEqual(G["nodes"]["root:local"]["state"], "warn")

    def test_a_failed_unit_that_needs_another_failed_one_is_down_too(self):
        def fail(cont, net, boot):
            boot.update(failed=["a.service", "b.service"], deps={"a.service": ["b.service"], "b.service": []})
        G = demo_graph(change=fail)
        b = G["nodes"]["unit:b.service"]
        self.assertEqual((b["state"], b["sub"]), ("down", "failed unit"))
        self.assertIn("unit:b.service", G["_kids"]["root:impact"])
        self.assertEqual(edge(G, "unit:b.service", "unit:a.service")["ev"], "declared")
        self.assertIn(("warn", "depends on a.service, which is down"), b["findings"])

    def test_entry_owners_are_the_ones_bound_where_the_entry_is(self):
        def linux(cont, net, boot):
            net["listeners"] += [{"proto": "tcp", "addr": "127.0.0.1", "port": 3000, "proc": "node"},
                                 {"proto": "tcp", "addr": "100.64.0.1", "port": 3000, "proc": "grafana"},
                                 {"proto": "tcp", "addr": "100.64.0.1", "port": 6379, "proc": "keydb"}]
        G = demo_graph(change=linux)
        self.assertEqual(G["nodes"]["port:3000/tcp@tailnet"]["owners"], ["proc:grafana"])
        self.assertEqual(G["nodes"]["port:3000/tcp@locale"]["owners"], ["proc:node"])
        self.assertEqual(G["nodes"]["port:6379/tcp@tailnet"]["owners"], ["proc:keydb"])  # cache-1 publishes it on loopback only
        self.assertEqual(G["nodes"]["port:6379/tcp@locale"]["owners"], ["ct:cache-1"])

        def windows(cont, net, boot):
            net["listeners"] += [{"proto": "tcp", "addr": "127.0.0.1", "port": 53, "proc": "localdns", "fw": ["open", "local"]},
                                 {"proto": "tcp", "addr": "192.0.2.10", "port": 53, "proc": "landns", "fw": ["open", "rule x"]}]
        G = demo_graph("windows", change=windows)
        self.assertEqual(G["nodes"]["port:53/tcp@lan"]["owners"], ["proc:landns"])
        self.assertEqual(G["nodes"]["port:53/tcp@locale"]["owners"], ["proc:localdns"])

    def test_every_handler_of_a_serve_port_is_behind_it(self):
        def serve(cont, net, boot):
            net["serve"] = [{"port": 8444, "path": "/webhook", "target": "http://127.0.0.1:5678/webhook", "funnel": True},
                            {"port": 8444, "path": "/", "target": "http://127.0.0.1:8080", "funnel": True},
                            {"port": 8445, "path": "/", "target": "http://192.0.2.50:8080", "funnel": False},
                            {"port": 8446, "path": "/", "target": "http://localhost", "funnel": False}]
            net["listeners"] += [{"proto": "tcp", "addr": "100.64.0.1", "port": p, "proc": "tailscaled"} for p in (8445, 8446)]
            net["listeners"].append({"proto": "tcp", "addr": "127.0.0.1", "port": 80, "proc": "caddy"})
        G = demo_graph(change=serve)
        p = G["nodes"]["port:8444/tcp@internet"]
        self.assertEqual(p["owners"], ["proc:node", "ct:shop-web-1"])
        self.assertEqual(len([k for k, _ in p["facts"] if k == "tailscale funnel"]), 2)
        self.assertEqual(G["nodes"]["port:8445/tcp@tailnet"]["owners"], ["ext:192.0.2.50"])  # another host, not our :8080
        self.assertNotIn("port:8445/tcp@tailnet", G["_kids"]["root:outbound"])
        self.assertEqual(G["nodes"]["port:8446/tcp@tailnet"]["owners"], ["proc:caddy"])  # http without a port: 80

    def test_notes_say_why_container_connections_are_missing(self):
        cont, net, boot, _ = demo.snapshot(now=1_790_000_000)
        why = "container namespaces unreadable (nsenter needs root): container links seen from the host only"
        net["links"].update(conn_source="host", errors=[why])  # Linux, collector not root
        notes = graph.build(cont, net)["notes"]
        self.assertFalse(any("Docker Desktop" in n for n in notes), notes)
        self.assertEqual(sum("namespaces" in n for n in notes), 1, notes)  # the collector's note says it: not twice
        net["links"]["errors"] = []
        self.assertTrue(any("namespaces unreadable" in n for n in graph.build(cont, net)["notes"]))
        bare = {"listeners": [], "os": "linux", "links": {"conn_source": "host", "containers": [], "conns": [], "errors": []}}
        self.assertEqual(graph.build({"containers": []}, bare)["notes"], [])  # no Docker: nothing to say about containers
        absent = {"listeners": [], "absent": ["links"], "errors": {}}
        notes = graph.build(None, absent)["notes"]
        self.assertTrue(any("is installed" in n for n in notes) and not any("restart" in n for n in notes), notes)
        self.assertTrue(any("config.ini" in n for n in graph.build(None, dict(absent, disabled=["links"]))["notes"]))

    def test_dependents_that_could_not_be_read_are_unknown_not_none(self):
        def needed_by(deps):
            G = demo_graph(change=lambda cont, net, boot: boot.update(failed=["x.service"], deps=deps))
            return [v for k, v in G["nodes"]["unit:x.service"]["facts"] if k == "needed by"]
        self.assertEqual(needed_by({}), ["unknown"])  # past the first 20 units, or the query failed
        self.assertEqual(needed_by({"x.service": []}), ["nothing that is installed"])

    def test_a_project_is_as_yellow_as_its_members_turned(self):
        def change(cont, net, boot):
            for x in net["links"]["containers"] + cont["containers"]:
                if x["name"] == "worker-1":  # the shop project is all up...
                    x.update(state="running", status="Up 1 hour", exit=None)
                if x["name"] == "cache-1":  # ...but the cache its api uses is down
                    x.update(state="exited", status="Exited (1) 1 minute ago", exit=1)
        G = demo_graph(change=change)
        self.assertEqual(G["nodes"]["ct:shop-api-1"]["state"], "warn")
        self.assertEqual(G["nodes"]["stack:shop"]["state"], "warn")

    def test_a_public_client_on_another_port_of_the_same_process_does_not_flag_this_entry(self):
        def public(cont, net, boot):
            net["listeners"].append({"proto": "tcp", "addr": "0.0.0.0", "port": 2222, "proc": "sshd"})
            net["links"]["conns"].append({"from": "ext:203.0.113.9", "to": "proc:sshd", "port": 2222, "n": 1, "last": 1_790_000_000})
        G = demo_graph(change=public)
        flagged = [k for k, n in G["nodes"].items() if any("203.0.113.9" in t for _, t in n["findings"])]
        self.assertEqual([k.split("@")[0] for k in flagged], ["port:2222/tcp"])

    def test_declared_web_apps_are_unknown_without_the_listening_ports(self):
        for net in (None, {"errors": {"listeners": "RuntimeError('ss failed')"}}, {"absent": ["listeners"]}):
            G = graph.build(None, net, None, {"shop-web": [8080]})
            n = G["nodes"]["webapp:shop-web"]
            self.assertEqual(n["state"], "unknown")  # never down, never ok
            self.assertEqual(n["sub"], "web app · listening ports unknown")
            self.assertNotIn("webapp:shop-web", G["_kids"].get("root:impact", []))


class Tree(unittest.TestCase):
    def test_roots_open_by_default_the_rest_closed(self):
        G = demo_graph()
        rs = graph.rows(G)
        self.assertEqual({r["depth"] for r in rs}, {0, 1})
        self.assertTrue(all(r["open"] for r in rs if r["depth"] == 0))

    def test_keys_are_stable_short_and_unique(self):
        a, b = graph.rows(demo_graph(), graph.State(all=True)), graph.rows(demo_graph(), graph.State(all=True))
        self.assertEqual([r["key"] for r in a], [r["key"] for r in b])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{10}", r["key"]) for r in a))
        self.assertEqual(len({r["key"] for r in a}), len(a))

    def test_toggle_opens_and_closes_one_branch(self):
        G = demo_graph()
        st = graph.State()
        row = next(r for r in graph.rows(G, st) if G["nodes"][r["node"]]["label"] == ":8080/tcp")
        st2 = st.toggled(row)
        self.assertEqual(st.open, set())  # toggled() is a copy: links are built from it
        self.assertIn(row["key"], st2.open)
        opened = next(r for r in graph.rows(G, st2) if r["key"] == row["key"])
        self.assertTrue(opened["open"])
        st2.toggle(opened)
        self.assertNotIn(row["key"], st2.open)

    def test_a_root_and_an_expanded_all_branch_close_through_shut(self):
        G = demo_graph()
        st = graph.State(all=True)
        rs = graph.rows(G, st)
        root = rs[0]
        st.toggle(root)
        self.assertIn(root["key"], st.shut)
        self.assertEqual([r for r in graph.rows(G, st) if r["depth"] == 0][0]["open"], False)
        st.toggle(graph.rows(G, st)[0])
        self.assertNotIn(root["key"], st.shut)
        st.collapse_all()
        self.assertEqual((st.all, st.open, st.shut), (False, set(), set()))
        st.expand_all()
        self.assertTrue(st.all)

    def test_cycles_stop_and_are_marked(self):
        net = {"listeners": [{"proto": "tcp", "addr": "127.0.0.1", "port": 80, "proc": "a"}],
               "links": {"conns": [{"from": "proc:a", "to": "proc:b", "port": 1, "n": 1},
                                   {"from": "proc:b", "to": "proc:a", "port": 2, "n": 1}]}}
        G = graph.build({"containers": []}, net)
        rs = graph.rows(G, graph.State(all=True))
        self.assertTrue(any(r["cycle"] for r in rs))
        self.assertLess(len(rs), 40)
        self.assertEqual(graph.parts(G, next(r for r in rs if r["cycle"]))["toggle"], "↻")

    def test_problems_only_keeps_the_paths_that_lead_somewhere_bad(self):
        G = demo_graph()
        names = [lbl for _, lbl in labels(G, graph.State(all=True, only=True))]
        self.assertIn(":5432/tcp", names)
        self.assertIn("worker-1", names)
        self.assertNotIn("LOCAL", names)       # nothing wrong only reachable from here
        self.assertNotIn(":22/tcp", names)

    def test_limit_truncates_and_says_so(self):
        rs = graph.rows(demo_graph(), graph.State(all=True), limit=5)
        self.assertEqual((len(rs), rs.truncated), (5, True))

    def test_fit_open_fills_the_room_without_going_over(self):
        G = demo_graph()
        base = len(graph.rows(G))
        for h in (base, base + 5, 40, 200):
            n = len(graph.rows(G, graph.State(open=graph.fit_open(G, h))))
            self.assertLessEqual(n, h)
        self.assertEqual(len(graph.rows(G, graph.State(open=graph.fit_open(G, 500)))), len(graph.rows(G, graph.State(all=True))))

    def test_port_rows_show_clients_then_what_is_behind(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        i = next(i for i, r in enumerate(rs) if G["nodes"][r["node"]]["label"] == ":8080/tcp")
        first, second = graph.parts(G, rs[i + 1]), graph.parts(G, rs[i + 2])
        self.assertEqual((first["arrow"], first["label"], first["sub"]), ("◄━━", "100.64.0.2", "tailnet · laptop"))
        self.assertEqual((second["arrow"], second["label"], second["port"]), ("━━►", "shop-api-1", ":3000"))
        self.assertEqual(graph.parts(G, rs[i])["owner"], "shop-web-1")

    def test_tree_glyphs_follow_the_siblings(self):
        G = demo_graph()
        rs = graph.rows(G, graph.State(all=True))
        trees = [graph.parts(G, r)["tree"] for r in rs]
        self.assertEqual(trees[0], "")
        self.assertTrue(all(t.endswith(("├─ ", "└─ ")) for t in trees[1:] if t))
        last_child = [graph.parts(G, r)["tree"] for r in rs if r["depth"] == 1 and r["last"]]
        self.assertTrue(all(t == "└─ " for t in last_child))

    def test_details_cover_the_entry_and_what_is_behind_it(self):
        G = demo_graph()
        d = graph.details(G, "port:8080/tcp@lan")
        keys = [k for k, _, _ in d]
        self.assertEqual(d[0][0], "entry port")
        self.assertIn("behind it", keys)
        self.assertTrue(any(k.strip() == "image" and v == "example/shop-web:1.4" for k, v, _ in d))
        self.assertTrue(any(k == "► to" and v.startswith("shop-api-1 :3000  seen now") for k, v, _ in d))
        self.assertEqual(graph.details(G, "ct:nope")[0][0], "gone")
        db = graph.details(G, "ct:shop-db-1")
        self.assertTrue(any(k == "exposed as" for k, _, _ in db))

    def test_problems_only_keeps_a_path_through_a_cycle_whatever_is_asked_first(self):
        def loop(cont, net, boot):  # node → api ⇄ web ← blog-app-1, and api names shop-db-1 (exposed: it needs attention)
            L = net["links"]
            L["containers"] += [demo._link("api", "x", "api", "example/api", {"x_default": ""}, [3000], env=["shop-db-1"]),
                                demo._link("web", "x", "web", "example/web", {"x_default": ""}, [80])]
            L["conns"] += [{"from": a, "to": b, "port": p, "n": 1, "last": 1_790_000_000}
                           for a, b, p in (("proc:node", "ct:api", 3000), ("ct:api", "ct:web", 80), ("ct:web", "ct:api", 3000),
                                           ("ct:blog-app-1", "ct:web", 80))]
        path = graph.path_key(("root:local", "port:8081/tcp@locale", "ct:web", "ct:api", "ct:shop-db-1"))
        G = demo_graph(change=loop)
        self.assertEqual(G["nodes"]["ct:shop-db-1"]["state"], "warn")
        self.assertIn(path, [r["key"] for r in graph.rows(G, graph.State(all=True))])
        for first in (None, "ct:web", "ct:api", "ct:blog-app-1", "port:8081/tcp@locale"):
            G = demo_graph(change=loop)
            if first:
                graph._bad_below(G, first, "fwd")
            keys = [r["key"] for r in graph.rows(G, graph.State(all=True, only=True))]
            self.assertIn(path, keys, first)  # :8081 → blog-app-1 → web → api → shop-db-1
            self.assertTrue(all(graph._bad_below(G, n, "fwd") for n in ("ct:web", "ct:blog-app-1", "ct:api")), first)

    def test_a_port_owner_met_again_below_its_port_is_a_cycle(self):
        def back(cont, net, boot):  # shop-api-1 calls back the shop-web-1 that owns :8080
            net["links"]["conns"].append({"from": "ct:shop-api-1", "to": "ct:shop-web-1", "port": 80, "n": 1,
                                          "last": 1_790_000_000})
        G = demo_graph(change=back)
        rs = graph.rows(G, graph.State(all=True))
        path = ("root:lan", "port:8080/tcp@lan", "ct:shop-api-1", "ct:shop-web-1")
        row = rs[graph.find(rs, graph.path_key(path))]
        self.assertEqual((row["cycle"], row["kids"], graph.parts(G, row)["toggle"]), (True, 0, "↻"))
        self.assertIsNone(graph.find(rs, graph.path_key(path + ("ct:shop-api-1",))))  # not walked a second time
        stacks = graph.path_key(("root:stacks", "stack:shop", "ct:shop-api-1", "ct:shop-web-1"))
        self.assertFalse(rs[graph.find(rs, stacks)]["cycle"])  # no port above it there: shop-web-1 is walked

    def test_problems_only_does_not_depend_on_how_deep_a_node_was_met_first(self):
        names = ["c%d" % i for i in range(14)]

        def chain(cont, net, boot):  # node → c0 → … → c13 → shop-db-1: c10 is 12 levels deep under LOCAL, 2 under STACKS
            L = net["links"]
            L["containers"] += [demo._link(n, "chain", n, "example/x", {"chain_default": ""}, [80],
                                           env=["shop-db-1"] if n == names[-1] else []) for n in names]
            L["conns"] += [{"from": a, "to": b, "port": 80, "n": 1, "last": 1_790_000_000}
                           for a, b in zip(["proc:node"] + ["ct:" + n for n in names[:-1]], ["ct:" + n for n in names])]
        G = demo_graph(change=chain)
        keys = [r["key"] for r in graph.rows(G, graph.State(all=True, only=True))]  # LOCAL is walked before STACKS
        path = ("root:stacks", "stack:chain", "ct:c10", "ct:c11", "ct:c12", "ct:c13", "ct:shop-db-1")
        self.assertIn(graph.path_key(path), keys)

    def test_malformed_published_ports_are_skipped(self):
        cont, net, boot, base = demo.snapshot(now=1_790_000_000)
        web = next(x for x in net["links"]["containers"] if x["name"] == "shop-web-1")
        web["ports"] = [None, "8080", {"p": 8080, "c": "\u00b2/tcp", "s": "*"}] + web["ports"]
        G = graph.build(cont, net, boot, WEBAPPS)
        self.assertIn(80, graph._port_numbers(G, G["nodes"]["port:8080/tcp@lan"]))
        st = graph.State()
        st.expand_all()
        self.assertTrue(graph.rows(G, st))


if __name__ == "__main__":
    unittest.main()
