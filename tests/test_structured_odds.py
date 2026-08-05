"""Tests de los proveedores estructurados de mercados de sets: mapeo dinámico
de catálogo, not_offered, metadatos completos del precio, garantía de solo
lectura de Betfair y e2e de scan con moneyline + un mercado de sets real."""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch, SourceStatus
from betbot.feeds.betfair import READ_ONLY_OPS, BetfairExchangeProvider
from betbot.feeds.structured_odds import (CanonicalPrice, map_market,
                                          summarize_catalogue, to_odds_quotes)
from betbot.schemas import OddsQuote

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"
TODAY = datetime.now(timezone.utc).date()
NOW_ISO = datetime.now(timezone.utc).isoformat()
needs_bundle = pytest.mark.skipif(not BUNDLE.exists(), reason="necesita modelos entrenados")

P1, P2 = "Fritz T.", "Jodar R."


# ---------------- mapeo dinámico de mercados ----------------

def test_map_match_odds_and_set1_winner():
    m = map_market("MATCH_ODDS", "Match Odds", ["Taylor Fritz", "Rafael Jodar"], P1, P2)
    assert m["Taylor Fritz"] == ("match_winner", "a")
    assert m["Rafael Jodar"] == ("match_winner", "b")
    s1 = map_market("SET_WINNER", "First Set Winner", ["Fritz", "Jodar"], P1, P2)
    assert s1["Fritz"] == ("set1_winner", "a") and s1["Jodar"] == ("set1_winner", "b")
    # un SET_WINNER del segundo set NO se mapea a set1_winner
    s2 = map_market("SET_WINNER", "Set 2 Winner", ["Fritz", "Jodar"], P1, P2)
    assert all(v == ("unmapped", "") for v in s2.values())


def test_map_win_a_set_contract_equivalences():
    """wins_set(x) == YES de 'x gana un set'; straight_sets(rival) == NO."""
    m = map_market("PLAYER_A_WIN_A_SET", "Fritz to win a set", ["Yes", "No"], P1, P2)
    assert m["Yes"] == ("wins_set", "a")
    assert m["No"] == ("straight_sets", "b")       # A no gana set <=> B 2-0
    mb = map_market("PLAYER_B_WIN_A_SET", "Jodar to win a set", ["Yes", "No"], P1, P2)
    assert mb["Yes"] == ("wins_set", "b") and mb["No"] == ("straight_sets", "a")


def test_map_number_of_sets_and_set_betting():
    n = map_market("NUMBER_OF_SETS", "Number of Sets", ["2 Sets", "3 Sets"], P1, P2)
    assert n["2 Sets"] == ("three_sets", "no")     # Bo3 acaba en 2 <=> NO hay 3er set
    assert n["3 Sets"] == ("three_sets", "yes")
    sb = map_market("SET_BETTING", "Set Betting",
                    ["Fritz 2-0", "Fritz 2-1", "Jodar 2-0", "Jodar 2-1"], P1, P2)
    assert sb["Fritz 2-0"] == ("straight_sets", "a")   # 2-0 == sets corridos
    assert sb["Jodar 2-0"] == ("straight_sets", "b")
    assert sb["Fritz 2-1"][0] == "set_score"           # metadato, sin cuota en el motor
    # marcador sin nombre: perspectiva del jugador 1
    sb2 = map_market("CORRECT_SCORE", "Set Correct Score", ["2-0", "0-2"], P1, P2)
    assert sb2["2-0"] == ("straight_sets", "a") and sb2["0-2"] == ("straight_sets", "b")


def test_unknown_market_type_is_unmapped_not_error():
    """El catálogo es dinámico: un marketType desconocido queda unmapped, jamás
    rompe ni exige una lista cerrada."""
    m = map_market("GAME_BY_GAME_05_06", "Game 6 Winner", ["Fritz", "Jodar"], P1, P2)
    assert all(v == ("unmapped", "") for v in m.values())


