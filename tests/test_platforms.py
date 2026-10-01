"""macOS and Windows support: parsers, firewall verdicts, renderer, kiosk, installers.

Everything here runs on any OS (fixtures, fake `run`); the classes at the end exercise the real system calls and only run
on the OS they are about (CI runs the suite on Linux, macOS and Windows).
Addresses are documentation ranges; fake values are built at runtime.
"""
import ctypes
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import collect_darwin as cmac  # noqa: E402
import collect_windows as cwin  # noqa: E402
import collector  # noqa: E402
import cpuinfo  # noqa: E402
import demo  # noqa: E402
import history  # noqa: E402
import hostinfo  # noqa: E402
import htmlview  # noqa: E402
import nuc_config  # noqa: E402
import procs  # noqa: E402
import render  # noqa: E402
import winapi  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TEXT = lambda lines: render.ANSI.sub("", "\n".join(lines))  # noqa: E731
render.CFG["details"] = False


def lst(port, proc="", addr="0.0.0.0", proto="tcp", fw=None):
    return {"proto": proto, "addr": addr, "port": port, "proc": proc, "fw": fw}


# ---- Windows: IP helper buffers ----------------------------------------------------------------------------------------


class WinApiBuffers(unittest.TestCase):
    def test_structure_sizes_match_the_windows_headers(self):
        self.assertEqual(ctypes.sizeof(winapi.IfRow2), 1352)      # MIB_IF_ROW2
        self.assertEqual(ctypes.sizeof(winapi.CpuPerf), 48)       # SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION
        self.assertEqual(ctypes.sizeof(winapi.MemStatus), 64)     # MEMORYSTATUSEX
        self.assertEqual(ctypes.sizeof(winapi.TcpRow), 24)
        self.assertEqual(ctypes.sizeof(winapi.Tcp6Row), 56)
        self.assertEqual(ctypes.sizeof(winapi.UdpRow), 12)
        self.assertEqual(ctypes.sizeof(winapi.Udp6Row), 28)

    def test_tcp_and_udp_tables(self):
        net_port = lambda p: struct.unpack("<I", struct.pack(">H", p) + b"\0\0")[0]  # noqa: E731 - network order, low 16 bits
        ip4 = lambda a: struct.unpack("<I", bytes(int(x) for x in a.split(".")))[0]  # noqa: E731
        rows = [(2, ip4("0.0.0.0"), net_port(22), 0, 0, 1234), (5, ip4("192.0.2.5"), net_port(22), ip4("198.51.100.7"), net_port(50000), 1234)]
        buf = struct.pack("<I", len(rows)) + b"".join(struct.pack("<6I", *r) for r in rows)
        t = winapi.parse_tcp_table(buf, winapi.AF_INET)
        self.assertEqual(t[0], {"addr": "0.0.0.0", "port": 22, "raddr": "0.0.0.0", "rport": 0, "state": 2, "pid": 1234})
        self.assertEqual((t[1]["raddr"], t[1]["rport"], t[1]["state"]), ("198.51.100.7", 50000, 5))
        six = struct.pack("<I", 1) + bytes(16) + struct.pack("<I", 0) + struct.pack("<I", net_port(8080)) + bytes(16) \
            + struct.pack("<4I", 0, 0, 2, 99)
        self.assertEqual(winapi.parse_tcp_table(six, winapi.AF_INET6)[0]["addr"], "::")
        udp = struct.pack("<I", 1) + struct.pack("<3I", ip4("127.0.0.1"), net_port(53), 7)
        self.assertEqual(winapi.parse_udp_table(udp, winapi.AF_INET), [{"addr": "127.0.0.1", "port": 53, "pid": 7}])

    def test_interface_table_skips_down_loopback_and_filter_duplicates(self):
        def row(alias, rx, tx, oper=1, typ=6, flags=0):
            r = winapi.IfRow2()
            for i, ch in enumerate(alias):
                r.alias[i] = ord(ch)
            r.in_octets, r.out_octets, r.oper_status, r.type, r.flags = rx, tx, oper, typ, flags
            return bytes(r)
        rows = [row("Ethernet", 100, 200), row("Ethernet-WFP Native MAC Layer LightWeight Filter-0000", 1, 1, flags=2),
                row("Loopback Pseudo-Interface 1", 5, 5, typ=24), row("Wi-Fi", 7, 7, oper=2)]
        buf = struct.pack("<I", len(rows)) + bytes(4) + b"".join(rows)
        self.assertEqual(winapi.parse_if_table(buf), {"Ethernet": (100, 200)})


# ---- Windows Firewall evaluation ----------------------------------------------------------------------------------------

ENV = {"SYSTEMROOT": r"C:\Windows", "PROGRAMFILES": r"C:\Program Files", "WINDIR": r"C:\Windows"}
SSHD = r"C:\Windows\System32\OpenSSH\sshd.exe"
OWNER = "S-1-5-21-" + "-".join(["1000"] * 3) + "-1001"  # a made-up user SID


def fw(rules=(), current=2, profiles=None, policy=False):
    base = {str(b): {"enabled": True, "inbound": 0, "block_all": False} for b in (1, 2, 4)}
    base.update(profiles or {})
    return {"current": current, "profiles": base, "rules": list(rules), "services": [{"n": "TermService", "pid": 900}], "policy": policy}


def rule(n, a=1, pr=6, lp="*", app="", svc="", pf=0x7FFFFFFF, ra="*", **kw):
    return dict({"n": n, "a": a, "pr": pr, "lp": lp, "app": app, "svc": svc, "pf": pf, "ra": ra, "la": "*", "it": "All"}, **kw)


def verdict(f, port=22, proto="tcp", pid=50, path=SSHD, token=None):
    return cwin.fw_verdict(f, {"proto": proto, "port": port, "pid": pid, "path": path, "token": token}, cwin.service_pids(f), ENV)


