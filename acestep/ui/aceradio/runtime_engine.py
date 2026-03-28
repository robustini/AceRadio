
from __future__ import annotations
import ast
import asyncio
import functools
import inspect
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import hmac
import hashlib
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

class _TeeStream:

    def __init__(self, original, capture_fp):
        self._original = original
        self._capture_fp = capture_fp
        self._capture_buffer = ""

    def write(self, data):
        if data is None:
            return 0
        written = 0
        try:
            written = self._original.write(data)
        except Exception:
            pass
        try:
            chunk = str(data)
            self._capture_buffer += chunk
            parts = re.split(r'(\r|\n)', self._capture_buffer)
            if len(parts) == 1:
                return written
            self._capture_buffer = ''
            assembled = []
            current = ''
            for part in parts:
                if part in ('\r', '\n'):
                    line = current
                    current = ''
                    if self._should_capture_cli_line(line):
                        assembled.append(line + part)
                else:
                    current += part
            self._capture_buffer = current
            if assembled:
                self._capture_fp.write(''.join(assembled))
                self._capture_fp.flush()
        except Exception:
            pass
        return written

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            if self._capture_buffer:
                self._capture_fp.write(self._capture_buffer)
                self._capture_buffer = ''
            self._capture_fp.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._original.isatty())
        except Exception:
            return False

    def _should_capture_cli_line(self, line):
        return True

    @property
    def encoding(self):
        try:
            return self._original.encoding
        except Exception:
            return 'utf-8'

from acestep.handler import AceStepHandler
from acestep.llm_inference import LLMHandler
from acestep.inference import generate_music, GenerationParams, GenerationConfig
from acestep.constants import TASK_TYPES_BASE, TASK_TYPES_TURBO, VALID_LANGUAGES
from .queue import InProcessJobQueue
import subprocess
import shlex

ACERADIO_MP3_BITRATE_OPTIONS = ("128k", "192k", "256k", "320k")
ACERADIO_MP3_SAMPLE_RATE_OPTIONS = (48000, 44100)
ACERADIO_MP3_DEFAULT_BITRATE = "128k"
ACERADIO_MP3_DEFAULT_SAMPLE_RATE = 48000

def _normalize_mp3_export_request(requested_format, requested_bitrate, requested_sample_rate):
    audio_format = str(requested_format or 'flac').strip().lower()
    if audio_format not in ('mp3', 'wav', 'flac', 'wav32', 'opus', 'aac'):
        audio_format = 'flac'
    mp3_bitrate = str(requested_bitrate or ACERADIO_MP3_DEFAULT_BITRATE).strip().lower()
    if mp3_bitrate not in ACERADIO_MP3_BITRATE_OPTIONS:
        mp3_bitrate = ACERADIO_MP3_DEFAULT_BITRATE
    try:
        mp3_sample_rate = int(requested_sample_rate or ACERADIO_MP3_DEFAULT_SAMPLE_RATE)
    except Exception:
        mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    if mp3_sample_rate not in ACERADIO_MP3_SAMPLE_RATE_OPTIONS:
        mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    if audio_format != 'mp3':
        mp3_bitrate = ACERADIO_MP3_DEFAULT_BITRATE
        mp3_sample_rate = ACERADIO_MP3_DEFAULT_SAMPLE_RATE
    return audio_format, mp3_bitrate, mp3_sample_rate

def _log_export_request(prefix: str, requested_format, requested_bitrate, requested_sample_rate, final_format: str, final_bitrate: str, final_sample_rate: int) -> None:
    try:
        logger.info(
            f"{prefix} export request: requested=(format={requested_format!r}, bitrate={requested_bitrate!r}, rate={requested_sample_rate!r}) "
            f"engine_rate=48000Hz -> final=(format={final_format!r}, bitrate={final_bitrate!r}, rate={final_sample_rate}Hz)"
        )
    except Exception:
        pass

def _resolve_binary(name: str) -> str:
    direct = shutil.which(name)
    if direct:
        return direct
    if os.name == 'nt' and not name.lower().endswith('.exe'):
        direct = shutil.which(name + '.exe')
        if direct:
            return direct
    return name

