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
                               str(ev.get("sport_key", "")), fetched, registry)
            if rec:
                _add(idx, rec)
                n += 1
        idx.sources_ok.append(f"{m['name']} ({n} eventos, generado {fetched[:16] or '?'})")
    return idx


def _record_from(p1, p2, commence, event_id, source, sport_key, fetched, registry):
    if not p1 or not p2:
        return None
    dt = _parse_iso(commence)
    if dt is None:
        return None
    return CommenceRecord(pair=_canon_pair(str(p1), str(p2), registry), commence_utc=dt,
                          event_id=str(event_id or ""), source=source,
                          sport_key=str(sport_key or ""), fetched_at=str(fetched or ""),
                          players_raw=(str(p1), str(p2)))


def _add(idx: CommenceIndex, rec: CommenceRecord) -> None:
    idx.by_pair.setdefault(rec.pair, []).append(rec)
    if rec.sport_key:
        idx.covered_tours.add(rec.sport_key.lower())


# ---------------------------------------------------------------------------
# contraste
# ---------------------------------------------------------------------------

VERDICTS = ("sin_datos", "coincide", "conflicto_horario", "ya_comenzado", "ausente")


def cross_check(pair: tuple, declared_utc: datetime | None, idx: CommenceIndex,
                now: datetime, tolerance_minutes: float = 90.0,
                tournament_hint: str = "") -> dict:
    """Contrasta la hora declarada por el calendario con la independiente.

    - `ya_comenzado`: la hora independiente ya pasó (manda sobre el calendario).
    - `conflicto_horario`: difieren más que la tolerancia -> exclusión.
    - `ausente`: el índice cubre el torneo pero no tiene el partido.
    - `sin_datos`: no hay índice o no cubre ese torneo -> no se concluye nada.
    """
    if not idx.available:
        return {"verdict": "sin_datos", "detail": "sin índice independiente de horas"}
    recs = idx.get(pair)
    if not recs:
        if tournament_hint and idx.covers(tournament_hint):
            return {"verdict": "ausente",
                    "detail": ("el índice cubre este torneo pero no lista el partido "
                               "en próximos/en juego")}
        return {"verdict": "sin_datos", "detail": "el índice no cubre este torneo"}
    best = min(recs, key=lambda r: r.commence_utc)
    info = {"commence_utc": best.commence_utc.isoformat(), "event_id": best.event_id,
            "source": best.source, "sport_key": best.sport_key,
            "fetched_at": best.fetched_at}
    if best.commence_utc <= now:
        return {"verdict": "ya_comenzado",
                "detail": f"hora independiente {best.commence_utc.isoformat()} ya pasada",
                **info}
    if declared_utc is not None:
        delta_min = abs((declared_utc - best.commence_utc).total_seconds()) / 60.0
        if delta_min > tolerance_minutes:
            return {"verdict": "conflicto_horario",
                    "detail": (f"calendario dice {declared_utc.isoformat()} y la fuente "
                               f"independiente {best.commence_utc.isoformat()} "
                               f"({delta_min:.0f} min de diferencia)"),
                    "delta_minutes": round(delta_min, 1), **info}
    return {"verdict": "coincide", "detail": "hora corroborada", **info}
