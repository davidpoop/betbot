"""Resultados recientes desde mirrors públicos estructurados en GitHub.

Dos ficheros CSV con fecha POR PARTIDO y marcador orientado al ganador
(verificados el 2026-08-04 desde este entorno):

- ATP: gmalbert/tennis-predictions tml-data/{year}.csv — espejo diario
  (~05:00 UTC) de stats.tennismylife.org, formato Sackmann/TML (50 columnas,
  incl. score con tiebreaks, best_of, round, surface, tourney_level).
- WTA: LuckyLoser91/TennisCourtLog tennis_wta/wta_matches_{year}.csv — main
  tour completo (GS/1000/500/250), cadencia semanal (lunes) + manual. El
  fichero está en Git LFS: hay que leerlo por media.githubusercontent.com
  (raw.githubusercontent.com devuelve solo el puntero LFS).

Sin adaptador ESPN: sus dominios están bloqueados desde este entorno y la
estructura JSON no pudo verificarse; publicar un parser a ciegas sería un stub.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone

import pandas as pd

from betbot.feeds import cache
from betbot.feeds.base import SourceStatus

ATP_URL = "https://raw.githubusercontent.com/gmalbert/tennis-predictions/main/tml-data/{year}.csv"
WTA_URL = ("https://media.githubusercontent.com/media/LuckyLoser91/TennisCourtLog/"
           "main/tennis_wta/wta_matches_{year}.csv")

# niveles main-tour por fichero; ATP 'D' (Davis Cup, equipos) queda excluido
_ATP_MAIN = {"G", "M", "A", "F", "500", "250"}
_WTA_MAIN = {"Grand Slam", "WTA1000", "WTA500", "WTA250"}


def _parse_score(raw: str) -> tuple[str, str]:
    """(score_limpio, status) a partir del marcador Sackmann/TML."""
    s = str(raw or "").strip()
    up = s.upper()
    if not s or up in ("W/O", "WO", "W-O"):
        return "", "walkover"
    for marker in (" RET", " DEF", " ABD", " ABN"):
        if up.endswith(marker.strip()) or marker in up:
            cleaned = up.replace(marker.strip(), "").strip()
            if not cleaned:
                return "", "walkover"
            return cleaned, "retired"
    return s, "completed"


class GithubResults:
    """ResultsSource sobre los dos mirrors GitHub (ATP diario, WTA semanal)."""
    name = "github"
    supported_tours = frozenset({"ATP", "WTA"})

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl = ttl_seconds

    def fetch_results(self, since: date, until: date) -> tuple[list[dict], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        rows: list[dict] = []
        errors: list[str] = []
        out_of_window: list[str] = []
        years = range(since.year, until.year + 1)
        for year in years:
            for tour, url_tpl, parser in (("ATP", ATP_URL, self._parse_atp),
                                          ("WTA", WTA_URL, self._parse_wta)):
                url = url_tpl.format(year=year)
                try:
                    text = cache.get_text(url, ttl_seconds=self.ttl)
                    df = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
                except Exception as exc:  # noqa: BLE001 - un fichero caído no rompe el resto
                    errors.append(f"{tour} {year}: {exc}")
                    continue
                n_before = len(rows)
                for _, r in df.iterrows():
                    parsed = parser(r)
                    if parsed is None:
                        continue
                    d = parsed["_date"]
                    if d > until:
                        out_of_window.append(
                            f"{parsed['tournament']} {parsed['date']} "
                            f"{parsed['winner']}–{parsed['loser']}")
                        continue
                    if d < since:
                        continue
                    parsed.pop("_date")
                    rows.append(parsed)
                st.notes.append(f"{tour} {year}: {len(rows) - n_before} filas en ventana")
        if out_of_window:
            st.notes.append(
                f"{len(out_of_window)} filas con fecha posterior a la ventana descartadas "
                f"(posible errata de la fuente): " + "; ".join(out_of_window[:3]))
        st.ok = bool(rows) or not errors
        st.n_items = len(rows)
        st.error = "; ".join(errors[:3])
        return rows, st

    @staticmethod
    def _parse_atp(r: pd.Series) -> dict | None:
        level = str(r.get("tourney_level", "")).strip()
        if level not in _ATP_MAIN:
            return None
        raw_d = str(r.get("tourney_date", "")).strip()
        try:
            d = datetime.strptime(raw_d, "%Y%m%d").date()
        except ValueError:
            return None
        score, status = _parse_score(r.get("score", ""))
        winner = str(r.get("winner_name", "")).strip()
        loser = str(r.get("loser_name", "")).strip()
        if not winner or not loser:
            return None
        return {
            "date": d.isoformat(), "_date": d, "tour": "ATP",
            "tournament": str(r.get("tourney_name", "")).strip(),
            "surface": str(r.get("surface", "")).strip().title() or "Hard",
            "indoor": "true" if str(r.get("indoor", "O")).strip().upper() == "I" else "false",
            "round": str(r.get("round", "")).strip(),
            "best_of": str(r.get("best_of", "3")).strip() or "3",
            "winner": winner, "loser": loser,
            "score": score, "status": status, "_level": "main",
            # rank del momento del partido: alimenta el ranking derivado
            "winner_rank": str(r.get("winner_rank", "")).strip(),
            "loser_rank": str(r.get("loser_rank", "")).strip(),
            "winner_rank_points": str(r.get("winner_rank_points", "")).strip(),
            "loser_rank_points": str(r.get("loser_rank_points", "")).strip(),
        }

    @staticmethod
    def _parse_wta(r: pd.Series) -> dict | None:
        level = str(r.get("tourney_level", "")).strip()
        if level not in _WTA_MAIN:
            return None
        raw_d = str(r.get("tourney_date", "")).strip()
        try:
            d = datetime.strptime(raw_d, "%Y/%m/%d").date()
        except ValueError:
            return None
        score, status = _parse_score(r.get("score", ""))
        winner = str(r.get("winner_name", "")).strip()
        loser = str(r.get("loser_name", "")).strip()
        if not winner or not loser:
            return None
        return {
            "date": d.isoformat(), "_date": d, "tour": "WTA",
            "tournament": str(r.get("tourney_name", "")).strip(),
            "surface": str(r.get("surface", "")).strip().title() or "Hard",
            "indoor": "false",
            "round": str(r.get("round", "")).strip(),
            "best_of": str(r.get("best_of", "3")).strip() or "3",
            "winner": winner, "loser": loser,
            "score": score, "status": status, "_level": "main",
            "winner_rank": str(r.get("winner_rank", "")).strip(),
            "loser_rank": str(r.get("loser_rank", "")).strip(),
            "winner_rank_points": str(r.get("winner_rank_points", "")).strip(),
            "loser_rank_points": str(r.get("loser_rank_points", "")).strip(),
        }
