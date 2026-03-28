"""
AceRadio v1.0
Built on top of Ace-Step v1.5

Copyright (C) 2026 Marco Robustini [Marcopter]

This file is part of AceRadio.
AceRadio is licensed under the GNU General Public License v3.0 or later.

You may redistribute and/or modify this software under the terms
of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or any later version.

AceRadio is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU General Public License for more details.
"""

from __future__ import annotations

import asyncio, contextlib, gc, json, logging, math, os, random, re, shutil, struct, time, uuid, socket, subprocess, threading
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote

try:
    import torch
except Exception:
    torch = None
from typing import Any, Optional

import hashlib, hmac, secrets
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acestep.constants import VALID_LANGUAGES
from .runtime_engine import create_app as create_engine_app
from .jingle_manager import JingleManager
from .playout_engine import RealtimePlayoutEngine

logger = logging.getLogger(__name__)
OLLAMA_MODEL = os.getenv('ACERADIO_OLLAMA_MODEL', os.getenv('OLLAMA_MODEL', 'qwen3.5:4b'))
OLLAMA_BASE_URL = os.getenv('ACERADIO_OLLAMA_BASE_URL', os.getenv('OLLAMA_HOST', 'http://127.0.0.1:11434')).rstrip('/')
OLLAMA_KEEP_ALIVE = os.getenv('ACERADIO_OLLAMA_KEEP_ALIVE', '0')
OLLAMA_CHAT_READ_TIMEOUT = max(30.0, float(os.getenv('ACERADIO_OLLAMA_CHAT_READ_TIMEOUT', '120.0')))
OLLAMA_CHAT_CONNECT_TIMEOUT = max(3.0, float(os.getenv('ACERADIO_OLLAMA_CHAT_CONNECT_TIMEOUT', '10.0')))
OLLAMA_CHAT_RETRIES = max(1, int(os.getenv('ACERADIO_OLLAMA_CHAT_RETRIES', '3')))
OLLAMA_CHAT_RETRY_BACKOFF = max(0.1, float(os.getenv('ACERADIO_OLLAMA_CHAT_RETRY_BACKOFF', '2.0')))
POLL_INTERVAL_S = float(os.getenv('ACERADIO_POLL_INTERVAL_S', '2.0'))
JOB_POLL_TOTAL_TIMEOUT = max(60.0, float(os.getenv('ACERADIO_JOB_POLL_TOTAL_TIMEOUT', '600.0')))
PLAYER_AUDIO_FORMAT = os.getenv('ACERADIO_AUDIO_FORMAT', 'mp3').strip().lower() or 'mp3'
ACERADIO_MP3_BITRATE_OPTIONS = ('128k', '192k', '256k', '320k')
ACERADIO_MP3_SAMPLE_RATE_OPTIONS = (48000, 44100)
ACERADIO_MP3_DEFAULT_BITRATE = '128k'
ACERADIO_MP3_DEFAULT_SAMPLE_RATE = 48000
RESERVOIR_TARGET = max(10, int(os.getenv('ACERADIO_RESERVOIR_TARGET', '10')))
RESERVOIR_REFILL_THRESHOLD = max(1, int(os.getenv('ACERADIO_RESERVOIR_REFILL_THRESHOLD', '3')))
OUTPUTS_ROOT = Path(os.getenv('ACERADIO_OUTPUTS_ROOT', str(Path.cwd() / 'aceradio_outputs'))).resolve()
CONFIGS_DIR = (OUTPUTS_ROOT / 'configs').resolve()
SYSTEM_CONFIG_DIR = (CONFIGS_DIR / 'system').resolve()
DEFAULT_SETTINGS_FILENAME = 'aceradio_config.json'
DEFAULT_SETTINGS_PATH = (CONFIGS_DIR / DEFAULT_SETTINGS_FILENAME).resolve()
LAST_USED_SETTINGS_FILENAME = 'last_used_config.json'
LAST_USED_SETTINGS_PATH = (SYSTEM_CONFIG_DIR / LAST_USED_SETTINGS_FILENAME).resolve()
LEGACY_LAST_USED_SETTINGS_PATH = (OUTPUTS_ROOT / LAST_USED_SETTINGS_FILENAME).resolve()
SETTINGS_PATH = Path(os.getenv('ACERADIO_SETTINGS_PATH', str(DEFAULT_SETTINGS_PATH)))


def _coerce_settings_path(value: Any) -> Path:
    raw = str(value or '').strip()
    if not raw:
        return SETTINGS_PATH
    path = Path(raw).expanduser()
    if path.suffix.lower() != '.json':
        path = path.with_suffix('.json')
    return path.resolve()

def _set_settings_path(value: Any) -> Path:
    global SETTINGS_PATH
    SETTINGS_PATH = _coerce_settings_path(value)
    return SETTINGS_PATH


def _configs_browse_dir() -> Path:
    _ensure_outputs_layout()
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIGS_DIR


def _read_last_used_settings_path() -> Path | None:
    try:
        candidate_paths = (LAST_USED_SETTINGS_PATH, LEGACY_LAST_USED_SETTINGS_PATH)
        for candidate in candidate_paths:
            if not candidate.exists():
                continue
            raw = json.loads(candidate.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                continue
            chosen = str(raw.get('last_used_config') or '').strip()
            if not chosen:
                continue
            return _coerce_settings_path(chosen)
        return None
    except Exception:
        logger.exception('Failed to read last used AceRadio settings path')
        return None


def _write_last_used_settings_path(path: Any) -> None:
    try:
        target = _coerce_settings_path(path)
        _configs_browse_dir()
        payload = {
            'last_used_config': str(target),
            'updated_at': time.time(),
        }
        LAST_USED_SETTINGS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
        if LEGACY_LAST_USED_SETTINGS_PATH != LAST_USED_SETTINGS_PATH and LEGACY_LAST_USED_SETTINGS_PATH.exists():
            try:
                LEGACY_LAST_USED_SETTINGS_PATH.unlink()
            except Exception:
                pass
    except Exception:
        logger.exception('Failed to persist last used AceRadio settings path')


def _resolve_startup_settings_target() -> tuple[Path | None, str, str]:
    _configs_browse_dir()
    remembered = _read_last_used_settings_path()
    if remembered and remembered.exists():
        return remembered, 'ok', f'Startup config loaded: {remembered.name}'
    if remembered and not remembered.exists():
        missing_msg = f'Last used config not found: {remembered}'
        if DEFAULT_SETTINGS_PATH.exists():
            return DEFAULT_SETTINGS_PATH, 'error', f'{missing_msg} · fallback to default config.'
        return None, 'error', f'{missing_msg} · default config not found in {CONFIGS_DIR}.'
    if DEFAULT_SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS_PATH, 'ok', f'Startup config loaded: {DEFAULT_SETTINGS_PATH.name}'
    return None, 'error', f'Default config not found in {CONFIGS_DIR}. No config loaded at startup.'
SONGS_PATH = Path(__file__).with_name('songs.json')
ACERADIO_TRACK_META_FILENAME = 'aceradio_track.json'
SONGS_EXTERNAL_GLOB = 'songs*.json'
GENERATED_SONGS_HISTORY_FILENAME = 'songs.generated.json'
GENERATED_SONGS_HISTORY_PATH = OUTPUTS_ROOT / GENERATED_SONGS_HISTORY_FILENAME
GENERATED_SONGS_DATED_PREFIX = 'songs.generated_'
GENERATED_SONGS_DATED_GLOB = f'{GENERATED_SONGS_DATED_PREFIX}*.json'
CUSTOM_CATALOG_DIR = OUTPUTS_ROOT / 'catalogs'
CUSTOM_CATALOG_FILENAME = 'custom_catalog.json'
EMBEDDED_CUSTOM_CATALOG_KEY = 'custom_catalog_snapshot'
CUSTOM_CATALOG_PATH = CUSTOM_CATALOG_DIR / CUSTOM_CATALOG_FILENAME


def _ensure_outputs_layout() -> None:
    for directory in (OUTPUTS_ROOT, CONFIGS_DIR, SYSTEM_CONFIG_DIR, CUSTOM_CATALOG_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception('Failed to create AceRadio directory: %s', directory)


_ensure_outputs_layout()
GENERATED_SONGS_ENTRY_KEYS = ('pack', 'title', 'description', 'style', 'lyrics', 'bpm', 'duration', 'keyscale', 'timesignature', 'vocal_language')
GENERATED_SONGS_LOCK = threading.Lock()
GENERATION_MODE_VALUES = {'ai_generated', 'local_catalog', 'hybrid'}
CATALOG_SOURCE_VALUES = {'library', 'generated', 'all_local', 'custom'}
LEGACY_GENERATION_SOURCE_VALUES = {'ai_generated', 'file', 'both', 'cache'}
VRAM_CLEANUP_MODE = os.getenv('ACERADIO_VRAM_CLEANUP', 'balanced').strip().lower() or 'balanced'
DEFAULT_MAX_SAVED_TRACKS = max(1, int(os.getenv('ACERADIO_MAX_SAVED_TRACKS', '100')))
RECENTLY_PLAYED_LIMIT = max(1, int(os.getenv('ACERADIO_RECENTLY_PLAYED_LIMIT', '20')) )
OUTPUTS_CACHE_STABILIZE_SECONDS = max(2.0, float(os.getenv('ACERADIO_OUTPUTS_CACHE_STABILIZE_SECONDS', '8.0')))
OUTPUTS_CACHE_FINALIZE_GRACE_SECONDS = max(15.0, float(os.getenv('ACERADIO_OUTPUTS_CACHE_FINALIZE_GRACE_SECONDS', '120.0')))

JINGLE_SEP_TRIGGER_MAX_REMAINING_S = float(os.getenv('ACERADIO_JINGLE_SEP_TRIGGER_S', '20.0'))
JINGLE_SEP_TRIGGER_MIN_REMAINING_S = 3.0
JINGLE_OVERLAY_MID_WINDOW_S   = 3.0
JINGLE_OVERLAY_MIN_DURATION_S = 60.0
JINGLE_EVENT_EXPIRY_S        = 45.0
JINGLE_ACTIVE_EXPIRY_S       = 120.0
TRACK_START_FALLBACK_S       = 15.0
DEFAULT_GENRES = [
    'synthwave','dream pop','italo disco','indie rock','dark techno','jazz noir',
    'ambient','trap','drum and bass','post-rock','electro swing','cinematic pop',
    'progressive house','trip hop','industrial','flamenco pop','lo-fi hip hop','funk rock',
    'jazz','jazz fusion','soul','neo soul',
    'blues','blues rock','funk',
    'house','deep house','techno','minimal techno',
    'reggae','dub','disco','nu-disco',
    'R&B','folk','indie folk',
    'metal','doom metal','symphonic metal',
    'classical','orchestral','bossa nova',
    'latin','salsa','afrobeats',
    'vaporwave','shoegaze','post-punk',
    'new age','soundtrack','celtic',
]
DEFAULT_THEMES = [
    'love','heartbreak','loneliness','nightlife','city life','freedom','memories','hope','rebellion','social struggle',
    'dreams','nostalgia','loss','escape','desire','friendship','travel','nature','revenge','faith',
    'healing','betrayal','longing','regret','redemption','youth','aging','family','survival','homecoming',
    'ambition','identity','alienation','temptation','obsession','resilience','grief','celebration','self-discovery','change',
]
THEME_SIGNAL_HINTS = {
    'love': ['love', 'lover', 'romance', 'romantic', 'kiss', 'embrace', 'heart', 'together', 'belong'],
    'heartbreak': ['heartbreak', 'heartbroken', 'goodbye', 'farewell', 'left me', 'tears', 'shattered', 'apart'],
    'loneliness': ['lonely', 'alone', 'empty room', 'silence', 'isolated', 'nobody', 'solitude'],
    'nightlife': ['night', 'midnight', 'after dark', 'city lights', 'neon', 'dancefloor', 'club', 'last call'],
    'city life': ['city', 'street', 'traffic', 'subway', 'skyscraper', 'downtown', 'crowd'],
    'freedom': ['free', 'freedom', 'break the chains', 'open road', 'unbound', 'release', 'fly away'],
    'memories': ['memory', 'memories', 'remember', 'yesterday', 'old days', 'flashback', 'echoes'],
    'hope': ['hope', 'tomorrow', 'sunrise', 'light ahead', 'believe', 'hold on', 'better day'],
    'rebellion': ['rebel', 'rebellion', 'riot', 'fight back', 'defy', 'resist', 'revolt'],
    'social struggle': ['struggle', 'working class', 'system', 'hunger', 'poverty', 'justice', 'oppression'],
    'dreams': ['dream', 'dreams', 'vision', 'wish', 'fantasy', 'imagine', 'awakening'],
    'nostalgia': ['nostalgia', 'back then', 'used to', 'old days', 'golden days', 'throwback'],
    'loss': ['loss', 'gone', 'missing', 'without you', 'mourning', 'ghost', 'ashes'],
    'escape': ['escape', 'run away', 'get away', 'leave tonight', 'break free', 'out of here', 'way out', 'find a way out', 'slip away', 'open road', 'front door', 'miles away', 'far away', 'leave behind'],
    'desire': ['desire', 'want you', 'need you', 'burning', 'craving', 'fever', 'temptation'],
    'friendship': ['friend', 'friendship', 'side by side', 'stand by me', 'old friend', 'my crew'],
    'travel': ['road', 'journey', 'travel', 'train', 'highway', 'miles', 'destination'],
    'nature': ['river', 'ocean', 'forest', 'wind', 'rain', 'mountain', 'earth'],
    'revenge': ['revenge', 'payback', 'settle the score', 'vengeance', 'karma', 'retribution'],
    'faith': ['faith', 'pray', 'grace', 'believe', 'heaven', 'miracle', 'holy'],
    'healing': ['healing', 'heal', 'mend', 'recover', 'scar', 'breathe again'],
    'betrayal': ['betrayal', 'betrayed', 'liar', 'backstab', 'deceived', 'broken trust'],
    'longing': ['longing', 'ache', 'yearn', 'yearning', 'missing you', 'far away'],
    'regret': ['regret', 'sorry', 'too late', 'mistake', 'wish i knew', 'if only'],
    'redemption': ['redemption', 'redeem', 'forgiven', 'second chance', 'rise again'],
    'youth': ['youth', 'young', 'teenage', 'reckless nights', 'growing up'],
    'aging': ['aging', 'older', 'silver hair', 'passing years', 'time moves on'],
    'family': ['family', 'mother', 'father', 'brother', 'sister', 'home'],
    'survival': ['survival', 'survive', 'stay alive', 'hold the line', 'endure', 'last one standing'],
    'homecoming': ['homecoming', 'back home', 'coming home', 'returning', 'front door'],
    'ambition': ['ambition', 'climb higher', 'reach the top', 'chasing more', 'dream big'],
    'identity': ['identity', 'who i am', 'my name', 'true self', 'inside me'],
    'alienation': ['alienation', 'outsider', 'out of place', 'stranger here', 'disconnected'],
    'temptation': ['temptation', 'forbidden', 'pull me in', 'dangerous desire', 'sweet sin'],
    'obsession': ['obsession', 'obsessed', "can't let go", 'all i see', 'haunting me'],
    'resilience': ['resilience', 'bounce back', 'stronger now', 'still standing', "won't break"],
    'grief': ['grief', 'grieving', 'funeral', 'mourning', 'sorrow', 'hollow'],
    'celebration': ['celebration', 'celebrate', 'party', 'raise a glass', 'tonight we shine'],
    'self-discovery': ['self discovery', 'find myself', 'learn my heart', 'becoming me', 'inner voice'],
    'change': ['change', 'turn the page', 'new beginning', 'transformed', 'different now'],
}
THEME_SIGNAL_TOKEN_STOPWORDS = {'and', 'the', 'for', 'with', 'into', 'from', 'than', 'this', 'that', 'your', 'their'}
DEFAULT_GENRE_LOOKUP = {re.sub(r'\s+', ' ', str(x).strip().lower()): x for x in DEFAULT_GENRES}
DEFAULT_THEME_LOOKUP = {re.sub(r'\s+', ' ', str(x).strip().lower()): x for x in DEFAULT_THEMES}
LANGUAGE_DISPLAY_NAMES = {
    'en': 'English',
    'it': 'Italian',
    'es': 'Spanish',
    'fr': 'French',
    'de': 'German',
    'zh': 'Chinese (Mandarin)',
    'el': 'Greek',
    'fi': 'Finnish',
    'sv': 'Swedish',
    'ja': 'Japanese',
    'ko': 'Korean',
}
LANGUAGE_MARKERS = {
    'en': ('the', 'and', 'you', 'your', 'with', 'for', 'in', 'on', 'tonight', 'heart', 'night', 'when', 'never', 'hold', 'love'),
    'it': ('che', 'non', 'con', 'per', 'come', 'sono', 'sei', 'notte', 'cuore', 'vita', 'ancora', 'dentro', 'senza', 'mentre', 'voglio'),
    'es': ('que', 'con', 'para', 'como', 'eres', 'somos', 'noche', 'corazon', 'vida', 'todavia', 'dentro', 'sin', 'quiero', 'libertad', 'amor'),
    'fr': ('que', 'avec', 'pour', 'comme', 'dans', 'nuit', 'coeur', 'vie', 'encore', 'sans', 'jamais', 'amour', 'toi', 'moi', 'toujours'),
    'de': ('und', 'die', 'der', 'das', 'mit', 'fur', 'nacht', 'herz', 'leben', 'noch', 'nicht', 'immer', 'frei', 'liebe', 'durch'),
    'fi': ('ja', 'kun', 'ole', 'olen', 'sina', 'yossa', 'sydan', 'yo', 'elama', 'viela', 'ilman', 'rakkaus', 'vapaus', 'mina', 'sinut'),
    'sv': ('och', 'att', 'det', 'som', 'med', 'for', 'natt', 'hjarta', 'liv', 'igen', 'utan', 'aldrig', 'karlek', 'frihet', 'du'),
    'el': ('και', 'με', 'για', 'στη', 'στην', 'νυχτα', 'καρδια', 'ζωη', 'ακομα', 'χωρις', 'παντα', 'αγαπη', 'ελευθερια', 'ειμαι', 'σου'),
    'zh': ('的', '了', '在', '你', '我', '心', '夜', '自由', '爱', '梦'),
    'ja': ('の', 'に', 'を', 'て', 'で', '夜', '心', '愛', '自由', 'まだ'),
    'ko': ('의', '가', '을', '를', '에', '밤', '마음', '사랑', '자유', '아직'),
}
LANGUAGE_SCRIPT_PATTERNS = {
    'zh': re.compile(r'[\u4e00-\u9fff]'),
    'ja': re.compile(r'[\u3040-\u30ff]'),
    'ko': re.compile(r'[\uac00-\ud7af]'),
    'el': re.compile(r'[\u0370-\u03ff]'),
}
FORBIDDEN_LM_MARKERS = ('<think>', '</think>', '[NEXT]', '<|endoftext|>', '<|im_start|>', '<|im_end|>')
_LLM_TRAILING_ARTIFACT_RE = re.compile(r'(?is)(?:<\|(?:endoftext|im_start|im_end)[^>]*\|>|</?s>|<eos>|\[end\])')
_STAGE_DIRECTION_KEYWORDS = (
    'synth', 'drum', 'bass', 'guitar', 'pad', 'arpeggio', 'reverb', 'delay', 'fade', 'fades', 'fading',
    'instrumental', 'sample', 'swells', 'swell', 'beat', 'solo', 'traffic', 'noise', 'ambient', 'intro', 'outro',
)
OLLAMA_CONTENT_RETRIES = max(1, int(os.getenv('ACERADIO_OLLAMA_CONTENT_RETRIES', '2')))
def _model_name_lower(model_name: Optional[str]) -> str:
    return str(model_name or '').strip().lower()

def _is_sft_model(model_name: Optional[str]) -> bool:
    return 'sft' in _model_name_lower(model_name)

def _is_base_model(model_name: Optional[str]) -> bool:
    return 'base' in _model_name_lower(model_name)

def _is_turbo_model(model_name: Optional[str]) -> bool:
    name = _model_name_lower(model_name)
    return bool(name) and ('turbo' in name) and ('sft' not in name)

def _is_base_or_sft_model(model_name: Optional[str]) -> bool:
    return _is_sft_model(model_name) or _is_base_model(model_name)

def _default_inference_steps_for_model(model_name: Optional[str]) -> int:
    if _is_sft_model(model_name):
        return 50
    if _is_base_model(model_name):
        return 32
    return 8


def _max_inference_steps_for_model(model_name: Optional[str]) -> int:
    return 200


def _resolve_shift_for_model(model_name: Optional[str], requested_shift: Any = None) -> float:
    auto_shift = 1.0 if _is_base_or_sft_model(model_name) else 3.0
    if requested_shift is None or str(requested_shift).strip() == '':
        return auto_shift
    try:
        parsed = float(requested_shift)
    except Exception:
        return auto_shift
    if math.isnan(parsed) or math.isinf(parsed):
        return auto_shift
    return max(1.0, min(parsed, 5.0))


def _resolve_inference_steps_for_model(model_name: Optional[str], requested_steps: Any = None) -> int:
    auto_steps = _default_inference_steps_for_model(model_name)
    if requested_steps is None or str(requested_steps).strip() == '':
        return auto_steps
    try:
        parsed = int(float(requested_steps))
    except Exception:
        return auto_steps
    return max(1, min(parsed, _max_inference_steps_for_model(model_name)))

def _radio_model_inventory_from_options(data: Any) -> list[dict[str, Any]]:
    items = data.get('models') if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        tasks = item.get('supported_task_types')
        if isinstance(tasks, list):
            supported_task_types = [str(x).strip() for x in tasks if str(x).strip()]
        else:
            supported_task_types = []
        supports_radio = bool(item.get('supports_radio')) or ('text2music' in supported_task_types) or not supported_task_types
        out.append({
            'name': name,
            'is_default': bool(item.get('is_default')),
            'is_loaded': bool(item.get('is_loaded')),
            'supported_task_types': supported_task_types,
            'supports_radio': supports_radio,
        })
    return out

def _radio_compatible_model_inventory(data: Any) -> list[dict[str, Any]]:
    return [item for item in _radio_model_inventory_from_options(data) if bool(item.get('supports_radio'))]

def _radio_compatible_model_names(data: Any) -> list[str]:
    return [str(item.get('name') or '').strip() for item in _radio_compatible_model_inventory(data) if str(item.get('name') or '').strip()]

async def _fetch_engine_model_options(engine: 'EngineClient') -> dict[str, Any]:
    data = await engine.get_json('/api/options')
    return data if isinstance(data, dict) else {}

async def _fetch_engine_radio_model_inventory(engine: 'EngineClient') -> list[dict[str, Any]]:
    return _radio_compatible_model_inventory(await _fetch_engine_model_options(engine))

async def _ensure_engine_radio_model_selected(engine: 'EngineClient', model_name: Any, *, allow_default: bool = True) -> str:
    options = await _fetch_engine_model_options(engine)
    selected = str(model_name or '').strip()
    current_model = str(options.get('current_model') or '').strip()
    if not selected:
        if allow_default and current_model:
            return current_model
        raise HTTPException(status_code=400, detail='No DiT model selected')
    valid = set(_radio_compatible_model_names(options))
    if selected not in valid:
        raise HTTPException(status_code=400, detail=f'Model not available for AceRadio: {selected}')
    return selected

def _coerce_duration_value(value: Any, fallback: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(fallback)
    return max(30, min(600, parsed))

def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if math.isnan(parsed) or math.isinf(parsed):
        parsed = float(default)
    return max(float(minimum), min(float(maximum), parsed))

def _resolve_separator_start_before_end_s(raw: Any) -> float:
    value = _clamp_float(raw, 0.0, -120.0, 120.0)
    if value == 0.0:
        return 0.0
    return abs(value)

def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(round(float(value)))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))

def _normalize_playback_rate(value: Any, default: float = 1.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if math.isnan(parsed) or math.isinf(parsed):
        parsed = float(default)
    return max(0.5, min(2.0, parsed))

def _clean_label_text(value: Any) -> str:
    text = re.sub(r'\s+', ' ', str(value or '').strip())
    return text.strip(' ,;|/')

def _norm_label(value: Any) -> str:
    return re.sub(r'\s+', ' ', _clean_label_text(value).lower())

def _caption_from_fields(*values: Any) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_label_text(value)
        if not cleaned:
            continue
        norm = _norm_label(cleaned)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        parts.append(cleaned)
    return ', '.join(parts)

def _strip_caption_prefix(caption: Any, title: Any = '', genre: Any = '') -> str:
    text = _clean_label_text(caption)
    if not text:
        return ''
    if '|' not in text:
        return text
    parts = [_clean_label_text(part) for part in text.split('|')]
    parts = [part for part in parts if part]
    if not parts:
        return ''
    if title and parts and _norm_label(parts[0]) == _norm_label(title):
        parts = parts[1:]
    if genre and parts and _norm_label(parts[0]) == _norm_label(genre):
        parts = parts[1:]
    if genre and len(parts) >= 2 and _norm_label(parts[1]) == _norm_label(genre):
        parts = [parts[0], *parts[2:]]
    return ' | '.join([part for part in parts if part])

def _derive_track_caption(source: Any) -> str:
    if isinstance(source, dict):
        getter = source.get
        prompt = source.get('prompt') if isinstance(source.get('prompt'), dict) else {}
    else:
        getter = lambda key, default=None: getattr(source, key, default)
        prompt = getattr(source, 'prompt', {}) if isinstance(getattr(source, 'prompt', {}), dict) else {}
    title = getter('song_title', '') or prompt.get('song_title') or ''
    genre = getter('genre', '') or prompt.get('genre') or prompt.get('style') or ''
    theme = getter('theme', '') or prompt.get('theme') or ''
    explicit = _strip_caption_prefix(getter('caption', '') or prompt.get('caption') or '', title, genre)
    if explicit:
        return explicit
    built = _caption_from_fields(
        prompt.get('instruments'),
        prompt.get('mood'),
        prompt.get('vocal_style'),
        prompt.get('production'),
    )
    if built:
        return built
    raw_tags = _clean_label_text(getter('tags', '') or '')
    if raw_tags:
        banned = {
            _norm_label(raw_tags),
            _norm_label(genre),
            _norm_label(theme),
            _norm_label(f'Genre: {genre} · Theme: {theme}' if genre or theme else ''),
            _norm_label(', '.join([x for x in [genre, theme] if x])),
        }
        if _norm_label(raw_tags) not in banned:
            return raw_tags
    return ''

def _normalize_genre_list(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        norm = _norm_label(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(DEFAULT_GENRE_LOOKUP.get(norm, _clean_label_text(raw)))
    return cleaned

def _normalize_theme_list(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        norm = _norm_label(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(DEFAULT_THEME_LOOKUP.get(norm, _clean_label_text(raw)))
    return cleaned

def _pick_canonical_genre(values: list[Any]) -> str:
    pool = _normalize_genre_list(values) or list(DEFAULT_GENRES)
    return random.choice(pool)

def _split_theme_parts(value: Any) -> list[str]:
    parts = []
    for raw in re.split(r'\s*/\s*', str(value or '')):
        norm = _norm_label(raw)
        if not norm:
            continue
        parts.append(DEFAULT_THEME_LOOKUP.get(norm, _clean_label_text(raw)))
    return parts

def _normalize_theme_signal_text(value: Any) -> str:
    text = re.sub(r'[^a-z0-9\s]+', ' ', str(value or '').lower())
    return re.sub(r'\s+', ' ', text).strip()

def _theme_signal_candidates(piece: str) -> list[str]:
    piece_norm = _norm_label(piece)
    candidates: list[str] = []
    if piece_norm:
        candidates.append(piece_norm)
    for token in piece_norm.split():
        if len(token) >= 4 and token not in THEME_SIGNAL_TOKEN_STOPWORDS:
            candidates.append(token)
    for hint in THEME_SIGNAL_HINTS.get(piece_norm, []):
        hint_norm = _normalize_theme_signal_text(hint)
        if hint_norm:
            candidates.append(hint_norm)
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        norm = _normalize_theme_signal_text(candidate)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(norm)
    return cleaned

def _theme_signal_score_for_text(piece: str, text: str) -> tuple[int, list[str]]:
    normalized_text = _normalize_theme_signal_text(text)
    if not normalized_text:
        return 0, []
    score = 0
    matched: list[str] = []
    for candidate in _theme_signal_candidates(piece):
        if ' ' in candidate:
            if candidate in normalized_text:
                score += 2
                matched.append(candidate)
            continue
        pattern = rf'(?<![a-z0-9]){re.escape(candidate)}(?:s|es|ed|ing)?(?![a-z0-9])'
        if re.search(pattern, normalized_text):
            score += 1
            matched.append(candidate)
    unique_matched: list[str] = []
    seen: set[str] = set()
    for candidate in matched:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_matched.append(candidate)
    return min(score, 6), unique_matched

def _theme_coherence_report(theme: str, lyrics_text: str) -> dict[str, Any]:
    parts = _split_theme_parts(theme)
    part_reports: list[dict[str, Any]] = []
    covered_parts = 0
    strong_parts = 0
    total_score = 0
    for part in parts:
        score, matches = _theme_signal_score_for_text(part, lyrics_text)
        if score > 0:
            covered_parts += 1
        if score >= 2:
            strong_parts += 1
        total_score += score
        part_reports.append({'theme_part': part, 'score': score, 'matches': matches})
    required_parts = len(parts)
    accepted = False
    severity = 'pass'
    if required_parts == 0:
        accepted = True
    elif required_parts == 1:
        score = int(part_reports[0]['score']) if part_reports else 0
        accepted = score >= 1
        severity = 'failed' if score <= 0 else 'pass'
    else:
        accepted = covered_parts == required_parts and strong_parts >= max(1, required_parts - 1) and total_score >= (required_parts + 1)
        if not accepted:
            severity = 'partial' if covered_parts > 0 or strong_parts > 0 or total_score > 0 else 'failed'
    return {
        'parts': part_reports,
        'covered_parts': covered_parts,
        'strong_parts': strong_parts,
        'required_parts': required_parts,
        'total_score': total_score,
        'accepted': accepted,
        'severity': severity,
    }

LYRIC_STRUCTURE_TEMPLATES = {
    'classic_full': {
        'id': 'classic_full',
        'sequence': ['Verse 1', 'Pre-Chorus', 'Chorus', 'Verse 2', 'Pre-Chorus', 'Chorus', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [1, 4, 3, 4, 3, 4, 2, 4, 1],
        'target_lines': [5, 3, 5, 5, 3, 5, 4, 7, 3],
    },
    'intro_classic': {
        'id': 'intro_classic',
        'sequence': ['Intro', 'Verse 1', 'Pre-Chorus', 'Chorus', 'Verse 2', 'Pre-Chorus', 'Chorus', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [1, 4, 3, 4, 4, 3, 4, 2, 4, 1],
        'target_lines': [2, 5, 3, 5, 5, 3, 5, 4, 7, 3],
    },
    'condensed_classic': {
        'id': 'condensed_classic',
        'sequence': ['Verse 1', 'Pre-Chorus', 'Chorus', 'Verse 2', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [4, 2, 4, 4, 2, 4, 1],
        'target_lines': [5, 3, 5, 5, 4, 7, 3],
    },
    'intro_condensed_classic': {
        'id': 'intro_condensed_classic',
        'sequence': ['Intro', 'Verse 1', 'Pre-Chorus', 'Chorus', 'Verse 2', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [1, 4, 2, 4, 4, 2, 4, 1],
        'target_lines': [2, 5, 3, 5, 5, 4, 7, 3],
    },
    'verse_chorus': {
        'id': 'verse_chorus',
        'sequence': ['Verse 1', 'Chorus', 'Verse 2', 'Chorus', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [4, 4, 4, 4, 2, 4, 1],
        'target_lines': [5, 5, 5, 5, 4, 7, 3],
    },
    'intro_verse_chorus': {
        'id': 'intro_verse_chorus',
        'sequence': ['Intro', 'Verse 1', 'Chorus', 'Verse 2', 'Chorus', 'Bridge', 'Final Chorus', 'Outro'],
        'min_lines': [1, 4, 4, 4, 4, 2, 4, 1],
        'target_lines': [2, 5, 5, 5, 5, 4, 7, 3],
    },
}
LYRIC_STRUCTURE_TEMPLATE_ORDER = tuple(LYRIC_STRUCTURE_TEMPLATES.keys())
DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID = 'classic_full'
_DEFAULT_LYRIC_STRUCTURE_TEMPLATE = LYRIC_STRUCTURE_TEMPLATES[DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID]
CANONICAL_LYRIC_SECTION_SEQUENCE = list(_DEFAULT_LYRIC_STRUCTURE_TEMPLATE['sequence'])
CANONICAL_LYRIC_SECTION_MIN_LINES = list(_DEFAULT_LYRIC_STRUCTURE_TEMPLATE['min_lines'])
CANONICAL_LYRIC_SECTION_TARGET_LINES = list(_DEFAULT_LYRIC_STRUCTURE_TEMPLATE['target_lines'])
CANONICAL_LYRIC_SECTION_OPTIONAL_WARNINGS = {'lyrics_final_chorus_small', 'lyrics_final_chorus_repeat'}
CANONICAL_LYRIC_SECTION_ALIASES = {
    'intro': 'Intro',
    'opening': 'Intro',
    'verse 1': 'Verse 1',
    'verse1': 'Verse 1',
    'v1': 'Verse 1',
    'pre chorus': 'Pre-Chorus',
    'pre-chorus': 'Pre-Chorus',
    'prechorus': 'Pre-Chorus',
    'chorus': 'Chorus',
    'hook': 'Chorus',
    'refrain': 'Pre-Chorus',
    'verse 2': 'Verse 2',
    'verse2': 'Verse 2',
    'v2': 'Verse 2',
    'bridge': 'Bridge',
    'final chorus': 'Final Chorus',
    'finalchorus': 'Final Chorus',
    'final hook': 'Final Chorus',
    'final refrain': 'Final Chorus',
    'outro': 'Outro',
    'ending': 'Outro',
    'end': 'Outro',
}


def _lyric_structure_template(template_id: Optional[str] = None) -> dict[str, Any]:
    key = str(template_id or DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID).strip().lower() or DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID
    return LYRIC_STRUCTURE_TEMPLATES.get(key, _DEFAULT_LYRIC_STRUCTURE_TEMPLATE)


def _lyric_structure_match_score(raw_names: list[str], template: dict[str, Any]) -> tuple[int, int, int, int]:
    expected = list(template.get('sequence') or [])
    if not raw_names:
        return (-999, 0, len(expected), 0)
    pos = 0
    matched = 0
    extras = 0
    skipped = 0
    for name in raw_names:
        found = False
        while pos < len(expected):
            if expected[pos] == name:
                matched += 1
                pos += 1
                found = True
                break
            skipped += 1
            pos += 1
        if not found:
            extras += 1
    missing = max(0, len(expected) - matched)
    score = (matched * 6) - (missing * 4) - (extras * 3) - skipped
    return (score, matched, missing, extras)


def _infer_lyric_structure_template(raw_names: list[str]) -> dict[str, Any]:
    best = _DEFAULT_LYRIC_STRUCTURE_TEMPLATE
    best_rank = (-10**9, -10**9, -10**9, -10**9)
    for template_id in LYRIC_STRUCTURE_TEMPLATE_ORDER:
        template = LYRIC_STRUCTURE_TEMPLATES[template_id]
        score, matched, missing, extras = _lyric_structure_match_score(raw_names, template)
        rank = (score, matched, -missing, -extras)
        if rank > best_rank:
            best = template
            best_rank = rank
    return best


def _normalize_lyric_section_key(value: str) -> str:
    text = str(value or '').strip()
    text = text.strip('[](){}')
    text = re.sub(r'^[\-–—:*#\s]+|[\-–—:*#\s]+$', '', text)
    text = text.replace('_', ' ')
    text = re.sub(r'[^A-Za-z0-9+\- ]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text


def _canonical_lyric_section_name(line: str) -> str:
    key = _normalize_lyric_section_key(line)
    if not key:
        return ''
    direct = CANONICAL_LYRIC_SECTION_ALIASES.get(key, '')
    if direct:
        return direct
    compact = re.sub(r'\s+', ' ', key).strip()
    if not compact:
        return ''
    if compact.startswith(('intro ', 'intro-', 'intro:')):
        return 'Intro'
    if compact.startswith(('verse 1 ', 'verse 1-', 'verse 1:', 'verse1 ', 'verse1-', 'verse1:', 'v1 ', 'v1-', 'v1:')):
        return 'Verse 1'
    if compact.startswith(('pre chorus ', 'pre chorus-', 'pre chorus:', 'pre-chorus ', 'pre-chorus-', 'pre-chorus:', 'prechorus ', 'prechorus-', 'prechorus:')):
        return 'Pre-Chorus'
    if compact.startswith(('final chorus ', 'final chorus-', 'final chorus:', 'finalchorus ', 'finalchorus-', 'finalchorus:', 'final hook ', 'final hook-', 'final hook:', 'final refrain ', 'final refrain-', 'final refrain:')):
        return 'Final Chorus'
    if compact.startswith(('chorus', 'hook', 'refrain')):
        if re.search(r'\b(final|last|ending|out)\b', compact):
            return 'Final Chorus'
        return 'Chorus'
    if compact.startswith(('verse 2 ', 'verse 2-', 'verse 2:', 'verse2 ', 'verse2-', 'verse2:', 'v2 ', 'v2-', 'v2:')):
        return 'Verse 2'
    if compact in {'middle 8', 'middle-8', 'middle eight', 'middle-eight'} or compact.startswith(('middle 8 ', 'middle 8-', 'middle 8:', 'middle-eight ', 'middle-eight:', 'middle-eight-')):
        return 'Bridge'
    if compact.startswith(('bridge ', 'bridge-', 'bridge:')):
        return 'Bridge'
    if compact.startswith(('outro ', 'outro-', 'outro:', 'ending ', 'ending-', 'ending:', 'end ', 'end-', 'end:')):
        return 'Outro'
    return ''


def _compact_lyrics_lines(lines: list[str]) -> str:
    out: list[str] = []
    for raw in lines:
        line = str(raw or '').rstrip()
        if not line.strip():
            if out and out[-1] != '':
                out.append('')
            continue
        out.append(line.strip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return '\n'.join(out)


def _split_inline_lyric_section(line: str) -> tuple[str, str]:
    text = str(line or '').strip()
    if not text:
        return '', ''
    match = re.match(r'^([A-Za-z][A-Za-z0-9 _+\-]{0,40})\s*[:\-–—]+\s*(.*)$', text)
    if not match:
        return '', text
    section = _canonical_lyric_section_name(match.group(1) or '')
    if not section:
        return '', text
    return section, str(match.group(2) or '').strip()


def _canonicalize_vocal_lyrics(lyrics: str) -> str:
    lines_out: list[str] = []
    cleaned_source = _strip_llm_artifacts_text(str(lyrics or ''), keep_newlines=True)
    for raw_line in cleaned_source.splitlines():
        line = str(raw_line or '').strip()
        if not line:
            if lines_out and lines_out[-1] != '':
                lines_out.append('')
            continue
        section = _canonical_lyric_section_name(line)
        remainder = ''
        if not section:
            section, remainder = _split_inline_lyric_section(line)
        if section:
            while lines_out and lines_out[-1] == '':
                lines_out.pop()
            if lines_out:
                lines_out.append('')
            lines_out.append(f'[{section}]')
            if remainder:
                lines_out.append(re.sub(r'\s+', ' ', remainder).strip())
            continue
        normalized = re.sub(r'\s+', ' ', line).strip()
        if normalized:
            lines_out.append(normalized)
    return _compact_lyrics_lines(lines_out)


def _parse_lyrics_sections(lyrics: str) -> dict[str, Any]:
    canonical = _canonicalize_vocal_lyrics(lyrics)
    sections: list[dict[str, Any]] = []
    prelude: list[str] = []
    current: Optional[dict[str, Any]] = None
    for raw_line in canonical.splitlines():
        line = str(raw_line or '').strip()
        if not line:
            continue
        section = _canonical_lyric_section_name(line)
        if section:
            current = {'name': section, 'lines': []}
            sections.append(current)
            continue
        if current is None:
            prelude.append(line)
            continue
        current['lines'].append(line)
    return {'lyrics': canonical, 'sections': sections, 'prelude': prelude}


_LYRIC_TEMPLATE_CLICHE_PATTERNS = [
    r'\bcoffee cup\b',
    r'\bstreetlights?\b',
    r'\bsafety net\b',
    r'\bsilver lining\b',
    r'\bgolden ticket\b',
    r'\bhold the line\b',
    r'\bquiet hum\b',
    r'\bwalk forward\b',
    r'\bold world\b',
    r'\bnew machine\b',
    r'\bbroken wings?\b',
    r'\brunning free\b',
    r'\bneon nights?\b',
    r'\bashes?\b',
    r'\bquiet strength\b',
]


def _current_ollama_model_is_qwen35_nineb() -> bool:
    return 'qwen3.5:9b' in str(OLLAMA_MODEL or '').strip().lower()


def _count_regex_hits(text: str, patterns: list[str]) -> int:
    haystack = str(text or '').strip().lower()
    if not haystack:
        return 0
    hits = 0
    for pattern in patterns:
        with contextlib.suppress(re.error):
            if re.search(pattern, haystack, flags=re.IGNORECASE):
                hits += 1
    return hits


def _style_sentence_count(text: str) -> int:
    cleaned = re.sub(r'\s+', ' ', str(text or '').strip())
    if not cleaned:
        return 0
    return len([part for part in re.split(r'(?<=[.!?])\s+', cleaned) if part.strip()])


def _style_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+", str(text or '').strip()))


def _lyrics_structure_issues(lyrics: str, instrumental: bool) -> list[str]:
    if instrumental:
        return []
    parsed = _parse_lyrics_sections(_repair_vocal_lyrics_structure(lyrics))
    sections = parsed['sections']
    names = [section['name'] for section in sections]
    issues: list[str] = []
    if parsed.get('prelude'):
        issues.append('lyrics_prelude')
    template = _infer_lyric_structure_template(names)
    expected_names = list(template.get('sequence') or [])
    expected_min_lines = list(template.get('min_lines') or [])
    if names != expected_names:
        issues.append('lyrics_sections')
        return issues
    for index, section in enumerate(sections):
        name = str(section.get('name') or '')
        min_needed = int(expected_min_lines[index]) if index < len(expected_min_lines) else 0
        real_lines = [line for line in section['lines'] if line.strip()]
        if not real_lines and name in {'Intro', 'Outro'}:
            continue
        if min_needed > 0 and not real_lines:
            issues.append(f'lyrics_empty:{name}')
            continue
        if not real_lines:
            continue
        if name == 'Bridge':
            if len(real_lines) < 2:
                issues.append('lyrics_short:Bridge')
            continue
        if name == 'Outro':
            continue
        if name == 'Final Chorus':
            continue
        if min_needed > 0 and len(real_lines) < min_needed:
            issues.append(f'lyrics_short:{name}')
    chorus_blocks = [
        [line for line in section['lines'] if line.strip()]
        for section in sections
        if section['name'] == 'Chorus'
    ]
    final_section = next((section for section in sections if section['name'] == 'Final Chorus'), None)
    if chorus_blocks and final_section is not None:
        final_lines = [line for line in final_section['lines'] if line.strip()]
        earlier_lines = {
            re.sub(r'\s+', ' ', line.strip().lower())
            for block in chorus_blocks
            for line in block
            if line.strip()
        }
        new_lines = [line for line in final_lines if re.sub(r'\s+', ' ', line.strip().lower()) not in earlier_lines]
        if not final_lines:
            issues.append('lyrics_empty:Final Chorus')
        elif len(final_lines) < 3:
            issues.append('lyrics_final_chorus_small')
        elif not new_lines and len(final_lines) < 4:
            issues.append('lyrics_final_chorus_repeat')
    plain_text = _lyrics_plain_text(parsed.get('lyrics') or lyrics, False)
    if _count_regex_hits(plain_text, _LYRIC_TEMPLATE_CLICHE_PATTERNS) >= 3:
        issues.append('lyrics_cliche')
    deduped: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        if issue in seen:
            continue
        seen.add(issue)
        deduped.append(issue)
    return deduped
def _build_local_instrumental_prompt(target_genre: str, lyrical_theme: str, duration: int, language: str) -> SongPrompt:
    theme_parts = _split_theme_parts(lyrical_theme) or ['instrumental']
    title_tokens = [target_genre, *theme_parts[:2], 'instrumental']
    title = _sanitize_title(' '.join(str(token).strip().title() for token in title_tokens if str(token).strip())) or 'Instrumental Broadcast'
    bpm_seed = sum(ord(ch) for ch in f"{target_genre}|{lyrical_theme}|{language}")
    bpm = 80 + (bpm_seed % 61)
    key_options = ['C Major', 'A Minor', 'G Major', 'E Minor', 'D Minor', 'F Major', 'D Major', 'B Minor']
    key_scale = key_options[bpm_seed % len(key_options)]
    return SongPrompt(
        song_title=title,
        style=str(target_genre or 'instrumental').strip() or 'instrumental',
        caption=f"An instrumental {str(target_genre or 'radio').strip()} piece shaped around {' / '.join(theme_parts)}.".strip(),
        theme=' / '.join(theme_parts),
        instruments=f"{str(target_genre or 'radio').strip()} instrumental arrangement".strip(),
        mood=f"Instrumental {str(target_genre or 'radio').strip()} atmosphere shaped around {' / '.join(theme_parts)}".strip(),
        vocal_style='instrumental',
        production='radio instrumental arrangement',
        bpm=bpm,
        key_scale=key_scale,
        timesignature='4/4',
        duration=max(30, int(duration or 180)),
        lyrics='[Instrumental]',
    )

def _pick_theme_candidate(values: list[Any], used: Optional[set[str]] = None) -> str:
    pool = _normalize_theme_list(values)
    if not pool:
        pool = list(DEFAULT_THEMES)
    if not pool:
        return 'love'
    used = used or set()
    valid_sizes = [size for size in (1, 2) if len(pool) >= size] or [1]
    for _ in range(64):
        size = random.choice(valid_sizes)
        picked = random.sample(pool, size)
        candidate = ' / '.join(picked)
        if candidate not in used:
            return candidate
    return ' / '.join(pool[:min(2, len(pool))])

def _strip_llm_fences(raw: str) -> str:
    text = str(raw or '')
    text = re.sub(r'^```[^\n]*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)
    return text.lstrip('\ufeff')


def _extract_song_examples_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        if isinstance(raw.get('examples'), list):
            return [x for x in raw.get('examples') if isinstance(x, dict)]
        if isinstance(raw.get('songs'), list):
            return [x for x in raw.get('songs') if isinstance(x, dict)]
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _normalize_custom_catalog_language(value: Any) -> str:
    lang = str(value or '').strip().lower()
    return lang if lang in VALID_LANGUAGES else ''


def _lyrics_indicates_instrumental(value: Any) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    normalized = re.sub(r'\s+', ' ', text).strip().lower()
    return normalized in {'[instrumental]', 'instrumental', '(instrumental)'}



def _normalize_custom_catalog_song(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    title = _sanitize_title(str(item.get('title') or item.get('song_title') or '').strip())
    genre = str(item.get('genre') or '').strip()
    style = str(item.get('style') or '').strip()
    theme = str(item.get('theme') or '').strip()
    caption = str(item.get('caption') or '').strip()
    lyrics = str(item.get('lyrics') or '').strip()
    instrumental = bool(item.get('instrumental')) or _lyrics_indicates_instrumental(lyrics)
    language = _normalize_custom_catalog_language(item.get('vocal_language') or item.get('language'))
    description = str(item.get('description') or '').strip()
    pack = str(item.get('pack') or '').strip()
    production = str(item.get('production') or '').strip()
    keyscale = _normalize_key_scale(item.get('keyscale') or item.get('key_scale') or '')
    timesignature = str(item.get('timesignature') or item.get('time_signature') or '').strip()
    try:
        bpm = int(item.get('bpm') or 0)
    except Exception:
        bpm = 0
    duration = _coerce_duration_value(item.get('duration'), 0)
    if instrumental:
        lyrics = '[Instrumental]'
    if not title or not style or not keyscale or bpm <= 0 or duration < 30:
        return None
    if not instrumental and (not lyrics or not language):
        return None
    return {
        'pack': pack,
        'title': title,
        'description': description,
        'theme': theme,
        'caption': caption,
        'genre': genre,
        'style': style,
        'lyrics': lyrics,
        'instrumental': instrumental,
        'bpm': bpm,
        'duration': duration,
        'keyscale': keyscale,
        'timesignature': timesignature,
        'production': production,
        'vocal_language': language,
    }


def _prepare_custom_catalog_payload(raw: Any, original_name: str = '') -> dict[str, Any]:
    raw_meta = raw.get('_meta') if isinstance(raw, dict) else {}
    valid: list[dict[str, Any]] = []
    ignored = 0
    for item in _extract_song_examples_payload(raw):
        normalized = _normalize_custom_catalog_song(item)
        if normalized is None:
            ignored += 1
            continue
        valid.append(normalized)
    final_name = str(original_name or (raw_meta or {}).get('original_name') or '').strip()
    final_path = str((raw_meta or {}).get('original_path') or '').strip()
    return {
        'songs': valid,
        '_meta': {
            'original_name': final_name,
            'original_path': final_path,
            'song_count': len(valid),
            'ignored_count': int(ignored),
        },
    }


def _custom_catalog_file_info(path: Any = None) -> dict[str, Any]:
    file_path = Path(path or CUSTOM_CATALOG_PATH)
    info = {
        'exists': False,
        'path': str(file_path),
        'name': '',
        'original_path': '',
        'song_count': 0,
        'ignored_count': 0,
    }
    if not file_path.exists():
        return info
    try:
        raw = json.loads(file_path.read_text(encoding='utf-8'))
        meta = raw.get('_meta') if isinstance(raw, dict) else {}
        songs = _extract_song_examples_payload(raw)
        info.update({
            'exists': True,
            'name': str((meta or {}).get('original_name') or file_path.name),
            'original_path': str((meta or {}).get('original_path') or ''),
            'song_count': int((meta or {}).get('song_count') or len(songs)),
            'ignored_count': int((meta or {}).get('ignored_count') or 0),
        })
    except Exception:
        logger.exception('Failed to read custom catalog metadata: %s', file_path)
    return info


def _custom_catalog_enabled(payload: Any) -> bool:
    return bool(getattr(payload, 'custom_catalog_enabled', False)) and CUSTOM_CATALOG_PATH.exists()


def _remove_active_custom_catalog_file() -> bool:
    removed = False
    with contextlib.suppress(Exception):
        if CUSTOM_CATALOG_PATH.exists():
            CUSTOM_CATALOG_PATH.unlink()
            removed = True
    with contextlib.suppress(Exception):
        if CUSTOM_CATALOG_DIR.exists() and not any(CUSTOM_CATALOG_DIR.iterdir()):
            CUSTOM_CATALOG_DIR.rmdir()
    return removed


def _activate_custom_catalog_from_file(path: Any) -> dict[str, Any]:
    source_path = Path(str(path or '')).expanduser()
    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f'Custom catalog file not found: {source_path}')
    try:
        raw = json.loads(source_path.read_text(encoding='utf-8-sig'))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Invalid custom catalog JSON: {e}')
    return _activate_custom_catalog_from_payload(raw, source_path.name or CUSTOM_CATALOG_FILENAME, str(source_path))


def _read_embedded_custom_catalog_payload(path: Any = None) -> Optional[dict[str, Any]]:
    file_path = Path(path or CUSTOM_CATALOG_PATH)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding='utf-8'))
    except Exception:
        logger.exception('Failed to read active custom catalog payload: %s', file_path)
        return None
    prepared = _prepare_custom_catalog_payload(raw, str(((raw.get('_meta') or {}).get('original_name') or getattr(file_path, 'name', '') or CUSTOM_CATALOG_FILENAME)))
    return prepared if list(prepared.get('songs') or []) else None


def _activate_custom_catalog_from_payload(raw: Any, original_name: str = '', original_path: str = '') -> dict[str, Any]:
    prepared = _prepare_custom_catalog_payload(raw, original_name or CUSTOM_CATALOG_FILENAME)
    prepared_meta = prepared.get('_meta') if isinstance(prepared, dict) else {}
    if isinstance(prepared_meta, dict):
        prepared_meta['original_path'] = str(original_path or prepared_meta.get('original_path') or '').strip()
    songs = list(prepared.get('songs') or [])
    if not songs:
        raise HTTPException(status_code=400, detail='No usable songs found in the custom catalog referenced by the settings file')
    CUSTOM_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CUSTOM_CATALOG_PATH.with_suffix(CUSTOM_CATALOG_PATH.suffix + '.tmp')
    tmp_path.write_text(json.dumps(prepared, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp_path.replace(CUSTOM_CATALOG_PATH)
    return _custom_catalog_file_info(CUSTOM_CATALOG_PATH)


def _settings_payload_for_client(data: dict[str, Any]) -> dict[str, Any]:
    clean = _sanitize_settings_payload(data if isinstance(data, dict) else {})
    clean.pop(EMBEDDED_CUSTOM_CATALOG_KEY, None)
    return clean


def _sync_loaded_settings_custom_catalog(settings: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw = _sanitize_settings_payload(settings if isinstance(settings, dict) else {})
    warning = ''
    requested_path = str(raw.get('custom_catalog_file') or '').strip()
    requested_name = str(raw.get('custom_catalog_name') or '').strip() or (Path(requested_path).name if requested_path else '') or CUSTOM_CATALOG_FILENAME
    wants_custom = bool(raw.get('custom_catalog_enabled')) and bool(requested_path)
    if wants_custom:
        try:
            info = _activate_custom_catalog_from_file(requested_path)
            raw.update({
                'custom_catalog_enabled': True,
                'custom_catalog_file': requested_path,
                'custom_catalog_name': str(requested_name or info.get('name') or CUSTOM_CATALOG_FILENAME),
                'custom_catalog_song_count': int(info.get('song_count') or 0),
                'custom_catalog_ignored_count': int(info.get('ignored_count') or 0),
            })
        except HTTPException as exc:
            _remove_active_custom_catalog_file()
            detail = getattr(exc, 'detail', '') or 'Custom catalog could not be loaded'
            raw.update({
                'custom_catalog_enabled': False,
                'custom_catalog_file': '',
                'custom_catalog_name': '',
                'custom_catalog_song_count': 0,
                'custom_catalog_ignored_count': 0,
            })
            warning = f'Settings applied, but the custom catalog was not loaded: {detail}'
        except Exception as exc:
            _remove_active_custom_catalog_file()
            raw.update({
                'custom_catalog_enabled': False,
                'custom_catalog_file': '',
                'custom_catalog_name': '',
                'custom_catalog_song_count': 0,
                'custom_catalog_ignored_count': 0,
            })
            warning = f'Settings applied, but the custom catalog was not loaded: {exc}'
    else:
        _remove_active_custom_catalog_file()
        raw.update({
            'custom_catalog_enabled': False,
            'custom_catalog_file': '',
            'custom_catalog_name': '',
            'custom_catalog_song_count': 0,
            'custom_catalog_ignored_count': 0,
        })
    raw.pop(EMBEDDED_CUSTOM_CATALOG_KEY, None)
    return _normalize_settings_for_storage(raw), warning


def _load_and_activate_settings_file(path: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    target = _coerce_settings_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f'File not found: {target}')
    try:
        raw = json.loads(target.read_text(encoding='utf-8'))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Invalid JSON: {e}')
    normalized, warning = _sync_loaded_settings_custom_catalog(raw if isinstance(raw, dict) else {})
    info = _save_settings_file(normalized, target)
    return normalized, info, warning


def _normalize_radio_request(payload: 'RadioStartRequest') -> 'RadioStartRequest':
    payload.genres = _normalize_genre_list(getattr(payload, 'genres', []))
    payload.themes = _normalize_theme_list(getattr(payload, 'themes', []))
    payload.languages = [x for x in payload.languages if x in VALID_LANGUAGES] or ['en']
    payload.min_duration = _coerce_duration_value(getattr(payload, 'min_duration', None), 60)
    payload.max_duration = max(payload.min_duration, _coerce_duration_value(getattr(payload, 'max_duration', None), payload.min_duration))
    payload.automatic_duration = bool(getattr(payload, 'automatic_duration', False))
    payload.inference_steps = _resolve_inference_steps_for_model(payload.model, payload.inference_steps)
    payload.instrumental_probability = max(0, min(100, int(payload.instrumental_probability)))
    payload.language_rotation_mode = (payload.language_rotation_mode or 'round_robin').strip().lower() or 'round_robin'
    payload.vram_cleanup_mode = (payload.vram_cleanup_mode or VRAM_CLEANUP_MODE).strip().lower() or VRAM_CLEANUP_MODE
    payload.max_saved_tracks = max(1, min(10000, int(payload.max_saved_tracks or DEFAULT_MAX_SAVED_TRACKS)))
    payload.lora_use_probability = max(0, min(100, int(payload.lora_use_probability or 0)))
    payload.generation_mode, payload.catalog_source, payload.generation_source = _resolve_generation_settings(
        getattr(payload, 'generation_mode', None),
        getattr(payload, 'catalog_source', None),
        getattr(payload, 'generation_source', None),
    )
    payload.generation_source_both_percent = max(0, min(100, int(getattr(payload, 'generation_source_both_percent', 50) or 0)))
    payload.custom_catalog_enabled = bool(getattr(payload, 'custom_catalog_enabled', False))
    payload.custom_catalog_file = str(getattr(payload, 'custom_catalog_file', '') or '').strip()
    payload.custom_catalog_name = str(getattr(payload, 'custom_catalog_name', '') or '').strip()
    payload.custom_catalog_song_count = max(0, int(getattr(payload, 'custom_catalog_song_count', 0) or 0))
    payload.custom_catalog_ignored_count = max(0, int(getattr(payload, 'custom_catalog_ignored_count', 0) or 0))
    custom_info = _custom_catalog_file_info(CUSTOM_CATALOG_PATH) if payload.custom_catalog_enabled else {'exists': False, 'path': str(CUSTOM_CATALOG_PATH), 'name': '', 'original_path': '', 'song_count': 0, 'ignored_count': 0}
    if payload.custom_catalog_enabled and custom_info.get('exists'):
        payload.custom_catalog_file = str(payload.custom_catalog_file or custom_info.get('original_path') or '').strip()
        payload.custom_catalog_name = str(payload.custom_catalog_name or custom_info.get('name') or Path(payload.custom_catalog_file).name or CUSTOM_CATALOG_FILENAME)
        payload.custom_catalog_song_count = int(custom_info.get('song_count') or payload.custom_catalog_song_count or 0)
        payload.custom_catalog_ignored_count = int(custom_info.get('ignored_count') or payload.custom_catalog_ignored_count or 0)
        payload.generation_mode = 'local_catalog'
        payload.catalog_source = 'custom'
        payload.generation_source = 'file'
    else:
        payload.custom_catalog_enabled = False
        payload.custom_catalog_file = ''
        payload.custom_catalog_name = ''
        payload.custom_catalog_song_count = 0
        payload.custom_catalog_ignored_count = 0
    payload.reservoir_target = max(1, min(50, int(payload.reservoir_target or RESERVOIR_TARGET)))
    payload.refill_threshold = max(1, min(payload.reservoir_target, int(payload.refill_threshold or RESERVOIR_REFILL_THRESHOLD)))
    payload.audio_format = (payload.audio_format or PLAYER_AUDIO_FORMAT).strip().lower()
    payload.audio_format = payload.audio_format if payload.audio_format in {'mp3', 'wav', 'flac', 'wav32', 'opus', 'aac'} else PLAYER_AUDIO_FORMAT
    payload.mp3_bitrate = str(payload.mp3_bitrate or ACERADIO_MP3_DEFAULT_BITRATE).strip().lower()
    payload.mp3_bitrate = payload.mp3_bitrate if payload.mp3_bitrate in ACERADIO_MP3_BITRATE_OPTIONS else ACERADIO_MP3_DEFAULT_BITRATE
    try:
        payload.mp3_sample_rate = int(payload.mp3_sample_rate or ACERADIO_MP3_DEFAULT_SAMPLE_RATE)
    except Exception:
        payload.mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    payload.mp3_sample_rate = payload.mp3_sample_rate if payload.mp3_sample_rate in ACERADIO_MP3_SAMPLE_RATE_OPTIONS else ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    payload.jingle_separator_arm_offset_s = _clamp_float(getattr(payload, 'jingle_separator_arm_offset_s', 0.0), 0.0, -60.0, 120.0)
    payload.jingle_separator_min_remaining_offset_s = _clamp_float(getattr(payload, 'jingle_separator_min_remaining_offset_s', 0.0), 0.0, -30.0, 30.0)
    payload.jingle_overlay_mid_offset_s = _clamp_float(getattr(payload, 'jingle_overlay_mid_offset_s', 0.0), 0.0, -120.0, 120.0)
    payload.jingle_overlay_trigger_window_s = _clamp_float(getattr(payload, 'jingle_overlay_trigger_window_s', JINGLE_OVERLAY_MID_WINDOW_S), JINGLE_OVERLAY_MID_WINDOW_S, 0.25, 30.0)
    payload.jingle_overlay_min_duration_s = _clamp_float(getattr(payload, 'jingle_overlay_min_duration_s', JINGLE_OVERLAY_MIN_DURATION_S), JINGLE_OVERLAY_MIN_DURATION_S, 0.0, 600.0)
    payload.admin_separator_fade_ms = _clamp_int(getattr(payload, 'admin_separator_fade_ms', 500), 500, 0, 10000)
    payload.admin_overlay_pre_duck_ms = _clamp_int(getattr(payload, 'admin_overlay_pre_duck_ms', 300), 300, 0, 10000)
    payload.admin_overlay_restore_ms = _clamp_int(getattr(payload, 'admin_overlay_restore_ms', 700), 700, 0, 10000)
    payload.auto_transition_cut_seconds = _clamp_int(getattr(payload, 'auto_transition_cut_seconds', 0), 0, 0, 7200)
    payload.batch_size = 1
    payload.use_adg = bool(payload.use_adg)
    try:
        payload.score_scale = float(payload.score_scale)
    except Exception:
        payload.score_scale = 0.5
    payload.score_scale = max(0.01, min(payload.score_scale, 1.0))
    payload.auto_score = bool(payload.auto_score)
    try:
        payload.guidance_scale = float(payload.guidance_scale)
    except Exception:
        payload.guidance_scale = 7.0
    payload.guidance_scale = max(1.0, min(payload.guidance_scale, 15.0))
    payload.shift = _resolve_shift_for_model(payload.model, payload.shift)
    try:
        payload.cfg_interval_start = float(payload.cfg_interval_start)
    except Exception:
        payload.cfg_interval_start = 0.0
    try:
        payload.cfg_interval_end = float(payload.cfg_interval_end)
    except Exception:
        payload.cfg_interval_end = 1.0
    payload.cfg_interval_start = max(0.0, min(payload.cfg_interval_start, 1.0))
    payload.cfg_interval_end = max(0.0, min(payload.cfg_interval_end, 1.0))
    try:
        payload.normalization_db = float(payload.normalization_db)
    except Exception:
        payload.normalization_db = -1.0
    payload.normalization_db = max(-10.0, min(payload.normalization_db, 0.0))
    try:
        payload.latent_shift = float(payload.latent_shift)
    except Exception:
        payload.latent_shift = 0.0
    try:
        payload.latent_rescale = float(payload.latent_rescale)
    except Exception:
        payload.latent_rescale = 1.0
    payload.latent_shift = max(-0.2, min(payload.latent_shift, 0.2))
    payload.latent_rescale = max(0.5, min(payload.latent_rescale, 1.5))
    try:
        payload.lm_temperature = float(payload.lm_temperature)
    except Exception:
        payload.lm_temperature = 0.85
    payload.lm_temperature = max(0.0, min(payload.lm_temperature, 2.0))
    try:
        payload.lm_cfg_scale = float(payload.lm_cfg_scale)
    except Exception:
        payload.lm_cfg_scale = 2.0
    payload.lm_cfg_scale = max(1.0, min(payload.lm_cfg_scale, 3.0))
    try:
        payload.lm_top_k = int(float(payload.lm_top_k))
    except Exception:
        payload.lm_top_k = 0
    payload.lm_top_k = max(0, min(payload.lm_top_k, 200))
    try:
        payload.lm_top_p = float(payload.lm_top_p)
    except Exception:
        payload.lm_top_p = 0.9
    payload.lm_top_p = max(0.0, min(payload.lm_top_p, 1.0))
    payload.lm_negative_prompt = str(payload.lm_negative_prompt or 'NO USER INPUT').strip() or 'NO USER INPUT'
    if not hasattr(payload, 'station_negative_prompt'):
        payload.station_negative_prompt = ''
    payload.use_constrained_decoding = bool(payload.use_constrained_decoding)
    payload.thinking = bool(payload.thinking)
    payload.use_cot_metas = bool(payload.use_cot_metas)
    payload.use_cot_caption = bool(payload.use_cot_caption)
    payload.use_cot_language = bool(payload.use_cot_language)
    payload.parallel_thinking = bool(payload.parallel_thinking)
    payload.constrained_decoding_debug = bool(payload.constrained_decoding_debug)
    if not payload.thinking:
        payload.use_cot_metas = False
        payload.use_cot_caption = False
        payload.use_cot_language = False
        payload.parallel_thinking = False
        payload.constrained_decoding_debug = False
    payload.timesteps = str(payload.timesteps or '').strip()
    return payload

def _resolve_auth_config() -> tuple[bool, str, str]:
    env_enabled_raw = str(os.getenv('ACERADIO_AUTH_ENABLED', '') or '').strip().lower()
    env_username = str(os.getenv('ACERADIO_USERNAME', '') or '').strip()
    env_password = str(os.getenv('ACERADIO_PASSWORD', '') or '').strip()
    json_username = ''
    json_password = ''
    try:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                json_username = str(raw.get('auth_username') or '').strip()
                json_password = str(raw.get('auth_password') or '').strip()
    except Exception:
        logger.exception('Failed to resolve AceRadio auth settings from JSON')
    auth_enabled = env_enabled_raw in {'1', 'true', 'yes', 'on'} or bool(env_password)
    username = env_username or json_username or 'admin'
    password = env_password or (json_password if auth_enabled else '')
    if not auth_enabled and password:
        auth_enabled = True
    return auth_enabled, username, password

AUTH_ENABLED, AUTH_USERNAME_RAW, AUTH_PASSWORD_RAW = _resolve_auth_config()
AUTH_USERNAME_HASH = hashlib.sha256(AUTH_USERNAME_RAW.encode()).hexdigest() if AUTH_ENABLED else ''
AUTH_PASSWORD_HASH = hashlib.sha256(AUTH_PASSWORD_RAW.encode()).hexdigest() if AUTH_ENABLED else ''
AUTH_COOKIE = 'aceradio_session'
_sessions: dict[str, float] = {}
SESSION_TTL = 86400 * 7

def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token

def _valid_session(token: str | None) -> bool:
    if not AUTH_ENABLED:
        return True
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp or time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True

def _check_auth(request: Request) -> bool:
    return _valid_session(request.cookies.get(AUTH_COOKIE))
_FIELD_RE = re.compile(r'^([A-Z][A-Z0-9_ ]*)\s*:\s*(.*)', re.IGNORECASE | re.MULTILINE)
_KEY_MAP = {
    'TITLE': 'song_title',
    'SONG_TITLE': 'song_title',
    'GENRE': 'genre',
    'STYLE': 'style',
    'DESCRIPTION': 'caption',
    'CAPTION': 'caption',
    'THEME': 'theme',
    'INSTRUMENTS': 'instruments',
    'MOOD': 'mood',
    'VOCAL_STYLE': 'vocal_style',
    'PRODUCTION': 'production',
    'BPM': 'bpm',
    'KEY': 'key_scale',
    'KEYSCALE': 'key_scale',
    'KEY_SCALE': 'key_scale',
    'TIMESIGNATURE': 'timesignature',
    'TIME_SIGNATURE': 'timesignature',
    'VOCAL_LANGUAGE': 'vocal_language',
    'LANGUAGE': 'vocal_language',
    'DURATION': 'duration',
    'LENGTH': 'duration',
}
_LM_LABEL_ALIASES = {
    'SONG_TITLE': 'TITLE',
    'CAPTION': 'DESCRIPTION',
    'KEYSCALE': 'KEY',
    'KEY_SCALE': 'KEY',
    'TIME_SIGNATURE': 'TIMESIGNATURE',
    'LENGTH': 'DURATION',
}
_JSON_LM_KEY_ALIASES = {
    'title': 'TITLE',
    'song_title': 'TITLE',
    'genre': 'GENRE',
    'style': 'STYLE',
    'description': 'DESCRIPTION',
    'caption': 'DESCRIPTION',
    'theme': 'THEME',
    'instruments': 'INSTRUMENTS',
    'mood': 'MOOD',
    'vocal_style': 'VOCAL_STYLE',
    'production': 'PRODUCTION',
    'bpm': 'BPM',
    'key': 'KEY',
    'keyscale': 'KEY',
    'key_scale': 'KEY',
    'timesignature': 'TIMESIGNATURE',
    'time_signature': 'TIMESIGNATURE',
    'vocal_language': 'VOCAL_LANGUAGE',
    'language': 'VOCAL_LANGUAGE',
    'duration': 'DURATION',
    'length': 'DURATION',
    'lyrics': 'LYRICS',
}
_REQUIRED_LM_FIELDS = ('TITLE', 'STYLE', 'BPM', 'KEY', 'DURATION')


def _canonical_lm_label(label: Any) -> str:
    key = re.sub(r'[^A-Z0-9_]+', '_', str(label or '').strip().upper()).strip('_')
    return _LM_LABEL_ALIASES.get(key, key)


def _extract_json_object_snippet(text: str) -> str:
    raw = str(text or '').strip()
    if not raw:
        return ''
    if raw.startswith('{') and raw.endswith('}'):
        return raw
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        return raw[start:end + 1].strip()
    return ''


def _coerce_lm_json_payload(cleaned: str) -> Optional[tuple[dict[str, str], str]]:
    snippet = _extract_json_object_snippet(cleaned)
    if not snippet:
        return None
    try:
        payload = json.loads(snippet)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    fields: dict[str, str] = {}
    lyrics = ''
    for raw_key, raw_value in payload.items():
        label = _JSON_LM_KEY_ALIASES.get(str(raw_key or '').strip().lower())
        if not label:
            continue
        if label == 'LYRICS':
            lyrics = _compact_lyrics_lines(str(raw_value or '').splitlines()).strip()
            continue
        value = _sanitize_field_text(raw_value)
        if value:
            fields[label] = value
    if not fields.get('TITLE') or (not fields.get('STYLE') and not lyrics):
        return None
    return fields, lyrics

def _cleanup_runtime(mode: str = VRAM_CLEANUP_MODE) -> None:
    gc.collect()
    if torch is None or not getattr(torch, "cuda", None) or not torch.cuda.is_available():
        return
    with contextlib.suppress(Exception):
        torch.cuda.empty_cache()
    if mode in {"balanced", "aggressive"}:
        with contextlib.suppress(Exception):
            torch.cuda.ipc_collect()
    if mode == "aggressive":
        with contextlib.suppress(Exception):
            torch.cuda.synchronize()

def _save_settings_file(payload: dict[str, Any], path: Any = None) -> dict[str, Any]:
    target = _coerce_settings_path(path) if path is not None else SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp_path = target.with_suffix(target.suffix + '.tmp')
    tmp_path.write_text(data, encoding="utf-8")
    tmp_path.replace(target)
    _set_settings_path(target)
    _write_last_used_settings_path(target)
    stat = target.stat()
    return {
        "path": str(target),
        "bytes": len(data.encode("utf-8")),
        "mtime": stat.st_mtime,
        "exists": target.exists(),
    }


def _custom_catalog_browse_dir() -> Path:
    _ensure_outputs_layout()
    CUSTOM_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    return CUSTOM_CATALOG_DIR

def _sanitize_settings_payload(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key, value in dict(data).items():
        name = str(key or '')
        lowered = name.lower()
        if lowered.startswith('master_') and 'dsp' in lowered:
            continue
        if 'vst' in lowered:
            continue
        clean[name] = value
    return clean

def _load_settings_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return _sanitize_settings_payload(data if isinstance(data, dict) else {})
    except Exception:
        logger.exception("Failed to load AceRadio settings file")
        return {}

def _normalize_generation_mode(value: Any) -> str:
    mode = str(value or '').strip().lower()
    aliases = {
        'ai': 'ai_generated',
        'ai generated': 'ai_generated',
        'ai_generated': 'ai_generated',
        'ollama': 'ai_generated',
        'local catalog': 'local_catalog',
        'local_catalog': 'local_catalog',
        'local': 'local_catalog',
    }
    mode = aliases.get(mode, mode)
    return mode if mode in GENERATION_MODE_VALUES else ''


def _normalize_catalog_source(value: Any) -> str:
    source = str(value or '').strip().lower()
    aliases = {
        'ai catalog': 'generated',
        'ai_catalog': 'generated',
        'all local': 'all_local',
        'all_local': 'all_local',
        'custom catalog': 'custom',
        'custom_catalog': 'custom',
        'custom': 'custom',
    }
    source = aliases.get(source, source)
    return source if source in CATALOG_SOURCE_VALUES else 'library'


def _normalize_catalog_source_optional(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw:
        return ''
    return _normalize_catalog_source(raw)


def _normalize_legacy_generation_source(value: Any) -> str:
    source = str(value or '').strip().lower()
    aliases = {
        'ai': 'ai_generated',
        'ai generated': 'ai_generated',
        'ai_generated': 'ai_generated',
        'ollama': 'ai_generated',
        'local catalog': 'file',
        'local_catalog': 'file',
        'hybrid': 'both',
    }
    source = aliases.get(source, source)
    return source if source in LEGACY_GENERATION_SOURCE_VALUES else 'ai_generated'


def _resolve_generation_settings(generation_mode: Any, catalog_source: Any, generation_source: Any) -> tuple[str, str, str]:
    raw_mode = _normalize_generation_mode(generation_mode)
    raw_catalog = _normalize_catalog_source(catalog_source)
    raw_source = _normalize_legacy_generation_source(generation_source)
    if raw_mode:
        mode = raw_mode
    elif raw_source == 'file':
        mode = 'local_catalog'
    elif raw_source == 'both':
        mode = 'hybrid'
    elif raw_source == 'cache':
        mode = 'local_catalog'
    else:
        mode = 'ai_generated'
    if raw_catalog == 'all_local' and mode == 'hybrid':
        mode = 'local_catalog'
    source = 'both' if mode == 'hybrid' else 'file' if mode == 'local_catalog' else 'ai_generated'
    if raw_source == 'cache' and mode == 'local_catalog':
        source = 'cache'
    return mode, raw_catalog, source


def _normalize_track_source_key(value: Any, default: str = 'ai_generated') -> str:
    raw = str(value or '').strip().lower()
    aliases = {
        'ai': 'ai_generated',
        'ai generated': 'ai_generated',
        'ai_generated': 'ai_generated',
        'ollama': 'ai_generated',
        'local catalog': 'file',
        'local_catalog': 'file',
        'library': 'file',
        'file': 'file',
        'hybrid': 'both',
        'both': 'both',
        'cache': 'cache',
        'cached': 'cache',
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in {'ai_generated', 'file', 'both', 'cache'} else default


def _normalize_display_source_key(value: Any) -> str:
    raw = str(value or '').strip().lower()
    aliases = {
        'ai generated': 'ai_generated',
        'ai_generated': 'ai_generated',
        'generated': 'ai_generated',
        'ollama': 'ai_generated',
        'library': 'library',
        'imported': 'library',
        'file': 'library',
        'ai catalog': 'ai_catalog',
        'ai_catalog': 'ai_catalog',
        'generated catalog': 'ai_catalog',
        'generated_catalog': 'ai_catalog',
        'mixed': 'mixed',
        'both': 'mixed',
        'cached': 'cached',
        'cache': 'cached',
        'custom catalog': 'custom_catalog',
        'custom_catalog': 'custom_catalog',
        'custom': 'custom_catalog',
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in {'ai_generated', 'library', 'ai_catalog', 'mixed', 'cached', 'custom_catalog'} else ''


def _display_source_label(value: Any) -> str:
    key = _normalize_display_source_key(value)
    return {
        'ai_generated': 'AI generated',
        'library': 'Library',
        'ai_catalog': 'AI catalog',
        'mixed': 'Mixed',
        'cached': 'Cached',
        'custom_catalog': 'Custom catalog',
    }.get(key, 'AI generated')


def _resolve_display_source(source: Any, prompt: Any = None, instrumental: bool = False) -> str:
    raw_source = _normalize_track_source_key(source)
    prompt_data = prompt if isinstance(prompt, dict) else {}
    display = ''
    if raw_source == 'cache':
        display = _normalize_display_source_key(prompt_data.get('display_source') or prompt_data.get('display_source_label'))
        if not display:
            catalog_source = _normalize_catalog_source_optional(prompt_data.get('catalog_source'))
            if catalog_source == 'generated':
                display = 'ai_catalog'
            elif catalog_source == 'library':
                display = 'library'
            elif catalog_source == 'custom':
                display = 'custom_catalog'
            else:
                base_source = _normalize_track_source_key(prompt_data.get('source') or prompt_data.get('original_source') or '')
                display = 'library' if base_source == 'file' else 'mixed' if base_source == 'both' else 'ai_generated'
    elif raw_source == 'file':
        catalog_source = _normalize_catalog_source_optional(prompt_data.get('catalog_source'))
        display = 'custom_catalog' if catalog_source == 'custom' else 'ai_catalog' if catalog_source == 'generated' else 'library'
    elif raw_source == 'both':
        display = 'mixed'
    else:
        display = 'ai_generated'
    if instrumental and display in {'library', 'ai_catalog', 'mixed'}:
        display = 'ai_generated'
    return display


def _normalize_mp3_export_settings(audio_format: Any, mp3_bitrate: Any, mp3_sample_rate: Any) -> tuple[str, str, int]:
    final_format = str(audio_format or PLAYER_AUDIO_FORMAT).strip().lower() or PLAYER_AUDIO_FORMAT
    if final_format not in {'mp3', 'wav', 'flac', 'wav32', 'opus', 'aac'}:
        final_format = PLAYER_AUDIO_FORMAT
    final_bitrate = str(mp3_bitrate or ACERADIO_MP3_DEFAULT_BITRATE).strip().lower() or ACERADIO_MP3_DEFAULT_BITRATE
    if final_bitrate not in ACERADIO_MP3_BITRATE_OPTIONS:
        final_bitrate = ACERADIO_MP3_DEFAULT_BITRATE
    try:
        final_rate = int(mp3_sample_rate or ACERADIO_MP3_DEFAULT_SAMPLE_RATE)
    except Exception:
        final_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    if final_rate not in ACERADIO_MP3_SAMPLE_RATE_OPTIONS:
        final_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    if final_format != 'mp3':
        final_bitrate = ACERADIO_MP3_DEFAULT_BITRATE
        final_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    return final_format, final_bitrate, final_rate

def _normalize_settings_for_storage(data: dict[str, Any]) -> dict[str, Any]:
    clean = _sanitize_settings_payload(data)
    final_format, final_bitrate, final_rate = _normalize_mp3_export_settings(
        clean.get('audio_format'),
        clean.get('mp3_bitrate'),
        clean.get('mp3_sample_rate'),
    )
    clean['audio_format'] = final_format
    clean['mp3_bitrate'] = final_bitrate
    clean['mp3_sample_rate'] = final_rate
    clean['batch_size'] = 1
    clean.pop('auto_duration', None)
    clean['automatic_duration'] = bool(clean.get('automatic_duration', False))
    clean['min_duration'] = _coerce_duration_value(clean.get('min_duration'), 60)
    clean['max_duration'] = max(clean['min_duration'], _coerce_duration_value(clean.get('max_duration'), clean['min_duration']))
    clean['station_prompt'] = str(clean.get('station_prompt') or 'Late-night radio for city insomniacs: cinematic, melodic, emotionally rich, and never predictable.').strip()
    clean['station_negative_prompt'] = str(clean.get('station_negative_prompt') or '').strip()
    clean['shift'] = _resolve_shift_for_model(clean.get('model'), clean.get('shift', 3.0))
    clean['inference_steps'] = _resolve_inference_steps_for_model(clean.get('model'), clean.get('inference_steps', 8))
    clean['generation_mode'], clean['catalog_source'], clean['generation_source'] = _resolve_generation_settings(
        clean.get('generation_mode'),
        clean.get('catalog_source'),
        clean.get('generation_source'),
    )
    clean['generation_source_both_percent'] = _clamp_int(clean.get('generation_source_both_percent'), 50, 0, 100)
    custom_file = str(clean.get('custom_catalog_file') or '').strip()
    custom_enabled = bool(clean.get('custom_catalog_enabled', False))
    custom_info = _custom_catalog_file_info(CUSTOM_CATALOG_PATH) if custom_enabled else {'exists': False, 'path': str(CUSTOM_CATALOG_PATH), 'name': '', 'original_path': '', 'song_count': 0, 'ignored_count': 0}
    clean.pop(EMBEDDED_CUSTOM_CATALOG_KEY, None)
    if custom_enabled:
        resolved_original_path = custom_file or str(custom_info.get('original_path') or '').strip()
        resolved_name = str(clean.get('custom_catalog_name') or custom_info.get('name') or (Path(resolved_original_path).name if resolved_original_path else '') or CUSTOM_CATALOG_FILENAME)
        clean['custom_catalog_enabled'] = True
        clean['custom_catalog_file'] = resolved_original_path
        clean['custom_catalog_name'] = resolved_name
        clean['custom_catalog_song_count'] = int(custom_info.get('song_count') or clean.get('custom_catalog_song_count') or 0)
        clean['custom_catalog_ignored_count'] = int(custom_info.get('ignored_count') or clean.get('custom_catalog_ignored_count') or 0)
        clean['generation_mode'] = 'local_catalog'
        clean['catalog_source'] = 'custom'
        clean['generation_source'] = 'file'
    else:
        clean['custom_catalog_enabled'] = False
        clean['custom_catalog_file'] = ''
        clean['custom_catalog_name'] = ''
        clean['custom_catalog_song_count'] = 0
        clean['custom_catalog_ignored_count'] = 0
    if 'stream_format' in clean:
        clean['stream_format'] = str(clean.get('stream_format') or 'mp3').strip().lower() or 'mp3'
    if 'stream_bitrate' in clean:
        try:
            clean['stream_bitrate'] = max(16, min(512, int(clean.get('stream_bitrate') or 128)))
        except Exception:
            clean['stream_bitrate'] = 128
    clean['jingle_separator_arm_offset_s'] = _clamp_float(clean.get('jingle_separator_arm_offset_s'), 0.0, -60.0, 120.0)
    clean['jingle_separator_min_remaining_offset_s'] = _clamp_float(clean.get('jingle_separator_min_remaining_offset_s'), 0.0, -30.0, 30.0)
    clean['jingle_overlay_mid_offset_s'] = _clamp_float(clean.get('jingle_overlay_mid_offset_s'), 0.0, -120.0, 120.0)
    clean['jingle_overlay_trigger_window_s'] = _clamp_float(clean.get('jingle_overlay_trigger_window_s'), JINGLE_OVERLAY_MID_WINDOW_S, 0.25, 30.0)
    clean['jingle_overlay_min_duration_s'] = _clamp_float(clean.get('jingle_overlay_min_duration_s'), JINGLE_OVERLAY_MIN_DURATION_S, 0.0, 600.0)
    clean['admin_separator_fade_ms'] = _clamp_int(clean.get('admin_separator_fade_ms'), 500, 0, 10000)
    clean['admin_overlay_pre_duck_ms'] = _clamp_int(clean.get('admin_overlay_pre_duck_ms'), 300, 0, 10000)
    clean['admin_overlay_restore_ms'] = _clamp_int(clean.get('admin_overlay_restore_ms'), 700, 0, 10000)
    clean['auto_transition_cut_seconds'] = _clamp_int(clean.get('auto_transition_cut_seconds'), 0, 0, 7200)
    return clean

@dataclass
class BootstrapState:
    phase:str='booting'
    message:str='Starting AceRadio…'
    ready:bool=False
    error:str=''
    started_at:float=0.0
    completed_at:float=0.0

    def payload(self)->dict[str,Any]:
        data=asdict(self)
        data["busy"]=not self.ready and not self.error
        return data

class SongPrompt(BaseModel):
    song_title:str='Untitled Transmission'
    genre:str=''
    style:str=''
    theme:str=''
    caption:str=''
    instruments:str=''
    mood:str=''
    vocal_style:str=''
    production:str=''
    lyrics:str=''
    bpm:int=100
    key_scale:str='C Major'
    timesignature:str='4/4'
    duration:int=60
    catalog_source:str=''
    display_source:str=''
    display_source_label:str=''
    vocal_language:str=''
    @property
    def tags(self)->str:
        return ', '.join(filter(None,[self.genre or self.style,self.theme]))

class SelectedLoRA(BaseModel):
    id:str
    weight:float=0.6
    enabled:bool=True

class RadioStartRequest(BaseModel):
    genres:list[str]=Field(default_factory=list)
    themes:list[str]=Field(default_factory=list)
    languages:list[str]=Field(default_factory=lambda:['en'])
    station_prompt:str=''
    station_negative_prompt:str=''
    instrumental_probability:int=25
    min_duration:int=60
    max_duration:int=90
    automatic_duration:bool=False
    model:str=''
    selected_loras:list[SelectedLoRA]=Field(default_factory=list)
    batch_size:int=1
    use_adg:bool=False
    inference_steps:int=50
    infer_method:str='ode'
    guidance_scale:float=7.0
    shift:float=3.0
    cfg_interval_start:float=0.0
    cfg_interval_end:float=1.0
    enable_normalization:bool=True
    normalization_db:float=-1.0
    score_scale:float=0.5
    auto_score:bool=False
    latent_shift:float=0.0
    latent_rescale:float=1.0
    timesteps:str=''
    thinking:bool=True
    lm_temperature:float=0.85
    lm_cfg_scale:float=2.0
    lm_top_k:int=0
    lm_top_p:float=0.9
    lm_negative_prompt:str='NO USER INPUT'
    use_constrained_decoding:bool=True
    use_cot_metas:bool=True
    use_cot_caption:bool=True
    use_cot_language:bool=True
    parallel_thinking:bool=False
    constrained_decoding_debug:bool=False
    keep_history:int=12
    ui_language:str='en'
    language_rotation_mode:str='round_robin'
    vram_cleanup_mode:str=VRAM_CLEANUP_MODE
    max_saved_tracks:int=DEFAULT_MAX_SAVED_TRACKS
    lora_use_probability:int=100
    generation_mode:str='ai_generated'
    catalog_source:str='library'
    generation_source:str='ai_generated'
    generation_source_both_percent:int=50
    custom_catalog_enabled:bool=False
    custom_catalog_file:str=''
    custom_catalog_name:str=''
    custom_catalog_song_count:int=0
    custom_catalog_ignored_count:int=0
    reservoir_target:int=RESERVOIR_TARGET
    refill_threshold:int=RESERVOIR_REFILL_THRESHOLD
    audio_format:str=PLAYER_AUDIO_FORMAT
    mp3_bitrate:str=ACERADIO_MP3_DEFAULT_BITRATE
    mp3_sample_rate:int=ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    monitor_muted:bool=False
    jingle_separator_arm_offset_s:float=0.0
    jingle_separator_min_remaining_offset_s:float=0.0
    jingle_overlay_mid_offset_s:float=0.0
    jingle_overlay_trigger_window_s:float=JINGLE_OVERLAY_MID_WINDOW_S
    jingle_overlay_min_duration_s:float=JINGLE_OVERLAY_MIN_DURATION_S
    admin_separator_fade_ms:int=500
    admin_overlay_pre_duck_ms:int=300
    admin_overlay_restore_ms:int=700
    auto_transition_cut_seconds:int=0
    auth_username:str=''
    auth_password:str=''

@dataclass
class Track:
    id:str
    job_id:str
    song_title:str
    tags:str
    lyrics:str
    bpm:int
    key_scale:str
    duration:int
    created_at:float
    audio_bytes:bytes
    audio_mime:str
    seed:str
    prompt:dict[str,Any]
    language:str
    genre:str=''
    theme:str=''
    instrumental:bool=False
    lora_id:str=''
    audio_path:str=''
    source:str='ai_generated'
    vote_count:int=0
    real_duration:Optional[float]=None

def _build_lm_negative_prompt(station_negative: str, lm_negative: str) -> str:
    DEFAULT_FALLBACK = (
        'long silent intro, dead air at start, silence longer than 1 second at beginning, '
        'overlong fade-out, silence at end longer than 3 seconds, repetitive generic lyrics, '
        'empty arrangement, weak monotonous structure, abrupt cut-off'
    )
    PLACEHOLDER = 'NO USER INPUT'
    sn = str(station_negative or '').strip()
    ln = str(lm_negative or '').strip()
    if ln.upper() == PLACEHOLDER.upper():
        ln = ''
    parts = [p for p in [sn, ln] if p]
    result = ' | '.join(parts) if parts else DEFAULT_FALLBACK
    return result

_CONSERVATIVE_TITLE_PROPER_PHRASES = {
    'new york', 'los angeles', 'san francisco', 'las vegas', 'new orleans', 'san diego', 'san jose',
    'rio de janeiro', 'buenos aires', 'hong kong', 'new jersey', 'new mexico', 'abu dhabi',
    'sao paulo', 'mexico city', 'cape town', 'las palmas', 'new delhi', 'santa monica',
    'saint louis', 'st louis', 'san remo',
}
_CONSERVATIVE_TITLE_PROPER_WORDS = {
    'new', 'york', 'los', 'angeles', 'san', 'francisco', 'las', 'vegas', 'rio', 'janeiro',
    'buenos', 'aires', 'hong', 'kong', 'abu', 'dhabi', 'sao', 'paulo', 'mexico', 'cape', 'town',
    'delhi', 'santa', 'monica', 'saint', 'louis', 'remo',
}
_TITLE_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:['’\-][A-Za-zÀ-ÿ0-9]+)*", re.UNICODE)


def _title_case_token(token: str) -> str:
    def _cap_piece(piece: str) -> str:
        return piece[:1].upper() + piece[1:].lower() if piece else ''
    token = str(token or '')
    parts = re.split(r"(['’\-])", token)
    rebuilt: list[str] = []
    for part in parts:
        if part in {"'", '’', '-'}:
            rebuilt.append(part)
        else:
            rebuilt.append(_cap_piece(part.lower()))
    return ''.join(rebuilt)


def _conservative_title_case(text: str) -> str:
    raw = re.sub(r'\s+', ' ', str(text or '').strip())
    if not raw:
        return ''
    words = [match.group(0) for match in _TITLE_WORD_RE.finditer(raw)]
    if not words:
        return raw
    lower_words = [word.lower() for word in words]
    preserve: dict[int, str] = {}
    for phrase in _CONSERVATIVE_TITLE_PROPER_PHRASES:
        parts = phrase.split()
        size = len(parts)
        if not size:
            continue
        canonical = [_title_case_token(part) for part in parts]
        for index in range(0, len(lower_words) - size + 1):
            if lower_words[index:index + size] == parts:
                for offset, token in enumerate(canonical):
                    preserve[index + offset] = token
    rendered: list[str] = []
    cursor = 0
    word_index = 0
    for match in _TITLE_WORD_RE.finditer(raw):
        rendered.append(raw[cursor:match.start()])
        token = match.group(0)
        lower = token.lower()
        if word_index in preserve:
            replacement = preserve[word_index]
        elif token.isupper() and len(token) <= 5:
            replacement = token
        elif any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token):
            replacement = token
        elif word_index == 0:
            replacement = _title_case_token(token)
        elif lower in _CONSERVATIVE_TITLE_PROPER_WORDS and token[:1].isupper():
            replacement = _title_case_token(token)
        else:
            replacement = token.lower()
        rendered.append(replacement)
        cursor = match.end()
        word_index += 1
    rendered.append(raw[cursor:])
    return ''.join(rendered)


def _strip_llm_artifacts_text(value: Any, *, keep_newlines: bool = False) -> str:
    text = str(value or '')
    if not text:
        return ''
    marker_match = _LLM_TRAILING_ARTIFACT_RE.search(text)
    if marker_match:
        text = text[:marker_match.start()]
    for marker in FORBIDDEN_LM_MARKERS:
        text = text.replace(marker, ' ')
    if keep_newlines:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
    return re.sub(r'\s+', ' ', text).strip()


def _normalize_vocal_language_code(value: Any, default: str = '') -> str:
    raw = _strip_llm_artifacts_text(value)
    if not raw:
        return str(default or '').strip().lower()
    cleaned = raw.strip().lower()
    if cleaned in {'instrumental', '[instrumental]'}:
        return 'instrumental'
    if cleaned in LANGUAGE_DISPLAY_NAMES:
        return cleaned
    reverse = {str(name or '').strip().lower(): code for code, name in LANGUAGE_DISPLAY_NAMES.items()}
    aliases = {
        'english': 'en', 'italian': 'it', 'italiano': 'it', 'spanish': 'es', 'espanol': 'es', 'español': 'es',
        'french': 'fr', 'francais': 'fr', 'français': 'fr', 'german': 'de', 'deutsch': 'de',
        'greek': 'el', 'hellenic': 'el', 'chinese': 'zh', 'mandarin': 'zh', 'japanese': 'ja', 'korean': 'ko',
        'swedish': 'sv', 'finnish': 'fi',
    }
    if cleaned in reverse:
        return reverse[cleaned]
    if cleaned in aliases:
        return aliases[cleaned]
    if re.fullmatch(r'[a-z]{2,3}', cleaned):
        return cleaned[:2]
    fallback = str(default or '').strip().lower()
    return fallback or cleaned


def _sanitize_title(t: str) -> str:
    import re as _re
    t = _strip_llm_artifacts_text(t)
    t = str(t or '').strip().rstrip('.,;:!?').strip()
    if _re.match(r'^\[', t):
        return ''
    return _conservative_title_case(t)


def _sanitize_field_text(value: Any) -> str:
    text = _strip_llm_artifacts_text(value)
    text = text.strip("\"'")
    text = re.sub(r'\s+', ' ', text).strip()
    return text.rstrip(',;')


def _coerce_field_int(value: Any) -> Optional[int]:
    text = str(value or '').strip()
    if not text:
        return None
    mmss = re.search(r'(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)', text)
    if mmss:
        try:
            return int(mmss.group(1)) * 60 + int(mmss.group(2))
        except Exception:
            pass
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        return None
    try:
        return int(float(match.group(0)))
    except Exception:
        return None


def _normalize_key_scale(value: Any) -> str:
    text = _sanitize_field_text(value)
    if not text:
        return ''
    compact = text.replace('♯', '#').replace('♭', 'b')
    compact = re.sub(r'\s+', ' ', compact).strip()
    match = re.fullmatch(r'([A-Ga-g])\s*([#b]?)(?:\s*[-/]?\s*)(maj(?:or)?|min(?:or)?|m)?', compact, re.I)
    if match:
        note = (match.group(1) or '').upper() + (match.group(2) or '')
        mode_raw = (match.group(3) or '').strip().lower()
        if mode_raw in {'m', 'min', 'minor'}:
            mode = 'Minor'
        else:
            mode = 'Major'
        return f'{note} {mode}'.strip()
    word_match = re.fullmatch(r'([A-Ga-g])\s*([#b]?)\s+(major|minor)', compact, re.I)
    if word_match:
        note = (word_match.group(1) or '').upper() + (word_match.group(2) or '')
        mode = 'Minor' if str(word_match.group(3) or '').strip().lower() == 'minor' else 'Major'
        return f'{note} {mode}'.strip()
    return re.sub(r'\bminor\b', 'Minor', re.sub(r'\bmajor\b', 'Major', compact, flags=re.I), flags=re.I)


def _extract_lm_fields_and_lyrics(raw: str) -> tuple[dict[str, str], str]:
    labels = {_canonical_lm_label(label) for label in (set(_KEY_MAP) | {'LYRICS'})}
    cleaned = _strip_llm_fences(raw)
    for marker in FORBIDDEN_LM_MARKERS:
        cleaned = re.sub(re.escape(marker), ' ', cleaned, flags=re.I)
    cleaned = cleaned.strip()
    json_payload = _coerce_lm_json_payload(cleaned)
    if json_payload is not None:
        return json_payload
    title_match = re.search(r'^\s*(TITLE|SONG(?:_|\s+)TITLE)\s*:?$', cleaned, re.I | re.M)
    if not title_match:
        title_match = re.search(r'^\s*(TITLE|SONG(?:_|\s+)TITLE)\s*:?', cleaned, re.I | re.M)
    if not title_match:
        raise RuntimeError('Ollama DJ returned unparsable content without TITLE')
    cleaned = cleaned[title_match.start():].strip()
    lines = [str(line or '').rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    fields: dict[str, str] = {}
    lyrics_lines: list[str] = []
    in_lyrics = False
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            if in_lyrics and lyrics_lines and lyrics_lines[-1] != '':
                lyrics_lines.append('')
            index += 1
            continue
        match = re.match(r'^([A-Z][A-Z0-9_ ]*)\s*:?(?:\s+(.*))?$', line, re.I)
        label = _canonical_lm_label(match.group(1) if match else '') if match else ''
        if match and label in labels:
            remainder = _sanitize_field_text(match.group(2) or '')
            if label == 'LYRICS':
                in_lyrics = True
                if remainder:
                    lyrics_lines.append(remainder)
                index += 1
                continue
            if not remainder:
                probe = index + 1
                value_lines: list[str] = []
                while probe < len(lines):
                    candidate = lines[probe].strip()
                    if not candidate:
                        if value_lines:
                            break
                        probe += 1
                        continue
                    candidate_match = re.match(r'^([A-Z][A-Z0-9_ ]*)\s*:?(?:\s+.*)?$', candidate, re.I)
                    candidate_label = _canonical_lm_label(candidate_match.group(1) if candidate_match else '') if candidate_match else ''
                    if candidate_label in labels:
                        break
                    value_lines.append(_sanitize_field_text(candidate))
                    probe += 1
                if value_lines:
                    remainder = ' '.join(part for part in value_lines if part).strip()
                    index = probe - 1
            if remainder:
                fields[label] = remainder
            index += 1
            continue
        if in_lyrics:
            lyrics_lines.append(line)
        index += 1
    lyrics = _compact_lyrics_lines(lyrics_lines).strip()
    if not lyrics:
        verse_match = re.search(r'\[(?:Verse\s*1|V1)\]', cleaned, re.I)
        if verse_match:
            lyrics = _compact_lyrics_lines(cleaned[verse_match.start():].splitlines()).strip()
    return fields, lyrics

def _clean_lyric_content_line(line: str) -> str:
    text = _strip_llm_artifacts_text(line)
    text = re.sub(r'\s+', ' ', str(text or '').strip()).strip()
    if not text:
        return ''
    if re.fullmatch(r'\[(?:end|eos)\]', text, re.I):
        return ''
    if re.fullmatch(r'\([^)]{1,160}\)', text):
        lowered = text.lower()
        if any(keyword in lowered for keyword in _STAGE_DIRECTION_KEYWORDS):
            return ''
    label_match = re.match(r'^([A-Z][A-Z_0-9]*)\s*:\s*', text, re.I)
    if label_match and label_match.group(1).upper() not in {'V1', 'V2'}:
        return ''
    return text


def _trim_final_chorus_lines(lines: list[str], earlier_chorus_blocks: list[list[str]], target: int) -> list[str]:
    cleaned = [line for line in [_clean_lyric_content_line(line) for line in lines] if line]
    if not cleaned and earlier_chorus_blocks:
        fallback_block = [line for line in (_clean_lyric_content_line(line) for line in (earlier_chorus_blocks[-1] or [])) if line]
        if fallback_block:
            cleaned = list(fallback_block)
    if len(cleaned) <= target:
        return cleaned
    earlier = {re.sub(r'\s+', ' ', line.strip().lower()) for block in earlier_chorus_blocks for line in block if str(line or '').strip()}
    repeated: list[str] = []
    fresh: list[str] = []
    for line in cleaned:
        normalized = re.sub(r'\s+', ' ', line.strip().lower())
        if normalized in earlier and len(repeated) < 5:
            repeated.append(line)
        elif normalized not in earlier and len(fresh) < max(0, target - 5):
            fresh.append(line)
    candidate = repeated + fresh
    for line in cleaned:
        if len(candidate) >= target:
            break
        candidate.append(line)
    return candidate[:target]


def _rebalance_adjacent_lyric_sections(sections: list[dict[str, Any]], targets: list[int]) -> None:
    if not sections:
        return
    moved = True
    while moved:
        moved = False
        for index in range(len(sections) - 1):
            current = sections[index]
            nxt = sections[index + 1]
            current_target = targets[index] if index < len(targets) else 0
            next_target = targets[index + 1] if index + 1 < len(targets) else 0
            if current_target <= 0 or next_target <= 0:
                continue
            current_lines = list(current.get('lines') or [])
            next_lines = list(nxt.get('lines') or [])
            if len(current_lines) <= current_target or len(next_lines) >= next_target:
                continue
            movable = max(0, len(current_lines) - current_target)
            need = max(0, next_target - len(next_lines))
            take = min(movable, need)
            if take <= 0:
                continue
            spill = current_lines[-take:]
            current['lines'] = current_lines[:-take]
            nxt['lines'] = spill + next_lines
            moved = True


def _repair_vocal_lyrics_structure(lyrics: str) -> str:
    parsed = _parse_lyrics_sections(lyrics)
    raw_sections = parsed['sections']
    if not raw_sections:
        return _canonicalize_vocal_lyrics(lyrics)
    template = _infer_lyric_structure_template([section['name'] for section in raw_sections])
    sequence = list(template.get('sequence') or CANONICAL_LYRIC_SECTION_SEQUENCE)
    target_lines = list(template.get('target_lines') or CANONICAL_LYRIC_SECTION_TARGET_LINES)
    rebuilt: list[dict[str, Any]] = []
    cursor = 0
    for expected_name in sequence:
        matched: Optional[dict[str, Any]] = None
        probe = cursor
        while probe < len(raw_sections):
            candidate = raw_sections[probe]
            if candidate['name'] == expected_name:
                matched = candidate
                cursor = probe + 1
                break
            probe += 1
        if matched is None:
            continue
        cleaned_lines = [line for line in (_clean_lyric_content_line(raw_line) for raw_line in matched.get('lines', [])) if line]
        rebuilt.append({'name': expected_name, 'lines': cleaned_lines})
    if not rebuilt:
        return _canonicalize_vocal_lyrics(lyrics)
    _rebalance_adjacent_lyric_sections(rebuilt, target_lines)
    out_lines: list[str] = []
    earlier_chorus_blocks: list[list[str]] = []
    for index, section in enumerate(rebuilt):
        target = target_lines[index] if index < len(target_lines) else 0
        cleaned_lines = [line for line in (_clean_lyric_content_line(raw_line) for raw_line in section.get('lines', [])) if line]
        if section['name'] == 'Final Chorus':
            cleaned_lines = _trim_final_chorus_lines(cleaned_lines, earlier_chorus_blocks, target or 7)
        elif target > 0 and len(cleaned_lines) > target:
            cleaned_lines = cleaned_lines[:target]
        if section['name'] == 'Chorus':
            earlier_chorus_blocks.append(list(cleaned_lines))
        if out_lines:
            out_lines.append('')
        out_lines.append(f'[{section["name"]}]')
        out_lines.extend(cleaned_lines)
    return _compact_lyrics_lines(out_lines)


def _prompt_validation_issues(prompt: SongPrompt, instrumental: bool, target_genre: str = '', target_theme: str = '', target_language: str = '', source: str = 'ai_generated') -> list[str]:
    issues = []
    if _title_looks_placeholder(prompt.song_title):
        issues.append('title')
    if not _field_has_real_text(prompt.style, min_len=3):
        issues.append('style')
    custom_catalog_prompt = _normalize_catalog_source_optional(getattr(prompt, 'catalog_source', '') or '') == 'custom'
    if not custom_catalog_prompt and not _field_has_real_text(prompt.theme, min_len=3):
        issues.append('theme')
    prompt_genre_value = _sanitize_field_text(prompt.genre or '')
    if target_genre and prompt_genre_value and _norm_label(prompt_genre_value) != _norm_label(target_genre):
        issues.append('style_exact')
    if target_theme and prompt.theme and _norm_label(prompt.theme) != _norm_label(target_theme):
        issues.append('theme_exact')
    if not custom_catalog_prompt and not _field_has_real_text(prompt.instruments, min_len=3):
        issues.append('instruments')
    if not custom_catalog_prompt and not _field_has_real_text(prompt.mood, min_len=3):
        issues.append('mood')
    if not _field_has_real_text(prompt.vocal_style, min_len=3):
        issues.append('vocal_style')
    if not custom_catalog_prompt and not _field_has_real_text(prompt.production, min_len=3):
        issues.append('production')
    if int(prompt.bpm or 0) <= 0:
        issues.append('bpm')
    if not str(prompt.key_scale or '').strip():
        issues.append('key')
    if int(prompt.duration or 0) <= 0:
        issues.append('duration')
    if not _lyrics_have_content(prompt.lyrics, instrumental):
        issues.append('lyrics')
    issues.extend(_lyrics_language_issues(prompt.lyrics, target_language, instrumental))
    issues.extend(_lyrics_structure_issues(prompt.lyrics, instrumental))
    return issues

def _prompt_has_structural_quality(prompt: SongPrompt, instrumental: bool, target_genre: str = '', target_theme: str = '', target_language: str = '', source: str = 'ai_generated') -> bool:
    return not _prompt_quality_issues(prompt, instrumental, target_genre, target_theme, target_language, source)


class FileSongLibrary:
    def __init__(self, path: Path = SONGS_PATH, outputs_root: Path = OUTPUTS_ROOT):
        self.path = path
        self.outputs_root = outputs_root
        self.generated_path = outputs_root / GENERATED_SONGS_HISTORY_FILENAME
        self.custom_path = CUSTOM_CATALOG_PATH
        self._catalog_songs: dict[str, list[dict[str, Any]]] = {'library': [], 'generated': [], 'custom': []}
        self._catalog_sources: dict[str, list[str]] = {'library': [], 'generated': [], 'custom': []}
        self.reload()

    def _extract_examples(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            if isinstance(raw.get('examples'), list):
                return [x for x in raw.get('examples') if isinstance(x, dict)]
            if isinstance(raw.get('songs'), list):
                return [x for x in raw.get('songs') if isinstance(x, dict)]
            return []
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    def _load_file(self, path: Path, catalog_key: str) -> list[dict[str, Any]]:
        try:
            raw = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            display_source = 'custom_catalog' if catalog_key == 'custom' else 'ai_catalog' if catalog_key == 'generated' else 'library'
            tagged: list[dict[str, Any]] = []
            for item in self._extract_examples(raw):
                if not isinstance(item, dict):
                    continue
                song = dict(item)
                if str(song.get('title') or '').strip():
                    song['title'] = _sanitize_title(str(song.get('title') or ''))
                song['_catalog_source'] = 'custom' if catalog_key == 'custom' else 'generated' if catalog_key == 'generated' else 'library'
                song['_display_source'] = display_source
                tagged.append(song)
            return tagged
        except Exception:
            logger.exception('Failed to load songs file: %s', path)
            return []

    def reload(self) -> None:
        catalog_songs: dict[str, list[dict[str, Any]]] = {'library': [], 'generated': [], 'custom': []}
        catalog_sources: dict[str, list[str]] = {'library': [], 'generated': [], 'custom': []}
        base_songs = self._load_file(self.path, 'library')
        if base_songs:
            catalog_songs['library'].extend(base_songs)
            catalog_sources['library'].append(str(self.path))
        generated_songs = self._load_file(self.generated_path, 'generated')
        if generated_songs:
            catalog_songs['generated'].extend(generated_songs)
            catalog_sources['generated'].append(str(self.generated_path))
        custom_songs = self._load_file(self.custom_path, 'custom')
        if custom_songs:
            catalog_songs['custom'].extend(custom_songs)
            catalog_sources['custom'].append(str(self.custom_path))
        self._catalog_songs = catalog_songs
        self._catalog_sources = catalog_sources
        logger.info(
            'AceRadio songs library loaded: library=%s generated=%s custom=%s',
            len(self._catalog_songs['library']),
            len(self._catalog_songs['generated']),
            len(self._catalog_songs['custom']),
        )

    def _norm(self, value: Any) -> str:
        return str(value or '').strip().lower()

    def _dedupe_songs(self, songs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for song in songs:
            key = (
                self._norm(song.get('title')),
                self._norm(song.get('vocal_language')),
                self._norm(song.get('style')),
                self._norm(song.get('duration')),
                self._norm(song.get('lyrics')),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(song)
        return deduped

    def _catalog_pool(self, catalog_source: str) -> tuple[list[dict[str, Any]], list[str]]:
        source = _normalize_catalog_source(catalog_source)
        if source == 'custom':
            return list(self._catalog_songs['custom']), list(self._catalog_sources['custom'])
        if source == 'generated':
            return list(self._catalog_songs['generated']), list(self._catalog_sources['generated'])
        if source == 'all_local':
            songs = self._dedupe_songs(list(self._catalog_songs['library']) + list(self._catalog_songs['generated']))
            sources = list(self._catalog_sources['library']) + [x for x in self._catalog_sources['generated'] if x not in self._catalog_sources['library']]
            return songs, sources
        return list(self._catalog_songs['library']), list(self._catalog_sources['library'])

    def _lang_match(self, song: dict[str, Any], language: str) -> bool:
        song_lang = self._norm(song.get('vocal_language'))
        target_lang = self._norm(language)
        if not song_lang or song_lang == 'unknown' or song_lang != target_lang:
            return False
        lyrics = str(song.get('lyrics') or '').strip()
        if not _lyrics_have_content(lyrics, False):
            return False
        report = _language_content_report(target_lang, lyrics, False)
        return bool(report.get('accepted') or report.get('uncertain'))

    def _genre_match(self, song: dict[str, Any], genre: str) -> bool:
        if not genre:
            return True
        hay = ' '.join([str(song.get('pack') or ''), str(song.get('title') or ''), str(song.get('description') or ''), str(song.get('style') or '')]).lower()
        wanted = self._norm(genre)
        return not wanted or wanted in hay

    def _theme_match_score(self, song: dict[str, Any], theme: str) -> int:
        report = _theme_coherence_report(
            theme,
            ' '.join([str(song.get('title') or ''), str(song.get('description') or ''), str(song.get('lyrics') or '')]),
        )
        return int(report['covered_parts'] * 10 + report['total_score'])

    def choose_custom(self, history: list[str]) -> SongPrompt:
        self.reload()
        songs = [s for s in self._catalog_songs['custom'] if isinstance(s, dict)]
        if not songs:
            raise RuntimeError('No usable songs found in active custom catalog')
        recent = {str(x).strip().lower() for x in history[-20:] if str(x).strip()}
        fresh = [s for s in songs if str(s.get('title') or '').strip().lower() not in recent]
        song = random.choice(fresh or songs)
        song_instrumental = bool(song.get('instrumental')) or _lyrics_indicates_instrumental(song.get('lyrics'))
        return SongPrompt(
            song_title=(_sanitize_title(str(song.get('title') or '')) or 'Untitled Broadcast'),
            genre=str(song.get('genre') or song.get('style') or '').strip(),
            style=str(song.get('style') or song.get('genre') or '').strip(),
            theme=str(song.get('theme') or '').strip(),
            caption=str(song.get('caption') or song.get('description') or '').strip(),
            instruments=str(song.get('pack') or '').replace('_', ' '),
            mood=str(song.get('description') or '').strip(),
            vocal_style='instrumental' if song_instrumental else 'expressive lead vocal',
            production=str(song.get('production') or '').strip(),
            bpm=int(song.get('bpm') or 110),
            key_scale=str(song.get('keyscale') or 'C Major').strip() or 'C Major',
            timesignature=str(song.get('timesignature') or song.get('time_signature') or '4/4').strip() or '4/4',
            duration=int(song.get('duration') or 180),
            lyrics='[Instrumental]' if song_instrumental else str(song.get('lyrics') or '').strip(),
            catalog_source='custom',
            display_source='custom_catalog',
            display_source_label=_display_source_label('custom_catalog'),
            vocal_language=str(song.get('vocal_language') or '').strip().lower(),
        )

    def choose(self, genre: str, theme: str, history: list[str], duration: int, language: str, instrumental: bool, catalog_source: str = 'library') -> SongPrompt:
        self.reload()
        pool, sources = self._catalog_pool(catalog_source)
        songs = [s for s in pool if isinstance(s, dict) and self._lang_match(s, language) and self._genre_match(s, genre)]
        if not songs:
            songs = [s for s in pool if isinstance(s, dict) and self._lang_match(s, language)]
        if not songs:
            source_label = ', '.join(sources) if sources else 'selected local catalog'
            raise RuntimeError(f'No usable songs found for language {language} in {source_label}')
        scored = [(self._theme_match_score(s, theme), s) for s in songs]
        best_theme_score = max((score for score, _ in scored), default=0)
        if best_theme_score > 0:
            songs = [song for score, song in scored if score == best_theme_score]
        recent = {str(x).strip().lower() for x in history[-20:] if str(x).strip()}
        fresh = [s for s in songs if str(s.get('title') or '').strip().lower() not in recent]
        pool = fresh or songs
        song = random.choice(pool)
        lyrics = str(song.get('lyrics') or '').strip()
        if instrumental:
            lyrics = '[Instrumental]'
        picked_catalog = _normalize_catalog_source(song.get('_catalog_source'))
        display_source = 'custom_catalog' if picked_catalog == 'custom' else 'ai_catalog' if picked_catalog == 'generated' else 'library'
        return SongPrompt(
            song_title=(_sanitize_title(str(song.get('title') or '')) or 'Untitled Broadcast'),
            genre=str(genre or song.get('genre') or song.get('style') or song.get('description') or '').strip(),
            style=str(song.get('style') or genre or song.get('genre') or song.get('description') or '').strip(),
            theme=str(theme or song.get('theme') or '').strip(),
            caption=str(song.get('caption') or song.get('description') or '').strip(),
            instruments=str(song.get('pack') or '').replace('_', ' '),
            mood=str(song.get('description') or '').strip(),
            vocal_style='instrumental' if instrumental else ('expressive lead vocal' if language != 'unknown' else 'radio vocal'),
            production=str(song.get('production') or '').strip(),
            bpm=int(song.get('bpm') or 110),
            key_scale=str(song.get('keyscale') or 'C Major').strip() or 'C Major',
            timesignature=str(song.get('timesignature') or song.get('time_signature') or '4/4').strip() or '4/4',
            duration=int(song.get('duration') or duration or 180),
            lyrics=lyrics,
            catalog_source=picked_catalog,
            display_source=display_source,
            display_source_label=_display_source_label(display_source),
            vocal_language=str(language or '').strip().lower(),
        )

class OllamaDJ:
    def __init__(self):
        self._base_timeout = httpx.Timeout(
            connect=OLLAMA_CHAT_CONNECT_TIMEOUT,
            read=OLLAMA_CHAT_READ_TIMEOUT,
            write=max(30.0, OLLAMA_CHAT_CONNECT_TIMEOUT),
            pool=max(30.0, OLLAMA_CHAT_CONNECT_TIMEOUT),
        )
        self.client=httpx.AsyncClient(timeout=self._base_timeout)
    async def close(self): await self.client.aclose()

    def _model_name(self) -> str:
        return str(OLLAMA_MODEL or '').strip()

    def _model_name_lower(self) -> str:
        return self._model_name().lower()

    def _model_size_billions(self, model_name: str = '') -> float:
        match = re.search(r':\s*(\d+(?:\.\d+)?)\s*b\b', str(model_name or self._model_name()).strip().lower())
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except Exception:
            return 0.0

    def _is_qwen_model(self, model_name: str = '') -> bool:
        return 'qwen' in str(model_name or self._model_name()).strip().lower()

    def _is_deepseek_model(self, model_name: str = '') -> bool:
        return 'deepseek' in str(model_name or self._model_name()).strip().lower()

    def _fallback_model_name(self, primary_model: str = '') -> str:
        primary = str(primary_model or self._model_name()).strip()
        lowered = primary.lower()
        if 'deepseek' in lowered:
            return 'qwen3.5:9b'
        if 'qwen' in lowered:
            return 'deepseek-r1:8b'
        return 'deepseek-r1:8b'

    def _candidate_model_names(self) -> list[str]:
        primary = self._model_name()
        fallback = self._fallback_model_name(primary)
        ordered: list[str] = []
        for item in (primary, fallback):
            model = str(item or '').strip()
            if model and model not in ordered:
                ordered.append(model)
        return ordered or ['qwen3.5:9b', 'deepseek-r1:8b']

    def _model_profile(self, model_name: str = '') -> dict[str, Any]:
        resolved = str(model_name or self._model_name()).strip()
        lowered = resolved.lower()
        if 'deepseek' in lowered:
            family = 'deepseek'
        elif 'qwen' in lowered:
            family = 'qwen'
        else:
            family = 'generic'
        return {
            'family': family,
            'size_b': self._model_size_billions(resolved),
            'template_pool': ['classic_full', 'intro_classic', 'verse_chorus', 'intro_verse_chorus'],
            'prompt_contract': 'minimal_structured_block',
        }

    def _stable_ollama_seed(self, target_genre: str, lyrical_theme: str, language: str, duration_text: str, automatic_duration: bool, history_block: str, model_name: str = '') -> int:
        material = '|'.join([
            str(model_name or self._model_name()).strip().lower(),
            str(target_genre or '').strip().lower(),
            str(lyrical_theme or '').strip().lower(),
            str(language or '').strip().lower(),
            str(duration_text or '').strip().lower(),
            'auto' if automatic_duration else 'fixed',
            str(history_block or '').strip().lower(),
        ])
        digest = hashlib.sha256(material.encode('utf-8')).hexdigest()
        return max(1, int(digest[:8], 16))

    def _pick_structure_template(self, profile: dict[str, Any], target_genre: str, lyrical_theme: str, language: str, duration_text: str, automatic_duration: bool, history_block: str, model_name: str = '') -> dict[str, Any]:
        pool = list(profile.get('template_pool') or [DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID])
        if not pool:
            return _lyric_structure_template(DEFAULT_LYRIC_STRUCTURE_TEMPLATE_ID)
        seed = self._stable_ollama_seed(target_genre, lyrical_theme, language, duration_text, automatic_duration, history_block, model_name=model_name)
        template_id = pool[seed % len(pool)]
        return _lyric_structure_template(template_id)

    def _metadata_contract(self, lang_name: str) -> str:
        return (
            'Inside it, provide a structured plain-text response with these fields in this exact order, one per line: '
            'TITLE, DESCRIPTION, STYLE, BPM, DURATION, KEYSCALE, TIMESIGNATURE, VOCAL_LANGUAGE, LYRICS.'
        )

    def _system_message(self, profile: dict[str, Any], template: dict[str, Any]) -> str:
        return "You're a professional lyricist and songwriter."

    def _user_message(self, profile: dict[str, Any], template: dict[str, Any], lang_name: str, target_genre: str, lyrical_theme: str, station_text: str, duration_clause: str, history_block: str, language: str) -> str:
        lang_code = _normalize_vocal_language_code(language, language)
        parts: list[str] = [
            f'Genre: {target_genre} | Theme: {lyrical_theme} | Vocal language: {lang_code} | Duration: {duration_clause}.',
            'Please reply with exactly one fenced code block and nothing else.',
            self._metadata_contract(lang_name),
            'Use classic section tags such as [Intro], [Verse 1], [Chorus], [Verse 2], [Bridge], [Final Chorus], [Outro].',
            'If you use [Intro], feel free to vary it creatively by song: it can be sung, sparse, spoken, or atmospheric, and it may use a few short lines when musically natural rather than following a fixed pattern.',
            'Make the lyrics clearly and recognizably reflect the requested theme in natural wording.',
        ]
        if lang_code and lang_code != 'en':
            parts.append(f'Write DESCRIPTION and STYLE in English, but write all LYRICS in natural {lang_name}.')
        else:
            parts.append('Write all text in natural English.')
        if station_text:
            parts.append(f'Creative direction: {station_text}.')
        if history_block and history_block != 'none yet':
            parts.append(f'Avoid these titles: {history_block}.')
        return ' '.join(str(part or '').strip() for part in parts if str(part or '').strip())

    def _build_chat_payload(self, model_name: str, target_genre: str, lyrical_theme: str, language: str, duration_text: str, automatic_duration: bool, history_block: str, system: str, user: str) -> dict[str, Any]:
        profile = self._model_profile(model_name)
        family = str(profile.get('family') or 'generic')
        seed = self._stable_ollama_seed(target_genre, lyrical_theme, language, duration_text, automatic_duration, history_block, model_name=model_name)
        temperature = 0.35
        top_p = 0.9
        if family == 'deepseek':
            temperature = 0.25
            top_p = 0.85
        elif family == 'qwen' and self._model_size_billions(model_name) >= 8.0:
            temperature = 0.3
            top_p = 0.88
        payload: dict[str, Any] = {
            'model': str(model_name or '').strip(),
            'stream': False,
            'keep_alive': OLLAMA_KEEP_ALIVE,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'think': False,
            'options': {
                'seed': seed,
                'temperature': temperature,
                'top_p': top_p,
            },
        }
        return payload

    async def generate(self, target_genre:str, lyrical_theme:str, station_prompt:str, history:list[str], duration:Optional[int], language:str, instrumental:bool, automatic_duration:bool=False)->SongPrompt:
        lang_name = LANGUAGE_DISPLAY_NAMES.get(language, language)
        history_block = ', '.join(str(x or '').strip() for x in history[-6:] if str(x or '').strip()) or 'none yet'
        station_text = re.sub(r'\s+', ' ', str(station_prompt or '').strip())[:320]
        target_duration = max(30, int(duration or 180))
        duration_text = str(target_duration)
        duration_clause = 'choose one integer between 170 and 230' if automatic_duration else duration_text
        model_errors: list[str] = []
        for model_name in self._candidate_model_names():
            profile = self._model_profile(model_name)
            template = self._pick_structure_template(profile, target_genre, lyrical_theme, language, duration_text, automatic_duration, history_block, model_name=model_name)
            system = self._system_message(profile, template)
            user = self._user_message(profile, template, lang_name, target_genre, lyrical_theme, station_text, duration_clause, history_block, language)
            payload = self._build_chat_payload(model_name, target_genre, lyrical_theme, language, duration_text, automatic_duration, history_block, system, user)
            last_exc: Optional[Exception] = None
            try:
                for attempt in range(1, OLLAMA_CHAT_RETRIES + 1):
                    try:
                        logger.info('AceRadio DJ prompt start (model=%s, attempt %s/%s, read_timeout=%.0fs)', model_name, attempt, OLLAMA_CHAT_RETRIES, OLLAMA_CHAT_READ_TIMEOUT)
                        timeout = httpx.Timeout(
                            connect=OLLAMA_CHAT_CONNECT_TIMEOUT,
                            read=OLLAMA_CHAT_READ_TIMEOUT,
                            write=max(30.0, OLLAMA_CHAT_CONNECT_TIMEOUT),
                            pool=max(30.0, OLLAMA_CHAT_CONNECT_TIMEOUT),
                        )
                        r = await self.client.post(
                            f'{OLLAMA_BASE_URL}/api/chat',
                            json=payload,
                            timeout=timeout,
                        )
                        r.raise_for_status()
                        response_json = r.json() or {}
                        raw = str(((response_json.get('message') or {}).get('content') or '')).strip()
                        logger.info('AceRadio DJ prompt end (model=%s, attempt=%s, response length=%d)', model_name, attempt, len(raw))
                        if not raw:
                            raise RuntimeError(f'Ollama DJ returned empty content for model {model_name}')
                        prompt = self._parse(raw, duration, instrumental)
                        try:
                            await self.unload_model(model_name)
                        except Exception:
                            logger.exception('AceRadio failed to unload Ollama model after request')
                        return prompt
                    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.HTTPStatusError, RuntimeError, ValueError) as e:
                        last_exc = e
                        logger.warning('AceRadio DJ prompt fail (model=%s, attempt %s/%s): %s', model_name, attempt, OLLAMA_CHAT_RETRIES, e)
                        if attempt >= OLLAMA_CHAT_RETRIES:
                            break
                        await asyncio.sleep(OLLAMA_CHAT_RETRY_BACKOFF * attempt)
            finally:
                with contextlib.suppress(Exception):
                    await self.unload_model(model_name)
            if last_exc is not None:
                model_errors.append(f'{model_name}: {last_exc}')
        raise RuntimeError('Ollama DJ failed across all configured models: ' + ' | '.join(model_errors))

    async def unload_model(self, model_name: str = '') -> None:
        resolved = str(model_name or OLLAMA_MODEL or '').strip()
        if resolved:
            with contextlib.suppress(Exception):
                await self.client.post(
                    f'{OLLAMA_BASE_URL}/api/generate',
                    json={'model': resolved, 'prompt': '', 'stream': False, 'keep_alive': 0},
                )
        _cleanup_runtime('aggressive')

    def _parse(self, raw:str, duration:int, instrumental:bool)->SongPrompt:
        fields, lyrics = _extract_lm_fields_and_lyrics(raw)
        missing = [label for label in _REQUIRED_LM_FIELDS if not _sanitize_field_text(fields.get(label) or '')]
        if missing:
            raise RuntimeError('Ollama DJ returned missing required fields: ' + ', '.join(missing))
        kwargs: dict[str, Any] = {'lyrics': lyrics}
        for label, name in _KEY_MAP.items():
            value = _sanitize_field_text(fields.get(label) or '')
            if not value:
                continue
            if name in {'bpm', 'duration'}:
                parsed_int = _coerce_field_int(value)
                if parsed_int is not None:
                    kwargs[name] = parsed_int
            elif name == 'song_title':
                kwargs[name] = _sanitize_title(value)
            elif name == 'key_scale':
                kwargs[name] = _normalize_key_scale(value)
            else:
                kwargs[name] = value
        prompt = SongPrompt(**kwargs)
        requested_duration = None
        try:
            requested_duration = int(duration) if duration is not None else None
        except Exception:
            requested_duration = None
        parsed_duration = None
        try:
            parsed_duration = int(prompt.duration) if prompt.duration is not None else None
        except Exception:
            parsed_duration = None
        if requested_duration is None:
            prompt.duration = max(30, min(parsed_duration or 180, 600))
        else:
            prompt.duration = max(30, min(parsed_duration or requested_duration, max(30, requested_duration)))
        prompt.caption = _sanitize_field_text(prompt.caption or fields.get('DESCRIPTION') or '')
        prompt.style = _sanitize_field_text(prompt.style or '')
        prompt.genre = _sanitize_field_text(prompt.genre or '')
        prompt.theme = _sanitize_field_text(prompt.theme or '')
        prompt.instruments = _sanitize_field_text(prompt.instruments or '') or prompt.genre or prompt.style
        prompt.mood = _sanitize_field_text(prompt.mood or '') or prompt.caption or prompt.theme or prompt.style
        prompt.vocal_style = _sanitize_field_text(prompt.vocal_style or '') or ('instrumental' if instrumental else 'expressive lead vocal')
        prompt.production = _sanitize_field_text(prompt.production or '') or prompt.style
        prompt.timesignature = _sanitize_field_text(getattr(prompt, 'timesignature', '') or '') or '4/4'
        prompt.vocal_language = _normalize_vocal_language_code(prompt.vocal_language or '')
        if instrumental:
            prompt.lyrics = '[Instrumental]'
        else:
            prompt.lyrics = _repair_vocal_lyrics_structure(_canonicalize_vocal_lyrics(prompt.lyrics))
        return prompt


def _normalize_track_language(language: str, instrumental: bool) -> str:
    value = str(language or '').strip()
    if instrumental:
        return value or 'instrumental'
    return value or 'unknown'

def _normalize_track_lyrics(lyrics: str, instrumental: bool) -> str:
    text = str(lyrics or '').strip()
    if instrumental:
        return '[Instrumental]'
    return _canonicalize_vocal_lyrics(text)

def _lyrics_have_content(lyrics: str, instrumental: bool) -> bool:
    text = _normalize_track_lyrics(lyrics, instrumental)
    if instrumental:
        return bool(text.strip())
    stripped_lines = []
    for raw_line in text.splitlines():
        line = str(raw_line or '').strip()
        if not line:
            continue
        if re.fullmatch(r'\[[^\]]+\]', line):
            continue
        line = re.sub(r'\[[^\]]+\]', ' ', line)
        line = re.sub(r"[^A-Za-zÀ-ÿ\u0370-\u03ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af0-9'’ -]+", ' ', line)
        line = re.sub(r'\s+', ' ', line).strip()
        if len(line) >= 8 and re.search(r'[A-Za-zÀ-ÿ\u0370-\u03ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]', line):
            stripped_lines.append(line)
    if len(stripped_lines) >= 2:
        return True
    if stripped_lines and sum(len(x) for x in stripped_lines) >= 32:
        return True
    return False


def _lyrics_plain_text(lyrics: str, instrumental: bool = False) -> str:
    text = _normalize_track_lyrics(lyrics, instrumental)
    if instrumental:
        return ''
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = str(raw_line or '').strip()
        if not line or re.fullmatch(r'\[[^\]]+\]', line):
            continue
        lines.append(line)
    return '\n'.join(lines).strip()


def _language_marker_hits(language: str, text: str) -> list[str]:
    normalized = re.sub(r"[^\wÀ-ÿͰ-Ͽ぀-ヿ一-鿿가-힯']+", ' ', str(text or '').lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    if not normalized:
        return []
    hits: list[str] = []
    for marker in LANGUAGE_MARKERS.get(language, ()):
        token = str(marker or '').strip().lower()
        if not token:
            continue
        if re.search(rf'(?<!\w){re.escape(token)}(?!\w)', normalized):
            hits.append(token)
    return sorted(set(hits))


def _language_content_report(language: str, lyrics: str, instrumental: bool = False) -> dict[str, Any]:
    target = str(language or '').strip().lower()
    plain = _lyrics_plain_text(lyrics, instrumental)
    if instrumental or not target or not plain:
        return {'accepted': True, 'uncertain': False, 'target_language': target, 'best_language': target, 'target_score': 0, 'best_other_score': 0, 'scores': {}}
    script_scores: dict[str, int] = {}
    for code, pattern in LANGUAGE_SCRIPT_PATTERNS.items():
        script_scores[code] = len(pattern.findall(plain))
    scores: dict[str, int] = {}
    for code in LANGUAGE_DISPLAY_NAMES:
        marker_hits = _language_marker_hits(code, plain)
        score = len(marker_hits) * 3
        script_score = script_scores.get(code, 0)
        if script_score:
            score += min(script_score, 12)
        scores[code] = score
    target_score = int(scores.get(target, 0))
    other_scores = {code: value for code, value in scores.items() if code != target}
    best_language = max(other_scores, key=other_scores.get, default=target)
    best_other_score = int(other_scores.get(best_language, 0)) if other_scores else 0
    accepted = True
    uncertain = False
    if target in LANGUAGE_SCRIPT_PATTERNS:
        accepted = bool(script_scores.get(target, 0)) or target_score >= max(3, best_other_score + 1)
        uncertain = not accepted and best_other_score < 4
    else:
        if target_score >= max(3, best_other_score + 1):
            accepted = True
        elif best_other_score >= max(4, target_score + 2):
            accepted = False
        else:
            accepted = True
            uncertain = True
    return {
        'accepted': accepted,
        'uncertain': uncertain,
        'target_language': target,
        'best_language': best_language,
        'target_score': target_score,
        'best_other_score': best_other_score,
        'scores': scores,
    }


def _lyrics_language_issues(lyrics: str, target_language: str, instrumental: bool) -> list[str]:
    if instrumental:
        return []
    report = _language_content_report(target_language, lyrics, instrumental)
    if report.get('accepted') or report.get('uncertain'):
        return []
    best_language = str(report.get('best_language') or '').strip()
    if best_language and best_language != str(target_language or '').strip().lower():
        return [f'lyrics_language:{best_language}']
    return ['lyrics_language']

def _title_looks_placeholder(title: str) -> bool:
    value = str(title or '').strip()
    if not value:
        return True
    lowered = value.lower()
    if lowered in {'untitled', 'untitled track', 'untitled broadcast', 'untitled transmission', 'song title', 'title'}:
        return True
    if re.fullmatch(r'[0-9a-f]{8,}(?:-[0-9a-f]{4,}){0,4}', lowered):
        return True
    if len(re.sub(r'[^a-z0-9]+', '', lowered)) < 4:
        return True
    return False

def _field_has_real_text(value: str, *, min_len: int = 6) -> bool:
    text = re.sub(r'\s+', ' ', str(value or '').strip())
    if len(text) < min_len:
        return False
    if text in {'...', 'n/a', 'none', 'unknown', 'tbd', 'todo'}:
        return False
    return bool(re.search(r'[A-Za-zÀ-ÿ\u0370-\u03ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]', text))

def _theme_signal_matches(theme: str, lyrics_text: str) -> tuple[int, dict[str, list[str]]]:
    report = _theme_coherence_report(theme, lyrics_text)
    matches = {
        _norm_label(str(item.get('theme_part') or '')): list(item.get('matches') or [])
        for item in report['parts']
        if int(item.get('score') or 0) > 0
    }
    return report['covered_parts'], matches

def _theme_quality_issues(prompt: SongPrompt, instrumental: bool, target_theme: str = '', target_language: str = '', source: str = 'ai_generated') -> list[str]:
    if instrumental or source == 'file':
        return []
    requested_language = str(target_language or '').strip().lower()
    if requested_language and requested_language != 'en':
        return []
    theme_value = str(target_theme or prompt.theme or '').strip()
    if not theme_value or not _lyrics_have_content(prompt.lyrics, instrumental):
        return []
    report = _theme_coherence_report(theme_value, prompt.lyrics)
    if report['accepted']:
        return []
    missing = [str(item.get('theme_part') or '').strip() for item in report['parts'] if int(item.get('score') or 0) <= 0]
    prefix = 'theme_lyrics_partial:' if report.get('severity') == 'partial' else 'theme_lyrics_failed:'
    if missing:
        return [prefix + ', '.join(missing)]
    return [prefix.rstrip(':')]

def _prompt_quality_issues(prompt: SongPrompt, instrumental: bool, target_genre: str = '', target_theme: str = '', target_language: str = '', source: str = 'ai_generated') -> list[str]:
    issues = list(_prompt_validation_issues(prompt, instrumental, target_genre, target_theme, target_language, source))
    issues.extend(_theme_quality_issues(prompt, instrumental, target_theme, target_language, source))
    return issues

def _is_theme_quality_issue(issue: str) -> bool:
    return str(issue or '').strip().lower().startswith('theme_lyrics')

def _is_relaxed_structure_issue(issue: str) -> bool:
    issue_text = str(issue or '').strip()
    return issue_text.startswith('lyrics_short:') or issue_text in CANONICAL_LYRIC_SECTION_OPTIONAL_WARNINGS


def _split_prompt_quality_issues(issues: list[str]) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    fatals: list[str] = []
    for issue in issues:
        if str(issue).startswith('theme_lyrics_partial') or _is_relaxed_structure_issue(issue):
            warnings.append(issue)
        else:
            fatals.append(issue)
    return fatals, warnings

def _finalize_prompt(prompt: SongPrompt, duration: int, language: str, instrumental: bool, source: str) -> SongPrompt:
    title = _sanitize_title(prompt.song_title) or 'Untitled Broadcast'
    key_scale = str(prompt.key_scale or '').strip() or 'C Major'
    bpm = int(prompt.bpm or 100)
    bpm = max(40, min(240, bpm))
    prompt.song_title = title
    prompt.key_scale = key_scale
    prompt.bpm = bpm
    prompt.duration = max(30, min(int(prompt.duration or duration or 60), max(30, int(duration or 60))))
    prompt.lyrics = _normalize_track_lyrics(prompt.lyrics, instrumental)
    if not instrumental:
        prompt.lyrics = _repair_vocal_lyrics_structure(prompt.lyrics)
    prompt.genre = str(prompt.genre or '').strip()
    prompt.style = str(prompt.style or prompt.genre or '').strip()
    prompt.theme = ' / '.join(_split_theme_parts(prompt.theme)) or str(prompt.theme or '').strip()
    prompt.caption = _strip_caption_prefix(getattr(prompt, 'caption', '') or '', prompt.song_title, prompt.genre or prompt.style)
    prompt.instruments = str(prompt.instruments or '').strip() or prompt.genre or prompt.style
    prompt.mood = str(prompt.mood or '').strip() or prompt.caption or prompt.theme or prompt.style
    prompt.vocal_style = str(prompt.vocal_style or ('instrumental' if instrumental else f'{language} vocal')).strip()
    prompt.timesignature = str(getattr(prompt, 'timesignature', '') or '').strip() or '4/4'
    prompt.vocal_language = _normalize_vocal_language_code(getattr(prompt, 'vocal_language', '') or '', language)
    custom_catalog_prompt = _normalize_catalog_source_optional(getattr(prompt, 'catalog_source', '') or '') == 'custom'
    prompt.production = str(prompt.production or '').strip() if custom_catalog_prompt else str(prompt.production or prompt.style or f'{source} radio generation').strip()
    if not prompt.genre:
        prompt.genre = str(prompt.style or '').strip()
    issues = _prompt_quality_issues(prompt, instrumental, prompt.genre or prompt.style, prompt.theme, language, source)
    fatal_issues, warning_issues = _split_prompt_quality_issues(issues)
    non_theme_fatals = [issue for issue in fatal_issues if not _is_theme_quality_issue(issue)]
    theme_fatals = [issue for issue in fatal_issues if _is_theme_quality_issue(issue)]
    if non_theme_fatals:
        raise RuntimeError('Generated track is invalid: ' + ', '.join(non_theme_fatals))
    if theme_fatals or warning_issues:
        logger.warning('AceRadio prompt accepted with relaxed theme gate: %s', ', '.join(theme_fatals + warning_issues))
    if not instrumental and str(prompt.theme or '').strip() and _lyrics_have_content(prompt.lyrics, instrumental):
        report = _theme_coherence_report(prompt.theme, prompt.lyrics)
        if report['accepted']:
            logger.debug('AceRadio theme coherence accepted for "%s": %s', prompt.theme, report['parts'])
        elif source == 'file' or report.get('severity') == 'partial':
            logger.info('AceRadio prompt accepted with relaxed theme gate for "%s": %s', prompt.theme, report['parts'])
    return prompt


def _estimate_audio_duration_from_lyrics(lyrics: str, fallback: int, instrumental: bool) -> int:
    base = max(120, min(600, int(fallback or 180)))
    if instrumental or not _lyrics_have_content(lyrics, instrumental):
        return base
    line_count = 0
    word_count = 0
    for raw_line in str(lyrics or '').splitlines():
        line = str(raw_line or '').strip()
        if not line or re.fullmatch(r'\[[^\]]+\]', line):
            continue
        clean = re.sub(r'\[[^\]]+\]', ' ', line)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if not clean:
            continue
        line_count += 1
        word_count += len(re.findall(r"[A-Za-zÀ-ÿ\u0370-\u03ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af0-9'’]+", clean))
    if line_count <= 0 and word_count <= 0:
        return base
    estimated = int(round(24 + (line_count * 1.2) + (word_count * 0.52)))
    upper_bound = max(180, min(420, max(base + 90, 360)))
    return max(120, min(estimated, upper_bound))

def _resolve_audio_generation_duration(prompt: SongPrompt, requested_duration: int, automatic_duration: bool, instrumental: bool) -> int:
    requested = max(30, min(600, int(requested_duration or 180)))
    if not automatic_duration:
        return requested
    fallback = max(120, min(600, int(getattr(prompt, 'duration', 0) or requested or 180)))
    return _estimate_audio_duration_from_lyrics(getattr(prompt, 'lyrics', ''), fallback, instrumental)

def _track_uses_custom_catalog(track: Optional[Track]) -> bool:
    if not track:
        return False
    prompt = dict(getattr(track, 'prompt', {}) or {})
    catalog_source = _normalize_catalog_source_optional(prompt.get('catalog_source') or getattr(track, 'catalog_source', ''))
    if catalog_source == 'custom':
        return True
    display_source = _normalize_display_source_key(prompt.get('display_source') or prompt.get('display_source_label') or getattr(track, 'display_source', ''))
    return display_source == 'custom_catalog'


def _build_track(prompt: SongPrompt, *, duration: int, language: str, instrumental: bool, source: str, job_id: str, audio_bytes: bytes, audio_mime: str, seed: str, lora_id: str, audio_path: str) -> Track:
    source = _normalize_track_source_key(source)
    prompt = _finalize_prompt(prompt, duration, language, instrumental, source)
    if source == 'file':
        prompt.catalog_source = _normalize_catalog_source(getattr(prompt, 'catalog_source', '') or 'library')
    else:
        prompt.catalog_source = ''
    prompt.display_source = _resolve_display_source(source, {'catalog_source': getattr(prompt, 'catalog_source', ''), 'display_source': getattr(prompt, 'display_source', '')}, instrumental)
    prompt.display_source_label = _display_source_label(prompt.display_source)
    return Track(
        id=str(uuid.uuid4()),
        job_id=job_id,
        song_title=prompt.song_title,
        tags=prompt.tags,
        lyrics=prompt.lyrics,
        bpm=int(prompt.bpm),
        key_scale=prompt.key_scale,
        duration=int(duration),
        created_at=time.time(),
        audio_bytes=audio_bytes,
        audio_mime=audio_mime,
        seed=seed,
        prompt={**prompt.model_dump(), 'source': source},
        language=_normalize_track_language(language, instrumental),
        genre=str(prompt.genre or prompt.style or '').strip(),
        theme=str(prompt.theme or '').strip(),
        instrumental=bool(instrumental),
        lora_id=lora_id,
        audio_path=str(audio_path or ''),
        source=source,
    )

def _merge_backup_prompt(primary: SongPrompt, backup: SongPrompt, instrumental: bool = False) -> SongPrompt:
    if instrumental:
        primary.lyrics = '[Instrumental]'
    else:
        primary.lyrics = backup.lyrics or primary.lyrics
    if not str(primary.style or '').strip():
        primary.style = backup.style
    if not str(primary.theme or '').strip():
        primary.theme = backup.theme
    if not str(primary.caption or '').strip():
        primary.caption = backup.caption
    if not str(primary.instruments or '').strip():
        primary.instruments = backup.instruments
    if not str(primary.mood or '').strip():
        primary.mood = backup.mood
    if not str(primary.vocal_style or '').strip():
        primary.vocal_style = backup.vocal_style
    if not str(primary.production or '').strip():
        primary.production = backup.production
    if not str(primary.catalog_source or '').strip():
        primary.catalog_source = backup.catalog_source
    if not str(primary.display_source or '').strip():
        primary.display_source = backup.display_source
    if not str(primary.display_source_label or '').strip():
        primary.display_source_label = backup.display_source_label
    return primary

def _title_looks_machine_generated(title: str, *, job_name: str = '', file_stem: str = '') -> bool:
    t = str(title or '').strip()
    if not t:
        return True
    low = t.lower()
    if low in {'cached track', 'untitled broadcast', 'untitled transmission'}:
        return True
    parts = [x.lower() for x in (job_name, file_stem) if str(x or '').strip()]
    if low in parts:
        return True
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', low):
        return True
    if re.fullmatch(r'[0-9a-f]{32,}', low):
        return True
    return False

def _fallback_title_from_lyrics(lyrics: str) -> str:
    for raw in str(lyrics or '').splitlines():
        line = str(raw).strip()
        if not line or line.startswith('['):
            continue
        line = re.sub(r'\s+', ' ', line).strip()
        if len(line) > 64:
            line = line[:64].rstrip()
        return _sanitize_title(line)
    return ''

def _stable_track_id(job_id: str, audio_path: str) -> str:
    base = f"{str(job_id or '').strip()}|{str(audio_path or '').strip()}"
    if not base.strip('|'):
        base = str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base))

def _job_dir_signature(job_dir: Path) -> tuple[float, int, int]:
    newest = 0.0
    file_count = 0
    total_bytes = 0
    try:
        for entry in job_dir.rglob('*'):
            try:
                st = entry.stat()
            except Exception:
                continue
            newest = max(newest, float(st.st_mtime))
            if entry.is_file():
                file_count += 1
                total_bytes += int(st.st_size)
    except Exception:
        with contextlib.suppress(Exception):
            st = job_dir.stat()
            newest = float(st.st_mtime)
            total_bytes = int(st.st_size)
    return (newest, file_count, total_bytes)

def _job_dir_is_still_settling(job_dir: Path, *, metadata_present: bool = False, sidecar_present: bool = False) -> bool:
    sig = _job_dir_signature(job_dir)
    newest = float(sig[0] or 0.0)
    age = time.time() - newest if newest > 0 else 999999.0
    if age >= OUTPUTS_CACHE_STABILIZE_SECONDS:
        return False
    if sidecar_present:
        return False
    return True

def _job_dir_has_audio_artifact(job_dir: Path) -> bool:
    try:
        for entry in job_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
                return True
    except Exception:
        return False
    return False

def _job_dir_is_pending_without_sidecar(job_dir: Path, *, metadata_present: bool = False, sidecar_present: bool = False) -> bool:
    if sidecar_present:
        return False
    sig = _job_dir_signature(job_dir)
    newest = float(sig[0] or 0.0)
    age = time.time() - newest if newest > 0 else 999999.0
    if age < OUTPUTS_CACHE_STABILIZE_SECONDS:
        return True
    if age < OUTPUTS_CACHE_FINALIZE_GRACE_SECONDS and (metadata_present or _job_dir_has_audio_artifact(job_dir)):
        return True
    return False

def _choose_best_audio_file(job_dir: Path, metadata: Optional[dict[str, Any]] = None) -> Optional[Path]:
    exts = {'.mp3', '.flac', '.wav', '.opus', '.aac'}
    candidates: list[Path] = []
    try:
        candidates = [p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    except Exception:
        return None
    if not candidates:
        return None

    def _looks_auxiliary(name: str) -> bool:
        low = str(name or '').lower()
        return any(token in low for token in ('preview', 'waveform', 'spectr', 'thumb', 'temp', 'segment', 'proxy'))

    meta = metadata or {}
    declared = []
    for raw in (meta.get('audio_paths') or []):
        try:
            declared.append(Path(str(raw)).resolve())
        except Exception:
            pass
    for cand in candidates:
        try:
            if cand.resolve() in declared:
                return cand
        except Exception:
            pass

    usable = [p for p in candidates if not _looks_auxiliary(p.name)] or candidates
    usable.sort(key=lambda p: (p.stat().st_size if p.exists() else 0, p.name.lower()), reverse=True)
    return usable[0] if usable else None

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default

def _listener_vote_fingerprint(listener_id: str) -> str:
    raw = str(listener_id or '').strip()
    if not raw:
        return ''
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _get_client_ip(request: Request) -> str:
    try:
        xff = request.headers.get('x-forwarded-for') or request.headers.get('X-Forwarded-For')
        if xff:
            first = str(xff).split(',')[0].strip()
            if first:
                return first
    except Exception:
        pass
    try:
        xr = request.headers.get('x-real-ip') or request.headers.get('X-Real-IP')
        if xr and str(xr).strip():
            return str(xr).strip()
    except Exception:
        pass
    try:
        host = getattr(getattr(request, 'client', None), 'host', None)
        if host:
            return str(host)
    except Exception:
        pass
    return ''

def _load_sidecar_json(job_dir: Path) -> dict[str, Any]:
    sidecar = job_dir / ACERADIO_TRACK_META_FILENAME
    try:
        if sidecar.exists() and sidecar.is_file():
            raw = json.loads(sidecar.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                return raw
    except Exception:
        logger.debug('AceRadio: failed to read sidecar %s', sidecar, exc_info=True)
    return {}

def _vote_scope_label(scope: str) -> str:
    raw = str(scope or '').strip().lower()
    if raw in {'listener_cookie', 'cookie-scoped'}:
        return '1 per browser'
    if raw in {'listener_ip', 'ip-scoped'}:
        return '1 per IP'
    return str(scope or '1 per browser').strip() or '1 per browser'

def _extract_vote_info(meta: Optional[dict[str, Any]]) -> tuple[int, list[str]]:
    payload = meta if isinstance(meta, dict) else {}
    raw_voters = payload.get('vote_voters') if isinstance(payload.get('vote_voters'), list) else []
    voters = [str(x).strip() for x in raw_voters if str(x).strip()]
    vote_count = max(0, _safe_int(payload.get('vote_count'), 0))
    vote_count = max(vote_count, len(voters))
    return vote_count, voters

def _track_sidecar_payload(track: Track) -> dict[str, Any]:
    prompt = dict(getattr(track, 'prompt', {}) or {})
    real_dur = getattr(track, 'real_duration', None)
    persisted_source = _normalize_track_source_key(prompt.get('source') or getattr(track, 'source', '') or 'ai_generated')
    persisted_catalog_source = _normalize_catalog_source_optional(prompt.get('catalog_source')) if persisted_source == 'file' else ''
    safe_title = _sanitize_title(str(getattr(track, 'song_title', '') or ''))
    return {
        'id': str(getattr(track, 'id', '') or ''),
        'track_id': str(getattr(track, 'id', '') or ''),
        'job_id': str(getattr(track, 'job_id', '') or ''),
        'song_title': safe_title,
        'lyrics': str(getattr(track, 'lyrics', '') or ''),
        'bpm': int(getattr(track, 'bpm', 100) or 100),
        'key_scale': str(getattr(track, 'key_scale', '') or 'C Major'),
        'duration': int(round(real_dur)) if (real_dur and real_dur > 0) else int(getattr(track, 'duration', 60) or 60),
        'real_duration': float(real_dur) if (real_dur and real_dur > 0) else None,
        'language': str(getattr(track, 'language', '') or 'en'),
        'genre': str(getattr(track, 'genre', '') or prompt.get('genre') or prompt.get('style') or ''),
        'theme': str(getattr(track, 'theme', '') or prompt.get('theme') or ''),
        'caption': _derive_track_caption(track),
        'instrumental': bool(getattr(track, 'instrumental', False)),
        'lora_id': str(getattr(track, 'lora_id', '') or ''),
        'seed': str(getattr(track, 'seed', '') or ''),
        'audio_path': str(getattr(track, 'audio_path', '') or ''),
        'audio_mime': str(getattr(track, 'audio_mime', '') or ''),
        'source': persisted_source,
        'catalog_source': persisted_catalog_source,
        'display_source': _resolve_display_source(getattr(track, 'source', ''), prompt, bool(getattr(track, 'instrumental', False))),
        'display_source_label': _display_source_label(_resolve_display_source(getattr(track, 'source', ''), prompt, bool(getattr(track, 'instrumental', False)))),
        'style': str(prompt.get('style') or getattr(track, 'tags', '') or ''),
        'tags': str(getattr(track, 'tags', '') or ''),
        'model': str(prompt.get('model') or ''),
        'audio_format': str(prompt.get('audio_format') or ''),
        'mp3_bitrate': str(prompt.get('mp3_bitrate') or ''),
        'mp3_sample_rate': int(prompt.get('mp3_sample_rate') or 0),
        'inference_steps': int(prompt.get('inference_steps') or 0),
        'infer_method': str(prompt.get('infer_method') or ''),
        'guidance_scale': float(prompt.get('guidance_scale') or 0.0),
        'shift': float(prompt.get('shift') or 0.0),
        'vote_count': max(0, int(getattr(track, 'vote_count', 0) or 0)),
        'vote_voters': [],
        'written_at': time.time(),
    }


def _songs_schema_version() -> str:
    try:
        raw = json.loads(SONGS_PATH.read_text(encoding='utf-8')) if SONGS_PATH.exists() else {}
        if isinstance(raw, dict):
            value = str(raw.get('version') or '').strip()
            if value:
                return value
    except Exception:
        logger.debug('AceRadio: failed to read songs schema version from %s', SONGS_PATH, exc_info=True)
    return 'creative_rewrite_v1_en_caption_fields'


def _generated_song_entry(track: Track) -> dict[str, Any]:
    prompt = dict(getattr(track, 'prompt', {}) or {})
    if _normalize_track_source_key(getattr(track, 'source', '')) != 'ai_generated':
        return {}
    genre = str(getattr(track, 'genre', '') or prompt.get('genre') or '').strip()
    theme = str(getattr(track, 'theme', '') or prompt.get('theme') or '').strip()
    language = _normalize_vocal_language_code(prompt.get('vocal_language') or getattr(track, 'language', '') or 'unknown') or 'unknown'
    timesignature = str(prompt.get('timesignature') or '').strip() or '4/4'
    duration = int(round(getattr(track, 'real_duration', 0) or 0)) if (getattr(track, 'real_duration', None) and getattr(track, 'real_duration', 0) > 0) else int(getattr(track, 'duration', 0) or 180)
    style = str(prompt.get('style') or genre or 'generated radio song').strip()
    description = _strip_caption_prefix(str(prompt.get('caption') or ''), getattr(track, 'song_title', ''), genre or style)
    if not description:
        description_bits = []
        if genre:
            description_bits.append(f'A generated {genre} song')
        else:
            description_bits.append('A generated song')
        if language and language != 'unknown':
            description_bits.append(f'in {language}')
        if theme:
            description_bits.append(f'about {theme}')
        description = ' '.join(description_bits).strip()
    safe_title = _sanitize_title(str(getattr(track, 'song_title', '') or '').strip()) or 'Untitled Transmission'
    entry = {
        'pack': 'ai_generated',
        'title': safe_title,
        'description': description,
        'style': style,
        'lyrics': str(getattr(track, 'lyrics', '') or '').strip(),
        'bpm': int(getattr(track, 'bpm', 100) or 100),
        'duration': max(1, duration),
        'keyscale': str(getattr(track, 'key_scale', '') or 'C Major').strip() or 'C Major',
        'timesignature': timesignature,
        'vocal_language': language,
    }
    return {key: entry.get(key) for key in GENERATED_SONGS_ENTRY_KEYS}


def _normalize_generated_songs_root(raw: Any) -> dict[str, Any]:
    version = _songs_schema_version()
    examples_raw: list[Any] = []
    if isinstance(raw, dict):
        raw_version = str(raw.get('version') or '').strip()
        if raw_version:
            version = raw_version
        if isinstance(raw.get('examples'), list):
            examples_raw = list(raw.get('examples') or [])
        elif isinstance(raw.get('songs'), list):
            examples_raw = list(raw.get('songs') or [])
    elif isinstance(raw, list):
        examples_raw = list(raw)
    examples: list[dict[str, Any]] = []
    for item in examples_raw:
        if not isinstance(item, dict):
            continue
        cleaned = {key: item.get(key) for key in GENERATED_SONGS_ENTRY_KEYS}
        cleaned['title'] = _sanitize_title(str(cleaned.get('title') or ''))
        if not str(cleaned.get('title') or '').strip():
            continue
        examples.append(cleaned)
    return {'version': version, 'count': len(examples), 'examples': examples}


def _generated_songs_dated_path(created_at: Optional[float] = None) -> Path:
    try:
        ts = float(created_at) if created_at is not None else time.time()
    except Exception:
        ts = time.time()
    return OUTPUTS_ROOT / f"{GENERATED_SONGS_DATED_PREFIX}{time.strftime('%y%m%d', time.localtime(ts))}.json"


def _load_generated_songs_payload(path: Path) -> dict[str, Any]:
    raw: Any = {}
    if path.exists() and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            logger.warning('AceRadio: failed to read %s, rebuilding generated songs file', path, exc_info=True)
            raw = {}
    return _normalize_generated_songs_root(raw)


def _write_generated_songs_payload(path: Path, payload: dict[str, Any]) -> None:
    normalized = _normalize_generated_songs_root(payload)
    text = json.dumps(normalized, ensure_ascii=False, indent=2) + '\n'
    path.write_text(text, encoding='utf-8')


def _append_generated_song_history(track: Optional[Track]) -> None:
    try:
        if not track or _normalize_track_source_key(getattr(track, 'source', '')) != 'ai_generated':
            return
        entry = _generated_song_entry(track)
        if not str(entry.get('title') or '').strip() or not str(entry.get('lyrics') or '').strip():
            return
        OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)
        dated_path = _generated_songs_dated_path(getattr(track, 'created_at', None))
        with GENERATED_SONGS_LOCK:
            for path in (GENERATED_SONGS_HISTORY_PATH, dated_path):
                payload = _load_generated_songs_payload(path)
                payload['examples'].append(entry)
                payload['count'] = len(payload['examples'])
                _write_generated_songs_payload(path, payload)
    except Exception:
        logger.warning('AceRadio: failed to append generated song history for %s', getattr(track, 'song_title', None), exc_info=True)


def _job_dir_has_track_artifacts(job_dir: Path) -> bool:
    try:
        if not job_dir or not Path(job_dir).exists() or not Path(job_dir).is_dir():
            return False
        exts = {'.mp3', '.wav', '.flac', '.opus', '.aac', '.wav32'}
        for entry in job_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() in exts:
                return True
            if entry.name in {ACERADIO_TRACK_META_FILENAME, 'metadata.json', 'generation.log', 'job.log', 'logs.txt'}:
                return True
            if entry.suffix.lower() in {'.log', '.json', '.txt'}:
                return True
        return False
    except Exception:
        return False

def _is_safe_song_job_dir(job_dir: Path) -> bool:
    try:
        if not job_dir or not Path(job_dir).exists() or not Path(job_dir).is_dir():
            return False
        name = str(Path(job_dir).name or '')
        if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', name):
            return True
        if re.match(r'^[0-9a-fA-F]{6}', name):
            return True
        return False
    except Exception:
        return False

def _write_track_sidecar(track: Optional[Track]) -> None:
    try:
        if not track or not getattr(track, 'audio_path', None):
            return
        audio_path = Path(str(track.audio_path)).resolve()
        if not audio_path.exists() or not audio_path.is_file():
            return
        job_dir = audio_path.parent
        existing = _load_sidecar_json(job_dir)
        existing_vote_count, existing_voters = _extract_vote_info(existing)
        if existing_vote_count and not getattr(track, 'vote_count', 0):
            track.vote_count = existing_vote_count
        payload = _track_sidecar_payload(track)
        payload['vote_count'] = max(int(getattr(track, 'vote_count', 0) or 0), existing_vote_count)
        payload['vote_voters'] = existing_voters
        sidecar = job_dir / ACERADIO_TRACK_META_FILENAME
        sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        logger.debug('AceRadio: failed to write track sidecar for %s', getattr(track, 'audio_path', None), exc_info=True)

def _backfill_job_metadata(track: Optional[Track]) -> None:
    try:
        if not track or not getattr(track, 'audio_path', None):
            return
        audio_path = Path(str(track.audio_path)).resolve()
        if not audio_path.exists() or not audio_path.is_file():
            return
        meta_path = audio_path.parent / 'metadata.json'
        if not meta_path.exists() or not meta_path.is_file():
            return
        raw = json.loads(meta_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return
        request = dict(raw.get('request') or {})
        prompt = dict(getattr(track, 'prompt', {}) or {})
        genre = str(getattr(track, 'genre', '') or prompt.get('genre') or prompt.get('style') or '')
        theme = str(getattr(track, 'theme', '') or prompt.get('theme') or '')
        song_title = _sanitize_title(str(getattr(track, 'song_title', '') or ''))
        lyrics = str(getattr(track, 'lyrics', '') or '')
        changed = False
        if genre and not str(request.get('genre') or '').strip():
            request['genre'] = genre
            changed = True
        style = str(prompt.get('style') or '')
        if style and not str(request.get('style') or '').strip():
            request['style'] = style
            changed = True
        if theme and not str(request.get('theme') or '').strip():
            request['theme'] = theme
            changed = True
        if song_title and not str(request.get('song_title') or '').strip():
            request['song_title'] = song_title
            changed = True
        if song_title and not str(request.get('title') or '').strip():
            request['title'] = song_title
            changed = True
        if lyrics and not str(request.get('lyrics') or '').strip():
            request['lyrics'] = lyrics
            changed = True
        if not changed:
            return
        raw['request'] = request
        meta_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        logger.debug('AceRadio: failed to backfill metadata for %s', getattr(track, 'audio_path', None), exc_info=True)

class EngineClient:
    def __init__(self, app:FastAPI):
        self.app=app; self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://engine.local', timeout=120.0, headers={'X-AceRadio-Internal': '1'}); self._started=False; self._lock=asyncio.Lock()
    async def ensure_started(self):
        if self._started: return
        async with self._lock:
            if self._started: return
            await self.app.router.startup(); self._started=True
    async def close(self):
        try:
            if self._started: await self.app.router.shutdown()
        finally:
            await self.client.aclose()
    async def get_json(self, path:str):
        await self.ensure_started(); return (await self.client.get(path)).json()
    async def post_json(self, path:str, payload:dict[str,Any]):
        await self.ensure_started(); r=await self.client.post(path, json=payload)
        if r.status_code>=400: raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    async def get_bytes(self, path:str):
        await self.ensure_started(); r=await self.client.get(path)
        if r.status_code>=400: raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.content, r.headers.get('content-type','audio/mpeg')

async def _probe_audio_duration(audio_path: str) -> Optional[float]:
    if not audio_path:
        return None
    try:
        p = Path(audio_path)
        if not p.exists() or not p.is_file():
            return None
        proc = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
        val = (stdout or b'').decode('utf-8', errors='replace').strip()
        if val and val not in {'N/A', ''}:
            result = float(val)
            if result > 0:
                return result
    except Exception:
        pass
    return None

def _probe_audio_duration_sync(audio_path: str) -> Optional[float]:
    if not audio_path:
        return None
    try:
        import subprocess as _sp
        p = Path(audio_path)
        if not p.exists() or not p.is_file():
            return None
        out = _sp.check_output(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             str(p)],
            stderr=_sp.DEVNULL,
            timeout=6,
        ).decode('utf-8', errors='replace').strip()
        if out and out not in {'N/A', ''}:
            val = float(out)
            if val > 0:
                return val
    except Exception:
        pass
    return None

class RadioManager:
    def __init__(self, engine:EngineClient, dj:OllamaDJ):
        self.engine=engine; self.dj=dj; self.running=False; self.config=RadioStartRequest(); self.current_track:Optional[Track]=None; self.next_track:Optional[Track]=None; self.reservoir:list[Track]=[]; self.prompt_history:list[str]=[]; self.recently_played:list[Track]=[]; self.last_error=''; self.player_started_at=0.0; self._supervisor=None; self._refill=None; self._gen_lock=asyncio.Lock(); self._lora_catalog=[]; self._language_cycle:list[str]=[]; self.archived_tracks:list[Track]=[]; self.song_library=FileSongLibrary(SONGS_PATH, OUTPUTS_ROOT); self.backend_playback=True
        self.current_playback_rate: float = 1.0
        self._last_refill_reason: str = 'idle'
        self._last_generation_action: str = 'idle'
        self._generation_in_progress: bool = False
        self._ops_events: deque[dict[str, Any]] = deque(maxlen=120)
        self.outputs_cache=OutputsCache(OUTPUTS_ROOT)
        self._playout_status_provider = None
        self._playout_controller = None
        self._advancing: bool = False
        self._last_advanced_from_id: str = ''
        self._track_promoted_at: float = 0.0
        self._track_started_confirmed: bool = False
        self._separator_transition_pending: bool = False
        self.songs_since_overlay: int = 0
        self.songs_since_separator: int = 0
        self._jingle_event: Optional[dict] = None
        self._queued_separator: Optional[dict] = None
        self.jingle_mgr: Optional[Any] = None
    async def start(self, cfg:RadioStartRequest):
        self.config=cfg; self.running=True; self.current_track=None; self.next_track=None; self.reservoir=[]; self.prompt_history=[]; self.recently_played=[]; self.last_error=''; self.player_started_at=0.0; self._track_promoted_at=0.0; self._separator_transition_pending=False; self._jingle_event=None; self._queued_separator=None; self._track_started_confirmed=False; self._language_cycle=[]; self.archived_tracks=[]; self._lora_catalog=await self.engine.get_json('/api/lora_catalog'); self._last_refill_reason='startup'; self._last_generation_action='startup'; self._generation_in_progress=False; self._ops_events.clear(); self._push_event('info', 'Radio started', f'speed {int(round(self.current_playback_rate * 100))}%')
        self._sync_playout_tracks()
        try:
            cached = await self.outputs_cache.scan(force=True)
            if cached:
                logger.info('RadioManager: found %d cached tracks in outputs folder', cached)
        except Exception:
            logger.exception('RadioManager: outputs cache scan failed')
        await self._stop_tasks(); self._supervisor=asyncio.create_task(self._supervise())
    async def stop(self):
        self.running=False; self.current_track=None; self.next_track=None; self.reservoir=[]; self.archived_tracks=[]; self.recently_played=[]; self.player_started_at=0.0; self._separator_transition_pending=False; self._jingle_event=None; self._queued_separator=None; self._track_started_confirmed=False; self._last_refill_reason='stopped'; self._last_generation_action='stopped'; self._generation_in_progress=False; self._push_event('info', 'Radio stopped', f'speed {int(round(self.current_playback_rate * 100))}% remains active')
        self._sync_playout_tracks()
        await self._stop_tasks()
        self.outputs_cache._loaded_dirs = {
            d for d in self.outputs_cache._loaded_dirs if Path(d).exists()
        }
    async def _stop_tasks(self):
        for t in (self._supervisor,self._refill):
            if t and not t.done(): t.cancel()
            if t:
                with contextlib.suppress(asyncio.CancelledError): await t
        self._supervisor=None; self._refill=None
    def attach_playout_status_provider(self, provider) -> None:
        self._playout_status_provider = provider
    def attach_playout_controller(self, controller) -> None:
        self._playout_controller = controller
    def detach_playout_controller(self) -> None:
        self._playout_controller = None
    def _sync_playout_tracks(self) -> None:
        controller = self._playout_controller
        if controller is None:
            return
        with contextlib.suppress(Exception):
            controller.sync_radio_state(self)
    def _notify_playout_jingle_event(self, event: Optional[dict]) -> None:
        controller = self._playout_controller
        if controller is None or not event:
            return
        with contextlib.suppress(Exception):
            controller.play_jingle_event(event)
    def _clear_future_prepared_tracks(self, *, reason: str = '') -> None:
        detail = str(reason or '').strip() or 'prepared queue reset'
        if self.next_track is not None:
            self.next_track = None
        if self.reservoir:
            self.reservoir = []
        if getattr(self.outputs_cache, '_pool', None):
            self.outputs_cache._pool = []
        self.outputs_cache._loaded_dirs = set()
        self.outputs_cache._pending_dirs = {}
        self.outputs_cache._invalid_dirs = {}
        self._sync_playout_tracks()
        self._last_generation_action = detail
        self._push_event('info', 'Prepared queue cleared', detail)


    def _push_event(self, level: str, title: str, detail: str = '') -> None:
        stamp = time.time()
        self._ops_events.append({
            'ts': round(stamp, 3),
            'level': str(level or 'info'),
            'title': str(title or '').strip(),
            'detail': str(detail or '').strip(),
        })
    async def playout_jingle_started(self, event: dict[str, Any]) -> bool:
        current = self._jingle_event
        event_id = str((event or {}).get('event_id', '') or '')
        if not current or str(current.get('event_id', '') or '') != event_id:
            return False
        if not current.get('confirmed'):
            current['confirmed'] = True
            current['confirmed_at'] = float((event or {}).get('started_at') or time.time())
            if self.jingle_mgr is not None:
                with contextlib.suppress(Exception):
                    self.jingle_mgr.record_played(str(current.get('filename', '') or ''), str(current.get('mode', '') or ''))
            self._push_event('info', 'Jingle started', f"{str(current.get('mode', '') or '').upper()} · {str(current.get('filename', '') or '')}")
        return True
    async def playout_jingle_ended(self, event: dict[str, Any]) -> bool:
        current = self._jingle_event
        event_id = str((event or {}).get('event_id', '') or '')
        if current and str(current.get('event_id', '') or '') == event_id:
            current['status'] = 'ended'
            current['ended_at'] = float((event or {}).get('ended_at') or time.time())
        if current and str(current.get('event_id', '') or '') == event_id:
            self._push_event('info', 'Jingle ended', f"{str(current.get('mode', '') or '').upper()} · {str(current.get('filename', '') or '')}")
        if bool((event or {}).get('is_transition', False)) and self._separator_transition_pending:
            self._separator_transition_pending = False
            await self._advance_rotation(from_track_id=str((event or {}).get('track_id', '') or ''))
            return True
        return bool(current and str(current.get('event_id', '') or '') == event_id)
    def _playout_status(self) -> dict[str, Any]:
        provider = self._playout_status_provider
        if provider is None:
            return {}
        try:
            data = provider() or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    def _prepared_count(self)->int:
        return (1 if self.next_track else 0) + len(self.reservoir)
    def _ensure_refill(self):
        _rt=getattr(self.config,'reservoir_target',RESERVOIR_TARGET); _rft=getattr(self.config,'refill_threshold',RESERVOIR_REFILL_THRESHOLD)
        should_refill = bool(self.running and (self._prepared_count()<_rt or self._prepared_count()<=_rft))
        if should_refill:
            self._last_refill_reason = f'prepared {self._prepared_count()} <= threshold {int(_rft)} / target {int(_rt)}'
            with contextlib.suppress(Exception):
                asyncio.create_task(self.outputs_cache.scan())
        if should_refill and (self._refill is None or self._refill.done()):
            cooldown = getattr(self, '_refill_cooldown_until', 0.0)
            if time.time() < cooldown:
                return
            self._push_event('info', 'Reservoir refill requested', self._last_refill_reason)
            self._refill=asyncio.create_task(self._refill_loop())
    def _pick_separator_candidate(self, *, allow_round_robin_fallback: bool = False) -> Optional[dict[str, Any]]:
        jmgr = self.jingle_mgr
        if jmgr is None:
            return None
        sep = jmgr.pick_eligible('separator')
        if sep or not allow_round_robin_fallback:
            return sep
        enabled_separators = [j for j in jmgr.all_jingles() if j.get('mode') == 'separator' and j.get('enabled', True)]
        if not enabled_separators:
            return None
        rr = int(getattr(jmgr, '_rr_index', {}).get('separator', 0) or 0)
        sep = dict(enabled_separators[rr % len(enabled_separators)])
        jmgr._rr_index['separator'] = (rr + 1) % max(1, len(enabled_separators))
        return sep
    def _arm_separator_for_imminent_transition(self, *, remaining: Optional[float] = None, reason: str = 'transition') -> bool:
        if self._separator_transition_pending or self._queued_separator or (self._jingle_event and self._jingle_event.get('status') == 'active'):
            return False
        cfg = self.config or RadioStartRequest()
        sep_start_before_end = _resolve_separator_start_before_end_s(getattr(cfg, 'jingle_separator_arm_offset_s', 0.0))
        sep = self._pick_separator_candidate(allow_round_robin_fallback=bool(sep_start_before_end > 0.0))
        if not sep:
            return False
        self._queued_separator = sep
        logger.info('[AceRadio] separator armed for imminent transition via %s: %s (remaining=%s)', reason, sep['filename'], 'n/a' if remaining is None else f'{remaining:.2f}s')
        return True
    def _track_seen_recently(self, track: Optional[Track]) -> bool:
        if _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated')) == 'cache':
            return False
        if not track:
            return False
        current_id = self.current_track.id if self.current_track else ''
        next_id = self.next_track.id if self.next_track else ''
        recent_ids = {x.id for x in self.recently_played[-RECENTLY_PLAYED_LIMIT:] if x}
        recent_titles = {str(x.song_title or '').strip().lower() for x in self.recently_played[-RECENTLY_PLAYED_LIMIT:] if x and str(x.song_title or '').strip()}
        title = str(track.song_title or '').strip().lower()
        return bool(track.id and track.id in ({current_id, next_id} | recent_ids)) or bool(title and title in recent_titles)

    def _promote_reservoir_to_next(self):
        if self.next_track is not None:
            return
        is_cache = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated')) == 'cache'
        attempts = len(self.reservoir)
        while self.reservoir and attempts > 0:
            attempts -= 1
            candidate=self.reservoir.pop(0)
            if is_cache or not self._track_seen_recently(candidate):
                self.next_track=candidate
                self._last_generation_action = f'promoted prepared track {candidate.song_title} into next slot'
                self._push_event('info', 'Next track prepared', candidate.song_title)
                self._sync_playout_tracks()
                return
            if str(getattr(candidate, 'source', '') or '') == 'cache':
                self.reservoir.append(candidate)
                self._last_generation_action = f'recycled cached track {candidate.song_title} back to reservoir'
    def _promote_next_to_current(self):
        self._promote_reservoir_to_next()
        if self.current_track is None and self.next_track is not None:
            self.current_track=self.next_track
            self.next_track=None
            self._track_promoted_at = time.time()
            self._push_event('info', 'Track on air', str(getattr(self.current_track, 'song_title', '') or ''))
            self.player_started_at = 0.0
            self._track_started_confirmed = False
            self._promote_reservoir_to_next()
            if self.current_track and self.current_track.real_duration is None:
                asyncio.create_task(self._probe_current_duration())
            self._sync_playout_tracks()
    async def _probe_current_duration(self) -> None:
        track = self.current_track
        if not track or not track.audio_path:
            return
        real = await _probe_audio_duration(track.audio_path)
        if real and real > 0 and track is self.current_track:
            track.real_duration = real
            logger.debug(
                'AceRadio: ffprobe duration for "%s": %.1fs (declared %ds, diff %.1fs)',
                track.song_title, real, track.duration, real - track.duration,
            )
    def _start_backend_playback_clock(self):
        self.player_started_at=time.time() if self.current_track else 0.0
    def _current_track_elapsed(self) -> float:
        if not self.current_track:
            return 0.0
        snap = self._playout_status()
        current_id = str(getattr(self.current_track, 'id', '') or '')
        snap_track_id = str(snap.get('current_track_id') or '')
        try:
            snap_elapsed = max(0.0, float(snap.get('track_elapsed') or 0.0))
        except Exception:
            snap_elapsed = 0.0
        if current_id and current_id == snap_track_id:
            if snap.get('playback_authoritative'):
                return snap_elapsed
            if snap.get('stale') or snap.get('child_alive') is False or snap.get('snapshot_fresh') is False:
                return snap_elapsed
            if snap.get('running'):
                return snap_elapsed
        if not self.player_started_at:
            return 0.0
        return max(0.0, time.time()-self.player_started_at) * _normalize_playback_rate(self.current_playback_rate)
    def _has_authoritative_playout(self) -> bool:
        if not self.current_track:
            return False
        snap = self._playout_status()
        return bool(snap.get('playback_authoritative') and str(snap.get('current_track_id') or '') == str(getattr(self.current_track, 'id', '') or ''))
    def _current_track_duration(self) -> float:
        try:
            real = getattr(self.current_track, 'real_duration', None)
            if real and real > 0:
                return float(real)
            return max(0.0, float(getattr(self.current_track, 'duration', 0) or 0))
        except Exception:
            return 0.0
    def _auto_transition_cut_seconds(self) -> float:
        try:
            value = getattr(self.config, 'auto_transition_cut_seconds', 0)
        except Exception:
            value = 0
        try:
            return max(0.0, float(value or 0.0))
        except Exception:
            return 0.0
    def _effective_transition_target_elapsed(self, duration: Optional[float] = None) -> float:
        if duration is None:
            total = self._current_track_duration()
        else:
            try:
                total = max(0.0, float(duration or 0.0))
            except Exception:
                total = 0.0
        if total <= 0.0:
            return 0.0
        transition_cut = self._auto_transition_cut_seconds()
        if transition_cut > 0.0:
            return max(0.0, min(total, transition_cut))
        return total
    def _remaining_to_transition_seconds(self, elapsed: Optional[float] = None, duration: Optional[float] = None) -> float:
        if elapsed is None:
            current_elapsed = self._current_track_elapsed()
        else:
            try:
                current_elapsed = max(0.0, float(elapsed or 0.0))
            except Exception:
                current_elapsed = 0.0
        target = self._effective_transition_target_elapsed(duration)
        if target <= 0.0:
            return 0.0
        return max(0.0, target - current_elapsed)
    def _current_track_finished(self) -> bool:
        if not self.current_track or not self.player_started_at:
            return False
        if self._separator_transition_pending:
            return False
        elapsed = self._current_track_elapsed()
        total = self._current_track_duration()
        transition_cut = self._auto_transition_cut_seconds()
        transition_target = self._effective_transition_target_elapsed(total)
        if transition_cut > 0.0 and transition_target > 0.0 and elapsed >= transition_target:
            remaining = self._remaining_to_transition_seconds(elapsed, total)
            if self._arm_separator_for_imminent_transition(remaining=remaining, reason='auto-transition-cut'):
                self._push_event('warn', 'Automatic transition cut reached', f'{self.current_track.song_title} at {int(round(transition_target))}s')
            return True
        real = self.current_track.real_duration
        if real and real > 0:
            return elapsed >= real + 2.0
        declared = total
        if declared <= 0:
            return False
        grace = max(4.0, declared * 0.06)
        return elapsed >= declared + grace
    def _mark_recently_played(self, track: Optional[Track]):
        if not track:
            return
        self.recently_played.append(track)
        self.recently_played=self.recently_played[-RECENTLY_PLAYED_LIMIT:]
    async def _refill_loop(self):
        try:
            _rt=getattr(self.config,'reservoir_target',RESERVOIR_TARGET)
            is_cache = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated')) == 'cache'
            self._generation_in_progress = True
            consecutive_skips = 0
            max_consecutive_skips = 5
            consecutive_generation_failures = 0
            max_generation_failures = 5
            while self.running and self._prepared_count()<_rt:
                try:
                    track=await self._generate_track()
                except asyncio.CancelledError:
                    raise
                except Exception as track_exc:
                    consecutive_generation_failures += 1
                    self.last_error=str(track_exc)
                    logger.warning('AceRadio refill: generation attempt failed (%d/%d): %s', consecutive_generation_failures, max_generation_failures, track_exc)
                    self._push_event('warn', 'Prepared track rejected, retrying', str(track_exc)[:120])
                    if consecutive_generation_failures >= max_generation_failures:
                        raise
                    await asyncio.sleep(min(4.0, 0.5 * consecutive_generation_failures))
                    continue
                consecutive_generation_failures = 0
                if not is_cache and self._track_seen_recently(track):
                    consecutive_skips += 1
                    if str(getattr(track, 'source', '') or '') == 'cache':
                        self.outputs_cache.reinsert(track)
                        self._last_generation_action = f'recycled cached track {track.song_title} after recent-track rejection'
                    else:
                        self._last_generation_action = f'skipped generated track {track.song_title} because it was recently seen'
                    if consecutive_skips >= max_consecutive_skips:
                        logger.warning('AceRadio refill: %d consecutive skips (duplicates), accepting next track regardless', consecutive_skips)
                        consecutive_skips = 0
                    else:
                        continue
                consecutive_skips = 0
                if self.next_track is None:
                    self.next_track=track
                    self._last_generation_action = f'promoted prepared track {track.song_title} into next slot'
                    self._sync_playout_tracks()
                else:
                    self.reservoir.append(track)
                    self._last_generation_action = f'appended prepared track {track.song_title} to reservoir'
                await asyncio.sleep(0)
            self._last_refill_reason = f'refill complete at {self._prepared_count()} prepared / target {int(_rt)}'
            self._push_event('info', 'Reservoir refill complete', self._last_refill_reason)
            self._refill_cooldown_until = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error=str(e); logger.exception('AceRadio refill failed')
            self._refill_cooldown_until = time.time() + 20.0
            self._push_event('warn', 'Refill failed, cooldown 20s', str(e)[:120])
        finally:
            self._generation_in_progress = False
    def _pick_language(self)->str:
        langs=[str(x).strip() for x in self.config.languages if str(x).strip()] or ['en']
        mode=(self.config.language_rotation_mode or 'round_robin').strip().lower()
        if len(langs) == 1 or mode == 'random':
            return random.choice(langs)
        valid=[x for x in self._language_cycle if x in langs]
        if not valid:
            valid=list(langs)
            random.shuffle(valid)
        chosen=valid.pop(0)
        self._language_cycle=valid
        return chosen
    def _pick_duration(self, automatic_duration: bool = False)->int:
        lo = _coerce_duration_value(getattr(self.config, 'min_duration', None), 60)
        hi = max(lo, _coerce_duration_value(getattr(self.config, 'max_duration', None), lo))
        if automatic_duration:
            return 180
        return random.randint(lo, hi)
    def _pick_lora(self):
        items=self._lora_catalog if isinstance(self._lora_catalog,list) else []
        cat={str(x.get('id') or '').strip():x for x in items if isinstance(x,dict) and str(x.get('id') or '').strip()}
        enabled=[x for x in self.config.selected_loras if x.enabled and x.id in cat]
        if not enabled: return '','',0.0
        chance=max(0,min(100,int(getattr(self.config, 'lora_use_probability', 100) or 0)))
        if random.randint(1,100) > chance:
            return '','',0.0
        chosen=random.choice(enabled); meta=cat[chosen.id]; return chosen.id, str(meta.get('trigger') or '').strip(), max(0.0,min(float(chosen.weight),2.0))
    def _duration_caption_hint(self, duration: int, instrumental: bool) -> str:
        minutes = max(2, min(6, int(round(max(120, int(duration or 180)) / 60.0))))
        if minutes == 3:
            return 'full-length radio instrumental around three minutes' if instrumental else 'full-length radio song around three minutes'
        return f"full-length radio {'instrumental' if instrumental else 'song'} around {minutes} minutes"
    async def _generate_prompt_resilient(self, source: str, duration: int, language: str, instrumental: bool, automatic_duration: bool, canonical_genre: str) -> tuple[SongPrompt, str]:
        if _custom_catalog_enabled(self.config):
            return self.song_library.choose_custom(self.prompt_history), 'file'
        requested_source = _normalize_track_source_key(source or 'ai_generated')
        source_mode = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated'))
        allow_file_fallback = source_mode == 'both'

        if instrumental:
            theme = _pick_theme_candidate(getattr(self.config, 'themes', []), set())
            return _build_local_instrumental_prompt(canonical_genre, theme, duration, language), 'ai_generated'

        def _choose_file_prompt(theme: str) -> SongPrompt:
            return self.song_library.choose(canonical_genre, theme, self.prompt_history, duration, language, instrumental, getattr(self.config, 'catalog_source', 'library'))

        async def _generate_ollama_prompt() -> SongPrompt:
            used_themes: set[str] = set()
            last_error: Optional[Exception] = None
            relaxed_prompt: Optional[SongPrompt] = None
            relaxed_issues: list[str] = []
            best_theme_prompt: Optional[SongPrompt] = None
            best_theme_issues: list[str] = []
            best_theme_rank: tuple[int, int, int] = (-1, -1, -1)
            await self._offload_engine_music_runtime('ollama_prompt')
            try:
                for _ in range(max(1, OLLAMA_CONTENT_RETRIES)):
                    theme = _pick_theme_candidate(getattr(self.config, 'themes', []), used_themes)
                    used_themes.add(theme)
                    try:
                        prompt = await self.dj.generate(
                            canonical_genre,
                            theme,
                            self.config.station_prompt,
                            self.prompt_history,
                            duration,
                            language,
                            instrumental,
                            automatic_duration=automatic_duration,
                        )
                        if not str(getattr(prompt, 'genre', '') or '').strip():
                            prompt.genre = str(canonical_genre or '').strip()
                        if not str(getattr(prompt, 'theme', '') or '').strip():
                            prompt.theme = str(theme or '').strip()
                        issues = _prompt_quality_issues(prompt, instrumental, canonical_genre, theme, language, 'ai_generated')
                        fatal_issues, warning_issues = _split_prompt_quality_issues(issues)
                        non_theme_fatals = [issue for issue in fatal_issues if not _is_theme_quality_issue(issue)]
                        if not fatal_issues and not warning_issues:
                            return prompt
                        if not non_theme_fatals:
                            if fatal_issues or warning_issues:
                                logger.warning('AceRadio accepting first Ollama prompt with theme-only issues: %s', ', '.join(fatal_issues + warning_issues))
                            return prompt
                        last_error = RuntimeError('Ollama DJ returned weak prompt (' + ', '.join(fatal_issues + warning_issues) + ')')
                    except Exception as exc:
                        last_error = exc
                if relaxed_prompt is not None:
                    logger.warning('AceRadio accepting relaxed Ollama prompt after retries: %s', ', '.join(relaxed_issues))
                    return relaxed_prompt
                if best_theme_prompt is not None:
                    logger.warning('AceRadio accepting best available Ollama prompt after retries: %s', ', '.join(best_theme_issues))
                    return best_theme_prompt
                raise RuntimeError(f'Ollama DJ failed after theme retries: {last_error}')
            finally:
                await self._ensure_engine_music_runtime_loaded(str(self.config.model or ''))

        if requested_source == 'ai_generated':
            try:
                prompt = await _generate_ollama_prompt()
                return prompt, 'ai_generated'
            except Exception:
                if allow_file_fallback:
                    fallback_theme = _pick_theme_candidate(getattr(self.config, 'themes', []), set())
                    prompt = _choose_file_prompt(fallback_theme)
                    return prompt, 'file'
                raise
        if requested_source == 'file':
            fallback_theme = _pick_theme_candidate(getattr(self.config, 'themes', []), set())
            try:
                prompt = _choose_file_prompt(fallback_theme)
                return prompt, 'file'
            except Exception:
                if source_mode == 'both':
                    logger.warning('AceRadio selected local catalog unavailable; falling back to Ollama')
                    prompt = await _generate_ollama_prompt()
                    return prompt, 'ai_generated'
                raise
        logger.warning('AceRadio file prompt library unavailable; falling back to Ollama')
        prompt = await _generate_ollama_prompt()
        return prompt, 'ai_generated'

    async def _offload_engine_music_runtime(self, reason: str) -> None:
        try:
            result = await self.engine.post_json('/api/runtime/music_model/offload', {'reason': str(reason or '').strip()})
            if bool(result.get('changed')):
                logger.info('AceRadio offloaded music runtime before Ollama (%s)', str(reason or '').strip() or 'request')
        except Exception as exc:
            logger.warning('AceRadio failed to offload music runtime before Ollama: %r', exc)

    async def _ensure_engine_music_runtime_loaded(self, model_name: str = '') -> None:
        payload = {'model': str(model_name or self.config.model or '').strip()}
        result = await self.engine.post_json('/api/runtime/music_model/ensure_loaded', payload)
        if not bool(result.get('loaded')):
            raise RuntimeError('AceRadio music runtime failed to load')

    def _pick_generation_source(self) -> str:
        mode = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated'))
        if mode == 'both':
            file_pct = max(0, min(100, int(getattr(self.config, 'generation_source_both_percent', 50) or 0)))
            return 'file' if random.randint(1, 100) <= file_pct else 'ai_generated'
        return 'file' if mode == 'file' else 'ai_generated'
    async def _generate_track(self)->Track:
        async with self._gen_lock:
            active_job_ids = {str(getattr(x, 'job_id', '') or '') for x in ([self.current_track, self.next_track] + list(self.reservoir) + list(self.archived_tracks)) if x}
            source_mode = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated'))
            if self.outputs_cache.peek_count() <= 0:
                await self.outputs_cache.scan()

            if source_mode == 'cache':
                track = self.outputs_cache.pop_rotate(active_job_ids)
                if track:
                    logger.info('RadioManager [cache mode]: rotating track "%s" (%s)', track.song_title, track.job_id)
                    self.prompt_history.append(track.song_title)
                    self.prompt_history = self.prompt_history[-max(1, self.config.keep_history):]
                    return track
                logger.info('RadioManager [cache mode]: pool empty, rescanning outputs folder…')
                await self.outputs_cache.scan()
                active_job_ids = {str(getattr(x, 'job_id', '') or '') for x in ([self.current_track, self.next_track] + list(self.reservoir) + list(self.archived_tracks)) if x}
                track = self.outputs_cache.pop_rotate(active_job_ids)
                if track:
                    logger.info('RadioManager [cache mode]: rotating track "%s" after rescan', track.song_title)
                    self.prompt_history.append(track.song_title)
                    self.prompt_history = self.prompt_history[-max(1, self.config.keep_history):]
                    return track
                track = self.outputs_cache.pop_rotate(set())
                if track:
                    logger.warning('RadioManager [cache mode]: only excluded tracks available, relaxing exclusion for "%s"', track.song_title)
                    self.prompt_history.append(track.song_title)
                    self.prompt_history = self.prompt_history[-max(1, self.config.keep_history):]
                    return track
                raise RuntimeError('Cache-only mode: no cached tracks found in aceradio_outputs/. Add tracks and click Rebuild cache.')

            cached = self.outputs_cache.pop(active_job_ids)
            if cached:
                logger.info('RadioManager: using cached track "%s" (%s)', cached.song_title, cached.job_id)
                self.prompt_history.append(cached.song_title)
                self.prompt_history = self.prompt_history[-max(1, self.config.keep_history):]
                return cached

            language=self._pick_language(); instrumental=random.randint(1,100)<=max(0,min(100,int(self.config.instrumental_probability))); source=self._pick_generation_source(); automatic_duration=bool(getattr(self.config, 'automatic_duration', False)); duration=self._pick_duration(automatic_duration); canonical_genre=_pick_canonical_genre(getattr(self.config, 'genres', []))
            prompt, source = await self._generate_prompt_resilient(source, duration, language, instrumental, automatic_duration, canonical_genre)
            if str(getattr(prompt, 'catalog_source', '') or '').strip().lower() == 'custom':
                custom_lang = str(getattr(prompt, 'vocal_language', '') or '').strip().lower()
                custom_instrumental = bool(getattr(prompt, 'vocal_style', '') == 'instrumental') or _lyrics_indicates_instrumental(getattr(prompt, 'lyrics', ''))
                if custom_lang in VALID_LANGUAGES:
                    language = custom_lang
                elif custom_instrumental:
                    language = 'unknown'
                instrumental = bool(instrumental or custom_instrumental)
            source_mode = _normalize_track_source_key(getattr(self.config, 'generation_source', 'ai_generated'))
            if source_mode in {'file', 'both'} and not _lyrics_have_content(prompt.lyrics, instrumental):
                with contextlib.suppress(Exception):
                    prompt=_merge_backup_prompt(prompt, self.song_library.choose(canonical_genre, _pick_theme_candidate(getattr(self.config, 'themes', []), set()), self.prompt_history, duration, language, instrumental), instrumental=instrumental)
            generation_duration = _resolve_audio_generation_duration(prompt, duration, automatic_duration, instrumental)
            prompt.duration = int(generation_duration)
            prompt=_finalize_prompt(prompt, generation_duration, language, instrumental, source)
            opts=await self.engine.get_json('/api/options'); defaults=dict(opts.get('defaults') or {})
            lora_id,lora_trigger,lora_weight=self._pick_lora(); model=str(self.config.model or opts.get('current_model') or 'acestep-v15-turbo').strip(); custom_catalog_track = str(getattr(prompt, 'catalog_source', '') or '').strip().lower() == 'custom'; station_caption='' if instrumental or custom_catalog_track else self.config.station_prompt.strip(); auto_duration_caption=self._duration_caption_hint(generation_duration, instrumental) if automatic_duration else ''; display_genre=str(prompt.genre or prompt.style or '').strip(); explicit_prompt_caption=_strip_caption_prefix(getattr(prompt, 'caption', '') or '', prompt.song_title, display_genre); rich_style_caption=_strip_caption_prefix(getattr(prompt, 'style', '') or '', prompt.song_title, display_genre); fallback_caption=' | '.join(x for x in [prompt.song_title,display_genre,station_caption,auto_duration_caption] if x); caption=explicit_prompt_caption or rich_style_caption or fallback_caption
            lm_enabled = bool(self.config.thinking) and not instrumental
            audio_format=str(getattr(self.config,'audio_format',PLAYER_AUDIO_FORMAT) or PLAYER_AUDIO_FORMAT).strip().lower()
            mp3_bitrate=str(getattr(self.config, 'mp3_bitrate', ACERADIO_MP3_DEFAULT_BITRATE) or ACERADIO_MP3_DEFAULT_BITRATE).strip().lower()
            if mp3_bitrate not in ACERADIO_MP3_BITRATE_OPTIONS:
                mp3_bitrate = ACERADIO_MP3_DEFAULT_BITRATE
            try:
                mp3_sample_rate=int(getattr(self.config, 'mp3_sample_rate', ACERADIO_MP3_DEFAULT_SAMPLE_RATE) or ACERADIO_MP3_DEFAULT_SAMPLE_RATE)
            except Exception:
                mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
            if mp3_sample_rate not in ACERADIO_MP3_SAMPLE_RATE_OPTIONS:
                mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
            resolved_shift=_resolve_shift_for_model(model, getattr(self.config, 'shift', 3.0))
            resolved_steps=_resolve_inference_steps_for_model(model, getattr(self.config, 'inference_steps', 8))
            payload={'model':model,'generation_mode':'Custom','task_type':'text2music','caption':caption,'song_title':str(prompt.song_title or ''),'title':str(prompt.song_title or ''),'lyrics':prompt.lyrics,'genre':str(prompt.genre or prompt.style or ''),'style':str(prompt.style or prompt.genre or ''),'theme':prompt.theme,'instrumental':instrumental,'thinking':lm_enabled,'duration':int(generation_duration),'duration_auto':bool(automatic_duration),'seed':-1,'lora_id':lora_id or None,'lora_trigger':lora_trigger or None,'lora_weight':lora_weight,'batch_size':1,'audio_format':audio_format,'mp3_bitrate':mp3_bitrate if audio_format == 'mp3' else None,'mp3_sample_rate':mp3_sample_rate if audio_format == 'mp3' else None,'use_adg':bool(getattr(self.config,'use_adg',False)),'inference_steps':int(resolved_steps),'infer_method':str(self.config.infer_method or defaults.get('infer_method') or 'ode'),'guidance_scale':float(self.config.guidance_scale),'shift':float(resolved_shift),'cfg_interval_start':float(self.config.cfg_interval_start),'cfg_interval_end':float(self.config.cfg_interval_end),'enable_normalization':bool(self.config.enable_normalization),'normalization_db':float(self.config.normalization_db),'score_scale':float(getattr(self.config,'score_scale',0.5) or 0.5),'auto_score':bool(getattr(self.config,'auto_score',False)) and not instrumental,'latent_shift':float(self.config.latent_shift),'latent_rescale':float(self.config.latent_rescale),'timesteps':self.config.timesteps,'bpm':prompt.bpm,'bpm_auto':False,'keyscale':prompt.key_scale,'key_auto':False,'timesignature':str(getattr(prompt,'timesignature','') or '4/4'),'timesig_auto':False,'vocal_language':str(getattr(prompt,'vocal_language','') or ('unknown' if instrumental else language)),'language_auto':instrumental,'lm_temperature':float(getattr(self.config,'lm_temperature',0.85) or 0.85),'use_cot_metas':bool(self.config.use_cot_metas) and lm_enabled,'use_cot_caption':bool(self.config.use_cot_caption) and lm_enabled,'use_cot_language':bool(self.config.use_cot_language) and lm_enabled,'lm_cfg_scale':float(self.config.lm_cfg_scale),'lm_top_k':int(getattr(self.config,'lm_top_k',0) or 0),'lm_top_p':float(getattr(self.config,'lm_top_p',0.9) or 0.9),'lm_negative_prompt':_build_lm_negative_prompt(getattr(self.config,'station_negative_prompt',''), getattr(self.config,'lm_negative_prompt','')),'use_constrained_decoding':bool(getattr(self.config,'use_constrained_decoding',True)) and lm_enabled,'parallel_thinking':bool(getattr(self.config,'parallel_thinking',False)) and lm_enabled,'constrained_decoding_debug':bool(getattr(self.config,'constrained_decoding_debug',False)) and lm_enabled}
            logger.info('AceRadio generation export request: format=%s requested_bitrate=%s requested_rate=%s payload_bitrate=%s payload_rate=%s', audio_format, getattr(self.config, 'mp3_bitrate', None), getattr(self.config, 'mp3_sample_rate', None), payload.get('mp3_bitrate'), payload.get('mp3_sample_rate'))
            await self._ensure_engine_music_runtime_loaded(model)
            job=await self.engine.post_json('/api/jobs', payload); job_id=job['job_id']
            logger.info('AceRadio audio job submitted: %s', job_id)
            _poll_start = asyncio.get_event_loop().time()
            while True:
                info=await self.engine.get_json(f'/api/jobs/{job_id}')
                _job_status = info.get('status', 'unknown')
                if _job_status=='done':
                    logger.info('AceRadio audio job done: %s (%.0fs)', job_id, asyncio.get_event_loop().time() - _poll_start)
                    break
                if _job_status in {'failed','cancelled','error'}:
                    logger.error('AceRadio audio job %s failed: %s', job_id, info.get('error', 'unknown'))
                    raise RuntimeError(info.get('error') or f'Job {job_id} failed')
                _poll_elapsed = asyncio.get_event_loop().time() - _poll_start
                if _poll_elapsed > JOB_POLL_TOTAL_TIMEOUT:
                    logger.error('AceRadio audio job %s timed out after %.0fs (status=%s)', job_id, _poll_elapsed, _job_status)
                    raise RuntimeError(f'Job {job_id} timed out after {int(_poll_elapsed)}s (last status: {_job_status})')
                await asyncio.sleep(POLL_INTERVAL_S)
            result=(info.get('result') or {}); audio_urls=result.get('audio_urls') or []; audio_paths=result.get('audio_paths') or []
            if not audio_urls: raise RuntimeError(f'Job {job_id} produced no audio')
            audio_bytes,audio_mime=await self.engine.get_bytes(audio_urls[0]); seeds=result.get('audio_resolved_seeds') or []; seed=str(seeds[0]) if seeds else ''
            await asyncio.to_thread(_cleanup_runtime, self.config.vram_cleanup_mode)
            track=_build_track(prompt, duration=generation_duration, language=language, instrumental=instrumental, source=source, job_id=job_id, audio_bytes=audio_bytes, audio_mime=audio_mime, seed=seed, lora_id=lora_id, audio_path=str(audio_paths[0]) if audio_paths else '')
            if track.audio_path:
                _real_dur = await _probe_audio_duration(track.audio_path)
                if _real_dur and _real_dur > 0:
                    track.duration = int(round(_real_dur))
                    track.real_duration = _real_dur
                    logger.info('AceRadio: track duration from file=%ds (requested=%ds) for "%s"',
                                track.duration, duration, track.song_title)
                else:
                    try:
                        _lm_dur = int((((result.get('audios') or [{}])[0] or {}).get('lm_metadata') or {}).get('duration') or 0)
                        if _lm_dur > 0:
                            track.duration = _lm_dur
                            logger.info('AceRadio: track duration from lm_metadata=%ds (requested=%ds) for "%s"',
                                        track.duration, duration, track.song_title)
                    except Exception:
                        pass
            with contextlib.suppress(Exception):
                audio0 = ((result.get('audios') or [None])[0] or {}) if isinstance(result, dict) else {}
                export_applied = (audio0.get('export_applied') or {}) if isinstance(audio0, dict) else {}
                prompt_meta = dict(getattr(track, 'prompt', {}) or {})
                prompt_meta.update({
                    'source': source,
                    'catalog_source': _normalize_catalog_source_optional(getattr(prompt, 'catalog_source', '')) if str(source or '').strip().lower() == 'file' else '',
                    'display_source': _resolve_display_source(source, {'catalog_source': getattr(prompt, 'catalog_source', ''), 'display_source': getattr(prompt, 'display_source', '')}, instrumental),
                    'display_source_label': _display_source_label(_resolve_display_source(source, {'catalog_source': getattr(prompt, 'catalog_source', ''), 'display_source': getattr(prompt, 'display_source', '')}, instrumental)),
                    'genre': str(prompt.genre or prompt.style or ''),
                    'style': str(prompt.style or ''),
                    'theme': str(prompt.theme or ''),
                    'caption': _strip_caption_prefix(getattr(prompt, 'caption', '') or '', prompt.song_title, prompt.genre or prompt.style)
                               or _strip_caption_prefix(getattr(prompt, 'style', '') or '', prompt.song_title, prompt.genre or prompt.style)
                               or _strip_caption_prefix(payload.get('caption') or '', prompt.song_title, prompt.genre or prompt.style),
                    'timesignature': str(getattr(prompt, 'timesignature', '') or '4/4'),
                    'vocal_language': str(getattr(prompt, 'vocal_language', '') or ('unknown' if instrumental else language)),
                    'model': str(payload.get('model') or ''),
                    'audio_format': str(payload.get('audio_format') or ''),
                    'mp3_bitrate': str(export_applied.get('requested_bitrate') or audio0.get('mp3_bitrate') or payload.get('mp3_bitrate') or ''),
                    'mp3_sample_rate': int(export_applied.get('applied_sample_rate') or audio0.get('mp3_sample_rate') or payload.get('mp3_sample_rate') or 0),
                    'inference_steps': int(payload.get('inference_steps') or 0),
                    'infer_method': str(payload.get('infer_method') or ''),
                    'guidance_scale': float(payload.get('guidance_scale') or 0.0),
                    'shift': float(payload.get('shift') or 0.0),
                })
                track.prompt = prompt_meta
            _prot = {p for p in {_track_audio_job_dir(self.current_track), _track_audio_job_dir(self.next_track), _track_audio_job_dir(track), *[_track_audio_job_dir(t) for t in self.reservoir]} if p is not None}
            try:
                await asyncio.to_thread(_write_track_sidecar, track)
            except Exception as _sc_err:
                logger.warning('[AceRadio] sidecar write failed for "%s": %s', track.song_title, _sc_err)
            try:
                await asyncio.to_thread(_backfill_job_metadata, track)
            except Exception as _meta_err:
                logger.warning('[AceRadio] metadata backfill failed for "%s": %s', track.song_title, _meta_err)
            try:
                await asyncio.to_thread(_append_generated_song_history, track)
            except Exception as _hist_err:
                logger.warning('[AceRadio] generated songs history write failed for "%s": %s', track.song_title, _hist_err)
            try:
                trim_report = await asyncio.to_thread(_trim_outputs_root_by_votes, int(getattr(self.config, 'max_saved_tracks', DEFAULT_MAX_SAVED_TRACKS) or DEFAULT_MAX_SAVED_TRACKS), protected_dirs=_prot)
                if trim_report.get('removed'):
                    logger.info('AceRadio auto-trim outputs: removed=%d kept=%d protected=%d deleted_dirs=%s errors=%s', trim_report.get('removed', 0), trim_report.get('kept', 0), trim_report.get('protected', 0), trim_report.get('removed_dirs', [])[:8], trim_report.get('errors', [])[:5])
            except Exception as _tr_err:
                logger.warning('[AceRadio] outputs trim failed: %s', _tr_err)
            self.prompt_history.append(track.song_title); self.prompt_history=self.prompt_history[-max(1,self.config.keep_history):]
            return track
    def _delete_track_file(self, track: Optional[Track]):
        if not track or not track.audio_path:
            return
        return

    def _trim_saved_tracks(self):
        max_saved = int(self.config.max_saved_tracks or DEFAULT_MAX_SAVED_TRACKS)
        if max_saved <= 0:
            logger.info('AceRadio retention trim skipped: unlimited archived tracks configured')
            return
        max_saved = max(1, max_saved)
        before = len(self.archived_tracks)
        removed = 0
        removed_titles: list[str] = []
        while len(self.archived_tracks) > max_saved:
            old = self.archived_tracks.pop(0)
            removed += 1
            removed_titles.append(str(getattr(old, 'song_title', '') or getattr(old, 'job_id', '') or 'unknown'))
            self._delete_track_file(old)
        logger.info('AceRadio retention trim: archived_before=%d keep=%d removed=%d archived_after=%d removed_items=%s', before, max_saved, removed, len(self.archived_tracks), removed_titles[:8])
    def payload(self, t:Optional[Track]):
        if t is None: return None
        lora_label = ''
        if t.lora_id:
            cat = {str(x.get('id') or '').strip(): x for x in (self._lora_catalog if isinstance(self._lora_catalog, list) else [])}
            entry = cat.get(str(t.lora_id).strip())
            lora_label = str(entry.get('label') or t.lora_id) if entry else t.lora_id
        if t.audio_bytes:
            audio_size_bytes = len(t.audio_bytes)
        elif t.audio_path:
            try:
                audio_size_bytes = Path(t.audio_path).stat().st_size
            except Exception:
                audio_size_bytes = 0
        else:
            audio_size_bytes = 0
        duration_s = max(1, int(t.duration or 1))
        prompt_meta = (t.prompt or {}) if isinstance(t.prompt, dict) else {}
        export_meta = prompt_meta.get('export_applied') if isinstance(prompt_meta.get('export_applied'), dict) else {}

        def _first_nonempty(*values):
            for value in values:
                if value not in (None, '', [], {}, 0, '0'):
                    return value
            return None

        def _parse_bitrate_kbps(value: Any) -> int:
            if value in (None, '', [], {}):
                return 0
            if isinstance(value, (int, float)):
                iv = int(value)
                return iv if iv > 0 else 0
            raw = str(value).strip().lower()
            if not raw:
                return 0
            m = re.search(r'(\d+(?:[.,]\d+)?)', raw)
            if not m:
                return 0
            try:
                num = float(m.group(1).replace(',', '.'))
            except Exception:
                return 0
            if 'm' in raw and 'mb' in raw:
                num *= 1000
            return int(round(num)) if num > 0 else 0

        def _parse_sample_rate_hz(value: Any) -> int:
            if value in (None, '', [], {}):
                return 0
            if isinstance(value, (int, float)):
                iv = int(value)
                return iv if iv > 0 else 0
            raw = str(value).strip().lower()
            if not raw:
                return 0
            m = re.search(r'(\d+(?:[.,]\d+)?)', raw)
            if not m:
                return 0
            try:
                num = float(m.group(1).replace(',', '.'))
            except Exception:
                return 0
            if 'khz' in raw or re.search(r'\bk\b', raw):
                num *= 1000
            elif 0 < num < 1000:
                num *= 1000
            return int(round(num)) if num > 0 else 0

        fmt = str(t.audio_mime or '').replace('audio/', '').lower()
        fmt_clean = {'mpeg': 'mp3', 'x-wav': 'wav', 'x-flac': 'flac'}.get(fmt, fmt) or str(prompt_meta.get('audio_format') or 'mp3')

        bitrate_kbps = _parse_bitrate_kbps(_first_nonempty(
            prompt_meta.get('bitrate_kbps'),
            export_meta.get('applied_bitrate_kbps'),
            export_meta.get('before_bitrate_kbps'),
            prompt_meta.get('mp3_bitrate'),
        ))
        if bitrate_kbps <= 0 and audio_size_bytes and duration_s > 0:
            if fmt_clean in {'flac', 'wav', 'wav32'}:
                bitrate_kbps = round((audio_size_bytes * 8) / (duration_s * 1000))

        sample_rate = _parse_sample_rate_hz(_first_nonempty(
            prompt_meta.get('sample_rate_hz'),
            export_meta.get('applied_sample_rate'),
            export_meta.get('before_sample_rate'),
            prompt_meta.get('mp3_sample_rate'),
        ))
        display_source = _resolve_display_source(t.source, t.prompt or {}, bool(getattr(t, 'instrumental', False)))
        display_source_label = _display_source_label(display_source)
        _real_dur_val = getattr(t, 'real_duration', None)
        _best_duration = (int(round(_real_dur_val)) if (_real_dur_val and _real_dur_val > 0)
                          else int(t.duration or 1))
        return {
            'id': t.id, 'job_id': t.job_id, 'song_title': t.song_title, 'tags': t.tags,
            'caption': _derive_track_caption(t),
            'lyrics': t.lyrics, 'bpm': t.bpm, 'key_scale': t.key_scale,
            'genre': str(getattr(t, 'genre', '') or prompt_meta.get('genre') or prompt_meta.get('style') or ''),
            'theme': str(getattr(t, 'theme', '') or prompt_meta.get('theme') or ''),
            'duration': _best_duration,
            'real_duration': float(_real_dur_val) if (_real_dur_val and _real_dur_val > 0) else None,
            'seed': t.seed, 'audio_url': f'/api/audio/{t.id}', 'created_at': t.created_at,
            'prompt': t.prompt, 'language': t.language, 'instrumental': t.instrumental,
            'lora_id': t.lora_id, 'lora_label': lora_label,
            'source': t.source, 'display_source': display_source,
            'display_source_label': display_source_label,
            'ready': True, 'metadata_complete': True,
            'audio_mime': t.audio_mime, 'audio_format': fmt_clean,
            'audio_size_bytes': audio_size_bytes,
            'bitrate_kbps': bitrate_kbps,
            'sample_rate_hz': sample_rate,
            'vote_count': max(0, int(getattr(t, 'vote_count', 0) or 0)),
        }
    def set_current_playback_rate(self, rate: Any) -> float:
        previous = _normalize_playback_rate(self.current_playback_rate)
        elapsed = self._current_track_elapsed() if self.current_track else 0.0
        self.current_playback_rate = _normalize_playback_rate(rate)
        if self.current_track:
            if self._has_authoritative_playout():
                self._sync_playout_tracks()
            else:
                self.player_started_at = (time.time() - (elapsed / max(0.5, self.current_playback_rate))) if elapsed > 0 else time.time()
        else:
            self._sync_playout_tracks()
        self._push_event('warn' if abs(self.current_playback_rate - 1.0) > 0.001 else 'info', 'Playback speed updated', f'{int(round(previous * 100))}% → {int(round(self.current_playback_rate * 100))}%')
        self._last_generation_action = f'playback speed set to {int(round(self.current_playback_rate * 100))}%'
        return self.current_playback_rate
    async def status(self):
        opts=await self.engine.get_json('/api/options'); health=await self.engine.get_json('/api/health')
        prepared=self._prepared_count()
        state='stopped'
        if self.running and self.current_track:
            state='on_air'
        elif self.running and (self._refill and not self._refill.done()):
            state='refilling'
        elif self.running and prepared:
            state='ready'
        elif self.running:
            state='idle'
        playout=self._playout_status()
        playback_elapsed=round(self._current_track_elapsed(),2) if self.current_track else 0
        playback_rate=_normalize_playback_rate(self.current_playback_rate)
        playback_rate_percent=int(round(playback_rate*100))
        transition_cut=self._auto_transition_cut_seconds()
        separator_before_end=_resolve_separator_start_before_end_s(getattr(self.config,'jingle_separator_arm_offset_s',0.0))
        current_id=str(getattr(self.current_track,'id','') or '')
        next_id=str(getattr(self.next_track,'id','') or '')
        playout_track_id=str(playout.get('current_track_id') or '')
        playout_fresh_for_current=bool(current_id and current_id==playout_track_id and not playout.get('stale') and playout.get('child_alive') is not False and playout.get('snapshot_fresh') is not False)
        remaining_to_cut_seconds=None
        if self.current_track and transition_cut>0:
            remaining_to_cut_seconds=self._remaining_to_transition_seconds()
        runtime_active=bool(self.running and self.current_track)
        playout_child_active=bool(playout.get('running'))
        playout_authoritative=bool(playout.get('playback_authoritative'))
        child_alive=playout.get('child_alive') is not False if playout else False
        snapshot_fresh=playout.get('snapshot_fresh') is not False if playout else False
        stale=bool(playout.get('stale'))
        last_error=str(playout.get('last_error') or self.last_error or '')
        fallback_mode=bool(runtime_active and not playout_authoritative)
        healthy=bool((playout_child_active or fallback_mode) and not stale and child_alive and snapshot_fresh and not last_error)
        degraded=bool(playout and (stale or not child_alive or not snapshot_fresh or last_error))
        authority_source='playout' if playout_authoritative else ('runtime' if runtime_active else 'idle')
        backend_health={'runtime_active':runtime_active,'playout_active':bool(playout_child_active or runtime_active),'playout_child_active':playout_child_active,'playout_authoritative':playout_authoritative,'authority_source':authority_source,'radio_on_air':runtime_active,'current_track_loaded':bool(self.current_track),'child_alive':bool(child_alive),'healthy':healthy,'degraded':degraded,'fallback_mode':fallback_mode,'snapshot_fresh':bool(snapshot_fresh),'stale':stale,'stale_reason':str(playout.get('stale_reason') or ''),'last_error':last_error}
        return {'running':self.running,'radio_state':state,'model':self.config.model or opts.get('current_model') or health.get('model'),'ollama_model':OLLAMA_MODEL,'current_track':self.payload(self.current_track),'playback_elapsed':playback_elapsed,'playback_authoritative':self._has_authoritative_playout(),'playout':playout,'next_track':self.payload(self.next_track),'prepared_count':prepared,'reservoir_count':len(self.reservoir),'reservoir_target':getattr(self.config,'reservoir_target',RESERVOIR_TARGET),'refill_threshold':getattr(self.config,'refill_threshold',RESERVOIR_REFILL_THRESHOLD),'is_refilling':bool(self._refill and not self._refill.done()),'reservoir':[self.payload(x) for x in self.reservoir],'history':[x.song_title for x in self.recently_played[-RECENTLY_PLAYED_LIMIT:]],'recently_played':[self.payload(x) for x in reversed(self.recently_played[-RECENTLY_PLAYED_LIMIT:])],'last_error':self.last_error,'defaults':opts.get('defaults') or {},'settings_path':str(SETTINGS_PATH),'vram_cleanup_mode':self.config.vram_cleanup_mode,'max_saved_tracks':self.config.max_saved_tracks,'lora_use_probability':getattr(self.config,'lora_use_probability',100),'archived_tracks':len(self.archived_tracks),'monitor_muted':bool(getattr(self.config,'monitor_muted',False)),'backend_playback':bool(self.backend_playback),'cache_available':self.outputs_cache.peek_count(),'cache_on_disk':_outputs_cache_on_disk_count(),'automatic_duration':bool(getattr(self.config,'automatic_duration',False)),'current_playback_rate':playback_rate,'current_playback_rate_percent':playback_rate_percent,'auto_transition_cut_seconds':transition_cut,'playback_modifiers':{'active':bool(transition_cut>0 or separator_before_end>0.0 or abs(playback_rate-1.0)>0.001),'transition_cut_seconds':transition_cut,'separator_before_end_seconds':separator_before_end,'speed_percent':playback_rate_percent,'speed_active':bool(abs(playback_rate-1.0)>0.001)},'reservoir_state':{'prepared_tracks':prepared,'next_ready':1 if self.next_track is not None else 0,'reservoir_ready':len(self.reservoir),'cache_pool_ready':self.outputs_cache.peek_count(),'cache_on_disk':_outputs_cache_on_disk_count(),'generation_in_progress':bool(self._generation_in_progress),'preparing_tracks':max(0, prepared-(1 if self.next_track is not None else 0)),'refill_threshold':getattr(self.config,'refill_threshold',RESERVOIR_REFILL_THRESHOLD),'reservoir_target':getattr(self.config,'reservoir_target',RESERVOIR_TARGET),'last_refill_reason':self._last_refill_reason,'last_generation_action':self._last_generation_action,'replenishment_state':'refilling' if self._generation_in_progress or bool(self._refill and not self._refill.done()) else ('ready' if prepared>=getattr(self.config,'reservoir_target',RESERVOIR_TARGET) else 'idle')},'backend_health':backend_health,'transition_state':{'current_track_title':str(getattr(self.current_track,'song_title','') or ''),'current_track_id':current_id,'next_track_title':str(getattr(self.next_track,'song_title','') or ''),'next_track_id':next_id,'queued_separator':str((self._queued_separator or {}).get('filename','') or ''),'active_jingle':str((self._jingle_event or {}).get('filename','') or ''),'active_jingle_mode':str((self._jingle_event or {}).get('mode','') or ''),'separator_transition_pending':bool(self._separator_transition_pending),'auto_transition_cut_seconds':transition_cut,'remaining_to_cut_seconds':round(remaining_to_cut_seconds,2) if remaining_to_cut_seconds is not None else None,'playback_elapsed':playback_elapsed,'playback_rate_percent':playback_rate_percent,'playout_fresh_for_current':playout_fresh_for_current},'ops_events':list(self._ops_events),'jingle_timing':{'jingle_separator_arm_offset_s':getattr(self.config,'jingle_separator_arm_offset_s',0.0),'jingle_separator_start_before_end_s':separator_before_end,'jingle_separator_min_remaining_offset_s':getattr(self.config,'jingle_separator_min_remaining_offset_s',0.0),'jingle_overlay_mid_offset_s':getattr(self.config,'jingle_overlay_mid_offset_s',0.0),'jingle_overlay_trigger_window_s':getattr(self.config,'jingle_overlay_trigger_window_s',JINGLE_OVERLAY_MID_WINDOW_S),'jingle_overlay_min_duration_s':getattr(self.config,'jingle_overlay_min_duration_s',JINGLE_OVERLAY_MIN_DURATION_S),'admin_separator_fade_ms':getattr(self.config,'admin_separator_fade_ms',500),'admin_overlay_pre_duck_ms':getattr(self.config,'admin_overlay_pre_duck_ms',300),'admin_overlay_restore_ms':getattr(self.config,'admin_overlay_restore_ms',700)},'can_step_previous':len(self.archived_tracks)>0,'can_step_next':self.next_track is not None or len(self.reservoir)>0}
    async def _advance_rotation(self, from_track_id: str = '') -> bool:
        current_id = self.current_track.id if self.current_track else ''

        if from_track_id and current_id and from_track_id != current_id:
            logger.debug(
                'AceRadio: _advance_rotation ignored — from_track_id=%s but current=%s',
                from_track_id, current_id,
            )
            return False

        if from_track_id and from_track_id == self._last_advanced_from_id:
            logger.debug(
                'AceRadio: _advance_rotation ignored — already advanced from track_id=%s',
                from_track_id,
            )
            return False

        if self._advancing:
            logger.debug('AceRadio: _advance_rotation already in progress, skipping duplicate call')
            return False

        if self._separator_transition_pending:
            logger.debug('AceRadio: _advance_rotation deferred — separator transition in progress')
            return False

        if self._queued_separator and not (self._jingle_event and self._jingle_event.get('status') == 'active'):
            sep = self._queued_separator
            self._queued_separator = None
            self._separator_transition_pending = True
            self._fire_jingle_event(sep, 'separator', is_transition=True)
            logger.info('[AceRadio] queued separator intercepted in advance — firing "%s" before next track', sep['filename'])
            return False

        self._advancing = True
        try:
            finished = self.current_track
            finished_id = finished.id if finished else ''
            if finished:
                self.archived_tracks.append(finished)
                self._trim_saved_tracks()
                self._mark_recently_played(finished)
                self.songs_since_overlay += 1
                self.songs_since_separator += 1
                if self.jingle_mgr:
                    self.jingle_mgr.increment_song_counters('overlay')
                    self.jingle_mgr.increment_song_counters('separator')
            self.current_track = None
            self.player_started_at = 0.0
            self._track_promoted_at = 0.0
            self._track_started_confirmed = False
            self._separator_transition_pending = False
            if finished_id:
                self._last_advanced_from_id = finished_id
            self._promote_next_to_current()
            self._sync_playout_tracks()
            self._ensure_refill()
            await asyncio.sleep(0)
            self._promote_reservoir_to_next()
            self._sync_playout_tracks()
            self._ensure_refill()
            return True
        finally:
            self._advancing = False

    def _fire_jingle_event(self, jingle: dict, mode: str,
                           launch_volume: Optional[float] = None,
                           is_transition: bool = False) -> None:
        import uuid as _uuid_mod
        filename = jingle['filename']
        if launch_volume is not None:
            effective_volume = max(0.0, min(1.0, float(launch_volume)))
        else:
            effective_volume = float(jingle.get('volume', 1.0))
        event: dict = {
            'event_id':    str(_uuid_mod.uuid4()),
            'mode':        mode,
            'filename':    filename,
            'audio_url':   f'/api/jingles/audio/{mode}/{filename}',
            'volume':      effective_volume,
            'fired_at':    time.time(),
            'status':      'active',
            'confirmed':   False,
            'is_transition': bool(is_transition),
        }
        self._jingle_event = event
        if mode == 'overlay':
            self.songs_since_overlay = 0
        else:
            self.songs_since_separator = 0
        logger.info('[Jingle] fired %s event: %s (id=%s, transition=%s)', mode, filename, event['event_id'], is_transition)
        self._notify_playout_jingle_event(event)

    def _check_jingle_events(self) -> None:
        jmgr = self.jingle_mgr
        if jmgr is None or not self.running or not self.current_track:
            return
        now = time.time()
        ev = self._jingle_event
        if ev and ev.get('status') == 'ended':
            if now - ev.get('fired_at', now) > JINGLE_EVENT_EXPIRY_S:
                self._jingle_event = None
        if ev and ev.get('status') == 'active':
            if now - ev.get('fired_at', now) > JINGLE_ACTIVE_EXPIRY_S:
                logger.warning('[Jingle] auto-expiring stuck active event %s (no confirm received)',
                               ev.get('event_id', '?'))
                self._jingle_event = None
                if ev.get('is_transition') and self._separator_transition_pending:
                    logger.warning('[AceRadio] transition separator auto-expired — completing deferred advance')
                    self._separator_transition_pending = False
                    asyncio.create_task(self._advance_rotation())
        if self._jingle_event and self._jingle_event.get('status') == 'active':
            return
        elapsed   = self._current_track_elapsed()
        duration  = self._current_track_duration()
        if duration <= 0:
            return
        transition_target = self._effective_transition_target_elapsed(duration)
        if transition_target <= 0.0:
            return
        remaining = max(0.0, transition_target - elapsed)
        cfg = self.config or RadioStartRequest()
        sep_start_before_end = _resolve_separator_start_before_end_s(getattr(cfg, 'jingle_separator_arm_offset_s', 0.0))
        if (sep_start_before_end > 0.0
                and remaining <= sep_start_before_end
                and remaining > 0.05
                and elapsed > 1.0
                and not self._separator_transition_pending):
            sep = self._pick_separator_candidate(allow_round_robin_fallback=True)
            if sep:
                self._queued_separator = None
                self._separator_transition_pending = True
                self._fire_jingle_event(sep, 'separator', is_transition=True)
                logger.info('[AceRadio] auto-separator started in closing window: %s (remaining=%.2fs, target=%.2fs)',
                            sep['filename'], remaining, sep_start_before_end)
                return

        sep_min_offset = _clamp_float(getattr(cfg, 'jingle_separator_min_remaining_offset_s', 0.0), 0.0, -30.0, 30.0)
        min_remaining_eff = max(0.0, JINGLE_SEP_TRIGGER_MIN_REMAINING_S + sep_min_offset)
        max_remaining_eff = max(0.25, JINGLE_SEP_TRIGGER_MAX_REMAINING_S)
        if max_remaining_eff <= min_remaining_eff:
            max_remaining_eff = min_remaining_eff + 0.25
        if (sep_start_before_end <= 0.0
                and min_remaining_eff < remaining <= max_remaining_eff
                and elapsed > 5.0
                and not self._queued_separator):
            sep = self._pick_separator_candidate(allow_round_robin_fallback=False)
            if sep:
                self._queued_separator = sep
                self._push_event('info', 'Separator queued', str(sep.get('filename') or ''))
                logger.info('[AceRadio] auto-separator armed for next transition: %s (remaining=%.2fs, window=%.2f..%.2fs)',
                            sep['filename'], remaining, min_remaining_eff, max_remaining_eff)
                return
        overlay_min_duration = _clamp_float(getattr(cfg, 'jingle_overlay_min_duration_s', JINGLE_OVERLAY_MIN_DURATION_S), JINGLE_OVERLAY_MIN_DURATION_S, 0.0, 600.0)
        overlay_span = transition_target if self._auto_transition_cut_seconds() > 0.0 else duration
        if overlay_span >= overlay_min_duration:
            overlay_mid_offset = _clamp_float(getattr(cfg, 'jingle_overlay_mid_offset_s', 0.0), 0.0, -120.0, 120.0)
            overlay_window = _clamp_float(getattr(cfg, 'jingle_overlay_trigger_window_s', JINGLE_OVERLAY_MID_WINDOW_S), JINGLE_OVERLAY_MID_WINDOW_S, 0.25, 30.0)
            mid = overlay_span * 0.5 + overlay_mid_offset
            safe_margin = min(max(overlay_window, 0.25), max(overlay_span / 2.0, 0.25))
            mid = max(safe_margin, min(overlay_span - safe_margin, mid))
            if elapsed <= overlay_span and abs(elapsed - mid) <= overlay_window and (self._track_started_confirmed or self.player_started_at > 0.0 or elapsed > 0.25):
                ov = jmgr.pick_eligible('overlay')
                if ov:
                    self._fire_jingle_event(ov, 'overlay')

    async def _supervise(self):
        try:
            self._ensure_refill()
            while self.running:
                self._promote_reservoir_to_next()
                self._promote_next_to_current()
                if self.current_track and not self.player_started_at and not self._has_authoritative_playout():
                    snap = self._playout_status()
                    current_id = str(getattr(self.current_track, 'id', '') or '')
                    snap_track_id = str(snap.get('current_track_id') or '')
                    playout_stale_for_current = bool(current_id and current_id == snap_track_id and (snap.get('stale') or snap.get('child_alive') is False or snap.get('snapshot_fresh') is False))
                    if (not playout_stale_for_current
                            and self._track_promoted_at
                            and not self._track_started_confirmed
                            and (time.time() - self._track_promoted_at) > TRACK_START_FALLBACK_S):
                        logger.warning(
                            '[AceRadio] FALLBACK CLOCK: track-started mai ricevuto per "%s" '
                            '(%.0fs dalla promozione) — autoplay bloccato o client assente. '
                            'Avvio clock backend. Il timing potrebbe essere impreciso.',
                            self.current_track.song_title,
                            time.time() - self._track_promoted_at,
                        )
                        self._start_backend_playback_clock()
                if self._current_track_finished():
                    current_id = self.current_track.id if self.current_track else ''
                    await self._advance_rotation(from_track_id=current_id)
                    continue
                self._check_jingle_events()
                self._ensure_refill()
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = str(e)
            self.running = False
            logger.exception('AceRadio supervisor failed')
    async def skip(self):
        await self._advance_rotation()
    async def track_started(self, track_id: str = '') -> bool:
        if not self.current_track:
            logger.debug('AceRadio: track-started ignored — no current track')
            return False
        if self._has_authoritative_playout():
            self._track_started_confirmed = True
            return False

        current_id = self.current_track.id if self.current_track else ''
        if track_id and current_id and track_id != current_id:
            logger.debug(
                'AceRadio: track-started ignored — stale track_id=%s current=%s',
                track_id, current_id,
            )
            return False

        if self.player_started_at > 0:
            if not self._track_started_confirmed:
                self._track_started_confirmed = True
                logger.debug(
                    '[AceRadio] track-started acknowledged for "%s" — clock already running, keeping elapsed %.2fs',
                    self.current_track.song_title,
                    self._current_track_elapsed(),
                )
            else:
                logger.debug(
                    '[AceRadio] duplicate track-started ignored for "%s" — elapsed %.2fs',
                    self.current_track.song_title,
                    self._current_track_elapsed(),
                )
            return False

        self._start_backend_playback_clock()
        self._track_started_confirmed = True
        self._check_jingle_events()
        logger.debug(
            '[AceRadio] track-started received for "%s" — clock started',
            self.current_track.song_title,
        )
        return True
    async def sync_playback_position(self, track_id: str, elapsed: float) -> bool:
        if not self.current_track:
            logger.debug('AceRadio: seek-sync ignored — no current track')
            return False
        if self._has_authoritative_playout():
            return False
        current_id = self.current_track.id if self.current_track else ''
        snap = self._playout_status()
        snap_track_id = str(snap.get('current_track_id') or '')
        if current_id and current_id == snap_track_id and (snap.get('stale') or snap.get('child_alive') is False or snap.get('snapshot_fresh') is False):
            logger.debug('AceRadio: seek-sync ignored — playout not authoritative for current track %s', current_id)
            return False
        if track_id and current_id and track_id != current_id:
            logger.debug('AceRadio: seek-sync ignored — stale track_id=%s current=%s', track_id, current_id)
            return False
        try:
            elapsed = float(elapsed)
        except Exception:
            return False
        duration = self._current_track_duration()
        if duration > 0:
            elapsed = max(0.0, min(elapsed, duration))
        else:
            elapsed = max(0.0, elapsed)
        rate = _normalize_playback_rate(self.current_playback_rate)
        self.player_started_at = max(0.0, time.time() - (elapsed / max(0.5, rate))) if self.current_track else 0.0
        self._track_started_confirmed = True
        self._check_jingle_events()
        remaining = self._remaining_to_transition_seconds(elapsed, duration)
        cfg = self.config or RadioStartRequest()
        sep_start_before_end = _resolve_separator_start_before_end_s(getattr(cfg, 'jingle_separator_arm_offset_s', 0.0))
        sep_min_offset = _clamp_float(getattr(cfg, 'jingle_separator_min_remaining_offset_s', 0.0), 0.0, -30.0, 30.0)
        min_remaining_eff = max(0.0, JINGLE_SEP_TRIGGER_MIN_REMAINING_S + sep_min_offset)
        max_remaining_eff = max(0.25, JINGLE_SEP_TRIGGER_MAX_REMAINING_S)
        if max_remaining_eff <= min_remaining_eff:
            max_remaining_eff = min_remaining_eff + 0.25
        imminent_window = max(0.75, sep_start_before_end if sep_start_before_end > 0.0 else max_remaining_eff)
        if duration > 0 and elapsed > 0.0 and remaining <= imminent_window:
            self._arm_separator_for_imminent_transition(remaining=remaining, reason='seek-sync')
        logger.debug('[AceRadio] seek-sync applied for "%s": elapsed=%.2fs remaining=%.2fs', self.current_track.song_title, elapsed, remaining)
        return True
    async def track_ended(self, track_id: str) -> bool:
        if not track_id:
            logger.debug('AceRadio: track-ended ignored — missing track_id')
            return False
        return await self._advance_rotation(from_track_id=track_id)
    async def previous(self) -> bool:
        if not self.archived_tracks:
            return False
        prev = self.archived_tracks.pop()
        if self.current_track:
            self.reservoir.insert(0, self.current_track)
            if self.next_track:
                self.reservoir.insert(1, self.next_track)
                self.next_track = None
        self.current_track = prev
        self.player_started_at = 0.0
        self._start_backend_playback_clock()
        self._promote_reservoir_to_next()
        self._sync_playout_tracks()
        return True
    async def next(self) -> bool:
        if self.next_track is None and not self.reservoir:
            return False
        await self._advance_rotation()
        return True

class StreamConfig(BaseModel):
    stream_preset: str = 'custom'
    protocol: str = 'icecast'
    host: str = 'localhost'
    port: int = 8000
    mount: str = '/stream'
    username: str = ''
    password: str = 'hackme'
    bitrate: int = 128
    format: str = 'mp3'
    name: str = 'AceRadio'
    description: str = 'AI-generated radio'
    genre: str = 'Various'
    public: bool = False

@dataclass
class StreamPlan:
    protocol: str
    preset: str
    auth_mode: str
    host: str
    port: int
    mount: str
    username: str
    password: str
    bitrate: int
    format: str
    name: str
    description: str
    genre: str
    public: bool
    transport_url: str
    display_target: str
    ffmpeg_format: str
    codec_flags: list[str]
    output_flags: list[str]
    notes: list[str]
    transport_backend: str = 'ffmpeg_url'
    source_port: int = 0

class StreamStatus(BaseModel):
    running: bool = False
    protocol: str = ''
    preset: str = ''
    auth_mode: str = ''
    host: str = ''
    port: int = 0
    mount: str = ''
    bitrate: int = 0
    format: str = ''
    pid: int = 0
    error: str = ''
    target_url: str = ''
    attempted_urls: list[str] = Field(default_factory=list)
    ffmpeg_state: str = ''
    ffmpeg_available: bool = False
    ffmpeg_command: list[str] = Field(default_factory=list)
    login_summary: str = ''
    stderr_tail: str = ''
    last_validation: dict[str, Any] = Field(default_factory=dict)

class StreamManager:

    def __init__(self):
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._config: Optional[StreamConfig] = None
        self._started_at: float = 0.0
        self._error: str = ''
        self._target_url: str = ''
        self._attempted_urls: list[str] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._feeder_task: Optional[asyncio.Task] = None
        self._radio_ref: Optional['RadioManager'] = None
        self._plan: Optional[StreamPlan] = None
        self._socket_reader: Optional[asyncio.StreamReader] = None
        self._socket_writer: Optional[asyncio.StreamWriter] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._socket_thread: Optional[threading.Thread] = None
        self._playout: Optional[RealtimePlayoutEngine] = None
        self._startup_event = threading.Event()
        self._startup_error: str = ''
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ffmpeg_command: list[str] = []
        self._last_ffmpeg_command_line: str = ''
        self._stderr_tail: str = ''
        self._last_validation: dict[str, Any] = {}
        self._last_audio_profile: dict[str, Any] = {}
        self._tx_bytes_sent: int = 0
        self._tx_started_at: float = 0.0
        self._tx_last_chunk_at: float = 0.0
        self._tx_window: deque = deque()

    def _reset_tx_stats(self) -> None:
        self._tx_bytes_sent = 0
        self._tx_started_at = 0.0
        self._tx_last_chunk_at = 0.0
        self._tx_window.clear()

    def _mark_tx_start(self) -> None:
        now = time.monotonic()
        self._tx_started_at = now
        self._tx_last_chunk_at = now
        self._tx_bytes_sent = 0

    def _record_tx_bytes(self, count: int) -> None:
        if count <= 0:
            return
        now = time.monotonic()
        if self._tx_started_at <= 0:
            self._mark_tx_start()
        self._tx_bytes_sent += int(count)
        self._tx_window.append((now, int(count)))
        self._tx_last_chunk_at = now

    def _tx_rate_kbps(self) -> float:
        if not self._tx_window:
            return 0.0
        now = time.monotonic()
        window_sec = 10.0
        cutoff = now - window_sec
        while self._tx_window and self._tx_window[0][0] < cutoff:
            self._tx_window.popleft()
        if not self._tx_window:
            return 0.0
        window_bytes = sum(b for _, b in self._tx_window)
        oldest_ts = self._tx_window[0][0]
        elapsed = max(now - oldest_ts, 0.1)
        return round((window_bytes * 8.0 / elapsed) / 1024.0, 1)

    @property
    def running(self) -> bool:
        if self._plan and self._plan.transport_backend == 'shoutcast_socket':
            proc_running = self._proc is not None and self._proc.returncode is None
            task_running = self._monitor_task is not None and not self._monitor_task.done()
            return bool(proc_running and task_running)
        return bool(self._playout is not None and self._playout.is_running())

    def _playout_status_provider(self) -> dict[str, Any]:
        if self._playout is not None:
            return self._playout.snapshot()
        if self._plan and self._plan.transport_backend == 'shoutcast_socket' and self._proc is not None:
            current_track_id = ''
            track_elapsed = 0.0
            if self._radio_ref is not None:
                current_track_id = str(getattr(getattr(self._radio_ref, 'current_track', None), 'id', '') or '')
                with contextlib.suppress(Exception):
                    track_elapsed = float(self._radio_ref._current_track_elapsed())
            return {
                'running': self._proc.returncode is None and self._monitor_task is not None and not self._monitor_task.done(),
                'engine_pid': int(getattr(self._proc, 'pid', 0) or 0),
                'child_alive': self._proc.returncode is None,
                'snapshot_fresh': True,
                'stale': False,
                'playback_authoritative': False,
                'current_track_id': current_track_id,
                'track_elapsed': max(0.0, track_elapsed),
                'stream_rate_kbps': self._tx_rate_kbps(),
                'stream_bytes_sent': int(self._tx_bytes_sent),
                'stream_started_monotonic': self._tx_started_at,
                'last_chunk_monotonic': self._tx_last_chunk_at,
                'last_error': self._sanitize_stderr(self._error),
            }
        return {}

    def _radio_track_payload(self, track: Optional['Track']) -> Optional[dict[str, Any]]:
        if track is None:
            return None
        track_id = str(getattr(track, 'id', '') or '')
        audio_path = str(getattr(track, 'audio_path', '') or '')
        if not track_id or not audio_path:
            return None
        return {
            'id': track_id,
            'song_title': str(getattr(track, 'song_title', '') or ''),
            'audio_path': audio_path,
            'playback_rate': _normalize_playback_rate(getattr(self._radio_ref, 'current_playback_rate', 1.0) if self._radio_ref is not None else 1.0),
        }

    def _radio_jingle_payload(self, event: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not event or str(event.get('status', '') or '') != 'active':
            return None
        event_id = str(event.get('event_id', '') or '')
        mode = str(event.get('mode', '') or '')
        filename = str(event.get('filename', '') or '')
        if not event_id or not mode or not filename or self._radio_ref is None or self._radio_ref.jingle_mgr is None:
            return None
        try:
            audio_path = self._radio_ref.jingle_mgr.audio_path(filename, mode)
        except Exception:
            return None
        if not audio_path:
            return None
        path = Path(audio_path)
        if not path.exists():
            return None
        return {
            'event_id': event_id,
            'mode': mode,
            'filename': filename,
            'audio_path': str(path),
            'volume': float(event.get('volume', 1.0) or 1.0),
            'is_transition': bool(event.get('is_transition', False)),
        }

    def sync_radio_state(self, radio: Optional['RadioManager'] = None) -> None:
        target = radio or self._radio_ref
        if target is None or self._playout is None:
            return
        self._playout.update_tracks(
            self._radio_track_payload(target.current_track),
            self._radio_track_payload(target.next_track),
        )

    def play_jingle_event(self, event: Optional[dict[str, Any]]) -> None:
        payload = self._radio_jingle_payload(event)
        if payload is None or self._playout is None:
            return
        self._playout.play_jingle(payload)

    def status(self) -> dict:
        cfg = self._config
        plan = self._plan
        ffmpeg_path = shutil.which('ffmpeg') or ''
        playout = self._playout.snapshot() if self._playout is not None else {}
        return {
            'running': self.running,
            'protocol': plan.protocol if plan else (cfg.protocol if cfg else ''),
            'preset': plan.preset if plan else (cfg.stream_preset if cfg else ''),
            'auth_mode': plan.auth_mode if plan else '',
            'host': plan.host if plan else (cfg.host if cfg else ''),
            'port': plan.port if plan else (cfg.port if cfg else 0),
            'mount': plan.mount if plan else (cfg.mount if cfg else ''),
            'bitrate': plan.bitrate if plan else (cfg.bitrate if cfg else 0),
            'format': plan.format if plan else (cfg.format if cfg else ''),
            'pid': int(playout.get('engine_pid') or (self._proc.pid if self._proc else 0) or 0),
            'error': self._sanitize_stderr(self._error),
            'target_url': self._target_url,
            'attempted_urls': list(self._attempted_urls),
            'started_at': self._started_at,
            'ffmpeg_state': 'running' if self.running else ('missing' if not ffmpeg_path else 'idle'),
            'ffmpeg_available': bool(ffmpeg_path),
            'ffmpeg_command': list(self._ffmpeg_command),
            'ffmpeg_command_line': getattr(self, '_last_ffmpeg_command_line', ''),
            'login_summary': self._login_summary(plan) if plan else '',
            'stderr_tail': self._sanitize_stderr(self._stderr_tail),
            'last_validation': dict(self._last_validation),
            'audio_profile': dict(getattr(self, '_last_audio_profile', {})),
            'stream_rate_kbps': float(playout.get('stream_rate_kbps') or 0.0),
            'stream_bytes_sent': int(playout.get('stream_bytes_sent') or 0),
            'stream_started_monotonic': float(playout.get('stream_started_monotonic') or 0.0),
            'stream_last_chunk_monotonic': float(playout.get('last_chunk_monotonic') or 0.0),
            'stream_declared_kbps': plan.bitrate if plan else 0,
            'stream_declared_format': plan.format.upper() if plan else '',
            'playout': playout,
        }


    def _sanitize_text(self, value: Any) -> str:
        if value is None:
            return ''
        text = str(value).strip()
        if not text:
            return ''
        return re.sub(r'[\x00-\x1f\x7f]+', '', text)

    def _ensure_ffmpeg(self) -> str:
        ffmpeg_path = shutil.which('ffmpeg')
        if not ffmpeg_path:
            raise RuntimeError('ffmpeg not found in PATH')
        return ffmpeg_path

    async def _read_stderr_nowait(self, proc: Optional[asyncio.subprocess.Process]) -> str:
        if proc is None:
            return ''
        data = b''
        stderr = getattr(proc, 'stderr', None)
        if stderr is not None:
            with contextlib.suppress(Exception):
                chunk = await asyncio.wait_for(stderr.read(), timeout=0.75)
                if isinstance(chunk, (bytes, bytearray)):
                    data += bytes(chunk)
                elif chunk:
                    return str(chunk)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=0.75)
        return data.decode('utf-8', errors='replace') if data else ''


    async def _wait_for_stream_start(self, timeout: float = 6.0) -> bool:
        deadline = time.monotonic() + max(0.5, float(timeout or 0.0))
        while time.monotonic() < deadline:
            if self._startup_error:
                return False
            playout = self._playout
            if playout is None:
                self._startup_error = 'playout process failed to start'
                return False
            snap = playout.snapshot()
            if snap.get('last_error'):
                err = str(snap.get('last_error') or '').strip()
                if err:
                    self._startup_error = err
                    self._stderr_tail = err[-2000:]
                    self._error = err[-1200:]
                return False
            if snap.get('playback_authoritative') and snap.get('running'):
                return True
            if self._startup_event.is_set() and not self._startup_error and snap.get('child_alive') is not False:
                return True
            if snap.get('child_alive') is False:
                self._startup_error = 'playout process failed to start'
                return False
            await asyncio.sleep(0.15)
        return bool((self._playout is not None and self._playout.wait_until_started(timeout=0.1)) or (self._startup_event.is_set() and not self._startup_error))

    async def start(self, cfg: StreamConfig, radio: 'RadioManager') -> None:
        if self.running:
            await self.stop()
        self._ensure_ffmpeg()
        plan = self._build_plan(cfg)
        self._config = cfg
        self._plan = plan
        self._loop = asyncio.get_running_loop()
        self._error = ''
        self._stderr_tail = ''
        self._target_url = plan.display_target
        self._attempted_urls = [plan.display_target]
        self._startup_error = ''
        self._startup_event.clear()
        self._reset_tx_stats()
        cmd = self._build_ffmpeg_command(plan, radio=radio, test_seconds=None, use_nullsrc=False)
        self._ffmpeg_command = self._redact_command(cmd)
        self._last_ffmpeg_command_line = self._format_command_line(self._ffmpeg_command)
        self._last_audio_profile = self._describe_audio_profile(plan, radio=radio, use_nullsrc=False, test_seconds=None)
        logger.info('[AceRadio] stream start: mode=%s auth=%s target=%s audio=%s cmd=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(self._last_audio_profile), self._last_ffmpeg_command_line)
        self._radio_ref = radio

        if plan.transport_backend == 'shoutcast_socket':
            with contextlib.suppress(Exception):
                radio.detach_playout_controller()
            with contextlib.suppress(Exception):
                radio.attach_playout_status_provider(self._playout_status_provider)
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._started_at = time.time()
            self._feeder_task = asyncio.create_task(self._feed_pcm_to_stdin(self._proc, plan))
            self._monitor_task = asyncio.create_task(self._stream_via_shoutcast_socket(self._proc, plan, startup_timeout=0.0))
            await asyncio.sleep(1.2)
            if self._proc.returncode is None and self._monitor_task and not self._monitor_task.done():
                return
            if self._monitor_task and self._monitor_task.done():
                with contextlib.suppress(Exception):
                    await self._monitor_task
            stderr = await self._read_stderr_nowait(self._proc)
            self._stderr_tail = stderr[-2000:]
            self._error = stderr[-1200:] if stderr else 'stream connection failed immediately'
            logger.warning('[AceRadio] stream start failed: mode=%s auth=%s target=%s audio=%s cmd=%s error=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(self._last_audio_profile), self._last_ffmpeg_command_line, self._sanitize_stderr(self._error))
            await self.stop()
            raise RuntimeError(self._humanize_stream_error(self._error, plan))

        radio.attach_playout_controller(self)
        radio.attach_playout_status_provider(self._playout_status_provider)
        self._start_playout_engine(plan, radio, cmd)
        self._started_at = time.time()
        self.sync_radio_state(radio)
        self.play_jingle_event(getattr(radio, '_jingle_event', None))
        if await self._wait_for_stream_start(timeout=6.0):
            return
        snap = self._playout.snapshot() if self._playout is not None else {}
        stderr = (
            str(snap.get('last_error') or '').strip()
            or self._stderr_tail
            or self._startup_error
            or ('playout process failed to start' if snap.get('child_alive') is False else '')
            or 'stream connection failed immediately'
        )
        self._error = stderr[-1200:] if stderr else 'stream connection failed immediately'
        logger.warning('[AceRadio] stream start failed: mode=%s auth=%s target=%s audio=%s cmd=%s error=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(self._last_audio_profile), self._last_ffmpeg_command_line, self._sanitize_stderr(self._error))
        await self.stop()
        raise RuntimeError(self._humanize_stream_error(self._error, plan))

    def _start_playout_engine(self, plan: StreamPlan, radio: 'RadioManager', ffmpeg_command: list[str]) -> None:
        self._playout = RealtimePlayoutEngine(
            on_track_started=lambda track_id: self._notify_track_started(track_id),
            on_track_end=lambda track_id: self._notify_track_end(track_id),
            on_jingle_started=lambda event: self._notify_jingle_started(event),
            on_jingle_ended=lambda event: self._notify_jingle_ended(event),
            on_stream_started=lambda event: self._notify_stream_started(event),
            on_stream_stopped=lambda event: self._notify_stream_stopped(event),
            on_error=lambda message: self._handle_playout_error(message),
        )
        stream_config = {
            'transport_backend': plan.transport_backend,
            'host': plan.host,
            'port': int(plan.port or 0),
            'source_port': int(plan.source_port or 0),
            'password': plan.password,
            'bitrate': int(plan.bitrate or 0),
            'format': plan.format,
            'name': plan.name,
            'genre': plan.genre,
            'public': bool(plan.public),
        }
        self._playout.start(ffmpeg_command, stream_config, self._encoder_sample_rate(plan))

    def _dispatch_coro_nonblocking(self, coro) -> None:
        loop = self._loop
        if loop is None:
            with contextlib.suppress(Exception):
                coro.close()
            return

        def _schedule() -> None:
            with contextlib.suppress(Exception):
                asyncio.create_task(coro)

        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(_schedule)

    def _notify_track_started(self, track_id: str) -> None:
        if not self._radio_ref or not track_id:
            return
        self._dispatch_coro_nonblocking(self._radio_ref.track_started(track_id))

    def _notify_track_end(self, track_id: str) -> None:
        if not self._radio_ref or not track_id:
            return
        self._dispatch_coro_nonblocking(self._radio_ref.track_ended(track_id))

    def _notify_jingle_started(self, event: dict[str, Any]) -> None:
        if not self._radio_ref:
            return
        self._dispatch_coro_nonblocking(self._radio_ref.playout_jingle_started(dict(event or {})))

    def _notify_jingle_ended(self, event: dict[str, Any]) -> None:
        if not self._radio_ref:
            return
        self._dispatch_coro_nonblocking(self._radio_ref.playout_jingle_ended(dict(event or {})))

    def _notify_stream_started(self, event: dict[str, Any]) -> None:
        self._startup_event.set()

    def _notify_stream_stopped(self, event: dict[str, Any]) -> None:
        stderr_tail = str((event or {}).get('stderr_tail', '') or '')
        if stderr_tail:
            self._stderr_tail = stderr_tail[-2000:]
        self._startup_event.set()

    def _handle_playout_error(self, message: str) -> None:
        text = str(message or '').strip()
        if text:
            self._startup_error = text
            self._stderr_tail = text[-2000:]
            self._error = text[-1200:]
        self._startup_event.set()


    async def validate(self, cfg: StreamConfig) -> dict[str, Any]:
        self._ensure_ffmpeg()
        plan = self._build_plan(cfg)
        if plan.transport_backend == 'shoutcast_socket':
            return await self._validate_shoutcast_socket(cfg)
        cmd = self._build_ffmpeg_command(plan, radio=None, test_seconds=3, use_nullsrc=True)
        safe_cmd = self._redact_command(cmd)
        safe_cmd_line = self._format_command_line(safe_cmd)
        audio_profile = self._describe_audio_profile(plan, radio=None, use_nullsrc=True, test_seconds=3)
        logger.info('[AceRadio] stream validate: mode=%s auth=%s target=%s audio=%s cmd=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), safe_cmd_line)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            stderr = await self._read_stderr_nowait(proc)
            result = {
                'ok': False,
                'mode': plan.preset,
                'protocol': plan.protocol,
                'auth_mode': plan.auth_mode,
                'login_summary': self._login_summary(plan),
                'target_url': plan.display_target,
                'ffmpeg_command': safe_cmd,
                'ffmpeg_command_line': safe_cmd_line,
                'stderr_tail': self._sanitize_stderr(stderr[-2000:] if stderr else 'validation timeout'),
                'reason': 'validation timeout',
                'audio_profile': dict(audio_profile),
            }
            self._last_validation = result
            return result
        stderr_text = (stderr or b'').decode('utf-8', errors='replace') if isinstance(stderr, (bytes, bytearray)) else str(stderr or '')
        stderr_tail = self._sanitize_stderr(stderr_text[-2000:])
        ok = proc.returncode == 0
        reason = 'validation succeeded' if ok else self._humanize_stream_error(stderr_tail or f'ffmpeg exited with code {proc.returncode}', plan)
        result = {
            'ok': ok,
            'mode': plan.preset,
            'protocol': plan.protocol,
            'auth_mode': plan.auth_mode,
            'login_summary': self._login_summary(plan),
            'target_url': plan.display_target,
            'ffmpeg_command': safe_cmd,
            'ffmpeg_command_line': safe_cmd_line,
            'stderr_tail': stderr_tail,
            'audio_profile': dict(audio_profile),
            'reason': reason,
            'returncode': proc.returncode,
        }
        if ok:
            logger.info('[AceRadio] stream validate result: ok mode=%s auth=%s target=%s audio=%s reason=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), reason)
        else:
            logger.warning('[AceRadio] stream validate failed: mode=%s auth=%s target=%s audio=%s cmd=%s error=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), safe_cmd_line, stderr_tail or reason)
        self._last_validation = result
        return result

    async def stop(self) -> None:
        self._startup_event.set()
        if self._feeder_task and not self._feeder_task.done():
            self._feeder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._feeder_task
        self._feeder_task = None
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        self._monitor_task = None
        if self._socket_writer is not None:
            with contextlib.suppress(Exception):
                self._socket_writer.close()
            with contextlib.suppress(Exception):
                await self._socket_writer.wait_closed()
        self._socket_reader = None
        self._socket_writer = None
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                if self._proc.stdin:
                    self._proc.stdin.close()
            with contextlib.suppress(Exception):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    self._proc.kill()
        self._proc = None
        if self._playout is not None:
            self._playout.stop()
        self._playout = None
        if self._radio_ref is not None:
            with contextlib.suppress(Exception):
                self._radio_ref.detach_playout_controller()
            with contextlib.suppress(Exception):
                self._radio_ref.attach_playout_status_provider(None)
        self._monitor_thread = None
        self._socket_thread = None
        self._radio_ref = None
        self._reset_tx_stats()


    def _sanitize_stderr(self, text: str) -> str:
        sanitized = self._redact_url(str(text or ''))
        if self._plan and self._plan.password:
            sanitized = sanitized.replace(self._plan.password, '***')
        return sanitized

    def _redact_url(self, url: str) -> str:
        try:
            scheme, rest = url.split('://', 1)
            creds, tail = rest.split('@', 1)
            if ':' in creds:
                user, _pwd = creds.split(':', 1)
                return f'{scheme}://{user}:***@{tail}'
        except Exception:
            return url
        return url

    def _redact_command(self, cmd: list[str]) -> list[str]:
        safe = list(cmd)
        if safe:
            safe[-1] = self._redact_url(safe[-1])
        if self._plan and self._plan.password:
            safe = [part.replace(self._plan.password, '***') for part in safe]
        return safe

    def _format_command_line(self, cmd: list[str]) -> str:
        try:
            return shlex.join([str(part) for part in cmd])
        except Exception:
            return ' '.join([str(part) for part in cmd])

    def _encoder_sample_rate(self, plan: StreamPlan) -> int:
        if plan.preset == 'listen2myradio_free' and plan.transport_backend == 'shoutcast_socket':
            return 44100
        if plan.preset in {'listen2myradio_live_only', 'listen2myradio_shoutcast2_autodj'}:
            return 48000
        return 48000 if plan.format == 'opus' else 44100


    async def _stream_decode_chunks(
            self,
            audio_path: str,
            sample_rate: int,
            chunk_bytes: int = 16384,
            start_sec: float = 0.0,
    ):
        p = Path(audio_path)
        if not p.exists() or not p.is_file() or p.stat().st_size < 512:
            return
        seek_flags = ['-ss', f'{start_sec:.3f}'] if start_sec > 0.05 else []
        decode_proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            *seek_flags,
            '-i', str(p),
            '-f', 's16le',
            '-ar', str(sample_rate),
            '-ac', '2',
            'pipe:1',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert decode_proc.stdout is not None
            while True:
                chunk = await decode_proc.stdout.read(chunk_bytes)
                if not chunk:
                    break
                yield chunk
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                decode_proc.kill()
            raise
        finally:
            with contextlib.suppress(Exception):
                if decode_proc.returncode is None:
                    decode_proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(decode_proc.wait(), timeout=3.0)

    async def _feed_pcm_to_stdin(self, proc: asyncio.subprocess.Process, plan: StreamPlan) -> None:
        sample_rate = self._encoder_sample_rate(plan)
        radio = self._radio_ref
        if not radio:
            return

        BYTES_PER_FRAME: int   = 4
        CHUNK_BYTES:     int   = 16384

        JF_DUCK_LEVEL:     float = 0.35
        PRE_DUCK_CHUNKS:   int   = 3
        RESTORE_CHUNKS:    int   = 8

        _last_track_id:    str   = ''
        _feeding_track_id: str   = ''
        _track_resume_sec: float = 0.0
        _last_jingle_event_id: str = ''

        _pcm_bytes_per_sec: float = float(sample_rate * BYTES_PER_FRAME)
        _pcm_rt: list = [time.monotonic(), 0]
        _chunk_duration_s: float = CHUNK_BYTES / _pcm_bytes_per_sec

        def _apply_gain(raw: bytes, gain: float) -> bytes:
            import array as _array
            a = _array.array('h', raw)
            for i in range(len(a)):
                v = int(a[i] * gain)
                a[i] = max(-32768, min(32767, v))
            return a.tobytes()

        def _mix_pcm(song_raw: bytes, jingle_raw: bytes,
                     song_gain: float, jingle_gain: float) -> bytes:
            try:
                import numpy as np
                s = np.frombuffer(song_raw,   dtype='<i2').astype(np.float32)
                j = np.frombuffer(jingle_raw, dtype='<i2').astype(np.float32)
                if len(j) < len(s):
                    j = np.pad(j, (0, len(s) - len(j)))
                elif len(j) > len(s):
                    j = j[:len(s)]
                return np.clip(s * song_gain + j * jingle_gain,
                               -32768, 32767).astype('<i2').tobytes()
            except Exception:
                import array as _array, struct as _struct
                n = len(song_raw) // 2
                s = _array.array('h', song_raw[:n * 2])
                jb = jingle_raw[:n * 2].ljust(n * 2, b'\x00')
                j = _array.array('h', jb)
                out = _array.array('h', [0] * n)
                for i in range(n):
                    v = int(s[i] * song_gain) + int(j[i] * jingle_gain)
                    out[i] = max(-32768, min(32767, v))
                return out.tobytes()

        async def _throttle_write(data: bytes) -> None:
            if not proc.stdin:
                raise asyncio.CancelledError
            proc.stdin.write(data)
            await proc.stdin.drain()
            _pcm_rt[1] += len(data)
            ahead = _pcm_rt[1] / _pcm_bytes_per_sec - (time.monotonic() - _pcm_rt[0])
            if ahead < -(_chunk_duration_s * 3):
                _pcm_rt[0] = time.monotonic()
                _pcm_rt[1] = 0
            elif ahead > (_chunk_duration_s * 0.5):
                await asyncio.sleep(ahead)

        async def _anext_or_none(gen):
            try:
                return await gen.__anext__()
            except StopAsyncIteration:
                return None

        async def _buffered_gen(source, prefetch=10):
            q = asyncio.Queue(maxsize=prefetch)
            _eof = object()
            _cancelled = object()
            _error_box = [None]
            async def _fill():
                try:
                    async for chunk in source:
                        await q.put(chunk)
                except asyncio.CancelledError:
                    try:
                        q.put_nowait(_cancelled)
                    except Exception:
                        pass
                    return
                except Exception as _exc:
                    _error_box[0] = _exc
                try:
                    await q.put(_eof)
                except asyncio.CancelledError:
                    try:
                        q.put_nowait(_cancelled)
                    except Exception:
                        pass
            filler = asyncio.create_task(_fill())
            try:
                while True:
                    item = await q.get()
                    if item is _eof:
                        if _error_box[0] is not None:
                            raise _error_box[0]
                        break
                    if item is _cancelled:
                        raise asyncio.CancelledError
                    yield item
            except asyncio.CancelledError:
                filler.cancel()
                with contextlib.suppress(Exception):
                    await filler
                raise
            finally:
                if not filler.done():
                    filler.cancel()
                    with contextlib.suppress(Exception):
                        await filler

        async def _write_chunks(gen, label: str, interrupt_check=None) -> tuple:
            if not proc.stdin:
                return False, 0
            written = 0
            try:
                async for chunk in gen:
                    if interrupt_check and interrupt_check():
                        logger.debug('[stream feeder] %s interrupted at %d B', label, written)
                        return False, written
                    await _throttle_write(chunk)
                    written += len(chunk)
                logger.info('[stream feeder] fed %s (%d B PCM)', label, written)
                return True, written
            except (BrokenPipeError, ConnectionResetError):
                raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[stream feeder] write error %s: %s', label, exc)
                return False, written

        async def _write_overlay_mix(
                song_gen,
                jingle_gen,
                jingle_vol: float,
                label: str,
        ) -> tuple:
            if not proc.stdin:
                return 0, False

            song_bytes_written = 0

            silence = b'\x00' * CHUNK_BYTES

            try:
                for i in range(PRE_DUCK_CHUNKS):
                    song_chunk = await _anext_or_none(song_gen)
                    if song_chunk is None:
                        return song_bytes_written, False
                    t = (i + 1) / PRE_DUCK_CHUNKS
                    gain = 1.0 + t * (JF_DUCK_LEVEL - 1.0)
                    await _throttle_write(_apply_gain(song_chunk, gain))
                    song_bytes_written += len(song_chunk)

                jingle_done = False
                last_song_chunk = None
                while True:
                    song_chunk = await _anext_or_none(song_gen)
                    if song_chunk is None:
                        while True:
                            j_chunk = await _anext_or_none(jingle_gen)
                            if j_chunk is None:
                                break
                            await _throttle_write(_apply_gain(j_chunk, jingle_vol))
                        return song_bytes_written, False
                    if not jingle_done:
                        j_chunk = await _anext_or_none(jingle_gen)
                        if j_chunk is None:
                            jingle_done = True
                            last_song_chunk = song_chunk
                            break
                        mixed = _mix_pcm(song_chunk, j_chunk, JF_DUCK_LEVEL, jingle_vol)
                        await _throttle_write(mixed)
                        song_bytes_written += len(song_chunk)
                    else:
                        last_song_chunk = song_chunk
                        break

                restore_list = []
                if last_song_chunk is not None:
                    restore_list.append(last_song_chunk)
                while len(restore_list) < RESTORE_CHUNKS:
                    c = await _anext_or_none(song_gen)
                    if c is None:
                        break
                    restore_list.append(c)

                total_restore = max(len(restore_list), 1)
                for i, rc in enumerate(restore_list):
                    t = (i + 1) / total_restore
                    gain = JF_DUCK_LEVEL + t * (1.0 - JF_DUCK_LEVEL)
                    await _throttle_write(_apply_gain(rc, gain))
                    song_bytes_written += len(rc)

                logger.info('[stream feeder] overlay mix done — %s: %d B song PCM', label, song_bytes_written)
                return song_bytes_written, True

            except (BrokenPipeError, ConnectionResetError):
                raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[stream feeder] overlay mix error %s: %s', label, exc)
                return song_bytes_written, False

        async def _write_separator_mix(
                song_gen,
                jingle_gen,
                jingle_vol: float,
                label: str,
        ) -> int:
            SEP_FADE_SEC     = 0.500
            PRE_SEP_CHUNKS   = 2
            OVERLAP_CHUNKS   = 3
            chunk_sec        = CHUNK_BYTES / BYTES_PER_FRAME / sample_rate

            if not proc.stdin:
                return 0

            song_bytes = 0
            silence    = b'\x00' * CHUNK_BYTES

            def _sep_gain(chunk_idx: int) -> float:
                return max(0.0, 1.0 - (chunk_idx + 0.5) * chunk_sec / SEP_FADE_SEC)

            try:
                for i in range(PRE_SEP_CHUNKS):
                    chunk = await _anext_or_none(song_gen)
                    if chunk is None:
                        return song_bytes
                    await _throttle_write(_apply_gain(chunk, _sep_gain(i)))
                    song_bytes += len(chunk)

                for i in range(OVERLAP_CHUNKS):
                    song_chunk = await _anext_or_none(song_gen)
                    sep_chunk  = await _anext_or_none(jingle_gen)
                    if song_chunk is None and sep_chunk is None:
                        return song_bytes
                    s_buf = song_chunk if song_chunk else silence
                    j_buf = sep_chunk  if sep_chunk  else silence
                    mixed = _mix_pcm(s_buf, j_buf, _sep_gain(PRE_SEP_CHUNKS + i), jingle_vol)
                    await _throttle_write(mixed)
                    if song_chunk:
                        song_bytes += len(song_chunk)

                while True:
                    sep_chunk = await _anext_or_none(jingle_gen)
                    if sep_chunk is None:
                        break
                    await _throttle_write(_apply_gain(sep_chunk, jingle_vol))

                logger.info('[stream feeder] separator mix done — %s: %d B song PCM', label, song_bytes)
                return song_bytes

            except (BrokenPipeError, ConnectionResetError):
                raise asyncio.CancelledError
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[stream feeder] separator mix error %s: %s', label, exc)
                return song_bytes

        logger.info('[AceRadio stream feeder] starting — sample_rate=%d', sample_rate)
        try:
            while True:
                if proc.returncode is not None:
                    logger.info('[AceRadio stream feeder] ffmpeg exited — stopping feeder')
                    break

                jingle_ev = radio._jingle_event
                if (jingle_ev
                        and jingle_ev.get('status') == 'active'
                        and jingle_ev.get('event_id') != _last_jingle_event_id):

                    jingle_ev_id  = str(jingle_ev.get('event_id', ''))
                    mode          = str(jingle_ev.get('mode', ''))
                    filename      = str(jingle_ev.get('filename', ''))
                    is_transition = bool(jingle_ev.get('is_transition', False))
                    jingle_vol    = float(jingle_ev.get('volume', 1.0))

                    _last_jingle_event_id = jingle_ev_id

                    jingle_path = None
                    if filename and radio.jingle_mgr:
                        jingle_path = radio.jingle_mgr.audio_path(filename, mode)

                    if jingle_path and jingle_path.exists():
                        if not jingle_ev.get('confirmed'):
                            jingle_ev['confirmed']    = True
                            jingle_ev['confirmed_at'] = time.time()
                            with contextlib.suppress(Exception):
                                radio.jingle_mgr.record_played(filename, mode)

                        if mode == 'overlay':
                            track = radio.current_track
                            if track and getattr(track, 'audio_path', ''):
                                song_gen   = _buffered_gen(self._stream_decode_chunks(
                                    track.audio_path, sample_rate, CHUNK_BYTES,
                                    start_sec=_track_resume_sec))
                                jingle_gen = _buffered_gen(self._stream_decode_chunks(
                                    str(jingle_path), sample_rate, CHUNK_BYTES), prefetch=8)
                                song_written, _ = await _write_overlay_mix(
                                    song_gen, jingle_gen, jingle_vol,
                                    f'overlay "{filename}" over "{track.song_title}"',
                                )
                                _track_resume_sec += song_written / _pcm_bytes_per_sec
                                _feeding_track_id  = track.id
                            else:
                                jingle_gen = _buffered_gen(self._stream_decode_chunks(
                                    str(jingle_path), sample_rate, CHUNK_BYTES), prefetch=8)
                                await _write_chunks(jingle_gen, f'overlay "{filename}" (no base track)')

                        else:
                            song_for_sep = None
                            if radio.current_track and getattr(radio.current_track, 'audio_path', ''):
                                song_for_sep = _buffered_gen(self._stream_decode_chunks(
                                    radio.current_track.audio_path, sample_rate, CHUNK_BYTES,
                                    start_sec=_track_resume_sec))
                            jingle_gen = _buffered_gen(self._stream_decode_chunks(
                                str(jingle_path), sample_rate, CHUNK_BYTES), prefetch=8)
                            if song_for_sep:
                                await _write_separator_mix(
                                    song_for_sep, jingle_gen, jingle_vol,
                                    f'separator "{filename}"',
                                )
                            else:
                                await _write_chunks(jingle_gen, f'separator "{filename}" (no base track)')

                        ev_now = radio._jingle_event
                        if ev_now and ev_now.get('event_id') == jingle_ev_id:
                            ev_now['status']   = 'ended'
                            ev_now['ended_at'] = time.time()
                            if is_transition and radio._separator_transition_pending:
                                logger.info('[AceRadio stream feeder] transition separator ended'
                                            ' — completing deferred advance')
                                radio._separator_transition_pending = False
                                await radio._advance_rotation()

                    else:
                        logger.warning('[stream feeder] jingle file missing: mode=%s file=%s event=%s',
                                       mode, filename, jingle_ev_id)
                        if jingle_ev.get('event_id') == jingle_ev_id:
                            jingle_ev['status']   = 'ended'
                            jingle_ev['ended_at'] = time.time()
                            if is_transition and radio._separator_transition_pending:
                                radio._separator_transition_pending = False
                                await radio._advance_rotation()

                    continue

                track = radio.current_track
                if not track:
                    await asyncio.sleep(0.1)
                    continue

                if track.id == _last_track_id:
                    await asyncio.sleep(0.1)
                    continue

                if track.id != _feeding_track_id:
                    _track_resume_sec = 0.0

                _feeding_track_id = track.id
                audio_path = getattr(track, 'audio_path', '') or ''

                if not audio_path:
                    logger.warning('[stream feeder] track "%s" has no audio_path', track.song_title)
                    _last_track_id    = track.id
                    _track_resume_sec = 0.0
                    await asyncio.sleep(0.5)
                    continue

                resume_sec           = _track_resume_sec
                _captured_track_id   = track.id

                gen = _buffered_gen(self._stream_decode_chunks(audio_path, sample_rate, CHUNK_BYTES,
                                                 start_sec=resume_sec))

                def _track_changed_or_jingle() -> bool:
                    if radio.current_track is None or radio.current_track.id != _captured_track_id:
                        return True
                    ev = radio._jingle_event
                    return bool(ev
                                and ev.get('status') == 'active'
                                and ev.get('event_id') != _last_jingle_event_id)

                completed, written = await _write_chunks(
                    gen,
                    f'track "{track.song_title}"' + (f' (resume {resume_sec:.1f}s)' if resume_sec else ''),
                    interrupt_check=_track_changed_or_jingle,
                )

                if completed:
                    _last_track_id    = track.id
                    _track_resume_sec = 0.0
                else:
                    cur = radio.current_track
                    if cur is None or cur.id != track.id:
                        _last_track_id    = track.id
                        _track_resume_sec = 0.0
                    else:
                        _track_resume_sec = resume_sec + written / _pcm_bytes_per_sec

        except asyncio.CancelledError:
            logger.info('[AceRadio stream feeder] cancelled')
        except Exception:
            logger.exception('[AceRadio stream feeder] unexpected error')
        finally:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()

    def _describe_audio_profile(self, plan: StreamPlan, radio: Optional['RadioManager'], use_nullsrc: bool, test_seconds: Optional[int]) -> dict[str, Any]:
        sample_rate = self._encoder_sample_rate(plan)
        profile = {
            'mode': 'validate' if use_nullsrc else 'start',
            'source': 'anullsrc' if use_nullsrc else 'track',
            'codec': 'libmp3lame' if plan.format == 'mp3' else ('aac' if plan.format == 'aac' else ('libopus' if plan.format == 'opus' else 'libvorbis')),
            'format': plan.format,
            'bitrate_kbps': int(plan.bitrate or 0),
            'sample_rate_hz': sample_rate,
            'channels': 2,
            'channel_layout': 'stereo',
            'legacy_source_mode': bool(plan.preset == 'listen2myradio_free' and plan.protocol == 'shoutcast'),
            'transport_protocol': plan.protocol,
            'test_seconds': int(test_seconds or 0),
        }
        if not use_nullsrc and radio and radio.current_track:
            track = radio.current_track
            profile['track_id'] = str(getattr(track, 'id', '') or '')
            profile['track_path'] = str(getattr(track, 'audio_path', '') or '')
            profile['track_mime'] = str(getattr(track, 'audio_mime', '') or '')
        return profile

    def _format_audio_profile(self, profile: dict[str, Any]) -> str:
        if not profile:
            return ''
        parts = [
            f"src={profile.get('source') or '?'}",
            f"fmt={profile.get('format') or '?'}",
            f"codec={profile.get('codec') or '?'}",
            f"br={profile.get('bitrate_kbps') or 0}k",
            f"sr={profile.get('sample_rate_hz') or 0}Hz",
            f"ch={profile.get('channels') or 0}",
        ]
        if profile.get('legacy_source_mode'):
            parts.append('legacy_source=1')
        if profile.get('test_seconds'):
            parts.append(f"test={profile.get('test_seconds')}s")
        return ' '.join(parts)

    def _normalize_host(self, cfg: StreamConfig) -> str:
        host = self._sanitize_text(cfg.host)
        if not host:
            raise ValueError('Stream host is required')
        return host

    def _normalize_port(self, cfg: StreamConfig) -> int:
        port = int(cfg.port or 0)
        if port <= 0 or port > 65535:
            raise ValueError('Stream port must be between 1 and 65535')
        return port

    def _normalize_format(self, cfg: StreamConfig) -> str:
        fmt = self._sanitize_text(cfg.format).lower() or 'mp3'
        if fmt not in {'mp3', 'aac', 'opus'}:
            raise ValueError('Unsupported stream format')
        return fmt

    def _normalize_mount(self, value: str, *, default: str = '') -> str:
        mount = self._sanitize_text(value)
        return mount or default

    def _normalize_preset(self, cfg: StreamConfig) -> str:
        preset = self._sanitize_text(cfg.stream_preset).lower() or 'custom'
        known = {
            'custom',
            'listen2myradio_free',
            'listen2myradio_shoutcast2_autodj',
            'listen2myradio_live_only',
            'generic_icecast2',
            'generic_shoutcast2',
            'generic_rtmp',
            'generic_srt',
        }
        return preset if preset in known else 'custom'

    def _build_plan(self, cfg: StreamConfig) -> StreamPlan:
        preset = self._normalize_preset(cfg)
        protocol = self._sanitize_text(cfg.protocol).lower() or 'icecast'
        host = self._normalize_host(cfg)
        port = self._normalize_port(cfg)
        fmt = self._normalize_format(cfg)
        bitrate = max(32, min(320, int(cfg.bitrate or 128)))
        password = str(cfg.password or '')
        username = self._sanitize_text(cfg.username)
        mount = self._normalize_mount(cfg.mount)
        name = self._sanitize_text(cfg.name) or 'AceRadio'
        description = self._sanitize_text(cfg.description)
        genre = self._sanitize_text(cfg.genre)
        public = bool(cfg.public)
        notes: list[str] = []
        source_port = port

        if preset == 'listen2myradio_free':
            username = ''
            if protocol == 'shoutcast2':
                protocol = 'shoutcast2'
                auth_mode = 'stream-password-sid'
                mount = self._normalize_mount(mount, default='1')
                if not password:
                    raise ValueError('Stream password is required for Listen2MyRadio Free / Shoutcast2')
                mount_path = '/' + quote(mount.lstrip('/') or '1', safe='')
                notes.append('Listen2MyRadio Free with protocol SHOUTcast2 uses Stream ID + Stream Password and keeps the username empty.')
                transport_url = f'icecast://:{quote(password, safe="")}@{host}:{port}{mount_path}'
                display_target = f'icecast://(empty-username):***@{host}:{port}{mount_path} [Shoutcast2 stream-password mode]'
                transport_backend = 'ffmpeg_url'
                source_port = port
            else:
                protocol = 'shoutcast'
                auth_mode = 'shoutcast-v1-password'
                mount = ''
                if not password:
                    raise ValueError('Broadcasting password is required for Listen2MyRadio Free')
                source_port = min(65535, port + 1)
                notes.append('Listen2MyRadio Free Shoutcast v1 now follows BUTT exactly: TCP connect to port+1, send password, Host, icy-* headers, wait for OK, then stream MP3 bytes.')
                notes.append('The panel port is kept as entered, while the native source socket uses port+1 just like BUTT shoutcast.cpp.')
                transport_url = f'shoutcast://***@{host}:{port}'
                display_target = f'shoutcast://***@{host}:{port} [native Shoutcast v1 password mode via source socket {source_port}]'
                transport_backend = 'shoutcast_socket'
        elif preset == 'listen2myradio_shoutcast2_autodj':
            protocol = 'shoutcast2'
            auth_mode = 'dj-user-id-dj-password'
            mount = self._normalize_mount(mount, default='1')
            if not username:
                raise ValueError('DJ User ID is required for Listen2MyRadio Shoutcast v2 (AutoDJ ON)')
            if not password:
                raise ValueError('DJ password is required for Listen2MyRadio Shoutcast v2 (AutoDJ ON)')
            transport_url = f'icecast://{quote(username, safe="")}:{quote(password, safe="")}@{host}:{port}/{quote(mount.lstrip("/"), safe="")}'
            display_target = f'icecast://{username}:***@{host}:{port}/{mount.lstrip("/") or "1"}'
            notes.append('Use Quick Links SHOUTcast v2 port only when AutoDJ is ON.')
        elif preset == 'listen2myradio_live_only':
            protocol = 'shoutcast'
            auth_mode = 'source-password-live-only'
            username = ''
            mount = ''
            if not password:
                raise ValueError('Source password is required for Listen2MyRadio Live Only')
            source_port = min(65535, port + 1)
            transport_url = f'shoutcast://***@{host}:{port}'
            display_target = f'shoutcast://***@{host}:{port} [live-only native Shoutcast v1 via source socket {source_port}]'
            notes.append('Use Account Overview port with AutoDJ OFF / live-only mode.')
            notes.append('AceRadio now mirrors BUTT for Shoutcast v1 live-only: connect to port+1, send password and icy-* headers, wait for OK, then stream MP3.')
            transport_backend = 'shoutcast_socket'
        elif protocol in ('icecast', 'icecast2'):
            protocol = 'icecast'
            auth_mode = 'icecast-username-password'
            mount = self._normalize_mount(mount, default='/stream')
            if not password:
                raise ValueError('Password is required for Icecast streaming')
            if not username:
                raise ValueError('Username is required for Icecast streaming')
            mount_path = '/' + mount.lstrip('/')
            transport_url = f'icecast://{quote(username, safe="")}:{quote(password, safe="")}@{host}:{port}{mount_path}'
            display_target = f'icecast://{username}:***@{host}:{port}{mount_path}'
        elif protocol in ('shoutcast', 'shoutcast1'):
            protocol = 'shoutcast'
            auth_mode = 'shoutcast-v1-password'
            mount = ''
            if not password:
                raise ValueError('Password is required for Shoutcast v1 streaming')
            source_port = min(65535, port + 1)
            transport_url = f'shoutcast://***@{host}:{port}'
            display_target = f'shoutcast://***@{host}:{port} [native Shoutcast v1 password mode via source socket {source_port}]'
            notes.append('Native Shoutcast v1 mode now mirrors BUTT: connect to port+1, send password then icy-* headers, and wait for an OK response before streaming audio.')
            transport_backend = 'shoutcast_socket'
        elif protocol == 'shoutcast2':
            auth_mode = 'shoutcast2-user-password-sid'
            mount = self._normalize_mount(mount, default='1')
            if not username:
                raise ValueError('User ID is required for Shoutcast v2 streaming')
            if not password:
                raise ValueError('Password is required for Shoutcast v2 streaming')
            transport_url = f'icecast://{quote(username, safe="")}:{quote(password, safe="")}@{host}:{port}/{quote(mount.lstrip("/"), safe="")}'
            display_target = f'icecast://{username}:***@{host}:{port}/{mount.lstrip("/") or "1"}'
        elif protocol == 'rtmp':
            auth_mode = 'rtmp-path'
            mount = self._normalize_mount(mount, default='live/stream')
            transport_url = f'rtmp://{host}:{port}/{mount.lstrip("/")}'
            display_target = transport_url
        elif protocol == 'srt':
            auth_mode = 'srt-streamid-passphrase'
            mount = self._normalize_mount(mount, default='mystream')
            if not password:
                raise ValueError('Passphrase is required for SRT streaming')
            transport_url = f'srt://{host}:{port}?streamid={quote(mount.lstrip("/"), safe="")}&passphrase={quote(password, safe="")}'
            display_target = f'srt://{host}:{port}?streamid={mount.lstrip("/")}&passphrase=***'
        else:
            raise ValueError(f'Unknown streaming protocol: {protocol}')

        use_legacy_icecast = preset == 'listen2myradio_live_only'
        use_minimal_listen2myradio_legacy_flags = (preset == 'listen2myradio_live_only')
        icecast_meta_flags = []
        if protocol in {'icecast', 'shoutcast', 'shoutcast2'} and not use_minimal_listen2myradio_legacy_flags:
            if name:
                icecast_meta_flags.extend(['-ice_name', name])
            if description:
                icecast_meta_flags.extend(['-ice_description', description])
            if genre:
                icecast_meta_flags.extend(['-ice_genre', genre])
            icecast_meta_flags.extend(['-ice_public', '1' if public else '0'])

        listen2myradio_48k = preset in {'listen2myradio_live_only', 'listen2myradio_shoutcast2_autodj'}
        encoder_sample_rate = '48000' if listen2myradio_48k else '44100'

        if fmt == 'mp3':
            codec_flags = ['-c:a', 'libmp3lame', '-b:a', f'{bitrate}k', '-ar', encoder_sample_rate, '-ac', '2']
            ffmpeg_format = 'mp3'
            output_flags = ['-legacy_icecast', '1' if use_legacy_icecast else '0', *icecast_meta_flags, '-content_type', 'audio/mpeg', '-f', 'mp3'] if protocol in {'icecast', 'shoutcast', 'shoutcast2'} else (['-f', 'flv'] if protocol == 'rtmp' else ['-f', 'mp3'])
        elif fmt == 'aac':
            codec_flags = ['-c:a', 'aac', '-b:a', f'{bitrate}k', '-ar', encoder_sample_rate, '-ac', '2']
            ffmpeg_format = 'adts'
            output_flags = ['-legacy_icecast', '1' if use_legacy_icecast else '0', *icecast_meta_flags, '-f', 'adts'] if protocol in {'icecast', 'shoutcast', 'shoutcast2'} else (['-f', 'flv'] if protocol == 'rtmp' else ['-f', 'adts'])
        else:
            codec_flags = ['-c:a', 'libopus', '-b:a', f'{bitrate}k', '-ar', '48000', '-ac', '2', '-vbr', 'on']
            ffmpeg_format = 'ogg'
            output_flags = ['-legacy_icecast', '1' if use_legacy_icecast else '0', *icecast_meta_flags, '-f', 'ogg'] if protocol in {'icecast', 'shoutcast', 'shoutcast2'} else (['-f', 'flv'] if protocol == 'rtmp' else ['-f', 'ogg'])

        if use_minimal_listen2myradio_legacy_flags and protocol in {'icecast', 'shoutcast'}:
            if fmt == 'mp3':
                output_flags = ['-legacy_icecast', '1', '-content_type', 'audio/mpeg', '-f', 'mp3']
            elif fmt == 'aac':
                output_flags = ['-legacy_icecast', '1', '-f', 'adts']
            else:
                output_flags = ['-legacy_icecast', '1', '-f', 'ogg']
            notes.append('Listen2MyRadio legacy presets use a minimal FFmpeg output flag set to match the validated CLI transport shape exactly.')
        if preset in {'listen2myradio_live_only', 'listen2myradio_shoutcast2_autodj'}:
            notes.append('Listen2MyRadio presets force a 48000 Hz encoder/input profile for validate/start to match the requested provider-side sample rate.')
        if preset == 'listen2myradio_free' and transport_backend == 'shoutcast_socket':
            notes.append('Listen2MyRadio Free Shoutcast v1 is encoded at 44100 Hz to match the successful BUTT-style connection profile.')

        transport_backend = locals().get('transport_backend', 'ffmpeg_url')
        return StreamPlan(
            protocol=protocol,
            preset=preset,
            auth_mode=auth_mode,
            host=host,
            port=port,
            mount=mount,
            username=username,
            password=password,
            bitrate=bitrate,
            format=fmt,
            name=name,
            description=description,
            genre=genre,
            public=public,
            transport_url=transport_url,
            display_target=display_target,
            ffmpeg_format=ffmpeg_format,
            codec_flags=codec_flags,
            output_flags=output_flags,
            notes=notes,
            transport_backend=transport_backend,
            source_port=source_port,
        )

    def _build_ffmpeg_command(self, plan: StreamPlan, radio: Optional['RadioManager'], test_seconds: Optional[int], use_nullsrc: bool) -> list[str]:
        sample_rate = str(self._encoder_sample_rate(plan))
        if use_nullsrc or test_seconds:
            input_flags = ['-re', '-f', 'lavfi', '-i', f'anullsrc=r={sample_rate}:cl=stereo']
            common = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', *input_flags]
            if test_seconds:
                common.extend(['-t', str(test_seconds)])
            metadata_flags = ['-vn', '-map', '0:a:0', *plan.codec_flags]
            if plan.transport_backend == 'shoutcast_socket':
                return [*common, *metadata_flags, '-f', 'mp3', 'pipe:1']
            return [*common, *metadata_flags, *plan.output_flags, plan.transport_url]
        input_flags = [
            '-f', 's16le',
            '-ar', sample_rate,
            '-ac', '2',
            '-i', 'pipe:0',
        ]
        common = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *input_flags]
        metadata_flags = ['-vn', '-map', '0:a:0', *plan.codec_flags]
        if plan.transport_backend == 'shoutcast_socket':
            return [*common, *metadata_flags, '-f', 'mp3', 'pipe:1']
        return [*common, *metadata_flags, *plan.output_flags, plan.transport_url]

    def _login_summary(self, plan: StreamPlan) -> str:
        if plan.auth_mode == 'empty-username-broadcast-password':
            return 'SHOUTcast v1 · password only · native source handshake'
        if plan.auth_mode == 'stream-password-sid':
            return 'SHOUTcast2 · username empty · stream password · Stream ID'
        if plan.auth_mode == 'dj-user-id-dj-password':
            return 'SHOUTcast v2 · DJ User ID + DJ password · SID enabled'
        if plan.auth_mode == 'source-password-live-only':
            return 'live-only · source password · Account Overview port'
        if plan.auth_mode == 'icecast-username-password':
            return 'Icecast 2 · username + password'
        if plan.auth_mode == 'legacy-source-password':
            return 'SHOUTcast v1 · password only · native source handshake'
        if plan.auth_mode == 'shoutcast2-user-password-sid':
            return 'Shoutcast v2 · user/password · SID'
        if plan.auth_mode == 'srt-streamid-passphrase':
            return 'SRT · streamid + passphrase'
        return plan.auth_mode

    def _humanize_stream_error(self, error: str, plan: StreamPlan) -> str:
        text = self._sanitize_stderr(error).strip()
        lower = text.lower()
        if '10053' in lower or 'broken pipe' in lower or 'connection reset' in lower:
            if plan.preset == 'listen2myradio_free':
                if plan.protocol == 'shoutcast2':
                    base = (
                        'Listen2MyRadio SHOUTcast2 rejected the session after connect/open and first audio bytes. '
                        'Most likely causes, in order: wrong Quick Links v2 publish port, wrong SID, or wrong credentials for that publish mode. '
                        'Listen2MyRadio docs for CentovaCast v3 distinguish Account Overview live-only ports from Quick Links live source ports.'
                    )
                else:
                    base = (
                        'Listen2MyRadio rejected the native SHOUTcast v1 source session after connect/open and first audio bytes. '
                        'Most likely causes, in order: wrong Account Overview port, wrong broadcasting password, or another source client already connected to the same Shoutcast v1 server. '
                        'The successful BUTT-style connection profile for Listen2MyRadio Free uses only host, port, and broadcasting password.'
                    )
                return f'{base} {text}'.strip()
            if plan.preset == 'listen2myradio_live_only':
                base = (
                    'Listen2MyRadio Live Only rejected the SOURCE session after connect/open and first audio bytes. '
                    'Most likely causes, in order: wrong Account Overview source port, AutoDJ not actually OFF, or wrong Source Password. '
                    'Listen2MyRadio docs say live-only uses the Account Overview port and Source Password, not the Quick Links AutoDJ publish ports.'
                )
                return f'{base} {text}'.strip()
            if plan.preset == 'listen2myradio_shoutcast2_autodj':
                base = (
                    'Listen2MyRadio SHOUTcast v2 AutoDJ rejected the DJ source session after connect/open. '
                    'Most likely causes, in order: wrong Quick Links v2 port, wrong DJ User ID / DJ password, or wrong SID. '
                    'Listen2MyRadio docs say AutoDJ-enabled live source connections use dedicated Quick Links ports and DJ credentials.'
                )
                return f'{base} {text}'.strip()
            return f'Server closed the connection for {plan.preset}. Verify port/mode/auth combination. {text}'.strip()
        if 'authentication' in lower or '401' in lower or '403' in lower:
            return f'Authentication failed for {self._login_summary(plan)}. {text}'.strip()
        if 'name or service not known' in lower or 'failed to resolve hostname' in lower:
            return f'Host resolution failed for {plan.host}. {text}'.strip()
        if 'error muxing a packet' in lower or 'error writing trailer' in lower or 'error closing file' in lower:
            if plan.preset in {'listen2myradio_free', 'listen2myradio_live_only'}:
                return (
                    f'Listen2MyRadio SHOUTcast v1 write failed after connect/open. '
                    'This usually points to password/source-session rejection rather than a missing mountpoint. '
                    f'{text}'
                ).strip()
            return f'Stream target rejected the write. Check protocol/port/mode for {plan.preset}. {text}'.strip()
        return text or f'Stream failed for {plan.preset}'

    async def _shoutcast_content_type(self, plan: StreamPlan) -> str:
        if plan.format == 'mp3':
            return 'audio/mpeg'
        if plan.format == 'aac':
            return 'audio/aac'
        return 'audio/ogg'

    async def _perform_shoutcast_handshake(self, plan: StreamPlan, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
        host_header = f'{plan.host}:{plan.port}' if int(plan.port or 0) not in {80, 443} else plan.host
        header_lines = [
            f'Host: {host_header}',
            f'icy-name:{plan.name or "AceRadio"}',
        ]
        genre = (plan.genre or '').strip()
        if genre:
            header_lines.append(f'icy-genre:{genre}')
        public_flag = '1' if plan.public else '0'
        header_lines.append(f'icy-pub:{public_flag}')
        header_lines.append(f'icy-br:{int(plan.bitrate or 128)}')
        header_lines.append(f'content-type:{self._shoutcast_content_type(plan)}')

        async def _read_response(timeout: float) -> bytes:
            try:
                return await asyncio.wait_for(reader.read(256), timeout=timeout)
            except asyncio.IncompleteReadError as exc:
                return exc.partial or b''

        def _decode_response(data: bytes) -> tuple[str, str]:
            text = (data or b'').decode('utf-8', errors='replace').strip()
            return text, text.lower()

        def _validate_response(data: bytes, *, empty_message: str) -> str:
            text, lower = _decode_response(data)
            if not data:
                raise RuntimeError(empty_message)
            if 'invalid password' in lower or 'bad password' in lower:
                raise RuntimeError(text or 'invalid shoutcast password')
            if not (lower.startswith('ok') and len(text) >= 2):
                raise RuntimeError(text or 'unexpected SHOUTcast handshake response')
            return text

        writer.write((str(plan.password or '') + '\r\n').encode('utf-8', errors='replace'))
        await writer.drain()

        phase1_response = b''
        phase1_timeout = False
        try:
            phase1_response = await _read_response(1.1)
        except asyncio.TimeoutError:
            phase1_timeout = True
        phase1_text, phase1_lower = _decode_response(phase1_response)
        if phase1_response:
            if 'invalid password' in phase1_lower or 'bad password' in phase1_lower:
                raise RuntimeError(phase1_text or 'invalid shoutcast password')
            if phase1_lower.startswith('ok') and len(phase1_text) >= 2:
                writer.write(('\r\n'.join(header_lines) + '\r\n\r\n').encode('utf-8', errors='replace'))
                await writer.drain()
                return phase1_text

        writer.write(('\r\n'.join(header_lines) + '\r\n\r\n').encode('utf-8', errors='replace'))
        await writer.drain()

        try:
            response = await _read_response(5.0)
        except asyncio.TimeoutError:
            if phase1_timeout:
                raise RuntimeError('timeout waiting for SHOUTcast server response')
            raise RuntimeError(phase1_text or 'unexpected SHOUTcast handshake response')
        if not response and phase1_response:
            return _validate_response(phase1_response, empty_message='empty SHOUTcast server response')
        return _validate_response(response, empty_message='empty SHOUTcast server response')
    async def _stream_via_shoutcast_socket(self, proc: asyncio.subprocess.Process, plan: StreamPlan, *, startup_timeout: float = 1.2) -> None:
        stderr_task = asyncio.create_task(self._read_stderr_nowait(proc))
        bytes_sent = 0
        handshake_text = ''
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(plan.host, int(plan.source_port or (plan.port + 1))), timeout=4.0)
            self._socket_reader = reader
            self._socket_writer = writer
            handshake_text = await self._perform_shoutcast_handshake(plan, reader, writer)
            logger.info('[AceRadio] shoutcast handshake ok: mode=%s target=%s source_port=%s response=%s', plan.preset, plan.display_target, int(plan.source_port or (plan.port + 1)), handshake_text.replace('\r', ' ').replace('\n', ' | '))
            self._mark_tx_start()
            started = time.monotonic()
            while True:
                chunk = await proc.stdout.read(16384) if proc.stdout else b''
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                bytes_sent += len(chunk)
                self._record_tx_bytes(len(chunk))
                if startup_timeout and bytes_sent > 0 and (time.monotonic() - started) >= startup_timeout:
                    break
            if startup_timeout and bytes_sent <= 0:
                raise RuntimeError('no audio bytes were sent to the Shoutcast socket')
            if startup_timeout:
                return
        except Exception as exc:
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
            stderr_text = ''
            with contextlib.suppress(Exception):
                stderr_text = await stderr_task
            combined = ' | '.join([part for part in [str(exc).strip(), self._sanitize_stderr(stderr_text)] if part])
            self._stderr_tail = combined[-2000:]
            self._error = combined[-1200:] if combined else str(exc)
            raise RuntimeError(combined or str(exc))
        finally:
            stderr_text = ''
            with contextlib.suppress(Exception):
                stderr_text = await stderr_task
            if stderr_text:
                self._stderr_tail = self._sanitize_stderr(stderr_text[-2000:])
            if self._socket_writer is not None:
                with contextlib.suppress(Exception):
                    self._socket_writer.close()
                with contextlib.suppress(Exception):
                    await self._socket_writer.wait_closed()
            self._socket_reader = None
            self._socket_writer = None

    async def _validate_shoutcast_socket(self, cfg: StreamConfig) -> dict[str, Any]:
        self._ensure_ffmpeg()
        plan = self._build_plan(cfg)
        cmd = self._build_ffmpeg_command(plan, radio=None, test_seconds=3, use_nullsrc=True)
        safe_cmd = self._redact_command(cmd)
        safe_cmd_line = self._format_command_line(safe_cmd)
        audio_profile = self._describe_audio_profile(plan, radio=None, use_nullsrc=True, test_seconds=3)
        logger.info('[AceRadio] stream validate: mode=%s auth=%s target=%s audio=%s cmd=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), safe_cmd_line)
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(self._stream_via_shoutcast_socket(proc, plan, startup_timeout=1.2), timeout=6.0)
            ok = True
            stderr_tail = self._sanitize_stderr(self._stderr_tail)
            reason = 'validation succeeded'
        except Exception as exc:
            ok = False
            stderr_tail = self._sanitize_stderr(str(exc) or self._stderr_tail)
            reason = self._humanize_stream_error(stderr_tail or 'validation failed', plan)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
        result = {
            'ok': ok,
            'mode': plan.preset,
            'protocol': plan.protocol,
            'auth_mode': plan.auth_mode,
            'login_summary': self._login_summary(plan),
            'target_url': plan.display_target,
            'ffmpeg_command': safe_cmd,
            'ffmpeg_command_line': safe_cmd_line,
            'stderr_tail': stderr_tail,
            'audio_profile': dict(audio_profile),
            'reason': reason,
            'returncode': 0 if ok else 1,
        }
        if ok:
            logger.info('[AceRadio] stream validate result: ok mode=%s auth=%s target=%s audio=%s reason=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), reason)
        else:
            logger.warning('[AceRadio] stream validate failed: mode=%s auth=%s target=%s audio=%s cmd=%s error=%s', plan.preset, plan.auth_mode, plan.display_target, self._format_audio_profile(audio_profile), safe_cmd_line, stderr_tail or reason)
        self._last_validation = result
        return result

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AceRadio — Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0b1018; --card: #121922; --border: rgba(255,255,255,.1);
    --accent: #00d4a0; --red: #ff2d78; --text: #e8edf5; --muted: #5a6a80;
    --font: 'Inter', system-ui, sans-serif;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--font);
    display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 16px;
    padding: 40px 36px 36px; width: 100%; max-width: 380px;
    box-shadow: 0 8px 40px rgba(0,0,0,.5);
  }
  .logo-row { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
  .logo-row img { height: 36px; opacity: .9; }
  .logo-row h1 { font-size: 1.25rem; font-weight: 900; letter-spacing: -.01em; }
  .logo-row p { font-size: .7rem; color: var(--muted); margin-top: 2px; }
  label { display: block; font-size: .72rem; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em; margin-bottom: 6px; }
  label { display: block; font-size: .72rem; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em; margin-bottom: 6px; margin-top: 14px; }
  input[type=text], input[type=password] {
    width: 100%; background: rgba(255,255,255,.05); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text); font-family: var(--font); font-size: .9rem;
    padding: 10px 14px; outline: none; transition: border-color .2s;
  }
  input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); }
  button {
    margin-top: 18px; width: 100%; background: var(--accent); color: #0b1018;
    border: none; border-radius: 8px; font-family: var(--font); font-size: .88rem;
    font-weight: 800; padding: 11px; cursor: pointer; transition: opacity .15s;
  }
  button:hover { opacity: .88; }
  .login-error {
    background: rgba(255,45,120,.12); border: 1px solid rgba(255,45,120,.35);
    border-radius: 7px; color: var(--red); font-size: .78rem; font-weight: 600;
    padding: 8px 12px; margin-bottom: 16px;
  }
  .hint { font-size: .66rem; color: var(--muted); margin-top: 14px; text-align: center; }
</style>
</head>
<body>
<div class="card">
  <div class="logo-row">
    <img src="/static/logo-marcopter-white_8k.png" alt="Marcopter">
    <div><h1>AceRadio</h1><p>AI Web Radio · Ace-Step V1.5</p></div>
  </div>
  {{ERROR}}
  <label for="usr">Username</label>
  <input type="text" id="usr" autofocus placeholder="Enter username…" autocomplete="username">
  <label for="pwd">Password</label>
  <input type="password" id="pwd" placeholder="Enter password…" autocomplete="current-password">
  <button id="loginBtn" onclick="doLogin()">Enter</button>
  <p class="hint">Set <code>ACERADIO_USERNAME</code> and <code>ACERADIO_PASSWORD</code> env vars to configure credentials.</p>
</div>
<script>
  document.getElementById('usr').addEventListener('keydown', e => { if(e.key==='Enter') document.getElementById('pwd').focus(); });
  document.getElementById('pwd').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
  async function doLogin() {
    const usr = document.getElementById('usr').value;
    const pwd = document.getElementById('pwd').value;
    const btn = document.getElementById('loginBtn');
    btn.disabled = true; btn.textContent = 'Checking…';
    try {
      const r = await fetch('/api/auth/login', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({username: usr, password: pwd})
      });
      if (r.ok) { window.location.href = '/'; }
      else { window.location.href = '/login?error=1'; }
    } catch(e) { window.location.href = '/login?error=1'; }
  }
</script>
</body>
</html>"""

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.opus', '.aac', '.wav32'}
AUDIO_MIME_MAP = {'.mp3':'audio/mpeg','.wav':'audio/wav','.flac':'audio/flac',
                  '.opus':'audio/ogg','.aac':'audio/aac','.wav32':'audio/wav'}

def _track_audio_job_dir(track: Optional[Track]) -> Optional[Path]:
    if not track or not getattr(track, 'audio_path', ''):
        return None
    try:
        return Path(track.audio_path).resolve().parent
    except Exception:
        return None

def _track_audio_spec(track: Optional[Track]) -> dict[str, Any]:
    prompt = getattr(track, 'prompt', {}) or {}
    audio_format = str(prompt.get('audio_format') or '').strip().lower()
    bitrate_raw = prompt.get('mp3_bitrate')
    sample_rate_raw = prompt.get('mp3_sample_rate')
    try:
        sample_rate = int(sample_rate_raw) if sample_rate_raw is not None else 0
    except Exception:
        sample_rate = 0
    bitrate = str(bitrate_raw or '').strip().lower()
    return {
        'audio_format': audio_format,
        'mp3_bitrate': bitrate,
        'mp3_sample_rate': sample_rate,
    }

class OutputsCache:

    def __init__(self, outputs_root: Path):
        self.outputs_root = outputs_root
        self._pool: list[Track] = []
        self._loaded_dirs: set[str] = set()
        self._invalid_dirs: dict[str, tuple[float, int, int]] = {}
        self._pending_dirs: dict[str, tuple[float, int, int]] = {}
        self._lock = asyncio.Lock()
        self._rotate_index: int = 0
        self._last_rebuild_report: dict[str, Any] = {'scanned': 0, 'deleted': 0, 'skipped': 0, 'errors': 0, 'deleted_dirs': [], 'error_details': []}

    def rebuild(self) -> int:
        self._pool = []
        self._loaded_dirs = set()
        self._invalid_dirs = {}
        self._pending_dirs = {}
        self._rotate_index = 0
        self._last_rebuild_report = _cleanup_incomplete_output_cache_dirs(self.outputs_root)
        return self._scan_sync()

    @property
    def available(self) -> int:
        return len(self._pool)

    def _job_dir_key_for_track(self, track: Optional[Track]) -> str:
        if not track or not getattr(track, 'audio_path', None):
            return ''
        try:
            return str(Path(str(track.audio_path)).resolve().parent)
        except Exception:
            return ''

    def _mark_track_rescannable(self, track: Optional[Track]) -> None:
        dir_key = self._job_dir_key_for_track(track)
        if not dir_key:
            return
        self._loaded_dirs.discard(dir_key)
        self._pending_dirs.pop(dir_key, None)
        self._invalid_dirs.pop(dir_key, None)

    def pop(self, exclude_job_ids: Optional[set[str]] = None) -> Optional[Track]:
        excluded = {str(x) for x in (exclude_job_ids or set()) if str(x or '').strip()}
        kept: list[Track] = []
        while self._pool:
            idx = random.randrange(len(self._pool))
            self._pool[idx], self._pool[-1] = self._pool[-1], self._pool[idx]
            track = self._pool.pop()
            try:
                if str(getattr(track, 'job_id', '') or '') in excluded:
                    kept.append(track)
                    continue
                audio_path = Path(str(getattr(track, 'audio_path', '') or ''))
                if audio_path and audio_path.exists() and audio_path.is_file() and audio_path.stat().st_size >= 1024:
                    self._mark_track_rescannable(track)
                    self._pool.extend(kept)
                    return track
            except Exception:
                pass
        self._pool.extend(kept)
        return None

    def reinsert(self, track: Optional[Track]) -> bool:
        if track is None:
            return False
        try:
            audio_path = Path(str(getattr(track, 'audio_path', '') or ''))
            key = str(audio_path.resolve()) if audio_path else ''
        except Exception:
            key = str(getattr(track, 'audio_path', '') or '')
        if not key:
            return False
        for item in self._pool:
            try:
                existing = str(Path(str(getattr(item, 'audio_path', '') or '')).resolve())
            except Exception:
                existing = str(getattr(item, 'audio_path', '') or '')
            if existing == key:
                return False
        self._pool.append(track)
        return True

    def peek_count(self) -> int:
        return len(self._pool)

    def pop_rotate(self, exclude_job_ids: Optional[set[str]] = None) -> Optional[Track]:
        if not self._pool:
            return None
        excluded = {str(x) for x in (exclude_job_ids or set()) if str(x or '').strip()}

        def _stable_key(t: Track) -> str:
            return str(getattr(t, 'audio_path', '') or '').lower()

        candidates = sorted(
            (t for t in self._pool
             if str(getattr(t, 'job_id', '') or '') not in excluded
             and Path(str(getattr(t, 'audio_path', '') or '')).is_file()),
            key=_stable_key,
        )
        if not candidates:
            candidates = sorted(
                (t for t in self._pool
                 if Path(str(getattr(t, 'audio_path', '') or '')).is_file()),
                key=_stable_key,
            )
        if not candidates:
            return None
        idx = self._rotate_index % len(candidates)
        self._rotate_index = (idx + 1) % len(candidates)
        return candidates[idx]

    async def scan(self, *, force: bool = False) -> int:
        async with self._lock:
            fn = self.rebuild if force else self._scan_sync
            return await asyncio.get_event_loop().run_in_executor(None, fn)

    def _scan_sync(self) -> int:
        if not self.outputs_root.exists():
            return 0
        added = 0
        for job_dir in sorted(self.outputs_root.iterdir()):
            if not job_dir.is_dir():
                continue
            if job_dir.name.startswith('_'):
                continue
            dir_key = str(job_dir)
            if dir_key in self._loaded_dirs:
                continue
            sidecar_exists = (job_dir / ACERADIO_TRACK_META_FILENAME).exists()
            meta_exists = (job_dir / 'metadata.json').exists()
            try:
                dir_sig = _job_dir_signature(job_dir)
            except Exception:
                dir_sig = None
            if dir_sig is not None and self._pending_dirs.get(dir_key) == dir_sig:
                continue
            if dir_sig is not None and self._invalid_dirs.get(dir_key) == dir_sig:
                continue
            if _job_dir_is_pending_without_sidecar(job_dir, metadata_present=meta_exists, sidecar_present=sidecar_exists):
                if dir_sig is not None:
                    self._pending_dirs[dir_key] = dir_sig
                continue
            track = self._load_job_dir(job_dir)
            if track:
                self._pending_dirs.pop(dir_key, None)
                self._invalid_dirs.pop(dir_key, None)
                self._pool.append(track)
                self._loaded_dirs.add(dir_key)
                added += 1
            elif dir_sig is not None:
                self._pending_dirs.pop(dir_key, None)
                self._invalid_dirs[dir_key] = dir_sig
        logger.info('OutputsCache: scanned %s, found %d new tracks (%d total)', self.outputs_root, added, len(self._pool))
        return added

    def register_existing_job_dir(self, job_dir: Path) -> bool:
        try:
            if not job_dir or not Path(job_dir).exists() or not Path(job_dir).is_dir():
                return False
            dir_key = str(Path(job_dir))
            if dir_key in self._loaded_dirs:
                return False
            track = self._load_job_dir(Path(job_dir))
            if not track:
                return False
            self._pending_dirs.pop(dir_key, None)
            self._invalid_dirs.pop(dir_key, None)
            self._pool.append(track)
            self._loaded_dirs.add(dir_key)
            logger.info('OutputsCache: registered generated track %s (%s)', track.song_title, job_dir.name)
            return True
        except Exception:
            logger.debug('OutputsCache: failed to register generated dir %s', job_dir)
            return False

    def _load_job_dir(self, job_dir: Path) -> Optional[Track]:
        meta: dict[str, Any] = {}
        sidecar_path = job_dir / ACERADIO_TRACK_META_FILENAME
        sidecar_exists = sidecar_path.exists()
        if sidecar_exists:
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding='utf-8'))
                if isinstance(sidecar, dict):
                    meta.update(sidecar)
            except Exception:
                logger.debug('OutputsCache: failed to parse sidecar in %s', job_dir)

        meta_path = job_dir / 'metadata.json'
        meta_exists = meta_path.exists()
        if _job_dir_is_still_settling(job_dir, metadata_present=meta_exists, sidecar_present=sidecar_exists):
            logger.debug('OutputsCache: deferring scan for %s because the folder is still being written', job_dir)
            return None
        if meta_exists:
            try:
                raw = json.loads(meta_path.read_text(encoding='utf-8'))
                req  = raw.get('request') or {}
                res  = raw.get('result')  or {}
                extra_outputs = res.get('extra_outputs') or {}
                lm_meta = extra_outputs.get('lm_metadata') if isinstance(extra_outputs, dict) else {}
                lm_meta = lm_meta if isinstance(lm_meta, dict) else {}
                audios = res.get('audios') or []
                audio0 = audios[0] if audios else {}
                engine_meta = {
                    'job_id':       raw.get('job_id') or job_dir.name,
                    'audio_paths':  res.get('audio_paths') or [],
                    'song_title':   req.get('song_title') or req.get('title') or req.get('caption') or '',
                    'title':        req.get('title') or req.get('song_title') or '',
                    'caption':      req.get('caption') or lm_meta.get('caption') or '',
                    'lyrics':       req.get('lyrics') or '',
                    'genre':        req.get('genre') or '',
                    'theme':        req.get('theme') or '',
                    'bpm':          req.get('bpm') or 100,
                    'key_scale':    req.get('keyscale') or 'C Major',
                    'duration':     req.get('duration') or 60,
                    'language':     req.get('vocal_language') or 'en',
                    'instrumental': bool(req.get('instrumental', False)),
                    'lora_id':      req.get('lora_id') or '',
                    'seed':         str(audio0.get('resolved_seed') or req.get('seed') or ''),
                    'style':        req.get('style') or req.get('caption') or '',
                    'inference_steps': _resolve_inference_steps_for_model(req.get('model'), req.get('inference_steps') or 8),
                    'infer_method': req.get('infer_method') or 'ode',
                    'guidance_scale': req.get('guidance_scale') or 7.0,
                    'shift':        req.get('shift') or 3.0,
                    'audio_format': req.get('audio_format') or '',
                    'model':        req.get('model_used') or req.get('model') or '',
                    'mp3_bitrate':  (audio0.get('export_applied') or {}).get('requested_bitrate')
                                    or audio0.get('mp3_bitrate')
                                    or req.get('mp3_bitrate') or '',
                    'mp3_sample_rate': (audio0.get('export_applied') or {}).get('applied_sample_rate')
                                    or audio0.get('mp3_sample_rate')
                                    or req.get('mp3_sample_rate') or 0,
                }
                for key, value in engine_meta.items():
                    if key not in meta or meta.get(key) in (None, '', [], {}, 0):
                        meta[key] = value
            except Exception:
                logger.debug('OutputsCache: failed to parse metadata in %s', job_dir)

        audio_path = _choose_best_audio_file(job_dir, meta)
        if not audio_path or not audio_path.exists() or not audio_path.is_file():
            return None

        try:
            file_size = audio_path.stat().st_size
        except Exception:
            return None
        if file_size < 1024:
            return None

        audio_bytes = b''
        ext = audio_path.suffix.lower()
        audio_mime = AUDIO_MIME_MAP.get(ext, 'audio/mpeg')
        if not str(meta.get('audio_format') or '').strip():
            meta['audio_format'] = ext.lstrip('.')

        raw_title = _sanitize_title(str(meta.get('song_title') or ''))
        if _title_looks_machine_generated(raw_title, job_name=job_dir.name, file_stem=audio_path.stem):
            raw_title = _sanitize_title(str(meta.get('title') or ''))
        if _title_looks_machine_generated(raw_title, job_name=job_dir.name, file_stem=audio_path.stem):
            caption_title = _sanitize_title(str(meta.get('caption') or ''))
            if ' | ' in caption_title:
                caption_title = caption_title.split(' | ')[0].strip()
            raw_title = caption_title
        if _title_looks_machine_generated(raw_title, job_name=job_dir.name, file_stem=audio_path.stem):
            raw_title = _fallback_title_from_lyrics(str(meta.get('lyrics') or ''))
        if _title_looks_machine_generated(raw_title, job_name=job_dir.name, file_stem=audio_path.stem):
            raw_title = _sanitize_title(audio_path.stem)
        if _title_looks_machine_generated(raw_title, job_name=job_dir.name, file_stem=audio_path.stem):
            if _job_dir_is_pending_without_sidecar(job_dir, metadata_present=meta_exists, sidecar_present=sidecar_exists):
                logger.debug('OutputsCache: deferring incomplete job dir %s until sidecar/title metadata is finalized', job_dir)
                return None
            logger.warning('OutputsCache: skipping %s because no human title could be recovered; suppressing repeats until the folder changes', job_dir)
            return None
        title = raw_title or 'Cached Track'
        if ' | ' in title:
            title = title.split(' | ')[0].strip()
        duration = max(10, int(meta.get('duration') or 60))
        _file_dur = _probe_audio_duration_sync(str(audio_path))
        _real_duration_for_track: Optional[float] = None
        if _file_dur and _file_dur > 0:
            duration = int(round(_file_dur))
            _real_duration_for_track = _file_dur
        bpm = max(40, min(240, int(meta.get('bpm') or 100)))

        prompt_source = _normalize_track_source_key(meta.get('original_source') or meta.get('source') or 'ai_generated')
        prompt_catalog_source = _normalize_catalog_source_optional(meta.get('catalog_source'))
        if not prompt_catalog_source and _normalize_display_source_key(meta.get('display_source')) == 'ai_catalog':
            prompt_catalog_source = 'generated'
        elif not prompt_catalog_source and prompt_source == 'file':
            prompt_catalog_source = 'library'
        prompt_display_source = _resolve_display_source(meta.get('source') or prompt_source, {'source': prompt_source, 'catalog_source': prompt_catalog_source, 'display_source': meta.get('display_source') or meta.get('display_source_label')}, bool(meta.get('instrumental', False)))
        prompt_dict = {
            'song_title': title,
            'genre': str(meta.get('genre') or ''),
            'style': str(meta.get('style') or meta.get('genre') or ''),
            'theme': str(meta.get('theme') or ''),
            'caption': str(meta.get('caption') or ''),
            'bpm': bpm,
            'key_scale': str(meta.get('key_scale') or 'C Major'),
            'duration': duration,
            'source': prompt_source,
            'catalog_source': prompt_catalog_source,
            'display_source': prompt_display_source,
            'display_source_label': _display_source_label(prompt_display_source),
            'audio_format': str(meta.get('audio_format') or ext.lstrip('.')),
            'inference_steps': _resolve_inference_steps_for_model(meta.get('model'), meta.get('inference_steps') or 8),
            'infer_method': meta.get('infer_method') or 'ode',
            'guidance_scale': meta.get('guidance_scale') or 7.0,
            'shift': meta.get('shift') or 3.0,
            'model': str(meta.get('model') or ''),
            'mp3_bitrate': str(meta.get('mp3_bitrate') or '').strip().lower(),
            'mp3_sample_rate': int(meta.get('mp3_sample_rate') or 0),
        }

        stable_job_id = str(meta.get('job_id') or job_dir.name)
        vote_count, _vote_voters = _extract_vote_info(meta)
        track = Track(
            id=_stable_track_id(stable_job_id, str(audio_path)),
            job_id=stable_job_id,
            song_title=title,
            tags=str(meta.get('style') or ''),
            lyrics=str(meta.get('lyrics') or ''),
            bpm=bpm,
            key_scale=str(meta.get('key_scale') or 'C Major'),
            duration=duration,
            created_at=audio_path.stat().st_mtime,
            audio_bytes=audio_bytes,
            audio_mime=audio_mime,
            seed=str(meta.get('seed') or ''),
            prompt=prompt_dict,
            language=str(meta.get('language') or 'en'),
            genre=str(meta.get('genre') or prompt_dict.get('genre') or prompt_dict.get('style') or ''),
            theme=str(meta.get('theme') or prompt_dict.get('theme') or ''),
            instrumental=bool(meta.get('instrumental', False)),
            lora_id=str(meta.get('lora_id') or ''),
            audio_path=str(audio_path),
            source='cache',
            vote_count=vote_count,
        )
        if _real_duration_for_track and _real_duration_for_track > 0:
            track.real_duration = _real_duration_for_track
            _sidecar_dur = meta.get('duration')
            _sidecar_real = meta.get('real_duration')
            _sidecar_needs_update = (
                _sidecar_real is None
                or abs(float(_sidecar_real or 0) - _real_duration_for_track) > 0.5
                or (isinstance(_sidecar_dur, (int, float))
                    and abs(int(_sidecar_dur) - int(round(_real_duration_for_track))) > 1)
            )
            if _sidecar_needs_update:
                try:
                    _write_track_sidecar(track)
                    logger.debug('OutputsCache: corrected stale duration in sidecar for %s '
                                 '(was %s, real=%.1fs)', job_dir.name, _sidecar_dur, _real_duration_for_track)
                except Exception:
                    pass
        return track

def _job_dir_is_complete_cache_entry(job_dir: Path) -> bool:
    sidecar = job_dir / ACERADIO_TRACK_META_FILENAME
    meta = job_dir / 'metadata.json'
    if not sidecar.exists() or not sidecar.is_file():
        return False
    if not meta.exists() or not meta.is_file():
        return False
    audio_path = _choose_best_audio_file(job_dir, _load_sidecar_json(job_dir))
    return bool(audio_path and audio_path.exists() and audio_path.is_file())


def _cleanup_incomplete_output_cache_dirs(outputs_root: Path, *, protected_dirs: Optional[set[Path]] = None) -> dict[str, Any]:
    protected = {p.resolve() for p in (protected_dirs or set()) if p is not None}
    report = {'scanned': 0, 'deleted': 0, 'skipped': 0, 'errors': 0, 'deleted_dirs': [], 'error_details': []}
    if not outputs_root.exists():
        return report
    for job_dir in sorted(outputs_root.iterdir()):
        if not job_dir.is_dir() or job_dir.name.startswith('_'):
            continue
        if not _is_safe_song_job_dir(job_dir):
            continue
        report['scanned'] += 1
        sidecar_exists = (job_dir / ACERADIO_TRACK_META_FILENAME).exists()
        meta_exists = (job_dir / 'metadata.json').exists()
        if job_dir.resolve() in protected:
            report['skipped'] += 1
            continue
        if _job_dir_is_pending_without_sidecar(job_dir, metadata_present=meta_exists, sidecar_present=sidecar_exists) or _job_dir_is_still_settling(job_dir, metadata_present=meta_exists, sidecar_present=sidecar_exists):
            report['skipped'] += 1
            continue
        if _job_dir_is_complete_cache_entry(job_dir):
            continue
        try:
            shutil.rmtree(job_dir)
            report['deleted'] += 1
            report['deleted_dirs'].append(job_dir.name)
        except Exception as exc:
            report['errors'] += 1
            report['error_details'].append(f'{job_dir.name}: {exc}')
    return report


def _collect_output_job_entries(*, protected_dirs: Optional[set[Path]] = None) -> list[dict[str, Any]]:
    protected = {p.resolve() for p in (protected_dirs or set()) if p is not None}
    entries: list[dict[str, Any]] = []
    if not OUTPUTS_ROOT.exists():
        return entries
    for d in OUTPUTS_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith('_'):
            continue
        if not _job_dir_is_complete_cache_entry(d):
            continue
        try:
            files = [f for f in d.iterdir() if f.is_file()]
        except Exception:
            continue
        if not files:
            continue
        try:
            mtime = max(f.stat().st_mtime for f in files)
        except Exception:
            mtime = 0.0
        meta = _load_sidecar_json(d)
        vote_count, _voters = _extract_vote_info(meta)
        entries.append({
            'mtime': mtime,
            'dir': d,
            'protected': d.resolve() in protected,
            'vote_count': vote_count,
        })
    return entries

def _choose_output_dirs_to_delete(keep: int, *, protected_dirs: Optional[set[Path]] = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keep = max(1, int(keep or 1))
    entries = _collect_output_job_entries(protected_dirs=protected_dirs)
    protected_entries = [e for e in entries if e['protected']]
    candidate_entries = [e for e in entries if not e['protected']]
    random.shuffle(candidate_entries)
    candidate_entries.sort(key=lambda e: int(e.get('vote_count', 0) or 0), reverse=True)
    kept_candidates = candidate_entries[:keep]
    delete_candidates = candidate_entries[keep:]
    return protected_entries + kept_candidates, delete_candidates

def _outputs_cache_on_disk_count() -> int:
    try:
        return len(_collect_output_job_entries())
    except Exception:
        return 0

def _trim_outputs_root_by_votes(keep: int, *, protected_dirs: Optional[set[Path]] = None) -> dict[str, Any]:
    import shutil as _shutil
    kept_entries, delete_candidates = _choose_output_dirs_to_delete(keep, protected_dirs=protected_dirs)
    total = len(kept_entries) + len(delete_candidates)
    removed = 0
    errors: list[str] = []
    removed_dirs: list[str] = []
    for entry in delete_candidates:
        d = entry['dir']
        try:
            _shutil.rmtree(d)
            removed += 1
            removed_dirs.append(d.name)
        except Exception as e:
            errors.append(f'{d.name}: {e}')
    return {
        'total': total,
        'kept': total - removed,
        'removed': removed,
        'protected': sum(1 for e in kept_entries if e.get('protected')),
        'removed_dirs': removed_dirs,
        'errors': errors,
    }

def create_app()->FastAPI:
    engine_app=create_engine_app(); app=FastAPI(title='AceRadio', version='1.0.0'); app.mount('/_engine', engine_app)
    static_dir=Path(__file__).parent/'static'; app.mount('/static', StaticFiles(directory=static_dir), name='static')
    engine=EngineClient(engine_app); dj=OllamaDJ(); radio=RadioManager(engine,dj); app.state.radio=radio
    stream_mgr = StreamManager(); app.state.stream_mgr = stream_mgr
    _project_root_for_jingles = str(Path(__file__).resolve().parents[3])
    jingle_mgr = JingleManager(_project_root_for_jingles)
    jingle_mgr.reload()
    app.state.jingle_mgr = jingle_mgr
    radio.jingle_mgr = jingle_mgr
    @app.middleware('http')
    async def auth_middleware(request: Request, call_next):
        if request.headers.get('X-AceRadio-Internal') == '1':
            return await call_next(request)
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        open_paths = {'/login', '/api/auth/login', '/api/auth/status', '/api/bootstrap_status',
                      '/listen', '/api/listener/ping', '/api/listener/count',
                      '/api/radio/status', '/api/stream/status', '/api/system',
                      '/api/radio/navigation',
                      '/api/jingles/confirm',
                      '/api/radio/track-ended',
                      '/api/listener/vote'}
        if (path in open_paths
                or path.startswith('/static/')
                or path.startswith('/api/audio/')
                or path.startswith('/api/radio/download/')
                or path.startswith('/api/jingles/audio/')):
            return await call_next(request)
        if not _check_auth(request):
            if path.startswith('/api/'):
                return JSONResponse({'detail': 'Unauthorized', 'auth_required': True}, status_code=401)
            return RedirectResponse('/login', status_code=302)
        return await call_next(request)

    @app.get('/login', response_class=HTMLResponse)
    async def login_page(request: Request):
        if _check_auth(request):
            return RedirectResponse('/', status_code=302)
        err_param = request.query_params.get('error', '')
        error_html = '<div class="login-error">Wrong password — try again.</div>' if err_param else ''
        return HTMLResponse(_LOGIN_HTML.replace('{{ERROR}}', error_html))

    @app.post('/api/auth/login')
    async def auth_login(request: Request):
        data = await request.json()
        usr = str(data.get('username', '')).strip()
        pwd = str(data.get('password', '')).strip()
        usr_hash = hashlib.sha256(usr.encode()).hexdigest()
        pwd_hash = hashlib.sha256(pwd.encode()).hexdigest()
        usr_ok = hmac.compare_digest(usr_hash, AUTH_USERNAME_HASH)
        pwd_ok = hmac.compare_digest(pwd_hash, AUTH_PASSWORD_HASH)
        if not (usr_ok and pwd_ok):
            return JSONResponse({'ok': False, 'detail': 'Wrong username or password'}, status_code=401)
        token = _new_session()
        resp = JSONResponse({'ok': True})
        resp.set_cookie(AUTH_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite='lax')
        return resp

    @app.post('/api/auth/logout')
    async def auth_logout(request: Request):
        token = request.cookies.get(AUTH_COOKIE)
        _sessions.pop(token, None)
        resp = JSONResponse({'ok': True})
        resp.delete_cookie(AUTH_COOKIE)
        return resp

    @app.get('/api/auth/status')
    async def auth_status(request: Request):
        return {'auth_enabled': AUTH_ENABLED, 'authenticated': _check_auth(request)}

    bootstrap=BootstrapState(phase='booting', message='Starting AceRadio bootstrap…', started_at=time.time())
    app.state.bootstrap=bootstrap
    app.state.bootstrap_task=None

    async def _bootstrap_runtime():
        bootstrap.phase='warming'
        bootstrap.message='Initializing ACE-Step runtime…'
        bootstrap.ready=False
        bootstrap.error=''
        try:
            await engine.ensure_started()
            bootstrap.phase='syncing'
            bootstrap.message='Loading model catalogs and runtime status…'
            await engine.get_json('/api/options')
            await engine.get_json('/api/health')
            bootstrap.phase='ready'
            bootstrap.message='AceRadio runtime ready.'
            bootstrap.ready=True
            bootstrap.completed_at=time.time()
        except Exception as e:
            bootstrap.phase='error'
            bootstrap.message='AceRadio bootstrap failed.'
            bootstrap.error=str(e)
            bootstrap.ready=False
            logger.exception('AceRadio bootstrap failed')

    @app.on_event('startup')
    async def _startup():
        _ensure_outputs_layout()
        _custom_catalog_browse_dir()
        _configs_browse_dir()
        app.state.settings_notice = ''
        app.state.settings_notice_level = 'ok'
        target, notice_level, notice = _resolve_startup_settings_target()
        if target is not None:
            _set_settings_path(target)
            try:
                _, info, warning = _load_and_activate_settings_file(target)
                app.state.settings_notice = warning or notice or f'Startup config loaded: {Path(info.get("path") or str(target)).name}'
                app.state.settings_notice_level = 'error' if warning else notice_level
            except HTTPException as exc:
                detail = getattr(exc, 'detail', '') or 'Startup config could not be loaded'
                app.state.settings_notice = f'Startup settings not loaded: {detail}'
                app.state.settings_notice_level = 'error'
                _set_settings_path(target)
            except Exception as exc:
                app.state.settings_notice = f'Startup settings not loaded: {exc}'
                app.state.settings_notice_level = 'error'
                _set_settings_path(target)
        else:
            _set_settings_path(DEFAULT_SETTINGS_PATH)
            app.state.settings_notice = notice
            app.state.settings_notice_level = notice_level
        app.state.bootstrap_task=asyncio.create_task(_bootstrap_runtime())

    @app.on_event('shutdown')
    async def _shutdown():
        task=getattr(app.state, 'bootstrap_task', None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        await radio.stop(); await stream_mgr.stop(); await engine.close(); await dj.close()
    @app.get('/', response_class=HTMLResponse)
    async def index(): return (static_dir/'index.html').read_text(encoding='utf-8')

    @app.get('/listen', response_class=HTMLResponse)
    async def listener(): return (static_dir/'listener.html').read_text(encoding='utf-8')
    @app.get('/api/options')
    async def options():
        opts = await _fetch_engine_model_options(engine)
        health = await engine.get_json('/api/health')
        inventory = _radio_compatible_model_inventory(opts)
        current = str(opts.get('current_model') or health.get('model') or 'acestep-v15-turbo').strip()
        models = [str(item.get('name') or '').strip() for item in inventory if str(item.get('name') or '').strip()]
        if current and current not in models:
            models.insert(0, current)
        saved_settings = _settings_payload_for_client(_normalize_settings_for_storage(_load_settings_file()))
        saved_model = str(saved_settings.get('model') or '').strip()
        saved_model_issue = ''
        if saved_model and saved_model not in set(models):
            saved_model_issue = f'Saved settings reference an unavailable AceRadio model: {saved_model}'
        return {
            'engine': opts,
            'health': health,
            'lora_catalog': await engine.get_json('/api/lora_catalog'),
            'dit_models': models,
            'dit_model_inventory': inventory,
            'current_dit_model': current,
            'default_genres': DEFAULT_GENRES,
            'default_themes': DEFAULT_THEMES,
            'valid_languages': list(VALID_LANGUAGES),
            'ollama_model': OLLAMA_MODEL,
            'ollama_base_url': OLLAMA_BASE_URL,
            'settings_path': str(SETTINGS_PATH),
            'settings_dir': str(CONFIGS_DIR),
            'settings_notice': str(getattr(app.state, 'settings_notice', '') or ''),
            'settings_notice_level': str(getattr(app.state, 'settings_notice_level', 'ok') or 'ok'),
            'saved_settings': saved_settings,
            'saved_model_issue': saved_model_issue,
            'vram_cleanup_modes': ['fast', 'balanced', 'aggressive'],
            'bootstrap': bootstrap.payload(),
            'generation_sources': ['ai_generated', 'file', 'both', 'cache'],
            'generation_modes': ['ai_generated', 'local_catalog', 'hybrid'],
            'catalog_sources': ['library', 'generated', 'all_local'],
            'audio_formats': ['mp3', 'flac', 'wav', 'wav32', 'opus', 'aac'],
            'mp3_bitrate_options': list(ACERADIO_MP3_BITRATE_OPTIONS),
            'mp3_sample_rate_options': list(ACERADIO_MP3_SAMPLE_RATE_OPTIONS),
            'songs_path': str(SONGS_PATH),
            'songs_external_glob': str(OUTPUTS_ROOT / SONGS_EXTERNAL_GLOB),
            'generated_songs_path': str(GENERATED_SONGS_HISTORY_PATH),
            'custom_catalog_path': str(CUSTOM_CATALOG_PATH),
            'defaults': {
                'lora_use_probability': 100,
                'generation_mode': 'ai_generated',
                'catalog_source': 'library',
                'generation_source': 'ai_generated',
                'mp3_bitrate': ACERADIO_MP3_DEFAULT_BITRATE,
                'mp3_sample_rate': ACERADIO_MP3_DEFAULT_SAMPLE_RATE,
                'automatic_duration': False,
                'station_prompt': 'Late-night radio for city insomniacs: cinematic, melodic, emotionally rich, and never predictable.',
            },
        }
    @app.get('/api/bootstrap_status')
    async def bootstrap_status():
        task=getattr(app.state, 'bootstrap_task', None)
        payload=bootstrap.payload()
        payload['task_done']=bool(task.done()) if task else False
        return payload

    @app.get('/api/radio/status')
    async def radio_status():
        active_listeners = sum(1 for v in _listener_sessions.values() if time.time() - v <= LISTENER_TTL)
        if not bootstrap.ready:
            return {
                'running': False,
                'radio_state': 'booting' if not bootstrap.error else 'error',
                'model': None,
                'ollama_model': OLLAMA_MODEL,
                'current_track': None,
                'playback_elapsed': 0,
                'next_track': None,
                'prepared_count': 0,
                'reservoir_count': 0,
                'reservoir_target': RESERVOIR_TARGET,
                'refill_threshold': RESERVOIR_REFILL_THRESHOLD,
                'is_refilling': False,
                'reservoir': [],
                'history': [],
                'recently_played': [],
                'last_error': bootstrap.error or '',
                'defaults': {},
                'settings_path': str(SETTINGS_PATH),
                'vram_cleanup_mode': VRAM_CLEANUP_MODE,
                'max_saved_tracks': DEFAULT_MAX_SAVED_TRACKS,
                'lora_use_probability': 100,
                'archived_tracks': 0,
                'monitor_muted': False,
                'backend_playback': False,
                'automatic_duration': False,
                'current_playback_rate': 1.0,
                'current_playback_rate_percent': 100,
                'auto_transition_cut_seconds': 0,
                'playback_modifiers': {'active': False, 'transition_cut_seconds': 0, 'separator_before_end_seconds': 0.0, 'speed_percent': 100, 'speed_active': False},
                'reservoir_state': {'prepared_tracks': 0, 'next_ready': 0, 'reservoir_ready': 0, 'cache_pool_ready': 0, 'generation_in_progress': False, 'preparing_tracks': 0, 'refill_threshold': RESERVOIR_REFILL_THRESHOLD, 'reservoir_target': RESERVOIR_TARGET, 'last_refill_reason': '', 'last_generation_action': '', 'replenishment_state': 'idle'},
                'backend_health': {'runtime_active': False, 'playout_active': False, 'playout_child_active': False, 'playout_authoritative': False, 'authority_source': 'idle', 'radio_on_air': False, 'current_track_loaded': False, 'child_alive': False, 'healthy': False, 'degraded': False, 'fallback_mode': False, 'snapshot_fresh': False, 'stale': False, 'stale_reason': '', 'last_error': ''},
                'transition_state': {'current_track_title': '', 'current_track_id': '', 'next_track_title': '', 'next_track_id': '', 'queued_separator': '', 'active_jingle': '', 'active_jingle_mode': '', 'separator_transition_pending': False, 'auto_transition_cut_seconds': 0, 'remaining_to_cut_seconds': None, 'playback_elapsed': 0, 'playback_rate_percent': 100},
                'ops_events': [],
                'listener_count': active_listeners,
                'bootstrap': bootstrap.payload(),
            }
        data=await radio.status()
        data['bootstrap']=bootstrap.payload()
        jmgr: JingleManager = app.state.jingle_mgr
        data['jingle_event'] = radio._jingle_event
        data['songs_since_overlay'] = radio.songs_since_overlay
        data['songs_since_separator'] = radio.songs_since_separator
        data['queued_separator'] = radio._queued_separator
        data['jingles_count'] = len(jmgr.all_jingles())
        data['listener_count'] = active_listeners
        return data
    @app.post('/api/radio/start')
    async def radio_start(payload:RadioStartRequest):
        payload.model = await _ensure_engine_radio_model_selected(engine, getattr(payload, 'model', ''), allow_default=True)
        payload = _normalize_radio_request(payload)
        await radio.start(payload)
        return {'ok':True, 'shift': payload.shift}
    @app.post('/api/radio/apply-settings')
    async def radio_apply_settings(payload:RadioStartRequest):
        previous = radio.config or RadioStartRequest()
        payload.model = await _ensure_engine_radio_model_selected(engine, getattr(payload, 'model', ''), allow_default=True)
        payload = _normalize_radio_request(payload)
        custom_before = bool(getattr(previous, 'custom_catalog_enabled', False))
        custom_after = bool(getattr(payload, 'custom_catalog_enabled', False))
        custom_changed = (
            custom_before != custom_after
            or str(getattr(previous, 'custom_catalog_name', '') or '').strip() != str(getattr(payload, 'custom_catalog_name', '') or '').strip()
            or int(getattr(previous, 'custom_catalog_song_count', 0) or 0) != int(getattr(payload, 'custom_catalog_song_count', 0) or 0)
            or int(getattr(previous, 'custom_catalog_ignored_count', 0) or 0) != int(getattr(payload, 'custom_catalog_ignored_count', 0) or 0)
        )
        radio.config=payload
        if custom_changed:
            if custom_after:
                radio._clear_future_prepared_tracks(reason='custom catalog changed')
            else:
                radio._clear_future_prepared_tracks(reason='custom catalog disabled')
        return {'ok':True,'applied':True,'running':radio.running,'shift':payload.shift}
    @app.post('/api/radio/stop')
    async def radio_stop(): await radio.stop(); return {'ok':True}
    @app.post('/api/radio/skip')
    async def radio_skip(): await radio.skip(); return {'ok':True}
    @app.post('/api/radio/previous')
    async def radio_previous():
        ok = await radio.previous()
        return {'ok': ok, 'available': ok}
    @app.post('/api/radio/next')
    async def radio_next():
        ok = await radio.next()
        return {'ok': ok, 'available': ok}
    @app.get('/api/radio/navigation')
    async def radio_navigation():
        has_prev = len(radio.archived_tracks) > 0
        has_next = radio.next_track is not None or len(radio.reservoir) > 0
        return {'has_previous': has_prev, 'has_next': has_next}
    @app.get('/api/radio/download/{track_id}')
    async def radio_download(track_id: str):
        all_tracks = (
            ([radio.current_track] if radio.current_track else []) +
            ([radio.next_track] if radio.next_track else []) +
            radio.reservoir + radio.archived_tracks
        )
        for t in all_tracks:
            if not t or t.id != track_id:
                continue
            if t.source == 'cache' and not t.audio_bytes and t.audio_path:
                try:
                    t.audio_bytes = Path(t.audio_path).read_bytes()
                except Exception:
                    raise HTTPException(status_code=404, detail='Cache audio file missing from disk')
            if not t.audio_bytes:
                raise HTTPException(status_code=404, detail='Audio not available')
            ext_map = {'audio/mpeg': 'mp3', 'audio/flac': 'flac', 'audio/wav': 'wav',
                       'audio/ogg': 'ogg', 'audio/aac': 'aac'}
            ext = ext_map.get(t.audio_mime, 'mp3')
            safe_title = re.sub(r'[^\w\s-]', '', str(t.song_title or 'track')).strip().replace(' ', '_')
            filename = f"{safe_title}.{ext}"
            return Response(
                content=t.audio_bytes,
                media_type=t.audio_mime,
                headers={'Content-Disposition': f'attachment; filename="{filename}"'}
            )
        raise HTTPException(status_code=404, detail='Track not found')
    @app.post('/api/radio/track-started')
    async def radio_track_started(request: Request):
        try:
            body = await request.json()
            track_id = str(body.get('track_id') or '') if isinstance(body, dict) else ''
        except Exception:
            track_id = ''
        started = await radio.track_started(track_id)
        return {'ok': True, 'started': started}
    @app.post('/api/radio/seek-sync')
    async def radio_seek_sync(request: Request):
        try:
            body = await request.json()
            track_id = str(body.get('track_id') or '') if isinstance(body, dict) else ''
            elapsed = float(body.get('elapsed') or 0.0) if isinstance(body, dict) else 0.0
        except Exception:
            track_id = ''
            elapsed = 0.0
        synced = await radio.sync_playback_position(track_id, elapsed)
        return {'ok': True, 'synced': synced, 'elapsed': round(max(0.0, float(elapsed or 0.0)), 3)}
    @app.post('/api/radio/track-ended')
    async def radio_track_ended(request: Request):
        try:
            body = await request.json()
            track_id = str(body.get('track_id') or '') if isinstance(body, dict) else ''
        except Exception:
            track_id = ''
        advanced = await radio.track_ended(track_id)
        return {'ok': True, 'advanced': advanced}
    @app.post('/api/radio/current-speed')
    async def radio_current_speed(request: Request):
        try:
            body = await request.json()
            rate = body.get('rate') if isinstance(body, dict) else 1.0
        except Exception:
            rate = 1.0
        applied = radio.set_current_playback_rate(rate)
        return {'ok': True, 'rate': applied, 'rate_percent': int(round(applied * 100)), 'running': radio.running}
    @app.get('/api/settings')
    async def api_get_settings(): return {'path': str(SETTINGS_PATH), 'settings': _settings_payload_for_client(_load_settings_file())}
    @app.post('/api/settings/save')
    async def api_save_settings(payload: dict[str, Any]):
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='Invalid settings payload')
        clean = _normalize_settings_for_storage(payload)
        clean['model'] = await _ensure_engine_radio_model_selected(engine, clean.get('model'), allow_default=True)
        clean = _normalize_settings_for_storage(clean)
        info = _save_settings_file(clean)
        return {'ok': True, **info, 'settings': _settings_payload_for_client(clean)}

    @app.post('/api/settings/save-as')
    async def api_save_settings_as(payload: dict[str, Any]):
        import asyncio, json as _json
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail='Invalid settings payload')
        clean = _normalize_settings_for_storage(payload)
        clean['model'] = await _ensure_engine_radio_model_selected(engine, clean.get('model'), allow_default=True)
        clean = _normalize_settings_for_storage(clean)
        initial_dir = str(_configs_browse_dir())
        initial_file = SETTINGS_PATH.name or DEFAULT_SETTINGS_FILENAME
        script = f'''
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.wm_attributes("-topmost", True)
path = filedialog.asksaveasfilename(
    title="Save AceRadio settings as",
    initialdir={_json.dumps(initial_dir)},
    initialfile={_json.dumps(initial_file)},
    defaultextension=".json",
    filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
)
print(path or "", end="")
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                'python', '-c', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            chosen = stdout.decode('utf-8', errors='replace').strip()
            if not chosen:
                return {'ok': False, 'cancelled': True, 'path': str(SETTINGS_PATH), 'settings': clean}
            info = _save_settings_file(clean, chosen)
            return {'ok': True, 'cancelled': False, **info, 'settings': _settings_payload_for_client(clean)}
        except asyncio.TimeoutError:
            return {'ok': False, 'cancelled': True, 'path': str(SETTINGS_PATH), 'settings': clean}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post('/api/settings/load')
    async def api_load_settings():
        settings, info, warning = _load_and_activate_settings_file(SETTINGS_PATH)
        return {'ok': True, 'path': info.get('path') or str(SETTINGS_PATH), 'exists': SETTINGS_PATH.exists(), 'settings': _settings_payload_for_client(settings), 'warning': warning}

    @app.post('/api/custom-catalog/apply')
    async def api_custom_catalog_apply(file: UploadFile = File(...)):
        try:
            raw_bytes = await file.read()
            text_payload = raw_bytes.decode('utf-8-sig')
            raw = json.loads(text_payload)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f'Invalid JSON: {e}')
        info = _activate_custom_catalog_from_payload(raw, getattr(file, 'filename', '') or CUSTOM_CATALOG_FILENAME, '')
        info = _custom_catalog_file_info(CUSTOM_CATALOG_PATH)
        current_settings = _normalize_settings_for_storage(_load_settings_file())
        current_settings.update({
            'custom_catalog_enabled': True,
            'custom_catalog_file': '',
            'custom_catalog_name': str(info.get('name') or getattr(file, 'filename', '') or CUSTOM_CATALOG_FILENAME),
            'custom_catalog_song_count': int(info.get('song_count') or 0),
            'custom_catalog_ignored_count': int(info.get('ignored_count') or 0),
        })
        current_settings = _normalize_settings_for_storage(current_settings)
        saved_info = _save_settings_file(current_settings)
        return {'ok': True, 'active': True, **info, 'settings': _settings_payload_for_client(current_settings), 'settings_path': saved_info.get('path')}

    @app.post('/api/custom-catalog/browse')
    async def api_custom_catalog_browse():
        script = f'''
from tkinter import Tk, filedialog
root = Tk(); root.withdraw(); root.attributes('-topmost', True)
path = filedialog.askopenfilename(
    title="Select AceRadio custom catalog",
    initialdir=r"{str(_custom_catalog_browse_dir())}",
    filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
)
print(path or "", end="")
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                'python', '-c', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            chosen = stdout.decode('utf-8', errors='replace').strip()
            if not chosen:
                return {'ok': False, 'cancelled': True, 'path': '', 'name': '', 'song_count': 0, 'ignored_count': 0}
            chosen_path = Path(chosen).expanduser().resolve()
            if not chosen_path.exists():
                raise HTTPException(status_code=404, detail=f'Custom catalog file not found: {chosen_path}')
            try:
                raw = json.loads(chosen_path.read_text(encoding='utf-8-sig'))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f'Invalid custom catalog JSON: {e}')
            prepared = _prepare_custom_catalog_payload(raw, chosen_path.name)
            songs = list(prepared.get('songs') or [])
            if not songs:
                raise HTTPException(status_code=400, detail='No usable songs found in the selected custom catalog')
            meta = prepared.get('_meta') if isinstance(prepared, dict) else {}
            return {
                'ok': True,
                'cancelled': False,
                'path': str(chosen_path),
                'name': str((meta or {}).get('original_name') or chosen_path.name),
                'song_count': int((meta or {}).get('song_count') or len(songs)),
                'ignored_count': int((meta or {}).get('ignored_count') or 0),
            }
        except asyncio.TimeoutError:
            return {'ok': False, 'cancelled': True, 'path': '', 'name': '', 'song_count': 0, 'ignored_count': 0}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post('/api/custom-catalog/apply-path')
    async def api_custom_catalog_apply_path(payload: dict[str, Any]):
        chosen = str((payload or {}).get('path') or '').strip()
        if not chosen:
            raise HTTPException(status_code=400, detail='No custom catalog file selected')
        info = _activate_custom_catalog_from_file(chosen)
        current_settings = _normalize_settings_for_storage(_load_settings_file())
        current_settings.update({
            'custom_catalog_enabled': True,
            'custom_catalog_file': chosen,
            'custom_catalog_name': str(info.get('name') or Path(chosen).name or CUSTOM_CATALOG_FILENAME),
            'custom_catalog_song_count': int(info.get('song_count') or 0),
            'custom_catalog_ignored_count': int(info.get('ignored_count') or 0),
        })
        current_settings = _normalize_settings_for_storage(current_settings)
        saved_info = _save_settings_file(current_settings)
        return {'ok': True, 'active': True, **info, 'settings': _settings_payload_for_client(current_settings), 'settings_path': saved_info.get('path')}

    @app.post('/api/custom-catalog/remove')
    async def api_custom_catalog_remove():
        removed = _remove_active_custom_catalog_file()
        current_settings = _normalize_settings_for_storage(_load_settings_file())
        current_settings.update({
            'custom_catalog_enabled': False,
            'custom_catalog_file': '',
            'custom_catalog_name': '',
            'custom_catalog_song_count': 0,
            'custom_catalog_ignored_count': 0,
        })
        current_settings = _normalize_settings_for_storage(current_settings)
        saved_info = _save_settings_file(current_settings)
        return {'ok': True, 'removed': removed, 'path': str(CUSTOM_CATALOG_PATH), 'settings': _settings_payload_for_client(current_settings), 'settings_path': saved_info.get('path')}
    @app.get('/api/stats')
    async def api_stats():
        in_memory = len(radio.archived_tracks) + (1 if radio.current_track else 0) + len(radio.reservoir)
        try:
            on_disk = sum(1 for p in OUTPUTS_ROOT.rglob('*.mp3')) + sum(1 for p in OUTPUTS_ROOT.rglob('*.wav')) + sum(1 for p in OUTPUTS_ROOT.rglob('*.flac'))
        except Exception:
            on_disk = in_memory
        engine_stats = await engine.get_json('/api/stats')
        total_generated = max(int(engine_stats.get('songs_generated') or 0), in_memory, on_disk)
        baseline = getattr(app.state, 'songs_generated_baseline', None)
        if baseline is None:
            baseline = total_generated
            app.state.songs_generated_baseline = baseline
        return {'songs_generated': total_generated, 'songs_generated_total': total_generated, 'songs_generated_this_run': max(0, total_generated - int(baseline))}
    @app.get('/api/system')
    async def api_system(request: Request):
        info = {'gpu_name': None, 'vram_used_mb': None, 'vram_total_mb': None, 'gpu_temp_c': None, 'gpu_power_w': None, 'client_ip': _get_client_ip(request), 'vote_scope': _vote_scope_label('listener_cookie')}
        if torch is not None and getattr(torch, 'cuda', None) and torch.cuda.is_available():
            import contextlib as _cl
            with _cl.suppress(Exception):
                info['gpu_name'] = torch.cuda.get_device_name(0)
            with _cl.suppress(Exception):
                mem = torch.cuda.mem_get_info(0)
                info['vram_used_mb'] = round((mem[1] - mem[0]) / 1024 / 1024)
                info['vram_total_mb'] = round(mem[1] / 1024 / 1024)
            with _cl.suppress(Exception):
                import subprocess
                out = subprocess.check_output(['nvidia-smi','--query-gpu=temperature.gpu,power.draw','--format=csv,noheader,nounits'], timeout=3).decode().strip()
                for line in out.splitlines():
                    parts = [x.strip() for x in line.split(',')]
                    if parts and parts[0].replace('.', '', 1).isdigit():
                        info['gpu_temp_c'] = int(float(parts[0]))
                    if len(parts) > 1:
                        with contextlib.suppress(Exception):
                            info['gpu_power_w'] = int(round(float(parts[1])))
                    break
        return info
    @app.post('/api/settings/browse')
    async def api_settings_browse():
        import asyncio, json as _json
        initial_dir = str(_configs_browse_dir())
        script = f'''
import tkinter as tk
from tkinter import filedialog
import json, sys
root = tk.Tk()
root.withdraw()
root.wm_attributes("-topmost", True)
path = filedialog.askopenfilename(
    title="Select AceRadio settings file",
    initialdir={_json.dumps(initial_dir)},
    filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
)
print(path or "", end="")
'''
        try:
            proc = await asyncio.create_subprocess_exec(
                'python', '-c', script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            chosen = stdout.decode('utf-8', errors='replace').strip()
            if not chosen:
                return {'ok': False, 'cancelled': True, 'path': None, 'settings': None}
            settings, info, warning = _load_and_activate_settings_file(chosen)
            return {'ok': True, 'cancelled': False, 'path': info.get('path') or str(chosen), 'settings': _settings_payload_for_client(settings), 'warning': warning}
        except asyncio.TimeoutError:
            return {'ok': False, 'cancelled': True, 'path': None, 'settings': None}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get('/api/audio/{track_id}')
    async def audio(track_id:str, request: Request):
        all_tracks = (
            ([radio.current_track] if radio.current_track else [])
            + ([radio.next_track] if radio.next_track else [])
            + list(radio.reservoir)
            + list(radio.archived_tracks)
            + list(radio.recently_played)
            + list(getattr(radio.outputs_cache, '_pool', []) or [])
        )
        seen_ids: set[str] = set()
        matched_track = None
        for t in all_tracks:
            if not t or not getattr(t, 'id', None) or t.id in seen_ids:
                continue
            seen_ids.add(t.id)
            if t.id != track_id:
                continue
            matched_track = t
            break

        def _stream_track_bytes(t):
            audio_bytes = b''
            if t.audio_path:
                try:
                    audio_bytes = Path(t.audio_path).read_bytes()
                except Exception:
                    audio_bytes = b''
            if not audio_bytes and t.audio_bytes:
                audio_bytes = t.audio_bytes
            if not audio_bytes:
                raise HTTPException(status_code=404, detail='Track has no audio bytes available')
            if t.source == 'cache' and not getattr(t, '_bitrate_fixed', False):
                try:
                    t._bitrate_fixed = True
                except Exception:
                    pass
            total = len(audio_bytes)
            headers = {'Accept-Ranges': 'bytes'}
            range_header = str(request.headers.get('range') or '').strip().lower()
            if range_header.startswith('bytes='):
                try:
                    raw = range_header[6:].split(',', 1)[0].strip()
                    start_raw, end_raw = raw.split('-', 1)
                    if start_raw == '':
                        length = int(end_raw)
                        if length <= 0:
                            raise ValueError
                        start = max(0, total - length)
                        end = total - 1
                    else:
                        start = int(start_raw)
                        end = total - 1 if end_raw == '' else int(end_raw)
                    if start < 0 or end < start or start >= total:
                        raise ValueError
                    end = min(end, total - 1)
                except Exception:
                    raise HTTPException(status_code=416, detail='Invalid range request')
                chunk = audio_bytes[start:end + 1]
                headers['Content-Range'] = f'bytes {start}-{end}/{total}'
                headers['Content-Length'] = str(len(chunk))
                return Response(content=chunk, media_type=t.audio_mime or 'audio/mpeg', status_code=206, headers=headers)
            headers['Content-Length'] = str(total)
            return Response(content=audio_bytes, media_type=t.audio_mime or 'audio/mpeg', headers=headers)

        if matched_track is not None:
            return _stream_track_bytes(matched_track)

        candidate_paths: list[Path] = []
        for t in all_tracks:
            if not t:
                continue
            ap = str(getattr(t, 'audio_path', '') or '').strip()
            if ap:
                candidate_paths.append(Path(ap))
        try:
            candidate_paths.extend(sorted(OUTPUTS_ROOT.rglob(ACERADIO_TRACK_META_FILENAME), key=lambda p: p.stat().st_mtime, reverse=True)[:64])
        except Exception:
            pass
        try:
            candidate_paths.extend(sorted(OUTPUTS_ROOT.rglob('metadata.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:64])
        except Exception:
            pass

        checked: set[str] = set()
        for candidate in candidate_paths:
            try:
                key = str(candidate.resolve())
            except Exception:
                key = str(candidate)
            if key in checked:
                continue
            checked.add(key)
            job_dir = candidate if candidate.is_dir() else candidate.parent
            sidecars = [job_dir / ACERADIO_TRACK_META_FILENAME, job_dir / 'metadata.json']
            meta = None
            for sidecar in sidecars:
                if not sidecar.exists() or not sidecar.is_file():
                    continue
                try:
                    raw = json.loads(sidecar.read_text(encoding='utf-8'))
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                possible_ids = {
                    str(raw.get('id') or '').strip(),
                    str(raw.get('track_id') or '').strip(),
                }
                if track_id in possible_ids:
                    meta = raw
                    break
            if meta is None:
                continue
            try:
                restored = radio.outputs_cache._load_job_dir(job_dir)
            except Exception:
                restored = None
            if restored is None:
                continue
            if not getattr(restored, 'id', None) or restored.id != track_id:
                restored.id = track_id
            return _stream_track_bytes(restored)

        raise HTTPException(status_code=404, detail='Track not found')
    @app.post('/api/radio/crossfade')
    async def radio_crossfade():
        if not radio.running: raise HTTPException(status_code=400, detail='Radio not running')
        await radio.skip()
        return {'ok':True}
    @app.get('/api/radio/deck-b')
    async def radio_deck_b():
        nxt = radio.next_track
        if nxt: return radio.payload(nxt)
        archived = [t for t in reversed(radio.archived_tracks) if t and t.audio_bytes]
        if archived: return radio.payload(archived[0])
        return None
    _listener_sessions: dict[str, float] = {}
    LISTENER_TTL = 12.0

    @app.post('/api/listener/ping')
    async def listener_ping(request: Request):
        now = time.time()
        sid = request.cookies.get('aceradio_listener_id') or secrets.token_urlsafe(16)
        _listener_sessions[sid] = now
        expired = [k for k, v in _listener_sessions.items() if now - v > LISTENER_TTL]
        for k in expired:
            del _listener_sessions[k]
        count = len(_listener_sessions)
        resp = JSONResponse({'ok': True, 'listeners': count})
        resp.set_cookie('aceradio_listener_id', sid, max_age=3600, httponly=True, samesite='lax')
        return resp

    @app.get('/api/listener/count')
    async def listener_count():
        now = time.time()
        active = sum(1 for v in _listener_sessions.values() if now - v <= LISTENER_TTL)
        return {'listeners': active}

    @app.post('/api/listener/vote')
    async def listener_vote(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        track_id = str((body or {}).get('track_id') or '').strip()
        if not track_id:
            raise HTTPException(status_code=400, detail='track_id required')

        all_tracks = ([radio.current_track] if radio.current_track else []) + ([radio.next_track] if radio.next_track else []) + list(radio.reservoir) + list(radio.archived_tracks) + list(radio.recently_played) + list(getattr(radio.outputs_cache, '_pool', []) or [])
        track = next((t for t in all_tracks if t and str(getattr(t, 'id', '') or '') == track_id), None)
        if track is None:
            for sidecar in sorted(OUTPUTS_ROOT.rglob(ACERADIO_TRACK_META_FILENAME), key=lambda p: p.stat().st_mtime, reverse=True)[:128]:
                job_dir = sidecar.parent
                try:
                    restored = radio.outputs_cache._load_job_dir(job_dir)
                except Exception:
                    restored = None
                if restored and str(getattr(restored, 'id', '') or '') == track_id:
                    track = restored
                    break
        if track is None or not getattr(track, 'audio_path', None):
            raise HTTPException(status_code=404, detail='Track not found')

        sid = request.cookies.get('aceradio_listener_id') or secrets.token_urlsafe(16)
        _listener_sessions[sid] = time.time()
        job_dir = Path(str(track.audio_path)).resolve().parent
        existing = _load_sidecar_json(job_dir)
        existing_vote_count, voters = _extract_vote_info(existing)
        fp = _listener_vote_fingerprint(sid)
        already_voted = bool(fp and fp in voters)
        if not already_voted and fp:
            voters.append(fp)
            existing_vote_count = max(existing_vote_count, int(getattr(track, 'vote_count', 0) or 0)) + 1
            track.vote_count = existing_vote_count
            _write_track_sidecar(track)
            latest = _load_sidecar_json(job_dir)
            latest['vote_count'] = existing_vote_count
            latest['vote_voters'] = voters
            latest['last_voted_at'] = time.time()
            (job_dir / ACERADIO_TRACK_META_FILENAME).write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding='utf-8')
            for candidate in all_tracks:
                if candidate and str(getattr(candidate, 'id', '') or '') == track_id:
                    candidate.vote_count = existing_vote_count
        else:
            existing_vote_count = max(existing_vote_count, int(getattr(track, 'vote_count', 0) or 0))

        resp = JSONResponse({'ok': True, 'track_id': track_id, 'vote_count': existing_vote_count, 'already_voted': already_voted, 'message': 'Thanks!' if not already_voted else 'Already voted', 'client_ip': _get_client_ip(request), 'vote_scope': _vote_scope_label('listener_cookie')})
        resp.set_cookie('aceradio_listener_id', sid, max_age=3600*24*90, httponly=True, samesite='lax')
        return resp

    @app.post('/api/radio/rescan-cache')
    async def radio_rescan_cache():
        added = await radio.outputs_cache.scan(force=True)
        if radio.running:
            radio._ensure_refill()
        cleanup = dict(getattr(radio.outputs_cache, '_last_rebuild_report', {}) or {})
        return {'ok': True, 'added': added, 'total': radio.outputs_cache.peek_count(), 'ready': radio.outputs_cache.peek_count(), 'on_disk': _outputs_cache_on_disk_count(), 'cleanup': cleanup}

    @app.post('/api/radio/clear-cache')
    async def radio_clear_cache():
        keep = max(1, int(getattr(radio.config, 'max_saved_tracks', DEFAULT_MAX_SAVED_TRACKS) or DEFAULT_MAX_SAVED_TRACKS))
        protected_dirs = {
            p for p in {
                _track_audio_job_dir(radio.current_track),
                _track_audio_job_dir(radio.next_track),
                *[_track_audio_job_dir(t) for t in radio.reservoir],
            } if p is not None
        }
        if not OUTPUTS_ROOT.exists():
            logger.info('AceRadio clear-cache: outputs root missing, nothing to trim')
            return {'ok': True, 'kept': 0, 'removed': 0, 'total': 0, 'protected': 0, 'criteria': {'keep': keep, 'mode': 'votes-then-random-ties'}}
        report = _trim_outputs_root_by_votes(keep, protected_dirs=protected_dirs)
        removed_generated_files = []
        generated_file_errors = []
        with GENERATED_SONGS_LOCK:
            for generated_file in sorted(OUTPUTS_ROOT.glob(GENERATED_SONGS_DATED_GLOB)):
                if not generated_file.is_file():
                    continue
                try:
                    generated_file.unlink()
                    removed_generated_files.append(generated_file.name)
                except Exception as e:
                    generated_file_errors.append(f'{generated_file.name}: {e}')
        radio.outputs_cache._pool = [t for t in radio.outputs_cache._pool if (_track_audio_job_dir(t) or Path('.')).exists()]
        radio.outputs_cache._loaded_dirs = set()
        radio.outputs_cache._pending_dirs = {}
        radio.outputs_cache._invalid_dirs = {}
        logger.info('AceRadio clear-cache: total=%d protected=%d requested_keep=%d deleted=%d remaining=%d criteria=votes-then-random-ties deleted_dirs=%s deleted_generated_files=%s errors=%s generated_file_errors=%s', report.get('total', 0), report.get('protected', 0), keep, report.get('removed', 0), report.get('kept', 0), report.get('removed_dirs', [])[:12], removed_generated_files[:12], report.get('errors', [])[:5], generated_file_errors[:5])
        return {'ok': True, 'total': report.get('total', 0), 'kept': report.get('kept', 0), 'removed': report.get('removed', 0), 'protected': report.get('protected', 0), 'ready': radio.outputs_cache.peek_count(), 'on_disk': _outputs_cache_on_disk_count(), 'errors': (report.get('errors', []) + generated_file_errors)[:5], 'removed_generated_files': removed_generated_files[:20], 'criteria': {'keep': keep, 'mode': 'votes-then-random-ties', 'protected_active_dirs': len(protected_dirs)}}

    @app.post('/api/radio/clear-all-songs')
    async def radio_clear_all_songs():
        import shutil as _shutil
        await radio.stop()
        removed = 0
        errors = []
        removed_dirs = []
        cleared_logs = 0
        if OUTPUTS_ROOT.exists():
            uuid_like = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
            song_dir_prefix = re.compile(r'^[0-9a-fA-F]{6}')
            with GENERATED_SONGS_LOCK:
                for generated_file in sorted(OUTPUTS_ROOT.glob(GENERATED_SONGS_DATED_GLOB)):
                    if not generated_file.is_file():
                        continue
                    try:
                        generated_file.unlink()
                    except Exception as e:
                        errors.append(f'{generated_file.name}: {e}')
            logs_dir = OUTPUTS_ROOT / '_logs'
            if logs_dir.exists() and logs_dir.is_dir():
                for child in sorted(logs_dir.iterdir()):
                    try:
                        if child.is_dir():
                            _shutil.rmtree(child)
                        else:
                            child.unlink()
                        cleared_logs += 1
                    except Exception as e:
                        errors.append(f'_logs/{child.name}: {e}')
            for d in sorted(OUTPUTS_ROOT.iterdir()):
                if not d.is_dir():
                    continue
                if d.name == '_logs':
                    continue
                if d.name.startswith('_'):
                    continue
                if not _is_safe_song_job_dir(d):
                    continue
                try:
                    _shutil.rmtree(d)
                    removed += 1
                    removed_dirs.append(d.name)
                except Exception as e:
                    errors.append(f'{d.name}: {e}')
        radio.current_track = None
        radio.next_track = None
        radio._sync_playout_tracks()
        radio.reservoir = []
        radio.archived_tracks = []
        radio.recently_played = []
        radio.player_started_at = 0.0
        radio.outputs_cache._pool = []
        radio.outputs_cache._loaded_dirs = set()
        logger.info('AceRadio clear-all-songs: removed=%d cleared_logs=%d deleted_dirs=%s errors=%s', removed, cleared_logs, removed_dirs[:12], errors[:5])
        return {'ok': True, 'removed': removed, 'cleared_logs': cleared_logs, 'errors': errors[:5], 'deleted_dirs': removed_dirs[:20]}

    @app.post('/api/stream/start')
    async def stream_start(payload: StreamConfig):
        if not bootstrap.ready:
            raise HTTPException(status_code=503, detail='Runtime not ready')
        try:
            await stream_mgr.start(payload, radio)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            detail = str(exc)
            status = 503 if 'ffmpeg not found' in detail.lower() else 500
            raise HTTPException(status_code=status, detail=detail)
        except Exception as exc:
            logger.exception('AceRadio stream start failed')
            raise HTTPException(status_code=500, detail=str(exc))
        radio._push_event('info', 'Stream started', str((stream_mgr.status() or {}).get('target_url') or ''))
        return {'ok': True, 'running': stream_mgr.running, 'status': stream_mgr.status()}

    @app.post('/api/stream/validate')
    async def stream_validate(payload: StreamConfig):
        if not bootstrap.ready:
            raise HTTPException(status_code=503, detail='Runtime not ready')
        try:
            result = await stream_mgr.validate(payload)
            radio._push_event('info' if result.get('ok') else 'error', 'Check streaming', str(result.get('reason') or 'validation completed'))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            detail = str(exc)
            status = 503 if 'ffmpeg not found' in detail.lower() else 500
            raise HTTPException(status_code=status, detail=detail)
        except Exception as exc:
            logger.exception('AceRadio stream validate failed')
            raise HTTPException(status_code=500, detail=str(exc))
        return result

    @app.post('/api/stream/stop')
    async def stream_stop():
        target_url=str((stream_mgr.status() or {}).get('target_url') or '')
        await stream_mgr.stop()
        radio._push_event('info', 'Stream stopped', target_url)
        return {'ok': True, 'running': stream_mgr.running, 'status': stream_mgr.status()}

    @app.get('/api/stream/status')
    async def stream_status_alias():
        return stream_mgr.status()

    @app.get('/api/jingles/status')
    async def jingles_status():
        jmgr: JingleManager = app.state.jingle_mgr
        jmgr.reload()
        state = jmgr.runtime_state()
        state['songs_since_overlay'] = radio.songs_since_overlay
        state['songs_since_separator'] = radio.songs_since_separator
        state['jingle_event'] = radio._jingle_event
        return state

    @app.post('/api/jingles/config')
    async def jingles_save_config(payload: dict):
        jmgr: JingleManager = app.state.jingle_mgr
        filename = str((payload or {}).get('filename', '')).strip()
        mode     = str((payload or {}).get('mode', '')).strip()
        updates  = dict((payload or {}).get('updates', {}) or {})
        if not filename or mode not in ('overlay', 'separator'):
            raise HTTPException(status_code=400, detail='filename and valid mode required')
        result = jmgr.update_jingle(filename, mode, updates)
        if result is None:
            raise HTTPException(status_code=404, detail='Jingle not found')
        return {'ok': True, 'updated': result}

    @app.post('/api/jingles/reload')
    async def jingles_reload():
        jmgr: JingleManager = app.state.jingle_mgr
        jmgr.reload()
        return {'ok': True, **jmgr.runtime_state()}

    @app.get('/api/jingles/audio/{mode}/{filename}')
    async def jingle_audio(mode: str, filename: str):
        import re as _re
        if mode not in ('overlay', 'separator'):
            raise HTTPException(status_code=400, detail='mode must be overlay or separator')
        if _re.search(r'[\\/]|\.\.', filename):
            raise HTTPException(status_code=400, detail='invalid filename')
        jmgr: JingleManager = app.state.jingle_mgr
        p = jmgr.audio_path(filename, mode)
        if not p or not p.exists():
            raise HTTPException(status_code=404, detail='Jingle audio not found')
        ext = p.suffix.lower()
        mime_map = {'.mp3':'audio/mpeg','.wav':'audio/wav','.flac':'audio/flac',
                    '.ogg':'audio/ogg','.opus':'audio/ogg','.aac':'audio/aac','.m4a':'audio/aac'}
        from fastapi.responses import FileResponse as _FR
        return _FR(str(p), media_type=mime_map.get(ext, 'audio/mpeg'), filename=filename)

    @app.post('/api/jingles/play/overlay')
    async def jingle_play_overlay(payload: dict = {}):
        jmgr: JingleManager = app.state.jingle_mgr
        if radio._jingle_event and radio._jingle_event.get('status') == 'active':
            raise HTTPException(status_code=409,
                                detail='Jingle already active — wait for it to end or stop it first')
        p_dict = payload or {}
        filename = str(p_dict.get('filename', '')).strip()
        launch_volume: Optional[float] = None
        raw_vol = p_dict.get('volume')
        if raw_vol is not None:
            try:
                launch_volume = max(0.0, min(1.0, float(raw_vol)))
            except (TypeError, ValueError):
                pass
        if filename:
            jingle = jmgr.get_jingle(filename, 'overlay')
            if not jingle:
                raise HTTPException(status_code=404, detail='Overlay jingle not found')
        else:
            candidates = [j for j in jmgr.all_jingles()
                          if j.get('mode') == 'overlay' and j.get('enabled', True)]
            if not candidates:
                raise HTTPException(status_code=404, detail='No enabled overlay jingles')
            import random as _rnd
            jingle = _rnd.choice(candidates)
        p = jmgr.audio_path(jingle['filename'], 'overlay')
        if not p:
            raise HTTPException(status_code=404, detail='Overlay audio file missing from disk')
        radio._fire_jingle_event(jingle, 'overlay', launch_volume=launch_volume)
        return {'ok': True, 'event': radio._jingle_event}

    @app.post('/api/jingles/play/separator')
    async def jingle_play_separator(payload: dict = {}):
        jmgr: JingleManager = app.state.jingle_mgr
        if radio._jingle_event and radio._jingle_event.get('status') == 'active':
            raise HTTPException(status_code=409,
                                detail='Jingle already active — wait for it to end or stop it first')
        p_dict = payload or {}
        filename = str(p_dict.get('filename', '')).strip()
        launch_volume: Optional[float] = None
        raw_vol = p_dict.get('volume')
        if raw_vol is not None:
            try:
                launch_volume = max(0.0, min(1.0, float(raw_vol)))
            except (TypeError, ValueError):
                pass
        if filename:
            jingle = jmgr.get_jingle(filename, 'separator')
            if not jingle:
                raise HTTPException(status_code=404, detail='Separator jingle not found')
        else:
            candidates = [j for j in jmgr.all_jingles()
                          if j.get('mode') == 'separator' and j.get('enabled', True)]
            if not candidates:
                raise HTTPException(status_code=404, detail='No enabled separator jingles')
            import random as _rnd
            jingle = _rnd.choice(candidates)
        p = jmgr.audio_path(jingle['filename'], 'separator')
        if not p:
            raise HTTPException(status_code=404, detail='Separator audio file missing from disk')

        transition_now = bool(radio.current_track and (radio.next_track is not None or len(radio.reservoir) > 0))
        if transition_now:
            radio._separator_transition_pending = True
        radio._fire_jingle_event(
            jingle,
            'separator',
            launch_volume=launch_volume,
            is_transition=transition_now,
        )
        return {'ok': True, 'event': radio._jingle_event, 'transition': transition_now}

    @app.post('/api/jingles/confirm')
    async def jingle_confirm(payload: dict = {}):
        event_id = str((payload or {}).get('event_id', '')).strip()
        phase    = str((payload or {}).get('phase', 'started')).strip()
        if not event_id:
            raise HTTPException(status_code=400, detail='event_id required')
        ev = radio._jingle_event
        if not ev or ev.get('event_id') != event_id:
            return {'ok': True, 'detail': 'event not found or expired'}
        if phase == 'started' and not ev.get('confirmed'):
            ev['confirmed']    = True
            ev['confirmed_at'] = time.time()
            try:
                jmgr: JingleManager = app.state.jingle_mgr
                jmgr.record_played(ev['filename'], ev['mode'])
            except Exception:
                pass
        if phase == 'ended':
            ev['status']   = 'ended'
            ev['ended_at'] = time.time()
            if ev.get('is_transition') and radio._separator_transition_pending:
                logger.info('[AceRadio] transition separator ended — completing deferred advance')
                radio._separator_transition_pending = False
                await radio._advance_rotation()
        return {'ok': True, 'event_id': event_id, 'phase': phase,
                'current_track': radio.payload(radio.current_track) if radio.current_track else None}

    @app.post('/api/jingles/stop')
    async def jingle_stop():
        ev = radio._jingle_event
        if not ev or ev.get('status') != 'active':
            return {'ok': True, 'detail': 'no active jingle to stop'}
        ev['status']   = 'ended'
        ev['ended_at'] = time.time()
        return {'ok': True, 'stopped': ev.get('event_id')}

    @app.post('/api/jingles/queue-separator')
    async def jingle_queue_separator(payload: dict = {}):
        jmgr: JingleManager = app.state.jingle_mgr
        filename = str((payload or {}).get('filename', '')).strip()
        if not filename:
            raise HTTPException(status_code=400, detail='filename required')
        if filename == '__clear__':
            radio._queued_separator = None
            return {'ok': True, 'queued': None}
        jingle = jmgr.get_jingle(filename, 'separator')
        if not jingle:
            raise HTTPException(status_code=404, detail='Separator jingle not found')
        if not jmgr.audio_path(filename, 'separator'):
            raise HTTPException(status_code=404, detail='Separator audio missing from disk')
        radio._queued_separator = jingle
        return {'ok': True, 'queued': filename}

    @app.get('/api/jingles/list')
    async def jingle_list():
        jmgr: JingleManager = app.state.jingle_mgr
        jmgr.reload()
        all_j = jmgr.all_jingles()
        return {
            'overlay':          [j for j in all_j if j.get('mode') == 'overlay'],
            'separator':        [j for j in all_j if j.get('mode') == 'separator'],
            'jingle_event':     radio._jingle_event,
            'queued_separator': getattr(radio, '_queued_separator', None),
        }

    return app
