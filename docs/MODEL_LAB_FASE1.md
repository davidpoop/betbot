# MODEL LAB — FASE 1: auditoría del champion, inventario de datos y laboratorio mínimo

Fecha: 2026-08-09 · El modelo actual queda **CONGELADO como CHAMPION**
(bundle `artifacts/model_bundle.joblib`, entrenado 2026-08-03, git `c1391da`).
Nada de esta fase toca producción: `betbot scan` no importa `model_lab` (test
automático lo verifica) y ningún threshold, calibración ni lógica de value ha
cambiado.

---

## A. Auditoría exacta del modelo actual

### A.1 Modelos existentes

| Modelo | Algoritmo | Features | Rol |
|---|---|---|---|
| M0_market_novig | — (no es modelo) | p no-vig proporcional de la 1ª casa de `book_priority` | referencia OOF |
| M1_rank | LogisticRegression(C=1.0, lbfgs, max_iter=2000) simétrica | `log_rank_ratio` | baseline |
| M2_elo | ídem | `elo_diff` | baseline |
| M3_elo_surf | ídem | `elo_diff, elo_surf_diff` | baseline |
| M4_full | ídem | las 18 features v1 | **producción sin mercado** |
| M5_market | ídem | 18 + `market_logit` (entrenado solo en filas con mercado) | **producción con mercado** |

Todos envueltos en `SymmetricModel` (predictor.py:34-55): augmentación con
bandos intercambiados en fit y `p = 0.5·(f(x) + 1 − f(swap(x)))` en predict
⇒ `p(A,B) = 1 − p(B,A)` exacto. Sin escalado, sin interacciones, sin tuning de
C (1.0 fijo, default de sklearn).

**p_model de producción** (screener/run.py:317-326): `p4 = cal_M4(M4(x))`;
si hay cuota bilateral de moneyline, `p5 = cal_M5(M5(x))` y `p_match = p5`
(sigma = 0.02 + |p5−p4|/2); sin mercado `p_match = p4` (sigma = 0.04).

### A.2 Features exactas (18 + mercado), transformaciones e imputaciones

DIFF (antisimétricas): `elo_diff`/100 · `elo_surf_diff`/100 ·
`log_rank_ratio`=log(rank_b/rank_a) · `log_pts_ratio`=log1p(pts_a)−log1p(pts_b) ·
`rest_diff`/7 (rest cap 30d) · `m14_diff` · `m12m_diff`/10 · `layoff_diff` (≥90d) ·
`retired_recent_diff` (≤30d) · `age_diff`/5 · `lefty_diff` · `experience_diff`
=log1p(n_prev_a)−log1p(n_prev_b).
CTX: `best_of5`, `surface_clay/grass/carpet` (Hard base), `indoor`,
`round_level`/7. Mercado: `market_logit` = logit(p_novig prop).

Imputaciones en entrenamiento (builder.py:103-129): rank ausente → **500.0
sin indicador de ausencia**; pts → 0.0; age_diff → 0.0 si falta cualquier dob;
sin partido previo → rest=30; sin mercado → market_logit=0. En inferencia el
screener difiere levemente: rank ausente → log_rank_ratio=0 directo (no 500) y
experience usa `elo_state.n`. Divergencia train/serve documentada, leve.

### A.3 Elo, rankings, superficie, forma, OOD, calibración

- **Elo** (ratings/elo.py): K = 250/(n+5)^0.4 (estilo 538), inicial 1500, por
  tour, global + por superficie exacta, **sin decay temporal**. Replay
  cronológico (`sort_values(["date","match_id"])`); ratings PRE-partido
  escritos antes del update. Retiradas actualizan; walkovers no.
- **Rankings**: histórico point-in-time embebido por partido (rank_a/b del
  canonical, ~100% cobertura); en producción, CSV as-of con fallback Elo.
- **Superficie**: dummies + Elo por superficie; en scan, jerarquía
  official > tournament_registry > inferred.
- **Forma/actividad**: rest, m14, m12 (365d), layoff≥90d, retirada≤30d;
  estado leído ANTES de registrar el partido (as-of por construcción).
