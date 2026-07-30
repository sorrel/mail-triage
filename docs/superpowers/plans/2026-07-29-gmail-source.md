# Gmail as a Second Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Triage the Gmail inbox alongside iCloud in one run, filing both into the single existing iCloud folder tree.

**Architecture:** `Config` grows a list of `Source` records (account name, URL prefix, inbox, trash) plus one filing target that supplies the candidate folder list. Gmail inbox membership is read from the Envelope Index `labels` table rather than `messages.mailbox`, via a new `EnvelopeReader.inbox_messages`. Filing crosses accounts; binning and deletion evidence stay within each source. The source account for any message is derived from `account_prefix(message.mailbox_url)`, so neither `Proposal` nor `Decision` needs a new field.

**Tech Stack:** Python 3.13, `uv`, `click`, `pytest`, `sqlite3`, AppleScript via `osascript`.

**Spec:** `docs/superpowers/specs/2026-07-29-gmail-source-design.md`

## Global Constraints

- **British English** everywhere: code, comments, output, docs, commit messages.
- **Never write to Apple Mail's database.** Read from a snapshot, opened read-only. Every mutation goes through AppleScript.
- **No test may touch a real mailbox or shell out to `osascript`.** Use synthetic fixtures and `FakeMail`.
- **Nothing personal in `src/`, `tests/` or `docs/`** — no addresses, account UUIDs, real folder names or subject lines. Use `imap://AAAAAAAA` / `imap://BBBBBBBB` and folder names like `Parent/Child`.
- **Never commit to `master`.** This work is on branch `feature/gmail-source`.
- **Never verify with a live run.** Task 10 is the only live step and it is a user checkpoint.
- **Precedence is unchanged:** guards → rules → deletion veto → stage A → stage B.
- Run the suite with `uv run pytest -q`. It must stay green: 488 tests pass before this plan starts.
- Terminal output uses `review.display_width()`, never `len()`.

---

### Task 1: `Source` records in config

**Files:**
- Modify: `src/mail_triage/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Source` frozen dataclass with fields `name: str`, `prefix: str`, `inbox: str = "INBOX"`, `trash: str = "Deleted Messages"`, `ignore: list[str] = []`
  - `Config.sources: list[Source]`
  - `Config.filing_account: str` and `Config.filing_account_prefix: str`
  - `Config.source_for(prefix: str) -> Source | None`
  - `Config.training_prefixes` unchanged in meaning

A legacy config (one naming `account_url_prefix` and no `[[source]]`) must synthesise exactly one `Source` and behave as today. That is the migration path and it is tested first.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
import tomllib
from pathlib import Path

from mail_triage.config import Config, Source, load_config


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
```

Add `import pytest` at the top of the file if it is not already imported.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'Source'`.

- [ ] **Step 3: Implement**

In `src/mail_triage/config.py`, add above `Config`:

```python
@dataclass(frozen=True)
class Source:
    """One account whose inbox gets triaged.

    ``trash`` is where a "delete" answer sends a message *from this account*.
    A bin is not a filing destination, so binning never crosses accounts and
    each source needs its own name for it — Apple Mail calls the iCloud one
    "Deleted Messages" and the Gmail one "[Gmail]/Bin".

    ``ignore`` lists folder patterns that represent no filing decision in this
    account, over and above the standard set. Gmail needs it: "[Gmail]/All
    Mail" holds every message in the account and must never be counted as a
    filing. Patterns are fnmatch globs, in which "[Gmail]" is a *character
    class* matching one of G m a i l — write "[[]Gmail]*" to match the
    literal bracket. See ``folders.is_excluded``.
    """

    name: str
    prefix: str
    inbox: str = "INBOX"
    trash: str = "Deleted Messages"
    ignore: list[str] = field(default_factory=list)
```

Add to `Config` (keeping every existing field, including the legacy
`account_url_prefix`, `inbox_folder` and `trash_folder`, which the
synthesised source reads from):

```python
    sources: list[Source] = field(default_factory=list)
    filing_account: str = "iCloud"
    filing_account_prefix: str = ""

    def source_for(self, prefix: str) -> Source | None:
        """The source owning ``prefix``, or None if it is not being triaged."""
        for source in self.sources:
            if source.prefix == prefix:
                return source
        return None
```

Replace the tail of `load_config` (from `values = tomllib.loads(...)`) with:

```python
    values = tomllib.loads(path.read_text())
    local_dir = Path(values.pop("local_dir", path.parent))
    raw_sources = values.pop("source", [])
    account_name = values.pop("account_name", "iCloud")
    filing_prefix = values.pop("filing_account_prefix", "")
    filing_account = values.pop("filing_account", account_name)

    if raw_sources:
        sources = [Source(**entry) for entry in raw_sources]
        if not filing_prefix:
            raise ValueError(
                "config with [[source]] tables must also set filing_account_prefix, "
                "naming the account whose folders mail is filed into"
            )
    else:
        # Legacy single-account shape. Synthesised rather than special-cased
        # downstream, so there is exactly one code path from here on.
        if "account_url_prefix" not in values:
            raise ValueError("config must set account_url_prefix or [[source]] tables")
        sources = [
            Source(
                name=account_name,
                prefix=values["account_url_prefix"],
                inbox=values.get("inbox_folder", "INBOX"),
                trash=values.get("trash_folder", "Deleted Messages"),
            )
        ]
        filing_prefix = filing_prefix or values["account_url_prefix"]

    seen: set[str] = set()
    for source in sources:
        if source.prefix in seen:
            raise ValueError(
                f"prefix {source.prefix!r} appears more than once in [[source]]; "
                "each account may only be triaged once per run"
            )
        seen.add(source.prefix)

    values.setdefault("account_url_prefix", sources[0].prefix)
    return Config(
        local_dir=local_dir,
        sources=sources,
        filing_account=filing_account,
        filing_account_prefix=filing_prefix,
        **values,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: 488 + 6 pass, nothing broken.

- [ ] **Step 5: Document the new shape**

Rewrite `config.example.toml` to lead with the sources shape, keeping every
existing tuning key and its comment unchanged:

```toml
# Copy to local/config.toml and edit. local/ is gitignored.
# Run 'mail-triage accounts' to discover your account prefixes.

# Where filed mail lands: the account whose folder tree is the filing
# structure. Mail from every source below is filed into it, crossing
# accounts where it must.
filing_account        = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

