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
| `pipeline.py` | Config in, proposals out — the run every entry point shares |
| `model/classify.py` | Stage orchestration, guards, precedence |
| `guards.py` | Do-not-file: flagged, or may need a reply |
| `security.py` | Do-not-file-unattended: security-relevant mail |
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
| `report.py` | What the unattended runs did, security first |
| `sizes.py` | Measure disk and envelope size per mailbox |
| `size_report.py` | Render the size grids |
| `mail_app.py` | AppleScript bridge, plus `FakeMail` for tests |
| `web/security.py` | Host, token and origin checks for the local server |
| `web/routes.py` | Pure request → response over one run's proposals |
| `web/server.py` | The loopback socket, and the interface's lifetime |
| `web/session.py` | A run's proposals under opaque ids |
| `web/payloads.py` | What the browser is told, and what it is not |
| `cli.py` | The group, and what is registered on it. Nothing else |
| `commands/_shared.py` | Pieces more than one command needs |
| `commands/inspect.py` | `accounts`, `size` |
| `commands/teach.py` | `learn`, `rules`, `explain`, `security` |
| `commands/triage.py` | `triage`, and the loops it runs |
| `commands/unsubscribe.py` | `unsubscribe`, and the bounce check |
| `commands/journal.py` | `undo`, `runs`, `report` |
| `commands/web.py` | The browser interface |

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

1. Per-message guards (security-relevant, a bill, flagged, may need a reply)
2. Hard rules (file / bin / leave alone)
3. The deletion veto
4. Stage A, then stage B

A rule is about a *sender*; a guard is about a *message*. Messages win.

The security guard is applied outermost, so it wins the *label* when a
message trips more than one. Only the label — every one of these holds the
message, so nothing is at stake but which sentence is printed. It is also the
only guard that carries `held_folder` through, because filing security mail
where it would have gone is the answer most of it wants once it has been
read.

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
- **A move that reports success is not a message that left the inbox.**
  A Gmail inbox is a label, so a cross-account move copies the message to the
  filing account and leaves the label untouched. Mail returns happily, the
  journal records "moved", and the message is still in the inbox to be filed
  again next run — with a fresh copy landing each time. Measured on 9 August
  2026: four attempts on one newsletter, three copies in the destination, the
  original in the Gmail inbox throughout. `execute` now verifies with
  `message_exists` after every move, clears the label by moving the leftover
  to the source's `archive` (`[Gmail]/All Mail`), verifies again, and reports
  a failure when it cannot. Note the shape — it is the same defect as the
  unsubscribe that reported "sent" and bounced: the success was reported by
  the thing that wanted to succeed, and nobody asked the mailbox.
  The boundary is *cross-account*: a move within the account does clear the
  label, which is what makes the archive step work. Proved live on 9 August
  2026 — INBOX -> [Gmail]/All Mail left the message in All Mail and gone
  from the inbox. `FakeMail` models that boundary rather than leaving the
  original on every move; a fake that did the latter could only ever test
  the failure path, which is how the success path went uncovered at first.
- **An outgoing message with no `sender` is composed from Mail's first
  account, which has nothing to do with the list being left.** On 9 August
  2026 an unsubscribe for a subscription on iCloud was composed from a Yahoo
  account — first in Mail's list, and not even a configured source. Yahoo
  would not send it, so three attempts left three drafts and nothing reached
  the list; had it sent, the request would have come from an address that
  never subscribed, which identifies nobody. `UnsubscribeOption.account` had
  recorded the right account all along and `send_unsubscribe` simply never
  passed it on. `send_mail` now takes `from_account` with no default and
  refuses rather than falling back — a wrong-address send is invisible,
  whereas not sending is not. Two limits worth knowing: where an account has
  several addresses the *primary* is used (this Mac's iCloud primary is an
  `@me.com` alias), which is right for a target carrying a subscriber token
  in its address and a guess for a bare `unsubscribe@` one; and the address a
  newsletter was actually delivered to is recorded nowhere we can consult.
