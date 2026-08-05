# betbot — uso del MVP

Screener cuantitativo local de tenis (ATP/WTA, prepartido). **No ejecuta apuestas
ni scrapea operadores.** El flujo normal es automático (`betbot scan`); la
entrada manual de partidos/cuotas queda como fallback.

## Uso normal: descubrimiento automático

```bash
betbot scan                 # sincroniza resultados + jornada + cuotas + señales
betbot watch                # escaneo continuo cada 15 min con alertas
```

`scan` ejecuta primero `sync-results` (desactivable con `--no-sync-results`) y
muestra SIEMPRE la frescura de resultados por circuito antes del informe:

```
Frescura resultados: ATP hasta 2026-08-03  ·  WTA hasta 2026-08-03
Sync resultados: +12 nuevos, 6 duplicados omitidos, 0 en cuarentena
Cobertura de cuotas por mercado: Moneyline: 56/56;  Primer set: 0/56; ...
```

La cobertura se informa POR MERCADO: un "100 %" de moneyline nunca se presenta
como cobertura total. Los mercados que la fuente no publica aparecen como
`not_offered`; los suspendidos y los partidos no enlazados se listan aparte.

`scan` descubre los partidos elegibles (excluye Challenger/ITF/dobles/qualies/
equipos), obtiene las cuotas disponibles de las fuentes configuradas, resuelve
los nombres contra el registro interno (ambiguos → cuarentena/descartada),
analiza todos los mercados con cuota real y muestra las señales ordenadas por
estado, EV conservador y frescura, con una única ⭐ `recommended_primary` por
partido y cobertura honesta (qué % de partidos tiene cuota y qué mercados
faltan en la fuente). TODAS las evaluaciones (incluidas descartadas) van al
ledger. Opciones:

```bash
betbot scan --today | --hours 48 | --tour ATP | --tour WTA
betbot scan --market match_winner
betbot scan --min-odds 1.40 --max-odds 1.60     # filtro SOLO de visualización
betbot scan --show-watchlist                    # añade probable_sin_value
betbot scan --show-rejected                     # añade descartadas/sin value
betbot scan --export signals.csv
betbot watch --interval 30 --no-notify
```

## Validación prepartido (fail closed)

BetBot **no genera ninguna señal** salvo que una fuente de calendario
AUTORITATIVA confirme el partido. Autoritativa significa que publica, por
partido: identificador, hora de inicio y estado real. Un partido solo es
elegible si su estado es `scheduled`/`delayed`, su hora de inicio está en el
futuro (con margen configurable de 5 min), cae dentro de la ventana pedida, y
el almacén local de resultados no lo contradice (`already_completed`).

- **`github_te` NO es autoritativa** y no puede crear un evento: su JSON no
  trae fecha, ni zona horaria, ni estado por partido. Sigue siendo fuente de
  cuotas moneyline, pero sus precios solo se asocian a eventos ya confirmados;
  los que no encuentran evento quedan como `orphan_quote` y no se analizan.
- **Calendarios autoritativos**, en orden (`config feeds.calendar_order`):
  1. `espn` — scoreboard JSON público, sin credencial. Cubre ATP y WTA. Primera opción.
  2. `thesportsdb` — API REST pública y **documentada** (`/free_sports_api`), ATP
     y WTA. Su clave gratuita es un valor público fijo que aparece en la propia
     documentación (`"3"`, en `feeds.thesportsdb_key`): no hay alta, ni cuenta,
     ni secreto que guardar, por eso va en la config y no en el entorno.
     Su base la alimenta la comunidad, así que sus listados diarios pueden venir
     incompletos: es respaldo, nunca fuente única.
  3. `wta_official` — `api.wtatennis.com`, la API de primera parte que alimenta
     la web de la WTA. Sin clave de ningún tipo. **Solo WTA.** De su campo
     `MatchState` solo se aceptan los tres códigos confirmados (F/P/U); el resto
     se trata como desconocido y la puerta lo excluye.
  4. `sportradar` — opcional, solo si defines `SPORTRADAR_API_KEY`.

  Ninguna de las tres primeras requiere cuenta, pago, login ni sortear bloqueos.
  Si prefieres cero claves de cualquier clase, borra `thesportsdb` de
  `calendar_order`: quedarás con ESPN (ATP+WTA) y la WTA oficial, y el ATP
  dependerá solo de ESPN.

  **Descartado a propósito:** el gateway de `app.atptour.com` no publica hora de
  inicio por partido (`matchTimeStamp` llega vacío), está tras Cloudflare —
  sortearlo sería evasión de un control de acceso — y los términos de la ATP
  limitan el uso a personal y no comercial.
