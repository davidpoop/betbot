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
           tournament="Canadian Open", source="oddsapi", tour="ATP",
           observed=None):
    return FeedMatch(date=start.date(), tour=tour, tournament=tournament,
                     player1=p1, player2=p2, level="main", is_doubles=False,
                     is_qualifying=False, round="SF", best_of=3, surface="Hard",
                     source=source, event_id="primary:1", scheduled_at_utc=start,
                     status="scheduled",
                     source_updated_at=(observed or NOW).isoformat(),
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

# ------------------------- HORA PROGRAMADA vs EVIDENCIA DE JUEGO -----------
# Una scheduled_start antigua puede quedar obsoleta (lluvia, orden de juego,
# partido anterior largo, cambio de pista, reprogramación). Un estado
# explícito de juego, no.

PAST_19H = datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc)   # ya pasada a las 20:00+
LATER = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)      # "ahora" tras las 19:00
STALE_11H = (LATER - timedelta(hours=11)).isoformat()          # espejo de 11 h
FRESH_30M = (LATER - timedelta(minutes=30)).isoformat()        # espejo reciente


def test_1_recent_primary_future_vs_stale_past_schedule_is_not_started(cfg):
    """primary reciente 22:00 scheduled + secondary 11 h viejo 19:00 scheduled,
    a las 21:00 => NO `ya_comenzado`: manda la primaria y se marca la
    secundaria como caducada."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, "")], fetched=STALE_11H)
    m = _match(start=FUTURE, observed=LATER)                   # 22:00, futura
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.counts().get("ya_comenzado", 0) == 0
    assert res.counts().get("conflicto_de_fuentes", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "secondary_stale_schedule"
    assert conf["corroboration"] == "secundaria_caducada"


def test_2_recent_primary_vs_recent_past_schedule_is_conflict(cfg):
    """Si AMBAS son suficientemente recientes y muestran 19:00 vs 22:00, la
    discrepancia sigue siendo un conflicto real (tolerancia 90 min)."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, "")], fetched=FRESH_30M)
    m = _match(start=FUTURE, observed=LATER)
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == []
    assert res.counts()["conflicto_de_fuentes"] == 1
    d = res.details[m.pair_key]
    assert "22:00" in d and "19:00" in d and "180 min" in d


@pytest.mark.parametrize("status,label", [("live", "live"), ("in_play", "in_play")])
def test_3_stale_secondary_with_explicit_live_blocks(cfg, status, label):
    """primary futuro + secondary VIEJO con status=live => `ya_comenzado`:
    la evidencia de juego explícita no caduca."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, status)], fetched=STALE_11H)
    m = _match(start=FUTURE, observed=LATER)
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == [] and res.counts()["ya_comenzado"] == 1
    d = res.details[m.pair_key]
    assert label in d and "no caduca" in d


def test_4_stale_secondary_with_explicit_completed_blocks(cfg):
    """primary futuro + secondary VIEJO con status=completed => bloquea."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, "completed")], fetched=STALE_11H)
    m = _match(start=FUTURE, observed=LATER)
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == [] and res.counts()["ya_comenzado"] == 1
    assert "completed" in res.details[m.pair_key]


def test_5_stale_past_schedule_alone_is_never_strong_evidence(cfg):
    """Sin primaria suficiente (estado de calendario caducado), una hora
    programada vieja NO se usa como evidencia de juego: el partido cae por las
    OTRAS reglas, jamás etiquetado falsamente como `ya_comenzado`."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, "")], fetched=STALE_11H)
    m = _match(start=FUTURE)
    m.source_updated_at = (NOW - timedelta(hours=30)).isoformat()   # estado viejo
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == []
    assert res.counts().get("ya_comenzado", 0) == 0
    assert res.counts()["estado_calendario_caducado"] == 1          # otra regla


def test_unknown_snapshot_age_is_treated_as_recent(cfg):
    """Sin marca de snapshot no se puede PROBAR que esté caducada: se trata
    como reciente (no se relaja la protección por falta de metadatos)."""
    idx = _idx([("jodar_r", "nakashima_b", PAST_19H, "")], fetched="")
    m = _match(start=FUTURE, observed=LATER)
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == [] and res.counts()["conflicto_de_fuentes"] == 1


def test_agreeing_recent_past_time_still_means_started(cfg):
    """Dentro de la tolerancia (las fuentes coinciden) y con la hora ya pasada,
    `ya_comenzado` sigue siendo la conclusión correcta."""
    agreed = LATER - timedelta(minutes=30)
    idx = _idx([("jodar_r", "nakashima_b", agreed, "")], fetched=FRESH_30M)
    m = _match(start=agreed + timedelta(minutes=20), observed=LATER)  # dentro de tolerancia
    res = gate([m], cfg, now=LATER, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.eligible == [] and res.counts()["ya_comenzado"] == 1
    d = res.details[m.pair_key]
    assert "ya pasada" in d or "ya pasado" in d


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


def test_stale_snapshot_schedule_does_not_prove_play(cfg):
    """CORRECCIÓN: una HORA PROGRAMADA sí caduca. Un snapshot viejo cuya única
    evidencia es scheduled_start < now NO puede producir `ya_comenzado`: en
    tenis el orden de juego se mueve por lluvia, partido anterior largo o
    reprogramación. Solo la evidencia de juego EXPLÍCITA no caduca."""
    old = (NOW - timedelta(hours=ABSENCE_MAX_SNAPSHOT_AGE_H + 5)).isoformat()
    past = NOW - timedelta(hours=2)
    idx = _idx([("jodar_r", "nakashima_b", past, "")], fetched=old)
    m = _match()
    res = gate([m], cfg, now=NOW, completed_idx={},
               registry={"jodar_r", "nakashima_b"}, fresh_until=FRESH_FULL,
               commence_idx=idx, coverage={})
    assert res.counts().get("ya_comenzado", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "secondary_stale_schedule"
    assert conf["corroboration"] == "secundaria_caducada"
