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

import re
from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level,
                               mark_placeholder_times, strip_seed)
from betbot.schemas import OddsQuote

URL = "https://api.wtatennis.com/tennis/matches/global"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betbot/0.1; herramienta local)",
           "Accept": "application/json"}

# Solo los tres códigos confirmados de forma independiente; el resto -> unknown.
# Distribución observada sobre 25.952 partidos reales: F=25591, U=316, P=42,
# C=2, W=1 (C y W con duración 00:00:00 y marcador vacío: evidencia insuficiente
# para mapearlos, así que se quedan en `unknown` y la puerta los excluye).
MATCH_STATE = {"F": "completed", "P": "live", "U": "scheduled"}

_RESULT_D_RX = re.compile(r"\S\s+d\.?\s+\S")          # "X d Y" / "X d. Y"
_SET_TOKEN_RX = re.compile(r"^(\d{1,2})-(\d{1,2})(?:\((\d+)\))?$")


def _sets_from_scoresets(r: dict) -> list[tuple[int, int]]:
    """Sets desde ScoreSet{1..5}{A,B}, en orientación A/B del feed."""
    sets = []
    for i in range(1, 6):
        a = str(r.get(f"ScoreSet{i}A", "") or "").strip()
        b = str(r.get(f"ScoreSet{i}B", "") or "").strip()
        if not a or not b:
            break
        try:
            sets.append((int(float(a)), int(float(b))))
        except ValueError:
            break
    return sets


def _sets_from_scorestring(raw: str) -> list[tuple[int, int]]:
    """Sets desde ScoreString "6-4,7-6(4)" (orientado al GANADOR, igual que
    ResultString: verificado en payload real "[6]A. Smith d E. Schoppe
    6-4,7-6(4)"). Token no parseable -> lista vacía (no se adivina)."""
    out = []
    for tok in str(raw or "").replace(" ", "").split(","):
        if not tok:
            continue
        m = _SET_TOKEN_RX.match(tok)
        if not m:
            return []
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def _sets_won(sets: list[tuple[int, int]]) -> tuple[int, int]:
    """(sets del primer lado, del segundo) contando solo sets COMPLETOS."""
    w = l = 0
    for a, b in sets:
        hi, lo = max(a, b), min(a, b)
        if (hi >= 6 and hi - lo >= 2) or hi == 7:
            if a > b:
                w += 1
            else:
                l += 1
    return w, l


def has_completed_evidence(r: dict) -> tuple[bool, str]:
    """Función ÚNICA de "este partido ya terminó" para calendario, puerta
    prematch y sync de resultados — nunca reglas distintas por camino.

    Evidencia suficiente (cualquiera):
    - MatchState == F (código confirmado de finalizado);
    - ResultString con patrón "X d Y" (la WTA solo lo publica al acabar);
    - un lado con 2 sets COMPLETOS ganados (Bo3 terminado por definición);
    - Winner DECIDIDO ("A"/"B"/"1"/"2"; el feed publica "0" hasta que se
      decide — convención verificada) corroborado por marcador o duración
      (cubre retiradas con marcador parcial y MatchState rezagado).
    Un marcador PARCIAL (live) no basta: un partido en juego tiene sets
    incompletos y Winner="0", y MatchTimeTotal por sí solo tampoco (un
    partido en juego también acumula duración)."""
    if str(r.get("MatchState", "")).strip().upper() == "F":
        return True, "MatchState=F"
    rs = str(r.get("ResultString", "") or "").strip()
    if rs and _RESULT_D_RX.search(rs):
        return True, f"ResultString='{rs[:60]}'"
    sets = _sets_from_scoresets(r)
    if sets:
        w, l = _sets_won(sets)
        if max(w, l) >= 2:
            return True, f"{max(w, l)} sets completos ganados en ScoreSet*"
    ss = _sets_from_scorestring(r.get("ScoreString"))
    if ss:
        w, l = _sets_won(ss)
        if max(w, l) >= 2:
            return True, "2 sets completos ganados en ScoreString"
    if str(r.get("Winner", "") or "").strip().upper() in ("A", "B", "1", "2"):
        dur = str(r.get("MatchTimeTotal", "") or "").strip()
        if sets or ss or (dur and dur != "00:00:00"):
            return True, "Winner decidido + marcador/duración"
    return False, ""


