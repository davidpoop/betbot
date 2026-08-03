"""Plantillas de importación manual del día (CSV). Las líneas que empiezan por
'#' son ejemplos/comentarios y se ignoran al importar."""
from __future__ import annotations

from pathlib import Path

MATCHES_TEMPLATE = """\
# Plantilla de partidos del dia. Borra los ejemplos y añade tus filas.
# tour: ATP|WTA · surface: Hard|Clay|Grass|Carpet · indoor: true|false · best_of: 3|5
# Los nombres, en formato Tennis-Data: "Apellido I." (p.ej. "Alcaraz C.", "Swiatek I.")
date,tour,tournament,surface,indoor,round,best_of,player_a,player_b
# 2026-08-04,ATP,Canadian Open,Hard,false,Quarterfinals,3,Alcaraz C.,Draper J.
# 2026-08-04,WTA,Canadian Open,Hard,false,Semifinals,3,Swiatek I.,Gauff C.
"""

ODDS_TEMPLATE = """\
# Plantilla de cuotas del dia (introducidas por ti; el sistema NUNCA scrapea).
# market: match_winner|set1_winner|wins_set|straight_sets|three_sets
# selection: a|b (jugador de TU columna player_a/player_b) · yes|no (para three_sets)
# timestamp opcional ISO (si falta, se usa la hora de importacion)
player_a,player_b,market,selection,odds,bookmaker,timestamp
# Alcaraz C.,Draper J.,match_winner,a,1.45,bet365,
# Alcaraz C.,Draper J.,match_winner,b,2.75,bet365,
# Alcaraz C.,Draper J.,straight_sets,a,2.10,bet365,
# Swiatek I.,Gauff C.,match_winner,a,1.52,winamax,
"""


def write_templates(out_dir: Path) -> list[Path]:
    from betbot.ingest.manual_results import RESULTS_TEMPLATE
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = out_dir / "day_matches.csv"
    p2 = out_dir / "day_odds.csv"
    p3 = out_dir / "recent_results.csv"
    p1.write_text(MATCHES_TEMPLATE, encoding="utf-8")
    p2.write_text(ODDS_TEMPLATE, encoding="utf-8")
    p3.write_text(RESULTS_TEMPLATE, encoding="utf-8")
    return [p1, p2, p3]
