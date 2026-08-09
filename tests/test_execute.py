"""Carrying out decisions: journalled first, and verified afterwards."""

from __future__ import annotations

from mail_triage.config import Config, Source
from mail_triage.envelope import MessageRow
from mail_triage.execute import execute
from mail_triage.journal import Journal
from mail_triage.mail_app import FakeMail
from mail_triage.model.classify import Proposal
from mail_triage.review import Decision


# --- a move that reports success is not a message that left the inbox --------
#
# Measured on a live mailbox, 9 August 2026: filing a Gmail message into the
# iCloud tree copied it and left the original in the Gmail inbox. Four
# attempts, three copies in the destination, and the journal said "moved"
# every time.

def gmail_config(tmp_path, archive=None):
    return Config(
        account_url_prefix="imap://GGGGGGGG",
        local_dir=tmp_path / "local",
        filing_account="iCloud",
        filing_account_prefix="imap://AAAAAAAA",
        sources=[
            Source(name="Gmail", prefix="imap://GGGGGGGG", inbox="INBOX",
                   trash="[Gmail]/Bin", archive=archive),
        ],
    )


def gmail_decision():
    message = MessageRow(
        rowid=1, sender="news@list.example", subject="Issue 350",
        date_sent=1_700_000_000, mailbox_url="imap://GGGGGGGG/INBOX", read=False,
    )
    return Decision(
        proposal=Proposal(message, "Filed/News", 0.9, "history", "sender"),
        accepted=True,
    )


def sticky_mail():
    """A mailbox where a move copies and leaves the original — the Gmail case."""
    return FakeMail(
        inbox=[1],
        mailboxes=["Filed/News", "[Gmail]/Bin", "[Gmail]/All Mail"],
        keys={1: "<issue-350@list.example>"},
        leaves_original=True,
    )


def test_a_move_that_left_the_original_behind_is_reported_as_a_failure(tmp_path):
    """Not as a success. The whole point: no more silent duplicates."""
    mail = sticky_mail()
    config = gmail_config(tmp_path)
    journal = Journal(config)
    journal.begin("test-run")
    moved, failed = execute([gmail_decision()], mail, journal, config)
    assert (moved, failed) == (0, 1)
    entry = journal.load("test-run")[-1]
    assert entry.status == "failed"


def test_an_archive_clears_the_label_and_the_move_then_counts(tmp_path):
    """The success path, and the reason the archive is configured at all.

    The cross-account file copies and leaves the label; the archive move is
    *within* the account, so it clears it, and the message really has left
    the inbox. Proved against live Gmail on 9 August 2026: INBOX ->
    [Gmail]/All Mail left the message in All Mail and gone from the inbox.
    """
    mail = sticky_mail()
    config = gmail_config(tmp_path, archive="[Gmail]/All Mail")
    journal = Journal(config)
    journal.begin("test-run")
    moved, failed = execute([gmail_decision()], mail, journal, config)
    assert (moved, failed) == (1, 0)
    assert journal.load("test-run")[-1].status == "moved"
    # One copy in the destination, and nothing left in the inbox.
    assert mail.folder_message_ids("Filed/News", "iCloud") == [1]
    assert not mail.message_exists("<issue-350@list.example>", "INBOX", "Gmail")


class OptimisticMail(FakeMail):
    """Mail as it actually answered on 9 August 2026.

    Asked whether the message has left the inbox, it says yes — from its own
    local state, before the server has confirmed anything — and the label is
    back by the time anybody looks again. The contents underneath never
    changed, which is the point: the *reading* is wrong, not the mailbox.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.inbox_questions = 0

    def message_exists(self, message_key: str, folder: str, account: str) -> bool:
        if folder == "INBOX":
            self.inbox_questions += 1
            if self.inbox_questions == 1:
                return False
        return super().message_exists(message_key, folder, account)


def test_the_label_is_cleared_even_when_mail_claims_the_message_already_left(tmp_path):
    """The live failure this whole mechanism exists to stop, one layer deeper.

    Clearing the label used to be conditional on the very reading that cannot
    be trusted: ask Mail whether the message is still in the inbox, and only
    archive it if the answer is yes. On 9 August 2026 the answer was no, the
    archive step was skipped, the journal recorded "moved" — and the message
    was still labelled INBOX fifteen minutes later, to be filed again on the
    next run. Four runs, four copies in the destination.

    So the archive is no longer a repair conditional on a diagnosis. It runs
    on every cross-account move, and the check only reports.
    """
    mail = OptimisticMail(
        inbox=[1],
        mailboxes=["Filed/News", "[Gmail]/Bin", "[Gmail]/All Mail"],
        keys={1: "<issue-350@list.example>"},
        leaves_original=True,
    )
    config = gmail_config(tmp_path, archive="[Gmail]/All Mail")
    journal = Journal(config)
    journal.begin("test-run")
    execute([gmail_decision()], mail, journal, config)
    # The mailbox itself, not Mail's opinion of it: the label really is gone.
    assert mail.folder_message_ids("INBOX", "Gmail") == []
    assert mail.folder_message_ids("[Gmail]/All Mail", "Gmail") == [1]
    # And exactly one copy in the destination — no second copy next run.
    assert mail.folder_message_ids("Filed/News", "iCloud") == [1]


def test_without_an_archive_the_same_move_is_reported_as_a_failure(tmp_path):
    """No configured way to clear the label, so the move cannot be proved —
    and an unproven move is never reported as a success."""
    mail = sticky_mail()
    config = gmail_config(tmp_path, archive=None)
    journal = Journal(config)
    journal.begin("test-run")
    assert execute([gmail_decision()], mail, journal, config) == (0, 1)


def test_an_ordinary_move_is_unaffected_by_the_check(tmp_path):
    """Exchange and iCloud move properly; nothing here may slow them down or
    turn a real success into a failure."""
    mail = FakeMail(
        inbox=[1], mailboxes=["Filed/News"], keys={1: "<issue-350@list.example>"}
    )
    config = gmail_config(tmp_path)
    journal = Journal(config)
    journal.begin("test-run")
    moved, failed = execute([gmail_decision()], mail, journal, config)
    assert (moved, failed) == (1, 0)


def test_a_message_with_no_durable_key_is_not_second_guessed(tmp_path):
    """execute already refuses to move one of those, so verification never
    sees it — but if it ever does, it must not invent a failure."""
    from mail_triage.execute import _left_the_inbox
    from mail_triage.journal import JournalEntry

    entry = JournalEntry(1, "s", "INBOX", "Filed/News", "planned")
    entry.message_key = ""
    source = Source(name="Gmail", prefix="imap://GGGGGGGG")
    assert _left_the_inbox(entry, FakeMail(inbox=[], mailboxes=[]), source) is True
