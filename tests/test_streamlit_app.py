"""Smoke test de la app Streamlit con el runner oficial (sin navegador)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "betbot" / "ui" / "app.py"
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"


@pytest.mark.skipif(not BUNDLE.exists(), reason="ejecutar antes 'betbot train'")
def test_app_runs_without_exception():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=180)
    at.run()
    assert not at.exception, f"la app lanzó excepción: {at.exception}"
    # elementos clave presentes
    assert len(at.tabs) == 4
    assert any("betbot" in str(t.value) for t in at.title)


@pytest.mark.skipif(not BUNDLE.exists(), reason="ejecutar antes 'betbot train'")
def test_app_analyze_button_flow():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    # pulsar "Analizar" con la fila por defecto (vacía) no debe romper la app
    buttons = [b for b in at.button if "Analizar" in str(b.label)]
    assert buttons, "no se encontró el botón Analizar"
    buttons[0].click()
    at.run()
    assert not at.exception, f"analizar rompió la app: {at.exception}"
