"""Resultados recientes ATP desde The Odds API `/v4/sports/{sport}/scores/`.

VEREDICTO DE GRANULARIDAD (verificado ANTES de integrar, spec OpenAPI oficial
+ sample oficial the-odds-api/apps-script + doc v4): la respuesta trae, por
evento, `scores: [{name, score}]` — UN string agregado por participante, SIN
ningún campo de desglose por set. El esquema NO PUEDE transportar "6-4,3-6,6-2".

Consecuencia (CASO B del mandato): este adaptador NO fabrica resultados
canónicos a partir de agregados. Solo produce filas si el payload real trae
marcador por sets reconstruible (dos formatos aceptados, ver
`_sets_from_scores`); con agregados devuelve 0 filas y lo dice en el status.
Un agregado jamás pasa además `prepare_rows` (exige el marcador completo del
ganador), doble cinturón verificado por test.

Si algún día produce filas (CASO A), llevan `_src="oddsapi_scores"` y la
fuente declara `coverage="partial"`: cubre SOLO los torneos con sport key
activa (Slams/1000/500 grandes), así que sus filas NUNCA avanzan la frescura
GLOBAL del circuito — solo la cobertura activa por torneo (ver sync.py).

Cuota: /sports = 0 créditos; /scores con daysFrom (1..3) = 2 créditos por
sport key. Con TTL de caché y el tope `max_sports`, un sync diario consume
~2-8 créditos. La clave va SOLO en variables de entorno y las URLs se citan
siempre redactadas (cache.redact).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone

from betbot.feeds import cache
from betbot.feeds.base import SourceStatus

BASE = "https://api.the-odds-api.com/v4"

_SET_TOKENS_RX = re.compile(r"^\s*\d{1,2}-\d{1,2}(?:\(\d+\))?(?:[ ,]+\d{1,2}-\d{1,2}(?:\(\d+\))?)+\s*$")
_GAMES_LIST_RX = re.compile(r"^\s*\d{1,2}(?:\s*,\s*\d{1,2})+\s*$")


def _tournament_from_sport(sport_key: str, sport_title: str) -> str:
    """'tennis_atp_canadian_open' / 'ATP Canadian Open' -> 'Canadian Open'."""
    t = str(sport_title or "").strip()
    if t.upper().startswith(("ATP ", "WTA ")):
        return t[4:].strip()
    if t:
        return t
    return sport_key.replace("tennis_atp_", "").replace("tennis_wta_", "").replace("_", " ").title()


def _sets_from_scores(scores: list) -> tuple[list[tuple[int, int]], str]:
    """Intenta reconstruir el marcador POR SETS desde `scores` (orientación
    home/away = primer/segundo participante del array).

    Formatos aceptados como marcador completo:
      A) un participante lleva tokens de set: "6-4 3-6 6-2" o "6-4,3-6,6-2"
         (pares juegos_home-juegos_away);
      B) cada participante lleva su lista de juegos por set: "6,3,6" y "4,6,2"
         (misma longitud).
    Cualquier otra cosa — en particular el AGREGADO documentado ('2', '20') —
    devuelve ([], motivo) y el partido NO se convierte en resultado."""
    if not isinstance(scores, list) or len(scores) != 2:
        return [], "scores ausente o sin dos participantes"
    s0 = str(scores[0].get("score", "") or "").strip()
    s1 = str(scores[1].get("score", "") or "").strip()

    def _tokens(cand: str) -> list[tuple[int, int]] | None:
        if not _SET_TOKENS_RX.match(cand):
            return None
        out = []
        for tok in re.split(r"[ ,]+", cand.strip()):
            m = re.match(r"^(\d{1,2})-(\d{1,2})", tok)
            if not m:
                return None
            out.append((int(m.group(1)), int(m.group(2))))
        return out

    # formato A: UN participante lleva los tokens de set completos
    if s0 and (sets := _tokens(s0)) is not None:
        return sets, ""
    if s1 and (sets := _tokens(s1)) is not None:
        return [(b, a) for a, b in sets], ""     # tokens en away: reorientar a home
    if not s0 or not s1:
        return [], "score vacío"
    # formato B: cada participante con su lista de juegos por set
    if _GAMES_LIST_RX.match(s0) and _GAMES_LIST_RX.match(s1):
        g0 = [int(x) for x in re.split(r"\s*,\s*", s0.strip())]
        g1 = [int(x) for x in re.split(r"\s*,\s*", s1.strip())]
        if len(g0) == len(g1) and len(g0) >= 2:
            return list(zip(g0, g1)), ""
        return [], "listas de juegos de longitud distinta"
    return [], f"agregado sin desglose por sets (scores={s0!r}/{s1!r})"


def _sets_won(sets: list[tuple[int, int]]) -> tuple[int, int]:
    w = l = 0
    for a, b in sets:
        hi, lo = max(a, b), min(a, b)
        if (hi >= 6 and hi - lo >= 2) or hi == 7:
            if a > b:
                w += 1
            else:
                l += 1
    return w, l


class OddsApiScores:
    """ResultsSource de The Odds API /scores. Solo ATP (WTA mantiene
    wta_official como primaria, por mandato)."""
    name = "oddsapi_scores"
    supported_tours = frozenset({"ATP"})
    coverage = "partial"          # solo torneos con sport key activa, jamás todo el circuito

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 900,
                 max_sports: int = 4) -> None:
        self.key = api_key or os.environ.get("BETBOT_ODDS_API_KEY", "")
        self.ttl = ttl_seconds
        self.max_sports = max_sports

    def _tennis_atp_keys(self) -> list[tuple[str, str]]:
        sports = cache.get_json(f"{BASE}/sports/?apiKey={self.key}", ttl_seconds=3600)
        return [(s["key"], str(s.get("title", ""))) for s in sports
                if str(s.get("key", "")).startswith("tennis_atp") and s.get("active")]

    def fetch_results(self, since: date, until: date) -> tuple[list[dict], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.key:
            st.error = "sin BETBOT_ODDS_API_KEY (adaptador inactivo)"
            return [], st
        today = datetime.now(timezone.utc).date()
        days_back = max(1, min(3, (today - since).days or 1))
        try:
            keys = self._tennis_atp_keys()
        except Exception as exc:  # noqa: BLE001
            st.error = cache.redact(str(exc))
            return [], st
        if len(keys) > self.max_sports:
            st.notes.append(f"{len(keys)} sport keys ATP activas; se consultan "
                            f"{self.max_sports} (tope de créditos)")
            keys = keys[: self.max_sports]
        rows: list[dict] = []
        n_completed = n_agg = n_bad = 0
        errors: list[str] = []
        for skey, title in keys:
            try:
                events = cache.get_json(
                    f"{BASE}/sports/{skey}/scores/?daysFrom={days_back}&apiKey={self.key}",
                    ttl_seconds=self.ttl)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{skey}: {cache.redact(str(exc))}")
                continue
            for ev in events if isinstance(events, list) else []:
                if not ev.get("completed"):
                    continue                    # live/upcoming JAMÁS entran como completed
                row, why = self._row_from_event(ev, skey, title, since, until)
                if why == "fuera_de_ventana":
                    continue
                n_completed += 1
                if row is not None:
                    rows.append(row)
                elif why == "agregado":
                    n_agg += 1
                else:
                    n_bad += 1
        st.ok = not errors or bool(rows) or n_completed > 0
        st.error = "; ".join(errors[:3])
        st.n_items = len(rows)
        st.notes.append(f"{len(keys)} torneos consultados (daysFrom={days_back}; "
                        f"~{2 * len(keys)} créditos si no había caché)")
        st.notes.append(f"{n_completed} finalizados observados; {len(rows)} con marcador "
                        f"por sets utilizable; {n_agg} solo agregado (CASO B: no se "
                        f"inventa un 6-x); {n_bad} descartados por payload incompleto")
        if n_agg and not rows:
            st.notes.append("granularidad insuficiente para resultados canónicos: "
                            "el feed publica un único score agregado por jugador")
        st.notes.append("cobertura PARCIAL (solo torneos con sport key): sus filas "
                        "no avanzan la frescura global ATP")
        return rows, st

    def _row_from_event(self, ev: dict, sport_key: str, sport_title: str,
                        since: date, until: date) -> tuple[dict | None, str]:
        try:
            d = datetime.fromisoformat(str(ev.get("commence_time", ""))
                                       .replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except ValueError:
            return None, "sin fecha"
        if not (since <= d <= until):
            return None, "fuera_de_ventana"
        home = str(ev.get("home_team", "") or "").strip()
        away = str(ev.get("away_team", "") or "").strip()
        if not home or not away:
            return None, "participantes ausentes"
        sets, why = _sets_from_scores(ev.get("scores"))
        if not sets:
            return None, ("agregado" if "agregado" in why else why)
        w, l = _sets_won(sets)
        tournament = _tournament_from_sport(sport_key, sport_title)
        from betbot.tournaments import resolve_surface
        surf, _src, info = resolve_surface("ATP", tournament)
        best_of = 5 if (info.get("level") == "grand_slam" or len(sets) >= 4) else 3
        need = best_of // 2 + 1
        if max(w, l) < need or w == l:
            return None, "marcador por sets incompleto para completed (no se inventa)"
        home_won = w > l
        win_sets = sets if home_won else [(b, a) for a, b in sets]
        return {
            "date": d.isoformat(), "tour": "ATP", "tournament": tournament,
            "surface": surf or "", "indoor": "false", "round": "",
            "best_of": str(best_of),
            "winner": home if home_won else away,
            "loser": away if home_won else home,
            "score": " ".join(f"{a}-{b}" for a, b in win_sets),
            "status": "completed",
            "_level": "main", "_src": self.name,
        }, ""

    def completed_events(self, since: date, until: date) -> list[dict]:
        """Evidencia live/completed para diagnóstico (id, fecha, jugadores) —
        independiente de la granularidad del marcador. NO alimenta canonical
        y hoy tampoco la puerta prematch: para eventos del calendario oddsapi
        sería corroboración de la MISMA familia de proveedor (prohibida por
        anti-circularidad), y `ya_comenzado` ya se cubre con commence_time."""
        rows, _ = self.fetch_results(since, until)
        out = [{"date": r["date"], "winner": r["winner"], "loser": r["loser"]} for r in rows]
        return out
