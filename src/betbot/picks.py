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


def degraded_banner(summary: dict) -> str | None:
    """MODO DEGRADADO visible cuando los resultados van con retraso."""
    fresh = summary.get("results_freshness") or {}
    worst, per_tour = 0, []
    for t in ("ATP", "WTA"):
        iso = fresh.get(t)
        if not iso:
            per_tour.append(f"{t} sin datos")
            worst = max(worst, 99)
            continue
        try:
            age = (date.fromisoformat(str(summary.get("date"))) - date.fromisoformat(iso)).days
        except ValueError:
            age = 0
        if age > 2:
            per_tour.append(f"{t} {age}d")
            worst = max(worst, age)
    if not worst:
        return None
    detail = ", ".join(per_tour)
    return (f"⚠ MODO DEGRADADO — resultados con retraso ({detail}). Afecta a: "
            f"verificación already_completed (solo cubre hasta esa fecha), Elo y "
            f"actividad/OOD (medidos con datos viejos) y fuentes sin evidencia de "
            f"juego (exigen corroboración independiente). No se oculta ninguna señal "
            f"bloqueada por esto: ver `--verbose`.")


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
    # estado compacto de fuentes
    cals = [f"{x['name']} {'OK' if x['ok'] else 'no disponible'}"
            for x in s["sources"] if x.get("authoritative")]
    if cals:
        L.append("Calendario: " + " · ".join(cals))
    cov = s.get("market_coverage") or {}
    ml = cov.get("match_winner", 0)
    sets_cov = sum(cov.get(k, 0) for k in ("set1_winner", "wins_set",
                                           "straight_sets", "three_sets"))
    L.append(f"Cuotas: Moneyline {ml}/{s['eligible']}"
             + (f" · Sets {sets_cov}/{s['eligible']}" if sets_cov
                else " · Sets no disponibles"))
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
