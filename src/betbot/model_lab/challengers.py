"""Challengers del laboratorio: C0 (champion reproducido) + registro C1–C4.

FASE 1: SOLO C0 es ejecutable — la reproducción fiel del walk-forward del
champion con las clases de producción (SymmetricModel + calibradores del
champion), para demostrar que el laboratorio mide exactamente lo que produce
producción antes de comparar nada. C1–C4 quedan REGISTRADOS con su
especificación exacta pero sin entrenamiento: entrenarlos es la fase
siguiente, tras revisar la auditoría.

Ningún challenger se promociona automáticamente: el criterio pre-registrado
vive en docs/MODEL_LAB_FASE1.md §11 y la decisión final es del usuario.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from betbot.features.builder import FEATURES, MARKET_FEATURE
from betbot.models.calibrate import (calibration_slope_intercept,
                                     expected_calibration_error, log_loss_safe,
                                     select_calibrator)
from betbot.models.predictor import SymmetricModel
from betbot.model_lab.dataset import LabDataset
from betbot.model_lab.metrics import accuracy, brier
from betbot.model_lab.validation import walk_forward_folds

MATCH_MODELS: dict[str, list[str]] = {          # espejo literal de models/train.py
    "M1_rank": ["log_rank_ratio"],
    "M2_elo": ["elo_diff"],
    "M3_elo_surf": ["elo_diff", "elo_surf_diff"],
    "M4_full": FEATURES,
    "M5_market": FEATURES + [MARKET_FEATURE],
}


@dataclass(frozen=True)
class ChallengerSpec:
    cid: str
    nombre: str
    algoritmo: str
    features: str
    estado: str                 # "baseline" | "propuesto"
    dependencias: str = "instaladas"
    notas: str = ""


CHALLENGER_SPECS: list[ChallengerSpec] = [
    ChallengerSpec("C0", "champion actual (M4/M5 + beta/platt + shrink)",
                   "LogisticRegression simétrica C=1.0 (clases de producción)",
                   "18 features v1 (+market_logit en M5)", "baseline",
                   notas="reproducido en este módulo; es la vara de medir"),
    ChallengerSpec("C1", "logística regularizada bien especificada",
                   "LogisticRegression con C elegido por fold interno (grid pequeño "
                   "pre-registrado), misma simetría A/B que producción",
                   "features v2 factibles (Elo decay, forma EWMA, fatiga, H2H shrunk)",
                   "propuesto",
                   notas="baseline interpretable del set v2; aísla el valor de las "
                         "features nuevas del valor del algoritmo"),
    ChallengerSpec("C2", "gradient boosted trees",
                   "sklearn HistGradientBoostingClassifier (sin dependencias nuevas; "
                   "lightgbm/xgboost/catboost NO están instalados). Simetría por "
                   "augmentación swap como producción; early stopping con cola "
                   "temporal del train del fold, jamás con el valid",
                   "features v2 completas", "propuesto",
                   dependencias="sklearn ya instalado",
                   notas="profundidad/hojas conservadoras pre-registradas; la no "
                         "linealidad debe justificarse contra C1, no asumirse"),
    ChallengerSpec("C3", "rating + residual ML",
                   "prior = p_elo calibrada (Elo con decay/superficie); un modelo "
                   "pequeño aprende SOLO el residuo contextual sobre el logit del "
                   "prior (offset)", "prior Elo + contexto v2", "propuesto",
                   notas="estructura favorita a priori: preserva la señal estable "
                         "del rating y limita el sobreajuste del ML"),
    ChallengerSpec("C4", "ensemble/stacking",
                   "combinación convexa o stacking logístico de los mejores C1–C3, "
                   "pesos ajustados por fold interno (nunca en test)",
                   "salidas de C1–C3", "propuesto",
                   notas="solo si ≥2 challengers aportan señal no redundante"),
    ChallengerSpec("C5", "bayesiano jerárquico (OPCIONAL)",
                   "solo si la fase 2 muestra evidencia empírica de que la "
                   "incertidumbre por jugador/torneo aporta (p.ej. C3 mejora en "
                   "jugadores con pocas observaciones)", "ratings jerárquicos",
                   "propuesto", notas="no se implementa por moda; requiere justificación"),
]


# ------------------------------------------------------------------ C0

def reproduce_champion_oof(cfg: dict, ds: LabDataset) -> dict:
    """Reproduce el walk-forward OOF del champion (models/train.py) con las
    clases de PRODUCCIÓN a través del dataset/folds del laboratorio, y calcula
    las mismas métricas con el mismo redondeo. La comparación contra
    artifacts/reports/train_report.json debe dar delta 0.0 — esa igualdad es la
    acreditación del laboratorio."""
    labelled = ds.labelled().copy()
    folds = walk_forward_folds(cfg)
    out: dict = {"tours": {}}
    for tour in ("ATP", "WTA"):
        tdf = labelled[labelled["tour"] == tour]
        oof_parts: list[pd.DataFrame] = []
        for fold in folds:
            tr = tdf[(tdf["year"] >= fold.train_years[0]) & (tdf["year"] <= fold.train_years[1])]
            va = tdf[tdf["year"] == fold.valid_year]
            if len(tr) < 2000 or len(va) == 0:
                continue
            part = va[["match_id", "year", "date", "has_market",
                       "label_a_wins", "p_novig_a_prop"]].copy()
            for name, feats in MATCH_MODELS.items():
                sub = tr[tr["has_market"]] if MARKET_FEATURE in feats else tr
                model = SymmetricModel(feats).fit(sub, sub["label_a_wins"].to_numpy(dtype=float))
                part[f"oof_{name}"] = model.predict(va)
            oof_parts.append(part)
        oof = pd.concat(oof_parts, ignore_index=True)
        y = oof["label_a_wins"].to_numpy(dtype=float)

        metrics: dict = {}
        calibrators: dict = {}
        for name in MATCH_MODELS:
            mask = oof["has_market"].to_numpy() if name == "M5_market" else np.ones(len(oof), bool)
            p_raw = oof.loc[mask, f"oof_{name}"].to_numpy(dtype=float)
            y_m = y[mask]
            cal, _table = select_calibrator(p_raw, y_m)     # paridad champion (id/platt/beta)
            calibrators[name] = cal
            p_cal = cal.transform(p_raw)
            metrics[name] = {"n": int(mask.sum()),
                             "log_loss": round(log_loss_safe(y_m, p_cal), 5),
                             "brier": round(brier(y_m, p_cal), 5),
                             "acc": round(accuracy(y_m, p_cal), 5),
                             "calibrator": cal.name}
        mkt = oof["has_market"].to_numpy()
        p_mkt = oof.loc[mkt, "p_novig_a_prop"].to_numpy(dtype=float)
        metrics["M0_market_novig"] = {"n": int(mkt.sum()),
                                      "log_loss": round(log_loss_safe(y[mkt], p_mkt), 5),
                                      "brier": round(brier(y[mkt], p_mkt), 5),
                                      "acc": round(accuracy(y[mkt], p_mkt), 5),
                                      "calibrator": "-"}
        p_m4 = calibrators["M4_full"].transform(oof["oof_M4_full"].to_numpy(dtype=float))
        p_m5 = np.where(mkt, calibrators["M5_market"].transform(
            oof["oof_M5_market"].to_numpy(dtype=float)), p_m4)
        slope, intercept = calibration_slope_intercept(y, p_m5)
        out["tours"][tour] = {
            "n_oof": int(len(oof)),
            "match_models_oof": metrics,
            "p_match_operativa_oof": {
                "log_loss": round(log_loss_safe(y, p_m5), 5),
                "slope": round(slope, 3), "intercept": round(intercept, 3),
                "ece": round(expected_calibration_error(y, p_m5), 4)},
        }
    return out


def compare_with_train_report(repro: dict, train_report: dict) -> dict:
    """Deltas laboratorio − train_report.json por tour/modelo/métrica."""
    out: dict = {"max_abs_delta": 0.0, "detalle": {}}
    for tour, rt in repro["tours"].items():
        base = train_report["tours"][tour]
        det: dict = {}
        for m, met in rt["match_models_oof"].items():
            ref = base["match_models_oof"][m]
            det[m] = {k: round(met[k] - ref[k], 6) for k in ("log_loss", "brier", "acc")
                      if isinstance(met.get(k), float)}
            det[m]["n_delta"] = met["n"] - ref["n"]
            det[m]["mismo_calibrador"] = met["calibrator"] == ref["calibrator"]
            for v in det[m].values():
                if isinstance(v, float):
                    out["max_abs_delta"] = max(out["max_abs_delta"], abs(v))
        po, pr = rt["p_match_operativa_oof"], base["p_match_operativa_oof"]
        det["p_match_operativa"] = {k: round(po[k] - pr[k], 6)
                                    for k in ("log_loss", "slope", "intercept", "ece")}
        for v in det["p_match_operativa"].values():
            out["max_abs_delta"] = max(out["max_abs_delta"], abs(v))
        out["detalle"][tour] = det
    return out
