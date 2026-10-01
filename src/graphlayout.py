"""nuc-console graph view: where each node of the MAP sits on the page. Pure, no I/O, no randomness.

layout() is deterministic: the same nodes and edges give the same positions whatever their order, and a node keeps roughly
its place when others come and go (its starting point is a hash of its id), so a page that refreshes does not jump.
"""
import hashlib
import math


def layout(nodes, edges, width=1000.0, height=700.0, margin=40.0):
    """nodes: ids; edges: (id, id) pairs (direction ignored, unknown ids skipped) -> {id: (x, y)} inside the margins."""
    ids = sorted(set(nodes))
    out = {}
    for i, nid in enumerate(ids):  # placeholder: on a circle, in hash order
        a = int(hashlib.sha1(nid.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF * 2 * math.pi
        out[nid] = (width / 2 + (width / 2 - margin) * math.cos(a), height / 2 + (height / 2 - margin) * math.sin(a))
    return out
