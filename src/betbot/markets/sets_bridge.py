"""Puente de sets: de la probabilidad de partido a la distribución de marcador.

Supuesto: sets i.i.d. con probabilidad s de que A gane un set cualquiera.
Bo3:  m = P(A gana) = s^2 + 2 s^2 (1-s) = s^2 (3 - 2s)
Bo5:  m = s^3 (10 - 15 s + 6 s^2)
Ambas son estrictamente crecientes en [0,1] -> inversión por bisección.

El puente es el BASELINE de los mercados de sets; los modelos directos y la
combinación OOF viven en models/derived.py. La recalibración empírica por
mercado corrige el sesgo del supuesto iid.
"""
from __future__ import annotations

from dataclasses import dataclass


def match_prob_bo3(s: float) -> float:
    return s * s * (3.0 - 2.0 * s)

def match_prob_bo5(s: float) -> float:
    return s ** 3 * (10.0 - 15.0 * s + 6.0 * s * s)


def _invert(fn, m: float, tol: float = 1e-12, max_iter: int = 200) -> float:
    if not (0.0 <= m <= 1.0):
        raise ValueError(f"probabilidad de partido invalida: {m}")
    if m == 0.0:
        return 0.0
    if m == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(max_iter):
        s = 0.5 * (lo + hi)
        v = fn(s)
        if abs(v - m) < tol:
            return s
        if v < m:
            lo = s
        else:
            hi = s
    return 0.5 * (lo + hi)


def set_prob_from_match(m: float, best_of: int = 3) -> float:
    """s tal que P(ganar partido | s) = m."""
    return _invert(match_prob_bo3 if best_of == 3 else match_prob_bo5, m)


@dataclass
class SetDistribution:
    """Distribución Bo3 de marcador de sets desde la perspectiva de A."""
    p20: float
    p21: float
    p12: float
    p02: float

    def as_dict(self) -> dict[str, float]:
        return {"2-0": self.p20, "2-1": self.p21, "1-2": self.p12, "0-2": self.p02}

    @property
    def match_a(self) -> float:
        return self.p20 + self.p21

    @property
    def set1_a(self) -> float:
        raise AttributeError("set1 se obtiene de bridge_markets_bo3, no de la distribución")

    @property
    def wins_set_a(self) -> float:
        return 1.0 - self.p02

    @property
    def wins_set_b(self) -> float:
        return 1.0 - self.p20

    @property
    def three_sets(self) -> float:
        return self.p21 + self.p12


def dist_from_set_prob_bo3(s: float) -> SetDistribution:
    return SetDistribution(
        p20=s * s,
        p21=2.0 * s * s * (1.0 - s),
        p12=2.0 * s * (1.0 - s) ** 2,
        p02=(1.0 - s) ** 2,
    )


def bridge_markets_bo3(m: float) -> dict[str, float]:
    """Todos los mercados de sets Bo3 derivados de la probabilidad de partido m
    (perspectiva de A). set1_a = s bajo el supuesto iid."""
    s = set_prob_from_match(m, best_of=3)
    d = dist_from_set_prob_bo3(s)
    return {
        "set_prob": s,
        "p20": d.p20, "p21": d.p21, "p12": d.p12, "p02": d.p02,
        "match_a": d.match_a,
        "set1_a": s,
        "wins_set_a": d.wins_set_a,
        "wins_set_b": d.wins_set_b,
        "straight_a": d.p20,
        "straight_b": d.p02,
        "three_sets": d.three_sets,
    }


def reconcile_distribution(m: float, p_straight_a: float, p_straight_b: float) -> SetDistribution:
    """Reconciliación final coherente anclada en la probabilidad de partido m:
      P20 = min(p_straight_a, m·(1-eps));  P21 = m - P20
      P02 = min(p_straight_b, (1-m)·(1-eps));  P12 = (1-m) - P02
    Garantiza: suma 1, P20+P21 = m, celdas no negativas."""
    eps = 1e-6
    p20 = min(max(p_straight_a, 0.0), m * (1.0 - eps))
    p02 = min(max(p_straight_b, 0.0), (1.0 - m) * (1.0 - eps))
    return SetDistribution(p20=p20, p21=m - p20, p12=(1.0 - m) - p02, p02=p02)
