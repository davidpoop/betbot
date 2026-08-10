"""Importación manual de resultados recientes (sin scraping ni descargas).

Plantilla: recent_results.csv (via `betbot template`). El marcador va SIEMPRE
orientado al GANADOR ("6-4 3-6 7-6"; en retiradas incluye el parcial: "6-4 3-1").

Validaciones por fila (las inválidas se rechazan con motivo; nada se corrige en
silencio): fecha parseable y NO futura; tour/superficie/best_of/status válidos;
ganador != perdedor; marcador coherente con best_of y status; duplicados (dentro
del fichero y contra el dataset fusionado). Nombres no reconocidos van a
CUARENTENA con sugerencias (o se aceptan como jugadores nuevos con allow_new).

Cada importación queda registrada con SHA256 en manual_imports_log.jsonl.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from betbot.canonical.names import canonical_key
from betbot.canonical.score import infer_retired, parse_score_string
from betbot.canonical.store import append_manual, existing_match_ids
from betbot.config import resolve_path

RESULTS_TEMPLATE = """\
# Resultados recientes introducidos a mano (via legal; sin scraping).
# score: orientado al GANADOR ("6-4 7-6(3)"); en retiradas, sets jugados incl. parcial ("6-4 3-1").
# status: completed | retired | walkover  ·  surface: Hard|Clay|Grass|Carpet  ·  best_of: 3|5
date,tour,tournament,surface,indoor,round,best_of,winner,loser,score,status
# 2026-08-02,ATP,Canadian Open,Hard,false,Semifinals,3,Alcaraz C.,Ruud C.,6-3 6-4,completed
# 2026-08-02,WTA,Canadian Open,Hard,false,The Final,3,Swiatek I.,Gauff C.,6-4 3-1,retired
"""

_VALID_SURFACES = {"Hard", "Clay", "Grass", "Carpet"}
_VALID_STATUS = {"completed", "retired", "walkover"}


def _row_error(i: int, msg: str) -> dict:
    return {"row": i, "error": msg}


def prepare_rows(df: pd.DataFrame, *, registry: set, known_ids: set,
                 source_label: str, allow_new: bool | str, today: date,
                 collect_dups: list | None = None
                 ) -> tuple[list[dict], list[dict], list[dict]]:
    """Núcleo de validación por filas (compartido por la importación manual y la
    sincronización automática). Devuelve (aceptadas, rechazadas, cuarentena).

    `collect_dups`: si se pasa una lista, las filas VÁLIDAS rechazadas por ser
    duplicado contra el dataset se depositan ahí ya parseadas — el sync las usa
    para enriquecer (ranks) o corregir (resultado) filas manuales existentes."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    quarantined: list[dict] = []
    seen_in_file: set[str] = set()

    for i, r in df.iterrows():
        rownum = i + 1
        try:
            d = pd.to_datetime(str(r["date"]).strip()).date()
        except (ValueError, TypeError):
            rejected.append(_row_error(rownum, f"fecha invalida: {r.get('date')}"))
            continue
        if d > today:
            rejected.append(_row_error(rownum, f"dato_futuro: {d} > {today}"))
            continue
        tour = str(r.get("tour", "")).strip().upper()
        if tour not in ("ATP", "WTA"):
            rejected.append(_row_error(rownum, f"tour invalido: {tour}"))
            continue
        surface = str(r.get("surface", "")).strip().title() or "Hard"
        if surface not in _VALID_SURFACES:
            rejected.append(_row_error(rownum, f"superficie invalida: {surface}"))
            continue
        status = str(r.get("status", "completed")).strip().lower() or "completed"
        if status not in _VALID_STATUS:
            rejected.append(_row_error(rownum, f"status invalido: {status}"))
            continue
        try:
            best_of = int(float(r.get("best_of", "3") or 3))
            assert best_of in (3, 5)
        except (ValueError, AssertionError):
            rejected.append(_row_error(rownum, f"best_of invalido: {r.get('best_of')}"))
            continue
        w_raw, l_raw = str(r.get("winner", "")).strip(), str(r.get("loser", "")).strip()
        w_key, l_key = canonical_key(w_raw), canonical_key(l_raw)
        if not w_key or not l_key or w_key == l_key:
            rejected.append(_row_error(rownum, f"jugadores invalidos: '{w_raw}' vs '{l_raw}'"))
            continue

        # ---------- cuarentena de nombres no reconocidos ----------
        # allow_new=True: acepta cualquier desconocido. allow_new=False: todos a
        # cuarentena. allow_new="unambiguous" (sync): acepta desconocidos SIN
        # parecido a nadie del registro (debutantes reales); si hay una clave
        # parecida (posible errata), a cuarentena — nunca se aproxima en silencio.
        unknown = [(name, key) for name, key in ((w_raw, w_key), (l_raw, l_key))
                   if key not in registry]
        if unknown and allow_new is not True:
            sugg = {name: difflib.get_close_matches(key, registry, n=3, cutoff=0.75)
                    for name, key in unknown}
            has_close = any(s for s in sugg.values())
            if allow_new != "unambiguous" or has_close:
                quarantined.append({"row": rownum, "winner": w_raw, "loser": l_raw,
                                    "date": str(d), "unknown": {n: s for n, s in sugg.items()},
                                    "hint": "corrige el nombre o reimporta con --allow-new"})
                continue

        # ---------- marcador ----------
        score_str = str(r.get("score", "")).strip()
        ps = parse_score_string(score_str)
        need = best_of // 2 + 1
        if status == "completed":
            if ps.sets_w != need or ps.sets_l >= need:
                rejected.append(_row_error(
                    rownum, f"marcador_incoherente para completed Bo{best_of}: '{score_str}' "
                            f"(sets ganador={ps.sets_w})"))
                continue
        elif status == "retired":
            if ps.n_sets == 0:
                rejected.append(_row_error(rownum, "retirada sin marcador: usa walkover si no se jugo"))
                continue
            if not infer_retired(ps, best_of) and ps.sets_w >= need:
                rejected.append(_row_error(
                    rownum, f"marcador completo con status retired: '{score_str}'"))
                continue
        elif status == "walkover" and ps.n_sets > 0:
            rejected.append(_row_error(rownum, f"walkover no debe llevar marcador: '{score_str}'"))
            continue

        a_key, b_key = (w_key, l_key) if w_key < l_key else (l_key, w_key)
        a_is_winner = a_key == w_key
        match_id = f"{tour}_{d.isoformat()}_{a_key}__{b_key}"
        is_dup_file = match_id in seen_in_file
        is_dup_dataset = (not is_dup_file) and match_id in known_ids
        if is_dup_file:
            rejected.append(_row_error(rownum, f"duplicado dentro del fichero: {match_id}"))
            continue

        # rank/puntos del momento del partido, si la fuente los publica (TML y
        # TennisCourtLog los traen); alimentan el ranking derivado automático
        def _num(v):
            try:
                x = float(v)
                return x if x > 0 else None
            except (TypeError, ValueError):
                return None
        w_rank, l_rank = _num(r.get("winner_rank")), _num(r.get("loser_rank"))
        w_pts, l_pts = _num(r.get("winner_rank_points")), _num(r.get("loser_rank_points"))

        set1_w = ps.set1_w if ps.set1_completed else None
        # procedencia por fila: los feeds del sync pueden marcar "_src" con su
        # nombre; queda "sync:{feed}" para distinguir después las fuentes de
        # cobertura PARCIAL (frescura) sin tocar la validación
        srcx = str(r.get("_src", "") or "").strip()
        row_source = f"{source_label}:{srcx}" if srcx and srcx.lower() != "nan" else source_label
        target = accepted
        if is_dup_dataset:
            rejected.append(_row_error(rownum, f"duplicado (ya existe en el dataset): {match_id}"))
            if collect_dups is None:
                continue
            target = collect_dups           # fila parseada para enriquecer/corregir
        else:
            seen_in_file.add(match_id)
        target.append({
            "tour": tour, "date": d, "tournament": str(r.get("tournament", "")).strip(),
            "series": "", "surface": surface,
            "indoor": str(r.get("indoor", "false")).strip().lower() in ("true", "1", "si", "sí", "yes"),
            "round": str(r.get("round", "")).strip(), "best_of": best_of,
            "player_a": a_key, "player_b": b_key,
            "raw_name_a": w_raw if a_is_winner else l_raw,
            "raw_name_b": l_raw if a_is_winner else w_raw,
            "label_a_wins": (1 if a_is_winner else 0) if status != "walkover" else None,
            "status": status,
            "sets_a": ps.sets_w if a_is_winner else ps.sets_l,
            "sets_b": ps.sets_l if a_is_winner else ps.sets_w,
            "games_a": ps.games_w if a_is_winner else ps.games_l,
            "games_b": ps.games_l if a_is_winner else ps.games_w,
            "set1_winner_a": (set1_w if a_is_winner else 1 - set1_w) if set1_w is not None else None,
            "rank_a": w_rank if a_is_winner else l_rank,
            "rank_b": l_rank if a_is_winner else w_rank,
            "pts_a": w_pts if a_is_winner else l_pts,
            "pts_b": l_pts if a_is_winner else w_pts,
            "odds_json": "{}",
            "source": row_source,
            "match_id": match_id,
        })
    return accepted, rejected, quarantined


