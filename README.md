# mail-triage

A local-first command-line tool that learns how you file mail in Apple Mail,
then proposes where new inbox messages should go — and files them once you
agree.

It learns from what you have already done. There are no rules to write and no
model to configure: your existing folder structure and years of filing
decisions are the training data.

```
Sender                       Subject                                  → Folder                     Conf
news@example.com             Weekly roundup                           Reading/Newsletters          0.88
orders@example.net           Your order has been dispatched           Shopping/Orders              0.95

14 of 65 would be filed; 51 staying in the inbox.
  confidence: average 0.87; 7 of 14 at 0.90 or above.
  2 need dealing with — these look like bills:
    Your monthly statement — the subject looks like a bill or invoice
  9 staying in the inbox: no filing history for this sender.
  31 staying in the inbox: sender known, but filing history is too inconsistent to call.
```

## What it will not do

The design assumes that **leaving a message in the inbox is the safe outcome,
not a failure**. Most of a typical run stays put, and that is deliberate.

Nothing is filed away when:

- **You flagged it**, or it looks like a person wrote to you and may want a
  reply. Bulk mail is identified by a `List-Unsubscribe` header or a
  no-reply-style address; anything else is treated as person-to-person.
- **It looks like a bill.** Invoices, receipts, statements and direct-debit
  notices stay in the inbox and are listed separately, because the point of a
  bill is that it needs dealing with. This outranks every other instruction,
  including an explicit "file everything from this sender" rule.
- **The classifier is not confident enough**, or two folders fit nearly as
  well as each other.
- **You have been deleting that sender's mail lately** rather than filing it.

## Safety

- **The mail database is never written to.** Bulk data is read from a
  *snapshot copy* of Apple Mail's `Envelope Index`, opened read-only. All
  changes go through AppleScript, which is the only supported way to ask Mail
  to move something.
- **Every intended move is journalled before it happens**, so a run can be
  reversed with `mail-triage undo` — including deletions, which are moves to
  the Trash and never hard deletes.
- **Moves are keyed on the RFC-822 `Message-ID`**, not on Mail's numeric id,
  which changes when a message moves. A message whose durable key cannot be
  read is left alone rather than moved somewhere it could not be recovered
  from.
- **Nothing personal is in the repository.** The trained model, your rules,
  run journals and real configuration all live in a gitignored `local/`
  directory. A test suite check (`tests/test_no_personal_data.py`) fails the
  build if an address, account UUID or folder name reaches the source, tests
  or docs.

## Requirements

- macOS with Apple Mail configured
- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- **Full Disk Access** for your terminal, so the mail database can be read.
  System Settings → Privacy & Security → Full Disk Access.

## Setup

```bash
uv sync

# Find your accounts. The prefix in the first column is what you need.
uv run mail-triage accounts

cp config.example.toml local/config.toml
# Edit local/config.toml: set filing_account_prefix, and add a [[source]]
# for each account whose inbox you want gone through.

# Learn from your filing history. Read-only; touches no mail.
uv run mail-triage learn

# See what it would do. Moves nothing.
uv run mail-triage triage --dry-run
```

## Using it

```bash
uv run mail-triage triage
```

Every configured account's inbox is scanned in one run, and the proposals
are shown together with an Account column when there is more than one.

A run has four stages:

1. **Questions.** Up to five senders whose mail you file inconsistently.
   Answer with a numbered folder, a typed folder path, `b` to bin that sender's
   mail from now on, `l` to leave them alone for good, or Enter to skip.
   Answers are saved as you give them, so interrupting loses nothing.
2. **The proposal table**, and a summary of what is staying put and why.
3. **Review.** `a` accepts everything proposed, `s` steps through one at a
   time (`y` file, `n` skip, `d` bin, `b` go back and change the previous
   answer), `q` does nothing. Typing a folder name instead files the message
   there — a leaf name is enough, and an unrecognised one is refused rather
   than guessed at. That is recorded as a **correction**: the next `learn`
   weights it at ten historical filings, so putting a sender right once is
   enough to change where its mail goes.
