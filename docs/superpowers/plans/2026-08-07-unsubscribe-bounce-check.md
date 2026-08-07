# Unsubscribe bounce check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every unsubscribe request that is actually sent, then find and report the bounces that say it was rejected.

**Architecture:** Two new modules. `sends.py` writes an append-only JSONL record per send batch under `local/unsubscribe-sends/`. `bounces.py` holds pure functions — no I/O, no config, no AppleScript — that narrow an inbox to bounce-shaped messages, confirm them as delivery reports from their headers, attribute them to requests on exact matches only, and render the report. `cli.py` supplies the I/O around them.

**Tech Stack:** Python 3.13, `click`, `uv`, `pytest`. Standard library only for the new modules.

**Spec:** `docs/superpowers/specs/2026-08-07-unsubscribe-bounce-design.md`

## Global Constraints

- **British English everywhere** — code, comments, CLI output, docs, commit messages.
- **No test touches a real mailbox or shells out to `osascript`.** Use `FakeMail` and synthetic fixtures.
- **Nothing personal in the repository.** No real addresses, account UUIDs, folder names or subject lines in `src/`, `tests/` or `docs/`. Run `uv run pytest tests/test_no_personal_data.py` before every commit — it rejects any address at a domain that is not `.example` or allow-listed.
- **Never write to Apple Mail's database.** Reads come from a snapshot; the only writes are AppleScript.
- Run everything through `uv run`.
- Work on branch `feature/unsubscribe-bounce-check` (already created; the spec is committed there).
- Full suite must pass before each commit: `uv run pytest -q`.

## File structure

| File | Responsibility |
|---|---|
| `src/mail_triage/sends.py` | **New.** The record of what was sent. `SentRequest`, batch ids, append and load. |
| `src/mail_triage/bounces.py` | **New.** Identify bounce-shaped messages, confirm them as delivery reports, attribute them to requests, render the report. Pure functions throughout. |
| `src/mail_triage/envelope.py` | Add `date_received` to `MessageRow` and `_BASE_QUERY`. |
| `src/mail_triage/mail_app.py` | `send_mail` returns the account it sent from. Protocol and `FakeMail` follow. |
| `src/mail_triage/unsubscribe.py` | `send_unsubscribe` returns a `SentRequest`. Promote `_folder_url` to `folder_url`. |
| `src/mail_triage/cli.py` | Write the batch, take one free look, add `--check`. |
| `tests/conftest.py` | Fixture builder gains a `date_received` column. |
| `tests/test_sends.py` | **New.** Round-trip the send log. |
| `tests/test_bounces.py` | **New.** The matching rules — the safety-critical tests. |
| `tests/test_cli_unsubscribe.py` | Extend: the batch is written, `--check` reports. |

**Note on the spec's module table:** the spec describes `bounces.py` as "identify and attribute". This plan also puts the report rendering there, rather than adding a third module for one pure function. Task 6 updates the spec's line to match.

---

### Task 1: `date_received` on `MessageRow`

The bounce window must be measured on our clock. `date_sent` belongs to the bouncing daemon, and a relay with a skewed clock would put its bounce outside any window computed from it.

**Files:**
- Modify: `src/mail_triage/envelope.py:19-46` (`_BASE_QUERY`, `MessageRow`), `:127-139` (`_rows`)
- Modify: `tests/conftest.py:29-69` (schema and insert)
- Test: `tests/test_envelope.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MessageRow.date_received: int` — epoch seconds, `0` when the column is null. Fixture rows accept an optional `date_received` key defaulting to the row's `date_sent`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_envelope.py`:

```python
def test_message_row_carries_date_received(tmp_path):
    """The bounce window is measured on our clock, not the sender's."""
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(
        db_path,
        [
            {
                "sender": "someone@work.example",
                "subject": "Hello",
                "date_sent": 1_700_000_000,
                "date_received": 1_700_000_900,
                "mailbox_url": "imap://AAAAAAAA/INBOX",
                "read": 0,
            }
        ],
    )
    reader = EnvelopeReader(db_path)
    try:
        row = next(iter(reader.all_messages()))
    finally:
        reader.close()
    assert row.date_sent == 1_700_000_000
    assert row.date_received == 1_700_000_900


def test_date_received_defaults_to_date_sent_in_fixtures(tmp_path):
    """Every existing fixture omits it; they must keep working."""
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(
        db_path,
        [
            {
                "sender": "someone@work.example",
                "subject": "Hello",
                "date_sent": 1_700_000_000,
                "mailbox_url": "imap://AAAAAAAA/INBOX",
                "read": 0,
            }
        ],
    )
    reader = EnvelopeReader(db_path)
    try:
        row = next(iter(reader.all_messages()))
    finally:
        reader.close()
    assert row.date_received == 1_700_000_000
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_envelope.py -q -k date_received`
Expected: FAIL — `TypeError: build_fixture_db() ... unexpected` or `AttributeError: 'MessageRow' object has no attribute 'date_received'`.

- [ ] **Step 3: Add the column to the fixture builder**

In `tests/conftest.py`, add `date_received INTEGER,` to the `messages` table, immediately after `date_sent INTEGER,`:

```python
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            sender INTEGER, subject INTEGER NOT NULL,
            date_sent INTEGER, date_received INTEGER, mailbox INTEGER NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            flagged INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0
        );
```

and change the insert to carry it, defaulting to `date_sent` so every fixture written before this task keeps working:

```python
        db.execute(
            "INSERT INTO messages (ROWID, sender, subject, date_sent, date_received, "
            "mailbox, read, flagged, size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("rowid", index),
                intern("addresses", "address", addresses, row["sender"]),
                intern("subjects", "subject", subjects, row["subject"]),
                row["date_sent"],
                row.get("date_received", row["date_sent"]),
                intern("mailboxes", "url", mailboxes, row["mailbox_url"]),
                int(row.get("read", 0)),
                int(row.get("flagged", 0)),
                int(row.get("size", 0)),
            ),
        )
```

Update the docstring's second line to read:

```
    Each row dict needs: sender, subject, date_sent, mailbox_url, read.
    ``date_received`` is optional and defaults to ``date_sent``; ``flagged`` is
    optional and defaults to 0. ``labels`` is an optional list
```

- [ ] **Step 4: Add the field to `MessageRow` and the query**

In `src/mail_triage/envelope.py`, change `_BASE_QUERY`'s select list:

```python
_BASE_QUERY = """
    SELECT m.ROWID, a.address, s.subject, m.date_sent, m.date_received, b.url, m.read, m.flagged
    FROM messages m
    JOIN addresses a ON a.ROWID = m.sender
    JOIN subjects s ON s.ROWID = m.subject
    JOIN mailboxes b ON b.ROWID = m.mailbox
"""
```

