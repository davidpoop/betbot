"""The Odds API /scores como fuente de resultados ATP: honestidad CASO A/B.

Veredicto verificado con payloads reales grabados (fixture
oddsapi_scores_recorded.json): en tenis `scores` trae SETS GANADOS como string
("2"/"1") — jamás marcador por sets. Por tanto (CASO B) el adaptador no puede
fabricar resultados canónicos con el payload actual, y estos tests fijan ese
contrato: live/upcoming jamás entran, un agregado jamás se convierte en "6-x",
y una fila agregada tampoco pasaría `prepare_rows`. El camino CASO A (marcador
por sets reconstruible) queda implementado y testeado por si el proveedor lo
publica algún día — con cobertura PARCIAL que nunca avanza la frescura global.
"""
import copy
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.cache import redact
from betbot.feeds.oddsapi_scores import OddsApiScores, _sets_from_scores
from betbot.sync import (PARTIAL_RESULT_SOURCES, freshness_detail,
                         local_freshness, run_sync)

FIX = Path(__file__).parent / "fixtures" / "oddsapi_scores_recorded.json"
RECORDED = json.loads(FIX.read_text())

SPORTS = [{"key": "tennis_atp_canadian_open", "title": "ATP Canadian Open", "active": True}]


def _src(monkeypatch, events, sports=None, key="k"):
    src = OddsApiScores(api_key=key)

    def fake_get(url, ttl_seconds, headers=None):
        if "/sports/?" in url:
            return sports if sports is not None else SPORTS
        return events

    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json", fake_get)
    return src


@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    mirror = pd.DataFrame([{
        "tour": "ATP", "date": date(2026, 8, 3), "tournament": "Washington", "series": "",
        "surface": "Hard", "indoor": False, "round": "The Final", "best_of": 3,
        "player_a": "fritz_t", "player_b": "jodar_r", "raw_name_a": "Fritz T.",
        "raw_name_b": "Jodar R.", "label_a_wins": 1, "status": "completed",
        "sets_a": 2, "sets_b": 0, "games_a": 13, "games_b": 10, "set1_winner_a": 1,
        "rank_a": 4.0, "rank_b": 80.0, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test",
        "match_id": "ATP_2026-08-03_fritz_t__jodar_r"}])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": "ATP", "n_matches": 1, "display_name": p,
                   "hand": None, "dob": None}
                  for p in ("fritz_t", "jodar_r", "fils_a", "ruud_c")]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


# ------------------------------------------------ CASO B: el payload REAL

def test_recorded_aggregate_payload_yields_zero_canonical_rows(monkeypatch):
    """El payload real (sets ganados '2'/'1') NO produce resultados canónicos:
    2 finalizados observados, 0 utilizables, y el status lo explica."""
    src = _src(monkeypatch, RECORDED["events"])
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == []
    assert st.ok
    assert any("2 finalizados observados; 0 con marcador" in n for n in st.notes)
    assert any("granularidad insuficiente" in n for n in st.notes)


def test_live_never_enters_as_completed(monkeypatch):
    live = [e for e in RECORDED["events"] if e["id"] == "anon_live"]
    src = _src(monkeypatch, live)
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == [] and st.n_items == 0
    assert not any("finalizados observados; 1" in n for n in st.notes)


def test_upcoming_never_enters(monkeypatch):
    upc = [e for e in RECORDED["events"] if e["id"] == "anon_upcoming"]
    src = _src(monkeypatch, upc)
    rows, _ = src.fetch_results(date(2026, 8, 4), date(2026, 8, 12))
    assert rows == []


