"""Sportradar preparado para el trial: header auth, incremental, presupuesto,
backfill explícito y huecos que no declaran frescura.

Nada de esto toca modelo/calibración/thresholds/WTA. ATP queda "preparado
para reparación": el backfill real exige la SPORTRADAR_API_KEY del usuario.
"""
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.results import SportradarResults
from betbot.sync import default_results_sources, local_freshness, run_sync

TODAY = date(2026, 8, 10)


def _summary(day, winner="Alcaraz, Carlos", loser="Sinner, Jannik",
             match_status="ended", periods=((6, 4), (7, 5)), category="ATP",
             comp="Canadian Open", ctype="singles"):
    return {"sport_event": {
                "id": f"sr:sport_event:{day}:{winner[:3]}",
                "start_time": f"{day}T15:00:00+00:00",
                "sport_event_context": {
                    "category": {"name": category},
                    "competition": {"name": comp, "surface": "hard"},
                    "round": {"name": "quarterfinal"}},
                "competitors": [
                    {"id": "sr:c:1", "name": winner, "type": ctype},
                    {"id": "sr:c:2", "name": loser, "type": ctype}]},
            "sport_event_status": {
                "match_status": match_status, "status": match_status,
                "winner_id": "sr:c:1",
                "period_scores": [{"home_score": a, "away_score": b} for a, b in periods]}}


class _FakeCache:
    """Sustituto de cache.get_json/is_cached con contabilidad de red."""

    def __init__(self, by_day: dict, cached_days=(), fail_days=()):
        self.by_day = by_day
        self.cached = set(cached_days)
        self.fail = set(fail_days)
        self.network_calls: list[str] = []
        self.headers_seen: list[dict] = []
        self.urls_seen: list[str] = []

    def get_json(self, url, ttl_seconds, headers=None):
        self.urls_seen.append(url)
        self.headers_seen.append(dict(headers or {}))
        day = url.rsplit("/schedules/", 1)[1].split("/")[0]
        if day in self.fail:
            raise RuntimeError(f"HTTP 500: {url}")
        if day not in self.cached:
            self.network_calls.append(day)
        return {"summaries": self.by_day.get(day, [])}

    def is_cached(self, url, ttl_seconds):
        day = url.rsplit("/schedules/", 1)[1].split("/")[0]
        return day in self.cached


def _wire(monkeypatch, fake):
    monkeypatch.setattr("betbot.feeds.results.cache.get_json", fake.get_json)
    monkeypatch.setattr("betbot.feeds.results.cache.is_cached", fake.is_cached)


def _src(**kw):
    kw.setdefault("api_key", "trial_key")
    return SportradarResults(**kw)


# ------------------------------------------------ autenticación

