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
- Solo cubre WTA: es complemento de ESPN/TheSportsDB, no sustituto.
- Es un endpoint sin contrato publicado: puede cambiar sin aviso. Se consume
  con caché, ritmo bajo y User-Agent identificable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import FeedMatch, SourceStatus, classify_level, strip_seed
from betbot.schemas import OddsQuote

URL = "https://api.wtatennis.com/tennis/matches/global"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betbot/0.1; herramienta local)",
           "Accept": "application/json"}

# Solo los tres códigos confirmados de forma independiente; el resto -> unknown
MATCH_STATE = {"F": "completed", "P": "live", "U": "scheduled"}


def normalize_match_state(raw: str | None) -> str:
    return MATCH_STATE.get(str(raw or "").strip().upper(), "unknown")


def _player_name(first: str, last: str) -> str:
    """('Iga', 'Swiatek') -> 'Swiatek I.' (formato Tennis-Data)."""
    last, first = str(last or "").strip(), str(first or "").strip()
    if not last:
        return ""
    return f"{last} {first[:1]}." if first else last


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
            start = _parse_iso(r.get("MatchTimeStamp")) or _parse_iso(r.get("NotBeforeISOTime"))
            if start is None:
                n_skipped += 1
                continue                                  # sin hora completa no entra
            if lo and hi and not (lo <= start <= hi):
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
            out.append(FeedMatch(
                date=start.date(), tour="WTA", tournament=tournament,
                player1=p1, player2=p2, level=level, is_doubles=False,
                is_qualifying="q" in rnd.lower() and "qual" in rnd.lower(),
                round=rnd, best_of=3, surface=None, source="wta_official",
                event_id=f"wta:{r.get('MatchID', '')}:{start:%Y%m%d}:{a}__{b}",
                scheduled_at_utc=start,
                status=normalize_match_state(r.get("MatchState")),
                source_updated_at=observed_at, start_tz="UTC",
                authoritative=True))
        return out, n_raw, n_skipped

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=True,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        st.notes.append("la WTA no publica cuotas: solo calendario y estado")
        return [], st
