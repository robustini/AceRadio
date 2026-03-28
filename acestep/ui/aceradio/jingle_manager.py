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

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

JINGLE_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.ogg', '.opus', '.aac', '.m4a'}

DEFAULT_JINGLE_FIELDS: dict[str, Any] = {
    'enabled': True,
    'every_n_songs': 3,
    'volume': 1.0,
    'last_played_at': None,
    'play_count': 0,
}

def _jingle_root(project_root: str) -> Path:
    return Path(project_root) / 'aceradio_jingles'

def _jingle_config_path(project_root: str) -> Path:
    return _jingle_root(project_root) / 'jingles.json'

def _compound_key(mode: str, filename: str) -> str:
    return f"{mode}::{filename}"

def _scan_folder(folder: Path) -> list[str]:
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        f.name for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in JINGLE_AUDIO_EXTENSIONS
    )

class JingleManager:

    def __init__(self, project_root: str):
        self._root = _jingle_root(project_root)
        self._config_path = _jingle_config_path(project_root)
        self._overlay_dir = self._root / 'overlay'
        self._separator_dir = self._root / 'separator'
        self._jingles: dict[str, dict[str, Any]] = {}
        self._rr_index: dict[str, int] = {'overlay': 0, 'separator': 0}
        self._songs_since: dict[str, int] = {}

    def ensure_dirs(self) -> None:
        self._overlay_dir.mkdir(parents=True, exist_ok=True)
        self._separator_dir.mkdir(parents=True, exist_ok=True)

    def reload(self) -> None:
        self.ensure_dirs()
        existing = self._load_raw()
        changed = False
        new_config: dict[str, dict[str, Any]] = {}

        for mode, folder in [('overlay', self._overlay_dir), ('separator', self._separator_dir)]:
            for fname in _scan_folder(folder):
                key = _compound_key(mode, fname)
                if key in existing:
                    entry = dict(existing[key])
                else:
                    entry = dict(DEFAULT_JINGLE_FIELDS)
                    changed = True
                entry['filename'] = fname
                entry['mode'] = mode
                new_config[key] = entry

        self._jingles = new_config
        for key in new_config:
            if key not in self._songs_since:
                self._songs_since[key] = 0
        stale = [k for k in self._songs_since if k not in new_config]
        for k in stale:
            del self._songs_since[k]
        if changed:
            self._save_raw()

        ov_n = sum(1 for k in self._jingles if k.startswith('overlay::'))
        sep_n = sum(1 for k in self._jingles if k.startswith('separator::'))
        logger.info('[JingleManager] loaded %d jingles (%d overlay, %d separator)',
                    len(self._jingles), ov_n, sep_n)

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self._config_path.exists():
            return {}
        try:
            raw = json.loads(self._config_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            logger.warning('[JingleManager] failed to parse jingles.json')
        return {}

    def _save_raw(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._config_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(self._jingles, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self._config_path)
        except Exception:
            logger.exception('[JingleManager] failed to save jingles.json')

    def all_jingles(self) -> list[dict[str, Any]]:
        return sorted(
            [dict(j) for j in self._jingles.values()],
            key=lambda j: (j.get('mode', ''), j.get('filename', ''))
        )

    def get_jingle(self, filename: str, mode: str) -> Optional[dict[str, Any]]:
        entry = self._jingles.get(_compound_key(mode, filename))
        return dict(entry) if entry else None

    def update_jingle(self, filename: str, mode: str,
                      updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        key = _compound_key(mode, filename)
        entry = self._jingles.get(key)
        if entry is None:
            return None
        allowed = {'enabled', 'every_n_songs', 'volume'}
        for k, v in updates.items():
            if k in allowed:
                entry[k] = v
        self._save_raw()
        return dict(entry)

    def record_played(self, filename: str, mode: str) -> None:
        key = _compound_key(mode, filename)
        entry = self._jingles.get(key)
        if entry is None:
            return
        entry['last_played_at'] = time.time()
        entry['play_count'] = int(entry.get('play_count', 0)) + 1
        self._songs_since[key] = 0
        self._save_raw()

    def audio_path(self, filename: str, mode: str) -> Optional[Path]:
        folder = self._overlay_dir if mode == 'overlay' else self._separator_dir
        p = folder / filename
        return p if p.exists() else None

    def increment_song_counters(self, mode: str) -> None:
        prefix = f"{mode}::"
        for key in list(self._songs_since):
            if key.startswith(prefix):
                self._songs_since[key] += 1

    def pick_eligible(self, mode: str) -> Optional[dict[str, Any]]:
        candidates = [
            j for j in self._jingles.values()
            if j.get('mode') == mode
            and j.get('enabled', True)
            and self._songs_since.get(_compound_key(mode, j['filename']), 0)
               >= int(j.get('every_n_songs', 3))
        ]
        if not candidates:
            return None
        idx = self._rr_index.get(mode, 0) % len(candidates)
        chosen = candidates[idx]
        self._rr_index[mode] = (idx + 1) % len(candidates)
        return dict(chosen)

    def runtime_state(self) -> dict[str, Any]:
        return {
            'jingles': self.all_jingles(),
            'overlay_dir': str(self._overlay_dir),
            'separator_dir': str(self._separator_dir),
            'config_path': str(self._config_path),
            'count': len(self._jingles),
            'overlay_count': sum(1 for k in self._jingles if k.startswith('overlay::')),
            'separator_count': sum(1 for k in self._jingles if k.startswith('separator::')),
        }
