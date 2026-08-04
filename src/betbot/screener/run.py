"""Screener diario: importación manual de partidos y cuotas -> tabla de oportunidades.

Estados: fuerte | normal | experimental | vigilar_precio | probable_sin_value |
sin_value | descartada (con reason codes). Una unica recommended_primary por
partido. Exporta CSV y anota cada ejecucion en el ledger (JSONL, append-only).
"""
from __future__ import annotations

import difflib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import click
import joblib
import numpy as np
import pandas as pd

from betbot.canonical.names import canonical_key, extend_initials
from betbot.config import resolve_path
from betbot.features.builder import _ROUND_LEVEL, _MAX_REST_DAYS  # reutiliza las mismas constantes
from betbot.models import derived as dv
from betbot.models.derived import final_set_markets
from betbot.schemas import DayMatch, OddsQuote
from betbot.value.engine import MarketEval, evaluate_selection, pick_recommended_primary

EXPERIMENTAL_MARKETS = {"set1_winner", "wins_set", "straight_sets", "three_sets"}


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, comment="#", skip_blank_lines=True, dtype=str).fillna("")


def _activity(state: dict, tour: str, pid: str, d: date, cfg_sel: dict) -> dict:
    """Actividad as-of con corrección de frescura: si NUESTROS datos del circuito
    terminan mucho antes del partido (retraso del proveedor), la inactividad se
    mide contra la fecha de frescura F, no contra la fecha del partido — así una
    jugadora activa hasta F no se convierte en un falso OOD."""
    key = f"{tour}|{pid}"
    ld = state["last_date"].get(key)
    last = date.fromisoformat(ld) if ld else None
    f_raw = (state.get("freshness") or {}).get(tour)
    fresh = date.fromisoformat(f_raw) if f_raw else d
    lag_days = max(0, (d - fresh).days)
    freshness_unknown = lag_days > int(cfg_sel.get("freshness_lag_max_days", 14))
    ref = min(d, fresh) if freshness_unknown else d      # ventana efectiva de observación
    rest = min(float((d - last).days), _MAX_REST_DAYS) if last else _MAX_REST_DAYS
    rec = [date.fromisoformat(x) for x in state["recent"].get(key, [])]
    m14 = sum(1 for x in rec if 0 <= (d - x).days <= 14)
    m12 = sum(1 for x in rec if 0 <= (ref - x).days <= 365)
    lr = state["last_retired"].get(key)
    lr_d = date.fromisoformat(lr) if lr else None
    gap_vs_data = (fresh - last).days if last else 10 ** 6
    return {
        "last_date": last, "rest": rest, "m14": m14, "m12": m12,
        "layoff": 1.0 if (last and (d - last).days >= cfg_sel["layoff_days_ood"]) else 0.0,
        "retired_recent": 1.0 if (lr_d and (d - lr_d).days <= 30) else 0.0,
        # inactividad REAL: medida dentro de la ventana observada, no inflada por
        # el retraso de la fuente
        "stale": last is None or gap_vs_data >= cfg_sel["stale_days_unknown"],
        "freshness_unknown": freshness_unknown,
        "freshness_date": fresh, "lag_days": lag_days,
    }


