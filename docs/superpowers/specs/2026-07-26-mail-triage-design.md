# mail-triage — design

*Date: 26 July 2026*

## Purpose

A local-first CLI that reads the Apple Mail inbox, classifies each message
against the folders the user already uses, and files it — proposing every move for
confirmation until the classifier has earned the right to act unattended.

Primary job: **triage**. A read-only briefing mode is a later addition.

**Unsubscribe** (added 26 July 2026, after the initial design): once sorting is
trustworthy, the tool suggests mailing lists worth leaving, ranked by volume and
how much of it goes unread. Each suggestion is offered individually and sent
only on an explicit `y`. Only `mailto:` unsubscribe targets are used; HTTP
one-click unsubscribe is out of scope, as it means arbitrary outbound requests
to sender-supplied addresses. This is the sole circumstance in which the tool
sends mail — it never composes or replies otherwise.

## Constraints

- **Publishable.** The repository is destined for GitHub and must contain no
  personal data: no folder names, no addresses, no message content, no model.
- **Local learning area.** All identifying artefacts live in `local/`, which is
  gitignored: the trained model, corrections, run journals, and real config.
- **Local-first.** ~95% of decisions are made on-machine from learnt
  associations. An LLM tier exists for the remainder, is **off by default**, and
  redacts before sending.
- **British English** throughout — code, comments, output, docs.
- **Never write to Mail's SQLite database.** Mail owns it; direct writes corrupt
  it. All mutation goes through AppleScript.

## Scope

- **Account:** iCloud only, named in config so further accounts are a config
  change rather than a rewrite.
- **Candidate messages:** everything in the inbox, **read or unread**. Unread
  mail is explicitly in scope — the accumulated unread pile is the problem being
  solved. Mitigations: moves preserve unread state, mode A shows the full
  proposed list before acting, and every run is undoable.
- **Folders:** existing folders only. The tool never creates a mailbox. A
  message with no confident destination stays in the inbox, which doubles as the
  safety net for important mail.
- **Cross-account moves:** out of scope.

## Modes

| Mode | Command | Behaviour |
|---|---|---|
| Report | `triage --dry-run` | Classifies and prints; touches nothing |
| Confirm (default) | `triage` | Proposes moves as a table; approve / edit / reject; then acts |
| Auto | `triage --auto` | Moves above the confidence threshold without prompting |

Start in confirm mode. Auto mode becomes appropriate once the correction rate in
confirm mode is low enough to trust — a judgement the user makes, not the tool.

## Architecture

```
mail-triage/
├── src/mail_triage/
│   ├── cli.py           # Click commands
│   ├── config.py        # account, thresholds, folder exclusions
│   ├── envelope.py      # read-only SQLite snapshot + queries
│   ├── corpus.py        # history → weighted training rows
│   ├── model/
│   │   ├── sender.py    # stage A: sender + domain → folder map
│   │   ├── tokens.py    # stage B: hand-rolled naive Bayes
│   │   ├── llm.py       # stage C: optional, redacted
│   │   └── classify.py  # orchestration → Proposal
│   ├── mail_app.py      # AppleScript bridge (the only writer)
│   ├── review.py        # propose-then-confirm table
│   └── journal.py       # undo log
├── local/               # GITIGNORED
├── config.example.toml
└── tests/
```

Src layout per workspace convention: `[tool.hatch.build.targets.wheel]
packages = ["src/mail_triage"]`, `[tool.pytest.ini_options] pythonpath =
["src", "."]`.

No module under `src/` may contain a folder name or address literal.

### Data flow

1. **Read.** Copy `~/Library/Mail/V10/MailData/Envelope Index` plus its `-wal`
   and `-shm` companions to a temporary snapshot; query read-only. The live
   database is never opened. Requires Full Disk Access for the terminal.
2. **Learn.** Build weighted training rows from filed history (~74k messages
   across ~194 mailboxes at time of writing).
3. **Classify.** Entirely in-process. Each result is a `Proposal` carrying the
   destination folder, a confidence score, and a human-readable reason naming
   the stage that decided.
