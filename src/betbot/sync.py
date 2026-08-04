"""`betbot sync-results`: sincronización automática de resultados recientes.

Backfill desde la última fecha local de cada circuito (con solape de 2 días
para dedupe) hasta hoy, con tope configurable feeds.sync_max_days por ciclo.
Reutiliza el núcleo de validación/dedupe/cuarentena de la importación manual y
el refresco determinista de Elo/estado. Append-only; una segunda ejecución no
duplica partidos ni altera el Elo.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from betbot.canonical.store import append_manual, existing_match_ids, load_matches
from betbot.config import resolve_path
from betbot.feeds.base import SourceStatus
from betbot.ingest.manual_results import prepare_rows


def local_freshness(cfg: dict) -> dict[str, date]:
    canon = resolve_path(cfg, "canonical_dir")
    try:
        m = load_matches(canon)
    except FileNotFoundError:
        return {}
    return {t: max(g["date"]) for t, g in m.groupby("tour")}


def default_results_sources(cfg: dict) -> list:
    from betbot.feeds.results import SportradarResults
    from betbot.feeds.results_github import GithubResults
    fcfg = cfg.get("feeds", {})
    ttl = int(fcfg.get("ttl_seconds", 900))
    available = {"sportradar": lambda: SportradarResults(ttl_seconds=ttl),
                 "github": lambda: GithubResults(ttl_seconds=ttl)}
    order = fcfg.get("results_order", ["sportradar", "github"])
    return [available[n]() for n in order if n in available]


def run_sync(cfg: dict, sources: list | None = None, days: int | None = None,
             refresh: bool = True, today: date | None = None) -> dict:
    canon = resolve_path(cfg, "canonical_dir")
    today = today or datetime.now(timezone.utc).date()
    if sources is None:
        sources = default_results_sources(cfg)
    fresh_before = local_freshness(cfg)
    max_days = days or int(cfg.get("feeds", {}).get("sync_max_days", 30))
    if days is not None:
        # --days explícito: backfill forzado de N días (rellena huecos aunque
        # la fecha máxima local esté inflada por importaciones puntuales)
        since = today - timedelta(days=days)
    else:
        base = min(fresh_before.values()) if fresh_before else today - timedelta(days=max_days)
        since = max(base - timedelta(days=2), today - timedelta(days=max_days))

    statuses: list[SourceStatus] = []
    raw_rows: list[dict] = []
    for src in sources:
        try:
            rows, st = src.fetch_results(since, today)
        except Exception as exc:  # noqa: BLE001 - una fuente caída no detiene el sync
            rows, st = [], SourceStatus(name=getattr(src, "name", "?"), ok=False, error=str(exc))
        statuses.append(st)
        raw_rows.extend(r for r in rows if r.get("_level", "main") == "main")

    report: dict = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "window": {"since": str(since), "until": str(today), "max_days": max_days},
        "sources": [{"name": s.name, "ok": s.ok, "n": s.n_items, "error": s.error,
                     "notes": s.notes} for s in statuses],
        "n_fetched_main": len(raw_rows),
        "freshness_before": {k: str(v) for k, v in fresh_before.items()},
    }
    accepted: list[dict] = []
    rejected: list[dict] = []
    quarantined: list[dict] = []
    n_near = 0
    if raw_rows:
        df = pd.DataFrame(raw_rows).drop(columns=["_level"], errors="ignore").astype(str)
        players_path = canon / "players.parquet"
        registry = set(pd.read_parquet(players_path)["player_id"]) if players_path.exists() else set()
        # nombres completos ("Taylor Fritz") -> clave del registro ("fritz_t");
        # sin coincidencia se conserva el nombre original y acaba en cuarentena
        from betbot.canonical.names import resolve_full_name

        def _resolve(n: str) -> str:
            k = resolve_full_name(n, registry)
            return k if k in registry else n

        for col in ("winner", "loser"):
            df[col] = df[col].map(_resolve)
        # casi-duplicados: mismo par de jugadoras/es con fecha desplazada ±2 días
        # respecto a un partido ya almacenado (p.ej. import manual vs feed)
        near_dup = _near_duplicate_mask(df, canon, since, today)
        n_near = int(near_dup.sum())
        df = df[~near_dup]
        accepted, rejected, quarantined = prepare_rows(
            df, registry=registry, known_ids=existing_match_ids(canon),
            source_label="sync", allow_new="unambiguous", today=today)
        if accepted:
            append_manual(canon, pd.DataFrame(accepted))
            n_new_players = _register_new_players(canon, accepted, registry)
            report["n_new_players"] = n_new_players
        if quarantined:
            qpath = canon / "import_quarantine.csv"
            qdf = pd.DataFrame(quarantined)
            if qpath.exists():
                qdf = pd.concat([pd.read_csv(qpath), qdf], ignore_index=True)
            qdf.to_csv(qpath, index=False)
    dup = sum(1 for r in rejected if "duplicado" in r.get("error", ""))
    report.update({
        "n_accepted": len(accepted), "n_rejected": len(rejected),
        "n_duplicates_skipped": dup + n_near, "n_near_duplicates": n_near,
        "n_quarantined": len(quarantined),
        "quarantine_sample": quarantined[:5],
        "rejected_sample": [r for r in rejected if "duplicado" not in r.get("error", "")][:5],
    })
    if accepted and refresh:
        from betbot.state import refresh_state
        report["state"] = refresh_state(cfg)
    report["freshness_after"] = {k: str(v) for k, v in local_freshness(cfg).items()}
    with open(canon / "manual_imports_log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"sync": True, **{k: v for k, v in report.items()
                                              if k not in ("quarantine_sample", "rejected_sample")}},
                            ensure_ascii=False, default=str) + "\n")
    return report


def _register_new_players(canon: Path, accepted: list[dict], registry: set) -> int:
    """Da de alta en players.parquet a los debutantes aceptados (sin bio; la
    reconstruye `betbot prepare-data` cuando el mirror los incorpore)."""
    recs: dict[str, dict] = {}
    for r in accepted:
        for side in ("a", "b"):
            key = r[f"player_{side}"]
            if key in registry or key in recs:
                continue
            recs[key] = {"player_id": key, "tours": r["tour"], "n_matches": 0,
                         "first_date": r["date"], "last_date": r["date"],
                         "display_name": r[f"raw_name_{side}"],
                         "hand": None, "dob": None, "height": None,
                         "bio_source": "sync"}
    if not recs:
        return 0
    players = pd.read_parquet(canon / "players.parquet")
    merged = pd.concat([players, pd.DataFrame(list(recs.values()))], ignore_index=True)
    merged = merged.drop_duplicates(subset="player_id", keep="first")
    merged.to_parquet(canon / "players.parquet", index=False)
    registry.update(recs)
    return len(recs)


def _near_duplicate_mask(df: pd.DataFrame, canon: Path, since: date,
                         until: date) -> pd.Series:
    """True para filas que repiten un partido ya almacenado con la fecha
    desplazada ±2 días: mismo par, MISMO ganador y MISMO marcador por sets.
    (Una revancha real a los pocos días —p.ej. final de Eastbourne y R128 de
    Wimbledon 2026 Bergs–Humbert— difiere en sets y NO se filtra.)"""
    from betbot.canonical.names import canonical_key
    from betbot.canonical.score import parse_score_string
    try:
        existing = load_matches(canon)
    except FileNotFoundError:
        return pd.Series(False, index=df.index)
    lo, hi = since - timedelta(days=3), until + timedelta(days=3)
    win = existing[(existing["date"] >= lo) & (existing["date"] <= hi)]
    by_pair: dict[tuple, list[tuple]] = {}
    for _, r in win.iterrows():
        rec = (r["date"], r.get("label_a_wins"), r.get("sets_a"), r.get("sets_b"))
        by_pair.setdefault((r["tour"], r["player_a"], r["player_b"]), []).append(rec)
    mask = []
    for _, r in df.iterrows():
        try:
            d = pd.to_datetime(str(r["date"])).date()
        except (ValueError, TypeError):
            mask.append(False)
            continue
        w, l = canonical_key(str(r["winner"])), canonical_key(str(r["loser"]))
        a, b = (w, l) if w < l else (l, w)
        a_wins = 1 if a == w else 0
        ps = parse_score_string(str(r.get("score", "")))
        sets_a = ps.sets_w if a_wins else ps.sets_l
        sets_b = ps.sets_l if a_wins else ps.sets_w
        near = False
        for (dx, lab, sa, sb) in by_pair.get((str(r["tour"]).upper(), a, b), []):
            if (0 < abs((d - dx).days) <= 2 and not pd.isna(lab)
                    and int(lab) == a_wins and sa == sets_a and sb == sets_b):
                near = True
                break
        mask.append(near)
    return pd.Series(mask, index=df.index)


def freshness_header(cfg: dict) -> str:
    fresh = local_freshness(cfg)
    today = datetime.now(timezone.utc).date()
    lines = ["Frescura resultados:"]
    for t in ("ATP", "WTA"):
        if t in fresh:
            age = (today - fresh[t]).days
            warn = "" if age <= 2 else f"  ⚠ {age} días de retraso"
            lines.append(f"  {t}: actualizado hasta {fresh[t]}{warn}")
        else:
            lines.append(f"  {t}: sin datos")
    return "\n".join(lines)
