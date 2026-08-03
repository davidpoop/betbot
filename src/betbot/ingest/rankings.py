"""Rankings actuales introducidos por fichero (vía legal y reproducible).

Formato CSV (uno por circuito, editable a mano desde la web oficial ATP/WTA):
    data/manual/rankings_atp.csv  y  data/manual/rankings_wta.csv
    columnas: date_published,rank,player,points
    (player en formato Tennis-Data "Alcaraz C." o nombre completo "Carlos Alcaraz")

Reglas as-of estrictas:
- solo se usan filas con date_published <= fecha de referencia (nunca ranking futuro);
- se toma la publicación más reciente que cumpla la regla;
- si no hay fichero o queda vacío tras el filtro: fallback a Elo (sin inventar);
- aviso si la publicación usada es más vieja que rankings.max_age_days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from betbot.canonical.names import canonical_key, split_last_initials

RANKINGS_TEMPLATE = """\
# Rankings actuales (copia manual desde la web oficial ATP/WTA; via legal sin scraping)
# date_published = fecha oficial de publicacion (lunes). NUNCA pongas fechas futuras.
date_published,rank,player,points
# 2026-08-03,1,Sinner J.,11830
# 2026-08-03,2,Alcaraz C.,9860
"""


@dataclass
class RankingsAsOf:
    published: date | None = None
    by_player: dict[str, tuple[int, float]] = field(default_factory=dict)   # key -> (rank, points)
    warnings: list[str] = field(default_factory=list)

    def lookup(self, player_key: str) -> tuple[int, float] | None:
        if player_key in self.by_player:
            return self.by_player[player_key]
        last, ini = split_last_initials(player_key)
        if ini:
            hit = self.by_player.get(f"{last}_{ini[0]}")
            if hit:
                return hit
        return None


def _to_td_key(raw_name: str) -> str:
    """Acepta "Alcaraz C." (Tennis-Data) o "Carlos Alcaraz" (nombre completo)."""
    key = canonical_key(raw_name)
    parts = key.split("_")
    if len(parts) >= 2 and len(parts[-1]) <= 3:
        return key                       # ya viene en formato apellido_inicial
    if len(parts) >= 2:
        first, rest = parts[0], parts[1:]
        return "_".join(rest + [first[0]])   # "carlos_alcaraz" -> "alcaraz_c"
    return key


def load_rankings(manual_dir: Path, tour: str, ref_date: date, max_age_days: int = 45) -> RankingsAsOf:
    out = RankingsAsOf()
    path = Path(manual_dir) / f"rankings_{tour.lower()}.csv"
    if not path.exists():
        out.warnings.append(f"sin fichero de rankings {path.name}: fallback a Elo")
        return out
    df = pd.read_csv(path, comment="#", dtype=str).dropna(subset=["rank", "player"])
    if df.empty:
        out.warnings.append(f"{path.name} vacio: fallback a Elo")
        return out
    df["date_published"] = pd.to_datetime(df["date_published"], errors="coerce").dt.date
    bad = df["date_published"].isna()
    if bad.any():
        raise ValueError(f"{path.name}: {int(bad.sum())} filas con date_published invalida")
    future = df[df["date_published"] > ref_date]
    if len(future):
        out.warnings.append(f"{len(future)} filas con fecha futura IGNORADAS (regla as-of)")
    df = df[df["date_published"] <= ref_date]
    if df.empty:
        out.warnings.append("todas las publicaciones son futuras: fallback a Elo")
        return out
    pub = df["date_published"].max()
    snap = df[df["date_published"] == pub]
    out.published = pub
    age = (ref_date - pub).days
    if age > max_age_days:
        out.warnings.append(f"ranking usado tiene {age} dias (> {max_age_days}): revisar")
    for _, r in snap.iterrows():
        try:
            key = _to_td_key(r["player"])
            pts = float(r.get("points", 0) or 0)
            out.by_player[key] = (int(float(r["rank"])), pts)
        except (TypeError, ValueError):
            out.warnings.append(f"fila de ranking invalida ignorada: {dict(r)}")
    return out


def write_rankings_templates(manual_dir: Path) -> list[Path]:
    manual_dir = Path(manual_dir)
    manual_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for tour in ("atp", "wta"):
        p = manual_dir / f"rankings_{tour}.csv"
        if not p.exists():
            p.write_text(RANKINGS_TEMPLATE, encoding="utf-8")
        paths.append(p)
    return paths
