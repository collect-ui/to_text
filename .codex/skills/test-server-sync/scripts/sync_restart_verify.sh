#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
HOST="${TEST_SERVER_HOST:-202.140.140.117}"
USER_NAME="${TEST_SERVER_USER:-root}"
PASSWORD="${TEST_SERVER_PASSWORD:-}"
REMOTE_DIR="${TEST_SERVER_REMOTE_DIR:-/data/to_text}"
BRANCH="${TEST_SERVER_BRANCH:-main}"
AUDIO_URL="${TEST_SERVER_AUDIO_URL:-https://df.qi.work/fs/file/phone/2067/2026/04/44da98e4-6963-4999-b112-14496f336c48.mp3}"
LOCAL_SOURCE_DIR="${TEST_SERVER_LOCAL_SOURCE_DIR:-/data/project/to_text}"
PUBLIC_BASE_URL="${TEST_SERVER_BASE_URL:-http://collect-ui.top:8014}"
LOCAL_COMPARE_PORT="${TEST_SERVER_LOCAL_COMPARE_PORT:-18014}"
PLAYWRIGHT_RUNNER_DIR="${TEST_SERVER_PLAYWRIGHT_RUNNER_DIR:-/tmp/test-server-sync-playwright}"

if [[ -z "${PASSWORD}" ]]; then
  echo "Missing required env var: TEST_SERVER_PASSWORD" >&2
  echo "Set TEST_SERVER_PASSWORD before running this sync script." >&2
  exit 2
fi

if [[ ! -d "${LOCAL_SOURCE_DIR}" ]]; then
  echo "Local source dir not found: ${LOCAL_SOURCE_DIR}" >&2
  exit 2
fi

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
cd "__REMOTE_DIR__"
CONFIG_BACKUP=""
if [[ -f transcribe_config.json ]]; then
  CONFIG_BACKUP="$(mktemp)"
  cp transcribe_config.json "${CONFIG_BACKUP}"
fi
git fetch origin
git checkout "__BRANCH__"
git reset --hard "origin/__BRANCH__"
if [[ -n "${CONFIG_BACKUP}" && -f "${CONFIG_BACKUP}" ]]; then
  cp "${CONFIG_BACKUP}" transcribe_config.json
  rm -f "${CONFIG_BACKUP}"
fi
if [[ ! -f transcribe_config.json && -f transcribe_config.template.json ]]; then
  cp transcribe_config.template.json transcribe_config.json
fi
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
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8014/health >/tmp/to_text_remote_health.json 2>/dev/null; then
    cat /tmp/to_text_remote_health.json
    rm -f /tmp/to_text_remote_health.json
    break
  fi
  sleep 2
done
if ! curl -fsS http://127.0.0.1:8014/health >/tmp/to_text_remote_health.json 2>/dev/null; then
  echo "remote health check failed after retries" >&2
  exit 1
fi
cat /tmp/to_text_remote_health.json
rm -f /tmp/to_text_remote_health.json

