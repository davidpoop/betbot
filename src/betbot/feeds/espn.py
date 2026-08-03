"""Fuente "espn": scoreboard público JSON de ESPN (solo lectura, sin credenciales).

Da calendario ATP/WTA por fechas (hoy y próximos días) y, cuando ESPN los
publica, un par de cuotas de moneyline. NOTA: en el entorno de desarrollo de
BetBot este dominio está bloqueado por el proxy; el adaptador queda operativo
para máquinas del usuario con salida normal a Internet. El escáner tolera su
fallo y lo refleja en la cobertura.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               strip_seed)
from betbot.schemas import OddsQuote

BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis/{league}/scoreboard?dates={dates}"


def _short_name(full: str) -> str:
    """"Carlos Alcaraz" -> "Alcaraz C." (formato Tennis-Data)."""
    parts = [p for p in str(full).replace("-", "-").split() if p]
    if len(parts) < 2:
        return full
    return " ".join(parts[1:]) + " " + parts[0][0] + "."


class EspnFeed:
    name = "espn"
    markets = ["match_winner"]

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl = ttl_seconds
        self._odds: list[OddsQuote] = []

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        now = datetime.now(timezone.utc)
        d0 = now.strftime("%Y%m%d")
        d1 = (now + timedelta(hours=window_hours)).strftime("%Y%m%d")
        dates = d0 if d1 == d0 else f"{d0}-{d1}"
        out: list[FeedMatch] = []
        self._odds = []
        errors = []
        for league, tour in (("atp", "ATP"), ("wta", "WTA")):
            try:
                data = cache.get_json(BASE.format(league=league, dates=dates), ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{league}: {exc}")
                continue
            for ev in data.get("events", []):
                tournament = str(ev.get("name") or (ev.get("league") or {}).get("name") or "")
                for comp in ev.get("competitions", []) or (ev.get("groupings") and
                        [c for g in ev.get("groupings", []) for c in g.get("competitions", [])]) or []:
                    ctype = str((comp.get("type") or {}).get("text", "")).lower()
                    if "doubles" in ctype:
                        continue
                    competitors = comp.get("competitors", [])
                    if len(competitors) != 2:
                        continue
                    names = []
                    for c in competitors:
                        ath = c.get("athlete") or c.get("team") or {}
                        names.append(str(ath.get("displayName") or ath.get("shortName") or ""))
                    if any("/" in n or not n for n in names):
                        continue
                    status = str(((comp.get("status") or {}).get("type") or {}).get("state", ""))
                    if status not in ("pre", ""):
                        continue                      # solo prepartido (sin live)
                    try:
                        d = datetime.fromisoformat(str(comp.get("date", ev.get("date", ""))).
                                                   replace("Z", "+00:00")).date()
                    except ValueError:
                        d = now.date()
                    rnd = str((comp.get("round") or {}).get("displayName", ""))
                    p1, p2 = _short_name(names[0]), _short_name(names[1])
                    fm = FeedMatch(
                        date=d, tour=tour, tournament=tournament,
                        player1=strip_seed(p1), player2=strip_seed(p2),
                        level=classify_level(tournament),
                        is_qualifying="qual" in rnd.lower(),
                        round=rnd,
                        best_of=5 if (tour == "ATP" and is_grand_slam(tournament)) else 3,
                        surface=None, source=self.name)
                    out.append(fm)
                    for oddsrec in comp.get("odds", []) or []:
                        prov = str((oddsrec.get("provider") or {}).get("name", "espn"))
                        ts = datetime.now(timezone.utc).isoformat()
                        for side, key in (("a", "homeTeamOdds"), ("b", "awayTeamOdds")):
                            mo = (oddsrec.get(key) or {}).get("moneyLine")
                            dec = _american_to_decimal(mo)
                            if dec:
                                self._odds.append(OddsQuote(
                                    player_a=fm.player1, player_b=fm.player2,
                                    market="match_winner", selection=side, odds=dec,
                                    bookmaker=f"espn:{prov}", timestamp=ts))
        if out or not errors:
            st.ok = True
        st.n_items = len(out)
        st.error = "; ".join(errors)
        return out, st

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=True, n_items=len(self._odds),
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self._odds:
            st.notes.append("ESPN no publicó cuotas en este ciclo (o calendario no cargado)")
        return list(self._odds), st


def _american_to_decimal(mo) -> float | None:
    try:
        v = float(mo)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    return round(1.0 + (v / 100.0 if v > 0 else 100.0 / abs(v)), 3)
