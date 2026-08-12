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

from collections import Counter

from betbot.feeds.base import (PREMATCH_STATES, FeedMatch, evidence_of_play,
                               is_placeholder_time)

# Motivos de exclusión (códigos estables; se registran en el ledger)
EXCLUSION_REASONS = (
    "fuente_no_autoritativa",     # la fuente no confirma estado ni hora
    "sin_hora_de_inicio",         # sin scheduled_at_utc utilizable
    "hora_provisional",           # la hora es un placeholder (fin de día local)
    "estado_no_prepartido",       # live/completed/cancelled/postponed/walkover
    "estado_desconocido",         # la fuente no declara estado
    "estado_calendario_caducado", # la observación del estado es demasiado vieja
    "evidencia_de_resultado",     # los campos crudos delatan que ya se jugó
    "conflicto_de_fuentes",       # calendario y hora independiente discrepan
    "results_feed_stale_pre_match_unverified",  # sin resultados frescos ni fuente live
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
    # detalle legible por partido excluido (hora provisional, conflicto, etc.)
    details: dict[tuple, str] = field(default_factory=dict)

    def add(self, reason: str, m: FeedMatch, detail: str = "") -> None:
        self.excluded.setdefault(reason, []).append(m)
        if detail:
            self.details[m.pair_key] = detail

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
    # incluye los RECENT OUTCOMES no reconciliados: un outcome (ganador+sets)
    # es evidencia plena de que el partido ya se jugó — refuerza la
    # verificación already_completed sin esperar al marcador completo
    from betbot.canonical.outcomes import load_state_matches
    from betbot.config import resolve_path
    around = around or datetime.now(timezone.utc).date()
    try:
        m = load_state_matches(resolve_path(cfg, "canonical_dir"))
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

def _same_provider_family(a: str, b: str) -> bool:
    """True si dos nombres de fuente pertenecen al mismo proveedor de datos
    (p.ej. 'oddsapi' y 'odds_api_mirror:...': ambos The Odds API)."""
    def fam(s: str) -> str:
        s = (s or "").lower().replace("-", "_")
        return "oddsapi" if ("oddsapi" in s or "odds_api" in s) else s
    return bool(a and b) and fam(a) == fam(b)


def results_freshness(cfg: dict) -> dict:
    """Hasta qué día tenemos resultados por circuito."""
    try:
        from betbot.sync import local_freshness
        return local_freshness(cfg)
    except Exception:  # noqa: BLE001
        return {}


def tournament_coverage(cfg: dict) -> dict:
    """{tour: {evento_canónico: date}} — hasta cuándo hay cobertura reciente
    demostrada POR TORNEO (recent outcomes + fuentes parciales full). Es la
    misma cobertura que reporta capability_freshness; la puerta la usa para
    no bloquear por frescura GLOBAL un torneo cuyo estado sí está al día."""
    try:
        from betbot.sync import freshness_detail
        det = freshness_detail(cfg)
    except Exception:  # noqa: BLE001
        return {}
    return {t: {k: c["until"] for k, c in (d.get("active_coverage") or {}).items()}
            for t, d in det.items()}


def _coverage_until(coverage: dict | None, tour: str, tournament: str):
    if not coverage or not tournament:
        return None
    cov = coverage.get(tour) or {}
    if not cov:
        return None
    try:
        from betbot.tournaments import canonical_event
        key = canonical_event(tour, str(tournament))
    except Exception:  # noqa: BLE001
        return None                       # identidad de torneo no resoluble: sin rescate
    u = cov.get(key)
    if u is None:
        return None
    return u if isinstance(u, date) else date.fromisoformat(str(u))


# la cobertura de un torneo cuenta como FRESCA con esta antigüedad máxima —
# el MISMO umbral que usa capability_freshness para declarar FRESH (2 días)
COVERAGE_FRESH_MAX_AGE_DAYS = 2


def gate(matches: list[FeedMatch], cfg: dict, *, window_hours: int = 48,
         now: datetime | None = None, tours: list[str] | None = None,
         completed_idx: dict | None = None, registry: set | None = None,
         commence_idx=None, fresh_until: dict | None = None,
         coverage: dict | None = None) -> GateResult:
    """Clasifica los partidos del calendario en elegibles + excluidos con motivo.

    Orden deliberado: primero lo que descalifica de forma absoluta (no es main
    tour, fuente no autoritativa), después la evidencia de que YA se jugó, luego
    la calidad de la hora, y por último los contrastes con fuentes externas.
    """
    now = now or datetime.now(timezone.utc)
    fcfg = cfg.get("feeds", {})
    margin = timedelta(minutes=float(fcfg.get("prematch_margin_minutes", 5)))
    max_status_age = float(fcfg.get("calendar_status_max_age_hours", 6.0))
    tol = float(fcfg.get("commence_tolerance_minutes", 90.0))
    horizon = now + timedelta(hours=window_hours)
    res = GateResult()
    if completed_idx is None:
        completed_idx = completed_index(cfg, around=now.date())
    if fresh_until is None:
        fresh_until = results_freshness(cfg)
    if coverage is None:
        coverage = tournament_coverage(cfg)
    # SEGUNDA BARRERA contra horas placeholder (bug real: "Inicio confirmado
    # 03:59Z"). time_precision lo marca cada adaptador, pero su valor por
    # defecto es "exact": un adaptador que olvide marcarlo dejaría pasar el
    # placeholder. La puerta re-verifica SIEMPRE por su cuenta, con los
    # recuentos de horas compartidas calculados sobre el lote completo.
    time_counts = Counter(m.scheduled_at_utc for m in matches
                          if m.scheduled_at_utc is not None)

    for m in matches:
        if m.is_doubles or m.is_qualifying or m.level != "main" \
                or (tours and m.tour not in tours):
            res.add("no_main_tour", m)
            continue
        if not m.authoritative:
            res.add("fuente_no_autoritativa", m)
            continue

        # ---- evidencia de que ya se jugó: manda sobre cualquier código de estado ----
        played, why = evidence_of_play(m.raw)
        if played:
            res.add("evidencia_de_resultado", m,
                    f"los campos crudos delatan juego: {', '.join(why)}")
            continue

        if m.status == "unknown":
            res.add("estado_desconocido", m, m.time_note)
            continue
        if m.status not in PREMATCH_STATES:
            res.add("estado_no_prepartido", m, f"estado={m.status}")
            continue

        # ---- calidad de la hora ----
        if m.scheduled_at_utc is None:
            # una hora suelta sin fecha NO convierte un partido en futuro
            res.add("sin_hora_de_inicio", m, m.time_note)
            continue
        start = m.scheduled_at_utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        # re-verificación INDEPENDIENTE del marcado del adaptador
        ph, ph_why = is_placeholder_time(start, time_counts.get(m.scheduled_at_utc, 1))
        time_precision = m.time_precision if m.time_precision != "exact" else \
            ("date_only" if ph else "exact")
        time_source = m.source
        xcheck = None
        if time_precision != "exact":
            # única vía de rescate: una hora REAL de una fuente independiente,
            # todavía en el futuro. Sin ella, el partido queda excluido.
            rescued = False
            if commence_idx is not None:
                from betbot.feeds.commence import cross_check
                xcheck = cross_check(m.pair_key[1:], None, commence_idx, now,
                                     tolerance_minutes=tol, tournament_hint=m.tournament)
                if xcheck["verdict"] == "ya_comenzado":
                    res.add("ya_comenzado", m, xcheck["detail"])
                    continue
                if xcheck["verdict"] == "coincide":
                    start = datetime.fromisoformat(xcheck["commence_utc"])
                    time_precision = "independiente"
                    time_source = xcheck.get("source", "")
                    rescued = True
            if not rescued:
                res.add("hora_provisional", m,
                        (m.time_note or ph_why or f"precisión = {m.time_precision}")
                        + "; sin hora independiente que la sustituya")
                continue
        age = m.status_age_hours(now)
        if age is not None and age > max_status_age:
            res.add("estado_calendario_caducado", m, f"observado hace {age:.1f} h")
            continue

        # ---- contraste con la hora independiente (The Odds API / espejos) ----
        # AUSENCIA != CONTRADICCIÓN: que un espejo no liste el partido puede ser
        # snapshot antiguo, feed incompleto o ventana distinta. Solo bloquean la
        # contradicción real sobre la MISMA pareja (hora incompatible) y la
        # declaración explícita de que el evento ya no está por jugar.
        if commence_idx is not None and xcheck is None:
            from betbot.feeds.commence import cross_check, trust_of
            xcheck = cross_check(m.pair_key[1:], start, commence_idx, now,
                                 tolerance_minutes=tol, tournament_hint=m.tournament)
            if xcheck["verdict"] == "ya_comenzado":
                res.add("ya_comenzado", m, xcheck["detail"])
                continue
            if xcheck["verdict"] == "conflicto_horario":
                # conflicto REAL: se conserva la evidencia de AMBAS fuentes
                # (hora, snapshot y nivel de confianza) para poder resolverlo
                res.add("conflicto_de_fuentes", m,
                        (f"{xcheck['detail']} · primaria '{m.source}' "
                         f"[trust {trust_of(m.source)}, observada "
                         f"{m.source_updated_at or '?'}] vs secundaria "
                         f"'{xcheck.get('source', '?')}' [trust "
                         f"{xcheck.get('trust', '?')}, snapshot "
                         f"{xcheck.get('fetched_at') or '?'}]"))
                continue

        if start <= now + margin:
            res.add("ya_comenzado", m, f"inicio {start.isoformat()} ya pasado")
            continue
        if start > horizon:
            res.add("fuera_de_ventana", m)
            continue
        done = find_completed(m, completed_idx, registry)
        if done is not None:
            res.add("already_completed", m,
                    f"resultado local del {done['date']} ({done['status']})")
            continue

        # ---- una fuente sin evidencia de juego exige resultados frescos ----
        results_check = "full_frescos"
        if m.trust_tier != "live_verified":
            f = fresh_until.get(m.tour)
            # la corroboración debe ser INDEPENDIENTE: si el calendario ES de la
            # familia The Odds API, contrastarlo con el índice de commence-time
            # (espejos del mismo proveedor) sería circular y no cuenta
            same_family = _same_provider_family(m.source,
                                                (xcheck or {}).get("source", ""))
            corroborated = bool(xcheck and xcheck["verdict"] == "coincide"
                                and not same_family)
            # cobertura reciente DEL TORNEO (recent outcomes): si el estado de
            # este torneo está al día, la frescura GLOBAL stale no puede ser el
            # único motivo de bloqueo — el completed_index ya incluye esos
            # outcomes, así que "sigue por jugar" SÍ es verificable aquí
            cov_until = _coverage_until(coverage, m.tour, m.tournament)
            cov_fresh = (cov_until is not None
                         and (now.date() - cov_until).days <= COVERAGE_FRESH_MAX_AGE_DAYS)
            global_fresh = f is not None and f >= now.date()
            if not corroborated and not global_fresh and not cov_fresh:
                cov_note = (f"cobertura del torneo hasta {cov_until} (vieja)"
                            if cov_until else "el torneo no tiene cobertura reciente")
                res.add("results_feed_stale_pre_match_unverified", m,
                        (f"fuente '{m.source}' publica estado sin evidencia de juego, "
                         f"los resultados {m.tour} solo llegan hasta "
                         f"{f or 'ninguna fecha'} (< {now.date()}) y {cov_note}; sin "
                         f"hora independiente que lo corrobore no se puede afirmar "
                         f"prepartido"))
                continue
            if corroborated:
                results_check = "hora_independiente_corrobora"
            elif global_fresh:
                results_check = "full_frescos"
            else:
                results_check = f"cobertura_outcomes_torneo hasta {cov_until}"

        # cinturón final: NUNCA confirmar una hora que siga pareciendo placeholder
        final_ph, final_why = is_placeholder_time(start, 1)
        if final_ph and time_precision != "independiente":
            res.add("hora_provisional", m, final_why)
            continue
        res.eligible.append(m)
        res.confirmations[m.pair_key] = {
            "source": m.source, "event_id": m.event_id, "status": m.status,
            "start_utc": start.isoformat(), "start_local": m.start_local,
            "start_tz": m.start_tz, "source_updated_at": m.source_updated_at,
            "status_age_h": round(age, 2) if age is not None else None,
            "time_precision": time_precision, "time_source": time_source,
            "trust_tier": m.trust_tier, "results_check": results_check,
            # trazabilidad del contraste: qué aportó la primaria y qué la
            # secundaria (corroboración presente, ausente o no exigida)
            "schedule_check": ("primary_exact_future" if time_precision == "exact"
                               else f"primary_{time_precision}"),
            "corroboration": {"coincide": "corroborado",
                              "no_corroborado": "ausente_en_secundaria",
                              "sin_datos": "no_disponible"}.get(
                (xcheck or {}).get("verdict", "sin_datos"), "no_disponible"),
            "commence_check": (xcheck or {}).get("verdict", "sin_datos"),
            "commence_utc": (xcheck or {}).get("commence_utc"),
            "commence_event_id": (xcheck or {}).get("event_id"),
            "commence_source": (xcheck or {}).get("source"),
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
