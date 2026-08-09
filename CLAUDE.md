# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Non-negotiable safety rules

1. **Never write to Apple Mail's database.** Read from a snapshot copy only,
   opened read-only. Every mutation goes through AppleScript.
2. **Never move, delete or send real mail without explicit approval.** Tasks
   that touch a live mailbox are checkpoints: stop, describe exactly what will
   happen to which message, and wait. Test on one message, verify it, and undo
   it before running any batch.
3. **Never verify with a live run.** Use `--dry-run`, the test suite, or the
   `FakeMail` interface. A scripted live run once moved three real messages
   when its input ran out; they were recovered from the journal, but the
   lesson stands.
4. **Never commit to `master`.** Work on `feature/<name>` branches.
5. **Nothing personal in the repository.** No addresses, account UUIDs, real
   folder names or subject lines in `src/`, `tests/` or `docs/`.
   `tests/test_no_personal_data.py` enforces this — run it before publishing
   anything.

## Layout

- `src/mail_triage/` — the package (src layout; the only route to it is the
  install)
- `local/` — **gitignored.** Trained model, rules, run journals, real config
- `docs/superpowers/` — design specs and the implementation plan
- Run everything through `uv run`

## Architecture

Bulk reads come from a snapshot of `~/Library/Mail/V10/MailData/Envelope Index`
(with its `-wal` and `-shm` companions — omitting them yields stale data that
looks convincingly like a bug). Writes go through `osascript`.

| Module | Responsibility |
|---|---|
| `envelope.py` | Snapshot and read the mail database |
| `inputs.py` | One snapshot's worth of everything a triage run reads |
| `folders.py` | Parse mailbox URLs; normalise folder names |
| `corpus.py` | Filing history into recency-weighted training examples |
| `model/sender.py` | Stage A: sender and domain → folder |
| `model/tokens.py` | Stage B: naive Bayes over subject tokens |
| `model/classify.py` | Stage orchestration, guards, precedence |
| `guards.py` | Do-not-file: flagged, or may need a reply |
| `never_personal.py` | Senders vouched for as never awaiting a reply |
| `invoices.py` | Bill detection |
| `rules.py` | Hard per-sender rules |
| `corrections.py` | Folders typed over a proposal, weighted 10× in training |
| `asking.py` | Choosing and asking about uncertain senders |
| `review.py` | Proposal table and the confirm loops |
| `layout.py` | Terminal column arithmetic shared by every table |
| `execute.py` | The only module that moves mail |
| `deletion.py` | Deletion as negative evidence, counted per account |
| `unsubscribe.py` | Rank lists worth leaving; send the request (the only sender) |
| `sends.py` | What was actually sent, so a bounce can be matched against it |
| `bounces.py` | Did the request land? Identify, attribute, report |
| `journal.py` | Run journal and undo |
| `sizes.py` | Measure disk and envelope size per mailbox |
| `size_report.py` | Render the size grids |
| `mail_app.py` | AppleScript bridge, plus `FakeMail` for tests |

### Several accounts

`Config.sources` lists every account whose inbox is triaged; a config naming
only `account_url_prefix` synthesises one in `Config.__post_init__`, so
there is a single code path. Filing crosses accounts into
`filing_account`'s tree — one filing structure, not one per account —
whilst binning and deletion evidence stay within each source. A message's
own account is derived from `account_prefix(message.mailbox_url)`, never
passed in: only the message knows which inbox it came from.

### Precedence — the safety-critical part

Highest first. Get this wrong and mail is filed contrary to instructions.

1. Per-message guards (a bill, flagged, may need a reply)
2. Hard rules (file / bin / leave alone)
3. The deletion veto
4. Stage A, then stage B

A rule is about a *sender*; a guard is about a *message*. Messages win.

### Things learnt the hard way

- **Mail's AppleScript `id` equals the SQLite `ROWID`, and it is not stable.**
  A message that moves gets a new one. The RFC-822 `Message-ID`, captured
  before the move, is the only durable handle undo has.
- **`name of mailboxes of account` returns a flat list of leaf names.** Source
  the folder list from the database instead, which preserves nested paths and
  real capitalisation. Address nested mailboxes as
  `mailbox "Parent/Child" of account "…"`.
- **Any AppleScript `whose` query over a large mailbox costs seconds.** That
  is the floor; do not try to optimise it.
- **A Gmail inbox is a label, not a mailbox.** Every Gmail message's
  `messages.mailbox` points at `[Gmail]/All Mail`; inbox membership is a row
  in `labels(message_id, mailbox_id)`. Use `EnvelopeReader.inbox_messages`,
  which unions both. Joining `labels` to `messages` drops stale rows for
  free — 11 raw rows became the 9 messages Mail itself reported.
- **`[Gmail]` is an fnmatch character class.** `[Gmail]*` matches any name
  beginning with g, m, a, i or l — `Accounts`, `Local`, `Invoices` — whilst
  *appearing* to work, because it also catches `[Gmail]/All Mail` via the
  `a` of `All Mail`. Write `[[]Gmail]*`. The failure mode is a silent hole
  in the training corpus, not an unexcluded All Mail.
