"""Tests de rankings as-of, paper trading (settlement prospectivo + CLV) y lógica de UI."""
import copy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.ingest.rankings import load_rankings, write_rankings_templates
from betbot.ui import logic

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical" / "matches.parquet"


# ---------------- rankings ----------------

def test_rankings_asof_and_future_guard(tmp_path):
    p = tmp_path / "rankings_atp.csv"
    p.write_text(
        "date_published,rank,player,points\n"
        "2026-07-27,1,Sinner J.,11830\n"
        "2026-07-27,2,Carlos Alcaraz,9860\n"      # formato nombre completo
        "2026-08-10,1,FUTURO X.,99999\n")          # fila futura: debe ignorarse
    rk = load_rankings(tmp_path, "ATP", ref_date=date(2026, 8, 3))
    assert rk.published == date(2026, 7, 27)
    assert rk.lookup("sinner_j") == (1, 11830.0)
    assert rk.lookup("alcaraz_c") == (2, 9860.0)   # nombre completo convertido
    assert rk.lookup("futuro_x") is None
    assert any("futura" in w for w in rk.warnings)


def test_rankings_all_future_falls_back(tmp_path):
    p = tmp_path / "rankings_wta.csv"
    p.write_text("date_published,rank,player,points\n2027-01-01,1,Swiatek I.,9000\n")
    rk = load_rankings(tmp_path, "WTA", ref_date=date(2026, 8, 3))
    assert rk.published is None and rk.by_player == {}


def test_rankings_stale_warning(tmp_path):
    p = tmp_path / "rankings_atp.csv"
    p.write_text("date_published,rank,player,points\n2026-01-01,1,Sinner J.,11000\n")
    rk = load_rankings(tmp_path, "ATP", ref_date=date(2026, 8, 3), max_age_days=45)
    assert rk.published == date(2026, 1, 1)
    assert any("dias" in w for w in rk.warnings)


def test_rankings_missing_file_fallback(tmp_path):
    rk = load_rankings(tmp_path, "ATP", ref_date=date(2026, 8, 3))
    assert rk.published is None
    assert any("fallback" in w for w in rk.warnings)


def test_rankings_templates_written(tmp_path):
    paths = write_rankings_templates(tmp_path)
    assert all(p.exists() for p in paths)


# ---------------- paper trading ----------------

@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    return cfg


def _base_pick(**kw):
    d = {"match": "Alcaraz C. vs Draper J.", "player_a": "Alcaraz C.", "player_b": "Draper J.",
         "date": "2026-03-01", "tour": "ATP", "tournament": "T", "market": "match_winner",
         "selection": "a", "selection_name": "Alcaraz C.", "odds": 2.0, "bookmaker": "b",
         "p_model": 0.55, "p_novig": 0.52, "ev_cons": 0.04, "state": "normal"}
    d.update(kw)
    return d


def test_paper_add_close_clv_and_manual_settle(cfg_tmp):
    from betbot import paper
    pid = paper.add_pick(cfg_tmp, _base_pick())
    df = paper.load_picks(cfg_tmp)
    assert len(df) == 1 and df.iloc[0]["status"] == "open"
    rec = paper.set_close(cfg_tmp, pid, close_odds=1.8, close_opposite_odds=2.2)
    assert rec["clv_odds_pct"] == pytest.approx((2.0 / 1.8 - 1) * 100, abs=1e-3)
    ic, io = 1 / 1.8, 1 / 2.2
    assert rec["clv_prob_pp"] == pytest.approx((ic / (ic + io) - 0.52) * 100, abs=1e-3)
    rec2 = paper.settle_manual(cfg_tmp, pid, "win")
    assert rec2["pnl"] == pytest.approx(1.0)
    m = paper.metrics(cfg_tmp)
    assert m["n_settled"] == 1 and m["pnl_units"] == pytest.approx(1.0)
    assert m["prospective"] is True
    assert "warning" in m           # muestra pequeña


def test_paper_close_without_opposite_no_prob_clv(cfg_tmp):
    from betbot import paper
    pid = paper.add_pick(cfg_tmp, _base_pick())
    rec = paper.set_close(cfg_tmp, pid, close_odds=1.9)
    assert rec["clv_odds_pct"] is not None
    assert pd.isna(rec["clv_prob_pp"]) or rec["clv_prob_pp"] is None   # no se inventa


def test_paper_rejects_bad_pick(cfg_tmp):
    from betbot import paper
    with pytest.raises(ValueError):
        paper.add_pick(cfg_tmp, _base_pick(odds=1.0))
    with pytest.raises(ValueError):
        paper.add_pick(cfg_tmp, _base_pick(market="parlay"))


