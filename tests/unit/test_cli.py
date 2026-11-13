from importlib.metadata import version

from typer.testing import CliRunner

from radar.entrypoints.cli import app

runner = CliRunner()


def test_help_lists_version_subcommand() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "version" in result.output


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == version("radar")
