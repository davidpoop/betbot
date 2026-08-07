"""Rediseño de la salida diaria: deduplicación por oportunidad económica,
TOP PICKS (máx. 10, sin mínimo), watchlist deduplicada, --verbose diagnóstico
y regresión de la hora placeholder 03:59 en la salida operativa.

Sin tocar modelos/calibración/thresholds: se agregan evaluaciones ya calculadas.
"""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from betbot.config import load_config
from betbot.picks import (MAX_TOP_PICKS, aggregate_opportunities, degraded_banner,
                          render_daily, select_top_picks, watchlist)
from betbot.scan import ScanResult

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"
needs_bundle = pytest.mark.skipif(not BUNDLE.exists(), reason="necesita modelos entrenados")
NOW_ISO = datetime.now(timezone.utc).isoformat()


def _row(match="Eala A. vs McNally C.", market="match_winner", selection="a",
         selection_name="Eala A.", bookmaker="betfair_ex_eu", odds=1.44,
         o_min=1.43, state="normal", ev_cons=0.039, edge=0.032, p_model=0.722,
         odds_age_min=5.0, reasons="", recommended_primary=False,
         event_id="wta:1001:2026:LS001:eala_a__mcnally_c",
         start_utc="2026-08-08T15:00:00+00:00"):
    return {"match": match, "tour": "WTA", "tournament": "Cincinnati",
            "market": market, "selection": selection, "selection_name": selection_name,
            "bookmaker": bookmaker, "odds": odds, "o_min": o_min, "p_model": p_model,
            "p_novig": 0.69, "edge": edge,
            "ev": (ev_cons + 0.01) if ev_cons is not None else None, "ev_cons": ev_cons,
            "state": state, "reasons": reasons, "recommended_primary": recommended_primary,
            "odds_age_min": odds_age_min, "fair_odds": 1.39, "best_odds": False,
            "event_id": event_id, "start_utc": start_utc, "event_status": "scheduled",
            "calendar_source": "espn", "calendar_updated_at": NOW_ISO,
            "calendar_status_age_h": 0.1}


def _res(rows, **summary_extra):
    df = pd.DataFrame(rows)
    from betbot.picks import select_top_picks as stp, watchlist as wl_fn
    opps = aggregate_opportunities(df)
    top = stp(opps)
    wl, near = wl_fn(opps)
    summary = {"date": "2026-08-07", "window_hours": 48, "eligible": 10,
               "found": 12, "calendar_confirmed": 10,
               "excluded": {"doubles": 0, "qualifying": 0, "challenger": 0,
                            "itf": 0, "other": 0, "tour": 0},
               "market_coverage": {"match_winner": 10},
               "states": df["state"].value_counts().to_dict(),
               "markets_evaluated": len(df), "opportunities": len(opps),
               "analyzed_matches": int(df["match"].nunique()),
               "unresolved_players": 0, "matches_with_odds": 10,
               "odds_coverage_pct": 100.0, "markets_with_quotes": ["match_winner"],
               "markets_missing_from_sources": [], "stale_discarded": 0,
               "structured": {"active": False, "providers": [], "events": []},
               "results_freshness": {"ATP": "2026-08-07", "WTA": "2026-08-07"},
               "sources": [], "prematch_excluded": {}, "orphan_quotes": 0}
    summary.update(summary_extra)
    return ScanResult(summary=summary, rows=df, displayed=df, opportunities=opps,
                      top_picks=top, watchlist=wl, watchlist_near=near)


# --------------------------------------------------------------------------
# 1. deduplicación: 20 bookmakers de la misma selección => 1 oportunidad
# --------------------------------------------------------------------------

def test_twenty_bookmakers_one_opportunity():
    rows = [_row(bookmaker=f"book_{i:02d}", odds=1.45 - i * 0.001,
                 odds_age_min=float(i)) for i in range(20)]
    opps = aggregate_opportunities(pd.DataFrame(rows))
    assert len(opps) == 1
    o = opps[0]
    assert o.n_books == 20
    assert o.best_odds == 1.45 and o.best_bookmaker == "book_00"   # la mejor
    assert o.second_odds == 1.449 and o.second_bookmaker == "book_01"


