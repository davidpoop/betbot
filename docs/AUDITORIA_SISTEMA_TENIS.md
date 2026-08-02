# Auditoría técnica, científica y estratégica del sistema de apuestas de tenis

**Rol:** arquitecto cuantitativo principal.
**Objeto:** auditoría del documento «Diseño científico y técnico de un sistema propio para detectar apuestas de tenis con valor esperado positivo» (32 págs.) y propuesta de visión propia.
**Fecha:** 2026-08-02.
**Estado:** entregable de auditoría. No se ha escrito código, no se ha creado la aplicación, no se ha entrenado ningún modelo.

**Convenciones usadas en todo el informe:**

- **[EVIDENCIA]** — afirmación respaldada por fuentes verificadas (se citan en §30).
- **[INFERENCIA]** — deducción razonada a partir de evidencia, no verificada directamente.
- **[DECISIÓN DE DISEÑO]** — elección de ingeniería defendible entre varias válidas.
- **[HIPÓTESIS]** — pendiente de probar con datos propios; el sistema debe poder refutarla.
- **Confianza:** Alta / Media / Baja, indicada por recomendación.
- Cuando no he podido verificar algo por falta de acceso o porque la fuente no lo publica, lo digo explícitamente.

---

## 1. Resumen ejecutivo

El documento auditado es **notablemente bueno** en su núcleo metodológico: separa probabilidad, selección y staking; impone un modelo de datos event-time con `effective_at`/`observed_at`; exige baselines de mercado y Elo antes de cualquier modelo complejo; sella un test final; trata la abstención como salida normal; y prohíbe la ejecución automática. Ese núcleo coincide con la práctica de sistemas cuantitativos serios y **lo mantengo casi íntegro**.

Mis discrepancias principales no son estadísticas sino **económicas y operativas**:

1. **El documento subestima el problema del precio obtenible en España.** Todo el edge que la literatura considera plausible en tenis es pequeño (1–4 pp sobre el precio no-vig de un operador sharp). Un residente en España opera principalmente con casas DGOJ (bet365.es, Winamax.es, etc.) con márgenes superiores y política activa de limitación de ganadores; el exchange de Betfair y Pinnacle no son legalmente accesibles desde España, y la vía Kalshi/Polymarket es incierta y de liquidez dudosa para tenis. Esto no invalida el proyecto, pero **cambia su función objetivo**: el sistema debe medir el EV contra el mejor precio realmente accesible, usar el cierre sharp solo como diagnóstico (CLV), y tratar la "monetizabilidad en España" como un gate explícito del roadmap, no como un supuesto.
2. **El documento contiene una contradicción interna sobre Tennis-Data**: exige timestamps en todas las cuotas y a la vez basa el backtest inicial en un dataset sin timestamps. Se resuelve con un backtester de dos modos (benchmark de calidad de modelo vs. simulación de ejecución), que defino en §20.
3. **El motor jerárquico punto→juego→set→partido debe construirse antes de lo que propone el documento**, no para apostar en mercados derivados, sino como librería de pricing y verificación de consistencia. Es barato, testeable con fórmulas cerradas y es la llave de la fase 2.
4. **La incertidumbre del modelo necesita un método concreto**, no solo la fórmula `p_robusto = p̂ − z·σ`. Propongo estimarla por desacuerdo de ensemble + bootstrap temporal, y usar como estimador conservador la contracción hacia el precio de mercado (shrinkage), con la resta de σ como segunda variante reportada.
5. **El plan de captura de cuotas prospectivas no puede quedar como "decisión abierta"**: sin snapshots propios con timestamp no hay CLV medible ni obtainability, y por tanto no hay gate de paso a dinero real. Es la primera inversión del proyecto (en tiempo o en presupuesto).

**Hallazgos de la verificación externa (2026-08-02) que alteran materialmente el documento:**

- **Los repositorios de Jeff Sackmann `tennis_atp`, `tennis_wta` y `tennis_slam_pointbypoint` han desaparecido de GitHub** (404 verificado hoy vía API; solo sobrevive `tennis_MatchChartingProject`, último push 2026-05-25). El motivo no está confirmado. Existen mirrors con datos hasta ~2024 y licencia heredada CC BY-NC-SA (no comercial). El "stack gratuito" del documento está parcialmente roto y el riesgo de discontinuidad que el propio documento señalaba **se ha materializado**. [EVIDENCIA]
- **Kalshi y Polymarket están bloqueados en España desde el 26-05-2026** (expediente sancionador y orden cautelar de bloqueo de la DGOJ, nota oficial del Ministerio). Quedan fuera del perímetro operativo de este proyecto — no por prudencia, por regulación. [EVIDENCIA]
- **Betfair.es sí ofrece exchange con licencia DGOJ** (modalidad "apuestas cruzadas", Orden HAP/1369/2014; comisión 2% sobre ganancias netas; API oficial con Delayed App Key gratuita y Live App Key con cuota única ~£499). Es un pool separado del internacional (betfair.com bloquea IPs españolas) con liquidez muy inferior, pero es la única vía legal de exchange desde España y **el documento no la analiza**. [EVIDENCIA]
- El cierre de la API pública de Pinnacle (23-07-2025) queda verificado; además Pinnacle no acepta clientes residentes en España. [EVIDENCIA]
- TML-Database (el sustituto de Sackmann que propone el documento) lleva sin commits desde 2026-01-27, es solo ATP y su base legal es difusa. No es una columna vertebral fiable. [EVIDENCIA]
- Tennis-Data sigue activo pero **solo contiene cuotas de moneyline** y el instante de captura (apertura/cierre) no está documentado. [EVIDENCIA]

**Recomendación global (confianza Alta):** ejecutar el MVP moneyline ATP+WTA propuesto por el documento, con las cinco correcciones anteriores, un protocolo de validación reforzado (§19) y gates económicos añadidos (§26). No construir todavía mercados derivados, redes neuronales, live betting ni comparativas con tipsters. El éxito de la fase 1 se define como: predicciones reconstruibles, calibradas, con CLV positivo frente a un cierre comparable y EV positivo conservador frente a precios obtenibles — no como ROI retrospectivo.

**Probabilidad honesta de éxito comercial** [INFERENCIA, confianza Media]: incluso ejecutando todo correctamente, la probabilidad de que este sistema produzca beneficio sostenido y escalable desde España es minoritaria (el mercado principal de tenis es eficiente en cierre; las casas accesibles limitan a ganadores). El valor esperado del proyecto se reparte entre: aprendizaje metodológico reutilizable (alto y casi seguro), detección de nichos explotables (posible), y beneficio sostenido (incierto). El sistema debe diseñarse para descubrir rápido y barato en cuál de esos escenarios estamos.

---

## 2. Qué partes del documento original mantendría

| # | Elemento | Juicio | Confianza |
|---|----------|--------|-----------|
| M1 | MVP = screener prepartido moneyline, individuales ATP+WTA main tour; sin Challenger/ITF, sin live, sin ejecución automática | Mantener | Alta |
| M2 | Separación estricta probabilidad / selección / staking; backtest principal con stake plano 1u | Mantener | Alta |
| M3 | Target = resultado binario del partido optimizado con proper scoring rules (log loss primaria, Brier secundaria) | Mantener | Alta |
| M4 | Modelo de datos event-time: `event_time`, `effective_at`, `observed_at`, `ingested_at`, `prediction_time`; as-of joins | Mantener; es el activo principal del proyecto | Alta |
| M5 | Jerarquía de baselines: mercado no-vig → ranking → Elo → Elo superficie → logística → **mercado+Elo** → saque/resto | Mantener íntegra | Alta |
| M6 | Modelos separados ATP/WTA con código y esquema comunes; superficie como feature/Elo específico, no como modelos separados | Mantener | Media-Alta |
| M7 | Calibración con predicciones out-of-fold, calibrador por circuito; Platt/beta/isotónica como candidatos | Mantener (matiz en §18) | Alta |
| M8 | División temporal: desarrollo ≤2023, validación 2024, test sellado 2025, shadow/prospectivo 2026; walk-forward expanding | Mantener (añadidos en §19) | Alta |
| M9 | Tabla de fugas (leakage) y tests automatizables, incluido el placebo leakage test | Mantener; es de lo mejor del documento | Alta |
| M10 | Walkovers fuera del target; retiradas con indicador separado y análisis de sensibilidad | Mantener | Alta |
| M11 | Abstención como salida normal ("cero picks es un resultado deseable"); guardar candidatos rechazados con motivo | Mantener | Alta |
| M12 | Realismo muestral: ~38k apuestas para CI del 95% con semiancho 1%; 50–100 apuestas no prueban nada (aritmética verificada por mí: SE(ROI)≈1/√N a cuota ~2) | Mantener | Alta |
| M13 | Distinción accuracy / calibración / proper scores / ROI / CLV; no seleccionar modelos por accuracy ni ROI retrospectivo | Mantener | Alta |
| M14 | Kill switches operativos y condiciones de escala; microstakes antes de stakes relevantes; Kelly fraccional solo tras validación | Mantener | Alta |
| M15 | Arquitectura local-first: Python, Parquet inmutable, DuckDB, Postgres para ledger, Pydantic, scikit-learn, dashboard mínimo al final | Mantener | Media-Alta |
| M16 | Resolución de identidades con `player_id` interno + tabla de aliases + cuarentena de matches ambiguos | Mantener | Alta |
| M17 | Prohibición de scraping/automatización contra bet365/Winamax y de VPN/evasión geográfica; registro manual del precio visto y ejecutado | Mantener | Alta |
| M18 | Reproducibilidad total de cada predicción (commit, hashes de datos/modelo/calibrador, snapshot de odds, versión de reglas, seed) | Mantener | Alta |
| M19 | Guardar simultáneamente probabilidad proporcional, power y Shin con parámetros y divergencias | Mantener | Alta |
| M20 | Errores amateur y su prevención (tabla final del documento) | Mantener como checklist de PR/CI | Alta |

---

## 3. Qué partes modificaría

