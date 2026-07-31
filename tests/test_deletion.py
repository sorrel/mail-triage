"""Tests for deletion as negative evidence (Task 11C).

The user, 26 July 2026: "sometimes I delete messages instead of filing them
away." Two patterns matter: a sender who is now only deleted (veto filing
outright) and a sender who is kept sometimes and binned sometimes (still
proposed, but carrying its bin-rate).

The Trash purges on a rolling window, so filed and deleted counts must come
from the *same* recent window — never lifetime filing against a short
deletion window. ``test_old_filing_history_does_not_mask_a_recent_only_delete_sender``
is the one that catches getting this wrong: it is the case that looked fine
in the first dry run but wasn't.
"""

from __future__ import annotations

from mail_triage.config import Config, Source
from mail_triage.deletion import DeletionStats, build_deletion_index, deletion_veto
from mail_triage.envelope import MessageRow

NOW = 1_700_000_000
DAY = 86_400

ICLOUD = Source(name="iCloud", prefix="imap://AAAAAAAA", trash="Deleted Messages")
GMAIL = Source(
    name="Gmail", prefix="imap://BBBBBBBB", trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]
)
EXCHANGE = Source(
    name="Exchange", prefix="ews://CCCCCCCC", inbox="Inbox",
    trash="Deleted Items", ignore=["Conversation History"],
)


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def row(sender, mailbox_url, date_sent):
    return MessageRow(
        rowid=1, sender=sender, subject="s", date_sent=date_sent,
        mailbox_url=mailbox_url, read=True,
    )


class FakeReader:
    def __init__(self, rows):
        self._rows = rows

    def all_messages(self):
        return iter(self._rows)


# --- DeletionStats ---------------------------------------------------------

def test_delete_ratio_is_share_of_deleted():
    assert DeletionStats(filed=1, deleted=9).delete_ratio == 0.9


def test_delete_ratio_is_zero_with_no_history_at_all():
    assert DeletionStats(filed=0, deleted=0).delete_ratio == 0.0


def test_delete_ratio_is_one_when_only_deleted():
    assert DeletionStats(filed=0, deleted=9).delete_ratio == 1.0


# --- build_deletion_index ---------------------------------------------------

def test_only_deletes_sender_has_ratio_one(tmp_path):
    config = make_config(tmp_path)
    rows = [
        row("news@bulletin.example", "imap://AAAAAAAA/Deleted Messages", NOW - day * DAY)
        for day in range(9)
    ]
    index = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    stats = index["news@bulletin.example"]
    assert stats.filed == 0
    assert stats.deleted == 9


def test_mixed_sender_carries_both_counts(tmp_path):
    config = make_config(tmp_path)
    rows = (
        [row("dan@bulletin.example", "imap://AAAAAAAA/Reading", NOW - day * DAY) for day in range(5)]
        + [row("dan@bulletin.example", "imap://AAAAAAAA/Deleted Messages", NOW - day * DAY) for day in range(21)]
    )
    index = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    stats = index["dan@bulletin.example"]
    assert stats.filed == 5
    assert stats.deleted == 21


def test_trash_folder_counts_as_deleted_too(tmp_path):
    config = make_config(tmp_path)
    rows = [row("news@bulletin.example", "imap://AAAAAAAA/Trash", NOW - DAY)]
    index = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    assert index["news@bulletin.example"].deleted == 1


def test_inbox_and_junk_are_neither_filed_nor_deleted(tmp_path):
    config = make_config(tmp_path)
    rows = [
        row("someone@work.example", "imap://AAAAAAAA/INBOX", NOW - DAY),
        row("someone@work.example", "imap://AAAAAAAA/Junk", NOW - DAY),
    ]
    index = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    assert "someone@work.example" not in index


def test_old_filing_history_does_not_mask_a_recent_only_delete_sender(tmp_path):
    """The window bug: ten years of filing plus two recent months of deletes
    must read as 'only deletes now', not as a balanced mixed sender."""
    config = make_config(tmp_path, deletion_window_days=75)
    ten_years_of_filings = [
        row("news@bulletin.example", "imap://AAAAAAAA/Reading", NOW - (365 * 10 + offset) * DAY)
        for offset in range(50)
    ]
    recent_deletions = [
        row("news@bulletin.example", "imap://AAAAAAAA/Deleted Messages", NOW - day * DAY)
        for day in range(9)
    ]
    index = build_deletion_index(FakeReader(ten_years_of_filings + recent_deletions), config, ICLOUD, now=NOW)
    stats = index["news@bulletin.example"]
    assert stats.filed == 0
    assert stats.deleted == 9
    assert stats.delete_ratio == 1.0


def test_window_boundary_includes_the_edge_day_and_excludes_the_day_after(tmp_path):
    config = make_config(tmp_path, deletion_window_days=75)
    on_boundary = row("edge@bulletin.example", "imap://AAAAAAAA/Deleted Messages", NOW - 75 * DAY)
    just_outside = row("edge@bulletin.example", "imap://AAAAAAAA/Deleted Messages", NOW - 76 * DAY)
    index = build_deletion_index(FakeReader([on_boundary, just_outside]), config, ICLOUD, now=NOW)
    assert index["edge@bulletin.example"].deleted == 1


