"""`betbot watch`: escaneo continuo con alertas deduplicadas.

- Repite `scan` cada N minutos (15 por defecto); sincroniza resultados en el
  primer ciclo y después cada ~6 h (cadencia real de las fuentes).
- En CADA ciclo revalida estado y hora de inicio: retira al instante la señal
  de un partido que empieza, se completa, se cancela o se aplaza, y congela su
  última observación prepartido en `artifacts/ledger/prematch_frozen.jsonl`.
  Nunca recomienda un evento en juego. Si el calendario cae: FAIL_CLOSED.
- Alerta en terminal (y notificación local best-effort) cuando:
  * aparece una señal nueva (fuerte/normal/experimental) — moneyline o sets;
  * una cuota cruza al alza su o_min (una oportunidad "vigilar" se activa);
  * una señal previa desaparece o caduca;
  * un partido publica cuota en un mercado que antes no tenía;
  * cambia la fuente con mejor cuota de una selección;
  * la fuente estructurada suspende un mercado;
  * cambia el estado OOD de un partido tras un sync de resultados.
- Sin alertas repetidas sin cambio material; historial de precios en el ledger.
- No ejecuta apuestas.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from betbot.config import resolve_path
from betbot.scan import run_scan

SIGNAL_STATES = ("fuerte", "normal", "experimental")


def signal_key(row: dict) -> str:
    return f"{row['match']}|{row['market']}|{row['selection']}|{row['bookmaker']}"


def extract_signals(rows: pd.DataFrame) -> dict[str, dict]:
    """Señales activas (clave -> info) del resultado de un escaneo."""
    out: dict[str, dict] = {}
    if rows is None or not len(rows):
        return out
    sig = rows[rows["state"].isin(SIGNAL_STATES)]
    for _, r in sig.iterrows():
        d = r.to_dict()
        out[signal_key(d)] = {"state": d["state"], "odds": d.get("odds"),
                              "ev_cons": d.get("ev_cons"), "selection_name": d.get("selection_name"),
                              "o_min": d.get("o_min")}
    return out


def detect_crossings(prev_odds: dict[str, float], rows: pd.DataFrame) -> list[dict]:
    """Cuotas que cruzan al alza su o_min respecto al ciclo anterior."""
    events: list[dict] = []
    if rows is None or not len(rows):
        return events
    watch = rows[rows["odds"].notna() & rows["o_min"].notna()]
    for _, r in watch.iterrows():
        key = signal_key(r.to_dict())
        prev = prev_odds.get(key)
        if prev is not None and prev < r["o_min"] <= r["odds"]:
            events.append({"key": key, "match": r["match"], "market": r["market"],
                           "selection_name": r["selection_name"], "odds": float(r["odds"]),
                           "o_min": float(r["o_min"]), "bookmaker": r["bookmaker"]})
    return events


def diff_alerts(prev_signals: dict[str, dict], new_signals: dict[str, dict],
                crossings: list[dict], already_alerted: set[str]) -> tuple[list[str], set[str]]:
    """Alertas de este ciclo (deduplicadas) y el nuevo conjunto de claves avisadas."""
    alerts: list[str] = []
    alerted = set(already_alerted)
    for key, info in new_signals.items():
        tag = f"new:{key}:{info['state']}"
        if tag not in alerted:
            alerts.append(f"🟢 SEÑAL {info['state'].upper()}: {key.split('|')[0]} · "
                          f"{info['selection_name']} @ {info['odds']} (EV_cons "
                          f"{100 * (info['ev_cons'] or 0):+.1f}%)")
            alerted.add(tag)
    for ev in crossings:
        tag = f"cross:{ev['key']}"
        if tag not in alerted:
            alerts.append(f"📈 CRUCE o_min: {ev['match']} · {ev['selection_name']} "
                          f"@ {ev['odds']:.2f} ≥ o_min {ev['o_min']:.2f} ({ev['bookmaker']})")
            alerted.add(tag)
    for key, info in prev_signals.items():
        if key not in new_signals:
            tag = f"gone:{key}"
            if tag not in alerted:
                alerts.append(f"⚪ SEÑAL DESAPARECIDA: {key.split('|')[0]} · "
                              f"{info['selection_name']} (era {info['state']})")
                alerted.add(tag)
    return alerts, alerted


def prematch_transitions(prev_conf: dict, cur_conf: dict, alerted: set[str],
                         now: datetime | None = None,
                         margin_minutes: float = 5.0) -> tuple[list[str], set[str], dict]:
    """Retirada inmediata de señales cuyo partido deja de ser prepartido.

    Devuelve (alertas, avisadas, congeladas) donde `congeladas` guarda la ÚLTIMA
    observación prepartido de cada evento retirado, para el registro. Nunca se
    recomienda un evento en juego: basta con que la hora de inicio haya pasado
    (con margen) o que el estado deje de ser scheduled/delayed.
    """
    from betbot.prematch import signal_still_prematch
    now = now or datetime.now(timezone.utc)
    alerts: list[str] = []
    seen = set(alerted)
    frozen: dict = {}
    for key, conf in prev_conf.items():
        cur = cur_conf.get(key)
        if cur is not None and signal_still_prematch(cur, now, margin_minutes):
            continue                      # sigue siendo prepartido: nada que hacer
        if cur is None and signal_still_prematch(conf, now, margin_minutes):
            continue                      # desapareció del calendario pero aún no empieza
        state = (cur or conf).get("status", "?")
        reason = ("comenzado" if state in ("scheduled", "delayed") else state)
        tag = f"prematch_off:{key}:{reason}"
        frozen[key] = dict(conf, frozen_at=now.isoformat(), final_status=state)
        if tag not in seen:
            alerts.append(f"⏹ SEÑAL RETIRADA ({reason}): {key.replace('__', ' vs ')} "
                          f"· inicio {conf.get('start_utc', '?')}")
            seen.add(tag)
    return alerts, seen, frozen


def market_snapshot(rows: pd.DataFrame, summary: dict) -> dict:
    """Estado por ciclo para alertas de mercado: mercados cotizados por partido,
    mejor fuente por selección, mercados suspendidos y partidos en OOD."""
    snap = {"quoted": set(), "best": {}, "suspended": set(), "ood": {}}
    if rows is not None and len(rows):
        with_odds = rows[rows["odds"].notna()]
        for _, r in with_odds.iterrows():
            snap["quoted"].add((r["match"], r["market"]))
        for (match, market, sel), grp in with_odds.groupby(["match", "market", "selection"]):
            best = grp.loc[grp["odds"].idxmax()]
            snap["best"][(match, market, sel)] = str(best["bookmaker"])
        ood = rows[rows["reasons"].fillna("").str.contains("ood_", na=False)]
        for _, r in ood.iterrows():
            toks = [t.split("(")[0].split(":")[0] for t in str(r["reasons"]).split(";")
                    if t.strip().startswith("ood_")]
            snap["ood"][r["match"]] = ";".join(sorted(set(toks)))
    for e in (summary.get("structured") or {}).get("events", []):
        for m in e.get("suspended", []):
            snap["suspended"].add((e["match"], m))
    return snap


def market_alerts(prev: dict | None, cur: dict, alerted: set[str],
                  synced: bool) -> tuple[list[str], set[str]]:
    """Alertas de mercado del ciclo (deduplicadas). Con prev=None (primer
    ciclo) solo se alertan suspensiones: el estado inicial no es un cambio."""
    alerts: list[str] = []
    seen = set(alerted)
    for match, market in sorted(cur["suspended"]):
        tag = f"susp:{match}|{market}"
        if tag not in seen:
            alerts.append(f"⛔ MERCADO SUSPENDIDO: {match} · {market}")
            seen.add(tag)
    if prev is None:
        return alerts, seen
    for match, market in sorted(cur["quoted"] - prev["quoted"]):
        tag = f"mkt:{match}|{market}"
        if tag not in seen:
            alerts.append(f"🆕 MERCADO NUEVO CON CUOTA: {match} · {market}")
            seen.add(tag)
    for key, bk in cur["best"].items():
        old = prev["best"].get(key)
        if old is not None and old != bk:
            tag = f"best:{'|'.join(key)}:{bk}"
            if tag not in seen:
                match, market, sel = key
                alerts.append(f"🔀 MEJOR FUENTE: {match} · {market}/{sel} "
                              f"ahora {bk} (antes {old})")
                seen.add(tag)
    if synced:
        for match, reasons in cur["ood"].items():
            if match not in prev["ood"]:
                tag = f"ood_on:{match}:{reasons}"
                if tag not in seen:
                    alerts.append(f"🚫 OOD TRAS SYNC: {match} ({reasons})")
                    seen.add(tag)
        for match, reasons in prev["ood"].items():
            if match not in cur["ood"]:
                tag = f"ood_off:{match}"
                if tag not in seen:
                    alerts.append(f"✅ OOD RESUELTO TRAS SYNC: {match} (era {reasons})")
                    seen.add(tag)
    return alerts, seen


def _notify_local(title: str, body: str) -> None:
    """Notificación del sistema, best-effort y sin dependencias nuevas."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{body}" with title "{title}"'],
                           capture_output=True, timeout=10)
        elif sys.platform.startswith("win"):
            ps = ("[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')|Out-Null;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                  f"$n.ShowBalloonTip(8000,'{title}','{body}','Info')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        else:
            subprocess.run(["notify-send", title, body], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001 - la notificación nunca rompe el watch
        pass


def run_watch(cfg: dict, interval_min: int = 15, max_cycles: int | None = None,
              notify: bool = True, **scan_kwargs) -> None:
    ledger_dir = resolve_path(cfg, "ledger_dir")
    hist_path = ledger_dir / "price_history.jsonl"
    prev_signals: dict[str, dict] = {}
    prev_odds: dict[str, float] = {}
    prev_snap: dict | None = None
    prev_conf: dict = {}
    alerted: set[str] = set()
    margin = float(cfg.get("feeds", {}).get("prematch_margin_minutes", 5))
    cycle = 0
    sync_every = max(1, round(360 / max(1, interval_min)))   # sync ~cada 6 h
    print(f"betbot watch — cada {interval_min} min (Ctrl+C para salir). No ejecuta apuestas.")
    while True:
        cycle += 1
        ts = datetime.now(timezone.utc).isoformat()
        do_sync = (cycle - 1) % sync_every == 0
        try:
            res = run_scan(cfg, sync_results=do_sync, **scan_kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts[:16]}] escaneo fallido: {exc} (reintento en {interval_min} min)")
            if max_cycles and cycle >= max_cycles:
                return
            time.sleep(interval_min * 60)
            continue
        if res.summary.get("fail_closed"):
            print(f"[{ts[11:16]}] ciclo {cycle}: "
                  + res.summary.get("fail_closed_message", "FAIL_CLOSED"))
            prev_signals, prev_odds, prev_snap = {}, {}, None
            if max_cycles and cycle >= max_cycles:
                return
            time.sleep(interval_min * 60)
            continue
        rows = res.rows
        # 1º: retirar señales cuyo partido ya no es prepartido (empezó/terminó)
        cur_conf = res.summary.get("confirmations", {}) or {}
        t_alerts, alerted, frozen = prematch_transitions(prev_conf, cur_conf, alerted,
                                                         margin_minutes=margin)
        if frozen:
            with open(ledger_dir / "prematch_frozen.jsonl", "a", encoding="utf-8") as fh:
                for k, v in frozen.items():
                    fh.write(json.dumps({"ts": ts, "event": k, **v},
                                        ensure_ascii=False, default=str) + "\n")
        prev_conf = cur_conf
        new_signals = extract_signals(rows)
        crossings = detect_crossings(prev_odds, rows)
        alerts, alerted = diff_alerts(prev_signals, new_signals, crossings, alerted)
        alerts = t_alerts + alerts
        snap = market_snapshot(rows, res.summary)
        m_alerts, alerted = market_alerts(prev_snap, snap, alerted, synced=do_sync)
        alerts.extend(m_alerts)
        prev_snap = snap

        # historial de precios (append-only)
        if rows is not None and len(rows):
            with open(hist_path, "a", encoding="utf-8") as fh:
                for _, r in rows[rows["odds"].notna()].iterrows():
                    fh.write(json.dumps({"ts": ts, "key": signal_key(r.to_dict()),
                                         "odds": float(r["odds"]), "state": r["state"],
                                         "o_min": None if pd.isna(r["o_min"]) else float(r["o_min"])})
                             + "\n")
        s = res.summary
        cov = s.get("market_coverage") or {}
        cov_s = ", ".join(f"{m}:{n}/{s['eligible']}" for m, n in cov.items() if n) or "sin cuotas"
        print(f"[{ts[11:16]}] ciclo {cycle}: {s['eligible']} elegibles · "
              f"cuotas por mercado [{cov_s}] · señales activas {len(new_signals)} · "
              f"alertas nuevas {len(alerts)}")
        for a in alerts:
            print("  " + a + "\a")
            if notify:
                _notify_local("BetBot", a.replace("🟢 ", "").replace("📈 ", "").replace("⚪ ", ""))
        prev_signals = new_signals
        prev_odds = {signal_key(r.to_dict()): float(r["odds"])
                     for _, r in rows.iterrows()} if rows is not None and len(rows) else {}
        prev_odds = {k: v for k, v in prev_odds.items() if not pd.isna(v)}
        if max_cycles and cycle >= max_cycles:
            return
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            print("\nwatch detenido.")
            return
