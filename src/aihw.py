"""What this machine has (CPU, RAM, GPUs with their memory) and whether a local model fits: advice, not a measurement.

  detect()   the hardware, read the way each OS tells it; every failure becomes a short note, never an exception
  cached()   detect() at most every 5 minutes per process, thread-safe (hardware does not change, free memory does)
  need_mb()  memory a model needs: weights + KV cache for the context + runtime overhead
  assess()   verdict gpu | partial | ram | slow | no for one model, with GPU layers, a speed range and a plain sentence
  recommend() the id of the model to suggest first

  Linux    /proc/meminfo, /proc/cpuinfo, nvidia-smi (CSV), /sys/class/drm/card*/device (AMD VRAM, vendor ids)
  macOS    sysctl, vm_stat; Apple silicon = unified memory + Metal; Intel Macs: system_profiler SPDisplaysDataType -json
  Windows  GlobalMemoryStatusEx and GetLogicalProcessorInformationEx (ctypes), nvidia-smi (PATH or Program Files), otherwise
           the 64-bit HardwareInformation.qwMemorySize of the display adapters in the registry (WMI AdapterRAM is 32-bit)
Tools run with a fixed argument list (no shell, no privileges, 5 s at most). `run(argv, timeout) -> (rc, stdout)` and
`read(path) -> str` are injectable: tests feed fixtures of every OS. Besides files, the default `read` serves two pseudo paths
for Windows: "reg:HKLM\\key\\Value" (the value as text) and "api:memory" / "api:cpu" (key=value text from the Windows API).
Standard library only, Python 3.8+. The parsers are pure functions, tested on every OS.
"""
import copy
import ctypes
import json
import math
import ntpath
import os
import platform
import re
import subprocess
import sys
import threading
import time

import cpuinfo   # same directory: parse_cpuinfo, parse_slpi_ex
import hostinfo  # same directory: parse_vm_stat

# ---- thresholds: every verdict boundary lives here --------------------------------------------------------------------------
DEFAULT_CTX = 4096          # tokens of context assumed when the caller gives none (aisetup.DEFAULT_CTX)
OVERHEAD_MB = 300           # runtime buffers, compute graph, loader
KV_BYTES_PER_LAYER_TOKEN = 4096  # f16 keys+values of a typical grouped-query model (8 KV heads x 128): Qwen3 4B = 147 KB/token
DEFAULT_LAYERS = 32         # transformer blocks when the model does not say
MB_PER_B_PARAMS = 600       # Q4_K_M weighs about 0.6 GB per billion parameters
FALLBACK_WEIGHTS_MB = 4096  # a model that says nothing about its size is assumed mid-size: the advice errs on the cautious side
GPU_FIT = 1.10              # a GPU holds a model when its free VRAM >= need x this (desktop, fragmentation, growth)
GPU_DISPLAY_RESERVE_MB = 512  # VRAM the display keeps when only the total is known (no free figure)
UNIFIED_MAX_FRAC = 0.65     # Apple silicon: Metal may wire about two thirds of the RAM; above that the model does not run on the GPU
PARTIAL_MIN_FRAC = 0.10     # less than this share of the layers on the GPU is not worth calling "partial": CPU rules apply
RAM_COMFY_FRAC = 0.50       # "ram": the model needs at most half of the RAM ...
RAM_RESERVE_MB = 1024       # ... and leaves 1 GB of what is free now
RAM_MAX_FRAC = 0.85         # above this share of the RAM it will not work (swap storm): "no"
APU_VRAM_MAX_MB = 2048      # an AMD GPU reporting less than this as VRAM is an APU's carve-out: shared memory, not VRAM
USABLE_BACKENDS = ("cuda", "rocm", "metal", "vulkan")  # a GPU with backend "none" is not used for a model
# speed: tokens/s ~ effective memory bandwidth (GB/s) / GB read per token; (low, high) ranges, a guess by class, never a promise
CPU_BW = (20.0, 40.0)       # dual channel DDR4/DDR5, as llama.cpp reaches it
APPLE_BW = {"base": (45.0, 85.0), "pro": (100.0, 180.0), "max": (220.0, 380.0), "ultra": (450.0, 650.0)}
APPLE_CPU_FACTOR = 0.6      # the CPU cores of a Mac use less of the unified memory bandwidth than the GPU
GPU_BW = {"nvidia": (150.0, 400.0), "amd": (100.0, 300.0), "intel": (80.0, 250.0), "other": (50.0, 150.0)}
HIGH_END_VRAM_MB, HIGH_END_BW = 20000, (400.0, 900.0)  # 24 GB class cards are the fast ones whatever the vendor
SLOW_FACTOR = (0.2, 0.7)    # "slow" (swapping, memory squeezed): the speed range is cut by this
EPS = 1e-6                  # 1300 x 1.1 is 1430.0000000000002 in floating point: a boundary is "at most", not "a hair less"

