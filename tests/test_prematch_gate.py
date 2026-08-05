"""Regresión del fallo Tsitsipas–Fonseca (2026-08-05): BetBot no puede generar
ninguna señal salvo que una fuente de calendario AUTORITATIVA confirme el
partido como prepartido (identificador + hora de inicio + estado), y salvo que
el almacén local de resultados no lo contradiga.

Cubre los once casos exigidos + el parseo de una respuesta REAL de ESPN.
"""
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot import watchcmd
from betbot.config import load_config
from betbot.feeds.base import FeedMatch, SourceStatus, dedupe_matches, normalize_status
from betbot.feeds.espn import EspnFeed, espn_status
from betbot.feeds.github_te import GithubTEFeed
from betbot.prematch import FAIL_CLOSED_MSG, gate, signal_still_prematch
from betbot.schemas import OddsQuote

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"
FIXTURE = ROOT / "tests" / "fixtures" / "espn_tennis_scoreboard.json"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
needs_bundle = pytest.mark.skipif(not BUNDLE.exists(), reason="necesita modelos entrenados")


def _fm(p1, p2, *, tour="ATP", status="scheduled", start=None, authoritative=True,
        tournament="Montreal", updated=None, **kw):
    start = NOW + timedelta(hours=5) if start == "default" or start is None else start
    return FeedMatch(
        date=(start.date() if start else NOW.date()), tour=tour, tournament=tournament,
        player1=p1, player2=p2, source="cal_test", event_id=f"ev:{p1}:{p2}",
        scheduled_at_utc=start, status=status,
        source_updated_at=updated or NOW_ISO, start_tz="UTC",
        authoritative=authoritative, **kw)


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    # tests deterministas: el contraste con fuentes externas se prueba aparte
    c.setdefault("feeds", {})["commence_crosscheck"] = False
    return c


# --------------------------------------------------------------------------
# 1. github_te NO puede crear un evento elegible
# --------------------------------------------------------------------------

def test_github_te_cannot_create_eligible_event(cfg, monkeypatch):
    """La causa raíz: el feed no trae fecha, ni estado, ni hora; sus filas
    NUNCA pueden declararse prepartido."""
    fixture = {"last_updated": NOW.replace(tzinfo=None).isoformat(), "count": 1,
               "matches": [{"tournament": "Montreal", "time": "17:00",
                            "player1": "Alcaraz C.", "player2": "Draper J.",
                            "odds1": 1.5, "odds2": 2.6, "tour": "ATP"}]}
    monkeypatch.setattr("betbot.feeds.github_te.cache.get_json",
                        lambda url, ttl_seconds: fixture)
    feed = GithubTEFeed()
    assert feed.authoritative is False
    ms, st = feed.fetch_matches(48)
    assert len(ms) == 1
    m = ms[0]
    assert m.authoritative is False and m.status == "unknown" and m.scheduled_at_utc is None
    res = gate(ms, cfg, now=NOW, completed_idx={})
    assert res.eligible == []
    assert res.counts()["fuente_no_autoritativa"] == 1


# --------------------------------------------------------------------------
# 2. cuota sin evento confirmado -> orphan_quote
# --------------------------------------------------------------------------

def test_quote_without_confirmed_event_is_orphan(monkeypatch):
    fixture = {"last_updated": NOW.replace(tzinfo=None).isoformat(), "count": 2,
               "matches": [
                   {"tournament": "Montreal", "time": "17:00", "player1": "Alcaraz C.",
                    "player2": "Draper J.", "odds1": 1.5, "odds2": 2.6, "tour": "ATP"},
                   {"tournament": "Montreal", "time": "19:00", "player1": "Tsitsipas S.",
                    "player2": "Fonseca J.", "odds1": 2.12, "odds2": 1.71, "tour": "ATP"}]}
    monkeypatch.setattr("betbot.feeds.github_te.cache.get_json",
                        lambda url, ttl_seconds: fixture)
    feed = GithubTEFeed()
    confirmed = [_fm("Alcaraz C.", "Draper J.")]        # solo uno confirmado
    quotes, st = feed.fetch_odds(confirmed)
    assert {q.player_a for q in quotes} == {"Alcaraz C."}
    assert st.n_orphan == 1                             # Tsitsipas–Fonseca queda huérfana
    assert any("orphan_quote" in n for n in st.notes)
    # sin ningún evento confirmado, TODAS son huérfanas
    quotes2, st2 = feed.fetch_odds([])
    assert quotes2 == [] and st2.n_orphan == 2