def test_summarize_catalogue_not_offered():
    cat = [{"marketType": "MATCH_ODDS", "marketName": "Match Odds",
            "runnerNames": ["Fritz", "Jodar"]},
           {"marketType": "NUMBER_OF_SETS", "marketName": "Number of Sets",
            "runnerNames": ["2", "3"]}]
    em = summarize_catalogue("e1", f"{P1} v {P2}", P1, P2, cat)
    assert em.canonical_offered == ["match_winner", "three_sets"]
    assert set(em.not_offered) == {"set1_winner", "wins_set", "straight_sets"}
    # catálogo vacío: TODO not_offered, sin error
    em2 = summarize_catalogue("e2", f"{P1} v {P2}", P1, P2, [])
    assert len(em2.not_offered) == 5


# ---------------- CanonicalPrice: metadatos y cotizabilidad ----------------

def _price(**kw):
    base = dict(provider="betfair", bookmaker="betfair_es", event_id="e1",
                market_id="1.1", market_type_raw="SET_WINNER",
                market_canonical="set1_winner", runner_raw="Fritz", selection="a",
                back_odds=1.8, back_size=120.0, timestamp=NOW_ISO, status="open",
                in_play=False, player_a=P1, player_b=P2)
    base.update(kw)
    return CanonicalPrice(**base)


def test_price_quotable_rules_no_invented_prices():
    assert _price().quotable
    assert not _price(status="suspended").quotable     # suspendido no se cotiza
    assert not _price(back_odds=None).quotable         # sin back real NO se inventa
    assert not _price(in_play=True).quotable           # en juego queda fuera (pre-match)
    assert not _price(market_canonical="set_score", selection="a:2-1").quotable
    rows = to_odds_quotes([_price(), _price(status="suspended"), _price(back_odds=None)])
    assert len(rows) == 1 and rows[0]["market"] == "set1_winner"
    assert rows[0]["bookmaker"] == "betfair_es" and rows[0]["odds"] == 1.8


# ---------------- Betfair: solo lectura garantizada ----------------

def test_betfair_read_only_guard():
    prov = BetfairExchangeProvider(app_key="k", username="u", password="p")
    for op in ("placeOrders", "cancelOrders", "replaceOrders", "updateOrders",
               "getAccountFunds", "listCurrentOrders"):
        with pytest.raises(PermissionError):
            prov._call(op, {})
    assert set(READ_ONLY_OPS) == {"listEvents", "listMarketCatalogue", "listMarketBook"}
    # el módulo no contiene NINGUNA ruta de apuesta ni de saldo
    src = (ROOT / "src" / "betbot" / "feeds" / "betfair.py").read_text(encoding="utf-8")
    for forbidden in ("placeOrders", "cancelOrders", "replaceOrders", "updateOrders",
                      "getAccountFunds", "getAccountDetails", "transferFunds"):
        assert forbidden not in src


def test_betfair_inactive_without_credentials(monkeypatch):
    for var in ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    prov = BetfairExchangeProvider()
    assert not prov.active()
    prices, st = prov.fetch_prices([])
    assert prices == [] and not st.ok and "inactivo" in st.error


# ---------------- Betfair con fixtures de la API real ----------------

def _fm(p1, p2, tour="ATP", hours_ahead=6, **kw):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    base = dict(date=start.date(), tour=tour, tournament="Washington", player1=p1,
                player2=p2, source="fake", event_id=f"ev-{p1}", scheduled_at_utc=start,
                status="scheduled", source_updated_at=NOW_ISO, authoritative=True)
    base.update(kw)
    return FeedMatch(**base)


