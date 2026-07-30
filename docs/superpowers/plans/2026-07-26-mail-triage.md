# mail-triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local-first CLI that reads the Apple Mail inbox, classifies each message against the folders the user already uses, proposes moves for confirmation, learns from corrections, and later offers unsubscribe suggestions.

**Architecture:** Read bulk data from a snapshot copy of Mail's `Envelope Index` SQLite database (read-only, never the live file). Decide entirely in-process using a three-stage classifier: sender/domain map, then hand-rolled naive Bayes, then an optional LLM. Act only through AppleScript, journalling every intended move first so any batch can be undone.

**Tech Stack:** Python 3.13, `uv`, Click, stdlib `sqlite3`, AppleScript via `osascript`. No scikit-learn — the Bayes is hand-rolled. `pytest` for tests.

## Global Constraints

- **British English** in all code, comments, output, documentation and commit messages.
- **Publishable repo.** No module under `src/` may contain a folder name, email address, account UUID, or any other personal literal. Tests use synthetic fixtures only.
- **`local/` is gitignored** and holds every identifying artefact: model, corrections, journals, real config, cached headers.
- **Never write to Mail's SQLite database.** Read from a snapshot copy only; all mutation goes through AppleScript.
- **Src layout:** `[tool.hatch.build.targets.wheel] packages = ["src/mail_triage"]`, `[tool.pytest.ini_options] pythonpath = ["src", "."]`.
- **Never commit to `master`.** All work on `feature/<name>` branches.
- **No live-mail mutation without explicit user consent.** Tasks that move or send real mail are marked **CHECKPOINT** and must stop for approval.
- **Run everything via `uv run`.**

## Verified environment facts

These were confirmed against the real machine on 26 July 2026. Do not re-derive them; do check them still hold if something behaves oddly.

- Mail database: `~/Library/Mail/V10/MailData/Envelope Index` (plus `-wal`, `-shm`). 74,075 messages, 194 mailboxes.
- **Mail's AppleScript message `id` equals the SQLite `messages.ROWID`.** Verified on three live inbox messages by subject comparison. This is the identity join. Note the ROWID is *not* stable: a message moved between mailboxes gets a new one (447166 → 447759, observed 26 July 2026 across a move and its undo).
- ~~the spec's stated RFC-822 `Message-ID` key **does not exist in the database**~~ — **WRONG, corrected 26 July 2026.** It does: `message_global_data.message_id_header` holds the full RFC-822 string (`<…@host>`), joinable to `messages` via the 64-bit hash in `messages.message_id` = `message_global_data.message_id`. Verified by resolving a known key straight to the correct current ROWID and mailbox. This means a durable key *can* be resolved to a message's current location in milliseconds via SQLite, with no AppleScript involved — relevant to undo, and to any future check of "where did this message actually end up".
- ~~Raw headers are **not** in the database~~ — **partly wrong.** The full header block is not, but Mail pre-computes several fields the plan assumed needed an AppleScript fetch:
  - `messages.unsubscribe_type` — Mail's own unsubscribe classification (values 0,1,2,3,6,7 seen; NULL for 71,204 of 74,075, so populated only for a subset — do not treat NULL as "no unsubscribe option"). Potentially replaces the per-message `all headers of message` fetch in the unsubscribe work (Task 16).
  - `message_global_data.follow_up_start_date` / `read_later_date` — Mail's own follow-up and read-later flags, directly relevant to the "awaiting a reply or action" guard.
  - `messages.list_id_hash`, `brand_indicator`, `is_urgent`, `conversation_id`.

  None of these are used yet; they are recorded because the current header-fetch step is the slowest part of a dry run.
- `mailboxes.url` format: `<scheme>://<account-uuid>/<url-encoded/folder/path>`, schemes `imap:`, `ews:`, `local:`. Nested folders appear as `/`-separated path segments; 46 mailboxes are nested.
- **Nested folders, verified 26 July 2026 after Task 6 — this corrects the plan.** The user's folders are almost entirely nested: of the 44 folders the trained model learnt, **41 contain a `/`** (`Parent/Orders`, `Team/Tech/Cloud`). Two consequences:
  1. **Mail's AppleScript `name of mailboxes of account` returns a FLAT list of LEAF names** (`Orders`, `Cloud`) — not paths. Matching model folders against that list fails for all 41 nested folders, so a classifier checking predictions against it would reject virtually every proposal as "folder does not exist". **Source the available-folder list from the envelope database instead** (`folder_path(url)` for mailboxes under the configured account prefix), which yields full paths *with real capitalisation* — `Parent/Accounts/Security`, not `parent/accounts/security`.
  2. **`first mailbox of account whose name is "X"` cannot address a nested mailbox** and is ambiguous when a leaf name repeats. **Path addressing works and is verified:** `mailbox "Parent/Orders" of account "iCloud"` resolves correctly. Use it for every move.
- Message history is split: `local://` (On My Mac) holds 53,034 messages across 47 folders; the iCloud IMAP account holds 19,002 across 52 folders. **36 folder names appear in both.** The local archive is older iCloud mail that the user used to move off the server yearly to save space — the same folders, just older. That practice has lapsed.
  **Decision (26 July 2026): train on the iCloud account only.** 19,002 messages is ample, and the archive is precisely the mail that recency weighting would discount anyway. Training scope is a config list so the archive can be folded in later without code changes. Folder-name normalisation (Task 3) is still required for URL parsing and for matching predictions to real mailboxes, and it means enabling the archive later is a one-line config edit.
- `messages.date_sent` is Unix epoch seconds. `messages.read` is 0/1.
- Core join resolves 74,066 of 74,075 messages:
  `messages` → `addresses` (sender) → `subjects` (subject) → `mailboxes` (mailbox).

## File structure

| File | Responsibility |
|---|---|
| `src/mail_triage/cli.py` | Click command definitions only; no logic |
| `src/mail_triage/config.py` | Load/validate TOML config, resolve `local/` paths |
| `src/mail_triage/envelope.py` | Snapshot the database; typed read-only queries |
| `src/mail_triage/folders.py` | Parse mailbox URLs; normalise and match folder names across accounts |
| `src/mail_triage/corpus.py` | Turn history rows into recency-weighted training examples |
| `src/mail_triage/model/sender.py` | Stage A: sender/domain → folder, with consistency gating |
| `src/mail_triage/model/tokens.py` | Stage B: hand-rolled naive Bayes |
| `src/mail_triage/model/llm.py` | Stage C: optional LLM tier plus redaction |
| `src/mail_triage/model/classify.py` | Stage orchestration; produces `Proposal` |
| `src/mail_triage/model/store.py` | Serialise/deserialise the trained model to `local/` |
| `src/mail_triage/mail_app.py` | AppleScript bridge — the only writer — behind an interface |
| `src/mail_triage/review.py` | Propose-then-confirm interaction |
| `src/mail_triage/journal.py` | Run journal and undo |
| `src/mail_triage/corrections.py` | Record and load user corrections |
| `src/mail_triage/unsubscribe.py` | Detect unsubscribe options; send unsubscribe mail |

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`, `config.example.toml`, `src/mail_triage/__init__.py`, `src/mail_triage/config.py`, `src/mail_triage/cli.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Config` dataclass with fields `account_url_prefix: str`, `inbox_folder: str`, `training_accounts: list[str]`, `training_exclusions: list[str]`, `confidence_threshold: float`, `auto_threshold: float`, `half_life_days: float`, `correction_weight: float`, `local_dir: Path`; `load_config(path: Path | None = None) -> Config`; `Config.model_path`, `Config.corrections_path`, `Config.journal_dir`, `Config.training_prefixes` properties.

`training_accounts` is a list of account prefixes to learn from. Empty (the default) means "the account being triaged, and only that" — `Config.training_prefixes` resolves it to `[account_url_prefix]`. Adding the On My Mac archive later is a config edit, not a code change.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/scaffold-and-config
```

- [x] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "mail-triage"
version = "0.1.0"
description = "Local-first triage for Apple Mail"
requires-python = ">=3.13"
dependencies = ["click>=8.0.0"]

[project.scripts]
mail-triage = "mail_triage.cli:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mail_triage"]

[tool.pytest.ini_options]
pythonpath = ["src", "."]

[dependency-groups]
dev = ["pytest>=8.0.0"]
```

- [x] **Step 3: Write the failing test**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from mail_triage.config import Config, load_config


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
```

- [x] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.config'`

- [x] **Step 5: Implement `config.py`**

```python
"""Configuration loading for mail-triage.

Real configuration lives in ``local/config.toml`` which is never committed.
``config.example.toml`` in the repository root documents the shape without
containing anything personal.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUSIONS = [
    "INBOX",
    "Junk",
    "Spam",
    "Sent*",
    "Drafts*",
    "Outbox",
    "Deleted*",
    "Trash",
    "Archive",
    "Recovered Messages*",
]


@dataclass(frozen=True)
class Config:
    """Runtime settings. Thresholds are probabilities in the range 0..1."""

    account_url_prefix: str
    local_dir: Path
    inbox_folder: str = "INBOX"
    training_accounts: list[str] = field(default_factory=list)
    training_exclusions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))
    confidence_threshold: float = 0.7
    auto_threshold: float = 0.9
    half_life_days: float = 365.0
    correction_weight: float = 10.0

    @property
    def training_prefixes(self) -> list[str]:
        """Accounts to learn from; by default, only the account being triaged.

        The On My Mac archive holds older mail moved off the server yearly. It
        uses the same folder names, so it can be folded in by listing its prefix
        in ``training_accounts`` — no code change needed.
        """
        return self.training_accounts or [self.account_url_prefix]

    @property
    def model_path(self) -> Path:
        return self.local_dir / "model.json"

    @property
    def corrections_path(self) -> Path:
        return self.local_dir / "corrections.jsonl"

    @property
    def journal_dir(self) -> Path:
        return self.local_dir / "journal"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> Config:
    """Load configuration, defaulting to ``local/config.toml``."""
    if path is None:
        path = _project_root() / "local" / "config.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"No configuration at {path}. Copy config.example.toml to local/config.toml "
            "and run 'mail-triage accounts' to find your account prefix."
        )
    values = tomllib.loads(path.read_text())
    if "account_url_prefix" not in values:
        raise ValueError("config must set account_url_prefix")
    local_dir = Path(values.pop("local_dir", path.parent))
    return Config(local_dir=local_dir, **values)
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (3 tests)

- [x] **Step 7: Write `config.example.toml`**

```toml
# Copy to local/config.toml and edit. local/ is gitignored.
# Run 'mail-triage accounts' to discover your account prefix.

# Which account to triage, as scheme://first-8-chars-of-account-uuid
account_url_prefix = "imap://XXXXXXXX"

# Name of the inbox mailbox within that account
inbox_folder = "INBOX"

# Accounts to learn from. Empty means "only the account being triaged".
# Add an archive account's prefix here to learn from it too — useful if older
# mail was moved to local storage but uses the same folder names.
training_accounts = []

# Folder-name patterns excluded from training (fnmatch syntax, case-insensitive)
training_exclusions = [
    "INBOX", "Junk", "Spam", "Sent*", "Drafts*", "Outbox",
    "Deleted*", "Trash", "Archive", "Recovered Messages*",
]

# Minimum confidence to propose a move at all
confidence_threshold = 0.7
# Minimum confidence for --auto to move without asking
auto_threshold = 0.9
# Recency decay: a filing decision this old counts half as much
half_life_days = 365.0
# How much more a correction counts than a historical filing
correction_weight = 10.0
```

- [x] **Step 8: Write a minimal `cli.py`**

```python
"""Command-line entry point."""

from __future__ import annotations

import click


@click.group()
@click.version_option()
def cli() -> None:
    """Local-first triage for Apple Mail."""


if __name__ == "__main__":
    cli()
```

Also create an empty `src/mail_triage/__init__.py`.

- [x] **Step 9: Verify the CLI runs**

Run: `uv run mail-triage --help`
Expected: usage text, exit 0

- [x] **Step 10: Commit**

```bash
git add pyproject.toml config.example.toml src tests uv.lock
git commit -m "feat: project scaffold and configuration loading"
```

---

### Task 2: Envelope Index snapshot and reader

**Files:**
- Create: `src/mail_triage/envelope.py`
- Modify: `src/mail_triage/cli.py` (add `accounts` command)
- Test: `tests/test_envelope.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `Config` from Task 1.
- Produces: `MessageRow` dataclass (`rowid: int`, `sender: str`, `subject: str`, `date_sent: int`, `mailbox_url: str`, `read: bool`); `snapshot_database(source: Path, dest_dir: Path) -> Path`; `EnvelopeReader(db_path: Path)` with `.all_messages() -> Iterator[MessageRow]`, `.messages_in_mailbox(url: str) -> Iterator[MessageRow]`, `.mailbox_urls() -> list[str]`, `.account_summary() -> list[tuple[str, int, int]]` returning `(account_prefix, mailbox_count, message_count)`; `DEFAULT_DB_PATH: Path`.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/envelope-reader
```

- [x] **Step 2: Write the test fixture builder**

`tests/conftest.py`:

```python
"""Synthetic Envelope Index fixtures.

Deliberately mirrors the real schema's shape (normalised senders and subjects)
without containing any real data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def build_fixture_db(path: Path, rows: list[dict]) -> None:
    """Create a miniature Envelope Index at ``path``.

    Each row dict needs: sender, subject, date_sent, mailbox_url, read.
    """
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '');
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT NOT NULL);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT NOT NULL);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            sender INTEGER, subject INTEGER NOT NULL,
            date_sent INTEGER, mailbox INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    addresses: dict[str, int] = {}
    subjects: dict[str, int] = {}
    mailboxes: dict[str, int] = {}

    def intern(table: str, column: str, cache: dict[str, int], value: str) -> int:
        if value not in cache:
            cache[value] = len(cache) + 1
            db.execute(f"INSERT INTO {table} (ROWID, {column}) VALUES (?, ?)", (cache[value], value))
        return cache[value]

    for index, row in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO messages (ROWID, sender, subject, date_sent, mailbox, read) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.get("rowid", index),
                intern("addresses", "address", addresses, row["sender"]),
                intern("subjects", "subject", subjects, row["subject"]),
                row["date_sent"],
                intern("mailboxes", "url", mailboxes, row["mailbox_url"]),
                int(row.get("read", 0)),
            ),
        )
    db.commit()
    db.close()


@pytest.fixture
def fixture_db(tmp_path):
    """A small database: two accounts, overlapping folder names."""
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "orders@shop.example", "subject": "Your order", "date_sent": 1_700_000_000,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "orders@shop.example", "subject": "Dispatched", "date_sent": 1_700_100_000,
             "mailbox_url": "imap://AAAAAAAA/Orders", "read": 1},
            {"sender": "news@list.example", "subject": "Weekly digest", "date_sent": 1_700_200_000,
             "mailbox_url": "local://BBBBBBBB/Newsletters", "read": 0},
            {"sender": "someone@work.example", "subject": "Standup notes", "date_sent": 1_700_300_000,
             "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
        ],
    )
    return path
```

- [x] **Step 3: Write the failing test**

`tests/test_envelope.py`:

```python
from mail_triage.envelope import EnvelopeReader, snapshot_database


def test_reads_all_messages(fixture_db):
    reader = EnvelopeReader(fixture_db)
    rows = list(reader.all_messages())
    assert len(rows) == 4
    first = next(r for r in rows if r.subject == "Your order")
    assert first.sender == "orders@shop.example"
    assert first.mailbox_url == "imap://AAAAAAAA/Orders"
    assert first.read is True


def test_messages_in_mailbox_filters(fixture_db):
    reader = EnvelopeReader(fixture_db)
    rows = list(reader.messages_in_mailbox("imap://AAAAAAAA/INBOX"))
    assert [r.subject for r in rows] == ["Standup notes"]


def test_account_summary_groups_by_account(fixture_db):
    reader = EnvelopeReader(fixture_db)
    summary = dict((prefix, (boxes, msgs)) for prefix, boxes, msgs in reader.account_summary())
    assert summary["imap://AAAAAAAA"] == (2, 3)
    assert summary["local://BBBBBBBB"] == (1, 1)


def test_snapshot_copies_database(fixture_db, tmp_path):
    dest = tmp_path / "snap"
    copied = snapshot_database(fixture_db, dest)
    assert copied.exists()
    assert copied != fixture_db
    assert list(EnvelopeReader(copied).all_messages())


def test_reader_opens_read_only(fixture_db):
    reader = EnvelopeReader(fixture_db)
    import sqlite3
    import pytest as _pytest
    with _pytest.raises(sqlite3.OperationalError):
        reader.connection.execute("DELETE FROM messages")
```

- [x] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_envelope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.envelope'`

- [x] **Step 5: Implement `envelope.py`**

```python
"""Read-only access to Apple Mail's Envelope Index.

