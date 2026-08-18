"""What happened, and putting it back."""

from __future__ import annotations

import click

from mail_triage.commands._shared import _make_header_guard, _run_errors, _select_sources
from mail_triage.config import load_config
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.journal import Journal, list_runs, undo_run
from mail_triage.mail_app import AppleScriptMail
from mail_triage.pipeline import classify_run
from mail_triage.report import DEFAULT_SINCE_DAYS, recent_runs, render
from mail_triage.commands._shared import PROGRESS_LINE_WIDTH

@click.command()
@click.argument("run_id", required=False)
@click.option(
    "--account",
    default="iCloud",
    help="Fallback account for journal entries written before runs recorded "
    "their own. Ignored for anything triaged since.",
)
def undo(run_id: str | None, account: str) -> None:
    """Reverse a triage run. Defaults to the most recent."""
    config = load_config()
    runs = list_runs(config)
    if not runs:
        raise click.ClickException("No runs to undo.")
    target = run_id or runs[0]
    if target not in runs:
        raise click.ClickException(
            f"No journal for run {target!r}. Known runs: {', '.join(runs[:5])}"
        )
    reversed_count, failed = undo_run(target, config, AppleScriptMail(), account)
    click.echo(f"Reversed {reversed_count} moves from run {target} ({failed} failed).")


@click.command()
def runs() -> None:
    """List triage runs that can be undone, newest first."""
    config = load_config()
    known = list_runs(config)
    if not known:
        click.echo("No runs recorded yet.")
        return
    journal = Journal(config)
    for run_id in known:
        entries = journal.load(run_id)
        moved = sum(1 for entry in entries if entry.status == "moved")
        undone = sum(1 for entry in entries if entry.status == "undone")
        click.echo(f"{run_id}  {moved} moved, {undone} undone")


@click.command()
@click.option(
    "--since",
    "since_days",
    default=DEFAULT_SINCE_DAYS,
    type=float,
    metavar="DAYS",
    help="How far back to read the run journals.",
)
@click.option(
    "--source",
    "source_names",
    multiple=True,
    help="Classify only these sources when listing held-back mail.",
)
def report(since_days: float, source_names: tuple[str, ...]) -> None:
    """What the unattended runs did, and what is still waiting on you.

    Security-relevant mail leads, in full: it is the one category whose cost
    is measured in hours, and a scheduled run's output may not be read for a
    day. Everything else is counted — a filing that went where it always
    goes is not news.

    Read-only. It classifies the current inbox to find what a guard is
    holding, and reads the journals for what moved. It moves nothing.
    """
    config = load_config()
    runs = recent_runs(config, since_days)

    # The journal records moves; a message a guard held back never became an
    # entry, because nothing was attempted on it. So the held half has to be
    # classified fresh — which is also what makes the report current rather
    # than a description of the last run's opinion.
    sources = _select_sources(config, source_names)
    mail = AppleScriptMail()
    guard, guard_state = _make_header_guard(mail)
    with _run_errors():
        run = classify_run(config, sources, guard=guard, db_path=DEFAULT_DB_PATH)
    if guard_state["fetches"]:
        click.echo("\r" + " " * PROGRESS_LINE_WIDTH + "\r", nl=False, err=True)

    held_security = run.held("security")
    held_other = sum(
        1 for item in run.proposals
        if item.veto is not None and item.veto_kind != "security"
    )
    click.echo(render(runs, held_security, held_other, since_days))
