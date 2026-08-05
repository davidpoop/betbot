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
    """Calendario: SOLO fuentes autoritativas (estado + hora + id de evento).
    Cuotas: cualquier fuente, pero sus precios se atan a eventos confirmados."""
    from betbot.feeds.espn import EspnFeed
    from betbot.feeds.github_te import GithubTEFeed
    from betbot.feeds.oddsapi import OddsApiFeed
    from betbot.feeds.results import SportradarResults
    fcfg = cfg.get("feeds", {})
    ttl = int(fcfg.get("ttl_seconds", 900))
    te = GithubTEFeed(ttl_seconds=ttl)
    es = EspnFeed(ttl_seconds=ttl)
    oa = OddsApiFeed(api_key=fcfg.get("odds_api_key"), ttl_seconds=ttl)
    cal_map = {"espn": es, "sportradar": SportradarResults(ttl_seconds=ttl)}
    odds_map = {"github_te": te, "espn": es, "oddsapi": oa}
    cals = [cal_map[n] for n in fcfg.get("calendar_order", ["espn"]) if n in cal_map]
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
             structured_providers: list | None = None,
             log_ledger: bool = True, sync_results: bool = False) -> ScanResult:
    if calendar_sources is None or odds_sources is None:
        d_cal, d_odds = default_sources(cfg)
        calendar_sources = calendar_sources or d_cal
        odds_sources = odds_sources or d_odds
    statuses: list[SourceStatus] = []

    # ---------- 0. sync de resultados (por defecto en CLI; --no-sync-results lo salta) ----------
    sync_report: dict | None = None
    if sync_results:
        try:
            from betbot.sync import run_sync
            full = run_sync(cfg)
            sync_report = {k: full.get(k) for k in
                           ("n_accepted", "n_duplicates_skipped", "n_quarantined",
                            "sources", "window")}
        except Exception as exc:  # noqa: BLE001 - el sync nunca detiene el scan
            sync_report = {"error": str(exc)}
    try:
        from betbot.sync import local_freshness
        results_freshness = {k: str(v) for k, v in local_freshness(cfg).items()}
    except Exception:  # noqa: BLE001
        results_freshness = {}

    # ---------- 1. calendario: SOLO fuentes autoritativas crean eventos ----------
    now = datetime.now(timezone.utc)
    found: list[FeedMatch] = []
    n_authoritative_ok = 0
    for src in calendar_sources:
        auth = bool(getattr(src, "authoritative", False))
        try:
            ms, st = src.fetch_matches(hours)
        except Exception as exc:  # noqa: BLE001 - una fuente caída no detiene el escaneo
            ms, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        st.authoritative = auth
        statuses.append(st)
        if not auth:
            # una fuente sin estado ni hora NO puede aportar eventos al calendario
            st.notes.append("descartada como calendario: no es autoritativa")
            continue
        if st.ok:
            n_authoritative_ok += 1
        found.extend(ms)
    found = dedupe_matches(found)

    # FAIL CLOSED: sin ninguna fuente de calendario autoritativa operativa no se
    # analiza nada. Cero señales es preferible a recomendar un partido terminado.
    if n_authoritative_ok == 0:
        return _fail_closed_result(cfg, hours, statuses, results_freshness, sync_report)

    # ---------- 2. puerta PREPARTIDO (estado + hora + resultados locales) ----------
    from betbot.prematch import gate
    registry = _player_registry(cfg)
    gres = gate(found, cfg, window_hours=hours, now=now, tours=tours, registry=registry)
    eligible = gres.eligible
    gate_counts = gres.counts()
    excluded = {
        "doubles": sum(1 for m in gres.excluded.get("no_main_tour", []) if m.is_doubles),
        "qualifying": sum(1 for m in gres.excluded.get("no_main_tour", []) if m.is_qualifying),
        "challenger": sum(1 for m in gres.excluded.get("no_main_tour", []) if m.level == "challenger"),
        "itf": sum(1 for m in gres.excluded.get("no_main_tour", []) if m.level == "itf"),
        "other": sum(1 for m in gres.excluded.get("no_main_tour", [])
                     if m.level == "other" and not m.is_doubles and not m.is_qualifying),
        "tour": sum(1 for m in gres.excluded.get("no_main_tour", [])
                    if tours and m.tour not in tours),
    }

    # ---------- 3. cuotas: solo se asocian a eventos CONFIRMADOS ----------
    quotes: list[OddsQuote] = []
    n_orphan_quotes = 0
    for src in odds_sources:
        try:
            qs, st = src.fetch_odds(eligible)
        except Exception as exc:  # noqa: BLE001
            qs, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        statuses.append(st)
        n_orphan_quotes += int(getattr(st, "n_orphan", 0) or 0)
        # segunda barrera: aunque una fuente no filtre, aquí solo entran cuotas
        # cuyo enfrentamiento está confirmado por el calendario
        confirmed_pairs = {m.pair_key[1:] for m in eligible}
        for q in qs:
            probe = FeedMatch(date=now.date(), tour="ATP", tournament="",
                              player1=q.player_a, player2=q.player_b)
            if probe.pair_key[1:] in confirmed_pairs:
                quotes.append(q)
            else:
                n_orphan_quotes += 1

    # ---------- 3b. proveedores estructurados (mercados de sets, solo lectura) ----------
    from betbot.feeds.structured_odds import QUOTABLE, to_odds_quotes
    if structured_providers is None:
        from betbot.feeds.manage import default_structured_providers
        structured_providers = default_structured_providers(cfg)
    struct_prices = []
    structured_info: dict = {"active": False, "providers": [], "events": []}
    for prov in structured_providers:
        if not prov.active():
            statuses.append(SourceStatus(
                name=getattr(prov, "name", "?"), ok=False,
                error="inactivo: faltan credenciales (ver betbot feeds status)"))
            continue
        structured_info["active"] = True
        try:
            prices, st = prov.fetch_prices(eligible)
        except Exception as exc:  # noqa: BLE001
            prices, st = [], SourceStatus(name=getattr(prov, "name", "?"),
                                          ok=False, error=str(exc))
        statuses.append(st)
        struct_prices.extend(prices)
        structured_info["providers"].append(st.name)
    if struct_prices:
        for row in to_odds_quotes(struct_prices):
            quotes.append(OddsQuote(**row))
        by_ev: dict[tuple, dict] = {}
        for p in struct_prices:
            d = by_ev.setdefault((p.player_a, p.player_b),
                                 {"offered": set(), "suspended": set(), "in_play": False})
            if p.market_canonical in QUOTABLE:
                d["offered"].add(p.market_canonical)
                if p.status == "suspended":
                    d["suspended"].add(p.market_canonical)
            d["in_play"] = d["in_play"] or p.in_play
        structured_info["events"] = [
            {"match": f"{k[0]} vs {k[1]}", "offered": sorted(v["offered"]),
             "not_offered": [m for m in QUOTABLE if m not in v["offered"]],
             "suspended": sorted(v["suspended"]), "in_play": v["in_play"]}
            for k, v in by_ev.items()]
        if log_ledger:
            _log_structured_prices(cfg, struct_prices)

    # ---------- 4. screener (modelos congelados; ledger completo) ----------
    day_matches: list[DayMatch] = []
    est_surface_keys: set[tuple] = set()
    for m in eligible:
        # la fecha de análisis SIEMPRE es la del inicio confirmado en UTC
        mdate = m.scheduled_at_utc.date() if m.scheduled_at_utc else m.date
        surface, estimated = (m.surface, False) if m.surface else guess_surface(m.tournament, mdate)
        if estimated:
            est_surface_keys.add(m.pair_key)
        day_matches.append(DayMatch(date=mdate, tour=m.tour, tournament=m.tournament,
                                    surface=surface, indoor=m.indoor, round=m.round,
                                    best_of=m.best_of, player_a=m.player1, player_b=m.player2))
    out, warns = (pd.DataFrame(), [])
    if day_matches:
        out, warns = screen_day(cfg, day_matches, quotes, log_ledger=log_ledger)

    # ---------- 5. post-proceso ----------
    if len(out):
        out["odds_age_min"] = _odds_age_minutes(out, quotes)
        out["best_odds"] = _mark_best_odds(out)
        # trazabilidad prepartido por señal (inicio, estado, fuente, actualización)
        conf_by_label = {m.label: gres.confirmations.get(m.pair_key, {}) for m in eligible}
        for col, key in (("start_utc", "start_utc"), ("event_status", "status"),
                         ("calendar_source", "source"),
                         ("calendar_updated_at", "source_updated_at"),
                         ("calendar_status_age_h", "status_age_h"),
                         ("event_id", "event_id")):
            out[col] = out["match"].map(lambda lbl, k=key: conf_by_label.get(lbl, {}).get(k))
        # aviso de superficie estimada como reason
        est_matches = {m.label for m in eligible if m.pair_key in est_surface_keys}
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

    # ---------- 6. cobertura honesta (por mercado, nunca solo moneyline) ----------
    quoted_matches = {(q.player_a, q.player_b) for q in quotes}
    n_with_odds = sum(1 for m in eligible if (m.player1, m.player2) in quoted_matches
                      or (m.player2, m.player1) in quoted_matches)
    all_markets = list(SUPPORTED_MARKETS) + sorted(
        {q.market for q in quotes} - set(SUPPORTED_MARKETS))
    market_coverage: dict[str, int] = {}
    for mk in all_markets:
        keys = {frozenset((q.player_a, q.player_b)) for q in quotes if q.market == mk}
        market_coverage[mk] = sum(1 for m in eligible
                                  if frozenset((m.player1, m.player2)) in keys)
    unresolved = 0
    if len(out):
        unresolved = out[(out["state"] == "descartada")
                         & out["reasons"].str.contains("jugador_desconocido", na=False)][
            "match"].nunique()
    markets_available = sorted({q.market for q in quotes})
    state_counts = out["state"].value_counts().to_dict() if len(out) else {}
    n_stale = 0
    if len(out):
        n_stale = int(out["reasons"].str.contains("cuota_caducada", na=False).sum())
    summary = {
        "date": str(date.today()),
        "window_hours": hours,
        "found": len(found), "eligible": len(eligible),
        "excluded": excluded,
        "analyzed_matches": int(out["match"].nunique()) if len(out) else 0,
        "unresolved_players": int(unresolved),
        "matches_with_odds": n_with_odds,
        "odds_coverage_pct": round(100.0 * n_with_odds / len(eligible), 1) if eligible else 0.0,
        "market_coverage": market_coverage,
        "markets_evaluated": int(len(out)),
        "markets_with_quotes": markets_available,
        "markets_missing_from_sources": [m for m in SUPPORTED_MARKETS if m not in markets_available],
        "stale_discarded": n_stale,
        "states": state_counts,
        "results_freshness": results_freshness,
        "sync": sync_report,
        "structured": structured_info,
        # ---- trazabilidad prepartido (fallo Tsitsipas–Fonseca) ----
        "fail_closed": False,
        "calendar_confirmed": len(eligible),
        "calendar_sources_ok": n_authoritative_ok,
        "prematch_excluded": gate_counts,
        "prematch_excluded_samples": {
            r: [f"{m.label} [{m.tour} · {m.tournament}"
                + (f" · {m.status}" if m.status != "unknown" else " · sin estado")
                + (f" · inicio {m.scheduled_at_utc.isoformat()}" if m.scheduled_at_utc else "")
                + f" · fuente {m.source}]" for m in ms[:5]]
            for r, ms in gres.excluded.items() if ms and r != "no_main_tour"},
        "orphan_quotes": n_orphan_quotes,
        "confirmations": {f"{k[1]}__{k[2]}": v for k, v in gres.confirmations.items()},
        "sources": [{"name": s.name, "ok": s.ok, "n": s.n_items, "error": s.error,
                     "data_timestamp": s.data_timestamp, "notes": s.notes,
                     "authoritative": s.authoritative, "orphan": s.n_orphan}
                    for s in statuses],
        "warnings": warns,
    }
    if export and len(disp):
        disp.to_csv(export, index=False)
        summary["export"] = export
    return ScanResult(summary=summary, rows=out, displayed=disp, statuses=statuses)


