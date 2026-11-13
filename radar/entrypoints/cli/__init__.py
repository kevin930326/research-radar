"""`radar` 指令：專案唯一的 CLI 入口（NFR-11）。typer 只能出現在這個套件。"""

from importlib.metadata import version as package_version

import typer

app = typer.Typer(help="Research Radar 的指令列工具。", no_args_is_help=True)


@app.callback()
def _root() -> None:
    """Research Radar 的指令列工具。"""


@app.command()
def version() -> None:
    """印出 radar 的版本。"""
    typer.echo(package_version("radar"))


def main() -> None:
    app()
