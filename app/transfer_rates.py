"""Per-job recent transfer rates, sampled independently of UI/Telegram polling."""
from collections import deque
import threading
import time


class TransferRates:
    def __init__(self, window=5.0, clock=None):
        self.window = window
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.samples = {}

    def start(self, job_id):
        with self.lock:
            self.samples[job_id] = (self.clock(), deque())

    def stop(self, job_id):
        with self.lock:
            self.samples.pop(job_id, None)

    def record(self, job_id, count):
        with self.lock:
            if job_id not in self.samples:
                return
            _, samples = self.samples[job_id]
            now = self.clock()
            samples.append((now, count))
            self._prune(samples, now)

    def _prune(self, samples, now):
        while samples and samples[0][0] <= now - self.window:
            samples.popleft()

    def current(self, job_id):
        with self.lock:
            if job_id not in self.samples:
                return 0.0
            started, samples = self.samples[job_id]
            now = self.clock()
            self._prune(samples, now)
            elapsed = min(now - started, self.window)
            return sum(count for _, count in samples) / elapsed if elapsed > 0 else 0.0