4. **Binning.** An optional pass over what stayed in the inbox, offering to
   bin each one. Bills and anything that may need a reply are never offered
   here.

Nothing moves until you have answered, and the run ends by telling you how to
reverse it.

`triage --auto` skips all of that and files everything at or above
`auto_threshold` (0.9 by default). It only ever files: it never bins, and
never touches mail a guard held back — flagged, apparently awaiting a reply,
or carrying a bill — however confident the classifier was about it. The run is
journalled like any other, so `mail-triage undo` reverses it in full. Worth
running `triage --dry-run` first to see what it would do.

### Commands

| Command | What it does |
|---|---|
| `accounts` | List mail accounts with mailbox and message counts |
| `size` | Show how much space each mail folder occupies |
| `size --min-size 0` | Show every folder, however small |
| `size --account NAME` | Report one account only |
| `size --bytes` | Exact byte counts instead of rounded sizes |
| `learn` | Build the classifier from your filing history |
| `triage` | Classify every configured inbox, then file what you approve |
| `triage --dry-run` | Report only; move nothing |
| `triage --limit N` | Offer at most N messages for filing |
| `triage --auto` | File everything at or above `auto_threshold` without asking |
| `triage --no-ask` | Skip the sender questions |
| `triage --source NAME` | Triage only that source. Repeatable |
| `rules` | List the answers you have given about senders |
| `rules --forget <sender>` | Remove one sender's rule |
| `explain <sender>` | Show why that sender's mail goes where it does |
| `unsubscribe` | Suggest lists worth leaving, and send the request if you agree |
| `unsubscribe --dry-run` | List the candidates; send nothing |
| `unsubscribe --limit N` | How many senders to fetch headers for (default 20) |
| `runs` | List runs that can be undone |
| `undo [run-id]` | Reverse a run, defaulting to the most recent |

## Getting less mail

`unsubscribe` is the only command that sends anything. It ranks the senders
whose mail you most reliably ignore, and offers them one at a time:

```
news@list.example [iCloud] — 4 in the inbox, 4 unread, 22 binned (96% ignored)
  Unsubscribe via leave@list.example? [y/N]
```

**Deleted mail is the main evidence.** Unread-in-the-inbox only sees what you
have not got round to; the mail you binned is a decision, and it has already
left the inbox where an unread count would find it. Deletions are counted
within each account, over the same recent window the filing veto uses, so a
sender you bin in one account and read in another is not mistaken for noise.

Nothing is sent without an explicit `y` for that particular sender — the
prompt defaults to no, and `--dry-run` sends nothing at all. Only `mailto:`
unsubscribe targets are used; HTTP one-click unsubscribe is deliberately
unsupported, as it would mean firing arbitrary web requests at an address the
sender chose. Fetching the `List-Unsubscribe` header costs an AppleScript
round trip per sender, which is why `--limit` exists and why the ranking
happens before the fetching.

## Where the space has gone

`mail-triage size` reports what the mail store occupies, as a grid per
account: an all-accounts summary, then each account's folders as an indented
tree with roll-up totals, then Mail's own `MailData` directory.

Each folder carries two figures, because they answer different questions.

- **In Mail** is the size of every message Mail knows about, taken from the
  envelope database. It covers accounts whose bodies were never downloaded.
- **On disk** is what the folder actually costs this Mac, measured the way
  `du` measures it — allocated blocks, not apparent size, which matters when a
  mailbox is tens of thousands of small files.

Where the two differ markedly, that is a fact about the account rather than an
error: mail held on the server and not cached locally shows a large "In Mail"
against a small "On disk". An account with no local store at all shows a dash,
never a zero — "not kept here" is not "empty".

Folders below `--min-size` (2 MB by default) collapse into a single
`N smaller folders` line that carries their totals, so the visible rows always
add up to the account's total.

The disk column is weighted in three tiers — bold yellow for a folder worth
acting on, plain yellow for one worth noticing, dim for the rest — judged as a
share of the grid's total rather than by a fixed cut-off, so the colour means
the same thing in a small account as in a large one. Colours are chosen for a
dark terminal, and are dropped automatically when the output is piped.

