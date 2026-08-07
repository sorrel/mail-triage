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

Candidates are the union of senders with mail in an inbox and senders with
mail in the bin. Drawing them from the inbox alone was the first attempt and
it was wrong in the worst direction: a well-triaged inbox holds almost nothing
from the senders most worth leaving, precisely because they get binned on
sight. A live dry run on 5 August 2026 found two candidates, and the strongest
of them surfaced only because a single message happened to still be sitting in
the inbox.

The ``List-Unsubscribe`` header is not in the database, so it is read from a
message Mail can still find — from the inbox where possible, otherwise from
the bin, which is why ``message_headers`` takes a mailbox. A message that has
been purged between the snapshot and the fetch simply drops out.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, unquote

from mail_triage.config import Config, Source
from mail_triage.corpus import normalise_sender
from mail_triage.deletion import SECONDS_PER_DAY, build_deletion_index
from mail_triage.envelope import MessageRow
from mail_triage.folders import folder_path
from mail_triage.mail_app import MailError, MailInterface

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
    # What to put in the request. Not decoration: providers match the token
    # carried here against the subscription, so the wrong one is a rejected
    # request. See ``parse_list_unsubscribe``.
    subject: str = "unsubscribe"
    body: str = "unsubscribe"

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


@dataclass(frozen=True)
class UnsubscribeTarget:
    """Where an unsubscribe request goes, and what it must say."""

    method: str  # mailto | http
    target: str
    subject: str = "unsubscribe"
    body: str = "unsubscribe"


def parse_list_unsubscribe(header: str) -> UnsubscribeTarget | None:
    """Extract a target from a List-Unsubscribe header, preferring mailto.

    **A mailto URL's parameters are part of the request, not decoration.**
    The first live unsubscribe (6 August 2026) discarded them and bounced:
    ``554 Message rejected: The unsubscribe request has invalid form``. Its
    header was ``<mailto:unsubscribe-ENG@…?subject=hmv-prod/unsub/CgxnVLz…>``
    — the subject *is* the subscriber token. A request without it identifies
    nobody, which is exactly what the provider said.

    Parameters are percent-decoded per RFC 6068. ``subject`` and ``body``
    default to the word "unsubscribe" when the URL carries neither, which is
    the convention for the plain ``<mailto:leave@list>`` form.
    """
    targets = _TARGET.findall(header or "")
    for target in targets:
        if target.casefold().startswith("mailto:"):
            address, _, query = target[len("mailto:") :].partition("?")
            # keep_blank_values: a deliberate "?subject=" with nothing after
            # it is the sender asking for an empty subject, not an absent one.
            fields = dict(parse_qsl(query, keep_blank_values=True))
            return UnsubscribeTarget(
                method="mailto",
                target=unquote(address),
                subject=fields.get("subject", "unsubscribe"),
                body=fields.get("body", "unsubscribe"),
            )
    for target in targets:
        if target.casefold().startswith("http"):
            # An http target's query string belongs to the URL; splitting it
            # off the way a mailto's is split would break the link.
            return UnsubscribeTarget(method="http", target=target)
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
    pairs: list[tuple[UnsubscribeOption, "_Exemplar"]],
) -> list[tuple[UnsubscribeOption, "_Exemplar"]]:
    """``rank_candidates`` over (option, exemplar) pairs, same order."""
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


def _folder_url(reader, source: Source, folder: str) -> str | None:
    for url in reader.mailbox_urls():
        if url.startswith(source.prefix) and folder_path(url).casefold() == folder.casefold():
            return url
    return None


