"""`betbot scan`: descubrimiento automático de jornada + cuotas + análisis.

Orquesta: fuentes de calendario -> elegibilidad (main tour, individuales, sin
qualies/equipos) -> resolución de identidades -> fuentes de cuotas -> screener
existente (modelos congelados) -> señales por estado + cobertura honesta.
La entrada manual (CSV) queda como fallback; este es el camino normal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from betbot.feeds.base import CalendarSource, FeedMatch, OddsSource, SourceStatus, dedupe_matches
from betbot.schemas import DayMatch, OddsQuote
from betbot.screener.run import screen_day

DISPLAY_DEFAULT = ("fuerte", "normal", "experimental", "vigilar_precio")
STATE_RANK = {"fuerte": 0, "normal": 1, "experimental": 2, "vigilar_precio": 3,
              "probable_sin_value": 4, "sin_value": 5, "descartada": 6}
SUPPORTED_MARKETS = ("match_winner", "set1_winner", "wins_set", "straight_sets", "three_sets")

# calendario de superficies (estimación con aviso cuando la fuente no la da)
_SURFACE_OVERRIDES = {
    "roland garros": "Clay", "french open": "Clay", "wimbledon": "Grass",
    "halle": "Grass", "queens": "Grass", "eastbourne": "Grass", "stuttgart wta": "Grass",
    "monte carlo": "Clay", "madrid": "Clay", "rome": "Clay", "roma": "Clay",
    "barcelona": "Clay", "hamburg": "Clay", "gstaad": "Clay", "kitzbuhel": "Clay",
    "bastad": "Clay", "umag": "Clay", "bucharest": "Clay",
}


def guess_surface(tournament: str, d: date) -> tuple[str, bool]:
    """(superficie, es_estimada). Regla estacional + overrides de torneos."""
    t = tournament.lower()
    for k, s in _SURFACE_OVERRIDES.items():
        if k in t:
            return s, False
    if d.month in (4, 5):
        return "Clay", True
    if (d.month == 6 and d.day > 8) or (d.month == 7 and d.day <= 14):
        return "Grass", True
    return "Hard", True


def default_sources(cfg: dict) -> tuple[list[CalendarSource], list[OddsSource]]:
    from betbot.feeds.espn import EspnFeed
    from betbot.feeds.github_te import GithubTEFeed
    from betbot.feeds.oddsapi import OddsApiFeed
    fcfg = cfg.get("feeds", {})
    ttl = int(fcfg.get("ttl_seconds", 900))
    te = GithubTEFeed(ttl_seconds=ttl)
    es = EspnFeed(ttl_seconds=ttl)
    oa = OddsApiFeed(api_key=fcfg.get("odds_api_key"), ttl_seconds=ttl)
    cal_map = {"github_te": te, "espn": es}
    odds_map = {"github_te": te, "espn": es, "oddsapi": oa}
    cals = [cal_map[n] for n in fcfg.get("calendar_order", ["github_te", "espn"]) if n in cal_map]
    odds = [odds_map[n] for n in fcfg.get("odds_order", ["github_te", "espn", "oddsapi"])
            if n in odds_map]
    return cals, odds


@dataclass
class ScanResult:
    summary: dict
    rows: pd.DataFrame
    displayed: pd.DataFrame
    statuses: list[SourceStatus] = field(default_factory=list)


def run_scan(cfg: dict, hours: int = 48, tours: list[str] | None = None,
             markets: list[str] | None = None, min_odds: float | None = None,
             max_odds: float | None = None, show_likely: bool = False,
             show_rejected: bool = False, export: str | None = None,
             calendar_sources: list[CalendarSource] | None = None,
             odds_sources: list[OddsSource] | None = None,
             log_ledger: bool = True) -> ScanResult:
    if calendar_sources is None or odds_sources is None:
        d_cal, d_odds = default_sources(cfg)
        calendar_sources = calendar_sources or d_cal
        odds_sources = odds_sources or d_odds
    statuses: list[SourceStatus] = []

    # ---------- 1. calendario (tolerante a fallos por fuente) ----------
    found: list[FeedMatch] = []
    for src in calendar_sources:
        try:
            ms, st = src.fetch_matches(hours)
        except Exception as exc:  # noqa: BLE001 - una fuente caída no detiene el escaneo
            ms, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        statuses.append(st)
        found.extend(ms)
    found = dedupe_matches(found)

    # ---------- 2. elegibilidad ----------
    excluded = {"doubles": 0, "qualifying": 0, "challenger": 0, "itf": 0, "other": 0, "tour": 0}
    eligible: list[FeedMatch] = []
    for m in found:
        if m.is_doubles:
            excluded["doubles"] += 1
        elif m.is_qualifying:
            excluded["qualifying"] += 1
        elif m.level != "main":
            excluded[m.level if m.level in excluded else "other"] += 1
        elif tours and m.tour not in tours:
            excluded["tour"] += 1
        else:
            eligible.append(m)

    # ---------- 3. cuotas (todas las fuentes; se guardan todas) ----------
    quotes: list[OddsQuote] = []
    for src in odds_sources:
        try:
            qs, st = src.fetch_odds(eligible)
        except Exception as exc:  # noqa: BLE001
            qs, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        statuses.append(st)
        quotes.extend(qs)

    # ---------- 4. screener (modelos congelados; ledger completo) ----------
    day_matches: list[DayMatch] = []
    est_surface_keys: set[tuple] = set()
    for m in eligible:
        surface, estimated = (m.surface, False) if m.surface else guess_surface(m.tournament, m.date)
        if estimated:
            est_surface_keys.add(m.key)
        day_matches.append(DayMatch(date=m.date, tour=m.tour, tournament=m.tournament,
                                    surface=surface, indoor=m.indoor, round=m.round,
                                    best_of=m.best_of, player_a=m.player1, player_b=m.player2))
    out, warns = (pd.DataFrame(), [])
    if day_matches:
        out, warns = screen_day(cfg, day_matches, quotes, log_ledger=log_ledger)

    # ---------- 5. post-proceso ----------
    if len(out):
        out["odds_age_min"] = _odds_age_minutes(out, quotes)
        out["best_odds"] = _mark_best_odds(out)
        # aviso de superficie estimada como reason
        est_matches = {f"{m.player1} vs {m.player2}" for m in eligible if m.key in est_surface_keys}
        mask = out["match"].isin(est_matches) & (out["state"] != "descartada")
        out.loc[mask, "reasons"] = out.loc[mask, "reasons"].map(
            lambda r: (r + ";" if r else "") + "superficie_estimada")

    display_states = set(DISPLAY_DEFAULT)
    if show_likely:
        display_states.add("probable_sin_value")
    if show_rejected:
        display_states.update({"descartada", "sin_value"})
    disp = out.copy()
    if len(disp):
        disp = disp[disp["state"].isin(display_states)]
        if markets:
            disp = disp[disp["market"].isin(markets)]
        if min_odds is not None:
            disp = disp[disp["odds"].isna() | (disp["odds"] >= min_odds)]
        if max_odds is not None:
            disp = disp[disp["odds"].isna() | (disp["odds"] <= max_odds)]
        disp["_rank"] = disp["state"].map(STATE_RANK).fillna(9)
        disp = disp.sort_values(["_rank", "ev_cons", "odds_age_min"],
                                ascending=[True, False, True]).drop(columns="_rank")

    # ---------- 6. cobertura honesta ----------
    quoted_matches = {(q.player_a, q.player_b) for q in quotes}
    n_with_odds = sum(1 for m in eligible if (m.player1, m.player2) in quoted_matches
                      or (m.player2, m.player1) in quoted_matches)
    unresolved = 0
    if len(out):
        unresolved = out[(out["state"] == "descartada")
                         & out["reasons"].str.contains("jugador_desconocido", na=False)][
            "match"].nunique()
    markets_available = sorted({q.market for q in quotes})
    state_counts = out["state"].value_counts().to_dict() if len(out) else {}
    summary = {
        "date": str(date.today()),
        "window_hours": hours,
        "found": len(found), "eligible": len(eligible),
        "excluded": excluded,
        "analyzed_matches": int(out["match"].nunique()) if len(out) else 0,
        "unresolved_players": int(unresolved),
        "matches_with_odds": n_with_odds,
        "odds_coverage_pct": round(100.0 * n_with_odds / len(eligible), 1) if eligible else 0.0,
        "markets_evaluated": int(len(out)),
        "markets_with_quotes": markets_available,
        "markets_missing_from_sources": [m for m in SUPPORTED_MARKETS if m not in markets_available],
        "states": state_counts,
        "sources": [{"name": s.name, "ok": s.ok, "n": s.n_items, "error": s.error,
                     "data_timestamp": s.data_timestamp, "notes": s.notes} for s in statuses],
        "warnings": warns,
    }
    if export and len(disp):
        disp.to_csv(export, index=False)
        summary["export"] = export
    return ScanResult(summary=summary, rows=out, displayed=disp, statuses=statuses)


def _odds_age_minutes(out: pd.DataFrame, quotes: list[OddsQuote]) -> pd.Series:
    now = datetime.now(timezone.utc)
    ts_by_book: dict[str, float] = {}
    for q in quotes:
        try:
            ts = pd.Timestamp(q.timestamp)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts_by_book[q.bookmaker] = (now - ts.to_pydatetime()).total_seconds() / 60.0
        except (ValueError, TypeError):
            continue
    return out["bookmaker"].map(lambda b: round(ts_by_book.get(b, float("nan")), 1))


def _mark_best_odds(out: pd.DataFrame) -> pd.Series:
    """True en la fila con MEJOR cuota disponible por (partido, mercado, selección)."""
    best = pd.Series(False, index=out.index)
    with_odds = out[out["odds"].notna()]
    for _, grp in with_odds.groupby(["match", "market", "selection"]):
        best.loc[grp["odds"].idxmax()] = True
    return best


# ---------------- salida de terminal ----------------

def render_report(res: ScanResult, show_likely: bool = False) -> str:
    s = res.summary
    lines: list[str] = []
    lines.append(f"BETBOT — {s['date']}  (ventana {s['window_hours']}h)")
    lines.append(f"Partidos encontrados: {s['found']}  ·  elegibles main tour: {s['eligible']} "
                 f"(excluidos: dobles {s['excluded']['doubles']}, challenger {s['excluded']['challenger']}, "
                 f"itf {s['excluded']['itf']}, otros {s['excluded']['other'] + s['excluded']['qualifying']})")
    lines.append(f"Partidos analizados: {s['analyzed_matches']}  ·  no enlazados: {s['unresolved_players']}")
    lines.append(f"Cobertura de cuotas: {s['matches_with_odds']}/{s['eligible']} "
                 f"({s['odds_coverage_pct']}%)  ·  mercados con cuota: "
                 f"{', '.join(s['markets_with_quotes']) or 'ninguno'}")
    if s["markets_missing_from_sources"]:
        lines.append(f"Mercados SIN cuota en las fuentes activas: "
                     f"{', '.join(s['markets_missing_from_sources'])}")
    lines.append(f"Mercados evaluados: {s['markets_evaluated']}")
    st = s["states"]
    lines.append(f"Fuertes: {st.get('fuerte', 0)}  Normales: {st.get('normal', 0)}  "
                 f"Experimentales: {st.get('experimental', 0)}  Vigilar: {st.get('vigilar_precio', 0)}  "
                 f"Probables sin value: {st.get('probable_sin_value', 0)}  "
                 f"Sin value: {st.get('sin_value', 0)}  Descartados: {st.get('descartada', 0)}")
    for src in s["sources"]:
        flag = "OK " if src["ok"] else "FALLO"
        extra = f" · datos de {src['data_timestamp'][:16]}" if src["data_timestamp"] else ""
        err = f" · {src['error']}" if src["error"] else ""
        lines.append(f"  fuente {src['name']}: {flag} ({src['n']} items){extra}{err}")
        for n in src["notes"]:
            lines.append(f"    aviso: {n}")

    if res.displayed is None or len(res.displayed) == 0:
        lines.append("\nSin señales que mostrar (la abstención es un resultado normal).")
        return "\n".join(lines)

    order = ["fuerte", "normal", "experimental", "vigilar_precio", "probable_sin_value",
             "sin_value", "descartada"]
    for state in order:
        block = res.displayed[res.displayed["state"] == state]
        if not len(block):
            continue
        lines.append(f"\n===== {state.upper().replace('_', ' ')} ({len(block)}) =====")
        for _, r in block.iterrows():
            lines.append(f"\n{r['match']}   [{r['tour']} · {r.get('tournament', '')} · {r.get('surface', '')}]")
            sel = r["selection_name"]
            mk = r["market"]
            lines.append(f"  Mercado: {mk} — {sel}")
            if pd.notna(r.get("odds")):
                age = r.get("odds_age_min")
                age_s = f" · hace {age:.0f} min" if pd.notna(age) else ""
                best = "  ← mejor cuota" if r.get("best_odds") else ""
                lines.append(f"  Cuota: {r['odds']:.2f} — {r['bookmaker']}{age_s}{best}")
                lines.append(f"  Break-even: {100.0 / r['odds']:.1f}%")
            p_model = r.get("p_model")
            if pd.notna(p_model):
                lines.append(f"  Probabilidad modelo: {100 * p_model:.1f}%"
                             + (f" · no-vig: {100 * r['p_novig']:.1f}%" if pd.notna(r.get("p_novig")) else "")
                             + (f" · edge: {100 * r['edge']:+.1f} pp" if pd.notna(r.get("edge")) else ""))
                lines.append(f"  Cuota justa: {r['fair_odds']:.2f} · Cuota mínima: {r['o_min']:.2f}")
            if pd.notna(r.get("ev_cons")):
                lines.append(f"  EV conservador: {100 * r['ev_cons']:+.1f}%"
                             + (f" · EV central: {100 * r['ev']:+.1f}%" if pd.notna(r.get("ev")) else ""))
            if r.get("recommended_primary"):
                lines.append("  ⭐ recommended_primary")
            if r.get("reasons"):
                lines.append(f"  Avisos: {r['reasons']}")
    return "\n".join(lines)