def test_osaka_ladder_is_one_watchlist_entry():
    """El caso real: Osaka ML repetida a 1.45, 1.44, 1.43... por muchas casas."""
    rows = [_row(match="Osaka N. vs Rival X.", selection_name="Osaka N.",
                 event_id="wta:1:2026:LS002:osaka_n__rival_x",
                 bookmaker=f"b{i}", odds=1.45 - i * 0.01, o_min=1.50,
                 state="vigilar_precio", ev_cons=None, edge=None)
            for i in range(6)]
    opps = aggregate_opportunities(pd.DataFrame(rows))
    assert len(opps) == 1
    wl, near = watchlist(opps)
    assert len(wl) == 1 and wl[0].best_odds == 1.45 and wl[0].n_books == 6


def test_same_event_different_order_or_label_still_one():
    """El event_id estable manda: etiquetas distintas del mismo evento no crean
    dos oportunidades."""
    rows = [_row(match="Eala A. vs McNally C.", bookmaker="betfair"),
            _row(match="McNally C. vs Eala A.", bookmaker="matchbook", odds=1.43)]
    opps = aggregate_opportunities(pd.DataFrame(rows))
    assert len(opps) == 1 and opps[0].n_books == 2


# --------------------------------------------------------------------------
# 2-3. máximo 10, sin relleno artificial
# --------------------------------------------------------------------------

def test_top_picks_never_more_than_ten():
    rows = []
    for i in range(15):
        rows.append(_row(match=f"J{i} A. vs R{i} B.", selection_name=f"J{i} A.",
                         event_id=f"ev{i}", state="fuerte",
                         ev_cons=0.05 + i * 0.001))
    top = select_top_picks(aggregate_opportunities(pd.DataFrame(rows)))
    assert len(top) == MAX_TOP_PICKS == 10


def test_top_picks_no_artificial_fill():
    rows = [_row(state="normal"),
            _row(match="Osaka N. vs Rival X.", event_id="ev2",
                 state="vigilar_precio", o_min=1.50, ev_cons=None)]
    res = _res(rows)
    assert len(res.top_picks) == 1              # solo la que cumple, jamás 10
    out = render_daily(res)
    assert "TOP PICKS (1 de máx. 10)" in out


def test_zero_picks_is_a_normal_result():
    rows = [_row(state="sin_value", ev_cons=-0.01)]
    res = _res(rows)
    assert res.top_picks == []
    out = render_daily(res)
    assert "TOP PICKS: 0" in out
    assert "No hay apuestas que superen todos los filtros hoy." in out


# --------------------------------------------------------------------------
# 4-5. filtros de entrada
# --------------------------------------------------------------------------

def test_vigilar_and_lower_states_never_in_top_picks():
    rows = [_row(match=f"M{i} A. vs N{i} B.", event_id=f"e{i}", state=st,
                 ev_cons=0.05)
            for i, st in enumerate(["vigilar_precio", "probable_sin_value",
                                    "sin_value", "descartada", "experimental"])]
    top = select_top_picks(aggregate_opportunities(pd.DataFrame(rows)))
    assert top == []


def test_best_odds_below_o_min_never_enters():
    """Estado normal pero la mejor cuota quedó por debajo de la mínima: fuera."""
    rows = [_row(odds=1.42, o_min=1.43, state="normal")]
    top = select_top_picks(aggregate_opportunities(pd.DataFrame(rows)))
    assert top == []


def test_grave_vetoes_block_top_picks():
    for flag in ("data_freshness_unknown(WTA hasta 2026-07-01, 30d de retraso)",
                 "ood_pocos_partidos_12m:x(m12=2)", "cuota_caducada(9.0h)"):
        rows = [_row(state="normal", reasons=flag)]
        assert select_top_picks(aggregate_opportunities(pd.DataFrame(rows))) == []


def test_stale_best_quote_falls_back_to_executable():
    """La cuota más alta está caducada (descartada): la oportunidad opera con
    la mejor cuota EJECUTABLE, no con la caducada."""
    rows = [_row(bookmaker="viejo", odds=1.50, state="descartada",
                 reasons="cuota_caducada(9.4h)"),
            _row(bookmaker="fresco", odds=1.44, state="normal")]
    opps = aggregate_opportunities(pd.DataFrame(rows))
    assert len(opps) == 1
    o = opps[0]
    assert o.best_bookmaker == "fresco" and o.best_odds == 1.44
    assert o.state == "normal"
    assert select_top_picks(opps)[0].best_bookmaker == "fresco"