def _build_feature_row(m: DayMatch, a_c: str, b_c: str, elo_state, act_a: dict, act_b: dict,
                       bio: dict, market_logit: float,
                       rank_a: tuple[int, float] | None = None,
                       rank_b: tuple[int, float] | None = None) -> pd.DataFrame:
    def _age(pid: str) -> float | None:
        d0 = bio.get(pid, {}).get("dob")
        if d0 is None or (isinstance(d0, float) and math.isnan(d0)) or pd.isna(d0):
            return None
        d0 = pd.Timestamp(d0).date()
        return (m.date - d0).days / 365.25

    def _lefty(pid: str) -> float:
        return 1.0 if bio.get(pid, {}).get("hand") == "L" else 0.0

    age_a, age_b = _age(a_c), _age(b_c)
    surface = m.surface.title()
    row = {
        "elo_diff": (elo_state.get(a_c) - elo_state.get(b_c)) / 100.0,
        "elo_surf_diff": (elo_state.get_surf(a_c, surface) - elo_state.get_surf(b_c, surface)) / 100.0,
        "log_rank_ratio": (math.log(rank_b[0] / rank_a[0])
                           if rank_a and rank_b and rank_a[0] > 0 and rank_b[0] > 0 else 0.0),
        "log_pts_ratio": (math.log1p(rank_a[1]) - math.log1p(rank_b[1])
                          if rank_a and rank_b else 0.0),
        "rest_diff": (act_a["rest"] - act_b["rest"]) / 7.0,
        "m14_diff": float(act_a["m14"] - act_b["m14"]),
        "m12m_diff": (act_a["m12"] - act_b["m12"]) / 10.0,
        "layoff_diff": act_a["layoff"] - act_b["layoff"],
        "retired_recent_diff": act_a["retired_recent"] - act_b["retired_recent"],
        "age_diff": ((age_a - age_b) / 5.0) if (age_a is not None and age_b is not None) else 0.0,
        "lefty_diff": _lefty(a_c) - _lefty(b_c),
        "experience_diff": math.log1p(elo_state.n.get(a_c, 0)) - math.log1p(elo_state.n.get(b_c, 0)),
        "best_of5": 1.0 if m.best_of == 5 else 0.0,
        "surface_clay": 1.0 if surface == "Clay" else 0.0,
        "surface_grass": 1.0 if surface == "Grass" else 0.0,
        "surface_carpet": 1.0 if surface == "Carpet" else 0.0,
        "indoor": 1.0 if m.indoor else 0.0,
        "round_level": float(_ROUND_LEVEL.get(m.round.strip().lower(), 1)) / 7.0,
        "market_logit": market_logit,
    }
    return pd.DataFrame([row])


