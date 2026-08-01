"""Measurement of what the local mail store occupies.

Every fixture here is a fabricated directory tree. No test reads the real
mail store, and none of these folder names is a real one.
"""

from pathlib import Path

from mail_triage.sizes import (
    account_disk_usage,
    build_account_usage,
    folder_path_from_mbox,
    maildata_usage,
)


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_folder_path_from_mbox_strips_the_suffix_at_every_level():
    assert folder_path_from_mbox(Path("Parent.mbox/Child.mbox")) == "Parent/Child"
    assert folder_path_from_mbox(Path("Parent.mbox")) == "Parent"


def test_own_bytes_exclude_nested_mailboxes(tmp_path):
    """A parent must not absorb its children, or the roll-up double-counts."""
    write(tmp_path / "Parent.mbox" / "UUID" / "Data" / "1.emlx", 4096)
    write(tmp_path / "Parent.mbox" / "Child.mbox" / "UUID" / "2.emlx", 4096)
    usage = account_disk_usage(tmp_path)
    assert usage.by_folder["Parent"] >= 4096
    assert usage.by_folder["Parent/Child"] >= 4096


def test_files_outside_any_mailbox_are_counted_as_loose(tmp_path):
    write(tmp_path / "AccountInfo.plist", 100)
    usage = account_disk_usage(tmp_path)
    assert usage.loose_bytes > 0
    assert usage.by_folder == {}


def test_a_missing_account_directory_yields_nothing(tmp_path):
    usage = account_disk_usage(tmp_path / "absent")
    assert usage.by_folder == {}
    assert usage.loose_bytes == 0


def test_an_unreadable_directory_is_recorded_not_raised(tmp_path):
    """A permissions gap costs one folder, never the whole report."""
    locked = tmp_path / "Locked.mbox"
    write(locked / "UUID" / "1.emlx", 100)
    locked.chmod(0o000)
    try:
        usage = account_disk_usage(tmp_path)
    finally:
        locked.chmod(0o700)
    assert usage.unreadable


# --- Joining the disk walk to the database ------------------------------------


def test_totals_roll_up_through_the_tree(tmp_path):
    (tmp_path / "AAAAAAAA").mkdir()
    accounts = build_account_usage(
        [("imap://AAAAAAAA/Parent", 2, 350), ("imap://AAAAAAAA/Parent/Child", 1, 40)],
        tmp_path,
        {},
    )
    root = accounts[0].root
    parent = root.children[0]
    assert parent.name == "Parent"
    assert parent.envelope_bytes == 350
    assert parent.total_envelope_bytes == 390
    assert parent.total_messages == 3
    assert root.total_envelope_bytes == 390


def test_an_account_with_no_directory_reports_blank_disk(tmp_path):
    """Blank, never zero: nought would read as 'empty' rather than 'not here'."""
    accounts = build_account_usage([("imap://BBBBBBBB/Parent", 1, 10)], tmp_path, {})
    assert accounts[0].on_disk is False
    assert accounts[0].root.total_disk_bytes is None


def test_disk_and_envelope_are_joined_on_the_folder_path(tmp_path):
    data = tmp_path / "AAAAAAAA" / "Parent.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    accounts = build_account_usage([("imap://AAAAAAAA/Parent", 1, 350)], tmp_path, {})
    parent = accounts[0].root.children[0]
    assert parent.envelope_bytes == 350
    assert parent.own_disk_bytes >= 4096


def test_a_folder_on_disk_but_not_in_the_database_still_appears(tmp_path):
    data = tmp_path / "AAAAAAAA" / "Ghost.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    accounts = build_account_usage([("imap://AAAAAAAA/Placeholder", 0, 0)], tmp_path, {})
    names = [child.name for child in accounts[0].root.children]
    assert "Ghost" in names
    ghost = next(c for c in accounts[0].root.children if c.name == "Ghost")
    assert ghost.message_count == 0


def test_url_encoded_folder_names_are_decoded(tmp_path):
    (tmp_path / "AAAAAAAA").mkdir()
    accounts = build_account_usage([("imap://AAAAAAAA/Two%20Words", 1, 10)], tmp_path, {})
    assert accounts[0].root.children[0].name == "Two Words"


def test_missing_intermediate_folders_are_synthesised(tmp_path):
    """A child must never lose its parent, even if the parent holds no mail."""
    (tmp_path / "AAAAAAAA").mkdir()
    accounts = build_account_usage([("imap://AAAAAAAA/Parent/Child", 1, 10)], tmp_path, {})
    parent = accounts[0].root.children[0]
    assert parent.name == "Parent"
    assert parent.message_count == 0
    assert parent.total_messages == 1


def test_accounts_are_sorted_largest_first(tmp_path):
    accounts = build_account_usage(
        [("imap://AAAAAAAA/Small", 1, 10), ("imap://BBBBBBBB/Big", 1, 5000)],
        tmp_path,
        {},
    )
    assert [account.prefix for account in accounts] == [
        "imap://BBBBBBBB",
        "imap://AAAAAAAA",
    ]


def test_loose_account_files_are_kept_in_the_account_total(tmp_path):
    """Otherwise the account's folders would not sum to its directory size."""
    account = tmp_path / "AAAAAAAA"
    account.mkdir()
    (account / "AccountInfo.plist").write_bytes(b"x" * 4096)
    accounts = build_account_usage([("imap://AAAAAAAA/Parent", 1, 10)], tmp_path, {})
    assert accounts[0].root.own_disk_bytes >= 4096
    assert accounts[0].root.total_disk_bytes >= 4096


# --- Mail's own bookkeeping ---------------------------------------------------


def test_maildata_lists_immediate_children_largest_first(tmp_path):
    data = tmp_path / "MailData"
    (data / "Nested").mkdir(parents=True)
    (data / "Nested" / "big.db").write_bytes(b"x" * 40960)
    (data / "small.plist").write_bytes(b"x" * 10)
    assert [name for name, _ in maildata_usage(tmp_path)] == ["Nested", "small.plist"]


def test_maildata_is_empty_when_absent(tmp_path):
    assert maildata_usage(tmp_path) == []


def test_maildata_is_not_reported_as_an_account(tmp_path):
    """It holds no mail; listing it beside the accounts would be a lie."""
    (tmp_path / "MailData").mkdir()
    accounts = build_account_usage([("imap://AAAAAAAA/Parent", 1, 10)], tmp_path, {})
    assert [account.prefix for account in accounts] == ["imap://AAAAAAAA"]