# --------------------------------------------------------------------------
# 3-4. completed / live no se analizan
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("completed", "estado_no_prepartido"),
    ("live", "estado_no_prepartido"),
    ("cancelled", "estado_no_prepartido"),
    ("postponed", "estado_no_prepartido"),
    ("walkover", "estado_no_prepartido"),
    ("unknown", "estado_desconocido"),
])
def test_non_prematch_states_excluded(cfg, status, expected):
    res = gate([_fm("Alcaraz C.", "Draper J.", status=status)], cfg, now=NOW,
               completed_idx={})
    assert res.eligible == [] and res.counts()[expected] == 1


def test_scheduled_and_delayed_are_the_only_playable_states(cfg):
    ms = [_fm("Alcaraz C.", "Draper J.", status="scheduled"),
          _fm("Sinner J.", "Zverev A.", status="delayed")]
    res = gate(ms, cfg, now=NOW, completed_idx={})
    assert len(res.eligible) == 2


# --------------------------------------------------------------------------
# 5. un partido de AYER no se convierte en "hoy"
# --------------------------------------------------------------------------

def test_yesterday_match_is_not_promoted_to_today(cfg):
    ayer = NOW - timedelta(days=1)
    res = gate([_fm("Alcaraz C.", "Draper J.", status="scheduled", start=ayer,
                    updated=ayer.isoformat())], cfg, now=NOW, completed_idx={})
    assert res.eligible == []
    # el estado viejo se detecta antes incluso que la hora pasada
    assert "estado_calendario_caducado" in res.counts() or "ya_comenzado" in res.counts()
    # con el estado recién observado, el motivo es inequívoco: ya comenzó
    res2 = gate([_fm("Alcaraz C.", "Draper J.", status="scheduled", start=ayer)],
                cfg, now=NOW, completed_idx={})
    assert res2.eligible == [] and res2.counts()["ya_comenzado"] == 1


def test_safety_margin_before_start(cfg):
    """Margen configurable: un partido que empieza en 2 min ya no es apostable."""
    res = gate([_fm("Alcaraz C.", "Draper J.", start=NOW + timedelta(minutes=2))],
               cfg, now=NOW, completed_idx={})
    assert res.counts()["ya_comenzado"] == 1
    res2 = gate([_fm("Alcaraz C.", "Draper J.", start=NOW + timedelta(minutes=30))],
                cfg, now=NOW, completed_idx={})
    assert len(res2.eligible) == 1


def test_out_of_window_excluded(cfg):
    res = gate([_fm("Alcaraz C.", "Draper J.", start=NOW + timedelta(hours=72))],
               cfg, window_hours=48, now=NOW, completed_idx={})
    assert res.eligible == [] and res.counts()["fuera_de_ventana"] == 1


# --------------------------------------------------------------------------
# 6. sin hora de inicio no se analiza
# --------------------------------------------------------------------------

def test_no_scheduled_at_utc_not_analyzed(cfg):
    """Una hora suelta ("17:00") sin fecha real NO declara futuro un partido."""
    m = _fm("Alcaraz C.", "Draper J.", start=None)
    m.scheduled_at_utc = None
    m.start_local = "17:00"
    res = gate([m], cfg, now=NOW, completed_idx={})
    assert res.eligible == [] and res.counts()["sin_hora_de_inicio"] == 1


# --------------------------------------------------------------------------
# 7. evento invertido entre fuentes
# --------------------------------------------------------------------------

def test_inverted_event_between_sources_is_one_event():
    a = _fm("Alcaraz C.", "Draper J.")
    b = _fm("Draper J.", "Alcaraz C.", authoritative=False)
    b.source = "otra_fuente"
    out = dedupe_matches([a, b])
    assert len(out) == 1 and out[0].source == "cal_test"
    # y la autoritativa gana aunque llegue después
    out2 = dedupe_matches([b, a])
    assert len(out2) == 1 and out2[0].authoritative is True
    assert a.pair_key == b.pair_key


def test_pair_key_ignores_date_and_order():
    a = _fm("Alcaraz C.", "Draper J.", start=NOW + timedelta(hours=1))
    b = _fm("Draper J.", "Alcaraz C.", start=NOW + timedelta(hours=25))
    assert a.pair_key == b.pair_key and a.key != b.key


