"""The CPU screen as components (screens.py): the model, what the console draws from it at several sizes, and what the web draws from the same
nodes (no <pre>, sortable heads that are links with the keymap's keys, rows that are links, escaping, unknown values as '?').

The console's bytes are held by tests/golden and by the differential check the pull request reports; here the structure is.
"""
import copy
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import ansi  # noqa: E402
import demo  # noqa: E402
import htmlview  # noqa: E402
import render  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402
import webjs  # noqa: E402

NOW = 1_790_000_000
HOSTILE = '<script>alert(1)</script>"\'&'


class Clock(object):
    time = staticmethod(lambda: NOW)
    sleep = staticmethod(lambda s: None)
    strftime = staticmethod(lambda fmt, t=None: time.strftime(fmt, time.gmtime(NOW) if t is None else t))
    localtime = staticmethod(lambda t=None: time.gmtime(NOW if t is None else t))


class Case(unittest.TestCase):
    def setUp(self):
        self.saved = (render.time, demo.time, render.DEMO, render.DEMO_OS, render.SENSORS)
        render.time = demo.time = Clock
        render.SENSORS = "/nonexistent/sensors.json"

    def tearDown(self):
        render.time, demo.time, render.DEMO, render.DEMO_OS, render.SENSORS = self.saved

    def data(self, os_name=None):
        render.DEMO, render.DEMO_OS = True, os_name
        return render.CpuFeed().read()

    def ctx(self, os_name="linux"):
        return screens.CpuCtx(os_name, None, False, Clock)

    def web(self, d, sort="cpu", cur=None, links=True):
        lk = screens.CpuLinks(lambda by: "/?view=cpu&sort=" + by, lambda pid: f"/?view=cpu&sel={pid}") if links else None
        sc = screens.cpu_view(d, self.ctx(), screens.WEB_W, 10 ** 4, sort, cur, cur is not None, 0, lk)
        return "".join(htmlview.html(n) for n in sc.nodes)


class Model(Case):
    def test_it_imports_nothing_of_the_renderer(self):
        src = open(os.path.join(os.path.dirname(screens.__file__), "screens.py"), encoding="utf-8").read()
        self.assertNotIn("import render", src)
        self.assertIn("# ---- CPU", src)

    def test_the_view_is_components_and_the_numbers_the_loop_needs(self):
        d = self.data()
        sc = screens.cpu_view(d, self.ctx(), 119, 31, "cpu", d["procs"]["procs"][0]["pid"], True, 0)
        self.assertTrue(all(isinstance(n, (ui.Group, ui.Split)) for n in sc.nodes))
        self.assertEqual(len(sc.rows), len(d["procs"]["procs"]))
        self.assertGreater(sc.vis, 0)
        self.assertEqual(len(sc.pids), sc.vis)
        self.assertGreaterEqual(sc.top, 0)

    def test_the_process_table_has_priority_columns_and_the_sorted_one_marked(self):
        d = self.data()
        for sort, key, mark in (("cpu", "cpu", "desc"), ("pid", "pid", "asc"), ("mem", "mem_pct", "desc"), ("time", "time", "desc"), ("user", "user", "asc")):
            sc = screens.cpu_view(d, self.ctx(), 200, 50, sort, None, False, 0)
            table = next(c for g in sc.nodes for c in _walk(g) if isinstance(c, ui.Table))
            cols = {c.key: c for c in table.cols}
            self.assertEqual(cols[key].sort, mark, sort)
            self.assertEqual([k for k, c in cols.items() if c.sort], [key] + (["mem"] if sort == "mem" else []), sort)
            self.assertEqual(cols["cpu"].prio, 0)
            self.assertGreater(cols["time"].prio, cols["mem_pct"].prio)

    def test_each_sort_has_the_keys_of_the_keymap(self):
        row = next(r for r in ui.KEYMAP if r.scope == "cpu" and r.action == "sort")
        for sort in screens.CPU_SORTS:
            keys = screens.sort_keys(sort).split()
            self.assertEqual(len(keys), 2)
            for k in keys:
                self.assertIn(k, row.keys)
                self.assertEqual(screens.CPU_SORT_KEYS[k.lower()], sort)

    def test_the_selected_row_is_the_cursor_and_unknown_values_are_question_marks(self):
        d = copy.deepcopy(self.data())
        d["procs"]["procs"][0].update(cpu=None, mem=None, time=None, user=None, state=None)
        pid = d["procs"]["procs"][0]["pid"]
        sc = screens.cpu_view(d, self.ctx(), 200, 50, "pid", pid, False, 0)
        table = next(c for g in sc.nodes for c in _walk(g) if isinstance(c, ui.Table))
        sel = [r for r in table.rows if r.tone == "sel"]
        self.assertEqual([r.key for r in sel], [str(pid)])
        text = [ansi.ANSI.sub("", x) for x in ansi.render(table, 200)[0]]
        row = next(x for x in text if x.split()[:1] == [str(pid)])
        self.assertGreaterEqual(row.split()[1:10].count("?"), 5)


def _walk(node):
    yield node
    for ch in list(getattr(node, "children", ())) + list(getattr(node, "left", ())) + list(getattr(node, "right", ())):
        yield from _walk(ch)


