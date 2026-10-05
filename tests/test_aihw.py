"""What the machine has and whether a model fits (src/aihw.py): fixtures of every OS, every verdict boundary, the contract shape.

Everything runs on every OS: the hardware comes from a fake `run` (tools) and `read` (files, registry values, API text) that
serve the fixtures below; only `RealHost` runs the real detect() of the OS the tests run on, and checks nothing but its shape.
Machine names and figures are made up (or typical of the machine they stand for)."""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import aihw  # noqa: E402

# ---- fixtures: Linux --------------------------------------------------------------------------------------------------------
MEMINFO = """MemTotal:       32768000 kB
MemFree:         1024000 kB
MemAvailable:   24576000 kB
Buffers:          204800 kB
Cached:         20000000 kB
SwapTotal:       2097148 kB
"""
MEMINFO_OLD = "MemTotal:        4096000 kB\nMemFree:         1000000 kB\nBuffers:          100000 kB\nCached:           900000 kB\n"
CPUINFO_X86 = "".join(
    f"processor\t: {i}\nvendor_id\t: GenuineIntel\nmodel name\t: Intel(R) Core(TM) i5-8259U CPU @ 2.30GHz\nphysical id\t: 0\n"
    f"core id\t\t: {i % 4}\nflags\t\t: fpu vme sse4_2 avx fma avx2 bmi2 ht\n\n" for i in range(8))
CPUINFO_AVX512 = ("processor\t: 0\nmodel name\t: Intel(R) Xeon(R) Gold\nphysical id\t: 0\ncore id\t\t: 0\n"
                  "flags\t\t: fpu avx avx2 avx512f avx512vl\n")
CPUINFO_ARM = "".join(f"processor\t: {i}\nBogoMIPS\t: 108.00\nFeatures\t: fp asimd evtstrm crc32 cpuid\nCPU implementer\t: 0x41\n"
                      f"CPU part\t: 0xd08\n\n" for i in range(4))
NVIDIA_CSV = "NVIDIA GeForce RTX 3060, 12288, 11800\nNVIDIA GeForce GTX 1050 Ti, 4096, 3900\n"


def sysfs(n, vendor, device, **extra):
    """The files of /sys/class/drm/card<n> a driver would expose."""
    base = f"/sys/class/drm/card{n}/"
    files = {base + "device/vendor": vendor + "\n", base + "device/device": device + "\n"}
    files.update({base + k.replace("__", "/"): v for k, v in extra.items()})
    return files


# ---- fixtures: macOS --------------------------------------------------------------------------------------------------------
SYSCTL_M2 = """hw.memsize: 17179869184
machdep.cpu.brand_string: Apple M2 Pro
hw.ncpu: 10
hw.physicalcpu: 10
hw.optional.arm64: 1
"""
SYSCTL_INTEL = """hw.memsize: 17179869184
machdep.cpu.brand_string: Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz
hw.ncpu: 12
hw.physicalcpu: 6
machdep.cpu.features: FPU VME DE PSE TSC MSR PAE MCE CX8 APIC SEP MTRR PGE SSE3 FMA CX16 SSE4.2 AES XSAVE OSXSAVE AVX1.0 RDRAND F16C
machdep.cpu.leaf7_features: RDWRFSGS TSC_THREAD_OFFSET SGX BMI1 AVX2 SMEP BMI2 ERMS INVPCID FPU_CSDS MPX RDSEED ADX SMAP CLFSOPT
"""
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               50000.
Pages active:                            300000.
Pages inactive:                          250000.
Pages speculative:                        10000.
Pages wired down:                        100000.
"""
PROFILER = json.dumps({"SPDisplaysDataType": [
    {"_name": "Intel UHD Graphics 630", "spdisplays_vendor": "sppci_vendor_Intel", "sppci_model": "Intel UHD Graphics 630",
     "spdisplays_vram_shared": "1536 MB", "sppci_bus": "spdisplays_builtin"},
    {"_name": "Radeon Pro 560X", "spdisplays_vendor": "sppci_vendor_AMD", "sppci_model": "Radeon Pro 560X",
     "spdisplays_vram": "4 GB", "spdisplays_metal": "spdisplays_supported"}]})

# ---- fixtures: Windows ------------------------------------------------------------------------------------------------------
CLS = aihw.DISPLAY_CLASS


def reg(n, desc, devid, mem=None):
    """Registry values of display adapter 000<n> (HardwareInformation.qwMemorySize in bytes, as the injected reader gives it)."""
    key = "reg:%s\\%04d\\" % (CLS, n)
    out = {key + "DriverDesc": desc, key + "MatchingDeviceId": devid}
    if mem is not None:
        out[key + "HardwareInformation.qwMemorySize"] = str(mem)
    return out


WIN_BASE = {"api:memory": "total=17179869184 available=8589934592", "api:cpu": "threads=16 cores=8 avx=1 avx2=1 avx512=0",
            "reg:" + aihw.CPU_KEY + "\\ProcessorNameString": "AMD Ryzen 7 5800H with Radeon Graphics        "}


# The fixtures describe x86_64 machines unless a test says otherwise (it patches platform.machine itself): the real host's
# architecture (an arm64 macOS runner) must not leak into them.
X86_HOST = mock.patch.object(aihw.platform, "machine", lambda: "x86_64")


class Fake(object):
    """A fake machine: `files` (path -> text, or an Exception to raise) and `tools` (program name -> (rc, out) or a callable
    taking argv). A program that is not listed cannot be started (rc None); every call is recorded."""

    def __init__(self, files=None, tools=None):
        self.files, self.tools, self.calls, self.reads = dict(files or {}), dict(tools or {}), [], []

    def read(self, path):
        self.reads.append(path)
        v = self.files.get(path)
        if v is None:
            raise FileNotFoundError(path)
        if isinstance(v, Exception):
            raise v
        return v

    def run(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        r = self.tools.get(re.split(r"[\\/]", argv[0])[-1])
        r = r(argv) if callable(r) else r
        if isinstance(r, Exception):
            raise r
        return r if r is not None else (None, "")

    def detect(self, plat):
        return aihw.detect(plat, self.run, self.read)


def linux_box(**kw):
    files = {"/proc/meminfo": MEMINFO, "/proc/cpuinfo": CPUINFO_X86}
    files.update(kw.pop("files", {}))
    return Fake(files, kw.pop("tools", {}))


# ---- the contract's shape ---------------------------------------------------------------------------------------------------
def opt_int(v):
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)


def check_shape(tc, hw):
    """The shape of detect() as the contract (feat/ai, section 1) defines it."""
    tc.assertEqual(set(hw), {"os", "arch", "cpu", "ram", "gpus", "notes"})
    tc.assertIsInstance(hw["os"], str)
    tc.assertIsInstance(hw["arch"], str)
    tc.assertEqual(set(hw["cpu"]), {"model", "cores", "threads", "flags"})
    tc.assertTrue(hw["cpu"]["model"] is None or (isinstance(hw["cpu"]["model"], str) and hw["cpu"]["model"]))
    tc.assertTrue(opt_int(hw["cpu"]["cores"]) and opt_int(hw["cpu"]["threads"]))
    tc.assertIsInstance(hw["cpu"]["flags"], list)
    tc.assertTrue(set(hw["cpu"]["flags"]) <= set(aihw.FLAG_ORDER), hw["cpu"]["flags"])
    tc.assertEqual(set(hw["ram"]), {"total_mb", "available_mb"})
    tc.assertTrue(opt_int(hw["ram"]["total_mb"]) and opt_int(hw["ram"]["available_mb"]))
    tc.assertIsInstance(hw["gpus"], list)
    for g in hw["gpus"]:
        tc.assertEqual(set(g), {"vendor", "name", "vram_mb", "vram_free_mb", "unified", "backend", "source"})
        tc.assertIn(g["vendor"], ("nvidia", "amd", "intel", "apple", "other"))
        tc.assertTrue(isinstance(g["name"], str) and g["name"])
        tc.assertTrue(opt_int(g["vram_mb"]) and opt_int(g["vram_free_mb"]))
        tc.assertIsInstance(g["unified"], bool)
        tc.assertIn(g["backend"], ("cuda", "rocm", "metal", "vulkan", "none"))
        tc.assertIn(g["source"], ("nvidia-smi", "sysfs", "system_profiler", "registry", "wmi", "sysctl"))
        if g["vram_mb"] is not None and g["vram_free_mb"] is not None:
            tc.assertLessEqual(g["vram_free_mb"], g["vram_mb"])
    tc.assertIsInstance(hw["notes"], list)
    tc.assertTrue(all(isinstance(n, str) and 0 < len(n) <= 120 for n in hw["notes"]), hw["notes"])
    tc.assertEqual(len(hw["notes"]), len(set(hw["notes"])))
    json.dumps(hw)  # plain data: the screens and the web view serialise it


def check_calls(tc, fake):
    """Tools are run with a fixed argument list of strings, a timeout of 5 s at most, and never through sudo or a shell."""
    for argv, timeout in fake.calls:
        tc.assertIsInstance(argv, list)
        tc.assertTrue(all(isinstance(a, str) for a in argv), argv)
        tc.assertLessEqual(timeout, 5)
        tc.assertNotIn(re.split(r"[\\/]", argv[0])[-1], ("sudo", "sh", "bash", "cmd", "cmd.exe", "powershell", "doas"))


# ---- models and machines for the verdicts -----------------------------------------------------------------------------------
def model(**kw):
    """need_mb(model, 0) = 1300: 1000 of weights + 300 of runtime, no cache at 0 tokens."""
    m = {"id": "m", "approx_mb": 1000, "layers": 32}
    m.update(kw)
    return m


def box(total=None, avail=None, gpus=()):
    return {"ram": {"total_mb": total, "available_mb": avail}, "gpus": list(gpus)}


def nv(vram, free=None, name="NVIDIA GeForce RTX 3060"):
    return {"vendor": "nvidia", "name": name, "vram_mb": vram, "vram_free_mb": free, "unified": False, "backend": "cuda",
            "source": "nvidia-smi"}


def apple(name="Apple M2 GPU"):
    return {"vendor": "apple", "name": name, "vram_mb": 16384, "vram_free_mb": 10000, "unified": True, "backend": "metal",
            "source": "sysctl"}


def verdict(m, hw, ctx=0):
    return aihw.assess(m, hw, ctx)["verdict"]


# =============================================================================================================================
IOREG_M1 = """+-o AGXAcceleratorG13X  <class AGXAcceleratorG13X, id 0x1000002a0, registered, matched, active, busy 0 (0 ms), retain 104>
    {
      "IOClass" = "AGXAcceleratorG13X"
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=2348810240,"Tiler Utilization %"=3,"Renderer Utilization %"=11,"Device Utilization %"=12,"In use system memory"=578813952}
      "model" = "Apple M1 Pro"
    }