def test_aggregate_score_would_never_pass_prepare_rows():
    """Doble cinturón exigido por el mandato: aunque alguien construyera una
    fila con el agregado '2-1' como si fuera marcador, prepare_rows la rechaza
    (2-1 no es un set completo: sets del ganador = 0)."""
    from betbot.ingest.manual_results import prepare_rows
    df = pd.DataFrame([{"date": "2026-08-08", "tour": "ATP", "tournament": "Canadian Open",
                        "surface": "Hard", "indoor": "false", "round": "", "best_of": "3",
                        "winner": "Fils A.", "loser": "Ruud C.", "score": "2-1",
                        "status": "completed"}]).astype(str)
    acc, rej, _q = prepare_rows(df, registry={"fils_a", "ruud_c"}, known_ids=set(),
                                source_label="sync", allow_new="unambiguous",
                                today=date(2026, 8, 10))
    assert acc == []
    assert any("marcador_incoherente" in r["error"] for r in rej)


# ------------------------------------------------ CASO A: si algún día hay sets

def _caso_a_event(**kw):
    ev = {"id": "x", "sport_key": "tennis_atp_canadian_open",
          "sport_title": "ATP Canadian Open",
          "commence_time": "2026-08-08T15:00:00Z", "completed": True,
          "home_team": "Fils A.", "away_team": "Ruud C.",
          "scores": [{"name": "Fils A.", "score": "6-4 3-6 6-2"},
                     {"name": "Ruud C.", "score": ""}],
          "last_update": "2026-08-08T17:00:00Z"}
    ev.update(kw)
    return ev


def test_caso_a_sets_tokens_build_row_with_correct_orientation(monkeypatch):
    src = _src(monkeypatch, [_caso_a_event()])
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert len(rows) == 1
    r = rows[0]
    assert r["winner"] == "Fils A." and r["loser"] == "Ruud C."
    assert r["score"] == "6-4 3-6 6-2"            # orientado al ganador
    assert r["tournament"] == "Canadian Open" and r["surface"] == "Hard"
    assert r["status"] == "completed" and r["_src"] == "oddsapi_scores"


def test_caso_a_away_winner_reorients_score(monkeypatch):
    ev = _caso_a_event(scores=[{"name": "Fils A.", "score": "4-6 3-6"},
                               {"name": "Ruud C.", "score": ""}])
    src = _src(monkeypatch, [ev])
    rows, _ = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert len(rows) == 1
    assert rows[0]["winner"] == "Ruud C." and rows[0]["loser"] == "Fils A."
    assert rows[0]["score"] == "6-4 6-3"          # invertido al ganador real


def test_caso_a_games_lists_per_player(monkeypatch):
    ev = _caso_a_event(scores=[{"name": "Fils A.", "score": "6,3,6"},
                               {"name": "Ruud C.", "score": "4,6,2"}])
    src = _src(monkeypatch, [ev])
    rows, _ = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert len(rows) == 1 and rows[0]["score"] == "6-4 3-6 6-2"


def test_incomplete_sets_rejected_never_invented(monkeypatch):
    """Payload con sets que no completan el partido (1 set): reject, jamás se
    inventa una retirada ni un resultado."""
    ev = _caso_a_event(scores=[{"name": "Fils A.", "score": "6-4"},
                               {"name": "Ruud C.", "score": ""}])
    src = _src(monkeypatch, [ev])
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == []
    assert any("payload incompleto" in n for n in st.notes)


def test_sets_from_scores_edge_cases():
    assert _sets_from_scores(None)[0] == []
    assert _sets_from_scores([{"name": "a", "score": "2"}])[0] == []
    sets, why = _sets_from_scores([{"name": "a", "score": "2"},
                                   {"name": "b", "score": "1"}])
    assert sets == [] and "agregado" in why
    sets, _ = _sets_from_scores([{"name": "a", "score": ""},
                                 {"name": "b", "score": "7-6(4) 6-3"}])
    assert sets == [(6, 7), (3, 6)]               # tokens en away -> reorientado a home


# ------------------------------------------------ sync: dedupe, frescura, fallback

def _sync_with(monkeypatch, cfg, events, extra_sources=(), today=date(2026, 8, 10)):
    src = _src(monkeypatch, events)
    return run_sync(cfg, sources=[src, *extra_sources], today=today, refresh=False)


