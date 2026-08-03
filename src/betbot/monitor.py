"""Monitorización prospectiva: días, estados, descartes y calibración realizada.

Fuentes: ledger del screener (screen_runs.jsonl), picks de paper trading y el
dataset canónico (para resolver resultados de partidos ya jugados). Todo es
PROSPECTIVO: nunca se mezcla con el backtest histórico modo A.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from betbot.canonical.names import canonical_key
from betbot.config import resolve_path
from betbot.models.calibrate import (calibration_slope_intercept, expected_calibration_error,
                                     log_loss_safe)

PICK_STATES = ("fuerte", "normal", "experimental")


def _runs(cfg: dict) -> list[dict]:
    p = resolve_path(cfg, "ledger_dir") / "screen_runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def days_summary(cfg: dict) -> dict:
    """Candidatas por día, % de días sin picks, distribución de estados y descartes."""
    runs = _runs(cfg)
    if not runs:
        return {"n_runs": 0, "warning": "sin ejecuciones del screener todavia"}
    by_day: dict[str, Counter] = {}
    reasons = Counter()
    states = Counter()
    for r in runs:
        day = r["run_at"][:10]
        c = by_day.setdefault(day, Counter())
        for row in r.get("rows", []):
            st = row.get("state", "")
            c[st] += 1
            states[st] += 1
            if st == "descartada":
                for code in str(row.get("reasons", "")).split(";"):
                    code = code.split(":")[0].strip()
                    if code:
                        reasons[code] += 1
    days = sorted(by_day)
    picks_per_day = {d: sum(v for k, v in c.items() if k in PICK_STATES) for d, c in by_day.items()}
    zero_days = sum(1 for v in picks_per_day.values() if v == 0)
    out = {
        "n_runs": len(runs), "n_days": len(days),
        "first_day": days[0], "last_day": days[-1],
        "states_total": dict(states),
        "candidates_per_day": picks_per_day,
        "mean_candidates_per_day": round(float(np.mean(list(picks_per_day.values()))), 2),
        "pct_zero_pick_days": round(100.0 * zero_days / max(len(days), 1), 1),
        "discard_reason_codes": dict(reasons.most_common(15)),
    }
    if len(days) < 10:
        out["warning"] = f"solo {len(days)} dias de uso: metricas orientativas"
    return out


def prospective_calibration(cfg: dict) -> dict:
    """Calibración de las probabilidades EMITIDAS por el screener contra los
    resultados reales posteriores (via canónico). Solo moneyline; se usa la
    última emisión por partido."""
    runs = _runs(cfg)
    canon = resolve_path(cfg, "canonical_dir")
    if not runs or not (canon / "matches.parquet").exists():
        return {"n": 0, "warning": "sin datos suficientes"}
    from betbot.canonical.store import load_matches
    matches = load_matches(canon)
    matches["d"] = pd.to_datetime(matches["date"]).dt.date

    latest: dict[tuple, dict] = {}
    for r in runs:
        for row in r.get("rows", []):
            if row.get("market") != "match_winner" or row.get("p_model") in (None, ""):
                continue
            names = str(row.get("match", "")).split(" vs ")
            if len(names) != 2:
                continue
            ka, kb = canonical_key(names[0]), canonical_key(names[1])
            a_c, b_c = (ka, kb) if ka < kb else (kb, ka)
            key = (row.get("date"), a_c, b_c)
            # guardar la probabilidad del jugador canonico A
            sel = row.get("selection")
            p_row = float(row["p_model"])
            sel_key = ka if sel == "a" else kb
            p_a = p_row if sel_key == a_c else 1.0 - p_row
            latest[key] = {"p_a": p_a, "run_at": r["run_at"], "tour": row.get("tour")}

    recs = []
    for (dstr, a_c, b_c), v in latest.items():
        try:
            d0 = pd.Timestamp(dstr).date()
        except (TypeError, ValueError):
            continue
        cand = matches[(matches["player_a"] == a_c) & (matches["player_b"] == b_c)
                       & (matches["status"] == "completed")]
        cand = cand[cand["d"].map(lambda d: abs((d - d0).days) <= 3)]
        if cand.empty:
            continue
        row = cand.iloc[-1]
        recs.append({"p": v["p_a"], "y": float(row["label_a_wins"]), "tour": v["tour"]})
    if not recs:
        return {"n": 0, "warning": "ningun partido emitido tiene todavia resultado en el canonico"}
    df = pd.DataFrame(recs)
    y, p = df["y"].to_numpy(), df["p"].to_numpy()
    out = {"n": int(len(df)), "log_loss": round(log_loss_safe(y, p), 5),
           "brier": round(float(np.mean((p - y) ** 2)), 5),
           "acc": round(float(np.mean((p > 0.5) == (y == 1))), 4),
           "ece": round(expected_calibration_error(y, p), 4)}
    if len(df) >= 100:
        s, b = calibration_slope_intercept(y, p)
        out["slope"], out["intercept"] = round(s, 3), round(b, 3)
    else:
        out["warning"] = f"muestra pequeña (n={len(df)}): pendiente/intercepto no fiables aun"
    bins = pd.cut(p, [0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0])
    tab = df.groupby(bins, observed=True).agg(n=("y", "size"), obs=("y", "mean"), pred=("p", "mean"))
    out["reliability_bins"] = [
        {"bin": str(i), "n": int(r["n"]), "pred": round(float(r["pred"]), 3),
         "obs": round(float(r["obs"]), 3)} for i, r in tab.iterrows() if r["n"] > 0]
    return out
