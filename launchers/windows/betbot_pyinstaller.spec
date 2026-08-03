# Spec reproducible de BetBot.exe (lanzador sin consola).
# Nota: streamlit necesita sus metadatos; por eso se usa collect_all.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("streamlit", "betbot", "altair", "pyarrow", "pandas", "sklearn"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(["run_launcher.py"], pathex=["src"], datas=datas,
             binaries=binaries, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, name="BetBot", console=False,
          icon="../icons/betbot.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="BetBot")
