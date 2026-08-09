# What to do next, and in what order

9 August 2026, after the browser interface landed (#24).

Ordered by risk, not by size. The first two are the only ones where leaving
them undone can cost you mail or privacy; everything below is tidying and
comfort.

---

## 1. Prove the Gmail archive step against real mail — **done** (#25)

Run on 9 August 2026 against one message that was already filed, whose
leftover was sitting in the Gmail inbox. One move through the shipped code,
`INBOX` → `[Gmail]/All Mail`, matched on the durable Message-ID:

    before   INBOX=True   All Mail=True
    after    INBOX=False  All Mail=True

The label is cleared, the message is intact, nothing was copied. Filing a
Gmail message into the iCloud tree now completes instead of duplicating.

Proving it exposed a hole worth remembering: `FakeMail` left the original on
*every* move, so the archive could never clear it either and only the
failure path had coverage — a test double disagreeing with reality, and a
test written to match the double. The fake now models the real boundary
(cross-account leaves the label, same-account does not), and the success
path is covered end to end.

**Still unexercised live:** the full path in one go — file a Gmail message
and watch it land once with nothing left behind. The remaining Gmail inbox
message is a build-failure notice better binned than filed, so this will
happen naturally on the next Gmail message worth keeping. It will now either
work or report a failure honestly; it can no longer duplicate silently.

---

## 2. Security review of the code, not the design

**Why second:** the review in `2026-08-09-web-ui-review.md` was of a design
and a plan. What shipped is a network-listening process that moves and
deletes mail, and it has never been reviewed as written code.

The review found real problems when there was nothing but a document. There
is now ~1,400 lines of it, some written quickly under a stream of live
feedback, and four of the bugs found today were found by *using* it rather
than by reading it.

Run `/code-review high` (or `/code-review ultra` for the multi-agent pass)
over the merged diff. Worth particular attention:

- `web/security.py` — the api/static split was written twice, once wrongly
- `web/routes.py` — `_permitted` encodes the precedence rules; a mistake
  there files mail contrary to a guard
- the lock in `_decisions`, which was added after a live failure

---

## 3. Tidy the duplicates already created

Read-only inventory taken, nothing deleted:

- `📖 [The CloudSecList] Issue 350`, Message-ID
  `0102019fe68457a6-…@eu-west-1.amazonses.com`
  - **3 copies** in the newsletters folder — 2 surplus
  - **1 still in the Gmail inbox**

No other message is affected: every other move on 9 August was
same-account and clean. Deleting real mail needs confirming one message at
a time, so this is a short deliberate session rather than a script.

---

## 4. Correct two misleading journal runs

`2026-08-09T14-31-07` and `2026-08-09T14-31-08` record `failed` for
messages that really moved, because two runs shared a journal file before
`new_run_id` was fixed. The mail is where it should be, so this costs
nothing today — but `mail-triage undo` on those runs would skip messages it
should reverse, and the entries will outlive anyone's memory of why.

Either annotate them or leave a note beside them. Not worth code.

---

## 5. Fold in today's corrections

Several folder choices were recorded as corrections and are weighted 10×
against plain history — but only at the next `mail-triage learn`. Until
then the model still proposes what it proposed before, and the same
corrections get made again.

    mail-triage learn

Cheap, and it makes the rest of the tool feel less repetitive.

---

## 6. Keyboard parity for the Mailing lists dialog

The confirmation got arrows and letter shortcuts; the unsubscribe panel did
not, and it has the same limitation for the same reason — the page's
shortcuts stop at an open dialog. Tab and Escape work. Arrows to walk the
candidates and Return to act would finish the keyboard story.

---

## 7. Smaller things, in no particular order

- **`--ask` in the browser.** Deliberately out of scope for #24: the sender
  questions are a conversation with their own design, and the terminal does
  them well. The folder box covers correcting one message.
- **The bounce check in the browser.** Also deliberately out: `unsubscribe
  --check` reads the same send log, so a browser send is already covered
  from the terminal.
- **`aria-owns` on the folder combobox**, so the match list is announced as
  belonging to the input rather than as a stray list.
- **A second look at the aesthetic** now there is real mail in it — the
  review's judgements were made against a mockup.

---

## Deliberately not doing

- **HTTP one-click unsubscribe performed by the tool.** Still refused. The
  iframe is the browser's request, made on a click.
- **Reading message bodies.** Never done, and rendering sender HTML is the
  largest single risk this design could take on.
- **Serving to anything but loopback.**
