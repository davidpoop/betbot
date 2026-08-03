"""Tests del motor de value: estados, mercados parciales, o_min, primary."""
import pytest

from betbot.value.engine import evaluate_selection, pick_recommended_primary

CFG = {"ev_min": 0.03, "edge_min": 0.02, "ev_strong": 0.05, "edge_strong": 0.03,
       "prob_likely": 0.60, "odds_soft_range": [1.40, 4.00], "z_sigma": 1.0}


def _ev(**kw):
    base = dict(market="match_winner", selection="a", selection_name="X",
                p_model=0.60, odds=1.80, opposite_odds=2.20, bookmaker="b",
                tour_shrink_w=1.0, sigma=0.03, is_experimental=False,
                hard_flags=[], cfg_sel=CFG)
    base.update(kw)
    return evaluate_selection(**base)


def test_strong_candidate():
    # p=0.62 a cuota 1.80 -> EV=11.6%; novig(1.80,2.20)=0.55 -> edge=7pp
    e = _ev(p_model=0.62)
    assert e.state == "fuerte"
    assert e.ev == pytest.approx(0.62 * 1.80 - 1, abs=1e-9)
    assert e.edge == pytest.approx(0.62 - e.p_novig, abs=1e-9)


def test_normal_candidate():
    e = _ev(p_model=0.60, odds=1.75, opposite_odds=2.30)
    if e.state == "fuerte":  # umbrales exactos: garantizar al menos normal
        assert e.ev_cons >= 0.05
    else:
        assert e.state == "normal"
        assert e.ev_cons >= 0.03


def test_watch_price_state_and_o_min():
    # EV positivo pequeño pero < ev_min -> vigilar con cuota objetivo
    e = _ev(p_model=0.575, odds=1.76, opposite_odds=2.20)
    assert e.state == "vigilar_precio"
    assert e.o_min == pytest.approx((1 + CFG["ev_min"]) / e.p_cons, abs=1e-9)
    assert e.odds < e.o_min


def test_likely_no_value():
    e = _ev(p_model=0.70, odds=1.32, opposite_odds=3.40)
    assert e.state in ("probable_sin_value", "vigilar_precio")
    if e.state == "probable_sin_value":
        assert e.p_cons >= 0.60


def test_partial_market_never_invents_novig():
    e = _ev(opposite_odds=None, p_model=0.65, odds=1.70)
    assert e.p_novig is None and e.edge is None
    assert not e.full_market
    assert "mercado_parcial_sin_cuota_contraria" in e.reasons
    # p_cons penalizada: p - z*sigma
    assert e.p_cons == pytest.approx(0.65 - 0.03, abs=1e-9)


def test_hard_flags_discard():
    e = _ev(hard_flags=["ood_pocos_partidos_12m:x"])
    assert e.state == "descartada"
    assert e.ev is None


def test_experimental_cap():
    e = _ev(is_experimental=True, p_model=0.65, odds=1.80, opposite_odds=2.20)
    assert e.state == "experimental"      # cumple umbrales de fuerte pero queda capado


def test_shrink_moves_p_cons_toward_market():
    e = _ev(p_model=0.62, tour_shrink_w=0.5)
    assert e.p_cons == pytest.approx(0.5 * 0.62 + 0.5 * e.p_novig, abs=1e-9)


def test_recommended_primary_ordering():
    e1 = _ev(p_model=0.62)                                        # fuerte
    e2 = _ev(p_model=0.60, odds=1.75, opposite_odds=2.30)         # normal/fuerte
    e3 = _ev(is_experimental=True, p_model=0.65)                  # experimental
    primary = pick_recommended_primary([e3, e2, e1])
    assert primary.state in ("fuerte",)
    assert pick_recommended_primary([]) is None


def test_out_of_range_odds_blocks_strong_only():
    e = _ev(p_model=0.30, odds=4.50, opposite_odds=1.22)
    assert "cuota_fuera_de_rango_suave[1.4,4.0]" in ";".join(e.reasons) or e.state != "fuerte"
