# Exchange as a Third Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Triage an Exchange mailbox as a third source, filing into the same tree as the other two.

**Architecture:** The multi-source work built for Gmail already generalises — no label indirection, a real `Inbox`, and `account_prefix` is scheme-agnostic. So this is one `[[source]]` block in the user's config plus one widened glob in `deletion.py`, with tests proving the generalisation rather than assuming it.

**Tech Stack:** Python 3.13, `uv`, `click`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-07-31-exchange-source-design.md`

## Global Constraints

- **British English** everywhere: code, comments, output, docs, commit messages.
- **Never write to Apple Mail's database.** Snapshot, read-only; writes go through AppleScript.
- **No test may touch a real mailbox or shell out to `osascript`.**
- **Nothing identifying in `src/`, `tests/` or `docs/`.** The account prefix is `ews://CCCCCCCC` in all committed material; the real one belongs only in gitignored `local/`. `tests/test_no_personal_data.py` enforces this against `local/identifying-terms.txt` — run it.
- **Never commit to `master` or to `publish-clean`.** This work is on `feature/outlook-source`.
- **Never verify with a live run.** The dry run in Task 2 moves nothing.
- **Precedence is unchanged.**
- Run the suite with `uv run pytest -q`. It must stay green: 537 tests pass before this plan starts.

---

### Task 1: Stop `Junk Email` counting as a filing

**Files:**
- Modify: `src/mail_triage/deletion.py`
- Test: `tests/test_deletion.py`

**Interfaces:**
- Consumes: `Source(name, prefix, inbox, trash, ignore)` and
  `build_deletion_index(reader, config, source, now=None)`, both unchanged.
- Produces: no API change. `_IGNORED_FOLDER_PATTERNS` gains a wildcard.

The shared ignore list has `"Junk"`, which does not match `Junk Email` — the
standard Exchange name. Every spam message therefore counts as evidence that
the user *files* that sender's mail, inflating the filed side of the deletion
ratio and suppressing the veto that catches senders whose mail is only ever
binned. That is a latent bug, not a quirk of one mailbox.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deletion.py`. `EXCHANGE` goes beside the existing `ICLOUD`
and `GMAIL` source constants near the top of the file:

```python
EXCHANGE = Source(
    name="Exchange", prefix="ews://CCCCCCCC", inbox="Inbox",
    trash="Deleted Items", ignore=["Conversation History"],
)
```

and these tests at the end:

```python
def test_junk_email_is_not_counted_as_a_filing(tmp_path):
    """"Junk Email" is Exchange's name for the junk folder.

    Counted as a filing, spam becomes evidence that the sender's mail gets
    kept — which is the exact opposite of what it means, and it suppresses
    the deletion veto.
    """
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk%20Email", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_plain_junk_is_still_not_counted_as_a_filing(tmp_path):
    """The widened pattern must not lose what the old one caught."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_a_folder_merely_starting_with_junk_is_still_ignored(tmp_path):
    """"Junk*" is deliberately broad: any junk-ish folder is not a filing."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Junk%20Mail", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_exchange_deleted_items_counts_as_a_deletion(tmp_path):
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Deleted%20Items", NOW - 100)]
    stats = build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (0, 1)


def test_a_source_ignore_entry_is_not_counted_as_a_filing(tmp_path):
    """Conversation History is written by the mail client, not by the user."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Conversation%20History", NOW - 100)]
    assert build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW) == {}


def test_a_real_exchange_folder_is_still_counted_as_a_filing(tmp_path):
    """The ignores must not swallow genuine filing decisions."""
    config = make_config(tmp_path)
    rows = [row("a@example.com", "ews://CCCCCCCC/Parent", NOW - 100)]
    stats = build_deletion_index(FakeReader(rows), config, EXCHANGE, now=NOW)["a@example.com"]
    assert (stats.filed, stats.deleted) == (1, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deletion.py -q`
Expected: `test_junk_email_is_not_counted_as_a_filing` and
`test_a_folder_merely_starting_with_junk_is_still_ignored` FAIL, because
`Junk Email` and `Junk Mail` are counted as filings. The other four pass
already — they document behaviour that must not regress.

- [ ] **Step 3: Widen the pattern**

In `src/mail_triage/deletion.py`, change the constant:

```python
# Folders that represent no filing decision at all: still in the inbox,
# junk, sent mail, drafts. Ignored entirely rather than counted as "filed".
# "Junk*" not "Junk": Exchange calls its junk folder "Junk Email", and the
# bare pattern missed it — so every spam message counted as evidence that
# the sender's mail gets filed, which is the opposite of what it means.
_IGNORED_FOLDER_PATTERNS = ["INBOX", "Junk*", "Spam", "Sent*", "Drafts*", "Outbox"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deletion.py -q` — expected: PASS.
Then `uv run pytest -q` — expected: all green, nothing else disturbed.

- [ ] **Step 5: Commit**

```bash
git add src/mail_triage/deletion.py tests/test_deletion.py
git commit -m "fix: Junk Email counted as a filing, suppressing the deletion veto"
```

---

### Task 2: Prove an `ews://` source works, and configure it

**Files:**
- Test: `tests/test_config.py`
- Test: `tests/test_cli.py`
- Modify: `config.example.toml`
- Modify: `local/config.toml` (gitignored — **user's real config, checkpoint**)

**Interfaces:**
- Consumes: `Config.source_for(prefix)`, `Source`, `EnvelopeReader.inbox_messages(url)`, all from the Gmail work and unchanged.
- Produces: no API change.

The spec claims `ews://` needs no special handling and that a config saying
`INBOX` finds a mailbox named `Inbox`. Both are load-bearing and neither is
currently tested, so this task tests them rather than trusting them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_a_non_imap_scheme_round_trips(tmp_path):
    """Exchange accounts are ews://; nothing may assume imap://."""
    path = _write(tmp_path, """
filing_account = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name = "Exchange"
prefix = "ews://CCCCCCCC"
inbox = "Inbox"
trash = "Deleted Items"
ignore = ["Conversation History"]
""")
    config = load_config(path)
    source = config.source_for("ews://CCCCCCCC")
    assert source is not None
    assert (source.name, source.inbox, source.trash) == ("Exchange", "Inbox", "Deleted Items")
    assert source.ignore == ["Conversation History"]
