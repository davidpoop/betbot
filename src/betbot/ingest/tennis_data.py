"""Parsers de la familia Tennis-Data (orientación Winner/Loser, cuotas por casa).

Cubre dos formatos físicos con el mismo esquema lógico:
- CSV concatenado del mirror MLT (ATP 2000–2019, WTA 2007–2019).
- XLSX anuales originales (ATP 2020–2026, mirror gmalbert).

Salida: DataFrame homogéneo con una fila por partido y columnas:
tour, date, tournament, series, court, surface, round, best_of,
winner_raw, loser_raw, wrank, lrank, wpts, lpts, w1..w5, l1..l5,
wsets, lsets, comment, odds_json (dict book -> [w_odds, l_odds]), source.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

# Sufijos W/L de casas conocidas en tennis-data (varían por año)
_BOOK_PREFIXES = ["B365", "PS", "Max", "Avg", "CB", "EX", "GB", "IW", "LB", "SB", "SJ", "UB", "B&W", "BFE"]

_BASE_COLS = {
    "Tournament": "tournament", "Date": "date", "Series": "series", "Court": "court",
    "Surface": "surface", "Round": "round", "Best of": "best_of", "Winner": "winner_raw",
    "Loser": "loser_raw", "WRank": "wrank", "LRank": "lrank", "WPts": "wpts", "LPts": "lpts",
    "Wsets": "wsets", "Lsets": "lsets", "Comment": "comment", "Tier": "series",
}


def _clean_odds(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f <= 1.0 or f > 1001.0:
        return None
    return f


def _extract(df: pd.DataFrame, tour: str, source: str) -> pd.DataFrame:
    out = pd.DataFrame()
    for src_col, dst in _BASE_COLS.items():
        if src_col in df.columns and dst not in out.columns:
            out[dst] = df[src_col]
    for i in range(1, 6):
        out[f"w{i}"] = pd.to_numeric(df.get(f"W{i}"), errors="coerce")
        out[f"l{i}"] = pd.to_numeric(df.get(f"L{i}"), errors="coerce")
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["best_of"] = pd.to_numeric(out.get("best_of"), errors="coerce").fillna(3).astype(int)
    for c in ("wrank", "lrank"):
        out[c] = pd.to_numeric(out.get(c), errors="coerce")
    for c in ("wpts", "lpts"):
        out[c] = pd.to_numeric(out.get(c), errors="coerce")
    if "series" not in out.columns:
        out["series"] = ""
    if "comment" not in out.columns:
        out["comment"] = "Completed"

    odds_cols: dict[str, tuple[str, str]] = {}
    for bk in _BOOK_PREFIXES:
        wcol, lcol = f"{bk}W", f"{bk}L"
        if wcol in df.columns and lcol in df.columns:
            odds_cols[bk] = (wcol, lcol)
    odds_list: list[dict] = []
    for _, row in df.iterrows():
        d: dict[str, list[float]] = {}
        for bk, (wc, lc) in odds_cols.items():
            wo, lo = _clean_odds(row[wc]), _clean_odds(row[lc])
            if wo is not None or lo is not None:
                d[bk] = [wo, lo]
        odds_list.append(d)
    out["odds"] = odds_list
    out["tour"] = tour
    out["source"] = source
    return out[out["date"].notna() & out["winner_raw"].notna() & out["loser_raw"].notna()]


def load_mlt_csv(path: Path, tour: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0, low_memory=False)
    return _extract(df, tour, f"mlt:{Path(path).name}")


def load_xlsx_year(path: Path, tour: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    return _extract(df, tour, f"tennis_data:{Path(path).name}")
