"""Arquitectura de dos niveles: RECENT OUTCOMES (ganador+sets, outcome_only)
junto al canónico full — sin inventar marcadores, sin relajar prepare_rows.

Cubre las reglas seguras de /scores (Bo3/Bo5, retirada UNKNOWN, empate/live/
upcoming rechazados), el almacén separado, la reconciliación con el full
(Elo/actividad exactamente UNA vez), la freshness por capacidad y el
presupuesto de The Odds API.
"""
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from betbot.canonical.outcomes import (append_outcomes, load_outcomes,
                                       load_state_matches, outcomes_as_state_rows,
                                       unreconciled_outcomes)
from betbot.config import load_config
from betbot.feeds.oddsapi_scores import OddsApiScores
from betbot.sync import freshness_detail, local_freshness, run_sync

TODAY = date(2026, 8, 10)


def _ev(sets=("2", "1"), completed=True, home="Alejandro Tabilo",
        away="Ben Shelton", sport="tennis_atp_canadian_open",
        title="ATP Canadian Open", commence="2026-08-08T15:00:00Z", eid="ev1"):
    return {"id": eid, "sport_key": sport, "sport_title": title,
            "commence_time": commence, "completed": completed,
            "home_team": home, "away_team": away,
            "scores": None if sets is None else [
                {"name": home, "score": sets[0]}, {"name": away, "score": sets[1]}],
            "last_update": commence}


def _outcome(src=None, ev=None, since=date(2026, 8, 4), until=TODAY):
    src = src or OddsApiScores(api_key="k")
    return src._outcome_from_event(ev, ev["sport_key"], ev["sport_title"], since, until)


# ------------------------------------------------ reglas seguras /scores

def test_bo3_2_0_and_2_1_terminal():
    for sets in (("2", "0"), ("2", "1")):
        rec, verdict = _outcome(ev=_ev(sets=sets))
        assert verdict == "terminal"
        assert rec["winner_name"] == "Alejandro Tabilo"
        assert (rec["sets_w"], rec["sets_l"]) == (2, int(sets[1]))
        assert rec["completion_confidence"] == "terminal_y_completed"
        assert rec["retirement"] == "unknown"          # jamás se afirma ni se niega


def test_bo5_grand_slam_needs_three_sets():
    ev = _ev(sets=("3", "2"), sport="tennis_atp_us_open", title="ATP US Open")
    rec, verdict = _outcome(ev=ev)
    assert verdict == "terminal" and rec["sets_w"] == 3
    # 2-1 en un Grand Slam NO es terminal: live sin terminar -> reject
    ev2 = _ev(sets=("2", "1"), completed=False,
              sport="tennis_atp_us_open", title="ATP US Open")
    rec2, verdict2 = _outcome(ev=ev2)
    assert rec2 is None and verdict2 == "live_no_terminal"


def test_completed_non_terminal_gives_winner_but_retirement_unknown():
    rec, verdict = _outcome(ev=_ev(sets=("1", "0"), completed=True))
    assert verdict == "winner_sin_terminal"
    assert rec["winner_name"] == "Alejandro Tabilo"
    assert rec["retirement"] == "unknown"
    assert rec["completion_confidence"] == "completed_sin_terminal"


def test_tie_rejected():
    rec, verdict = _outcome(ev=_ev(sets=("1", "1"), completed=True))
    assert rec is None and verdict == "empate"


def test_live_non_terminal_rejected_but_terminal_score_accepted():
    rec, verdict = _outcome(ev=_ev(sets=("1", "0"), completed=False))
    assert rec is None and verdict == "live_no_terminal"
    # marcador matemáticamente terminal con completed=false (flag rezagado
    # demostrado): SÍ se acepta, con confianza distinta
    rec2, verdict2 = _outcome(ev=_ev(sets=("2", "0"), completed=False))
    assert verdict2 == "terminal" and rec2["completion_confidence"] == "terminal_score"


def test_upcoming_never_and_absurd_fail_closed():
    rec, verdict = _outcome(ev=_ev(sets=None))
    assert rec is None and verdict == "upcoming"
    rec, verdict = _outcome(ev=_ev(sets=("4", "1")))       # >need: imposible
    assert rec is None and verdict == "ambiguo"
    rec, verdict = _outcome(ev=_ev(sets=("6-4 6-3", "")))  # no entero
    assert rec is None and verdict == "ambiguo"