Add the field to `MessageRow`, after `date_sent`:

```python
    rowid: int
    sender: str
    subject: str
    date_sent: int
    mailbox_url: str
    read: bool
    flagged: bool = False
    # When the message reached *us*. ``date_sent`` is the sending machine's
    # clock, which is fine for ordering a correspondent's mail but not for
    # "did this arrive after we sent that": a bouncing relay with a skewed
    # clock would fall outside any window computed from it. Defaulted so
    # callers constructing rows by hand (the tests do) need not supply it.
    date_received: int = 0
```

and unpack it in `_rows`:

```python
    def _rows(self, where: str = "", params: tuple = ()) -> Iterator[MessageRow]:
        for (
            rowid, sender, subject, date_sent, date_received, url, read, flagged
        ) in self.connection.execute(_BASE_QUERY + where, params):
            yield MessageRow(
                rowid=rowid,
                sender=sender,
                subject=subject or "",
                date_sent=date_sent or 0,
                mailbox_url=url,
                read=bool(read),
                flagged=bool(flagged),
                date_received=date_received or 0,
            )
```

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, including the two new tests. If anything else fails, a fixture is constructing `MessageRow` positionally — fix that call site to use keywords.

- [ ] **Step 6: Commit**

```bash
uv run pytest tests/test_no_personal_data.py -q
git add src/mail_triage/envelope.py tests/conftest.py tests/test_envelope.py
git commit -m "feat: MessageRow carries date_received

The bounce window has to be measured on our clock. date_sent is the
sending machine's, and a relay with a skewed clock would put its bounce
outside any window computed from it."
```

---

### Task 2: The send log

There is currently no record that anything was ever sent. This is that record.

**Files:**
- Create: `src/mail_triage/sends.py`
- Test: `tests/test_sends.py`

**Interfaces:**
- Consumes: `Config.local_dir` (a `Path`).
- Produces:
  - `SentRequest(sender: str, to_address: str, subject: str, sent_at: int, from_account: str)` — frozen dataclass.
  - `sends_dir(config: Config) -> Path`
  - `new_batch_id() -> str` — `"%Y-%m-%dT%H-%M-%S"` local time, same shape as `journal.new_run_id`.
  - `record_send(config: Config, batch_id: str, request: SentRequest) -> None`
  - `list_batches(config: Config) -> list[str]` — newest first, `[]` when none.
  - `load_batch(config: Config, batch_id: str) -> list[SentRequest]` — in send order; unparseable lines skipped with a warning.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sends.py`:

```python
"""The record of what was actually sent."""

from __future__ import annotations

import warnings

import pytest

from mail_triage.sends import (
    SentRequest,
    list_batches,
    load_batch,
    new_batch_id,
    record_send,
    sends_dir,
)

from tests.cli_helpers import stub_config


def _request(**overrides) -> SentRequest:
    values = dict(
        sender="news@list.example",
        to_address="leave@list.example",
        subject="token-abc12345",
        sent_at=1_700_000_000,
        from_account="iCloud",
    )
    values.update(overrides)
    return SentRequest(**values)


def test_a_recorded_send_comes_back_unchanged(tmp_path):
    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", _request())
    assert load_batch(config, "2026-08-07T10-00-00") == [_request()]


def test_sends_are_appended_in_order(tmp_path):
    config = stub_config(tmp_path)
    first = _request(sender="a@x.example")
    second = _request(sender="b@y.example")
    record_send(config, "batch", first)
    record_send(config, "batch", second)
    assert load_batch(config, "batch") == [first, second]


def test_batches_list_newest_first(tmp_path):
    config = stub_config(tmp_path)
    record_send(config, "2026-08-05T09-00-00", _request())
    record_send(config, "2026-08-07T10-00-00", _request())
    record_send(config, "2026-08-06T11-00-00", _request())
    assert list_batches(config) == [
        "2026-08-07T10-00-00",
        "2026-08-06T11-00-00",
        "2026-08-05T09-00-00",
    ]


def test_no_batches_is_an_empty_list_not_an_error(tmp_path):
    assert list_batches(stub_config(tmp_path)) == []


def test_a_corrupt_line_is_skipped_and_the_rest_survive(tmp_path):
    """A killed process can leave one truncated line. It must not cost the batch."""
    config = stub_config(tmp_path)
    record_send(config, "batch", _request(sender="a@x.example"))
    path = sends_dir(config) / "batch.jsonl"
    with path.open("a") as handle:
        handle.write('{"sender": "b@y.example", "to_addr\n')
    record_send(config, "batch", _request(sender="c@z.example"))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = load_batch(config, "batch")
    assert [request.sender for request in loaded] == ["a@x.example", "c@z.example"]
    assert len(caught) == 1


def test_batch_ids_sort_lexicographically_by_time():
    assert new_batch_id() > "2020-01-01T00-00-00"


def test_loading_a_batch_that_does_not_exist_is_empty(tmp_path):
    assert load_batch(stub_config(tmp_path), "nope") == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sends.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.sends'`.

- [ ] **Step 3: Implement `sends.py`**