def test_caso_a_rows_do_not_advance_global_freshness(cfg_tmp, monkeypatch):
    """La regla principal del mandato: un resultado del Canadian Open NO
    convierte en 'fresco' al ATP global. PARTIAL_FRESH, no ✓ ATP fresco."""
    rep = _sync_with(monkeypatch, cfg_tmp, [_caso_a_event()])
    assert rep["n_accepted"] == 1
    fresh = local_freshness(cfg_tmp)
    assert fresh["ATP"] == date(2026, 8, 3)       # historial global INTACTO
    det = freshness_detail(cfg_tmp, today=date(2026, 8, 10))["ATP"]
    assert det["global_history"] == date(2026, 8, 3)
    assert det["coverage_status"] == "PARTIAL_FRESH"
    cov = det["active_coverage"]["canadian open"]
    assert cov["until"] == date(2026, 8, 8)
    # y la fila queda etiquetada como fuente parcial
    from betbot.canonical.store import load_matches
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    row = m[m["date"] == date(2026, 8, 8)].iloc[0]
    assert row["source"] in PARTIAL_RESULT_SOURCES


def test_partial_coverage_matches_scan_tournament_alias(cfg_tmp, monkeypatch):
    """La cobertura registrada como 'Canadian Open' aplica al partido que el
    scan ve como 'National Bank Open ...' (identidad de evento compartida)."""
    _sync_with(monkeypatch, cfg_tmp, [_caso_a_event()])
    from betbot.screener.run import _coverage_fresh
    from betbot.tournaments import canonical_event
    det = freshness_detail(cfg_tmp, today=date(2026, 8, 10))["ATP"]
    state = {"active_coverage": {"ATP": {k: {"tournament": c["tournament"],
                                             "until": str(c["until"])}
                                         for k, c in det["active_coverage"].items()}}}
    assert canonical_event("ATP", "National Bank Open Presented by Rogers") == "canadian open"
    got = _coverage_fresh(state, "ATP", "National Bank Open Presented by Rogers")
    assert got == date(2026, 8, 8)
    assert _coverage_fresh(state, "ATP", "Cincinnati") is None      # torneo NO cubierto


def test_screener_freshness_override_only_for_covered_tournament():
    from betbot.screener.run import _activity
    state = {"last_date": {"ATP|fils_a": "2026-08-01"},
             "recent": {"ATP|fils_a": ["2026-07-20", "2026-08-01"]},
             "last_retired": {}, "freshness": {"ATP": "2026-07-20"}}
    cfg_sel = {"freshness_lag_max_days": 14, "layoff_days_ood": 90,
               "stale_days_unknown": 540, "min_matches_12m": 5}
    d = date(2026, 8, 10)
    sin = _activity(state, "ATP", "fils_a", d, cfg_sel)
    con = _activity(state, "ATP", "fils_a", d, cfg_sel, fresh_override=date(2026, 8, 9))
    assert sin["freshness_unknown"] is True       # global con 21d de retraso
    assert con["freshness_unknown"] is False      # torneo cubierto hasta ayer


def test_duplicate_against_existing_history_is_skipped(cfg_tmp, monkeypatch):
    """Una fila de oddsapi_scores que repite un partido ya almacenado (TML) se
    deduplica y el histórico queda intacto."""
    ev = _caso_a_event(commence_time="2026-08-03T15:00:00Z",
                       home_team="Fritz T.", away_team="Jodar R.",
                       sport_title="ATP Washington",
                       sport_key="tennis_atp_washington_open",
                       scores=[{"name": "Fritz T.", "score": "7-6 6-4"},
                               {"name": "Jodar R.", "score": ""}])
    rep = _sync_with(monkeypatch, cfg_tmp, [ev])
    assert rep["n_accepted"] == 0
    assert rep["n_duplicates_skipped"] >= 1
    from betbot.canonical.store import load_matches
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    assert len(m) == 1 and m.iloc[0]["source"] == "test"    # histórico intacto


