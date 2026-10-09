#!/usr/bin/env bash
# Install nuc-console. Idempotent. Needs root: sudo ./install.sh
# The monitor switches to the dashboard immediately, no reboot (getty on the chosen tty is masked and stopped).
# Rollback: sudo ./install.sh --uninstall
# Python: the system's /usr/bin/python3 (3.8+) when there is one, else the one the release archive carries (python/, a
# python-build-standalone build for this processor): it is copied to /opt/nuc-console/python and the units and commands run it.
# The archive needs no Python and no network: download, unpack, install.
# Options (environment): NUC_CONSOLE_TZ=Europe/Rome  time zone of the dashboard (default: the system one)
#                                 NUC_CONSOLE_VT=1            virtual terminal to draw on (default 1; on desktops
#                                                             use a free one, e.g. 3: GDM takes tty1 and tty2). Re-running
#                                                             without options keeps the values already chosen.
set -euo pipefail
# macOS: the same command, its own installer (launchd instead of systemd, BSD tools); Windows: install-windows.cmd
if [ "$(uname -s)" = Darwin ]; then exec /bin/bash "$(dirname "$0")/install-macos.sh" "$@"; fi
[ "$(id -u)" -eq 0 ] || { echo "root required: sudo $0" >&2; exit 1; }
cd "$(dirname "$0")"
DEST=/opt/nuc-console
UNITD=/etc/systemd/system/nuc-console.service.d
UNITF=/etc/systemd/system/nuc-console.service
# values already installed (kept on re-install): from the drop-in, or from the old unit that had the time zone hardcoded
# sed quits at the first match by itself: `sed ... | head -1` under pipefail dies with SIGPIPE (rc 141) when two files match.
# (`s/x/y/{p;q}` is NOT valid sed: braces go around the whole command, `/x/{s///;p;q}`; tests/test_install.py runs these lines)
OLD_TZ="$(sed -n '/^Environment=TZ=/{s///;p;q}' "$UNITD/local.conf" "$UNITF" 2>/dev/null || true)"
OLD_VT="$(sed -n '/^TTYPath=\/dev\/tty/{s///;p;q}' "$UNITD/local.conf" 2>/dev/null || true)"
TZ_VAL="${NUC_CONSOLE_TZ:-$OLD_TZ}"
VT="${NUC_CONSOLE_VT:-${OLD_VT:-1}}"

if [ "${1:-}" = "--uninstall" ]; then
    for u in nuc-console.service nuc-console-collector.service nuc-console-web.service nuc-console-notify.service; do
        systemctl disable --now "$u" 2>/dev/null || true  # one by one: a unit an older version did not have must not stop the others
    done
    # the AI model server exists only after `nuc-console-ai serve --install-service`; it runs code from $DEST, so it goes too
    systemctl disable --now nuc-console-ai.service 2>/dev/null || true
    rm -f /usr/local/bin/nuc-console-problems /usr/local/bin/nuc-console-ask "$UNITF" /etc/systemd/system/nuc-console-collector.service /etc/systemd/system/nuc-console-web.service /etc/systemd/system/nuc-console-ai.service
    rm -f /usr/local/bin/nuc-console-telegram /etc/systemd/system/nuc-console-notify.service
    rm -rf /var/lib/nuc-console-notify  # the Telegram bot token is in there: it must not outlive the installation
    rm -rf "$DEST"
    rm -f /usr/local/sbin/nuc-console-accept /usr/local/sbin/nuc-console-update /usr/local/sbin/nuc-console-ai /usr/local/sbin/nuc-console-config
    systemctl daemon-reload
    rm -rf "$UNITD"
    systemctl unmask "getty@tty$VT.service"
    systemctl start "getty@tty$VT.service"
    echo "removed: login on tty$VT restored (user nuc-console, /var/lib/nuc-console and /etc/nuc-console left in place; so are the AI runtime and models in /var/lib/nuc-console/ai: delete that folder to free the disk)"
    exit 0
