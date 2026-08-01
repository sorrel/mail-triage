"""Hard veto: mail awaiting a reply or action must never be filed away.

The user, 26 July 2026: "If anything requires me to do something or needs a
reply they mustn't be filed away unless that has happened." Two conditions,
both his choice, and nothing else:

1. He flagged it — an explicit do-not-file marker, free to check.
2. A human wrote it to him — i.e. it is not bulk mail. Bulk means a
   ``List-Unsubscribe`` header is present, or the sender looks like a
   no-reply address.

Unread status is deliberately excluded — clearing the unread pile is the
point of the tool, not a reason to hold mail back.

A veto overrides confidence entirely: it is checked after classification has
already decided a message would be filed, and it wins regardless of how
confident that decision was.
"""

from __future__ import annotations

from dataclasses import dataclass

from mail_triage.envelope import MessageRow

# Strong enough on their own to call a sender bulk without fetching headers —
# the whole point being to skip the AppleScript round trip for the obvious
# cases. Matched as a substring against the lower-cased sender address, so
# both "no-reply@shop.example" and "Shop <no-reply@shop.example>" match.
#
# Written without separators because the local part has its own stripped out
# before matching: senders spell the same address "no-reply@", "noreply@",
# "do_not_reply@" and "do.not.reply@" interchangeably, and enumerating the
# permutations means the one nobody thought of goes unmatched. An
# underscored "do_not_reply@" was exactly that — the sender adds no
# List-Unsubscribe either, so a bulk notification was held back as though a
# person had written it.
_NO_REPLY_PATTERNS = ("noreply@", "donotreply@", "notifications@", "bounce@")

# Separators stripped from the local part before matching. Only the local
# part: a domain is not where these addresses vary, and rewriting it could
# only invent matches that were never there.
_LOCAL_PART_SEPARATORS = str.maketrans("", "", "-_.")

# The same idea applied to a display name, where spaces are a separator too:
# "Do Not Reply <mailer@service.example>" says plainly that nobody reads
# replies whilst the address itself gives no clue. Matched without the "@"
# anchor, since a display name has no address in it.
_DISPLAY_NAME_SEPARATORS = str.maketrans("", "", "-_. ")
_NO_REPLY_DISPLAY_NAMES = ("noreply", "donotreply")

# Headers that declare a message automated. ``Precedence`` is the old
# convention and ``Auto-Submitted`` (RFC 3834) the standardised one; both
# appear on service notifications that carry no List-Unsubscribe, because
# there is nothing to unsubscribe *from* — a sign-in alert is not a
# newsletter. Without these such a message has no bulk evidence at all and
# is held back as though a person had written it.
_BULK_PRECEDENCE_VALUES = ("bulk", "list", "junk")


@dataclass(frozen=True)
class Veto:
    """Why a message must stay in the inbox despite a filing proposal.

    ``reason`` is written for the person reading the proposal table, not for
    a log — "you flagged this", not a rule name.
    """

    reason: str


def _looks_no_reply(sender: str) -> bool:
    normalised = _normalise_local_part(sender.casefold())
    if any(pattern in normalised for pattern in _NO_REPLY_PATTERNS):
        return True
    return _display_name_says_no_reply(sender)


def _display_name_says_no_reply(sender: str) -> bool:
    """Whether the part before ``<`` names the sender as a no-reply address.

    Only applied when there *is* a display name: without the angle brackets
    the whole string is an address, already covered above, and matching the
    unanchored words against it would fire on a local part like
    "noreplyneeded" in a way the ``@``-anchored patterns deliberately do not.
    """
    display, bracket, _ = sender.casefold().partition("<")
    if not bracket:
        return False
    squashed = display.translate(_DISPLAY_NAME_SEPARATORS)
    return any(name in squashed for name in _NO_REPLY_DISPLAY_NAMES)


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    Header names are case-insensitive by RFC 5322 and senders genuinely vary
    ("List-unsubscribe" is common), so an exact-match lookup would miss a
    header that is plainly present.
    """
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _headers_say_automated(headers: dict[str, str]) -> bool:
    """Whether the headers declare the message machine-generated."""
    precedence = _header(headers, "Precedence")
    if precedence is not None and precedence.strip().casefold() in _BULK_PRECEDENCE_VALUES:
        return True
    auto = _header(headers, "Auto-Submitted")
    # RFC 3834: "no" is the explicit way of saying a human sent it, so the
    # header's presence alone must not count — that would invert its meaning.
    return auto is not None and auto.strip().casefold() != "no"


def _normalise_local_part(sender: str) -> str:
    """Strip separators from everything before the last ``@``.

    ``rpartition`` rather than ``partition``: a display-name form like
    "Shop <no-reply@shop.example>" has no other ``@``, but should one appear
    in a quoted local part the address's own ``@`` is the last of them.
    Senders with no ``@`` at all are returned unchanged rather than treated
    as one long local part.
    """
    local, at, domain = sender.rpartition("@")
    if not at:
        return sender
    return local.translate(_LOCAL_PART_SEPARATORS) + at + domain


def is_bulk(sender: str, headers: dict[str, str] | None) -> bool:
    """Whether this message is bulk mail rather than person-to-person.

    A no-reply-style address is decisive on its own, checked first so the
    (expensive) headers are never required for the obvious case. Otherwise,
    bulk is defined by the presence of a ``List-Unsubscribe`` header — an
    ordinary-looking address proves nothing by itself, so with headers
    unavailable (``None``) this returns ``False`` rather than assuming bulk.
    Assuming bulk from silence would be the failure mode this guard exists
    to prevent.
    """
    if _looks_no_reply(sender):
        return True
    if headers is None:
        return False
    if _header(headers, "List-Unsubscribe") is not None:
        return True
    return _headers_say_automated(headers)


def needs_attention(message: MessageRow, headers: dict[str, str] | None) -> Veto | None:
    """Decide whether ``message`` must stay in the inbox regardless of confidence.

    ``headers`` is the message's raw headers, or ``None`` if they were never
    fetched (a no-reply sender made the fetch unnecessary) or could not be
    fetched (Mail not running, an AppleScript error, a timeout). Both are
    passed the same way; this function still resolves them correctly because
    the no-reply case is already settled by ``is_bulk`` before ``headers`` is
    consulted again.

    An unavailable signal is not permission to file: if the sender heuristic
    is inconclusive and headers could not be obtained, this fails safe by
    vetoing rather than filing.
    """
    if message.flagged:
        return Veto("you flagged this")
    if is_bulk(message.sender, headers):
        return None
    if headers is None:
        return Veto("could not check whether this is bulk mail — keeping it safe")
    return Veto("looks personal, may need a reply")
