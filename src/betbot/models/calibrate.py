"""Calibradores: identidad, Platt (logístico sobre el logit) y beta calibration.

Se ajustan EXCLUSIVAMENTE sobre predicciones out-of-fold. La selección se hace
por log loss OOF. La simetría A/B la garantiza el wrapper del predictor
(promedia f(x) y 1-f(swap)), por lo que aquí se admiten interceptos.
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression

EPS = 1e-6


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def log_loss_safe(y: np.ndarray, p: np.ndarray) -> float:
    p = _clip(p)
    y = np.asarray(y, dtype=float)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


class IdentityCalibrator:
    name = "identity"

    def fit(self, p: np.ndarray, y: np.ndarray) -> "IdentityCalibrator":
        return self

    def transform(self, p: np.ndarray) -> np.ndarray:
        return _clip(p)


class PlattCalibrator:
    """sigma(a*logit(p) + b)."""
    name = "platt"

    def __init__(self) -> None:
        self.lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)

    def fit(self, p: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        z = np.log(_clip(p) / (1 - _clip(p))).reshape(-1, 1)
        self.lr.fit(z, np.asarray(y, dtype=int))
        return self

    def transform(self, p: np.ndarray) -> np.ndarray:
        z = np.log(_clip(p) / (1 - _clip(p))).reshape(-1, 1)
        return _clip(self.lr.predict_proba(z)[:, 1])


class BetaCalibrator:
    """Kull et al. (2017): logístico sobre [ln p, -ln(1-p)]."""
    name = "beta"

    def __init__(self) -> None:
        self.lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)

    def fit(self, p: np.ndarray, y: np.ndarray) -> "BetaCalibrator":
        p = _clip(p)
        X = np.column_stack([np.log(p), -np.log(1 - p)])
        self.lr.fit(X, np.asarray(y, dtype=int))
        return self

    def transform(self, p: np.ndarray) -> np.ndarray:
        p = _clip(p)
        X = np.column_stack([np.log(p), -np.log(1 - p)])
        return _clip(self.lr.predict_proba(X)[:, 1])


def select_calibrator(p_oof: np.ndarray, y_oof: np.ndarray) -> tuple[object, dict[str, float]]:
    """Ajusta los tres candidatos sobre OOF y devuelve (mejor, tabla de LL)."""
    results: dict[str, float] = {}
    fitted: dict[str, object] = {}
    for cal in (IdentityCalibrator(), PlattCalibrator(), BetaCalibrator()):
        try:
            cal.fit(p_oof, y_oof)
            results[cal.name] = log_loss_safe(y_oof, cal.transform(p_oof))
            fitted[cal.name] = cal
        except Exception:  # noqa: BLE001 - un calibrador que no converge no rompe el pipeline
            continue
    best = min(results, key=results.get)
    return fitted[best], results


def calibration_slope_intercept(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """Pendiente e intercepto de recalibración logística (diagnóstico)."""
    z = np.log(_clip(p) / (1 - _clip(p))).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
    lr.fit(z, np.asarray(y, dtype=int))
    return float(lr.coef_[0][0]), float(lr.intercept_[0])


def expected_calibration_error(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    p = _clip(p)
    y = np.asarray(y, dtype=float)
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.digitize(p, bins[1:-1])
    ece = 0.0
    for b in range(n_bins):
        mask = idx == b
        if mask.sum() == 0:
            continue
        ece += mask.mean() * abs(y[mask].mean() - p[mask].mean())
    return float(ece)
