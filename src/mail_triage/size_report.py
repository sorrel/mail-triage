"""Render the size grids.

Kept apart from ``sizes.py`` so that measurement can be tested without a
terminal and layout without a mail store.

Layout arithmetic comes from ``layout``: widths are measured in terminal
columns rather than characters, and colour is applied *after* padding is
computed. See that module for why both matter.
"""

from __future__ import annotations

import re

import click

from mail_triage.accounts import NOT_IN_MAIL
from mail_triage.layout import cell, display_width
from mail_triage.sizes import AccountUsage, FolderNode

GRID_FOLDER_WIDTH = 44
NUMBER_WIDTH = 12
COUNT_WIDTH = 9

# Shares of the grid's total at which the disk figure changes weight. Shares
# rather than fixed byte counts, so the colour means the same thing in a 20 MB
# account as in a 5 GB one. Three tiers, so the eye finds the big folders
# without reading a single number.
HIGHLIGHT_SHARE = 0.20
NOTABLE_SHARE = 0.05
NEGLIGIBLE_SHARE = 0.01

GRID_WIDTH = GRID_FOLDER_WIDTH + COUNT_WIDTH + NUMBER_WIDTH * 2 + 3

# Guides drawn down the left of the folder column. Dark-terminal friendly:
# dimmed box-drawing rather than another colour competing with the figures.
GUIDE_BRANCH = "├─ "
GUIDE_LAST = "└─ "
GUIDE_LINE = "│  "
GUIDE_BLANK = "   "

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


def _heading(label: str = "Folder") -> str:
    line = (
        f"{cell(label, GRID_FOLDER_WIDTH)} "
        f"{cell('Messages', COUNT_WIDTH, right=True)} "
        f"{cell('In Mail', NUMBER_WIDTH, right=True)} "
        f"{cell('On disk', NUMBER_WIDTH, right=True)}"
    )
    return click.style(line, fg="bright_black", bold=True)


def _title(text: str) -> str:
    """A section heading: cyan reads clearly on a dark terminal."""
    return click.style(text, fg="cyan", bold=True)


def _rule(char: str = "─") -> str:
    return click.style(char * GRID_WIDTH, fg="bright_black")


def _disk_cell(text: str, share: float | None) -> str:
    """The disk figure, weighted by how much of the grid's total it is.

    Bold yellow for the folders worth acting on, plain yellow for the ones
    worth noticing, dim for the rest — and dim for a dash, which is an
    absence rather than a small number.

    ``share`` of ``None`` means "do not weight this": a total is not competing
    with the rows above it, and dimming it would bury the very figure the
    grid exists to report.
    """
    padded = cell(text, NUMBER_WIDTH, right=True)
    if text == NO_FIGURE:
        return click.style(padded, dim=True)
    if share is None:
        return padded
    if share >= HIGHLIGHT_SHARE:
        return click.style(padded, fg="yellow", bold=True)
    if share >= NOTABLE_SHARE:
        return click.style(padded, fg="yellow")
    if share < NEGLIGIBLE_SHARE:
        return click.style(padded, dim=True)
    return padded


def _row(
    label: str,
    count: str,
    envelope: str,
    disk: str,
    share: float | None = None,
    guide: str = "",
    bold_label: bool = False,
) -> str:
    """Build one grid row.

    Every cell is padded to width *before* it is styled — an ANSI escape
    occupies no columns but would be counted as several, so colouring first
    would throw the whole grid out of alignment.
    """
    name = cell(label, GRID_FOLDER_WIDTH - display_width(guide))
    if bold_label:
        name = click.style(name, bold=True)
    return (
        f"{click.style(guide, fg='bright_black') if guide else ''}{name} "
        f"{click.style(cell(count, COUNT_WIDTH, right=True), dim=True)} "
        f"{click.style(cell(envelope, NUMBER_WIDTH, right=True), fg='bright_blue')} "
        f"{_disk_cell(disk, share)}"
    )


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


def _share(value: int | None, total: int) -> float:
    """What fraction of the grid's total ``value`` represents."""
    if not total or value is None:
        return 0.0
    return value / total


def _collapse_row(
    dropped: list[FolderNode], guide: str, exact: bool, total: int
) -> str:
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
    return _row(
        f"{len(dropped)} smaller {noun}",
        f"{messages:,}" if messages else "",
        human_bytes(envelope, exact),
        human_bytes(disk, exact),
        share=_share(disk, total),
        guide=guide,
    )


