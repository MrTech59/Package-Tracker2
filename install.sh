#!/usr/bin/env bash
# Package Tracker installer / updater for a Debian/Ubuntu Proxmox LXC.
#
# Usage (as root, from the project directory):
#   sudo ./install.sh              Fresh install (or re-install in place)
#   sudo ./install.sh --update     Update an existing install, preserving data
#   sudo ./install.sh --uninstall  Stop and remove the service (keeps data)
#   sudo ./install.sh --help
#
# Both install and update are safe to re-run. An update keeps your database
# (packages, events) and your .env configuration untouched, and rolls back the
# code if the new version fails to start.

set -euo pipefail

APP_NAME="package-tracker"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_USER="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PORT="${PORT:-8080}"

BACKUP_ROOT="/var/backups/${APP_NAME}"

# ------------------------------------------------------------------- helpers

c_reset=$'\033[0m'; c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_info=$'\033[36m'

log()  { echo "${c_info}==>${c_reset} $*"; }
ok()   { echo "    ${c_ok}OK${c_reset}   $*"; }
warn() { echo "    ${c_warn}WARN${c_reset} $*"; }
die()  { echo "${c_err}ERROR:${c_reset} $*" >&2; exit 1; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# --------------------------------------------------------------- arg parsing

MODE="install"
case "${1:-}" in
  ""|--install)    MODE="install" ;;
  -u|--update)     MODE="update" ;;
  --uninstall)     MODE="uninstall" ;;
  -h|--help)       usage ;;
  *) die "Unknown option: $1 (try --help)" ;;
esac

if [[ $EUID -ne 0 ]]; then
  die "Run as root, e.g. sudo ./install.sh ${1:-}"
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------- uninstall

if [[ "$MODE" == "uninstall" ]]; then
  log "Stopping and disabling ${APP_NAME}"
  systemctl disable --now "${APP_NAME}.service" 2>/dev/null || true
  rm -f "${SERVICE_FILE}"
  systemctl daemon-reload 2>/dev/null || true
  echo
  echo "Service removed. Your data and config were left in place:"
  echo "  ${INSTALL_DIR}/data   (database)"
  echo "  ${INSTALL_DIR}/.env   (configuration)"
  echo "Delete ${INSTALL_DIR} yourself if you want it gone."
  exit 0
fi

# =============================================================== install/update

IS_UPDATE=0
if [[ "$MODE" == "update" ]]; then
  [[ -d "${INSTALL_DIR}" ]] || die "Nothing to update: ${INSTALL_DIR} does not exist. Run a fresh install first."
  [[ -f "${INSTALL_DIR}/.env" ]] || warn "No ${INSTALL_DIR}/.env found; one will be created from the example."
  IS_UPDATE=1
  log "Updating existing installation in ${INSTALL_DIR}"
  echo "    data/ and .env will be preserved."
elif [[ -d "${INSTALL_DIR}" && -f "${INSTALL_DIR}/.env" ]]; then
  warn "${INSTALL_DIR} already exists - updating in place (data and .env preserved)."
  warn "Use --update explicitly next time, or --uninstall to remove."
  IS_UPDATE=1
fi

# 1 --------------------------------------------------------- system packages
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates

# 2 ------------------------------------------------------------ service user
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  log "Creating service user ${SERVICE_USER}"
  useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
else
  ok "Service user ${SERVICE_USER} already exists"
fi

# 3 --------------------------------------------------- stop service, snapshot
if [[ "$IS_UPDATE" == "1" ]]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="${BACKUP_ROOT}/${STAMP}"

  log "Stopping service"
  systemctl stop "${APP_NAME}.service" 2>/dev/null || true

  log "Backing up current version to ${BACKUP_DIR}"
  mkdir -p "${BACKUP_DIR}"

  # Database: use SQLite's own online backup so we get a consistent copy.
  if [[ -f "${INSTALL_DIR}/data/tracker.db" ]]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "${INSTALL_DIR}/data/tracker.db" ".backup '${BACKUP_DIR}/tracker.db'" \
        && ok "Backed up database (sqlite3 .backup)"
    else
      cp "${INSTALL_DIR}/data/tracker.db" "${BACKUP_DIR}/tracker.db"
      # WAL/SHM must travel with the db for a consistent restore.
      [[ -f "${INSTALL_DIR}/data/tracker.db-wal" ]] && cp "${INSTALL_DIR}/data/tracker.db-wal" "${BACKUP_DIR}/" || true
      [[ -f "${INSTALL_DIR}/data/tracker.db-shm" ]] && cp "${INSTALL_DIR}/data/tracker.db-shm" "${BACKUP_DIR}/" || true
      ok "Backed up database (file copy; install sqlite3 for a safer backup)"
    fi

    DB_COUNT="$(sqlite3 "${INSTALL_DIR}/data/tracker.db" 'SELECT count(*) FROM packages;' 2>/dev/null || echo '?')"
    echo "    Packages currently tracked: ${DB_COUNT}"
  else
    ok "No database yet - nothing to back up"
  fi

  # Config.
  [[ -f "${INSTALL_DIR}/.env" ]] && cp "${INSTALL_DIR}/.env" "${BACKUP_DIR}/.env"

  # Code snapshot, so we can roll back if the new version is broken.
  mkdir -p "${BACKUP_DIR}/code"
  for item in app run.py requirements.txt scripts; do
    [[ -e "${INSTALL_DIR}/${item}" ]] && cp -r "${INSTALL_DIR}/${item}" "${BACKUP_DIR}/code/" || true
  done
  ok "Backed up application code"

  # Keep only the 5 most recent backups.
  ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf
