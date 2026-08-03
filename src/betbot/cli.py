"""CLI del MVP. Comandos: download-data, prepare-data, train, evaluate, template, screen."""
from __future__ import annotations

import json
from pathlib import Path

import click

from betbot.config import load_config, resolve_path


@click.group()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None,
              help="YAML de configuración que extiende config/default.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """betbot — screener cuantitativo de tenis (MVP local; no ejecuta apuestas)."""
    ctx.obj = load_config(config_path)


@cli.command("download-data")
@click.option("--force", is_flag=True, help="Re-descargar aunque el fichero exista")
@click.pass_obj
def download_data(cfg: dict, force: bool) -> None:
    """Descarga las fuentes reales a data/raw con manifest + SHA256."""
    from betbot.ingest.download import download_all
    res = download_all(resolve_path(cfg, "raw_dir"), force=force)
    click.echo(f"OK={len(res['ok'])} SKIP={len(res['skipped'])} FAIL={len(res['failed'])}")
    for f in res["failed"]:
        click.echo(f"  FALLO: {f}", err=True)


@cli.command("prepare-data")
@click.pass_obj
def prepare_data(cfg: dict) -> None:
    """Construye el dataset canónico (matches.parquet, players.parquet)."""
    from betbot.canonical.build import build_canonical
    summary = build_canonical(resolve_path(cfg, "raw_dir"), resolve_path(cfg, "canonical_dir"))
    click.echo(json.dumps(summary, indent=2))


@cli.command("update-data")
@click.pass_obj
def update_data(cfg: dict) -> None:
    """Actualización incremental: fuentes vivas + canónico + estado (modelos intactos)."""
    from betbot.ingest.update import run_update
    log = run_update(cfg)
    click.echo(json.dumps(log, indent=2, ensure_ascii=False))


@cli.command("train")
@click.pass_obj
def train(cfg: dict) -> None:
    """Ratings + features + walk-forward + calibración + artefactos del screener."""
    from betbot.models.train import run_training
    report = run_training(cfg)
    click.echo(json.dumps(report, indent=2, ensure_ascii=False))


@cli.command("evaluate")
@click.option("--test", "use_test", is_flag=True,
              help="Evalúa el año de TEST SELLADO (un solo uso legítimo; queda registrado)")
@click.pass_obj
def evaluate(cfg: dict, use_test: bool) -> None:
    """Evaluación temporal (validación por defecto; --test para el año sellado)."""
    from betbot.backtest.run import run_evaluation
    report = run_evaluation(cfg, use_test=use_test)
    click.echo(json.dumps(report, indent=2, ensure_ascii=False))


@cli.command("template")
@click.option("--out", "out_dir", type=click.Path(), default="templates")
@click.pass_obj
def template(cfg: dict, out_dir: str) -> None:
    """Escribe las plantillas del día (partidos, cuotas) y de rankings manuales."""
    from betbot.ingest.rankings import write_rankings_templates
    from betbot.screener.templates import write_templates
    paths = write_templates(Path(out_dir))
    paths += write_rankings_templates(resolve_path(cfg, "manual_dir"))
    for p in paths:
        click.echo(str(p))


@cli.command("ui")
@click.pass_obj
def ui(cfg: dict) -> None:
    """Abre la interfaz Streamlit local (una página)."""
    import subprocess
    import sys
    app = Path(__file__).parent / "ui" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app),
                    "--server.headless", "true"], check=False)


@cli.command("screen")
@click.option("--matches", "matches_path", type=click.Path(exists=True), required=True)
@click.option("--odds", "odds_path", type=click.Path(exists=True), required=False, default=None)
@click.option("--out", "out_path", type=click.Path(), default=None, help="CSV de salida")
@click.pass_obj
def screen(cfg: dict, matches_path: str, odds_path: str | None, out_path: str | None) -> None:
    """Genera la tabla diaria de oportunidades desde la importación manual."""
    from betbot.screener.run import run_screener
    run_screener(cfg, Path(matches_path), Path(odds_path) if odds_path else None,
                 Path(out_path) if out_path else None)


@cli.group("paper")
def paper_group() -> None:
    """Paper trading prospectivo (picks virtuales, settlement, CLV, métricas)."""


@paper_group.command("list")
@click.option("--status", type=click.Choice(["open", "settled", "void"]), default=None)
@click.pass_obj
def paper_list(cfg: dict, status: str | None) -> None:
    from betbot.paper import load_picks
    df = load_picks(cfg)
    if status:
        df = df[df["status"] == status]
    cols = ["pick_id", "match_date", "tour", "player_a", "player_b", "market", "selection",
            "odds", "bookmaker", "state", "status", "result", "pnl", "clv_odds_pct"]
    click.echo(df[cols].to_string(index=False) if len(df) else "sin picks")


@paper_group.command("close")
@click.argument("pick_id")
@click.argument("close_odds", type=float)
@click.option("--opposite", type=float, default=None, help="cuota de cierre del complemento (para CLV en probabilidad)")
@click.pass_obj
def paper_close(cfg: dict, pick_id: str, close_odds: float, opposite: float | None) -> None:
    from betbot.paper import set_close
    rec = set_close(cfg, pick_id, close_odds, opposite)
    click.echo(f"CLV cuota: {rec['clv_odds_pct']}% | CLV prob: {rec.get('clv_prob_pp')}")


@paper_group.command("settle")
@click.option("--pick-id", default=None, help="liquidación manual de un pick concreto")
@click.option("--result", type=click.Choice(["win", "loss", "void"]), default=None)
@click.pass_obj
def paper_settle(cfg: dict, pick_id: str | None, result: str | None) -> None:
    """Sin argumentos: liquida contra el canónico. Con --pick-id y --result: manual."""
    from betbot.paper import settle_from_canonical, settle_manual
    if pick_id:
        if not result:
            raise click.UsageError("--result es obligatorio con --pick-id")
        rec = settle_manual(cfg, pick_id, result)
        click.echo(f"{pick_id}: {rec['result']} pnl={rec['pnl']}")
    else:
        click.echo(json.dumps(settle_from_canonical(cfg), indent=2))


@paper_group.command("metrics")
@click.pass_obj
def paper_metrics(cfg: dict) -> None:
    from betbot.paper import metrics
    click.echo(json.dumps(metrics(cfg), indent=2, ensure_ascii=False))


@cli.command("study-three-sets")
@click.pass_obj
def study_three_sets(cfg: dict) -> None:
    """Estudio de calibración de three_sets (dev+validación; test intacto)."""
    from betbot.analysis import three_sets_study
    click.echo(json.dumps(three_sets_study(cfg), indent=2, ensure_ascii=False))


@cli.command("thresholds-report")
@click.pass_obj
def thresholds_report(cfg: dict) -> None:
    """Sensibilidad de umbrales en validación (no cambia la configuración)."""
    from betbot.analysis import threshold_sensitivity
    click.echo(json.dumps(threshold_sensitivity(cfg), indent=2, ensure_ascii=False))


@cli.command("monitor")
@click.pass_obj
def monitor_cmd(cfg: dict) -> None:
    """Resumen de monitorización prospectiva en consola."""
    from betbot.monitor import days_summary, prospective_calibration
    click.echo(json.dumps({"dias": days_summary(cfg),
                           "calibracion_prospectiva": prospective_calibration(cfg)},
                          indent=2, ensure_ascii=False))


if __name__ == "__main__":
    cli()