@pytest.fixture()
def betfair_fixture(monkeypatch):
    prov = BetfairExchangeProvider(app_key="k", username="u", password="p")
    events = [{"event": {"id": "31", "name": "Jodar v Fritz",     # invertido a proposito
                         "openDate": f"{TODAY}T18:00:00.000Z"}}]
    catalogue = [
        {"marketId": "1.10", "marketName": "Match Odds", "event": {"id": "31"},
         "description": {"marketType": "MATCH_ODDS"},
         "runners": [{"selectionId": 1, "runnerName": "Rafael Jodar"},
                     {"selectionId": 2, "runnerName": "Taylor Fritz"}]},
        {"marketId": "1.11", "marketName": "First Set Winner", "event": {"id": "31"},
         "description": {"marketType": "SET_WINNER"},
         "runners": [{"selectionId": 1, "runnerName": "Rafael Jodar"},
                     {"selectionId": 2, "runnerName": "Taylor Fritz"}]},
        {"marketId": "1.12", "marketName": "Number of Sets", "event": {"id": "31"},
         "description": {"marketType": "NUMBER_OF_SETS"},
         "runners": [{"selectionId": 5, "runnerName": "2 Sets"},
                     {"selectionId": 6, "runnerName": "3 Sets"}]},
        {"marketId": "1.13", "marketName": "Extraño", "event": {"id": "31"},
         "description": {"marketType": "TIE_BREAK_MATCH"},
         "runners": [{"selectionId": 7, "runnerName": "Yes"},
                     {"selectionId": 8, "runnerName": "No"}]}]
    books = [
        {"marketId": "1.10", "status": "OPEN", "inplay": False, "runners": [
            {"selectionId": 1, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 3.2, "size": 500.0}]}},
            {"selectionId": 2, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 1.38, "size": 900.0}]}}]},
        {"marketId": "1.11", "status": "OPEN", "inplay": False, "runners": [
            {"selectionId": 1, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 2.9, "size": 60.0}]}},
            {"selectionId": 2, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 1.45, "size": 80.0}]}}]},
        {"marketId": "1.12", "status": "SUSPENDED", "inplay": False, "runners": [
            {"selectionId": 5, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 1.6, "size": 40.0}]}},
            {"selectionId": 6, "status": "ACTIVE",
             "ex": {"availableToBack": [{"price": 2.4, "size": 30.0}]}}]},
        {"marketId": "1.13", "status": "OPEN", "inplay": False, "runners": [
            {"selectionId": 7, "status": "ACTIVE", "ex": {"availableToBack": []}},
            {"selectionId": 8, "status": "ACTIVE", "ex": {"availableToBack": []}}]}]
    calls: list[str] = []

    def fake_call(op, payload):
        calls.append(op)
        assert op in READ_ONLY_OPS
        if op == "listEvents":
            return events
        if op == "listMarketCatalogue":
            return catalogue
        return books

    monkeypatch.setattr(prov, "_call", fake_call)
    prov._fixture_calls = calls
    return prov


def test_betfair_fetch_prices_dynamic_mapping_and_orientation(betfair_fixture):
    prov = betfair_fixture
    matches = [_fm(P1, P2)]                       # feed: Fritz primero
    prices, st = prov.fetch_prices(matches)
    assert st.ok and prices
    # orientación: el evento Betfair va invertido (Jodar v Fritz); la selección
    # canónica sigue el orden del FEED: Fritz='a'
    ml = {p.runner_raw: p for p in prices if p.market_canonical == "match_winner"}
    assert ml["Taylor Fritz"].selection == "a" and ml["Taylor Fritz"].back_odds == 1.38
    assert ml["Rafael Jodar"].selection == "b" and ml["Rafael Jodar"].back_size == 500.0
    s1 = {p.runner_raw: p for p in prices if p.market_canonical == "set1_winner"}
    assert s1["Taylor Fritz"].selection == "a" and s1["Taylor Fritz"].back_odds == 1.45
    # NUMBER_OF_SETS suspendido: metadato presente, cuota NO cotizable
    ts = [p for p in prices if p.market_canonical == "three_sets"]
    assert ts and all(p.status == "suspended" and not p.quotable for p in ts)
    # marketType desconocido conservado como unmapped (catálogo dinámico)
    assert any(p.market_canonical == "unmapped" and p.market_type_raw == "TIE_BREAK_MATCH"
               for p in prices)
    # metadatos completos en todos los precios
    for p in prices:
        assert p.provider == "betfair" and p.event_id == "31" and p.market_id
        assert p.timestamp and p.market_type_raw
    # a OddsQuote solo pasa lo cotizable: moneyline + set1 (4 filas)
    rows = to_odds_quotes(prices)
    assert {(r["market"], r["selection"]) for r in rows} == {
        ("match_winner", "a"), ("match_winner", "b"),
        ("set1_winner", "a"), ("set1_winner", "b")}


