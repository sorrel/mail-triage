"""Deletion as negative evidence.

The user, 26 July 2026: "sometimes I delete messages instead of filing them
away. Harder to see as the messages are gone. But it often happens."

Measured against real filing history, this was not marginal: 19 of the 23
proposals in the first dry run traced back to senders whose mail had
recently been deleted — mail that would have been filed at 0.82-0.88
confidence purely on the strength of filing history the sender's *current*
behaviour has superseded.

Two patterns, handled differently, per the user's decision:

- **Only deletes now** — nothing at all filed in the window, everything
  binned. Filing is vetoed outright; the sender is worth flagging as an
  unsubscribe candidate (the veto reason itself does that flagging, plainly,
  in the proposal summary).
- **Keeps some, bins some** — still proposed, but the bin-rate is carried
  alongside so the choice stays informed.

The Trash purges on a rolling window (roughly two months at the time this
was measured), so filed and deleted counts here are always drawn from the
*same* recent window, ``config.deletion_window_days`` back from ``now`` —
never lifetime filing history against a short deletion history. A sender
with ten years of filing and two recent months of deletions must read as
"only deletes now", not as a falsely balanced mixed sender — comparing
across mismatched windows is the single easiest way to get this wrong.

Deleted mail feeds this index only. It must never contribute a folder
prediction — ``corpus.py`` continues to exclude ``Deleted*``/``Trash`` from
training for that reason; this module reads the same messages independently,
for a different purpose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from mail_triage.config import Config, Source
from mail_triage.corpus import SECONDS_PER_DAY, normalise_sender
from mail_triage.folders import account_prefix, folder_path, is_excluded

# Folders that mean "this message was deleted". Deliberately separate from
# Config.training_exclusions, which also excludes INBOX, Junk, Sent, etc. —
# folders that are neither a filing nor a deletion and mustn't count as
# either side of this ratio.
_DELETED_FOLDER_PATTERNS = ["Deleted*", "Trash"]

# Folders that represent no filing decision at all: still in the inbox,
# junk, sent mail, drafts. Ignored entirely rather than counted as "filed".
#
# "Junk*" rather than "Junk": Exchange calls its junk folder "Junk Email",
# which the bare pattern missed — so every spam message counted as evidence
# that the sender's mail gets *filed*, inflating the filed side of the ratio
# and suppressing the veto that exists to catch senders who are only binned.
_IGNORED_FOLDER_PATTERNS = ["INBOX", "Junk*", "Spam", "Sent*", "Drafts*", "Outbox"]


@dataclass(frozen=True)
class DeletionStats:
    """How often one sender's recent mail was filed versus deleted."""

    filed: int
    deleted: int

    @property
    def delete_ratio(self) -> float:
        """Share of in-window mail that was deleted; 0.0 with no history at all."""
        total = self.filed + self.deleted
        if total == 0:
            return 0.0
        return self.deleted / total


def build_deletion_index(
    reader, config: Config, source: Source, now: int | None = None
) -> dict[str, DeletionStats]:
    """Count one source's filed vs deleted messages per sender, in one window.

    Scoped to a single account on purpose (the user's decision, 29 July 2026).
    Filing and binning habits differ between accounts, and a sender filed in
    one whilst binned in the other would produce a pooled ratio reflecting
    neither — so each source gets its own index and each message is judged
    against its own account's.

    Both counts are drawn from ``config.deletion_window_days`` back from
    ``now`` — the window boundary (exactly ``deletion_window_days`` days old)
    is included; anything older is not. Reads directly from ``reader``, not
    via ``build_corpus``, since deleted mail is deliberately excluded from
    that corpus and must stay excluded from folder training regardless of
    what happens here.
    """
    if now is None:
        now = int(time.time())
    cutoff = now - config.deletion_window_days * SECONDS_PER_DAY
    prefixes = (source.prefix,)
    ignored = _IGNORED_FOLDER_PATTERNS + list(source.ignore)
    counts: dict[str, list[int]] = {}
    for message in reader.all_messages():
        if not message.date_sent or message.date_sent < cutoff:
            continue
        if not message.mailbox_url.startswith(prefixes):
            continue
        folder = folder_path(message.mailbox_url)
        if not folder:
            continue
        sender = normalise_sender(message.sender)
        if not sender:
            continue
        # Deleted is tested *first*: a source's ignore patterns may also match
        # its own trash — Gmail's "[[]Gmail]*" covers "[Gmail]/Bin" — and
        # testing ignore first would silently discard every deletion it has.
        if folder.casefold() == source.trash.casefold() or is_excluded(
            folder, _DELETED_FOLDER_PATTERNS
        ):
            counts.setdefault(sender, [0, 0])[1] += 1
        elif is_excluded(folder, ignored):
            continue
        else:
            counts.setdefault(sender, [0, 0])[0] += 1
    return {sender: DeletionStats(filed=f, deleted=d) for sender, (f, d) in counts.items()}


class PerAccountDeletionIndex:
    """Several accounts' deletion indices, selected by the message's own account.

    The classifier asks for one sender's stats without knowing which account
    the message came from, so the account is resolved here, from the message's
    ``mailbox_url``. An account with no index of its own contributes no
    evidence and therefore no veto — the safe direction, since a missing index
    should never invent a reason to hold mail back.

    Kept separate from a plain ``dict`` rather than replacing it: a
    single-source run has exactly one index and gains nothing from the
    indirection.
    """

    def __init__(self, indices: dict[str, dict[str, DeletionStats]]) -> None:
        self.indices = indices

    def for_message(self, message) -> dict[str, DeletionStats]:
        return self.indices.get(account_prefix(message.mailbox_url), {})


def deletion_veto(stats: DeletionStats | None, config: Config) -> str | None:
    """Veto reason for a sender who is only being binned lately, or ``None``.

    Vetoes only once the delete ratio meets ``config.delete_veto_ratio``
    (default 1.0: nothing at all filed in the window). A sender with no
    in-window history at all — unknown, or simply quiet lately — is not
    vetoed; there is nothing to warn about. A mixed sender who still gets
    filed sometimes is left alone here too, on purpose: the user's decision was
    to keep proposing for those, just with the ratio visible, not to veto
    them.
    """
    if stats is None:
        return None
    total = stats.filed + stats.deleted
    if total == 0:
        return None
    if stats.delete_ratio >= config.delete_veto_ratio:
        return f"you have binned the last {stats.deleted} from this sender"
    return None
