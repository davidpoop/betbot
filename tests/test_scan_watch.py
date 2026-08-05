"""Tests de `betbot scan`/`watch`: descubrimiento, elegibilidad, matching,
mercados, mejor cuota, staleness, alertas y resiliencia de fuentes."""
import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.feeds.base import FeedMatch, SourceStatus, classify_level, dedupe_matches, strip_seed
from betbot.schemas import OddsQuote
from betbot import watchcmd

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"
TODAY = datetime.now(timezone.utc).date()
NOW_ISO = datetime.now(timezone.utc).isoformat()


class FakeCalendar:
    """Calendario AUTORITATIVO de prueba (publica estado + hora de inicio)."""
    name = "fake_cal"
    authoritative = True

    def __init__(self, matches, fail=False):
        self.matches, self.fail = matches, fail

    def fetch_matches(self, window_hours):
        if self.fail:
            raise RuntimeError("fuente caida")
        return self.matches, SourceStatus(name=self.name, ok=True, authoritative=True,
                                          n_items=len(self.matches))


class FakeOdds:
    name = "fake_odds"
    markets = ["match_winner"]

    def __init__(self, quotes, fail=False):
        self.quotes, self.fail = quotes, fail

    def fetch_odds(self, matches):
        if self.fail:
            raise RuntimeError("odds caida")
        return self.quotes, SourceStatus(name=self.name, ok=True, n_items=len(self.quotes))


def _fm(p1, p2, tour="ATP", hours_ahead=6, **kw):
    """Partido CONFIRMADO prepartido por defecto (estado + hora + fuente autoritativa)."""
    start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    base = dict(date=start.date(), tour=tour, tournament="Montreal", player1=p1, player2=p2,
                source="fake_cal", event_id=f"ev-{p1}-{p2}", scheduled_at_utc=start,
                status="scheduled", source_updated_at=NOW_ISO, start_tz="UTC",
                authoritative=True)
    base.update(kw)
    return FeedMatch(**base)


def _q(p1, p2, sel, odds, book="B1", market="match_winner", ts=None):
    return OddsQuote(player_a=p1, player_b=p2, market=market, selection=sel, odds=odds,
                     bookmaker=book, timestamp=ts or NOW_ISO)


# ---------------- parsers y elegibilidad ----------------

def test_strip_seed_and_level():
    assert strip_seed("Shelbayh A. (2)") == "Shelbayh A."
    assert strip_seed("Martin A. (q)") == "Martin A."
    assert strip_seed("Alcaraz C.") == "Alcaraz C."
    assert classify_level("Lexington challenger") == "challenger"
    assert classify_level("Toronto WTA") == "main"
    assert classify_level("United Cup") == "other"
    assert classify_level("Praga 125") == "other"


def test_github_te_parser(monkeypatch, tmp_path):
    from betbot.feeds import github_te
    fixture = {"last_updated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
               "count": 4, "matches": [
                   {"tournament": "Montreal", "time": "12:00", "player1": "Alcaraz C. (1)",
                    "player2": "Draper J.", "odds1": 1.5, "odds2": 2.6, "tour": "ATP"},
                   {"tournament": "Lexington challenger", "time": "12:00", "player1": "X Y.",
                    "player2": "Z W.", "odds1": 1.5, "odds2": 2.4, "tour": "ATP"},
                   {"tournament": "Toronto WTA", "time": "13:00",
                    "player1": "Swiatek I./Gauff C.", "player2": "A B./C D.",
                    "odds1": 1.6, "odds2": 2.2, "tour": "WTA"},
                   {"tournament": "Montreal", "time": "14:00", "player1": "Sin Cuota A.",
                    "player2": "Rival B.", "odds1": -1, "odds2": 2.0, "tour": "ATP"}]}
    monkeypatch.setattr(github_te.cache, "get_json", lambda url, ttl_seconds: fixture)
    feed = github_te.GithubTEFeed()
    ms, st = feed.fetch_matches(48)
    assert st.ok and len(ms) == 4
    assert ms[0].player1 == "Alcaraz C." and ms[0].level == "main"
    assert ms[1].level == "challenger"
    assert ms[2].is_doubles
    qs, st2 = feed.fetch_odds(ms)
    # cuota -1 (ausente) no se inventa: solo el lado real
    sincuota = [q for q in qs if q.player_a == "Sin Cuota A."]
    assert len(sincuota) == 1 and sincuota[0].selection == "b"
    both = [q for q in qs if q.player_a == "Alcaraz C."]
    assert {q.selection for q in both} == {"a", "b"}