def test_other_accounts_are_not_counted(tmp_path):
    config = make_config(tmp_path)
    rows = [row("news@bulletin.example", "imap://ZZZZZZZZ/Deleted Messages", NOW - DAY)]
    index = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    assert "news@bulletin.example" not in index


# --- deletion_veto -----------------------------------------------------------

def test_only_deletes_sender_is_vetoed(tmp_path):
    config = make_config(tmp_path)
    stats = DeletionStats(filed=0, deleted=9)
    reason = deletion_veto(stats, config)
    assert reason is not None
    assert "9" in reason


def test_mixed_sender_is_not_vetoed(tmp_path):
    config = make_config(tmp_path)
    stats = DeletionStats(filed=5, deleted=21)
    assert deletion_veto(stats, config) is None


def test_unknown_sender_produces_no_veto(tmp_path):
    config = make_config(tmp_path)
    assert deletion_veto(None, config) is None


def test_custom_veto_ratio_allows_some_leeway(tmp_path):
    config = make_config(tmp_path, delete_veto_ratio=0.8)
    stats = DeletionStats(filed=1, deleted=9)  # ratio 0.9
    reason = deletion_veto(stats, config)
    assert reason is not None


def test_sender_with_no_recent_history_at_all_is_not_vetoed(tmp_path):
    config = make_config(tmp_path)
    stats = DeletionStats(filed=0, deleted=0)
    assert deletion_veto(stats, config) is None


# --- Evidence is counted per account --------------------------------------------
#
# The user's decision, 29 July 2026: Gmail's bin informs Gmail proposals and
# iCloud's informs iCloud's. Filing and binning habits differ between accounts,
# so a pooled ratio would describe neither.

GMAIL_BIN = "imap://BBBBBBBB/%5BGmail%5D/Bin"
GMAIL_ALL_MAIL = "imap://BBBBBBBB/%5BGmail%5D/All%20Mail"


def test_gmail_bin_counts_as_a_deletion(tmp_path):
    config = make_config(tmp_path)
    rows = [row("a@example.com", GMAIL_BIN, NOW - 100)]
    stats = build_deletion_index(FakeReader(rows), config, GMAIL, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (0, 1)


def test_gmail_all_mail_is_ignored_not_counted_as_filed(tmp_path):
    """All Mail holds every message; counting it as filing defeats the veto."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", GMAIL_ALL_MAIL, NOW - 100)]
    assert "a@example.com" not in build_deletion_index(
        FakeReader(rows), config, GMAIL, now=NOW
    )


def test_the_ignore_pattern_does_not_swallow_the_bin(tmp_path):
    """"[[]Gmail]*" also matches "[Gmail]/Bin" — deleted must be tested first."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", GMAIL_BIN, NOW - 100)]
    index = build_deletion_index(FakeReader(rows), config, GMAIL, now=NOW)
    assert index["a@example.com"].deleted == 1, "the bin was swallowed by ignore"


def test_an_index_only_sees_its_own_account(tmp_path):
    config = make_config(tmp_path)
    rows = [
        row("a@example.com", "imap://AAAAAAAA/Deleted%20Messages", NOW - 100),
        row("a@example.com", GMAIL_BIN, NOW - 100),
    ]
    icloud = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)
    gmail = build_deletion_index(FakeReader(rows), config, GMAIL, now=NOW)
    assert icloud["a@example.com"].deleted == 1
    assert gmail["a@example.com"].deleted == 1


def test_icloud_counting_is_unchanged_by_the_source_parameter(tmp_path):
    config = make_config(tmp_path)
    rows = [
        row("a@example.com", "imap://AAAAAAAA/Deleted%20Messages", NOW - 100),
        row("a@example.com", "imap://AAAAAAAA/Parent/Child", NOW - 100),
    ]
    stats = build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (1, 1)


def test_an_ignored_folder_creates_no_empty_bucket(tmp_path):
    """A sender seen only in an ignored folder has no history, not a blank one."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "imap://AAAAAAAA/INBOX", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, ICLOUD, now=NOW) == {}


# --- Exchange folder names ------------------------------------------------------
#
# "Junk Email" is Exchange's name for the junk folder, and the shared ignore
# list only had "Junk". Counted as a filing, spam becomes evidence that the
# sender's mail gets kept — the opposite of what it means — and it suppresses
# the veto that catches senders whose mail is only ever binned.

def test_junk_email_is_not_counted_as_a_filing(tmp_path):
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk%20Email", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_plain_junk_is_still_not_counted_as_a_filing(tmp_path):
    """The widened pattern must not lose what the old one caught."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_a_folder_merely_starting_with_junk_is_still_ignored(tmp_path):
    """"Junk*" is deliberately broad: any junk-ish folder is not a filing."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk%20Mail", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_exchange_deleted_items_counts_as_a_deletion(tmp_path):
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Deleted%20Items", NOW - 100)]
    stats = build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (0, 1)


def test_a_source_ignore_entry_is_not_counted_as_a_filing(tmp_path):
    """Conversation History is written by the mail client, not by the user."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Conversation%20History", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_a_real_exchange_folder_is_still_counted_as_a_filing(tmp_path):
    """The ignores must not swallow genuine filing decisions."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Parent", NOW - 100)]
    stats = build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (1, 0)
