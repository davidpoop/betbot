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
    """
    name = "sportradar"
    BASE = "https://api.sportradar.com/tennis/trial/v3/en"

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 900) -> None:
        self.key = api_key or os.environ.get("SPORTRADAR_API_KEY", "")
        self.ttl = ttl_seconds

    def fetch_results(self, since: date, until: date) -> tuple[list[dict], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.key:
            st.error = "sin SPORTRADAR_API_KEY (adaptador inactivo)"
            return [], st
        rows: list[dict] = []
        d = since
        errors: list[str] = []
        while d <= until:
            url = f"{self.BASE}/schedules/{d.isoformat()}/summaries.json?api_key={self.key}"
            try:
                data = cache.get_json(url, ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{d}: {exc}")
                d += timedelta(days=1)
                continue
            for summ in data.get("summaries", []):
                row = self._parse_summary(summ, d)
                if row:
                    rows.append(row)
            d += timedelta(days=1)
        st.ok = not errors or bool(rows)
        st.n_items = len(rows)
        st.error = "; ".join(errors[:3])
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
            url = f"{self.BASE}/schedules/{d.isoformat()}/summaries.json?api_key={self.key}"
            try:
                data = cache.get_json(url, ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{d}: {exc}")
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
