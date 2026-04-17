#!/usr/bin/env bash
set -euo pipefail

# Deploy bacopy Master (API + /master UI) + HTTPS via Caddy to a VPS.
#
# You run this locally (WSL/Linux/macOS). It will SSH into the VPS and:
#   - Install caddy (if needed)
#   - Upload minimal bacopy API files to /opt/bacopy
#   - Create systemd service bacopy-api (localhost:8010)
#   - Configure Caddy for https://<domain>/master
#
# Prereqs (local):
#   - ssh/scp available
#   - SSH key to the VPS (default: ~/.ssh/laplace_vps)
#
# Prereqs (DNS):
#   - <domain> A-record points to the VPS public IP (proxy OFF while issuing cert)
#
# Usage:
#   bash scripts/deploy_master_to_vps.sh --domain master.example.com --host 1.2.3.4 --user laplace
#
# Secrets:
#   Set env vars (recommended) or enter interactively:
#     BACOPY_API_KEY
#     BACOPY_MASTER_PASSWORD

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DOMAIN=""
VPS_HOST="210.131.215.116"
VPS_USER="laplace"
SSH_KEY="${HOME}/.ssh/laplace_vps"

usage() {
  cat <<EOF
Usage:
  bash scripts/deploy_master_to_vps.sh --domain master.example.com [--host <ip>] [--user <user>] [--ssh-key <path>]

Env (optional):
  BACOPY_API_KEY
  BACOPY_MASTER_PASSWORD
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --host) VPS_HOST="${2:-}"; shift 2 ;;
    --user) VPS_USER="${2:-}"; shift 2 ;;
    --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1"; usage; exit 2 ;;
  esac
done

if [[ -z "${DOMAIN}" ]]; then
  echo "ERROR: --domain is required"
  usage
  exit 2
fi

if [[ ! -f "${SSH_KEY}" ]]; then
  echo "ERROR: SSH key not found: ${SSH_KEY}"
  echo "  Put the key there or pass --ssh-key <path>"
  exit 2
fi

if ! command -v ssh >/dev/null 2>&1 || ! command -v scp >/dev/null 2>&1; then
  echo "ERROR: ssh/scp is required on this machine"
  exit 2
fi

read_secret() {
  local name="$1"
  local cur="${!name:-}"
  if [[ -n "${cur}" ]]; then
    return 0
  fi
  # shellcheck disable=SC2162
  read -r -s -p "Enter ${name}: " cur
  echo
  if [[ -z "${cur}" ]]; then
    echo "ERROR: ${name} is required"
    exit 2
  fi
  export "${name}=${cur}"
}

read_secret "BACOPY_API_KEY"
read_secret "BACOPY_MASTER_PASSWORD"

SSH_BASE=(ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15 "${VPS_USER}@${VPS_HOST}")
SCP_BASE=(scp -i "${SSH_KEY}" -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15)

echo "[1/6] SSH connectivity check..."
if ! "${SSH_BASE[@]}" "echo OK" >/dev/null 2>&1; then
  echo "ERROR: SSH failed. Check key/user/host, and that port 22 is reachable."
  echo "Try manually: ssh -i \"${SSH_KEY}\" ${VPS_USER}@${VPS_HOST}"
  exit 2
fi

echo "[2/6] Detect OS and check ports 80/443..."
OS_ID="$("${SSH_BASE[@]}" 'set -e; . /etc/os-release; echo "${ID:-}"' 2>/dev/null || true)"
if [[ -z "${OS_ID}" ]]; then
  echo "ERROR: failed to detect OS via /etc/os-release"
  exit 2
fi
echo "  OS_ID=${OS_ID}"

PORTS="$("${SSH_BASE[@]}" "sudo ss -lntp | egrep ':80|:443' || true" || true)"
if [[ -n "${PORTS}" ]]; then
  echo "ERROR: ports 80/443 are already in use on the VPS:"
  echo "${PORTS}"
  echo
  echo "Caddy cannot bind 80/443 in this state."
  echo "Stop the existing web server or switch to an Nginx-based setup."
  exit 2
fi

echo "[3/6] Upload bacopy API files to /opt/bacopy..."
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

cp "${ROOT_DIR}/bacopy_api.py" "${TMP_DIR}/bacopy_api.py"
cp "${ROOT_DIR}/bacopy_db.py" "${TMP_DIR}/bacopy_db.py"
cp "${ROOT_DIR}/decision_logger.py" "${TMP_DIR}/decision_logger.py"
cp "${ROOT_DIR}/snapshot_store.py" "${TMP_DIR}/snapshot_store.py"