4. **Review.** Confirm mode renders the proposals; corrections are captured.
5. **Act.** AppleScript `move message to mailbox`. Unread state is preserved.
6. **Journal.** Each intended move is written to
   `local/journal/<run-id>.jsonl` *before* execution, so `undo <run-id>` can
   reverse the batch — including a batch that failed part-way.

### Message identity

**Verified 26 July 2026.** Mail's AppleScript message `id` is the SQLite
`messages.ROWID` — confirmed against three live inbox messages by subject
comparison. That is the join key **for matching database rows to live messages
before anything moves**.

**It is not a durable identity.** Moving a message changes its numeric id, and
moving it back does not restore the old one — measured live:
`447494` in INBOX → `447714` in the destination → `447715` back in INBOX. Any
record holding a numeric id is worthless the moment the message moves.

The durable key is `message id of message`, the RFC-822 `Message-ID`, which
belongs to the message rather than the database row. Verified across a move:
the numeric id changed again whilst `message id` stayed byte-identical, and
`messages of mailbox "X" whose message id is "…"` matched exactly one message.

**The journal therefore records the RFC-822 Message-ID**, and undo looks
messages up by it. Recording the numeric id would have produced an undo that
silently reversed nothing whilst reporting success.

Mail also exposes `flagged status` and `was replied to` per message, both used
by the do-not-file guard below.

The RFC-822 `Message-ID` originally proposed for this role **is not stored in
the database at all**, nor are raw headers. Anything header-derived — notably
`List-Unsubscribe` — must be fetched per message via AppleScript
`all headers of message`, which is a round trip and therefore rationed.

## Nested folders

**Verified 26 July 2026, after the model was first trained.** The folder tree is
almost entirely nested: of the 44 folders the model learnt, 41 contain a `/`
(`Parent/Orders`, `Team/Tech/Cloud`).

Mail's AppleScript returns a *flat list of leaf names* for an account's
mailboxes, which cannot be matched against nested paths and is ambiguous when a
leaf name repeats. The folder list therefore comes from the envelope database,
which preserves full paths and real capitalisation. Moves address mailboxes by
path — `mailbox "Parent/Orders" of account "iCloud"` — which is verified to work.

Had this gone unnoticed, the classifier would have rejected nearly every
proposal as "folder does not exist in this account", and the failure would have
looked like a modelling problem rather than a lookup mismatch.

## Training scope

Filing history spans two accounts: the live iCloud account (19,002 messages, 52
folders) and an On My Mac archive (53,034 messages, 47 folders, 36 names shared
with iCloud). The archive is older iCloud mail that was moved off the server
yearly to save space — a practice that has since lapsed.

**Decision: train on the iCloud account only.** 19,002 filed messages is ample,
and the archive is exactly the mail that recency weighting would discount. The
set of accounts to learn from is a config list, so folding the archive in later
is a one-line change rather than a rewrite.

## Do-not-file guards

Two hard vetoes, added 26 July 2026 after the first dry run. Both override
confidence entirely: a vetoed message stays in the inbox no matter how certain
the classifier is, and the proposal table says why.

### Mail awaiting a reply or action

The user: *"If anything requires me to do something or needs a reply they mustn't
be filed away unless that has happened."*

A message is vetoed when either holds:

- **A human wrote it to him** — it is not bulk mail. Bulk is identified by a
  `List-Unsubscribe` header or a no-reply-style sender address; anything else
  is treated as person-to-person and left alone.
- **He has flagged it.** A flag is an explicit do-not-file marker.

Unread status is deliberately *not* a guard. Clearing the unread pile is the
problem this tool exists to solve, and the user confirmed that when asked.

### Senders whose mail is now deleted rather than filed

The user: *"sometimes I delete messages instead of filing them away. Harder to
see as the messages are gone. But it often happens."*

Measured on 26 July 2026, this was not a minor gap: **19 of the 23 proposals in
the first dry run came from senders whose mail had recently been deleted.** Two
distinct patterns, handled differently:

- **Only deletes now** (`0 filed, 9 deleted` over the last 75 days, and
  similar): the classifier would have filed these at 0.82–0.88 confidence on
  the strength of older history, when the current behaviour is to bin every
  one. **Vetoed from filing, and surfaced as unsubscribe candidates.**
