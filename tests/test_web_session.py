"""The run's in-memory state, keyed so a page cannot address a rowid."""

from __future__ import annotations

from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.web.session import Session


def proposal(rowid=1, folder="Filed/Orders", sender="shop@shop.example"):
    message = MessageRow(
        rowid=rowid, sender=sender, subject="Order confirmed",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX", read=False,
    )
    return Proposal(message, folder, 0.99, "12 filings", "sender")


def test_every_proposal_gets_an_id_that_is_not_its_rowid():
    """A guessed or stale page must not be able to name a message by rowid."""
    session = Session([proposal(rowid=900)])
    (identifier,) = session.entries
    assert identifier != "900"
    assert session.get(identifier).message.rowid == 900


def test_ids_are_unique_across_proposals():
    session = Session([proposal(rowid=1), proposal(rowid=2), proposal(rowid=3)])
    assert len(set(session.entries)) == 3


def test_an_unknown_id_resolves_to_nothing():
    session = Session([proposal()])
    assert session.get("not-an-id") is None


def test_applying_marks_only_the_named_ids():
    session = Session([proposal(rowid=1), proposal(rowid=2)])
    first, second = list(session.entries)
    session.mark_applied([first])
    assert session.is_applied(first) is True
    assert session.is_applied(second) is False


def test_unapplied_filters_out_what_has_already_moved():
    """The guard against a double-click moving mail twice."""
    session = Session([proposal(rowid=1), proposal(rowid=2)])
    first, second = list(session.entries)
    session.mark_applied([first])
    assert session.unapplied([first, second]) == [second]