def _ffprobe_audio_stream(path: str) -> dict:
    target = str(path or '').strip()
    if not target or not os.path.exists(target):
        return {}
    cmd = [
        _resolve_binary('ffprobe'),
        '-v', 'error',
        '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name,sample_rate,bit_rate',
        '-of', 'json',
        target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        logger.warning('[ffprobe] failed path=%s err=%s', target, exc)
        return {}
    if proc.returncode != 0:
        stderr = (proc.stderr or '').strip()[:1000]
        logger.warning('[ffprobe] non-zero exit path=%s rc=%s stderr=%s', target, proc.returncode, stderr)
        return {}
    try:
        payload = json.loads(proc.stdout or '{}')
        streams = payload.get('streams') or []
        stream = streams[0] if streams else {}
        sample_rate = stream.get('sample_rate')
        bit_rate = stream.get('bit_rate')
        return {
            'codec': str(stream.get('codec_name') or ''),
            'sample_rate': int(sample_rate) if str(sample_rate).strip() else 0,
            'bit_rate': int(bit_rate) if str(bit_rate).strip() else 0,
        }
    except Exception as exc:
        logger.warning('[ffprobe] parse failed path=%s err=%s', target, exc)
        return {}

def _ensure_mp3_export(original_path: str, mp3_bitrate: str, mp3_sample_rate: int):
    original = str(original_path or '').strip()
    if not original:
        raise RuntimeError('MP3 export path mancante')
    original_p = Path(original)
    if not original_p.exists():
        raise RuntimeError(f'MP3 export source non trovato: {original}')
    target = original_p.with_suffix('.mp3')
    if original_p.suffix.lower() == '.mp3':
        target = original_p.with_name(f'{original_p.stem}.tmp_reencode.mp3')
    cmd = [
        _resolve_binary('ffmpeg'),
        '-y',
        '-i', str(original_p),
        '-vn',
        '-ar', str(int(mp3_sample_rate)),
        '-b:a', str(mp3_bitrate),
        '-codec:a', 'libmp3lame',
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not target.exists():
        stderr = (proc.stderr or '').strip()[:4000]
        raise RuntimeError(f'MP3 export ffmpeg failed rc={proc.returncode}: {stderr}')
    if original_p.suffix.lower() == '.mp3':
        backup = original_p.with_name(f'{original_p.stem}.pre_reencode.mp3')
        try:
            if backup.exists():
                backup.unlink()
        except Exception:
            pass
        os.replace(str(original_p), str(backup))
        os.replace(str(target), str(original_p))
        try:
            backup.unlink()
        except Exception:
            pass
        target = original_p
    probe = _ffprobe_audio_stream(str(target))
    return str(target), probe

def _model_name_lower(model_name: Optional[str]) -> str:

    return str(model_name or "").strip().lower()

def _is_sft_model(model_name: Optional[str]) -> bool:

    return "sft" in _model_name_lower(model_name)

def _is_base_model(model_name: Optional[str]) -> bool:

    return "base" in _model_name_lower(model_name)

def _is_turbo_model(model_name: Optional[str]) -> bool:

    name_lower = _model_name_lower(model_name)
    return ("turbo" in name_lower) and (not _is_sft_model(name_lower))

def _parse_lora_weight_value(value: Any, default: float = 0.5) -> float:

    if isinstance(value, (int, float)):
        try:
            n = float(value)
        except Exception:
            return default
    else:
        raw = str(value or '').strip()
        if not raw:
            return default
        raw = re.sub(r"[\s\u00A0\u202F]+", "", raw)
        raw = re.sub(r"[^0-9,\.\-\+]", "", raw)
        if not raw or raw in {'-', '+', '.', ',', '-.', '-,', '+.', '+,'}:
            return default
        last_comma = raw.rfind(',')
        last_dot = raw.rfind('.')
        if last_comma >= 0 and last_dot >= 0:
            if last_comma > last_dot:
                raw = raw.replace('.', '').replace(',', '.')
            else:
                raw = raw.replace(',', '')
        elif last_comma >= 0:
            raw = raw.replace('.', '').replace(',', '.')
        try:
            n = float(raw)
        except Exception:
            return default
    if n != n:
        return default
    return max(0.0, min(n, 2.0))

def _parse_timesteps_input(value):

    if value is None:
        return None
    if isinstance(value, list):
        if all(isinstance(t, (int, float)) for t in value):
            return [float(t) for t in value]
        return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("[") or raw.startswith("("):
        try:
            parsed = ast.literal_eval(raw)
        except Exception:
            return None
        if isinstance(parsed, list) and all(isinstance(t, (int, float)) for t in parsed):
            return [float(t) for t in parsed]
        return None
    try:
        return [float(t.strip()) for t in raw.split(",") if t.strip()]
    except Exception:
        return None

_CHORD_NOTE_INDEX = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
_CHORD_NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_CHORD_NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
def _is_peft_like(obj: Any) -> bool:

    if obj is None:
        return False
    if hasattr(obj, "peft_config") or hasattr(obj, "active_adapters") or hasattr(obj, "active_adapter"):
        return True
    if hasattr(obj, "get_base_model") or hasattr(obj, "disable_adapter") or hasattr(obj, "set_adapter"):
        return True
    mod = getattr(obj.__class__, "__module__", "") or ""
    name = getattr(obj.__class__, "__name__", "") or ""
    return ("peft" in mod.lower()) or name.lower().startswith("peft")

def _strip_peft_attributes(model: Any) -> None:

    if model is None:
        return
    for attr in (
        "peft_config",
        "active_adapter",
        "active_adapters",
        "peft_type",
        "base_model",
        "modules_to_save",
        "prompt_encoder",
        "_hf_peft_config_loaded",
    ):
        if hasattr(model, attr):
            try:
                delattr(model, attr)
            except Exception:
                try:
                    setattr(model, attr, None)
                except Exception:
                    pass

def _unwrap_peft(model: Any) -> Any:

    m = model
    if m is None:
        return m
    tuner = getattr(m, "base_model", None)
    tuner_unload = getattr(tuner, "unload", None)
    if callable(tuner_unload):
        try:
            unloaded = tuner_unload()
            if unloaded is not None:
                m = unloaded
        except Exception:
            pass
    unload_fn = getattr(m, "unload", None)
    if callable(unload_fn):
        try:
            unloaded = unload_fn()
            if unloaded is not None:
                m = unloaded
        except Exception:
            pass
    for _ in range(6):
        if not _is_peft_like(m):
            break
        get_base = getattr(m, "get_base_model", None)
        if callable(get_base):
            try:
                m2 = get_base()
                if m2 is not None and m2 is not m:
                    m = m2
                    continue
            except Exception:
                pass
        base_model = getattr(m, "base_model", None)
        inner = getattr(base_model, "model", None) if base_model is not None else None
        if inner is not None and inner is not m:
            m = inner
            continue
        break
    _strip_peft_attributes(model)
    if m is not model:
        _strip_peft_attributes(m)
    return m

def _best_effort_release_runtime_value(value: Any) -> None:

    if value is None:
        return
    try:
        inner = getattr(value, "base_model", None)
        unload_inner = getattr(inner, "unload", None)
        if callable(unload_inner):
            try:
                unload_inner()
            except Exception:
                pass
    except Exception:
        pass
    try:
        unload_fn = getattr(value, "unload", None)
        if callable(unload_fn):
            try:
                unload_fn()
            except Exception:
                pass
    except Exception:
        pass
    try:
        disable_one = getattr(value, "disable_adapter", None)
        if callable(disable_one):
            try:
                disable_one()
            except Exception:
                pass
        disable_many = getattr(value, "disable_adapters", None)
        if callable(disable_many):
            try:
                disable_many()
            except Exception:
                pass
    except Exception:
        pass
    try:
        cpu_fn = getattr(value, "cpu", None)
        if callable(cpu_fn):
            try:
                cpu_fn()
            except Exception:
                pass
    except Exception:
        pass

def _restore_decoder_state_dict(decoder_model: Any, backup_sd: dict) -> Any:

    try:
        return decoder_model.load_state_dict(backup_sd, strict=False)
    except Exception:
        pass
    model_keys = set(decoder_model.state_dict().keys())
    remapped = {}
    for k, v in backup_sd.items():
        if k in model_keys:
            remapped[k] = v
            continue
        if isinstance(k, str) and k.endswith(".weight"):
            alt = k[:-7] + ".base_layer.weight"
            if alt in model_keys:
                remapped[alt] = v
                continue
        remapped[k] = v
    return decoder_model.load_state_dict(remapped, strict=False)

def _cleanup_lora_runtime_memory() -> None:

    try:
        import gc
        gc.collect()
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass

def _collect_lora_runtime_state(handler) -> dict:

    state = {
        "lora_loaded": bool(getattr(handler, "lora_loaded", False)),
        "use_lora": bool(getattr(handler, "use_lora", False)),
        "adapter_type": getattr(handler, "_adapter_type", None),
        "active_adapter": None,
        "active_loras": [],
        "registry_keys": [],
        "peft_adapters": [],
        "decoder_is_peft": False,
        "has_lycoris": False,
    }
    try:
        active = getattr(handler, "_active_loras", None)
        if isinstance(active, dict):
            state["active_loras"] = sorted(str(k) for k in active.keys())
        elif active:
            state["active_loras"] = [str(active)]
    except Exception:
        pass
    try:
        registry = getattr(handler, "_lora_adapter_registry", None)
        if isinstance(registry, dict):
            state["registry_keys"] = sorted(str(k) for k in registry.keys())
    except Exception:
        pass
    decoder = getattr(getattr(handler, "model", None), "decoder", None)
    try:
        state["decoder_is_peft"] = bool(_is_peft_like(decoder))
    except Exception:
        pass
    try:
        state["has_lycoris"] = getattr(decoder, "_lycoris_net", None) is not None
    except Exception:
        pass
    try:
        active_adapter = getattr(handler, "_lora_active_adapter", None)
        if not active_adapter:
            svc = getattr(handler, "_lora_service", None)
            active_adapter = getattr(svc, "active_adapter", None)
        state["active_adapter"] = active_adapter
    except Exception:
        pass
    try:
        if _is_peft_like(decoder):
            names = []
            peft_cfg = getattr(decoder, "peft_config", None)
            if isinstance(peft_cfg, dict):
                names.extend(list(peft_cfg.keys()))
            list_fn = getattr(decoder, "list_adapters", None)
            if callable(list_fn):
                try:
                    listed = list_fn()
                    if isinstance(listed, dict):
                        for _, vals in listed.items():
                            if isinstance(vals, (list, tuple, set)):
                                names.extend(list(vals))
                    elif isinstance(listed, (list, tuple, set)):
                        names.extend(list(listed))
                    elif listed:
                        names.append(str(listed))
                except Exception:
                    pass
            state["peft_adapters"] = sorted(dict.fromkeys(str(n) for n in names if n))
    except Exception:
        pass
    return state

def _format_lora_runtime_state(handler) -> str:

    state = _collect_lora_runtime_state(handler)
    return (
        f"loaded={state['lora_loaded']} use_lora={state['use_lora']} "
        f"adapter_type={state['adapter_type']!r} active_adapter={state['active_adapter']!r} "
        f"active_loras={state['active_loras']} registry={state['registry_keys']} "
        f"peft_adapters={state['peft_adapters']} decoder_is_peft={state['decoder_is_peft']} "
        f"lycoris={state['has_lycoris']}"
    )

def _install_lora_runtime_patch() -> None:

    if getattr(AceStepHandler, "_aceradio_lora_runtime_patch", False):
        return
    original_add_lora = getattr(AceStepHandler, "add_lora", None)
    original_unload_lora = getattr(AceStepHandler, "unload_lora", None)
    if not callable(original_add_lora) or not callable(original_unload_lora):
        logger.warning("[AceRadio LoRA] runtime patch skipped: handler methods not found")
        return

    def patched_unload_lora(self) -> str:

        if getattr(self, "_base_decoder", None) is None:
            return original_unload_lora(self)
        decoder = getattr(getattr(self, "model", None), "decoder", None)
        has_active = bool(getattr(self, "lora_loaded", False) or (getattr(self, "_active_loras", None) or {}))
        has_lycoris = getattr(decoder, "_lycoris_net", None) is not None
        if (not has_active) and (not has_lycoris) and (not _is_peft_like(decoder)):
            logger.info(f"[AceRadio LoRA] state before unload (noop): {_format_lora_runtime_state(self)}")
            return "⚠️ No LoRA adapter loaded."
        try:
            mem_before = None
            if hasattr(self, "_memory_allocated"):
                try:
                    mem_before = self._memory_allocated() / (1024**3)
                    logger.info(f"[AceRadio LoRA] VRAM before unload: {mem_before:.2f}GB")
                except Exception:
                    mem_before = None
            logger.info(f"[AceRadio LoRA] state before unload: {_format_lora_runtime_state(self)}")
            lycoris_net = getattr(self.model.decoder, "_lycoris_net", None)
            if lycoris_net is not None:
                restore_fn = getattr(lycoris_net, "restore", None)
                if callable(restore_fn):
                    logger.info("[AceRadio LoRA] restoring decoder structure from LyCORIS adapter")
                    restore_fn()
                self.model.decoder._lycoris_net = None
            peft_decoder = self.model.decoder
            is_peft = _is_peft_like(peft_decoder)
            if is_peft:
                logger.info("[AceRadio LoRA] unloading PEFT adapters")
                try:
                    disable_one = getattr(peft_decoder, "disable_adapter", None)
                    if callable(disable_one):
                        disable_one()
                    disable_many = getattr(peft_decoder, "disable_adapters", None)
                    if callable(disable_many):
                        disable_many()
                except Exception:
                    pass
                base_model = None
                unload_fn = getattr(peft_decoder, "unload", None)
                if callable(unload_fn):
                    try:
                        base_model = unload_fn()
                    except Exception as exc:
                        logger.warning(f"[AceRadio LoRA] PEFT unload() failed, falling back to delete_adapter(): {exc!r}")
                if base_model is None:
                    try:
                        names = []
                        peft_cfg = getattr(peft_decoder, "peft_config", None)
                        if isinstance(peft_cfg, dict):
                            names.extend(list(peft_cfg.keys()))
                        list_fn = getattr(peft_decoder, "list_adapters", None)
                        if callable(list_fn):
                            try:
                                names.extend(list(list_fn()))
                            except Exception:
                                pass
                        names = list(dict.fromkeys([n for n in names if isinstance(n, str) and n]))
                        delete_fn = getattr(peft_decoder, "delete_adapter", None)
                        if callable(delete_fn):
                            for name in names:
                                try:
                                    delete_fn(name)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                try:
                    base_model = _unwrap_peft(peft_decoder)
                except Exception:
                    base_model = peft_decoder
                if _is_peft_like(base_model):
                    try:
                        bm = getattr(peft_decoder, "base_model", None)
                        bm = getattr(bm, "model", bm)
                        if bm is not None:
                            base_model = bm
                    except Exception:
                        pass
                _strip_peft_attributes(peft_decoder)
                _strip_peft_attributes(base_model)
                self.model.decoder = base_model
                load_result = _restore_decoder_state_dict(self.model.decoder, self._base_decoder)
            else:
                logger.info("[AceRadio LoRA] restoring base decoder from state_dict backup")
                load_result = _restore_decoder_state_dict(self.model.decoder, self._base_decoder)
            try:
                self.model.decoder = self.model.decoder.to(self.device).to(self.dtype)
                self.model.decoder.eval()
            except Exception:
                pass
            self.lora_loaded = False
            self.use_lora = False
            self._adapter_type = None
            self.lora_scale = 1.0
            active = getattr(self, "_active_loras", None)
            if active is not None:
                try:
                    active.clear()
                except Exception:
                    pass
            try:
                self._ensure_lora_registry()
                self._lora_service.registry = {}
                self._lora_service.scale_state = {}
                self._lora_service.active_adapter = None
                self._lora_service.last_scale_report = {}
            except Exception:
                pass
            try:
                self._lora_adapter_registry = {}
                self._lora_active_adapter = None
                self._lora_scale_state = {}
            except Exception:
                pass
            if getattr(load_result, "missing_keys", None):
                logger.warning(f"[AceRadio LoRA] missing keys when restoring decoder: {load_result.missing_keys[:5]}")
            if getattr(load_result, "unexpected_keys", None):
                logger.warning(f"[AceRadio LoRA] unexpected keys when restoring decoder: {load_result.unexpected_keys[:5]}")
            _cleanup_lora_runtime_memory()
            if mem_before is not None and hasattr(self, "_memory_allocated"):
                try:
                    mem_after = self._memory_allocated() / (1024**3)
                    logger.info(f"[AceRadio LoRA] VRAM after unload: {mem_after:.2f}GB (freed: {mem_before - mem_after:.2f}GB)")
                except Exception:
                    pass
            logger.info(f"[AceRadio LoRA] state after unload: {_format_lora_runtime_state(self)}")
            logger.info("[AceRadio LoRA] unload complete; base decoder restored")
            return "✅ LoRA unloaded, using base model"
        except Exception as exc:
            logger.exception("[AceRadio LoRA] robust unload failed; falling back to upstream unload")
            try:
                return original_unload_lora(self)
            finally:
                _cleanup_lora_runtime_memory()

    def patched_add_lora(self, lora_path: str, adapter_name: Optional[str] = None) -> str:

        logger.info(f"[AceRadio LoRA] state before load request: {_format_lora_runtime_state(self)}")
        decoder = getattr(getattr(self, "model", None), "decoder", None)
        needs_cleanup = bool(
            getattr(self, "lora_loaded", False)
            or (getattr(self, "_active_loras", None) or {})
            or _is_peft_like(decoder)
            or getattr(decoder, "_lycoris_net", None) is not None
        )
        if needs_cleanup:
            try:
                cleanup_msg = patched_unload_lora(self)
                logger.info(f"[AceRadio LoRA] pre-load cleanup: {cleanup_msg}")
            except Exception as exc:
                logger.warning(f"[AceRadio LoRA] pre-load cleanup failed (continuing): {exc!r}")
        decoder = getattr(getattr(self, "model", None), "decoder", None)
        if _is_peft_like(decoder):
            try:
                base_model = _unwrap_peft(decoder)
                _strip_peft_attributes(base_model)
                self.model.decoder = base_model
            except Exception:
                pass
        result = original_add_lora(self, lora_path, adapter_name)
        logger.info(f"[AceRadio LoRA] state after load: {_format_lora_runtime_state(self)}")
        return result
    AceStepHandler.unload_lora = patched_unload_lora
    AceStepHandler.add_lora = patched_add_lora
    AceStepHandler._aceradio_lora_runtime_patch = True
    logger.info("[AceRadio LoRA] runtime patch enabled (single-LoRA policy, upstream lifecycle untouched)")

def _query_nvidia_smi() -> Optional[dict]:

    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
            "--id=0",
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=1.5)
        line = out.decode("utf-8", errors="replace").strip().splitlines()[0].strip()
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return None
        name = parts[0]
        used = int(float(parts[1]))
        total = int(float(parts[2]))
        temp = None
        try:
            if len(parts) >= 4 and parts[3] != "":
                temp = int(float(parts[3]))
        except Exception:
            temp = None
        return {
            "gpu_name": name,
            "vram_used_mb": used,
            "vram_total_mb": total,
            "gpu_temp_c": temp,
        }
    except Exception:
        return None

def _get_gpu_info_cached(app: FastAPI, ttl_seconds: float = 1.0) -> Optional[dict]:

    now = time.time()
    cache = getattr(app.state, "_gpu_cache", None)
    if cache and (now - cache.get("ts", 0.0)) < ttl_seconds:
        return cache.get("val")
    val = _query_nvidia_smi()
    app.state._gpu_cache = {"ts": now, "val": val}
    return val

_UUID_RE = re.compile(

    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

)

def is_job_dir(p: Path) -> bool:

    try:
        if not p.is_dir():
            return False
        if p.is_symlink():
            return False
        if not _UUID_RE.match(p.name or ""):
            return False
        if (p / "metadata.json").is_file():
            return True
        for ext in (".wav", ".mp3", ".flac", ".opus", ".aac"):
            try:
                if any(p.glob(f"*{ext}")):
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False

def cleanup_old_job_dirs(base_dir: Path, ttl_seconds: int | None = None) -> dict:

    if ttl_seconds is None:
        ttl_seconds = RUNTIME_DEFAULT_CLEANUP_TTL_SECONDS
    report = {"scanned": 0, "deleted": 0, "skipped": 0, "errors": 0}
    try:
        base_resolved = base_dir.resolve()
    except Exception:
        report["errors"] += 1
        return report
    try:
        if not getattr(cleanup_old_job_dirs, "_logged_base", False):
            logger.info("[cleanup] base={}", str(base_resolved))
            setattr(cleanup_old_job_dirs, "_logged_base", True)
    except Exception:
        pass
    now = time.time()
    try:
        for child in base_dir.iterdir():
            report["scanned"] += 1
            try:
                if not child.is_dir():
                    report["skipped"] += 1
                    continue
                if child.is_symlink():
                    report["skipped"] += 1
                    continue
                try:
                    child_resolved = child.resolve()
                except Exception:
                    report["skipped"] += 1
                    continue
                try:
                    if not child_resolved.is_relative_to(base_resolved):
                        report["skipped"] += 1
                        continue
                except AttributeError:
                    if not str(child_resolved).startswith(str(base_resolved) + os.sep):
                        report["skipped"] += 1
                        continue
                if child_resolved == base_resolved:
                    report["skipped"] += 1
                    continue
                if not is_job_dir(child):
                    report["skipped"] += 1
                    continue
                try:
                    mtime = float(child.stat().st_mtime)
                except Exception:
                    report["skipped"] += 1
                    continue
                if (now - mtime) <= float(ttl_seconds):
                    report["skipped"] += 1
                    continue
                try:
                    shutil.rmtree(child)
                    report["deleted"] += 1
                except Exception as exc:
                    report["errors"] += 1
                    logger.warning("[cleanup] failed path={} err={!r}", str(child), exc)
            except Exception as exc:
                report["errors"] += 1
                logger.warning("[cleanup] scan failed path={} err={!r}", str(child), exc)
    except Exception as exc:
        report["errors"] += 1
        logger.warning("[cleanup] iterdir failed base={} err={!r}", str(base_dir), exc)
    return report

def cleanup_old_log_files(logs_dir: Path, ttl_seconds: int | None = None) -> dict:

    if ttl_seconds is None:
        ttl_seconds = RUNTIME_DEFAULT_CLEANUP_TTL_SECONDS
    report = {"scanned": 0, "deleted": 0, "skipped": 0, "errors": 0}
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        base_resolved = logs_dir.resolve()
    except Exception:
        report["errors"] += 1
        return report
    now = time.time()
    try:
        for child in logs_dir.iterdir():
            report["scanned"] += 1
            try:
                if not child.is_file():
                    report["skipped"] += 1
                    continue
                if child.is_symlink():
                    report["skipped"] += 1
                    continue
                try:
                    child_resolved = child.resolve()
                except Exception:
                    report["skipped"] += 1
                    continue
                try:
                    if not child_resolved.is_relative_to(base_resolved):
                        report["skipped"] += 1
                        continue
                except AttributeError:
                    if not str(child_resolved).startswith(str(base_resolved) + os.sep):
                        report["skipped"] += 1
                        continue
                try:
                    mtime = float(child.stat().st_mtime)
                except Exception:
                    report["skipped"] += 1
                    continue
                if (now - mtime) <= float(ttl_seconds):
                    report["skipped"] += 1
                    continue
                try:
                    child.unlink()
                    report["deleted"] += 1
                except Exception as exc:
                    report["errors"] += 1
                    logger.warning("[cleanup_logs] failed path={} err={!r}", str(child), exc)
            except Exception as exc:
                report["errors"] += 1
                logger.warning("[cleanup_logs] scan failed path={} err={!r}", str(child), exc)
    except Exception as exc:
        report["errors"] += 1
        logger.warning("[cleanup_logs] iterdir failed base={} err={!r}", str(logs_dir), exc)
    return report

def _get_project_root() -> str:

    p = Path(__file__).resolve()
    return str(p.parents[3])

def _get_checkpoint_dir(project_root: str) -> str:

    return os.path.join(project_root, "checkpoints")

def _read_model_supported_tasks(checkpoint_dir: str, model_name: str) -> List[str]:

    config_file = os.path.join(checkpoint_dir, str(model_name or '').strip(), 'config.json')
    if not os.path.isfile(config_file):
        return list(TASK_TYPES_BASE)
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if bool(config.get('is_turbo', False)):
            return list(TASK_TYPES_TURBO)
    except Exception:
        return list(TASK_TYPES_BASE)
    return list(TASK_TYPES_BASE)

def _collect_dit_model_inventory(project_root: str, active_model: str = '', default_model: str = '', loaded_models: Optional[set[str]] = None) -> List[Dict[str, Any]]:

    checkpoint_dir = _get_checkpoint_dir(project_root)
    loaded = {str(x or '').strip() for x in (loaded_models or set()) if str(x or '').strip()}
    available: set[str] = set(loaded)
    if str(active_model or '').strip():
        available.add(str(active_model).strip())
    if str(default_model or '').strip():
        available.add(str(default_model).strip())
    if os.path.isdir(checkpoint_dir):
        for name in os.listdir(checkpoint_dir):
            model_name = str(name or '').strip()
            full_path = os.path.join(checkpoint_dir, model_name)
            if not model_name or not os.path.isdir(full_path):
                continue
            if not model_name.startswith('acestep-') or model_name.startswith('acestep-5Hz-lm-'):
                continue
            available.add(model_name)
    inventory = []
    for model_name in sorted(available):
        supported_task_types = _read_model_supported_tasks(checkpoint_dir, model_name)
        inventory.append({
            'name': model_name,
            'is_default': bool(model_name and model_name == str(default_model or '').strip()),
            'is_loaded': model_name in loaded,
            'supported_task_types': supported_task_types,
            'supports_radio': 'text2music' in supported_task_types,
        })
    return inventory

def _collect_radio_model_inventory(project_root: str, active_model: str = '', default_model: str = '', loaded_models: Optional[set[str]] = None) -> List[Dict[str, Any]]:

    return [entry for entry in _collect_dit_model_inventory(project_root, active_model, default_model, loaded_models) if bool(entry.get('supports_radio'))]

def _collect_radio_model_names(project_root: str, active_model: str = '', default_model: str = '', loaded_models: Optional[set[str]] = None) -> List[str]:

    return [str(entry.get('name') or '').strip() for entry in _collect_radio_model_inventory(project_root, active_model, default_model, loaded_models) if str(entry.get('name') or '').strip()]

def _env_int(name: str, default: int) -> int:

    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return default
        return int(float(v))
    except Exception:
        return default

def _env_flag(name: str, default: bool = False) -> bool:

    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}

RUNTIME_DEFAULT_CLEANUP_TTL_SECONDS = 0
RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO = 20
RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_BASE = 200
RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_SFT = 200
ACERADIO_TURBO_CLAMP_BYPASS_ENV = "ACERADIO_BYPASS_CORE_TURBO_STEP_CLAMP"
ACERADIO_CLEANUP_TTL_ENV = "ACERADIO_CLEANUP_TTL_SECONDS"

def _get_cleanup_ttl_seconds() -> int:

    ttl = _env_int(ACERADIO_CLEANUP_TTL_ENV, RUNTIME_DEFAULT_CLEANUP_TTL_SECONDS)
    return max(0, ttl)

def _is_core_turbo_step_clamp_bypass_enabled() -> bool:

    return _env_flag(ACERADIO_TURBO_CLAMP_BYPASS_ENV, False)

def _get_max_inference_steps_for_model(model_name: Optional[str]) -> int:

    if _is_sft_model(model_name):
        return RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_SFT
    if _is_base_model(model_name):
        return RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_BASE
    if _is_turbo_model(model_name):
        return RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO
    return RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO

RUNTIME_TURBO_VALID_TIMESTEPS = [
    1.0, 0.9545454545454546, 0.9333333333333333, 0.9, 0.875,
    0.8571428571428571, 0.8333333333333334, 0.7692307692307693, 0.75,
    0.6666666666666666, 0.6428571428571429, 0.625, 0.5454545454545454,
    0.5, 0.4, 0.375, 0.3, 0.25, 0.2222222222222222, 0.125,
]

def _get_turbo_timesteps_for_infer_steps(infer_steps: int) -> List[float]:

    steps = max(1, min(int(infer_steps), RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO))
    return RUNTIME_TURBO_VALID_TIMESTEPS[:steps]

def _get_callable_signature(fn):

    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None

def _bind_call_arguments(fn, self_obj, args, kwargs):

    signature = _get_callable_signature(fn)
    if signature is None:
        return None
    try:
        bound = signature.bind_partial(self_obj, *args, **kwargs)
    except TypeError:
        return None
    try:
        bound.apply_defaults()
    except Exception:
        pass
    return bound.arguments

def _get_bound_argument(fn, self_obj, args, kwargs, *names, default=None):

    bound_arguments = _bind_call_arguments(fn, self_obj, args, kwargs)
    if not bound_arguments:
        return default
    for name in names:
        if name in bound_arguments:
            return bound_arguments[name]
    return default