- **Keeps some, bins some** (`5 filed, 21 deleted`): the user triages these per
  message. Still proposed, but the table must show how often he bins them, so
  the choice is informed.

Deleted mail was previously excluded from training altogether, which is what
made this invisible. It now counts as evidence — against filing, not for a
folder.

**Caveat on the data.** The Trash purges on a rolling window; at the time of
measurement it spanned roughly two months. This signal is recent-only and
cannot be back-filled, which suits recency weighting but means a sender that
was abandoned long ago leaves no trace.

### Invoices and bills — RULE 2 IMPLEMENTED 27 JULY 2026

The user, 26 July 2026: *"You don't unsubscribe from invoices. If a message has
an included invoice we want to deal with that first."*

Two distinct rules, neither yet built:

1. **Never offer to unsubscribe from a sender who sends invoices or bills.**
   Not yet enforced anywhere, because nothing offers to unsubscribe yet.
   `invoices.sends_invoices()` exists and is tested as the hook for it.
   Unsubscribing from billing correspondence is actively harmful — it is the
   one category where the unsubscribe feature must not fire, regardless of how
   ignored or repetitive the mail looks.
2. **An invoice must be dealt with before it can be filed.** It joins the
   do-not-file guards above: an invoice stays in the inbox, and should be
   surfaced prominently rather than merely held back, since the point is that
   it needs action.
   **Done, 27 July 2026** (`invoices.py`). It outranks everything, including a
   hard rule — a "file everything from this sender" instruction must not sweep
   away a bill — and is applied to *unplaced* messages too, since an unmarked
   invoice staying in the inbox would otherwise be offered for binning. It
   gets its own heading in the summary rather than being listed among ordinary
   vetoes.

   **Calibration, measured 27 July 2026.** Over 3,878 messages from the last
   year the subject rules fire on 7.3%, with no false positives in a
   hand-check of the first thirty. Two candidate terms were tested and
   rejected: `bill` fired six times in two years and half were wrong (a
   parliamentary Bill, a newsletter headline), and bare `payment` matched
   1,174 subjects, mostly "payment method updated". `payment` is now only
   accepted next to a due/overdue word, a currency amount, or "direct debit".
   On the live inbox this catches both real bills — an invoice and a direct
   debit notice — and nothing else.

Candidate detection signals, in rough order of reliability: an attachment whose
name or type indicates an invoice or statement (the envelope database has an
`attachments` table, so this is cheap); subject-line terms such as invoice,
receipt, bill, statement, payment due, overdue, amount due; and known billing
senders learnt from the folders the user already files bills into.

Note the first dry run vetoed an invoice already, but only incidentally — it
had no `List-Unsubscribe` header and so was treated as person-to-person. That
is luck, not a rule, and it would not hold for a billing sender that does
include the header.

### Delete as a first-class choice — RULE 1 IMPLEMENTED 27 JULY 2026

The user, 26 July 2026: *"When unsure you can ask about moving versus delete,
then learn from that. Or consider unsubscribing from too many deletes."*

The review loop currently offers accept or reject, so the only thing it can
learn is "this folder was right" or "this folder was wrong". Rejecting tells us
nothing about *why*, and the commonest why — the user did not want the message at
all — is exactly the signal the deletion guard above is reconstructing after
the fact from the Trash.

Two rules, neither yet built:

1. **Offer delete alongside file, particularly when confidence is low.** A
   third answer (file / delete / leave) turns the ambiguous cases into a
   direct question rather than a guess. Deletion is destructive, so it goes to
   the Trash via the same journalled, undoable path as a move — never a hard
   delete.
   **Done, 27 July 2026.** Step mode offers `[y/n/d]`; `d` moves to
   `config.trash_folder` through `execute`, so `undo` reverses it with no
   knowledge that a delete happened. Two limits worth recording: "accept all"
   cannot bin anything (a batch keystroke is the wrong instrument for a
   destructive choice), and — **closed 27 July 2026** — a second pass now
   offers binning for mail the classifier could *not* place, which is where
   most of the binnable mail actually is.

   That second pass excludes one category on purpose: mail held back by an
   **attention veto** (flagged, or apparently awaiting a reply). Binning it
   would defeat the guard that held it back, and more finally than filing
   would. Mail held back by the **deletion veto** is included, since that veto
   means "you keep binning this sender" — the strongest candidate, not the
   weakest. The distinction is carried by `Proposal.veto_kind`, added so the
   policy does not rest on substring-matching prose written for humans.

   Measured on the real inbox, 27 July 2026: of 65 messages, 45 were offered
   for binning (9 no history, 31 inconsistent, 5 deletion-vetoed) and 4 were
   correctly withheld — among them an invoice and a payment notice.