- **OOD**: reglas duras (no un modelo): stale≥540d, m12<5, jugador
  desconocido → descartada; `data_freshness_unknown` como aviso.
- **Calibración** (calibrate.py): identity/Platt/beta ajustados sobre
  predicciones **out-of-fold** del walk-forward y elegidos por log loss OOF.
  Bundle real: ATP beta en M1–M5; WTA platt en M3/M4, beta en el resto.

### A.4 Splits y métricas originales (verificadas cifra a cifra)

- Desarrollo ≤2022 con walk-forward anual expanding, OOF 2013–2022
  (mín. 2000 filas de train por fold). Validación 2023–2024 (elige shrink_w y
  revisa umbrales). **Test 2025 sellado** — un único acceso registrado
  (2026-08-03T04:39:58Z). El sellado es procedimental (log + aviso), no un
  bloqueo duro; el laboratorio añade `SealedTestGuard`.
- Partidos (completed+label): ATP dev 59.078 / OOF 23.796 / val 5.250 / test
  2.491; WTA dev 36.469 / OOF 22.333 / val 4.801 / test 2.158.

| Métrica (log loss) | ATP | WTA |
|---|---|---|
| OOF M4_full | 0.59943 | 0.61238 |
| OOF M5_market | 0.57077 | 0.58626 |
| OOF M0 mercado | 0.57130 | 0.58693 |
| Validación 23-24 (operativa) | 0.58294 | 0.58362 |
| Test 2025 (operativa) | 0.59169 | 0.59062 |
| Test 2025 mercado | 0.59214 | 0.59140 |

ΔLL vs mercado en test 2025: ATP −0.00045 (IC80 [−0.00186, +0.00008]),
WTA −0.00058 (IC80 [−0.0027, +0.00089]) — **el champion iguala al mercado, no
lo supera**. Esa es la vara real que cualquier challenger debe mover.

### A.5 EV conservador — hallazgo clave del mandato §8 CONFIRMADO

`EV_central = p_model·odds − 1`; `EV_cons = p_cons·odds − 1` con
`p_cons = w·p_model + (1−w)·p_novig` en mercados completos (engine.py:80,94-95).
**w del bundle: ATP=0.9, WTA=1.0** (elegidos por log loss en validación, no por
aversión al riesgo) ⇒ en WTA `EV_cons ≡ EV_central` SIEMPRE, y en ATP la
reducción es mínima (ej.: p=0.60, cuota 1.90/2.10 → EV 0.140 vs EV_cons 0.126).
No existe ninguna estimación formal de incertidumbre de p: la sigma heurística
(0.02+|p5−p4|/2) solo actúa en mercados **parciales**, que están capados a
"normal" y nunca llegan a TOP PICK "fuerte". La única protección real en los
mercados que sí llegan a picks es el gate de edge (≥0.02/0.03). El diseño
objetivo (p_central/p_lower/p_upper/p_conservative con abstención) queda
especificado en `model_lab/uncertainty.py` para FASE 2.

### A.6 Mapa del pipeline

```
RAW (mirrors tennis-data: mlt CSV, xlsx ATP 20-26, kaggle WTA 20-25; sackmann bio; manual 2026)
  → canonical/build.py  → data/canonical/matches.parquet (114.675) + players.parquet (3.040)
  → ratings/elo.replay  (Elo pre-partido por tour: global + superficie)
  → features/builder.build_features  (18 features + market_logit; as-of en una pasada)
  → models/train.py  (walk-forward OOF 2013-22 → M1..M5 por tour)
  → models/calibrate.select_calibrator  (identity/platt/beta por log loss OOF)
  → [incertidumbre: INEXISTENTE en mercados completos — solo sigma heurística en parciales]
  → p_match = cal(M5) con mercado | cal(M4) sin  (screener/run.py)
  → value/engine.evaluate_selection  (p_cons = shrink a mercado; EV, edge, estados)
  → picks.py  (opportunidades únicas, GRAVE_FLAGS, TOP PICKS ≤10)
```

### A.7 Columnas del canonical que el modelo NO usa hoy

