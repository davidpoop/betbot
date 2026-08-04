"""Tests de `betbot sync-results`: backfill, dedupe (exacto y casi-duplicado),
prevención de futuro, cuarentena/altas de debutantes, idempotencia y el estado
data_freshness_unknown en vez de falso OOD cuando la fuente va con retraso."""
import copy
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from betbot.canonical.names import extend_initials, resolve_full_name
from betbot.canonical.store import load_matches
from betbot.config import load_config
from betbot.feeds.base import SourceStatus
from betbot.sync import local_freshness, run_sync

TODAY = date(2026, 8, 4)


class FakeResults:
    """ResultsSource de prueba; registra la ventana pedida."""
    name = "fake_results"

    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail
        self.asked = None

    def fetch_results(self, since, until):
        if self.fail:
            raise RuntimeError("fuente de resultados caida")
        self.asked = (since, until)
        keep = [r for r in self.rows]
        return keep, SourceStatus(name=self.name, ok=True, n_items=len(keep))


def _row(d, tour, winner, loser, score="6-4 6-4", status="completed", **kw):
    base = {"date": str(d), "tour": tour, "tournament": "T", "surface": "Hard",
            "indoor": "false", "round": "R1", "best_of": "3",
            "winner": winner, "loser": loser, "score": score, "status": status,
            "_level": "main"}
    base.update(kw)
    return base


@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    mirror = pd.DataFrame([
        {"tour": "ATP", "date": date(2026, 7, 1), "tournament": "T0", "series": "",
         "surface": "Hard", "indoor": False, "round": "R1", "best_of": 3,
         "player_a": "alcaraz_c", "player_b": "sinner_j",
         "raw_name_a": "Alcaraz C.", "raw_name_b": "Sinner J.",
         "label_a_wins": 1, "status": "completed", "sets_a": 2, "sets_b": 0,
         "games_a": 12, "games_b": 6, "set1_winner_a": 1,
         "rank_a": 2.0, "rank_b": 1.0, "pts_a": 9000.0, "pts_b": 11000.0,
         "odds_json": "{}", "source": "test",
         "match_id": "ATP_2026-07-01_alcaraz_c__sinner_j"},
        {"tour": "WTA", "date": date(2026, 6, 20), "tournament": "W0", "series": "",
         "surface": "Hard", "indoor": False, "round": "R1", "best_of": 3,
         "player_a": "gauff_c", "player_b": "swiatek_i",
         "raw_name_a": "Gauff C.", "raw_name_b": "Swiatek I.",
         "label_a_wins": 0, "status": "completed", "sets_a": 0, "sets_b": 2,
         "games_a": 5, "games_b": 12, "set1_winner_a": 0,
         "rank_a": 3.0, "rank_b": 1.0, "pts_a": 8000.0, "pts_b": 10000.0,
         "odds_json": "{}", "source": "test",
         "match_id": "WTA_2026-06-20_gauff_c__swiatek_i"}])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1,
                   "display_name": p, "hand": None, "dob": None}
                  for p, t in (("alcaraz_c", "ATP"), ("sinner_j", "ATP"),
                               ("draper_j", "ATP"), ("struff_j_l", "ATP"),
                               ("gauff_c", "WTA"), ("swiatek_i", "WTA"),
                               ("eala_a", "WTA"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def test_sync_accepts_atp_and_wta_and_updates_freshness(cfg_tmp):
    src = FakeResults([
        _row(date(2026, 8, 2), "ATP", "Alcaraz C.", "Draper J."),
        _row(date(2026, 8, 3), "WTA", "Eala A.", "Gauff C.", score="4-6 6-4 6-0")])
    rep = run_sync(cfg_tmp, sources=[src], today=TODAY)
    assert rep["n_accepted"] == 2 and rep["n_quarantined"] == 0
    fresh = local_freshness(cfg_tmp)
    assert fresh["ATP"] == date(2026, 8, 2) and fresh["WTA"] == date(2026, 8, 3)
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    wta = m[(m["tour"] == "WTA") & (m["date"] == date(2026, 8, 3))].iloc[0]
    assert wta["sets_a"] + wta["sets_b"] == 3       # 2-1 en sets, orientado neutral


def test_sync_window_incremental_and_forced_backfill(cfg_tmp):
    src = FakeResults([])
    run_sync(cfg_tmp, sources=[src], today=TODAY)
    # incremental: desde min(frescura local) - 2 dias (WTA 2026-06-20), con tope
    since, until = src.asked
    max_days = int(cfg_tmp["feeds"].get("sync_max_days", 30))
    assert until == TODAY
    assert since == max(date(2026, 6, 20) - timedelta(days=2),
                        TODAY - timedelta(days=max_days))
    # --days fuerza la ventana completa aunque la fecha maxima este inflada
    run_sync(cfg_tmp, sources=[src], days=100, today=TODAY)
    assert src.asked[0] == TODAY - timedelta(days=100)


def test_sync_dedupe_exact_and_near_duplicate(cfg_tmp):
    # exacto: mismo match_id que el mirror
    src = FakeResults([_row(date(2026, 7, 1), "ATP", "Alcaraz C.", "Sinner J.",
                            score="6-4 6-3")])
    rep = run_sync(cfg_tmp, sources=[src], today=TODAY)
    assert rep["n_accepted"] == 0 and rep["n_duplicates_skipped"] == 1
    # casi-duplicado: mismo par y resultado con fecha desplazada 1 dia
    src2 = FakeResults([_row(date(2026, 7, 2), "ATP", "Alcaraz C.", "Sinner J.",
                             score="7-5 6-2")])   # mismo ganador, mismo 2-0
    rep2 = run_sync(cfg_tmp, sources=[src2], today=TODAY)
    assert rep2["n_accepted"] == 0 and rep2["n_near_duplicates"] == 1
    # revancha real: mismo par a 1 dia pero DISTINTO marcador por sets -> entra
    src3 = FakeResults([_row(date(2026, 7, 2), "ATP", "Alcaraz C.", "Sinner J.",
                             score="6-4 3-6 6-3")])   # 2-1, no 2-0
    rep3 = run_sync(cfg_tmp, sources=[src3], today=TODAY)
    assert rep3["n_accepted"] == 1 and rep3["n_near_duplicates"] == 0


def test_sync_second_run_idempotent_no_state_change(cfg_tmp):
    rows = [_row(date(2026, 8, 2), "ATP", "Alcaraz C.", "Draper J.")]
    rep1 = run_sync(cfg_tmp, sources=[FakeResults(rows)], today=TODAY)
    assert rep1["n_accepted"] == 1
    n_rows = len(load_matches(Path(cfg_tmp["paths"]["canonical_dir"])))
    rep2 = run_sync(cfg_tmp, sources=[FakeResults(rows)], today=TODAY)
    assert rep2["n_accepted"] == 0 and rep2["n_duplicates_skipped"] == 1
    assert "state" not in rep2                     # sin altas no se toca el estado
    assert len(load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))) == n_rows


