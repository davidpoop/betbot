"""Model Lab FASE 1: tests de leakage temporal y del contrato del laboratorio.

El corazón es la PROPIEDAD DE PREFIJO: construir el dataset con la historia
truncada en T produce filas idénticas para todo partido <= T. Cualquier feature
que use el futuro (resultado propio, ranking posterior, agregados con datos
futuros) rompe la igualdad y el test la detecta sin conocer la feature.
"""
import copy
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from betbot.config import load_config
from betbot.model_lab.calibration import fit_in_fold, make_calibrator
from betbot.model_lab.dataset import (FORBIDDEN_IN_X, build_dataset,
                                      prefix_property_ok)
from betbot.model_lab.metrics import block_bootstrap_diff, full_report
from betbot.model_lab.validation import (Fold, SealedTestGuard,
                                         assert_temporal_integrity, outer_scheme,
                                         run_fold, walk_forward_folds)

RNG = np.random.default_rng(7)


def _cfg(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["canonical_dir"] = str(tmp_path / "canonical")
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    (tmp_path / "canonical").mkdir(exist_ok=True)
    return cfg


def _matches(n=400, start=date(2019, 1, 7), players=None) -> pd.DataFrame:
    """Historial sintético plausible: fechas crecientes, resultados aleatorios."""
    players = players or [f"p{i:02d}" for i in range(16)]
    rows = []
    d = start
    for i in range(n):
        a, b = RNG.choice(players, size=2, replace=False)
        d = d + timedelta(days=int(RNG.integers(0, 3)))
        rows.append({
            "tour": "ATP", "date": d, "tournament": "T", "series": "ATP250",
            "surface": ["Hard", "Clay", "Grass"][int(RNG.integers(0, 3))],
            "indoor": False, "round": "2nd Round", "best_of": 3,
            "player_a": a, "player_b": b, "raw_name_a": a, "raw_name_b": b,
            "label_a_wins": int(RNG.integers(0, 2)), "status": "completed",
            "sets_a": 2, "sets_b": 1, "games_a": 18, "games_b": 15,
            "set1_winner_a": 1, "rank_a": float(RNG.integers(1, 200)),
            "rank_b": float(RNG.integers(1, 200)), "pts_a": 1000.0, "pts_b": 900.0,
            "odds_json": json.dumps({"PS": [1.8, 2.0]}), "source": "test",
            "match_id": f"ATP_{d.isoformat()}_{i:04d}_{a}__{b}",
        })
    return pd.DataFrame(rows)


def _players(matches: pd.DataFrame) -> pd.DataFrame:
    ids = sorted(set(matches["player_a"]) | set(matches["player_b"]))
    return pd.DataFrame([{"player_id": p, "tours": "ATP", "n_matches": 10,
                          "display_name": p, "hand": "R", "dob": date(1995, 5, 1)}
                         for p in ids])


# ------------------------------------------------------------ leakage temporal

def test_prefix_property_holds_on_clean_pipeline(tmp_path):
    """El pipeline real (Elo replay + builder del champion) debe superar la
    propiedad de prefijo en varios cortes: features idénticas con o sin futuro."""
    cfg = _cfg(tmp_path)
    m = _matches(400)
    p = _players(m)
    dates = sorted(m["date"].unique())
    for cutoff in (dates[len(dates) // 3], dates[2 * len(dates) // 3]):
        ok, why = prefix_property_ok(cfg, m, p, cutoff)
        assert ok, why


def test_prefix_property_detects_future_contamination(tmp_path):
    """Sanidad del detector: una feature contaminada con el futuro (win-rate de
    TODO el historial, incluido lo posterior) debe romper la propiedad."""
    cfg = _cfg(tmp_path)
    m = _matches(300)
    p = _players(m)
    import betbot.model_lab.dataset as dsmod
    orig = dsmod.build_dataset

    def poisoned(cfg2, matches=None, players=None, cutoff=None):
        ds = orig(cfg2, matches, players, cutoff)
        # win-rate del jugador sobre TODA la historia disponible en la
        # construcción (sin as-of): con historia completa ve el futuro, con la
        # truncada no -> las filas del prefijo dejan de coincidir
        src = matches if matches is not None else m
        if cutoff is not None:
            src = src[src["date"] <= cutoff]
        wr = src.groupby("player_a")["label_a_wins"].mean()
        ds.df["elo_diff"] = ds.df["elo_diff"] + ds.df["player_a"].map(wr).fillna(0.5)
        return ds

    dsmod.build_dataset = poisoned
    try:
        cutoff = sorted(m["date"].unique())[len(m["date"].unique()) // 2]
        ok, why = dsmod.prefix_property_ok(cfg, m, p, cutoff)
    finally:
        dsmod.build_dataset = orig
    assert not ok and "difiere" in why


def test_elo_row_is_strictly_prematch(tmp_path):
    """El Elo escrito en la fila i no incluye el resultado del partido i:
    dos partidos consecutivos del mismo par -> la fila 2 refleja SOLO el 1º."""
    cfg = _cfg(tmp_path)
    rows = []
    for i, d in enumerate([date(2020, 1, 6), date(2020, 1, 8), date(2020, 1, 10)]):
        rows.append({"tour": "ATP", "date": d, "tournament": "T", "series": "ATP250",
                     "surface": "Hard", "indoor": False, "round": "2nd Round",
                     "best_of": 3, "player_a": "x", "player_b": "y",
                     "raw_name_a": "x", "raw_name_b": "y", "label_a_wins": 1,
                     "status": "completed", "sets_a": 2, "sets_b": 0, "games_a": 12,
                     "games_b": 4, "set1_winner_a": 1, "rank_a": 10.0, "rank_b": 20.0,
                     "pts_a": 1000.0, "pts_b": 500.0, "odds_json": "{}",
                     "source": "t", "match_id": f"ATP_{d}_{i}_x__y"})
    m = pd.DataFrame(rows)
    ds = build_dataset(cfg, m, _players(m))
    diffs = ds.df["elo_diff"].to_list()
    assert diffs[0] == 0.0                     # ambos parten de 1500: aún sin información
    assert diffs[1] > 0.0                      # tras ganar el 1º, x por delante
    assert diffs[2] > diffs[1]                 # y tras ganar también el 2º, más aún


def test_activity_window_excludes_current_match(tmp_path):
    cfg = _cfg(tmp_path)
    rows = []
    for i, d in enumerate([date(2021, 3, 1), date(2021, 3, 3)]):
        rows.append({"tour": "ATP", "date": d, "tournament": "T", "series": "ATP250",
                     "surface": "Hard", "indoor": False, "round": "2nd Round",
                     "best_of": 3, "player_a": "x", "player_b": f"z{i}",
                     "raw_name_a": "x", "raw_name_b": f"z{i}", "label_a_wins": 1,
                     "status": "completed", "sets_a": 2, "sets_b": 0, "games_a": 12,
                     "games_b": 4, "set1_winner_a": 1, "rank_a": 10.0, "rank_b": 50.0,
                     "pts_a": 1000.0, "pts_b": 300.0, "odds_json": "{}",
                     "source": "t", "match_id": f"ATP_{d}_{i}"})
    ds = build_dataset(cfg, pd.DataFrame(rows), _players(pd.DataFrame(rows)))
    # m14_diff de la fila 1: x lleva 1 partido (el del día 1), z1 lleva 0.
    # Si la ventana incluyera el partido actual, x llevaría 2.
    assert ds.df["m14_diff"].iloc[1] == 1.0


def test_forbidden_columns_never_in_X(tmp_path):
    cfg = _cfg(tmp_path)
    m = _matches(60)
    ds = build_dataset(cfg, m, _players(m))
    with pytest.raises(ValueError, match="resultado"):
        ds.X(["elo_diff", "label_a_wins"])
    for c in FORBIDDEN_IN_X:
        assert c not in ds.feature_cols


def test_match_id_unique_no_mirror_rows(tmp_path):
    cfg = _cfg(tmp_path)
    m = _matches(200)
    ds = build_dataset(cfg, m, _players(m))
    assert ds.df["match_id"].is_unique


# ------------------------------------------------------------ validación

def test_walk_forward_folds_match_champion_scheme():
    cfg = load_config()
    folds = walk_forward_folds(cfg)
    assert folds[0].valid_year == 2013 and folds[-1].valid_year == 2022
    for f in folds:
        assert f.train_years[1] == f.valid_year - 1     # expanding, sin solape
    sch = outer_scheme(cfg)
    assert sch == {"dev_max_year": 2022, "valid_years": [2023, 2024], "test_year": 2025}


def test_temporal_integrity_raises_on_overlap():
    tr = pd.DataFrame({"date": [date(2020, 5, 1), date(2021, 1, 1)]})
    va = pd.DataFrame({"date": [date(2020, 12, 31)]})
    with pytest.raises(ValueError, match="Leakage temporal"):
        assert_temporal_integrity(tr, va)


def test_run_fold_slices_and_fits_only_on_train(tmp_path):
    cfg = _cfg(tmp_path)
    m = _matches(500, start=date(2018, 1, 8))       # ~500 días: cruza a 2019
    ds = build_dataset(cfg, m, _players(m))
    df = ds.labelled()
    assert df["year"].max() > 2018                  # el fold necesita dos años
    seen: dict = {}

    def fit(tr):
        seen["max_train_date"] = max(tr["date"])
        return "modelo"

    def predict(model, va):
        return np.full(len(va), 0.5)

    fold = Fold((2018, 2018), 2019)
    out = run_fold(df, fold, fit, predict, min_train_rows=10)
    assert out is not None
    va, p = out
    assert seen["max_train_date"] < min(va["date"])
    assert len(p) == len(va)


def test_sealed_test_guard(tmp_path):
    cfg = _cfg(tmp_path)
    df = pd.DataFrame({"year": [2024, 2025, 2026], "x": [1, 2, 3]})
    guard = SealedTestGuard(cfg)
    assert 2025 not in set(guard.filter(df)["year"])          # sellado por defecto
    with pytest.raises(ValueError, match="motivo"):
        guard.filter(df, unseal=True)
    out = guard.filter(df, unseal=True, reason="prueba unitaria")
    assert 2025 in set(out["year"])
    log = json.loads((guard.log_path).read_text())
    assert log["accesses"][0]["reason"] == "prueba unitaria"  # acceso registrado


# ------------------------------------------------------------ métricas/calibración

def test_metrics_known_values():
    y = np.array([1, 0, 1, 0])
    p = np.array([0.9, 0.1, 0.8, 0.2])
    rep = full_report(y, p)
    assert rep["acc"] == 1.0 and rep["brier"] == pytest.approx(0.025, abs=1e-9)
    assert rep["auc"] == 1.0 and rep["n"] == 4


def test_block_bootstrap_diff_detects_clear_winner():
    n = 600
    y = RNG.integers(0, 2, n).astype(float)
    p_good = np.clip(0.72 * y + 0.28 * (1 - y) + RNG.normal(0, 0.03, n), 0.02, 0.98)
    p_bad = np.full(n, 0.5)
    dates = pd.Series(pd.date_range("2024-01-01", periods=n, freq="6h"))
    out = block_bootstrap_diff(dates, y, p_good, p_bad, n_boot=300)
    assert out["diff"] < 0 and out["significativo"] is True
    assert out["ci95"][0] <= out["diff"] <= out["ci95"][1]


def test_calibrators_fit_in_fold_and_isotonic_available():
    p = np.clip(RNG.uniform(0.1, 0.9, 800), None, None)
    y = (RNG.uniform(size=800) < p).astype(float)
    cal, table = fit_in_fold(p, y)
    assert set(table) <= {"identity", "platt", "beta", "isotonic"}
    out = cal.transform(np.array([0.3, 0.7]))
    assert ((out > 0) & (out < 1)).all()
    iso = make_calibrator("isotonic").fit(p, y)
    assert iso.transform(np.array([0.5])).shape == (1,)


def test_lab_does_not_touch_production_scan():
    """El runtime productivo no importa el laboratorio: scan/screener/value no
    deben referenciar model_lab (nada llega a producción sin promoción)."""
    import pathlib
    src = pathlib.Path("src/betbot")
    for mod in ("scan.py", "screener/run.py", "value/engine.py", "picks.py",
                "models/train.py", "models/predictor.py"):
        assert "model_lab" not in (src / mod).read_text(), mod