`tournament`, `series` (nivel del torneo — ¡100% cobertura y sin usar!),
`games_a/b` (carga real por partido), `raw_name_*`, `source`; `sets_a/b` y
`set1_winner_a` solo como targets; de `odds_json`, solo la primera casa
(dispersión entre casas sin explotar); de players: `height` sin usar.

### A.8 Discrepancias documentación ↔ código (halladas por la auditoría)

1. `docs/AUDITORIA_SISTEMA_TENIS.md` §M8/§19 dice splits ≤2023/2024/2025; el
   código y la especificación usan ≤2022/2023-24/2025 (lo implementado siguió
   a la especificación).
2. La isotónica prometida en la auditoría vieja no se implementó en el
   champion (el laboratorio la añade como candidata de FASE 2), y la selección
   de calibrador es por log loss OOF, no "en validación" como decía el doc.
3. Elementos del plan §19 sin implementar: folds torneo-semana, evaluación
   estratificada obligatoria, sensibilidad de régimen (2020/Slams), ablations
   — exactamente lo que este Model Lab viene a cubrir.
4. `meta.data_hash` del bundle (`bf08ba4eb6bbcbf8`) ya no coincide con el
   `matches.parquet` actual (`245e2d643bc344ab`, reconstruido tras el train);
   los conteos por split siguen siendo idénticos, pero la verificación bit a
   bit por hash quedó rota — un `betbot train` la restauraría.
5. `train.py` lee `matches.parquet` directo (mirror, WTA hasta 2025-10-12);
   los 2.831 resultados de 2026 de `manual_results.parquet` solo entran al
   ESTADO del screener vía `load_matches`, no a un futuro reentrenamiento —
   coherente con el diseño congelado, pero conviene saberlo antes de FASE 2.

---

## B. Inventario de datos históricos realmente disponibles

- **canonical/matches.parquet**: 114.675 filas × 26 col, 2000-01-03→2026-03-29
  (ATP 70.067 desde 2000; WTA 44.608 desde 2007). Fuentes: mirrors de
  tennis-data.co.uk (mlt hasta 2019, xlsx ATP 2020-26, kaggle WTA 2020-25) +
  `manual_results.parquet` (2.831 filas de 2026 vía sync, **sin cuotas**).
- **Cobertura**: rank 99.4–100% en todos los tramos; pts ATP 0% en 2000-04;
  sets/games 100%; set1 98–99%; superficie/indoor/round/series 100%.
- **Cuotas**: una **única foto prematch por casa** (sin apertura/cierre ⇒ CLV
  no medible con estos datos). ≥2 casas: 97–100% en casi todo… **excepto WTA
  2020-2025: 0%** (solo la casa 'TD1' del daily pull). ATP 2019+: B365, PS,
  Max, Avg (+BFE en 2025-26). Agregados Max/Avg desde 2010.
- **Servicio/resto y minutos**: **NO existen en ninguna fuente del repo**
  (verificado columna a columna en mlt, kaggle, xlsx, sackmann, manual).
- **Huecos**: WTA sin partidos 2025-10-13→2026-01-01; ATP abril-agosto 2026
  solo vía manual (sin cuotas); kaggle ATP daily (hasta 2025-11-16) existe
  pero no se fusiona (solo reconciliación).
- **Rankings CSV**: una única publicación derivada (2026-08-03) — sin serie
  histórica; el rank point-in-time histórico vive por partido en el canonical.

## C. Matriz de cobertura de features avanzadas

Catálogo ejecutable en `model_lab/features_v2.py` (con `coverage_report()`).
Resumen de veredictos:

| Grupo | Factibles hoy (point-in-time) | No factibles / parciales |
|---|---|---|
| Ratings | Elo decay, incertidumbre (n efectivo), SOS, peso Elo-superficie por muestra | — |
| Forma | EWMA 30/60/90/365d, ajustada por rival, por superficie, momentum de Elo | — |
| Fatiga | partidos 3/7/14d, **sets y juegos** 7/14d, días consecutivos, profundidad torneo previo | minutos (sin datos) |
| Contexto | `series` (nivel, sin usar hoy), ronda mejor codificada, best_of, quali | indoor ya está; home country parcial (bio solo ATP); edad parcial (dob WTA baja) |
| Rankings | rank momentum entre partidos, **flag de ausencia** (hoy imputa 500 silenciosamente) | — |
| Matchup | H2H con shrinkage bayesiano fuerte, H2H superficie con n mínimo | — |
| Serve/return | — | **todo el bloque: sin datos en el repo** |
| Mercado | p_novig (ya, M5), dispersión entre casas **solo ATP** | dispersión WTA 2020+ (una casa) |

