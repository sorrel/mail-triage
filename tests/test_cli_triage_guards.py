"""The per-message guards, proved to be wired into ``triage`` itself.

A guard that works in isolation but is not reached by the command would let
mail be filed contrary to instructions, so these drive the whole command.
"""

from __future__ import annotations

import time

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli
from mail_triage.mail_app import MailError, MailNotRunningError

from tests.cli_helpers import StubMail, stub_config, triage_fixture_with_one_strong_sender
from tests.conftest import build_fixture_db


def test_triage_vetoes_a_message_the_headers_show_is_not_bulk(tmp_path, monkeypatch):
    db_path = triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    # No List-Unsubscribe: an ordinary human sender, so the guard must hold
    # this back even though the sender's filing history is strong.
    monkeypatch.setattr(
        cli_module, "AppleScriptMail",
        lambda: StubMail(headers={900: {"Subject": "Can you take a look"}}),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code == 0
    # It must NOT appear in the filed table (its destination, "Projects",
    # never shows up) — the veto overrode a real filing decision, so proving
    # this catches a reversion to the unwired state, where this message would
    # have been filed there. It DOES appear in the summary's veto detail line
    # (that's the "must be able to see why" requirement), alongside the reason.
    assert "Projects" not in result.output
    assert "0 of 1 would be filed" in result.output
    assert "Can you take a look — looks personal, may need a reply" in result.output


def test_triage_mail_not_running_vetoes_and_warns_instead_of_crashing(tmp_path, monkeypatch):
    db_path = triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    monkeypatch.setattr(
        cli_module, "AppleScriptMail",
        lambda: StubMail(error=MailNotRunningError("Mail is not running.")),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code == 0
    assert "Projects" not in result.output
    assert "0 of 1 would be filed" in result.output
    assert "Mail is not running" in result.output
    assert "could not check whether this is bulk mail" in result.output


def test_triage_generic_header_fetch_failure_vetoes_and_warns(tmp_path, monkeypatch):
    db_path = triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    monkeypatch.setattr(
        cli_module, "AppleScriptMail",
        lambda: StubMail(error=MailError("something went wrong")),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code == 0
    assert "Projects" not in result.output
    assert "0 of 1 would be filed" in result.output
    assert "Could not check message headers" in result.output


# --- Task 11C: deletion veto must actually be wired into `triage` ----------
#
# Same shape of trap as 11B: a test only checking exit code 0 would not catch
# Classifier being constructed with no deletion_index at all. This proves a
# sender with a strong filing history who has recently only been deleted is
# held back specifically because of that, not filed on the strength of
# superseded history.

def test_triage_vetoes_a_sender_who_is_now_only_deleted(tmp_path, monkeypatch):


    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(
        db_path,
        [
            # Old, consistent filing history — outside the 75-day deletion
            # window (so build_deletion_index sees filed=0 there) but recent
            # enough, and numerous enough, to clear the sender model's own
            # confidence threshold on history alone if the deletion veto
            # didn't apply — this is what proves the veto is the reason.
            {"sender": "news@bulletin.example", "subject": "Issue 1", "date_sent": now - 400 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 2", "date_sent": now - 390 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 3", "date_sent": now - 380 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 4", "date_sent": now - 370 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 5", "date_sent": now - 360 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 6", "date_sent": now - 350 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 7", "date_sent": now - 340 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Issue 8", "date_sent": now - 330 * day,
             "mailbox_url": "imap://AAAAAAAA/Reading", "read": 1},
            # Recent behaviour: every one of the last several has been binned.
            {"sender": "news@bulletin.example", "subject": "Recent 1", "date_sent": now - 5 * day,
             "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Recent 2", "date_sent": now - 10 * day,
             "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1},
            {"sender": "news@bulletin.example", "subject": "Recent 3", "date_sent": now - 15 * day,
             "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1},
            # Currently in the inbox, awaiting classification.
            {"rowid": 700, "sender": "news@bulletin.example", "subject": "Latest issue",
             "date_sent": now - 1 * day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
        ],
    )
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: StubMail())
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code == 0
    assert "Reading" not in result.output
    assert "0 of 1 would be filed" in result.output
    assert "Latest issue — you have binned the last 3 from this sender" in result.output


def test_learn_no_drift_flag_suppresses_the_drift_report(tmp_path, monkeypatch):

    db_path = tmp_path / "Envelope Index"
    build_fixture_db(
        db_path,
        [
            {"sender": "a@shop.example", "subject": "Old", "date_sent": 1_580_000_000,
             "mailbox_url": "imap://AAAAAAAA/Old Folder", "read": 1},
            {"sender": "a@shop.example", "subject": "New", "date_sent": 1_750_000_000,
             "mailbox_url": "imap://AAAAAAAA/New Folder", "read": 1},
        ],
    )
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0
    assert "changed destination" not in result.output