| # | Elemento del documento | Modificación propuesta | Motivo | Confianza |
|---|------------------------|------------------------|--------|-----------|
| C1 | El precio obtenible se trata como un detalle de implementación ("decisión abierta: operador y presupuesto") | Elevarlo a **restricción de primer orden**: el EV operativo se calcula contra el mejor precio accesible desde España — casas DGOJ (bet365.es, Winamax.es, etc., margen ~5–8% y limitación activa de ganadores documentada) **y el exchange de Betfair.es** (comisión 2%, liquidez limitada, API oficial). El cierre sharp queda solo como referencia diagnóstica de CLV. Añadir gate económico explícito (§26, G5) | El edge plausible (1–4 pp) es del orden del sobrecoste de margen de las casas accesibles; sin esta restricción el backtest mide una rentabilidad que el usuario no puede capturar. Betfair.es es además la única vía con API de ejecución legal (consulta de precios; la colocación sigue siendo decisión manual) | Alta |
| C2 | Backtest inicial sobre Tennis-Data, y a la vez regla "rechazar odds sin timestamp" | **Backtester de dos modos** (§20): modo A "benchmark" (Tennis-Data, sin pretensión de ejecutabilidad, para comparar modelos y calibración) y modo B "ejecución" (solo snapshots propios o históricos con timestamp; único modo del que se derivan decisiones de dinero) | Resuelve la contradicción interna sin renunciar a 20+ años de datos | Alta |
| C3 | Motor Markov pospuesto a versión dos | Construir en fase temprana la **librería de pricing jerárquico** (punto→juego→set→partido, fórmulas cerradas) como módulo puro con property-based tests; usarla para consistencia interna y sanity checks del modelo saque/resto. Apostar en derivados sigue pospuesto | Coste bajo (fórmulas conocidas), gran valor de test y opcionalidad para fase 2 | Alta |
| C4 | `p_robusto = clip(p̂ − z·σ_total)` con σ_total sin método | Definir σ_epistémica = f(desacuerdo entre modelos del ensemble, ancho bootstrap temporal, error de calibración por bin, flags de datos). Estimador conservador **primario**: shrinkage hacia el mercado `p_cons = w·p̂ + (1−w)·p_novig` con w fijado en validación; la resta de z·σ se mantiene como variante secundaria reportada | El shrinkage hacia el precio tiene mejor fundamento (posterior que combina modelo y mercado) y penaliza de forma natural el desacuerdo extremo | Media-Alta |
| C5 | Power como método no-vig operativo por defecto | Decisión **empírica por operador y rango de cuota** en validación; mantener las tres variantes almacenadas. La evidencia comparada favorece ligeramente a Shin en precisión de probabilidades, con diferencias pequeñas en mercados binarios de margen bajo | No fijar en el diseño lo que puede decidirse con datos | Media |
| C6 | Versión dos = "Challenger moneyline; después hándicap/totales" | Reordenar: fase 2 = **mercados derivados de sets en main tour** (ganador 1er set, hándicap de sets, gana al menos un set) desde el motor jerárquico; Challenger queda en fase 3 condicionado a datos | Challenger añade peores datos, menos liquidez y límites de stake más bajos; los derivados de sets reutilizan el generador ya validado y los marcadores históricos son abundantes | Media |
| C7 | Snapshots de cuotas propios: "si es posible" | Obligatorio desde el inicio del paper trading: scheduler de snapshots multi-horizonte (T-24h, T-6h, T-1h, cierre) desde fuentes con API autorizada + registro manual de bet365/Winamax en el momento de decisión | Sin ello no existen CLV, obtainability ni slippage medibles; es el cuello de botella del gate a dinero real | Alta |
| C8 | Régimen temporal tratado como homogéneo 2010–2026 | Añadir manejo explícito de regímenes: flag y sensibilidad para 2020 (COVID), cambios de formato (super tie-break en dobles no aplica, pero sí Slams con formatos de 5.º set cambiantes 2019–2022), Davis Cup/United Cup/exhibiciones fuera del universo, Bo3 vs Bo5 como feature dura | Mezclar regímenes infla varianza y puede sesgar Elo/calibración | Alta |
| C9 | Criterio de aceptación de CLV "positivo tras spread/fee ≥500 apuestas" sin definir referencia | Definir **dos CLV**: (a) vs cierre del mejor precio accesible (métrica económica), (b) vs cierre sharp de referencia si hay acceso a él (métrica de habilidad). No mezclarlas jamás en una sola serie | Miden cosas distintas; el documento ya intuye el problema pero no lo cierra | Alta |
| C10 | Filtros iniciales (EV≥3%, edge≥2pp, cuota 1.40–4.00) presentados como política razonable | Mantener como **hiperparámetros pre-registrados con fecha de revisión**, y añadir análisis de sensibilidad de thresholds en validación (curva EV-threshold → volumen) antes de congelarlos | Son números plausibles pero arbitrarios; el pre-registro evita optimizarlos sobre resultados | Alta |
| C11 | Comparativa TemisBet/TIPERO/Dimers/VSiN como sección del sistema | Degradar a **experimento opcional post-MVP** con su protocolo (que es bueno); no consume recursos de las fases 0–2 | Coste de oportunidad; no reduce ningún riesgo del sistema propio | Media-Alta |
| C12 | Roadmap sin criterio de abandono global | Añadir **kill criteria del proyecto** (§26): condiciones bajo las cuales se declara que no hay edge monetizable y el sistema pasa a modo investigación/hobby sin dinero real | Un sistema profesional también define cuándo parar | Alta |

---

## 4. Qué partes rechazaría

| # | Elemento | Motivo del rechazo | Confianza |
|---|----------|--------------------|-----------|
| R1 | Kalshi/Polymarket como fuentes operativas de precio del MVP para un residente en España | **Rechazado con evidencia:** ambos están bloqueados en España por orden cautelar de la DGOJ (26-05-2026, expediente sancionador; bloqueo por ISPs). Operar en ellos desde España significaría usar plataformas no autorizadas — incompatible con el marco legal del proyecto. Además, incluso donde son legales, sus fees por contrato (Kalshi ≈ 0,07·P·(1−P); Polymarket deportes ≈ 0,05·P·(1−P) taker desde jul-2026) y la liquidez fina fuera de Slams los hacían dudosos como referencia de precio | Alta |
| R2 | "Microstakes y automatización parcial" como paquete de la versión dos | La automatización de colocación (aunque sea parcial) contra operadores DGOJ viola sus términos; toda ejecución es manual en todas las fases. La única automatización aceptable es la de captura de datos autorizada y la de generación de recomendaciones | Alta |
| R3 | Objetivo implícito de "800 apuestas/año con ROI de dos dígitos" sugerido por las comparativas con tipsters | Incompatible con la aritmética muestral del propio documento; el sistema no debe heredar KPIs de marketing de terceros | Alta |
| R4 | Uso de The Odds API free tier como plan de snapshots del MVP | Un free tier con créditos limitados no soporta snapshots multi-horizonte de un calendario completo de tenis; presupuestar o reducir universo. (Detalle de planes verificado en §15/§30; los precios exactos, si no publicados, se marcan como no verificados) | Media |
| R5 | Cualquier interpretación del documento que trate el backtest sobre Tennis-Data como estimación de rentabilidad ejecutable | Sin timestamp ni libro identificable con precisión de instante, ese ROI es una cota superior optimista; solo sirve para comparar modelos entre sí | Alta |

---

## 5. Supuestos no demostrados del documento (y míos)

Cada uno queda registrado como hipótesis falsable con su test asociado:

| # | Supuesto | Estado | Test que lo decide |
|---|----------|--------|--------------------|
| S1 | Existe información incremental capturable sobre "mercado + Elo superficie" con datos públicos | [HIPÓTESIS] central del proyecto | Gate G3: mejora de log loss/Brier fuera de muestra con bootstrap emparejado |
| S2 | Un edge estadístico frente al cierre sharp se traduce en EV positivo a precios accesibles DGOJ | [HIPÓTESIS] — el documento la da por supuesta | Paper trading con doble CLV (C9) y obtainability real |
| S3 | 500 apuestas / 6 meses bastan para decidir el paso a microstakes | [HIPÓTESIS] optimista: con 500 apuestas el CI de ROI a cuota ~2 es ±8.8 pp; solo detecta edges grandes. Válido como gate de *no-rechazo* (CLV, calibración), no como prueba de rentabilidad | Reformular el gate: CLV + calibración + obtainability, no ROI |
| S4 | Beta calibration conserva ventajas con muestras de este tamaño | [HIPÓTESIS] razonable | Comparación Platt/beta/isotónica en OOF, por circuito |
| S5 | Las diferencias proporcional/power/Shin importan en tenis con margen bajo | [HIPÓTESIS]; la evidencia sugiere diferencias pequeñas cerca del 50% y mayores en colas | Análisis por operador y rango de cuota en validación |
| S6 | La calidad de datos WTA soporta el mismo esquema de features que ATP (saque/resto histórico) | [HIPÓTESIS]; cobertura de estadísticas WTA históricamente peor | Auditoría de completitud por año/circuito en fase 0 |
| S7 | Los modelos separados ATP/WTA superan a un modelo conjunto con feature de circuito | [HIPÓTESIS] plausible | Ablation en validación |
| S8 | El pipeline de snapshots propios es sostenible en coste y ToS durante ≥12 meses | [HIPÓTESIS] operativa | Fase 0: prueba de 2 semanas de captura + revisión de términos |
| S9 | Las retiradas pueden excluirse del target sin sesgar el modelo (los partidos con retirada no son aleatorios: correlacionan con favoritos/lesiones) | [HIPÓTESIS]; el documento propone sensibilidad, correcto | Análisis de sensibilidad con/sin retiradas y con liquidación por reglas de operador |
| S10 | El usuario podrá mantener cuentas operativas (sin limitación severa) el tiempo suficiente para validar | [HIPÓTESIS] — riesgo real documentado de limitación a ganadores en casas soft | Registrar límites de stake ofrecidos en cada apuesta real; kill criterion si el stake medio permitido cae |

---

## 6. Riesgos principales

Ordenados por producto probabilidad × impacto [INFERENCIA]:

1. **Riesgo de mercado eficiente (existencial):** no existe S1 → el sistema es correcto pero no rentable. Mitigación: baselines de mercado desde el día 1, gates baratos y rápidos, kill criteria.
2. **Riesgo de precio no obtenible (existencial en España):** S2 falsa → edge real pero no monetizable legalmente desde España. Mitigación: C1, doble CLV, gate económico G5.
3. **Riesgo de leakage y grados de libertad del investigador (metodológico):** inflación silenciosa de resultados por fugas temporales, cherry-picking de segmentos o thresholds ajustados al test. Mitigación: M9, test sellado, pre-registro, placebo tests, registro de accesos al test.
4. **Riesgo de limitación de cuentas (operativo):** casas DGOJ limitan stakes a ganadores; el sistema valida pero no escala. Mitigación: S10, medir "stake máximo aceptado", diversificar operadores DGOJ.
5. **Riesgo de discontinuidad de datos (operativo) — ya materializado:** los repos ATP/WTA de Sackmann han desaparecido de GitHub (verificado 2026-08-02) y TML-Database está estancado desde 2026-01. Mitigación: snapshot inmutable inmediato de los mirrors disponibles (datos ≤2024), Tennis-Data como fuente activa de resultados+cuotas, plan de actualización continua propio (fase 0), identificador maestro propio.
6. **Riesgo legal/ToS:** scraping o automatización no autorizada; uso comercial de datos con licencia no comercial. Mitigación: M17, inventario de licencias por fuente (fase 0), separación de datasets "solo investigación".
7. **Riesgo de retiradas y settlement:** reglas heterogéneas por operador; liquidación mal simulada distorsiona el backtest de ejecución. Mitigación: tabla versionada de reglas, settlement reproducible, exclusión del target.
8. **Riesgo de varianza y disciplina (humano):** drawdowns normales de 15–20 unidades con edge real pueden inducir cambios de reglas. Mitigación: pre-registro, fechas de revisión, kill switches automáticos, staking por fases.
9. **Riesgo de coste creciente:** suscripciones de odds/datos superan el beneficio esperado a stakes bajos. Mitigación: presupuesto máximo mensual decidido por el usuario (§27), contabilidad de costes en el ledger.
10. **Riesgo de sobreingeniería:** construir dashboard/microservicios antes de validar datos. Mitigación: roadmap con gates, arquitectura local-first.

---

## 7. Objetivo matemático recomendado

