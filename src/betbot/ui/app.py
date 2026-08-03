"""Interfaz Streamlit local de una página. Lanzar con: betbot ui

No ejecuta apuestas ni scrapea: los partidos y cuotas se introducen a mano.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
import streamlit as st

from betbot import paper
from betbot.config import load_config, resolve_path
from betbot.monitor import days_summary, prospective_calibration
from betbot.screener.run import parse_matches_df, parse_odds_df, screen_day
from betbot.ui import logic

st.set_page_config(page_title="betbot — screener de tenis", layout="wide")
cfg = load_config()
ART = resolve_path(cfg, "artifacts_dir")
REPORTS = resolve_path(cfg, "reports_dir")


@st.cache_resource
def _meta() -> dict:
    p = ART / "model_bundle.joblib"
    if not p.exists():
        return {}
    return joblib.load(p)["meta"]


meta = _meta()
if not meta:
    st.error("No hay modelo entrenado. Ejecuta: betbot download-data && betbot prepare-data && betbot train")
    st.stop()

# ---------------- sidebar ----------------
with st.sidebar:
    st.title("🎾 betbot")
    st.caption("Screener local · paper trading · SIN ejecución de apuestas")
    st.markdown(f"**Modelo:** `{meta.get('git_sha')}` · datos `{meta.get('data_hash')}`\n\n"
                f"Entrenado: {str(meta.get('trained_at'))[:10]} · "
                f"Estado hasta: {meta.get('state_data_max_date', 'entrenamiento')}")
    st.markdown(f"Umbrales: EV≥{cfg['selection']['ev_min']:.0%}, "
                f"edge≥{cfg['selection']['edge_min']:.0%} "
                f"(fuerte: {cfg['selection']['ev_strong']:.0%}/{cfg['selection']['edge_strong']:.0%})")
    if st.button("🔄 Actualizar datos (incremental)"):
        from betbot.ingest.update import run_update
        with st.spinner("Descargando fuentes vivas y refrescando estado..."):
            try:
                log = run_update(cfg)
                st.success(f"OK · nuevos partidos: {log.get('new_matches', '?')} · "
                           f"frescura: {log['freshness_by_tour']}")
                for wmsg in log.get("safeguards", []):
                    st.warning(wmsg)
                _meta.clear()
            except RuntimeError as exc:
                st.error(str(exc))
    if st.button("⚖️ Liquidar picks contra resultados"):
        res = paper.settle_from_canonical(cfg)
        st.success(f"Liquidados: {res['settled']} · abiertos: {res['still_open']} (regla {res['rule']})")

tab_day, tab_paper, tab_mon, tab_sens = st.tabs(
    ["📅 Día", "📌 Paper trading", "📈 Monitor", "⚙️ Sensibilidad"])

# ---------------- TAB DÍA ----------------
with tab_day:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Partidos del día")
        up_m = st.file_uploader("Importar day_matches.csv", type="csv", key="up_m")
        if up_m is not None and st.session_state.get("_loaded_m") != up_m.name:
            st.session_state["matches_df"] = pd.read_csv(up_m, comment="#", dtype=str).fillna("")
            st.session_state["_loaded_m"] = up_m.name
        mdf_default = st.session_state.get("matches_df", pd.DataFrame(
            [{"date": str(date.today()), "tour": "ATP", "tournament": "", "surface": "Hard",
              "indoor": "false", "round": "", "best_of": "3", "player_a": "", "player_b": ""}]))
        mdf = st.data_editor(mdf_default, num_rows="dynamic", key="ed_matches",
                             use_container_width=True)
    with c2:
        st.subheader("Cuotas (por mercado y operador)")
        up_o = st.file_uploader("Importar day_odds.csv", type="csv", key="up_o")
        if up_o is not None and st.session_state.get("_loaded_o") != up_o.name:
            st.session_state["odds_df"] = pd.read_csv(up_o, comment="#", dtype=str).fillna("")
            st.session_state["_loaded_o"] = up_o.name
        odf_default = st.session_state.get("odds_df", pd.DataFrame(
            [{"player_a": "", "player_b": "", "market": "match_winner", "selection": "a",
              "odds": "", "bookmaker": "bet365", "timestamp": ""}]))
        odf = st.data_editor(odf_default, num_rows="dynamic", key="ed_odds",
                             use_container_width=True,
                             column_config={"market": st.column_config.SelectboxColumn(
                                 options=["match_winner", "set1_winner", "wins_set",
                                          "straight_sets", "three_sets"]),
                                 "selection": st.column_config.SelectboxColumn(
                                     options=["a", "b", "yes", "no"])})

    if st.button("▶️ Analizar", type="primary"):
        now = datetime.now(timezone.utc)
        matches, err_m = parse_matches_df(mdf)
        quotes, err_o = parse_odds_df(odf[odf["odds"].astype(str).str.strip() != ""], now)
        for e in err_m + err_o:
            st.warning(e)
        if matches:
            with st.spinner("Calculando probabilidades y value..."):
                res, warns = screen_day(cfg, matches, quotes)
            st.session_state["results"] = res
            for wmsg in warns:
                st.info(wmsg)
        else:
            st.error("No hay partidos válidos.")

    res = st.session_state.get("results")
    if res is not None and len(res):
        st.divider()
        st.subheader("Oportunidades")
        f1, f2, f3, f4, f5 = st.columns([1, 1, 2, 2, 2])
        tours = f1.multiselect("Circuito", sorted(res["tour"].unique()))
        surfaces = f2.multiselect("Superficie", sorted(res["surface"].dropna().unique()))
        markets = f3.multiselect("Mercado", sorted(res["market"].unique()))
        states = f4.multiselect("Estado", sorted(res["state"].unique()))
        omin = float(pd.to_numeric(res["odds"], errors="coerce").min() or 1.01)
        omax = float(pd.to_numeric(res["odds"], errors="coerce").max() or 10.0)
        odds_rng = f5.slider("Rango de cuota", 1.01, max(omax, 1.02), (max(omin, 1.01), max(omax, 1.02)))
        view = logic.sort_results(logic.filter_results(
            res, tours or None, surfaces or None, markets or None, states or None, odds_rng))
        st.dataframe(view[logic.RESULT_COLS], use_container_width=True, height=380)
        st.download_button("⬇️ Exportar CSV", view.to_csv(index=False).encode(),
                           file_name=f"oportunidades_{date.today()}.csv")

        cA, cB = st.columns(2)
        with cA:
            st.subheader("Detalle por partido")
            sel_match = st.selectbox("Partido", sorted(res["match"].unique()))
            det = logic.match_detail(res, sel_match)
            if det:
                if len(det["primary"]):
                    p = det["primary"].iloc[0]
                    st.success(f"⭐ recommended_primary: {logic.row_label(p)}")
                if det.get("set_distribution"):
                    st.markdown(f"Distribución de sets (A alfabético): `{det['set_distribution']}`")
                st.dataframe(det["rows"][["market", "selection_name", "bookmaker", "odds",
                                          "p_model", "fair_odds", "o_min", "ev", "ev_cons",
                                          "state", "reasons"]],
                             use_container_width=True)
        with cB:
            st.subheader("Seguir virtualmente (paper)")
            fol = logic.followable(res)
            if len(fol):
                labels = {logic.row_label(r): i for i, r in fol.iterrows()}
                pick_label = st.selectbox("Candidata", list(labels))
                if st.button("📌 Seguir esta candidata"):
                    row = fol.loc[labels[pick_label]].to_dict()
                    pid = paper.add_pick(cfg, row)
                    st.success(f"Pick registrado: {pid}")
            else:
                st.info("No hay candidatas seguibles (estados fuerte/normal/experimental/"
                        "vigilar con cuota). Cero picks es un resultado normal.")

# ---------------- TAB PAPER ----------------
with tab_paper:
    st.subheader("Picks prospectivos (paper trading)")
    st.caption("PROSPECTIVO: separado del backtest histórico modo A (non_executable).")
    picks = paper.load_picks(cfg)
    if len(picks) == 0:
        st.info("Aún no hay picks. Márcalos desde la pestaña Día.")
    else:
        st.dataframe(picks[["pick_id", "created_at", "match_date", "tour", "player_a", "player_b",
                            "market", "selection", "odds", "bookmaker", "state", "status",
                            "result", "pnl", "close_odds", "clv_odds_pct", "clv_prob_pp"]],
                     use_container_width=True, height=300)
        open_picks = picks[picks["status"] == "open"]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Cuota de cierre / posterior (CLV)**")
            if len(open_picks):
                pid = st.selectbox("Pick", open_picks["pick_id"].tolist(), key="clv_pick")
                co = st.number_input("Cuota de cierre (mismo lado)", 1.01, 100.0, step=0.01, key="co")
                cop = st.number_input("Cuota de cierre del complemento (opcional, 0 = no)",
                                      0.0, 100.0, step=0.01, key="cop")
                if st.button("Guardar cierre"):
                    rec = paper.set_close(cfg, pid, co, cop if cop > 1.0 else None)
                    st.success(f"CLV cuota {rec['clv_odds_pct']}% · CLV prob {rec.get('clv_prob_pp')}")
            else:
                st.caption("Sin picks abiertos.")
        with c2:
            st.markdown("**Liquidación manual** (si el resultado aún no está en el canónico)")
            if len(open_picks):
                pid2 = st.selectbox("Pick", open_picks["pick_id"].tolist(), key="settle_pick")
                rsel = st.radio("Resultado", ["win", "loss", "void"], horizontal=True)
                if st.button("Liquidar manualmente"):
                    rec = paper.settle_manual(cfg, pid2, rsel)
                    st.success(f"{pid2}: {rec['result']} pnl={rec['pnl']}")
    st.divider()
    m = paper.metrics(cfg)
    st.subheader("Métricas prospectivas")
    if m.get("warning"):
        st.warning(m["warning"])
    cols = st.columns(6)
    for c, (k, label) in zip(cols, [("n_picks", "Picks"), ("n_settled", "Liquidados"),
                                    ("pnl_units", "PnL (u)"), ("yield", "Yield"),
                                    ("hit_rate", "Acierto"), ("max_drawdown_units", "Max DD (u)")]):
        c.metric(label, m.get(k, "—"))
    if m.get("clv_odds_pct_mean") is not None:
        st.metric(f"CLV medio en cuota (n={m.get('n_with_close')})", f"{m['clv_odds_pct_mean']}%")
    for dim in ("by_tour", "by_market", "by_state", "by_odds"):
        if m.get(dim):
            st.markdown(f"**{dim}**: {m[dim]}")

# ---------------- TAB MONITOR ----------------
with tab_mon:
    st.subheader("Monitorización prospectiva")
    ds = days_summary(cfg)
    if ds.get("warning"):
        st.warning(ds["warning"])
    if ds.get("n_runs", 0) > 0:
        c = st.columns(4)
        c[0].metric("Días de uso", ds["n_days"])
        c[1].metric("Candidatas/día (media)", ds["mean_candidates_per_day"])
        c[2].metric("% días con 0 picks", f"{ds['pct_zero_pick_days']}%")
        c[3].metric("Ejecuciones", ds["n_runs"])
        st.markdown("**Distribución de estados (todas las ejecuciones)**")
        st.bar_chart(pd.Series(ds["states_total"]))
        if ds.get("discard_reason_codes"):
            st.markdown("**Reason codes de descartes**")
            st.dataframe(pd.Series(ds["discard_reason_codes"], name="n"), use_container_width=True)
        st.markdown("**Candidatas por día**")
        st.line_chart(pd.Series(ds["candidates_per_day"]))
    st.divider()
    st.subheader("Calibración prospectiva (moneyline emitida vs resultado real)")
    pc = prospective_calibration(cfg)
    if pc.get("warning"):
        st.warning(pc["warning"])
    if pc.get("n", 0) > 0:
        c = st.columns(4)
        c[0].metric("n", pc["n"])
        c[1].metric("Log loss", pc["log_loss"])
        c[2].metric("Acierto", pc["acc"])
        c[3].metric("ECE", pc["ece"])
        if "slope" in pc:
            st.markdown(f"Pendiente {pc['slope']} · intercepto {pc['intercept']}")
        if pc.get("reliability_bins"):
            rb = pd.DataFrame(pc["reliability_bins"])
            st.dataframe(rb, use_container_width=True)

# ---------------- TAB SENSIBILIDAD ----------------
with tab_sens:
    st.subheader("Sensibilidad de umbrales (solo validación; defaults intactos)")
    p = REPORTS / "threshold_sensitivity.json"
    if p.exists():
        r = json.loads(p.read_text())
        st.caption(f"Periodo {r['period']} · non_executable={r['non_executable']} · "
                   f"default actual: {r['default']} · {r['note']}")
        st.dataframe(pd.DataFrame(r["grid"]), use_container_width=True)
    else:
        st.info("Genera el informe con: betbot thresholds-report")
    st.divider()
    st.subheader("Estudio three_sets")
    p2 = REPORTS / "three_sets_study.json"
    if p2.exists():
        r2 = json.loads(p2.read_text())
        st.warning(r2["decision"])
        for t, v in r2["tours"].items():
            st.markdown(f"**{t}** · actual: {v['current']} · candidata: {v['conditional_platt']}")
    else:
        st.info("Genera el estudio con: betbot study-three-sets")
