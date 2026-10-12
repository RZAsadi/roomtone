#!/usr/bin/env bash
set -Eeuo pipefail

# RoomTone installer for a fresh Ubuntu 22.04/24.04 VPS.
# Usage: sudo bash install.sh [domain]
# For remote one-line installs, set ROOMTONE_REPO to the public Git repository URL.

APP_NAME="roomtone"
APP_DIR="/opt/roomtone"
SERVICE_FILE="/etc/systemd/system/roomtone.service"
REPO_URL="${ROOMTONE_REPO:-https://github.com/RZAsadi/roomtone.git}"
DOMAIN="${1:-}"

if [[ "${EUID}" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    echo "Run this installer as root, or install sudo and curl first." >&2
    exit 1
  fi
  SELF_URL="https://raw.githubusercontent.com/RZAsadi/roomtone/main/install.sh"
  SELF_COPY="$(mktemp)"
  trap 'rm -f "${SELF_COPY}"' EXIT
  curl -fsSL "${SELF_URL}" -o "${SELF_COPY}"
  sudo bash "${SELF_COPY}" "$@"
  exit $?
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this Linux distribution. Ubuntu 22.04 or 24.04 is recommended." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "This installer currently supports Ubuntu 22.04/24.04. Detected: ${PRETTY_NAME:-unknown}" >&2
  exit 1
fi

if [[ -z "${DOMAIN}" ]]; then
  read -r -p "Domain for RoomTone (e.g. voice.example.com): " DOMAIN
fi
DOMAIN="${DOMAIN,,}"
if [[ ! "${DOMAIN}" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
  echo "Invalid domain name: ${DOMAIN}" >&2
  exit 1
fi

echo
echo "RoomTone will be installed at ${APP_DIR} and served at https://${DOMAIN}"
echo "Before continuing, ensure this domain's A/AAAA records point to this VPS."
read -r -p "Continue? [y/N] " CONFIRM
[[ "${CONFIRM}" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv python3-pip caddy coturn

if ! id "${APP_NAME}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_NAME}"
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT
git clone --depth 1 "${REPO_URL}" "${TMP_DIR}/repo"
if [[ ! -f "${TMP_DIR}/repo/app.py" || ! -f "${TMP_DIR}/repo/requirements.txt" ]]; then
  echo "Repository must contain app.py and requirements.txt at its root." >&2
  exit 1
fi

install -d -o "${APP_NAME}" -g "${APP_NAME}" "${APP_DIR}"
cp "${TMP_DIR}/repo/app.py" "${APP_DIR}/app.py"
cp "${TMP_DIR}/repo/requirements.txt" "${APP_DIR}/requirements.txt"
chown "${APP_NAME}:${APP_NAME}" "${APP_DIR}/app.py" "${APP_DIR}/requirements.txt"
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  runuser -u "${APP_NAME}" -- python3 -m venv "${APP_DIR}/venv"
fi
runuser -u "${APP_NAME}" -- "${APP_DIR}/venv/bin/pip" install --upgrade pip
runuser -u "${APP_NAME}" -- "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# Generate a shared secret for time-limited TURN REST credentials.
install -d -m 0750 -o root -g "${APP_NAME}" /etc/roomtone
TURN_SECRET_FILE="/etc/roomtone/turn.env"
if [[ ! -s "${TURN_SECRET_FILE}" ]]; then
  TURN_SECRET="$(openssl rand -hex 32)"
  printf 'ROOMTONE_TURN_DOMAIN=%s\nROOMTONE_TURN_SECRET=%s\n' "${DOMAIN}" "${TURN_SECRET}" > "${TURN_SECRET_FILE}"
  chown root:"${APP_NAME}" "${TURN_SECRET_FILE}"
  chmod 0640 "${TURN_SECRET_FILE}"
fi
TURN_SECRET="$(sed -n 's/^ROOMTONE_TURN_SECRET=//p' "${TURN_SECRET_FILE}" | head -n1)"
if [[ -z "${TURN_SECRET}" ]]; then echo "Could not read TURN secret." >&2; exit 1; fi
printf 'TURNSERVER_ENABLED=1\n' > /etc/default/coturn
cat > /etc/turnserver.conf <<EOF
listening-port=3478
fingerprint
use-auth-secret
static-auth-secret=${TURN_SECRET}
realm=${DOMAIN}
total-quota=100
stale-nonce=600
no-loopback-peers
no-multicast-peers
min-port=49160
max-port=49200
listening-ip=0.0.0.0
EOF

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=RoomTone voice chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_NAME}
Group=${APP_NAME}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=${TURN_SECRET_FILE}
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF

systemctl daemon-reload
systemctl enable --now roomtone
systemctl enable --now coturn
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload-or-restart caddy

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow 3478/tcp
  ufw allow 3478/udp
  ufw allow 49160:49200/udp
fi

echo
echo "RoomTone installation complete."
echo "URL: https://${DOMAIN}"
echo "Service status: systemctl status roomtone"
echo "Logs: journalctl -u roomtone -f"
echo
echo "DNS must point to this VPS, and TCP ports 80/443 must be allowed by the cloud firewall."
echo "Caddy will obtain the HTTPS certificate automatically once public DNS and ports are correct."
echo "TURN relay is configured on ${DOMAIN}:3478 (UDP/TCP), with media relay UDP ports 49160-49200."
echo "Also allow TCP 3478, UDP 3478, and UDP 49160-49200 in your VPS provider firewall/security group."