# ---------------- e2e: scan con moneyline + un mercado de sets ----------------

class FakeCalendar:
    name = "fake_cal"
    authoritative = True

    def __init__(self, matches):
        self.matches = matches

    def fetch_matches(self, window_hours):
        return self.matches, SourceStatus(name=self.name, ok=True, authoritative=True,
                                          n_items=len(self.matches))


class FakeOdds:
    name = "fake_odds"

    def __init__(self, quotes):
        self.quotes = quotes

    def fetch_odds(self, matches):
        return self.quotes, SourceStatus(name=self.name, ok=True, n_items=len(self.quotes))


class FakeStructured:
    """Proveedor estructurado con precios en formato CanonicalPrice real."""
    name = "fake_struct"

    def __init__(self, prices):
        self.prices = prices

    def active(self):
        return True

    def list_markets(self, matches):
        return [], SourceStatus(name=self.name, ok=True)

    def fetch_prices(self, matches):
        return self.prices, SourceStatus(name=self.name, ok=True, n_items=len(self.prices))


@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    cfg.setdefault("feeds", {})["commence_crosscheck"] = False
    return cfg


@needs_bundle
def test_scan_e2e_moneyline_plus_sets_market(cfg_tmp):
    from betbot.scan import run_scan
    p1, p2 = "Alcaraz C.", "Draper J."
    matches = [_fm(p1, p2)]
    quotes = [OddsQuote(player_a=p1, player_b=p2, market="match_winner", selection="a",
                        odds=1.55, bookmaker="B1", timestamp=NOW_ISO),
              OddsQuote(player_a=p1, player_b=p2, market="match_winner", selection="b",
                        odds=2.60, bookmaker="B1", timestamp=NOW_ISO)]
    struct = [
        CanonicalPrice(provider="fake_struct", bookmaker="ex1", event_id="9",
                       market_id="1.9", market_type_raw="SET_WINNER",
                       market_canonical="set1_winner", runner_raw=p1, selection="a",
                       back_odds=1.70, back_size=100.0, timestamp=NOW_ISO,
                       status="open", player_a=p1, player_b=p2),
        CanonicalPrice(provider="fake_struct", bookmaker="ex1", event_id="9",
                       market_id="1.9", market_type_raw="SET_WINNER",
                       market_canonical="set1_winner", runner_raw=p2, selection="b",
                       back_odds=2.35, back_size=90.0, timestamp=NOW_ISO,
                       status="open", player_a=p1, player_b=p2),
        CanonicalPrice(provider="fake_struct", bookmaker="ex1", event_id="9",
                       market_id="1.8", market_type_raw="NUMBER_OF_SETS",
                       market_canonical="three_sets", runner_raw="3 Sets",
                       selection="yes", back_odds=2.1, back_size=50.0,
                       timestamp=NOW_ISO, status="suspended",
                       player_a=p1, player_b=p2)]
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(matches)],
                   odds_sources=[FakeOdds(quotes)],
                   structured_providers=[FakeStructured(struct)],
                   show_rejected=True, show_likely=True)
    s = res.summary
    # cobertura POR MERCADO: el 100% de moneyline no se presenta como total
    assert s["market_coverage"]["match_winner"] == 1
    assert s["market_coverage"]["set1_winner"] == 1
    assert s["market_coverage"]["wins_set"] == 0
    ev = s["structured"]["events"][0]
    assert "set1_winner" in ev["offered"] and "wins_set" in ev["not_offered"]
    assert "three_sets" in ev["suspended"]
    # el mercado de sets se evaluó con la cuota real del exchange
    s1 = res.rows[(res.rows["market"] == "set1_winner") & (res.rows["odds"].notna())]
    assert len(s1) == 2 and set(s1["bookmaker"]) == {"ex1"}
    # experimental como tope y con aviso si es three_sets; set1 con cuota real
    assert (s1["state"].isin(["experimental", "vigilar_precio", "sin_value",
                              "probable_sin_value", "descartada"])).all()
    # la cuota suspendida de three_sets NO entro como cotizacion
    t3 = res.rows[(res.rows["market"] == "three_sets") & (res.rows["odds"].notna())]
    assert len(t3) == 0
    # metadatos completos persistidos en el ledger
    led = Path(cfg_tmp["paths"]["ledger_dir"]) / "structured_prices.jsonl"
    assert led.exists() and len(led.read_text().splitlines()) == 3