fi

[[ "$VT" =~ ^[0-9]+$ ]] && [ "$VT" -ge 1 ] && [ "$VT" -le 63 ] || { echo "invalid NUC_CONSOLE_VT: $VT" >&2; exit 1; }
[ -z "$TZ_VAL" ] || [ -e "/usr/share/zoneinfo/$TZ_VAL" ] || { echo "invalid time zone: $TZ_VAL" >&2; exit 1; }
# stopping getty@ttyN kills the session running on that terminal: never install from there
[ "$(tty 2>/dev/null)" != "/dev/tty$VT" ] || { echo "do not run from tty$VT: use SSH or another terminal" >&2; exit 1; }

# ---- Python 3.8+: the system's, else the one of the archive ---------------------------------------------------------------------
SYS_PY=/usr/bin/python3  # what the units and the commands name
py_ok() { [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; }
PY=$SYS_PY
if ! py_ok "$SYS_PY"; then
    [ -x python/bin/python3 ] || { echo "Python 3.8 or newer is needed at $SYS_PY: install python3, or use the release archive for $(uname -m) (it carries its own Python in python/; this folder has none: a clone?)" >&2; exit 1; }
    PY=$DEST/python/bin/python3
fi

id nuc-console >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console
# the Telegram notifier has a user of its own: the web view (user nuc-console, maybe reachable on the LAN) cannot read the bot token
id nuc-console-notify >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console-notify
install -d "$DEST"
rm -f "$DEST"/*.py  # a module dropped from src/ must not linger
if [ "$PY" != "$SYS_PY" ]; then
    # the archive's Python, copied (links kept) and made root's: the services run it, as root and as their own users, so nobody else
    # may write it and everybody can read it (an archive extracted with a strict umask would not be). Checked before it is put in place.
    rm -rf "$DEST/python.new"
    cp -a python "$DEST/python.new"
    chown -R root:root "$DEST/python.new"
    chmod -R u+rwX,go+rX,go-w "$DEST/python.new"
    py_ok "$DEST/python.new/bin/python3" || { rm -rf "$DEST/python.new"; echo "the Python in python/ does not run on this machine ($(uname -m)): use the release archive for it (linux-x86_64 or linux-arm64; glibc systems only)" >&2; exit 1; }
    rm -rf "$DEST/python.old"
    [ ! -e "$DEST/python" ] || mv "$DEST/python" "$DEST/python.old"
    mv "$DEST/python.new" "$DEST/python"
    rm -rf "$DEST/python.old"
    echo "nuc-console: using the Python of the archive: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"
else
    rm -rf "$DEST/python" "$DEST/python.new" "$DEST/python.old"  # one an earlier install put there, not used any more
fi
install -m 0644 src/*.py "$DEST"/  # every module: web.py needs htmlview.py, render.py graph.py
install -m 0644 systemd/*.service /etc/systemd/system/
install -m 0755 bin/nuc-console-accept bin/nuc-console-update /usr/local/sbin/
install -m 0755 bin/nuc-console-config /usr/local/sbin/  # brings an old config.ini to the shipped layout (never run by itself)
install -m 0755 bin/nuc-console-ai /usr/local/sbin/  # the optional local AI model: models, setup, serve (docs/AI.md); nothing runs until you ask
install -m 0755 bin/nuc-console-problems /usr/local/bin/
install -m 0755 bin/nuc-console-telegram /usr/local/bin/
install -m 0755 bin/nuc-console-ask /usr/local/bin/  # questions to that model (read-only)
if [ "$PY" != "$SYS_PY" ]; then  # the units and the commands name the system's Python: they run the archive's instead
    sed -i "s|$SYS_PY|$PY|g" /etc/systemd/system/nuc-console*.service /usr/local/sbin/nuc-console-{accept,update,ai,config} \
        /usr/local/bin/nuc-console-{problems,ask,telegram}
fi
install -d "$UNITD"
{
    echo "[Service]"
    echo "TTYPath=/dev/tty$VT"
    echo "Environment=NUC_CONSOLE_VT=$VT"  # src/ttyprep.py: font and blanking of that tty ([console] in config.ini)
    [ -z "$TZ_VAL" ] || echo "Environment=TZ=$TZ_VAL"
} > "$UNITD/local.conf"
install -d /etc/nuc-console
[ -e /etc/nuc-console/config.ini ] || install -m 0644 config/config.ini /etc/nuc-console/config.ini  # never overwrite the admin's edits
install -m 0644 config/config.ini /etc/nuc-console/config.ini.dist  # always refreshed: diff it with config.ini to see new options
install -d /var/lib/nuc-console
# the AI folder (docs/AI.md): the AI page of the web view and the AI screen of the console download the local model into it, as the user nuc-console
# (the units' ReadWritePaths). Empty until you choose a model there; what an earlier `sudo nuc-console-ai setup` put in it stays root's, and stays usable.
install -d -o nuc-console -g nuc-console -m 0755 /var/lib/nuc-console/ai /var/lib/nuc-console/ai/runtime /var/lib/nuc-console/ai/models
install -d -m 0711 -o nuc-console-notify -g nuc-console-notify /var/lib/nuc-console-notify  # the Telegram notifier's own folder: bot token, paired chat
# its inbox: the web view's Telegram page leaves its requests there (its unit is in this group: SupplementaryGroups); the group may create
# files and nothing else (no read, no list), and the setgid bit gives them the group, so only the notifier reads them (docs/TELEGRAM.md)
install -d -m 2730 -o nuc-console-notify -g nuc-console-notify /var/lib/nuc-console-notify/inbox
chmod 2730 /var/lib/nuc-console-notify/inbox
systemctl daemon-reload
systemctl enable nuc-console-collector.service
# restart, not --now: on an upgrade the service is already running and would keep the old code
t0=$(date +%s)
systemctl restart nuc-console-collector.service
systemctl enable nuc-console.service
systemctl mask --now "getty@tty$VT.service"
[ "$VT" = 1 ] || chvt "$VT" 2>/dev/null || true  # on desktops: switch the monitor to the chosen terminal
# port-alarm baseline: only if missing (a re-install must not reset the one you accepted).
# It captures the state of right now, even if that is what you want to fix: afterwards run sudo nuc-console-accept
# wait for a net.json written AFTER the collector restart: an older one would be stale
for _ in $(seq 1 60); do [ "$(stat -c %Y /run/nuc-console/net.json 2>/dev/null || echo 0)" -ge "$t0" ] && break; sleep 1; done
"$PY" "$DEST/render.py" --accept --if-missing || echo "warning: baseline not created (collector not ready yet): run sudo nuc-console-accept"
# optional web view (read-only; only the AI page has buttons): only if [web] enabled = yes in config.ini (never opens a port otherwise)
if "$PY" "$DEST/web.py" --enabled; then systemctl enable nuc-console-web.service && systemctl restart nuc-console-web.service
else systemctl disable --now nuc-console-web.service 2>/dev/null || true; fi
# optional Telegram notifier (outbound HTTPS only): always installed; it exits 0 while it has nothing to do (off, and no web view whose
# Telegram page may set it up), otherwise it runs: it sends when it is on and paired, and takes the web page's requests
systemctl enable nuc-console-notify.service
if "$PY" "$DEST/notify.py" --needed; then systemctl restart nuc-console-notify.service
else systemctl stop nuc-console-notify.service 2>/dev/null || true; fi
systemctl restart nuc-console.service
echo "ok: dashboard on tty$VT${TZ_VAL:+ (time zone $TZ_VAL)}. Logs: journalctl -u nuc-console -u nuc-console-collector"
