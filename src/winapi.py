"""The few Windows API calls nuc-console needs, through ctypes (standard library only).

Used by hostinfo.py (renderer, unprivileged: CPU, memory, interfaces, sessions, drives) and by collect_windows.py
(collector, runs as SYSTEM: TCP/UDP tables with the owning process). The DLLs are loaded only when a function is called,
so this module imports on any OS: the buffer parsers below are plain bytes -> data and are tested on Linux too.
Every call raises OSError when Windows refuses; callers turn that into "unavailable", never into a reassuring value.
Structures use fixed-size types only (no c_wchar: 2 bytes on Windows, 4 elsewhere), so their size is the same everywhere.
"""
import ctypes
import ipaddress
import os
import struct
from ctypes import c_int, c_int64, c_ubyte, c_uint16, c_uint32, c_uint64, c_void_p

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_ALL, UDP_TABLE_OWNER_PID = 5, 1
TCP_LISTEN, TCP_ESTABLISHED = 2, 5
ERROR_INSUFFICIENT_BUFFER = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
IF_TYPE_LOOPBACK, IF_OPER_UP = 24, 1
DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_RAMDISK = 2, 3, 4, 6


class TcpRow(ctypes.Structure):  # MIB_TCPROW_OWNER_PID
    _fields_ = [("state", c_uint32), ("laddr", c_uint32), ("lport", c_uint32), ("raddr", c_uint32), ("rport", c_uint32),
                ("pid", c_uint32)]


class Tcp6Row(ctypes.Structure):  # MIB_TCP6ROW_OWNER_PID
    _fields_ = [("laddr", c_ubyte * 16), ("lscope", c_uint32), ("lport", c_uint32), ("raddr", c_ubyte * 16),
                ("rscope", c_uint32), ("rport", c_uint32), ("state", c_uint32), ("pid", c_uint32)]


class UdpRow(ctypes.Structure):  # MIB_UDPROW_OWNER_PID
    _fields_ = [("laddr", c_uint32), ("lport", c_uint32), ("pid", c_uint32)]


class Udp6Row(ctypes.Structure):  # MIB_UDP6ROW_OWNER_PID
    _fields_ = [("laddr", c_ubyte * 16), ("lscope", c_uint32), ("lport", c_uint32), ("pid", c_uint32)]


class Guid(ctypes.Structure):
    _fields_ = [("d1", c_uint32), ("d2", c_uint16), ("d3", c_uint16), ("d4", c_ubyte * 8)]


class IfRow2(ctypes.Structure):  # MIB_IF_ROW2, 1352 bytes
    _fields_ = [("luid", c_uint64), ("index", c_uint32), ("guid", Guid), ("alias", c_uint16 * 257), ("descr", c_uint16 * 257),
                ("phys_len", c_uint32), ("phys", c_ubyte * 32), ("perm_phys", c_ubyte * 32), ("mtu", c_uint32), ("type", c_uint32),
                ("tunnel_type", c_uint32), ("media_type", c_uint32), ("phys_medium", c_uint32), ("access_type", c_uint32),
                ("direction", c_uint32), ("flags", c_ubyte), ("oper_status", c_uint32), ("admin_status", c_uint32),
                ("media_connect", c_uint32), ("network_guid", Guid), ("conn_type", c_uint32), ("tx_speed", c_uint64),
                ("rx_speed", c_uint64), ("in_octets", c_uint64), ("in_ucast", c_uint64), ("in_nucast", c_uint64),
                ("in_discards", c_uint64), ("in_errors", c_uint64), ("in_unknown", c_uint64), ("in_ucast_octets", c_uint64),
                ("in_mcast_octets", c_uint64), ("in_bcast_octets", c_uint64), ("out_octets", c_uint64), ("out_ucast", c_uint64),
                ("out_nucast", c_uint64), ("out_discards", c_uint64), ("out_errors", c_uint64), ("out_ucast_octets", c_uint64),
                ("out_mcast_octets", c_uint64), ("out_bcast_octets", c_uint64), ("out_qlen", c_uint64)]


IF_FILTER_INTERFACE = 0x02  # InterfaceAndOperStatusFlags.FilterInterface: the many "WFP/QoS ... Filter-0000" duplicates