## D. Riesgos de leakage encontrados

Auditoría dedicada con verificación adversarial de cada sospecha (un segundo
agente intenta REFUTAR cada hallazgo con el código delante; §D.1).

**Verificado LIMPIO** (con evidencia file:line): Elo estrictamente pre-partido
(snapshot antes del update, elo.py:74-83); actividad as-of por construcción;
ranks de entrenamiento per-match de la fuente (ranking vigente en el partido);
calibradores ajustados solo sobre OOF ≤2022 y APLICADOS sin reajuste a
2023-25 (evaluación val/test genuinamente out-of-sample); sin ningún
scaler/imputador global (solo constantes fijas); una fila por partido con
orientación neutral (0 duplicados, verificado empíricamente); rankings_auto
jamás entra en entrenamiento y su loader es as-of estricto; test 2025 fuera de
todo ajuste (1 único acceso registrado). El laboratorio añade la **propiedad
de prefijo** como test automático permanente.

**Hallazgos que sobrevivieron** (por severidad):

- **L1 · orden intradía sin desempate por ronda (media — el único leak
  real)**: el replay ordena por (date, match_id) y dentro del mismo día el
  orden es alfabético, no por ronda; si la final se procesa antes que la
  semifinal, su update contamina el snapshot de ésta. Agravado en 2000-2002:
  tennis-data pone la fecha de INICIO del torneo a todos los partidos (medido:
  1.00 fechas distintas por torneo en 2000 vs 7.3 en 2003+), así que esos
  torneos enteros se procesan alfabéticamente. Filas afectadas medidas:
  6.386 (5,6% del total; 5.318 en 2000-02, solo-train), 446 en OOF 2013-22,
  97 en validación, **26 en el test 2025 (~0,2% — impacto marginal)**.
  FASE 2: desempate por ronda en el replay del laboratorio y sensibilidad
  excluyendo 2000-2002.
- **L2 · shrink_w in-sample para el backtest de VALIDACIÓN (media)**: w se
  elige en 2023-24 y el backtest por defecto reporta PnL sobre esos mismos
  años ⇒ el PnL de validación es optimista respecto a ese hiperparámetro. El
  test 2025 NO está afectado (w quedó fijado antes).
- **L3 · calibrador ajustado y evaluado sobre el mismo pool OOF (baja)**:
  las métricas "OOF" de train_report son levemente optimistas (2-3 grados de
  libertad); val/test honestos. El laboratorio ofrece `fit_in_fold`.
- **L4 · cuotas de cierre sin timestamp (baja, auto-declarado)**: market_logit
  y el backtest usan la misma foto de cierre ⇒ modo A = cota superior
  non_executable, exactamente como ya lo etiqueta producción.

**Descartados tras investigación**: L5 (rankings derivados con futuro en
entrenamiento — jamás entran) y L6 (features_all.parquet con 2.831 filas de
2026 que el train actual no produce — problema de trazabilidad del artefacto,
no de contaminación temporal; los fits filtran ≤2022).

### D.1 Veredictos de la verificación adversarial

| Hallazgo | Veredicto | Cómo se verificó |
|---|---|---|
| L1 orden intradía | **CONFIRMADO (media)** | Medición independiente reproducida: ATP 2000/01/02 = 1.00/1.06/1.05 fechas distintas por torneo vs 7.32+ desde 2003; ~1.500 filas/año con el mismo jugador 2+ veces el mismo día en 2000-02 vs ~80-140 después. Solo 26 filas en test 2025. |
| L2 shrink_w in-sample en validación | **CONFIRMADO, severidad baja** | Verificador adversarial independiente: cadena literal train.py:214-257 → backtest/run.py:76-138; rebajado a baja porque el test 2025 no lo hereda (w fijado con 2023-24) y solo infla el PnL reportado de validación. |
| L3 calibrador fit+report mismo OOF | **CONFIRMADO (baja)** | Lectura directa del código (train.py:132-146): mecanismo literal, 2-3 grados de libertad; val/test no afectados. |
| L4 cuotas de cierre sin timestamp | **CONFIRMADO (baja, auto-declarado)** | El propio backtest lo etiqueta non_executable; convención estándar del dataset. |
| L5 rankings derivados con futuro | **DESCARTADO** | Nunca entran en entrenamiento; loader as-of estricto. |
| L6 features_all con filas 2026 | **DESCARTADO como leak** | Problema de trazabilidad del artefacto (el train actual no lo reproduce), sin contaminación temporal: los fits filtran ≤2022. |

