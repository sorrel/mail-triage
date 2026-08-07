"""The ``unsubscribe`` command: listing candidates, then sending."""

from __future__ import annotations

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli
from mail_triage.mail_app import FakeMail
from mail_triage.sends import SentRequest, list_batches, load_batch, record_send
from mail_triage.unsubscribe import UnsubscribeOption

from tests.cli_helpers import stub_config
from tests.conftest import build_fixture_db


def _stub_candidates(monkeypatch, options):
    monkeypatch.setattr(
        cli_module, "find_candidates", lambda reader, config, mail, limit: list(options)
    )


def _sending_runner(tmp_path, monkeypatch, options):

    mail = FakeMail(inbox=[], mailboxes=[])
    monkeypatch.setattr(cli_module, "load_config", lambda: stub_config(tmp_path))
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    monkeypatch.setattr(cli_module, "snapshot_database", lambda source, work: source)
    monkeypatch.setattr(cli_module, "EnvelopeReader", lambda path: _NullReader())
    _stub_candidates(monkeypatch, options)
    return CliRunner(), mail


class _NullReader:
    """An account with an inbox and nothing in it.

    The inbox has to exist, because the send path now takes one free look
    for bounces afterwards and reports a missing inbox rather than passing
    over it in silence.
    """

    def mailbox_urls(self):
        return ["imap://AAAAAAAA/INBOX"]

    def inbox_messages(self, url):
        return []

    def close(self):
        pass


def _option(sender="news@x.example", **overrides):

    domain = sender.split("@", 1)[1]
    values = dict(
        sender=sender,
        domain=domain,
        method="mailto",
        target=f"leave@{domain}",
        message_count=4,
        unread_count=4,
        deleted_count=22,
        account="iCloud",
        subject="unsubscribe",
        body="unsubscribe",
    )
    values.update(overrides)
    return UnsubscribeOption(**values)


