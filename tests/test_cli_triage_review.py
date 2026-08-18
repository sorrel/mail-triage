"""What the review loop offers, and what ``--auto`` does without one.

Covers binning as an answer, mail the classifier could not place,
corrections captured over a proposal, and unattended filing.
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from mail_triage.cli import cli
from mail_triage.corrections import Correction, record_correction
from mail_triage.corrections import load_corrections
from mail_triage.mail_app import FakeMail
from mail_triage.rules import Rule, record_rule

from tests.cli_helpers import strong_sender_rows, stub_config, triage_fixture_with_one_strong_sender, patch_all
from tests.conftest import build_fixture_db


# --- Delete as an answer in the review loop, wired into `triage` --------------

def _prepare_live_run(tmp_path, monkeypatch, with_trash=True):
    """A run with one filable message and a real FakeMail, so the execute
    path — the code that actually moves mail — is exercised end to end.

    The Trash is added to the *database* fixture as well as to FakeMail,
    because the trash-folder check reads the account's real mailbox list from
    the envelope database, exactly as the classifier's folder list does.
    """


    db_path = triage_fixture_with_one_strong_sender(tmp_path)
    if with_trash:
        db_path.unlink()  # rebuild from scratch; build_fixture_db creates tables
        now = int(time.time())
        day = 86_400
        rows = strong_sender_rows(now, day)
        # A different sender, so this does not feed the deletion veto for the
        # message under test.
        rows.append({"sender": "someone-else@elsewhere.example", "subject": "Binned",
                     "date_sent": now - 40 * day,
                     "mailbox_url": "imap://AAAAAAAA/Deleted Messages", "read": 1})
        build_fixture_db(db_path, rows)
    mailboxes = ["Projects", "Deleted Messages"] if with_trash else ["Projects"]
    patch_all(monkeypatch, "DEFAULT_DB_PATH", db_path)
    patch_all(monkeypatch, "load_config", lambda: stub_config(tmp_path))
    mail = FakeMail(
        inbox=[900], mailboxes=list(mailboxes),
        headers={900: {"List-Unsubscribe": "<mailto:x@work.example>"}},
        keys={900: "<nine-hundred@work.example>"},
    )
    patch_all(monkeypatch, "AppleScriptMail", lambda: mail)
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


    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"
    rows = strong_sender_rows(now, day)
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
    patch_all(monkeypatch, "DEFAULT_DB_PATH", db_path)
    patch_all(monkeypatch, "load_config", lambda: stub_config(tmp_path))
    mail = FakeMail(
        inbox=[900, 950], mailboxes=["Projects", "Deleted Messages"],
        headers={900: {"List-Unsubscribe": "<mailto:x@work.example>"}},
        keys={900: "<nine-hundred@work.example>", 950: "<nine-fifty@nowhere.example>"},
    )
    patch_all(monkeypatch, "AppleScriptMail", lambda: mail)
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

    runner, _ = _prepare_live_run(tmp_path, monkeypatch)
    record_rule(
        tmp_path / "local" / "rules.json",
        Rule(sender="person@work.example", action="bin", folder=None,
             answered_at=1, candidates={}),
    )
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"], input="")
    assert "delete" in result.output.casefold()


def test_rules_lists_a_bin_rule_clearly(tmp_path, monkeypatch):

    patch_all(monkeypatch, "load_config", lambda: stub_config(tmp_path))
    record_rule(
        tmp_path / "local" / "rules.json",
        Rule(sender="junk@shop.example", action="bin", folder=None,
             answered_at=1, candidates={}),
    )
    result = CliRunner().invoke(cli, ["rules"])
    assert result.exit_code == 0
    assert "junk@shop.example" in result.output
    assert "delete" in result.output.casefold()


# --- Corrections captured during review (Task 12) -----------------------------

def test_typing_a_folder_over_a_proposal_records_a_correction(tmp_path, monkeypatch):
    """The correction signal, end to end: proposed Projects, filed Personal."""
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    # The fixture's second training folder, so it is a real mailbox in the
    # database's folder list and a real destination in the fake.
    mail._mailboxes.append("Personal")
    mail._folder_contents[("*", "Personal")] = []

    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\nPersonal\n\nn\n")

    assert result.exit_code == 0
    assert "Recorded 1 correction" in result.output
    recorded = load_corrections(stub_config(tmp_path))
    assert [(item.chosen_folder, item.rejected_folder) for item in recorded] == [
        ("Personal", "Projects")
    ]
    assert [entry[1] for entry in mail.moved] == ["Personal"]


def test_accepting_the_proposal_records_no_correction(tmp_path, monkeypatch):
    runner, _ = _prepare_unplaceable_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--no-ask"], input="s\ny\n\nn\n")
    assert result.exit_code == 0
    assert "correction" not in result.output
    assert load_corrections(stub_config(tmp_path)) == []


def test_learn_reports_the_corrections_it_folded_in(tmp_path, monkeypatch):

    runner, _ = _prepare_unplaceable_run(tmp_path, monkeypatch)
    record_correction(
        Correction(sender="stranger@nowhere.example", domain="nowhere.example",
                   subject="Unsolicited offer", chosen_folder="Personal",
                   rejected_folder=None, recorded_at=1_700_000_000),
        stub_config(tmp_path),
    )
    result = runner.invoke(cli, ["learn", "--no-drift"])
    assert result.exit_code == 0
    assert "Including 1 correction at 10× weight." in result.output


# --- Unattended filing: triage --auto (Task 14) ------------------------------

def test_auto_files_confident_mail_without_asking(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    # The fixture's filable message reads 0.80, below the 0.9 default, so the
    # threshold is lowered to put it in range rather than inflating the
    # fixture's history until it clears a number.
    patch_all(
        monkeypatch, "load_config", lambda: stub_config(tmp_path, auto_threshold=0.75)
    )
    # No input at all: an unattended run must never block on a prompt.
    result = runner.invoke(cli, ["triage", "--auto"], input="")
    assert result.exit_code == 0
    assert [entry[1] for entry in mail.moved] == ["Projects"]
    assert "Filing 1 message" in result.output
    assert "Reverse this with" in result.output


def test_auto_leaves_everything_it_cannot_place(tmp_path, monkeypatch):
    """The unplaceable message is the one --auto must not touch."""
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    patch_all(
        monkeypatch, "load_config", lambda: stub_config(tmp_path, auto_threshold=0.75)
    )
    runner.invoke(cli, ["triage", "--auto"], input="")
    assert [entry[0] for entry in mail.moved] == [900]


def test_auto_and_dry_run_together_are_refused(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--auto", "--dry-run"])
    assert result.exit_code != 0
    assert "--auto" in result.output and "--dry-run" in result.output
    assert mail.moved == []


def test_auto_does_not_ask_about_uncertain_senders(tmp_path, monkeypatch):
    """Asking is a conversation, and there is nobody there to have it."""
    runner, _ = _prepare_unplaceable_run(tmp_path, monkeypatch)
    patch_all(
        monkeypatch, "load_config", lambda: stub_config(tmp_path, auto_threshold=0.75)
    )
    result = runner.invoke(cli, ["triage", "--auto"], input="")
    assert result.exit_code == 0
    assert "senders I can't call" not in result.output


def test_auto_with_nothing_confident_enough_moves_nothing(tmp_path, monkeypatch):
    runner, mail = _prepare_unplaceable_run(tmp_path, monkeypatch)
    patch_all(
        monkeypatch, "load_config", lambda: stub_config(tmp_path, auto_threshold=1.01)
    )
    result = runner.invoke(cli, ["triage", "--auto"], input="")
    assert result.exit_code == 0
    assert mail.moved == []
    assert "Nothing was confident enough" in result.output
