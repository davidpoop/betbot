"""Carga de configuración YAML con defaults del repo + .env local opcional."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


def load_dotenv(path: Path | None = None) -> list[str]:
    """Carga variables desde `.env` local (ignorado por git) SIN pisar el
    entorno del sistema: `export VAR=x` en la shell siempre tiene precedencia.

    Formato mínimo y explícito: líneas KEY=VALUE, comillas opcionales,
    `#` comenta. Nunca se imprime ningún valor — solo se devuelven los NOMBRES
    de las variables cargadas, para poder decir "configured/not configured".
    """
    path = Path(path) if path else REPO_ROOT / ".env"
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:      # el sistema tiene precedencia
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _deep_update(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_update(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv()          # credenciales locales opcionales; el sistema manda
    with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if path is not None:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = _deep_update(cfg, yaml.safe_load(fh) or {})
    return cfg


def resolve_path(cfg: dict, key: str) -> Path:
    p = Path(cfg["paths"][key])
    if not p.is_absolute():
        p = REPO_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p
