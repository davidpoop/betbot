"""Odds-API.io (v3) — adaptador de SONDEO, no de producción.

Objetivo único: medir en la máquina del usuario qué cobertura REAL ofrece el
plan gratuito para los partidos ATP/WTA de las próximas 48 h. No genera
señales, no toca modelos y no se registra como fuente de cuotas del escáner:
es una fuente ADICIONAL y sustituible que solo se activará si el sondeo
demuestra que aporta mercados de sets reales.

Credencial EXCLUSIVAMENTE por entorno: ODDS_API_IO_KEY. La clave nunca se
imprime, ni se guarda en el fixture, ni aparece en los recuentos de peticiones.

Superficie de la API (tomada de código de producción de terceros que la
consume, no de suposiciones):
    GET /v3/sports
    GET /v3/leagues?sport=tennis
    GET /v3/bookmakers
    GET /v3/events?sport=tennis&startsAfter=ISO&startsBefore=ISO&limit=N&live=false
    GET /v3/odds?eventId=ID&bookmakers=NOMBRE
    GET /v3/odds/multi?eventIds=A,B,C&bookmakers=NOMBRE
La autenticación va como parámetro `apiKey` en la consulta.

Las respuestas llegan con envoltorios variables (lista directa, o bajo `data`,
`events`, `results`, `items`, `odds`, `markets`, `bookmakers`), así que se
normalizan de forma defensiva. NADA se da por soportado si no aparece
literalmente en la respuesta.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.odds-api.io/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; betbot/0.1; herramienta local)",
           "Accept": "application/json"}
# cabeceras de cuota que publican habitualmente este tipo de APIs; se leen si
# existen y, si no, el recuento de restantes se declara como ESTIMADO
QUOTA_HEADERS = ("x-ratelimit-remaining", "x-requests-remaining", "x-ratelimit-limit",
                 "x-requests-used", "x-ratelimit-used", "ratelimit-remaining")


@dataclass
class RequestLog:
    """Contabilidad EXACTA de peticiones: una entrada por llamada realizada."""
    calls: list[dict] = field(default_factory=list)
    quota_headers: dict = field(default_factory=dict)

    @property
    def used(self) -> int:
        return len(self.calls)

    @property
    def ok(self) -> int:
        return sum(1 for c in self.calls if c["status"] == 200)

    def add(self, endpoint: str, params: dict, status, ms: float, note: str = "") -> None:
        safe = {k: v for k, v in params.items() if k != "apiKey"}
        self.calls.append({"endpoint": endpoint, "params": safe, "status": status,
                           "ms": round(ms, 1), "note": note})

    def remaining(self, daily_budget: int) -> tuple[int | None, str]:
        for h in ("x-ratelimit-remaining", "x-requests-remaining", "ratelimit-remaining"):
            if h in self.quota_headers:
                try:
                    return int(self.quota_headers[h]), f"declarado por la API ({h})"
                except (TypeError, ValueError):
                    pass
        return max(0, daily_budget - self.used), "ESTIMADO (presupuesto local − consumidas)"


def _strip_key(text: str) -> str:
    return re.sub(r"(apiKey=)[^&\s]+", r"\1<oculta>", str(text))


class OddsApiIoClient:
    """Cliente de solo lectura con contabilidad exacta de peticiones."""

    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL,
                 timeout: int = 20) -> None:
        self.key = api_key or os.environ.get("ODDS_API_IO_KEY", "")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.log = RequestLog()

    def active(self) -> bool:
        return bool(self.key)

    def get(self, endpoint: str, params: dict | None = None) -> tuple[object | None, str]:
        """(datos, error). Cada llamada cuenta exactamente una petición.
        Sin caché a propósito: el objetivo es medir el consumo real."""
        params = dict(params or {})
        params["apiKey"] = self.key
        qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                     if v is not None and str(v) != ""})
        url = f"{self.base}{endpoint}?{qs}"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                for h in QUOTA_HEADERS:
                    if resp.headers.get(h) is not None:
                        self.log.quota_headers[h] = resp.headers.get(h)
                self.log.add(endpoint, params, resp.status, (time.time() - t0) * 1000)
            try:
                return json.loads(body), ""
            except json.JSONDecodeError:
                return None, f"respuesta no JSON: {_strip_key(body[:200])}"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                pass
            self.log.add(endpoint, params, exc.code, (time.time() - t0) * 1000,
                         note=_strip_key(detail)[:160])
            return None, f"HTTP {exc.code}: {_strip_key(detail)}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.log.add(endpoint, params, "ERROR", (time.time() - t0) * 1000,
                         note=_strip_key(str(exc))[:160])
            return None, _strip_key(str(exc))


# ---------------------------------------------------------------------------
# normalización defensiva de envoltorios
# ---------------------------------------------------------------------------

def as_list(payload, keys=("data", "results", "events", "items", "odds",
                           "markets", "bookmakers")) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for k in keys:
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def event_players(ev: dict) -> tuple[str, str]:
    """Los dos jugadores, probando los nombres de campo observados."""
    def pick(*names):
        for n in names:
            v = ev.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                for k in ("name", "title"):
                    if isinstance(v.get(k), str) and v[k].strip():
                        return v[k].strip()
        return ""
    home = pick("home", "homeTeam", "home_team", "team1", "player1")
    away = pick("away", "awayTeam", "away_team", "team2", "player2")
    if not home or not away:
        comp = ev.get("competitors")
        if isinstance(comp, dict):
            home = home or str(comp.get("home", ""))
            away = away or str(comp.get("away", ""))
        elif isinstance(comp, list) and len(comp) == 2:
            def cname(c):
                return c if isinstance(c, str) else str((c or {}).get("name", ""))
            home, away = home or cname(comp[0]), away or cname(comp[1])
    return home, away


def event_start(ev: dict) -> datetime | None:
    for k in ("date", "startTime", "start_time", "commence_time", "startsAt", "time"):
        raw = ev.get(k)
        if not raw:
            continue
        s = str(raw).strip().replace(" ", "T").replace("Z", "+00:00")
        if len(s) == 16:
            s += ":00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            continue
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def event_tour(ev: dict) -> str:
    """ATP/WTA a partir de la liga; cadena vacía si no se puede afirmar."""
    parts = []
    for k in ("league", "competition", "tournament", "sport", "category"):
        v = ev.get(k)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            parts.extend(str(v.get(x, "")) for x in ("name", "slug", "title"))
    text = " ".join(parts).upper()
    if "WTA" in text:
        return "WTA"
    if "ATP" in text:
        return "ATP"
    return ""


def _name_of(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("name", "title", "market", "marketName", "key", "slug", "label", "id"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def extract_markets(payload) -> list[dict]:
    """Recorre la respuesta de cuotas y devuelve los mercados CRUDOS observados:
    [{bookmaker, market_raw, outcomes: [{name, odds}]}]. No interpreta nada:
    solo recolecta lo que la respuesta contiene realmente."""
    out: list[dict] = []

    def outcomes_of(node) -> list[dict]:
        res = []
        for key in ("outcomes", "selections", "runners", "options", "odds", "values"):
            seq = node.get(key) if isinstance(node, dict) else None
            if isinstance(seq, list):
                for o in seq:
                    if isinstance(o, dict):
                        price = None
                        for pk in ("price", "odds", "decimal", "value", "odd"):
                            v = o.get(pk)
                            if isinstance(v, (int, float)):
                                price = float(v)
                                break
                            if isinstance(v, str):
                                try:
                                    price = float(v)
                                    break
                                except ValueError:
                                    pass
                        res.append({"name": _name_of(o) or str(o.get("label", "")),
                                    "odds": price})
                    elif isinstance(o, (int, float)):
                        res.append({"name": "", "odds": float(o)})
                if res:
                    return res
            if isinstance(seq, dict):
                for k, v in seq.items():
                    if isinstance(v, (int, float)):
                        res.append({"name": str(k), "odds": float(v)})
                    elif isinstance(v, str):
                        try:
                            res.append({"name": str(k), "odds": float(v)})
                        except ValueError:
                            continue
                if res:
                    return res
        return res

    def walk(node, book=""):
        if isinstance(node, list):
            for x in node:
                walk(x, book)
            return
        if not isinstance(node, dict):
            return
        cur_book = book
        for bk in ("bookmaker", "bookmakerName", "book", "provider"):
            v = node.get(bk)
            if isinstance(v, str) and v.strip():
                cur_book = v.strip()
            elif isinstance(v, dict) and _name_of(v):
                cur_book = _name_of(v)
        mname = ""
        for mk in ("market", "marketName", "market_name", "name", "key", "type"):
            v = node.get(mk)
            if isinstance(v, str) and v.strip():
                mname = v.strip()
                break
        outs = outcomes_of(node)
        if mname and outs:
            out.append({"bookmaker": cur_book, "market_raw": mname, "outcomes": outs})
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                # diccionarios {nombre_de_mercado: {...}} sin campo 'market'
                if isinstance(v, dict) and not mname:
                    inner = outcomes_of(v)
                    if inner and isinstance(k, str):
                        out.append({"bookmaker": cur_book, "market_raw": k,
                                    "outcomes": inner})
                        continue
                walk(v, cur_book)

    walk(payload)
    # deduplica (bookmaker, mercado) conservando el primero
    seen, uniq = set(), []
    for m in out:
        k = (m["bookmaker"], m["market_raw"].lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(m)
    return uniq
