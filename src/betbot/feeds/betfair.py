"""Betfair Exchange API (España) — EXCLUSIVAMENTE lectura de mercados.

Operaciones permitidas: listEvents, listMarketCatalogue, listMarketBook.
`_call` rechaza cualquier otra operación por construcción: este módulo no
contiene (ni contendrá) rutas de consulta de saldo ni de colocación,
modificación o cancelación de apuestas.

Credenciales SOLO por variables de entorno (nunca en ficheros/logs/ledger):
  BETFAIR_APP_KEY   — App Key del panel de desarrollador de Betfair
  BETFAIR_USERNAME  — usuario de la cuenta betfair.es
  BETFAIR_PASSWORD  — contraseña
Sin las tres, el adaptador queda inactivo (estado visible en `betbot feeds`).

Con la Delayed App Key (gratuita) los precios llegan con retardo del exchange;
la Live App Key requiere activación de pago en Betfair. El catálogo de
mercados por evento se consulta SIEMPRE en vivo (sin lista cerrada asumida).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from betbot.canonical.names import canonical_key
from betbot.feeds.base import FeedMatch, SourceStatus
from betbot.feeds.structured_odds import (CanonicalPrice, EventMarkets, map_market,
                                          summarize_catalogue)

IDENTITY_URL = "https://identitysso.betfair.es/api/login"
API_BASE = "https://api.betfair.com/exchange/betting/rest/v1.0/"
TENNIS_EVENT_TYPE = "2"
READ_ONLY_OPS = ("listEvents", "listMarketCatalogue", "listMarketBook")
_BACKOFF = [1.0, 2.0, 4.0]


class BetfairExchangeProvider:
    name = "betfair"
    supported_tours = frozenset({"ATP", "WTA"})
    bookmaker = "betfair_es"

    def __init__(self, app_key: str | None = None, username: str | None = None,
                 password: str | None = None, timeout: int = 20) -> None:
        self.app_key = app_key or os.environ.get("BETFAIR_APP_KEY", "")
        self.username = username or os.environ.get("BETFAIR_USERNAME", "")
        self.password = password or os.environ.get("BETFAIR_PASSWORD", "")
        self.timeout = timeout
        self._token: str | None = None
        self._token_at: float = 0.0

    def active(self) -> bool:
        return bool(self.app_key and self.username and self.password)

    # ------------------------------------------------------------------
    # transporte (solo lectura, con backoff)
    # ------------------------------------------------------------------
    def _login(self) -> str:
        if self._token and (time.time() - self._token_at) < 3600 * 4:
            return self._token
        body = urllib.parse.urlencode({"username": self.username,
                                       "password": self.password}).encode()
        req = urllib.request.Request(
            IDENTITY_URL, data=body, method="POST",
            headers={"X-Application": self.app_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "SUCCESS" or not data.get("token"):
            raise RuntimeError(f"login betfair.es rechazado: {data.get('error', 'sin detalle')}")
        self._token, self._token_at = data["token"], time.time()
        return self._token

    def _call(self, op: str, payload: dict) -> list | dict:
        if op not in READ_ONLY_OPS:
            raise PermissionError(f"operación no permitida (solo lectura): {op}")
        token = self._login()
        body = json.dumps(payload).encode()
        last: Exception | None = None
        for wait in [0.0] + _BACKOFF:
            if wait:
                time.sleep(wait)
            req = urllib.request.Request(
                API_BASE + op + "/", data=body, method="POST",
                headers={"X-Application": self.app_key, "X-Authentication": token,
                         "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except OSError:
                    pass
                if "INVALID_SESSION" in detail or exc.code == 401:
                    self._token = None
                    token = self._login()
                last = RuntimeError(f"{op} HTTP {exc.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError) as exc:
                last = exc
        raise RuntimeError(f"betfair {op} falló tras reintentos: {last}")

    # ------------------------------------------------------------------
    # enlace evento <-> partido del feed
    # ------------------------------------------------------------------
    @staticmethod
    def _link_events(events: list[dict], matches: list[FeedMatch]
                     ) -> dict[str, tuple[FeedMatch, bool]]:
        """{event_id: (match, invertido)} — invertido=True si el jugador 1 de
        Betfair es el player2 del feed."""
        out: dict[str, tuple[FeedMatch, bool]] = {}
        def sides(name: str) -> tuple[str, str] | None:
            for sep in (" v ", " vs ", " @ "):
                if sep in name:
                    l, r = name.split(sep, 1)
                    return l.strip(), r.strip()
            return None
        by_pair = {}
        for m in matches:
            k1, k2 = canonical_key(m.player1), canonical_key(m.player2)
            by_pair[frozenset((k1.rsplit("_", 1)[0], k2.rsplit("_", 1)[0]))] = m
        for ev in events:
            e = ev.get("event", ev)
            nm = str(e.get("name", ""))
            pair = sides(nm)
            if not pair:
                continue
            sl = canonical_key(pair[0]).rsplit("_", 1)[0] if "_" in canonical_key(pair[0]) else canonical_key(pair[0])
            sr = canonical_key(pair[1]).rsplit("_", 1)[0] if "_" in canonical_key(pair[1]) else canonical_key(pair[1])
            m = by_pair.get(frozenset((sl, sr)))
            if m is None:
                continue
            s1 = canonical_key(m.player1).rsplit("_", 1)[0]
            inverted = sl != s1 and sr == s1
            out[str(e.get("id"))] = (m, inverted)
        return out

    def _catalogue_for(self, event_ids: list[str]) -> list[dict]:
        cats: list[dict] = []
        for i in range(0, len(event_ids), 10):
            chunk = event_ids[i:i + 10]
            cats.extend(self._call("listMarketCatalogue", {
                "filter": {"eventTypeIds": [TENNIS_EVENT_TYPE], "eventIds": chunk},
                "maxResults": "200",
                "marketProjection": ["EVENT", "MARKET_DESCRIPTION",
                                     "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            }))
        return cats

    def _events_window(self, hours: int = 48) -> list[dict]:
        now = datetime.now(timezone.utc)
        return self._call("listEvents", {
            "filter": {"eventTypeIds": [TENNIS_EVENT_TYPE],
                       "marketStartTime": {
                           "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "to": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")}},
        })

    # ------------------------------------------------------------------
    # API del protocolo StructuredTennisOddsProvider
    # ------------------------------------------------------------------
    def list_markets(self, matches: list[FeedMatch]
                     ) -> tuple[list[EventMarkets], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.active():
            st.error = ("adaptador inactivo: faltan BETFAIR_APP_KEY / "
                        "BETFAIR_USERNAME / BETFAIR_PASSWORD")
            return [], st
        try:
            events = self._events_window()
            linked = self._link_events(events, matches)
            out: list[EventMarkets] = []
            if linked:
                cats = self._catalogue_for(list(linked))
                by_event: dict[str, list[dict]] = {}
                for c in cats:
                    eid = str((c.get("event") or {}).get("id", ""))
                    entry = {"marketType": (c.get("description") or {}).get("marketType", ""),
                             "marketName": c.get("marketName", ""),
                             "runnerNames": [r.get("runnerName", "")
                                             for r in c.get("runners", [])]}
                    by_event.setdefault(eid, []).append(entry)
                for eid, (m, inverted) in linked.items():
                    p1, p2 = (m.player2, m.player1) if inverted else (m.player1, m.player2)
                    em = summarize_catalogue(eid, f"{p1} v {p2}", p1, p2,
                                             by_event.get(eid, []), best_of=m.best_of)
                    out.append(em)
            st.ok = True
            st.n_items = len(out)
            st.notes.append(f"eventos tenis en ventana: {len(events)}; enlazados: {len(linked)}")
            return out, st
        except Exception as exc:  # noqa: BLE001 - el fallo se reporta, no detiene el scan
            st.error = str(exc)
            return [], st

    def fetch_prices(self, matches: list[FeedMatch]
                     ) -> tuple[list[CanonicalPrice], SourceStatus]:
        st = SourceStatus(name=self.name, ok=False,
                          fetched_at=datetime.now(timezone.utc).isoformat())
        if not self.active():
            st.error = ("adaptador inactivo: faltan BETFAIR_APP_KEY / "
                        "BETFAIR_USERNAME / BETFAIR_PASSWORD")
            return [], st
        try:
            events = self._events_window()
            linked = self._link_events(events, matches)
            if not linked:
                st.ok = True
                st.notes.append("sin eventos enlazables en la ventana")
                return [], st
            cats = self._catalogue_for(list(linked))
            # mapa marketId -> contexto de mapeo dinámico
            ctx: dict[str, dict] = {}
            for c in cats:
                eid = str((c.get("event") or {}).get("id", ""))
                if eid not in linked:
                    continue
                m, inverted = linked[eid]
                p1, p2 = (m.player2, m.player1) if inverted else (m.player1, m.player2)
                runners = {str(r.get("selectionId")): r.get("runnerName", "")
                           for r in c.get("runners", [])}
                mt = (c.get("description") or {}).get("marketType", "")
                mapped = map_market(mt, c.get("marketName", ""),
                                    list(runners.values()), p1, p2, best_of=m.best_of)
                ctx[str(c.get("marketId"))] = {
                    "event_id": eid, "market_type": mt, "runners": runners,
                    "mapped": mapped, "p1": m.player1, "p2": m.player2,
                    "inverted": inverted}
            prices: list[CanonicalPrice] = []
            now_iso = datetime.now(timezone.utc).isoformat()
            ids = list(ctx)
            for i in range(0, len(ids), 40):
                books = self._call("listMarketBook", {
                    "marketIds": ids[i:i + 40],
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"],
                                        "virtualise": True}})
                for book in books:
                    mid = str(book.get("marketId"))
                    c = ctx.get(mid)
                    if not c:
                        continue
                    status = str(book.get("status", "OPEN")).lower()
                    in_play = bool(book.get("inplay", False))
                    for rb in book.get("runners", []):
                        rid = str(rb.get("selectionId"))
                        rname = c["runners"].get(rid, rid)
                        mk, sel = c["mapped"].get(rname, ("unmapped", ""))
                        # la selección viene en la perspectiva del evento Betfair;
                        # si el evento está invertido respecto al feed, se voltea
                        if c["inverted"] and sel in ("a", "b"):
                            sel = "b" if sel == "a" else "a"
                        backs = (rb.get("ex") or {}).get("availableToBack") or []
                        best = backs[0] if backs else None
                        rstatus = str(rb.get("status", "ACTIVE")).lower()
                        prices.append(CanonicalPrice(
                            provider=self.name, bookmaker=self.bookmaker,
                            event_id=c["event_id"], market_id=mid,
                            market_type_raw=c["market_type"], market_canonical=mk,
                            runner_raw=rname, selection=sel,
                            back_odds=float(best["price"]) if best else None,
                            back_size=float(best["size"]) if best else None,
                            timestamp=now_iso,
                            status="closed" if rstatus == "removed" else status,
                            in_play=in_play,
                            player_a=c["p1"], player_b=c["p2"]))
            st.ok = True
            st.n_items = len(prices)
            st.notes.append(f"eventos enlazados: {len(linked)}; mercados leídos: {len(ctx)}")
            return prices, st
        except Exception as exc:  # noqa: BLE001
            st.error = str(exc)
            return [], st
