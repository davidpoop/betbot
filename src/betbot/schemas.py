"""Esquemas Pydantic del dataset canónico y de las importaciones diarias.

Convenciones:
- Orientación neutral: player_a es el jugador cuyo nombre canónico es menor en
  orden lexicográfico. `label_a_wins` = 1 si ganó player_a. Así el target nunca
  se codifica en la posición (anti-leakage).
- Todas las fechas son date (partido) o datetime UTC (cuotas).
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Tour(str, Enum):
    ATP = "ATP"
    WTA = "WTA"


class MatchStatus(str, Enum):
    COMPLETED = "completed"
    RETIRED = "retired"
    WALKOVER = "walkover"
    OTHER = "other"


class CanonicalMatch(BaseModel):
    match_id: str
    tour: Tour
    date: date
    tournament: str
    series: str = ""
    surface: str = "Hard"
    indoor: bool = False
    round: str = ""
    best_of: int = 3
    player_a: str            # id canónico (nombre normalizado)
    player_b: str
    label_a_wins: Optional[int] = None   # None si walkover/otro
    status: MatchStatus = MatchStatus.COMPLETED
    score_raw: str = ""
    sets_a: Optional[int] = None
    sets_b: Optional[int] = None
    games_a: Optional[int] = None
    games_b: Optional[int] = None
    set1_winner_a: Optional[int] = None  # 1/0; None si set 1 no completado
    rank_a: Optional[int] = None
    rank_b: Optional[int] = None
    pts_a: Optional[float] = None
    pts_b: Optional[float] = None
    # cuotas prepartido (decimal) por casa, orientadas a player_a / player_b
    odds: dict[str, tuple[Optional[float], Optional[float]]] = Field(default_factory=dict)
    source: str = ""

    @field_validator("best_of")
    @classmethod
    def _bo(cls, v: int) -> int:
        if v not in (3, 5):
            raise ValueError(f"best_of invalido: {v}")
        return v


class DayMatch(BaseModel):
    """Fila de la plantilla de partidos del día (importación manual)."""
    date: date
    tour: Tour
    tournament: str
    surface: str = "Hard"
    indoor: bool = False
    round: str = ""
    best_of: int = 3
    player_a: str
    player_b: str


class OddsQuote(BaseModel):
    """Fila de la plantilla de cuotas del día (importación manual)."""
    player_a: str
    player_b: str
    market: str                       # match_winner | set1_winner | wins_set | straight_sets | three_sets
    selection: str                    # "a" | "b" | "yes" | "no"
    odds: float
    bookmaker: str = "manual"
    timestamp: Optional[datetime] = None

    @field_validator("odds")
    @classmethod
    def _odds(cls, v: float) -> float:
        if not (1.0 < v < 1001.0):
            raise ValueError(f"cuota decimal invalida: {v}")
        return v

    @field_validator("market")
    @classmethod
    def _market(cls, v: str) -> str:
        allowed = {"match_winner", "set1_winner", "wins_set", "straight_sets", "three_sets"}
        if v not in allowed:
            raise ValueError(f"mercado no soportado: {v} (validos: {sorted(allowed)})")
        return v

    @field_validator("selection")
    @classmethod
    def _sel(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"a", "b", "yes", "no"}:
            raise ValueError(f"selection invalida: {v} (a|b|yes|no)")
        return v
