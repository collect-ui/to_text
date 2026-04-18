#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DOWNLOAD_PADDLE_OCR="${DOWNLOAD_PADDLE_OCR:-1}"

cd "${PROJECT_DIR}"

echo "[1/2] preparing faster-whisper model -> ${PROJECT_DIR}/models/small"
"${PROJECT_DIR}/prepare_faster_whisper_model.sh"

if [ "${DOWNLOAD_PADDLE_OCR}" = "1" ]; then
  echo "[2/2] preloading PaddleOCR runtime model cache (first run only)"
  "${PYTHON_BIN}" - <<'PY'
import tempfile
from pathlib import Path

from PIL import Image
from paddleocr import PaddleOCR

ocr = PaddleOCR(use_angle_cls=True, lang='ch')
with tempfile.TemporaryDirectory() as td:
    img_path = Path(td) / "warmup.png"
    Image.new('RGB', (32, 32), color='white').save(img_path)
    try:
        ocr.predict(str(img_path))
    except Exception:
        # Warmup failure is non-fatal; cache may still be initialized.
        pass
print("paddleocr warmup done")
PY
else
  echo "[skip] DOWNLOAD_PADDLE_OCR=${DOWNLOAD_PADDLE_OCR}, skip paddleocr warmup"
fi

echo "model download completed"
