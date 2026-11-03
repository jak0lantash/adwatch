"""A shared HTTP client that looks like Chrome (TLS fingerprint included).

Many classifieds sites reject ordinary HTTP clients; curl_cffi presents Chrome's handshake.
One session (cookies) per site.
"""
import threading

from .throttle import RateLimited

_sessions = {}
_lock = threading.Lock()


def _session(key):
    from curl_cffi import requests as creq
    with _lock:
        if key not in _sessions:
            _sessions[key] = creq.Session(impersonate="chrome")
        return _sessions[key]


def forget(key):
    with _lock:
        _sessions.pop(key, None)


def get(key, url, referer=None, timeout=40, raise_on_block=True):
    """Returns (status, final_url, text). HTTP 429/403 raise RateLimited unless told not to."""
    headers = {"Accept-Language": "en-IE,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    r = _session(key).get(url, timeout=timeout, headers=headers)
    if raise_on_block and r.status_code == 429:
        raise RateLimited(f"{key} asked us to slow down (HTTP 429)")
    if raise_on_block and r.status_code == 403:
        forget(key)  # start a fresh session next time
        raise RateLimited(f"{key} showed its security check (HTTP 403)")
    return r.status_code, str(r.url), r.text


def get_bytes(key, url, referer=None, timeout=30):
    """Returns (status, content_type, body)."""
    headers = {"Referer": referer} if referer else {}
    r = _session(key).get(url, timeout=timeout, headers=headers)
    return r.status_code, r.headers.get("content-type", ""), r.content


def next_data(html, key="the site"):
    """The JSON a Next.js page embeds in <script id="__NEXT_DATA__">."""
    import json
    import re
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        raise RateLimited("No listing data in the page (title: %s). %s may be blocking us."
                          % (title.group(1).strip()[:80] if title else "none", key))
    return json.loads(m.group(1))
