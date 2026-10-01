#!/bin/bash
# Install nuc-console on macOS 11 or newer. Idempotent: run it again to upgrade. Needs root: sudo ./install.sh
# (install.sh hands over to this script on macOS). Rollback: sudo ./install.sh --uninstall
#
# What it does:
#   1. finds a Python 3.8+ owned by the system (python.org framework, or Apple's Command Line Tools); if there is none it
#      installs the official python.org package (SHA-256 pinned, signature checked), framework only: no PATH or shell
#      profile changes. Homebrew's Python is never used: its files belong to a user, and the collector runs as root;
#   2. copies the code to /opt/nuc-console, config to /etc/nuc-console (config.ini only if missing), state to /var/run,
#      the baseline to /var/lib/nuc-console, logs to /var/log/nuc-console (rotated by newsyslog);
#   3. starts the collector as a LaunchDaemon (root) and, if [web] enabled = yes, the web view as user _nuc-console;
#   4. installs a LaunchAgent that opens the dashboard full screen at every desktop login (a Chrome/Edge/Brave window if
#      installed, else Safari: press Ctrl+Cmd+F once), and opens it now for the user at the console.
# Options (environment): NUC_CONSOLE_DISPLAY=no   no dashboard at login (a Mac without a monitor)
set -euo pipefail
[ "$(uname -s)" = Darwin ] || { echo "this installer is for macOS; on Linux use install.sh" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "root required: sudo $0" >&2; exit 1; }
cd "$(dirname "$0")"

DEST=/opt/nuc-console
ETC=/etc/nuc-console
LIB=/var/lib/nuc-console
LOG=/var/log/nuc-console
LD=/Library/LaunchDaemons
LA=/Library/LaunchAgents
SVC_USER=_nuc-console
PY_VERSION=3.14.8
PY_PKG_SHA256=507fc086c5c006ff875d344a75b4e67b8fb3c401f1bc4908c6250adb673d4907  # verified against python.org's Sigstore signature
PY_FRAMEWORK=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
CONSOLE_UID="$(stat -f %u /dev/console)"  # the user at the screen (0 at the login window)

if [ "${1:-}" = "--uninstall" ]; then
    for label in com.nuc-console.collector com.nuc-console.web; do launchctl bootout "system/$label" 2>/dev/null || true; done
    [ "$CONSOLE_UID" = 0 ] || launchctl bootout "gui/$CONSOLE_UID/com.nuc-console.display" 2>/dev/null || true
    rm -f "$LD"/com.nuc-console.*.plist "$LA/com.nuc-console.display.plist" /etc/newsyslog.d/nuc-console.conf
    for link in /usr/local/bin/nuc-console-problems /usr/local/sbin/nuc-console-accept; do
        if [ -L "$link" ]; then rm -f "$link"; fi
    done
    rm -rf "$DEST"
    echo "removed ($ETC, $LIB, $LOG and the user $SVC_USER are left in place)"
    exit 0
fi