- **Si ninguna responde**, el escaneo termina con
  `FAIL_CLOSED: calendario prepartido no verificable; 0 señales generadas`.
  Cero señales es preferible a recomendar un partido ya jugado.

Cada señal muestra su trazabilidad: `Inicio confirmado`, `Estado`,
`Fuente de calendario` y `Última actualización`. El informe separa además los
partidos confirmados, los de estado no verificable, los ya completados, los
live excluidos y las `orphan_quotes`.

### Puertas adicionales sobre la hora de inicio

Un calendario puede publicar una hora que NO es una hora. Caso real
(2026-08-05): la WTA devolvió `2026-08-06T03:59:00Z` para dos partidos de
Toronto — las 23:59 locales, es decir "tengo la fecha, no el horario" — cuando
sus horas reales eran 18:00Z y 21:00Z del día anterior. Tomada al pie de la
letra, esa marca sitúa el partido hasta 10 h más tarde de lo que empieza, así
que un encuentro ya en juego seguiría pareciendo prepartido. Ahora:

- **Hora provisional** (`hora_provisional`): se detectan las marcas terminadas
  en `:59`/`:58`, la medianoche UTC exacta y cualquier hora idéntica compartida
  por tres o más partidos (pistas distintas no empiezan al mismo segundo). Esas
  horas quedan como `date_only` y **nunca** se etiquetan «Inicio confirmado».
- **Contraste independiente** (`conflicto_de_fuentes`): la hora se compara con
  el `commence_time` de The Odds API — vía su API si tienes clave, o vía dos
  espejos públicos en GitHub que la republican sin credencial. Si discrepan más
  de 90 min, o si la hora independiente ya pasó, o si el índice cubre el torneo
  pero ya no lista el partido, se excluye. Se conservan el `event_id` y el
  `commence_time` originales.
- **El marcador manda** (`evidencia_de_resultado`): si los campos crudos traen
  sets, ganador o duración, el partido se marca jugado aunque el estado diga
  "por jugar". La detección recorre todos los campos, no una lista fija.
- **Resultados desactualizados**
  (`results_feed_stale_pre_match_unverified`): una fuente que publica un código
  de estado sin evidencia de juego (`schedule_only`, como la WTA o TheSportsDB)
  no basta si los resultados locales no llegan al día en curso y ninguna hora
  independiente lo corrobora. ESPN, que publica marcador y `completed`, no
  queda afectada.
- **Identidad estable**: el `event_id` ya NO incluye la fecha
  (`wta:{EventID}:{año}:{MatchID}:{pareja}`), así que un cambio de horario no
  puede crear un evento nuevo que eluda la reconciliación con resultados.

Para auditar un partido concreto:

```bash
betbot feeds wta-raw --match-id LS052 --match-id LS061
```

vuelca todos sus campos crudos, dice si la hora es provisional y si hay
evidencia de que ya se jugó, y guarda un snapshot anonimizado en
`artifacts/exports/wta_raw_snapshot.json`.

`watch` revalida estado y hora en cada ciclo: retira la señal en cuanto el
partido empieza, se completa, se cancela o se aplaza, y congela la última
observación prepartido en `artifacts/ledger/prematch_frozen.jsonl`.

**Fuentes automáticas** (adaptadores sustituibles en `config feeds`):
`github_te` — dataset público republicado en GitHub (partidos de HOY con
moneyline, actualizado cada 6 h; sin superficie/ronda: se estima con aviso);
`espn` — scoreboard JSON público: CALENDARIO autoritativo (id, hora ISO en UTC
y estado). No publica cuotas ni superficie en tenis. En el contenedor de
desarrollo está bloqueado por la política de red del proxy (falla el CONNECT,
antes del handshake TLS: ESPN nunca ve la petición), pero funciona en una
máquina con salida normal a Internet;
`oddsapi` — The Odds API oficial con tu clave gratuita en `BETBOT_ODDS_API_KEY`
(multioperador, Slams/1000/500; solo moneyline en tenis). Si una fuente cae, el
escaneo continúa con las demás y lo refleja. Los mercados de sets solo se
evalúan si alguna fuente aporta su cuota real (nunca se inventan cuotas).
`watch` alerta de: señal nueva (moneyline o sets), cruce de `o_min`, señal
desaparecida, partido con cuota en un mercado nuevo, cambio de mejor fuente,
mercado suspendido y cambio de estado OOD tras un sync — sin repetir alertas
sin cambio material; sincroniza resultados cada ~6 h.

