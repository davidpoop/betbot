"""Empaqueta BetBot como ZIP portable (código + modelos + datos canónicos +
lanzadores + documentación). Sin data/raw (40 MB re-descargables), sin ledger
personal, sin logs, sin secretos.

Uso: python scripts/package.py  ->  dist/BetBot_portable.zip
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "BetBot"
DIST = ROOT / "dist"

LEEME = """\
==============================================
  BETBOT — screener local de tenis (portable)
==============================================

INSTALACION EN 3 PASOS
  1) Descomprime esta carpeta donde quieras (p. ej. Documentos\\BetBot).
  2) Doble clic en el instalador de tu sistema (solo la primera vez):
       - Windows:  INSTALAR_BETBOT.bat
       - macOS:    INSTALAR_BETBOT.command   (clic derecho > Abrir la 1a vez)
       - Linux:    INSTALAR_BETBOT.sh
     Necesita Python 3.11+ y conexion a Internet una unica vez.
  3) Abre BetBot con doble clic:
       - Windows:  ABRIR_BETBOT.vbs
       - macOS:    BetBot.app               (clic derecho > Abrir la 1a vez)
       - Linux:    ABRIR_BETBOT.sh
     Se abrira tu navegador con la aplicacion. Todo funciona SOLO en tu
     ordenador (127.0.0.1); nada se expone a Internet y no se ejecutan apuestas.

CERRAR / DESINSTALAR
  - Boton "Cerrar BetBot" en la barra lateral de la aplicacion
    (o CERRAR_BETBOT segun tu sistema).
  - DESINSTALAR_BETBOT elimina el entorno; tus datos y picks NO se borran.

INCLUYE modelos entrenados y datos historicos preparados: la aplicacion
funciona nada mas instalar. Si faltara algo, la propia interfaz muestra la
pantalla de primer arranque con botones (Descargar / Preparar / Entrenar).

Documentacion completa: docs/USO.md · Logs: artifacts/logs/
"""

INCLUDE_TREES = ["src", "config", "templates", "docs", "tests", "scripts", "launchers/icons"]
INCLUDE_FILES = ["pyproject.toml", ".gitignore"]
DATA_TREES = ["data/canonical", "data/manual"]
ARTIFACTS = ["artifacts/model_bundle.joblib", "artifacts/features_all.parquet"]
ARTIFACT_TREES = ["artifacts/reports"]
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", "betbot.egg-info", "canonical_prev_backup"}
EXCLUDE_REPORT_PREFIXES = ("eval_test",)   # el test sellado no viaja en el paquete


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*EXCLUDE_NAMES, "*.pyc", "*.log"))


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    for t in INCLUDE_TREES:
        if (ROOT / t).exists():
            _copytree(ROOT / t, BUILD / t)
    for f in INCLUDE_FILES:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, BUILD / f)
    for t in DATA_TREES:
        if (ROOT / t).exists():
            _copytree(ROOT / t, BUILD / t)
    for f in ARTIFACTS:
        if (ROOT / f).exists():
            (BUILD / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, BUILD / f)
    for t in ARTIFACT_TREES:
        src = ROOT / t
        if src.exists():
            dst = BUILD / t
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file() and not f.name.startswith(EXCLUDE_REPORT_PREFIXES):
                    shutil.copy2(f, dst / f.name)

    # lanzadores en la raíz del paquete (descubribles por doble clic)
    win, mac, lin = ROOT / "launchers/windows", ROOT / "launchers/macos", ROOT / "launchers/linux"
    for f in win.iterdir():
        shutil.copy2(f, BUILD / f.name)
    for name in ("INSTALAR_BETBOT.command", "DESINSTALAR_BETBOT.command"):
        shutil.copy2(mac / name, BUILD / name)
    _copytree(mac / "BetBot.app", BUILD / "BetBot.app")
    for f in lin.iterdir():
        shutil.copy2(f, BUILD / f.name)
    (BUILD / "LEEME_PRIMERO.txt").write_text(LEEME, encoding="utf-8")

    # permisos de ejecucion dentro del zip
    zpath = DIST / "BetBot_portable.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(BUILD.rglob("*")):
            rel = Path("BetBot") / p.relative_to(BUILD)
            if p.is_dir():
                continue
            zi = zipfile.ZipInfo(str(rel))
            data = p.read_bytes()
            if p.suffix in (".sh", ".command") or p.name == "BetBot":
                zi.external_attr = 0o755 << 16
            else:
                zi.external_attr = 0o644 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    size_mb = zpath.stat().st_size / 1e6
    print(f"OK: {zpath} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
