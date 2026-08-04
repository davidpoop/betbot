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
from betbot.feeds.base import SourceStatus, classify_level, is_grand_slam, strip_seed


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