# ---- 1. Python --------------------------------------------------------------------------------------------------------
py_ok() {  # Python 3.8+, owned by root (the collector runs it as root)
    [ -x "$1" ] && [ "$(stat -f %u "$(python_real "$1")")" = 0 ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null
}
python_real() { "$1" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null || echo "$1"; }
PY="" best=0
for cand in /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do  # python.org installs: the newest one
    if py_ok "$cand"; then
        v="$("$cand" -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])')"
        if [ "$v" -gt "$best" ]; then PY="$cand" best="$v"; fi
    fi
done
# Apple's python3 only with the Command Line Tools present: without them /usr/bin/python3 opens an install dialog
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1 && py_ok /usr/bin/python3; then PY=/usr/bin/python3; fi
if [ -z "$PY" ]; then
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "nuc-console: downloading Python $PY_VERSION from python.org"
    curl -fsSL -o "$tmp/python.pkg" "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-macos11.pkg"
    echo "$PY_PKG_SHA256  $tmp/python.pkg" | shasum -a 256 -c - >/dev/null || { echo "python.pkg: wrong SHA-256, not installed" >&2; exit 1; }
    pkgutil --check-signature "$tmp/python.pkg" | grep -q "Developer ID Installer: Python Software Foundation" \
        || { echo "python.pkg is not signed by the Python Software Foundation: not installed" >&2; exit 1; }
    # framework only: no IDLE/apps, no /usr/local/bin links, no shell profile changes, no pip
    {
        echo '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><array>'
        for c in PythonApplications PythonUnixTools PythonDocumentation PythonProfileChanges PythonInstallPip; do
            echo "<dict><key>attributeSetting</key><integer>0</integer><key>choiceAttribute</key><string>selected</string><key>choiceIdentifier</key><string>org.python.Python.$c-3.14</string></dict>"
        done
        echo '</array></plist>'
    } > "$tmp/choices.xml"
    installer -pkg "$tmp/python.pkg" -target / -applyChoiceChangesXML "$tmp/choices.xml" >/dev/null
    py_ok "$PY_FRAMEWORK" || { echo "Python installation failed" >&2; exit 1; }
    PY="$PY_FRAMEWORK"
fi
echo "nuc-console: using $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# ---- 2. files ---------------------------------------------------------------------------------------------------------
fill() { sed -e "s|@PYTHON@|$PY|g" -e "s|@DEST@|$DEST|g" "$1"; }
load() {  # right after a bootout launchd may still be unloading the job: retry for a few seconds
    for _ in 1 2 3 4 5; do launchctl bootstrap "$1" "$2" 2>/dev/null && return 0; sleep 1; done
    launchctl bootstrap "$1" "$2"
}
for label in com.nuc-console.collector com.nuc-console.web; do launchctl bootout "system/$label" 2>/dev/null || true; done
install -d -m 0755 "$DEST" "$DEST/bin" "$ETC" "$LIB" "$LOG"
rm -f "$DEST"/*.py
install -m 0644 src/*.py "$DEST/"
for f in nuc-console-accept nuc-console-problems; do
    sed -e "s|/usr/bin/python3|$PY|g" -e "s|/opt/nuc-console/|$DEST/|g" "bin/$f" > "$DEST/bin/$f"
    chmod 0755 "$DEST/bin/$f"
done
# commands on the PATH, but only into folders root owns (on Intel Macs Homebrew makes /usr/local/bin a user's folder:
# a link there could be swapped for anything, then run with sudo)
for pair in "bin:nuc-console-problems" "sbin:nuc-console-accept"; do
    dir="/usr/local/${pair%%:*}" cmd="${pair#*:}"
    [ -d "$dir" ] || { [ "$(stat -f %u /usr/local 2>/dev/null || echo 1)" = 0 ] && install -d -m 0755 "$dir"; } || true
    if [ -d "$dir" ] && [ "$(stat -f %u "$dir")" = 0 ]; then ln -sf "$DEST/bin/$cmd" "$dir/$cmd"
    else echo "nuc-console: $dir is not owned by root: run $DEST/bin/$cmd by its full path"; fi
done
[ -e "$ETC/config.ini" ] || install -m 0644 config/config.ini "$ETC/config.ini"  # never overwrite the admin's edits
install -m 0644 config/config.ini "$ETC/config.ini.dist"  # always refreshed: diff it with config.ini to see new options
echo "$LOG/collector.log  root:wheel  644  5  1024  *  NJ" > /etc/newsyslog.d/nuc-console.conf  # rotate at 1 MB, keep 5

# ---- 3. collector and web view --------------------------------------------------------------------------------------------
fill launchd/com.nuc-console.collector.plist > "$LD/com.nuc-console.collector.plist"
chown root:wheel "$LD/com.nuc-console.collector.plist" && chmod 0644 "$LD/com.nuc-console.collector.plist"
t0=$(date +%s)
load system "$LD/com.nuc-console.collector.plist"
if "$PY" -B "$DEST/web.py" --enabled; then
    if ! dscl . -read "/Users/$SVC_USER" >/dev/null 2>&1; then  # a hidden service account, like Apple's _www
        id=""
        for i in $(seq 400 499); do
            if [ -z "$(dscl . -search /Users UniqueID "$i")" ] && [ -z "$(dscl . -search /Groups PrimaryGroupID "$i")" ]; then id=$i; break; fi
        done
        [ -n "$id" ] || { echo "no free id for $SVC_USER" >&2; exit 1; }
        dscl . -create "/Groups/$SVC_USER" PrimaryGroupID "$id"
        dscl . -create "/Groups/$SVC_USER" RealName "nuc-console web view"
        dscl . -create "/Users/$SVC_USER" UniqueID "$id"
        dscl . -create "/Users/$SVC_USER" PrimaryGroupID "$id"
        dscl . -create "/Users/$SVC_USER" UserShell /usr/bin/false
        dscl . -create "/Users/$SVC_USER" NFSHomeDirectory /var/empty
        dscl . -create "/Users/$SVC_USER" RealName "nuc-console web view"
        dscl . -create "/Users/$SVC_USER" IsHidden 1
        dscl . -create "/Users/$SVC_USER" Password '*'
    fi
    touch "$LOG/web.log" && chown "$SVC_USER:$SVC_USER" "$LOG/web.log"
    echo "$LOG/web.log  $SVC_USER:$SVC_USER  644  5  1024  *  NJ" >> /etc/newsyslog.d/nuc-console.conf
    fill launchd/com.nuc-console.web.plist > "$LD/com.nuc-console.web.plist"
    chown root:wheel "$LD/com.nuc-console.web.plist" && chmod 0644 "$LD/com.nuc-console.web.plist"
    load system "$LD/com.nuc-console.web.plist"
else
    rm -f "$LD/com.nuc-console.web.plist"
fi

# ---- 4. first snapshot, baseline, dashboard ---------------------------------------------------------------------------
# wait for a net.json written AFTER the collector start: an older one would be stale
for _ in $(seq 1 90); do [ "$(stat -f %m /var/run/nuc-console/net.json 2>/dev/null || echo 0)" -ge "$t0" ] && break; sleep 1; done
"$PY" -B "$DEST/render.py" --accept --if-missing || echo "warning: baseline not created (collector not ready yet): run sudo nuc-console-accept"
if [ "${NUC_CONSOLE_DISPLAY:-yes}" = no ]; then
    [ "$CONSOLE_UID" = 0 ] || launchctl bootout "gui/$CONSOLE_UID/com.nuc-console.display" 2>/dev/null || true
    rm -f "$LA/com.nuc-console.display.plist"
    where="not opened (NUC_CONSOLE_DISPLAY=no)"
else
    fill launchd/com.nuc-console.display.plist > "$LA/com.nuc-console.display.plist"
    chown root:wheel "$LA/com.nuc-console.display.plist" && chmod 0644 "$LA/com.nuc-console.display.plist"
    if [ "$CONSOLE_UID" != 0 ]; then  # open it now for the user at the screen; everyone else gets it at login
        launchctl bootout "gui/$CONSOLE_UID/com.nuc-console.display" 2>/dev/null || true
        load "gui/$CONSOLE_UID" "$LA/com.nuc-console.display.plist" || echo "nuc-console: the dashboard opens at the next login"
    fi
    where="opens full screen at every login (Cmd+Q closes it)"
fi
echo "ok: collector running; dashboard $where. Config: $ETC/config.ini  Logs: $LOG"
