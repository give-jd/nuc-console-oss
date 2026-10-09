"""The overview stays on one screen when everything fits without the blank lines between the sections.

A busy host (22 ufw rules, 23 containers, 24 listeners; documentation addresses only) on a console a few rows short of the
airy layout: the overview used to leave a "+N more" cut and rotate to a second (Details) screen although the whole
content fits once the blank separators go. The fixture is built by hand: nothing here reads the machine running the tests.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # golden.py
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import collector  # noqa: E402
import golden  # noqa: E402  (FrozenWorld: the default configuration, whatever another test left in render.CFG)
import render  # noqa: E402
import ansi  # noqa: E402

SM = {"cpu": {"cpu0": 0.1}, "thermal": {"cpu": (87.0, 105.0), "nvme": (33.0, 85.85), "throttle_s": 469.0, "recent": 0, "clk": (2.6, 4.9)}}


def fx():
    rules="".join(f"{8000+i}/tcp                   ALLOW IN    192.0.2.0/24\n" for i in range(20))
    ufw=collector.parse_ufw("Status: active\nLogging: on (low)\nDefault: deny (incoming), allow (outgoing), deny (routed)\nNew profiles: skip\n\nTo                         Action      From\n--                         ------      ----\n"+rules+"22/tcp                     ALLOW IN    192.0.2.0/24\nAnywhere on tailscale0     ALLOW IN    Anywhere\n")
    now=time.time()
    cont={"ts":now,"containers":[{"name":f"app{i}-1","status":"Up 3 days","state":"running","project":f"p{i%4}","ports":[{"p":9100+i,"s":"lo"}],"mem":100*2**20} for i in range(22)]+[{"name":"db-1","status":"Exited (1) 2 hours ago","state":"exited","project":"p0","ports":[],"mem":None}]}
    lis=[{"proto":"tcp","addr":"127.0.0.1","port":9100+i,"proc":f"proc{i}"} for i in range(22)]+[{"proto":"tcp","addr":"192.0.2.5","port":5432,"proc":"docker-proxy"},{"proto":"tcp","addr":"0.0.0.0","port":22,"proc":"sshd"}]
    net={"ts":now,"errors":{},"ufw":ufw,"docker_user":[],"f2b":{"jails":[{"name":"sshd","banned":0,"ips":[]}]},"drops":{"n":5,"src":[["198.51.100.9",5]],"dpt":[["23",5]]},"serve":[],"listeners":lis}
    boot={"ts":now,"errors":{},"kernel":"7.0","btime":int(now)-86400,"analyze":{"parts":{"firmware":6.7,"loader":6.5,"kernel":0.8,"initrd":1.3,"userspace":16.0},"total":31.3},"blame":[{"unit":f"svc{i}.service","s":20.0-i} for i in range(12)],"failed":[],"enabled":[],"journal":{"err":3,"warn":5,"capped":False,"top":[]},"containers":[]}
    return cont, net, boot


class OverviewSingleScreen(unittest.TestCase):
    def setUp(self):
        world = golden.FrozenWorld(cfg={"spacing": 1, "details": True})
        world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)

    def test_busy_host_fits_one_screen_without_details(self):
        cont, net, boot = fx()
        for w, h in ((120, 55), (160, 59), (240, 50)):
            det = []
            lines = render.page_overview(SM, cont, net, boot, w - 1, h - 2, details=det, now=time.time())
            self.assertEqual(det, [], f"{w}x{h}: a second screen for what the first one can hold")
            self.assertLessEqual(len(lines), h - 2)
            text = ansi.ANSI.sub("", "\n".join(lines))
            self.assertNotRegex(text, r"\+\d+ (more|rules)", f"{w}x{h}: something is still cut")
            self.assertIn("1 stopped", text, f"{w}x{h}: the failing container is still said")

    def test_slides_are_one(self):
        cont, net, boot = fx()
        sl = render.slides(SM, cont, net, 119, 53, boot, baseline=False, mode="overview")
        self.assertEqual(len(sl), 1)


if __name__ == "__main__":
    unittest.main()
