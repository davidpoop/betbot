"""Fuente "oddsapi": The Odds API v4 (oficial, solo lectura, requiere clave
gratuita del usuario en la variable de entorno BETBOT_ODDS_API_KEY o en
config feeds.odds_api_key). Cubre torneos grandes (Slams/1000/500) con h2h de
múltiples operadores. Sin clave, el adaptador se declara no disponible y el
escáner continúa con las demás fuentes.

También actúa como CALENDARIO (ATP+WTA): el endpoint `/events` publica, por
partido próximo, un id nativo estable, `commence_time` ISO UTC y los dos
jugadores. El proveedor solo lista eventos por comenzar (los empezados salen
del feed), así que su presencia con hora futura es evidencia razonable de
prepartido; aun así se marca `trust_tier="schedule_only"` (no publica marcador)
y la puerta le exige resultados frescos o corroboración INDEPENDIENTE — la
corroboración del propio índice de commence-time (misma familia The Odds API)
no cuenta como independiente.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import FeedMatch, SourceStatus, classify_level, strip_seed
from betbot.schemas import OddsQuote

BASE = "https://api.the-odds-api.com/v4"


def _sport_to_tour(sport_key: str, title: str = "") -> str:
    s = f"{sport_key} {title}".lower()
    if "wta" in s:
        return "WTA"
    if "atp" in s:
        return "ATP"
    return ""


def _parse_commence(raw) -> datetime | None:
    s = str(raw or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _short(full: str) -> str:
    parts = str(full).split()
    if len(parts) < 2:
        return full
    return " ".join(parts[1:]) + " " + parts[0][0] + "."


class OddsApiFeed:
    name = "oddsapi"
    supported_tours = frozenset({"ATP", "WTA"})
    markets = ["match_winner"]
    authoritative = True         # /events publica id + commence_time + jugadores

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 600) -> None:
        self.key = api_key or os.environ.get("BETBOT_ODDS_API_KEY", "")
        self.ttl = ttl_seconds

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        """CalendarSource sobre `/events` (no consume cuota de odds). Es hoy la
        única vía autoritativa de calendario ATP operativa sin ESPN."""
        now = datetime.now(timezone.utc)
        st = SourceStatus(name=self.name, ok=False, authoritative=True,
                          fetched_at=now.isoformat())
        if not self.key:
            st.error = "sin BETBOT_ODDS_API_KEY (adaptador inactivo)"
            return [], st
        out: list[FeedMatch] = []
        try:
            sports = cache.get_json(f"{BASE}/sports/?apiKey={self.key}", ttl_seconds=3600)
            tennis = [(s["key"], str(s.get("title", ""))) for s in sports
                      if s.get("group") == "Tennis" and s.get("active")]
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        hi = now + timedelta(hours=window_hours)
        errors: list[str] = []
        for skey, title in tennis:
            tour = _sport_to_tour(skey, title)
            if not tour:
                continue                       # challengers/exhibiciones fuera
            try:
                events = cache.get_json(
                    f"{BASE}/sports/{skey}/events/?apiKey={self.key}",
                    ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{skey}: {exc}")
                continue
            for ev in events or []:
                p1 = strip_seed(_short(ev.get("home_team", "")))
                p2 = strip_seed(_short(ev.get("away_team", "")))
                if not p1 or not p2 or "/" in p1 or "/" in p2:
                    continue
                start = _parse_commence(ev.get("commence_time"))
                if start is None or not (now - timedelta(hours=1) <= start <= hi):
                    continue
                tournament = title or skey
                out.append(FeedMatch(
                    date=start.date(), tour=tour, tournament=tournament,
                    player1=p1, player2=p2, level=classify_level(tournament),
                    best_of=3 if tour == "WTA" else
                    (5 if "open" in tournament.lower() and
                     any(g in tournament.lower() for g in
                         ("australian", "french", "wimbledon", "us open")) else 3),
                    surface=None, source=self.name,
                    event_id=f"oddsapi:{ev.get('id', '')}",
                    scheduled_at_utc=start,
                    status="scheduled",       # el feed solo lista próximos
                    source_updated_at=now.isoformat(), start_tz="UTC",
                    authoritative=True, trust_tier="schedule_only",
                    raw={"sport_key": skey, "id": ev.get("id")}))
        from betbot.feeds.base import mark_placeholder_times
        mark_placeholder_times(out)
        st.ok = not errors or bool(out)
        st.n_items = len(out)
        st.error = "; ".join(errors[:3])
        if st.ok:
            st.notes.append(f"{len(tennis)} torneos de tenis activos en el proveedor")
        return out, st

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.key:
            st.error = "sin BETBOT_ODDS_API_KEY (adaptador inactivo)"
            return [], st
        quotes: list[OddsQuote] = []
        try:
            sports = cache.get_json(f"{BASE}/sports/?apiKey={self.key}", ttl_seconds=3600)
            tennis_keys = [s["key"] for s in sports
                           if s.get("group") == "Tennis" and s.get("active")]
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        for skey in tennis_keys:
            try:
                events = cache.get_json(
                    f"{BASE}/sports/{skey}/odds/?apiKey={self.key}&regions=eu&markets=h2h"
                    "&oddsFormat=decimal", ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                st.notes.append(f"{skey}: {exc}")
                continue
            for ev in events:
                p1 = strip_seed(_short(ev.get("home_team", "")))
                p2 = strip_seed(_short(ev.get("away_team", "")))
                for bk in ev.get("bookmakers", []):
                    book = str(bk.get("key", "book"))
                    ts = str(bk.get("last_update") or datetime.now(timezone.utc).isoformat())
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for oc in mkt.get("outcomes", []):
                            sel_name = strip_seed(_short(oc.get("name", "")))
                            sel = "a" if sel_name == p1 else ("b" if sel_name == p2 else None)
                            try:
                                odds = float(oc.get("price"))
                            except (TypeError, ValueError):
                                continue
                            if sel and odds > 1.0:
                                quotes.append(OddsQuote(
                                    player_a=p1, player_b=p2, market="match_winner",
                                    selection=sel, odds=odds, bookmaker=book, timestamp=ts))
        st.ok = True
        st.n_items = len(quotes)
        return quotes, st