Mail owns the live database. We never open it directly for anything but a
copy, and we never write to it under any circumstances.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "Library" / "Mail" / "V10" / "MailData" / "Envelope Index"

_BASE_QUERY = """
    SELECT m.ROWID, a.address, s.subject, m.date_sent, b.url, m.read
    FROM messages m
    JOIN addresses a ON a.ROWID = m.sender
    JOIN subjects s ON s.ROWID = m.subject
    JOIN mailboxes b ON b.ROWID = m.mailbox
"""


@dataclass(frozen=True)
class MessageRow:
    """One message as stored in the envelope database.

    ``rowid`` is also Mail's AppleScript message id — verified against the live
    application. It is the join key between this database and Mail itself.
    """

    rowid: int
    sender: str
    subject: str
    date_sent: int
    mailbox_url: str
    read: bool


def snapshot_database(source: Path = DEFAULT_DB_PATH, dest_dir: Path | None = None) -> Path:
    """Copy the database and its write-ahead log to ``dest_dir``.

    Copying the -wal and -shm companions keeps the snapshot consistent with
    what Mail has most recently written.
    """
    if dest_dir is None:
        raise ValueError("dest_dir is required")
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / source.name
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        companion = source.with_name(source.name + suffix)
        if companion.exists():
            shutil.copy2(companion, dest_dir / companion.name)
    return destination


class EnvelopeReader:
    """Typed, read-only queries over an envelope database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def _rows(self, where: str = "", params: tuple = ()) -> Iterator[MessageRow]:
        for rowid, sender, subject, date_sent, url, read in self.connection.execute(
            _BASE_QUERY + where, params
        ):
            yield MessageRow(
                rowid=rowid,
                sender=sender,
                subject=subject or "",
                date_sent=date_sent or 0,
                mailbox_url=url,
                read=bool(read),
            )

    def all_messages(self) -> Iterator[MessageRow]:
        yield from self._rows()

    def messages_in_mailbox(self, url: str) -> Iterator[MessageRow]:
        yield from self._rows("WHERE b.url = ?", (url,))

    def mailbox_urls(self) -> list[str]:
        return [url for (url,) in self.connection.execute("SELECT url FROM mailboxes")]

    def account_summary(self) -> list[tuple[str, int, int]]:
        """Return (account_prefix, mailbox_count, message_count) per account."""
        from mail_triage.folders import account_prefix

        boxes: dict[str, int] = {}
        for url in self.mailbox_urls():
            boxes[account_prefix(url)] = boxes.get(account_prefix(url), 0) + 1
        messages: dict[str, int] = {}
        for (url, count) in self.connection.execute(
            "SELECT b.url, COUNT(*) FROM messages m JOIN mailboxes b ON b.ROWID = m.mailbox GROUP BY b.url"
        ):
            messages[account_prefix(url)] = messages.get(account_prefix(url), 0) + count
        return sorted(
            ((prefix, boxes[prefix], messages.get(prefix, 0)) for prefix in boxes),
            key=lambda item: item[2],
            reverse=True,
        )

    def close(self) -> None:
        self.connection.close()
```

This depends on `folders.account_prefix`, written in Task 3. Write Task 3 first if executing strictly in order, or stub `account_prefix` here and move it in Task 3. **Recommended: swap the order — do Task 3 before Task 2's `account_summary`.**

- [x] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_envelope.py -v`
Expected: PASS (5 tests)

- [x] **Step 7: Add the `accounts` CLI command**

In `cli.py`:

```python
import tempfile
from pathlib import Path

from mail_triage.envelope import DEFAULT_DB_PATH, EnvelopeReader, snapshot_database


@cli.command()
def accounts() -> None:
    """List mail accounts with mailbox and message counts.

    Use the prefix shown here as account_url_prefix in local/config.toml.
    """
    if not DEFAULT_DB_PATH.exists():
        raise click.ClickException(
            f"Cannot find {DEFAULT_DB_PATH}. Is this macOS with Apple Mail configured?"
        )
    with tempfile.TemporaryDirectory() as work:
        try:
            snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
        except PermissionError as error:
            raise click.ClickException(
                "Cannot read Mail's database. Grant Full Disk Access to your terminal "
                "in System Settings → Privacy & Security → Full Disk Access."
            ) from error
        reader = EnvelopeReader(snapshot)
        click.echo(f"{'Account':<28}{'Mailboxes':>10}{'Messages':>10}")
        for prefix, mailbox_count, message_count in reader.account_summary():
            click.echo(f"{prefix:<28}{mailbox_count:>10}{message_count:>10}")
        reader.close()
```

- [x] **Step 8: Verify against the real database**

Run: `uv run mail-triage accounts`
Expected: a table of accounts. The iCloud account should show roughly 52 mailboxes and ~19,000 messages; a `local://` account roughly 47 and ~53,000. Read-only — nothing is modified.

- [x] **Step 9: Commit**

```bash
git add src/mail_triage/envelope.py src/mail_triage/cli.py tests/
git commit -m "feat: read-only envelope database snapshot and reader"
```

---

### Task 3: Folder naming across accounts

**Files:**
- Create: `src/mail_triage/folders.py`
- Test: `tests/test_folders.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `account_prefix(url: str) -> str`; `folder_path(url: str) -> str` (decoded, `/`-separated, no leading slash); `normalise_folder(name: str) -> str` (casefolded, whitespace-collapsed); `is_excluded(folder: str, patterns: list[str]) -> bool`.

Two jobs. First, parse mailbox URLs into account prefix and folder path — needed everywhere. Second, normalise folder names so a prediction matches the account's real mailbox regardless of capitalisation or spacing, and so evidence pools across accounts *if* the archive is ever enabled in `training_accounts`. It is not enabled by default.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/folder-naming
```

- [x] **Step 2: Write the failing test**

`tests/test_folders.py`:

```python
import pytest

from mail_triage.folders import account_prefix, folder_path, is_excluded, normalise_folder


def test_account_prefix_truncates_uuid():
    assert account_prefix("imap://AAAAAAAA-1111-2222/Orders") == "imap://AAAAAAAA"


def test_folder_path_decodes_and_strips():
    assert folder_path("imap://AAAAAAAA/Home%20Tech") == "Home Tech"


def test_folder_path_keeps_nesting():
    assert folder_path("imap://AAAAAAAA/Team/Meetings") == "Team/Meetings"


def test_normalise_is_case_and_space_insensitive():
    assert normalise_folder("Home  Tech") == normalise_folder("home tech")


def test_normalise_keeps_distinct_folders_distinct():
    assert normalise_folder("Home Tech") != normalise_folder("Work Tech")


@pytest.mark.parametrize(
    "folder,expected",
    [("INBOX", True), ("Deleted Messages", True), ("Sent", True), ("Orders", False)],
)
def test_exclusion_patterns(folder, expected):
    patterns = ["INBOX", "Deleted*", "Sent*"]
    assert is_excluded(folder, patterns) is expected
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_folders.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.folders'`

- [x] **Step 4: Implement `folders.py`**

```python
"""Mailbox URL parsing and folder-name normalisation.

Mail stores mailboxes as ``<scheme>://<account-uuid>/<url-encoded/path>``.
Filing history is spread across several accounts — notably a large On My Mac
archive and the live IMAP account — and the same folder name recurs in both.
Keying the model on a normalised folder name pools that evidence.
"""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import unquote, urlparse

_WHITESPACE = re.compile(r"\s+")


def account_prefix(url: str) -> str:
    """Return ``scheme://`` plus the first eight characters of the account UUID."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc[:8]}"


def folder_path(url: str) -> str:
    """Return the decoded, slash-separated folder path with no leading slash."""
    return unquote(urlparse(url).path).lstrip("/")


def normalise_folder(name: str) -> str:
    """Casefold and collapse whitespace so the same folder matches across accounts."""
    return _WHITESPACE.sub(" ", name.strip()).casefold()


def is_excluded(folder: str, patterns: list[str]) -> bool:
    """True if the folder's leaf name matches any fnmatch pattern, case-insensitively."""
    leaf = folder.rsplit("/", 1)[-1].casefold()
    whole = folder.casefold()
    return any(
        fnmatch.fnmatch(leaf, pattern.casefold()) or fnmatch.fnmatch(whole, pattern.casefold())
        for pattern in patterns
    )
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_folders.py -v`
Expected: PASS (7 tests)

- [x] **Step 6: Commit**

```bash
git add src/mail_triage/folders.py tests/test_folders.py
git commit -m "feat: mailbox URL parsing and cross-account folder normalisation"
```

---

### Task 4: Weighted training corpus

**Files:**
- Create: `src/mail_triage/corpus.py`
- Test: `tests/test_corpus.py`

**Interfaces:**
- Consumes: `MessageRow` (Task 2), `folder_path`/`normalise_folder`/`is_excluded` (Task 3), `Config` (Task 1).
- Produces: `TrainingExample` dataclass (`sender: str`, `domain: str`, `subject: str`, `folder: str`, `weight: float`); `recency_weight(date_sent: int, now: int, half_life_days: float) -> float`; `build_corpus(rows: Iterable[MessageRow], config: Config, now: int | None = None) -> list[TrainingExample]`; `sender_domain(address: str) -> str`.

This implements the spec's first mechanism for imperfect history: **recency weighting**, half-life of one year by default.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/training-corpus
```

- [x] **Step 2: Write the failing test**

`tests/test_corpus.py`:

```python
from mail_triage.config import Config
from mail_triage.corpus import build_corpus, recency_weight, sender_domain
from mail_triage.envelope import MessageRow

NOW = 1_700_000_000
DAY = 86_400


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def row(mailbox_url, date_sent=NOW, sender="a@example.com", subject="Hello"):
    return MessageRow(rowid=1, sender=sender, subject=subject,
                      date_sent=date_sent, mailbox_url=mailbox_url, read=True)


def test_sender_domain_extraction():
    assert sender_domain("Orders <orders@Shop.Example>") == "shop.example"
    assert sender_domain("plain@example.com") == "example.com"
    assert sender_domain("not-an-address") == ""


def test_recency_weight_is_one_today():
    assert recency_weight(NOW, NOW, 365.0) == 1.0


def test_recency_weight_halves_after_one_half_life():
    weight = recency_weight(NOW - 365 * DAY, NOW, 365.0)
    assert 0.49 < weight < 0.51


def test_recency_weight_decays_further_with_age():
    old = recency_weight(NOW - 730 * DAY, NOW, 365.0)
    recent = recency_weight(NOW - 30 * DAY, NOW, 365.0)
    assert old < recent < 1.0


def test_excluded_folders_are_dropped(tmp_path):
    config = make_config(tmp_path, training_exclusions=["INBOX", "Deleted*"])
    rows = [row("imap://AAAAAAAA/INBOX"), row("imap://AAAAAAAA/Orders")]
    corpus = build_corpus(rows, config, now=NOW)
    assert [example.folder for example in corpus] == ["orders"]


def test_only_training_accounts_are_learnt_from(tmp_path):
    config = make_config(tmp_path)  # training_accounts empty → iCloud only
    rows = [row("imap://AAAAAAAA/Orders"), row("local://BBBBBBBB/Archive Stuff")]
    corpus = build_corpus(rows, config, now=NOW)
    assert [example.folder for example in corpus] == ["orders"]


def test_folders_pool_when_a_second_account_is_enabled(tmp_path):
    config = make_config(
        tmp_path, training_accounts=["imap://AAAAAAAA", "local://BBBBBBBB"]
    )
    rows = [row("imap://AAAAAAAA/Orders"), row("local://BBBBBBBB/orders")]
    corpus = build_corpus(rows, config, now=NOW)
    assert {example.folder for example in corpus} == {"orders"}
    assert len(corpus) == 2


def test_undated_messages_are_dropped(tmp_path):
    config = make_config(tmp_path)
    corpus = build_corpus([row("imap://AAAAAAAA/Orders", date_sent=0)], config, now=NOW)
    assert corpus == []
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.corpus'`

- [x] **Step 4: Implement `corpus.py`**

