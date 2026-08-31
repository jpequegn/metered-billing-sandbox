"""Command-line entry point."""

import typer

from metered_billing import __version__

app = typer.Typer(
    name="metered-billing",
    help="Simulate metered billing and agent spend controls locally.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Run local billing simulations and inspect their evidence."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
