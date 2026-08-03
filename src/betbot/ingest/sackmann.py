"""Enriquecimiento biográfico desde el mirror de Jeff Sackmann (CC BY-NC-SA 4.0).

Solo se usa atp_players.csv (mano, fecha de nacimiento, altura). El enlace con
las claves canónicas de Tennis-Data se hace por (apellido, primera inicial);
los casos ambiguos van a cuarentena (sin enriquecer) y se listan en un informe.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from betbot.canonical.names import canonical_key, split_last_initials


def load_players(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"dob": "Int64"}, low_memory=False)
    df = df[df["name_last"].notna()].copy()

    def mkdob(v) -> date | None:
        try:
            v = int(v)
            return date(v // 10000, (v // 100) % 100, v % 100)
        except (TypeError, ValueError):
            return None

    df["dob_date"] = df["dob"].map(mkdob)
    df["last_key"] = df["name_last"].map(canonical_key)
    df["first_initial"] = df["name_first"].fillna("").map(lambda s: canonical_key(s)[:1])
    return df


def build_bio_lookup(players: pd.DataFrame, active_since: int = 1995) -> tuple[dict, list[str]]:
    """Devuelve (lookup, cuarentena). lookup: clave_canonica_tennisdata -> bio.

    Filtra jugadores nacidos antes de `active_since - 15` para reducir
    colisiones con jugadores históricos.
    """
    cutoff = date(active_since - 45, 1, 1)  # nacidos despues de ~1950
    recent = players[players["dob_date"].notna() & (players["dob_date"] > cutoff)]
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for _, r in recent.iterrows():
        groups[(r["last_key"], r["first_initial"])].append(r)
    lookup: dict[str, dict] = {}
    quarantine: list[str] = []
    for (last, ini), rows in groups.items():
        if not last or not ini:
            continue
        key = f"{last}_{ini}"
        if len(rows) == 1:
            r = rows[0]
            lookup[key] = {"hand": r.get("hand"), "dob": r["dob_date"], "height": r.get("height")}
        else:
            # ambiguo: si todos comparten mano y años de nacimiento cercanos, usar el mas reciente
            dobs = sorted(r["dob_date"] for r in rows)
            if (dobs[-1].year - dobs[0].year) <= 3:
                r = max(rows, key=lambda x: x["dob_date"])
                lookup[key] = {"hand": r.get("hand"), "dob": r["dob_date"], "height": r.get("height")}
            else:
                quarantine.append(key)
    return lookup, sorted(quarantine)


def bio_for(key: str, lookup: dict) -> dict | None:
    """Busca bio para clave canónica Tennis-Data (apellido..._iniciales)."""
    if key in lookup:
        return lookup[key]
    last, ini = split_last_initials(key)
    if ini:
        return lookup.get(f"{last}_{ini[0]}")
    return None