def _winner_side(r: dict) -> str:
    """'A'/'B' o ''. Acepta las DOS convenciones reales del campo Winner
    ("A"/"B" y "1"/"2" — el consumidor Java verificado hace parseInt) y, si
    falta, deriva el ganador de los sets completos de ScoreSet*. Con datos
    contradictorios devuelve '' (no se inventa)."""
    raw = str(r.get("Winner", "") or "").strip().upper()
    by_field = {"A": "A", "1": "A", "B": "B", "2": "B"}.get(raw, "")
    sets = _sets_from_scoresets(r)
    by_sets = ""
    if sets:
        w, l = _sets_won(sets)
        if w >= 2 and w > l:
            by_sets = "A"
        elif l >= 2 and l > w:
            by_sets = "B"
    if by_field and by_sets and by_field != by_sets:
        return ""                    # contradicción: fuera, jamás adivinar
    return by_field or by_sets


def _sets_from_resultstring(raw: str) -> list[tuple[int, int]]:
    """Último recurso: los tokens de set dentro de ResultString
    ("[7]I. Swiatek d [10]M. Kostyuk 3-6,6-1,6-2" -> [(3,6),(6,1),(6,2)]),
    también orientados al ganador. Los nombres no contienen dígito-dígito y
    las cabezas de serie van entre corchetes, así que no hay falsos tokens."""
    s = str(raw or "")
    if not s or not _RESULT_D_RX.search(s):
        return []
    toks = re.findall(r"\b(\d{1,2})-(\d{1,2})(?:\((\d+)\))?\b", s)
    return [(int(a), int(b)) for a, b, _tb in toks]


