from mail_triage.envelope import EnvelopeReader, snapshot_database


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
