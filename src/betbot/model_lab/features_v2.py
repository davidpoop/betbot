"""Catálogo de features v2 con FACTIBILIDAD verificada contra los datos reales.

Regla del mandato: una feature solo se implementa si puede reconstruirse
históricamente point-in-time. Este módulo registra los candidatos con su
veredicto de factibilidad (medido sobre data/canonical, no supuesto) y provee
`coverage_report(cfg)` para re-verificarlo con los datos vivos. La
IMPLEMENTACIÓN de las features factibles es FASE 2: aquí no se computa ninguna
todavía.

Hechos de cobertura que sustentan los veredictos (2026-08, 114.675 partidos):
- canonical: rank/pts ~100%, sets/games 100%, surface/indoor/round/series 100%,
  odds_json 89-100% según bloque; SIN minutos, SIN estadísticas de servicio.
- cuotas: una foto prematch por casa (tennis-data); ATP 2020s con 4 casas
  (Max/Avg/PS/B365) -> dispersión factible; WTA reciente solo TD1 -> dispersión
  NO factible en WTA moderna. No hay par apertura/cierre -> CLV solo aproximable.
- data/raw/sackmann contiene SOLO atp_players.csv (bio ATP). Los per-match de
  Sackmann (w_ace, w_df, svpt, minutes...) NO están descargados: serve/return
  y minutos requieren añadir esa fuente (CC BY-NC-SA, uso local no comercial).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from betbot.config import resolve_path


@dataclass(frozen=True)
class FeatureCandidate:
    name: str
    grupo: str
    descripcion: str
    requiere: str                # columnas/datos necesarios
    factible: str                # "sí" | "no" | "parcial"
    nota: str = ""


CATALOG: list[FeatureCandidate] = [
    # ---- RATINGS ----
    FeatureCandidate("elo_decay", "ratings", "Elo con decaimiento temporal hacia la media "
                     "en inactividad", "historial canonical (completo)", "sí"),
    FeatureCandidate("elo_uncertainty", "ratings", "nº efectivo de observaciones / varianza "
                     "del rating por jugador", "n_prev ya disponible en replay", "sí"),
    FeatureCandidate("elo_sos", "ratings", "strength of schedule: Elo medio de rivales "
                     "recientes", "historial canonical", "sí"),
    FeatureCandidate("elo_surface_share", "ratings", "peso relativo del Elo de superficie "
                     "según muestra en esa superficie", "surf_n del replay", "sí"),
    # ---- FORMA ----
    FeatureCandidate("form_ewma", "forma", "EWMA de resultados 30/60/90/365d en lugar de "
                     "win-rate simple", "fechas+resultados (100%)", "sí"),
    FeatureCandidate("form_opp_adj", "forma", "forma ajustada por Elo del rival",
                     "historial + Elo replay", "sí"),
    FeatureCandidate("form_surface", "forma", "forma específica por superficie",
                     "surface 100%", "sí"),
    FeatureCandidate("rating_momentum", "forma", "delta de Elo en los últimos k partidos",
                     "replay", "sí"),
    # ---- FATIGA / CARGA ----
    FeatureCandidate("matches_3_7_14d", "fatiga", "partidos en 3/7/14 días (14d ya existe)",
                     "fechas", "sí"),
    FeatureCandidate("sets_games_load", "fatiga", "sets y juegos disputados en 7/14 días",
                     "sets_a/b, games_a/b (100%)", "sí"),
    FeatureCandidate("minutes_load", "fatiga", "minutos en pista recientes",
                     "minutos por partido", "no",
                     "ninguna fuente actual trae minutos; requiere Sackmann per-match"),
    FeatureCandidate("consecutive_days", "fatiga", "días consecutivos jugando", "fechas", "sí"),
    FeatureCandidate("prev_tournament_depth", "fatiga", "profundidad alcanzada en el torneo "
                     "anterior", "round 100%", "sí"),
    # ---- CONTEXTO ----
    FeatureCandidate("tournament_level", "contexto", "nivel del torneo (series ya está en "
                     "canonical y NO se usa hoy)", "series 100%", "sí"),
    FeatureCandidate("round_onehot", "contexto", "ronda con codificación mejor que el escalar "
                     "actual", "round 100%", "sí"),
    FeatureCandidate("home_country", "contexto", "jugador local", "país del jugador + país "
                     "del torneo", "parcial",
                     "país de jugador: solo ATP vía sackmann players; torneo: registry"),
    FeatureCandidate("age", "contexto", "edad (ya existe age_diff con dob de Sackmann ATP)",
                     "dob", "parcial", "cobertura dob WTA baja; hoy se imputa 0 sin indicador"),
    # ---- RANKINGS ----
    FeatureCandidate("rank_momentum", "rankings", "variación del rank observado del jugador "
                     "entre sus partidos recientes", "rank_a/b por partido (~100%)", "sí"),
    FeatureCandidate("rank_missing_flag", "rankings", "indicador explícito de rank ausente "
                     "(hoy se imputa 500 SIN indicador)", "-", "sí"),
    # ---- MATCHUP ----
    FeatureCandidate("h2h_shrunk", "matchup", "H2H con shrinkage bayesiano fuerte hacia 0.5",
                     "historial de la pareja", "sí",
                     "prior Beta; 1-2 partidos apenas mueven la aguja, por diseño"),
    FeatureCandidate("h2h_surface", "matchup", "H2H por superficie con muestra mínima",
                     "historial + surface", "sí", "solo con n>=umbral pre-registrado"),
    # ---- SERVE/RETURN ----
    FeatureCandidate("serve_return_block", "serve", "hold%, break%, ace%, df%, 1st/2nd won, "
                     "return won, BP saved/converted, ajustados por rival",
                     "estadísticas por partido", "no",
                     "NO existen en ninguna fuente actual del repo; requieren ingestar los "
                     "per-match de Sackmann (ATP/WTA hasta ~temporada previa) y resolver el "
                     "enlace de identidades"),
    # ---- MERCADO (capa separada, §7 del mandato) ----
    FeatureCandidate("book_dispersion", "mercado", "dispersión entre casas como señal de "
                     "incertidumbre del mercado", ">=2 casas por partido", "parcial",
                     "ATP 2020s: 4 casas (sí); WTA moderna: solo TD1 (no)"),
    FeatureCandidate("market_prob_feature", "mercado", "p_novig como feature de un challenger "
                     "CLARAMENTE separado del modelo deportivo (ya existe como M5)",
                     "odds_json 89-100%", "sí",
                     "mantener la separación SPORTS MODEL vs MARKET/VALUE MODEL"),
]


def coverage_report(cfg: dict) -> dict:
    """Re-verifica sobre los datos vivos los hechos que sustentan el catálogo."""
    canon = resolve_path(cfg, "canonical_dir")
    m = pd.read_parquet(canon / "matches.parquet")
    m = m.assign(year=pd.to_datetime(m["date"]).dt.year)
    out: dict = {"n": int(len(m)), "rango": [str(m['date'].min()), str(m['date'].max())],
                 "tours": {}}
    for t, s in m.groupby("tour"):
        has_odds = s["odds_json"].fillna("").map(lambda x: isinstance(x, str) and len(x) > 2)
        out["tours"][str(t)] = {
            "n": int(len(s)),
            "rank_pct": round(100 * s["rank_a"].notna().mean(), 1),
            "sets_pct": round(100 * s["sets_a"].notna().mean(), 1),
            "games_pct": round(100 * s["games_a"].notna().mean(), 1),
            "odds_pct": round(100 * has_odds.mean(), 1),
            "series_pct": round(100 * s["series"].notna().mean(), 1) if "series" in s else 0.0,
        }
    out["columnas_sin_usar_por_champion"] = ["series", "games_a", "games_b",
                                             "sets_a/b (solo targets derivados)",
                                             "casas adicionales de odds_json"]
    out["ausentes_en_toda_fuente"] = ["minutos", "aces", "dobles faltas", "puntos de saque",
                                      "breaks", "estadísticas de resto"]
    return out