```python
"""What was actually sent, so that a bounce can be matched against it.

Until this existed, ``send_unsubscribe`` fired and returned ``None``: the
tool printed "sent" and kept no record at all, so there was nothing a bounce
could be checked against. A batch of ten could report a perfect score with
all ten rejected.

**A send is recorded after it succeeds, not before.** That inverts the run
journal's record-then-act discipline, deliberately. The journal records
intent first because an interrupted batch must still be reversible, and a
move that never happened is harmless to attempt to undo. Here the risk runs
the other way: a record written before the send describes a request that
might never have gone out, and the bounce check would then find no bounce
for it and report it as fine — recreating, in a new place, the exact false
clean bill of health this whole feature exists to abolish. Losing a record
to a crash between the send and the write merely returns that one request to
the old behaviour.

One file per batch, mirroring the journal's convention, so "the last batch"
is simply the newest filename.
"""

from __future__ import annotations

import json
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from mail_triage.config import Config

SENDS_DIRNAME = "unsubscribe-sends"


@dataclass(frozen=True)
class SentRequest:
    """One unsubscribe request that definitely left the machine."""

    sender: str
    to_address: str
    subject: str
    sent_at: int
    # The account Mail actually sent from, captured rather than assumed:
    # ``send_mail`` uses Mail's default account, which need not be any
    # configured source. The bounce comes back to this account's inbox, so
    # guessing here means searching the wrong mailbox and reporting a clean
    # run that never happened.
    from_account: str


def sends_dir(config: Config) -> Path:
    return config.local_dir / SENDS_DIRNAME


def new_batch_id() -> str:
    """A batch id that is both unique enough and sortable by time."""
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())


def _batch_path(config: Config, batch_id: str) -> Path:
    return sends_dir(config) / f"{batch_id}.jsonl"


def record_send(config: Config, batch_id: str, request: SentRequest) -> None:
    """Append one sent request. Call only after the send has succeeded."""
    directory = sends_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    with _batch_path(config, batch_id).open("a") as handle:
        handle.write(json.dumps(asdict(request)) + "\n")


def list_batches(config: Config) -> list[str]:
    """Batch ids, newest first. The ids sort by time, so this is a sort."""
    directory = sends_dir(config)
    if not directory.is_dir():
        return []
    return sorted((path.stem for path in directory.glob("*.jsonl")), reverse=True)


def load_batch(config: Config, batch_id: str) -> list[SentRequest]:
    """Every request in a batch, in send order.

    A line that will not parse is skipped with a warning rather than taken as
    the end of the file: only the most recent write can be truncated, and the
    entries either side of it are complete and worth keeping.
    """
    path = _batch_path(config, batch_id)
    if not path.exists():
        return []
    requests: list[SentRequest] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            requests.append(SentRequest(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            warnings.warn(f"Skipping unreadable line in send log {path.name}", stacklevel=2)
    return requests
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_sends.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
uv run pytest -q && uv run pytest tests/test_no_personal_data.py -q
git add src/mail_triage/sends.py tests/test_sends.py
git commit -m "feat: record every unsubscribe request that is sent

Recorded after the send, not before: a record written first would describe
a request that might never have gone out, and the bounce check would then
call it fine — the same false clean bill of health, in a new place."
```

---

### Task 3: Capture the sending account, and return a record

`send_mail` sends from Mail's default account, which need not be a configured source. The check has to know which one it was.

**Files:**
- Modify: `src/mail_triage/mail_app.py:46-62` (`MailInterface`), `:322-364` (`_send_script`, `send_mail`), `:519-522` (`FakeMail.send_mail`)
- Modify: `src/mail_triage/unsubscribe.py:358-366` (`send_unsubscribe`), `:165-169` (`_folder_url` → `folder_url`)
- Test: `tests/test_mail_app.py`, `tests/test_unsubscribe.py`

**Interfaces:**
- Consumes: `SentRequest` from Task 2.
- Produces:
  - `MailInterface.send_mail(to_address: str, subject: str, body: str) -> str` — returns the account name, `""` when it cannot be determined.
  - `unsubscribe.send_unsubscribe(option: UnsubscribeOption, mail: MailInterface, now: int | None = None) -> SentRequest`
  - `unsubscribe.folder_url(reader, source: Source, folder: str) -> str | None` — the old `_folder_url`, made public.
  - `FakeMail.__init__` gains `sending_account: str = "iCloud"`; `FakeMail.sent` entries stay `(to_address, subject)` tuples.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mail_app.py`:

```python
def test_send_script_asks_which_account_it_sent_from():
    """The bounce comes back to the sending account, so we must know it."""
    script = AppleScriptMail()._send_script("leave@list.example", "token", "unsubscribe")
    assert "send newMessage" in script
    assert "email addresses of acct" in script
    # The account must be read before the compose object is discarded.
    assert script.index("email addresses of acct") < script.index("delete newMessage")


def test_fake_mail_reports_its_sending_account():
    mail = FakeMail(inbox=[], mailboxes=[], sending_account="Gmail")
    assert mail.send_mail("leave@list.example", "token", "unsubscribe") == "Gmail"
```

Add to `tests/test_unsubscribe.py`:

```python
def test_send_unsubscribe_returns_a_record_of_what_went_out():
    option = UnsubscribeOption(
        sender="news@list.example",
        domain="list.example",
        method="mailto",
        target="leave@list.example",
        message_count=1,
        unread_count=1,
        subject="token-abc12345",
        body="unsubscribe",
    )
    mail = FakeMail(inbox=[], mailboxes=[], sending_account="iCloud")
    record = send_unsubscribe(option, mail, now=1_700_000_000)
    assert record == SentRequest(
        sender="news@list.example",
        to_address="leave@list.example",
        subject="token-abc12345",
        sent_at=1_700_000_000,
        from_account="iCloud",
    )


def test_refusing_to_send_records_nothing():
    """A request that never went out must not appear in the log."""
    option = UnsubscribeOption(
        sender="news@list.example",
        domain="list.example",
        method="http",
        target="https://list.example/unsub",
        message_count=1,
        unread_count=1,
    )
    with pytest.raises(ValueError):
        send_unsubscribe(option, FakeMail(inbox=[], mailboxes=[]))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_mail_app.py tests/test_unsubscribe.py -q`
Expected: FAIL — `TypeError: FakeMail.__init__() got an unexpected keyword argument 'sending_account'` and `AssertionError` on the script.

- [ ] **Step 3: Return the account from the AppleScript bridge**

In `src/mail_triage/mail_app.py`, update the protocol:

```python
    def send_mail(self, to_address: str, subject: str, body: str) -> str: ...
```

Extend `_send_script` — keep the existing docstring, and append this paragraph to it:

```
        The account is resolved by matching the outgoing message's ``sender``
        against each account's ``email addresses``. Mail has no "default
        account" property worth reading, and the account is needed because
        the bounce comes back to *its* inbox. Read before ``delete``, which
        discards the compose object. An unmatched sender yields "", which the
        caller reports rather than guessing at.
```

and change the returned script to:

```python
        return (
            'tell application "Mail"\n'
            "  set newMessage to make new outgoing message with properties "
            f'{{subject:"{_escape_applescript_string(subject)}", '
            f'content:"{_escape_applescript_string(body)}", visible:false}}\n'
            "  tell newMessage\n"
            "    make new to recipient at end of to recipients with properties "
            f'{{address:"{_escape_applescript_string(to_address)}"}}\n'
            "  end tell\n"
            "  send newMessage\n"
            "  set senderValue to sender of newMessage as string\n"
            '  set accountName to ""\n'
            "  repeat with acct in accounts\n"
            "    repeat with addr in email addresses of acct\n"
            "      if senderValue contains (addr as string) then\n"
            "        set accountName to name of acct as string\n"
            "        exit repeat\n"
            "      end if\n"
            "    end repeat\n"
            '    if accountName is not "" then exit repeat\n'
            "  end repeat\n"
            "  delete newMessage\n"
            "  return accountName\n"
            "end tell"
        )
