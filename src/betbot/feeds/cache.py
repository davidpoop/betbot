"""Caché en disco con TTL y reintentos con backoff para las fuentes."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from betbot.config import REPO_ROOT

CACHE_DIR = REPO_ROOT / "artifacts" / "cache"
_BACKOFF = [1.0, 2.0, 4.0]

# Los errores citan la URL para diagnóstico, pero algunas fuentes llevan la
# credencial como query param: se REDACTA SIEMPRE antes de citar. Ningún
# carácter de una key puede acabar en logs, status ni informes.
_SECRET_Q = re.compile(r"(?i)\b(api_?key|token|secret|password)=([^&\s]+)")


def redact(text: str) -> str:
    return _SECRET_Q.sub(lambda m: f"{m.group(1)}=***", str(text))


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
    n_tries = 0
    for i, wait in enumerate([0.0] + _BACKOFF):
        if wait:
            time.sleep(wait)
        n_tries += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "betbot/0.1"} | (headers or {}))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cpath.write_text(json.dumps(data), encoding="utf-8")
            return data
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 4xx (salvo 429) es ESTRUCTURAL: reintentar con backoff es inútil
            # (caso real: ESPN 403 sistemático x4 intentos x2 ligas por scan)
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"HTTP {exc.code} (estructural, sin reintentos): {redact(url)}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_exc = exc
    raise RuntimeError(f"fuente inaccesible tras {n_tries} intentos: {redact(url)} "
                       f"({redact(str(last_exc))})")


def get_text(url: str, ttl_seconds: int = 300, timeout: int = 60,
             headers: dict | None = None) -> str:
    """GET texto plano (CSV, etc.) con la misma caché TTL y backoff que get_json."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
    if cpath.exists() and (time.time() - cpath.stat().st_mtime) < ttl_seconds:
        return cpath.read_text(encoding="utf-8")
    last_exc: Exception | None = None
    n_tries = 0
    for wait in [0.0] + _BACKOFF:
        if wait:
            time.sleep(wait)
        n_tries += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "betbot/0.1"} | (headers or {}))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            cpath.write_text(text, encoding="utf-8")
            return text
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"HTTP {exc.code} (estructural, sin reintentos): {redact(url)}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
    raise RuntimeError(f"fuente inaccesible tras {n_tries} intentos: {redact(url)} "
                       f"({redact(str(last_exc))})")