fi

# 4 ------------------------------------------------------------- copy code
log "Copying application to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}"

# Replace only the code, never data/ or .env.
rm -rf "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts"
cp -r "${SRC_DIR}/app" "${INSTALL_DIR}/"
cp "${SRC_DIR}/run.py" "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/"
if [[ -d "${SRC_DIR}/scripts" ]]; then
  cp -r "${SRC_DIR}/scripts" "${INSTALL_DIR}/"
fi
if [[ -f "${SRC_DIR}/.env.example" ]]; then
  cp "${SRC_DIR}/.env.example" "${INSTALL_DIR}/.env.example"
fi
if [[ -f "${SRC_DIR}/README.md" ]]; then
  cp "${SRC_DIR}/README.md" "${INSTALL_DIR}/"
fi
ok "Code copied (data/ and .env untouched)"

# Strip bytecode so we never ship .pyc built on another machine/OS.
find "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts" \
  -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts" \
  -type f -name '*.py[co]' -delete 2>/dev/null || true

# 5 ----------------------------------------------------------- virtualenv
log "Updating Python dependencies"
if [[ -d "${INSTALL_DIR}/venv" ]]; then
  "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
  "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade -r "${INSTALL_DIR}/requirements.txt"
else
  python3 -m venv "${INSTALL_DIR}/venv"
  "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
  "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
fi
ok "Dependencies installed"

# 6 ------------------------------------------------------------ data + env
mkdir -p "${INSTALL_DIR}/data"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  log "Creating ${INSTALL_DIR}/.env from the example"
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  sed -i "s/^PORT=.*/PORT=${PORT}/" "${INSTALL_DIR}/.env"
  echo "    -> Edit ${INSTALL_DIR}/.env to set MCP_URL, Telegram credentials, etc."
else
  ok "Keeping existing .env"
fi

log "Setting ownership"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 600 "${INSTALL_DIR}/.env"

# 7 --------------------------------------------------------- systemd unit
log "Writing systemd unit"
cat > "${SERVICE_FILE}" <<UNIT
[Unit]
Description=Package Tracker (shipment tracking with MCP scraping + Telegram alerts)
Documentation=https://github.com/MrTech59/Package-Tracker2
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

# 8 ------------------------------------------------------------ start + verify
systemctl daemon-reload
systemctl enable "${APP_NAME}.service" >/dev/null 2>&1 || true

log "Starting service"
systemctl restart "${APP_NAME}.service"

# Poll the health endpoint so we know the app really came up, not just systemd.
HEALTH_PORT="$(grep -E '^PORT=' "${INSTALL_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
HEALTH_PORT="${HEALTH_PORT:-$PORT}"
HEALTHY=0
for i in $(seq 1 30); do
  if systemctl is-active --quiet "${APP_NAME}.service" \
     && curl -fsS --max-time 2 "http://127.0.0.1:${HEALTH_PORT}/api/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done

if [[ "$HEALTHY" == "1" ]]; then
  ok "Health check passed on port ${HEALTH_PORT}"
else
  warn "Health check did not pass"
  systemctl status "${APP_NAME}.service" --no-pager -n 15 || true

  if [[ "$IS_UPDATE" == "1" && -d "${BACKUP_DIR}/code" ]]; then
    echo
    warn "Rolling back to the previous version"
    systemctl stop "${APP_NAME}.service" 2>/dev/null || true

    rm -rf "${INSTALL_DIR}/app" "${INSTALL_DIR}/scripts"
    for item in app scripts; do
      [[ -d "${BACKUP_DIR}/code/${item}" ]] && cp -r "${BACKUP_DIR}/code/${item}" "${INSTALL_DIR}/" || true
    done
    for item in run.py requirements.txt; do
      [[ -f "${BACKUP_DIR}/code/${item}" ]] && cp "${BACKUP_DIR}/code/${item}" "${INSTALL_DIR}/" || true
    done

    if [[ -f "${BACKUP_DIR}/tracker.db" ]]; then
      cp "${BACKUP_DIR}/tracker.db" "${INSTALL_DIR}/data/tracker.db"
      cp "${BACKUP_DIR}/tracker.db-wal" "${INSTALL_DIR}/data/" 2>/dev/null || true
      cp "${BACKUP_DIR}/tracker.db-shm" "${INSTALL_DIR}/data/" 2>/dev/null || true
      ok "Restored database from backup"
    fi

    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt" || true
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
    systemctl restart "${APP_NAME}.service"

    echo
    if systemctl is-active --quiet "${APP_NAME}.service"; then
      warn "Rolled back. The previous version is running again."
      warn "Your data is intact (recovered from ${BACKUP_DIR})."
    else
      die "Rollback also failed. Check: journalctl -u ${APP_NAME} -n 50 --no-pager"
    fi
    exit 1
  else
    die "Service failed to start. Check: journalctl -u ${APP_NAME} -n 50 --no-pager"
  fi
fi

# 9 ------------------------------------------------------------- summary
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "=========================================================="
if [[ "$IS_UPDATE" == "1" ]]; then
  echo " Package Tracker updated successfully."
  echo "   Backup:  ${BACKUP_DIR}"
else
  echo " Package Tracker installed and running."
fi
echo "   URL:     http://${IP:-<lxc-ip>}:${HEALTH_PORT}"
echo "   Config:  ${INSTALL_DIR}/.env"
echo "   Logs:    journalctl -u ${APP_NAME} -f"
echo "   Update:  sudo ./install.sh --update"
echo "   Restart: systemctl restart ${APP_NAME}"
echo "=========================================================="
