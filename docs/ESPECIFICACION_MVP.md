# Especificación de cierre de diseño — MVP del screener de tenis

**Fecha:** 2026-08-03 · **Base:** documento original + auditoría (`docs/AUDITORIA_SISTEMA_TENIS.md`) + verificación externa ya realizada. Investigación nueva: solo puntos marcados ⚠ (a confirmar en la primera descarga de datos, no bloquean el diseño).
**Ámbito:** individuales ATP y WTA main tour, prepartido, Bo3 (Slams masculinos Bo5 solo para moneyline). Sin ejecución automática de apuestas.

---

## 1. Taxonomía de mercados

Regla previa de equivalencias (Bo3), para no contar dos veces la misma apuesta:

- **Hándicap −1.5 sets ≡ Gana 2‑0 ≡ "gana en sets corridos"** (un solo mercado).
- **Hándicap +1.5 sets ≡ Gana al menos un set ≡ 1 − P(pierde 0‑2)** (un solo mercado).
- **Total de sets O/U 2.5 ≡ P(partido a 3 sets) = P(2‑1) + P(1‑2)** (derivado directo del marcador exacto).
- **Marcador exacto de sets** = la distribución {2‑0, 2‑1, 1‑2, 0‑2} de la que TODOS los anteriores son agregaciones.

| Mercado | Definición / target | Datos para el target | Resultados históricos | Cuotas (act./hist.) | Margen y varianza | Retiradas/settlement | Calibración | Recomendación |
|---|---|---|---|---|---|---|---|---|
| **Ganador del partido (moneyline)** | P(A gana) | Resultados | ✔ 2000+ (Tennis-Data, mirror Sackmann) | Act.: todas las casas; Hist.: ✔ Tennis-Data 2001+ (sin timestamp) | 2–7%; varianza moderada | Reglas divergentes por casa (bet365 exige partido completo; Pinnacle/Betfair 1 set) | Fácil (binario, ~5k partidos/año/circuito) | **PRINCIPAL** |
| **Ganador del 1er set** | P(A gana set 1) | Marcadores por sets | ✔ (score strings) | Act.: habitual; Hist.: ✖ | ~5–10%; varianza alta (un set) | Bajo (set 1 casi siempre se completa) | Fácil contra marcadores | **EXPERIMENTAL** |
| **+1.5 sets (gana ≥1 set)** | 1 − P(0‑2) | Marcadores por sets | ✔ | Act.: habitual; Hist.: ✖ | ~5–10%; varianza baja para el lado favorito del mercado; cuotas frecuentes 1.15–1.60 | Alto: reglas de anulación variables si no se completa | Fácil contra marcadores | **EXPERIMENTAL** |
| **−1.5 sets (gana 2‑0)** | P(2‑0) | Marcadores por sets | ✔ | Act.: habitual; Hist.: ✖ | ~5–10%; varianza alta; cuotas frecuentes 1.40–2.20 para favoritos claros | Alto (anulación si incompleto) | Media (evento condicional) | **EXPERIMENTAL** |
| **Marcador exacto de sets** | Distribución {2‑0,2‑1,1‑2,0‑2} | Marcadores por sets | ✔ | Act.: habitual; Hist.: ✖ | Alto (>8–12%); varianza alta | Alto | Multiclase, muestra por celda menor | **EXPERIMENTAL** (como salida interna es obligatorio: es el generador de los tres anteriores) |
| **Total de sets O/U 2.5** | P(3 sets) = 2·s·(1−s) | Marcadores por sets | ✔ | Act.: frecuente; Hist.: ✖ | Alto; varianza alta | Alto | Fácil contra marcadores | **EXPERIMENTAL** (derivado; no UI propia inicialmente) |
| **Total de juegos O/U** | Distribución del nº de juegos | Juegos por set + modelo saque/resto | ✔ (juegos) | Act.: frecuente; Hist.: ✖ (solo Betfair HD ⚠) | 5–8%; varianza alta; el iid sesga ±7% los juegos esperados | Muy alto (anulación casi universal si incompleto) | Difícil (requiere p_hold de ambos bien calibradas) | **POSTERIOR** |
| **Hándicap de juegos ±k.5** | Distribución del margen de juegos | Ídem + margen | ✔ | Ídem | 5–8%; varianza alta | Muy alto | Difícil | **POSTERIOR** |
| Tie-break en el partido (sí/no) | P(≥1 TB) | Marcadores con TB | ✔ | Act.: ocasional; Hist.: ✖ | Alto; el rendimiento en TB es ≈ azar (evidencia) | Muy alto | Difícil y sin señal estable | **DESCARTADO** |
| Marcadores finos (juegos exactos por set, "gana un set a 0", etc.) | — | — | — | — | Margen muy alto, liquidez mínima | — | — | **DESCARTADO** |
| Outright de torneo, combinadas, live | — | — | — | — | — | — | — | **DESCARTADO** (fuera del sistema) |

