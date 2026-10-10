"""Debounced local registry watcher; malformed files never replace live state."""

import json
import threading
import time
from pathlib import Path

from .registry import validate_registry


class RegistryWatcher:
    def __init__(self, path, apply, *, clock=time.monotonic, debounce=0.5):
        self.path, self.apply = Path(path), apply
        self.clock, self.debounce = clock, debounce
        self._candidate = None
        self._seen_at = 0
        self._applied = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._status = dict(enabled=True, state='watching', reloads=0, last_error=None)

    def snapshot(self):
        with self._lock:
            return dict(self._status)

    def check_once(self):
        try:
            with self.path.open('rb') as handle:
                content = handle.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('registry exceeds byte limit')
            if content != self._candidate:
                self._candidate, self._seen_at = content, self.clock()
                return
            if content == self._applied:
                with self._lock:
                    self._status.update(state="watching", last_error=None)
                return
            if self.clock() - self._seen_at < self.debounce:
                return
            nodes = validate_registry(json.loads(content.decode('utf-8')))
            self.apply(nodes)
            self._applied = content
            with self._lock:
                self._status.update(state='watching', last_error=None, reloads=self._status['reloads']+1)
        except (OSError, ValueError, UnicodeError) as exc:
            with self._lock:
                self._status.update(state='error', last_error=type(exc).__name__)

    def _run(self):
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(0.25)

    def start(self):
        self._thread = threading.Thread(target=self._run, name='hub-registry', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
