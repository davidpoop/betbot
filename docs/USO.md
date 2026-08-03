# betbot — uso del MVP

Screener cuantitativo local de tenis (ATP/WTA, prepartido). **No ejecuta apuestas
ni scrapea operadores.** El flujo normal es automático (`betbot scan`); la
entrada manual de partidos/cuotas queda como fallback.

## Uso normal: descubrimiento automático

```bash
betbot scan                 # jornada ATP/WTA main tour + cuotas + señales
betbot watch                # escaneo continuo cada 15 min con alertas
```

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

**Fuentes automáticas** (adaptadores sustituibles en `config feeds`):
`github_te` — dataset público republicado en GitHub (partidos de HOY con
moneyline, actualizado cada 6 h; sin superficie/ronda: se estima con aviso);
`espn` — scoreboard JSON público (calendario 48 h y cuotas cuando las publica;
bloqueado en el entorno de desarrollo, operativo en máquinas normales);
`oddsapi` — The Odds API oficial con tu clave gratuita en `BETBOT_ODDS_API_KEY`
(multioperador, Slams/1000/500). Si una fuente cae, el escaneo continúa con las
demás y lo refleja. Los mercados de sets solo se evalúan si alguna fuente
aporta su cuota real (nunca se inventan cuotas). `watch` alerta de señales
nuevas, cruces de `o_min` y señales desaparecidas, sin repetir alertas, y
guarda el historial de precios en el ledger.

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
betbot import-results --file recent_results.csv   # resultados manuales (ver arriba)
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