def _tournament_identity(r: dict) -> tuple[str, dict]:
    """(nombre de torneo, metadatos) desde los campos REALES del payload.

    Busca, por orden: TournamentName, EventTitle, el objeto Tournament
    anidado (name/title/tournamentGroup), TournamentCity, Venue.name.
    CourtName NUNCA es un torneo ("Center Court" no identifica nada).
    Sin metadatos -> ("WTA", {}): un nombre genérico no puede fingir una
    identificación y la superficie se queda en inferred/unknown."""
    meta: dict = {}
    for key in ("TournamentName", "EventTitle"):
        v = str(r.get(key, "") or "").strip()
        if v:
            meta["from"] = key
            return v, meta
    t = r.get("Tournament")
    if isinstance(t, dict):
        for key in ("name", "Name", "title", "Title", "TournamentName"):
            v = str(t.get(key, "") or "").strip()
            if v:
                meta["from"] = f"Tournament.{key}"
                return v, meta
        g = t.get("tournamentGroup")
        if isinstance(g, dict):
            for key in ("name", "Name"):
                v = str(g.get(key, "") or "").strip()
                if v:
                    meta["from"] = f"Tournament.tournamentGroup.{key}"
                    return v, meta
        for key in ("city", "City"):
            v = str(t.get(key, "") or "").strip()
            if v:
                meta["from"] = f"Tournament.{key}"
                return v, meta
    for key in ("TournamentCity",):
        v = str(r.get(key, "") or "").strip()
        if v:
            meta["from"] = key
            return v, meta
    venue = r.get("Venue")
    if isinstance(venue, dict):
        v = str(venue.get("name", "") or venue.get("Name", "") or "").strip()
        if v:
            meta["from"] = "Venue.name"
            return v, meta
    return "WTA", meta


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
    supported_tours = frozenset({"WTA"})   # SOLO WTA: jamás cubre ATP
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
            tournament, _tmeta = _tournament_identity(r)
            rnd = str(r.get("RoundID") or "").strip()
            p1, p2 = strip_seed(p1), strip_seed(p2)
            a, b = sorted([canonical_key(p1), canonical_key(p2)])
            level = classify_level(tournament)
            if str(r.get("DrawLevelType", "")).strip().upper() in ("I", "ITF"):
                level = "itf"

            # el marcador se impone al código de estado — con la MISMA regla
            # que usa fetch_results (has_completed_evidence): un marcador
            # PARCIAL de un partido en juego ya no fuerza `completed`.
            status = normalize_match_state(r.get("MatchState"))
            played, why = has_completed_evidence(r)
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
                    "evidencia de partido jugado: " + why

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
        """Partidos con evidencia de FINALIZADO -> filas de la plantilla manual
        (marcador orientado al GANADOR).

        La evidencia es la MISMA que usa el calendario: `has_completed_evidence`
        (MatchState=F | ResultString "X d Y" | 2 sets completos ganados). El
        feed real publica el ganador como "1"/"2" (no "A"/"B") y a veces solo
        trae ResultString/ScoreString sin ScoreSet*: ambos casos entran ahora.
        Sin ganador claro o sin ningún marcador, la fila se descarta: nunca se
        inventa un resultado, y un marcador parcial (live) jamás cuenta."""
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
        from betbot.tournaments import resolve_surface
        for r in rows_in:
            if not isinstance(r, dict):
                continue
            n_raw += 1
            if str(r.get("DrawMatchType", "S")).strip().upper() not in ("S", ""):
                n_skipped += 1
                continue
            done, _why = has_completed_evidence(r)
            if not done:
                n_skipped += 1
                continue
            winner_side = _winner_side(r)
            if winner_side not in ("A", "B"):
                n_skipped += 1
                continue                      # sin ganador claro: no se inventa
            pA = _player_name(r.get("PlayerNameFirstA"), r.get("PlayerNameLastA"))
            pB = _player_name(r.get("PlayerNameFirstB"), r.get("PlayerNameLastB"))
            if not pA or not pB:
                n_skipped += 1
                continue
            ref = _parse_iso(r.get("MatchTimeStamp")) or _parse_iso(r.get("NotBeforeISOTime"))
            if ref is None or not (since <= ref.date() <= until):
                n_skipped += 1
                continue
            # marcador orientado al ganador: ScoreSet* (orientación A/B, se
            # reorienta) > ScoreString > tokens de ResultString (estos dos ya
            # vienen orientados al ganador)
            sets = _sets_from_scoresets(r)
            if sets and winner_side == "B":
                sets = [(b, a) for a, b in sets]
            if not sets:
                sets = _sets_from_scorestring(r.get("ScoreString"))
            if not sets:
                sets = _sets_from_resultstring(r.get("ResultString"))
            if not sets:
                n_skipped += 1
                continue                      # finalizado sin marcador: no se inventa
            texto = " ".join(str(r.get(k, "") or "") for k in
                             ("ResultString", "ScoreString", "FreeText")).lower()
            retired = "ret" in texto or "retir" in texto
            winner, loser = (pA, pB) if winner_side == "A" else (pB, pA)
            tournament, _tmeta = _tournament_identity(r)
            surf, _src, _info = resolve_surface("WTA", tournament)
            row = {
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
            }
            # ranking oficial si el payload lo trae (mismos nombres de columna
            # que el resto de fuentes de resultados; ausente -> vacío)
            rank_a = str(r.get("PlayerRankA", "") or r.get("RankA", "") or "").strip()
            rank_b = str(r.get("PlayerRankB", "") or r.get("RankB", "") or "").strip()
            if rank_a.isdigit() or rank_b.isdigit():
                w_rank, l_rank = (rank_a, rank_b) if winner_side == "A" else (rank_b, rank_a)
                row["winner_rank"] = w_rank if w_rank.isdigit() else ""
                row["loser_rank"] = l_rank if l_rank.isdigit() else ""
            out.append(row)
        return out, n_raw, n_skipped
