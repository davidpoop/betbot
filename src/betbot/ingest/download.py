"""Descarga de fuentes reales a la capa raw inmutable, con manifest y SHA256.

Fuentes (mirrors públicos en raw.githubusercontent.com; el proxy de red de este
entorno bloquea tennis-data.co.uk directo, api.github.com y codeload):

- MLT (0xsimulacra/MLT): concatenado real de Tennis-Data.co.uk
  ATP 2000–2019 y WTA 2007–2019 con columnas completas (B365, PS, Max, Avg...).
- gmalbert/tennis-predictions: ficheros XLSX ANUALES ORIGINALES de
  Tennis-Data.co.uk, ATP 2020–2026 (columnas completas).
- gilmullinau/tennis_wta: derivado de Tennis-Data (formato Kaggle "daily pull"),
  ATP 2000–2025 y WTA 2007–2025 con UNA cuota por lado (libro no identificado).
  Se usa como fuente primaria WTA 2020–2025 y para tests de reconciliación.
- farhadGithub/tennis-atp-data: mirror del dataset de Jeff Sackmann (licencia
  CC BY-NC-SA 4.0, uso NO comercial): atp_players.csv para mano/fecha de
  nacimiento.

Si una URL no responde, se registra el fallo y se continúa: el sistema puede
operar con los ficheros ya presentes en data/raw (o colocados a mano).
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MLT = "https://raw.githubusercontent.com/0xsimulacra/MLT/master"
GMA = "https://raw.githubusercontent.com/gmalbert/tennis-predictions/main/data_files"
GIL = "https://raw.githubusercontent.com/gilmullinau/tennis_wta/main"
FAR = "https://raw.githubusercontent.com/farhadGithub/tennis-atp-data/main/data/raw"

SOURCES: list[dict] = (
    [
        {"name": "mlt_df_atp", "url": f"{MLT}/df_atp.csv", "dest": "mlt/df_atp.csv",
         "license": "Datos originales (c) tennis-data.co.uk; mirror publico. Uso investigacion personal."},
        {"name": "mlt_df_wta", "url": f"{MLT}/df_wta.csv", "dest": "mlt/df_wta.csv",
         "license": "Datos originales (c) tennis-data.co.uk; mirror publico. Uso investigacion personal."},
        {"name": "kaggle_daily_atp", "url": f"{GIL}/atp_data.csv", "dest": "kaggle_daily/atp_data.csv",
         "license": "Derivado de tennis-data.co.uk (formato Kaggle daily pull). Uso investigacion personal."},
        {"name": "kaggle_daily_wta", "url": f"{GIL}/wta_data.csv", "dest": "kaggle_daily/wta_data.csv",
         "license": "Derivado de tennis-data.co.uk (formato Kaggle daily pull). Uso investigacion personal."},
        {"name": "sackmann_atp_players", "url": f"{FAR}/atp_players.csv", "dest": "sackmann/atp_players.csv",
         "license": "Jeff Sackmann / Tennis Abstract, CC BY-NC-SA 4.0 (NO comercial, atribucion)."},
    ]
    + [
        {"name": f"tennisdata_atp_{y}", "url": f"{GMA}/{y}.xlsx", "dest": f"tennis_data/atp_{y}.xlsx",
         "license": "Datos originales (c) tennis-data.co.uk; mirror publico. Uso investigacion personal."}
        for y in range(2020, 2027)
    ]
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_all(raw_dir: Path, force: bool = False, timeout: int = 120) -> dict:
    raw_dir = Path(raw_dir)
    manifest_path = raw_dir / "manifest.json"
    manifest: dict = {"downloaded_at": None, "files": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    results = {"ok": [], "skipped": [], "failed": []}
    for src in SOURCES:
        dest = raw_dir / src["dest"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            results["skipped"].append(src["name"])
            continue
        try:
            req = urllib.request.Request(src["url"], headers={"User-Agent": "betbot-mvp/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            manifest["files"][src["dest"]] = {
                "name": src["name"], "url": src["url"], "sha256": _sha256(dest),
                "bytes": dest.stat().st_size, "license": src["license"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            results["ok"].append(src["name"])
        except Exception as exc:  # noqa: BLE001 - registrar y continuar
            if dest.exists():
                dest.unlink()
            results["failed"].append(f"{src['name']}: {exc}")
    manifest["downloaded_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return results
