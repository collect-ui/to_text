# Linux 安装与部署文档

## 1. 适用场景

- Ubuntu / Debian / CentOS / Rocky / AlmaLinux
- 需要一键安装并启动 `to_text` 服务

## 2. 一键部署命令

```bash
cd /data/project/to_text
./scripts/install_linux_oneclick.sh
```

## 3. 一键脚本做了什么

1. 安装系统依赖：
- `python3`
- `python3-venv`
- `python3-pip`
- `ffmpeg`
- `curl`
- `tesseract-ocr`
- `tesseract-ocr-chi-sim`（或同类中文语言包）

2. 创建虚拟环境并安装 Python 依赖：
- `faster-whisper`
- `huggingface_hub`
- `zhconv`
- `pillow`
- `pytesseract`
- `paddleocr`
- （可选）`paddlepaddle`

3. 下载模型：
- 语音模型：`Systran/faster-whisper-small` 到 `./models/small`
- OCR 模型：PaddleOCR 首次调用时缓存到用户目录（常见 `~/.paddlex/official_models/`）

4. 启动服务：
- 命令：`./start_transcribe_service.sh`
- 默认端口：`8014`

## 4. 可配置参数（环境变量）

执行脚本前可指定：

```bash
VENV_DIR=/opt/to_text_venv \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
INSTALL_PADDLE_OCR=1 \
START_AFTER_INSTALL=1 \
./scripts/install_linux_oneclick.sh
```

参数说明：
- `VENV_DIR`：虚拟环境目录
- `PIP_INDEX_URL`：pip 镜像源
- `INSTALL_PADDLE_OCR`：是否安装 `paddlepaddle`（`1/0`）
- `START_AFTER_INSTALL`：安装后是否自动启动服务（`1/0`）

## 5. 服务管理

启动：
```bash
cd /data/project/to_text
./start_transcribe_service.sh
```

停止：
```bash
cd /data/project/to_text
./stop_transcribe_service.sh
```

状态：
```bash
cd /data/project/to_text
./status_transcribe_service.sh
```

日志：`/data/project/to_text/transcribe_http_to_text.log`

## 6. systemd 开机自启（推荐生产）

```bash
cd /data/project/to_text
SERVICE_NAME=to-text ./scripts/install_systemd_service.sh
```

安装后：
```bash
systemctl status to-text
systemctl restart to-text
systemctl stop to-text
```

## 7. 模型下载位置与体积

### 7.1 faster-whisper-small（语音）

- 下载脚本：`./prepare_faster_whisper_model.sh`
- 存放位置：`/data/project/to_text/models/small`
- 体积：约 `450MB~550MB`
- 是否提交 Git：否（已忽略）

### 7.2 PaddleOCR（图片）

- 触发方式：首次 OCR 时自动下载
- 常见缓存目录：
  - `~/.paddlex/official_models/`
  - `~/.paddleocr/`
- 体积：按模型组合波动，常见几十 MB 到 200+ MB
- 是否提交 Git：否

## 8. 如何拿模型（在线/离线）

### 8.1 在线下载（默认）

```bash
cd /data/project/to_text
./prepare_faster_whisper_model.sh
```

脚本行为：
1. 优先从 `HF_CACHE_SOURCE` 指向的本地缓存拷贝（默认 `/home/zz/.cache/huggingface/hub`）
2. 若缓存不存在，再联网下载 `Systran/faster-whisper-small` 到 `./models/small`

### 8.2 网络不通机器（离线部署）

步骤 A：在可联网机器准备模型
```bash
cd /data/project/to_text
./prepare_faster_whisper_model.sh
tar -czf faster-whisper-small.tar.gz -C /data/project/to_text/models small
```

步骤 B：拷贝到目标机器并解压
```bash
cd /data/project/to_text
mkdir -p models
tar -xzf faster-whisper-small.tar.gz -C models
ls -lah /data/project/to_text/models/small
```

如果你的模型目录名不是 `small`，启动时显式指定：
```bash
MODEL=/data/project/to_text/models/small ./start_transcribe_service.sh
```

### 8.3 网络不通时的依赖安装建议

1. 在线机器先下载 wheel：
```bash
mkdir -p /tmp/wheels
pip download -r /data/project/to_text/requirements.txt -d /tmp/wheels
```

2. 拷贝 `/tmp/wheels` 到离线机后安装：
```bash
source /data/project/to_text/.venv/bin/activate
pip install --no-index --find-links /path/to/wheels -r /data/project/to_text/requirements.txt
```

## 9. OCR 安装（重点）

### 9.1 推荐：PaddleOCR（中文效果更好）

```bash
source /data/project/to_text/.venv/bin/activate
pip install paddleocr
pip install paddlepaddle
```

CPU 机器可直接使用；首次 OCR 会自动下载运行时模型到 `~/.paddlex/official_models/` 或 `~/.paddleocr/`。

强制使用 PaddleOCR 启动：
```bash
IMAGE_OCR_PROVIDER=paddleocr ./start_transcribe_service.sh
```

### 9.2 备用：pytesseract

系统依赖（Ubuntu/Debian）：
```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
```

Python 依赖：
```bash
source /data/project/to_text/.venv/bin/activate
pip install pillow pytesseract
```

强制使用 pytesseract：
```bash
IMAGE_OCR_PROVIDER=pytesseract ./start_transcribe_service.sh
```

### 9.3 AI OCR（可选）

当本地 OCR 不理想时可用：
```bash
OCR_API_KEY=xxx \
IMAGE_OCR_PROVIDER=ai \
./start_transcribe_service.sh
```

可选参数：`OCR_API_ENDPOINT`、`OCR_MODEL`（默认 `gpt-4o-mini`）

## 10. 常见报错与处理

1. `OCR dependency missing: install paddleocr`
- 处理：
```bash
source .venv/bin/activate
pip install paddleocr paddlepaddle
```

2. `tesseract not found`
- 处理（Ubuntu/Debian）：
```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
```

3. `No module named faster_whisper`
- 处理：
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

4. 模型目录为空导致转写失败
- 处理：
```bash
cd /data/project/to_text
./prepare_faster_whisper_model.sh
```

5. `Address already in use`
- 处理：
```bash
PORT=18014 ./start_transcribe_service.sh
```

6. 网络超时/下载失败（模型拉取失败）
- 处理：
```bash
# 方案1：从在线机器离线拷贝 models/small
# 方案2：设置公司代理后再拉取
export HTTPS_PROXY=http://<proxy-host>:<proxy-port>
export HTTP_PROXY=http://<proxy-host>:<proxy-port>
./prepare_faster_whisper_model.sh
```

## 11. 生产部署建议

- 将 `OCR_API_KEY` 放到环境变量，不要写入仓库
- 通过 Nginx 反代暴露 `/transcribe` 与 `/ocr`
- 定期清理日志并监控 `transcribe_http_to_text.log`
