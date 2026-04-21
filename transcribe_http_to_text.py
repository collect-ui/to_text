#!/usr/bin/env python3

"""Single-file tool for transcribing one URL or running an HTTP service."""

import argparse
import datetime
import hashlib
import hmac
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
from urllib.parse import parse_qs, urlparse
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
DEFAULT_CONFIG_FILE = SCRIPT_DIR / 'transcribe_config.json'
DEFAULT_INDEX_FILE = SCRIPT_DIR / 'index.html'
DEFAULT_RESULT_CACHE_DIR = SCRIPT_DIR / 'cache' / 'transcribe_result'
DEFAULT_RESULT_CACHE_MAX_ENTRIES = 500
DEFAULT_RESULT_CACHE_MAX_SIZE_MB = 200
TENCENT_ASR_ENDPOINT = 'https://asr.tencentcloudapi.com'
TENCENT_ASR_VERSION = '2019-06-14'
TENCENT_USAGE_REGION_FALLBACKS = ('ap-guangzhou', 'ap-shanghai', 'ap-beijing')
TENCENT_USAGE_CACHE_TTL_SECONDS = 300
TENCENT_SELECTION_RESERVATION_SECONDS = 180


def _deep_update(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_update(dst[key], value)
        else:
            dst[key] = value
    return dst


def _load_runtime_config(path: Path) -> dict:
    defaults = {
        'asr': {
            'default_provider': 'tencent',
            'tencent': {
                'secret_id': '',
                'secret_key': '',
                'region': 'ap-beijing',
                'engine_model_type': '16k_zh',
                'channel_num': 1,
                'res_text_format': 3,
                'quality_mode': 'standard',
                'hotword_id': '',
                'hotword_list': '',
                'convert_num_mode': 1,
                'filter_modal': 1,
                'filter_punc': 0,
                'filter_dirty': 0,
                'poll_interval_seconds': 2,
                'poll_timeout_seconds': 600,
            },
        },
    }
    if not path.exists():
        return defaults
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            return _deep_update(defaults, loaded)
    except Exception:
        pass
    return defaults


def _mask_secret(value: str) -> str:
    if not value:
        return ''
    if len(value) <= 8:
        return '*' * len(value)
    return f'{value[:4]}***{value[-4:]}'


def _seconds_to_hours(seconds: int | float) -> float:
    return round(float(seconds or 0) / 3600.0, 2)


def _normalize_tencent_accounts(raw_accounts: object, fallback_account: dict) -> list[dict]:
    accounts: list[dict] = []
    if isinstance(raw_accounts, list):
        for idx, item in enumerate(raw_accounts):
            if not isinstance(item, dict):
                continue
            account = {
                'name': str(item.get('name') or f'account-{idx + 1}'),
                'secret_id': str(item.get('secret_id') or '').strip(),
                'secret_key': str(item.get('secret_key') or '').strip(),
                'region': str(item.get('region') or fallback_account.get('region') or 'ap-beijing').strip(),
                'monthly_quota_seconds': int(item.get('monthly_quota_seconds') or 0),
            }
            if account['secret_id'] and account['secret_key']:
                accounts.append(account)

    if accounts:
        return accounts

    fallback = {
        'name': 'default',
        'secret_id': str(fallback_account.get('secret_id') or '').strip(),
        'secret_key': str(fallback_account.get('secret_key') or '').strip(),
        'region': str(fallback_account.get('region') or 'ap-beijing').strip(),
        'monthly_quota_seconds': int(fallback_account.get('monthly_quota_seconds') or 0),
    }
    if fallback['secret_id'] and fallback['secret_key']:
        return [fallback]
    return []


class TencentCredentialPool:
    def __init__(self, accounts: list[dict]):
        self._accounts = [dict(item) for item in accounts if item.get('secret_id') and item.get('secret_key')]
        self._lock = threading.Lock()
        self._cursor = 0
        self._usage_cache: dict | None = None
        self._usage_cache_at = 0.0
        self._virtual_remaining: dict[str, int] = {}

    def next_account(self) -> dict | None:
        if not self._accounts:
            return None
        with self._lock:
            usage_summary = self._refresh_usage_cache_locked()
            usage_map = {
                str(item.get('name') or ''): item
                for item in (usage_summary.get('accounts') or [])
                if isinstance(item, dict)
            } if isinstance(usage_summary, dict) else {}

            quota_ready = all(int(account.get('monthly_quota_seconds') or 0) > 0 for account in self._accounts)
            if quota_ready and len(usage_map) == len(self._accounts):
                ranked: list[tuple[int, int, dict]] = []
                for idx, account in enumerate(self._accounts):
                    name = str(account.get('name') or '')
                    usage = usage_map.get(name, {})
                    remaining = int(usage.get('remaining_quota_seconds') or 0)
                    if name not in self._virtual_remaining:
                        self._virtual_remaining[name] = remaining
                    ranked.append((self._virtual_remaining[name], -((self._cursor + idx) % len(self._accounts)), account))
                ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
                selected = dict(ranked[0][2])
                selected_name = str(selected.get('name') or '')
                self._virtual_remaining[selected_name] = max(
                    0,
                    int(self._virtual_remaining.get(selected_name, 0)) - TENCENT_SELECTION_RESERVATION_SECONDS,
                )
                selected['_selection_strategy'] = 'highest_remaining_quota'
                selected['_selection_remaining_quota_seconds'] = int(self._virtual_remaining.get(selected_name, 0))
                self._cursor = (self._cursor + 1) % len(self._accounts)
                return selected

            account = dict(self._accounts[self._cursor % len(self._accounts)])
            self._cursor = (self._cursor + 1) % len(self._accounts)
            account['_selection_strategy'] = 'round_robin'
            return account

    def quota_summary(self, force_refresh: bool = False) -> dict:
        with self._lock:
            if force_refresh:
                self._usage_cache_at = 0.0
            return dict(self._refresh_usage_cache_locked())

    def _refresh_usage_cache_locked(self) -> dict:
        now = time.time()
        if self._usage_cache is not None and now - self._usage_cache_at < TENCENT_USAGE_CACHE_TTL_SECONDS:
            return self._usage_cache

        today = datetime.date.today()
        summary = summarize_tencent_usage(
            self._accounts,
            start_date=today.replace(day=1).isoformat(),
            end_date=today.isoformat(),
            biz_names=['asr_rec'],
        )
        self._usage_cache = summary
        self._usage_cache_at = now
        self._virtual_remaining = {
            str(item.get('name') or ''): int(item.get('remaining_quota_seconds') or 0)
            for item in (summary.get('accounts') or [])
            if isinstance(item, dict)
        }
        return summary


RUNTIME_CONFIG = _load_runtime_config(DEFAULT_CONFIG_FILE)
tencent_runtime = RUNTIME_CONFIG.setdefault('asr', {}).setdefault('tencent', {})
tencent_runtime['accounts'] = _normalize_tencent_accounts(tencent_runtime.get('accounts'), tencent_runtime)

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


def _tc3_sign(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

    secret_date = sign(('TC3' + secret_key).encode('utf-8'), date)
    secret_service = sign(secret_date, service)
    secret_signing = sign(secret_service, 'tc3_request')
    return hmac.new(secret_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()


def _tencent_api_request(action: str, payload: dict, secret_id: str, secret_key: str,
                         region: str) -> dict:
    service = 'asr'
    host = 'asr.tencentcloudapi.com'
    content_type = 'application/json; charset=utf-8'
    timestamp = int(time.time())
    date = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime('%Y-%m-%d')
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    hashed_payload = hashlib.sha256(payload_json.encode('utf-8')).hexdigest()

    canonical_headers = (
        f'content-type:{content_type}\n'
        f'host:{host}\n'
        f'x-tc-action:{action.lower()}\n'
    )
    signed_headers = 'content-type;host;x-tc-action'
    canonical_request = (
        'POST\n'
        '/\n'
        '\n'
        f'{canonical_headers}\n'
        f'{signed_headers}\n'
        f'{hashed_payload}'
    )

    credential_scope = f'{date}/{service}/tc3_request'
    string_to_sign = (
        'TC3-HMAC-SHA256\n'
        f'{timestamp}\n'
        f'{credential_scope}\n'
        f'{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'
    )
    signature = _tc3_sign(secret_key, date, service, string_to_sign)
    authorization = (
        'TC3-HMAC-SHA256 '
        f'Credential={secret_id}/{credential_scope}, '
        f'SignedHeaders={signed_headers}, '
        f'Signature={signature}'
    )

    req = Request(
        TENCENT_ASR_ENDPOINT,
        data=payload_json.encode('utf-8'),
        method='POST',
    )
    req.add_header('Authorization', authorization)
    req.add_header('Content-Type', content_type)
    req.add_header('Host', host)
    req.add_header('X-TC-Action', action)
    req.add_header('X-TC-Version', TENCENT_ASR_VERSION)
    req.add_header('X-TC-Timestamp', str(timestamp))
    req.add_header('X-TC-Region', region)

    with urlopen(req, timeout=60) as response:
        body = response.read().decode('utf-8')
    parsed = json.loads(body)
    resp = parsed.get('Response', {})
    if 'Error' in resp:
        err = resp.get('Error') or {}
        code = err.get('Code', 'UnknownError')
        msg = err.get('Message', '')
        raise RuntimeError(f'{code}: {msg}')
    return parsed


def get_tencent_usage_by_date(secret_id: str,
                              secret_key: str,
                              region: str,
                              start_date: str,
                              end_date: str,
                              biz_names: list[str] | None = None) -> dict:
    return _tencent_api_request(
        action='GetUsageByDate',
        payload={
            'BizNameList': biz_names or ['asr_rec'],
            'StartDate': start_date,
            'EndDate': end_date,
        },
        secret_id=secret_id,
        secret_key=secret_key,
        region=region,
    )


def get_tencent_usage_by_date_with_fallback(secret_id: str,
                                            secret_key: str,
                                            region: str,
                                            start_date: str,
                                            end_date: str,
                                            biz_names: list[str] | None = None) -> tuple[dict, str]:
    tried: list[str] = []
    for current_region in [region, *TENCENT_USAGE_REGION_FALLBACKS]:
        current_region = (current_region or '').strip()
        if not current_region or current_region in tried:
            continue
        tried.append(current_region)
        try:
            return (
                get_tencent_usage_by_date(
                    secret_id=secret_id,
                    secret_key=secret_key,
                    region=current_region,
                    start_date=start_date,
                    end_date=end_date,
                    biz_names=biz_names,
                ),
                current_region,
            )
        except Exception as exc:
            if 'UnsupportedRegion' not in str(exc):
                raise
    raise RuntimeError(f'UnsupportedRegion for usage query, tried={",".join(tried)}')


def summarize_tencent_usage(accounts: list[dict],
                            start_date: str,
                            end_date: str,
                            biz_names: list[str] | None = None) -> dict:
    requested_biz_names = biz_names or ['asr_rec']
    items: list[dict] = []
    total_duration = 0
    total_count = 0
    for idx, account in enumerate(accounts):
        current = {
            'name': account.get('name') or f'account-{idx + 1}',
            'secret_id_masked': _mask_secret(str(account.get('secret_id') or '')),
            'region': account.get('region') or 'ap-beijing',
            'monthly_quota_seconds': int(account.get('monthly_quota_seconds') or 0),
            'monthly_quota_hours': _seconds_to_hours(int(account.get('monthly_quota_seconds') or 0)),
            'start_date': start_date,
            'end_date': end_date,
            'biz_names': requested_biz_names,
        }
        try:
            resp, used_region = get_tencent_usage_by_date_with_fallback(
                secret_id=str(account.get('secret_id') or ''),
                secret_key=str(account.get('secret_key') or ''),
                region=str(account.get('region') or 'ap-beijing'),
                start_date=start_date,
                end_date=end_date,
                biz_names=requested_biz_names,
            )
            usage_list = (((resp.get('Response') or {}).get('Data') or {}).get('UsageByDateInfoList') or [])
            current['usage_region'] = used_region
            used_duration = sum(int(item.get('Duration') or 0) for item in usage_list if isinstance(item, dict))
            used_count = sum(int(item.get('Count') or 0) for item in usage_list if isinstance(item, dict))
            current['usage'] = usage_list
            current['used_duration_seconds'] = used_duration
            current['used_duration_hours'] = _seconds_to_hours(used_duration)
            current['used_count'] = used_count
            if current['monthly_quota_seconds'] > 0:
                current['remaining_quota_seconds'] = max(0, current['monthly_quota_seconds'] - used_duration)
                current['remaining_quota_hours'] = _seconds_to_hours(current['remaining_quota_seconds'])
            total_duration += used_duration
            total_count += used_count
        except Exception as exc:
            current['error'] = str(exc)
        items.append(current)
    return {
        'status': 'ok',
        'start_date': start_date,
        'end_date': end_date,
        'biz_names': requested_biz_names,
        'account_count': len(items),
        'total_used_duration_seconds': total_duration,
        'total_used_duration_hours': _seconds_to_hours(total_duration),
        'total_used_count': total_count,
        'accounts': items,
        'selection_strategy': 'highest_remaining_quota_with_short_term_reservation',
        'note': 'Tencent Cloud GetUsageByDate returns usage only. remaining_quota_seconds is computed from local monthly_quota_seconds when configured.',
    }


def transcribe_with_tencent(url: str,
                            language: str | None,
                            tencent_secret_id: str,
                            tencent_secret_key: str,
                            tencent_region: str,
                            tencent_engine_model_type: str,
                            tencent_channel_num: int,
                            tencent_res_text_format: int,
                            tencent_quality_mode: str,
                            tencent_hotword_id: str,
                            tencent_hotword_list: str,
                            tencent_convert_num_mode: int,
                            tencent_filter_modal: int,
                            tencent_filter_punc: int,
                            tencent_filter_dirty: int,
                            tencent_poll_interval: int,
                            tencent_poll_timeout: int) -> Dict:
    if not tencent_secret_id or not tencent_secret_key:
        raise RuntimeError('Tencent ASR credentials missing: set tencent.secret_id / tencent.secret_key')

    quality_mode = (tencent_quality_mode or 'standard').strip().lower()
    engine_model_type = tencent_engine_model_type
    res_text_format = int(tencent_res_text_format)
    convert_num_mode = int(tencent_convert_num_mode)
    filter_modal = int(tencent_filter_modal)
    filter_punc = int(tencent_filter_punc)
    filter_dirty = int(tencent_filter_dirty)
    hotword_id = (tencent_hotword_id or '').strip()
    hotword_list = (tencent_hotword_list or '').strip()
    if quality_mode == 'max':
        # Prefer large model + rich formatted output for better readability/accuracy.
        if engine_model_type == '16k_zh':
            engine_model_type = '16k_zh_large'
        if res_text_format < 3:
            res_text_format = 3
        convert_num_mode = 1
        filter_modal = max(filter_modal, 1)

    create_payload = {
        'EngineModelType': engine_model_type,
        'ChannelNum': int(tencent_channel_num),
        'ResTextFormat': res_text_format,
        'SourceType': 0,
        'Url': url,
        'ConvertNumMode': convert_num_mode,
        'FilterModal': filter_modal,
        'FilterPunc': filter_punc,
        'FilterDirty': filter_dirty,
    }
    if hotword_id:
        create_payload['HotwordId'] = hotword_id
    if hotword_list:
        create_payload['HotwordList'] = hotword_list
    create_resp = _tencent_api_request(
        action='CreateRecTask',
        payload=create_payload,
        secret_id=tencent_secret_id,
        secret_key=tencent_secret_key,
        region=tencent_region,
    )
    task_id = (((create_resp.get('Response') or {}).get('Data') or {}).get('TaskId'))
    if not task_id:
        raise RuntimeError('Tencent ASR create task failed: missing TaskId')

    start = time.time()
    poll_interval = max(1, int(tencent_poll_interval))
    timeout_sec = max(10, int(tencent_poll_timeout))

    while True:
        if time.time() - start > timeout_sec:
            raise RuntimeError(f'Tencent ASR polling timeout ({timeout_sec}s), task_id={task_id}')

        status_resp = _tencent_api_request(
            action='DescribeTaskStatus',
            payload={'TaskId': int(task_id)},
            secret_id=tencent_secret_id,
            secret_key=tencent_secret_key,
            region=tencent_region,
        )
        data = ((status_resp.get('Response') or {}).get('Data') or {})
        status_str = str(data.get('StatusStr') or '').lower()
        status_code = int(data.get('Status') or 0)
        if status_str == 'success' or status_code == 2:
            text = str(data.get('Result') or '').strip()
            if not text:
                details = data.get('ResultDetail') or []
                lines = []
                for item in details:
                    one = str((item or {}).get('FinalSentence') or '').strip()
                    if one:
                        lines.append(one)
                text = '\n'.join(lines).strip()
            text = to_simplified_chinese(text)
            return {
                'url': url,
                'status': 'ok',
                'task': 'audio',
                'text': text,
                'language': language or data.get('LangType') or 'zh',
                'duration': None,
                'engine': 'tencent-asr',
                'model': engine_model_type,
                'model_path': None,
                'audio_chunk_seconds': 0,
                'chunk_count': 1,
                'tencent_task_id': int(task_id),
                'tencent_quality_mode': quality_mode,
                'tencent_res_text_format': res_text_format,
            }
        if status_str == 'failed' or status_code == 3:
            err_msg = str(data.get('ErrorMsg') or 'Tencent ASR task failed')
            raise RuntimeError(err_msg)
        time.sleep(poll_interval)


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


class ResultCache:
    def __init__(self, cache_dir: Path, max_entries: int, max_size_mb: int) -> None:
        self._cache_dir = cache_dir
        self._entries_dir = cache_dir / 'entries'
        self._index_file = cache_dir / 'index.json'
        self._max_entries = max(1, int(max_entries))
        self._max_size_bytes = max(1, int(max_size_mb)) * 1024 * 1024
        self._lock = threading.Lock()
        self._index: Dict[str, Dict] = {}
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._entries_dir.mkdir(parents=True, exist_ok=True)
        self._load_index()

    @staticmethod
    def _key_for_url(url: str) -> str:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

    @staticmethod
    def _now() -> Tuple[float, str]:
        ts = time.time()
        return ts, datetime.datetime.fromtimestamp(ts).isoformat(timespec='seconds')

    def _entry_file(self, key: str) -> Path:
        return self._entries_dir / f'{key}.json'

    def _write_json_atomic(self, path: Path, payload: Dict) -> int:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(raw)
        tmp.replace(path)
        return len(raw)

    def _load_index(self) -> None:
        try:
            data = json.loads(self._index_file.read_text(encoding='utf-8'))
            entries = data.get('entries', {}) if isinstance(data, dict) else {}
            if isinstance(entries, dict):
                self._index = entries
        except Exception:
            self._index = {}

    def _save_index_locked(self) -> None:
        payload = {
            'version': 1,
            'updated_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'entries': self._index,
        }
        self._write_json_atomic(self._index_file, payload)

    def _delete_entry_locked(self, key: str) -> None:
        self._index.pop(key, None)
        try:
            self._entry_file(key).unlink(missing_ok=True)
        except Exception:
            pass

    def _evict_locked(self) -> None:
        total_size = sum(int(meta.get('size_bytes', 0) or 0) for meta in self._index.values())
        while len(self._index) > self._max_entries or total_size > self._max_size_bytes:
            victim_key = None
            victim_access_ts = None
            for key, meta in self._index.items():
                access_ts = float(meta.get('last_access_ts') or meta.get('created_ts') or 0)
                if victim_key is None or access_ts < victim_access_ts:
                    victim_key = key
                    victim_access_ts = access_ts
            if victim_key is None:
                break
            total_size -= int(self._index.get(victim_key, {}).get('size_bytes', 0) or 0)
            self._delete_entry_locked(victim_key)

    def get(self, url: str) -> Dict | None:
        key = self._key_for_url(url)
        with self._lock:
            meta = self._index.get(key)
            if not meta:
                return None
            entry_file = self._entry_file(key)
            if not entry_file.exists():
                self._delete_entry_locked(key)
                self._save_index_locked()
                return None
            try:
                payload = json.loads(entry_file.read_text(encoding='utf-8'))
                result = payload.get('result') if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    raise RuntimeError('invalid_cache_payload')
            except Exception:
                self._delete_entry_locked(key)
                self._save_index_locked()
                return None

            now_ts, now_iso = self._now()
            meta['last_access_ts'] = now_ts
            meta['last_access_at'] = now_iso
            payload['last_access_at'] = now_iso
            payload['last_access_ts'] = now_ts
            try:
                size_bytes = self._write_json_atomic(entry_file, payload)
                meta['size_bytes'] = size_bytes
            except Exception:
                self._delete_entry_locked(key)
                self._save_index_locked()
                return None
            self._save_index_locked()
            return result

    def put(self, url: str, result: Dict) -> None:
        # Cache only successful audio transcripts.
        if result.get('status') != 'ok' or result.get('task') != 'audio':
            return
        key = self._key_for_url(url)
        now_ts, now_iso = self._now()
        with self._lock:
            prev = self._index.get(key, {})
            created_ts = float(prev.get('created_ts') or now_ts)
            created_at = str(prev.get('created_at') or now_iso)
            cached_result = dict(result)
            cached_result.pop('cache_hit', None)
            cached_result.pop('duration_ms', None)
            cached_result.pop('transcription_source', None)
            entry_payload = {
                'url': url,
                'created_at': created_at,
                'created_ts': created_ts,
                'updated_at': now_iso,
                'updated_ts': now_ts,
                'last_access_at': now_iso,
                'last_access_ts': now_ts,
                'status': cached_result.get('status'),
                'task': cached_result.get('task'),
                'text': cached_result.get('text', ''),
                'result': cached_result,
            }
            entry_file = self._entry_file(key)
            size_bytes = self._write_json_atomic(entry_file, entry_payload)
            self._index[key] = {
                'key': key,
                'url': url,
                'entry_file': str(entry_file.relative_to(self._cache_dir)),
                'size_bytes': size_bytes,
                'status': entry_payload['status'],
                'task': entry_payload['task'],
                'created_at': created_at,
                'created_ts': created_ts,
                'updated_at': now_iso,
                'updated_ts': now_ts,
                'last_access_at': now_iso,
                'last_access_ts': now_ts,
            }
            self._evict_locked()
            self._save_index_locked()


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
                   asr_provider: str,
                   tencent_secret_id: str,
                   tencent_secret_key: str,
                   tencent_region: str,
                   tencent_engine_model_type: str,
                   tencent_channel_num: int,
                   tencent_res_text_format: int,
                   tencent_quality_mode: str,
                   tencent_hotword_id: str,
                   tencent_hotword_list: str,
                   tencent_convert_num_mode: int,
                   tencent_filter_modal: int,
                   tencent_filter_punc: int,
                   tencent_filter_dirty: int,
                   tencent_poll_interval: int,
                   tencent_poll_timeout: int,
                   tencent_account_pool: TencentCredentialPool | None,
                   task: str,
                   image_ocr_provider: str,
                   ai_model: str,
                   ai_endpoint: str,
                   ai_timeout: int,
                   ai_api_key: str | None,
                   result_cache: ResultCache | None = None) -> Dict:
    if result_cache is not None and task != 'image':
        cached = result_cache.get(url)
        if cached is not None:
            cache_result = dict(cached)
            cache_result['cache_hit'] = True
            return cache_result

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
                'cache_hit': False,
            }

        if asr_provider == 'tencent':
            selected_account = None
            effective_secret_id = tencent_secret_id
            effective_secret_key = tencent_secret_key
            effective_region = tencent_region
            if not effective_secret_id or not effective_secret_key:
                selected_account = tencent_account_pool.next_account() if tencent_account_pool is not None else None
                if selected_account is not None:
                    effective_secret_id = str(selected_account.get('secret_id') or '')
                    effective_secret_key = str(selected_account.get('secret_key') or '')
                    effective_region = str(selected_account.get('region') or effective_region or 'ap-beijing')
            result = transcribe_with_tencent(
                url=url,
                language=language,
                tencent_secret_id=effective_secret_id,
                tencent_secret_key=effective_secret_key,
                tencent_region=effective_region,
                tencent_engine_model_type=tencent_engine_model_type,
                tencent_channel_num=tencent_channel_num,
                tencent_res_text_format=tencent_res_text_format,
                tencent_quality_mode=tencent_quality_mode,
                tencent_hotword_id=tencent_hotword_id,
                tencent_hotword_list=tencent_hotword_list,
                tencent_convert_num_mode=tencent_convert_num_mode,
                tencent_filter_modal=tencent_filter_modal,
                tencent_filter_punc=tencent_filter_punc,
                tencent_filter_dirty=tencent_filter_dirty,
                tencent_poll_interval=tencent_poll_interval,
                tencent_poll_timeout=tencent_poll_timeout,
            )
            if result_cache is not None:
                try:
                    result_cache.put(url, result)
                except Exception:
                    pass
            if selected_account is not None:
                result['tencent_account_name'] = selected_account.get('name') or 'default'
                result['tencent_secret_id_masked'] = _mask_secret(effective_secret_id)
                result['tencent_region'] = effective_region
                result['tencent_selection_strategy'] = selected_account.get('_selection_strategy', 'round_robin')
                if '_selection_remaining_quota_seconds' in selected_account:
                    result['tencent_selection_remaining_quota_hours'] = _seconds_to_hours(
                        int(selected_account.get('_selection_remaining_quota_seconds') or 0)
                    )
            result['cache_hit'] = False
            return result

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

        result = {
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
        if result_cache is not None:
            try:
                result_cache.put(url, result)
            except Exception:
                pass
        result['cache_hit'] = False
        return result
    except Exception as err:  # pragma: no cover - entrypoint handling
        return {
            'url': url,
            'status': 'error',
            'error': str(err),
            'cache_hit': False,
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
    parser.add_argument('--asr-provider',
                        default=os.getenv('ASR_PROVIDER', str(RUNTIME_CONFIG['asr']['default_provider'])),
                        choices=['local', 'tencent'],
                        help='Audio ASR provider: local or tencent')
    parser.add_argument('--cache-dir',
                        default=os.getenv('RESULT_CACHE_DIR', str(DEFAULT_RESULT_CACHE_DIR)),
                        help='Result cache directory (default: ./cache/transcribe_result)')
    parser.add_argument('--cache-max-entries',
                        type=int,
                        default=int(os.getenv('RESULT_CACHE_MAX_ENTRIES', str(DEFAULT_RESULT_CACHE_MAX_ENTRIES))),
                        help='Result cache max entry count (LRU)')
    parser.add_argument('--cache-max-size-mb',
                        type=int,
                        default=int(os.getenv('RESULT_CACHE_MAX_SIZE_MB', str(DEFAULT_RESULT_CACHE_MAX_SIZE_MB))),
                        help='Result cache max total size in MB (LRU)')
    parser.add_argument('--no-result-cache',
                        action='store_true',
                        help='Disable local URL result cache')
    parser.add_argument('--tencent-secret-id',
                        default=os.getenv('TENCENT_SECRET_ID', str(RUNTIME_CONFIG['asr']['tencent']['secret_id'])),
                        help='Tencent Cloud SecretId')
    parser.add_argument('--tencent-secret-key',
                        default=os.getenv('TENCENT_SECRET_KEY', str(RUNTIME_CONFIG['asr']['tencent']['secret_key'])),
                        help='Tencent Cloud SecretKey')
    parser.add_argument('--tencent-region',
                        default=os.getenv('TENCENT_REGION', str(RUNTIME_CONFIG['asr']['tencent']['region'])),
                        help='Tencent Cloud ASR region')
    parser.add_argument('--tencent-engine-model-type',
                        default=os.getenv('TENCENT_ENGINE_MODEL_TYPE',
                                          str(RUNTIME_CONFIG['asr']['tencent']['engine_model_type'])),
                        help='Tencent ASR EngineModelType')
    parser.add_argument('--tencent-channel-num',
                        type=int,
                        default=int(os.getenv('TENCENT_CHANNEL_NUM',
                                              str(RUNTIME_CONFIG['asr']['tencent']['channel_num']))),
                        help='Tencent ASR ChannelNum')
    parser.add_argument('--tencent-res-text-format',
                        type=int,
                        default=int(os.getenv('TENCENT_RES_TEXT_FORMAT',
                                              str(RUNTIME_CONFIG['asr']['tencent']['res_text_format']))),
                        help='Tencent ASR ResTextFormat')
    parser.add_argument('--tencent-quality-mode',
                        default=os.getenv('TENCENT_QUALITY_MODE',
                                          str(RUNTIME_CONFIG['asr']['tencent']['quality_mode'])),
                        choices=['standard', 'max'],
                        help='Tencent quality preset: standard/max')
    parser.add_argument('--tencent-hotword-id',
                        default=os.getenv('TENCENT_HOTWORD_ID',
                                          str(RUNTIME_CONFIG['asr']['tencent']['hotword_id'])),
                        help='Tencent HotwordId')
    parser.add_argument('--tencent-hotword-list',
                        default=os.getenv('TENCENT_HOTWORD_LIST',
                                          str(RUNTIME_CONFIG['asr']['tencent']['hotword_list'])),
                        help='Tencent HotwordList, words joined by |')
    parser.add_argument('--tencent-convert-num-mode',
                        type=int,
                        default=int(os.getenv('TENCENT_CONVERT_NUM_MODE',
                                              str(RUNTIME_CONFIG['asr']['tencent']['convert_num_mode']))),
                        help='Tencent ConvertNumMode')
    parser.add_argument('--tencent-filter-modal',
                        type=int,
                        default=int(os.getenv('TENCENT_FILTER_MODAL',
                                              str(RUNTIME_CONFIG['asr']['tencent']['filter_modal']))),
                        help='Tencent FilterModal')
    parser.add_argument('--tencent-filter-punc',
                        type=int,
                        default=int(os.getenv('TENCENT_FILTER_PUNC',
                                              str(RUNTIME_CONFIG['asr']['tencent']['filter_punc']))),
                        help='Tencent FilterPunc')
    parser.add_argument('--tencent-filter-dirty',
                        type=int,
                        default=int(os.getenv('TENCENT_FILTER_DIRTY',
                                              str(RUNTIME_CONFIG['asr']['tencent']['filter_dirty']))),
                        help='Tencent FilterDirty')
    parser.add_argument('--tencent-poll-interval',
                        type=int,
                        default=int(os.getenv('TENCENT_POLL_INTERVAL',
                                              str(RUNTIME_CONFIG['asr']['tencent']['poll_interval_seconds']))),
                        help='Tencent polling interval seconds')
    parser.add_argument('--tencent-poll-timeout',
                        type=int,
                        default=int(os.getenv('TENCENT_POLL_TIMEOUT',
                                              str(RUNTIME_CONFIG['asr']['tencent']['poll_timeout_seconds']))),
                        help='Tencent polling timeout seconds')


def run_transcribe_command(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_pool = ModelPool(model_dir)
    cache = None if args.no_result_cache else ResultCache(
        Path(args.cache_dir).expanduser().resolve(),
        args.cache_max_entries,
        args.cache_max_size_mb,
    )
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
        asr_provider=args.asr_provider,
        tencent_secret_id=args.tencent_secret_id,
        tencent_secret_key=args.tencent_secret_key,
        tencent_region=args.tencent_region,
        tencent_engine_model_type=args.tencent_engine_model_type,
        tencent_channel_num=args.tencent_channel_num,
        tencent_res_text_format=args.tencent_res_text_format,
        tencent_quality_mode=args.tencent_quality_mode,
        tencent_hotword_id=args.tencent_hotword_id,
        tencent_hotword_list=args.tencent_hotword_list,
        tencent_convert_num_mode=args.tencent_convert_num_mode,
        tencent_filter_modal=args.tencent_filter_modal,
        tencent_filter_punc=args.tencent_filter_punc,
        tencent_filter_dirty=args.tencent_filter_dirty,
        tencent_poll_interval=args.tencent_poll_interval,
        tencent_poll_timeout=args.tencent_poll_timeout,
        tencent_account_pool=TencentCredentialPool(RUNTIME_CONFIG['asr']['tencent'].get('accounts', [])),
        task=args.task,
        image_ocr_provider=args.image_ocr_provider,
        ai_model=args.ocr_model,
        ai_endpoint=args.ocr_api_endpoint,
        ai_timeout=args.ocr_timeout,
        ai_api_key=args.ocr_api_key,
        result_cache=cache,
    )

    print(json.dumps(result, ensure_ascii=False) if args.json else result.get('text', ''))
    return 0 if result.get('status') == 'ok' else 2


def serve_command(args: argparse.Namespace) -> int:
    host = args.host
    port = args.port
    model_pool = ModelPool(Path(args.model_dir).expanduser().resolve())
    cache = None if args.no_result_cache else ResultCache(
        Path(args.cache_dir).expanduser().resolve(),
        args.cache_max_entries,
        args.cache_max_size_mb,
    )
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
        'asr_provider': args.asr_provider,
        'tencent_secret_id': args.tencent_secret_id,
        'tencent_secret_key': args.tencent_secret_key,
        'tencent_region': args.tencent_region,
        'tencent_engine_model_type': args.tencent_engine_model_type,
        'tencent_channel_num': args.tencent_channel_num,
        'tencent_res_text_format': args.tencent_res_text_format,
        'tencent_quality_mode': args.tencent_quality_mode,
        'tencent_hotword_id': args.tencent_hotword_id,
        'tencent_hotword_list': args.tencent_hotword_list,
        'tencent_convert_num_mode': args.tencent_convert_num_mode,
        'tencent_filter_modal': args.tencent_filter_modal,
        'tencent_filter_punc': args.tencent_filter_punc,
        'tencent_filter_dirty': args.tencent_filter_dirty,
        'tencent_poll_interval': args.tencent_poll_interval,
        'tencent_poll_timeout': args.tencent_poll_timeout,
        'tencent_accounts': [dict(item) for item in RUNTIME_CONFIG['asr']['tencent'].get('accounts', [])],
        'task': args.task,
        'image_ocr_provider': args.image_ocr_provider,
        'ai_model': args.ocr_model,
        'ai_endpoint': args.ocr_api_endpoint,
        'ai_timeout': args.ocr_timeout,
        'ai_api_key': args.ocr_api_key,
        'cache_enabled': cache is not None,
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        server_version = 'transcribe-http/1.0'
        server_ctx = {
            'model_pool': model_pool,
            'result_cache': cache,
            'defaults': defaults,
            'tencent_account_pool': TencentCredentialPool(defaults['tencent_accounts']),
        }

        def _json_resp(self, payload: Dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _file_resp(self, path: Path, content_type: str, status: int = 200) -> None:
            data = path.read_bytes()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _error(self, status: int, message: str) -> None:
            self._json_resp({'status': 'error', 'error': message}, status)

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                if parsed.path in {'/', '/index.html'}:
                    if DEFAULT_INDEX_FILE.exists():
                        self._file_resp(DEFAULT_INDEX_FILE, 'text/html; charset=utf-8')
                    else:
                        self._error(404, 'index.html not found')
                    return
                if parsed.path == '/health':
                    self._json_resp({'status': 'ok'})
                    return
                if parsed.path == '/tencent/quota':
                    pool = self.server_ctx['tencent_account_pool']
                    cfg = self.server_ctx['defaults']
                    accounts = cfg.get('tencent_accounts') or []
                    if not accounts:
                        self._error(400, 'Tencent account pool not configured')
                        return
                    params = parse_qs(parsed.query or '')
                    start_date = (params.get('start_date') or [datetime.date.today().replace(day=1).isoformat()])[0]
                    end_date = (params.get('end_date') or [datetime.date.today().isoformat()])[0]
                    biz_names_raw = (params.get('biz_names') or ['asr_rec'])[0]
                    biz_names = [item.strip() for item in biz_names_raw.split(',') if item.strip()]
                    force_refresh = (params.get('refresh') or ['0'])[0] in {'1', 'true', 'yes'}
                    if (
                        pool is not None
                        and start_date == datetime.date.today().replace(day=1).isoformat()
                        and end_date == datetime.date.today().isoformat()
                        and biz_names == ['asr_rec']
                    ):
                        summary = pool.quota_summary(force_refresh=force_refresh)
                    else:
                        summary = summarize_tencent_usage(accounts, start_date, end_date, biz_names)
                    self._json_resp(summary)
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
                    asr_provider=payload.get('asr_provider', cfg['asr_provider']),
                    tencent_secret_id=payload.get('tencent_secret_id', cfg['tencent_secret_id']),
                    tencent_secret_key=payload.get('tencent_secret_key', cfg['tencent_secret_key']),
                    tencent_region=payload.get('tencent_region', cfg['tencent_region']),
                    tencent_engine_model_type=payload.get('tencent_engine_model_type', cfg['tencent_engine_model_type']),
                    tencent_channel_num=int(payload.get('tencent_channel_num', cfg['tencent_channel_num'])),
                    tencent_res_text_format=int(payload.get('tencent_res_text_format', cfg['tencent_res_text_format'])),
                    tencent_quality_mode=payload.get('tencent_quality_mode', cfg['tencent_quality_mode']),
                    tencent_hotword_id=payload.get('tencent_hotword_id', cfg['tencent_hotword_id']),
                    tencent_hotword_list=payload.get('tencent_hotword_list', cfg['tencent_hotword_list']),
                    tencent_convert_num_mode=int(payload.get('tencent_convert_num_mode', cfg['tencent_convert_num_mode'])),
                    tencent_filter_modal=int(payload.get('tencent_filter_modal', cfg['tencent_filter_modal'])),
                    tencent_filter_punc=int(payload.get('tencent_filter_punc', cfg['tencent_filter_punc'])),
                    tencent_filter_dirty=int(payload.get('tencent_filter_dirty', cfg['tencent_filter_dirty'])),
                    tencent_poll_interval=int(payload.get('tencent_poll_interval', cfg['tencent_poll_interval'])),
                    tencent_poll_timeout=int(payload.get('tencent_poll_timeout', cfg['tencent_poll_timeout'])),
                    tencent_account_pool=self.server_ctx['tencent_account_pool'],
                    task=payload.get('task') or ('image' if self.path == '/ocr' else cfg['task']),
                    image_ocr_provider=payload.get('image_ocr_provider', cfg['image_ocr_provider']),
                    ai_model=payload.get('ocr_model', cfg['ai_model']),
                    ai_endpoint=payload.get('ocr_api_endpoint', cfg['ai_endpoint']),
                    ai_timeout=int(payload.get('ocr_timeout', cfg['ai_timeout'])),
                    ai_api_key=payload.get('ocr_api_key', cfg['ai_api_key']),
                    result_cache=self.server_ctx['result_cache'] if self.path == '/transcribe' else None,
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
        '--asr-provider', args.asr_provider,
        '--cache-dir', args.cache_dir,
        '--cache-max-entries', str(args.cache_max_entries),
        '--cache-max-size-mb', str(args.cache_max_size_mb),
        '--tencent-secret-id', args.tencent_secret_id,
        '--tencent-secret-key', args.tencent_secret_key,
        '--tencent-region', args.tencent_region,
        '--tencent-engine-model-type', args.tencent_engine_model_type,
        '--tencent-channel-num', str(args.tencent_channel_num),
        '--tencent-res-text-format', str(args.tencent_res_text_format),
        '--tencent-quality-mode', args.tencent_quality_mode,
        '--tencent-hotword-id', args.tencent_hotword_id,
        '--tencent-hotword-list', args.tencent_hotword_list,
        '--tencent-convert-num-mode', str(args.tencent_convert_num_mode),
        '--tencent-filter-modal', str(args.tencent_filter_modal),
        '--tencent-filter-punc', str(args.tencent_filter_punc),
        '--tencent-filter-dirty', str(args.tencent_filter_dirty),
        '--tencent-poll-interval', str(args.tencent_poll_interval),
        '--tencent-poll-timeout', str(args.tencent_poll_timeout),
    ]
    if args.no_vad:
        cmd.append('--no-vad')
    if args.no_result_cache:
        cmd.append('--no-result-cache')

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
