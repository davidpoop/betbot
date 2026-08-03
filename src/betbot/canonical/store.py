"""Accessor único del dataset de partidos: mirrors + resultados manuales.

- matches.parquet: derivado de los mirrors públicos (se reconstruye entero).
- manual_results.parquet: append-only, introducido a mano via `betbot import-results`;
  sobrevive a las reconstrucciones.
- Fusión con precedencia del MIRROR: si un match_id manual aparece después en el
  mirror, la fila del mirror gana (el manual queda como redundante, no se borra).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

MANUAL_FILE = "manual_results.parquet"


def load_matches(canonical_dir: Path) -> pd.DataFrame:
    canonical_dir = Path(canonical_dir)
    frames = []
    mirror = canonical_dir / "matches.parquet"
    if mirror.exists():
        frames.append(pd.read_parquet(mirror))
    manual = canonical_dir / MANUAL_FILE
    if manual.exists():
        frames.append(pd.read_parquet(manual))
    if not frames:
        raise FileNotFoundError(f"no hay dataset canonico en {canonical_dir}")
    df = pd.concat(frames, ignore_index=True)
    # precedencia del mirror: keep='first' con el mirror concatenado primero
    df = df.drop_duplicates(subset="match_id", keep="first")
    return df.sort_values(["date", "tour", "tournament", "match_id"]).reset_index(drop=True)


def existing_match_ids(canonical_dir: Path) -> set[str]:
    try:
        return set(load_matches(canonical_dir)["match_id"])
    except FileNotFoundError:
        return set()


def append_manual(canonical_dir: Path, rows: pd.DataFrame) -> int:
    """Append-only al parquet manual. Devuelve el total de filas manuales."""
    canonical_dir = Path(canonical_dir)
    path = canonical_dir / MANUAL_FILE
    if path.exists():
        prev = pd.read_parquet(path)
        rows = pd.concat([prev, rows], ignore_index=True)
    rows.to_parquet(path, index=False)
    return len(rows)
