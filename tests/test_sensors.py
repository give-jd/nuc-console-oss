"""CPU temperature sensors written by the collector on macOS and Windows (sensors.json).

Parsers and the collection logic run on every OS with fixtures and a fake `run`; the real calls are in
tests/test_platforms.py (OnMacOS / OnWindows). The fixtures are shaped like the real tools' output; values are made up.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"  # hermetic: never read the host's config.ini
import collect_darwin as cmac  # noqa: E402
import collect_windows as cwin  # noqa: E402
import collector  # noqa: E402
import nuc_config  # noqa: E402

CPU_KEYS = {"package", "cores", "sensors", "source", "pressure", "clusters"}
SOURCES = {None, "powermetrics", "smctemp", "osx-cpu-temp", "LibreHardwareMonitor", "OpenHardwareMonitor", "ACPI"}


def check_contract(tc, d, os_name):
    """sensors.json as the renderer reads it (docs: the CPU contract, section 3): valid JSON (no NaN), exact keys, types."""
    d = json.loads(json.dumps(d, allow_nan=False))
    tc.assertEqual(set(d) - {"disabled"}, {"ts", "os", "cpu", "errors", "absent"})
    tc.assertEqual(d["os"], os_name)
    tc.assertIsInstance(d["ts"], float)
    cpu = d["cpu"]
    tc.assertEqual(set(cpu), CPU_KEYS)
    temp = lambda v: isinstance(v, float) and -20 <= v <= 150  # noqa: E731
    tc.assertTrue(cpu["package"] is None or temp(cpu["package"]), cpu["package"])
    tc.assertTrue(all(k.isdigit() and temp(v) for k, v in cpu["cores"].items()), cpu["cores"])
    for s in cpu["sensors"]:
        tc.assertEqual(set(s), {"label", "c"})
        tc.assertTrue(isinstance(s["label"], str) and s["label"] and temp(s["c"]), s)
    tc.assertIn(cpu["source"], SOURCES)
    tc.assertEqual(cpu["source"] is None, not cpu["sensors"])                      # a source only for what it read
    if cpu["package"] is not None:
        tc.assertIn(cpu["package"], [s["c"] for s in cpu["sensors"]])            # the package is one of the listed sensors
    tc.assertTrue(cpu["pressure"] is None or isinstance(cpu["pressure"], str))
    for c in cpu["clusters"]:
        tc.assertEqual(set(c), {"name", "mhz", "active", "cpus"})
        tc.assertTrue(c["name"].endswith("-Cluster"))
        tc.assertTrue(all(x is None or isinstance(x, float) for x in (c["mhz"], c["active"])))
    tc.assertTrue(all(isinstance(k, str) and isinstance(v, str) and len(v) <= 120 for k, v in d["errors"].items()), d["errors"])
    tc.assertTrue(all(isinstance(x, str) for x in d["absent"]))
    if os_name == "windows":
        tc.assertEqual((cpu["pressure"], cpu["clusters"]), (None, []))            # macOS only
    return d


# ---- macOS: powermetrics and the optional tools --------------------------------------------------------------------------

PM_HEADER = ("Machine model: Mac0,0\nOS version: 23A000\nBoot arguments:\nBoot time: Thu Oct  1 08:00:00 2026\n\n\n\n"
             "*** Sampled system activity (Thu Oct  1 10:00:00 2026 +0000) (1004.21ms elapsed) ***\n\n")
PM_INTEL = PM_HEADER + """
**** SMC sensors ****

CPU Thermal level: 0
GPU Thermal level: 0
IO Thermal level: 0
Fan: 1797.27 rpm
CPU die temperature: 45.69 C
GPU die temperature: 40.00 C
CPU Plimit: 0.00
GPU Plimit (Int): 0.00
Number of prochots: 0

**** Thermal pressure ****

Current pressure level: Nominal

