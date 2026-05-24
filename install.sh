#!/usr/bin/env bash
# install.sh - smart installer for sms-gateway on Ubuntu 22.04.
#
#   Step 1. Scan: report which dependencies, drivers, services and TLS
#           material are already present.
#   Step 2. Install only what's missing. Existing data, users, API keys
#           and certs are preserved.
#
# Usage:
#   sudo ./install.sh                                          # self-signed TLS
#   sudo ./install.sh --tls=letsencrypt --domain=sms.example.com --email=admin@example.com
#   sudo ./install.sh --scan-only                              # only scan, do not change anything
#   sudo ./install.sh --no-driver                              # skip driver step (e.g. testing without dongle)
set -euo pipefail

# ---------------------------------------------------------------------------
# Known SIMCom A7670G USB IDs. Add more as encountered.
# ---------------------------------------------------------------------------
A7670G_USB_IDS=(
    "1e0e:9001"
    "1e0e:9011"
    "1e0e:9018"
)

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
SECRET_KEY="${ETC_DIR}/secret.key"
PORT="${PORT:-8443}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

REQUIRED_APT_PKGS=(
    python3 python3-venv python3-pip
    rsync usb-modeswitch usbutils udev openssl curl ca-certificates
)

# -- arg parsing -------------------------------------------------------------
TLS_MODE="self-signed"
LE_DOMAIN=""
LE_EMAIL=""
SCAN_ONLY=0
SKIP_DRIVER=0
for arg in "$@"; do
    case "$arg" in
        --tls=self-signed) TLS_MODE="self-signed" ;;
        --tls=letsencrypt) TLS_MODE="letsencrypt" ;;
        --domain=*)        LE_DOMAIN="${arg#--domain=}" ;;
        --email=*)         LE_EMAIL="${arg#--email=}" ;;
        --port=*)          PORT="${arg#--port=}" ;;
        --scan-only)       SCAN_ONLY=1 ;;
        --no-driver)       SKIP_DRIVER=1 ;;
        -h|--help)
            sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# -- output ------------------------------------------------------------------
