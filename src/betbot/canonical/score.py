"""Parseo de marcadores.

Dos vías: columnas numéricas W1..W5/L1..L5 (Tennis-Data) o string "6-4 2-6 7-6"
orientado al primer jugador (formato Kaggle daily). Produce sets/juegos por
bando, ganador del set 1 y si el set 1 se completó.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

_SET_RE = re.compile(r"^(\d+)-(\d+)")


@dataclass
class ParsedScore:
    sets_w: int = 0          # sets del ganador (o del jugador de referencia)
    sets_l: int = 0
    games_w: int = 0
    games_l: int = 0
    set1_w: int | None = None   # 1 si el jugador de referencia ganó el set 1
    set1_completed: bool = False
    n_sets: int = 0


def _set_completed(gw: int, gl: int) -> bool:
    """Un set está completado si alguien llegó a 6+ con margen 2, o hubo tie-break (7-6/6-7),
    o formatos antiguos/largos (p.ej. 8-6, 70-68 en Isner-Mahut)."""
    hi, lo = max(gw, gl), min(gw, gl)
    if hi < 6:
        return False
    if hi == 6:
        return lo <= 4
    if hi == 7:
        return lo in (5, 6)
    return hi - lo >= 2  # sets largos sin tie-break


def parse_numeric_sets(pairs: list[tuple[float | None, float | None]]) -> ParsedScore:
    """pairs = [(W1,L1),(W2,L2),...] con NaN/None donde no hay set."""
    out = ParsedScore()
    for i, (gw, gl) in enumerate(pairs):
        if gw is None or gl is None:
            break
        try:
            if isinstance(gw, float) and math.isnan(gw):
                break
            if isinstance(gl, float) and math.isnan(gl):
                break
        except TypeError:
            pass
        gw_i, gl_i = int(gw), int(gl)
        out.n_sets += 1
        out.games_w += gw_i
        out.games_l += gl_i
        completed = _set_completed(gw_i, gl_i)
        if completed:
            if gw_i > gl_i:
                out.sets_w += 1
            elif gl_i > gw_i:
                out.sets_l += 1
        if i == 0:
            out.set1_completed = completed
            if completed:
                out.set1_w = 1 if gw_i > gl_i else 0
    return out


def parse_score_string(score: str) -> ParsedScore:
    """Parses "6-4 2-6 7-6" (orientado al jugador de referencia). Ignora
    anotaciones de tie-break "7-6(5)" y sufijos tipo 'ret'/'RET'."""
    out = ParsedScore()
    if not score or not isinstance(score, str):
        return out
    tokens = score.replace("(", " (").split()
    idx = 0
    for tok in tokens:
        m = _SET_RE.match(tok.strip())
        if not m:
            continue
        gw_i, gl_i = int(m.group(1)), int(m.group(2))
        if gw_i > 50 and gl_i > 50:  # artefactos
            continue
        out.n_sets += 1
        out.games_w += gw_i
        out.games_l += gl_i
        completed = _set_completed(gw_i, gl_i)
        if completed:
            if gw_i > gl_i:
                out.sets_w += 1
            elif gl_i > gw_i:
                out.sets_l += 1
        if idx == 0:
            out.set1_completed = completed
            if completed:
                out.set1_w = 1 if gw_i > gl_i else 0
        idx += 1
    return out


def infer_retired(ps: ParsedScore, best_of: int) -> bool:
    """True si el marcador no alcanza los sets necesarios (heurística para
    fuentes sin columna de estado)."""
    need = best_of // 2 + 1
    return max(ps.sets_w, ps.sets_l) < need
