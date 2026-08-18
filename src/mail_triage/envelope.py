"""Read-only access to Apple Mail's Envelope Index.

Mail owns the live database. We never open it directly for anything but a
copy, and we never write to it under any circumstances.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mail_triage.folders import account_prefix

DEFAULT_DB_PATH = Path.home() / "Library" / "Mail" / "V10" / "MailData" / "Envelope Index"

_BASE_QUERY = """
    SELECT m.ROWID, a.address, s.subject, m.date_sent, m.date_received, b.url, m.read, m.flagged
    FROM messages m
    JOIN addresses a ON a.ROWID = m.sender
    JOIN subjects s ON s.ROWID = m.subject
    JOIN mailboxes b ON b.ROWID = m.mailbox
"""


@dataclass(frozen=True)
class MessageRow:
    """One message as stored in the envelope database.

    ``rowid`` is also Mail's AppleScript message id — verified against the live
    application. It is the join key between this database and Mail itself.

    ``flagged`` is an explicit do-not-file marker (Task 11B): a message the
    user flagged must never be filed away, regardless of confidence.
    """

    rowid: int
    sender: str
    subject: str
    date_sent: int
    mailbox_url: str
    read: bool
    flagged: bool = False
    # When the message reached *us*. ``date_sent`` is the sending machine's
    # clock, which is fine for ordering a correspondent's mail but not for
    # "did this arrive after we sent that": a bouncing relay with a skewed
    # clock would fall outside any window computed from it. Defaulted so
    # callers constructing rows by hand (the tests do) need not supply it.
    date_received: int = 0


SNAPSHOT_ATTEMPTS = 5


class SnapshotError(RuntimeError):
    """Mail would not hold still long enough to be copied consistently."""


def _checkpoint_state(source: Path) -> tuple[int, int, bytes]:
    """What a checkpoint disturbs: the database file, and the log's identity.

    The write-ahead log's 8-byte salt (header bytes 16–24) changes every time
    the log restarts, which is precisely what a checkpoint does. Ordinary
    commits only append frames, leaving both the database file and the salt
    alone — so this deliberately ignores the log's length.
    """
    stat = source.stat()
    salt = b""
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        with wal.open("rb") as handle:
            salt = handle.read(24)[16:24]
    return (stat.st_size, stat.st_mtime_ns, salt)


def snapshot_database(
    source: Path = DEFAULT_DB_PATH,
    dest_dir: Path | None = None,
    attempts: int = SNAPSHOT_ATTEMPTS,
) -> Path:
    """Copy the database and its write-ahead log to ``dest_dir``.

    Copying the -wal and -shm companions keeps the snapshot consistent with
    what Mail has most recently written.

    The copies are not one operation, and Mail does not pause for them. If it
    checkpoints in between, the database is copied *before* the checkpoint —
    so it lacks everything still in the log — and the log is copied *after*
    the restart, by which point it no longer carries those commits either.
    The snapshot then opens perfectly happily and is simply missing every
    recent message. That is not theoretical: a triage run on 2 August 2026
    saw 3 of 10 inbox messages, all of them the old ones, because Mail
    checkpointed during that very second.

    So the copy is verified rather than assumed: if the database file or the
    log's salt moved whilst we were copying, the snapshot is thrown away and
    taken again. The copy is an APFS clone — 265 MB in 0.05 s — so the window
    is tiny and a retry costs nothing.
    """
    if dest_dir is None:
        raise ValueError("dest_dir is required")
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / source.name
    companions = [source.with_name(source.name + suffix) for suffix in ("-wal", "-shm")]
    for _ in range(attempts):
        before = _checkpoint_state(source)
        # Any leftovers from a raced attempt: a companion that has since gone
        # would otherwise be read as though it were part of this snapshot.
        for companion in companions:
            (dest_dir / companion.name).unlink(missing_ok=True)
        shutil.copy2(source, destination)
        for companion in companions:
            if companion.exists():
                shutil.copy2(companion, dest_dir / companion.name)
        if _checkpoint_state(source) == before:
            return destination
    raise SnapshotError(
        f"Mail checkpointed its database every time it was copied "
        f"({attempts} attempts), so no consistent snapshot could be taken. "
        "Nothing was read; try again in a moment."
    )


@contextmanager
def open_snapshot(db_path: Path = DEFAULT_DB_PATH) -> Iterator["EnvelopeReader"]:
    """A reader over a fresh snapshot, closed and cleaned up afterwards.

    Every bulk read in the tool wants the same four things: a temporary
    directory, a verified snapshot into it, a reader over that, and both
    released at the end whatever happens. Written out longhand it was eight
    lines, repeated eight times, and the ``finally: reader.close()`` was the
    part most easily left off — a leaked handle on a copy inside a directory
    being deleted underneath it.

    The snapshot is thrown away with the directory, which is deliberate: it is
    a point-in-time copy, and keeping one would only invite a later read
    believing it was current.
    """
    with tempfile.TemporaryDirectory() as work:
        reader = EnvelopeReader(snapshot_database(db_path, Path(work)))
        try:
            yield reader
        finally:
            reader.close()


class EnvelopeReader:
    """Typed, read-only queries over an envelope database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def _rows(self, where: str = "", params: tuple = ()) -> Iterator[MessageRow]:
        for (
            rowid, sender, subject, date_sent, date_received, url, read, flagged
        ) in self.connection.execute(_BASE_QUERY + where, params):
            yield MessageRow(
                rowid=rowid,
                sender=sender,
                subject=subject or "",
                date_sent=date_sent or 0,
                mailbox_url=url,
                read=bool(read),
                flagged=bool(flagged),
                date_received=date_received or 0,
            )

    def all_messages(self) -> Iterator[MessageRow]:
        yield from self._rows()

    def messages_in_mailbox(self, url: str) -> Iterator[MessageRow]:
        yield from self._rows("WHERE b.url = ?", (url,))

    def inbox_messages(self, url: str) -> Iterator[MessageRow]:
        """Messages in a mailbox by primary attribution *or* by Gmail label.

        Apple Mail models Gmail's labels properly: a Gmail message's
        ``messages.mailbox`` points at "[Gmail]/All Mail" whatever labels it
        carries, and inbox membership is a row in ``labels``. Filtering on the
        mailbox URL alone therefore reports a Gmail inbox as empty — which is
        exactly what the triage scan used to do.

        The join to ``messages`` is what discards stale ``labels`` rows,
        entries whose message has gone. On the mailbox this was written
        against, 11 raw rows became the 9 messages Mail itself reports, with
        no explicit staleness handling needed.

        Generic rather than Gmail-specific: an account with no label rows
        contributes nothing to the second half, so a plain IMAP account
        behaves exactly as ``messages_in_mailbox`` does.
        """
        yield from self._rows("WHERE b.url = ?", (url,))
        try:
            labelled = self.connection.execute(
                "SELECT l.message_id FROM labels l "
                "JOIN mailboxes lb ON lb.ROWID = l.mailbox_id WHERE lb.url = ?",
                (url,),
            ).fetchall()
        except sqlite3.OperationalError:
            # No labels table: an older Mail version, or a fixture predating
            # them. Not an error — the account simply has no labels.
            return
        ids = [row[0] for row in labelled]
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        # ``b.url != ?`` keeps a message that is both attributed to the mailbox
        # and labelled with it from being yielded twice.
        yield from self._rows(
            f"WHERE m.ROWID IN ({placeholders}) AND b.url != ?", (*ids, url)
        )

    def mailbox_urls(self) -> list[str]:
        return [url for (url,) in self.connection.execute("SELECT url FROM mailboxes")]

    def account_summary(self) -> list[tuple[str, int, int]]:
        """Return (account_prefix, mailbox_count, message_count) per account."""
        boxes: dict[str, int] = {}
        for url in self.mailbox_urls():
            boxes[account_prefix(url)] = boxes.get(account_prefix(url), 0) + 1
        messages: dict[str, int] = {}
        for (url, count) in self.connection.execute(
            "SELECT b.url, COUNT(*) FROM messages m JOIN mailboxes b ON b.ROWID = m.mailbox GROUP BY b.url"
        ):
            messages[account_prefix(url)] = messages.get(account_prefix(url), 0) + count
        return sorted(
            ((prefix, boxes[prefix], messages.get(prefix, 0)) for prefix in boxes),
            key=lambda item: item[2],
            reverse=True,
        )

    def mailbox_sizes(self) -> list[tuple[str, int, int]]:
        """Return (mailbox_url, message_count, total_size) for each mailbox.

        The plain mailbox join, deliberately — not ``inbox_messages``. A Gmail
        message is *stored* once, under All Mail, whatever labels it carries,
        so unioning the labels here would count those bytes twice. Labels
        answer "what is in this inbox"; this answers "what does this mailbox
        hold", and for sizing the latter is the honest question.
        """
        return [
            (url, count, total or 0)
            for url, count, total in self.connection.execute(
                "SELECT b.url, COUNT(*), SUM(m.size) "
                "FROM messages m JOIN mailboxes b ON b.ROWID = m.mailbox "
                "GROUP BY b.url"
            )
        ]

    def attachment_names(self, rowids: Iterable[int]) -> dict[int, list[str]]:
        """Attachment filenames for the given messages, keyed by message rowid.

        Scoped to the messages actually being triaged rather than loading the
        whole table (20,653 rows on the real mailbox), and tolerant of a
        database with no attachments table at all — older Mail versions, and
        the minimal fixtures used elsewhere in the suite.
        """
        ids = list(rowids)
        if not ids:
            return {}
        names: dict[int, list[str]] = {}
        placeholders = ",".join("?" * len(ids))
        try:
            rows = self.connection.execute(
                f"SELECT message, name FROM attachments WHERE message IN ({placeholders})",
                ids,
            )
        except sqlite3.OperationalError:
            return {}
        for message_id, name in rows:
            if name:
                names.setdefault(message_id, []).append(name)
        return names

    def close(self) -> None:
        self.connection.close()
