#!/bin/sh
# nuc-console, portable: run it from the extracted folder. No installation, no service, no system change: everything it
# writes (config, state, baseline, logs) is in ./data, and nothing listens beyond 127.0.0.1.
#
#   ./run.sh                       Linux: the dashboard in this terminal (--console); macOS: in your browser (--web)
#   ./run.sh --console             in this terminal (q or Ctrl+C quits)
#   ./run.sh --web [--port N]      on http://127.0.0.1:N (a free port if N is left out) and opens your browser; Ctrl+C quits
#   ./run.sh --no-open             with --web: print the address, do not open a browser
#   ./run.sh --problems [--json]   what needs attention now, why it matters and how to fix it (while it runs in another terminal)
#   ./run.sh --accept              accept the ports exposed right now as the alarm baseline (while it runs in another terminal);
#                                  also ./run.sh --accept --problem ID --reason "why" (a known ATTENTION item) | --forget ID
#
# As a normal user it still runs: what needs root (firewall, other users' processes, containers) shows as missing on the screen.
# `sudo ./run.sh` collects everything, and then every part runs as root: keep this folder yours alone.
# It stops everything it started when you quit. Update: bin/nuc-console-update (keeps ./data).
set -eu

die() { echo "nuc-console: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: ./run.sh [--console | --web] [--port N] [--no-open] | --problems [--json] | --accept [--problem ID --reason TEXT | --forget ID]
  --console   the dashboard in this terminal (default on Linux); q or Ctrl+C quits
  --web       the dashboard in your browser (default on macOS), on 127.0.0.1 only; Ctrl+C quits
  --port N    with --web: the port (default: a free one)
  --no-open   with --web: only print the address
  --problems  list what needs attention now: why it matters and how to fix it (--json: for scripts)
  --accept    accept the ports exposed now as the baseline of the port alarms (or a known problem: --problem ID --reason TEXT)
Everything it writes is in ./data. sudo ./run.sh shows more.
EOF
}

VIEW="" PORT=0 OPEN=1 ACCEPT=0 PROBLEMS=0 WHICH=0
while [ $# -gt 0 ]; do
    case $1 in
        --console) VIEW=console ;;
        --web) VIEW=web ;;
        --port) [ $# -ge 2 ] || die "--port needs a number"; PORT=$2; shift ;;
        --port=*) PORT=${1#--port=} ;;
        --no-open) OPEN=0 ;;
        --accept) ACCEPT=1; shift; break ;;  # what follows (--problem ID --reason ... | --forget ID) is for render.py
        --problems) PROBLEMS=1; shift; break ;;  # and --json
        --which-python) WHICH=1 ;;  # for bin/nuc-console-update: the Python this folder runs with
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done
case $PORT in ''|*[!0-9]*) die "invalid port: $PORT" ;; esac
[ "$PORT" -le 65535 ] || die "invalid port: $PORT"
OS=$(uname -s)
if [ -z "$VIEW" ]; then if [ "$OS" = Darwin ]; then VIEW=web; else VIEW=console; fi; fi
if [ "$VIEW" = console ] && [ "$ACCEPT" = 0 ] && [ "$PROBLEMS" = 0 ] && [ "$WHICH" = 0 ]; then
    [ -t 0 ] && [ -t 1 ] || die "--console needs a terminal: use --web"
fi

# ---- Python 3.8+ ------------------------------------------------------------------------------------------------------
py_ok() { [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; }
find_python() {
    if [ -n "${PYTHON:-}" ]; then py_ok "$PYTHON" && { echo "$PYTHON"; return 0; }; return 1; fi
    cands=""
    if [ "$OS" = Darwin ]; then  # python.org's (the newest), then Apple's with the Command Line Tools (else it opens an install dialog)
        best=0 found=""
        for c in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
            py_ok "$c" || continue
            v=$("$c" -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])')
            if [ "$v" -gt "$best" ]; then best=$v found=$c; fi
        done
        [ -z "$found" ] || { echo "$found"; return 0; }
        if xcode-select -p >/dev/null 2>&1; then cands=/usr/bin/python3; fi
    else
        cands=/usr/bin/python3
    fi
    for c in $cands "$(command -v python3 2>/dev/null || true)"; do
        [ -n "$c" ] && py_ok "$c" && { echo "$c"; return 0; }
    done
    return 1
}
PY=$(find_python) || die "Python 3.8 or newer not found (Linux: install python3; macOS: python.org/downloads or xcode-select --install)"

if [ "$WHICH" = 1 ]; then echo "$PY"; exit 0; fi

# ---- the data folder --------------------------------------------------------------------------------------------------
HERE=$(cd "$(dirname "$0")" && pwd -P)
DATA=$HERE/data
LOGS=$DATA/logs
[ -f "$HERE/src/collector.py" ] || die "src/collector.py not found next to run.sh: run it from the extracted folder"
unsafe() {  # as root: a link, or a folder others can write to, could be turned against root
    [ -L "$1" ] && return 0
    case $(ls -ld "$1") in ?????w????*|????????w?*) return 0 ;; esac
    return 1
}
if [ "$(id -u)" -eq 0 ]; then
    for d in "$HERE/src" "$DATA" "$DATA/run" "$DATA/lib" "$LOGS"; do
        if [ -e "$d" ] || [ -L "$d" ]; then
            ! unsafe "$d" || die "$d is a link or writable by others: as root nothing is written there. Fix it (chmod go-w) or move this folder"
        fi
    done
