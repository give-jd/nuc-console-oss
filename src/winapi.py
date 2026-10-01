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
