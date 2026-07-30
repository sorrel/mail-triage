# A second account: triaging Gmail alongside iCloud — design

**Date:** 29 July 2026
**Status:** designed
**Extends:** `2026-07-26-mail-triage-design.md` (precedence, guards, journal)

## Purpose

The tool triages exactly one account. The user has a Gmail account in Apple
Mail whose inbox never gets the same treatment, and asked for it to be "gone
through too — exactly the same as we have done for iCloud, going to the same
places".

That last clause is the whole design. Gmail is not a second filing system to
be learnt separately; it is a second *source* of inbox mail that flows into
the one filing tree that already exists.

## What the mailbox actually looks like

Measured against the live Envelope Index, 29 July 2026. Three findings shaped
the design, and the second one overturned the first plan.

**1. A Gmail inbox is not a mailbox.** Every message in the Gmail account has
its `messages.mailbox` pointing at `[Gmail]/All Mail`. Inbox membership lives
in a separate table, `labels(message_id, mailbox_id)` — Apple Mail models
Gmail's labels properly rather than pretending they are folders. The current
inbox scan filters on the mailbox URL, so **it sees the Gmail inbox as empty**
and would triage nothing at all.

Joining `labels` to `messages` yields 9 inbox messages, which is exactly what
Mail itself reports. The raw table holds 11 rows for that mailbox; two are
stale, pointing at messages no longer present. The join discards them for
free, so the count agrees with Mail without any explicit staleness handling.

**2. There is nothing to learn from and nowhere to file.** The account's four
user labels are all empty — zero messages between them. Contents are All Mail
303, Bin 70, Spam 3, and Important 287 (a label, not a folder). Only about
three months are retained locally.

So Gmail contributes no training data, and it has no folder tree to file
into. The classifier is trained on the iCloud account's 52 folders and would
otherwise propose destinations that do not exist in Gmail.

**Decision:** Gmail inbox mail is filed **into the iCloud tree**. Filing is
cross-account. The alternative — creating labels in Gmail on demand to mirror
iCloud's structure — was rejected: it makes the tool start creating mailboxes,
a new class of write, and it splits filed mail across two trees when the
user's stated aim is one place to look.

**3. Housekeeping differs.** The bin is `[Gmail]/Bin`, not `Deleted Messages`,
so the trash name must become per-account. The account also carries two junk
mailboxes with a missing separator, `[Gmail]All Mail` and `[Gmail]Bin`, both
empty; excluding them costs nothing.

## Architecture

### Config: one filing target, several sources

`Config` loses its single `account_url_prefix` / `inbox_folder` /
`trash_folder` triple in favour of a filing target plus a list of sources:

```toml
filing_account        = "iCloud"          # name as Mail shows it
filing_account_prefix = "imap://AAAAAAAA" # supplies the folder list

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
```

`training_accounts` is unchanged and lists iCloud only. The candidate folder
list comes solely from `filing_account_prefix`, so both sources are classified
against one set of destinations.

A config naming a single source must behave exactly as today. This is the
migration path and it is worth a test of its own.

### Reading an inbox

`envelope.py` gains one method rather than altering `messages_in_mailbox`,
keeping the change away from the corpus and deletion paths:

```python
def inbox_messages(self, url: str) -> Iterator[MessageRow]:
    """Messages in a mailbox by primary attribution or by Gmail label."""
```

It unions the messages attributed to the mailbox with those joined to it
through `labels`. The method is generic, not a Gmail special case: an account
with no label rows contributes nothing to the second half of the union, so
iCloud's behaviour is bit-for-bit what it was.

### Identical treatment, per account

"The same as iCloud" means each source is put through the same pipeline, not
that the two share state. Two things are therefore per-source, and are
symmetrical rather than exceptional:

- **The deletion veto.** `build_deletion_index` runs once per source, and a
  message is judged against its own account's index. Gmail's 70 recent
  deletions inform Gmail proposals; iCloud's inform iCloud's. Pooling them by
  sender was considered and rejected — a sender filed in one account and
  binned in the other would produce a veto that reflects neither habit.
- **Binning.** A "delete" answer sends the message to its own source's trash.
  A bin is not a filing destination, so it never crosses accounts.

Everything else — guards, rules, precedence, thresholds, the asking loop — is
untouched and applies uniformly. Precedence is unchanged.

### Moving mail

`mail_app.move_message` splits its `account` parameter into `source_account`
and `target_account`, generating `move … to mailbox "X" of account "iCloud"`.

`execute()` takes the source account per decision instead of one value for the
batch. The journal needs no format change: `from_account` and `to_account`
were added in commit `d1dbd62` for precisely this, and default to `""` so
existing journals still load.

`undo_run` locates a message in `to_folder` of `to_account` and returns it to
`from_folder` of `from_account`.

### Review table

`render_table` gains an Account column, shown only when more than one source
is configured, sized with `display_width()` rather than `len()`.

## Risks

**Cross-account moves are copy-plus-delete over IMAP.** They are slower than
a move within an account, and a half-failure leaves a duplicate rather than a
loss. Undo is unaffected: it keys on the RFC-822 Message-ID, which survives
the copy.

**The Gmail copy does not stay in All Mail.** Moving a message to another
account removes it from Gmail, rather than merely unlabelling it as an
in-account move would. This is asserted from how IMAP moves work, *not*
verified — verifying it needs a live run, which the safety rules forbid. It is
therefore the first thing the live checkpoint establishes, on one message.

**`[Gmail]` is a glob character class.** `is_excluded` uses `fnmatch`, in
which `[Gmail]` matches a single character from `G m a i l`. The correct
pattern is `[[]Gmail]*`, which needs a comment in `config.example.toml` and a
test.

**Corrected 30 July 2026, after measuring.** This section previously claimed
the naive `[Gmail]*` would match `G/anything` whilst missing every real Gmail
folder. That is wrong, and the truth is worse. `[Gmail]*` **does** exclude
`[Gmail]/All Mail` — via the `a` of the leaf name `All Mail`, not the bracket
— so it looks like it works. Meanwhile it also excludes every folder whose
leaf begins with g, m, a, i or l: `Accounts`, `Local`, `Invoices`, `Music`,
and so on. The failure mode is not an unexcluded All Mail but a large,
silent hole in the training corpus.

Getting this wrong is not cosmetic in either direction: `[Gmail]/All Mail`
holds every message in the account, and left in the corpus it would teach the
model that all mail is filed there.

## Testing

All against synthetic fixtures and `FakeMail`; nothing touches a real mailbox.

- `inbox_messages` finds label-only members, ignores stale label rows, and
  returns the same result as before for a mailbox with no labels
- `[[]Gmail]*` excludes the bracketed folders and does not match a plain
  `Gmail/...` path
- deletion indices are built per source and do not leak between them
- a cross-account decision journals distinct `from_account` / `to_account`,
  and undo returns the message to the source account
- a single-source config behaves exactly as the current one does

`FakeMail` needs to model two accounts to support the last three.

The live checkpoint, when the suite is green: one Gmail message, dry run
first, then a single move, verify, undo, verify.

## Out of scope

- Creating labels or mailboxes in any account
- The second Gmail account present in Mail, and the other five accounts
- Gmail API access; everything continues to go through Apple Mail
- Pooling training data or deletion evidence across accounts

## Follow-on task

`tests/test_no_personal_data.py` scans `src/` only, whilst CLAUDE.md rule 5
covers `src/`, `tests/` and `docs/`. This spec was written to that rule by
hand. The guard should be extended to match the rule it claims to enforce.
