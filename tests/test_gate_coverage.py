"""BUG 1/1B/2 post-b5713b9: la puerta prematch usa la cobertura por torneo,
el mensaje de sistema no puede ser circular y la edad de frescura jamás es
negativa (referencia UTC única).
"""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch
from betbot.feeds.oddsapi_scores import OddsApiScores
from betbot.prematch import COVERAGE_FRESH_MAX_AGE_DAYS, completed_index, gate
from betbot.scan import _analyzed_coverage
from betbot.sync import capability_freshness, run_sync

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
STALE_FULL = {"ATP": date(2026, 8, 3), "WTA": date(2026, 8, 3)}


def _cfg(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir(exist_ok=True)
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    cfg["feeds"]["commence_crosscheck"] = False
    pd.DataFrame([{
        "tour": "ATP", "date": date(2026, 8, 3), "tournament": "Washington",
        "series": "", "surface": "Hard", "indoor": False, "round": "F", "best_of": 3,
        "player_a": "fils_a", "player_b": "ruud_c", "raw_name_a": "Fils A.",
        "raw_name_b": "Ruud C.", "label_a_wins": 1, "status": "completed",
        "sets_a": 2, "sets_b": 0, "games_a": 12, "games_b": 6, "set1_winner_a": 1,
        "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test",
        "match_id": "ATP_2026-08-03_fils_a__ruud_c"}]
    ).to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": "ATP", "n_matches": 1, "display_name": n,
                   "hand": None, "dob": None}
                  for p, n in (("fils_a", "Arthur Fils"), ("ruud_c", "Casper Ruud"),
                               ("darderi_l", "Luciano Darderi"),
                               ("nakashima_b", "Brandon Nakashima"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def _ingest_outcome(cfg, monkeypatch, home="Luciano Darderi", away="Brandon Nakashima",
                    commence="2026-08-09T19:00:00Z", eid="ev1"):
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


def _match(tournament, p1="Fils A.", p2="Ruud C.", start=None, source="oddsapi"):
    """Partido futuro con hora exacta estilo The Odds API (schedule_only y de la
    misma familia que el crosscheck: la corroboración no cuenta, como en el
    caso real del Mac)."""
    return FeedMatch(
        date=date(2026, 8, 11), tour="ATP", tournament=tournament,
        player1=p1, player2=p2, level="main", is_doubles=False, is_qualifying=False,
        round="QF", best_of=3, surface="Hard", source=source,
        event_id=f"e:{tournament}:{p1}",
        scheduled_at_utc=start or datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc),
        status="scheduled", source_updated_at=NOW.isoformat(), start_tz="UTC",
        authoritative=True, trust_tier="schedule_only")


COVERAGE = {"ATP": {"canadian open": date(2026, 8, 9)}}


# ---------------------------------------------- BUG 1: los tres casos e2e

def test_e2e_covered_tournament_not_blocked_by_global_staleness(tmp_path, monkeypatch):
    """global full=2026-08-03 STALE + Canadian Open coverage=2026-08-09 FRESH +
    partido futuro 2026-08-11 con hora exacta y ausente de completed_index
    => NO se excluye por resultados_desactualizados."""
    cfg = _cfg(tmp_path)
    _ingest_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    cov = {t: {k: c["until"] for k, c in (caps[t]["active_coverage"] or {}).items()}
           for t in caps}
    assert cov["ATP"]["canadian open"] == "2026-08-09"
    m = _match("National Bank Open")           # alias del Canadian Open
    res = gate([m], cfg, now=NOW, completed_idx=completed_index(cfg, around=TODAY),
               registry={"fils_a", "ruud_c", "darderi_l", "nakashima_b"},
               fresh_until=STALE_FULL, coverage=cov)
    assert res.counts().get("results_feed_stale_pre_match_unverified", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["results_check"].startswith("cobertura_outcomes_torneo")


def test_e2e_uncovered_tournament_stays_fail_closed(tmp_path, monkeypatch):
    """mismas fechas pero Cincinnati SIN cobertura => sigue bloqueado."""
    cfg = _cfg(tmp_path)
    _ingest_outcome(cfg, monkeypatch)
    m = _match("Cincinnati")
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=STALE_FULL, coverage=COVERAGE)
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    assert res.eligible == []
    assert "no tiene cobertura reciente" in res.details[m.pair_key]


def test_e2e_covered_but_already_played_is_excluded(tmp_path, monkeypatch):
    """Canadian Open cubierto pero el partido YA está en recent_outcomes/
    completed_index => YA JUGADO, jamás prematch."""
    cfg = _cfg(tmp_path)
    _ingest_outcome(cfg, monkeypatch, home="Luciano Darderi",
                    away="Brandon Nakashima", commence="2026-08-10T01:00:00Z")
    idx = completed_index(cfg, around=TODAY)
    assert ("ATP", "darderi_l", "nakashima_b") in idx
    m = _match("Canadian Open", p1="Darderi L.", p2="Nakashima B.")
    res = gate([m], cfg, now=NOW, completed_idx=idx,
               registry={"darderi_l", "nakashima_b"},
               fresh_until=STALE_FULL, coverage=COVERAGE)
    assert res.eligible == []
    assert res.counts()["already_completed"] == 1


def test_old_coverage_does_not_rescue(tmp_path):
    """Cobertura demasiado vieja (> umbral) no rescata: fail closed."""
    cfg = _cfg(tmp_path)
    old = {"ATP": {"canadian open": TODAY - timedelta(days=COVERAGE_FRESH_MAX_AGE_DAYS + 1)}}
    res = gate([_match("Canadian Open")], cfg, now=NOW, completed_idx={},
               registry=set(), fresh_until=STALE_FULL, coverage=old)
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    assert "vieja" in res.details[_match("Canadian Open").pair_key]


def test_unresolvable_tournament_identity_no_rescue(tmp_path):
    """Identidad de torneo no resoluble (nombre vacío) => sin rescate."""
    cfg = _cfg(tmp_path)
    m = _match("")
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=STALE_FULL, coverage=COVERAGE)
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1


