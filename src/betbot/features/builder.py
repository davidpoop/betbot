"""Feature builder event-time con whitelist explícita.

Garantía as-of por construcción: una única pasada cronológica; el estado de
actividad (descanso, carga, retiradas) se lee ANTES de registrar el partido.

Dos grupos de features:
- DIFF (antisimétricas, se niegan al intercambiar A/B)
- CTX  (contexto simétrico del partido)
El predictor usa el wrapper simétrico p = (f(diff,ctx) + 1 - f(-diff,ctx)) / 2.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

DIFF_FEATURES = [
    "elo_diff", "elo_surf_diff", "log_rank_ratio", "log_pts_ratio",
    "rest_diff", "m14_diff", "m12m_diff", "layoff_diff", "retired_recent_diff",
    "age_diff", "lefty_diff", "experience_diff",
]
CTX_FEATURES = [
    "best_of5", "surface_clay", "surface_grass", "surface_carpet", "indoor", "round_level",
]
MARKET_FEATURE = "market_logit"          # solo la usa M5
FEATURES = DIFF_FEATURES + CTX_FEATURES  # matriz base sin mercado

META_COLS = ["match_id", "tour", "date", "player_a", "player_b", "best_of", "status",
             "label_a_wins", "set1_winner_a", "sets_a", "sets_b", "odds_json",
             "has_market", "p_novig_a_prop", "novig_book"]

_ROUND_LEVEL = {"1st round": 1, "2nd round": 2, "3rd round": 3, "4th round": 4,
                "round robin": 3, "quarterfinals": 5, "semifinals": 6, "the final": 7}

_RANK_MISSING = 500.0
_MAX_REST_DAYS = 30.0


def assert_whitelist(requested: list[str]) -> None:
    allowed = set(FEATURES + [MARKET_FEATURE])
    extra = [c for c in requested if c not in allowed]
    if extra:
        raise ValueError(f"Features fuera de la whitelist (posible leakage): {extra}")


def _market_prob(odds_json: str, priority: list[str]) -> tuple[float | None, str]:
    """p_novig proporcional de player_a usando la primera casa con AMBAS cuotas."""
    odds = json.loads(odds_json) if odds_json else {}
    for bk in priority:
        pair = odds.get(bk)
        if pair and pair[0] and pair[1] and pair[0] > 1.0 and pair[1] > 1.0:
            ia, ib = 1.0 / pair[0], 1.0 / pair[1]
            return ia / (ia + ib), bk
    return None, ""


def build_features(elo_df: pd.DataFrame, players: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Devuelve (tabla de features + meta, estado_actividad_final por tour/jugador)."""
    priority = list(cfg["data"]["book_priority"])
    bio = {p.player_id: {"dob": p.dob, "hand": p.hand} for p in players.itertuples(index=False)}

    last_date: dict[tuple, date] = {}
    recent: dict[tuple, list] = {}          # fechas ultimos 400 dias (pruned)
    last_retired: dict[tuple, date] = {}
    n_matches: dict[tuple, int] = {}

    rows: list[dict] = []
    df = elo_df.sort_values(["date", "match_id"]).reset_index(drop=True)
    for r in df.itertuples(index=False):
        d: date = r.date
        ka, kb = (r.tour, r.player_a), (r.tour, r.player_b)

        def _activity(k: tuple) -> tuple[float, int, int, float, float]:
            ld = last_date.get(k)
            rest = float((d - ld).days) if ld else _MAX_REST_DAYS
            rest = min(rest, _MAX_REST_DAYS)
            rec = recent.get(k, [])
            m14 = sum(1 for x in rec if (d - x).days <= 14)
            m12 = sum(1 for x in rec if (d - x).days <= 365)
            lr = last_retired.get(k)
            layoff = 1.0 if (ld and (d - ld).days >= cfg["selection"]["layoff_days_ood"]) else 0.0
            retired_recent = 1.0 if (lr and (d - lr).days <= 30) else 0.0
            return rest, m14, m12, layoff, retired_recent

        rest_a, m14_a, m12_a, lay_a, retr_a = _activity(ka)
        rest_b, m14_b, m12_b, lay_b, retr_b = _activity(kb)

        def _age(pid: str) -> float | None:
            b = bio.get(pid, {})
            dob = b.get("dob")
            if dob is None or (isinstance(dob, float) and math.isnan(dob)):
                return None
            return (d - dob).days / 365.25

        def _lefty(pid: str) -> float:
            h = bio.get(pid, {}).get("hand")
            return 1.0 if h == "L" else 0.0

        age_a, age_b = _age(r.player_a), _age(r.player_b)
        rank_a = float(r.rank_a) if r.rank_a is not None and not pd.isna(r.rank_a) else _RANK_MISSING
        rank_b = float(r.rank_b) if r.rank_b is not None and not pd.isna(r.rank_b) else _RANK_MISSING
        pts_a = float(r.pts_a) if r.pts_a is not None and not pd.isna(r.pts_a) else 0.0
        pts_b = float(r.pts_b) if r.pts_b is not None and not pd.isna(r.pts_b) else 0.0
        p_novig, novig_book = _market_prob(r.odds_json, priority)

        surface = r.surface
        feat = {
            "elo_diff": (r.elo_a - r.elo_b) / 100.0,
            "elo_surf_diff": (r.elo_surf_a - r.elo_surf_b) / 100.0,
            "log_rank_ratio": math.log(rank_b / rank_a),   # >0 si A mejor rankeado
            "log_pts_ratio": math.log1p(pts_a) - math.log1p(pts_b),
            "rest_diff": (rest_a - rest_b) / 7.0,
            "m14_diff": float(m14_a - m14_b),
            "m12m_diff": (m12_a - m12_b) / 10.0,
            "layoff_diff": lay_a - lay_b,
            "retired_recent_diff": retr_a - retr_b,
            "age_diff": ((age_a - age_b) / 5.0) if (age_a is not None and age_b is not None) else 0.0,
            "lefty_diff": _lefty(r.player_a) - _lefty(r.player_b),
            "experience_diff": math.log1p(r.n_prev_a) - math.log1p(r.n_prev_b),
            "best_of5": 1.0 if r.best_of == 5 else 0.0,
            "surface_clay": 1.0 if surface == "Clay" else 0.0,
            "surface_grass": 1.0 if surface == "Grass" else 0.0,
            "surface_carpet": 1.0 if surface == "Carpet" else 0.0,
            "indoor": 1.0 if r.indoor else 0.0,
            "round_level": float(_ROUND_LEVEL.get(str(r.round).strip().lower(), 1)) / 7.0,
            "market_logit": (math.log(p_novig / (1 - p_novig)) if p_novig is not None else 0.0),
            "has_market": p_novig is not None,
            "p_novig_a_prop": p_novig,
            "novig_book": novig_book,
        }
        meta = {c: getattr(r, c) for c in ("match_id", "tour", "date", "player_a", "player_b",
                                           "best_of", "status", "label_a_wins", "set1_winner_a",
                                           "sets_a", "sets_b", "odds_json")}
        rows.append(meta | feat)

        # registrar el partido DESPUES de leer el estado (as-of)
        if r.status != "walkover":
            for k in (ka, kb):
                last_date[k] = d
                rec = recent.setdefault(k, [])
                rec.append(d)
                if len(rec) > 200:
                    cutoff = d - timedelta(days=400)
                    recent[k] = [x for x in rec if x >= cutoff]
            if r.status == "retired":
                loser_key = kb if r.label_a_wins == 1 else ka
                last_retired[loser_key] = d
        n_matches[ka] = n_matches.get(ka, 0) + 1
        n_matches[kb] = n_matches.get(kb, 0) + 1

    out = pd.DataFrame(rows)
    freshness = {}
    if len(out):
        for tour, grp in out.groupby("tour"):
            freshness[str(tour)] = max(grp["date"]).isoformat()
    state = {"last_date": {f"{k[0]}|{k[1]}": v.isoformat() for k, v in last_date.items()},
             "last_retired": {f"{k[0]}|{k[1]}": v.isoformat() for k, v in last_retired.items()},
             "recent": {f"{k[0]}|{k[1]}": [x.isoformat() for x in v[-150:]] for k, v in recent.items()},
             "freshness": freshness}
    return out, state


def derived_targets(feats: pd.DataFrame) -> pd.DataFrame:
    """Targets de mercados de sets (solo Bo3; retiradas excluidas salvo set1)."""
    df = feats.copy()
    bo3 = (df["best_of"] == 3)
    comp = (df["status"] == "completed") & bo3 & df["label_a_wins"].notna()
    df["y_set1_a"] = df["set1_winner_a"]                       # valido tambien en retiradas con set1 completo
    df.loc[~bo3, "y_set1_a"] = np.nan
    df["y_wins_set_a"] = np.where(comp, (df["sets_a"] >= 1).astype(float), np.nan)
    df["y_straight_a"] = np.where(comp, ((df["sets_a"] == 2) & (df["sets_b"] == 0)).astype(float), np.nan)
    df["y_three_sets"] = np.where(comp, ((df["sets_a"] + df["sets_b"]) == 3).astype(float), np.nan)
    return df
