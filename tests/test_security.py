"""The security guard: what it holds, what it refuses to let past, and why.

The property under test throughout is a single sentence — security-relevant
mail is never filed by a run nobody watched — and most of these tests exist
to check it survives the things that outrank everything else: a hard rule, a
high confidence, an unattended run.
"""

from __future__ import annotations

import json

import pytest

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Classifier, Proposal
from mail_triage.model.store import TrainedModel
from mail_triage.model.sender import SenderModel
from mail_triage.review import auto_decisions, binnable, held_back
from mail_triage.rules import Rule
from mail_triage.security import (
    SecuritySendersError,
    declare_security_sender,
    forget_security_sender,
    load_security_senders,
    security_reason,
)

NOTHING: frozenset[str] = frozenset()


def message(sender="alerts@vendor.example", subject="Hello", rowid=1, flagged=False):
    return MessageRow(
        rowid=rowid, sender=sender, subject=subject, date_sent=1_700_000_000,
        mailbox_url="imap://AAAA/INBOX", read=False, flagged=flagged,
    )


# --- the vocabulary ---------------------------------------------------------

@pytest.mark.parametrize(
    "subject",
    [
        "Security alert: new sign-in to your account",
        "Suspicious sign-in attempt blocked",
        "Your password was reset",
        "Your verification code is 123456",
        "Two-factor authentication is now on",
        "You have been pwned in a data breach",
        "CVE-2026-11111 affects one of your dependencies",
        "Critical vulnerability in a package you use",
        "New device signed in",
        "API token created for your account",
        "Your certificate expires in 7 days",
        "Phishing attempt reported",
    ],
)
def test_a_security_subject_is_held(subject):
    assert security_reason("alerts@vendor.example", subject, NOTHING) is not None


@pytest.mark.parametrize(
    "subject",
    [
        "Your order has been despatched",
        "Newsletter: what we built this month",
        "Your receipt from the corner shop",
        "Meeting notes from Tuesday",
        "Reminder: your appointment tomorrow",
    ],
)
def test_ordinary_mail_is_not_held(subject):
    assert security_reason("shop@vendor.example", subject, NOTHING) is None


def test_a_word_inside_another_word_does_not_fire():
    """The boundaries are the difference between a guard and a nuisance.

    "mfa" and "2fa" unanchored would match inside product codes and order
    references, which is a large share of the mail this tool exists to file.
    """
    assert security_reason("a@b.example", "Order ref 2FAB99 confirmed", NOTHING) is None
    assert security_reason("a@b.example", "Model MFAX-3 in stock", NOTHING) is None


def test_the_reason_names_what_fired():
    """A surprising hold has to be traceable to the word that caused it,
    otherwise the only way to tune the vocabulary is to guess."""
    reason = security_reason("a@b.example", "Security alert: sign-in", NOTHING)
    assert "security alert" in reason


# --- declared senders -------------------------------------------------------

def test_a_declared_address_is_held_whatever_the_subject():
    declared = frozenset({"alerts@vendor.example"})
    assert security_reason(
        "Vendor <alerts@vendor.example>", "Monthly newsletter", declared
    ) is not None


def test_a_declared_domain_covers_its_subdomains():
    declared = frozenset({"vendor.example"})
    assert security_reason(
        "no-reply@signin.vendor.example", "Anything at all", declared
    ) is not None


def test_a_declared_domain_is_matched_on_a_label_boundary():
    """"vendor.example" must not be satisfied by "vendor.example.attacker.example".

    A declaration is a statement about a party you trust to be telling you
    about your security. Substring matching would let anybody who can register
    a domain inherit that.
    """
    declared = frozenset({"vendor.example"})
    assert security_reason(
        "spoof@vendor.example.attacker.example", "Newsletter", declared
    ) is None


def test_declaring_and_forgetting_round_trip(tmp_path):
    path = tmp_path / "security-senders.json"
    assert declare_security_sender(path, "Alerts <alerts@vendor.example>") is True
    assert declare_security_sender(path, "alerts@vendor.example") is False
    assert load_security_senders(path) == frozenset({"alerts@vendor.example"})
    assert forget_security_sender(path, "alerts@vendor.example") is True
    assert load_security_senders(path) == frozenset()


def test_a_bare_domain_may_be_declared(tmp_path):
    path = tmp_path / "security-senders.json"
    assert declare_security_sender(path, "Vendor.Example") is True
    assert load_security_senders(path) == frozenset({"vendor.example"})


def test_a_typo_is_refused_where_it_is_made(tmp_path):
    """Storing it would be a declaration matching nothing, silently — and
    silence is the failure mode this whole module exists to prevent."""
    path = tmp_path / "security-senders.json"
    for bad in ("", "   ", "not an address", "no-dots-here"):
        with pytest.raises(SecuritySendersError):
            declare_security_sender(path, bad)


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_security_senders(tmp_path / "nothing.json") == frozenset()