```

Change `send_mail` to return it:

```python
    def send_mail(self, to_address: str, subject: str, body: str) -> str:
        """Send a message from Mail's default account; return that account's name.

        The only method in mail-triage that sends anything. Callers must have
        an explicit per-message confirmation in hand before calling it.

        Returns "" if the sending account could not be identified, which the
        caller reports honestly rather than substituting a guess.
        """
        return _run(self._send_script(to_address, subject, body)).strip()
```

- [ ] **Step 4: Teach `FakeMail` the same**

```python
    def send_mail(self, to_address: str, subject: str, body: str) -> str:
        # Body deliberately not recorded: what matters to a test is that
        # exactly one message went to exactly one address.
        self.sent.append((to_address, subject))
        return self._sending_account
```

and in `FakeMail.__init__`, add the parameter after `accounts` and store it:

```python
        accounts: dict[str, dict[str, list[int]]] | None = None,
        sending_account: str = "iCloud",
    ) -> None:
```

```python
        # Which account send_mail reports having sent from. Mail's default
        # account is not necessarily one this tool triages, and the bounce
        # check depends on knowing which it was.
        self._sending_account = sending_account
```

- [ ] **Step 5: Return a record from `send_unsubscribe`**

In `src/mail_triage/unsubscribe.py`, add `import time` is already present; add the import:

```python
from mail_triage.sends import SentRequest
```

Rename `_folder_url` to `folder_url` (drop the underscore), update its two call sites inside `find_candidates`, and give it a docstring:

```python
def folder_url(reader, source: Source, folder: str) -> str | None:
    """The mailbox URL for one of a source's folders, or None if absent."""
    for url in reader.mailbox_urls():
        if url.startswith(source.prefix) and folder_path(url).casefold() == folder.casefold():
            return url
    return None