# --------------------------------------------------------------------------
# 6-7. mejor cuota mostrada y ranking
# --------------------------------------------------------------------------

def test_best_quote_is_the_one_rendered():
    rows = [_row(bookmaker="matchbook", odds=1.43),
            _row(bookmaker="betfair_ex_eu", odds=1.44)]
    res = _res(rows)
    out = render_daily(res)
    assert "Mejor cuota: 1.44 @ betfair_ex_eu" in out
    assert "2ª: 1.43 @ matchbook" in out
    # la misma apuesta NO aparece una vez por bookmaker
    assert out.count("Eala A. ML vs McNally C.") == 1


def test_ranking_fuerte_before_normal_then_ev_edge_freshness():
    rows = [
        _row(match="A A. vs B B.", event_id="e1", state="normal", ev_cons=0.09),
        _row(match="C C. vs D D.", event_id="e2", state="fuerte", ev_cons=0.05),
        _row(match="E E. vs F F.", event_id="e3", state="fuerte", ev_cons=0.07),
        _row(match="G G. vs H H.", event_id="e4", state="normal", ev_cons=0.09,
             edge=0.05),
        _row(match="I I. vs J J.", event_id="e5", state="normal", ev_cons=0.09,
             edge=0.05, odds_age_min=1.0),
    ]
    top = select_top_picks(aggregate_opportunities(pd.DataFrame(rows)))
    states = [o.state for o in top]
    assert states[:2] == ["fuerte", "fuerte"]           # fuerte antes que normal
    assert top[0].ev_cons == 0.07                        # EV desc dentro de fuerte
    # dentro de normal: mismo EV -> más edge primero; mismo edge -> más fresca
    normals = [o for o in top if o.state == "normal"]
    assert normals[0].edge == 0.05 and normals[0].odds_age_min == 1.0
    assert normals[-1].edge == 0.032


def test_recommended_primary_lives_at_opportunity_level():
    rows = [_row(bookmaker="betfair", recommended_primary=True),
            _row(bookmaker="matchbook", odds=1.43, recommended_primary=False)]
    opps = aggregate_opportunities(pd.DataFrame(rows))
    assert len(opps) == 1 and opps[0].recommended_primary
    out = render_daily(_res(rows))
    assert out.count("⭐") == 1                          # una estrella, no una por casa


# --------------------------------------------------------------------------
# 8. --watch deduplica
# --------------------------------------------------------------------------

def test_watch_flag_shows_deduped_watchlist():
    rows = ([_row(match="Osaka N. vs Rival X.", selection_name="Osaka N.",
                  event_id="ev-osaka", bookmaker=f"b{i}", odds=1.45 - i * 0.01,
                  o_min=1.46, state="vigilar_precio", ev_cons=None)
             for i in range(6)]
            + [_row(match="Otro P. vs Q Q.", selection_name="Otro P.",
                    event_id="ev-otro", bookmaker="b1", odds=1.80, o_min=2.00,
                    state="vigilar_precio", ev_cons=None)])
    res = _res(rows)
    assert len(res.watchlist) == 2
    assert res.watchlist_near == 1                       # Osaka a <=1% de 1.46
    teaser = render_daily(res, show_watch=False)
    assert "WATCHLIST: 2 oportunidades únicas" in teaser
    assert "1 a ≤1% de la cuota mínima" in teaser
    assert "betbot scan --watch" in teaser
    assert "Osaka N. ML" not in teaser                   # el detalle no va por defecto
    full = render_daily(res, show_watch=True)
    assert full.count("Osaka N. ML vs Rival X.") == 1    # deduplicada
    assert "mejor 1.45 @ b0" in full and "necesita ≥ 1.46" in full


# --------------------------------------------------------------------------
# 9. --verbose conserva el diagnóstico
# --------------------------------------------------------------------------