@pytest.mark.skipif(not CANON.exists(), reason="necesita canonico real")
def test_paper_settle_from_canonical_real_match(cfg_tmp):
    """Settlement prospectivo contra un partido REAL del canónico."""
    from betbot import paper
    m = pd.read_parquet(CANON)
    row = m[(m["tour"] == "ATP") & (m["status"] == "completed")
            & (pd.to_datetime(m["date"]).dt.year == 2026)].iloc[-1]
    winner_key = row["player_a"] if row["label_a_wins"] == 1 else row["player_b"]
    loser_key = row["player_b"] if row["label_a_wins"] == 1 else row["player_a"]
    win_name = row["raw_name_a"] if row["label_a_wins"] == 1 else row["raw_name_b"]
    lose_name = row["raw_name_b"] if row["label_a_wins"] == 1 else row["raw_name_a"]
    # pick al ganador (usuario pone al ganador como player_a -> seleccion a)
    pid_w = paper.add_pick(cfg_tmp, _base_pick(
        player_a=win_name, player_b=lose_name, date=str(row["date"]), odds=1.8))
    # pick al perdedor
    pid_l = paper.add_pick(cfg_tmp, _base_pick(
        player_a=win_name, player_b=lose_name, date=str(row["date"]),
        selection="b", selection_name=lose_name, odds=2.4))
    res = paper.settle_from_canonical(cfg_tmp)
    assert res["settled"] == 2
    df = paper.load_picks(cfg_tmp).set_index("pick_id")
    assert df.loc[pid_w, "result"] == "win" and df.loc[pid_w, "pnl"] == pytest.approx(0.8)
    assert df.loc[pid_l, "result"] == "loss" and df.loc[pid_l, "pnl"] == pytest.approx(-1.0)


def test_paper_determined_outcomes_retirement():
    from betbot.paper import _determined_outcome
    r = pd.Series({"status": "retired", "label_a_wins": 1, "sets_a": 1, "sets_b": 0,
                   "set1_winner_a": 1, "best_of": 3})
    # moneyline regla 1_set: se liquida; match_completed: void
    assert _determined_outcome("match_winner", "a", False, r, "1_set") == ("win", 1)
    assert _determined_outcome("match_winner", "a", False, r, "match_completed") == ("void", 0)
    # wins_set del que ya ganó un set: determinado incluso con retirada
    assert _determined_outcome("wins_set", "a", False, r, "1_set") == ("win", 1)
    # wins_set del rival sin sets: no determinable -> void
    assert _determined_outcome("wins_set", "b", False, r, "1_set") == ("void", 0)
    # straight del que retiró sin perder set del lider: void
    assert _determined_outcome("straight_sets", "a", False, r, "1_set") == ("void", 0)
    # three_sets con retirada temprana: void
    assert _determined_outcome("three_sets", "yes", False, r, "1_set") == ("void", 0)


# ---------------- lógica de UI ----------------

def _results_df():
    return pd.DataFrame([
        {"match": "A vs B", "tour": "ATP", "surface": "Hard", "market": "match_winner",
         "selection_name": "A", "bookmaker": "b1", "odds": 1.5, "p_model": 0.7,
         "fair_odds": 1.43, "p_novig": 0.66, "edge": 0.04, "ev": 0.05, "ev_cons": 0.04,
         "o_min": 1.47, "state": "normal", "recommended_primary": True, "reasons": ""},
        {"match": "A vs B", "tour": "ATP", "surface": "Hard", "market": "straight_sets",
         "selection_name": "A", "bookmaker": "b1", "odds": 2.4, "p_model": 0.45,
         "fair_odds": 2.2, "p_novig": None, "edge": None, "ev": 0.08, "ev_cons": 0.05,
         "o_min": 2.3, "state": "experimental", "recommended_primary": False, "reasons": ""},
        {"match": "C vs D", "tour": "WTA", "surface": "Clay", "market": "match_winner",
         "selection_name": "C", "bookmaker": "b2", "odds": None, "p_model": 0.65,
         "fair_odds": 1.54, "p_novig": None, "edge": None, "ev": None, "ev_cons": None,
         "o_min": 1.6, "state": "probable_sin_value", "recommended_primary": True, "reasons": "sin_cuota"},
        {"match": "E vs F", "tour": "ATP", "surface": "Grass", "market": "match_winner",
         "selection_name": "E vs F", "bookmaker": "", "odds": None, "p_model": None,
         "fair_odds": None, "p_novig": None, "edge": None, "ev": None, "ev_cons": None,
         "o_min": None, "state": "descartada", "recommended_primary": False,
         "reasons": "jugador_desconocido:X"},
    ])


def test_ui_filters():
    df = _results_df()
    assert len(logic.filter_results(df, tours=["ATP"])) == 3
    assert len(logic.filter_results(df, surfaces=["Clay"])) == 1
    assert len(logic.filter_results(df, states=["descartada"])) == 1
    # el rango de cuota no oculta filas sin cuota
    out = logic.filter_results(df, odds_range=(1.4, 2.0))
    assert len(out) == 3 and (out["market"] == "straight_sets").sum() == 0


def test_ui_sort_and_followable():
    df = logic.sort_results(_results_df())
    assert df.iloc[0]["state"] in ("normal", "probable_sin_value")   # primaries primero
    fol = logic.followable(_results_df())
    assert len(fol) == 2                    # normal + experimental (con cuota)
    assert "descartada" not in set(fol["state"])
    lbl = logic.row_label(fol.iloc[0])
    assert "match_winner" in lbl and "@" in lbl


def test_ui_match_detail():
    det = logic.match_detail(_results_df(), "A vs B")
    assert len(det["rows"]) == 2
    assert len(det["primary"]) == 1
