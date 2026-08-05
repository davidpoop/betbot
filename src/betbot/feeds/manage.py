"""`betbot feeds status|test|markets` — estado real de las fuentes.

Las credenciales se leen SOLO de variables de entorno y aquí únicamente se
informa de su PRESENCIA (true/false); nunca se imprime ni almacena su valor.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from betbot.feeds.base import SourceStatus


def default_structured_providers(cfg: dict) -> list:
    from betbot.feeds.betfair import BetfairExchangeProvider
    order = cfg.get("feeds", {}).get("structured_odds_order", ["betfair"])
    available = {"betfair": BetfairExchangeProvider}
    return [available[n]() for n in order if n in available]


def feeds_status(cfg: dict) -> dict:
    fcfg = cfg.get("feeds", {})
    env = os.environ
    return {
        "calendar_order": fcfg.get("calendar_order", []),
        "odds_order": fcfg.get("odds_order", []),
        "results_order": fcfg.get("results_order", []),
        "structured_odds_order": fcfg.get("structured_odds_order", []),
        "credentials_present": {           # solo presencia; el valor jamás se muestra
            "SPORTRADAR_API_KEY": bool(env.get("SPORTRADAR_API_KEY")),
            "BETFAIR_APP_KEY": bool(env.get("BETFAIR_APP_KEY")),
            "BETFAIR_USERNAME": bool(env.get("BETFAIR_USERNAME")),
            "BETFAIR_PASSWORD": bool(env.get("BETFAIR_PASSWORD")),
            "ODDS_API_KEY": bool(env.get("BETBOT_ODDS_API_KEY") or env.get("ODDS_API_KEY")
                                 or fcfg.get("odds_api_key")),
        },
        "notes": [
            "sportradar: resultados oficiales; inactivo sin SPORTRADAR_API_KEY",
            "betfair: mercados de sets del exchange (solo lectura: listEvents/"
            "listMarketCatalogue/listMarketBook); inactivo sin las 3 credenciales",
            "github: resultados ATP (diario) y WTA (semanal) sin credenciales",
        ],
    }


def feeds_test(cfg: dict) -> dict:
    """Prueba real de cada fuente activa (solo peticiones de lectura)."""
    from datetime import date, timedelta

    from betbot.scan import default_sources
    from betbot.sync import default_results_sources
    report: dict = {"tested_at": datetime.now(timezone.utc).isoformat(), "sources": []}

    def add(role: str, st: SourceStatus) -> None:
        report["sources"].append({"role": role, "name": st.name, "ok": st.ok,
                                  "n": st.n_items, "error": st.error, "notes": st.notes})

    cals, odds = default_sources(cfg)
    matches = []
    for src in cals:
        try:
            ms, st = src.fetch_matches(48)
        except Exception as exc:  # noqa: BLE001
            ms, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        add("calendar", st)
        matches = matches or ms
    for src in odds:
        try:
            _, st = src.fetch_odds(matches)
        except Exception as exc:  # noqa: BLE001
            st = SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        add("odds", st)
    today = date.today()
    for src in default_results_sources(cfg):
        try:
            _, st = src.fetch_results(today - timedelta(days=3), today)
        except Exception as exc:  # noqa: BLE001
            st = SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        add("results", st)
    for prov in default_structured_providers(cfg):
        try:
            _, st = prov.list_markets(matches)
        except Exception as exc:  # noqa: BLE001
            st = SourceStatus(name=getattr(prov, "name", "?"), ok=False, error=str(exc))
        add("structured_odds", st)
    return report


def feeds_calendar(cfg: dict, hours: int = 48) -> str:
    """Prueba EN VIVO de cada calendario autoritativo y demostración de que se
    encuentran partidos PREPARTIDO confirmados. Pensado para ejecutarse en una
    conexión doméstica normal: imprime, por fuente, si respondió, cuántos
    partidos trae, cuántos superan la puerta prepartido y una muestra con hora
    de inicio, estado y jugadores."""
    from datetime import datetime, timezone

    from betbot.prematch import FAIL_CLOSED_MSG, gate
    from betbot.scan import default_sources
    now = datetime.now(timezone.utc)
    cals, _ = default_sources(cfg)
    lines = [f"Comprobación de calendarios prepartido — {now.isoformat(timespec='seconds')}",
             f"Ventana: {hours} h", ""]
    total_ok = 0
    all_matches = []
    for src in cals:
        name = getattr(src, "name", "?")
        if not getattr(src, "authoritative", False):
            lines.append(f"[{name}] IGNORADA como calendario: no es autoritativa")
            continue
        try:
            ms, st = src.fetch_matches(hours)
        except Exception as exc:  # noqa: BLE001
            ms, st = [], SourceStatus(name=name, ok=False, error=str(exc))
        if not st.ok:
            lines.append(f"[{name}] FALLO — {st.error or 'sin detalle'}")
            lines.append("")
            continue
        total_ok += 1
        all_matches.extend(ms)
        res = gate(ms, cfg, window_hours=hours, now=now)
        lines.append(f"[{name}] OK — {len(ms)} partidos en ventana · "
                     f"{len(res.eligible)} CONFIRMADOS prepartido")
        for note in st.notes:
            lines.append(f"    nota: {note}")
        for m in res.eligible[:5]:
            mins = int((m.scheduled_at_utc - now).total_seconds() // 60)
            lines.append(f"    ✓ {m.tour} {m.tournament}: {m.player1} vs {m.player2}")
            lines.append(f"        inicio {m.scheduled_at_utc.isoformat()} (en {mins} min) · "
                         f"estado {m.status} · id {m.event_id}")
        counts = res.counts()
        if counts:
            lines.append("    excluidos: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        lines.append("")
    if total_ok == 0:
        lines.append(FAIL_CLOSED_MSG)
        lines.append("Ninguna fuente autoritativa respondió: `betbot scan` no analizaría nada.")
        return "\n".join(lines)
    from betbot.feeds.base import dedupe_matches
    merged = dedupe_matches(all_matches)
    final = gate(merged, cfg, window_hours=hours, now=now)
    lines.append(f"COMBINADO (tras deduplicar entre fuentes): {len(merged)} partidos · "
                 f"{len(final.eligible)} confirmados prepartido")
    by_tour: dict[str, int] = {}
    for m in final.eligible:
        by_tour[m.tour] = by_tour.get(m.tour, 0) + 1
    if by_tour:
        lines.append("  por circuito: " + ", ".join(f"{k}={v}" for k, v in sorted(by_tour.items())))
        lines.append("  -> `betbot scan` analizaría estos partidos.")
    else:
        lines.append("  -> 0 confirmados: `betbot scan` no emitiría señales (correcto si no "
                     "hay jornada en la ventana).")
    return "\n".join(lines)


def feeds_markets(cfg: dict, hours: int = 48) -> dict:
    """Catálogo REAL de mercados por evento en los proveedores estructurados
    (consulta dinámica; sin lista cerrada de marketType)."""
    from betbot.scan import default_sources
    cals, _ = default_sources(cfg)
    matches = []
    statuses = []
    for src in cals:
        try:
            ms, st = src.fetch_matches(hours)
        except Exception as exc:  # noqa: BLE001
            ms, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        statuses.append({"name": st.name, "ok": st.ok, "error": st.error})
        matches.extend(ms)
    out: dict = {"asked_at": datetime.now(timezone.utc).isoformat(),
                 "calendar_sources": statuses, "providers": []}
    for prov in default_structured_providers(cfg):
        try:
            ems, st = prov.list_markets(matches)
        except Exception as exc:  # noqa: BLE001
            ems, st = [], SourceStatus(name=getattr(prov, "name", "?"), ok=False, error=str(exc))
        out["providers"].append({
            "name": st.name, "ok": st.ok, "error": st.error, "notes": st.notes,
            "events": [{"event": e.event_name, "event_id": e.event_id,
                        "market_types_raw": e.market_types_raw,
                        "canonical_offered": e.canonical_offered,
                        "not_offered": e.not_offered} for e in ems]})
    return out