# Each account whose inbox gets triaged. 'name' must match the name Mail
# shows; 'prefix' is scheme://first-8-chars-of-account-uuid.
[[source]]
name   = "iCloud"
prefix = "imap://AAAAAAAA"
inbox  = "INBOX"
trash  = "Deleted Messages"

# Gmail keeps inbox membership as a label, not a mailbox — mail-triage
# handles that. 'ignore' keeps Gmail's pseudo-folders out of the filing and
# deletion counts: "[Gmail]/All Mail" holds every message in the account.
# Note "[[]Gmail]*", not "[Gmail]*": these are fnmatch globs, in which
# "[Gmail]" is a character class matching one of G m a i l.
[[source]]
name   = "Gmail"
prefix = "imap://BBBBBBBB"
inbox  = "INBOX"
trash  = "[Gmail]/Bin"
ignore = ["[[]Gmail]*"]
```

Then append the existing `training_accounts` through `deletion_window_days`
block from the current file verbatim, dropping only `account_url_prefix`,
`inbox_folder` and `trash_folder`, which sources now carry.

- [ ] **Step 6: Commit**

```bash
git add src/mail_triage/config.py tests/test_config.py config.example.toml
git commit -m "feat: config describes several triage sources, one filing tree"
```

---

### Task 2: Read a Gmail inbox from the `labels` table

**Files:**
- Modify: `src/mail_triage/envelope.py`
- Test: `tests/test_envelope.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EnvelopeReader.inbox_messages(url: str) -> Iterator[MessageRow]`

This is the finding that makes the whole feature necessary: a Gmail message's
`messages.mailbox` points at `[Gmail]/All Mail`, and its inbox membership is a
row in `labels(message_id, mailbox_id)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_envelope.py`, following the fixture style already in that
file (build a small SQLite database in `tmp_path`):

```python
def _build_db(tmp_path, *, with_labels=True):
    """A two-account database: one plain mailbox, one Gmail-shaped one."""
    path = tmp_path / "Envelope Index"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT);
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY, sender INTEGER, subject INTEGER,
            date_sent INTEGER, mailbox INTEGER, read INTEGER, flagged INTEGER
        );
        """
    )
    if with_labels:
        con.execute("CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER)")
    con.execute("INSERT INTO mailboxes VALUES (1, 'imap://AAAAAAAA/INBOX')")
    con.execute("INSERT INTO mailboxes VALUES (2, 'imap://BBBBBBBB/INBOX')")
    con.execute("INSERT INTO mailboxes VALUES (3, 'imap://BBBBBBBB/%5BGmail%5D/All%20Mail')")
    con.execute("INSERT INTO addresses VALUES (1, 'someone@example.com')")
    con.execute("INSERT INTO subjects VALUES (1, 'A subject')")
    # 10: a plain inbox message. 20 and 21: Gmail messages living in All Mail.
    con.execute("INSERT INTO messages VALUES (10, 1, 1, 1000, 1, 1, 0)")
    con.execute("INSERT INTO messages VALUES (20, 1, 1, 1000, 3, 1, 0)")
    con.execute("INSERT INTO messages VALUES (21, 1, 1, 1000, 3, 1, 0)")
    if with_labels:
        # 20 is in the Gmail inbox; 21 is not. 99 is a stale row whose
        # message no longer exists — real databases carry these.
        con.execute("INSERT INTO labels VALUES (20, 2)")
        con.execute("INSERT INTO labels VALUES (99, 2)")
    con.commit()
    con.close()
    return path


def test_inbox_messages_finds_label_only_members(tmp_path):
    reader = EnvelopeReader(_build_db(tmp_path))
    found = list(reader.inbox_messages("imap://BBBBBBBB/INBOX"))
    reader.close()
    assert [m.rowid for m in found] == [20]


def test_inbox_messages_ignores_stale_label_rows(tmp_path):
    """A label row pointing at a vanished message must not invent a message."""
    reader = EnvelopeReader(_build_db(tmp_path))
    found = list(reader.inbox_messages("imap://BBBBBBBB/INBOX"))
    reader.close()
    assert 99 not in [m.rowid for m in found]


def test_inbox_messages_matches_messages_in_mailbox_without_labels(tmp_path):
    """An account with no label rows must behave exactly as before."""
    reader = EnvelopeReader(_build_db(tmp_path))
    plain = [m.rowid for m in reader.messages_in_mailbox("imap://AAAAAAAA/INBOX")]
    via_inbox = [m.rowid for m in reader.inbox_messages("imap://AAAAAAAA/INBOX")]
    reader.close()
    assert plain == via_inbox == [10]


def test_inbox_messages_survives_a_database_with_no_labels_table(tmp_path):
    """Older Mail versions and minimal fixtures have no labels table."""
    reader = EnvelopeReader(_build_db(tmp_path, with_labels=False))
    found = list(reader.inbox_messages("imap://AAAAAAAA/INBOX"))
    reader.close()
    assert [m.rowid for m in found] == [10]


def test_inbox_messages_does_not_duplicate_a_message_in_both(tmp_path):
    """A message attributed to the mailbox *and* labelled with it appears once."""
    path = _build_db(tmp_path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO labels VALUES (10, 1)")
    con.commit()
    con.close()
    reader = EnvelopeReader(path)
    found = list(reader.inbox_messages("imap://AAAAAAAA/INBOX"))
    reader.close()
    assert [m.rowid for m in found] == [10]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_envelope.py -q`
Expected: FAIL with `AttributeError: 'EnvelopeReader' object has no attribute 'inbox_messages'`.

- [ ] **Step 3: Implement**

Add to `EnvelopeReader` in `src/mail_triage/envelope.py`, directly after
`messages_in_mailbox`:

```python
    def inbox_messages(self, url: str) -> Iterator[MessageRow]:
        """Messages in a mailbox by primary attribution *or* by Gmail label.

        Apple Mail models Gmail's labels properly: a Gmail message's
        ``messages.mailbox`` points at "[Gmail]/All Mail" whatever labels it
        carries, and inbox membership is a row in ``labels``. Filtering on
        the mailbox URL alone therefore reports a Gmail inbox as empty.

        The join to ``messages`` is what discards stale ``labels`` rows —
        entries whose message has gone. On the mailbox this was written
        against, 11 raw rows became the 9 messages Mail itself reports, with
        no explicit staleness handling needed.

        Generic rather than Gmail-specific: an account with no label rows
        contributes nothing to the second half of the union, so a plain IMAP
        account behaves exactly as ``messages_in_mailbox`` does.
        """
        yield from self._rows("WHERE b.url = ?", (url,))
        try:
            labelled = self.connection.execute(
                "SELECT l.message_id FROM labels l "
                "JOIN mailboxes lb ON lb.ROWID = l.mailbox_id WHERE lb.url = ?",
                (url,),
            ).fetchall()
        except sqlite3.OperationalError:
            # No labels table: an older Mail version, or a minimal fixture.
            return
        ids = [row[0] for row in labelled]
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        # ``b.url != ?`` keeps a message that is both attributed to the
        # mailbox and labelled with it from being yielded twice.
        yield from self._rows(
            f"WHERE m.ROWID IN ({placeholders}) AND b.url != ?", (*ids, url)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_envelope.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/envelope.py tests/test_envelope.py
git commit -m "feat: read inbox membership from the labels table as well"
```

---

### Task 3: Make the `[Gmail]` glob safe, and prove it

**Files:**
- Modify: `src/mail_triage/folders.py` (comment only)
- Test: `tests/test_folders.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no new API. This task is a regression test plus a warning comment.

`is_excluded` uses `fnmatch`, where `[Gmail]` is a character class matching one
of `G m a i l`. The pattern a reader would reach for first is silently wrong in
both directions, and the folder it fails to exclude holds every message in the
account. No code change is needed — only proof and a signpost.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_folders.py`:

```python
def test_bracketed_gmail_pattern_needs_an_escaped_bracket():
    """"[Gmail]*" is a character class, not a literal — the classic trap."""
    assert not is_excluded("[Gmail]/All Mail", ["[Gmail]*"])
    assert is_excluded("[Gmail]/All Mail", ["[[]Gmail]*"])


def test_escaped_gmail_pattern_excludes_the_pseudo_folders():
    patterns = ["[[]Gmail]*"]
    for folder in ("[Gmail]/All Mail", "[Gmail]/Bin", "[Gmail]/Important",
                   "[Gmail]/Starred", "[Gmail]All Mail"):
        assert is_excluded(folder, patterns), folder


def test_escaped_gmail_pattern_does_not_match_a_plain_folder():
    """The unescaped form would match "G/anything"; the escaped form must not."""
    for folder in ("Gmail/Notes", "G/Something", "Parent/Child"):
        assert not is_excluded(folder, ["[[]Gmail]*"]), folder
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_folders.py -q`
Expected: FAIL — `test_bracketed_gmail_pattern_needs_an_escaped_bracket` fails
on the first assertion if `is_excluded` behaves differently than assumed, and
`test_escaped_gmail_pattern_excludes_the_pseudo_folders` fails on
`"[Gmail]All Mail"` if the leaf-name branch does not cover it.

If a test fails for a reason other than the pattern being unescaped, stop and
report it rather than adjusting the assertion to match: the point of this task
is to pin down the real behaviour.

- [ ] **Step 3: Make them pass**

Expected outcome: the first and third tests pass immediately (they describe
`fnmatch`'s existing behaviour), and the second passes because `is_excluded`
matches the whole path as well as the leaf. If any do not, fix `is_excluded`
rather than the test.

Add this comment above `is_excluded` in `src/mail_triage/folders.py`:

```python
# Patterns are fnmatch globs, so square brackets are character classes. A
# literal bracket must be escaped as "[[]": "[Gmail]*" matches "Gmail/..." and
# "G/..." whilst missing every real Gmail folder, and the one it most needs to
# catch — "[Gmail]/All Mail" — holds every message in the account. Write
# "[[]Gmail]*". See tests/test_folders.py.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_folders.py -q` — expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/folders.py tests/test_folders.py
git commit -m "test: pin down the [Gmail] glob trap before relying on it"
```

---

### Task 4: A deletion index per source

**Files:**
- Modify: `src/mail_triage/deletion.py`
- Test: `tests/test_deletion.py`

**Interfaces:**
- Consumes: `Source` from Task 1.
- Produces: `build_deletion_index(reader, config, source, now=None) -> dict[str, DeletionStats]` — the `source` parameter is new and required.

Per the spec, a message is judged against its own account's deletion evidence.
Two changes: the window is scoped to one source's prefix, and "deleted" means
that source's own `trash`.

**Ordering matters.** The check for deleted must come *before* the ignore
check, because Gmail's `ignore` pattern `[[]Gmail]*` also matches `[Gmail]/Bin`.
Get this the wrong way round and Gmail's deletions vanish silently.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deletion.py`, reusing that file's existing fake-reader style:

```python
from mail_triage.config import Source


def _reader(rows):
    """Minimal stand-in exposing only what build_deletion_index reads."""
    class _R:
        def all_messages(self):
            return iter(rows)
    return _R()


ICLOUD = Source(name="iCloud", prefix="imap://AAAAAAAA", trash="Deleted Messages")
GMAIL = Source(
    name="Gmail", prefix="imap://BBBBBBBB", trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]
)


def _row(rowid, sender, url, date_sent):
    return MessageRow(
        rowid=rowid, sender=sender, subject="s", date_sent=date_sent,
        mailbox_url=url, read=True,
    )


def test_gmail_bin_counts_as_a_deletion(tmp_path):
    now = 1_000_000
    rows = [_row(1, "a@example.com", "imap://BBBBBBBB/%5BGmail%5D/Bin", now - 100)]
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    index = build_deletion_index(_reader(rows), config, GMAIL, now=now)
    assert index["a@example.com"].deleted == 1
    assert index["a@example.com"].filed == 0


def test_gmail_all_mail_is_ignored_not_counted_as_filed(tmp_path):
    """All Mail holds every message; counting it as filing defeats the veto."""
    now = 1_000_000
    rows = [_row(1, "a@example.com", "imap://BBBBBBBB/%5BGmail%5D/All%20Mail", now - 100)]
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    index = build_deletion_index(_reader(rows), config, GMAIL, now=now)
    assert "a@example.com" not in index


def test_an_index_only_sees_its_own_account(tmp_path):
    now = 1_000_000
    rows = [
        _row(1, "a@example.com", "imap://AAAAAAAA/Deleted%20Messages", now - 100),
        _row(2, "a@example.com", "imap://BBBBBBBB/%5BGmail%5D/Bin", now - 100),
    ]
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    assert build_deletion_index(_reader(rows), config, ICLOUD, now=now)[
        "a@example.com"
    ].deleted == 1
    assert build_deletion_index(_reader(rows), config, GMAIL, now=now)[
        "a@example.com"
    ].deleted == 1


def test_icloud_deletion_counting_is_unchanged(tmp_path):
    """The legacy Deleted*/Trash patterns must keep working alongside trash."""
    now = 1_000_000
    rows = [
        _row(1, "a@example.com", "imap://AAAAAAAA/Deleted%20Messages", now - 100),
        _row(2, "a@example.com", "imap://AAAAAAAA/Parent/Child", now - 100),
    ]
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    stats = build_deletion_index(_reader(rows), config, ICLOUD, now=now)["a@example.com"]
    assert (stats.filed, stats.deleted) == (1, 1)
```

Import `Config`, `MessageRow` and `build_deletion_index` at the top of the file
if they are not already imported.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deletion.py -q`
Expected: FAIL with `TypeError: build_deletion_index() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Implement**

In `src/mail_triage/deletion.py`, change the signature and the loop body:

```python
def build_deletion_index(
    reader, config: Config, source: Source, now: int | None = None
) -> dict[str, DeletionStats]:
    """Count one source's filed vs deleted messages per sender, in one window.

    Scoped to a single account on purpose. Filing and binning habits differ
    between accounts, and a sender filed in one whilst binned in the other
    would produce a pooled ratio reflecting neither — so each source gets its
    own index and each message is judged against its own account's.
    """
```

Replace `prefixes = tuple(config.training_prefixes)` with:

```python
    prefixes = (source.prefix,)
    ignored = _IGNORED_FOLDER_PATTERNS + list(source.ignore)
```

and replace the classification block at the end of the loop with:

```python
        # Deleted is checked *first*: a source's ignore patterns may also
        # match its trash (Gmail's "[[]Gmail]*" covers "[Gmail]/Bin"), and
        # testing ignore first would silently discard every deletion.
        if folder.casefold() == source.trash.casefold() or is_excluded(
            folder, _DELETED_FOLDER_PATTERNS
        ):
            bucket = counts.setdefault(sender, [0, 0])
            bucket[1] += 1
        elif is_excluded(folder, ignored):
            continue
        else:
            bucket = counts.setdefault(sender, [0, 0])
            bucket[0] += 1
```

Note the `counts.setdefault` moves inside the branches so an ignored folder no
longer creates an empty bucket. Add `from mail_triage.config import Config, Source`
to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deletion.py -q` — expected: PASS.
Then `uv run pytest -q`. Existing callers in `cli.py` will now fail to run;
that is expected and is fixed in Task 9. If any *test* fails, update it to pass
the source explicitly.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/deletion.py tests/test_deletion.py
git commit -m "feat: deletion evidence is counted per account, not pooled"
```

---

### Task 5: Cross-account moves in the Mail bridge

**Files:**
- Modify: `src/mail_triage/mail_app.py`
- Test: `tests/test_mail_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MailInterface.move_message(..., source_account: str | None = None)` — `account` remains the *target*; `source_account` defaults to it
  - `FakeMail(..., accounts: dict[str, dict[str, list[int]]] | None = None)`

Adding an optional `source_account` that defaults to `account` keeps every
existing call site correct and leaves single-account behaviour untouched.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mail_app.py`:

```python
def test_move_script_addresses_source_and_target_accounts_separately():
    mail = AppleScriptMail()
    script = mail._move_script(
        1, "Parent/Child", "iCloud", "INBOX", message_key="<k@example.com>",
        source_account="Gmail",
    )
    assert 'mailbox "Parent/Child" of account "iCloud"' in script
    assert 'of account "Gmail" whose message id is "<k@example.com>"' in script


def test_move_script_defaults_source_account_to_the_target():
    mail = AppleScriptMail()
    script = mail._move_script(1, "Parent/Child", "iCloud", "INBOX")
    assert script.count('of account "iCloud"') == 2


def test_fake_mail_keeps_accounts_separate():
    mail = FakeMail(
        inbox=[],
        mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {"INBOX": [2]}},
        keys={1: "<one@example.com>"},
    )
    assert mail.inbox_message_ids("Gmail") == [1]
    assert mail.inbox_message_ids("iCloud") == [2]


def test_fake_mail_moves_across_accounts():
    mail = FakeMail(
        inbox=[],
        mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys={1: "<one@example.com>"},
    )
    mail.move_message(1, "Parent/Child", "iCloud", source_folder="INBOX",
                      source_account="Gmail")
    assert mail.inbox_message_ids("Gmail") == []
    assert mail.folder_message_ids("Parent/Child", account="iCloud") == [1]


def test_fake_mail_without_accounts_behaves_as_before():
    """The legacy single-namespace fake must be untouched."""
    mail = FakeMail(inbox=[1], mailboxes=["INBOX", "Parent/Child"])
    mail.move_message(1, "Parent/Child", "anything", source_folder="INBOX")
    assert mail.inbox_message_ids("anything") == []
    assert mail.folder_message_ids("Parent/Child") == [1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mail_app.py -q`
Expected: FAIL with `TypeError: _move_script() got an unexpected keyword argument 'source_account'`.

- [ ] **Step 3: Implement**

In `AppleScriptMail`, add `source_account: str | None = None` as the last
parameter of both `_move_script` and `move_message`. At the top of
`_move_script`:

```python
        # ``account`` names the *target*. A Gmail message filed into the
        # iCloud tree has a different account at each end; defaulting the
        # source to the target keeps every within-account call identical to
        # what it was. Note that a cross-account move is a copy-and-delete
        # over IMAP, not a relabelling: the message leaves the source account.
        source_account = source_account or account
        source_account_escaped = _escape_applescript_string(source_account)
```

and replace `of account "{account_escaped}"` with
`of account "{source_account_escaped}"` in **both** source-mailbox lookups
(the `message_key` branch and the numeric-id branch), leaving the two
`set theBox to mailbox ... of account "{account_escaped}"` lines alone.

In `move_message`, pass it through:

```python
        script = self._move_script(
            message_id, folder, account, source_folder, message_key, source_account
        )
```

Add `source_account: str | None = None` to the `MailInterface` protocol's
`move_message` signature too.

In `FakeMail.__init__`, add the parameter and key contents by account, using a
wildcard bucket for the legacy shape:

```python
        accounts: dict[str, dict[str, list[int]]] | None = None,
```

```python
        # Contents are keyed (account, folder). The legacy constructor args
        # land under "*", a wildcard that answers for any account name — that
        # is exactly the account-blind behaviour every existing test expects.
        self._folder_contents: dict[tuple[str, str], list[int]] = {
            ("*", "INBOX"): list(inbox)
        }
        for name, ids in (folders or {}).items():
            self._folder_contents[("*", name)] = list(ids)
        for account_name, contents in (accounts or {}).items():
            for name, ids in contents.items():
                self._folder_contents[(account_name, name)] = list(ids)
        self._accounts_given = bool(accounts)
```

Add a resolver and update the readers:

```python
    def _contents(self, account: str, folder: str) -> list[int]:
        """The list backing one mailbox, creating it on first use."""
        key = (account, folder)
        if key not in self._folder_contents and ("*", folder) in self._folder_contents:
            key = ("*", folder)
        return self._folder_contents.setdefault(key, [])

    def inbox_message_ids(self, account: str) -> list[int]:
        return list(self._contents(account, "INBOX"))

    def folder_message_ids(self, folder: str, account: str = "*") -> list[int]:
        return list(self._contents(account, folder))
```

In `FakeMail.move_message`, add the same `source_account` parameter, then
replace the body's contents lookups:

```python
        source_account = source_account or account
        source_contents = self._contents(source_account, source_folder)
```

and the final mutation:

```python
        source_contents.remove(message_id)
        self._contents(account, folder).append(message_id)
        self.moved.append((message_id, folder, account, source_folder))
```

Leave the `moved` tuple shape alone — existing tests assert on it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mail_app.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: all pass, including every existing
`FakeMail` test unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/mail_app.py tests/test_mail_app.py
git commit -m "feat: move mail between accounts, and let FakeMail model two"
```

---

### Task 6: Execute decisions against their own source

**Files:**
- Modify: `src/mail_triage/execute.py`
- Test: `tests/test_execute.py`

**Interfaces:**
- Consumes: `Source` (Task 1), `move_message(..., source_account=)` (Task 5).
- Produces: `execute(decisions, mail, journal, config, sources_by_prefix: dict[str, Source]) -> tuple[int, int]`

The source account for a decision comes from
`account_prefix(decision.proposal.message.mailbox_url)`, so no new field is
needed on `Decision`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_execute.py`:

```python
from mail_triage.config import Source
from mail_triage.folders import account_prefix

ICLOUD = Source(name="iCloud", prefix="imap://AAAAAAAA", trash="Deleted Messages")
GMAIL = Source(name="Gmail", prefix="imap://BBBBBBBB", trash="[Gmail]/Bin")
BY_PREFIX = {ICLOUD.prefix: ICLOUD, GMAIL.prefix: GMAIL}


def _gmail_decision(folder="Parent/Child", action="file"):
    message = MessageRow(
        rowid=1, sender="a@example.com", subject="s", date_sent=0,
        mailbox_url="imap://BBBBBBBB/%5BGmail%5D/All%20Mail", read=True,
    )
    proposal = Proposal(message=message, folder=folder, confidence=0.95,
                        reason="r", stage="sender")
    return Decision(proposal=proposal, accepted=True, action=action)


def test_a_gmail_message_is_filed_into_the_filing_account(tmp_path):
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child", "[Gmail]/Bin"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys={1: "<one@example.com>"},
    )
    journal = Journal(_config(tmp_path))
    journal.begin("r1")
    moved, failed = execute(
        [_gmail_decision()], mail, journal, _config(tmp_path), BY_PREFIX
    )
    assert (moved, failed) == (1, 0)
    assert mail.folder_message_ids("Parent/Child", account="iCloud") == [1]


def test_the_journal_records_both_accounts(tmp_path):
    config = _config(tmp_path)
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys={1: "<one@example.com>"},
    )
    journal = Journal(config)
    journal.begin("r1")
    execute([_gmail_decision()], mail, journal, config, BY_PREFIX)
    entry = list(journal.load("r1"))[-1]
    assert entry.from_account == "Gmail"
    assert entry.to_account == "iCloud"
    assert entry.from_folder == "INBOX"


def test_binning_a_gmail_message_stays_in_gmail(tmp_path):
    """A bin is not a filing destination, so it must never cross accounts."""
    config = _config(tmp_path)
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child", "[Gmail]/Bin"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys={1: "<one@example.com>"},
    )
    journal = Journal(config)
    journal.begin("r1")
    execute([_gmail_decision(folder=None, action="delete")], mail, journal,
            config, BY_PREFIX)
    assert mail.folder_message_ids("[Gmail]/Bin", account="Gmail") == [1]
    entry = list(journal.load("r1"))[-1]
    assert entry.to_account == "Gmail"
    assert entry.to_folder == "[Gmail]/Bin"


def test_a_message_from_an_unconfigured_account_is_skipped(tmp_path):
    """Never guess which account a message came from."""
    config = _config(tmp_path)
    message = MessageRow(
        rowid=1, sender="a@example.com", subject="s", date_sent=0,
        mailbox_url="imap://CCCCCCCC/INBOX", read=True,
    )
    decision = Decision(
        proposal=Proposal(message=message, folder="Parent/Child", confidence=0.9,
                          reason="r", stage="sender"),
        accepted=True,
    )
    mail = FakeMail(inbox=[], mailboxes=["INBOX", "Parent/Child"],
                    accounts={"iCloud": {}}, keys={1: "<one@example.com>"})
    journal = Journal(config)
    journal.begin("r1")
    moved, failed = execute([decision], mail, journal, config, BY_PREFIX)
    assert (moved, failed) == (0, 1)
```

Add a `_config` helper to the file if one is not already present:

```python
def _config(tmp_path):
    return Config(
        local_dir=tmp_path,
        account_url_prefix="imap://AAAAAAAA",
        filing_account="iCloud",
        filing_account_prefix="imap://AAAAAAAA",
        sources=[ICLOUD, GMAIL],
    )
```

Existing tests in this file call `execute(..., account="iCloud", inbox_folder=..., trash_folder=...)`.
Update each to the new signature, passing `_config(tmp_path)` and a
`{ICLOUD.prefix: ICLOUD}` mapping, and give their `MessageRow` fixtures a
`mailbox_url` under `imap://AAAAAAAA/`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_execute.py -q`
Expected: FAIL with `TypeError` on `execute()`.

- [ ] **Step 3: Implement**

Rewrite the signature and the per-decision preamble in
`src/mail_triage/execute.py`:

```python
def execute(
    decisions: list[Decision],
    mail: MailInterface,
    journal: Journal,
    config: Config,
    sources_by_prefix: dict[str, Source],
) -> tuple[int, int]:
```

Inside the loop, after the `folder is None and not is_delete` skip:

```python
        # Which account this message came from, taken from the message itself
        # rather than passed in: a run covers several sources at once, and
        # only the message knows which one it belongs to.
        prefix = account_prefix(decision.proposal.message.mailbox_url)
        source = sources_by_prefix.get(prefix)
        if source is None:
            # An unconfigured account. Never guess: a wrong source account
            # sends the move looking in the wrong mailbox, and the message
            # would be recorded in the journal as having come from somewhere
            # it did not.
            failed += 1
            continue
        # A bin stays in its own account; a filing goes to the filing tree.
        if decision.is_delete:
            destination = source.trash
            target_account = source.name
        else:
            destination = decision.folder
            target_account = config.filing_account
```

Set the entry's accounts and pass them through:

```python
            from_folder=source.inbox,
            to_folder=destination,
            from_account=source.name,
            to_account=target_account,
```

```python
            entry.message_key = mail.message_key(message_id, source.inbox, source.name)
```

```python
            mail.move_message(
                message_id,
                entry.to_folder,
                target_account,
                source_folder=source.inbox,
                message_key=entry.message_key,
                source_account=source.name,
            )
```

Delete the now-unused `account`, `inbox_folder` and `trash_folder` parameters,
and update the module docstring's numbered rules to mention that a decision's
source account is derived from its message. Add
`from mail_triage.config import Config, Source` and
`from mail_triage.folders import account_prefix` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_execute.py -q` — expected: PASS.
Then `uv run pytest -q`. `cli.py` still calls the old signature and is fixed in
Task 9; only test failures matter here.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/execute.py tests/test_execute.py
git commit -m "feat: file each message from the account it actually came from"
```

---

### Task 7: Undo across accounts

**Files:**
- Modify: `src/mail_triage/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `move_message(..., source_account=)` (Task 5).
- Produces: `undo_run` unchanged in signature; `account` stays the fallback for journals written before cross-account support.

Reversing a cross-account move means looking for the message in `to_folder` of
`to_account` and returning it to `from_folder` of `from_account`. Today
`undo_run` passes one account for both ends.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_journal.py`:

```python
def test_undo_returns_a_cross_account_move_to_its_source(tmp_path):
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    journal = Journal(config)
    journal.begin("r1")
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", from_account="Gmail", to_account="iCloud",
        message_key="<one@example.com>",
    ))
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": []}, "iCloud": {"Parent/Child": [1]}},
        keys={1: "<one@example.com>"},
    )
    reversed_count, failed = undo_run("r1", config, mail, "iCloud")
    assert (reversed_count, failed) == (1, 0)
    assert mail.inbox_message_ids("Gmail") == [1]
    assert mail.folder_message_ids("Parent/Child", account="iCloud") == []


def test_undo_of_a_single_account_move_is_unchanged(tmp_path):
    """A journal with no accounts recorded still reverses against the fallback."""
    config = Config(local_dir=tmp_path, account_url_prefix="imap://AAAAAAAA")
    journal = Journal(config)
    journal.begin("r1")
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", message_key="<one@example.com>",
    ))
    mail = FakeMail(inbox=[], mailboxes=["INBOX", "Parent/Child"],
                    folders={"Parent/Child": [1]}, keys={1: "<one@example.com>"})
    reversed_count, failed = undo_run("r1", config, mail, "iCloud")
    assert (reversed_count, failed) == (1, 0)
    assert mail.inbox_message_ids("iCloud") == [1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_journal.py -q`
Expected: FAIL — the first test leaves the message in `Parent/Child` because
the reversal looks for it in the wrong account.

- [ ] **Step 3: Implement**

In `undo_run`, replace the `mail.move_message(...)` call with:

```python
            mail.move_message(
                entry.message_id,
                entry.from_folder,
                entry.from_account or account,
                source_folder=entry.to_folder,
                message_key=entry.message_key,
                # Where the message is *now*. For a cross-account filing this
                # differs from where it is going back to, and looking in the
                # wrong account finds nothing at all.
                source_account=entry.to_account or entry.from_account or account,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_journal.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/journal.py tests/test_journal.py
git commit -m "feat: undo puts cross-account mail back in the account it left"
```

---

### Task 8: An Account column in the proposal table

**Files:**
- Modify: `src/mail_triage/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `Source` (Task 1).
- Produces: `render_table(proposals, accounts: dict[str, str] | None = None) -> str` — `accounts` maps account prefix to display name; when `None` or holding fewer than two entries, the column is omitted and output is byte-identical to today's.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_review.py`:

```python
def test_table_has_no_account_column_for_one_source():
    proposals = [_proposal("a@example.com", "Parent/Child")]
    assert "Account" not in render_table(proposals)
    assert render_table(proposals) == render_table(proposals, {"imap://AAAAAAAA": "iCloud"})


def test_table_shows_the_account_when_there_are_two():
    accounts = {"imap://AAAAAAAA": "iCloud", "imap://BBBBBBBB": "Gmail"}
    table = render_table(
        [_proposal("a@example.com", "Parent/Child", prefix="imap://BBBBBBBB")], accounts
    )
    assert "Account" in table
    assert "Gmail" in table


def test_account_column_is_padded_by_display_width():
    """Emoji in a sender must not shift the columns that follow it."""
    accounts = {"imap://AAAAAAAA": "iCloud", "imap://BBBBBBBB": "Gmail"}
    lines = render_table(
        [
            _proposal("📧@example.com", "Parent/Child", prefix="imap://AAAAAAAA"),
            _proposal("b@example.com", "Parent/Child", prefix="imap://BBBBBBBB"),
        ],
        accounts,
    ).splitlines()
    assert len({display_width(line) for line in lines}) == 1
```

Add a `_proposal` helper if the file lacks one, giving the message a
`mailbox_url` built from `prefix`:

```python
def _proposal(sender, folder, prefix="imap://AAAAAAAA", confidence=0.9):
    message = MessageRow(
        rowid=1, sender=sender, subject="A subject", date_sent=0,
        mailbox_url=f"{prefix}/INBOX", read=True,
    )
    return Proposal(message=message, folder=folder, confidence=confidence,
                    reason="r", stage="sender")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_review.py -q`
Expected: FAIL with `TypeError: render_table() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement**

In `src/mail_triage/review.py`, add `ACCOUNT_WIDTH = 10` beside the other
widths and rewrite `render_table`:

```python
def render_table(
    proposals: list[Proposal], accounts: dict[str, str] | None = None
) -> str:
    """Render placed proposals as an aligned table.

    Unplaced proposals (``folder is None``) are deliberately excluded — they
    are not a filing suggestion, so showing them here would misrepresent what
    the tool is about to do. Use ``summarise`` for an account of those.

    ``accounts`` maps account prefix to the name Mail shows. The Account
    column appears only when more than one account is being triaged: with a
    single source it is the same value on every row and buys nothing but
    width.
    """
    show_account = accounts is not None and len(accounts) > 1
    header = ""
    if show_account:
        header = f"{_column('Account', ACCOUNT_WIDTH)} "
    lines = [
        f"{header}{_column('Sender', SENDER_WIDTH)} {_column('Subject', SUBJECT_WIDTH)} "
        f"{_column('→ Folder', FOLDER_WIDTH)} Conf"
    ]
    for item in proposals:
        if not item.is_actionable:
            continue
        destination = item.folder if item.folder is not None else "(bin — your rule)"
        prefix_cell = ""
        if show_account:
            name = accounts.get(account_prefix(item.message.mailbox_url), "?")
            prefix_cell = f"{_column(name, ACCOUNT_WIDTH)} "
        lines.append(
            f"{prefix_cell}{_column(item.message.sender, SENDER_WIDTH)} "
            f"{_column(item.message.subject, SUBJECT_WIDTH)} "
            f"{_column(destination, FOLDER_WIDTH)} {item.confidence:.2f}"
        )
    return "\n".join(lines)
```

Add `from mail_triage.folders import account_prefix` to the imports. `_column`
already pads by `display_width`, so no width arithmetic changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_review.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/review.py tests/test_review.py
git commit -m "feat: name the account in the proposal table when triaging two"
```

---

### Task 9: Wire the triage command to every source

**Files:**
- Modify: `src/mail_triage/cli.py:200-350`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: `mail-triage triage [--source NAME]...`, replacing `--account`.

`--account` is removed. Its default was one hardcoded account name, which no
longer means anything now that a run covers several. `--source NAME` may be
given more than once to restrict the run; the default is every configured
source.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`, in the style that file already uses for invoking
commands via `CliRunner`:

```python
def test_source_option_rejects_an_unknown_name(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)  # two sources: iCloud and Gmail
    result = CliRunner().invoke(cli, ["triage", "--source", "Nonesuch", "--dry-run"])
    assert result.exit_code != 0
    assert "Nonesuch" in result.output
    assert "iCloud" in result.output  # names the ones it does know


def test_account_option_is_gone(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, ["triage", "--account", "iCloud", "--dry-run"])
    assert result.exit_code != 0
    assert "no such option" in result.output.casefold()


def test_dry_run_scans_every_configured_source(tmp_path, monkeypatch):
    """Both inboxes are read, and nothing moves."""
    _install_config(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, ["triage", "--dry-run", "--no-ask"])
    assert result.exit_code == 0
    assert "Dry run — nothing was moved." in result.output
```

Write `_install_config` to write a two-source `local/config.toml` into
`tmp_path`, monkeypatch `mail_triage.cli.load_config` to load it, and
monkeypatch `snapshot_database` to return a fixture database containing both
accounts (reuse the builder from Task 2's tests by importing it, or duplicate
it locally — do not import from `tests.test_envelope` if the suite has no
package layout for it).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL — `--source` is not a known option.

- [ ] **Step 3: Implement**

Replace the `--account` option on `triage` with:

```python
@click.option(
    "--source",
    "source_names",
    multiple=True,
    help="Triage only these sources, by name. Repeatable. Default: all of them.",
)
def triage(dry_run: bool, limit: int, ask: bool, source_names: tuple[str, ...]) -> None:
```

After `config = load_config()`, select the sources:

```python
    sources = list(config.sources)
    if source_names:
        known = {source.name: source for source in sources}
        missing = [name for name in source_names if name not in known]
        if missing:
            raise click.ClickException(
                f"No source named {', '.join(repr(n) for n in missing)}. "
                f"Configured sources: {', '.join(sorted(known))}."
            )
        sources = [known[name] for name in source_names]
```

Inside the `with tempfile.TemporaryDirectory()` block, replace the
single-inbox lookup with a loop over `sources`, collecting messages and
per-source deletion indices:

```python
            messages = []
            deletion_indices: dict[str, dict] = {}
            for source in sources:
                inbox_url = next(
                    (
                        url for url in reader.mailbox_urls()
                        if url.startswith(source.prefix)
                        and folder_path(url).casefold() == source.inbox.casefold()
                    ),
                    None,
                )
                if inbox_url is None:
                    raise click.ClickException(
                        f"No mailbox '{source.inbox}' under {source.prefix} for source "
                        f"'{source.name}'. Run 'mail-triage accounts' to check the prefix."
                    )
                # inbox_messages, not messages_in_mailbox: a Gmail inbox is a
                # label, and filtering on the mailbox URL alone finds nothing.
                messages.extend(reader.inbox_messages(inbox_url))
                deletion_indices[source.prefix] = build_deletion_index(
                    reader, config, source
                )
            # Candidate folders come from the filing account only: there is one
            # filing tree, and every source's mail goes into it.
            folders = [
                folder_path(url)
                for url in reader.mailbox_urls()
                if url.startswith(config.filing_account_prefix) and folder_path(url)
            ]
```

The classifier takes one deletion index, so give it a view that dispatches on
the message's own account. Add near the top of `cli.py`:

```python
class _PerAccountDeletionIndex:
    """Deletion stats looked up against the message's own account.

    The classifier asks for one sender's stats without knowing which account
    the message came from, so the sender key is qualified with the account
    prefix on the way in. Filing and binning habits differ per account and
    pooling them would describe neither.
    """

    def __init__(self, indices: dict[str, dict]) -> None:
        self._indices = indices

    def for_message(self, message) -> dict:
        return self._indices.get(account_prefix(message.mailbox_url), {})
```

and pass `deletion_index=_PerAccountDeletionIndex(deletion_indices)` to
`Classifier`. In `model/classify.py`, where the classifier currently reads
`self.deletion_index.get(sender)`, change it to:

```python
        index = self.deletion_index
        if hasattr(index, "for_message"):
            index = index.for_message(message)
        stats = index.get(sender) if index else None
```

Add a test in `tests/test_classify.py` covering both shapes:

```python
def test_classifier_accepts_a_plain_deletion_index_or_a_per_account_one():
    ...
```

asserting the same veto fires whether a plain dict or a `_PerAccountDeletionIndex`
is supplied.

Update the trash pre-check before the batch to check each source's own trash
against that source's mailboxes, and update the `execute` and table calls:

```python
    click.echo(render_table(proposals, {s.prefix: s.name for s in sources}))
```

```python
    moved, failed = execute(
        accepted, mail, journal, config, {s.prefix: s for s in sources}
    )
```

Update the `undo` command's `--account` option, which becomes a *fallback*
rather than the account: entries record both ends themselves, and this value
is only consulted for journals written before that existed. Reword its help
to say so:

```python
@click.option(
    "--account",
    default="iCloud",
    help="Fallback account for journal entries written before runs recorded "
    "their own. Ignored for anything triaged since.",
)
```

Also update
`build_yearly_counts` / `build_billing_senders`, which take `config` and read
`training_prefixes` — leave those on the filing account, since they concern
training history rather than the inbox being scanned.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: everything green.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/cli.py src/mail_triage/model/classify.py tests/
git commit -m "feat: triage every configured source in one run"
```

---

### Task 10: Documentation, the real config, and the live checkpoint

**Files:**
- Modify: `README.md`, `CLAUDE.md`
- Modify: `local/config.toml` (gitignored — **user's real config, checkpoint**)

**Interfaces:**
- Consumes: everything.
- Produces: no code.

- [ ] **Step 1: Bring the CLI help up to date**

`--help` is the first place anyone looks, so it must not still describe a
single-account tool. Check every command's docstring and option help against
what the code now does:

- `cli` group docstring: "Local-first triage for Apple Mail." still holds.
- `triage` docstring says "Classify the inbox". It now classifies several,
  so reword to name the arrangement: every configured source's inbox, filed
  into the one filing account.
- `accounts` docstring should point out that the prefixes it lists are what
  `[[source]]` and `filing_account_prefix` want.
- `--source` help, added in Task 9.
- `undo --account` help, reworded in Task 9.
- `learn` docstring: confirm it still reads true given training stays on the
  filing account.

Verify by eye, not by assertion:

```bash
uv run mail-triage --help
uv run mail-triage triage --help
uv run mail-triage undo --help
uv run mail-triage accounts --help
```

Every line of output must match current behaviour. Fix any that does not.

- [ ] **Step 2: Update the docs**

In `README.md`, revise the configuration section for the sources shape, update
the "### Commands" section so each command's summary matches its `--help`
(`triage` in particular, and the removal of `--account` from it), and add
a short subsection explaining that Gmail inboxes are read from the `labels`
table and that Gmail mail is filed into the filing account, crossing accounts.

In `CLAUDE.md`, add to "Things learnt the hard way":

```markdown
- **A Gmail inbox is a label, not a mailbox.** Every Gmail message's
  `messages.mailbox` points at `[Gmail]/All Mail`; inbox membership lives in
  `labels(message_id, mailbox_id)`. Use `EnvelopeReader.inbox_messages`, which
  unions both. Joining `labels` to `messages` also drops stale rows for free.
- **`[Gmail]` is an fnmatch character class.** `[Gmail]*` matches `G/...` and
  misses every real Gmail folder. Write `[[]Gmail]*`.
```

Add `accounts.py` and the multi-source arrangement to the module table if the
table does not already describe it.

- [ ] **Step 3: Run the full suite and the personal-data guard**

Run: `uv run pytest -q`
Expected: all pass, including `tests/test_no_personal_data.py`.

- [ ] **Step 4: Commit the docs**

```bash
git add README.md CLAUDE.md
git commit -m "docs: describe triaging several accounts into one filing tree"
```

- [ ] **Step 5: CHECKPOINT — update the real config with the user**

`local/config.toml` is the user's live configuration and gitignored. Do not
edit it silently. Show the proposed contents, including the real account
prefixes from `mail-triage accounts`, and wait for approval.

- [ ] **Step 6: CHECKPOINT — dry run, then one message**

**Stop and get explicit approval before anything here moves mail.**

1. `uv run mail-triage triage --dry-run --no-ask` — confirm the Gmail inbox is
   found (it should report roughly 9 messages, not 0) and that proposals name
   iCloud folders.
2. `uv run mail-triage triage --source Gmail --limit 1` — one message only.
3. Verify in Mail: the message is in the proposed iCloud folder. **Check
   whether the Gmail copy remains in All Mail** — the spec asserts it does not,
   and this is the step that settles it. Record the answer in the commit
   message either way.
4. `uv run mail-triage undo <run-id>` — confirm the message returns to the
   Gmail inbox.

Only after this passes is a batch run appropriate.

---

## Self-Review

**Spec coverage.** Config sources → Task 1. `inbox_messages` via `labels` →
Task 2. The `[Gmail]` glob trap → Task 3. Per-source deletion evidence and
per-source binning → Tasks 4 and 6. Cross-account moves → Tasks 5 and 6.
Undo → Task 7. Account column → Task 8. Single-source parity → tested in
Tasks 1, 5, 7 and 8. The "does the Gmail copy stay in All Mail" risk → Task 10
Step 6. Out-of-scope items are absent, as intended.

**Not covered, deliberately.** The spec's follow-on task — extending
`tests/test_no_personal_data.py` to scan `tests/` and `docs/` — has no task
here. It is unrelated to making Gmail work and would be a separate branch.

**Type consistency.** `Source` fields (`name`, `prefix`, `inbox`, `trash`,
`ignore`) are used identically in Tasks 1, 4, 6 and 9. `build_deletion_index`
takes `(reader, config, source, now=None)` in Tasks 4 and 9.
`move_message`'s `source_account` keyword is defined in Task 5 and used in 6
and 7. `folder_message_ids(folder, account="*")` is defined in Task 5 and used
in 6 and 7. `render_table(proposals, accounts)` is defined in Task 8 and called
in Task 9. `execute(decisions, mail, journal, config, sources_by_prefix)` is
defined in Task 6 and called in Task 9.
