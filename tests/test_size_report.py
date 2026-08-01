"""Rendering of the size grids.

Trees are constructed by hand here, so the arithmetic assertions test the
renderer's own reconciliation rather than the measurement it was given.
"""

import re

import pytest

from mail_triage.size_report import (
    human_bytes,
    parse_size,
    render_account,
    render_maildata,
    render_summary,
)
from mail_triage.sizes import AccountUsage, FolderNode

MB = 1024**2
GB = 1024**3


def plain(text: str) -> str:
    """Strip ANSI colour, so assertions read content rather than styling."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def leaf(name, disk, envelope, count, children=()):
    return FolderNode(
        path=name,
        name=name,
        own_disk_bytes=disk,
        envelope_bytes=envelope,
        message_count=count,
        children=tuple(children),
    )


def account(root, on_disk=True, name="Test"):
    return AccountUsage(
        prefix="imap://AAAAAAAA", name=name, root=root, on_disk=on_disk
    )


# --- Units --------------------------------------------------------------------


def test_human_bytes_uses_binary_units():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * GB) == "5.0 GB"


def test_human_bytes_marks_the_absence_of_a_disk_figure():
    """A dash, not a zero: 'not stored here' is not 'empty'."""
    assert human_bytes(None) == "—"


def test_human_bytes_exact_gives_digits():
    assert human_bytes(1536, exact=True) == "1536"
    assert human_bytes(None, exact=True) == "—"


def test_parse_size_accepts_units_and_bare_bytes():
    assert parse_size("2MB") == 2 * MB
    assert parse_size("1.5 gb") == int(1.5 * GB)
    assert parse_size("0") == 0
    assert parse_size("4096") == 4096


def test_parse_size_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_size("big")


# --- Collapsing ---------------------------------------------------------------


def test_small_folders_collapse_into_a_reconciling_line():
    usage = account(
        leaf("", 0, 0, 0, [
            leaf("Large", 10 * MB, 10 * MB, 5),
            leaf("Tiny", 100, 100, 1),
            leaf("Alsotiny", 200, 200, 2),
        ])
    )
    output = plain(render_account(usage, min_size=MB, exact=False))
    assert "Large" in output
    assert "Tiny" not in output
    assert "2 smaller folders" in output


def test_the_collapse_line_carries_the_bytes_it_replaced():
    """Visible rows plus the collapse line must equal the account total."""
    usage = account(
        leaf("", 0, 0, 0, [
            leaf("Large", 10 * MB, 0, 0),
            leaf("A", 300 * 1024, 0, 0),
            leaf("B", 400 * 1024, 0, 0),
        ])
    )
    output = plain(render_account(usage, min_size=MB, exact=True))
    collapsed = next(line for line in output.splitlines() if "smaller folders" in line)
    assert str(700 * 1024) in collapsed


def test_nothing_collapses_when_min_size_is_zero():
    usage = account(leaf("", 0, 0, 0, [leaf("Tiny", 100, 100, 1)]))
    output = plain(render_account(usage, min_size=0, exact=False))
    assert "Tiny" in output
    assert "smaller folders" not in output


def test_a_single_small_folder_reads_in_the_singular():
    usage = account(leaf("", 0, 0, 0, [
        leaf("Large", 10 * MB, 0, 0), leaf("Tiny", 100, 0, 0),
    ]))
    output = plain(render_account(usage, min_size=MB, exact=False))
    assert "1 smaller folder " in output or output.rstrip().endswith("1 smaller folder")


def test_a_small_parent_with_a_large_child_is_kept():
    """Collapsing on the parent's own size alone would hide the child."""
    usage = account(
        leaf("", 0, 0, 0, [
            leaf("Small parent", 10, 0, 0, [leaf("Big child", 10 * MB, 0, 0)]),
        ])
    )
    output = plain(render_account(usage, min_size=MB, exact=False))
    assert "Small parent" in output
    assert "Big child" in output


# --- Layout -------------------------------------------------------------------