# ------------------------------------------------ almacén + reconciliación

def _cfg_tmp(tmp_path, atp_until=date(2026, 8, 3)):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir(exist_ok=True)
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    mirror = pd.DataFrame([{
        "tour": "ATP", "date": atp_until, "tournament": "Washington", "series": "",
        "surface": "Hard", "indoor": False, "round": "The Final", "best_of": 3,
        "player_a": "shelton_b", "player_b": "tabilo_a", "raw_name_a": "Shelton B.",
        "raw_name_b": "Tabilo A.", "label_a_wins": 1, "status": "completed",
        "sets_a": 2, "sets_b": 0, "games_a": 12, "games_b": 7, "set1_winner_a": 1,
        "rank_a": 6.0, "rank_b": 24.0, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test",
        "match_id": f"ATP_{atp_until}_shelton_b__tabilo_a"}])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": "ATP", "n_matches": 1, "display_name": n,
                   "hand": None, "dob": None}
                  for p, n in (("tabilo_a", "Alejandro Tabilo"),
                               ("shelton_b", "Ben Shelton"),
                               ("fils_a", "Arthur Fils"), ("ruud_c", "Casper Ruud"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def _sync_outcomes(cfg, events, monkeypatch, today=TODAY):
    src = OddsApiScores(api_key="k")

    def fake_get(url, ttl_seconds, headers=None):
        if "/sports/?" in url:
            return [{"key": "tennis_atp_canadian_open",
                     "title": "ATP Canadian Open", "active": True}]
        return events

    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json", fake_get)
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.is_cached",
                        lambda url, ttl: False)
    return run_sync(cfg, sources=[src], today=today, refresh=False)


def test_outcome_ingested_into_separate_store_not_canonical(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    rep = _sync_outcomes(cfg, [_ev()], monkeypatch)
    canon = Path(cfg["paths"]["canonical_dir"])
    assert rep["outcomes"]["accepted"] == 1
    assert rep["n_accepted"] == 0                       # matches.parquet INTACTO
    o = load_outcomes(canon)
    assert len(o) == 1
    r = o.iloc[0]
    assert r["detail_level"] == "outcome_only"
    assert r["retirement"] == "unknown"
    assert r["surface"] == "Hard"                       # registro de torneos, no inventada
    assert {r["player_a"], r["player_b"]} == {"tabilo_a", "shelton_b"}
    m = pd.read_parquet(canon / "matches.parquet")
    assert len(m) == 1                                  # full sin tocar
    # la frescura FULL no se mueve; el estado sí tiene el outcome
    assert local_freshness(cfg)["ATP"] == date(2026, 8, 3)
    assert max(load_state_matches(canon)["date"]) == date(2026, 8, 8)


def test_unknown_player_never_fuzzy(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    rep = _sync_outcomes(cfg, [_ev(home="Nadie Conocido", eid="evX")], monkeypatch)
    assert rep["outcomes"]["accepted"] == 0
    assert rep["outcomes"]["unknown_players"] == 1


def test_outcome_duplicate_of_full_is_skipped(tmp_path, monkeypatch):
    """Outcome del MISMO partido ya presente en full (fecha ±1): no se guarda."""
    cfg = _cfg_tmp(tmp_path)
    ev = _ev(commence="2026-08-03T20:00:00Z", home="Ben Shelton",
             away="Alejandro Tabilo", eid="dupF")
    rep = _sync_outcomes(cfg, [ev], monkeypatch)
    assert rep["outcomes"]["dup_full"] == 1 and rep["outcomes"]["accepted"] == 0


def test_full_later_supersedes_outcome_elo_applied_exactly_once(tmp_path, monkeypatch):
    """Reconciliación: llega primero el outcome (Tabilo d. Shelton 2-1) y días
    después el full (6-4 3-6 6-2). El estado cuenta el partido UNA vez: mismo
    nº de filas de estado, mismo Elo, misma actividad antes y después."""
    cfg = _cfg_tmp(tmp_path)
    canon = Path(cfg["paths"]["canonical_dir"])
    _sync_outcomes(cfg, [_ev()], monkeypatch)
    state1 = load_state_matches(canon)
    assert len(state1) == 2                              # 1 full + 1 outcome
    # replay del estado con el outcome
    from betbot.ratings.elo import replay
    elo1, states1 = replay(state1, cfg["elo"])
    r_tabilo_after_outcome = states1["ATP"].get("tabilo_a")

    # ...llega el FULL equivalente (TML) por el pipeline normal
    class FakeGithub:
        name = "github"
        supported_tours = frozenset({"ATP", "WTA"})

        def fetch_results(self, since, until):
            from betbot.feeds.base import SourceStatus
            return ([{"date": "2026-08-08", "tour": "ATP",
                      "tournament": "Canadian Open", "surface": "Hard",
                      "indoor": "false", "round": "", "best_of": "3",
                      "winner": "Tabilo A.", "loser": "Shelton B.",
                      "score": "6-4 3-6 6-2", "status": "completed",
                      "_level": "main"}],
                    SourceStatus(name="github", ok=True, n_items=1))

    rep = run_sync(cfg, sources=[FakeGithub()], today=TODAY, refresh=False)
    assert rep["n_accepted"] == 1
    state2 = load_state_matches(canon)
    assert len(state2) == 2                              # SIGUE siendo un partido
    row = state2[state2["date"] == date(2026, 8, 8)]
    assert len(row) == 1
    assert row.iloc[0]["games_a"] + row.iloc[0]["games_b"] > 0   # ahora es el FULL
    elo2, states2 = replay(state2, cfg["elo"])
    assert states2["ATP"].get("tabilo_a") == pytest.approx(r_tabilo_after_outcome)
    assert states2["ATP"].n.get("tabilo_a") == states1["ATP"].n.get("tabilo_a") == 2


def test_activity_counted_exactly_once_after_reconciliation(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    canon = Path(cfg["paths"]["canonical_dir"])
    _sync_outcomes(cfg, [_ev()], monkeypatch)
    from betbot.features.builder import build_features
    from betbot.ratings.elo import replay
    players = pd.read_parquet(canon / "players.parquet")

    def m14_of(pid):
        state_df = load_state_matches(canon)
        elo_df, _ = replay(state_df, cfg["elo"])
        _f, act = build_features(elo_df, players, cfg)
        return len([x for x in act["recent"].get(f"ATP|{pid}", [])
                    if (TODAY - date.fromisoformat(x)).days <= 14])

    before = m14_of("tabilo_a")
    # tras reconciliar con el full, el conteo NO se duplica
    from betbot.canonical.store import append_manual
    append_manual(canon, pd.DataFrame([{
        "tour": "ATP", "date": date(2026, 8, 8), "tournament": "Canadian Open",
        "series": "", "surface": "Hard", "indoor": False, "round": "", "best_of": 3,
        "player_a": "shelton_b", "player_b": "tabilo_a", "raw_name_a": "Shelton B.",
        "raw_name_b": "Tabilo A.", "label_a_wins": 0, "status": "completed",
        "sets_a": 1, "sets_b": 2, "games_a": 13, "games_b": 16, "set1_winner_a": 0,
        "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "sync", "match_id": "ATP_2026-08-08_shelton_b__tabilo_a"}]))
    assert m14_of("tabilo_a") == before == 2             # exactamente una vez


def test_state_rows_never_invent_partial_features(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    _sync_outcomes(cfg, [_ev()], monkeypatch)
    rows = outcomes_as_state_rows(load_outcomes(Path(cfg["paths"]["canonical_dir"])))
    r = rows.iloc[0]
    assert pd.isna(r["rank_a"]) and pd.isna(r["rank_b"])          # rank NO inventado
    assert pd.isna(r["set1_winner_a"])                             # set1 NO inventado
    assert r["games_a"] == 0 and r["games_b"] == 0                 # juegos NO inventados
    assert r["odds_json"] == "{}"
    assert r["status"] == "completed"                              # retirada UNKNOWN -> sin marca
    assert r["source"].startswith("recent_outcome:")


# ------------------------------------------------ freshness por capacidad

def test_capability_freshness_partial_semantics(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    det0 = freshness_detail(cfg, today=TODAY)["ATP"]
    assert det0["production_feature_freshness"] == "STALE"
    _sync_outcomes(cfg, [_ev(commence="2026-08-09T15:00:00Z")], monkeypatch)
    det = freshness_detail(cfg, today=TODAY)["ATP"]
    assert det["global_history"] == date(2026, 8, 3)      # full sigue stale: honesto
    assert det["outcome_state"] == date(2026, 8, 9)
    assert det["production_feature_freshness"] == "PARTIAL"
    assert "canadian open" in det["active_coverage"]
    # y el banner pasa de MODO DEGRADADO severo a FRESCURA PARCIAL
    from betbot.picks import degraded_banner
    summary = {"date": str(TODAY),
               "results_freshness": {"ATP": "2026-08-03", "WTA": str(TODAY)},
               "results_coverage": {"ATP": {
                   "production_feature_freshness": det["production_feature_freshness"],
                   "outcome_state": str(det["outcome_state"]),
                   "active_coverage": {k: {"tournament": c["tournament"],
                                           "until": str(c["until"])}
                                       for k, c in det["active_coverage"].items()}}}}
    banner = degraded_banner(summary)
    assert "FRESCURA PARCIAL" in banner and "MODO DEGRADADO" not in banner
    assert "retired_recent" in banner                     # sin verde artificial
    # sin outcomes recientes el banner severo se mantiene
    severe = degraded_banner({"date": str(TODAY),
                              "results_freshness": {"ATP": "2026-08-03", "WTA": str(TODAY)},
                              "results_coverage": {"ATP": {"production_feature_freshness": "STALE"}}})
    assert "MODO DEGRADADO" in severe


def test_completed_index_includes_outcomes(tmp_path, monkeypatch):
    """Un outcome es evidencia already_completed para la puerta prematch."""
    cfg = _cfg_tmp(tmp_path)
    _sync_outcomes(cfg, [_ev(commence="2026-08-09T15:00:00Z")], monkeypatch)
    from betbot.prematch import completed_index
    idx = completed_index(cfg, around=TODAY)
    assert ("ATP", "shelton_b", "tabilo_a") in idx


# ------------------------------------------------ presupuesto / fallos

def test_outcome_budget_max_sports_and_telemetry(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, ttl_seconds, headers=None):
        calls.append(url)
        if "/sports/?" in url:
            return [{"key": f"tennis_atp_t{i}", "title": f"ATP T{i}", "active": True}
                    for i in range(6)]
        return []

    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json", fake_get)
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.is_cached", lambda u, t: False)
    src = OddsApiScores(api_key="k", max_sports=2)
    _recs, st = src.fetch_outcomes(date(2026, 8, 7), TODAY)
    scores_calls = [c for c in calls if "/scores/" in c]
    assert len(scores_calls) == 2                        # tope respetado
    tele = next(n for n in st.notes if n.startswith("telemetría"))
    assert "network_requests=2" in tele and "~4 créditos" in tele
    assert any("tope de créditos" in n for n in st.notes)


def test_outcome_api_failure_fail_closed(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    src = OddsApiScores(api_key="k")
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json",
                        lambda url, ttl_seconds, headers=None:
                        (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    rep = run_sync(cfg, sources=[src], today=TODAY, refresh=False)
    assert rep["outcomes"]["fetched"] == 0 and rep["outcomes"]["accepted"] == 0
    assert load_outcomes(Path(cfg["paths"]["canonical_dir"])).empty
    assert local_freshness(cfg)["ATP"] == date(2026, 8, 3)   # nada se movió


def test_unreconciled_respects_one_day_drift():
    o = pd.DataFrame([{"event_id": "e", "date": date(2026, 8, 8), "tour": "ATP",
                       "tournament": "X", "surface": "", "player_a": "a_x",
                       "player_b": "b_y", "label_a_wins": 1, "sets_a": 2, "sets_b": 0,
                       "status": "completed", "retirement": "unknown", "source": "s",
                       "observed_at": "", "detail_level": "outcome_only",
                       "completion_confidence": "terminal_y_completed",
                       "canonical_match_id": "ATP_2026-08-08_a_x__b_y"}])
    full = pd.DataFrame([{"tour": "ATP", "player_a": "a_x", "player_b": "b_y",
                          "date": date(2026, 8, 7)}])
    assert len(unreconciled_outcomes(o, full)) == 0      # ±1 día: mismo partido
    full2 = pd.DataFrame([{"tour": "ATP", "player_a": "a_x", "player_b": "b_y",
                           "date": date(2026, 8, 5)}])
    assert len(unreconciled_outcomes(o, full2)) == 1     # 3 días: partido distinto
