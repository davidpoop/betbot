"""Tests del dataset canónico REAL construido (integridad, dedupe, orientación)."""
import json
from pathlib import Path

import pandas as pd
import pytest

CANON = Path(__file__).resolve().parents[1] / "data" / "canonical" / "matches.parquet"

pytestmark = pytest.mark.skipif(not CANON.exists(), reason="ejecutar antes 'betbot prepare-data'")


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return pd.read_parquet(CANON)


def test_no_duplicate_match_ids(df):
    assert df["match_id"].is_unique


def test_orientation_invariants(df):
    assert (df["player_a"] < df["player_b"]).all()
    comp = df[df["status"] == "completed"]
    # el label coincide con los sets: si a ganó, sets_a > sets_b
    won = comp[comp["label_a_wins"] == 1]
    lost = comp[comp["label_a_wins"] == 0]
    assert (won["sets_a"] >= won["sets_b"]).mean() > 0.995
    assert (lost["sets_b"] >= lost["sets_a"]).mean() > 0.995


def test_walkovers_have_no_label(df):
    wo = df[df["status"] == "walkover"]
    assert wo["label_a_wins"].isna().all()


def test_temporal_coverage(df):
    years = pd.to_datetime(df["date"]).dt.year
    assert years.min() == 2000 and years.max() >= 2025
    per_year = df.groupby(years).size()
    # todos los años (salvo 2020 COVID y el año en curso) con volumen razonable
    for y in range(2008, 2025):
        if y == 2020:
            continue
        assert per_year.get(y, 0) > 3000, f"año {y} con solo {per_year.get(y, 0)} partidos"


def test_odds_orientation_consistency(df):
    """En mercados con ambas cuotas, el ganador debe tener cuota media menor
    (sanity global: el favorito gana mas veces que el underdog)."""
    comp = df[(df["status"] == "completed") & (df["odds_json"] != "{}")].sample(8000, random_state=1)
    fav_wins, n = 0, 0
    for _, r in comp.iterrows():
        odds = json.loads(r["odds_json"])
        for bk in ("PS", "B365", "TD1", "Avg"):
            if bk in odds and odds[bk][0] and odds[bk][1]:
                oa, ob = odds[bk]
                if abs(oa - ob) < 1e-9:
                    break
                fav_a = oa < ob
                if (r["label_a_wins"] == 1) == fav_a:
                    fav_wins += 1
                n += 1
                break
    assert n > 5000
    assert 0.60 < fav_wins / n < 0.75, f"tasa favorito={fav_wins / n:.3f} fuera de rango plausible"


def test_retired_share_plausible(df):
    share = (df["status"] == "retired").mean()
    assert 0.015 < share < 0.05, f"retiradas {share:.3f} fuera del rango documentado"


def test_set1_winner_consistency(df):
    comp = df[(df["status"] == "completed") & df["set1_winner_a"].notna()]
    # quien gana el set 1 gana el partido ~80% en Bo3 (literatura)
    bo3 = comp[comp["best_of"] == 3]
    agree = (bo3["set1_winner_a"] == bo3["label_a_wins"]).mean()
    assert 0.75 < agree < 0.85, f"P(ganar partido | ganar set1)={agree:.3f}"