**Función objetivo del sistema completo** [DECISIÓN DE DISEÑO, confianza Alta]: maximizar el crecimiento esperado del capital a largo plazo sujeto a restricciones de riesgo y operativas, descompuesto en cuatro capas independientes y auditables por separado:

**Capa 1 — Estimación probabilística.** Producir p̂ = P(A gana | I_t) minimizando log loss esperado sobre predicciones out-of-sample temporalmente válidas:

```
min_θ  E[ −y·log p̂_θ − (1−y)·log(1−p̂_θ) ]   con  observed_at(x) ≤ t
```

con requisito de calibración: pendiente ∈ [0.90, 1.10], intercepto ∈ [−0.05, +0.05] log-odds (política inicial del documento, mantenida), y sharpness reportada.

**Capa 2 — Incertidumbre y probabilidad conservadora.**

```
p_cons = w·p̂ + (1−w)·p_novig          (primario; w ∈ [0,1] fijado en validación)
p_rob  = clip(p̂ − z·σ_epist, 0, 1)     (secundario, reportado)
σ_epist = g(desacuerdo ensemble, ancho bootstrap, ECE del bin, flags de datos)
```

**Capa 3 — Valor esperado neto y decisión.** Para cuota decimal ejecutable o_exec, comisión c sobre ganancias (0 en bookmaker), y probabilidad conservadora:

```
EV_neto  = p_cons·(o_exec−1)·(1−c) − (1−p_cons)
EV_cons  = LCB_α(EV_neto)             (cota inferior por incertidumbre)
Apostar  ⇔  EV_cons ≥ τ  ∧  edge = p_cons − p_novig ≥ ε  ∧  restricciones:
            liquidez ≥ stake requerido, datos completos, no-OOD,
            contrato comparable, señal no caducada, o_exec ≥ o_min
o_min    = (1 + τ) / p_cons
Abstención = acción por defecto con valor 0 (no es un fallo del sistema)
```

τ (EV mínimo) y ε (edge mínimo) son hiperparámetros pre-registrados (inicialmente τ=3%, ε=2 pp según el documento; sujetos a curva de sensibilidad en validación, no al test).

**Capa 4 — Cartera y staking (separada, pospuesta).** Cuando se active dinero real: maximizar E[log(1+Σ_i f_i·R_i)] con Kelly fraccional (0.10–0.25) sobre p_cons (no sobre p̂), sujeto a caps por apuesta/día/jugador/torneo/operador, límite de correlación (dos precios del mismo resultado = una idea económica), y freno por drawdown. **El backtest y el paper trading usan stake plano 1u**; el staking nunca se optimiza junto con el modelo.

**Jerarquía lexicográfica de objetivos del proyecto** (ligeramente modificada respecto al documento) [DECISIÓN DE DISEÑO, confianza Alta]:

1. Integridad temporal (sin ella todo lo demás es ficción).
2. Calibración out-of-sample.
3. EV conservador **sobre precio obtenible** (fusiona los niveles 3 y 5 del documento: un EV sobre precio no accesible no es un objetivo, es un espejismo).
4. Robustez entre segmentos, modelos y supuestos.
5. CLV prospectivo (diagnóstico adelantado).
6. Rentabilidad prospectiva neta.
7. Escalabilidad (mercados, circuitos, operadores).

**Qué significa "maximizar ganancias y minimizar pérdidas" en este sistema** (respuesta a la sección homónima del encargo): no significa eliminar pérdidas ni subir el hit rate; significa (a) apostar solo cuando EV_cons ≥ τ a precio realmente disponible, (b) dimensionar posiciones para que el riesgo de ruina a horizonte de validación sea despreciable (Kelly fraccional + caps), (c) controlar la correlación para que el drawdown realizado sea compatible con el simulado, y (d) pagar solo los costes de datos que el volumen de apuestas justifica. Hit rate alto y varianza baja son *preferencias operativas* del usuario que se implementan como filtros de rango de cuota, nunca como objetivo de entrenamiento.

---

## 8. Taxonomía de mercados prepartido de tenis

Universo razonablemente modelable en individuales (Bo3 salvo Slams masculinos Bo5). Todos los mercados derivan del mismo generador latente — las habilidades de saque/resto de ambos jugadores — lo que implica correlaciones fuertes y estructura jerárquica natural [EVIDENCIA: fórmulas cerradas punto→juego→set→partido, ver §30].

| Mercado | Target a estimar | Estructura |
|---|---|---|
| Ganador del partido (moneyline) | P(A gana) | Binario |
| Ganador del 1er set | P(A gana set 1) | Binario |
| Jugador gana ≥1 set (≈ hándicap +1.5 sets en Bo3) | 1 − P(A pierde 0–2) | Binario |
| Gana 2–0 (≈ hándicap −1.5 sets en Bo3) | P(marcador 2–0) | Binario |
| Resultado exacto de sets | Distribución {2–0, 2–1, 1–2, 0–2} (más en Bo5) | Multiclase |
| Total de sets (2 vs 3 en Bo3) | P(2–0 ∪ 0–2) | Binario (derivado del anterior) |
| Hándicap de juegos (±k.5) | Distribución del margen de juegos | Ordinal/continuo |
| Total de juegos (O/U k.5) | Distribución del total de juegos | Ordinal/continuo |
| Tie-break en el partido (sí/no) | P(≥1 tie-break) | Binario |
| Mercados de juegos por set, "gana un set a cero", etc. | Distribuciones finas de marcador | Multiclase fina |
| Outright de torneo | P(ganar el torneo) | Multi-etapa (fuera del alcance prepartido por partido) |

Relaciones estructurales relevantes:

- Moneyline y hándicap de sets del mismo jugador: correlación positiva alta (apostar ambos duplica exposición).
- Moneyline del favorito y over de juegos: correlación negativa (partido igualado → más juegos).
- Ganador del 1er set y ganador del partido: correlación alta (el ganador del set 1 en Bo3 gana el partido con probabilidad muy elevada).
- Un motor jerárquico produce todas estas probabilidades **de forma internamente consistente** y permite estimar la matriz de correlación por simulación; modelos independientes por mercado no garantizan ni siquiera coherencia (p.ej. P(2–0)+P(2–1) ≤ P(ganar)).

---

## 9. Matriz comparativa de mercados

Escala: ●●● alto / ●● medio / ● bajo. Valoraciones [INFERENCIA] salvo donde se indica evidencia; celdas con ⚠ dependen de verificación de datos en §15/§30.

| Criterio | Moneyline | 1er set | Gana ≥1 set | Hándicap sets | Marcador sets | Hándicap juegos | Total juegos | Tie-break sí/no | Total sets |
|---|---|---|---|---|---|---|---|---|---|
| Potencial de edge | ●● | ●● | ●● | ●● | ●● | ●● | ●● | ●● | ● |
| Calidad de datos para el target | ●●● (resultados completos) | ●●● (marcadores por sets) | ●●● | ●●● | ●●● | ●●● (juegos) | ●●● | ●● (necesita punto a punto para validar bien) | ●●● |
| Odds históricas disponibles | ●●● (⚠ décadas, sin timestamp) | ● | ● | ● (⚠ exchange) | ● | ● | ● | ● | ● |
| Margen del operador | ●●● bajo (2–7%) ⚠ | ●● | ●● | ●● | ● alto | ●● | ●● | ● alto | ● |
| Liquidez | ●●● máxima | ●● | ● | ●● | ● | ●● | ●● | ● | ● |
| Varianza del resultado | ●● | ●●● mayor | ● baja (favorito) / ●●● | ●●● | ●●● | ●●● | ●●● | ●●● | ●●● |
| Complejidad estadística | ● baja | ●● | ●● | ●● | ●● | ●●● | ●●● | ●●● | ●● |
| Riesgo de settlement (retiradas) | ●● | ● (set 1 suele completarse) | ●● | ●●● | ●●● | ●●● | ●●● | ●●● | ●●● |
| Riesgo de leakage | ●● controlable | ●● | ●● | ●● | ●● | ●●● | ●●● | ●●● | ●● |
| Facilidad de calibración/validación | ●●● (binario, ~5k partidos/año/circuito) | ●●● | ●● | ●● | ●● (multiclase) | ●● | ●● | ● | ●● |
| Baseline fuerte disponible | ●●● (no-vig con historia larga) | ● (no-vig actual sí, histórico escaso) | ● | ● | ● | ● | ● | ● | ● |
| Valor para el MVP | ●●● | ● | ● | ● | ● | ● | ● | ● | ● |
| Escalabilidad futura | ●●● | ●●● | ●● | ●●● | ●● | ●●● | ●●● | ● | ● |

Lecturas clave de la matriz (actualizadas con la verificación externa):

- **Solo la moneyline tiene a la vez odds históricas largas, margen bajo, liquidez máxima y baseline de mercado fuerte.** Tennis-Data contiene **exclusivamente cuotas de moneyline** [EVIDENCIA, notes.txt]; Betfair Historical Data cubre "casi todos los mercados desde 2016" pero **excluye explícitamente los mercados de juegos de tenis** y la presencia de Set Betting/totales exige verificación fichero a fichero [EVIDENCIA parcial]; The Odds API solo cubre Slams/1000/500 con spreads/totales limitados e histórico desde jun-2020 [EVIDENCIA parcial]. Es el único mercado donde puede ejecutarse el programa completo del documento.
- Márgenes verificados (parcial): Pinnacle moneyline ≈2–2,5%; casas soft ≈5–8% (1–2% en Slams muy competidos, más en 250); mercados secundarios documentados cualitativamente como claramente más caros (cifras tipo ">10%" solo en fuentes industriales). **No existe literatura académica de eficiencia para los mercados secundarios de tenis** — el edge alegado en ellos está sin auditar externamente. [EVIDENCIA de la ausencia]
- Los mercados de sets son **derivables** del mismo generador y validables contra marcadores históricos (abundantes) aunque sus odds históricas sean escasas: puede validarse la *calibración del modelo* retrospectivamente y el *value* solo prospectivamente con snapshots propios. Esta asimetría define la fase 2.
- **Sensibilidad crítica del motor jerárquico** [EVIDENCIA + cálculo reproducible del equipo de verificación]: con jugadores parejos a niveles ATP típicos, un error de ±0,01 en p_serve se amplifica a ≈±5 pp en la probabilidad de partido (factor ×5); y el supuesto iid sesga el número esperado de juegos en ~±7% según formato (material para totales: 1,5–2,5 juegos sobre líneas de 22–37). Esto exige calibrar los derivados por separado y explica por qué los mercados de juegos van en última posición.
- Correlaciones entre mercados del mismo partido (Monte Carlo reproducible, favorito p=0,736): corr(moneyline, −1.5 sets) ≈ **+0,53**; corr(moneyline, over juegos) ≈ **−0,17**; corr(−1.5 sets, over) ≈ **−0,69**. Tratar estos mercados como apuestas independientes en un backtest o una cartera subestima gravemente la varianza. [INFERENCIA cuantificada]
- Retiradas: ~2–3% de partidos ATP y ~1,7–2,7% WTA acaban en retirada (más ~0,4–0,5% walkover) [EVIDENCIA]. Sus reglas de liquidación divergen por operador y por mercado (los totales/hándicaps suelen anularse si el partido no se completa), lo que penaliza especialmente a los mercados de juegos.
- Tie-breaks: el rendimiento en tie-breaks es indistinguible de azar para la gran mayoría de jugadores (casi nula correlación entre bloques consecutivos de 43 tie-breaks; análisis de Tennis Abstract, serio pero no peer-reviewed) → no modelar "habilidad de tie-break" persistente; el iid jerárquico es consistente con esta evidencia. [EVIDENCIA de segundo nivel]

