"""Synthetic Envelope Index fixtures.

Deliberately mirrors the real schema's shape (normalised senders and subjects)
without containing any real data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def build_fixture_db(
    path: Path, rows: list[dict], stale_labels: list[tuple[int, str]] | None = None
) -> None:
    """Create a miniature Envelope Index at ``path``.

    Each row dict needs: sender, subject, date_sent, mailbox_url, read.
    ``flagged`` is optional and defaults to 0. ``labels`` is an optional list
    of mailbox URLs labelling the message, which is how Gmail inboxes work.

    ``stale_labels`` adds ``(message_id, mailbox_url)`` rows for messages that
    do not exist, as real databases carry. The ``labels`` table is created
    only when something needs it, so databases without one stay representative
    of a plain IMAP account.
    """
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '');
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT NOT NULL);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT NOT NULL);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            sender INTEGER, subject INTEGER NOT NULL,
            date_sent INTEGER, mailbox INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            flagged INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    addresses: dict[str, int] = {}
    subjects: dict[str, int] = {}
    mailboxes: dict[str, int] = {}

    def intern(table: str, column: str, cache: dict[str, int], value: str) -> int:
        if value not in cache:
            cache[value] = len(cache) + 1
            db.execute(f"INSERT INTO {table} (ROWID, {column}) VALUES (?, ?)", (cache[value], value))
        return cache[value]

    for index, row in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO messages (ROWID, sender, subject, date_sent, mailbox, read, flagged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("rowid", index),
                intern("addresses", "address", addresses, row["sender"]),
                intern("subjects", "subject", subjects, row["subject"]),
                row["date_sent"],
                intern("mailboxes", "url", mailboxes, row["mailbox_url"]),
                int(row.get("read", 0)),
                int(row.get("flagged", 0)),
            ),
        )
    if any(row.get("attachments") for row in rows):
        db.executescript(
            """
            CREATE TABLE attachments (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
                message INTEGER NOT NULL,
                attachment_id TEXT,
                name TEXT
            );
            """
        )
        for index, row in enumerate(rows, start=1):
            for name in row.get("attachments") or []:
                db.execute(
                    "INSERT INTO attachments (message, name) VALUES (?, ?)",
                    (row.get("rowid", index), name),
                )
    if any(row.get("labels") for row in rows) or stale_labels:
        # Gmail label membership. Apple Mail keeps a message's mailbox as
        # "[Gmail]/All Mail" whatever labels it carries, and records the
        # labels here — so a Gmail inbox is a set of rows in this table, not
        # a mailbox with messages attributed to it.
        db.execute("CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER)")
        for index, row in enumerate(rows, start=1):
            for url in row.get("labels") or []:
                db.execute(
                    "INSERT INTO labels (message_id, mailbox_id) VALUES (?, ?)",
                    (row.get("rowid", index), intern("mailboxes", "url", mailboxes, url)),
                )
        # Rows pointing at a message that no longer exists. Real databases
        # carry these; the join to ``messages`` is what discards them.
        for message_id, url in stale_labels or []:
            db.execute(
                "INSERT INTO labels (message_id, mailbox_id) VALUES (?, ?)",
                (message_id, intern("mailboxes", "url", mailboxes, url)),
            )
    db.commit()
    db.close()


@pytest.fixture
def gmail_db(tmp_path):
    """A Gmail-shaped account beside a plain one.

    Mirrors what the real database looks like: every Gmail message is
    attributed to "[Gmail]/All Mail", and inbox membership is a label.
    """
    path = tmp_path / "Envelope Index"
    all_mail = "imap://BBBBBBBB/%5BGmail%5D/All%20Mail"
    gmail_inbox = "imap://BBBBBBBB/INBOX"
    build_fixture_db(
        path,
        [
            {"rowid": 10, "sender": "plain@example.com", "subject": "Plain inbox",
             "date_sent": 1_700_000_000, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 1},
            {"rowid": 20, "sender": "labelled@example.com", "subject": "In the Gmail inbox",
             "date_sent": 1_700_000_000, "mailbox_url": all_mail, "read": 1,
             "labels": [gmail_inbox]},
            {"rowid": 21, "sender": "archived@example.com", "subject": "Not in the inbox",
             "date_sent": 1_700_000_000, "mailbox_url": all_mail, "read": 1},
        ],
        stale_labels=[(99, gmail_inbox)],
    )
    return path


@pytest.fixture
def attachment_db(tmp_path):
    """Three messages: one with a bill attached, one with ordinary files, one bare."""
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"rowid": 1, "sender": "accounts@shop.example", "subject": "Order confirmation",
             "date_sent": 1_700_000_000, "mailbox_url": "imap://AAAAAAAA/INBOX",
             "attachments": ["Invoice-424102.pdf"]},
            {"rowid": 2, "sender": "friend@example.com", "subject": "Holiday",
             "date_sent": 1_700_000_000, "mailbox_url": "imap://AAAAAAAA/INBOX",
             "attachments": ["beach.jpg", "notes.txt"]},
            {"rowid": 3, "sender": "news@example.com", "subject": "Digest",
             "date_sent": 1_700_000_000, "mailbox_url": "imap://AAAAAAAA/INBOX"},
        ],
    )
    return path


@pytest.fixture
def fixture_db(tmp_path):
    """A small database: two accounts, overlapping folder names."""
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "orders@shop.example", "subject": "Your order", "date_sent": 1_700_000_000,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "orders@shop.example", "subject": "Dispatched", "date_sent": 1_700_100_000,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "news@list.example", "subject": "Weekly digest", "date_sent": 1_700_200_000,
             "mailbox_url": "local://BBBBBBBB/Newsletters", "read": 0},
            {"sender": "someone@work.example", "subject": "Standup notes", "date_sent": 1_700_300_000,
             "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
        ],
    )
    return path
