"""Dataset del Model Lab: filas PREMATCH point-in-time safe y reproducibles.

Cada fila representa un partido y TODAS sus features pudieron conocerse antes
de ese partido. La garantía es doble:

1. Por construcción: el Elo y el estado de actividad se generan con el replay
   cronológico del champion (`betbot.ratings.elo.replay` escribe los ratings
   PRE-partido; `betbot.features.builder.build_features` lee el estado ANTES de
   registrar el partido). Se REUTILIZA el código del champion a propósito: el
   laboratorio debe medir exactamente lo que produce producción, no una copia.
2. Por verificación: la PROPIEDAD DE PREFIJO (`prefix_property_ok`) — construir
   el dataset con la historia truncada en una fecha T produce filas idénticas
   para todo partido <= T. Si cualquier feature usara datos futuros (resultado
   propio, ranking posterior, agregados con futuro), la comparación falla. Los
   tests automáticos de leakage la ejercitan con cortes múltiples.

Columnas prohibidas en X: el resultado y todo lo derivado de él viven en
META/TARGETS y `forbidden_in_X` las veta explícitamente.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from betbot.config import resolve_path
from betbot.features.builder import FEATURES, MARKET_FEATURE, build_features
from betbot.ratings.elo import replay

# resultado del partido y derivados: JAMÁS pueden entrar en una matriz de diseño
FORBIDDEN_IN_X = [
    "label_a_wins", "sets_a", "sets_b", "games_a", "games_b", "set1_winner_a",
    "status", "score", "y_set1_a", "y_wins_set_a", "y_straight_a", "y_three_sets",
]

TARGET = "label_a_wins"
META_KEEP = ["match_id", "tour", "date", "year", "player_a", "player_b", "best_of",
             "status", "has_market", "p_novig_a_prop", "novig_book", "odds_json",
             TARGET]


@dataclass
class LabDataset:
    """Tabla de laboratorio + contrato de columnas + huella reproducible."""
    df: pd.DataFrame
    feature_cols: list[str]
    target: str = TARGET
    data_hash: str = ""
    cfg_splits: dict = field(default_factory=dict)

    def X(self, cols: list[str] | None = None) -> pd.DataFrame:
        cols = list(cols or self.feature_cols)
        bad = [c for c in cols if c in FORBIDDEN_IN_X]
        if bad:
            raise ValueError(f"Columnas de resultado en la matriz de diseño: {bad}")
        return self.df[cols]

    def labelled(self) -> pd.DataFrame:
        """Filas con target válido (completados; retiradas excluidas del target
        como en el champion — sí cuentan en Elo/actividad)."""
        m = (self.df["status"] == "completed") & self.df[TARGET].notna()
        return self.df[m]


def _hash_frame(df: pd.DataFrame, extra: dict) -> str:
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(df[["match_id", "date"]], index=False).values.tobytes())
    h.update(json.dumps(extra, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def build_dataset(cfg: dict, matches: pd.DataFrame | None = None,
                  players: pd.DataFrame | None = None,
                  cutoff: date | None = None) -> LabDataset:
    """Construye el dataset del laboratorio (paridad champion, features v1).

    `cutoff` trunca la HISTORIA DE ENTRADA (no las filas de salida): sirve para
    la verificación de la propiedad de prefijo. En uso normal se omite.
    """
    canon = resolve_path(cfg, "canonical_dir")
    if matches is None:
        matches = pd.read_parquet(canon / "matches.parquet")
    if players is None:
        players = pd.read_parquet(canon / "players.parquet")
    if cutoff is not None:
        matches = matches[matches["date"] <= cutoff]
    elo_df, _states = replay(matches, cfg["elo"])
    feats, _activity = build_features(elo_df, players, cfg)
    feats["year"] = pd.to_datetime(feats["date"]).dt.year
    cols = META_KEEP + [c for c in FEATURES + [MARKET_FEATURE] if c in feats.columns]
    df = feats[[c for c in cols if c in feats.columns]].copy()
    splits = dict(cfg.get("splits", {}))
    return LabDataset(df=df, feature_cols=FEATURES + [MARKET_FEATURE],
                      data_hash=_hash_frame(df, {"splits": splits, "elo": cfg.get("elo", {})}),
                      cfg_splits=splits)


def prefix_property_ok(cfg: dict, matches: pd.DataFrame, players: pd.DataFrame,
                       cutoff: date, atol: float = 1e-12) -> tuple[bool, str]:
    """Test de leakage temporal: el dataset construido con TODA la historia y el
    construido con la historia truncada en `cutoff` deben coincidir exactamente
    en todas las filas con fecha <= cutoff. Cualquier feature contaminada por
    el futuro rompe la igualdad."""
    full = build_dataset(cfg, matches, players)
    trunc = build_dataset(cfg, matches, players, cutoff=cutoff)
    f = full.df[full.df["date"] <= cutoff].reset_index(drop=True)
    t = trunc.df.reset_index(drop=True)
    if len(f) != len(t):
        return False, f"nº de filas distinto hasta {cutoff}: {len(f)} vs {len(t)}"
    if list(f["match_id"]) != list(t["match_id"]):
        return False, "orden/identidad de partidos distinto entre prefijo y truncado"
    for c in full.feature_cols:
        a = pd.to_numeric(f[c], errors="coerce")
        b = pd.to_numeric(t[c], errors="coerce")
        if not ((a - b).abs().fillna(0) <= atol).all():
            i = int((a - b).abs().fillna(0).idxmax())
            return False, (f"feature '{c}' difiere con historia truncada "
                           f"(match {f.loc[i, 'match_id']}): {a[i]} vs {b[i]}")
    return True, "ok"