class Console(Case):
    def test_it_fills_its_screen_and_nothing_more_at_several_sizes(self):
        d = self.data()
        for w, h in ((40, 12), (79, 24), (120, 33), (200, 50), (226, 50)):
            body = render.cpu_view(d, w - 1, h - 2)[0]
            self.assertEqual(len(body), h - 2, (w, h))
            for line in body:
                self.assertLessEqual(len(ansi.ANSI.sub("", line)), w - 1)

    def test_the_header_the_cores_the_temperatures_and_the_table_are_all_there(self):
        txt = "\n".join(ansi.ANSI.sub("", x) for x in render.cpu_view(self.data(), 199, 48)[0])
        for want in ("── CPU ", "sockets 1", "ALL ", "── TEMPERATURES ", "PKG ", "── PROCESSES ", "PID USER", "CPU%▼", "NAME"):
            self.assertIn(want, txt)

    def test_the_cursor_row_is_reversed_and_the_details_sit_beside_a_wide_table(self):
        d = self.data()
        pid = d["procs"]["procs"][3]["pid"]
        wide = render.cpu_view(d, 199, 48, "cpu", pid, True)[0]
        self.assertTrue(any("\x1b[7m" in x for x in wide))
        self.assertTrue(any(" │ " in x and "parent" in x for x in wide) or any(" │ " in x for x in wide))
        narrow = render.cpu_view(d, 99, 48, "cpu", pid, True)[0]
        self.assertFalse(any(" │ " in x for x in narrow))
        self.assertTrue(any("PROCESS %d" % pid in ansi.ANSI.sub("", x) for x in narrow))

    def test_a_short_screen_counts_what_it_left_out(self):
        txt = "\n".join(ansi.ANSI.sub("", x) for x in render.cpu_view(self.data(), 78, 22)[0])
        self.assertIn("more processes", txt)

    def test_windows_and_macos_demos_draw(self):
        for os_name in ("windows", "darwin"):
            body = render.cpu_view(self.data(os_name), 199, 48)[0]
            self.assertEqual(len(body), 48)


class Web(Case):
    def test_it_is_html_not_a_pre_and_the_heads_are_links_with_the_keys(self):
        page = self.web(self.data(), "mem")
        self.assertNotIn("<pre", page)
        for sort, keys in (("cpu", "p P"), ("mem", "m M"), ("time", "t T"), ("pid", "n N"), ("user", "u U")):
            self.assertIn(f'<a href="/?view=cpu&amp;sort={sort}" data-key="{keys}">', page)
        self.assertEqual(page.count('aria-sort="descending"'), 1)
        self.assertIn('<th class="r n p1 sorted" aria-sort="descending" scope="col"><a href="/?view=cpu&amp;sort=mem" data-key="m M">MEM%</a></th>', page)

    def test_rows_are_links_and_the_selected_one_is_marked(self):
        d = self.data()
        pid = d["procs"]["procs"][2]["pid"]
        page = self.web(d, "cpu", pid)
        self.assertIn(f'data-key="{pid}" data-row aria-current="true"', page)
        self.assertEqual(page.count("aria-current"), 1)
        self.assertEqual(page.count(" data-row"), len(d["procs"]["procs"]))
        self.assertIn(f'<a href="/?view=cpu&amp;sel={pid}">', page)
        self.assertIn(f"PROCESS {pid}", page)
        self.assertIn('<dl class="kv">', page)
        self.assertIn('<div class="split">', page)

    def test_every_process_is_listed_and_the_cores_are_a_grid_of_meters(self):
        d = self.data()
        page = self.web(d)
        self.assertEqual(page.count("data-row"), len(d["procs"]["procs"]))
        self.assertEqual(page.count('<ul class="grid">'), 1)
        n = len(d["cpu"]["usage"]["cores"])
        grid = re.search(r'<ul class="grid">(.*?)</ul>', page, re.S).group(1)
        self.assertEqual(grid.count("<li>"), n)
        self.assertGreaterEqual(page.count('class="meter"'), n + 1)
        self.assertIn('data-kpi="busy"', page)

    def test_hostile_names_are_escaped(self):
        d = copy.deepcopy(self.data())
        p = d["procs"]["procs"][0]
        p.update(name=HOSTILE, user=HOSTILE)
        d["cpu"]["model"] = HOSTILE
        d["extra"]["notes"].append(("warn", HOSTILE))
        page = self.web(d, "cpu", p["pid"])
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('"onmouseover', page)

    def test_unknown_values_are_question_marks(self):
        d = copy.deepcopy(self.data())
        d["cpu"].update(load=None, uptime=None, freq={}, temps={})
        for p in d["procs"]["procs"]:
            p.update(cpu=None, mem=None)
        page = self.web(d)
        self.assertRegex(page, r'data-kpi="load"[^>]*><span class="sym">\?</span><span class="lbl">load</span><span class="n">\?</span>')
        self.assertIn('data-kpi="up"', page)
        self.assertIn("no package temperature", page)
        self.assertIn(">?<", page)

    def test_only_what_a_fragment_may_carry(self):
        page = self.web(self.data(), "time", self.data()["procs"]["procs"][1]["pid"])
        tags = set(re.findall(r"<([a-z0-9]+)[ >/]", page))
        self.assertLessEqual(tags, set(webjs.FRAG_TAGS), tags - set(webjs.FRAG_TAGS))
        attrs = set(re.findall(r' ([a-zA-Z-]+)="', page)) | set(re.findall(r" (data-row|open)[ >]", page))
        self.assertLessEqual(attrs, set(webjs.FRAG_ATTRS), attrs - set(webjs.FRAG_ATTRS))
        self.assertNotIn("style=", page)


if __name__ == "__main__":
    unittest.main()
