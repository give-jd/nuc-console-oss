"""nuc-console AI advisor: an optional local model turns the HEALTH findings into advice and answers questions.

It talks to an OpenAI-compatible server on this machine ([ai] endpoint), sends the findings (never raw logs), and may call
only predefined read-only queries on the history. It suggests; it never acts. Off unless [ai] enabled = yes.
"""


def available(cfg):
    """(True, "") when the advisor may be used, else (False, why)."""
    ai = (cfg or {}).get("ai") or {}
    if not ai.get("enabled"):
        return False, "[ai] enabled = no in config.ini"
    return False, "not implemented yet"
