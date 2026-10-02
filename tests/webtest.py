"""Helper of the tests that look at the classic web pages. The shell is the default web interface (`[ui] web = app`); these tests ask for the
classic pages, which `[ui] web = classic` still serves for one release, so they send their requests inside `classic_default()`."""
import contextlib
import threading

import render

_LOCK = threading.Lock()
_STATE = {"users": 0, "saved": None}


@contextlib.contextmanager
def classic_default():
    """`[ui] web = classic` for the requests made inside (the server reads it per request, in its own thread; the caller waits for the answer).
    Requests may overlap (a test asks from several threads): the first one in sets it, the last one out puts back what was there."""
    ui = render.CFG["ui"]
    with _LOCK:
        if _STATE["users"] == 0:
            _STATE["saved"] = ui.get("web")
            ui["web"] = "classic"
        _STATE["users"] += 1
    try:
        yield
    finally:
        with _LOCK:
            _STATE["users"] -= 1
            if _STATE["users"] == 0:
                if _STATE["saved"] is None:
                    ui.pop("web", None)
                else:
                    ui["web"] = _STATE["saved"]
