"""Fuentes estructuradas de RESULTADOS recientes ATP/WTA (solo lectura).

Protocolo ResultsSource: fetch_results(since, until) -> (filas, SourceStatus)
donde cada fila usa el mismo esquema que la plantilla manual:
date,tour,tournament,surface,indoor,round,best_of,winner,loser,score,status
(el marcador orientado al ganador). La validación/dedupe/cuarentena la hace el
núcleo compartido de ingest.manual_results.prepare_rows.

Orden recomendado (config feeds.results_order):
1. sportradar  — oficial, requiere SPORTRADAR_API_KEY (inactivo sin clave).
2. github      — mirrors públicos estructurados (feeds.results_github):
                 ATP diario (espejo TML) y WTA semanal (TennisCourtLog).
No hay adaptador ESPN: sus endpoints están bloqueados desde este entorno y su
estructura JSON no pudo verificarse; un parser a ciegas sería un stub.
Los mirrors históricos se actualizan con `betbot update-data` (no aquí).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import pandas as pd

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               strip_seed)

# Enums CERRADOS del contrato OpenAPI "Tennis v3" de Sportradar
# (sport_event_status.match_status y .status) -> estados canónicos de BetBot.
SR_MATCH_STATUS = {
    "not_started": "scheduled", "match_about_to_start": "scheduled",
    "start_delayed": "delayed", "postponed": "postponed",
    "started": "live", "interrupted": "live", "suspended": "live",
    "1st_set": "live", "2nd_set": "live", "3rd_set": "live",
    "4th_set": "live", "5th_set": "live",
    "ended": "completed", "closed": "completed", "retired": "completed",
    "defaulted": "completed", "walkover": "walkover",
    "cancelled": "cancelled", "abandoned": "cancelled",
}
SR_STATUS = {
    "not_started": "scheduled", "delayed": "delayed", "postponed": "postponed",
    "live": "live", "started": "live", "interrupted": "live", "suspended": "live",
    "ended": "completed", "closed": "completed",
    "cancelled": "cancelled", "abandoned": "cancelled",
}


def sportradar_status(status_block: dict) -> str:
    """match_status manda (es más específico); status es el respaldo."""
    ms = str(status_block.get("match_status") or "").strip().lower()
    if ms in SR_MATCH_STATUS:
        return SR_MATCH_STATUS[ms]
    s = str(status_block.get("status") or "").strip().lower()
    return SR_STATUS.get(s, "unknown")


class ResultsSource(Protocol):
    # cada adaptador declara además `supported_tours` (frozenset de "ATP"/"WTA"):
    # la matriz de capacidades solo puede listar una fuente bajo los tours que
    # declara, nunca inferir cobertura de un tour porque el fetch global trajo datos
    name: str

    def fetch_results(self, since: date, until: date) -> tuple[list[dict], SourceStatus]: ...


def _score_from_periods(periods: list[tuple[int, int]], winner_first: bool) -> str:
    parts = []
    for a, b in periods:
        parts.append(f"{a}-{b}" if winner_first else f"{b}-{a}")
    return " ".join(parts)


class SportradarResults:
    """API oficial de Sportradar Tennis v3 (summaries por día).

    Requiere SPORTRADAR_API_KEY (plan trial o de pago). Solo lectura.

    CONSCIENTE DE CUOTA (el trial es limitado):
    - Autenticación por header `x-api-key`: la clave JAMÁS va en la URL, así
      tampoco entra en el hash del fichero de caché ni en ningún error que
      cite la URL (y cache.redact sigue de segundo cinturón).
    - Fetch INCREMENTAL: con frescura local D solo se consultan los días
      posteriores a D más una ventana de replay corta (`replay_days`, def. 2:
      D-1 y D) para capturar correcciones oficiales tardías. Jamás se
      recorre entero el lookback del sync una vez al día ya alcanzado.
    - TTL por antigüedad del día: hoy = ttl corto (900 s, el feed cambia);
      ayer = 6 h (correcciones frecuentes el primer día); días anteriores =
      7 días (un día cerrado solo cambia por corrección oficial rara — la
      ventana de replay ya cubre las normales, y la expiración semanal
      permite igualmente las excepcionales).
    - PRESUPUESTO por sync (`max_requests`, config
      feeds.sportradar_max_requests_per_sync): si el plan de días exige más
      peticiones de red, NO se gasta nada en silencio — se informa de
      cuántas harían falta y se exige el backfill explícito
      (`betbot sync-results --backfill-atp`).
    - HUECOS: si un día falla, los días POSTERIORES no se consultan ni se
      devuelven: la frescura global no puede saltar por encima de un hueco.
      El siguiente sync reintenta desde el día fallido (los días ya bajados
      quedan en caché y no vuelven a gastar red).
    """
    name = "sportradar"
    supported_tours = frozenset({"ATP", "WTA"})
    BASE = "https://api.sportradar.com/tennis/trial/v3/en"
    TTL_YESTERDAY = 6 * 3600
    TTL_PAST = 7 * 24 * 3600

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 900,
                 fresh_until: dict | None = None, replay_days: int = 2,
                 max_requests: int = 8, allow_backfill: bool = False) -> None:
        self.key = api_key or os.environ.get("SPORTRADAR_API_KEY", "")
        self.ttl = ttl_seconds
        self.fresh_until = dict(fresh_until or {})
        self.replay_days = max(1, int(replay_days))
        self.max_requests = max(1, int(max_requests))
        self.allow_backfill = allow_backfill

    def _headers(self) -> dict:
        return {"x-api-key": self.key, "Accept": "application/json"}

    def _day_url(self, d: date) -> str:
        return f"{self.BASE}/schedules/{d.isoformat()}/summaries.json"

    def _ttl_for(self, d: date, today: date) -> int:
        if d >= today:
            return self.ttl
        if d == today - timedelta(days=1):
            return self.TTL_YESTERDAY
        return self.TTL_PAST

    def _plan_days(self, since: date, until: date) -> list[date]:
        """Días a consultar: (frescura local mínima de los tours cubiertos −
        replay) .. until, sin bajar del `since` del sync."""
        start = since
        fresh = [self.fresh_until[t] for t in self.supported_tours
                 if self.fresh_until.get(t)]
        if fresh:
            d0 = min(fresh) - timedelta(days=self.replay_days - 1)
            start = max(since, d0)
        if start > until:
            return []
        return [start + timedelta(days=i) for i in range((until - start).days + 1)]

    def fetch_results(self, since: date, until: date) -> tuple[list[dict], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.key:
            st.error = "sin SPORTRADAR_API_KEY (adaptador inactivo)"
            return [], st
        today = datetime.now(timezone.utc).date()
        days = self._plan_days(since, until)
        if not days:
            st.ok = True
            st.notes.append("sin días nuevos que consultar (frescura al día)")
            return [], st
        need_net = [d for d in days
                    if not cache.is_cached(self._day_url(d), self._ttl_for(d, today))]
        if len(need_net) > self.max_requests and not self.allow_backfill:
            st.error = (f"presupuesto insuficiente: harían falta {len(need_net)} "
                        f"peticiones de red para {days[0]}..{days[-1]} (límite "
                        f"feeds.sportradar_max_requests_per_sync={self.max_requests}). "
                        f"Autoriza el gasto UNA vez con: betbot sync-results --backfill-atp")
            st.notes.append("no se ha gastado ninguna petición")
            return [], st

        rows: list[dict] = []
        n_net = n_cache = 0
        failed: date | None = None
        fetched: list[date] = []
        comp_by_tour: dict[str, set] = {"ATP": set(), "WTA": set()}
        n_fin = {"ATP": 0, "WTA": 0}
        for d in days:
            url = self._day_url(d)
            was_cached = cache.is_cached(url, self._ttl_for(d, today))
            try:
                data = cache.get_json(url, ttl_seconds=self._ttl_for(d, today),
                                      headers=self._headers())
            except Exception as exc:  # noqa: BLE001
                failed = d
                st.error = cache.redact(f"{d}: {exc}")
                break                 # los días posteriores NO se consultan: sin huecos
            n_cache += 1 if was_cached else 0
            n_net += 0 if was_cached else 1
            fetched.append(d)
            for summ in data.get("summaries", []):
                row = self._parse_summary(summ, d)
                if row:
                    rows.append(row)
                    t = row["tour"]
                    comp_by_tour.setdefault(t, set()).add(row["tournament"])
                    n_fin[t] = n_fin.get(t, 0) + 1
        st.ok = failed is None or bool(fetched)
        st.n_items = len(rows)
        rng = f"{days[0]}..{days[-1]}"
        st.notes.append(f"telemetría: dates_requested={len(days)} · "
                        f"from_cache={n_cache} · network_requests={n_net} · rango={rng}")
        for t in ("ATP", "WTA"):
            if n_fin.get(t):
                comps = sorted(comp_by_tour.get(t, set()))
                st.notes.append(f"{t}: {n_fin[t]} finalizados en {len(comps)} "
                                f"competiciones ({', '.join(comps[:4])}"
                                + ("…" if len(comps) > 4 else "") + ")")
        if failed is not None:
            withheld = [d for d in days if d > failed]
            st.notes.append(f"día {failed} FALLÓ: {len(withheld)} días posteriores "
                            f"retenidos para no declarar frescura con hueco; el "
                            f"próximo sync reintentará desde {failed}")
        return rows, st

    # ------------------------------------------------------------------
    # calendario prepartido (mismo endpoint, otra proyección)
    # ------------------------------------------------------------------
    authoritative = True

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        """CalendarSource: partidos con id URN estable, hora ISO con offset y
        estado de enum cerrado. Inactivo sin SPORTRADAR_API_KEY."""
        now = datetime.now(timezone.utc)
        st = SourceStatus(name=self.name, ok=False, authoritative=True,
                          fetched_at=now.isoformat())
        if not self.key:
            st.error = "sin SPORTRADAR_API_KEY (adaptador inactivo)"
            return [], st
        out: list[FeedMatch] = []
        errors: list[str] = []
        lo, hi = now - timedelta(days=1), now + timedelta(hours=window_hours)
        d, last = now.date(), (now + timedelta(hours=window_hours)).date()
        while d <= last:
            url = self._day_url(d)                    # key SOLO en header x-api-key
            try:
                data = cache.get_json(url, ttl_seconds=self.ttl, headers=self._headers())
            except Exception as exc:  # noqa: BLE001
                errors.append(cache.redact(f"{d}: {exc}"))
                d += timedelta(days=1)
                continue
            for summ in data.get("summaries", []):
                fm = self.parse_event(summ, now.isoformat())
                if fm is None or fm.scheduled_at_utc is None:
                    continue
                if lo <= fm.scheduled_at_utc <= hi:
                    out.append(fm)
            d += timedelta(days=1)
        from betbot.feeds.base import mark_placeholder_times
        mark_placeholder_times(out)
        st.ok = not errors or bool(out)
        st.n_items = len(out)
        st.error = "; ".join(errors[:3])
        return out, st

    @staticmethod
    def parse_event(summ: dict, observed_at: str) -> FeedMatch | None:
        """summaries[] -> FeedMatch con id URN, hora UTC y estado canónico."""
        ev = summ.get("sport_event", {}) or {}
        status_block = summ.get("sport_event_status", {}) or {}
        ctx = ev.get("sport_event_context", {}) or {}
        category = str((ctx.get("category") or {}).get("name", "")).upper()
        if category not in ("ATP", "WTA"):
            return None
        comp_name = str((ctx.get("competition") or {}).get("name", ""))
        competitors = ev.get("competitors", []) or []
        if len(competitors) != 2:
            return None
        is_doubles = any(str(c.get("type", "")).lower() == "double" or
                         "/" in str(c.get("name", "")) for c in competitors)
        names = [str(c.get("name", "")) for c in competitors]
        if not all(names):
            return None
        start = None
        raw = str(ev.get("start_time", ""))
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                start = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                start = None
        rnd = str((ctx.get("round") or {}).get("name", ""))

        def td_name(n: str) -> str:
            if "," in n:
                last, first = [x.strip() for x in n.split(",", 1)]
                return f"{last} {first[:1]}."
            return n

        p1, p2 = strip_seed(td_name(names[0])), strip_seed(td_name(names[1]))
        return FeedMatch(
            date=(start.date() if start else datetime.now(timezone.utc).date()),
            tour=category, tournament=comp_name, player1=p1, player2=p2,
            level=classify_level(comp_name), is_doubles=is_doubles,
            is_qualifying="qualif" in rnd.lower(),
            round=rnd,
            best_of=5 if (category == "ATP" and is_grand_slam(comp_name)) else 3,
            surface=str((ctx.get("competition") or {}).get("surface") or "").title() or None,
            source="sportradar", event_id=str(ev.get("id", "")),
            scheduled_at_utc=start, status=sportradar_status(status_block),
            source_updated_at=observed_at, start_tz="UTC", authoritative=True)

    @staticmethod
    def _parse_summary(summ: dict, d: date) -> dict | None:
        ev = summ.get("sport_event", {})
        status = summ.get("sport_event_status", {})
        ctx = ev.get("sport_event_context", {})
        category = str((ctx.get("category") or {}).get("name", "")).upper()
        if category not in ("ATP", "WTA"):
            return None
        comp_name = str((ctx.get("competition") or {}).get("name", ""))
        level = classify_level(comp_name)
        competitors = ev.get("competitors", [])
        if len(competitors) != 2 or any(c.get("type") != "singles"
                                        for c in competitors if c.get("type")):
            return None
        match_status = str(status.get("match_status") or status.get("status") or "")
        if match_status not in ("ended", "closed", "retired", "walkover"):
            return None
        winner_id = status.get("winner_id")
        if not winner_id:
            return None
        names = {c["id"]: str(c.get("name", "")) for c in competitors}
        winner = names.get(winner_id, "")
        loser = next((n for cid, n in names.items() if cid != winner_id), "")
        if not winner or not loser:
            return None
        # "Alcaraz, Carlos" -> "Alcaraz C."
        def td_name(n: str) -> str:
            if "," in n:
                last, first = [x.strip() for x in n.split(",", 1)]
                return f"{last} {first[:1]}."
            return n
        # marcador por sets orientado al ganador
        w_first = competitors[0]["id"] == winner_id
        periods = [(int(p.get("home_score", 0)), int(p.get("away_score", 0)))
                   for p in status.get("period_scores", [])]
        score = _score_from_periods(periods, winner_first=w_first)
        st_map = {"ended": "completed", "closed": "completed",
                  "retired": "retired", "walkover": "walkover"}
        surface = str((ctx.get("competition") or {}).get("surface")
                      or ev.get("venue", {}).get("surface") or "").title() or ""
        start = str(ev.get("start_time", ""))[:10]
        return {
            "date": start or d.isoformat(), "tour": category, "tournament": comp_name,
            "surface": surface or "Hard", "indoor": "false",
            "round": str((ctx.get("round") or {}).get("name", "")),
            "best_of": "5" if (category == "ATP" and is_grand_slam(comp_name)) else "3",
            "winner": strip_seed(td_name(winner)), "loser": strip_seed(td_name(loser)),
            "score": score, "status": st_map.get(match_status, "completed"),
            "_level": level,
        }
