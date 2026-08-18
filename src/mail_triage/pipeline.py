"""Configuration in, proposals out — the run every entry point shares.

``triage``, ``web`` and ``report`` all answer the same question first: given
this configuration, what does the classifier think should happen to what is in
the inbox right now? Each of them used to answer it in about twenty-eight
lines of its own — load the model, load the rules, load the never-personal
list, load the security senders, gather a snapshot's worth of inputs, build a
``Classifier``, classify every message.

Three copies is two too many, and they had already begun to drift: ``report``
collapsed the three loader errors into one ``try`` whilst the other two used
three separate blocks with the comments copied between them. The drift is the
argument. This is the block a new guard has to be wired into, and a guard
wired into two callers out of three is a guard that silently does not apply —
exactly the class of failure the guards exist to prevent.

Deliberately not here: anything that prints. The header guard is passed in,
because fetching headers is slow enough to want a progress line and a progress
line is the caller's business. And the loaders raise their own errors rather
than ``click`` ones, so this module stays usable by something that is not a
command line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mail_triage.config import Config, Source
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.inputs import TriageInputs, gather
from mail_triage.model.classify import Classifier, Proposal
from mail_triage.model.store import TrainedModel, load_model
from mail_triage.never_personal import load_never_personal
from mail_triage.rules import Rule, load_rules
from mail_triage.security import load_security_senders


@dataclass(frozen=True)
class Run:
    """One classification of one snapshot, and what it took to make it."""

    config: Config
    sources: list[Source]
    model: TrainedModel
    rules: dict[str, Rule]
    inputs: TriageInputs
    proposals: list[Proposal]
    # Re-run the classification with different rules, against the *same*
    # snapshot. ``triage`` needs this: answering questions about uncertain
    # senders creates rules, and the proposals must then be redrawn — but
    # against the inbox as it was when the run started, not as it is now.
    # Re-gathering would silently change the message list mid-run.
    reclassify: Callable[[dict[str, Rule]], list[Proposal]]

    @property
    def folders(self) -> list[str]:
        return self.inputs.folders

    def held(self, kind: str) -> list[Proposal]:
        """Proposals a particular guard held back."""
        return [item for item in self.proposals if item.veto_kind == kind]


def classify_run(
    config: Config,
    sources: list[Source],
    *,
    ask: bool = False,
    guard: Callable | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> Run:
    """Load everything a run needs, then classify the inbox once.

    The loaders are called in a fixed order and none of their errors are
    caught: an unreadable rules file, never-personal list or security-sender
    list must stop the run rather than be skipped. Silence in any of them
    files mail contrary to instructions — and in the security list's case,
    files exactly the mail that most wanted reading.
    """
    model = load_model(config.model_path)
    rules = load_rules(config.rules_path)
    never_personal = load_never_personal(config.never_personal_path)
    security_senders = load_security_senders(config.security_senders_path)
    inputs = gather(config, sources, ask, db_path)

    def reclassify(current_rules: dict[str, Rule]) -> list[Proposal]:
        classifier = Classifier(
            model,
            config,
            inputs.folders,
            guard=guard,
            deletion_index=inputs.deletion_index,
            rules=current_rules,
            attachments=inputs.attachments,
            never_personal=never_personal,
            security_senders=security_senders,
        )
        return [classifier.classify(message) for message in inputs.messages]

    return Run(
        config=config,
        sources=sources,
        model=model,
        rules=rules,
        inputs=inputs,
        proposals=reclassify(rules),
        reclassify=reclassify,
    )
