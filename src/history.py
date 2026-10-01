"""nuc-console history: what happened on this machine over days and weeks, in one small SQLite file (stdlib sqlite3).

Written only by the collector; everyone else opens it read-only. Names and counts only: an app is a process name (never its
arguments), a log line is kept as a template with its variable parts removed (so no secret is stored). Schema and
retention: docs/DESIGN.md ("## Health").
"""
import os
import sqlite3

import nuc_config

PATH = os.path.join(nuc_config.LIB_DIR, "history.db")


def open_ro(path=PATH):
    """A read-only connection, or None when there is no history yet."""
    if not os.path.exists(path):
        return None
    return sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