# --------------------------------------------------------------------------
# 8. el resultado local contradice al calendario
# --------------------------------------------------------------------------

def test_local_result_overrides_calendar(cfg):
    """El calendario dice 'scheduled' pero ya tenemos el resultado: no se analiza."""
    idx = {("ATP", "alcaraz_c", "draper_j"): [
        {"date": NOW.date(), "status": "completed", "tournament": "Montreal"}]}
    res = gate([_fm("Alcaraz C.", "Draper J.")], cfg, now=NOW, completed_idx=idx)
    assert res.eligible == [] and res.counts()["already_completed"] == 1
    # tolerancia de fechas: un resultado de hace 5 días es de otro partido
    idx_old = {("ATP", "alcaraz_c", "draper_j"): [
        {"date": NOW.date() - timedelta(days=5), "status": "completed", "tournament": "X"}]}
    res2 = gate([_fm("Alcaraz C.", "Draper J.")], cfg, now=NOW, completed_idx=idx_old)
    assert len(res2.eligible) == 1
    # retirada y walkover también cuentan como jugado
    for stt in ("retired", "walkover"):
        idx_r = {("ATP", "alcaraz_c", "draper_j"): [
            {"date": NOW.date(), "status": stt, "tournament": "Montreal"}]}
        assert gate([_fm("Alcaraz C.", "Draper J.")], cfg, now=NOW,
                    completed_idx=idx_r).counts().get("already_completed") == 1


def test_local_result_matching_resolves_initials_without_fuzzy(cfg):
    """'Struff J.' se resuelve a struff_j_l por extensión ÚNICA de iniciales;
    un apellido parecido NO se acepta."""
    registry = {"struff_j_l", "michelsen_a", "schruff_j"}
    idx = {("ATP", "michelsen_a", "struff_j_l"): [
        {"date": NOW.date(), "status": "completed", "tournament": "Montreal"}]}
    res = gate([_fm("Michelsen A.", "Struff J.")], cfg, now=NOW,
               completed_idx=idx, registry=registry)
    assert res.counts().get("already_completed") == 1
    # con dos candidatas la extensión no se aplica: no hay coincidencia difusa
    res2 = gate([_fm("Michelsen A.", "Struff J.")], cfg, now=NOW, completed_idx=idx,
                registry=registry | {"struff_j_x"})
    assert len(res2.eligible) == 1


# --------------------------------------------------------------------------
# 9. calendario caído -> FAIL_CLOSED
# --------------------------------------------------------------------------

class DeadCalendar:
    name = "cal_caido"
    authoritative = True

    def fetch_matches(self, window_hours):
        raise RuntimeError("fuente de calendario caida")


class LiveOddsOnly:
    """Fuente de cuotas que NO es calendario (como github_te)."""
    name = "solo_cuotas"
    authoritative = False
    markets = ["match_winner"]

    def __init__(self, matches, quotes):
        self.matches, self.quotes = matches, quotes

    def fetch_matches(self, window_hours):
        return self.matches, SourceStatus(name=self.name, ok=True, n_items=len(self.matches))

    def fetch_odds(self, matches):
        return self.quotes, SourceStatus(name=self.name, ok=True, n_items=len(self.quotes))


@needs_bundle
def test_fail_closed_when_no_authoritative_calendar(cfg):
    from betbot.scan import render_report, run_scan
    ghost = _fm("Alcaraz C.", "Draper J.", authoritative=False, status="unknown")
    ghost.scheduled_at_utc = None
    quotes = [OddsQuote(player_a="Alcaraz C.", player_b="Draper J.", market="match_winner",
                        selection="a", odds=1.5, bookmaker="TE",
                        timestamp=datetime.now(timezone.utc).isoformat())]
    res = run_scan(cfg, calendar_sources=[DeadCalendar(), LiveOddsOnly([ghost], quotes)],
                   odds_sources=[LiveOddsOnly([ghost], quotes)],
                   structured_providers=[], log_ledger=False, show_rejected=True)
    assert res.summary["fail_closed"] is True
    assert res.summary["fail_closed_message"] == FAIL_CLOSED_MSG
    assert len(res.rows) == 0 and len(res.displayed) == 0
    assert FAIL_CLOSED_MSG in render_report(res)