def test_key_travels_in_header_never_in_url(monkeypatch):
    fake = _FakeCache({"2026-08-10": [_summary("2026-08-10")]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": TODAY, "WTA": TODAY})
    rows, st = src.fetch_results(TODAY - timedelta(days=2), TODAY)
    assert rows
    assert all("trial_key" not in u for u in fake.urls_seen)
    assert all("api_key" not in u for u in fake.urls_seen)
    assert all(h.get("x-api-key") == "trial_key" for h in fake.headers_seen)


def test_key_never_in_exception_or_status(monkeypatch):
    fake = _FakeCache({}, fail_days={"2026-08-09", "2026-08-10"})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": TODAY, "WTA": TODAY})
    _rows, st = src.fetch_results(TODAY - timedelta(days=2), TODAY)
    blob = json.dumps({"e": st.error, "n": st.notes})
    assert "trial_key" not in blob


def test_without_key_inactive_config(monkeypatch):
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    src = SportradarResults(api_key="")
    rows, st = src.fetch_results(TODAY - timedelta(days=3), TODAY)
    assert rows == [] and "SPORTRADAR_API_KEY" in st.error
    from betbot.feeds.manage import classify_status
    assert classify_status({"ok": st.ok, "error": st.error, "n": 0}) == "INACTIVO_CONFIG"


# ------------------------------------------------ incremental + replay

def test_fresh_today_only_replay_window_requested(monkeypatch):
    """Frescura al día => NO se recorre el lookback de 8 días: solo D-1 y D."""
    fake = _FakeCache({d.isoformat(): [] for d in
                       [TODAY - timedelta(days=i) for i in range(10)]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": TODAY, "WTA": TODAY}, replay_days=2)
    src.fetch_results(TODAY - timedelta(days=8), TODAY)
    assert fake.network_calls == ["2026-08-09", "2026-08-10"]


def test_missing_two_days_requests_only_those_plus_replay(monkeypatch):
    fake = _FakeCache({d.isoformat(): [] for d in
                       [TODAY - timedelta(days=i) for i in range(10)]})
    _wire(monkeypatch, fake)
    fresh = TODAY - timedelta(days=2)                 # faltan D+1, D+2
    src = _src(fresh_until={"ATP": fresh, "WTA": fresh}, replay_days=2)
    src.fetch_results(TODAY - timedelta(days=8), TODAY)
    # replay: 08-07..08-08 (D-1, D) + missing: 08-09, 08-10
    assert fake.network_calls == ["2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]


def test_cache_reduces_network_and_telemetry_reports_it(monkeypatch):
    days = [TODAY - timedelta(days=i) for i in range(3)]
    fake = _FakeCache({d.isoformat(): [] for d in days},
                      cached_days={"2026-08-08", "2026-08-09"})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": TODAY - timedelta(days=2),
                            "WTA": TODAY - timedelta(days=2)}, replay_days=2)
    _rows, st = src.fetch_results(TODAY - timedelta(days=8), TODAY)
    assert fake.network_calls == ["2026-08-07", "2026-08-10"]     # 08-08/09 de caché
    tele = next(n for n in st.notes if n.startswith("telemetría"))
    assert "dates_requested=4" in tele and "from_cache=2" in tele \
        and "network_requests=2" in tele and "rango=2026-08-07..2026-08-10" in tele


def test_ttl_by_day_age():
    src = _src()
    assert src._ttl_for(TODAY, TODAY) == src.ttl
    assert src._ttl_for(TODAY - timedelta(days=1), TODAY) == src.TTL_YESTERDAY
    assert src._ttl_for(TODAY - timedelta(days=5), TODAY) == src.TTL_PAST


# ------------------------------------------------ presupuesto y backfill

def test_budget_exceeded_spends_nothing_and_explains(monkeypatch):
    fake = _FakeCache({d.isoformat(): [] for d in
                       [date(2026, 8, 1) + timedelta(days=i) for i in range(10)]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 3), "WTA": TODAY},
               replay_days=2, max_requests=3)
    rows, st = src.fetch_results(date(2026, 8, 1), TODAY)
    assert rows == [] and fake.network_calls == []    # ni UNA petición
    assert "presupuesto insuficiente" in st.error
    assert "harían falta 9" in st.error and "--backfill-atp" in st.error


def test_backfill_flag_authorizes_the_spend(monkeypatch):
    fake = _FakeCache({d.isoformat(): [_summary(d.isoformat())] for d in
                       [date(2026, 8, 4) + timedelta(days=i) for i in range(7)]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 3), "WTA": TODAY},
               replay_days=2, max_requests=3, allow_backfill=True)
    rows, st = src.fetch_results(date(2026, 8, 4), TODAY)
    assert len(rows) == 7 and len(fake.network_calls) == 7
    assert not st.error


def test_run_sync_backfill_atp_starts_at_first_missing_date(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)

    seen = {}

    class Probe:
        name = "probe"
        supported_tours = frozenset({"ATP"})

        def fetch_results(self, since, until):
            from betbot.feeds.base import SourceStatus
            seen["since"], seen["until"] = since, until
            return [], SourceStatus(name="probe", ok=True)

    rep = run_sync(cfg, sources=[Probe()], today=TODAY, refresh=False,
                   backfill_tour="ATP")
    assert seen["since"] == date(2026, 8, 4)          # first_missing_date automático
    assert seen["until"] == TODAY
    assert rep["backfill"]["tour"] == "ATP"
    assert rep["backfill"]["first_missing_date"] == "2026-08-04"


def test_default_sources_backfill_flag_reaches_sportradar(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    normal = default_results_sources(cfg)
    back = default_results_sources(cfg, allow_backfill=True)
    sr_n = next(s for s in normal if s.name == "sportradar")
    sr_b = next(s for s in back if s.name == "sportradar")
    assert sr_n.allow_backfill is False and sr_b.allow_backfill is True
    assert sr_n.fresh_until.get("ATP") == date(2026, 8, 3)   # frescura inyectada


# ------------------------------------------------ estados, orientación, huecos

def test_completed_retired_walkover_and_live_mapping(monkeypatch):
    day = "2026-08-09"
    fake = _FakeCache({day: [
        _summary(day, match_status="ended"),
        _summary(day, winner="Fritz, Taylor", loser="Ruud, Casper",
                 match_status="closed"),
        _summary(day, winner="Zverev, Alexander", loser="Rune, Holger",
                 match_status="retired", periods=((6, 4), (2, 1))),
        _summary(day, winner="Draper, Jack", loser="Paul, Tommy",
                 match_status="walkover", periods=()),
        _summary(day, winner="Live, Player", loser="Other, One",
                 match_status="2nd_set"),               # live: JAMÁS entra
        _summary(day, winner="Sched, Player", loser="Next, One",
                 match_status="not_started"),
    ]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 8), "WTA": date(2026, 8, 8)})
    rows, _ = src.fetch_results(date(2026, 8, 5), TODAY)
    by_status = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    assert len(rows) == 4
    assert len(by_status["completed"]) == 2
    assert by_status["retired"][0]["winner"] == "Zverev A."
    assert by_status["walkover"][0]["score"] == ""
    assert not any("Live" in r["winner"] or "Sched" in r["winner"] for r in rows)


def test_winner_and_score_orientation(monkeypatch):
    """El ganador es competitors[1] (away): el marcador debe reorientarse."""
    day = "2026-08-09"
    summ = _summary(day, winner="Loser, Larry", loser="Winner, Wanda",
                    periods=((4, 6), (3, 6)))
    summ["sport_event_status"]["winner_id"] = "sr:c:2"   # gana el away
    fake = _FakeCache({day: [summ]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 8), "WTA": date(2026, 8, 8)})
    rows, _ = src.fetch_results(date(2026, 8, 5), TODAY)
    assert rows[0]["winner"] == "Winner W." and rows[0]["loser"] == "Loser L."
    assert rows[0]["score"] == "6-4 6-3"                 # orientado al ganador


