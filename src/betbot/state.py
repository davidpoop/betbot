"""Refresco del estado operativo (Elo, actividad, features) SIN re-entrenar.

El "recálculo incremental" se implementa como replay determinista completo del
dataset fusionado (mirrors + manuales): con ~115k partidos tarda ~15 s, es
inmune al orden de importación y garantiza as-of por construcción. Los modelos,
calibradores y umbrales congelados no se tocan.
"""
from __future__ import annotations

from datetime import datetime, timezone

import joblib
import pandas as pd

from betbot.canonical.store import load_matches
from betbot.config import resolve_path
from betbot.features.builder import build_features, derived_targets
from betbot.ratings.elo import replay


def refresh_state(cfg: dict) -> dict:
    art = resolve_path(cfg, "artifacts_dir")
    canon = resolve_path(cfg, "canonical_dir")
    bundle_path = art / "model_bundle.joblib"
    if not bundle_path.exists():
        return {"state": "sin bundle: ejecuta 'betbot train' primero"}
    bundle = joblib.load(bundle_path)
    matches = load_matches(canon)
    players = pd.read_parquet(canon / "players.parquet")
    elo_df, elo_states = replay(matches, cfg["elo"])
    feats_all, activity_state = build_features(elo_df, players, cfg)
    feats_all = derived_targets(feats_all)
    feats_all["year"] = pd.to_datetime(feats_all["date"]).dt.year
    # El MARCADOR de frescura debe ser honesto: build_features toma el máximo
    # de TODAS las filas, pero una fuente de cobertura parcial (p.ej. solo el
    # Canadian Open) no puede hacer pasar por fresco al circuito entero. El
    # Elo/actividad SÍ incorporan esas filas (features al día); el marcador
    # global usa solo cobertura amplia y la parcial va aparte, por torneo.
    from betbot.sync import freshness_detail, local_freshness
    activity_state["freshness"] = {t: d.isoformat() for t, d in local_freshness(cfg).items()}
    activity_state["active_coverage"] = {
        t: {k: {"tournament": c["tournament"], "until": str(c["until"])}
            for k, c in d["active_coverage"].items()}
        for t, d in freshness_detail(cfg).items()}
    bundle["elo_states"] = elo_states
    bundle["activity_state"] = activity_state
    bundle["meta"]["state_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    bundle["meta"]["state_data_max_date"] = str(matches["date"].max())
    joblib.dump(bundle, bundle_path)
    feats_all.to_parquet(art / "features_all.parquet", index=False)
    return {"state": "refrescado (modelos congelados intactos)",
            "n_matches": int(len(matches)), "date_max": str(matches["date"].max())}
