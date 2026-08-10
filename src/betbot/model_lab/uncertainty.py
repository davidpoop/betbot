"""Incertidumbre de p y EV conservador — auditoría y diseño (FASE 1).

AUDITORÍA DEL ESTADO ACTUAL (verificada en código y bundle):
- Mercado completo (value/engine.py:80-95):
      p_cons = w·p_model + (1−w)·p_novig      EV_cons = p_cons·odds − 1
  con w = shrink_w del bundle ajustado en validación. Valores REALES del
  bundle congelado: ATP w=0.9, WTA w=1.0. Consecuencia: en WTA
  p_cons ≡ p_model y EV_cons ≡ EV_central SIEMPRE; en ATP el shrink es un 10%
  hacia el mercado. La sospecha del mandato queda CONFIRMADA: no existe
  estimación de incertidumbre de p en el camino principal.
- Mercado parcial (screener/run.py:323-325): sigma = 0.02 + |p_M5 − p_M4|/2
  (o 0.03+0.01 sin mercado) y p_cons = p − z·sigma con z=1.0. Es el único
  proxy de incertidumbre existente y solo se usa sin cuota contraria.

DISEÑO OBJETIVO (a implementar y validar en FASE 2, nunca antes de comparar
challengers): el value engine debe recibir un `UncertaintyEstimate` y poder
ABSTENERSE aunque EV_central > 0. Métodos candidatos, por orden de preferencia
empírica a evaluar:
  1. dispersión de ensemble (varianza entre folds/challengers sobre el logit);
  2. residuos de calibración por región de p (binned, out-of-fold);
  3. bootstrap por bloques del modelo final;
  4. intervalo estilo conformal (cuantiles OOF del residuo |y − p|);
  5. ensanchamiento condicionado por OOD/data-quality (banderas existentes).
p_conservative = el extremo del intervalo DESFAVORABLE a la apuesta evaluada
(p_lower para apostar a favor), jamás un descuento fijo cosmético.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UncertaintyEstimate:
    """Contrato de salida para el value engine de FASE 2."""
    p_central: float
    p_lower: float
    p_upper: float
    p_conservative: float       # extremo desfavorable a la selección evaluada
    method: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not (0.0 < self.p_lower <= self.p_central <= self.p_upper < 1.0):
            raise ValueError(f"Intervalo incoherente: {self.p_lower}/{self.p_central}/{self.p_upper}")


def current_production_proxy(p_m5: float, p_m4: float, has_market_pair: bool,
                             shrink_w: float, p_novig: float | None) -> UncertaintyEstimate:
    """Réplica EXACTA del comportamiento actual, para poder compararlo contra
    los métodos de FASE 2 con el mismo contrato. NO es un método nuevo."""
    if has_market_pair and p_novig is not None:
        p_cons = shrink_w * p_m5 + (1.0 - shrink_w) * p_novig
        return UncertaintyEstimate(
            p_central=p_m5, p_lower=min(p_m5, p_cons), p_upper=max(p_m5, p_cons),
            p_conservative=p_cons, method="shrink_a_mercado_actual",
            detail=f"w={shrink_w}; con w=1.0 el intervalo colapsa a un punto "
                   f"(EV_cons==EV_central, WTA actual)")
    sigma = 0.02 + abs(p_m5 - p_m4) / 2
    lo = max(1e-6, p_m5 - sigma)
    return UncertaintyEstimate(p_central=p_m5, p_lower=lo,
                               p_upper=min(1 - 1e-6, p_m5 + sigma),
                               p_conservative=lo, method="sigma_proxy_actual",
                               detail=f"sigma={sigma:.4f} (solo mercados parciales)")