## Sincronización automática de resultados

```bash
betbot sync-results               # incremental desde la última fecha local
betbot sync-results --days 60     # backfill forzado de N días (rellena huecos)
```

Fuentes (orden en `config feeds.results_order`): **sportradar** (API oficial;
requiere `SPORTRADAR_API_KEY`, plan trial gratuito) y **github** (sin
credenciales): resultados ATP con marcador por sets actualizados a DIARIO
(espejo del proyecto TML) y WTA main tour completos con cadencia SEMANAL
(lunes) — es decir, la WTA puede acumular hasta 7 días de retraso entre
semanas. Garantías: append-only con dedupe (exacto por `match_id` y
casi-duplicados con fecha desplazada ±2 días con mismo ganador y sets),
resultados futuros rechazados (`dato_futuro`), debutantes sin ambigüedad dados
de alta automáticamente, nombres dudosos a cuarentena con sugerencias, y una
segunda ejecución no duplica partidos ni altera el Elo. Si una fuente falla,
se conserva el último estado válido y la frescura real se muestra en cada scan;
cuando nuestros datos van >14 días por detrás del partido, la falta de
historial se marca `data_freshness_unknown` (aviso), nunca como falso OOD.

## Mercados de sets con fuente estructurada (Betfair, solo lectura)

No existe (verificado el 2026-08-04) ningún feed público gratuito que publique
cuotas de mercados de sets. El proveedor integrado y testeado es **Betfair
Exchange** (el único exchange legal en España), en modo ESTRICTAMENTE lectura:
solo `listEvents`, `listMarketCatalogue` y `listMarketBook` — el módulo no
contiene ninguna ruta de saldo ni de colocación/modificación/cancelación de
apuestas (garantizado por test). Para activarlo solo necesitas:

```bash
export BETFAIR_APP_KEY="..."      # App Key del panel developer.betfair.com
export BETFAIR_USERNAME="..."     # tu usuario de betfair.es
export BETFAIR_PASSWORD="..."
```

La **Delayed App Key es gratuita** (precios con retardo del exchange, válidos
para prepartido); la Live App Key es de pago. El catálogo de mercados se
consulta EN VIVO por evento (sin lista cerrada de marketType) y se mapea
dinámicamente: `MATCH_ODDS→match_winner`, `SET_WINNER (set 1)→set1_winner`,
`PLAYER_X_WIN_A_SET Yes→wins_set(x)` / `No→straight_sets(rival)`,
`SET_BETTING "X 2-0"→straight_sets(x)`, `NUMBER_OF_SETS 3→three_sets(yes)`.
Se conserva TODO el metadato del precio (ids, tipo crudo, runner, mejor back
disponible con tamaño, timestamp, estado open/suspended) en
`artifacts/ledger/structured_prices.jsonl`; mercados ausentes del catálogo →
`not_offered`; suspendidos no se cotizan. `three_sets` mantiene su aviso
permanente de calibración y nunca puede ser señal fuerte.

## Estado y prueba de fuentes

```bash
betbot feeds status     # orden configurado y presencia de credenciales (nunca el valor)
betbot feeds calendar   # prueba EN VIVO los calendarios y lista los partidos confirmados
betbot feeds oddsapiio-probe   # evalúa la cobertura real del plan gratuito de Odds-API.io
betbot feeds test       # petición real de lectura a cada fuente activa
betbot feeds markets    # catálogo REAL de mercados por evento (proveedores estructurados)
```

## Sondeo de Odds-API.io (evaluación, no integración)

```bash
export ODDS_API_IO_KEY='tu-clave'          # solo entorno; nunca se escribe en disco
betbot feeds oddsapiio-probe --hours 48 --sample 6
```

