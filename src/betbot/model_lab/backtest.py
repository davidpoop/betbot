"""Backtest de apuestas del laboratorio — reglas CONGELADAS antes del test.

FASE 1: aquí solo se PRE-REGISTRAN las reglas y el contrato del informe. No se
ejecuta ningún backtest de challengers todavía (primero: auditoría revisada y
challengers entrenados en FASE 2). El champion ya tiene su backtest modo A en
backtest/run.py; este módulo no lo sustituye.

Las reglas de decisión se congelan AQUÍ, con hash, antes de tocar cualquier
test: cambiar una regla después de ver resultados invalida el run y exige
declarar un run nuevo. PROHIBIDO optimizar thresholds sobre el test.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FrozenRules:
    """Reglas de apuesta idénticas a las de producción (config selection),
    congeladas para todos los backtests de challengers de FASE 2."""
    ev_min: float = 0.03
    edge_min: float = 0.02
    odds_range: tuple[float, float] = (1.40, 4.00)
    stake: str = "plano_1u"
    retirement_rule: str = "1_set"
    requires_full_market: bool = True

    def rules_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


REPORT_CONTRACT = [
    # lo que TODO backtest de challenger deberá reportar, por tour/superficie/
    # tramo de cuota/tramo de edge/confianza del modelo/temporada:
    "n_bets", "hit_rate", "avg_odds", "roi", "yield", "max_drawdown_units",
    "profit_factor", "clv_si_hay_cierre", "bootstrap_ci_roi", "worst_period",
]


def not_yet(*_args, **_kwargs):
    raise NotImplementedError(
        "Backtest de challengers: FASE 2. Las reglas ya están congeladas "
        f"(hash {FrozenRules().rules_hash()}); ningún threshold se ajustará sobre el test.")
