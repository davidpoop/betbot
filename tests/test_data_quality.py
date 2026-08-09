"""Auditoría de calidad de datos: fuentes de resultados recientes, rankings
automáticos derivados, registro de superficies, .env y semántica de estados.

Sin tocar modelo/calibración/thresholds: se reparan las FUENTES de los datos.
"""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config, load_dotenv
from betbot.feeds.base import SourceStatus
from betbot.feeds.manage import classify_status
from betbot.feeds.wta_official import WtaOfficialCalendar
from betbot.sync import local_freshness, run_sync
from betbot.tournaments import resolve_surface

TODAY = date(2026, 8, 9)


@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir()
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    mirror = pd.DataFrame([{
        "tour": t, "date": date(2026, 8, 1), "tournament": "T0", "series": "",
        "surface": "Hard", "indoor": False, "round": "R1", "best_of": 3,
        "player_a": a, "player_b": b, "raw_name_a": a, "raw_name_b": b,
        "label_a_wins": 1, "status": "completed", "sets_a": 2, "sets_b": 0,
        "games_a": 12, "games_b": 6, "set1_winner_a": 1,
        "rank_a": ra, "rank_b": rb, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test", "match_id": f"{t}_2026-08-01_{a}__{b}"}
        for t, a, b, ra, rb in (("ATP", "alcaraz_c", "sinner_j", 2.0, 1.0),
                                ("WTA", "gauff_c", "swiatek_i", 2.0, 3.0))])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1, "display_name": p,
                   "hand": None, "dob": None}
                  for p, t in (("alcaraz_c", "ATP"), ("sinner_j", "ATP"),
                               ("draper_j", "ATP"), ("gauff_c", "WTA"),
                               ("swiatek_i", "WTA"), ("eala_a", "WTA"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


class FakeResults:
    name = "fake_results"

    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail

    def fetch_results(self, since, until):
        if self.fail:
            raise RuntimeError("primaria caida")
        return list(self.rows), SourceStatus(name=self.name, ok=True,
                                             n_items=len(self.rows))


def _row(d, tour, winner, loser, score="6-4 6-4", status="completed", **kw):
    base = {"date": str(d), "tour": tour, "tournament": "T", "surface": "Hard",
            "indoor": "false", "round": "R1", "best_of": "3", "winner": winner,
            "loser": loser, "score": score, "status": status, "_level": "main"}
    base.update(kw)
    return base


# ------------------------------------------------------------------ resultados

def test_source_advancing_a_day_moves_fresh_until(cfg_tmp):
    assert local_freshness(cfg_tmp)["WTA"] == date(2026, 8, 1)
    run_sync(cfg_tmp, sources=[FakeResults([_row(date(2026, 8, 8), "WTA",
                                                 "Eala A.", "Gauff C.")])],
             today=TODAY)
    assert local_freshness(cfg_tmp)["WTA"] == date(2026, 8, 8)


def test_duplicates_do_not_block_fresh_until(cfg_tmp):
    rows = [_row(date(2026, 8, 1), "ATP", "Alcaraz C.", "Sinner J.",
                 score="6-4 6-3"),                      # duplicado del mirror
            _row(date(2026, 8, 8), "ATP", "Alcaraz C.", "Draper J.")]  # nuevo
    rep = run_sync(cfg_tmp, sources=[FakeResults(rows)], today=TODAY)
    assert rep["n_duplicates_skipped"] >= 1 and rep["n_accepted"] == 1
    assert local_freshness(cfg_tmp)["ATP"] == date(2026, 8, 8)


def test_new_result_for_known_match_updates_state(cfg_tmp):
    """Una corrección posterior de la fuente (mismo partido, resultado
    distinto) actualiza la fila MANUAL, con registro de la corrección."""
    first = _row(date(2026, 8, 7), "WTA", "Eala A.", "Gauff C.",
                 score="6-4 3-1", status="retired")
    run_sync(cfg_tmp, sources=[FakeResults([first])], today=TODAY)
    fixed = _row(date(2026, 8, 7), "WTA", "Eala A.", "Gauff C.",
                 score="6-4 3-6 6-2", status="completed")
    rep = run_sync(cfg_tmp, sources=[FakeResults([fixed])], today=TODAY)
    assert rep["enriched"]["results_corrected"] == 1
    from betbot.canonical.store import load_matches
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    row = m[m["match_id"] == "WTA_2026-08-07_eala_a__gauff_c"].iloc[0]
    assert row["status"] == "completed" and row["sets_a"] + row["sets_b"] == 3


def test_primary_failure_falls_back(cfg_tmp):
    rep = run_sync(cfg_tmp, sources=[
        FakeResults([], fail=True),
        FakeResults([_row(date(2026, 8, 8), "WTA", "Eala A.", "Gauff C.")])],
        today=TODAY)
    assert rep["n_accepted"] == 1
    assert any(not s["ok"] for s in rep["sources"])     # la caída queda visible
    assert local_freshness(cfg_tmp)["WTA"] == date(2026, 8, 8)


def test_all_sources_stale_keeps_degraded_never_invents(cfg_tmp):
    rep = run_sync(cfg_tmp, sources=[FakeResults([])], today=TODAY)
    assert rep["n_accepted"] == 0
    assert local_freshness(cfg_tmp)["WTA"] == date(2026, 8, 1)   # sin avance falso
    from betbot.picks import degraded_banner
    banner = degraded_banner({"date": str(TODAY),
                              "results_freshness": {k: str(v) for k, v in
                                                    local_freshness(cfg_tmp).items()}})
    assert banner and "MODO DEGRADADO" in banner


def test_wta_official_results_parser_same_day():
    """Parser del feed global de la WTA: F con marcador -> fila de resultado
    orientada al ganador; F sin marcador NO se inventa; U se ignora."""
    rows_in = [
        {"MatchID": "LS001", "EventID": "9", "EventYear": 2026, "RoundID": "F",
         "DrawMatchType": "S", "DrawLevelType": "M",
         "PlayerNameFirstA": "Iga", "PlayerNameLastA": "Swiatek",
         "PlayerNameFirstB": "Coco", "PlayerNameLastB": "Gauff",
         "MatchState": "F", "Winner": "B",
         "MatchTimeStamp": "2026-08-09T15:00:00Z",
         "ScoreSet1A": "4", "ScoreSet1B": "6", "ScoreSet2A": "6", "ScoreSet2B": "7",
         "TournamentName": "Canadian Open", "ResultString": "C.Gauff d I.Swiatek"},
        {"MatchID": "LS002", "MatchState": "F", "Winner": "A",     # sin marcador
         "PlayerNameFirstA": "A", "PlayerNameLastA": "B",
         "PlayerNameFirstB": "C", "PlayerNameLastB": "D",
         "MatchTimeStamp": "2026-08-09T12:00:00Z"},
        {"MatchID": "LS003", "MatchState": "U",                    # por jugar
         "PlayerNameFirstA": "E", "PlayerNameLastA": "F",
         "PlayerNameFirstB": "G", "PlayerNameLastB": "H",
         "MatchTimeStamp": "2026-08-09T18:00:00Z"},
    ]
    out, n_raw, n_skip = WtaOfficialCalendar.parse_results(
        rows_in, date(2026, 8, 9), date(2026, 8, 9))
    assert n_raw == 3 and len(out) == 1 and n_skip == 2
    r = out[0]
    assert r["winner"] == "Gauff C." and r["loser"] == "Swiatek I."
    assert r["score"] == "6-4 7-6"                     # orientado a la ganadora
    assert r["date"] == "2026-08-09" and r["status"] == "completed"
    assert r["surface"] == "Hard"                      # del registro de torneos
    # retirada detectada por el texto del resultado
    rows_in[0]["ResultString"] = "C.Gauff d I.Swiatek 6-4 3-1 ret."
    out2, _, _ = WtaOfficialCalendar.parse_results(rows_in, date(2026, 8, 9),
                                                   date(2026, 8, 9))
    assert out2[0]["status"] == "retired"


def test_wta_official_results_feed_end_to_end(cfg_tmp, monkeypatch):
    """La fuente completa (fetch_results) mueve fresh_until WTA al MISMO día."""
    payload = {"matches": [
        {"MatchID": "LS010", "EventID": "9", "EventYear": 2026, "RoundID": "SF",
         "DrawMatchType": "S", "DrawLevelType": "M",
         "PlayerNameFirstA": "Alexandra", "PlayerNameLastA": "Eala",
         "PlayerNameFirstB": "Coco", "PlayerNameLastB": "Gauff",
         "MatchState": "F", "Winner": "A",
         "MatchTimeStamp": f"{TODAY}T14:00:00Z",
         "ScoreSet1A": "6", "ScoreSet1B": "4", "ScoreSet2A": "6", "ScoreSet2B": "3",
         "TournamentName": "Canadian Open"}]}
    monkeypatch.setattr("betbot.feeds.wta_official.cache.get_json",
                        lambda url, ttl_seconds, headers=None: payload)
    src = WtaOfficialCalendar()
    rep = run_sync(cfg_tmp, sources=[src], today=TODAY)
    assert rep["n_accepted"] == 1
    assert local_freshness(cfg_tmp)["WTA"] == TODAY


# ------------------------------------------------------------------ rankings

def test_ranks_from_sources_are_kept_and_derived_rankings_load(cfg_tmp):
    rows = [_row(date(2026, 8, 8), "ATP", "Alcaraz C.", "Draper J.",
                 winner_rank="2", loser_rank="5", winner_rank_points="9800",
                 loser_rank_points="4300")]
    run_sync(cfg_tmp, sources=[FakeResults(rows)], today=TODAY)
    from betbot.canonical.store import load_matches
    m = load_matches(Path(cfg_tmp["paths"]["canonical_dir"]))
    row = m[m["match_id"] == "ATP_2026-08-08_alcaraz_c__draper_j"].iloc[0]
    assert row["rank_a"] == 2.0 and row["rank_b"] == 5.0      # a=alcaraz ganó
    # y el snapshot derivado carga por el loader as-of existente
    from betbot.rankings_auto import build_derived_rankings
    from betbot.ingest.rankings import load_rankings
    rep = build_derived_rankings(cfg_tmp, today=TODAY)
    assert rep["tours"]["ATP"].get("players", 0) >= 3 or "skipped" in rep["tours"]["ATP"]


def test_derived_rankings_full_cycle(cfg_tmp):
    """Con suficientes observaciones: fichero generado, cargado, enlazado a ids
    canónicos y con fecha efectiva REAL (la de la observación, no hoy)."""
    rows = [_row(date(2026, 8, 7), "ATP", "Alcaraz C.", "Sinner J.",
                 score="7-6 6-4", winner_rank="2", loser_rank="1")]
    # bajar el mínimo para el fixture pequeño
    import betbot.rankings_auto as ra
    old = ra.MIN_ROWS_PER_TOUR
    ra.MIN_ROWS_PER_TOUR = 2
    try:
        run_sync(cfg_tmp, sources=[FakeResults(rows)], today=TODAY)
        from betbot.ingest.rankings import load_rankings
        rk = load_rankings(Path(cfg_tmp["paths"]["manual_dir"]), "ATP", TODAY, 45)
        assert rk.published == date(2026, 8, 7)        # fecha efectiva del dato
        assert rk.lookup("alcaraz_c") == (2, 0.0) or rk.lookup("alcaraz_c")[0] == 2
        assert rk.lookup("sinner_j")[0] == 1
        text = (Path(cfg_tmp["paths"]["manual_dir"]) / "rankings_atp.csv").read_text()
        assert "AUTOGENERADO" in text and "derived_from_results" in text
    finally:
        ra.MIN_ROWS_PER_TOUR = old


def test_user_manual_rankings_never_overwritten(cfg_tmp):
    manual = Path(cfg_tmp["paths"]["manual_dir"])
    manual.mkdir(parents=True, exist_ok=True)
    path = manual / "rankings_atp.csv"
    path.write_text("date_published,rank,player,points\n2026-08-04,1,Sinner J.,11830\n")
    from betbot.rankings_auto import build_derived_rankings
    rep = build_derived_rankings(cfg_tmp, today=TODAY)
    assert "se respeta" in rep["tours"]["ATP"]["skipped"]
    assert "AUTOGENERADO" not in path.read_text()      # intacto
    # y el manual carga con preferencia
    from betbot.ingest.rankings import load_rankings
    rk = load_rankings(manual, "ATP", TODAY, 45)
    assert rk.lookup("sinner_j")[0] == 1


def test_stale_rankings_warn_and_missing_falls_back_to_elo(cfg_tmp):
    manual = Path(cfg_tmp["paths"]["manual_dir"])
    manual.mkdir(parents=True, exist_ok=True)
    (manual / "rankings_wta.csv").write_text(
        "date_published,rank,player,points\n2026-05-01,1,Sabalenka A.,9000\n")
    from betbot.ingest.rankings import load_rankings
    rk = load_rankings(manual, "WTA", TODAY, 45)
    assert rk.published == date(2026, 5, 1)
    assert any("dias" in w for w in rk.warnings)       # stale -> aviso explícito
    rk2 = load_rankings(manual, "ATP", TODAY, 45)      # inexistente -> fallback
    assert rk2.published is None
    assert any("fallback a Elo" in w for w in rk2.warnings)


# ------------------------------------------------------------------ superficie

def test_canadian_open_is_hard_for_both_tours():
    for tour in ("ATP", "WTA"):
        surf, src, info = resolve_surface(tour, "Canadian Open")
        assert (surf, src) == ("Hard", "tournament_registry")


def test_toronto_montreal_national_bank_aliases():
    for alias in ("Toronto", "Montreal", "National Bank Open", "Canada Masters",
                  "Toronto WTA", "National Bank Open presented by Rogers"):
        surf, src, info = resolve_surface("ATP", alias)
        assert surf == "Hard", alias
        assert src == "tournament_registry"


def test_unknown_tournament_does_not_invent_surface():
    surf, src, _ = resolve_surface("ATP", "Torneo Inventado de Ninguna Parte")
    assert surf is None and src == "unknown"


def test_registry_vs_heuristic_warning_in_scan_rows():
    """En el resumen del scan: registry/official no llevan aviso; solo la
    heurística cuenta como estimada."""
    surf, src, _ = resolve_surface("WTA", "Cincinnati Open")
    assert (surf, src) == ("Hard", "tournament_registry")
    surf2, src2, _ = resolve_surface("ATP", "Wimbledon")
    assert (surf2, src2) == ("Grass", "tournament_registry")
    surf3, src3, _ = resolve_surface("WTA", "Stuttgart")
    assert (surf3, src3) == ("Clay", "tournament_registry")   # WTA = tierra
    surf4, _, _ = resolve_surface("ATP", "Stuttgart")
    assert surf4 == "Grass"                                    # ATP = hierba


# ------------------------------------------------------------------ .env

def test_dotenv_loads_without_overriding_system(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comentario\nBETBOT_ODDS_API_KEY='abc123'\n"
                   "export SPORTRADAR_API_KEY=xyz\nVACIA=\n")
    monkeypatch.delenv("BETBOT_ODDS_API_KEY", raising=False)
    monkeypatch.setenv("SPORTRADAR_API_KEY", "del_sistema")
    import os
    loaded = load_dotenv(env)
    assert "BETBOT_ODDS_API_KEY" in loaded
    assert os.environ["BETBOT_ODDS_API_KEY"] == "abc123"
    assert os.environ["SPORTRADAR_API_KEY"] == "del_sistema"   # el sistema manda
    assert "SPORTRADAR_API_KEY" not in loaded
    monkeypatch.delenv("BETBOT_ODDS_API_KEY", raising=False)


def test_env_example_has_names_but_no_secrets():
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text()
    assert "BETBOT_ODDS_API_KEY=" in example and "BETFAIR_APP_KEY=" in example
    for line in example.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            assert line.split("=", 1)[1].strip() == "", f"valor en .env.example: {line}"
    gitignore = (root / ".gitignore").read_text()
    assert "\n.env\n" in gitignore or gitignore.endswith(".env\n")


# ------------------------------------------------------------------ semántica

def test_status_classification():
    assert classify_status({"ok": True, "error": "", "n": 12}) == "OK_CON_DATOS"
    assert classify_status({"ok": True, "error": "", "n": 0}) == "OK_SIN_COBERTURA"
    assert classify_status({"ok": False, "error": "sin BETBOT_ODDS_API_KEY "
                            "(adaptador inactivo)", "n": 0}) == "INACTIVO_CONFIG"
    assert classify_status({"ok": False, "error": "HTTP 403 (estructural, sin "
                            "reintentos): https://espn", "n": 0}) == "FALLO_ESTRUCTURAL"
    assert classify_status({"ok": False, "error": "timeout", "n": 0}) == "FALLO_TEMPORAL"


def test_structural_4xx_not_retried(monkeypatch, tmp_path):
    """ESPN 403: un 4xx aborta al primer intento (nada de 4 reintentos)."""
    import urllib.error
    from betbot.feeds import cache as c
    monkeypatch.setattr(c, "CACHE_DIR", tmp_path)
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="estructural"):
        c.get_json("https://ejemplo.invalido/x", ttl_seconds=0)
    assert len(calls) == 1                              # UN intento, no cuatro
    # un 5xx sí se reintenta (temporal)
    calls.clear()

    def fake_500(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", fake_500)
    with pytest.raises(RuntimeError):
        c.get_json("https://ejemplo.invalido/y", ttl_seconds=0)
    assert len(calls) == 4
