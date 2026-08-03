"""Evaluación temporal y backtest histórico MODO A (non_executable).

- Por defecto evalúa los años de VALIDACIÓN. Con use_test=True evalúa el año de
  test SELLADO y registra el acceso en artifacts/reports/test_access_log.json.
- Las cuotas históricas de Tennis-Data no tienen timestamp: el PnL del modo A
  es una COTA SUPERIOR no ejecutable; sirve para comparar modelos, no para
  estimar rentabilidad real (etiqueta non_executable en todas las salidas).
- Settlement de retiradas: regla configurable; por defecto "1_set"
  (Pinnacle/Betfair: la apuesta se mantiene si se completó >=1 set; si no, void).
  bet365-style "match_completed" también implementada.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from betbot.config import resolve_path
from betbot.models.calibrate import (calibration_slope_intercept, expected_calibration_error,
                                     log_loss_safe)
from betbot.models import derived as dv
from betbot.value.engine import evaluate_selection

RETIREMENT_RULES = ("1_set", "match_completed")


def _paired_block_bootstrap_dll(df: pd.DataFrame, p_model: np.ndarray, p_mkt: np.ndarray,
                                y: np.ndarray, n_boot: int = 500, seed: int = 7) -> dict:
    """CI bootstrap por bloques (torneo-semana) de Delta LL (modelo - mercado)."""
    eps = 1e-6
    pm, pk = np.clip(p_model, eps, 1 - eps), np.clip(p_mkt, eps, 1 - eps)
    ll_m = -(y * np.log(pm) + (1 - y) * np.log(1 - pm))
    ll_k = -(y * np.log(pk) + (1 - y) * np.log(1 - pk))
    diff = ll_m - ll_k
    week = pd.to_datetime(df["date"]).dt.strftime("%G-%V")
    groups = pd.Series(diff).groupby(week.values).mean()
    rng = np.random.default_rng(seed)
    vals = np.array([groups.sample(len(groups), replace=True, random_state=rng.integers(1 << 31)).mean()
                     for _ in range(n_boot)])
    return {"dll_mean": round(float(diff.mean()), 5),
            "ci80": [round(float(np.quantile(vals, 0.10)), 5),
                     round(float(np.quantile(vals, 0.90)), 5)],
            "ci95": [round(float(np.quantile(vals, 0.025)), 5),
                     round(float(np.quantile(vals, 0.975)), 5)],
            "n_blocks": int(len(groups))}


def _settle(row: pd.Series, side: str, odds: float, rule: str) -> tuple[float, str]:
    """Devuelve (pnl, resultado) para stake plano 1u en moneyline."""
    if row["status"] == "walkover":
        return 0.0, "void"
    if row["status"] == "retired":
        completed_sets = (row.get("sets_a") or 0) + (row.get("sets_b") or 0)
        if rule == "match_completed" or completed_sets < 1:
            return 0.0, "void"
    won = (row["label_a_wins"] == 1) == (side == "a")
    return (odds - 1.0, "win") if won else (-1.0, "loss")


def run_evaluation(cfg: dict, use_test: bool = False) -> dict:
    art = resolve_path(cfg, "artifacts_dir")
    reports = resolve_path(cfg, "reports_dir")
    bundle = joblib.load(art / "model_bundle.joblib")
    feats = pd.read_parquet(art / "features_all.parquet")
    feats["year"] = pd.to_datetime(feats["date"]).dt.year

    if use_test:
        years = [int(cfg["splits"]["test_year"])]
        _log_test_access(reports)
        period = "TEST_SELLADO"
    else:
        years = list(range(int(cfg["splits"]["valid_years"][0]),
                           int(cfg["splits"]["valid_years"][1]) + 1))
        period = "validacion"

    rule = cfg.get("backtest", {}).get("retirement_rule", "1_set")
    assert rule in RETIREMENT_RULES
    report: dict = {"period": period, "years": years, "non_executable": True,
                    "retirement_rule": rule, "tours": {}}
    all_bets: list[dict] = []

    for tour in ("ATP", "WTA"):
        sub = feats[(feats["tour"] == tour) & feats["year"].isin(years)
                    & feats["status"].isin(["completed", "retired"])
                    & feats["label_a_wins"].notna()].copy()
        if sub.empty:
            continue
        tb = bundle["tours"][tour]
        p_m4 = tb["match_calibrators"]["M4_full"].transform(tb["match_models"]["M4_full"].predict(sub))
        has_m = sub["has_market"].to_numpy(bool)
        p_op = p_m4.copy()
        if has_m.any():
            p5 = tb["match_calibrators"]["M5_market"].transform(
                tb["match_models"]["M5_market"].predict(sub[has_m]))
            p_op[has_m] = p5
        sub["p_match"] = p_op
        comp = sub[sub["status"] == "completed"]
        y = comp["label_a_wins"].to_numpy(dtype=float)
        p = comp["p_match"].to_numpy(dtype=float)
        slope, intercept = calibration_slope_intercept(y, p)
        tour_metrics = {
            "n": int(len(comp)),
            "log_loss": round(log_loss_safe(y, p), 5),
            "brier": round(float(np.mean((p - y) ** 2)), 5),
            "acc": round(float(np.mean((p > 0.5) == (y == 1))), 5),
            "slope": round(slope, 3), "intercept": round(intercept, 3),
            "ece": round(expected_calibration_error(y, p), 4),
        }
        cm = comp[comp["has_market"]]
        if len(cm):
            ym = cm["label_a_wins"].to_numpy(dtype=float)
            pm = cm["p_match"].to_numpy(dtype=float)
            pk = cm["p_novig_a_prop"].to_numpy(dtype=float)
            tour_metrics["market_log_loss"] = round(log_loss_safe(ym, pk), 5)
            tour_metrics["dll_vs_market_bootstrap"] = _paired_block_bootstrap_dll(cm, pm, pk, ym)

        # ---------- backtest modo A (stake plano, non_executable) ----------
        w = float(bundle["shrink_w"].get(tour, 0.5))
        sigma_default = 0.03
        bets = []
        for _, r in sub[sub["has_market"]].iterrows():
            odds_all = json.loads(r["odds_json"])
            book = r["novig_book"]
            pair = odds_all.get(book)
            if not pair or not pair[0] or not pair[1]:
                continue
            for side, (o_side, o_opp) in (("a", (pair[0], pair[1])), ("b", (pair[1], pair[0]))):
                p_side = r["p_match"] if side == "a" else 1.0 - r["p_match"]
                me = evaluate_selection(
                    market="match_winner", selection=side,
                    selection_name=r["player_a"] if side == "a" else r["player_b"],
                    p_model=float(p_side), odds=float(o_side), opposite_odds=float(o_opp),
                    bookmaker=book, tour_shrink_w=w, sigma=sigma_default,
                    is_experimental=False, hard_flags=[], cfg_sel=cfg["selection"])
                if me.state in ("fuerte", "normal"):
                    pnl, result = _settle(r, side, float(o_side), rule)
                    bets.append({"date": str(r["date"]), "tour": tour, "match_id": r["match_id"],
                                 "side": side, "sel": me.selection_name, "odds": float(o_side),
                                 "book": book, "state": me.state, "ev_cons": me.ev_cons,
                                 "edge": me.edge, "pnl": pnl, "result": result,
                                 "non_executable": True})
        bdf = pd.DataFrame(bets)
        bt: dict = {"n_bets": int(len(bdf))}
        if len(bdf):
            pnl = bdf["pnl"].to_numpy()
            cum = np.cumsum(pnl)
            dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
            settled = bdf[bdf["result"] != "void"]
            bt.update({
                "pnl_units": round(float(pnl.sum()), 2),
                "yield": round(float(pnl.sum() / max(len(settled), 1)), 4),
                "hit_rate": round(float((settled["result"] == "win").mean()), 4) if len(settled) else None,
                "n_void": int((bdf["result"] == "void").sum()),
                "max_drawdown_units": round(dd, 2),
                "avg_odds": round(float(bdf["odds"].mean()), 3),
                "by_state": {k: int(v) for k, v in bdf["state"].value_counts().items()},
            })
        all_bets.extend(bets)

        # ---------- mercados derivados (experimentales): calibración fuera de muestra ----------
        deriv = {}
        bo3 = sub[(sub["best_of"] == 3) & (sub["status"] == "completed")].copy()
        if len(bo3):
            dcfg = tb["derived_cfg"]
            dm = tb["derived_models"]
            preds = {
                "set1": dm["set1"].predict(bo3),
                "wins_set": dm["wins_set"].predict(bo3, "a"),
                "straight": dm["straight"].predict(bo3, "a"),
                "three": dm["three"].predict(bo3),
            }
            for mkt in dv.DERIVED_MARKETS:
                ycol = dv.TARGET_COL[mkt]
                mask = bo3[ycol].notna().to_numpy()
                if mask.sum() < 200:
                    continue
                y_d = bo3.loc[mask, ycol].to_numpy(dtype=float)
                p_dir = dcfg[mkt]["cal_direct"].transform(np.asarray(preds[mkt])[mask])
                p_bri = dcfg[mkt]["cal_bridge"].transform(
                    dv.bridge_prob(mkt, bo3.loc[mask, "p_match"].to_numpy(dtype=float)))
                p_fin = dv.combine(p_dir, p_bri, dcfg[mkt]["w_direct"])
                s_d, i_d = calibration_slope_intercept(y_d, p_fin)
                deriv[mkt] = {"n": int(mask.sum()),
                              "log_loss": round(log_loss_safe(y_d, p_fin), 5),
                              "base_rate": round(float(y_d.mean()), 4),
                              "slope": round(s_d, 3), "intercept": round(i_d, 3),
                              "ece": round(expected_calibration_error(y_d, p_fin), 4)}
        report["tours"][tour] = {"match": tour_metrics,
                                 "backtest_modo_A_non_executable": bt,
                                 "derived_experimental": deriv}

    out_name = f"eval_{'test' if use_test else 'val'}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    (reports / f"{out_name}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    if all_bets:
        pd.DataFrame(all_bets).to_csv(reports / f"{out_name}_bets.csv", index=False)
    report["report_file"] = str(reports / f"{out_name}.json")
    return report


def _log_test_access(reports: Path) -> None:
    log_path = reports / "test_access_log.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {"accesses": []}
    log["accesses"].append(datetime.now(timezone.utc).isoformat())
    log_path.write_text(json.dumps(log, indent=2))
    if len(log["accesses"]) > 1:
        print(f"AVISO: el test sellado ya se habia consultado {len(log['accesses']) - 1} veces. "
              "Accesos repetidos invalidan su valor como auditoria final.")