```python
"""Turn filing history into weighted training examples.

Filing history is evidence, not ground truth. the user's habits have changed and
some mail was filed carelessly. Recency weighting is the first defence: an
example decays exponentially with age, so recent habits dominate old ones.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.folders import folder_path, is_excluded, normalise_folder

SECONDS_PER_DAY = 86_400
_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+")


@dataclass(frozen=True)
class TrainingExample:
    """One historical filing decision, weighted by how much we should trust it."""

    sender: str
    domain: str
    subject: str
    folder: str
    weight: float


def sender_domain(address: str) -> str:
    """Extract a lower-cased domain from an address or display-name form."""
    match = _ADDRESS.search(address or "")
    if not match:
        return ""
    return match.group(0).split("@", 1)[1].casefold()


def normalise_sender(address: str) -> str:
    """Extract the bare lower-cased address, discarding any display name."""
    match = _ADDRESS.search(address or "")
    return match.group(0).casefold() if match else ""


def recency_weight(date_sent: int, now: int, half_life_days: float) -> float:
    """Exponential decay: weight halves every ``half_life_days``."""
    age_days = max(0.0, (now - date_sent) / SECONDS_PER_DAY)
    return math.pow(0.5, age_days / half_life_days)


def build_corpus(
    rows: Iterable[MessageRow], config: Config, now: int | None = None
) -> list[TrainingExample]:
    """Build weighted training examples from historical messages.

    Messages outside the training accounts, in excluded folders, undated, or
    with no parseable sender contribute nothing.
    """
    if now is None:
        now = int(time.time())
    prefixes = tuple(config.training_prefixes)
    examples: list[TrainingExample] = []
    for message in rows:
        if not message.date_sent:
            continue
        if not message.mailbox_url.startswith(prefixes):
            continue
        folder = folder_path(message.mailbox_url)
        if not folder or is_excluded(folder, config.training_exclusions):
            continue
        sender = normalise_sender(message.sender)
        if not sender:
            continue
        examples.append(
            TrainingExample(
                sender=sender,
                domain=sender_domain(message.sender),
                subject=message.subject,
                folder=normalise_folder(folder),
                weight=recency_weight(message.date_sent, now, config.half_life_days),
            )
        )
    return examples
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (7 tests)

- [x] **Step 6: Commit**

```bash
git add src/mail_triage/corpus.py tests/test_corpus.py
git commit -m "feat: recency-weighted training corpus"
```

---

### Task 5: Stage A — sender and domain model with consistency gating

**Files:**
- Create: `src/mail_triage/model/__init__.py`, `src/mail_triage/model/sender.py`
- Test: `tests/test_sender_model.py`

**Interfaces:**
- Consumes: `TrainingExample` (Task 4).
- Produces: `SenderModel` with `.train(examples: list[TrainingExample]) -> None`, `.predict(sender: str, domain: str) -> Prediction | None`, `.to_dict() -> dict`, `.from_dict(data: dict) -> SenderModel` (classmethod), `.drift_report() -> list[DriftEntry]`; `Prediction` dataclass (`folder: str`, `confidence: float`, `reason: str`); `DriftEntry` dataclass (`key: str`, `old_folder: str`, `new_folder: str`, `switch_year: int`).

This implements the spec's **consistency gating** and **drift reporting**. Confidence is the weighted share of the winning folder, so a sender split 55/45 yields 0.55 and falls below the default 0.7 threshold — no proposal, exactly as specified.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/sender-model
```

- [x] **Step 2: Write the failing test**

`tests/test_sender_model.py`:

```python
from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import SenderModel


def example(sender, folder, weight=1.0, subject="Subject"):
    domain = sender.split("@", 1)[1]
    return TrainingExample(sender=sender, domain=domain, subject=subject,
                           folder=folder, weight=weight)


def test_consistent_sender_predicts_confidently():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders") for _ in range(10)])
    prediction = model.predict("orders@shop.example", "shop.example")
    assert prediction.folder == "orders"
    assert prediction.confidence == 1.0
    assert "orders@shop.example" in prediction.reason


def test_split_sender_yields_low_confidence():
    model = SenderModel()
    model.train(
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    prediction = model.predict("mixed@shop.example", "shop.example")
    assert prediction.folder == "orders"
    assert 0.5 < prediction.confidence < 0.6


def test_falls_back_to_domain_for_unknown_sender():
    model = SenderModel()
    model.train([example(f"user{i}@shop.example", "orders") for i in range(5)])
    prediction = model.predict("brand-new@shop.example", "shop.example")
    assert prediction.folder == "orders"
    assert "domain" in prediction.reason


def test_unknown_sender_and_domain_returns_none():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders")])
    assert model.predict("nobody@elsewhere.example", "elsewhere.example") is None


def test_recent_evidence_outweighs_old():
    model = SenderModel()
    model.train(
        [example("drift@shop.example", "home tech", weight=0.05) for _ in range(20)]
        + [example("drift@shop.example", "security & tech", weight=1.0) for _ in range(3)]
    )
    prediction = model.predict("drift@shop.example", "shop.example")
    assert prediction.folder == "security & tech"


def test_round_trips_through_dict():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders")])
    restored = SenderModel.from_dict(model.to_dict())
    assert restored.predict("orders@shop.example", "shop.example").folder == "orders"


def test_single_observation_is_not_fully_confident():
    model = SenderModel()
    model.train([example("once@shop.example", "orders")])
    prediction = model.predict("once@shop.example", "shop.example")
    assert prediction.confidence < 1.0
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sender_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.model'`

- [x] **Step 4: Implement `model/sender.py`**

Create an empty `src/mail_triage/model/__init__.py`, then:

```python
"""Stage A: file mail by who sent it.

Sender address first, then sender domain. Most mail is decided here — a
newsletter or a shop always goes to the same place. Confidence is the weighted
share of the winning folder, so a sender whose mail is scattered across folders
produces a low score and, above in the pipeline, no proposal at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from mail_triage.corpus import TrainingExample

# A single observation should not read as certainty. This pseudo-count damps
# confidence for thinly-evidenced senders: one sighting gives 1/(1+1) = 0.5.
PRIOR_STRENGTH = 1.0


@dataclass(frozen=True)
class Prediction:
    """A stage's answer, with a human-readable justification."""

    folder: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class DriftEntry:
    """A key whose destination folder changed over time."""

    key: str
    old_folder: str
    new_folder: str
    switch_year: int


class SenderModel:
    """Weighted folder counts per sender address and per sender domain."""

    def __init__(self) -> None:
        self.by_sender: dict[str, dict[str, float]] = {}
        self.by_domain: dict[str, dict[str, float]] = {}

    def train(self, examples: list[TrainingExample]) -> None:
        senders: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        domains: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for item in examples:
            if item.sender:
                senders[item.sender][item.folder] += item.weight
            if item.domain:
                domains[item.domain][item.folder] += item.weight
        self.by_sender = {key: dict(value) for key, value in senders.items()}
        self.by_domain = {key: dict(value) for key, value in domains.items()}

    @staticmethod
    def _best(counts: dict[str, float]) -> tuple[str, float, float]:
        """Return (folder, share, total) for the highest-weighted folder."""
        total = sum(counts.values())
        folder, weight = max(counts.items(), key=lambda item: item[1])
        share = weight / (total + PRIOR_STRENGTH) if total else 0.0
        return folder, share, total

    def predict(self, sender: str, domain: str) -> Prediction | None:
        counts = self.by_sender.get(sender)
        if counts:
            folder, share, total = self._best(counts)
            return Prediction(
                folder=folder,
                confidence=share,
                reason=f"sender {sender} filed to '{folder}' ({total:.1f} weighted sightings)",
            )
        counts = self.by_domain.get(domain)
        if counts:
            folder, share, total = self._best(counts)
            return Prediction(
                folder=folder,
                confidence=share,
                reason=f"sender domain {domain} filed to '{folder}' ({total:.1f} weighted sightings)",
            )
        return None

    def to_dict(self) -> dict:
        return {"by_sender": self.by_sender, "by_domain": self.by_domain}

    @classmethod
    def from_dict(cls, data: dict) -> SenderModel:
        model = cls()
        model.by_sender = data.get("by_sender", {})
        model.by_domain = data.get("by_domain", {})
        return model
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_sender_model.py -v`
Expected: PASS (7 tests)

- [x] **Step 6: Add drift reporting**

Drift needs dates, which `TrainingExample` does not carry. Add `year: int` to `TrainingExample` in `corpus.py` (set from `time.gmtime(message.date_sent).tm_year`), update `build_corpus` accordingly, then add to `SenderModel`:

```python
    def train_drift(self, examples: list[TrainingExample]) -> None:
        """Record, per sender, the dominant folder in each year."""
        per_year: dict[str, dict[int, dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float))
        )
        for item in examples:
            if item.sender:
                per_year[item.sender][item.year][item.folder] += 1.0
        self._per_year = {
            sender: {year: dict(folders) for year, folders in years.items()}
            for sender, years in per_year.items()
        }

    def drift_report(self) -> list[DriftEntry]:
        """Senders whose dominant folder changed between their first and last year."""
        entries: list[DriftEntry] = []
        for sender, years in getattr(self, "_per_year", {}).items():
            if len(years) < 2:
                continue
            ordered = sorted(years.items())
            first_year, first_folders = ordered[0]
            last_year, last_folders = ordered[-1]
            old = max(first_folders.items(), key=lambda item: item[1])[0]
            new = max(last_folders.items(), key=lambda item: item[1])[0]
            if old != new:
                entries.append(DriftEntry(key=sender, old_folder=old, new_folder=new,
                                          switch_year=last_year))
        return sorted(entries, key=lambda entry: entry.key)
```

Add a test asserting a sender filed to `home tech` in 2023 and `security & tech` in 2026 appears in the report with those folders, and that a stable sender does not.

- [x] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/model tests/test_sender_model.py src/mail_triage/corpus.py tests/test_corpus.py
git commit -m "feat: sender and domain model with consistency gating and drift reporting"
```

---

### Task 6: Model persistence and the `learn` command

**Files:**
- Create: `src/mail_triage/model/store.py`
- Modify: `src/mail_triage/cli.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `SenderModel` (Task 5), `Config` (Task 1), `EnvelopeReader` (Task 2), `build_corpus` (Task 4).
- Produces: `TrainedModel` dataclass (`sender: SenderModel`, `trained_at: int`, `example_count: int`); `save_model(model: TrainedModel, path: Path) -> None`; `load_model(path: Path) -> TrainedModel`; `train_from_history(config: Config, db_path: Path) -> TrainedModel`.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/learn-command
```

- [x] **Step 2: Write the failing test**

`tests/test_store.py`:

```python
import pytest

from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import TrainedModel, load_model, save_model


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
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — no module `mail_triage.model.store`

- [x] **Step 4: Implement `model/store.py`**

```python
"""Persist the trained model to the gitignored local area."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mail_triage.config import Config
from mail_triage.corpus import build_corpus
from mail_triage.envelope import EnvelopeReader, snapshot_database
from mail_triage.model.sender import SenderModel

MODEL_VERSION = 1


@dataclass
class TrainedModel:
    sender: SenderModel
    trained_at: int
    example_count: int


def save_model(model: TrainedModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MODEL_VERSION,
        "trained_at": model.trained_at,
        "example_count": model.example_count,
        "sender": model.sender.to_dict(),
    }
    path.write_text(json.dumps(payload))


def load_model(path: Path) -> TrainedModel:
    if not path.exists():
        raise FileNotFoundError(f"No model at {path}. Run 'mail-triage learn' first.")
    payload = json.loads(path.read_text())
    if payload.get("version") != MODEL_VERSION:
        raise ValueError(
            f"Model at {path} is version {payload.get('version')}, expected {MODEL_VERSION}. "
            "Run 'mail-triage learn' to rebuild it."
        )
    return TrainedModel(
        sender=SenderModel.from_dict(payload["sender"]),
        trained_at=payload["trained_at"],
        example_count=payload["example_count"],
    )


def train_from_history(config: Config, db_path: Path) -> TrainedModel:
    """Snapshot the database, build the corpus, and train."""
    with tempfile.TemporaryDirectory() as work:
        snapshot = snapshot_database(db_path, Path(work))
        reader = EnvelopeReader(snapshot)
        try:
            examples = build_corpus(reader.all_messages(), config)
        finally:
            reader.close()
    sender_model = SenderModel()
    sender_model.train(examples)
    sender_model.train_drift(examples)
    return TrainedModel(
        sender=sender_model, trained_at=int(time.time()), example_count=len(examples)
    )
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS (2 tests)

- [x] **Step 6: Add the `learn` command**

```python
@cli.command()
@click.option("--drift/--no-drift", default=True, help="Show senders whose destination changed.")
def learn(drift: bool) -> None:
    """Build the classifier from your filing history."""
    config = load_config()
    model = train_from_history(config, DEFAULT_DB_PATH)
    save_model(model, config.model_path)
    click.echo(f"Trained on {model.example_count:,} filed messages.")
    click.echo(f"Known senders: {len(model.sender.by_sender):,}")
    click.echo(f"Known domains: {len(model.sender.by_domain):,}")
    click.echo(f"Model written to {config.model_path}")
    if drift:
        entries = model.sender.drift_report()
        if entries:
            click.echo(f"\n{len(entries)} senders changed destination over time:")
            for entry in entries[:20]:
                click.echo(f"  {entry.key}: '{entry.old_folder}' → '{entry.new_folder}' (by {entry.switch_year})")
            if len(entries) > 20:
                click.echo(f"  ... and {len(entries) - 20} more")
```

- [x] **Step 7: Run it against real history**

Run: `uv run mail-triage learn`
Expected: trains on tens of thousands of examples, writes `local/model.json`, prints a drift report. **Read-only — no mail is touched.**

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/model/store.py src/mail_triage/cli.py tests/test_store.py
git commit -m "feat: model persistence and learn command"
```

---

### Task 7: AppleScript bridge with a fake for tests

**Files:**
- Create: `src/mail_triage/mail_app.py`
- Test: `tests/test_mail_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MailInterface` protocol with `inbox_message_ids(account: str) -> list[int]`, `mailbox_names(account: str) -> list[str]`, `move_message(message_id: int, folder: str, account: str) -> None`, `message_headers(message_id: int) -> dict[str, str]`; `AppleScriptMail` implementing it; `FakeMail` for tests; `MailNotRunningError`, `MailboxNotFoundError`, `MessageNotFoundError`.

**Nothing in this task moves real mail.** The first live move is Task 10, which is a CHECKPOINT.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/applescript-bridge
```

- [x] **Step 2: Write the failing test**

`tests/test_mail_app.py`:

```python
import pytest

from mail_triage.mail_app import FakeMail, MailboxNotFoundError, MessageNotFoundError


def test_fake_lists_inbox():
    mail = FakeMail(inbox=[1, 2, 3], mailboxes=["Orders"])
    assert mail.inbox_message_ids("iCloud") == [1, 2, 3]


def test_fake_move_records_the_move():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    mail.move_message(1, "Orders", "iCloud")
    assert mail.moved == [(1, "Orders", "iCloud")]
    assert mail.inbox_message_ids("iCloud") == []


def test_fake_move_to_unknown_mailbox_raises():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    with pytest.raises(MailboxNotFoundError, match="Nonexistent"):
        mail.move_message(1, "Nonexistent", "iCloud")


