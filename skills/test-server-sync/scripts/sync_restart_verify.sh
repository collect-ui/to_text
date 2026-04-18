#!/usr/bin/env bash
set -euo pipefail

HOST="202.140.140.117"
USER_NAME="root"
PASSWORD='Zhangzhi@888'
REMOTE_DIR="/data/to_text"
BRANCH="main"
AUDIO_URL="https://df.qi.work/fs/file/phone/2067/2026/04/44da98e4-6963-4999-b112-14496f336c48.mp3"

SYNC_ONLY=0
VERIFY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --sync-only) SYNC_ONLY=1 ;;
    --verify-only) VERIFY_ONLY=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done
if [[ $SYNC_ONLY -eq 1 && $VERIFY_ONLY -eq 1 ]]; then
  echo "Cannot use --sync-only with --verify-only" >&2
  exit 2
fi

remote_sync_cmd=$(cat <<'CMD'
set -e
cd /data/to_text
git fetch origin
git checkout main
git pull --ff-only origin main
if [[ -f transcribe_config.json ]]; then
python3 - <<'PY'
import json
from pathlib import Path
p=Path('transcribe_config.json')
d=json.loads(p.read_text(encoding='utf-8'))
asr=d.setdefault('asr',{})
asr['default_provider']='tencent'
t=asr.setdefault('tencent',{})
t.setdefault('region','ap-beijing')
t.setdefault('engine_model_type','16k_zh')
t['res_text_format']=3
t.setdefault('convert_num_mode',1)
t['filter_modal']=1
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding='utf-8')
print('patched transcribe_config.json defaults')
PY
fi
if [[ -x ./stop_transcribe_service.sh ]]; then ./stop_transcribe_service.sh || true; fi
if [[ -x ./start_transcribe_service.sh ]]; then ./start_transcribe_service.sh; fi
sleep 2
curl -fsS http://127.0.0.1:8014/health

echo
printf 'REMOTE_HEAD=%s\n' "$(git rev-parse --short HEAD)"
CMD
)

remote_verify_cmd=$(cat <<'CMD'
set -e
cd /data/to_text
curl -fsS http://127.0.0.1:8014/health
curl -fsS -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://df.qi.work/fs/file/phone/2067/2026/04/44da98e4-6963-4999-b112-14496f336c48.mp3",
    "raw": true
  }'
CMD
)

run_remote() {
  local cmd="$1"
  if command -v sshpass >/dev/null 2>&1; then
    sshpass -p "$PASSWORD" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${USER_NAME}@${HOST}" "$cmd"
  else
    echo "sshpass not found. Run manually:" >&2
    echo "ssh ${USER_NAME}@${HOST}" >&2
    echo "$cmd" >&2
    exit 3
  fi
}

if [[ $VERIFY_ONLY -eq 1 ]]; then
  run_remote "$remote_verify_cmd"
  exit 0
fi

if [[ $SYNC_ONLY -eq 1 ]]; then
  run_remote "$remote_sync_cmd"
  exit 0
fi

run_remote "$remote_sync_cmd"
echo
run_remote "$remote_verify_cmd"