def test_watch_market_alerts_dedupe():
    from betbot import watchcmd
    rows1 = pd.DataFrame([{"match": "M1", "market": "match_winner", "selection": "a",
                           "bookmaker": "B1", "odds": 1.6, "o_min": 1.5, "state": "normal",
                           "ev_cons": 0.04, "selection_name": "X", "reasons": ""}])
    sum1 = {"structured": {"events": [{"match": "M1", "offered": ["match_winner"],
                                      "not_offered": [], "suspended": [], "in_play": False}]}}
    snap1 = watchcmd.market_snapshot(rows1, sum1)
    alerts1, seen = watchcmd.market_alerts(None, snap1, set(), synced=True)
    assert alerts1 == []                      # primer ciclo: el estado inicial no alerta
    # ciclo 2: nuevo mercado cotizado + cambio de mejor fuente + suspension + OOD
    rows2 = pd.DataFrame([
        {"match": "M1", "market": "match_winner", "selection": "a", "bookmaker": "B2",
         "odds": 1.7, "o_min": 1.5, "state": "normal", "ev_cons": 0.05,
         "selection_name": "X", "reasons": ""},
        {"match": "M1", "market": "set1_winner", "selection": "a", "bookmaker": "ex1",
         "odds": 1.8, "o_min": None, "state": "experimental", "ev_cons": 0.03,
         "selection_name": "X", "reasons": ""},
        {"match": "M2", "market": "match_winner", "selection": "a", "bookmaker": "B1",
         "odds": 2.0, "o_min": None, "state": "descartada", "ev_cons": None,
         "selection_name": "Y", "reasons": "ood_pocos_partidos_12m:p(m12=2)"}])
    sum2 = {"structured": {"events": [{"match": "M1", "offered": ["match_winner"],
                                      "not_offered": [], "suspended": ["three_sets"],
                                      "in_play": False}]}}
    snap2 = watchcmd.market_snapshot(rows2, sum2)
    alerts2, seen = watchcmd.market_alerts(snap1, snap2, seen, synced=True)
    text = "\n".join(alerts2)
    assert "MERCADO NUEVO" in text and "set1_winner" in text
    assert "MEJOR FUENTE" in text and "B2" in text
    assert "SUSPENDIDO" in text and "three_sets" in text
    assert "OOD TRAS SYNC" in text and "M2" in text
    # repetir el mismo estado: sin alertas nuevas
    alerts3, seen = watchcmd.market_alerts(snap2, snap2, seen, synced=True)
    assert alerts3 == []
    # OOD resuelto tras un sync posterior
    snap3 = watchcmd.market_snapshot(rows2[rows2["match"] == "M1"], sum2)
    alerts4, _ = watchcmd.market_alerts(snap2, snap3, seen, synced=True)
    assert any("OOD RESUELTO" in a for a in alerts4)
