"""What the unattended runs did, read back out of the journals.

Unattended must not mean invisible. A scheduled run writes to a log file
nobody opens; the journal, on the other hand, is already a complete record of
every move that was attempted and how it went, written before the fact. This
module reads it back.

The ordering is the point. Security-relevant mail leads, in full and by
subject, because it is the one category whose cost is measured in hours and a
scheduled run's output may not be looked at for a day. Everything else is
counted rather than listed: a filing that went where it always goes is not
news, and burying the one line worth reading in three hundred that are not is
how a report stops being read at all.

The journal records moves, not holds — a message a guard held back never
became a journal entry, because nothing was attempted on it. So the held-back
half comes from a classification of the current inbox rather than from
history, and the two halves of the report answer different questions: what
happened, and what is still waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from mail_triage.journal import Journal, JournalEntry, list_runs

SECONDS_PER_DAY = 86_400

# Long enough to catch a fortnight's runs at the default cadence, short enough
# that the report is about the recent past rather than the whole archive.
DEFAULT_SINCE_DAYS = 7


@dataclass
class RunSummary:
    """One journal run, folded down to what a person would want to know."""

    run_id: str
    moved: int = 0
    failed: int = 0
    undone: int = 0
    by_folder: dict[str, int] = field(default_factory=dict)


def _run_started_at(run_id: str) -> float | None:
    """The timestamp encoded in a run id, or ``None`` if it is not one.

    ``new_run_id`` builds "%Y-%m-%dT%H-%M-%S" plus a random suffix. Parsing it
    back is what makes ``--since`` possible without opening every file, and a
    run id from some other scheme is simply always included rather than
    silently dropped — a report that hides runs it cannot date would be worse
    than one that shows too many.
    """
    stamp = run_id[:19]
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%dT%H-%M-%S"))
    except ValueError:
        return None


def summarise_run(entries: list[JournalEntry]) -> RunSummary:
    summary = RunSummary(run_id="")
    for entry in entries:
        if entry.status == "moved":
            summary.moved += 1
            summary.by_folder[entry.to_folder] = summary.by_folder.get(entry.to_folder, 0) + 1
        elif entry.status == "failed":
            summary.failed += 1
        elif entry.status == "undone":
            summary.undone += 1
    return summary


def recent_runs(config, since_days: float = DEFAULT_SINCE_DAYS) -> list[RunSummary]:
    """Every run in the window, newest first."""
    cutoff = time.time() - since_days * SECONDS_PER_DAY
    journal = Journal(config)
    summaries = []
    for run_id in list_runs(config):
        started = _run_started_at(run_id)
        if started is not None and started < cutoff:
            continue
        summary = summarise_run(journal.load(run_id))
        summary.run_id = run_id
        summaries.append(summary)
    return summaries


def render(
    runs: list[RunSummary],
    held_security: list,
    held_other: int,
    since_days: float,
) -> str:
    """The report, security first.

    ``held_security`` is a list of proposals; ``held_other`` a bare count of
    everything else a guard held. The asymmetry is deliberate and is the whole
    design: one of these is a list of things to read, the other is reassurance
    that the rest of the machinery ran.
    """
    lines: list[str] = []

    if held_security:
        lines.append(
            f"{len(held_security)} held back as security-relevant — read these first:"
        )
        for proposal in held_security:
            sender = proposal.message.sender
            lines.append(f"  {proposal.message.subject}")
            lines.append(f"      from {sender} — {proposal.veto}")
        lines.append("")
    else:
        lines.append("Nothing held back as security-relevant.")
        lines.append("")

    # With the security section, not after the runs: both are answers to
    # "what is still waiting on me", and the count was once written below the
    # early return for an empty window, where a week with no runs printed no
    # held-back count at all — the one case where mail piling up unfiled is
    # most likely and least visible.
    if held_other:
        lines.append(f"{held_other} more held back by the other guards.")
        lines.append("")

    window = f"the last {since_days:g} day{'s' if since_days != 1 else ''}"
    if not runs:
        lines.append(f"No runs in {window}.")
        return "\n".join(lines)

    moved = sum(run.moved for run in runs)
    failed = sum(run.failed for run in runs)
    undone = sum(run.undone for run in runs)
    lines.append(
        f"{len(runs)} run{'s' if len(runs) != 1 else ''} in {window}: "
        f"{moved} filed, {failed} failed, {undone} undone."
    )

    folders: dict[str, int] = {}
    for run in runs:
        for folder, count in run.by_folder.items():
            folders[folder] = folders.get(folder, 0) + count
    if folders:
        lines.append("")
        lines.append("Where it went:")
        for folder, count in sorted(folders.items(), key=lambda pair: -pair[1]):
            lines.append(f"  {count:>5}  {folder}")

    if failed:
        # Named rather than counted, because a failure is the other thing in
        # this report that wants acting on: a move that did not happen means
        # a message still sitting in the inbox with a journal entry saying so.
        lines.append("")
        lines.append("Runs with failures:")
        for run in runs:
            if run.failed:
                lines.append(f"  {run.run_id}  {run.failed} failed")

    return "\n".join(lines)