Otros mercados comerciales revisados y **no** añadidos: total de aces/dobles faltas y props de jugador (sin datos históricos de cuotas ni ventaja modelable), "race to N games" (equivalente fino de juegos), ganador del 2.º set (condicionado al 1.º → live de facto). Nada de esto cumple los criterios.

Nota sobre el rango 1.40–1.60: no se impone como límite. Los mercados del MVP lo cubren de forma natural — moneyline de favoritos moderados, +1.5 sets de underdogs razonables y −1.5 sets de favoritos fuertes caen sistemáticamente en esa zona — sin necesidad de forzar la distribución de cuotas.

---

## 2. Modelo probabilístico central

**Decisión: diseño híbrido en dos niveles con un puente de marcador de sets.** [DECISIÓN DE DISEÑO, confianza Alta]

Estimar directamente la probabilidad de partido es lo mejor validado por la literatura (mercado+Elo+logística es el listón; Kovalchik 2016, Wilkens 2021). El motor saque/resto→Markov es la única vía a los mercados de juegos, pero amplifica errores (±0,01 en p_serve ≈ ±5 pp en el partido) y exige estadísticas de saque limpias (peores en WTA). Por tanto:

- **Nivel 1 (MVP): modelo directo de partido** p̂ = P(A gana), calibrado por circuito.
- **Puente de sets (MVP):** de p̂ se deriva toda la distribución de marcador de sets con el supuesto de sets i.i.d. + recalibración empírica.
- **Nivel 2 (fase 1.5, no bloquea el MVP): motor saque/resto→Markov** (Barnett-Clarke + O'Malley/Newton-Keller) como modelo challenger, verificación de consistencia y única puerta a totales/hándicaps de juegos.

### Matemática del puente (Bo3)

Sea `s` = probabilidad de que A gane un set cualquiera (sets independientes e idénticos):

```
P(2-0) = s²             P(2-1) = 2·s²·(1−s)
P(0-2) = (1−s)²         P(1-2) = 2·s·(1−s)²
m ≡ P(A gana) = P(2-0) + P(2-1) = s²·(3 − 2s)
```

`m(s)` es estrictamente creciente en [0,1] → dada la probabilidad de partido calibrada `m`, **se invierte numéricamente (bisección) para obtener `s`**, y de `s` sale todo lo demás:

```
Ganador 1er set          = s
Gana ≥1 set (+1.5)       = 1 − (1−s)²
Gana 2-0 (−1.5)          = s²
Total sets: P(3 sets)    = 2·s·(1−s)      ;  P(2 sets) = s² + (1−s)²
```

**Corrección empírica obligatoria:** el supuesto de sets i.i.d. es una aproximación (ignora que ganar el set 1 revela habilidad/estado). Cada probabilidad derivada pasa por un **calibrador propio por mercado** (Platt o beta) ajustado sobre los marcadores históricos out-of-fold: se predice el derivado con el puente, se compara con el resultado real del marcador y se corrige el sesgo sistemático. Con ~50–70k partidos con marcador hay muestra de sobra. Este es el mecanismo que convierte "experimental" en fiable sin necesitar cuotas históricas de esos mercados.

Bo5 (Slams masculinos): el MVP solo emite moneyline; el puente Bo5 (fórmulas análogas con 3 sets objetivo) se implementa pero sus derivados quedan sin exponer.

### Outputs adicionales para juegos (fase 1.5)

Del motor saque/resto (`p_serve_A`, `p_serve_B` estimadas por Barnett-Clarke con shrinkage a la media de circuito/superficie): P(hold) por jugador (fórmula cerrada de juego), distribución de juegos por set y del total por convolución/Markov, hándicap de juegos como distribución del margen. Condición de activación: el modelo M6 debe igualar o superar en log loss de partido al M5 en validación, y la calibración del total de juegos contra los totales históricos debe corregir el sesgo i.i.d. (~±7%).

### Objeto de salida común (por partido y jugador)

```
p_match (calibrada) · s · {P20,P21,P12,P02} · p_set1 · p_ganaset · p_2a0 · p_3sets
σ_epist (desacuerdo de modelos + banda de calibración) · p_cons = w·p̂ + (1−w)·p_novig
por mercado: cuota_justa = 1/p · o_min = (1+τ)/p_cons · EV_neto y EV_cons a la cuota introducida
flags: OOD, datos incompletos, régimen (Bo5, retirada previa reciente)
```

---

## 3. Datos y fuentes

**Estrategia: cinco capas con adaptadores sustituibles, todo importable de fichero, nada obligatorio en línea.**

| Capa | Fuente elegida (v1) | Formato | Campos reales relevantes | Estado |
|---|---|---|---|---|
| **Histórico de entrenamiento (resultados+cuotas)** | **Tennis-Data.co.uk** (ATP 2000+, WTA 2007+) | XLS/CSV por año, descarga manual | Torneo, fecha, superficie, court (indoor/outdoor), ronda, Best of, ganador/perdedor, rankings y puntos previos, sets y juegos por set (W1..W5/L1..L5, Wsets/Lsets), estado (`Comment`: Completed/Retired/Walkover), cuotas moneyline B365, PS/Pinnacle, Max, Avg ⚠ (lista exacta de columnas por año: confirmar contra `notes.txt` en la primera descarga) | Activo, verificado parcial |
| **Histórico de enriquecimiento (stats + biografía)** | **Mirror congelado de Sackmann `tennis_atp`/`tennis_wta`** (datos ≤2024; licencia CC BY-NC-SA → uso no comercial) | CSV | `winner_id/loser_id`, mano, altura, edad, ranking, y estadísticas de saque por partido: aces, DF, svpt, 1stIn, 1stWon, 2ndWon, SvGms, bpSaved, bpFaced | Congelar snapshot en la semana 1 (repos originales desaparecidos) |
| **Partidos del día** | **Importación manual** (CSV/JSON con plantilla) escrita por el usuario o exportada de cualquier web de calendario | CSV/JSON | fecha-hora, torneo, superficie, ronda, jugador A, jugador B, formato | Sin dependencia externa |
| **Cuotas del día** | **Introducidas por el usuario** (las que ve en bet365.es/Winamax/Betfair.es), con timestamp automático al importarlas | CSV/JSON o formulario del screener | mercado, selección, cuota, operador, hora | Manual por diseño (los ToS prohíben scraping) |
| **Fuente automática opcional de solo lectura** | **API oficial de Betfair.es con Delayed App Key (gratuita)**: calendario y precios retrasados del exchange español. Alternativa/segundo adaptador: The Odds API (free tier, solo Slams/1000/500) | JSON (API) | event, market, runner, back/lay, totalMatched ⚠ (profundidad exacta del delayed feed: confirmar al crear la key) | Opcional; el sistema funciona sin ella |

Prohibiciones que la v1 respeta por construcción: sin cuenta de apuestas, sin API de ejecución, sin scraping, sin Google, sin LLM como fuente de datos. Cada fuente entra por un **adaptador** (`HistoricalResultsAdapter`, `DayMatchesAdapter`, `OddsAdapter`) con esquema Pydantic común, de modo que sustituir Tennis-Data u añadir Betfair no toca el resto del sistema. Todo lo descargado se guarda en capa raw inmutable con fecha y licencia.

Identidades: `player_id` propio; el enlace Tennis-Data (nombres "Federer R.") ↔ Sackmann (IDs numéricos) se hace por (apellido normalizado, inicial, ranking±2, fecha) con cuarentena manual de ambiguos — es el único punto delicado de la integración y tiene test propio.

---

## 4. Alcance del primer MVP

**Mercados principales** (generan candidatas con el circuito completo de value, porque tienen backtest histórico):

1. Ganador del partido — ATP.
2. Ganador del partido — WTA.

**Mercados experimentales** (se calculan y muestran SIEMPRE con probabilidad, cuota justa y EV contra la cuota introducida, etiquetados "experimental"; sin historial de cuotas propio no ascienden a candidata fuerte):

3. Ganador del 1er set. 4. +1.5 sets (gana ≥1 set). 5. −1.5 sets (gana 2‑0). 6. Marcador exacto de sets y total de sets (como vista de la misma distribución).

Ascenso de experimental → principal, por mercado: calibración retrospectiva contra marcadores dentro de bandas (pendiente 0.9–1.1) **y** ≥6 meses de cuotas propias registradas con CLV medible. 

**Descartados:** totales y hándicaps de juegos (hasta fase 1.5 con M6 validado), tie-breaks, marcadores finos, props, outrights, combinadas, live, Challenger/ITF, dobles.

**Estados de candidata** (evaluados en este orden; el primero que aplica gana):

| Estado | Regla inicial (pre-registrada, revisable en fechas fijas) |
|---|---|
| **Descartada** | Falla un filtro duro: datos esenciales incompletos, conflicto de identidad, OOD (debut, <5 partidos en 12 meses, regreso ≥90 días, superficie sin muestra), cuota sin timestamp o >6h, mercado con regla de retirada no comparable. Siempre con reason code |
| **Experimental** | Mercado 3–6 con EV_cons ≥ 3% y edge ≥ 2 pp |
| **Candidata fuerte** | Mercado principal, EV_cons ≥ 5%, edge ≥ 3 pp, acuerdo direccional de M4 y M5, sin ningún flag |
| **Candidata normal** | Mercado principal, EV_cons ≥ 3% y edge ≥ 2 pp |
| **Vigilar precio** | EV central > 0 pero cuota actual < o_min → se muestra "apta a cuota ≥ o_min hasta [hora]" |
| **Probable sin value** | p_cons ≥ 60% pero EV_cons < 3% (responde a la preferencia del usuario por favoritos: se ve, pero se dice claramente que no hay value) |

Sin mínimo ni máximo diario. Cero candidatas es una salida normal. Todos los candidatos (aceptados y rechazados) se guardan.

---

## 5. Especificación de implementación

**Inputs:** ficheros Tennis-Data (por año), snapshot mirror Sackmann, CSV/JSON de partidos del día, CSV/JSON o formulario de cuotas del día, config YAML (τ, ε, w, rangos, horizonte, universo).

**Outputs:** (a) tabla diaria de candidatas con estado, p, cuota justa, o_min, EV central y conservador, razones y caducidad; (b) informe de backtest/validación (log loss, Brier, calibración por segmento, comparaciones emparejadas); (c) ledger append-only de predicciones y picks con hashes (commit, dataset, modelo, calibrador, config).

**Modelos iniciales (orden estricto):** M0 no-vig (proporcional/power/Shin, las tres almacenadas) → M1 logística sobre ranking → M2 Elo propio → M3 blend Elo general+superficie → M4 logística regularizada sin mercado → **M5 logística mercado+Elo (candidato operativo)** → calibración Platt/beta por circuito sobre OOF → puente de sets + calibradores por mercado. M6 (saque/resto+Markov) y ensemble: fase 1.5. Nada de redes neuronales.

**Features del MVP (deliberadamente pocas):** Δ Elo general, Δ Elo superficie, log-ratio de ranking, log-ratio de puntos, Δ edad, superficie, indoor, Bo5, ronda, días de descanso de cada jugador (calculados del propio histórico), partidos jugados últimos 14 días, flag retirada propia en últimos 30 días, y (solo M5) p_novig. Excluidos: H2H, rachas crudas, stats de presión.

**Flujo de datos:** raw inmutable → normalización + resolución de identidades → tabla canónica de partidos (con estado y marcadores) → features as-of (regla `observed_at ≤ prediction_time`) → modelos → calibración → puente de sets → motor de value (EV contra cuota introducida o del adaptador) → estados de candidata → ledger y reportes.

**Derivación de probabilidades:** sección 2 (inversión numérica de `m = s²(3−2s)` + calibrador por mercado sobre OOF).

**Reglas iniciales de selección:** tabla de estados de la sección 4; τ=3%, ε=2 pp, w (shrinkage a mercado) elegido por log loss en validación; caps: máx. 1 candidata por partido (mejor EV_cons), exposición sugerida informativa (stake plano 1u virtual).

**Validación rápida (días, no meses):** split temporal train ≤2022 / validación 2023–2024 / test 2025 sellado (un solo acceso); walk-forward anual ligero dentro del train para OOF; criterios: M5 ≥ mercado en log loss y M5 > M2/M3/M4 con bootstrap emparejado; curvas de calibración por circuito y por rango de cuota; calibración de los 4 derivados contra marcadores; backtest modo A con stake plano etiquetado `non_executable`.

**Tests indispensables:** (1) no-vig: probabilidades suman 1, round-trip cuota↔prob; (2) puente: `P20+P21 = m` exacto, distribución suma 1, inversión m→s→m con error <1e-9, monotonía; (3) simetría A/B del modelo; (4) as-of: inyectar ranking t+1 y exigir fallo del pipeline (placebo leakage); (5) desplazamiento +7 días degrada métricas; (6) identidades: cero duplicados, muestreo manual de 50 matches; (7) settlement de retiradas según regla configurada por operador; (8) golden dataset de 20 partidos con resultados calculados a mano; (9) reproducibilidad: mismo hash de config+datos ⇒ mismo backtest bit a bit.

**Orden de implementación (primer pipeline funcional en ~2–3 semanas de trabajo efectivo):**

1. Esqueleto del repo + esquemas Pydantic + loaders de Tennis-Data y mirror Sackmann → capa raw.
2. Resolución de identidades + tabla canónica de partidos (con tests 6).
3. Módulo no-vig (tests 1) + motor Elo (general y superficie, replay determinista).
4. Feature builder as-of (tests 4–5).
5. Baselines M0–M4 + M5 + walk-forward + calibración OOF (validación rápida).
6. Puente de sets + calibradores por mercado (tests 2, 8).
7. Backtest modo A + informe de métricas (test 9).
8. Screener diario: importar partidos + cuotas manuales → tabla de candidatas con estados.
9. Ledger de predicciones/picks + captura opcional del adaptador Betfair.es delayed.
10. (Fase 1.5, tras usar el sistema) M6 saque/resto, ensemble, juegos.

Sin Docker, sin microservicios, sin Postgres (SQLite + Parquet bastan), dashboard mínimo (CLI o Streamlit de una página) solo en el paso 8.

---

## A. Lista exacta de mercados del MVP

**Principales:** 1) Ganador del partido ATP; 2) Ganador del partido WTA.
**Experimentales (calculados y mostrados, no recomendaciones fuertes):** 3) Ganador del 1er set; 4) +1.5 sets; 5) −1.5 sets (2‑0); 6) Marcador exacto / total de sets (misma distribución).
**Posteriores:** total de juegos, hándicap de juegos (requieren M6 + captura de cuotas propia).
**Descartados:** tie-breaks, marcadores finos, props, outrights, combinadas, live, Challenger/ITF, dobles.

