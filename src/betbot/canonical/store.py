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


def enrich_manual(canonical_dir: Path, updates: dict[str, dict]) -> dict:
    """Enriquecimiento AUDITADO de filas manuales ya almacenadas.

    Solo toca manual_results.parquet (el mirror es intocable) y solo dos casos:
    - rellenar rank/pts NULOS con el valor que la fuente publica ahora
      (`ranks_filled`);
    - corregir status/score/label cuando la fuente trae un RESULTADO distinto
      para un partido nuestro — p.ej. una corrección posterior del feed
      (`results_corrected`).
    Devuelve contadores; cada corrección queda listada para el log del sync.
    """
    canonical_dir = Path(canonical_dir)
    path = canonical_dir / MANUAL_FILE
    out = {"ranks_filled": 0, "results_corrected": 0, "corrections": []}
    if not path.exists() or not updates:
        return out
    df = pd.read_parquet(path)
    idx_by_id = {mid: i for i, mid in enumerate(df["match_id"])}
    for mid, up in updates.items():
        i = idx_by_id.get(mid)
        if i is None:
            continue
        row = df.iloc[i]
        filled = False
        for col in ("rank_a", "rank_b", "pts_a", "pts_b"):
            v = up.get(col)
            if v is not None and pd.isna(row[col]):
                df.iat[i, df.columns.get_loc(col)] = float(v)
                filled = True
        if filled:
            out["ranks_filled"] += 1
        new_status = up.get("status")
        new_score = (up.get("sets_a"), up.get("sets_b"))
        if new_status and (str(row["status"]) != str(new_status)
                           or (new_score[0] is not None
                               and (row["sets_a"], row["sets_b"]) !=
                               (new_score[0], new_score[1]))):
            before = {"status": str(row["status"]), "sets_a": row["sets_a"],
                      "sets_b": row["sets_b"], "label_a_wins": row["label_a_wins"]}
            for col in ("status", "sets_a", "sets_b", "games_a", "games_b",
                        "set1_winner_a", "label_a_wins"):
                if col in up and up[col] is not None:
                    df.iat[i, df.columns.get_loc(col)] = up[col]
            out["results_corrected"] += 1
            out["corrections"].append({"match_id": mid, "before": before,
                                       "after": {k: up.get(k) for k in before}})
    if out["ranks_filled"] or out["results_corrected"]:
        df.to_parquet(path, index=False)
    return out
