"""Agregación de evaluaciones en OPORTUNIDADES únicas y salida operativa.

Motivo (observado en un scan real): 2 señales "normales" que eran LA MISMA idea
repetida por dos bookmakers, y 49 "vigilar" que eran ~6 ideas repetidas por
muchas casas. Una casa distinta NO constituye una apuesta distinta.

Una oportunidad económica se define por (event_id estable, mercado, selección).
Todas las cuotas de bookmakers de esa oportunidad se agrupan; se opera SIEMPRE
con la mejor cuota ejecutable (la mayor no caducada). `recommended_primary`
vive al nivel de la oportunidad agregada.

Este módulo NO toca modelos, probabilidades, calibración ni thresholds: lee las
evaluaciones ya calculadas (que siguen íntegras en el ledger) y las agrega para
presentarlas. La abstención es un resultado normal: TOP PICKS puede ser 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

TOP_STATES = ("fuerte", "normal")
MAX_TOP_PICKS = 10
# vetos de calidad: si la mejor cuota arrastra alguno, la oportunidad no puede
# ser un TOP PICK aunque su estado sea fuerte/normal (sí aparece en --verbose)
GRAVE_FLAGS = ("ood_", "jugador_desconocido", "cuota_caducada",
               "data_freshness_unknown", "historial_escaso_en_datos")

_MARKET_SHORT = {"match_winner": "ML", "set1_winner": "1er set",
                 "wins_set": "+1.5 sets", "straight_sets": "2-0",
                 "three_sets": "O/U 2.5 sets"}


@dataclass
class Opportunity:
    """Una idea apostable única, con todas sus cuotas agrupadas."""
    key: tuple                    # (event_id | match, market, selection)
    match: str
    tour: str = ""
    tournament: str = ""
    market: str = ""
    selection: str = ""
    selection_name: str = ""
    state: str = ""               # estado de la MEJOR cuota ejecutable
    best_odds: float | None = None
    best_bookmaker: str = ""
    second_odds: float | None = None
    second_bookmaker: str = ""
    n_books: int = 0
    o_min: float | None = None
    p_model: float | None = None
    edge: float | None = None
    ev_cons: float | None = None
    odds_age_min: float | None = None
    recommended_primary: bool = False
    reasons: str = ""
    start_utc: str = ""
    event_status: str = ""
    calendar_source: str = ""
    calendar_updated_at: str = ""
    event_id: str = ""
    all_books: list = field(default_factory=list)   # [(bookmaker, odds, state)]

    @property
    def grave_veto(self) -> str:
        for flag in GRAVE_FLAGS:
            if flag in (self.reasons or ""):
                return flag
        return ""


def _f(v) -> float | None:
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def aggregate_opportunities(rows: pd.DataFrame) -> list[Opportunity]:
    """Colapsa las evaluaciones por-bookmaker en oportunidades únicas.

    La identidad usa el event_id ESTABLE del calendario cuando existe (el mismo
    partido con jugadores en orden distinto o etiqueta distinta entre fuentes
    sigue siendo una única oportunidad) y cae al par (partido, mercado,
    selección) si no lo hay.
    """
    if rows is None or not len(rows):
        return []
    with_odds = rows[rows["odds"].notna()].copy()
    if not len(with_odds):
        return []
    ev = with_odds.get("event_id")
    with_odds["_opp_key"] = [
        ((str(e) if e and not pd.isna(e) else str(m)), str(mk), str(sel))
        for e, m, mk, sel in zip(with_odds.get("event_id", [None] * len(with_odds)),
                                 with_odds["match"], with_odds["market"],
                                 with_odds["selection"])]
    out: list[Opportunity] = []
    for key, grp in with_odds.groupby("_opp_key", sort=False):
        grp = grp.sort_values("odds", ascending=False)
        # mejor cuota EJECUTABLE: la mayor cuya evaluación no quedó descartada
        # (una cuota caducada o de partido vetado no es ejecutable)
        exe = grp[grp["state"] != "descartada"]
        best = exe.iloc[0] if len(exe) else grp.iloc[0]
        # segunda mejor de un bookmaker DISTINTO
        second = None
        pool = exe if len(exe) else grp
        for _, r in pool.iterrows():
            if r["bookmaker"] != best["bookmaker"]:
                second = r
                break
        opp = Opportunity(
            key=key, match=str(best["match"]), tour=str(best.get("tour", "")),
            tournament=str(best.get("tournament", "")),
            market=str(best["market"]), selection=str(best["selection"]),
            selection_name=str(best.get("selection_name", "")),
            state=str(best["state"]),
            best_odds=_f(best.get("odds")), best_bookmaker=str(best.get("bookmaker", "")),
            second_odds=_f(second.get("odds")) if second is not None else None,
            second_bookmaker=str(second.get("bookmaker", "")) if second is not None else "",
            n_books=int(grp["bookmaker"].nunique()),
            o_min=_f(best.get("o_min")), p_model=_f(best.get("p_model")),
            edge=_f(best.get("edge")), ev_cons=_f(best.get("ev_cons")),
            odds_age_min=_f(best.get("odds_age_min")),
            recommended_primary=bool(grp.get("recommended_primary", pd.Series(dtype=bool)).any()),
            reasons=str(best.get("reasons", "") or ""),
            start_utc=str(best.get("start_utc") or ""),
            event_status=str(best.get("event_status") or ""),
            calendar_source=str(best.get("calendar_source") or ""),
            calendar_updated_at=str(best.get("calendar_updated_at") or ""),
            event_id=str(best.get("event_id") or ""),
            all_books=[(str(r["bookmaker"]), _f(r["odds"]), str(r["state"]))
                       for _, r in grp.iterrows()])
        out.append(opp)
    return out


def select_top_picks(opps: list[Opportunity], max_n: int = MAX_TOP_PICKS
                     ) -> list[Opportunity]:
    """Filtro estricto + ranking. No existe mínimo: puede devolver 0.

    Requisitos: señal fuerte/normal en la MEJOR cuota; mejor cuota >= cuota
    mínima; hora de inicio confirmada (los partidos sin puertas superadas ni
    llegan aquí: el screener solo analiza elegibles); sin veto grave de
    calidad/OOD/frescura; cuota no caducada (una caducada estaría descartada).
    """
    picks = [o for o in opps
             if o.state in TOP_STATES
             and o.best_odds is not None and o.o_min is not None
             and o.best_odds >= o.o_min
             and o.start_utc
             and not o.grave_veto]
    rank = {s: i for i, s in enumerate(TOP_STATES)}
    picks.sort(key=lambda o: (rank.get(o.state, 9),
                              -(o.ev_cons if o.ev_cons is not None else -1e9),
                              -(o.edge if o.edge is not None else -1e9),
                              o.odds_age_min if o.odds_age_min is not None else 1e9))
    return picks[:max_n]


def watchlist(opps: list[Opportunity]) -> tuple[list[Opportunity], int]:
    """(oportunidades vigilar_precio deduplicadas, cuántas a <=1% de o_min)."""
    wl = [o for o in opps if o.state == "vigilar_precio" and not o.grave_veto]
    wl.sort(key=lambda o: ((o.o_min - o.best_odds) / o.o_min
                           if o.o_min and o.best_odds else 1e9))
    near = sum(1 for o in wl
               if o.o_min and o.best_odds and o.best_odds >= o.o_min * 0.99)
    return wl, near


# ---------------------------------------------------------------------------
# salida de terminal compacta
# ---------------------------------------------------------------------------

def _pick_title(o: Opportunity) -> str:
    parts = [p.strip() for p in o.match.split(" vs ")]
    if o.market == "match_winner" and len(parts) == 2 and o.selection_name in parts:
        rival = parts[1] if o.selection_name == parts[0] else parts[0]
        return f"{o.selection_name} ML vs {rival}"
    return f"{o.match} — {_MARKET_SHORT.get(o.market, o.market)}: {o.selection_name}"


def _pick_block(i: int, o: Opportunity) -> list[str]:
    star = "⭐ " if o.recommended_primary else ""
    L = [f"{i}. {star}{_pick_title(o)}   [{o.tour} · {o.tournament}]"]
    if o.start_utc:
        L.append(f"   Inicio confirmado: {o.start_utc} ({o.calendar_source})")
    age = f" · hace {o.odds_age_min:.0f} min" if o.odds_age_min is not None else ""
    L.append(f"   Mejor cuota: {o.best_odds:.2f} @ {o.best_bookmaker}{age}"
             + (f"  ·  2ª: {o.second_odds:.2f} @ {o.second_bookmaker}"
                if o.second_odds is not None else "")
             + (f"  ·  {o.n_books} bookmakers" if o.n_books > 2 else ""))
    stats = []
    if o.p_model is not None:
        stats.append(f"Modelo: {100 * o.p_model:.1f}%")
    if o.o_min is not None:
        stats.append(f"Mínima: {o.o_min:.2f}")
    if o.edge is not None:
        stats.append(f"Edge: {100 * o.edge:+.1f} pp")
    if o.ev_cons is not None:
        stats.append(f"EV conservador: {100 * o.ev_cons:+.1f}%")
    if stats:
        L.append("   " + " · ".join(stats))
    L.append(f"   {o.state.upper()}")
    return L


def _age_days(ref: str, iso: str | None) -> int | None:
    """Edad en días, NUNCA negativa: si el dato es 'del futuro' respecto a la
    referencia (cruce de medianoche UTC/local), la edad honesta es 0."""
    if not iso:
        return None
    try:
        return max(0, (date.fromisoformat(str(ref)) - date.fromisoformat(str(iso))).days)
    except ValueError:
        return 0


def _within_policy(iso: str | None, ref: str) -> bool:
    """Política ÚNICA de frescura de marcadores completos (sync.py): la misma
    que usan capability_freshness, la matriz y la puerta prepartido."""
    from betbot.sync import is_full_results_fresh
    if not iso:
        return False
    try:
        return is_full_results_fresh(date.fromisoformat(str(iso)),
                                     date.fromisoformat(str(ref)))
    except ValueError:
        return True


def _rankings_line(t: str, summary: dict) -> str:
    """El ✓ de rankings significa algo concreto: DENTRO de la política real
    del sistema (rankings.max_age_days), no 'el fichero existe'."""
    r = (summary.get("rankings_status") or {}).get(t) or {}
    if not r.get("published"):
        return f"⚠ rankings {t}: ausentes (fallback Elo)"
    age, cap = r.get("age_days"), r.get("max_age_days", 45)
    ok = r.get("within_policy", (age is not None and age <= cap and not r.get("warnings")))
    mark = "✓" if ok else "⚠"
    tail = "" if ok else " FUERA DE POLÍTICA"
    return (f"{mark} rankings {t} efectivos {r['published']} "
            f"({age}d de {cap} permitidos){tail}")


def _partial_block(t: str, iso: str, age: int, cap: dict, covered: list,
                   summary: dict) -> list[str]:
    """Bloque honesto: qué está al día (features) y qué no (marcadores)."""
    lines = [f"⚠ FRESCURA PARCIAL {t}"]
    for c in covered[:4]:
        name = c["tournament"] if isinstance(c, dict) else str(c)
        until = c.get("until", "?") if isinstance(c, dict) else "?"
        lines.append(f"   ✓ outcome state {name} hasta {until}")
    lines.append("   ✓ Elo, Elo de superficie, actividad (rest/m14/m12m/layoff) y "
                 "experiencia actualizados con recent outcomes")
    lines.append("   ✓ already_completed cubre también esos partidos recientes")
    lines.append(f"   ⚠ marcadores completos hasta {iso} ({age}d): juegos, set1 y "
                 f"stats de servicio siguen atrasados")
    lines.append("   ⚠ retired_recent incompleto en outcome-only: la retirada no se "
                 "conoce y jamás se inventa")
    lines.append("   " + _rankings_line(t, summary))
    return lines


def degraded_banner(summary: dict) -> str | None:
    """Aviso de frescura CONTEXTUAL y por capacidad.

    Distingue FULL RESULT FRESHNESS (marcadores completos) de OPERATIONAL/
    FEATURE FRESHNESS (lo que alimenta a M4/M5) y lo aplica al CONTEXTO real
    de la jornada:
    - torneo analizado CON cobertura reciente -> FRESCURA PARCIAL (las
      features están al día; el detalle de marcadores no);
    - torneo analizado SIN cobertura reciente -> MODO DEGRADADO para ese tour,
      nombrando los torneos afectados;
    - tour con retraso pero SIN partidos analizados -> nota de sistema
      separada: jamás contamina la calidad de las picks del otro circuito;
    - todo fresco -> sin banner."""
    ref = str(summary.get("date"))
    fresh = summary.get("results_freshness") or {}
    cov = summary.get("results_coverage") or {}
    analyzed = summary.get("analyzed_coverage") or {}
    lines: list[str] = []
    notes: list[str] = []
    for t in ("ATP", "WTA"):
        iso = fresh.get(t)
        c = cov.get(t) or {}
        an = analyzed.get(t) or {}
        # sin contexto de jornada (llamadas directas/paneles) se evalúan todos
        is_analyzed = bool(an.get("n_matches")) if analyzed else True
        age = _age_days(ref, iso)
        if age is None:
            (lines if is_analyzed else notes).append(f"⚠ {t}: sin datos de resultados")
            continue
        if _within_policy(iso, ref):
            continue                      # dentro de política: no es un fallo                                  # full fresco: nada que avisar
        prod = c.get("production_feature_freshness")
        if analyzed:
            # con contexto de jornada manda el contexto: un torneo NO cubierto
            # jamás hereda el bloque PARCIAL de otro torneo del mismo circuito
            covered = list(an.get("covered") or [])
        else:
            # sin contexto (paneles/llamadas directas) la cobertura global sirve
            covered = [{"tournament": cc["tournament"], "until": str(cc["until"])}
                       for cc in (c.get("active_coverage") or {}).values()]
        uncovered = list(an.get("uncovered") or [])
        if not is_analyzed:
            estado = ("cobertura reciente PARCIAL" if prod == "PARTIAL"
                      else "sin cobertura reciente")
            n_blocked = int(an.get("blocked") or 0)
            n_found = int(an.get("found_in_window") or 0)
            if n_blocked and not an.get("n_matches"):
                # CIRCULARIDAD PROHIBIDA: si los partidos del tour fueron
                # bloqueados por frescura/confirmación, no se puede afirmar
                # "no hay picks" como prueba de que el problema no afecta
                lines.append(f"⚠ {t}: {n_blocked} partido(s) en ventana NO "
                             f"analizados por frescura/confirmación (histórico "
                             f"completo hasta {iso}, {age}d; {estado}). Ver "
                             f"`--verbose` para el motivo de cada exclusión.")
            elif n_found == 0:
                notes.append(f"{t}: {estado} · histórico completo hasta {iso} "
                             f"({age}d) — sin partidos {t} en la ventana, no "
                             f"afecta a las probabilidades mostradas")
            else:
                notes.append(f"{t}: {estado} · histórico completo hasta {iso} "
                             f"({age}d) — los partidos {t} analizados no "
                             f"produjeron picks")
            continue
        if prod == "PARTIAL" and covered:
            lines += _partial_block(t, iso, age, c, covered, summary)
            if uncovered:
                lines.append(f"⚠ MODO DEGRADADO {t} — sin cobertura reciente para: "
                             f"{', '.join(uncovered[:4])}. Ahí Elo y actividad/OOD se "
                             f"miden con datos viejos y already_completed solo cubre "
                             f"hasta {iso}.")
        else:
            lines.append(
                f"⚠ MODO DEGRADADO {t} — resultados con retraso ({age}d, hasta {iso}) "
                f"y sin cobertura reciente suficiente"
                + (f" para: {', '.join(uncovered[:4])}" if uncovered else "")
                + ". Afecta a: verificación already_completed (solo cubre hasta esa "
                  "fecha), Elo y actividad/OOD (medidos con datos viejos) y fuentes "
                  "sin evidencia de juego (exigen corroboración independiente).")
            lines.append("   " + _rankings_line(t, summary))
    if not lines and not notes:
        return None
    if notes:
        lines.append("Nota de sistema (no afecta a las picks de esta jornada): "
                     + " · ".join(notes))
    lines.append("No se oculta ninguna señal bloqueada por esto: ver `--verbose`.")
    return "\n".join(lines)


def render_daily(res, show_watch: bool = False) -> str:
    """Salida operativa por defecto de `betbot scan`: TOP PICKS + resumen."""
    s = res.summary
    L: list[str] = [f"BETBOT — {s['date']} · {s['window_hours']}h"]
    if s.get("fail_closed"):
        L.append("")
        L.append(s.get("fail_closed_message", ""))
        for src in s["sources"]:
            if src.get("authoritative"):
                L.append(f"  calendario {src['name']}: "
                         + ("OK" if src["ok"] else f"FALLO · {src['error'][:90]}"))
        return "\n".join(L)
    banner = degraded_banner(s)
    if banner:
        L.append(banner)
    # ---- sección "Datos": estado honesto por capacidad, sin verde artificial ----
    L.append("Datos:")
    fresh = s.get("results_freshness") or {}
    analyzed = s.get("analyzed_coverage") or {}
    for t in ("ATP", "WTA"):
        iso = fresh.get(t)
        if not iso:
            L.append(f"  ⚠ resultados {t}: sin datos")
            continue
        age = _age_days(str(s.get("date")), iso) or 0
        cov = (s.get("results_coverage") or {}).get(t) or {}
        prod = cov.get("production_feature_freshness")
        mark = ("✓" if _within_policy(iso, str(s.get("date")))
                else ("~" if prod == "PARTIAL" else "⚠"))
        sin_picks = "" if (not analyzed or (analyzed.get(t) or {}).get("n_matches")) \
            else "  (sin partidos analizados hoy)"
        L.append(f"  {mark} resultados {t}: marcadores completos hasta {iso} ({age}d)"
                 f"{sin_picks}")
        by_t = (cov.get("outcome_state") or {}).get("by_tournament") or {}
        for c in list(by_t.values())[:3]:
            m2 = "✓" if c.get("status") == "FRESH" else "⚠"
            L.append(f"      {m2} outcome state {c['tournament']} hasta {c['until']} "
                     f"({c['age_days']}d)")
        if prod:
            L.append(f"      estado predictivo (Elo/actividad/experiencia): {prod}"
                     + (" · retired_recent PARTIAL (outcome-only no conoce la retirada)"
                        if prod == "PARTIAL" else ""))
    L.append("  " + "  ·  ".join(_rankings_line(t, s) for t in ("ATP", "WTA")))
    ss = s.get("surface_sources") or {}
    n_conf = ss.get("official", 0) + ss.get("tournament_registry", 0)
    n_inf = ss.get("inferred", 0)
    L.append(f"  {'✓' if not n_inf else '⚠'} superficie: {n_conf} confirmadas "
             f"(oficial/registro de torneos)"
             + (f" · {n_inf} estimadas heurísticamente" if n_inf else ""))
    cals = [f"{x['name']} {x.get('class', 'OK' if x['ok'] else 'FALLO')}"
            for x in s["sources"] if x.get("authoritative")]
    if cals:
        L.append("  Calendario: " + " · ".join(cals))
    cov = s.get("market_coverage") or {}
    ml = cov.get("match_winner", 0)
    sets_cov = sum(cov.get(k, 0) for k in ("set1_winner", "wins_set",
                                           "straight_sets", "three_sets"))
    L.append(f"  Cuotas: Moneyline {ml}/{s['eligible']}"
             + (f" · Sets {sets_cov}/{s['eligible']}" if sets_cov
                else " · Sets: not offered (fase posterior)"))
    L.append(f"Partidos confirmados prepartido: {s.get('calendar_confirmed', 0)}"
             + (f" · excluidos con motivo: {sum((s.get('prematch_excluded') or {}).values())}"
                if s.get("prematch_excluded") else ""))

    top = res.top_picks
    L.append("")
    if top:
        L.append(f"===== TOP PICKS ({len(top)} de máx. {MAX_TOP_PICKS}) =====")
        for i, o in enumerate(top, 1):
            L.append("")
            L.extend(_pick_block(i, o))
    else:
        L.append("TOP PICKS: 0")
        L.append("No hay apuestas que superen todos los filtros hoy.")

    wl, near = res.watchlist, res.watchlist_near
    L.append("")
    if wl:
        unidad = "oportunidad única" if len(wl) == 1 else "oportunidades únicas"
        L.append(f"WATCHLIST: {len(wl)} {unidad}"
                 + (f" · {near} a ≤1% de la cuota mínima" if near else ""))
        if show_watch:
            for o in wl:
                gap = ((o.o_min - o.best_odds) / o.o_min * 100
                       if o.o_min and o.best_odds else float("nan"))
                L.append(f"  · {_pick_title(o)}  mejor {o.best_odds:.2f} @ "
                         f"{o.best_bookmaker} → necesita ≥ {o.o_min:.2f} "
                         f"(a {gap:.1f}%) · {o.n_books} bookmakers"
                         + (f" · inicio {o.start_utc}" if o.start_utc else ""))
        else:
            L.append("Ejecuta `betbot scan --watch` para verlas.")
    else:
        L.append("WATCHLIST: 0 oportunidades")

    st = s.get("states") or {}
    n_desc = st.get("descartada", 0)
    L.append("")
    L.append(f"Diagnóstico: {s.get('markets_evaluated', 0)} evaluaciones · "
             f"{s.get('opportunities', 0)} oportunidades únicas · "
             f"prob. sin value {st.get('probable_sin_value', 0)} · "
             f"sin value {st.get('sin_value', 0)} · descartadas {n_desc}"
             + (f" · orphan_quotes {s['orphan_quotes']}" if s.get("orphan_quotes") else "")
             + " — `--verbose` para el detalle completo")
    return "\n".join(L)
