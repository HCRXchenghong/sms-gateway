#!/usr/bin/env bash
# install.sh - installer for sms-gateway on Ubuntu 22.04/24.04.
#
# Common usage:
#   sudo bash install.sh
#   sudo bash install.sh --tls=letsencrypt --domain=sms.example.com --email=admin@example.com
#
# Options:
#   --tls=self-signed        Use a local self-signed certificate (default)
#   --tls=letsencrypt       Request/refresh a Let's Encrypt certificate
#   --domain=DOMAIN         Domain name for Let's Encrypt
#   --email=EMAIL           Email address for Let's Encrypt
#   --port=PORT             HTTPS listen port, default 8443
#   --scan-only             Check host dependencies without installing
#   --no-driver             Skip udev/driver setup
#   --keep-modemmanager     Do not disable ModemManager
set -euo pipefail

A7670G_USB_IDS=("1e0e:9001" "1e0e:9011" "1e0e:9018")

SVC_NAME="sms-gateway"
SVC_USER="smsgw"
SVC_GROUP="smsgw"
APP_DIR="/opt/${SVC_NAME}"
DATA_DIR="/var/lib/${SVC_NAME}"
LOG_DIR="/var/log/${SVC_NAME}"
ETC_DIR="/etc/${SVC_NAME}"
TLS_DIR="${ETC_DIR}/tls"
ENV_FILE="${ETC_DIR}/${SVC_NAME}.env"
UDEV_RULE="/etc/udev/rules.d/99-a7670g.rules"
SYSTEMD_UNIT="/etc/systemd/system/${SVC_NAME}.service"
LE_RENEW_HOOK="/etc/letsencrypt/renewal-hooks/deploy/${SVC_NAME}.sh"
SECRET_KEY="${ETC_DIR}/secret.key"
PORT="${PORT:-8443}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

TLS_MODE="self-signed"
LE_DOMAIN=""
LE_EMAIL=""
SCAN_ONLY=0
SKIP_DRIVER=0
DISABLE_MODEMMANAGER=1
for arg in "$@"; do
    case "$arg" in
        --tls=self-signed) TLS_MODE="self-signed" ;;
        --tls=letsencrypt) TLS_MODE="letsencrypt" ;;
        --domain=*)        LE_DOMAIN="${arg#--domain=}" ;;
        --email=*)         LE_EMAIL="${arg#--email=}" ;;
        --port=*)          PORT="${arg#--port=}" ;;
        --scan-only)       SCAN_ONLY=1 ;;
        --no-driver)       SKIP_DRIVER=1 ;;
        --keep-modemmanager) DISABLE_MODEMMANAGER=0 ;;
        -h|--help)
            sed -n '1,80p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf "\n\033[1;34m== %s ==\033[0m\n" "$1"; }
