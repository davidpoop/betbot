"""Entrenamiento completo: walk-forward, baselines M1–M5, calibración OOF,
mercados derivados (directo + puente + combo) y artefactos del screener.

División temporal (config): desarrollo <= train_end_year con walk-forward anual
expanding (OOF desde walkforward_first_valid_year); validación valid_years;
test_year SELLADO (solo backtest/run.py --test, que registra el acceso).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from betbot.config import resolve_path
from betbot.features.builder import FEATURES, MARKET_FEATURE, build_features, derived_targets
from betbot.models import derived as dv
from betbot.models.calibrate import (calibration_slope_intercept, expected_calibration_error,
                                     log_loss_safe, select_calibrator)
from betbot.models.predictor import PerspectiveModel, SymmetricEventModel, SymmetricModel
from betbot.ratings.elo import replay

MATCH_MODELS: dict[str, list[str]] = {
    "M1_rank": ["log_rank_ratio"],
    "M2_elo": ["elo_diff"],
    "M3_elo_surf": ["elo_diff", "elo_surf_diff"],
    "M4_full": FEATURES,
    "M5_market": FEATURES + [MARKET_FEATURE],
}


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True, timeout=10).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _fit_match_models(train: pd.DataFrame) -> dict[str, SymmetricModel]:
    y = train["label_a_wins"].to_numpy(dtype=float)
    models: dict[str, SymmetricModel] = {}
    for name, feats in MATCH_MODELS.items():
        sub = train[train["has_market"]] if MARKET_FEATURE in feats else train
        ysub = sub["label_a_wins"].to_numpy(dtype=float) if len(sub) != len(train) else y
        models[name] = SymmetricModel(feats).fit(sub, ysub)
    return models


def _fit_derived_models(train: pd.DataFrame) -> dict[str, object]:
    feats = FEATURES + [MARKET_FEATURE]
    out: dict[str, object] = {}
    t1 = train[train["y_set1_a"].notna()]
    out["set1"] = SymmetricModel(feats).fit(t1, t1["y_set1_a"].to_numpy(dtype=float))
    tw = train[train["y_wins_set_a"].notna()]
    # perspectiva B: B gana >=1 set  <=>  A NO gana 2-0
    out["wins_set"] = PerspectiveModel(feats).fit(
        tw, tw["y_wins_set_a"].to_numpy(dtype=float),
        y_b=(1.0 - tw["y_straight_a"].to_numpy(dtype=float)))
    out["straight"] = PerspectiveModel(feats).fit(
        tw, tw["y_straight_a"].to_numpy(dtype=float),
        y_b=(1.0 - tw["y_wins_set_a"].to_numpy(dtype=float)))
    t3 = train[train["y_three_sets"].notna()]
    out["three"] = SymmetricEventModel(feats).fit(t3, t3["y_three_sets"].to_numpy(dtype=float))
    return out


def run_training(cfg: dict) -> dict:
    canon = resolve_path(cfg, "canonical_dir")
    art = resolve_path(cfg, "artifacts_dir")
    reports = resolve_path(cfg, "reports_dir")
    np.random.seed(cfg["seed"])

    matches = pd.read_parquet(canon / "matches.parquet")
    players = pd.read_parquet(canon / "players.parquet")
    elo_df, elo_states = replay(matches, cfg["elo"])
    feats_all, activity_state = build_features(elo_df, players, cfg)
    feats_all = derived_targets(feats_all)
    feats_all["year"] = pd.to_datetime(feats_all["date"]).dt.year

    train_end = int(cfg["splits"]["train_end_year"])
    wf_first = int(cfg["splits"]["walkforward_first_valid_year"])
    # Retiradas EXCLUIDAS del target de entrenamiento (decisión pre-registrada,
    # análisis de sensibilidad pendiente en fase 2); sí cuentan en Elo y settlement.
    labelled = feats_all[(feats_all["status"] == "completed")
                         & feats_all["label_a_wins"].notna()].copy()

    report: dict = {"tours": {}, "meta": {}}
    bundle: dict = {"tours": {}, "cfg": cfg}

    for tour in ("ATP", "WTA"):
        tdf = labelled[labelled["tour"] == tour]
        dev = tdf[tdf["year"] <= train_end]
        # ---------- walk-forward OOF ----------
        oof_parts: list[pd.DataFrame] = []
        for vy in range(wf_first, train_end + 1):
            tr = dev[dev["year"] < vy]
            va = dev[dev["year"] == vy]
            if len(tr) < 2000 or len(va) == 0:
                continue
            mm = _fit_match_models(tr)
            dm = _fit_derived_models(tr)
            part = va[["match_id", "year", "has_market", "label_a_wins", "p_novig_a_prop",
                       "y_set1_a", "y_wins_set_a", "y_straight_a", "y_three_sets"]].copy()
            for name, model in mm.items():
                part[f"oof_{name}"] = model.predict(va)
            part["oof_d_set1"] = dm["set1"].predict(va)
            part["oof_d_wins_a"] = dm["wins_set"].predict(va, "a")
            part["oof_d_straight_a"] = dm["straight"].predict(va, "a")
            part["oof_d_three"] = dm["three"].predict(va)
            oof_parts.append(part)
        oof = pd.concat(oof_parts, ignore_index=True)

        # ---------- calibración del modelo de partido (OOF) ----------
        y_oof = oof["label_a_wins"].to_numpy(dtype=float)
        calibrators: dict[str, object] = {}
        cal_tables: dict[str, dict] = {}
        oof_metrics: dict[str, dict] = {}
        for name in MATCH_MODELS:
            mask = oof["has_market"].to_numpy() if name == "M5_market" else np.ones(len(oof), bool)
            p_raw = oof.loc[mask, f"oof_{name}"].to_numpy(dtype=float)
            y_m = y_oof[mask]
            cal, table = select_calibrator(p_raw, y_m)
            calibrators[name] = cal
            cal_tables[name] = table
            p_cal = cal.transform(p_raw)
            oof_metrics[name] = {
                "n": int(mask.sum()),
                "log_loss": round(log_loss_safe(y_m, p_cal), 5),
                "brier": round(float(np.mean((p_cal - y_m) ** 2)), 5),
                "acc": round(float(np.mean((p_cal > 0.5) == (y_m == 1))), 5),
                "calibrator": cal.name,
            }
        mkt_mask = oof["has_market"].to_numpy()
        p_mkt = oof.loc[mkt_mask, "p_novig_a_prop"].to_numpy(dtype=float)
        oof_metrics["M0_market_novig"] = {
            "n": int(mkt_mask.sum()),
            "log_loss": round(log_loss_safe(y_oof[mkt_mask], p_mkt), 5),
            "brier": round(float(np.mean((p_mkt - y_oof[mkt_mask]) ** 2)), 5),
            "acc": round(float(np.mean((p_mkt > 0.5) == (y_oof[mkt_mask] == 1))), 5),
            "calibrator": "-",
        }

        # ---------- p_match operativa OOF (M5 con mercado, M4 sin) ----------
        p_m4 = calibrators["M4_full"].transform(oof["oof_M4_full"].to_numpy(dtype=float))
        p_m5 = np.where(mkt_mask,
                        calibrators["M5_market"].transform(oof["oof_M5_market"].to_numpy(dtype=float)),
                        p_m4)
        oof["p_match_cal"] = p_m5

        # ---------- mercados derivados: directo vs puente vs combo (OOF) ----------
        derived_cfg: dict[str, dict] = {}
        derived_report: dict[str, dict] = {}
        oof_bo3 = oof.copy()
        for mkt in dv.DERIVED_MARKETS:
            ycol = dv.TARGET_COL[mkt]
            dcol = {"set1": "oof_d_set1", "wins_set": "oof_d_wins_a",
                    "straight": "oof_d_straight_a", "three": "oof_d_three"}[mkt]
            mask = oof_bo3[ycol].notna().to_numpy()
            y_d = oof_bo3.loc[mask, ycol].to_numpy(dtype=float)
            p_direct_raw = oof_bo3.loc[mask, dcol].to_numpy(dtype=float)
            p_bridge_raw = dv.bridge_prob(mkt, oof_bo3.loc[mask, "p_match_cal"].to_numpy(dtype=float))
            cal_direct, _ = select_calibrator(p_direct_raw, y_d)
            cal_bridge, _ = select_calibrator(p_bridge_raw, y_d)
            pd_c, pb_c = cal_direct.transform(p_direct_raw), cal_bridge.transform(p_bridge_raw)
            w_best, lls = dv.best_weight(y_d, pd_c, pb_c)
            derived_cfg[mkt] = {"cal_direct": cal_direct, "cal_bridge": cal_bridge, "w_direct": w_best}
            slope, intercept = calibration_slope_intercept(y_d, dv.combine(pd_c, pb_c, w_best))
            derived_report[mkt] = {
                "n": int(mask.sum()), "w_direct": w_best,
                **{k: round(v, 5) for k, v in lls.items()},
                "combo_slope": round(slope, 3), "combo_intercept": round(intercept, 3),
            }

        # ---------- ajuste final (todo el desarrollo) ----------
        final_match = _fit_match_models(dev)
        final_derived = _fit_derived_models(dev)

        # ---------- diagnóstico de calibración del candidato ----------
        slope, intercept = calibration_slope_intercept(y_oof, oof["p_match_cal"].to_numpy())
        ece = expected_calibration_error(y_oof, oof["p_match_cal"].to_numpy())

        bundle["tours"][tour] = {
            "match_models": final_match, "match_calibrators": calibrators,
            "derived_models": final_derived, "derived_cfg": derived_cfg,
        }
        report["tours"][tour] = {
            "n_dev": int(len(dev)), "n_oof": int(len(oof)),
            "oof_years": f"{wf_first}-{train_end}",
            "match_models_oof": oof_metrics,
            "match_calibrator_selection": {k: {kk: round(vv, 5) for kk, vv in v.items()}
                                           for k, v in cal_tables.items()},
            "p_match_operativa_oof": {
                "log_loss": round(log_loss_safe(y_oof, oof["p_match_cal"].to_numpy()), 5),
                "slope": round(slope, 3), "intercept": round(intercept, 3), "ece": round(ece, 4),
            },
            "derived_markets_oof": derived_report,
        }

    # ---------- shrinkage a mercado (se decide en validación, no en test) ----------
    valid_years = list(range(int(cfg["splits"]["valid_years"][0]),
                             int(cfg["splits"]["valid_years"][1]) + 1))
    shrink = _fit_shrink_w(cfg, feats_all, labelled, bundle, valid_years)
    bundle["shrink_w"] = shrink["w_by_tour"]
    report["shrink_w_validation"] = shrink

    # ---------- artefactos ----------
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "data_hash": _hash_file(canon / "matches.parquet"),
        "train_end_year": train_end, "valid_years": valid_years,
        "test_year": int(cfg["splits"]["test_year"]),
        "features": FEATURES + [MARKET_FEATURE],
    }
    bundle["meta"] = meta
    bundle["elo_states"] = elo_states
    bundle["activity_state"] = activity_state
    joblib.dump(bundle, art / "model_bundle.joblib")
    feats_all.to_parquet(art / "features_all.parquet", index=False)
    report["meta"] = meta
    (reports / "train_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def _fit_shrink_w(cfg: dict, feats_all: pd.DataFrame, labelled: pd.DataFrame,
                  bundle: dict, valid_years: list[int]) -> dict:
    """w de p_cons = w*p_modelo + (1-w)*p_novig elegido por log loss en validación."""
    out = {"w_by_tour": {}, "detail": {}}
    for tour in ("ATP", "WTA"):
        va = labelled[(labelled["tour"] == tour) & labelled["year"].isin(valid_years)
                      & labelled["has_market"]]
        if len(va) == 0:
            out["w_by_tour"][tour] = 0.5
            continue
        tb = bundle["tours"][tour]
        p_raw = tb["match_models"]["M5_market"].predict(va)
        p_model = tb["match_calibrators"]["M5_market"].transform(p_raw)
        p_mkt = va["p_novig_a_prop"].to_numpy(dtype=float)
        y = va["label_a_wins"].to_numpy(dtype=float)
        grid = np.linspace(0.0, 1.0, 21)
        lls = {float(w): log_loss_safe(y, np.clip(w * p_model + (1 - w) * p_mkt, 1e-6, 1 - 1e-6))
               for w in grid}
        w_best = min(lls, key=lls.get)
        out["w_by_tour"][tour] = w_best
        out["detail"][tour] = {"n": int(len(va)), "w": w_best,
                               "ll_model": round(lls[1.0], 5), "ll_market": round(lls[0.0], 5),
                               "ll_cons": round(lls[w_best], 5)}
    return out
