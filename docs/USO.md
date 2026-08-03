# betbot — uso del MVP

Screener cuantitativo local de tenis (ATP/WTA, prepartido). **No ejecuta apuestas,
no scrapea operadores, no depende de cuentas de apuestas.** Las cuotas del día
las introduces tú; el sistema calcula probabilidades calibradas, valor esperado
y estados de candidata.

## Instalación

```bash
pip install -e .          # requiere Python 3.11+
```

## Flujo completo (5 comandos)

```bash
# 1) Descargar datos reales (mirrors públicos; ~40 MB; manifest con SHA256)
betbot download-data

# 2) Construir el dataset canónico (114k+ partidos 2000–2026)
betbot prepare-data

# 3) Entrenar: Elo, features event-time, baselines M0–M5 walk-forward,
#    calibración OOF, mercados de sets (puente + directos + combo)  [~2 min]
betbot train

# 4) Evaluación temporal en VALIDACIÓN (2023–2024)
betbot evaluate
#    El año de TEST (2025) está SELLADO; consúltalo UNA sola vez al final:
#    betbot evaluate --test        (cada acceso queda registrado)

# 5) Screener diario
betbot template --out mi_dia          # escribe day_matches.csv y day_odds.csv
#    ... rellena ambos ficheros a mano (o exporta de tu fuente) ...
betbot screen --matches mi_dia/day_matches.csv --odds mi_dia/day_odds.csv --out resultado.csv
```

`betbot screen` sin `--odds` también funciona: muestra las probabilidades del
modelo y marca los favoritos como `probable_sin_value` (estado informativo).

## Plantillas

- `day_matches.csv`: `date,tour,tournament,surface,indoor,round,best_of,player_a,player_b`.
  Nombres en formato Tennis-Data (`"Alcaraz C."`, `"Swiatek I."`). Si un nombre
  no se reconoce, la fila sale `descartada` con sugerencias de nombres próximos.
- `day_odds.csv`: `player_a,player_b,market,selection,odds,bookmaker,timestamp`.
  Mercados: `match_winner`, `set1_winner`, `wins_set` (gana ≥1 set / +1.5 sets),
  `straight_sets` (gana 2‑0 / −1.5 sets), `three_sets` (más/menos 2,5 sets).
  `selection`: `a`/`b` según TU columna `player_a`/`player_b` (`yes`/`no` en
  `three_sets`). Las líneas con `#` se ignoran.

## Estados de la tabla

| Estado | Significado |
|---|---|
| `fuerte` | Mercado principal, EV_cons ≥ 5%, edge ≥ 3 pp, sin avisos |
| `normal` | EV_cons ≥ 3% y edge ≥ 2 pp |
| `experimental` | Mercado de sets con umbrales cumplidos (capado hasta validar su historial) |
| `vigilar_precio` | EV positivo pero cuota < `o_min`; se indica la cuota mínima y a qué precio entra |
| `probable_sin_value` | p_cons ≥ 60% pero sin value (favorito caro) |
| `sin_value` | Sin ventaja a esa cuota |
| `descartada` | Filtro duro con reason code (jugador desconocido, OOD, cuota caducada, Bo5 en mercado de sets…) |

`recommended_primary`: como máximo UNA selección por partido (mejor estado y,
a igualdad, mayor EV conservador). **Cero candidatas es una salida normal.**

Mercados **completos vs parciales**: el no-vig y el edge solo se calculan si
introduces la cuota del complemento real (en `straight_sets(a)` el complemento
es `wins_set(b)`, no `straight_sets(b)`); si falta, el sistema NO inventa la
probabilidad de mercado y aplica el descuento conservador `p − z·σ`.

## Reglas fijas del MVP (pre-registradas en config/default.yaml)

- Umbrales: EV_cons ≥ 3% (normal), ≥ 5% + edge ≥ 3 pp (fuerte); rango suave de
  cuota 1.40–4.00; cuotas caducan a las 6 h; OOD por debut/inactividad/lesión larga.
- División temporal: entrenamiento ≤ 2022 · validación 2023–2024 · test 2025
  sellado · 2026 = prospectivo.
- Backtest histórico = **modo A, `non_executable`**: las cuotas históricas de
  Tennis-Data no tienen timestamp; su PnL es cota superior para comparar
  modelos, nunca rentabilidad esperada.
- Retiradas: excluidas del target de entrenamiento; settlement configurable
  (`1_set` estilo Pinnacle/Betfair por defecto; `match_completed` estilo bet365).

## Datos y licencias

`data/raw/manifest.json` registra URL, SHA256 y licencia de cada fuente:
Tennis-Data (mirror MLT 2000–2019 + XLSX anuales 2020–2026), derivado daily
(WTA 2020–2025, cuota única), y `atp_players.csv` de Jeff Sackmann
(**CC BY-NC-SA 4.0: uso no comercial**, atribución requerida). Mientras el
proyecto use esa fuente, es no comercial.

Limitaciones conocidas de la v1: ranking oficial "del día" no disponible en el
screener (el Elo propio lleva la señal); WTA 2020–2025 con una sola casa;
estado de jugadores congelado en la última fecha del dataset canónico
(re-ejecuta `prepare-data` + `train` para refrescarlo); WTA sin biografía
(mano/edad) por falta de fuente con licencia clara.

## Ledger y reproducibilidad

Cada ejecución del screener se anota en `artifacts/ledger/screen_runs.jsonl`
(append-only) con: timestamp, hash del dataset, SHA de git del modelo, umbrales
y todas las filas (aceptadas y rechazadas). Los informes de entrenamiento y
evaluación quedan en `artifacts/reports/`.
