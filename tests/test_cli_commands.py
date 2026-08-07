"""The commands that do not move mail: ``accounts``, ``learn``, ``rules``, ``size``.

All of them read a fixture Envelope Index rather than the real one, and
``load_config`` is stubbed so no real ``local/config.toml`` is required.
"""

from __future__ import annotations

import time

from click.testing import CliRunner
from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage import cli as cli_module
from mail_triage.cli import cli
from mail_triage.envelope import SnapshotError
from mail_triage.rules import Rule, load_rules, record_rule
from mail_triage.rules import Rule, record_rule

from tests.cli_helpers import StubMail, stub_config
from tests.conftest import build_fixture_db


def test_accounts_success_path(fixture_db, monkeypatch):
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", fixture_db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts"])
    assert result.exit_code == 0
    assert "imap://AAAAAAAA" in result.output
    assert "local://BBBBBBBB" in result.output


def test_accounts_table_shows_placeholders_when_no_names(fixture_db, monkeypatch):
    # Neither fixture account has a matching Mail account: the imap:// one
    # should read "(not in Mail)" and the local:// one "On My Mac".
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", fixture_db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts"])
    assert result.exit_code == 0
    assert "On My Mac" in result.output
    assert "(not in Mail)" in result.output


def test_accounts_table_shows_resolved_name(fixture_db, monkeypatch):
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", fixture_db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {"AAAAAAAA": "Test Account"})
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts"])
    assert result.exit_code == 0
    assert "Test Account" in result.output


def test_accounts_missing_database(tmp_path, monkeypatch):
    missing = tmp_path / "Envelope Index"
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", missing)
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts"])
    assert result.exit_code != 0
    assert "Cannot find" in result.output
    assert "Full Disk Access" not in result.output


def test_accounts_permission_denied(tmp_path, monkeypatch):
    def raise_permission_error(source, dest_dir):
        raise PermissionError("denied")

    # A restricted (no Full Disk Access) path also fails Path.exists(), which
    # swallows PermissionError and returns False — the same shape as a
    # genuinely missing database. DEFAULT_DB_PATH must point at a path that
    # does not exist so a fix relying on `.exists()` as a pre-check cannot
    # accidentally pass this test.
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", tmp_path / "Envelope Index")
    monkeypatch.setattr(cli_module, "snapshot_database", raise_permission_error)
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts"])
    assert result.exit_code != 0
    assert "Full Disk Access" in result.output
    assert "Cannot find" not in result.output


def test_a_database_that_never_settles_is_reported_not_thrown(tmp_path, monkeypatch):
    """A raced snapshot is a condition to explain, not a traceback."""
    def raise_snapshot_error(source, dest_dir):
        raise SnapshotError("Mail checkpointed its database every time")

    monkeypatch.setattr(cli_module, "snapshot_database", raise_snapshot_error)
    result = CliRunner().invoke(cli, ["accounts"])
    assert result.exit_code != 0
    assert "checkpointed its database" in result.output
    assert not isinstance(result.exception, SnapshotError)