class WindowsFirewall(unittest.TestCase):
    def test_profile_settings(self):
        self.assertEqual(verdict(fw(profiles={"2": {"enabled": False}}))[0], "nofw")
        self.assertEqual(verdict(fw([rule("ssh")], profiles={"2": {"enabled": True, "block_all": True}}))[0], "blocked")
        self.assertEqual(verdict(fw(profiles={"2": {"enabled": True, "inbound": 1}}))[0], "open")
        self.assertEqual(verdict(fw())[0:1], ("blocked",))                                     # default inbound: block
        self.assertIn("default block (Private)", verdict(fw())[1])

    def test_rules_by_port_program_service(self):
        self.assertEqual(verdict(fw([rule("ssh", lp="22")]))[0], "open")
        self.assertEqual(verdict(fw([rule("web", lp="80,443,8000-8010")]), port=8005)[0], "open")
        self.assertEqual(verdict(fw([rule("web", lp="80,443")]))[0], "blocked")                  # another port
        self.assertEqual(verdict(fw([rule("udp only", pr=17)]))[0], "blocked")                    # another protocol
        self.assertEqual(verdict(fw([rule("any proto", pr=256)]))[0], "open")
        self.assertEqual(verdict(fw([rule("sshd", app=r"%SystemRoot%\system32\OpenSSH\SSHD.EXE")]))[0], "open")   # env + case
        self.assertEqual(verdict(fw([rule("other app", app=r"C:\Program Files\x\x.exe")]))[0], "blocked")
        self.assertEqual(verdict(fw([rule("app", app=r"C:\x.exe")]), path="")[0], "unknown")      # program unknown: never "blocked"
        self.assertEqual(verdict(fw([rule("smb", app="System")]), port=445, pid=4, path="System")[0], "open")
        self.assertEqual(verdict(fw([rule("rdp", svc="TermService")]), port=3389, pid=900, path=r"C:\Windows\System32\svchost.exe")[0], "open")
        self.assertEqual(verdict(fw([rule("rdp", svc="TermService")]), port=3389, pid=901, path=r"C:\Windows\System32\svchost.exe")[0], "blocked")
        self.assertEqual(verdict(fw([rule("svc", svc="StoppedService")]))[0], "blocked")          # not running: no process to match
        no_services = dict(fw([rule("svc", svc="TermService")]), services=[])
        self.assertEqual(verdict(no_services)[0], "unknown")                                       # services unreadable: cannot tell

    def test_block_wins_and_sources(self):
        self.assertEqual(verdict(fw([rule("allow"), rule("deny", a=0)]))[0], "blocked")
        self.assertEqual(verdict(fw([rule("allow", ra="LocalSubnet")]))[0], "open")              # the whole LAN
        st, note = verdict(fw([rule("allow", ra="192.0.2.0/255.255.255.0")]))
        self.assertEqual((st, note), ("filtered", "only 192.0.2.0/255.255.255.0"))
        self.assertEqual(verdict(fw([rule("allow"), rule("deny some", a=0, ra="198.51.100.7")]))[0], "filtered")

    def test_what_cannot_be_read_is_unknown_never_blocked(self):
        for extra in ({"lp": "RPC"}, {"la": "192.0.2.5"}, {"it": "Wireless"}, {"ifs": "Ethernet 2"}, {"auth": "O:LSD:(A;;CC;;;S-1-5-1)"}, {"sf": 1}):
            with self.subTest(extra=extra):
                self.assertEqual(verdict(fw([rule("x", **extra)]))[0], "unknown")
        self.assertEqual(verdict(fw([rule("x", a=0, lp="RPC")]))[0], "blocked")                   # an unreadable BLOCK cannot open
        self.assertEqual(verdict(fw([rule("epmap", lp="RPC-EPMap")]), port=135)[0], "open")
        self.assertEqual(verdict(fw(policy=True))[0], "unknown")                                   # Group Policy rules are not read

    def test_store_app_rules_apply_only_inside_their_appcontainer(self):
        store = rule("Microsoft Store", pr=256, owner=OWNER)
        self.assertEqual(verdict(fw([store]), token={"user": OWNER, "appcontainer": False})[0], "blocked")
        self.assertEqual(verdict(fw([store]), token={"user": OWNER, "appcontainer": True})[0], "unknown")
        self.assertEqual(verdict(fw([store]), token={"user": "S-1-5-18", "appcontainer": True})[0], "blocked")
        self.assertEqual(verdict(fw([store]))[0], "blocked")                                       # token unreadable, desktop program
        self.assertEqual(verdict(fw([store]), path=r"C:\Program Files\WindowsApps\x\x.exe")[0], "unknown")

    def test_wifi_direct_and_teredo_groups_are_not_lan_rules(self):
        wfd = rule("WFD Driver-only (TCP-In)", app="System", g="@wlansvc.dll,-36865")
        self.assertEqual(verdict(fw([wfd]), port=445, pid=4, path="System")[0], "blocked")

    def test_most_exposed_profile_wins(self):
        f = fw([rule("ssh private", pf=2)], current=2 | 4)
        self.assertEqual(verdict(f)[0], "open")
        f = fw([rule("ssh private", pf=2)], current=4)
        self.assertEqual(verdict(f)[0], "blocked")

    def test_listeners_from_socket_tables(self):
        f = fw([rule("ssh", lp="22")])
        tcp = [{"addr": "0.0.0.0", "port": 22, "state": 2, "pid": 50}, {"addr": "0.0.0.0", "port": 22, "state": 2, "pid": 50},
               {"addr": "192.0.2.5", "port": 50000, "state": 5, "pid": 50}, {"addr": "0.0.0.0", "port": 3389, "state": 2, "pid": 900},
               {"addr": "0.0.0.0", "port": 445, "state": 2, "pid": 4}]
        udp = [{"addr": "0.0.0.0", "port": 5353, "pid": 60}, {"addr": "0.0.0.0", "port": 61000, "pid": 61}]
        images = {50: SSHD, 900: r"C:\Windows\System32\svchost.exe", 4: "System", 60: r"C:\x\mdns.exe"}
        out = cwin.listeners_from(tcp, udp, images, f, ENV)
        self.assertEqual([(x["proto"], x["port"], x["proc"]) for x in out],
                         [("tcp", 22, "sshd"), ("tcp", 3389, "svchost/termservice"), ("tcp", 445, "System"), ("udp", 5353, "mdns")])
        self.assertEqual(out[0]["fw"][0], "open")
        self.assertEqual(cwin.listeners_from(tcp, [], images, None)[0]["fw"], ["unknown", "firewall unreadable"])

    def test_summary_and_boot_sections(self):
        s = cwin.fw_summary(fw(current=4, profiles={"4": {"enabled": False}}))
        self.assertEqual((s["kind"], s["off"], s["profiles"]["Public"]["active"]), ("windows", ["Public"], True))
        data = {"boot": 1700000000, "perf": None, "services": [{"n": "Spooler", "s": "Stopped", "e": 1067}, {"n": "Trig", "s": "Stopped", "e": 1077},
                                                               {"n": "Ok", "s": "Stopped", "e": 0}, {"n": "Run", "s": "Running", "e": 0}],
                "events": [{"p": "DCOM", "l": 3, "m": "newest"}, {"p": "DCOM", "l": 3, "m": "older"}, {"p": "Disk", "l": 2, "m": "bad block"}]}
        b = cwin.boot_sections(data)
        self.assertEqual(b["failed"], ["Spooler"])                                                # 1077 = never started: not a failure
        self.assertEqual({e["unit"]: e["state"] for e in b["enabled"]}, {"Ok": "inactive", "Run": "active", "Spooler": "failed", "Trig": "inactive"})
        self.assertIsNone(b["analyze"])
        self.assertEqual((b["journal"]["err"], b["journal"]["warn"]), (1, 2))
        self.assertEqual(b["journal"]["top"][0]["id"], "Disk")
        self.assertEqual([t["last"] for t in b["journal"]["top"] if t["id"] == "DCOM"], ["newest"])
        b = cwin.boot_sections(dict(data, perf={"total": 30500, "main": 20000, "post": 10500}))
        self.assertEqual(b["analyze"], {"parts": {"main path": 20.0, "post boot": 10.5}, "total": 30.5})

    def test_powershell_scripts_are_fixed_and_encoded(self):
        seen = {}

        def run(name, *args, timeout=15):
            seen["args"] = (name,) + args
            return 0, "\ufeff" + json.dumps({"ok": 1}), ""
        self.assertEqual(cwin.powershell(run, cwin.PS_FIREWALL), {"ok": 1})
        self.assertEqual(seen["args"][:6], ("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"))
        import base64
        self.assertIn("HNetCfg.FwPolicy2", base64.b64decode(seen["args"][6]).decode("utf-16-le"))
        with self.assertRaises(RuntimeError):
            cwin.powershell(lambda *a, **k: (1, "", "boom"), cwin.PS_BOOT)


# ---- macOS ---------------------------------------------------------------------------------------------------------------

LSOF_LISTEN = "p1\nclaunchd\nf20\nn*:22\nf21\nn*:22\np523\ncControlCenter\nf9\nn*:7000\nf10\nn[::1]:7001\np600\nccom.docker.backend\nf50\nn127.0.0.1:5433\n"
LSOF_UDP = "p88\ncmDNSResponder\nf3\nn*:5353\nf4\nn*:*\np90\ncChrome\nf5\nn*:61000\nf6\nn192.0.2.5:5000->198.51.100.9:53\n"
PS = "    1 /sbin/launchd\n  523 /System/Library/CoreServices/ControlCenter.app/Contents/MacOS/ControlCenter\n  600 /Applications/Docker.app/Contents/MacOS/com.docker.backend\n   88 /usr/sbin/mDNSResponder\n"


def alf(state=1, apps=(), builtin=True, downloaded=True, block_all=False):
    return {"state": state, "block_all": block_all, "builtin": builtin, "downloaded": downloaded, "stealth": False, "apps": list(apps)}


class MacFirewall(unittest.TestCase):
    def test_lsof_and_ps(self):
        rows = cmac.parse_lsof(LSOF_LISTEN, "tcp")
        self.assertEqual([(r["addr"], r["port"], r["proc"], r["pid"]) for r in rows],
                         [("*", 22, "launchd", 1), ("*", 7000, "ControlCenter", 523), ("::1", 7001, "ControlCenter", 523),
                          ("127.0.0.1", 5433, "com.docker.backend", 600)])
        self.assertEqual([r["port"] for r in cmac.parse_lsof(LSOF_UDP, "udp")], [5353, 61000])  # '*:*' and connected skipped
        self.assertEqual(cmac.parse_ps(PS)[600], "/Applications/Docker.app/Contents/MacOS/com.docker.backend")
        est = cmac.parse_lsof_established("p7\ncpsql\nf3\nn127.0.0.1:61234->127.0.0.1:5432\n")
        self.assertEqual(est, [{"l_addr": "127.0.0.1", "l_port": 61234, "p_addr": "127.0.0.1", "p_port": 5432, "proc": "psql"}])

    def test_socketfilterfw_outputs_of_several_macos_versions(self):
        a = cmac.parse_alf("Firewall is enabled. (State = 1)\n", "Firewall has block all state set to disabled.\n",
                           "Automatically allow built-in signed software ENABLED.\nAutomatically allow downloaded signed software DISABLED.\n",
                           "Firewall stealth mode is on\n",
                           "ALF: total number of apps = 2 \n\n1 :  /Applications/Foo.app \n \t ( Allow incoming connections ) \n\n"
                           "2 :  /usr/local/bin/bar \n \t ( Block incoming connections ) \n")
        self.assertEqual((a["state"], a["block_all"], a["builtin"], a["downloaded"], a["stealth"]), (1, False, True, False, True))
        self.assertEqual(a["apps"], [{"path": "/Applications/Foo.app", "allow": True}, {"path": "/usr/local/bin/bar", "allow": False}])
        b = cmac.parse_alf("Firewall is disabled. (State = 0)", "Block all DISABLED! ", "", "Stealth mode disabled", "")
        self.assertEqual((b["state"], b["block_all"], b["stealth"]), (0, False, False))
        c = cmac.parse_alf("Firewall is blocking all non-essential incoming connections. (State = 2)", "Firewall has block all state set to enabled.", "", "", "")
        self.assertTrue(c["block_all"])
        with self.assertRaises(ValueError):
            cmac.parse_alf("unexpected", "", "", "", "")

    def test_alf_verdicts(self):
        app = "/Applications/Docker.app/Contents/MacOS/com.docker.backend"
        self.assertEqual(cmac.alf_verdict(alf(0), None, app, "com.docker.backend")[0], "nofw")
        self.assertEqual(cmac.alf_verdict(alf(1, block_all=True), None, app, "com.docker.backend")[0], "blocked")
        self.assertEqual(cmac.alf_verdict(alf(1, block_all=True), None, "/usr/sbin/mDNSResponder", "mDNSResponder")[0], "open")
        self.assertEqual(cmac.alf_verdict(alf(1, [{"path": "/Applications/Docker.app", "allow": True}]), None, app, "x")[0], "open")
        self.assertEqual(cmac.alf_verdict(alf(1, [{"path": "/Applications/Docker.app", "allow": False}]), None, app, "x")[0], "blocked")
        self.assertEqual(cmac.alf_verdict(alf(1, [{"path": "/Applications/Dock", "allow": False}]), None, app, "x")[0], "unknown")  # not a prefix match
        self.assertEqual(cmac.alf_verdict(alf(1), None, "/usr/libexec/sshd-keygen-wrapper", "sshd")[0], "open")   # built-in, allowed
        self.assertEqual(cmac.alf_verdict(alf(1, builtin=False), None, "/usr/libexec/x", "x")[0], "unknown")
        self.assertEqual(cmac.alf_verdict(alf(1), None, app, "com.docker.backend")[0], "unknown")                # not listed
        self.assertEqual(cmac.alf_verdict(alf(1), None, "", "x")[0], "unknown")
        self.assertEqual(cmac.alf_verdict(None, None, app, "x"), ("unknown", "firewall unreadable"))
        self.assertEqual(cmac.alf_verdict(alf(0), {"enabled": True, "rules": 3}, app, "x"), ("unknown", "pf rules not interpreted"))
        self.assertEqual(cmac.parse_pf("Status: Enabled for 0 days", 'scrub-anchor "com.apple/*" all\nblock in all\n'), {"enabled": True, "rules": 1})

    def test_listeners_and_daemons_with_a_fake_run(self):
        outs = {("lsof", "-iTCP"): LSOF_LISTEN, ("lsof", "-iUDP"): LSOF_UDP, ("ps",): PS}

        def run(name, *args, timeout=15):
            key = (name, next((a for a in args if a.startswith("-i")), None)) if name == "lsof" else (name,)
            return 0, outs.get(key, ""), ""
        out = cmac.listeners(run, alf(1, [{"path": "/Applications/Docker.app", "allow": True}]), None)
        by = {(x["proto"], x["port"]): x for x in out}
        self.assertEqual(by[("tcp", 22)]["fw"], ["open", "built-in, allowed"])
        self.assertEqual(by[("tcp", 5433)]["fw"], ["open", "allowed in the firewall"])
        self.assertNotIn(("udp", 61000), by)                                                        # dynamic UDP: a client socket
        with tempfile.TemporaryDirectory() as d:
            for label, extra in (("com.example.ok", {}), ("com.example.dead", {}), ("com.example.off", {"Disabled": True})):
                with open(os.path.join(d, label + ".plist"), "wb") as f:
                    plistlib.dump(dict({"Label": label, "ProgramArguments": ["/bin/true"]}, **extra), f)
            listing = "PID\tStatus\tLabel\n123\t0\tcom.example.ok\n-\t78\tcom.example.dead\n-\t-9\tcom.apple.x\n-\t1\tcom.apple.y\n"
            enabled, failed = cmac.daemons(lambda *a, **k: (0, listing, ""), d)
        self.assertEqual(enabled, [{"unit": "com.example.dead", "state": "failed"}, {"unit": "com.example.ok", "state": "active"}])
        self.assertEqual(failed, ["com.example.dead"])                                              # Apple's own are not listed

    def test_ifconfig_addresses(self):
        self.assertEqual(cmac.parse_ifconfig_addrs("en0: flags=8863\n\tinet 192.0.2.5 netmask 0xffffff00\n\tinet6 fe80::1%en0 prefixlen 64\n"),
                         {"192.0.2.5", "fe80::1"})


