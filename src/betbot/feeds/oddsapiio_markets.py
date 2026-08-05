"""Clasificación de mercados CRUDOS de Odds-API.io a los contratos de BetBot.

Regla dura del encargo: *sin adivinar*. Un mercado solo se declara mapeado
cuando el nombre crudo Y las selecciones observadas son consistentes con el
contrato. Si el nombre encaja con una familia pero las selecciones lo
contradicen, el resultado es `ambiguo` — nunca soportado. Todo lo demás queda
como `no_mapeado`, con su nombre crudo a la vista.

El caso ambiguo típico del tenis es "Correct Score": puede ser el marcador por
SETS (2-0, 2-1...) o el marcador en juegos del primer set (6-4, 7-5...). Solo
el primero es `set_score`; el segundo no tiene contrato en BetBot y se informa
como no mapeado en vez de forzarlo.
"""
from __future__ import annotations

import re
import unicodedata

CANONICAL = ("match_winner", "set1_winner", "wins_set", "straight_sets",
             "three_sets", "set_score")

_SET_SCORE_RX = re.compile(r"\b([0-3])\s*[-:]\s*([0-3])\b")
_GAME_SCORE_RX = re.compile(r"\b([4-7])\s*[-:]\s*([0-7])\b")
_OVER_UNDER_RX = re.compile(r"\b(over|under|más|mas|menos|\+|-)\s*2[.,]5\b", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _is_player_like(name: str, p1: str, p2: str) -> bool:
    n, a, b = _norm(name), _norm(p1), _norm(p2)
    if not n:
        return False
    if n in ("1", "2", "home", "away", "local", "visitante"):
        return True
    for full in (a, b):
        if not full:
            continue
        if n == full or n in full or full in n:
            return True
        for tok in full.split():
            if len(tok) >= 4 and tok in n:
                return True
    return False


def _yes_no(names: list[str]) -> bool:
    s = {_norm(x) for x in names}
    return bool(s) and s <= {"yes", "no", "si", "sí", "y", "n"}


def classify_market(market_raw: str, outcomes: list[dict], p1: str = "",
                    p2: str = "") -> tuple[str, str, str]:
    """(canonico, veredicto, motivo). veredicto ∈ mapeado|ambiguo|no_mapeado."""
    m = _norm(market_raw)
    names = [str(o.get("name", "")) for o in outcomes]
    nnames = [_norm(x) for x in names]
    joined = " ".join(nnames)
    has_set_scores = any(_SET_SCORE_RX.search(x) for x in nnames)
    has_game_scores = any(_GAME_SCORE_RX.search(x) for x in nnames)
    player_like = sum(1 for x in names if _is_player_like(x, p1, p2))

    if not m:
        return "", "no_mapeado", "mercado sin nombre"

    first_set = bool(re.search(r"\b(1st set|first set|set 1|primer set|set1)\b", m))
    mentions_set = "set" in m
    mentions_game = bool(re.search(r"\bgames?\b|\bjuegos?\b", m))

    # ---- marcador exacto por sets --------------------------------------
    if re.search(r"\bset betting\b|\bset score\b|\bmarcador de sets\b", m) or \
            (re.search(r"\bcorrect score\b|\bmarcador exacto\b", m) and mentions_set):
        if has_set_scores and not has_game_scores:
            return "set_score", "mapeado", "nombre de marcador por sets + selecciones 2-0/2-1"
        if has_game_scores:
            return "", "ambiguo", ("nombre de marcador pero selecciones con marcador de "
                                   "JUEGOS (6-4...): no es set_score")
        return "", "ambiguo", "nombre de marcador sin selecciones reconocibles por sets"
    if re.search(r"\bcorrect score\b|\bmarcador exacto\b", m):
        if has_set_scores and not has_game_scores:
            return "set_score", "mapeado", "selecciones de marcador por sets"
        return "", "ambiguo", ("'correct score' sin indicar sets: podría ser marcador "
                               "de juegos del primer set")

    # ---- ganador del primer set ----------------------------------------
    if first_set:
        if re.search(r"winner|ganador|home/away|1x2|moneyline|to win", m) or player_like == 2:
            if player_like == 2:
                return "set1_winner", "mapeado", "primer set + dos selecciones de jugador"
            return "", "ambiguo", "nombre de ganador de primer set sin selecciones de jugador"
        if _OVER_UNDER_RX.search(m) or mentions_game:
            return "", "no_mapeado", "mercado de juegos del primer set (sin contrato en BetBot)"
        return "", "ambiguo", f"mercado de primer set no reconocido: {market_raw}"

    # ---- total de sets (three_sets) ------------------------------------
    if re.search(r"\btotal sets\b|\bnumber of sets\b|\bsets? o/u\b|\btotal de sets\b|"
                 r"\bnumero de sets\b", m) or (mentions_set and _OVER_UNDER_RX.search(m)):
        over_under = any(re.search(r"\bover|under|mas|menos|\+|-", x) for x in nnames)
        two_three = any(re.fullmatch(r"[23](\.0)?( sets?)?", x) for x in nnames)
        if over_under or two_three:
            return "three_sets", "mapeado", "total de sets con selecciones over/under o 2/3"
        return "", "ambiguo", "total de sets sin selecciones reconocibles"
    if re.search(r"\b(match|partido) (goes to|llega a) (3|three|tres)\b", m):
        return "three_sets", "mapeado", "'el partido llega al tercer set'"

    # ---- gana un set / sets corridos ------------------------------------
    if re.search(r"\bto win a set\b|\bwin a set\b|\bganar un set\b|\bat least one set\b|"
                 r"\bgana(r)? al menos un set\b", m):
        if _yes_no(names) or player_like >= 1:
            return "wins_set", "mapeado", "'gana un set' con selecciones sí/no o de jugador"
        return "", "ambiguo", "'gana un set' sin selecciones reconocibles"
    if re.search(r"\bstraight sets\b|\bsets corridos\b|\bto nil\b|\bwhitewash\b", m):
        if player_like >= 1 or _yes_no(names):
            return "straight_sets", "mapeado", "'sets corridos'"
        return "", "ambiguo", "'sets corridos' sin selecciones reconocibles"
    # hándicap de sets: +1.5 == gana un set ; -1.5 == 2-0
    if mentions_set and re.search(r"handicap|hcp|spread", m):
        if any("+1.5" in x or "+1,5" in x for x in nnames):
            return "wins_set", "mapeado", "hándicap de sets +1.5 (equivale a ganar un set)"
        if any("-1.5" in x or "-1,5" in x for x in nnames):
            return "straight_sets", "mapeado", "hándicap de sets -1.5 (equivale a 2-0)"
        return "", "ambiguo", "hándicap de sets sin línea +1.5/-1.5 identificable"

    # ---- ganador del partido -------------------------------------------
    if re.search(r"\bmatch winner\b|\bmoneyline\b|\bmatch result\b|\bhome/away\b|\b1x2\b|"
                 r"\bto win match\b|\bganador del partido\b|\bwinner\b|\bganador\b", m) \
            and not mentions_set and not mentions_game:
        if player_like == 2:
            return "match_winner", "mapeado", "ganador del partido con dos jugadores"
        return "", "ambiguo", "nombre de ganador sin dos selecciones de jugador"

    return "", "no_mapeado", f"sin contrato conocido: {market_raw}"


def classify_all(markets: list[dict], p1: str = "", p2: str = "") -> list[dict]:
    """Añade a cada mercado crudo su clasificación, sin descartar nada."""
    out = []
    for mk in markets:
        canon, verdict, why = classify_market(mk.get("market_raw", ""),
                                              mk.get("outcomes", []), p1, p2)
        out.append({**mk, "canonical": canon, "verdict": verdict, "why": why})
    return out


def supported_summary(classified: list[dict]) -> dict:
    """Un mercado SOLO cuenta como soportado si apareció realmente y con
    cuota utilizable (>1.0) en alguna respuesta."""
    supported: dict[str, dict] = {}
    for mk in classified:
        if mk["verdict"] != "mapeado" or not mk["canonical"]:
            continue
        prices = [o.get("odds") for o in mk["outcomes"]
                  if isinstance(o.get("odds"), (int, float)) and o["odds"] > 1.0]
        if not prices:
            continue
        e = supported.setdefault(mk["canonical"], {"n_observado": 0, "bookmakers": set(),
                                                   "ejemplos": []})
        e["n_observado"] += 1
        if mk.get("bookmaker"):
            e["bookmakers"].add(mk["bookmaker"])
        if len(e["ejemplos"]) < 3:
            e["ejemplos"].append(f"{mk['market_raw']} -> {prices[:4]}")
    return {k: {**v, "bookmakers": sorted(v["bookmakers"])} for k, v in supported.items()}
