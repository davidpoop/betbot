"""Comprobaciones de primer arranque (usadas por el asistente de la interfaz).

Todo es consultable sin terminal: la pantalla inicial de la UI muestra estos
checks y ofrece botones para resolver lo que falte. El test sellado de 2025
NUNCA se ejecuta desde aquí.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from betbot.config import resolve_path

MIN_PYTHON = (3, 11)


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, delete=True):
            pass
        return True
    except OSError:
        return False


def check_environment(cfg: dict) -> dict:
    """Estado completo del entorno para la pantalla de primer arranque."""
    from betbot.launcher import missing_dependencies
    canon = resolve_path(cfg, "canonical_dir")
    art = resolve_path(cfg, "artifacts_dir")
    raw = resolve_path(cfg, "raw_dir")
    checks = {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": sys.version_info >= MIN_PYTHON,
        "venv_active": (hasattr(sys, "real_prefix") or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
                        or bool(os.environ.get("VIRTUAL_ENV"))),
        "missing_deps": missing_dependencies(),
        "raw_data_present": any(raw.rglob("*.csv")) or any(raw.rglob("*.xlsx")),
        "canonical_present": (canon / "matches.parquet").exists(),
        "models_present": (art / "model_bundle.joblib").exists(),
        "features_present": (art / "features_all.parquet").exists(),
        "data_writable": _writable(canon.parent),
        "artifacts_writable": _writable(art),
    }
    checks["deps_ok"] = not checks["missing_deps"]
    checks["ready"] = bool(checks["python_ok"] and checks["deps_ok"]
                           and checks["canonical_present"] and checks["models_present"]
                           and checks["data_writable"] and checks["artifacts_writable"])
    return checks


def needs_setup(checks: dict) -> bool:
    return not checks["ready"]


def setup_actions(checks: dict) -> list[str]:
    """Acciones pendientes, en orden, para la pantalla inicial."""
    actions: list[str] = []
    if not checks["python_ok"]:
        actions.append("python")
    if checks["missing_deps"]:
        actions.append("deps")
    if not (checks["data_writable"] and checks["artifacts_writable"]):
        actions.append("permissions")
    if not checks["raw_data_present"]:
        actions.append("download")
    if not checks["canonical_present"]:
        actions.append("prepare")
    if not checks["models_present"] or not checks["features_present"]:
        actions.append("train")
    return actions
