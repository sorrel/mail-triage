"""Carry out accepted decisions, journalling before acting.

This is the only module that causes mail to move in anger. Two rules govern
everything here, and both exist so that a batch is always reversible:

1. **Journal before acting.** The entry is written with status ``"planned"``
   before the move is attempted, so a process killed mid-batch still leaves a
   complete record of what it meant to do — see ``journal.undo_run``, which
   treats a stranded ``planned`` entry as a move that may or may not have
   happened and checks rather than assumes.
2. **No move without a durable key.** The numeric AppleScript id changes when
   a message moves and moving it back does not restore the old value, so the
   RFC-822 Message-ID captured *beforehand* is the only handle undo will have
   later. A message whose key cannot be read is therefore left exactly where
   it is and counted as a failure. Moving it would succeed at the cost of
   being unable to put it back, which is the one outcome the journal exists
   to prevent.
"""

from __future__ import annotations

from mail_triage.config import Config
from mail_triage.folders import account_prefix
from mail_triage.journal import Journal, JournalEntry
from mail_triage.mail_app import MailError, MailInterface
from mail_triage.review import Decision


def execute(
    decisions: list[Decision],
    mail: MailInterface,
    journal: Journal,
    config: Config,
) -> tuple[int, int]:
    """Move each accepted message. Returns ``(moved, failed)``.

    Rejected decisions, and any decision with no folder, are skipped silently:
    they are not failures, just messages the user chose to leave alone.

    A failure never stops the batch. One unreachable mailbox or one message
    that has moved since the proposal was drawn up should cost that message
    and nothing else — the remaining decisions are still acted on, and the
    count comes back for the caller to report.
    """
    moved = 0
    failed = 0
    for decision in decisions:
        if not decision.accepted:
            continue
        if decision.folder is None and not decision.is_delete:
            # Nothing to file it into. A *delete* needs no folder — binning
            # mail the classifier could not place is the main use for it.
            continue
        message_id = decision.proposal.message.rowid
        # Which account this message came from, read from the message itself
        # rather than passed in: a run covers several sources at once, and
        # only the message knows which one it belongs to.
        prefix = account_prefix(decision.proposal.message.mailbox_url)
        source = config.source_for(prefix)
        if source is None:
            # An account that is not configured as a source. Never guess: a
            # wrong source account sends the move looking in the wrong
            # mailbox, and the journal would record the message as having
            # come from somewhere it did not — which undo would then trust.
            failed += 1
            continue
        # A delete is a move to the Trash and nothing more: same journal entry,
        # same durable-key requirement, same undo. Recording the Trash as the
        # destination is what lets undo_run put it back without knowing a
        # delete ever happened.
        #
        # A bin stays in its own account whilst a filing goes to the filing
        # tree: a bin is not a filing destination, so binning never crosses
        # accounts.
        if decision.is_delete:
            destination = source.trash
            target_account = source.name
        else:
            destination = decision.folder
            target_account = config.filing_account
        entry = JournalEntry(
            message_id=message_id,
            subject=decision.proposal.message.subject,
            from_folder=source.inbox,
            to_folder=destination,
            status="planned",
            # The two ends differ whenever a message is filed out of one
            # account into the filing tree of another. Undo reads both.
            from_account=source.name,
            to_account=target_account,
        )
        try:
            entry.message_key = mail.message_key(message_id, source.inbox, source.name)
        except MailError:
            # Reading the key failed outright (Mail closed, message already
            # gone). Record the attempt so the message is visible in the
            # journal rather than absent from it, and move on.
            entry.status = "failed"
            journal.record(entry)
            failed += 1
            continue
        if not entry.message_key:
            entry.status = "failed"
            journal.record(entry)
            failed += 1
            continue
        journal.record(entry)
        try:
            mail.move_message(
                message_id,
                entry.to_folder,
                target_account,
                source_folder=source.inbox,
                message_key=entry.message_key,
                source_account=source.name,
            )
        except MailError:
            entry.status = "failed"
            journal.record(entry)
            failed += 1
            continue
        entry.status = "moved"
        journal.record(entry)
        moved += 1
    return moved, failed
