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
def template(out_dir: str) -> None:
    """Escribe las plantillas de importación del día (partidos y cuotas)."""
    from betbot.screener.templates import write_templates
    paths = write_templates(Path(out_dir))
    for p in paths:
        click.echo(str(p))


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


if __name__ == "__main__":
    cli()