def test_dedupe_matches_prefers_first_source():
    a = _fm("Alcaraz C.", "Draper J.", source="s1")
    b = _fm("Draper J.", "Alcaraz C.", source="s2")   # mismo partido, orden invertido
    out = dedupe_matches([a, b])
    assert len(out) == 1 and out[0].source == "s1"


# ---------------- run_scan con fuentes simuladas ----------------

@pytest.fixture()
def cfg_tmp(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    return cfg


needs_bundle = pytest.mark.skipif(not BUNDLE.exists(), reason="necesita modelos entrenados")


@needs_bundle
def test_scan_eligibility_and_coverage(cfg_tmp):
    from betbot.scan import run_scan
    matches = [
        _fm("Alcaraz C.", "Draper J."),
        _fm("X Y.", "Z W.", tournament="Hagen challenger", level="challenger"),
        _fm("Swiatek I.", "Gauff C.", tour="WTA", tournament="Toronto WTA"),
        _fm("A B.", "C D.", is_doubles=True),
        _fm("E F.", "G H.", is_qualifying=True),
    ]
    quotes = [_q("Alcaraz C.", "Draper J.", "a", 1.55), _q("Alcaraz C.", "Draper J.", "b", 2.6)]
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(matches)],
                   odds_sources=[FakeOdds(quotes)], show_rejected=True)
    s = res.summary
    assert s["found"] == 5 and s["eligible"] == 2
    assert s["excluded"]["challenger"] == 1 and s["excluded"]["doubles"] == 1 \
        and s["excluded"]["qualifying"] == 1
    assert s["matches_with_odds"] == 1 and s["odds_coverage_pct"] == 50.0
    assert "match_winner" in s["markets_with_quotes"]
    assert "set1_winner" in s["markets_missing_from_sources"]
    # Swiatek/Gauff sin cuota -> filas de solo-modelo; Alcaraz con mercado completo
    ml = res.rows[(res.rows["match"] == "Alcaraz C. vs Draper J.")
                  & (res.rows["market"] == "match_winner")]
    assert ml["p_novig"].notna().any()


@needs_bundle
def test_scan_source_failure_resilience(cfg_tmp):
    from betbot.scan import run_scan
    good = FakeCalendar([_fm("Alcaraz C.", "Draper J.")])
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar([], fail=True), good],
                   odds_sources=[FakeOdds([], fail=True),
                                 FakeOdds([_q("Alcaraz C.", "Draper J.", "a", 1.6),
                                           _q("Alcaraz C.", "Draper J.", "b", 2.4)])])
    s = res.summary
    assert s["eligible"] == 1 and s["matches_with_odds"] == 1
    fails = [x for x in s["sources"] if not x["ok"] and x["name"] in ("fake_cal", "fake_odds")]
    assert len(fails) == 2                     # ambas caídas registradas, escaneo continúa


@needs_bundle
def test_scan_multibook_best_odds_and_no_cross_book_novig(cfg_tmp):
    from betbot.scan import run_scan
    m = [_fm("Alcaraz C.", "Draper J.")]
    quotes = [
        _q("Alcaraz C.", "Draper J.", "a", 1.55, book="B1"),
        _q("Alcaraz C.", "Draper J.", "b", 2.50, book="B1"),
        _q("Alcaraz C.", "Draper J.", "a", 1.60, book="B2"),   # mejor cuota lado a, SIN lado b
    ]
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(m)],
                   odds_sources=[FakeOdds(quotes)], show_rejected=True, show_likely=True)
    rows = res.rows[res.rows["market"] == "match_winner"]
    b2 = rows[(rows["bookmaker"] == "B2") & (rows["selection"] == "a")].iloc[0]
    b1 = rows[(rows["bookmaker"] == "B1") & (rows["selection"] == "a")].iloc[0]
    assert bool(b2["best_odds"]) and not bool(b1["best_odds"])
    # B2 no tiene lado contrario: mercado parcial, sin no-vig inventado
    assert pd.isna(b2["p_novig"]) and "mercado_parcial" in b2["reasons"]
    assert pd.notna(b1["p_novig"])             # B1 completo: no se mezclan operadores


