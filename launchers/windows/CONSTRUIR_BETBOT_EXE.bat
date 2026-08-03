@echo off
REM Construccion OPCIONAL de BetBot.exe (sin consola) con PyInstaller.
REM El camino soportado y probado es ABRIR_BETBOT.vbs; este .exe es equivalente.
cd /d "%~dp0"
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm --clean betbot_pyinstaller.spec
echo El ejecutable queda en dist\BetBot\BetBot.exe (copialo junto a esta carpeta).
pause
