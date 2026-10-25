"""A fleet cache for tests: the entries the poller (peers.py) would write for peers in given states, put into a folder the web view reads."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import peers  # noqa: E402

HEX = "0123456789abcdef"


def peer(name, **kw):
    """A [hosts] row as nuc_config.parse_hosts gives it."""
    return dict({"name": name, "url": "https://%s.example.net" % name, "token": "/nonexistent/%s.token" % name, "pin": "", "scope": "summary",
                 "alert": False, "insecure": False}, **kw)


def entry(name, now, state="ok", age=12, host=None, counts=(0, 0, 0), worst=(), **kw):
    """The cache entry of `name` as poll_peer writes it: a summary read `age` seconds before `now`. kw overrides top-level fields."""
    e = peers.new_entry(peer(name), now)
    summary = {"host": host or name + "-box", "status_text": "ALL OK" if state == "ok" else "PROBLEMS", "host_id": (HEX + HEX)[len(name):len(name) + 16],
               "version": "2.6.0", "os": "linux", "api": 1, "state": state if state in peers.SUMMARY_STATES else "unknown",
               "problems": counts[0] + counts[1], "counts": {"err": counts[0], "warn": counts[1], "unknown": counts[2]}, "worst": list(worst),
               "at": now - age}
    e.update(state=state, summary=summary, last_ok=int(now - age), skew_s=0, age_s=age)
    e.update(kw)
    return e


def write(folder, entries, now, poller=True):
    """The entries and (poller) fleet.json into `folder`, as the poller writes them."""
    if poller:
        peers.write_cache(folder, entries, now)
        return
    peers.own_folder(folder)
    for e in entries:
        peers.write_json(folder, e["name"] + ".json", e)
