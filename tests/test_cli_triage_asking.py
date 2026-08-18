"""Asking about uncertain senders, wired into ``triage``."""

from __future__ import annotations

import time

from click.testing import CliRunner

from mail_triage.cli import cli

from tests.cli_helpers import StubMail, stub_config, patch_all
from tests.conftest import build_fixture_db


# --- Asking about uncertain senders, wired into `triage` ----------------------
#
# A test that only checks the questions are *printed* would not catch the
# defect that matters: an answer that is recorded but never applied, leaving
# the message in the inbox anyway. These assert the answer changes this run's
# outcome, and that the rule survives to the next one.

def _split_sender_fixture(tmp_path):
    """History where one sender is split between two folders, so the
    classifier cannot call it — the exact case a question exists to settle."""


    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    rows = []
    for index in range(11):
        rows.append({"sender": "news@shop.example", "subject": f"Keep {index}",
                     "date_sent": now - (index + 2) * day,
                     "mailbox_url": "imap://AAAAAAAA/Parent/Keep", "read": 1})
    for index in range(9):
        rows.append({"sender": "news@shop.example", "subject": f"Politics {index}",
                     "date_sent": now - (index + 2) * day,
                     "mailbox_url": "imap://AAAAAAAA/Parent/Reading", "read": 1})
    rows.append({"rowid": 700, "sender": "news@shop.example", "subject": "Today's bulletin",
                 "date_sent": now - day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0})
    build_fixture_db(db_path, rows)
    return db_path


def _prepare_split_run(tmp_path, monkeypatch):
    db_path = _split_sender_fixture(tmp_path)
    patch_all(monkeypatch, "DEFAULT_DB_PATH", db_path)
    patch_all(monkeypatch, "load_config", lambda: stub_config(tmp_path))
    patch_all(
        monkeypatch, "AppleScriptMail",
        lambda: StubMail(headers={700: {"List-Unsubscribe": "<mailto:x@shop.example>"}}),
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner


def test_triage_asks_about_a_sender_it_cannot_call(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run"], input="\n")
    assert result.exit_code == 0
    assert "news@shop.example" in result.output
    assert "Parent/Keep" in result.output
    assert "Parent/Reading" in result.output


def test_an_answer_files_the_message_in_the_same_run(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run"], input="1\n")
    assert result.exit_code == 0
    assert "1 of 1 would be filed" in result.output


def test_an_answer_is_remembered_for_the_next_run(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    assert runner.invoke(cli, ["triage", "--dry-run"], input="1\n").exit_code == 0
    # Second run: no input at all. If the rule were not persisted and applied,
    # the message would be unfilable again and this would read "0 of 1".
    result = runner.invoke(cli, ["triage", "--dry-run"], input="")
    assert "1 of 1 would be filed" in result.output


def test_skipping_the_question_leaves_the_message_in_the_inbox(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run"], input="\n")
    assert "0 of 1 would be filed" in result.output
    assert not (tmp_path / "local" / "rules.json").exists()


def test_no_ask_flag_suppresses_the_questions(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"], input="1\n")
    assert result.exit_code == 0
    assert "Where should this sender's mail go" not in result.output
    assert "0 of 1 would be filed" in result.output


def test_a_corrupt_rules_file_stops_triage_rather_than_filing_regardless(tmp_path, monkeypatch):
    runner = _prepare_split_run(tmp_path, monkeypatch)
    rules_path = tmp_path / "local" / "rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text("{ this is not json")
    result = runner.invoke(cli, ["triage", "--dry-run"], input="\n")
    assert result.exit_code != 0
    assert "rules" in result.output.casefold()