@needs_bundle
def test_no_fail_closed_when_authoritative_source_returns_zero(cfg):
    """Una fuente sana con 0 partidos NO es un fallo: es una jornada vacía."""
    from betbot.scan import run_scan

    class EmptyCal:
        name = "cal_vacio"
        authoritative = True

        def fetch_matches(self, window_hours):
            return [], SourceStatus(name=self.name, ok=True, authoritative=True)

    res = run_scan(cfg, calendar_sources=[EmptyCal()], odds_sources=[],
                   structured_providers=[], log_ledger=False)
    assert res.summary["fail_closed"] is False and res.summary["calendar_confirmed"] == 0


# --------------------------------------------------------------------------
# 10. watch retira la señal al comenzar el partido
# --------------------------------------------------------------------------

def test_watch_drops_signal_when_match_starts():
    key = "alcaraz_c__draper_j"
    conf = {key: {"status": "scheduled", "start_utc": (NOW + timedelta(minutes=20)).isoformat(),
                  "source": "espn", "event_id": "e1"}}
    # todavía prepartido: sin alertas
    alerts, seen, frozen = watchcmd.prematch_transitions(conf, conf, set(), now=NOW)
    assert alerts == [] and frozen == {}
    # 25 minutos después ya ha empezado: señal retirada y observación congelada
    later = NOW + timedelta(minutes=25)
    alerts2, seen2, frozen2 = watchcmd.prematch_transitions(conf, conf, seen, now=later)
    assert len(alerts2) == 1 and "RETIRADA" in alerts2[0]
    assert frozen2[key]["start_utc"] == conf[key]["start_utc"]
    # sin alerta repetida
    alerts3, _, _ = watchcmd.prematch_transitions(conf, conf, seen2, now=later)
    assert alerts3 == []
    # pasa a live/completed antes de la hora: también se retira
    conf_live = {key: dict(conf[key], status="live")}
    alerts4, _, _ = watchcmd.prematch_transitions(conf, conf_live, set(), now=NOW)
    assert len(alerts4) == 1 and "live" in alerts4[0]


def test_signal_still_prematch_rules():
    start = (NOW + timedelta(hours=1)).isoformat()
    assert signal_still_prematch({"status": "scheduled", "start_utc": start}, NOW)
    assert not signal_still_prematch({"status": "live", "start_utc": start}, NOW)
    assert not signal_still_prematch({"status": "scheduled"}, NOW)          # sin hora
    assert not signal_still_prematch({"status": "scheduled",
                                      "start_utc": (NOW - timedelta(minutes=1)).isoformat()}, NOW)


# --------------------------------------------------------------------------
# 11. REGRESIÓN Tsitsipas–Fonseca
# --------------------------------------------------------------------------

def test_regression_tsitsipas_fonseca_never_pending(cfg, monkeypatch):
    """La fila REAL del feed que causó el fallo (2026-08-05): sin fecha, sin
    estado y sin zona horaria. No puede producir un evento analizable por
    ninguna vía, y si además ya tiene resultado, queda doblemente excluida."""
    fila_real = {"tournament": "Montreal", "time": "17:00", "player1": "Tsitsipas S.",
                 "player2": "Fonseca J. (22)", "odds1": 2.12, "odds2": 1.71, "tour": "ATP"}
    monkeypatch.setattr("betbot.feeds.github_te.cache.get_json",
                        lambda url, ttl_seconds: {"last_updated": NOW.replace(tzinfo=None).isoformat(),
                                                  "count": 1, "matches": [fila_real]})
    ms, _ = GithubTEFeed().fetch_matches(48)
    assert len(ms) == 1 and ms[0].player2 == "Fonseca J."      # semilla eliminada
    assert gate(ms, cfg, now=NOW, completed_idx={}).eligible == []

    # aunque una fuente autoritativa lo listara, si ya terminó no se analiza
    confirmado = _fm("Tsitsipas S.", "Fonseca J.", status="completed")
    assert gate([confirmado], cfg, now=NOW, completed_idx={}).eligible == []

    # y si el calendario dijera "scheduled" pero el resultado local ya existe:
    idx = {("ATP", "fonseca_j", "tsitsipas_s"): [
        {"date": NOW.date(), "status": "completed", "tournament": "Montreal"}]}
    res = gate([_fm("Tsitsipas S.", "Fonseca J.")], cfg, now=NOW, completed_idx=idx)
    assert res.eligible == [] and res.counts()["already_completed"] == 1


