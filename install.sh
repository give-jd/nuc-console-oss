#!/usr/bin/env bash
# Install nuc-console. Idempotent. Needs root: sudo ./install.sh
# The monitor switches to the dashboard immediately, no reboot (getty on the chosen tty is masked and stopped).
# Rollback: sudo ./install.sh --uninstall
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
    rm -f /usr/local/bin/nuc-console-problems "$UNITF" /etc/systemd/system/nuc-console-collector.service /etc/systemd/system/nuc-console-web.service
    rm -f /usr/local/bin/nuc-console-telegram /etc/systemd/system/nuc-console-notify.service
    rm -rf /var/lib/nuc-console-notify  # the Telegram bot token is in there: it must not outlive the installation
    rm -rf "$DEST"
    rm -f /usr/local/sbin/nuc-console-accept
    systemctl daemon-reload
    rm -rf "$UNITD"
    systemctl unmask "getty@tty$VT.service"
    systemctl start "getty@tty$VT.service"
    echo "removed: login on tty$VT restored (user nuc-console, /var/lib/nuc-console and /etc/nuc-console left in place)"
    exit 0
fi

[[ "$VT" =~ ^[0-9]+$ ]] && [ "$VT" -ge 1 ] && [ "$VT" -le 63 ] || { echo "invalid NUC_CONSOLE_VT: $VT" >&2; exit 1; }
[ -z "$TZ_VAL" ] || [ -e "/usr/share/zoneinfo/$TZ_VAL" ] || { echo "invalid time zone: $TZ_VAL" >&2; exit 1; }
# stopping getty@ttyN kills the session running on that terminal: never install from there
[ "$(tty 2>/dev/null)" != "/dev/tty$VT" ] || { echo "do not run from tty$VT: use SSH or another terminal" >&2; exit 1; }

id nuc-console >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console
# the Telegram notifier has a user of its own: the web view (user nuc-console, maybe reachable on the LAN) cannot read the bot token
id nuc-console-notify >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nuc-console-notify
install -d "$DEST"
rm -f "$DEST"/*.py  # a module dropped from src/ must not linger
install -m 0644 src/*.py "$DEST"/  # every module: web.py needs htmlview.py, render.py graph.py
install -m 0644 systemd/*.service /etc/systemd/system/
install -m 0755 bin/nuc-console-accept /usr/local/sbin/
install -m 0755 bin/nuc-console-problems /usr/local/bin/
install -m 0755 bin/nuc-console-telegram /usr/local/bin/
install -d "$UNITD"
{
    echo "[Service]"
    echo "TTYPath=/dev/tty$VT"
    [ -z "$TZ_VAL" ] || echo "Environment=TZ=$TZ_VAL"
} > "$UNITD/local.conf"
install -d /etc/nuc-console
[ -e /etc/nuc-console/config.ini ] || install -m 0644 config/config.ini /etc/nuc-console/config.ini  # never overwrite the admin's edits
install -m 0644 config/config.ini /etc/nuc-console/config.ini.dist  # always refreshed: diff it with config.ini to see new options
install -d /var/lib/nuc-console
install -d -m 0711 -o nuc-console-notify -g nuc-console-notify /var/lib/nuc-console-notify  # the Telegram notifier's own folder: bot token, paired chat
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
python3 "$DEST/render.py" --accept --if-missing || echo "warning: baseline not created (collector not ready yet): run sudo nuc-console-accept"
# optional read-only web view: only if [web] enabled = yes in config.ini (never opens a port otherwise)
if python3 "$DEST/web.py" --enabled; then systemctl enable nuc-console-web.service && systemctl restart nuc-console-web.service
else systemctl disable --now nuc-console-web.service 2>/dev/null || true; fi
# optional Telegram notifier (outbound HTTPS only): always installed, it idles (exit 0) until [telegram] enabled = yes and paired
systemctl enable nuc-console-notify.service
if python3 "$DEST/notify.py" --enabled; then systemctl restart nuc-console-notify.service
else systemctl stop nuc-console-notify.service 2>/dev/null || true; fi
systemctl restart nuc-console.service
echo "ok: dashboard on tty$VT${TZ_VAL:+ (time zone $TZ_VAL)}. Logs: journalctl -u nuc-console -u nuc-console-collector"
