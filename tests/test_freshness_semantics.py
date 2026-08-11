"""Semántica de frescura alineada con la arquitectura de dos niveles.

FULL RESULT FRESHNESS (marcadores completos) != OPERATIONAL/FEATURE FRESHNESS
(lo que realmente alimenta a M4/M5). El banner, la sección "Datos", la matriz
de capacidades y el estado de rankings deben decir la verdad POR CAPACIDAD y
POR CONTEXTO — sin describir como stale lo que los recent outcomes mantienen
al día, y sin pintar de verde lo que no lo está.
"""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch
from betbot.feeds.oddsapi_scores import OddsApiScores
from betbot.picks import degraded_banner
from betbot.scan import _analyzed_coverage
from betbot.sync import capability_freshness, run_sync

TODAY = date(2026, 8, 10)
REF = str(TODAY)


def _cfg(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir(exist_ok=True)
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    rows = [
        {"tour": "ATP", "date": date(2026, 8, 3), "player_a": "fils_a", "player_b": "ruud_c"},
        {"tour": "WTA", "date": date(2026, 8, 10), "player_a": "gauff_c", "player_b": "swiatek_i"},
    ]
    pd.DataFrame([{
        "tour": r["tour"], "date": r["date"], "tournament": "T0", "series": "",
        "surface": "Hard", "indoor": False, "round": "R1", "best_of": 3,
        "player_a": r["player_a"], "player_b": r["player_b"],
        "raw_name_a": r["player_a"], "raw_name_b": r["player_b"],
        "label_a_wins": 1, "status": "completed", "sets_a": 2, "sets_b": 0,
        "games_a": 12, "games_b": 6, "set1_winner_a": 1, "rank_a": None, "rank_b": None,
        "pts_a": None, "pts_b": None, "odds_json": "{}", "source": "test",
        "match_id": f"{r['tour']}_{r['date']}_{r['player_a']}__{r['player_b']}"}
        for r in rows]).to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1, "display_name": n,
                   "hand": None, "dob": None}
                  for p, t, n in (("fils_a", "ATP", "Arthur Fils"),
                                  ("ruud_c", "ATP", "Casper Ruud"),
                                  ("shelton_b", "ATP", "Ben Shelton"),
                                  ("tabilo_a", "ATP", "Alejandro Tabilo"),
                                  ("gauff_c", "WTA", "Coco Gauff"),
                                  ("swiatek_i", "WTA", "Iga Swiatek"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def _add_outcome(cfg, monkeypatch, commence="2026-08-09T19:10:00Z",
                 home="Arthur Fils", away="Casper Ruud", eid="ev1"):
    ev = {"id": eid, "sport_key": "tennis_atp_canadian_open",
          "sport_title": "ATP Canadian Open", "commence_time": commence,
          "completed": True, "home_team": home, "away_team": away,
          "scores": [{"name": home, "score": "2"}, {"name": away, "score": "1"}],
          "last_update": commence}
    src = OddsApiScores(api_key="k")
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json",
                        lambda url, ttl_seconds, headers=None:
                        ([{"key": "tennis_atp_canadian_open",
                           "title": "ATP Canadian Open", "active": True}]
                         if "/sports/?" in url else [ev]))
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.is_cached", lambda u, t: False)
    return run_sync(cfg, sources=[src], today=TODAY, refresh=False)


def _fm(tour, tournament, p1="Fils A.", p2="Ruud C."):
    return FeedMatch(date=TODAY, tour=tour, tournament=tournament, player1=p1,
                     player2=p2, level="main", is_doubles=False, is_qualifying=False,
                     round="QF", best_of=3, surface="Hard", source="test",
                     event_id=f"e:{tournament}:{p1}", status="scheduled",
                     scheduled_at_utc=datetime(2026, 8, 10, 18, tzinfo=timezone.utc),
                     source_updated_at=REF, authoritative=True)


# ------------------------------------------------ capacidades

def test_capability_freshness_separates_full_from_features(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY,
                                rankings={"ATP": {"published": "2026-08-03",
                                                  "age_days": 7, "warnings": []}})
    atp = caps["ATP"]
    assert atp["full_scores"] == {"until": "2026-08-03", "age_days": 7, "status": "STALE"}
    assert atp["outcome_state"]["until"] == "2026-08-09"
    bt = atp["outcome_state"]["by_tournament"]["canadian open"]
    assert bt["status"] == "FRESH" and bt["until"] == "2026-08-09"
    f = atp["features"]
    for k in ("elo", "elo_surface", "activity", "experience"):
        assert f[k]["status"] == "FRESH_EN_TORNEOS_CUBIERTOS"
        assert f[k]["source"] == "recent_outcomes"
    assert f["retired_recent"]["status"] == "PARTIAL"     # outcome-only no lo sabe
    assert f["full_score_dependent"]["status"] == "STALE"
    assert f["rankings"]["status"] == "FRESH"             # 7d dentro de 45 de política
    assert f["rankings"]["max_age_days"] == 45
    assert atp["production_feature_freshness"] == "PARTIAL"


def test_rankings_status_follows_real_policy_not_arbitrary_days(tmp_path):
    cfg = _cfg(tmp_path)
    caps = capability_freshness(cfg, today=TODAY, rankings={
        "ATP": {"published": "2026-08-03", "age_days": 7, "warnings": []},
        "WTA": {"published": "2026-05-01", "age_days": 101, "warnings": ["viejo"]}})
    assert caps["ATP"]["features"]["rankings"]["status"] == "FRESH"    # 7d <= 45
    assert caps["WTA"]["features"]["rankings"]["status"] == "STALE"    # 101d > 45


def test_rankings_line_marks_within_policy():
    from betbot.picks import _rankings_line
    s = {"rankings_status": {"ATP": {"published": "2026-08-03", "age_days": 7,
                                     "max_age_days": 45, "within_policy": True,
                                     "n": 218, "warnings": []}}}
    line = _rankings_line("ATP", s)
    assert line.startswith("✓") and "efectivos 2026-08-03" in line and "7d de 45" in line
    s2 = {"rankings_status": {"ATP": {"published": "2026-05-01", "age_days": 101,
                                      "max_age_days": 45, "within_policy": False,
                                      "n": 218, "warnings": ["viejo"]}}}
    assert _rankings_line("ATP", s2).startswith("⚠")
    assert "FUERA DE POLÍTICA" in _rankings_line("ATP", s2)
    assert _rankings_line("ATP", {}).startswith("⚠")      # ausentes


# ------------------------------------------------ banner contextual

def _summary(caps, analyzed, fresh=None):
    return {"date": REF,
            "results_freshness": fresh or {"ATP": "2026-08-03", "WTA": REF},
            "results_coverage": caps, "analyzed_coverage": analyzed,
            "rankings_status": {"ATP": {"published": "2026-08-03", "age_days": 7,
                                        "max_age_days": 45, "within_policy": True,
                                        "n": 218, "warnings": []},
                                "WTA": {"published": "2026-08-03", "age_days": 7,
                                        "max_age_days": 45, "within_policy": True,
                                        "n": 241, "warnings": []}}}


def test_banner_partial_for_covered_tournament_never_claims_elo_stale(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage([_fm("ATP", "National Bank Open")], caps)
    b = degraded_banner(_summary(caps, analyzed))
    assert "FRESCURA PARCIAL ATP" in b
    assert "MODO DEGRADADO" not in b
    assert "outcome state National Bank Open hasta 2026-08-09" in b
    assert "Elo, Elo de superficie, actividad" in b and "experiencia" in b
    assert "marcadores completos hasta 2026-08-03" in b
    assert "retired_recent incompleto" in b
    assert "rankings ATP efectivos 2026-08-03" in b
    # la afirmación antigua y ya falsa NO puede aparecer para un torneo cubierto
    assert "medidos con datos viejos" not in b


def test_banner_degraded_for_uncovered_atp_tournament(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage([_fm("ATP", "Cincinnati")], caps)
    b = degraded_banner(_summary(caps, analyzed))
    assert "MODO DEGRADADO ATP" in b and "Cincinnati" in b
    assert "sin cobertura reciente" in b


def test_banner_mixed_covered_and_uncovered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage(
        [_fm("ATP", "National Bank Open"), _fm("ATP", "Winston-Salem")], caps)
    b = degraded_banner(_summary(caps, analyzed))
    assert "FRESCURA PARCIAL ATP" in b                     # el cubierto
    assert "MODO DEGRADADO ATP" in b and "Winston-Salem" in b   # el no cubierto


def test_wta_pick_not_contaminated_by_atp_staleness(tmp_path, monkeypatch):
    """Si solo hay oportunidades WTA y WTA está fresca, el retraso ATP va como
    NOTA DE SISTEMA, jamás como degradación de la pick WTA."""
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage([_fm("WTA", "Canadian Open", "Gauff C.", "Swiatek I.")],
                                  caps)
    b = degraded_banner(_summary(caps, analyzed))
    assert "Nota de sistema" in b
    assert "no afecta a las probabilidades mostradas" in b
    assert "MODO DEGRADADO ATP" not in b and "FRESCURA PARCIAL ATP" not in b
    assert "FRESCURA PARCIAL WTA" not in b and "MODO DEGRADADO WTA" not in b


def test_banner_none_when_everything_fresh(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    caps = capability_freshness(cfg, today=date(2026, 8, 4))
    analyzed = _analyzed_coverage([_fm("ATP", "Washington")], caps)
    s = _summary(caps, analyzed, fresh={"ATP": "2026-08-03", "WTA": "2026-08-03"})
    s["date"] = "2026-08-04"
    assert degraded_banner(s) is None


def test_uncovered_and_stale_keeps_full_severity_message(tmp_path):
    cfg = _cfg(tmp_path)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage([_fm("ATP", "Cincinnati")], caps)
    b = degraded_banner(_summary(caps, analyzed))
    assert "already_completed" in b and "OOD" in b        # sigue explicando el impacto


# ------------------------------------------------ completed_index end-to-end

def test_outcome_blocks_match_from_reappearing_as_prematch(tmp_path, monkeypatch):
    """END-TO-END: un recent outcome entra en completed_index y la puerta
    prepartido excluye ese mismo partido — jamás vuelve a ser prematch."""
    cfg = _cfg(tmp_path)
    rep = _add_outcome(cfg, monkeypatch, commence="2026-08-10T15:00:00Z",
                       home="Ben Shelton", away="Alejandro Tabilo", eid="e2e")
    assert rep["outcomes"]["accepted"] == 1
    from betbot.prematch import completed_index, gate
    idx = completed_index(cfg, around=TODAY)
    assert ("ATP", "shelton_b", "tabilo_a") in idx
    # el MISMO partido, publicado por un calendario como si fuera prematch
    m = FeedMatch(date=TODAY, tour="ATP", tournament="Canadian Open",
                  player1="Shelton B.", player2="Tabilo A.", level="main",
                  is_doubles=False, is_qualifying=False, round="QF", best_of=3,
                  surface="Hard", source="thesportsdb", event_id="cal:1",
                  status="scheduled",
                  scheduled_at_utc=datetime(2026, 8, 10, 23, tzinfo=timezone.utc),
                  source_updated_at=datetime.now(timezone.utc).isoformat(),
                  authoritative=True)
    cfg2 = copy.deepcopy(cfg)
    cfg2["feeds"]["commence_crosscheck"] = False
    res = gate([m], cfg2, now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
               completed_idx=idx, registry={"shelton_b", "tabilo_a"},
               fresh_until={"ATP": TODAY, "WTA": TODAY})
    assert res.eligible == []
    assert "already_completed" in res.counts()


# ------------------------------------------------ matriz y fuentes opcionales

def test_matrix_shows_two_level_results(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    from betbot.feeds.base import SourceStatus
    from betbot.feeds.manage import feeds_matrix

    class Cal:
        name = "thesportsdb"
        authoritative = True
        supported_tours = frozenset({"ATP", "WTA"})

        def fetch_matches(self, hours):
            return [], SourceStatus(name=self.name, ok=True, n_items=0)

    class SR:
        name = "sportradar"
        authoritative = True
        supported_tours = frozenset({"ATP", "WTA"})

        def fetch_matches(self, hours):
            return [], SourceStatus(name=self.name, ok=False,
                                    error="sin SPORTRADAR_API_KEY (adaptador inactivo)")

    monkeypatch.setattr("betbot.scan.default_sources", lambda cfg: ([Cal(), SR()], []))
    monkeypatch.setattr("betbot.sync.default_results_sources",
                        lambda cfg, allow_backfill=False: [])
    txt = feeds_matrix(cfg, hours=48)
    assert "full canonical: 2026-08-03" in txt
    assert "recent outcome state:" in txt and "Canadian Open: 2026-08-09" in txt
    assert "production freshness: PARTIAL" in txt
    assert "source recent: oddsapi_scores (outcome_only)" in txt
    assert "source full: github/TML" in txt
    # sportradar sin key: OPCIONAL, nunca fallo del producto
    assert "sportradar: INACTIVO_CONFIG (OPCIONAL, sin suscripción)" in txt
    assert "FALLO_ESTRUCTURAL" not in txt.split("CALENDARIO")[1].split("RESULTADOS")[0]


def test_sportradar_without_key_is_inactive_not_failure():
    from betbot.feeds.manage import OPTIONAL_SOURCES, classify_status
    from betbot.feeds.results import SportradarResults
    rows, st = SportradarResults(api_key="").fetch_results(date(2026, 8, 8), TODAY)
    assert rows == []
    assert classify_status({"ok": st.ok, "error": st.error, "n": 0}) == "INACTIVO_CONFIG"
    assert "sportradar" in OPTIONAL_SOURCES


# ------------------------------------------------ render

def test_datos_section_shows_capabilities(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _add_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    analyzed = _analyzed_coverage([_fm("ATP", "National Bank Open")], caps)
    from betbot.picks import render_daily
    from betbot.scan import ScanResult
    s = _summary(caps, analyzed)
    s.update({"window_hours": 48, "sources": [], "surface_sources": {},
              "market_coverage": {}, "states": {}, "analyzed_matches": 1,
              "eligible": 1, "found": 1, "matches_with_odds": 1,
              "odds_coverage_pct": 100.0, "unresolved_players": 0,
              "markets_with_quotes": ["match_winner"], "structured": {"active": False},
              "top_picks": [], "watchlist": [], "watchlist_near": 0,
              "opportunities": [], "orphan_quotes": 0, "stale_discarded": 0,
              "prematch_excluded": {}})
    res = ScanResult(summary=s, rows=pd.DataFrame(), displayed=pd.DataFrame())
    txt = render_daily(res)
    assert "marcadores completos hasta 2026-08-03" in txt
    assert "outcome state National Bank Open hasta 2026-08-09" in txt
    assert "estado predictivo (Elo/actividad/experiencia): PARTIAL" in txt
    assert "retired_recent PARTIAL" in txt
    assert "✓ rankings ATP efectivos 2026-08-03 (7d de 45 permitidos)" in txt