```

Add to `tests/test_config.py` as well, covering the prefix helper directly:

```python
def test_account_prefix_handles_any_scheme():
    from mail_triage.folders import account_prefix

    assert account_prefix("ews://CCCCCCCC-1111-2222/Inbox") == "ews://CCCCCCCC"
    assert account_prefix("imap://AAAAAAAA-1111-2222/INBOX") == "imap://AAAAAAAA"
    assert account_prefix("local://BBBBBBBB-1111-2222/Archive") == "local://BBBBBBBB"
```

Add to `tests/test_cli.py`, reusing the two-source helpers already there:

```python
def _three_source_config(tmp_path):
    from mail_triage.config import Source
    return Config(
        account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local",
        filing_account="iCloud", filing_account_prefix="imap://AAAAAAAA",
        sources=[
            Source(name="iCloud", prefix="imap://AAAAAAAA", inbox="INBOX",
                   trash="Deleted Messages"),
            Source(name="Gmail", prefix="imap://BBBBBBBB", inbox="INBOX",
                   trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]),
            # Config says INBOX; the mailbox is named Inbox. The lookup is
            # case-insensitive and this proves it.
            Source(name="Exchange", prefix="ews://CCCCCCCC", inbox="INBOX",
                   trash="Deleted Items", ignore=["Conversation History"]),
        ],
    )


def _prepare_three_sources(tmp_path, monkeypatch):
    import time

    from mail_triage.mail_app import FakeMail
    from tests.conftest import build_fixture_db

    now = int(time.time())
    day = 86_400
    rows = _strong_sender_rows(now, day)
    rows.append({
        "rowid": 700, "sender": "gmail-person@work.example", "subject": "Gmail message",
        "date_sent": now - day, "read": 1,
        "mailbox_url": "imap://BBBBBBBB/%5BGmail%5D/All%20Mail",
        "labels": ["imap://BBBBBBBB/INBOX"],
    })
    rows.append({
        "rowid": 800, "sender": "exchange-person@work.example",
        "subject": "Exchange message", "date_sent": now - day, "read": 1,
        "mailbox_url": "ews://CCCCCCCC/Inbox",
    })
    db_path = tmp_path / "Envelope Index"
    if db_path.exists():
        db_path.unlink()
    build_fixture_db(db_path, rows)
    monkeypatch.setattr(cli_module, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: _three_source_config(tmp_path))
    mail = FakeMail(
        inbox=[], mailboxes=["Projects", "Deleted Messages", "[Gmail]/Bin",
                             "Deleted Items", "INBOX", "Inbox"],
        accounts={"iCloud": {"INBOX": [900]}, "Gmail": {"INBOX": [700]},
                  "Exchange": {"Inbox": [800]}},
        keys={900: "<nine-hundred@work.example>", 700: "<seven-hundred@work.example>",
              800: "<eight-hundred@work.example>"},
        headers={
            900: {"List-Unsubscribe": "<mailto:x@work.example>"},
            700: {"List-Unsubscribe": "<mailto:y@work.example>"},
            800: {"List-Unsubscribe": "<mailto:z@work.example>"},
        },
    )
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    runner = CliRunner()
    assert runner.invoke(cli, ["learn", "--no-drift"]).exit_code == 0
    return runner, mail