---

## 10. Mercados recomendados para comenzar

**Moneyline prepartido, individuales, ATP y WTA main tour (incluyendo Grand Slams con feature Bo5), cuadro final; qualies excluidas inicialmente** [DECISIÓN DE DISEÑO, confianza Alta]. Motivos: matriz §9, disponibilidad de baseline de mercado con décadas de historia, margen mínimo, liquidez máxima, settlement más simple, y toda la literatura comparativa disponible se centra en este target (permite contrastar resultados propios con resultados publicados).

Un solo mercado con dos modelos (ATP/WTA) y calibración por circuito. Sin Challenger/ITF (calidad de datos y límites), sin dobles (datos pobres), sin live.

## 11. Mercados recomendados para una segunda fase

Condicionados al gate G4 (modelo saque/resto validado) y a ≥6 meses de snapshots propios multi-mercado [DECISIÓN DE DISEÑO, confianza Media]:

1. **Ganador del 1er set** — derivado directo del motor jerárquico; target validable con marcadores históricos completos; settlement simple.
2. **Hándicap de sets ±1.5 / gana ≥1 set** — mismos requisitos; atención máxima a reglas de retirada por operador.
3. (Solo si 1–2 muestran calibración estable) **Total de juegos** en Bo3 masculino/femenino con formato homogéneo.

Criterio de entrada por mercado: calibración retrospectiva del derivado (contra marcadores) dentro de tolerancias, odds propias capturadas ≥6 meses, reglas de settlement modeladas y test de correlación con posiciones moneyline activo (una exposición económica, no dos — recuérdese corr(moneyline, −1.5 sets) ≈ +0,53 en el modelo jerárquico).

Datos de contexto verificados para esta fase: el ganador del 1er set gana ~80% de los Bo3 (fuentes industriales concordantes, parcial); la frecuencia de retirada (~2–3%) apenas afecta al mercado de 1er set (el set 1 casi siempre se completa) pero sí a hándicaps/totales; y la amplificación de errores de p_serve (×5 hacia probabilidad de partido, mayor aún en derivados condicionales) obliga a recalibrar cada derivado por separado en lugar de confiar en la consistencia teórica del motor.

## 12. Mercados que no merece la pena modelar inicialmente

- **Marcador exacto de sets** (multiclase, margen alto, settlement frágil) — quizá nunca como mercado apostable; útil solo como output interno del motor.
- **Total de sets** — redundante con marcador exacto, liquidez mínima.
- **Mercados de tie-break** — señal débil, varianza extrema, evidencia de fuerte regresión a la media en rendimiento en tie-breaks [EVIDENCIA parcial, §30].
- **Hándicap de juegos** antes de fase 3 — exige lo mismo que total de juegos y además distribución del margen.
- **Outrights, especiales, combinadas, live** — fuera del sistema.
- **Challenger/ITF** — datos y mercados peores; reconsiderar solo en fase 3 con evidencia de edge en main tour.

---

## 13. Criterios de selección respaldados por evidencia

(Clasificación pedida: 1 evidencia / 2 razonable no demostrado / 3 heurística de riesgo / 4 popular sin evidencia / 5 marketing. Fuentes en §30.)

**Grupo 1 — Respaldados por evidencia (todas las citas verificadas, §30):**

- **El precio de mercado (no-vig) es el predictor individual más fuerte.** Kovalchik (2016, JQAS): de 11 modelos comparados sobre 2.395 partidos ATP, ninguno superó a los bookmakers; el mejor (Elo tipo 538) solo "se acercó". Wilkens (2021): con técnicas ML modernas la accuracy no supera ~70% y la mayoría de estrategias pierde a largo plazo; la información pública ya está en las cuotas. [EVIDENCIA]
- **El cierre es más preciso que la apertura** (movimiento hacia la precisión documentado académicamente en mercados deportivos, Gandar et al. 1998; eficiencia variable por operador, Angelini & De Angelis 2019). [EVIDENCIA]
- **Probabilidad calibrada + proper scoring rules** como base de la detección de value (no accuracy). [EVIDENCIA]
- **Edge frente al no-vig del cierre (CLV) como diagnóstico adelantado de habilidad**: corolario aceptado de la eficiencia del cierre; ojo — el vínculo "CLV positivo ⇒ rentabilidad" se apoya en evidencia de industria (Pinnacle, Buchdahl) más que en peer-review directa. Útil como gate de *no-rechazo*, no como prueba. [EVIDENCIA parcial + INFERENCIA]
- **Elo (y variantes por superficie/margen/ponderadas) como resumen eficiente de la fuerza del jugador**, superior a ranking oficial como predictor (Kovalchik 2016; WElo de Angelini et al. 2022 con paquete R `welo` en CRAN). [EVIDENCIA]
- **Modelos jerárquicos punto→partido**: competitivos con los de nivel partido (Ingram 2019: log loss 0.592 vs 0.641 del mejor rival punto-a-punto) y con valor estructural para derivados. [EVIDENCIA]
- **Aritmética muestral**: ROI en <1.000 apuestas es mayormente ruido; los intervalos deben acompañar cualquier métrica de rentabilidad. [EVIDENCIA matemática]
- **Favorite-longshot bias en tenis** (Forrest & McHale 2007: mayor retorno esperado en favoritos que en longshots en ATP; Lahvička 2014: el sesgo se intensifica en rankings bajos, rondas tardías y torneos de perfil alto, atribuido en parte a protección frente a insiders; también documentado en exchanges, Abinzano et al. 2016/2019). Implicación práctica: desconfiar sistemáticamente del "value" detectado en cuotas altas. [EVIDENCIA]
- **Shin > normalización proporcional** para extraer probabilidades de cuotas (Štrumbelj 2014); los bookmakers difieren como fuentes de probabilidad. Refuerza C5. [EVIDENCIA]
- **El iid punto a punto es una aproximación válida para moneyline** (Klaassen & Magnus 2001: desviaciones reales pero pequeñas), **pero sesga los totales** (~±7% en juegos esperados según formato). [EVIDENCIA]

**Grupo 2 — Razonables pero no demostrados (activar solo tras validación propia):**

- Thresholds concretos de edge mínimo (2 pp) y EV mínimo (3%).
- Robustez entre modelos (exigir acuerdo direccional del ensemble) como filtro de selección.
- Fatiga/descanso/carga reciente y cambio de superficie como información incremental al mercado (el mercado ya los conoce; la pregunta es si los pondera mal).
- Ventaja de modelos saque/resto sobre Elo en subpoblaciones (p.ej. jugadores con estilos extremos de servicio).
- Reglas de caducidad de señal por movimiento de línea adverso.

**Grupo 3 — Heurísticas útiles de control de riesgo (sin pretensión predictiva):**

- Rangos de cuota operativos (p.ej. 1.40–4.00) para acotar varianza y errores de calibración en colas.
- Abstención por datos incompletos, OOD (debut, regreso de lesión larga, superficie sin muestra), conflicto de identidad o mercado stale.
- Caps de exposición por día/jugador/torneo/operador; deduplicación de la misma idea económica.
- Kill switches por divergencia precio mostrado/ejecutado, feed antiguo o drift de calibración.

**Grupo 4 — Populares sin evidencia suficiente (no implementar como señal):**

- Head-to-head crudo entre dos jugadores (muestras minúsculas, confundido por superficie y época).
- "Forma" como rachas de victorias sin ajuste por rival y superficie.
- Estadísticas de presión/clutch (break points, deciding sets) como rasgo estable del jugador: alta regresión a la media documentada; solo admisibles como experimento del grupo 2.
- Narrativas de motivación, cansancio de viaje sin datos, "rivalidad", "necesita el torneo".
- "Cuota baja = apuesta segura" y su inversa "cuota alta = value".

**Grupo 5 — Claims de marketing (ignorar como evidencia):**

- Hit rates y yields autopublicados de tipsters sin universo completo con timestamps y precio ejecutable.
- "Precisión del 70–80%" como reclamo de calidad (alcanzable apostando siempre al favorito; irrelevante para EV).
- Track records retrospectivos de servicios comerciales no auditables externamente.

---

## 14. Heurísticas que requieren validación propia

Lista de experimentos pre-registrables, cada uno con métrica y umbral antes de mirar resultados:

| # | Heurística | Métrica de validación |
|---|-----------|----------------------|
| H1 | Edge mínimo 2 pp vs no-vig | Curva volumen–EV realizado por threshold en validación |
| H2 | EV_cons ≥ 3% | Ídem |
| H3 | Rango de cuota 1.40–4.00 | Log loss/calibración y EV por bucket de cuota |
| H4 | Shrinkage w hacia mercado | w óptimo por log loss en OOF; estabilidad entre años |
| H5 | Acuerdo de ensemble como filtro | EV realizado con/sin filtro, emparejado |
| H6 | Descanso/fatiga/viaje como features | Δ log loss marginal en OOF con bootstrap emparejado |
| H7 | Caducidad de señal por movimiento adverso ≥ x pp | CLV de señales ejecutadas tarde vs temprano |
| H8 | Exclusión de retiradas del entrenamiento | Sensibilidad de coeficientes y calibración |
| H9 | Modelos separados ATP/WTA | Comparación emparejada vs modelo conjunto con feature |
| H10 | Elección proporcional/power/Shin | Log loss del no-vig contra resultados, por operador y rango |

---

## 15. Plan de datos

**Principio rector** [DECISIÓN DE DISEÑO, confianza Alta]: el activo del proyecto es un almacén event-time inmutable y reconstruible; toda fuente externa se ingiere a una capa raw con snapshot fechado y licencia anotada. Diferenciación estricta entre `event_time`, `effective_at`, `observed_at`, `ingested_at`, `prediction_time` (mantengo el modelo del documento).

### 15.1 Necesidades por capa

| Capa | Datos | Uso |
|---|---|---|
| Resultados y marcadores | Partidos ATP/WTA main tour ≥2010 (idealmente ≥2000), sets/juegos, estado (completed/retired/walkover), ronda, formato | Target, Elo, features |
| Estadísticas de partido | Puntos de saque/resto, aces, DF, BP | Modelo saque/resto, features regularizadas |
| Rankings históricos | Semanales, con fecha de publicación | Baseline, feature |
| Biografía | Fecha nacimiento, mano, altura | Features estables |
| Contexto | Superficie, indoor/outdoor, altitud, ciudad, huso | Features; OOD |
| Cuotas históricas | Moneyline por partido, por operador, apertura/cierre | Baseline no-vig, backtest modo A |
| Cuotas snapshots propios | Multi-horizonte con timestamp, bid/ask/profundidad donde exista | Backtest modo B, CLV, obtainability |
| Reglas de settlement | Por operador y fecha, versionadas | Liquidación, comparabilidad de contratos |
| Ledger | Picks, precios vistos/ejecutados, resultados, fees | Auditoría, fiscalidad |

### 15.2 Fuentes evaluadas (verificación 2026-08-02)