Ninguno de los cuatro confirmados invalida las métricas del test 2025 (impacto
conjunto ≤0,2% de filas y sin ajuste alguno sobre ese año). Para FASE 2 quedan
como reglas del laboratorio: desempate por ronda en el replay, sensibilidad
excluyendo 2000-2002, y PnL de validación siempre etiquetado como in-sample
respecto a shrink_w.

## E. Propuesta exacta de challengers

Registrados en `model_lab/challengers.py::CHALLENGER_SPECS`; solo C0 es
ejecutable en FASE 1.

- **C0 — champion actual** (baseline, reproducido: §J).
- **C1 — logística regularizada bien especificada**: misma simetría A/B, C
  elegido por fold interno en grid pequeño pre-registrado, sobre las features
  v2 factibles. Aísla el valor de las features nuevas.
- **C2 — gradient boosted trees**: `HistGradientBoostingClassifier` de sklearn
  (lightgbm/xgboost/catboost NO están instalados; no se añade dependencia sin
  necesidad demostrada). Simetría por augmentación swap; early stopping con
  cola temporal del train del fold.
- **C3 — rating + residual ML**: prior = p_Elo calibrada (con decay/superficie);
  el ML aprende solo el residuo contextual sobre el logit (offset). Favorito
  estructural: preserva la señal estable y limita el sobreajuste.
- **C4 — ensemble/stacking** de los mejores C1–C3, pesos por fold interno.
- **C5 (opcional) — bayesiano jerárquico**: solo con justificación empírica de
  fase 2 (p.ej. si C3 mejora específicamente en jugadores con pocas
  observaciones). Sin redes neuronales: el volumen (~110k filas etiquetadas)
  no las justifica frente a GBT bien regularizado.

Capas separadas (mandato §7): **SPORTS MODEL** = P(win | información de tenis)
(C1–C3 sin market_logit) y **MARKET/VALUE MODEL** aparte; un challenger con
mercado como feature (heredero de M5) se evalúa por separado y se compara
contra el modelo independiente, nunca se mezclan sus métricas.

## F. Diseño walk-forward (adaptado a los datos reales)

Implementado en `model_lab/validation.py`:

- **Interno (OOF)**: expanding anual, train < vy → valid vy, vy = 2013…2022
  (mismo esquema del champion ⇒ comparaciones justas contra C0; mínimo 2000
  filas de train por fold). Todo preprocess/calibración/hiperparámetro se
  ajusta DENTRO del fold (`run_fold` + `fit_in_fold`).
- **Externo**: dev ≤2022 → validación 2023–2024 (selección de challenger,
  calibrador final y cualquier peso) → **test 2025 sellado** (un único run
  final por challenger promovible, vía SealedTestGuard con motivo registrado)
  → 2026 como shadow/paper si aplica.
- Métricas por fold Y agregadas; una mejora debe aparecer en la mayoría de
  folds, no en uno (§11). IC bootstrap por bloques semanales
  (`metrics.block_bootstrap_diff`).
- 2020 (COVID, n≈½) se reporta por separado en los desgloses; no se excluye.

## G. Features factibles hoy (lista corta para C1–C3)

Elo con decay + incertidumbre por n efectivo; forma EWMA multi-ventana ajustada
por rival y por superficie; carga real (sets/juegos 7/14d, días consecutivos,
profundidad del torneo previo); `series` como nivel del torneo; rank momentum +
flag de ausencia de ranking; H2H shrunk global y por superficie. Todas
reconstruibles point-in-time del canonical actual (§C) y verificables con la
propiedad de prefijo del laboratorio.

