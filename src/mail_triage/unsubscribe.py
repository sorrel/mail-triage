"""Suggest mailing lists worth leaving, and send the unsubscribe request.

This is the one part of mail-triage that sends mail. It never does so without
an explicit 'y' for that specific sender. HTTP one-click unsubscribe is
deliberately unsupported: it would mean arbitrary outbound requests to
addresses supplied by the sender.

**Deleted mail is the strongest evidence here** (the user's request, 5 August
2026). Unread-in-the-inbox only sees the mail you have not got round to;
the mail you actively binned has left the inbox altogether, and Mail marks
plenty of it read on the way past. ``deletion.build_deletion_index`` already
counts it per sender and per account for the filing veto, so this module
reuses that index rather than growing a second notion of "ignored". Counts
stay within each account, as they do there: a sender you bin in one account
and read in another is not being ignored.

Candidates are drawn from senders with mail *currently in an inbox*, because
the ``List-Unsubscribe`` header can only be fetched from a message Mail can
still find, and because a list you have finished binning is one that is still
sending. A sender who has gone quiet needs no unsubscribing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace

from mail_triage.config import Config, Source
from mail_triage.corpus import normalise_sender
from mail_triage.deletion import build_deletion_index
from mail_triage.envelope import MessageRow
from mail_triage.folders import folder_path
from mail_triage.mail_app import MailInterface

_TARGET = re.compile(r"<([^>]+)>")

# A conservative address shape for the one value we hand to Mail's sender.
# The target arrives in a header the sender wrote, so it is checked rather
# than trusted: no spaces, no control characters, exactly one @.
_ADDRESS = re.compile(r"^[^\s<>@,;\"\\]+@[^\s<>@,;\"\\]+\.[^\s<>@,;\"\\]+$")


@dataclass(frozen=True)
class UnsubscribeOption:
    sender: str
    domain: str
    method: str  # mailto | http
    target: str
    message_count: int
    unread_count: int
    deleted_count: int = 0
    account: str = ""

    @property
    def ignored_count(self) -> int:
        """Messages you did not engage with: left unread, or binned."""
        return self.unread_count + self.deleted_count

    @property
    def seen_count(self) -> int:
        """Everything this sender sent that we have evidence about."""
        return self.message_count + self.deleted_count

    @property
    def ignored_share(self) -> float:
        return self.ignored_count / self.seen_count if self.seen_count else 0.0


def parse_list_unsubscribe(header: str) -> tuple[str, str] | None:
    """Extract a target from a List-Unsubscribe header, preferring mailto."""
    targets = _TARGET.findall(header or "")
    for target in targets:
        if target.casefold().startswith("mailto:"):
            address = target[len("mailto:") :]
            return "mailto", address.split("?", 1)[0]
    for target in targets:
        if target.casefold().startswith("http"):
            return "http", target
    return None


def _rank_key(option: UnsubscribeOption) -> tuple[int, float, int]:
    return (option.ignored_count, option.ignored_share, option.seen_count)


def rank_candidates(options: list[UnsubscribeOption]) -> list[UnsubscribeOption]:
    """Most-ignored, highest-volume senders first.

    Ignored *count* leads rather than share, so that a list you have binned
    thirty times outranks a stranger whose single message you have not opened
    — a 100% share on a sample of one is not evidence of anything.
    """
    return sorted(options, key=_rank_key, reverse=True)


def rank_candidates_with_ids(
    pairs: list[tuple[UnsubscribeOption, int]],
) -> list[tuple[UnsubscribeOption, int]]:
    """``rank_candidates`` over (option, message id) pairs, same order."""
    return sorted(pairs, key=lambda pair: _rank_key(pair[0]), reverse=True)


def tally_senders(messages: list[MessageRow]) -> dict[str, tuple[int, int]]:
    """Return sender → (message_count, unread_count)."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for message in messages:
        sender = normalise_sender(message.sender)
        if not sender:
            continue
        totals[sender][0] += 1
        if not message.read:
            totals[sender][1] += 1
    return {sender: (counts[0], counts[1]) for sender, counts in totals.items()}


def _inbox_url(reader, source: Source) -> str | None:
    for url in reader.mailbox_urls():
        if url.startswith(source.prefix) and folder_path(url).casefold() == source.inbox.casefold():
            return url
    return None


def find_candidates(
    reader,
    config: Config,
    mail: MailInterface,
    limit: int = 20,
    now: int | None = None,
) -> list[UnsubscribeOption]:
    """The senders most worth leaving, across every configured source.

    ``limit`` caps the number of *header fetches*, not the number of results:
    each fetch is an AppleScript round trip costing the better part of a
    second, so the ranking on counts happens first and only the top slice is
    asked about. Senders whose mail carries no ``List-Unsubscribe`` header
    drop out at that point, which is why the returned list is usually shorter
    than the limit.
    """
    # Each entry pairs a fully-counted option with the message its header will
    # be fetched from. The counts are complete before any fetching, which is
    # what lets the ranking decide who is worth a round trip.
    provisional: list[tuple[UnsubscribeOption, int]] = []
    for source in config.sources:
        inbox_url = _inbox_url(reader, source)
        if inbox_url is None:
            continue
        messages = list(reader.inbox_messages(inbox_url))
        deletions = build_deletion_index(reader, config, source, now=now)
        # One message id per sender, to fetch that sender's header from.
        exemplar: dict[str, int] = {}
        for message in messages:
            exemplar.setdefault(normalise_sender(message.sender), message.rowid)
        for sender, (count, unread) in tally_senders(messages).items():
            stats = deletions.get(sender)
            provisional.append(
                (
                    UnsubscribeOption(
                        sender=sender,
                        domain=sender.rpartition("@")[2],
                        method="",
                        target="",
                        message_count=count,
                        unread_count=unread,
                        deleted_count=stats.deleted if stats else 0,
                        account=source.name,
                    ),
                    exemplar[sender],
                )
            )
    provisional = rank_candidates_with_ids(provisional)

    options: list[UnsubscribeOption] = []
    for option, message_id in provisional[: max(limit, 0)]:
        header = mail.message_headers(message_id).get("List-Unsubscribe", "")
        parsed = parse_list_unsubscribe(header)
        if parsed is None:
            continue
        method, target = parsed
        options.append(replace(option, method=method, target=target))
    return rank_candidates(options)


def send_unsubscribe(option: UnsubscribeOption, mail: MailInterface) -> None:
    """Send the unsubscribe request. Only mailto targets are supported."""
    if option.method != "mailto":
        raise ValueError(
            f"Cannot send to a {option.method} target; only mailto unsubscribe is supported."
        )
    if not _ADDRESS.match(option.target):
        raise ValueError(f"Refusing to send: {option.target!r} is not an email address.")
    mail.send_mail(option.target, "unsubscribe", "unsubscribe")