2. **Repeated deletes for one sender become an unsubscribe prompt.** — still not built. The
   deletion index already counts binned messages per sender, so the threshold
   exists; what is missing is the step from "vetoed, and surfaced as an
   unsubscribe candidate" to actually asking. Subject to the invoice rule
   above: billing senders are never unsubscribe candidates however often their
   mail is binned.

Answers here are stronger evidence than historical filing — the user is telling
us directly, about this message, now — so they should carry at least the
`correction_weight` given to corrections.

**Designed in full** in `2026-07-26-asking-when-unsure-design.md`, which settles
the questions this note leaves open: questions are asked per sender rather than
per message, answers become hard rules rather than weighted evidence, and five
senders are asked about per run, ranked by how much mail each answer settles.

## Classification

Three stages, tried in order, each able to explain itself:

- **Stage A — sender/domain map.** Exact sender address first, then sender
  domain. Expected to resolve the large majority of inbox mail. Reason reads
  e.g. "sender domain seen 214× in <folder>, 98% consistent".
- **Stage B — naive Bayes over tokens.** Features: sender, sender domain,
  subject tokens, `List-Id`, presence of `List-Unsubscribe`. Hand-rolled in pure
  Python rather than scikit-learn — around eighty lines, no install burden, and
  able to report the tokens that drove the decision.
- **Stage C — LLM (optional, off by default).** Headers plus a short plain-text
  body snippet, redacted: addresses reduced to domains, digit runs resembling
  account or order references masked. Only reached when A and B are both
  uncertain.

Anything still uncertain after all three stays in the inbox.

Stage A and B see headers only. The body snippet is used solely by stage C.

## Handling imperfect history

Existing folder assignments are **evidence, not ground truth**. Filing has been
inconsistent and intentions have changed over time. Four mechanisms:

- **Recency weighting.** Training rows decay exponentially with a ~12-month
  half-life, so recent habits dominate older ones.
- **Consistency gating.** For each sender, measure the spread of destination
  folders. A sender split 55/45 across two folders yields *no proposal* rather
  than a coin-flip. Confidence reflects agreement, not merely volume.
- **Drift reporting.** `learn` reports senders whose destination changed over
  time, so the model's assumptions are visible and can be overridden.
- **Corrections outrank history.** A correction made in confirm mode is stored
  in `local/corrections.jsonl` at roughly 10× weight. Correcting the tool is the
  mechanism for changing an old habit, and the signal that earns auto mode.

Training excludes `Deleted`, `Junk`, `Sent`, `Drafts`, `Outbox` and
`Recovered Messages (*)`; these encode nothing about filing intent. The
exclusion list lives in config, expressed as patterns, not as personal folder
names in source.

## Commands

| Command | Purpose |
|---|---|
| `learn` | Build the model from history; print stats, coverage estimate, drift report |
| `triage` | Propose-then-confirm |
| `triage --dry-run` | Report only |
| `triage --auto` | Threshold-based, unattended |
| `undo <run-id>` | Reverse a batch |
| `explain <sender>` | Show why mail from a sender lands where it does |
| `unsubscribe` | Suggest lists worth leaving; send the request one at a time |
| `unsubscribe --dry-run` | List the candidates; send nothing |
| `unsubscribe --sender TEXT` | Offer only senders matching TEXT |

## Unsubscribe

The one place mail-triage *sends* mail, added at the user's request of 26 July
2026. The original spec forbade sending outright; this is the amendment.

