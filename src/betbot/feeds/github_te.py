"""Fuente "github_te": dataset público republicado en GitHub (raw) con los
partidos DEL DÍA y cuotas de moneyline (proyecto Mriganka-codes/tennis_data,
actualizado cada 6 h vía GitHub Actions a partir de tennisexplorer).

BetBot solo CONSUME el JSON publicado en raw.githubusercontent.com (lectura de
un dataset público); no scrapea la web de origen.

*** NO ES FUENTE AUTORITATIVA DE CALENDARIO (authoritative = False) ***
El feed publica por fila únicamente {tournament, time, player1, player2,
odds1, odds2, tour}: no trae fecha, ni zona horaria, ni estado del partido, ni
identificador de evento. Un partido ya terminado sigue apareciendo con sus
cuotas prepartido (caso real Tsitsipas–Fonseca, 2026-08-05). Por eso sus filas
NUNCA pueden crear un evento elegible: solo sirven para aportar cuotas a
eventos ya confirmados por una fuente autoritativa; las que no encuentran
evento quedan como `orphan_quote`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               strip_seed)
from betbot.schemas import OddsQuote

URL = "https://raw.githubusercontent.com/Mriganka-codes/tennis_data/main/matches.json"
BOOK = "TE"


class GithubTEFeed:
    """Fuente de CUOTAS moneyline. Implementa también fetch_matches, pero sus
    filas salen marcadas como no autoritativas / estado desconocido / sin hora
    de inicio, de modo que la puerta prepartido las rechaza siempre."""
    name = "github_te"
    markets = ["match_winner"]
    authoritative = False        # ← nunca puede confirmar un partido prepartido

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

    @staticmethod
    def _feed_ts(data: dict) -> str:
        ts = str(data.get("last_updated", "")) or datetime.now(timezone.utc).isoformat()
        if ts and "T" in ts and "+" not in ts and "Z" not in ts:
            ts = ts + "+00:00"           # el feed publica UTC naive
        return ts

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        """Filas informativas del feed. NO son calendario: van con
        status='unknown', scheduled_at_utc=None y authoritative=False."""
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        try:
            data = self._load()
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        ts = self._feed_ts(data)
        st.data_timestamp = ts
        # la fecha del feed es la del snapshot, NO la del partido: se usa solo
        # como etiqueta informativa, nunca para declarar el partido futuro
        try:
            snap_date = datetime.fromisoformat(ts).date()
        except ValueError:
            snap_date = datetime.now(timezone.utc).date()
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
                date=snap_date, tour=tour, tournament=tournament,
                player1=p1, player2=p2,
                level=classify_level(tournament),
                is_doubles=is_doubles,
                is_qualifying=False,     # la fuente no lo distingue (limitación declarada)
                best_of=5 if (tour == "ATP" and is_grand_slam(tournament)) else 3,
                surface=None,            # la fuente no da superficie
                start_local=str(r.get("time", "")),
                source=self.name,
                # --- lo que esta fuente NO puede afirmar ---
                event_id="", scheduled_at_utc=None, status="unknown",
                source_updated_at=ts, start_tz="", authoritative=False))
        st.ok = True
        st.n_items = len(out)
        st.notes.append("NO autoritativa: sin fecha, hora ni estado por partido; "
                        "solo aporta cuotas a eventos ya confirmados")
        return out, st

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        """Cuotas moneyline enlazadas por enfrentamiento (sin fecha) contra los
        eventos confirmados. Sin evento confirmado -> orphan_quote (se cuenta y
        se informa, pero no se analiza)."""
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        try:
            data = self._load()
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        ts = self._feed_ts(data)
        st.data_timestamp = ts
        wanted = {m.pair_key for m in matches}
        quotes: list[OddsQuote] = []
        n_orphan = 0
        for r in data.get("matches", []):
            p1 = strip_seed(str(r.get("player1", "")))
            p2 = strip_seed(str(r.get("player2", "")))
            tour = str(r.get("tour", "")).upper()
            if not p1 or not p2 or tour not in ("ATP", "WTA"):
                continue
            probe = FeedMatch(date=datetime.now(timezone.utc).date(), tour=tour,
                              tournament=str(r.get("tournament", "")),
                              player1=p1, player2=p2)
            if probe.pair_key not in wanted:
                n_orphan += 1
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
        st.n_orphan = n_orphan
        if n_orphan:
            st.notes.append(f"{n_orphan} cuotas sin evento confirmado (orphan_quote): "
                            f"no se analizan")
        return quotes, st
