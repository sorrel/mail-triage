from pathlib import Path

import pytest

from mail_triage.config import Config, Source, load_config


def test_load_config_reads_values(tmp_path):
    (tmp_path / "config.toml").write_text(
        """
        account_url_prefix = "imap://ABCDEF01"
        inbox_folder = "INBOX"
        training_exclusions = ["Junk", "Deleted*"]
        confidence_threshold = 0.7
        auto_threshold = 0.9
        half_life_days = 365.0
        correction_weight = 10.0
        """
    )
    config = load_config(tmp_path / "config.toml")
    assert config.account_url_prefix == "imap://ABCDEF01"
    assert config.training_exclusions == ["Junk", "Deleted*"]
    assert config.confidence_threshold == 0.7


def test_local_paths_derive_from_local_dir(tmp_path):
    (tmp_path / "config.toml").write_text('account_url_prefix = "imap://ABCDEF01"\n')
    config = load_config(tmp_path / "config.toml")
    assert config.model_path == config.local_dir / "model.json"
    assert config.corrections_path == config.local_dir / "corrections.jsonl"
    assert config.journal_dir == config.local_dir / "journal"


def test_missing_account_prefix_is_an_error(tmp_path):
    (tmp_path / "config.toml").write_text("inbox_folder = \"INBOX\"\n")
    with pytest.raises(ValueError, match="account_url_prefix"):
        load_config(tmp_path / "config.toml")


def test_training_defaults_to_the_triaged_account_alone(tmp_path):
    (tmp_path / "config.toml").write_text('account_url_prefix = "imap://AAAAAAAA"\n')
    config = load_config(tmp_path / "config.toml")
    assert config.training_prefixes == ["imap://AAAAAAAA"]


def test_training_accounts_can_add_the_archive(tmp_path):
    (tmp_path / "config.toml").write_text(
        'account_url_prefix = "imap://AAAAAAAA"\n'
        'training_accounts = ["imap://AAAAAAAA", "local://BBBBBBBB"]\n'
    )
    config = load_config(tmp_path / "config.toml")
    assert config.training_prefixes == ["imap://AAAAAAAA", "local://BBBBBBBB"]


def test_trash_folder_defaults_to_the_apple_mail_name(tmp_path):
    """A delete is a move to this mailbox, so the name must match the account."""
    (tmp_path / "config.toml").write_text('account_url_prefix = "imap://AAAAAAAA"\n')
    assert load_config(tmp_path / "config.toml").trash_folder == "Deleted Messages"


def test_trash_folder_is_configurable(tmp_path):
    (tmp_path / "config.toml").write_text(
        'account_url_prefix = "imap://AAAAAAAA"\ntrash_folder = "Trash"\n'
    )
    assert load_config(tmp_path / "config.toml").trash_folder == "Trash"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_legacy_config_synthesises_one_source(tmp_path):
    """A config with no [[source]] must behave exactly as it did before."""
    path = _write(tmp_path, """
account_url_prefix = "imap://AAAAAAAA"
inbox_folder = "INBOX"
trash_folder = "Deleted Messages"
""")
    config = load_config(path)
    assert len(config.sources) == 1
    only = config.sources[0]
    assert only.prefix == "imap://AAAAAAAA"
    assert only.inbox == "INBOX"
    assert only.trash == "Deleted Messages"
    assert config.filing_account_prefix == "imap://AAAAAAAA"


def test_legacy_source_name_defaults_and_can_be_set(tmp_path):
    path = _write(tmp_path, 'account_url_prefix = "imap://AAAAAAAA"\n')
    assert load_config(path).sources[0].name == "iCloud"
    path = _write(
        tmp_path, 'account_url_prefix = "imap://AAAAAAAA"\naccount_name = "Elsewhere"\n'
    )
    assert load_config(path).sources[0].name == "Elsewhere"


def test_two_sources_are_loaded_in_order(tmp_path):
    path = _write(tmp_path, """
filing_account = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "iCloud"
prefix = "imap://AAAAAAAA"
inbox = "INBOX"
trash = "Deleted Messages"

[[source]]
name = "Gmail"
prefix = "imap://BBBBBBBB"
inbox = "INBOX"
trash = "[Gmail]/Bin"
ignore = ["[[]Gmail]*"]
""")
    config = load_config(path)
    assert [s.name for s in config.sources] == ["iCloud", "Gmail"]
    assert config.sources[1].trash == "[Gmail]/Bin"
    assert config.sources[1].ignore == ["[[]Gmail]*"]
    assert config.filing_account == "iCloud"


def test_source_for_finds_by_prefix_and_returns_none_otherwise(tmp_path):
    path = _write(tmp_path, """
filing_account = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "Gmail"
prefix = "imap://BBBBBBBB"
""")
    config = load_config(path)
    assert config.source_for("imap://BBBBBBBB").name == "Gmail"
    assert config.source_for("imap://CCCCCCCC") is None


def test_sources_config_requires_a_filing_prefix(tmp_path):
    path = _write(tmp_path, """
[[source]]
name = "Gmail"
prefix = "imap://BBBBBBBB"
""")
    with pytest.raises(ValueError, match="filing_account_prefix"):
        load_config(path)


def test_duplicate_source_prefixes_are_rejected(tmp_path):
    """Two sources with one prefix would double-triage the same inbox."""
    path = _write(tmp_path, """
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "One"
prefix = "imap://AAAAAAAA"

[[source]]
name = "Two"
prefix = "imap://AAAAAAAA"
""")
    with pytest.raises(ValueError, match="more than once"):
        load_config(path)


def test_the_example_config_actually_loads():
    """The documented shape must be a working one, ordering trap included."""
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert [s.name for s in config.sources] == ["iCloud", "Gmail"]
    assert config.sources[1].ignore == ["[[]Gmail]*"]
    assert config.deletion_window_days == 75


def test_a_setting_misplaced_after_a_source_table_explains_itself(tmp_path):
    """The TOML trap that broke the first draft of config.example.toml."""
    path = _write(tmp_path, """
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "iCloud"
prefix = "imap://AAAAAAAA"

deletion_window_days = 75
""")
    with pytest.raises(ValueError, match="deletion_window_days"):
        load_config(path)


def test_account_url_prefix_defaults_to_the_filing_account(tmp_path):
    """Training follows the filing account unless told otherwise.

    The real config relies on this: it names filing_account_prefix and no
    account_url_prefix, so that the two cannot drift apart.
    """
    path = _write(tmp_path, """
filing_account = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "iCloud"
prefix = "imap://AAAAAAAA"

[[source]]
name = "Gmail"
prefix = "imap://BBBBBBBB"
""")
    config = load_config(path)
    assert config.account_url_prefix == "imap://AAAAAAAA"
    assert config.training_prefixes == ["imap://AAAAAAAA"]
