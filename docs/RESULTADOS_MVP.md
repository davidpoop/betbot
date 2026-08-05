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

## Corrección crítica — validación prepartido (2026-08-05)

**Causa exacta del fallo.** `github_te` se usaba como fuente de calendario.
Su JSON publica por fila únicamente `{tournament, time, player1, player2,
odds1, odds2, tour}` — sin fecha, sin zona horaria, sin estado y sin
identificador. El adaptador asignaba `date = datetime.now(timezone.utc).date()`
a TODAS las filas, de modo que cualquier fila del feed se convertía en un
partido "de hoy" analizable. Fila real que lo destapó:

```json
{"tournament": "Montreal", "time": "17:00", "player1": "Tsitsipas S.",
 "player2": "Fonseca J. (22)", "odds1": 2.12, "odds2": 1.71, "tour": "ATP"}
```

Que un partido ya jugado siga en ese feed no es hipotético: los feeds del
mismo tipo arrastran partidos empezados (snapshot de las 02:33Z con partidos
iniciados a las 00:51Z, 01:45Z y 01:49Z del mismo día).

**Corrección.** Solo una fuente AUTORITATIVA (identificador + hora de inicio +
estado) puede crear un partido elegible; `github_te` queda como fuente de
cuotas y sus precios sin evento confirmado son `orphan_quote`. Estados
canónicos: scheduled, delayed, postponed, live, completed, cancelled,
walkover, unknown; solo los dos primeros son analizables, con la hora de
inicio en el futuro (margen de 5 min), dentro de ventana, y sin resultado
local que lo contradiga. Sin calendario autoritativo operativo: FAIL_CLOSED.

**Prueba real (2026-08-05 07:39 UTC).**

| Comprobación | Resultado |
|---|---|
| Escaneo real de la jornada | `FAIL_CLOSED: calendario prepartido no verificable; 0 señales generadas` (ESPN bloqueado por la red del contenedor, Sportradar sin clave) |
| Respuesta REAL de ESPN (Roland Garros, 2 partidos finalizados) + cuotas reales del feed | 2 partidos leídos, **0 confirmados prepartido**, ambos excluidos como `estado_no_prepartido` con su hora e id; 71 cuotas quedaron `orphan_quote` |
| Partido finalizado presentado como pendiente | ninguno |
| Señales emitidas sin hora y estado confirmados | ninguna |

**Sobre Tsitsipas–Fonseca en concreto:** tres fuentes independientes
alcanzables (breakpoint, tennis-odds-mvp y odds_api_today) coinciden al
segundo en que ese partido empezaba a las **2026-08-05T15:00:00Z**, es decir
que en el momento de la consulta (07:30Z) todavía era prepartido. El defecto
denunciado es real y está corregido — BetBot no podía saberlo, y de hecho le
asignaba una fecha inventada — pero conviene dejar constancia de que en ese
caso concreto el partido aún no se había jugado.

## Calendarios de respaldo sin credenciales (2026-08-05)

ESPN sigue siendo la primera opción. Como respaldo se integraron dos fuentes
que no exigen cuenta, pago, login ni sortear bloqueos:

| Fuente | Circuitos | Clave | Da id / hora / estado | Papel |
|---|---|---|---|---|
| `espn` | ATP + WTA | ninguna | sí / sí / sí | principal |
| `thesportsdb` | ATP + WTA | valor público de su documentación | sí / sí / sí (sin normalizar) | respaldo |
| `wta_official` | solo WTA | ninguna | sí / sí / solo F,P,U fiables | complemento |
| `sportradar` | ATP + WTA | `SPORTRADAR_API_KEY` | sí / sí / enum cerrado | opcional |

Ambos respaldos son conservadores por diseño: un estado que no se puede leer
con certeza se devuelve como `unknown` y la puerta prepartido lo excluye
(`estado_desconocido`), igual que un partido sin fecha-hora completa
(`sin_hora_de_inicio`). En `wta_official` solo se aceptan los tres códigos de
`MatchState` confirmados de forma independiente (F/P/U); C/S/D/I/L tienen
evidencia contradictoria entre implementaciones reales y se tratan como
desconocidos.

**Descartado tras evaluarlo:** el gateway `app.atptour.com` no publica hora de
inicio por partido (`matchTimeStamp` vacío), está tras Cloudflare (sortearlo
sería evasión de un control de acceso) y los términos de la ATP limitan el uso
a personal y no comercial.

**Verificación en vivo pendiente del usuario.** El contenedor de desarrollo
bloquea en el CONNECT del proxy todos los dominios de terceros —ESPN,
TheSportsDB y api.wtatennis.com incluidos—, antes del handshake TLS, así que
aquí NO es posible una prueba con red doméstica: `betbot feeds calendar`
devuelve FAIL_CLOSED con las tres fuentes en FALLO por 403 del proxy. La
comprobación en una máquina con salida normal es un solo comando:

```bash
betbot feeds calendar --hours 48     # y después: betbot scan
```

Lo que sí está verificado aquí: el parseo de cada fuente contra payloads con su
esquema real de campos, el mapeo de estados, el descarte de dobles/TBD/sin
hora, la deduplicación entre calendarios con jugadores en orden invertido, el
relevo del respaldo cuando ESPN cae (0 → 1 partido confirmado, con fuente,
estado y hora en el informe) y el FAIL_CLOSED cuando fallan todos.

## Tests

156 tests automatizados en verde (+1 saltado sin GUI), de los cuales 34 son la
regresión prepartido: `github_te` no puede crear un evento, cuota huérfana,
completed/live/cancelled/postponed/walkover no analizados, partido de ayer no
promovido a hoy, margen de seguridad, sin hora de inicio, evento invertido
entre fuentes, resultado local que contradice al calendario (con extensión de
iniciales sin coincidencia difusa), FAIL_CLOSED con calendario caído, jornada
vacía que NO es fallo, retirada de señal en `watch` al comenzar, el caso
Tsitsipas–Fonseca de punta a punta, y el parseo de una respuesta REAL de ESPN
(groupings anidados, ISO sin segundos, enum de estado, dobles por roster,
recorte de fechas en cliente, no duplicar Grand Slams entre atp y wta).
El resto: nombres/marcadores/
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
