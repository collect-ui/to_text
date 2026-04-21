# API 测试文档

默认服务地址：`http://127.0.0.1:8014`

默认 ASR：`tencent`（`engine_model_type=16k_zh`，`res_text_format=3`）

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8014/health
```

示例响应：
```json
{"status":"ok"}
```

腾讯云账号用量查询：
```bash
curl -s 'http://127.0.0.1:8014/tencent/quota'
curl -s 'http://127.0.0.1:8014/tencent/quota?start_date=2026-04-01&end_date=2026-04-21'
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

腾讯云模式（覆盖默认模式）：
```bash
curl -s -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/demo.mp3",
    "asr_provider": "tencent",
    "raw": true
  }'
```

缓存命中验证（第二次同 URL 请求应返回 `cache_hit=true`）：
```bash
curl -s -X POST 'http://127.0.0.1:8014/transcribe' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/demo.mp3",
    "raw": true
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
  "cache_hit": false,
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
- `audio_chunk_seconds`：音频分段秒数；`0` 为不分段（默认）
- `asr_provider`：`local|tencent`，默认读取运行时配置 `transcribe_config.json`（模板文件为 `transcribe_config.template.json`）
- `cache_hit`（响应字段，仅 `raw=true` 时可见）：是否命中本地缓存
- `tencent_secret_id` / `tencent_secret_key`：单次请求覆盖腾讯云密钥
- `asr.tencent.accounts`：服务级多账号池，未显式传单次密钥时按账号轮询
- `tencent_region`：默认 `ap-beijing`
- `tencent_engine_model_type`：默认 `16k_zh`
- `tencent_res_text_format`：当前默认 `3`
- `tencent_quality_mode`：`standard|max`，默认 `standard`
- `tencent_filter_modal`：当前默认 `1`

`GET /tencent/quota` 查询参数：
- `start_date`：开始日期，默认当月 `1` 号
- `end_date`：结束日期，默认今天
- `biz_names`：逗号分隔，默认 `asr_rec`

注意：
- 接口底层调用腾讯云官方 `GetUsageByDate`，返回的是“已用量”
- 若配置了 `monthly_quota_seconds`，服务会额外返回按本地配置推算的 `remaining_quota_seconds`

服务级缓存参数（CLI）：
- `--cache-dir`：缓存目录（默认 `./cache/transcribe_result`）
- `--cache-max-entries`：最大缓存条目数（默认 `500`）
- `--cache-max-size-mb`：最大缓存体积（默认 `200` MB）
- `--no-result-cache`：禁用缓存

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
