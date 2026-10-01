"""Host metrics for the renderer on macOS and Windows (unprivileged, standard library only).

Linux reads /proc and /sys directly in render.py; this module gives the same shapes on the other two systems:
  cpu_times()    {'cpu0': (busy, total), ...}    monotonic counters, render.py turns deltas into percentages
  net_counters() {interface: (rx_bytes, tx_bytes)}
  meminfo()      {'MemTotal', 'MemAvailable', 'Cached', 'SwapTotal', 'SwapFree'} in bytes (the /proc/meminfo keys)
  loadavg()      ['0.12', '0.30', '0.25'] or None (Windows has no load average)
  uptime()       seconds since boot
  root_disk()    (used, total, label) of the system volume
  filesystems()  [{'mount', 'used', 'total'}]
  sessions()     {'local': [{'user', 'tty'}], 'ssh': [peer IPs], 'rdp': [peer IPs]}
  screen_size()  (width, height) in pixels or None
Every function raises on failure: render.py shows "unavailable", never an invented value.
The text parsers are pure functions, tested with fixtures on every OS.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time

WINDOWS, MACOS = sys.platform == "win32", sys.platform == "darwin"
if WINDOWS:
    import winapi

UNIX_TOOLS = "/usr/sbin:/usr/bin:/sbin:/bin"
MAC_NET_SKIP = ("lo", "gif", "stf", "anpi", "awdl", "llw", "ap", "bridge")  # loopback, tunnels of the system, Apple links
MAC_FS = {"apfs", "hfs", "exfat", "msdos", "ntfs", "smbfs", "nfs", "afpfs", "webdav"}


def _run(*args, timeout=3):
    """Output of a system tool (fixed path list, no shell); raises if it fails: the block then says 'unavailable'."""
    exe = shutil.which(args[0], path=UNIX_TOOLS)
    if not exe:
        raise FileNotFoundError(args[0])
    r = subprocess.run([exe, *args[1:]], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} rc={r.returncode}")
    return r.stdout


# ---- macOS: parsers (pure) ---------------------------------------------------------------------------------------------

def parse_vm_stat(text):
    """`vm_stat` -> ({'free': pages, 'inactive': ..., 'file_backed': ...}, page_size)."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    names = {"Pages free": "free", "Pages active": "active", "Pages inactive": "inactive", "Pages speculative": "speculative",
             "Pages wired down": "wired", "Pages purgeable": "purgeable", "File-backed pages": "file_backed"}
    out = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        v = v.strip().rstrip(".")
        if k.strip() in names and v.isdigit():
            out[names[k.strip()]] = int(v)
    return out, page


def parse_swapusage(text):
    """`sysctl -n vm.swapusage` 'total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)' -> (total, used) bytes."""
    mult = {"K": 2 ** 10, "M": 2 ** 20, "G": 2 ** 30, "T": 2 ** 40}
    vals = {k: float(v) * mult.get(u, 1) for k, v, u in re.findall(r"(total|used|free) = ([\d.]+)([KMGT]?)", text)}
    return int(vals.get("total", 0)), int(vals.get("used", 0))


def parse_boottime(text):
    """`sysctl -n kern.boottime` '{ sec = 1727760000, usec = 5 } Tue Oct  1 ...' -> epoch seconds."""
    m = re.search(r"sec = (\d+)", text)
    if not m:
        raise ValueError("kern.boottime unreadable")
    return int(m.group(1))


def parse_netstat_ib(text):
    """`netstat -ibn` -> {interface: (rx_bytes, tx_bytes)} from the <Link#N> rows.

    The Address column is empty for interfaces without a MAC (utun...): the byte counters are read from the end of the line
    (Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll), which never moves."""
    out = {}
    for ln in text.splitlines():
        f = ln.split()
        if len(f) < 9 or not f[2].startswith("<Link#") or f[0].rstrip("*").startswith(MAC_NET_SKIP):
            continue
        name = f[0].rstrip("*")
        if name not in out and f[-5].isdigit() and f[-2].isdigit():
            out[name] = (int(f[-5]), int(f[-2]))
    return out


