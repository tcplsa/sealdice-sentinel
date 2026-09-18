#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="sealdice-sentinel"
INSTALL_ROOT="/opt/sealdice-sentinel"
CONFIG_ROOT="/etc/sealdice-sentinel"
STATE_ROOT="/var/lib/sealdice-sentinel"
SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_ROOT}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this installer with sudo." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install python3, python3-venv and python3-pip first." >&2
  exit 1
fi

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is required; found ${PYTHON_VERSION}." >&2
  exit 1
fi

PROJECT_VERSION="$(python3 -c 'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])' )"
RELEASE_ROOT="${INSTALL_ROOT}/releases/${PROJECT_VERSION}"

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${INSTALL_ROOT}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_ROOT}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_ROOT}"
install -d -o root -g root -m 0755 "${RELEASE_ROOT}"

python3 -m venv "${RELEASE_ROOT}/venv"
"${RELEASE_ROOT}/venv/bin/python" -m pip install --upgrade pip
"${RELEASE_ROOT}/venv/bin/python" -m pip install "${SCRIPT_ROOT}"
printf '%s\n' "${PROJECT_VERSION}" >"${RELEASE_ROOT}/VERSION"
touch "${RELEASE_ROOT}/.complete"

if [[ -L "${INSTALL_ROOT}/current" ]]; then
  CURRENT_TARGET="$(readlink -f "${INSTALL_ROOT}/current")"
  if [[ "${CURRENT_TARGET}" != "${RELEASE_ROOT}" ]]; then
    ln -sfn "${CURRENT_TARGET}" "${INSTALL_ROOT}/previous"
  fi
fi
ln -sfn "${RELEASE_ROOT}" "${INSTALL_ROOT}/current"

if [[ ! -f "${CONFIG_ROOT}/config.yaml" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${SCRIPT_ROOT}/config.example.yaml" "${CONFIG_ROOT}/config.yaml"
  echo "Created ${CONFIG_ROOT}/config.yaml; edit it before starting the service."
fi

if [[ ! -f "${CONFIG_ROOT}/secrets.env" ]]; then
  install -o root -g root -m 0600 /dev/null "${CONFIG_ROOT}/secrets.env"
  printf '%s\n' \
    'SEALDICE_MONITOR_SMTP_PASSWORD=replace-me' \
    '# SEALDICE_MONITOR_GITHUB_TOKEN=github_pat_replace-me' \
    '# SEALDICE_SENTINEL_WEB_PASSWORD=replace-with-at-least-12-characters' \
    >"${CONFIG_ROOT}/secrets.env"
  echo "Created ${CONFIG_ROOT}/secrets.env; replace the SMTP password."
fi

install -o root -g root -m 0644 "${SCRIPT_ROOT}/systemd/sealdice-sentinel.service" \
  /etc/systemd/system/sealdice-sentinel.service
install -o root -g root -m 0644 "${SCRIPT_ROOT}/systemd/sealdice-sentinel-updater.service" \
  /etc/systemd/system/sealdice-sentinel-updater.service
install -o root -g root -m 0644 "${SCRIPT_ROOT}/systemd/sealdice-sentinel-updater.timer" \
  /etc/systemd/system/sealdice-sentinel-updater.timer

systemctl daemon-reload
systemctl enable sealdice-sentinel.service sealdice-sentinel-updater.timer

echo "Installed SealDice Sentinel ${PROJECT_VERSION}."
echo "Next: edit ${CONFIG_ROOT}/config.yaml and ${CONFIG_ROOT}/secrets.env, then run:"
echo "  sudo systemctl start sealdice-sentinel"
echo "  sudo systemctl start sealdice-sentinel-updater.timer"
