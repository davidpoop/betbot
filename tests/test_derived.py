"""Tests del módulo de mercados derivados (coherencia y combinación)."""
import numpy as np
import pytest

from betbot.models.derived import (best_weight, bridge_prob, combine, final_set_markets)


def test_bridge_prob_vectorized_matches_scalar():
    ms = np.array([0.2, 0.5, 0.8])
    v = bridge_prob("straight", ms)
    for i, m in enumerate(ms):
        assert v[i] == pytest.approx(bridge_prob("straight", float(m)), abs=1e-12)


def test_final_set_markets_coherence():
    out = final_set_markets(m=0.70, p_set1=0.66, p_wins_a=0.85, p_wins_b=0.55,
                            p_straight_a=0.48, p_straight_b=0.12)
    d = np.array([out["p20"], out["p21"], out["p12"], out["p02"]])
    assert d.sum() == pytest.approx(1.0, abs=1e-9)
    assert (d >= 0).all()
    assert out["p20"] + out["p21"] == pytest.approx(0.70, abs=1e-9)      # ancla en m
    assert out["straight_a"] <= out["match_a"] + 1e-12                     # P(2-0) <= P(ganar)
    assert out["wins_set_a"] >= out["match_a"] - 1e-12                     # P(>=1 set) >= P(ganar)
    assert out["wins_set_a"] == pytest.approx(1 - out["p02"], abs=1e-12)
    assert out["wins_set_b"] == pytest.approx(1 - out["p20"], abs=1e-12)
    assert out["three_sets"] == pytest.approx(out["p21"] + out["p12"], abs=1e-12)


def test_final_set_markets_clips_incoherent_direct_models():
    # el modelo directo dice P(2-0)=0.9 pero P(ganar)=0.3: se recorta a <= m
    out = final_set_markets(m=0.30, p_set1=0.4, p_wins_a=0.2, p_wins_b=0.5,
                            p_straight_a=0.90, p_straight_b=0.05)
    assert out["straight_a"] <= 0.30
    assert out["wins_set_a"] >= 0.30
    assert out["p21"] >= 0.0 and out["p12"] >= 0.0


def test_best_weight_prefers_better_component():
    rng = np.random.default_rng(5)
    y = (rng.random(3000) < 0.6).astype(float)
    p_good = np.clip(0.6 + rng.normal(0, 0.05, 3000), 0.01, 0.99)
    p_bad = np.clip(0.5 + rng.normal(0, 0.2, 3000), 0.01, 0.99)
    w, lls = best_weight(y, p_direct=p_good, p_bridge=p_bad)
    assert w >= 0.75                     # el peso cae del lado del componente bueno
    assert lls["ll_combo"] <= min(lls["ll_bridge"], lls["ll_direct"]) + 1e-9


def test_combine_clips():
    p = combine(np.array([0.0]), np.array([1.0]), 0.5)
    assert 0.0 < p[0] < 1.0