# ---- probes ------------------------------------------------------------------------------------------------------------------
TOOL_TIMEOUT, SMI_TIMEOUT, PROFILER_TIMEOUT = 3, 5, 5  # seconds; nothing here may wait longer than 5
MAX_OUTPUT = 4 * 2 ** 20    # bytes of a tool's output kept
MAX_CARDS = 16              # /sys/class/drm/card0..15 are probed one by one (read() cannot list a directory)
MAX_ADAPTERS = 32           # HKLM\...\Class\{display}\0000..0031
SAFE_DIRS = ("/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin", "/sbin", "/usr/lib/wsl/lib", "/usr/local/nvidia/bin")
NVIDIA_SMI = ["--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"]
DISPLAY_CLASS = r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
CPU_KEY = r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"
VENDOR_IDS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}  # PCI vendor ids (sysfs)
FLAG_ORDER = ("avx", "avx2", "avx512", "fma", "neon", "sve")
INTEL_DISCRETE = re.compile(r"Arc\W+(?:TM\W+)?(?:Pro\W+)?[AB]\d{2,3}", re.I)  # Arc A770, B580; "Arc(TM) Graphics" is an iGPU


# ---------------------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------------------
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _clean(text):
    return " ".join((text or "").split()) or None


def _mb(nbytes):
    return nbytes // 2 ** 20 if nbytes is not None and nbytes >= 0 else None


def _os_name(plat=None):
    p = (plat or sys.platform).lower()
    if p.startswith("win"):
        return "windows"
    if p.startswith("darwin") or p == "macos":
        return "darwin"
    return "linux" if p.startswith("linux") else p


def _arch():
    m = (platform.machine() or "").lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("i386", "i486", "i586", "i686", "x86"):
        return "x86"
    return m or "unknown"


def _rd(read, path):
    """A file or registry value as text, or None when it is not there (any failure of the reader is 'not there')."""
    try:
        text = read(path)
    except Exception:  # an injected reader may raise anything
        return None
    return text if isinstance(text, str) else None


def _call(run, argv, timeout):
    """(rc, stdout); rc is None when the program could not be started (missing, refused, timed out)."""
    try:
        rc, out = run(argv, min(timeout, 5))
    except Exception:
        return None, ""
    return (rc if isinstance(rc, int) else None), (out if isinstance(out, str) else "")


def _gpu(vendor, name, vram_mb=None, free_mb=None, unified=False, backend="none", source=None):
    return {"vendor": vendor, "name": name, "vram_mb": vram_mb, "vram_free_mb": free_mb, "unified": unified,
            "backend": backend, "source": source}


def _flags(words):
    """CPU feature words (any case, any spelling) -> the few that matter for inference, in a fixed order."""
    w = {x.lower() for x in words}
    found = {"avx": bool(w & {"avx", "avx1.0"}), "avx2": "avx2" in w, "avx512": any(x.startswith("avx512") for x in w),
             "fma": "fma" in w, "neon": bool(w & {"neon", "asimd"}), "sve": "sve" in w}
    return [k for k in FLAG_ORDER if found[k]]


