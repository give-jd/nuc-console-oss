#!/bin/bash
# Install nuc-console on macOS 11 or newer. Idempotent: run it again to upgrade. Needs root: sudo ./install.sh
# (install.sh hands over to this script on macOS). Rollback: sudo ./install.sh --uninstall
#
# What it does:
#   1. finds a Python 3.8+ owned by the system (python.org framework, or Apple's Command Line Tools); if there is none it
#      installs the official python.org package (SHA-256 pinned, signature checked), framework only: no PATH or shell
#      profile changes. Homebrew's Python is never used: its files belong to a user, and the collector runs as root.
#      The package is kept in /Library/Caches/nuc-console and never downloaded twice;
#   2. copies the code to /opt/nuc-console, config to /etc/nuc-console (config.ini only if missing), state to /var/run,
#      the baseline to /var/lib/nuc-console, logs to /var/log/nuc-console (rotated by newsyslog);
#   3. starts the collector as a LaunchDaemon (root) and the read-only web view as user _nuc-console, on 127.0.0.1 only
#      (as configured in [web] if enabled there): it shows the dashboard to this Mac's browser; and the optional Telegram
#      notifier (outbound HTTPS to api.telegram.org only, idle until switched on: docs/TELEGRAM.md) as the same user;
#   4. a LaunchAgent opens the dashboard at every desktop login, and now: [display] mode = browser (default) in a normal
#      window of your browser; fullscreen: full screen (a Chrome/Edge/Brave window if installed, else Safari: press
#      Ctrl+Cmd+F once; Cmd+Q closes it); none: never. /Applications/nuc-console.webloc opens it again any time.
# Options (environment): NUC_CONSOLE_DISPLAY=browser|fullscreen|none   written to [display] mode in config.ini
set -euo pipefail
[ "$(uname -s)" = Darwin ] || { echo "this installer is for macOS; on Linux use install.sh" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "root required: sudo $0" >&2; exit 1; }
cd "$(dirname "$0")"

DEST=/opt/nuc-console
ETC=/etc/nuc-console
LIB=/var/lib/nuc-console
NOTIFY_LIB=/var/lib/nuc-console-notify  # the Telegram notifier's own folder: bot token, paired chat
LOG=/var/log/nuc-console
LD=/Library/LaunchDaemons
LA=/Library/LaunchAgents
SVC_USER=_nuc-console
PY_VERSION=3.14.8
PY_PKG_SHA256=507fc086c5c006ff875d344a75b4e67b8fb3c401f1bc4908c6250adb673d4907  # verified against python.org's Sigstore signature
PY_FRAMEWORK=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
CACHE=/Library/Caches/nuc-console  # downloads kept for the next install or update (root:wheel 0755, files 0644)
CONSOLE_UID="$(stat -f %u /dev/console)"  # the user at the screen (0 at the login window)

if [ "${1:-}" = "--uninstall" ]; then
    # com.nuc-console.ai: the local AI model server, there only after `nuc-console-ai serve --install-service`
    for label in com.nuc-console.collector com.nuc-console.web com.nuc-console.notify com.nuc-console.ai; do launchctl bootout "system/$label" 2>/dev/null || true; done
    [ "$CONSOLE_UID" = 0 ] || launchctl bootout "gui/$CONSOLE_UID/com.nuc-console.display" 2>/dev/null || true
    rm -f "$LD"/com.nuc-console.*.plist "$LA/com.nuc-console.display.plist" /etc/newsyslog.d/nuc-console.conf /etc/newsyslog.d/nuc-console-ai.conf /Applications/nuc-console.webloc
    for link in /usr/local/bin/nuc-console-problems /usr/local/bin/nuc-console-ask /usr/local/bin/nuc-console-telegram /usr/local/sbin/nuc-console-accept /usr/local/sbin/nuc-console-update /usr/local/sbin/nuc-console-ai; do
        if [ -L "$link" ]; then rm -f "$link"; fi
    done
    rm -rf "$DEST" "$NOTIFY_LIB"  # the Telegram bot token is in the notifier's folder: it must not outlive the installation
    echo "removed ($ETC, $LIB, $LOG, the download cache $CACHE and the user $SVC_USER are left in place; so are the AI runtime and models in \"/Library/Application Support/nuc-console/ai\": delete that folder to free the disk)"
    exit 0
