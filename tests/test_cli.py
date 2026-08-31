from typer.testing import CliRunner

from metered_billing import __version__
from metered_billing.cli import app

runner = CliRunner()


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "metered billing" in result.output.lower()


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__