def test_an_unreadable_file_stops_the_run(tmp_path):
    """Unlike never-personal, an empty set here is not the safe direction:
    it means security mail is filed unattended."""
    path = tmp_path / "security-senders.json"
    path.write_text("{ not json")
    with pytest.raises(SecuritySendersError):
        load_security_senders(path)


def test_a_file_that_is_not_a_list_is_refused(tmp_path):
    path = tmp_path / "security-senders.json"
    path.write_text(json.dumps({"alerts@vendor.example": True}))
    with pytest.raises(SecuritySendersError):
        load_security_senders(path)


# --- precedence -------------------------------------------------------------

def rule(action, folder):
    return Rule(
        sender="alerts@vendor.example", action=action, folder=folder, answered_at=0
    )


def classifier(tmp_path, rules=None, security_senders=NOTHING, folders=("Alerts",)):
    sender_model = SenderModel()
    model = TrainedModel(sender=sender_model, trained_at=0, example_count=0)
    config = Config(account_url_prefix="imap://AAAA", local_dir=tmp_path)
    return Classifier(
        model, config, list(folders), rules=rules or {},
        security_senders=security_senders,
    )


def test_a_hard_rule_cannot_file_security_mail(tmp_path):
    """A rule is about a sender; a guard is about this message. Messages win —
    and this is the case where getting the precedence wrong files a breach
    notice because of an instruction given months earlier about a newsletter.
    """
    rules = {"alerts@vendor.example": rule("file", "Alerts")}
    subject = "Security alert: new sign-in to your account"
    proposal = classifier(tmp_path, rules=rules).classify(message(subject=subject))
    assert proposal.veto_kind == "security"
    assert proposal.folder is None


def test_a_bin_rule_cannot_bin_security_mail(tmp_path):
    rules = {"alerts@vendor.example": rule("bin", None)}
    subject = "You have been pwned in a data breach"
    proposal = classifier(tmp_path, rules=rules).classify(message(subject=subject))
    assert proposal.veto_kind == "security"
    assert proposal.action == "file"


def test_the_destination_is_kept_so_the_review_can_offer_it(tmp_path):
    """Unlike the invoice guard, this one carries held_folder through: filing
    it where it would have gone is the answer most security mail wants once
    somebody has actually read it."""
    rules = {"alerts@vendor.example": rule("file", "Alerts")}
    proposal = classifier(tmp_path, rules=rules).classify(
        message(subject="Security alert: sign-in")
    )
    assert proposal.held_folder == "Alerts"


def test_flagging_is_not_weakened_by_the_security_guard(tmp_path):
    """Both hold the message; nothing is at stake but which sentence shows.
    Asserted so a later reordering cannot quietly let a flagged message file."""
    proposal = classifier(tmp_path).classify(
        message(subject="Security alert: sign-in", flagged=True)
    )
    assert proposal.folder is None
    assert proposal.veto is not None


# --- the property the whole thing exists for --------------------------------

def held(folder="Alerts", confidence=0.99, kind="security"):
    return Proposal(
        message(), None, confidence, "reason", "sender",
        veto="looks security-relevant", veto_kind=kind, held_folder=folder,
    )


def test_an_unattended_run_never_files_security_mail(tmp_path):
    config = Config(account_url_prefix="imap://AAAA", local_dir=tmp_path)
    assert auto_decisions([held(confidence=0.99)], config) == []


def test_security_mail_is_never_offered_for_binning():
    """Worse than filing it: a bin in a batch answer leaves it nowhere."""
    assert binnable([held()]) == []


def test_security_mail_is_offered_in_the_attended_review():
    """The guard's claim is about *unattended* runs. Somebody stepping through
    one message at a time, reading each reason, is the opposite of that."""
    assert len(held_back([held()])) == 1


def test_the_auto_limit_bounds_an_unattended_run(tmp_path):
    config = Config(account_url_prefix="imap://AAAA", local_dir=tmp_path, auto_limit=3)
    proposals = [
        Proposal(message(rowid=n), "Alerts", 0.90 + n / 1000, "reason", "sender")
        for n in range(10)
    ]
    decisions = auto_decisions(proposals, config)
    assert len(decisions) == 3


def test_the_cap_keeps_the_most_confident(tmp_path):
    """A bounded run should file what it was surest about, not whatever
    happened to be listed first."""
    config = Config(account_url_prefix="imap://AAAA", local_dir=tmp_path, auto_limit=2)
    proposals = [
        Proposal(message(rowid=1), "Alerts", 0.91, "r", "sender"),
        Proposal(message(rowid=2), "Alerts", 0.99, "r", "sender"),
        Proposal(message(rowid=3), "Alerts", 0.95, "r", "sender"),
    ]
    kept = {d.proposal.message.rowid for d in auto_decisions(proposals, config)}
    assert kept == {2, 3}


def test_no_cap_when_the_run_is_within_it(tmp_path):
    config = Config(account_url_prefix="imap://AAAA", local_dir=tmp_path, auto_limit=50)
    proposals = [
        Proposal(message(rowid=n), "Alerts", 0.95, "r", "sender") for n in range(5)
    ]
    assert len(auto_decisions(proposals, config)) == 5
