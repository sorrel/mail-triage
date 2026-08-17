"""The security guard, proved to be wired into ``triage`` itself.

A guard that holds mail in isolation but is never reached by the command is
the defect worth testing for: it looks correct in its own test file whilst
`--auto` files a breach notice every morning. So these drive the whole
command, and assert on what came out.
"""

from __future__ import annotations

import json
import time

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli

from tests.cli_helpers import StubMail, stub_config
from tests.conftest import build_fixture_db


def alerting_sender_rows(now, day, subject):
    """A sender with a filing history strong enough for --auto to act on.

    Four filings into one folder and nothing else, which is what stage A
    needs to be confident. Without the history the message would stay in the
    inbox for want of a destination, and the test would pass whether or not
    the guard existed at all.
    """
    return [
        {"sender": "no-reply@vendor.example", "subject": f"Routine notice {n}",
         "date_sent": now - (30 - n) * day,
         "mailbox_url": "imap://AAAAAAAA/Alerts", "read": 1}
        for n in range(8)
    ] + [
        {"rowid": 900, "sender": "no-reply@vendor.example", "subject": subject,
         "date_sent": now - day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
    ]


def fixture(tmp_path, subject):
    now = int(time.time())
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(db_path, alerting_sender_rows(now, 86_400, subject))
    return db_path


def run_triage(tmp_path, monkeypatch, subject, args=("triage", "--dry-run")):
    db_path = fixture(tmp_path, subject)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    # A no-reply address settles is_bulk from the address alone, so the reply
    # guard never even fetches headers for this message. That is the whole
    # point, and it is the real-world finding these tests encode: the guards
    # that already exist have nothing to say about alert mail, so the security
    # guard is the only thing that can hold it.
    monkeypatch.setattr(
        cli_module, "AppleScriptMail", lambda: StubMail(headers={900: {}})
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    result = runner.invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_an_ordinary_subject_from_this_sender_is_filed(tmp_path, monkeypatch):
    """The control. Without it, a guard that held *everything* would pass
    every test below whilst making the tool useless."""
    output = run_triage(tmp_path, monkeypatch, "Routine notice for August")
    assert "Alerts" in output
    assert "security-relevant" not in output


def test_a_security_subject_is_held_back_by_the_command(tmp_path, monkeypatch):
    output = run_triage(tmp_path, monkeypatch, "Security alert: new sign-in")
    assert "security-relevant" in output


def test_the_security_section_leads_the_summary(tmp_path, monkeypatch):
    """Above the bills, and above everything else held. It is the one category
    whose cost is measured in hours, and a scheduled run's output may not be
    read for a day."""
    output = run_triage(tmp_path, monkeypatch, "You have been pwned in a breach")
    assert "read these first" in output


def test_an_unattended_run_does_not_file_it(tmp_path, monkeypatch):
    """The property the whole thing exists for, driven end to end. --auto
    cannot be combined with --dry-run, so this asserts on what --auto says it
    is about to do rather than on a move."""
    output = run_triage(
        tmp_path, monkeypatch, "Security alert: new sign-in", args=("triage", "--auto")
    )
    assert "Nothing was confident enough to file on its own" in output
    assert "held back as security-relevant" in output


def test_a_declared_sender_is_held_whatever_the_subject(tmp_path, monkeypatch):
    db_path = fixture(tmp_path, "Routine notice for August")
    config = stub_config(tmp_path)
    config.security_senders_path.parent.mkdir(parents=True, exist_ok=True)
    config.security_senders_path.write_text(json.dumps(["vendor.example"]))
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(
        cli_module, "AppleScriptMail", lambda: StubMail(headers={900: {}})
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    output = runner.invoke(cli, ["triage", "--dry-run"]).output
    assert "you declared this sender security-relevant" in output


def test_an_unreadable_declaration_file_stops_the_run(tmp_path, monkeypatch):
    """Silence here means security mail is filed unattended, so this is one of
    the few places the tool refuses to start rather than carrying on."""
    db_path = fixture(tmp_path, "Routine notice")
    config = stub_config(tmp_path)
    config.security_senders_path.parent.mkdir(parents=True, exist_ok=True)
    config.security_senders_path.write_text("{ not json")
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    result = runner.invoke(cli, ["triage", "--dry-run"])
    assert result.exit_code != 0
    assert "security-sender list" in result.output


# --- the command that manages the list --------------------------------------

def test_declaring_and_listing_a_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    runner = CliRunner()
    added = runner.invoke(cli, ["security", "--add", "no-reply@vendor.example"])
    assert added.exit_code == 0
    assert "will not be filed unattended" in added.output
    listed = runner.invoke(cli, ["security"])
    assert "no-reply@vendor.example" in listed.output


def test_forgetting_a_sender_that_was_never_declared_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["security", "--forget", "nobody@vendor.example"])
    assert result.exit_code != 0


def test_the_empty_list_still_explains_the_vocabulary(tmp_path, monkeypatch):
    """"No senders declared" on its own reads as "this guard is off", which
    is exactly wrong — the subject vocabulary applies regardless."""
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    output = CliRunner().invoke(cli, ["security"]).output
    assert "subject vocabulary still applies" in output