ok()   { printf "  \033[1;32mOK\033[0m %s\n" "$1"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$1"; }
die()  { printf "\n\033[1;31m[FAIL]\033[0m %s\n" "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "please run with sudo"
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
case "${ID:-}/${VERSION_ID:-}" in
    ubuntu/22.04) ok "Ubuntu 22.04" ;;
    ubuntu/*)     warn "Ubuntu ${VERSION_ID:-?}; tested on 22.04" ;;
    *)            warn "Non-Ubuntu system: ${ID:-?}" ;;
esac
command -v apt-get >/dev/null || die "apt-get is required"
command -v systemctl >/dev/null || die "systemd is required"

REQUIRED_APT_PKGS=(python3 python3-venv python3-pip rsync usb-modeswitch usbutils udev openssl curl ca-certificates iproute2)
[[ "$TLS_MODE" == "letsencrypt" ]] && REQUIRED_APT_PKGS+=(certbot)
[[ "$TLS_MODE" == "letsencrypt" && (-z "$LE_DOMAIN" || -z "$LE_EMAIL") ]] && die "letsencrypt requires --domain and --email"

say "1) Scan"
MISSING_APT=()
for p in "${REQUIRED_APT_PKGS[@]}"; do
    if dpkg-query -W -f='${Status}\n' "$p" 2>/dev/null | grep -q "install ok installed"; then
        ok "apt package installed: $p"
    else
        MISSING_APT+=("$p")
        warn "apt package missing: $p"
    fi
done

if [[ "$SCAN_ONLY" -eq 1 ]]; then
    say "scan complete"
    exit 0
fi

say "2) Install dependencies"
if (( ${#MISSING_APT[@]} > 0 )); then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${MISSING_APT[@]}" >/dev/null
fi
ok "dependencies ready"

say "3) Configure USB driver"
if [[ "$SKIP_DRIVER" -ne 1 ]]; then
    if [[ "$DISABLE_MODEMMANAGER" -eq 1 ]] && systemctl list-unit-files ModemManager.service >/dev/null 2>&1; then
        systemctl disable --now ModemManager.service >/dev/null 2>&1 || true
        ok "ModemManager disabled to avoid AT port contention"
    fi
    modprobe option || true
    DETECTED_USB_ID=""
    for vidpid in "${A7670G_USB_IDS[@]}"; do
        if lsusb 2>/dev/null | grep -iq "ID ${vidpid}"; then
            DETECTED_USB_ID="$vidpid"
            break
        fi
    done
    if [[ -n "$DETECTED_USB_ID" && -w /sys/bus/usb-serial/drivers/option1/new_id ]]; then
        echo "${DETECTED_USB_ID/:/ }" > /sys/bus/usb-serial/drivers/option1/new_id 2>/dev/null || true
    fi
    {
        echo "# Auto-bind A7670G to option driver and grant access to ${SVC_USER}."
        for vidpid in "${A7670G_USB_IDS[@]}"; do
            v="${vidpid%%:*}"; p="${vidpid##*:}"
            echo "ACTION==\"add\", SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"${v}\", ATTRS{idProduct}==\"${p}\", RUN+=\"/sbin/modprobe option\", RUN+=\"/bin/sh -c '/bin/echo ${v} ${p} > /sys/bus/usb-serial/drivers/option1/new_id'\""
            echo "SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"${v}\", ATTRS{idProduct}==\"${p}\", GROUP=\"${SVC_GROUP}\", MODE=\"0660\""
        done
    } > "$UDEV_RULE"
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=tty || true
fi
ok "driver rules ready"

say "4) Users and directories"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin --user-group "$SVC_USER"
fi
usermod -aG dialout "$SVC_USER" || true
install -d -m 0750 -o "$SVC_USER" -g "$SVC_GROUP" "$APP_DIR"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_GROUP" "$DATA_DIR"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_GROUP" "$LOG_DIR"
install -d -m 0750 -o root -g "$SVC_GROUP" "$ETC_DIR" "$TLS_DIR"

say "5) Sync code and Python dependencies"
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='_devdata' \
      "${SCRIPT_DIR}/app" "${SCRIPT_DIR}/requirements.txt" "$APP_DIR/"
chown -R "$SVC_USER":"$SVC_GROUP" "$APP_DIR"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
ok "app installed"

say "6) TLS and environment"
TLS_CERT="${TLS_DIR}/cert.pem"
TLS_KEY="${TLS_DIR}/key.pem"
case "$TLS_MODE" in
    self-signed)
        if [[ ! -s "$TLS_CERT" || ! -s "$TLS_KEY" ]]; then
            openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
                -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
                -keyout "$TLS_KEY" -out "$TLS_CERT" 2>/dev/null
        fi
        ;;
    letsencrypt)
        if [[ ! -e "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" ]]; then
            certbot certonly --standalone --non-interactive --agree-tos \
                -d "$LE_DOMAIN" -m "$LE_EMAIL" --preferred-challenges http
        fi
        install -m 0644 -o root -g "$SVC_GROUP" "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" "$TLS_CERT"
        install -m 0640 -o root -g "$SVC_GROUP" "/etc/letsencrypt/live/${LE_DOMAIN}/privkey.pem" "$TLS_KEY"
        install -d -m 0755 "$(dirname "$LE_RENEW_HOOK")"
        cat > "$LE_RENEW_HOOK" <<EOF
#!/usr/bin/env bash
set -euo pipefail
install -m 0644 -o root -g "${SVC_GROUP}" "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" "${TLS_CERT}"
install -m 0640 -o root -g "${SVC_GROUP}" "/etc/letsencrypt/live/${LE_DOMAIN}/privkey.pem" "${TLS_KEY}"
systemctl try-restart "${SVC_NAME}.service" >/dev/null 2>&1 || true
EOF
        chmod 0755 "$LE_RENEW_HOOK"
        systemctl enable --now certbot.timer >/dev/null 2>&1 || true
        ;;
esac
chown root:"$SVC_GROUP" "$TLS_CERT" "$TLS_KEY" 2>/dev/null || true
chmod 0640 "$TLS_KEY" 2>/dev/null || true
chmod 0644 "$TLS_CERT" 2>/dev/null || true

if [[ ! -s "$SECRET_KEY" ]]; then
    head -c 32 /dev/urandom > "$SECRET_KEY"
    chown root:"$SVC_GROUP" "$SECRET_KEY"
    chmod 0640 "$SECRET_KEY"
fi

cat > "$ENV_FILE" <<EOF
SMSGW_HOST=0.0.0.0
SMSGW_PORT=${PORT}
SMSGW_DATA_DIR=${DATA_DIR}
SMSGW_LOG_DIR=${LOG_DIR}
SMSGW_CONFIG_DIR=${ETC_DIR}
SMSGW_TLS_CERT=${TLS_CERT}
SMSGW_TLS_KEY=${TLS_KEY}
SMSGW_SESSION_SECRET_FILE=${SECRET_KEY}
SMSGW_MOCK_MODEM=false
SMSGW_MESSAGE_RETENTION_DAYS=7
SMSGW_API_SEND_RATE_LIMIT_PER_MINUTE=30
EOF
chown root:"$SVC_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

say "7) systemd service"
cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=SMS Gateway (SIMCom A7670G)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_GROUP}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app \\
    --host 0.0.0.0 --port ${PORT} \\
    --ssl-certfile ${TLS_CERT} --ssl-keyfile ${TLS_KEY}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${DATA_DIR} ${LOG_DIR} ${ETC_DIR}
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null
systemctl restart "$SVC_NAME"
sleep 2
systemctl is-active --quiet "$SVC_NAME" || { journalctl -u "$SVC_NAME" --no-pager -n 60 || true; die "service failed to start"; }

ip_show=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -n1 | cut -d/ -f1)
ip_show="${ip_show:-<server-ip>}"
display_host="${LE_DOMAIN:-$ip_show}"

say "Done"
cat <<EOF

  Web console: https://${display_host}:${PORT}/
  Service:     ${SVC_NAME}.service
  Logs:        journalctl -u ${SVC_NAME} -f
  Data:        ${DATA_DIR}
  Config:      ${ETC_DIR}
EOF