Mide qué ofrece REALMENTE el plan gratuito para la jornada ATP/WTA actual. **No
genera señales, no toca el ledger y Odds-API.io no participa todavía en
`betbot scan`**: es una fuente adicional y sustituible que solo se integrará si
el sondeo demuestra que aporta mercados de sets con cuota real. Betfair y el
resto de fuentes siguen intactas.

El informe trae: eventos ATP/WTA en la ventana; la muestra elegida priorizando
los partidos que el calendario autoritativo ya confirmó como prepartido; los
**nombres crudos de todos los mercados** por partido y bookmaker con sus
selecciones y cuotas; el mapeo a los contratos de BetBot (`match_winner`,
`set1_winner`, `wins_set`, `straight_sets`, `three_sets`, `set_score`); qué
bookmakers habilita el plan, deducido tanteándolos uno a uno; la contabilidad
exacta de peticiones consumidas y restantes; y una política de polling por
debajo de 500 peticiones diarias.

Dos reglas de honestidad: un contrato **solo** se declara soportado si apareció
en la respuesta con cuota utilizable, y un mercado cuyo nombre encaja pero cuyas
selecciones lo contradicen se marca `ambiguo` en vez de forzarlo — el caso
típico es «Correct Score», que puede ser marcador por sets (2-0, 2-1) o por
juegos del primer set (6-4, 7-5); solo el primero es `set_score`.

Al terminar guarda `artifacts/exports/oddsapiio_probe_fixture.json`: la
respuesta real anonimizada (jugadores seudonimizados, sin clave ni datos de
cuenta, conservando mercados y cuotas) para poder escribir tests contra ella.

`betbot feeds calendar` es la comprobación que conviene hacer al estrenar el
sistema en tu máquina: pide la jornada a cada calendario, aplica la puerta
prepartido y te enseña, por fuente, cuántos partidos confirma y una muestra con
hora de inicio, estado, jugadores e identificador — más el total combinado tras
deduplicar. Si todas fallan, imprime el mismo `FAIL_CLOSED` que emitiría `scan`.

Las claves se leen EXCLUSIVAMENTE de variables de entorno (`SPORTRADAR_API_KEY`,
`BETFAIR_APP_KEY`, `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`, `BETBOT_ODDS_API_KEY`)
y jamás se guardan en ficheros, logs ni ledger.

## Instalación de doble clic (recomendada)

Descomprime `BetBot_portable.zip` y sigue `LEEME_PRIMERO.txt`: **1)** doble clic
en `INSTALAR_BETBOT` (.bat en Windows, .command en macOS, .sh en Linux; solo la
primera vez, necesita Python 3.11+ e Internet una vez), **2)** doble clic en
`ABRIR_BETBOT.vbs` / `BetBot.app` / `ABRIR_BETBOT.sh` — se abre el navegador
sin ninguna consola, en un puerto libre y **solo en 127.0.0.1**. Si BetBot ya
está abierto, el lanzador reenfoca la pestaña y avisa (instancia única).
Cierre seguro: botón «🛑 Cerrar BetBot» en la barra lateral o `CERRAR_BETBOT`.
`DESINSTALAR_BETBOT` elimina el entorno sin borrar tus datos ni picks.
Si faltan datos o modelos, la interfaz muestra la pantalla de primer arranque
con botones (Descargar → Preparar → Entrenar → Reintentar); el test sellado
2025 nunca se ejecuta desde ahí. Logs en `artifacts/logs/`.
`BetBot.exe` opcional: `CONSTRUIR_BETBOT_EXE.bat` (PyInstaller, reproducible).

## Instalación manual (desarrolladores)

```bash
pip install -e .          # requiere Python 3.11+ (incluye la interfaz Streamlit)
```

## Uso diario en <5 minutos (interfaz)

```bash
betbot ui                  # abre la interfaz local en el navegador
```

1. **(~1 min, semanal)** Botón `🔄 Actualizar datos` en la barra lateral (o
   `betbot update-data`): re-descarga fuentes vivas, refresca Elo/estado y
   avisa de frescura por circuito. Los modelos congelados NO se re-entrenan.
