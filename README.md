# to_text

音频/图片 URL 转文本服务，支持：
- 音频转写（`Tencent Cloud ASR`，默认）
- 音频转写（`faster-whisper`，可切换）
- 图片 OCR（`Tencent Cloud OCR`，默认；可切换 `PaddleOCR` / `pytesseract` / AI 视觉模型）
- HTTP API（`/health`、`/transcribe`、`/tencent/quota`、`/tencent/account-requests`；`/ocr` 仅兼容保留）
- 腾讯账号申请页与审核页（`/apply`、`/review`）

## 1. 项目结构

- `transcribe_http_to_text.py`：主程序（CLI + HTTP 服务）
- `start_transcribe_service.sh`：后台启动
- `stop_transcribe_service.sh`：停止服务
- `status_transcribe_service.sh`：服务状态
- `prepare_faster_whisper_model.sh`：下载/准备语音模型
- `scripts/install_linux_oneclick.sh`：Linux 一键安装 + 下载模型 + 启动
- `scripts/install_systemd_service.sh`：安装为 systemd 服务（开机自启）
- `scripts/download_models.sh`：仅下载模型
- `scripts/api_smoke_test.sh`：API 冒烟测试
- `docs/INSTALL_DEPLOY.md`：安装部署文档
- `docs/API_TEST.md`：API 测试文档

## 2. 模型说明（重点）

### 2.1 语音模型（本地）

- 引擎：`faster-whisper`
- 模型：`Systran/faster-whisper-small`
- 默认本地路径：`./models/small`
- 体积：约 `450MB~550MB`（不同版本略有波动）
- Git 提交策略：**不提交模型文件**（已在 `.gitignore` 忽略 `models/`）

### 2.2 腾讯云 ASR（默认）

- 接口：`CreateRecTask` + `DescribeTaskStatus`（异步轮询）
- 运行时配置文件：`./transcribe_config.json`（已忽略，不提交）
- 审批申请存储：`./tencent_account_requests.json`（已忽略，不提交）
- 配置模板：`./transcribe_config.template.json`
- 默认模式字段：`asr.default_provider`（当前默认 `tencent`）
- 凭证字段：`asr.tencent.secret_id`、`asr.tencent.secret_key`
- 多账号字段：`asr.tencent.accounts`
- 审核管理口令：环境变量 `ADMIN_TOKEN` 或启动参数 `--admin-token`
- 当前默认识别参数：`engine_model_type=16k_zh`、`res_text_format=3`

下载方式：
```bash
cd /data/project/to_text
./prepare_faster_whisper_model.sh
```

离线机器拿模型（网络不通）：
1. 在可联网机器先执行 `./prepare_faster_whisper_model.sh`
2. 打包 `models/small` 后拷贝到目标机 `/data/project/to_text/models/small`
3. 目标机启动前确认目录非空：`ls -lah /data/project/to_text/models/small`

### 2.3 图片 OCR 模型

- 默认云端 OCR：`Tencent Cloud OCR`（`GeneralAccurateOCR`，复用腾讯密钥）
- 本地 OCR：`PaddleOCR`（`lang=ch`）
- 本地兼容：`pytesseract`
- 云端 OCR：OpenAI 兼容接口（默认 `gpt-4o-mini`）

PaddleOCR 运行时模型缓存（自动下载）：
- 常见位置：`~/.paddlex/official_models/` 或 `~/.paddleocr/`
- 建议：在部署机首次执行 `scripts/download_models.sh` 预热

## 3. 快速启动（已有环境）

```bash
cd /data/project/to_text
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
./scripts/download_models.sh
./start_transcribe_service.sh
```

默认监听：`0.0.0.0:8014`

健康检查：
```bash
curl http://127.0.0.1:8014/health
```

## 4. Linux 一键部署（推荐）

```bash
cd /data/project/to_text
chmod +x scripts/install_linux_oneclick.sh
./scripts/install_linux_oneclick.sh
```

脚本会执行：
1. 安装系统依赖（python/ffmpeg/tesseract 等）
2. 创建虚拟环境并安装 Python 依赖
3. 下载语音模型与 OCR 运行时模型
4. 自动启动服务

更多参数见：`docs/INSTALL_DEPLOY.md`
模型下载失败、离线拷贝、OCR 安装重点见：`docs/INSTALL_DEPLOY.md` 第 `7/8/9` 节

### 4.1 安装为 Linux systemd 服务（开机自启）

```bash
cd /data/project/to_text
SERVICE_NAME=to-text ./scripts/install_systemd_service.sh
```

## 5. API 入口

