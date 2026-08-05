"""Calendarios de respaldo SIN credenciales: TheSportsDB (ATP+WTA) y la API de
primera parte de la WTA. Ambos autoritativos (id + fecha-hora completa +
estado); lo que no se puede leer con certeza queda `unknown` y la puerta
prepartido lo excluye.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from betbot.config import load_config
from betbot.feeds.base import dedupe_matches
from betbot.feeds.thesportsdb import (PUBLIC_FREE_KEY, TheSportsDBCalendar,
                                      normalize_sportsdb_status)
from betbot.feeds.wta_official import WtaOfficialCalendar, normalize_match_state
from betbot.prematch import gate

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
# resultados al dia: desactiva la puerta de frescura salvo donde se pruebe
FRESH_OK = {"ATP": NOW.date(), "WTA": NOW.date()}
NOW_ISO = NOW.isoformat()
LO, HI = NOW - timedelta(days=1), NOW + timedelta(hours=48)


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    # tests deterministas: el contraste con fuentes externas se prueba aparte
    c.setdefault("feeds", {})["commence_crosscheck"] = False
    return c


# ---------------------------------------------------------------- TheSportsDB

def _tsdb_event(**kw):
    base = {"idEvent": "2100001", "strEvent": "Canadian Open",
            "strLeague": "ATP Tour", "strHomeTeam": "Carlos Alcaraz",
            "strAwayTeam": "Jack Draper", "strTimestamp": "2026-08-05T17:00:00",
            "strStatus": "NS", "strRound": "Round of 32",
            "dateEvent": "2026-08-05", "strTime": "17:00:00"}
    base.update(kw)
    return base


def test_tsdb_parses_id_time_status_players():
    out, n_raw, n_skip = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event()]}, NOW_ISO, LO, HI)
    assert n_raw == 1 and len(out) == 1
    m = out[0]
    assert m.authoritative is True and m.source == "thesportsdb"
    assert m.tour == "ATP" and m.status == "scheduled"
    assert m.scheduled_at_utc == datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)
    assert {m.player1, m.player2} == {"Alcaraz C.", "Draper J."}
    assert m.event_id == "tsdb:2100001:alcaraz_c__draper_j"  # estable, sin fecha
    assert m.round == "Round of 32"


def test_tsdb_null_events_and_non_tennis_leagues():
    assert TheSportsDBCalendar.parse_day({"events": None}, NOW_ISO)[0] == []
    assert TheSportsDBCalendar.parse_day({}, NOW_ISO)[0] == []
    out, _, skipped = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strLeague="ITF Men")]}, NOW_ISO, LO, HI)
    assert out == [] and skipped == 1        # ni ATP ni WTA


def test_tsdb_timestamp_fallback_to_date_plus_time():
    out, _, _ = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strTimestamp="")]}, NOW_ISO, LO, HI)
    assert len(out) == 1
    assert out[0].scheduled_at_utc == datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)
    # sin fecha-hora completa NO entra (una hora suelta no basta)
    out2, _, skipped = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strTimestamp="", dateEvent="", strTime="17:00:00")]},
        NOW_ISO, LO, HI)
    assert out2 == [] and skipped == 1


def test_tsdb_status_normalisation_and_unknown_is_fail_closed(cfg):
    assert normalize_sportsdb_status("NS") == "scheduled"
    assert normalize_sportsdb_status("Match Finished") == "completed"
    assert normalize_sportsdb_status("FT") == "completed"
    assert normalize_sportsdb_status("In Progress") == "live"
    assert normalize_sportsdb_status("Postponed") == "postponed"
    assert normalize_sportsdb_status("Cancelled") == "cancelled"
    # respaldo por subcadenas (los valores no están normalizados aguas arriba)
    assert normalize_sportsdb_status("Second Half Finished") == "completed"
    assert normalize_sportsdb_status("Match Abandoned") == "cancelled"
    # valor no reconocido -> unknown -> la puerta lo excluye
    assert normalize_sportsdb_status("¿?") == "unknown"
    assert normalize_sportsdb_status("") == "unknown"
    out, _, _ = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strStatus="algo raro")]}, NOW_ISO, LO, HI)
    res = gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK)
    assert res.eligible == [] and res.counts()["estado_desconocido"] == 1


def test_tsdb_finished_and_live_never_eligible(cfg):
    for status in ("Match Finished", "In Progress", "Cancelled", "Postponed"):
        out, _, _ = TheSportsDBCalendar.parse_day(
            {"events": [_tsdb_event(strStatus=status)]}, NOW_ISO, LO, HI)
        assert gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK).eligible == []


def test_tsdb_doubles_excluded():
    out, _, skipped = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strHomeTeam="Guo Hanyu / Kristina Mladenovic",
                                strAwayTeam="Shuko Aoyama / En-Shuo Liang")]},
        NOW_ISO, LO, HI)
    assert out == [] and skipped == 1


def test_tsdb_window_is_applied():
    out, _, skipped = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strTimestamp="2026-08-20T17:00:00")]}, NOW_ISO, LO, HI)
    assert out == [] and skipped == 1


def test_tsdb_uses_public_documented_key_by_default():
    """La clave gratuita es un valor público de la documentación: ni alta, ni
    cuenta, ni secreto — por eso vive en la config, no en el entorno."""
    assert TheSportsDBCalendar().key == PUBLIC_FREE_KEY
    assert TheSportsDBCalendar(api_key="123").key == "123"


# ------------------------------------------------------------- WTA de primera parte

def _wta_match(**kw):
    base = {"MatchID": 55021, "EventID": 901, "EventYear": 2026, "RoundID": "R32",
            "DrawMatchType": "S", "DrawLevelType": "P",
            "PlayerIDA": "320124", "PlayerIDB": "326408",
            "PlayerNameFirstA": "Iga", "PlayerNameLastA": "Swiatek",
            "PlayerNameFirstB": "Coco", "PlayerNameLastB": "Gauff",
            "MatchState": "U", "MatchTimeStamp": "2026-08-05T18:30:00Z",
            "NotBeforeISOTime": "", "TournamentName": "Toronto"}
    base.update(kw)
    return base


def test_wta_parses_id_time_status_players():
    out, n_raw, _ = WtaOfficialCalendar.parse_matches([_wta_match()], NOW_ISO, LO, HI)
    assert n_raw == 1 and len(out) == 1
    m = out[0]
    assert m.tour == "WTA" and m.authoritative is True and m.source == "wta_official"
    assert m.status == "scheduled"
    assert m.scheduled_at_utc == datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    assert {m.player1, m.player2} == {"Swiatek I.", "Gauff C."}
    assert m.event_id == "wta:901:2026:55021:gauff_c__swiatek_i"  # estable, sin fecha


def test_wta_response_may_be_list_or_wrapped():
    for payload in ([_wta_match()], {"Matches": [_wta_match()]},
                    {"matches": [_wta_match()]}, {"data": [_wta_match()]}):
        out, _, _ = WtaOfficialCalendar.parse_matches(payload, NOW_ISO, LO, HI)
        assert len(out) == 1
    assert WtaOfficialCalendar.parse_matches({"otra": 1}, NOW_ISO)[0] == []


def test_wta_match_state_only_trusts_confirmed_codes(cfg):
    assert normalize_match_state("U") == "scheduled"
    assert normalize_match_state("P") == "live"
    assert normalize_match_state("F") == "completed"
    # C/S/D/I/L tienen evidencia CONTRADICTORIA entre implementaciones reales:
    # se tratan como desconocidos, nunca como jugables
    for code in ("C", "S", "D", "I", "L", "X", ""):
        assert normalize_match_state(code) == "unknown"
        out, _, _ = WtaOfficialCalendar.parse_matches(
            [_wta_match(MatchState=code)], NOW_ISO, LO, HI)
        assert gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK).eligible == []


def test_wta_finished_and_live_never_eligible(cfg):
    for code in ("F", "P"):
        out, _, _ = WtaOfficialCalendar.parse_matches(
            [_wta_match(MatchState=code)], NOW_ISO, LO, HI)
        assert gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK).eligible == []


def test_wta_skips_tbd_and_doubles():
    out, _, skipped = WtaOfficialCalendar.parse_matches(
        [_wta_match(PlayerIDA="TBD"), _wta_match(DrawMatchType="D")], NOW_ISO, LO, HI)
    assert out == [] and skipped == 2


def test_wta_not_before_time_never_confirms(cfg):
    """'Not before 19:00' es una COTA INFERIOR, no un inicio: no puede
    confirmar hora ni producir una señal."""
    out, _, _ = WtaOfficialCalendar.parse_matches(
        [_wta_match(MatchTimeStamp="", NotBeforeISOTime="2026-08-05T19:00:00Z")],
        NOW_ISO, LO, HI)
    assert len(out) == 1
    m = out[0]
    assert m.scheduled_at_utc is None and m.time_precision == "unknown"
    assert "cota inferior" in m.time_note
    assert gate([m], cfg, now=NOW, completed_idx={}).eligible == []
    # sin ninguna de las dos horas, el partido no entra siquiera
    out2, _, skipped = WtaOfficialCalendar.parse_matches(
        [_wta_match(MatchTimeStamp="", NotBeforeISOTime="")], NOW_ISO, LO, HI)
    assert out2 == [] and skipped == 1


# ------------------------------------------------------------- integración

def test_fallback_order_and_espn_first(cfg):
    from betbot.scan import default_sources
    cals, odds = default_sources(cfg)
    names = [c.name for c in cals]
    assert names[0] == "espn"                       # ESPN sigue siendo la primera
    assert "thesportsdb" in names and "wta_official" in names
    assert all(getattr(c, "authoritative", False) for c in cals)
    assert "github_te" not in names                 # nunca como calendario
    assert "github_te" in [o.name for o in odds]    # sí como fuente de cuotas


def test_sources_dedupe_across_calendars(cfg):
    """El mismo partido en ESPN y en el respaldo cuenta una sola vez, aunque
    venga con los jugadores en orden invertido."""
    tsdb, _, _ = TheSportsDBCalendar.parse_day({"events": [_tsdb_event()]}, NOW_ISO, LO, HI)
    otro, _, _ = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(idEvent="999", strHomeTeam="Jack Draper",
                                strAwayTeam="Carlos Alcaraz")]}, NOW_ISO, LO, HI)
    merged = dedupe_matches(tsdb + otro)
    assert len(merged) == 1
    res = gate(merged, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK)
    assert len(res.eligible) == 1


def test_fallback_takes_over_when_espn_fails(cfg, monkeypatch):
    """Con ESPN caído, un respaldo sano evita el FAIL_CLOSED y confirma partidos
    — siempre que los resultados esten al dia (si no, ver el test siguiente)."""
    from betbot.feeds.base import SourceStatus
    from betbot.scan import run_scan
    monkeypatch.setattr("betbot.prematch.results_freshness",
                        lambda cfg: {"ATP": datetime.now(timezone.utc).date(),
                                     "WTA": datetime.now(timezone.utc).date()})

    class DeadEspn:
        name = "espn"
        authoritative = True

        def fetch_matches(self, window_hours):
            raise RuntimeError("403 desde la red del contenedor")

    class LiveFallback:
        name = "thesportsdb"
        authoritative = True

        def fetch_matches(self, window_hours):
            ms, _, _ = TheSportsDBCalendar.parse_day(
                {"events": [_tsdb_event(
                    strTimestamp=(datetime.now(timezone.utc) + timedelta(hours=5))
                    .replace(minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S"))]},
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc) - timedelta(days=1),
                datetime.now(timezone.utc) + timedelta(hours=48))
            return ms, SourceStatus(name=self.name, ok=True, authoritative=True,
                                    n_items=len(ms))

    res = run_scan(cfg, calendar_sources=[DeadEspn(), LiveFallback()], odds_sources=[],
                   structured_providers=[], log_ledger=False, show_rejected=True)
    assert res.summary["fail_closed"] is False
    assert res.summary["calendar_confirmed"] == 1
    conf = list(res.summary["confirmations"].values())[0]
    assert conf["source"] == "thesportsdb" and conf["status"] == "scheduled"
    assert conf["start_utc"] and conf["time_precision"] == "exact"


def test_schedule_only_source_blocked_when_results_are_stale(cfg, monkeypatch):
    """Una fuente que publica estado SIN evidencia de juego no basta si los
    resultados locales no llegan al dia en curso: no se puede saber si el
    partido ya se jugo."""
    monkeypatch.setattr("betbot.prematch.results_freshness",
                        lambda cfg: {"WTA": NOW.date() - timedelta(days=2)})
    out, _, _ = TheSportsDBCalendar.parse_day(
        {"events": [_tsdb_event(strLeague="WTA Tour",
                                strHomeTeam="Iga Swiatek", strAwayTeam="Coco Gauff")]},
        NOW_ISO, LO, HI)
    assert out and out[0].trust_tier == "schedule_only"
    res = gate(out, cfg, now=NOW, completed_idx={})
    assert res.eligible == []
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    detalle = list(res.details.values())[0]
    assert "sin evidencia de juego" in detalle and "2026-08-03" in detalle


def test_fail_closed_still_applies_when_all_calendars_fail(cfg):
    from betbot.prematch import FAIL_CLOSED_MSG
    from betbot.scan import run_scan

    class Dead:
        def __init__(self, name):
            self.name = name
        authoritative = True

        def fetch_matches(self, window_hours):
            raise RuntimeError("caida")

    res = run_scan(cfg, calendar_sources=[Dead("espn"), Dead("thesportsdb"),
                                          Dead("wta_official")],
                   odds_sources=[], structured_providers=[], log_ledger=False)
    assert res.summary["fail_closed"] is True
    assert res.summary["fail_closed_message"] == FAIL_CLOSED_MSG
