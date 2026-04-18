#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PID_FILE="${PID_FILE:-${SCRIPT_DIR}/transcribe_http_to_text.pid}"

"${PYTHON_BIN}" "${SCRIPT_DIR}/transcribe_http_to_text.py" status --pid-file "${PID_FILE}"
