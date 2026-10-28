"""One shared pace for every Yahoo Finance request made from this computer.

Yahoo limits how many requests one IP address may make. All fetching (prices and
fundamentals, every thread) goes through GATE, which
  * spaces requests out (YAHOO_RPS, default 1 per second), and
  * when Yahoo answers "Too Many Requests", pauses everything: 1 minute, then
    2, 4, 8 and at most 15 minutes if it keeps happening; successful requests
    shrink the pause again.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

RPS = float(os.environ.get("YAHOO_RPS", "1"))
MAX_PAUSE = 15 * 60

# yfinance logs every failed ticker; the gate prints one line per pause instead.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def is_rate_limit(err: object) -> bool:
    text = f"{type(err).__name__} {err}".lower()
    return "ratelimit" in text or "rate limit" in text or "too many requests" in text or "429" in text


class Gate:
    def __init__(self, rps: float = RPS) -> None:
        self.interval = 1.0 / max(rps, 0.01)
        self.lock = threading.Lock()
        self.next = 0.0      # earliest time for the next request
        self.until = 0.0     # paused until this time
        self.pause = 0       # length of the current/last pause, seconds

    def wait(self) -> None:
        """Block until this thread may make one request."""
        while True:
            with self.lock:
                now = time.monotonic()
                if now < self.until:
                    delay = self.until - now
                elif now < self.next:
                    delay = self.next - now
                else:
                    self.next = now + self.interval
                    return
            time.sleep(min(delay, 5))

    def limited(self) -> None:
        """Yahoo said "Too Many Requests": pause every request."""
        with self.lock:
            now = time.monotonic()
            if now < self.until:
                return   # another thread already started this pause
            self.pause = min(max(self.pause * 2, 60), MAX_PAUSE)
            self.until = now + self.pause
            pause = self.pause
        print(f"Yahoo rate limit reached: pausing all requests for {pause // 60} min.", file=sys.stderr, flush=True)

    def ok(self) -> None:
        with self.lock:
            if self.pause and time.monotonic() >= self.until:
                self.pause = self.pause // 2 if self.pause > 60 else 0

    def paused_for(self) -> float:
        with self.lock:
            return max(0.0, self.until - time.monotonic())


GATE = Gate()
