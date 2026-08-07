"""Gathering one snapshot's worth of everything a triage run needs.

Exercised directly rather than only through the CLI, because the guarantee
this module exists to hold — that every figure comes from one snapshot — is
easier to assert on the returned object than on printed output.
"""

import time

import pytest

from mail_triage.config import Config
from mail_triage.inputs import InputError, gather

from tests.conftest import build_fixture_db

# Inside the one-year window `build_ranking_inputs` counts over, so the
# ranking assertions do not quietly stop exercising anything as time passes.
RECENT = int(time.time()) - 86_400

INBOX = "imap://AAAAAAAA/INBOX"
FILED = "imap://AAAAAAAA/Orders"


def _config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")
    values.update(overrides)
    return Config(**values)


def _db(tmp_path, rows):
    path = tmp_path / "Envelope Index"
    build_fixture_db(path, rows)
    return path


def _row(subject, mailbox_url, sender="a@shop.example", read=1):
    return {
        "sender": sender, "subject": subject, "date_sent": RECENT,
        "mailbox_url": mailbox_url, "read": read,
    }


def test_gather_returns_the_inbox_messages(tmp_path):
    db = _db(tmp_path, [_row("Waiting", INBOX), _row("Already filed", FILED)])
    config = _config(tmp_path)

    inputs = gather(config, list(config.sources), ask=False, db_path=db)

    assert [m.subject for m in inputs.messages] == ["Waiting"]


def test_gather_offers_the_filing_accounts_folders_as_candidates(tmp_path):
    db = _db(tmp_path, [_row("Waiting", INBOX), _row("Filed", FILED)])
    config = _config(tmp_path)

    inputs = gather(config, list(config.sources), ask=False, db_path=db)

    assert "Orders" in inputs.folders


def test_gather_skips_the_sender_scan_when_not_asking(tmp_path):
    """The scan costs a full pass over the message table, so an unattended
    run must not pay for answers nobody is there to give."""
    db = _db(tmp_path, [_row("Waiting", INBOX), _row("Filed", FILED)])
    config = _config(tmp_path)

    inputs = gather(config, list(config.sources), ask=False, db_path=db)

    assert inputs.yearly_counts == {}
    assert inputs.billing_senders == set()


def test_gather_counts_senders_when_asking(tmp_path):
    db = _db(tmp_path, [_row("Waiting", INBOX), _row("Filed", FILED)])
    config = _config(tmp_path)

    inputs = gather(config, list(config.sources), ask=True, db_path=db)

    assert inputs.yearly_counts.get("a@shop.example")


def test_gather_records_each_sources_own_folders(tmp_path):
    """A bin stays in the account it came from, so the pre-flight check needs
    that account's own mailbox list rather than the filing account's."""
    db = _db(tmp_path, [_row("Waiting", INBOX), _row("Filed", FILED)])
    config = _config(tmp_path)

    inputs = gather(config, list(config.sources), ask=False, db_path=db)

    assert "orders" in inputs.source_folders["imap://AAAAAAAA"]


def test_a_missing_inbox_names_the_source_rather_than_failing_obscurely(tmp_path):
    db = _db(tmp_path, [_row("Filed", FILED)])
    config = _config(tmp_path)

    with pytest.raises(InputError) as error:
        gather(config, list(config.sources), ask=False, db_path=db)

    assert "INBOX" in str(error.value)
    assert "mail-triage accounts" in str(error.value)
