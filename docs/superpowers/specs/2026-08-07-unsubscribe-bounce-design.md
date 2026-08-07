# A reported send is not a completed unsubscribe

Design for Task 20 of the mail-triage plan. 7 August 2026.

## The problem

The first live unsubscribe went out on 6 August 2026. The tool printed
`sent` in green and moved on. Eighteen seconds later the provider rejected
it — `554 Message rejected: The unsubscribe request has invalid form` — as
a bounce from `mailer-daemon` that nothing was watching for. It was found
only by going looking, by hand, because someone happened to be suspicious.

The tool currently ends its send loop with advice:

> A sent request is not a completed unsubscribe: a rejection comes back as
> a bounce moments later. Check your inbox for mailer-daemon.

That advice is correct and it is not enough. A batch of ten can report a
perfect score with all ten requests rejected, and nothing in the tool will
ever say otherwise. The reporting is not merely incomplete, it is
confidently wrong in the direction that matters: it says the job is done.

## What this adds

1. A record of what was actually sent — there is currently none at all.
   `send_unsubscribe` fires and returns `None`.
2. A check that looks for bounces against that record and reports what it
   finds, including what it cannot account for.

## Scope, and what is deliberately left out

**In:** the send log; bounce identification; attribution of a bounce to a
request; a `--check` flag; one free look at the end of a send run.

**Out, by decision:**

- *Reading message bodies.* The SMTP diagnostic (`554 …invalid form`) lives
  in the DSN's `message/delivery-status` body part, not in any header. This
  design therefore reports **that** a request bounced and **which** one, and
  cannot report **why**. Reading bodies to recover the reason would widen
  what the tool touches to the most sensitive content in a mailbox, for a
  string the user can read themselves by opening the message. Not worth it.
- *Waiting inside the send loop.* A minute's pause per batch to catch a
  bounce that may take longer anyway. The plan called this unpleasant and it
  was right.
- *Feeding outcomes back into the candidate ranking.* A sender you
  unsubscribed from stays an ordinary candidate until its mail actually
  stops, which is already correct — the request may have been accepted and
  ignored. The log makes annotation possible later; this design does not do
  it.
- *HTTP one-click.* Task 21, undecided.

## Architecture

Two new modules, one job each:

| Module | Responsibility |
|---|---|
| `sends.py` | The record of what was sent. Append-only JSONL, one file per batch, under `local/unsubscribe-sends/`. |
| `bounces.py` | Identify bounces, attribute them, and render the report. Pure functions over `MessageRow`s and header dicts — no snapshotting, no AppleScript, no config. |

`bounces.py` holding no I/O is what makes the matching rules — the
safety-critical part — testable against synthetic fixtures without a
mailbox anywhere near them.

The send log is one file per batch rather than one rolling file, mirroring
the journal's convention so that "the last batch" is simply the newest
filename. It does **not** reuse `JournalEntry`, which is move-shaped
(`from_folder`, `to_folder`, `message_key`) and would carry four unused
fields to describe something that is not a move.

### Changes to existing modules

- **`mail_app.py`** — `send_mail` returns the account name it sent from
  instead of `None`. Sends go from *Mail's default account*, which need not
  be any configured source; assuming otherwise would mean searching the
  wrong inbox. The outgoing message is already in hand in the script, so
  this is one extra value out of a round trip already being made.
  `MailInterface` and `FakeMail` follow.
- **`unsubscribe.py`** — `send_unsubscribe` returns a `SentRequest` rather
  than `None`. Candidate finding and ranking are untouched.
- **`envelope.py`** — `MessageRow` gains `date_received`. The window must be
  measured on our clock: `date_sent` belongs to the bouncing daemon, and a
  relay with a skewed clock would place its bounce outside any window
  computed from it. One column on `_BASE_QUERY`, defaulted so existing
  callers are unaffected.
- **`cli.py`** — the send loop writes the log and takes one free look; a new
  `--check` flag re-reads the last batch.

## Data shapes

```python
@dataclass(frozen=True)
class SentRequest:
    sender: str        # the list itself, normalised: "news@retailer.example"
    to_address: str    # where the request went
    subject: str       # what we sent — the subscriber token, where there is one
    sent_at: int       # epoch seconds, our clock
    from_account: str  # captured from Mail, not assumed


@dataclass(frozen=True)
class Bounce:
    rowid: int
    subject: str
    received_at: int
    failed_recipient: str | None   # X-Failed-Recipients, when present
    request: SentRequest | None    # None means unattributed
```

`Bounce.request is None` is load-bearing. It makes "a bounce I cannot tie to
anything" a representable state, so the renderer is obliged to deal with it
rather than the matcher quietly dropping it and the run looking cleaner than
it was.

## Data flow

### Sending

Selection and confirmation are unchanged. After each **successful** send,
one line is appended to the batch file.

