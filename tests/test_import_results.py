"""Tests de la importación manual de resultados (validación, cuarentena,
append-only, prevención de futuro y replay de Elo)."""
import copy
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from betbot.canonical.store import load_matches
from betbot.config import load_config
from betbot.ingest.manual_results import import_results
from betbot.ratings.elo import replay

TODAY = date(2026, 8, 3)
CFG_ELO = {"start_rating": 1500.0, "k_base": 250.0, "k_offset": 5.0, "k_shape": 0.4}


@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    # mirror mínimo: 1 partido histórico + registro de jugadores
    mirror = pd.DataFrame([{
        "tour": "ATP", "date": date(2026, 3, 1), "tournament": "T0", "series": "",
        "surface": "Hard", "indoor": False, "round": "1st Round", "best_of": 3,
        "player_a": "alcaraz_c", "player_b": "sinner_j",
        "raw_name_a": "Alcaraz C.", "raw_name_b": "Sinner J.",
        "label_a_wins": 1, "status": "completed", "sets_a": 2, "sets_b": 0,
        "games_a": 12, "games_b": 6, "set1_winner_a": 1,
        "rank_a": 2.0, "rank_b": 1.0, "pts_a": 9000.0, "pts_b": 11000.0,
        "odds_json": "{}", "source": "test",
        "match_id": "ATP_2026-03-01_alcaraz_c__sinner_j",
    }])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": "ATP", "n_matches": 1,
                   "display_name": p, "hand": None, "dob": None}
                  for p in ("alcaraz_c", "sinner_j", "draper_j")]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def _csv(tmp_path, name, rows):
    header = "date,tour,tournament,surface,indoor,round,best_of,winner,loser,score,status\n"
    p = tmp_path / name
    p.write_text(header + "\n".join(rows) + "\n")
    return p


def test_import_accept_and_append_only(cfg_tmp, tmp_path):
    f = _csv(tmp_path, "r1.csv",
             ["2026-07-10,ATP,Wimbledon,Grass,false,The Final,5,Alcaraz C.,Sinner J.,6-4 4-6 6-3 6-4,completed"])
    rep = import_results(cfg_tmp, f, today=TODAY)
    assert rep["n_accepted"] == 1 and rep["n_rejected"] == 0
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    assert len(m) == 2
    row = m[m["date"] == date(2026, 7, 10)].iloc[0]
    assert row["best_of"] == 5 and row["status"] == "completed"
    # segundo import: append sin tocar lo anterior
    f2 = _csv(tmp_path, "r2.csv",
              ["2026-07-12,ATP,Bastad,Clay,false,1st Round,3,Draper J.,Alcaraz C.,7-6 6-4,completed"])
    rep2 = import_results(cfg_tmp, f2, today=TODAY)
    assert rep2["n_accepted"] == 1 and rep2["total_manual_rows"] == 2
    assert len(load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))) == 3
    # log con hash
    log = (Path(cfg_tmp["paths"]["canonical_dir"]) / "manual_imports_log.jsonl").read_text()
    entries = [json.loads(x) for x in log.splitlines()]
    assert len(entries) == 2 and all(len(e["sha256_16"]) == 16 for e in entries)


def test_import_duplicates_rejected(cfg_tmp, tmp_path):
    row = "2026-07-10,ATP,W,Grass,false,F,3,Alcaraz C.,Sinner J.,6-4 6-4,completed"
    f = _csv(tmp_path, "dup.csv", [row, row])          # duplicado dentro del fichero
    rep = import_results(cfg_tmp, f, today=TODAY)
    assert rep["n_accepted"] == 1 and rep["n_rejected"] == 1
    assert "duplicado dentro del fichero" in rep["rejected"][0]["error"]
    f2 = _csv(tmp_path, "dup2.csv", [row])             # duplicado contra el dataset
    rep2 = import_results(cfg_tmp, f2, today=TODAY)
    assert rep2["n_accepted"] == 0
    assert "ya existe" in rep2["rejected"][0]["error"]
    # tambien contra el mirror
    f3 = _csv(tmp_path, "dup3.csv",
              ["2026-03-01,ATP,T0,Hard,false,1st Round,3,Alcaraz C.,Sinner J.,6-3 6-3,completed"])
    rep3 = import_results(cfg_tmp, f3, today=TODAY)
    assert rep3["n_accepted"] == 0 and "ya existe" in rep3["rejected"][0]["error"]


def test_import_future_date_rejected(cfg_tmp, tmp_path):
    f = _csv(tmp_path, "fut.csv",
             ["2026-09-01,ATP,US Open,Hard,false,F,5,Alcaraz C.,Sinner J.,6-4 6-4 6-4,completed"])
    rep = import_results(cfg_tmp, f, today=TODAY)
    assert rep["n_accepted"] == 0
    assert "dato_futuro" in rep["rejected"][0]["error"]


