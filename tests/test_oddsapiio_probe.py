"""Sondeo de Odds-API.io: clasificación de mercados SIN adivinar, contabilidad
exacta de peticiones, anonimización del fixture y ausencia total de señales."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from betbot.config import load_config
from betbot.feeds.oddsapiio import (OddsApiIoClient, RequestLog, as_list, event_players,
                                    event_start, event_tour, extract_markets)
from betbot.feeds.oddsapiio_markets import (CANONICAL, classify_market, classify_all,
                                            supported_summary)
from betbot.feeds.oddsapiio_probe import polling_policy, run_probe

P1, P2 = "Carlos Alcaraz", "Jack Draper"


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["exports_dir"] = str(tmp_path / "exports")
    c["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    return c


# ----------------------------------------------------- mapeo sin adivinar

def _o(*names_odds):
    return [{"name": n, "odds": o} for n, o in names_odds]


def test_map_match_winner():
    canon, verdict, _ = classify_market("Match Winner", _o((P1, 1.5), (P2, 2.6)), P1, P2)
    assert (canon, verdict) == ("match_winner", "mapeado")
    canon2, v2, _ = classify_market("Moneyline", _o(("Alcaraz", 1.5), ("Draper", 2.6)), P1, P2)
    assert (canon2, v2) == ("match_winner", "mapeado")


def test_map_set1_winner_requires_player_selections():
    canon, v, _ = classify_market("1st Set Winner", _o((P1, 1.7), (P2, 2.2)), P1, P2)
    assert (canon, v) == ("set1_winner", "mapeado")
    # nombre de primer set pero selecciones que no son jugadores -> ambiguo
    canon2, v2, why = classify_market("First Set Winner", _o(("Over", 1.8), ("Under", 2.0)),
                                      P1, P2)
    assert canon2 == "" and v2 == "ambiguo"


def test_map_wins_set_and_straight_sets():
    canon, v, _ = classify_market("Player to win a set", _o(("Yes", 1.4), ("No", 2.9)), P1, P2)
    assert (canon, v) == ("wins_set", "mapeado")
    canon2, v2, _ = classify_market("Straight Sets", _o((P1, 2.1), (P2, 4.5)), P1, P2)
    assert (canon2, v2) == ("straight_sets", "mapeado")
    # hándicap de sets con línea explícita
    assert classify_market("Set Handicap", _o(("+1.5", 1.3), ("-1.5", 3.2)),
                           P1, P2)[0] == "wins_set"
    assert classify_market("Set Handicap", _o(("-1.5", 3.2),), P1, P2)[0] == "straight_sets"
    # sin línea identificable -> ambiguo, nunca soportado
    assert classify_market("Set Handicap", _o(("A", 1.9), ("B", 1.9)),
                           P1, P2)[1] == "ambiguo"


def test_map_three_sets():
    canon, v, _ = classify_market("Total Sets", _o(("Over 2.5", 2.1), ("Under 2.5", 1.7)),
                                  P1, P2)
    assert (canon, v) == ("three_sets", "mapeado")
    assert classify_market("Number of Sets", _o(("2", 1.6), ("3", 2.3)), P1, P2)[0] == "three_sets"
    assert classify_market("Total Sets", _o(("algo", 1.6),), P1, P2)[1] == "ambiguo"


def test_map_set_score_only_with_set_like_outcomes():
    canon, v, _ = classify_market("Set Betting",
                                  _o(("2-0", 2.4), ("2-1", 3.6), ("0-2", 6.0)), P1, P2)
    assert (canon, v) == ("set_score", "mapeado")
    # 'Correct Score' con marcador de JUEGOS no es set_score: ambiguo, no soportado
    canon2, v2, why = classify_market("Correct Score", _o(("6-4", 5.5), ("7-5", 7.0)), P1, P2)
    assert canon2 == "" and v2 == "ambiguo" and "juegos" in why.lower()


def test_unknown_markets_are_never_invented():
    for name in ("Total Games", "Tie Break in Match", "First Game Winner", "Race to 3 Games"):
        canon, verdict, _ = classify_market(name, _o(("Yes", 1.8), ("No", 1.9)), P1, P2)
        assert canon == "" and verdict in ("no_mapeado", "ambiguo")


def test_supported_summary_requires_real_price():
    """Un contrato NO se declara soportado si no apareció con cuota utilizable."""
    mapped_no_price = classify_all(
        [{"bookmaker": "B1", "market_raw": "Total Sets",
          "outcomes": _o(("Over 2.5", None), ("Under 2.5", None))}], P1, P2)
    assert supported_summary(mapped_no_price) == {}
    mapped_bad_price = classify_all(
        [{"bookmaker": "B1", "market_raw": "Total Sets",
          "outcomes": _o(("Over 2.5", 1.0), ("Under 2.5", 0.5))}], P1, P2)
    assert supported_summary(mapped_bad_price) == {}
    good = classify_all(
        [{"bookmaker": "B1", "market_raw": "Total Sets",
          "outcomes": _o(("Over 2.5", 2.1), ("Under 2.5", 1.7))}], P1, P2)
    sup = supported_summary(good)
    assert sup["three_sets"]["n_observado"] == 1 and sup["three_sets"]["bookmakers"] == ["B1"]
    # los ambiguos jamás llegan al resumen de soportados
    amb = classify_all([{"bookmaker": "B1", "market_raw": "Correct Score",
                         "outcomes": _o(("6-4", 5.5))}], P1, P2)
    assert supported_summary(amb) == {}


# ----------------------------------------------------- extracción de respuestas

def test_extract_markets_from_nested_shapes():
    payload = {"data": [{"bookmaker": "Bet365", "markets": [
        {"name": "Match Winner", "outcomes": [{"name": P1, "price": 1.55},
                                              {"name": P2, "price": 2.5}]},
        {"name": "Set Betting", "selections": [{"name": "2-0", "odds": "2.4"}]}]}]}
    mks = extract_markets(payload)
    names = {m["market_raw"] for m in mks}
    assert names == {"Match Winner", "Set Betting"}
    mw = next(m for m in mks if m["market_raw"] == "Match Winner")
    assert mw["bookmaker"] == "Bet365" and mw["outcomes"][0]["odds"] == 1.55
    sb = next(m for m in mks if m["market_raw"] == "Set Betting")
    assert sb["outcomes"][0]["odds"] == 2.4          # cadena numérica convertida


def test_extract_markets_dict_of_markets():
    payload = {"odds": {"Total Sets": {"outcomes": [{"name": "Over 2.5", "price": 2.05}]}}}
    mks = extract_markets(payload)
    assert any(m["market_raw"] == "Total Sets" for m in mks)


def test_envelope_and_event_helpers():
    assert as_list([1, 2]) == [1, 2]
    assert as_list({"data": [1]}) == [1]
    assert as_list({"events": [1, 2]}) == [1, 2]
    assert as_list({"nada": 1}) == []
    ev = {"home": P1, "away": P2, "date": "2026-08-05T17:00:00Z",
          "league": {"name": "ATP Canadian Open"}}
    assert event_players(ev) == (P1, P2)
    assert event_tour(ev) == "ATP"
    assert event_start(ev) == datetime(2026, 8, 5, 17, 0, tzinfo=timezone.utc)
    assert event_tour({"league": {"name": "WTA Toronto"}}) == "WTA"
    assert event_tour({"league": {"name": "ITF M15"}}) == ""     # no se infiere
    assert event_players({"competitors": [{"name": P1}, {"name": P2}]}) == (P1, P2)


# ----------------------------------------------------- contabilidad y claves

def test_request_log_counts_and_remaining():
    log = RequestLog()
    log.add("/events", {"apiKey": "SECRETO", "sport": "tennis"}, 200, 12.0)
    log.add("/odds", {"apiKey": "SECRETO", "eventId": "1"}, 402, 8.0, note="plan limit")
    assert log.used == 2 and log.ok == 1
    assert all("apiKey" not in c["params"] for c in log.calls)   # la clave nunca se guarda
    rem, how = log.remaining(500)
    assert rem == 498 and "ESTIMADO" in how
    log.quota_headers["x-ratelimit-remaining"] = "137"
    rem2, how2 = log.remaining(500)
    assert rem2 == 137 and "declarado" in how2


def test_client_inactive_without_env_key(monkeypatch, cfg):
    monkeypatch.delenv("ODDS_API_IO_KEY", raising=False)
    assert OddsApiIoClient().active() is False
    out = run_probe(cfg)
    assert "ODDS_API_IO_KEY" in out and "INACTIVO" in out


# ----------------------------------------------------- sondeo completo simulado

class FakeClient(OddsApiIoClient):
    """Cliente con respuestas fijas; cuenta peticiones como el real."""

    def __init__(self, responses):
        super().__init__(api_key="clave-de-prueba")
        self.responses = responses

    def get(self, endpoint, params=None):
        params = dict(params or {})
        self.log.add(endpoint, params, 200, 1.0)
        for key, value in self.responses.items():
            if endpoint.startswith(key):
                return (value(params) if callable(value) else value), ""
        return None, "sin respuesta simulada"


def _fake_responses():
    start = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = {"data": [
        {"id": "ev1", "home": P1, "away": P2, "date": start,
         "league": {"name": "ATP Canadian Open"}, "status": "pending"},
        {"id": "ev2", "home": "Iga Swiatek", "away": "Coco Gauff", "date": start,
         "league": {"name": "WTA Toronto"}, "status": "pending"},
        {"id": "ev3", "home": "A B / C D", "away": "E F / G H", "date": start,
         "league": {"name": "ATP Canadian Open"}, "status": "pending"}]}
    books = {"data": [{"name": "Bet365"}, {"name": "Pinnacle"}, {"name": "1xBet"}]}

    def odds(params):
        book = params.get("bookmakers", "?")
        if book == "1xBet":
            return None
        return {"data": [{"bookmaker": book, "markets": [
            {"name": "Match Winner", "outcomes": [{"name": P1, "price": 1.55},
                                                  {"name": P2, "price": 2.5}]},
            {"name": "1st Set Winner", "outcomes": [{"name": P1, "price": 1.7},
                                                    {"name": P2, "price": 2.2}]},
            {"name": "Total Games", "outcomes": [{"name": "Over 22.5", "price": 1.9}]},
        ]}]}
    return {"/events": events, "/bookmakers": books, "/odds": odds}


def test_probe_end_to_end_reports_only_real_markets(cfg, monkeypatch):
    monkeypatch.setattr("betbot.scan.default_sources", lambda c: ([], []))
    client = FakeClient(_fake_responses())
    out = run_probe(cfg, hours=48, sample=2, client=client)
    # dobles descartados; ATP/WTA identificados
    assert "2 identificables como ATP/WTA" in out
    # mercados crudos visibles tal cual
    assert "«Match Winner»" in out and "«1st Set Winner»" in out and "«Total Games»" in out
    # soportados SOLO los que aparecieron con cuota real
    assert "SOPORTADO  match_winner" in out
    assert "SOPORTADO  set1_winner" in out
    for canon in ("wins_set", "straight_sets", "three_sets", "set_score"):
        assert f"NO PRESENTE {canon}" in out
    # bookmakers: los que respondieron con datos
    assert "PERMITIDOS" in out and "Bet365" in out
    # contabilidad exacta
    assert f"Peticiones consumidas en este sondeo: {client.log.used}" in out
    assert client.log.used == len(client.log.calls) > 0
    # ninguna clave impresa
    assert "clave-de-prueba" not in out


def test_probe_writes_anonymised_fixture(cfg, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr("betbot.scan.default_sources", lambda c: ([], []))
    run_probe(cfg, hours=48, sample=1, client=FakeClient(_fake_responses()))
    path = Path(cfg["paths"]["exports_dir"]) / "oddsapiio_probe_fixture.json"
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    # jugadores seudonimizados en todo el fichero
    assert "Alcaraz" not in raw and "Draper" not in raw
    assert data["events"][0]["player1"].startswith("Jugador A")
    # mercados y cuotas CONSERVADOS (es lo que los tests necesitan)
    assert "Match Winner" in raw and "1.55" in raw
    # ninguna clave ni dato de cuenta
    assert "apiKey" not in raw and "clave-de-prueba" not in raw


def test_probe_emits_no_signals(cfg, monkeypatch):
    """El sondeo no puede invocar el screener ni escribir en el ledger."""
    from pathlib import Path
    called = []
    monkeypatch.setattr("betbot.screener.run.screen_day",
                        lambda *a, **k: called.append(1) or (None, []))
    monkeypatch.setattr("betbot.scan.default_sources", lambda c: ([], []))
    out = run_probe(cfg, hours=48, sample=2, client=FakeClient(_fake_responses()))
    assert called == []
    assert "NO genera señales" in out
    ledger = Path(cfg["paths"]["ledger_dir"])
    assert not ledger.exists() or not any(ledger.iterdir())


def test_probe_handles_events_failure_gracefully(cfg):
    client = FakeClient({})          # /events sin respuesta simulada
    out = run_probe(cfg, hours=48, sample=2, client=client)
    assert "FALLO al pedir /events" in out
    assert "Peticiones consumidas" in out      # la contabilidad se reporta igualmente


# ----------------------------------------------------- política de polling

def test_polling_policy_stays_within_budget():
    lines = polling_policy(20, budget=500)
    text = "\n".join(lines)
    assert "≤ 500" in text
    general = 4 * (1 + 20 * 2)
    assert str(general) in text and general < 500
    assert "15 min" in text and "candidatas" in text


def test_polling_policy_scales_with_match_count():
    small = "\n".join(polling_policy(5, budget=500))
    big = "\n".join(polling_policy(50, budget=500))
    assert "24 peticiones" in small or "24" in small
    assert small != big


def test_oddsapiio_is_not_a_scan_source(cfg):
    """Odds-API.io es adicional y sustituible: hoy NO participa en el escaneo."""
    from betbot.scan import default_sources
    cals, odds = default_sources(cfg)
    names = [getattr(s, "name", "") for s in cals + odds]
    assert not any("odds-api.io" in n or "oddsapiio" in n for n in names)
    # y Betfair y las fuentes actuales siguen intactas
    from betbot.feeds.manage import default_structured_providers
    assert "betfair" in [p.name for p in default_structured_providers(cfg)]
    assert "github_te" in [o.name for o in odds]
    assert [c.name for c in cals][0] == "wta_official"
