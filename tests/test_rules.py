"""Rules are the user speaking directly, so losing or ignoring one is a bug.

These tests pin the two properties the spec calls safety-critical: an answer
is on disk the moment it is given, and a rules file that cannot be parsed is
an error rather than a silent fallback to no rules at all.
"""

from pathlib import Path

import pytest

from mail_triage.rules import Rule, RulesError, forget_rule, load_rules, record_rule


def file_rule(sender="news@shop.example", folder="Parent/Keep"):
    return Rule(
        sender=sender,
        action="file",
        folder=folder,
        answered_at=1_785_000_000,
        candidates={"Parent/Keep": 12.0, "Parent/Reading": 8.0},
    )


def test_loading_a_missing_rules_file_yields_no_rules(tmp_path):
    assert load_rules(tmp_path / "rules.json") == {}


def test_a_recorded_rule_round_trips(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, file_rule())
    loaded = load_rules(path)
    assert loaded["news@shop.example"].folder == "Parent/Keep"
    assert loaded["news@shop.example"].action == "file"
    assert loaded["news@shop.example"].candidates == {"Parent/Keep": 12.0, "Parent/Reading": 8.0}


def test_each_answer_is_on_disk_before_the_next_is_asked(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, file_rule(sender="first@shop.example"))
    assert set(load_rules(path)) == {"first@shop.example"}
    record_rule(path, file_rule(sender="second@shop.example"))
    assert set(load_rules(path)) == {"first@shop.example", "second@shop.example"}


def test_a_later_answer_replaces_an_earlier_one_for_the_same_sender(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, file_rule(folder="Parent/Keep"))
    record_rule(path, file_rule(folder="Parent/Reading"))
    rules = load_rules(path)
    assert len(rules) == 1
    assert rules["news@shop.example"].folder == "Parent/Reading"


def test_leave_alone_is_a_recorded_answer_with_no_folder(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(
        path,
        Rule(sender="mixed@shop.example", action="leave", folder=None,
             answered_at=1_785_000_000, candidates={}),
    )
    rule = load_rules(path)["mixed@shop.example"]
    assert rule.action == "leave"
    assert rule.folder is None


def test_a_corrupt_rules_file_is_an_error_naming_the_file(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{\n  "news@shop.example": {"action": "file",\n}\n')
    with pytest.raises(RulesError) as caught:
        load_rules(path)
    assert str(path) in str(caught.value)


def test_a_corrupt_rules_file_reports_the_failing_line(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{\n  "news@shop.example": {"action": "file",\n}\n')
    with pytest.raises(RulesError, match="line 2"):
        load_rules(path)


def test_a_file_rule_without_a_folder_is_rejected_on_load(tmp_path):
    """Hand-editable means hand-breakable; a rule pointing nowhere must not load."""
    path = tmp_path / "rules.json"
    path.write_text('{"news@shop.example": {"action": "file", "answered_at": 1}}')
    with pytest.raises(RulesError, match="folder"):
        load_rules(path)


def test_an_unknown_action_is_rejected_on_load(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"news@shop.example": {"action": "incinerate", "answered_at": 1}}')
    with pytest.raises(RulesError, match="incinerate"):
        load_rules(path)


def test_bin_is_now_an_accepted_action(tmp_path):
    """Unblocked 27 July 2026, once invoice detection existed to make it safe."""
    path = tmp_path / "rules.json"
    path.write_text('{"news@shop.example": {"action": "bin", "answered_at": 1}}')
    assert load_rules(path)["news@shop.example"].action == "bin"


def test_a_bin_rule_needs_no_folder(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, Rule(sender="news@shop.example", action="bin", folder=None,
                           answered_at=1, candidates={}))
    assert load_rules(path)["news@shop.example"].folder is None


def test_forgetting_a_rule_removes_it(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, file_rule(sender="first@shop.example"))
    record_rule(path, file_rule(sender="second@shop.example"))
    assert forget_rule(path, "first@shop.example") is True
    assert set(load_rules(path)) == {"second@shop.example"}


def test_forgetting_an_unknown_sender_reports_that_it_did_nothing(tmp_path):
    path = tmp_path / "rules.json"
    record_rule(path, file_rule())
    assert forget_rule(path, "stranger@shop.example") is False
    assert set(load_rules(path)) == {"news@shop.example"}


def test_sender_matching_is_case_insensitive(tmp_path):
    """Addresses arrive from Mail in whatever case the sender chose."""
    path = tmp_path / "rules.json"
    record_rule(path, file_rule(sender="News@Shop.Example"))
    assert "news@shop.example" in load_rules(path)


def test_rules_path_lives_in_the_local_directory(tmp_path):
    from mail_triage.config import Config

    config = Config(account_url_prefix="imap://AAAAAAAA", local_dir=Path(tmp_path))
    assert config.rules_path == config.local_dir / "rules.json"