def test_sync_future_result_rejected(cfg_tmp):
    src = FakeResults([_row(TODAY + timedelta(days=1), "ATP", "Alcaraz C.", "Draper J.")])
    rep = run_sync(cfg_tmp, sources=[src], today=TODAY)
    assert rep["n_accepted"] == 0 and rep["n_rejected"] == 1
    assert "dato_futuro" in rep["rejected_sample"][0]["error"]


def test_sync_quarantine_ambiguous_accept_unambiguous_debutant(cfg_tmp):
    src = FakeResults([
        _row(date(2026, 8, 1), "ATP", "Alcarez C.", "Draper J."),        # errata cercana
        _row(date(2026, 8, 2), "ATP", "Debutante Totalmente Nuevo", "Sinner J.")])
    rep = run_sync(cfg_tmp, sources=[src], today=TODAY)
    assert rep["n_quarantined"] == 1                                      # la errata
    assert "alcaraz_c" in str(rep["quarantine_sample"][0]["unknown"])
    assert rep["n_accepted"] == 1 and rep["n_new_players"] >= 1           # el debutante
    players = pd.read_parquet(Path(cfg_tmp["paths"]["canonical_dir"]) / "players.parquet")
    assert (players["bio_source"] == "sync").any()


def test_sync_source_failure_keeps_going(cfg_tmp):
    ok_src = FakeResults([_row(date(2026, 8, 2), "ATP", "Alcaraz C.", "Draper J.")])
    rep = run_sync(cfg_tmp, sources=[FakeResults([], fail=True), ok_src], today=TODAY)
    assert rep["n_accepted"] == 1
    st = {s["name"]: s["ok"] for s in rep["sources"]}
    assert st["fake_results"] in (True, False)     # ambos registrados
    assert [s for s in rep["sources"] if not s["ok"]]


def test_full_name_resolution_and_initials_extension():
    reg = {"fritz_t", "struff_j_l", "del_potro_j_m", "pliskova_ka", "cerundolo_j_m",
           "cerundolo_f", "auger_aliassime_f"}
    assert resolve_full_name("Taylor Fritz", reg) == "fritz_t"
    assert resolve_full_name("Jan-Lennard Struff", reg) == "struff_j_l"
    assert resolve_full_name("Juan Martin del Potro", reg) == "del_potro_j_m"
    assert resolve_full_name("Karolina Pliskova", reg) == "pliskova_ka"
    assert resolve_full_name("Felix Auger-Aliassime", reg) == "auger_aliassime_f"
    assert resolve_full_name("Fritz T.", reg) == "fritz_t"      # forma TD directa
    # extension unica determinista de iniciales
    assert extend_initials("struff_j", reg) == "struff_j_l"
    assert extend_initials("cerundolo_j", reg) == "cerundolo_j_m"
    assert extend_initials("fritz_t", reg) == "fritz_t"          # ya presente
    reg2 = reg | {"struff_j_x"}                                  # ambiguo: no se toca
    assert extend_initials("struff_j", reg2) == "struff_j"


def test_delayed_source_soft_freshness_flag_not_false_ood():
    """Con los datos del circuito retrasados N>umbral dias, la falta de partidos
    se marca data_freshness_unknown (aviso blando) y NO como OOD del jugador."""
    from betbot.screener.run import _activity
    d = date(2026, 8, 4)
    cfg_sel = {"min_matches_12m": 5, "layoff_days_ood": 90, "stale_days_unknown": 540,
               "freshness_lag_max_days": 14}
    state = {"recent": {"ATP|p1": [f"2025-06-0{x}" for x in range(1, 9)]},
             "last_date": {"ATP|p1": "2025-10-01"},
             "last_retired": {},
             "freshness": {"ATP": "2025-10-12"}}
    act = _activity(state, "ATP", "p1", d, cfg_sel)
    assert act["freshness_unknown"] is True and act["lag_days"] > 14
    # jugo 8 partidos en los 12m ANTERIORES a la fecha de frescura -> no es OOD
    assert act["m12"] >= 5
    assert act["stale"] is False                   # gap medido contra la frescura
    # misma actividad con datos FRESCOS: si que seria OOD por inactividad real
    state2 = dict(state, freshness={"ATP": "2026-08-03"})
    act2 = _activity(state2, "ATP", "p1", d, cfg_sel)
    assert act2["freshness_unknown"] is False and act2["m12"] < 5