def test_api_failure_falls_back_to_other_sources(cfg_tmp, monkeypatch):
    class Boom:
        name = "oddsapi_scores"
        supported_tours = frozenset({"ATP"})

        def fetch_results(self, since, until):
            raise RuntimeError("HTTP 500")

    class FakeGithub:
        name = "github"
        supported_tours = frozenset({"ATP", "WTA"})

        def fetch_results(self, since, until):
            from betbot.feeds.base import SourceStatus
            return ([{"date": "2026-08-05", "tour": "ATP", "tournament": "Canadian Open",
                      "surface": "Hard", "indoor": "false", "round": "", "best_of": "3",
                      "winner": "Fils A.", "loser": "Ruud C.", "score": "6-4 6-2",
                      "status": "completed", "_level": "main"}],
                    SourceStatus(name="github", ok=True, n_items=1))

    rep = run_sync(cfg_tmp, sources=[Boom(), FakeGithub()], today=date(2026, 8, 10),
                   refresh=False)
    assert rep["n_accepted"] == 1                 # el fallback siguió funcionando
    st = {s["name"]: s for s in rep["sources"]}
    assert not st["oddsapi_scores"]["ok"]


def test_quota_and_malformed_and_empty_coverage(monkeypatch):
    # 429/límite: error reportado sin romper
    src = OddsApiScores(api_key="k")
    monkeypatch.setattr("betbot.feeds.oddsapi_scores.cache.get_json",
                        lambda url, ttl_seconds, headers=None:
                        (_ for _ in ()).throw(RuntimeError("HTTP 429: rate limit")))
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == [] and not st.ok and "429" in st.error
    # payload malformado: dict en vez de lista, tokens rotos
    src2 = _src(monkeypatch, {"unexpected": True})
    rows, st2 = src2.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == []
    bad = _caso_a_event(scores=[{"name": "a", "score": "6-4 kk 6-2"},
                                {"name": "b", "score": ""}])
    src3 = _src(monkeypatch, [bad])
    rows, _ = src3.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == []
    # cobertura vacía: sin sport keys ATP activas
    src4 = _src(monkeypatch, [], sports=[])
    rows, st4 = src4.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == [] and st4.ok


def test_inactive_without_key(monkeypatch):
    monkeypatch.delenv("BETBOT_ODDS_API_KEY", raising=False)
    src = OddsApiScores(api_key="")
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    assert rows == [] and "BETBOT_ODDS_API_KEY" in st.error


# ------------------------------------------------ el secreto jamás aparece

def test_secret_never_in_errors_or_notes(monkeypatch):
    secret = "sk_live_SECRETO123"
    src = OddsApiScores(api_key=secret)
    monkeypatch.setattr(
        "betbot.feeds.oddsapi_scores.cache.get_json",
        lambda url, ttl_seconds, headers=None:
        (_ for _ in ()).throw(RuntimeError(
            f"fuente inaccesible: https://api.the-odds-api.com/v4/sports/?apiKey={secret}")))
    rows, st = src.fetch_results(date(2026, 8, 4), date(2026, 8, 10))
    blob = json.dumps({"error": st.error, "notes": st.notes})
    assert secret not in blob and "apiKey=***" in blob


def test_cache_redacts_key_in_its_own_errors(monkeypatch, tmp_path):
    import betbot.feeds.cache as c
    monkeypatch.setattr(c, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        c.get_json("https://x.test/v4/scores/?daysFrom=3&apiKey=SECRETO999", ttl_seconds=0)
    assert "SECRETO999" not in str(ei.value) and "apiKey=***" in str(ei.value)
    assert redact("a?api_key=AB12&x=1") == "a?api_key=***&x=1"


def test_probe_without_key_makes_no_request_and_prints_no_secret(monkeypatch):
    from betbot.feeds.oddsapi_scores_probe import run_probe
    monkeypatch.delenv("BETBOT_ODDS_API_KEY", raising=False)
    cfg = copy.deepcopy(load_config())
    cfg["feeds"]["odds_api_key"] = None
    out = run_probe(cfg)
    assert "Falta BETBOT_ODDS_API_KEY" in out and "ninguna petición" in out
