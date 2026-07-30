"""Executing accepted decisions — the first code that moves real mail.

Every test here runs against ``FakeMail``. Nothing in the suite touches a real
mailbox, by design: the live path is exercised once, by hand, on one message,
under the checkpoint in Task 11.
"""

from mail_triage.config import Config, Source
from mail_triage.envelope import MessageRow
from mail_triage.execute import execute
from mail_triage.journal import Journal, new_run_id, undo_run
from mail_triage.mail_app import FakeMail, MailNotRunningError
from mail_triage.model.classify import Proposal
from mail_triage.review import Decision

KEYS = {1: "<one@example.com>", 2: "<two@example.com>"}


def decision(rowid=1, folder="Orders", accepted=True):
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s",
                         date_sent=1, mailbox_url="imap://A/INBOX", read=False)
    return Decision(Proposal(message, folder, 0.9, "reason", "sender"), accepted=accepted)


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://A", local_dir=tmp_path,
                  filing_account="iCloud")
    values.update(overrides)
    return Config(**values)


def started_journal(config):
    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    return journal, run_id


# execute() derives each message's source account from its mailbox_url, so
# the config it is handed must name a source with a matching prefix.
CONFIG_PREFIX = "imap://A"


def test_accepted_decisions_are_moved(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"], keys=KEYS)
    moved, failed = execute([decision(1), decision(2)], mail, journal, make_config(tmp_path))
    assert (moved, failed) == (2, 0)
    assert {entry[0] for entry in mail.moved} == {1, 2}


def test_rejected_decisions_are_not_moved(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys=KEYS)
    moved, failed = execute([decision(1, accepted=False)], mail, journal, make_config(tmp_path))
    assert (moved, failed) == (0, 0)
    assert mail.moved == []


def test_a_failure_does_not_stop_the_batch(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"], keys=KEYS)
    moved, failed = execute(
        [decision(1, folder="Nonexistent"), decision(2)], mail, journal, make_config(tmp_path)
    )
    assert (moved, failed) == (1, 1)
    assert [entry[0] for entry in mail.moved] == [2]


def test_journal_is_written_before_the_move(tmp_path):
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys=KEYS)
    execute([decision(1)], mail, journal, make_config(tmp_path))
    entries = Journal(config).load(run_id)
    assert entries[0].status == "moved"
    assert entries[0].from_folder == "INBOX"


def test_the_durable_key_is_journalled(tmp_path):
    """Without the RFC-822 key, ``undo_run`` cannot reverse the move at all."""
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys=KEYS)
    execute([decision(1)], mail, journal, make_config(tmp_path))
    assert Journal(config).load(run_id)[0].message_key == "<one@example.com>"


def test_a_message_with_no_durable_key_is_not_moved(tmp_path):
    """Refusing to move beats moving something we could never put back."""
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys={})
    moved, failed = execute([decision(1)], mail, journal, make_config(tmp_path))
    assert (moved, failed) == (0, 1)
    assert mail.moved == []
    assert Journal(config).load(run_id)[0].status == "failed"


def test_a_failure_to_read_the_key_is_not_fatal(tmp_path):
    """A key lookup that raises is one message lost, not the whole batch."""
    config = make_config(tmp_path)
    journal, _ = started_journal(config)
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"], keys=KEYS)
    original = mail.message_key

    def flaky(message_id, source_folder, account):
        if message_id == 1:
            raise MailNotRunningError("Mail is not running")
        return original(message_id, source_folder, account)

    mail.message_key = flaky
    moved, failed = execute([decision(1), decision(2)], mail, journal, make_config(tmp_path))
    assert (moved, failed) == (1, 1)
    assert [entry[0] for entry in mail.moved] == [2]


def test_an_executed_run_can_be_undone(tmp_path):
    """The whole point of the journal: a live batch is reversible."""
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"], keys=KEYS)
    execute([decision(1), decision(2)], mail, journal, make_config(tmp_path))
    assert mail.folder_message_ids("INBOX") == []

    reversed_count, failed = undo_run(run_id, config, mail, "iCloud")
    assert (reversed_count, failed) == (2, 0)
    assert sorted(mail.folder_message_ids("INBOX")) == [1, 2]
    assert mail.folder_message_ids("Orders") == []


