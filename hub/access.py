"""Bounded local HTTP admission; authentication belongs to the TLS proxy."""
import threading
import time


class RequestBudget:
    def __init__(self, rate=100, burst=200, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.previous = float(burst), clock()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0, now-self.previous)*self.rate)
            self.previous = now
            if self.tokens < 1: return False
            self.tokens -= 1
            return True
