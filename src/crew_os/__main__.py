"""Entry point for `python -m crew_os` - delegates to the CLI."""

from __future__ import annotations

from crew_os.cli.main import cli

if __name__ == "__main__":
    cli()
