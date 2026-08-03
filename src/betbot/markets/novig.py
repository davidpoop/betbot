"""Eliminación del margen (no-vig) para mercados binarios: proporcional, power y Shin.

Entradas: cuotas decimales (oa, ob) ambas > 1. Salida: probabilidad del lado A.
Nunca inventa probabilidad si falta un lado (el llamante debe comprobarlo).
"""
from __future__ import annotations

import math


def implied(oa: float, ob: float) -> tuple[float, float, float]:
    if oa <= 1.0 or ob <= 1.0:
        raise ValueError(f"cuotas decimales invalidas para no-vig: ({oa}, {ob})")
    ia, ib = 1.0 / oa, 1.0 / ob
    return ia, ib, ia + ib


def proportional(oa: float, ob: float) -> float:
    ia, ib, s = implied(oa, ob)
    return ia / s


def power(oa: float, ob: float, tol: float = 1e-12, max_iter: int = 200) -> float:
    """Busca k tal que (1/oa)^k + (1/ob)^k = 1 (k>=1 si hay margen)."""
    ia, ib, s = implied(oa, ob)
    if abs(s - 1.0) < 1e-12:
        return ia
    lo, hi = 1e-6, 100.0
    for _ in range(max_iter):
        k = 0.5 * (lo + hi)
        val = ia ** k + ib ** k
        if abs(val - 1.0) < tol:
            break
        # val decrece con k
        if val > 1.0:
            lo = k
        else:
            hi = k
    return ia ** k / (ia ** k + ib ** k)


def shin(oa: float, ob: float, tol: float = 1e-12, max_iter: int = 200) -> float:
    """Método de Shin para binarios: busca z (proporción de insiders) tal que
    las probabilidades sumen 1. Formula estándar:
      p_i = ( sqrt(z^2 + 4(1-z) * pi_i^2 / S) - z ) / (2(1-z))
    con pi_i = 1/o_i y S = pi_a + pi_b.
    """
    ia, ib, s = implied(oa, ob)
    if abs(s - 1.0) < 1e-12:
        return ia

    def probs(z: float) -> tuple[float, float]:
        den = 2.0 * (1.0 - z)
        pa = (math.sqrt(z * z + 4.0 * (1.0 - z) * (ia * ia) / s) - z) / den
        pb = (math.sqrt(z * z + 4.0 * (1.0 - z) * (ib * ib) / s) - z) / den
        return pa, pb

    lo, hi = 0.0, 0.5
    z = 0.0
    for _ in range(max_iter):
        z = 0.5 * (lo + hi)
        pa, pb = probs(z)
        val = pa + pb
        if abs(val - 1.0) < tol:
            break
        # la suma decrece al aumentar z
        if val > 1.0:
            lo = z
        else:
            hi = z
    pa, pb = probs(z)
    return pa / (pa + pb)


def all_methods(oa: float, ob: float) -> dict[str, float]:
    return {"prop": proportional(oa, ob), "power": power(oa, ob), "shin": shin(oa, ob)}


def overround(oa: float, ob: float) -> float:
    return 1.0 / oa + 1.0 / ob - 1.0
