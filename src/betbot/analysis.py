"""Estudios pre-registrados sobre desarrollo+validación (el test 2025 NO se toca):

1. three_sets: diagnóstico de la pendiente >1.1 y recalibración mínima
   temporalmente válida (ajustada en OOF <=2022, evaluada en validación 2023-24).
2. Sensibilidad de thresholds: parrilla predefinida evaluada SOLO en validación;
   la configuración por defecto no cambia sin revisión humana.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from betbot.config import resolve_path
from betbot.models import derived as dv
from betbot.models.calibrate import calibration_slope_intercept, log_loss_safe
from betbot.value.engine import evaluate_selection

EPS = 1e-6


class ConditionalPlatt:
    """Recalibración mínima condicional: sigma(a·z + c·|z_m| + d·z·|z_m| + b),
    donde z = logit(p_three) y z_m = logit(p_match). Captura el defecto del
    puente iid: la dispersión de three_sets depende de lo desequilibrado del
    partido. Temporalmente válida: se ajusta SOLO con años <= train_end."""

    def __init__(self) -> None:
        self.lr = LogisticRegression(C=1e6, max_iter=2000)

    @staticmethod
    def _feats(p: np.ndarray, m: np.ndarray) -> np.ndarray:
        z = np.log(np.clip(p, EPS, 1 - EPS) / np.clip(1 - p, EPS, 1 - EPS))
        zm = np.abs(np.log(np.clip(m, EPS, 1 - EPS) / np.clip(1 - m, EPS, 1 - EPS)))
        return np.column_stack([z, zm, z * zm])

    def fit(self, p: np.ndarray, m: np.ndarray, y: np.ndarray) -> "ConditionalPlatt":
        self.lr.fit(self._feats(p, m), y.astype(int))
        return self

    def transform(self, p: np.ndarray, m: np.ndarray) -> np.ndarray:
        return np.clip(self.lr.predict_proba(self._feats(p, m))[:, 1], EPS, 1 - EPS)


def _match_probs(tb: dict, df: pd.DataFrame) -> np.ndarray:
    p4 = tb["match_calibrators"]["M4_full"].transform(tb["match_models"]["M4_full"].predict(df))
    has = df["has_market"].to_numpy(bool)
    p = p4.copy()
    if has.any():
        p[has] = tb["match_calibrators"]["M5_market"].transform(
            tb["match_models"]["M5_market"].predict(df[has]))
    return p


def three_sets_study(cfg: dict) -> dict:
    art = resolve_path(cfg, "artifacts_dir")
    reports = resolve_path(cfg, "reports_dir")
    bundle = joblib.load(art / "model_bundle.joblib")
    feats = pd.read_parquet(art / "features_all.parquet")
    feats["year"] = pd.to_datetime(feats["date"]).dt.year
    train_end = int(cfg["splits"]["train_end_year"])
    v0, v1 = cfg["splits"]["valid_years"]
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "protocol": f"fit<= {train_end}, eval {v0}-{v1}; test 2025 NO usado",
                 "tours": {}}

    for tour in ("ATP", "WTA"):
        tb = bundle["tours"][tour]
        base = feats[(feats["tour"] == tour) & (feats["status"] == "completed")
                     & (feats["best_of"] == 3) & feats["y_three_sets"].notna()]
        dev = base[base["year"] <= train_end]
        val = base[(base["year"] >= v0) & (base["year"] <= v1)]

        # diagnóstico: tasa de 3 sets por época y por desequilibrio
        m_dev, m_val = _match_probs(tb, dev), _match_probs(tb, val)
        diag = {
            "base_rate_dev": round(float(dev["y_three_sets"].mean()), 4),
            "base_rate_val": round(float(val["y_three_sets"].mean()), 4),
            "rate_by_year_recent": {int(y): round(float(g["y_three_sets"].mean()), 4)
                                    for y, g in base[base["year"] >= 2018].groupby("year")},
        }
        # tasa observada por tramo de |m-0.5| en validación (el puente predice
        # 2s(1-s), demasiado plano si el efecto real es más fuerte)
        bal = pd.cut(np.abs(m_val - 0.5), [0, 0.1, 0.2, 0.3, 0.5])
        diag["rate_by_balance_val"] = {
            str(i): {"n": int(g.size), "obs": round(float(g.mean()), 4)}
            for i, g in val["y_three_sets"].groupby(bal, observed=True)}

        # probabilidad actual (pipeline vigente) en validación
        dcfg = tb["derived_cfg"]["three"]
        def _current(df_, m_):
            p_dir = dcfg["cal_direct"].transform(tb["derived_models"]["three"].predict(df_))
            p_bri = dcfg["cal_bridge"].transform(dv.bridge_prob("three", m_))
            return dv.combine(p_dir, p_bri, dcfg["w_direct"])
        p_cur_val = _current(val, m_val)
        y_val = val["y_three_sets"].to_numpy(dtype=float)
        s_cur, i_cur = calibration_slope_intercept(y_val, p_cur_val)

        # candidata: ConditionalPlatt ajustada en dev (OOF-approx: modelos ya
        # son honestos en dev por walk-forward; el calibrador se ajusta en dev)
        p_cur_dev = _current(dev, m_dev)
        y_dev = dev["y_three_sets"].to_numpy(dtype=float)
        cp = ConditionalPlatt().fit(p_cur_dev, m_dev, y_dev)
        p_fix_val = cp.transform(p_cur_val, m_val)
        s_fix, i_fix = calibration_slope_intercept(y_val, p_fix_val)

        res = {
            "diagnosis": diag,
            "current": {"ll_val": round(log_loss_safe(y_val, p_cur_val), 5),
                        "slope_val": round(s_cur, 3), "intercept_val": round(i_cur, 3)},
            "conditional_platt": {"ll_val": round(log_loss_safe(y_val, p_fix_val), 5),
                                  "slope_val": round(s_fix, 3), "intercept_val": round(i_fix, 3)},
        }
        improves = (res["conditional_platt"]["ll_val"] < res["current"]["ll_val"] - 0.001
                    and 0.85 <= s_fix <= 1.15)
        res["adopt"] = bool(improves)
        out["tours"][tour] = res

    adopt_both = all(out["tours"][t]["adopt"] for t in out["tours"])
    out["decision"] = ("adoptar recalibracion condicional (mejora robusta en ambos circuitos)"
                       if adopt_both else
                       "NO adoptar: mantener three_sets como experimental con AVISO de calibracion")
    out["adopted"] = adopt_both
    if adopt_both:
        for tour in ("ATP", "WTA"):
            tb = bundle["tours"][tour]
            base = feats[(feats["tour"] == tour) & (feats["status"] == "completed")
                         & (feats["best_of"] == 3) & feats["y_three_sets"].notna()]
            dev = base[base["year"] <= train_end]
            m_dev = _match_probs(tb, dev)
            dcfg = tb["derived_cfg"]["three"]
            p_dir = dcfg["cal_direct"].transform(tb["derived_models"]["three"].predict(dev))
            p_bri = dcfg["cal_bridge"].transform(dv.bridge_prob("three", m_dev))
            p_cur = dv.combine(p_dir, p_bri, dcfg["w_direct"])
            dcfg["conditional_platt"] = ConditionalPlatt().fit(
                p_cur, m_dev, dev["y_three_sets"].to_numpy(dtype=float))
        joblib.dump(bundle, art / "model_bundle.joblib")
    (reports / "three_sets_study.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


THRESHOLD_GRID = [
    {"ev_min": 0.02, "edge_min": 0.01}, {"ev_min": 0.02, "edge_min": 0.02},
    {"ev_min": 0.03, "edge_min": 0.02}, {"ev_min": 0.03, "edge_min": 0.03},
    {"ev_min": 0.04, "edge_min": 0.02}, {"ev_min": 0.04, "edge_min": 0.03},
    {"ev_min": 0.05, "edge_min": 0.03},
]


def threshold_sensitivity(cfg: dict) -> dict:
    """Informe de sensibilidad SOLO en validación (2023-24). No cambia defaults.
    PnL/yield heredan la etiqueta non_executable del modo A."""
    art = resolve_path(cfg, "artifacts_dir")
    reports = resolve_path(cfg, "reports_dir")
    bundle = joblib.load(art / "model_bundle.joblib")
    feats = pd.read_parquet(art / "features_all.parquet")
    feats["year"] = pd.to_datetime(feats["date"]).dt.year
    v0, v1 = cfg["splits"]["valid_years"]
    val = feats[(feats["year"] >= v0) & (feats["year"] <= v1)
                & feats["status"].isin(["completed", "retired"])
                & feats["label_a_wins"].notna() & feats["has_market"]]
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "period": f"{v0}-{v1}",
           "non_executable": True,
           "default": {"ev_min": cfg["selection"]["ev_min"], "edge_min": cfg["selection"]["edge_min"]},
           "note": "la configuracion por defecto NO cambia sin revision humana",
           "grid": []}

    rows_cache = []
    for tour in ("ATP", "WTA"):
        sub = val[val["tour"] == tour].copy()
        if sub.empty:
            continue
        tb = bundle["tours"][tour]
        sub["p_match"] = _match_probs(tb, sub)
        w = float(bundle["shrink_w"].get(tour, 0.5))
        for _, r in sub.iterrows():
            odds_all = json.loads(r["odds_json"])
            pair = odds_all.get(r["novig_book"])
            if not pair or not pair[0] or not pair[1]:
                continue
            rows_cache.append((tour, r, pair, w))

    for combo in THRESHOLD_GRID:
        sel_cfg = dict(cfg["selection"])
        sel_cfg.update(combo)
        bets = []
        for tour, r, pair, w in rows_cache:
            for side, (o_s, o_o) in (("a", (pair[0], pair[1])), ("b", (pair[1], pair[0]))):
                p_side = r["p_match"] if side == "a" else 1 - r["p_match"]
                me = evaluate_selection(
                    market="match_winner", selection=side, selection_name="x",
                    p_model=float(p_side), odds=float(o_s), opposite_odds=float(o_o),
                    bookmaker=r["novig_book"], tour_shrink_w=w, sigma=0.03,
                    is_experimental=False, hard_flags=[], cfg_sel=sel_cfg)
                if me.state in ("fuerte", "normal"):
                    won = (r["label_a_wins"] == 1) == (side == "a")
                    void = r["status"] == "retired" and (r["sets_a"] + r["sets_b"]) < 1
                    pnl = 0.0 if void else ((float(o_s) - 1.0) if won else -1.0)
                    bets.append({"odds": float(o_s), "pnl": pnl, "void": void, "won": won})
        b = pd.DataFrame(bets)
        entry = dict(combo)
        entry["n_bets"] = int(len(b))
        if len(b):
            live = b[~b["void"]]
            entry.update({
                "odds_min": round(float(b["odds"].min()), 2),
                "odds_max": round(float(b["odds"].max()), 2),
                "odds_avg": round(float(b["odds"].mean()), 2),
                "hit_rate": round(float(live["won"].mean()), 4) if len(live) else None,
                "pnl_units": round(float(b["pnl"].sum()), 2),
                "yield_non_executable": round(float(b["pnl"].sum() / max(len(live), 1)), 4),
            })
        out["grid"].append(entry)
    (reports / "threshold_sensitivity.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out