**Candidates** are senders with mail currently in one of the triaged inboxes,
ranked by how thoroughly their mail is ignored. Two signals feed that, both
drawn from the same recent window:

- mail sitting unread in the inbox, and
- **mail that was deleted** — the user's addition of 5 August 2026. This is the
  stronger of the two. Unread only catches what you have not got round to,
  whereas binning is a decision; and by the time you have binned it, the
  message has left the inbox where a read-flag count would find it.
  `deletion.build_deletion_index` already maintains these counts per sender for
  the filing veto, so the same index is reused rather than a second notion of
  "ignored" being invented. Counts stay **within each account**, exactly as
  they do for the veto: a sender binned in one account and read in another is
  not being ignored.

Ranking leads on the ignored *count*, not the share, so a list binned thirty
times outranks a stranger whose single message is merely unopened — a 100%
share of one message is not evidence.

**The header fetch is the slow part.** `List-Unsubscribe` is not in the
database and each read is an AppleScript round trip, so the ranking on counts
happens first and only the top `--limit` senders (default 20) are asked about.
Senders whose mail carries no such header simply drop out.

**The request must carry the sender's own parameters.** A `mailto:` URL's
`?subject=` and `?body=` are part of the request: the subject typically *is*
the subscriber token. The first live send discarded them and was rejected —
`554 Message rejected: The unsubscribe request has invalid form` — which is
the provider correctly observing that the request identified nobody. Note how
this fails: the send succeeds, the tool says "Sent", and the rejection arrives
seconds later as a bounce. A reported send is not a completed unsubscribe.

**The interaction is list-then-choose** (revised 6 August 2026). The whole
ranked list is printed, numbered, and the user picks from it — several at once
(`1,4`, `1-3`) — because deciding about a list is one job, not one per sender.
The first implementation walked the ranking asking about each sender in turn,
which meant seventeen answers to reach the one you wanted; the ranking is a
view, not a queue.

Selecting a number *is* the explicit per-sender consent this design requires.
The selection is then shown back and confirmed as a set, which is the second
gate rather than the first. Picking an HTTP-only sender stops the run rather
than being skipped over: it misreads the list, and sending the others whilst
mentioning it afterwards would bury that. `--sender` narrows the list before
any of this, which is how a single deliberate send is aimed.

**Known defect (6 August 2026):** Mail leaves a copy of the sent message in
Drafts. The first live send landed correctly in Sent Messages on the right
account, and a stray draft appeared in the same account half a minute later.
Harmless per send, but it would accumulate. Unfixed. Only `mailto:` targets are sent. HTTP
one-click unsubscribe (RFC 8058) stays unsupported: it would mean arbitrary
outbound web requests to an address the sender chose. The target address comes
from a header the *sender* wrote, so it is treated as untrusted — checked
against an address shape before sending, and escaped before it reaches
AppleScript, unlike the folder and account names elsewhere in that module,
which are the user's own.

## Error handling

- Mail not running → abort with a clear message; do not launch it.
- Target mailbox missing → skip the message, leave it in the inbox, report it.
- AppleScript failure mid-batch → the journal is already written, so `undo`
  covers the moves that succeeded.
- Database locked → impossible; we work from a snapshot.
- Full Disk Access not granted → detect the failure mode and say so explicitly,
  with the fix.

## Testing

The Bayes maths, confidence gating, decay weighting, redaction and journal
round-tripping are pure functions and are unit-tested. `envelope.py` is tested
against a small fixture database built in the test suite. The AppleScript layer
sits behind an interface with a fake implementation, so the whole suite runs
without touching real mail and without moving a single message.

## Deliberately excluded

Composing, replying to, or sending mail *other than* unsubscribe requests;
creating mailboxes; cross-account moves; HTTP one-click unsubscribe; a GUI;
rewriting Mail's rules; a daemon. Scheduling, if wanted later, is `--auto` under
cron rather than a resident process.

Re-archiving old iCloud mail to local storage — the practice that produced the
On My Mac archive — is out of scope. It was raised as a possible future need and
would warrant its own spec, being a bulk mutation of thousands of messages.
