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

import io
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf

try:
    from pydub import AudioSegment
except Exception:
    AudioSegment = None

try:
    from pedalboard import load_plugin
except Exception:
    load_plugin = None

@dataclass
class DspProcessResult:
    ok: bool
    audio_bytes: bytes
    audio_mime: str
    reason: str = ''
    sample_rate: int = 0
    channels: int = 0
    plugin_name: str = ''

class MasterDspHost:
    def __init__(self):
        self._lock = threading.RLock()
        self._plugin_path = ''
        self._plugin = None
        self._plugin_name = ''
        self._last_error = ''
        self._last_ok = 0.0
        self._supported = callable(load_plugin)
        self._editor_hostable = False

    @property
    def supported(self) -> bool:
        return bool(self._supported)

    def support_reason(self) -> str:
        if self.supported:
            return 'Pedalboard VST3 host available'
        return 'Pedalboard not installed in venv'

    def status(self, *, enabled: bool, bypass: bool, plugin_path: str) -> dict[str, Any]:
        with self._lock:
            normalized = self._normalize_plugin_path(plugin_path)
            loaded = bool(self._plugin is not None and self._plugin_path and self._plugin_path == normalized)
            active = bool(enabled and not bypass and loaded and self.supported)
            return {
                'supported': self.supported,
                'support_reason': self.support_reason(),
                'editor_hostable': self._editor_hostable,
                'enabled_requested': bool(enabled),
                'bypass': bool(bypass),
                'plugin_path': normalized,
                'plugin_loaded': loaded,
                'active': active,
                'plugin_name': self._plugin_name if loaded else '',
                'last_error': self._last_error,
                'last_ok': self._last_ok,
                'mode': 'master_output_render',
            }

    def _normalize_plugin_path(self, plugin_path: str) -> str:
        raw = str(plugin_path or '').strip()
        if not raw:
            return ''
        try:
            return str(Path(raw).resolve())
        except Exception:
            return os.path.normpath(raw)

    def reset(self) -> None:
        with self._lock:
            self._plugin = None
            self._plugin_path = ''
            self._plugin_name = ''
            self._last_error = ''

    def configure(self, plugin_path: str) -> bool:
        normalized = self._normalize_plugin_path(plugin_path)
        with self._lock:
            if not normalized:
                self._plugin = None
                self._plugin_path = ''
                self._plugin_name = ''
                self._last_error = ''
                return False
            if not self.supported:
                self._plugin = None
                self._plugin_path = normalized
                self._plugin_name = ''
                self._last_error = self.support_reason()
                return False
            if self._plugin is not None and self._plugin_path == normalized:
                return True
            try:
                plugin = load_plugin(normalized)
                self._plugin = plugin
                self._plugin_path = normalized
                self._plugin_name = str(getattr(plugin, 'name', '') or Path(normalized).stem)
                self._last_error = ''
                return True
            except Exception as exc:
                self._plugin = None
                self._plugin_path = normalized
                self._plugin_name = ''
                self._last_error = str(exc)
                return False

    def process(self, audio_bytes: bytes, audio_mime: str, *, enabled: bool, bypass: bool, plugin_path: str) -> DspProcessResult:
        if not audio_bytes:
            return DspProcessResult(ok=False, audio_bytes=audio_bytes, audio_mime=audio_mime, reason='Empty audio buffer')
        if not enabled or bypass:
            return DspProcessResult(ok=False, audio_bytes=audio_bytes, audio_mime=audio_mime, reason='DSP disabled or bypassed')
        if not self.configure(plugin_path):
            return DspProcessResult(ok=False, audio_bytes=audio_bytes, audio_mime=audio_mime, reason=self._last_error or 'Plugin unavailable')
        with self._lock:
            plugin = self._plugin
            plugin_name = self._plugin_name
        if plugin is None:
            return DspProcessResult(ok=False, audio_bytes=audio_bytes, audio_mime=audio_mime, reason='Plugin not loaded')
        try:
            data, sample_rate = self._decode(audio_bytes)
            processed = self._run_plugin(plugin, data, sample_rate)
            encoded_bytes, encoded_mime = self._encode(processed, sample_rate, audio_mime)
            with self._lock:
                self._last_ok = time.time()
                self._last_error = ''
            channels = int(processed.shape[0]) if processed.ndim == 2 else 1
            return DspProcessResult(ok=True, audio_bytes=encoded_bytes, audio_mime=encoded_mime, sample_rate=int(sample_rate), channels=channels, plugin_name=plugin_name)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return DspProcessResult(ok=False, audio_bytes=audio_bytes, audio_mime=audio_mime, reason=str(exc), plugin_name=plugin_name)

    def _decode(self, audio_bytes: bytes) -> tuple[np.ndarray, int]:
        try:
            with io.BytesIO(audio_bytes) as bio:
                data, sample_rate = sf.read(bio, dtype='float32', always_2d=True)
            if data.ndim != 2 or data.size == 0:
                raise RuntimeError('Invalid decoded audio')
            data = np.ascontiguousarray(data.T)
            return data, int(sample_rate)
        except Exception:
            if AudioSegment is None:
                raise
            segment = AudioSegment.from_file(io.BytesIO(audio_bytes))
            sample_rate = int(segment.frame_rate)
            channels = int(segment.channels)
            samples = np.array(segment.get_array_of_samples())
            if channels > 1:
                samples = samples.reshape((-1, channels))
            else:
                samples = samples.reshape((-1, 1))
            scale = float(1 << (8 * segment.sample_width - 1))
            data = (samples.astype(np.float32) / scale).T
            return np.ascontiguousarray(data), sample_rate

    def _run_plugin(self, plugin: Any, data: np.ndarray, sample_rate: int) -> np.ndarray:
        block_size = 8192
        channels, total = data.shape
        output = np.zeros_like(data)
        with self._lock:
            if hasattr(plugin, 'reset'):
                try:
                    plugin.reset()
                except Exception:
                    pass
        for start in range(0, total, block_size):
            end = min(total, start + block_size)
            chunk = np.ascontiguousarray(data[:, start:end])
            try:
                processed = plugin(chunk, sample_rate, reset=False)
            except TypeError:
                processed = plugin(chunk, sample_rate)
            if processed is None:
                processed = chunk
            processed = np.asarray(processed, dtype=np.float32)
            if processed.ndim == 1:
                processed = processed.reshape(1, -1)
            if processed.shape[0] != channels:
                if processed.shape[0] == 1 and channels > 1:
                    processed = np.repeat(processed, channels, axis=0)
                elif channels == 1 and processed.shape[0] > 1:
                    processed = np.mean(processed, axis=0, keepdims=True)
                else:
                    raise RuntimeError('Plugin channel count mismatch')
            if processed.shape[1] != (end - start):
                if processed.shape[1] > (end - start):
                    processed = processed[:, : end - start]
                else:
                    padded = np.zeros((processed.shape[0], end - start), dtype=np.float32)
                    padded[:, : processed.shape[1]] = processed
                    processed = padded
            output[:, start:end] = np.clip(processed, -1.0, 1.0)
        return output

    def _encode(self, data: np.ndarray, sample_rate: int, audio_mime: str) -> tuple[bytes, str]:
        mime = str(audio_mime or 'audio/wav').lower()
        if 'flac' in mime:
            fmt = 'FLAC'
            out_mime = 'audio/flac'
        elif 'mpeg' in mime or 'mp3' in mime or 'aac' in mime or 'opus' in mime:
            fmt = 'WAV'
            out_mime = 'audio/wav'
        else:
            fmt = 'WAV'
            out_mime = 'audio/wav'
        with io.BytesIO() as bio:
            sf.write(bio, data.T, sample_rate, format=fmt)
            return bio.getvalue(), out_mime