def test_learn_reports_counts_and_writes_the_model(fixture_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", fixture_db)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["learn"])
    assert result.exit_code == 0
    assert "Trained on 2 filed messages." in result.output
    assert "Known senders: 1" in result.output
    assert "Known domains: 1" in result.output
    model_path = tmp_path / "local" / "model.json"
    assert "Model written to" in result.output
    assert model_path.exists()


def test_learn_shows_drift_by_default(tmp_path, monkeypatch):

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
    result = runner.invoke(cli, ["learn"])
    assert result.exit_code == 0
    assert "senders changed destination over time" in result.output
    assert "a@shop.example" in result.output
    assert "old folder" in result.output
    assert "new folder" in result.output


def test_triage_dry_run_reports_placed_and_unplaced_without_moving(tmp_path, monkeypatch):


    # Recency weighting decays with a 365-day half-life, so historical
    # examples need to be close to "now" or the sender-confidence prior
    # damping never clears confidence_threshold regardless of consistency.
    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(
        db_path,
        [
            # History: orders@shop.example is filed to Orders consistently.
            {"sender": "orders@shop.example", "subject": "Old order", "date_sent": now - 30 * day,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "orders@shop.example", "subject": "Another order", "date_sent": now - 20 * day,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "orders@shop.example", "subject": "Yet another order", "date_sent": now - 10 * day,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "orders@shop.example", "subject": "Fourth order", "date_sent": now - 5 * day,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            # Currently in the inbox: one sender with strong history, one unknown.
            {"rowid": 501, "sender": "orders@shop.example", "subject": "New order",
             "date_sent": now - 1 * day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
            {"sender": "stranger@nowhere.example", "subject": "Hello there", "date_sent": now - 1 * day,
             "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
        ],
    )
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    # The guard (Task 11B) fetches headers for "New order" since
    # orders@shop.example is not a no-reply-style address — supply
    # List-Unsubscribe so the pre-existing "would be filed" expectation
    # still holds, proving the guard ran and correctly did not veto it.
    monkeypatch.setattr(
        cli_module, "AppleScriptMail",
        lambda: StubMail(headers={501: {"List-Unsubscribe": "<mailto:x@shop.example>"}}),
    )
    runner = CliRunner()

    # Train first, as 'learn' would.
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code == 0
    assert "New order" in result.output
    assert "Orders" in result.output
    assert "would be filed" in result.output
    assert "staying in the inbox" in result.output


# --- The `rules` command ------------------------------------------------------

def test_rules_reports_when_there_are_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["rules"])
    assert result.exit_code == 0
    assert "No rules" in result.output


def test_rules_lists_what_has_been_answered(tmp_path, monkeypatch):

    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    record_rule(
        (tmp_path / "local" / "rules.json"),
        Rule(sender="news@shop.example", action="file", folder="Parent/Keep",
             answered_at=1_785_000_000, candidates={}),
    )
    result = CliRunner().invoke(cli, ["rules"])
    assert result.exit_code == 0
    assert "news@shop.example" in result.output
    assert "Parent/Keep" in result.output


def test_rules_forget_removes_a_rule(tmp_path, monkeypatch):

    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    path = tmp_path / "local" / "rules.json"
    record_rule(path, Rule(sender="news@shop.example", action="file", folder="Parent/Keep",
                           answered_at=1, candidates={}))
    result = CliRunner().invoke(cli, ["rules", "--forget", "news@shop.example"])
    assert result.exit_code == 0
    assert load_rules(path) == {}


def test_rules_forget_reports_an_unknown_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["rules", "--forget", "stranger@nowhere.example"])
    assert result.exit_code != 0
    assert "stranger@nowhere.example" in result.output


def _size_store(tmp_path):
    """A miniature V10 tree: one account with one mailbox, plus MailData."""

    store = tmp_path / "V10"
    data = store / "AAAAAAAA" / "Parent.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    db = store / "MailData" / "Envelope Index"
    db.parent.mkdir(parents=True)
    build_fixture_db(
        db,
        [
            {"sender": "a@example.com", "subject": "one", "date_sent": 1,
             "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 4096},
        ],
    )
    return db


def test_size_command_renders_grids(tmp_path, monkeypatch):


    db = _size_store(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "0"])
    assert result.exit_code == 0, result.output
    assert "Parent" in result.output
    assert "All accounts" in result.output
    assert "MailData" in result.output


def test_size_command_rejects_a_bad_min_size(tmp_path, monkeypatch):


    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "huge"])
    assert result.exit_code != 0
    assert "min-size" in result.output.lower()


def test_size_command_filters_by_account(tmp_path, monkeypatch):


    db = _size_store(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    result = CliRunner().invoke(
        cli_module.cli, ["size", "--min-size", "0", "--account", "nosuchaccount"]
    )
    assert result.exit_code != 0
    assert "no account" in result.output.lower()


def test_size_command_never_writes_to_the_real_database(tmp_path, monkeypatch):
    """The command must read a snapshot, never open the live file writable."""


    db = _size_store(tmp_path)
    before = db.read_bytes()
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    CliRunner().invoke(cli_module.cli, ["size", "--min-size", "0"])
    assert db.read_bytes() == before
