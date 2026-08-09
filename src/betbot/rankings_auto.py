"""Rankings ATP/WTA automáticos derivados de los resultados canónicos.

Diagnóstico que motiva este módulo: el pipeline de rankings SIEMPRE existió
(loader as-of en ingest/rankings.py; el screener pasa rank_a/rank_b como
feature a M4/M5), pero su fuente de datos era un CSV que había que rellenar A
MANO desde la web oficial — nunca se rellenó, de ahí el aviso permanente
`sin_rankings_cargados_fallback_elo`.

Fuente automática y reproducible: las propias fuentes de resultados publican el
RANKING OFICIAL del jugador en el momento de cada partido (columnas
winner_rank/loser_rank de los mirrors TML y TennisCourtLog, y del histórico
tennis-data). De ahí se deriva un snapshot as-of: para cada jugador, su último
ranking observado dentro de una ventana. No es la tabla oficial del lunes, pero
cada valor ES un ranking oficial real con su fecha de observación — sin
scraping, sin credenciales y sin inventar nada.

El snapshot se escribe en data/manual/rankings_{tour}.csv, EXACTAMENTE el
formato que el loader as-of ya espera, con columnas extra de trazabilidad
(observed_date, source) y cabecera AUTOGENERADO. Un fichero rellenado a mano
por el usuario (sin la cabecera) NUNCA se sobreescribe: lo manual/oficial
tiene preferencia. La frescura la vigila el loader (aviso > max_age_days).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

AUTO_MARKER = "# AUTOGENERADO por betbot (rankings derivados de resultados)"
DEFAULT_WINDOW_DAYS = 120      # observaciones más viejas no entran al snapshot
MIN_ROWS_PER_TOUR = 20         # menos que esto no es un snapshot utilizable


def _is_user_file(path: Path) -> bool:
    """True si el fichero existe, tiene filas de datos y NO es autogenerado."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if AUTO_MARKER in text:
        return False
    has_data = any(line.strip() and not line.startswith("#")
                   and not line.startswith("date_published")
                   for line in text.splitlines())
    return has_data


def build_derived_rankings(cfg: dict, today: date | None = None,
                           window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Escribe rankings_{atp,wta}.csv derivados del canónico. Devuelve informe."""
    from betbot.canonical.store import load_matches
    from betbot.config import resolve_path
    today = today or datetime.now(timezone.utc).date()
    canon = resolve_path(cfg, "canonical_dir")
    manual_dir = Path(resolve_path(cfg, "manual_dir"))
    manual_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "tours": {}}
    try:
        m = load_matches(canon)
    except FileNotFoundError:
        report["error"] = "sin dataset canónico"
        return report
    cutoff = today - pd.Timedelta(days=window_days).to_pytimedelta()
    for tour in ("ATP", "WTA"):
        path = manual_dir / f"rankings_{tour.lower()}.csv"
        if _is_user_file(path):
            report["tours"][tour] = {"skipped": "fichero manual del usuario: se respeta"}
            continue
        g = m[(m["tour"] == tour) & (m["date"] >= cutoff) & (m["date"] <= today)]
        obs: list[dict] = []
        for side in ("a", "b"):
            sub = g[["date", f"player_{side}", f"rank_{side}", f"pts_{side}"]].dropna(
                subset=[f"rank_{side}"])
            for r in sub.itertuples(index=False):
                rank = float(getattr(r, f"rank_{side}"))
                if rank <= 0:
                    continue
                pts = getattr(r, f"pts_{side}")
                obs.append({"player": getattr(r, f"player_{side}"), "date": r.date,
                            "rank": int(rank),
                            "points": float(pts) if pd.notna(pts) else 0.0})
        if len(obs) < MIN_ROWS_PER_TOUR:
            report["tours"][tour] = {"skipped": f"solo {len(obs)} observaciones con "
                                                f"rank en {window_days}d: insuficiente"}
            continue
        df = pd.DataFrame(obs).sort_values("date")
        latest = df.groupby("player").last().reset_index()
        effective = df["date"].max()          # fecha efectiva REAL del snapshot
        lines = [AUTO_MARKER,
                 f"# fuente: rankings oficiales observados por partido en los mirrors "
                 f"de resultados (TML / TennisCourtLog / tennis-data)",
                 f"# fetched_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                 f" · ventana: {window_days}d · jugadores: {len(latest)}",
                 "date_published,rank,player,points,observed_date,source"]
        for r in latest.sort_values("rank").itertuples(index=False):
            lines.append(f"{effective},{r.rank},{r.player},{r.points:.0f},"
                         f"{r.date},derived_from_results")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report["tours"][tour] = {"file": str(path), "players": int(len(latest)),
                                 "effective_date": str(effective),
                                 "age_days": (today - effective).days,
                                 "source": "derived_from_results"}
    return report