# ---------------------------------------------------------------------------------------------------------------------------
# Default readers (the injectable `run` and `read`)
# ---------------------------------------------------------------------------------------------------------------------------
def _which(name):
    """Executable by name in fixed places only: never the current directory, never a relative PATH entry."""
    if os.name == "nt":
        dirs = [ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")]
        dirs += [d for d in (os.environ.get("PATH") or "").split(os.pathsep) if d and ntpath.isabs(d)]
        names = (name + ".exe", name)
    else:
        dirs, names = SAFE_DIRS, (name,)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _run(argv, timeout):
    """Run a tool with a fixed argument list, no shell, no stdin; (None, '') if it cannot be started or times out."""
    exe = argv[0] if os.path.isabs(argv[0]) else _which(argv[0])
    if not exe:
        return None, ""
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
    try:
        r = subprocess.run([exe] + list(argv[1:]), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=min(timeout, 5), env=dict(os.environ, LC_ALL="C"), **kw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""
    return r.returncode, r.stdout[:MAX_OUTPUT].decode("utf-8", "replace")


def _winreg_value(spec):
    """'HKLM\\key\\path\\Value' -> the value as text (REG_QWORD/DWORD/BINARY as a decimal integer); OSError if it is not there."""
    try:
        import winreg  # Windows only
    except ImportError:
        raise OSError("no registry on this OS")
    key, _, name = spec.rpartition("\\")
    hive, _, sub = key.partition("\\")
    if hive != "HKLM":
        raise OSError("only HKLM is read")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
        v = winreg.QueryValueEx(k, name)[0]
    if isinstance(v, (bytes, bytearray)):
        return str(int.from_bytes(bytes(v[:8]), "little"))
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return str(v)


def _win_api(what):
    """Text from the Windows API (kernel32 through ctypes): 'memory' -> 'total=N available=N', 'cpu' -> 'cores=N threads=N avx=1...'."""
    import winapi  # same directory (imports on any OS, loads DLLs only when called)
    if what == "memory":
        m = winapi.memory()
        return "total=%d available=%d" % (m["total"], m["available"])
    if what == "cpu":
        out = ["threads=%d" % (os.cpu_count() or 1)]
        try:
            out.append("cores=%d" % cpuinfo.parse_slpi_ex(winapi.logical_processor_info())["cores"])
        except Exception:  # the cores are one figure among several: the others are still worth having
            pass
        k = ctypes.WinDLL("kernel32")  # IsProcessorFeaturePresent: 39 AVX, 40 AVX2, 41 AVX-512F
        for name, pf in (("avx", 39), ("avx2", 40), ("avx512", 41)):
            out.append("%s=%d" % (name, 1 if k.IsProcessorFeaturePresent(pf) else 0))
        return " ".join(out)
    raise OSError("unknown api")


def _read(path):
    if path.startswith("reg:"):
        return _winreg_value(path[4:])
    if path.startswith("api:"):
        return _win_api(path[4:])
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(MAX_OUTPUT)


# ---------------------------------------------------------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------------------------------------------------------
def parse_meminfo(text):
    """/proc/meminfo -> (total_mb, available_mb); available falls back on free+buffers+cached for kernels before 3.14."""
    kb = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+)\s*kB", text or "", re.M)}
    if "MemTotal" not in kb:
        return None, None
    avail = kb.get("MemAvailable")
    if avail is None and "MemFree" in kb:
        avail = kb["MemFree"] + kb.get("Buffers", 0) + kb.get("Cached", 0)
    total = kb["MemTotal"] // 1024
    return total, (min(avail // 1024, total) if avail is not None else None)


def parse_cpu_flags(text):
    """/proc/cpuinfo -> flag list: x86 'flags' line, ARM 'Features' line (aarch64 NEON is called asimd)."""
    for line in (text or "").splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("flags", "Features"):
            return _flags(v.split())
    return []


def parse_nvidia_smi(text):
    """`nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits` -> [(name, total_mb, free_mb)].

    A value the driver cannot give ('[N/A]', '[Not Supported]') is None; lines that are not CSV of this shape are skipped."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(.+?)\s*,\s*(\d+|\[[^\]]*\]|N/A)\s*,\s*(\d+|\[[^\]]*\]|N/A)\s*$", line)
        if m:
            out.append((m.group(1), int(m.group(2)) if m.group(2).isdigit() else None,
                        int(m.group(3)) if m.group(3).isdigit() else None))
    return out


def parse_sysctl(text):
    """`sysctl name1 name2 ...` on macOS prints 'name: value' lines -> {name: value}; unknown names print nothing here."""
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition(": ")
        if sep and k and " " not in k and k != "sysctl":  # not the 'sysctl: unknown oid' message
            out[k.strip()] = v.strip()
    return out


def _size_mb(text):
    """'4 GB', '1536 MB', '0.5 GB' -> MB (None if it is not a size)."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB)\b", text or "", re.I)
    return int(float(m.group(1)) * {"mb": 1, "gb": 1024, "tb": 1024 * 1024}[m.group(2).lower()]) if m else None


def parse_displays(text):
    """`system_profiler SPDisplaysDataType -json` (Intel Macs) -> [gpu dicts]. Dedicated VRAM is 'spdisplays_vram'; an
    integrated GPU reports 'spdisplays_vram_shared' (it borrows RAM: unified, no VRAM figure)."""
    try:
        items = json.loads(text).get("SPDisplaysDataType") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        name = _clean(d.get("sppci_model") or d.get("_name")) or "GPU"
        vendor = str(d.get("spdisplays_vendor") or "").replace("sppci_vendor_", "").lower()
        vendor = vendor if vendor in ("amd", "nvidia", "intel", "apple") else _vendor_of(name)
        vram = _size_mb(d.get("spdisplays_vram"))
        shared = d.get("spdisplays_vram_shared") is not None or (vendor == "intel" and vram is None)
        if shared or vram is None:
            out.append(_gpu(vendor, name, unified=shared, source="system_profiler"))
        else:
            out.append(_gpu(vendor, name, vram, None, False, "metal", "system_profiler"))
    return out


def _vendor_of(name, device_id=""):
    """vendor from a PCI id ('pci\\ven_10de&dev_...') or, failing that, from the adapter's name."""
    m = re.search(r"ven_([0-9a-f]{4})", (device_id or "").lower())
    if m and "0x" + m.group(1) in VENDOR_IDS:
        return VENDOR_IDS["0x" + m.group(1)]
    n = (name or "").lower()
    for key, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("quadro", "nvidia"), ("amd", "amd"), ("radeon", "amd"),
                        ("intel", "intel"), ("apple", "apple")):
        if key in n:
            return vendor
    return "other"


def adapter_gpu(name, device_id, mem_bytes, source="registry"):
    """One Windows display adapter (registry values) -> gpu dict, or None for virtual adapters (no memory, no known vendor)."""
    vendor = _vendor_of(name, device_id)
    vram = _mb(mem_bytes) or None
    if vendor == "other" and vram is None:
        return None  # Microsoft Basic Display, Hyper-V video, remote desktop: not something a model runs on
    if vendor == "intel" and not INTEL_DISCRETE.search(name or ""):
        return _gpu("intel", name, unified=True, source=source)  # integrated: borrows system RAM
    if vendor == "amd" and vram is not None and vram < APU_VRAM_MAX_MB:
        return _gpu("amd", name, unified=True, source=source)  # an APU's carve-out is not VRAM
    backend = {"nvidia": "cuda", "amd": "vulkan", "intel": "vulkan"}.get(vendor, "none")
    return _gpu(vendor, name, vram, None, False, backend if vram else "none", source)


# ---------------------------------------------------------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------------------------------------------------------
def _part(hw, label, fn, *args):
    """Run one part of the detection: a failure is one short note, the other parts go on."""
    try:
        fn(*args)
    except Exception as e:  # nothing may escape detect()
        hw["notes"].append(f"{label}: unreadable ({e.__class__.__name__})")


def _nvidia(hw, run, argv0s):
    """(listed, missing): nvidia-smi's GPUs are added to hw; `missing` = the tool is not there (a note only where that matters,
    the caller knows); a tool that ran and failed leaves its own note. `argv0s`: where to look (a bare name, then a known path)."""
    rc, out = None, ""
    for exe in argv0s:
        rc, out = _call(run, [exe] + NVIDIA_SMI, SMI_TIMEOUT)
        if rc is not None:
            break
    if rc is None:
        return False, True
    rows = parse_nvidia_smi(out)
    if rc != 0 or not rows:
        hw["notes"].append(f"nvidia-smi failed (rc {rc})" if rc != 0 else "nvidia-smi: no GPU in its output")
        return False, False
    for name, total, free in rows:
        hw["gpus"].append(_gpu("nvidia", name, total, free, False, "cuda", "nvidia-smi"))
        if total is None:  # shared-memory parts (Jetson, GB10) answer [N/A]: nothing to compare a model with
            hw["notes"].append(f"{_short(name)}: nvidia-smi does not report its memory")
    return True, False


def _linux_ram(hw, read):
    text = _rd(read, "/proc/meminfo")
    if text is None:
        raise OSError("/proc/meminfo")
    hw["ram"]["total_mb"], hw["ram"]["available_mb"] = parse_meminfo(text)


def _linux_cpu(hw, read):
    text = _rd(read, "/proc/cpuinfo")
    if text is None:
        raise OSError("/proc/cpuinfo")
    info = cpuinfo.parse_cpuinfo(text)
    threads = len(re.findall(r"^processor\s*:", text, re.M)) or None
    cores = len(info["pairs"]) or (threads if hw["arch"] in ("arm64", "arm") else None)  # ARM has no core ids; no SMT to speak of
    hw["cpu"].update({"model": info["model"], "cores": cores, "threads": threads, "flags": parse_cpu_flags(text)})


def _linux_cards(hw, read):
    """(nvidia cards seen, [gpu dicts of AMD and Intel cards]) from /sys/class/drm/card0..N."""
    nv, gpus = [], []
    for n in range(MAX_CARDS):
        base = f"/sys/class/drm/card{n}/"
        vendor = VENDOR_IDS.get((_rd(read, base + "device/vendor") or "").strip().lower())
        dev = (_rd(read, base + "device/device") or "").strip().lower().replace("0x", "")
        if vendor == "nvidia":
            nv.append(_gpu("nvidia", f"NVIDIA GPU 10de:{dev or '????'}", source="sysfs"))
        elif vendor == "amd":
            total = _int(_rd(read, base + "device/mem_info_vram_total"))
            used = _int(_rd(read, base + "device/mem_info_vram_used"))
            name = _clean(_rd(read, base + "device/product_name")) or f"AMD GPU 1002:{dev or '????'}"
            if total is None:
                hw["notes"].append("AMD GPU: its memory is not readable (amdgpu driver?)")
                gpus.append(_gpu("amd", name, source="sysfs"))
                continue
            vram, free = _mb(total), (_mb(total - used) if used is not None else None)
            if vram < APU_VRAM_MAX_MB:
                gpus.append(_gpu("amd", name, unified=True, source="sysfs"))  # an APU: its "VRAM" is a carve-out of the RAM
            else:
                gpus.append(_gpu("amd", name, vram, free, False, "rocm", "sysfs"))
        elif vendor == "intel":
            name = f"Intel GPU 8086:{dev or '????'}"
            lmem = _int(_rd(read, base + "lmem_total_bytes"))  # only a card with its own memory (Arc) has local memory
            if lmem:
                avail = _int(_rd(read, base + "lmem_avail_bytes"))
                gpus.append(_gpu("intel", name, _mb(lmem), _mb(avail), False, "vulkan", "sysfs"))
            else:
                gpus.append(_gpu("intel", name, unified=True, source="sysfs"))
    return nv, gpus


def _linux_gpus(hw, run, read):
    nv_cards, gpus = _linux_cards(hw, read)
    hw["gpus"].extend(gpus)
    listed, missing = _nvidia(hw, run, ["nvidia-smi"])
    if not listed and nv_cards:  # the card is there, its tool is not: say what is known (no NVIDIA card: a missing tool is no news)
        hw["gpus"].extend(nv_cards)
        if missing:
            hw["notes"].append("nvidia-smi not found: the NVIDIA GPU's memory is unknown (driver not installed?)")


def _linux(hw, run, read):
    _part(hw, "RAM", _linux_ram, hw, read)
    _part(hw, "CPU", _linux_cpu, hw, read)
    _part(hw, "GPU", _linux_gpus, hw, run, read)


def _darwin(hw, run, read):
    names = ["hw.memsize", "machdep.cpu.brand_string", "hw.ncpu", "hw.physicalcpu", "hw.optional.arm64",
             "machdep.cpu.features", "machdep.cpu.leaf7_features"]
    rc, out = _call(run, ["/usr/sbin/sysctl"] + names, TOOL_TIMEOUT)
    sysctl = parse_sysctl(out)  # a name this Mac does not know (the Intel ones on Apple silicon) makes rc 1: not an error
    if "hw.memsize" not in sysctl and "machdep.cpu.brand_string" not in sysctl:
        hw["notes"].append("sysctl not available" if rc is None else f"sysctl failed (rc {rc})")
        return
    brand = _clean(sysctl.get("machdep.cpu.brand_string"))
    arm = sysctl.get("hw.optional.arm64") == "1" or bool(brand and brand.startswith("Apple"))
    if arm:
        hw["arch"] = "arm64"
    words = (sysctl.get("machdep.cpu.features", "") + " " + sysctl.get("machdep.cpu.leaf7_features", "")).split()
    flags = ["neon"] if arm else _flags(words)
    hw["cpu"].update({"model": brand, "cores": _int(sysctl.get("hw.physicalcpu")), "threads": _int(sysctl.get("hw.ncpu")),
                      "flags": flags})
    total = _int(sysctl.get("hw.memsize"))
    hw["ram"]["total_mb"] = _mb(total)
    rc, out = _call(run, ["/usr/bin/vm_stat"], TOOL_TIMEOUT)
    pages, page = hostinfo.parse_vm_stat(out) if rc == 0 else ({}, 4096)
    if pages and total:
        free = (pages.get("free", 0) + pages.get("speculative", 0) + pages.get("inactive", 0)) * page
        hw["ram"]["available_mb"] = min(_mb(free), hw["ram"]["total_mb"])
    else:
        hw["notes"].append("vm_stat not available: free memory unknown")
    if arm:  # unified memory: the GPU is the RAM (Metal); its "VRAM" is what the RAM has
        name = (brand or "Apple silicon") + " GPU"
        hw["gpus"].append(_gpu("apple", name, hw["ram"]["total_mb"], hw["ram"]["available_mb"], True, "metal", "sysctl"))
        return
    rc, out = _call(run, ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"], PROFILER_TIMEOUT)
    gpus = parse_displays(out) if rc == 0 else []
    if not gpus:
        hw["notes"].append("system_profiler: no GPU information")
    hw["gpus"].extend(gpus)


def _windows_ram(hw, read):
    text = _rd(read, "api:memory")
    kv = dict(re.findall(r"(\w+)=(\d+)", text or ""))
    if "total" not in kv:
        raise OSError("GlobalMemoryStatusEx")
    hw["ram"]["total_mb"] = _mb(int(kv["total"]))
    hw["ram"]["available_mb"] = _mb(int(kv["available"])) if "available" in kv else None


def _windows_cpu(hw, read):
    kv = dict(re.findall(r"(\w+)=(\d+)", _rd(read, "api:cpu") or ""))
    words = [k for k in ("avx", "avx2", "avx512") if kv.get(k) == "1"]
    if hw["arch"] == "arm64":
        words.append("neon")  # every ARM64 Windows CPU has it
    hw["cpu"].update({"model": _clean(_rd(read, "reg:" + CPU_KEY + r"\ProcessorNameString")),
                      "cores": int(kv["cores"]) if "cores" in kv else None,
                      "threads": int(kv["threads"]) if "threads" in kv else None, "flags": _flags(words)})


def _windows_gpus(hw, run, read):
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    smi, missing = _nvidia(hw, run, ["nvidia-smi", ntpath.join(pf, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")])
    adapters, has_nvidia = [], False
    for n in range(MAX_ADAPTERS):
        key = "reg:%s\\%04d\\" % (DISPLAY_CLASS, n)
        name = _clean(_rd(read, key + "DriverDesc"))
        if not name:
            continue
        g = adapter_gpu(name, _rd(read, key + "MatchingDeviceId"), _int(_rd(read, key + "HardwareInformation.qwMemorySize")))
        if g and not (g["vendor"] == "nvidia" and smi):  # the registry would list nvidia-smi's cards again
            adapters.append(g)
            has_nvidia = has_nvidia or g["vendor"] == "nvidia"
    hw["gpus"].extend(adapters)
    if has_nvidia and missing:
        hw["notes"].append("nvidia-smi not found: the free memory of the NVIDIA GPU is unknown")
    if not adapters and not smi:
        hw["notes"].append("no display adapter with a memory size in the registry")


def _windows(hw, run, read):
    _part(hw, "RAM", _windows_ram, hw, read)
    _part(hw, "CPU", _windows_cpu, hw, read)
    _part(hw, "GPU", _windows_gpus, hw, run, read)


def detect(plat=None, run=None, read=None):
    """The machine: {"os", "arch", "cpu", "ram", "gpus", "notes"} (the shapes are in the contract: feat/ai, section 1).

    `plat` is a sys.platform-like string (default: this OS); `run(argv, timeout) -> (rc, stdout)` and `read(path) -> str`
    stand in for the tools and files (default: the real ones). What cannot be read is None, and says why in "notes"."""
    osname = _os_name(plat)
    hw = {"os": osname, "arch": _arch(), "cpu": {"model": None, "cores": None, "threads": None, "flags": []},
          "ram": {"total_mb": None, "available_mb": None}, "gpus": [], "notes": []}
    run, read = run or _run, read or _read
    try:
        steps = {"linux": _linux, "darwin": _darwin, "windows": _windows}.get(osname)
        if steps is None:
            hw["notes"].append(f"hardware detection is not implemented for {osname}")
        else:
            _part(hw, "hardware", steps, hw, run, read)
    except Exception as e:  # belt and braces: detect() never raises
        hw["notes"].append(f"hardware: unreadable ({e.__class__.__name__})")
    hw["gpus"].sort(key=lambda g: (g["unified"], -(g["vram_mb"] or 0)))  # dedicated GPUs first, the biggest first
    seen = set()
    hw["notes"] = [n for n in hw["notes"] if not (n in seen or seen.add(n))]
    return hw


_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "hw": None}


def cached(max_age=300):
    """detect() at most every `max_age` seconds per process (thread-safe); a copy, so a caller may change it."""
    with _LOCK:
        now = time.monotonic()
        if _CACHE["hw"] is None or max_age <= 0 or now - _CACHE["at"] >= max_age:
            _CACHE["hw"], _CACHE["at"] = detect(), time.monotonic()
        return copy.deepcopy(_CACHE["hw"])


# ---------------------------------------------------------------------------------------------------------------------------
# Does a model fit?
# ---------------------------------------------------------------------------------------------------------------------------
def _layers(model):
    n = model.get("layers")
    return int(n) if _num(n) and n > 0 else DEFAULT_LAYERS


def weights_mb(model):
    """Size of the weights in MB: approx_mb, else the pinned size (bytes), else params_b x 600 MB, else the old ram_mb."""
    if _num(model.get("approx_mb")) and model["approx_mb"] > 0:
        return float(model["approx_mb"])
    if _num(model.get("size")) and model["size"] > 0:
        return model["size"] / 2.0 ** 20
    if _num(model.get("params_b")) and model["params_b"] > 0:
        return model["params_b"] * MB_PER_B_PARAMS
    if _num(model.get("ram_mb")) and model["ram_mb"] > 0:  # aisetup's older field: weights + cache + overhead at 4096 tokens
        return max(model["ram_mb"] - OVERHEAD_MB - _kv_mb(model, DEFAULT_CTX), model["ram_mb"] * 0.5)
    return float(FALLBACK_WEIGHTS_MB)


def _kv_mb(model, ctx):
    return max(0, ctx) * _layers(model) * KV_BYTES_PER_LAYER_TOKEN / 2.0 ** 20


def need_mb(model, ctx=DEFAULT_CTX):
    """Memory to run `model` with `ctx` tokens of context: weights + KV cache + about 300 MB of runtime."""
    return int(math.ceil(weights_mb(model) + _kv_mb(model, ctx) + OVERHEAD_MB))


def _active_gb(model):
    """GB read from memory per generated token: all the weights, or only the active experts of a mixture of experts."""
    w = weights_mb(model)
    act, par = model.get("active_b"), model.get("params_b")
    if _num(act) and act > 0:
        w = w * min(1.0, act / par) if _num(par) and par > 0 else act * MB_PER_B_PARAMS
    return max(w, 1.0) / 1024.0


def _rate(x):
    return int(round(x)) if x >= 10 else round(max(x, 0.1), 1)


def _tok_s(model, parts, slow=False):
    """[lo, hi] tokens/s from [(share of the weights, (bw_lo, bw_hi) in GB/s), ...]: time per token = sum(share / bandwidth)."""
    gb = _active_gb(model)
    lo = 1.0 / (gb * sum(f / bw[0] for f, bw in parts if f > 0))
    hi = 1.0 / (gb * sum(f / bw[1] for f, bw in parts if f > 0))
    if slow:
        lo, hi = lo * SLOW_FACTOR[0], hi * SLOW_FACTOR[1]
    return [_rate(lo), _rate(hi)]


def _apple_class(name):
    m = re.search(r"\bM\d+\s*(Pro|Max|Ultra)?", name or "", re.I)
    return (m.group(1) or "base").lower() if m else "base"


def _gpu_bw(g):
    if g.get("vendor") == "apple":
        return APPLE_BW[_apple_class(g.get("name"))]
    if (g.get("vram_mb") or 0) >= HIGH_END_VRAM_MB:
        return HIGH_END_BW
    return GPU_BW.get(g.get("vendor"), GPU_BW["other"])


def _ram(hw):
    r = (hw or {}).get("ram") or {}
    total, avail = r.get("total_mb"), r.get("available_mb")
    total = total if _num(total) and total > 0 else None
    avail = avail if _num(avail) and avail >= 0 else None
    return total, (min(avail, total) if avail is not None and total else avail)


def _gb(mb):
    g = mb / 1024.0
    return ("%.0f" if g >= 10 or abs(g - round(g)) < 0.05 else "%.1f") % g


def _short(name):
    """'NVIDIA GeForce RTX 3060' -> 'RTX 3060' (the way people say it)."""
    n = _clean((name or "").replace("(R)", "").replace("(TM)", "")) or "GPU"
    return re.sub(r"^(?:(?:NVIDIA|GeForce)\s+)+", "", n) or n


def _free_mb(g):
    """VRAM a model can use now: the free figure, or the total less what the display keeps; None if unknown."""
    if _num(g.get("vram_free_mb")):
        return g["vram_free_mb"]
    if _num(g.get("vram_mb")) and g["vram_mb"] > 0:
        return max(0, g["vram_mb"] - GPU_DISPLAY_RESERVE_MB)
    return None


def _cpu_verdict(need, total, avail):
    """ram | slow | no for a part of `need` MB held by the system RAM; (verdict, why-fragment)."""
    if total is None:
        return "slow", "the RAM size could not be read"
    if need > RAM_MAX_FRAC * total + EPS:
        return "no", f"more than {int(RAM_MAX_FRAC * 100)}% of the {_gb(total)} GB of RAM"
    if need <= RAM_COMFY_FRAC * total + EPS and (avail is None or need <= avail - RAM_RESERVE_MB):
        return "ram", f"this machine has {_gb(total)} GB of RAM" + (f" ({_gb(avail)} GB free)" if avail is not None else "")
    if avail is not None and need > avail - RAM_RESERVE_MB:
        return "slow", f"only {_gb(avail)} GB of the {_gb(total)} GB of RAM are free now"
    return "slow", f"more than half of the {_gb(total)} GB of RAM"


def assess(model, hw, ctx=DEFAULT_CTX):
    """Can this machine run `model` (a dict as in aisetup.MODELS), and how well?

    {"verdict": gpu|partial|ram|slow|no, "where": GPU|GPU+CPU|CPU, "need_mb", "gpu_layers" (0 = CPU only; more than the model's
    layers = all of them, output layer included: pass it as -ngl), "tok_s": [lo, hi] or None (an estimate), "why": a sentence}."""
    hw = hw or {}
    need, layers = need_mb(model, ctx), _layers(model)
    total, avail = _ram(hw)
    gpus = [g for g in (hw.get("gpus") or []) if isinstance(g, dict)]
    res = {"verdict": "no", "where": "CPU", "need_mb": need, "gpu_layers": 0, "tok_s": None, "why": ""}
    cpu_bw = CPU_BW
    # Apple silicon: the GPU is the RAM; it holds the model while about two thirds of the RAM and what is free allow
    apple = next((g for g in gpus if g.get("vendor") == "apple" and g.get("unified") and g.get("backend") in USABLE_BACKENDS), None)
    if apple:
        bw = _gpu_bw(apple)
        cpu_bw = (bw[0] * APPLE_CPU_FACTOR, bw[1] * APPLE_CPU_FACTOR)
        if total and need <= UNIFIED_MAX_FRAC * total + EPS and (avail is None or need <= avail):
            res.update(verdict="gpu", where="GPU", gpu_layers=layers + 1, tok_s=_tok_s(model, [(1.0, bw)]))
            res["why"] = (f"needs {_gb(need)} GB of the {_gb(total)} GB of unified memory"
                          + (f" ({_gb(avail)} GB free)" if avail is not None else "") + ": all on the GPU (Metal)")
            return res
    # a dedicated GPU: the one with the most memory free now
    best, best_free = None, 0
    for g in gpus:
        free = None if g.get("unified") or g.get("backend") not in USABLE_BACKENDS else _free_mb(g)
        if free and free > best_free:
            best, best_free = g, free
    gname = _short(best["name"]) if best else ""
    if best and best_free + EPS >= need * GPU_FIT:
        res.update(verdict="gpu", where="GPU", gpu_layers=layers + 1, tok_s=_tok_s(model, [(1.0, _gpu_bw(best))]))
        res["why"] = f"needs {_gb(need)} GB, the {gname} has {_gb(best_free)} GB free: all on the GPU"
        return res
    on_gpu, frac = 0, 0.0
    if best:
        fit = best_free / (need * GPU_FIT)  # share of the model whose layers (and their cache) the free VRAM can hold
        on_gpu = int(layers * fit + EPS)
        frac = on_gpu / float(layers)
        if frac < PARTIAL_MIN_FRAC or on_gpu < 1:
            on_gpu, frac = 0, 0.0
    rest = need * (1.0 - frac)  # what the system RAM must hold
    verdict, why = _cpu_verdict(rest, total, avail)
    tail = "cannot tell if it fits" if total is None else "it runs, but the PC will slow down (swapping, other programs squeezed)"
    if frac:
        parts = [(frac, _gpu_bw(best)), (1.0 - frac, cpu_bw)]
        head = f"needs {_gb(need)} GB, the {gname} has {_gb(best_free)} GB free"
        res.update(where="GPU+CPU", gpu_layers=on_gpu)
        split = f"{on_gpu} of {layers} layers on the GPU, the rest ({_gb(rest)} GB) in RAM"
        if verdict == "ram":
            res.update(verdict="partial", tok_s=_tok_s(model, parts))
            res["why"] = f"{head}: {split}; works, slower than a GPU that holds it all"
        elif verdict == "slow":
            res.update(verdict="slow", tok_s=_tok_s(model, parts, True) if total else None)
            res["why"] = f"{head}: {split}, but {why}: {tail}"
        else:  # even the part left for the RAM is too big
            res.update(where="CPU", gpu_layers=0)
            res["why"] = f"{head}, and the rest of the model would take {why}: it will not work"
        return res
    res["verdict"] = verdict
    if verdict == "ram":
        res["tok_s"] = _tok_s(model, [(1.0, cpu_bw)])
        res["why"] = f"needs {_gb(need)} GB, {why}: runs on the CPU, comfortably"
    elif verdict == "slow":
        res["tok_s"] = _tok_s(model, [(1.0, cpu_bw)], True) if total else None
        res["why"] = f"needs {_gb(need)} GB but {why}: {tail}" if total is None else f"needs {_gb(need)} GB, {why}: {tail}"
    else:
        res["why"] = f"needs {_gb(need)} GB, {why} and no GPU that can hold it: it will not work"
    return res


def recommend(models, hw):
    """Id of the model to suggest: the best-ranked one that fits comfortably (all on the GPU, or in RAM: a "partial" one counts
    when the RAM alone would hold it just as well); else the best-ranked partial one; else the smallest that is not "no";
    None if nothing fits. Rank 1 is best; no rank = list order, after the ranked ones."""
    order = sorted(range(len(models)), key=lambda i: (models[i].get("rank") if _num(models[i].get("rank")) else 10 ** 9, i))
    verdicts = {i: assess(models[i], hw)["verdict"] for i in order}
    no_gpu = dict(hw or {}, gpus=[])
    for i in order:
        if verdicts[i] in ("gpu", "ram") or (verdicts[i] == "partial" and assess(models[i], no_gpu)["verdict"] == "ram"):
            return models[i].get("id")
    for i in order:
        if verdicts[i] == "partial":
            return models[i].get("id")
    left = [i for i in order if verdicts[i] != "no"]
    return models[min(left, key=lambda i: need_mb(models[i]))].get("id") if left else None
