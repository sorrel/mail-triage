"""Everything a triage run reads before it decides anything.

Gathering is kept apart from the command that uses it for one reason above
all: every figure here must come from **one** snapshot, opened once. Filing
history and deletion counts are compared against each other, so reading them
from two snapshots taken moments apart would silently break the guarantee
that they describe the same mailbox at the same instant.

Holding it in one function makes that structural rather than a convention to
remember — there is a single ``reader``, and it is closed before anything is
returned. Nothing here writes, and nothing here talks to Mail.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from mail_triage.asking import build_ranking_inputs
from mail_triage.config import Config, Source
from mail_triage.deletion import PerAccountDeletionIndex, build_deletion_index
from mail_triage.envelope import DEFAULT_DB_PATH, EnvelopeReader, MessageRow, snapshot_database
from mail_triage.folders import folder_path


class InputError(RuntimeError):
    """The mailbox does not look the way the configuration says it does.

    Raised rather than a ``ClickException`` so this module stays usable — and
    testable — away from the command line. ``cli`` translates it.
    """


@dataclass(frozen=True)
class TriageInputs:
    """One snapshot's worth of everything the classifier and the prompts need."""

    messages: list[MessageRow]
    deletion_index: PerAccountDeletionIndex
    # Per account prefix: that account's own mailboxes, folded for comparison.
    # A bin stays in the account it came from, so the filing account's folder
    # list cannot answer the pre-flight check for it.
    source_folders: dict[str, set[str]]
    # Candidate folders, from the filing account alone: there is one filing
    # tree and every source's mail goes into it.
    folders: list[str]
    attachments: dict[int, list[str]]
    yearly_counts: dict[str, int]
    billing_senders: set[str]


def _inbox_url(mailbox_urls: list[str], source: Source) -> str:
    """The URL of ``source``'s inbox, or raise saying which source is wrong."""
    for url in mailbox_urls:
        if url.startswith(source.prefix) and folder_path(url).casefold() == source.inbox.casefold():
            return url
    raise InputError(
        f"No mailbox '{source.inbox}' under {source.prefix} for source "
        f"'{source.name}'. Run 'mail-triage accounts' to check the prefix."
    )


def gather(
    config: Config, sources: list[Source], ask: bool, db_path: Path = DEFAULT_DB_PATH
) -> TriageInputs:
    """Read one snapshot and return everything the run needs from it.

    The folder list comes from the database rather than AppleScript: it
    preserves full nested paths ("Parent/Child") and real capitalisation,
    both of which AppleScript's flat leaf-name list would lose.

    ``ask`` is taken here rather than later because the two sender-level
    facts it needs cost a full scan of the message table, and an unattended
    run has nobody to ask.

    ``db_path`` is a parameter rather than a module lookup so a caller — or a
    test — can point the run at a different database explicitly.
    """
    with tempfile.TemporaryDirectory() as work:
        reader = EnvelopeReader(snapshot_database(db_path, Path(work)))
        try:
            mailbox_urls = reader.mailbox_urls()
            messages: list[MessageRow] = []
            indices: dict[str, dict] = {}
            source_folders: dict[str, set[str]] = {}
            for source in sources:
                # inbox_messages, not messages_in_mailbox: a Gmail inbox is a
                # label, so filtering on the mailbox URL alone finds nothing.
                messages.extend(reader.inbox_messages(_inbox_url(mailbox_urls, source)))
                # Built from this same open reader, not a second one: filing
                # and deletion counts must come from identical data or the
                # same-window guarantee is meaningless. One index per account,
                # because habits differ between them.
                indices[source.prefix] = build_deletion_index(reader, config, source)
                source_folders[source.prefix] = {
                    folder_path(url).casefold()
                    for url in mailbox_urls
                    if url.startswith(source.prefix) and folder_path(url)
                }
            folders = [
                folder_path(url)
                for url in mailbox_urls
                if url.startswith(config.filing_account_prefix) and folder_path(url)
            ]
            # Scoped to the inbox rather than the whole table: the invoice
            # guard only ever asks about a message it is about to file.
            attachments = reader.attachment_names(m.rowid for m in messages)
            # How often each sender writes, not how many of their messages are
            # in today's inbox; and which of them send bills, who are never
            # offered the bin answer. Both from a single scan.
            yearly_counts, billing_senders = (
                build_ranking_inputs(reader, config) if ask else ({}, set())
            )
        finally:
            reader.close()

    return TriageInputs(
        messages=messages,
        deletion_index=PerAccountDeletionIndex(indices),
        source_folders=source_folders,
        folders=folders,
        attachments=attachments,
        yearly_counts=yearly_counts,
        billing_senders=billing_senders,
    )
