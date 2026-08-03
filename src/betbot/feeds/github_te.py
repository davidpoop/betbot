"""Fuente "github_te": dataset público republicado en GitHub (raw) con los
partidos DEL DÍA y cuotas de moneyline (proyecto Mriganka-codes/tennis_data,
actualizado cada 6 h vía GitHub Actions a partir de tennisexplorer).

BetBot solo CONSUME el JSON publicado en raw.githubusercontent.com (lectura de
un dataset público); no scrapea la web de origen. Limitaciones reales que esta
fuente declara: solo partidos de HOY, solo moneyline, una única serie de cuotas
("TE"), sin superficie ni ronda. El escáner informa de esa cobertura.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               strip_seed)
from betbot.schemas import OddsQuote

URL = "https://raw.githubusercontent.com/Mriganka-codes/tennis_data/main/matches.json"
BOOK = "TE"


class GithubTEFeed:
    """Implementa CalendarSource y OddsSource a la vez."""
    name = "github_te"
    markets = ["match_winner"]

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl = ttl_seconds
        self._raw: dict | None = None
        self._status = SourceStatus(name=self.name, ok=False)

    def _load(self) -> dict:
        if self._raw is None:
            data = cache.get_json(URL, ttl_seconds=self.ttl)
            if not isinstance(data, dict) or "matches" not in data:
                raise RuntimeError("formato inesperado de matches.json")
            self._raw = data
        return self._raw

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        try:
            data = self._load()
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        st.data_timestamp = str(data.get("last_updated", ""))
        today = datetime.now(timezone.utc).date()
        out: list[FeedMatch] = []
        for r in data.get("matches", []):
            p1 = strip_seed(str(r.get("player1", "")))
            p2 = strip_seed(str(r.get("player2", "")))
            tournament = str(r.get("tournament", "")).strip()
            tour = str(r.get("tour", "")).strip().upper()
            if not p1 or not p2 or tour not in ("ATP", "WTA"):
                continue
            is_doubles = "/" in p1 or "/" in p2
            out.append(FeedMatch(
                date=today, tour=tour, tournament=tournament,
                player1=p1, player2=p2,
                level=classify_level(tournament),
                is_doubles=is_doubles,
                is_qualifying=False,     # la fuente no lo distingue (limitación declarada)
                best_of=5 if (tour == "ATP" and is_grand_slam(tournament)) else 3,
                surface=None,            # la fuente no da superficie
                start_local=str(r.get("time", "")),
                source=self.name))
        st.ok = True
        st.n_items = len(out)
        if window_hours > 24:
            st.notes.append("esta fuente solo publica los partidos de HOY (no 48h)")
        st.notes.append("sin ronda ni superficie; no distingue qualifying")
        return out, st

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        try:
            data = self._load()
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        ts = str(data.get("last_updated", "")) or datetime.now(timezone.utc).isoformat()
        if ts and "T" in ts and "+" not in ts and "Z" not in ts:
            ts = ts + "+00:00"           # el feed publica UTC naive
        st.data_timestamp = ts
        wanted = {m.key for m in matches}
        quotes: list[OddsQuote] = []
        today = datetime.now(timezone.utc).date()
        for r in data.get("matches", []):
            p1 = strip_seed(str(r.get("player1", "")))
            p2 = strip_seed(str(r.get("player2", "")))
            fm = FeedMatch(date=today, tour=str(r.get("tour", "")).upper(),
                           tournament=str(r.get("tournament", "")), player1=p1, player2=p2)
            if wanted and fm.key not in wanted:
                continue
            o1, o2 = r.get("odds1"), r.get("odds2")
            for sel, odds in (("a", o1), ("b", o2)):
                try:
                    odds_f = float(odds)
                except (TypeError, ValueError):
                    continue
                if odds_f <= 1.0:
                    continue            # la fuente no tiene cuota real de ese lado
                quotes.append(OddsQuote(player_a=p1, player_b=p2, market="match_winner",
                                        selection=sel, odds=odds_f, bookmaker=BOOK,
                                        timestamp=ts))
        st.ok = True
        st.n_items = len(quotes)
        return quotes, st