def parse_mount(text):
    """`mount` -> [mountpoint] of real volumes: one per APFS container (its volumes share the same space), no hidden
    system volumes ('nobrowse'), no Time Machine snapshots."""
    out, containers = [], set()
    for ln in text.splitlines():
        m = re.match(r"^(\S+) on (.+) \(([^,)]+)(.*)\)$", ln)
        if not m:
            continue
        dev, mp, fstype, opts = m.groups()
        if fstype not in MAC_FS or "nobrowse" in opts or ".timemachine" in mp or mp.startswith("/System/Volumes/"):
            continue
        cont = re.match(r"/dev/(disk\d+)", dev)
        key = cont.group(1) if cont and fstype == "apfs" else dev
        if key in containers:
            continue
        containers.add(key)
        out.append(mp)
    return out


def parse_who(text):
    """`who` -> local sessions [{'user', 'tty'}]: lines with a '(host)' are remote logins (counted from the network side)."""
    out = []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) >= 2 and not ln.rstrip().endswith(")"):
            out.append({"user": f[0], "tty": f[1]})
    return out


def parse_netstat_established(text, ports=(22,)):
    """`netstat -an -p tcp` -> {port: sorted peer IPs} of ESTABLISHED connections to the given local ports.

    macOS separates the port with a dot: '192.0.2.5.22', 'fe80::1%en0.22'."""
    out = {p: set() for p in ports}
    for ln in text.splitlines():
        f = ln.split()
        if len(f) < 6 or not f[0].startswith("tcp") or f[5] != "ESTABLISHED":
            continue
        laddr, _, lport = f[3].rpartition(".")
        paddr = f[4].rpartition(".")[0]
        if lport.isdigit() and int(lport) in out and paddr:
            out[int(lport)].add(paddr.split("%")[0])
    return {p: sorted(v) for p, v in out.items()}


# ---- macOS: system calls -------------------------------------------------------------------------------------------------

def _libsystem():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.mach_host_self.restype = ctypes.c_uint32
    return lib


def _sysctl_int(name):
    lib = _libsystem()
    buf, size = ctypes.create_string_buffer(8), ctypes.c_size_t(8)
    lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    if lib.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name} failed")
    return int.from_bytes(buf.raw[:size.value], "little")