@needs_bundle
def test_regression_full_scan_with_real_feed_row_yields_no_signal(cfg, monkeypatch):
    """End-to-end: el feed real como ÚNICA fuente -> FAIL_CLOSED, 0 señales."""
    from betbot.scan import run_scan
    fixture = {"last_updated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
               "count": 1,
               "matches": [{"tournament": "Montreal", "time": "17:00",
                            "player1": "Tsitsipas S.", "player2": "Fonseca J. (22)",
                            "odds1": 2.12, "odds2": 1.71, "tour": "ATP"}]}
    monkeypatch.setattr("betbot.feeds.github_te.cache.get_json",
                        lambda url, ttl_seconds: fixture)
    te = GithubTEFeed()
    res = run_scan(cfg, calendar_sources=[te], odds_sources=[te],
                   structured_providers=[], log_ledger=False, show_rejected=True)
    assert res.summary["fail_closed"] is True
    assert len(res.rows) == 0


# --------------------------------------------------------------------------
# ESPN: parseo de una respuesta REAL capturada
# --------------------------------------------------------------------------

@pytest.mark.skipif(not FIXTURE.exists(), reason="falta el fixture real de ESPN")
def test_espn_parses_real_payload_schema():
    """Esquema real de tenis: los partidos viven en groupings[].competitions[],
    la hora ISO viene SIN segundos y el estado en status.type."""
    data = json.loads(FIXTURE.read_text())
    assert data["events"][0].get("competitions") in (None, [])   # NO es plano
    ms, n_raw, _ = EspnFeed.parse_scoreboard(data, "WTA", NOW_ISO)
    assert n_raw >= 2 and len(ms) >= 2
    m = next(x for x in ms if "Kalinskaya" in x.player1 or "Kalinskaya" in x.player2)
    assert m.scheduled_at_utc == datetime(2026, 6, 3, 9, 10, tzinfo=timezone.utc)
    assert m.status == "completed" and m.best_of == 3
    assert m.round == "Quarterfinal" and m.is_doubles is False
    assert m.tournament == "Roland Garros" and m.authoritative is True
    assert m.event_id == "espn:172-2026:chwalinska_m__kalinskaya_a"  # estable, sin fecha
    # los dobles del mismo torneo NO entran en el cuadro individual
    assert all("/" not in x.player1 and "/" not in x.player2 for x in ms)


@pytest.mark.skipif(not FIXTURE.exists(), reason="falta el fixture real de ESPN")
def test_espn_grand_slam_not_duplicated_between_leagues():
    """Roland Garros aparece íntegro en los endpoints atp y wta: el reparto por
    slug de cuadro evita duplicar cada partido de Grand Slam."""
    data = json.loads(FIXTURE.read_text())
    wta, _, _ = EspnFeed.parse_scoreboard(data, "WTA", NOW_ISO)
    atp, _, _ = EspnFeed.parse_scoreboard(data, "ATP", NOW_ISO)
    assert len(wta) >= 2 and len(atp) == 0     # el cuadro femenino solo cuenta como WTA
    assert not ({m.pair_key for m in wta} & {m.pair_key for m in atp})


@pytest.mark.skipif(not FIXTURE.exists(), reason="falta el fixture real de ESPN")
def test_espn_real_payload_produces_no_eligible_match(cfg):
    """Los partidos del payload real están FINALIZADOS: cero elegibles."""
    data = json.loads(FIXTURE.read_text())
    ms, _, _ = EspnFeed.parse_scoreboard(data, "WTA", NOW_ISO)
    res = gate(ms, cfg, now=NOW, completed_idx={})
    assert res.eligible == []
    assert res.counts()["estado_no_prepartido"] == len(ms)


