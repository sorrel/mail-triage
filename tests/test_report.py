"""The report: what the unattended runs did, security first."""

from __future__ import annotations

import time

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.journal import Journal, JournalEntry
from mail_triage.model.classify import Proposal
from mail_triage.report import recent_runs, render, summarise_run


def entry(status="moved", to_folder="Filed/Alerts", message_id=1):
    return JournalEntry(
        message_id=message_id, subject="s", from_folder="INBOX",
        to_folder=to_folder, status=status, message_key=f"<{message_id}@x.example>",
    )


def proposal(subject="Security alert: new sign-in", reason="looks security-relevant"):
    message = MessageRow(
        rowid=1, sender="alerts@vendor.example", subject=subject,
        date_sent=1, mailbox_url="imap://A/INBOX", read=False,
    )
    return Proposal(
        message, None, 0.97, "reason", "sender",
        veto=reason, veto_kind="security", held_folder="Filed/Alerts",
    )


def config(tmp_path):
    return Config(account_url_prefix="imap://A", local_dir=tmp_path)


# --- folding the journal ----------------------------------------------------

def test_a_run_is_folded_to_counts_and_destinations():
    summary = summarise_run([
        entry(message_id=1, to_folder="Filed/Alerts"),
        entry(message_id=2, to_folder="Filed/Alerts"),
        entry(message_id=3, to_folder="Filed/Orders"),
        entry(message_id=4, status="failed"),
        entry(message_id=5, status="undone"),
    ])
    assert summary.moved == 3
    assert summary.failed == 1
    assert summary.undone == 1
    assert summary.by_folder == {"Filed/Alerts": 2, "Filed/Orders": 1}


def test_a_planned_entry_counts_as_neither_moved_nor_failed():
    """"planned" means the outcome is unknown — undo checks rather than
    assumes, and so should a report."""
    summary = summarise_run([entry(status="planned")])
    assert summary.moved == 0
    assert summary.failed == 0


def test_only_runs_inside_the_window_are_read(tmp_path):
    settings = config(tmp_path)
    journal = Journal(settings)
    old = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(time.time() - 40 * 86_400))
    recent = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())
    for run_id in (f"{old}-aaaa", f"{recent}-bbbb"):
        journal.begin(run_id)
        journal.record(entry())
    found = {run.run_id for run in recent_runs(settings, since_days=7)}
    assert found == {f"{recent}-bbbb"}


def test_a_run_id_that_cannot_be_dated_is_kept(tmp_path):
    """A report that hides runs it cannot date would be worse than one that
    shows too many."""
    settings = config(tmp_path)
    journal = Journal(settings)
    journal.begin("not-a-timestamp")
    journal.record(entry())
    assert [run.run_id for run in recent_runs(settings, since_days=1)] == ["not-a-timestamp"]


# --- rendering --------------------------------------------------------------

def test_security_mail_leads_and_is_listed_in_full():
    text = render([], [proposal()], held_other=3, since_days=7)
    first = text.splitlines()[0]
    assert "security-relevant" in first
    assert "read these first" in first
    assert "Security alert: new sign-in" in text


def test_the_other_guards_are_counted_not_listed():
    """The asymmetry is the design: one of these is a list of things to read,
    the other is reassurance that the rest of the machinery ran."""
    text = render([], [], held_other=12, since_days=7)
    assert "12 more held back by the other guards." in text


def test_nothing_held_is_said_rather_than_left_blank():
    text = render([], [], held_other=0, since_days=7)
    assert "Nothing held back as security-relevant." in text


def test_an_empty_window_says_so():
    assert "No runs in the last 7 days." in render([], [], 0, since_days=7)


def test_destinations_are_totalled_across_runs():
    runs = [summarise_run([entry(message_id=n) for n in range(3)])]
    text = render(runs, [], 0, since_days=7)
    assert "1 run in the last 7 days: 3 filed" in text
    assert "Filed/Alerts" in text


def test_failures_are_named_by_run_not_merely_counted():
    """A failure means a message still sitting in the inbox with a journal
    entry saying otherwise — worth being able to find."""
    summary = summarise_run([entry(status="failed")])
    summary.run_id = "2026-08-17T09-00-00-abcd"
    text = render([summary], [], 0, since_days=7)
    assert "Runs with failures:" in text
    assert "2026-08-17T09-00-00-abcd" in text