def test_a_dry_run_scans_all_three_inboxes(tmp_path, monkeypatch):
    runner, mail = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    assert result.exit_code == 0, result.output
    assert " of 3 would be filed" in result.output
    assert "Dry run — nothing was moved." in result.output
    assert mail.moved == []


def test_an_exchange_inbox_named_Inbox_is_found_by_a_config_saying_INBOX(tmp_path, monkeypatch):
    """Case-insensitive inbox lookup — the spec depends on it."""
    runner, _ = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--source", "Exchange", "--dry-run", "--no-ask"])
    assert result.exit_code == 0, result.output
    assert " of 1 would be filed" in result.output


def test_the_exchange_row_names_its_own_account(tmp_path, monkeypatch):
    runner, _ = _prepare_three_sources(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["triage", "--dry-run", "--no-ask"])
    row = next(line for line in result.output.splitlines() if "Exchange message" in line)
    assert row.startswith("Exchange")
    assert "Projects" in row
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_cli.py -q`
Expected: the new tests FAIL — `_three_source_config` and
`_prepare_three_sources` do not exist yet, so collection errors on the CLI
file, and the config tests fail on the missing source.

If instead any of them passes immediately, stop and check the test is not
vacuous before continuing — an assertion that never exercised an `ews://`
source proves nothing.

- [ ] **Step 3: No implementation needed — confirm that**

There is deliberately no production change in this task. If the tests pass
once the helpers exist, the spec's claim is proven: `account_prefix` is
scheme-agnostic and the inbox lookup is case-insensitive.

If any test fails for a reason other than a missing helper, that is a real
gap the spec missed. Stop and report it rather than adjusting the assertion.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q` — expected: all green.

- [ ] **Step 5: Document the shape in the example config**

Append to `config.example.toml`, after the existing `[[source]]` blocks:

```toml
# An Exchange/Outlook account. The scheme is ews:// rather than imap://,
# which needs no special handling. Its bin is "Deleted Items" and its junk
# folder is "Junk Email" (caught by the shared "Junk*" pattern).
# "Conversation History" is written by the mail client itself, so it is
# ignored rather than read as a filing decision.
[[source]]
name   = "Exchange"
prefix = "ews://CCCCCCCC"
inbox  = "Inbox"
trash  = "Deleted Items"
ignore = ["Conversation History"]
```

Then confirm the example still loads:

```bash
uv run python -c "
from pathlib import Path
from mail_triage.config import load_config
c = load_config(Path('config.example.toml'))
print([s.name for s in c.sources])
"
```

Expected: `['iCloud', 'Gmail', 'Exchange']`.

- [ ] **Step 6: Run the personal-data guard**

Run: `uv run pytest tests/test_no_personal_data.py -q`
Expected: all pass. The real prefix must appear nowhere outside `local/`.

- [ ] **Step 7: Commit**

```bash
git add tests/test_config.py tests/test_cli.py config.example.toml
git commit -m "test: prove an ews:// source triages like any other"
```

- [ ] **Step 8: CHECKPOINT — add the real source to the user's config**

`local/config.toml` is the user's live configuration and gitignored. Do not
edit it silently. Show the proposed block, with the real prefix from
`mail-triage accounts`, and wait for approval.

- [ ] **Step 9: CHECKPOINT — dry run across all three accounts**

**Read-only. Moves nothing.**

```bash
uv run mail-triage triage --dry-run --no-ask
```

Confirm the Exchange inbox is found (roughly 10 messages, not 0), that the
Account column names three accounts, and that nothing moved.

**Then stop.** Per the spec's deferred section, this account's vendor and rail
mail will be proposed into the shared tree — the opposite of what the user
wants for it. Report which proposals fall into that category rather than
suggesting a live run.

---

## Self-Review

**Spec coverage.** The `Junk*` fix and its measured table → Task 1. The
config block → Task 2 Steps 5 and 8. The scheme-agnostic and case-insensitive
claims → Task 2 Steps 1–4. The dry run across three accounts → Task 2 Step 9.
The deferred keep-local requirement is explicitly out of scope in both spec
and plan, and Task 2 Step 9 names the resulting interim risk rather than
hiding it.

**Not covered, deliberately.** Adding `TheKey` and `CIISEC` to
`local/identifying-terms.txt` was done during the design work, not deferred
to this plan; the guard run in Task 2 Step 6 confirms it holds.

**Type consistency.** `Source(name, prefix, inbox, trash, ignore)` is used
identically in both tasks and matches `src/mail_triage/config.py`.
`build_deletion_index(reader, config, source, now=None)` matches
`src/mail_triage/deletion.py`. `EXCHANGE` is defined once, in Task 1, beside
the existing `ICLOUD` and `GMAIL` constants. The prefix `ews://CCCCCCCC` is
the same in every task and in `config.example.toml`.
