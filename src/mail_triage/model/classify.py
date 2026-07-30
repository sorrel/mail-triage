"""Run the classification stages in order and produce an explainable proposal.

Stage order is deliberate: the cheapest and most explainable stage goes first.
A message that no stage can place confidently stays in the inbox — that is the
safety net for important mail, and it is not a failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mail_triage.config import Config
from mail_triage.corpus import normalise_sender, sender_domain
from mail_triage.deletion import DeletionStats, PerAccountDeletionIndex, deletion_veto
from mail_triage.envelope import MessageRow
from mail_triage.folders import normalise_folder
from mail_triage.guards import is_bulk, needs_attention
from mail_triage.invoices import invoice_reason
from mail_triage.model.store import TrainedModel
from mail_triage.rules import Rule


@dataclass(frozen=True)
class Proposal:
    """What we think should happen to one message, and why."""

    message: MessageRow
    folder: str | None
    confidence: float
    reason: str
    stage: str  # sender | tokens | llm | none
    # Task 11B: set when a hard veto overrode a filing decision. ``reason``
    # above still records why the classifier would have filed it — ``veto``
    # records why it didn't happen. Confidence is untouched by a veto: it
    # stays exactly what the classifier computed, at 0.99 or 0.71 alike.
    veto: str | None = None
    # Machine-readable companion to ``veto``, whose prose is written for a
    # person to read. "attention" means this message may need a reply or was
    # flagged; "deletion" means the sender's recent mail is only ever binned.
    # The distinction drives policy: attention-vetoed mail must never be
    # offered for binning, whereas deletion-vetoed mail is the prime candidate.
    veto_kind: str | None = None
    # "file" sends the message to ``folder``; "delete" sends it to the Trash.
    # Only a bin rule produces "delete", and any veto resets it to "file" —
    # a vetoed message is acted on in no way at all.
    action: str = "file"

    @property
    def is_actionable(self) -> bool:
        """Whether there is something to do with this message."""
        return self.folder is not None or self.action == "delete"


class Classifier:
    def __init__(
        self,
        model: TrainedModel,
        config: Config,
        available_folders: list[str],
        guard: Callable[[MessageRow], dict[str, str] | None] | None = None,
        deletion_index: dict[str, DeletionStats] | None = None,
        rules: dict[str, Rule] | None = None,
        attachments: dict[int, list[str]] | None = None,
    ) -> None:
        self.model = model
        self.config = config
        # Map normalised name back to the account's real capitalisation.
        # available_folders must carry full nested paths (e.g. "Parent/Child",
        # sourced from folder_path() over the envelope database) — a flat
        # leaf-only list from Mail's AppleScript would cause every nested
        # prediction to be rejected as "does not exist in this account".
        self.folders = {normalise_folder(name): name for name in available_folders}
        # Optional hook that fetches a message's raw headers (List-Unsubscribe
        # in particular) so the do-not-file guard (Task 11B) can tell bulk
        # mail from a human correspondent. Fetching costs an AppleScript round
        # trip, so it is only ever called for a message that would otherwise
        # be filed, and only when the free sender-address check is
        # inconclusive. With no guard supplied, that half of the veto is
        # simply not checked — existing filing behaviour is unaffected — but
        # the flagged check below needs no fetch at all and always applies.
        self.guard = guard
        # Task 11C: sender -> DeletionStats over the same recent window as
        # the rest of this run, built once by the caller from the same
        # snapshot everything else here uses (see build_deletion_index).
        # With none supplied, this half of the veto simply isn't checked —
        # existing filing behaviour is unaffected, same as an absent guard.
        self.deletion_index = deletion_index
        # Hard rules, keyed by lower-cased sender address. A rule is the user
        # answering "where does this sender's mail go?" directly, so it is
        # consulted before any statistics — but it loses to a per-message
        # do-not-file guard, because a rule is about a sender and a guard is
        # about this particular message. See _apply_rule.
        self.rules = rules or {}
        # Message rowid -> attachment filenames, for the invoice guard. Read
        # from the same snapshot as everything else in the run. Without it the
        # guard still works on subjects alone; it just misses the commonest
        # shape, a neutral subject with the invoice attached.
        self.attachments = attachments or {}

    def classify(self, message: MessageRow) -> Proposal:
        """Classify one message, then hold back anything that looks like a bill."""
        return self._invoice_guard(message, self._classify(message))

    def _invoice_guard(self, message: MessageRow, proposal: Proposal) -> Proposal:
        """Hold back a bill, whatever else was decided about it.

        Applied last and to *every* proposal, placed or not. Placed, because an
        invoice must be dealt with before it is filed and outranks even a hard
        rule. Unplaced, because an unmarked invoice staying in the inbox would
        be offered for binning — the very harm this rule exists to prevent.
        """
        reason = invoice_reason(message.subject, self.attachments.get(message.rowid, ()))
        if reason is None:
            return proposal
        return Proposal(
            proposal.message, None, proposal.confidence, proposal.reason, proposal.stage,
            veto=reason, veto_kind="invoice",
        )

    def _classify(self, message: MessageRow) -> Proposal:
        sender = normalise_sender(message.sender)
        rule = self.rules.get(sender)
        if rule is not None:
            return self._apply_rule(message, rule, sender)
        domain = sender_domain(message.sender)
        prediction = self.model.sender.predict(sender, domain)
        if prediction is None:
            # No history for this address or domain. Stage B is still tried
            # below — placing a stranger by what their message says is the
            # thing stage A structurally cannot do.
            proposal = Proposal(
                message, None, 0.0, "no history for this sender or domain", "none"
            )
        else:
            proposal = self._from_prediction(message, prediction, "sender")
        if proposal.folder is None:
            # Stage B: the subject line. Tried whenever stage A produced no
            # folder — unknown sender, split history, or a folder that no
            # longer exists — since that is exactly when what a message *says*
            # is more informative than who sent it.
            fallback = self._try_tokens(message)
            if fallback is not None:
                proposal = fallback
        if proposal.folder is None:
            # Keep stage A's proposal, not stage B's: the reason and stage are
            # what rank_uncertain reads to decide which senders to ask about.
            return proposal
        return self._apply_guard(message, proposal)

    def _try_tokens(self, message: MessageRow) -> Proposal | None:
        """Stage B's proposal, or ``None`` if it cannot place the message."""
        if self.model.tokens is None:
            return None
        prediction = self.model.tokens.predict(message.subject, message.sender)
        if prediction is None:
            return None
        proposal = self._from_prediction(message, prediction, "tokens")
        return proposal if proposal.folder is not None else None

    def _from_prediction(self, message: MessageRow, prediction, stage: str) -> Proposal:
        """Turn a stage's prediction into a proposal, applying the two rejections
        every stage shares: a folder that is not in this account, and a
        confidence below the threshold."""
        actual_folder = self.folders.get(prediction.folder)
        if actual_folder is None:
            return Proposal(
                message, None, prediction.confidence,
                f"'{prediction.folder}' does not exist in this account", stage,
            )
        if prediction.confidence < self.config.confidence_threshold:
            return Proposal(
                message, None, prediction.confidence,
                f"{prediction.reason} — below threshold "
                f"({prediction.confidence:.2f} < {self.config.confidence_threshold:.2f})",
                stage,
            )
        return Proposal(message, actual_folder, prediction.confidence, prediction.reason, stage)

    def _apply_rule(self, message: MessageRow, rule: Rule, sender: str) -> Proposal:
        """File by explicit instruction, subject only to the per-message guards.

        The deletion veto is deliberately *not* consulted here. That veto is
        inference from what has been landing in the Trash; a rule is a direct
        and recent answer from the user. The direct answer wins. The per-message
        guards still apply above it, because "this message needs a reply" is a
        judgement about the message, not about the sender.
        """
        if rule.action == "bin":
            # A move to the Trash, journalled and undoable like any other.
            # Still subject to the per-message guards below, so a bill from a
            # binned sender is held in the inbox rather than thrown away.
            proposal = Proposal(
                message, None, 1.0,
                f"your rule: bin mail from {sender}", "rule", action="delete",
            )
            return self._message_guards(message, proposal)
        if rule.action == "leave":
            return Proposal(
                message, None, 1.0,
                f"you asked me to leave {sender} alone", "rule",
            )
        actual_folder = self.folders.get(normalise_folder(rule.folder or ""))
        if actual_folder is None:
            # Reported rather than silently ignored, so the sender surfaces
            # for re-asking exactly as an orphaned prediction does.
            return Proposal(
                message, None, 1.0,
                f"your rule files {sender} to '{rule.folder}', which no longer "
                "exists in this account",
                "rule",
            )
        proposal = Proposal(
            message, actual_folder, 1.0,
            f"your rule: file {sender} to '{actual_folder}'", "rule",
        )
        return self._message_guards(message, proposal)

    def _apply_guard(self, message: MessageRow, proposal: Proposal) -> Proposal:
        """Check the do-not-file veto for a message that would otherwise be filed.

        Only reached once a folder has actually been decided — a message
        already staying in the inbox has nothing to veto and needs no header
        fetch, keeping the AppleScript cost to exactly the messages it can
        change the outcome for.
        """
        if message.flagged:
            return self._vetoed(proposal, "you flagged this", "attention")
        if self.deletion_index is not None:
            # Free check — no header fetch, just a dict lookup — so it runs
            # unconditionally, same as the flagged check above and ahead of
            # the guard-based bulk check, which does need a fetch.
            sender = normalise_sender(message.sender)
            # Evidence is counted per account, so a run covering several
            # resolves the message's own account first. A plain dict is
            # the single-source case and needs no resolution.
            index = self.deletion_index
            if isinstance(index, PerAccountDeletionIndex):
                index = index.for_message(message)
            veto = deletion_veto(index.get(sender), self.config)
            if veto is not None:
                return self._vetoed(proposal, veto, "deletion")
        return self._bulk_guard(message, proposal)

    def _message_guards(self, message: MessageRow, proposal: Proposal) -> Proposal:
        """The guards that judge this message rather than its sender.

        These are the only checks that override a hard rule, so they are kept
        separate from the deletion veto, which a rule outranks.
        """
        if message.flagged:
            return self._vetoed(proposal, "you flagged this", "attention")
        return self._bulk_guard(message, proposal)

    def _bulk_guard(self, message: MessageRow, proposal: Proposal) -> Proposal:
        """Veto mail from a human correspondent — it may be awaiting a reply."""
        if self.guard is None:
            return proposal
        if is_bulk(message.sender, None):
            # The sender address alone already proves this is bulk mail
            # (no-reply@ and friends) — no need to spend a header fetch.
            return proposal
        headers = self._fetch_headers(message)
        veto = needs_attention(message, headers)
        if veto is not None:
            return self._vetoed(proposal, veto.reason, "attention")
        return proposal

    def _fetch_headers(self, message: MessageRow) -> dict[str, str] | None:
        """Fetch headers via the guard hook, failing safe on any error.

        Mail not running, an AppleScript error, a timeout — whatever the
        cause, an unavailable signal must not be read as permission to file.
        Returning ``None`` here feeds straight into ``needs_attention``'s own
        fail-safe handling of missing headers.
        """
        try:
            return self.guard(message)  # type: ignore[misc]
        except Exception:
            return None

    @staticmethod
    def _vetoed(proposal: Proposal, reason: str, kind: str) -> Proposal:
        return Proposal(
            proposal.message, None, proposal.confidence, proposal.reason, proposal.stage,
            veto=reason, veto_kind=kind,
        )
