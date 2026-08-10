"""Informes del Model Lab (FASE 1). Uso, sin tocar el CLI productivo:

    python -m betbot.model_lab.reports dataset    # ficha del dataset + cobertura
    python -m betbot.model_lab.reports baseline   # J: reproducción del champion
                                                  #    y comparación vs train_report.json

Artefactos en artifacts/model_lab/ — nunca sobre los del champion.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from betbot.config import load_config, resolve_path


def _outdir(cfg: dict) -> Path:
    d = Path(resolve_path(cfg, "artifacts_dir")) / "model_lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_dataset(cfg: dict) -> dict:
    from betbot.model_lab.dataset import build_dataset
    from betbot.model_lab.features_v2 import CATALOG, coverage_report
    from betbot.model_lab.validation import outer_scheme, walk_forward_folds
    ds = build_dataset(cfg)
    lab = ds.labelled()
    rep = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "data_hash": ds.data_hash,
        "n_filas": int(len(ds.df)), "n_etiquetadas": int(len(lab)),
        "por_tour": {t: int(n) for t, n in lab["tour"].value_counts().items()},
        "features_v1": ds.feature_cols,
        "esquema_externo": outer_scheme(cfg),
        "folds_walk_forward": [f.label() for f in walk_forward_folds(cfg)],
        "cobertura": coverage_report(cfg),
        "catalogo_v2": [c.__dict__ for c in CATALOG],
    }
    out = _outdir(cfg) / "dataset_card.json"
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    print(f"ficha del dataset -> {out}")
    print(json.dumps({k: rep[k] for k in ("data_hash", "n_filas", "n_etiquetadas",
                                          "por_tour", "esquema_externo")},
                     indent=2, ensure_ascii=False, default=str))
    return rep


def report_baseline(cfg: dict) -> dict:
    from betbot.model_lab.challengers import compare_with_train_report, reproduce_champion_oof
    from betbot.model_lab.dataset import build_dataset
    reports_dir = Path(resolve_path(cfg, "reports_dir"))
    train_report = json.loads((reports_dir / "train_report.json").read_text())
    ds = build_dataset(cfg)
    repro = reproduce_champion_oof(cfg, ds)
    cmp_ = compare_with_train_report(repro, train_report)
    rep = {"generado": datetime.now(timezone.utc).isoformat(),
           "data_hash": ds.data_hash,
           "reproduccion": repro, "comparacion_vs_train_report": cmp_,
           "acreditado": cmp_["max_abs_delta"] <= 1e-4}
    out = _outdir(cfg) / "baseline_champion.json"
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    print(f"baseline champion -> {out}")
    for tour, det in cmp_["detalle"].items():
        print(f"{tour}: deltas vs train_report -> " + json.dumps(det, ensure_ascii=False))
    print(f"max_abs_delta = {cmp_['max_abs_delta']}  "
          f"({'ACREDITADO' if rep['acreditado'] else 'NO COINCIDE: investigar antes de seguir'})")
    return rep


def main(argv: list[str]) -> int:
    cfg = load_config()
    cmd = argv[0] if argv else "dataset"
    if cmd == "dataset":
        report_dataset(cfg)
    elif cmd == "baseline":
        report_baseline(cfg)
    else:
        print("uso: python -m betbot.model_lab.reports [dataset|baseline]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
