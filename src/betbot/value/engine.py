"""Motor de value y estados de candidata (compartido por backtester y screener).

Estados (precedencia): descartada > fuerte > normal > vigilar_precio >
probable_sin_value > sin_value. Los mercados experimentales quedan capados en
"experimental" aunque cumplan umbrales de fuerte/normal.

Mercados completos vs parciales:
- completo: el usuario aportó AMBOS lados -> p_novig real (prop/power/shin) y edge.
- parcial: solo un lado -> NUNCA se inventa no-vig; p_cons usa la variante
  p_rob = p - z*sigma y el estado se capa en "normal".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from betbot.markets import novig

STATE_ORDER = ["descartada", "experimental", "fuerte", "normal",
               "vigilar_precio", "probable_sin_value", "sin_value"]
_STATE_RANK = {"fuerte": 0, "normal": 1, "experimental": 2, "vigilar_precio": 3,
               "probable_sin_value": 4, "sin_value": 5, "descartada": 6}


@dataclass
class MarketEval:
    market: str
    selection: str          # 'a' | 'b' | 'yes' | 'no'
    selection_name: str
    odds: float | None
    bookmaker: str = ""
    p_model: float | None = None
    p_novig: float | None = None       # solo mercados completos
    novig_all: dict = field(default_factory=dict)
    p_cons: float | None = None
    edge: float | None = None
    ev: float | None = None
    ev_cons: float | None = None
    o_min: float | None = None
    fair_odds: float | None = None
    full_market: bool = False
    state: str = "sin_value"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["reasons"] = ";".join(self.reasons)
        d["novig_all"] = {k: round(v, 4) for k, v in self.novig_all.items()}
        for k in ("p_model", "p_novig", "p_cons", "edge", "ev", "ev_cons", "o_min", "fair_odds"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        return d


def evaluate_selection(*, market: str, selection: str, selection_name: str,
                       p_model: float, odds: float | None, opposite_odds: float | None,
                       bookmaker: str, tour_shrink_w: float, sigma: float,
                       is_experimental: bool, hard_flags: list[str], cfg_sel: dict) -> MarketEval:
    """Evalúa un lado de un mercado. p_model = probabilidad calibrada del lado."""
    ev_min = float(cfg_sel["ev_min"])
    edge_min = float(cfg_sel["edge_min"])
    ev_strong = float(cfg_sel["ev_strong"])
    edge_strong = float(cfg_sel["edge_strong"])
    z = float(cfg_sel["z_sigma"])
    lo, hi = cfg_sel["odds_soft_range"]

    me = MarketEval(market=market, selection=selection, selection_name=selection_name,
                    odds=odds, bookmaker=bookmaker, p_model=p_model)
    me.fair_odds = 1.0 / max(p_model, 1e-6)

    if hard_flags:
        me.state = "descartada"
        me.reasons = list(hard_flags)
        return me

    full = odds is not None and opposite_odds is not None and odds > 1.0 and opposite_odds > 1.0
    me.full_market = full
    if full:
        me.novig_all = novig.all_methods(odds, opposite_odds)
        me.p_novig = me.novig_all["prop"]
        me.p_cons = tour_shrink_w * p_model + (1.0 - tour_shrink_w) * me.p_novig
        me.edge = p_model - me.p_novig
    else:
        me.p_cons = max(1e-6, p_model - z * sigma)
        if odds is not None:
            me.reasons.append("mercado_parcial_sin_cuota_contraria")
    me.o_min = (1.0 + ev_min) / max(me.p_cons, 1e-6)

    if odds is None:
        me.state = ("probable_sin_value" if me.p_cons is not None
                    and me.p_cons >= cfg_sel["prob_likely"] else "sin_value")
        me.reasons.append("sin_cuota")
        return me

    me.ev = p_model * odds - 1.0
    me.ev_cons = me.p_cons * odds - 1.0
    if not (lo <= odds <= hi):
        me.reasons.append(f"cuota_fuera_de_rango_suave[{lo},{hi}]")

    passes_normal = me.ev_cons >= ev_min and (me.edge is None or me.edge >= edge_min)
    passes_strong = (full and me.ev_cons >= ev_strong and me.edge is not None
                     and me.edge >= edge_strong and not me.reasons)

    if is_experimental:
        me.state = "experimental" if passes_normal else (
            "vigilar_precio" if me.ev is not None and me.ev > 0 and odds < me.o_min else (
                "probable_sin_value" if me.p_cons >= cfg_sel["prob_likely"] else "sin_value"))
        return me
    if passes_strong:
        me.state = "fuerte"
    elif passes_normal:
        me.state = "normal"
    elif me.ev is not None and me.ev > 0 and odds < me.o_min:
        me.state = "vigilar_precio"
        me.reasons.append(f"apta_a_cuota>={me.o_min:.2f}")
    elif me.p_cons >= cfg_sel["prob_likely"]:
        me.state = "probable_sin_value"
    else:
        me.state = "sin_value"
    return me


def pick_recommended_primary(evals: list[MarketEval]) -> MarketEval | None:
    """Una sola recommended_primary por partido: mejor estado y, a igualdad,
    mayor EV conservador."""
    candidates = [e for e in evals if e.state not in ("descartada", "sin_value")]
    if not candidates:
        return None
    return sorted(candidates, key=lambda e: (_STATE_RANK[e.state],
                                             -(e.ev_cons if e.ev_cons is not None else -9)))[0]
