# Resultados del MVP (entrenamiento 2026-08-03)

Datos reales: 114.675 partidos (ATP 70.067 · 2000–mar 2026; WTA 44.608 · 2007–2025),
110.652 con cuotas; 2.976 jugadores; 2,75% retiradas + 0,49% walkovers (consistente
con la literatura). División: entrenamiento ≤2022 · OOF walk-forward 2013–2022 ·
validación 2023–2024 · **test 2025 sellado (1 acceso, registrado)**.

## Jerarquía de modelos (log loss OOF 2013–2022, calibrados)

| Modelo | ATP | WTA |
|---|---|---|
| M1 ranking | 0.6166 | 0.6293 |
| M2 Elo | 0.6064 | 0.6184 |
| M3 Elo+superficie | 0.6023 | 0.6167 |
| M4 features sin mercado | 0.5994 | 0.6124 |
| **M5 mercado+Elo+features (operativo)** | **0.5708** | **0.5863** |
| M0 mercado no-vig | 0.5713 | 0.5869 |

Orden idéntico al esperado por la literatura (Kovalchik 2016; Wilkens 2021).
Calibración operativa OOF: pendiente 1.00, intercepto ≈0, ECE 0.7–0.9%.
Calibrador elegido por OOF: beta (mayoría de modelos).

## Validación 2023–2024 y test sellado 2025 (ΔLL modelo − mercado; negativo = mejor que el mercado)

| Circuito | Validación ΔLL [IC80] | Test 2025 ΔLL [IC80] | Test: pendiente / ECE |
|---|---|---|---|
| ATP | −0.0007 [−0.0011, +0.0003] | −0.00045 [−0.0019, +0.0001] | 0.959 / 0.016 |
| WTA | −0.0021 [−0.0037, −0.0009] | −0.00058 [−0.0027, +0.0009] | 0.955 / 0.022 |

**Conclusión honesta:** el modelo iguala al mercado y calibra bien, pero NO
demuestra ventaja sistemática sobre el precio no-vig histórico (la mejora WTA de
validación no persistió en test). Coincide con la conclusión central de la
auditoría: el value real solo puede aparecer contra cuotas concretas de casas
accesibles introducidas en el screener, y debe verificarse prospectivamente
(CLV) — no contra el histórico.

## Backtest modo A (stake plano 1u, `non_executable` — cuotas sin timestamp)

| Circuito | Periodo | Apuestas | PnL (u) | Yield | Nota |
|---|---|---|---|---|---|
| ATP | val 23–24 | 452 | +22.6 | +5.0% | dentro del ruido (IC ±9 pp) |
| WTA | val 23–24 | 114 | +31.4 | +27.5% | artefacto probable del libro único TD1 |
| ATP | test 25 | 201 | +13.1 | +6.5% | no significativo |
| WTA | test 25 | 37 | −4.7 | −12.6% | no significativo |

## Mercados de sets (experimentales)

El puente `m = s²(3−2s)` recalibrado GANÓ a los modelos directos en los 4
mercados (w_directo = 0 en la combinación OOF, ambos circuitos). Calibración
fuera de muestra (test 2025, pendiente/ECE):

| Mercado | ATP | WTA | Estado |
|---|---|---|---|
| Ganador 1er set | 1.16 / 3.0% | 0.96 / 2.7% | experimental |
| Gana ≥1 set (+1.5) | 1.07 / 1.5% | 0.97 / 2.8% | experimental (la mejor) |
| Gana 2‑0 (−1.5) | 1.02 / 2.1% | 0.94 / 2.4% | experimental |
| 3 sets (O/U 2.5) | 1.14 / 0.4% | 1.27 / 0.7% | experimental con AVISO (pendiente >1.1 sostenida) |

## Tests

61 tests automatizados, todos en verde: nombres/marcadores (9), integridad del
canónico real (7), Elo golden + anti-leakage (9), no-vig/puente (10), modelos y
simetría exacta (7), derivados (5), motor de value (10), settlement + E2E CLI (4).

## Registro

- Informes: `artifacts/reports/train_report.json`, `eval_val_*.json`, `eval_test_*.json`.
- Acceso al test sellado: `artifacts/reports/test_access_log.json` (1 acceso).
- Ledger del screener: `artifacts/ledger/screen_runs.jsonl`.
