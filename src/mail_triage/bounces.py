"""Did the unsubscribe request actually land? Usually you cannot tell. Sometimes you can.

The first live send (6 August 2026) reported "sent" and was rejected 18
seconds later — ``554 Message rejected: The unsubscribe request has invalid
form`` — as a bounce from ``mailer-daemon`` that nothing was watching for.
This module is what watches.

**Every rule here is an exact match against a string the tool generated
itself**: a recipient address it wrote to, or a subject token it sent. That
constraint is the whole design. A fuzzy match — nearest subject, nearest
time — would attribute a bounce to a request that was fine, which is a
worse outcome than the silence it replaces, because it is silence that
sounds like information.

What this module deliberately cannot tell you is *why* a request bounced.
The SMTP diagnostic lives in the delivery report's ``message/delivery-status``
body part, and mail-triage does not read message bodies. It reports which
request bounced and leaves the reason to be read in Mail.

Nothing here does any I/O: it takes rows and header dictionaries and returns
values. That is what lets the matching rules — the part where a mistake is
expensive — be tested exhaustively without a mailbox anywhere near them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mail_triage.envelope import MessageRow
from mail_triage.layout import display_width, pad
from mail_triage.sends import SentRequest

# Bounces arrive from MAILER-DAEMON@whatever-relay, and the domain is
# unpredictable whilst the local part has been fixed by convention for
# decades. Matching the local part alone is therefore both wider and safer
# than any domain rule would be.
BOUNCE_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster"})

# The window either side of the batch. Five minutes early allows for a relay
# whose clock is behind ours; a day late is a sanity ceiling, not a claim
# that a bounce takes a day. The one measurement we have is 18 seconds.
SKEW_SECONDS = 300
WINDOW_SECONDS = 86_400

# The subject used when a sender's mailto: URL carries no parameters. It is
# never matched on: it appears in a large fraction of all marketing mail,
# so as a substring test it would match almost anything.
DEFAULT_SUBJECT = "unsubscribe"

# A token shorter than this is excluded from subject matching for the same
# reason DEFAULT_SUBJECT is — a short string is not distinctive enough for a
# substring test to mean anything.
MIN_TOKEN_LENGTH = 8


@dataclass(frozen=True)
class Bounce:
    """A delivery report, and the request it belongs to if that is knowable.

    ``request is None`` is the point of this type: it makes "a bounce I
    cannot account for" a state the renderer has to deal with, rather than
    one the matcher can quietly drop to make the run look cleaner.
    """

    rowid: int
    subject: str
    received_at: int
    failed_recipient: str | None = None
    request: SentRequest | None = None


def is_bounce_sender(sender: str) -> bool:
    """Does this address belong to a bounce daemon?"""
    local_part, at, _ = sender.partition("@")
    return bool(at) and local_part.casefold() in BOUNCE_LOCAL_PARTS


def candidate_rows(
    messages: Iterable[MessageRow], batch: list[SentRequest]
) -> list[MessageRow]:
    """Messages that could be bounces for this batch, cheaply.

    This runs against the snapshot with no AppleScript at all, because a
    header fetch costs the better part of a second and an inbox holds
    thousands of messages. It usually leaves between nought and three.
    """
    if not batch:
        return []
    first_send = min(request.sent_at for request in batch)
    earliest = first_send - SKEW_SECONDS
    latest = first_send + WINDOW_SECONDS
    return [
        message
        for message in messages
        if earliest <= message.date_received <= latest and is_bounce_sender(message.sender)
    ]


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup — Mail's casing is not guaranteed."""
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def is_delivery_report(headers: Mapping[str, str]) -> bool:
    """Is this actually a DSN, rather than a human called postmaster?"""
    content_type = _header(headers, "Content-Type").casefold()
    if "report-type=delivery-status" in content_type:
        return True
    return _header(headers, "Auto-Submitted").casefold().startswith("auto-replied")