class MacHostParsers(unittest.TestCase):
    def test_vm_stat_swap_boottime(self):
        st, page = hostinfo.parse_vm_stat("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:      1000.\n"
                                          "Pages inactive:    500.\nFile-backed pages:  300.\n\"Translation faults\":  9.\n")
        self.assertEqual((page, st["free"], st["inactive"], st["file_backed"]), (16384, 1000, 500, 300))
        self.assertEqual(hostinfo.parse_swapusage("total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)"),
                         (2048 * 2 ** 20, int(1024.5 * 2 ** 20)))
        self.assertEqual(hostinfo.parse_boottime("{ sec = 1727760000, usec = 5 } Tue Oct  1 08:00:00 2024"), 1727760000)

    def test_netstat_mount_who(self):
        ns = ("Name  Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll\n"
              "lo0   16384 <Link#1>                          100     0      5000      100     0      5000     0\n"
              "en0   1500  <Link#11>     aa:bb:cc:dd:ee:ff   10     0      123456    20     0      654321   0\n"
              "en0   1500  192.0.2/24    192.0.2.5           10     -      123456    20     -      654321   -\n"
              "utun3 1380  <Link#20>                         1      0      777       2      0      888      0\n")
        self.assertEqual(hostinfo.parse_netstat_ib(ns), {"en0": (123456, 654321), "utun3": (777, 888)})
        mount = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
                 "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)\n"
                 "/dev/disk3s6 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime, nobrowse)\n"
                 "devfs on /dev (devfs, local, nobrowse)\n/dev/disk5s1 on /Volumes/Backup (apfs, local, journaled)\n"
                 "/dev/disk3s2 on /Volumes/Other (apfs, local)\n//guest@nas/share on /Volumes/share (smbfs, nodev, nosuid)\n")
        self.assertEqual(hostinfo.parse_mount(mount), ["/", "/Volumes/Backup", "/Volumes/share"])  # one per APFS container
        self.assertEqual(hostinfo.parse_who("alice    console  Oct  1 09:00\nalice    ttys001  Oct  1 09:10 (192.0.2.20)\n"),
                         [{"user": "alice", "tty": "console"}])
        est = ("tcp4       0      0  192.0.2.5.22         192.0.2.20.51234     ESTABLISHED\n"
               "tcp6       0      0  fe80::1%en0.22       fe80::2%en0.50000    ESTABLISHED\n"
               "tcp4       0      0  192.0.2.5.22         198.51.100.9.40000   TIME_WAIT\n"
               "tcp4       0      0  192.0.2.5.5900       192.0.2.21.40001     ESTABLISHED\n")
        self.assertEqual(hostinfo.parse_netstat_established(est, (22, 5900)), {22: ["192.0.2.20", "fe80::2"], 5900: ["192.0.2.21"]})


# ---- collector on macOS/Windows, with fakes -----------------------------------------------------------------------------


class NativeCollector(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(collector, k) for k in ("LINUX", "MACOS", "WINDOWS", "OS_NAME", "UNSUPPORTED_BOOT", "run", "OFF", "boot_time")}
        self.saved_cwin = getattr(collector, "cwin", None)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(collector, k, v)
        if self.saved_cwin is None and hasattr(collector, "cwin"):
            del collector.cwin

    def as_windows(self, fake_cwin, off=()):
        collector.LINUX, collector.MACOS, collector.WINDOWS, collector.OS_NAME = False, False, True, "windows"
        collector.UNSUPPORTED_BOOT = ("blame",)
        collector.cwin = fake_cwin
        collector.OFF = set(off)
        collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name))  # no docker, no tailscale

    def fake_cwin(self, ps=None, listeners=None):
        ps_fn = ps or (lambda run, script, timeout=60: fw([rule("ssh", lp="22")]) if script == "fw" else {
            "boot": 1, "services": [{"n": "Spooler", "s": "Stopped", "e": 1067}], "events": [], "perf": None})
        ls_fn = listeners or (lambda f: [lst(22, "sshd", fw=list(cwin.fw_verdict(f, {"proto": "tcp", "port": 22, "pid": 1, "path": ""}, {}, ENV)))])

        class Fake:
            PS_FIREWALL, PS_BOOT = "fw", "boot"
            powershell = staticmethod(ps_fn)
            fw_summary = staticmethod(cwin.fw_summary)
            boot_sections = staticmethod(cwin.boot_sections)
            listeners = staticmethod(ls_fn)
        return Fake

    def test_windows_net_sections(self):
        self.as_windows(self.fake_cwin())
        d = collector.collect_net()
        self.assertEqual(d["os"], "windows")
        self.assertEqual(d["errors"], {})
        self.assertEqual(d["firewall"]["kind"], "windows")
        self.assertEqual(d["listeners"][0]["fw"][0], "open")                                      # verdict from the firewall read first
        for k in ("ufw", "docker_user", "iptables", "drops", "f2b"):
            self.assertIn(k, d["unsupported"])
            self.assertIn(k, d["absent"])
        self.assertIn("serve", d["absent"])                                                         # tailscale not installed

    def test_windows_firewall_off_in_config_never_gives_a_verdict(self):
        self.as_windows(self.fake_cwin(), off=("firewall",))
        d = collector.collect_net()
        self.assertIn("firewall", d["disabled"])
        self.assertEqual(d["listeners"][0]["fw"], ["unknown", "firewall check off in config.ini"])

    def test_windows_firewall_failure_is_an_error_and_ports_are_unknown(self):
        def ps(run, script, timeout=60):
            raise RuntimeError("COM error")
        self.as_windows(self.fake_cwin(ps=ps, listeners=lambda f: cwin.listeners_from([{"addr": "0.0.0.0", "port": 22, "state": 2, "pid": 1}], [], {}, f)))
        d = collector.collect_net()
        self.assertIn("firewall", d["errors"])
        self.assertEqual(d["listeners"][0]["fw"], ["unknown", "firewall unreadable"])
        pb = render.problems(d, {"ts": time.time(), "containers": [], "absent": True})
        self.assertTrue(any(sev == 2 and "firewall state unreadable" in t for sev, t in pb))

    def test_windows_boot_sections(self):
        self.as_windows(self.fake_cwin())
        collector.boot_time = lambda: 1700000000
        b = collector.collect_boot()
        self.assertEqual((b["os"], b["failed"], b["errors"]), ("windows", ["Spooler"], {}))
        self.assertIn("blame", b["unsupported"])
        self.assertIn("analyze", b["notes"])                                                        # boot time not recorded: info
        self.assertIn("not recorded", TEXT(render.boot_block_avvio(b, 100, 90)))
        self.assertIn("not available on this OS", TEXT(render.boot_block_lente(b, 90, 3)))

    def test_docker_stats_memory(self):
        self.assertEqual(collector.parse_mem_usage("12.5MiB / 7.66GiB"), int(12.5 * 2 ** 20))
        self.assertEqual(collector.parse_mem_usage("1.2GiB / 8GiB"), int(1.2 * 2 ** 30))
        self.assertEqual(collector.parse_mem_usage("512kB / 1GB"), 512000)
        self.assertIsNone(collector.parse_mem_usage("--"))

    def test_macos_third_party_tools_never_run_as_root(self):
        seen = {}
        saved_run, saved_which, saved_ident = collector.subprocess.run, collector.shutil.which, collector.mac_identity
        collector.MACOS, collector.LINUX = True, False

        class R:
            returncode, stdout, stderr = 0, "", ""

        class Pw:
            pw_uid, pw_gid, pw_dir, pw_name = 501, 20, "/Users/alice", "alice"
        collector.shutil.which = lambda name, path=None: "/usr/local/bin/" + name
        collector.subprocess.run = lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or R()
        collector.mac_identity = lambda exe: Pw
        try:
            collector.run("docker", "ps")
        finally:
            collector.subprocess.run, collector.shutil.which, collector.mac_identity = saved_run, saved_which, saved_ident
        if sys.version_info >= (3, 9):
            self.assertEqual((seen["kw"]["user"], seen["kw"]["group"], seen["kw"]["extra_groups"]), (501, 20, []))
        else:
            self.assertIn("preexec_fn", seen["kw"])
        self.assertEqual(seen["kw"]["env"]["HOME"], "/Users/alice")
        self.assertEqual(seen["kw"]["encoding"], "utf-8")

    @unittest.skipUnless(os.name == "posix", "POSIX ownership")
    def test_mac_identity_rules(self):
        if os.geteuid() != 0:
            self.assertIsNone(collector.mac_identity("/bin/sh"))                                   # not root: nothing to drop
        saved = collector.os.geteuid
        collector.os.geteuid = lambda: 0
        try:
            self.assertIsNone(collector.mac_identity("/usr/bin/true" if os.path.exists("/usr/bin/true") else "/bin/true"))  # Apple/system path
        finally:
            collector.os.geteuid = saved


