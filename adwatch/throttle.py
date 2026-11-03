"""One polite request at a time per website.

Each site gets at least its minimum gap (+ jitter) between requests. If a site says to slow
down (HTTP 429/403 or a security page), it's left alone for MIN_BACKOFF_SECONDS (5 min),
doubling up to an hour.
"""
import os
import random
import threading
import time
from datetime import datetime, timezone

MIN_BACKOFF = float(os.environ.get("MIN_BACKOFF_SECONDS", "300"))

_lock = threading.Lock()
_last = {}       # site -> time of last request
_backoff = {}    # site -> current backoff seconds
_blocked = {}    # site -> unix time until which we leave it alone


class RateLimited(Exception):
    """The site asked us to slow down."""


def blocked_until(key):
    t = _blocked.get(key, 0)
    if t <= time.time():
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_turn(key, gap, jitter=5.0):
    """Sleep until it's polite to ask `key` again, then reserve the slot."""
    while True:
        with _lock:
            now = time.time()
            until = max(_last.get(key, 0) + gap + random.uniform(0, jitter), _blocked.get(key, 0))
            if until <= now:
                _last[key] = now
                return
        time.sleep(min(until - now, 30))


def ok(key):
    with _lock:
        _last[key] = time.time()
        _backoff[key] = 0
        _blocked.pop(key, None)


def done(key):
    """A request finished (successfully or not, but not rate-limited)."""
    with _lock:
        _last[key] = time.time()


def slow_down(key):
    with _lock:
        b = min(3600, max(MIN_BACKOFF, _backoff.get(key, 0) * 2))
        _backoff[key] = b
        _last[key] = time.time()
        _blocked[key] = time.time() + b
        return b