echo
printf 'REMOTE_HEAD=%s\n' "$(git rev-parse --short HEAD)"
CMD
)
remote_sync_cmd=${remote_sync_cmd//__REMOTE_DIR__/${REMOTE_DIR}}
remote_sync_cmd=${remote_sync_cmd//__BRANCH__/${BRANCH}}

remote_verify_cmd=$(cat <<'CMD'
set -e
cd "__REMOTE_DIR__"
curl -fsS http://127.0.0.1:8014/health
curl -fsS -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "__AUDIO_URL__",
    "raw": true
  }'
CMD
)
remote_verify_cmd=${remote_verify_cmd//__REMOTE_DIR__/${REMOTE_DIR}}
remote_verify_cmd=${remote_verify_cmd//__AUDIO_URL__/${AUDIO_URL}}

run_remote() {
  local cmd="$1"
  if command -v sshpass >/dev/null 2>&1; then
    SSHPASS="$PASSWORD" sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${USER_NAME}@${HOST}" "$cmd"
  else
    echo "sshpass not found. Run manually:" >&2
    echo "ssh ${USER_NAME}@${HOST}" >&2
    echo "$cmd" >&2
    exit 3
  fi
}

assert_remote_head() {
  local remote_head
  remote_head="$(run_remote "cd '${REMOTE_DIR}' && git rev-parse --short HEAD" | tail -n 1 | tr -d '\r')"
  echo "REMOTE_HEAD=${remote_head}"
  if [[ "${remote_head}" != "${EXPECTED_HEAD}" ]]; then
    echo "Remote HEAD mismatch: expected ${EXPECTED_HEAD}, got ${remote_head}" >&2
    exit 1
  fi
}

assert_remote_html_matches_local() {
  local local_sha remote_sha
  local_sha="$(sha256sum "${LOCAL_SOURCE_DIR}/index.html" | awk '{print $1}')"
  remote_sha="$(curl -fsS "${PUBLIC_BASE_URL}" | sha256sum | awk '{print $1}')"
  echo "LOCAL_INDEX_SHA=${local_sha}"
  echo "REMOTE_INDEX_SHA=${remote_sha}"
  if [[ "${local_sha}" != "${remote_sha}" ]]; then
    echo "Remote root HTML differs from local index.html" >&2
    exit 1
  fi
}

ensure_playwright_runner() {
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "node and npm are required for headless page verification" >&2
    exit 4
  fi
  mkdir -p "${PLAYWRIGHT_RUNNER_DIR}"
  if [[ ! -f "${PLAYWRIGHT_RUNNER_DIR}/package.json" ]]; then
    (
      cd "${PLAYWRIGHT_RUNNER_DIR}"
      npm init -y >/dev/null 2>&1
    )
  fi
  if [[ ! -f "${PLAYWRIGHT_RUNNER_DIR}/node_modules/playwright/package.json" ]]; then
    (
      cd "${PLAYWRIGHT_RUNNER_DIR}"
      npm install playwright >/dev/null 2>&1
    )
  fi
}

start_local_compare_server() {
  local port="$1"
  python3 - <<'PY' "$LOCAL_SOURCE_DIR" "$port" &
import json
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

root = Path(sys.argv[1])
port = int(sys.argv[2])
quota = {
    "account_count": 3,
    "biz_names": ["asr_rec"],
    "total_used_duration_hours": 4.85,
    "total_used_count": 516,
    "start_date": "2026-03-31",
    "end_date": "2026-04-23",
    "selection_strategy": "highest_remaining_quota_with_short_term_reservation",
    "note": "Tencent Cloud GetUsageByDate returns usage only. remaining_quota_seconds is computed from local monthly_quota_seconds when configured.",
    "accounts": [
        {
            "name": "account-1",
            "secret_id_masked": "AKID***1111",
            "region": "ap-beijing",
            "monthly_quota_seconds": 36000,
            "monthly_quota_hours": 10,
            "used_duration_seconds": 16344,
            "used_duration_hours": 4.54,
            "remaining_quota_seconds": 19656,
            "remaining_quota_hours": 5.46,
            "used_count": 469,
            "usage_region": "ap-guangzhou",
            "biz_names": ["asr_rec"],
        },
        {
            "name": "account-2",
            "secret_id_masked": "AKID***2222",
            "region": "ap-beijing",
            "monthly_quota_seconds": 36000,
            "monthly_quota_hours": 10,
            "used_duration_seconds": 828,
            "used_duration_hours": 0.23,
            "remaining_quota_seconds": 35172,
            "remaining_quota_hours": 9.77,
            "used_count": 21,
            "usage_region": "ap-guangzhou",
            "biz_names": ["asr_rec"],
        },
        {
            "name": "account-3",
            "secret_id_masked": "AKID***3333",
            "region": "ap-beijing",
            "monthly_quota_seconds": 36000,
            "monthly_quota_hours": 10,
            "used_duration_seconds": 360,
            "used_duration_hours": 0.1,
            "remaining_quota_seconds": 35640,
            "remaining_quota_hours": 9.9,
            "used_count": 26,
            "usage_region": "ap-beijing",
            "biz_names": ["asr_rec"],
        },
    ],
    "ocr_usage": {
        "total_call_count": 42,
        "total_success_count": 40,
        "total_fail_count": 2,
        "total_billed_count": 39,
        "accounts": [
            {
                "name": "account-1",
                "secret_id_masked": "AKID***1111",
                "region": "ap-beijing",
                "call_count": 20,
                "success_count": 19,
                "fail_count": 1,
                "billed_count": 18,
                "sub_uins": ["10001"],
                "interfaces": [
                    {"interface_name": "GeneralAccurateOCR", "call_count": 18},
                    {"interface_name": "VatInvoiceOCR", "call_count": 2},
                ],
            },
            {
                "name": "account-2",
                "secret_id_masked": "AKID***2222",
                "region": "ap-beijing",
                "call_count": 22,
                "success_count": 21,
                "fail_count": 1,
                "billed_count": 21,
                "sub_uins": [],
                "interfaces": [
                    {"interface_name": "GeneralBasicOCR", "call_count": 22},
                ],
            },
        ],
    },
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (root / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/tencent/quota":
            body = json.dumps(quota, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
PY
  LOCAL_COMPARE_PID=$!
  trap 'if [[ -n "${LOCAL_COMPARE_PID:-}" ]]; then kill "${LOCAL_COMPARE_PID}" >/dev/null 2>&1 || true; fi' EXIT
  sleep 1
}

verify_page_dom() {
  assert_remote_html_matches_local
  ensure_playwright_runner
  start_local_compare_server "${LOCAL_COMPARE_PORT}"
  LOCAL_COMPARE_URL="http://127.0.0.1:${LOCAL_COMPARE_PORT}/"
  export LOCAL_COMPARE_URL PUBLIC_BASE_URL PLAYWRIGHT_RUNNER_DIR
  node - <<'NODE'
const { chromium } = require(process.env.PLAYWRIGHT_RUNNER_DIR + '/node_modules/playwright');

function stableArray(values) {
  return values.map((v) => String(v || '').trim()).filter(Boolean);
}

async function snapshotPage(page, url) {
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  const desktop = await page.evaluate(() => ({
    title: document.title,
    h1: document.querySelector('h1')?.textContent?.trim() || '',
    heroText: document.querySelector('.hero p')?.textContent?.trim() || '',
    actionButtons: Array.from(document.querySelectorAll('.actions a, .actions button')).map((el) => el.textContent.trim()),
    quickItemTitles: Array.from(document.querySelectorAll('.quick-item strong')).map((el) => el.textContent.trim()),
    sectionTitles: Array.from(document.querySelectorAll('.section-title')).map((el) => el.textContent.trim()),
    docHeadings: Array.from(document.querySelectorAll('.doc h3')).map((el) => el.textContent.trim()),
    docListCount: document.querySelectorAll('.doc-list').length,
    quickListCount: document.querySelectorAll('.quick-list .quick-item').length,
    hasQuickList: !!document.querySelector('.quick-list'),
    hasStack: !!document.querySelector('.stack'),
    hasSidebarCard: !!document.querySelector('.sidebar-card'),
    hasDualCards: !!document.querySelector('.cards.dual'),
    hasAccountTitle: !!document.querySelector('.account-title'),
    summaryStatsCount: document.querySelectorAll('#summaryStats .stat').length,
    ocrSummaryStatsCount: document.querySelectorAll('#ocrSummaryStats .stat').length,
  }));
  await page.setViewportSize({ width: 390, height: 1600 });
  await page.waitForTimeout(300);
  const mobile = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    buttonFont: getComputedStyle(document.querySelector('button')).fontSize,
    preFont: getComputedStyle(document.querySelector('pre')).fontSize,
    gridTemplate: getComputedStyle(document.querySelector('.grid')).gridTemplateColumns,
  }));
  return {
    status: response && response.status(),
    signature: {
      ...desktop,
      actionButtons: stableArray(desktop.actionButtons),
      quickItemTitles: stableArray(desktop.quickItemTitles),
      sectionTitles: stableArray(desktop.sectionTitles),
      docHeadings: stableArray(desktop.docHeadings),
    },
    mobile,
    consoleErrors,
    pageErrors,
  };
}

(async() => {
  const browser = await chromium.launch({ headless: true });
  const localPage = await browser.newPage({ viewport: { width: 1440, height: 2200 } });
  const remotePage = await browser.newPage({ viewport: { width: 1440, height: 2200 } });
  const local = await snapshotPage(localPage, process.env.LOCAL_COMPARE_URL);
  const remote = await snapshotPage(remotePage, process.env.PUBLIC_BASE_URL);
  await browser.close();

  const failures = [];
  if (local.status !== 200) failures.push(`local compare page returned HTTP ${local.status}`);
  if (remote.status !== 200) failures.push(`remote page returned HTTP ${remote.status}`);
  if (local.consoleErrors.length || local.pageErrors.length) failures.push('local compare page has browser errors');
  if (remote.consoleErrors.length || remote.pageErrors.length) failures.push('remote page has browser errors');
  if (JSON.stringify(local.signature) !== JSON.stringify(remote.signature)) failures.push('remote DOM signature differs from local');
  if (remote.mobile.scrollWidth !== remote.mobile.clientWidth) failures.push(`remote mobile overflow detected: ${remote.mobile.scrollWidth} != ${remote.mobile.clientWidth}`);

  console.log(JSON.stringify({ local, remote, failures }, null, 2));
  if (failures.length) process.exit(1);
})();
NODE
}

EXPECTED_HEAD="$(git -C "${LOCAL_SOURCE_DIR}" rev-parse --short "origin/${BRANCH}" 2>/dev/null || git -C "${LOCAL_SOURCE_DIR}" rev-parse --short HEAD)"

if [[ $VERIFY_ONLY -eq 1 ]]; then
  echo "EXPECTED_HEAD=${EXPECTED_HEAD}"
  assert_remote_head
  run_remote "$remote_verify_cmd"
  verify_page_dom
  exit 0
fi

if [[ $SYNC_ONLY -eq 1 ]]; then
  echo "EXPECTED_HEAD=${EXPECTED_HEAD}"
  run_remote "$remote_sync_cmd"
  assert_remote_head
  exit 0
fi

echo "EXPECTED_HEAD=${EXPECTED_HEAD}"
run_remote "$remote_sync_cmd"
assert_remote_head
echo
run_remote "$remote_verify_cmd"
echo
verify_page_dom