def parse_matches_df(mdf: pd.DataFrame) -> tuple[list[DayMatch], list[str]]:
    day_matches: list[DayMatch] = []
    errors: list[str] = []
    for i, r in mdf.iterrows():
        try:
            if not str(r.get("player_a", "")).strip():
                continue
            day_matches.append(DayMatch(
                date=r["date"], tour=str(r["tour"]).strip().upper(), tournament=r["tournament"],
                surface=r.get("surface", "Hard") or "Hard",
                indoor=str(r.get("indoor", "false")).strip().lower() in ("true", "1", "si", "sí", "yes"),
                round=r.get("round", ""), best_of=int(float(r.get("best_of", "3") or 3)),
                player_a=r["player_a"], player_b=r["player_b"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fila {i + 1} de partidos invalida: {exc}")
    return day_matches, errors


def parse_odds_df(odf: pd.DataFrame, now: datetime) -> tuple[list[OddsQuote], list[str]]:
    quotes: list[OddsQuote] = []
    errors: list[str] = []
    for i, r in odf.iterrows():
        try:
            if not str(r.get("player_a", "")).strip():
                continue
            ts = str(r.get("timestamp", "") or "").strip()
            quotes.append(OddsQuote(
                player_a=r["player_a"], player_b=r["player_b"], market=str(r["market"]).strip(),
                selection=str(r["selection"]), odds=float(r["odds"]),
                bookmaker=r.get("bookmaker", "manual") or "manual",
                timestamp=ts if ts else now.isoformat()))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fila {i + 1} de cuotas invalida: {exc}")
    return quotes, errors


def screen_day(cfg: dict, day_matches: list[DayMatch], quotes: list[OddsQuote],
               log_ledger: bool = True) -> tuple[pd.DataFrame, list[str]]:
    """Núcleo del screener (compartido por CLI y UI). Devuelve (tabla, avisos)."""
    art = resolve_path(cfg, "artifacts_dir")
    canon = resolve_path(cfg, "canonical_dir")
    ledger_dir = resolve_path(cfg, "ledger_dir")
    bundle = joblib.load(art / "model_bundle.joblib")
    players = pd.read_parquet(canon / "players.parquet")
    registry = set(players["player_id"])
    bio = {p.player_id: {"dob": p.dob, "hand": p.hand} for p in players.itertuples(index=False)}
    cfg_sel = cfg["selection"]
    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    from betbot.ingest.rankings import load_rankings
    manual_dir = resolve_path(cfg, "manual_dir")
    rankings = {}
    for tour in ("ATP", "WTA"):
        rk = load_rankings(manual_dir, tour, now.date(),
                           int(cfg.get("rankings", {}).get("max_age_days", 45)))
        rankings[tour] = rk
        warnings.extend(f"[rankings {tour}] {w}" for w in rk.warnings)

    rows: list[dict] = []
    for m in day_matches:
        rows.extend(_screen_match(m, quotes, bundle, registry, bio, cfg_sel, now,
                                  rankings=rankings))
    for r in rows:
        r["model_git_sha"] = bundle["meta"].get("git_sha")
        r["model_data_hash"] = bundle["meta"].get("data_hash")
    out = pd.DataFrame(rows)

    if log_ledger:
        ledger_file = ledger_dir / "screen_runs.jsonl"
        with open(ledger_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_at": now.isoformat(), "n_matches": len(day_matches), "n_quotes": len(quotes),
                "model_meta": {k: bundle["meta"][k] for k in ("trained_at", "git_sha", "data_hash")},
                "thresholds": {k: cfg_sel[k] for k in ("ev_min", "edge_min", "ev_strong", "edge_strong")},
                "rows": rows}, ensure_ascii=False, default=str) + "\n")
    return out, warnings


def run_screener(cfg: dict, matches_path: Path, odds_path: Path | None,
                 out_path: Path | None) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    day_matches, errs_m = parse_matches_df(_read_csv(matches_path))
    quotes: list[OddsQuote] = []
    errs_o: list[str] = []
    if odds_path is not None:
        quotes, errs_o = parse_odds_df(_read_csv(odds_path), now)
    for e in errs_m + errs_o:
        click.echo(f"AVISO {e}", err=True)
    out, warnings = screen_day(cfg, day_matches, quotes)
    for w in warnings:
        click.echo(w, err=True)

    # ---------- salida ----------
    if len(out):
        cols = ["match", "tour", "market", "selection_name", "bookmaker", "odds", "p_model",
                "fair_odds", "p_novig", "edge", "ev", "ev_cons", "o_min", "state",
                "recommended_primary", "reasons"]
        printable = out[cols].copy()
        order = {"fuerte": 0, "normal": 1, "experimental": 2, "vigilar_precio": 3,
                 "probable_sin_value": 4, "sin_value": 5, "descartada": 6}
        printable = printable.sort_values(["recommended_primary", "state", "ev_cons"],
                                          ascending=[False, True, False],
                                          key=lambda s: s.map(order) if s.name == "state" else s)
        click.echo(printable.to_string(index=False, max_colwidth=28))
    else:
        click.echo("Sin filas: revisa las plantillas.")

    if out_path is not None and len(out):
        out.to_csv(out_path, index=False)
        click.echo(f"\nExportado: {out_path}")
    click.echo(f"Ledger actualizado: {resolve_path(cfg, 'ledger_dir') / 'screen_runs.jsonl'}")
    return out


def _screen_match(m: DayMatch, quotes: list[OddsQuote], bundle: dict, registry: set,
                  bio: dict, cfg_sel: dict, now: datetime,
                  rankings: dict | None = None) -> list[dict]:
    tour = m.tour.value
    tb = bundle["tours"][tour]
    elo_state = bundle["elo_states"][tour]
    act_state = bundle["activity_state"]
    ka, kb = canonical_key(m.player_a), canonical_key(m.player_b)
    # extensión determinista de iniciales ("Struff J." -> struff_j_l) solo si
    # hay exactamente una candidata en el registro; nunca coincidencia difusa
    ka, kb = extend_initials(ka, registry), extend_initials(kb, registry)
    match_label = f"{m.player_a} vs {m.player_b}"
    base = {"match": match_label, "tour": tour, "date": str(m.date),
            "tournament": m.tournament, "surface": m.surface.title(), "round": m.round,
            "best_of": m.best_of, "player_a": m.player_a, "player_b": m.player_b,
            "recommended_primary": False}

    # ---------- resolución de identidades ----------
    hard_flags: list[str] = []
    for name, key in ((m.player_a, ka), (m.player_b, kb)):
        if key not in registry:
            sugg = difflib.get_close_matches(key, registry, n=3, cutoff=0.75)
            hard_flags.append(f"jugador_desconocido:{name}" + (f" (¿{'|'.join(sugg)}?)" if sugg else ""))
    soft_flags: list[str] = []
    if not hard_flags:
        a_c, b_c = (ka, kb) if ka < kb else (kb, ka)
        flip = a_c != ka          # el jugador A del usuario es el B canónico
        act_a = _activity(act_state, tour, a_c, m.date, cfg_sel)
        act_b = _activity(act_state, tour, b_c, m.date, cfg_sel)
        if act_a["freshness_unknown"]:
            soft_flags.append(f"data_freshness_unknown({tour} hasta "
                              f"{act_a['freshness_date']}, {act_a['lag_days']}d de retraso)")
        for pid, act in ((a_c, act_a), (b_c, act_b)):
            if act["stale"]:
                hard_flags.append(f"ood_sin_actividad_reciente:{pid}")
            elif act["m12"] < cfg_sel["min_matches_12m"]:
                if act["freshness_unknown"]:
                    # el retraso es de la FUENTE: aviso, no falso OOD
                    soft_flags.append(f"historial_escaso_en_datos:{pid}(m12={act['m12']})")
                else:
                    hard_flags.append(f"ood_pocos_partidos_12m:{pid}({act['m12']})")

    # cuotas del partido (acepta también el orden invertido de jugadores)
    mq: list[OddsQuote] = []
    for q in quotes:
        qa, qb = canonical_key(q.player_a), canonical_key(q.player_b)
        if qa == ka and qb == kb:
            mq.append(q)
        elif qa == kb and qb == ka:
            swapped = {"a": "b", "b": "a"}.get(q.selection, q.selection)
            mq.append(q.model_copy(update={"player_a": q.player_b, "player_b": q.player_a,
                                           "selection": swapped}))

    if hard_flags:
        return [base | MarketEval(market="match_winner", selection="-",
                                  selection_name=match_label, odds=None).to_dict()
                | {"state": "descartada", "reasons": ";".join(hard_flags)}]

    # ---------- feature de mercado (match_winner con ambos lados, prioridad por libro) ----------
    def _quote(market: str, sel: str, book: str | None = None) -> OddsQuote | None:
        for q in mq:
            if q.market == market and q.selection == sel and (book is None or q.bookmaker == book):
                return q
        return None

    books = [q.bookmaker for q in mq if q.market == "match_winner"]
    market_logit, p_novig_feat = 0.0, None
    for bk in dict.fromkeys(books):
        qa, qb = _quote("match_winner", "a", bk), _quote("match_winner", "b", bk)
        if qa and qb:
            ia, ib = 1 / qa.odds, 1 / qb.odds
            p_user_a = ia / (ia + ib)
            p_novig_feat = 1 - p_user_a if flip else p_user_a       # orientar a canónico
            market_logit = math.log(p_novig_feat / (1 - p_novig_feat))
            break
    has_market = p_novig_feat is not None

    # ---------- rankings actuales (as-of; fallback a Elo si faltan) ----------
    rank_a = rank_b = None
    rank_notes: list[str] = []
    rk = (rankings or {}).get(tour)
    if rk is not None and rk.published is not None:
        ra_hit, rb_hit = rk.lookup(a_c), rk.lookup(b_c)
        if ra_hit and rb_hit:
            rank_a, rank_b = ra_hit, rb_hit
        else:
            for pid, hit in ((a_c, ra_hit), (b_c, rb_hit)):
                if not hit:
                    rank_notes.append(f"sin_ranking_actual:{pid}")
    else:
        rank_notes.append("sin_rankings_cargados_fallback_elo")

    # ---------- predicción ----------
    feat = _build_feature_row(m, a_c, b_c, elo_state, act_a, act_b, bio, market_logit,
                              rank_a=rank_a, rank_b=rank_b)
    p4 = float(tb["match_calibrators"]["M4_full"].transform(tb["match_models"]["M4_full"].predict(feat))[0])
    if has_market:
        p5 = float(tb["match_calibrators"]["M5_market"].transform(
            tb["match_models"]["M5_market"].predict(feat))[0])
        p_match_c, sigma_match = p5, 0.02 + abs(p5 - p4) / 2
    else:
        p_match_c, sigma_match = p4, 0.03 + 0.01
    w_shrink = float(bundle["shrink_w"].get(tour, 0.5)) if has_market else 1.0

    derived_ok = m.best_of == 3
    if derived_ok:
        dcfg = tb["derived_cfg"]
        dm = tb["derived_models"]
        p_dir = {
            "set1": float(dcfg["set1"]["cal_direct"].transform(dm["set1"].predict(feat))[0]),
            "wins_a": float(dcfg["wins_set"]["cal_direct"].transform(dm["wins_set"].predict(feat, "a"))[0]),
            "wins_b": float(dcfg["wins_set"]["cal_direct"].transform(dm["wins_set"].predict(feat, "b"))[0]),
            "str_a": float(dcfg["straight"]["cal_direct"].transform(dm["straight"].predict(feat, "a"))[0]),
            "str_b": float(dcfg["straight"]["cal_direct"].transform(dm["straight"].predict(feat, "b"))[0]),
            "three": float(dcfg["three"]["cal_direct"].transform(dm["three"].predict(feat))[0]),
        }
        def _combo(mkt: str, direct: float, bridge_key: str) -> float:
            pb = float(dcfg[mkt]["cal_bridge"].transform(
                np.array([dv.bridge_prob(bridge_key if bridge_key in dv.DERIVED_MARKETS else mkt,
                                         p_match_c)]))[0])
            p = float(dv.combine(np.array([direct]), np.array([pb]), dcfg[mkt]["w_direct"])[0])
            cp = dcfg[mkt].get("conditional_platt")
            if cp is not None:   # recalibracion condicional adoptada tras estudio en validacion
                p = float(cp.transform(np.array([p]), np.array([p_match_c]))[0])
            return p
        mk = final_set_markets(
            m=p_match_c,
            p_set1=_combo("set1", p_dir["set1"], "set1"),
            p_wins_a=_combo("wins_set", p_dir["wins_a"], "wins_set"),
            p_wins_b=1 - _combo("straight", p_dir["str_a"], "straight"),
            p_straight_a=_combo("straight", p_dir["str_a"], "straight"),
            p_straight_b=1 - _combo("wins_set", p_dir["wins_a"], "wins_set"),
        )
        sigma_deriv = 0.03
    else:
        mk = {"match_a": p_match_c}
        sigma_deriv = 0.05

    # probabilidad de cada (mercado, selección canónica)
    def p_of(market: str, sel_user: str) -> float | None:
        sel = sel_user
        if market in ("match_winner", "set1_winner", "wins_set", "straight_sets") and flip:
            sel = "b" if sel_user == "a" else "a"
        table = {
            "match_winner": {"a": mk["match_a"], "b": 1 - mk["match_a"]},
            "set1_winner": {"a": mk.get("set1_a"), "b": 1 - mk.get("set1_a", 0.5)},
            "wins_set": {"a": mk.get("wins_set_a"), "b": mk.get("wins_set_b")},
            "straight_sets": {"a": mk.get("straight_a"), "b": mk.get("straight_b")},
            "three_sets": {"yes": mk.get("three_sets"), "no": 1 - mk.get("three_sets", 0.5)},
        }
        v = table.get(market, {}).get(sel)
        return None if v is None else float(v)

    # Complementos REALES por mercado. Ojo: straight_sets(a) y wins_set(b) son
    # complementarios (P(A 2-0) + P(B gana >=1 set) = 1); los lados a/b de
    # straight_sets NO lo son entre sí.
    opp_map = {
        ("match_winner", "a"): ("match_winner", "b"), ("match_winner", "b"): ("match_winner", "a"),
        ("set1_winner", "a"): ("set1_winner", "b"), ("set1_winner", "b"): ("set1_winner", "a"),
        ("three_sets", "yes"): ("three_sets", "no"), ("three_sets", "no"): ("three_sets", "yes"),
        ("straight_sets", "a"): ("wins_set", "b"), ("straight_sets", "b"): ("wins_set", "a"),
        ("wins_set", "a"): ("straight_sets", "b"), ("wins_set", "b"): ("straight_sets", "a"),
    }
    evals: list[MarketEval] = []
    seen: set[tuple] = set()
    for q in mq:
        keyq = (q.market, q.selection, q.bookmaker)
        if keyq in seen:
            continue
        seen.add(keyq)
        flags = []
        if not derived_ok and q.market != "match_winner":
            flags.append("mercado_de_sets_no_soportado_en_bo5")
        ts = pd.Timestamp(q.timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        age_h = (now - ts.to_pydatetime()).total_seconds() / 3600
        max_age = float(cfg_sel.get("odds_max_age_hours_by_book", {}).get(
            q.bookmaker, cfg_sel["odds_max_age_hours"]))
        if age_h > max_age:
            flags.append(f"cuota_caducada({age_h:.1f}h)")
        p_sel = p_of(q.market, q.selection)
        if p_sel is None:
            continue
        opp_market, opp_sel = opp_map[(q.market, q.selection)]
        opp = _quote(opp_market, opp_sel, q.bookmaker)
        sel_name = {"a": m.player_a, "b": m.player_b,
                    "yes": "3 sets: SI", "no": "3 sets: NO"}[q.selection]
        me = evaluate_selection(
            market=q.market, selection=q.selection, selection_name=sel_name,
            p_model=p_sel, odds=q.odds, opposite_odds=opp.odds if opp else None,
            bookmaker=q.bookmaker, tour_shrink_w=w_shrink,
            sigma=sigma_match if q.market == "match_winner" else sigma_deriv,
            is_experimental=q.market in EXPERIMENTAL_MARKETS,
            hard_flags=flags, cfg_sel=cfg_sel)
        if q.market == "three_sets":
            # estudio 2026-08: pendiente de calibracion >1.1 no corregible de
            # forma robusta -> aviso permanente (ver reports/three_sets_study.json)
            me.reasons.append("aviso_calibracion_three_sets")
        evals.append(me)

    # sin cuota de moneyline: mostrar la vista del modelo (favorito probable)
    if not any(e.market == "match_winner" for e in evals):
        for sel_user in ("a", "b"):
            p_sel = p_of("match_winner", sel_user)
            evals.append(evaluate_selection(
                market="match_winner", selection=sel_user,
                selection_name=m.player_a if sel_user == "a" else m.player_b,
                p_model=p_sel, odds=None, opposite_odds=None, bookmaker="",
                tour_shrink_w=w_shrink, sigma=sigma_match, is_experimental=False,
                hard_flags=[], cfg_sel=cfg_sel))

    primary = pick_recommended_primary(evals)
    out_rows = []
    for e in evals:
        if rank_notes:
            e.reasons.extend(rank_notes)
        if soft_flags:
            e.reasons.extend(soft_flags)
        d = base | e.to_dict()
        d["recommended_primary"] = (primary is e)
        d["set_distribution"] = ({k: round(mk[k], 4) for k in ("p20", "p21", "p12", "p02")}
                                 if derived_ok and "p20" in mk else None)
        out_rows.append(d)
    return out_rows