Estados: ✅ verificado con fuente primaria/oficial · ◐ parcial (fuente indexada o secundaria concordante) · ✖ no verificado. Varias webs primarias bloquearon el acceso automatizado del entorno de investigación (403); esas verificaciones se apoyan en contenido indexado y deben re-abrirse manualmente antes de decidir (lista en §30).

| Fuente | Qué aporta | Cobertura | Licencia / términos | Coste | Estado 2026 y riesgo |
|---|---|---|---|---|---|
| Sackmann `tennis_atp`/`tennis_wta`/`slam_pointbypoint` | Resultados, marcadores, stats, rankings | ATP/WTA 1968–~2024 | CC BY-NC-SA 4.0 (**no comercial**) | Gratis | ✅ **Desaparecidos de GitHub (404)**; solo quedan mirrors (p.ej. datos ≤2024, misma licencia). Riesgo materializado |
| Sackmann Match Charting Project | Punto a punto / golpe a golpe crowdsourced (>5.000 partidos, muestra no aleatoria) | Selección ATP/WTA 2013– | CC BY-NC-SA 4.0, atribución obligatoria, **no comercial** (texto verificado) | Gratis | ✅ Vivo (push 2026-05-25) pero ritmo decreciente; riesgo medio-alto |
| TML-Database | Resultados ATP re-scrapeados de atptour.com | Solo ATP 1968–2026 | Sin LICENSE; README prohíbe uso comercial; base legal difusa | Gratis | ✅ Sin commits desde 2026-01-27; riesgo alto; no usar como columna vertebral |
| Tennis-Data.co.uk | Resultados + **cuotas moneyline** (B365, PS/Pinnacle, Max/Avg de Oddsportal, etc.) en CSV/XLS | ATP desde 2000 (cuotas ~2001), WTA desde 2007 | Términos no publicados; uso comercial dudoso | Gratis | ◐ Activo, actualización semanal; **instante de captura de cuotas indeterminado** (no tratar como cierre); riesgo medio |
| The Odds API | Cuotas actuales + snapshots históricos timestampados (10 min desde jun-2020; 5 min desde sep-2022) | Tenis: **solo Slams, ATP/WTA 1000 y 500** (sin 250/Challenger); h2h y spreads/totales limitados | Servicio comercial | Free 500 req/mes; históricos solo de pago (desde ~$25/mes, cifras ◐) | ◐ Activo; riesgo bajo-medio; **hueco de universo: no cubre 250** |
| Betfair Historical Data | Volcados del Stream API del exchange: precios, volumen, BSP desde 2016 | "Casi todos" los mercados de tenis; **game betting excluido** (✅); Set Betting/totales sin confirmar fichero a fichero | Licencia propia (no leída) | Basic gratis (1 min, sin volumen); Advanced/Pro de pago (precio no público) | ◐ Producto oficial activo; mejor fuente de cierres reales con profundidad; riesgo bajo |
| Betfair Exchange API (betfair.es) | Precios/order book del **exchange español** (pool separado, licencia DGOJ) | Mercados de tenis del pool .es, liquidez limitada | ToS Betfair España; API oficial | Delayed App Key gratis; Live App Key ~£499 única (✅ artículo oficial) | ✅ Única vía legal de exchange y de captura programática de precios desde España |
| Sportradar Tennis API | Datos oficiales: ATP (vía TDI desde dic-2023), WTA/Slams/Wimbledon (vía compra de IMG Arena, cerrada 03-11-2025); **ITF World Tennis Tour fuera del feed desde 2025** | ATP/WTA/Challenger/Slams | B2B | No público (presupuesto) | ✅ En derechos; inviable para fase personal por coste; candidato si el proyecto se hace comercial |
| Pinnacle API | Precio sharp de referencia | — | Solo bespoke B2B (api@pinnacle.com) | — | ✅ **Cerrada al público desde 23-07-2025**; Pinnacle no acepta clientes españoles; no diseñar nada que la asuma |
| OnCourt (oncourt.info) | App Windows: +420k partidos desde 1990, stats, y **cuotas Pinnacle históricas (ATP 2004+, WTA 2006+)** | ATP/WTA/Challenger/Futures | Licencia de app propietaria; extracción programática en zona gris | ~€49/año o ~€89 vitalicia (◐ terceros) | ◐ Complemento barato de cuotas históricas Pinnacle; formato propietario; riesgo medio |
| Ultimate Tennis Statistics / tennis-crystal-ball | Código Apache-2.0 (Elo por superficie, esquema BD) | — | Código Apache-2.0; datos subyacentes eran Sackmann (NC) | Gratis | ✅ Abandonado (último push 2022); útil solo como referencia metodológica |
| API-Tennis / RapidAPI (varios) | Feeds agregados no oficiales | ? | Procedencia opaca (posible scraping) | Freemium | ✖ Sin verificación de cobertura/SLA; riesgo alto; no usar |
| Sofascore / Flashscore / OddsPortal | Stats y cuotas visuales | Amplia | **Scraping prohibido por ToS** (Sofascore y OddsPortal con cláusulas citadas; Flashscore presumido) | — | ✅/◐ Solo consulta manual y resolución de discrepancias |
| Kaggle (mirrors) | Réplicas de Tennis-Data/Sackmann | Variable | Licencias upstream heredadas aunque mal re-declaradas | Gratis | ◐ Solo prototipado; congelados; nunca source of truth |
| bet365.es / Winamax.es | Precio realmente accesible al usuario | Mercados DGOJ | **ToS prohíben expresamente scraping y robots** (cláusulas verificadas); Winamax se reserva anular apuestas asistidas por programas/IA | — | ✅ Solo lectura manual y ejecución manual |
| Kalshi / Polymarket | Prediction markets con order book y API | Tenis por partido (Kalshi serie KXATPMATCH; Polymarket concentrado en Slams) | **Bloqueados en España por la DGOJ desde 26-05-2026** | Fees por contrato | ✅ Fuera del perímetro legal del proyecto |

### 15.3 Decisiones de datos propuestas (actualizadas tras la verificación)

- **Histórico de resultados/estadísticas:** congelar **inmediatamente** (fase 0, primera semana) un snapshot versionado de los mirrors de Sackmann disponibles (datos hasta ~2024, licencia CC BY-NC-SA → proyecto no comercial mientras dependa de ellos), reconciliado con Tennis-Data (activo) para 2024–2026 y verificación manual muestreada contra webs oficiales. La actualización continua 2025+ pasa a ser responsabilidad propia (Tennis-Data semanal + entrada manual de correcciones), no de un repo comunitario. [DECISIÓN DE DISEÑO, confianza Alta; urgencia añadida por la desaparición de los repos]
- **Cuotas históricas (modo A):** Tennis-Data como columna vertebral, con semántica documentada como "prepartido, instante indeterminado, solo moneyline"; **opcional recomendado**: OnCourt (~€49/año) como segunda serie histórica de Pinnacle para contraste. [Confianza Alta / Media en OnCourt]
- **Cuotas con timestamp (modo B histórico):** Betfair Historical Data desde 2016 (nivel Basic gratuito para empezar; Advanced si el proyecto lo justifica), verificando fichero a fichero qué market types de tenis incluye. [Confianza Media-Alta]
- **Cuotas prospectivas (modo B):** scheduler propio de snapshots (T-24h/T-6h/T-1h/cierre) sobre **API de Betfair.es (Delayed App Key gratuita)** como fuente programática legal + The Odds API de pago si el presupuesto D3 lo permite (ojo: no cubre 250 — decidir si el universo evaluable con CLV se restringe a Slams/1000/500 o se acepta cobertura parcial) + registro manual del precio bet365/Winamax en el instante de decisión. [Confianza Alta en la necesidad; Media en la combinación exacta]
- **Elo:** cálculo propio reproducible desde resultados (general + superficie + variante ponderada tipo WElo), con Tennis Abstract solo como benchmark de sanity. [Confianza Alta]
- **Identidades:** `player_id` propio + aliases por fuente + cuarentena (mantengo el diseño del documento). [Confianza Alta]
- **Lesiones/retiradas noticiosas:** fuera del MVP salvo fuente estructurada con timestamps; nunca extraídas por LLM de texto libre. [Confianza Alta]
- **Reglas de settlement por operador (verificadas, tabla de referencia inicial):** bet365 = anula el mercado de partido si no se completa el partido (con promo comercial de "garantía por abandono"); Pinnacle y Betfair = liquidan el ganador con ≥1 set completado; Winamax = anula el mercado de partido pero liquida las fases completadas. Totales/hándicaps: anulación generalizada si el partido no se completa. Esta heterogeneidad obliga a la tabla versionada de reglas del documento y afecta al CLV comparable (contratos no equivalentes = `market_definition_id` distinto). [EVIDENCIA]

---

## 16. Plan de modelos

Orden de construcción y evaluación [DECISIÓN DE DISEÑO, confianza Alta], cada uno comparado partido a partido con bootstrap emparejado sobre las mismas observaciones:

| Orden | Modelo | Rol | Riesgos clave |
|---|---|---|---|
| 0 | No-vig del mercado (3 variantes) | Baseline duro; también input | Semántica de la cuota histórica |
| 1 | Ranking oficial → logística | Baseline débil de referencia | — |
| 2 | Elo general propio | Baseline; feature | Half-life ajustado solo en folds |
| 3 | Elo superficie (blend con general) | Baseline; feature | Muestra de hierba |
| 4 | Logística regularizada sin mercado ("market-free") | Medir conocimiento propio | Leakage en features de forma |
| 5 | Logística mercado + Elo (+superficie) | **Baseline principal a batir** | — |
| 6 | Saque/resto regularizado (shrinkage a media circuito/superficie) + conversión jerárquica | Candidato con valor estructural; habilita fase 2 | Supuesto iid; calidad stats WTA |
| 7 | Gradient boosting (LightGBM/XGBoost) sobre features tabulares | Challenger no lineal | Overfitting, calibración posterior obligatoria |
| 8 | Ensemble (mercado, Elo, saque/resto, boosting) con pesos temporalmente validados | Candidato operativo probable | Stacking con OOF temporal estricto |
| 9 | Jerárquico bayesiano (estilo Ingram) | Solo si 6–8 muestran que falta incertidumbre estructural | Coste, convergencia |
| — | Redes neuronales, GP, grafos/intransitividad | No en el roadmap inicial; revisar tras 12 meses con evidencia propia | Muestra insuficiente, calibración |

Justificación del tope de complejidad: la literatura comparativa no muestra ventaja prospectiva robusta de modelos profundos sobre mercado+Elo+logística en tenis con datos públicos [EVIDENCIA: Kovalchik 2016 — ningún modelo batió al mercado; Wilkens 2021 — retornos mayoritariamente negativos con ML moderno]; el coste marginal de mantenimiento sí es cierto. Los ROIs positivos publicados (Knottenbelt et al. 2012: 3,8% — la cifra 6,85% corresponde a la tesis asociada de Madurska, no al paper; Sipko 2015: 4,35%, tesis no peer-reviewed; GNN de intransitividad 2025: 3,26%, preliminar) son backtests retrospectivos sin validación prospectiva publicada y deben tratarse como cotas optimistas [EVIDENCIA sobre las cifras; INFERENCIA sobre su fiabilidad]. Nota adicional: la ola 2024–2025 de papers "momentum + XGBoost con 80%+ accuracy" procede en su mayoría del problema C del concurso MCM-2024 y suele contener leakage in-play — no citarla como evidencia de mejora ex ante [EVIDENCIA].

