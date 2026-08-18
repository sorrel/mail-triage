"""Command-line entry point: the group, and what is registered on it.

Nothing is implemented here. Each command lives with the others that share its
purpose, under ``commands/``, and this file exists to name them in one place —
so that "what can this tool do?" is answered by reading twenty lines rather
than by scrolling through thirteen hundred.

Commands are plain ``click.Command`` objects added here rather than decorated
against the group where they are defined. That is what keeps the imports
running one way: ``cli`` knows about the command modules, and no command
module knows about ``cli``.
"""

from __future__ import annotations

import click

from mail_triage.cli_help import ColouredGroup
from mail_triage.commands import inspect, journal, teach, triage, unsubscribe, web


@click.group(cls=ColouredGroup)
@click.version_option()
def cli() -> None:
    """Local-first triage for Apple Mail."""


for _command in (
    inspect.accounts,
    inspect.size,
    teach.learn,
    teach.rules,
    teach.explain,
    teach.security,
    triage.triage,
    unsubscribe.unsubscribe,
    journal.undo,
    journal.runs,
    journal.report,
    web.web,
):
    cli.add_command(_command)


if __name__ == "__main__":
    cli()