def _tree_rows(
    node: FolderNode,
    min_size: int,
    exact: bool,
    total: int,
    prefix: str = "",
    reserve_last: bool = False,
) -> list[str]:
    """Render ``node``'s children as a guided tree, largest first.

    ``prefix`` carries the guides for the levels already drawn, so a deep
    branch stays visually connected to the folder it hangs from.

    ``reserve_last`` says a further row will be appended after this level, so
    the closing guide belongs to that row rather than to the last child here.
    Two closing guides at one level read as two separate trees.
    """
    rows: list[str] = []
    kept, dropped = _split_small(node.children, min_size)
    for index, child in enumerate(kept):
        last = index == len(kept) - 1 and not dropped and not reserve_last
        rows.append(
            _row(
                child.name,
                f"{child.total_messages:,}" if child.total_messages else "",
                human_bytes(child.total_envelope_bytes, exact),
                human_bytes(child.total_disk_bytes, exact),
                share=_share(_weight(child), total),
                guide=prefix + (GUIDE_LAST if last else GUIDE_BRANCH),
                bold_label=bool(child.children),
            )
        )
        rows.extend(
            _tree_rows(
                child,
                min_size,
                exact,
                total,
                prefix + (GUIDE_BLANK if last else GUIDE_LINE),
            )
        )
    if dropped:
        rows.append(
            _collapse_row(
                dropped,
                prefix + (GUIDE_BRANCH if reserve_last else GUIDE_LAST),
                exact,
                total,
            )
        )
    return rows


def render_account(usage: AccountUsage, min_size: int, exact: bool) -> str:
    """Render one account's folder tree as a grid."""
    total = _weight(usage.root)
    lines = [
        _title(f"{usage.name}  {click.style(usage.prefix, fg='bright_black', bold=False)}"),
        _heading(),
        _rule(),
    ]
    lines.extend(
        _tree_rows(
            usage.root, min_size, exact, total,
            reserve_last=bool(usage.root.own_disk_bytes),
        )
    )
    if usage.root.own_disk_bytes:
        # Files sitting in the account directory outside any mailbox. Shown so
        # the folders plus this equal the account total.
        lines.append(
            _row(
                "(account files)",
                "",
                "",
                human_bytes(usage.root.own_disk_bytes, exact),
                share=_share(usage.root.own_disk_bytes, total),
                guide=GUIDE_LAST,
            )
        )
    lines.append(_rule())
    lines.append(
        _row(
            "All folders",
            f"{usage.root.total_messages:,}",
            human_bytes(usage.root.total_envelope_bytes, exact),
            human_bytes(usage.root.total_disk_bytes, exact),
            bold_label=True,
        )
    )
    if usage.unreadable:
        lines.append(
            click.style(
                f"{len(usage.unreadable)} item(s) could not be read and are not counted.",
                fg="bright_black",
            )
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
    lines = [_title("All accounts"), _heading("Account"), _rule()]
    for account in accounts:
        disk = account.root.total_disk_bytes
        lines.append(
            _row(
                _summary_label(account),
                f"{account.root.total_messages:,}",
                human_bytes(account.root.total_envelope_bytes, exact),
                human_bytes(disk, exact),
                share=_share(disk, total_disk),
            )
        )
    if maildata_total:
        lines.append(
            _row(
                "MailData (Mail's own indexes, not mail)",
                "",
                "",
                human_bytes(maildata_total, exact),
                share=_share(maildata_total, total_disk),
            )
        )
    lines.append(_rule())
    lines.append(
        _row(
            "Total",
            f"{sum(a.root.total_messages for a in accounts):,}",
            human_bytes(sum(a.root.total_envelope_bytes for a in accounts), exact),
            human_bytes(total_disk, exact),
            bold_label=True,
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
    kept = [item for item in items if item[1] >= min_size] if min_size > 0 else items
    dropped = [item for item in items if item[1] < min_size] if min_size > 0 else []
    lines = [
        _title("MailData — Mail's own indexes and caches, not mail"),
        _heading("Item"),
        _rule(),
    ]
    for index, (name, size) in enumerate(kept):
        lines.append(
            _row(
                name, "", "", human_bytes(size, exact),
                share=_share(size, total),
                guide=GUIDE_LAST if index == len(kept) - 1 and not dropped else GUIDE_BRANCH,
            )
        )
    if dropped:
        noun = "item" if len(dropped) == 1 else "items"
        lines.append(
            _row(
                f"{len(dropped)} smaller {noun}",
                "", "",
                human_bytes(sum(size for _name, size in dropped), exact),
                guide=GUIDE_LAST,
            )
        )
    lines.append(_rule())
    lines.append(_row("Total", "", "", human_bytes(total, exact), bold_label=True))
    return "\n".join(lines)
