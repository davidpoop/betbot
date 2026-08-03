"""Tests críticos de la matemática de mercados: no-vig y puente de sets."""
import math
import random

import pytest

from betbot.markets.novig import all_methods, overround, power, proportional, shin
from betbot.markets.sets_bridge import (bridge_markets_bo3, dist_from_set_prob_bo3,
                                        match_prob_bo3, match_prob_bo5,
                                        reconcile_distribution, set_prob_from_match)


def test_novig_sums_to_one_all_methods():
    rng = random.Random(7)
    for _ in range(200):
        pa = rng.uniform(0.05, 0.90)
        margin = rng.uniform(0.0, 0.08)
        ia, ib = pa * (1 + margin), (1 - pa) * (1 + margin)
        if ia >= 0.98 or ib >= 0.98:
            continue  # cuota <=1.02: fuera de dominio realista
        oa, ob = 1 / ia, 1 / ib
        for name, p in all_methods(oa, ob).items():
            q = {"prop": proportional, "power": power, "shin": shin}[name](ob, oa)
            assert p + q == pytest.approx(1.0, abs=1e-8), f"{name} no suma 1"
            assert 0.0 < p < 1.0


def test_novig_roundtrip_fair_odds():
    # sin margen, los tres métodos devuelven la probabilidad implícita exacta
    oa, ob = 1 / 0.62, 1 / 0.38
    for p in all_methods(oa, ob).values():
        assert p == pytest.approx(0.62, abs=1e-9)


def test_novig_favorite_longshot_ordering():
    """Con margen, power y Shin descargan más margen en el longshot que la
    proporcional: p_power(fav) >= p_prop(fav) y p_shin(fav) >= p_prop(fav)."""
    oa, ob = 1.20, 5.50   # favorito claro con margen
    m = all_methods(oa, ob)
    assert m["power"] >= m["prop"] - 1e-12
    assert m["shin"] >= m["prop"] - 1e-12
    assert overround(oa, ob) > 0.01


def test_bridge_inversion_roundtrip():
    for m in [0.01, 0.1, 0.25, 0.5, 0.6667, 0.9, 0.99]:
        s = set_prob_from_match(m, 3)
        assert match_prob_bo3(s) == pytest.approx(m, abs=1e-9)
        s5 = set_prob_from_match(m, 5)
        assert match_prob_bo5(s5) == pytest.approx(m, abs=1e-9)


def test_bridge_identities():
    for m in [0.2, 0.5, 0.55, 0.8]:
        b = bridge_markets_bo3(m)
        assert b["p20"] + b["p21"] + b["p12"] + b["p02"] == pytest.approx(1.0, abs=1e-9)
        assert b["p20"] + b["p21"] == pytest.approx(m, abs=1e-9)       # P(2-0)+P(2-1) = m EXACTO
        assert b["wins_set_a"] == pytest.approx(1 - b["p02"], abs=1e-12)
        assert b["three_sets"] == pytest.approx(b["p21"] + b["p12"], abs=1e-12)
        # simetría: mercado de B con probabilidad 1-m
        c = bridge_markets_bo3(1 - m)
        assert c["p20"] == pytest.approx(b["p02"], abs=1e-9)
        assert c["p21"] == pytest.approx(b["p12"], abs=1e-9)


def test_bridge_monotonic():
    ms = [0.05 * i for i in range(1, 20)]
    ss = [set_prob_from_match(m, 3) for m in ms]
    assert all(s2 > s1 for s1, s2 in zip(ss, ss[1:]))


def test_bridge_known_values():
    # m=0.5 -> s=0.5 -> P20=0.25, P21=0.25, tres sets=0.5
    b = bridge_markets_bo3(0.5)
    assert b["set_prob"] == pytest.approx(0.5, abs=1e-9)
    assert b["p20"] == pytest.approx(0.25, abs=1e-9)
    assert b["three_sets"] == pytest.approx(0.5, abs=1e-9)
    # P(3 sets) = 2 s (1-s): máximo 0.5 en s=0.5
    for m in (0.3, 0.7, 0.9):
        assert bridge_markets_bo3(m)["three_sets"] < 0.5 + 1e-12


def test_bridge_set1_equals_set_prob_iid():
    b = bridge_markets_bo3(0.75)
    s = b["set_prob"]
    assert b["set1_a"] == pytest.approx(s, abs=1e-12)
    d = dist_from_set_prob_bo3(s)
    assert d.match_a == pytest.approx(0.75, abs=1e-9)


def test_reconcile_distribution_coherent():
    m = 0.72
    d = reconcile_distribution(m, p_straight_a=0.5, p_straight_b=0.1)
    total = d.p20 + d.p21 + d.p12 + d.p02
    assert total == pytest.approx(1.0, abs=1e-9)
    assert d.p20 + d.p21 == pytest.approx(m, abs=1e-9)
    assert min(d.p20, d.p21, d.p12, d.p02) >= 0.0
    # si el modelo directo dice straight > m (incoherente), se recorta
    d2 = reconcile_distribution(0.3, p_straight_a=0.9, p_straight_b=0.05)
    assert d2.p20 <= 0.3
    assert d2.p21 >= 0.0


def test_bo5_favorite_amplification():
    # el formato Bo5 amplifica al favorito: m5(s) > m3(s) para s>0.5
    for s in (0.55, 0.6, 0.7):
        assert match_prob_bo5(s) > match_prob_bo3(s)
    s = 0.5
    assert match_prob_bo5(s) == pytest.approx(0.5, abs=1e-9)
    assert match_prob_bo3(s) == pytest.approx(0.5, abs=1e-9)