```

Replace `send_unsubscribe`:

```python
def send_unsubscribe(
    option: UnsubscribeOption, mail: MailInterface, now: int | None = None
) -> SentRequest:
    """Send the unsubscribe request, and describe what went out.

    Only mailto targets are supported. The returned record is what the bounce
    check matches against later; it is returned rather than written here so
    that this function stays the one that sends and nothing else.
    """
    if option.method != "mailto":
        raise ValueError(
            f"Cannot send to a {option.method} target; only mailto unsubscribe is supported."
        )
    if not _VALID_ADDRESS.match(option.target):
        raise ValueError(f"Refusing to send: {option.target!r} is not an email address.")
    from_account = mail.send_mail(option.target, option.subject, option.body)
    return SentRequest(
        sender=option.sender,
        to_address=option.target,
        subject=option.subject,
        sent_at=int(time.time()) if now is None else now,
        from_account=from_account,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mail_app.py tests/test_unsubscribe.py -q`
Expected: PASS. Then `uv run pytest -q` — `tests/test_cli_unsubscribe.py` should still pass, because `cli.py` discards the return value until Task 6.

- [ ] **Step 7: Commit**

```bash
uv run pytest -q && uv run pytest tests/test_no_personal_data.py -q
git add src/mail_triage/mail_app.py src/mail_triage/unsubscribe.py tests/test_mail_app.py tests/test_unsubscribe.py
git commit -m "feat: capture which account an unsubscribe was sent from

Mail sends from its default account, which need not be a configured source.
The bounce returns to that account's inbox, so assuming otherwise means
searching the wrong mailbox and reporting a clean run that never happened."
```

---

### Task 4: Identifying and attributing bounces

The safety-critical task. Every rule here is an exact match against a string we generated ourselves; anything else is reported as unattributed rather than guessed at.

**Files:**
- Create: `src/mail_triage/bounces.py`
- Test: `tests/test_bounces.py`

**Interfaces:**
- Consumes: `MessageRow` (with `date_received`, Task 1), `SentRequest` (Task 2).
- Produces:
  - `Bounce(rowid: int, subject: str, received_at: int, failed_recipient: str | None = None, request: SentRequest | None = None)`
  - `is_bounce_sender(sender: str) -> bool`
  - `candidate_rows(messages: Iterable[MessageRow], batch: list[SentRequest]) -> list[MessageRow]`
  - `is_delivery_report(headers: Mapping[str, str]) -> bool`
  - `failed_recipients(headers: Mapping[str, str]) -> list[str]`
  - `attribute(pairs: list[tuple[MessageRow, Mapping[str, str]]], batch: list[SentRequest]) -> list[Bounce]`
  - Constants `SKEW_SECONDS = 300`, `WINDOW_SECONDS = 86_400`, `DEFAULT_SUBJECT = "unsubscribe"`, `MIN_TOKEN_LENGTH = 8`, `BOUNCE_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster"})`

**Note on `MIN_TOKEN_LENGTH`:** the spec's rule 2 excludes only the literal word `unsubscribe`. This plan also requires a token of at least 8 characters, because a three-character subject would be an unsafe substring test for exactly the reason the word `unsubscribe` is. It is a strengthening in the direction the spec argues for, not a departure from it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bounces.py`:

```python
"""Finding bounces, and tying them to the request that caused them.

Every rule here is an exact match against something the tool generated
itself. The alternative — a fuzzy match on subject or sender — is what the
plan for this task warned against by name: a rule that quietly matches the
wrong message reports a bounce for a request that was fine, which is worse
than reporting nothing at all.
"""

from __future__ import annotations

from mail_triage.bounces import (
    Bounce,
    attribute,
    candidate_rows,
    failed_recipients,
    is_bounce_sender,
    is_delivery_report,
)
from mail_triage.envelope import MessageRow
from mail_triage.sends import SentRequest

SENT_AT = 1_700_000_000
DSN_HEADERS = {"Content-Type": 'multipart/report; report-type=delivery-status; boundary="x"'}


def _request(**overrides) -> SentRequest:
    values = dict(
        sender="news@list.example",
        to_address="leave@list.example",
        subject="token-abc12345",
        sent_at=SENT_AT,
        from_account="iCloud",
    )
    values.update(overrides)
    return SentRequest(**values)


def _row(rowid=1, sender="MAILER-DAEMON@relay.example", subject="Delivery Status Notification (Failure)",
         received_at=SENT_AT + 20) -> MessageRow:
    return MessageRow(
        rowid=rowid,
        sender=sender,
        subject=subject,
        date_sent=received_at,
        mailbox_url="imap://AAAAAAAA/INBOX",
        read=False,
        date_received=received_at,
    )


# --- phase 1: narrowing the inbox without any round trips ---------------------

def test_daemon_local_parts_are_recognised_whatever_the_domain():
    assert is_bounce_sender("MAILER-DAEMON@relay.example")
    assert is_bounce_sender("postmaster@some.other.example")
    assert is_bounce_sender("Mailer-Daemon@x.example")


def test_an_ordinary_sender_is_not_a_bounce():
    assert not is_bounce_sender("news@list.example")
    assert not is_bounce_sender("mailer-daemon-news@list.example")


def test_only_messages_inside_the_window_are_candidates():
    batch = [_request()]
    too_early = _row(rowid=1, received_at=SENT_AT - 600)
    just_early_enough = _row(rowid=2, received_at=SENT_AT - 60)
    prompt = _row(rowid=3, received_at=SENT_AT + 20)
    too_late = _row(rowid=4, received_at=SENT_AT + 90_000)
    rows = candidate_rows([too_early, just_early_enough, prompt, too_late], batch)
    assert [row.rowid for row in rows] == [2, 3]


def test_an_ordinary_message_in_the_window_is_not_a_candidate():
    batch = [_request()]
    rows = candidate_rows([_row(sender="news@list.example")], batch)
    assert rows == []


def test_no_batch_means_no_candidates():
    assert candidate_rows([_row()], []) == []


# --- phase 2: confirming it really is a delivery report -----------------------

def test_a_multipart_report_is_a_delivery_report():
    assert is_delivery_report(DSN_HEADERS)


def test_an_auto_replied_message_is_a_delivery_report():
    assert is_delivery_report({"Auto-Submitted": "auto-replied"})


def test_a_human_called_postmaster_is_not_a_delivery_report():
    assert not is_delivery_report({"Content-Type": "text/plain; charset=utf-8"})


def test_delivery_report_detection_ignores_header_case():
    assert is_delivery_report({"content-type": "multipart/report; report-type=delivery-status"})


# --- attribution --------------------------------------------------------------

def test_an_exact_failed_recipient_attributes_to_its_request():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    [bounce] = attribute([(_row(), headers)], [request])
    assert bounce.request == request
    assert bounce.failed_recipient == "leave@list.example"


def test_failed_recipient_matching_ignores_case_and_spacing():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "other@x.example, LEAVE@List.Example "}
    [bounce] = attribute([(_row(), headers)], [request])
    assert bounce.request == request


def test_a_distinctive_subject_token_attributes_when_no_header_carries_it():
    request = _request()
    row = _row(subject="Undeliverable: token-abc12345")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request == request


def test_the_word_unsubscribe_never_attributes_by_subject():
    """The dangerous case: it is the default subject and appears everywhere."""
    request = _request(subject="unsubscribe")
    row = _row(subject="Re: unsubscribe from our newsletter")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request is None


def test_a_short_token_never_attributes_by_subject():
    request = _request(subject="stop")
    row = _row(subject="Undeliverable: please stop the presses")
    [bounce] = attribute([(row, DSN_HEADERS)], [request])
    assert bounce.request is None


def test_a_bounce_for_an_address_nobody_wrote_to_is_unattributed():
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "someone@else.example"}
    [bounce] = attribute([(_row(), headers)], [_request()])
    assert bounce.request is None
    assert bounce.failed_recipient == "someone@else.example"


def test_one_bounce_never_claims_two_requests():
    """Two lists can share an unsubscribe address."""
    first = _request(sender="a@list.example", sent_at=SENT_AT)
    second = _request(sender="b@list.example", sent_at=SENT_AT + 1)
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    bounces = attribute([(_row(rowid=1), headers), (_row(rowid=2), headers)], [first, second])
    assert [bounce.request for bounce in bounces] == [first, second]


def test_a_second_bounce_with_no_request_left_is_unattributed():
    request = _request()
    headers = {**DSN_HEADERS, "X-Failed-Recipients": "leave@list.example"}
    bounces = attribute([(_row(rowid=1), headers), (_row(rowid=2), headers)], [request])
    assert bounces[0].request == request
    assert bounces[1].request is None


def test_a_message_that_is_not_a_delivery_report_is_dropped_entirely():
    headers = {"Content-Type": "text/plain", "X-Failed-Recipients": "leave@list.example"}
    assert attribute([(_row(), headers)], [_request()]) == []


def test_failed_recipients_handles_an_absent_header():
    assert failed_recipients({}) == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_bounces.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail_triage.bounces'`.

- [ ] **Step 3: Implement `bounces.py`**

```python
"""Did the unsubscribe request actually land? Usually you cannot tell. Sometimes you can.

The first live send (6 August 2026) reported "sent" and was rejected 18
seconds later — ``554 Message rejected: The unsubscribe request has invalid
form`` — as a bounce from ``mailer-daemon`` that nothing was watching for.
This module is what watches.

**Every rule here is an exact match against a string the tool generated
itself**: a recipient address it wrote to, or a subject token it sent. That
constraint is the whole design. A fuzzy match — nearest subject, nearest
time — would attribute a bounce to a request that was fine, which is a
worse outcome than the silence it replaces, because it is silence that
sounds like information.

What this module deliberately cannot tell you is *why* a request bounced.
The SMTP diagnostic lives in the delivery report's ``message/delivery-status``
body part, and mail-triage does not read message bodies. It reports which
request bounced and leaves the reason to be read in Mail.

Nothing here does any I/O: it takes rows and header dictionaries and returns
values. That is what lets the matching rules — the part where a mistake is
expensive — be tested exhaustively without a mailbox anywhere near them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mail_triage.envelope import MessageRow
from mail_triage.sends import SentRequest

# Bounces arrive from MAILER-DAEMON@whatever-relay, and the domain is
# unpredictable whilst the local part has been fixed by convention for
# decades. Matching the local part alone is therefore both wider and safer
# than any domain rule would be.
BOUNCE_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster"})

# The window either side of the batch. Five minutes early allows for a relay
# whose clock is behind ours; a day late is a sanity ceiling, not a claim
# that a bounce takes a day. The one measurement we have is 18 seconds.
SKEW_SECONDS = 300
WINDOW_SECONDS = 86_400

# The subject used when a sender's mailto: URL carries no parameters. It is
# never matched on: it appears in a large fraction of all marketing mail,
# so as a substring test it would match almost anything.
DEFAULT_SUBJECT = "unsubscribe"

# A token shorter than this is excluded from subject matching for the same
# reason DEFAULT_SUBJECT is — a short string is not distinctive enough for a
# substring test to mean anything.
MIN_TOKEN_LENGTH = 8


@dataclass(frozen=True)
class Bounce:
    """A delivery report, and the request it belongs to if that is knowable.

    ``request is None`` is the point of this type: it makes "a bounce I
    cannot account for" a state the renderer has to deal with, rather than
    one the matcher can quietly drop to make the run look cleaner.
    """

    rowid: int
    subject: str
    received_at: int
    failed_recipient: str | None = None
    request: SentRequest | None = None


def is_bounce_sender(sender: str) -> bool:
    """Does this address belong to a bounce daemon?"""
    local_part, at, _ = sender.partition("@")
    return bool(at) and local_part.casefold() in BOUNCE_LOCAL_PARTS


def candidate_rows(
    messages: Iterable[MessageRow], batch: list[SentRequest]
) -> list[MessageRow]:
    """Messages that could be bounces for this batch, cheaply.

    This runs against the snapshot with no AppleScript at all, because a
    header fetch costs the better part of a second and an inbox holds
    thousands of messages. It usually leaves between nought and three.
    """
    if not batch:
        return []
    first_send = min(request.sent_at for request in batch)
    earliest = first_send - SKEW_SECONDS
    latest = first_send + WINDOW_SECONDS
    return [
        message
        for message in messages
        if earliest <= message.date_received <= latest and is_bounce_sender(message.sender)
    ]


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup — Mail's casing is not guaranteed."""
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def is_delivery_report(headers: Mapping[str, str]) -> bool:
    """Is this actually a DSN, rather than a human called postmaster?"""
    content_type = _header(headers, "Content-Type").casefold()
    if "report-type=delivery-status" in content_type:
        return True
    return _header(headers, "Auto-Submitted").casefold().startswith("auto-replied")


def failed_recipients(headers: Mapping[str, str]) -> list[str]:
    """Addresses from ``X-Failed-Recipients``, case-folded and stripped."""
    raw = _header(headers, "X-Failed-Recipients")
    return [part.strip().casefold() for part in raw.split(",") if part.strip()]


def _matches_by_subject(request: SentRequest, subject: str) -> bool:
    token = request.subject.strip()
    if token.casefold() == DEFAULT_SUBJECT or len(token) < MIN_TOKEN_LENGTH:
        return False
    return token.casefold() in subject.casefold()


def attribute(
    pairs: list[tuple[MessageRow, Mapping[str, str]]], batch: list[SentRequest]
) -> list[Bounce]:
    """Turn (row, headers) pairs into bounces, attributed where possible.

    Rows whose headers say they are not delivery reports are dropped
    entirely — they were only ever candidates because of the sender's local
    part, and a person called postmaster is not a bounce.

    A request is claimed at most once: two lists can share an unsubscribe
    address, and letting one bounce satisfy both would report a failure that
    was never observed.
    """
    bounces: list[Bounce] = []
    unclaimed = list(batch)
    for row, headers in pairs:
        if not is_delivery_report(headers):
            continue
        recipients = failed_recipients(headers)
        matched: SentRequest | None = None
        for request in unclaimed:
            if request.to_address.casefold() in recipients:
                matched = request
                break
        if matched is None:
            for request in unclaimed:
                if _matches_by_subject(request, row.subject):
                    matched = request
                    break
        if matched is not None:
            unclaimed.remove(matched)
        bounces.append(
            Bounce(
                rowid=row.rowid,
                subject=row.subject,
                received_at=row.date_received,
                failed_recipient=recipients[0] if recipients else None,
                request=matched,
            )
        )
    return bounces
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_bounces.py -q`
Expected: PASS, 18 tests.

- [ ] **Step 5: Commit**

```bash
uv run pytest -q && uv run pytest tests/test_no_personal_data.py -q
git add src/mail_triage/bounces.py tests/test_bounces.py
git commit -m "feat: identify bounces and attribute them on exact matches only

X-Failed-Recipients against an address we wrote to, or a distinctive
subject token we sent. The literal word 'unsubscribe' and any token under
eight characters are excluded by name: they are the default subject and
would match almost any marketing mail. Anything unmatched is reported as
unattributed rather than pinned on the nearest request."
```

---

### Task 5: Rendering the report

**Files:**
- Modify: `src/mail_triage/bounces.py` (append)
- Test: `tests/test_bounces.py` (append)

**Interfaces:**
- Consumes: `Bounce`, `SentRequest`.
- Produces: `render_report(batch: list[SentRequest], bounces: list[Bounce], batch_id: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bounces.py`:

```python
from mail_triage.bounces import render_report


def test_a_bounced_request_is_named_with_its_recipient():
    request = _request()
    bounce = Bounce(rowid=1, subject="Delivery Status Notification (Failure)",
                    received_at=SENT_AT + 20, failed_recipient="leave@list.example",
                    request=request)
    report = render_report([request], [bounce], "2026-08-07T10-00-00")
    assert "news@list.example" in report
    assert "leave@list.example" in report
    assert "bounced" in report


def test_a_request_with_no_bounce_is_never_called_delivered():
    """A discarded request is indistinguishable from an accepted one."""
    report = render_report([_request()], [], "2026-08-07T10-00-00")
    assert "no bounce seen" in report
    assert "delivered" not in report.casefold()
    assert "confirmed" not in report.casefold()


def test_an_unattributed_bounce_is_reported_separately():
    bounce = Bounce(rowid=9, subject="Undelivered Mail Returned to Sender",
                    received_at=SENT_AT + 30)
    report = render_report([_request()], [bounce], "2026-08-07T10-00-00")
    assert "Undelivered Mail Returned to Sender" in report
    assert "unattributed" in report.casefold()


def test_the_report_says_it_cannot_give_a_reason():
    request = _request()
    bounce = Bounce(rowid=1, subject="Failure", received_at=SENT_AT + 20,
                    failed_recipient="leave@list.example", request=request)
    report = render_report([request], [bounce], "2026-08-07T10-00-00")
    assert "does not read" in report


def test_the_batch_and_account_are_named():
    report = render_report([_request()], [], "2026-08-07T10-00-00")
    assert "2026-08-07T10-00-00" in report
    assert "iCloud" in report
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_bounces.py -q -k report`
Expected: FAIL — `ImportError: cannot import name 'render_report'`.

- [ ] **Step 3: Implement `render_report`**

Append to `src/mail_triage/bounces.py` (and add `from mail_triage.layout import display_width, pad` to the imports):

```python
def render_report(batch: list[SentRequest], bounces: list[Bounce], batch_id: str) -> str:
    """One batch's outcome, as far as it can honestly be known.

    A request with no bounce is reported as "no bounce seen", never as
    delivered or confirmed. At this distance a silently discarded request is
    indistinguishable from an accepted one, and a tool that overclaims here
    is the bug being fixed rather than the fix.

    Widths come from ``display_width``, not ``len``: an emoji in a subject
    line occupies two terminal columns and would skew every row after it.
    """
    if not batch:
        return "That batch recorded no sent requests."

    account = batch[0].from_account or "an unidentified account"
    # Keyed by identity, not equality: two requests to the same list in one
    # batch are equal as values, and a dict keyed on the value would let one
    # bounce appear against both. ``attribute`` hands back the very objects
    # it was given, so identity is exactly the right key here.
    attributed = {
        id(bounce.request): bounce for bounce in bounces if bounce.request is not None
    }
    lines = [
        f"Batch {batch_id}, {len(batch)} "
        f"{'request' if len(batch) == 1 else 'requests'} sent from {account}.",
        "",
    ]

    rows = []
    for request in batch:
        bounce = attributed.get(id(request))
        if bounce is None:
            rows.append((request.sender, request.to_address, "no bounce seen", ""))
        else:
            rows.append((request.sender, request.to_address, "bounced", f'"{bounce.subject}"'))
    widths = [max(display_width(row[column]) for row in rows) for column in range(3)]
    for row in rows:
        padded = [pad(value, widths[column]) for column, value in enumerate(row[:3])]
        lines.append("  " + "  ".join(padded + [row[3]]).rstrip())

    orphans = [bounce for bounce in bounces if bounce.request is None]
    if orphans:
        lines.append("")
        lines.append(
            f"{len(orphans)} unattributed "
            f"{'bounce' if len(orphans) == 1 else 'bounces'} arrived in the window:"
        )
        for bounce in orphans:
            lines.append(f'  "{bounce.subject}" — read it yourself.')

    lines.append("")
    if any(bounce.request is not None for bounce in bounces):
        lines.append(
            "A bounce names the reason in its body, which this tool does not read."
        )
    lines.append(
        '"No bounce seen" is not confirmation: a request can be accepted and ignored.'
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run them and watch them pass**

Run: `uv run pytest tests/test_bounces.py -q`
Expected: PASS, 23 tests.

Note: `test_the_report_says_it_cannot_give_a_reason` needs the "does not read" line, which only appears when something actually bounced — that test supplies an attributed bounce, so it is satisfied.

- [ ] **Step 5: Commit**

```bash
uv run pytest -q && uv run pytest tests/test_no_personal_data.py -q
git add src/mail_triage/bounces.py tests/test_bounces.py
git commit -m "feat: report a batch's outcome without overclaiming

A request with no bounce is 'no bounce seen', never 'delivered'. A
silently discarded request looks identical from here, and a tool that
overclaims is the bug being fixed."
```

---

### Task 6: Wire it into the CLI, and document it

**Files:**
- Modify: `src/mail_triage/cli.py:620-738` (the `unsubscribe` command)
- Modify: `CLAUDE.md` (module table), `README.md`, `docs/superpowers/plans/2026-07-26-mail-triage.md` (tick Task 20), `docs/superpowers/specs/2026-08-07-unsubscribe-bounce-design.md` (the `bounces.py` line)
- Test: `tests/test_cli_unsubscribe.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `unsubscribe --check`; a `_check_batch(config, mail, batch) -> tuple[list[Bounce], str | None]` helper in `cli.py` returning the bounces and an error message when the batch cannot be checked.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_unsubscribe.py`:

```python
from mail_triage.sends import list_batches, load_batch


def test_a_successful_send_is_recorded(tmp_path, monkeypatch):
    options = [_option("a@x.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1\ny\n")
    assert result.exit_code == 0
    config = stub_config(tmp_path)
    [batch_id] = list_batches(config)
    [record] = load_batch(config, batch_id)
    assert record.sender == "a@x.example"
    assert record.to_address == "leave@x.example"
    assert record.from_account == "iCloud"


def test_a_refused_send_records_nothing(tmp_path, monkeypatch):
    """Nothing went out, so nothing may appear in the log."""
    options = [_option("a@x.example")]
    runner, mail = _sending_runner(tmp_path, monkeypatch, options)
    result = runner.invoke(cli, ["unsubscribe"], input="1\nn\n")
    assert result.exit_code == 0
    assert list_batches(stub_config(tmp_path)) == []


def test_check_with_no_batches_says_so(tmp_path, monkeypatch):
    runner, mail = _sending_runner(tmp_path, monkeypatch, [])
    result = runner.invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "No unsubscribe requests recorded yet" in result.output


def test_check_refuses_when_the_sending_account_is_not_configured(tmp_path, monkeypatch):
    """Searching the configured inboxes instead would report a clean run
    from the wrong mailbox."""
    from mail_triage.sends import SentRequest, record_send

    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", SentRequest(
        sender="a@x.example", to_address="leave@x.example", subject="token-abc12345",
        sent_at=1_700_000_000, from_account="Some Other Account",
    ))
    runner, mail = _sending_runner(tmp_path, monkeypatch, [])
    result = runner.invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "Some Other Account" in result.output
    assert "not a configured source" in result.output