def test_import_unknown_player_quarantined_with_suggestions(cfg_tmp, tmp_path):
    f = _csv(tmp_path, "unk.csv",
             ["2026-07-10,ATP,W,Grass,false,F,3,Alcarez C.,Sinner J.,6-4 6-4,completed"])
    rep = import_results(cfg_tmp, f, today=TODAY)
    assert rep["n_accepted"] == 0 and rep["n_quarantined"] == 1
    sugg = rep["quarantined"][0]["unknown"]["Alcarez C."]
    assert "alcaraz_c" in sugg
    # con allow_new se acepta
    rep2 = import_results(cfg_tmp, f, allow_new=True, today=TODAY)
    assert rep2["n_accepted"] == 1


def test_import_retirement_walkover_and_incoherent_scores(cfg_tmp, tmp_path):
    f = _csv(tmp_path, "mix.csv", [
        "2026-07-10,ATP,A,Hard,false,R1,3,Alcaraz C.,Sinner J.,6-4 3-1,retired",
        "2026-07-11,ATP,A,Hard,false,R2,3,Alcaraz C.,Draper J.,,walkover",
        "2026-07-12,ATP,A,Hard,false,R3,3,Sinner J.,Draper J.,6-4,completed",       # incoherente
        "2026-07-13,ATP,A,Hard,false,R4,3,Sinner J.,Draper J.,6-4 6-2,retired",     # completo con retired
        "2026-07-14,ATP,A,Hard,false,R5,3,Sinner J.,Draper J.,6-4 6-3,walkover",    # walkover con score
    ])
    rep = import_results(cfg_tmp, f, today=TODAY)
    assert rep["n_accepted"] == 2 and rep["n_rejected"] == 3
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    ret = m[m["status"] == "retired"].iloc[0]
    assert ret["sets_a"] + ret["sets_b"] == 1          # solo el set completado cuenta
    wo = m[m["status"] == "walkover"].iloc[0]
    assert pd.isna(wo["label_a_wins"])


def test_import_order_independent_elo_replay(cfg_tmp, tmp_path):
    """Importar en orden cronológico inverso produce el MISMO Elo final que en
    orden correcto (el replay ordena por fecha)."""
    r_early = "2026-07-01,ATP,A,Hard,false,R1,3,Alcaraz C.,Draper J.,6-4 6-4,completed"
    r_late = "2026-07-08,ATP,B,Hard,false,R1,3,Sinner J.,Alcaraz C.,7-5 7-5,completed"
    # escenario 1: desordenado (tarde primero)
    import_results(cfg_tmp, _csv(tmp_path, "late.csv", [r_late]), today=TODAY)
    import_results(cfg_tmp, _csv(tmp_path, "early.csv", [r_early]), today=TODAY)
    m1 = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    _, states1 = replay(m1, CFG_ELO)
    # escenario 2: dataset equivalente construido de una vez y en orden
    canon2 = tmp_path / "canon2"
    canon2.mkdir()
    m_mirror = pd.read_parquet(Path(cfg_tmp["paths"]["canonical_dir"]) / "matches.parquet")
    m_mirror.to_parquet(canon2 / "matches.parquet", index=False)
    cfg2 = copy.deepcopy(cfg_tmp)
    cfg2["paths"]["canonical_dir"] = str(canon2)
    pd.read_parquet(Path(cfg_tmp["paths"]["canonical_dir"]) / "players.parquet").to_parquet(
        canon2 / "players.parquet", index=False)
    import_results(cfg2, _csv(tmp_path, "both.csv", [r_early, r_late]), today=TODAY)
    m2 = load_matches(canon2)
    _, states2 = replay(m2, CFG_ELO)
    for p in ("alcaraz_c", "sinner_j", "draper_j"):
        assert states1["ATP"].get(p) == pytest.approx(states2["ATP"].get(p), abs=1e-9)
    # y el partido tardío ve el Elo actualizado por el temprano
    df1, _ = replay(m1, CFG_ELO)
    late_row = df1[df1["date"] == date(2026, 7, 8)].iloc[0]
    assert late_row["elo_a"] != 1500.0 or late_row["elo_b"] != 1500.0


def test_import_dry_run_writes_nothing(cfg_tmp, tmp_path):
    f = _csv(tmp_path, "dry.csv",
             ["2026-07-10,ATP,W,Grass,false,F,3,Alcaraz C.,Sinner J.,6-4 6-4,completed"])
    rep = import_results(cfg_tmp, f, dry_run=True, today=TODAY)
    assert rep["n_accepted"] == 1 and rep["dry_run"]
    assert len(load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))) == 1   # solo el mirror