- `GET /health`
- `GET /tencent/quota`
- `GET /apply`
- `GET /review`
- `GET /tencent/account-requests`（管理员）
- `POST /tencent/account-credentials/validate`（管理员）
- `POST /tencent/account-requests`
- `POST /tencent/account-requests/{id}/validate`（管理员）
- `POST /tencent/account-requests/{id}/approve`（管理员）
- `POST /tencent/account-requests/{id}/reject`（管理员）
- `POST /tencent/account-requests/{id}/undo`（管理员）
- `POST /tencent/account-requests/{id}/delete`（管理员；已通过记录需先撤销）
- `POST /transcribe`

图片与音频统一走 `POST /transcribe`：
- 音频 URL：自动识别为 `audio`
- 图片 URL：自动识别为 `image`
- 也可显式传 `task=image`
- `POST /ocr` 仅作为兼容别名保留

`GET /tencent/quota` 当前会同时返回：
- ASR 用量汇总（`asr_rec` 等）
- OCR 官方控制台调用统计（字段 `ocr_usage`，来自 `QueryCallForConsole`）

腾讯账号审批流：
- 申请页 `/apply` 公开提交名称和腾讯密钥，不直接修改运行配置
- 审核页 `/review` 用 `X-Admin-Token` 调管理接口，支持验证、通过、拒绝和撤销
- 申请列表接口支持 `account_name` + `match_mode(contains|exact)`，可按账号做包含或精确搜索（不区分大小写）
- 审核通过后会把账号追加到 `asr.tencent.accounts`，并立即热加载生效
- 撤销仅允许最近一次已生效审批，回滚到该次审批前的配置快照

### 5.1 转写结果缓存（默认开启）

- 缓存音频转写与图片 OCR 的成功结果（`status=ok` 且 `task=audio|image`）
- 缓存键：完整 URL 完全一致
- 默认目录：`./cache/transcribe_result`
- 默认策略：LRU，最多 `500` 条且最多 `200MB`（任一超限都会淘汰最久未访问项）
- `raw=true` 时会返回 `cache_hit: true|false`

可通过参数调整：
- `--cache-dir`
- `--cache-max-entries`
- `--cache-max-size-mb`
- `--no-result-cache`

详细请求样例见：`docs/API_TEST.md`

长音频建议在请求中设置 `audio_chunk_seconds`（例如 `60`）启用分段转写，服务会自动拼接文本，降低 7~8 分钟以上音频的失败率。

## 6. 腾讯云模式示例

多账号配置示例：

```json
{
  "asr": {
    "default_provider": "tencent",
    "tencent": {
      "region": "ap-beijing",
      "engine_model_type": "16k_zh",
      "accounts": [
        {
          "name": "account-1",
          "secret_id": "AKIDxxxxx",
          "secret_key": "xxxxx",
          "region": "ap-beijing",
          "monthly_quota_seconds": 18000
        },
        {
          "name": "account-2",
          "secret_id": "AKIDyyyyy",
          "secret_key": "yyyyy",
          "region": "ap-beijing",
          "monthly_quota_seconds": 18000
        }
      ]
    }
  }
}
```

首次使用：

```bash
cp transcribe_config.template.json transcribe_config.json
```

如需启用审核页：

```bash
export ADMIN_TOKEN='change-this-token'
./start_transcribe_service.sh
```

说明：
- 配了 `accounts` 后，腾讯转写会按账号轮询
- 如果单次请求显式传了 `tencent_secret_id` / `tencent_secret_key`，仍然优先走请求内密钥
- `monthly_quota_seconds` 是本地配置额度，不是腾讯云接口直接返回字段
- 待审核申请落在 `tencent_account_requests.json`，与运行配置文件分开保存

默认走配置里的 `asr.default_provider`。单次请求可覆盖：

```bash
curl -s -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/demo.mp3",
    "asr_provider": "tencent",
    "raw": true
  }'
```

CLI 示例：

```bash
python3 transcribe_http_to_text.py transcribe 'https://example.com/demo.mp3' \
  --asr-provider tencent \
  --json
```

用量查询示例：

```bash
curl -s 'http://127.0.0.1:8014/tencent/quota'
curl -s 'http://127.0.0.1:8014/tencent/quota?start_date=2026-04-01&end_date=2026-04-21'
curl -s 'http://127.0.0.1:8014/tencent/quota?biz_names=asr_rec,asr_rt'
```

## 7. Git 提交说明

仓库已配置忽略大文件：
- `models/`
- `*.log`
- `*.pid`
- `.venv/`

因此只提交代码和文档，不提交几百 MB 的模型文件。