def _install_core_turbo_step_clamp_bypass_patch() -> bool:

    if not _is_core_turbo_step_clamp_bypass_enabled():
        return False
    try:
        from acestep.core.generation.handler.service_generate_request import ServiceGenerateRequestMixin
        from acestep.core.generation.handler.service_generate_execute import ServiceGenerateExecuteMixin
        import torch
    except Exception as exc:
        logger.warning("[AceRadio] could not import core turbo clamp target; bypass disabled err={!r}", exc)
        return False

    normalize_target = getattr(ServiceGenerateRequestMixin, "_normalize_service_generate_inputs", None)
    build_kwargs_target = getattr(ServiceGenerateExecuteMixin, "_build_service_generate_kwargs", None)
    if not callable(normalize_target) or not callable(build_kwargs_target):
        logger.warning("[AceRadio] core turbo clamp target methods not found; bypass disabled")
        return False

    normalize_signature = _get_callable_signature(normalize_target)
    build_kwargs_signature = _get_callable_signature(build_kwargs_target)
    if normalize_signature is None or build_kwargs_signature is None:
        logger.warning("[AceRadio] core turbo clamp target signatures unavailable; bypass disabled")
        return False

    normalize_parameters = normalize_signature.parameters
    build_kwargs_parameters = build_kwargs_signature.parameters
    if "infer_steps" not in normalize_parameters or "infer_steps" not in build_kwargs_parameters or "timesteps" not in build_kwargs_parameters:
        logger.warning(
            "[AceRadio] core turbo clamp target signatures changed normalize_params={} build_kwargs_params={}; runtime patch will stay inactive",
            list(normalize_parameters.keys()),
            list(build_kwargs_parameters.keys()),
        )
        return False

    if getattr(ServiceGenerateRequestMixin, "_aceradio_turbo_clamp_patch_installed", False) and getattr(ServiceGenerateExecuteMixin, "_aceradio_turbo_timestep_patch_installed", False):
        return True

    original_normalize = normalize_target
    original_build_kwargs = build_kwargs_target

    class _ConfigProxy:
        def __init__(self, config):
            self._config = config

        @property
        def is_turbo(self):
            return False

        def __getattr__(self, name):
            return getattr(self._config, name)

    class _HostProxy:
        def __init__(self, host):
            self._host = host
            self.config = _ConfigProxy(getattr(host, "config", None))

        def __getattr__(self, name):
            return getattr(self._host, name)

    @functools.wraps(original_normalize)
    def patched_normalize(self, *args, **kwargs):
        if not getattr(getattr(self, "config", None), "is_turbo", False):
            return original_normalize(self, *args, **kwargs)
        infer_steps = _get_bound_argument(original_normalize, self, args, kwargs, "infer_steps")
        try:
            infer_steps_int = int(infer_steps)
        except Exception:
            return original_normalize(self, *args, **kwargs)
        if infer_steps_int <= 8:
            return original_normalize(self, *args, **kwargs)
        logger.warning(
            "[AceRadio] bypassing core turbo infer_steps clamp via runtime patch requested={}",
            infer_steps_int,
        )
        return original_normalize(_HostProxy(self), *args, **kwargs)

    @functools.wraps(original_build_kwargs)
    def patched_build_kwargs(self, *args, **kwargs):
        build_kwargs = original_build_kwargs(self, *args, **kwargs)
        if not getattr(getattr(self, "config", None), "is_turbo", False):
            return build_kwargs
        timesteps = _get_bound_argument(original_build_kwargs, self, args, kwargs, "timesteps", default=build_kwargs.get("timesteps"))
        if timesteps is not None:
            return build_kwargs
        infer_steps = _get_bound_argument(original_build_kwargs, self, args, kwargs, "infer_steps", default=build_kwargs.get("infer_steps"))
        try:
            infer_steps_int = int(infer_steps)
        except Exception:
            return build_kwargs
        if infer_steps_int <= 8:
            return build_kwargs
        effective_steps = max(1, min(infer_steps_int, RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO))
        schedule = _get_turbo_timesteps_for_infer_steps(effective_steps)
        build_kwargs["timesteps"] = torch.tensor(schedule, dtype=torch.float32, device=self.device)
        build_kwargs["infer_steps"] = effective_steps
        logger.warning(
            "[AceRadio] turbo runtime patch mapped requested infer_steps={} to explicit timesteps schedule len={} values={}",
            infer_steps_int,
            len(schedule),
            schedule,
        )
        return build_kwargs

    ServiceGenerateRequestMixin._normalize_service_generate_inputs = patched_normalize
    ServiceGenerateRequestMixin._aceradio_turbo_clamp_patch_installed = True
    ServiceGenerateRequestMixin._aceradio_turbo_clamp_patch_original = original_normalize
    ServiceGenerateExecuteMixin._build_service_generate_kwargs = patched_build_kwargs
    ServiceGenerateExecuteMixin._aceradio_turbo_timestep_patch_installed = True
    ServiceGenerateExecuteMixin._aceradio_turbo_timestep_patch_original = original_build_kwargs
    logger.warning("[AceRadio] core turbo infer_steps clamp bypass enabled")
    logger.warning(
        "[AceRadio] turbo runtime timestep patch enabled: requested steps > 8 are converted to explicit 1..20 timestep schedules",
    )
    return True

def _resolve_lora_root(project_root: str) -> str:

    preferred = str(os.environ.get("ACESTEP_REMOTE_LORA_ROOT", "") or "").strip()
    if preferred:
        return preferred
    return os.path.join(project_root, "lora")

def _iter_disk_lora_entries(lora_root: str) -> list[dict]:

    entries: list[dict] = []
    try:
        root_path = Path(lora_root)
    except Exception:
        return entries
    try:
        if not root_path.exists() or not root_path.is_dir():
            return entries
    except Exception:
        return entries
    for child in sorted(root_path.iterdir(), key=lambda p: p.name.lower()):
        try:
            if child.is_file():
                if child.suffix.lower() not in {".safetensors", ".pt", ".bin", ".ckpt"}:
                    continue
                lora_id = child.name
                entries.append({"id": lora_id, "trigger": child.stem, "label": child.stem})
                continue
            if not child.is_dir():
                continue
            adapter_cfg = child / 'adapter_config.json'
            adapter_model = child / 'adapter_model.safetensors'
            if not adapter_cfg.is_file() and not adapter_model.is_file():
                continue
            trigger = child.name
            try:
                if adapter_cfg.is_file():
                    cfg = json.loads(adapter_cfg.read_text(encoding='utf-8'))
                    trigger_words = cfg.get('trigger_words')
                    if isinstance(trigger_words, (list, tuple)):
                        trigger_words = ' '.join(str(x).strip() for x in trigger_words if str(x).strip())
                    trigger = str(trigger_words or cfg.get('trigger') or child.name).strip() or child.name
            except Exception:
                trigger = child.name
            entries.append({"id": child.name, "trigger": trigger, "label": child.name})
            for sub in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if not sub.is_dir():
                        continue
                    if not (sub / 'adapter_model.safetensors').is_file():
                        continue
                    sub_id = f"{child.name}/{sub.name}"
                    entries.append({"id": sub_id, "trigger": trigger, "label": f"{child.name} ({sub.name})"})
                except Exception:
                    continue
        except Exception:
            continue
    return entries

def _merge_lora_catalog_with_disk(static_items: list[dict], lora_root: str) -> list[dict]:

    merged: list[dict] = []
    seen_ids: set[str] = set()
    for item in static_items or []:
        if not isinstance(item, dict):
            continue
        lora_id = str(item.get("id", "") or "")
        normalized = {
            "id": lora_id,
            "trigger": str((item.get("trigger", item.get("tag", ""))) or ""),
            "label": str(item.get("label", "") or lora_id),
        }
        merged.append(normalized)
        seen_ids.add(lora_id)
    for item in _iter_disk_lora_entries(lora_root):
        lora_id = str(item.get("id", "") or "")
        if not lora_id or lora_id in seen_ids:
            continue
        merged.append(item)
        seen_ids.add(lora_id)
    return merged

def _json_safe(obj, _depth: int = 0, _seen: Optional[set[int]] = None):

    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if _seen is None:
        _seen = set()
    try:
        oid = id(obj)
        if oid in _seen:
            return "<circular>"
        _seen.add(oid)
    except Exception:
        pass
    if _depth > 25:
        return "<max_depth>"
    try:
        from pathlib import Path as _Path
        if isinstance(obj, _Path):
            return str(obj)
    except Exception:
        pass
    if isinstance(obj, (bytes, bytearray, memoryview)):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return str(obj)
    try:
        import torch
        if isinstance(obj, torch.Tensor):
            try:
                numel = int(obj.numel())
                if numel == 1:
                    return obj.detach().cpu().item()
                if numel <= 64:
                    return obj.detach().cpu().tolist()
                return {
                    "__tensor__": True,
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                    "device": str(obj.device),
                    "numel": numel,
                }
            except Exception:
                return {
                    "__tensor__": True,
                    "shape": list(getattr(obj, "shape", [])),
                    "dtype": str(getattr(obj, "dtype", "")),
                }
    except Exception:
        pass
    try:
        import numpy as np
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.generic,)):
            return obj.item()
    except Exception:
        pass
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            try:
                sk = str(k)
            except Exception:
                sk = "<key>"
            out[sk] = _json_safe(v, _depth=_depth + 1, _seen=_seen)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v, _depth=_depth + 1, _seen=_seen) for v in obj]
    try:
        import dataclasses
        if dataclasses.is_dataclass(obj):
            return _json_safe(dataclasses.asdict(obj), _depth=_depth + 1, _seen=_seen)
    except Exception:
        pass
    for attr in ("model_dump", "dict", "to_dict"):
        if hasattr(obj, attr):
            try:
                fn = getattr(obj, attr)
                if callable(fn):
                    return _json_safe(fn(), _depth=_depth + 1, _seen=_seen)
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        try:
            return _json_safe(vars(obj), _depth=_depth + 1, _seen=_seen)
        except Exception:
            pass
    try:
        return str(obj)
    except Exception:
        return "<unprintable>"

def _write_json(path: str, data: dict):

    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = _json_safe(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)

