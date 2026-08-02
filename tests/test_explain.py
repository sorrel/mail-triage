"""Tests for the ``explain`` CLI command.

``explain`` is read-only: it answers "why does this sender's mail go there?"
without touching a mailbox. The tests build a model with ``save_model`` and
monkeypatch ``load_config``, so no real model, config or mailbox is involved.

The case that matters most is the last group: a hard rule outranks stage A, so
an explanation that reported only the model's opinion would be wrong exactly
when the user is most likely to be asking.
"""

from __future__ import annotations

from click.testing import CliRunner

import mail_triage.cli as cli_module
from mail_triage.cli import cli
from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import TrainedModel, save_model
from mail_triage.rules import Rule, record_rule


def _stub_config(tmp_path):
    return Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")


def _example(sender, folder, weight=1.0):
    return TrainingExample(
        sender=sender, domain=sender.split("@", 1)[1], subject="Subject",
        folder=folder, weight=weight, year=2026,
    )


def _train(tmp_path, examples):
    """Write a model built from ``examples`` where the CLI will look for it."""
    sender_model = SenderModel()
    sender_model.train(examples)
    save_model(
        TrainedModel(sender=sender_model, trained_at=1, example_count=len(examples)),
        _stub_config(tmp_path).model_path,
    )


def _run(tmp_path, monkeypatch, sender):
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    return CliRunner().invoke(cli, ["explain", sender])


def test_explains_a_sender_with_history(tmp_path, monkeypatch):
    _train(tmp_path, [_example("orders@shop.example", "Orders", 3.0)])
    result = _run(tmp_path, monkeypatch, "orders@shop.example")
    assert result.exit_code == 0
    assert "Orders" in result.output


def test_shows_the_weighted_breakdown_across_folders(tmp_path, monkeypatch):
    _train(tmp_path, [
        _example("news@shop.example", "Parent/Keep", 11.0),
        _example("news@shop.example", "Parent/Reading", 9.0),
    ])
    result = _run(tmp_path, monkeypatch, "news@shop.example")
    assert result.exit_code == 0
    # Both destinations and both weights: the point of the command is seeing
    # how close a split call was, not just which side won.
    assert "Parent/Keep" in result.output
    assert "Parent/Reading" in result.output
    assert "11.00" in result.output
    assert "9.00" in result.output


def test_falls_back_to_the_domain_when_the_address_is_unknown(tmp_path, monkeypatch):
    _train(tmp_path, [_example("orders@shop.example", "Orders", 4.0)])
    result = _run(tmp_path, monkeypatch, "returns@shop.example")
    assert result.exit_code == 0
    assert "shop.example" in result.output
    assert "Orders" in result.output


def test_reports_a_sender_with_no_history_at_all(tmp_path, monkeypatch):
    _train(tmp_path, [_example("orders@shop.example", "Orders")])
    result = _run(tmp_path, monkeypatch, "stranger@nowhere.example")
    assert result.exit_code == 0
    assert "No filing history" in result.output


def test_the_sender_is_matched_regardless_of_case(tmp_path, monkeypatch):
    _train(tmp_path, [_example("orders@shop.example", "Orders", 3.0)])
    result = _run(tmp_path, monkeypatch, "Orders@Shop.Example")
    assert result.exit_code == 0
    assert "Orders" in result.output
    assert "No filing history" not in result.output


def test_a_missing_model_asks_you_to_learn_rather_than_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "load_config", lambda: _stub_config(tmp_path))
    result = CliRunner().invoke(cli, ["explain", "orders@shop.example"])
    assert result.exit_code != 0
    assert "learn" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --- Rules outrank the model, and the explanation must say so ----------------
#
# Reporting only stage A's opinion would be actively misleading here: the
# model may favour one folder whilst a rule sends the mail somewhere else
# entirely, and the rule is what actually happens.

def _rule_run(tmp_path, monkeypatch, rule, sender="news@shop.example"):
    _train(tmp_path, [
        _example("news@shop.example", "Parent/Keep", 11.0),
        _example("news@shop.example", "Parent/Reading", 9.0),
    ])
    record_rule(_stub_config(tmp_path).rules_path, rule)
    return _run(tmp_path, monkeypatch, sender)


def test_a_file_rule_is_reported_as_what_decides(tmp_path, monkeypatch):
    result = _rule_run(tmp_path, monkeypatch, Rule(
        sender="news@shop.example", action="file", folder="Parent/Reading",
        answered_at=1, candidates={},
    ))
    assert result.exit_code == 0
    assert "rule" in result.output.casefold()
    assert "Parent/Reading" in result.output


def test_a_bin_rule_is_reported(tmp_path, monkeypatch):
    result = _rule_run(tmp_path, monkeypatch, Rule(
        sender="news@shop.example", action="bin", folder=None,
        answered_at=1, candidates={},
    ))
    assert result.exit_code == 0
    assert "deleted" in result.output.casefold()


def test_a_leave_rule_is_reported(tmp_path, monkeypatch):
    result = _rule_run(tmp_path, monkeypatch, Rule(
        sender="news@shop.example", action="leave", folder=None,
        answered_at=1, candidates={},
    ))
    assert result.exit_code == 0
    assert "left alone" in result.output.casefold()


def test_the_model_breakdown_is_still_shown_alongside_a_rule(tmp_path, monkeypatch):
    """The rule decides, but seeing the history behind it is the whole point
    of asking — not least to judge whether the rule still looks right."""
    result = _rule_run(tmp_path, monkeypatch, Rule(
        sender="news@shop.example", action="file", folder="Parent/Reading",
        answered_at=1, candidates={},
    ))
    assert "Parent/Keep" in result.output
    assert "11.00" in result.output


def test_a_corrupt_rules_file_stops_rather_than_reporting_the_model_alone(tmp_path, monkeypatch):
    """Same reasoning as `triage`: an unreadable rules file must not be
    silently treated as "no rules", or the explanation states the opposite of
    what would happen."""
    _train(tmp_path, [_example("news@shop.example", "Parent/Keep", 11.0)])
    rules_path = _stub_config(tmp_path).rules_path
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text("{ this is not json")
    result = _run(tmp_path, monkeypatch, "news@shop.example")
    assert result.exit_code != 0
    assert "rules" in result.output.casefold()