fi
umask 077
mkdir -p "$DATA/run" "$DATA/lib" "$LOGS"
[ -w "$DATA" ] || die "$DATA is not writable by you (an earlier sudo run made it root's? sudo chown -R \"\$USER\" \"$DATA\")"
[ -e "$DATA/config.ini" ] || cp "$HERE/config/config.ini" "$DATA/config.ini"  # never overwrite your edits
cp "$HERE/config/config.ini" "$DATA/config.ini.dist"  # always refreshed: diff it with config.ini to see new options
NUC_CONSOLE_HOME=$DATA
export NUC_CONSOLE_HOME
unset NUC_CONSOLE_CONFIG NUC_CONSOLE_STATE NUC_CONSOLE_NET NUC_CONSOLE_BOOT NUC_CONSOLE_BASELINE NUC_CONSOLE_ACCEPTED

if [ "$ACCEPT" = 1 ]; then
    exec "$PY" -B "$HERE/src/render.py" --accept "$@"
fi
if [ "$PROBLEMS" = 1 ]; then
    exec "$PY" -B "$HERE/src/render.py" --problems "$@"
fi

# one instance per folder: two collectors would write the same files
if [ -f "$DATA/portable.pid" ]; then
    old=$(cat "$DATA/portable.pid" 2>/dev/null || true)
    case $old in
        ''|*[!0-9]*) ;;
        *) if kill -0 "$old" 2>/dev/null; then die "already running (pid $old): quit it first (if that is wrong, delete $DATA/portable.pid)"; fi ;;
    esac
fi
echo $$ > "$DATA/portable.pid"

# ---- start, and stop everything we started ------------------------------------------------------------------------------
PIDS=""
cleanup() {
    trap - EXIT INT TERM HUP
    for p in $PIDS; do kill "$p" 2>/dev/null || true; done
    for p in $PIDS; do wait "$p" 2>/dev/null || true; done
    rm -f "$DATA/portable.pid"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

rotate() {  # a log over 1 MB is kept once as .1
    [ -f "$1" ] || return 0
    [ $(( $(wc -c < "$1") + 0 )) -le 1048576 ] || mv -f "$1" "$1.1"
}
rotate "$LOGS/collector.log"
rotate "$LOGS/web.log"
if [ "$(id -u)" -ne 0 ]; then
    echo "nuc-console: running without root: the sections that need it (firewall, containers, other users' processes) show less. sudo ./run.sh shows everything." >&2
fi
"$PY" -B "$HERE/src/collector.py" >>"$LOGS/collector.log" 2>&1 &
PIDS="$PIDS $!"

# the baseline of the port alarms: created once the collector has written its first complete snapshot (a normal user's one is
# incomplete: it is tried for a few minutes, then ./run.sh --accept or sudo ./run.sh)
if [ ! -e "$DATA/lib/baseline.json" ]; then
    (  # every command runs in the background and is waited for: a trap would wait for a foreground one to end
        trap - EXIT
        job="" n=0
        trap '[ -z "$job" ] || kill "$job" 2>/dev/null; exit 0' TERM
        while [ "$n" -lt 30 ] && kill -0 "$$" 2>/dev/null; do
            sleep 4 &
            job=$!
            wait "$job" || true
            "$PY" -B "$HERE/src/render.py" --accept --if-missing >"$LOGS/baseline.log" 2>&1 &
            job=$!
            if wait "$job"; then exit 0; fi
            n=$((n + 1))
        done
    ) &
    PIDS="$PIDS $!"
fi

rc=0
if [ "$VIEW" = console ]; then
    # In the background, and this shell waits for it: a trap runs only once a foreground command has ended, so a kill (or
    # timeout) that reaches this shell alone would otherwise leave the dashboard on the screen. Ctrl+C reaches the shell too
    # (an asynchronous command ignores it): the trap stops the dashboard, which restores the terminal on SIGTERM. The
    # keyboard is passed on explicitly: an asynchronous command gets /dev/null as its input otherwise.
    exec 3<&0
    "$PY" -B "$HERE/src/render.py" <&3 3<&- &
    RENDER=$!
    PIDS="$PIDS $RENDER"
    wait "$RENDER" || rc=$?
    exit "$rc"
fi

: > "$LOGS/web.log"
"$PY" -B "$HERE/src/web.py" --local --port "$PORT" >>"$LOGS/web.log" 2>&1 &
WEB=$!
PIDS="$PIDS $WEB"
URL="" i=0
while [ -z "$URL" ]; do
    URL=$(sed -n 's|^nuc-console web view on \(http://[^ ]*\).*|\1|p' "$LOGS/web.log")
    [ -z "$URL" ] || break
    if ! kill -0 "$WEB" 2>/dev/null || [ "$i" -ge 150 ]; then
        cat "$LOGS/web.log" >&2
        die "the web view did not start"
    fi
    i=$((i + 1))
    sleep 0.2
done
URL="$URL/?fit=1"
echo "nuc-console: dashboard on $URL (Ctrl+C to stop)"
if [ "$OPEN" = 1 ]; then
    if [ "$OS" = Darwin ]; then
        /usr/bin/open "$URL" || echo "nuc-console: could not open the browser: open $URL yourself" >&2
    elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 &
    fi
fi
wait "$WEB" || rc=$?
echo "nuc-console: the web view stopped (see $LOGS/web.log)" >&2
exit "$rc"
