#!/usr/bin/env bash
# Package Tracker installer for a Debian/Ubuntu Proxmox LXC.
#
# Usage (as root, from the project directory):
#   sudo ./install.sh
#
# Installs into /opt/package-tracker, creates a dedicated system user,
# registers a systemd service, and starts it.

set -euo pipefail

APP_NAME="package-tracker"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_USER="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PORT="${PORT:-8080}"

if [[ $EUID -ne 0 ]]; then
  echo "This installer must be run as root (use sudo)." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates

echo "==> Creating service user ${SERVICE_USER}"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Copying application to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}"
cp -r "${SRC_DIR}/app" "${INSTALL_DIR}/"
cp "${SRC_DIR}/run.py" "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/"
if [[ -d "${SRC_DIR}/scripts" ]]; then
  cp -r "${SRC_DIR}/scripts" "${INSTALL_DIR}/"
fi
if [[ -f "${SRC_DIR}/.env.example" ]]; then
  cp "${SRC_DIR}/.env.example" "${INSTALL_DIR}/.env.example"
fi

# Strip bytecode and caches so we never ship .pyc files built on another
# machine/OS (they are ignored by Python, but they are clutter).
echo "==> Removing cached bytecode"
find "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts" \
  -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts" \
  -type f -name '*.py[co]' -delete 2>/dev/null || true

echo "==> Creating Python virtualenv"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

echo "==> Preparing data directory"
mkdir -p "${INSTALL_DIR}/data"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  echo "==> Creating ${INSTALL_DIR}/.env from the example"
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  sed -i "s/^PORT=.*/PORT=${PORT}/" "${INSTALL_DIR}/.env"
  echo "    -> Edit ${INSTALL_DIR}/.env to set MCP_URL, Telegram credentials, etc."
else
  echo "==> Keeping existing ${INSTALL_DIR}/.env"
fi

echo "==> Setting ownership"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 600 "${INSTALL_DIR}/.env"

echo "==> Writing systemd unit"
cat > "${SERVICE_FILE}" <<UNIT
[Unit]
Description=Package Tracker (shipment tracking with MCP scraping + Telegram alerts)
Documentation=https://github.com/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn app.main:app --host \${HOST} --port \${PORT} --proxy-headers
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/data

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Enabling and starting the service"
systemctl daemon-reload
systemctl enable --now "${APP_NAME}.service"

sleep 2
if systemctl is-active --quiet "${APP_NAME}.service"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "=========================================================="
  echo " Package Tracker is running."
  echo "   URL:     http://${IP:-<lxc-ip>}:${PORT}"
  echo "   Config:  ${INSTALL_DIR}/.env"
  echo "   Logs:    journalctl -u ${APP_NAME} -f"
  echo "   Restart: systemctl restart ${APP_NAME}"
  echo "=========================================================="
else
  echo "Service failed to start. Check: journalctl -u ${APP_NAME} -n 50 --no-pager" >&2
  exit 1
fi
