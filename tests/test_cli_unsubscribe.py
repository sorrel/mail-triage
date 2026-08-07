"""The ``unsubscribe`` command: listing candidates, then sending."""

from __future__ import annotations

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli
from mail_triage.mail_app import FakeMail
from mail_triage.unsubscribe import UnsubscribeOption

from tests.cli_helpers import stub_config


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
    """The first live send reported success and was rejected 18s later."""
    runner, _ = _sending_runner(tmp_path, monkeypatch, [_option()])
    result = runner.invoke(cli, ["unsubscribe"], input="1\ny\n")
    assert "mailer-daemon" in result.output


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
