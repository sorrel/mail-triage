"""Record where the user actually wanted a message to go.

History is evidence; a correction is instruction. Corrections are weighted far
higher than historical filings, which is how an old habit gets overridden
without editing thousands of past decisions.

Distinct from ``rules.py``, which answers the same question absolutely and for
every message a sender ever sends. A correction is one message's worth of
evidence, weighted heavily and then left to argue with the rest of the
history; a rule outranks the model entirely. Overriding a single proposal
should not silently commit the user to a rule, so the two stay separate.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mail_triage.config import Config
from mail_triage.corpus import TrainingExample, normalise_sender, sender_domain
from mail_triage.folders import normalise_folder


@dataclass
class Correction:
    """One message the user filed somewhere other than where it was proposed.

    ``rejected_folder`` is what the classifier had suggested, or None where it
    suggested nothing. It is recorded for the record — nothing trains on it
    yet — because "wrong in this particular way" is the raw material for
    measuring whether the model is improving.
    """

    sender: str
    domain: str
    subject: str
    chosen_folder: str
    rejected_folder: str | None
    recorded_at: int


def record_correction(correction: Correction, config: Config) -> None:
    """Append one correction. Append-only: the sequence is the evidence."""
    path: Path = config.corrections_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(asdict(correction)) + "\n")


def record_overrides(decisions: Iterable[Any], config: Config, now: int | None = None) -> int:
    """Record a correction for every decision whose folder the user typed.

    Only overrides: agreeing with a proposal teaches nothing the history does
    not already say, and a plain rejection names nowhere to file instead.

    Sender and domain are stored in the corpus's own normalised form. Keyed
    any other way — "Bills <BILLS@Shop.example>" as it arrived, say — the
    correction would train a sender the model never looks up, and the history
    it exists to outweigh would go on winning silently.
    """
    recorded = 0
    when = int(time.time()) if now is None else now
    for decision in decisions:
        if not decision.override_folder:
            continue
        message = decision.proposal.message
        record_correction(
            Correction(
                sender=normalise_sender(message.sender),
                domain=sender_domain(message.sender),
                subject=message.subject,
                chosen_folder=decision.override_folder,
                rejected_folder=decision.proposal.folder,
                recorded_at=when,
            ),
            config,
        )
        recorded += 1
    return recorded


def load_corrections(config: Config) -> list[Correction]:
    """Every correction recorded so far, damaged lines skipped.

    A half-written line — an interrupted run, a file edited by hand — costs
    that one correction rather than every correction. Losing the lot to a
    stray keystroke would be a poor trade for strictness that buys nothing:
    unlike the rules file, nothing here can file mail on its own.
    """
    path = config.corrections_path
    if not path.exists():
        return []
    corrections = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            corrections.append(Correction(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return corrections


def corrections_as_examples(
    corrections: list[Correction], config: Config
) -> list[TrainingExample]:
    """Corrections enter training at ``correction_weight`` times normal weight.

    A correction naming no folder is dropped: it would otherwise train the
    empty string as a destination, which is worse than learning nothing.
    """
    return [
        TrainingExample(
            sender=item.sender,
            domain=item.domain,
            subject=item.subject,
            folder=normalise_folder(item.chosen_folder),
            weight=config.correction_weight,
            year=time.gmtime(item.recorded_at).tm_year,
        )
        for item in corrections
        if item.chosen_folder.strip()
    ]