def failed_recipients(headers: Mapping[str, str]) -> list[str]:
    """Addresses from ``X-Failed-Recipients``, case-folded and stripped."""
    raw = _header(headers, "X-Failed-Recipients")
    return [part.strip().casefold() for part in raw.split(",") if part.strip()]


def _matches_by_subject(request: SentRequest, subject: str) -> bool:
    token = request.subject.strip()
    if token.casefold() == DEFAULT_SUBJECT or len(token) < MIN_TOKEN_LENGTH:
        return False
    return token.casefold() in subject.casefold()


def attribute(
    pairs: list[tuple[MessageRow, Mapping[str, str]]], batch: list[SentRequest]
) -> list[Bounce]:
    """Turn (row, headers) pairs into bounces, attributed where possible.

    Rows whose headers say they are not delivery reports are dropped
    entirely — they were only ever candidates because of the sender's local
    part, and a person called postmaster is not a bounce.

    A request is claimed at most once: two lists can share an unsubscribe
    address, and letting one bounce satisfy both would report a failure that
    was never observed.
    """
    bounces: list[Bounce] = []
    unclaimed = list(batch)
    for row, headers in pairs:
        if not is_delivery_report(headers):
            continue
        recipients = failed_recipients(headers)
        matched: SentRequest | None = None
        for request in unclaimed:
            if request.to_address.casefold() in recipients:
                matched = request
                break
        if matched is None:
            for request in unclaimed:
                if _matches_by_subject(request, row.subject):
                    matched = request
                    break
        if matched is not None:
            unclaimed.remove(matched)
        bounces.append(
            Bounce(
                rowid=row.rowid,
                subject=row.subject,
                received_at=row.date_received,
                failed_recipient=recipients[0] if recipients else None,
                request=matched,
            )
        )
    return bounces


def render_report(batch: list[SentRequest], bounces: list[Bounce], batch_id: str) -> str:
    """One batch's outcome, as far as it can honestly be known.

    A request with no bounce is reported as "no bounce seen", never as
    delivered or confirmed. At this distance a silently discarded request is
    indistinguishable from an accepted one, and a tool that overclaims here
    is the bug being fixed rather than the fix.

    Widths come from ``display_width``, not ``len``: an emoji in a subject
    line occupies two terminal columns and would skew every row after it.
    """
    if not batch:
        return "That batch recorded no sent requests."

    account = batch[0].from_account or "an unidentified account"
    # Keyed by identity, not equality: two requests to the same list in one
    # batch are equal as values, and a dict keyed on the value would let one
    # bounce appear against both. ``attribute`` hands back the very objects
    # it was given, so identity is exactly the right key here.
    attributed = {
        id(bounce.request): bounce for bounce in bounces if bounce.request is not None
    }
    lines = [
        f"Batch {batch_id}, {len(batch)} "
        f"{'request' if len(batch) == 1 else 'requests'} sent from {account}.",
        "",
    ]

    rows = []
    for request in batch:
        bounce = attributed.get(id(request))
        if bounce is None:
            rows.append((request.sender, request.to_address, "no bounce seen", ""))
        else:
            rows.append((request.sender, request.to_address, "bounced", f'"{bounce.subject}"'))
    widths = [max(display_width(row[column]) for row in rows) for column in range(3)]
    for row in rows:
        padded = [pad(value, widths[column]) for column, value in enumerate(row[:3])]
        lines.append("  " + "  ".join(padded + [row[3]]).rstrip())

    orphans = [bounce for bounce in bounces if bounce.request is None]
    if orphans:
        lines.append("")
        lines.append(
            f"{len(orphans)} unattributed "
            f"{'bounce' if len(orphans) == 1 else 'bounces'} arrived in the window:"
        )
        for bounce in orphans:
            lines.append(f'  "{bounce.subject}" — read it yourself.')

    lines.append("")
    if any(bounce.request is not None for bounce in bounces):
        lines.append(
            "A bounce names the reason in its body, which this tool does not read."
        )
    lines.append(
        '"No bounce seen" is not confirmation: a request can be accepted and ignored.'
    )
    return "\n".join(lines)
