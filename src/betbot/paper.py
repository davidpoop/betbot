"""Paper trading prospectivo: picks seguidos virtualmente, settlement por reglas,
CLV manual y métricas ESTRICTAMENTE separadas del backtest histórico (modo A).

Almacén: artifacts/ledger/paper_picks.csv (append/update; nunca se borra).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from betbot.canonical.names import canonical_key
from betbot.config import resolve_path

COLUMNS = ["pick_id", "created_at", "match_date", "tour", "tournament",
           "player_a", "player_b", "market", "selection", "selection_name",
           "odds", "bookmaker", "p_model", "p_novig", "ev_cons", "state",
           "model_git_sha", "model_data_hash", "status", "result", "pnl",
           "close_odds", "close_opposite_odds", "close_ts", "clv_odds_pct", "clv_prob_pp",
           "settled_at", "notes"]

VALID_MARKETS = {"match_winner", "set1_winner", "wins_set", "straight_sets", "three_sets"}


def _store_path(cfg: dict) -> Path:
    return resolve_path(cfg, "ledger_dir") / "paper_picks.csv"


NUM_COLS = ["odds", "p_model", "p_novig", "ev_cons", "pnl", "close_odds",
            "close_opposite_odds", "clv_odds_pct", "clv_prob_pp"]


def load_picks(cfg: dict) -> pd.DataFrame:
    p = _store_path(cfg)
    if not p.exists():
        df = pd.DataFrame({c: pd.Series(dtype=("float64" if c in NUM_COLS else "object"))
                           for c in COLUMNS})
        return df
    df = pd.read_csv(p, dtype=str)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[COLUMNS]


def _save(cfg: dict, df: pd.DataFrame) -> None:
    df.to_csv(_store_path(cfg), index=False)


def add_pick(cfg: dict, row: dict) -> str:
    """Registra una candidata como seguida virtualmente. `row` = fila del screener."""
    if row.get("market") not in VALID_MARKETS:
        raise ValueError(f"mercado invalido: {row.get('market')}")
    if not row.get("odds") or float(row["odds"]) <= 1.0:
        raise ValueError("un pick necesita cuota decimal > 1.0")
    df = load_picks(cfg)
    dup = df[(df["status"] == "open")
             & (df["player_a"] == row.get("player_a", row.get("match", "").split(" vs ")[0]))
             & (df["market"] == row["market"]) & (df["selection"] == row["selection"])]
    pick_id = uuid.uuid4().hex[:10]
    players = row.get("match", " vs ").split(" vs ")
    rec = {c: None for c in COLUMNS}
    rec.update({
        "pick_id": pick_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "match_date": row.get("date"), "tour": row.get("tour"),
        "tournament": row.get("tournament", ""),
        "player_a": row.get("player_a", players[0] if players else ""),
        "player_b": row.get("player_b", players[-1] if players else ""),
        "market": row["market"], "selection": row["selection"],
        "selection_name": row.get("selection_name", ""),
        "odds": float(row["odds"]), "bookmaker": row.get("bookmaker", "manual"),
        "p_model": row.get("p_model"), "p_novig": row.get("p_novig"),
        "ev_cons": row.get("ev_cons"), "state": row.get("state"),
        "model_git_sha": row.get("model_git_sha"), "model_data_hash": row.get("model_data_hash"),
        "status": "open", "notes": ("duplicado_de_pick_abierto;" if len(dup) else "") + str(row.get("notes", "") or ""),
    })
    df = pd.concat([df, pd.DataFrame([rec])], ignore_index=True)
    _save(cfg, df)
    return pick_id


def set_close(cfg: dict, pick_id: str, close_odds: float,
              close_opposite_odds: float | None = None, ts: str | None = None) -> dict:
    """Introduce la cuota posterior/cierre y calcula CLV. No inventa nada:
    - clv_odds_pct siempre que haya cierre del mismo lado;
    - clv_prob_pp solo si hay cierre de AMBOS lados y no-vig de entrada."""
    df = load_picks(cfg)
    idx = df.index[df["pick_id"] == pick_id]
    if len(idx) == 0:
        raise KeyError(f"pick no encontrado: {pick_id}")
    i = idx[0]
    if not close_odds or close_odds <= 1.0:
        raise ValueError("cuota de cierre invalida")
    df.loc[i, "close_odds"] = close_odds
    df.loc[i, "close_opposite_odds"] = close_opposite_odds
    df.loc[i, "close_ts"] = ts or datetime.now(timezone.utc).isoformat()
    df.loc[i, "clv_odds_pct"] = round((float(df.loc[i, "odds"]) / close_odds - 1.0) * 100, 3)
    if close_opposite_odds and close_opposite_odds > 1.0:
        ic, io = 1.0 / close_odds, 1.0 / close_opposite_odds
        p_close = ic / (ic + io)
        p_entry = df.loc[i, "p_novig"]
        if pd.notna(p_entry):
            df.loc[i, "clv_prob_pp"] = round((p_close - float(p_entry)) * 100, 3)
    _save(cfg, df)
    return df.loc[i].to_dict()


def _determined_outcome(market: str, sel: str, flip: bool, row: pd.Series,
                        rule: str) -> tuple[str, float] | None:
    """Resultado del pick según el partido canónico. Devuelve (result, pnl_mult)
    con pnl_mult en {1: win, -1: loss, 0: void} o None si aún no determinable."""
    status = row["status"]
    if status == "walkover":
        return ("void", 0)
    label_a = row["label_a_wins"]
    sets_a, sets_b = int(row["sets_a"] or 0), int(row["sets_b"] or 0)
    # perspectiva del usuario -> canónica
    sel_c = sel
    if sel in ("a", "b") and flip:
        sel_c = "b" if sel == "a" else "a"
    my_sets, opp_sets = (sets_a, sets_b) if sel_c == "a" else (sets_b, sets_a)
    completed = status == "completed"
    retired = status == "retired"

    if market == "match_winner":
        if retired and (rule == "match_completed" or (sets_a + sets_b) < 1):
            return ("void", 0)
        if pd.isna(label_a):
            return ("void", 0)
        won = (int(label_a) == 1) == (sel_c == "a")
        return ("win", 1) if won else ("loss", -1)
    if market == "set1_winner":
        s1 = row["set1_winner_a"]
        if s1 is None or pd.isna(s1):
            return ("void", 0)
        won = (int(s1) == 1) == (sel_c == "a")
        return ("win", 1) if won else ("loss", -1)
    if market == "wins_set":
        if my_sets >= 1:
            return ("win", 1)                       # determinado aunque haya retirada
        if completed:
            return ("loss", -1)
        return ("void", 0)                          # retirada sin set ganado: no determinable
    if market == "straight_sets":
        if my_sets >= 1 and opp_sets >= 1:
            return ("loss", -1)                     # ya perdió un set: determinado
        if completed:
            need = 2 if row["best_of"] == 3 else 3
            return ("win", 1) if (my_sets >= need and opp_sets == 0) else ("loss", -1)
        if retired and opp_sets >= 1 and sel_c == ("a" if sets_b >= 1 else "b"):
            return ("loss", -1)
        return ("void", 0)
    if market == "three_sets":
        went_three = sets_a >= 1 and sets_b >= 1
        if went_three:
            return ("win", 1) if sel == "yes" else ("loss", -1)
        if completed:
            return ("loss", -1) if sel == "yes" else ("win", 1)
        return ("void", 0)
    return ("void", 0)


def settle_from_canonical(cfg: dict) -> dict:
    """Liquida los picks abiertos contra el dataset canónico actualizado."""
    canon = resolve_path(cfg, "canonical_dir")
    matches = pd.read_parquet(canon / "matches.parquet")
    rule = cfg.get("paper", {}).get("retirement_rule", "1_set")
    df = load_picks(cfg)
    open_idx = df.index[df["status"] == "open"]
    settled, still_open = 0, 0
    for i in open_idx:
        ka, kb = canonical_key(df.loc[i, "player_a"]), canonical_key(df.loc[i, "player_b"])
        a_c, b_c = (ka, kb) if ka < kb else (kb, ka)
        flip = a_c != ka
        cand = matches[(matches["tour"] == df.loc[i, "tour"])
                       & (matches["player_a"] == a_c) & (matches["player_b"] == b_c)]
        if df.loc[i, "match_date"]:
            d0 = pd.Timestamp(df.loc[i, "match_date"]).date()
            cand = cand[pd.to_datetime(cand["date"]).dt.date.map(
                lambda d: abs((d - d0).days) <= 3)]
        if cand.empty:
            still_open += 1
            continue
        row = cand.sort_values("date").iloc[-1]
        out = _determined_outcome(df.loc[i, "market"], df.loc[i, "selection"], flip, row, rule)
        if out is None:
            still_open += 1
            continue
        result, mult = out
        odds = float(df.loc[i, "odds"])
        df.loc[i, "result"] = result
        df.loc[i, "pnl"] = round((odds - 1.0) if mult == 1 else (-1.0 if mult == -1 else 0.0), 4)
        df.loc[i, "status"] = "settled" if result != "void" else "void"
        df.loc[i, "settled_at"] = datetime.now(timezone.utc).isoformat()
        settled += 1
    _save(cfg, df)
    return {"settled": settled, "still_open": still_open, "rule": rule}


def settle_manual(cfg: dict, pick_id: str, result: str) -> dict:
    """Liquidación manual (win|loss|void) para partidos aún no presentes en el canónico."""
    if result not in ("win", "loss", "void"):
        raise ValueError("result debe ser win|loss|void")
    df = load_picks(cfg)
    idx = df.index[df["pick_id"] == pick_id]
    if len(idx) == 0:
        raise KeyError(f"pick no encontrado: {pick_id}")
    i = idx[0]
    odds = float(df.loc[i, "odds"])
    df.loc[i, "result"] = result
    df.loc[i, "pnl"] = round((odds - 1.0) if result == "win" else (-1.0 if result == "loss" else 0.0), 4)
    df.loc[i, "status"] = "settled" if result != "void" else "void"
    df.loc[i, "settled_at"] = datetime.now(timezone.utc).isoformat()
    df.loc[i, "notes"] = (str(df.loc[i, "notes"] or "")) + ";settle_manual"
    _save(cfg, df)
    return df.loc[i].to_dict()


def metrics(cfg: dict) -> dict:
    """Métricas PROSPECTIVAS del paper trading (jamás mezclar con modo A)."""
    df = load_picks(cfg)
    out: dict = {"prospective": True, "n_picks": int(len(df)),
                 "n_open": int((df["status"] == "open").sum()),
                 "n_void": int((df["status"] == "void").sum())}
    st = df[df["status"] == "settled"].copy()
    out["n_settled"] = int(len(st))
    if len(st) == 0:
        out["warning"] = "sin picks liquidados todavia"
        return out
    st = st.sort_values("settled_at")
    pnl = st["pnl"].astype(float).to_numpy()
    cum = np.cumsum(pnl)
    out.update({
        "pnl_units": round(float(pnl.sum()), 2),
        "yield": round(float(pnl.mean()), 4),
        "hit_rate": round(float((st["result"] == "win").mean()), 4),
        "avg_odds": round(float(st["odds"].astype(float).mean()), 3),
        "max_drawdown_units": round(float(np.max(np.maximum.accumulate(cum) - cum)), 2) if len(cum) else 0.0,
    })
    clv = df["clv_odds_pct"].dropna().astype(float)
    if len(clv):
        out["clv_odds_pct_mean"] = round(float(clv.mean()), 3)
        out["n_with_close"] = int(len(clv))
    clvp = df["clv_prob_pp"].dropna().astype(float)
    if len(clvp):
        out["clv_prob_pp_mean"] = round(float(clvp.mean()), 3)
    for dim in ("tour", "market", "state"):
        grp = st.groupby(dim)["pnl"].agg(["count", "sum"])
        out[f"by_{dim}"] = {str(k): {"n": int(v["count"]), "pnl": round(float(v["sum"]), 2)}
                            for k, v in grp.iterrows()}
    st["odds_bucket"] = pd.cut(st["odds"].astype(float), [1.0, 1.4, 1.6, 2.0, 3.0, 100.0],
                               labels=["<1.40", "1.40-1.60", "1.60-2.00", "2.00-3.00", ">3.00"])
    grp = st.groupby("odds_bucket", observed=True)["pnl"].agg(["count", "sum"])
    out["by_odds"] = {str(k): {"n": int(v["count"]), "pnl": round(float(v["sum"]), 2)}
                      for k, v in grp.iterrows() if v["count"] > 0}
    if len(st) < 100:
        out["warning"] = f"muestra pequeña ({len(st)} liquidados): las métricas son ruido en su mayor parte"
    return out
