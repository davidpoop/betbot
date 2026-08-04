"""Proveedores estructurados de cuotas multi-mercado (sets) — solo lectura.

`CanonicalPrice` conserva TODOS los metadatos del precio real: proveedor,
operador, ids de evento/mercado, tipo crudo y canónico, runner crudo y
selección canónica, mejor cuota back DISPONIBLE con su tamaño, timestamp y
estado del mercado. Nunca se inventa un precio (ni midpoint ni last) cuando
existe un back real; si el mercado no está en el catálogo del evento se
registra `not_offered`, jamás un error ni una cuota a cero.

El mapeo de mercados es DINÁMICO: se consulta el catálogo real por evento y
`map_market` interpreta cada marketType/runners observado. No hay una lista
cerrada asumida de marketType.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol

from betbot.canonical.names import canonical_key
from betbot.feeds.base import FeedMatch, SourceStatus

# mercados canónicos que el motor de valor puede cotizar hoy
QUOTABLE = ("match_winner", "set1_winner", "wins_set", "straight_sets", "three_sets")


@dataclass
class CanonicalPrice:
    provider: str                 # p.ej. "betfair"
    bookmaker: str                # operador/exchange concreto ("betfair_es")
    event_id: str
    market_id: str
    market_type_raw: str          # marketType tal cual lo publica la fuente
    market_canonical: str         # match_winner|set1_winner|wins_set|straight_sets|three_sets|set_score|unmapped
    runner_raw: str               # nombre crudo del runner
    selection: str                # "a"|"b"|"yes"|"no"|"2-0"...|"" si no mapeable
    back_odds: float | None       # mejor back DISPONIBLE (None si no hay)
    back_size: float | None       # tamaño disponible a esa cuota
    timestamp: str                # observación UTC ISO
    status: str = "open"          # open|suspended|closed
    in_play: bool = False
    player_a: str = ""            # jugadores del partido enlazado (orden del feed)
    player_b: str = ""

    @property
    def quotable(self) -> bool:
        return (self.market_canonical in QUOTABLE and self.selection in
                ("a", "b", "yes", "no") and self.back_odds is not None
                and self.back_odds > 1.0 and self.status == "open"
                and not self.in_play)


@dataclass
class EventMarkets:
    """Catálogo real de un evento enlazado: qué mercados publica la fuente."""
    event_id: str
    event_name: str
    player_a: str
    player_b: str
    market_types_raw: list[str] = field(default_factory=list)
    canonical_offered: list[str] = field(default_factory=list)
    not_offered: list[str] = field(default_factory=list)   # canónicos ausentes del catálogo


class StructuredTennisOddsProvider(Protocol):
    """Adaptador genérico: catálogo dinámico + precios con metadatos completos."""
    name: str

    def active(self) -> bool: ...

    def list_markets(self, matches: list[FeedMatch]) -> tuple[list[EventMarkets], SourceStatus]: ...

    def fetch_prices(self, matches: list[FeedMatch]) -> tuple[list[CanonicalPrice], SourceStatus]: ...


# ---------------------------------------------------------------------------
# Mapeo dinámico marketType/runners -> mercado canónico
# ---------------------------------------------------------------------------

_SCORE_RX = re.compile(r"\b([0-3])\s*-\s*([0-3])\b")
_SET_N_RX = re.compile(r"(?:set\s*(\d)|(\d)(?:st|nd|rd|th)\s*set|first\s*set|segundo?\s*set)", re.I)


def _which_player(text: str, p1: str, p2: str) -> str | None:
    """'a' si el texto se refiere al jugador 1 del feed, 'b' si al 2."""
    k = canonical_key(text)
    k1, k2 = canonical_key(p1), canonical_key(p2)
    if not k:
        return None
    if k == k1 or (k1 and (k1 in k or k in k1)):
        return "a"
    if k == k2 or (k2 and (k2 in k or k in k2)):
        return "b"
    # apellido contenido (runner "Fritz" vs feed "Fritz T.")
    s1, s2 = k1.rsplit("_", 1)[0], k2.rsplit("_", 1)[0]
    if s1 and s1 in k:
        return "a"
    if s2 and s2 in k:
        return "b"
    return None


def _set_number(market_name: str) -> int | None:
    m = _SET_N_RX.search(market_name or "")
    if not m:
        return None
    if m.group(1) or m.group(2):
        return int(m.group(1) or m.group(2))
    return 1 if "first" in market_name.lower() else None


def map_market(market_type: str, market_name: str, runners: list[str],
               p1: str, p2: str, best_of: int = 3) -> dict[str, tuple[str, str]]:
    """Mapea un mercado REAL del catálogo a contratos canónicos por runner.

    Devuelve {runner_raw: (market_canonical, selection)}. Runner no mapeable ->
    ("unmapped", ""). Verificaciones de contrato aplicadas (Bo3):
    - straight_sets(x) ≡ x gana 2-0 ≡ 'x NO cede set' ≡ NO de win_a_set del rival
    - wins_set(x) ≡ x +1.5 sets ≡ YES de x win a set
    - three_sets(yes) ≡ el partido alcanza el 3er set ≡ NUMBER_OF_SETS = 3
    """
    mt = (market_type or "").upper().strip()
    name = market_name or ""
    out: dict[str, tuple[str, str]] = {}

    def put(r: str, mk: str, sel: str) -> None:
        out[r] = (mk, sel)

    if mt == "MATCH_ODDS":
        for r in runners:
            side = _which_player(r, p1, p2)
            put(r, "match_winner", side or "")
            if side is None:
                out[r] = ("unmapped", "")
        return out

    if mt == "SET_WINNER":
        n = _set_number(name) or _set_number(mt)
        for r in runners:
            if n != 1:
                put(r, "unmapped", "")   # solo set 1 tiene contrato canónico hoy
                continue
            side = _which_player(r, p1, p2)
            put(r, "set1_winner", side or "")
            if side is None:
                out[r] = ("unmapped", "")
        return out

    if mt in ("PLAYER_A_WIN_A_SET", "PLAYER_B_WIN_A_SET") and best_of == 3:
        subject = "a" if mt == "PLAYER_A_WIN_A_SET" else "b"
        other = "b" if subject == "a" else "a"
        for r in runners:
            rl = r.strip().lower()
            if rl == "yes":
                put(r, "wins_set", subject)          # x gana >=1 set == +1.5
            elif rl == "no":
                put(r, "straight_sets", other)       # x no gana set == rival 2-0
            else:
                put(r, "unmapped", "")
        return out

    if mt in ("NUMBER_OF_SETS", "TOTAL_SETS") and best_of == 3:
        for r in runners:
            digits = re.findall(r"\d", r)
            if digits and digits[0] == "3":
                put(r, "three_sets", "yes")           # se juega el 3er set
            elif digits and digits[0] == "2":
                put(r, "three_sets", "no")
            else:
                put(r, "unmapped", "")
        return out

    if mt in ("SET_BETTING", "SET_CORRECT_SCORE", "CORRECT_SCORE") and best_of == 3:
        # runners "Fritz 2-0", "Jodar 2-1" o "2-0"/"0-2" (perspectiva jugador 1)
        for r in runners:
            m = _SCORE_RX.search(r)
            if not m:
                put(r, "unmapped", "")
                continue
            w, l = int(m.group(1)), int(m.group(2))
            side = _which_player(_SCORE_RX.sub("", r), p1, p2)
            if side is None:
                # sin nombre: el marcador va en perspectiva del jugador 1
                side, hi, lo = ("a", w, l) if w >= l else ("b", l, w)
            else:
                hi, lo = max(w, l), min(w, l)
            if (hi, lo) == (2, 0):
                put(r, "straight_sets", side)         # x 2-0 == sets corridos
            elif (hi, lo) == (2, 1):
                put(r, "set_score", f"{side}:2-1")    # metadato; sin cuota en el motor
            else:
                put(r, "unmapped", "")
        return out

    return {r: ("unmapped", "") for r in runners}


def summarize_catalogue(event_id: str, event_name: str, p1: str, p2: str,
                        markets: list[dict], best_of: int = 3) -> EventMarkets:
    """Resume el catálogo real de un evento: tipos crudos, canónicos ofrecidos
    y canónicos NO ofrecidos (not_offered) frente a los mercados soportados."""
    em = EventMarkets(event_id=event_id, event_name=event_name,
                      player_a=p1, player_b=p2)
    offered: set[str] = set()
    for mkt in markets:
        mt = str(mkt.get("marketType") or mkt.get("market_type") or "")
        em.market_types_raw.append(mt)
        mapped = map_market(mt, str(mkt.get("marketName") or ""),
                            [str(r) for r in mkt.get("runnerNames", [])], p1, p2, best_of)
        offered.update(mc for mc, _sel in mapped.values() if mc in QUOTABLE)
    em.canonical_offered = sorted(offered)
    em.not_offered = [m for m in QUOTABLE if m not in offered]
    return em


def to_odds_quotes(prices: list[CanonicalPrice]) -> list[dict]:
    """Convierte precios cotizables a filas OddsQuote (metadatos aparte)."""
    rows = []
    for p in prices:
        if not p.quotable:
            continue
        rows.append({"player_a": p.player_a, "player_b": p.player_b,
                     "market": p.market_canonical, "selection": p.selection,
                     "odds": float(p.back_odds), "bookmaker": p.bookmaker,
                     "timestamp": p.timestamp})
    return rows
