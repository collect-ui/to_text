#!/usr/bin/env bash

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8014}"
AUDIO_URL="${AUDIO_URL:-}"
IMAGE_URL="${IMAGE_URL:-}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required"
  exit 1
fi

echo "[1/3] health"
curl -fsS "${BASE_URL}/health" | sed 's/.*/  &/'

echo "[2/3] transcribe (wrapped response)"
if [ -n "${AUDIO_URL}" ]; then
  curl -fsS -X POST "${BASE_URL}/transcribe" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"${AUDIO_URL}\"}" | sed 's/.*/  &/'
else
  echo "  skip: set AUDIO_URL to run this test"
fi

echo "[3/3] ocr (raw response)"
if [ -n "${IMAGE_URL}" ]; then
  curl -fsS -X POST "${BASE_URL}/ocr" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"${IMAGE_URL}\",\"raw\":true}" | sed 's/.*/  &/'
else
  echo "  skip: set IMAGE_URL to run this test"
fi