def test_fake_move_of_unknown_message_raises():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    with pytest.raises(MessageNotFoundError):
        mail.move_message(99, "Orders", "iCloud")


def test_fake_returns_configured_headers():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"],
                    headers={1: {"List-Unsubscribe": "<mailto:leave@list.example>"}})
    assert mail.message_headers(1)["List-Unsubscribe"] == "<mailto:leave@list.example>"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mail_app.py -v`
Expected: FAIL — no module `mail_triage.mail_app`

- [x] **Step 4: Implement `mail_app.py`**

```python
"""The only component that changes anything in Mail.

Everything else in mail-triage is read-only. Mutations go through AppleScript
because it is the sole supported write path — writing to Mail's SQLite database
directly corrupts it.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


class MailError(RuntimeError):
    """Base class for Mail interaction failures."""


class MailNotRunningError(MailError):
    """Mail is not running. We never launch it on the user's behalf."""


class MailboxNotFoundError(MailError):
    """The target mailbox does not exist in the account."""


class MessageNotFoundError(MailError):
    """The message is no longer where we expected it."""


class MailInterface(Protocol):
    def inbox_message_ids(self, account: str) -> list[int]: ...
    def mailbox_names(self, account: str) -> list[str]: ...
    def move_message(self, message_id: int, folder: str, account: str) -> None: ...
    def message_headers(self, message_id: int) -> dict[str, str]: ...


def _run(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        error = result.stderr.strip()
        if "not running" in error or "-600" in error:
            raise MailNotRunningError("Mail is not running. Please open it and try again.")
        raise MailError(error)
    return result.stdout.strip()


class AppleScriptMail:
    """Drives Mail.app via osascript."""

    def inbox_message_ids(self, account: str) -> list[int]:
        script = f'tell application "Mail" to get id of messages of mailbox "INBOX" of account "{account}"'
        output = _run(script)
        return [int(part) for part in output.split(", ") if part.strip()]

    def mailbox_names(self, account: str) -> list[str]:
        """Leaf names of the account's mailboxes.

        Note: Mail returns a flat list of LEAF names, so this cannot be used to
        match nested folders — 41 of the 44 folders in the trained model are
        nested. The authoritative folder list comes from the envelope database
        via ``folder_path``, which preserves full paths and capitalisation. This
        method is kept only for checking that an account is reachable.
        """
        script = f'tell application "Mail" to get name of mailboxes of account "{account}"'
        output = _run(script)
        return [part.strip() for part in output.split(", ") if part.strip()]

    def move_message(self, message_id: int, folder: str, account: str) -> None:
        # ``folder`` is a full path such as "Parent/Orders". Path addressing is
        # required: the user's folders are nested, and a leaf-name lookup is both
        # unable to reach them and ambiguous when a leaf name repeats.
        script = (
            'tell application "Mail"\n'
            f'  set theBox to mailbox "{folder}" of account "{account}"\n'
            f'  set theMessage to (first message of mailbox "INBOX" of account "{account}" '
            f'whose id is {message_id})\n'
            "  move theMessage to theBox\n"
            "end tell"
        )
        try:
            _run(script)
        except MailError as error:
            text = str(error)
            if "mailbox" in text.lower():
                raise MailboxNotFoundError(f"No mailbox '{folder}' in account '{account}'") from error
            raise MessageNotFoundError(f"Message {message_id} not found in INBOX") from error

    def message_headers(self, message_id: int) -> dict[str, str]:
        """Fetch raw headers. Mail's database does not store these."""
        script = (
            'tell application "Mail"\n'
            f"  set theMessage to (first message of inbox whose id is {message_id})\n"
            "  return all headers of theMessage\n"
            "end tell"
        )
        return _parse_headers(_run(script))


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse RFC-822 headers, joining folded continuation lines."""
    headers: dict[str, str] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and current:
            headers[current] += " " + line.strip()
        elif ":" in line:
            name, _, value = line.partition(":")
            current = name.strip()
            headers[current] = value.strip()
    return headers


class FakeMail:
    """In-memory stand-in so the suite never touches real mail."""

    def __init__(self, inbox: list[int], mailboxes: list[str],
                 headers: dict[int, dict[str, str]] | None = None) -> None:
        self._inbox = list(inbox)
        self._mailboxes = list(mailboxes)
        self._headers = headers or {}
        self.moved: list[tuple[int, str, str]] = []
        self.sent: list[tuple[str, str]] = []

    def inbox_message_ids(self, account: str) -> list[int]:
        return list(self._inbox)

    def mailbox_names(self, account: str) -> list[str]:
        return list(self._mailboxes)

    def move_message(self, message_id: int, folder: str, account: str) -> None:
        if folder not in self._mailboxes:
            raise MailboxNotFoundError(f"No mailbox '{folder}'")
        if message_id not in self._inbox:
            raise MessageNotFoundError(f"Message {message_id} not in inbox")
        self._inbox.remove(message_id)
        self.moved.append((message_id, folder, account))

    def message_headers(self, message_id: int) -> dict[str, str]:
        return dict(self._headers.get(message_id, {}))
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mail_app.py -v`
Expected: PASS (5 tests)

- [x] **Step 6: Add a test for header parsing**

```python
from mail_triage.mail_app import _parse_headers


def test_parses_folded_headers():
    raw = "Subject: A subject\nList-Unsubscribe: <mailto:a@b.example>,\n <https://c.example/u>\n"
    headers = _parse_headers(raw)
    assert headers["Subject"] == "A subject"
    assert headers["List-Unsubscribe"] == "<mailto:a@b.example>, <https://c.example/u>"
```

Run: `uv run pytest tests/test_mail_app.py -v` — expected PASS.

- [x] **Step 7: Verify read-only AppleScript against the live app**

Run:
```bash
uv run python -c "
from mail_triage.mail_app import AppleScriptMail
mail = AppleScriptMail()
names = mail.mailbox_names('iCloud')
print(f'{len(names)} mailboxes visible')
print(f'{len(mail.inbox_message_ids(\"iCloud\"))} messages in inbox')
"
```
Expected: counts printed, nothing modified. If the account name `iCloud` is wrong, list account names with `osascript -e 'tell application "Mail" to get name of every account'` and record the correct one for config.

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/mail_app.py tests/test_mail_app.py
git commit -m "feat: AppleScript bridge with in-memory fake"
```

---

### Task 8: Run journal and undo

**Files:**
- Create: `src/mail_triage/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `MailInterface` (Task 7).
- Produces: `JournalEntry` dataclass (`message_id: int`, `subject: str`, `from_folder: str`, `to_folder: str`, `status: str`); `Journal` with `.begin(run_id: str) -> None`, `.record(entry: JournalEntry) -> None`, `.mark(message_id: int, status: str) -> None`, `.entries() -> list[JournalEntry]`; `new_run_id() -> str`; `undo_run(run_id: str, config: Config, mail: MailInterface, account: str) -> tuple[int, int]` returning `(reversed_count, failed_count)`; `list_runs(config: Config) -> list[str]`.

Entries are written **before** the move is attempted, so a batch that dies half-way is still fully undoable.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/journal-and-undo
```

- [x] **Step 2: Write the failing test**

`tests/test_journal.py`:

```python
from mail_triage.config import Config
from mail_triage.journal import Journal, JournalEntry, list_runs, new_run_id, undo_run
from mail_triage.mail_app import FakeMail


def make_config(tmp_path):
    return Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)


def test_entries_round_trip(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(message_id=1, subject="Order", from_folder="INBOX",
                                to_folder="Orders", status="planned"))
    assert [entry.message_id for entry in Journal(config).load("run-1")] == [1]


def test_mark_updates_status(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    journal.mark(1, "moved")
    assert Journal(config).load("run-1")[0].status == "moved"


def test_undo_reverses_only_completed_moves(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    journal.mark(1, "moved")
    journal.record(JournalEntry(2, "Digest", "INBOX", "Newsletters", "planned"))
    mail = FakeMail(inbox=[], mailboxes=["Orders", "Newsletters", "INBOX"])
    mail._inbox = [1, 2]  # both currently reachable
    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")
    assert reversed_count == 1
    assert failed == 0
    assert mail.moved == [(1, "INBOX", "iCloud")]


def test_run_ids_are_unique_and_sortable():
    first, second = new_run_id(), new_run_id()
    assert first <= second


def test_list_runs_returns_newest_first(tmp_path):
    config = make_config(tmp_path)
    for run_id in ("2026-01-01T00-00-00", "2026-06-01T00-00-00"):
        journal = Journal(config)
        journal.begin(run_id)
        journal.record(JournalEntry(1, "s", "INBOX", "Orders", "planned"))
    assert list_runs(config)[0] == "2026-06-01T00-00-00"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL — no module `mail_triage.journal`

- [x] **Step 4: Implement `journal.py`**

```python
"""Record every intended move before it happens, so any run can be reversed."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mail_triage.config import Config
from mail_triage.mail_app import MailError, MailInterface


@dataclass
class JournalEntry:
    message_id: int
    subject: str
    from_folder: str
    to_folder: str
    status: str  # planned | moved | failed | undone


def new_run_id() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())


class Journal:
    """Append-only JSONL log, one file per run."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.run_id: str | None = None

    def _path(self, run_id: str) -> Path:
        return self.config.journal_dir / f"{run_id}.jsonl"

    def begin(self, run_id: str) -> None:
        self.run_id = run_id
        self.config.journal_dir.mkdir(parents=True, exist_ok=True)
        self._path(run_id).touch()

    def record(self, entry: JournalEntry) -> None:
        if self.run_id is None:
            raise RuntimeError("begin() must be called before record()")
        with self._path(self.run_id).open("a") as handle:
            handle.write(json.dumps(asdict(entry)) + "\n")

    def mark(self, message_id: int, status: str) -> None:
        """Append a status update. The last entry for an id wins on load."""
        entries = {entry.message_id: entry for entry in self.load(self.run_id)}
        entry = entries[message_id]
        entry.status = status
        self.record(entry)

    def load(self, run_id: str) -> list[JournalEntry]:
        path = self._path(run_id)
        if not path.exists():
            return []
        latest: dict[int, JournalEntry] = {}
        for line in path.read_text().splitlines():
            if line.strip():
                entry = JournalEntry(**json.loads(line))
                latest[entry.message_id] = entry
        return list(latest.values())

    def entries(self) -> list[JournalEntry]:
        return self.load(self.run_id) if self.run_id else []


def list_runs(config: Config) -> list[str]:
    if not config.journal_dir.exists():
        return []
    return sorted((path.stem for path in config.journal_dir.glob("*.jsonl")), reverse=True)


def undo_run(
    run_id: str, config: Config, mail: MailInterface, account: str
) -> tuple[int, int]:
    """Move every completed message back where it came from."""
    journal = Journal(config)
    journal.run_id = run_id
    reversed_count = 0
    failed = 0
    for entry in journal.load(run_id):
        if entry.status != "moved":
            continue
        try:
            mail.move_message(entry.message_id, entry.from_folder, account)
        except MailError:
            failed += 1
            continue
        entry.status = "undone"
        journal.record(entry)
        reversed_count += 1
    return reversed_count, failed
```

Note: `undo_run` moves *back to* `from_folder`, which requires `FakeMail.move_message` to find the message. The fake removes moved ids from its inbox, so the test seeds `_inbox` directly. For the real implementation, undo looks the message up in the destination folder — adjust `AppleScriptMail.move_message` to take an optional `source_folder: str = "INBOX"` parameter and use it in the lookup, then update the protocol, the fake, and the callers to match.

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_journal.py -v`
Expected: PASS (5 tests)

- [x] **Step 6: Commit**

```bash
git add src/mail_triage/journal.py tests/test_journal.py
git commit -m "feat: run journal with undo"
```

---

### Task 9: Classification orchestration

**Files:**
- Create: `src/mail_triage/model/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `SenderModel`/`Prediction` (Task 5), `TrainedModel` (Task 6), `MessageRow` (Task 2), `Config` (Task 1).
- Produces: `Proposal` dataclass (`message: MessageRow`, `folder: str | None`, `confidence: float`, `reason: str`, `stage: str`); `Classifier(model: TrainedModel, config: Config, available_folders: list[str])` with `.classify(message: MessageRow) -> Proposal`.

`available_folders` enforces the spec's "existing folders only" rule: a prediction naming a folder that does not exist in the target account is discarded and the message stays in the inbox.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/classification
```

- [x] **Step 2: Write the failing test**

`tests/test_classify.py`:

```python
from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Classifier
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import TrainedModel


def make_model(examples):
    sender_model = SenderModel()
    sender_model.train(examples)
    return TrainedModel(sender=sender_model, trained_at=0, example_count=len(examples))


def example(sender, folder, weight=1.0):
    return TrainingExample(sender=sender, domain=sender.split("@")[1], subject="s",
                           folder=folder, weight=weight, year=2026)


def message(sender="orders@shop.example", subject="Your order"):
    return MessageRow(rowid=1, sender=sender, subject=subject, date_sent=1_700_000_000,
                      mailbox_url="imap://AAAAAAAA/INBOX", read=False)


def config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def test_confident_sender_produces_a_proposal(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.stage == "sender"


def test_low_confidence_produces_no_folder(tmp_path):
    model = make_model(
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders", "Finance"])
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder is None
    assert "below threshold" in proposal.reason


def test_folder_absent_from_account_is_rejected(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Finance"])
    proposal = classifier.classify(message())
    assert proposal.folder is None
    assert "does not exist" in proposal.reason


def test_unknown_sender_produces_no_folder(tmp_path):
    model = make_model([example("orders@shop.example", "orders")])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message(sender="stranger@nowhere.example"))
    assert proposal.folder is None
    assert proposal.stage == "none"


def test_proposal_preserves_original_folder_capitalisation(tmp_path):
    model = make_model([example("orders@shop.example", "home tech") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Home Tech"])
    assert classifier.classify(message()).folder == "Home Tech"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — no module `mail_triage.model.classify`

- [x] **Step 4: Implement `model/classify.py`**

```python
"""Run the classification stages in order and produce an explainable proposal.

Stage order is deliberate: the cheapest and most explainable stage goes first.
A message that no stage can place confidently stays in the inbox — that is the
safety net for important mail, and it is not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from mail_triage.config import Config
from mail_triage.corpus import normalise_sender, sender_domain
from mail_triage.envelope import MessageRow
from mail_triage.folders import normalise_folder
from mail_triage.model.store import TrainedModel


@dataclass(frozen=True)
class Proposal:
    """What we think should happen to one message, and why."""

    message: MessageRow
    folder: str | None
    confidence: float
    reason: str
    stage: str  # sender | tokens | llm | none


