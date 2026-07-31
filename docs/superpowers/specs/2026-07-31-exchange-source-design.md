# A third account: triaging an Exchange mailbox — design

**Date:** 31 July 2026
**Status:** designed
**Extends:** `2026-07-29-gmail-source-design.md` (several sources, one filing tree)

## Purpose

The user asked for a third account — an Exchange/Outlook mailbox — to be
triaged alongside the other two.

The multi-source architecture built for Gmail already generalises to it. This
design is therefore mostly a confirmation that nothing new is needed, plus one
latent bug the account exposes, plus a deferred requirement recorded with the
evidence that makes it hard.

## What the mailbox looks like

Measured against the live index, 31 July 2026. 14 mailboxes, 349 messages, an
inbox of 10.

**It has none of Gmail's awkwardness.** `Inbox` is a real mailbox with its
messages attributed to it, and the account has zero rows in the `labels`
table, so `inbox_messages` already reads it correctly and reports the same
count as Mail itself. The `ews://` scheme needs no handling: `account_prefix`
splits on `://` and does not care what precedes it. The inbox lookup is
case-insensitive, so `Inbox` matches whatever case the config uses.

**Housekeeping names differ.** The bin is `Deleted Items`, junk is
`Junk Email`, and there is a `Conversation History` folder the mail client
maintains by itself.

**It has real filing history** — four populated folders holding 273 messages,
plus an `Archive`. Gmail had none, which is what forced cross-account filing
there. This account genuinely has somewhere of its own to file into.

## Decisions

**Filing goes to the filing account, as Gmail's does.** One filing tree
remains the rule; a message arriving here is filed into the filing account's
folders. This was chosen over per-account filing for consistency, in the
knowledge that the account has folders of its own.

**Training is unchanged.** `training_accounts` still names the filing account
only. This account's 273 filed messages are a rounding error against the
filing account's ~19,000, and its folder names do not exist in that tree.

## Architecture

### Config — one source block

```toml
[[source]]
name   = "Exchange"          # the name the mail client shows
prefix = "ews://CCCCCCCC"
inbox  = "Inbox"
trash  = "Deleted Items"
ignore = ["Conversation History"]
```

### Code — one word

`_IGNORED_FOLDER_PATTERNS` in `deletion.py` changes `"Junk"` to `"Junk*"`.

This is a latent bug rather than a quirk of one mailbox. The entry exists to
stop junk mail counting as a filing decision, and `Junk Email` is the standard
Exchange name for that folder — which the pattern misses. Left unfixed, every
spam message counts as evidence that the user *files* that sender's mail,
inflating the filed side of the deletion ratio and suppressing the veto that
exists to catch senders whose mail is only ever binned.

Measured against the folder set of this account:

| Folder | Before | After |
|---|---|---|
| `Junk Email` | counted as **filed** | ignored |
| `Deleted Items` | deleted (matches `Deleted*`) | unchanged |
| `Sent Items` | ignored (matches `Sent*`) | unchanged |
| `Inbox` | ignored | unchanged |
| `Conversation History` | counted as filed | ignored, via the source's `ignore` |

`Conversation History` stays per-source rather than joining the shared list:
the `ignore` field exists precisely for folders that represent no filing
decision in one particular account.

Nothing else changes.

## Deferred: keeping some mail in its own account

The user's requirement, stated after seeing the folder list: two of this
account's folders — one holding a hardware vendor's mail, one holding a rail
operator's — should keep receiving mail locally rather than having it filed
into the shared tree. **Except travel tickets**, which should go to the shared
tree's travel folder like everything else.

This is **not** implemented here. It is recorded because the evidence for it
was gathered now and is the hard part of designing it later.

**Per-account, the vendor habit is real.** Across all accounts, roughly 2,900
of that vendor's messages sit in the filing tree's own folders against 121 in
this account's vendor folder — but within *this* account the filing is clean:
121 of that folder's 124 messages are from the vendor, and almost none of its
mail goes elsewhere here. So the requirement is about the account a message
*arrives* in, not about the sender globally.

**The travel exception cannot be learnt.** The rail-operator folder holds 115
messages: 51 claims, 39 booking confirmations and tickets, 3 smartcard, 4
refunds, 18 other. The shared tree's travel folder holds 1,990: 695 booking
confirmations and tickets, 163 claims, 1,127 other. **The history is mixed in
both directions** — 39 tickets are already filed locally and 163 claims are
already filed centrally. No amount of training separates them, because both
patterns exist. It is a statement of future intent, not a pattern.

Worse, both kinds come from one sender. Stage A maps a sender to a folder and
structurally cannot split one sender across two destinations. Only the subject
distinguishes them.

**The chosen mechanism, for the follow-up spec:** explicit per-source rules —
rules gain a destination account, plus a content qualifier for the exception.
That is a real extension to the precedence model, which is the safety-critical
part of this tool, so it warrants its own design rather than being bolted on
here.

**Interim behaviour, and its risk.** Until that lands, this account's vendor
and rail mail *will be proposed into the shared tree* — the opposite of what
the user wants for it. Nothing moves without a keystroke, so the mitigation is
to reject those at the review prompt. A temporary "leave alone" rule is not a
clean substitute: rules key on exact sender addresses, and the vendor alone
sends from six domains.

## Testing

All against synthetic fixtures; nothing touches a real mailbox.

- `Junk*` matches `Junk Email` and `Junk`, and a per-source deletion index
  counts neither as a filing
- a folder named in a source's `ignore` is not counted as a filing
- an `ews://`-style prefix resolves through `account_prefix` and
  `source_for` exactly as an `imap://` one does
- a source whose inbox is spelled `Inbox` is found by a config saying `INBOX`,
  and vice versa

Then a dry run across all three accounts, confirming the third inbox is found
and nothing moves.

## Out of scope

- Keeping any mail in its own account (deferred above)
- Training on this account
- The other accounts present in the mail client but not triaged
- Stage B's stopword weakness, which has its own pending design
