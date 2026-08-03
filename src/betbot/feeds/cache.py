"""Caché en disco con TTL y reintentos con backoff para las fuentes."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from betbot.config import REPO_ROOT

CACHE_DIR = REPO_ROOT / "artifacts" / "cache"
_BACKOFF = [1.0, 2.0, 4.0]


def _key(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")


def get_json(url: str, ttl_seconds: int = 300, timeout: int = 30,
             headers: dict | None = None) -> dict | list:
    """GET JSON con caché TTL y reintentos (1s/2s/4s). Lanza la última excepción
    si todos los intentos fallan y no hay caché válida."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = _key(url)
    if cpath.exists() and (time.time() - cpath.stat().st_mtime) < ttl_seconds:
        return json.loads(cpath.read_text(encoding="utf-8"))
    last_exc: Exception | None = None
    for i, wait in enumerate([0.0] + _BACKOFF):
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "betbot/0.1"} | (headers or {}))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cpath.write_text(json.dumps(data), encoding="utf-8")
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as exc:
            last_exc = exc
    raise RuntimeError(f"fuente inaccesible tras {1 + len(_BACKOFF)} intentos: {url} ({last_exc})")
