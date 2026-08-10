"""Validación temporal del laboratorio: walk-forward expanding + test sellado.

Random train_test_split está PROHIBIDO como evaluación principal: aquí no
existe. El esquema reproduce y generaliza el del champion:

    fold k:  train = años < vy      valid = año vy     (vy = 2013..train_end)
    outer:   dev <= train_end_year; valid_years; test_year SELLADO

Todo ajuste (modelo, calibrador, hiperparámetros, pesos de ensemble) debe
ejecutarse DENTRO del fold vía `run_fold`, que corta, verifica la integridad
temporal y solo entonces entrena. El año de test está detrás de
`SealedTestGuard`: servirlo exige unseal explícito y deja registro.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from betbot.config import resolve_path


@dataclass(frozen=True)
class Fold:
    train_years: tuple[int, int]      # [desde, hasta] inclusive
    valid_year: int

    def label(self) -> str:
        return f"train<={self.train_years[1]}->valid {self.valid_year}"


def walk_forward_folds(cfg: dict) -> list[Fold]:
    """Folds OOF expanding del esquema del champion (config splits)."""
    first = int(cfg["splits"]["walkforward_first_valid_year"])
    end = int(cfg["splits"]["train_end_year"])
    start = min(int(cfg["data"]["atp_years"][0]), int(cfg["data"]["wta_years"][0]))
    return [Fold((start, vy - 1), vy) for vy in range(first, end + 1)]


def outer_scheme(cfg: dict) -> dict:
    v0, v1 = (int(x) for x in cfg["splits"]["valid_years"])
    return {"dev_max_year": int(cfg["splits"]["train_end_year"]),
            "valid_years": list(range(v0, v1 + 1)),
            "test_year": int(cfg["splits"]["test_year"])}


def assert_temporal_integrity(train: pd.DataFrame, valid: pd.DataFrame) -> None:
    """Toda fecha de entrenamiento debe ser ESTRICTAMENTE anterior a la mínima
    de validación. Falla ruidosamente ante cualquier solape."""
    if len(train) == 0 or len(valid) == 0:
        return
    tmax, vmin = max(train["date"]), min(valid["date"])
    if tmax >= vmin:
        raise ValueError(f"Leakage temporal: train llega a {tmax} y valid empieza {vmin}")


def run_fold(df: pd.DataFrame, fold: Fold, fit_fn, predict_fn,
             min_train_rows: int = 2000):
    """Contrato de fold: corta por años, verifica integridad, entrena SOLO con
    el train del fold y predice el valid. `fit_fn(train_df) -> model`,
    `predict_fn(model, valid_df) -> np.ndarray`. Devuelve (valid_df, p) o None
    si el fold no alcanza el mínimo de filas (misma regla que el champion)."""
    tr = df[(df["year"] >= fold.train_years[0]) & (df["year"] <= fold.train_years[1])]
    va = df[df["year"] == fold.valid_year]
    if len(tr) < min_train_rows or len(va) == 0:
        return None
    assert_temporal_integrity(tr, va)
    model = fit_fn(tr)
    return va, predict_fn(model, va)


class SealedTestGuard:
    """El año de test es de UN solo uso por decisión. El laboratorio no sirve
    sus filas salvo `unseal=True`, y cada desbloqueo queda registrado en
    artifacts/model_lab/test_access_log.json (espejo del log del champion)."""

    def __init__(self, cfg: dict) -> None:
        self.test_year = int(cfg["splits"]["test_year"])
        self.log_path = Path(resolve_path(cfg, "artifacts_dir")) / "model_lab" / "test_access_log.json"

    def filter(self, df: pd.DataFrame, unseal: bool = False, reason: str = "") -> pd.DataFrame:
        if not unseal:
            return df[df["year"] != self.test_year]
        if not reason.strip():
            raise ValueError("Desbloquear el test sellado exige un motivo explícito")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = {"accesses": []}
        if self.log_path.exists():
            log = json.loads(self.log_path.read_text())
        log["accesses"].append({"at": datetime.now(timezone.utc).isoformat(), "reason": reason})
        self.log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False))
        return df