@dataclass(frozen=True)
class _Exemplar:
    """Where to read one sender's ``List-Unsubscribe`` header from.

    A mailbox and account are carried, not just an id: a binned sender's only
    remaining message is in the Trash, and Mail's unified ``inbox`` query
    cannot see it.
    """

    message_id: int
    mailbox: str | None
    account: str | None


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
    if now is None:
        now = int(time.time())
    cutoff = now - config.deletion_window_days * SECONDS_PER_DAY
    # Each entry pairs a fully-counted option with the message its header will
    # be fetched from. The counts are complete before any fetching, which is
    # what lets the ranking decide who is worth a round trip.
    provisional: list[tuple[UnsubscribeOption, _Exemplar]] = []
    for source in config.sources:
        inbox_url = _folder_url(reader, source, source.inbox)
        messages = list(reader.inbox_messages(inbox_url)) if inbox_url else []
        deletions = build_deletion_index(reader, config, source, now=now)

        # One message per sender to read the header from, preferring one still
        # in the inbox: the Trash purges on a rolling window, so a message
        # sitting in it is the likelier of the two to have gone by the time
        # the fetch happens.
        exemplars: dict[str, _Exemplar] = {}
        for message in messages:
            exemplars.setdefault(
                normalise_sender(message.sender),
                _Exemplar(message.rowid, source.inbox, source.name),
            )
        trash_url = _folder_url(reader, source, source.trash)
        if trash_url:
            for message in reader.messages_in_mailbox(trash_url):
                if not message.date_sent or message.date_sent < cutoff:
                    continue
                exemplars.setdefault(
                    normalise_sender(message.sender),
                    _Exemplar(message.rowid, source.trash, source.name),
                )

        # Senders are the union of "has inbox mail" and "has binned mail".
        # Inbox-only would hide the best candidates of all: a sender you bin
        # on sight leaves nothing in the inbox to be found by.
        inbox_counts = tally_senders(messages)
        senders = set(inbox_counts) | {
            sender for sender, stats in deletions.items() if stats.deleted
        }
        for sender in senders:
            exemplar = exemplars.get(sender)
            if exemplar is None:
                # Counted as deleted from a folder that is not this source's
                # Trash (the "Deleted*" patterns cover a few), so there is no
                # message we know how to address. No header, no candidate.
                continue
            count, unread = inbox_counts.get(sender, (0, 0))
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
                    exemplar,
                )
            )
    provisional = rank_candidates_with_ids(provisional)

    options: list[UnsubscribeOption] = []
    for option, exemplar in provisional[: max(limit, 0)]:
        try:
            headers = mail.message_headers(
                exemplar.message_id, exemplar.mailbox, exemplar.account
            )
        except MailError:
            # The Trash purges, and messages move. A candidate we can no
            # longer read a header from is simply not a candidate; it is not
            # a reason to abandon the run.
            continue
        parsed = parse_list_unsubscribe(headers.get("List-Unsubscribe", ""))
        if parsed is None:
            continue
        options.append(
            replace(
                option,
                method=parsed.method,
                target=parsed.target,
                subject=parsed.subject,
                body=parsed.body,
            )
        )
    return rank_candidates(options)


class SelectionError(ValueError):
    """What was typed at the "which of these?" prompt was not a selection."""


def parse_selection(typed: str, count: int) -> list[int]:
    """Turn "1, 3-5" into [1, 3, 4, 5], one-based, sorted, deduplicated.

    Anything unrecognised raises rather than being skipped. A selection is a
    list of things to unsubscribe from: quietly dropping a number the user
    typed, or quietly keeping one off the end, both act on a set they did not
    choose.
    """
    if not typed or not typed.strip():
        return []
    chosen: set[int] = set()
    for part in typed.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            first, _, last = part.partition("-")
            try:
                start, end = int(first.strip()), int(last.strip())
            except ValueError:
                raise SelectionError(f"{part!r} is not a number or a range.") from None
            if start > end:
                raise SelectionError(f"{part!r} runs backwards.")
            chosen.update(range(start, end + 1))
        else:
            try:
                chosen.add(int(part))
            except ValueError:
                raise SelectionError(f"{part!r} is not a number or a range.") from None
    off_the_end = [number for number in sorted(chosen) if number < 1 or number > count]
    if off_the_end:
        raise SelectionError(
            f"There is no {off_the_end[0]} in the list — the numbers run from 1 and {count}."
        )
    return sorted(chosen)


def render_candidates(options: list[UnsubscribeOption]) -> str:
    """The whole list, numbered, to choose from.

    Widths are measured with ``display_width`` rather than ``len``: an emoji
    in a sender's display name occupies two terminal columns and would skew
    every row after it.
    """
    from mail_triage.review import display_width

    rows = []
    for number, option in enumerate(options, start=1):
        share = f"{round(option.ignored_share * 100)}%"
        counts = f"{option.deleted_count} binned, {option.unread_count} unread"
        note = "mailto" if option.method == "mailto" else "http — open in a browser yourself"
        rows.append((str(number), option.sender, option.account, counts, share, note))

    widths = [
        max(display_width(row[column]) for row in rows) for column in range(len(rows[0]))
    ] if rows else []
    lines = []
    for row in rows:
        padded = [
            value + " " * (widths[column] - display_width(value))
            for column, value in enumerate(row)
        ]
        lines.append(" " + "  ".join(padded).rstrip())
    return "\n".join(lines)


def send_unsubscribe(option: UnsubscribeOption, mail: MailInterface) -> None:
    """Send the unsubscribe request. Only mailto targets are supported."""
    if option.method != "mailto":
        raise ValueError(
            f"Cannot send to a {option.method} target; only mailto unsubscribe is supported."
        )
    if not _ADDRESS.match(option.target):
        raise ValueError(f"Refusing to send: {option.target!r} is not an email address.")
    mail.send_mail(option.target, option.subject, option.body)
