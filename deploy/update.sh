#!/usr/bin/env bash
# Update an existing native install of the Leapmotor -> ABRP bridge.
# Pulls the latest code, re-syncs dependencies and the systemd unit, and
# restarts the service. Idempotent: a no-op when already up to date.
#
#   sudo /opt/leapmotor-abrp-sync/deploy/update.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/leapmotor-abrp-sync}"
SERVICE="leapmotor-abrp.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "No install found at ${INSTALL_DIR}. Run deploy/install.sh first." >&2
  exit 1
fi

# Run git/pip as whoever owns the checkout: as root when the code is
# root-owned (the recommended least-privilege layout), or as the owning user
# otherwise -- which also avoids git's "dubious ownership" error.
REPO_OWNER="$(stat -c '%U' "${INSTALL_DIR}/.git")"
as_owner() {
  if [ "${REPO_OWNER}" = "root" ]; then
    "$@"
  else
    sudo -u "${REPO_OWNER}" "$@"
  fi
}

echo ">> Checking for updates in ${INSTALL_DIR} (owner: ${REPO_OWNER})..."
before="$(as_owner git -C "${INSTALL_DIR}" rev-parse HEAD)"
as_owner git -C "${INSTALL_DIR}" pull --ff-only
after="$(as_owner git -C "${INSTALL_DIR}" rev-parse HEAD)"

if [ "${before}" = "${after}" ]; then
  echo ">> Already up to date (${after:0:7}); nothing to do."
  exit 0
fi
echo ">> Updated ${before:0:7} -> ${after:0:7}"

echo ">> Syncing Python dependencies..."
as_owner "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

# Reinstall the unit only if it changed in the repo.
if ! cmp -s "${INSTALL_DIR}/deploy/${SERVICE}" "/etc/systemd/system/${SERVICE}"; then
  echo ">> Updating systemd unit..."
  install -m 644 "${INSTALL_DIR}/deploy/${SERVICE}" "/etc/systemd/system/${SERVICE}"
  systemctl daemon-reload
fi

echo ">> Restarting service..."
systemctl restart "${SERVICE}"
sleep 2
systemctl --no-pager --lines=5 status "${SERVICE}" || true
echo ">> Done. Follow logs: journalctl -u ${SERVICE} -f"