def _player_registry(cfg: dict) -> set:
    """Registro de jugadores para resolver iniciales de forma determinista."""
    try:
        import pandas as _pd

        from betbot.config import resolve_path
        p = resolve_path(cfg, "canonical_dir") / "players.parquet"
        return set(_pd.read_parquet(p)["player_id"]) if p.exists() else set()
    except Exception:  # noqa: BLE001
        return set()


def _fail_closed_result(cfg: dict, hours: int, statuses: list[SourceStatus],
                        results_freshness: dict, sync_report: dict | None) -> ScanResult:
    """Sin calendario autoritativo operativo: cero señales, con motivo explícito."""
    from betbot.prematch import FAIL_CLOSED_MSG
    summary = {
        "date": str(date.today()), "window_hours": hours,
        "fail_closed": True, "fail_closed_message": FAIL_CLOSED_MSG,
        "found": 0, "eligible": 0, "calendar_confirmed": 0, "calendar_sources_ok": 0,
        "excluded": {"doubles": 0, "qualifying": 0, "challenger": 0, "itf": 0,
                     "other": 0, "tour": 0},
        "prematch_excluded": {}, "prematch_excluded_samples": {}, "orphan_quotes": 0,
        "confirmations": {}, "analyzed_matches": 0, "unresolved_players": 0,
        "matches_with_odds": 0, "odds_coverage_pct": 0.0, "market_coverage": {},
        "markets_evaluated": 0, "markets_with_quotes": [],
        "markets_missing_from_sources": list(SUPPORTED_MARKETS),
        "stale_discarded": 0, "states": {},
        "results_freshness": results_freshness, "sync": sync_report,
        "structured": {"active": False, "providers": [], "events": []},
        "sources": [{"name": s.name, "ok": s.ok, "n": s.n_items, "error": s.error,
                     "data_timestamp": s.data_timestamp, "notes": s.notes,
                     "authoritative": s.authoritative} for s in statuses],
        "warnings": [FAIL_CLOSED_MSG],
    }
    return ScanResult(summary=summary, rows=pd.DataFrame(), displayed=pd.DataFrame(),
                      statuses=statuses)