"""


def _cluster(name, mhz, active, cpus):
    out = f"{name} HW active frequency: {mhz} MHz\n{name} HW active residency: {active:6.2f}% (600 MHz:   0% 972 MHz:  64% 2064 MHz: .86%)\n"
    out += f"{name} idle residency: {100 - active:6.2f}%\n"
    for cpu, f in cpus:
        out += f"CPU {cpu} frequency: {f} MHz\nCPU {cpu} active residency:   4.27% (600 MHz:  86% 828 MHz:   0%)\nCPU {cpu} idle residency:  95.73%\n"
    return out


PM_M1 = PM_HEADER + "**** Processor usage ****\n\n" + _cluster("E-Cluster", 1020, 12.34, [(0, 1146), (1, 1127), (2, 1090), (3, 1031)]) \
    + _cluster("P-Cluster", 1257, 6.02, [(4, 2064), (5, 1863), (6, 600), (7, 600)]) \
    + "\nCPU Power: 77 mW\nGPU Power: 7 mW\nANE Power: 0 mW\nCombined Power (CPU + GPU + ANE): 84 mW\n\n" \
    + "**** Thermal pressure ****\n\nCurrent pressure level: Moderate\n"
PM_MAX = PM_HEADER + "**** Processor usage ****\n\n" + _cluster("E-Cluster", 1600, 30.5, [(0, 1700), (1, 1500)]) \
    + _cluster("P0-Cluster", 3228, 55.0, [(2, 3228), (3, 3100), (4, 2900), (5, 2800)]) \
    + _cluster("P1-Cluster", 600, 0.0, [(6, 600), (7, 600), (8, 600), (9, 600)]) \
    + "\nCPU Power: 4512 mW\n\n**** Thermal pressure ****\n\nCurrent pressure level: Heavy\n"


class Powermetrics(unittest.TestCase):
    def test_intel_die_temperature_and_pressure(self):
        self.assertEqual(cmac.parse_powermetrics(PM_INTEL), {"die": 45.69, "pressure": "Nominal", "clusters": []})

    def test_apple_silicon_m1_clusters_and_their_cpus(self):
        p = cmac.parse_powermetrics(PM_M1)
        self.assertEqual((p["die"], p["pressure"]), (None, "Moderate"))               # no temperature on Apple Silicon
        self.assertEqual(p["clusters"], [
            {"name": "E-Cluster", "mhz": 1020.0, "active": 12.34, "cpus": {0: 1146.0, 1: 1127.0, 2: 1090.0, 3: 1031.0}},
            {"name": "P-Cluster", "mhz": 1257.0, "active": 6.02, "cpus": {4: 2064.0, 5: 1863.0, 6: 600.0, 7: 600.0}}])

    def test_apple_silicon_max_has_two_p_clusters(self):
        p = cmac.parse_powermetrics(PM_MAX)
        self.assertEqual([c["name"] for c in p["clusters"]], ["E-Cluster", "P0-Cluster", "P1-Cluster"])
        self.assertEqual([c["active"] for c in p["clusters"]], [30.5, 55.0, 0.0])
        self.assertEqual(sorted(p["clusters"][2]["cpus"]), [6, 7, 8, 9])
        self.assertEqual(p["pressure"], "Heavy")

    def test_what_does_not_match_stays_unknown(self):
        self.assertEqual(cmac.parse_powermetrics(""), {"die": None, "pressure": None, "clusters": []})
        self.assertEqual(cmac.parse_powermetrics(None), {"die": None, "pressure": None, "clusters": []})
        p = cmac.parse_powermetrics("CPU die temperature: hot\nCurrent pressure level: 42\nCPU 3 frequency: 2000 MHz\n"
                                    "E0-Cluster HW active frequency: 972 MHz\nCurrent pressure level: Serious\n")
        self.assertIsNone(p["die"])
        self.assertEqual(p["pressure"], "Serious")                                       # an unknown level is passed on, not dropped
        self.assertEqual(p["clusters"], [{"name": "E0-Cluster", "mhz": 972.0, "active": None, "cpus": {}}])  # CPU before any cluster: left out

    def test_sampler_lists_per_architecture(self):
        arm, intel = cmac.pm_samplers("arm64"), cmac.pm_samplers("x86_64")
        self.assertEqual(arm[0], ("thermal", "cpu_power"))
        self.assertFalse([s for s in arm if "smc" in s])                                 # smc is Intel only: it would fail
        self.assertEqual(intel[0], ("smc", "thermal"))
        self.assertIn(("thermal", "cpu_power"), intel)                                   # an Intel Python under Rosetta
        self.assertTrue(all(len(b) <= len(a) for a, b in zip(arm, arm[1:])))             # fewer samplers on each retry

    def test_one_fixed_call_and_fewer_samplers_on_failure(self):
        calls = []

        def run(name, *args, timeout=15):
            calls.append((name,) + args + (timeout,))
            return (0, PM_M1, "") if args[-1] == "cpu_power" else (1, "", "unrecognized sampler: thermal")
        self.assertEqual(cmac.powermetrics(run, "arm64")["pressure"], "Moderate")
        self.assertEqual(calls, [("powermetrics", "-n", "1", "-i", "1000", "--samplers", "thermal,cpu_power", 10),
                                 ("powermetrics", "-n", "1", "-i", "1000", "--samplers", "cpu_power", 10)])

    def test_not_root_or_timeout_does_not_retry(self):
        for answer in ((1, "", "powermetrics must be invoked as the superuser"), (None, "", "TimeoutExpired()")):
            calls = []
            with self.assertRaises(RuntimeError) as e:
                cmac.powermetrics(lambda *a, **k: calls.append(a) or answer, "x86_64")
            self.assertEqual(len(calls), 1)
            self.assertEqual(str(e.exception), answer[2])
        calls = []
        with self.assertRaises(RuntimeError):
            cmac.powermetrics(lambda *a, **k: calls.append(a) or (1, "", "bad"), "arm64")
        self.assertEqual(len(calls), len(cmac.pm_samplers("arm64")))


class TempTools(unittest.TestCase):
    def test_outputs(self):
        for text, want in (("52.3\n", 52.3), ("64\n", 64.0), ("52.3°C\n", 52.3), (" 48.1 °C", 48.1), ("50.0�C\n", 50.0),
                           ("0.0°C\n", 0.0), ("-127.0\n", -127.0), ("125.6°F\n", None), ("", None), ("Fail\n", None),
                           ("**Measurement failed**\n", None), ("52.3\n61.0\n", None)):
            self.assertEqual(cmac.parse_tool_temp(text), want, text)

    def test_fixed_argv_and_failures(self):
        seen = []

        def run(name, *args, timeout=15):
            seen.append((name,) + args)
            return 0, {"smctemp": "52.3\n", "osx-cpu-temp": "52.3°C\n"}[name], ""
        self.assertEqual([cmac.temp_tool(run, t) for t in cmac.TEMP_TOOLS], [52.3, 52.3])
        self.assertEqual(seen, [("smctemp", "-c"), ("osx-cpu-temp",)])
        with self.assertRaises(RuntimeError):
            cmac.temp_tool(lambda *a, **k: (1, "", "no SMC"), "smctemp")
        with self.assertRaises(ValueError):
            cmac.temp_tool(lambda *a, **k: (0, "Unknown model\n", ""), "smctemp")


# ---- Windows: LibreHardwareMonitor, OpenHardwareMonitor, ACPI ---------------------------------------------------------

def hw(name, ident, value):
    return {"n": name, "id": ident, "p": ident.rsplit("/", 2)[0], "v": value}


LHM_INTEL = [hw("CPU Core #1", "/intelcpu/0/temperature/0", 52.0), hw("CPU Core #2", "/intelcpu/0/temperature/1", 55.0),
             hw("CPU Core #3", "/intelcpu/0/temperature/2", 61.0), hw("CPU Core #4", "/intelcpu/0/temperature/3", 58.0),
             hw("CPU Package", "/intelcpu/0/temperature/4", 63.0), hw("Core Max", "/intelcpu/0/temperature/5", 61.0),
             hw("Core Average", "/intelcpu/0/temperature/6", 56.5),
             hw("CPU Core #1 Distance to TjMax", "/intelcpu/0/temperature/7", 48.0),
             hw("GPU Core", "/gpu-nvidia/0/temperature/0", 45.0), hw("Temperature", "/nvme/0/temperature/0", 38.0),
             hw("CPU", "/lpc/nct6798d/temperature/1", 40.0)]
LHM_AMD = [hw("Core (Tctl/Tdie)", "/amdcpu/0/temperature/2", 66.25), hw("CCD1 (Tdie)", "/amdcpu/0/temperature/3", 60.5),
           hw("CCDs Max (Tdie)", "/amdcpu/0/temperature/4", 60.5), hw("GPU Core", "/gpu-amd/0/temperature/0", 50.0)]
OHM = [hw("CPU Core #1", "/intelcpu/0/temperature/0", 47.0), hw("CPU Core #2", "/intelcpu/0/temperature/1", 49.0),
       hw("CPU Package", "/intelcpu/0/temperature/2", 50.0)]
NOT_SUPPORTED = {"error": "Not supported ", "code": "NotSupported", "id": "HRESULT 0x8004100c,Microsoft.Management.Infrastructure.CimCmdlets.GetCimInstanceCommand"}
ABSENT = {"absent": True}


def ps(lhm=ABSENT, ohm=ABSENT, acpi=NOT_SUPPORTED):
    wrap = lambda rows, key: {key: rows} if isinstance(rows, list) else rows  # noqa: E731
    return {"LibreHardwareMonitor": wrap(lhm, "sensors"), "OpenHardwareMonitor": wrap(ohm, "sensors"), "ACPI": wrap(acpi, "zones")}


class WindowsSensors(unittest.TestCase):
    def test_lhm_intel_cores_package_and_details(self):
        r = cwin.hwmon_readings(LHM_INTEL)
        self.assertEqual([(x["label"], x["role"], x.get("core")) for x in r], [
            ("CPU Package", "package", None), ("CPU Core #1", "core", 1), ("CPU Core #2", "core", 2), ("CPU Core #3", "core", 3),
            ("CPU Core #4", "core", 4), ("Core Average", None, None), ("Core Max", None, None),
            ("CPU (board)", None, None)])                                                 # the board's CPU sensor: listed, never the package
        self.assertFalse([x for x in r if "Distance" in x["label"] or "GPU" in x["label"]])

    def test_lhm_amd_tctl_is_the_package(self):
        r = cwin.hwmon_readings(LHM_AMD)
        self.assertEqual([(x["label"], x["role"]) for x in r],
                         [("Core (Tctl/Tdie)", "package"), ("CCD1 (Tdie)", None), ("CCDs Max (Tdie)", None)])

    def test_two_cpus_cores_of_the_first(self):
        rows = OHM + [hw(x["n"], x["id"].replace("/0/", "/1/"), x["v"] + 10) for x in OHM]
        r = cwin.hwmon_readings(rows)
        self.assertEqual([x["role"] for x in r].count("package"), 1)
        self.assertEqual({x["core"]: x["c"] for x in r if x["role"] == "core"}, {1: 47.0, 2: 49.0})
        self.assertIn("CPU Package (CPU 2)", [x["label"] for x in r])

    def test_acpi_zones(self):
        one = cwin.acpi_readings([{"n": "ACPI\\ThermalZone\\TZ00_0", "t": 3132}])
        self.assertEqual(one[0]["label"], "ACPI TZ00_0")
        self.assertAlmostEqual(one[0]["c"], 40.05)                                       # tenths of kelvin
        self.assertEqual(one[0]["role"], "package")                                      # the only reading
        two = cwin.acpi_readings([{"n": "ACPI\\ThermalZone\\TZ00_0", "t": 3132}, {"n": "ACPI\\ThermalZone\\TZ01_0", "t": "x"}])
        self.assertEqual([x["role"] for x in two], [None, None])                         # which one is the CPU: unknown
        self.assertIsNone(two[1]["c"])

    def test_status_of_each_source(self):
        self.assertEqual(cwin.wmi_status(ABSENT, "sensors"), ("absent", []))
        self.assertEqual(cwin.wmi_status(NOT_SUPPORTED, "zones"), ("absent", []))
        self.assertEqual(cwin.wmi_status({"error": "x", "code": "InvalidNamespace", "id": ""}, "sensors"), ("absent", []))
        self.assertEqual(cwin.wmi_status({"error": "", "code": "", "id": "HRESULT 0x80041010,Get"}, "sensors"), ("absent", []))
        self.assertEqual(cwin.wmi_status({"error": "Access denied ", "code": "AccessDenied", "id": "HRESULT 0x80041003"}, "zones"),
                         ("Access denied", []))
        self.assertEqual(cwin.wmi_status({"zones": {"n": "TZ", "t": 3000}}, "zones"), (None, [{"n": "TZ", "t": 3000}]))
        for bad in (None, "x", {"zones": "x"}):
            self.assertEqual(cwin.wmi_status(bad, "zones")[0], "unreadable answer")

    def test_sources_best_first(self):
        src = cwin.sensor_sources(ps(lhm=[hw("GPU Core", "/gpu-nvidia/0/temperature/0", 45.0)], ohm=OHM))
        self.assertEqual([(s, st) for s, st, _ in src], [("LibreHardwareMonitor", "no CPU temperature sensor"),
                                                          ("OpenHardwareMonitor", None), ("ACPI", "absent")])
        self.assertEqual(cwin.sensor_sources(ps(acpi={"zones": []}))[2][1], "absent")  # no thermal zone on this machine
        with self.assertRaises(ValueError):
            cwin.sensor_sources(None)

    def test_script_is_fixed_and_reads_every_source(self):
        for text in ("root/$src", "LibreHardwareMonitor", "OpenHardwareMonitor", "SensorType='Temperature'",
                     "MSAcpi_ThermalZoneTemperature", "ConvertTo-Json", "NativeErrorCode", "FullyQualifiedErrorId"):
            self.assertIn(text, cwin.PS_SENSORS)
        self.assertTrue(cwin.PS_SENSORS.startswith(cwin.PS_PRELUDE))
        self.assertNotIn("Format-", cwin.PS_SENSORS)                                     # objects, never localised text

    @unittest.skipUnless(shutil.which("pwsh") or (sys.platform == "win32" and shutil.which("powershell")), "PowerShell")
    def test_script_parses(self):
        script = cwin.PS_SENSORS.replace("'", "''")
        cmd = ("$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseInput('%s', [ref]$t, [ref]$e); "
               "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }" % script)
        r = subprocess.run([shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ---- the collector: validation, sources in order, contract, feature switch, loop ---------------------------------------

class Collect(unittest.TestCase):
    SAVED = ("LINUX", "MACOS", "WINDOWS", "OS_NAME", "run", "OFF", "collect", "collect_net", "collect_boot", "collect_sensors",
             "write_atomic", "time")

    def setUp(self):
        self.saved = {k: getattr(collector, k) for k in self.SAVED}
        self.saved_mods = {k: getattr(collector, k, None) for k in ("cmac", "cwin")}
        self.saved_machine = collector.platform.machine
        self.calls = []
        collector.OFF = set()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(collector, k, v)
        for k, v in self.saved_mods.items():
            if v is None and hasattr(collector, k):
                delattr(collector, k)
            elif v is not None:
                setattr(collector, k, v)
        collector.platform.machine = self.saved_machine

    def as_os(self, name):
        collector.LINUX, collector.MACOS, collector.WINDOWS, collector.OS_NAME = name == "linux", name == "darwin", name == "windows", name
        collector.cmac, collector.cwin = cmac, cwin

    def as_mac(self, machine, answers):
        """answers: tool -> (rc, out, err) | None (not installed)."""
        self.as_os("darwin")
        collector.platform.machine = lambda: machine

        def run(name, *args, timeout=15):
            self.calls.append((name,) + args)
            a = answers.get(name)
            if a is None:
                raise collector.Absent(name)
            return a(args) if callable(a) else a
        collector.run = run

    def as_windows(self, data, rc=0):
        self.as_os("windows")

        def run(name, *args, timeout=15):
            self.calls.append(name)
            if name != "powershell":
                raise collector.Absent(name)
            self.assertEqual(base64.b64decode(args[-1]).decode("utf-16-le"), cwin.PS_SENSORS)  # the fixed script, nothing else
            return rc, ("﻿" + json.dumps(data) if rc == 0 else ""), ("" if rc == 0 else "boom")
        collector.run = run

    def collect(self):
        return check_contract(self, collector.collect_sensors(), collector.OS_NAME)

    # -- validation

    def test_sane_temperatures(self):
        for v, want in ((45.69, 45.7), (61, 61.0), (-19.5, -19.5), (150, 150.0), (150.1, None), (-25, None), (0.0, None),
                        (0.05, None), (float("nan"), None), (float("inf"), None), (True, None), ("52", None), (None, None)):
            self.assertEqual(collector.sane_c(v), want, v)

    def test_implausible_readings_are_dropped_with_a_note(self):
        errors = {}
        t = collector.cpu_temps("LibreHardwareMonitor", [{"label": "CPU Package", "c": 255.0, "role": "package"},
                                                        {"label": "CPU Core #1", "c": 50.0, "role": "core", "core": 1},
                                                        {"label": "CPU Core #2", "c": float("nan"), "role": "core", "core": 2}], errors)
        self.assertEqual((t["package"], t["cores"], t["source"]), (None, {1: 50.0}, "LibreHardwareMonitor"))
        self.assertIn("CPU Package: 255.0", errors["LibreHardwareMonitor"])
        self.assertIn("CPU Core #2: nan", errors["LibreHardwareMonitor"])
        self.assertIsNone(collector.cpu_temps("x", [], {}))
        self.assertIsNone(collector.cpu_temps("x", [{"label": "a", "c": 999, "role": None}], {}))

    # -- macOS

    def test_mac_intel(self):
        self.as_mac("x86_64", {"powermetrics": (0, PM_INTEL, ""), "smctemp": (0, "99\n", "")})
        d = self.collect()
        self.assertEqual(d["cpu"], {"package": 45.7, "cores": {}, "sensors": [{"label": "CPU die", "c": 45.7}], "source": "powermetrics",
                                    "pressure": "Nominal", "clusters": []})
        self.assertEqual((d["errors"], d["absent"]), ({}, []))
        self.assertEqual(self.calls, [("powermetrics", "-n", "1", "-i", "1000", "--samplers", "smc,thermal")])  # no tool needed

    def test_mac_apple_silicon_with_smctemp(self):
        self.as_mac("arm64", {"powermetrics": (0, PM_M1, ""), "smctemp": (0, "52.3\n", ""), "osx-cpu-temp": (0, "0.0°C\n", "")})
        d = self.collect()
        cpu = d["cpu"]
        self.assertEqual((cpu["package"], cpu["source"], cpu["sensors"]), (52.3, "smctemp", [{"label": "CPU (smctemp)", "c": 52.3}]))
        self.assertEqual((cpu["pressure"], [c["name"] for c in cpu["clusters"]]), ("Moderate", ["E-Cluster", "P-Cluster"]))
        self.assertEqual(d["cpu"]["clusters"][1]["cpus"], {"4": 2064.0, "5": 1863.0, "6": 600.0, "7": 600.0})  # JSON: string keys
        self.assertEqual([c[0] for c in self.calls], ["powermetrics", "smctemp"])          # the first tool that answers is enough
        self.assertEqual(d["errors"], {})

    def test_mac_apple_silicon_osx_cpu_temp_zero_is_no_reading(self):
        self.as_mac("arm64", {"powermetrics": (0, PM_MAX, ""), "osx-cpu-temp": (0, "0.0°C\n", "")})
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["source"], d["cpu"]["sensors"]), (None, None, []))
        self.assertEqual(d["absent"], ["smctemp"])
        self.assertIn("0.0", d["errors"]["osx-cpu-temp"])
        self.assertEqual(len(d["cpu"]["clusters"]), 3)

    def test_mac_apple_silicon_without_tools(self):
        self.as_mac("arm64", {"powermetrics": (0, PM_M1, "")})
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["errors"], d["absent"]), (None, {}, ["smctemp", "osx-cpu-temp"]))
        self.assertEqual(d["cpu"]["pressure"], "Moderate")

    def test_mac_not_root_and_broken_tool(self):
        self.as_mac("arm64", {"powermetrics": (1, "", "powermetrics must be invoked as the superuser"), "smctemp": (1, "", "SMC error"),
                              "osx-cpu-temp": (0, "61.5°C\n", "")})
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["source"], d["cpu"]["pressure"]), (61.5, "osx-cpu-temp", None))
        self.assertEqual(d["errors"], {"powermetrics": "powermetrics must be invoked as the superuser", "smctemp": "SMC error"})

    def test_mac_powermetrics_missing_or_crashing(self):
        self.as_mac("x86_64", {})
        d = self.collect()
        self.assertEqual(d["absent"], ["powermetrics", "smctemp", "osx-cpu-temp"])
        self.assertEqual(d["errors"], {})

        def crash(args):
            raise OSError("exec format error")
        self.as_mac("x86_64", {"powermetrics": crash})
        self.assertIn("exec format error", self.collect()["errors"]["powermetrics"])

    def test_mac_implausible_die_temperature_falls_back_to_the_tools(self):
        self.as_mac("x86_64", {"powermetrics": (0, PM_INTEL.replace("45.69", "175.00"), ""), "smctemp": (0, "47.5\n", "")})
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["source"]), (47.5, "smctemp"))
        self.assertIn("175.0", d["errors"]["powermetrics"])

    def test_powermetrics_runs_as_root_and_the_optional_tools_never_do(self):
        """The real collector.run and mac_identity: Apple's SIP-protected powermetrics as root; smctemp (user-writable
        place) as its owner, or not at all when root owns it and nobody is at the console."""
        if os.name != "posix":
            self.skipTest("POSIX ownership")
        seen = []
        saved = collector.subprocess.run, collector.shutil.which, collector.os.geteuid
        with tempfile.TemporaryDirectory() as tmp:
            tool = os.path.join(tmp, "smctemp")
            open(tool, "w").close()

            class R:
                returncode, stdout, stderr = 0, "50.0\n", ""
            collector.MACOS, collector.LINUX = True, False
            collector.subprocess.run = lambda cmd, **kw: seen.append((cmd, kw)) or R()
            collector.shutil.which = lambda name, path=None: "/usr/bin/powermetrics" if name == "powermetrics" else tool
            collector.os.geteuid = lambda: 0
            try:
                collector.run("powermetrics", "-n", "1")
                try:
                    collector.run("smctemp", "-c")
                except (RuntimeError, OSError):                                           # root-owned (tests run as root) and
                    self.assertEqual(len(seen), 1)                                        # nobody at the console: not run at all
            finally:
                collector.subprocess.run, collector.shutil.which, collector.os.geteuid = saved
        self.assertEqual(seen[0][0], ["/usr/bin/powermetrics", "-n", "1"])
        self.assertFalse({"user", "preexec_fn"} & set(seen[0][1]))                        # Apple's tool: as root
        if len(seen) > 1:
            self.assertEqual(seen[1][0], [tool, "-c"])
            self.assertTrue(seen[1][1].get("user") or seen[1][1].get("preexec_fn"))       # third-party: dropped to its owner
            if "user" in seen[1][1]:
                self.assertNotEqual(seen[1][1]["user"], 0)

    # -- Windows

    def test_windows_lhm(self):
        self.as_windows(ps(lhm=LHM_INTEL, ohm=OHM, acpi={"zones": [{"n": "ACPI\\ThermalZone\\TZ00_0", "t": 3132}]}))
        d = self.collect()
        cpu = d["cpu"]
        self.assertEqual((cpu["package"], cpu["source"]), (63.0, "LibreHardwareMonitor"))
        self.assertEqual(cpu["cores"], {"1": 52.0, "2": 55.0, "3": 61.0, "4": 58.0})
        self.assertEqual([s["label"] for s in cpu["sensors"]][:2], ["CPU Package", "CPU Core #1"])
        self.assertNotIn("ACPI TZ00_0", [s["label"] for s in cpu["sensors"]])            # a fallback only
        self.assertEqual((d["errors"], d["absent"]), ({}, []))
        self.assertEqual(self.calls, ["powershell"])                                     # one call per pass

    def test_windows_ohm_when_lhm_is_absent(self):
        self.as_windows(ps(ohm=OHM))
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["cores"], d["cpu"]["source"]), (50.0, {"1": 47.0, "2": 49.0}, "OpenHardwareMonitor"))
        self.assertEqual(d["absent"], ["LibreHardwareMonitor"])

    def test_windows_amd_lhm(self):
        self.as_windows(ps(lhm=LHM_AMD))
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["cores"], len(d["cpu"]["sensors"])), (66.2, {}, 3))

    def test_windows_acpi_only(self):
        self.as_windows(ps(acpi={"zones": [{"n": "ACPI\\ThermalZone\\TZ00_0", "t": 3132}]}))
        d = self.collect()
        self.assertEqual(d["cpu"]["package"], 40.1)                                     # 313.2 K, at 0.1 precision
        self.assertEqual((d["cpu"]["source"], d["cpu"]["sensors"]), ("ACPI", [{"label": "ACPI TZ00_0", "c": 40.1}]))
        self.assertEqual(d["absent"], ["LibreHardwareMonitor", "OpenHardwareMonitor"])
        self.as_windows(ps(acpi={"zones": [{"n": "ACPI\\ThermalZone\\TZ00_0", "t": 3132}, {"n": "ACPI\\ThermalZone\\TZ01_0", "t": 3232}]}))
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["source"], len(d["cpu"]["sensors"])), (None, "ACPI", 2))

    def test_windows_dummy_acpi_zone_is_no_reading(self):
        self.as_windows(ps(acpi={"zones": [{"n": "ACPI\\ThermalZone\\THRM_0", "t": 2732}]}))      # 273.2 K: a fixed dummy value
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["sensors"]), (None, []))
        self.assertIn("ACPI THRM_0", d["errors"]["ACPI"])

    def test_windows_nothing(self):
        self.as_windows(ps())
        d = self.collect()
        self.assertEqual((d["cpu"]["package"], d["cpu"]["source"], d["errors"]), (None, None, {}))
        self.assertEqual(d["absent"], ["LibreHardwareMonitor", "OpenHardwareMonitor", "ACPI"])  # not installed: no error, no alarm

    def test_windows_errors(self):
        self.as_windows(ps(lhm=[hw("GPU Core", "/gpu-nvidia/0/temperature/0", 45.0)],
                           acpi={"error": "Access denied ", "code": "AccessDenied", "id": "HRESULT 0x80041003"}))
        d = self.collect()
        self.assertEqual(d["errors"], {"LibreHardwareMonitor": "no CPU temperature sensor", "ACPI": "Access denied"})
        self.as_windows(None, rc=1)
        self.assertEqual(self.collect()["errors"], {"powershell": "boom"})
        self.as_windows(None)                                                              # 'null': unreadable, not a crash
        self.assertIn("unreadable", self.collect()["errors"]["powershell"])
        self.as_windows(ps())
        collector.run = lambda name, *a, **k: (_ for _ in ()).throw(collector.Absent(name))
        self.assertEqual(self.collect()["absent"], ["powershell"])

    # -- Linux, feature switch, --once, the loop

    def test_linux_writes_nothing(self):
        self.as_os("linux")
        collector.run = lambda *a, **k: self.fail("nothing to run on Linux")
        self.assertNotIn(collector.sensors_loop, collector.loops())
        collector.collect, collector.collect_net, collector.collect_boot = dict, dict, dict
        self.assertNotIn("sensors", collector.once())
        d = self.collect()                                                                  # a direct call: valid and empty
        self.assertEqual((d["cpu"]["package"], d["errors"], d["absent"]), (None, {}, []))

    def test_mac_and_windows_start_the_sensors_loop(self):
        collector.collect, collector.collect_net, collector.collect_boot = dict, dict, dict
        for name in ("darwin", "windows"):
            self.as_os(name)
            self.assertIn(collector.sensors_loop, collector.loops())
            self.assertIn(collector.net_loop, collector.loops())
            collector.collect_sensors = lambda: {"cpu": "read"}
            self.assertEqual(collector.once()["sensors"], {"cpu": "read"})

    def test_cpu_off_no_thread_and_never_collected(self):
        """Like the other switches (tests/test_nuc_console.py, Config): [features] cpu = no runs nothing at all."""
        collector.collect, collector.collect_net, collector.collect_boot = dict, dict, dict
        for name in ("darwin", "windows"):
            self.as_os(name)
            collector.OFF = {"cpu"}
            collector.run = lambda *a, **k: self.fail("cpu is off: nothing may run")
            collector.collect_sensors = lambda: self.fail("cpu is off: collect_sensors must not be called")
            self.assertNotIn(collector.sensors_loop, collector.loops())
            self.assertIsNone(collector.once()["sensors"])
            collector.collect_sensors = self.saved["collect_sensors"]
            d = self.collect()                                                              # called directly anyway: disabled, nothing run
            self.assertTrue(d["disabled"])
            self.assertEqual((d["cpu"]["package"], d["errors"], d["absent"]), (None, {}, []))

    def test_loop_never_dies_and_writes_sensors_json(self):
        class Stop(BaseException):
            pass
        n, writes, sleeps = [], [], []

        def collect():
            n.append(1)
            if len(n) == 1:
                raise RuntimeError("first pass broken")
            return {"ok": len(n)}

        class FakeTime:
            @staticmethod
            def sleep(s):
                sleeps.append(s)
                if len(sleeps) == 3:
                    raise Stop()
        collector.collect_sensors, collector.time = collect, FakeTime
        collector.write_atomic = lambda data, path: writes.append((data, path))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(Stop):
            collector.sensors_loop()
        self.assertIn("first pass broken", err.getvalue())
        self.assertEqual(writes, [({"ok": 2}, collector.OUT_SENSORS), ({"ok": 3}, collector.OUT_SENSORS)])
        self.assertEqual(sleeps, [collector.SENSORS_INTERVAL_S] * 3)

    def test_state_file_and_interval(self):
        self.assertEqual(collector.OUT_SENSORS, os.path.join(nuc_config.RUN_DIR, "sensors.json"))
        self.assertEqual(collector.SENSORS_INTERVAL_S, 30 if collector.WINDOWS else 10)  # PowerShell per reading on Windows
        self.assertIn("cpu", nuc_config.FEATURES)


if __name__ == "__main__":
    unittest.main()
