"""nuc-console CPU screen: the processes, like htop (renderer side, unprivileged, every OS). Names only: never the
command line, whose arguments may carry passwords or tokens.

ProcSampler.sample() returns the dict described in docs/DESIGN.md ("## CPU"): what cannot be read is None.
"""


class ProcSampler(object):
    def __init__(self):
        self.prev = {}

    def sample(self):
        return {"procs": [], "total": {"count": 0, "running": None, "threads": None, "unreadable": 0}, "notes": ["not implemented yet"]}
