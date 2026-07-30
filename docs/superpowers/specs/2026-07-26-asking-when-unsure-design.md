# Asking when unsure — design

**Date:** 26 July 2026
**Status:** implemented 27 July 2026 (bin answer still deferred)
**Extends:** `2026-07-26-mail-triage-design.md` (do-not-file guards; delete as a
first-class choice)

## Purpose

The first live dry run classified 55 inbox messages: 12 filable, 6 vetoed, 7
from senders with no history at all — and **30 from senders the model knows but
cannot call**, because their mail has gone to two or three different folders.
That is the largest group by some margin, and today the tool neither mentions
them nor learns anything from them. They sit in the inbox, and the next run
reaches the same impasse.

These are the cases worth a question. The sender is known, the candidate
folders are known, and the only missing input is the user's intent — which he can
supply in one keystroke and the statistics cannot recover at any price.

**Why asking beats correcting.** Reversing a wrong move costs 8–10 seconds per
message (measured; see the appendix), against a question answered in one
keystroke before anything moves. The asymmetry is smaller than first assumed
but it runs the right way, and it compounds: a correction fixes one message,
whereas an answer fixes every message from that sender for good.

## What gets asked

**Per sender, never per message.** The question is "where does this sender's
mail go?", not "where does this message go?". One answer settles every message
from them, in this run and every future run.

**The leverage is in future mail, not this run — measured, 26 July 2026.** An
earlier draft of this spec claimed the uncertain messages came from "far fewer"
senders, so a handful of answers would clear most of the inbox. That is false.
The 31 uncertain messages came from **24 distinct senders**, at most 3 messages
each — a long tail, not a concentration. Five answers settle only 11 of the 31.

Ranked by how often each sender writes, however, the picture inverts:

| Ranking basis | What 5 answers buy |
|---|---|
| Messages in today's inbox | 11 messages, once |
| Messages sent in the last year | **85 messages per year, permanently** |

All 24 senders together account for 180 messages a year. So the case for asking
is not that it clears the inbox tonight — it will not — but that five questions
a run permanently remove roughly half the annual uncertainty, and the whole
backlog is answered in five sittings.

**Two classes of sender qualify:**

1. **Inconsistent** — known sender, but the weighted folder share falls below
   `confidence_threshold`. The classifier's existing "sender known, but filing
   history is too inconsistent to call" bucket.
2. **Orphaned** — the predicted folder no longer exists in the account. The
   model is confident and its answer is unusable; asking is strictly better
   than silence.

Senders with no history at all are **not** asked about. There are no candidate
folders to offer, so the question degrades into "type a folder path for this
stranger" — high effort, low leverage, and the wrong trade for a feature
optimised for teaching the model.

**Ranked by leverage, capped at five per run.** Leverage is **the number of
messages the sender sent in the last year**, tie-broken by how many are in
today's inbox. The cap is five.

Ranking on the inbox count instead — the obvious choice, and the one this spec
originally specified — is measurably worse: inbox counts are all 1 to 3, so it
is nearly arbitrary, and it would put a sender who wrote 3 times ever above one
who writes 27 times a year. The year window is deliberate: it matches the
`half_life_days` default, and a sender who has gone quiet should not consume one
of five questions.

## The answers

For a sender with candidate folders `Parent/Keep` (12) and `Parent/Reading` (8):

| Answer | Effect |
|---|---|
| **A past folder**, picked by number | Rule: file this sender there. Counts shown so the choice is informed. |
| **A different folder**, typed | Same, but validated against real mailboxes first — a typo must not create a rule pointing nowhere. |
| **Bin these from now on** — **implemented 27 July 2026** | Rule: move to Trash. Journalled and undoable like any other move; **never a hard delete**. Also marks the sender an unsubscribe candidate. Withheld until invoice detection exists — see Decisions taken. |
| **It depends / leave alone** | Rule: never ask about this sender again, never auto-file them. The escape hatch. |
| **Skip** (the default, on Enter) | No rule. Asked again next run. |

The escape hatch is not optional. Some senders genuinely split by content — a
shop sending both order confirmations and marketing — and a per-sender question
with no "it depends" answer would force a bad rule rather than admit the case
exists. "Leave alone" is a real, recorded answer, distinct from "skip".

## What an answer becomes

**A hard rule**, consulted before any statistics. A sender with a rule is filed
at full confidence by the rule; Stages A, B and C never run for them. Rules are
deterministic and predictable, which is what "right the first time" demands.

Rules live in `local/rules.json` — gitignored, human-readable, hand-editable —
and are listed by a `rules` command so they never become invisible magic. A
rule is removed with `rules --forget <sender>`.

```json
{
  "sender@example.com": {
    "action": "file",
    "folder": "Parent/Keep",
    "answered_at": 1785000000,
    "candidates": {"Parent/Keep": 12, "Parent/Reading": 8}
  }
}
```

`candidates` records what was on offer when the question was answered. It is
not consulted at classification time; it exists so a later "you chose Parent/Keep
when it was 12-vs-8, and it is now 12-vs-40" review is possible without
re-deriving history.

## Precedence — the safety-critical part

A rule is an instruction about a **sender**. The do-not-file guards are
judgements about an **individual message**. Messages win.

Ordering, highest first:

1. **Per-message do-not-file guards.** Awaiting a reply or action; invoices
   once implemented. These override every rule, including a bin rule. A billing
   sender under a "bin these" rule must still have its invoice held in the
   inbox — the alternative is the tool binning a bill because the marketing
   from the same address was unwanted.
