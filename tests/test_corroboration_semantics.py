"""AUSENCIA != CONTRADICCIÓN en la corroboración de calendario.

Caso real que lo motivó (Jodar R. vs Nakashima B., ATP Canadian Open,
2026-08-12T22:00:00Z): el índice independiente cubría el torneo pero no
listaba ESE partido, y el gate lo excluía como `conflicto_de_fuentes`. Una
fuente que no lista un partido no está contradiciendo que exista (snapshot
antiguo, feed incompleto, ventana distinta, cobertura parcial).

Semántica fijada aquí:
  conflicto_horario  la MISMA pareja con horas incompatibles  -> bloquea
  ya_comenzado       la MISMA pareja ya empezada, o la fuente lo declara
                     explícitamente completed/live               -> bloquea
  no_corroborado     el índice cubre el torneo pero no lista el partido
                                                                -> NO bloquea
  sin_datos          sin índice, sin cobertura, o snapshot demasiado viejo
"""
import copy
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch
from betbot.feeds.commence import (ABSENCE_MAX_SNAPSHOT_AGE_H, CommenceIndex,
                                   CommenceRecord, cross_check, trust_of)
from betbot.prematch import gate

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
FUTURE = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc)     # Jodar–Nakashima real
FRESH_FULL = {"ATP": TODAY, "WTA": TODAY}
STALE_FULL = {"ATP": date(2026, 8, 3), "WTA": date(2026, 8, 3)}
COVERAGE_FRESH = {"ATP": {"canadian open": date(2026, 8, 11)}}
COVERAGE_STALE = {"ATP": {"canadian open": date(2026, 8, 3)}}


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    c["paths"]["canonical_dir"] = str(canon)
    c["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    pd.DataFrame(columns=["tour", "date", "player_a", "player_b", "status",
                          "tournament"]).to_parquet(canon / "matches.parquet")
    return c


def _idx(pairs, fetched="2026-08-12T11:30:00Z", sport="tennis_atp_canadian_open",
         source="odds_api_mirror:alienorsutinn"):
    """Índice secundario con las parejas indicadas: [(a, b, commence, status)]."""
    idx = CommenceIndex()
    for a, b, when, status in pairs:
        rec = CommenceRecord(pair=tuple(sorted((a, b))), commence_utc=when,
                             event_id=f"ev:{a}", source=source, sport_key=sport,
                             fetched_at=fetched, players_raw=(a, b), status=status)
        idx.by_pair.setdefault(rec.pair, []).append(rec)
        idx.covered_tours.add(sport)
    idx.sources_ok.append(source)
    return idx


def _match(p1="Jodar R.", p2="Nakashima B.", start=FUTURE,
           tournament="Canadian Open", source="oddsapi", tour="ATP"):
    return FeedMatch(date=start.date(), tour=tour, tournament=tournament,
                     player1=p1, player2=p2, level="main", is_doubles=False,
                     is_qualifying=False, round="SF", best_of=3, surface="Hard",
                     source=source, event_id="primary:1", scheduled_at_utc=start,
                     status="scheduled", source_updated_at=NOW.isoformat(),
                     start_tz="UTC", authoritative=True, trust_tier="schedule_only")


# --------------------------------------------------- A) ausencia != conflicto

def test_A_absent_in_secondary_with_old_snapshot_is_not_conflict(cfg):
    """primary lista el matchup futuro; secondary NO lo lista y su snapshot es
    antiguo => NO conflicto (y su ausencia ni siquiera informa)."""
    old = (NOW - timedelta(hours=ABSENCE_MAX_SNAPSHOT_AGE_H + 1)).isoformat()
    idx = _idx([("otro_a", "otro_b", FUTURE, "")], fetched=old)
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=FRESH_FULL, commence_idx=idx, coverage={})
    assert res.counts().get("conflicto_de_fuentes", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "sin_datos"      # snapshot viejo: no informa
    assert conf["corroboration"] == "no_disponible"


def test_A2_absent_with_fresh_snapshot_is_not_corroborated_not_conflict(cfg):
    """Con snapshot reciente la ausencia se registra como no_corroborado, pero
    tampoco bloquea: el caso real Jodar–Nakashima."""
    idx = _idx([("otro_a", "otro_b", FUTURE, "")])
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=FRESH_FULL, commence_idx=idx, coverage={})
    assert res.counts().get("conflicto_de_fuentes", 0) == 0
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "no_corroborado"
    assert conf["corroboration"] == "ausente_en_secundaria"
    assert conf["schedule_check"] == "primary_exact_future"


# --------------------------------------------------- B) contradicción real

def test_B_same_matchup_different_time_is_conflict(cfg):
    """primary 22:00 vs secondary 18:00 sobre la MISMA pareja => conflicto."""
    idx = _idx([("jodar_r", "nakashima_b",
                 datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc), "")])
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == []
    assert res.counts()["conflicto_de_fuentes"] == 1
    d = res.details[m.pair_key]
    assert "22:00" in d and "18:00" in d               # ambas horas
    assert "primaria 'oddsapi'" in d and "trust" in d  # ambas fuentes y confianza
    assert "snapshot" in d


# --------------------------------------------------- C/D) cobertura outcomes