- **Copying the database and its `-wal` is two operations, and Mail does not
  pause between them.** If Mail checkpoints in the gap, the database is copied
  before the checkpoint (so it lacks everything still in the log) and the log
  is copied after the restart (so it no longer carries those commits either).
  The snapshot then opens perfectly happily whilst missing every recent
  message: a run on 2 August 2026 saw 3 of 10 inbox messages, all of them old.
  `snapshot_database` now verifies the copy — the database file's size and
  mtime, and the log's 8-byte salt, which changes on every restart — and takes
  it again if any of them moved. Ordinary commits only append frames, so they
  are deliberately not treated as a race; watching the log's *length* would
  spin on a busy mailbox for nothing.
- **The database's folder path and the on-disk `.mbox` tree correspond
  exactly.** A mailbox URL path of `Parent/Child` is
  `V10/<account-uuid>/Parent.mbox/Child.mbox` on disk, so the two can be
  joined on the folder path alone. When summing a folder's own bytes, attribute
  each file to its *nearest* `.mbox` ancestor — otherwise a parent absorbs its
  children and every roll-up double-counts.
- **Size on disk means `st_blocks * 512`, not `st_size`.** A mail store is tens
  of thousands of small `.emlx` files; apparent size understates real
  consumption badly once block rounding is counted. `du` agrees with the former.
- **A `mailto:` unsubscribe URL's parameters are the request, not decoration.**
  `<mailto:unsubscribe-ENG@…?subject=hmv-prod/unsub/CgxnVLz…>` carries the
  subscriber token in `?subject=`. The first live send stripped the query
  string and was rejected outright — `554 Message rejected: The unsubscribe
  request has invalid form` — because a request without the token identifies
  nobody. Parse with `parse_qsl` and percent-decode (RFC 6068); default to
  the word "unsubscribe" only when the URL carries no parameters at all.
  Note the failure mode: the send *succeeds*, the tool reports "Sent", and
  the rejection arrives seconds later as a bounce nobody is watching for.
- **A send that reports success is not a request that landed.** The first
  live send printed "sent" and was rejected 18 seconds later by a bounce
  nobody was watching for. `sends.py` records what went out and
  `unsubscribe --check` looks for the bounce. Note what it cannot do: the
  SMTP diagnostic lives in the DSN's `message/delivery-status` body part,
  which this tool does not read, so it reports *which* request bounced and
  not *why*. And "no bounce seen" is never reported as "delivered" — a
  silently discarded request looks identical from here. Attribution is on
  exact matches only (`X-Failed-Recipients`, or a subject token we
  generated); the literal word "unsubscribe" is excluded by name, because it
  is the default subject and would match almost any marketing mail.
- **Mail leaves an autosaved copy of every sent message in Drafts**, and the
  sending script cannot prevent it. `delete newMessage` after `send` was
  tried live and changed nothing — the draft is already on the server by
  then. Deleting it afterwards means matching a stored message on subject
  and recipient, which is not a guess worth making.
- **`Feedback-Id` does not mean bulk, and betting the reply guard on it
  would file away the mail that most wants an answer.** Transactional mail is
  the case the guard has no signal for: an order confirmation is not a
  newsletter, so it carries no `List-Unsubscribe`, and plenty of senders set
  neither `Precedence` nor `Auto-Submitted`. `Feedback-Id` — the ESP
  feedback-loop header — looks like the perfect general fix, and on a survey
  of the live inbox (9 August 2026) it caught every order confirmation. It
  also sat on a heating firm's personal chase about a heat pump enquiry, sent
  through their CRM: addressed to the user directly, plainly wanting a reply,
  and indistinguishable in the envelope. The difference between "your order is
  confirmed" and "we have been trying to reach you" is in the meaning, not the
  headers. Hence `never_personal.py`: where the message carries no evidence,
  the evidence has to come from the user, and it lifts the reply guard *only*
  — flagging still wins above it. The failure mode this avoids is the quiet
  one: the tests stay green and the heat pump enquiry is simply gone.
- **A survey beats a grep when deciding what a header means.** Checking three
  header names on one message concluded "no signal available"; dumping every
  header across all 14 messages found `Feedback-Id` and then disproved it. The
  second run is what made the difference, and it cost one AppleScript loop.
- **In TOML, every top-level key must precede the first `[[source]]` table.**
  Anything after one is parsed as part of it. This broke the first draft of
  `config.example.toml`; `load_config` now says so by name when it happens.

## Conventions

- **British English** everywhere: code, comments, output, docs, commits.
- **Try the simple approach first.** Add complexity only when something
  demands it.
- **Measure rather than argue.** Several design decisions here were reversed
  by evaluating against held-out real mail; a plausible-sounding rule is not
  evidence. Record the numbers in the commit message.
- **Terminal output:** never use `len()` for column widths — emoji occupy two
  columns. Use `review.display_width()`.

## Testing

```bash
uv run pytest -q
```

All tests use synthetic fixtures and `FakeMail`. No test touches a real
mailbox or shells out to `osascript`.
