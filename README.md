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
| `web` | Triage in a browser, on 127.0.0.1 only |
| `web --no-open` | Print the URL instead of launching a browser |
| `web --port N` | Listen on another port (still loopback) |
| `rules` | List the answers you have given about senders |
| `rules --forget <sender>` | Remove one sender's rule |
| `rules --never-personal <sender>` | Vouch that a sender never awaits a reply |
| `rules --forget-never-personal <sender>` | Withdraw that |
| `explain <sender>` | Show why that sender's mail goes where it does |
| `unsubscribe` | List lists worth leaving; unsubscribe from the ones you pick |
| `unsubscribe --dry-run` | List the candidates; send nothing |
| `unsubscribe --limit N` | How many senders to fetch headers for (default 20) |
| `unsubscribe --sender TEXT` | Only offer senders whose address contains TEXT |
| `unsubscribe --check` | Report bounces for the last batch instead of sending |
| `runs` | List runs that can be undone |
| `undo [run-id]` | Reverse a run, defaulting to the most recent |

### In a browser

`mail-triage web` runs the same triage pass and serves it to a page you can
click through, with the reasoning shown beside every message.

```
$ mail-triage web
Serving 15 messages on http://127.0.0.1:8765
Opening your browser…   (ctrl-C to stop)
```

It binds `127.0.0.1` and nothing else, so it is reachable only from this
machine. The URL carries a token that works **once**: the page trades it for
a header token and drops it from the address bar, so the URL left in your
scrollback is worthless afterwards. The server stops on Ctrl-C, or after
thirty minutes with no request — it can move mail, and should not still be
listening tomorrow morning.

Nothing moves until you press Apply, and what moves is journalled and
undoable exactly as a terminal run is. Mail a guard held back cannot be filed
from the browser at all; the guards are enforced on the server, not in the
page.

#### What a message shows

Each row carries the sender, the subject, where it is going, and *why* — the
same reasoning the terminal prints, in the same words. A confidence figure and
a small meter sit on the right, with the intent spelt out beside them ("will
file", "will bin") so no meaning rests on colour alone.

A message a guard held back says so instead, in place of the destination: what
the guard was, and the folder it *would* have gone to, struck through. Those
rows are the ones to read.

#### Deciding

Every filable message gets a folder box, not only the ones with nowhere to go.
It is a typed, fuzzy-matched list of your real folders with the expected one
already at the top, so Return accepts the proposal and typing is only needed
to disagree. A folder you type over a proposal is recorded as a correction and
weighted ten times ordinary history at the next `learn` — answering once
teaches the model rather than moving one message.

What a held message offers depends on why it was held, mirroring the rules the
server enforces:

- **Held because you keep binning this sender** — Bin, on one click, and File
  if there is somewhere to file it. Binning is the obvious answer here, not an
  override.
- **Held as a possible bill, or as possibly awaiting a reply** — filing only,
  once, and it asks first by name. Never binning, and never from the keyboard.

#### The keyboard

The whole page works without a mouse. Two axes: up and down move between
messages, left and right along the controls of the message you are on.

| Key | What it does |
|---|---|
| `↑` `↓` or `k` `j` | Move between messages |
| `←` `→` | Move along that message's controls |
| `f` | Open the folder box (it does not file blind) |
| `b` | Bin — where the button exists and asks nothing |
| `s` | Skip |
| `⌘↵` | Apply, from anywhere, including mid-word in the folder box |
| `esc` | Close the folder box, or answer "leave it" to a confirmation |

A keystroke presses the message's own button, so it can only ever do what a
click would, and a button that stops to ask has no key at all. In a
confirmation, `L` leaves the mail alone and `F` files it; Escape, the backdrop
and the default focus all mean "leave it".

**The unsubscribe panel** ranks lists by what you bin without reading. A
`mailto` list is left by sending the request, as the terminal does. An
`https` one opens the sender's own page in a sandboxed frame — no cookies, no
storage, and it cannot see anything else on the page. It is still their code,
and loading it tells them your address is live. Plenty of providers refuse to
be framed at all, so there is an "open in a tab" link beside it.

mail-triage itself still makes no outbound HTTP request of any kind: the
frame is your browser's request, made on your click.

## Getting less mail

`unsubscribe` is the only command that sends anything. It prints the senders
whose mail you most reliably ignore, and you choose from the list:

```
 1  news@list.example    iCloud  22 binned, 0 unread  96%   mailto
 2  offers@shop.example  iCloud  19 binned, 1 unread  100%  mailto
 3  digest@site.example  Gmail    8 binned, 0 unread  100%  http — open in a browser yourself

3 candidates, 2 of them sendable (1 is HTTP-only — open those yourself).
Which to unsubscribe from? (numbers, or Enter for none): 1,2
```

Pick several at once — `1,4` or `1-3`. The selection is shown back to you and
confirmed as a set before anything is sent, and Enter on its own sends nothing.

**Deleted mail is the main evidence.** Unread-in-the-inbox only sees what you
have not got round to; the mail you binned is a decision, and it has already
left the inbox where an unread count would find it. Deletions are counted
within each account, over the same recent window the filing veto uses, so a
sender you bin in one account and read in another is not mistaken for noise.

`--sender` narrows the list to one address. Prefer it over answering the
first prompt when you mean to send exactly one: the ranking moves as mail
arrives, so position is not a reliable way to aim.

Nothing is sent for a sender you did not name, the confirmation defaults to
no, and `--dry-run` sends nothing at all. Only `mailto:`
unsubscribe targets are used; HTTP one-click unsubscribe is deliberately
unsupported, as it would mean firing arbitrary web requests at an address the
sender chose. Fetching the `List-Unsubscribe` header costs an AppleScript
round trip per sender, which is why `--limit` exists and why the ranking
happens before the fetching.

**A sent request is not a completed unsubscribe.** The provider may reject it,
and the rejection arrives seconds later as a bounce from `mailer-daemon` — the
first live send was rejected that way and reported success.

`mail-triage unsubscribe --check` reports whether the last batch bounced. A
run already looks once before it finishes, but a rejection can take longer
than the run does, so check again shortly afterwards:

```
$ mail-triage unsubscribe --check
Batch 2026-08-06T19-48-56, 2 requests sent from iCloud.

  news@list.example    leave@list.example   bounced         "Delivery Status Notification (Failure)"
  offers@shop.example  unsub@shop.example   no bounce seen

A bounce names the reason in its body, which this tool does not read.
"No bounce seen" is not confirmation: a request can be accepted and ignored.
```

Two things it will not tell you. It reports *which* request bounced, not
*why*: the SMTP diagnostic lives in the bounce's `message/delivery-status`
body part, and this tool does not read message bodies. And it never reports
a request as delivered — a silently discarded request looks exactly like an
accepted one from here.

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

That label is also why a Gmail source wants an `archive`:

```toml
archive = "[Gmail]/All Mail"
```

Filing a Gmail message into another account's tree *copies* it and leaves the
inbox label alone — Mail reports success whilst the message sits exactly where
it was, to be filed again, and copied again, on the next run. Moving the
leftover to `[Gmail]/All Mail` is what removes the label; it is Gmail's own
word for archiving, and it works because it is a move *within* the account.
mail-triage does this on every crossing and then checks, and a move it cannot
prove is reported as a failure rather than counted as a success. Without an
`archive` set there is nothing to clear the label with, so filing out of Gmail
into another account is reported as a failure instead.

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