class Classifier:
    def __init__(
        self, model: TrainedModel, config: Config, available_folders: list[str]
    ) -> None:
        self.model = model
        self.config = config
        # Map normalised name back to the account's real capitalisation.
        self.folders = {normalise_folder(name): name for name in available_folders}

    def classify(self, message: MessageRow) -> Proposal:
        sender = normalise_sender(message.sender)
        domain = sender_domain(message.sender)
        prediction = self.model.sender.predict(sender, domain)
        if prediction is None:
            return Proposal(message, None, 0.0, "no history for this sender or domain", "none")

        actual_folder = self.folders.get(prediction.folder)
        if actual_folder is None:
            return Proposal(
                message, None, prediction.confidence,
                f"'{prediction.folder}' does not exist in this account", "sender",
            )
        if prediction.confidence < self.config.confidence_threshold:
            return Proposal(
                message, None, prediction.confidence,
                f"{prediction.reason} — below threshold "
                f"({prediction.confidence:.2f} < {self.config.confidence_threshold:.2f})",
                "sender",
            )
        return Proposal(message, actual_folder, prediction.confidence, prediction.reason, "sender")
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: PASS (5 tests)

- [x] **Step 6: Commit**

```bash
git add src/mail_triage/model/classify.py tests/test_classify.py
git commit -m "feat: classification orchestration producing explainable proposals"
```

---

### Task 10: `triage --dry-run` and the review interface

**Files:**
- Create: `src/mail_triage/review.py`
- Modify: `src/mail_triage/cli.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `Proposal` (Task 9), `MailInterface` (Task 7), `Journal` (Task 8).
- Produces: `render_table(proposals: list[Proposal]) -> str`; `Decision` dataclass (`proposal: Proposal`, `accepted: bool`, `override_folder: str | None`); `review(proposals: list[Proposal], prompt: Callable[[str], str]) -> list[Decision]`; `summarise(proposals: list[Proposal]) -> str`.

`prompt` is injected so the review loop is testable without a terminal.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/dry-run-and-review
```

- [x] **Step 2: Write the failing test**

`tests/test_review.py`:

```python
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.review import render_table, review, summarise


def proposal(folder="Orders", subject="Your order", confidence=0.95, rowid=1):
    message = MessageRow(rowid=rowid, sender="orders@shop.example", subject=subject,
                         date_sent=1_700_000_000, mailbox_url="imap://A/INBOX", read=False)
    return Proposal(message, folder, confidence, "sender seen often", "sender")


def test_table_shows_subject_and_destination():
    table = render_table([proposal()])
    assert "Your order" in table
    assert "Orders" in table


def test_table_truncates_long_subjects():
    table = render_table([proposal(subject="x" * 200)])
    assert max(len(line) for line in table.splitlines()) < 160


def test_summary_counts_placed_and_unplaced():
    text = summarise([proposal(), proposal(folder=None, rowid=2)])
    assert "1" in text and "inbox" in text.lower()


def test_review_accepts_all_on_a():
    decisions = review([proposal(), proposal(rowid=2)], prompt=lambda _: "a")
    assert all(decision.accepted for decision in decisions)


def test_review_rejects_all_on_q():
    decisions = review([proposal()], prompt=lambda _: "q")
    assert decisions == []


def test_review_per_message_yes_and_no():
    answers = iter(["s", "y", "n"])  # step through, accept first, reject second
    decisions = review([proposal(), proposal(rowid=2)], prompt=lambda _: next(answers))
    assert [decision.accepted for decision in decisions] == [True, False]


def test_unplaced_proposals_are_never_offered():
    decisions = review([proposal(folder=None)], prompt=lambda _: "a")
    assert decisions == []
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_review.py -v`
Expected: FAIL — no module `mail_triage.review`

- [x] **Step 4: Implement `review.py`**

```python
"""Present proposals and collect decisions.

The prompt function is injected so the whole loop is testable without a
terminal, and so a future non-interactive mode reuses the same code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mail_triage.model.classify import Proposal

SUBJECT_WIDTH = 48
FOLDER_WIDTH = 22
SENDER_WIDTH = 32


@dataclass(frozen=True)
class Decision:
    proposal: Proposal
    accepted: bool
    override_folder: str | None = None

    @property
    def folder(self) -> str | None:
        return self.override_folder or self.proposal.folder


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(proposals: list[Proposal]) -> str:
    """Render placed proposals as an aligned table."""
    lines = [
        f"{'Sender':<{SENDER_WIDTH}} {'Subject':<{SUBJECT_WIDTH}} {'→ Folder':<{FOLDER_WIDTH}} Conf"
    ]
    for item in proposals:
        if item.folder is None:
            continue
        lines.append(
            f"{_clip(item.message.sender, SENDER_WIDTH):<{SENDER_WIDTH}} "
            f"{_clip(item.message.subject, SUBJECT_WIDTH):<{SUBJECT_WIDTH}} "
            f"{_clip(item.folder, FOLDER_WIDTH):<{FOLDER_WIDTH}} {item.confidence:.2f}"
        )
    return "\n".join(lines)


def summarise(proposals: list[Proposal]) -> str:
    placed = sum(1 for item in proposals if item.folder)
    unplaced = len(proposals) - placed
    return f"{placed} to file, {unplaced} staying in the inbox."


def review(proposals: list[Proposal], prompt: Callable[[str], str]) -> list[Decision]:
    """Ask what to do. Returns only the decisions the user made.

    Answers: a = accept all, q = quit without acting, s = step through one by one.
    In step mode: y = accept, n = reject.
    """
    placed = [item for item in proposals if item.folder is not None]
    if not placed:
        return []
    answer = prompt("[a]ccept all, [s]tep through, [q]uit? ").strip().casefold()
    if answer == "a":
        return [Decision(item, accepted=True) for item in placed]
    if answer != "s":
        return []
    decisions: list[Decision] = []
    for item in placed:
        reply = prompt(
            f"{_clip(item.message.subject, SUBJECT_WIDTH)} → {item.folder}? [y/n] "
        ).strip().casefold()
        decisions.append(Decision(item, accepted=reply == "y"))
    return decisions
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_review.py -v`
Expected: PASS (7 tests)

- [x] **Step 6: Add the `triage` command with `--dry-run` only**

In `cli.py`. Note `--dry-run` is the safe default for this step; live moving arrives in Task 11.

```python
@cli.command()
@click.option("--dry-run", is_flag=True, default=True, help="Report only; move nothing.")
@click.option("--account", default="iCloud", help="Mail account name as shown in Mail.")
def triage(dry_run: bool, account: str) -> None:
    """Classify the inbox and report what would be filed."""
    config = load_config()
    model = load_model(config.model_path)
    # The folder list comes from the database, not AppleScript: it preserves
    # full nested paths ("Parent/Orders") and real capitalisation, both of which
    # AppleScript's flat leaf-name list loses.
    with tempfile.TemporaryDirectory() as work:
        snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
        reader = EnvelopeReader(snapshot)
        inbox_url = next(
            (url for url in reader.mailbox_urls()
             if url.startswith(config.account_url_prefix)
             and folder_path(url).casefold() == config.inbox_folder.casefold()),
            None,
        )
        if inbox_url is None:
            raise click.ClickException(
                f"No mailbox '{config.inbox_folder}' under {config.account_url_prefix}. "
                "Run 'mail-triage accounts' to check your account prefix."
            )
        messages = list(reader.messages_in_mailbox(inbox_url))
        folders = [
            folder_path(url)
            for url in reader.mailbox_urls()
            if url.startswith(config.account_url_prefix) and folder_path(url)
        ]
        reader.close()
    classifier = Classifier(model, config, folders)
    proposals = [classifier.classify(message) for message in messages]
    click.echo(render_table(proposals))
    click.echo()
    click.echo(summarise(proposals))
```

- [x] **Step 7: Run against the real inbox**

Run: `uv run mail-triage triage --dry-run`
Expected: a table of proposals. **Nothing is moved.** This is the first real read of what the classifier would do — inspect the output carefully and sanity-check a dozen rows before proceeding.

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/review.py src/mail_triage/cli.py tests/test_review.py
git commit -m "feat: dry-run triage with proposal table and review loop"
```

---

### Task 11A: Durable message identity for the journal

**Files:**
- Modify: `src/mail_triage/mail_app.py`, `src/mail_triage/journal.py`, `src/mail_triage/envelope.py`
- Test: `tests/test_mail_app.py`, `tests/test_journal.py`

**Interfaces:**
- Consumes: `Journal`, `JournalEntry` (Task 8); `MailInterface` (Task 7).
- Produces: `JournalEntry` gains `message_key: str` (the RFC-822 Message-ID); `MailInterface.message_key(message_id: int) -> str`; `move_message` gains `message_key: str | None = None` and looks up by key when given one; `EnvelopeReader` unchanged.

**Why this task exists.** Verified live on 26 July 2026: a message's numeric AppleScript id **changes when it moves** (`447494` → `447714` → `447715`) and is not restored on the way back. `undo_run` looks messages up by the recorded numeric id, so it would find nothing, reverse nothing, and report success. The journal is the safety net that justifies moving unread mail; it does not currently work.

The durable key is `message id of message` — the RFC-822 `Message-ID`. Verified across a move: the numeric id changed whilst `message id` stayed byte-identical, and `messages of mailbox "X" of account "Y" whose message id is "…"` matched exactly one message.

- [x] **Step 1: Write the failing test**

In `tests/test_journal.py`, assert that a journal entry round-trips a `message_key`, and that `undo_run` calls `move_message` with the key rather than the numeric id:

```python
def test_undo_reverses_by_message_key_not_numeric_id(tmp_path):
    config = Config(account_url_prefix="imap://A", local_dir=tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(message_id=1, message_key="<abc@example.com>",
                                subject="s", from_folder="INBOX",
                                to_folder="Parent/Child", status="moved"))
    mail = FakeMail(inbox=[], mailboxes=["INBOX", "Parent/Child"],
                    folders={"Parent/Child": [99]},
                    keys={99: "<abc@example.com>"})
    reversed_count, failed = undo_run("run-1", config, mail, account="Test")
    assert reversed_count == 1
    # The message now has numeric id 99, not the 1 recorded at move time.
    assert mail.moved == [(99, "INBOX", "Test", "Parent/Child")]
```

`FakeMail` gains a `keys` mapping and resolves a `message_key` to whatever numeric id currently holds it, mirroring the real lookup.

- [x] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL — `JournalEntry` has no `message_key`.

- [x] **Step 3: Add `message_key` to the AppleScript bridge**

```python
    def message_key(self, message_id: int, source_folder: str = "INBOX",
                    account: str = "") -> str:
        """Return the RFC-822 Message-ID, which survives moves.

        The numeric AppleScript id does not: moving a message changes it, and
        moving it back does not restore the old value.
        """
        script = (
            'tell application "Mail"\n'
            f'  set theMessage to (first message of mailbox "{_escape(source_folder)}" '
            f'of account "{_escape(account)}" whose id is {message_id})\n'
            "  return message id of theMessage\n"
            "end tell"
        )
        return _run(script)
```

And `move_message` looks up by key when one is supplied:
`first message of mailbox "…" whose message id is "…"`.

- [x] **Step 4: Record the key at journal time**

`execute()` (Task 11) and any caller must capture `message_key` **before** the move, whilst the message is still findable by numeric id, and store it on the entry.

- [x] **Step 5: Run the tests**

Run: `uv run pytest -v` — all must pass.

- [x] **Step 6: Migration**

A journal written before this change has no `message_key`. `load()` must tolerate that (default to empty) and `undo_run` must skip such entries with a clear warning rather than crashing or silently doing nothing. Add a test.

- [x] **Step 7: Commit**

```bash
git commit -m "fix: record RFC-822 Message-ID so undo survives moves"
```

---

### Task 11B: Do-not-file guard — mail awaiting a reply or action

**Files:**
- Create: `src/mail_triage/guards.py`
- Modify: `src/mail_triage/model/classify.py`, `src/mail_triage/envelope.py`, `src/mail_triage/review.py`
- Test: `tests/test_guards.py`

**Interfaces:**
- Consumes: `MessageRow` (Task 2), `Proposal` (Task 9).
- Produces: `Veto` dataclass (`reason: str`); `needs_attention(message: MessageRow, headers: dict[str, str] | None) -> Veto | None`; `is_bulk(sender: str, headers: dict[str, str] | None) -> bool`; `Classifier` gains an optional `guards` hook and `Proposal` gains `veto: str | None`.

The user's requirement, 26 July 2026: *"If anything requires me to do something or needs a reply they mustn't be filed away unless that has happened."*

A veto overrides confidence entirely. Two conditions, both chosen by the user:

1. **A human wrote it to him** — i.e. it is *not* bulk. Bulk means a `List-Unsubscribe` header is present, or the sender is a no-reply-style address. Everything else is treated as person-to-person and left in the inbox.
2. **He has flagged it** — `messages.flagged` in the envelope database, so this costs nothing.

**Unread status is deliberately not a guard.** Clearing the unread pile is the point of the tool; the user confirmed this explicitly when asked.

`MessageRow` must carry `flagged` — add it to the envelope query, which already selects from `messages`.

**On cost:** `List-Unsubscribe` requires an AppleScript round trip per message (~0.1–0.5s). With ~64 inbox messages that is acceptable, but only fetch headers for messages that would otherwise be filed, and only when the cheap sender-address heuristic is inconclusive.

Tests must cover: a flagged message vetoed despite 0.99 confidence; a bulk message with `List-Unsubscribe` not vetoed; a plain human sender vetoed; a `no-reply@` address treated as bulk; and headers being unavailable (AppleScript failure) failing **safe** — that is, veto rather than file.

---

### Task 11C: Deletion as negative evidence

**Files:**
- Create: `src/mail_triage/deletion.py`
- Modify: `src/mail_triage/corpus.py`, `src/mail_triage/model/store.py`, `src/mail_triage/review.py`
- Test: `tests/test_deletion.py`

**Interfaces:**
- Consumes: `EnvelopeReader`, `MessageRow`, `Config`.
- Produces: `DeletionStats` dataclass (`filed: int`, `deleted: int`) with a `delete_ratio` property; `build_deletion_index(reader, config, now=None) -> dict[str, DeletionStats]` keyed by normalised sender; `deletion_veto(stats: DeletionStats | None, config) -> str | None`.

The user's observation, 26 July 2026: *"sometimes I delete messages instead of filing them away."* Measured, this was not marginal — **19 of the 23 proposals in the first dry run came from senders whose mail had recently been deleted.**

Two patterns, handled differently per the user's decision:

- **Only deletes now** (measured examples: `0 filed / 9 deleted`, `0/7`, `0/4` over 75 days) — **veto the filing** and mark the sender as an unsubscribe candidate. These would otherwise be filed at 0.82–0.88 confidence on the strength of superseded history.
- **Keeps some, bins some** (`5/21`, `4/12`, `2/6`) — still propose, but the table shows the bin-rate so the choice is informed.

Config gains `delete_veto_ratio` (default 1.0, meaning veto only when nothing at all has been filed recently) and `deletion_window_days` (default 75). Both must be tunable without code changes, since the correct thresholds are unknown.

**Data caveat that must be honoured in the code and its comments:** the Trash purges on a rolling window — roughly two months at measurement time. Compare filing and deletion **over the same window**, or the comparison is meaningless: a sender with ten years of filing history and two months of deletions will look falsely balanced.

Training must stop excluding `Deleted Messages` wholesale; instead deleted mail feeds this index rather than the folder model. Deleted mail must never contribute a *folder* prediction.

Tests must cover: a sender with recent deletions and no recent filings vetoed; a mixed sender not vetoed but carrying its ratio; a sender with old filings and recent deletions correctly vetoed (the window bug); an unknown sender producing no veto; and the window boundary.

---

### Task 11: Live moves — CHECKPOINT

**Files:**
- Modify: `src/mail_triage/cli.py`
- Test: `tests/test_triage_flow.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `execute(decisions: list[Decision], mail: MailInterface, journal: Journal, account: str) -> tuple[int, int]` in a new `src/mail_triage/execute.py`, returning `(moved, failed)`.

