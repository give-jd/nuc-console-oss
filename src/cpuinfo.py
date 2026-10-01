"""nuc-console CPU screen: what the processor is and what each core does (renderer side, unprivileged, every OS).

CpuSampler.sample() returns the dict described in docs/DESIGN.md ("## CPU"): what cannot be read is None, never a guess.
"""
import os
import platform


class CpuSampler(object):
    def __init__(self):
        self.prev = None

    def sample(self):
        return {"model": None, "vendor": None, "arch": platform.machine(), "sockets": None, "cores": None,
                "threads": os.cpu_count() or 1, "kinds": {}, "cache": {},
                "freq": {"cur": {}, "min": None, "max": None, "base": None, "governor": None, "driver": None},
                "usage": {"total": None, "cores": []}, "load": None,
                "rates": {"ctxt": None, "intr": None, "running": None, "blocked": None}, "uptime": None,
                "temps": {"package": None, "cores": {}, "sensors": [], "high": None, "crit": None, "source": None},
                "throttle": {"package": None, "cores": {}, "package_s": None}, "notes": ["not implemented yet"]}
