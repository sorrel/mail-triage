# Folder size report — design

**Date:** 1 August 2026
**Command:** `mail-triage size`
**Status:** agreed, ready for an implementation plan

## The problem

Apple Mail's local store is large — on the mailbox this was designed against,
8.5 GB under `~/Library/Mail/V10/` — and Mail itself offers no way to see
which folders account for it. Deciding what to prune, or even whether pruning
is worth the effort, currently means reaching for `du` and reading raw UUID
directory names that say nothing about which account or folder they are.

`mail-triage size` answers "where has my disk gone" in the vocabulary of
mailboxes rather than paths.

## Two measures, deliberately both

The report shows two numbers per folder, because they answer different
questions and the gap between them is itself informative.

**Envelope size** is `SUM(messages.size)` from the envelope database: the
RFC-822 byte size of every message Mail knows about, including messages whose
bodies were never downloaded. It is complete across every account and costs a
single grouped query.

**Disk size** is what the folder actually consumes on the volume, measured by
walking the `.mbox` tree.

Where they disagree substantially, that is a fact about the account, not a
defect in the report: an IMAP account configured not to download message
bodies will show envelope bytes far exceeding disk bytes. An account with no
local directory at all shows a **blank** disk column — never a zero, which
would read as "this folder is empty" rather than "not stored on this Mac".

### Why block-based sizing

Disk size sums `st_blocks × 512`, matching `du`, not `st_size`. A mail store
is tens of thousands of small `.emlx` files; apparent size understates real
consumption substantially once block rounding is accounted for. The point of
the column is what the volume has actually committed.

## How the two trees are joined

The database records mailboxes as URLs of the form
`<scheme>://<account-uuid>/<url-encoded/path>`. On disk, the same mailbox is a
directory under `V10/<account-uuid>/`, with each path component suffixed
`.mbox` — a database path of `Parent/Child` is `Parent.mbox/Child.mbox` on
disk. This correspondence was verified against a real store before the design
was settled, and it is exact.

Each `.mbox` directory also contains a UUID-named subdirectory holding the
message data itself. Those bytes count as the folder's own.

**A folder's own disk bytes exclude its child `.mbox` directories.** The
roll-up then adds up, rather than counting nested folders twice.

Accounts present in the database but with no directory on disk (dormant or
server-only accounts) are reported with counts and envelope sizes, and a blank
disk column.

## Output

A summary grid of every account by total size, largest first. Then one grid
per account: folders as an indented tree, sorted largest first at each level,
with roll-up totals on parents. Then a final grid for `MailData`.

Columns: folder, message count, envelope size, disk size.

Larger values are highlighted in yellow. The threshold is a share of the
account's total rather than a fixed byte count, so the highlighting carries
meaning in a 20 MB account as well as a 5 GB one.

Column widths are computed with `review.display_width()`, never `len()` —
the house rule, because the tree indent and any emoji occupy two columns.

### MailData

`~/Library/Mail/V10/MailData/` holds no mail. It is Mail's own bookkeeping:
the envelope database, search and junk-filter indexes, a cache of remote
images from HTML mail, and settings. On the reference mailbox it is 383 MB of
the 8.5 GB total, dominated by the envelope database itself.

It gets its own grid, listing its contents by disk size. The message-count and
envelope-size columns are blank there, because neither applies. Omitting it
would be worse than the small inconsistency: without it the accounts do not
sum to the figure Finder reports, which invites the reader to distrust every
other number on the screen.

### Small folders

Folders below `--min-size` (default 2 MB) are collapsed into a single
`N smaller folders — X MB` line at the end of each account's tree.

That line is not cosmetic. Without it the visible rows would silently fail to
reconcile with the account total, and a total that does not add up is worse
than a long list.

## Options

| Option | Default | Effect |
|---|---|---|
| `--min-size` | `2MB` | Collapse folders below this into the roll-up line. `0` shows everything. |
| `--account NAME` | all | Restrict to one account. Matched case-insensitively as a substring of the account name shown by `mail-triage accounts`, or against its prefix. An ambiguous match is an error listing the candidates, never a silent pick. |
| `--bytes` | off | Exact byte counts instead of human-readable sizes. |

## Modules

Measurement is kept separate from rendering, so each can be tested without the
other.

| Module | Responsibility |
|---|---|
| `sizes.py` | Walk the `.mbox` tree; merge with envelope aggregates; emit the account/folder tree |
| `size_report.py` | Render the grids: layout, units, colour |

One addition to the existing reader:

`EnvelopeReader.mailbox_sizes() -> list[tuple[str, int, int]]` — `(url,
message_count, total_size)` per mailbox, from a single `GROUP BY`. It follows
`account_summary()` in shape and placement.

### Data model

```python
@dataclass(frozen=True)
class FolderNode:
    path: str                  # full folder path, as in the database
    name: str                  # leaf name, for display
    own_disk_bytes: int | None # None when the account has no local store
    envelope_bytes: int
    message_count: int
    children: tuple[FolderNode, ...]
```

Totals are derived by recursion rather than stored, so a node cannot fall out
of agreement with its children.

```python
@dataclass(frozen=True)
class AccountUsage:
    prefix: str            # e.g. scheme://AAAAAAAA
    name: str              # resolved via accounts.resolve_account_name
    root: FolderNode
    on_disk: bool          # False → blank disk column throughout
```

## Gmail labels

A Gmail message's `messages.mailbox` points at All Mail whatever labels it
carries. For sizing this is the behaviour we want: each message is counted
once, against the mailbox that actually stores it. The report therefore uses
the plain mailbox join and **not** `inbox_messages`, whose label union exists
to answer a different question and would double-count here.

## Safety

Read-only in the strongest sense available. The command:

- snapshots the envelope database and opens the copy `mode=ro`, as every other
  read path in this tool does;
- calls `os.stat` and directory listing on the mail store, and reads no file
  content;
- issues no AppleScript and moves no message.

There is no live-run risk to manage, which is why this command needs no
dry-run flag.

Unreadable directories are skipped and counted, with a note at the end of the
report rather than a crash — a permissions gap should not cost the whole
report. Missing Full Disk Access is reported with the same guidance the
`accounts` command already gives.

## Testing

Synthetic fixtures throughout; no test reads the real mailbox.

- A temporary directory of fabricated `.mbox` trees, with known file sizes,
  covering: nesting, a folder whose children dominate its total, an account
  directory that does not exist, and an unreadable directory.
- A small SQLite envelope fixture, as elsewhere in the suite, for the
  aggregate query.
- Rendering tested against a constructed tree, asserting the roll-up
  arithmetic, the collapse line's arithmetic, blank-versus-zero handling, and
  that widths come from `display_width`.

The arithmetic assertions are the important ones: every visible row plus the
collapse line must equal the account total, and each parent must equal its own
bytes plus its children's.

## Out of scope

Deleting or archiving anything. This command reports; it does not prune. If
pruning follows, it is its own design, with its own approval gates.
