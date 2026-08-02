"""Present proposals and collect decisions.

The prompt function is injected so the whole loop is testable without a
terminal, and so a future non-interactive mode reuses the same code.

Nothing in this module moves a message. The review loop only ever hands
back a list of ``Decision`` objects describing what the user chose; acting
on them belongs to a later stage, gated on the user's explicit approval.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from mail_triage.folders import account_prefix
from mail_triage.model.classify import Proposal

SUBJECT_WIDTH = 40
FOLDER_WIDTH = 28
SENDER_WIDTH = 28
ACCOUNT_WIDTH = 10

_WIDE_EMOJI_THRESHOLD = 0x1F300


@dataclass(frozen=True)
class Decision:
    proposal: Proposal
    accepted: bool
    override_folder: str | None = None
    # "file" sends the message to ``folder``; "delete" sends it to the Trash
    # instead. A delete is still a journalled, undoable move — never a hard
    # delete — so the only difference downstream is the destination.
    action: str = "file"

    @property
    def folder(self) -> str | None:
        return self.override_folder or self.proposal.folder

    @property
    def is_delete(self) -> bool:
        return self.action == "delete"


def _char_width(char: str) -> int:
    """Terminal columns a single character occupies.

    ``len()`` counts every character as one, but emoji and East Asian wide/
    fullwidth characters occupy two terminal columns. Subjects routinely
    contain emoji, so padding on ``len()`` would silently misalign columns.
    """
    if ord(char) >= _WIDE_EMOJI_THRESHOLD:
        return 2
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def display_width(text: str) -> int:
    """Terminal columns ``text`` occupies, accounting for wide characters."""
    return sum(_char_width(char) for char in text)


def _clip(text: str, width: int) -> str:
    """Collapse newlines and truncate to ``width`` display columns, adding an ellipsis."""
    text = text.replace("\n", " ").strip()
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    total = 0
    for char in text:
        char_width = _char_width(char)
        if total + char_width > width - 1:
            break
        kept.append(char)
        total += char_width
    return "".join(kept) + "…"


def _pad(text: str, width: int) -> str:
    """Right-pad to ``width`` display columns. Apply after clipping, before colouring."""
    return text + " " * max(0, width - display_width(text))


def _column(text: str, width: int) -> str:
    return _pad(_clip(text, width), width)


def render_table(
    proposals: list[Proposal], accounts: dict[str, str] | None = None
) -> str:
    """Render placed proposals as an aligned table.

    Unplaced proposals (``folder is None``) are deliberately excluded — they
    are not a filing suggestion, so showing them here would misrepresent what
    the tool is about to do. Use ``summarise`` for an account of those.

    ``accounts`` maps account prefix to the name Mail shows. The Account
    column appears only when more than one account is being triaged: with a
    single source it repeats the same value on every row and buys nothing but
    width.
    """
    show_account = accounts is not None and len(accounts) > 1
    heading = f"{_column('Account', ACCOUNT_WIDTH)} " if show_account else ""
    lines = [
        f"{heading}{_column('Sender', SENDER_WIDTH)} {_column('Subject', SUBJECT_WIDTH)} "
        f"{_column('→ Folder', FOLDER_WIDTH)} Conf"
    ]
    for item in proposals:
        if not item.is_actionable:
            continue
        destination = item.folder if item.folder is not None else "(delete — your rule)"
        cell = ""
        if show_account:
            # "?" rather than a guess: a message from an account that is not
            # configured should look wrong, not plausible.
            name = accounts.get(account_prefix(item.message.mailbox_url), "?")
            cell = f"{_column(name, ACCOUNT_WIDTH)} "
        lines.append(
            f"{cell}{_column(item.message.sender, SENDER_WIDTH)} "
            f"{_column(item.message.subject, SUBJECT_WIDTH)} "
            f"{_column(destination, FOLDER_WIDTH)} {item.confidence:.2f}"
        )
    return "\n".join(lines)


def _categorise(
    proposals: list[Proposal],
) -> tuple[list[Proposal], list[Proposal], list[Proposal], list[Proposal], list[Proposal]]:
    """Split proposals into placed, three unplaced reasons, and vetoed.

    The three plain-unplaced buckets mirror the three ``folder=None`` code
    paths in ``Classifier.classify`` that run *before* any veto check: no
    history at all, a predicted folder that no longer exists, and a known
    sender whose history is too inconsistent to clear the confidence
    threshold. A veto (Task 11B) is checked first and takes priority over
    those — a vetoed proposal's ``stage``/``reason`` still describe the
    filing decision the veto overrode, so it must not be miscategorised as
    plain "no history" or "inconsistent" just because it also has
    ``folder=None``.
    """
    placed: list[Proposal] = []
    no_history: list[Proposal] = []
    missing_folder: list[Proposal] = []
    inconsistent: list[Proposal] = []
    vetoed: list[Proposal] = []
    for item in proposals:
        if item.veto is not None:
            vetoed.append(item)
        elif item.is_actionable:
            placed.append(item)
        elif item.stage == "none":
            no_history.append(item)
        elif "does not exist" in item.reason:
            missing_folder.append(item)
        else:
            inconsistent.append(item)
    return placed, no_history, missing_folder, inconsistent, vetoed


def summarise(proposals: list[Proposal], accounts: dict[str, str] | None = None) -> str:
    """Describe the outcome, giving equal weight to what stays in the inbox.

    For most real inboxes the majority of messages stay put — either the
    sender has no filing history, or the history is too inconsistent to call
    confidently. That is the expected, safe outcome, not a shortfall, so it
    is broken down here rather than collapsed into a single "unplaced" count.

    Vetoed messages (Task 11B) get their own line per message, not just a
    count: these are messages the classifier was otherwise ready to file,
    some at very high confidence, so the user needs to see exactly which
    ones were held back and why — "you flagged this", "looks personal, may
    need a reply" — in order to trust that the veto is doing the right thing.

    ``accounts`` maps account prefix to the name Mail shows, and follows the
    same rule as ``render_table``: the name is shown only when more than one
    account is being triaged. Without it a whole account's inbox could be
    held back and read as never having been scanned at all, which is exactly
    how the Exchange source first appeared to be broken when it was not.
    """
    placed, no_history, missing_folder, inconsistent, vetoed = _categorise(proposals)
    show_account = accounts is not None and len(accounts) > 1

    def held_line(item: Proposal) -> str:
        name = ""
        if show_account:
            # "?" rather than a guess, as in ``render_table``: a message from
            # an unconfigured account should look wrong, not plausible.
            name = accounts.get(account_prefix(item.message.mailbox_url), "?") + " — "
        return f"    {name}{item.message.subject} — {item.veto}"

    total = len(proposals)
    unplaced_count = total - len(placed)
    lines = [f"{len(placed)} of {total} would be filed; {unplaced_count} staying in the inbox."]
    if placed:
        average = sum(item.confidence for item in placed) / len(placed)
        confident = sum(1 for item in placed if item.confidence >= 0.9)
        lines.append(
            f"  confidence: average {average:.2f}; {confident} of {len(placed)} at 0.90 or above."
        )
    # Bills come first and get their own heading: the requirement is that an
    # invoice is *dealt with*, not merely held back, so burying it in a list
    # of ordinary vetoes would miss the point.
    bills = [item for item in vetoed if item.veto_kind == "invoice"]
    other_vetoes = [item for item in vetoed if item.veto_kind != "invoice"]
    if bills:
        lines.append(f"  {len(bills)} need dealing with — these look like bills:")
        for item in bills:
            lines.append(held_line(item))
    if other_vetoes:
        lines.append(
            f"  {len(other_vetoes)} staying in the inbox despite a filing proposal:"
        )
        for item in other_vetoes:
            lines.append(held_line(item))
    if no_history:
        lines.append(
            f"  {len(no_history)} staying in the inbox: no filing history for this sender."
        )
    if inconsistent:
        lines.append(
            f"  {len(inconsistent)} staying in the inbox: sender known, "
            "but filing history is too inconsistent to call."
        )
    if missing_folder:
        lines.append(
            f"  {len(missing_folder)} staying in the inbox: "
            "the predicted folder no longer exists in this account."
        )
    return "\n".join(lines)


def _confirm_or_go_back(answered: int, noun: str, prompt: Callable[[str], str]) -> bool:
    """After the last message, offer one chance to revisit it.

    Without this the final answer is the one thing that cannot be corrected —
    the loop ends the moment it is given — which is precisely the "whoops"
    case when you have just pressed the wrong key.
    """
    if not answered:
        return False
    reply = prompt(
        f"That's all {answered}. [Enter] to go ahead, [b] to change the last {noun} "
    ).strip().casefold()
    return reply == "b"


def review(
    proposals: list[Proposal],
    prompt: Callable[[str], str],
    match_folders: Callable[[str], list[str]] | None = None,
) -> list[Decision]:
    """Ask what to do. Returns only the decisions the user made.

    Answers: a = accept all, q = quit without acting, s = step through one by one.
    In step mode: y = accept, n = reject, d = delete (to the Trash), b = go
    back and re-answer the previous message. Typing a folder name instead
    files the message there — the correction that teaches the model it
    proposed the wrong place.

    ``d`` is deliberately available only per message. "Accept all" must never
    be able to bin anything — a batch answer given in one keystroke is the
    wrong instrument for a destructive choice, even a reversible one.

    ``match_folders`` maps a typed name to every real mailbox it could mean,
    exactly as the sender questions do. Without it a stray reply still means
    "no": four of the answers here are single letters, so an unrecognised
    reply is far more likely to be a slip than a destination, and with no
    folder list to check it against there is no way to tell the difference.

    Unplaced proposals (``folder is None``) are never offered — there is
    nothing to accept or reject for a message the classifier left alone.
    """
    placed = [item for item in proposals if item.is_actionable]
    if not placed:
        return []
    answer = prompt("[a]ccept all, [s]tep through, [q]uit? ").strip().casefold()
    if answer == "a":
        # A bin proposal comes from a rule already agreed to, so accepting it
        # in bulk is agreeing to what was already decided — unlike the
        # per-message "d", which is a fresh destructive choice.
        return [Decision(item, accepted=True, action=item.action) for item in placed]
    if answer != "s":
        return []
    # One slot per message rather than an append-only list, so "b" can revisit
    # a message and overwrite the answer given for it.
    answers: list[Decision | None] = [None] * len(placed)
    index = 0
    # Carried between iterations so a refused folder name can explain itself
    # in the next question rather than in a line above it that scrolls away.
    note = ""
    while index < len(placed):
        item = placed[index]
        destination = item.folder if item.folder is not None else "delete"
        options = "[y/n/d, b=back, or a folder] " if match_folders else "[y/n/d, b=back] "
        raw = prompt(
            f"{note}{_clip(item.message.subject, SUBJECT_WIDTH)} → {destination}? {options}"
        ).strip()
        note = ""
        reply = raw.casefold()
        if reply == "b":
            # Nothing has moved yet — the whole loop only collects decisions —
            # so going back is just discarding the previous answer.
            if index > 0:
                index -= 1
                answers[index] = None
            continue
        if reply == "d":
            answers[index] = Decision(item, accepted=True, action="delete")
        elif match_folders and raw and reply not in ("y", "n"):
            matches = match_folders(raw)
            if not matches:
                note = f"No mailbox called '{raw}'. "
                continue
            if len(matches) > 1:
                note = f"'{raw}' could be {', '.join(matches)}; type more of the path. "
                continue
            answers[index] = Decision(item, accepted=True, override_folder=matches[0])
        else:
            answers[index] = Decision(item, accepted=reply == "y", action=item.action)
        index += 1
        if index == len(placed) and _confirm_or_go_back(
            sum(answer is not None for answer in answers), "answer", prompt
        ):
            index -= 1
            answers[index] = None
    return [answer for answer in answers if answer is not None]


def binnable(proposals: list[Proposal]) -> list[Proposal]:
    """Unplaced messages that may be offered for binning.

    Everything staying in the inbox qualifies *except* mail held back by an
    attention veto — flagged, or apparently awaiting a reply — or an invoice
    veto, since binning a bill is exactly what that rule exists to prevent.
    Offering to bin
    those would defeat the guard that held them back, and more finally than
    filing would have. Mail held back by the deletion veto is included on
    purpose: that veto means "you keep binning this sender", which makes it
    the strongest candidate here rather than the weakest.
    """
    return [
        item for item in proposals
        if not item.is_actionable and item.veto_kind not in ("attention", "invoice")
    ]


def review_unplaced(proposals: list[Proposal], prompt: Callable[[str], str]) -> list[Decision]:
    """Offer to bin the messages the classifier could not place.

    This is the answer that was missing from the main loop: it only ever
    showed messages it had a folder for, leaving the largest group — mail
    staying put because the sender's history is inconsistent or unknown —
    with nothing that could be done about it, run after run.

    Only deletions come out of here. Keeping is the default on Enter, and no
    decision is recorded for a kept message, so an accidental keystroke costs
    nothing.
    """
    candidates = binnable(proposals)
    if not candidates:
        return []
    answer = prompt(
        f"\n{len(candidates)} messages stayed in the inbox. "
        "Go through them for binning? [y/N] "
    ).strip().casefold()
    if answer != "y":
        return []
    answers: list[Decision | None] = [None] * len(candidates)
    index = 0
    while index < len(candidates):
        item = candidates[index]
        why = item.veto or item.reason
        reply = prompt(
            f"{_clip(item.message.subject, SUBJECT_WIDTH)} — {_clip(why, 52)} "
            "[k]eep / [d]elete / [b]ack / [q]uit "
        ).strip().casefold()
        if reply == "q":
            break
        if reply == "b":
            if index > 0:
                index -= 1
                answers[index] = None
            continue
        answers[index] = (
            Decision(item, accepted=True, action="delete") if reply == "d" else None
        )
        index += 1
        if index == len(candidates) and _confirm_or_go_back(
            sum(answer is not None for answer in answers), "one", prompt
        ):
            index -= 1
            answers[index] = None
    return [answer for answer in answers if answer is not None]


def held_back(proposals: list[Proposal]) -> list[Proposal]:
    """Messages the attention guard held back, and only those.

    Deliberately narrower than "everything vetoed". A bill has its own
    handling — the point of that veto is that the invoice gets *dealt with*,
    and quietly filing it here would be the failure it exists to prevent. A
    deletion veto already has an answer in ``review_unplaced``, where binning
    is the natural verb. What is left over is the mail this loop was written
    for: flagged, or apparently awaiting a reply.
    """
    return [item for item in proposals if item.veto_kind == "attention"]


def review_held(proposals: list[Proposal], prompt: Callable[[str], str]) -> list[Decision]:
    """Step through mail the attention guard held back, one message at a time.

    The guard is right to hold this mail, and nothing here weakens it: the
    offer is declined by default, there is no batch answer, and every message
    is shown with the reason it was held and the destination the veto
    overrode. This is the override made deliberate rather than absent — the
    summary used to name these messages and give no way to act on them, so
    the same mail was reported run after run.

    Leaving is the default and records no decision, so a stray keystroke
    costs nothing. Filing requires ``held_folder``: a message the classifier
    never had a destination for has nothing to accept, and inventing one
    would be worse than declining.
    """
    candidates = held_back(proposals)
    if not candidates:
        return []
    answer = prompt(
        f"\n{len(candidates)} held back as possibly needing a reply. "
        "Go through them? [y/N] "
    ).strip().casefold()
    if answer != "y":
        return []
    answers: list[Decision | None] = [None] * len(candidates)
    index = 0
    while index < len(candidates):
        item = candidates[index]
        destination = item.held_folder or "nowhere — no folder was predicted"
        reply = prompt(
            f"{_clip(item.message.subject, SUBJECT_WIDTH)} — {_clip(item.veto or '', 44)}\n"
            f"  file → {destination}? [f]ile / [d]elete / [l]eave / [b]ack / [q]uit "
        ).strip().casefold()
        if reply == "q":
            break
        if reply == "b":
            # Nothing has moved yet, so going back is just discarding an answer.
            if index > 0:
                index -= 1
                answers[index] = None
            continue
        if reply == "f" and item.held_folder is not None:
            answers[index] = Decision(
                item, accepted=True, override_folder=item.held_folder, action="file"
            )
        elif reply == "d":
            answers[index] = Decision(item, accepted=True, action="delete")
        else:
            answers[index] = None
        index += 1
        if index == len(candidates) and _confirm_or_go_back(
            sum(answer is not None for answer in answers), "one", prompt
        ):
            index -= 1
            answers[index] = None
    return [answer for answer in answers if answer is not None]