def import_results(cfg: dict, file: Path, allow_new: bool = False,
                   dry_run: bool = False, today: date | None = None) -> dict:
    """Valida e incorpora resultados desde CSV. Devuelve informe con aceptadas/
    rechazadas/cuarentena. Con dry_run no escribe nada."""
    canon = resolve_path(cfg, "canonical_dir")
    file = Path(file)
    raw = file.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:16]
    today = today or datetime.now(timezone.utc).date()

    players_path = canon / "players.parquet"
    registry = set(pd.read_parquet(players_path)["player_id"]) if players_path.exists() else set()
    known_ids = existing_match_ids(canon)

    df = pd.read_csv(file, comment="#", dtype=str).fillna("")
    accepted, rejected, quarantined = prepare_rows(
        df, registry=registry, known_ids=known_ids,
        source_label=f"manual_import:{file.name}", allow_new=allow_new, today=today)

    report = {
        "file": str(file), "sha256_16": sha,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": int(len(df)), "n_accepted": len(accepted),
        "n_rejected": len(rejected), "n_quarantined": len(quarantined),
        "rejected": rejected, "quarantined": quarantined,
        "dry_run": dry_run, "allow_new": allow_new,
    }
    if dry_run or not accepted:
        if not dry_run:
            _log(canon, report)
        return report

    new_df = pd.DataFrame(accepted)
    total_manual = append_manual(canon, new_df)
    report["total_manual_rows"] = int(total_manual)
    if quarantined:
        qpath = canon / "import_quarantine.csv"
        qdf = pd.DataFrame(quarantined)
        if qpath.exists():
            qdf = pd.concat([pd.read_csv(qpath), qdf], ignore_index=True)
        qdf.to_csv(qpath, index=False)
        report["quarantine_file"] = str(qpath)
    _log(canon, report)
    return report


def _log(canon: Path, report: dict) -> None:
    with open(canon / "manual_imports_log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")
