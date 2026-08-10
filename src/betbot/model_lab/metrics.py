"""Métricas predictivas del laboratorio.

Primarias: log loss y Brier. Secundarias: AUC y accuracy. Calibración:
pendiente/intercepto, ECE y tabla de fiabilidad. Nitidez: distribución de p.
Las diferencias champion vs challenger llevan IC bootstrap POR BLOQUES
SEMANALES (los partidos de una misma semana comparten torneo/condiciones: el
bootstrap i.i.d. por fila subestimaría la varianza).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from betbot.models.calibrate import (calibration_slope_intercept,
                                     expected_calibration_error, log_loss_safe)

EPS = 1e-6


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((_clip(p) - np.asarray(y, dtype=float)) ** 2))


def roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score
    y = np.asarray(y, dtype=float)
    if len(np.unique(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, p))


def accuracy(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) > 0.5) == (np.asarray(y, dtype=float) == 1)))


def reliability_table(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> list[dict]:
    """Tabla del diagrama de fiabilidad (para render/inspección, no gráfico)."""
    p = _clip(p)
    y = np.asarray(y, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.digitize(p, edges[1:-1])
    rows = []
    for b in range(n_bins):
        m = idx == b
        rows.append({"bin": f"[{edges[b]:.1f},{edges[b + 1]:.1f})", "n": int(m.sum()),
                     "p_media": round(float(p[m].mean()), 4) if m.any() else None,
                     "y_media": round(float(y[m].mean()), 4) if m.any() else None})
    return rows


def sharpness(p: np.ndarray) -> dict:
    p = _clip(p)
    return {"media_abs_desviacion_de_0.5": round(float(np.mean(np.abs(p - 0.5))), 4),
            "std": round(float(np.std(p)), 4),
            "deciles": [round(float(x), 4) for x in np.percentile(p, range(0, 101, 10))]}


def full_report(y: np.ndarray, p: np.ndarray) -> dict:
    slope, intercept = calibration_slope_intercept(y, p)
    return {"n": int(len(y)),
            "log_loss": round(log_loss_safe(y, p), 5),
            "brier": round(brier(y, p), 5),
            "auc": round(roc_auc(y, p), 5),
            "acc": round(accuracy(y, p), 5),
            "cal_slope": round(slope, 3), "cal_intercept": round(intercept, 3),
            "ece": round(expected_calibration_error(y, p), 4),
            "sharpness": sharpness(p),
            "reliability": reliability_table(y, p)}


# ---------------------------------------------------------------- subgrupos

def _fav_dog(p: np.ndarray) -> pd.Series:
    return pd.Series(np.where(np.asarray(p) >= 0.5, "favorito", "underdog"))


def by_subgroups(df: pd.DataFrame, y_col: str, p_col: str,
                 rank_cols: tuple[str, str] = ("", "")) -> dict:
    """Métricas por: tour, superficie (si está), favorito/underdog, tramo de
    probabilidad. Devuelve solo n/logloss/brier/acc por celda (lo demás es
    ruido con muestras pequeñas)."""
    out: dict = {}

    def cell(sub: pd.DataFrame) -> dict:
        y, p = sub[y_col].to_numpy(dtype=float), sub[p_col].to_numpy(dtype=float)
        return {"n": int(len(sub)), "log_loss": round(log_loss_safe(y, p), 5),
                "brier": round(brier(y, p), 5), "acc": round(accuracy(y, p), 5)}

    for key in ("tour", "surface"):
        if key in df.columns:
            out[key] = {str(k): cell(g) for k, g in df.groupby(key) if len(g) >= 50}
    lab = _fav_dog(df[p_col].to_numpy())
    out["favorito_underdog"] = {k: cell(df[lab.values == k]) for k in ("favorito", "underdog")
                                if (lab.values == k).sum() >= 50}
    bins = pd.cut(df[p_col], [0, 0.35, 0.5, 0.65, 0.8, 1.0])
    out["tramo_probabilidad"] = {str(k): cell(g) for k, g in df.groupby(bins, observed=True)
                                 if len(g) >= 50}
    return out


# ---------------------------------------------------------------- bootstrap

def block_bootstrap_diff(dates: pd.Series, y: np.ndarray, p_a: np.ndarray,
                         p_b: np.ndarray, metric=log_loss_safe,
                         n_boot: int = 2000, seed: int = 20260803) -> dict:
    """IC bootstrap de metric(y,p_a) - metric(y,p_b) remuestreando SEMANAS
    completas. Negativo = A mejor (para log loss/brier)."""
    rng = np.random.default_rng(seed)
    weeks = pd.to_datetime(pd.Series(dates).reset_index(drop=True)).dt.strftime("%G-%V").to_numpy()
    uniq = np.unique(weeks)
    idx_by_week = {w: np.flatnonzero(weeks == w) for w in uniq}
    y = np.asarray(y, dtype=float)
    p_a, p_b = np.asarray(p_a, dtype=float), np.asarray(p_b, dtype=float)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        take = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idx_by_week[w] for w in take])
        diffs[i] = metric(y[idx], p_a[idx]) - metric(y[idx], p_b[idx])
    point = metric(y, p_a) - metric(y, p_b)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": round(float(point), 5), "ci95": [round(float(lo), 5), round(float(hi), 5)],
            "n_blocks": int(len(uniq)), "n_boot": n_boot,
            "significativo": bool(hi < 0 or lo > 0)}