2. **Hard rules.** File or leave alone (bin deferred — see Decisions taken).
3. **The deletion veto** (senders whose mail is now binned rather than filed).
   A rule is the user speaking directly and recently; the veto is inference from
   Trash contents. The direct answer wins.
4. **Stages A, B, C.** The statistical classifier, unchanged.

**Invoices and bin rules never mix.** A sender flagged as sending invoices must
never be offered as an unsubscribe candidate and never acquire a bin rule, per
the existing invoice requirement. Nothing currently detects an invoice, so that
protection cannot be honoured — which is exactly why bin rules are deferred
rather than shipped with a caveat. The ordering above is written as it will
stand once both exist; until then, item 2 offers no bin.

## Data flow

Asking happens **before** the proposal table, so that answers apply to the
current run rather than only the next one:

```
classify all inbox messages
  → rank uncertain senders, take the top 5
  → ask, collect answers, write rules
  → re-classify the affected messages under the new rules
  → render the proposal table
  → review (accept / reject, as now)
  → execute (journalled, undoable)
```

Re-classification is confined to messages from senders that were just answered
about; nothing else can change, so a full second pass is wasted work.

## Error handling

- A typed folder that matches no real mailbox is re-prompted, not stored.
- Interrupting the questions (Ctrl-C) keeps the rules already answered and
  proceeds to the table with them applied. Answers are written as they are
  given, not batched at the end, so an interrupted session never loses work.
- A rules file that is corrupt or unparseable is an error naming the file and
  the failing line, not a silent fallback to no rules — silently ignoring rules
  would file mail contrary to explicit instructions, which is precisely the
  failure this feature exists to prevent.
- A rule pointing at a folder that has since been deleted is reported and the
  sender is re-asked, exactly as an orphaned prediction is.

## Testing

All against `FakeMail` and synthetic fixtures; no personal literals under
`src/`. Coverage must include: ranking by yearly rate, its inbox-count
tie-break, and the cap; each of the four offered answers; the escape hatch
producing a "leave alone" rule distinct from a skip; rules taking precedence
over the deletion veto; per-message guards taking precedence over a rule; a
corrupt rules file erroring rather than being ignored; and answers applying
within the same run.

One test must assert that **no bin answer is offered**, so the deferral is
enforced by the suite rather than by memory.

## Deliberately excluded

- **Asking about senders with no history.** No candidates to offer; poor
  trade.
- **Rules keyed on anything but the sender address.** Domain rules, subject
  rules and content rules all reintroduce the ambiguity the question exists to
  remove.
- **Automatic rule expiry.** Considered and rejected: a rule that quietly stops
  applying is worse than one that is wrong and visible. `rules` lists them;
  `rules --forget` removes them.

## Decisions taken

Both settled by the user on 27 July 2026:

1. **Ranking is by yearly sending rate**, not inbox count, on the evidence
   above.
2. **Bin rules are held back** until invoice detection exists. The answer is
   designed here and must not be offered by the first implementation: a bin
   rule on a billing sender is precisely the harm the invoice requirement
   names, and there is currently nothing to prevent it. Until then the question
   offers four answers, not five.

   **Lifted 27 July 2026**, once invoice detection shipped. Two protections,
   both tested: a per-message invoice guard outranks every rule, so a bill
   from a binned sender is still held in the inbox; and a sender whose recent
   mail includes anything billing-shaped is never *offered* the bin answer at
   all. 56 senders are flagged as billing on the real mailbox, among them
   a broadband provider, a card issuer, Apple and a retailer.

   Prompted by the user on 27 July 2026, from using the tool: *"it asks for a
   blanket rule for certain emails, but there isn't a blanket rule"*, and
   *"basically there are a lot more deletions"*. Investigating the LinkedIn
   example he gave found the premise was only half right and is worth
   recording: **LinkedIn is nine sender addresses, not one**, and they already
   separate the cases — `invitations@` is 759/765 to `Team/People` and is
   filed at 0.99 without ever being asked about, while
   `notifications-noreply@` is genuinely mixed and mostly binned (20 of 38).
   Address-level splitting handles more than expected, which is why bin rules
   were the right next step and subject-pattern rules were not.

## Appendix: undo speed, measured

Timed against `Team/People` (1,053 messages) on 26 July 2026:

| Operation | Time |
|---|---|
| Bare `count of messages` | 4.0s |
| `whose id is <numeric>` | 7.1s |
| `whose message id is <RFC-822 key>` | 8.3s |

The two lookups are within ~15% of each other, so the cost is the `whose` scan
itself, not header reading — **the hypothesis that switching to numeric-id
lookup would make undo nearly free is wrong.** Any `whose` query over a large
mailbox costs seconds, and that is the floor for AppleScript.

So undo is roughly 8–10s per message on a large destination folder, not the two
minutes the first live undo took; that run was most likely a cold Mail cache. A
twelve-message batch would reverse in a couple of minutes, which is tolerable.
**Recommendation: leave the implementation alone** and re-measure a warm undo
before spending effort here.

**Re-measured 27 July 2026, as recommended: 3.9s** for a one-message undo out
of `Deleted Messages`. Faster than the 8–10s estimate, which was taken against
`Team/People` (1,053 messages) — the cost scales with the size of the mailbox
being scanned, and the estimate should be read as an upper bound for large
folders rather than a flat per-message rate. Nothing here needs attention.

Worth knowing if it ever does need attention: the RFC-822 key resolves to a
message's current ROWID and mailbox via SQLite in milliseconds (see the
corrected environment facts in the plan), so *locating* a message is cheap.
Only *addressing* it through AppleScript is slow, and no way around that has
been found.
