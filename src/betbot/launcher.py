"""Lanzador local de BetBot (sin terminal en el uso cotidiano).

- Puerto libre automático, SOLO en 127.0.0.1 (nunca expuesto a la red).
- Instancia única con lockfile: si ya está abierto, reabre la pestaña y avisa.
- Sin consola visible (CREATE_NO_WINDOW en Windows; pythonw/.vbs/.app fuera).
- Cierre seguro: botón en la interfaz (fichero de parada) o `--stop`.
- Logs en artifacts/logs/.

Uso (los dobles clics llaman a esto):
    python -m betbot.launcher            # abrir (o enfocar si ya está abierto)
    python -m betbot.launcher --stop     # cerrar
    python -m betbot.launcher --restart  # reiniciar
    python -m betbot.launcher --status   # estado JSON
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = REPO_ROOT / "artifacts" / "betbot.lock"
STOP_FILE = REPO_ROOT / "artifacts" / "betbot.stop"
LOG_DIR = REPO_ROOT / "artifacts" / "logs"
APP_PATH = Path(__file__).parent / "ui" / "app.py"

REQUIRED_MODULES = ["streamlit", "pandas", "numpy", "sklearn", "pydantic", "click",
                    "yaml", "joblib", "pyarrow"]


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "launcher.log", "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def missing_dependencies() -> list[str]:
    import importlib.util
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def read_lock() -> dict | None:
    if not LOCK_FILE.exists():
        return None
    try:
        return json.loads(LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def running_instance() -> dict | None:
    """Devuelve el lock si hay una instancia viva; limpia locks huérfanos."""
    lock = read_lock()
    if not lock:
        return None
    if _pid_alive(int(lock.get("pid", -1))) and _health_ok(int(lock.get("port", 0))):
        return lock
    log(f"lock huerfano eliminado: {lock}")
    LOCK_FILE.unlink(missing_ok=True)
    return None


def _notify(title: str, message: str) -> None:
    """Aviso sin consola: MessageBox en Windows; log siempre."""
    log(f"NOTIFY: {title}: {message}")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
        except Exception:  # noqa: BLE001
            pass


def open_app(no_browser: bool = False, wait: bool = True) -> dict:
    """Arranca el servidor (o enfoca la instancia existente). Devuelve estado."""
    existing = running_instance()
    if existing:
        url = f"http://127.0.0.1:{existing['port']}"
        if not no_browser:
            webbrowser.open(url)
        _notify("BetBot ya está abierto",
                f"BetBot ya estaba en marcha; se ha reabierto la pestaña ({url}).")
        return {"status": "already_running", **existing, "url": url}

    missing = missing_dependencies()
    if missing:
        msg = ("Faltan dependencias: " + ", ".join(missing)
               + ". Ejecuta el instalador (INSTALAR_BETBOT) para repararlas.")
        _notify("BetBot — instalación incompleta", msg)
        return {"status": "missing_deps", "missing": missing}

    STOP_FILE.unlink(missing_ok=True)
    port = find_free_port()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server_log = open(LOG_DIR / "streamlit.log", "a", encoding="utf-8")
    server_log.write(f"\n===== arranque {datetime.now(timezone.utc).isoformat()} puerto {port} =====\n")
    server_log.flush()
    cmd = [sys.executable, "-m", "streamlit", "run", str(APP_PATH),
           "--server.address", "127.0.0.1",          # SOLO localhost, nunca la red
           "--server.port", str(port),
           "--server.headless", "true",
           "--browser.gatherUsageStats", "false"]
    creationflags = 0x08000000 if os.name == "nt" else 0    # CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdout=server_log, stderr=subprocess.STDOUT,
                            cwd=str(REPO_ROOT), creationflags=creationflags,
                            start_new_session=(os.name != "nt"))
    lock = {"pid": proc.pid, "port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "launcher_pid": os.getpid()}
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps(lock))
    log(f"servidor lanzado pid={proc.pid} puerto={port}")

    for _ in range(120):                               # hasta 60 s de arranque
        if _health_ok(port):
            break
        if proc.poll() is not None:
            LOCK_FILE.unlink(missing_ok=True)
            _notify("BetBot — error de arranque",
                    "El servidor no pudo iniciarse. Revisa artifacts/logs/streamlit.log")
            return {"status": "failed", "log": str(LOG_DIR / "streamlit.log")}
        time.sleep(0.5)
    url = f"http://127.0.0.1:{port}"
    if not no_browser:
        webbrowser.open(url)
    threading.Thread(target=_stop_watchdog, args=(proc,), daemon=True).start()
    result = {"status": "started", "url": url, **lock}
    if wait:
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        _terminate(proc)
        LOCK_FILE.unlink(missing_ok=True)
        log("servidor finalizado")
    return result


def _stop_watchdog(proc: subprocess.Popen) -> None:
    """El botón 'Cerrar BetBot' de la interfaz crea STOP_FILE."""
    while proc.poll() is None:
        if STOP_FILE.exists():
            log("stopfile detectado: cierre seguro solicitado desde la interfaz")
            _terminate(proc)
            STOP_FILE.unlink(missing_ok=True)
            LOCK_FILE.unlink(missing_ok=True)
            return
        time.sleep(1.0)


def _terminate(proc_or_pid) -> None:
    pid = proc_or_pid if isinstance(proc_or_pid, int) else proc_or_pid.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(pid):
                    return
                time.sleep(0.25)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception as exc:  # noqa: BLE001
        log(f"error terminando pid {pid}: {exc}")


def stop_app() -> dict:
    lock = read_lock()
    if not lock:
        return {"status": "not_running"}
    _terminate(int(lock["pid"]))
    LOCK_FILE.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    log(f"cerrado pid={lock['pid']}")
    return {"status": "stopped", **lock}


def status() -> dict:
    inst = running_instance()
    if inst:
        return {"status": "running", "url": f"http://127.0.0.1:{inst['port']}", **inst}
    return {"status": "not_running"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="betbot.launcher")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-wait", action="store_true",
                    help="no bloquear (para tests); el servidor queda huérfano gestionable via --stop")
    args = ap.parse_args(argv)
    if args.status:
        print(json.dumps(status()))
        return 0
    if args.stop:
        print(json.dumps(stop_app()))
        return 0
    if args.restart:
        stop_app()
        time.sleep(1.0)
    res = open_app(no_browser=args.no_browser, wait=not args.no_wait)
    print(json.dumps({k: v for k, v in res.items() if k != "launcher_pid"}))
    return 0 if res["status"] in ("started", "already_running") else 1


if __name__ == "__main__":
    raise SystemExit(main())
