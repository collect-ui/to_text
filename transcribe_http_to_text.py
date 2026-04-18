#!/usr/bin/env python3

"""Single-file tool for transcribing one URL or running an HTTP service."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, Literal, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from faster_whisper import WhisperModel

try:
    from PIL import Image, ImageEnhance
except Exception:  # pragma: no cover - optional dependency
    Image = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None

try:
    from paddleocr import PaddleOCR
except Exception:  # pragma: no cover - optional dependency
    PaddleOCR = None

try:
    import zhconv
except Exception:
    zhconv = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = SCRIPT_DIR
DEFAULT_PID_FILE = SCRIPT_DIR / 'transcribe_http_to_text.pid'
DEFAULT_LOG_FILE = SCRIPT_DIR / 'transcribe_http_to_text.log'
DEFAULT_AI_OCR_ENDPOINT = 'https://api.openai.com/v1/chat/completions'
DEFAULT_AI_OCR_MODEL = 'gpt-4o-mini'

AUDIO_EXTS = {
    '.mp3', '.m4a', '.wav', '.aac', '.flac', '.ogg', '.opus', '.amr', '.mp4',
    '.webm', '.mpeg', '.mpg', '.mkv', '.avi', '.audio'
}
IMAGE_EXTS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif', '.heic', '.heif'
}


_PADDLE_OCR = None
_PADDLE_OCR_LOCK = threading.Lock()


def resolve_model_path(model_name_or_path: str, base_dir: Path) -> str:
    explicit_path = Path(model_name_or_path)
    if explicit_path.exists():
        return str(explicit_path)

    candidates = [
        base_dir / model_name_or_path,
        base_dir / 'models' / model_name_or_path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return model_name_or_path


def stream_download(url: str, target: Path) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; transcribe-script/1.0)',
        'Accept': '*/*',
        'Connection': 'close',
    }
    last_err: Exception | None = None
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=60) as response, target.open('wb') as out_file:
                content_type = response.headers.get('Content-Type', '')
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        return content_type
                    out_file.write(chunk)
        except HTTPError as err:
            last_err = err
            # Retry transient server-side errors; fail fast for 4xx.
            if err.code < 500 or attempt >= max_attempts:
                raise
            time.sleep(0.8 * attempt)
        except URLError as err:
            last_err = err
            if attempt >= max_attempts:
                raise
            time.sleep(0.8 * attempt)

    if last_err is not None:
        raise last_err
    raise RuntimeError('download_failed')


def concat_segments(segments: Iterable) -> str:
    return ''.join(segment.text for segment in segments).strip()


def split_audio_to_wav_chunks(source: Path, chunk_seconds: int, output_dir: Path) -> list[Path]:
    if chunk_seconds <= 0:
        return [source]
    try:
        import av
        import numpy as np
    except Exception as err:
        raise RuntimeError(f'chunk split dependency missing: {err}') from err

    output_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = 16000
    bytes_per_sample = 2
    chunk_size_bytes = int(chunk_seconds) * sample_rate * bytes_per_sample
    if chunk_size_bytes <= 0:
        return [source]

    buffer = bytearray()
    chunks: list[Path] = []
    chunk_idx = 0

    def write_chunk(raw_pcm: bytes) -> None:
        nonlocal chunk_idx
        out_path = output_dir / f'chunk_{chunk_idx:05d}.wav'
        with wave.open(str(out_path), 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(bytes_per_sample)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(raw_pcm)
        chunks.append(out_path)
        chunk_idx += 1

    with av.open(str(source)) as container:
        audio_stream = next((stream for stream in container.streams if stream.type == 'audio'), None)
        if audio_stream is None:
            raise RuntimeError('no audio stream found for chunk split')
        resampler = av.audio.resampler.AudioResampler(
            format='s16',
            layout='mono',
            rate=sample_rate,
        )
        for frame in container.decode(audio_stream):
            resampled = resampler.resample(frame)
            if resampled is None:
                continue
            frames = resampled if isinstance(resampled, list) else [resampled]
            for one in frames:
                pcm = one.to_ndarray()
                if pcm.ndim == 2:
                    pcm = pcm[0]
                pcm_bytes = np.asarray(pcm, dtype=np.int16).tobytes()
                if pcm_bytes:
                    buffer.extend(pcm_bytes)
                while len(buffer) >= chunk_size_bytes:
                    write_chunk(bytes(buffer[:chunk_size_bytes]))
                    del buffer[:chunk_size_bytes]

    if buffer:
        write_chunk(bytes(buffer))
    if not chunks:
        raise RuntimeError('audio chunk split produced no chunks')
    return chunks


def to_simplified_chinese(text: str) -> str:
    if not text or zhconv is None:
        return text
    try:
        return zhconv.convert(text, 'zh-cn')
    except Exception:
        return text


def detect_task(url: str, content_type: str, explicit_task: str, local_path: Path) -> Literal['audio', 'image']:
    if explicit_task == 'audio':
        return 'audio'
    if explicit_task == 'image':
        return 'image'

    ctype = (content_type or '').lower()
    if ctype.startswith('image/'):
        return 'image'
    if ctype.startswith('audio/') or ctype.startswith('video/'):
        return 'audio'

    if looks_like_image(local_path):
        return 'image'

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTS:
        return 'image'
    if suffix in AUDIO_EXTS:
        return 'audio'
    return 'audio'


def looks_like_image(path: Path) -> bool:
    try:
        with path.open('rb') as f:
            head = f.read(12)
    except Exception:
        return False

    if head.startswith(b'\xff\xd8\xff'):
        return True
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return True
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return True
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return True
    return False


def extract_text_with_openai(url: str, model: str, api_key: str,
                            endpoint: str, timeout: int = 30) -> str:
    target = endpoint.rstrip('/')
    if not target.endswith('/chat/completions'):
        target = f'{target}/v1/chat/completions'

    payload = {
        'model': model,
        'messages': [
            {
                'role': 'system',
                'content': 'You are an OCR assistant. Extract all readable text from the image and output only the text.'
            },
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'text',
                        'text': 'Extract all visible text from this image, output plain text only.'
                    },
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': url,
                        },
                    },
                ],
            },
        ],
        'max_tokens': 1024,
    }

    req = Request(
        target,
        data=json.dumps(payload).encode('utf-8'),
        method='POST'
    )
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    with urlopen(req, timeout=timeout) as response:
        body = response.read().decode('utf-8')

    response_json = json.loads(body)
    choices = response_json.get('choices') or []
    if not choices:
        return ''
    return (choices[0].get('message', {}).get('content') or '').strip()


def extract_text_with_paddleocr(path: Path) -> str:
    if PaddleOCR is None:
        raise RuntimeError('OCR dependency missing: install paddleocr for local OCR')
    global _PADDLE_OCR
    with _PADDLE_OCR_LOCK:
        if _PADDLE_OCR is None:
            # Use Chinese+English model for mixed business chat screenshots.
            _PADDLE_OCR = PaddleOCR(use_angle_cls=True, lang='ch')
        ocr = _PADDLE_OCR

    result = ocr.predict(str(path))
    lines: list[str] = []
    if not result:
        return ''

    for item in result:
        # PaddleOCR 3.x result object
        rec_texts = getattr(item, 'rec_texts', None)
        if rec_texts:
            for text in rec_texts:
                text = str(text).strip()
                if text:
                    lines.append(text)
            continue

        # Defensive parsing for dict-like results
        if isinstance(item, dict):
            text = item.get('rec_text') or item.get('text') or ''
            text = str(text).strip()
            if text:
                lines.append(text)
    return '\n'.join(lines).strip()


def extract_text_from_image(path: Path, url: str, provider: str,
                           ai_model: str, ai_endpoint: str,
                           ai_timeout: int, ai_api_key: str | None) -> Tuple[str, str]:
    provider = (provider or 'auto').lower()
    ai_api_key = (ai_api_key or '').strip()
    local_text = ''
    local_provider: str | None = None

    if provider in {'auto', 'paddleocr'}:
        try:
            text = extract_text_with_paddleocr(path)
            if provider == 'paddleocr' or text:
                return text, 'paddleocr'
            local_text = local_text or text
            local_provider = local_provider or 'paddleocr'
        except Exception as err:
            if provider == 'paddleocr':
                if Image is not None and pytesseract is not None:
                    raw = Image.open(path).convert('L')
                    try:
                        enlarged = raw.resize((raw.width * 2, raw.height * 2))
                        enhanced = ImageEnhance.Contrast(enlarged).enhance(2.0)
                        binary = enhanced.point(lambda x: 255 if x > 170 else 0, 'L')
                        preferred = os.getenv('OCR_LANGS', 'chi_sim+eng')
                        try:
                            text = pytesseract.image_to_string(binary, lang=preferred)
                        except Exception:
                            text = pytesseract.image_to_string(binary, lang='eng')
                        return text.strip(), 'pytesseract-fallback'
                    finally:
                        raw.close()
                raise RuntimeError(f'PaddleOCR failed: {err}')

    if provider in {'auto', 'pytesseract'}:
        if Image is None or pytesseract is None:
            if provider == 'pytesseract':
                raise RuntimeError('OCR dependency missing: install pillow and pytesseract before using image mode')
        else:
            raw = Image.open(path).convert('L')
            try:
                enlarged = raw.resize((raw.width * 2, raw.height * 2))
                enhanced = ImageEnhance.Contrast(enlarged).enhance(2.0)
                binary = enhanced.point(lambda x: 255 if x > 170 else 0, 'L')

                preferred = os.getenv('OCR_LANGS', 'chi_sim+eng')
                try:
                    text = pytesseract.image_to_string(binary, lang=preferred)
                except Exception:
                    try:
                        text = pytesseract.image_to_string(binary, lang='eng')
                    except Exception:
                        text = pytesseract.image_to_string(binary)
                text = text.strip()
                local_text = text
                local_provider = 'pytesseract'
                if provider == 'pytesseract':
                    return text, 'pytesseract'
                # auto mode: if AI is unavailable, return local OCR result directly.
                if len(text) >= 4 or not ai_api_key:
                    return text, 'pytesseract'
            finally:
                raw.close()

    if provider in {'auto', 'ai'}:
        if not ai_api_key:
            if provider == 'auto' and local_provider:
                return local_text, local_provider
            raise RuntimeError('AI OCR not configured: set OCR_API_KEY (or OPENAI_API_KEY) and OCR_API_ENDPOINT')
        try:
            return extract_text_with_openai(
                url,
                ai_model,
                ai_api_key,
                ai_endpoint,
                ai_timeout,
            ), 'ai'
        except Exception as err:
            if provider == 'auto' and local_provider:
                # Keep service available even when remote OCR endpoint fails.
                return local_text, f'{local_provider}-fallback'
            raise RuntimeError(f'AI OCR request failed: {err}')

    if provider == 'auto' and local_provider:
        return local_text, local_provider

    raise RuntimeError(f'Unsupported OCR provider: {provider}')


class ModelPool:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._cache: Dict[Tuple[str, str, str], WhisperModel] = {}
        self._lock = threading.Lock()

    def get(self, model_name: str, device: str, compute_type: str) -> Tuple[WhisperModel, str]:
        key = (model_name, device, compute_type)
        with self._lock:
            if key in self._cache:
                return self._cache[key], resolve_model_path(model_name, self._base_dir)

            model_path = resolve_model_path(model_name, self._base_dir)
            model = WhisperModel(model_path, device=device, compute_type=compute_type)
            self._cache[key] = model
            return model, model_path


def wrap_result_payload(result: Dict) -> Dict:
    if result.get('status') == 'ok':
        return {
            'count': 1,
            'success': True,
            'code': '0',
            'msg': 'ok',
            'data': result.get('text', ''),
        }
    return {
        'count': 0,
        'success': False,
        'code': '-1',
        'msg': json.dumps(result, ensure_ascii=False),
        'data': None,
    }


def transcribe_url(url: str,
                   model_name: str,
                   model_pool: ModelPool,
                   device: str,
                   compute_type: str,
                   language: str | None,
                   vad_filter: bool,
                   beam_size: int,
                   temperature: float,
                   audio_chunk_seconds: int,
                   task: str,
                   image_ocr_provider: str,
                   ai_model: str,
                   ai_endpoint: str,
                   ai_timeout: int,
                   ai_api_key: str | None) -> Dict:
    suffix = Path(urlparse(url).path).suffix or '.audio'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        content_type = stream_download(url, tmp_path)
        resolved_task = detect_task(url, content_type, task, tmp_path)

        if resolved_task == 'image':
            text, used_provider = extract_text_from_image(
                tmp_path,
                url,
                image_ocr_provider,
                ai_model,
                ai_endpoint,
                ai_timeout,
                ai_api_key,
            )
            return {
                'url': url,
                'status': 'ok',
                'task': 'image',
                'text': text,
                'language': language,
                'duration': None,
                'engine': used_provider,
                'model': 'image-ocr',
                'model_path': None,
                'transcription_source': str(tmp_path),
            }

        model, model_path = model_pool.get(model_name, device, compute_type)
        chunk_seconds = max(0, int(audio_chunk_seconds))
        chunk_paths = [tmp_path]
        if chunk_seconds > 0:
            chunk_dir = Path(tempfile.mkdtemp(prefix='transcribe_chunks_'))
            chunk_paths = split_audio_to_wav_chunks(tmp_path, chunk_seconds, chunk_dir)
        else:
            chunk_dir = None

        text_parts: list[str] = []
        detected_language = language
        total_duration = 0.0
        for chunk_path in chunk_paths:
            segments, info = model.transcribe(
                str(chunk_path),
                language=language,
                vad_filter=vad_filter,
                beam_size=beam_size,
                temperature=temperature,
            )
            part = concat_segments(segments)
            if part:
                text_parts.append(part.strip())
            if info and info.language:
                detected_language = info.language
            if info and info.duration:
                total_duration += float(info.duration)

        text = '\n'.join(text_parts).strip()
        text = to_simplified_chinese(text)

        return {
            'url': url,
            'status': 'ok',
            'task': 'audio',
            'text': text,
            'language': detected_language,
            'duration': total_duration if total_duration > 0 else None,
            'engine': 'faster-whisper',
            'model': model_name,
            'model_path': model_path,
            'audio_chunk_seconds': chunk_seconds,
            'chunk_count': len(chunk_paths),
            'transcription_source': str(tmp_path),
        }
    except Exception as err:  # pragma: no cover - entrypoint handling
        return {
            'url': url,
            'status': 'error',
            'error': str(err),
        }
    finally:
        chunk_dir_obj = locals().get('chunk_dir')
        if chunk_dir_obj is not None:
            try:
                shutil.rmtree(chunk_dir_obj, ignore_errors=True)
            except Exception:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--model', default='small', help='Model name or model folder')
    parser.add_argument('--model-dir', default=str(DEFAULT_MODEL_DIR),
                        help='Directory to search for local model first (default: script folder)')
    parser.add_argument('--device', default='cpu', choices=['cpu', 'cuda'],
                        help='faster-whisper device')
    parser.add_argument('--compute-type', default='int8',
                        help='faster-whisper compute type, e.g. int8, int8_float32, float16')
    parser.add_argument('--language', default='zh',
                        help='Language code, e.g. zh/en. Empty to auto-detect')
    parser.add_argument('--image-ocr-provider',
                        default=os.getenv('IMAGE_OCR_PROVIDER', 'auto'),
                        choices=['auto', 'pytesseract', 'paddleocr', 'ai'],
                        help='Image OCR provider: auto/paddleocr/pytesseract/ai')
    parser.add_argument('--ocr-api-endpoint',
                        default=os.getenv('OCR_API_ENDPOINT', DEFAULT_AI_OCR_ENDPOINT),
                        help='AI OCR endpoint')
    parser.add_argument('--ocr-model',
                        default=os.getenv('OCR_MODEL', DEFAULT_AI_OCR_MODEL),
                        help='AI OCR model')
    parser.add_argument('--ocr-api-key',
                        default=(os.getenv('OCR_API_KEY') or os.getenv('OPENAI_API_KEY') or ''),
                        help='AI OCR API key')
    parser.add_argument('--ocr-timeout',
                        type=int,
                        default=int(os.getenv('OCR_TIMEOUT', '30')),
                        help='AI OCR request timeout seconds')
    parser.add_argument('--task', default='auto', choices=['auto', 'audio', 'image'],
                        help='Force task: auto detects by URL/media type')
    parser.add_argument('--no-vad', action='store_true', help='Disable VAD filter')
    parser.add_argument('--beam-size', type=int, default=5, help='Whisper beam size')
    parser.add_argument('--temperature', type=float, default=0.0, help='Sampling temperature')
    parser.add_argument('--audio-chunk-seconds',
                        type=int,
                        default=int(os.getenv('AUDIO_CHUNK_SECONDS', '0')),
                        help='Split audio into chunks (seconds) before transcribe; 0 disables split')


def run_transcribe_command(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_pool = ModelPool(model_dir)
    language = args.language.strip() or None

    result = transcribe_url(
        url=args.url,
        model_name=args.model,
        model_pool=model_pool,
        device=args.device,
        compute_type=args.compute_type,
        language=language,
        vad_filter=not args.no_vad,
        beam_size=args.beam_size,
        temperature=args.temperature,
        audio_chunk_seconds=args.audio_chunk_seconds,
        task=args.task,
        image_ocr_provider=args.image_ocr_provider,
        ai_model=args.ocr_model,
        ai_endpoint=args.ocr_api_endpoint,
        ai_timeout=args.ocr_timeout,
        ai_api_key=args.ocr_api_key,
    )

    print(json.dumps(result, ensure_ascii=False) if args.json else result.get('text', ''))
    return 0 if result.get('status') == 'ok' else 2


def serve_command(args: argparse.Namespace) -> int:
    host = args.host
    port = args.port
    model_pool = ModelPool(Path(args.model_dir).expanduser().resolve())
    defaults = {
        'model': args.model,
        'model_dir': Path(args.model_dir).expanduser().resolve(),
        'device': args.device,
        'compute_type': args.compute_type,
        'language': args.language.strip() or None,
        'vad_filter': not args.no_vad,
        'beam_size': args.beam_size,
        'temperature': args.temperature,
        'audio_chunk_seconds': args.audio_chunk_seconds,
        'task': args.task,
        'image_ocr_provider': args.image_ocr_provider,
        'ai_model': args.ocr_model,
        'ai_endpoint': args.ocr_api_endpoint,
        'ai_timeout': args.ocr_timeout,
        'ai_api_key': args.ocr_api_key,
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'transcribe-http/1.0'
        server_ctx = {
            'model_pool': model_pool,
            'defaults': defaults,
        }

        def _json_resp(self, payload: Dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _error(self, status: int, message: str) -> None:
            self._json_resp({'status': 'error', 'error': message}, status)

        def do_GET(self):
            try:
                if self.path == '/health':
                    self._json_resp({'status': 'ok'})
                    return
                self._error(404, 'Not Found')
            except Exception:
                self._error(500, 'server_error')
                self.log_error('GET %s failed', self.path)

        def do_POST(self):
            try:
                if self.path not in {'/transcribe', '/ocr'}:
                    self._error(404, 'Not Found')
                    return

                length = int(self.headers.get('Content-Length', '0') or '0')
                if length <= 0:
                    self._error(400, 'Missing JSON body')
                    return

                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode('utf-8'))
                except Exception as err:
                    self._error(400, str(err))
                    return

                url = payload.get('url')
                if not url:
                    self._error(400, 'Missing url')
                    return

                cfg = self.server_ctx['defaults']
                req_lang = payload.get('language')
                lang = req_lang.strip() if isinstance(req_lang, str) else cfg['language']
                lang = lang or None

                try:
                    beam = int(payload.get('beam_size', cfg['beam_size']))
                except Exception:
                    self._error(400, 'Invalid beam_size')
                    return

                try:
                    temp = float(payload.get('temperature', cfg['temperature']))
                except Exception:
                    self._error(400, 'Invalid temperature')
                    return
                try:
                    audio_chunk_seconds = int(payload.get('audio_chunk_seconds', cfg['audio_chunk_seconds']))
                except Exception:
                    self._error(400, 'Invalid audio_chunk_seconds')
                    return
                if audio_chunk_seconds < 0:
                    self._error(400, 'Invalid audio_chunk_seconds')
                    return

                req = transcribe_url(
                    url=url,
                    model_name=payload.get('model', cfg['model']),
                    model_pool=self.server_ctx['model_pool'],
                    device=payload.get('device', cfg['device']),
                    compute_type=payload.get('compute_type', cfg['compute_type']),
                    language=lang,
                    vad_filter=not bool(payload.get('no_vad', False)) if payload.get('no_vad', False) else cfg['vad_filter'],
                    beam_size=beam,
                    temperature=temp,
                    audio_chunk_seconds=audio_chunk_seconds,
                    task=payload.get('task') or ('image' if self.path == '/ocr' else cfg['task']),
                    image_ocr_provider=payload.get('image_ocr_provider', cfg['image_ocr_provider']),
                    ai_model=payload.get('ocr_model', cfg['ai_model']),
                    ai_endpoint=payload.get('ocr_api_endpoint', cfg['ai_endpoint']),
                    ai_timeout=int(payload.get('ocr_timeout', cfg['ai_timeout'])),
                    ai_api_key=payload.get('ocr_api_key', cfg['ai_api_key']),
                )
                req['duration_ms'] = int(req.get('duration', 0) * 1000) if req.get('duration') else 0
                if payload.get('raw') is True:
                    self._json_resp(req, 200 if req.get('status') == 'ok' else 500)
                else:
                    wrapped = wrap_result_payload(req)
                    # Keep wrapped response HTTP 200 to avoid transport-layer retries.
                    self._json_resp(wrapped, 200)
            except Exception as exc:
                import traceback
                self.log_error('POST %s failed: %s', self.path, traceback.format_exc())
                self._error(500, 'server_error')

    # attach model_pool for closure safety
    Handler.server_ctx['model_pool'] = model_pool

    server = ThreadingHTTPServer((host, port), Handler)
    print(f'Started service at http://{host}:{port}/transcribe or /ocr')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cmd_start(args: argparse.Namespace) -> int:
    pid_file = Path(args.pid_file).expanduser()
    log_file = Path(args.log_file).expanduser()

    if pid_file.exists():
        old_pid = int(pid_file.read_text(encoding='utf-8').strip())
        if is_running(old_pid):
            print(f'service already running, pid={old_pid}')
            return 0
        pid_file.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        'serve',
        '--host', args.host,
        '--port', str(args.port),
        '--model', args.model,
        '--model-dir', args.model_dir,
        '--device', args.device,
        '--compute-type', args.compute_type,
        '--language', args.language,
        '--image-ocr-provider', args.image_ocr_provider,
        '--ocr-api-endpoint', args.ocr_api_endpoint,
        '--ocr-model', args.ocr_model,
        '--ocr-api-key', args.ocr_api_key,
        '--ocr-timeout', str(args.ocr_timeout),
        '--task', args.task,
        '--beam-size', str(args.beam_size),
        '--temperature', str(args.temperature),
        '--audio-chunk-seconds', str(args.audio_chunk_seconds),
    ]
    if args.no_vad:
        cmd.append('--no-vad')

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open('a', encoding='utf-8') as out:
        proc = subprocess.Popen(
            cmd,
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    pid_file.write_text(str(proc.pid), encoding='utf-8')
    print(f'service started, pid={proc.pid}, log={log_file}')
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    pid_file = Path(args.pid_file).expanduser()
    if not pid_file.exists():
        print('service not running (pid file missing)')
        return 0

    pid = int(pid_file.read_text(encoding='utf-8').strip())
    if not is_running(pid):
        pid_file.unlink(missing_ok=True)
        print('stale pid file removed')
        return 0

    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not is_running(pid):
            break
        time.sleep(0.2)

    if is_running(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)

    pid_file.unlink(missing_ok=True)
    print('service stopped')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    pid_file = Path(args.pid_file).expanduser()
    if not pid_file.exists():
        print('service not running')
        return 1
    pid = int(pid_file.read_text(encoding='utf-8').strip())
    print(f'pid={pid} running={is_running(pid)}')
    return 0 if is_running(pid) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='faster-whisper URL transcribe utility')
    subparsers = parser.add_subparsers(dest='command', required=True)

    p_trans = subparsers.add_parser('transcribe', help='transcribe one URL and print result')
    p_trans.add_argument('url', help='http/https audio or image URL')
    p_trans.add_argument('--json', action='store_true', help='print JSON output')
    add_common_args(p_trans)

    p_serve = subparsers.add_parser('serve', help='run HTTP service in foreground')
    p_serve.add_argument('--host', default='0.0.0.0', help='bind host')
    p_serve.add_argument('--port', type=int, default=8000, help='bind port')
    add_common_args(p_serve)

    p_start = subparsers.add_parser('start', help='start HTTP service as background')
    p_start.add_argument('--host', default='0.0.0.0', help='bind host')
    p_start.add_argument('--port', type=int, default=8000, help='bind port')
    add_common_args(p_start)
    p_start.add_argument('--pid-file', default=str(DEFAULT_PID_FILE), help='pid file path')
    p_start.add_argument('--log-file', default=str(DEFAULT_LOG_FILE), help='log file path')

    p_stop = subparsers.add_parser('stop', help='stop background service')
    p_stop.add_argument('--pid-file', default=str(DEFAULT_PID_FILE), help='pid file path')

    p_status = subparsers.add_parser('status', help='check background service')
    p_status.add_argument('--pid-file', default=str(DEFAULT_PID_FILE), help='pid file path')

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == 'transcribe':
        return run_transcribe_command(args)
    if args.command == 'serve':
        return serve_command(args)
    if args.command == 'start':
        return cmd_start(args)
    if args.command == 'stop':
        return cmd_stop(args)
    if args.command == 'status':
        return cmd_status(args)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
