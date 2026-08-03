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

st.set_page_config(page_title="BetBot — screener de tenis", page_icon="🎾", layout="wide")
cfg = load_config()
ART = resolve_path(cfg, "artifacts_dir")
REPORTS = resolve_path(cfg, "reports_dir")
EXPORTS = resolve_path(cfg, "exports_dir") if "exports_dir" in cfg["paths"] else ART / "exports"
EXPORTS.mkdir(parents=True, exist_ok=True)
LOGS_DIR = ART / "logs"


def _open_folder(path: Path) -> str | None:
    """Abre una carpeta en el explorador local. Devuelve error o None."""
    import subprocess
    import sys as _sys
    try:
        if _sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(path)])
        elif _sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)


@st.cache_resource
def _meta() -> dict:
    p = ART / "model_bundle.joblib"
    if not p.exists():
        return {}
    return joblib.load(p)["meta"]


# ---------------- PRIMER ARRANQUE (asistente sin terminal) ----------------
from betbot.firstrun import check_environment, needs_setup  # noqa: E402

checks = check_environment(cfg)
if needs_setup(checks):
    st.title("🎾 BetBot — primer arranque")
    st.caption("Servidor local en 127.0.0.1 (solo este ordenador). No se ejecutan apuestas.")
    ok = "✅"
    bad = "❌"
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"{ok if checks['python_ok'] else bad} Python {checks['python_version']} "
                    f"(se requiere ≥ 3.11)")
        st.markdown(f"{ok if checks['deps_ok'] else bad} Dependencias "
                    + ("completas" if checks["deps_ok"] else f"faltan: {checks['missing_deps']}"))
        st.markdown(f"{ok if checks['data_writable'] else bad} Permisos de escritura en data/")
        st.markdown(f"{ok if checks['artifacts_writable'] else bad} Permisos de escritura en artifacts/")
    with c2:
        st.markdown(f"{ok if checks['raw_data_present'] else bad} Datos descargados (data/raw)")
        st.markdown(f"{ok if checks['canonical_present'] else bad} Dataset preparado (canónico)")
        st.markdown(f"{ok if checks['models_present'] else bad} Modelos y calibradores entrenados")
    if not checks["python_ok"] or checks["missing_deps"]:
        st.error("Ejecuta el instalador (INSTALAR_BETBOT) para reparar el entorno y vuelve a abrir BetBot.")
    if not (checks["data_writable"] and checks["artifacts_writable"]):
        st.error("BetBot no puede escribir en sus carpetas. Muévelo a una carpeta con permisos "
                 "(p. ej. Documentos) y vuelve a abrirlo.")
    st.divider()
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("⬇️ 1. Descargar datos", disabled=checks["raw_data_present"]):
        from betbot.ingest.download import download_all
        with st.status("Descargando fuentes reales (~40 MB)...", expanded=True) as s:
            res = download_all(resolve_path(cfg, "raw_dir"))
            st.write(f"OK: {len(res['ok'])} · ya presentes: {len(res['skipped'])}")
            for f in res["failed"]:
                st.error(f"Fallo: {f}")
            s.update(label="Descarga terminada", state="complete")
        st.rerun()
    if b2.button("🧱 2. Preparar datos", disabled=not checks["raw_data_present"]):
        from betbot.canonical.build import build_canonical
        with st.status("Construyendo dataset canónico (~2 min)...", expanded=True) as s:
            summary = build_canonical(resolve_path(cfg, "raw_dir"), resolve_path(cfg, "canonical_dir"))
            st.write(f"Partidos: {summary['n_matches']} · hasta {summary['date_max']}")
            s.update(label="Dataset preparado", state="complete")
        st.rerun()
    if b3.button("🧠 3. Entrenar modelos", disabled=not checks["canonical_present"]):
        from betbot.models.train import run_training
        with st.status("Entrenando baselines y calibración (~2-3 min)... "
                       "El test sellado 2025 NO se toca.", expanded=True) as s:
            report = run_training(cfg)
            for tour in report.get("tours", {}):
                m5 = report["tours"][tour]["match_models_oof"].get("M5_market", {})
                st.write(f"{tour}: log loss OOF M5 = {m5.get('log_loss')}")
            s.update(label="Modelos entrenados", state="complete")
        _meta.clear()
        st.rerun()
    if b4.button("🔁 Reintentar comprobación"):
        st.rerun()
    st.stop()

meta = _meta()
if not meta:
    st.error("Faltan artefactos del modelo. Usa los botones de primer arranque (recarga la página).")
    st.stop()


# ---------------- cabecera de estado ----------------
@st.cache_data
def _freshness(state_max: str) -> dict:
    from betbot.canonical.store import load_matches
    m = load_matches(resolve_path(cfg, "canonical_dir"))
    return {t: str(m.loc[m["tour"] == t, "date"].max()) for t in ("ATP", "WTA")}


fresh = _freshness(str(meta.get("state_data_max_date", "")))
h1, h2, h3, h4 = st.columns([2, 2, 2, 3])
h1.metric("Datos ATP hasta", fresh.get("ATP", "—"))
h2.metric("Datos WTA hasta", fresh.get("WTA", "—"))
h3.metric("Modelo", str(meta.get("git_sha", "—")))
h4.markdown("🔒 **Servidor solo local (127.0.0.1)** · sin ejecución de apuestas\n\n"
            f"Entrenado {str(meta.get('trained_at'))[:10]} · datos `{meta.get('data_hash')}`")
