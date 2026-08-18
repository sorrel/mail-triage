"""Triaging more than one account in a single run."""

from __future__ import annotations

import time

from click.testing import CliRunner

from mail_triage.cli import cli
from mail_triage.config import Config
from mail_triage.config import Source
from mail_triage.mail_app import FakeMail

from tests.cli_helpers import strong_sender_rows, patch_all
from tests.conftest import build_fixture_db


# --- Triaging several sources ---------------------------------------------------

def _two_source_config(tmp_path):
    return Config(
        account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local",
        filing_account="iCloud", filing_account_prefix="imap://AAAAAAAA",
        sources=[
            Source(name="iCloud", prefix="imap://AAAAAAAA", inbox="INBOX",
                   trash="Deleted Messages"),
            Source(name="Gmail", prefix="imap://BBBBBBBB", inbox="INBOX",
                   trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]),
        ],
    )


def _prepare_two_sources(tmp_path, monkeypatch):
    """A database with an iCloud inbox and a Gmail inbox held as a label."""


    now = int(time.time())
    day = 86_400
    rows = strong_sender_rows(now, day)
    rows.append({
        "rowid": 700, "sender": "gmail-person@work.example", "subject": "Gmail message",
        "date_sent": now - day, "read": 1,
        "mailbox_url": "imap://BBBBBBBB/%5BGmail%5D/All%20Mail",
        "labels": ["imap://BBBBBBBB/INBOX"],
    })
    db_path = tmp_path / "Envelope Index"
    if db_path.exists():
        db_path.unlink()
    build_fixture_db(db_path, rows)
    patch_all(monkeypatch, "DEFAULT_DB_PATH", db_path)
    patch_all(monkeypatch, "load_config", lambda: _two_source_config(tmp_path))
    mail = FakeMail(
        inbox=[], mailboxes=["Projects", "Deleted Messages", "[Gmail]/Bin", "INBOX"],
        accounts={"iCloud": {"INBOX": [900]}, "Gmail": {"INBOX": [700]}},
        keys={900: "<nine-hundred@work.example>", 700: "<seven-hundred@work.example>"},
        # Bulk headers on both, so neither is vetoed as possibly-personal
        # and the table actually has rows to name accounts on.
        headers={
            900: {"List-Unsubscribe": "<mailto:x@work.example>"},
            700: {"List-Unsubscribe": "<mailto:y@work.example>"},
        },
    )
    patch_all(monkeypatch, "AppleScriptMail", lambda: mail)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner, mail


def test_source_option_rejects_an_unknown_name(tmp_path, monkeypatch):
    runner, _ = _prepare_two_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--source", "Nonesuch", "--dry-run", "--no-ask"])
    assert result.exit_code != 0
    assert "Nonesuch" in result.output
    # Names the sources it does know, so the fix is obvious from the message.
    assert "iCloud" in result.output and "Gmail" in result.output


def test_the_account_option_is_gone_from_triage(tmp_path, monkeypatch):
    runner, _ = _prepare_two_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--account", "iCloud", "--dry-run"])
    assert result.exit_code != 0
    assert "no such option" in result.output.casefold()


def test_a_dry_run_scans_every_source_and_names_the_accounts(tmp_path, monkeypatch):
    runner, mail = _prepare_two_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    assert result.exit_code == 0, result.output
    # Both inboxes were read: two messages classified, not just iCloud's one.
    assert " of 2 would be filed" in result.output
    assert "Dry run — nothing was moved." in result.output
    assert mail.moved == []


def test_the_account_column_appears_with_two_sources(tmp_path, monkeypatch):
    runner, _ = _prepare_two_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    assert "Account" in result.output
    # A real row, not just the heading: the Gmail message is named as
    # Gmail's and proposed into the iCloud tree.
    row = next(line for line in result.output.splitlines() if "Gmail message" in line)
    assert row.startswith("Gmail")
    assert "Projects" in row


