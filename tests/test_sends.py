"""The record of what was actually sent."""

from __future__ import annotations

import warnings

from mail_triage.sends import (
    FailedRequest,
    SentRequest,
    list_batches,
    load_batch,
    new_batch_id,
    load_failures,
    record_failure,
    record_send,
    sends_dir,
)

from tests.cli_helpers import stub_config


def _request(**overrides) -> SentRequest:
    values = dict(
        sender="news@list.example",
        to_address="leave@list.example",
        subject="token-abc12345",
        sent_at=1_700_000_000,
        from_account="iCloud",
    )
    values.update(overrides)
    return SentRequest(**values)


def test_a_recorded_send_comes_back_unchanged(tmp_path):
    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", _request())
    assert load_batch(config, "2026-08-07T10-00-00") == [_request()]


def test_sends_are_appended_in_order(tmp_path):
    config = stub_config(tmp_path)
    first = _request(sender="a@x.example")
    second = _request(sender="b@y.example")
    record_send(config, "batch", first)
    record_send(config, "batch", second)
    assert load_batch(config, "batch") == [first, second]


def test_batches_list_newest_first(tmp_path):
    config = stub_config(tmp_path)
    record_send(config, "2026-08-05T09-00-00", _request())
    record_send(config, "2026-08-07T10-00-00", _request())
    record_send(config, "2026-08-06T11-00-00", _request())
    assert list_batches(config) == [
        "2026-08-07T10-00-00",
        "2026-08-06T11-00-00",
        "2026-08-05T09-00-00",
    ]


def test_no_batches_is_an_empty_list_not_an_error(tmp_path):
    assert list_batches(stub_config(tmp_path)) == []


def test_a_corrupt_line_is_skipped_and_the_rest_survive(tmp_path):
    """A killed process can leave one truncated line. It must not cost the batch."""
    config = stub_config(tmp_path)
    record_send(config, "batch", _request(sender="a@x.example"))
    path = sends_dir(config) / "batch.jsonl"
    with path.open("a") as handle:
        handle.write('{"sender": "b@y.example", "to_addr\n')
    record_send(config, "batch", _request(sender="c@z.example"))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = load_batch(config, "batch")
    assert [request.sender for request in loaded] == ["a@x.example", "c@z.example"]
    assert len(caught) == 1


def test_batch_ids_sort_lexicographically_by_time():
    assert new_batch_id() > "2020-01-01T00-00-00"


def test_loading_a_batch_that_does_not_exist_is_empty(tmp_path):
    assert load_batch(stub_config(tmp_path), "nope") == []


# --- and what did not go out ----------------------------------------------
#
# A failed send used to leave nothing anywhere. On 9 August 2026 an
# unsubscribe was composed from the wrong account, Mail refused to send it,
# and the only surviving trace was three drafts the user happened to notice.
# There was no log to review.


def test_a_failure_is_recorded_where_it_can_be_reviewed(tmp_path):
    config = stub_config(tmp_path)
    record_failure(
        config,
        "batch",
        FailedRequest(
            sender="news@list.example",
            to_address="leave@list.example",
            subject="token-abc",
            attempted_at=1_700_000_000,
            from_account="iCloud",
            reason="Mail got an error: the account has no server to send from",
        ),
    )
    failures = load_failures(config, "batch")
    assert [failure.sender for failure in failures] == ["news@list.example"]
    assert "no server" in failures[0].reason


def test_a_failure_is_not_a_send_and_the_bounce_check_never_sees_it(tmp_path):
    """The reason sends are recorded only after they succeed: a record of a
    request that never went out would be looked up, found un-bounced, and
    reported as fine — the exact false clean bill of health the send log
    exists to abolish. So failures live apart from it entirely."""
    config = stub_config(tmp_path)
    record_send(config, "batch", _request(sender="went@x.example"))
    record_failure(
        config,
        "batch",
        FailedRequest(
            sender="never@y.example",
            to_address="leave@y.example",
            subject="s",
            attempted_at=1_700_000_000,
            from_account="",
            reason="refused",
        ),
    )
    assert [request.sender for request in load_batch(config, "batch")] == ["went@x.example"]
    # And a failure file is not mistaken for a batch of its own.
    assert list_batches(config) == ["batch"]


def test_no_failures_is_an_empty_list_not_an_error(tmp_path):
    assert load_failures(stub_config(tmp_path), "nope") == []
