"""Dataclasses to JSON-safe dicts.

Deliberately narrow. The page is given what it must display and the opaque
id it acts with, and nothing else — no rowid, no mailbox URL, no account
prefix. That is not tidiness: a rowid is the handle that moves mail, and
there is no reason for one to exist in a browser tab.

Message bodies are not read anywhere in this project and are not read here.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from mail_triage.folders import account_prefix
from mail_triage.model.classify import Proposal
from mail_triage.unsubscribe import UnsubscribeOption
from mail_triage.web.session import Session


def is_safe_target(target: str) -> bool:
    """Whether an unsubscribe URL may be handed to the browser.

    ``parse_list_unsubscribe`` selects an http target with
    ``startswith("http")``, which accepts plaintext ``http://`` and, read
    literally, anything merely beginning with those four letters. The value
    comes from a header the *sender* wrote, and the page puts it in both an
    iframe ``src`` and an anchor ``href`` — and an anchor is covered by no
    content policy at all.

    So the scheme is checked here rather than assumed: exactly ``https``,
    with a host. The page checks again before assigning either attribute.
    Two layers, because they fail differently — this one is the correctness
    check, the client-side one is what survives somebody loosening the CSP.
    """
    try:
        parts = urlsplit(target)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc)


def _proposal_payload(identifier: str, proposal: Proposal, accounts: dict[str, str]) -> dict:
    message = proposal.message
    return {
        "id": identifier,
        "sender": message.sender,
        "subject": message.subject,
        "account": accounts.get(account_prefix(message.mailbox_url), ""),
        "read": message.read,
        "flagged": message.flagged,
        "folder": proposal.folder,
        "confidence": proposal.confidence,
        "reason": proposal.reason,
        "stage": proposal.stage,
        "veto": proposal.veto,
        "veto_kind": proposal.veto_kind,
        "held_folder": proposal.held_folder,
        "action": proposal.action,
    }


def proposals_payload(session: Session, accounts: dict[str, str]) -> dict:
    return {
        "proposals": [
            _proposal_payload(identifier, proposal, accounts)
            for identifier, proposal in session.entries.items()
        ]
    }


def outcome_payload(moved: int, failed: int, run_id: str) -> dict:
    return {"moved": moved, "failed": failed, "run_id": run_id}


def candidates_payload(options: list[UnsubscribeOption]) -> dict:
    """The ranked candidates, with any unframeable target withheld.

    An http target that is not plain ``https`` is reported with a null
    target and ``"blocked"`` as its method, so the page can say so rather
    than silently offering a button that does nothing.
    """
    rows = []
    for option in options:
        safe = option.method == "mailto" or is_safe_target(option.target)
        rows.append(
            {
                "sender": option.sender,
                "domain": option.domain,
                "account": option.account,
                "method": option.method if safe else "blocked",
                "target": option.target if safe and option.method != "mailto" else None,
                "deleted_count": option.deleted_count,
                "unread_count": option.unread_count,
                "ignored_share": round(option.ignored_share, 3),
            }
        )
    return {"candidates": rows}
