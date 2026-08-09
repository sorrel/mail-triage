"""Record every intended move before it happens, so any run can be reversed.

The journal is the tool's safety net: mail-triage is trusted to move unread
mail out of the inbox, and the journal is what makes that acceptable. Every
entry is written with status ``"planned"`` *before* the corresponding move is
attempted, so a batch that dies half-way still has a complete, reconstructible
record of what it meant to do — not just what it managed to finish.

Status values:

- ``planned`` — the move was about to be attempted; the outcome is unknown
  until proven otherwise (see ``undo_run``).
- ``moved``   — the move was attempted and the caller confirmed it succeeded.
- ``failed``  — the move was attempted and confirmed to have failed.
- ``undone``  — a prior ``moved``/``planned`` entry has been reversed by undo.
- ``unmoved`` — undo checked a ``planned`` entry and found the move never
  actually happened (the message was never in ``to_folder``), so there was
  nothing to reverse.
"""

from __future__ import annotations

import json
import secrets
import time
import warnings
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from mail_triage.config import Config
from mail_triage.mail_app import MailError, MailInterface, MessageNotFoundError


@dataclass
class JournalEntry:
    message_id: int
    subject: str
    from_folder: str
    to_folder: str
    status: str  # planned | moved | failed | undone | unmoved
    # The RFC-822 Message-ID, captured before the move whilst the message is
    # still findable by ``message_id`` (the numeric AppleScript id). It is the
    # only identifier that survives a move: the numeric id changes when a
    # message is moved and moving it back does not restore the old value, so
    # ``message_id`` alone cannot be used to find the message again for undo.
    # Defaults to "" so a journal written before this field existed still
    # loads — see ``undo_run``, which treats an empty key as unreversable and
    # warns rather than trusting the (by-then meaningless) numeric id.
    message_key: str = ""
    # The Mail account names either end of the move. Filing Gmail into the
    # iCloud folder structure means source and destination are in different
    # accounts, and undo must return the message to the one it came from —
    # a journal that recorded only folder names would look in the wrong
    # place. Both default to "" so journals written before cross-account
    # support still load; ``undo_run`` then falls back to the account it was
    # given, which is exactly the old single-account behaviour.
    from_account: str = ""
    to_account: str = ""


def new_run_id() -> str:
    """A run id that is both unique and lexicographically sortable by time.

    The suffix is what makes the first half of that sentence true. Second
    resolution alone does not: two runs starting in the same second shared a
    journal file, and because ``load`` folds repeated entries for a message
    down to the last one written, one run's ``failed`` overwrote the other's
    ``moved``. Undo then skipped messages that really had moved.

    That is not hypothetical — it happened on 9 August 2026, when a browser
    Apply was pressed twice and two runs landed in the same second. Four
    messages moved; the journal said two of them had failed, so undo would
    not have put them back.
    """
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())
    return f"{stamp}-{secrets.token_hex(2)}"


def _journal_path(config: Config, run_id: str) -> Path:
    return config.journal_dir / f"{run_id}.jsonl"


