"""Adaptadores de fuentes automáticas (calendario y cuotas), sustituibles.

Dos funciones separadas:
- CalendarSource.fetch_matches(window_hours) -> list[FeedMatch]
- OddsSource.fetch_odds(matches) -> list[OddsQuote]
Una misma fuente puede implementar ambas.

Reglas duras: solo lectura, sin credenciales de ejecución, sin evasión de
bloqueos, sin scraping contrario a términos. Nunca se inventan cuotas ni
mercados: si la fuente no los da, se informa la cobertura que falta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from betbot.schemas import OddsQuote


@dataclass
class FeedMatch:
    date: date
    tour: str                    # ATP | WTA
    tournament: str
    player1: str                 # nombre estilo Tennis-Data ("Alcaraz C.")
    player2: str
    level: str = "main"          # main | challenger | itf | other
    is_doubles: bool = False
    is_qualifying: bool = False
    round: str = ""
    best_of: int = 3
    surface: str | None = None   # None = desconocida (se estimará con aviso)
    indoor: bool = False
    start_local: str = ""        # hora textual de la fuente, informativa
    source: str = ""

    @property
    def key(self) -> tuple:
        from betbot.canonical.names import canonical_key
        a, b = sorted([canonical_key(self.player1), canonical_key(self.player2)])
        return (self.tour, str(self.date), a, b)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    n_items: int = 0
    error: str = ""
    fetched_at: str = ""
    data_timestamp: str = ""     # frescura declarada por la propia fuente
    notes: list[str] = field(default_factory=list)


class CalendarSource(Protocol):
    name: str

    def fetch_matches(self, window_hours: int) -> tuple[list[FeedMatch], SourceStatus]: ...


class OddsSource(Protocol):
    name: str
    markets: list[str]           # mercados que esta fuente puede ofrecer

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]: ...


def dedupe_matches(matches: list[FeedMatch]) -> list[FeedMatch]:
    """Deduplica por (tour, fecha, pareja de jugadores); prioridad = orden de fuentes."""
    seen: set[tuple] = set()
    out: list[FeedMatch] = []
    for m in matches:
        if m.key in seen:
            continue
        seen.add(m.key)
        out.append(m)
    return out


def strip_seed(name: str) -> str:
    """"Shelbayh A. (2)" -> "Shelbayh A." ; conserva iniciales con punto."""
    import re
    return re.sub(r"\s*\((?:[0-9]+|q|Q|WC|wc|LL|ll|PR|pr|SE|se|ALT|alt)\)\s*$", "", name.strip())


def classify_level(tournament: str) -> str:
    t = tournament.lower()
    if "challenger" in t or " chall" in t:
        return "challenger"
    if "itf" in t or "futures" in t or "25k" in t or "15k" in t:
        return "itf"
    if "125" in t or "utr" in t or "exhibition" in t or "united cup" in t \
            or "davis cup" in t or "billie jean" in t or "hopman" in t:
        return "other"
    return "main"


GRAND_SLAMS = ("australian open", "roland garros", "french open", "wimbledon", "us open")


def is_grand_slam(tournament: str) -> bool:
    t = tournament.lower()
    return any(g in t for g in GRAND_SLAMS)
