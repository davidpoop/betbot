"""Construcción del dataset canónico a partir de las fuentes raw.

Precedencia por tramo (documentada en docs/USO.md):
- ATP 2000–2019: MLT (Tennis-Data completo)  |  ATP 2020–2026: XLSX Tennis-Data.
- WTA 2007–2019: MLT (Tennis-Data completo)  |  WTA 2020–2025: daily pull (cuota única "TD1").
- El daily pull ATP no se fusiona: solo se usa para el informe de reconciliación.

Orientación neutral: player_a = min(clave_a, clave_b); label_a_wins = 1 si ganó A.
Las cuotas se re-orientan de (winner, loser) a (a, b).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from betbot.canonical.names import canonical_key
from betbot.canonical.score import parse_numeric_sets
from betbot.ingest import kaggle_daily, sackmann, tennis_data

STATUS_MAP = {
    "completed": "completed", "retired": "retired", "walkover": "walkover",
    "disqualified": "retired", "def.": "retired", "def": "retired", "sched": "other",
}


def _status(comment: str) -> str:
    c = str(comment or "").strip().lower()
    for k, v in STATUS_MAP.items():
        if k in c:
            return v
    return "completed" if c in ("", "nan") else "other"


def _rows_from_source(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for _, r in df.iterrows():
        w_key, l_key = canonical_key(r["winner_raw"]), canonical_key(r["loser_raw"])
        if not w_key or not l_key or w_key == l_key:
            continue
        status = _status(r.get("comment"))

        def _int(v, fallback: int) -> int:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return fallback

        if "_games_w" in r.index and pd.notna(r.get("_games_w")):
            games_w, games_l = _int(r["_games_w"], 0), _int(r["_games_l"], 0)
            sets_w = _int(r.get("wsets"), 0)
            sets_l = _int(r.get("lsets"), 0)
            set1_w = r.get("_set1_w")
            set1_w = _int(set1_w, 0) if set1_w is not None and pd.notna(set1_w) else None
        else:
            pairs = [(r.get(f"w{i}"), r.get(f"l{i}")) for i in range(1, 6)]
            ps = parse_numeric_sets(pairs)
            games_w, games_l = ps.games_w, ps.games_l
            sets_w = _int(r.get("wsets"), ps.sets_w) if pd.notna(r.get("wsets")) else ps.sets_w
            sets_l = _int(r.get("lsets"), ps.sets_l) if pd.notna(r.get("lsets")) else ps.sets_l
            set1_w = ps.set1_w if ps.set1_completed else None
        a_key, b_key = (w_key, l_key) if w_key < l_key else (l_key, w_key)
        a_is_winner = a_key == w_key
        odds_wl = r.get("odds") or {}
        odds_ab = {}
        for bk, wl in odds_wl.items():
            wo, lo = wl[0], wl[1]
            odds_ab[bk] = [wo, lo] if a_is_winner else [lo, wo]
        surface = str(r.get("surface") or "Hard").strip().title()
        if surface not in ("Hard", "Clay", "Grass", "Carpet"):
            surface = "Hard"
        rows.append({
            "tour": r["tour"], "date": r["date"], "tournament": str(r.get("tournament", "")).strip(),
            "series": str(r.get("series", "") or ""), "surface": surface,
            "indoor": str(r.get("court", "")).strip().lower() == "indoor",
            "round": str(r.get("round", "") or ""), "best_of": int(r.get("best_of", 3) or 3),
            "player_a": a_key, "player_b": b_key,
            "raw_name_a": str(r["winner_raw"]).strip() if a_is_winner else str(r["loser_raw"]).strip(),
            "raw_name_b": str(r["loser_raw"]).strip() if a_is_winner else str(r["winner_raw"]).strip(),
            "label_a_wins": (1 if a_is_winner else 0) if status != "walkover" else None,
            "status": status,
            "sets_a": sets_w if a_is_winner else sets_l,
            "sets_b": sets_l if a_is_winner else sets_w,
            "games_a": games_w if a_is_winner else games_l,
            "games_b": games_l if a_is_winner else games_w,
            "set1_winner_a": (set1_w if a_is_winner else (1 - set1_w)) if set1_w is not None else None,
            "rank_a": r.get("wrank") if a_is_winner else r.get("lrank"),
            "rank_b": r.get("lrank") if a_is_winner else r.get("wrank"),
            "pts_a": r.get("wpts") if a_is_winner else r.get("lpts"),
            "pts_b": r.get("lpts") if a_is_winner else r.get("wpts"),
            "odds_json": json.dumps(odds_ab),
            "source": r.get("source", ""),
        })
    return rows


def build_canonical(raw_dir: Path, out_dir: Path) -> dict:
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []

    mlt_atp = raw_dir / "mlt" / "df_atp.csv"
    if mlt_atp.exists():
        frames.append(tennis_data.load_mlt_csv(mlt_atp, "ATP"))
    for f in sorted((raw_dir / "tennis_data").glob("atp_*.xlsx")):
        frames.append(tennis_data.load_xlsx_year(f, "ATP"))
    mlt_wta = raw_dir / "mlt" / "df_wta.csv"
    if mlt_wta.exists():
        frames.append(tennis_data.load_mlt_csv(mlt_wta, "WTA"))
    kg_wta = raw_dir / "kaggle_daily" / "wta_data.csv"
    if kg_wta.exists():
        kw = kaggle_daily.load_daily_csv(kg_wta, "WTA")
        kw = kw[pd.to_datetime(kw["date"]).dt.year >= 2020]
        frames.append(kw)
    if not frames:
        raise FileNotFoundError(f"No hay fuentes raw en {raw_dir}. Ejecuta 'betbot download-data' o coloca ficheros.")

    all_rows: list[dict] = []
    for fr in frames:
        all_rows.extend(_rows_from_source(fr))
    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # match_id + dedupe (precedencia = orden de concatenación de fuentes)
    df["match_id"] = df.apply(
        lambda r: f"{r['tour']}_{r['date'].isoformat()}_{r['player_a']}__{r['player_b']}", axis=1)
    n_before = len(df)
    df = df.drop_duplicates(subset="match_id", keep="first").reset_index(drop=True)
    n_dupes = n_before - len(df)
    df = df.sort_values(["date", "tour", "tournament", "match_id"]).reset_index(drop=True)

    # Registro de jugadores + enriquecimiento biográfico (ATP; WTA sin fuente bio)
    players = _build_registry(df, raw_dir, out_dir)

    df.to_parquet(out_dir / "matches.parquet", index=False)
    players.to_parquet(out_dir / "players.parquet", index=False)

    summary = {
        "n_matches": int(len(df)),
        "n_dupes_removed": int(n_dupes),
        "by_tour": {t: int(n) for t, n in df["tour"].value_counts().items()},
        "date_min": str(df["date"].min()), "date_max": str(df["date"].max()),
        "status_counts": {k: int(v) for k, v in df["status"].value_counts().items()},
        "with_any_odds": int((df["odds_json"] != "{}").sum()),
        "n_players": int(len(players)),
    }
    (out_dir / "build_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _build_registry(df: pd.DataFrame, raw_dir: Path, out_dir: Path) -> pd.DataFrame:
    recs: dict[str, dict] = {}
    for _, r in df.iterrows():
        for side in ("a", "b"):
            key = r[f"player_{side}"]
            rec = recs.setdefault(key, {"player_id": key, "tours": set(), "n_matches": 0,
                                        "first_date": r["date"], "last_date": r["date"],
                                        "display_name": r[f"raw_name_{side}"]})
            rec["tours"].add(r["tour"])
            rec["n_matches"] += 1
            rec["first_date"] = min(rec["first_date"], r["date"])
            rec["last_date"] = max(rec["last_date"], r["date"])
    players = pd.DataFrame(sorted(recs.values(), key=lambda x: x["player_id"]))
    players["tours"] = players["tours"].map(lambda s: ",".join(sorted(s)))

    hand, dob, height, bio_src = [], [], [], []
    pfile = raw_dir / "sackmann" / "atp_players.csv"
    lookup: dict = {}
    quarantine: list[str] = []
    if pfile.exists():
        lookup, quarantine = sackmann.build_bio_lookup(sackmann.load_players(pfile))
    for _, p in players.iterrows():
        bio = sackmann.bio_for(p["player_id"], lookup) if "ATP" in p["tours"] else None
        hand.append((bio or {}).get("hand"))
        dob.append((bio or {}).get("dob"))
        height.append((bio or {}).get("height"))
        bio_src.append("sackmann" if bio else "")
    players["hand"], players["dob"], players["height"], players["bio_source"] = hand, dob, height, bio_src
    (out_dir / "bio_quarantine.json").write_text(json.dumps(quarantine, indent=2))
    return players
