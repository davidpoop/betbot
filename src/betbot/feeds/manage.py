"""`betbot feeds status|test|markets` — estado real de las fuentes.

Las credenciales se leen SOLO de variables de entorno y aquí únicamente se
informa de su PRESENCIA (true/false); nunca se imprime ni almacena su valor.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from betbot.feeds.base import SourceStatus


STATUS_CLASSES = ("OK_CON_DATOS", "OK_SIN_COBERTURA", "INACTIVO_CONFIG",
                  "FALLO_TEMPORAL", "FALLO_ESTRUCTURAL")


def classify_status(entry) -> str:
    """Semántica honesta del estado de una fuente. 'OK 0 items' NO es cobertura:
    se distingue OK_SIN_COBERTURA. Sin credencial configurada es INACTIVO_CONFIG
    (no un fallo del proveedor). Un 4xx estructural (403/404) no es temporal."""
    ok = bool(entry.get("ok") if isinstance(entry, dict) else entry.ok)
    err = str((entry.get("error") if isinstance(entry, dict) else entry.error) or "")
    n = int((entry.get("n") if isinstance(entry, dict) else entry.n_items) or 0)
    low = err.lower()
    if not ok or err:
        if "inactivo" in low or "sin betbot_" in low or "sin sportradar" in low \
                or "_key" in low or "credencial" in low:
            return "INACTIVO_CONFIG"
        if "estructural" in low or "http 403" in low or "http 404" in low \
                or "403 forbidden" in low or "response 403" in low:
            return "FALLO_ESTRUCTURAL"
        if not ok:
            return "FALLO_TEMPORAL"
    return "OK_CON_DATOS" if n > 0 else "OK_SIN_COBERTURA"


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
        entry = {"role": role, "name": st.name, "ok": st.ok,
                 "n": st.n_items, "error": st.error, "notes": st.notes}
        entry["class"] = classify_status(entry)
        report["sources"].append(entry)

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


def feeds_matrix(cfg: dict, hours: int = 48) -> str:
    """Matriz de capacidades ATP/WTA: qué fuente cubre cada celda, con qué
    frescura y en qué estado REAL (probes de solo lectura en vivo)."""
    from datetime import date, datetime, timezone

    from betbot.ingest.rankings import load_rankings
    from betbot.config import resolve_path
    from betbot.scan import default_sources
    from betbot.sync import default_results_sources, local_freshness
    now = datetime.now(timezone.utc)
    today = now.date()
    L = [f"MATRIZ DE CAPACIDADES — {now.isoformat(timespec='seconds')}", ""]

    # --- calendario (probe en vivo por fuente) ---
    cals, odds_srcs = default_sources(cfg)
    cal_status: dict[str, list[str]] = {"ATP": [], "WTA": []}
    for src in cals:
        if not getattr(src, "authoritative", False):
            continue
        # una fuente solo puede aparecer bajo los tours que DECLARA cubrir:
        # que el fetch global traiga datos jamás implica cobertura del otro tour
        # (wta_official salía como calendario ATP por esta inferencia)
        sup = set(getattr(src, "supported_tours", ("ATP", "WTA")))
        try:
            ms, st = src.fetch_matches(hours)
        except Exception as exc:  # noqa: BLE001
            ms, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False,
                                      error=str(exc))
        cls = classify_status({"ok": st.ok, "error": st.error, "n": st.n_items})
        for t in ("ATP", "WTA"):
            if t not in sup:
                continue
            n_t = sum(1 for m in ms if m.tour == t)
            cal_status[t].append(
                f"{st.name}: {cls}" + (f" ({n_t} partidos)" if n_t else ""))
    # --- resultados (fallbacks también filtrados por tour declarado) ---
    fresh = local_freshness(cfg)
    res_srcs = default_results_sources(cfg)

    def res_names_for(t: str) -> list[str]:
        return [getattr(s, "name", "?") for s in res_srcs
                if t in set(getattr(s, "supported_tours", ("ATP", "WTA")))]
    # --- rankings ---
    rank_status = {}
    for t in ("ATP", "WTA"):
        rk = load_rankings(resolve_path(cfg, "manual_dir"), t, today,
                           int(cfg.get("rankings", {}).get("max_age_days", 45)))
        rank_status[t] = (f"efectivos {rk.published} ({len(rk.by_player)} jugadoras/es)"
                          if rk.published else "AUSENTES (fallback Elo)")
        if rk.warnings:
            rank_status[t] += " ⚠ " + "; ".join(rk.warnings[:1])
    # --- cuotas ---
    import os
    odds_ok = bool(os.environ.get("BETBOT_ODDS_API_KEY")
                   or cfg.get("feeds", {}).get("odds_api_key"))
    bf_ok = all(os.environ.get(v) for v in
                ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD"))

    def cell(cap: str, t: str) -> str:
        if cap == "calendar":
            return " · ".join(cal_status[t]) or "SIN FUENTE"
        if cap == "results":
            f = fresh.get(t)
            age = (today - f).days if f else None
            primary = ("wta_official (mismo día)" if t == "WTA"
                       else "github/TML (diario si upstream publica)")
            return (f"hasta {f} ({age}d) · primaria {primary} · "
                    f"fallbacks {', '.join(res_names_for(t))}")
        if cap == "rankings":
            return rank_status[t] + " · derived_from_results"
        if cap == "surface":
            return "official > tournament_registry > inferred(con aviso)"
        if cap == "moneyline":
            base = "github_te (sin clave, 6h)"
            return base + (" + oddsapi multioperador [configured]" if odds_ok
                           else " · oddsapi INACTIVO_CONFIG (falta BETBOT_ODDS_API_KEY)")
        if cap == "set_odds":
            return ("betfair listo [configured]" if bf_ok else
                    "NOT OFFERED · betfair integrado pero INACTIVO_CONFIG (opcional)")
        return "?"

    for cap, label in (("calendar", "calendario"), ("results", "resultados"),
                       ("rankings", "rankings"), ("surface", "superficie"),
                       ("moneyline", "cuotas moneyline"), ("set_odds", "cuotas de sets")):
        L.append(f"{label.upper()}")
        for t in ("ATP", "WTA"):
            L.append(f"  {t}: {cell(cap, t)}")
        L.append("")
    L.append("Clases: OK_CON_DATOS / OK_SIN_COBERTURA / INACTIVO_CONFIG / "
             "FALLO_TEMPORAL / FALLO_ESTRUCTURAL")
    L.append("Una fuente OPCIONAL inactiva (sportradar/betfair sin credencial) no es "
             "un error operacional.")
    return "\n".join(L)


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


def feeds_wta_raw(cfg: dict, match_ids: tuple = (), limit: int = 5) -> str:
    """Vuelca los campos CRUDOS de `api.wtatennis.com` y guarda un snapshot
    anonimizado, para auditar por qué un partido se consideró prepartido."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from betbot.config import resolve_path
    from betbot.feeds import cache
    from betbot.feeds.base import evidence_of_play, is_placeholder_time
    from betbot.feeds.wta_official import URL, HEADERS, WtaOfficialCalendar
    now = datetime.now(timezone.utc)
    L = [f"Volcado crudo de api.wtatennis.com — {now.isoformat(timespec='seconds')}"]
    try:
        data = cache.get_json(URL, ttl_seconds=0, headers=HEADERS)
    except Exception as exc:  # noqa: BLE001
        L.append(f"FALLO: {exc}")
        return "\n".join(L)
    rows = data
    if isinstance(data, dict):
        for k in ("Matches", "matches", "data"):
            if isinstance(data.get(k), list):
                rows = data[k]
                break
    rows = rows if isinstance(rows, list) else []
    L.append(f"{len(rows)} filas recibidas")
    wanted = {str(x).upper() for x in match_ids}
    sel = [r for r in rows if isinstance(r, dict)
           and (not wanted or str(r.get("MatchID", "")).upper() in wanted)][:limit or None]
    L.append(f"{len(sel)} filas seleccionadas" + (f" (MatchID en {sorted(wanted)})" if wanted else ""))
    # reparto de marcas horarias: delata los placeholders compartidos
    stamps: dict = {}
    for r in rows:
        if isinstance(r, dict):
            stamps[str(r.get("MatchTimeStamp", ""))] = stamps.get(
                str(r.get("MatchTimeStamp", "")), 0) + 1
    rep = sorted(stamps.items(), key=lambda kv: -kv[1])[:5]
    L.append("Marcas horarias más repetidas: "
             + ", ".join(f"{k or '(vacía)'} ×{v}" for k, v in rep))
    for r in sel:
        L.append("")
        L.append(f"── MatchID {r.get('MatchID')} ──")
        for k in sorted(r):
            L.append(f"    {k}: {r[k]!r}")
        ts = str(r.get("MatchTimeStamp", ""))
        parsed = None
        try:
            from betbot.feeds.wta_official import _parse_iso
            parsed = _parse_iso(ts)
        except Exception:  # noqa: BLE001
            pass
        bad, why = is_placeholder_time(parsed, stamps.get(ts, 1))
        L.append(f"  -> hora provisional: {bad}" + (f" ({why})" if bad else ""))
        played, ev = evidence_of_play(r)
        L.append(f"  -> evidencia de partido jugado: {played}"
                 + (f" ({', '.join(ev)})" if played else ""))
    # snapshot anonimizado
    def anon(row, i):
        out = dict(row)
        for k in list(out):
            if "PlayerName" in k or k in ("PlayerIDA", "PlayerIDB"):
                out[k] = f"<jugador{i}{k[-1]}>"
        return out
    snap = [anon(r, i) for i, r in enumerate(sel, 1)]
    path = Path(resolve_path(cfg, "exports_dir")) / "wta_raw_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"captured_at": now.isoformat(), "rows": snap},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    L.append("")
    L.append(f"Snapshot anonimizado: {path}")
    return "\n".join(L)


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
