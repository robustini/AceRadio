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

import contextlib
import multiprocessing as mp
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import numpy as np
except Exception:
    np = None


class _Sentinel:
    pass


_EOF = _Sentinel()
_WAIT = _Sentinel()
_SNAPSHOT_INTERVAL_S = 0.2
_PARENT_STALE_TIMEOUT_S = max(1.0, _SNAPSHOT_INTERVAL_S * 6.0)


class PCMChunkDecoder:
    def __init__(self, audio_path: str, sample_rate: int, chunk_bytes: int, start_sec: float = 0.0, prefetch_chunks: int = 64, playback_rate: float = 1.0):
        self.audio_path = str(audio_path or '')
        self.sample_rate = int(sample_rate)
        self.chunk_bytes = int(chunk_bytes)
        self.start_sec = max(0.0, float(start_sec or 0.0))
        self.prefetch_chunks = max(8, int(prefetch_chunks or 64))
        try:
            self.playback_rate = max(0.5, min(2.0, float(playback_rate or 1.0)))
        except Exception:
            self.playback_rate = 1.0
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.prefetch_chunks)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._error: Optional[Exception] = None
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name=f'ace-radio-decode-{Path(self.audio_path).stem[:24]}', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            p = Path(self.audio_path)
            if not p.exists() or not p.is_file() or p.stat().st_size < 512:
                self._queue.put(_EOF)
                return
            seek_flags = ['-ss', f'{self.start_sec:.3f}'] if self.start_sec > 0.05 else []
            af = ['-af', f'atempo={self.playback_rate:.6f}'] if abs(self.playback_rate - 1.0) > 0.001 else []
            self._proc = subprocess.Popen(
                [
                    'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                    *seek_flags,
                    '-i', str(p),
                    *af,
                    '-f', 's16le',
                    '-ar', str(self.sample_rate),
                    '-ac', '2',
                    'pipe:1',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
            stdout = self._proc.stdout
            if stdout is None:
                self._queue.put(_EOF)
                return
            while not self._stop.is_set():
                chunk = stdout.read(self.chunk_bytes)
                if not chunk:
                    break
                while not self._stop.is_set():
                    try:
                        self._queue.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
            self._queue.put(_EOF)
        except Exception as exc:
            self._error = exc
            with contextlib.suppress(Exception):
                self._queue.put(_EOF)
        finally:
            with contextlib.suppress(Exception):
                if self._proc and self._proc.poll() is None:
                    self._proc.kill()
            with contextlib.suppress(Exception):
                if self._proc:
                    self._proc.wait(timeout=3.0)

    def read_chunk(self, timeout: float = 0.25):
        self.start()
        if self._closed:
            return _EOF
        try:
            item = self._queue.get(timeout=max(0.01, float(timeout or 0.25)))
        except queue.Empty:
            return _WAIT
        if item is _EOF and self._error is not None:
            raise self._error
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with contextlib.suppress(Exception):
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        with contextlib.suppress(Exception):
            if self._proc:
                self._proc.wait(timeout=1.5)


@dataclass
class PlayoutSnapshot:
    running: bool = False
    current_track_id: str = ''
    current_track_title: str = ''
    track_elapsed: float = 0.0
    track_started_wallclock: float = 0.0
    track_started_monotonic: float = 0.0
    stream_started_monotonic: float = 0.0
    last_chunk_monotonic: float = 0.0
    stream_bytes_sent: int = 0
    stream_rate_kbps: float = 0.0
    chunk_count: int = 0
    underrun_count: int = 0
    max_chunk_gap_ms: float = 0.0
    last_chunk_gap_ms: float = 0.0
    jingle_event_id: str = ''
    track_playback_rate: float = 1.0
    engine_pid: int = 0
    engine_mode: str = 'process'
    last_error: str = ''
    ffmpeg_pid: int = 0


class _ChildPlayoutRuntime:
    def __init__(self, command_q, event_q, sample_rate: int, ffmpeg_command: list[str], stream_config: dict[str, Any]):
        self._command_q = command_q
        self._event_q = event_q
        self._sample_rate = int(sample_rate)
        self._ffmpeg_command = [str(x) for x in (ffmpeg_command or [])]
        self._stream_config = dict(stream_config or {})
        self._chunk_bytes = 16384
        self._bytes_per_frame = 4
        self._pcm_bytes_per_sec = float(self._sample_rate * self._bytes_per_frame)
        self._current_track: Optional[dict[str, Any]] = None
        self._next_track: Optional[dict[str, Any]] = None
        self._pending_jingle: Optional[dict[str, Any]] = None
        self._stop = False
        self._state_lock = threading.Lock()
        self._snapshot = PlayoutSnapshot(engine_pid=0, engine_mode='process')
        self._tx_window: list[tuple[float, int]] = []
        self._last_snapshot_emit = 0.0
        self._transport_proc: Optional[subprocess.Popen] = None
        self._transport_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stream_started_emitted = False
        self._stream_stopped_emitted = False
        self._stderr_tail = ''
        self._snapshot_emission_enabled = True
        self._current_track_rate = 1.0

    def _emit_event(self, event: dict[str, Any], *, important: bool = False) -> None:
        try:
            if important:
                self._event_q.put(event, timeout=0.5)
            else:
                self._event_q.put_nowait(event)
        except Exception:
            pass

    def _snapshot_dict(self) -> dict[str, Any]:
        with self._state_lock:
            snap = dict(self._snapshot.__dict__)
            now = time.monotonic()
            cutoff = now - 10.0
            self._tx_window = [(ts, b) for ts, b in self._tx_window if ts >= cutoff]
            if self._tx_window:
                window_bytes = sum(b for _, b in self._tx_window)
                elapsed = max(now - self._tx_window[0][0], 0.1)
                snap['stream_rate_kbps'] = round((window_bytes * 8.0 / elapsed) / 1024.0, 1)
            return snap

    def _emit_snapshot(self, *, force: bool = False) -> None:
        if not self._snapshot_emission_enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_snapshot_emit) < _SNAPSHOT_INTERVAL_S:
            return
        self._last_snapshot_emit = now
        self._emit_event({'type': 'snapshot', 'snapshot': self._snapshot_dict()}, important=False)

    def _set_snapshot(self, **updates: Any) -> None:
        with self._state_lock:
            data = dict(self._snapshot.__dict__)
            data.update(updates)
            self._snapshot = PlayoutSnapshot(**data)

    def _record_output_bytes(self, count: int, now: Optional[float] = None) -> None:
        if count <= 0:
            return
        ts = now or time.monotonic()
        with self._state_lock:
            self._tx_window.append((ts, int(count)))
            data = dict(self._snapshot.__dict__)
            data['stream_bytes_sent'] = int(data.get('stream_bytes_sent') or 0) + int(count)
            data['stream_started_monotonic'] = float(data.get('stream_started_monotonic') or 0.0) or ts
            self._snapshot = PlayoutSnapshot(**data)
        self._emit_snapshot(force=False)

    def _record_chunk(self, count: int, now: float, gap_ms: float) -> None:
        effective_gap_ms = 0.0 if self._snapshot.chunk_count <= 0 else float(gap_ms or 0.0)
        self._set_snapshot(
            running=True,
            last_chunk_monotonic=now,
            chunk_count=int(self._snapshot.chunk_count) + 1,
            last_chunk_gap_ms=effective_gap_ms,
            max_chunk_gap_ms=max(float(self._snapshot.max_chunk_gap_ms or 0.0), effective_gap_ms),
        )
        if self._stream_config.get('transport_backend') != 'shoutcast_socket':
            self._record_output_bytes(int(count), now)
        else:
            self._emit_snapshot(force=False)

    def _mark_track_start(self, track: dict[str, Any], elapsed: float = 0.0) -> None:
        now_m = time.monotonic()
        now_w = time.time()
        self._set_snapshot(
            running=True,
            current_track_id=str(track.get('id', '') or ''),
            current_track_title=str(track.get('song_title', '') or ''),
            track_elapsed=max(0.0, float(elapsed or 0.0)),
            track_started_wallclock=now_w - max(0.0, float(elapsed or 0.0)),
            track_started_monotonic=now_m - max(0.0, float(elapsed or 0.0)) / max(0.5, self._track_playback_rate(track)),
            track_playback_rate=self._track_playback_rate(track),
        )
        self._emit_event({'type': 'track_started', 'track_id': str(track.get('id', '') or '')}, important=False)
        self._emit_snapshot(force=True)

    def _track_playback_rate(self, track: Optional[dict[str, Any]]) -> float:
        try:
            return max(0.5, min(2.0, float((track or {}).get('playback_rate', 1.0) or 1.0)))
        except Exception:
            return 1.0

    def _advance_track_elapsed(self, amount_bytes: int) -> None:
        if amount_bytes <= 0 or self._pcm_bytes_per_sec <= 0:
            return
        rate = max(0.5, min(2.0, float(self._current_track_rate or 1.0)))
        self._set_snapshot(track_elapsed=max(0.0, float(self._snapshot.track_elapsed or 0.0) + ((float(amount_bytes) / self._pcm_bytes_per_sec) * rate)), track_playback_rate=rate)

    def _sleep_until(self, deadline: float) -> float:
        now = time.monotonic()
        ahead = deadline - now
        if ahead > 0:
            time.sleep(ahead)
            return 0.0
        return abs(ahead) * 1000.0

    def _write_pcm(self, data: bytes) -> int:
        proc = self._transport_proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise BrokenPipeError('stream transport stdin unavailable')
        written = proc.stdin.write(data)
        if written is None:
            written = len(data)
        with contextlib.suppress(Exception):
            proc.stdin.flush()
        return int(written)

    def _emit_pcm(self, data: bytes, *, track_bytes: int = 0, deadline: float = 0.0) -> float:
        if not data or self._stop:
            return deadline
        gap_ms = self._sleep_until(deadline) if deadline > 0 else 0.0
        written = self._write_pcm(data)
        if track_bytes > 0:
            self._advance_track_elapsed(track_bytes)
        now = time.monotonic()
        self._record_chunk(written, now, gap_ms)
        duration = float(len(data)) / self._pcm_bytes_per_sec if self._pcm_bytes_per_sec > 0 else 0.0
        return max(deadline, now) + duration

    def _apply_gain(self, raw: bytes, gain: float) -> bytes:
        import array
        arr = array.array('h', raw)
        for i in range(len(arr)):
            value = int(arr[i] * gain)
            arr[i] = max(-32768, min(32767, value))
        return arr.tobytes()

    def _mix_pcm(self, song_raw: bytes, jingle_raw: bytes, song_gain: float, jingle_gain: float) -> bytes:
        if np is not None:
            s = np.frombuffer(song_raw, dtype='<i2').astype(np.float32)
            j = np.frombuffer(jingle_raw, dtype='<i2').astype(np.float32)
            if len(j) < len(s):
                j = np.pad(j, (0, len(s) - len(j)))
            elif len(j) > len(s):
                j = j[:len(s)]
            return np.clip(s * song_gain + j * jingle_gain, -32768, 32767).astype('<i2').tobytes()
        import array
        n = len(song_raw) // 2
        s = array.array('h', song_raw[: n * 2])
        jb = jingle_raw[: n * 2].ljust(n * 2, b'\x00')
        j = array.array('h', jb)
        out = array.array('h', [0] * n)
        for i in range(n):
            value = int(s[i] * song_gain) + int(j[i] * jingle_gain)
            out[i] = max(-32768, min(32767, value))
        return out.tobytes()

    def _warm_decoder_for_track(self, track: Optional[dict[str, Any]], existing_id: str) -> tuple[str, Optional[PCMChunkDecoder]]:
        track_id = str((track or {}).get('id', '') or '')
        audio_path = str((track or {}).get('audio_path', '') or '')
        if not track_id or not audio_path or track_id == existing_id:
            return existing_id, None
        decoder = PCMChunkDecoder(audio_path, self._sample_rate, self._chunk_bytes, 0.0, 96, self._track_playback_rate(track))
        decoder.start()
        return track_id, decoder

    def _read_decoder(self, decoder: Optional[PCMChunkDecoder], timeout: float = 0.2):
        if decoder is None:
            return _WAIT
        return decoder.read_chunk(timeout=timeout)

    def _finish_jingle(self, jingle_ev: dict[str, Any], current_track_id: str) -> None:
        event_id = str(jingle_ev.get('event_id', '') or '')
        self._set_snapshot(jingle_event_id='')
        self._emit_event(
            {
                'type': 'jingle_ended',
                'event_id': event_id,
                'mode': str(jingle_ev.get('mode', '') or ''),
                'filename': str(jingle_ev.get('filename', '') or ''),
                'is_transition': bool(jingle_ev.get('is_transition', False)),
                'track_id': current_track_id,
                'ended_at': time.time(),
            },
            important=True,
        )
        self._emit_snapshot(force=True)

    def _play_overlay(self, current_decoder: Optional[PCMChunkDecoder], jingle_ev: dict[str, Any], deadline: float) -> float:
        if current_decoder is None:
            return deadline
        jingle_decoder = PCMChunkDecoder(str(jingle_ev.get('audio_path', '') or ''), self._sample_rate, self._chunk_bytes, 0.0, 48)
        jingle_decoder.start()
        jingle_vol = float(jingle_ev.get('volume', 1.0) or 1.0)
        try:
            pre_duck_chunks = 3
            restore_chunks = 8
            duck_level = 0.35
            for i in range(pre_duck_chunks):
                song_chunk = self._read_decoder(current_decoder, timeout=0.5)
                if song_chunk in {_WAIT, _EOF}:
                    return deadline
                t = float(i + 1) / float(pre_duck_chunks)
                gain = 1.0 + t * (duck_level - 1.0)
                deadline = self._emit_pcm(self._apply_gain(song_chunk, gain), track_bytes=len(song_chunk), deadline=deadline)
            pending_restore: list[bytes] = []
            while not self._stop:
                song_chunk = self._read_decoder(current_decoder, timeout=0.5)
                if song_chunk is _WAIT:
                    continue
                if song_chunk is _EOF:
                    break
                jingle_chunk = jingle_decoder.read_chunk(timeout=0.5)
                if jingle_chunk is _WAIT:
                    continue
                if jingle_chunk is _EOF:
                    pending_restore.append(song_chunk)
                    break
                mixed = self._mix_pcm(song_chunk, jingle_chunk, duck_level, jingle_vol)
                deadline = self._emit_pcm(mixed, track_bytes=len(song_chunk), deadline=deadline)
            while len(pending_restore) < restore_chunks:
                song_chunk = self._read_decoder(current_decoder, timeout=0.5)
                if song_chunk in {_WAIT, _EOF}:
                    break
                pending_restore.append(song_chunk)
            total_restore = max(1, len(pending_restore))
            for idx, chunk in enumerate(pending_restore):
                t = float(idx + 1) / float(total_restore)
                gain = duck_level + t * (1.0 - duck_level)
                deadline = self._emit_pcm(self._apply_gain(chunk, gain), track_bytes=len(chunk), deadline=deadline)
            return deadline
        finally:
            jingle_decoder.close()

    def _play_separator(self, current_decoder: Optional[PCMChunkDecoder], jingle_ev: dict[str, Any], deadline: float) -> float:
        if current_decoder is None:
            return deadline
        jingle_decoder = PCMChunkDecoder(str(jingle_ev.get('audio_path', '') or ''), self._sample_rate, self._chunk_bytes, 0.0, 48)
        jingle_decoder.start()
        jingle_vol = float(jingle_ev.get('volume', 1.0) or 1.0)
        try:
            sep_fade_sec = 0.5
            pre_sep_chunks = 2
            overlap_chunks = 3
            chunk_sec = float(self._chunk_bytes) / self._pcm_bytes_per_sec if self._pcm_bytes_per_sec > 0 else 0.0
            silence = b'\x00' * self._chunk_bytes

            def sep_gain(chunk_idx: int) -> float:
                return max(0.0, 1.0 - (float(chunk_idx) + 0.5) * chunk_sec / sep_fade_sec)

            for i in range(pre_sep_chunks):
                song_chunk = self._read_decoder(current_decoder, timeout=0.5)
                if song_chunk in {_WAIT, _EOF}:
                    return deadline
                deadline = self._emit_pcm(self._apply_gain(song_chunk, sep_gain(i)), track_bytes=len(song_chunk), deadline=deadline)
            for i in range(overlap_chunks):
                song_chunk = self._read_decoder(current_decoder, timeout=0.5)
                if song_chunk is _WAIT:
                    song_chunk = silence
                elif song_chunk is _EOF:
                    song_chunk = silence
                sep_chunk = jingle_decoder.read_chunk(timeout=0.5)
                if sep_chunk is _WAIT:
                    sep_chunk = silence
                elif sep_chunk is _EOF:
                    sep_chunk = silence
                if song_chunk == silence and sep_chunk == silence:
                    return deadline
                mixed = self._mix_pcm(song_chunk, sep_chunk, sep_gain(pre_sep_chunks + i), jingle_vol)
                deadline = self._emit_pcm(mixed, track_bytes=0 if song_chunk == silence else len(song_chunk), deadline=deadline)
            while not self._stop:
                sep_chunk = jingle_decoder.read_chunk(timeout=0.5)
                if sep_chunk is _WAIT:
                    continue
                if sep_chunk is _EOF:
                    break
                deadline = self._emit_pcm(self._apply_gain(sep_chunk, jingle_vol), track_bytes=0, deadline=deadline)
            return deadline
        finally:
            jingle_decoder.close()

    def _handle_command(self, cmd: dict[str, Any]) -> None:
        cmd_type = str(cmd.get('type', '') or '')
        if cmd_type == 'shutdown':
            self._stop = True
            return
        if cmd_type == 'set_current_track':
            self._current_track = cmd.get('track') if isinstance(cmd.get('track'), dict) else None
            return
        if cmd_type == 'set_next_track':
            self._next_track = cmd.get('track') if isinstance(cmd.get('track'), dict) else None
            return
        if cmd_type == 'play_jingle':
            ev = cmd.get('event')
            if isinstance(ev, dict):
                self._pending_jingle = ev
            return
        if cmd_type == 'set_snapshot_emission':
            self._snapshot_emission_enabled = bool(cmd.get('enabled', True))
            if self._snapshot_emission_enabled:
                self._emit_snapshot(force=True)
            return

    def _drain_commands(self) -> None:
        while not self._stop:
            try:
                cmd = self._command_q.get_nowait()
            except queue.Empty:
                break
            self._handle_command(cmd)

    def _emit_stream_started(self) -> None:
        if self._stream_started_emitted:
            return
        self._stream_started_emitted = True
        self._set_snapshot(running=True, stream_started_monotonic=self._snapshot.stream_started_monotonic or time.monotonic())
        self._emit_event({'type': 'stream_started', 'started_at': time.time()}, important=True)
        self._emit_snapshot(force=True)

    def _emit_stream_stopped(self) -> None:
        if self._stream_stopped_emitted:
            return
        self._stream_stopped_emitted = True
        self._set_snapshot(running=False)
        self._emit_event({'type': 'stream_stopped', 'stopped_at': time.time(), 'stderr_tail': self._stderr_tail[-2000:]}, important=True)
        self._emit_snapshot(force=True)

    def _stderr_monitor_loop(self) -> None:
        proc = self._transport_proc
        if proc is None or proc.stderr is None:
            return
        chunks: list[str] = []
        try:
            while not self._stop:
                raw = proc.stderr.read(4096)
                if not raw:
                    break
                chunks.append(raw.decode('utf-8', errors='replace'))
                self._stderr_tail = ''.join(chunks)[-2000:]
            with contextlib.suppress(Exception):
                proc.wait(timeout=10.0)
            if not self._stop and proc.returncode not in (0, None):
                message = self._stderr_tail[-1200:] or f'ffmpeg exited with code {proc.returncode}'
                self._set_snapshot(last_error=message, running=False)
                self._emit_event({'type': 'error', 'message': message}, important=True)
            self._emit_stream_stopped()
        except Exception as exc:
            if not self._stop:
                self._set_snapshot(last_error=str(exc), running=False)
                self._emit_event({'type': 'error', 'message': str(exc)}, important=True)
                self._emit_stream_stopped()

    def _perform_shoutcast_handshake(self, sock: socket.socket) -> None:
        host = str(self._stream_config.get('host', '') or '')
        port = int(self._stream_config.get('port', 0) or 0)
        host_header = f'{host}:{port}' if port not in {80, 443} else host
        fmt = str(self._stream_config.get('format', 'mp3') or 'mp3')
        content_type = 'audio/mpeg' if fmt == 'mp3' else ('audio/aac' if fmt == 'aac' else 'audio/ogg')
        header_lines = [
            f'Host: {host_header}',
            f'icy-name:{str(self._stream_config.get("name", "AceRadio") or "AceRadio")}',
        ]
        genre = str(self._stream_config.get('genre', '') or '').strip()
        if genre:
            header_lines.append(f'icy-genre:{genre}')
        header_lines.append(f'icy-pub:{"1" if bool(self._stream_config.get("public", False)) else "0"}')
        header_lines.append(f'icy-br:{int(self._stream_config.get("bitrate", 128) or 128)}')
        header_lines.append(f'content-type:{content_type}')

        def _decode_response(data: bytes) -> tuple[str, str]:
            text = (data or b'').decode('utf-8', errors='replace').strip()
            return text, text.lower()

        def _validate_response(data: bytes, *, empty_message: str) -> None:
            text, lower = _decode_response(data)
            if not data:
                raise RuntimeError(empty_message)
            if 'invalid password' in lower or 'bad password' in lower:
                raise RuntimeError(text or 'invalid shoutcast password')
            if not (lower.startswith('ok') and len(text) >= 2):
                raise RuntimeError(text or 'unexpected SHOUTcast handshake response')

        sock.sendall((str(self._stream_config.get('password', '') or '') + '\r\n').encode('utf-8', errors='replace'))
        phase1_response = b''
        phase1_timeout = False
        sock.settimeout(1.1)
        try:
            phase1_response = sock.recv(256)
        except socket.timeout:
            phase1_timeout = True
        phase1_text, phase1_lower = _decode_response(phase1_response)
        if phase1_response:
            if 'invalid password' in phase1_lower or 'bad password' in phase1_lower:
                raise RuntimeError(phase1_text or 'invalid shoutcast password')
            if phase1_lower.startswith('ok') and len(phase1_text) >= 2:
                sock.sendall(('\r\n'.join(header_lines) + '\r\n\r\n').encode('utf-8', errors='replace'))
                return

        sock.sendall(('\r\n'.join(header_lines) + '\r\n\r\n').encode('utf-8', errors='replace'))
        sock.settimeout(5.0)
        try:
            response = sock.recv(256)
        except socket.timeout:
            if phase1_timeout:
                raise RuntimeError('timeout waiting for SHOUTcast server response')
            raise RuntimeError(phase1_text or 'unexpected SHOUTcast handshake response')
        if not response and phase1_response:
            _validate_response(phase1_response, empty_message='empty SHOUTcast server response')
            return
        _validate_response(response, empty_message='empty SHOUTcast server response')
    def _shoutcast_sender_loop(self) -> None:
        proc = self._transport_proc
        if proc is None or proc.stdout is None:
            self._set_snapshot(last_error='ffmpeg stdout unavailable', running=False)
            self._emit_event({'type': 'error', 'message': 'ffmpeg stdout unavailable'}, important=True)
            self._stop = True
            return
        host = str(self._stream_config.get('host', '') or '')
        source_port = int(self._stream_config.get('source_port', 0) or 0)
        if source_port <= 0:
            source_port = int(self._stream_config.get('port', 0) or 0) + 1
        try:
            with socket.create_connection((host, source_port), timeout=4.0) as sock:
                self._perform_shoutcast_handshake(sock)
                self._emit_stream_started()
                while not self._stop and proc.poll() is None:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    self._record_output_bytes(len(chunk), time.monotonic())
        except Exception as exc:
            if not self._stop:
                self._set_snapshot(last_error=str(exc), running=False)
                self._emit_event({'type': 'error', 'message': str(exc)}, important=True)
                self._stop = True
                with contextlib.suppress(Exception):
                    if proc and proc.poll() is None:
                        proc.terminate()

    def _start_transport(self) -> None:
        stdout_target = subprocess.PIPE if self._stream_config.get('transport_backend') == 'shoutcast_socket' else subprocess.DEVNULL
        self._transport_proc = subprocess.Popen(
            list(self._ffmpeg_command),
            stdin=subprocess.PIPE,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._set_snapshot(ffmpeg_pid=int(self._transport_proc.pid or 0))
        self._stderr_thread = threading.Thread(target=self._stderr_monitor_loop, name='ace-radio-stream-stderr', daemon=True)
        self._stderr_thread.start()
        if self._stream_config.get('transport_backend') == 'shoutcast_socket':
            self._transport_thread = threading.Thread(target=self._shoutcast_sender_loop, name='ace-radio-stream-socket', daemon=True)
            self._transport_thread.start()
        else:
            self._emit_stream_started()

    def _stop_transport(self) -> None:
        proc = self._transport_proc
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2.0)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        if self._transport_thread and self._transport_thread.is_alive():
            self._transport_thread.join(timeout=2.0)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)
        self._transport_thread = None
        self._stderr_thread = None
        self._transport_proc = None
        self._emit_stream_stopped()

    def run(self) -> None:
        current_decoder: Optional[PCMChunkDecoder] = None
        current_track_id = ''
        current_track_rate = 1.0
        warmed_track_id = ''
        warmed_track_rate = 1.0
        warmed_decoder: Optional[PCMChunkDecoder] = None
        notified_track_end_for = ''
        deadline = time.monotonic()
        self._set_snapshot(running=False, stream_started_monotonic=0.0, engine_pid=getattr(mp.current_process(), 'pid', 0) or 0)
        self._emit_snapshot(force=True)
        try:
            self._start_transport()
            while not self._stop:
                self._drain_commands()
                track = self._current_track
                if track is None:
                    if current_decoder is not None:
                        current_decoder.close()
                        current_decoder = None
                    current_track_id = ''
                    current_track_rate = 1.0
                    self._current_track_rate = 1.0
                    self._set_snapshot(current_track_id='', current_track_title='', track_elapsed=0.0, track_started_wallclock=0.0, track_started_monotonic=0.0, track_playback_rate=1.0)
                    self._emit_snapshot(force=False)
                    silence = b'\x00' * self._chunk_bytes
                    deadline = self._emit_pcm(silence, track_bytes=0, deadline=deadline)
                    continue
                track_id = str(track.get('id', '') or '')
                desired_rate = self._track_playback_rate(track)
                if track_id != current_track_id:
                    notified_track_end_for = ''
                    if current_decoder is not None:
                        current_decoder.close()
                        current_decoder = None
                    if warmed_decoder is not None and warmed_track_id == track_id:
                        current_decoder = warmed_decoder
                        warmed_decoder = None
                        warmed_track_id = ''
                        current_track_rate = warmed_track_rate
                        self._current_track_rate = warmed_track_rate
                    else:
                        current_decoder = PCMChunkDecoder(str(track.get('audio_path', '') or ''), self._sample_rate, self._chunk_bytes, 0.0, 96, desired_rate)
                        current_decoder.start()
                    current_track_id = track_id
                    current_track_rate = desired_rate
                    self._current_track_rate = desired_rate
                    self._mark_track_start(track, 0.0)
                elif current_decoder is not None and abs(desired_rate - current_track_rate) > 0.001:
                    elapsed = max(0.0, float(self._snapshot.track_elapsed or 0.0))
                    current_decoder.close()
                    current_decoder = PCMChunkDecoder(str(track.get('audio_path', '') or ''), self._sample_rate, self._chunk_bytes, elapsed, 96, desired_rate)
                    current_decoder.start()
                    current_track_rate = desired_rate
                    self._current_track_rate = desired_rate
                    now_m = time.monotonic()
                    now_w = time.time()
                    self._set_snapshot(track_started_wallclock=now_w - (elapsed / max(0.5, desired_rate)), track_started_monotonic=now_m - (elapsed / max(0.5, desired_rate)), track_playback_rate=desired_rate)
                next_track = self._next_track
                next_track_id = str((next_track or {}).get('id', '') or '') if next_track else ''
                next_track_rate = self._track_playback_rate(next_track)
                if next_track_id and next_track_id != current_track_id and next_track_id != warmed_track_id:
                    if warmed_decoder is not None:
                        warmed_decoder.close()
                    warmed_track_id, warmed_decoder = self._warm_decoder_for_track(next_track, current_track_id)
                    warmed_track_rate = next_track_rate
                elif next_track_id and warmed_decoder is not None and warmed_track_id == next_track_id and abs(next_track_rate - warmed_track_rate) > 0.001:
                    warmed_decoder.close()
                    warmed_track_id, warmed_decoder = self._warm_decoder_for_track(next_track, current_track_id)
                    warmed_track_rate = next_track_rate
                elif not next_track_id and warmed_decoder is not None:
                    warmed_decoder.close()
                    warmed_decoder = None
                    warmed_track_id = ''
                if self._pending_jingle:
                    jingle_ev = self._pending_jingle
                    self._pending_jingle = None
                    event_id = str(jingle_ev.get('event_id', '') or '')
                    self._set_snapshot(jingle_event_id=event_id)
                    self._emit_event(
                        {
                            'type': 'jingle_started',
                            'event_id': event_id,
                            'mode': str(jingle_ev.get('mode', '') or ''),
                            'filename': str(jingle_ev.get('filename', '') or ''),
                            'is_transition': bool(jingle_ev.get('is_transition', False)),
                            'track_id': current_track_id,
                            'started_at': time.time(),
                        },
                        important=True,
                    )
                    self._emit_snapshot(force=True)
                    mode = str(jingle_ev.get('mode', '') or '')
                    if mode == 'overlay':
                        deadline = self._play_overlay(current_decoder, jingle_ev, deadline)
                    else:
                        deadline = self._play_separator(current_decoder, jingle_ev, deadline)
                    self._finish_jingle(jingle_ev, current_track_id)
                    continue
                chunk = self._read_decoder(current_decoder, timeout=0.25)
                if chunk is _WAIT:
                    self._set_snapshot(underrun_count=int(self._snapshot.underrun_count) + 1)
                    self._emit_snapshot(force=False)
                    continue
                if chunk is _EOF:
                    if track_id and notified_track_end_for != track_id:
                        notified_track_end_for = track_id
                        self._emit_event({'type': 'track_ended', 'track_id': track_id, 'ended_at': time.time()}, important=True)
                    time.sleep(0.05)
                    continue
                deadline = self._emit_pcm(chunk, track_bytes=len(chunk), deadline=deadline)
        except Exception as exc:
            self._set_snapshot(last_error=str(exc), running=False)
            self._emit_event({'type': 'error', 'message': str(exc)}, important=True)
            self._emit_snapshot(force=True)
        finally:
            self._stop = True
            if current_decoder is not None:
                current_decoder.close()
            if warmed_decoder is not None:
                warmed_decoder.close()
            self._stop_transport()
            self._set_snapshot(running=False)
            self._emit_snapshot(force=True)