_today = date.today()
for _t, _d in fresh.items():
    try:
        _age = (_today - pd.Timestamp(_d).date()).days
        if _age > 45:
            st.warning(f"⚠️ Los datos de {_t} tienen {_age} días (hasta {_d}). Usa "
                       "«Actualizar datos» y/o «Importar resultados recientes» en la barra lateral.")
    except (ValueError, TypeError):
        pass

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
    conf_upd = st.checkbox("Confirmo actualizar (2–3 min)", key="conf_upd")
    if st.button("🔄 Actualizar datos (incremental)", disabled=not conf_upd):
        from betbot.ingest.update import run_update
        prog = st.progress(10, text="Descargando fuentes vivas...")
        try:
            log = run_update(cfg)
            prog.progress(90, text="Refrescando Elo y estado...")
            st.success(f"OK · nuevos partidos: {log.get('new_matches', '?')} · "
                       f"frescura: {log['freshness_by_tour']}")
            for wmsg in log.get("safeguards", []):
                st.warning(wmsg)
            _meta.clear()
            _freshness.clear()
            prog.progress(100, text="Actualización completa")
        except RuntimeError as exc:
            st.error(str(exc))
    if st.button("⚖️ Liquidar picks contra resultados"):
        res = paper.settle_from_canonical(cfg)
        st.success(f"Liquidados: {res['settled']} · abiertos: {res['still_open']} (regla {res['rule']})")
    st.divider()
    st.markdown("**📥 Importar resultados recientes** (plantilla `recent_results.csv`)")
    up_r = st.file_uploader("CSV de resultados", type="csv", key="up_results",
                            label_visibility="collapsed")
    allow_new = st.checkbox("Aceptar jugadores nuevos (si no, cuarentena)", value=False)
    if up_r is not None and st.button("Importar resultados"):
        from betbot.ingest.manual_results import import_results
        from betbot.state import refresh_state
        tmp = Path(resolve_path(cfg, "ledger_dir")) / f"_upload_{up_r.name}"
        tmp.write_bytes(up_r.getvalue())
        rep = import_results(cfg, tmp, allow_new=allow_new)
        st.success(f"Aceptadas {rep['n_accepted']} · rechazadas {rep['n_rejected']} · "
                   f"cuarentena {rep['n_quarantined']}")
        for q in rep["quarantined"][:5]:
            st.warning(f"Cuarentena fila {q['row']}: {q['unknown']}")
        for e in rep["rejected"][:5]:
            st.warning(f"Rechazada fila {e['row']}: {e['error']}")
        if rep["n_accepted"] > 0:
            with st.spinner("Refrescando Elo y estado..."):
                refresh_state(cfg)
            st.success("Estado y Elo actualizados (modelos intactos).")
            _meta.clear()
            _freshness.clear()
    st.divider()
    st.markdown("**🧰 Utilidades**")
    from betbot.ingest.manual_results import RESULTS_TEMPLATE
    from betbot.screener.templates import MATCHES_TEMPLATE, ODDS_TEMPLATE
    st.download_button("📄 Plantilla de resultados", RESULTS_TEMPLATE,
                       file_name="recent_results.csv", use_container_width=True)
    st.download_button("📄 Plantilla de partidos", MATCHES_TEMPLATE,
                       file_name="day_matches.csv", use_container_width=True)
    st.download_button("📄 Plantilla de cuotas", ODDS_TEMPLATE,
                       file_name="day_odds.csv", use_container_width=True)
    if st.button("📂 Abrir carpeta de exportaciones", use_container_width=True):
        err = _open_folder(EXPORTS)
        st.error(f"No se pudo abrir: {err}") if err else st.toast("Carpeta abierta")
    if st.button("🧾 Abrir carpeta de logs", use_container_width=True):
        err = _open_folder(LOGS_DIR if LOGS_DIR.exists() else ART)
        st.error(f"No se pudo abrir: {err}") if err else st.toast("Carpeta abierta")
    with st.expander("Últimas líneas del log técnico"):
        lg = LOGS_DIR / "streamlit.log"
        st.code("\n".join(lg.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
                if lg.exists() else "sin log aún", language=None)
    st.divider()
    if st.button("🛑 Cerrar BetBot", type="secondary", use_container_width=True):
        from betbot.launcher import STOP_FILE
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop")
        st.warning("Cerrando BetBot de forma segura... puedes cerrar esta pestaña. "
                   "Para volver a abrirlo, usa ABRIR_BETBOT.")

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
        cexp1, cexp2 = st.columns(2)
        cexp1.download_button("⬇️ Descargar CSV", view.to_csv(index=False).encode(),
                              file_name=f"oportunidades_{date.today()}.csv")
        if cexp2.button("💾 Guardar en carpeta de exportaciones"):
            outp = EXPORTS / f"oportunidades_{date.today()}.csv"
            view.to_csv(outp, index=False)
            st.success(f"Guardado: {outp.name} (usa «Abrir carpeta de exportaciones»)")

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
    conf_thr = st.checkbox("Confirmo regenerar el informe (~1 min, solo validación)")
    if st.button("📊 Generar/actualizar informe de umbrales", disabled=not conf_thr):
        from betbot.analysis import threshold_sensitivity
        with st.status("Calculando parrilla de umbrales en validación...", expanded=False) as s:
            threshold_sensitivity(cfg)
            s.update(label="Informe actualizado", state="complete")
        st.rerun()
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