def _log_structured_prices(cfg: dict, prices: list) -> None:
    """Metadatos completos de cada precio estructurado al ledger (append-only)."""
    import json
    from dataclasses import asdict

    from betbot.config import resolve_path
    ldir = resolve_path(cfg, "ledger_dir")
    ldir.mkdir(parents=True, exist_ok=True)
    with open(ldir / "structured_prices.jsonl", "a", encoding="utf-8") as fh:
        for p in prices:
            fh.write(json.dumps(asdict(p), ensure_ascii=False, default=str) + "\n")


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

_MARKET_LABELS = {"match_winner": "Moneyline", "set1_winner": "Primer set",
                  "wins_set": "Gana un set (+1.5)", "straight_sets": "2-0 (sets corridos)",
                  "three_sets": "Total sets (O/U 2.5)", "set_score": "Marcador exacto de sets"}


_EXCL_LABELS = {
    "fuente_no_autoritativa": "fuente sin estado verificable",
    "sin_hora_de_inicio": "sin hora de inicio",
    "estado_desconocido": "estado no verificable",
    "estado_no_prepartido": "no prepartido (live/terminado/cancelado)",
    "estado_calendario_caducado": "estado de calendario caducado",
    "ya_comenzado": "ya comenzado",
    "fuera_de_ventana": "fuera de la ventana",
    "already_completed": "ya completados (resultado local)",
    "no_main_tour": "fuera de main tour",
}