def create_app() -> FastAPI:

    project_root = _get_project_root()
    config_path = os.environ.get("ACESTEP_REMOTE_CONFIG_PATH", "acestep-v15-turbo")
    device = os.environ.get("ACESTEP_REMOTE_DEVICE", "auto")
    max_duration = 600
    results_root = os.environ.get(
        "ACESTEP_REMOTE_RESULTS_DIR",
        os.path.join(project_root, "aceradio_outputs"),
    )
    results_root = results_root.replace("\\", "/")
    config_root = os.path.join(results_root, "configs").replace("\\", "/")
    system_config_root = os.path.join(config_root, "system").replace("\\", "/")
    counter_path = os.path.join(system_config_root, "aceradio_songs_generated.json").replace("\\", "/")
    legacy_counter_path = os.path.join(results_root, "aceradio_songs_generated.json").replace("\\", "/")
    legacy_counter_path_alt = os.path.join(results_root, "_songs_generated.json").replace("\\", "/")
    os.makedirs(results_root, exist_ok=True)
    os.makedirs(config_root, exist_ok=True)
    os.makedirs(system_config_root, exist_ok=True)
    logs_dir = os.path.join(results_root, "_logs").replace("\\", "/")
    os.makedirs(logs_dir, exist_ok=True)

    def _ensure_logs_dir() -> str:

        try:
            Path(results_root).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[logs] ensure results_root failed base={} err={!r}", results_root, exc)
            raise
        try:
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[logs] ensure logs_dir failed dir={} err={!r}", logs_dir, exc)
            raise
        return logs_dir

    def _start_job_cli_capture(job_id: str) -> str:

        _ensure_logs_dir()
        tmp_path = os.path.join(logs_dir, f"{job_id}__live_cli.txt").replace("\\", "/")
        capture_fp = open(tmp_path, "a", encoding="utf-8", buffering=1)
        fmt = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}"
        sink_id = logger.add(capture_fp, level="DEBUG", format=fmt, enqueue=False, backtrace=False, diagnose=False)
        py_handler = logging.StreamHandler(capture_fp)
        py_handler.setLevel(logging.DEBUG)
        py_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        attached = []
        for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            py_logger = logging.getLogger(logger_name)
            py_logger.addHandler(py_handler)
            attached.append(py_logger)
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = _TeeStream(original_stdout, capture_fp)
        sys.stderr = _TeeStream(original_stderr, capture_fp)
        app.state._job_cli_captures[job_id] = {
            "tmp_path": tmp_path,
            "capture_fp": capture_fp,
            "sink_id": sink_id,
            "py_handler": py_handler,
            "attached": attached,
            "stdout": original_stdout,
            "stderr": original_stderr,
        }
        return tmp_path

    def _finalize_job_cli_capture(job_id: str, audio_paths: list[str] | None = None) -> list[str]:

        created = []
        ctx = app.state._job_cli_captures.pop(job_id, None)
        if not ctx:
            return created
        try:
            sys.stdout = ctx.get("stdout", sys.stdout)
            sys.stderr = ctx.get("stderr", sys.stderr)
        except Exception:
            pass
        try:
            logger.remove(ctx.get("sink_id"))
        except Exception:
            pass
        py_handler = ctx.get("py_handler")
        for py_logger in ctx.get("attached", []):
            try:
                py_logger.removeHandler(py_handler)
            except Exception:
                pass
        capture_fp = ctx.get("capture_fp")
        if capture_fp is not None:
            try:
                capture_fp.flush()
            except Exception:
                pass
            try:
                capture_fp.close()
            except Exception:
                pass
        tmp_path = str(ctx.get("tmp_path") or "")
        if not tmp_path or not Path(tmp_path).exists():
            return created
        targets = []
        if audio_paths:
            for idx, audio_path in enumerate(audio_paths or []):
                audio_name = os.path.basename(str(audio_path or "")).strip()
                base_name = os.path.splitext(audio_name)[0].strip() or f"{job_id}_{idx}"
                targets.append(os.path.join(logs_dir, f"{base_name}_log.txt").replace("\\", "/"))
        else:
            targets.append(os.path.join(logs_dir, f"{job_id}_log.txt").replace("\\", "/"))
        for target in targets:
            try:
                shutil.copyfile(tmp_path, target)
                created.append(target)
            except Exception as exc:
                logger.warning("[job_log] copy failed src={} dst={} err={!r}", tmp_path, target, exc)
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
        return created
    app_counter_lock = None
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    ui_root = os.path.dirname(__file__)
    app = FastAPI(title="AceRadio")
    app.state._job_cli_captures = {}

    def _loaded_dit_model_names() -> set[str]:

        loaded = set()
        active = str(getattr(app.state, '_active_model', '') or '').strip()
        default_model = str(getattr(app.state, '_default_model', '') or '').strip()
        if _music_runtime_loaded() and active:
            loaded.add(active)
        elif _music_runtime_loaded() and default_model:
            loaded.add(default_model)
        return loaded

    def _radio_model_inventory() -> List[Dict[str, Any]]:

        active = str(getattr(app.state, '_active_model', config_path) or config_path).strip()
        default_model = str(getattr(app.state, '_default_model', config_path) or config_path).strip()
        return _collect_radio_model_inventory(project_root, active, default_model, _loaded_dit_model_names())

    def _radio_model_names() -> List[str]:

        return _collect_radio_model_names(project_root, str(getattr(app.state, '_active_model', config_path) or config_path).strip(), str(getattr(app.state, '_default_model', config_path) or config_path).strip(), _loaded_dit_model_names())

    def _normalize_model_choice(v: Optional[str], *, allow_default: bool = True) -> str:

        s = str(v or '').strip()
        if s:
            return s
        current = str(getattr(app.state, '_active_model', config_path) or config_path).strip()
        if current:
            return current
        if allow_default:
            return str(config_path or '').strip()
        return ''

    def _ensure_radio_model_choice(v: Optional[str], *, allow_default: bool = True) -> str:

        selected = _normalize_model_choice(v, allow_default=allow_default)
        if not selected:
            raise ValueError('No DiT model selected')
        available = set(_radio_model_names())
        if selected not in available:
            raise ValueError(f'Model not available for AceRadio: {selected}')
        return selected

    remote_token = os.environ.get('ACESTEP_REMOTE_TOKEN', '').strip()
    auth_dir = os.path.join(results_root, "_auth").replace("\\", "/")
    users_path = os.path.join(auth_dir, "users.json").replace("\\", "/")
    auth_log_path = os.path.join(auth_dir, "access_log.jsonl").replace("\\", "/")
    auth_enabled = str(os.environ.get("ACERADIO_AUTH_ENABLED", os.environ.get("ACERADIO_AUTH_ENABLED", "0"))).strip().lower() in {"1", "true", "yes", "on"}
    session_cookie_name = str(os.environ.get("ACERADIO_SESSION_COOKIE", os.environ.get("ACERADIO_SESSION_COOKIE", "aceradio_session")) or "aceradio_session").strip()
    session_cookie_secure = str(os.environ.get("ACERADIO_SESSION_SECURE", os.environ.get("ACERADIO_SESSION_SECURE", "0"))).strip().lower() in {"1", "true", "yes", "on"}

    def _ensure_auth_dir() -> str:
        try:
            Path(auth_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[auth] ensure auth dir failed dir={} err={!r}", auth_dir, exc)
            raise
        return auth_dir

    def _password_hash(password: str, salt: bytes | None = None, iterations: int = 200_000) -> dict:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', str(password or '').encode('utf-8'), salt, int(iterations))
        return {
            "salt": urlsafe_b64encode(salt).decode('ascii'),
            "hash": urlsafe_b64encode(digest).decode('ascii'),
            "iterations": int(iterations),
        }

    def _verify_password(password: str, rec: dict) -> bool:
        try:
            salt = urlsafe_b64decode(str(rec.get('password_salt') or '').encode('ascii'))
            expected = str(rec.get('password_hash') or '')
            iterations = int(rec.get('password_iterations') or 200_000)
        except Exception:
            return False
        trial = _password_hash(password, salt=salt, iterations=iterations)
        return hmac.compare_digest(str(trial.get('hash') or ''), expected)

    def _generate_temp_password(length: int = 16) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#$%+=_-"
        return ''.join(secrets.choice(alphabet) for _ in range(max(12, int(length))))

    def _normalize_email(value: str) -> str:
        return str(value or '').strip().lower()

    def _auth_now() -> float:
        return float(time.time())

    def _blank_auth_store() -> dict:
        return {"users": []}

    def _load_auth_store() -> dict:
        if not os.path.exists(users_path):
            return _blank_auth_store()
        try:
            with open(users_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('users'), list):
                return data
        except Exception as exc:
            logger.warning("[auth] load users failed path={} err={!r}", users_path, exc)
        return _blank_auth_store()

    def _save_auth_store(data: dict) -> None:
        _ensure_auth_dir()
        tmp = users_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, users_path)

    def _append_auth_event(event_type: str, *, email: str = '', ip: str = '', ok: bool = True, detail: str = '', session_id: str = '') -> None:
        try:
            _ensure_auth_dir()
            payload = {
                'ts': _auth_now(),
                'event': str(event_type or ''),
                'email': _normalize_email(email),
                'ip': str(ip or ''),
                'ok': bool(ok),
                'detail': str(detail or ''),
                'session_id': str(session_id or ''),
            }
            with open(auth_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning('[auth] append audit failed path={} err={!r}', auth_log_path, exc)

    def _read_auth_events(limit: int = 100) -> list[dict]:
        if not os.path.exists(auth_log_path):
            return []
        try:
            with open(auth_log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:
            logger.warning('[auth] read audit failed path={} err={!r}', auth_log_path, exc)
            return []
        out = []
        for raw in lines[-max(1, int(limit)):]:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return list(reversed(out))

    def _sanitize_user(rec: dict) -> dict:
        return {
            "email": str(rec.get('email') or ''),
            "role": str(rec.get('role') or 'user'),
            "must_change_password": bool(rec.get('must_change_password', False)),
            "created_at": rec.get('created_at'),
            "updated_at": rec.get('updated_at'),
            "last_login_at": rec.get('last_login_at'),
            "last_login_ip": rec.get('last_login_ip'),
        }

    def _find_user(data: dict, email: str) -> tuple[dict | None, int | None]:
        target = _normalize_email(email)
        users = data.get('users') if isinstance(data, dict) else None
        if not isinstance(users, list):
            return None, None
        for idx, rec in enumerate(users):
            if _normalize_email((rec or {}).get('email')) == target:
                return rec, idx
        return None, None

    def _set_password(rec: dict, password: str, *, must_change_password: bool) -> None:
        ph = _password_hash(password)
        rec['password_salt'] = ph['salt']
        rec['password_hash'] = ph['hash']
        rec['password_iterations'] = ph['iterations']
        rec['must_change_password'] = bool(must_change_password)
        rec['updated_at'] = _auth_now()

    def _invalidate_session_locked(email: str) -> None:
        target = _normalize_email(email)
        sid = app.state._auth_user_to_session.pop(target, None)
        if sid:
            app.state._auth_sessions.pop(sid, None)

    def _create_session_locked(email: str, ip: str, user_agent: str) -> str:
        target = _normalize_email(email)
        _invalidate_session_locked(target)
        sid = secrets.token_urlsafe(32)
        now = _auth_now()
        app.state._auth_sessions[sid] = {
            'email': target,
            'ip': str(ip or 'unknown'),
            'user_agent': str(user_agent or ''),
            'created_at': now,
            'last_seen': now,
        }
        app.state._auth_user_to_session[target] = sid
        return sid

    def _bootstrap_admin_if_needed() -> None:
        if not auth_enabled:
            return
        with app.state._auth_lock:
            data = _load_auth_store()
            if isinstance(data.get('users'), list) and data['users']:
                return
            admin_email = _normalize_email(os.environ.get('ACERADIO_ADMIN_EMAIL', os.environ.get('ACERADIO_ADMIN_EMAIL', 'admin@local')))
            preset_password = str(os.environ.get('ACERADIO_ADMIN_PASSWORD', os.environ.get('ACERADIO_ADMIN_PASSWORD', '')) or '').strip()
            temp_password = preset_password or _generate_temp_password()
            now = _auth_now()
            rec = {
                'email': admin_email,
                'role': 'admin',
                'created_at': now,
                'updated_at': now,
                'last_login_at': None,
                'last_login_ip': None,
                'must_change_password': not bool(preset_password),
            }
            _set_password(rec, temp_password, must_change_password=not bool(preset_password))
            data['users'] = [rec]
            _save_auth_store(data)
            _append_auth_event('bootstrap_admin', email=admin_email, ok=True, detail='bootstrap admin created')
            logger.warning('[auth] bootstrap admin created email={} temporary_password={} must_change_password={}', admin_email, temp_password, not bool(preset_password))

    def _get_authenticated_user(request: Request) -> dict | None:
        if not auth_enabled:
            return None
        sid = str(request.cookies.get(session_cookie_name) or '').strip()
        if not sid:
            return None
        ip = _get_client_ip(request)
        with app.state._auth_lock:
            session = app.state._auth_sessions.get(sid)
            if not session:
                return None
            email = _normalize_email(session.get('email'))
            if str(session.get('ip') or '').strip() != str(ip or '').strip():
                _append_auth_event('session_ip_mismatch', email=email, ip=ip, ok=False, detail='session invalidated due to IP mismatch', session_id=sid)
                _invalidate_session_locked(email)
                return None
            data = _load_auth_store()
            rec, _ = _find_user(data, email)
            if not rec:
                _invalidate_session_locked(email)
                return None
            if app.state._auth_user_to_session.get(email) != sid:
                return None
            session['last_seen'] = _auth_now()
            return dict(rec)

    def _set_session_cookie(response: Response, sid: str) -> None:
        response.set_cookie(
            key=session_cookie_name,
            value=sid,
            httponly=True,
            secure=session_cookie_secure,
            samesite='lax',
            max_age=7 * 24 * 60 * 60,
            path='/',
        )

    def _clear_session_cookie(response: Response) -> None:
        response.delete_cookie(session_cookie_name, path='/', samesite='lax')

    def _auth_payload(request: Request, user: dict | None) -> dict:
        return {
            'enabled': bool(auth_enabled),
            'authenticated': bool(user),
            'must_change_password': bool((user or {}).get('must_change_password', False)),
            'user': _sanitize_user(user) if user else None,
            'is_admin': bool((user or {}).get('role') == 'admin'),
            'ip': _get_client_ip(request),
        }

    def _require_token(request: Request) -> None:

        if not remote_token:
            return
        tok = (request.headers.get('x-ace-token') or request.headers.get('authorization') or '').strip()
        if tok.lower().startswith('bearer '):
            tok = tok[7:].strip()
        if tok != remote_token:
            raise HTTPException(status_code=401, detail='Unauthorized')

    @app.middleware("http")

    async def _no_cache_ui(request, call_next):
        resp = await call_next(request)
        p = request.url.path or ""
        if p == "/" or p.startswith("/static") or p.startswith("/favicon") or p.startswith('/api/auth') or p.startswith('/api/admin'):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path or ""
        if request.headers.get('X-AceRadio-Internal') == '1':
            return await call_next(request)
        if auth_enabled and (path.startswith('/api') or path.startswith('/download')):
            allow = {'/api/auth/status', '/api/auth/login'}
            user = _get_authenticated_user(request)
            if path not in allow and not user:
                return JSONResponse(status_code=401, content={'detail': 'AUTH_REQUIRED'})
            if user and bool(user.get('must_change_password')) and path not in {'/api/auth/status', '/api/auth/logout', '/api/auth/change-password'}:
                return JSONResponse(status_code=403, content={'detail': 'PASSWORD_CHANGE_REQUIRED'})
            if user is not None:
                request.state.auth_user = user
        return await call_next(request)
    import threading
    app.state._counter_lock = threading.Lock()
    app.state._rate_lock = threading.Lock()
    app.state._last_job_at_by_ip = {}
    app.state._ab_compare_window_by_ip = {}
    app.state._rate_min_interval_s = float(os.environ.get("ACESTEP_REMOTE_MIN_JOB_INTERVAL_S", "5"))
    app.state._queue_active_cap = int(os.environ.get("ACESTEP_REMOTE_MAX_ACTIVE_JOBS", "30"))
    app.state._auth_lock = threading.Lock()
    app.state._auth_sessions = {}
    app.state._auth_user_to_session = {}
    app.state._auth_enabled = bool(auth_enabled)
    _bootstrap_admin_if_needed()

    def _load_counter() -> int:

        for path in (counter_path, legacy_counter_path, legacy_counter_path_alt):
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    v = int(data.get("songs_generated", 0))
                    return max(0, v)
            except Exception:
                continue
        return 0

    def _save_counter(n: int) -> None:

        try:
            tmp = counter_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"songs_generated": int(n)}, f, ensure_ascii=False)
            os.replace(tmp, counter_path)
            for old_path in (legacy_counter_path, legacy_counter_path_alt):
                if old_path != counter_path and os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
        except Exception:
            pass
    app.state.songs_generated = _load_counter()
    lora_catalog_path = os.path.join(ui_root, "lora_catalog.json").replace("\\", "/")

    def _load_lora_catalog() -> list[dict]:

        out: list[dict] = []
        try:
            if os.path.exists(lora_catalog_path):
                with open(lora_catalog_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    out = _merge_lora_catalog_with_disk(data, _resolve_lora_root(project_root))
        except Exception:
            out = []
        if not out:
            out = _merge_lora_catalog_with_disk([], _resolve_lora_root(project_root))
        if not out or out[0].get("id", "") != "":
            out.insert(0, {"id": "", "trigger": "", "label": "(Nessun LoRA)"})
        try:
            if out and str(out[0].get("id", "") or "") == "":
                out[0]["trigger"] = ""
        except Exception:
            pass
        return out
    app.state._lora_catalog = _load_lora_catalog()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    _install_lora_runtime_patch()
    dit_handler = AceStepHandler()
    llm_handler = LLMHandler()
    app.state._active_model = _normalize_model_choice(config_path)
    app.state._default_model = app.state._active_model

    def _dispose_handler(h: object) -> None:
        try:
            try:
                unload_lora = getattr(h, "unload_lora", None)
                if callable(unload_lora):
                    try:
                        unload_lora()
                    except Exception:
                        pass
            except Exception:
                pass
            decoder = getattr(getattr(h, "model", None), "decoder", None)
            try:
                if decoder is not None:
                    _best_effort_release_runtime_value(decoder)
            except Exception:
                pass
            for attr in (
                "model",
                "vae",
                "text_encoder",
                "text_tokenizer",
                "silence_latent",
                "reward_model",
                "mlx_decoder",
                "mlx_vae",
                "_mlx_compiled_decode",
                "_mlx_compiled_encode_sample",
                "_lora_service",
            ):
                try:
                    value = getattr(h, attr, None)
                except Exception:
                    value = None
                try:
                    _best_effort_release_runtime_value(value)
                except Exception:
                    pass
                if hasattr(h, attr):
                    try:
                        setattr(h, attr, None)
                    except Exception:
                        pass
            for attr in (
                "config",
                "last_init_params",
                "quantization",
                "compiled",
                "current_offload_cost",
                "lora_loaded",
                "use_lora",
                "lora_scale",
                "_base_decoder",
                "_active_loras",
                "_lora_adapter_registry",
                "_lora_active_adapter",
                "_lora_scale_state",
                "_lora_last_scale_report",
                "_adapter_type",
                "_mlx_vae_dtype",
            ):
                if hasattr(h, attr):
                    try:
                        setattr(h, attr, None)
                    except Exception:
                        pass
        except Exception:
            pass

    def _cleanup_cuda_cache() -> None:
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                try:
                    torch.cuda.synchronize()
                except Exception:
                    pass
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
            try:
                import torch._dynamo
                torch._dynamo.reset()
            except Exception:
                pass
            try:
                import torch._inductor.codecache
                torch._inductor.codecache.clear_cache()
            except Exception:
                pass
        except Exception:
            pass

    def _initialize_dit_handler(model_name: str) -> AceStepHandler:
        want = _ensure_radio_model_choice(model_name)
        use_flash_attention = _env_flag("ACESTEP_REMOTE_USE_FLASH_ATTENTION", True)
        compile_model = _env_flag("ACESTEP_REMOTE_COMPILE_MODEL", True)
        offload_to_cpu = _env_flag("ACESTEP_REMOTE_OFFLOAD_TO_CPU", False)
        offload_dit_to_cpu = _env_flag("ACESTEP_REMOTE_OFFLOAD_DIT_TO_CPU", False)
        int8_quantization = _env_flag("ACESTEP_REMOTE_INT8_QUANTIZATION", False)
        use_mlx_dit = _env_flag("ACESTEP_REMOTE_USE_MLX_DIT", False)
        quantization = "int8" if int8_quantization else None
        newh = AceStepHandler()
        status, ok = newh.initialize_service(
            project_root=project_root,
            config_path=want,
            device=device,
            use_flash_attention=use_flash_attention,
            compile_model=compile_model,
            offload_to_cpu=offload_to_cpu,
            offload_dit_to_cpu=offload_dit_to_cpu,
            quantization=quantization,
            use_mlx_dit=use_mlx_dit,
        )
        if not ok or newh.model is None:
            raise RuntimeError(f"Model init failed: {status}")
        return newh

    def _music_runtime_loaded() -> bool:
        handler = getattr(app.state, "dit_handler", None)
        return bool(handler is not None and getattr(handler, "model", None) is not None)

    def _offload_music_runtime(reason: str = "") -> dict:
        active_model = str(getattr(app.state, "_active_model", app.state._default_model) or app.state._default_model)
        old = getattr(app.state, "dit_handler", None)
        loaded = bool(old is not None and getattr(old, "model", None) is not None)
        if loaded:
            logger.info(f"[AceRadio] Offloading music runtime for {reason or 'request'}: {active_model}")
            _dispose_handler(old)
            app.state.dit_handler = None
            _cleanup_cuda_cache()
            try:
                del old
            except Exception:
                pass
        return {
            "ok": True,
            "model": active_model,
            "loaded": False,
            "changed": loaded,
        }

    def _ensure_music_runtime_loaded(model_name: str = "") -> AceStepHandler:
        want = _ensure_radio_model_choice(model_name or getattr(app.state, "_active_model", app.state._default_model) or app.state._default_model)
        cur = str(getattr(app.state, "_active_model", app.state._default_model) or app.state._default_model)
        if want == cur and _music_runtime_loaded():
            return app.state.dit_handler
        old = getattr(app.state, "dit_handler", None)
        if old is not None:
            logger.info(f"[AceRadio] Switching model: {cur} -> {want}")
            _dispose_handler(old)
            app.state.dit_handler = None
            _cleanup_cuda_cache()
            try:
                del old
            except Exception:
                pass
        else:
            logger.info(f"[AceRadio] Loading music runtime: {want}")
        try:
            newh = _initialize_dit_handler(want)
        except Exception as exc:
            logger.error(str(exc))
            rollback_to = cur
            if want != rollback_to:
                logger.warning(f"[AceRadio] Model switch failed; attempting rollback to {rollback_to}...")
                try:
                    rh = _initialize_dit_handler(rollback_to)
                    app.state.dit_handler = rh
                    app.state._active_model = rollback_to
                    return rh
                except Exception:
                    pass
            raise
        app.state.dit_handler = newh
        app.state._active_model = want
        _cleanup_cuda_cache()
        return newh

    def _ensure_model_loaded(model_name: str) -> AceStepHandler:
        return _ensure_music_runtime_loaded(model_name)

    @app.on_event("startup")

    async def _startup():
        bypass_requested = _is_core_turbo_step_clamp_bypass_enabled()
        bypass_installed = _install_core_turbo_step_clamp_bypass_patch()
        app.state._core_turbo_step_clamp_bypass_requested = bool(bypass_requested)
        app.state._core_turbo_step_clamp_bypass_installed = bool(bypass_installed)
        if bypass_requested and not bypass_installed:
            logger.warning("[AceRadio] core turbo infer_steps clamp bypass requested but not installed; core clamp remains active")

        logger.info("[AceRadio] Initializing DiT model…")
        use_flash_attention = _env_flag("ACESTEP_REMOTE_USE_FLASH_ATTENTION", True)
        compile_model = _env_flag("ACESTEP_REMOTE_COMPILE_MODEL", True)
        offload_to_cpu = _env_flag("ACESTEP_REMOTE_OFFLOAD_TO_CPU", False)
        offload_dit_to_cpu = _env_flag("ACESTEP_REMOTE_OFFLOAD_DIT_TO_CPU", False)
        int8_quantization = _env_flag("ACESTEP_REMOTE_INT8_QUANTIZATION", False)
        use_mlx_dit = _env_flag("ACESTEP_REMOTE_USE_MLX_DIT", False)
        quantization = "int8" if int8_quantization else None

        import functools as _functools
        status, ok = await asyncio.to_thread(
            _functools.partial(
                dit_handler.initialize_service,
                project_root=project_root,
                config_path=app.state._active_model,
                device=device,
                use_flash_attention=use_flash_attention,
                compile_model=compile_model,
                offload_to_cpu=offload_to_cpu,
                offload_dit_to_cpu=offload_dit_to_cpu,
                quantization=quantization,
                use_mlx_dit=use_mlx_dit,
            )
        )
        if not ok or dit_handler.model is None:
            logger.error(status)
            raise RuntimeError(f"Model init failed: {status}")
        logger.info(status)

        app.state.dit_handler = dit_handler
        app.state.llm_handler = llm_handler
        app.state.project_root = project_root
        app.state.results_root = results_root
        app.state.max_duration = max_duration
        app.state.queue = InProcessJobQueue(worker_fn=_run_job, outputs_root=results_root)
        logger.info(f"[AceRadio] Queue online. outputs={results_root}")

        lm_model_path = os.environ.get("ACESTEP_REMOTE_LM_MODEL_PATH", "acestep-5Hz-lm-4B").strip() or "acestep-5Hz-lm-4B"
        lm_backend = os.environ.get("ACESTEP_REMOTE_LM_BACKEND", "vllm").strip().lower() or "vllm"
        if lm_backend not in {"vllm", "pt", "mlx"}:
            lm_backend = "vllm"
        lm_device = os.environ.get("ACESTEP_REMOTE_LM_DEVICE", device)
        lm_offload = os.environ.get("ACESTEP_REMOTE_LM_OFFLOAD_TO_CPU", "").strip().lower() in {"1", "true", "yes", "y", "on"}
        try:
            logger.info(f"[AceRadio] Initializing 5Hz LM… ({lm_model_path}, backend={lm_backend})")
            llm_status, llm_ok = await asyncio.to_thread(
                _functools.partial(
                    llm_handler.initialize,
                    checkpoint_dir=os.path.join(project_root, "checkpoints"),
                    lm_model_path=lm_model_path,
                    backend=lm_backend,
                    device=lm_device,
                    offload_to_cpu=lm_offload,
                    dtype=None,
                )
            )
            if llm_ok:
                logger.info(f"[AceRadio] 5Hz LM ready: {lm_model_path}")
                app.state._llm_ready = True
            else:
                logger.warning(f"[AceRadio] 5Hz LM init failed: {llm_status}")
                app.state._llm_ready = False
        except Exception as exc:
            logger.warning(f"[AceRadio] 5Hz LM init exception: {exc}")
            app.state._llm_ready = False

    @app.get("/api/runtime/music_model")

    def music_model_status():

        active_model = str(getattr(app.state, "_active_model", app.state._default_model) or app.state._default_model)
        return {
            "ok": True,
            "model": active_model,
            "loaded": bool(_music_runtime_loaded()),
        }

    @app.post("/api/runtime/music_model/offload")

    async def offload_music_model(request: Request):

        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        reason = str((payload or {}).get("reason") or "").strip()
        return await asyncio.to_thread(_offload_music_runtime, reason)

    @app.post("/api/runtime/music_model/ensure_loaded")

    async def ensure_music_model_loaded(request: Request):

        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        model_name = _ensure_radio_model_choice((payload or {}).get("model"), allow_default=True)
        handler = await asyncio.to_thread(_ensure_music_runtime_loaded, model_name)
        return {
            "ok": True,
            "model": str(getattr(app.state, "_active_model", app.state._default_model) or app.state._default_model),
            "loaded": bool(handler is not None and getattr(handler, "model", None) is not None),
        }

    @app.on_event("shutdown")

    def _shutdown():

        q: Optional[InProcessJobQueue] = getattr(app.state, "queue", None)
        if q:
            q.stop()

    def _run_job(job_id: str, req: dict) -> dict:

        save_dir = os.path.join(results_root, job_id).replace("\\", "/")
        os.makedirs(save_dir, exist_ok=True)
        meta_path = os.path.join(save_dir, 'metadata.json').replace('\\', '/')
        dt = 0.0
        caption = (req.get('caption') or '').strip()
        lyrics = (req.get('lyrics') or '').strip()
        instrumental = bool(req.get('instrumental', False))
        lora_id = (req.get('lora_id') or '').strip()
        lora_trigger = (req.get('lora_trigger') or req.get('lora_tag') or '').strip()
        lora_weight = _parse_lora_weight_value(req.get('lora_weight', 0.5), default=0.5)
        lora_path = ''
        lora_loaded_for_job = False
        try:
                duration_auto = bool(req.get("duration_auto", False))
                bpm_auto = bool(req.get("bpm_auto", False))
                key_auto = bool(req.get("key_auto", False))
                timesig_auto = bool(req.get("timesig_auto", False))
                language_auto = bool(req.get("language_auto", False))
                requested_model = _ensure_radio_model_choice(req.get("model"), allow_default=True)
                try:
                    dit_handler = _ensure_model_loaded(requested_model)
                except Exception as e:
                    logger.error(f"[AceRadio] model load failed ({requested_model}): {e}")
                    raise RuntimeError(f"Model load failed ({requested_model}): {e}")
                if duration_auto:
                    duration = -1.0
                else:
                    duration = float(req.get("duration", max_duration))
                    if duration <= 0:
                        duration = float(max_duration)
                    duration = max(10.0, min(duration, float(max_duration)))
                caption = (req.get("caption") or "").strip()
                lyrics = (req.get("lyrics") or "").strip()
                original_caption = caption
                original_lyrics = lyrics
                instrumental = bool(req.get("instrumental", False))
                lora_id = (req.get("lora_id") or "").strip()
                lora_trigger = (req.get("lora_trigger") or req.get("lora_tag") or "").strip()
                lora_weight = _parse_lora_weight_value(req.get("lora_weight", 0.5), default=0.5)
                try:
                    logger.info(
                        f"[LoRA] requested id='{lora_id or ''}' trigger='{lora_trigger or ''}' weight={lora_weight:.2f}"
                    )
                except Exception:
                    pass
                if lora_id and lora_trigger:
                    try:
                        known_tags = {
                            str((it.get("trigger", it.get("tag", "")) ) or "").strip()
                            for it in (getattr(app.state, "_lora_catalog", []) or [])
                            if isinstance(it, dict)
                        }
                        known_tags.discard("")
                    except Exception:
                        known_tags = set()
                    cap_trim = str(caption or "").lstrip()
                    try:
                        import re
                        m = re.match(r"^([a-zA-Z0-9_\-]+)\s*,\s*(.*)$", cap_trim)
                        if m and (m.group(1) in known_tags):
                            cap_trim = str(m.group(2) or "").strip()
                    except Exception:
                        pass
                    prefix = f"{lora_trigger},"
                    if not cap_trim.lower().startswith(prefix.lower()):
                        caption = f"{lora_trigger}, {cap_trim}" if cap_trim else f"{lora_trigger}"
                    else:
                        caption = cap_trim
                    try:
                        logger.info(f"[LoRA] Caption prefixed (first 80 chars): {caption[:80]!r}")
                    except Exception:
                        pass
                seed = req.get("seed", -1)
                try:
                    seed = int(seed)
                except Exception:
                    seed = -1
                batch_size = 1
                inference_steps = req.get("inference_steps", None)
                try:
                    inference_steps = (
                        int(inference_steps)
                        if inference_steps is not None and str(inference_steps) != ""
                        else None
                    )
                except Exception:
                    inference_steps = None
                model_name_for_limits = req.get("model") or req.get("model_used") or ""
                config_name = str(model_name_for_limits) if model_name_for_limits is not None else ""
                is_sft = _is_sft_model(config_name)
                is_turbo = _is_turbo_model(config_name)
                if inference_steps is None:
                    inference_steps = 50 if is_sft else 8
                if inference_steps is not None:
                    max_steps = _get_max_inference_steps_for_model(config_name)
                    inference_steps = max(1, min(inference_steps, max_steps))
                infer_method = str(req.get("infer_method") or "ode").strip().lower()
                if infer_method not in {"ode", "sde"}:
                    infer_method = "ode"
                timesteps_raw = req.get("timesteps", None)
                parsed_timesteps = _parse_timesteps_input(timesteps_raw)
                source_start = req.get("source_start", 0.0)
                source_end = req.get("source_end", -1.0)
                try:
                    source_start = float(source_start) if source_start is not None and str(source_start) != "" else 0.0
                except Exception:
                    source_start = 0.0
                try:
                    source_end = float(source_end) if source_end is not None and str(source_end) != "" else -1.0
                except Exception:
                    source_end = -1.0
                source_start = max(0.0, min(source_start, float(max_duration)))
                source_end = max(-1.0, min(source_end, float(max_duration)))
                guidance_scale = req.get("guidance_scale", None)
                try:
                    guidance_scale = (
                        float(guidance_scale)
                        if guidance_scale is not None and str(guidance_scale) != ""
                        else None
                    )
                except Exception:
                    guidance_scale = None
                if guidance_scale is not None:
                    guidance_scale = max(1.0, min(guidance_scale, 15.0))
                shift = req.get("shift", None)
                try:
                    shift = float(shift) if shift is not None and str(shift) != "" else None
                except Exception:
                    shift = None
                if shift is not None:
                    shift = max(1.0, min(shift, 5.0))
                use_adg = bool(req.get("use_adg", False))
                cfg_interval_start = req.get("cfg_interval_start", None)
                cfg_interval_end = req.get("cfg_interval_end", None)
                try:
                    cfg_interval_start = (
                        float(cfg_interval_start)
                        if cfg_interval_start is not None and str(cfg_interval_start) != ""
                        else None
                    )
                except Exception:
                    cfg_interval_start = None
                try:
                    cfg_interval_end = (
                        float(cfg_interval_end)
                        if cfg_interval_end is not None and str(cfg_interval_end) != ""
                        else None
                    )
                except Exception:
                    cfg_interval_end = None
                if cfg_interval_start is not None:
                    cfg_interval_start = max(0.0, min(cfg_interval_start, 1.0))
                if cfg_interval_end is not None:
                    cfg_interval_end = max(0.0, min(cfg_interval_end, 1.0))
                enable_normalization = bool(req.get("enable_normalization", True))
                normalization_db = req.get("normalization_db", None)
                try:
                    normalization_db = (
                        float(normalization_db)
                        if normalization_db is not None and str(normalization_db) != ""
                        else None
                    )
                except Exception:
                    normalization_db = None
                if normalization_db is not None:
                    normalization_db = max(-10.0, min(normalization_db, 0.0))
                score_scale = req.get("score_scale", 0.5)
                try:
                    score_scale = float(score_scale)
                except Exception:
                    score_scale = 0.5
                score_scale = max(0.01, min(score_scale, 1.0))
                auto_score = bool(req.get("auto_score", False))
                latent_shift = req.get("latent_shift", None)
                latent_rescale = req.get("latent_rescale", None)
                try:
                    latent_shift = (
                        float(latent_shift)
                        if latent_shift is not None and str(latent_shift) != ""
                        else None
                    )
                except Exception:
                    latent_shift = None
                try:
                    latent_rescale = (
                        float(latent_rescale)
                        if latent_rescale is not None and str(latent_rescale) != ""
                        else None
                    )
                except Exception:
                    latent_rescale = None
                if latent_shift is not None:
                    latent_shift = max(-0.2, min(latent_shift, 0.2))
                if latent_rescale is not None:
                    latent_rescale = max(0.5, min(latent_rescale, 1.5))
                bpm = req.get("bpm", None)
                try:
                    bpm = float(bpm) if bpm is not None and str(bpm) != "" else None
                except Exception:
                    bpm = None
                if bpm is not None:
                    bpm = max(30.0, min(bpm, 300.0))
                if bpm_auto:
                    bpm = None
                keyscale = (req.get("keyscale") or "").strip()
                if key_auto:
                    keyscale = ""
                timesignature = (req.get("timesignature") or "").strip()
                if timesignature not in {"", "2/4", "3/4", "4/4", "6/8"}:
                    timesignature = ""
                if timesig_auto:
                    timesignature = ""
                vocal_language = (req.get("vocal_language") or "unknown").strip()
                if vocal_language not in set(VALID_LANGUAGES):
                    vocal_language = "unknown"
                if language_auto:
                    vocal_language = "unknown"
                if instrumental:
                    lyrics = "[Instrumental]"
                    vocal_language = "unknown"
                try:
                    logger.info(
                        "[worker] metas duration=%r duration_auto=%r bpm=%r bpm_auto=%r keyscale=%r key_auto=%r timesignature=%r timesig_auto=%r vocal_language=%r language_auto=%r"
                        % (duration, duration_auto, bpm, bpm_auto, keyscale, key_auto, timesignature, timesig_auto, vocal_language, language_auto)
                    )
                except Exception:
                    pass
                thinking = bool(req.get("thinking", True))
                if "use_lm" in req:
                    try:
                        thinking = bool(req.get("use_lm"))
                    except Exception:
                        pass
                lm_temperature = req.get("lm_temperature", 0.85)
                lm_cfg_scale = req.get("lm_cfg_scale", 2.0)
                lm_top_k = req.get("lm_top_k", 0)
                lm_top_p = req.get("lm_top_p", 0.9)
                lm_negative_prompt = req.get("lm_negative_prompt", "NO USER INPUT")
                use_constrained_decoding = req.get("use_constrained_decoding", True)
                try:
                    lm_temperature = float(lm_temperature)
                except Exception:
                    lm_temperature = 0.85
                lm_temperature = max(0.0, min(lm_temperature, 2.0))
                try:
                    lm_cfg_scale = float(lm_cfg_scale)
                except Exception:
                    lm_cfg_scale = 2.0
                lm_cfg_scale = max(1.0, min(lm_cfg_scale, 3.0))
                try:
                    lm_top_k = int(float(lm_top_k))
                except Exception:
                    lm_top_k = 0
                lm_top_k = max(0, min(lm_top_k, 200))
                try:
                    lm_top_p = float(lm_top_p)
                except Exception:
                    lm_top_p = 0.9
                lm_top_p = max(0.0, min(lm_top_p, 1.0))
                try:
                    lm_negative_prompt = str(lm_negative_prompt or "NO USER INPUT")
                except Exception:
                    lm_negative_prompt = "NO USER INPUT"
                lm_negative_prompt = lm_negative_prompt.strip() or "NO USER INPUT"
                use_constrained_decoding = bool(use_constrained_decoding)
                use_cot_metas = bool(req.get("use_cot_metas", thinking))
                use_cot_caption = bool(req.get("use_cot_caption", thinking))
                use_cot_language = bool(req.get("use_cot_language", thinking))
                parallel_thinking = bool(req.get("parallel_thinking", False))
                constrained_decoding_debug = bool(req.get("constrained_decoding_debug", False))
                auto_score = bool(req.get("auto_score", False))
                if instrumental:
                    thinking = False
                    auto_score = False
                    use_constrained_decoding = False
                    use_cot_metas = False
                    use_cot_caption = False
                    use_cot_language = False
                    parallel_thinking = False
                    constrained_decoding_debug = False
                if not thinking:
                    use_cot_metas = False
                    use_cot_caption = False
                    use_cot_language = False
                    parallel_thinking = False
                    constrained_decoding_debug = False
                audio_format, mp3_bitrate, mp3_sample_rate = _normalize_mp3_export_request(
                    req.get("audio_format"),
                    req.get("mp3_bitrate"),
                    req.get("mp3_sample_rate"),
                )
                _log_export_request(
                    "[api/jobs]",
                    req.get("audio_format"),
                    req.get("mp3_bitrate"),
                    req.get("mp3_sample_rate"),
                    audio_format,
                    mp3_bitrate,
                    mp3_sample_rate,
                )
                generation_mode = str(req.get("generation_mode") or "").strip() or "Custom"
                if generation_mode not in {"Simple", "Custom"}:
                    generation_mode = "Custom"
                task_type = "text2music"
                src_audio_abs = None
                src_audio_rel = ""
                _params_kwargs = dict(
                    task_type=task_type,
                    src_audio=src_audio_abs,
                    caption=caption,
                    lyrics=lyrics,
                    instrumental=instrumental,
                    duration=duration,
                    seed=seed,
                    bpm=bpm,
                    keyscale=keyscale,
                    timesignature=timesignature,
                    vocal_language=vocal_language,
                    enable_normalization=enable_normalization,
                    normalization_db=normalization_db,
                    latent_shift=latent_shift,
                    latent_rescale=latent_rescale,
                    inference_steps=inference_steps,
                    guidance_scale=guidance_scale,
                    use_adg=use_adg,
                    cfg_interval_start=cfg_interval_start,
                    cfg_interval_end=cfg_interval_end,
                    shift=shift,
                    infer_method=infer_method,
                    timesteps=parsed_timesteps,
                    thinking=thinking,
                    lm_temperature=lm_temperature,
                    lm_cfg_scale=lm_cfg_scale,
                    lm_top_k=lm_top_k,
                    lm_top_p=lm_top_p,
                    lm_negative_prompt=lm_negative_prompt,
                    use_constrained_decoding=use_constrained_decoding,
                    use_cot_metas=use_cot_metas,
                    use_cot_caption=use_cot_caption,
                    use_cot_language=use_cot_language,
                )
                _is_source_audio_flow = str(req.get("generation_mode") or "").strip() == "Remix"
                if _is_source_audio_flow:
                    _params_kwargs["source_start"] = source_start
                    _params_kwargs["source_end"] = source_end
                try:
                    params = GenerationParams(**_params_kwargs)
                except TypeError as exc:
                    _err = str(exc)
                    if _is_source_audio_flow and ("source_start" in _err or "source_end" in _err):
                        _compat_kwargs = dict(_params_kwargs)
                        _compat_kwargs.pop("source_start", None)
                        _compat_kwargs.pop("source_end", None)
                        _compat_kwargs["rep" + "ainting_start"] = source_start
                        _compat_kwargs["rep" + "ainting_end"] = source_end
                        params = GenerationParams(**_compat_kwargs)
                    else:
                        raise
                config = GenerationConfig(
                    batch_size=batch_size,
                    use_random_seed=(seed < 0),
                    seeds=([int(seed)] if (isinstance(seed, int) and seed >= 0) else None),
                    allow_lm_batch=parallel_thinking,
                    constrained_decoding_debug=constrained_decoding_debug,
                    audio_format=audio_format,
                    mp3_bitrate=mp3_bitrate,
                    mp3_sample_rate=mp3_sample_rate,
                )
                lora_loaded_for_job = False
                lora_path = ""
                if lora_id:
                    if ('..' in lora_id) or ('/' in lora_id) or ('\\' in lora_id):
                        raise RuntimeError('LoRA id non valido (path).')
                    lora_root = _resolve_lora_root(project_root)
                    lora_path = os.path.join(lora_root, lora_id)
                    try:
                        exists = bool(os.path.exists(lora_path))
                    except Exception:
                        exists = False
                    logger.info(f"[LoRA] requested id='{lora_id}' weight={lora_weight:.2f}")
                    logger.info(f"[LoRA] resolved path={lora_path} exists={exists}")
                    if not exists:
                        logger.error("[LoRA] load FAIL (path missing)")
                        raise RuntimeError(f"LoRA non trovato: {lora_id} (atteso: {lora_path})")
                    try:
                        msg = dit_handler.load_lora(lora_path)
                        if not str(msg).startswith("✅"):
                            logger.error(f"[LoRA] load FAIL ({msg})")
                            raise RuntimeError(str(msg))
                        logger.info("[LoRA] load OK")
                    except Exception:
                        logger.exception("[LoRA] load FAIL (exception)")
                        raise
                    try:
                        dit_handler.set_use_lora(True)
                    except Exception:
                        pass
                    try:
                        scale_msg = dit_handler.set_lora_scale(lora_id, lora_weight)
                        if isinstance(scale_msg, str) and scale_msg.startswith("❌"):
                            scale_msg = dit_handler.set_lora_scale(lora_weight)
                        if isinstance(scale_msg, str) and not scale_msg.startswith("✅"):
                            logger.warning(f"[LoRA] set_lora_scale warning: {scale_msg}")
                    except Exception as e:
                        logger.warning(f"[LoRA] set_lora_scale exception (continuo comunque): {e}")
                    lora_loaded_for_job = True
                _seed_list = getattr(config, "seeds", None)
                logger.info(
                    f"[job {job_id}] summary mode={generation_mode} task_type={task_type} seed={seed} "
                    f"use_random_seed={bool(getattr(config,'use_random_seed',False))} src_present={bool(src_audio_abs)}"
                )
                logger.debug(
                    f"[job {job_id}] mode={generation_mode} task_type={task_type} seed={seed} "
                    f"use_random_seed={bool(getattr(config,'use_random_seed',False))} seeds={_seed_list}"
                )
                _log_export_request(
                    f"[job {job_id}]",
                    req.get("audio_format"),
                    req.get("mp3_bitrate"),
                    req.get("mp3_sample_rate"),
                    audio_format,
                    mp3_bitrate,
                    mp3_sample_rate,
                )
                t0 = time.time()
                try:
                    result = generate_music(
                        dit_handler=dit_handler,
                        llm_handler=(app.state.llm_handler if (thinking and not instrumental and getattr(app.state, "_llm_ready", False)) else None),
                        params=params,
                        config=config,
                        save_dir=save_dir,
                    )
                    dt = time.time() - t0
                finally:
                    if lora_loaded_for_job:
                        try:
                            try:
                                import torch
                                import gc
                                if torch.cuda.is_available():
                                    alloc0 = torch.cuda.memory_allocated() / (1024**3)
                                    res0 = torch.cuda.memory_reserved() / (1024**3)
                                    logger.info(f"[LoRA] VRAM before unload: allocated={alloc0:.2f}GB reserved={res0:.2f}GB")
                            except Exception:
                                pass
                            dit_handler.unload_lora()
                            try:
                                dit_handler.set_use_lora(False)
                            except Exception:
                                pass
                            try:
                                import torch
                                import gc
                                if torch.cuda.is_available():
                                    gc.collect()
                                    torch.cuda.empty_cache()
                                    try:
                                        torch.cuda.ipc_collect()
                                    except Exception:
                                        pass
                                    alloc1 = torch.cuda.memory_allocated() / (1024**3)
                                    res1 = torch.cuda.memory_reserved() / (1024**3)
                                    logger.info(f"[LoRA] VRAM after unload: allocated={alloc1:.2f}GB reserved={res1:.2f}GB (delta_alloc={alloc1-alloc0:+.2f}GB delta_res={res1-res0:+.2f}GB)")
                            except Exception:
                                pass
                            logger.info("[LoRA] unload OK")
                        except Exception as e:
                            logger.exception("[LoRA] unload FAIL")
                if not result.success:
                    raise RuntimeError(result.error or result.status_message or "Unknown error")
                audio_paths = []
                if result.audios:
                    for a in result.audios:
                        p = a.get("path", "")
                        if p:
                            audio_paths.append(p)
                export_applied = []
                if audio_format == 'mp3' and audio_paths:
                    rewritten_paths = []
                    for original_path in audio_paths:
                        probe_before = _ffprobe_audio_stream(original_path)
                        final_path, probe_after = _ensure_mp3_export(original_path, mp3_bitrate, mp3_sample_rate)
                        rewritten_paths.append(final_path)
                        export_applied.append({
                            'path': final_path,
                            'requested_bitrate': mp3_bitrate,
                            'requested_sample_rate': mp3_sample_rate,
                            'applied_bitrate_kbps': int(round((probe_after.get('bit_rate') or 0) / 1000.0)) if probe_after.get('bit_rate') else 0,
                            'applied_sample_rate': int(probe_after.get('sample_rate') or 0),
                            'codec': str(probe_after.get('codec') or ''),
                            'before_codec': str(probe_before.get('codec') or ''),
                            'before_sample_rate': int(probe_before.get('sample_rate') or 0) if probe_before.get('sample_rate') else 0,
                            'before_bitrate_kbps': int(round((probe_before.get('bit_rate') or 0) / 1000.0)) if probe_before.get('bit_rate') else 0,
                        })
                        logger.info('[job %s] mp3 export applied: path=%s requested_bitrate=%s requested_rate=%s applied_bitrate_kbps=%s applied_rate=%s codec=%s', job_id, final_path, mp3_bitrate, mp3_sample_rate, export_applied[-1]['applied_bitrate_kbps'], export_applied[-1]['applied_sample_rate'], export_applied[-1]['codec'])
                    audio_paths = rewritten_paths
                    for idx, a in enumerate(result.audios or []):
                        if idx < len(audio_paths) and isinstance(a, dict):
                            a['path'] = audio_paths[idx]
                score_entries = []
                if auto_score:
                    try:
                        from acestep.core.scoring.lm_score import calculate_pmi_score_per_condition
                        llm_handler = app.state.llm_handler if getattr(app.state, "_llm_ready", False) else None
                        lm_meta = getattr(result, "extra_outputs", {}) or {}
                        lm_metadata = lm_meta.get("lm_metadata") if isinstance(lm_meta, dict) else None
                        for a in (result.audios or []):
                            a_params = a.get("params") or {}
                            audio_codes_str = str(a_params.get("audio_codes") or "").strip()
                            if not audio_codes_str or not llm_handler or not getattr(llm_handler, "llm_initialized", False):
                                score_entries.append({
                                    "quality_score": None,
                                    "quality_score_per_condition": {},
                                    "quality_score_status": "skipped" if not audio_codes_str else "lm_not_ready",
                                })
                                continue
                            metadata = {}
                            if isinstance(lm_metadata, dict):
                                metadata.update(lm_metadata)
                            if caption and "caption" not in metadata:
                                metadata["caption"] = caption
                            if bpm is not None and "bpm" not in metadata:
                                try:
                                    metadata["bpm"] = int(bpm)
                                except Exception:
                                    pass
                            if duration and duration > 0 and "duration" not in metadata:
                                try:
                                    metadata["duration"] = int(duration)
                                except Exception:
                                    pass
                            if keyscale and "keyscale" not in metadata:
                                metadata["keyscale"] = str(keyscale)
                            if vocal_language and "language" not in metadata:
                                metadata["language"] = str(vocal_language)
                            if timesignature and "timesignature" not in metadata:
                                metadata["timesignature"] = str(timesignature)
                            scores_per_condition, global_score, status = calculate_pmi_score_per_condition(
                                llm_handler=llm_handler,
                                audio_codes=audio_codes_str,
                                caption=caption or "",
                                lyrics=lyrics or "",
                                metadata=(metadata if metadata else None),
                                temperature=1.0,
                                topk=10,
                                score_scale=float(score_scale),
                            )
                            score_entries.append({
                                "quality_score": float(global_score) if global_score is not None else None,
                                "quality_score_per_condition": scores_per_condition or {},
                                "quality_score_status": status,
                            })
                    except Exception as _score_exc:
                        logger.warning(f"[score] auto_score failed: {_score_exc}")
                meta_path = os.path.join(save_dir, "metadata.json").replace("\\", "/")
                _resolved_seeds = []
                for i, a in enumerate(result.audios or []):
                    _audio_seed = a.get("seed") if isinstance(a, dict) else None
                    if _audio_seed is None and isinstance(a, dict) and isinstance(a.get("params"), dict):
                        _audio_seed = a["params"].get("seed")
                    if _audio_seed is None and seed >= 0 and batch_size == 1:
                        _audio_seed = seed
                    if _audio_seed is None:
                        _audio_seed = -1
                    try:
                        _resolved_seeds.append(int(_audio_seed))
                    except Exception:
                        _resolved_seeds.append(-1)
                result_block = {
                    "success": bool(getattr(result, "success", True)),
                    "error": getattr(result, "error", None),
                    "status_message": (
                        (f"[LM] thinking={bool(thinking)} temp={lm_temperature:.2f} cfg={lm_cfg_scale:.2f} top_k={int(lm_top_k)} top_p={lm_top_p:.2f} constrained={bool(use_constrained_decoding)}\n")
                        + str(getattr(result, "status_message", ""))
                    ),
                    "audios": [
                        {
                            "path": p,
                            "filename": os.path.basename(str(p or "")),
                            "format": audio_format,
                            "resolved_seed": (_resolved_seeds[i] if i < len(_resolved_seeds) else -1),
                            "mp3_bitrate": mp3_bitrate if audio_format == 'mp3' else None,
                            "mp3_sample_rate": mp3_sample_rate if audio_format == 'mp3' else None,
                            "export_applied": (export_applied[i] if i < len(export_applied) else None),
                            **(
                                (score_entries[i] if (score_entries and i < len(score_entries)) else {})
                            ),
                        }
                        for i, p in enumerate(audio_paths or [])
                    ],
                    "resolved_seeds": _resolved_seeds,
                    "extra_outputs": _json_safe(getattr(result, "extra_outputs", {})),
                }
                job_log_paths = _finalize_job_cli_capture(job_id, audio_paths)
                payload = {
                    "job_id": job_id,
                    "created_at": int(time.time()),
                    "seconds": dt,
                    "request": {
                        "model": requested_model,
                        "model_used": str(getattr(app.state, "_active_model", requested_model) or requested_model),
                        "caption": original_caption,
                        "lyrics": original_lyrics,
                        "instrumental": instrumental,
                        "duration": duration,
                        "duration_auto": duration_auto,
                        "seed": seed,
                        "generation_mode": generation_mode,
                        "task_type": task_type,
                        "src_audio": src_audio_rel,
                        "lora_id": lora_id,
                        "lora_trigger": lora_trigger,
                        "lora_weight": lora_weight,
                        "lora_path": lora_path,
                        "lora_loaded": bool(lora_loaded_for_job),
                        "batch_size": batch_size,
                        "audio_format": audio_format,
                        "mp3_bitrate": mp3_bitrate,
                        "mp3_sample_rate": mp3_sample_rate,
                        "inference_steps": inference_steps,
                        "infer_method": infer_method,
                        "timesteps": timesteps_raw if isinstance(timesteps_raw, str) else (parsed_timesteps if parsed_timesteps is not None else ""),
                        "source_start": source_start,
                        "source_end": source_end,
                        "guidance_scale": guidance_scale,
                        "shift": shift,
                        "use_adg": use_adg,
                        "cfg_interval_start": cfg_interval_start,
                        "cfg_interval_end": cfg_interval_end,
                        "enable_normalization": enable_normalization,
                        "normalization_db": normalization_db,
                        "score_scale": score_scale,
                        "auto_score": auto_score,
                        "latent_shift": latent_shift,
                        "latent_rescale": latent_rescale,
                        "bpm": bpm,
                        "bpm_auto": bpm_auto,
                        "keyscale": keyscale,
                        "key_auto": key_auto,
                        "timesignature": timesignature,
                        "timesig_auto": timesig_auto,
                        "vocal_language": vocal_language,
                        "language_auto": language_auto,
                        "song_title": req.get("song_title") or req.get("title") or "",
                        "title": req.get("title") or req.get("song_title") or "",
                        "genre": req.get("genre") or req.get("style") or "",
                        "theme": req.get("theme") or req.get("lyrical_theme") or "",
                        "thinking": thinking,
                        "lm_temperature": lm_temperature,
                        "lm_cfg_scale": lm_cfg_scale,
                        "lm_top_k": lm_top_k,
                        "lm_top_p": lm_top_p,
                        "lm_negative_prompt": lm_negative_prompt,
                        "use_constrained_decoding": use_constrained_decoding,
                        "use_cot_metas": use_cot_metas,
                        "use_cot_caption": use_cot_caption,
                        "use_cot_language": use_cot_language,
                        "parallel_thinking": parallel_thinking,
                        "constrained_decoding_debug": constrained_decoding_debug,
                    },
                    "result": result_block,
                }
                _write_json(meta_path, payload)
                try:
                    with app.state._counter_lock:
                        app.state.songs_generated = int(getattr(app.state, "songs_generated", 0)) + 1
                        _save_counter(app.state.songs_generated)
                except Exception:
                    pass
                return {
                    "audio_paths": audio_paths,
                    "json_path": meta_path,
                    "audio_count": len(audio_paths) if isinstance(audio_paths, list) else 0,
                    "job_log_paths": job_log_paths,
                    "save_dir": save_dir,
                    "seconds": dt,
                }
        except Exception as e:
            try:
                payload = {
                    'job_id': job_id,
                    'created_at': int(time.time()),
                    'seconds': float(dt or 0.0),
                    'request': {
                        'model': _normalize_model_choice(req.get('model')),
                        'model_used': str(getattr(app.state, '_active_model', _normalize_model_choice(req.get('model'))) or _normalize_model_choice(req.get('model'))),
                        'caption': caption,
                        'lyrics': lyrics,
                        'instrumental': instrumental,
                        'duration': req.get('duration', None),
                        'seed': req.get('seed', None),
                        'generation_mode': req.get('generation_mode', None),
                        'task_type': req.get('task_type', None),
                        'src_audio': req.get('src_audio', None),
                        'lora_id': lora_id,
                        'lora_weight': lora_weight,
                        'lora_path': lora_path,
                        'lora_loaded': bool(lora_loaded_for_job),
                    },
                    'result': {
                        'success': False,
                        'error': str(e),
                        'status_message': str(e),
                        'audios': [],
                        'extra_outputs': {},
                    },
                }
                _write_json(meta_path, payload)
            except Exception:
                pass
            try:
                _finalize_job_cli_capture(job_id, [])
            except Exception:
                pass
            try:
                _cleanup_failed_job_dir(save_dir)
            except Exception:
                pass
            raise

    def _get_client_ip(request) -> str:

        try:
            xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
            if xff:
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if parts:
                    return parts[0]
        except Exception:
            pass
        try:
            xri = request.headers.get("x-real-ip") or request.headers.get("X-Real-IP")
            if xri:
                return str(xri).strip()
        except Exception:
            pass
        try:
            return request.client.host
        except Exception:
            return "unknown"

    def _job_dir_has_audio_files(save_dir: str | os.PathLike[str]) -> bool:

        try:
            job_dir = Path(save_dir)
            if not job_dir.exists() or not job_dir.is_dir():
                return False
            exts = {'.mp3', '.wav', '.flac', '.opus', '.aac', '.wav32'}
            return any(entry.is_file() and entry.suffix.lower() in exts for entry in job_dir.iterdir())
        except Exception:
            return False

    def _cleanup_failed_job_dir(save_dir: str | os.PathLike[str]) -> bool:

        try:
            job_dir = Path(save_dir)
            if not job_dir.exists() or not job_dir.is_dir():
                return False
            if _job_dir_has_audio_files(job_dir):
                return False
            shutil.rmtree(job_dir)
            return True
        except Exception:
            return False

    @app.get('/api/auth/status')
    def auth_status(request: Request):
        user = _get_authenticated_user(request)
        return _auth_payload(request, user)

    @app.post('/api/auth/login')
    def auth_login(payload: dict, request: Request):
        if not auth_enabled:
            return _auth_payload(request, None)
        email = _normalize_email((payload or {}).get('email'))
        password = str((payload or {}).get('password') or '')
        if not email or not password:
            raise HTTPException(status_code=400, detail='Missing email or password.')
        with app.state._auth_lock:
            data = _load_auth_store()
            rec, idx = _find_user(data, email)
            if rec is None or idx is None or not _verify_password(password, rec):
                _append_auth_event('login', email=email, ip=_get_client_ip(request), ok=False, detail='invalid credentials')
                raise HTTPException(status_code=401, detail='Invalid credentials.')
            ip = _get_client_ip(request)
            sid = _create_session_locked(email, ip, request.headers.get('user-agent') or '')
            data['users'][idx]['last_login_at'] = _auth_now()
            data['users'][idx]['last_login_ip'] = ip
            data['users'][idx]['updated_at'] = _auth_now()
            _save_auth_store(data)
            _append_auth_event('login', email=email, ip=ip, ok=True, detail='login ok', session_id=sid)
            response = JSONResponse(content=_auth_payload(request, data['users'][idx]))
            _set_session_cookie(response, sid)
            return response

    @app.post('/api/auth/logout')
    def auth_logout(request: Request):
        response = JSONResponse(content={'ok': True})
        if auth_enabled:
            user = _get_authenticated_user(request)
            with app.state._auth_lock:
                if user:
                    _append_auth_event('logout', email=str(user.get('email') or ''), ip=_get_client_ip(request), ok=True, detail='logout')
                    _invalidate_session_locked(str(user.get('email') or ''))
            _clear_session_cookie(response)
        return response

    @app.post('/api/auth/change-password')
    def auth_change_password(payload: dict, request: Request):
        if not auth_enabled:
            return {'ok': True, 'enabled': False}
        user = _get_authenticated_user(request)
        if not user:
            raise HTTPException(status_code=401, detail='AUTH_REQUIRED')
        new_password = str((payload or {}).get('new_password') or '')
        if len(new_password) < 10:
            raise HTTPException(status_code=400, detail='New password must be at least 10 characters long.')
        email = _normalize_email(user.get('email'))
        with app.state._auth_lock:
            data = _load_auth_store()
            rec, idx = _find_user(data, email)
            if rec is None or idx is None:
                raise HTTPException(status_code=404, detail='User not found.')
            _set_password(data['users'][idx], new_password, must_change_password=False)
            _save_auth_store(data)
            _append_auth_event('change_password', email=email, ip=_get_client_ip(request), ok=True, detail='password updated')
            user = data['users'][idx]
        return {'ok': True, 'user': _sanitize_user(user)}

    @app.get('/api/admin/users')
    def admin_list_users(request: Request):
        if not auth_enabled:
            raise HTTPException(status_code=404, detail='Auth disabled.')
        user = _get_authenticated_user(request)
        if not user or str(user.get('role') or '') != 'admin':
            raise HTTPException(status_code=403, detail='Admin only.')
        with app.state._auth_lock:
            data = _load_auth_store()
            users = [_sanitize_user(rec) for rec in data.get('users', []) if isinstance(rec, dict)]
        return {'users': users, 'count': len(users)}

    @app.post('/api/admin/users')
    def admin_create_user(payload: dict, request: Request):
        if not auth_enabled:
            raise HTTPException(status_code=404, detail='Auth disabled.')
        user = _get_authenticated_user(request)
        if not user or str(user.get('role') or '') != 'admin':
            raise HTTPException(status_code=403, detail='Admin only.')
        email = _normalize_email((payload or {}).get('email'))
        role = str((payload or {}).get('role') or 'user').strip().lower()
        if role not in {'user', 'admin'}:
            role = 'user'
        if not email or '@' not in email:
            raise HTTPException(status_code=400, detail='Enter a valid email address.')
        temp_password = _generate_temp_password()
        now = _auth_now()
        with app.state._auth_lock:
            data = _load_auth_store()
            existing, _ = _find_user(data, email)
            if existing is not None:
                raise HTTPException(status_code=409, detail='User already exists.')
            rec = {
                'email': email,
                'role': role,
                'created_at': now,
                'updated_at': now,
                'last_login_at': None,
                'last_login_ip': None,
                'must_change_password': True,
            }
            _set_password(rec, temp_password, must_change_password=True)
            data.setdefault('users', []).append(rec)
            _save_auth_store(data)
            _append_auth_event('create_user', email=email, ip=_get_client_ip(request), ok=True, detail=f'created by {user.get("email", "admin")}')
        return {'ok': True, 'user': _sanitize_user(rec), 'temporary_password': temp_password}

    @app.delete('/api/admin/users')
    def admin_delete_user(request: Request, email: str = ''):
        if not auth_enabled:
            raise HTTPException(status_code=404, detail='Auth disabled.')
        user = _get_authenticated_user(request)
        if not user or str(user.get('role') or '') != 'admin':
            raise HTTPException(status_code=403, detail='Admin only.')
        target_email = _normalize_email(email)
        actor_email = _normalize_email(str(user.get('email') or ''))
        if not target_email or '@' not in target_email:
            raise HTTPException(status_code=400, detail='Enter a valid email address.')
        if target_email == actor_email:
            raise HTTPException(status_code=400, detail='You cannot delete your own account.')
        with app.state._auth_lock:
            data = _load_auth_store()
            rec, idx = _find_user(data, target_email)
            if rec is None or idx is None:
                raise HTTPException(status_code=404, detail='User not found.')
            role = str(rec.get('role') or 'user').strip().lower()
            if role == 'admin':
                admins = [u for u in (data.get('users') or []) if isinstance(u, dict) and str(u.get('role') or 'user').strip().lower() == 'admin']
                if len(admins) <= 1:
                    raise HTTPException(status_code=400, detail='You cannot delete the last admin account.')
            del data['users'][idx]
            _invalidate_session_locked(target_email)
            _save_auth_store(data)
            _append_auth_event('delete_user', email=target_email, ip=_get_client_ip(request), ok=True, detail=f'deleted by {user.get("email", "admin")}')
            users = [_sanitize_user(item) for item in data.get('users', []) if isinstance(item, dict)]
        return {'ok': True, 'deleted_email': target_email, 'users': users, 'count': len(users)}

    @app.get('/api/admin/auth-events')
    def admin_auth_events(request: Request, limit: int = 100):
        if not auth_enabled:
            raise HTTPException(status_code=404, detail='Auth disabled.')
        user = _get_authenticated_user(request)
        if not user or str(user.get('role') or '') != 'admin':
            raise HTTPException(status_code=403, detail='Admin only.')
        return {'events': _read_auth_events(limit=max(1, min(int(limit or 100), 500)))}

    @app.get("/favicon.ico")

    def favicon():

        fav_path = os.path.join(static_dir, "favicon.ico")
        if os.path.exists(fav_path):
            return FileResponse(fav_path, media_type="image/x-icon")
        return Response(status_code=204)

    @app.get("/api/client_ip")

    def client_ip(request: Request):

        return {"ip": _get_client_ip(request)}

    @app.get("/api/stats")

    def stats(request: Request):

        with app.state._counter_lock:
            n = int(getattr(app.state, "songs_generated", 0))
        return {"ip": _get_client_ip(request), "songs_generated": n}

    @app.get("/api/system")

    def system_info(request: Request):

        gpu = _get_gpu_info_cached(app)
        if not gpu:
            return {
                "gpu_name": None,
                "vram_used_mb": None,
                "vram_total_mb": None,
                "gpu_temp_c": None,
            }
        return gpu

    @app.get("/api/lora_catalog")

    def lora_catalog():

        return app.state._lora_catalog

    @app.get("/", response_class=HTMLResponse)

    def index():

        index_path = os.path.join(static_dir, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()

    @app.get("/api/health")

    def health():

        active_model = str(getattr(app.state, "_active_model", config_path) or config_path)
        bypass_requested = bool(getattr(app.state, "_core_turbo_step_clamp_bypass_requested", _is_core_turbo_step_clamp_bypass_enabled()))
        bypass_installed = bool(getattr(app.state, "_core_turbo_step_clamp_bypass_installed", False))
        model_inventory = _radio_model_inventory()
        return {
            "status": "ok",
            "max_duration": max_duration,
            "model": active_model,
            "models": model_inventory,
            "max_batch_size": 1,
            "audio_formats": ["flac","wav","mp3","opus","aac","wav32"],
            "limits": {
                "max_inference_steps_current_model": _get_max_inference_steps_for_model(active_model),
                "max_inference_steps_turbo": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO,
                "max_inference_steps_base": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_BASE,
                "max_inference_steps_sft": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_SFT,
            },
            "cleanup_ttl_seconds": _get_cleanup_ttl_seconds(),
            "core_turbo_step_clamp_bypass_enabled": bypass_installed,
            "core_turbo_step_clamp_bypass_requested": bypass_requested,
        }

    @app.get("/api/options")

    def options():

        active_model = str(getattr(app.state, "_active_model", config_path) or config_path)
        bypass_requested = bool(getattr(app.state, "_core_turbo_step_clamp_bypass_requested", _is_core_turbo_step_clamp_bypass_enabled()))
        bypass_installed = bool(getattr(app.state, "_core_turbo_step_clamp_bypass_installed", False))
        model_inventory = _radio_model_inventory()
        model_limits = {entry.get('name'): {'max_inference_steps': _get_max_inference_steps_for_model(entry.get('name'))} for entry in model_inventory if entry.get('name')}
        default_shift = 1.0 if (_is_sft_model(active_model) or _is_base_model(active_model)) else 3.0
        default_inference_steps = 50 if _is_sft_model(active_model) else (32 if _is_base_model(active_model) else 8)
        return {
            "valid_languages": VALID_LANGUAGES,
            "time_signatures": ["", "2/4", "3/4", "4/4", "6/8"],
            "lm_ready": bool(getattr(app.state, "_llm_ready", False)),
            "think_default": True,
            "current_model": active_model,
            "models": model_inventory,
            "model_limits": model_limits,
            "limits": {
                "max_inference_steps_current_model": _get_max_inference_steps_for_model(active_model),
                "max_inference_steps_turbo": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_TURBO,
                "max_inference_steps_base": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_BASE,
                "max_inference_steps_sft": RUNTIME_DEFAULT_MAX_INFERENCE_STEPS_SFT,
            },
            "infer_methods": ["ode", "sde"],
            "core_turbo_step_clamp_bypass_enabled": bypass_installed,
            "core_turbo_step_clamp_bypass_requested": bypass_requested,
            "defaults": {
                "inference_steps": default_inference_steps,
                "infer_method": "ode",
                "timesteps": "",
                "source_start": 0.0,
                "source_end": -1.0,
                "guidance_scale": 7.0,
                "shift": default_shift,
                "cfg_interval_start": 0.0,
                "cfg_interval_end": 1.0,
                "latent_shift": 0.0,
                "latent_rescale": 1.0,
                "enable_normalization": True,
                "normalization_db": -1.0,
            },
        }
    examples_path = os.path.join(os.path.dirname(__file__), "examples.json").replace("\\", "/")
    _examples_cache = None

    def _load_examples():

        nonlocal _examples_cache
        if _examples_cache is not None:
            return _examples_cache
        if not os.path.exists(examples_path):
            _examples_cache = {"examples": []}
            return _examples_cache
        with open(examples_path, "r", encoding="utf-8") as f:
            _examples_cache = json.load(f)
        return _examples_cache

    @app.get("/api/examples/random")

    def random_example():

        data = _load_examples()
        items = data.get("examples", []) if isinstance(data, dict) else []
        if not items:
            return {}
        return random.choice(items)
    def _normalize_custom_mode_payload(payload: dict | None) -> dict:

        return dict(payload or {})

    def _safe_json_dump(value) -> str:

        try:
            return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        except Exception as exc:
            return json.dumps({"_serialization_error": repr(exc)}, ensure_ascii=False, indent=2, sort_keys=True)

    def _build_formatted_prompt_with_cot_snapshot(req: dict) -> str:

        caption = str(req.get("caption") or "").strip()
        lyrics = str(req.get("lyrics") or "").strip()
        bpm = req.get("bpm", None)
        duration = req.get("duration", None)
        keyscale = str(req.get("keyscale") or "").strip()
        timesignature = str(req.get("timesignature") or "").strip()
        return f"""<|im_start|>system
Instruction:
Generate audio semantic tokens based on the given conditions:

<|im_end|>
<|im_start|>user
Caption:
{caption}

Lyric:
{lyrics}
<|im_end|>
<|im_start|>assistant
<think>
bpm: {bpm}
duration: {duration}
keyscale: {keyscale}
timesignature: {timesignature}
</think>

<|im_end|>"""

    @app.post("/api/jobs")

    def create_job(payload: dict, request: Request):

        _require_token(request)
        payload = _normalize_custom_mode_payload(payload)
        job_id = str(uuid4())
        try:
            _start_job_cli_capture(job_id)
        except Exception as exc:
            logger.warning("[job_log] live capture start failed job_id={} err={!r}", job_id, exc)
        try:
            snap = app.state.queue.snapshot_queue()
            active = len(snap.get("queued", []) or []) + (1 if snap.get("running") else 0)
        except Exception:
            active = 0
        cap = int(getattr(app.state, "_queue_active_cap", 30) or 30)
        if cap > 0 and active >= cap:
            raise HTTPException(
                status_code=429,
                detail={"error_code": "queue_full", "cap": cap, "active": active},
            )
        ip = _get_client_ip(request)
        now = time.time()
        min_interval = float(getattr(app.state, "_rate_min_interval_s", 5.0) or 5.0)
        compare_key = str(payload.get("_aceradio_compare_key") or payload.get("_aceradio_compare_key") or "").strip()
        compare_step = str(payload.get("_aceradio_compare_step") or payload.get("_aceradio_compare_step") or "").strip().upper()
        allow_compare_followup = False
        if min_interval > 0:
            with app.state._rate_lock:
                compare_windows = getattr(app.state, "_ab_compare_window_by_ip", {})
                ip_windows = compare_windows.get(ip)
                if isinstance(ip_windows, dict):
                    compare_windows[ip] = {
                        k: v for k, v in ip_windows.items()
                        if isinstance(v, (int, float)) and (now - float(v)) <= max(min_interval * 2.0, 10.0)
                    }
                else:
                    compare_windows[ip] = {}
                if compare_key and compare_step == "B":
                    started_at = compare_windows[ip].get(compare_key)
                    if isinstance(started_at, (int, float)) and (now - float(started_at)) <= max(min_interval * 2.0, 10.0):
                        allow_compare_followup = True
                last = float(app.state._last_job_at_by_ip.get(ip, 0.0) or 0.0)
                if (not allow_compare_followup) and ((now - last) < min_interval):
                    wait_s = max(0.0, min_interval - (now - last))
                    raise HTTPException(
                        status_code=429,
                        detail={"error_code": "rate_limited", "retry_after_s": round(float(wait_s), 2)},
                    )
                app.state._last_job_at_by_ip[ip] = now
                if compare_key and compare_step == "A":
                    compare_windows[ip][compare_key] = now
                elif compare_key and compare_step == "B":
                    compare_windows[ip].pop(compare_key, None)
        cleanup_ttl_seconds = _get_cleanup_ttl_seconds()
        if cleanup_ttl_seconds <= 0:
            logger.info("[cleanup] disabled ttl={}s via environment flag", cleanup_ttl_seconds)
        else:
            try:
                rep = cleanup_old_job_dirs(Path(results_root), cleanup_ttl_seconds)
                logger.info(
                    "[cleanup] ttl={}s scanned={} deleted={} skipped={} errors={}",
                    cleanup_ttl_seconds,
                    rep.get("scanned", 0),
                    rep.get("deleted", 0),
                    rep.get("skipped", 0),
                    rep.get("errors", 0),
                )
            except Exception as exc:
                logger.warning("[cleanup] exception err={!r}", exc)
            try:
                _ensure_logs_dir()
                repl = cleanup_old_log_files(Path(logs_dir), cleanup_ttl_seconds)
                _ensure_logs_dir()
                logger.info(
                    "[cleanup_logs] ttl={}s scanned={} deleted={} skipped={} errors={}",
                    cleanup_ttl_seconds,
                    repl.get("scanned", 0),
                    repl.get("deleted", 0),
                    repl.get("skipped", 0),
                    repl.get("errors", 0),
                )
            except Exception as exc:
                logger.warning("[cleanup_logs] exception err={!r}", exc)
        q: InProcessJobQueue = app.state.queue
        caption = (payload.get("caption") or "").strip()
        lyrics = (payload.get("lyrics") or "").strip()
        instrumental = bool(payload.get("instrumental", False))
        thinking = bool(payload.get("thinking", True))
        duration_auto = bool(payload.get("duration_auto", False))
        bpm_auto = bool(payload.get("bpm_auto", False))
        key_auto = bool(payload.get("key_auto", False))
        timesig_auto = bool(payload.get("timesig_auto", False))
        language_auto = bool(payload.get("language_auto", False))
        duration = payload.get("duration", max_duration)
        seed = payload.get("seed", -1)
        lora_id = (payload.get("lora_id") or "").strip()
        lora_trigger = (payload.get("lora_trigger") or payload.get("lora_tag") or "").strip()
        lora_weight = _parse_lora_weight_value(payload.get("lora_weight", 0.5), default=0.5)
        _keys = sorted([str(k) for k in payload.keys()]) if isinstance(payload, dict) else []
        payload.pop('_aceradio_compare_key', None)
        payload.pop('_aceradio_compare_key', None)
        payload.pop('_aceradio_compare_step', None)
        payload.pop('_aceradio_compare_step', None)
        _src_audio = str(payload.get('src_audio') or '').strip()
        logger.info(
            "[api/jobs] summary mode={!r} src_present={} lora_id={!r} lora_weight={!r}",
            str(payload.get('generation_mode') or ''),
            bool(_src_audio),
            lora_id,
            payload.get('lora_weight', None),
        )
        logger.debug(f"[api/jobs] payload keys={_keys}")
        logger.debug(f"[api/jobs] lora id={lora_id!r} trigger={lora_trigger!r} weight={payload.get('lora_weight', None)!r}")
        model_choice = _normalize_model_choice(payload.get("model"))
        lora_entry = None
        if lora_id:
            if (".." in lora_id) or ("/" in lora_id) or ("\\" in lora_id):
                raise HTTPException(status_code=400, detail="LoRA non valido.")
            catalog = (getattr(app.state, "_lora_catalog", []) or [])
            by_id = {str(it.get("id", "") or ""): it for it in catalog if isinstance(it, dict)}
            by_id.pop("", None)
            lora_entry = by_id.get(lora_id)
            if not lora_entry:
                logger.warning(f"[LoRA] Rejected unknown id='{lora_id}'. Valid: {sorted(by_id.keys())}")
                raise HTTPException(status_code=400, detail="LoRA non valido.")
            if not lora_trigger:
                try:
                    cat_trigger = str((lora_entry.get("trigger", lora_entry.get("tag", "")) ) or "").strip()
                except Exception:
                    cat_tag = ""
                if cat_trigger:
                    lora_trigger = cat_trigger
                    logger.info("[LoRA] compat: missing lora_trigger -> using catalog trigger")
                else:
                    lora_trigger = lora_id
                    logger.info("[LoRA] compat: missing catalog trigger -> using lora_id as trigger")
            try:
                canonical_trigger = str((lora_entry.get("trigger", lora_entry.get("tag", "")) ) or "").strip()
            except Exception:
                canonical_tag = ""
            if canonical_trigger and lora_trigger != canonical_trigger:
                logger.warning(
                    f"[LoRA] overriding client lora_trigger={lora_trigger!r} with catalog trigger={canonical_trigger!r} for id={lora_id!r}"
                )
                lora_trigger = canonical_trigger
        if not lora_id:
            lora_trigger = ""
        batch_size = payload.get("batch_size", 1)
        audio_format = payload.get("audio_format", "flac")
        inference_steps = payload.get("inference_steps", None)
        infer_method = str(payload.get("infer_method") or "ode").strip().lower()
        timesteps = payload.get("timesteps", None)
        source_start = payload.get("source_start", None)
        source_end = payload.get("source_end", None)
        guidance_scale = payload.get("guidance_scale", None)
        shift = payload.get("shift", None)
        use_adg = payload.get("use_adg", False)
        cfg_interval_start = payload.get("cfg_interval_start", None)
        cfg_interval_end = payload.get("cfg_interval_end", None)
        enable_normalization = bool(payload.get("enable_normalization", True))
        normalization_db = payload.get("normalization_db", None)
        score_scale = payload.get("score_scale", 0.5)
        try:
            score_scale = float(score_scale)
        except Exception:
            score_scale = 0.5
        score_scale = max(0.01, min(score_scale, 1.0))
        auto_score = bool(payload.get("auto_score", False))
        latent_shift = payload.get("latent_shift", None)
        latent_rescale = payload.get("latent_rescale", None)
        bpm = payload.get("bpm", None)
        keyscale = payload.get("keyscale", "")
        timesignature = (payload.get("timesignature") or "").strip()
        vocal_language = (payload.get("vocal_language") or "unknown").strip()
        generation_mode = str(payload.get("generation_mode") or "Custom").strip()
        if generation_mode not in {"Simple", "Custom"}:
            generation_mode = "Custom"
        source_start = None
        source_end = None
        payload.pop("source_start", None)
        payload.pop("source_end", None)
        task_type = "text2music"
        src_audio = ""
        payload["src_audio"] = ""
        if duration_auto:
            duration = -1
        if bpm_auto:
            bpm = None
        if key_auto:
            keyscale = ""
        if timesig_auto:
            timesignature = ""
        if language_auto:
            vocal_language = "unknown"
        if len(caption) > 50000:
            raise HTTPException(status_code=400, detail="Stile troppo lungo (max 50000 caratteri).")
        if len(lyrics) > 20000:
            raise HTTPException(status_code=400, detail="Testo troppo lungo (max 20000 caratteri).")
        try:
            bs = int(batch_size)
        except Exception:
            bs = 1
        if bs < 1 or bs > 4:
            raise HTTPException(status_code=400, detail="Batch size non valido (consentito: 1–4).")
        try:
            d = float(duration)
        except Exception:
            d = float(max_duration)
        if int(d) != -1:
            if d < 10 or d > float(max_duration):
                raise HTTPException(status_code=400, detail=f"Durata non valida (10–{max_duration} secondi).")
        else:
            d = -1
        af = str(audio_format).lower().strip()
        if af not in ("mp3", "wav", "flac", "wav32", "opus", "aac"):
            raise HTTPException(status_code=400, detail="Formato audio non valido.")
        if timesignature not in {"", "2/4", "3/4", "4/4", "6/8"}:
            timesignature = ""
        if vocal_language not in set(VALID_LANGUAGES):
            vocal_language = "unknown"
        if instrumental:
            lyrics = "[Instrumental]"
            vocal_language = "unknown"
            thinking = False
            auto_score = False
        try:
            logger.info(
                "[api/jobs] metas duration=%r duration_auto=%r bpm=%r bpm_auto=%r keyscale=%r key_auto=%r timesignature=%r timesig_auto=%r vocal_language=%r language_auto=%r"
                % (d, duration_auto, bpm, bpm_auto, keyscale, key_auto, timesignature, timesig_auto, vocal_language, language_auto)
            )
        except Exception:
            pass
        st = q.submit(
            job_id,
            {
                "model": model_choice,
                "generation_mode": generation_mode,
                "task_type": task_type,
                "src_audio": src_audio,
                "caption": caption,
                "lyrics": lyrics,
                "instrumental": instrumental,
                "thinking": thinking,
                "duration": d,
                "duration_auto": duration_auto,
                "seed": seed,
                "lora_id": lora_id,
                "lora_trigger": lora_trigger,
                "lora_weight": lora_weight,
                "batch_size": batch_size,
                "audio_format": audio_format,
                "mp3_bitrate": payload.get("mp3_bitrate", None),
                "mp3_sample_rate": payload.get("mp3_sample_rate", None),
                "inference_steps": inference_steps,
                "infer_method": infer_method,
                "timesteps": timesteps,
                "source_start": source_start,
                "source_end": source_end,
                "guidance_scale": guidance_scale,
                "shift": shift,
                "use_adg": use_adg,
                "cfg_interval_start": cfg_interval_start,
                "cfg_interval_end": cfg_interval_end,
                "enable_normalization": enable_normalization,
                "normalization_db": normalization_db,
                "score_scale": score_scale,
                "auto_score": auto_score,
                "latent_shift": latent_shift,
                "latent_rescale": latent_rescale,
                "bpm": bpm,
                "bpm_auto": bpm_auto,
                "keyscale": keyscale,
                "key_auto": key_auto,
                "timesignature": timesignature,
                "timesig_auto": timesig_auto,
                "vocal_language": vocal_language,
                "language_auto": language_auto,
                "lm_temperature": payload.get("lm_temperature", 0.85),
                "lm_cfg_scale": payload.get("lm_cfg_scale", 2.0),
                "lm_top_k": payload.get("lm_top_k", 0),
                "lm_top_p": payload.get("lm_top_p", 0.9),
                "lm_negative_prompt": payload.get("lm_negative_prompt", "NO USER INPUT"),
                "use_constrained_decoding": bool(payload.get("use_constrained_decoding", True)) and bool(thinking) and not bool(instrumental),
                "use_cot_metas": bool(payload.get("use_cot_metas", thinking)) and bool(thinking) and not bool(instrumental),
                "use_cot_caption": bool(payload.get("use_cot_caption", thinking)) and bool(thinking) and not bool(instrumental),
                "use_cot_language": bool(payload.get("use_cot_language", thinking)) and bool(thinking) and not bool(instrumental),
                "parallel_thinking": bool(payload.get("parallel_thinking", False)) and bool(thinking) and not bool(instrumental),
                "constrained_decoding_debug": bool(payload.get("constrained_decoding_debug", False)) and bool(thinking) and not bool(instrumental),
            },
        )
        return {
            "job_id": job_id,
            "status": st.status,
            "position": st.position,
        }

    @app.post("/api/jobs/{job_id}/cancel")

    def cancel_job(job_id: str, request: Request):

        _require_token(request)
        q: InProcessJobQueue = app.state.queue
        st = q.cancel(job_id)
        if not st:
            raise HTTPException(status_code=404, detail="Job non trovato")
        if st.status == "running":
            raise HTTPException(status_code=409, detail={"error_code": "job_not_cancelable", "status": "running"})
        if st.status not in ("queued", "cancelled"):
            raise HTTPException(status_code=409, detail={"error_code": "job_not_cancelable", "status": st.status})
        return {
            "job_id": st.job_id,
            "status": "cancelled",
            "position": 0,
        }

    @app.get("/api/jobs/{job_id}")

    def get_job(job_id: str, request: Request):

        _require_token(request)
        q: InProcessJobQueue = app.state.queue
        st = q.get(job_id)
        if not st:
            raise HTTPException(status_code=404, detail="Job non trovato")
        out = {
            "job_id": st.job_id,
            "status": st.status,
            "position": st.position,
            "created_at": st.created_at,
            "started_at": st.started_at,
            "finished_at": st.finished_at,
            "error": st.error,
        }
        if st.status == "done" and st.result:
            audio_paths = st.result.get("audio_paths") or []
            audio_count = max(1, int(st.result.get("audio_count", 1)))
            _rseeds = []
            _json_path = st.result.get("json_path", "")
            if _json_path and os.path.exists(_json_path):
                try:
                    import json as _json
                    with open(_json_path, "r", encoding="utf-8") as _jf:
                        _jdata = _json.load(_jf)
                    _rseeds = (_jdata.get("result") or {}).get("resolved_seeds") or []
                except Exception:
                    pass
            if not _rseeds:
                _req_seed = -1
                _req_batch = 1
                try:
                    _req_seed = int((st.result.get("request") or {}).get("seed", -1) or -1)
                except Exception:
                    pass
                try:
                    _req_batch = int((st.result.get("request") or {}).get("batch_size", 1) or 1)
                except Exception:
                    pass
                if _req_seed >= 0 and _req_batch == 1:
                    _rseeds = [_req_seed] * audio_count
                else:
                    _rseeds = [-1] * audio_count
            out["result"] = {
                "seconds": st.result.get("seconds"),
                "audio_urls": [f"/download/{job_id}/audio/{i}" for i in range(audio_count)],
                "audio_filenames": [os.path.basename(str(p or "")) for p in audio_paths[:audio_count]],
                "audio_resolved_seeds": _rseeds[:audio_count],
                "audio_paths": audio_paths[:audio_count],
                "json_url": f"/download/{job_id}/json",
            }
        return out

    @app.get("/api/queue")

    def queue_status():

        q: InProcessJobQueue = app.state.queue
        return q.snapshot_queue()

    @app.get("/download/{job_id}/audio")

    def download_audio_first(job_id: str, request: Request):

        _require_token(request)
        return download_audio_index(job_id, 0)

    @app.get("/download/{job_id}/audio/{idx}")

    def download_audio_index(job_id: str, idx: int, request: Request):

        _require_token(request)
        q: InProcessJobQueue = app.state.queue
        st = q.get(job_id)
        if not st or st.status != "done" or not st.result:
            raise HTTPException(status_code=404, detail="File non disponibile")
        audio_paths = st.result.get("audio_paths") or []
        if not isinstance(audio_paths, list) or len(audio_paths) == 0:
            raise HTTPException(status_code=404, detail="Audio non trovato")
        try:
            idx = int(idx)
        except Exception:
            idx = 0
        if idx < 0 or idx >= len(audio_paths):
            raise HTTPException(status_code=404, detail="Indice audio non valido")
        audio_path = audio_paths[idx]
        if not audio_path or not os.path.exists(audio_path):
            raise HTTPException(status_code=404, detail="Audio non trovato")
        filename = os.path.basename(audio_path)
        lower = audio_path.lower()
        if lower.endswith(".flac"):
            media_type = "audio/flac"
        elif lower.endswith(".wav") or lower.endswith(".wave"):
            media_type = "audio/wav"
        elif lower.endswith(".opus") or lower.endswith(".ogg"):
            media_type = "audio/ogg"
        elif lower.endswith(".aac") or lower.endswith(".m4a"):
            media_type = "audio/aac"
        else:
            media_type = "audio/mpeg"
        return FileResponse(
            path=audio_path,
            media_type=media_type,
            filename=filename,
        )

    @app.get("/download/{job_id}/json")

    def download_json(job_id: str, request: Request):

        _require_token(request)
        q: InProcessJobQueue = app.state.queue
        st = q.get(job_id)
        if not st or st.status != "done" or not st.result:
            raise HTTPException(status_code=404, detail="File non disponibile")
        json_path = st.result.get("json_path")
        if not json_path or not os.path.exists(json_path):
            raise HTTPException(status_code=404, detail="JSON non trovato")
        audio_paths = st.result.get("audio_paths") or []
        download_name = "metadata.json"
        if isinstance(audio_paths, list) and audio_paths:
            first_audio = str(audio_paths[0] or "").strip()
            if first_audio:
                audio_name = os.path.basename(first_audio)
                root, _ext = os.path.splitext(audio_name)
                if root:
                    download_name = f"{root}.json"
        return FileResponse(
            path=json_path,
            media_type="application/json",
            filename=download_name,
        )
    return app
