"""Regresión de los tres bugs post-3a69328.

BUG 1 — wta_official.fetch_results devolvía "N leídos; 0 finalizados" mientras
el MISMO feed mostraba partidos terminados en scan --verbose. Causas reales
reproducidas: (a) el feed publica Winner como "1"/"2" y el parser solo aceptaba
"A"/"B"; (b) partidos finalizados solo con ResultString/ScoreString (todos los
ScoreSet* vacíos) se descartaban. La evidencia de finalización ahora es UNA
función común (has_completed_evidence) para calendario, puerta y sync.

Snapshot raw representativo de los tres casos reales del 2026-08-08:
Kostyuk–Swiatek, Lee–Knutson y Pegula–Shnaider, más controles (en juego con
marcador parcial, programado sin jugar, finalizado sin metadata de torneo).

BUG 2 — la identidad del torneo llegaba como genérico "WTA" (o CourtName) y el
registro de superficies nunca aplicaba. Ahora se lee la metadata REAL del
payload (Tournament{title,tournamentGroup,city}, Venue) — nunca CourtName — y
el genérico no finge confirmación.

BUG 3 — la matriz de capacidades atribuía wta_official a ATP. Cada adaptador
declara supported_tours y la matriz no puede inferir cobertura.
"""
import copy
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import SourceStatus
from betbot.feeds.wta_official import (WtaOfficialCalendar, has_completed_evidence,
                                       _winner_side, _tournament_identity)
from betbot.sync import local_freshness, run_sync
from betbot.tournaments import resolve_surface

TODAY = date(2026, 8, 9)
SNAPSHOT = Path(__file__).parent / "fixtures" / "wta_matches_snapshot_20260808.json"


