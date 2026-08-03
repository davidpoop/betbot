"""Mercados de sets: puente (baseline), modelos directos ligeros y combinación OOF.

Mercados Bo3: set1 (simétrico-complementario), wins_set (por jugador),
straight (por jugador), three (evento simétrico). La salida final se
reconcilia en una distribución de marcador coherente anclada en la
probabilidad de partido (ver markets/sets_bridge.reconcile_distribution).
"""
from __future__ import annotations

import numpy as np

from betbot.markets.sets_bridge import bridge_markets_bo3, reconcile_distribution

DERIVED_MARKETS = ["set1", "wins_set", "straight", "three"]

# clave -> nombre del target en la tabla de features
TARGET_COL = {"set1": "y_set1_a", "wins_set": "y_wins_set_a",
              "straight": "y_straight_a", "three": "y_three_sets"}


def bridge_prob(market: str, m: np.ndarray | float) -> np.ndarray:
    """Probabilidad del puente para el lado A (o el evento) dado p(partido)=m."""
    scalar = np.isscalar(m)
    ms = np.atleast_1d(np.asarray(m, dtype=float))
    out = np.empty_like(ms)
    for i, mi in enumerate(ms):
        b = bridge_markets_bo3(float(np.clip(mi, 1e-6, 1 - 1e-6)))
        out[i] = {"set1": b["set1_a"], "wins_set": b["wins_set_a"],
                  "straight": b["straight_a"], "three": b["three_sets"]}[market]
    return float(out[0]) if scalar else out


def combine(p_direct: np.ndarray, p_bridge: np.ndarray, w_direct: float) -> np.ndarray:
    return np.clip(w_direct * p_direct + (1.0 - w_direct) * p_bridge, 1e-6, 1 - 1e-6)


def best_weight(y: np.ndarray, p_direct: np.ndarray, p_bridge: np.ndarray,
                grid: np.ndarray | None = None) -> tuple[float, dict[str, float]]:
    """Peso de la combinación elegido por log loss OOF."""
    from betbot.models.calibrate import log_loss_safe
    if grid is None:
        grid = np.linspace(0.0, 1.0, 21)
    lls = {float(w): log_loss_safe(y, combine(p_direct, p_bridge, w)) for w in grid}
    w_best = min(lls, key=lls.get)
    return w_best, {"ll_bridge": lls[0.0], "ll_direct": lls[1.0], "ll_combo": lls[w_best]}


def final_set_markets(m: float, p_set1: float, p_wins_a: float, p_wins_b: float,
                      p_straight_a: float, p_straight_b: float) -> dict[str, float]:
    """Coherencia final de los mercados de sets Bo3 (perspectiva A):
    - P(gana >=1 set) >= P(gana partido); P(2-0) <= P(gana partido)
    - distribución de marcador reconciliada anclada en m
    - three_sets se lee de la distribución (coherente por construcción)."""
    m = float(np.clip(m, 1e-6, 1 - 1e-6))
    p_wins_a = max(p_wins_a, m)
    p_wins_b = max(p_wins_b, 1.0 - m)
    p_straight_a = min(p_straight_a, m, p_wins_a)
    p_straight_b = min(p_straight_b, 1.0 - m, p_wins_b)
    # coherencia dura: P(B gana >=1 set) = 1 - P(2-0 de A) y viceversa
    p_straight_a = min(p_straight_a, 1.0 - 1e-6)
    dist = reconcile_distribution(m, p_straight_a, p_straight_b)
    return {
        "match_a": m,
        "set1_a": float(np.clip(p_set1, 1e-6, 1 - 1e-6)),
        "wins_set_a": 1.0 - dist.p02,
        "wins_set_b": 1.0 - dist.p20,
        "straight_a": dist.p20,
        "straight_b": dist.p02,
        "three_sets": dist.three_sets,
        "p20": dist.p20, "p21": dist.p21, "p12": dist.p12, "p02": dist.p02,
    }
