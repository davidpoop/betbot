"""Tests de settlement del backtester y E2E del screener (con artefactos reales)."""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from betbot.backtest.run import _settle

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"


def _row(status="completed", label=1, sets_a=2, sets_b=0):
    return pd.Series({"status": status, "label_a_wins": label,
                      "sets_a": sets_a, "sets_b": sets_b})


def test_settle_completed_win_loss():
    pnl, res = _settle(_row(), "a", 1.80, "1_set")
    assert (pnl, res) == (pytest.approx(0.80), "win")
    pnl, res = _settle(_row(), "b", 2.50, "1_set")
    assert (pnl, res) == (-1.0, "loss")


def test_settle_walkover_void():
    pnl, res = _settle(_row(status="walkover"), "a", 1.8, "1_set")
    assert (pnl, res) == (0.0, "void")


def test_settle_retirement_rules():
    # retirada con 1 set completado: regla 1_set liquida, match_completed anula
    r = _row(status="retired", label=1, sets_a=1, sets_b=0)
    assert _settle(r, "a", 2.0, "1_set") == (pytest.approx(1.0), "win")
    assert _settle(r, "b", 2.0, "1_set") == (-1.0, "loss")
    assert _settle(r, "a", 2.0, "match_completed") == (0.0, "void")
    # retirada sin ningún set completado: void en ambas reglas
    r0 = _row(status="retired", label=1, sets_a=0, sets_b=0)
    assert _settle(r0, "a", 2.0, "1_set") == (0.0, "void")


@pytest.mark.skipif(not BUNDLE.exists(), reason="ejecutar antes 'betbot train'")
def test_e2e_screener_cli(tmp_path):
    """Pipeline completo por CLI: plantilla -> partidos+cuotas -> tabla + export."""
    matches = tmp_path / "m.csv"
    odds = tmp_path / "o.csv"
    out = tmp_path / "result.csv"
    matches.write_text(
        "date,tour,tournament,surface,indoor,round,best_of,player_a,player_b\n"
        "2026-08-04,ATP,Test Open,Hard,false,Quarterfinals,3,Alcaraz C.,Draper J.\n"
        "2026-08-04,ATP,Test Open,Hard,false,1st Round,3,Fantasma Z.,Alcaraz C.\n")
    odds.write_text(
        "player_a,player_b,market,selection,odds,bookmaker,timestamp\n"
        "Alcaraz C.,Draper J.,match_winner,a,1.50,book,\n"
        "Alcaraz C.,Draper J.,match_winner,b,2.60,book,\n"
        "Alcaraz C.,Draper J.,set1_winner,a,1.70,book,\n")
    r = subprocess.run([sys.executable, "-m", "betbot.cli", "screen",
                        "--matches", str(matches), "--odds", str(odds), "--out", str(out)],
                       capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    df = pd.read_csv(out)
    # descartada por jugador desconocido con reason code
    disc = df[df["state"] == "descartada"]
    assert len(disc) == 1 and "jugador_desconocido" in disc.iloc[0]["reasons"]
    # el partido real produce moneyline completo + set1 parcial experimental
    ml = df[(df["market"] == "match_winner") & (df["state"] != "descartada")]
    assert len(ml) == 2
    assert (ml["p_novig"].notna()).all()
    s1 = df[df["market"] == "set1_winner"]
    assert len(s1) == 1
    assert "mercado_parcial" in s1.iloc[0]["reasons"]
    # como maximo una recommended_primary por partido; con un favorito claro
    # el estado probable_sin_value debe emerger como primary
    assert int(df["recommended_primary"].sum()) == 1
    assert df[df["recommended_primary"]].iloc[0]["state"] in (
        "probable_sin_value", "vigilar_precio", "normal", "fuerte")
    # simetría de la moneyline mostrada: p_a + p_b = 1
    ps = ml["p_model"].to_numpy()
    assert abs(ps.sum() - 1.0) < 1e-3
    # distribución de sets exportada y coherente
    sd = ml.iloc[0]["set_distribution"]
    dist = json.loads(sd.replace("'", '"')) if isinstance(sd, str) else sd
    # los valores exportados van redondeados a 4 decimales: tolerancia acorde
    assert abs(sum(dist.values()) - 1.0) < 1e-3


@pytest.mark.skipif(not BUNDLE.exists(), reason="ejecutar antes 'betbot train'")
def test_e2e_no_odds_still_reports_probabilities(tmp_path):
    matches = tmp_path / "m.csv"
    matches.write_text(
        "date,tour,tournament,surface,indoor,round,best_of,player_a,player_b\n"
        "2026-08-04,WTA,Test Open,Clay,false,Semifinals,3,Swiatek I.,Gauff C.\n")
    r = subprocess.run([sys.executable, "-m", "betbot.cli", "screen",
                        "--matches", str(matches)],
                       capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "Swiatek" in r.stdout
    assert "sin_cuota" in r.stdout