**Detección OOD y drift** [DECISIÓN DE DISEÑO]: reglas explícitas (debut en tour, <N partidos en 12 meses, regreso tras ≥90 días, superficie con <M partidos, edad fuera de rango de entrenamiento, desacuerdo |p̂ − p_novig| > umbral extremo, features críticas imputadas) + monitor de drift (PSI de features, ECE móvil, log loss móvil vs mercado). OOD ⇒ abstención o degradación a "solo informativo".

---

## 17. Baselines obligatorios

Mantengo los siete del documento, con el añadido del orden y el criterio de comparación (bootstrap emparejado, mismas observaciones, mismas ventanas):

1. No-vig proporcional del mejor precio disponible en el instante de referencia.
2. No-vig power y Shin (variantes, mismas cuotas).
3. Ranking oficial → probabilidad (logística univariante).
4. Elo general propio.
5. Blend Elo general + Elo superficie.
6. Logística regularizada sin mercado.
7. **Mercado + Elo (+superficie): baseline principal.** Ningún modelo pasa a paper trading sin superarlo fuera de muestra en log loss y Brier con CI emparejado favorable.

---

## 18. Plan de calibración

- Calibrador ajustado **exclusivamente** con predicciones out-of-fold del periodo de desarrollo; nunca con predicciones de entrenamiento. [Mantengo]
- Candidatos: Platt (logística), beta calibration, isotónica solo si la muestra de calibración es amplia (≥ varios miles). Selección por log loss en validación, por circuito. [Mantengo; matiz: con ~5k partidos/año y OOF de 5 años, isotónica es viable pero propensa a escalones en colas; beta es el candidato a priori]
- Calibración separada ATP/WTA; condicional por rango de cuota o superficie **solo** si los tests de los grupos muestran miscalibración sistemática con CI.
- Métricas: curva de calibración, ECE (bins fijos y adaptativos), pendiente e intercepto de recalibración (siempre juntos), log loss/Brier por decil.
- Re-calibración programada: ventana móvil anual con fecha fija (no tras rachas); el calibrador tiene hash y versión como el modelo.
- Prueba de estrés de calibración: desplazamiento de régimen (entrenar calibrador ≤2022, evaluar 2023–2024).

---

## 19. Plan de validación

Mantengo la estructura del documento (desarrollo ≤2023 con walk-forward expanding; validación 2024; test sellado 2025; shadow/prospectivo 2026) y añado:

1. **Pre-registro** (archivo versionado): hipótesis, métricas, umbrales, segmentos y análisis previstos, antes de tocar validación; cambios solo con nueva entrada fechada.
2. **Folds agrupados por torneo-semana** dentro del walk-forward: evita fuga sutil intra-torneo (mismo jugador, misma semana, información compartida).
3. **Sensibilidad de régimen:** repetir métricas excluyendo 2020; excluyendo Slams (Bo5); por era de formato.
4. **Stress tests específicos:** debuts y jugadores con <10 partidos; regresos de lesión; primeras rondas vs finales; indoor vs outdoor.
5. **Placebo leakage tests:** inyectar deliberadamente una feature futura (ranking t+1) y verificar que el pipeline la detecta/el monitor dispara; inyectar ruido y verificar que la mejora no aparece.
6. **Ablations:** cada familia de features entra con Δlog loss emparejado o no entra.
7. **Comparaciones emparejadas** con bootstrap por bloques temporales (semana/torneo), no por apuesta iid; IC del 95% para ΔLL, ΔBrier, ΔCLV.
8. **Evaluación estratificada obligatoria:** circuito, superficie, rango de cuota, horizonte de predicción, ronda, año.
9. **Separación de preguntas** (pedida en el encargo): calidad predictiva (LL/Brier vs baselines) → calibración (curvas/ECE) → detección de value (EV teórico vs no-vig en modo A) → selección (precision del filtro: EV realizado de candidatos aceptados vs rechazados) → rentabilidad (solo modo B y prospectivo) → staking (solo simulación posterior, nunca junto al modelo).
10. **Presupuesto de accesos al test 2025:** un único acceso final por modelo congelado, registrado en log; el shadow 2026 solo se consume al congelar.

---

## 20. Diseño del backtester

[DECISIÓN DE DISEÑO, confianza Alta]

**Dos modos estrictamente separados:**

- **Modo A — Benchmark de modelos (histórico largo).** Datos: Tennis-Data u odds equivalentes sin timestamp fino. Pretensión: comparar modelos/calibración y estimar *cotas* de value teórico. Prohibido: derivar de él decisiones de dinero, slippage, obtainability o ROI "esperado". Toda salida del modo A se etiqueta `non_executable`.
- **Modo B — Simulación de ejecución (histórico corto + prospectivo).** Datos: exclusivamente snapshots con timestamp y operador identificado (propios o históricos de exchange con profundidad). Simula: precio disponible en `prediction_time + latencia`, profundidad vs stake, comisión/fee por operador, regla de settlement versionada (incluidas retiradas), degradación de precio (o_min), señales caducadas. Único modo que alimenta gates económicos.

**Propiedades comunes:** event-driven por orden cronológico estricto; as-of joins contra el feature store; stake plano 1u; registro de TODOS los candidatos (aceptados y rechazados con reason codes); settlement reproducible desde reglas versionadas; salida = ledger inmutable + métricas §19; determinista bajo seed; golden tests (partidos sintéticos con resultados conocidos a mano) y property tests (simetría A/B, sumas de probabilidad, EV de cuota justa = 0).

**Anti-look-ahead:** el backtester no puede leer tablas fuera de su reloj; acceso a datos solo vía API `get_asof(entity, t)`; test automático que desplaza todas las fuentes +7 días y exige degradación (si mejora, hay fuga).

---

## 21. Diseño del paper trading

- Comienza tras gate G3, con **modelo, calibrador, thresholds y universo congelados** (hashes registrados); duración mínima 6 meses y ≥500 recomendaciones (como *no-rechazo*, ver S3).
- Rutina diaria: ingesta → features → predicción a `prediction_time` fijo (decisión D2 del usuario) → snapshots de precios → decisión/abstención → publicación **inmutable con timestamp previo al partido** (hash encadenado del ledger) → captura de cierre → settlement.
- Se registran: precio recomendado (o_min), precio visto en operadores accesibles, precio "ejecutado" virtual, profundidad si existe, CLV doble (C9), motivo de cada abstención.
- **Métricas semanales automáticas:** log loss/Brier vs mercado, calibración móvil, CLV medio e IC, obtainability, distribución de cuotas, drawdown virtual, drift.
- **Reglas de intocabilidad:** prohibido cambiar modelo/thresholds durante la ventana salvo kill switch; toda excepción se registra y reinicia el reloj del gate.
- Coste: el paper trading debe operar ya con el pipeline de snapshots definitivo (C7) — es el ensayo general de la infraestructura, no solo del modelo.

---

## 22. Arquitectura conceptual

Mantengo la arquitectura del documento con dos ascensos de rango (snapshots y reglas de mercado) y una separación investigación/operación. Sin carpetas ni código todavía.

```
[Fuentes externas]
   ├─ Resultados/stats históricos      ├─ Rankings
   ├─ Odds históricas (modo A)         ├─ Snapshots propios (modo B)  ← componente de 1er orden
   └─ Reglas de mercado por operador (registro versionado) ← 1er orden
            ↓
[Capa raw inmutable]  (snapshot fechado por fuente + licencia anotada)
            ↓
[Normalización + resolución de identidades]  (aliases, cuarentena)
            ↓
[Almacén canónico event-time]  (partidos, marcadores, estados, rankings, odds)
            ↓
[Feature store as-of]  (contratos de feature por horizonte T-24/6/1)
            ↓
[Ratings]  (Elo variantes, saque/resto)     [Registro de modelos] (hashes, métricas)
            ↓                                        ↓
[Modelos] → [Calibración OOF] → [Motor de incertidumbre (σ_epist, OOD)]
            ↓
[Motor de mercados]  (librería jerárquica punto→juego→set→partido; consistencia)
            ↓
[Motor de precios ejecutables]  (no-vig ×3, bid/ask, profundidad, fees, staleness)
            ↓
[Motor de value]  (EV_neto, EV_cons, o_min, caducidad)
            ↓
[Motor de decisiones + reglas de abstención]  (filtros pre-registrados, caps, correlación)
            ↓
[Backtester modo A/B] ─ [Paper ledger + settlement + closing tracker] ─ [Ledger real (manual)]
            ↓
[Monitorización + drift + auditoría]  →  [Dashboard]
```

Por componente (responsabilidad / inputs / outputs / dependencias / riesgos / separación):

- **Ingesta:** descargar/registrar fuentes autorizadas con reintentos y rate limits. In: fuentes. Out: raw fechado. Riesgo: ToS, discontinuidad. Separar de toda transformación.
- **Raw inmutable:** custodia bit a bit con manifest y hash. Nunca se edita; solo se añade.
- **Resolución de identidades:** mapear entidades a IDs canónicos con confianza y cuarentena. Riesgo: falsos matches silenciosos → tests de unicidad y revisión manual muestreada.
- **Almacén canónico:** única fuente de verdad para consultas; estados de partido explícitos. Separado de features (sin agregados).
- **Feature store as-of:** construir vectores por (partido, horizonte) usando solo `observed_at ≤ prediction_time`; versionado de feature-sets. Riesgo principal del sistema: leakage → tests §19–20.
- **Ratings:** procesos incrementales reproducibles (replay determinista desde raw).
- **Modelos/Calibración:** entrenar en folds temporales, calibrar en OOF, registrar hashes. Separación estricta de selección (validación) y auditoría (test).
- **Motor de mercados:** transformar (p_serve_A, p_serve_B) → probabilidades de todos los mercados derivados; property tests; sin acceso a odds (puro).
- **Motor de precios ejecutables:** semántica de cada precio (fuente, timestamp, bid/ask, profundidad, fee, staleness); produce el "mejor precio accesible" por decisión D1.
- **Motor de value/decisiones/abstención:** aplicar §7 capa 3; emitir recomendación con o_min, stake máximo, caducidad, razones y riesgos; registrar también los rechazos.
- **Backtester/Settlement:** §20; reglas versionadas por operador y fecha.
- **Paper trading/Registro de predicciones:** §21; ledger inmutable con hash encadenado.
- **Versionado/Auditoría:** cada predicción reconstruible (M18); log de accesos al test.
- **Monitorización/Dashboard:** drift, calidad de datos, kill switches; interfaz al final (Streamlit), nunca antes del backtester.

Stack concreto: mantengo el del documento (M15). Matiz: SQLite es suficiente hasta el paper trading; PostgreSQL cuando haya ledger real y snapshots concurrentes. [DECISIÓN DE DISEÑO, confianza Media]

---

## 23. Estrategia de control de riesgo

