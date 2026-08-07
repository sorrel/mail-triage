import shutil
import sqlite3
from pathlib import Path

import pytest

from mail_triage.envelope import (
    EnvelopeReader,
    SnapshotError,
    snapshot_database,
)

from tests.conftest import build_fixture_db


def _one_row_reader(tmp_path, **extra):
    row = {
        "sender": "someone@work.example",
        "subject": "Hello",
        "date_sent": 1_700_000_000,
        "mailbox_url": "imap://AAAAAAAA/INBOX",
        "read": 0,
    }
    row.update(extra)
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(db_path, [row])
    return EnvelopeReader(db_path)


def test_message_row_carries_date_received(tmp_path):
    """The bounce window is measured on our clock, not the sender's."""
    reader = _one_row_reader(tmp_path, date_received=1_700_000_900)
    try:
        row = next(iter(reader.all_messages()))
    finally:
        reader.close()
    assert row.date_sent == 1_700_000_000
    assert row.date_received == 1_700_000_900


def test_date_received_defaults_to_date_sent_in_fixtures(tmp_path):
    """Every existing fixture omits it; they must keep working."""
    reader = _one_row_reader(tmp_path)
    try:
        row = next(iter(reader.all_messages()))
    finally:
        reader.close()
    assert row.date_received == 1_700_000_000


def test_reads_all_messages(fixture_db):
    reader = EnvelopeReader(fixture_db)
    rows = list(reader.all_messages())
    assert len(rows) == 4
    first = next(r for r in rows if r.subject == "Your order")
    assert first.sender == "orders@shop.example"
    assert first.mailbox_url == "imap://AAAAAAAA/Orders"
    assert first.read is True


def test_messages_in_mailbox_filters(fixture_db):
    reader = EnvelopeReader(fixture_db)
    rows = list(reader.messages_in_mailbox("imap://AAAAAAAA/INBOX"))
    assert [r.subject for r in rows] == ["Standup notes"]


def test_account_summary_groups_by_account(fixture_db):
    reader = EnvelopeReader(fixture_db)
    summary = dict((prefix, (boxes, msgs)) for prefix, boxes, msgs in reader.account_summary())
    assert summary["imap://AAAAAAAA"] == (2, 3)
    assert summary["local://BBBBBBBB"] == (1, 1)


def test_snapshot_copies_database(fixture_db, tmp_path):
    dest = tmp_path / "snap"
    copied = snapshot_database(fixture_db, dest)
    assert copied.exists()
    assert copied != fixture_db
    assert list(EnvelopeReader(copied).all_messages())


def test_reader_opens_read_only(fixture_db):
    reader = EnvelopeReader(fixture_db)
    import sqlite3
    import pytest as _pytest
    with _pytest.raises(sqlite3.OperationalError):
        reader.connection.execute("DELETE FROM messages")


def test_attachment_names_are_read_for_the_given_messages(attachment_db):
    reader = EnvelopeReader(attachment_db)
    names = reader.attachment_names([1, 2])
    assert names[1] == ["Invoice-424102.pdf"]
    assert sorted(names[2]) == ["beach.jpg", "notes.txt"]


def test_messages_without_attachments_are_absent(attachment_db):
    reader = EnvelopeReader(attachment_db)
    assert 3 not in reader.attachment_names([1, 2, 3])


def test_only_the_requested_messages_are_returned(attachment_db):
    reader = EnvelopeReader(attachment_db)
    assert set(reader.attachment_names([1])) == {1}


def test_asking_for_no_messages_costs_nothing(attachment_db):
    assert EnvelopeReader(attachment_db).attachment_names([]) == {}


def test_a_database_without_an_attachments_table_yields_nothing(fixture_db):
    """Older Mail versions, and the minimal fixtures used elsewhere."""
    assert EnvelopeReader(fixture_db).attachment_names([1, 2]) == {}


# --- Gmail inboxes are labels, not mailboxes ------------------------------------
#
# Every Gmail message's ``messages.mailbox`` points at "[Gmail]/All Mail";
# inbox membership lives in ``labels``. Filtering on the mailbox URL alone
# reports a Gmail inbox as empty, which is what these pin down.

def test_inbox_messages_finds_label_only_members(gmail_db):
    reader = EnvelopeReader(gmail_db)
    found = list(reader.inbox_messages("imap://BBBBBBBB/INBOX"))
    reader.close()
    assert [r.rowid for r in found] == [20]


def test_inbox_messages_ignores_stale_label_rows(gmail_db):
    """A label row pointing at a vanished message must not invent a message."""
    reader = EnvelopeReader(gmail_db)
    found = list(reader.inbox_messages("imap://BBBBBBBB/INBOX"))
    reader.close()
    assert 99 not in [r.rowid for r in found]


def test_inbox_messages_matches_messages_in_mailbox_for_a_plain_account(gmail_db):
    """An account with no label rows must behave exactly as before."""
    reader = EnvelopeReader(gmail_db)
    plain = [r.rowid for r in reader.messages_in_mailbox("imap://AAAAAAAA/INBOX")]
    via_inbox = [r.rowid for r in reader.inbox_messages("imap://AAAAAAAA/INBOX")]
    reader.close()
    assert plain == via_inbox == [10]


