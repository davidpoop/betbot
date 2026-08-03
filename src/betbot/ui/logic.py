"""Lógica pura de la interfaz (testeable sin Streamlit)."""
from __future__ import annotations

import pandas as pd

FOLLOWABLE_STATES = {"fuerte", "normal", "experimental", "vigilar_precio"}

RESULT_COLS = ["match", "tour", "surface", "market", "selection_name", "bookmaker", "odds",
               "p_model", "fair_odds", "p_novig", "edge", "ev", "ev_cons", "o_min",
               "state", "recommended_primary", "reasons"]

STATE_ORDER = {"fuerte": 0, "normal": 1, "experimental": 2, "vigilar_precio": 3,
               "probable_sin_value": 4, "sin_value": 5, "descartada": 6}


def filter_results(df: pd.DataFrame, tours: list[str] | None = None,
                   surfaces: list[str] | None = None, markets: list[str] | None = None,
                   states: list[str] | None = None,
                   odds_range: tuple[float, float] | None = None) -> pd.DataFrame:
    """Filtros de la tabla principal. Las filas sin cuota solo se filtran por
    rango si este es más estrecho que el total (no se ocultan por defecto)."""
    out = df.copy()
    if tours:
        out = out[out["tour"].isin(tours)]
    if surfaces and "surface" in out.columns:
        out = out[out["surface"].isin(surfaces)]
    if markets:
        out = out[out["market"].isin(markets)]
    if states:
        out = out[out["state"].isin(states)]
    if odds_range is not None:
        lo, hi = odds_range
        with_odds = out["odds"].notna()
        keep = (~with_odds) | ((out["odds"] >= lo) & (out["odds"] <= hi))
        out = out[keep]
    return out


def sort_results(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["_rank"] = out["state"].map(STATE_ORDER).fillna(9)
    out = out.sort_values(["recommended_primary", "_rank", "ev_cons"],
                          ascending=[False, True, False]).drop(columns="_rank")
    return out


def followable(df: pd.DataFrame) -> pd.DataFrame:
    """Filas que pueden marcarse como seguidas virtualmente (necesitan cuota)."""
    if df.empty:
        return df
    return df[df["state"].isin(FOLLOWABLE_STATES) & df["odds"].notna()]


def row_label(r: pd.Series) -> str:
    return (f"{r['match']} · {r['market']} · {r['selection_name']} @ {r['odds']} "
            f"({r.get('bookmaker', '')}) [{r['state']}]")


def match_detail(df: pd.DataFrame, match: str) -> dict:
    sub = df[df["match"] == match]
    if sub.empty:
        return {}
    first = sub.iloc[0]
    return {
        "match": match, "tour": first["tour"], "surface": first.get("surface"),
        "date": first.get("date"), "tournament": first.get("tournament"),
        "set_distribution": first.get("set_distribution"),
        "rows": sub,
        "primary": sub[sub["recommended_primary"]].head(1),
    }
