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

## Prueba real A — sincronización automática de resultados (2026-08-04)

Ejecutada contra fuentes reales (`betbot sync-results --days 215`, sin
credenciales, solo los mirrors GitHub verificados):

| Métrica | Antes | Después |
|---|---|---|
| Frescura ATP | 2026-03-29 (mirror parado) | **2026-08-03 (D−1)** |
| Frescura WTA | 2025-10-12 (mirror muerto) | **2026-08-03 (D−1)** |
| Partidos incorporados | — | **2.829** (1.500+ ATP · 1.300+ WTA) |
| Duplicados omitidos (exactos + fecha±2d) | — | 3.518 + 154 |
| Debutantes dados de alta (sin ambigüedad) | — | 64 |
| En cuarentena (nombres ambiguos/erratas de la fuente) | — | 21 |
| Estado tras 2ª ejecución | — | 0 altas · 0 cambios de Elo (idempotente) |

Efecto sobre el escaneo real de la jornada (2026-08-04, 56 elegibles):

| Motivo de descarte | Antes del sync | Después |
|---|---|---|
| `ood_pocos_partidos_12m` | 23 | **7** |
| `ood_sin_actividad_reciente` | 11 | **1** |
| Señales reales emergidas | 0 | 2 normales (⭐) + 5 vigilar_precio |

El caso objetivo (WTA descartada en bloque por el mirror parado en oct-2025)
queda corregido: las jugadoras activas ya no computan como inactivas, y si una
fuente vuelve a atrasarse >14 días el sistema marca `data_freshness_unknown`
(aviso blando) en lugar de un falso OOD. La fila corrupta real de la fuente
WTA (Iasi con año 2029) fue detectada y rechazada con aviso, no corregida en
silencio.

## Prueba real B — mercados de sets con fuente estructurada

Verificado el 2026-08-04 (búsqueda exhaustiva documentada): **no existe ningún
feed público gratuito** que publique cuotas de mercados de sets de tenis; los
únicos proveedores reales son APIs oficiales con credencial (Betfair Exchange,
api-tennis, oddspapi, Sportradar Odds). Conforme al criterio de aceptación, el
proveedor oficial queda **totalmente integrado y testeado**: cliente Betfair
Exchange de SOLO lectura (`listEvents`/`listMarketCatalogue`/`listMarketBook`;
la ausencia de rutas de apuesta/saldo está garantizada por test), catálogo
dinámico por evento sin lista cerrada de marketType, mapeo verificado de
contratos (SET_WINNER set 1 → primer set; WIN_A_SET Yes → +1.5 / No → 2-0 del
rival; SET_BETTING X 2-0 → sets corridos; NUMBER_OF_SETS 3 → three_sets),
orientación correcta aunque el evento liste a los jugadores invertidos,
suspensión y `not_offered` distinguidos de error, y metadatos completos de
cada precio en `artifacts/ledger/structured_prices.jsonl`. El e2e con las
formas reales de la API (fixtures) ejecuta el motor de valor sobre moneyline +
primer set con cuota real de exchange. **Única pieza que falta: credenciales**
(`BETFAIR_APP_KEY` + usuario/contraseña de betfair.es; la Delayed App Key es
gratuita). Los endpoints de Betfair están bloqueados por la red del entorno de
desarrollo, no por el código.

## Tests

122 tests automatizados en verde (+1 saltado sin GUI): nombres/marcadores/
resolución de identidad, canónico real, Elo golden + anti-leakage, no-vig y
puente de sets, modelos y simetría exacta, derivados, motor de value,
settlement + E2E CLI, importación manual, scan/watch, **sync de resultados
(ventana, dedupe, futuro, cuarentena/debutantes, idempotencia, frescura vs
falso OOD)** y **cuotas estructuradas (mapeo dinámico, not_offered, solo
lectura Betfair, orientación, e2e moneyline+sets)**.

## Registro

- Informes: `artifacts/reports/train_report.json`, `eval_val_*.json`, `eval_test_*.json`.
- Acceso al test sellado: `artifacts/reports/test_access_log.json` (1 acceso).
- Ledger del screener: `artifacts/ledger/screen_runs.jsonl`.