def test_C_covered_tournament_absent_secondary_is_eligible(cfg):
    """primary futuro + torneo con outcome coverage FRESH + no completado +
    secundaria que no lo lista => ELEGIBLE."""
    idx = _idx([("otro_a", "otro_b", FUTURE, "")])
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=STALE_FULL, commence_idx=idx, coverage=COVERAGE_FRESH)
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["results_check"].startswith("cobertura_outcomes_torneo")
    assert conf["corroboration"] == "ausente_en_secundaria"
    assert conf["schedule_check"] == "primary_exact_future"


def test_D_same_case_with_stale_coverage_fails_closed(cfg):
    """Mismo caso pero con la cobertura del torneo vieja => fail closed por las
    reglas de frescura existentes (no por conflicto)."""
    idx = _idx([("otro_a", "otro_b", FUTURE, "")])
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={}, registry=set(),
               fresh_until=STALE_FULL, commence_idx=idx, coverage=COVERAGE_STALE)
    assert res.eligible == []
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    assert res.counts().get("conflicto_de_fuentes", 0) == 0


# --------------------------------------------------- E) declaración explícita

@pytest.mark.parametrize("status", ["completed", "live", "in_play", "finished"])
def test_E_secondary_explicitly_not_upcoming_blocks(cfg, status):
    """La secundaria declara EXPLÍCITAMENTE el mismo evento como completed/live
    => bloquear (esto sí es información, no una ausencia)."""
    idx = _idx([("jodar_r", "nakashima_b", FUTURE, status)])
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == []
    assert res.counts()["ya_comenzado"] == 1
    assert "explícitamente" in res.details[m.pair_key]


def test_E2_declared_status_only_from_known_values():
    from betbot.feeds.commence import _declared_status
    assert _declared_status({"completed": True}) == "completed"
    assert _declared_status({"status": "LIVE"}) == "live"
    assert _declared_status({"completed": False}) == ""      # no afirma nada
    assert _declared_status({"status": "scheduled"}) == ""
    assert _declared_status({}) == ""


# --------------------------------------------------- F) WTA conflicto real

def test_F_wta_authoritative_vs_secondary_keeps_both_evidences(cfg):
    """Svitolina–Swiatek: wta_official 23:00Z vs independiente 19:00Z. Sigue
    siendo conflicto, pero el detalle conserva AMBAS fuentes con su snapshot y
    su nivel de confianza — no se trata igual que una ausencia."""
    idx = _idx([("svitolina_e", "swiatek_i",
                 datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc), "")],
               sport="tennis_wta_canadian_open")
    m = _match(p1="Svitolina E.", p2="Swiatek I.", tour="WTA",
               start=datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc),
               source="wta_official")
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"svitolina_e", "swiatek_i"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == []
    assert res.counts()["conflicto_de_fuentes"] == 1
    d = res.details[m.pair_key]
    assert "23:00" in d and "19:00" in d and "240 min" in d
    assert "primaria 'wta_official'" in d and "secundaria" in d
    assert "2026-08-12T11:30:00Z" in d                 # snapshot de la secundaria
    # la jerarquía queda registrada: wta_official manda sobre un espejo
    assert trust_of("wta_official") < trust_of("odds_api_mirror:alienorsutinn")


# --------------------------------------------------- jerarquía y unidad

def test_trust_hierarchy_order():
    assert (trust_of("wta_official") < trust_of("sportradar")
            < trust_of("oddsapi") < trust_of("thesportsdb")
            <= trust_of("odds_api_mirror:gmalbert") < trust_of("github_te"))
    assert trust_of("fuente_desconocida") == 4        # nivel espejo por defecto


def test_cross_check_verdicts_unit():
    idx = _idx([("a_x", "b_y", FUTURE, "")])
    # misma pareja y hora compatible -> coincide
    v = cross_check(("a_x", "b_y"), FUTURE, idx, NOW, tournament_hint="Canadian Open")
    assert v["verdict"] == "coincide" and v["trust"] == trust_of("odds_api_mirror")
    # torneo no cubierto -> sin_datos (no se concluye nada)
    v = cross_check(("q_1", "q_2"), FUTURE, idx, NOW, tournament_hint="Cincinnati")
    assert v["verdict"] == "sin_datos"
    # torneo cubierto y pareja ausente -> no_corroborado
    v = cross_check(("q_1", "q_2"), FUTURE, idx, NOW, tournament_hint="Canadian Open")
    assert v["verdict"] == "no_corroborado" and "NO es contradicción" in v["detail"]
    # sin índice -> sin_datos
    assert cross_check(("a_x", "b_y"), FUTURE, CommenceIndex(), NOW)["verdict"] == "sin_datos"


def test_stale_snapshot_still_provides_positive_information(cfg):
    """Un snapshot viejo no puede VETAR por ausencia, pero su información
    positiva (una hora concreta ya pasada) sigue valiendo."""
    old = (NOW - timedelta(hours=ABSENCE_MAX_SNAPSHOT_AGE_H + 5)).isoformat()
    past = NOW - timedelta(hours=2)
    idx = _idx([("jodar_r", "nakashima_b", past, "")], fetched=old)
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == [] and res.counts()["ya_comenzado"] == 1