def test_restricting_to_one_source_scans_only_that_inbox(tmp_path, monkeypatch):
    runner, _ = _prepare_two_sources(tmp_path, monkeypatch)
    result = runner.invoke(
        cli, ["triage", "--source", "Gmail", "--dry-run", "--no-ask"]
    )
    assert result.exit_code == 0, result.output
    assert " of 1 would be filed" in result.output
    # One source selected, so the column is pointless and must not appear.
    assert "Account" not in result.output


# --- A third source, on a different scheme --------------------------------------

def _three_source_config(tmp_path):
    return Config(
        account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local",
        filing_account="iCloud", filing_account_prefix="imap://AAAAAAAA",
        sources=[
            Source(name="iCloud", prefix="imap://AAAAAAAA", inbox="INBOX",
                   trash="Deleted Messages"),
            Source(name="Gmail", prefix="imap://BBBBBBBB", inbox="INBOX",
                   trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]),
            # Config says INBOX; the mailbox is named Inbox. The lookup is
            # case-insensitive and this proves it.
            Source(name="Exchange", prefix="ews://CCCCCCCC", inbox="INBOX",
                   trash="Deleted Items", ignore=["Conversation History"]),
        ],
    )


def _prepare_three_sources(tmp_path, monkeypatch):


    now = int(time.time())
    day = 86_400
    rows = strong_sender_rows(now, day)
    rows.append({
        "rowid": 700, "sender": "gmail-person@work.example", "subject": "Gmail message",
        "date_sent": now - day, "read": 1,
        "mailbox_url": "imap://BBBBBBBB/%5BGmail%5D/All%20Mail",
        "labels": ["imap://BBBBBBBB/INBOX"],
    })
    rows.append({
        "rowid": 800, "sender": "exchange-person@work.example",
        "subject": "Exchange message", "date_sent": now - day, "read": 1,
        "mailbox_url": "ews://CCCCCCCC/Inbox",
    })
    db_path = tmp_path / "Envelope Index"
    if db_path.exists():
        db_path.unlink()
    build_fixture_db(db_path, rows)
    patch_all(monkeypatch, "DEFAULT_DB_PATH", db_path)
    patch_all(monkeypatch, "load_config", lambda: _three_source_config(tmp_path))
    mail = FakeMail(
        inbox=[], mailboxes=["Projects", "Deleted Messages", "[Gmail]/Bin",
                             "Deleted Items", "INBOX", "Inbox"],
        accounts={"iCloud": {"INBOX": [900]}, "Gmail": {"INBOX": [700]},
                  "Exchange": {"Inbox": [800]}},
        keys={900: "<nine-hundred@work.example>", 700: "<seven-hundred@work.example>",
              800: "<eight-hundred@work.example>"},
        headers={
            900: {"List-Unsubscribe": "<mailto:x@work.example>"},
            700: {"List-Unsubscribe": "<mailto:y@work.example>"},
            800: {"List-Unsubscribe": "<mailto:z@work.example>"},
        },
    )
    patch_all(monkeypatch, "AppleScriptMail", lambda: mail)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner, mail


def test_a_dry_run_scans_all_three_inboxes(tmp_path, monkeypatch):
    runner, mail = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    assert result.exit_code == 0, result.output
    assert " of 3 would be filed" in result.output
    assert "Dry run — nothing was moved." in result.output
    assert mail.moved == []


def test_an_exchange_inbox_named_Inbox_is_found_by_a_config_saying_INBOX(tmp_path, monkeypatch):
    """Case-insensitive inbox lookup — the spec depends on it."""
    runner, _ = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--source", "Exchange", "--dry-run", "--no-ask"])
    assert result.exit_code == 0, result.output
    assert " of 1 would be filed" in result.output


def test_the_exchange_row_names_its_own_account(tmp_path, monkeypatch):
    runner, _ = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    row = next(line for line in result.output.splitlines() if "Exchange message" in line)
    assert row.startswith("Exchange")
    assert "Projects" in row
