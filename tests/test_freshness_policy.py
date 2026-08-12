"""UNA SOLA definición de "resultados frescos".

Bug real (2026-08-12T02:28Z): `feeds matrix` declaraba WTA full canonical
2026-08-11 (1d) [FRESH] y `production freshness: FRESH`, pero la puerta
prepartido excluía Svitolina–Swiatek con "los resultados WTA solo llegan hasta
2026-08-11 (< 2026-08-12)" — porque el gate exigía fecha de HOY
(`f >= now.date()`) mientras la capa de capacidades usaba <= 2 días.

Aquí se fija que capability_freshness, la matriz, el banner y el gate comparten
la MISMA política (`sync.is_full_results_fresh`). Solo pueden discrepar por una
razón explícita de contexto (cobertura por torneo), nunca por tener umbrales
distintos.
"""
import copy
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch
from betbot.prematch import gate
from betbot.sync import (FULL_RESULTS_FRESH_MAX_AGE_DAYS, capability_freshness,
                         full_results_status, is_full_results_fresh)

# instante real del caso del usuario
NOW = datetime(2026, 8, 12, 2, 28, tzinfo=timezone.utc)
TODAY = NOW.date()
YESTERDAY = date(2026, 8, 11)
LONG_AGO = date(2026, 8, 3)


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    c["paths"]["canonical_dir"] = str(canon)
    c["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    c["paths"]["manual_dir"] = str(tmp_path / "manual")
    c["feeds"]["commence_crosscheck"] = False
    rows = [("ATP", LONG_AGO, "fils_a", "ruud_c"),          # ATP muy atrasado
            ("WTA", YESTERDAY, "svitolina_e", "swiatek_i")]  # WTA de ayer
    pd.DataFrame([{
        "tour": t, "date": d, "tournament": "T0", "series": "", "surface": "Hard",
        "indoor": False, "round": "R1", "best_of": 3, "player_a": a, "player_b": b,
        "raw_name_a": a, "raw_name_b": b, "label_a_wins": 1, "status": "completed",
        "sets_a": 2, "sets_b": 0, "games_a": 12, "games_b": 6, "set1_winner_a": 1,
        "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test", "match_id": f"{t}_{d}_{a}__{b}"}
        for t, d, a, b in rows]).to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1, "display_name": p,
                   "hand": None, "dob": None}
                  for p, t in (("fils_a", "ATP"), ("ruud_c", "ATP"),
                               ("svitolina_e", "WTA"), ("swiatek_i", "WTA"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return c


def _match(tour="WTA", tournament="Canadian Open", p1="Svitolina E.",
           p2="Swiatek I.", start=None, source="wta_official"):
    return FeedMatch(
        date=TODAY, tour=tour, tournament=tournament, player1=p1, player2=p2,
        level="main", is_doubles=False, is_qualifying=False, round="SF",
        best_of=3, surface="Hard", source=source, event_id=f"e:{p1}",
        scheduled_at_utc=start or datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc),
        status="scheduled", source_updated_at=NOW.isoformat(), start_tz="UTC",
        authoritative=True, trust_tier="schedule_only")


# --------------------------------------------------- A) ayer sigue siendo fresco

def test_A_results_from_yesterday_do_not_block(cfg):
    """WTA hasta 2026-08-11, ahora 2026-08-12: capability dice FRESH, así que
    el gate NO puede excluir por resultados desactualizados."""
    caps = capability_freshness(cfg, today=TODAY)
    assert caps["WTA"]["full_scores"]["status"] == "FRESH"
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"ATP": LONG_AGO, "WTA": YESTERDAY}, coverage={})
    assert res.counts().get("results_feed_stale_pre_match_unverified", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["results_check"].startswith("full_frescos")
    assert "dentro de política" in conf["results_check"]


@pytest.mark.parametrize("age", range(0, FULL_RESULTS_FRESH_MAX_AGE_DAYS + 1))
def test_A2_every_age_within_policy_is_accepted(cfg, age):
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": TODAY - timedelta(days=age)}, coverage={})
    assert len(res.eligible) == 1, f"edad {age}d debería estar dentro de política"


# --------------------------------------------------- B) fuera de política bloquea

def test_B_outside_policy_without_coverage_is_blocked(cfg):
    over = FULL_RESULTS_FRESH_MAX_AGE_DAYS + 1
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": TODAY - timedelta(days=over)}, coverage={})
    assert res.eligible == []
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    d = res.details[m.pair_key]
    assert f"fuera de la política de {FULL_RESULTS_FRESH_MAX_AGE_DAYS}d" in d


def test_B2_atp_far_behind_without_coverage_still_fails_closed(cfg):
    """ATP full 2026-08-03 el 2026-08-12 y Cincinnati sin cobertura: intacto."""
    caps = capability_freshness(cfg, today=TODAY)
    assert caps["ATP"]["full_scores"]["status"] == "STALE"
    m = _match(tour="ATP", tournament="Cincinnati", p1="Fils A.", p2="Ruud C.",
               source="oddsapi")
    res = gate([m], cfg, now=NOW, completed_idx={}, registry={"fils_a", "ruud_c"},
               fresh_until={"ATP": LONG_AGO}, coverage={})
    assert res.eligible == []
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1


# --------------------------------------------------- C) cobertura por torneo

def test_C_stale_global_with_fresh_tournament_coverage_is_eligible(cfg):
    """El rescate por cobertura de torneo sigue funcionando igual."""
    m = _match(tour="ATP", tournament="Canadian Open", p1="Fils A.", p2="Ruud C.",
               source="oddsapi")
    res = gate([m], cfg, now=NOW, completed_idx={}, registry={"fils_a", "ruud_c"},
               fresh_until={"ATP": LONG_AGO},
               coverage={"ATP": {"canadian open": date(2026, 8, 11)}})
    assert len(res.eligible) == 1
    assert res.confirmations[m.pair_key]["results_check"].startswith(
        "cobertura_outcomes_torneo")


# --------------------------------------------------- D) evidencia de juego manda

def test_D_fresh_results_do_not_rescue_a_played_match(cfg):
    """WTA de ayer (dentro de política) pero el partido YA está en el índice de
    completados: sigue bloqueado por juego, no por frescura."""
    m = _match()
    idx = {("WTA", "svitolina_e", "swiatek_i"):
           [{"date": TODAY, "status": "completed", "tournament": "Canadian Open"}]}
    res = gate([m], cfg, now=NOW, completed_idx=idx,
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": YESTERDAY}, coverage={})
    assert res.eligible == []
    assert res.counts()["already_completed"] == 1
    assert res.counts().get("results_feed_stale_pre_match_unverified", 0) == 0


def test_D2_fresh_results_do_not_rescue_a_started_match(cfg):
    # -45 min: 01:43, evita el patrón :58/:59 del detector de placeholders
    started = _match(start=NOW - timedelta(minutes=45))
    res = gate([started], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": YESTERDAY}, coverage={})
    assert res.eligible == [] and res.counts()["ya_comenzado"] == 1


# --------------------------------------------------- E) las capas coinciden

@pytest.mark.parametrize("age_days", [0, 1, 2, 3, 7, 30])
def test_E_matrix_capability_and_gate_agree(cfg, age_days):
    """Para el MISMO (tour, fecha), capacidad/matriz/banner/gate dan el mismo
    veredicto de frescura. Cualquier discrepancia sería un bug, no una
    decisión de diseño."""
    until = TODAY - timedelta(days=age_days)
    expected_fresh = is_full_results_fresh(until, TODAY)

    # 1) política
    assert full_results_status(until, TODAY) == ("FRESH" if expected_fresh else "STALE")

    # 2) gate: solo bloquea por frescura cuando la política dice STALE
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": until}, coverage={})
    blocked = res.counts().get("results_feed_stale_pre_match_unverified", 0) == 1
    assert blocked is (not expected_fresh)

    # 3) banner: dentro de política no genera aviso de resultados
    from betbot.picks import _within_policy, degraded_banner
    assert _within_policy(str(until), str(TODAY)) is expected_fresh
    banner = degraded_banner({
        "date": str(TODAY),
        "results_freshness": {"WTA": str(until), "ATP": str(TODAY)},
        "results_coverage": {"WTA": {"production_feature_freshness":
                                     "FRESH" if expected_fresh else "STALE"}},
        "analyzed_coverage": {"WTA": {"n_matches": 1, "found_in_window": 1,
                                      "blocked": 0, "covered": [], "uncovered": []}},
        "rankings_status": {}})
    assert (banner is None) is expected_fresh


def test_E2_capability_matches_policy_on_real_store(cfg):
    caps = capability_freshness(cfg, today=TODAY)
    for tour, until in (("ATP", LONG_AGO), ("WTA", YESTERDAY)):
        assert caps[tour]["full_scores"]["status"] == full_results_status(until, TODAY)


def test_E3_single_threshold_constant_is_not_duplicated():
    """Nadie puede reintroducir su propio umbral: el gate y el banner citan la
    política, no un número suelto."""
    import pathlib
    gate_src = pathlib.Path("src/betbot/prematch.py").read_text()
    assert "is_full_results_fresh" in gate_src
    assert ">= now.date()" not in gate_src          # el umbral antiguo, fuera
    picks_src = pathlib.Path("src/betbot/picks.py").read_text()
    assert "is_full_results_fresh" in picks_src


# --------------------------------------------------- mensajes

def test_within_policy_is_never_reported_as_a_failure(cfg):
    """§5: 'resultados hasta ayer' dentro de política no puede aparecer como
    motivo de exclusión."""
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"},
               fresh_until={"WTA": YESTERDAY}, coverage={})
    assert res.details == {} or all(
        "solo llegan hasta" not in v for v in res.details.values())
    conf = res.confirmations[m.pair_key]
    assert "2026-08-11" in conf["results_check"] and "1d" in conf["results_check"]