"${SSH_BASE[@]}" "sudo mkdir -p /opt/bacopy /opt/bacopy/data /etc/bacopy"
"${SCP_BASE[@]}" "${TMP_DIR}/"*.py "${VPS_USER}@${VPS_HOST}:/tmp/"
"${SSH_BASE[@]}" "sudo mv /tmp/bacopy_api.py /tmp/bacopy_db.py /tmp/decision_logger.py /tmp/snapshot_store.py /opt/bacopy/"
"${SSH_BASE[@]}" "sudo chown -R ${VPS_USER}:${VPS_USER} /opt/bacopy"

echo "[4/6] Create env + systemd service..."
ENV_TMP="${TMP_DIR}/bacopy.env"
cat >"${ENV_TMP}" <<EOF
BACOPY_API_KEY=${BACOPY_API_KEY}
BACOPY_MASTER_PASSWORD=${BACOPY_MASTER_PASSWORD}
BACOPY_COOKIE_SECURE=1
BACOPY_DB_PATH=/opt/bacopy/data/bacopy.sqlite3
EOF
"${SCP_BASE[@]}" "${ENV_TMP}" "${VPS_USER}@${VPS_HOST}:/tmp/bacopy.env"
"${SSH_BASE[@]}" "sudo mv /tmp/bacopy.env /etc/bacopy/bacopy.env && sudo chmod 600 /etc/bacopy/bacopy.env"

SERVICE_TMP="${TMP_DIR}/bacopy-api.service"
cat >"${SERVICE_TMP}" <<EOF
[Unit]
Description=bacopy master API (/master UI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${VPS_USER}
WorkingDirectory=/opt/bacopy
EnvironmentFile=/etc/bacopy/bacopy.env
ExecStart=/usr/bin/python3 /opt/bacopy/bacopy_api.py --host 127.0.0.1 --port 8010
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

"${SCP_BASE[@]}" "${SERVICE_TMP}" "${VPS_USER}@${VPS_HOST}:/tmp/bacopy-api.service"
"${SSH_BASE[@]}" "sudo mv /tmp/bacopy-api.service /etc/systemd/system/bacopy-api.service"
"${SSH_BASE[@]}" "sudo systemctl daemon-reload && sudo systemctl enable --now bacopy-api"

echo "[5/6] Install and configure Caddy (HTTPS)..."
case "${OS_ID}" in
  ubuntu|debian)
    "${SSH_BASE[@]}" "sudo apt-get update -y"
    "${SSH_BASE[@]}" "sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl"
    "${SSH_BASE[@]}" "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    "${SSH_BASE[@]}" "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null"
    "${SSH_BASE[@]}" "sudo apt-get update -y && sudo apt-get install -y caddy"
    ;;
  centos|rhel|almalinux|rocky|fedora)
    "${SSH_BASE[@]}" "sudo yum install -y yum-utils curl || sudo dnf install -y dnf-plugins-core curl"
    "${SSH_BASE[@]}" "sudo yum-config-manager --add-repo https://dl.cloudsmith.io/public/caddy/stable/rpm.repo || sudo dnf config-manager --add-repo https://dl.cloudsmith.io/public/caddy/stable/rpm.repo"
    "${SSH_BASE[@]}" "sudo yum install -y caddy || sudo dnf install -y caddy"
    ;;
  *)
    echo "ERROR: unsupported OS_ID for auto-install: ${OS_ID}"
    echo "Install caddy manually, then set /etc/caddy/Caddyfile with reverse_proxy to 127.0.0.1:8010"
    exit 2
    ;;
esac

CADDY_TMP="${TMP_DIR}/Caddyfile"
cat >"${CADDY_TMP}" <<EOF
${DOMAIN} {
  reverse_proxy 127.0.0.1:8010
}
EOF
"${SCP_BASE[@]}" "${CADDY_TMP}" "${VPS_USER}@${VPS_HOST}:/tmp/Caddyfile"
"${SSH_BASE[@]}" "sudo mv /tmp/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy"

echo "[6/6] Done."
echo
echo "Master URL:"
echo "  https://${DOMAIN}/master"
echo
echo "Notes:"
echo "  - Cloudflare proxy (orange cloud) should be OFF until the first certificate is issued."
echo "  - If you turn Cloudflare proxy ON later, set SSL/TLS mode to Full (strict)."
