"""`betbot feeds oddsapiio-probe`: medición de la cobertura REAL del plan
gratuito de Odds-API.io para la jornada ATP/WTA actual.

Es un diagnóstico, no una integración: no evalúa modelos, no toca el ledger y
no emite ninguna señal. Su salida sirve para decidir si merece la pena añadir
Odds-API.io como fuente de cuotas de mercados de sets.

Presupuesto: el sondeo está diseñado para gastar unas pocas decenas de
peticiones y las cuenta una a una.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betbot.feeds.oddsapiio import (OddsApiIoClient, as_list, event_players,
                                    event_start, event_tour, extract_markets)
from betbot.feeds.oddsapiio_markets import CANONICAL, classify_all, supported_summary

DEFAULT_SAMPLE = 6           # partidos a los que se piden cuotas
BOOKMAKER_TRIALS = 8         # bookmakers distintos a tantear para descubrir el plan


def _fmt_calls(client: OddsApiIoClient, budget: int) -> list[str]:
    rem, how = client.log.remaining(budget)
    lines = [f"Peticiones consumidas en este sondeo: {client.log.used} "
             f"({client.log.ok} con éxito)",
             f"Peticiones restantes hoy: {rem}  [{how}]"]
    if client.log.quota_headers:
        lines.append("  cabeceras de cuota devueltas: "
                     + ", ".join(f"{k}={v}" for k, v in client.log.quota_headers.items()))
    else:
        lines.append("  la API no devolvió cabeceras de cuota: el restante es una "
                     "estimación sobre el presupuesto local")
    lines.append("  desglose:")
    for c in client.log.calls:
        p = {k: v for k, v in c["params"].items() if k not in ("apiKey",)}
        note = f"  · {c['note']}" if c.get("note") else ""
        lines.append(f"    {c['status']:>5}  {c['endpoint']:<14} {p}  {c['ms']:.0f} ms{note}")
    return lines


def _anonymise(payload, names: dict[str, str]):
    """Sustituye nombres de jugador por seudónimos y borra cualquier clave.
    Conserva mercados, selecciones y cuotas: es lo que los tests necesitan."""
    def fix(s: str) -> str:
        out = s
        for real, alias in names.items():
            if real:
                out = re.sub(re.escape(real), alias, out, flags=re.I)
        out = re.sub(r"(apiKey|api_key|token)=[^&\"\s]+", r"\1=<oculta>", out)
        return out

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()
                    if k.lower() not in ("apikey", "api_key", "token", "email", "user",
                                         "username", "account", "accountid")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, str):
            return fix(node)
        return node
    return walk(payload)


def run_probe(cfg: dict, hours: int = 48, sample: int = DEFAULT_SAMPLE,
              client: OddsApiIoClient | None = None) -> str:
    from betbot.config import resolve_path
    fcfg = (cfg.get("feeds", {}) or {}).get("oddsapiio", {}) or {}
    budget = int(fcfg.get("daily_request_budget", 500))
    client = client or OddsApiIoClient(base_url=fcfg.get("base_url")
                                       or "https://api.odds-api.io/v3")
    now = datetime.now(timezone.utc)
    L: list[str] = [f"SONDEO Odds-API.io — {now.isoformat(timespec='seconds')}",
                    f"Ventana: {hours} h · muestra objetivo: {sample} partidos",
                    "Este comando NO genera señales ni escribe en el ledger.", ""]
    if not client.active():
        L.append("INACTIVO: falta la variable de entorno ODDS_API_IO_KEY.")
        L.append("  export ODDS_API_IO_KEY='tu-clave'   # nunca se guarda en disco ni en logs")
        return "\n".join(L)

    # ---------------- 1. eventos ATP/WTA de las próximas 48 h ----------------
    L.append("── 1. Eventos de tenis en la ventana ──")
    params = {"sport": "tennis", "limit": 200, "live": "false",
              "startsAfter": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "startsBefore": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    data, err = client.get("/events", params)
    if err:
        L.append(f"  FALLO al pedir /events: {err}")
        L.append("")
        L.extend(_fmt_calls(client, budget))
        return "\n".join(L)
    raw_events = as_list(data)
    events = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        p1, p2 = event_players(ev)
        if not p1 or not p2 or "/" in p1 or "/" in p2:
            continue
        tour = event_tour(ev)
        start = event_start(ev)
        events.append({"id": ev.get("id") or ev.get("eventId") or ev.get("_id"),
                       "p1": p1, "p2": p2, "tour": tour, "start": start,
                       "status": str(ev.get("status", "")), "raw": ev})
    atp_wta = [e for e in events if e["tour"] in ("ATP", "WTA")]
    L.append(f"  {len(raw_events)} eventos devueltos · {len(events)} individuales · "
             f"{len(atp_wta)} identificables como ATP/WTA")
    if not atp_wta:
        L.append("  Ningún evento ATP/WTA identificable: el plan puede no cubrir tenis "
                 "de circuito principal, o la liga no viene etiquetada.")

    # ---------------- 2. muestra priorizada por el calendario ----------------
    L.append("")
    L.append("── 2. Muestra (prioridad: partidos confirmados por el calendario) ──")
    confirmed_pairs: set = set()
    try:
        from betbot.feeds.base import dedupe_matches
        from betbot.prematch import gate
        from betbot.scan import default_sources
        cals, _ = default_sources(cfg)
        cal_matches = []
        for src in cals:
            if not getattr(src, "authoritative", False):
                continue
            try:
                ms, st = src.fetch_matches(hours)
            except Exception:  # noqa: BLE001
                continue
            if st.ok:
                cal_matches.extend(ms)
        res = gate(dedupe_matches(cal_matches), cfg, window_hours=hours, now=now)
        confirmed_pairs = {m.pair_key[1:] for m in res.eligible}
        L.append(f"  calendario autoritativo: {len(confirmed_pairs)} partidos confirmados "
                 f"prepartido (no consume cuota de Odds-API.io)")
    except Exception as exc:  # noqa: BLE001
        L.append(f"  calendario no disponible ({exc}); se muestrea por orden de inicio")

    def is_confirmed(e) -> bool:
        from betbot.canonical.names import canonical_key
        a, b = sorted([canonical_key(e["p1"]), canonical_key(e["p2"])])
        return (a, b) in confirmed_pairs

    pool = sorted(atp_wta or events,
                  key=lambda e: (not is_confirmed(e), e["start"] or now))
    chosen = [e for e in pool if e["id"]][:sample]
    for e in chosen:
        mark = "✓confirmado" if is_confirmed(e) else "·sin confirmar"
        st = e["start"].isoformat() if e["start"] else "sin hora"
        L.append(f"  {mark}  [{e['tour'] or '?'}] {e['p1']} vs {e['p2']}  inicio {st}")
    if not chosen:
        L.append("  sin eventos con identificador: no se pueden pedir cuotas")
        L.append("")
        L.extend(_fmt_calls(client, budget))
        return "\n".join(L)

    # ---------------- 3. bookmakers que permite el plan ----------------
    L.append("")
    L.append("── 3. Bookmakers permitidos por el plan ──")
    bdata, berr = client.get("/bookmakers", {})
    catalogue = []
    if berr:
        L.append(f"  /bookmakers falló: {berr}")
    else:
        for b in as_list(bdata):
            name = b if isinstance(b, str) else (
                b.get("name") or b.get("title") or b.get("slug") or b.get("key")
                or b.get("id") if isinstance(b, dict) else None)
            if name:
                catalogue.append(str(name))
        L.append(f"  catálogo devuelto: {len(catalogue)} bookmakers")
        if catalogue:
            L.append("  muestra: " + ", ".join(catalogue[:20]))

    # se tantean uno a uno para ver CUÁLES responde realmente el plan
    allowed, denied = [], []
    probe_event = chosen[0]["id"]
    for name in (catalogue or [])[:BOOKMAKER_TRIALS]:
        od, oerr = client.get("/odds", {"eventId": probe_event, "bookmakers": name})
        if oerr:
            denied.append((name, oerr[:110]))
            continue
        mk = extract_markets(od)
        allowed.append((name, len(mk)))
    if allowed:
        L.append(f"  PERMITIDOS (respuesta con datos): "
                 + ", ".join(f"{n} ({k} mercados)" for n, k in allowed))
        L.append(f"  -> el plan gratuito habilita {len(allowed)} de los "
                 f"{min(len(catalogue), BOOKMAKER_TRIALS)} tanteados")
    else:
        L.append("  ninguno de los tanteados devolvió datos")
    for n, e in denied[:6]:
        L.append(f"  DENEGADO {n}: {e}")

    # ---------------- 4. mercados crudos + mapeo sin adivinar ----------------
    L.append("")
    L.append("── 4. Mercados CRUDOS por partido y bookmaker ──")
    books_to_use = [n for n, _ in allowed] or (catalogue[:2] if catalogue else [])
    all_classified: list[dict] = []
    fixture: dict = {"probe_version": 1, "captured_at": now.isoformat(),
                     "note": "respuesta anonimizada de Odds-API.io para tests",
                     "events": []}
    alias_map: dict[str, str] = {}
    for i, e in enumerate(chosen, 1):
        alias_map[e["p1"]] = f"Jugador A{i}"
        alias_map[e["p2"]] = f"Jugador B{i}"
        raw_all = []
        for book in books_to_use[:2]:            # el plan gratuito permite pocos
            od, oerr = client.get("/odds", {"eventId": e["id"], "bookmakers": book})
            if oerr:
                L.append(f"  [{e['p1']} vs {e['p2']}] {book}: FALLO {oerr[:110]}")
                continue
            mks = extract_markets(od)
            raw_all.append({"bookmaker": book, "payload": od})
            if not mks:
                L.append(f"  [{e['p1']} vs {e['p2']}] {book}: sin mercados en la respuesta")
                continue
            cls = classify_all(mks, e["p1"], e["p2"])
            all_classified.extend(cls)
            L.append(f"  [{e['p1']} vs {e['p2']}] {book}: {len(cls)} mercados")
            for c in cls:
                outs = ", ".join(f"{o['name'] or '?'}={o['odds']}" for o in c["outcomes"][:4])
                tag = (f"→ {c['canonical']}" if c["verdict"] == "mapeado"
                       else f"[{c['verdict']}]")
                L.append(f"      «{c['market_raw']}» {tag}")
                L.append(f"          selecciones: {outs}")
                if c["verdict"] != "mapeado":
                    L.append(f"          motivo: {c['why']}")
        fixture["events"].append({
            "tour": e["tour"], "start": e["start"].isoformat() if e["start"] else None,
            "player1": alias_map[e["p1"]], "player2": alias_map[e["p2"]],
            "responses": raw_all})

    # ---------------- 5. veredicto por contrato ----------------
    L.append("")
    L.append("── 5. Contratos de BetBot realmente presentes ──")
    sup = supported_summary(all_classified)
    for canon in CANONICAL:
        if canon in sup:
            info = sup[canon]
            L.append(f"  SOPORTADO  {canon}: visto {info['n_observado']} veces "
                     f"en {', '.join(info['bookmakers']) or '?'}")
            for ex in info["ejemplos"]:
                L.append(f"      ej.: {ex}")
        else:
            L.append(f"  NO PRESENTE {canon}: no apareció con cuota real en esta muestra")
    amb = [c for c in all_classified if c["verdict"] == "ambiguo"]
    unm = [c for c in all_classified if c["verdict"] == "no_mapeado"]
    if amb:
        L.append(f"  ambiguos (NO se dan por soportados): {len(amb)}")
        for c in amb[:8]:
            L.append(f"      «{c['market_raw']}» — {c['why']}")
    if unm:
        L.append(f"  sin contrato en BetBot: {len(unm)}")
        for c in unm[:8]:
            L.append(f"      «{c['market_raw']}»")

    # ---------------- 6. fixture anonimizado ----------------
    fixture_path = Path(resolve_path(cfg, "exports_dir")) / "oddsapiio_probe_fixture.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(_anonymise(fixture, alias_map), indent=2,
                                       ensure_ascii=False), encoding="utf-8")
    L.append("")
    L.append(f"Fixture anonimizado guardado en: {fixture_path}")
    L.append("  (sin clave, sin datos de cuenta y con los jugadores seudonimizados)")

    # ---------------- 7. contabilidad + política de polling ----------------
    L.append("")
    L.append("── 6. Contabilidad de peticiones ──")
    L.extend(_fmt_calls(client, budget))
    L.append("")
    L.extend(polling_policy(len(confirmed_pairs) or len(atp_wta), budget))
    return "\n".join(L)


def polling_policy(n_matches: int, budget: int = 500) -> list[str]:
    """Propuesta de cadencia que no supera `budget` peticiones al día."""
    n = max(1, n_matches)
    # escaneo general: 1 petición de eventos + 1 de cuotas por partido y bookmaker
    scans_per_day = 4
    books = 2
    general = scans_per_day * (1 + n * books)
    # watch: solo candidatas, cada 15 min durante 6 h -> 24 ciclos
    cycles = 24
    remaining = max(0, budget - general)
    per_candidate = cycles * books
    n_cand = remaining // per_candidate if per_candidate else 0
    return [
        "── 7. Política de polling propuesta (≤ %d peticiones/día) ──" % budget,
        f"  Supuesto: {n} partidos main tour al día y {books} bookmakers del plan.",
        f"  · Escaneo general {scans_per_day}×/día (cada 6 h): "
        f"{scans_per_day} × (1 evento + {n}×{books} cuotas) = {general} peticiones.",
        f"    Sirve para descubrir la jornada y ver qué mercados publica cada partido.",
        f"  · Watch de 15 min SOLO sobre candidatas, en las 6 h previas al inicio: "
        f"{cycles} ciclos × {books} bookmakers = {per_candidate} peticiones por candidata.",
        f"    Con {remaining} peticiones libres caben ≈ {n_cand} candidatas simultáneas.",
        "  · Reglas: no consultar partidos sin señal, parar el watch al comenzar el "
        "partido (ya lo hace la puerta prepartido), y respetar la caché TTL para no "
        "repetir peticiones dentro del mismo ciclo.",
        "  · Si la cuota diaria se agota, el escáner NO degrada a adivinar: "
        "simplemente deja de ofrecer los mercados de esa fuente.",
    ]
