"""`betbot feeds oddsapi-scores-probe` — UNA prueba live de /v4/.../scores/.

Mide con la clave real (BETBOT_ODDS_API_KEY, solo entorno) qué devuelve el
endpoint de scores para tenis: campos, granularidad del marcador y coste
EXACTO en créditos (headers x-requests-used/remaining antes y después).
No emite señales, no escribe en canonical, no hace bucles: /sports (0
créditos) + UNA llamada /scores (2 créditos con daysFrom).

Guarda un fixture ANONIMIZADO (jugadores -> "Jugador A1"/"Jugador B1") en
tests/fixtures/oddsapi_scores_snapshot.json. La clave jamás se imprime y toda
URL citada pasa por cache.redact.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from betbot.config import REPO_ROOT
from betbot.feeds.cache import redact
from betbot.feeds.oddsapi_scores import _sets_from_scores

BASE = "https://api.the-odds-api.com/v4"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "oddsapi_scores_snapshot.json"


def _get(url: str) -> tuple[object, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "betbot/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        heads = {k.lower(): v for k, v in resp.headers.items()}
    return data, heads


def _quota(heads: dict) -> str:
    used = heads.get("x-requests-used", "?")
    remaining = heads.get("x-requests-remaining", "?")
    last = heads.get("x-requests-last", "?")
    return f"usados={used} · restantes={remaining} · coste_última={last}"


def _anonymize(events: list) -> list:
    names: dict[str, str] = {}

    def alias(n: str, side: str, i: int) -> str:
        if n not in names:
            names[n] = f"Jugador {side}{i}"
        return names[n]

    out = []
    for i, ev in enumerate(events, 1):
        e = dict(ev)
        h, a = str(e.get("home_team") or ""), str(e.get("away_team") or "")
        e["home_team"] = alias(h, "A", i) if h else h
        e["away_team"] = alias(a, "B", i) if a else a
        if isinstance(e.get("scores"), list):
            e["scores"] = [{**s, "name": names.get(str(s.get("name")), str(s.get("name")))}
                           for s in e["scores"]]
        e["id"] = f"anon_{i:02d}"
        out.append(e)
    return out


def run_probe(cfg: dict, days_from: int = 3, sport: str = "") -> str:
    key = os.environ.get("BETBOT_ODDS_API_KEY", "") or \
        (cfg.get("feeds", {}).get("odds_api_key") or "")
    if not key:
        return ("Falta BETBOT_ODDS_API_KEY en el entorno (o .env). "
                "No se ha hecho ninguna petición.")
    days_from = max(1, min(3, int(days_from)))
    L = [f"SONDEO /scores de The Odds API — {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
         f"daysFrom={days_from} (rango válido 1-3; coste 2 créditos por llamada /scores)", ""]
    try:
        sports, heads = _get(f"{BASE}/sports/?apiKey={key}")
    except Exception as exc:  # noqa: BLE001
        return "\n".join(L + [f"FALLO /sports: {redact(str(exc))}"])
    L.append(f"/sports (0 créditos) · cuota: {_quota(heads)}")
    tennis = [(s["key"], s.get("title", ""), bool(s.get("active")))
              for s in sports if str(s.get("key", "")).startswith("tennis")]
    L.append(f"sport keys de tenis: {len(tennis)}")
    for k, t, act in tennis:
        L.append(f"  {'●' if act else '○'} {k}  ({t})")
    skey = sport or next((k for k, _t, act in tennis
                          if act and k.startswith("tennis_atp")), "")
    if not skey:
        return "\n".join(L + ["", "Sin sport key ATP activa ahora mismo: no se gasta nada más."])

    L += ["", f"→ GET /sports/{skey}/scores/?daysFrom={days_from}   (UNA llamada)"]
    try:
        events, heads = _get(f"{BASE}/sports/{skey}/scores/?daysFrom={days_from}&apiKey={key}")
    except Exception as exc:  # noqa: BLE001
        return "\n".join(L + [f"FALLO /scores: {redact(str(exc))}"])
    L.append(f"cuota tras /scores: {_quota(heads)}")
    if not isinstance(events, list):
        return "\n".join(L + [f"payload inesperado: {type(events).__name__}"])

    n_comp = sum(1 for e in events if e.get("completed"))
    L += ["", f"eventos: {len(events)} (completed={n_comp}, live/upcoming={len(events) - n_comp})", ""]
    verdicts = {"A": 0, "B": 0, "otro": 0}
    for ev in events[:12]:
        sc = ev.get("scores")
        sets, why = _sets_from_scores(sc) if sc else ([], "scores=null")
        if sets:
            verdicts["A"] += 1
            g = f"CASO A: sets reconstruibles {sets}"
        elif "agregado" in why:
            verdicts["B"] += 1
            g = f"CASO B: {why}"
        else:
            verdicts["otro"] += 1
            g = f"otro: {why}"
        L.append(f"  {str(ev.get('commence_time', ''))[:16]} · completed={ev.get('completed')} · "
                 f"{ev.get('home_team')} vs {ev.get('away_team')}")
        L.append(f"      scores={json.dumps(sc, ensure_ascii=False)}")
        L.append(f"      → {g}")
    L += ["", f"VEREDICTO GRANULARIDAD sobre {min(len(events), 12)} eventos: "
              f"A(sets)={verdicts['A']} · B(agregado)={verdicts['B']} · otro={verdicts['otro']}"]
    if verdicts["A"] == 0:
        L.append("⇒ CASO B: sin marcador por sets, /scores NO puede alimentar resultados "
                 "canónicos (el adaptador oddsapi_scores devolverá 0 filas, por diseño).")
    else:
        L.append("⇒ CASO A detectado: el adaptador oddsapi_scores producirá filas canónicas "
                 "con marcador completo (cobertura PARCIAL, sin avanzar frescura global).")

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sport_key": skey, "days_from": days_from,
        "note": "payload real anonimizado; sin clave; ids sustituidos",
        "events": _anonymize(events[:10]),
    }, indent=1, ensure_ascii=False))
    L += ["", f"fixture anonimizado → {FIXTURE.relative_to(REPO_ROOT)}",
          "coste total del sondeo: 0 (/sports) + 2 (/scores) = 2 créditos"]
    return "\n".join(L)