def test_verbose_keeps_full_diagnostics():
    from betbot.scan import render_report
    rows = [_row(bookmaker="betfair"), _row(bookmaker="matchbook", odds=1.43),
            _row(match="X X. vs Y Y.", event_id="e9", state="sin_value",
                 ev_cons=-0.02)]
    res = _res(rows)
    compact = render_daily(res)
    verbose = render_report(res)
    # el compacto NO enumera sin_value ni cada bookmaker; el verboso SÍ
    assert "sin value 1" in compact and "X X. vs Y Y." not in compact
    assert "matchbook" in verbose and "betfair" in verbose
    assert "SIN VALUE" in verbose


def test_default_output_hides_noise_keeps_summary():
    rows = [_row(state="probable_sin_value", ev_cons=0.0),
            _row(match="Z Z. vs W W.", event_id="ez", state="descartada",
                 reasons="jugador_desconocido:Z Z.")]
    out = render_daily(_res(rows, orphan_quotes=71))
    assert "prob. sin value 1" in out and "descartadas 1" in out
    assert "orphan_quotes 71" in out
    assert "Z Z." not in out                             # sin bloques de ruido
    assert "--verbose" in out


# --------------------------------------------------------------------------
# 10. hora placeholder 03:59 jamás en TOP PICKS (extremo a extremo)
# --------------------------------------------------------------------------

@needs_bundle
def test_placeholder_0359_never_reaches_top_picks(tmp_path):
    from betbot.feeds.base import FeedMatch, SourceStatus
    from betbot.scan import run_scan
    from betbot.schemas import OddsQuote
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    cfg.setdefault("feeds", {})["commence_crosscheck"] = False
    now = datetime.now(timezone.utc)
    start = (now + timedelta(days=1)).replace(hour=3, minute=59, second=0, microsecond=0)

    class Cal:
        name = "cal_placeholder"
        authoritative = True

        def fetch_matches(self, window_hours):
            ms = [FeedMatch(date=start.date(), tour="WTA", tournament="Cincinnati",
                            player1="McNally C.", player2="Eala A.",
                            scheduled_at_utc=start, status="scheduled",
                            source=self.name, source_updated_at=now.isoformat(),
                            authoritative=True)]     # SIN marcar: time_precision="exact"
            return ms, SourceStatus(name=self.name, ok=True, authoritative=True,
                                    n_items=1)

    class Odds:
        name = "odds_fake"

        def fetch_odds(self, matches):
            qs = [OddsQuote(player_a="McNally C.", player_b="Eala A.",
                            market="match_winner", selection="b", odds=1.44,
                            bookmaker="betfair_ex_eu", timestamp=now.isoformat()),
                  OddsQuote(player_a="McNally C.", player_b="Eala A.",
                            market="match_winner", selection="a", odds=2.9,
                            bookmaker="betfair_ex_eu", timestamp=now.isoformat())]
            return qs, SourceStatus(name=self.name, ok=True, n_items=len(qs))

    res = run_scan(cfg, calendar_sources=[Cal()], odds_sources=[Odds()],
                   structured_providers=[], log_ledger=False, show_rejected=True)
    # la puerta lo excluye aunque el adaptador jurase precisión exacta
    assert res.summary["calendar_confirmed"] == 0
    assert res.summary["prematch_excluded"].get("hora_provisional") == 1
    assert res.top_picks == [] and len(res.rows) == 0
    out = render_daily(res)
    assert "Inicio confirmado" not in out
    from betbot.scan import render_report
    assert "Inicio confirmado" not in render_report(res)


# --------------------------------------------------------------------------
# banner de degradación
# --------------------------------------------------------------------------

def test_degraded_banner_thresholds():
    ok = {"date": "2026-08-07",
          "results_freshness": {"ATP": "2026-08-06", "WTA": "2026-08-07"}}
    assert degraded_banner(ok) is None                   # 1 día no es degradación
    stale = {"date": "2026-08-07",
             "results_freshness": {"ATP": "2026-08-04", "WTA": "2026-08-04"}}
    b = degraded_banner(stale)
    assert b and "MODO DEGRADADO" in b and "3d" in b
    assert "already_completed" in b and "OOD" in b       # qué depende de la frescura
    res = _res([_row()], results_freshness={"ATP": "2026-08-04", "WTA": "2026-08-04"})
    assert "MODO DEGRADADO" in render_daily(res)