def test_unsubscribe_lists_every_candidate_before_asking(tmp_path, monkeypatch):
    options = [_option("a@x.example"), _option("b@y.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="\n")
    assert result.exit_code == 0
    assert "a@x.example" in result.output
    assert "b@y.example" in result.output
    assert mail.sent == []


def test_unsubscribe_dry_run_sends_nothing(tmp_path, monkeypatch):
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe", "--dry-run"])
    assert result.exit_code == 0
    assert mail.sent == []
    assert "22 binned" in result.output
    assert "Nothing sent (--dry-run)." in result.output


def test_unsubscribe_selecting_nothing_sends_nothing(tmp_path, monkeypatch):
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe"], input="\n")
    assert result.exit_code == 0
    assert mail.sent == []
    assert "Nothing selected" in result.output


def test_unsubscribe_sends_the_one_you_picked(tmp_path, monkeypatch):
    options = [_option("a@x.example"), _option("b@y.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="2\ny\n")
    assert result.exit_code == 0
    assert mail.sent == [("leave@y.example", "unsubscribe")]


def test_unsubscribe_sends_several_at_once(tmp_path, monkeypatch):
    """Deciding about a list is one job, not seven."""
    options = [_option("a@x.example"), _option("b@y.example"), _option("c@z.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1,3\ny\n")
    assert result.exit_code == 0
    assert mail.sent == [
        ("leave@x.example", "unsubscribe"),
        ("leave@z.example", "unsubscribe"),
    ]


def test_unsubscribe_confirms_the_selection_before_sending(tmp_path, monkeypatch):
    """The number chooses; the confirmation is the second gate, not the first."""
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe"], input="1\nn\n")
    assert result.exit_code == 0
    assert mail.sent == []
    assert "Nothing sent." in result.output


def test_unsubscribe_sends_the_token_from_the_header(tmp_path, monkeypatch):
    """The subject is the subscriber token; the wrong one bounces."""
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option(subject="tok/en-9")])
    result = runner.invoke(cli, ["unsubscribe"], input="1\ny\n")
    assert result.exit_code == 0
    assert mail.sent == [("leave@x.example", "tok/en-9")]


def test_unsubscribe_refuses_a_picked_http_sender(tmp_path, monkeypatch):
    """Picking it misreads the list; sending the rest anyway would hide that."""
    options = [_option("a@x.example", method="http", target="https://x.example/u"),
               _option("b@y.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1,2\ny\n")
    assert result.exit_code != 0
    assert mail.sent == []
    assert "HTTP-only" in result.output


def test_unsubscribe_rejects_a_number_off_the_end(tmp_path, monkeypatch):
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe"], input="7\n")
    assert result.exit_code != 0
    assert mail.sent == []
    assert "no 7 in the list" in result.output


def test_unsubscribe_warns_that_a_send_may_still_bounce(tmp_path, monkeypatch):
    """The first live send reported success and was rejected 18s later.

    The run looks once for a bounce and finds none this quickly, so what it
    must not do is leave the user thinking the job is finished.
    """
    runner, _ = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe"], input="1\ny\n")
    assert "No bounces yet" in result.output
    assert "unsubscribe --check" in result.output


def test_unsubscribe_sender_filter_narrows_the_list(tmp_path, monkeypatch):
    options = [_option("a@x.example"), _option("b@y.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe", "--sender", "b@y"], input="1\ny\n")
    assert result.exit_code == 0
    assert mail.sent == [("leave@y.example", "unsubscribe")]
    assert "a@x.example" not in result.output


def test_unsubscribe_sender_filter_matching_nothing_is_an_error(tmp_path, monkeypatch):
    """Silence would look identical to 'nothing to unsubscribe from'."""
    runner, mail = _sending_runner(tmp_path, monkeypatch, [_option("a@x.example")])
    result = runner.invoke(cli, ["unsubscribe", "--sender", "nobody"], input="")
    assert result.exit_code != 0
    assert mail.sent == []
    assert "No candidate sender contains" in result.output


# --- Task 20: a reported send is not a completed unsubscribe -------------------

def test_a_successful_send_is_recorded(tmp_path, monkeypatch):
    options = [_option("a@x.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1\ny\n")
    assert result.exit_code == 0
    config = stub_config(tmp_path)
    [batch_id] = list_batches(config)
    [record] = load_batch(config, batch_id)
    assert record.sender == "a@x.example"
    assert record.to_address == "leave@x.example"
    assert record.from_account == "iCloud"


def test_a_refused_send_records_nothing(tmp_path, monkeypatch):
    """Nothing went out, so nothing may appear in the log."""
    options = [_option("a@x.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1\nn\n")
    assert result.exit_code == 0
    assert list_batches(stub_config(tmp_path)) == []


def test_check_with_no_batches_says_so(tmp_path, monkeypatch):
    runner, mail = _sending_runner(tmp_path, monkeypatch, [])
    result = runner.invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "No unsubscribe requests recorded yet" in result.output


def test_check_refuses_when_the_sending_account_is_not_configured(tmp_path, monkeypatch):
    """Searching the configured inboxes instead would report a clean run
    from the wrong mailbox."""
    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", SentRequest(
        sender="a@x.example", to_address="leave@x.example", subject="token-abc12345",
        sent_at=1_700_000_000, from_account="Some Other Account",
    ))
    runner, mail = _sending_runner(tmp_path, monkeypatch, [])
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    result = runner.invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "Some Other Account" in result.output
    assert "not a configured source" in result.output


def test_check_reports_a_bounce_it_can_attribute(tmp_path, monkeypatch):
    sent_at = 1_700_000_000
    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", SentRequest(
        sender="news@list.example", to_address="leave@list.example",
        subject="token-abc12345", sent_at=sent_at, from_account="iCloud",
    ))
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(db_path, [
        {"rowid": 77, "sender": "MAILER-DAEMON@relay.example",
         "subject": "Delivery Status Notification (Failure)",
         "date_sent": sent_at + 20, "date_received": sent_at + 20,
         "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
    ])

    mail = FakeMail(
        inbox=[77], mailboxes=["INBOX"],
        headers={77: {
            "Content-Type": "multipart/report; report-type=delivery-status",
            "X-Failed-Recipients": "leave@list.example",
        }},
    )
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    monkeypatch.setattr(cli_module, "snapshot_database", lambda source, work: db_path)

    result = CliRunner().invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "bounced" in result.output
    assert "news@list.example" in result.output