def test_day_failure_truncates_and_never_declares_freshness_with_gap(monkeypatch):
    """Si 08-08 falla, 08-09 y 08-10 se RETIENEN: la frescura no salta el hueco
    y el siguiente sync reintenta desde el día fallido."""
    days = {d.isoformat(): [_summary(d.isoformat())] for d in
            [date(2026, 8, 7) + timedelta(days=i) for i in range(4)]}
    fake = _FakeCache(days, fail_days={"2026-08-08"})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 6), "WTA": TODAY},
               allow_backfill=True)
    rows, st = src.fetch_results(date(2026, 8, 5), TODAY)
    got_days = sorted({r["date"] for r in rows})
    assert got_days == ["2026-08-07"]                    # nada posterior al hueco
    assert "2026-08-09" not in fake.network_calls        # ni siquiera se pidió
    assert any("retenidos" in n for n in st.notes)
    assert "2026-08-08" in st.error


def test_partial_day_failure_keeps_earlier_days(monkeypatch):
    fake = _FakeCache({"2026-08-09": [_summary("2026-08-09")]},
                      fail_days={"2026-08-10"})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 8), "WTA": date(2026, 8, 8)})
    rows, st = src.fetch_results(date(2026, 8, 5), TODAY)
    assert len(rows) == 1 and st.ok                      # lo anterior se conserva


