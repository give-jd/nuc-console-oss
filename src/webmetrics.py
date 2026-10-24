"""/metrics of the web view (docs/WEB.md, "Metrics"): the figures /api/v1/overview carries, as Prometheus text exposition (version 0.0.4).
Pure: web.py checks the request (the very checks of /api/v1/*), reads the frame (cards.Ctx) and this turns it into text. Nothing here reads
a file, the clock or the host. A figure that is unknown is left out, never 0. Label values are the closed names of the API (collectors,
severities, states, reaches) or what the cards already list (interfaces, mounts, problem ids); every one is escaped."""

import cards
import exposure
import ui

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
PROBLEM_IDS_MAX = 200  # nuc_console_problem_active series
MOUNTS_MAX = 64
IFACES_MAX = 64
LABEL_MAX = 64  # characters of a label value: a mount or interface name is cut here (the cards cut theirs shorter), so the answer stays small
SEVERITY = {1: "warning", 2: "error", 3: "change"}  # problems.py: a port change (3) is as urgent as an error
REACH = (("local", "LOCALE"), ("tailnet", "TAILNET"), ("lan", "LAN"), ("internet", "INTERNET"))


def esc(v):
    """A label value as the exposition format has it: backslash, double quote and newline escaped (other control characters become a space)."""
    out = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "".join(c if c >= " " and c != "\x7f" else " " for c in out)


def _fmt(x):
    return str(int(x)) if x == int(x) and abs(x) < 1e15 else repr(float(x))


class Out(object):
    """The families in order, each with its HELP and TYPE once; a series (name and labels) is written once."""

    def __init__(self):
        self.lines, self.seen, self.fams = [], set(), set()

    def add(self, name, kind, help_, value, **labels):
        v = ui.num(value)
        if v is None:  # unknown: no series
            return
        labels = {k: str(x)[:LABEL_MAX] for k, x in labels.items()}  # cut first: two names that end alike are one series, the first
        key = (name, tuple(sorted(labels.items())))
        if key in self.seen:
            return
        self.seen.add(key)
        if name not in self.fams:
            self.fams.add(name)
            self.lines += ["# HELP %s %s" % (name, help_), "# TYPE %s %s" % (name, kind)]
        tags = "{" + ",".join('%s="%s"' % (k, esc(x)) for k, x in labels.items()) + "}" if labels else ""
        self.lines.append("%s%s %s" % (name, tags, _fmt(v)))

    def text(self):
        return ("\n".join(self.lines) + "\n").encode("utf-8")


