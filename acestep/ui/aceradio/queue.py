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

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from loguru import logger

JobFn = Callable[[str, dict], dict]

@dataclass
class JobState:

    job_id: str
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: str = "queued"
    position: int = 0
    error: Optional[str] = None
    request: dict = field(default_factory=dict)
    result: Optional[dict] = None

class InProcessJobQueue:

    def __init__(self, worker_fn: JobFn, outputs_root: Optional[str] = None, **kwargs):
        if outputs_root is None:
            outputs_root = (
                kwargs.get("outputs_dir")
                or kwargs.get("results_root")
                or kwargs.get("results_dir")
                or kwargs.get("output_dir")
            )
        if outputs_root is None:
            outputs_root = os.path.join(os.getcwd(), "aceradio_outputs")

        self._worker_fn = worker_fn
        self._outputs_root = str(outputs_root)
        self._q: Deque[str] = deque()
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = False
        self._running_job_id: Optional[str] = None

        os.makedirs(self._outputs_root, exist_ok=True)

        self._thread = threading.Thread(target=self._loop, name="ace-step-remote-queue", daemon=True)
        self._thread.start()

    def stop(self):
        with self._cv:
            self._stop = True
            while self._q:
                job_id = self._q.popleft()
                st = self._jobs.get(job_id)
                if st and st.status == "queued":
                    st.status = "error"
                    st.error = "Queue stopped before job execution."
                    st.finished_at = time.time()
                    st.position = 0
            self._recompute_positions_locked()
            self._cv.notify_all()

    def submit(self, job_id: str, request: dict) -> JobState:
        with self._cv:
            if self._stop:
                raise RuntimeError("Queue is stopped and cannot accept new jobs.")
            state = JobState(job_id=job_id, request=request)
            self._jobs[job_id] = state
            self._q.append(job_id)
            self._recompute_positions_locked()
            self._cv.notify_all()
            return state

    def get(self, job_id: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Optional[JobState]:
        with self._cv:
            st = self._jobs.get(job_id)
            if not st:
                return None
            if st.status != "queued":
                return st
            try:
                self._q.remove(job_id)
            except ValueError:
                return st
            st.status = "cancelled"
            st.finished_at = time.time()
            st.position = 0
            st.error = None
            self._recompute_positions_locked()
            self._cv.notify_all()
            return st

    def snapshot_queue(self) -> dict:
        with self._lock:
            return {
                "running": self._running_job_id,
                "queued": list(self._q),
                "queue_length": len(self._q),
            }

    def _recompute_positions_locked(self):
        for idx, jid in enumerate(self._q, start=1):
            st = self._jobs.get(jid)
            if st:
                st.position = idx
        if self._running_job_id and self._running_job_id in self._jobs:
            self._jobs[self._running_job_id].position = 0

    def _loop(self):
        while True:
            with self._cv:
                while not self._stop and not self._q:
                    self._cv.wait(timeout=0.5)
                if self._stop:
                    return
                job_id = self._q.popleft()
                self._running_job_id = job_id
                st = self._jobs.get(job_id)
                if st:
                    st.status = "running"
                    st.started_at = time.time()
                self._recompute_positions_locked()

            try:
                result = self._worker_fn(job_id, st.request if st else {})
                with self._lock:
                    st2 = self._jobs.get(job_id)
                    if st2:
                        st2.status = "done"
                        st2.finished_at = time.time()
                        st2.result = result
            except Exception as e:
                logger.exception("AceRadio job %s failed during queue execution", job_id)
                with self._lock:
                    st2 = self._jobs.get(job_id)
                    if st2:
                        st2.status = "error"
                        st2.finished_at = time.time()
                        st2.error = str(e)
            finally:
                with self._lock:
                    self._running_job_id = None
                    self._recompute_positions_locked()
