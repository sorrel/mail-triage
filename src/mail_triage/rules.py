"""Hard rules: what the user said when asked where a sender's mail goes.

A rule is consulted before any statistics. The classifier's stages infer
intent from filing history; a rule *is* the intent, stated directly, so it
wins over inference every time (though not over a per-message do-not-file
guard — see the precedence order in the asking-when-unsure design).

Two design consequences show up here:

- **A corrupt file is an error, never an empty dict.** Silently ignoring
  rules would file mail contrary to explicit instructions, which is precisely
  the failure this feature exists to prevent. The message names the file and
  the failing line so it can be hand-repaired.
- **Every answer is written the moment it is given**, not batched at the end,
  so interrupting the questions never loses the answers already supplied.

``bin`` became available on 27 July 2026, once invoice detection existed. Two
protections make it safe, and both are enforced elsewhere: a per-message
invoice guard outranks every rule (see ``model.classify``), and a sender who
sends invoices is never *offered* a bin answer in the first place (see
``asking``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# "file" sends the sender's mail to a folder; "bin" sends it to the Trash
# (journalled and undoable, never a hard delete); "leave" is the escape hatch
# for senders that genuinely split by content — never ask again, never
# auto-file. "bin" was deferred until invoice detection existed, since a bin
# rule on a billing sender is precisely the harm the invoice requirement names.
VALID_ACTIONS = ("file", "bin", "leave")


class RulesError(Exception):
    """The rules file exists but cannot be trusted."""


@dataclass(frozen=True)
class Rule:
    """One answer to "where does this sender's mail go?"."""

    sender: str
    action: str
    folder: str | None
    answered_at: int
    # What was on offer when the question was answered. Never consulted at
    # classification time; kept so a later "you chose Parent/Child when it was
    # 12-vs-8, and it is now 12-vs-40" review needs no re-derivation.
    candidates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "folder": self.folder,
            "answered_at": self.answered_at,
            "candidates": self.candidates,
        }


def _parse_rule(sender: str, values: object, path: Path) -> Rule:
    if not isinstance(values, dict):
        raise RulesError(f"{path}: rule for {sender} is not an object")
    action = values.get("action")
    if action not in VALID_ACTIONS:
        raise RulesError(
            f"{path}: rule for {sender} has unknown action {action!r} "
            f"(expected one of {', '.join(VALID_ACTIONS)})"
        )
    folder = values.get("folder")
    if action == "file" and not folder:
        raise RulesError(f"{path}: rule for {sender} files mail but names no folder")
    return Rule(
        sender=sender,
        action=action,
        folder=folder,
        answered_at=int(values.get("answered_at", 0)),
        candidates=dict(values.get("candidates") or {}),
    )


def load_rules(path: Path) -> dict[str, Rule]:
    """Load rules keyed by lower-cased sender address.

    A missing file means no rules and is not an error — that is the state
    before the first question is ever answered.
    """
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise RulesError(
            f"{path}: cannot parse rules at line {error.lineno}, column {error.colno} "
            f"({error.msg}). Fix or delete the file — rules are not being applied "
            "while it is unreadable."
        ) from error
    if not isinstance(payload, dict):
        raise RulesError(f"{path}: expected an object keyed by sender address")
    return {
        sender.casefold(): _parse_rule(sender.casefold(), values, path)
        for sender, values in payload.items()
    }


def save_rules(path: Path, rules: dict[str, Rule]) -> None:
    """Write the whole rules file, human-readably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {sender: rule.to_dict() for sender, rule in sorted(rules.items())}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def record_rule(path: Path, rule: Rule) -> None:
    """Add or replace one rule and write immediately.

    Written per answer rather than batched so that interrupting the questions
    keeps everything already answered.
    """
    rules = load_rules(path)
    sender = rule.sender.casefold()
    rules[sender] = Rule(
        sender=sender,
        action=rule.action,
        folder=rule.folder,
        answered_at=rule.answered_at,
        candidates=rule.candidates,
    )
    save_rules(path, rules)


def forget_rule(path: Path, sender: str) -> bool:
    """Remove a rule. Returns whether there was one to remove."""
    rules = load_rules(path)
    if sender.casefold() not in rules:
        return False
    del rules[sender.casefold()]
    save_rules(path, rules)
    return True