"""
IOREG_INTEL = """+-o AMDRadeonX6000_AMDNavi14GraphicsAccelerator  <class AMDRadeonX6000_AMDNavi14GraphicsAccelerator, id 0x100000abc>
    {
      "PerformanceStatistics" = {"vramUsedBytes"=1073741824,"GPU Activity(%)"=7,"VRAM,totalMB"=4096}
    }
+-o IntelAccelerator  <class IntelAccelerator, id 0x100000def>
    {
      "IOClass" = "IntelAccelerator"
    }
"""


class GpuLoad(unittest.TestCase):
    """How busy the GPU is now (the AI page's MODEL USAGE): every OS's source, from fixtures."""

    def test_nvidia_smi(self):
        rows = aihw.parse_nvidia_load("NVIDIA GeForce RTX 3060, 45, 5210, 12288\nTesla T4, [N/A], 10, [Not Supported]\nnot csv\n")
        self.assertEqual(rows, [{"name": "NVIDIA GeForce RTX 3060", "busy_pct": 45.0, "used_mb": 5210.0, "total_mb": 12288.0},
                                {"name": "Tesla T4", "busy_pct": None, "used_mb": 10.0, "total_mb": None}])
        self.assertEqual(aihw.parse_nvidia_load(""), [])
        self.assertEqual(aihw.parse_nvidia_load(None), [])

    def test_ioreg(self):
        self.assertEqual(aihw.parse_ioreg_load(IOREG_M1), [{"name": "Apple M1 Pro", "busy_pct": 12.0, "used_mb": 552.0, "total_mb": None}])
        self.assertEqual(aihw.parse_ioreg_load(IOREG_INTEL), [{"name": "GPU", "busy_pct": 7.0, "used_mb": 1024.0, "total_mb": 4096.0}])
        self.assertEqual(aihw.parse_ioreg_load("garbage"), [])

    def test_each_os_asks_its_own_source_with_a_fixed_argument_list(self):
        seen = []

        def run(out, rc=0):
            def f(argv, timeout):
                seen.append((tuple(argv), timeout))
                return rc, out
            return f
        nope = lambda p: (_ for _ in ()).throw(OSError(p))  # noqa: E731
        got = aihw.gpu_load("linux", run("RTX, 45, 5210, 12288"), nope)
        self.assertEqual((got["source"], got["gpus"][0]["busy_pct"]), ("nvidia-smi", 45.0))
        self.assertEqual(seen[-1], (("nvidia-smi",) + tuple(aihw.NVIDIA_LOAD), aihw.LOAD_TIMEOUT))
        got = aihw.gpu_load("darwin", run(IOREG_M1), nope)
        self.assertEqual((got["source"], got["gpus"][0]["name"]), ("ioreg", "Apple M1 Pro"))
        self.assertEqual(seen[-1][0], tuple(aihw.IOREG))
        self.assertEqual(aihw.gpu_load("darwin", run("", 1), nope)["gpus"], [])
        amd = {"/sys/class/drm/card1/device/vendor": "0x1002\n", "/sys/class/drm/card1/device/gpu_busy_percent": "37\n",
               "/sys/class/drm/card1/device/mem_info_vram_used": str(3 * 2 ** 30), "/sys/class/drm/card1/device/mem_info_vram_total": str(16 * 2 ** 30)}
        got = aihw.gpu_load("linux", lambda a, t: (None, ""), lambda p: amd[p])
        self.assertEqual(got, {"gpus": [{"name": "AMD GPU", "busy_pct": 37.0, "used_mb": 3072, "total_mb": 16384}], "source": "sysfs", "note": ""})
        n = len(seen)
        got = aihw.gpu_load("windows", run("", None), nope)
        self.assertEqual((got["gpus"], got["source"]), ([], None))
        self.assertEqual([a[0][0] for a in seen[n:]][-1].lower()[-14:], "nvidia-smi.exe", "the driver's own folder is tried too")
        self.assertIn("NVIDIA", got["note"])

    def test_it_never_raises(self):
        def boom(*a):
            raise RuntimeError("x")
        got = aihw.gpu_load("linux", boom, boom)
        self.assertEqual((got["gpus"], got["source"]), ([], None))


class Parsers(unittest.TestCase):
    def test_meminfo(self):
        self.assertEqual(aihw.parse_meminfo(MEMINFO), (32000, 24000))
        # no MemAvailable (kernels before 3.14): free + buffers + cached
        self.assertEqual(aihw.parse_meminfo(MEMINFO_OLD), (4000, (1000000 + 100000 + 900000) // 1024))
        self.assertEqual(aihw.parse_meminfo("MemTotal: 4096 kB\nMemAvailable: 9000 kB\n"), (4, 4))  # never more than the total

    def test_meminfo_garbage(self):
        self.assertEqual(aihw.parse_meminfo(""), (None, None))
        self.assertEqual(aihw.parse_meminfo("nonsense\nMemFree: lots\n"), (None, None))
        self.assertEqual(aihw.parse_meminfo(None), (None, None))
        self.assertEqual(aihw.parse_meminfo("MemTotal: 2048 kB\n"), (2, None))  # no figure for free memory: None, not 0

    def test_cpu_flags(self):
        self.assertEqual(aihw.parse_cpu_flags(CPUINFO_X86), ["avx", "avx2", "fma"])
        self.assertEqual(aihw.parse_cpu_flags(CPUINFO_AVX512), ["avx", "avx2", "avx512"])
        self.assertEqual(aihw.parse_cpu_flags(CPUINFO_ARM), ["neon"])  # asimd is NEON on aarch64
        self.assertEqual(aihw.parse_cpu_flags("Features: half thumb fastmult vfp neon vfpv3\n"), ["neon"])  # arm32
        self.assertEqual(aihw.parse_cpu_flags("Features: fp asimd sve sve2\n"), ["neon", "sve"])
        self.assertEqual(aihw.parse_cpu_flags(""), [])
        self.assertEqual(aihw.parse_cpu_flags("model name: x\n"), [])

    def test_nvidia_smi(self):
        self.assertEqual(aihw.parse_nvidia_smi(NVIDIA_CSV), [("NVIDIA GeForce RTX 3060", 12288, 11800),
                                                             ("NVIDIA GeForce GTX 1050 Ti", 4096, 3900)])
        # a value the driver cannot give is None, other lines (warnings, errors) are skipped
        out = "WARNING: something\nNVIDIA A2, 15356, [N/A]\nNVIDIA T4, [Not Supported], [Not Supported]\nNo devices were found\n"
        self.assertEqual(aihw.parse_nvidia_smi(out), [("NVIDIA A2", 15356, None), ("NVIDIA T4", None, None)])
        self.assertEqual(aihw.parse_nvidia_smi(""), [])
        self.assertEqual(aihw.parse_nvidia_smi("NVIDIA-SMI has failed because it couldn't communicate with the driver"), [])
        self.assertEqual(aihw.parse_nvidia_smi("Quadro, with comma, 8192, 8000\n"), [("Quadro, with comma", 8192, 8000)])

    def test_sysctl(self):
        self.assertEqual(aihw.parse_sysctl(SYSCTL_M2)["machdep.cpu.brand_string"], "Apple M2 Pro")
        self.assertEqual(aihw.parse_sysctl(SYSCTL_M2)["hw.memsize"], "17179869184")
        self.assertEqual(aihw.parse_sysctl("sysctl: unknown oid 'x'\n"), {})
        self.assertEqual(aihw.parse_sysctl(""), {})

    def test_size(self):
        self.assertEqual([aihw._size_mb(t) for t in ("4 GB", "1536 MB", "0.5 GB", "1 TB", "16 gb")], [4096, 1536, 512, 1048576, 16384])
        self.assertIsNone(aihw._size_mb("Dynamic, Max"))
        self.assertIsNone(aihw._size_mb(None))

    def test_displays(self):
        g = aihw.parse_displays(PROFILER)
        self.assertEqual([x["name"] for x in g], ["Intel UHD Graphics 630", "Radeon Pro 560X"])
        igpu, dgpu = g
        self.assertEqual((igpu["vendor"], igpu["unified"], igpu["vram_mb"], igpu["backend"]), ("intel", True, None, "none"))
        self.assertEqual((dgpu["vendor"], dgpu["unified"], dgpu["vram_mb"], dgpu["backend"], dgpu["source"]),
                         ("amd", False, 4096, "metal", "system_profiler"))
        self.assertIsNone(dgpu["vram_free_mb"])  # the profiler does not tell what is free
        for bad in ("", "not json", "[]", '{"SPDisplaysDataType": "x"}', '{"SPDisplaysDataType": [1, null]}'):
            self.assertEqual(aihw.parse_displays(bad), [], bad)

    def test_vendor_of(self):
        self.assertEqual(aihw._vendor_of("x", "PCI\\VEN_10DE&DEV_2504&SUBSYS_1"), "nvidia")
        self.assertEqual(aihw._vendor_of("x", "pci\\ven_1002&dev_73bf"), "amd")
        self.assertEqual(aihw._vendor_of("x", "pci\\ven_8086&dev_46a6"), "intel")
        self.assertEqual(aihw._vendor_of("NVIDIA Quadro P620", ""), "nvidia")
        self.assertEqual(aihw._vendor_of("AMD Radeon RX 6700 XT", None), "amd")
        self.assertEqual(aihw._vendor_of("Microsoft Basic Display Adapter", "root\\basicdisplay"), "other")

    def test_adapter_gpu(self):
        gib = 2 ** 30
        g = aihw.adapter_gpu("NVIDIA GeForce RTX 3060", "pci\\ven_10de&dev_2504", 12 * gib)
        self.assertEqual((g["vendor"], g["vram_mb"], g["vram_free_mb"], g["backend"], g["unified"], g["source"]),
                         ("nvidia", 12288, None, "cuda", False, "registry"))
        g = aihw.adapter_gpu("AMD Radeon RX 6700 XT", "pci\\ven_1002&dev_73df", 12 * gib)  # > 4 GB: only the 64-bit value is right
        self.assertEqual((g["vendor"], g["vram_mb"], g["backend"], g["unified"]), ("amd", 12288, "vulkan", False))
        g = aihw.adapter_gpu("AMD Radeon(TM) Graphics", "pci\\ven_1002&dev_1638", 512 * 2 ** 20)  # an APU's carve-out is not VRAM
        self.assertEqual((g["vram_mb"], g["unified"], g["backend"]), (None, True, "none"))
        g = aihw.adapter_gpu("Intel(R) UHD Graphics 770", "pci\\ven_8086&dev_4680", 128 * 2 ** 20)
        self.assertEqual((g["vendor"], g["vram_mb"], g["unified"]), ("intel", None, True))
        g = aihw.adapter_gpu("Intel(R) Arc(TM) Graphics", "pci\\ven_8086&dev_7d55", 128 * 2 ** 20)  # Meteor Lake: integrated
        self.assertTrue(g["unified"])
        g = aihw.adapter_gpu("Intel(R) Arc(TM) A770 Graphics", "pci\\ven_8086&dev_56a0", 16 * gib)
        self.assertEqual((g["vram_mb"], g["unified"], g["backend"]), (16384, False, "vulkan"))
        self.assertIsNone(aihw.adapter_gpu("Microsoft Basic Display Adapter", "root\\basicdisplay", None))
        g = aihw.adapter_gpu("NVIDIA GeForce RTX 3060", "pci\\ven_10de", None)  # memory unknown: still a GPU, but not a usable one
        self.assertEqual((g["vram_mb"], g["backend"]), (None, "none"))


@X86_HOST
class DetectLinux(unittest.TestCase):
    def test_cpu_and_ram(self):
        f = linux_box()
        hw = f.detect("linux")
        check_shape(self, hw)
        check_calls(self, f)
        self.assertEqual(hw["os"], "linux")
        self.assertEqual(hw["cpu"], {"model": "Intel(R) Core(TM) i5-8259U CPU @ 2.30GHz", "cores": 4, "threads": 8,
                                     "flags": ["avx", "avx2", "fma"]})
        self.assertEqual(hw["ram"], {"total_mb": 32000, "available_mb": 24000})
        self.assertEqual((hw["gpus"], hw["notes"]), ([], []))  # no GPU, and a missing nvidia-smi is no news without NVIDIA hardware

    def test_nvidia_smi(self):
        f = linux_box(tools={"nvidia-smi": (0, NVIDIA_CSV)})
        hw = f.detect("linux")
        check_shape(self, hw)
        check_calls(self, f)
        self.assertEqual(f.calls, [(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], 5)])
        self.assertEqual([(g["name"], g["vram_mb"], g["vram_free_mb"]) for g in hw["gpus"]],
                         [("NVIDIA GeForce RTX 3060", 12288, 11800), ("NVIDIA GeForce GTX 1050 Ti", 4096, 3900)])  # biggest first
        g = hw["gpus"][0]
        self.assertEqual((g["vendor"], g["unified"], g["backend"], g["source"]), ("nvidia", False, "cuda", "nvidia-smi"))

    def test_nvidia_smi_failures(self):
        # the driver is not loaded: rc 9 and a message: one note, and the sysfs card is still listed (memory unknown)
        f = linux_box(files=sysfs(0, "0x10de", "0x2504"), tools={"nvidia-smi": (9, "NVIDIA-SMI has failed because it couldn't")})
        hw = f.detect("linux")
        check_shape(self, hw)
        self.assertIn("nvidia-smi failed (rc 9)", hw["notes"])
        self.assertEqual([(g["vendor"], g["vram_mb"], g["backend"], g["source"]) for g in hw["gpus"]], [("nvidia", None, "none", "sysfs")])
        # a clean exit with nothing to parse
        hw = linux_box(tools={"nvidia-smi": (0, "No devices were found\n")}).detect("linux")
        self.assertEqual((hw["gpus"], hw["notes"]), ([], ["nvidia-smi: no GPU in its output"]))
        # a tool that raises (an injected runner may do anything): not found, no exception
        hw = linux_box(tools={"nvidia-smi": OSError("boom")}).detect("linux")
        self.assertEqual(hw["gpus"], [])

    def test_gpu_that_does_not_report_its_memory(self):
        hw = linux_box(tools={"nvidia-smi": (0, "NVIDIA GB10, [N/A], [N/A]\n")}).detect("linux")
        check_shape(self, hw)
        self.assertEqual((hw["gpus"][0]["vram_mb"], hw["gpus"][0]["vram_free_mb"]), (None, None))
        self.assertEqual(hw["notes"], ["GB10: nvidia-smi does not report its memory"])
        self.assertEqual(verdict(model(), box(16384, 12000, hw["gpus"]), 0), "ram")  # nothing to compare: the CPU rules

    def test_nvidia_card_without_the_tool(self):
        hw = linux_box(files=sysfs(0, "0x10de", "0x2504")).detect("linux")
        check_shape(self, hw)
        self.assertEqual(len(hw["gpus"]), 1)
        self.assertEqual(hw["gpus"][0]["name"], "NVIDIA GPU 10de:2504")
        self.assertTrue(any("nvidia-smi not found" in n for n in hw["notes"]), hw["notes"])

    def test_nvidia_smi_wins_over_sysfs(self):
        hw = linux_box(files=sysfs(0, "0x10de", "0x2504"), tools={"nvidia-smi": (0, "NVIDIA GeForce RTX 3060, 12288, 11800\n")}
                       ).detect("linux")
        self.assertEqual([g["source"] for g in hw["gpus"]], ["nvidia-smi"])  # one card, not two
        self.assertEqual(hw["notes"], [])

    def test_amd_sysfs(self):
        files = sysfs(0, "0x1002", "0x73bf", device__mem_info_vram_total=str(16 * 2 ** 30) + "\n",
                      device__mem_info_vram_used=str(2 ** 30) + "\n")
        hw = linux_box(files=files).detect("linux")
        check_shape(self, hw)
        g = hw["gpus"][0]
        self.assertEqual((g["vendor"], g["name"], g["vram_mb"], g["vram_free_mb"], g["unified"], g["backend"], g["source"]),
                         ("amd", "AMD GPU 1002:73bf", 16384, 15360, False, "rocm", "sysfs"))

    def test_amd_name_from_sysfs(self):
        files = sysfs(1, "0x1002", "0x73bf", device__mem_info_vram_total=str(8 * 2 ** 30), device__product_name="  AMD Radeon  RX 6600 \n")
        hw = linux_box(files=files).detect("linux")
        self.assertEqual((hw["gpus"][0]["name"], hw["gpus"][0]["vram_free_mb"]), ("AMD Radeon RX 6600", None))  # card1 is probed too

    def test_amd_apu_is_shared_memory(self):
        files = sysfs(0, "0x1002", "0x1638", device__mem_info_vram_total=str(512 * 2 ** 20), device__mem_info_vram_used=str(100 * 2 ** 20))
        g = linux_box(files=files).detect("linux")["gpus"][0]
        self.assertEqual((g["vram_mb"], g["unified"], g["backend"]), (None, True, "none"))

    def test_amd_unreadable_memory(self):
        hw = linux_box(files=sysfs(0, "0x1002", "0x73bf")).detect("linux")
        check_shape(self, hw)
        self.assertEqual((hw["gpus"][0]["vram_mb"], hw["gpus"][0]["backend"]), (None, "none"))
        self.assertEqual(len(hw["notes"]), 1)

    def test_intel_igpu_and_arc(self):
        hw = linux_box(files=sysfs(0, "0x8086", "0x46a6")).detect("linux")
        g = hw["gpus"][0]
        self.assertEqual((g["vendor"], g["name"], g["vram_mb"], g["unified"], g["backend"]),
                         ("intel", "Intel GPU 8086:46a6", None, True, "none"))
        files = sysfs(0, "0x8086", "0x56a0", lmem_total_bytes=str(16 * 2 ** 30), lmem_avail_bytes=str(15 * 2 ** 30))
        g = linux_box(files=files).detect("linux")["gpus"][0]
        self.assertEqual((g["vram_mb"], g["vram_free_mb"], g["unified"], g["backend"]), (16384, 15360, False, "vulkan"))

    def test_virtual_adapters_are_not_gpus(self):
        hw = linux_box(files=sysfs(0, "0x1af4", "0x1050")).detect("linux")  # virtio, ASPEED BMC, VMware: nothing to run a model on
        self.assertEqual(hw["gpus"], [])

    def test_dedicated_gpus_come_first(self):
        files = sysfs(0, "0x8086", "0x46a6")
        files.update(sysfs(1, "0x1002", "0x73bf", device__mem_info_vram_total=str(8 * 2 ** 30)))
        hw = linux_box(files=files, tools={"nvidia-smi": (0, "NVIDIA GeForce RTX 3060, 12288, 11800\n")}).detect("linux")
        self.assertEqual([g["vendor"] for g in hw["gpus"]], ["nvidia", "amd", "intel"])

    def test_arm_has_no_core_ids(self):
        with mock.patch.object(aihw.platform, "machine", return_value="aarch64"):
            hw = linux_box(files={"/proc/cpuinfo": CPUINFO_ARM}).detect("linux")
        check_shape(self, hw)
        self.assertEqual((hw["arch"], hw["cpu"]["cores"], hw["cpu"]["threads"], hw["cpu"]["flags"]), ("arm64", 4, 4, ["neon"]))
        self.assertEqual(hw["cpu"]["model"], "Cortex-A72")

    def test_unreadable_files_become_notes(self):
        f = Fake(files={"/proc/meminfo": PermissionError("denied")})
        hw = f.detect("linux")
        check_shape(self, hw)
        self.assertEqual(hw["ram"], {"total_mb": None, "available_mb": None})
        self.assertEqual(hw["cpu"]["model"], None)
        self.assertEqual(sorted(n.split(":")[0] for n in hw["notes"]), ["CPU", "RAM"])
        self.assertTrue(all("unreadable" in n for n in hw["notes"]))

    def test_garbage_files(self):
        f = linux_box(files={"/proc/meminfo": "\x00\x01 garbage", "/proc/cpuinfo": "???"}, tools={"nvidia-smi": (0, "\x00\xff???")})
        hw = f.detect("linux")
        check_shape(self, hw)
        self.assertEqual(hw["ram"]["total_mb"], None)

    def test_reader_returning_junk(self):
        f = Fake()
        f.read = lambda path: 42  # not text
        check_shape(self, aihw.detect("linux", f.run, f.read))


@X86_HOST
class DetectDarwin(unittest.TestCase):
    def mac(self, sysctl, extra=None):
        tools = {"sysctl": (0, sysctl), "vm_stat": (0, VM_STAT)}
        tools.update(extra or {})
        return Fake(tools=tools)

    def test_apple_silicon_is_unified_memory(self):
        f = self.mac(SYSCTL_M2, {"sysctl": (1, SYSCTL_M2)})  # rc 1: the Intel-only names are unknown on arm64, that is not an error
        hw = f.detect("darwin")
        check_shape(self, hw)
        check_calls(self, f)
        self.assertEqual((hw["os"], hw["arch"]), ("darwin", "arm64"))
        self.assertEqual(hw["cpu"], {"model": "Apple M2 Pro", "cores": 10, "threads": 10, "flags": ["neon"]})
        # (50000 + 250000 + 10000 free, inactive, speculative pages) x 16 KiB
        self.assertEqual(hw["ram"], {"total_mb": 16384, "available_mb": 310000 * 16384 // 2 ** 20})
        g = hw["gpus"]
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0]["vendor"], g[0]["name"], g[0]["unified"], g[0]["backend"], g[0]["source"]),
                         ("apple", "Apple M2 Pro GPU", True, "metal", "sysctl"))
        self.assertEqual((g[0]["vram_mb"], g[0]["vram_free_mb"]), (16384, hw["ram"]["available_mb"]))  # the GPU is the RAM
        self.assertEqual([c[0][0] for c in f.calls], ["/usr/sbin/sysctl", "/usr/bin/vm_stat"])  # absolute paths, no profiler
        self.assertEqual(hw["notes"], [])

    def test_intel_mac_with_discrete_gpu(self):
        f = self.mac(SYSCTL_INTEL, {"system_profiler": (0, PROFILER)})
        hw = f.detect("darwin")
        check_shape(self, hw)
        check_calls(self, f)
        self.assertEqual(hw["cpu"], {"model": "Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz", "cores": 6, "threads": 12,
                                     "flags": ["avx", "avx2", "fma"]})
        self.assertEqual([(g["name"], g["vram_mb"], g["unified"], g["backend"]) for g in hw["gpus"]],
                         [("Radeon Pro 560X", 4096, False, "metal"), ("Intel UHD Graphics 630", None, True, "none")])
        self.assertIn(["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"], [c[0] for c in f.calls])

    def test_failures_are_notes(self):
        hw = Fake().detect("darwin")  # nothing runs
        check_shape(self, hw)
        self.assertEqual(hw["notes"], ["sysctl not available"])
        hw = self.mac(SYSCTL_INTEL, {"vm_stat": (None, ""), "system_profiler": (0, "not json")}).detect("darwin")
        check_shape(self, hw)
        self.assertIsNone(hw["ram"]["available_mb"])
        self.assertEqual(hw["ram"]["total_mb"], 16384)
        self.assertEqual(len(hw["notes"]), 2)
        hw = self.mac("sysctl: unknown oid\n", {"sysctl": (1, "sysctl: unknown oid\n")}).detect("darwin")
        self.assertEqual(hw["notes"], ["sysctl failed (rc 1)"])

    def test_no_nvidia_smi_on_a_mac(self):
        f = self.mac(SYSCTL_M2)
        f.detect("darwin")
        self.assertFalse(any("nvidia" in c[0][0] for c in f.calls))


@X86_HOST
class DetectWindows(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ProgramFiles": "C:\\Program Files"})
        env.start()
        self.addCleanup(env.stop)

    def win(self, adapters=(), tools=None, extra=None):
        files = dict(WIN_BASE)
        for i, a in enumerate(adapters):
            files.update(reg(i, *a))
        files.update(extra or {})
        return Fake(files, tools or {})

    def test_cpu_and_ram(self):
        f = self.win()
        hw = f.detect("win32")
        check_shape(self, hw)
        self.assertEqual(hw["os"], "windows")
        self.assertEqual(hw["cpu"], {"model": "AMD Ryzen 7 5800H with Radeon Graphics", "cores": 8, "threads": 16,
                                     "flags": ["avx", "avx2"]})
        self.assertEqual(hw["ram"], {"total_mb": 16384, "available_mb": 8192})
        self.assertEqual(hw["gpus"], [])
        self.assertEqual(hw["notes"], ["no display adapter with a memory size in the registry"])

    def test_nvidia_smi_on_path(self):
        adapters = [("NVIDIA GeForce RTX 3060", "PCI\\VEN_10DE&DEV_2504", 12 * 2 ** 30)]
        f = self.win(adapters, {"nvidia-smi": (0, "NVIDIA GeForce RTX 3060, 12288, 11000\r\n")})
        hw = f.detect("win32")
        check_shape(self, hw)
        check_calls(self, f)
        self.assertEqual([(g["vram_mb"], g["vram_free_mb"], g["source"]) for g in hw["gpus"]], [(12288, 11000, "nvidia-smi")])  # not twice
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(hw["notes"], [])

    def test_nvidia_smi_in_program_files(self):
        calls = []

        def smi(argv):
            calls.append(argv[0])
            return (0, "NVIDIA GeForce RTX 3060, 12288, 11000\n") if argv[0].startswith("C:\\Program Files\\NVIDIA Corporation\\NVSMI") \
                else (None, "")

        f = self.win(tools={"nvidia-smi.exe": smi, "nvidia-smi": smi})
        hw = f.detect("win32")
        self.assertEqual(calls, ["nvidia-smi", "C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"])
        self.assertEqual([g["source"] for g in hw["gpus"]], ["nvidia-smi"])
        check_calls(self, f)

    def test_registry_without_nvidia_smi(self):
        gib = 2 ** 30
        adapters = [("NVIDIA GeForce RTX 3060", "PCI\\VEN_10DE&DEV_2504&SUBSYS_1", 12 * gib),
                    ("Intel(R) UHD Graphics 770", "PCI\\VEN_8086&DEV_4680", 128 * 2 ** 20),
                    ("Microsoft Basic Display Adapter", "root\\basicdisplay", None)]
        hw = self.win(adapters).detect("win32")
        check_shape(self, hw)
        self.assertEqual([(g["vendor"], g["vram_mb"], g["vram_free_mb"], g["unified"], g["backend"], g["source"]) for g in hw["gpus"]],
                         [("nvidia", 12288, None, False, "cuda", "registry"), ("intel", None, None, True, "none", "registry")])
        self.assertEqual(hw["notes"], ["nvidia-smi not found: the free memory of the NVIDIA GPU is unknown"])

    def test_amd_card_above_4_gb(self):
        # the 64-bit value, not WMI's 32-bit AdapterRAM (it says 4 GB for any card above that)
        hw = self.win([("AMD Radeon RX 6700 XT", "PCI\\VEN_1002&DEV_73DF", 12 * 2 ** 30)]).detect("win32")
        self.assertEqual((hw["gpus"][0]["vram_mb"], hw["gpus"][0]["backend"], hw["notes"]), (12288, "vulkan", []))

    def test_adapter_keys_need_not_be_contiguous(self):
        files = dict(WIN_BASE)
        files.update(reg(3, "AMD Radeon RX 6600", "PCI\\VEN_1002&DEV_73FF", 8 * 2 ** 30))
        hw = Fake(files).detect("windows")
        self.assertEqual([g["vram_mb"] for g in hw["gpus"]], [8192])

    def test_qwmemorysize_as_text(self):
        files = dict(WIN_BASE)
        files.update(reg(0, "AMD Radeon RX 7900 XTX", "PCI\\VEN_1002&DEV_744C", 24 * 2 ** 30))
        key = "reg:%s\\0000\\HardwareInformation.qwMemorySize" % CLS
        files[key] = "not a number"  # a driver that stores something else: no VRAM, no crash
        hw = Fake(files).detect("windows")
        check_shape(self, hw)
        self.assertEqual((hw["gpus"][0]["vram_mb"], hw["gpus"][0]["backend"]), (None, "none"))

    def test_arm64_windows_has_neon(self):
        with mock.patch.object(aihw.platform, "machine", return_value="ARM64"):
            hw = self.win().detect("win32")
        self.assertEqual((hw["arch"], hw["cpu"]["flags"]), ("arm64", ["avx", "avx2", "neon"]))  # (the fixture's AVX is the emulator's)

    def test_unreadable_api_values(self):
        f = Fake({"api:memory": "garbage", "api:cpu": PermissionError("no")})
        hw = f.detect("win32")
        check_shape(self, hw)
        self.assertEqual(hw["ram"], {"total_mb": None, "available_mb": None})
        self.assertTrue(any(n.startswith("RAM") for n in hw["notes"]))


@X86_HOST
class DetectSafety(unittest.TestCase):
    def test_never_raises(self):
        class Boom(Exception):
            pass

        def run(argv, timeout):
            raise Boom("tool")

        def read(path):
            raise Boom("file")

        for plat in ("linux", "darwin", "win32", "windows", "freebsd13", "cygwin", "", None):
            hw = aihw.detect(plat, run, read)
            check_shape(self, hw)

    def test_unknown_os(self):
        hw = aihw.detect("freebsd13", Fake().run, Fake().read)
        self.assertEqual(hw["os"], "freebsd13")
        self.assertTrue(hw["notes"])

    def test_plat_spellings(self):
        for plat, name in (("linux", "linux"), ("linux2", "linux"), ("darwin", "darwin"), ("macos", "darwin"), ("win32", "windows"),
                           ("windows", "windows"), ("Windows", "windows")):
            self.assertEqual(aihw._os_name(plat), name)
        self.assertEqual(aihw._os_name(None), aihw._os_name(sys.platform))

    def test_run_result_of_the_wrong_type(self):
        hw = aihw.detect("linux", lambda argv, timeout: "oops", Fake({"/proc/meminfo": MEMINFO}).read)
        check_shape(self, hw)
        self.assertEqual(hw["ram"]["total_mb"], 32000)

    def test_timeouts_never_exceed_5_seconds(self):
        f = Fake(tools={"nvidia-smi": (0, NVIDIA_CSV), "sysctl": (0, SYSCTL_INTEL), "vm_stat": (0, VM_STAT),
                        "system_profiler": (0, PROFILER)})
        for plat in ("linux", "darwin", "win32"):
            f.detect(plat)
        self.assertTrue(f.calls)
        check_calls(self, f)
        asked = [t for _, t in f.calls]
        self.assertTrue(all(isinstance(t, (int, float)) and 0 < t <= 5 for t in asked), asked)

    def test_detect_result_is_independent_of_the_inputs(self):
        a, b = linux_box().detect("linux"), linux_box().detect("linux")
        a["gpus"].append(1)
        a["cpu"]["flags"].append("x")
        self.assertEqual(b["gpus"], [])
        self.assertNotIn("x", b["cpu"]["flags"])


class DefaultReaders(unittest.TestCase):
    """The real `run` and `read`, on harmless things only."""

    def test_run_missing_program(self):
        self.assertEqual(aihw._run(["nuc-console-no-such-tool-xyz"], 5), (None, ""))
        self.assertEqual(aihw._run([os.path.join(tempfile.gettempdir(), "nuc-console-no-such-tool-xyz")], 5), (None, ""))

    def test_run_captures_output_and_rc(self):
        rc, out = aihw._run([sys.executable, "-c", "print('hello')"], 5)
        self.assertEqual((rc, out.strip()), (0, "hello"))
        rc, out = aihw._run([sys.executable, "-c", "import sys; sys.exit(3)"], 5)
        self.assertEqual(rc, 3)

    def test_run_timeout(self):
        t0 = time.time()
        self.assertEqual(aihw._run([sys.executable, "-c", "import time; time.sleep(30)"], 0.4), (None, ""))
        self.assertLess(time.time() - t0, 10)

    def test_which_never_looks_in_the_current_directory(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("nvidia-smi", "nvidia-smi.exe"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("#!/bin/sh\n")
                os.chmod(os.path.join(d, name), 0o755)
            old = os.getcwd()
            os.chdir(d)
            try:
                found = aihw._which("nvidia-smi")
            finally:
                os.chdir(old)
        self.assertFalse(found and os.path.dirname(os.path.abspath(found)) == os.path.abspath(d), found)

    def test_read_file_and_pseudo_paths(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write("MemTotal: 1 kB\n")
        try:
            self.assertEqual(aihw._read(f.name), "MemTotal: 1 kB\n")
        finally:
            os.unlink(f.name)
        with self.assertRaises(OSError):
            aihw._read(os.path.join(tempfile.gettempdir(), "nuc-console-no-such-file-xyz"))
        with self.assertRaises(OSError):  # no such key (or no registry at all on this OS): an OSError either way
            aihw._read("reg:HKLM\\SOFTWARE\\nuc-console-no-such-key-xyz\\Value")
        with self.assertRaises(OSError):
            aihw._read("reg:HKCU\\SOFTWARE\\x\\Value")  # only HKLM is read
        if sys.platform != "win32":
            with self.assertRaises(Exception):  # Windows API text does not exist elsewhere
                aihw._read("api:memory")


class RealHost(unittest.TestCase):
    def test_detect_on_this_machine(self):
        hw = aihw.detect()
        check_shape(self, hw)
        self.assertEqual(hw["os"], aihw._os_name(sys.platform))

    def test_assess_on_this_machine(self):
        a = aihw.assess(model(), aihw.detect())
        self.assertIn(a["verdict"], ("gpu", "partial", "ram", "slow", "no"))


class Cached(unittest.TestCase):
    def setUp(self):
        aihw._CACHE.update(at=0.0, hw=None)
        self.addCleanup(aihw._CACHE.update, at=0.0, hw=None)
        self.calls = 0

        def fake_detect():
            self.calls += 1
            time.sleep(0.02)
            return {"os": "linux", "n": self.calls, "gpus": [], "notes": []}

        p = mock.patch.object(aihw, "detect", fake_detect)
        p.start()
        self.addCleanup(p.stop)

    def test_detects_once_within_max_age(self):
        self.assertEqual(aihw.cached()["n"], 1)
        self.assertEqual(aihw.cached()["n"], 1)
        self.assertEqual(self.calls, 1)

    def test_detects_again_after_max_age(self):
        aihw.cached()
        aihw._CACHE["at"] -= 301  # five minutes and a second old
        self.assertEqual(aihw.cached()["n"], 2)
        aihw._CACHE["at"] -= 100
        self.assertEqual(aihw.cached(max_age=50)["n"], 3)  # the caller's own limit
        self.assertEqual(aihw.cached(max_age=50)["n"], 3)

    def test_max_age_zero_always_detects(self):
        aihw.cached(0)
        aihw.cached(0)
        self.assertEqual(self.calls, 2)

    def test_callers_get_copies(self):
        a = aihw.cached()
        a["gpus"].append("x")
        a["notes"] = ["changed"]
        self.assertEqual(aihw.cached()["gpus"], [])
        self.assertEqual(aihw.cached()["notes"], [])

    def test_threads_share_one_detection(self):
        results = []

        def work():
            results.append(aihw.cached()["n"])

        threads = [threading.Thread(target=work) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [1] * 12)
        self.assertEqual(self.calls, 1)


class NeedMb(unittest.TestCase):
    def test_sources_of_the_weights_in_order(self):
        # approx_mb first, then the pinned size (bytes), then params_b x 600 MB; the runtime adds 300 MB, the cache 0 at 0 tokens
        self.assertEqual(aihw.need_mb({"approx_mb": 2500, "size": 9 * 2 ** 30, "params_b": 99}, 0), 2800)
        self.assertEqual(aihw.need_mb({"size": 2 ** 30, "params_b": 99}, 0), 1024 + 300)
        self.assertEqual(aihw.need_mb({"approx_mb": None, "size": None, "params_b": 4}, 0), 2400 + 300)
        self.assertEqual(aihw.need_mb({"approx_mb": 0, "params_b": 1.7}, 0), 1020 + 300)

    def test_legacy_ram_mb_and_no_information(self):
        # aisetup's older field already holds cache and runtime at 4096 tokens: the weights are what is left
        m = {"ram_mb": 3600}
        self.assertLess(aihw.need_mb(m, 4096), 3700)
        self.assertGreater(aihw.need_mb(m, 4096), 3000)
        # nothing at all: a cautious mid-size guess, never an exception and never "fits easily"
        self.assertEqual(aihw.need_mb({}, 0), aihw.FALLBACK_WEIGHTS_MB + 300)
        self.assertEqual(aihw.need_mb({"approx_mb": "a lot", "size": True, "params_b": -1}, 0), aihw.FALLBACK_WEIGHTS_MB + 300)

    def test_kv_cache_grows_with_context_and_layers(self):
        m = {"approx_mb": 2600, "layers": 36}
        # 36 layers x 4 KiB per layer and token: 4096 tokens = 576 MB
        self.assertEqual(aihw.need_mb(m, 4096), 2600 + 576 + 300)
        self.assertEqual(aihw.need_mb(m, 8192), 2600 + 1152 + 300)
        self.assertLess(aihw.need_mb(m, 2048), aihw.need_mb(m, 4096))
        self.assertGreater(aihw.need_mb({"approx_mb": 2600, "layers": 48}, 4096), aihw.need_mb(m, 4096))
        self.assertEqual(aihw.need_mb({"approx_mb": 2600}, 4096), 2600 + 512 + 300)  # 32 layers when the model does not say
        self.assertEqual(aihw.need_mb({"approx_mb": 2600}), aihw.need_mb({"approx_mb": 2600}, 4096))  # the default context
        self.assertEqual(aihw.need_mb({"approx_mb": 2600}, -5), 2900)  # nonsense context: no negative cache

    def test_returns_an_int(self):
        self.assertIsInstance(aihw.need_mb({"params_b": 1.7}), int)


class AssessGpu(unittest.TestCase):
    """A model that needs exactly 1300 MB (ctx 0): GPU boundary at 1.1 x need = 1430 MB free."""

    def test_gpu_boundary(self):
        hw = box(32768, 30000, [nv(2048, 1430)])
        a = aihw.assess(model(), hw, 0)
        self.assertEqual((a["verdict"], a["where"], a["need_mb"]), ("gpu", "GPU", 1300))
        self.assertGreaterEqual(a["gpu_layers"], 32)  # all of them
        hw = box(32768, 30000, [nv(2048, 1429)])
        a = aihw.assess(model(), hw, 0)
        self.assertEqual((a["verdict"], a["where"]), ("partial", "GPU+CPU"))
        self.assertEqual(a["gpu_layers"], 31)

    def test_example_sentence_of_the_contract(self):
        m = {"approx_mb": 4400, "layers": 32}  # 4400 + 512 + 300 = 5212 MB = 5.1 GB
        a = aihw.assess(m, box(32768, 30000, [nv(12288, 12288)]))
        self.assertEqual(a["why"], "needs 5.1 GB, the RTX 3060 has 12 GB free: all on the GPU")

    def test_free_vram_not_total_decides(self):
        self.assertEqual(verdict(model(), box(32768, 30000, [nv(12288, 12288)])), "gpu")
        self.assertEqual(verdict(model(), box(32768, 30000, [nv(12288, 600)])), "partial")  # other programs hold the VRAM

    def test_free_unknown_uses_total_less_the_display(self):
        self.assertEqual(verdict(model(), box(32768, 30000, [nv(2048, None)])), "gpu")  # 2048 - 512 = 1536 >= 1430
        self.assertEqual(verdict(model(), box(32768, 30000, [nv(1900, None)])), "partial")  # 1388 < 1430

    def test_gpu_works_without_knowing_the_ram(self):
        a = aihw.assess(model(), box(None, None, [nv(12288, 12000)]), 0)
        self.assertEqual((a["verdict"], a["where"]), ("gpu", "GPU"))

    def test_gpu_layers_for_a_full_fit_cover_the_model(self):
        for layers in (24, 32, 36, 48, None):
            m = model(layers=layers) if layers else {"approx_mb": 1000}
            a = aihw.assess(m, box(32768, 30000, [nv(12288, 12000)]), 0)
            self.assertGreaterEqual(a["gpu_layers"], layers or 32)

    def test_best_gpu_is_the_one_with_most_free_memory(self):
        hw = box(32768, 30000, [nv(4096, 3900, "NVIDIA GeForce GTX 1050 Ti"), nv(12288, 11800), nv(8192, 8000, "NVIDIA GeForce RTX 3070")])
        a = aihw.assess(model(approx_mb=9000), hw, 0)
        self.assertEqual(a["verdict"], "gpu")
        self.assertIn("RTX 3060", a["why"])

    def test_gpus_that_cannot_be_used_are_ignored(self):
        igpu = {"vendor": "intel", "name": "Intel UHD", "vram_mb": 16000, "vram_free_mb": 16000, "unified": True, "backend": "vulkan"}
        nobackend = dict(nv(24576, 24000), backend="none")  # a card without a driver
        for g in (igpu, nobackend):
            a = aihw.assess(model(), box(32768, 30000, [g]), 0)
            self.assertEqual((a["verdict"], a["where"], a["gpu_layers"]), ("ram", "CPU", 0), g)

    def test_other_gpu_vendors(self):
        amd = {"vendor": "amd", "name": "AMD Radeon RX 6700 XT", "vram_mb": 12288, "vram_free_mb": 12000, "unified": False,
               "backend": "rocm", "source": "sysfs"}
        arc = dict(amd, vendor="intel", name="Intel(R) Arc(TM) A770", backend="vulkan")
        for g in (amd, arc):
            self.assertEqual(verdict(model(), box(16384, 12000, [g])), "gpu")

    def test_gpu_name_in_the_sentence(self):
        a = aihw.assess(model(), box(16384, 12000, [nv(12288, 12000, "NVIDIA GeForce RTX 4070 Ti")]), 0)
        self.assertIn("the RTX 4070 Ti has 12 GB free", a["why"])
        a = aihw.assess(model(), box(16384, 12000, [nv(12288, 12000, "Quadro P620(R)")]), 0)
        self.assertIn("the Quadro P620", a["why"])


class AssessPartial(unittest.TestCase):
    def test_layers_are_proportional_to_the_vram(self):
        # 715 MB free of the 1430 needed: half of the layers
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 715)]), 0)
        self.assertEqual((a["verdict"], a["gpu_layers"]), ("partial", 16))
        a = aihw.assess(model(layers=40), box(32768, 30000, [nv(2048, 715)]), 0)
        self.assertEqual(a["gpu_layers"], 20)
        a = aihw.assess({"approx_mb": 1000}, box(32768, 30000, [nv(2048, 715)]), 0)  # no layers: 32
        self.assertEqual(a["gpu_layers"], 16)
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 358)]), 0)  # a quarter
        self.assertEqual(a["gpu_layers"], 8)

    def test_never_all_the_layers_when_it_does_not_fit(self):
        for free in (1000, 1300, 1429):
            a = aihw.assess(model(), box(32768, 30000, [nv(2048, free)]), 0)
            self.assertLess(a["gpu_layers"], 32, free)
            self.assertGreater(a["gpu_layers"], 0, free)

    def test_a_sliver_of_vram_is_not_partial(self):
        # 10 % of the layers is the least that is worth calling "partial": 4 of 32 layers (12.5 %) are, 3 (9.4 %) are not
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 179)]), 0)
        self.assertEqual((a["verdict"], a["gpu_layers"]), ("partial", 4))
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 143)]), 0)  # room for 3 layers: the CPU rules apply
        self.assertEqual((a["verdict"], a["where"], a["gpu_layers"]), ("ram", "CPU", 0))
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 20)]), 0)
        self.assertEqual((a["verdict"], a["gpu_layers"]), ("ram", 0))

    def test_the_rest_must_fit_the_ram_comfortably(self):
        # rest = 650 MB (half of 1300): comfortable when <= 50 % of the RAM and <= free - 1 GB
        gpu = [nv(2048, 715)]
        self.assertEqual(verdict(model(), box(1300, None, gpu)), "partial")
        self.assertEqual(verdict(model(), box(1299, None, gpu)), "slow")
        self.assertEqual(verdict(model(), box(4096, 1674, gpu)), "partial")
        self.assertEqual(verdict(model(), box(4096, 1673, gpu)), "slow")

    def test_a_rest_too_big_for_the_ram_is_no(self):
        # rest = 650 MB is more than 85 % of 700 MB of RAM
        a = aihw.assess(model(), box(700, 700, [nv(2048, 715)]), 0)
        self.assertEqual((a["verdict"], a["where"], a["gpu_layers"], a["tok_s"]), ("no", "CPU", 0, None))
        self.assertIn("it will not work", a["why"])

    def test_slow_with_a_gpu_keeps_its_layers(self):
        a = aihw.assess(model(), box(1299, 1299, [nv(2048, 715)]), 0)
        self.assertEqual((a["verdict"], a["where"], a["gpu_layers"]), ("slow", "GPU+CPU", 16))
        self.assertIsNotNone(a["tok_s"])

    def test_partial_is_slower_than_all_on_the_gpu_and_faster_than_the_cpu(self):
        m = model(approx_mb=8000)
        full = aihw.assess(m, box(32768, 30000, [nv(24576, 24000, "NVIDIA RTX 3090")]), 0)["tok_s"]
        part = aihw.assess(m, box(32768, 30000, [nv(8192, 5000)]), 0)
        cpu = aihw.assess(m, box(32768, 30000), 0)
        self.assertEqual(part["verdict"], "partial")
        self.assertLess(part["tok_s"][1], full[1])
        self.assertGreater(part["tok_s"][1], cpu["tok_s"][1])

    def test_sentence_says_what_goes_where(self):
        a = aihw.assess(model(), box(32768, 30000, [nv(2048, 715)]), 0)
        self.assertEqual(a["why"], "needs 1.3 GB, the RTX 3060 has 0.7 GB free: 16 of 32 layers on the GPU, the rest (0.6 GB) in RAM; "
                                   "works, slower than a GPU that holds it all")


class AssessCpu(unittest.TestCase):
    """CPU only. The model needs 1300 MB: comfortable up to 50 % of the RAM and 1 GB less than what is free, never above 85 %."""

    def test_ram_boundary_on_total(self):
        self.assertEqual(verdict(model(), box(2600, 2600)), "ram")
        self.assertEqual(verdict(model(), box(2599, 2599)), "slow")

    def test_ram_boundary_on_free(self):
        self.assertEqual(verdict(model(), box(10000, 2324)), "ram")  # 1300 <= 2324 - 1024
        self.assertEqual(verdict(model(), box(10000, 2323)), "slow")

    def test_no_boundary(self):
        self.assertEqual(verdict(model(), box(1530, 1530)), "slow")  # 85 % of 1530 = 1300.5
        self.assertEqual(verdict(model(), box(1529, 1529)), "no")

    def test_where_and_layers(self):
        for hw in (box(16384, 12000), box(2000, 2000), box(1000, 1000)):
            a = aihw.assess(model(), hw, 0)
            self.assertEqual((a["where"], a["gpu_layers"]), ("CPU", 0))

    def test_a_machine_with_no_gpu_key(self):
        for hw in ({"ram": {"total_mb": 16384, "available_mb": 12000}}, {"ram": {"total_mb": 16384, "available_mb": 12000}, "gpus": None},
                   {"ram": {"total_mb": 16384, "available_mb": 12000}, "gpus": []}):
            self.assertEqual(verdict(model(), hw), "ram")

    def test_free_ram_unknown_judges_by_the_total(self):
        self.assertEqual(verdict(model(), box(2600, None)), "ram")
        self.assertEqual(verdict(model(), box(2599, None)), "slow")
        self.assertEqual(verdict(model(), box(1529, None)), "no")

    def test_free_more_than_total_is_capped(self):
        self.assertEqual(verdict(model(), box(2000, 99999)), "slow")  # 1300 > 50 % of 2000, whatever "free" says

    def test_unknown_ram_is_not_good_news(self):
        for hw in (box(None, None), box(None, 8000), {}, None, {"ram": None}, box(0, 0)):
            a = aihw.assess(model(), hw, 0)
            self.assertEqual((a["verdict"], a["where"], a["gpu_layers"]), ("slow", "CPU", 0), hw)
            self.assertIsNone(a["tok_s"])
            self.assertIn("could not be read", a["why"])
            self.assertIn("cannot tell", a["why"])

    def test_slow_sentences(self):
        a = aihw.assess(model(approx_mb=4000), box(16384, 4000))  # fits the RAM, not what is free now
        self.assertEqual(a["verdict"], "slow")
        self.assertIn("only 3.9 GB of the 16 GB of RAM are free now", a["why"])
        a = aihw.assess(model(approx_mb=9000), box(16384, 16000))
        self.assertEqual(a["verdict"], "slow")
        self.assertIn("more than half of the 16 GB of RAM", a["why"])
        self.assertIn("slow down", a["why"])

    def test_ram_and_no_sentences(self):
        a = aihw.assess(model(), box(16384, 12288), 0)
        self.assertEqual(a["why"], "needs 1.3 GB, this machine has 16 GB of RAM (12 GB free): runs on the CPU, comfortably")
        a = aihw.assess(model(approx_mb=19000), box(16384, 12288), 0)
        self.assertEqual(a["why"], "needs 19 GB, more than 85% of the 16 GB of RAM and no GPU that can hold it: it will not work")

    def test_a_gpu_too_small_for_anything_does_not_rescue(self):
        a = aihw.assess(model(approx_mb=60000), box(16384, 12000, [nv(4096, 4000)]), 0)
        self.assertEqual((a["verdict"], a["gpu_layers"]), ("no", 0))


class AssessApple(unittest.TestCase):
    """Apple silicon: the model runs on the GPU while it needs at most 65 % of the RAM and at most what is available."""

    def test_unified_boundary_on_total(self):
        self.assertEqual(verdict(model(), box(2000, 2000, [apple()])), "gpu")  # 65 % of 2000 = 1300
        a = aihw.assess(model(), box(1999, 1999, [apple()]), 0)
        self.assertEqual((a["verdict"], a["where"], a["gpu_layers"]), ("slow", "CPU", 0))  # 1300 > 50 % of the RAM: tight

    def test_unified_boundary_on_available(self):
        self.assertEqual(verdict(model(), box(16384, 1300, [apple()])), "gpu")
        self.assertEqual(verdict(model(), box(16384, 1299, [apple()])), "slow")

    def test_gpu_verdict_shape(self):
        a = aihw.assess(model(), box(16384, 12000, [apple()]), 0)
        self.assertEqual((a["verdict"], a["where"]), ("gpu", "GPU"))
        self.assertGreaterEqual(a["gpu_layers"], 32)
        self.assertEqual(a["why"], "needs 1.3 GB of the 16 GB of unified memory (12 GB free): all on the GPU (Metal)")

    def test_apple_without_the_free_figure(self):
        self.assertEqual(verdict(model(), box(2000, None, [apple()])), "gpu")

    def test_apple_above_85_percent_is_no(self):
        self.assertEqual(verdict(model(approx_mb=14000), box(16384, 12000, [apple()]), 0), "no")

    def test_between_65_and_85_percent_it_runs_but_squeezes_the_mac(self):
        a = aihw.assess(model(approx_mb=11000), box(16384, 12000, [apple()]), 0)
        self.assertEqual((a["verdict"], a["where"]), ("slow", "CPU"))

    def test_apple_without_a_known_ram_size(self):
        a = aihw.assess(model(), box(None, None, [apple()]), 0)
        self.assertEqual((a["verdict"], a["tok_s"]), ("slow", None))


class AssessSpeed(unittest.TestCase):
    def test_cpu_range_from_bandwidth(self):
        # 1000 MB read per token at 20-40 GB/s
        a = aihw.assess(model(), box(16384, 12000), 0)
        self.assertEqual(a["tok_s"], [20, 41])
        # a model twice the size is half as fast
        a = aihw.assess(model(approx_mb=2000), box(16384, 12000), 0)
        self.assertEqual(a["tok_s"], [10, 20])

    def test_small_rates_keep_a_decimal(self):
        a = aihw.assess(model(approx_mb=4000), box(16384, 15000), 0)  # 3.9 GB per token: 5.1-10.2
        self.assertEqual(a["tok_s"], [5.1, 10])  # from 10 on, whole numbers
        lo, hi = aihw.assess(model(approx_mb=20000), box(16384, 15000, [nv(24576, 24000)]), 0)["tok_s"]
        self.assertLessEqual(lo, hi)

    def test_range_is_ordered_and_positive_everywhere(self):
        for hw in (box(64000, 60000), box(64000, 60000, [nv(12288, 12000)]), box(64000, 60000, [nv(4096, 3000)]),
                   box(64000, 60000, [apple("Apple M3 Max GPU")]), box(8000, 1500)):
            for mb in (500, 3000, 9000, 30000):
                a = aihw.assess(model(approx_mb=mb), hw, 0)
                if a["tok_s"] is not None:
                    lo, hi = a["tok_s"]
                    self.assertTrue(0 < lo <= hi, (hw, mb, a))

    def test_moe_reads_only_its_active_experts(self):
        dense = {"approx_mb": 18000, "params_b": 30, "layers": 48}
        moe = dict(dense, active_b=3)
        hw = box(64000, 60000)
        d, m = aihw.assess(dense, hw, 0)["tok_s"], aihw.assess(moe, hw, 0)["tok_s"]
        self.assertEqual(aihw.assess(moe, hw, 0)["need_mb"], aihw.assess(dense, hw, 0)["need_mb"])  # all the weights are in memory
        self.assertAlmostEqual(m[0] / d[0], 10, delta=1.0)  # 3 of 30 billion parameters: ten times the speed
        self.assertAlmostEqual(m[1] / d[1], 10, delta=1.0)
        self.assertGreater(m[0], 10)  # a 30B model at the speed of a 3B one, on a CPU: that is the point of a MoE
        # on a GPU too, and without params_b the active size is taken at 0.6 GB per billion
        g = box(64000, 60000, [nv(24576, 24000)])
        self.assertGreater(aihw.assess(moe, g, 0)["tok_s"][1], aihw.assess(dense, g, 0)["tok_s"][1] * 5)
        self.assertGreater(aihw.assess({"approx_mb": 18000, "active_b": 3}, hw, 0)["tok_s"][0], 10)

    def test_gpu_is_faster_than_cpu(self):
        m = model(approx_mb=4000)
        cpu = aihw.assess(m, box(32768, 30000), 0)["tok_s"]
        gpu = aihw.assess(m, box(32768, 30000, [nv(12288, 12000)]), 0)["tok_s"]
        self.assertGreater(gpu[0], cpu[1])
        big = aihw.assess(m, box(32768, 30000, [nv(24576, 24000)]), 0)["tok_s"]
        self.assertGreater(big[1], gpu[1])  # 24 GB class cards are in the fast class

    def test_apple_chip_class(self):
        speeds = {}
        for chip in ("Apple M2 GPU", "Apple M2 Pro GPU", "Apple M3 Max GPU", "Apple M2 Ultra GPU"):
            speeds[chip] = aihw.assess(model(approx_mb=3000), box(64000, 50000, [apple(chip)]), 0)["tok_s"]
        order = [speeds[c][1] for c in ("Apple M2 GPU", "Apple M2 Pro GPU", "Apple M3 Max GPU", "Apple M2 Ultra GPU")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(set(order)), 4)
        self.assertEqual(aihw._apple_class("Apple M1"), "base")
        self.assertEqual(aihw._apple_class("Apple M4 Pro"), "pro")
        self.assertEqual(aihw._apple_class("Apple silicon"), "base")

    def test_slow_is_slower(self):
        ok = aihw.assess(model(), box(2600, 2600), 0)  # ram
        slow = aihw.assess(model(), box(2599, 2599), 0)
        self.assertEqual((ok["verdict"], slow["verdict"]), ("ram", "slow"))
        self.assertLess(slow["tok_s"][1], ok["tok_s"][1])
        self.assertLess(slow["tok_s"][0], ok["tok_s"][0])

    def test_no_has_no_speed(self):
        self.assertIsNone(aihw.assess(model(), box(1000, 1000), 0)["tok_s"])


class AssessRobustness(unittest.TestCase):
    def test_result_shape(self):
        for hw in (box(16384, 12000), box(16384, 12000, [nv(12288, 12000)]), box(None, None), {}, None, box(1000, 1000)):
            a = aihw.assess(model(), hw)
            self.assertEqual(set(a), {"verdict", "where", "need_mb", "gpu_layers", "tok_s", "why"})
            self.assertIn(a["verdict"], ("gpu", "partial", "ram", "slow", "no"))
            self.assertIn(a["where"], ("GPU", "GPU+CPU", "CPU"))
            self.assertIsInstance(a["need_mb"], int)
            self.assertIsInstance(a["gpu_layers"], int)
            self.assertTrue(a["tok_s"] is None or (len(a["tok_s"]) == 2 and all(isinstance(x, (int, float)) for x in a["tok_s"])))
            self.assertTrue(isinstance(a["why"], str) and a["why"])
            self.assertNotIn("\n", a["why"])
            if a["verdict"] == "no":
                self.assertIsNone(a["tok_s"])
            json.dumps(a)

    def test_where_follows_the_verdict(self):
        cases = {"gpu": box(16384, 12000, [nv(12288, 12000)]), "partial": box(16384, 12000, [nv(2048, 715)]),
                 "ram": box(16384, 12000), "no": box(1000, 1000)}
        want = {"gpu": "GPU", "partial": "GPU+CPU", "ram": "CPU", "no": "CPU"}
        for v, hw in cases.items():
            a = aihw.assess(model(), hw, 0)
            self.assertEqual((a["verdict"], a["where"]), (v, want[v]))

    def test_the_machine_is_not_changed(self):
        hw = box(16384, 12000, [nv(2048, 715)])
        before = json.dumps(hw)
        m = model()
        mb = json.dumps(m)
        aihw.assess(m, hw)
        self.assertEqual((json.dumps(hw), json.dumps(m)), (before, mb))

    def test_gpu_entries_with_missing_fields(self):
        for g in ({}, {"vendor": "nvidia"}, {"vendor": "nvidia", "backend": "cuda"},
                  {"vendor": "nvidia", "backend": "cuda", "vram_mb": None}, {"vendor": "apple", "unified": True}, "not a dict", None):
            a = aihw.assess(model(), box(16384, 12000, [g]), 0)
            self.assertEqual(a["verdict"], "ram", g)

    def test_a_model_with_a_pinned_size(self):
        # the pinned size (bytes) is used when there is no approximate one: 2 GiB of weights + 300
        a = aihw.assess({"size": 2 * 2 ** 30}, box(16384, 12000), 0)
        self.assertEqual(a["need_mb"], 2348)

    def test_catalog_style_models(self):
        # the dicts of aisetup.MODELS plus the fields of the catalogue
        m = {"id": "qwen3-4b", "name": "Qwen3 4B", "license": "Apache-2.0", "ram_mb": 3600, "params_b": 4, "quant": "Q4_K_M",
             "layers": 36, "ctx_max": 32768, "rank": 3, "approx_mb": 2500, "notes": "thinking mode: /no_think",
             "repo": "x", "file": "y", "revision": None, "sha256": None, "size": None}
        self.assertEqual(aihw.need_mb(m), 2500 + 576 + 300)
        self.assertEqual(verdict(m, box(16384, 12000, [nv(12288, 12000)]), 4096), "gpu")


class Recommend(unittest.TestCase):
    MODELS = [
        {"id": "tiny", "approx_mb": 500, "layers": 24, "rank": 6},
        {"id": "small", "approx_mb": 1500, "layers": 28, "rank": 5},
        {"id": "mid", "approx_mb": 4000, "layers": 36, "rank": 3},
        {"id": "big", "approx_mb": 9000, "layers": 40, "rank": 2},
        {"id": "huge", "approx_mb": 18000, "layers": 48, "rank": 1},
    ]

    def test_best_ranked_that_runs_on_the_gpu(self):
        hw = box(16384, 15000, [nv(12288, 12000)])  # holds big (9.7 GB x 1.1 = 10.7 GB), not huge (and 16 GB of RAM cannot either)
        self.assertEqual(aihw.recommend(self.MODELS, hw), "big")

    def test_best_ranked_that_runs_comfortably_in_ram(self):
        self.assertEqual(aihw.recommend(self.MODELS, box(64000, 60000)), "huge")  # 18 GB + cache: under half of 64 GB
        self.assertEqual(aihw.recommend(self.MODELS, box(16384, 15000)), "mid")  # big needs 9.8 GB: more than half of 16 GB

    def test_a_partial_model_counts_when_the_ram_alone_would_hold_it(self):
        # a small GPU in a machine with a lot of RAM: huge is "partial" here, but 64 GB hold it comfortably: the GPU must not
        # push the choice down to a model that happens to fit the 4 GB card
        hw = box(64000, 60000, [nv(4096, 3800)])
        self.assertEqual(verdict(self.MODELS[4], hw, 4096), "partial")
        self.assertEqual(aihw.recommend(self.MODELS, hw), "huge")

    def test_a_partial_model_that_needs_the_gpu_does_not_beat_one_that_fits(self):
        hw = box(24576, 22000, [nv(12288, 12000)])  # huge: partial, and 19 GB are more than half of 24 GB of RAM
        self.assertEqual(verdict(self.MODELS[4], hw, 4096), "partial")
        self.assertEqual(aihw.recommend(self.MODELS, hw), "big")

    def test_partial_when_nothing_is_gpu_or_ram(self):
        models = [{"id": "a", "approx_mb": 3000, "layers": 32, "rank": 1}, {"id": "b", "approx_mb": 5000, "layers": 32, "rank": 2}]
        hw = box(6000, 5000, [nv(2048, 2000)])  # a: needs 3.8 GB: partial; b: partial too (rest fits); neither is gpu or ram
        self.assertEqual(verdict(models[0], hw, 4096), "partial")
        self.assertEqual(aihw.recommend(models, hw), "a")

    def test_smallest_that_is_not_no(self):
        models = [{"id": "x", "approx_mb": 5000, "rank": 1}, {"id": "y", "approx_mb": 3000, "rank": 2},
                  {"id": "z", "approx_mb": 9000, "rank": 3}]
        hw = box(6000, 4500)  # all slow or no: x needs 5.8 GB (96 % of 6 GB: no), y 3.8 GB (slow), z no
        self.assertEqual([verdict(m, hw, 4096) for m in models], ["no", "slow", "no"])
        self.assertEqual(aihw.recommend(models, hw), "y")
        models = [{"id": "x", "approx_mb": 3000, "rank": 1}, {"id": "y", "approx_mb": 2000, "rank": 2}]
        hw = box(5200, 4000)  # x needs 3.8 GB, y 2.8 GB: both slow: the smallest, not the best
        self.assertEqual([verdict(m, hw, 4096) for m in models], ["slow", "slow"])
        self.assertEqual(aihw.recommend(models, hw), "y")

    def test_none_if_nothing_fits(self):
        self.assertIsNone(aihw.recommend(self.MODELS, box(256, 200)))
        self.assertIsNone(aihw.recommend([], box(64000, 60000)))

    def test_unknown_ram_recommends_the_smallest(self):
        self.assertEqual(aihw.recommend(self.MODELS, box(None, None)), "tiny")
        self.assertEqual(aihw.recommend(self.MODELS, None), "tiny")

    def test_a_gpu_can_make_a_big_model_the_pick(self):
        hw = box(16384, 12000, [nv(24576, 24000, "NVIDIA GeForce RTX 4090")])
        self.assertEqual(aihw.recommend(self.MODELS, hw), "huge")  # 18 GB x 1.1 fits in 24 GB; 16 GB of RAM could not hold it

    def test_apple_recommendation_follows_the_65_percent_rule(self):
        hw = box(16384, 12000, [apple()])
        # huge needs 19 GB (> 85 %), big 9.9 GB (60 % of 16 GB: fits the GPU)
        self.assertEqual(aihw.recommend(self.MODELS, hw), "big")
        hw = box(8192, 5000, [apple()])  # 65 % of 8 GB = 5.3 GB, free 5.0 GB: mid needs 4.8 GB
        self.assertEqual(aihw.recommend(self.MODELS, hw), "mid")

    def test_ranks_decide_not_the_list_order(self):
        shuffled = list(reversed(self.MODELS))
        self.assertEqual(aihw.recommend(shuffled, box(64000, 60000)), "huge")
        self.assertEqual(aihw.recommend(shuffled, box(16384, 15000)), "mid")

    def test_without_ranks_the_list_is_best_first(self):
        models = [{"id": "a", "approx_mb": 4000}, {"id": "b", "approx_mb": 2000}, {"id": "c", "approx_mb": 500}]
        self.assertEqual(aihw.recommend(models, box(64000, 60000)), "a")
        self.assertEqual(aihw.recommend(models, box(12288, 10000)), "a")  # a needs 4.7 GB: under half of 12 GB, 1 GB less than free
        self.assertEqual(aihw.recommend(models, box(12288, 5500)), "b")  # a would leave less than 1 GB of what is free
        # ranked models come before the ones without a rank
        models = [{"id": "a", "approx_mb": 500}, {"id": "b", "approx_mb": 500, "rank": 7}]
        self.assertEqual(aihw.recommend(models, box(64000, 60000)), "b")

    def test_same_rank_keeps_the_list_order(self):
        models = [{"id": "a", "approx_mb": 500, "rank": 1}, {"id": "b", "approx_mb": 500, "rank": 1}]
        self.assertEqual(aihw.recommend(models, box(64000, 60000)), "a")

    def test_does_not_change_the_list(self):
        models = list(reversed(self.MODELS))
        ids = [m["id"] for m in models]
        aihw.recommend(models, box(64000, 60000))
        self.assertEqual([m["id"] for m in models], ids)


class Constants(unittest.TestCase):
    """The contract's numbers, named at the top of the file."""

    def test_thresholds(self):
        self.assertEqual((aihw.GPU_FIT, aihw.UNIFIED_MAX_FRAC, aihw.RAM_COMFY_FRAC, aihw.RAM_MAX_FRAC, aihw.RAM_RESERVE_MB),
                         (1.10, 0.65, 0.50, 0.85, 1024))
        self.assertEqual((aihw.OVERHEAD_MB, aihw.MB_PER_B_PARAMS, aihw.DEFAULT_LAYERS), (300, 600, 32))
        self.assertEqual(aihw.CPU_BW, (20.0, 40.0))
        self.assertLessEqual(max(aihw.TOOL_TIMEOUT, aihw.SMI_TIMEOUT, aihw.PROFILER_TIMEOUT), 5)


if __name__ == "__main__":
    unittest.main()