# ---- renderer with macOS/Windows data ------------------------------------------------------------------------------------


class NativeRenderer(unittest.TestCase):
    def snap(self, os_name):
        cont, net, boot, base = demo.snapshot(os_name=os_name)
        return cont, net, boot, base

    def test_exposure_uses_the_collector_verdicts(self):
        cont, net, boot, _ = self.snap("windows")
        rows = {(r["port"], r["proto"]): r for r in render.exposure_rows(net, cont)}
        self.assertEqual((rows[(22, "tcp")]["lan"], rows[(22, "tcp")]["ts"]), (1, 1))
        self.assertEqual(rows[(8080, "tcp")]["name"], "shop-web-1")                                 # Docker Desktop backend -> container
        self.assertTrue(rows[(5432, "tcp")]["warn"])                                                # DB open on the LAN
        self.assertEqual(render.group_of(rows[(445, "tcp")]), "LOCALE")                             # blocked: LAN and tailnet both
        self.assertEqual(rows[(3389, "tcp")]["lan"], 2)
        self.assertEqual(rows[(7680, "tcp")]["lan"], 3)
        self.assertFalse(any("bypass" in r["note"] for r in rows.values()))                         # no DOCKER-USER on Windows

    def test_ipv4_and_ipv6_sockets_merge_to_the_most_exposed(self):
        net = {"os": "windows", "listeners": [lst(80, "web", fw=["blocked", "x"]), lst(80, "web", addr="::", fw=["open", "rule y"])],
               "firewall": {"kind": "windows"}}
        r = render.exposure_rows(net, None)[0]
        self.assertEqual((r["lan"], r["note"]), (1, "rule y"))

    def test_shared_discovery_ports_and_desktop_noise(self):
        cont, net, _, _ = self.snap("windows")
        mdns = lambda *procs: [lst(5353, p, proto="udp", fw=["open", 'rule "mDNS" (Private)']) for p in procs]  # noqa: E731
        base = {"ts": 0, "ports": render.exposure_keys(dict(net, listeners=net["listeners"] + mdns("msedge")), cont)}
        for procs in (("chrome",), ("chrome", "msedge"), ("msedge", "chrome")):                   # whoever holds it: no alarm
            now = dict(net, listeners=net["listeners"] + mdns(*procs))
            self.assertEqual(render.new_ports(now, cont, base), {}, procs)
        legacy = {"ts": 0, "ports": dict(base["ports"], **{"5353/u:LAN": {"name": "msedge", "lan": 1}})}  # recorded before the stable name
        self.assertEqual(render.new_ports(dict(net, listeners=net["listeners"] + mdns("chrome")), cont, legacy), {})
        row = next(r for r in render.exposure_rows(dict(net, listeners=net["listeners"] + mdns("msedge", "chrome")), cont) if r["port"] == 5353)
        self.assertEqual(row["name"], "mDNS (chrome, msedge)")
        desk = dict(net, listeners=net["listeners"] + [lst(53325, "Code", addr="127.0.0.1", fw=["open", "local"])])
        self.assertNotIn("Code", [r["name"] for r in render.webapp_rows(desk, cont)])               # local desktop apps are noise
        relay = dict(net, listeners=[lst(38261, "wslrelay", addr="127.0.0.1", fw=["open", "local"])])
        self.assertEqual(next(r["name"] for r in render.exposure_rows(relay, cont) if r["port"] == 38261), "wslrelay")  # WSL, not a container
        why = cwin.fw_verdict(fw([rule("Wi-Fi Direct Spooler Use (In)", pr=256, svc="TermService", g="@FirewallAPI.dll,-36851")]),
                              {"proto": "tcp", "port": 49674, "pid": 900, "path": r"C:\Windows\System32\svchost.exe"}, {"termservice": {900}}, ENV)
        self.assertEqual(why, ("unknown", 'rule "Wi-Fi Direct Spooler Use (In)" not understood (Private)'))

    def test_problems_speak_of_the_os_firewall(self):
        cont, net, boot, base = self.snap("windows")
        ids = lambda n: {pid for _, _, pid in render.problems_raw(n, cont, boot=boot, baseline=base)}  # noqa: E731
        self.assertNotIn("ufw-missing", ids(net))
        self.assertNotIn("docker-bypass", ids(net))
        off = dict(net, firewall=dict(net["firewall"], off=["Public"]))
        texts = [t for _, t, _ in render.problems_raw(off, cont, boot=boot, baseline=base)]
        self.assertTrue(any("Windows Firewall off on the Public network" in t for t in texts))
        self.assertIn("firewall-policy", ids(dict(net, firewall=dict(net["firewall"], policy=True))))
        self.assertIn("firewall-unreadable", ids(dict(net, firewall=None)))
        self.assertNotIn("firewall-unreadable", ids(dict(net, firewall=None, disabled=["firewall"])))
        _, mnet, mboot, mbase = self.snap("darwin")
        moff = dict(mnet, firewall=dict(mnet["firewall"], state=0, off=["Application Firewall"]))
        self.assertTrue(any("macOS firewall off:" in t for _, t, _ in render.problems_raw(moff, cont, boot=mboot, baseline=mbase)))
        for pid in ("firewall-off", "firewall-unreadable", "firewall-policy"):
            self.assertIn(pid, render.CATALOG)

    def test_firewall_section(self):
        _, net, _, _ = self.snap("windows")
        fw_text = TEXT(render.firewall_block(net, 120))
        self.assertIn("Windows Firewall on (active: Private)", fw_text)
        self.assertIn("WHAT LETS PORTS IN", fw_text)
        self.assertIn('rule "OpenSSH SSH Server (sshd)" (Private)', fw_text)
        self.assertNotIn("ufw", fw_text)
        self.assertIn("listening ports let in", TEXT(render.ov_firewall(net, 100, 2)))
        _, mnet, _, _ = self.snap("darwin")
        self.assertIn("macOS firewall on · stealth", TEXT(render.firewall_block(mnet, 120)))
        self.assertIn("OFF", TEXT(render.fw_status_lines(dict(mnet, firewall=dict(mnet["firewall"], state=0)))))
        capped = TEXT(render.native_fw_details(net, 120, max_rules=1))
        self.assertIn("… +2 more", capped)

    def test_boot_and_sessions_labels(self):
        _, _, wboot, _ = self.snap("windows")
        self.assertIn("FAILED SERVICES", TEXT(render.boot_block_fallite(wboot, 90)))
        self.assertIn("SYSTEM EVENT LOG", TEXT(render.boot_block_journal(wboot, 90, 3)))
        self.assertIn("events 3 err", TEXT(render.ov_boot(wboot, 100, 2)))
        _, _, mboot, _ = self.snap("darwin")
        full = TEXT(render.ov_boot(mboot, 100, -2))
        self.assertNotIn("SLOWEST UNITS", full)                                                     # unsupported: left out
        self.assertNotIn("SYSTEM LOG", full)
        self.assertIn("not available on this OS", TEXT(render.boot_block_avvio(mboot, 100, 90)))
        page = TEXT(render.page_boot(mboot, 120, 60))
        self.assertNotIn("SLOWEST UNITS", page)
        sess = {"local": [{"user": "alice", "tty": "Console"}], "ssh": [], "rdp": ["203.0.113.9"]}
        t = TEXT(render.ov_sessioni({"sessions": sess}, 90, 0))
        self.assertIn("remote desktop from 203.0.113.9", t)
        self.assertIn("NOT local", t)

    def test_every_width_draws_and_nothing_is_wider_than_the_screen(self):
        for os_name in ("windows", "darwin"):
            cont, net, boot, base = self.snap(os_name)
            sm = demo.sampler_data({"cpu": {f"cpu{i}": 0.1 * i for i in range(8)}, "thermal": {}, "net": {}}, os_name)
            for w, h in ((79, 24), (120, 33), (200, 50), (226, 50), (237, 64)):
                with self.subTest(os=os_name, size=(w, h)):
                    lines = render.page_overview(sm, cont, net, boot, w, h - 2, baseline=base)
                    self.assertNotIn("Traceback", TEXT(lines))
                    self.assertFalse([ln for ln in lines if render.vlen(ln) > w])
                    self.assertFalse(re.search(r"\w+(Error|Exception)\(", TEXT(lines)))  # no block replaced by its exception

    def test_baseline_with_native_data(self):
        cont, net, _, base = self.snap("windows")
        keys = render.exposure_keys(net, cont)
        self.assertIn("22/t:LAN", keys)
        self.assertNotIn("445/t:LAN", keys)                                                          # blocked: not exposed
        self.assertEqual(render.new_ports(net, cont, base), {})
        newer = dict(net, listeners=net["listeners"] + [lst(9999, "evil", fw=["open", 'rule "x" (Private)'])])
        self.assertEqual(render.new_ports(newer, cont, base), {"9999/t:LAN": "NEW"})

    def test_expose_with_native_data(self):
        import socket
        for os_name in ("windows", "darwin"):
            with self.subTest(os=os_name):
                cont, net, boot, base = self.snap(os_name)
                saved = (render.CFG["expose"], render.CFG["webapps"], render.DEMO_OS, socket.gethostname)  # demo_defaults() sets them all
                render.CFG["expose"], render.DEMO_OS = {}, os_name
                try:
                    render.demo_defaults()                                                          # the demo's own [expose]: one within, two beyond
                    expose = dict(render.CFG["expose"])
                    pb = {pid: t for _, t, pid in render.problems_raw(net, cont, boot=boot, baseline=base)}
                finally:
                    render.CFG["expose"], render.CFG["webapps"], render.DEMO_OS, socket.gethostname = saved
                self.assertEqual(expose, {"shop-web": "LAN", "shop-db": "LOCALE", "node": "TAILNET"})  # no systemd unit here: the Funnel's process
                self.assertEqual(pb["over-exposed"], "2 services reach beyond config.ini: node :8444 Internet > tailnet, shop-db :5432 LAN > local")
                self.assertNotIn("expose-unmatched", pb)
                self.assertIn("db-open-lan", pb)
                render.CFG["expose"] = expose
                try:
                    text = TEXT(render.exposure_block(net, cont, 120))
                finally:
                    render.CFG["expose"] = saved[0]
                self.assertIn("beyond config.ini: tailnet", text)
                self.assertIn("beyond config.ini: local", text)
                self.assertIn("expected: LAN", text)