say()   { printf "\n\033[1;34m== %s ==\033[0m\n" "$1"; }
ok()    { printf "  \033[1;32m✓\033[0m %s\n" "$1"; }
miss()  { printf "  \033[1;33m·\033[0m %s\n" "$1"; }
todo()  { printf "  \033[1;33m→\033[0m %s\n" "$1"; }
warn()  { printf "  \033[1;33m!\033[0m %s\n" "$1"; }
die()   { printf "\n\033[1;31m[FAIL]\033[0m %s\n" "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请使用 sudo 运行：sudo $0 $*"

# ===========================================================================
# Phase 1 — scan
# ===========================================================================
say "1) 扫描当前环境"

. /etc/os-release 2>/dev/null || die "找不到 /etc/os-release"
case "${ID:-}/${VERSION_ID:-}" in
    ubuntu/22.04) ok "Ubuntu 22.04" ;;
    ubuntu/*)     warn "Ubuntu ${VERSION_ID:-?}（脚本仅在 22.04 上完整测试，继续）" ;;
    *)            warn "非 Ubuntu 系统（${ID:-?}），可能不兼容" ;;
esac
command -v apt-get   >/dev/null || die "缺少 apt-get（仅支持 Debian/Ubuntu 系）"
command -v systemctl >/dev/null || die "缺少 systemctl（需要 systemd）"

# Which apt packages are missing?
MISSING_APT=()
for p in "${REQUIRED_APT_PKGS[@]}"; do
    if dpkg-query -W -f='${Status}\n' "$p" 2>/dev/null | grep -q "install ok installed"; then
        ok "已安装 apt 包：$p"
    else
        miss "未安装 apt 包：$p"
        MISSING_APT+=("$p")
    fi
done
if [[ "$TLS_MODE" == "letsencrypt" ]]; then
    if dpkg-query -W -f='${Status}\n' certbot 2>/dev/null | grep -q "install ok installed"; then
        ok "已安装 apt 包：certbot"
    else
        miss "未安装 apt 包：certbot"
        MISSING_APT+=("certbot")
    fi
fi

# USB module presence
DETECTED_USB_ID=""
if [[ "$SKIP_DRIVER" -eq 1 ]]; then
    warn "跳过驱动检测（--no-driver）"
else
    for vidpid in "${A7670G_USB_IDS[@]}"; do
        if lsusb 2>/dev/null | grep -iq "ID ${vidpid}"; then
            DETECTED_USB_ID="$vidpid"
            break
        fi
    done
    if [[ -n "$DETECTED_USB_ID" ]]; then
        ok "检测到 4G 模组：${DETECTED_USB_ID}"
        if ls /dev/ttyUSB* >/dev/null 2>&1; then
            ok "ttyUSB 节点：$(ls /dev/ttyUSB* | tr '\n' ' ')"
        else
            miss "ttyUSB 节点不存在，需绑定驱动"
        fi
    else
        miss "未检测到 4G 模组（已扫描 VID:PID = ${A7670G_USB_IDS[*]}）"
    fi
fi

# Service user
if id -u "$SVC_USER" >/dev/null 2>&1; then
    ok "服务用户已存在：${SVC_USER}"
else
    miss "服务用户不存在：${SVC_USER}"
fi

# Service unit / state
if [[ -f "$SYSTEMD_UNIT" ]]; then
    state=$(systemctl is-active "$SVC_NAME" 2>/dev/null || true)
    ok "systemd 服务已部署（当前状态：${state:-unknown}）"
else
    miss "systemd 服务尚未部署"
fi

# TLS material
case "$TLS_MODE" in
    self-signed)
        if [[ -s "$TLS_DIR/cert.pem" && -s "$TLS_DIR/key.pem" ]]; then
            ok "已存在自签证书（保留）"
        else
            miss "自签证书缺失，将生成"
        fi
        ;;
    letsencrypt)
        [[ -n "$LE_DOMAIN" ]] || die "--tls=letsencrypt 必须配合 --domain="
        [[ -n "$LE_EMAIL"  ]] || die "--tls=letsencrypt 必须配合 --email="
        if [[ -e "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" ]]; then
            ok "已存在 Let's Encrypt 证书：${LE_DOMAIN}"
        else
            miss "Let's Encrypt 证书将申请：${LE_DOMAIN}"
        fi
        ;;
esac

# Existing data?
if [[ -s "$DATA_DIR/data.db" ]]; then
    ok "保留现有数据库：$DATA_DIR/data.db"
else
    miss "数据库尚未创建（首次部署）"
fi

if [[ "$SCAN_ONLY" -eq 1 ]]; then
    say "扫描完成（--scan-only），未做任何变更"
    exit 0
fi

# ===========================================================================
# Phase 2 — install only what's missing
# ===========================================================================
say "2) 安装缺失的依赖"

if (( ${#MISSING_APT[@]} > 0 )); then
    todo "apt-get install: ${MISSING_APT[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${MISSING_APT[@]}" >/dev/null
    ok "apt 依赖安装完成"
else
    ok "apt 依赖已齐全，跳过"
fi

# ---- driver ---------------------------------------------------------------
if [[ "$SKIP_DRIVER" -ne 1 ]]; then
    say "3) 加载并绑定 USB 串口驱动"
    if [[ -n "$DETECTED_USB_ID" ]]; then
        vid="${DETECTED_USB_ID%%:*}"
        pid="${DETECTED_USB_ID##*:}"

        # Mass-storage detection: A7670 sometimes boots in CD-ROM mode.
        if lsusb -d "$DETECTED_USB_ID" -v 2>/dev/null | grep -qi "Mass Storage"; then
            todo "模组当前为 Mass-Storage，调用 usb-modeswitch"
            usb_modeswitch -v 0x"$vid" -p 0x"$pid" -J 2>/dev/null || true
            sleep 2
        fi

        modprobe option || die "无法加载 option 内核模块"
        if [[ -w /sys/bus/usb-serial/drivers/option1/new_id ]]; then
            echo "${vid} ${pid}" > /sys/bus/usb-serial/drivers/option1/new_id 2>/dev/null || true
            ok "已写入 option.new_id = ${vid} ${pid}"
        fi
        # Wait for ttyUSB nodes
        for i in $(seq 1 30); do
            ls /dev/ttyUSB* >/dev/null 2>&1 && break
            sleep 1
        done
        if ls /dev/ttyUSB* >/dev/null 2>&1; then
            ok "ttyUSB 已就绪：$(ls /dev/ttyUSB* | tr '\n' ' ')"
        else
            warn "30 秒内 ttyUSB 节点仍未出现，请检查 dmesg"
        fi
    else
        warn "未检测到模组，跳过驱动绑定（部署后插入模组会通过 udev 自动绑定）"
    fi

    # Always (re)write udev rule so future hot-plug works.
    {
        echo "# Auto-bind A7670G to option driver and grant access to ${SVC_USER}."
        for vidpid in "${A7670G_USB_IDS[@]}"; do
            v="${vidpid%%:*}"; p="${vidpid##*:}"
            cat <<RULE
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="${v}", ATTRS{idProduct}=="${p}", RUN+="/sbin/modprobe option", RUN+="/bin/sh -c '/bin/echo ${v} ${p} > /sys/bus/usb-serial/drivers/option1/new_id'"
SUBSYSTEM=="tty", ATTRS{idVendor}=="${v}", ATTRS{idProduct}=="${p}", GROUP="${SVC_GROUP}", MODE="0660"
RULE
        done
    } > "$UDEV_RULE"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=tty || true
    ok "udev 规则已写入：${UDEV_RULE}"
fi

# ---- service user / dirs --------------------------------------------------
say "4) 部署用户与目录"

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin --user-group "$SVC_USER"
    ok "创建用户 ${SVC_USER}"
fi
usermod -aG dialout "$SVC_USER" || true

install -d -m 0750 -o "$SVC_USER" -g "$SVC_GROUP" "$APP_DIR"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_GROUP" "$DATA_DIR"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_GROUP" "$LOG_DIR"
install -d -m 0750 -o root        -g "$SVC_GROUP" "$ETC_DIR"
install -d -m 0750 -o root        -g "$SVC_GROUP" "$TLS_DIR"
ok "目录就绪"

# ---- code & venv ----------------------------------------------------------
say "5) 同步代码与 Python 依赖"

rsync -a --delete \
      --exclude='.git*' --exclude='.kiro' --exclude='__pycache__' \
      --exclude='*.pyc' --exclude='.venv' --exclude='_devdata' \
      --exclude='.env' --exclude='.env.dev' \
      "${SCRIPT_DIR}/app" "${SCRIPT_DIR}/requirements.txt" \
      "$APP_DIR/"
chown -R "$SVC_USER":"$SVC_GROUP" "$APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/.venv"
    ok "已创建 venv"
else
    ok "复用已有 venv"
fi
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
ok "Python 依赖完成"

# ---- TLS ------------------------------------------------------------------
say "6) TLS 证书"

TLS_CERT="${TLS_DIR}/cert.pem"
TLS_KEY="${TLS_DIR}/key.pem"

case "$TLS_MODE" in
    self-signed)
        if [[ ! -s "$TLS_CERT" || ! -s "$TLS_KEY" ]]; then
            openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
                -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
                -keyout "$TLS_KEY" -out "$TLS_CERT" 2>/dev/null
            ok "已生成自签证书"
        else
            ok "保留已有自签证书"
        fi
        ;;
    letsencrypt)
        if [[ ! -e "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" ]]; then
            certbot certonly --standalone --non-interactive --agree-tos \
                -d "$LE_DOMAIN" -m "$LE_EMAIL" --preferred-challenges http
            ok "Let's Encrypt 证书已签发"
        else
            ok "保留已有 Let's Encrypt 证书"
        fi
        ln -sf "/etc/letsencrypt/live/${LE_DOMAIN}/fullchain.pem" "$TLS_CERT"
        ln -sf "/etc/letsencrypt/live/${LE_DOMAIN}/privkey.pem"   "$TLS_KEY"
        systemctl enable --now certbot.timer >/dev/null 2>&1 || true
        ;;
esac
chown -h root:"$SVC_GROUP" "$TLS_CERT" "$TLS_KEY" 2>/dev/null || true
chmod 0640 "$TLS_KEY" 2>/dev/null || true
chmod 0644 "$TLS_CERT" 2>/dev/null || true

# Master key for AES-GCM
if [[ ! -s "$SECRET_KEY" ]]; then
    head -c 32 /dev/urandom > "$SECRET_KEY"
    chown root:"$SVC_GROUP" "$SECRET_KEY"
    chmod 0640 "$SECRET_KEY"
    ok "已生成主加密密钥"
else
    ok "保留已有主加密密钥"
fi

# Env file
cat > "$ENV_FILE" <<EOF
SMSGW_HOST=0.0.0.0
SMSGW_PORT=${PORT}
SMSGW_DATA_DIR=${DATA_DIR}
SMSGW_LOG_DIR=${LOG_DIR}
SMSGW_CONFIG_DIR=${ETC_DIR}
SMSGW_TLS_CERT=${TLS_CERT}
SMSGW_TLS_KEY=${TLS_KEY}
SMSGW_SESSION_SECRET_FILE=${SECRET_KEY}
EOF
chown root:"$SVC_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# ---- systemd unit ---------------------------------------------------------
say "7) systemd 服务"

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

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null
systemctl restart "$SVC_NAME"
sleep 2
if systemctl is-active --quiet "$SVC_NAME"; then
    ok "${SVC_NAME}.service 运行中"
else
    journalctl -u "$SVC_NAME" --no-pager -n 60 || true
    die "服务启动失败，请查看上面的日志"
fi

# ---- summary --------------------------------------------------------------
ip_show=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -n1 | cut -d/ -f1)
ip_show="${ip_show:-<server-ip>}"
display_host="${LE_DOMAIN:-$ip_show}"

say "完成"
cat <<EOF

  Web 控制台:   https://${display_host}:${PORT}/
  服务名称:     ${SVC_NAME}.service
  日志命令:     journalctl -u ${SVC_NAME} -f
  数据目录:     ${DATA_DIR}
  配置目录:     ${ETC_DIR}

  首次访问 Web 时按引导：
    1) 创建管理员账号与密码
    2) 用 Microsoft Authenticator 扫码绑定 2FA
    3) 妥善保存恢复码

EOF