class CpuPerf(ctypes.Structure):  # SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION, times in 100 ns
    _fields_ = [("idle", c_int64), ("kernel", c_int64), ("user", c_int64), ("dpc", c_int64), ("interrupt", c_int64),
                ("interrupts", c_uint32)]


class MemStatus(ctypes.Structure):  # MEMORYSTATUSEX
    _fields_ = [("length", c_uint32), ("load", c_uint32), ("total_phys", c_uint64), ("avail_phys", c_uint64),
                ("total_page", c_uint64), ("avail_page", c_uint64), ("total_virtual", c_uint64), ("avail_virtual", c_uint64),
                ("avail_ext_virtual", c_uint64)]


class PerfInfo(ctypes.Structure):  # PERFORMANCE_INFORMATION (SIZE_T fields: pointer-sized)
    _fields_ = [("cb", c_uint32), ("commit_total", ctypes.c_size_t), ("commit_limit", ctypes.c_size_t),
                ("commit_peak", ctypes.c_size_t), ("physical_total", ctypes.c_size_t), ("physical_available", ctypes.c_size_t),
                ("system_cache", ctypes.c_size_t), ("kernel_total", ctypes.c_size_t), ("kernel_paged", ctypes.c_size_t),
                ("kernel_nonpaged", ctypes.c_size_t), ("page_size", ctypes.c_size_t), ("handles", c_uint32),
                ("processes", c_uint32), ("threads", c_uint32)]


class WtsSession(ctypes.Structure):  # WTS_SESSION_INFOW
    _fields_ = [("id", c_uint32), ("station", ctypes.c_void_p), ("state", c_int)]


WTS_STATES = {0: "active", 1: "connected", 4: "disconnected"}


def _dll(name):
    return ctypes.WinDLL(name, use_last_error=True)  # only exists on Windows: AttributeError elsewhere, by design


def _fail(what):
    err = ctypes.get_last_error()
    return OSError(err, f"{what} failed (Windows error {err})")


def port_of(dw):
    """Port fields hold the port in network byte order in their low 16 bits."""
    return ((dw & 0xFF) << 8) | ((dw >> 8) & 0xFF)


def ipv4_of(dw):
    return str(ipaddress.IPv4Address(struct.pack("<I", dw)))


def ipv6_of(raw):
    return str(ipaddress.IPv6Address(bytes(raw)))


def utf16(arr):
    """WCHAR[] stored as uint16 -> str, up to the first NUL."""
    raw = bytes(bytearray(struct.pack(f"<{len(arr)}H", *arr)))
    return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]


def parse_tcp_table(buf, af):
    """GetExtendedTcpTable buffer -> [{'addr','port','raddr','rport','state','pid'}] (state: 2 LISTEN, 5 ESTABLISHED...)."""
    n = c_uint32.from_buffer_copy(buf[:4]).value
    row_t = TcpRow if af == AF_INET else Tcp6Row
    ip = ipv4_of if af == AF_INET else ipv6_of
    rows = (row_t * n).from_buffer_copy(buf[4:4 + n * ctypes.sizeof(row_t)])
    return [{"addr": ip(r.laddr), "port": port_of(r.lport), "raddr": ip(r.raddr), "rport": port_of(r.rport),
             "state": r.state, "pid": r.pid} for r in rows]


def parse_udp_table(buf, af):
    n = c_uint32.from_buffer_copy(buf[:4]).value
    row_t = UdpRow if af == AF_INET else Udp6Row
    ip = ipv4_of if af == AF_INET else ipv6_of
    rows = (row_t * n).from_buffer_copy(buf[4:4 + n * ctypes.sizeof(row_t)])
    return [{"addr": ip(r.laddr), "port": port_of(r.lport), "pid": r.pid} for r in rows]


def _table(fn, af, cls):
    size = c_uint32(0)
    for _ in range(5):  # the table can grow between the size query and the read
        buf = ctypes.create_string_buffer(max(size.value, 4))
        rc = fn(buf, ctypes.byref(size), False, af, cls, 0)
        if rc == 0:
            return buf.raw[:size.value]
        if rc != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(rc, f"IP helper table read failed (error {rc})")
    raise OSError(ERROR_INSUFFICIENT_BUFFER, "IP helper table kept growing")