def test_espn_status_mapping():
    assert espn_status({"status": {"type": {"name": "STATUS_SCHEDULED", "state": "pre",
                                            "completed": False}}}) == "scheduled"
    assert espn_status({"status": {"type": {"name": "STATUS_IN_PROGRESS", "state": "in",
                                            "completed": False}}}) == "live"
    assert espn_status({"status": {"type": {"name": "STATUS_FINAL", "state": "post",
                                            "completed": True}}}) == "completed"
    # ESPN marca el walkover como FINAL con shortDetail 'W/O'
    assert espn_status({"status": {"type": {"name": "STATUS_FINAL", "state": "post",
                                            "completed": True,
                                            "shortDetail": "W/O"}}}) == "walkover"
    assert espn_status({"status": {"type": {"name": "STATUS_RETIRED", "state": "post",
                                            "description": "Retired"}}}) == "completed"
    assert espn_status({"status": {"type": {"name": "STATUS_POSTPONED",
                                            "state": ""}}}) == "postponed"
    assert espn_status({"status": {}}) == "unknown"      # sin estado -> desconocido
    # un partido suspendido ya empezó: nunca es prepartido
    assert normalize_status("STATUS_SUSPENDED".lower()) == "live"


def test_espn_ignores_placeholder_and_incomplete_draw():
    """Los huecos del cuadro (TBD/Bye/Qualifier) no son partidos."""
    data = {"events": [{"id": "1-2026", "name": "Montreal", "groupings": [
        {"grouping": {"slug": "mens-singles"}, "competitions": [
            {"id": "1", "date": "2026-08-06T17:00Z",
             "status": {"type": {"state": "pre", "completed": False}},
             "competitors": [{"type": "athlete", "order": 1,
                              "athlete": {"displayName": "Carlos Alcaraz"}},
                             {"type": "athlete", "order": 2,
                              "athlete": {"displayName": "TBD"}}]},
            {"id": "2", "date": "2026-08-06T19:00Z",
             "status": {"type": {"state": "pre", "completed": False}},
             "competitors": [{"type": "athlete", "order": 1,
                              "athlete": {"displayName": "Jack Draper"}},
                             {"type": "athlete", "order": 2,
                              "athlete": {"displayName": "Jannik Sinner"}}]}]}]}]}
    ms, n_raw, _ = EspnFeed.parse_scoreboard(data, "ATP", NOW_ISO)
    assert n_raw == 2 and len(ms) == 1
    assert {ms[0].player1, ms[0].player2} == {"Draper J.", "Sinner J."}


def test_espn_doubles_use_roster_and_are_flagged():
    """En dobles no existe competitor['athlete']: el nombre va en roster."""
    data = {"events": [{"id": "1-2026", "name": "Montreal", "groupings": [
        {"grouping": {"slug": "mens-doubles"}, "competitions": [
            {"id": "9", "date": "2026-08-06T17:00Z",
             "status": {"type": {"state": "pre"}},
             "competitors": [
                 {"type": "team", "order": 1,
                  "roster": {"displayName": "Guo Hanyu / Kristina Mladenovic"}},
                 {"type": "team", "order": 2,
                  "roster": {"displayName": "Shuko Aoyama / En-Shuo Liang"}}]}]}]}]}
    # el cuadro de dobles ni siquiera entra en el reparto de individuales
    ms, _, _ = EspnFeed.parse_scoreboard(data, "ATP", NOW_ISO)
    assert ms == []
    # y si llegara por otra vía, quedaría marcado como dobles (excluido por la puerta)
    fm = EspnFeed._parse_competition(
        data["events"][0]["groupings"][0]["competitions"][0], "1-2026", "Montreal",
        "ATP", NOW_ISO)
    assert fm is not None and fm.is_doubles is True


def test_espn_client_side_date_window():
    """`?dates=` no filtra en tenis: el recorte se hace en el cliente."""
    data = {"events": [{"id": "1-2026", "name": "Cincinnati", "groupings": [
        {"grouping": {"slug": "mens-singles"}, "competitions": [
            {"id": "1", "date": "2026-08-06T17:00Z", "status": {"type": {"state": "pre"}},
             "competitors": [{"type": "athlete", "order": 1, "athlete": {"displayName": "Carlos Alcaraz"}},
                             {"type": "athlete", "order": 2, "athlete": {"displayName": "Jack Draper"}}]},
            {"id": "2", "date": "2026-08-20T17:00Z", "status": {"type": {"state": "pre"}},
             "competitors": [{"type": "athlete", "order": 1, "athlete": {"displayName": "Jannik Sinner"}},
                             {"type": "athlete", "order": 2, "athlete": {"displayName": "Alexander Zverev"}}]}]}]}]}
    lo, hi = NOW - timedelta(days=1), NOW + timedelta(days=3)
    ms, n_raw, n_oow = EspnFeed.parse_scoreboard(data, "ATP", NOW_ISO, lo, hi)
    assert n_raw == 2 and len(ms) == 1 and n_oow == 1
    assert {ms[0].player1, ms[0].player2} == {"Alcaraz C.", "Draper J."}


