"""Puerta de validación PREPARTIDO: qué partidos pueden analizarse.

Motivo (fallo real 2026-08-05, Tsitsipas–Fonseca): el escáner aceptaba como
"pendiente" cualquier fila del feed github_te, que no publica ni fecha ni
estado por partido — el código le asignaba la fecha UTC de hoy. Un partido ya
terminado podía así generar una señal.

Regla, deliberadamente conservadora (fail closed): un partido solo se analiza
si una fuente AUTORITATIVA de calendario confirma identificador, hora de inicio
y estado prepartido, y si el almacén local de resultados no lo contradice.
Cualquier duda excluye el partido con un código de motivo explícito.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from betbot.feeds.base import PREMATCH_STATES, FeedMatch

# Motivos de exclusión (códigos estables; se registran en el ledger)
EXCLUSION_REASONS = (
    "fuente_no_autoritativa",     # la fuente no confirma estado ni hora
    "sin_hora_de_inicio",         # sin scheduled_at_utc utilizable
    "estado_no_prepartido",       # live/completed/cancelled/postponed/walkover
    "estado_desconocido",         # la fuente no declara estado
    "estado_calendario_caducado", # la observación del estado es demasiado vieja
    "ya_comenzado",               # la hora de inicio ya pasó (con margen)
    "fuera_de_ventana",           # empieza más tarde que la ventana pedida
    "already_completed",          # el almacén local ya tiene su resultado
    "no_main_tour",               # challenger/itf/dobles/qualy/equipos
)


@dataclass
class GateResult:
    eligible: list[FeedMatch] = field(default_factory=list)
    excluded: dict[str, list[FeedMatch]] = field(default_factory=dict)
    # trazabilidad por partido elegible: qué fuente lo confirmó y cuándo
    confirmations: dict[tuple, dict] = field(default_factory=dict)

    def add(self, reason: str, m: FeedMatch) -> None:
        self.excluded.setdefault(reason, []).append(m)

    def counts(self) -> dict[str, int]:
        return {r: len(v) for r, v in sorted(self.excluded.items()) if v}


# ---------------------------------------------------------------------------
# índice de resultados ya conocidos (contradice al calendario)
# ---------------------------------------------------------------------------

def completed_index(cfg: dict, around: date | None = None,
                    days_back: int = 7, days_fwd: int = 2) -> dict[tuple, list[dict]]:
    """{(tour, jugador_a, jugador_b): [{date, status}]} desde el almacén local
    de resultados, en una ventana alrededor de hoy. Claves canónicas exactas
    (nada de coincidencia difusa)."""
    from betbot.canonical.store import load_matches
    from betbot.config import resolve_path
    around = around or datetime.now(timezone.utc).date()
    try:
        m = load_matches(resolve_path(cfg, "canonical_dir"))
    except FileNotFoundError:
        return {}
    lo, hi = around - timedelta(days=days_back), around + timedelta(days=days_fwd)
    win = m[(m["date"] >= lo) & (m["date"] <= hi)]
    idx: dict[tuple, list[dict]] = {}
    for r in win.itertuples(index=False):
        key = (str(r.tour), str(r.player_a), str(r.player_b))
        idx.setdefault(key, []).append({"date": r.date, "status": str(r.status),
                                        "tournament": str(r.tournament)})
    return idx


def _canonical_pair(m: FeedMatch, registry: set | None) -> tuple:
    """Par canónico del partido resolviendo iniciales contra el registro
    (extensión determinista única; nunca aproximación difusa)."""
    from betbot.canonical.names import canonical_key, extend_initials
    a, b = canonical_key(m.player1), canonical_key(m.player2)
    if registry:
        a, b = extend_initials(a, registry), extend_initials(b, registry)
    a, b = sorted([a, b])
    return (m.tour, a, b)


def find_completed(m: FeedMatch, idx: dict[tuple, list[dict]],
                   registry: set | None = None, day_tol: int = 2) -> dict | None:
    """Resultado ya almacenado compatible con este partido, si existe."""
    hits = idx.get(_canonical_pair(m, registry))
    if not hits:
        return None
    ref = (m.scheduled_at_utc.date() if m.scheduled_at_utc else m.date)
    for h in hits:
        if h["status"] in ("completed", "retired", "walkover") \
                and abs((h["date"] - ref).days) <= day_tol:
            return h
    return None


# ---------------------------------------------------------------------------
# puerta principal
# ---------------------------------------------------------------------------

def gate(matches: list[FeedMatch], cfg: dict, *, window_hours: int = 48,
         now: datetime | None = None, tours: list[str] | None = None,
         completed_idx: dict | None = None, registry: set | None = None) -> GateResult:
    """Clasifica los partidos del calendario en elegibles + excluidos con motivo."""
    now = now or datetime.now(timezone.utc)
    fcfg = cfg.get("feeds", {})
    margin = timedelta(minutes=float(fcfg.get("prematch_margin_minutes", 5)))
    max_status_age = float(fcfg.get("calendar_status_max_age_hours", 6.0))
    horizon = now + timedelta(hours=window_hours)
    res = GateResult()
    if completed_idx is None:
        completed_idx = completed_index(cfg, around=now.date())

    for m in matches:
        if m.is_doubles or m.is_qualifying or m.level != "main" \
                or (tours and m.tour not in tours):
            res.add("no_main_tour", m)
            continue
        if not m.authoritative:
            res.add("fuente_no_autoritativa", m)
            continue
        if m.status == "unknown":
            res.add("estado_desconocido", m)
            continue
        if m.status not in PREMATCH_STATES:
            res.add("estado_no_prepartido", m)
            continue
        if m.scheduled_at_utc is None:
            # una hora suelta sin fecha NO convierte un partido en futuro
            res.add("sin_hora_de_inicio", m)
            continue
        start = m.scheduled_at_utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        age = m.status_age_hours(now)
        if age is not None and age > max_status_age:
            res.add("estado_calendario_caducado", m)
            continue
        if start <= now + margin:
            res.add("ya_comenzado", m)
            continue
        if start > horizon:
            res.add("fuera_de_ventana", m)
            continue
        done = find_completed(m, completed_idx, registry)
        if done is not None:
            res.add("already_completed", m)
            continue
        res.eligible.append(m)
        res.confirmations[m.pair_key] = {
            "source": m.source, "event_id": m.event_id, "status": m.status,
            "start_utc": start.isoformat(), "start_local": m.start_local,
            "start_tz": m.start_tz, "source_updated_at": m.source_updated_at,
            "status_age_h": round(age, 2) if age is not None else None,
        }
    return res


def signal_still_prematch(conf: dict, now: datetime | None = None,
                          margin_minutes: float = 5.0) -> bool:
    """¿Sigue siendo prepartido una señal ya emitida? (usado por `watch`)."""
    now = now or datetime.now(timezone.utc)
    if conf.get("status") not in PREMATCH_STATES:
        return False
    raw = conf.get("start_utc")
    if not raw:
        return False
    try:
        start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start > now + timedelta(minutes=margin_minutes)


FAIL_CLOSED_MSG = "FAIL_CLOSED: calendario prepartido no verificable; 0 señales generadas"
