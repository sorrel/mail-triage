"""Senders the user has vouched for as never person-to-person.

The reply guard answers "did a human write this to me?" from the message
alone: a no-reply address, a minted token local part, ``List-Unsubscribe``,
``Precedence``, ``Auto-Submitted``. Some senders it cannot answer for. A
marketplace's order confirmations arrive from an ordinary-word address
carrying none of those, so the guard fails safe and holds them for a reply
nobody wants — for ever, since nothing about the next one will differ.

The obvious general fix was measured and disproved on the live mailbox, 9
August 2026. ``Feedback-Id`` — the ESP feedback-loop header — sits on exactly
those order confirmations, and equally on a heating firm's personal chase
about a heat pump enquiry, sent through their CRM. Both are addressed to the
user directly; neither carries any other bulk signal. A rule that files the
first files the second, which is precisely the harm the guard exists to
prevent. The distinction between "your order is confirmed" and "we have been
trying to reach you" is in the meaning, not the envelope.

So the evidence has to come from the user. A declaration here says: mail from
this address never awaits my reply. It is deliberately weaker than it sounds
— it lifts *only* the reply guard. Flagging still wins, the deletion veto
still applies, and where the mail goes is still the model's decision.
"""

from __future__ import annotations

import json
from pathlib import Path

from mail_triage.corpus import normalise_sender


class NeverPersonalError(Exception):
    """The never-personal file exists but cannot be trusted."""


def load_never_personal(path: Path) -> frozenset[str]:
    """Load the declared addresses, lower-cased.

    A missing file means nobody has been declared, which is the state before
    the first declaration and not an error.

    An unreadable one *is* an error, though it leans safe either way: an empty
    set holds more mail back, the opposite of ``rules.json``, where silence
    would file mail contrary to explicit instructions. It is raised because a
    declaration the user believes is in force but is not would leave the inbox
    behaving inexplicably.
    """
    if not path.exists():
        return frozenset()
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise NeverPersonalError(
            f"{path}: cannot parse the never-personal list at line {error.lineno}, "
            f"column {error.colno} ({error.msg}). Fix or delete the file — mail from "
            "these senders is being held for a reply while it is unreadable."
        ) from error
    if not isinstance(payload, list):
        raise NeverPersonalError(f"{path}: expected a list of sender addresses")
    return frozenset(normalise_sender(str(entry)) for entry in payload if entry)


def _require_address(sender: str) -> str:
    """The bare address, or refuse.

    ``normalise_sender`` returns "" for anything with no address in it. Storing
    that would be a declaration matching nothing, silently — so a typo is
    refused at the point it is made rather than discovered as mail that never
    stopped being held back.
    """
    address = normalise_sender(sender)
    if not address:
        raise NeverPersonalError(f"{sender!r} is not an email address")
    return address


def _save(path: Path, senders: frozenset[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(senders), indent=2) + "\n")


def declare_never_personal(path: Path, sender: str) -> bool:
    """Declare a sender never-personal. Returns whether this changed anything.

    Written immediately, as rules are, so an interrupted session keeps every
    declaration already made.
    """
    address = _require_address(sender)
    known = load_never_personal(path)
    if address in known:
        return False
    _save(path, known | {address})
    return True


def forget_never_personal(path: Path, sender: str) -> bool:
    """Undeclare a sender. Returns whether there was a declaration to remove."""
    address = _require_address(sender)
    known = load_never_personal(path)
    if address not in known:
        return False
    _save(path, known - {address})
    return True
