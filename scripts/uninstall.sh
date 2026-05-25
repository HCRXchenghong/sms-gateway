#!/usr/bin/env bash
# uninstall.sh - remove service & files. Data dir is preserved unless --purge.
set -euo pipefail

SVC_NAME="sms-gateway"
APP_DIR="/opt/${SVC_NAME}"
DATA_DIR="/var/lib/${SVC_NAME}"
LOG_DIR="/var/log/${SVC_NAME}"
ETC_DIR="/etc/${SVC_NAME}"
UDEV_RULE="/etc/udev/rules.d/99-a7670g.rules"
SYSTEMD_UNIT="/etc/systemd/system/${SVC_NAME}.service"
LE_RENEW_HOOK="/etc/letsencrypt/renewal-hooks/deploy/${SVC_NAME}.sh"

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "root required"; exit 1; }

systemctl disable --now "${SVC_NAME}" 2>/dev/null || true
rm -f "$SYSTEMD_UNIT"
rm -f "$LE_RENEW_HOOK"
systemctl daemon-reload

rm -f "$UDEV_RULE"
udevadm control --reload-rules || true

rm -rf "$APP_DIR"

if [[ "$PURGE" -eq 1 ]]; then
    rm -rf "$DATA_DIR" "$LOG_DIR" "$ETC_DIR"
    userdel smsgw 2>/dev/null || true
    groupdel smsgw 2>/dev/null || true
    echo "purged."
else
    echo "kept data directory: ${DATA_DIR} (use --purge to remove it)"
fi
