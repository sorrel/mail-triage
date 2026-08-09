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
from mail_triage.mail_app import MailError, MailInterface, MessageNotFoundError
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
        if not _left_the_inbox(entry, mail, source):
            entry.status = "failed"
            journal.record(entry)
            failed += 1
            continue
        entry.status = "moved"
        journal.record(entry)
        moved += 1
    return moved, failed


def _left_the_inbox(entry: JournalEntry, mail: MailInterface, source) -> bool:
    """Clear the source inbox's hold on the message, then say whether it went.

    **A move that reports success is not a message that left the inbox.** A
    Gmail inbox is a label rather than a mailbox, so a cross-account move
    copies the message to the filing account and leaves the label untouched:
    Mail returns happily, the journal says "moved", and the message is still
    sitting in the inbox to be filed again next run. Measured on a live
    mailbox on 9 August 2026 — four attempts on one newsletter left three
    copies in the destination and the original in the Gmail inbox throughout.

    Where the source names an ``archive``, moving the leftover there is what
    removes the label — Gmail's own word for it, and a move *within* the
    account, which is the boundary that makes it work.

    **The archive runs on every crossing, not only when Mail says it is
    needed.** It used to be conditional on asking Mail whether the message
    was still in the inbox, which put the unreliable reading in charge of the
    repair. On 9 August 2026 that reading came back "already gone" — Mail
    answering from its own optimistic local state, before the server had
    confirmed — so the label was never cleared, the journal recorded "moved",
    and the label was back fifteen minutes later. Four runs, four copies.
    Asking the component that just reported success whether it really
    succeeded is not an independent check; it is the same claim twice.

    So the question is no longer asked before acting, only after, and the
    answer is a report rather than a decision.

    A source with no ``archive`` has nothing to clear the label with, so for
    it the check is the whole story — which is right for the accounts that
    move properly (Exchange, iCloud), where the message has genuinely gone
    and there is nothing to repair. Failing to verify still counts as *not*
    left: an unproven move is reported as a failure, never as a success.
    """
    if not entry.message_key:
        return True
    if entry.to_account != entry.from_account and source.archive:
        try:
            mail.move_message(
                0,
                source.archive,
                source.name,
                source_folder=source.inbox,
                message_key=entry.message_key,
                source_account=source.name,
            )
        except MessageNotFoundError:
            # Genuinely gone from the inbox already — an account whose moves
            # behave, or a label that cleared itself. Nothing to clear.
            pass
        except MailError:
            return False
    try:
        return not mail.message_exists(entry.message_key, source.inbox, source.name)
    except MailError:
        return False
