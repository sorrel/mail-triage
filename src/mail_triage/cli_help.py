"""A grouped, coloured help screen.

Click lists commands alphabetically in one block, which says nothing about
how they relate: ``accounts`` and ``undo`` sit side by side though one is
where you start and the other is what you reach for when something went
wrong. Grouping them by the job they do makes the tool explain itself.

Colours are chosen for a dark terminal. Click drops them automatically when
the output is piped, so redirecting the help to a file stays plain.
"""

from __future__ import annotations

import click

from mail_triage.envelope import SnapshotError

from mail_triage.review import display_width

# Commands in the order they are usually wanted, grouped by the job they do.
# A command missing from here is not hidden — see ``_grouped``.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Look at what you have", ("accounts", "size")),
    ("Teach it", ("learn", "rules", "explain")),
    ("File your mail", ("triage",)),
    ("Get less of it", ("unsubscribe",)),
    ("Afterwards", ("runs", "undo")),
)

OTHER = "Other"


def _grouped(names: list[str]) -> list[tuple[str, list[str]]]:
    """Split command names into sections, keeping the ones nobody listed.

    A command absent from ``SECTIONS`` lands in "Other" rather than vanishing.
    A help screen that silently omits a command is worse than an ugly one, and
    this is exactly the sort of thing that rots when a command is added.
    """
    remaining = set(names)
    sections = []
    for title, wanted in SECTIONS:
        present = [name for name in wanted if name in remaining]
        if present:
            sections.append((title, present))
            remaining.difference_update(present)
    if remaining:
        sections.append((OTHER, sorted(remaining)))
    return sections


class ColouredGroup(click.Group):
    """A Click group whose help is grouped by task and coloured."""

    def invoke(self, ctx: click.Context):
        """Report an unco-operative snapshot as an error, not a traceback.

        Handled here rather than command by command: every command that reads
        mail starts by copying Mail's database, and each one wants the same
        answer — say what happened, and stop.
        """
        try:
            return super().invoke(ctx)
        except SnapshotError as error:
            raise click.ClickException(str(error)) from error

    def format_options(self, ctx: click.Context, formatter) -> None:
        records = [
            record
            for record in (param.get_help_record(ctx) for param in self.get_params(ctx))
            if record is not None
        ]
        if records:
            with formatter.section(click.style("Options", fg="cyan", bold=True)):
                formatter.write_dl(records)
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx: click.Context, formatter) -> None:
        commands = [
            (name, command)
            for name, command in (
                (name, self.get_command(ctx, name)) for name in self.list_commands(ctx)
            )
            if command is not None and not command.hidden
        ]
        if not commands:
            return
        by_name = dict(commands)
        # One width across every section, so the descriptions line up down the
        # whole screen rather than per group. Measured on the plain name:
        # styling is applied after padding, never before.
        width = max(display_width(name) for name, _ in commands) + 2
        for title, names in _grouped([name for name, _ in commands]):
            formatter.write_paragraph()
            formatter.write(f"  {click.style(title, fg='cyan', bold=True)}\n")
            for name in names:
                summary = by_name[name].get_short_help_str(limit=80 - width)
                padded = name + " " * (width - display_width(name))
                formatter.write(
                    f"    {click.style(padded, fg='yellow', bold=True)}{summary}\n"
                )