@needs_bundle
def test_scan_stale_quote_rejected_per_book(cfg_tmp):
    from betbot.scan import run_scan
    old = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    m = [_fm("Alcaraz C.", "Draper J.")]
    quotes = [_q("Alcaraz C.", "Draper J.", "a", 1.55, book="TE", ts=old),
              _q("Alcaraz C.", "Draper J.", "b", 2.6, book="TE", ts=old)]
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(m)],
                   odds_sources=[FakeOdds(quotes)], show_rejected=True)
    rows = res.rows[res.rows["bookmaker"] == "TE"]
    assert (rows["state"] == "descartada").all()
    assert rows["reasons"].str.contains("cuota_caducada").all()   # 9h > 8h (umbral TE)


@needs_bundle
def test_scan_unknown_player_counted_unresolved(cfg_tmp):
    from betbot.scan import run_scan
    m = [_fm("Jugador Inventado Q.", "Draper J.")]
    res = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(m)], odds_sources=[FakeOdds([])],
                   show_rejected=True)
    assert res.summary["unresolved_players"] == 1
    assert res.rows.iloc[0]["state"] == "descartada"


@needs_bundle
def test_scan_display_filters_do_not_touch_model(cfg_tmp):
    from betbot.scan import run_scan
    m = [_fm("Alcaraz C.", "Draper J.")]
    quotes = [_q("Alcaraz C.", "Draper J.", "a", 1.50), _q("Alcaraz C.", "Draper J.", "b", 2.7)]
    res_all = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(m)],
                       odds_sources=[FakeOdds(quotes)], show_rejected=True, show_likely=True)
    res_rng = run_scan(cfg_tmp, calendar_sources=[FakeCalendar(m)],
                       odds_sources=[FakeOdds(quotes)], show_rejected=True, show_likely=True,
                       min_odds=1.40, max_odds=1.60)
    # mismas evaluaciones subyacentes (el filtro es solo de visualización)
    assert len(res_all.rows) == len(res_rng.rows)
    assert set(res_rng.displayed["odds"].dropna()) <= {1.50}


# ---------------- watch: alertas ----------------

def _row(state="normal", odds=1.6, o_min=1.55, key_suffix="1"):
    return {"match": f"M{key_suffix}", "market": "match_winner", "selection": "a",
            "bookmaker": "B1", "state": state, "odds": odds, "o_min": o_min,
            "ev_cons": 0.04, "selection_name": "Alcaraz C."}


def test_watch_alert_dedupe_and_lifecycle():
    rows1 = pd.DataFrame([_row(state="fuerte")])
    sig1 = watchcmd.extract_signals(rows1)
    alerts1, alerted = watchcmd.diff_alerts({}, sig1, [], set())
    assert len(alerts1) == 1 and "FUERTE" in alerts1[0]
    # mismo ciclo repetido: sin nueva alerta
    alerts2, alerted = watchcmd.diff_alerts(sig1, sig1, [], alerted)
    assert alerts2 == []
    # desaparición: una única alerta
    alerts3, alerted = watchcmd.diff_alerts(sig1, {}, [], alerted)
    assert len(alerts3) == 1 and "DESAPARECIDA" in alerts3[0]
    alerts4, _ = watchcmd.diff_alerts({}, {}, [], alerted)
    assert alerts4 == []


def test_watch_o_min_crossing():
    prev = {"M1|match_winner|a|B1": 1.50}                 # antes por debajo de o_min
    rows = pd.DataFrame([_row(state="vigilar_precio", odds=1.58, o_min=1.55)])
    ev = watchcmd.detect_crossings(prev, rows)
    assert len(ev) == 1 and ev[0]["odds"] == 1.58
    # sin cruce si ya estaba por encima
    prev2 = {"M1|match_winner|a|B1": 1.57}
    assert watchcmd.detect_crossings(prev2, rows) == []
    alerts, alerted = watchcmd.diff_alerts({}, {}, ev, set())
    assert len(alerts) == 1 and "CRUCE" in alerts[0]
    alerts2, _ = watchcmd.diff_alerts({}, {}, ev, alerted)   # dedupe del cruce
    assert alerts2 == []
