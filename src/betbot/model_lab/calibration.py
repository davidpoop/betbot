"""Calibración del laboratorio: candidatos y disciplina de ajuste.

Candidatos: sin calibrar (identidad), Platt, beta (los tres del champion,
reutilizados) e ISOTÓNICA (nueva, solo laboratorio). La regla inquebrantable:
un calibrador se ajusta EXCLUSIVAMENTE con datos anteriores al conjunto donde
se evalúa — aquí eso se materializa en `fit_in_fold`, que recibe SOLO
predicciones del train/OOF del fold, y en que la selección del calibrador se
hace por log loss en OOF, nunca mirando el test final.

Nota de auditoría (documentada en docs/MODEL_LAB_FASE1.md): el champion
selecciona y ajusta su calibrador sobre el POOL OOF completo y reporta las
métricas OOF con ese mismo calibrador (optimismo leve in-sample del calibrador
en la MÉTRICA OOF; la evaluación en validación/test no lo hereda porque esos
años nunca entran en el ajuste). El laboratorio reproduce ese flujo en modo
paridad y ofrece además el modo honesto fold-interno.
"""
from __future__ import annotations

import numpy as np

from betbot.models.calibrate import (BetaCalibrator, IdentityCalibrator,
                                     PlattCalibrator, log_loss_safe)


class IsotonicCalibrator:
    """Regresión isotónica (solo laboratorio; el champion no la usa)."""
    name = "isotonic"

    def __init__(self) -> None:
        from sklearn.isotonic import IsotonicRegression
        self.iso = IsotonicRegression(y_min=1e-6, y_max=1 - 1e-6, out_of_bounds="clip")

    def fit(self, p: np.ndarray, y: np.ndarray) -> "IsotonicCalibrator":
        self.iso.fit(np.asarray(p, dtype=float), np.asarray(y, dtype=float))
        return self

    def transform(self, p: np.ndarray) -> np.ndarray:
        return np.clip(self.iso.predict(np.asarray(p, dtype=float)), 1e-6, 1 - 1e-6)


CANDIDATES = ("identity", "platt", "beta", "isotonic")


def make_calibrator(name: str):
    return {"identity": IdentityCalibrator, "platt": PlattCalibrator,
            "beta": BetaCalibrator, "isotonic": IsotonicCalibrator}[name]()


def fit_in_fold(p_train: np.ndarray, y_train: np.ndarray,
                candidates: tuple[str, ...] = CANDIDATES) -> tuple[object, dict[str, float]]:
    """Ajusta los candidatos SOLO con (p_train, y_train) del fold y elige por
    log loss sobre esos mismos datos de ajuste. El llamante es responsable de
    que (p_train, y_train) sean anteriores al conjunto de evaluación; la
    integridad temporal la fuerza validation.run_fold."""
    fitted: dict[str, object] = {}
    lls: dict[str, float] = {}
    for name in candidates:
        try:
            cal = make_calibrator(name).fit(p_train, y_train)
            fitted[name] = cal
            lls[name] = log_loss_safe(y_train, cal.transform(p_train))
        except Exception:  # noqa: BLE001 — un candidato que no converge no rompe el fold
            continue
    best = min(lls, key=lls.get)
    return fitted[best], {k: round(v, 5) for k, v in lls.items()}