> **CHECKPOINT — STOP HERE.** This task moves real mail for the first time. Before running any live command in this task, present the plan to the user and get explicit approval. Then test on **one** message, verify it landed in the right folder and is still unread if it was unread, and run `undo` to put it back — before ever running a full batch.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/live-moves
```

- [x] **Step 2: Write the failing test**

`tests/test_triage_flow.py`:

```python
from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.execute import execute
from mail_triage.journal import Journal, new_run_id
from mail_triage.mail_app import FakeMail
from mail_triage.model.classify import Proposal
from mail_triage.review import Decision


def decision(rowid=1, folder="Orders", accepted=True):
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s",
                         date_sent=1, mailbox_url="imap://A/INBOX", read=False)
    return Decision(Proposal(message, folder, 0.9, "reason", "sender"), accepted=accepted)


def test_accepted_decisions_are_moved(tmp_path):
    config = Config(account_url_prefix="imap://A", local_dir=tmp_path)
    journal = Journal(config)
    journal.begin(new_run_id())
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"])
    moved, failed = execute([decision(1), decision(2)], mail, journal, "iCloud")
    assert (moved, failed) == (2, 0)
    assert {entry[0] for entry in mail.moved} == {1, 2}


def test_rejected_decisions_are_not_moved(tmp_path):
    config = Config(account_url_prefix="imap://A", local_dir=tmp_path)
    journal = Journal(config)
    journal.begin(new_run_id())
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    moved, failed = execute([decision(1, accepted=False)], mail, journal, "iCloud")
    assert (moved, failed) == (0, 0)
    assert mail.moved == []


def test_a_failure_does_not_stop_the_batch(tmp_path):
    config = Config(account_url_prefix="imap://A", local_dir=tmp_path)
    journal = Journal(config)
    journal.begin(new_run_id())
    mail = FakeMail(inbox=[1, 2], mailboxes=["Orders"])
    moved, failed = execute(
        [decision(1, folder="Nonexistent"), decision(2)], mail, journal, "iCloud"
    )
    assert (moved, failed) == (1, 1)


def test_journal_is_written_before_the_move(tmp_path):
    config = Config(account_url_prefix="imap://A", local_dir=tmp_path)
    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    execute([decision(1)], mail, journal, "iCloud")
    entries = Journal(config).load(run_id)
    assert entries[0].status == "moved"
    assert entries[0].from_folder == "INBOX"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_triage_flow.py -v`
Expected: FAIL — no module `mail_triage.execute`

- [x] **Step 4: Implement `execute.py`**

```python
"""Carry out accepted decisions, journalling before acting."""

from __future__ import annotations

from mail_triage.journal import Journal, JournalEntry
from mail_triage.mail_app import MailError, MailInterface
from mail_triage.review import Decision


def execute(
    decisions: list[Decision], mail: MailInterface, journal: Journal, account: str
) -> tuple[int, int]:
    """Move each accepted message. Returns (moved, failed).

    The journal entry is written before the move is attempted, so a batch that
    fails part-way through is still fully reversible.
    """
    moved = 0
    failed = 0
    for decision in decisions:
        if not decision.accepted or decision.folder is None:
            continue
        entry = JournalEntry(
            message_id=decision.proposal.message.rowid,
            subject=decision.proposal.message.subject,
            from_folder="INBOX",
            to_folder=decision.folder,
            status="planned",
        )
        journal.record(entry)
        try:
            mail.move_message(entry.message_id, entry.to_folder, account)
        except MailError:
            entry.status = "failed"
            journal.record(entry)
            failed += 1
            continue
        entry.status = "moved"
        journal.record(entry)
        moved += 1
    return moved, failed
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_triage_flow.py -v`
Expected: PASS (4 tests)

- [x] **Step 6: Wire live mode into the CLI**

Change `--dry-run` to default `False`, add `--limit N` (default 0 meaning no limit) so the first live run can be capped at one message, and add the `undo` command:

```python
@cli.command()
@click.argument("run_id", required=False)
@click.option("--account", default="iCloud")
def undo(run_id: str | None, account: str) -> None:
    """Reverse a triage run. Defaults to the most recent."""
    config = load_config()
    runs = list_runs(config)
    if not runs:
        raise click.ClickException("No runs to undo.")
    target = run_id or runs[0]
    reversed_count, failed = undo_run(target, config, AppleScriptMail(), account)
    click.echo(f"Reversed {reversed_count} moves from run {target} ({failed} failed).")
```

- [x] **Step 7: CHECKPOINT — single-message live test, with approval**

Get the user's explicit approval, then:

```bash
uv run mail-triage triage --limit 1
```

Verify by hand in Mail: the message is in the expected folder, and its unread state is unchanged. Then:

```bash
uv run mail-triage undo
```

Verify it is back in the inbox, still unread. **If unread state is not preserved, stop and report — that contradicts the spec and needs a fix before any batch run.**

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/execute.py src/mail_triage/cli.py tests/test_triage_flow.py
git commit -m "feat: live moves with journalling and undo"
```

---

### Task 12: Corrections feed back into the model

**Files:**
- Create: `src/mail_triage/corrections.py`
- Modify: `src/mail_triage/model/store.py` (fold corrections into training), `src/mail_triage/review.py` (capture overrides)
- Test: `tests/test_corrections.py`

**Interfaces:**
- Consumes: `Decision` (Task 10), `TrainingExample` (Task 4), `Config` (Task 1).
- Produces: `Correction` dataclass (`sender: str`, `domain: str`, `subject: str`, `chosen_folder: str`, `rejected_folder: str | None`, `recorded_at: int`); `record_correction(correction: Correction, config: Config) -> None`; `load_corrections(config: Config) -> list[Correction]`; `corrections_as_examples(corrections: list[Correction], config: Config) -> list[TrainingExample]`.

This is the spec's fourth mechanism — **corrections outrank history** — at `correction_weight` (default 10×). It is also the signal that tells the user when auto mode is warranted.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feature/corrections
```

- [ ] **Step 2: Write the failing test**

`tests/test_corrections.py`:

```python
from mail_triage.config import Config
from mail_triage.corrections import (
    Correction, corrections_as_examples, load_corrections, record_correction,
)


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://A", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def correction(chosen="Finance", rejected="Orders"):
    return Correction(sender="bills@shop.example", domain="shop.example", subject="Invoice",
                      chosen_folder=chosen, rejected_folder=rejected, recorded_at=1_700_000_000)


def test_corrections_round_trip(tmp_path):
    config = make_config(tmp_path)
    record_correction(correction(), config)
    loaded = load_corrections(config)
    assert loaded[0].chosen_folder == "Finance"
    assert loaded[0].rejected_folder == "Orders"


def test_corrections_append_rather_than_overwrite(tmp_path):
    config = make_config(tmp_path)
    record_correction(correction(), config)
    record_correction(correction(chosen="Admin"), config)
    assert len(load_corrections(config)) == 2


def test_corrections_become_heavily_weighted_examples(tmp_path):
    config = make_config(tmp_path, correction_weight=10.0)
    examples = corrections_as_examples([correction()], config)
    assert examples[0].folder == "finance"
    assert examples[0].weight == 10.0


def test_loading_with_no_file_returns_empty(tmp_path):
    assert load_corrections(make_config(tmp_path)) == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_corrections.py -v`
Expected: FAIL — no module `mail_triage.corrections`

- [ ] **Step 4: Implement `corrections.py`**

```python
"""Record where the user actually wanted a message to go.

History is evidence; a correction is instruction. Corrections are weighted far
higher than historical filings, which is how an old habit gets overridden
without editing thousands of past decisions.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.folders import normalise_folder


@dataclass
class Correction:
    sender: str
    domain: str
    subject: str
    chosen_folder: str
    rejected_folder: str | None
    recorded_at: int


def record_correction(correction: Correction, config: Config) -> None:
    path: Path = config.corrections_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(asdict(correction)) + "\n")


def load_corrections(config: Config) -> list[Correction]:
    path = config.corrections_path
    if not path.exists():
        return []
    return [Correction(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def corrections_as_examples(
    corrections: list[Correction], config: Config
) -> list[TrainingExample]:
    """Corrections enter training at ``correction_weight`` times normal weight."""
    return [
        TrainingExample(
            sender=item.sender,
            domain=item.domain,
            subject=item.subject,
            folder=normalise_folder(item.chosen_folder),
            weight=config.correction_weight,
            year=time.gmtime(item.recorded_at).tm_year,
        )
        for item in corrections
    ]
```

- [ ] **Step 5: Fold corrections into training**

In `train_from_history`, after building the corpus:

```python
    from mail_triage.corrections import corrections_as_examples, load_corrections

    examples.extend(corrections_as_examples(load_corrections(config), config))
```

Add a test in `tests/test_store.py` asserting that a correction pointing `bills@shop.example` at `Finance` beats ten historical filings to `Orders`.

- [ ] **Step 6: Capture overrides during review**

Extend the step-through prompt so a reply that is neither `y` nor `n` is treated as a folder name override, producing `Decision(item, accepted=True, override_folder=<name>)`. Add a review test for it. In the `triage` command, write a `Correction` for every decision where the user rejected or overrode the proposal.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add src/mail_triage/corrections.py src/mail_triage/model/store.py src/mail_triage/review.py tests/
git commit -m "feat: corrections recorded and weighted above historical filings"
```

---

### Task 13: Stage B — naive Bayes over tokens — COMPLETE 27 July 2026

**Two deviations from the plan below, both driven by measurement against
2,568 held-out real messages (train on the older 85% of filed mail, test on
the newest 15%).**

1. **The folder-size prior was removed.** The plan's implementation multiplies
   in each folder's share of filed mail. Filing history is wildly imbalanced —
   `parent/orders` alone holds 26.9% — so big folders won ties on bulk rather
   than evidence: a payslip, an a social network digest and a newsletter all landed in
   `Parent/Orders` at 0.81–0.96 confidence. Precision falls monotonically as the
   prior is given more weight:

   | prior weight | right | wrong | precision |
   |---|---|---|---|
   | full (as planned) | 1607 | 459 | 77.8% |
   | half | 1636 | 360 | 82.0% |
   | one quarter | 1653 | 328 | 83.4% |
   | one tenth | 1701 | 306 | 84.8% |
   | **none** | **1698** | **297** | **85.1%** |

   Uniform is strictly better than full on *both* counts — more right and 162
   fewer wrong — so this is a defect fix, not a trade-off.

2. **A margin gate was added.** Naive Bayes probabilities are badly calibrated;
   the independence assumption drives them to 0.96 on thin evidence. Requiring
   the winner to be 10x likelier than the runner-up lifts precision to 87.1%
   (234 wrong), trading 112 correct filings to avoid 63 misfilings. Configurable
   via `TokenModel.predict(margin=...)`.

**One planned test was refuted and replaced.**
`test_weights_influence_the_outcome` asserted that a heavier folder should win
when two folders' subjects are identical. The sweep above shows that is exactly
the behaviour that costs precision, so the test now asserts the model abstains.
Recency weighting still reaches stage B through the token counts themselves.

**Effect on the live inbox (43 messages):** actionable proposals rose from 2 to
4. Modest, because most of the inbox is currently held by the deletion veto and
the invoice guard rather than by uncertainty. The held-out figures above are the
meaningful measure.

**Also fixed while wiring it up:** an unknown sender used to return early from
`classify`, so stage B never ran for the case the plan names as its main
purpose. `MODEL_VERSION` is now 2, and a version-1 model file is refused with
advice to re-run `learn` rather than silently loading without a token model.

**Files:**
- Create: `src/mail_triage/model/tokens.py`
- Modify: `src/mail_triage/model/classify.py`, `src/mail_triage/model/store.py`
- Test: `tests/test_tokens.py`

**Interfaces:**
- Consumes: `TrainingExample` (Task 4), `Prediction` (Task 5).
- Produces: `tokenise(subject: str, sender: str) -> list[str]`; `TokenModel` with `.train(examples) -> None`, `.predict(subject: str, sender: str) -> Prediction | None`, `.to_dict()`, `.from_dict()`.

Handles senders never seen before, which stage A cannot. Hand-rolled with Laplace smoothing and log-probabilities; no scikit-learn.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/token-model
```

- [x] **Step 2: Write the failing test**

`tests/test_tokens.py`:

```python
from mail_triage.corpus import TrainingExample
from mail_triage.model.tokens import TokenModel, tokenise


def example(subject, folder, sender="a@example.com", weight=1.0):
    return TrainingExample(sender=sender, domain="example.com", subject=subject,
                           folder=folder, weight=weight, year=2026)


def test_tokenise_lowercases_and_splits():
    assert "invoice" in tokenise("Your INVOICE is ready", "billing@shop.example")


def test_tokenise_includes_sender_domain_parts():
    tokens = tokenise("Hello", "billing@shop.example")
    assert "shop.example" in tokens


def test_tokenise_drops_very_short_tokens():
    assert "a" not in tokenise("a big thing", "x@y.example")


def test_predicts_from_subject_words():
    model = TokenModel()
    model.train(
        [example("Your invoice is ready", "finance") for _ in range(20)]
        + [example("Order dispatched today", "orders") for _ in range(20)]
    )
    prediction = model.predict("invoice attached", "new@stranger.example")
    assert prediction.folder == "finance"
    assert 0.0 < prediction.confidence <= 1.0


def test_returns_none_when_untrained():
    assert TokenModel().predict("anything", "a@b.example") is None


def test_unknown_words_do_not_crash():
    model = TokenModel()
    model.train([example("Your invoice", "finance")])
    assert model.predict("zzzz qqqq", "a@b.example") is not None


def test_round_trips_through_dict():
    model = TokenModel()
    model.train([example("Your invoice is ready", "finance") for _ in range(5)])
    restored = TokenModel.from_dict(model.to_dict())
    assert restored.predict("invoice", "a@b.example").folder == "finance"


def test_weights_influence_the_outcome():
    model = TokenModel()
    model.train(
        [example("shared word here", "orders", weight=0.01) for _ in range(50)]
        + [example("shared word here", "finance", weight=5.0) for _ in range(2)]
    )
    assert model.predict("shared word here", "a@b.example").folder == "finance"
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tokens.py -v`
Expected: FAIL — no module `mail_triage.model.tokens`

- [x] **Step 4: Implement `model/tokens.py`**

```python
"""Stage B: a weighted multinomial naive Bayes over subject and sender tokens.

