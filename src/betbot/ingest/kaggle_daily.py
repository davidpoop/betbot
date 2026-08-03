"""Parser del derivado "daily pull" de Tennis-Data (mirror gilmullinau).

Formato: Player_1/Player_2 con columna Winner, UNA cuota por lado (Odd_1/Odd_2,
casa no identificada -> book "TD1") y Score string orientado a Player_1.
-1 significa dato ausente. No hay columna de estado: las retiradas se infieren
del marcador incompleto.

Se reorienta a Winner/Loser para homogeneizar con tennis_data.py.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from betbot.canonical.score import infer_retired, parse_score_string

_KEEP = ["Tournament", "Date", "Series", "Court", "Surface", "Round", "Best of",
         "Player_1", "Player_2", "Winner", "Rank_1", "Rank_2", "Pts_1", "Pts_2",
         "Odd_1", "Odd_2", "Score"]


def _num(v):
    v = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(v) or v == -1 else float(v)


def load_daily_csv(path: Path, tour: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    cols = [c for c in _KEEP if c in df.columns]
    df = df[cols].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    rows: list[dict] = []
    for _, r in df.iterrows():
        p1, p2, w = str(r["Player_1"]).strip(), str(r["Player_2"]).strip(), str(r["Winner"]).strip()
        if not p1 or not p2 or r["Date"] is None or pd.isna(r["Date"]):
            continue
        p1_won = (w == p1)
        winner_raw, loser_raw = (p1, p2) if p1_won else (p2, p1)
        ps = parse_score_string(str(r.get("Score", "")))
        best_of = int(r.get("Best of", 3) or 3)
        # reorientar el marcador (Score va orientado a Player_1)
        if p1_won:
            sets_w, sets_l, g_w, g_l = ps.sets_w, ps.sets_l, ps.games_w, ps.games_l
            set1_w = ps.set1_w
        else:
            sets_w, sets_l, g_w, g_l = ps.sets_l, ps.sets_w, ps.games_l, ps.games_w
            set1_w = None if ps.set1_w is None else 1 - ps.set1_w
        retired = infer_retired(ps, best_of) if ps.n_sets > 0 else True
        comment = "Completed" if not retired else ("Walkover" if ps.n_sets == 0 else "Retired")
        o1, o2 = _num(r.get("Odd_1")), _num(r.get("Odd_2"))
        odds: dict[str, list] = {}
        wo, lo_ = (o1, o2) if p1_won else (o2, o1)
        if (wo is not None and wo > 1.0) or (lo_ is not None and lo_ > 1.0):
            odds["TD1"] = [wo if (wo and wo > 1.0) else None, lo_ if (lo_ and lo_ > 1.0) else None]
        rk1, rk2 = _num(r.get("Rank_1")), _num(r.get("Rank_2"))
        pt1, pt2 = _num(r.get("Pts_1")), _num(r.get("Pts_2"))
        rows.append({
            "tour": tour, "date": r["Date"], "tournament": r.get("Tournament", ""),
            "series": r.get("Series", "") if "Series" in df.columns else "",
            "court": r.get("Court", "Outdoor"), "surface": r.get("Surface", "Hard"),
            "round": r.get("Round", ""), "best_of": best_of,
            "winner_raw": winner_raw, "loser_raw": loser_raw,
            "wrank": rk1 if p1_won else rk2, "lrank": rk2 if p1_won else rk1,
            "wpts": pt1 if p1_won else pt2, "lpts": pt2 if p1_won else pt1,
            "w1": None, "l1": None, "w2": None, "l2": None, "w3": None, "l3": None,
            "w4": None, "l4": None, "w5": None, "l5": None,
            "wsets": sets_w, "lsets": sets_l, "comment": comment,
            "_score_str": str(r.get("Score", "")), "_set1_w": set1_w,
            "_games_w": g_w, "_games_l": g_l, "_set1_completed": ps.set1_completed,
            "odds": odds, "source": f"kaggle_daily:{Path(path).name}",
        })
    return pd.DataFrame(rows)
