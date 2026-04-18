#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
INSTALL_PADDLE_OCR="${INSTALL_PADDLE_OCR:-1}"
START_AFTER_INSTALL="${START_AFTER_INSTALL:-1}"

run_root_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "need root/sudo to install system packages: $*"
    return 1
  fi
}

install_system_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    run_root_cmd apt-get update
    run_root_cmd apt-get install -y \
      python3 python3-venv python3-pip \
      ffmpeg curl tesseract-ocr tesseract-ocr-chi-sim
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    run_root_cmd dnf install -y \
      python3 python3-pip \
      ffmpeg curl tesseract tesseract-langpack-chi_sim
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    run_root_cmd yum install -y \
      python3 python3-pip \
      ffmpeg curl tesseract tesseract-langpack-chi_sim || true
    return 0
  fi

  echo "skip system package install: unsupported package manager"
}

setup_venv() {
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"

  if [ -n "${PIP_INDEX_URL}" ]; then
    pip install -U pip setuptools wheel -i "${PIP_INDEX_URL}"
    pip install -r "${PROJECT_DIR}/requirements.txt" -i "${PIP_INDEX_URL}"
  else
    pip install -U pip setuptools wheel
    pip install -r "${PROJECT_DIR}/requirements.txt"
  fi

  if [ "${INSTALL_PADDLE_OCR}" = "1" ]; then
    if [ -n "${PIP_INDEX_URL}" ]; then
      pip install paddlepaddle -i "${PIP_INDEX_URL}" || true
    else
      pip install paddlepaddle || true
    fi
  fi
}

download_models() {
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
  "${PROJECT_DIR}/scripts/download_models.sh"
}

start_service() {
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
  cd "${PROJECT_DIR}"
  ./start_transcribe_service.sh
}

main() {
  cd "${PROJECT_DIR}"
  install_system_packages
  setup_venv
  download_models

  if [ "${START_AFTER_INSTALL}" = "1" ]; then
    start_service
    echo "service started"
  else
    echo "install finished, service not started (START_AFTER_INSTALL=${START_AFTER_INSTALL})"
  fi

  cat <<MSG

Done.
- venv: ${VENV_DIR}
- model dir: ${PROJECT_DIR}/models/small
- log file: ${PROJECT_DIR}/transcribe_http_to_text.log
- pid file: ${PROJECT_DIR}/transcribe_http_to_text.pid
MSG
}

main "$@"
