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

from betbot.canonical.names import canonical_key
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
    key = f"{tour}|{pid}"
    ld = state["last_date"].get(key)
    last = date.fromisoformat(ld) if ld else None
    rest = min(float((d - last).days), _MAX_REST_DAYS) if last else _MAX_REST_DAYS
    rec = [date.fromisoformat(x) for x in state["recent"].get(key, [])]
    m14 = sum(1 for x in rec if 0 <= (d - x).days <= 14)
    m12 = sum(1 for x in rec if 0 <= (d - x).days <= 365)
    lr = state["last_retired"].get(key)
    lr_d = date.fromisoformat(lr) if lr else None
    return {
        "last_date": last, "rest": rest, "m14": m14, "m12": m12,
        "layoff": 1.0 if (last and (d - last).days >= cfg_sel["layoff_days_ood"]) else 0.0,
        "retired_recent": 1.0 if (lr_d and (d - lr_d).days <= 30) else 0.0,
        "stale": last is None or (d - last).days >= cfg_sel["stale_days_unknown"],
    }


def _build_feature_row(m: DayMatch, a_c: str, b_c: str, elo_state, act_a: dict, act_b: dict,
                       bio: dict, market_logit: float) -> pd.DataFrame:
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
        "log_rank_ratio": 0.0,     # ranking actual no disponible en v1 (limitacion documentada)
        "log_pts_ratio": 0.0,
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


def run_screener(cfg: dict, matches_path: Path, odds_path: Path | None,
                 out_path: Path | None) -> pd.DataFrame:
    art = resolve_path(cfg, "artifacts_dir")
    canon = resolve_path(cfg, "canonical_dir")
    ledger_dir = resolve_path(cfg, "ledger_dir")
    bundle = joblib.load(art / "model_bundle.joblib")
    players = pd.read_parquet(canon / "players.parquet")
    registry = set(players["player_id"])
    bio = {p.player_id: {"dob": p.dob, "hand": p.hand} for p in players.itertuples(index=False)}
    cfg_sel = cfg["selection"]
    now = datetime.now(timezone.utc)

    # ---------- partidos ----------
    mdf = _read_csv(matches_path)
    day_matches: list[DayMatch] = []
    for i, r in mdf.iterrows():
        try:
            day_matches.append(DayMatch(
                date=r["date"], tour=r["tour"].strip().upper(), tournament=r["tournament"],
                surface=r.get("surface", "Hard") or "Hard",
                indoor=str(r.get("indoor", "false")).strip().lower() in ("true", "1", "si", "sí", "yes"),
                round=r.get("round", ""), best_of=int(r.get("best_of", "3") or 3),
                player_a=r["player_a"], player_b=r["player_b"]))
        except Exception as exc:  # noqa: BLE001
            click.echo(f"AVISO fila {i + 1} de partidos invalida: {exc}", err=True)

    # ---------- cuotas ----------
    quotes: list[OddsQuote] = []
    if odds_path is not None:
        odf = _read_csv(odds_path)
        for i, r in odf.iterrows():
            try:
                ts = r.get("timestamp", "").strip()
                quotes.append(OddsQuote(
                    player_a=r["player_a"], player_b=r["player_b"], market=r["market"].strip(),
                    selection=r["selection"], odds=float(r["odds"]),
                    bookmaker=r.get("bookmaker", "manual") or "manual",
                    timestamp=ts if ts else now.isoformat()))
            except Exception as exc:  # noqa: BLE001
                click.echo(f"AVISO fila {i + 1} de cuotas invalida: {exc}", err=True)

    rows: list[dict] = []
    for m in day_matches:
        rows.extend(_screen_match(m, quotes, bundle, registry, bio, cfg_sel, now))
    out = pd.DataFrame(rows)

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

    ledger_file = ledger_dir / "screen_runs.jsonl"
    with open(ledger_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "run_at": now.isoformat(), "n_matches": len(day_matches), "n_quotes": len(quotes),
            "model_meta": {k: bundle["meta"][k] for k in ("trained_at", "git_sha", "data_hash")},
            "thresholds": {k: cfg_sel[k] for k in ("ev_min", "edge_min", "ev_strong", "edge_strong")},
            "rows": rows}, ensure_ascii=False, default=str) + "\n")
    click.echo(f"Ledger actualizado: {ledger_file}")
    return out


def _screen_match(m: DayMatch, quotes: list[OddsQuote], bundle: dict, registry: set,
                  bio: dict, cfg_sel: dict, now: datetime) -> list[dict]:
    tour = m.tour.value
    tb = bundle["tours"][tour]
    elo_state = bundle["elo_states"][tour]
    act_state = bundle["activity_state"]
    ka, kb = canonical_key(m.player_a), canonical_key(m.player_b)
    match_label = f"{m.player_a} vs {m.player_b}"
    base = {"match": match_label, "tour": tour, "date": str(m.date),
            "tournament": m.tournament, "recommended_primary": False}

    # ---------- resolución de identidades ----------
    hard_flags: list[str] = []
    for name, key in ((m.player_a, ka), (m.player_b, kb)):
        if key not in registry:
            sugg = difflib.get_close_matches(key, registry, n=3, cutoff=0.75)
            hard_flags.append(f"jugador_desconocido:{name}" + (f" (¿{'|'.join(sugg)}?)" if sugg else ""))
    if not hard_flags:
        a_c, b_c = (ka, kb) if ka < kb else (kb, ka)
        flip = a_c != ka          # el jugador A del usuario es el B canónico
        act_a = _activity(act_state, tour, a_c, m.date, cfg_sel)
        act_b = _activity(act_state, tour, b_c, m.date, cfg_sel)
        for pid, act in ((a_c, act_a), (b_c, act_b)):
            if act["stale"]:
                hard_flags.append(f"ood_sin_actividad_reciente:{pid}")
            elif act["m12"] < cfg_sel["min_matches_12m"]:
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

    # ---------- predicción ----------
    feat = _build_feature_row(m, a_c, b_c, elo_state, act_a, act_b, bio, market_logit)
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
            return float(dv.combine(np.array([direct]), np.array([pb]), dcfg[mkt]["w_direct"])[0])
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
        if age_h > cfg_sel["odds_max_age_hours"]:
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
        d = base | e.to_dict()
        d["recommended_primary"] = (primary is e)
        d["set_distribution"] = ({k: round(mk[k], 4) for k in ("p20", "p21", "p12", "p02")}
                                 if derived_ok and "p20" in mk else None)
        out_rows.append(d)
    return out_rows
