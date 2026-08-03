"""Tests del lanzador (puerto, instancia única, ciclo arranque/parada) y del
asistente de primer arranque."""
import copy
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from betbot import launcher
from betbot.config import load_config
from betbot.firstrun import check_environment, needs_setup, setup_actions

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "artifacts" / "model_bundle.joblib"


def test_find_free_port_is_bindable_and_local():
    p1 = launcher.find_free_port()
    p2 = launcher.find_free_port()
    assert 1024 < p1 < 65536 and 1024 < p2 < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", p1))     # sigue libre y solo en localhost


def test_missing_dependencies_empty_in_dev_env():
    assert launcher.missing_dependencies() == []


def test_stale_lock_is_cleaned(tmp_path, monkeypatch):
    lock = tmp_path / "betbot.lock"
    monkeypatch.setattr(launcher, "LOCK_FILE", lock)
    lock.write_text(json.dumps({"pid": 99999999, "port": 65000}))
    assert launcher.running_instance() is None
    assert not lock.exists()          # lock huérfano eliminado
    # lock corrupto tampoco rompe
    lock.write_text("{corrupto")
    assert launcher.read_lock() is None


def test_status_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "LOCK_FILE", tmp_path / "none.lock")
    assert launcher.status() == {"status": "not_running"}


@pytest.mark.skipif(not BUNDLE.exists(), reason="necesita artefactos entrenados")
def test_launcher_full_cycle_single_instance():
    """Arranque real sin navegador, guardia de instancia única, cierre seguro."""
    if launcher.running_instance():
        launcher.stop_app()
        time.sleep(1)
    r = subprocess.run([sys.executable, "-m", "betbot.launcher", "--no-browser", "--no-wait"],
                       capture_output=True, text=True, cwd=ROOT, timeout=180)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["status"] == "started", out
    port = out["port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
        # segunda instancia -> already_running, mismo puerto, sin duplicar
        r2 = subprocess.run([sys.executable, "-m", "betbot.launcher", "--no-browser", "--no-wait"],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
        out2 = json.loads(r2.stdout.strip().splitlines()[-1])
        assert out2["status"] == "already_running" and out2["port"] == port
        st = launcher.status()
        assert st["status"] == "running" and st["port"] == port
    finally:
        stopped = launcher.stop_app()
        assert stopped["status"] in ("stopped", "not_running")
    time.sleep(1)
    assert launcher.status()["status"] == "not_running"
    # solo escucha en localhost: el puerto no responde en la IP externa
    # (comprobación indirecta: el comando usó --server.address 127.0.0.1)


def test_firstrun_ready_on_real_repo():
    cfg = load_config()
    checks = check_environment(cfg)
    assert checks["python_ok"] and checks["deps_ok"]
    if BUNDLE.exists():
        assert checks["ready"] and not needs_setup(checks)
        assert setup_actions(checks) == []


def test_firstrun_detects_missing_everything(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["raw_dir"] = str(tmp_path / "raw")
    cfg["paths"]["canonical_dir"] = str(tmp_path / "canonical")
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    checks = check_environment(cfg)
    assert needs_setup(checks)
    assert setup_actions(checks) == ["download", "prepare", "train"]
    assert checks["data_writable"] and checks["artifacts_writable"]


def test_firstrun_detects_unwritable_dir(tmp_path):
    import os
    cfg = copy.deepcopy(load_config())
    ro = tmp_path / "ro"
    ro.mkdir()
    cfg["paths"]["canonical_dir"] = str(ro / "canonical")
    cfg["paths"]["raw_dir"] = str(tmp_path / "raw")
    cfg["paths"]["artifacts_dir"] = str(tmp_path / "artifacts")
    if os.geteuid() == 0:
        pytest.skip("root ignora permisos de escritura")
    os.chmod(ro, 0o500)
    checks = check_environment(cfg)
    assert not checks["data_writable"]
    assert "permissions" in setup_actions(checks)
