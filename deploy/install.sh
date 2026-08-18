#!/usr/bin/env bash
# Native installer for the Leapmotor -> ABRP bridge.
# Target: Debian-based systems (DietPi, Raspberry Pi OS). Run as root.
#
#   sudo REPO_URL=https://github.com/<you>/leapmotor-abrp-sync bash deploy/install.sh
#
# Idempotent: safe to re-run to update the code and dependencies.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/frhumanes/leapmotor-abrp-sync}"
INSTALL_DIR="${INSTALL_DIR:-/opt/leapmotor-abrp-sync}"
ENV_FILE="${ENV_FILE:-/etc/leapmotor-abrp.env}"
SERVICE_USER="${SERVICE_USER:-leapmotor}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo ">> Installing system dependencies (git, python venv, curl, openssl)..."
apt-get update
apt-get install -y git python3-venv curl openssl

echo ">> Fetching source into ${INSTALL_DIR}..."
if [ -d "${INSTALL_DIR}/.git" ]; then
  # -c safe.directory avoids git's "dubious ownership" error when an older
  # install left the tree owned by the service user; ownership is normalized
  # to root further down.
  git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

echo ">> Creating / updating virtualenv..."
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo ">> Creating service user '${SERVICE_USER}'..."
id -u "${SERVICE_USER}" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"

# Least privilege: the code and venv stay root-owned (the service cannot
# rewrite its own code). Only the certificate cache -- the one path written at
# runtime -- is owned by the service user. Re-running normalizes ownership if
# an older install had chown'd the whole tree.
chown -R root:root "${INSTALL_DIR}"
CERT_DIR="${INSTALL_DIR}/custom_components/leapmotor"
mkdir -p "${CERT_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${CERT_DIR}"

echo ">> Downloading app certificates (one time)..."
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/python" - <<PY
import sys
sys.path.insert(0, "${INSTALL_DIR}")
from leapmotor_to_abrp import ensure_certs
ensure_certs()
print("certificates ready")
PY

NEEDS_CREDS=0
if [ ! -f "${ENV_FILE}" ]; then
  echo ">> Creating credentials file ${ENV_FILE} (you must edit it)..."
  cat > "${ENV_FILE}" <<'ENV'
LEAPMOTOR_USERNAME=change-me@example.com
LEAPMOTOR_PASSWORD=change-me
ABRP_TOKEN=change-me
ENV
  chown "${SERVICE_USER}:${SERVICE_USER}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  NEEDS_CREDS=1
fi

echo ">> Installing systemd units..."
install -m 644 "${INSTALL_DIR}/deploy/leapmotor-abrp.service" \
  /etc/systemd/system/leapmotor-abrp.service
# Auto-update units are installed but left disabled (opt-in).
install -m 644 "${INSTALL_DIR}/deploy/leapmotor-abrp-update.service" \
  /etc/systemd/system/leapmotor-abrp-update.service
install -m 644 "${INSTALL_DIR}/deploy/leapmotor-abrp-update.timer" \
  /etc/systemd/system/leapmotor-abrp-update.timer
systemctl daemon-reload

if [ "${NEEDS_CREDS}" = "1" ]; then
  cat <<MSG

==> Almost done. Edit your credentials, then start the service:

      sudo nano ${ENV_FILE}
      sudo systemctl enable --now leapmotor-abrp.service
      journalctl -u leapmotor-abrp -f
MSG
else
  systemctl enable --now leapmotor-abrp.service
  echo ">> Service (re)started. Follow logs: journalctl -u leapmotor-abrp -f"
fi