- **Mail's `send` returns when the message is queued, not when it has gone.**
  The three wrong-account requests above did not vanish: they sat in the
  Outbox and were retried, each retry raising "Cannot send message using the
  server iCloud — the sender address …@yahoo.com was rejected … From address
  is not one of your addresses". The tool had already reported all three
  sent. `send_unsubscribe` now polls `outbox_contains` (subject *and*
  recipient — the subject is often the bare word "unsubscribe") once a second
  for 20s, so the ordinary send costs nothing and a stuck one is reported as
  a failure. Note the deliberate asymmetry: a message still queued at the
  timeout may yet go, and is called failed anyway, which can leave a real
  send with no entry in the log for a bounce to match. That is the better way
  round — the reverse is claiming a success we cannot demonstrate.
- **A step after the irreversible one can only turn a success into a false
  failure.** The send script asked Mail, *after* `send`, which account it had
  sent from — matching the outgoing `sender` against every account's `email
  addresses`. On 26 August 2026 that raised `-1700` on the first account it
  touched, because `repeat with addr in email addresses of acct` hands out
  unresolved references (`«class emad» of item 1 of every «class mact»`) that
  will not coerce to string; `get` materialises the list. The request had
  already gone — it reached Sent at 18:02:38 — and the browser showed a red
  banner, so `record_send` never ran and `unsubscribe --check` will never look
  for its bounce. The `get` was the proximate bug; the real one is that the
  loop should not have existed. `from_account` already named the account, the
  script sets `sender` from it and errors if it cannot resolve one, so the
  read-back added nothing and could only subtract. Ask the mailbox only what
  the mailbox alone knows — and, where you must, do it *before* the send.
- **A failed send left no trace anywhere.** `record_send` runs only after
  success — deliberately, see `sends.py`, because a pre-send record would let
  the bounce check report "no bounce, therefore fine" for a request that never
  went out. But the web route had no `except` at all, so the `MailError`
  escaped through `Router.handle` into the HTTP handler, killed the
  connection, and the page could only say "failed". The reason existed solely
  as a traceback in a terminal. Failures now go to `unsubscribe-failures/`, a
  sibling directory the bounce check never reads — the fix is to record them
  *apart from* the sends, not to relax the rule about the sends.
- **Asking Mail to confirm what Mail just told you is the same claim twice.**
  The fix above verified a cross-account move with `message_exists`, and
  cleared the Gmail label only when that reading said the message was still
  in the inbox. On 9 August 2026 the reading came back "already gone" —
  Mail answering from its own optimistic local state, before the server had
  confirmed — so the archive step was skipped, the journal recorded "moved",
  and the label was back fifteen minutes later. Four runs, four copies of one
  newsletter in the destination, the original labelled INBOX throughout. The
  shape to recognise: putting an unreliable reading *in charge of the repair*
  means the repair is skipped exactly when it is needed. The archive now runs
  on every crossing and the check only reports. Note what is still not
  guaranteed — the final check can be optimistic too, so "moved" means the
  label clear was attempted and looked right, not that the server agreed.
  Filing is not yet idempotent: if a label survives anyway, the next run
  files a second copy.
- **The guards protect against the wrong action; they had nothing to say
  about the right action taken unwatched.** Auto mode never bins, never
  touches vetoed mail and is fully undoable — a good envelope, and beside the
  point for its actual failure, which is filing something *correctly* and
  quietly that wanted eyes on it. Measured on 17 August 2026: a Dependabot
  alert, a new-sign-in notice, a root-account-accessed warning and a breach
  notification were each put through `needs_attention` with a
  `List-Unsubscribe` header supplied, and **not one was held**. The reason is
  structural — `_NO_REPLY_WORDS` contains "notifications" and "noreply", so
  `is_bulk` settles them from the address alone and the function returns
  before a header is read. That is the right answer to the question the reply
  guard asks; nobody awaits a reply to a Dependabot alert. It is simply not
  the only question. And these are the senders auto mode is *most* sure
  about: high volume, consistent, long filing history, so a breach notice
  reads 0.97 and files itself. Hence `security.py`.
  Two things worth keeping straight about it. **The asymmetry runs opposite
  to the reply guard's**, which is what licenses a broader rule: there a
  false negative loses a message wanting an answer, so it is narrow and fails
  safe by holding back; here *both* directions fail into the inbox, so a
  false positive costs one message filed by hand and a false negative loses a
  breach notice. And the limit on that generosity is usefulness, not safety —
  which is a question about the mailbox, not the word list, so it was
  measured rather than argued: **178 of 13,993 filed messages, 1.3%**, with no
  single term running away and a tail of individual CVEs.
- **Three copies of a block is the point at which it starts to drift, and the
  drift is what costs you.** `triage`, `web` and `report` each answered "what
  does the classifier think about the inbox?" in about twenty-eight lines of
  their own. By the time it was noticed they had diverged — `report` collapsed
  the three loader errors into one `try` whilst the others used three separate
  blocks with the comments copied between them. Nothing was broken yet, and
  that is the trap: this is the block a new guard is wired into, and a guard
  wired into two callers out of three is a guard that silently does not apply
  to the third. `pipeline.classify_run` is now the only copy.
- **A duplicate test name is a test that never runs.** `tests/test_review.py`
  had two functions called `test_quitting_stops_without_touching_the_rest` —
  one for the binning loop, one for the held loop. Python keeps the later
  definition, so the first had silently never executed, and the suite reported
  1039 passing tests whilst running 1038. Found by `ruff --select F811`, not by
  the suite, which cannot see it by construction. Worth running periodically:
  `uvx ruff check --select F811,F821,F401 src/ tests/`.
- **This tool is deliberately never scheduled, and the reason is a permission
  it would have to hold.** A LaunchAgent was built, installed and tested on
  17 August 2026, then removed the same day. Two findings are worth keeping.
  First, `launchctl load` succeeding says nothing about whether the job can
  work: the agent registered perfectly and would still have failed every
  morning with `PermissionError: Operation not permitted` on
  `Envelope Index-wal`, because macOS grants Full Disk Access per *responsible
  binary* and an agent's binary is not the terminal that has had the grant for
  months. Kickstarting a `--dry-run` copy under a separate label found that in
  a minute; waiting for the schedule would have taken a morning.
  Second, and the reason the feature went rather than the bug being fixed:
  there is no narrower TCC grant than Full Disk Access for `~/Library/Mail`,
  so any unattended runner holds blanket disk read permanently — and pointing
  it at a Python interpreter extends that to every dependency in the venv,
  executing unattended. Narrowing *which* binary held it reduced blast radius
  across projects and did nothing about blast radius across dependencies,
  which is the one that matters. Running by hand from a terminal that already
  has the grant costs one command and holds nothing. If scheduling is ever
  revisited, the prerequisite is reading Mail without the database at all —
  and the blocker there is the folder list, since AppleScript returns flat
  leaf names and loses the nested paths filing depends on.
- **A batch that reports only at the end looks stalled, and then wastes the
  time it saved.** The browser's Apply sent every decision in one request:
  Mail moves one message at a time and takes seconds over each, so a press of
  ten was most of a minute behind an unchanging "Moving…" — and only *then*
  did the rows begin their departures, one after another, whilst there was
  nothing left to wait for. The animation had been designed to be watched and
  was instead a second queue after the first. It is now a request per message,
  with the row's word ("filing…") up whilst that message is actually moving,
  and each departure chained to the last but never awaited before the next
  request goes out — so the only serial thing is the part that has to be.
  The catch worth knowing: undo is about the *press*, not the message, so the
  page sends a batch id and `Router._run_for` keeps one journal run for the
  lot. Splitting the request without that would have left Undo putting back
  only whichever message happened to go last.
- **Binding to 127.0.0.1 does not stop a web page from reaching the server.**
  DNS rebinding resolves an attacker's hostname to 127.0.0.1, and their page is
  then same-origin with ours. The strict `Host` check in `web/security.py` is
  what actually closes that, and a per-run token in a *header* — which no
  cross-origin page can read out of our HTML — closes CSRF without depending
  on cookie `SameSite` rules. Neither is optional; they answer different
  attacks.
- **`Feedback-Id` does not mean bulk.** See above — the same measurement
  discipline applies to the browser interface: `frame-src https:` in the CSP
  covers the iframe, but an anchor `href` is covered by no policy at all, so
  an unsubscribe target is checked to be `https` in `web/payloads.py` *and*
  again in `app.js`.
- **`<dialog>` closes on Escape without firing your close button.** Reset the
  unsubscribe iframe on the dialog's own `close` event, or the sender's page
  goes on running invisibly for as long as the tab is open.
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