def test_check_reports_a_bounce_it_can_attribute(tmp_path, monkeypatch):
    from mail_triage.sends import SentRequest, record_send
    from tests.conftest import build_fixture_db

    sent_at = 1_700_000_000
    config = stub_config(tmp_path)
    record_send(config, "2026-08-07T10-00-00", SentRequest(
        sender="news@list.example", to_address="leave@list.example",
        subject="token-abc12345", sent_at=sent_at, from_account="iCloud",
    ))
    db_path = tmp_path / "Envelope Index"
    build_fixture_db(db_path, [
        {"rowid": 77, "sender": "MAILER-DAEMON@relay.example",
         "subject": "Delivery Status Notification (Failure)",
         "date_sent": sent_at + 20, "date_received": sent_at + 20,
         "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
    ])

    mail = FakeMail(
        inbox=[77], mailboxes=["INBOX"],
        headers={77: {
            "Content-Type": "multipart/report; report-type=delivery-status",
            "X-Failed-Recipients": "leave@list.example",
        }},
    )
    monkeypatch.setattr(cli_module, "load_config", lambda: config)
    monkeypatch.setattr(cli_module, "AppleScriptMail", lambda: mail)
    monkeypatch.setattr(cli_module, "snapshot_database", lambda source, work: db_path)

    result = CliRunner().invoke(cli, ["unsubscribe", "--check"])
    assert result.exit_code == 0
    assert "bounced" in result.output
    assert "news@list.example" in result.output
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_cli_unsubscribe.py -q`
Expected: FAIL — `No such option: --check`, and the recording tests fail because nothing is written.

- [ ] **Step 3: Wire it up**

In `src/mail_triage/cli.py`, add to the imports:

```python
from mail_triage.bounces import attribute, candidate_rows, render_report
from mail_triage.sends import SentRequest, list_batches, load_batch, new_batch_id, record_send
from mail_triage.unsubscribe import folder_url
```

Add the helper immediately above the `unsubscribe` command:

```python
def _check_batch(config, mail, batch: list[SentRequest]):
    """Bounces for one batch, or a reason it could not be checked.

    A fresh snapshot every time: the one taken to find candidates predates
    the requests and cannot contain a reply to them.
    """
    account = batch[0].from_account
    source = next((item for item in config.sources if item.name == account), None)
    if source is None:
        return [], (
            f"Sent from {account or 'an account this tool could not identify'}, which is "
            "not a configured source — so the bounce cannot be looked for. Add it to "
            "[[source]] in your config, or check that inbox yourself."
        )
    with tempfile.TemporaryDirectory() as work:
        snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
        reader = EnvelopeReader(snapshot)
        try:
            inbox_url = folder_url(reader, source, source.inbox)
            if inbox_url is None:
                return [], f"No inbox named {source.inbox!r} in {source.name}."
            rows = candidate_rows(reader.inbox_messages(inbox_url), batch)
        finally:
            reader.close()

    pairs = []
    unreadable = 0
    for row in rows:
        try:
            pairs.append((row, mail.message_headers(row.rowid, source.inbox, source.name)))
        except MailError:
            # A message can be moved or deleted between snapshot and fetch.
            # One unreadable candidate is not a reason to abandon the check.
            unreadable += 1
    bounces = attribute(pairs, batch)
    if unreadable:
        click.echo(
            f"({unreadable} candidate "
            f"{'message' if unreadable == 1 else 'messages'} could not be read — "
            "moved or deleted since the snapshot.)"
        )
    return bounces, None
```

Add the option to the command decorator, above `def unsubscribe`:

```python
@click.option(
    "--check",
    "check",
    is_flag=True,
    help="Report bounces for the last batch of requests instead of sending more.",
)
```

and add `check: bool` to the signature: `def unsubscribe(dry_run: bool, limit: int, sender: str | None, check: bool) -> None:`.

Insert this block as the *first* thing in the function body, after the docstring:

```python
    config = load_config()
    mail = AppleScriptMail()

    if check:
        batches = list_batches(config)
        if not batches:
            click.echo("No unsubscribe requests recorded yet.")
            return
        batch = load_batch(config, batches[0])
        if not batch:
            click.echo(f"Batch {batches[0]} recorded no sent requests.")
            return
        bounces, problem = _check_batch(config, mail, batch)
        if problem:
            click.echo(problem)
            return
        click.echo(render_report(batch, bounces, batches[0]))
        return
```

and delete the now-duplicated `config = load_config()` / `mail = AppleScriptMail()` lines that followed the docstring.

Replace the send loop and its trailing advice with:

```python
    batch_id = new_batch_id()
    recorded: list[SentRequest] = []
    sent = 0
    failed = 0
    for number, option in picked:
        try:
            request = send_unsubscribe(option, mail)
        except (ValueError, MailError) as error:
            click.echo(click.style(f"  {option.sender}: not sent — {error}", fg="red"))
            failed += 1
            continue
        # Recorded only now, after the send returned. A record written first
        # would describe a request that might never have gone out, and
        # --check would then find no bounce for it and call it fine.
        record_send(config, batch_id, request)
        recorded.append(request)
        sent += 1
        click.echo(click.style(f"  {option.sender}: sent", fg="green"))

    click.echo(f"\nSent {sent}, failed {failed}.")
    if not recorded:
        return

    bounces, problem = _check_batch(config, mail, recorded)
    if problem:
        click.echo(problem)
    elif bounces:
        click.echo()
        click.echo(render_report(recorded, bounces, batch_id))
    else:
        click.echo(
            "No bounces yet — a rejection can take a minute to come back.\n"
            "Run 'mail-triage unsubscribe --check' shortly to confirm."
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_unsubscribe.py -q`
Expected: PASS. Then `uv run pytest -q` for the whole suite.

- [ ] **Step 5: Update the docs**

In `CLAUDE.md`, add two rows to the module table, after the `unsubscribe.py` row:

```
| `sends.py` | What was actually sent, so a bounce can be matched against it |
| `bounces.py` | Did the request land? Identify, attribute, report |
```

and add to "Things learnt the hard way", after the `mailto:` entry:

```
- **A send that reports success is not a request that landed.** The first
  live send printed "sent" and was rejected 18 seconds later by a bounce
  nobody was watching for. `sends.py` records what went out and
  `unsubscribe --check` looks for the bounce. Note what it cannot do: the
  SMTP diagnostic lives in the DSN's `message/delivery-status` body part,
  which this tool does not read, so it reports *which* request bounced and
  not *why*. And "no bounce seen" is never reported as "delivered" — a
  silently discarded request looks identical from here.
```

In `README.md`, document the flag alongside the existing `unsubscribe` entry:

```markdown
`mail-triage unsubscribe --check` reports whether the last batch of requests
bounced. A sent request is not a completed unsubscribe: rejections come back
as a bounce moments later, and this is what notices. It can tell you which
request bounced, not why — the reason is in the message, which this tool
does not read.
```

In `docs/superpowers/specs/2026-08-07-unsubscribe-bounce-design.md`, change the `bounces.py` row of the module table to read:

```
| `bounces.py` | Identify bounces, attribute them, and render the report. Pure functions over `MessageRow`s and header dicts — no snapshotting, no AppleScript, no config. |
```

In `docs/superpowers/plans/2026-07-26-mail-triage.md`, mark Task 20's heading complete:

```
### Task 20: A reported send is not a completed unsubscribe — COMPLETE 7 August 2026
```

- [ ] **Step 6: Run the full suite and the leak check**

Run: `uv run pytest -q`
Expected: PASS — 748 existing tests plus roughly 35 new ones.

Run: `uv run pytest tests/test_no_personal_data.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: unsubscribe --check reports whether the last batch landed

The send loop now records what went out and takes one free look for
bounces; --check re-reads the last batch any time afterwards. A sending
account that is not a configured source is reported by name rather than
searched for in the wrong inbox."
```

---

## Self-review notes

**Spec coverage.** Every section of the spec maps to a task: `date_received` → 1; the send log and record-after-send → 2; capturing the sending account and `SentRequest` → 3; phase 1, phase 2, and both attribution rules → 4; the reporting rules including "no bounce seen" → 5; the CLI, the free look, the unconfigured-account error and the docs → 6.

**One deliberate strengthening.** The spec excludes only the literal word `unsubscribe` from subject matching; Task 4 also excludes tokens shorter than `MIN_TOKEN_LENGTH` (8). Same reasoning, applied consistently.

**One deliberate placement change.** The spec's module table calls `bounces.py` "identify and attribute"; rendering also lives there rather than in a third module for one pure function. Task 6 Step 5 updates the spec's wording.

**Not covered, by design.** Reading DSN bodies for the diagnostic; waiting inside the send loop; feeding outcomes back into the candidate ranking; HTTP one-click (Task 21); the stale `_send_script` docstring about the Drafts copy (Task 22).
