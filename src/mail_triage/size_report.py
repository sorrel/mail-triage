"""Render the size grids.

Kept apart from ``sizes.py`` so that measurement can be tested without a
terminal and layout without a mail store.

Two rules govern everything here. Widths come from ``review.display_width``,
never ``len()`` — folder names contain emoji, and padding on ``len()``
silently misaligns every column to their right. And colour is applied *after*
padding is computed, because an ANSI escape occupies no columns but would be
counted as several.
"""

from __future__ import annotations

import re

import click

from mail_triage.accounts import NOT_IN_MAIL
from mail_triage.review import display_width
from mail_triage.sizes import AccountUsage, FolderNode

FOLDER_WIDTH = 44
NUMBER_WIDTH = 12
COUNT_WIDTH = 9
INDENT = "  "

# A row at or above this share of the account's total is highlighted. A share
# rather than a fixed byte count, so the colour means the same thing in a
# 20 MB account as in a 5 GB one.
HIGHLIGHT_SHARE = 0.20

_UNITS = (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024))
_SIZE_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(b|kb|mb|gb)?\s*$", re.IGNORECASE)
_SUFFIXES = {None: 1, "b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}

NO_FIGURE = "—"


def human_bytes(count: int | None, exact: bool = False) -> str:
    """Format a byte count for the grid.

    ``None`` is rendered as a dash rather than a zero: an account Mail knows
    about but does not store on this Mac has no disk figure at all, and nought
    would read as "empty".
    """
    if count is None:
        return NO_FIGURE
    if exact:
        return str(count)
    for unit, size in _UNITS:
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{count} B"


def parse_size(text: str) -> int:
    """Parse ``--min-size`` values such as ``2MB``, ``1.5 gb`` or ``4096``.

    A bare number is bytes. Raises ``ValueError`` on anything else, so a typo
    stops the run rather than quietly hiding half the report.
    """
    match = _SIZE_PATTERN.match(text)
    if match is None:
        raise ValueError(
            f"{text!r} is not a size. Use a number of bytes, or a value such as "
            "'2MB' or '1.5GB'."
        )
    number, suffix = match.groups()
    return int(float(number) * _SUFFIXES[suffix.lower() if suffix else None])


def _cell(text: str, width: int, right: bool = False) -> str:
    """Pad or clip ``text`` to ``width`` display columns."""
    if display_width(text) > width:
        kept: list[str] = []
        total = 0
        for char in text:
            step = display_width(char)
            if total + step > width - 1:
                break
            kept.append(char)
            total += step
        text = "".join(kept) + "…"
    padding = " " * max(width - display_width(text), 0)
    return text + padding if not right else padding + text


def _heading(label: str = "Folder") -> str:
    return (
        f"{_cell(label, FOLDER_WIDTH)} "
        f"{_cell('Messages', COUNT_WIDTH, right=True)} "
        f"{_cell('In Mail', NUMBER_WIDTH, right=True)} "
        f"{_cell('On disk', NUMBER_WIDTH, right=True)}"
    )


def _row(
    label: str,
    count: str,
    envelope: str,
    disk: str,
    highlight: bool = False,
) -> str:
    """Build one grid row, colouring only after every width is settled."""
    line = (
        f"{_cell(label, FOLDER_WIDTH)} "
        f"{_cell(count, COUNT_WIDTH, right=True)} "
        f"{_cell(envelope, NUMBER_WIDTH, right=True)} "
        f"{_cell(disk, NUMBER_WIDTH, right=True)}"
    )
    return click.style(line, fg="yellow") if highlight else line


def _weight(node: FolderNode) -> int:
    """The figure a node is judged by: disk where known, envelope otherwise."""
    total = node.total_disk_bytes
    return total if total is not None else node.total_envelope_bytes


def _split_small(
    children: tuple[FolderNode, ...], min_size: int
) -> tuple[list[FolderNode], list[FolderNode]]:
    """Separate children worth showing from those to be collapsed.

    Judged on the node's *total*, not its own bytes: a small folder holding a
    large one must survive, or the large one disappears with it.
    """
    if min_size <= 0:
        return list(children), []
    kept = [child for child in children if _weight(child) >= min_size]
    dropped = [child for child in children if _weight(child) < min_size]
    return kept, dropped


def _collapse_row(dropped: list[FolderNode], depth: int, exact: bool) -> str:
    """One line standing in for the folders hidden at this level.

    Not cosmetic. Without it the visible rows would silently fail to add up to
    the account total, and a total that does not reconcile invites doubt about
    every other figure shown.
    """
    messages = sum(node.total_messages for node in dropped)
    envelope = sum(node.total_envelope_bytes for node in dropped)
    disks = [node.total_disk_bytes for node in dropped]
    disk = None if all(value is None for value in disks) else sum(v or 0 for v in disks)
    noun = "folder" if len(dropped) == 1 else "folders"
    label = f"{INDENT * depth}{len(dropped)} smaller {noun}"
    return _row(
        label,
        f"{messages:,}" if messages else "",
        human_bytes(envelope, exact),
        human_bytes(disk, exact),
    )


def _tree_rows(
    node: FolderNode, min_size: int, exact: bool, threshold: int, depth: int
) -> list[str]:
    """Render ``node``'s children, deepest-first recursion, largest first."""
    rows: list[str] = []
    kept, dropped = _split_small(node.children, min_size)
    for child in kept:
        rows.append(
            _row(
                f"{INDENT * depth}{child.name}",
                f"{child.total_messages:,}" if child.total_messages else "",
                human_bytes(child.total_envelope_bytes, exact),
                human_bytes(child.total_disk_bytes, exact),
                highlight=threshold > 0 and _weight(child) >= threshold,
            )
        )
        rows.extend(_tree_rows(child, min_size, exact, threshold, depth + 1))
    if dropped:
        rows.append(_collapse_row(dropped, depth, exact))
    return rows


def render_account(usage: AccountUsage, min_size: int, exact: bool) -> str:
    """Render one account's folder tree as a grid."""
    total = _weight(usage.root)
    threshold = int(total * HIGHLIGHT_SHARE)
    title = f"{usage.name}  ({usage.prefix})"
    lines = [
        click.style(title, bold=True),
        _heading(),
        _row(
            "All folders",
            f"{usage.root.total_messages:,}",
            human_bytes(usage.root.total_envelope_bytes, exact),
            human_bytes(usage.root.total_disk_bytes, exact),
        ),
    ]
    lines.extend(_tree_rows(usage.root, min_size, exact, threshold, depth=1))
    if usage.root.own_disk_bytes:
        # Files sitting in the account directory outside any mailbox. Shown so
        # the folders plus this equal the account total.
        lines.append(
            _row(
                f"{INDENT}(account files)",
                "",
                "",
                human_bytes(usage.root.own_disk_bytes, exact),
            )
        )
    if usage.unreadable:
        lines.append(
            f"  {len(usage.unreadable)} item(s) could not be read and are not counted."
        )
    return "\n".join(lines)


def _summary_label(account: AccountUsage) -> str:
    """The account's name, with its prefix where the name cannot stand alone.

    Accounts no longer configured in Mail all resolve to the same placeholder,
    so several rows would otherwise be identically labelled — unreadable, and
    impossible to pass to ``--account``.
    """
    if account.name == NOT_IN_MAIL:
        return f"{account.name} {account.prefix}"
    return account.name


def render_summary(
    accounts: list[AccountUsage], maildata_total: int, exact: bool
) -> str:
    """Render the all-accounts grid, with MailData and a reconciling total."""
    total_disk = sum(
        account.root.total_disk_bytes or 0 for account in accounts
    ) + maildata_total
    threshold = int(total_disk * HIGHLIGHT_SHARE)
    lines = [click.style("All accounts", bold=True), _heading("Account")]
    for account in accounts:
        disk = account.root.total_disk_bytes
        lines.append(
            _row(
                _summary_label(account),
                f"{account.root.total_messages:,}",
                human_bytes(account.root.total_envelope_bytes, exact),
                human_bytes(disk, exact),
                highlight=threshold > 0 and (disk or 0) >= threshold,
            )
        )
    if maildata_total:
        lines.append(
            _row("MailData (Mail's own indexes)", "", "", human_bytes(maildata_total, exact))
        )
    lines.append(
        _row(
            "Total",
            f"{sum(a.root.total_messages for a in accounts):,}",
            human_bytes(sum(a.root.total_envelope_bytes for a in accounts), exact),
            human_bytes(total_disk, exact),
        )
    )
    return "\n".join(lines)


def render_maildata(items: list[tuple[str, int]], exact: bool, min_size: int = 0) -> str:
    """Render Mail's own bookkeeping directory.

    The message and envelope columns are blank throughout: nothing here is a
    message, so neither figure applies. Slightly inconsistent with the other
    grids, and honest about what it is.

    ``min_size`` collapses the small items exactly as it does folders — this
    directory is mostly a few dozen tiny property lists, and listing them all
    buries the one item that matters.
    """
    if not items:
        return ""
    total = sum(size for _name, size in items)
    threshold = int(total * HIGHLIGHT_SHARE)
    kept = [item for item in items if item[1] >= min_size] if min_size > 0 else items
    dropped = [item for item in items if item[1] < min_size] if min_size > 0 else []
    lines = [
        click.style("MailData — Mail's own indexes and caches, not mail", bold=True),
        _heading("Item"),
    ]
    for name, size in kept:
        lines.append(
            _row(name, "", "", human_bytes(size, exact),
                 highlight=threshold > 0 and size >= threshold)
        )
    if dropped:
        noun = "item" if len(dropped) == 1 else "items"
        lines.append(
            _row(
                f"{INDENT}{len(dropped)} smaller {noun}",
                "", "",
                human_bytes(sum(size for _name, size in dropped), exact),
            )
        )
    lines.append(_row("Total", "", "", human_bytes(total, exact)))
    return "\n".join(lines)