1. **Riesgo de modelo:** ensemble + shrinkage a mercado (C4); OOD ⇒ abstención; drift ⇒ kill switch de recalibración programada.
2. **Riesgo de ejecución:** o_min obligatorio, caducidad de señal, verificación de profundidad, registro de divergencia precio visto/ejecutado con umbral de suspensión.
3. **Riesgo de cartera:** caps por apuesta (1% banca), día (3%), jugador, torneo, operador; deduplicación de exposición económica (mismo resultado en varios operadores = una posición); correlación aproximada vía motor jerárquico cuando haya multi-mercado.
4. **Riesgo de ruina:** staking por fases (paper → microstakes 0.25% → 0.5% → Kelly fraccional 0.10–0.25 sobre p_cons con caps); simulación Monte Carlo de trayectorias antes de cada subida; freno automático −10% banca → mitad de stake; −15% → suspensión y revisión post-mortem.
5. **Riesgo operativo:** kill switches del documento (M14) + "stake máximo aceptado" como métrica de limitación de cuenta (S10).
6. **Riesgo de proceso:** pre-registro, fechas fijas de revisión de reglas, prohibición de cambios tras pérdidas, auditoría mensual con checklist.
7. **Riesgo económico del proyecto:** contabilidad de costes (datos, suscripciones, tiempo) contra banca y contra expectativa realista de beneficio a stakes permitidos; revisión semestral.

## 24. Estrategia de abstención

La abstención es la acción por defecto; apostar requiere pasar TODOS los filtros [DECISIÓN DE DISEÑO, confianza Alta]:

**Abstención dura (no negociable):** datos esenciales incompletos; conflicto de identidad; partido fuera de universo; odds sin timestamp o stale (> umbral); contrato no comparable (reglas de retirada distintas de la referencia); OOD severo (debut, regreso ≥90 días, superficie sin muestra); walkover/incertidumbre de participación conocida.

**Abstención estadística:** EV_cons < τ; edge < ε; intervalo de p̂ que cruza el no-vig; desacuerdo extremo modelo–mercado por encima del umbral de plausibilidad (paradójicamente, "demasiado value" es señal de error propio o información que el mercado tiene y nosotros no — p.ej. lesión); pendiente/intercepto de calibración móvil fuera de banda.

**Abstención económica:** liquidez/stake mínimo incompatibles; o_actual < o_min; señal caducada; cap de exposición alcanzado; operador en cuarentena.

**Registro:** todo candidato rechazado se guarda con reason codes; el ratio candidatos/apuestas y la distribución de motivos son métricas de primera clase del dashboard (un sistema que nunca se abstiene está roto; uno que siempre se abstiene también informa).

---

## 25. Roadmap recomendado

| Fase | Contenido | Duración orientativa | Gate de salida |
|---|---|---|---|
| 0. Fundaciones | Decisiones D1–D10 (§27); inventario de licencias; snapshot congelado de fuentes; esquema canónico; resolución de identidades; **prueba de captura de snapshots 2 semanas** | 3–4 semanas | G0 |
| 1. Dataset + baselines | Capa raw→canónica; Elo propio; no-vig ×3; logística; librería jerárquica (C3); backtester modo A | 6–8 semanas | G1–G2 |
| 2. Modelo candidato | Saque/resto; boosting; ensemble; calibración OOF; validación 2024; un único acceso a test 2025 | 6–8 semanas | G3 |
| 3. Paper trading | Congelación; snapshots definitivos; ledger prospectivo; CLV doble | ≥6 meses, ≥500 señales | G4 |
| 4. Microstakes | Ejecución manual real, 0.25%→0.5% banca; medición de limitación | 6–12 meses | G5 |
| 5. Expansión | Mercados derivados de sets (§11); Kelly fraccional; ¿Challenger?; ¿comparativa tipsters? | condicionada | G6 por mercado |

Total hasta primera apuesta real: **~10–14 meses**. Es deliberadamente lento: la alternativa rápida no produce evidencia, produce anécdotas.

## 26. Gates objetivos entre fases

- **G0 (fundaciones):** decisiones D1–D10 documentadas; licencias inventariadas; captura de snapshots demostrada 14 días seguidos sin huecos >5%.
- **G1 (datos):** completitud >99% campos esenciales; duplicados no resueltos <0.1%; enlace partidos–cuotas ≥98% del universo evaluable; 0 violaciones `observed_at ≤ prediction_time`; walkovers/retiradas etiquetados; reconstrucción exacta del backtest desde hashes. (= documento, mantenido)
- **G2 (baselines):** Elo propio correlaciona con benchmark externo (sanity); no-vig reproduce resultados conocidos (favorito gana ~2/3 de partidos ATP [HIPÓTESIS a comprobar en datos]); backtester pasa golden y property tests; test de desplazamiento temporal (+7d) degrada métricas.
- **G3 (modelo → paper):** Δlog loss ≤ −0.005 vs mercado+Elo superficie (o menor con IC emparejado favorable); ΔBrier ≤ −0.002; calibración en bandas (pendiente 0.90–1.10, intercepto ±0.05); ningún segmento principal degradado >2% sin regla de abstención asociada; sensibilidad limitada al método no-vig; resultados coherentes ATP/WTA. (= documento, mantenido, con pre-registro)
- **G4 (paper → microstakes):** ≥500 señales y ≥6 meses; CLV medio vs cierre accesible > 0 con IC80 que excluye valores ≤ −0.5 pp; obtainability ≥90%; calibración prospectiva en bandas; drawdown virtual dentro del percentil 95 simulado; 0 incidentes de settlement/identidad sin resolver.
- **G5 (microstakes → escala) — gate económico:** ≥6 meses reales; EV realizado neto de costes ≥ 0 con IC80; stake medio aceptado por operadores no decreciente >30%; coste de datos < 50% del beneficio esperado anualizado a stakes objetivo.
- **G6 (por mercado nuevo):** calibración retrospectiva del derivado en tolerancias; ≥6 meses de odds propias del mercado; reglas de settlement modeladas; control de correlación activo.
- **KILL del proyecto (a dinero real):** tras 12 meses prospectivos, CLV vs cierre accesible ≤ 0 (IC80) **o** obtainability <70% sostenida **o** limitación de cuentas que impida el stake mínimo → el sistema queda en modo investigación sin dinero real; se documenta y se reevalúa solo con un cambio estructural (nueva fuente, nuevo mercado, nueva jurisdicción).

---

## 27. Decisiones que debe tomar el usuario

| # | Decisión | Opciones / notas |
|---|----------|------------------|
| D1 | Operadores realmente accesibles y usables (cuentas verificadas, país fiscal) | Determina "mejor precio accesible" y reglas de settlement de referencia |
| D2 | Hora/horizonte estándar de predicción diaria | T-24h / T-6h / T-1h / ventana fija diaria (afecta features, CLV y rutina personal) |
| D3 | Presupuesto máximo mensual para datos y odds | 0 € (universo reducido, más manual) / ~30–100 € / >100 € — condiciona §15.3 |
| D4 | Ambición comercial futura | Personal puro (licencias NC valen) vs posible producto (condiciona fuentes desde el día 0) |
| D5 | Banca conceptual y tolerancia de drawdown | Define caps, kill switches y expectativas |
| D6 | Tiempo personal disponible para registro manual diario | Sin ~15–30 min/día en paper/microstakes el diseño manual no se sostiene |
| D7 | Universo exacto: ¿qualies main tour? ¿United Cup/equipos? ¿Slams (Bo5) dentro o aparte? | Propuesta: cuadro final, sin equipos; Slams dentro con feature Bo5 |
| D8 | Tratamiento de retiradas en el ledger real | Según reglas del operador D1; decidir referencia |
| D9 | Idioma/stack de preferencia y dónde se ejecuta (local vs nube) | Propuesta: local-first |
| D10 | Apetito por fase 5 (derivados) y por el experimento tipsters | Puede recortarse sin afectar al núcleo |

## 28. Recomendación final

**Proceder con el MVP moneyline ATP/WTA en los términos del documento, corregido por C1–C12.** Construir en este orden: fundaciones y licencias → dataset event-time → baselines (mercado, Elo) → backtester modo A/B → modelo saque/resto + ensemble → congelación → paper trading con snapshots propios → microstakes solo si G4 se cumple. No construir: mercados derivados (hasta G4+G6), Challenger, live, redes neuronales, automatización de ejecución (nunca), comparativa de tipsters (opcional post-MVP).

El diseño debe optimizar para **aprender rápido si hay edge monetizable desde España**, gastando lo mínimo hasta ese veredicto. El sistema es valioso incluso si el veredicto es negativo — pero solo si puede emitirlo con evidencia limpia.

## 29. Nivel de confianza en cada recomendación

| Recomendación | Confianza | Base |
|---|---|---|
| MVP moneyline único mercado | Alta | Evidencia §9 + inferencia |
| Separación probabilidad/selección/staking | Alta | Práctica cuantitativa estándar |
| Baseline mercado+Elo como listón | Alta | Literatura §30 |
| Modelo de datos event-time y anti-leakage | Alta | Metodológica |
| División temporal y test sellado | Alta | Metodológica |
| EV contra precio accesible (C1) y doble CLV (C9) | Alta | Inferencia económica sólida |
| Backtester dos modos (C2) | Alta | Resuelve contradicción documental |
| Librería jerárquica temprana (C3) | Alta | Coste/beneficio |
| Shrinkage a mercado como conservador primario (C4) | Media-Alta | Fundamento teórico; falta validación propia |
| No-vig: elección empírica, Shin ligeramente favorecido (C5) | Media | Evidencia comparada limitada |
| Fase 2 = derivados de sets antes que Challenger (C6) | Media | Inferencia estructural |
| Umbrales concretos (τ=3%, ε=2pp, cuotas 1.40–4.00) | Baja-Media | Plausibles, arbitrarios, pre-registrados |
| Perímetro operativo España = casas DGOJ + exchange Betfair.es; Kalshi/Polymarket/Pinnacle excluidos | Alta | Evidencia regulatoria verificada (2026) |
| Plan de datos revisado (mirror Sackmann congelado + Tennis-Data + Betfair histórico + snapshots Betfair.es) | Media-Alta | Evidencia de estado de fuentes; combinación es decisión de diseño |
| Duraciones del roadmap | Media | Estimación |
| Probabilidad minoritaria de éxito comercial desde España | Media | Inferencia sobre márgenes/limitación; no cuantificable con precisión |

## 30. Fuentes utilizadas

**Método y limitaciones de verificación.** La verificación se realizó el 2026-08-02 mediante búsqueda web y lectura de páginas/abstracts por cuatro líneas de investigación independientes (literatura, fuentes de datos, mercados/operadores, mercados derivados). El proxy de red del entorno bloqueó el acceso directo a varios dominios primarios (operadores de juego, AEAT, DGOJ, arXiv/ScienceDirect en descarga directa, tennis-data, the-odds-api, historicdata.betfair); en esos casos la verificación se apoya en contenido primario indexado por buscadores y en concordancia multi-fuente, y así se etiqueta. **Ninguna referencia citada por el documento original resultó inventada.** Antes de decisiones operativas conviene reabrir manualmente las URLs oficiales marcadas ◐/PARCIAL.

### 30.1 Literatura científica (estado de verificación)

