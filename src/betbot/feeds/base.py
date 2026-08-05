"""Adaptadores de fuentes automáticas (calendario y cuotas), sustituibles.

Dos funciones separadas:
- CalendarSource.fetch_matches(window_hours) -> list[FeedMatch]
- OddsSource.fetch_odds(matches) -> list[OddsQuote]
Una misma fuente puede implementar ambas.

REGLA DE SEGURIDAD (fallo Tsitsipas–Fonseca, 2026-08-05): una fuente solo es
AUTORITATIVA de calendario si publica, por partido, un identificador, la hora
de inicio y el ESTADO real. Una fuente que solo lista "los partidos del día"
sin estado ni fecha (github_te) NO puede crear un evento elegible: sus cuotas
se asocian a eventos ya confirmados y, si no encuentran ninguno, quedan como
`orphan_quote`. Sin ninguna fuente autoritativa operativa el escaneo termina
en FAIL_CLOSED con cero señales.

Reglas duras: solo lectura, sin credenciales de ejecución, sin evasión de
bloqueos, sin scraping contrario a términos. Nunca se inventan cuotas ni
mercados: si la fuente no los da, se informa la cobertura que falta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol

from betbot.schemas import OddsQuote

# Estados canónicos del partido. `unknown` es el valor por defecto: una fuente
# que no publica estado NUNCA puede declarar un partido como jugable.
MATCH_STATES = ("scheduled", "delayed", "postponed", "live", "completed",
                "cancelled", "walkover", "unknown")
PREMATCH_STATES = ("scheduled", "delayed")

# vocabularios reales de las fuentes -> estado canónico
_STATUS_ALIASES = {
    # ESPN: status.type.state / .name / .description
    "pre": "scheduled", "status_scheduled": "scheduled", "scheduled": "scheduled",
    "in": "live", "status_in_progress": "live", "in progress": "live", "live": "live",
    "post": "completed", "status_final": "completed", "final": "completed",
    "completed": "completed", "ended": "completed", "closed": "completed",
    "status_end_of_period": "live", "halftime": "live",
    "status_delayed": "delayed", "delayed": "delayed", "status_rain_delay": "delayed",
    "status_postponed": "postponed", "postponed": "postponed",
    "status_canceled": "cancelled", "status_cancelled": "cancelled",
    "canceled": "cancelled", "cancelled": "cancelled", "abandoned": "cancelled",
    "status_forfeit": "walkover", "walkover": "walkover", "w/o": "walkover",
    "status_walkover": "walkover", "status_uncontested": "walkover",
    "status_retired": "completed", "retired": "completed",
    "status_abandoned": "cancelled",
    # suspendido = ya empezó y se interrumpió: nunca es prepartido
    "status_suspended": "live", "suspended": "live",
    "not_started": "scheduled", "notstarted": "scheduled", "upcoming": "scheduled",
}


def normalize_status(raw: str | None) -> str:
    """Estado crudo de una fuente -> estado canónico. Lo desconocido es
    `unknown` (nunca se asume que un partido está por jugar)."""
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower()
    if not s:
        return "unknown"
    if s in MATCH_STATES:
        return s
    return _STATUS_ALIASES.get(s, "unknown")


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
    start_local: str = ""        # hora textual tal cual la publica la fuente
    source: str = ""
    # ---- identidad y estado del evento (obligatorios para ser elegible) ----
    event_id: str = ""           # identificador en la fuente
    scheduled_at_utc: datetime | None = None   # inicio en UTC (tz-aware)
    status: str = "unknown"      # uno de MATCH_STATES
    source_updated_at: str = ""  # cuándo observó la fuente este estado (ISO UTC)
    start_tz: str = ""           # zona horaria original declarada por la fuente
    authoritative: bool = False  # la fuente confirma estado + hora de inicio

    @property
    def key(self) -> tuple:
        from betbot.canonical.names import canonical_key
        a, b = sorted([canonical_key(self.player1), canonical_key(self.player2)])
        return (self.tour, str(self.date), a, b)

    @property
    def pair_key(self) -> tuple:
        """Identidad del enfrentamiento SIN fecha: enlaza cuotas y calendarios
        entre fuentes aunque difieran el orden de los jugadores o el día UTC."""
        from betbot.canonical.names import canonical_key
        a, b = sorted([canonical_key(self.player1), canonical_key(self.player2)])
        return (self.tour, a, b)

    @property
    def label(self) -> str:
        return f"{self.player1} vs {self.player2}"

    def status_age_hours(self, now: datetime | None = None) -> float | None:
        """Antigüedad de la observación del estado, en horas."""
        if not self.source_updated_at:
            return None
        try:
            ts = datetime.fromisoformat(str(self.source_updated_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - ts).total_seconds() / 3600.0)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    n_items: int = 0
    error: str = ""
    fetched_at: str = ""
    data_timestamp: str = ""     # frescura declarada por la propia fuente
    notes: list[str] = field(default_factory=list)
    n_orphan: int = 0            # cuotas sin evento de calendario confirmado
    authoritative: bool = False  # solo para fuentes de calendario


class CalendarSource(Protocol):
    name: str
    authoritative: bool          # True solo si publica estado + hora de inicio

    def fetch_matches(self, window_hours: int) -> tuple[list[FeedMatch], SourceStatus]: ...


class OddsSource(Protocol):
    name: str
    markets: list[str]           # mercados que esta fuente puede ofrecer

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]: ...


def dedupe_matches(matches: list[FeedMatch]) -> list[FeedMatch]:
    """Deduplica por enfrentamiento (sin fecha: dos fuentes pueden discrepar en
    el día UTC de un partido nocturno). Prioridad = orden de fuentes, pero una
    entrada AUTORITATIVA siempre gana a una no autoritativa del mismo par."""
    best: dict[tuple, FeedMatch] = {}
    order: list[tuple] = []
    for m in matches:
        k = m.pair_key
        if k not in best:
            best[k] = m
            order.append(k)
        elif m.authoritative and not best[k].authoritative:
            best[k] = m
    return [best[k] for k in order]


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