## H. Datos adicionales para mejorar sustancialmente

1. **Sackmann per-match** (ATP/WTA hasta temporada previa): minutos + serve/
   return completos (aces, DF, svpt, break points) → bloque entero de features
   nuevas + fatiga real por minutos. Licencia CC BY-NC-SA (uso local no
   comercial OK); pipeline de identidades ya existe para bio.
2. **Segunda casa WTA 2020+** (o histórico multi-casa): sin ella no hay
   dispersión de mercado ni no-vig robusto WTA reciente.
3. **Cuotas con timestamp (apertura/cierre)**: imprescindible para CLV y para
   backtests ejecutables; The Odds API (ya integrada para calendario) las da
   hacia delante — acumularlas desde ya crearía el histórico que hoy no existe.
4. **Ranking oficial semanal histórico** (opcional): el rank por partido ya
   cubre el 99%+; solo aportaría puntos exactos pre-2006 ATP.

## I. Model Lab implementado (mínimo, separado de producción)

```
src/betbot/model_lab/
    dataset.py       # dataset point-in-time + hash + PROPIEDAD DE PREFIJO
    features_v2.py   # catálogo v2 con factibilidad medida + coverage_report()
    validation.py    # folds walk-forward, run_fold, SealedTestGuard
    metrics.py       # logloss/brier/auc/acc, slope/intercept, ECE, reliability,
                     # sharpness, subgrupos, bootstrap por bloques semanales
    calibration.py   # identity/platt/beta (champion) + isotónica (lab), fit_in_fold
    challengers.py   # C0 ejecutable + registro C1–C5
    uncertainty.py   # auditoría del EV_cons actual + contrato p_lower/upper (FASE 2)
    backtest.py      # reglas congeladas con hash; ejecución = FASE 2
    reports.py       # python -m betbot.model_lab.reports [dataset|baseline]
```

Artefactos propios en `artifacts/model_lab/`. Producción intacta (test
`test_lab_does_not_touch_production_scan`).

## J. Baseline champion reproducido — ACREDITADO

`python -m betbot.model_lab.reports baseline` reconstruye el dataset, repite el
walk-forward OOF con las clases de producción y compara contra
`artifacts/reports/train_report.json`:

**max_abs_delta = 0.0** — todas las métricas (log loss, Brier, accuracy, n,
calibrador seleccionado, p_match operativa con slope/intercepto/ECE) coinciden
EXACTAMENTE en ATP y WTA, modelo a modelo. El laboratorio mide lo mismo que
producción; cualquier diferencia futura de un challenger será atribuible al
challenger, no al arnés. (`artifacts/model_lab/baseline_champion.json`.)

---

## §11. Criterio de promoción (pre-registrado; sin promoción automática)

Un challenger solo se PROPONE como nuevo champion si TODAS:
1. mejora log loss/Brier/calibración out-of-sample de forma consistente;
2. la mejora aparece en la mayoría de folds walk-forward, no en uno;
3. supera los tests de leakage del laboratorio (propiedad de prefijo incluida);
4. no empeora brutalmente subgrupos relevantes (tour/superficie/fav-dog/tramo);
5. muestra suficiente (miles de partidos, no cientos);
6. el beneficio persiste con costes/margen (evaluación con no-vig);
7. el IC bootstrap por bloques de la diferencia excluye el cero.

Si ninguno cumple: **SE CONSERVA EL CHAMPION** — resultado válido.
La decisión final es siempre del usuario, nunca del laboratorio.

## Comandos (Mac)

```bash
cd ~/BetBot-final && source .venv/bin/activate
pytest -q                                        # suite completa (incluye lab)
python -m betbot.model_lab.reports dataset       # ficha del dataset + cobertura
python -m betbot.model_lab.reports baseline      # reproducción champion (≈5-10 min)
```

**FASE 2 (tras revisar esta auditoría, NO antes)**: implementar features v2
factibles (G) con tests de prefijo, entrenar C1–C4 en el walk-forward, comparar
con §5/§6, ablations, incertidumbre real (§8) y backtest con reglas congeladas.
