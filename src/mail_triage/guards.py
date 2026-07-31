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


@dataclass(frozen=True)
class Veto:
    """Why a message must stay in the inbox despite a filing proposal.

    ``reason`` is written for the person reading the proposal table, not for
    a log — "you flagged this", not a rule name.
    """

    reason: str


def _looks_no_reply(sender: str) -> bool:
    normalised = _normalise_local_part(sender.casefold())
    return any(pattern in normalised for pattern in _NO_REPLY_PATTERNS)


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
    return "List-Unsubscribe" in headers


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
