"""Elo general y por superficie con replay cronológico estricto.

- K estilo FiveThirtyEight-tenis: K = k_base / (n_partidos_previos + offset)^shape.
- Los ratings PRE-partido se escriben en cada fila durante el replay, lo que
  garantiza por construcción la regla observed_at <= prediction_time.
- Retiradas cuentan como resultado (el partido se disputó); walkovers no
  actualizan ratings ni cuentan como partido.
- Ratings separados por tour (ATP/WTA nunca se mezclan) y por superficie.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class EloState:
    start: float = 1500.0
    k_base: float = 250.0
    k_offset: float = 5.0
    k_shape: float = 0.4
    rating: dict[str, float] = field(default_factory=dict)
    n: dict[str, int] = field(default_factory=dict)
    surf_rating: dict[tuple[str, str], float] = field(default_factory=dict)
    surf_n: dict[tuple[str, str], int] = field(default_factory=dict)

    def get(self, p: str) -> float:
        return self.rating.get(p, self.start)

    def get_surf(self, p: str, s: str) -> float:
        return self.surf_rating.get((p, s), self.start)

    def _k(self, n: int) -> float:
        return self.k_base / (n + self.k_offset) ** self.k_shape

    @staticmethod
    def expected(ra: float, rb: float) -> float:
        return 1.0 / (1.0 + 10.0 ** (-(ra - rb) / 400.0))

    def update(self, pa: str, pb: str, surface: str, a_wins: int) -> None:
        ra, rb = self.get(pa), self.get(pb)
        ea = self.expected(ra, rb)
        ka, kb = self._k(self.n.get(pa, 0)), self._k(self.n.get(pb, 0))
        self.rating[pa] = ra + ka * (a_wins - ea)
        self.rating[pb] = rb + kb * ((1 - a_wins) - (1 - ea))
        self.n[pa] = self.n.get(pa, 0) + 1
        self.n[pb] = self.n.get(pb, 0) + 1
        sa, sb = self.get_surf(pa, surface), self.get_surf(pb, surface)
        esa = self.expected(sa, sb)
        ksa = self._k(self.surf_n.get((pa, surface), 0))
        ksb = self._k(self.surf_n.get((pb, surface), 0))
        self.surf_rating[(pa, surface)] = sa + ksa * (a_wins - esa)
        self.surf_rating[(pb, surface)] = sb + ksb * ((1 - a_wins) - (1 - esa))
        self.surf_n[(pa, surface)] = self.surf_n.get((pa, surface), 0) + 1
        self.surf_n[(pb, surface)] = self.surf_n.get((pb, surface), 0) + 1


def replay(matches: pd.DataFrame, cfg_elo: dict) -> tuple[pd.DataFrame, dict[str, EloState]]:
    """Añade columnas elo_a, elo_b, elo_surf_a, elo_surf_b, n_prev_a, n_prev_b
    (todas PRE-partido) y devuelve el estado final por tour para el screener.

    `matches` debe venir ordenado por fecha ascendente (se reordena por seguridad).
    """
    df = matches.sort_values(["date", "match_id"]).reset_index(drop=True).copy()
    states: dict[str, EloState] = {}
    cols: dict[str, list] = {c: [] for c in
                             ("elo_a", "elo_b", "elo_surf_a", "elo_surf_b", "n_prev_a", "n_prev_b")}
    for r in df.itertuples(index=False):
        st = states.setdefault(r.tour, EloState(
            start=cfg_elo["start_rating"], k_base=cfg_elo["k_base"],
            k_offset=cfg_elo["k_offset"], k_shape=cfg_elo["k_shape"]))
        cols["elo_a"].append(st.get(r.player_a))
        cols["elo_b"].append(st.get(r.player_b))
        cols["elo_surf_a"].append(st.get_surf(r.player_a, r.surface))
        cols["elo_surf_b"].append(st.get_surf(r.player_b, r.surface))
        cols["n_prev_a"].append(st.n.get(r.player_a, 0))
        cols["n_prev_b"].append(st.n.get(r.player_b, 0))
        label = r.label_a_wins
        if r.status != "walkover" and label is not None and not (
                isinstance(label, float) and math.isnan(label)):
            st.update(r.player_a, r.player_b, r.surface, int(label))
    for c, v in cols.items():
        df[c] = v
    return df, states
