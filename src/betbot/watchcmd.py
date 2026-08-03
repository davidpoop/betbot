"""`betbot watch`: escaneo continuo con alertas deduplicadas.

- Repite `scan` cada N minutos (15 por defecto).
- Alerta en terminal (y notificación local best-effort) cuando:
  * aparece una señal nueva (fuerte/normal/experimental);
  * una cuota cruza al alza su o_min (una oportunidad "vigilar" se activa);
  * una señal previa desaparece o caduca.
- No repite la misma alerta; guarda historial de precios en el ledger.
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
    alerted: set[str] = set()
    cycle = 0
    print(f"betbot watch — cada {interval_min} min (Ctrl+C para salir). No ejecuta apuestas.")
    while True:
        cycle += 1
        ts = datetime.now(timezone.utc).isoformat()
        try:
            res = run_scan(cfg, **scan_kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"[{ts[:16]}] escaneo fallido: {exc} (reintento en {interval_min} min)")
            if max_cycles and cycle >= max_cycles:
                return
            time.sleep(interval_min * 60)
            continue
        rows = res.rows
        new_signals = extract_signals(rows)
        crossings = detect_crossings(prev_odds, rows)
        alerts, alerted = diff_alerts(prev_signals, new_signals, crossings, alerted)

        # historial de precios (append-only)
        if rows is not None and len(rows):
            with open(hist_path, "a", encoding="utf-8") as fh:
                for _, r in rows[rows["odds"].notna()].iterrows():
                    fh.write(json.dumps({"ts": ts, "key": signal_key(r.to_dict()),
                                         "odds": float(r["odds"]), "state": r["state"],
                                         "o_min": None if pd.isna(r["o_min"]) else float(r["o_min"])})
                             + "\n")
        s = res.summary
        print(f"[{ts[11:16]}] ciclo {cycle}: {s['eligible']} elegibles · "
              f"cobertura cuotas {s['odds_coverage_pct']}% · señales activas {len(new_signals)} · "
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
