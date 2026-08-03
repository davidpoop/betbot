"""Actualización incremental: re-descarga solo las fuentes vivas, reconstruye el
canónico con salvaguardas y refresca el ESTADO (Elo, actividad, features) sin
re-entrenar los modelos congelados.

Salvaguardas explícitas:
- si una fuente viva no puede descargarse, se conserva la versión previa y se avisa;
- si el nuevo canónico tiene MENOS partidos o su fecha máxima retrocede, se
  aborta y se restaura el canónico anterior (fallo explícito, nunca silencioso);
- el manifest y el informe de frescura por circuito quedan registrados.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from betbot.canonical.build import build_canonical
from betbot.config import resolve_path
from betbot.features.builder import build_features, derived_targets
from betbot.ingest.download import SOURCES, download_all
from betbot.ratings.elo import replay

# fuentes que pueden cambiar (el resto es histórico congelado)
LIVE_SOURCES = {"tennisdata_atp_2025", "tennisdata_atp_2026", "kaggle_daily_wta", "kaggle_daily_atp"}


def run_update(cfg: dict) -> dict:
    raw_dir = resolve_path(cfg, "raw_dir")
    canon_dir = resolve_path(cfg, "canonical_dir")
    art = resolve_path(cfg, "artifacts_dir")
    log: dict = {"started_at": datetime.now(timezone.utc).isoformat(),
                 "downloads": {}, "safeguards": []}

    # ---------- 1. re-descarga de fuentes vivas (con copia de seguridad) ----------
    backups: dict[Path, Path] = {}
    for src in SOURCES:
        if src["name"] not in LIVE_SOURCES:
            continue
        dest = raw_dir / src["dest"]
        if dest.exists():
            bak = dest.with_suffix(dest.suffix + ".bak")
            shutil.copy2(dest, bak)
            backups[dest] = bak
            dest.unlink()
    res = download_all(raw_dir, force=False)
    for dest, bak in backups.items():
        if not dest.exists():           # descarga fallida: restaurar la previa
            shutil.copy2(bak, dest)
            log["safeguards"].append(f"descarga fallida, restaurada version previa: {dest.name}")
    log["downloads"] = {"ok": res["ok"], "failed": res["failed"]}

    # ---------- 2. reconstrucción del canónico con comparación ----------
    prev_summary_path = canon_dir / "build_summary.json"
    prev = json.loads(prev_summary_path.read_text()) if prev_summary_path.exists() else None
    prev_backup = canon_dir.parent / "canonical_prev_backup"
    if canon_dir.exists() and prev is not None:
        if prev_backup.exists():
            shutil.rmtree(prev_backup)
        shutil.copytree(canon_dir, prev_backup)
    summary = build_canonical(raw_dir, canon_dir)
    if prev is not None:
        if summary["n_matches"] < prev["n_matches"] or summary["date_max"] < prev["date_max"]:
            shutil.rmtree(canon_dir)
            shutil.copytree(prev_backup, canon_dir)
            raise RuntimeError(
                f"FALLO EXPLICITO: el nuevo canonico retrocede "
                f"(n {prev['n_matches']}->{summary['n_matches']}, "
                f"fecha_max {prev['date_max']}->{summary['date_max']}). "
                "Se restauro la version previa; revisa las fuentes.")
        log["new_matches"] = summary["n_matches"] - prev["n_matches"]
    log["canonical"] = {"n_matches": summary["n_matches"], "date_max": summary["date_max"]}
    for bak in backups.values():
        bak.unlink(missing_ok=True)

    # ---------- 3. refresco de estado SIN re-entrenar ----------
    bundle_path = art / "model_bundle.joblib"
    if bundle_path.exists():
        bundle = joblib.load(bundle_path)
        matches = pd.read_parquet(canon_dir / "matches.parquet")
        players = pd.read_parquet(canon_dir / "players.parquet")
        elo_df, elo_states = replay(matches, cfg["elo"])
        feats_all, activity_state = build_features(elo_df, players, cfg)
        feats_all = derived_targets(feats_all)
        feats_all["year"] = pd.to_datetime(feats_all["date"]).dt.year
        bundle["elo_states"] = elo_states
        bundle["activity_state"] = activity_state
        bundle["meta"]["state_refreshed_at"] = datetime.now(timezone.utc).isoformat()
        bundle["meta"]["state_data_max_date"] = summary["date_max"]
        joblib.dump(bundle, bundle_path)
        feats_all.to_parquet(art / "features_all.parquet", index=False)
        log["state"] = "refrescado (modelos congelados intactos)"
    else:
        log["state"] = "sin bundle: ejecuta 'betbot train' primero"

    # ---------- 4. frescura por circuito ----------
    m = pd.read_parquet(canon_dir / "matches.parquet")
    fresh = {t: str(m.loc[m["tour"] == t, "date"].max()) for t in ("ATP", "WTA")}
    log["freshness_by_tour"] = fresh
    today = datetime.now(timezone.utc).date()
    for t, dmax in fresh.items():
        age = (today - pd.Timestamp(dmax).date()).days
        if age > 45:
            log["safeguards"].append(
                f"AVISO: {t} desactualizado ({age} dias desde {dmax}); "
                "la fuente publica puede haber dejado de actualizarse")
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    reports = resolve_path(cfg, "reports_dir")
    hist_path = reports / "update_log.jsonl"
    with open(hist_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(log, ensure_ascii=False) + "\n")
    return log
