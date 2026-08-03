"""Tests críticos de modelos: simetría exacta, calibradores, reproducibilidad."""
import numpy as np
import pandas as pd
import pytest

from betbot.features.builder import CTX_FEATURES, DIFF_FEATURES, FEATURES, MARKET_FEATURE
from betbot.models.calibrate import (BetaCalibrator, PlattCalibrator, log_loss_safe,
                                     select_calibrator)
from betbot.models.predictor import (PerspectiveModel, SymmetricEventModel, SymmetricModel,
                                     swap_matrix)


def _fake_df(n=600, seed=3):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(size=n) for f in DIFF_FEATURES})
    for f in CTX_FEATURES:
        df[f] = rng.integers(0, 2, size=n).astype(float)
    df[MARKET_FEATURE] = rng.normal(size=n)
    logit = 1.2 * df["elo_diff"] + 0.8 * df[MARKET_FEATURE] + 0.3 * df["log_rank_ratio"]
    df["y"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    return df


def _swapped(df):
    out = df.copy()
    for f in DIFF_FEATURES + [MARKET_FEATURE]:
        out[f] = -out[f]
    return out


def test_symmetric_model_exact_symmetry():
    df = _fake_df()
    m = SymmetricModel(FEATURES + [MARKET_FEATURE]).fit(df, df["y"].to_numpy())
    p = m.predict(df)
    p_sw = m.predict(_swapped(df))
    assert np.allclose(p + p_sw, 1.0, atol=1e-12), "p(A,B) + p(B,A) debe ser exactamente 1"
    assert 0.55 < ((p > 0.5) == (df["y"] == 1)).mean() < 1.0


def test_perspective_model_swap_consistency():
    df = _fake_df()
    y_a = df["y"].to_numpy()
    y_b = 1 - y_a
    m = PerspectiveModel(FEATURES + [MARKET_FEATURE]).fit(df, y_a, y_b)
    pa = m.predict(df, "a")
    pb_on_swapped = m.predict(_swapped(df), "b")
    # ver el partido con bandos intercambiados y pedir la perspectiva B
    # equivale a la perspectiva A original
    assert np.allclose(pa, pb_on_swapped, atol=1e-12)


def test_symmetric_event_model_invariance():
    df = _fake_df()
    y = df["y"].to_numpy()
    m = SymmetricEventModel(FEATURES + [MARKET_FEATURE]).fit(df, y)
    assert np.allclose(m.predict(df), m.predict(_swapped(df)), atol=1e-12)


def test_swap_matrix_only_flips_antisymmetric():
    df = _fake_df(10)
    feats = FEATURES + [MARKET_FEATURE]
    X = df[feats].to_numpy(dtype=float)
    Xs = swap_matrix(X, feats)
    for i, f in enumerate(feats):
        if f in DIFF_FEATURES or f == MARKET_FEATURE:
            assert np.allclose(Xs[:, i], -X[:, i])
        else:
            assert np.allclose(Xs[:, i], X[:, i])


def test_whitelist_enforced_at_model_construction():
    with pytest.raises(ValueError, match="whitelist"):
        SymmetricModel(["elo_diff", "future_result_leak"])


def test_calibrators_improve_or_match_biased_probs():
    rng = np.random.default_rng(11)
    y = (rng.random(4000) < 0.5).astype(float)
    p_true = np.where(y == 1, rng.beta(5, 3, 4000), rng.beta(3, 5, 4000))
    p_biased = np.clip(p_true * 0.6 + 0.05, 0.01, 0.99)   # sesgo fuerte
    ll_raw = log_loss_safe(y, p_biased)
    for cal in (PlattCalibrator(), BetaCalibrator()):
        ll = log_loss_safe(y, cal.fit(p_biased, y).transform(p_biased))
        assert ll < ll_raw - 0.01, f"{cal.name} no corrige el sesgo"
    best, table = select_calibrator(p_biased, y)
    assert table[best.name] == min(table.values())


def test_training_reproducible_same_seed():
    df = _fake_df(800, seed=9)
    y = df["y"].to_numpy()
    m1 = SymmetricModel(FEATURES).fit(df, y)
    m2 = SymmetricModel(FEATURES).fit(df, y)
    assert np.allclose(m1.lr.coef_, m2.lr.coef_, atol=1e-10)
    assert np.allclose(m1.predict(df), m2.predict(df), atol=1e-12)