Recording *after* the send is deliberately the opposite of the journal's
record-then-act discipline, and the reason is specific to this task. A
record written before the send describes a request that might never have
gone out; `--check` would find no bounce for it and report it as fine —
reintroducing the exact false clean bill of health this task exists to
abolish, in a new place. Losing a record to a crash between send and write
merely returns that one request to today's behaviour. A send that raises is
not recorded, because nothing went out.

The run then takes one free look at whatever has already arrived — a bounce
in eighteen seconds was luck, but it was real — and prints the batch id. The
look re-snapshots rather than reusing the snapshot taken to find candidates:
that one was made before the requests went out and cannot contain a reply to
them. It runs the same two phases as `--check` and expects to find nothing
most of the time.

### Checking

`unsubscribe --check` loads the newest batch, takes a fresh snapshot (the
send-path snapshot predates the bounce by definition), resolves
`from_account` to a configured `Source` by name, and runs two phases.

**Phase 1 — database only, no round trips.** From that source's inbox, keep
messages where:

- `date_received` falls between five minutes before the batch's first send
  and 24 hours after it — skew allowance below, sanity ceiling above;
- the sender's local part, case-folded, is `mailer-daemon` or `postmaster`.

Local part only, never the domain. Bounces arrive from
`MAILER-DAEMON@some-relay.example`: the domain is unpredictable, the local
part has been fixed by convention for decades.

This phase exists because an AppleScript header fetch costs the better part
of a second and an inbox holds thousands of messages. It typically leaves
between nought and three candidates.

**Phase 2 — one header fetch per survivor.** First confirm the message
really is a delivery status notification: `Content-Type` containing
`report-type=delivery-status`, or `Auto-Submitted: auto-replied`. Anything
else is a human being who happens to be called postmaster; drop it.

Then attribute, in order:

1. **`X-Failed-Recipients`** — split on commas, case-fold, require an
   **exact** match against a `to_address` in the batch.
2. **Subject token** — for requests whose subject is not the default word
   `unsubscribe`, test whether that token appears in the bounce's subject.
   Tokens like `hmv-prod/unsub/CgxnVLz…` are distinctive enough for a
   substring test to be safe.
3. **Neither** — `request=None`, reported as an unattributed bounce with its
   subject, for the user to read.

Both surviving rules are exact matches against a string we generated
ourselves. The literal word `unsubscribe` is **explicitly excluded** from
rule 2: it is the default subject when a sender's `mailto:` carries no
parameters, and it appears in half the marketing mail ever sent, so as a
substring test it would match almost anything. This is the "rule that
quietly matches the wrong message" the plan warned against, and it is
excluded by name rather than by hoping it never comes up.

Where one bounce could match two requests — two lists sharing an
unsubscribe address — it attaches to the earliest request not already
matched, so a single bounce never claims two.

## Reporting

Requests with no bounce are reported as **"no bounce seen"**, never as
"delivered" or "confirmed". A silently discarded request is indistinguishable
from an accepted one at this distance, and a tool that overclaims here is the
bug being fixed, not the fix.

```
Batch 2026-08-06T19-48-56, 2 requests sent from iCloud.

  hmv        unsubscribe-ENG@…   bounced  "Delivery Status Notification (Failure)"
  substack   unsub-9f2@…         no bounce seen

1 unattributed bounce arrived in the window:
  "Undelivered Mail Returned to Sender" — open it yourself.

A bounce names the reason in its body, which this tool does not read.
"No bounce seen" is not confirmation: a request can be accepted and ignored.
```

## Error handling

| Situation | Behaviour |
|---|---|
| `from_account` is not a configured source | Name the account and stop. Do **not** search the configured inboxes instead — that reports a clean run from the wrong mailbox. Tell the user to add it to `sources`. |
| No batches recorded | "No unsubscribe requests recorded yet." |
| `MailError` on one header fetch | Skip that candidate, count it, carry on — the same discipline `find_candidates` already uses for messages purged from the Trash mid-run. |
| Mail not running | Report and stop; phase 2 needs it. |
| Batch file with an unparseable line | Skip the line with a warning, keep the rest, as `Journal.load` does. |

## Testing

`bounces.py` being pure makes the safety-critical rules directly testable:

- an exact `X-Failed-Recipients` hit attributes to the right request;
- a token-subject hit attributes when the header is absent;
- a `postmaster` message that is not a DSN is dropped;
- a bounce for an address nobody wrote to comes back unattributed, not
  attached to the nearest request;
- two requests sharing one `to_address` do not both claim one bounce;
- the literal subject `unsubscribe` does **not** match a marketing subject
  containing the word — the regression test for rule 2's exclusion;
- a bounce outside the window is not a candidate.

`sends.py` round-trips through a temporary directory. The CLI path runs
through `FakeMail` with a scripted headers dict. No test touches a real
mailbox or shells out to `osascript`, per the existing rule.

## Note for Task 22

`AppleScriptMail._send_script`'s docstring still claims `delete newMessage`
prevents the Drafts copy. CLAUDE.md and Task 22 both record that this was
tried live and had no effect, because the draft has already reached the
server. The prose is stale; correcting it belongs with Task 22, not here.