def test_espn_is_authoritative_and_publishes_no_odds():
    feed = EspnFeed()
    assert feed.authoritative is True
    quotes, st = feed.fetch_odds([])
    assert quotes == [] and st.ok      # ESPN no publica cuotas de tenis


# --------------------------------------------------------------------------
# Sportradar como segundo calendario autoritativo (enum cerrado del contrato)
# --------------------------------------------------------------------------

def test_sportradar_status_closed_enum():
    from betbot.feeds.results import sportradar_status
    # match_status manda sobre status
    assert sportradar_status({"status": "live", "match_status": "not_started"}) == "scheduled"
    assert sportradar_status({"status": "live", "match_status": "1st_set"}) == "live"
    assert sportradar_status({"status": "closed", "match_status": "ended"}) == "completed"
    assert sportradar_status({"status": "closed", "match_status": "retired"}) == "completed"
    assert sportradar_status({"status": "closed", "match_status": "walkover"}) == "walkover"
    assert sportradar_status({"status": "cancelled", "match_status": "cancelled"}) == "cancelled"
    assert sportradar_status({"status": "postponed", "match_status": "postponed"}) == "postponed"
    assert sportradar_status({"match_status": "start_delayed"}) == "delayed"
    assert sportradar_status({"status": "live"}) == "live"          # solo status
    assert sportradar_status({}) == "unknown"                        # sin estado


def test_sportradar_parse_event_real_shape(cfg):
    """Forma real del contrato Tennis v3: URN de evento, ISO con offset y
    sport_event_context con categoría/competición/ronda."""
    from betbot.feeds.results import SportradarResults
    summ = {
        "sport_event": {
            "id": "sr:sport_event:63735631",
            "start_time": "2026-08-05T18:30:00+00:00",
            "competitors": [{"id": "sr:competitor:145130", "name": "Tsitsipas, Stefanos",
                             "type": "person"},
                            {"id": "sr:competitor:225130", "name": "Fonseca, Joao",
                             "type": "person"}],
            "sport_event_context": {
                "category": {"name": "ATP"},
                "competition": {"name": "ATP Montreal, Canada Men Singles"},
                "round": {"name": "round_of_32"}}},
        "sport_event_status": {"status": "not_started", "match_status": "not_started"}}
    fm = SportradarResults.parse_event(summ, NOW_ISO)
    assert fm is not None
    assert fm.event_id == "sr:sport_event:63735631" and fm.authoritative is True
    assert fm.scheduled_at_utc == datetime(2026, 8, 5, 18, 30, tzinfo=timezone.utc)
    assert fm.status == "scheduled" and fm.tour == "ATP"
    assert {fm.player1, fm.player2} == {"Tsitsipas S.", "Fonseca J."}
    # con hora futura y estado programado, la puerta lo acepta
    assert len(gate([fm], cfg, now=NOW, completed_idx={}).eligible) == 1
    # el mismo evento ya empezado no pasa la puerta
    summ_live = {**summ, "sport_event_status": {"status": "live", "match_status": "2nd_set"}}
    fm_live = SportradarResults.parse_event(summ_live, NOW_ISO)
    assert fm_live.status == "live"
    assert gate([fm_live], cfg, now=NOW, completed_idx={}).eligible == []


def test_sportradar_calendar_inactive_without_key(monkeypatch):
    from betbot.feeds.results import SportradarResults
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    src = SportradarResults()
    assert src.authoritative is True
    ms, st = src.fetch_matches(48)
    assert ms == [] and not st.ok and "SPORTRADAR_API_KEY" in st.error


def test_default_calendar_sources_are_all_authoritative(cfg):
    """Ninguna fuente no autoritativa puede estar en el orden de calendario."""
    from betbot.scan import default_sources
    cals, odds = default_sources(cfg)
    assert cals and all(getattr(c, "authoritative", False) for c in cals)
    assert "github_te" not in [getattr(c, "name", "") for c in cals]
    # pero sí sigue siendo fuente de cuotas
    assert "github_te" in [getattr(o, "name", "") for o in odds]
