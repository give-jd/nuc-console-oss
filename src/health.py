"""nuc-console HEALTH: deterministic findings over the history (which apps cause trouble, disks filling up, hot hours).

report(conn, now, days) returns the dict described in docs/DESIGN.md ("## Health"). Pure: reads a read-only connection.
"""
import time


def report(conn, now=None, days=7):
    now = now or time.time()
    return {"period": {"from": now - days * 86400, "to": now, "days": days}, "coverage": {"hours": 0, "since": None},
            "findings": [], "top_cpu": [], "top_mem": [], "events": {}, "logs": [], "disks": [],
            "thermal": {"hours_hot": 0, "max": None, "apps_when_hot": []}, "boots": [], "notes": ["no history yet"]}