class Journal:
    """Append-only JSONL log, one file per run.

    Each call to ``record()``/``mark()`` appends a full ``JournalEntry`` as one
    line and flushes Python's write buffer to the OS immediately (no
    ``os.fsync``, so this is a defence against a killed process, not against
    power loss). Only the single most recent write can be lost or land as a
    truncated line — every entry before it is complete on disk. ``load()``
    treats a line it cannot parse the same way: skip that one line (with a
    warning), keep everything either side of it. Repeated entries for the
    same ``message_id`` fold down to the last
    one written (last-write-wins), which is how ``mark()`` "updates" a status
    without rewriting history: it appends a new line rather than editing one
    in place, so nothing already on disk is ever touched.

    A single ``Journal`` instance keeps an in-memory copy of the latest entry
    per message, populated from disk in ``begin()`` and kept in sync by every
    ``record()``/``mark()`` call after that. This is what makes ``mark()``
    cheap to call once per message in a long batch: without it, ``mark()``
    would have to re-read and re-parse the whole file — most of it unchanged
    from the previous call — turning an O(n) batch into O(n^2). The on-disk
    log is unaffected either way; the cache only changes how a *single*
    instance answers ``entries()`` between writes. Other ``Journal`` instances
    (e.g. the one ``undo_run`` creates) always see the truth via ``load()``,
    which re-reads from disk and is the correct choice there since it is used
    once, not once per message.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.run_id: str | None = None
        self._cache: dict[int, JournalEntry] = {}

    def _path(self, run_id: str) -> Path:
        return _journal_path(self.config, run_id)

    def begin(self, run_id: str) -> None:
        """Start (or resume) a run. Existing entries, if any, seed the cache
        so a process that restarts mid-batch continues from an accurate view
        rather than one that thinks nothing has happened yet."""
        self.run_id = run_id
        self.config.journal_dir.mkdir(parents=True, exist_ok=True)
        self._path(run_id).touch(exist_ok=True)
        self._cache = {entry.message_id: entry for entry in self.load(run_id)}

    def _append(self, entry: JournalEntry) -> None:
        if self.run_id is None:
            raise RuntimeError("begin() must be called before record()/mark()")
        with self._path(self.run_id).open("a") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")
        self._cache[entry.message_id] = entry

    def record(self, entry: JournalEntry) -> None:
        self._append(entry)

    def mark(self, message_id: int, status: str) -> None:
        """Append a status update for a previously record()-ed message.

        Raises ``KeyError`` if the message was never recorded in this run —
        marking an unknown id would either silently create a half-formed
        entry (missing subject/folders) or silently do nothing, and either
        failure mode could hide a message that the journal never wrote down
        in the first place. An error here is the safe outcome.
        """
        if self.run_id is None:
            raise RuntimeError("begin() must be called before record()/mark()")
        try:
            entry = self._cache[message_id]
        except KeyError:
            raise KeyError(
                f"No journal entry for message {message_id} in run {self.run_id!r}"
            ) from None
        self._append(replace(entry, status=status))

    def load(self, run_id: str) -> list[JournalEntry]:
        """Read a run's entries from disk, folded to one (the latest) per message.

        A line that cannot be parsed — most plausibly the last line of the
        file, truncated by a process killed mid-write — is skipped rather
        than raising. The alternative (letting the exception propagate) would
        take down undo for the *entire* run, including every entry that was
        written cleanly before the bad line, which is precisely the failure
        this journal exists to prevent. A ``RuntimeWarning`` is still raised
        for each skipped line so the truncation is not silently invisible —
        callers (the CLI, tests) can see or assert on it, but a run with one
        damaged line still recovers every entry that survived intact.
        """
        path = self._path(run_id)
        if not path.exists():
            return []
        latest: dict[int, JournalEntry] = {}
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = JournalEntry(**json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                warnings.warn(
                    f"Skipping unreadable line {line_number} in journal for run "
                    f"{run_id!r} ({path}): {error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            latest[entry.message_id] = entry
        return list(latest.values())

    def entries(self) -> list[JournalEntry]:
        return list(self._cache.values()) if self.run_id else []


def list_runs(config: Config) -> list[str]:
    """Run ids, newest first. Sortable because ``new_run_id`` is a timestamp."""
    if not config.journal_dir.exists():
        return []
    return sorted((path.stem for path in config.journal_dir.glob("*.jsonl")), reverse=True)


def undo_run(
    run_id: str, config: Config, mail: MailInterface, account: str
) -> tuple[int, int]:
    """Move every message from a run back to where it started.

    Only ``planned`` and ``moved`` entries are candidates — ``undone``,
    ``unmoved`` and ``failed`` entries are all terminal from a previous
    pass, and skipping them is what makes running undo twice safe: the
    second pass finds nothing left to act on and reverses nothing further.

    ``moved`` entries are the confirmed case: the move happened, so reverse
    it (``to_folder`` -> ``from_folder``).

    ``planned`` entries are the partial-failure case — the process may have
    died between the move succeeding and the journal being marked "moved".
    Rather than trust the status, the reversal is *attempted* exactly as for
    a "moved" entry:

    - if the message is found in ``to_folder``, the move really did happen;
      reversing it recovers the message rather than leaving it stranded
      wherever it silently ended up.
    - if it is not found there, the move most likely never happened at all
      (the message is presumably still wherever it started). There is
      nothing to reverse, so this is not counted as a failure — but the
      entry is still marked "unmoved" rather than left at "planned" forever,
      so it stays visible in the journal instead of looking unresolved.

    A failure other than "message not found while reversing a moved entry"
    (e.g. Mail is not running) leaves the entry's status exactly as it was,
    so a later retry of undo_run can still act on it — a permanent status
    would make a transient failure (Mail closed, network hiccup) impossible
    to recover from without editing the journal by hand.

    An entry with no ``message_key`` (a journal written before this field
    existed) cannot be reversed at all: the numeric ``message_id`` recorded
    at move time is worthless by the time undo runs, since moving a message
    changes its numeric id and moving it back does not restore the old
    value. Such entries are skipped with a warning naming the message and
    the reason, and counted as failed — not silently left alone, and not
    crashed on, but honestly reported as unreversable.
    """
    path = _journal_path(config, run_id)
    if not path.exists():
        raise FileNotFoundError(f"No journal for run {run_id!r}")

    journal = Journal(config)
    journal.begin(run_id)

    reversed_count = 0
    failed = 0
    for entry in journal.load(run_id):
        if entry.status not in ("planned", "moved"):
            continue
        if not entry.message_key:
            warnings.warn(
                f"Cannot undo message {entry.message_id} ({entry.subject!r}) in run "
                f"{run_id!r}: no message_key was recorded, so the message can no "
                "longer be reliably located (its numeric id changes on every move). "
                "This entry predates durable-key tracking and cannot be reversed.",
                RuntimeWarning,
                stacklevel=2,
            )
            failed += 1
            continue
        try:
            mail.move_message(
                entry.message_id,
                entry.from_folder,
                entry.from_account or account,
                source_folder=entry.to_folder,
                message_key=entry.message_key,
                # Where the message is *now*. For a cross-account filing this
                # is a different account from the one it is going back to, and
                # searching the wrong one simply finds nothing.
                source_account=entry.to_account or entry.from_account or account,
            )
        except MessageNotFoundError:
            if entry.status == "planned":
                # The move never actually happened — nothing to reverse.
                journal.mark(entry.message_id, "unmoved")
            else:
                # Status said "moved" but the message isn't in to_folder —
                # something has changed the picture since (e.g. the user
                # moved it by hand). A genuine problem worth surfacing.
                failed += 1
            continue
        except MailError:
            failed += 1
            continue
        journal.mark(entry.message_id, "undone")
        reversed_count += 1
    return reversed_count, failed