Hand-rolled rather than pulled from scikit-learn: it is a page of arithmetic,
adds no install burden, and can name the tokens that drove a decision — which
matters when the training data is known to be imperfect.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import Prediction

MIN_TOKEN_LENGTH = 2
SMOOTHING = 1.0
_WORD = re.compile(r"[a-z0-9.\-_]+")


def tokenise(subject: str, sender: str) -> list[str]:
    """Lower-case word tokens from the subject, plus the sender's domain."""
    tokens = [
        word for word in _WORD.findall((subject or "").casefold())
        if len(word) >= MIN_TOKEN_LENGTH
    ]
    if "@" in (sender or ""):
        tokens.append(sender.casefold().split("@", 1)[1])
    return tokens


class TokenModel:
    """Weighted token counts per folder, plus folder priors."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, float]] = {}
        self.folder_totals: dict[str, float] = {}
        self.priors: dict[str, float] = {}
        self.vocabulary_size: int = 0

    def train(self, examples: list[TrainingExample]) -> None:
        counts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        priors: dict[str, float] = defaultdict(float)
        vocabulary: set[str] = set()
        for item in examples:
            priors[item.folder] += item.weight
            for token in tokenise(item.subject, item.sender):
                counts[item.folder][token] += item.weight
                vocabulary.add(token)
        self.counts = {folder: dict(tokens) for folder, tokens in counts.items()}
        self.folder_totals = {folder: sum(tokens.values()) for folder, tokens in self.counts.items()}
        self.priors = dict(priors)
        self.vocabulary_size = len(vocabulary)

    def predict(self, subject: str, sender: str) -> Prediction | None:
        if not self.priors:
            return None
        tokens = tokenise(subject, sender)
        prior_total = sum(self.priors.values())
        scores: dict[str, float] = {}
        for folder, prior in self.priors.items():
            score = math.log(prior / prior_total)
            total = self.folder_totals.get(folder, 0.0)
            denominator = total + SMOOTHING * max(self.vocabulary_size, 1)
            for token in tokens:
                occurrences = self.counts.get(folder, {}).get(token, 0.0)
                score += math.log((occurrences + SMOOTHING) / denominator)
            scores[folder] = score
        best_folder = max(scores, key=scores.get)
        # Convert log-scores to a normalised probability, shifting for stability.
        highest = scores[best_folder]
        exponentials = {folder: math.exp(score - highest) for folder, score in scores.items()}
        confidence = exponentials[best_folder] / sum(exponentials.values())
        contributing = ", ".join(
            token for token in tokens if self.counts.get(best_folder, {}).get(token)
        )[:80]
        return Prediction(
            folder=best_folder,
            confidence=confidence,
            reason=f"subject tokens ({contributing or 'prior only'}) suggest '{best_folder}'",
        )

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "folder_totals": self.folder_totals,
            "priors": self.priors,
            "vocabulary_size": self.vocabulary_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenModel:
        model = cls()
        model.counts = data.get("counts", {})
        model.folder_totals = data.get("folder_totals", {})
        model.priors = data.get("priors", {})
        model.vocabulary_size = data.get("vocabulary_size", 0)
        return model
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tokens.py -v`
Expected: PASS (8 tests)

- [x] **Step 6: Wire stage B into `TrainedModel` and `Classifier`**

Add `tokens: TokenModel` to `TrainedModel`, train and serialise it alongside the sender model, bump `MODEL_VERSION` to 2, and in `Classifier.classify` fall through to the token model when stage A returns `None` or lands below threshold. Stage B proposals carry `stage="tokens"`. Add classifier tests: a sender unknown to stage A but with a decisive subject gets a `tokens` proposal; a token prediction below threshold still yields `folder=None`.

- [x] **Step 7: Retrain and compare**

Run: `uv run mail-triage learn && uv run mail-triage triage --dry-run`
Expected: more messages placed than before Task 13. Note the before/after placed counts in the commit message.

- [x] **Step 8: Commit**

```bash
git add src/mail_triage/model tests/
git commit -m "feat: naive Bayes token model for senders without history"
```

---

### Task 14: `--auto` mode and `explain` — `explain` COMPLETE 28 July 2026

> `explain` is done (steps 6–8 for that half); `--auto` and `auto_decisions`
> are deliberately deferred until Task 12 exists. Auto-filing with no
> correction signal has nothing to tell it when it is wrong.
>
> `explain` reports any hard rule *before* the model's opinion, which the
> sketch below did not: a rule outranks stage A, so model-only output would
> state the opposite of what happens whenever the two disagree.

**Files:**
- Modify: `src/mail_triage/cli.py`
- Test: `tests/test_auto_mode.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `triage --auto` behaviour; `explain SENDER` command.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feature/auto-mode-and-explain
```

- [ ] **Step 2: Write the failing test**

`tests/test_auto_mode.py`:

```python
from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.review import auto_decisions


def proposal(confidence, folder="Orders", rowid=1):
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s", date_sent=1,
                         mailbox_url="imap://A/INBOX", read=False)
    return Proposal(message, folder, confidence, "reason", "sender")


def config(tmp_path, auto_threshold=0.9):
    return Config(account_url_prefix="imap://A", local_dir=tmp_path, auto_threshold=auto_threshold)


def test_auto_accepts_only_above_threshold(tmp_path):
    decisions = auto_decisions([proposal(0.95), proposal(0.5, rowid=2)], config(tmp_path))
    assert [decision.proposal.message.rowid for decision in decisions] == [1]


def test_auto_ignores_unplaced_proposals(tmp_path):
    assert auto_decisions([proposal(0.99, folder=None)], config(tmp_path)) == []


def test_threshold_is_configurable(tmp_path):
    decisions = auto_decisions([proposal(0.6)], config(tmp_path, auto_threshold=0.5))
    assert len(decisions) == 1
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auto_mode.py -v`
Expected: FAIL — `ImportError: cannot import name 'auto_decisions'`

- [ ] **Step 4: Implement `auto_decisions` in `review.py`**

```python
def auto_decisions(proposals: list[Proposal], config: Config) -> list[Decision]:
    """Accept proposals confident enough to act on without asking."""
    return [
        Decision(item, accepted=True)
        for item in proposals
        if item.folder is not None and item.confidence >= config.auto_threshold
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auto_mode.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Wire `--auto` into the CLI and add `explain`**

```python
@cli.command()
@click.argument("sender")
def explain(sender: str) -> None:
    """Show why mail from a sender is filed where it is."""
    config = load_config()
    model = load_model(config.model_path)
    domain = sender.split("@", 1)[1] if "@" in sender else sender
    prediction = model.sender.predict(sender.casefold(), domain.casefold())
    if prediction is None:
        click.echo(f"No history for {sender}. Stage B would decide from the subject.")
        return
    click.echo(f"{sender} → '{prediction.folder}' at confidence {prediction.confidence:.2f}")
    click.echo(f"Reason: {prediction.reason}")
    counts = model.sender.by_sender.get(sender.casefold()) or model.sender.by_domain.get(domain.casefold(), {})
    for folder, weight in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        click.echo(f"  {folder:<30} {weight:>8.2f}")
```

Modes are mutually exclusive: raise a `ClickException` if both `--auto` and `--dry-run` are given.

- [ ] **Step 7: Verify**

Run: `uv run mail-triage explain <a sender from your inbox>`
Expected: destination, confidence, and the full weighted folder breakdown.

- [ ] **Step 8: Commit**

```bash
git add src/mail_triage/cli.py src/mail_triage/review.py tests/test_auto_mode.py
git commit -m "feat: auto mode and explain command"
```

---

### Task 15: Stage C — optional LLM tier with redaction

**Files:**
- Create: `src/mail_triage/model/llm.py`
- Modify: `src/mail_triage/model/classify.py`, `config.example.toml`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Proposal` (Task 9), `Config` (Task 1).
- Produces: `redact(text: str) -> str`; `LLMTier(client, folders: list[str])` with `.classify(subject: str, sender: str, snippet: str) -> Prediction | None`; `build_prompt(subject: str, sender: str, snippet: str, folders: list[str]) -> str`.

**Off by default.** Requires `llm_enabled = true` in config plus `ANTHROPIC_API_KEY`. Redaction is applied before anything leaves the machine, and is tested independently of any network call.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feature/llm-tier
```

- [ ] **Step 2: Write the failing test**

`tests/test_llm.py`:

```python
from mail_triage.model.llm import LLMTier, build_prompt, redact


def test_redacts_email_addresses_to_domains():
    assert redact("Contact alice@example.com now") == "Contact <address>@example.com now"


def test_redacts_long_digit_runs():
    assert redact("Order 1234567890123 shipped") == "Order <number> shipped"


def test_keeps_short_numbers():
    assert "12" in redact("12 items")


def test_prompt_lists_only_the_allowed_folders():
    prompt = build_prompt("Subject", "a@b.example", "snippet", ["Orders", "Finance"])
    assert "Orders" in prompt and "Finance" in prompt
    assert "exactly one" in prompt.casefold()


def test_prompt_contains_redacted_text_only():
    prompt = build_prompt("Invoice for alice@example.com", "a@b.example", "acct 9876543210987", ["Finance"])
    assert "alice@example.com" not in prompt
    assert "9876543210987" not in prompt


def test_unknown_folder_from_the_model_is_rejected():
    class StubClient:
        def complete(self, prompt: str) -> str:
            return "Nonexistent Folder"

    assert LLMTier(StubClient(), ["Orders"]).classify("s", "a@b.example", "x") is None


def test_valid_folder_is_accepted():
    class StubClient:
        def complete(self, prompt: str) -> str:
            return "Orders"

    prediction = LLMTier(StubClient(), ["Orders"]).classify("s", "a@b.example", "x")
    assert prediction.folder == "orders"


def test_client_is_never_called_when_disabled():
    class ExplodingClient:
        def complete(self, prompt: str) -> str:
            raise AssertionError("must not be called")

    assert LLMTier(ExplodingClient(), []).classify("s", "a@b.example", "x") is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — no module `mail_triage.model.llm`

- [ ] **Step 4: Implement `model/llm.py`**

```python
"""Stage C: an optional LLM tier for the small minority nothing else can place.

Off by default. Everything sent is redacted first: addresses lose their local
part, and long digit runs — account, order and card-like references — are
masked. The model may only answer with a folder that already exists.
"""

from __future__ import annotations

import re
from typing import Protocol

from mail_triage.folders import normalise_folder
from mail_triage.model.sender import Prediction

SNIPPET_LIMIT = 500
_ADDRESS = re.compile(r"[\w.+-]+@([\w.-]+)")
_LONG_DIGITS = re.compile(r"\d{6,}")


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


def redact(text: str) -> str:
    """Strip identifying detail before anything leaves the machine."""
    text = _ADDRESS.sub(lambda match: f"<address>@{match.group(1)}", text or "")
    return _LONG_DIGITS.sub("<number>", text)


def build_prompt(subject: str, sender: str, snippet: str, folders: list[str]) -> str:
    listing = "\n".join(f"- {name}" for name in folders)
    return (
        "Choose the best folder for this email. Reply with exactly one folder "
        "name from the list, and nothing else. If none fits, reply NONE.\n\n"
        f"Folders:\n{listing}\n\n"
        f"From: {redact(sender)}\n"
        f"Subject: {redact(subject)}\n"
        f"Body extract: {redact(snippet)[:SNIPPET_LIMIT]}\n"
    )


class LLMTier:
    def __init__(self, client: LLMClient, folders: list[str]) -> None:
        self.client = client
        self.folders = folders
        self._allowed = {normalise_folder(name) for name in folders}

    def classify(self, subject: str, sender: str, snippet: str) -> Prediction | None:
        if not self.folders:
            return None
        answer = self.client.complete(build_prompt(subject, sender, snippet, self.folders)).strip()
        normalised = normalise_folder(answer)
        if normalised not in self._allowed:
            return None
        return Prediction(
            folder=normalised,
            confidence=0.75,
            reason="classified by the language model from subject and body extract",
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Wire stage C in behind a config flag**

Add `llm_enabled: bool = False` and `llm_model: str = "claude-sonnet-5"` to `Config` and `config.example.toml`. In `Classifier`, accept an optional `llm: LLMTier | None = None` and consult it only when stages A and B both fail. Add a classifier test asserting the LLM is not consulted when stage A is confident.

The concrete Anthropic client is a thin wrapper implementing `complete`; add `anthropic` as an optional dependency group so the base install stays dependency-light.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add src/mail_triage/model/llm.py src/mail_triage/model/classify.py src/mail_triage/config.py config.example.toml tests/test_llm.py
git commit -m "feat: optional LLM tier with redaction, disabled by default"
```

---

### Task 16: Unsubscribe suggestions — CHECKPOINT

**Files:**
- Create: `src/mail_triage/unsubscribe.py`
- Modify: `src/mail_triage/cli.py`, `src/mail_triage/mail_app.py` (add `send_mail`)
- Test: `tests/test_unsubscribe.py`

**Interfaces:**
- Consumes: `MailInterface` (Task 7), `EnvelopeReader` (Task 2), `Config` (Task 1).
- Produces: `UnsubscribeOption` dataclass (`sender: str`, `domain: str`, `method: str`, `target: str`, `message_count: int`, `unread_count: int`); `parse_list_unsubscribe(header: str) -> tuple[str, str] | None` returning `(method, target)` where method is `mailto` or `http`; `find_candidates(reader, config, mail, limit: int) -> list[UnsubscribeOption]`; `send_unsubscribe(option: UnsubscribeOption, mail: MailInterface) -> None`.

> **CHECKPOINT — STOP HERE.** This task **sends mail**, which the original spec forbade. It exists because the user asked for it on 26 July 2026. Update the spec's "Deliberately excluded" section before implementing. Get explicit approval before the first real send, and send exactly one before any batch.

Behaviour: suggest the noisiest senders you never read, one at a time, with counts. Typing `y` sends the unsubscribe email; anything else skips. Only `mailto:` targets are sent — HTTP one-click unsubscribe is deliberately out of scope for now, as it means arbitrary outbound web requests.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feature/unsubscribe
```

- [ ] **Step 2: Write the failing test**

`tests/test_unsubscribe.py`:

```python
import pytest

from mail_triage.mail_app import FakeMail
from mail_triage.unsubscribe import (
    UnsubscribeOption, parse_list_unsubscribe, rank_candidates, send_unsubscribe,
)


def test_parses_a_mailto_target():
    assert parse_list_unsubscribe("<mailto:leave@list.example>") == ("mailto", "leave@list.example")


def test_prefers_mailto_over_http():
    header = "<https://list.example/u?x=1>, <mailto:leave@list.example>"
    assert parse_list_unsubscribe(header) == ("mailto", "leave@list.example")


def test_returns_http_when_that_is_all_there_is():
    assert parse_list_unsubscribe("<https://list.example/u>") == ("http", "https://list.example/u")


def test_strips_mailto_query_parameters():
    header = "<mailto:leave@list.example?subject=unsubscribe>"
    assert parse_list_unsubscribe(header) == ("mailto", "leave@list.example")


def test_returns_none_for_junk():
    assert parse_list_unsubscribe("not a header") is None
    assert parse_list_unsubscribe("") is None


def test_ranking_puts_the_most_ignored_first():
    options = [
        UnsubscribeOption("a@x.example", "x.example", "mailto", "l@x.example", message_count=10, unread_count=1),
        UnsubscribeOption("b@y.example", "y.example", "mailto", "l@y.example", message_count=40, unread_count=39),
    ]
    assert rank_candidates(options)[0].sender == "b@y.example"


def test_sending_uses_the_mail_bridge():
    mail = FakeMail(inbox=[], mailboxes=[])
    option = UnsubscribeOption("a@x.example", "x.example", "mailto", "leave@x.example", 10, 9)
    send_unsubscribe(option, mail)
    assert mail.sent == [("leave@x.example", "unsubscribe")]


def test_refuses_to_send_to_an_http_target():
    mail = FakeMail(inbox=[], mailboxes=[])
    option = UnsubscribeOption("a@x.example", "x.example", "http", "https://x.example/u", 10, 9)
    with pytest.raises(ValueError, match="mailto"):
        send_unsubscribe(option, mail)
    assert mail.sent == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_unsubscribe.py -v`
Expected: FAIL — no module `mail_triage.unsubscribe`

- [ ] **Step 4: Add `send_mail` to the Mail bridge**

To `MailInterface`, `AppleScriptMail` and `FakeMail`:

```python
    def send_mail(self, to_address: str, subject: str, body: str) -> None:
        """Send a message from the default account."""
        script = (
            'tell application "Mail"\n'
            f'  set newMessage to make new outgoing message with properties '
            f'{{subject:"{subject}", content:"{body}", visible:false}}\n'
            "  tell newMessage\n"
            f'    make new to recipient at end of to recipients with properties {{address:"{to_address}"}}\n'
            "  end tell\n"
            "  send newMessage\n"
            "end tell"
        )
        _run(script)
```

`FakeMail.send_mail` appends `(to_address, subject)` to `self.sent`.

- [ ] **Step 5: Implement `unsubscribe.py`**

```python
"""Suggest mailing lists worth leaving, and send the unsubscribe request.

