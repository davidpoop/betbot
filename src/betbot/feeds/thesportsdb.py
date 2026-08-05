"""Fuente "thesportsdb": calendario ATP/WTA público y documentado, sin registro.

TheSportsDB publica una API REST gratuita y DOCUMENTADA
(https://www.thesportsdb.com/free_sports_api) cuyo uso automatizado está
previsto por el propio proveedor: la clave gratuita es un valor público fijo
que aparece en su documentación, igual para todo el mundo — no hay alta, ni
cuenta, ni secreto que guardar. Por eso viaja en `config feeds.thesportsdb_key`
como un parámetro normal y NO como credencial.

Da por partido lo que exige la puerta prepartido: idEvent, fecha-hora completa
(`strTimestamp` en UTC, con respaldo `dateEvent` + `strTime`), estado
(`strStatus`) y los dos jugadores (`strHomeTeam`/`strAwayTeam`; el tenis se
modela como equipos de un jugador).

Dos limitaciones reales, tratadas de forma conservadora:
- `strStatus` NO está normalizado y no publica un valor documentado para
  "cancelado". Solo se aceptan los valores reconocidos; cualquier otro se
  devuelve como `unknown`, y la puerta prepartido lo excluye (fail closed).
- Es una base alimentada por la comunidad: sus listados diarios pueden venir
  INCOMPLETOS. Por eso es FALLBACK de ESPN, nunca la fuente principal, y
  `betbot feeds calendar` permite comparar su cobertura con la de ESPN.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from betbot.feeds import cache
from betbot.feeds.base import (FeedMatch, SourceStatus, classify_level, is_grand_slam,
                               mark_placeholder_times, strip_seed)
from betbot.schemas import OddsQuote

BASE = "https://www.thesportsdb.com/api/v1/json/{key}/eventsday.php?d={day}&s=Tennis"
PUBLIC_FREE_KEY = "3"        # clave pública de la documentación (sin alta ni secreto)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betbot/0.1; herramienta local)",
           "Accept": "application/json"}

# valores observados en clientes reales; lo no reconocido queda `unknown`
_STATUS = {
    "ns": "scheduled", "not started": "scheduled", "scheduled": "scheduled",
    "in progress": "live", "live": "live", "playing": "live", "1h": "live",
    "match finished": "completed", "ft": "completed", "finished": "completed",
    "ended": "completed", "aet": "completed",
    "postponed": "postponed", "pst": "postponed",
    "cancelled": "cancelled", "canceled": "cancelled", "canc": "cancelled",
    "abandoned": "cancelled", "walkover": "walkover", "wo": "walkover",
    "retired": "completed", "delayed": "delayed",
}


def normalize_sportsdb_status(raw: str | None) -> str:
    """`strStatus` -> estado canónico. Tabla + respaldo por subcadenas (los
    valores no están normalizados aguas arriba). Desconocido -> `unknown`."""
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    if s in _STATUS:
        return _STATUS[s]
    for needle, state in (("finish", "completed"), ("ended", "completed"),
                          ("progress", "live"), ("live", "live"), ("playing", "live"),
                          ("postpon", "postponed"), ("cancel", "cancelled"),
                          ("abandon", "cancelled"), ("walkover", "walkover"),
                          ("retir", "completed"), ("delay", "delayed"),
                          ("not started", "scheduled"), ("schedul", "scheduled")):
        if needle in s:
            return state
    return "unknown"


def _short_name(full: str) -> str:
    parts = [p for p in str(full).split() if p]
    if len(parts) < 2:
        return str(full)
    return " ".join(parts[1:]) + " " + parts[0][0] + "."


def _parse_start(ev: dict) -> datetime | None:
    """`strTimestamp` (UTC) y, si falta, `dateEvent` + `strTime`."""
    raw = str(ev.get("strTimestamp") or "").strip()
    if not raw:
        day, tm = str(ev.get("dateEvent") or "").strip(), str(ev.get("strTime") or "").strip()
        if not day or not tm:
            return None                      # sin fecha-hora completa no vale
        raw = f"{day}T{tm}"
    s = raw.replace(" ", "T").replace("Z", "+00:00")
    if len(s) == 16:                          # "2026-08-05T15:00"
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TheSportsDBCalendar:
    name = "thesportsdb"
    authoritative = True         # publica id + fecha-hora completa + estado
    markets: list[str] = []

    def __init__(self, api_key: str | None = None, ttl_seconds: int = 900) -> None:
        self.key = str(api_key or PUBLIC_FREE_KEY)
        self.ttl = ttl_seconds

    def fetch_matches(self, window_hours: int = 48) -> tuple[list[FeedMatch], SourceStatus]:
        now = datetime.now(timezone.utc)
        st = SourceStatus(name=self.name, ok=False, authoritative=True,
                          fetched_at=now.isoformat())
        out: list[FeedMatch] = []
        errors: list[str] = []
        n_raw = n_skipped = 0
        lo, hi = now - timedelta(days=1), now + timedelta(hours=window_hours)
        d, last = now.date(), (now + timedelta(hours=window_hours)).date()
        while d <= last:
            url = BASE.format(key=self.key, day=d.isoformat())
            try:
                data = cache.get_json(url, ttl_seconds=self.ttl, headers=HEADERS)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{d}: {exc}")
                d += timedelta(days=1)
                continue
            got, raw, skipped = self.parse_day(data, now.isoformat(), lo, hi)
            out.extend(got)
            n_raw += raw
            n_skipped += skipped
            d += timedelta(days=1)
        st.ok = not errors or bool(out)
        st.n_items = len(out)
        st.error = "; ".join(errors[:3])
        if st.ok:
            st.notes.append(f"{n_raw} eventos de tenis leídos; {len(out)} ATP/WTA en ventana "
                            f"({n_skipped} descartados)")
            st.notes.append("cobertura alimentada por la comunidad: puede venir incompleta; "
                            "úsala como respaldo de ESPN, no como única fuente")
        return out, st

    @classmethod
    def parse_day(cls, data: dict, observed_at: str, lo: datetime | None = None,
                  hi: datetime | None = None) -> tuple[list[FeedMatch], int, int]:
        out: list[FeedMatch] = []
        n_raw = n_skipped = 0
        for ev in (data or {}).get("events") or []:      # 'events' puede ser null
            n_raw += 1
            league = str(ev.get("strLeague") or "")
            tour = "WTA" if "WTA" in league.upper() else ("ATP" if "ATP" in league.upper() else "")
            if not tour:
                n_skipped += 1
                continue
            p1_raw = str(ev.get("strHomeTeam") or "").strip()
            p2_raw = str(ev.get("strAwayTeam") or "").strip()
            if not p1_raw or not p2_raw:
                n_skipped += 1
                continue
            if "/" in p1_raw or "/" in p2_raw:           # dobles
                n_skipped += 1
                continue
            start = _parse_start(ev)
            if start is None:
                n_skipped += 1
                continue                                  # sin fecha-hora completa no entra
            if lo and hi and not (lo <= start <= hi):
                n_skipped += 1
                continue
            tournament = str(ev.get("strEvent") or league).strip()
            rnd = str(ev.get("strRound") or "").strip()
            p1, p2 = strip_seed(_short_name(p1_raw)), strip_seed(_short_name(p2_raw))
            from betbot.canonical.names import canonical_key
            a, b = sorted([canonical_key(p1), canonical_key(p2)])
            out.append(FeedMatch(
                date=start.date(), tour=tour, tournament=tournament,
                player1=p1, player2=p2,
                level=classify_level(league) if classify_level(league) != "main"
                else classify_level(tournament),
                is_doubles=False,
                is_qualifying="qualif" in rnd.lower(),
                round=rnd,
                best_of=5 if (tour == "ATP" and is_grand_slam(tournament)) else 3,
                surface=None,
                source="thesportsdb",
                event_id=f"tsdb:{ev.get('idEvent', '')}:{a}__{b}",
                scheduled_at_utc=start,
                status=normalize_sportsdb_status(ev.get("strStatus")),
                source_updated_at=observed_at, start_tz="UTC",
                authoritative=True, trust_tier="schedule_only", raw=dict(ev)))
        mark_placeholder_times(out)
        return out, n_raw, n_skipped

    def fetch_odds(self, matches: list[FeedMatch]) -> tuple[list[OddsQuote], SourceStatus]:
        st = SourceStatus(name=self.name, ok=True,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        st.notes.append("TheSportsDB no publica cuotas: solo calendario y estado")
        return [], st