## B. Lista de fuentes elegidas

1. **Tennis-Data.co.uk** — histórico de resultados+cuotas moneyline (ATP 2000+, WTA 2007+), descarga manual anual/semanal.
2. **Mirror congelado de Sackmann** (tennis_atp/tennis_wta, datos ≤2024, CC BY-NC-SA, no comercial) — stats de saque, biografía, IDs.
3. **Importación manual** de partidos y cuotas del día (CSV/JSON/formulario), con timestamp.
4. **Opcional:** API Betfair.es Delayed App Key (gratuita, solo lectura) y/o The Odds API free tier como adaptadores sustituibles.
5. Explícitamente fuera: scraping, Google, LLM como fuente, cuentas de apuestas, APIs de ejecución.

## C. Arquitectura matemática del modelo

Logística regularizada **mercado + Elo (+superficie)** calibrada por circuito → probabilidad de partido `m` → inversión de `m = s²(3−2s)` → distribución de sets {2‑0, 2‑1, 1‑2, 0‑2} → todos los mercados de sets por agregación → recalibración empírica por mercado sobre marcadores históricos OOF → `p_cons = w·p̂ + (1−w)·p_novig` → EV neto y conservador contra la cuota introducida → estados de candidata con abstención por defecto. Motor saque/resto→Markov (O'Malley/Newton-Keller) como challenger y llave de los mercados de juegos en fase 1.5.

## D. Bloqueos que impiden programar

1. **Permiso de escritura al repositorio `davidpoop/betbot`** (git y API devuelven 403; los entregables están commiteados localmente y entregados como adjuntos). Único bloqueo operativo real. Se resuelve concediendo acceso de escritura a la integración de GitHub o creando el commit inicial manualmente.
2. **Ninguno técnico:** las fuentes de la v1 son descargables sin cuentas ni claves; los dos ⚠ (columnas exactas de Tennis-Data por año; contenido del feed delayed de Betfair.es) se confirman durante la implementación sin afectar al diseño.

## E. Decisiones restantes del usuario (con defaults propuestos para no bloquear)

| Decisión | Default propuesto si no dices lo contrario |
|---|---|
| Hora de la pasada diaria y horizonte | Una pasada por la mañana (Europe/Madrid) con las cuotas que introduzcas en ese momento |
| Universo exacto | Cuadros finales ATP+WTA, con Slams (Bo5 solo moneyline); sin qualies ni competiciones por equipos |
| Presupuesto de datos | 0 € en v1 (todo gratuito); OnCourt (~49 €/año) y The Odds API de pago, opcionales más adelante |
| Ambición comercial | No comercial (permite el mirror de Sackmann); si algún día cambia, se sustituye esa fuente |
| Interfaz | CLI + tabla exportable; Streamlit de una página solo si lo pides |
| Operadores cuyas cuotas introducirás | bet365.es / Winamax / Betfair.es (define las reglas de retirada a configurar) |

## F. Prompt recomendado para iniciar la implementación

> Implementa el MVP definido en `docs/ESPECIFICACION_MVP.md` del repo betbot, siguiendo su orden de implementación (pasos 1–9). Python 3.12, estructura `src/` con módulos `ingest/`, `canonical/`, `ratings/`, `features/`, `models/`, `markets/`, `value/`, `screener/`, `backtest/`; SQLite + Parquet; Pydantic para esquemas; pytest con los 9 tests indispensables de la especificación (empezando por no-vig, puente de sets y placebo de leakage). Primero los pasos 1–3 con sus tests en verde antes de tocar modelos. Usa datos reales: descarga Tennis-Data (ATP 2010–2024 para empezar) y un mirror de Sackmann, congélalos en `data/raw/` con manifest y licencia. No implementes scraping, ejecución de apuestas, redes neuronales ni el motor de juegos (M6). Al terminar cada paso, muestra las métricas o tests correspondientes y no avances si fallan. La regla `observed_at ≤ prediction_time` es innegociable y el test 2025 queda sellado.

---
*Tras tu aprobación de esta especificación se puede empezar a programar sin más rondas de investigación general.*
