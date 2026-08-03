@echo off
chcp 65001 >nul
title Desinstalador de BetBot
cd /d "%~dp0"
echo Este proceso cierra BetBot y elimina el entorno (.venv).
echo Tus datos, picks y modelos (data/ y artifacts/) NO se borran.
set /p CONF="Escribe SI para continuar: "
if /i not "%CONF%"=="SI" (echo Cancelado.& pause & exit /b 0)
if exist .venv\Scripts\python.exe .venv\Scripts\python -m betbot.launcher --stop >nul 2>nul
rmdir /s /q .venv 2>nul
del /q artifacts\betbot.lock 2>nul
echo [OK] BetBot desinstalado. Para borrarlo del todo, elimina esta carpeta.
pause