`MailData` holds no mail — it is the envelope database, the search and
junk-filter indexes, and a cache of remote images. It is listed anyway, so the
accounts sum to what the store really occupies.

The command is read-only: it copies the database, stats files, and touches
neither Mail nor a single message.

## Several accounts

Each account you want gone through is a `[[source]]` in `local/config.toml`,
naming the account as Mail shows it, its URL prefix, its inbox and its bin:

```toml
filing_account        = "iCloud"
filing_account_prefix = "imap://AAAAAAAA"

[[source]]
name   = "iCloud"
prefix = "imap://AAAAAAAA"
inbox  = "INBOX"
trash  = "Deleted Messages"

[[source]]
name   = "Gmail"
prefix = "imap://BBBBBBBB"
inbox  = "INBOX"
trash  = "[Gmail]/Bin"
ignore = ["[[]Gmail]*"]
```

In TOML every top-level setting must appear **before** the first `[[source]]`
table; anything after one is read as part of it.

**There is one filing tree.** Mail is filed into `filing_account`'s folders
whichever account it arrived in, so there is one place to look rather than one
per account. A cross-account move is a copy-and-delete over IMAP: the message
leaves the account it came from.

**Binning does not cross accounts.** A bin is not a filing destination, so a
message binned from Gmail goes to Gmail's own bin.

**Deletion evidence is counted per account.** What you bin in one account
informs proposals in that account only — filing and binning habits differ
between accounts, and a pooled rate would describe neither.

### Gmail

Gmail needs no special handling from you beyond the config above, but two
things are worth knowing.

A Gmail inbox is a *label*, not a mailbox. Apple Mail stores every Gmail
message under `[Gmail]/All Mail` and records inbox membership separately, so
mail-triage reads that membership rather than filtering on the mailbox.

`ignore` keeps Gmail's pseudo-folders out of the filing and deletion counts.
`[Gmail]/All Mail` contains every message in the account, so counting it as a
filing would mean nothing ever looked binned. Note the pattern is
`"[[]Gmail]*"` and not `"[Gmail]*"`: these are fnmatch globs, in which
`[Gmail]` is a character class matching one of g, m, a, i or l — it would
quietly exclude `Accounts`, `Local`, `Invoices` and anything else beginning
with those letters.

## How it decides

Checked in order, highest priority first:

1. **Per-message guards** — a bill, a flagged message, or mail that looks like
   it wants a reply. These override everything below, including your own
   rules: a "bin this sender" rule must not throw away their invoice.
2. **Your rules** — answers you gave when asked about a sender.
3. **The deletion veto** — senders whose recent mail you only ever bin.
4. **Stage A, the sender model.** Which folder this address or domain usually
   goes to, weighted so recent decisions count for more (a filing a year old
   counts half). A sender whose mail is scattered scores low and is not
   proposed.
5. **Stage B, the subject model.** A hand-rolled naive Bayes over subject
   words, tried when stage A cannot call it. This is what separates two kinds
   of mail arriving from the same address.

Stage B deliberately ignores how large a folder is. Filing history is heavily
imbalanced, and weighting by folder size made big folders win on bulk rather
than evidence — measured against 2,568 held-out messages, removing it took
precision from 77.8% to 85.1%, with both more correct answers and fewer
mistakes. Requiring the winning folder to be ten times likelier than the
runner-up takes it to 87.1%.

**Roughly one stage B suggestion in eight is wrong.** They are labelled
`subject tokens …` in the reason, and stepping through with `s` rather than
accepting all is worth it.

To see this applied to one sender — the rule, if any, then stage A's verdict
and the weighted folder counts behind it — use `mail-triage explain <sender>`.
It reads the model and your rules only, and touches no mailbox.

## Development

```bash
uv run pytest -q
```

Tests use synthetic fixtures and a fake Mail interface throughout. No test
touches a real mailbox or shells out to `osascript`.

## Licence

MIT