@pytest.fixture()
def snapshot():
    return json.loads(SNAPSHOT.read_text())


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
        "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test", "match_id": f"{t}_2026-08-01_{a}__{b}"}
        for t, a, b in (("ATP", "alcaraz_c", "sinner_j"),
                        ("WTA", "gauff_c", "swiatek_i"))])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1, "display_name": p,
                   "hand": None, "dob": None}
                  for p, t in (("alcaraz_c", "ATP"), ("sinner_j", "ATP"),
                               ("gauff_c", "WTA"), ("swiatek_i", "WTA"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


# ============================================================== BUG 1

def test_snapshot_three_finished_matches_come_out_of_fetch_results(snapshot):
    """Los tres partidos reales del mandato DEBEN salir de fetch_results."""
    rows, n_raw, _ = WtaOfficialCalendar.parse_results(
        snapshot, date(2026, 8, 4), date(2026, 8, 9))
    assert n_raw == 6
    got = {(r["winner"], r["loser"], r["score"]) for r in rows}
    assert ("Swiatek I.", "Kostyuk M.", "3-6 6-1 6-2") in got
    assert ("Lee C.", "Knutson G.", "6-4 7-5") in got
    assert ("Shnaider D.", "Pegula J.", "6-3 6-3") in got
    for r in rows:
        assert r["date"] == "2026-08-08"
        assert r["tour"] == "WTA"
        assert r["status"] == "completed"
        assert r["round"]


def test_winner_numeric_convention_accepted():
    """El feed real publica Winner="1"/"2" (el consumidor Java verificado hace
    parseInt); "A"/"B" sigue aceptándose y la contradicción nunca se inventa."""
    base = {"ScoreSet1A": "6", "ScoreSet1B": "3",
            "ScoreSet2A": "6", "ScoreSet2B": "4"}
    assert _winner_side({**base, "Winner": "1"}) == "A"
    assert _winner_side({**base, "Winner": "A"}) == "A"
    assert _winner_side({"Winner": "2"}) == "B"
    assert _winner_side({"Winner": "B"}) == "B"
    # sin campo Winner: se deriva de los sets completos
    assert _winner_side(base) == "A"
    # contradicción campo vs sets -> '' (jamás adivinar)
    assert _winner_side({**base, "Winner": "2"}) == ""
    # "0" = sin decidir (convención real verificada)
    assert _winner_side({"Winner": "0"}) == ""


def test_scorestring_only_match_is_a_result(snapshot):
    """Lee–Knutson: todos los ScoreSet* vacíos, solo ResultString/ScoreString/
    MatchTimeTotal. Antes se descartaba; ahora sale con su marcador."""
    lee = [m for m in snapshot["matches"] if m["PlayerNameLastA"] == "Lee"]
    rows, _, _ = WtaOfficialCalendar.parse_results(
        {"matches": lee}, date(2026, 8, 8), date(2026, 8, 8))
    assert len(rows) == 1
    assert rows[0]["winner"] == "Lee C." and rows[0]["score"] == "6-4 7-5"


def test_live_partial_score_is_never_a_result_nor_completed(snapshot):
    """Rybakina–Keys en juego (set y medio, PointA/B, Serve): distinción
    completed vs live — un marcador parcial no es evidencia de final."""
    live = [m for m in snapshot["matches"] if m["MatchState"] == "P"]
    assert live
    done, _ = has_completed_evidence(live[0])
    assert not done
    rows, _, _ = WtaOfficialCalendar.parse_results(
        {"matches": live}, date(2026, 8, 8), date(2026, 8, 8))
    assert rows == []
    ms, _, _ = WtaOfficialCalendar.parse_matches(
        {"matches": live}, "2026-08-08T22:00:00+00:00")
    assert ms[0].status == "live"        # NO forzado a completed


def test_scheduled_unplayed_is_not_a_result(snapshot):
    gauff = [m for m in snapshot["matches"] if m["PlayerNameLastA"] == "Gauff"]
    done, _ = has_completed_evidence(gauff[0])
    assert not done
    rows, _, _ = WtaOfficialCalendar.parse_results(
        {"matches": gauff}, date(2026, 8, 8), date(2026, 8, 10))
    assert rows == []


def test_stale_matchstate_with_resultstring_counts_as_finished(snapshot):
    """Pegula–Shnaider llegó MatchState=U con ResultString ya publicado: la
    evidencia manda sobre el código de estado, en el MISMO sentido en scan
    (status=completed) y en sync (fila de resultado)."""
    peg = [m for m in snapshot["matches"] if m["PlayerNameLastA"] == "Pegula"]
    assert peg[0]["MatchState"] == "U"
    done, why = has_completed_evidence(peg[0])
    assert done and "ResultString" in why
    ms, _, _ = WtaOfficialCalendar.parse_matches(
        {"matches": peg}, "2026-08-08T22:00:00+00:00")
    assert ms[0].status == "completed"
    rows, _, _ = WtaOfficialCalendar.parse_results(
        {"matches": peg}, date(2026, 8, 8), date(2026, 8, 8))
    assert rows and rows[0]["winner"] == "Shnaider D."


def test_calendar_and_results_share_the_same_evidence(snapshot):
    """Consistencia obligada: un partido sale de fetch_results EXACTAMENTE
    cuando el calendario lo consideraría terminado (misma función común)."""
    for m in snapshot["matches"]:
        done, _ = has_completed_evidence(m)
        rows, _, _ = WtaOfficialCalendar.parse_results(
            {"matches": [m]}, date(2026, 8, 4), date(2026, 8, 10))
        cal, _, _ = WtaOfficialCalendar.parse_matches(
            {"matches": [m]}, "2026-08-08T22:00:00+00:00")
        assert (cal[0].status == "completed") == done
        # con ganador claro, la evidencia se convierte en fila sincronizable
        if done and _winner_side(m):
            assert len(rows) == 1


def test_sync_end_to_end_moves_wta_freshness(cfg_tmp, snapshot, monkeypatch):
    """betbot sync-results con el snapshot real: acepta los resultados WTA
    nuevos y mueve fresh_until WTA más allá de 2026-08-03."""
    monkeypatch.setattr("betbot.feeds.wta_official.cache.get_json",
                        lambda url, ttl_seconds, headers=None: snapshot)
    rep = run_sync(cfg_tmp, sources=[WtaOfficialCalendar()], today=TODAY)
    assert rep["n_accepted"] >= 3
    fresh = local_freshness(cfg_tmp)
    assert fresh["WTA"] == date(2026, 8, 8)
    assert fresh["WTA"] > date(2026, 8, 3)


# ============================================================== BUG 2

def test_tournament_identity_from_real_payload_metadata(snapshot):
    """El nombre real viaja hasta FeedMatch y el registro lo identifica: Hard
    por tournament_registry, no heurística."""
    ms, _, _ = WtaOfficialCalendar.parse_matches(snapshot, "2026-08-08T22:00:00+00:00")
    kos = [m for m in ms if "Kostyuk" in m.player1][0]
    assert "National Bank Open" in kos.tournament
    surf, src, info = resolve_surface("WTA", kos.tournament)
    assert surf == "Hard" and src == "tournament_registry"
    # y el resultado de sync lleva la misma identidad + superficie
    rows, _, _ = WtaOfficialCalendar.parse_results(
        snapshot, date(2026, 8, 8), date(2026, 8, 8))
    kos_r = [r for r in rows if r["winner"] == "Swiatek I."][0]
    assert "National Bank Open" in kos_r["tournament"]
    assert kos_r["surface"] == "Hard"


@pytest.mark.parametrize("name", ["Canadian Open", "National Bank Open",
                                  "Toronto", "Montreal",
                                  "National Bank Open Presented by Rogers - Montreal"])
def test_canadian_open_variants_resolve_hard_both_tours(name):
    for tour in ("ATP", "WTA"):
        surf, src, _ = resolve_surface(tour, name)
        assert surf == "Hard" and src == "tournament_registry", (tour, name)


def test_tournament_identity_field_priority():
    r = {"TournamentName": "Canadian Open", "CourtName": "Centre Court"}
    assert _tournament_identity(r)[0] == "Canadian Open"
    r = {"Tournament": {"title": "National Bank Open - Montreal",
                        "tournamentGroup": {"name": "National Bank Open"},
                        "city": "Montreal"}}
    assert _tournament_identity(r)[0] == "National Bank Open - Montreal"
    r = {"Tournament": {"tournamentGroup": {"name": "National Bank Open"}}}
    assert _tournament_identity(r)[0] == "National Bank Open"
    r = {"Tournament": {"city": "Montreal"}}
    assert _tournament_identity(r)[0] == "Montreal"


def test_courtname_is_never_a_tournament(snapshot):
    """Sin metadata real la identidad cae al genérico "WTA" (nunca CourtName)
    y NO finge confirmación de superficie: se queda inferred/unknown."""
    r = {"CourtName": "Centre Court"}
    assert _tournament_identity(r)[0] == "WTA"
    generic = [m for m in snapshot["matches"] if m["PlayerNameLastA"] == "Torres"]
    ms, _, _ = WtaOfficialCalendar.parse_matches(
        {"matches": generic}, "2026-08-08T22:00:00+00:00")
    assert ms[0].tournament == "WTA"
    surf, src, _ = resolve_surface("WTA", "WTA")
    assert surf is None and src == "unknown"
    rows, _, _ = WtaOfficialCalendar.parse_results(
        {"matches": generic}, date(2026, 8, 8), date(2026, 8, 8))
    assert rows[0]["surface"] == ""       # sin invención


# ============================================================== BUG 3

def test_adapters_declare_supported_tours():
    from betbot.feeds.espn import EspnFeed
    from betbot.feeds.oddsapi import OddsApiFeed
    from betbot.feeds.results import SportradarResults
    from betbot.feeds.results_github import GithubResults
    from betbot.feeds.thesportsdb import TheSportsDBCalendar
    assert WtaOfficialCalendar.supported_tours == {"WTA"}
    for cls in (EspnFeed, OddsApiFeed, SportradarResults, GithubResults,
                TheSportsDBCalendar):
        assert cls.supported_tours == {"ATP", "WTA"}, cls


def _matrix_sections(text: str) -> dict:
    """{'CALENDARIO': {'ATP': cell, 'WTA': cell}, ...} del texto de la matriz."""
    out: dict = {}
    section = None
    for line in text.splitlines():
        if line and not line.startswith(" ") and line.upper() == line and ":" not in line:
            section = line.strip()
            out.setdefault(section, {})
        elif section and line.strip().startswith(("ATP:", "WTA:")):
            t, cell = line.strip().split(":", 1)
            out[section][t] = cell.strip()
    return out


def test_matrix_never_attributes_wta_official_to_atp(cfg_tmp, snapshot, monkeypatch):
    from betbot.feeds.manage import feeds_matrix

    class FakeWta:
        name = "wta_official"
        authoritative = True
        supported_tours = frozenset({"WTA"})

        def fetch_matches(self, hours):
            ms, _, _ = WtaOfficialCalendar.parse_matches(
                snapshot, "2026-08-08T22:00:00+00:00")
            return ms, SourceStatus(name=self.name, ok=True, n_items=len(ms))

    class FakeBoth:
        name = "thesportsdb"
        authoritative = True
        supported_tours = frozenset({"ATP", "WTA"})

        def fetch_matches(self, hours):
            return [], SourceStatus(name=self.name, ok=True, n_items=0)

    class _Res:
        def __init__(self, name, tours):
            self.name, self.supported_tours = name, frozenset(tours)

    monkeypatch.setattr("betbot.scan.default_sources",
                        lambda cfg: ([FakeWta(), FakeBoth()], []))
    monkeypatch.setattr("betbot.sync.default_results_sources",
                        lambda cfg: [_Res("sportradar", {"ATP", "WTA"}),
                                     _Res("wta_official", {"WTA"}),
                                     _Res("github", {"ATP", "WTA"})])
    sec = _matrix_sections(feeds_matrix(cfg_tmp, hours=48))
    cal, res = sec["CALENDARIO"], sec["RESULTADOS"]
    assert "wta_official" not in cal["ATP"]
    assert "wta_official" in cal["WTA"] and "OK_CON_DATOS" in cal["WTA"]
    assert "thesportsdb" in cal["ATP"]          # la fuente ATP sigue listada
    assert "wta_official" not in res["ATP"]
    assert "sportradar" in res["ATP"] and "github" in res["ATP"]
    assert "wta_official" in res["WTA"]


def test_matrix_source_with_no_declared_tour_defaults_to_both(cfg_tmp, monkeypatch):
    """Un adaptador sin el atributo conserva el comportamiento anterior (ambos
    tours): la restricción es opt-in explícito, sin romper fuentes externas."""
    from betbot.feeds.manage import feeds_matrix

    class Legacy:
        name = "legacy"
        authoritative = True

        def fetch_matches(self, hours):
            return [], SourceStatus(name=self.name, ok=True, n_items=0)

    monkeypatch.setattr("betbot.scan.default_sources", lambda cfg: ([Legacy()], []))
    monkeypatch.setattr("betbot.sync.default_results_sources", lambda cfg: [])
    cal = _matrix_sections(feeds_matrix(cfg_tmp, hours=48))["CALENDARIO"]
    assert "legacy" in cal["ATP"] and "legacy" in cal["WTA"]
