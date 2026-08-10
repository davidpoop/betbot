"""RECENT OUTCOMES — segundo nivel de resultados, explícitamente limitado.

Un "recent outcome" es un resultado reciente fiable (ganador + sets ganados +
fecha + torneo) SIN el marcador completo por juegos. Sirve para actualizar el
ESTADO predictivo (Elo, actividad, experiencia) mientras llega el resultado
canónico completo de TML/Tennis-Data. JAMÁS entra en matches.parquet ni pasa
por prepare_rows como si tuviera marcador: vive en su propio almacén
(recent_outcomes.parquet) con detail_level="outcome_only".

Prohibido por diseño (y por tests): juegos inventados, stats de servicio
inventadas, ranking inventado, retirada inventada (queda "unknown").

RECONCILIACIÓN: el estado operativo se construye como
    full canonical  UNION  outcomes SIN equivalente full
con dedupe estricto por (tour, pareja, fecha ±1 día). En cuanto TML publica el
partido completo, el outcome deja de aportarse al estado (el full manda):
el Elo se aplica exactamente UNA vez, m14/experience cuentan UNA vez. La
reconciliación se calcula en carga (sin mutar el parquet: idempotente).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

OUTCOMES_FILE = "recent_outcomes.parquet"

OUTCOME_COLUMNS = [
    "event_id", "date", "tour", "tournament", "surface",
    "player_a", "player_b", "label_a_wins", "sets_a", "sets_b",
    "status", "retirement", "source", "observed_at",
    "detail_level", "completion_confidence", "canonical_match_id",
]


def load_outcomes(canon: Path) -> pd.DataFrame:
    p = Path(canon) / OUTCOMES_FILE
    if not p.exists():
        return pd.DataFrame(columns=OUTCOME_COLUMNS)
    return pd.read_parquet(p)


def append_outcomes(canon: Path, rows: pd.DataFrame) -> int:
    """Append-only con dedupe por canonical_match_id y por event_id."""
    if len(rows) == 0:
        return 0
    rows = rows[OUTCOME_COLUMNS].copy()
    existing = load_outcomes(canon)
    if len(existing):
        known_mid = set(existing["canonical_match_id"].dropna())
        known_eid = set(existing["event_id"].dropna())
        rows = rows[~rows["canonical_match_id"].isin(known_mid)
                    & ~rows["event_id"].isin(known_eid)]
    if len(rows) == 0:
        return 0
    out = pd.concat([existing, rows], ignore_index=True) if len(existing) else rows
    out.to_parquet(Path(canon) / OUTCOMES_FILE, index=False)
    return int(len(rows))


def unreconciled_outcomes(outcomes: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    """Outcomes SIN equivalente en el almacén full: dedupe estricto por
    (tour, player_a, player_b) con fecha a ±1 día (la fecha del outcome viene
    del commence UTC y puede diferir en un día de la del dataset oficial)."""
    if len(outcomes) == 0:
        return outcomes
    if len(full) == 0:
        return outcomes
    full_keys: set[tuple] = set()
    for r in full.itertuples(index=False):
        for dd in (-1, 0, 1):
            full_keys.add((str(r.tour), str(r.player_a), str(r.player_b),
                           r.date + timedelta(days=dd)))
    mask = []
    for r in outcomes.itertuples(index=False):
        mask.append((str(r.tour), str(r.player_a), str(r.player_b), r.date)
                    not in full_keys)
    return outcomes[pd.Series(mask, index=outcomes.index)]


def outcomes_as_state_rows(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Convierte outcomes al esquema de matches.parquet SOLO para el replay de
    estado (Elo/actividad/experiencia). Campos sin dato quedan vacíos de
    verdad, no inventados: games=0 (el builder no los usa como feature),
    set1_winner_a/rank/pts = None, odds vacías, round desconocida.

    `retirement` desconocida -> status "completed": el builder solo registra
    last_retired con status=="retired", así que la ausencia de evidencia NO
    marca retirada (misma semántica que tenían estos partidos cuando
    simplemente faltaban por retraso de la fuente; decisión documentada)."""
    rows = []
    for r in outcomes.itertuples(index=False):
        rows.append({
            "tour": r.tour, "date": r.date, "tournament": r.tournament,
            "series": "", "surface": r.surface or "Hard",
            "indoor": False, "round": "", "best_of": 5 if max(int(r.sets_a), int(r.sets_b)) >= 3 else 3,
            "player_a": r.player_a, "player_b": r.player_b,
            "raw_name_a": r.player_a, "raw_name_b": r.player_b,
            "label_a_wins": float(r.label_a_wins),
            "status": "completed" if str(r.retirement) != "retired" else "retired",
            "sets_a": int(r.sets_a), "sets_b": int(r.sets_b),
            "games_a": 0, "games_b": 0, "set1_winner_a": None,
            "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
            "odds_json": "{}", "source": f"recent_outcome:{r.source}",
            "match_id": r.canonical_match_id,
        })
    return pd.DataFrame(rows)


def load_state_matches(canon: Path) -> pd.DataFrame:
    """Estado operativo: full canonical UNION outcomes no reconciliados.
    Es la entrada de refresh_state y de completed_index (verificación
    already_completed) — nunca del ENTRENAMIENTO, que sigue leyendo solo el
    dataset full."""
    from betbot.canonical.store import load_matches
    full = load_matches(canon)
    extra = unreconciled_outcomes(load_outcomes(canon), full)
    if len(extra) == 0:
        return full
    return pd.concat([full, outcomes_as_state_rows(extra)],
                     ignore_index=True).sort_values(["date", "match_id"]).reset_index(drop=True)


def outcome_freshness(canon: Path) -> dict[str, date]:
    """Máxima fecha con outcome por tour (capacidad outcome_state)."""
    o = load_outcomes(canon)
    if len(o) == 0:
        return {}
    return {str(t): max(g["date"]) for t, g in o.groupby("tour")}


def outcome_coverage(canon: Path) -> dict[str, dict]:
    """{tour: {evento_canónico: {tournament, until}}} para la cobertura activa
    (los outcomes de The Odds API cubren SOLO torneos con sport key)."""
    from betbot.tournaments import canonical_event
    o = load_outcomes(canon)
    out: dict[str, dict] = {}
    for (t, name), g in (o.groupby(["tour", "tournament"]) if len(o) else []):
        key = canonical_event(str(t), str(name))
        until = max(g["date"])
        cur = out.setdefault(str(t), {})
        if key not in cur or until > cur[key]["until"]:
            cur[key] = {"tournament": str(name), "until": until}
    return out
