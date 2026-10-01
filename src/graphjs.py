"""nuc-console graph view: the one script of the web view, on the graph page of the MAP only.

The page works without it (every node is a link); with it, nodes can be dragged, the view zoomed and panned. It reads the
SVG the server drew, never builds HTML from data, opens no connection and loads nothing: web.py sends it inline with its
SHA-256 in the Content-Security-Policy, so no other script can run.
"""
SCRIPT = ""
