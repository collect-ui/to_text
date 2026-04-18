# API 测试文档

默认服务地址：`http://127.0.0.1:8014`

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8014/health
```

示例响应：
```json
{"status":"ok"}
```

## 2. 音频转写 `/transcribe`

请求：
```bash
curl -s -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/demo.mp3"
  }'
```

默认返回为业务封装结构：
```json
{
  "count": 1,
  "success": true,
  "code": "0",
  "msg": "ok",
  "data": "...转写文本..."
}
```

## 3. 图片 OCR `/ocr`

请求（原始结果）：
```bash
curl -s -X POST 'http://127.0.0.1:8014/ocr' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/demo.jpg",
    "raw": true
  }'
```

示例响应（`raw=true`）：
```json
{
  "url": "https://example.com/demo.jpg",
  "status": "ok",
  "task": "image",
  "text": "...OCR文本...",
  "engine": "paddleocr",
  "model": "image-ocr",
  "duration_ms": 0
}
```

## 4. 可选请求字段

- `model`：语音模型名，默认 `small`
- `language`：默认 `zh`
- `task`：`auto|audio|image`
- `image_ocr_provider`：`auto|paddleocr|pytesseract|ai`
- `ocr_model`：AI OCR 模型名，默认 `gpt-4o-mini`
- `ocr_api_endpoint`：AI OCR endpoint
- `ocr_api_key`：AI OCR key
- `raw`：`true` 时返回原始结构

## 5. 一键冒烟测试脚本

```bash
cd /data/project/to_text
BASE_URL=http://127.0.0.1:8014 \
AUDIO_URL='https://example.com/demo.mp3' \
IMAGE_URL='https://example.com/demo.jpg' \
./scripts/api_smoke_test.sh
```

若只测健康检查，可直接运行：
```bash
./scripts/api_smoke_test.sh
```
