"""Finding bounces, and tying them to the request that caused them.

Every rule here is an exact match against something the tool generated
itself. The alternative — a fuzzy match on subject or sender — is what the
plan for this task warned against by name: a rule that quietly matches the
wrong message reports a bounce for a request that was fine, which is worse
than reporting nothing at all.
"""

from __future__ import annotations

from mail_triage.bounces import (
    Bounce,
    attribute,
    candidate_rows,
    failed_recipients,
    is_bounce_sender,
    is_delivery_report,
    render_report,
)
from mail_triage.envelope import MessageRow
from mail_triage.sends import SentRequest

SENT_AT = 1_700_000_000
DSN_HEADERS = {"Content-Type": 'multipart/report; report-type=delivery-status; boundary="x"'}


def _request(**overrides) -> SentRequest:
    values = dict(
        sender="news@list.example",
        to_address="leave@list.example",
        subject="token-abc12345",
        sent_at=SENT_AT,
        from_account="iCloud",
    )
    values.update(overrides)
    return SentRequest(**values)


def _row(
    rowid=1,
    sender="MAILER-DAEMON@relay.example",
    subject="Delivery Status Notification (Failure)",
    received_at=SENT_AT + 20,
) -> MessageRow:
    return MessageRow(
        rowid=rowid,
        sender=sender,
        subject=subject,
        date_sent=received_at,
        mailbox_url="imap://AAAAAAAA/INBOX",
        read=False,
        date_received=received_at,
    )


# --- phase 1: narrowing the inbox without any round trips ---------------------

def test_daemon_local_parts_are_recognised_whatever_the_domain():
    assert is_bounce_sender("MAILER-DAEMON@relay.example")
    assert is_bounce_sender("postmaster@some.other.example")
    assert is_bounce_sender("Mailer-Daemon@x.example")


def test_an_ordinary_sender_is_not_a_bounce():
    assert not is_bounce_sender("news@list.example")
    assert not is_bounce_sender("mailer-daemon-news@list.example")


def test_only_messages_inside_the_window_are_candidates():
    batch = [_request()]
    too_early = _row(rowid=1, received_at=SENT_AT - 600)
    just_early_enough = _row(rowid=2, received_at=SENT_AT - 60)
    prompt = _row(rowid=3, received_at=SENT_AT + 20)
    too_late = _row(rowid=4, received_at=SENT_AT + 90_000)
    rows = candidate_rows([too_early, just_early_enough, prompt, too_late], batch)
    assert [row.rowid for row in rows] == [2, 3]


def test_an_ordinary_message_in_the_window_is_not_a_candidate():
    batch = [_request()]
    rows = candidate_rows([_row(sender="news@list.example")], batch)
    assert rows == []


def test_no_batch_means_no_candidates():
    assert candidate_rows([_row()], []) == []


# --- phase 2: confirming it really is a delivery report -----------------------

def test_a_multipart_report_is_a_delivery_report():
    assert is_delivery_report(DSN_HEADERS)


def test_an_auto_replied_message_is_a_delivery_report():
    assert is_delivery_report({"Auto-Submitted": "auto-replied"})


def test_a_human_called_postmaster_is_not_a_delivery_report():
    assert not is_delivery_report({"Content-Type": "text/plain; charset=utf-8"})


def test_delivery_report_detection_ignores_header_case():
    assert is_delivery_report({"content-type": "multipart/report; report-type=delivery-status"})


# --- attribution --------------------------------------------------------------

def test_an_exact_failed_recipient_attributes_to_its_request():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    [bounce] = attribute([(_row(), headers)], [request])
    assert bounce.request == request
    assert bounce.failed_recipient == "leave@list.example"


def test_failed_recipient_matching_ignores_case_and_spacing():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "other@x.example, LEAVE@List.Example "}
    [bounce] = attribute([(_row(), headers)], [request])
    assert bounce.request == request


def test_a_distinctive_subject_token_attributes_when_no_header_carries_it():
    request = _request()
    row = _row(subject="Undeliverable: token-abc12345")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request == request


def test_the_word_unsubscribe_never_attributes_by_subject():
    """The dangerous case: it is the default subject and appears everywhere."""
    request = _request(subject="unsubscribe")
    row = _row(subject="Re: unsubscribe from our newsletter")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request is None


def test_a_short_token_never_attributes_by_subject():
    request = _request(subject="stop")
    row = _row(subject="Undeliverable: please stop the presses")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request is None


def test_a_bounce_for_an_address_nobody_wrote_to_is_unattributed():
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "someone@else.example"}
    [bounce] = attribute([(_row(), headers)], [_request()])
    assert bounce.request is None
    assert bounce.failed_recipient == "someone@else.example"


def test_one_bounce_never_claims_two_requests():
    """Two lists can share an unsubscribe address."""
    first = _request(sender="a@list.example", sent_at=SENT_AT)
    second = _request(sender="b@list.example", sent_at=SENT_AT + 1)
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    bounces = attribute([(_row(rowid=1), headers), (_row(rowid=2), headers)], [first, second])
    assert [bounce.request for bounce in bounces] == [first, second]


def test_a_second_bounce_with_no_request_left_is_unattributed():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    bounces = attribute([(_row(rowid=1), headers), (_row(rowid=2), headers)], [request])
    assert bounces[0].request == request
    assert bounces[1].request is None


def test_a_message_that_is_not_a_delivery_report_is_dropped_entirely():
    headers = {"Content-Type": "text/plain", "X-Failed-Recipients": "leave@list.example"}
    assert attribute([(_row(), headers)], [_request()]) == []


def test_failed_recipients_handles_an_absent_header():
    assert failed_recipients({}) == []


# --- the report ---------------------------------------------------------------

def test_a_bounced_request_is_named_with_its_recipient():
    request = _request()
    bounce = Bounce(rowid=1, subject="Delivery Status Notification (Failure)",
                    received_at=SENT_AT + 20, failed_recipient="leave@list.example",
                    request=request)
    report = render_report([request], [bounce], "2026-08-07T10-00-00")
    assert "news@list.example" in report
    assert "leave@list.example" in report
    assert "bounced" in report


def test_a_request_with_no_bounce_is_never_called_delivered():
    """A discarded request is indistinguishable from an accepted one."""
    report = render_report([_request()], [], "2026-08-07T10-00-00")
    assert "no bounce seen" in report
    assert "delivered" not in report.casefold()
    assert "confirmed" not in report.casefold()


def test_an_unattributed_bounce_is_reported_separately():
    bounce = Bounce(rowid=9, subject="Undelivered Mail Returned to Sender",
                    received_at=SENT_AT + 30)
    report = render_report([_request()], [bounce], "2026-08-07T10-00-00")
    assert "Undelivered Mail Returned to Sender" in report
    assert "unattributed" in report.casefold()


def test_the_report_says_it_cannot_give_a_reason():
    request = _request()
    bounce = Bounce(rowid=1, subject="Failure", received_at=SENT_AT + 20,
                    failed_recipient="leave@list.example", request=request)
    report = render_report([request], [bounce], "2026-08-07T10-00-00")
    assert "does not read" in report


def test_the_batch_and_account_are_named():
    report = render_report([_request()], [], "2026-08-07T10-00-00")
    assert "2026-08-07T10-00-00" in report
    assert "iCloud" in report