- Kovalchik (2016), "Searching for the GOAT of tennis win prediction", *JQAS* 12(3). Ningún modelo superó al mercado; mejor modelo: Elo tipo 538. VERIFICADO. https://www.degruyter.com/document/doi/10.1515/jqas-2015-0059/html
- Wilkens (2021), "Sports prediction and betting models in the ML age: The case of tennis", *J. Sports Analytics* 7(2). Accuracy ≤~70%; retornos mayoritariamente negativos. VERIFICADO. https://journals.sagepub.com/doi/10.3233/JSA-200463
- Klaassen & Magnus (2001), "Are Points in Tennis IID?", *JASA* 96(454). Desviaciones del iid reales pero pequeñas. VERIFICADO. https://www.tandfonline.com/doi/abs/10.1198/016214501753168217
- Newton & Keller (2005), *Stud. Appl. Math.* 114; O'Malley (2008), *JQAS* 4(2). Fórmulas cerradas punto→juego→set→partido (incl. tie-break). VERIFICADO. https://www.cis.upenn.edu/~bhusnur4/cit592_fall2013/NeKe2005.pdf · https://ideas.repec.org/a/bpj/jqsprt/v4y2008i2n15.html
- Barnett & Clarke (2005), *IMA J. Mgmt. Math.* 16(2). Estimación de p_serve por matchup. VERIFICADO. https://academic.oup.com/imaman/article-abstract/16/2/113/704903
- Knottenbelt, Spanias & Madurska (2012), *Comput. Math. Appl.* 64(12). Common-opponent; ROI 3,8% (el 6,85% es de la tesis de Madurska). PARCIAL. https://www.sciencedirect.com/science/article/pii/S0898122112002106
- McHale & Morton (2011), *Int. J. Forecasting* 27(2). Bradley-Terry dinámico con superficie. VERIFICADO. https://www.sciencedirect.com/science/article/abs/pii/S0169207010001019
- Ingram (2019), *JQAS* 15(4). Jerárquico bayesiano de puntos; LL 0.592. VERIFICADO. https://martiningram.github.io/papers/bayes_point_based.pdf
- Ingram (2019), arXiv:1902.07378. GP dinámico > Elo/Glicko en log loss. VERIFICADO. https://arxiv.org/abs/1902.07378
- Angelini, Candila & De Angelis (2022), *EJOR* 297(1). WElo; paquete R `welo` (CRAN). VERIFICADO. https://www.sciencedirect.com/science/article/abs/pii/S0377221721003234
- Sipko & Knottenbelt (2015), tesis Imperial College. ROI 4,35% (literatura gris). PARCIAL (vía citas secundarias). https://www.doc.ic.ac.uk/teaching/distinguished-projects/2015/m.sipko.pdf
- Candila & Palazzo (2020), *Risks* 8(3):68. VERIFICADO. https://www.mdpi.com/2227-9091/8/3/68
- Kull, Silva Filho & Flach (2017), AISTATS. Beta calibration; isotónica propensa a sobreajuste con validación pequeña (el umbral muestral concreto no procede de este paper). VERIFICADO. https://proceedings.mlr.press/v54/kull17a.html
- Forrest & McHale (2007), *Eur. J. Finance* 13(8); Lahvička (2014); Abinzano, Muga & Santamaría (2016, *Appl. Econ. Letters*; 2019, *J. Sports Economics*). Favorite-longshot bias en tenis (casas y exchanges). VERIFICADO. https://www.tandfonline.com/doi/abs/10.1080/13518470701705736 · https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2287335 · https://www.tandfonline.com/doi/full/10.1080/13504851.2015.1093074
- Gandar, Dare, Brown & Zuber (1998), *J. Finance* 53(1) (movimiento apertura→cierre mejora precisión); Angelini & De Angelis (2019), *Int. J. Forecasting* 35(2) (test de eficiencia). VERIFICADO; el corolario CLV→rentabilidad es evidencia de industria (Pinnacle, Buchdahl). https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.155346 · https://www.sciencedirect.com/science/article/abs/pii/S0169207018301134
- Štrumbelj (2014), *Int. J. Forecasting* 30(4). Shin > proporcional. VERIFICADO. https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533
- Easton & Uylangco (2010), *Int. J. Forecasting* 26(3). Eficiencia in-play Betfair. VERIFICADO. https://www.sciencedirect.com/science/article/abs/pii/S0169207009001721
- Gauriot & Page (2019), *Economic J.*; Meier et al. (2020, *J. Econ. Psychology*; 2022, *J. Sports Economics*). Momentum causal débil-moderado (asimétrico por sexo). VERIFICADO. https://academic.oup.com/ej/article-abstract/129/624/3107/5536246 · https://www.sciencedirect.com/science/article/abs/pii/S016748702030026X
- Retiradas: *Eur. J. Sport Science* 2024 (ATP 2,11%, WTA 1,73%); PLOS ONE 2024 (histórico: ATP 3,30% ret. + 0,43% w/o). VERIFICADO. https://pmc.ncbi.nlm.nih.gov/articles/PMC11451576/ · https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0304638
- Tie-breaks ≈ azar: Tennis Abstract (2012, 2019, 2025) — análisis serio no peer-reviewed. VERIFICADO como fuente. https://www.tennisabstract.com/blog/2012/10/18/the-luck-of-the-tiebreak/
- Wang & Drekic (2026), *J. Sports Analytics* (ensembles markovianos), y Li et al. (2026), arXiv:2602.08083 (Server Quality Score): existen, citas del documento correctas. VERIFICADO. https://journals.sagepub.com/doi/10.1177/22150218251412670 · https://arxiv.org/abs/2602.08083
- Clegg & Cartlidge (2025), arXiv:2510.20454 (GNN intransitividad; ROI 3,26% preliminar). VERIFICADO. https://arxiv.org/abs/2510.20454

### 30.2 Fuentes de datos (URLs principales; estados en §15.2)

https://github.com/JeffSackmann (perfil: 1 repo) · https://api.github.com/users/JeffSackmann/repos · https://github.com/JeffSackmann/tennis_MatchChartingProject · https://github.com/Tennismylife/TML-Database · http://www.tennis-data.co.uk/alldata.php · http://www.tennis-data.co.uk/notes.txt · https://the-odds-api.com/sports/tennis-odds.html · https://the-odds-api.com/historical-odds-data/ · https://historicdata.betfair.com/ · https://support.developer.betfair.com/hc/en-us/articles/360017615137 (game betting de tenis no incluido) · https://betfair-datascientists.github.io/data/usingHistoricDataSite/ · https://developer.betfair.com/exchange-api/ · https://support.developer.betfair.com/hc/en-us/articles/115003864531 (coste App Key) · https://docs.developer.betfair.com/display/1smk3cen4v3lu3yomq5qye0ni/Betting+on+Spanish+Exchange · https://investors.sportradar.com/news-releases/news-release-details/sportradar-announces-close-acquisition-img-arena-and-its · https://www.atptour.com/en/news/sportradar-atp-partnership-december-2023 · https://developer.sportradar.com/sportradar-updates/changelog/tennis-api-coverage-updates (ITF fuera desde 2025) · https://arbusers.com/access-to-pinnacle-api-closed-since-july-23rd-2025-t10682/ · https://www.oncourt.info/ · https://github.com/mcekovic/tennis-crystal-ball · https://torneo.sofascore.com/terms-of-service · https://www.oddsportal.com/terms/

### 30.3 Mercados, operadores y regulación (España)

- DGOJ / apuestas cruzadas: Orden HAP/1369/2014 (https://noticias.juridicas.com/base_datos/Admin/534286-om-hap-1369-2014-de-25-jul-reglamentacion-basica-de-las-apuestas-cruzadas.html); ficha Betfair en DGOJ (https://www.ordenacionjuego.es/en/operadores-juego/operadores-licencia/betfair-internacional-plc); Orden EHA/3080/2011 (BOE).
- Bloqueo Kalshi/Polymarket en España (26-05-2026): nota oficial https://www.dsca.gob.es/en/comunicacion/notas-prensa/consumo-abre-expediente-sancionador-plataformas-polymarket-kalshi-ordena · https://igamingbusiness.com/legal-compliance/dgoj-blocks-polymarket-kalshi-unauthorised-operations/ ; bloqueo de Polymarket en Francia (ANJ, 16-07-2026).
- Fiscalidad IRPF (ganancia patrimonial general; pérdidas compensables hasta ganancias del periodo, art. 33.5.d LIRPF): manual AEAT https://sede.agenciatributaria.gob.es/Sede/ayuda/manuales-videos-folletos/manuales-practicos/irpf-2024/c11-ganancias-perdidas-patrimoniales/determinacion-importe-ganancias-perdidas-patrimon-generales/no-derivadas-transmisiones-elementos-patrimoniales.html
- Reglas de retirada: bet365 https://help.bet365.es/es/product-help/sports/rules/tennis · Winamax https://www.winamax.es/terminos-y-condiciones-apuestas-deportivas?LICENSE=ES · Pinnacle https://help.future.pinnacle.com/en/support/solutions/articles/11000057351 · Betfair https://support.betfair.com/app/answers/detail/tennis-rules/
- Comisión Betfair.es 2%: https://www.betfair.es/es/aboutUs/Betfair.Charges/ ; Expert Fee (exchange internacional, 2025): https://betting.betfair.com/betfair-announcements/exchange-news/the-betfair-exchange-expert-fee-faq-111224-6.html
- Márgenes: https://www.pinnacle.com/en/help/knowledge-base/how-do-i-calculate-betting-margins · https://datagolf.com/how-sharp-are-bookmakers · https://livetennis.io/betting-guide/tennis-betting-markets-explained/ (parcial)
- CLV / winners welcome (Pinnacle, educativo): https://www.pinnacle.com/betting-resources/en/educational/how-often-do-you-need-to-beat-the-closing-line/y3u2rwvk4hcw76xu · https://www.pinnacle.com/betting-resources/en/educational/winners-welcome-at-pinnacle
- Limitación de ganadores en casas soft (documentación de práctica): informe del Médiateur des jeux ANJ; https://www.haas-avocats.com/consommation/affaire-winamax-un-tournant-pour-les-joueurs-de-paris-en-ligne/ · https://caanberry.com/bet365-account-limited/ · https://legalbet.es/escuela-de-apuestas/que-hacer-cuando-una-casa-de-apuestas-te-limita/
- Prohibición de robots/scraping: bet365 https://help.bet365.es/s/es-es/terms-and-conditions · Winamax (reglamento, cláusula de anulación de apuestas asistidas por robots/IA).
- Kalshi fees/mercados: https://kalshi.com/docs/kalshi-fee-schedule.pdf · https://kalshi.com/markets/kxatpmatch ; Polymarket fees deportes (parcial): https://marketmath.io/blog/polymarket-fees-explained

### 30.4 Cálculos propios reproducibles

- Simulación Monte Carlo del modelo jerárquico iid Bo3 (sensibilidad ±0,01 p_serve → ≈±5 pp; correlaciones entre mercados; P(2–0|gana)=0,60): script `tennis_model.py` generado durante la verificación (a incorporar al repo en fase 0 como test de referencia del motor de mercados).
- Aritmética muestral del ROI (SE≈1/√N a cuota 2; N≈(1,96/e)²): verificada analíticamente en esta auditoría; coincide con las tablas del documento original.

---
*Informe generado como entregable de auditoría previa. Ninguna decisión que dependa de las preferencias o recursos del usuario (§27) ha sido cerrada.*
