"""What the browser is told, and what it is deliberately not told."""

from __future__ import annotations

import json

from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.unsubscribe import UnsubscribeOption
from mail_triage.web.payloads import (
    candidates_payload,
    is_safe_target,
    outcome_payload,
    proposals_payload,
)
from mail_triage.web.session import Session

ACCOUNTS = {"imap://AAAAAAAA": "iCloud"}


def proposal(rowid=1, folder="Filed/Orders", veto=None, veto_kind=None):
    message = MessageRow(
        rowid=rowid, sender="Shop <shop@shop.example>", subject="Order confirmed",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX", read=False,
    )
    return Proposal(
        message, None if veto else folder, 0.99, "12 filings", "sender",
        veto=veto, veto_kind=veto_kind, held_folder=folder if veto else None,
    )


def option(method="mailto", target="leave@shop.example", sender="news@shop.example"):
    return UnsubscribeOption(
        sender=sender, domain=sender.rpartition("@")[2], method=method, target=target,
        message_count=2, unread_count=2, deleted_count=40, account="iCloud",
    )


def test_a_placed_proposal_carries_what_the_page_shows():
    session = Session([proposal()])
    (row,) = proposals_payload(session, ACCOUNTS)["proposals"]
    assert row["sender"] == "Shop <shop@shop.example>"
    assert row["subject"] == "Order confirmed"
    assert row["folder"] == "Filed/Orders"
    assert row["confidence"] == 0.99
    assert row["reason"] == "12 filings"
    assert row["account"] == "iCloud"
    assert row["id"] in session.entries


def test_a_vetoed_proposal_says_so_and_names_the_folder_it_would_have_used():
    session = Session([proposal(veto="looks personal, may need a reply", veto_kind="attention")])
    (row,) = proposals_payload(session, ACCOUNTS)["proposals"]
    assert row["veto"] == "looks personal, may need a reply"
    assert row["veto_kind"] == "attention"
    assert row["held_folder"] == "Filed/Orders"
    # folder stays None so no client can act on a vetoed message by accident
    assert row["folder"] is None


def test_no_rowid_or_mailbox_url_is_ever_sent_to_the_browser():
    """The page addresses messages by opaque id and needs nothing else."""
    payload = repr(proposals_payload(Session([proposal(rowid=900)]), ACCOUNTS))
    assert "900" not in payload
    assert "imap://" not in payload


def test_the_payload_is_json_serialisable():
    json.dumps(proposals_payload(Session([proposal()]), ACCOUNTS))


def test_outcome_reports_what_moved_and_how_to_reverse_it():
    assert outcome_payload(moved=4, failed=1, run_id="20260809-114500") == {
        "moved": 4, "failed": 1, "run_id": "20260809-114500",
    }


def test_candidates_carry_the_method_so_the_page_knows_which_button_to_show():
    rows = candidates_payload([
        option(),
        option(method="http", target="https://other.example/unsub?t=abc",
               sender="news@other.example"),
    ])["candidates"]
    assert rows[0]["method"] == "mailto"
    assert rows[0]["deleted_count"] == 40
    assert rows[1]["method"] == "http"
    assert rows[1]["target"] == "https://other.example/unsub?t=abc"


def test_only_https_counts_as_a_safe_target():
    """parse_list_unsubscribe accepts anything starting with "http", and the
    value is written by the sender. The page puts it in an anchor href, which
    no content policy covers."""
    assert is_safe_target("https://shop.example/unsub?t=1") is True
    assert is_safe_target("http://shop.example/unsub") is False
    assert is_safe_target("httpfoo:whatever") is False
    assert is_safe_target("javascript:alert(1)") is False
    assert is_safe_target("https://") is False
    assert is_safe_target("") is False


def test_an_unsafe_target_is_withheld_rather_than_passed_to_the_browser():
    rows = candidates_payload([
        option(method="http", target="javascript:alert(1)"),
        option(method="http", target="http://shop.example/unsub"),
    ])["candidates"]
    for row in rows:
        assert row["method"] == "blocked"
        assert row["target"] is None


def test_a_mailto_target_is_never_published_as_a_url():
    """The address is the server's business; the page only names the sender."""
    (row,) = candidates_payload([option()])["candidates"]
    assert row["target"] is None


def test_an_applied_message_leaves_the_list():
    """It faded away on Apply and then came back, looking exactly as though
    the move had been undone. It had not: the run holds its proposals for the
    life of the process, and the list was still describing the inbox as it
    was when the run started."""
    session = Session([proposal(rowid=1), proposal(rowid=2)])
    first, _ = list(session.entries)
    session.mark_applied([first])
    rows = proposals_payload(session, ACCOUNTS)["proposals"]
    assert len(rows) == 1
    assert rows[0]["id"] != first
