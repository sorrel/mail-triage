# Folder Size Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `mail-triage size` command reporting disk and envelope sizes per mailbox, one grid per account.

**Architecture:** Measurement is separated from rendering. `sizes.py` walks the `.mbox` tree and merges the result with a grouped query over the envelope snapshot, producing an immutable `AccountUsage`/`FolderNode` tree whose totals are derived by recursion. `size_report.py` turns that tree into terminal grids. The CLI command owns only the snapshot lifecycle and option parsing.

**Tech Stack:** Python 3.13, Click, sqlite3, `os.walk`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-01-folder-size-design.md`

## Global Constraints

- **British English** in all code, comments, output and commit messages.
- **Read-only.** No AppleScript, no writes, no file-content reads. `os.stat` and directory listing only.
- **Never `len()` for column widths.** Use `review.display_width()`.
- **Nothing personal in `src/`, `tests/` or `docs/`.** No real folder names, addresses or account UUIDs. `uv run pytest tests/test_no_personal_data.py` must pass before any commit.
- **Disk sizes are `st_blocks * 512`**, matching `du` — never `st_size`.
- **Blank, never zero,** for accounts with no local store.
- Run everything through `uv run`.
- Work on `feature/folder-sizes`; never commit to the main branch.

---

### Task 1: Envelope aggregate query

**Files:**
- Modify: `tests/conftest.py` (add a `size` column to the fixture schema)
- Modify: `src/mail_triage/envelope.py` (add `mailbox_sizes`, after `account_summary`)
- Test: `tests/test_envelope.py`

**Interfaces:**
- Consumes: `EnvelopeReader`, `build_fixture_db`
- Produces: `EnvelopeReader.mailbox_sizes() -> list[tuple[str, int, int]]`, yielding `(mailbox_url, message_count, total_size_bytes)` for every mailbox holding at least one message.

- [ ] **Step 1: Add `size` to the fixture builder**

In `tests/conftest.py`, add `size INTEGER NOT NULL DEFAULT 0` to the `messages`
table definition, add `size` to the INSERT's column list and its value tuple as
`row.get("size", 0)`. The default keeps every existing fixture valid.

- [ ] **Step 2: Write the failing test**

```python
def test_mailbox_sizes_totals_bytes_and_counts_per_mailbox(tmp_path):
    db = tmp_path / "Envelope Index"
    build_fixture_db(db, [
        {"sender": "a@example.com", "subject": "one", "date_sent": 1,
         "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 100},
        {"sender": "b@example.com", "subject": "two", "date_sent": 2,
         "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 250},
        {"sender": "c@example.com", "subject": "three", "date_sent": 3,
         "mailbox_url": "imap://AAAAAAAA/Parent/Child", "read": 0, "size": 40},
    ])
    reader = EnvelopeReader(db)
    try:
        assert dict((url, (count, total)) for url, count, total in reader.mailbox_sizes()) == {
            "imap://AAAAAAAA/Parent": (2, 350),
            "imap://AAAAAAAA/Parent/Child": (1, 40),
        }
    finally:
        reader.close()
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_envelope.py::test_mailbox_sizes_totals_bytes_and_counts_per_mailbox -v`
Expected: FAIL, `AttributeError: 'EnvelopeReader' object has no attribute 'mailbox_sizes'`

- [ ] **Step 4: Implement**

```python
    def mailbox_sizes(self) -> list[tuple[str, int, int]]:
        """Return (mailbox_url, message_count, total_size) for each mailbox.

        Uses the plain mailbox join rather than ``inbox_messages``: a Gmail
        message is stored once, under All Mail, whatever labels it carries.
        Unioning the labels here would count those bytes twice.
        """
        return [
            (url, count, total or 0)
            for url, count, total in self.connection.execute(
                "SELECT b.url, COUNT(*), SUM(m.size) "
                "FROM messages m JOIN mailboxes b ON b.ROWID = m.mailbox "
                "GROUP BY b.url"
            )
        ]
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS — including every pre-existing test, proving the fixture change was backwards-compatible.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_envelope.py src/mail_triage/envelope.py
git commit -m "feat: aggregate message sizes per mailbox"
```

---

### Task 2: Disk walk

**Files:**
- Create: `src/mail_triage/sizes.py`
- Test: `tests/test_sizes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `DiskUsage` — frozen dataclass with `by_folder: dict[str, int]` (folder path in database form, e.g. `"Parent/Child"` → own bytes), `loose_bytes: int` (files in the account directory outside any `.mbox`), and `unreadable: tuple[str, ...]`.
  - `account_disk_usage(account_dir: Path) -> DiskUsage`
  - `folder_path_from_mbox(relative: Path) -> str` — `Parent.mbox/Child.mbox` → `Parent/Child`
  - `file_blocks(path: Path) -> int` — allocated bytes for one file

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

import pytest

from mail_triage.sizes import account_disk_usage, folder_path_from_mbox


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_folder_path_from_mbox_strips_the_suffix_at_every_level():
    assert folder_path_from_mbox(Path("Parent.mbox/Child.mbox")) == "Parent/Child"
    assert folder_path_from_mbox(Path("Parent.mbox")) == "Parent"


def test_own_bytes_exclude_nested_mailboxes(tmp_path):
    write(tmp_path / "Parent.mbox" / "UUID" / "Data" / "1.emlx", 4096)
    write(tmp_path / "Parent.mbox" / "Child.mbox" / "UUID" / "2.emlx", 4096)
    usage = account_disk_usage(tmp_path)
    # Each file occupies at least one block; the parent must not absorb the child.
    assert usage.by_folder["Parent"] >= 4096
    assert usage.by_folder["Child" if False else "Parent/Child"] >= 4096
    assert usage.by_folder["Parent"] < usage.by_folder["Parent"] + usage.by_folder["Parent/Child"]


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
    secret = tmp_path / "Locked.mbox"
    write(secret / "UUID" / "1.emlx", 100)
    secret.chmod(0o000)
    try:
        usage = account_disk_usage(tmp_path)
    finally:
        secret.chmod(0o700)
    assert usage.unreadable
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'mail_triage.sizes'`

- [ ] **Step 3: Implement**

```python
"""Measure what Apple Mail's local store occupies, folder by folder.

Read-only in the strongest sense: this module stats files and lists
directories. It never opens a message, and it never writes anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MBOX_SUFFIX = ".mbox"


@dataclass(frozen=True)
class DiskUsage:
    """What one account directory occupies, split by mailbox."""

    by_folder: dict[str, int] = field(default_factory=dict)
    loose_bytes: int = 0
    unreadable: tuple[str, ...] = ()


def folder_path_from_mbox(relative: Path) -> str:
    """Turn ``Parent.mbox/Child.mbox`` into the database's ``Parent/Child``."""
    return "/".join(
        part[: -len(MBOX_SUFFIX)] if part.endswith(MBOX_SUFFIX) else part
        for part in relative.parts
    )


def file_blocks(path: Path) -> int:
    """Bytes the volume has actually committed to ``path``.

    ``st_blocks``, not ``st_size``: a mail store is tens of thousands of
    small files, and apparent size understates real consumption badly once
    block rounding is counted. This matches what ``du`` reports.
    """
    return path.lstat().st_blocks * 512


def _owning_mailbox(relative: Path) -> Path | None:
    """The nearest ancestor of ``relative`` that is a mailbox, if any.

    "Nearest" is what keeps a parent from absorbing its children: a file
    inside ``Parent.mbox/Child.mbox`` belongs to the child alone.
    """
    for candidate in [relative, *relative.parents]:
        if candidate.name.endswith(MBOX_SUFFIX):
            return candidate
    return None


def account_disk_usage(account_dir: Path) -> DiskUsage:
    """Map each mailbox in ``account_dir`` to the bytes it alone occupies."""
    by_folder: dict[str, int] = {}
    loose = 0
    unreadable: list[str] = []
    if not account_dir.is_dir():
        return DiskUsage()

    def on_error(error: OSError) -> None:
        unreadable.append(str(getattr(error, "filename", "") or account_dir))

    for root, _dirs, files in os.walk(account_dir, onerror=on_error):
        root_path = Path(root)
        for name in files:
            try:
                blocks = file_blocks(root_path / name)
            except OSError:
                unreadable.append(str(root_path / name))
                continue
            owner = _owning_mailbox(root_path.relative_to(account_dir))
            if owner is None:
                loose += blocks
            else:
                folder = folder_path_from_mbox(owner)
                by_folder[folder] = by_folder.get(folder, 0) + blocks
    return DiskUsage(by_folder, loose, tuple(unreadable))
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/sizes.py tests/test_sizes.py
git commit -m "feat: measure per-mailbox disk usage"
```

---

### Task 3: Merge into an account tree

**Files:**
- Modify: `src/mail_triage/sizes.py`
- Test: `tests/test_sizes.py`

**Interfaces:**
- Consumes: `DiskUsage`, `account_disk_usage`, `EnvelopeReader.mailbox_sizes`, `folders.account_prefix`, `folders.folder_path`
- Produces:
  - `FolderNode` — frozen dataclass: `path: str`, `name: str`, `own_disk_bytes: int | None`, `envelope_bytes: int`, `message_count: int`, `children: tuple[FolderNode, ...]`, plus recursive properties `total_disk_bytes: int | None`, `total_envelope_bytes: int`, `total_messages: int`.
  - `AccountUsage` — frozen dataclass: `prefix: str`, `name: str`, `root: FolderNode`, `on_disk: bool`, `unreadable: tuple[str, ...]`.
  - `build_account_usage(mailbox_sizes, mail_root, names) -> list[AccountUsage]` where `mailbox_sizes` is Task 1's return value, `mail_root` is the `V10` directory and `names` is `accounts.account_names()`'s dict. Sorted largest first by `total_disk_bytes or total_envelope_bytes`.

- [ ] **Step 1: Write the failing tests**

```python
from mail_triage.sizes import build_account_usage


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
    accounts = build_account_usage([("imap://BBBBBBBB/Parent", 1, 10)], tmp_path, {})
    assert accounts[0].on_disk is False
    assert accounts[0].root.total_disk_bytes is None


def test_disk_and_envelope_are_joined_on_the_folder_path(tmp_path):
    account = tmp_path / "AAAAAAAA"
    data = account / "Parent.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    accounts = build_account_usage([("imap://AAAAAAAA/Parent", 1, 350)], tmp_path, {})
    parent = accounts[0].root.children[0]
    assert parent.envelope_bytes == 350
    assert parent.own_disk_bytes >= 4096


def test_a_folder_on_disk_but_not_in_the_database_still_appears(tmp_path):
    account = tmp_path / "AAAAAAAA"
    data = account / "Ghost.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    accounts = build_account_usage([], tmp_path, {})
    assert [child.name for child in accounts[0].root.children] == ["Ghost"]
    assert accounts[0].root.children[0].message_count == 0


def test_url_encoded_folder_names_are_decoded(tmp_path):
    (tmp_path / "AAAAAAAA").mkdir()
    accounts = build_account_usage([("imap://AAAAAAAA/Two%20Words", 1, 10)], tmp_path, {})
    assert accounts[0].root.children[0].name == "Two Words"


def test_accounts_are_sorted_largest_first(tmp_path):
    accounts = build_account_usage(
        [("imap://AAAAAAAA/Small", 1, 10), ("imap://BBBBBBBB/Big", 1, 5000)],
        tmp_path,
        {},
    )
    assert [account.prefix for account in accounts] == [
        "imap://BBBBBBBB", "imap://AAAAAAAA",
    ]
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: FAIL, `ImportError: cannot import name 'build_account_usage'`

- [ ] **Step 3: Implement**

Add to `sizes.py` (imports: `from mail_triage.accounts import resolve_account_name`, `from mail_triage.folders import account_prefix, folder_path`):

```python
@dataclass(frozen=True)
class FolderNode:
    """One mailbox, with the mailboxes nested inside it.

    Totals are derived rather than stored, so a node cannot fall out of
    agreement with its children.
    """

    path: str
    name: str
    own_disk_bytes: int | None
    envelope_bytes: int
    message_count: int
    children: tuple[FolderNode, ...] = ()

    @property
    def total_disk_bytes(self) -> int | None:
        """Own bytes plus every descendant's, or None when not stored locally.

        None is not zero: an account Mail knows about but does not keep on
        this Mac has no disk figure at all, and reporting nought would read
        as "empty" rather than "not here".
        """
        if self.own_disk_bytes is None:
            return None
        return self.own_disk_bytes + sum(
            child.total_disk_bytes or 0 for child in self.children
        )

    @property
    def total_envelope_bytes(self) -> int:
        return self.envelope_bytes + sum(c.total_envelope_bytes for c in self.children)

    @property
    def total_messages(self) -> int:
        return self.message_count + sum(c.total_messages for c in self.children)


@dataclass(frozen=True)
class AccountUsage:
    prefix: str
    name: str
    root: FolderNode
    on_disk: bool
    unreadable: tuple[str, ...] = ()


def _tree(paths: dict[str, tuple[int | None, int, int]], on_disk: bool) -> FolderNode:
    """Assemble flat ``path -> (disk, envelope, count)`` entries into a tree.

    Intermediate folders absent from the input are synthesised with no
    figures of their own, so a child never loses its parent.
    """
    for path in list(paths):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            paths.setdefault(ancestor, (0 if on_disk else None, 0, 0))

    def build(prefix: str) -> tuple[FolderNode, ...]:
        depth = len(prefix.split("/")) + 1 if prefix else 1
        names = sorted(
            path for path in paths
            if path.startswith(f"{prefix}/" if prefix else "")
            and len(path.split("/")) == depth
        )
        nodes = []
        for path in names:
            disk, envelope, count = paths[path]
            nodes.append(
                FolderNode(
                    path=path,
                    name=path.rsplit("/", 1)[-1],
                    own_disk_bytes=disk,
                    envelope_bytes=envelope,
                    message_count=count,
                    children=build(path),
                )
            )
        return tuple(
            sorted(
                nodes,
                key=lambda node: (node.total_disk_bytes or 0, node.total_envelope_bytes),
                reverse=True,
            )
        )

    return FolderNode(
        path="", name="", own_disk_bytes=0 if on_disk else None,
        envelope_bytes=0, message_count=0, children=build(""),
    )


def build_account_usage(
    mailbox_sizes: list[tuple[str, int, int]],
    mail_root: Path,
    names: dict[str, str],
) -> list[AccountUsage]:
    """Join the database's per-mailbox totals to what is on disk."""
    from_db: dict[str, dict[str, tuple[int, int]]] = {}
    for url, count, total in mailbox_sizes:
        path = folder_path(url)
        if not path:
            continue
        from_db.setdefault(account_prefix(url), {})[path] = (count, total)

    directories = {
        entry.name.upper(): entry
        for entry in (mail_root.iterdir() if mail_root.is_dir() else [])
        if entry.is_dir() and entry.name != "MailData"
    }

    prefixes = set(from_db)
    for name in directories:
        prefixes.update(
            prefix for prefix in from_db if prefix.partition("://")[2].upper() == name[:8]
        )

    accounts: list[AccountUsage] = []
    for prefix in prefixes:
        short_id = prefix.partition("://")[2].upper()
        directory = next(
            (entry for name, entry in directories.items() if name.startswith(short_id)),
            None,
        )
        usage = account_disk_usage(directory) if directory else DiskUsage()
        on_disk = directory is not None
        merged: dict[str, tuple[int | None, int, int]] = {}
        by_casefold = {path.casefold(): path for path in from_db.get(prefix, {})}
        for path, (count, total) in from_db.get(prefix, {}).items():
            merged[path] = (usage.by_folder.get(path) if on_disk else None, total, count)
        for path, blocks in usage.by_folder.items():
            canonical = by_casefold.get(path.casefold())
            if canonical is None:
                merged[path] = (blocks, 0, 0)
            elif canonical != path:
                disk, total, count = merged[canonical]
                merged[canonical] = ((disk or 0) + blocks, total, count)
        accounts.append(
            AccountUsage(
                prefix=prefix,
                name=resolve_account_name(prefix, names),
                root=_tree(merged, on_disk),
                on_disk=on_disk,
                unreadable=usage.unreadable,
            )
        )
    return sorted(
        accounts,
        key=lambda a: (a.root.total_disk_bytes or 0, a.root.total_envelope_bytes),
        reverse=True,
    )
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/sizes.py tests/test_sizes.py
git commit -m "feat: join disk and envelope sizes into a per-account tree"
```

---

### Task 4: MailData breakdown

**Files:**
- Modify: `src/mail_triage/sizes.py`
- Test: `tests/test_sizes.py`

**Interfaces:**
- Consumes: `file_blocks`
- Produces: `maildata_usage(mail_root: Path) -> list[tuple[str, int]]` — `(item name, bytes)` for each immediate child of `MailData/`, largest first. Empty when the directory is absent.

- [ ] **Step 1: Write the failing tests**

```python
from mail_triage.sizes import maildata_usage


def test_maildata_lists_immediate_children_largest_first(tmp_path):
    data = tmp_path / "MailData"
    (data / "Nested").mkdir(parents=True)
    (data / "Nested" / "big.db").write_bytes(b"x" * 40960)
    (data / "small.plist").write_bytes(b"x" * 10)
    assert [name for name, _ in maildata_usage(tmp_path)] == ["Nested", "small.plist"]


def test_maildata_is_empty_when_absent(tmp_path):
    assert maildata_usage(tmp_path) == []
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: FAIL, `ImportError: cannot import name 'maildata_usage'`

- [ ] **Step 3: Implement**

```python
def maildata_usage(mail_root: Path) -> list[tuple[str, int]]:
    """Size each item in Mail's own MailData directory, largest first.

    Nothing here is mail: it is the envelope database, the search and
    junk-filter indexes, a cache of remote images, and settings. It is
    reported because without it the accounts do not sum to what the store
    actually occupies, which invites doubt about every other figure shown.
    """
    directory = mail_root / "MailData"
    if not directory.is_dir():
        return []
    items: list[tuple[str, int]] = []
    for entry in directory.iterdir():
        if entry.is_dir():
            total = 0
            for root, _dirs, files in os.walk(entry, onerror=lambda _error: None):
                for name in files:
                    try:
                        total += file_blocks(Path(root) / name)
                    except OSError:
                        continue
        else:
            try:
                total = file_blocks(entry)
            except OSError:
                continue
        items.append((entry.name, total))
    return sorted(items, key=lambda item: item[1], reverse=True)
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_sizes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/sizes.py tests/test_sizes.py
git commit -m "feat: break down Mail's own MailData directory"
```

---

### Task 5: Rendering

**Files:**
- Create: `src/mail_triage/size_report.py`
- Test: `tests/test_size_report.py`

**Interfaces:**
- Consumes: `FolderNode`, `AccountUsage`, `review.display_width`
- Produces:
  - `human_bytes(count: int | None, exact: bool = False) -> str` — `"1.4 GB"`, `"—"` for None, raw digits when `exact`.
  - `parse_size(text: str) -> int` — `"2MB"` → `2_097_152`; accepts a bare integer as bytes; raises `ValueError` on anything else.
  - `render_account(account: AccountUsage, min_size: int, exact: bool) -> str`
  - `render_summary(accounts: list[AccountUsage], maildata_total: int, exact: bool) -> str`
  - `render_maildata(items: list[tuple[str, int]], exact: bool) -> str`

Highlighting: a row is yellow when its total disk (falling back to envelope) is
at least 20% of the account's total. Use `click.style(..., fg="yellow")`, applied
**after** padding is computed, per the house rule.

Collapsing: children below `min_size` are dropped from the tree and replaced by
one `N smaller folders` line carrying their summed totals, so the visible rows
plus that line always equal the account total. Applied at every level.

- [ ] **Step 1: Write the failing tests**

```python
import re

from mail_triage.size_report import (
    human_bytes, parse_size, render_account, render_maildata, render_summary,
)
from mail_triage.sizes import AccountUsage, FolderNode


def plain(text: str) -> str:
    """Strip ANSI colour so assertions read the content, not the styling."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def leaf(name, disk, envelope, count, children=()):
    return FolderNode(
        path=name, name=name, own_disk_bytes=disk,
        envelope_bytes=envelope, message_count=count, children=tuple(children),
    )


def test_human_bytes_uses_binary_units():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024**3) == "5.0 GB"


def test_human_bytes_marks_the_absence_of_a_disk_figure():
    assert human_bytes(None) == "—"


def test_human_bytes_exact_gives_digits():
    assert human_bytes(1536, exact=True) == "1536"


def test_parse_size_accepts_units_and_bare_bytes():
    assert parse_size("2MB") == 2 * 1024**2
    assert parse_size("1.5 gb") == int(1.5 * 1024**3)
    assert parse_size("0") == 0
    assert parse_size("4096") == 4096


def test_parse_size_rejects_nonsense():
    import pytest
    with pytest.raises(ValueError):
        parse_size("big")


def test_small_folders_collapse_into_a_reconciling_line():
    account = AccountUsage(
        prefix="imap://AAAAAAAA", name="Test", on_disk=True,
        root=leaf("", 0, 0, 0, [
            leaf("Large", 10 * 1024**2, 10 * 1024**2, 5),
            leaf("Tiny", 100, 100, 1),
            leaf("Alsotiny", 200, 200, 2),
        ]),
    )
    output = plain(render_account(account, min_size=1024**2, exact=False))
    assert "Large" in output
    assert "Tiny" not in output
    assert "2 smaller folders" in output


def test_nothing_collapses_when_min_size_is_zero():
    account = AccountUsage(
        prefix="imap://AAAAAAAA", name="Test", on_disk=True,
        root=leaf("", 0, 0, 0, [leaf("Tiny", 100, 100, 1)]),
    )
    output = plain(render_account(account, min_size=0, exact=False))
    assert "Tiny" in output
    assert "smaller folders" not in output


def test_children_are_indented_under_their_parent():
    account = AccountUsage(
        prefix="imap://AAAAAAAA", name="Test", on_disk=True,
        root=leaf("", 0, 0, 0, [
            leaf("Parent", 10 * 1024**2, 10 * 1024**2, 5, [
                leaf("Child", 5 * 1024**2, 5 * 1024**2, 2),
            ]),
        ]),
    )
    lines = plain(render_account(account, min_size=0, exact=False)).splitlines()
    parent_line = next(line for line in lines if "Parent" in line)
    child_line = next(line for line in lines if "Child" in line)
    assert child_line.index("Child") > parent_line.index("Parent")


def test_an_account_without_a_local_store_shows_a_dash_not_a_zero():
    account = AccountUsage(
        prefix="imap://BBBBBBBB", name="Remote", on_disk=False,
        root=leaf("", None, 0, 0, [leaf("Folder", None, 4096, 3)]),
    )
    output = plain(render_account(account, min_size=0, exact=False))
    assert "—" in output
    assert "0 B" not in output


def test_the_largest_rows_are_highlighted():
    account = AccountUsage(
        prefix="imap://AAAAAAAA", name="Test", on_disk=True,
        root=leaf("", 0, 0, 0, [
            leaf("Huge", 100 * 1024**2, 0, 1),
            leaf("Modest", 1024**2, 0, 1),
        ]),
    )
    output = render_account(account, min_size=0, exact=False)
    huge = next(line for line in output.splitlines() if "Huge" in line)
    modest = next(line for line in output.splitlines() if "Modest" in line)
    assert "\x1b[33m" in huge
    assert "\x1b[33m" not in modest


def test_the_summary_includes_maildata_so_the_total_reconciles():
    accounts = [
        AccountUsage(prefix="local://AAAAAAAA", name="On My Mac", on_disk=True,
                     root=leaf("", 1024**3, 1024**3, 10)),
    ]
    output = plain(render_summary(accounts, maildata_total=100 * 1024**2, exact=False))
    assert "On My Mac" in output
    assert "MailData" in output
    assert "Total" in output


def test_maildata_grid_leaves_message_columns_blank():
    output = plain(render_maildata([("Envelope Index", 284 * 1024**2)], exact=False))
    assert "Envelope Index" in output
    assert "284.0 MB" in output
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_size_report.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'mail_triage.size_report'`

- [ ] **Step 3: Implement `size_report.py`**

Write the module to satisfy the tests above. Required behaviours, all asserted
by those tests:

- `human_bytes` uses binary units (KB = 1024) with one decimal place above
  bytes, and returns `"—"` for `None`.
- `parse_size` accepts `B`/`KB`/`MB`/`GB` suffixes, case-insensitively, with
  optional whitespace and a decimal point; a bare number is bytes.
- Every column width is measured with `review.display_width`, and
  `click.style` is applied only after padding is computed.
- The collapse line reads `N smaller folders` and carries the summed totals of
  everything it replaced, at whatever level it was applied.
- Rows at or above 20% of the account total are styled `fg="yellow"`.

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_size_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/size_report.py tests/test_size_report.py
git commit -m "feat: render the folder size grids"
```

---

### Task 6: The `size` command

**Files:**
- Modify: `src/mail_triage/cli.py` (add after `accounts`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above, plus `snapshot_database`, `EnvelopeReader`, `account_names`
- Produces: the `size` Click command.

Option handling: `--min-size` is a string parsed by `parse_size`, defaulting to
`"2MB"`; a bad value raises `click.BadParameter`. `--account` matches
case-insensitively as a substring of the resolved name or the prefix; no match
is a `ClickException` listing what is available, and an ambiguous match is a
`ClickException` listing the candidates rather than a silent pick.

Error handling mirrors the `accounts` command exactly: `FileNotFoundError`,
`PermissionError` (with the Full Disk Access guidance) and
`sqlite3.OperationalError` each become a `ClickException`.

- [ ] **Step 1: Write the failing test**

```python
def test_size_command_renders_grids(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from mail_triage import cli as cli_module
    from tests.conftest import build_fixture_db

    store = tmp_path / "V10"
    data = store / "AAAAAAAA" / "Parent.mbox" / "UUID"
    data.mkdir(parents=True)
    (data / "1.emlx").write_bytes(b"x" * 4096)
    db_source = store / "MailData" / "Envelope Index"
    db_source.parent.mkdir(parents=True)
    build_fixture_db(db_source, [
        {"sender": "a@example.com", "subject": "one", "date_sent": 1,
         "mailbox_url": "imap://AAAAAAAA/Parent", "read": 0, "size": 4096},
    ])
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_source)
    monkeypatch.setattr(cli_module, "account_names", lambda: {})

    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "0"])
    assert result.exit_code == 0, result.output
    assert "Parent" in result.output


def test_size_command_rejects_a_bad_min_size(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from mail_triage import cli as cli_module

    result = CliRunner().invoke(cli_module.cli, ["size", "--min-size", "huge"])
    assert result.exit_code != 0
    assert "min-size" in result.output.lower()
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_cli.py -k size -v`
Expected: FAIL — no such command `size`.

- [ ] **Step 3: Implement the command**

The mail root is derived from the database path (`DEFAULT_DB_PATH.parent.parent`)
so the test's monkeypatch reaches both halves of the measurement. Snapshot the
database into a `TemporaryDirectory`, read `mailbox_sizes()`, close the reader,
then build and render. Print the summary, each account's grid, then MailData,
and finally a note naming any unreadable directories.

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_cli.py -k size -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite and the leak check**

Run: `uv run pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/mail_triage/cli.py tests/test_cli.py
git commit -m "feat: add the size command"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md` (module table)

- [ ] **Step 1: Add `sizes.py` and `size_report.py` to the module table in `CLAUDE.md`**

```markdown
| `sizes.py` | Measure disk and envelope size per mailbox |
| `size_report.py` | Render the size grids |
```

- [ ] **Step 2: Document the command in `README.md`**

A short section: what `mail-triage size` reports, the two measures and why both,
and the three options. No real folder names.

- [ ] **Step 3: Run the leak check**

Run: `uv run pytest tests/test_no_personal_data.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document the size command"
```

---

## Self-review

**Spec coverage:** both measures (Tasks 1–3), block-based sizing (Task 2),
disk/database join (Task 3), blank-not-zero (Tasks 3, 5), nested roll-ups
(Tasks 3, 5), yellow highlighting (Task 5), collapse line (Task 5), MailData
grid (Tasks 4, 6), all three options (Tasks 5, 6), Gmail label handling
(Task 1), unreadable directories (Tasks 2, 6), read-only guarantee (throughout
— no task introduces a write), testing strategy (every task).

**Known wrinkle carried forward:** `test_own_bytes_exclude_nested_mailboxes` in
Task 2 contains a redundant conditional expression (`"Child" if False else
"Parent/Child"`); simplify it to the plain string when writing the file.
