#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

SERVICE_SCRIPT="${SCRIPT_DIR}/transcribe_http_to_text.py"
PID_FILE="${PID_FILE:-${SCRIPT_DIR}/transcribe_http_to_text.pid}"
LOG_FILE="${LOG_FILE:-${SCRIPT_DIR}/transcribe_http_to_text.log}"
MODEL_DIR="${MODEL_DIR:-${SCRIPT_DIR}}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8014}"
MODEL="${MODEL:-small}"
LANGUAGE="${LANGUAGE:-zh}"
DEVICE="${DEVICE:-cpu}"
COMPUTE_TYPE="${COMPUTE_TYPE:-int8}"
IMAGE_OCR_PROVIDER="${IMAGE_OCR_PROVIDER:-tencent}"

export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"
export DISABLE_MODEL_SOURCE_CHECK="${DISABLE_MODEL_SOURCE_CHECK:-True}"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="${PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK:-True}"

"${PYTHON_BIN}" "${SERVICE_SCRIPT}" \
  start \
  --host "${HOST}" \
  --port "${PORT}" \
  --model "${MODEL}" \
  --model-dir "${MODEL_DIR}" \
  --device "${DEVICE}" \
  --compute-type "${COMPUTE_TYPE}" \
  --language "${LANGUAGE}" \
  --image-ocr-provider "${IMAGE_OCR_PROVIDER}" \
  --pid-file "${PID_FILE}" \
  --log-file "${LOG_FILE}" "$@"