This is the one part of mail-triage that sends mail. It never does so without
an explicit 'y' for that specific sender. HTTP one-click unsubscribe is
deliberately unsupported: it would mean arbitrary outbound requests to
addresses supplied by the sender.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from mail_triage.envelope import MessageRow
from mail_triage.mail_app import MailInterface

_TARGET = re.compile(r"<([^>]+)>")


@dataclass(frozen=True)
class UnsubscribeOption:
    sender: str
    domain: str
    method: str  # mailto | http
    target: str
    message_count: int
    unread_count: int

    @property
    def ignored_share(self) -> float:
        return self.unread_count / self.message_count if self.message_count else 0.0


def parse_list_unsubscribe(header: str) -> tuple[str, str] | None:
    """Extract a target from a List-Unsubscribe header, preferring mailto."""
    targets = _TARGET.findall(header or "")
    for target in targets:
        if target.casefold().startswith("mailto:"):
            address = target[len("mailto:"):]
            return "mailto", address.split("?", 1)[0]
    for target in targets:
        if target.casefold().startswith("http"):
            return "http", target
    return None


def rank_candidates(options: list[UnsubscribeOption]) -> list[UnsubscribeOption]:
    """Most-ignored, highest-volume senders first."""
    return sorted(
        options,
        key=lambda option: (option.unread_count, option.ignored_share, option.message_count),
        reverse=True,
    )


def tally_senders(messages: list[MessageRow]) -> dict[str, tuple[int, int]]:
    """Return sender → (message_count, unread_count)."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for message in messages:
        totals[message.sender][0] += 1
        if not message.read:
            totals[message.sender][1] += 1
    return {sender: (counts[0], counts[1]) for sender, counts in totals.items()}


def send_unsubscribe(option: UnsubscribeOption, mail: MailInterface) -> None:
    """Send the unsubscribe request. Only mailto targets are supported."""
    if option.method != "mailto":
        raise ValueError(
            f"Cannot send to a {option.method} target; only mailto unsubscribe is supported."
        )
    mail.send_mail(option.target, "unsubscribe", "unsubscribe")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_unsubscribe.py -v`
Expected: PASS (8 tests)

- [ ] **Step 7: Add the `unsubscribe` command**

Reads the inbox and recent history via `EnvelopeReader`, tallies senders, fetches `List-Unsubscribe` headers via AppleScript for the top `--limit` candidates (default 20, since each header fetch is an AppleScript round trip), ranks them, and offers each in turn:

```
news@list.example — 84 messages, 81 unread (96% ignored)
Unsubscribe via leave@list.example? [y/N]
```

`y` sends; anything else skips. Print a tally at the end. Add `--dry-run` to list candidates without offering to send.

- [ ] **Step 8: CHECKPOINT — first real send, with approval**

Get the user's explicit approval. Run `uv run mail-triage unsubscribe --dry-run` first and review the list. Then run interactively and accept exactly **one**. Confirm in Mail's Sent folder that the message went where expected before doing any more.

- [ ] **Step 9: Update the spec**

Edit `docs/superpowers/specs/2026-07-26-mail-triage-design.md`: remove "composing or sending mail" from *Deliberately excluded*, and add an Unsubscribe section describing the confirm-per-sender flow and the mailto-only restriction.

- [ ] **Step 10: Commit**

```bash
git add src/mail_triage/unsubscribe.py src/mail_triage/mail_app.py src/mail_triage/cli.py tests/test_unsubscribe.py docs/
git commit -m "feat: unsubscribe suggestions with per-sender confirmation"
```

---

### Task 17: README and repository hygiene — COMPLETE 28 July 2026

> Delivered in `8124da2`, ahead of the tasks numbered before it. The guard
> test grew from the three below to eleven: real folder paths, identifying
> terms drawn from `local/identifying-terms.txt`, the example config carrying
> no real account, nothing under `local/` being tracked by git, and — after
> `e99b4d1` — that `src/` and `docs/` are not themselves accidentally
> ignored, which the original `.gitignore` came close to doing.

**Files:**
- Create: `README.md`, `CLAUDE.md`
- Test: `tests/test_no_personal_data.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a guard test that fails if personal data reaches `src/`.

- [x] **Step 1: Create the branch**

```bash
git checkout -b feature/docs-and-hygiene
```

- [x] **Step 2: Write the guard test**

`tests/test_no_personal_data.py`:

```python
"""The repository is destined for GitHub. Nothing personal may reach src/."""

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
ALLOWED_DOMAINS = {"example.com", "shop.example", "list.example"}


def test_no_real_email_addresses_in_source():
    import re

    pattern = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
    offences = []
    for path in SOURCE.rglob("*.py"):
        for match in pattern.findall(path.read_text()):
            domain = match.split("@", 1)[1]
            if domain not in ALLOWED_DOMAINS and not domain.endswith(".example"):
                offences.append(f"{path.name}: {match}")
    assert offences == [], f"Possible real addresses in source: {offences}"


def test_no_account_uuids_in_source():
    import re

    uuid = re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b")
    offences = [path.name for path in SOURCE.rglob("*.py") if uuid.search(path.read_text())]
    assert offences == [], f"Account UUIDs found in: {offences}"


def test_local_directory_is_gitignored():
    root = Path(__file__).resolve().parents[1]
    assert "local/" in (root / ".gitignore").read_text()
```

- [x] **Step 3: Run the tests**

Run: `uv run pytest tests/test_no_personal_data.py -v`
Expected: PASS (3 tests). If any fail, remove the offending literal before going further.

- [x] **Step 4: Write `README.md`**

Cover: what it does, the safety model (read-only database access, AppleScript for writes, journalled undo, nothing personal in the repo), setup (`uv sync`, `mail-triage accounts`, copy `config.example.toml` to `local/config.toml`, Full Disk Access), the command reference, and the confirm-then-auto progression. British English throughout.

- [x] **Step 5: Write `CLAUDE.md`**

Record for future sessions: the verified environment facts from this plan's header, the safety rules (never write to the database, never move mail without approval, never commit to master), the `local/` convention, and the command list.

- [x] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [x] **Step 7: Commit**

```bash
git add README.md CLAUDE.md tests/test_no_personal_data.py
git commit -m "docs: README, working notes, and a guard against personal data in source"
```

---

### Task 18: Asking about uncertain senders — COMPLETE

Implements `docs/superpowers/specs/2026-07-26-asking-when-unsure-design.md`.
Added after Task 11, ahead of Task 12, because the first live dry run left the
largest single group of inbox messages — senders the model knows but cannot
call — with nothing done about them and nothing learnt from them.

**Files:**
- Created: `src/mail_triage/rules.py`, `src/mail_triage/asking.py`
- Modified: `src/mail_triage/model/classify.py`, `src/mail_triage/cli.py`,
  `src/mail_triage/config.py`
- Tests: `tests/test_rules.py`, `tests/test_asking.py`, plus additions to
  `tests/test_classify.py` and `tests/test_cli.py`

- [x] **Step 1: Rules store** — `Rule`, `load_rules`, `record_rule`,
  `forget_rule`, `Config.rules_path`. A corrupt file raises `RulesError`
  naming the file and line; `bin` is rejected as an action, so the deferral is
  enforced by the suite.
- [x] **Step 2: Classifier precedence** — `Classifier(rules=...)`. Order:
  per-message guards, then rules, then the deletion veto, then Stages A/B/C.
  `_apply_guard` split into `_message_guards` (flagged, needs-a-reply — these
  alone override a rule) and `_bulk_guard`.
- [x] **Step 3: Ranking** — `rank_uncertain` by yearly sending rate, inbox
  count as tie-break, capped at five. `build_yearly_counts` over the same
  snapshot as everything else in the run.
- [x] **Step 4: The question** — `ask` / `ask_all`, four answers (numbered
  candidate, typed path, leave alone, skip). Typos are re-prompted, never
  stored. Every answer is written to disk as given, so Ctrl-C keeps them.
- [x] **Step 5: CLI** — `triage --ask/--no-ask` asks before the proposal
  table and re-classifies so answers apply to the current run; new `rules`
  command lists answers and `rules --forget <sender>` removes one.
- [x] **Step 6: Verified on the real inbox** (read-only; every question
  skipped, no rules written). 65 messages, 34 uncertain, top five senders
  ranked at 46, 27, 24, 12 and 11 messages a year — the ranking rule visibly
  doing what the spec argued for.

**Tests:** 283 passing (was 215).

**Still deferred, per the spec:** bin rules, pending invoice detection.

---

### Live checkpoint: deletion — PASSED 27 July 2026

The delete paths added on 27 July (per-message `d`, and the binning pass over
unplaced mail) had never touched real mail. Run under the same protocol as
Task 11: one message, verified, undone.

Message: a vendor marketing email, chosen because it was first in the
binning list, so no position counting was needed and the other 44 candidates
were provably untouched (`q` immediately after).

| Step | Result |
|---|---|
| Bin one message | `Moved 1 messages, 1 binned (0 failed)` |
| Journal | `planned` → `moved`, `to_folder: Deleted Messages`, durable key recorded |
| Location after | `Deleted Messages`, **rowid 447033 → 447790** |
| `undo` | `Reversed 1 moves (0 failed)`, 3.9s |
| Location after undo | `INBOX`, rowid 447791 |

Two things worth recording:

1. **The rowid changed twice** (447033 → 447790 → 447791), exactly as the
   environment facts predict. This is the case the durable RFC-822
   `message_key` exists for, and it worked.
2. **Verifying against the database requires copying the `-wal` file.** A
   first check copied only `Envelope Index` and showed the message still in
   the Trash *after* a successful undo — stale data, not a bug, but it looked
   exactly like one for a minute. Use `snapshot_database()`, which copies the
   companions, rather than an ad-hoc `cp`.

Read status is not a concern: the AppleScript only ever issues
`move theMessage to theBox` and never sets `read`.

---

## Self-review notes

**Spec coverage.** Every spec section maps to a task: scope and config → 1; snapshot and reader → 2; cross-account folder naming → 3; recency weighting → 4; consistency gating and drift → 5; model persistence and `learn` → 6; AppleScript bridge → 7; journal and undo → 8; "existing folders only" and explainability → 9; dry-run and confirm mode → 10; live moves → 11; corrections outranking history → 12; stage B → 13; auto mode and `explain` → 14; stage C with redaction → 15; unsubscribe → 16; publishability → 17.

**Two spec corrections this plan makes:**
1. The spec names RFC-822 `Message-ID` as the identity join. **It does not exist in the database.** The verified key is `messages.ROWID` == AppleScript message `id`.
2. The spec says the tool never sends mail. Task 16 sends unsubscribe requests, at the user's request of 26 July 2026. Task 16 Step 9 updates the spec.

**One spec gap the plan closes:** the spec did not say which accounts to *learn* from, only which to triage. Investigation found 53,000 messages in an On My Mac archive — older iCloud mail moved off the server yearly, using the same folder names. the user's decision on 26 July 2026 is to train on iCloud alone for now, so `training_accounts` defaults to the triaged account and the archive is one config line away.

**Known deferrals.**
- The read-only briefing mode the spec calls "a later addition" is not planned here; it should get its own spec once triage is in daily use.
- Re-archiving old iCloud mail to local storage — the practice that created the archive — was raised as a possible future feature. Not in scope; it would need its own spec, being a bulk mutation of thousands of messages.
- Task 8's `undo_run` needs `move_message` to accept a source folder — flagged inline in that task.
- HTTP one-click unsubscribe (RFC 8058) is deliberately unsupported in Task 16.
