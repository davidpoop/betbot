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

import re
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


TIME_PRECISIONS = ("exact", "date_only", "unknown")


def is_placeholder_time(dt: datetime | None, shared_count: int = 1) -> tuple[bool, str]:
    """¿La hora parece un marcador de posición en vez de un inicio real?

    Dos señales, ambas observadas en el fallo real del 2026-08-05:
    1. Minuto :59 (y :58) — los horarios de tenis se publican en :00/:15/:30/:45.
       23:59 local es la forma canónica de "solo tengo la fecha"; en Toronto
       (UTC-4) eso llega como 03:59 UTC del día siguiente.
    2. Medianoche exacta UTC, típica de un `date` convertido a `datetime`.
    3. La misma marca compartida por varios partidos distintos (pistas
       distintas no pueden empezar todas al mismo segundo).
    """
    if dt is None:
        return True, "sin hora"
    if dt.minute in (58, 59):
        return True, (f"hora terminada en :{dt.minute:02d} ({dt.isoformat()}), patrón de "
                      f"fin de día local: la fuente no tiene horario asignado")
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return True, f"medianoche UTC exacta ({dt.isoformat()}), probable fecha sin hora"
    if shared_count >= 3:
        return True, (f"{shared_count} partidos comparten exactamente {dt.isoformat()}: "
                      f"horario provisional, no un inicio real")
    return False, ""


def mark_placeholder_times(matches: list["FeedMatch"]) -> list["FeedMatch"]:
    """Aplica la detección de horas provisionales a un lote completo (necesita
    ver el lote para detectar marcas compartidas por varios partidos)."""
    counts: dict = {}
    for m in matches:
        if m.scheduled_at_utc is not None:
            counts[m.scheduled_at_utc] = counts.get(m.scheduled_at_utc, 0) + 1
    for m in matches:
        shared = counts.get(m.scheduled_at_utc, 1)
        bad, why = is_placeholder_time(m.scheduled_at_utc, shared)
        if bad:
            m.time_precision = "unknown" if m.scheduled_at_utc is None else "date_only"
            # el adaptador puede haber dejado un motivo más específico
            m.time_note = m.time_note or why
    return matches


# Indicios de que un partido YA SE JUGÓ, buscados sobre los campos crudos con
# independencia de cómo los llame cada fuente.
# Nombres verificados sobre 25.952 objetos de partido reales de la WTA
# (ScoreSet1A.., ScoreTbSet1.., ScoreString, ResultString, Winner, MatchTimeTotal,
# PointA/PointB, Serve) más los de otras fuentes (periodScores, linescores).
_SCORE_KEY_RX = re.compile(r"(score|set\d|games?|winner|duration|result|elapsed|"
                           r"matchtimetotal|periodscores|linescores|point[ab]?$|serve)", re.I)
# En la WTA los campos sin jugar llegan como CADENA VACÍA (verificado sobre un
# payload real: los 17 campos de marcador son "" en los 5 partidos por jugar).
# `Winner == "0"` también significa "sin decidir".
_EMPTY = ("", "0", "0.0", "none", "null", "-", "n/a", "tbd", "00:00", "0:00",
          "00:00:00", "0-0", "[]", "{}", " ")


def evidence_of_play(raw: dict) -> tuple[bool, list[str]]:
    """(hay_evidencia, motivos) — un marcador o un ganador no vacío se impone a
    cualquier código de estado que diga "por jugar"."""
    reasons: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        else:
            key = path.rsplit(".", 1)[-1].split("[")[0]
            if not _SCORE_KEY_RX.search(key):
                return
            s = str(node).strip().lower()
            if s in _EMPTY or node is None or node is False:
                return
            if isinstance(node, bool) and node is True:
                reasons.append(f"{path}={node}")
                return
            if isinstance(node, (int, float)) and float(node) > 0:
                reasons.append(f"{path}={node}")
                return
            if isinstance(node, str) and s not in _EMPTY:
                reasons.append(f"{path}='{node}'")

    walk(raw or {})
    return bool(reasons), reasons[:8]


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
    event_id: str = ""           # identificador ESTABLE (sin fecha: ver nota abajo)
    scheduled_at_utc: datetime | None = None   # inicio en UTC (tz-aware)
    status: str = "unknown"      # uno de MATCH_STATES
    source_updated_at: str = ""  # cuándo observó la fuente este estado (ISO UTC)
    start_tz: str = ""           # zona horaria original declarada por la fuente
    authoritative: bool = False  # la fuente confirma estado + hora de inicio
    # Precisión de la hora: solo `exact` puede considerarse "Inicio confirmado".
    # `date_only` = la fuente da el día pero rellena la hora con un placeholder
    # (fin de día local: 23:59 -> 03:59 UTC en Toronto). `unknown` = sin hora.
    time_precision: str = "exact"
    # Confianza del estado publicado por la fuente:
    #   live_verified  -> publica evidencia de juego (marcador, completed)
    #   schedule_only  -> publica un código de estado sin evidencia verificable
    trust_tier: str = "live_verified"
    # Motivo legible si la hora quedó marcada como no exacta
    time_note: str = ""
    # Campos crudos del partido, para auditar evidencia de resultado
    raw: dict = field(default_factory=dict)

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