def render_report(res: ScanResult, show_likely: bool = False) -> str:
    s = res.summary
    lines: list[str] = []
    lines.append(f"BETBOT — {s['date']}  (ventana {s['window_hours']}h)")
    if s.get("fail_closed"):
        lines.append("")
        lines.append(s.get("fail_closed_message", ""))
        lines.append("Ninguna fuente de calendario autoritativa respondió: no se analiza "
                     "ningún partido (cero señales es preferible a recomendar un "
                     "partido ya jugado).")
        for src in s["sources"]:
            flag = "OK " if src["ok"] else "FALLO"
            auth = " [autoritativa]" if src.get("authoritative") else ""
            err = f" · {src['error']}" if src["error"] else ""
            lines.append(f"  fuente {src['name']}{auth}: {flag}{err}")
        return "\n".join(lines)
    # frescura de resultados por circuito, SIEMPRE antes del resto del informe
    fresh = s.get("results_freshness") or {}
    if fresh:
        parts = []
        for t in ("ATP", "WTA"):
            if t in fresh:
                try:
                    age = (date.fromisoformat(s["date"]) - date.fromisoformat(fresh[t])).days
                except ValueError:
                    age = 0
                warn = f" ⚠ {age}d de retraso" if age > 2 else ""
                parts.append(f"{t} hasta {fresh[t]}{warn}")
            else:
                parts.append(f"{t}: sin datos")
        lines.append("Frescura resultados: " + "  ·  ".join(parts))
    sync = s.get("sync")
    if sync is not None:
        if sync.get("error"):
            lines.append(f"Sync resultados: FALLO ({sync['error']}) — se mantiene el último estado válido")
        else:
            srcs = ", ".join(f"{x['name']}:{'OK' if x['ok'] else 'FALLO'}"
                             for x in sync.get("sources", []))
            lines.append(f"Sync resultados: +{sync.get('n_accepted', 0)} nuevos, "
                         f"{sync.get('n_duplicates_skipped', 0)} duplicados omitidos, "
                         f"{sync.get('n_quarantined', 0)} en cuarentena  [{srcs}]")
    lines.append(f"Partidos en el calendario: {s['found']}  ·  "
                 f"CONFIRMADOS prepartido: {s.get('calendar_confirmed', s['eligible'])} "
                 f"(excluidos: dobles {s['excluded']['doubles']}, challenger {s['excluded']['challenger']}, "
                 f"itf {s['excluded']['itf']}, otros {s['excluded']['other'] + s['excluded']['qualifying']})")
    excl = s.get("prematch_excluded") or {}
    samples = s.get("prematch_excluded_samples") or {}
    for reason in ("estado_no_prepartido", "already_completed", "ya_comenzado",
                   "estado_desconocido", "sin_hora_de_inicio", "fuente_no_autoritativa",
                   "estado_calendario_caducado", "fuera_de_ventana"):
        n = excl.get(reason, 0)
        if not n:
            continue
        lines.append(f"  excluidos — {_EXCL_LABELS.get(reason, reason)}: {n}")
        for ex in (samples.get(reason) or [])[:3]:
            lines.append(f"      · {ex}")
    if s.get("orphan_quotes"):
        lines.append(f"  cuotas sin evento de calendario confirmado (orphan_quote): "
                     f"{s['orphan_quotes']} — no se analizan")
    lines.append(f"Partidos analizados: {s['analyzed_matches']}  ·  no enlazados: {s['unresolved_players']}")
    # cobertura POR MERCADO: nunca un "100%" que sea solo moneyline
    cov = s.get("market_coverage") or {}
    if cov:
        lines.append("Cobertura de cuotas por mercado: "
                     + ";  ".join(f"{_MARKET_LABELS.get(m, m)}: {n}/{s['eligible']}"
                                  for m, n in cov.items()))
    if s["markets_missing_from_sources"]:
        lines.append(f"Mercados SIN cuota en las fuentes activas (not_offered): "
                     f"{', '.join(_MARKET_LABELS.get(m, m) for m in s['markets_missing_from_sources'])}")
    stale = s.get("stale_discarded", 0)
    lines.append(f"Mercados evaluados: {s['markets_evaluated']}"
                 + (f"  ·  descartados por cuota caducada: {stale}" if stale else ""))
    struct = s.get("structured") or {}
    if struct.get("active"):
        evs = struct.get("events", [])
        lines.append(f"Fuente estructurada ({', '.join(struct.get('providers', []) or ['-'])}): "
                     f"{len(evs)} eventos enlazados")
        miss: dict[str, int] = {}
        for e in evs:
            for m in e["not_offered"]:
                miss[m] = miss.get(m, 0) + 1
        if miss and evs:
            lines.append("  not_offered: " + ";  ".join(
                f"{_MARKET_LABELS.get(m, m)}: {n}/{len(evs)}" for m, n in sorted(miss.items())))
        for e in evs:
            if e.get("suspended"):
                lines.append(f"  SUSPENDIDO: {e['match']} — "
                             f"{', '.join(_MARKET_LABELS.get(m, m) for m in e['suspended'])}")
            if e.get("in_play"):
                lines.append(f"  EN JUEGO (excluido del análisis pre-partido): {e['match']}")
    elif struct and not struct.get("active"):
        lines.append("Fuente estructurada de mercados de sets: INACTIVA "
                     "(sin credenciales; ver `betbot feeds status`)")
    st = s["states"]
    lines.append(f"Fuertes: {st.get('fuerte', 0)}  Normales: {st.get('normal', 0)}  "
                 f"Experimentales: {st.get('experimental', 0)}  Vigilar: {st.get('vigilar_precio', 0)}  "
                 f"Probables sin value: {st.get('probable_sin_value', 0)}  "
                 f"Sin value: {st.get('sin_value', 0)}  Descartados: {st.get('descartada', 0)}")
    for src in s["sources"]:
        flag = "OK " if src["ok"] else "FALLO"
        auth = " [calendario autoritativo]" if src.get("authoritative") else ""
        extra = f" · datos de {src['data_timestamp'][:16]}" if src["data_timestamp"] else ""
        err = f" · {src['error']}" if src["error"] else ""
        lines.append(f"  fuente {src['name']}{auth}: {flag} ({src['n']} items){extra}{err}")
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
            start = r.get("start_utc")
            if start:
                lines.append(f"  Inicio confirmado: {start} UTC")
                lines.append(f"  Estado: {r.get('event_status', '?')}")
                lines.append(f"  Fuente de calendario: {r.get('calendar_source', '?')}"
                             + (f" (evento {r['event_id']})" if r.get("event_id") else ""))
                age = r.get("calendar_status_age_h")
                lines.append(f"  Última actualización: {r.get('calendar_updated_at', '?')}"
                             + (f" (hace {age:.1f} h)" if pd.notna(age) else ""))
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
