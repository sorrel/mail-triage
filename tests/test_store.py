"""Tests for model persistence: ``TrainedModel``, ``save_model``, ``load_model``,
and ``train_from_history``.
"""

from __future__ import annotations

import json

import pytest

import mail_triage.model.store as store_module
from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import (
    MODEL_VERSION, TrainedModel, load_model, save_model, train_from_history,
)


def test_model_round_trips_to_disk(tmp_path):
    sender_model = SenderModel()
    sender_model.train([
        TrainingExample(sender="orders@shop.example", domain="shop.example",
                        subject="Order", folder="orders", weight=1.0, year=2026)
    ])
    original = TrainedModel(sender=sender_model, trained_at=1_700_000_000, example_count=1)
    path = tmp_path / "model.json"
    save_model(original, path)
    restored = load_model(path)
    assert restored.example_count == 1
    assert restored.trained_at == 1_700_000_000
    assert restored.sender.predict("orders@shop.example", "shop.example").folder == "orders"


def test_loading_a_missing_model_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="mail-triage learn"):
        load_model(tmp_path / "absent.json")


def test_loading_a_wrong_version_model_is_a_clear_error(tmp_path):
    path = tmp_path / "model.json"
    path.write_text('{"version": 999, "trained_at": 1, "example_count": 0, "sender": {}}')
    with pytest.raises(ValueError, match="mail-triage learn") as excinfo:
        load_model(path)
    # Confirm the version-mismatch branch, not merely that some ValueError fired.
    # Read against MODEL_VERSION rather than a hard-coded number: this line
    # said `"1" in ...` and went on passing after MODEL_VERSION became 2,
    # because the message embeds tmp_path and a pytest temp directory almost
    # always contains a "1" somewhere. It only failed once CI produced a path
    # that did not. Match the phrase, not a digit loose in the string.
    assert "version 999" in str(excinfo.value)
    assert f"expected {MODEL_VERSION}" in str(excinfo.value)


def test_loading_a_truncated_json_model_is_a_clear_error(tmp_path):
    path = tmp_path / "model.json"
    path.write_text('{"version": 1, "trained_at": 1, "example')  # cut off mid-write
    with pytest.raises(ValueError, match="mail-triage learn") as excinfo:
        load_model(path)
    assert "corrupt" in str(excinfo.value).lower()
    assert str(path) in str(excinfo.value)


def test_loading_a_model_missing_a_field_is_a_clear_error(tmp_path):
    path = tmp_path / "model.json"
    # Valid JSON, correct version, but no "example_count" key.
    from mail_triage.model.store import MODEL_VERSION

    path.write_text(json.dumps({"version": MODEL_VERSION, "trained_at": 1, "sender": {}}))
    with pytest.raises(ValueError, match="mail-triage learn") as excinfo:
        load_model(path)
    assert "example_count" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_save_model_is_atomic_no_partial_file_left_on_failure(tmp_path, monkeypatch):
    def raise_error(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_module.json, "dump", raise_error)
    model = TrainedModel(sender=SenderModel(), trained_at=1, example_count=0)
    path = tmp_path / "model.json"

    with pytest.raises(RuntimeError):
        save_model(model, path)

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_save_model_failure_does_not_corrupt_an_existing_good_model(tmp_path, monkeypatch):
    sender_model = SenderModel()
    sender_model.train([
        TrainingExample(sender="a@shop.example", domain="shop.example", subject="x",
                        folder="orders", weight=1.0, year=2026)
    ])
    good = TrainedModel(sender=sender_model, trained_at=1, example_count=1)
    path = tmp_path / "model.json"
    save_model(good, path)
    original_bytes = path.read_bytes()

    def raise_error(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_module.json, "dump", raise_error)
    broken = TrainedModel(sender=SenderModel(), trained_at=2, example_count=999)

    with pytest.raises(RuntimeError):
        save_model(broken, path)

    assert path.read_bytes() == original_bytes
    restored = load_model(path)
    assert restored.example_count == 1
    assert restored.sender.predict("a@shop.example", "shop.example").folder == "orders"


def test_drift_survives_a_save_and_load_cycle_to_disk(tmp_path):
    # Same sender, dominant folder changes between 2020 and 2026: drift.
    examples = [
        TrainingExample(sender="a@shop.example", domain="shop.example", subject="x",
                        folder="old-folder", weight=1.0, year=2020),
        TrainingExample(sender="a@shop.example", domain="shop.example", subject="x",
                        folder="new-folder", weight=1.0, year=2026),
    ]
    sender_model = SenderModel()
    sender_model.train(examples)
    sender_model.train_drift(examples)
    assert sender_model.drift_report(), "sanity check: drift exists before save"

    original = TrainedModel(sender=sender_model, trained_at=1_700_000_000, example_count=2)
    path = tmp_path / "model.json"
    save_model(original, path)
    restored = load_model(path)

    entries = restored.sender.drift_report()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "a@shop.example"
    assert entry.old_folder == "old-folder"
    assert entry.new_folder == "new-folder"
    assert entry.switch_year == 2026


def test_train_from_history_builds_a_trained_model(fixture_db):
    config = Config(account_url_prefix="imap://AAAAAAAA", local_dir=fixture_db.parent)
    model = train_from_history(config, fixture_db)
    # Two of the four fixture rows are trained on: the Orders account rows
    # match account_url_prefix and aren't excluded. The INBOX row is excluded
    # by default, and the local:// row belongs to a different account.
    assert model.example_count == 2
    assert model.trained_at > 0
    assert model.sender.predict("orders@shop.example", "shop.example").folder == "orders"


# --- Stage B lives in the model file too --------------------------------------

def test_the_token_model_round_trips_to_disk(tmp_path):
    from mail_triage.model.store import MODEL_VERSION
    from mail_triage.model.tokens import TokenModel

    sender_model = SenderModel()
    sender_model.train([
        TrainingExample(sender="orders@shop.example", domain="shop.example",
                        subject="Order", folder="orders", weight=1.0, year=2026)
    ])
    token_model = TokenModel()
    token_model.train([
        TrainingExample(sender="a@b.example", domain="b.example",
                        subject="Your invoice is ready", folder="finance",
                        weight=1.0, year=2026)
    ])
    original = TrainedModel(sender=sender_model, trained_at=1, example_count=1,
                            tokens=token_model)
    path = tmp_path / "model.json"
    save_model(original, path)
    restored = load_model(path)
    assert restored.tokens.predict("invoice", "x@y.example").folder == "finance"
    assert MODEL_VERSION >= 2


def test_a_model_from_before_stage_b_is_refused_with_advice(tmp_path):
    """Version 1 files have no token model; silently loading one would mean
    stage B never fires and nobody would know why."""
    import json

    path = tmp_path / "model.json"
    path.write_text(json.dumps({
        "version": 1, "trained_at": 1, "example_count": 1,
        "sender": {"by_sender": {}, "by_domain": {}},
    }))
    with pytest.raises(ValueError, match="mail-triage learn"):
        load_model(path)
