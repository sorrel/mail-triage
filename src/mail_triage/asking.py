"""Asking about senders the model knows but cannot call.

The first live dry run classified 55 inbox messages: 12 filable, 6 vetoed, 7
from senders with no history at all — and **30 from senders the model knows
but cannot call**, because their mail has gone to two or three different
folders. That is the largest group by some margin, and before this module the
tool neither mentioned them nor learnt anything from them.

These are the cases worth a question: the sender is known, the candidate
folders are known, and the only missing input is the user's intent, which he can
supply in one keystroke and the statistics cannot recover at any price.

**Per sender, never per message.** One answer settles every message from them,
in this run and every future run.

**Ranked by yearly sending rate, capped at five.** Measured on the real
inbox: those 30 messages came from 24 distinct senders at 1–3 messages each —
a long tail, not a concentration, so five answers clear only 11 messages
tonight. Ranked by how often each sender *writes*, the same five answers
permanently remove ~85 messages a year of uncertainty out of 180. Ranking on
inbox count instead is nearly arbitrary (every count is 1 to 3) and would put
a sender who wrote three times ever above one who writes 27 times a year.

Senders with no history at all are deliberately not asked about: there are no
candidate folders to offer, so the question degrades into "type a folder path
for this stranger" — high effort, low leverage.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mail_triage.config import Config
from mail_triage.corpus import normalise_sender
from mail_triage.folders import normalise_folder
from mail_triage.model.classify import Proposal
from mail_triage.invoices import invoice_reason
from mail_triage.model.store import TrainedModel
from mail_triage.rules import Rule, record_rule

SECONDS_PER_DAY = 86_400
YEAR_DAYS = 365

# Five questions a run: enough to clear the measured backlog in five sittings,
# few enough that a run does not turn into an interrogation.
DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class UncertainSender:
    """A sender worth one question, and everything needed to ask it well."""

    sender: str
    # Real folder names (account capitalisation) to weighted sightings.
    # Folders that no longer exist are excluded — offering one would invite an
    # answer that could not be honoured.
    candidates: dict[str, float] = field(default_factory=dict)
    yearly_count: int = 0
    inbox_count: int = 0
    kind: str = "inconsistent"  # inconsistent | orphaned
    # True if this sender's recent mail includes anything billing-shaped. Such
    # a sender is never offered the bin answer: binning a bill because the
    # marketing from the same address was unwanted is exactly the harm the
    # invoice requirement names.
    sends_invoices: bool = False


def build_yearly_counts(reader, config: Config, now: int | None = None) -> dict[str, int]:
    """Count each sender's messages over the last year, for ranking.

    Every folder counts, deletions included: this measures how often a sender
    *writes*, not what was done with it. The one-year window matches the
    ``half_life_days`` default, so a sender who has gone quiet does not consume
    one of five questions.
    """
    if now is None:
        now = int(time.time())
    cutoff = now - YEAR_DAYS * SECONDS_PER_DAY
    prefixes = tuple(config.training_prefixes)
    counts: dict[str, int] = {}
    for message in reader.all_messages():
        if not message.date_sent or message.date_sent < cutoff:
            continue
        if not message.mailbox_url.startswith(prefixes):
            continue
        sender = normalise_sender(message.sender)
        if not sender:
            continue
        counts[sender] = counts.get(sender, 0) + 1
    return counts


def build_billing_senders(reader, config: Config, now: int | None = None) -> set[str]:
    """Senders whose recent mail includes anything that looks like a bill.

    Subject lines only, deliberately: this is a sender-level flag used to
    withhold the bin answer, and the per-message invoice guard — which does
    read attachments — remains the real protection for any individual bill.
    Cheap defence in depth rather than a second detector.
    """
    if now is None:
        now = int(time.time())
    cutoff = now - YEAR_DAYS * SECONDS_PER_DAY
    prefixes = tuple(config.training_prefixes)
    senders: set[str] = set()
    for message in reader.all_messages():
        if not message.date_sent or message.date_sent < cutoff:
            continue
        if not message.mailbox_url.startswith(prefixes):
            continue
        sender = normalise_sender(message.sender)
        if not sender or sender in senders:
            continue
        if invoice_reason(message.subject) is not None:
            senders.add(sender)
    return senders


def _is_uncertain(proposal: Proposal) -> bool:
    """Whether this proposal is the kind a question could resolve.

    A veto is checked first: it already settled the message on grounds a
    filing rule would not change, so those are never asked about.
    """
    if proposal.veto is not None or proposal.folder is not None:
        return False
    if proposal.stage == "rule":
        # An answered sender is not re-asked — unless the folder chosen has
        # since been deleted, which leaves the rule unusable exactly as an
        # orphaned prediction is.
        return "no longer exists" in proposal.reason
    if proposal.stage != "sender":
        return False  # "none" — no history at all, nothing to offer
    return True


def _kind(proposal: Proposal) -> str:
    if "does not exist" in proposal.reason or "no longer exists" in proposal.reason:
        return "orphaned"
    return "inconsistent"


def rank_uncertain(
    proposals: Iterable[Proposal],
    model: TrainedModel,
    available_folders: Iterable[str],
    yearly_counts: dict[str, int],
    limit: int = DEFAULT_LIMIT,
    billing_senders: set[str] | frozenset[str] = frozenset(),
) -> list[UncertainSender]:
    """Pick the senders worth asking about, best leverage first."""
    folders = {normalise_folder(name): name for name in available_folders}
    inbox_counts: dict[str, int] = {}
    kinds: dict[str, str] = {}
    for proposal in proposals:
        if not _is_uncertain(proposal):
            continue
        sender = normalise_sender(proposal.message.sender)
        if not sender:
            continue
        inbox_counts[sender] = inbox_counts.get(sender, 0) + 1
        kinds.setdefault(sender, _kind(proposal))

    uncertain = [
        UncertainSender(
            sender=sender,
            candidates={
                folders[folder]: weight
                for folder, weight in model.sender.by_sender.get(sender, {}).items()
                if folder in folders
            },
            yearly_count=yearly_counts.get(sender, 0),
            inbox_count=count,
            kind=kinds[sender],
            sends_invoices=sender in billing_senders,
        )
        for sender, count in inbox_counts.items()
    ]
    # Yearly rate first, inbox count as the tie-break, then the address so a
    # run's questions are reproducible rather than dictionary-ordered.
    uncertain.sort(key=lambda item: (-item.yearly_count, -item.inbox_count, item.sender))
    return uncertain[:limit]


def _question(uncertain: UncertainSender, ordered: list[tuple[str, float]]) -> str:
    """Build the prompt."""
    lines = [
        f"\n{uncertain.sender} — {uncertain.yearly_count} messages a year, "
        f"{uncertain.inbox_count} in the inbox now"
    ]
    for index, (folder, weight) in enumerate(ordered, start=1):
        # Weights are recency-decayed, so a genuine past filing can sit below
        # 1. Rounding those to "(0)" would read as "never used here" and make
        # the choice worse-informed than showing no number at all.
        if weight < 0.1:
            shown = "<0.1"
        else:
            shown = f"{weight:.1f}" if weight < 1 else f"{weight:.0f}"
        lines.append(f"  [{index}] {folder} ({shown})")
    if not ordered:
        lines.append("  (the folder this sender used no longer exists)")
    choices = f"1-{len(ordered)}, " if ordered else ""
    # The delete answer is withheld entirely from billing senders — not merely
    # rejected if typed, though ``ask`` does that too.
    #
    # "d", never "b": "b" means *back* in the review loops, and a key that
    # means "go back" in one prompt and "delete the lot" in another is a
    # mistake waiting to happen.
    deleting = "" if uncertain.sends_invoices else "[d]elete these, "
    lines.append(
        f"Where should this sender's mail go? [{choices}a folder name, "
        f"{deleting}[l]eave alone, Enter to skip] "
    )
    return "\n".join(lines)


def ask(
    uncertain: UncertainSender,
    prompt: Callable[[str], str],
    match_folders: Callable[[str], list[str]],
    now: int | None = None,
) -> Rule | None:
    """Ask about one sender. Returns the rule to record, or ``None`` to skip.

    ``match_folders`` maps a typed folder name to every real mailbox it could
    mean, in the account's own capitalisation. Nothing matching is a typo, and
    a typo must not create a rule pointing nowhere, so it is re-prompted rather
    than stored; several matches means a leaf name shared by more than one
    folder, and the choice goes back to the user rather than being guessed.
    """
    if now is None:
        now = int(time.time())
    ordered = sorted(uncertain.candidates.items(), key=lambda item: (-item[1], item[0]))
    question = _question(uncertain, ordered)
    while True:
        answer = prompt(question).strip()
        if not answer:
            return None
        if answer.casefold() in ("d", "delete") and not uncertain.sends_invoices:
            # A move to the account's trash: journalled and undoable like any
            # other move, never a hard delete. The stored action stays "bin"
            # so rules answered before this wording still load.
            return Rule(sender=uncertain.sender, action="bin", folder=None,
                        answered_at=now, candidates=dict(uncertain.candidates))
        if answer.casefold() in ("l", "leave"):
            # The escape hatch. Some senders genuinely split by content — a
            # shop sending both order confirmations and marketing — and with
            # no "it depends" answer the question would force a bad rule
            # rather than admit the case exists.
            return Rule(sender=uncertain.sender, action="leave", folder=None,
                        answered_at=now, candidates=dict(uncertain.candidates))
        if answer.isdigit() and 1 <= int(answer) <= len(ordered):
            folder = ordered[int(answer) - 1][0]
        else:
            matches = match_folders(answer)
            if not matches:
                question = (
                    f"No mailbox called '{answer}' in this account. "
                    "Try again, or press Enter to skip: "
                )
                continue
            if len(matches) > 1:
                question = (
                    f"'{answer}' could be {', '.join(matches)}. "
                    "Type more of the path, or press Enter to skip: "
                )
                continue
            folder = matches[0]
        return Rule(sender=uncertain.sender, action="file", folder=folder,
                    answered_at=now, candidates=dict(uncertain.candidates))


def ask_all(
    uncertain: list[UncertainSender],
    prompt: Callable[[str], str],
    match_folders: Callable[[str], list[str]],
    rules_path: Path,
    now: int | None = None,
) -> list[Rule]:
    """Ask about each sender in turn, writing every answer as it is given.

    Written per answer rather than batched so interrupting the questions
    (Ctrl-C) keeps everything already answered and proceeds with it applied.
    """
    answered: list[Rule] = []
    for item in uncertain:
        try:
            rule = ask(item, prompt, match_folders, now=now)
        except (KeyboardInterrupt, EOFError):
            break
        if rule is None:
            continue
        record_rule(rules_path, rule)
        answered.append(rule)
    return answered