def test_children_are_indented_under_their_parent():
    usage = account(
        leaf("", 0, 0, 0, [
            leaf("Parent", 10 * MB, 10 * MB, 5, [leaf("Child", 5 * MB, 5 * MB, 2)]),
        ])
    )
    lines = plain(render_account(usage, min_size=0, exact=False)).splitlines()
    parent_line = next(line for line in lines if "Parent" in line)
    child_line = next(line for line in lines if "Child" in line)
    assert child_line.index("Child") > parent_line.index("Parent")


def test_an_account_without_a_local_store_shows_a_dash_not_a_zero():
    usage = account(
        leaf("", None, 0, 0, [leaf("Folder", None, 4096, 3)]),
        on_disk=False,
        name="Remote",
    )
    output = plain(render_account(usage, min_size=0, exact=False))
    assert "—" in output
    assert "0 B" not in output


def test_message_counts_are_shown():
    usage = account(leaf("", 0, 0, 0, [leaf("Folder", 10 * MB, 10 * MB, 42)]))
    assert "42" in plain(render_account(usage, min_size=0, exact=False))


def test_wide_folder_names_do_not_break_alignment():
    """Emoji occupy two columns; padding on len() would misalign the grid."""
    usage = account(
        leaf("", 0, 0, 0, [
            leaf("📦 Wide", 10 * MB, 0, 1),
            leaf("Narrow", 9 * MB, 0, 1),
        ])
    )
    lines = [
        line for line in plain(render_account(usage, min_size=0, exact=False)).splitlines()
        if "Wide" in line or "Narrow" in line
    ]
    from mail_triage.review import display_width

    assert len({display_width(line) for line in lines}) == 1


# --- Highlighting -------------------------------------------------------------


def test_the_largest_rows_are_highlighted():
    usage = account(
        leaf("", 0, 0, 0, [leaf("Huge", 100 * MB, 0, 1), leaf("Modest", MB, 0, 1)])
    )
    output = render_account(usage, min_size=0, exact=False)
    huge = next(line for line in output.splitlines() if "Huge" in line)
    modest = next(line for line in output.splitlines() if "Modest" in line)
    assert "\x1b[33m" in huge
    assert "\x1b[33m" not in modest


def test_highlighting_is_relative_to_the_account_not_an_absolute_cut_off():
    """A dominant folder in a small account still deserves the colour."""
    usage = account(leaf("", 0, 0, 0, [leaf("Dominant", 500 * 1024, 0, 1)]))
    output = render_account(usage, min_size=0, exact=False)
    assert "\x1b[33m" in output


# --- Summary and MailData -----------------------------------------------------


def test_the_summary_includes_maildata_so_the_total_reconciles():
    accounts = [
        AccountUsage(
            prefix="local://AAAAAAAA",
            name="On My Mac",
            root=leaf("", GB, GB, 10),
            on_disk=True,
        )
    ]
    output = plain(render_summary(accounts, maildata_total=100 * MB, exact=False))
    assert "On My Mac" in output
    assert "MailData" in output
    assert "Total" in output


def test_the_summary_total_is_the_sum_of_its_rows():
    accounts = [
        AccountUsage(prefix="local://AAAAAAAA", name="One",
                     root=leaf("", 2 * MB, 0, 0), on_disk=True),
        AccountUsage(prefix="imap://BBBBBBBB", name="Two",
                     root=leaf("", 3 * MB, 0, 0), on_disk=True),
    ]
    output = plain(render_summary(accounts, maildata_total=MB, exact=True))
    total_line = next(line for line in output.splitlines() if "Total" in line)
    assert str(6 * MB) in total_line


def test_maildata_grid_shows_items_by_size():
    output = plain(render_maildata([("Envelope Index", 284 * MB)], exact=False))
    assert "Envelope Index" in output
    assert "284.0 MB" in output


def test_maildata_grid_is_empty_when_there_is_nothing_to_show():
    assert render_maildata([], exact=False) == ""