fi

# ---- 1. Python --------------------------------------------------------------------------------------------------------
py_ok() {  # Python 3.8+, owned by root (the collector runs it as root)
    [ -x "$1" ] && [ "$(stat -f %u "$(python_real "$1")")" = 0 ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null
}
python_real() { "$1" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null || echo "$1"; }
pkg_ok() {  # $1: the pinned SHA-256 and the Python Software Foundation's signature
    echo "$PY_PKG_SHA256  $1" | shasum -a 256 -c - >/dev/null 2>&1 \
        && pkgutil --check-signature "$1" | grep -q "Developer ID Installer: Python Software Foundation"
}
# Sets PKG to the python.org package in $CACHE, downloaded only if no good copy is there. A copy is reused when it is
# root's (0644, in a root-owned 0755 folder: an admin can create folders in /Library/Caches, so anything else in its
# place is replaced) and passes the hash and signature checks. A download goes to a temporary folder in $CACHE and gets
# the name that is reused only after both checks: an interrupted or altered download is never picked up again.
fetch_python_pkg() {
    if [ -L "$CACHE" ] || { [ -e "$CACHE" ] && [ "$(stat -f '%u %Lp' "$CACHE")" != "0 755" ]; }; then rm -rf "$CACHE"; fi
    [ -e "$CACHE" ] || { mkdir -m 0755 "$CACHE" && chown root:wheel "$CACHE"; }
    if [ -L "$CACHE" ] || [ ! -d "$CACHE" ] || [ "$(stat -f '%u %Lp' "$CACHE")" != "0 755" ]; then
        echo "$CACHE is not a root-owned 0755 folder: not installed" >&2; exit 1
    fi
    PKG="$CACHE/python-$PY_VERSION-macos11.pkg"
    if [ -f "$PKG" ] && [ ! -L "$PKG" ] && [ "$(stat -f '%u %Lp' "$PKG")" = "0 644" ] && pkg_ok "$PKG"; then
        echo "nuc-console: Python $PY_VERSION already downloaded: $PKG"
        return 0
    fi
    rm -rf "$PKG" "$CACHE"/.download.*  # a copy that failed the checks, what an interrupted download left
    DL="$(mktemp -d "$CACHE/.download.XXXXXX")"
    echo "nuc-console: downloading Python $PY_VERSION from python.org to $CACHE"
    curl -fsSL -o "$DL/python.pkg" "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-macos11.pkg" \
        || { rm -rf "$DL"; echo "python.pkg: download failed, not installed" >&2; exit 1; }
    echo "$PY_PKG_SHA256  $DL/python.pkg" | shasum -a 256 -c - >/dev/null \
        || { rm -rf "$DL"; echo "python.pkg: wrong SHA-256, not installed" >&2; exit 1; }
    pkgutil --check-signature "$DL/python.pkg" | grep -q "Developer ID Installer: Python Software Foundation" \
        || { rm -rf "$DL"; echo "python.pkg is not signed by the Python Software Foundation: not installed" >&2; exit 1; }
    chmod 0644 "$DL/python.pkg"
    mv -f "$DL/python.pkg" "$PKG"  # same folder: atomic
    rm -rf "$DL"
    for old in "$CACHE"/python-*-macos11.pkg; do  # the package of an older pin is never used again
        if [ "$old" != "$PKG" ] && [ -f "$old" ]; then rm -f "$old"; fi
    done
}
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
    fetch_python_pkg
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    # framework only: no IDLE/apps, no /usr/local/bin links, no shell profile changes, no pip
    {
        echo '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><array>'
        for c in PythonApplications PythonUnixTools PythonDocumentation PythonProfileChanges PythonInstallPip; do
            echo "<dict><key>attributeSetting</key><integer>0</integer><key>choiceAttribute</key><string>selected</string><key>choiceIdentifier</key><string>org.python.Python.$c-3.14</string></dict>"
        done
        echo '</array></plist>'
    } > "$tmp/choices.xml"
    installer -pkg "$PKG" -target / -applyChoiceChangesXML "$tmp/choices.xml" >/dev/null
    py_ok "$PY_FRAMEWORK" || { echo "Python installation failed" >&2; exit 1; }
    PY="$PY_FRAMEWORK"
fi
echo "nuc-console: using $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# ---- 2. files ---------------------------------------------------------------------------------------------------------
fill() { sed -e "s|@PYTHON@|$PY|g" -e "s|@DEST@|$DEST|g" -e "s|@DISPLAY@|${DISPLAY_ARG:-}|g" "$1"; }
load() {  # right after a bootout launchd may still be unloading the job: retry for a few seconds
    for _ in 1 2 3 4 5; do launchctl bootstrap "$1" "$2" 2>/dev/null && return 0; sleep 1; done
    launchctl bootstrap "$1" "$2"
}
ensure_service_user() {  # the web view and the Telegram notifier run as this account, never as root
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
}
for label in com.nuc-console.collector com.nuc-console.web com.nuc-console.notify; do launchctl bootout "system/$label" 2>/dev/null || true; done
install -d -m 0755 "$DEST" "$DEST/bin" "$ETC" "$LIB" "$LOG"
rm -f "$DEST"/*.py
install -m 0644 src/*.py "$DEST/"
for f in nuc-console-accept nuc-console-ai nuc-console-problems nuc-console-ask nuc-console-update nuc-console-telegram; do
    sed -e "s|/usr/bin/python3|$PY|g" -e "s|/opt/nuc-console/|$DEST/|g" "bin/$f" > "$DEST/bin/$f"
    chmod 0755 "$DEST/bin/$f"
done
# commands on the PATH, but only into folders root owns (on Intel Macs Homebrew makes /usr/local/bin a user's folder:
# a link there could be swapped for anything, then run with sudo)
for pair in "bin:nuc-console-problems" "bin:nuc-console-ask" "bin:nuc-console-telegram" "sbin:nuc-console-accept" "sbin:nuc-console-update" "sbin:nuc-console-ai"; do
    dir="/usr/local/${pair%%:*}" cmd="${pair#*:}"
    [ -d "$dir" ] || { [ "$(stat -f %u /usr/local 2>/dev/null || echo 1)" = 0 ] && install -d -m 0755 "$dir"; } || true
    if [ -d "$dir" ] && [ "$(stat -f %u "$dir")" = 0 ]; then ln -sf "$DEST/bin/$cmd" "$dir/$cmd"
    else echo "nuc-console: $dir is not owned by root: run $DEST/bin/$cmd by its full path"; fi
done
[ -e "$ETC/config.ini" ] || install -m 0644 config/config.ini "$ETC/config.ini"  # never overwrite the admin's edits
install -m 0644 config/config.ini "$ETC/config.ini.dist"  # always refreshed: diff it with config.ini to see new options
case "${NUC_CONSOLE_DISPLAY:-}" in
    "") ;;
    browser|fullscreen|kiosk|none|no) "$PY" -B "$DEST/nuc_config.py" --set "$ETC/config.ini" display mode "$NUC_CONSOLE_DISPLAY" ;;
    *) echo "invalid NUC_CONSOLE_DISPLAY: $NUC_CONSOLE_DISPLAY (browser, fullscreen or none)" >&2; exit 1 ;;
esac
MODE="$("$PY" -B "$DEST/nuc_config.py" --get display mode)"
PORT="$("$PY" -B "$DEST/nuc_config.py" --get web port)"
URL="http://127.0.0.1:$PORT/?fit=1"
echo "$LOG/collector.log  root:wheel  644  5  1024  *  NJ" > /etc/newsyslog.d/nuc-console.conf  # rotate at 1 MB, keep 5

# ---- 3. collector and web view --------------------------------------------------------------------------------------------
fill launchd/com.nuc-console.collector.plist > "$LD/com.nuc-console.collector.plist"
chown root:wheel "$LD/com.nuc-console.collector.plist" && chmod 0644 "$LD/com.nuc-console.collector.plist"
t0=$(date +%s)
load system "$LD/com.nuc-console.collector.plist"
# the web view: as configured in [web] if enabled there; else, for this Mac's own browser, on 127.0.0.1 only (--local)
WEB=no
if "$PY" -B "$DEST/web.py" --enabled || [ "$MODE" != none ]; then WEB=yes; fi
if [ "$WEB" = yes ]; then
    ensure_service_user
    touch "$LOG/web.log" && chown "$SVC_USER:$SVC_USER" "$LOG/web.log"
    echo "$LOG/web.log  $SVC_USER:$SVC_USER  644  5  1024  *  NJ" >> /etc/newsyslog.d/nuc-console.conf
    fill launchd/com.nuc-console.web.plist > "$LD/com.nuc-console.web.plist"
    chown root:wheel "$LD/com.nuc-console.web.plist" && chmod 0644 "$LD/com.nuc-console.web.plist"
    load system "$LD/com.nuc-console.web.plist"
    # /Applications/nuc-console.webloc: Spotlight, Launchpad or a double-click open the dashboard in the default browser
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
        "<plist version=\"1.0\"><dict><key>URL</key><string>$URL</string></dict></plist>" > /Applications/nuc-console.webloc
else
    rm -f "$LD/com.nuc-console.web.plist" /Applications/nuc-console.webloc
fi
# the Telegram notifier, as the same account: always loaded, it exits 0 at once (and stays idle) unless [telegram] enabled = yes and
# the chat is paired; outbound HTTPS to api.telegram.org only. Its folder holds the bot token: nobody else can list it (0711)
ensure_service_user
install -d -m 0711 -o "$SVC_USER" -g "$SVC_USER" "$NOTIFY_LIB"
touch "$LOG/notify.log" && chown "$SVC_USER:$SVC_USER" "$LOG/notify.log"
echo "$LOG/notify.log  $SVC_USER:$SVC_USER  644  5  1024  *  NJ" >> /etc/newsyslog.d/nuc-console.conf
fill launchd/com.nuc-console.notify.plist > "$LD/com.nuc-console.notify.plist"
chown root:wheel "$LD/com.nuc-console.notify.plist" && chmod 0644 "$LD/com.nuc-console.notify.plist"
load system "$LD/com.nuc-console.notify.plist"

# ---- 4. first snapshot, baseline, dashboard ---------------------------------------------------------------------------
# wait for a net.json written AFTER the collector start: an older one would be stale
for _ in $(seq 1 90); do [ "$(stat -f %m /var/run/nuc-console/net.json 2>/dev/null || echo 0)" -ge "$t0" ] && break; sleep 1; done
"$PY" -B "$DEST/render.py" --accept --if-missing || echo "warning: baseline not created (collector not ready yet): run sudo nuc-console-accept"
if [ "$WEB" = yes ]; then  # the web view starts in a moment: wait for it before opening anything
    for _ in $(seq 1 20); do curl -fsS -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null && break; sleep 1; done
fi
[ "$CONSOLE_UID" = 0 ] || launchctl bootout "gui/$CONSOLE_UID/com.nuc-console.display" 2>/dev/null || true
if [ "$MODE" = none ]; then
    rm -f "$LA/com.nuc-console.display.plist"
    where="never opens by itself ([display] mode = none)"
else
    # at every login, as that user (never root): a normal browser window (--open) or a full-screen one (--kiosk)
    if [ "$MODE" = fullscreen ]; then DISPLAY_ARG=--kiosk; else DISPLAY_ARG=--open; fi
    fill launchd/com.nuc-console.display.plist > "$LA/com.nuc-console.display.plist"
    chown root:wheel "$LA/com.nuc-console.display.plist" && chmod 0644 "$LA/com.nuc-console.display.plist"
    if [ "$CONSOLE_UID" != 0 ]; then  # open it now for the user at the screen; everyone else gets it at login
        load "gui/$CONSOLE_UID" "$LA/com.nuc-console.display.plist" || echo "nuc-console: the dashboard opens at the next login"
    fi
    if [ "$MODE" = fullscreen ]; then
        where="opens full screen at every login (Cmd+Q closes it, Ctrl+Cmd+F leaves full screen; again: Applications > nuc-console)"
    else
        where="opens in your browser at every login (again: Applications > nuc-console, or $URL)"
    fi
fi
echo "ok: collector running; dashboard $where. Text size: A- / A+ at the bottom of the page. Config: $ETC/config.ini  Logs: $LOG"