# ------------------------------------------------ integración sync

def _cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    canon = tmp_path / "canonical"
    canon.mkdir(exist_ok=True)
    cfg["paths"]["canonical_dir"] = str(canon)
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    cfg["paths"]["manual_dir"] = str(tmp_path / "manual")
    mirror = pd.DataFrame([{
        "tour": t, "date": d, "tournament": "T0", "series": "", "surface": "Hard",
        "indoor": False, "round": "R1", "best_of": 3, "player_a": a, "player_b": b,
        "raw_name_a": a, "raw_name_b": b, "label_a_wins": 1, "status": "completed",
        "sets_a": 2, "sets_b": 0, "games_a": 12, "games_b": 6, "set1_winner_a": 1,
        "rank_a": None, "rank_b": None, "pts_a": None, "pts_b": None,
        "odds_json": "{}", "source": "test", "match_id": f"{t}_{d}_{a}__{b}"}
        for t, d, a, b in (("ATP", date(2026, 8, 3), "alcaraz_c", "sinner_j"),
                           ("WTA", date(2026, 8, 9), "gauff_c", "swiatek_i"))])
    mirror.to_parquet(canon / "matches.parquet", index=False)
    pd.DataFrame([{"player_id": p, "tours": t, "n_matches": 1, "display_name": p,
                   "hand": None, "dob": None}
                  for p, t in (("alcaraz_c", "ATP"), ("sinner_j", "ATP"),
                               ("fritz_t", "ATP"), ("ruud_c", "ATP"),
                               ("gauff_c", "WTA"), ("swiatek_i", "WTA"))]
                 ).to_parquet(canon / "players.parquet", index=False)
    return cfg


def test_sportradar_rows_dedupe_against_tml_history(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    day = "2026-08-03"
    summ = _summary(day, winner="Alcaraz, Carlos", loser="Sinner, Jannik",
                    periods=((6, 4), (6, 3)))
    fake = _FakeCache({day: [summ]})
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 2), "WTA": TODAY},
               allow_backfill=True)
    rep = run_sync(cfg, sources=[src], today=date(2026, 8, 4), refresh=False)
    assert rep["n_accepted"] == 0
    assert rep["n_duplicates_skipped"] >= 1              # el histórico TML manda
    m = pd.read_parquet(Path(cfg["paths"]["canonical_dir"]) / "matches.parquet")
    assert len(m) == 2                                   # intacto


def test_sync_end_to_end_moves_atp_freshness_with_real_engine(tmp_path, monkeypatch):
    cfg = _cfg_tmp(tmp_path)
    days = {d.isoformat(): [_summary(d.isoformat(), winner="Fritz, Taylor",
                                     loser="Ruud, Casper")]
            for d in [date(2026, 8, 4) + timedelta(days=i) for i in range(6)]}
    fake = _FakeCache(days)
    _wire(monkeypatch, fake)
    src = _src(fresh_until={"ATP": date(2026, 8, 3), "WTA": date(2026, 8, 9)},
               allow_backfill=True)
    rep = run_sync(cfg, sources=[src], today=date(2026, 8, 10), refresh=False,
                   backfill_tour="ATP")
    assert rep["n_accepted"] >= 1
    assert local_freshness(cfg)["ATP"] > date(2026, 8, 3)
    assert rep["freshness_after"]["ATP"] > "2026-08-03"


def test_wta_keeps_wta_official_as_primary_order():
    cfg = load_config()
    order = cfg["feeds"]["results_order"]
    assert order.index("wta_official") < order.index("sportradar")
    names = [s.name for s in default_results_sources(cfg)]
    assert names.index("wta_official") < names.index("sportradar")
    assert names[-1] == "github"
