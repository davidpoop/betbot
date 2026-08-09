"""Fuente "espn": scoreboard público JSON de ESPN — CALENDARIO AUTORITATIVO.

Es la fuente de calendario por defecto porque publica, por partido, lo que
BetBot necesita para no recomendar un evento ya jugado: identificador, hora de
inicio ISO en UTC y estado real (`status.type` con `state` pre|in|post,
`completed` y `description`).

Solo lectura, sin credenciales y sin scraping de HTML: se consume el mismo
endpoint JSON que alimenta las páginas públicas de ESPN, con cabeceras de
navegador normales, timeout y reintentos con backoff. No hay ninguna evasión
de bloqueos: si el servidor responde 403, el adaptador falla limpiamente y el
escáner entra en FAIL_CLOSED.

ESQUEMA REAL (verificado contra una respuesta capturada del endpoint, no de
memoria — ver tests/fixtures/espn_tennis_scoreboard.json). En tenis NO es
plano como en NBA/NFL; los adaptadores copiados de esos deportes devuelven
cero partidos en silencio:

    events[]                 -> un TORNEO (id "172-2026", date = rango del torneo)
      groupings[]            -> cuadro ("womens-singles", "mens-singles", ...)
        competitions[]       -> el PARTIDO
          id                 -> id de partido ("175549") — INESTABLE entre fetches
          date / startDate   -> "2026-06-03T09:10Z"  (ISO UTC, SIN segundos)
          status.type        -> {name: STATUS_FINAL, state: post, completed: true}
          format.regulation.periods -> 3 | 5  (al mejor de)
          round.displayName  -> "Quarterfinal" | "...Qualifying..."
          competitors[]      -> type "athlete" (athlete.displayName) o
                                type "team"    (roster.displayName, dobles)

Cuatro trampas verificadas, todas contempladas aquí:
1. `?dates=` NO filtra en tenis: ESPN devuelve torneos enteros que solapan la
   ventana. Se recorta en el cliente.
2. Los Grand Slams salen IDÉNTICOS en `atp` y en `wta` con las cinco
   groupings: sin repartir por slug se duplicarían todos sus partidos.
3. `competitions[].id` es inestable entre peticiones (mismo partido con id
   distinto), así que el `event_id` se deriva de forma determinista
   (torneo + día UTC + pareja) y el id crudo queda solo como metadato.
4. En dobles NO existe `competitor["athlete"]`; el nombre vive en
   `competitor["roster"]["displayName"]`.

NOTA DE ENTORNO: la red del contenedor de desarrollo bloquea todos los
dominios de terceros en el CONNECT del proxy (`CONNECT tunnel failed, 403`,
antes del handshake TLS: ESPN nunca llega a ver la petición). En un Mac con
salida normal a Internet el adaptador funciona; aquí el escáner lo refleja
honestamente y entra en FAIL_CLOSED.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               mark_placeholder_times, normalize_status, strip_seed)
from betbot.schemas import OddsQuote

BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis/{league}/scoreboard"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# reparto disjunto de cuadros por liga: sin esto los Grand Slams se duplican
SINGLES_SLUG = {"ATP": "mens-singles", "WTA": "womens-singles"}
# nombres de relleno del cuadro antes del orden del día: nunca son un partido
PLACEHOLDERS = {"tbd", "tba", "bye", "qualifier", "walkover", ""}
_ANOMALY_RX = re.compile(r"(?i)(retired|\bret\b\.?|walkover|\bw/o\b|default)")


def _short_name(full: str) -> str:
    """"Carlos Alcaraz" -> "Alcaraz C." (formato Tennis-Data)."""
    parts = [p for p in str(full).split() if p]
    if len(parts) < 2:
        return full
    return " ".join(parts[1:]) + " " + parts[0][0] + "."


def _parse_utc(raw) -> datetime | None:
    """ISO de ESPN ("2026-06-03T09:10Z", sin segundos) -> datetime UTC tz-aware."""
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def espn_status(comp: dict) -> str:
    """Estado canónico del bloque `status` de ESPN.

    Orden: anomalías por texto (ESPN marca el walkover como STATUS_FINAL con
    shortDetail 'W/O') -> completed -> state/name/description.
    """
    stype = (comp.get("status") or {}).get("type") or {}
    text = " ".join(str(stype.get(k, "")) for k in
                    ("description", "detail", "shortDetail"))
    if _ANOMALY_RX.search(text):
        return "walkover" if re.search(r"(?i)(walkover|w/o)", text) else "completed"
    if stype.get("completed") is True:
        return "completed"
    for field in ("state", "name", "description"):
        st = normalize_status(stype.get(field))
        if st != "unknown":
            return st
    return "unknown"


def _competitor_name(c: dict) -> tuple[str, bool]:
    """(nombre, es_dobles). En dobles NO hay 'athlete': el nombre va en 'roster'."""
    if str(c.get("type", "")).lower() == "team" or "roster" in c:
        roster = c.get("roster") or {}
        return str(roster.get("displayName") or ""), True
    ath = c.get("athlete") or {}
    return str(ath.get("displayName") or ath.get("shortName") or ""), False


class EspnFeed:
    name = "espn"
    supported_tours = frozenset({"ATP", "WTA"})
    markets = ["match_winner"]
    authoritative = True         # publica id + hora de inicio + estado

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl = ttl_seconds
        self._odds: list[OddsQuote] = []

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        now = datetime.now(timezone.utc)
        st = SourceStatus(name=self.name, ok=False, authoritative=True,
                          fetched_at=now.isoformat())
        d0 = now.strftime("%Y%m%d")
        d1 = (now + timedelta(hours=window_hours)).strftime("%Y%m%d")
        dates = d0 if d1 == d0 else f"{d0}-{d1}"
        out: list[FeedMatch] = []
        self._odds = []
        errors: list[str] = []
        n_raw = n_out_of_window = 0
        # ESPN ignora ?dates= en tenis: se recorta en el cliente con holgura de 1 día
        lo, hi = now - timedelta(days=1), now + timedelta(hours=window_hours) + timedelta(days=1)
        for league, tour in (("atp", "ATP"), ("wta", "WTA")):
            url = f"{BASE.format(league=league)}?dates={dates}&limit=300"
            try:
                data = cache.get_json(url, ttl_seconds=self.ttl, headers=HEADERS)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{league}: {exc}")
                continue
            got, raw, oow = self.parse_scoreboard(data, tour, now.isoformat(), lo, hi)
            out.extend(got)
            n_raw += raw
            n_out_of_window += oow
        # ok solo si al menos una liga respondió: si ambas fallan no hay calendario
        st.ok = len(errors) < 2
        st.n_items = len(out)
        st.error = "; ".join(errors)
        if st.ok:
            st.notes.append(f"{n_raw} partidos individuales leídos; {len(out)} en ventana "
                            f"({n_out_of_window} fuera, recorte en cliente)")
            st.notes.append("ESPN no publica cuotas ni superficie en tenis")
        return out, st

    @classmethod
    def parse_scoreboard(cls, data: dict, tour: str, observed_at: str,
                         lo: datetime | None = None, hi: datetime | None = None
                         ) -> tuple[list[FeedMatch], int, int]:
        """(partidos, leídos, fuera_de_ventana) de una respuesta del scoreboard."""
        out: list[FeedMatch] = []
        n_raw = n_oow = 0
        want_slug = SINGLES_SLUG.get(tour, "")
        for ev in data.get("events", []) or []:
            tournament = str(ev.get("name") or "")
            tid = str(ev.get("id", ""))
            # en tenis los partidos viven en groupings[].competitions[]
            for grouping in ev.get("groupings", []) or []:
                slug = str((grouping.get("grouping") or {}).get("slug", ""))
                # reparto disjunto atp/wta: evita duplicar los Grand Slams
                if slug and slug != want_slug:
                    continue
                for comp in grouping.get("competitions", []) or []:
                    n_raw += 1
                    fm = cls._parse_competition(comp, tid, tournament, tour, observed_at)
                    if fm is None:
                        continue
                    if lo and hi and fm.scheduled_at_utc is not None \
                            and not (lo <= fm.scheduled_at_utc <= hi):
                        n_oow += 1
                        continue
                    out.append(fm)
            # defensa: si algún día ESPN aplanara el esquema, no perder los partidos
            for comp in ev.get("competitions", []) or []:
                n_raw += 1
                fm = cls._parse_competition(comp, tid, tournament, tour, observed_at)
                if fm is not None and not any(x.pair_key == fm.pair_key for x in out):
                    out.append(fm)
        mark_placeholder_times(out)
        return out, n_raw, n_oow

    @classmethod
    def _parse_competition(cls, comp: dict, tid: str, tournament: str, tour: str,
                           observed_at: str) -> FeedMatch | None:
        competitors = comp.get("competitors", []) or []
        if len(competitors) != 2:
            return None
        (n1, d1), (n2, d2) = (_competitor_name(c) for c in
                              sorted(competitors, key=lambda c: c.get("order", 0)))
        is_doubles = d1 or d2 or "/" in n1 or "/" in n2
        if not n1 or not n2 or n1.strip().lower() in PLACEHOLDERS \
                or n2.strip().lower() in PLACEHOLDERS:
            return None                       # TBD/Bye/Qualifier: aún no es un partido
        start = _parse_utc(comp.get("date") or comp.get("startDate"))
        status = espn_status(comp)
        rnd = str((comp.get("round") or {}).get("displayName", ""))
        periods = ((comp.get("format") or {}).get("regulation") or {}).get("periods")
        try:
            best_of = int(periods) if periods in (3, 5, "3", "5") else None
        except (TypeError, ValueError):
            best_of = None
        p1, p2 = _short_name(n1), _short_name(n2)
        # id ESTABLE: ni el id de competición de ESPN (cambia entre peticiones)
        # ni la fecha (un cambio de horario no puede crear un evento nuevo)
        from betbot.canonical.names import canonical_key
        a, b = sorted([canonical_key(p1), canonical_key(p2)])
        return FeedMatch(
            date=(start.date() if start else datetime.now(timezone.utc).date()),
            tour=tour, tournament=tournament,
            player1=strip_seed(p1), player2=strip_seed(p2),
            level=classify_level(tournament),
            is_doubles=bool(is_doubles),
            is_qualifying="qualif" in rnd.lower(),
            round=rnd,
            best_of=best_of or (5 if (tour == "ATP" and is_grand_slam(tournament)) else 3),
            surface=None,                     # ESPN no publica superficie
            source="espn",
            event_id=f"espn:{tid}:{a}__{b}",
            scheduled_at_utc=start, status=status,
            source_updated_at=observed_at, start_tz="UTC",
            authoritative=True,
            # ESPN publica linescores y `completed`: su estado es verificable
            trust_tier="live_verified",
            raw={"status": comp.get("status"), "round": comp.get("round")})

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        """ESPN no publica cuotas en tenis (verificado sobre respuesta real);
        se mantiene el método por contrato de OddsSource."""
        st = SourceStatus(name=self.name, ok=True,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        st.notes.append("ESPN no publica cuotas en tenis: solo calendario y estado")
        return [], st