class _VmStats64(ctypes.Structure):  # struct vm_statistics64 (mach/vm_statistics.h), 152 bytes
    _fields_ = [("free_count", ctypes.c_uint32), ("active_count", ctypes.c_uint32), ("inactive_count", ctypes.c_uint32),
                ("wire_count", ctypes.c_uint32), ("zero_fill_count", ctypes.c_uint64), ("reactivations", ctypes.c_uint64),
                ("pageins", ctypes.c_uint64), ("pageouts", ctypes.c_uint64), ("faults", ctypes.c_uint64),
                ("cow_faults", ctypes.c_uint64), ("lookups", ctypes.c_uint64), ("hits", ctypes.c_uint64), ("purges", ctypes.c_uint64),
                ("purgeable_count", ctypes.c_uint32), ("speculative_count", ctypes.c_uint32), ("decompressions", ctypes.c_uint64),
                ("compressions", ctypes.c_uint64), ("swapins", ctypes.c_uint64), ("swapouts", ctypes.c_uint64),
                ("compressor_page_count", ctypes.c_uint32), ("throttled_count", ctypes.c_uint32),
                ("external_page_count", ctypes.c_uint32), ("internal_page_count", ctypes.c_uint32),
                ("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]


def _mac_cpu():
    """Per-core ticks from host_processor_info(PROCESSOR_CPU_LOAD_INFO): user, system, idle, nice for each CPU."""
    lib = _libsystem()
    count, info, info_cnt = ctypes.c_uint32(0), ctypes.POINTER(ctypes.c_uint32)(), ctypes.c_uint32(0)
    if lib.host_processor_info(lib.mach_host_self(), 2, ctypes.byref(count), ctypes.byref(info), ctypes.byref(info_cnt)) != 0:
        raise OSError("host_processor_info failed")
    try:
        out = {}
        for i in range(count.value):
            user, system, idle, nice = (info[i * 4 + j] for j in range(4))
            out[f"cpu{i}"] = (user + system + nice, user + system + nice + idle)
        return out
    finally:
        task = ctypes.c_uint32.in_dll(lib, "mach_task_self_")
        lib.vm_deallocate(task, ctypes.cast(info, ctypes.c_void_p), ctypes.c_size_t(info_cnt.value * 4))


def _mac_meminfo():
    lib = _libsystem()
    total = _sysctl_int("hw.memsize")
    try:
        vm, cnt, page = _VmStats64(), ctypes.c_uint32(ctypes.sizeof(_VmStats64) // 4), ctypes.c_size_t(0)
        if lib.host_statistics64(lib.mach_host_self(), 4, ctypes.byref(vm), ctypes.byref(cnt)) != 0 \
                or lib.host_page_size(lib.mach_host_self(), ctypes.byref(page)) != 0:
            raise OSError("host_statistics64 failed")
        free, inactive, cached, page = vm.free_count, vm.inactive_count, vm.external_page_count, page.value
    except (OSError, AttributeError):  # fall back on the command line tool
        st, page = parse_vm_stat(_run("vm_stat"))
        free, inactive, cached = st.get("free", 0) + st.get("speculative", 0), st.get("inactive", 0), st.get("file_backed", 0)
    swap_total, swap_used = _swap()
    return {"MemTotal": total, "MemAvailable": min(total, (free + inactive) * page), "Cached": cached * page,
            "SwapTotal": swap_total, "SwapFree": swap_total - swap_used}


_BOOT = []
_SWAP = {"ts": 0.0, "v": (0, 0)}


def _swap():
    """Swap changes slowly: one `sysctl` every 10 s, not at every 2 s frame."""
    if time.time() - _SWAP["ts"] > 10:
        _SWAP["v"], _SWAP["ts"] = parse_swapusage(_run("sysctl", "-n", "vm.swapusage")), time.time()
    return _SWAP["v"]


def _mac_boottime():
    if not _BOOT:
        _BOOT.append(parse_boottime(_run("sysctl", "-n", "kern.boottime")))
    return _BOOT[0]


def _mac_screen():
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayPixelsWide.restype = cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    d = cg.CGMainDisplayID()
    w, h = cg.CGDisplayPixelsWide(d), cg.CGDisplayPixelsHigh(d)
    return (w, h) if w and h else None


# ---- public API ---------------------------------------------------------------------------------------------------------

def cpu_times():
    if WINDOWS:
        return winapi.cpu_times()
    if MACOS:
        return _mac_cpu()
    return {}


def net_counters():
    if WINDOWS:  # adapters that never moved a byte are the virtual Wi-Fi Direct / WSL switch placeholders: noise
        return {k: v for k, v in winapi.interfaces().items() if v[0] or v[1]}
    if MACOS:
        return parse_netstat_ib(_run("netstat", "-ibn"))
    return {}


def meminfo():
    if WINDOWS:
        m = winapi.memory()
        return {"MemTotal": m["total"], "MemAvailable": m["available"], "Cached": m["cache"], "SwapTotal": 0, "SwapFree": 0}
    if MACOS:
        return _mac_meminfo()
    raise OSError("no memory information on this OS")


def loadavg():
    try:
        return [f"{x:.2f}" for x in os.getloadavg()]
    except (AttributeError, OSError):  # Windows: there is no load average
        return None


def uptime():
    if WINDOWS:
        return winapi.uptime()
    if MACOS:
        return time.time() - _mac_boottime()
    raise OSError("no uptime on this OS")


def root_disk():
    root = (os.environ.get("SystemDrive") or "C:") + "\\" if WINDOWS else "/"
    du = shutil.disk_usage(root)
    return du.used, du.total, root.rstrip("\\")


def filesystems():
    if WINDOWS:
        mounts = [r for r, _ in winapi.drives()]
    elif MACOS:
        mounts = parse_mount(_run("mount"))
    else:
        return []
    out = []
    for mp in mounts:
        try:
            du = shutil.disk_usage(mp)
        except OSError:  # removable drive without media, unreachable network share
            continue
        if du.total:
            out.append({"mount": mp.rstrip("\\") if WINDOWS else mp, "used": du.used, "total": du.total})
    return out


def sessions():
    if WINDOWS:
        local = [{"user": s["user"], "tty": s["station"] + ("" if s["state"] == "active" else f" ({s['state']})")}
                 for s in winapi.sessions()]
        peers = {22: set(), 3389: set()}
        for c in winapi.tcp_table():
            if c["state"] == winapi.TCP_ESTABLISHED and c["port"] in peers:
                peers[c["port"]].add(c["raddr"])
        return {"local": local, "ssh": sorted(peers[22]), "rdp": sorted(peers[3389])}
    if MACOS:
        peers = parse_netstat_established(_run("netstat", "-an", "-p", "tcp"), ports=(22, 5900))
        return {"local": parse_who(_run("who")), "ssh": peers[22], "vnc": peers[5900]}
    raise OSError("no session information on this OS")


def screen_size():
    try:
        return winapi.screen_size() if WINDOWS else _mac_screen() if MACOS else None
    except (OSError, AttributeError):
        return None