def _playout_process_main(command_q, event_q, sample_rate: int, ffmpeg_command: list[str], stream_config: dict[str, Any]) -> None:
    runtime = _ChildPlayoutRuntime(
        command_q=command_q,
        event_q=event_q,
        sample_rate=sample_rate,
        ffmpeg_command=ffmpeg_command,
        stream_config=stream_config,
    )
    runtime.run()


class RealtimePlayoutEngine:
    def __init__(
        self,
        on_track_started: Optional[Callable[[str], None]] = None,
        on_track_end: Optional[Callable[[str], None]] = None,
        on_jingle_started: Optional[Callable[[dict[str, Any]], None]] = None,
        on_jingle_ended: Optional[Callable[[dict[str, Any]], None]] = None,
        on_stream_started: Optional[Callable[[dict[str, Any]], None]] = None,
        on_stream_stopped: Optional[Callable[[dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self._on_track_started = on_track_started
        self._on_track_end = on_track_end
        self._on_jingle_started = on_jingle_started
        self._on_jingle_ended = on_jingle_ended
        self._on_stream_started = on_stream_started
        self._on_stream_stopped = on_stream_stopped
        self._on_error = on_error
        self._sample_rate = 0
        self._ctx: Optional[mp.context.BaseContext] = None
        self._command_q = None
        self._event_q = None
        self._process: Optional[mp.Process] = None
        self._event_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._snapshot = PlayoutSnapshot()
        self._snapshot_lock = threading.Lock()
        self._started_event = threading.Event()
        self._last_child_contact_monotonic = 0.0
        self._last_snapshot_monotonic = 0.0
        self._stale_timeout_s = float(_PARENT_STALE_TIMEOUT_S)

    def start(self, ffmpeg_command: list[str], stream_config: dict[str, Any], sample_rate: int) -> None:
        self.stop()
        self._sample_rate = int(sample_rate)
        self._stop.clear()
        self._started_event.clear()
        self._last_child_contact_monotonic = 0.0
        self._last_snapshot_monotonic = 0.0
        self._ctx = mp.get_context('spawn')
        self._command_q = self._ctx.Queue(maxsize=256)
        self._event_q = self._ctx.Queue(maxsize=512)
        self._process = self._ctx.Process(
            target=_playout_process_main,
            args=(self._command_q, self._event_q, self._sample_rate, list(ffmpeg_command or []), dict(stream_config or {})),
            name='ace-radio-playout-process',
            daemon=True,
        )
        self._process.start()
        self._event_thread = threading.Thread(target=self._event_loop, name='ace-radio-playout-events', daemon=True)
        self._event_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._started_event.set()
        if self._command_q is not None:
            with contextlib.suppress(Exception):
                self._command_q.put({'type': 'shutdown'}, timeout=0.2)
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=2.0)
        self._event_thread = None
        if self._process is not None:
            self._process.join(timeout=3.0)
            if self._process.is_alive():
                with contextlib.suppress(Exception):
                    self._process.terminate()
                self._process.join(timeout=2.0)
            self._process = None
        for attr in ('_command_q', '_event_q'):
            obj = getattr(self, attr, None)
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.close()
                setattr(self, attr, None)
        with self._snapshot_lock:
            self._snapshot = PlayoutSnapshot()
            self._last_child_contact_monotonic = 0.0
            self._last_snapshot_monotonic = 0.0

    def wait_until_started(self, timeout: float = 2.5) -> bool:
        self._started_event.wait(timeout=max(0.1, float(timeout or 0.0)))
        snap = self.snapshot()
        return bool(snap.get('running') and snap.get('playback_authoritative') and not snap.get('last_error'))

    def is_running(self) -> bool:
        snap = self.snapshot()
        return bool(snap.get('running') and snap.get('playback_authoritative') and not snap.get('last_error'))

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            snap = PlayoutSnapshot(**self._snapshot.__dict__)
            last_child_contact_monotonic = float(self._last_child_contact_monotonic or 0.0)
            last_snapshot_monotonic = float(self._last_snapshot_monotonic or 0.0)
        now = time.monotonic()
        child_alive = bool(self._process is not None and self._process.is_alive())
        child_contact_age_s = max(0.0, now - last_child_contact_monotonic) if last_child_contact_monotonic > 0.0 else None
        snapshot_age_s = max(0.0, now - last_snapshot_monotonic) if last_snapshot_monotonic > 0.0 else None
        stale_reasons: list[str] = []
        snapshot_fresh = bool(snapshot_age_s is not None and snapshot_age_s <= self._stale_timeout_s)
        if not child_alive and (last_child_contact_monotonic > 0.0 or last_snapshot_monotonic > 0.0 or snap.running or snap.current_track_id):
            stale_reasons.append('child_not_alive')
        if snapshot_age_s is not None and snapshot_age_s > self._stale_timeout_s:
            stale_reasons.append('snapshot_stale')
        if child_contact_age_s is not None and child_contact_age_s > self._stale_timeout_s:
            stale_reasons.append('child_silent')
        authoritative = bool(child_alive and snap.running and not snap.last_error and not stale_reasons and snapshot_fresh)
        data = dict(snap.__dict__)
        data['child_alive'] = child_alive
        data['snapshot_fresh'] = snapshot_fresh
        data['snapshot_age_s'] = round(snapshot_age_s, 3) if snapshot_age_s is not None else None
        data['child_contact_age_s'] = round(child_contact_age_s, 3) if child_contact_age_s is not None else None
        data['stale'] = bool(stale_reasons)
        data['stale_reason'] = ','.join(stale_reasons)
        data['playback_authoritative'] = authoritative
        data['running'] = bool(authoritative)
        return data

    def _set_snapshot(self, **updates: Any) -> None:
        with self._snapshot_lock:
            data = dict(self._snapshot.__dict__)
            data.update(updates)
            self._snapshot = PlayoutSnapshot(**data)

    def _mark_child_contact(self, *, snapshot: bool = False) -> None:
        now = time.monotonic()
        with self._snapshot_lock:
            self._last_child_contact_monotonic = now
            if snapshot:
                self._last_snapshot_monotonic = now

    def _watchdog_tick(self) -> None:
        snap = self.snapshot()
        if snap.get('stale') or snap.get('last_error'):
            self._set_snapshot(running=False)
            self._started_event.set()

    def _send_command(self, payload: dict[str, Any]) -> None:
        if self._stop.is_set() or self._command_q is None:
            return
        try:
            self._command_q.put(payload, timeout=0.2)
        except Exception:
            pass

    def set_current_track(self, track: Optional[dict[str, Any]]) -> None:
        self._send_command({'type': 'set_current_track', 'track': dict(track) if isinstance(track, dict) else None})

    def set_next_track(self, track: Optional[dict[str, Any]]) -> None:
        self._send_command({'type': 'set_next_track', 'track': dict(track) if isinstance(track, dict) else None})

    def update_tracks(self, current_track: Optional[dict[str, Any]], next_track: Optional[dict[str, Any]]) -> None:
        self.set_current_track(current_track)
        self.set_next_track(next_track)

    def set_snapshot_emission(self, enabled: bool) -> None:
        self._send_command({'type': 'set_snapshot_emission', 'enabled': bool(enabled)})

    def play_jingle(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        self._send_command({'type': 'play_jingle', 'event': dict(event)})

    def _event_loop(self) -> None:
        startup_failure_reported = False
        while not self._stop.is_set() and self._event_q is not None:
            try:
                event = self._event_q.get(timeout=0.2)
            except queue.Empty:
                if (not startup_failure_reported
                        and self._process is not None
                        and not self._process.is_alive()
                        and self._last_child_contact_monotonic <= 0.0
                        and not str(getattr(self._snapshot, 'last_error', '') or '').strip()):
                    startup_failure_reported = True
                    message = 'playout process failed to start'
                    self._set_snapshot(last_error=message, running=False)
                    self._started_event.set()
                    if self._on_error is not None:
                        with contextlib.suppress(Exception):
                            self._on_error(message)
                self._watchdog_tick()
                continue
            except Exception:
                break
            self._mark_child_contact(snapshot=False)
            if not isinstance(event, dict):
                self._watchdog_tick()
                continue
            event_type = str(event.get('type', '') or '')
            if event_type == 'snapshot':
                snapshot = event.get('snapshot') or {}
                if isinstance(snapshot, dict):
                    self._mark_child_contact(snapshot=True)
                    self._set_snapshot(**snapshot)
                    if snapshot.get('running'):
                        self._started_event.set()
                self._watchdog_tick()
                continue
            if event_type == 'stream_started':
                self._mark_child_contact(snapshot=True)
                self._set_snapshot(running=True, stream_started_monotonic=time.monotonic())
                self._started_event.set()
                if self._on_stream_started is not None:
                    with contextlib.suppress(Exception):
                        self._on_stream_started(dict(event))
                self._watchdog_tick()
                continue
            if event_type == 'stream_stopped':
                self._set_snapshot(running=False)
                self._started_event.set()
                if self._on_stream_stopped is not None:
                    with contextlib.suppress(Exception):
                        self._on_stream_stopped(dict(event))
                self._watchdog_tick()
                continue
            if event_type == 'track_started':
                if self._on_track_started is not None:
                    with contextlib.suppress(Exception):
                        self._on_track_started(str(event.get('track_id', '') or ''))
                self._watchdog_tick()
                continue
            if event_type == 'track_ended':
                if self._on_track_end is not None:
                    with contextlib.suppress(Exception):
                        self._on_track_end(str(event.get('track_id', '') or ''))
                self._watchdog_tick()
                continue
            if event_type == 'jingle_started':
                if self._on_jingle_started is not None:
                    with contextlib.suppress(Exception):
                        self._on_jingle_started(dict(event))
                self._watchdog_tick()
                continue
            if event_type == 'jingle_ended':
                if self._on_jingle_ended is not None:
                    with contextlib.suppress(Exception):
                        self._on_jingle_ended(dict(event))
                self._watchdog_tick()
                continue
            if event_type == 'error':
                message = str(event.get('message', '') or '')
                self._set_snapshot(last_error=message, running=False)
                self._started_event.set()
                if self._on_error is not None:
                    with contextlib.suppress(Exception):
                        self._on_error(message)
                self._watchdog_tick()
