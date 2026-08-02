"""Tests for the ``accounts`` and ``learn`` CLI commands.

Uses Click's CliRunner and monkeypatches ``mail_triage.cli.DEFAULT_DB_PATH``
(and, for the permission case, ``snapshot_database``) so the real Envelope
Index is never touched. ``learn`` tests also monkeypatch ``load_config`` so
the real ``local/config.toml`` is never required or touched.
"""

from __future__ import annotations

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli
from mail_triage.config import Config
from mail_triage.envelope import SnapshotError
from mail_triage.mail_app import MailError, MailNotRunningError


class StubMail:
    """Fake stand-in for ``AppleScriptMail`` (Task 11B guard wiring tests).

    Never touches real mail or shells out to ``osascript`` — headers are
    supplied directly, or a chosen error is raised, to prove the guard
    genuinely runs (and fails safe) without needing a live bridge.
    """

    def __init__(self, headers: dict[int, dict[str, str]] | None = None, error: Exception | None = None):
        self._headers = headers or {}
        self._error = error

    def message_headers(self, message_id: int) -> dict[str, str]:
        if self._error is not None:
            raise self._error
        return dict(self._headers.get(message_id, {}))


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


def _stub_config(tmp_path):
    return Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")


def test_learn_reports_counts_and_writes_the_model(fixture_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", fixture_db)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    from tests.conftest import build_fixture_db

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
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["learn"])
    assert result.exit_code == 0
    assert "senders changed destination over time" in result.output
    assert "a@shop.example" in result.output
    assert "old folder" in result.output
    assert "new folder" in result.output


def test_triage_dry_run_reports_placed_and_unplaced_without_moving(tmp_path, monkeypatch):
    import time

    from tests.conftest import build_fixture_db

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
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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


# --- Task 11B: the do-not-file guard must actually be wired into `triage` -----
#
# A test that only checks the command exits zero would not catch a reversion
# to the unwired state (Classifier constructed with no `guard` at all) — the
# defect the coordinator's review caught. These assert the guard's effect on
# the outcome: a message with real filing history is held back specifically
# because its headers prove it isn't bulk, or because Mail can't be reached.

def _triage_fixture_with_one_strong_sender(tmp_path):
    import time

    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    from tests.conftest import build_fixture_db

    build_fixture_db(db_path, _strong_sender_rows(now, day))
    return db_path


