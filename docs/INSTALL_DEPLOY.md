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

### 6.1 faster-whisper-small（语音）

- 下载脚本：`./prepare_faster_whisper_model.sh`
- 存放位置：`/data/project/to_text/models/small`
- 体积：约 `450MB~550MB`
- 是否提交 Git：否（已忽略）

### 6.2 PaddleOCR（图片）

- 触发方式：首次 OCR 时自动下载
- 常见缓存目录：
  - `~/.paddlex/official_models/`
  - `~/.paddleocr/`
- 体积：按模型组合波动，常见几十 MB 到 200+ MB
- 是否提交 Git：否

## 8. 常见报错与处理

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

## 9. 生产部署建议

- 将 `OCR_API_KEY` 放到环境变量，不要写入仓库
- 通过 Nginx 反代暴露 `/transcribe` 与 `/ocr`
- 定期清理日志并监控 `transcribe_http_to_text.log`