def test_inbox_messages_survives_a_database_with_no_labels_table(fixture_db):
    """Older Mail versions, and every fixture that predates labels."""
    reader = EnvelopeReader(fixture_db)
    found = list(reader.inbox_messages("imap://AAAAAAAA/INBOX"))
    reader.close()
    assert [r.subject for r in found] == ["Standup notes"]


def test_inbox_messages_does_not_duplicate_a_message_in_both(tmp_path):
    """A message attributed to the mailbox *and* labelled with it appears once."""
    from tests.conftest import build_fixture_db as _build
    path = tmp_path / "Envelope Index"
    _build(
        path,
        [
            {"rowid": 1, "sender": "a@example.com", "subject": "Both",
             "date_sent": 1_700_000_000, "mailbox_url": "imap://AAAAAAAA/INBOX",
             "labels": ["imap://AAAAAAAA/INBOX"]},
        ],
    )
    reader = EnvelopeReader(path)
    found = list(reader.inbox_messages("imap://AAAAAAAA/INBOX"))
    reader.close()
    assert [r.rowid for r in found] == [1]


def test_mailbox_sizes_totals_bytes_and_counts_per_mailbox(tmp_path):
    """The size report's raw material: bytes and counts, grouped per mailbox."""
    from tests.conftest import build_fixture_db

    db = tmp_path / "Envelope Index"
    build_fixture_db(
        db,
        [
            {"sender": "a@example.com", "subject": "one", "date_sent": 1,
             "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 100},
            {"sender": "b@example.com", "subject": "two", "date_sent": 2,
             "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 250},
            {"sender": "c@example.com", "subject": "three", "date_sent": 3,
             "mailbox_url": "imap://AAAAAAAA/Parent/Child", "read": 0, "size": 40},
        ],
    )
    reader = EnvelopeReader(db)
    try:
        assert {url: (count, total) for url, count, total in reader.mailbox_sizes()} == {
            "imap://AAAAAAAA/Parent": (2, 350),
            "imap://AAAAAAAA/Parent/Child": (1, 40),
        }
    finally:
        reader.close()


class _CheckpointingWriter:
    """A stand-in for Mail: a writer that checkpoints mid-copy.

    A checkpoint restarts the write-ahead log, so a -wal copied after one no
    longer carries the commits a db copied before it is missing. That gap is
    what silently emptied a triage run of everything recent.
    """

    def __init__(self, connection, real_copy, source, times):
        self.connection = connection
        self.real_copy = real_copy
        self.source = source
        self.times = times
        self.copies = 0

    def __call__(self, src, dst, *args, **kwargs):
        result = self.real_copy(src, dst, *args, **kwargs)
        if Path(src) == self.source:
            self.copies += 1
            if self.times:
                self.times -= 1
                self.connection.execute("PRAGMA wal_checkpoint(RESTART)")
                self.connection.execute("INSERT INTO notes(body) VALUES ('after')")
                self.connection.commit()
        return result


def _wal_database(path):
    """A tiny database in WAL mode with uncheckpointed commits, as Mail's is."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=wal")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE notes (ROWID INTEGER PRIMARY KEY, body TEXT)")
    connection.commit()
    for _ in range(5):
        connection.execute("INSERT INTO notes(body) VALUES ('in the wal')")
    connection.commit()
    return connection


def _snapshot_notes(copied):
    connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        connection.close()


def test_a_checkpoint_during_the_copy_is_retried(tmp_path, monkeypatch):
    """The snapshot must never quietly lose what was in the write-ahead log."""
    source = tmp_path / "Envelope Index"
    writer = _wal_database(source)
    real_copy = shutil.copy2
    faulty = _CheckpointingWriter(writer, real_copy, source, times=1)
    monkeypatch.setattr(shutil, "copy2", faulty)

    copied = snapshot_database(source, tmp_path / "snap")

    monkeypatch.undo()
    live = writer.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    assert _snapshot_notes(copied) == live
    assert faulty.copies == 2, "the raced copy should have been taken again"
    writer.close()


def test_a_database_that_never_settles_is_an_error(tmp_path, monkeypatch):
    """Better to stop than to hand back a snapshot missing recent mail."""
    source = tmp_path / "Envelope Index"
    writer = _wal_database(source)
    faulty = _CheckpointingWriter(writer, shutil.copy2, source, times=99)
    monkeypatch.setattr(shutil, "copy2", faulty)

    with pytest.raises(SnapshotError):
        snapshot_database(source, tmp_path / "snap")

    monkeypatch.undo()
    writer.close()


def test_mail_writing_whilst_we_copy_is_not_a_race(tmp_path, monkeypatch):
    """A commit appends to the log; only a checkpoint disturbs what we copied.

    Retrying on every arriving message would spin on a busy mailbox for no
    gain: those frames sit on top of the database we already have.
    """
    source = tmp_path / "Envelope Index"
    writer = _wal_database(source)
    real_copy = shutil.copy2

    def busy_copy(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        if Path(src) == source:
            busy_copy.copies += 1
            writer.execute("INSERT INTO notes(body) VALUES ('arrived mid-copy')")
            writer.commit()
        return result

    busy_copy.copies = 0
    monkeypatch.setattr(shutil, "copy2", busy_copy)

    copied = snapshot_database(source, tmp_path / "snap")

    monkeypatch.undo()
    assert busy_copy.copies == 1, "an ordinary commit should not force a retry"
    assert _snapshot_notes(copied) >= 5
    writer.close()
