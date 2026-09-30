#!/usr/bin/env bash
# Enables ufw without the risk of locking yourself out: a timer turns ufw off by itself unless you confirm.
#   sudo ./scripts/enable-ufw.sh            enable (with interactive confirmation within the timeout)
#   ./scripts/enable-ufw.sh --dry-run       show what it would do, without root and without touching anything
# Variables: LAN (REQUIRED: your LAN subnet, e.g. 192.168.0.0/24), ROLLBACK_S (default 300 seconds).
#
# What it protects: NON-Docker services on the LAN (ssh, node, streamlit). It does NOT protect ports published by Docker
# (DOCKER-USER is empty: those need a bind on 127.0.0.1) nor Tailscale traffic (ts-input accepts before ufw).
# Fail-open by design: if the script is interrupted (Ctrl-C, SIGHUP, end of input) before confirmation, the timer
# still turns ufw off; that is the opposite of the risk of being locked out.
set -euo pipefail
LAN="${LAN:-}"
[ -n "$LAN" ] || { echo "set LAN to your subnet, e.g.: LAN=192.168.0.0/24 $0" >&2; exit 1; }
ROLLBACK_S="${ROLLBACK_S:-300}"

[[ "$LAN" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || { echo "invalid LAN: $LAN" >&2; exit 1; }
[[ "$ROLLBACK_S" =~ ^[0-9]+$ ]] || { echo "invalid ROLLBACK_S: $ROLLBACK_S" >&2; exit 1; }

apply_rules() {  # $1 = "echo" (dry-run) or empty (executes)
    local run=("$@")
    "${run[@]}" ufw default deny incoming
    "${run[@]}" ufw default allow outgoing
    "${run[@]}" ufw allow from "$LAN" to any port 22 proto tcp comment 'ssh from LAN'
    "${run[@]}" ufw allow in on tailscale0 comment 'tailnet'
    "${run[@]}" ufw allow 41641/udp comment 'tailscale direct'
    "${run[@]}" ufw logging low
}

if [ "${1:-}" = "--dry-run" ]; then
    echo "# safety net: in ${ROLLBACK_S}s ufw turns itself off unless you confirm"
    echo "systemd-run --unit=ufw-rollback --on-active=${ROLLBACK_S}s /usr/sbin/ufw --force disable"
    apply_rules echo
    echo "ufw --force enable"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "root required: sudo $0" >&2; exit 1; }
command -v ufw >/dev/null || { echo "ufw not installed" >&2; exit 1; }

if ufw status | grep -q '^Status: active'; then
    echo "ufw is already active: no changes. Current state:"
    ufw status verbose
    exit 0
fi

# a timer left by a previous attempt would make systemd-run fail with an unclear message
systemctl stop ufw-rollback.timer ufw-rollback.service 2>/dev/null || true
systemctl reset-failed 'ufw-rollback.*' 2>/dev/null || true

echo ">> safety net: in ${ROLLBACK_S}s ufw turns itself off unless you confirm"
systemd-run --unit=ufw-rollback --on-active="${ROLLBACK_S}s" /usr/sbin/ufw --force disable >/dev/null

apply_rules
ufw --force enable
ufw status verbose

cat <<MSG

ufw is ACTIVE. Now, from ANOTHER terminal, open a NEW ssh connection to the server
(from the LAN or via Tailscale) and check that it works. You have ${ROLLBACK_S} seconds.
MSG
read -r -p "Does the new connection work? [y/N] " ans || ans=""
if [[ "$ans" =~ ^[sSyY] ]]; then
    # the timer may have fired while you were answering: do not confirm a state that no longer exists
    if ! ufw status | grep -q '^Status: active'; then
        echo "WARNING: ufw is already disabled (the timer fired before confirmation). Run the script again." >&2
        exit 1
    fi
    systemctl stop ufw-rollback.timer
    echo "confirmed: ufw stays active, safety net cancelled."
    if [ -x /usr/local/sbin/nuc-console-accept ]; then
        echo ">> updating the alert baseline"
        /usr/local/sbin/nuc-console-accept || echo "baseline not updated: run sudo nuc-console-accept again in 30 seconds"
    fi
else
    ufw --force disable
    systemctl stop ufw-rollback.timer
    echo "not confirmed: ufw disabled (the rules stay saved; re-enable with the script)."
fi
