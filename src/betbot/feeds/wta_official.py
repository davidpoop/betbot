"""Fuente "wta_official": calendario WTA desde la API de primera parte de la WTA.

`api.wtatennis.com` es el endpoint JSON propiedad de la WTA que alimenta su
propia web pública de resultados. Se sirve SIN autenticación: no hay login, ni
CAPTCHA, ni control de acceso que sortear, y no es scraping de HTML — se lee el
mismo JSON que el navegador recibe. No requiere clave de ningún tipo.

Da por partido: MatchID (+EventID/EventYear/RoundID), `MatchTimeStamp` en UTC
(con respaldo `NotBeforeISOTime` para los horarios condicionados al partido
anterior, típicos del orden de juego), `MatchState` y los dos jugadores.

Cautelas aplicadas:
- `MatchState` es un código de UNA letra y su vocabulario NO está documentado.
  Solo tres valores están confirmados de forma independiente por varios
  clientes reales: F = finalizado, P = en juego, U = próximo. El resto
  (C/S/D/I/L, sobre los que hay evidencia CONTRADICTORIA entre
  implementaciones) se devuelve como `unknown` y la puerta prepartido lo
  excluye. Preferimos perder un partido a apostar sobre un estado mal leído.
- **`MatchTimeStamp` puede ser un PLACEHOLDER de fin de día local.** Caso real
  (2026-08-05): Jović–Linette y Samsonova–Krejcikova llegaron con
  `2026-08-06T03:59:00Z` = 23:59 de Toronto, cuando sus horas reales eran
  18:00Z y 21:00Z del día anterior. Tomada como hora real, esa marca sitúa el
  partido HASTA 10 H MÁS TARDE de lo que empieza, así que un encuentro ya
  comenzado seguiría pareciendo prepartido. Esas horas se marcan
  `time_precision="date_only"` y la puerta las rechaza.
- **`NotBeforeISOTime` es una cota inferior, no un inicio.** "Not before 14:00"
  significa "no antes de", no "a las". Se conserva como metadato y NUNCA
  confirma una hora.
- **El marcador manda sobre el estado.** Si los campos crudos traen sets,
  ganador o duración, el partido se marca `completed` aunque `MatchState` diga
  U.
- La fuente es `schedule_only`: publica un código de estado sin evidencia de
  juego verificable, así que la puerta le exige corroboración adicional.
- Solo cubre WTA: es complemento de ESPN/TheSportsDB, no sustituto.
- Es un endpoint sin contrato publicado: puede cambiar sin aviso. Se consume
  con caché, ritmo bajo y User-Agent identificable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level,
                               evidence_of_play, mark_placeholder_times, strip_seed)
from betbot.schemas import OddsQuote

URL = "https://api.wtatennis.com/tennis/matches/global"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betbot/0.1; herramienta local)",
           "Accept": "application/json"}

# Solo los tres códigos confirmados de forma independiente; el resto -> unknown.
# Distribución observada sobre 25.952 partidos reales: F=25591, U=316, P=42,
# C=2, W=1 (C y W con duración 00:00:00 y marcador vacío: evidencia insuficiente
# para mapearlos, así que se quedan en `unknown` y la puerta los excluye).
MATCH_STATE = {"F": "completed", "P": "live", "U": "scheduled"}


def normalize_match_state(raw: str | None) -> str:
    return MATCH_STATE.get(str(raw or "").strip().upper(), "unknown")


def _player_name(first: str, last: str) -> str:
    """('Iga', 'Swiatek') -> 'Swiatek I.' (formato Tennis-Data)."""
    last, first = str(last or "").strip(), str(first or "").strip()
    if not last:
        return ""
    return f"{last} {first[:1]}." if first else last


def _truthy(v) -> bool:
    """Booleano tolerante: la fuente mezcla true/'true'/1/'1'."""
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _parse_iso(raw) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace(" ", "T").replace("Z", "+00:00")
    if len(s) == 16:
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class WtaOfficialCalendar:
    name = "wta_official"
    authoritative = True
    markets: list[str] = []

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl = ttl_seconds

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        now = datetime.now(timezone.utc)
        st = SourceStatus(name=self.name, ok=False, authoritative=True,
                          fetched_at=now.isoformat())
        try:
            data = cache.get_json(URL, ttl_seconds=self.ttl, headers=HEADERS)
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        lo, hi = now - timedelta(days=1), now + timedelta(hours=window_hours)
        out, n_raw, n_skipped = self.parse_matches(data, now.isoformat(), lo, hi)
        st.ok = True
        st.n_items = len(out)
        st.notes.append(f"{n_raw} partidos leídos; {len(out)} individuales en ventana "
                        f"({n_skipped} descartados)")
        st.notes.append("solo cubre WTA; estados distintos de F/P/U se tratan como desconocidos")
        return out, st

    @classmethod
    def parse_matches(cls, data, observed_at: str, lo: datetime | None = None,
                      hi: datetime | None = None) -> tuple[list[FeedMatch], int, int]:
        # la respuesta puede venir como lista o envuelta en un objeto
        rows = data
        if isinstance(data, dict):
            for k in ("Matches", "matches", "data"):
                if isinstance(data.get(k), list):
                    rows = data[k]
                    break
            else:
                rows = []
        if not isinstance(rows, list):
            rows = []
        out: list[FeedMatch] = []
        n_raw = n_skipped = 0
        from betbot.canonical.names import canonical_key
        for r in rows:
            if not isinstance(r, dict):
                continue
            n_raw += 1
            if str(r.get("DrawMatchType", "S")).strip().upper() not in ("S", ""):
                n_skipped += 1
                continue                                  # dobles
            ida, idb = str(r.get("PlayerIDA", "")), str(r.get("PlayerIDB", ""))
            if "TBD" in (ida.upper(), idb.upper()):
                n_skipped += 1
                continue                                  # cruce aún sin definir
            p1 = _player_name(r.get("PlayerNameFirstA"), r.get("PlayerNameLastA"))
            p2 = _player_name(r.get("PlayerNameFirstB"), r.get("PlayerNameLastB"))
            if not p1 or not p2:
                n_skipped += 1
                continue
            # `MatchTimeStamp` es la única candidata a hora real; NotBefore es una
            # COTA INFERIOR ("no antes de") y nunca confirma un inicio.
            start = _parse_iso(r.get("MatchTimeStamp"))
            not_before = _parse_iso(r.get("NotBeforeISOTime"))
            ref = start or not_before
            if ref is None:
                n_skipped += 1
                continue                                  # sin ninguna referencia temporal
            if lo and hi and not (lo <= ref <= hi):
                n_skipped += 1
                continue
            tournament = str(r.get("TournamentName") or r.get("EventTitle")
                             or r.get("CourtName") or "WTA").strip()
            rnd = str(r.get("RoundID") or "").strip()
            p1, p2 = strip_seed(p1), strip_seed(p2)
            a, b = sorted([canonical_key(p1), canonical_key(p2)])
            level = classify_level(tournament)
            if str(r.get("DrawLevelType", "")).strip().upper() in ("I", "ITF"):
                level = "itf"

            # el marcador se impone al código de estado
            status = normalize_match_state(r.get("MatchState"))
            played, why = evidence_of_play(r)
            if played and status not in ("completed", "walkover"):
                status = "completed"

            precision, note = "exact", ""
            # la propia fuente declara cuándo la hora no es firme
            if _truthy(r.get("Unscheduled")):
                precision = "date_only"
                note = "la fuente marca el partido como Unscheduled (sin horario asignado)"
            elif _truthy(r.get("isEstimatedStartTime")):
                precision = "date_only"
                note = "la fuente marca isEstimatedStartTime: la hora es una estimación"
            if start is None:
                precision = "unknown"
                note = (f"solo hay NotBeforeISOTime ({not_before.isoformat()}), que es una "
                        f"cota inferior, no una hora de inicio")
            if played:
                note = (note + "; " if note else "") + \
                    "evidencia de partido jugado: " + ", ".join(why)

            # identidad ESTABLE: torneo + año + hueco del cuadro + pareja.
            # Sin la fecha: un cambio de horario NO puede crear un evento nuevo
            # que eluda la reconciliación con resultados.
            ev = str(r.get("EventID", "") or "")
            yr = str(r.get("EventYear", "") or (ref.year if ref else ""))
            mid = str(r.get("MatchID", "") or "")
            out.append(FeedMatch(
                date=ref.date(), tour="WTA", tournament=tournament,
                player1=p1, player2=p2, level=level, is_doubles=False,
                is_qualifying="q" in rnd.lower() and "qual" in rnd.lower(),
                round=rnd, best_of=3, surface=None, source="wta_official",
                event_id=f"wta:{ev}:{yr}:{mid}:{a}__{b}",
                scheduled_at_utc=start, status=status,
                source_updated_at=observed_at, start_tz="UTC",
                authoritative=True, time_precision=precision, time_note=note,
                trust_tier="schedule_only", raw=dict(r)))
        # la detección de horas provisionales necesita ver el lote entero
        mark_placeholder_times(out)
        return out, n_raw, n_skipped

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=True,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        st.notes.append("la WTA no publica cuotas: solo calendario y estado")
        return [], st

    # ------------------------------------------------------------------
    # RESULTADOS del mismo día (ResultsSource): el propio feed global publica
    # los partidos finalizados con marcador por sets — primera parte, sin
    # credencial. Es la fuente de resultados WTA más fresca disponible; los
    # mirrors semanales quedan como histórico/respaldo.
    # ------------------------------------------------------------------
    def fetch_results(self, since, until) -> tuple[list[dict], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        try:
            data = cache.get_json(URL, ttl_seconds=self.ttl, headers=HEADERS)
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
        rows, n_raw, n_skipped = self.parse_results(data, since, until)
        st.ok = True
        st.n_items = len(rows)
        st.notes.append(f"{n_raw} partidos leídos; {len(rows)} finalizados con marcador "
                        f"en ventana ({n_skipped} descartados)")
        st.notes.append("cubre solo la ventana que publica el feed global (días "
                        "cercanos); el histórico semanal sigue viniendo del mirror")
        return rows, st

    @classmethod
    def parse_results(cls, data, since, until) -> tuple[list[dict], int, int]:
        """Partidos MatchState=F con marcador por sets -> filas de la plantilla
        manual (marcador orientado al GANADOR). Sin marcador completo o sin
        ganador claro, la fila se descarta: nunca se inventa un resultado."""
        rows_in = data
        if isinstance(data, dict):
            for k in ("Matches", "matches", "data"):
                if isinstance(data.get(k), list):
                    rows_in = data[k]
                    break
            else:
                rows_in = []
        if not isinstance(rows_in, list):
            rows_in = []
        out: list[dict] = []
        n_raw = n_skipped = 0
        for r in rows_in:
            if not isinstance(r, dict):
                continue
            n_raw += 1
            if str(r.get("DrawMatchType", "S")).strip().upper() not in ("S", ""):
                n_skipped += 1
                continue
            if str(r.get("MatchState", "")).strip().upper() != "F":
                n_skipped += 1
                continue
            winner_side = str(r.get("Winner", "")).strip().upper()
            if winner_side not in ("A", "B"):
                n_skipped += 1
                continue
            pA = _player_name(r.get("PlayerNameFirstA"), r.get("PlayerNameLastA"))
            pB = _player_name(r.get("PlayerNameFirstB"), r.get("PlayerNameLastB"))
            if not pA or not pB:
                n_skipped += 1
                continue
            ref = _parse_iso(r.get("MatchTimeStamp")) or _parse_iso(r.get("NotBeforeISOTime"))
            if ref is None or not (since <= ref.date() <= until):
                n_skipped += 1
                continue
            # marcador por sets orientado al ganador (campos verificados del DTO)
            sets = []
            for i in range(1, 6):
                a, b = str(r.get(f"ScoreSet{i}A", "") or "").strip(), \
                    str(r.get(f"ScoreSet{i}B", "") or "").strip()
                if not a or not b:
                    break
                try:
                    ga, gb = int(float(a)), int(float(b))
                except ValueError:
                    break
                sets.append((ga, gb) if winner_side == "A" else (gb, ga))
            if not sets:
                n_skipped += 1
                continue                      # F sin marcador: no se inventa
            texto = " ".join(str(r.get(k, "") or "") for k in
                             ("ResultString", "ScoreString", "FreeText")).lower()
            retired = "ret" in texto or "retir" in texto
            winner, loser = (pA, pB) if winner_side == "A" else (pB, pA)
            tournament = str(r.get("TournamentName") or r.get("EventTitle")
                             or "WTA").strip()
            from betbot.tournaments import resolve_surface
            surf, _src, _info = resolve_surface("WTA", tournament)
            out.append({
                "date": ref.date().isoformat(), "tour": "WTA",
                "tournament": tournament,
                "surface": surf or "", "indoor": "false",
                "round": str(r.get("RoundID", "") or "").strip(),
                "best_of": "3",
                "winner": strip_seed(winner), "loser": strip_seed(loser),
                "score": " ".join(f"{a}-{b}" for a, b in sets),
                "status": "retired" if retired else "completed",
                "_level": "main" if str(r.get("DrawLevelType", "M")).strip().upper()
                          not in ("Q", "I", "ITF") else "other",
            })
        return out, n_raw, n_skipped
