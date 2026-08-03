"""Fuente "oddsapi": The Odds API v4 (oficial, solo lectura, requiere clave
gratuita del usuario en la variable de entorno BETBOT_ODDS_API_KEY o en
config feeds.odds_api_key). Cubre torneos grandes (Slams/1000/500) con h2h de
múltiples operadores. Sin clave, el adaptador se declara no disponible y el
escáner continúa con las demás fuentes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from betbot.feeds import cache
from betbot.feeds.base import FeedMatch, SourceStatus, strip_seed
from betbot.schemas import OddsQuote

BASE = "https://api.the-odds-api.com/v4"


def _short(full: str) -> str:
    parts = str(full).split()
    if len(parts) < 2:
        return full
    return " ".join(parts[1:]) + " " + parts[0][0] + "."


class OddsApiFeed:
    name = "oddsapi"
    markets = ["match_winner"]

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 600) -> None:
        self.key = api_key or os.environ.get("BETBOT_ODDS_API_KEY", "")
        self.ttl = ttl_seconds

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