def _strong_sender_rows(now, day):
    return [
        {"sender": "person@work.example", "subject": "Old thread", "date_sent": now - 30 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Another thread", "date_sent": now - 20 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Yet another", "date_sent": now - 10 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Fourth", "date_sent": now - 5 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"rowid": 900, "sender": "person@work.example", "subject": "Can you take a look",
         "date_sent": now - 1 * day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
    ]


def test_triage_vetoes_a_message_the_headers_show_is_not_bulk(tmp_path, monkeypatch):
    db_path = _triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    db_path = _triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    db_path = _triage_fixture_with_one_strong_sender(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    import time

    from tests.conftest import build_fixture_db

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
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    from tests.conftest import build_fixture_db

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
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0
    assert "changed destination" not in result.output


# --- Asking about uncertain senders, wired into `triage` ----------------------
#
# A test that only checks the questions are *printed* would not catch the
# defect that matters: an answer that is recorded but never applied, leaving
# the message in the inbox anyway. These assert the answer changes this run's
# outcome, and that the rule survives to the next one.

def _split_sender_fixture(tmp_path):
    """History where one sender is split between two folders, so the
    classifier cannot call it — the exact case a question exists to settle."""
    import time

    from tests.conftest import build_fixture_db

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
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    monkeypatch.setattr(
        cli_module, "AppleScriptMail",
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


# --- The `rules` command ------------------------------------------------------

def test_rules_reports_when_there_are_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["rules"])
    assert result.exit_code == 0
    assert "No rules" in result.output


def test_rules_lists_what_has_been_answered(tmp_path, monkeypatch):
    from mail_triage.rules import Rule, record_rule

    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
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
    from mail_triage.rules import Rule, load_rules, record_rule

    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    path = tmp_path / "local" / "rules.json"
    record_rule(path, Rule(sender="news@shop.example", action="file", folder="Parent/Keep",
                           answered_at=1, candidates={}))
    result = CliRunner().invoke(cli, ["rules", "--forget", "news@shop.example"])
    assert result.exit_code == 0
    assert load_rules(path) == {}


def test_rules_forget_reports_an_unknown_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["rules", "--forget", "stranger@nowhere.example"])
    assert result.exit_code != 0
    assert "stranger@nowhere.example" in result.output


# --- Delete as an answer in the review loop, wired into `triage` --------------

def _prepare_live_run(tmp_path, monkeypatch, with_trash=True):
    """A run with one filable message and a real FakeMail, so the execute
    path — the code that actually moves mail — is exercised end to end.

    The Trash is added to the *database* fixture as well as to FakeMail,
    because the trash-folder check reads the account's real mailbox list from
    the envelope database, exactly as the classifier's folder list does.
    """
    import time

    from mail_triage.mail_app import FakeMail
    from tests.conftest import build_fixture_db

    db_path = _triage_fixture_with_one_strong_sender(tmp_path)
    if with_trash:
        db_path.unlink()  # rebuild from scratch; build_fixture_db creates tables
        now = int(time.time())
        day = 86_400
        rows = _strong_sender_rows(now, day)
        # A different sender, so this does not feed the deletion veto for the
        # message under test.
        rows.append({"sender": "someone-else@elsewhere.example", "subject": "Binned",
                     "date_sent": now - 40 * day,
                     "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1})
        build_fixture_db(db_path, rows)
    mailboxes = ["Projects", "Deleted Messages"] if with_trash else ["Projects"]
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    mail = FakeMail(
        inbox=[900], mailboxes=list(mailboxes),
        headers={900: {"List-Unsubscribe": "<mailto:x@work.example>"}},
        keys={900: "<nine-hundred@work.example>"},
    )
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner, mail


def test_triage_bins_a_message_when_you_answer_delete(tmp_path, monkeypatch):
    runner, mail = _prepare_live_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nd\n\n")
    assert result.exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["Deleted Messages"]


def test_binning_is_reported_separately_from_filing(tmp_path, monkeypatch):
    runner, _ = _prepare_live_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nd\n\n")
    assert "binned" in result.output.casefold()


def test_a_binned_message_can_be_undone(tmp_path, monkeypatch):
    runner, mail = _prepare_live_run(tmp_path, monkeypatch)
    assert runner.invoke(cli, ["triage", "--no-ask"], input="s\nd\n\n").exit_code == 0
    mail.moved.clear()
    result = runner.invoke(cli, ["undo"])
    assert result.exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["INBOX"]


def test_a_missing_trash_mailbox_is_refused_before_anything_is_binned(tmp_path, monkeypatch):
    runner, mail = _prepare_live_run(tmp_path, monkeypatch, with_trash=False)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nd\n\n")
    assert result.exit_code != 0
    assert "Deleted Messages" in result.output
    assert mail.moved == []


def test_a_missing_trash_mailbox_does_not_block_ordinary_filing(tmp_path, monkeypatch):
    """Only a run that actually bins something needs the Trash to exist."""
    runner, mail = _prepare_live_run(tmp_path, monkeypatch, with_trash=False)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\ny\n\n")
    assert result.exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["Projects"]


# --- Binning mail the classifier could not place, wired into `triage` ---------

def _prepare_unplaceable_run(tmp_path, monkeypatch):
    """One inbox message from a sender with no history at all — unplaceable,
    and therefore only reachable through the binning pass."""
    import time

    from mail_triage.mail_app import FakeMail
    from tests.conftest import build_fixture_db

    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    rows = _strong_sender_rows(now, day)
    rows.append({"sender": "someone-else@elsewhere.example", "subject": "Binned",
                 "date_sent": now - 40 * day,
                 "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1})
    # A second training folder with equal weight. With only one folder in the
    # history, stage B would place *any* subject there at full confidence —
    # true of the fixture, not of a real mailbox, and it would make the
    # "unplaceable" premise of these tests false for the wrong reason.
    for index in range(4):
        rows.append({"sender": "family@home.example", "subject": f"Kitchen plans {index}",
                     "date_sent": now - (index + 5) * day,
                     "mailbox_url": "imap://AAAAAAAA/Personal", "read": 1})
    rows.append({"rowid": 950, "sender": "stranger@nowhere.example",
                 "subject": "Unsolicited offer", "date_sent": now - day,
                 "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0})
    build_fixture_db(db_path, rows)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    mail = FakeMail(
        inbox=[900, 950], mailboxes=["Projects", "Deleted Messages"],
        headers={900: {"List-Unsubscribe": "<mailto:x@work.example>"}},
        keys={900: "<nine-hundred@work.example>", 950: "<nine-fifty@nowhere.example>"},
    )
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner, mail


def test_triage_offers_to_bin_messages_it_could_not_place(tmp_path, monkeypatch):
    runner, _ = _prepare_unplaceable_run(tmp_path, monkeypatch)
    # Reject the filable one, then decline the binning offer.
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nn\n\nn\n")
    assert result.exit_code == 0
    assert "stayed in the inbox" in result.output


def test_an_unplaceable_message_can_be_binned(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nn\n\ny\nd\n\n")
    assert result.exit_code == 0
    assert [entry[0] for entry in mail.moved] == [950]
    assert [entry[1] for entry in mail.moved] == ["Deleted Messages"]


def test_binning_an_unplaceable_message_can_be_undone(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    assert runner.invoke(cli, ["triage", "--no-ask"], input="s\nn\n\ny\nd\n\n").exit_code == 0
    mail.moved.clear()
    assert runner.invoke(cli, ["undo"]).exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["INBOX"]


def test_declining_the_binning_offer_moves_nothing(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nn\n\nn\n")
    assert result.exit_code == 0
    assert mail.moved == []


def test_the_binning_pass_is_skipped_on_a_dry_run(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"], input="y\nd\n")
    assert result.exit_code == 0
    assert mail.moved == []
    assert "stayed in the inbox" not in result.output


def test_a_bin_rule_bins_the_senders_mail_on_the_next_run(tmp_path, monkeypatch):
    """End to end: answer 'bin these', and the message goes to the Trash."""
    from mail_triage.rules import Rule, record_rule

    runner, mail = _prepare_live_run(tmp_path, monkeypatch)
    record_rule(
        tmp_path / "local" / "rules.json",
        Rule(sender="person@work.example", action="bin", folder=None,
             answered_at=1, candidates={}),
    )
    result = runner.invoke(cli, ["triage", "--no-ask"], input="a\nn\n")
    assert result.exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["Deleted Messages"]


def test_a_bin_rule_shows_the_bin_as_the_destination(tmp_path, monkeypatch):
    from mail_triage.rules import Rule, record_rule

    runner, _ = _prepare_live_run(tmp_path, monkeypatch)
    record_rule(
        tmp_path / "local" / "rules.json",
        Rule(sender="person@work.example", action="bin", folder=None,
             answered_at=1, candidates={}),
    )
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"], input="")
    assert "delete" in result.output.casefold()


def test_rules_lists_a_bin_rule_clearly(tmp_path, monkeypatch):
    from mail_triage.rules import Rule, record_rule

    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    record_rule(
        tmp_path / "local" / "rules.json",
        Rule(sender="junk@shop.example", action="bin", folder=None,
             answered_at=1, candidates={}),
    )
    result = CliRunner().invoke(cli, ["rules"])
    assert result.exit_code == 0
    assert "junk@shop.example" in result.output
    assert "delete" in result.output.casefold()


# --- Triaging several sources ---------------------------------------------------

def _two_source_config(tmp_path):
    from mail_triage.config import Source
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
    import time

    from mail_triage.mail_app import FakeMail
    from tests.conftest import build_fixture_db

    now = int(time.time())
    day = 86_400
    rows = _strong_sender_rows(now, day)
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
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _two_source_config(tmp_path))
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
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
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
    from mail_triage.config import Source
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
    import time

    from mail_triage.mail_app import FakeMail
    from tests.conftest import build_fixture_db

    now = int(time.time())
    day = 86_400
    rows = _strong_sender_rows(now, day)
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
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _three_source_config(tmp_path))
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
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
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


# --- The size command ---------------------------------------------------------


def _size_store(tmp_path):
    """A miniature V10 tree: one account with one mailbox, plus MailData."""
    from tests.conftest import build_fixture_db

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
    from click.testing import CliRunner

    from mail_triage import cli as cli_module

    db = _size_store(tmp_path)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "0"])
    assert result.exit_code == 0, result.output
    assert "Parent" in result.output
    assert "All accounts" in result.output
    assert "MailData" in result.output


def test_size_command_rejects_a_bad_min_size(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from mail_triage import cli as cli_module

    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "huge"])
    assert result.exit_code != 0
    assert "min-size" in result.output.lower()


def test_size_command_filters_by_account(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from mail_triage import cli as cli_module

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
    from click.testing import CliRunner

    from mail_triage import cli as cli_module

    db = _size_store(tmp_path)
    before = db.read_bytes()
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    CliRunner().invoke(cli_module.cli, ["size", "--min-size", "0"])
    assert db.read_bytes() == before
