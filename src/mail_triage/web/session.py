"""One run's state, held in memory for the life of the process.

Proposals are addressed by an opaque id rather than by their database rowid.
Two reasons, and the second is the important one: a rowid is not stable
across a move (see ``execute``), and an id the page cannot guess means a
stale tab — or any other page that somehow reached us — cannot name a
message we did not offer it.

Nothing here is persisted. The ids die with the process, as the token does.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from mail_triage.model.classify import Proposal


@dataclass
class Session:
    """The proposals on offer, and what has already been acted on."""

    proposals: list[Proposal]
    run_id: str | None = None
    entries: dict[str, Proposal] = field(init=False)

    def __post_init__(self) -> None:
        self.entries = {secrets.token_urlsafe(9): item for item in self.proposals}
        self._applied: set[str] = set()

    def get(self, identifier: str) -> Proposal | None:
        return self.entries.get(identifier)

    def mark_applied(self, identifiers: list[str]) -> None:
        self._applied.update(identifiers)

    def is_applied(self, identifier: str) -> bool:
        return identifier in self._applied

    def unapplied(self, identifiers: list[str]) -> list[str]:
        """Those not yet acted on, in the order given.

        A message moves once. Without this a double-click, a re-posted
        request or an over-eager retry files the same mail twice, and the
        second move is the one undo cannot reason about.
        """
        return [item for item in identifiers if item not in self._applied]
