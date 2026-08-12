"""Corroboración independiente de la HORA DE INICIO (commence_time).

Motivo (fallo real 2026-08-05): `api.wtatennis.com` publica `MatchTimeStamp`
con una hora PLACEHOLDER de fin de día local cuando el horario aún no está
asignado (23:59 de Toronto = 03:59 UTC del día siguiente). Tomada como hora de
inicio real, esa marca sitúa el partido 6-10 h MÁS TARDE de lo que empieza, de
modo que un encuentro ya comenzado seguiría pareciendo prepartido.

Este módulo construye un índice independiente de horas de inicio a partir de
The Odds API, por tres vías, en orden:
1. La API oficial, si hay `ODDS_API_KEY` / `BETBOT_ODDS_API_KEY`.
2. Espejos públicos en GitHub que republican su salida (sin credencial):
   `alienorsutinn/tennis-odds-mvp` y `gmalbert/tennis-predictions`. Conservan
   `event_id` nativo y `commence_time` en ISO UTC.
Se conservan SIEMPRE el identificador de evento y el `commence_time` originales.

El índice no sirve para confirmar por sí solo (un espejo puede arrastrar
partidos ya empezados), sino para CONTRASTAR: discrepancia material de hora,
hora ya pasada, o desaparición de un partido que el calendario sigue dando por
programado.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from betbot.feeds import cache

MIRRORS = (
    {"name": "odds_api_mirror:alienorsutinn",
     "url": "https://raw.githubusercontent.com/alienorsutinn/tennis-odds-mvp/main/data/odds_latest.json",
     "kind": "events"},
    {"name": "odds_api_mirror:gmalbert",
     "url": "https://raw.githubusercontent.com/gmalbert/tennis-predictions/main/data_files/odds_api_today.json",
     "kind": "matches"},
)


@dataclass
class CommenceRecord:
    pair: tuple                  # (jugador_a, jugador_b) canónicos, ordenados
    commence_utc: datetime
    event_id: str
    source: str
    sport_key: str = ""
    fetched_at: str = ""
    players_raw: tuple = ()
    # estado DECLARADO por la fuente secundaria, si lo publica ("completed",
    # "live"...). Solo un estado explícito permite afirmar "ya no está por
    # jugar"; la simple ausencia del partido en la lista nunca lo permite.
    status: str = ""


@dataclass
class CommenceIndex:
    by_pair: dict = field(default_factory=dict)
    sources_ok: list = field(default_factory=list)
    sources_failed: list = field(default_factory=list)
    covered_tours: set = field(default_factory=set)

    @property
    def available(self) -> bool:
        return bool(self.sources_ok)

    def get(self, pair: tuple) -> list[CommenceRecord]:
        return self.by_pair.get(pair, [])

    def covers(self, tournament_hint: str) -> bool:
        """¿El índice contiene algún evento de este torneo? Sin cobertura no se
        puede concluir nada de una ausencia."""
        hint = (tournament_hint or "").lower().replace(" ", "_")
        if not hint:
            return False
        return any(hint.split("_")[0] in k for k in self.covered_tours)


def _canon_pair(p1: str, p2: str, registry: set | None) -> tuple:
    from betbot.canonical.names import canonical_key, extend_initials, resolve_full_name
    keys = []
    for name in (p1, p2):
        k = resolve_full_name(name, registry or set())
        if registry and k not in registry:
            k = extend_initials(canonical_key(name), registry)
        keys.append(k)
    return tuple(sorted(keys))


def _parse_iso(raw) -> datetime | None:
    s = str(raw or "").strip().replace(" ", "T").replace("Z", "+00:00")
    if not s:
        return None
    if len(s) == 16:
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_index(cfg: dict, registry: set | None = None,
                ttl_seconds: int | None = None) -> CommenceIndex:
    idx = CommenceIndex()
    fcfg = cfg.get("feeds", {}) or {}
    ttl = int(ttl_seconds if ttl_seconds is not None else fcfg.get("ttl_seconds", 900))

    # 1) The Odds API oficial, solo si hay clave en el entorno
    key = (os.environ.get("BETBOT_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
           or fcfg.get("odds_api_key"))
    if key:
        try:
            from betbot.feeds.oddsapi import OddsApiFeed
            feed = OddsApiFeed(api_key=key, ttl_seconds=ttl)
            events = getattr(feed, "list_events", None)
            if callable(events):
                for ev in events() or []:
                    rec = _record_from(ev.get("home_team"), ev.get("away_team"),
                                       ev.get("commence_time"), ev.get("id", ""),
                                       "odds_api", ev.get("sport_key", ""), "", registry)
                    if rec:
                        _add(idx, rec)
                idx.sources_ok.append("odds_api")
        except Exception as exc:  # noqa: BLE001
            idx.sources_failed.append(f"odds_api: {exc}")

    # 2) espejos públicos (sin credencial)
    for m in MIRRORS:
        try:
            data = cache.get_json(m["url"], ttl_seconds=ttl)
        except Exception as exc:  # noqa: BLE001
            idx.sources_failed.append(f"{m['name']}: {exc}")
            continue
        n = 0
        fetched = str(data.get("fetchedAt") or data.get("fetched_at") or "")
        rows = data.get("events") if m["kind"] == "events" else data.get("matches")
        for ev in rows or []:
            rec = _record_from(ev.get("player1"), ev.get("player2"),
                               ev.get("start") or ev.get("commence_time"),
                               str(ev.get("event_id", "")), m["name"],
                               str(ev.get("sport_key", "")), fetched, registry,
                               status=_declared_status(ev))
            if rec:
                _add(idx, rec)
                n += 1
        idx.sources_ok.append(f"{m['name']} ({n} eventos, generado {fetched[:16] or '?'})")
    return idx


def _record_from(p1, p2, commence, event_id, source, sport_key, fetched, registry,
                 status=""):
    if not p1 or not p2:
        return None
    dt = _parse_iso(commence)
    if dt is None:
        return None
    return CommenceRecord(pair=_canon_pair(str(p1), str(p2), registry), commence_utc=dt,
                          event_id=str(event_id or ""), source=source,
                          sport_key=str(sport_key or ""), fetched_at=str(fetched or ""),
                          players_raw=(str(p1), str(p2)), status=str(status or ""))


def _declared_status(ev: dict) -> str:
    """Estado EXPLÍCITO publicado por la fuente secundaria, si lo hay.
    `completed: true` o un campo status/state con un valor conocido; cualquier
    otra cosa (incluida su ausencia) devuelve "" y no afirma nada."""
    if ev.get("completed") is True:
        return "completed"
    s = str(ev.get("status") or ev.get("state") or "").strip().lower()
    return s if s in ("completed", "live", "in_play", "finished") else ""


def _add(idx: CommenceIndex, rec: CommenceRecord) -> None:
    idx.by_pair.setdefault(rec.pair, []).append(rec)
    if rec.sport_key:
        idx.covered_tours.add(rec.sport_key.lower())


# ---------------------------------------------------------------------------
# contraste
# ---------------------------------------------------------------------------

VERDICTS = ("sin_datos", "coincide", "conflicto_horario", "ya_comenzado",
            "no_corroborado", "secondary_stale_schedule")

# Jerarquía de confianza: un espejo secundario JAMÁS anula silenciosamente a
# una fuente superior. Solo se usa para reportar la evidencia de ambos lados
# en un conflicto real; no cambia por sí sola ninguna exclusión.
TRUST_RANK = {
    "wta_official": 0,          # primera parte, estado + hora reales
    "sportradar": 1,            # proveedor oficial con enum cerrado
    "oddsapi": 2,               # proveedor directo (The Odds API /events)
    "thesportsdb": 3,
    "espn": 3,
    "commence": 4,              # índices/espejos de commence-time
    "github_te": 5,             # comunitaria
}


def trust_of(source: str) -> int:
    s = str(source or "").lower()
    for k, v in TRUST_RANK.items():
        if k in s:
            return v
    return 4                                     # desconocida: nivel espejo


# Un snapshot secundario viejo no informa por AUSENCIA: pasado este umbral la
# falta del partido en el índice se degrada a `sin_datos`.
ABSENCE_MAX_SNAPSHOT_AGE_H = 6.0

# Una HORA PROGRAMADA sí caduca. En tenis el orden de juego se mueve por
# lluvia, partido anterior largo, cambio de pista o reprogramación, así que un
# `scheduled_start` observado hace horas puede estar obsoleto: por sí solo NO
# puede producir `ya_comenzado` ni un conflicto. Distinto de la EVIDENCIA DE
# JUEGO EXPLÍCITA (status live/in_play/completed/finished o marcador real),
# que no caduca y sigue bloqueando por vieja que sea la observación.
SCHEDULE_MAX_SNAPSHOT_AGE_H = 6.0


def _age_h(fetched_at: str, now: datetime) -> float | None:
    """Antigüedad (horas) de una marca ISO, o None si no se puede determinar
    (sin marca no se puede PROBAR que esté caducada: se trata como reciente)."""
    if not fetched_at:
        return None
    try:
        dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _snapshot_age_h(idx: CommenceIndex, now: datetime) -> float | None:
    """Antigüedad del snapshot MÁS RECIENTE del índice (horas), o None."""
    ages = [a for recs in idx.by_pair.values() for r in recs
            if (a := _age_h(r.fetched_at, now)) is not None]
    return min(ages) if ages else None


def cross_check(pair: tuple, declared_utc: datetime | None, idx: CommenceIndex,
                now: datetime, tolerance_minutes: float = 90.0,
                tournament_hint: str = "",
                absence_max_age_h: float = ABSENCE_MAX_SNAPSHOT_AGE_H,
                schedule_max_age_h: float = SCHEDULE_MAX_SNAPSHOT_AGE_H) -> dict:
    """Contrasta la hora declarada por el calendario con la independiente.

    Distingue CONTRADICCIÓN de AUSENCIA (la ausencia no es contradicción) y
    EVIDENCIA DE JUEGO de HORA PROGRAMADA (la hora programada caduca):

    - `ya_comenzado`: la fuente declara explícitamente el evento en juego o
      terminado (evidencia de juego: NO caduca), o su hora RECIENTE ya pasó.
    - `secondary_stale_schedule`: la única evidencia de la secundaria es una
      hora programada observada hace demasiado (pudo reprogramarse por lluvia
      u orden de juego) -> no bloquea ni contradice; manda la primaria.
    - `conflicto_horario`: la MISMA pareja con hora incompatible
      (> tolerancia) y snapshot reciente. Contradicción real -> exclusión.
    - `no_corroborado`: el índice cubre el torneo pero NO lista este partido.
      Puede ser snapshot antiguo, feed incompleto, ventana distinta o
      cobertura parcial: NO se concluye nada en contra del partido.
    - `sin_datos`: no hay índice, no cubre el torneo, o el snapshot es
      demasiado viejo como para que su ausencia signifique algo.
    """
    if not idx.available:
        return {"verdict": "sin_datos", "detail": "sin índice independiente de horas"}
    recs = idx.get(pair)
    if not recs:
        age_h = _snapshot_age_h(idx, now)
        if tournament_hint and idx.covers(tournament_hint):
            if age_h is not None and age_h > absence_max_age_h:
                return {"verdict": "sin_datos", "snapshot_age_h": round(age_h, 1),
                        "detail": (f"el índice cubre el torneo pero su snapshot tiene "
                                   f"{age_h:.1f} h (> {absence_max_age_h} h): su "
                                   f"ausencia no informa")}
            return {"verdict": "no_corroborado",
                    **({"snapshot_age_h": round(age_h, 1)} if age_h is not None else {}),
                    "detail": ("el índice cubre este torneo pero no lista el partido; "
                               "ausencia NO es contradicción (snapshot incompleto, "
                               "ventana distinta o cobertura parcial)")}
        return {"verdict": "sin_datos", "detail": "el índice no cubre este torneo"}
    best = min(recs, key=lambda r: r.commence_utc)
    info = {"commence_utc": best.commence_utc.isoformat(), "event_id": best.event_id,
            "source": best.source, "sport_key": best.sport_key,
            "fetched_at": best.fetched_at, "trust": trust_of(best.source)}

    # (A) EVIDENCIA DE JUEGO EXPLÍCITA: la fuente declara el evento en juego o
    # terminado. NO caduca: bloquea por vieja que sea la observación.
    explicit = next((r for r in recs if str(getattr(r, "status", "")).lower()
                     in ("completed", "live", "in_play", "finished")), None)
    if explicit is not None:
        age = _age_h(explicit.fetched_at, now)
        return {"verdict": "ya_comenzado",
                "detail": (f"la fuente independiente declara explícitamente el evento "
                           f"{explicit.event_id or ''} como '{explicit.status}' "
                           f"(no upcoming; evidencia de juego, no caduca"
                           + (f"; snapshot de hace {age:.1f} h)" if age else ")")),
                **info, "explicit_status": explicit.status}

    # (B) HORA PROGRAMADA: sí caduca. Un snapshot viejo puede traer un horario
    # ya reprogramado (lluvia, orden de juego, cambio de pista), así que no
    # puede producir `ya_comenzado` ni conflicto por sí solo.
    sched_age = _age_h(best.fetched_at, now)
    if sched_age is not None and sched_age > schedule_max_age_h:
        return {"verdict": "secondary_stale_schedule",
                "snapshot_age_h": round(sched_age, 1),
                "detail": (f"la fuente independiente solo aporta una hora PROGRAMADA "
                           f"({best.commence_utc.isoformat()}) observada hace "
                           f"{sched_age:.1f} h (> {schedule_max_age_h} h): pudo "
                           f"reprogramarse, no es evidencia de juego ni contradice "
                           f"una hora primaria más reciente"),
                **info}
    # con snapshot reciente, una discrepancia material es contradicción real
    if declared_utc is not None:
        delta_min = abs((declared_utc - best.commence_utc).total_seconds()) / 60.0
        if delta_min > tolerance_minutes:
            return {"verdict": "conflicto_horario",
                    "detail": (f"calendario dice {declared_utc.isoformat()} y la fuente "
                               f"independiente {best.commence_utc.isoformat()} "
                               f"({delta_min:.0f} min de diferencia)"),
                    "delta_minutes": round(delta_min, 1), **info}
    # sin discrepancia (o sin hora primaria): una hora reciente ya pasada sí
    # indica que el partido debería haber empezado
    if best.commence_utc <= now:
        return {"verdict": "ya_comenzado",
                "detail": f"hora independiente {best.commence_utc.isoformat()} ya pasada",
                **info}
    return {"verdict": "coincide", "detail": "hora corroborada", **info}
