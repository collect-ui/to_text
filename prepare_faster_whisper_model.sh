#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

HF_CACHE_SOURCE="${HF_CACHE_SOURCE:-/home/zz/.cache/huggingface/hub}"
MODEL_ID="${MODEL_ID:-Systran/faster-whisper-small}"
MODEL_DIR="${MODEL_DIR:-${SCRIPT_DIR}/models/small}"
COMMIT_FILE="${HF_CACHE_SOURCE}/models--Systran--faster-whisper-small/refs/main"

mkdir -p "${SCRIPT_DIR}/models"

if [ -d "${MODEL_DIR}" ]; then
  shopt -s nullglob
  files=("${MODEL_DIR}"/*)
  shopt -u nullglob
  if [ "${#files[@]}" -gt 0 ]; then
    echo "model already exists: ${MODEL_DIR}"
    exit 0
  fi
fi

SOURCE_DIR="${HF_CACHE_SOURCE}/models--Systran--faster-whisper-small"
if [ -f "${COMMIT_FILE}" ]; then
  read -r COMMIT < "${COMMIT_FILE}"
  SNAPSHOT_DIR="${SOURCE_DIR}/snapshots/${COMMIT}"
  if [ -d "${SNAPSHOT_DIR}" ]; then
    cp -a "${SNAPSHOT_DIR}" "${MODEL_DIR}"
    echo "copied model from cache: ${SNAPSHOT_DIR} -> ${MODEL_DIR}"
    exit 0
  fi
fi

echo "cache snapshot not found, try download model into ${MODEL_DIR}"
MODEL_DIR_ENV="${MODEL_DIR}" MODEL_ID_ENV="${MODEL_ID}" python3 - <<'PY'
from pathlib import Path
import os
from huggingface_hub import snapshot_download

model_dir = Path(os.environ["MODEL_DIR_ENV"])
model_id = os.environ["MODEL_ID_ENV"]
model_dir.parent.mkdir(parents=True, exist_ok=True)

snapshot_download(
    repo_id=model_id,
    local_dir=str(model_dir),
    local_dir_use_symlinks=False,
)
print(f"model prepared: {model_dir}")
PY
