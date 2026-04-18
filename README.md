# to_text

音频/图片 URL 转文本服务，支持：
- 音频转写（`faster-whisper`）
- 图片 OCR（`PaddleOCR` / `pytesseract` / AI 视觉模型）
- HTTP API（`/health`、`/transcribe`、`/ocr`）

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

下载方式：
```bash
cd /data/project/to_text
./prepare_faster_whisper_model.sh
```

### 2.2 图片 OCR 模型

- 本地 OCR（推荐）：`PaddleOCR`（`lang=ch`）
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

### 4.1 安装为 Linux systemd 服务（开机自启）

```bash
cd /data/project/to_text
SERVICE_NAME=to-text ./scripts/install_systemd_service.sh
```

## 5. API 入口

- `GET /health`
- `POST /transcribe`
- `POST /ocr`

详细请求样例见：`docs/API_TEST.md`

## 6. Git 提交说明

仓库已配置忽略大文件：
- `models/`
- `*.log`
- `*.pid`
- `.venv/`

因此只提交代码和文档，不提交几百 MB 的模型文件。
