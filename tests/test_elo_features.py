"""Tests críticos de Elo (golden manual) y del feature builder (as-of, whitelist)."""
import math
from datetime import date

import pandas as pd
import pytest

from betbot.features.builder import FEATURES, assert_whitelist, build_features, derived_targets
from betbot.ratings.elo import EloState, replay

CFG_ELO = {"start_rating": 1500.0, "k_base": 250.0, "k_offset": 5.0, "k_shape": 0.4}
CFG = {"data": {"book_priority": ["PS", "B365", "Avg", "Max", "TD1"]},
       "selection": {"layoff_days_ood": 90}}


def _mk_matches(rows):
    base = {"series": "", "indoor": False, "round": "1st Round", "best_of": 3,
            "sets_a": 2, "sets_b": 0, "games_a": 12, "games_b": 6, "set1_winner_a": 1,
            "rank_a": 10, "rank_b": 20, "pts_a": 1000, "pts_b": 500, "odds_json": "{}",
            "status": "completed", "tournament": "T", "source": "test"}
    out = []
    for i, r in enumerate(rows):
        d = dict(base)
        d.update(r)
        d.setdefault("match_id", f"m{i}")
        out.append(d)
    return pd.DataFrame(out)


def test_elo_golden_hand_computed():
    """Primer partido: ambos 1500, K = 250/5^0.4 = 131.31...; el ganador sube
    exactamente K*0.5. Segundo partido del ganador contra un nuevo jugador:
    E = 1/(1+10^(-65.657.../400)) y K del ganador ya es 250/6^0.4."""
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
        {"tour": "ATP", "date": date(2010, 1, 8), "player_a": "a_x", "player_b": "c_z",
         "surface": "Hard", "label_a_wins": 1},
    ])
    df, states = replay(m, CFG_ELO)
    k1 = 250.0 / 5 ** 0.4
    assert df.loc[0, "elo_a"] == 1500.0 and df.loc[0, "elo_b"] == 1500.0
    exp_after = 1500.0 + k1 * 0.5
    assert df.loc[1, "elo_a"] == pytest.approx(exp_after, abs=1e-9)   # pre-partido 2 = post-partido 1
    assert df.loc[1, "elo_b"] == 1500.0
    e2 = 1.0 / (1.0 + 10 ** (-(exp_after - 1500.0) / 400.0))
    k2 = 250.0 / 6 ** 0.4
    final_a = exp_after + k2 * (1 - e2)
    st = states["ATP"]
    assert st.get("a_x") == pytest.approx(final_a, abs=1e-9)
    # el perdedor del partido 1 baja simétricamente
    assert st.get("b_y") == pytest.approx(1500.0 - k1 * 0.5, abs=1e-9)


def test_elo_walkover_no_update():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": None, "status": "walkover"},
        {"tour": "ATP", "date": date(2010, 1, 2), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
    ])
    df, _ = replay(m, CFG_ELO)
    assert df.loc[1, "elo_a"] == 1500.0 and df.loc[1, "elo_b"] == 1500.0


def test_elo_surface_independent():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Clay", "label_a_wins": 1},
        {"tour": "ATP", "date": date(2010, 2, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Grass", "label_a_wins": 1},
    ])
    df, _ = replay(m, CFG_ELO)
    # la hierba no ha visto partidos: elo_surf pre-partido 2 sigue 1500-1500
    assert df.loc[1, "elo_surf_a"] == 1500.0 and df.loc[1, "elo_surf_b"] == 1500.0
    assert df.loc[1, "elo_a"] > 1500.0 > df.loc[1, "elo_b"]


def test_tours_never_mix():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
        {"tour": "WTA", "date": date(2010, 1, 8), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
    ])
    df, _ = replay(m, CFG_ELO)
    assert df.loc[1, "elo_a"] == 1500.0  # misma clave, otro tour: estado independiente


def _players():
    return pd.DataFrame([{"player_id": p, "dob": None, "hand": None}
                         for p in ("a_x", "b_y", "c_z")])


def test_features_asof_rest_and_load():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
        {"tour": "ATP", "date": date(2010, 1, 11), "player_a": "a_x", "player_b": "c_z",
         "surface": "Hard", "label_a_wins": 1},
    ])
    elo_df, _ = replay(m, CFG_ELO)
    feats, state = build_features(elo_df, _players(), CFG)
    # partido 2: a_x descansó 10 días; c_z debuta (rest=30 cap)
    r2 = feats.iloc[1]
    assert r2["rest_diff"] == pytest.approx((10 - 30) / 7.0)
    assert r2["m14_diff"] == pytest.approx(1.0)      # a_x jugó 1 en 14d, c_z 0
    assert r2["experience_diff"] == pytest.approx(math.log1p(1) - math.log1p(0))
    assert state["last_date"]["ATP|a_x"] == "2010-01-11"


def test_features_asof_never_uses_current_match():
    """El partido actual no puede contar en su propia carga (m14 del primer
    partido de ambos = 0)."""
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1},
    ])
    elo_df, _ = replay(m, CFG_ELO)
    feats, _ = build_features(elo_df, _players(), CFG)
    assert feats.iloc[0]["m14_diff"] == 0.0
    assert feats.iloc[0]["elo_diff"] == 0.0


def test_market_prob_requires_both_sides():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1, "odds_json": '{"B365": [1.5, null]}'},
        {"tour": "ATP", "date": date(2010, 1, 2), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1, "odds_json": '{"B365": [1.5, 2.6]}'},
    ])
    elo_df, _ = replay(m, CFG_ELO)
    feats, _ = build_features(elo_df, _players(), CFG)
    assert not feats.iloc[0]["has_market"]           # falta la cuota contraria -> NO se inventa no-vig
    assert feats.iloc[1]["has_market"]
    ia, ib = 1 / 1.5, 1 / 2.6
    assert feats.iloc[1]["p_novig_a_prop"] == pytest.approx(ia / (ia + ib))


def test_whitelist_blocks_leaky_feature():
    with pytest.raises(ValueError, match="whitelist"):
        assert_whitelist(FEATURES + ["winner_rank_after_match"])


def test_derived_targets_bo3_only_and_retired_excluded():
    m = _mk_matches([
        {"tour": "ATP", "date": date(2010, 1, 1), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1, "sets_a": 2, "sets_b": 1},
        {"tour": "ATP", "date": date(2010, 1, 2), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1, "status": "retired", "sets_a": 1, "sets_b": 0},
        {"tour": "ATP", "date": date(2010, 1, 3), "player_a": "a_x", "player_b": "b_y",
         "surface": "Hard", "label_a_wins": 1, "best_of": 5, "sets_a": 3, "sets_b": 0},
    ])
    elo_df, _ = replay(m, CFG_ELO)
    feats, _ = build_features(elo_df, _players(), CFG)
    t = derived_targets(feats)
    assert t.iloc[0]["y_three_sets"] == 1.0 and t.iloc[0]["y_straight_a"] == 0.0
    assert pd.isna(t.iloc[1]["y_wins_set_a"])        # retirada: sin target de sets
    assert t.iloc[1]["y_set1_a"] == 1                # ...pero set1 completado vale
    assert pd.isna(t.iloc[2]["y_three_sets"])        # Bo5 excluido de derivados
