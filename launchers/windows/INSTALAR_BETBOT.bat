@echo off
chcp 65001 >nul
title Instalador de BetBot
echo ============================================
echo   INSTALADOR DE BETBOT (solo primera vez)
echo ============================================
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No se encontro Python. Instala Python 3.11 o superior desde
  echo         https://www.python.org/downloads/  (marca "Add python.exe to PATH")
  pause & exit /b 1
)
for /f "tokens=2 delims= " %%v in ('py -3 -V') do set PYVER=%%v
echo Python detectado: %PYVER%
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)"
if errorlevel 1 (
  echo [ERROR] Se requiere Python 3.11 o superior. Version detectada: %PYVER%
  pause & exit /b 1
)
if not exist .venv (
  echo Creando entorno virtual...
  py -3 -m venv .venv || (echo [ERROR] no se pudo crear .venv & pause & exit /b 1)
)
echo Instalando BetBot y dependencias (requiere Internet la primera vez)...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\python -m pip install -e . || (echo [ERROR] instalacion fallida & pause & exit /b 1)
echo.
echo [OK] Instalacion completada. Usa ABRIR_BETBOT para abrir la aplicacion.
pause