2. **(~2 min)** Pestaña **📅 Día**: teclea o importa los partidos y las cuotas
   que ves en tu operador → botón `▶️ Analizar`. Filtra por circuito,
   superficie, mercado, estado o rango de cuota; ordena por cualquier columna;
   consulta el detalle por partido (probabilidades, cuota justa, `o_min`,
   distribución de sets, avisos) y exporta CSV.
3. **(~1 min)** Marca las candidatas que habrías seguido con `📌 Seguir esta
   candidata` (quedan en el ledger con cuota, operador, timestamp y versión del
   modelo). Si un día no hay candidatas, ese cero también queda registrado.
4. **(~1 min, al día siguiente)** Pestaña **📌 Paper trading**: introduce la
   cuota de cierre si la apuntaste (CLV) y pulsa `⚖️ Liquidar picks` — los
   resultados se resuelven solos contra el dataset actualizado; el resto se
   liquida manualmente. Las métricas prospectivas (PnL, yield, acierto,
   drawdown, CLV) se acumulan separadas del backtest histórico.
5. Pestaña **📈 Monitor**: calibración prospectiva, candidatas/día, % de días
   sin picks, estados y reason codes de descartes, con avisos de muestra pequeña.

Rankings actuales (opcional): rellena `data/manual/rankings_atp.csv` /
`rankings_wta.csv` (plantillas via `betbot template`) copiando la tabla oficial;
join as-of estricto (jamás ranking futuro) con fallback a Elo si faltan.

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

## Importar resultados recientes a mano (cuando los mirrors van con retraso)

```bash
betbot template                                   # escribe recent_results.csv
# ... rellena filas copiando resultados de webs oficiales (via legal, sin scraping) ...
betbot import-results --file recent_results.csv   # valida + append-only + refresca Elo/estado
betbot import-results --file f.csv --dry-run      # solo validar
betbot import-results --file f.csv --allow-new    # acepta jugadores nuevos (si no: cuarentena)
```

Formato: `date,tour,tournament,surface,indoor,round,best_of,winner,loser,score,status`
con el marcador orientado al GANADOR (`6-4 7-6(3)`; retiradas con parcial `6-4 3-1`;
walkover sin marcador). Validaciones: fecha no futura, marcador coherente con
best_of y status, duplicados (fichero y dataset), y **cuarentena con sugerencias**
para nombres no reconocidos (`data/canonical/import_quarantine.csv`). Cada
importación queda registrada con SHA256 en `manual_imports_log.jsonl`. Los
resultados manuales viven en `manual_results.parquet` (append-only, sobrevive a
las reconstrucciones de mirrors; si el mirror alcanza ese partido, el mirror
manda). Tras importar, el Elo (general y por superficie), la actividad y el
descanso se recalculan con el replay determinista; los modelos no se tocan.
También disponible en la interfaz (barra lateral → *Importar resultados recientes*).

## Comandos adicionales (CLI)

```bash
betbot sync-results [--days N] [--no-refresh]     # sincronización de resultados (ver arriba)
betbot feeds status|test|markets                  # estado real de las fuentes
betbot import-results --file recent_results.csv   # resultados manuales (fallback)
betbot update-data          # actualización incremental con salvaguardas explícitas
betbot paper list|close|settle|metrics    # paper trading desde consola
betbot monitor              # días, estados, descartes y calibración prospectiva
betbot thresholds-report    # sensibilidad de umbrales SOLO en validación (no cambia defaults)
betbot study-three-sets     # estudio de calibración de three_sets (test 2025 intacto)
```

Decisiones registradas de esta fase: `three_sets` mantiene AVISO permanente
(`aviso_calibracion_three_sets`) porque su miscalibración (pendiente 1.14–1.34)
no se corrige de forma robusta con recalibración temporalmente válida — el
puente iid sobreestima los 3 sets en partidos igualados (obs. 40% vs ~48%
teórico con |m−0.5|<0.1) y el patrón no transfiere de desarrollo a validación.
Los umbrales por defecto NO han cambiado; el informe de sensibilidad es
consultivo (pestaña ⚙️ Sensibilidad).

## Ledger y reproducibilidad

Cada ejecución del screener se anota en `artifacts/ledger/screen_runs.jsonl`
(append-only) con: timestamp, hash del dataset, SHA de git del modelo, umbrales
y todas las filas (aceptadas y rechazadas). Los informes de entrenamiento y
evaluación quedan en `artifacts/reports/`.