def test_the_source_folder_is_configurable(tmp_path):
    """The inbox is named in config; it is not always literally "INBOX"."""
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "Inbox"], folders={"Inbox": [1]}, keys=KEYS
    )
    moved, failed = execute(
        [decision(1)], mail, journal, make_config(tmp_path, inbox_folder="Inbox")
    )
    assert (moved, failed) == (1, 0)
    assert mail.moved[0][3] == "Inbox"


# --- Deletion ---------------------------------------------------------------
#
# A delete is a move to the Trash, deliberately: it goes through the same
# journal and the same undo path as any other move, so "I binned that by
# mistake" is recoverable. Nothing here hard-deletes, and nothing bypasses
# the durable-key rule that makes undo possible.

TRASH = "Deleted Messages"


def delete_decision(rowid=1, folder="Orders"):
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s",
                         date_sent=1, mailbox_url="imap://A/INBOX", read=False)
    return Decision(Proposal(message, folder, 0.9, "reason", "sender"),
                    accepted=True, action="delete")


def test_a_delete_moves_the_message_to_the_trash(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys=KEYS)
    moved, failed = execute([delete_decision(1)], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    assert (moved, failed) == (1, 0)
    assert [(entry[0], entry[1]) for entry in mail.moved] == [(1, TRASH)]


def test_a_delete_goes_to_the_trash_not_the_proposed_folder(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys=KEYS)
    execute([delete_decision(1, folder="Orders")], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    assert [entry[1] for entry in mail.moved] == [TRASH]


def test_a_delete_is_journalled_as_a_move_to_the_trash(tmp_path):
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys=KEYS)
    execute([delete_decision(1)], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    entry = Journal(config).load(run_id)[-1]
    assert entry.to_folder == TRASH
    assert entry.from_folder == "INBOX"
    assert entry.status == "moved"


def test_a_delete_without_a_durable_key_is_refused(tmp_path):
    """Same rule as a move: bin nothing that cannot be put back."""
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys={})
    moved, failed = execute([delete_decision(1)], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    assert (moved, failed) == (0, 1)
    assert mail.moved == []


def test_undo_puts_a_deleted_message_back_in_the_inbox(tmp_path):
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys=KEYS)
    execute([delete_decision(1)], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    mail.moved.clear()
    reversed_count, failed = undo_run(run_id, config, mail, "iCloud")
    assert (reversed_count, failed) == (1, 0)
    assert [entry[1] for entry in mail.moved] == ["INBOX"]


def test_a_delete_and_a_file_in_one_batch_both_happen(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders", TRASH], keys=KEYS)
    moved, failed = execute(
        [delete_decision(1), decision(2)], mail, journal, make_config(tmp_path, trash_folder=TRASH)
    )
    assert (moved, failed) == (2, 0)
    assert {entry[0]: entry[1] for entry in mail.moved} == {1: TRASH, 2: "Orders"}


def test_a_rejected_delete_bins_nothing(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders", TRASH], keys=KEYS)
    rejected = Decision(delete_decision(1).proposal, accepted=False, action="delete")
    moved, failed = execute([rejected], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    assert (moved, failed) == (0, 0)
    assert mail.moved == []


def test_a_delete_needs_no_proposed_folder(tmp_path):
    """Binning mail the classifier could not place is the main use for delete,
    so a delete decision must not be skipped for having no folder."""
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=[TRASH], keys=KEYS)
    unplaced = MessageRow(rowid=1, sender="a@b.example", subject="s",
                          date_sent=1, mailbox_url="imap://A/INBOX", read=False)
    decision_ = Decision(Proposal(unplaced, None, 0.2, "too inconsistent", "sender"),
                         accepted=True, action="delete")
    moved, failed = execute([decision_], mail, journal, make_config(tmp_path, trash_folder=TRASH))
    assert (moved, failed) == (1, 0)
    assert [entry[1] for entry in mail.moved] == [TRASH]


def test_an_accepted_file_decision_with_no_folder_is_still_skipped(tmp_path):
    journal, _ = started_journal(make_config(tmp_path))
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys=KEYS)
    unplaced = MessageRow(rowid=1, sender="a@b.example", subject="s",
                          date_sent=1, mailbox_url="imap://A/INBOX", read=False)
    decision_ = Decision(Proposal(unplaced, None, 0.2, "too inconsistent", "sender"),
                         accepted=True)
    moved, failed = execute([decision_], mail, journal, make_config(tmp_path))
    assert (moved, failed) == (0, 0)
    assert mail.moved == []


def test_the_journal_records_the_account_for_a_same_account_move(tmp_path):
    """Groundwork for cross-account filing: today both ends are the same
    account, but the journal must say so rather than leave it blank."""
    config = make_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys=KEYS)
    execute([decision(1)], mail, journal, make_config(tmp_path))
    entry = Journal(config).load(run_id)[-1]
    assert entry.from_account == "iCloud"
    assert entry.to_account == "iCloud"


# --- Filing across accounts -----------------------------------------------------
#
# A Gmail message is filed into the iCloud tree, so the two ends of the move
# are different accounts. The source is derived from the message's own
# mailbox_url — a run covers several accounts and only the message knows which
# one it came from.

GMAIL = Source(name="Gmail", prefix="imap://B", inbox="INBOX", trash="[Gmail]/Bin")
ICLOUD = Source(name="iCloud", prefix="imap://A", inbox="INBOX",
                trash="Deleted Messages")


def two_account_config(tmp_path):
    return Config(
        account_url_prefix="imap://A", local_dir=tmp_path,
        filing_account="iCloud", filing_account_prefix="imap://A",
        sources=[ICLOUD, GMAIL],
    )


def gmail_decision(rowid=1, folder="Orders", action="file"):
    """A message living in Gmail's All Mail, as every Gmail message does."""
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s",
                         date_sent=1, mailbox_url="imap://B/%5BGmail%5D/All%20Mail",
                         read=False)
    return Decision(Proposal(message, folder, 0.9, "reason", "sender"),
                    accepted=True, action=action)


def two_account_mail():
    return FakeMail(
        inbox=[], mailboxes=["Orders", "[Gmail]/Bin", "INBOX"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys=KEYS,
    )


def test_a_gmail_message_is_filed_into_the_filing_account(tmp_path):
    config = two_account_config(tmp_path)
    journal, _ = started_journal(config)
    mail = two_account_mail()
    moved, failed = execute([gmail_decision()], mail, journal, config)
    assert (moved, failed) == (1, 0)
    assert mail.folder_message_ids("Orders", account="iCloud") == [1]
    assert mail.inbox_message_ids("Gmail") == []


def test_the_journal_records_both_accounts(tmp_path):
    config = two_account_config(tmp_path)
    journal, run_id = started_journal(config)
    execute([gmail_decision()], mail := two_account_mail(), journal, config)
    assert mail  # the fake is used; silences the walrus lint
    entry = list(journal.load(run_id))[-1]
    assert (entry.from_account, entry.to_account) == ("Gmail", "iCloud")
    assert (entry.from_folder, entry.to_folder) == ("INBOX", "Orders")


def test_binning_a_gmail_message_stays_in_gmail(tmp_path):
    """A bin is not a filing destination, so it must never cross accounts."""
    config = two_account_config(tmp_path)
    journal, run_id = started_journal(config)
    mail = two_account_mail()
    moved, failed = execute([gmail_decision(action="delete")], mail, journal, config)
    assert (moved, failed) == (1, 0)
    assert mail.folder_message_ids("[Gmail]/Bin", account="Gmail") == [1]
    entry = list(journal.load(run_id))[-1]
    assert entry.to_account == "Gmail"
    assert entry.to_folder == "[Gmail]/Bin"


def test_a_message_from_an_unconfigured_account_is_not_moved(tmp_path):
    """Never guess the source account: the wrong guess moves the wrong mail."""
    config = two_account_config(tmp_path)
    journal, _ = started_journal(config)
    message = MessageRow(rowid=1, sender="a@b.example", subject="s", date_sent=1,
                         mailbox_url="imap://Z/INBOX", read=False)
    stray = Decision(Proposal(message, "Orders", 0.9, "reason", "sender"), accepted=True)
    mail = two_account_mail()
    moved, failed = execute([stray], mail, journal, config)
    assert (moved, failed) == (0, 1)
    assert mail.folder_message_ids("Orders", account="iCloud") == []
