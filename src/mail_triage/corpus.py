"""Turn filing history into weighted training examples.

Filing history is evidence, not ground truth. the user's habits have changed and
some mail was filed carelessly. Recency weighting is the first defence: an
example decays exponentially with age, so recent habits dominate old ones.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.folders import folder_path, is_excluded, normalise_folder

SECONDS_PER_DAY = 86_400
_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+")


@dataclass(frozen=True)
class TrainingExample:
    """One historical filing decision, weighted by how much we should trust it."""

    sender: str
    domain: str
    subject: str
    folder: str
    weight: float
    year: int


def sender_domain(address: str) -> str:
    """Extract a lower-cased domain from an address or display-name form."""
    match = _ADDRESS.search(address or "")
    if not match:
        return ""
    return match.group(0).split("@", 1)[1].casefold()


def normalise_sender(address: str) -> str:
    """Extract the bare lower-cased address, discarding any display name."""
    match = _ADDRESS.search(address or "")
    return match.group(0).casefold() if match else ""


def recency_weight(date_sent: int, now: int, half_life_days: float) -> float:
    """Exponential decay: weight halves every ``half_life_days``."""
    age_days = max(0.0, (now - date_sent) / SECONDS_PER_DAY)
    return math.pow(0.5, age_days / half_life_days)


def build_corpus(
    rows: Iterable[MessageRow], config: Config, now: int | None = None
) -> list[TrainingExample]:
    """Build weighted training examples from historical messages.

    Messages outside the training accounts, in excluded folders, undated, or
    with no parseable sender contribute nothing.

    ``Deleted*``/``Trash`` stay in ``config.training_exclusions`` by default
    and are deliberately still excluded here on purpose (Task 11C): deleted
    mail must never contribute a *folder* prediction, however consistently a
    sender's earlier mail was filed before they started being deleted. That
    mail isn't discarded, though — ``mail_triage.deletion`` reads the same
    messages independently, as negative filing evidence rather than training
    data for a destination.
    """
    if now is None:
        now = int(time.time())
    prefixes = tuple(config.training_prefixes)
    examples: list[TrainingExample] = []
    for message in rows:
        if not message.date_sent:
            continue
        if not message.mailbox_url.startswith(prefixes):
            continue
        folder = folder_path(message.mailbox_url)
        if not folder or is_excluded(folder, config.training_exclusions):
            continue
        sender = normalise_sender(message.sender)
        if not sender:
            continue
        examples.append(
            TrainingExample(
                sender=sender,
                domain=sender_domain(message.sender),
                subject=message.subject,
                folder=normalise_folder(folder),
                weight=recency_weight(message.date_sent, now, config.half_life_days),
                year=time.gmtime(message.date_sent).tm_year,
            )
        )
    return examples