def tcp_table():
    """Every TCP socket (IPv4 and IPv6) with its owning PID. Works without admin rights."""
    fn = _dll("iphlpapi").GetExtendedTcpTable
    return [dict(r, v6=af == AF_INET6) for af in (AF_INET, AF_INET6)
            for r in parse_tcp_table(_table(fn, af, TCP_TABLE_OWNER_PID_ALL), af)]


def udp_table():
    fn = _dll("iphlpapi").GetExtendedUdpTable
    return [dict(r, v6=af == AF_INET6) for af in (AF_INET, AF_INET6)
            for r in parse_udp_table(_table(fn, af, UDP_TABLE_OWNER_PID), af)]


def process_image(pid):
    """Full path of the process executable; 'System' for the kernel (PID 4, it owns http.sys/SMB ports); '' if unreadable."""
    if pid == 4:
        return "System"
    if pid == 0:
        return ""
    k = _dll("kernel32")
    k.OpenProcess.restype = c_void_p
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = c_uint32(len(buf))
        return buf.value if k.QueryFullProcessImageNameW(c_void_p(h), 0, buf, ctypes.byref(n)) else ""
    finally:
        k.CloseHandle(c_void_p(h))


def process_token(pid):
    """{'user': owner SID, 'appcontainer': bool} of a process (needs SYSTEM for other users' processes); None if unreadable.

    Store apps run in an AppContainer: Windows Firewall rules created for them carry the user as owner and apply only there."""
    k, a = _dll("kernel32"), _dll("advapi32")
    k.OpenProcess.restype = c_void_p
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    tok = c_void_p()
    try:
        if not a.OpenProcessToken(c_void_p(h), 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            return None
        try:
            buf, n = ctypes.create_string_buffer(256), c_uint32(0)
            if not a.GetTokenInformation(tok, 1, buf, len(buf), ctypes.byref(n)):  # TokenUser: SID_AND_ATTRIBUTES first
                return None
            psid, text = c_void_p.from_buffer(buf).value, ctypes.c_wchar_p()
            if not a.ConvertSidToStringSidW(c_void_p(psid), ctypes.byref(text)):
                return None
            sid = text.value
            k.LocalFree(text)
            flag = c_uint32(0)
            appc = bool(a.GetTokenInformation(tok, 29, ctypes.byref(flag), 4, ctypes.byref(n)) and flag.value)  # TokenIsAppContainer
            return {"user": sid, "appcontainer": appc}
        finally:
            k.CloseHandle(tok)
    finally:
        k.CloseHandle(c_void_p(h))


def cpu_times():
    """{'cpu0': (busy, total), ...} since boot, in 100 ns units. Busy = kernel + user - idle (kernel time includes idle)."""
    n = os.cpu_count() or 1
    buf = (CpuPerf * n)()
    ret = c_uint32(0)
    status = _dll("ntdll").NtQuerySystemInformation(8, buf, ctypes.sizeof(buf), ctypes.byref(ret))  # 8 = processor performance
    if status != 0:
        raise OSError(status, f"NtQuerySystemInformation failed (status {status & 0xFFFFFFFF:#x})")
    return {f"cpu{i}": (r.kernel + r.user - r.idle, r.kernel + r.user)
            for i, r in enumerate(buf[:ret.value // ctypes.sizeof(CpuPerf)])}


def memory():
    """{'total', 'available', 'cache'} in bytes."""
    k = _dll("kernel32")
    ms = MemStatus(length=ctypes.sizeof(MemStatus))
    if not k.GlobalMemoryStatusEx(ctypes.byref(ms)):
        raise _fail("GlobalMemoryStatusEx")
    pi = PerfInfo(cb=ctypes.sizeof(PerfInfo))
    cache = pi.system_cache * pi.page_size if k.K32GetPerformanceInfo(ctypes.byref(pi), pi.cb) else 0
    return {"total": ms.total_phys, "available": ms.avail_phys, "cache": cache}


def uptime():
    k = _dll("kernel32")
    k.GetTickCount64.restype = c_uint64
    return k.GetTickCount64() / 1000.0


def parse_if_table(buf):
    """GetIfTable2 memory -> {alias: (rx_bytes, tx_bytes)}: interfaces up, without loopback and filter-driver duplicates."""
    n = c_uint32.from_buffer_copy(buf[:4]).value
    rows = (IfRow2 * n).from_buffer_copy(buf[8:8 + n * ctypes.sizeof(IfRow2)])  # the rows are 8-byte aligned
    out = {}
    for r in rows:
        if r.oper_status != IF_OPER_UP or r.type == IF_TYPE_LOOPBACK or r.flags & IF_FILTER_INTERFACE:
            continue
        name = utf16(r.alias) or utf16(r.descr)
        if name and name not in out:
            out[name] = (r.in_octets, r.out_octets)
    return out


def interfaces():
    ip = _dll("iphlpapi")
    table = c_void_p()
    rc = ip.GetIfTable2(ctypes.byref(table))
    if rc != 0:
        raise OSError(rc, f"GetIfTable2 failed (error {rc})")
    try:
        n = c_uint32.from_address(table.value).value
        return parse_if_table(ctypes.string_at(table.value, 8 + n * ctypes.sizeof(IfRow2)))
    finally:
        ip.FreeMibTable(table)


def sessions():
    """Windows logon sessions with a user: [{'user', 'station', 'state'}] ('Console', 'RDP-Tcp#0'...)."""
    w = _dll("wtsapi32")
    info, count = c_void_p(), c_uint32(0)
    if not w.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(info), ctypes.byref(count)):
        raise _fail("WTSEnumerateSessionsW")
    out = []
    try:
        rows = (WtsSession * count.value).from_address(info.value)
        for r in rows:
            buf, size = c_void_p(), c_uint32(0)
            user = ""
            if w.WTSQuerySessionInformationW(None, r.id, 5, ctypes.byref(buf), ctypes.byref(size)):  # 5 = WTSUserName
                user = ctypes.wstring_at(buf.value)
                w.WTSFreeMemory(buf)
            if user:
                out.append({"user": user, "station": ctypes.wstring_at(r.station) if r.station else "",
                            "state": WTS_STATES.get(r.state, "other")})
    finally:
        w.WTSFreeMemory(info)
    return out


def drives():
    """[(root, kind)] of fixed, removable, network and RAM drives (CD/DVD drives are skipped: no media = an error)."""
    k = _dll("kernel32")
    buf = ctypes.create_unicode_buffer(1024)
    n = k.GetLogicalDriveStringsW(len(buf), buf)
    if not n:
        raise _fail("GetLogicalDriveStringsW")
    kinds = {DRIVE_REMOVABLE: "removable", DRIVE_FIXED: "fixed", DRIVE_REMOTE: "network", DRIVE_RAMDISK: "ram"}
    roots = [x for x in buf[:n].split("\0") if x]
    return [(r, kinds[t]) for r in roots for t in (k.GetDriveTypeW(r),) if t in kinds]


def screen_size():
    """(width, height) of the primary screen in pixels; None without a desktop."""
    u = _dll("user32")
    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    return (w, h) if w and h else None


def enable_vt():
    """Lets a classic Windows console understand ANSI escape codes (ENABLE_VIRTUAL_TERMINAL_PROCESSING)."""
    k = _dll("kernel32")
    k.GetStdHandle.restype = c_void_p
    h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = c_uint32(0)
    if h and k.GetConsoleMode(c_void_p(h), ctypes.byref(mode)):
        k.SetConsoleMode(c_void_p(h), mode.value | 0x0004)


# ---- processes (renderer, unprivileged: the CPU screen's process list) ---------------------------------------------------

TH32CS_SNAPPROCESS = 0x2
ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER = 5, 87  # OpenProcess: not allowed / no such process (any more)
STILL_ACTIVE = 259
FILETIME_UNIX_EPOCH = 116444736000000000  # 1970-01-01 in 100 ns units since 1601-01-01


class ProcessEntry32W(ctypes.Structure):  # PROCESSENTRY32W: 568 bytes on 64-bit Windows, 556 on 32-bit
    _fields_ = [("size", c_uint32), ("usage", c_uint32), ("pid", c_uint32), ("heap_id", ctypes.c_size_t),
                ("module_id", c_uint32), ("threads", c_uint32), ("ppid", c_uint32), ("pri_base", ctypes.c_int32),
                ("flags", c_uint32), ("exe", c_uint16 * 260)]


class MemCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS (SIZE_T: pointer-sized): 72 bytes on 64-bit, 40 on 32-bit
    _fields_ = [("cb", c_uint32), ("page_faults", c_uint32), ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t), ("quota_peak_paged", ctypes.c_size_t), ("quota_paged", ctypes.c_size_t),
                ("quota_peak_nonpaged", ctypes.c_size_t), ("quota_nonpaged", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]


def filetime_epoch(ft):
    """FILETIME as one integer (100 ns since 1601-01-01 UTC) -> Unix epoch seconds; None for 0 (not set)."""
    return (ft - FILETIME_UNIX_EPOCH) / 1e7 if ft else None


def parse_process_entry(entry):
    """PROCESSENTRY32W -> {'pid', 'ppid', 'threads', 'pri', 'name'}. szExeFile is the image file name ('svchost.exe'),
    never the command line; pcPriClassBase the base priority of its threads (8 normal, 13 high, 4 idle, 24 realtime)."""
    name = bytes(entry.exe).decode("utf-16-le", errors="replace").split("\0", 1)[0]
    return {"pid": entry.pid, "ppid": entry.ppid, "threads": entry.threads, "pri": entry.pri_base, "name": name}


_PROC_API = []


def _proc_api():
    """kernel32 and advapi32 with the prototypes of the process calls (handles are pointer-sized), loaded once: the CPU
    screen lists every process at every refresh."""
    if not _PROC_API:
        k, a = _dll("kernel32"), _dll("advapi32")
        h, p, dw = c_void_p, c_void_p, c_uint32
        k.CreateToolhelp32Snapshot.restype, k.CreateToolhelp32Snapshot.argtypes = h, [dw, dw]
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [h, p]
        k.OpenProcess.restype, k.OpenProcess.argtypes = h, [dw, c_int, dw]
        k.GetProcessTimes.argtypes = [h, p, p, p, p]
        k.GetExitCodeProcess.argtypes = [h, p]
        k.K32GetProcessMemoryInfo.argtypes = [h, p, dw]
        k.CloseHandle.argtypes = [h]
        k.LocalFree.restype, k.LocalFree.argtypes = c_void_p, [c_void_p]
        a.OpenProcessToken.argtypes = [h, dw, p]
        a.GetTokenInformation.argtypes = [h, c_int, p, dw, p]
        a.ConvertSidToStringSidW.argtypes = [c_void_p, p]
        a.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, p]
        a.LookupAccountSidW.argtypes = [ctypes.c_wchar_p, c_void_p, ctypes.c_wchar_p, p, ctypes.c_wchar_p, p, p]
        _PROC_API.extend((k, a))
    return _PROC_API


def process_list():
    """[{'pid', 'ppid', 'threads', 'pri', 'name'}] of every process (CreateToolhelp32Snapshot: no privilege needed)."""
    k, _ = _proc_api()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == c_void_p(-1).value:  # INVALID_HANDLE_VALUE
        raise _fail("CreateToolhelp32Snapshot")
    try:
        e = ProcessEntry32W(size=ctypes.sizeof(ProcessEntry32W))
        out, ok = [], k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append(parse_process_entry(e))
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return out
    finally:
        k.CloseHandle(snap)


def _token_sid(k, a, h):
    tok = c_void_p()
    if not a.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
        return None
    try:
        buf, n = ctypes.create_string_buffer(256), c_uint32(0)
        if not a.GetTokenInformation(tok, 1, buf, len(buf), ctypes.byref(n)):  # TokenUser: SID_AND_ATTRIBUTES first
            return None
        text = ctypes.c_wchar_p()
        if not a.ConvertSidToStringSidW(c_void_p.from_buffer(buf).value, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            k.LocalFree(text)
    finally:
        k.CloseHandle(tok)


def process_stats(pid, user=True):
    """{'created': FILETIME, 'exited': bool, 'cpu': kernel + user time in 100 ns, 'rss': working set bytes, 'sid': owner SID}
    of a process opened with PROCESS_QUERY_LIMITED_INFORMATION (enough for each call here on Windows 8.1+). A value Windows
    refuses is None (the SID too when user=False). Raises OSError when the process cannot be opened: errno 5
    (ERROR_ACCESS_DENIED: protected, or another account's as LOCAL SERVICE), 87 (ERROR_INVALID_PARAMETER: gone)."""
    k, a = _proc_api()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        raise _fail("OpenProcess")
    try:
        out = {"created": None, "exited": False, "cpu": None, "rss": None, "sid": None}
        created, exited, kernel, usr, code = c_uint64(), c_uint64(), c_uint64(), c_uint64(), c_uint32(0)
        if k.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(usr)):
            out["created"], out["cpu"] = created.value or None, kernel.value + usr.value
        if k.GetExitCodeProcess(h, ctypes.byref(code)):  # the exit time is undefined while it runs: ask the exit code
            out["exited"] = code.value != STILL_ACTIVE
        mc = MemCounters(cb=ctypes.sizeof(MemCounters))
        if k.K32GetProcessMemoryInfo(h, ctypes.byref(mc), mc.cb):
            out["rss"] = mc.working_set
        if user:
            out["sid"] = _token_sid(k, a, h)
        return out
    finally:
        k.CloseHandle(h)


def account_name(sid):
    """'alice', 'SYSTEM', 'LOCAL SERVICE'... for a SID string (LookupAccountSidW: the name as Windows shows it, possibly
    translated: displayed, never parsed). None when Windows cannot resolve it (deleted account, unreachable domain)."""
    k, a = _proc_api()
    psid = c_void_p()
    if not a.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        return None
    try:
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, dn, use = c_uint32(len(name)), c_uint32(len(domain)), c_uint32(0)
        if not a.LookupAccountSidW(None, psid, name, ctypes.byref(n), domain, ctypes.byref(dn), ctypes.byref(use)):
            return None
        return name.value or None
    finally:
        k.LocalFree(psid)

# ---- CPU screen (cpuinfo.py): raw buffers, parsed there ----------------------------------------------------------------

STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
RELATION_ALL = 0xFFFF
PDH_MORE_DATA = 0x800007D2
PDH_FMT_DOUBLE, PDH_FMT_NOCAP100 = 0x00000200, 0x00008000


class PowerInfo(ctypes.Structure):  # PROCESSOR_POWER_INFORMATION, 24 bytes
    _fields_ = [("number", c_uint32), ("max_mhz", c_uint32), ("current_mhz", c_uint32), ("mhz_limit", c_uint32),
                ("max_idle", c_uint32), ("current_idle", c_uint32)]


class PdhValue(ctypes.Structure):  # PDH_FMT_COUNTERVALUE with a double
    _fields_ = [("status", c_uint32), ("value", ctypes.c_double)]


class PdhItem(ctypes.Structure):  # PDH_FMT_COUNTERVALUE_ITEM_W
    _fields_ = [("name", c_void_p), ("fmt", PdhValue)]


def _nt_fail(what, status):
    return OSError(status, f"{what} failed (status {status & 0xFFFFFFFF:#x})")


def processor_groups():
    """[(group, active logical CPUs)]: one group up to 64 CPUs, bigger machines have several."""
    k = _dll("kernel32")
    n = k.GetActiveProcessorGroupCount() & 0xFFFF
    return [(g, k.GetActiveProcessorCount(g)) for g in range(n)] or [(0, os.cpu_count() or 1)]


def cpu_perf():
    """([(first logical id, raw SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION array)], complete).

    One NtQuerySystemInformation call with room for every CPU. Where it answers for the caller's processor group only
    (machines with more than 64 CPUs), each group is read with NtQuerySystemInformationEx; if that fails too, the first
    group is returned and `complete` is False."""
    nt = _dll("ntdll")
    try:
        groups = processor_groups()
    except (OSError, AttributeError):
        groups = [(0, os.cpu_count() or 1)]
    total = sum(n for _, n in groups)
    buf, ret = (CpuPerf * total)(), c_uint32(0)
    st = nt.NtQuerySystemInformation(8, buf, ctypes.sizeof(buf), ctypes.byref(ret))
    if st != 0:
        raise _nt_fail("NtQuerySystemInformation", st)
    got = ret.value // ctypes.sizeof(CpuPerf)
    if got >= total or len(groups) < 2:
        return [(0, bytes(buf)[:ret.value])], got >= total
    try:
        out, first = [], 0
        for g, n in groups:
            gbuf, gret, grp = (CpuPerf * n)(), c_uint32(0), c_uint16(g)
            st = nt.NtQuerySystemInformationEx(8, ctypes.byref(grp), 2, gbuf, ctypes.sizeof(gbuf), ctypes.byref(gret))
            if st != 0 or gret.value // ctypes.sizeof(CpuPerf) < n:
                raise _nt_fail("NtQuerySystemInformationEx", st)
            out.append((first, bytes(gbuf)[:gret.value]))
            first += n
        return out, True
    except (OSError, AttributeError):
        return [(0, bytes(buf)[:ret.value])], False


def logical_processor_info():
    """Raw GetLogicalProcessorInformationEx(RelationAll) buffer: cores (with their EfficiencyClass), caches, packages, groups."""
    k = _dll("kernel32")
    size = c_uint32(0)
    for _ in range(4):  # first call: the size; the buffer can grow in between (a CPU added)
        buf = ctypes.create_string_buffer(max(size.value, 1))
        if k.GetLogicalProcessorInformationEx(RELATION_ALL, buf, ctypes.byref(size)):
            return buf.raw[:size.value]
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            raise _fail("GetLogicalProcessorInformationEx")
    raise OSError(ERROR_INSUFFICIENT_BUFFER, "GetLogicalProcessorInformationEx kept growing")


def power_info(n=None):
    """Raw CallNtPowerInformation(ProcessorInformation) buffer: one PROCESSOR_POWER_INFORMATION per logical CPU."""
    p = _dll("powrprof")
    buf = (PowerInfo * (n or os.cpu_count() or 1))()
    st = p.CallNtPowerInformation(11, None, 0, buf, ctypes.sizeof(buf))
    if st != 0:
        raise _nt_fail("CallNtPowerInformation", st)
    return bytes(buf)


def system_performance_info():
    """Raw NtQuerySystemInformation(SystemPerformanceInformation) buffer. Its first 312 bytes have kept their layout since
    NT 4 (later versions append fields): ContextSwitches is the ULONG at offset 296."""
    nt = _dll("ntdll")
    size = 1024
    for _ in range(4):
        buf, ret = ctypes.create_string_buffer(size), c_uint32(0)
        st = nt.NtQuerySystemInformation(2, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if st == 0:
            return buf.raw[:ret.value or size]
        if st != STATUS_INFO_LENGTH_MISMATCH:
            raise _nt_fail("NtQuerySystemInformation", st)
        size = max(ret.value, size * 2)
    raise OSError(STATUS_INFO_LENGTH_MISMATCH, "SystemPerformanceInformation kept growing")


def processor_registry():
    """{'name', 'vendor', 'mhz'} of CPU 0 from HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0 (None if absent)."""
    import winreg  # Windows only
    out = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        for value, field in (("ProcessorNameString", "name"), ("VendorIdentifier", "vendor"), ("~MHz", "mhz")):
            try:
                out[field] = winreg.QueryValueEx(key, value)[0]
            except OSError:
                out[field] = None
    return out


class PdhCounter(object):
    """One PDH counter with all its instances, by its English path (PdhAddEnglishCounterW: the same on every Windows
    language). Rate counters need two collections: values() measures since the previous call (or the creation)."""

    def __init__(self, path):
        self.pdh = _dll("pdh")
        self.query, self.counter = c_void_p(), c_void_p()
        st = self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query))
        if st != 0:
            raise _nt_fail("PdhOpenQuery", st)
        st = self.pdh.PdhAddEnglishCounterW(self.query, ctypes.c_wchar_p(path), None, ctypes.byref(self.counter))
        if st != 0:
            self.close()
            raise _nt_fail("PdhAddEnglishCounter", st)
        self.pdh.PdhCollectQueryData(self.query)

    def values(self):
        """{instance: value} of the instances with valid data."""
        st = self.pdh.PdhCollectQueryData(self.query)
        if st != 0:
            raise _nt_fail("PdhCollectQueryData", st)
        fmt, size, count = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, c_uint32(0), c_uint32(0)
        st = self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if st & 0xFFFFFFFF != PDH_MORE_DATA:
            raise _nt_fail("PdhGetFormattedCounterArray", st)
        buf = ctypes.create_string_buffer(size.value)
        st = self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if st != 0:
            raise _nt_fail("PdhGetFormattedCounterArray", st)
        items = (PdhItem * count.value).from_buffer(buf)  # the names point into the same buffer, after the items
        return {ctypes.wstring_at(it.name): it.fmt.value for it in items if it.name and it.fmt.status in (0, 1)}

    def close(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = c_void_p()
