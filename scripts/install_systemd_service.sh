#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-to-text}"
RUN_USER="${RUN_USER:-$(id -un)}"
RUN_GROUP="${RUN_GROUP:-$(id -gn)}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8014}"
MODEL="${MODEL:-small}"
MODEL_DIR="${MODEL_DIR:-${PROJECT_DIR}}"
DEVICE="${DEVICE:-cpu}"
COMPUTE_TYPE="${COMPUTE_TYPE:-int8}"
LANGUAGE="${LANGUAGE:-zh}"
IMAGE_OCR_PROVIDER="${IMAGE_OCR_PROVIDER:-auto}"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

run_root_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "need root/sudo: $*"
    return 1
  fi
}

TMP_FILE="$(mktemp)"
cat > "${TMP_FILE}" <<SERVICE
[Unit]
Description=to_text transcribe service
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=DISABLE_MODEL_SOURCE_CHECK=True
Environment=PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/transcribe_http_to_text.py serve --host ${HOST} --port ${PORT} --model ${MODEL} --model-dir ${MODEL_DIR} --device ${DEVICE} --compute-type ${COMPUTE_TYPE} --language ${LANGUAGE} --image-ocr-provider ${IMAGE_OCR_PROVIDER}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

run_root_cmd cp "${TMP_FILE}" "${SERVICE_FILE}"
run_root_cmd systemctl daemon-reload
run_root_cmd systemctl enable --now "${SERVICE_NAME}"
rm -f "${TMP_FILE}"

echo "installed and started: ${SERVICE_NAME}"
echo "status command: systemctl status ${SERVICE_NAME}"