def render(ctx, version):
    """The exposition of one frame (cards.Ctx), as bytes."""
    o = Out()
    p = "nuc_console_"
    s = ctx.s if isinstance(ctx.s, dict) else {}
    now = cards._now(ctx)
    o.add(p + "build_info", "gauge", "nuc-console version (the label).", 1, version=version)
    o.add(p + "up", "gauge", "1 while the web view answers.", 1)
    for name, d in (("network", ctx.net), ("containers", ctx.cont), ("boot", ctx.boot)):
        ts = ui.num(d.get("ts")) if isinstance(d, dict) else None
        if ts is not None:
            o.add(p + "collector_age_seconds", "gauge", "Seconds since a collector last wrote its state.", max(0.0, now - ts), collector=name)
    pb = ctx.problems
    if pb is not None:
        count = {}
        for sev, _pid in ctx.problem_ids():
            count[SEVERITY.get(sev, "other")] = count.get(SEVERITY.get(sev, "other"), 0) + 1
        for name in list(SEVERITY.values()) + (["other"] if "other" in count else []):
            o.add(p + "problems", "gauge", "Problems in the list (ATTENTION), by severity.", count.get(name, 0), severity=name)
        ids = sorted({pid for _sev, pid in ctx.problem_ids() if pid})[:PROBLEM_IDS_MAX]
        for pid in ids:
            o.add(p + "problem_active", "gauge", "1 for each active, unmuted problem id.", 1, id=pid)
        o.add(p + "problems_muted", "gauge", "Problems silenced by [alerts] mute.", getattr(pb, "muted", 0))
        o.add(p + "problems_accepted", "gauge", "Problems accepted as known.", getattr(pb, "accepted", 0))
    cores = cards._cores(ctx)
    if cores:
        o.add(p + "cpu_usage_ratio", "gauge", "CPU use, the mean of the threads, 0 to 1.", sum(cores) / len(cores))
    load = s.get("load")
    if isinstance(load, (list, tuple)) and len(load) == 3:
        for n, v in zip(("1", "5", "15"), load):
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            o.add(p + "load" + n, "gauge", "Load average over %s minute(s)." % n, v)
    ram = cards.ram_figures(s.get("mem"))
    if ram:
        o.add(p + "memory_used_bytes", "gauge", "RAM in use (total less available).", ram[0])
        o.add(p + "memory_total_bytes", "gauge", "RAM installed.", ram[1])
    for f in (s.get("fs") if isinstance(s.get("fs"), list) else [])[:MOUNTS_MAX]:
        if isinstance(f, dict) and f.get("mount") is not None:
            o.add(p + "filesystem_used_bytes", "gauge", "Space used on a filesystem the DISKS card lists.", f.get("used"), mount=ui.safe(f["mount"]))
            o.add(p + "filesystem_total_bytes", "gauge", "Size of a filesystem the DISKS card lists.", f.get("total"), mount=ui.safe(f["mount"]))
    th, temps = s.get("thermal"), []
    for key in ("cpu", "nvme"):
        try:
            t = ui.num(th[key][0])
        except (TypeError, KeyError, IndexError):
            continue
        if t is not None:
            temps.append(t)
    if temps:
        o.add(p + "temperature_celsius", "gauge", "Hottest of the CPU and NVMe sensors.", max(temps))
    o.add(p + "uptime_seconds", "gauge", "Seconds since boot.", s.get("uptime"))
    nets = s.get("net")
    if isinstance(nets, dict):
        def traffic(n):  # the busiest interfaces stay when there are more than IFACES_MAX (veth* and br-* are many on a Docker host)
            v = nets[n] if isinstance(nets[n], dict) else {}
            return -((ui.num(v.get("rx_tot")) or 0) + (ui.num(v.get("tx_tot")) or 0))
        for name in sorted(nets, key=lambda n: (traffic(n), str(n)))[:IFACES_MAX]:
            v = nets[name] if isinstance(nets[name], dict) else {}
            o.add(p + "network_receive_bytes_total", "counter", "Bytes received on an interface since boot.", v.get("rx_tot"), interface=ui.safe(name))
            o.add(p + "network_transmit_bytes_total", "counter", "Bytes sent on an interface since boot.", v.get("tx_tot"), interface=ui.safe(name))
    cont = ctx.cont
    cs = cont.get("containers") if isinstance(cont, dict) and not cont.get("disabled") and not cont.get("absent") else None
    if isinstance(cs, list):
        h = "Containers, by state."
        o.add(p + "containers", "gauge", h, sum(1 for c in cs if c["state"] == "running"), state="running")
        o.add(p + "containers", "gauge", h, sum(1 for c in cs if cards._sick(c)), state="unhealthy")
        o.add(p + "containers", "gauge", h, sum(1 for c in cs if c["state"] == "exited"), state="exited")
    try:
        rows = cards._rows(ctx)
    except Exception:  # noqa: BLE001 - what a producer hands over is data, not a promise
        rows = None
    if rows is not None:
        for label, group in REACH:
            o.add(p + "exposed_ports", "gauge", "Listening ports, by the widest reach they have.", sum(1 for r in rows if exposure.group_of(r) == group), reach=label)
    fw = cards._kpi_firewall(ctx).value
    if fw in ("on", "off"):
        o.add(p + "firewall_active", "gauge", "1 when the OS firewall is on, 0 when it is off (unknown: no series).", 1 if fw == "on" else 0)
    return o.text()