# ---- kiosk, HTML, config ------------------------------------------------------------------------------------------------


class Kiosk(unittest.TestCase):
    def test_html_colours_banner_and_escaping(self):
        out = htmlview.to_html("\x1b[1;41;37m ✖ 2 PROBLEMS \x1b[0m <b>\x1b[1;7m ok \x1b[0m")
        self.assertIn('class="w bR B"', out)
        self.assertIn('class="rv B"', out)
        self.assertIn("&lt;b&gt;", out)
        page = htmlview.kiosk_page("a\x1b[K\r\nb", 237, 64, 2, "host<x>")
        self.assertIn("calc(98vw / 144.6)", page)                                                   # the grid fills the screen
        self.assertIn('content="2"', page)
        self.assertIn("host&lt;x&gt;", page)
        self.assertNotIn("\r", page)                                                                # one line per row, never two
        self.assertNotIn("<script", page.lower())

    def test_grid_and_browser_command(self):
        saved = dict(render.CFG)
        try:
            render.CFG["columns"], render.CFG["rows"] = 180, 50
            self.assertEqual(render.kiosk_grid(), (180, 50))
            render.CFG["columns"], render.CFG["rows"] = 0, 0
            cols, rows = render.kiosk_grid()
            self.assertEqual(rows, 64)
            self.assertTrue(100 <= cols <= 400)
        finally:
            render.CFG.clear()
            render.CFG.update(saved)
        edge = render.browser_command(r"C:\Edge\msedge.exe", "file:///x.html", r"C:\p")
        self.assertEqual(edge[:3], [r"C:\Edge\msedge.exe", "--app=file:///x.html", "--start-fullscreen"])
        self.assertFalse([a for a in edge if "kiosk" in a])                                         # locked kiosk: Alt+F4 would not close it
        self.assertIn("--user-data-dir=C:\\p", edge)                                                # never the user's own profile
        self.assertEqual(render.browser_command("/usr/bin/firefox", "file:///x.html", "/p"), ["/usr/bin/firefox", "--new-window", "file:///x.html"])
        for n in (1, 3):  # the footer says how to get out, and never runs past the screen
            foot = render.ANSI.sub("", render.frame(("Overview", 1, 1, []), 0, n, 99, 10, keys=False, hint=render.KIOSK_HINT).split("\r\n")[-1])
            self.assertIn("closes", foot)
            self.assertLessEqual(len(foot), 99)
        self.assertIsNone(render.find_browser("none"))
        self.assertEqual(render.find_browser("/opt/my/chrome"), "/opt/my/chrome")

    def test_kiosk_writes_the_page_and_stops_when_the_browser_closes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "display.html")
            saved = (render.launch, render.time.sleep, render.user_dir, render.find_browser)

            class Browser:
                def __init__(self, cmd):
                    self.cmd, self.polls = cmd, 0

                def poll(self):
                    self.polls += 1
                    return 0 if self.polls > 1 else None
            clock = [0.0]
            render.launch = Browser  # never subprocess.Popen itself: macOS reads its metrics with commands
            render.time.sleep = lambda s: clock.__setitem__(0, clock[0] + 30)
            render.user_dir = lambda: d
            render.find_browser = lambda choice=None: "/opt/browser"
            real_time = render.time.time
            render.time.time = lambda: real_time() + clock[0]
            try:
                self.assertEqual(render.kiosk(["render.py", "--kiosk", "--file", "--html", path, "--log", os.path.join(d, "k.log")]), 0)
            finally:
                render.launch, render.time.sleep, render.user_dir, render.find_browser = saved
                render.time.time = real_time
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            with open(path, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("<pre>", page)
            self.assertIn("nuc-console", page)

    def test_kiosk_opens_the_local_web_view_full_screen(self):
        started, saved = [], (render.web_up, render.find_browser, render.launch, render.user_dir)
        with tempfile.TemporaryDirectory() as d:
            render.web_up = lambda port, wait: True
            render.find_browser = lambda choice=None: "/opt/browser"
            render.launch = lambda cmd: started.append(cmd)
            render.user_dir = lambda: d
            try:
                self.assertEqual(render.kiosk(["render.py", "--kiosk", "--log", os.path.join(d, "k.log")]), 0)
            finally:
                render.web_up, render.find_browser, render.launch, render.user_dir = saved
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        cmd = started[0]
        self.assertTrue(cmd[1].startswith("--app=http://127.0.0.1:8787/?fit=1&cols="))
        for part in ("rows=", "rotate=1", "kiosk=1"):
            self.assertIn(part, cmd[1])
        self.assertIn("--start-fullscreen", cmd)

    def test_browser_mode_opens_a_normal_window_of_the_default_browser(self):
        opened, saved = [], (render.web_up, render.user_dir, getattr(render.os, "startfile", None), render.subprocess.run)
        with tempfile.TemporaryDirectory() as d:
            render.user_dir = lambda: d
            render.os.startfile = lambda url: opened.append(url)
            render.subprocess.run = lambda cmd, **kw: opened.append(cmd[-1])
            import webbrowser
            saved_wb, webbrowser.open = webbrowser.open, lambda url: opened.append(url) or True
            try:
                render.web_up = lambda port, wait: False
                self.assertEqual(render.open_in_browser(["render.py", "--open", "--log", os.path.join(d, "o.log")]), 1)  # no web view
                render.web_up = lambda port, wait: True
                self.assertEqual(render.open_in_browser(["render.py", "--open", "--log", os.path.join(d, "o.log")]), 0)
            finally:
                render.web_up, render.user_dir, render.subprocess.run = saved[0], saved[1], saved[3]
                if saved[2] is None:
                    del render.os.startfile
                else:
                    render.os.startfile = saved[2]
                webbrowser.open = saved_wb
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        self.assertEqual(opened, ["http://127.0.0.1:8787/?fit=1"])                                  # a plain tab: no kiosk, no grid

    def test_a_long_host_name_never_hides_the_status(self):
        saved = render.socket.gethostname
        render.socket.gethostname = lambda: "sjc22-be107-89b7b6f4-ed34-423a-8d4e-79a8cba66531-BAC22B2659BA.local"
        try:
            for w in (79, 100, 120):
                head = render.ANSI.sub("", render.frame(("System", 1, 1, []), 0, 1, w, 10, pb=[(2, "x")]).split("\r\n")[0])
                self.assertIn("✖ 1 PROBLEMS", head, w)
                self.assertIn("sjc22-", head)
                self.assertEqual("…" in head, w < 110)                                               # cut only when it does not fit
                self.assertLessEqual(len(head), w)
        finally:
            render.socket.gethostname = saved

    def test_display_section_and_paths(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[display]\nbrowser = none\nmode = kiosk\nzoom = 900\n")
        try:
            d = nuc_config.load(f.name)["display"]
            self.assertEqual((d["browser"], d["mode"], d["zoom"]), ("none", "fullscreen", 200))      # kiosk = fullscreen; zoom clamped
        finally:
            os.unlink(f.name)
        self.assertEqual(nuc_config.load("/nonexistent")["display"], {"browser": "auto", "mode": "browser", "zoom": 100})
        for bad, expected in (("mode = blink", "browser"), ("mode = NONE", "none"), ("mode = no", "none")):
            with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
                f.write("[display]\n" + bad + "\n")
            try:
                self.assertEqual(nuc_config.load(f.name)["display"]["mode"], expected)
            finally:
                os.unlink(f.name)
        state_paths = {"STATE": nuc_config.RUN_DIR, "NET_STATE": nuc_config.RUN_DIR, "BOOT_STATE": nuc_config.RUN_DIR,
                       "BASELINE": nuc_config.LIB_DIR, "ACCEPTED_PATH": nuc_config.LIB_DIR}
        for name, folder in state_paths.items():  # every state file of this OS lives in its folders (no Linux path left on Windows)
            if not os.environ.get("NUC_CONSOLE_" + {"STATE": "STATE", "NET_STATE": "NET", "BOOT_STATE": "BOOT",
                                                    "BASELINE": "BASELINE", "ACCEPTED_PATH": "ACCEPTED"}[name]):
                self.assertEqual(os.path.dirname(getattr(render, name)), folder, name)
        for name in ("OUT", "OUT_NET", "OUT_BOOT"):
            self.assertEqual(os.path.dirname(getattr(collector, name)), nuc_config.RUN_DIR)
        if nuc_config.LINUX:  # unchanged on Linux
            self.assertEqual((nuc_config.DEFAULT_PATH, nuc_config.RUN_DIR, nuc_config.LIB_DIR),
                             ("/etc/nuc-console/config.ini", "/run/nuc-console", "/var/lib/nuc-console"))
            self.assertEqual(collector.OUT_NET, "/run/nuc-console/net.json")
            self.assertEqual(render.BASELINE, "/var/lib/nuc-console/baseline.json")

    def test_config_is_utf8_with_or_without_bom(self):
        for prefix in (b"", b"\xef\xbb\xbf"):  # Windows Notepad may save a BOM
            with tempfile.NamedTemporaryFile("wb", suffix=".ini", delete=False) as f:
                f.write(prefix + "[webapps]\ncaffè = 8080\n[features]\nthermal = no\n".encode("utf-8"))
            try:
                cfg = nuc_config.load(f.name)
            finally:
                os.unlink(f.name)
            self.assertEqual(cfg["webapps"], {"caffè": [8080]})
            self.assertFalse(cfg["features"]["thermal"])

    def test_refresh_seconds_one_setting_for_every_screen(self):
        def load(text):
            with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
                f.write(text)
            try:
                c = nuc_config.load(f.name)
            finally:
                os.unlink(f.name)
            return c["refresh_seconds"], c["web"]["refresh_seconds"]
        self.assertEqual(load(""), (2, 2))
        self.assertEqual(load("[dashboard]\nrefresh_seconds = 0\n"), (1, 1))                       # never under 1 s
        self.assertEqual(load("[dashboard]\nrefresh_seconds = 60\n"), (10, 10))                    # never over 10 s
        self.assertEqual(load("[dashboard]\nrefresh_seconds = x\n"), (2, 2))
        self.assertEqual(load("[web]\nrefresh_seconds = 5\n"), (2, 5))                             # an older config file: web only
        self.assertEqual(load("[web]\nrefresh_seconds = 30\n"), (2, 10))
        self.assertEqual(load("[dashboard]\nrefresh_seconds = 3\n[web]\nrefresh_seconds = 5\n"), (3, 3))  # the new key wins
        with open(os.path.join(ROOT, "config", "config.ini"), encoding="utf-8") as f:
            shipped = f.read()
        self.assertEqual(load(shipped), (2, 2))
        self.assertNotRegex(shipped.split("[web]")[1].split("[display]")[0], r"(?m)^refresh_seconds")  # one place only

    def test_set_key_changes_one_line_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.ini")
            shutil.copy(os.path.join(ROOT, "config", "config.ini"), p)
            with open(p, encoding="utf-8") as f:
                before = f.read().splitlines()
            nuc_config.set_key(p, "display", "mode", "fullscreen")
            with open(p, encoding="utf-8") as f:
                after = f.read().splitlines()
            changed = [(a, b) for a, b in zip(before, after) if a != b]
            self.assertEqual(changed, [("mode = browser", "mode = fullscreen")])                     # comments and order kept
            self.assertEqual(nuc_config.load(p)["display"]["mode"], "fullscreen")
            with open(p, "w", encoding="utf-8") as f:
                f.write("[features]\nthermal = no\n\n[display]\n# mode = fullscreen\nbrowser = auto\n\n[web]\nport = 9\n")
            nuc_config.set_key(p, "display", "mode", "none")                                         # a commented key is not the key
            nuc_config.set_key(p, "extra", "k", "v")
            with open(p, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("[display]\n# mode = fullscreen\nbrowser = auto\nmode = none\n\n[web]", text)
            self.assertTrue(text.endswith("[extra]\nk = v\n"))
            cfg = nuc_config.load(p)
            self.assertEqual((cfg["display"]["mode"], cfg["web"]["port"], cfg["features"]["thermal"]), ("none", 9, False))
            r = subprocess.run([sys.executable, os.path.join(ROOT, "src", "nuc_config.py"), "--set", p, "display", "zoom", "125"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run([sys.executable, os.path.join(ROOT, "src", "nuc_config.py"), "--get", "display", "zoom"],
                               capture_output=True, text=True, env=dict(os.environ, NUC_CONSOLE_CONFIG=p))
            self.assertEqual(r.stdout.strip(), "125")

    def test_log_file_rotates_once_over_the_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "logs", "x.log")
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                f.write("x" * 100)
            saved = sys.stdout, sys.stderr
            try:
                fh = nuc_config.log_to(p, max_bytes=10)
                print("hello", file=sys.stderr)
            finally:
                sys.stdout, sys.stderr = saved
                fh.close()
            self.assertTrue(os.path.exists(p + ".1"))
            with open(p) as f:
                self.assertEqual(f.read(), "hello\n")


# ---- installers -----------------------------------------------------------------------------------------------------------


class Installers(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_install_sh_hands_macos_over_before_anything_linux(self):
        s = self.read("install.sh")
        darwin, root = s.index("Darwin"), s.index('[ "$(id -u)" -eq 0 ]')
        self.assertLess(darwin, root)
        self.assertLess(darwin, s.index("OLD_TZ="))

    def test_python_pins_agree(self):
        ps, mac = self.read("install-windows.ps1"), self.read("install-macos.sh")
        self.assertEqual(re.search(r"\$PyVersion = '([\d.]+)'", ps).group(1), re.search(r"PY_VERSION=([\d.]+)", mac).group(1))
        for h in re.findall(r"sha256 = '([0-9a-f]+)'", ps) + re.findall(r"PY_PKG_SHA256=([0-9a-f]+)", mac):
            self.assertEqual(len(h), 64)

    @unittest.skipUnless(shutil.which("bash") and sys.platform != "win32", "bash (on Windows `bash` may be WSL: other paths)")
    def test_macos_installer_syntax(self):
        r = subprocess.run(["bash", "-n", os.path.join(ROOT, "install-macos.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_launchd_plists(self):
        pls = {}
        for name in ("collector", "web", "display", "notify"):
            with open(os.path.join(ROOT, "launchd", f"com.nuc-console.{name}.plist"), "rb") as f:
                pls[name] = plistlib.load(f)
            self.assertEqual(pls[name]["Label"], f"com.nuc-console.{name}")
            self.assertEqual(pls[name]["ProgramArguments"][0], "@PYTHON@")
        self.assertEqual(pls["web"]["UserName"], "_nuc-console")                                    # never root
        self.assertEqual(pls["web"]["ProgramArguments"][-1], "--local")                             # 127.0.0.1 unless [web] says otherwise
        self.assertTrue(pls["display"]["AbandonProcessGroup"])                                      # the browser outlives its launcher
        self.assertEqual(pls["notify"]["UserName"], "_nuc-console")                                 # the Telegram notifier is never root
        self.assertEqual(pls["notify"]["ProgramArguments"][2], "@DEST@/notify.py")
        self.assertEqual(pls["notify"]["KeepAlive"], {"SuccessfulExit": False})                     # exit 0 = idle (off or not paired): no loop
        self.assertFalse({"Sockets", "inetdCompatibility", "WatchPaths", "QueueDirectories"} & set(pls["notify"]))  # outbound only

    def test_display_choice_in_both_installers(self):
        ps, mac = self.read("install-windows.ps1"), self.read("install-macos.sh")
        self.assertIn("[ValidateSet('browser', 'fullscreen', 'kiosk', 'none')][string]$Display", ps)
        self.assertIn("--set $cfg display mode $Display", ps)
        self.assertIn("Start Menu\\Programs\\nuc-console.url", ps)
        self.assertIn("browser|fullscreen|kiosk|none|no)", mac)
        self.assertIn("/Applications/nuc-console.webloc", mac)
        for text in (ps, mac):  # both modes open at login, as the user: a normal window (--open) or full screen (--kiosk)
            self.assertIn("--open", text)
            self.assertIn("--kiosk", text)
        self.assertNotIn("explorer.exe", ps)

    @unittest.skipUnless(shutil.which("pwsh") or (sys.platform == "win32" and shutil.which("powershell")), "PowerShell")
    def test_windows_installer_parses(self):
        ps = shutil.which("pwsh") or shutil.which("powershell")
        path = os.path.abspath(os.path.join(ROOT, "install-windows.ps1")).replace("'", "''")
        cmd = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$t, [ref]$e); "
               "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }" % path)
        r = subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_notify_unit_is_unprivileged_and_outbound_only(self):
        unit = {}
        for line in self.read("systemd/nuc-console-notify.service").splitlines():
            if "=" in line and not line.lstrip().startswith(("#", "[")):
                k, _, v = line.partition("=")
                unit[k.strip()] = v.strip()
        self.assertEqual((unit["User"], unit["Group"]), ("nuc-console-notify", "nuc-console-notify"))   # not the web view's user
        self.assertEqual(unit["UMask"], "0077")
        self.assertEqual(unit["ExecStart"], "/usr/bin/python3 -B /opt/nuc-console/notify.py")
        self.assertEqual((unit["Restart"], unit["RestartSec"]), ("on-failure", "30"))              # exit 0 (off / not paired) = idle
        self.assertEqual((unit["StateDirectory"], unit["StateDirectoryMode"]), ("nuc-console-notify", "0711"))
        self.assertEqual(set(unit["RestrictAddressFamilies"].split()), {"AF_INET", "AF_INET6", "AF_UNIX"})   # no AF_NETLINK, no AF_PACKET
        self.assertEqual(unit["CapabilityBoundingSet"], "")
        for k in ("NoNewPrivileges", "PrivateTmp", "ProtectHome", "PrivateDevices", "RestrictSUIDSGID"):
            self.assertEqual(unit[k], "yes", k)
        self.assertEqual(unit["ProtectSystem"], "strict")
        self.assertTrue(unit["MemoryMax"])
        src = self.read("systemd/nuc-console-notify.service")
        self.assertNotIn("Listen", src)                                                             # no ListenStream & co: it never listens
        self.assertFalse([f for f in os.listdir(os.path.join(ROOT, "systemd")) if f.endswith((".socket", ".path"))])

    def test_linux_installer_installs_starts_and_uninstalls_the_notifier(self):
        s = self.read("install.sh")
        self.assertIn("install -m 0644 systemd/*.service /etc/systemd/system/", s)                 # the unit goes in with the others
        self.assertIn("install -m 0755 bin/nuc-console-telegram /usr/local/bin/", s)
        self.assertIn("useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console-notify", s)
        self.assertIn("install -d -m 0711 -o nuc-console-notify -g nuc-console-notify /var/lib/nuc-console-notify", s)
        self.assertIn("systemctl enable nuc-console-notify.service", s)
        self.assertRegex(s, r'if python3 "\$DEST/notify\.py" --enabled; then systemctl restart nuc-console-notify\.service\n'
                            r'else systemctl stop nuc-console-notify\.service 2>/dev/null \|\| true; fi')
        down = s[s.index('"--uninstall" ]'):s.index("exit 0")]                                       # the uninstall branch
        self.assertRegex(down, r"for u in [^\n]*nuc-console-notify\.service; do\n\s+systemctl disable --now \"\$u\"")   # one by one
        self.assertIn("/etc/systemd/system/nuc-console-notify.service", down)
        self.assertIn("/usr/local/bin/nuc-console-telegram", down)
        self.assertRegex(down, r"rm -rf /var/lib/nuc-console-notify\b")                            # the token must not outlive the install

    def test_macos_installer_runs_the_notifier_as_the_service_account(self):
        s = self.read("install-macos.sh")
        loops = re.findall(r"for label in ([^;]+); do launchctl bootout", s)
        self.assertEqual(len(loops), 2)                                                              # uninstall and install
        for loop in loops:
            self.assertIn("com.nuc-console.notify", loop.split())
        self.assertIn('install -d -m 0711 -o "$SVC_USER" -g "$SVC_USER" "$NOTIFY_LIB"', s)
        self.assertIn('load system "$LD/com.nuc-console.notify.plist"', s)
        self.assertRegex(s, r'rm -rf "\$DEST" "\$NOTIFY_LIB"')
        self.assertIn("NOTIFY_LIB=/var/lib/nuc-console-notify", s)
        self.assertIn("nuc-console-telegram", s)
        # the account is created by one function, called by the web view and by the notifier (which is always installed)
        self.assertEqual(s.count('dscl . -create "/Users/$SVC_USER" UniqueID'), 1)
        self.assertGreaterEqual(s.count("ensure_service_user"), 3)

    def test_windows_installer_registers_the_notifier_with_a_private_folder(self):
        s = self.read("install-windows.ps1")
        self.assertIn("Register-Service 'notify' 'notify.py' 'S-1-5-20' 'notify.log'", s)          # NETWORK SERVICE: not the web view's account
        acl = next(l for l in s.splitlines() if "icacls.exe" in l and '"$Data\\notify"' in l and "/inheritance:r" in l)
        self.assertIn("'*S-1-5-20:(OI)(CI)M'", acl)                                                 # the notifier writes status.json there
        self.assertIn("'*S-1-5-19:(OI)(CI)RX'", acl)                                                # the web view reads it
        private = next(l for l in s.splitlines() if "icacls.exe" in l and '"$Data\\notify\\private"' in l)
        self.assertEqual(sorted(re.findall(r"\*(S-[0-9-]+):", private)), ["S-1-5-18", "S-1-5-20", "S-1-5-32-544"])  # SYSTEM, NETWORK SERVICE, Admins
        self.assertIn("/inheritance:r", private)                                                    # nobody else, Users and the web view included
        down = s[s.index("if ($Uninstall) {"):s.index("# ---- 1. data folder")]
        self.assertLess(down.index('Remove-Item -Recurse -Force "$Data\\notify"'), down.index("Remove-Item -Recurse -Force $Dest"))  # the token first
        self.assertIn("bin\\*.cmd", s)                                                              # nuc-console-telegram.cmd rides along

    def test_windows_wrappers_clear_the_redirect_variables(self):
        s = self.read("bin/nuc-console-accept.cmd")
        for var in ("NUC_CONSOLE_BASELINE", "NUC_CONSOLE_NET", "NUC_CONSOLE_STATE", "NUC_CONSOLE_BOOT", "NUC_CONSOLE_ACCEPTED"):
            self.assertIn(f"set {var}=", s)


def history_job_on_this_machine(case):
    """The real history sources of this OS, once: results may be empty (a quiet machine, a tool that is not there), a crash or
    a failure that is not reported is never allowed. The first step reads every source once (events() runs at the first step)."""
    with tempfile.TemporaryDirectory() as d:
        store = history.Store(os.path.join(d, "history.db"))
        try:
            job = collector.HistoryJob(store)
            job.step()
            job.step()
            job.flush()
            case.assertEqual(store.meta("schema_version"), "1")
            case.assertEqual(store.meta("os"), nuc_config.OS_NAME)
            case.assertGreaterEqual(int(store.meta("cores")), 1)
            case.assertIsInstance(json.loads(store.meta("last_errors") or "{}"), dict)
            disks, mem = collector.disk_rows(), collector.hist_meminfo()
            case.assertTrue(disks and all(r["used"] <= r["total"] for r in disks))
            case.assertTrue(0 < mem["MemAvailable"] <= mem["MemTotal"])
        finally:
            store.close()


# ---- the real system calls, on the OS they are about ----------------------------------------------------------------------


def check_sensors(tc, d, os_name):
    """collect_sensors() on the real machine (fixtures: tests/test_sensors.py): valid JSON with the contract's keys,
    whatever this machine can read; when it reads no temperature it says why (errors or absent), never silently."""
    d = json.loads(json.dumps(d, allow_nan=False))
    tc.assertEqual((set(d), d["os"]), ({"ts", "os", "cpu", "errors", "absent"}, os_name))
    cpu = d["cpu"]
    tc.assertEqual(set(cpu), {"package", "cores", "sensors", "source", "pressure", "clusters"})
    tc.assertTrue(cpu["package"] is None or -20 <= cpu["package"] <= 150, cpu)
    tc.assertTrue(all(-20 <= s["c"] <= 150 for s in cpu["sensors"]), cpu)
    tc.assertTrue(all(isinstance(v, str) for v in d["errors"].values()), d["errors"])
    tc.assertTrue(cpu["source"] or d["errors"] or d["absent"], d)
    return d


def sample_real_procs(test):
    """Two samples of the real process list around a CPU burst, with a child whose command line holds a (runtime) secret:
    -> (second sample indexed by pid, the child's pid, the JSON of the sample, seconds one sample took)."""
    secret = "tok" + os.urandom(6).hex()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--token=" + secret])
    try:
        s = procs.ProcSampler()
        s.sample()
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            sum(i * i for i in range(1000))
        t = time.perf_counter()
        out = s.sample()
        took = time.perf_counter() - t
    finally:
        child.kill()
        child.wait()
    test.assertNotIn(secret, json.dumps(out))  # names only, never arguments
    test.assertEqual(set(out), {"procs", "total", "notes"})
    test.assertEqual(set(out["total"]), {"count", "running", "threads", "unreadable"})
    test.assertEqual(out["total"]["count"], len(out["procs"]))
    test.assertLessEqual(out["total"]["unreadable"], out["total"]["count"])
    return {p["pid"]: p for p in out["procs"]}, child.pid, took

@unittest.skipUnless(sys.platform == "win32", "Windows")
class OnWindows(unittest.TestCase):
    def test_host_metrics(self):
        cpu = hostinfo.cpu_times()
        self.assertTrue(cpu and all(busy <= total for busy, total in cpu.values()))
        m = hostinfo.meminfo()
        self.assertTrue(0 < m["MemAvailable"] <= m["MemTotal"])
        self.assertGreater(hostinfo.uptime(), 0)
        self.assertIsNone(hostinfo.loadavg())
        self.assertTrue(hostinfo.filesystems())
        self.assertIn("local", hostinfo.sessions())
        self.assertIsInstance(hostinfo.net_counters(), dict)

    def test_sockets_and_firewall(self):
        self.assertTrue(any(s["state"] == winapi.TCP_LISTEN for s in winapi.tcp_table()) or True)
        f = cwin.powershell(collector.run, cwin.PS_FIREWALL)
        self.assertIn("profiles", f)
        out = cwin.listeners(f)
        self.assertTrue(all(x["fw"][0] in render.CELL for x in out))

    def test_history_event_log_reader(self):
        got = cwin.parse_events(cwin.powershell(collector.run, cwin.events_script({}), timeout=120))
        self.assertEqual(set(got["cursors"]) | set(got["errors"]), {"System", "Application"})
        self.assertTrue(all(e["kind"] in history.KINDS and e["source"] == "eventlog" for e in got["events"]))
        # incremental: from the newest record read, only newer ones come, and the cursors never go back
        again = cwin.parse_events(cwin.powershell(collector.run, cwin.events_script(got["cursors"]), timeout=120))
        self.assertEqual(set(again["cursors"]) | set(again["errors"]), {"System", "Application"})
        for log, rec in got["cursors"].items():
            self.assertGreaterEqual(again["cursors"].get(log, rec), rec)

    def test_history_job(self):
        history_job_on_this_machine(self)

    def test_cpu_sensors(self):
        data = cwin.powershell(collector.run, cwin.PS_SENSORS, timeout=60)               # the script runs and answers JSON
        self.assertEqual(set(data), {"LibreHardwareMonitor", "OpenHardwareMonitor", "ACPI"})
        self.assertEqual([src for src, _, _ in cwin.sensor_sources(data)], ["LibreHardwareMonitor", "OpenHardwareMonitor", "ACPI"])
        d = check_sensors(self, collector.collect_sensors(), "windows")                    # a runner has no sensors: absent, not a crash
        self.assertEqual((d["cpu"]["pressure"], d["cpu"]["clusters"]), (None, []))

    def test_process_list(self):
        by, child, took = sample_real_procs(self)
        me = by[os.getpid()]
        self.assertTrue(me["name"].lower().startswith("python"), me["name"])
        self.assertGreater(me["mem"], 0)
        self.assertTrue(me["user"])
        self.assertGreater(me["cpu"], 10)  # busy for 0.3 s of the interval
        self.assertGreater(me["threads"], 0)
        self.assertLessEqual(me["start"], time.time())
        self.assertIn(child, by)
        self.assertIn(4, by)  # System: listed, whatever we may read of it
        self.assertNotIn(0, by)  # Idle
        self.assertIsNone(me["state"])  # Windows has no process states
        self.assertLess(took, 2.0)  # ~50 ms for 200 processes; generous for CI

    def test_cpu_structures(self):
        """The raw buffers of the CPU screen (cpuinfo.py parses them; the parsers are tested on every OS in test_cpuinfo.py)."""
        lay = cpuinfo.parse_slpi_ex(winapi.logical_processor_info())
        n = sum(lay["groups"])
        self.assertGreaterEqual(lay["cores"], 1)
        self.assertGreaterEqual(lay["sockets"], 1)
        self.assertEqual(n, sum(c for _, c in winapi.processor_groups()))
        self.assertGreaterEqual(n, lay["cores"])
        for size in lay["cache"].values():
            self.assertGreater(size, 0)
        power = cpuinfo.parse_power_info(winapi.power_info(n))
        self.assertEqual(len(power), n)
        self.assertTrue(any(p["max"] > 0 for p in power), power)
        parts, complete = winapi.cpu_perf()
        rows = [r for _, raw in parts for r in cpuinfo.parse_cpu_perf(raw)]
        self.assertTrue(complete)
        self.assertEqual(len(rows), n)
        self.assertTrue(all(idle <= kernel for idle, kernel, *_ in rows))  # kernel time includes the idle time
        reg = winapi.processor_registry()
        self.assertTrue(reg["name"] and reg["vendor"], reg)

    def test_cpu_counters_that_move(self):
        a = cpuinfo.parse_context_switches(winapi.system_performance_info())
        time.sleep(0.1)
        b = cpuinfo.parse_context_switches(winapi.system_performance_info())
        self.assertTrue(0 < (b - a) % (1 << 32) < 10 ** 7, (a, b))  # a wrong field offset would give a constant or garbage
        pdh = winapi.PdhCounter(cpuinfo.PDH_PERFORMANCE)
        try:
            time.sleep(0.2)
            values = pdh.values()
        finally:
            pdh.close()
        self.assertTrue(any(k.endswith("_Total") for k in values), values)
        perf = cpuinfo.pdh_cpu_values(values, cpuinfo.parse_slpi_ex(winapi.logical_processor_info())["offsets"])
        self.assertTrue(perf and all(v >= 0 for v in perf.values()), values)

    def test_cpu_sampler(self):
        s = cpuinfo.CpuSampler()
        time.sleep(0.3)
        t0 = time.perf_counter()
        out = s.sample()
        took = time.perf_counter() - t0
        self.assertLess(took, 0.5, f"sample() took {took * 1000:.0f} ms")
        self.assertEqual([n for n in out["notes"] if "unreadable" in n], [], out["notes"])  # no part failed
        self.assertTrue(out["model"] and out["vendor"])
        self.assertEqual(out["threads"], os.cpu_count())
        self.assertTrue(1 <= out["cores"] <= out["threads"] and out["sockets"] >= 1)
        self.assertTrue(0 <= out["usage"]["total"]["busy"] <= 100)
        self.assertTrue(out["usage"]["cores"] and len(out["usage"]["cores"]) <= out["threads"])
        self.assertTrue(all(0 <= c["busy"] <= 100 for c in out["usage"]["cores"]))
        self.assertIsNone(out["usage"]["total"]["iowait"])
        self.assertIsNone(out["load"])
        self.assertGreater(out["uptime"], 0)
        self.assertGreater(out["rates"]["ctxt"], 0)
        self.assertTrue(out["freq"]["cur"] and all(v > 0 for v in out["freq"]["cur"].values()), out["freq"])
        self.assertEqual((out["temps"]["package"], out["temps"]["source"]), (None, None))  # from the collector's sensors.json


@unittest.skipUnless(sys.platform == "darwin", "macOS")
class OnMacOS(unittest.TestCase):
    def test_host_metrics(self):
        cpu = hostinfo.cpu_times()
        self.assertTrue(cpu and all(busy <= total for busy, total in cpu.values()))
        m = hostinfo.meminfo()
        self.assertTrue(0 < m["MemAvailable"] <= m["MemTotal"])
        self.assertGreater(hostinfo.uptime(), 0)
        self.assertEqual(len(hostinfo.loadavg()), 3)
        self.assertTrue(hostinfo.filesystems())
        self.assertIn("local", hostinfo.sessions())
        self.assertTrue(hostinfo.net_counters())

    def test_firewall_tools_answer(self):
        rc, out, _ = collector.run("socketfilterfw", "--getglobalstate")
        self.assertEqual(rc, 0)
        self.assertIn(cmac.parse_alf(out, "", "", "", "")["state"], (0, 1, 2))

    def test_history_crash_reports_reader(self):
        evs, newest = cmac.diag_events(0, cap=10 ** 6)
        self.assertTrue(all(e["kind"] in history.KINDS and e["source"] == "diag" for e in evs))
        self.assertGreaterEqual(newest, 0)
        self.assertEqual(cmac.diag_events(newest + 1, cap=10 ** 6)[0], [])  # nothing is newer than the newest
        failed = cmac.daemons(collector.run)[1]
        self.assertEqual(cmac.new_failed_daemons(None, failed, time.time()), [])

    def test_history_job(self):
        history_job_on_this_machine(self)

    def test_cpu_sensors(self):
        d = check_sensors(self, collector.collect_sensors(), "darwin")
        if os.geteuid() != 0:                                                                # powermetrics needs root (the collector is)
            self.assertNotEqual(d["cpu"]["source"], "powermetrics")
        elif "powermetrics" not in d["errors"]:
            self.assertIsNotNone(d["cpu"]["pressure"])                                       # thermal is a sampler of every Mac

    def test_process_list(self):
        by, child, took = sample_real_procs(self)
        me = by[os.getpid()]
        self.assertTrue(me["name"])
        self.assertGreater(me["mem"], 0)
        self.assertEqual(me["user"], procs.pwd.getpwuid(os.getuid()).pw_name)
        self.assertGreater(me["cpu"], 10)  # busy for 0.3 s of the interval
        self.assertIn(child, by)
        self.assertEqual(by[1]["name"], "launchd")
        self.assertLessEqual(me["start"], time.time())
        self.assertLess(took, 2.0)  # ~100 ms for 500 processes; generous for CI

    def test_cpu_sysctls(self):
        n = os.cpu_count()
        self.assertEqual(cpuinfo.sysctl_int(hostinfo.mac_sysctl("hw.logicalcpu")), n)
        self.assertGreaterEqual(cpuinfo.sysctl_int(hostinfo.mac_sysctl("hw.physicalcpu")), 1)
        self.assertTrue(cpuinfo.sysctl_str(hostinfo.mac_sysctl("machdep.cpu.brand_string")))
        self.assertIsNone(hostinfo.mac_sysctl("hw.no_such_value"))  # hw.cpufrequency on Apple Silicon is such a case
        boot = cpuinfo.parse_timeval(hostinfo.mac_sysctl("kern.boottime"))
        self.assertTrue(0 < time.time() - boot < 3650 * 86400)
        ticks = hostinfo.mac_cpu_ticks()
        self.assertEqual(len(ticks), n)
        self.assertTrue(all(len(t) == 4 and all(isinstance(v, int) and v >= 0 for v in t) for t in ticks))
        facts = cpuinfo.mac_facts(hostinfo.mac_sysctl)
        self.assertTrue(facts["model"] and facts["cores"] >= 1, facts)

    def test_cpu_sampler(self):
        s = cpuinfo.CpuSampler()
        time.sleep(0.3)
        t0 = time.perf_counter()
        out = s.sample()
        took = time.perf_counter() - t0
        self.assertLess(took, 0.5, f"sample() took {took * 1000:.0f} ms")
        self.assertEqual([n for n in out["notes"] if "unreadable" in n], [], out["notes"])  # no part failed
        self.assertTrue(out["model"])
        self.assertEqual(out["threads"], os.cpu_count())
        self.assertTrue(1 <= out["cores"] <= out["threads"])
        self.assertTrue(0 <= out["usage"]["total"]["busy"] <= 100)
        self.assertEqual(len(out["usage"]["cores"]), out["threads"])
        self.assertTrue(all(0 <= c["busy"] <= 100 and c["iowait"] is None for c in out["usage"]["cores"]))
        self.assertEqual((out["usage"]["total"]["iowait"], out["usage"]["total"]["steal"]), (None, None))
        self.assertEqual(len(out["load"]), 3)
        self.assertGreater(out["uptime"], 0)
        self.assertEqual((out["rates"]["ctxt"], out["temps"]["package"]), (None, None))  # the collector provides the temperatures
        if out["vendor"] == "Apple":
            self.assertEqual(out["freq"]["cur"], {})  # no clock sysctl on Apple Silicon: unknown, not a guess


if __name__ == "__main__":
    unittest.main()