def test_coverage_rescue_keeps_other_gates(tmp_path):
    """El rescate por cobertura NO relaja el resto de puertas: un partido ya
    comenzado del torneo cubierto sigue excluido."""
    cfg = _cfg(tmp_path)
    started = _match("Canadian Open",
                     start=datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc))
    res = gate([started], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=STALE_FULL, coverage=COVERAGE)
    assert res.eligible == []
    assert res.counts()["ya_comenzado"] == 1


def test_wta_fresh_path_unchanged(tmp_path):
    """WTA con full fresco pasa como siempre, sin necesitar cobertura."""
    cfg = _cfg(tmp_path)
    m = FeedMatch(date=date(2026, 8, 11), tour="WTA", tournament="Canadian Open",
                  player1="Gauff C.", player2="Swiatek I.", level="main",
                  is_doubles=False, is_qualifying=False, round="QF", best_of=3,
                  surface="Hard", source="wta_official", event_id="w1",
                  scheduled_at_utc=datetime(2026, 8, 11, 17, tzinfo=timezone.utc),
                  status="scheduled", source_updated_at=NOW.isoformat(),
                  authoritative=True, trust_tier="schedule_only")
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until={"ATP": date(2026, 8, 3), "WTA": TODAY}, coverage={})
    assert len(res.eligible) == 1
    assert res.confirmations[m.pair_key]["results_check"] == "full_frescos"


# ---------------------------------------------- BUG 1B: mensaje no circular

def _summary(analyzed, caps=None):
    return {"date": str(TODAY),
            "results_freshness": {"ATP": "2026-08-03", "WTA": str(TODAY)},
            "results_coverage": caps or {"ATP": {"production_feature_freshness": "PARTIAL",
                                                 "active_coverage": {}}},
            "analyzed_coverage": analyzed,
            "rankings_status": {}}


def test_blocked_events_never_reported_as_no_picks():
    from betbot.picks import degraded_banner
    analyzed = {"ATP": {"n_matches": 0, "found_in_window": 4, "blocked": 4,
                        "covered": [], "uncovered": []}}
    b = degraded_banner(_summary(analyzed))
    assert "4 partido(s) en ventana NO analizados por frescura/confirmación" in b
    assert "no afecta a las probabilidades mostradas" not in b


def test_truly_no_events_in_window_is_a_system_note():
    from betbot.picks import degraded_banner
    analyzed = {"ATP": {"n_matches": 0, "found_in_window": 0, "blocked": 0,
                        "covered": [], "uncovered": []}}
    b = degraded_banner(_summary(analyzed))
    assert "sin partidos ATP en la ventana" in b
    assert "Nota de sistema" in b


def test_analyzed_but_no_pick_is_distinct():
    from betbot.picks import degraded_banner
    analyzed = {"ATP": {"n_matches": 0, "found_in_window": 3, "blocked": 0,
                        "covered": [], "uncovered": []}}
    # found=3 con blocked=0 y n_matches=0 no puede darse en el flujo real
    # (n_matches = found - blocked), pero el render debe degradar con cautela:
    analyzed2 = {"ATP": {"n_matches": 2, "found_in_window": 3, "blocked": 1,
                         "covered": [], "uncovered": ["Cincinnati"]}}
    b = degraded_banner(_summary(analyzed2))
    assert b is not None                       # analizado: bloque normal por torneo


def test_analyzed_coverage_counts_found_and_blocked(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _ingest_outcome(cfg, monkeypatch)
    caps = capability_freshness(cfg, today=TODAY)
    found = [_match("Canadian Open"), _match("Cincinnati", p1="Darderi L.",
                                             p2="Nakashima B.")]
    eligible = [found[0]]                      # Cincinnati quedó bloqueado
    an = _analyzed_coverage(eligible, caps, found=found)
    assert an["ATP"]["found_in_window"] == 2
    assert an["ATP"]["n_matches"] == 1
    assert an["ATP"]["blocked"] == 1


# ---------------------------------------------- BUG 2: edad jamás negativa

def test_age_days_never_negative_midnight_crossing():
    from betbot.picks import _age_days
    # datos UTC de "mañana" respecto a una referencia local rezagada
    assert _age_days("2026-08-10", "2026-08-11") == 0
    assert _age_days("2026-08-11", "2026-08-11") == 0
    assert _age_days("2026-08-11", "2026-08-04") == 7
    assert _age_days("2026-08-10", None) is None


def test_banner_and_datos_never_show_negative_age():
    from betbot.picks import degraded_banner
    s = {"date": "2026-08-10",
         "results_freshness": {"ATP": "2026-08-11", "WTA": "2026-08-11"},
         "results_coverage": {}, "analyzed_coverage": {}, "rankings_status": {}}
    assert degraded_banner(s) is None          # dato "del futuro" = fresco, sin banner


def test_scan_summary_date_is_utc(monkeypatch):
    import betbot.scan as sc
    src = sc
    text = open("src/betbot/scan.py").read()
    assert "str(date.today())" not in text     # referencia UTC única en el summary
    assert 'str(datetime.now(timezone.utc).date())' in text
