# Auto mode, with a focus on security

*Spec — 17 August 2026. Implemented the same day; see the measurement at the
foot, which is what the plan made conditional.*

Auto mode exists (`triage --auto`, since 2 August 2026) but has never been
scheduled: the design note at `2026-07-26-mail-triage-design.md:443` left
that as "`--auto` under cron, if wanted later". This is later. The request
is for the unattended runs to carry a standing focus on security.

## What auto mode is safe against today

`review.auto_decisions` accepts a proposal on two conditions, and the first
does most of the work:

- `folder is not None` — which excludes everything a veto held back (a
  vetoed proposal keeps its destination in `held_folder` and empties
  `folder` for exactly this purpose) and excludes bin proposals too, whose
  destination is the Trash. So an unattended run **files and does nothing
  else**: it never bins, and never touches mail that is flagged, awaiting a
  reply, or carrying a bill.
- `confidence >= auto_threshold` (0.9).

Every move is journalled before it happens and `mail-triage undo` reverses
the run, exactly as for an interactive one. That is a good safety envelope
and none of it needs changing.

## The gap, measured

The envelope protects against the wrong *action*. It does not protect
against the right action taken on mail that should have been seen. Auto
mode's characteristic failure is not moving mail somewhere silly — it is
filing something correctly, quietly, that wanted eyes on it.

For this user that is security mail. And the existing guards do not hold it
back. Run against `guards.needs_attention` on 17 August 2026, with a
`List-Unsubscribe` header supplied so the message had every chance of being
held:

| Sender | Subject | Guard |
|---|---|---|
| `notifications@code-host.example` | Dependabot alert: critical vulnerability | **files** |
| `no-reply@accounts.search-co.example` | Security alert: new sign-in to your account | **files** |
| `no-reply@signin.cloud-co.example` | Your root account was accessed | **files** |
| `noreply@breach-check.example` | You have been pwned in the X breach | **files** |
| `noreply@notify.cdn-co.example` | API token created | **files** |

Not one is held. The reason is structural rather than accidental:
`_NO_REPLY_WORDS` contains `"notifications"` and `"noreply"`, so every one of
these is decided *bulk* by the address alone, and `needs_attention` returns
`None` before any header is consulted. That is correct for the guard's own
question — no human is waiting for a reply to a Dependabot alert — but the
guard's question is not the only one worth asking.

Worse, these are the senders auto mode is *most* confident about. They are
high-volume, consistent, and stage A will have learnt a folder for them from
a long filing history. A breach notification arrives, reads 0.97, and is
filed into `Filed/Alerts` at 07:00 by a process nobody watched.

## The security guard

A new per-message guard, `security.py`, returning `Veto(reason)` with
`veto_kind="security"`. It sits with the other per-message guards at the top
of the precedence order — above rules, above the deletion veto, above both
classifier stages — so no sender rule and no amount of confidence can talk
past it.

Its effect is narrow and specific: **security-relevant mail is never filed
unattended.** It stays in the inbox with its reason shown, and is offered
normally in an interactive run, where there is somebody there to read it.

### The asymmetry runs the other way

This matters, because it licenses a broader guard than the repository would
normally accept. For the *reply* guard a false positive is friction and a
false negative loses a message that wanted an answer — so it is written
narrowly and fails safe by holding back.

Here both directions fail into the inbox. A false positive means one extra
message stays put and is filed by hand next run. A false negative means a
breach notice is filed away silently. The costs are not close, so this guard
is deliberately generous where the others are careful.

The limit on that generosity is usefulness, not safety: a guard that holds
back half a security engineer's inbox makes auto mode pointless. Hence the
measurement step below.

### What counts as security-relevant

Three layers, in order of authority.

1. **Declared senders** — `local/security-senders.json`, the same shape and
   loader as `never_personal.json`, managed by `mail-triage security
   --add / --forget`. A declaration is ground truth: this sender's mail is
   always held. Domains as well as addresses, since the interesting senders
   mint per-message addresses.
2. **Subject vocabulary** — a conservative list held in code, matched on
   whole words: `security alert`, `suspicious`, `sign-in`, `signed in`,
   `password (was )?(reset|changed)`, `verification code`, `two-factor`,
   `2fa`, `mfa`, `breach`, `CVE-\d{4}-\d+`, `vulnerability`, `advisory`,
   `unauthorised access`, `api (key|token) created`, `new device`,
   `recovery (code|email)`.
3. **Nothing else.** No header signal is used, because the survey above
   shows the headers of a breach notice and a marketing mail are
   indistinguishable — the difference is in the meaning, the same finding
   that produced `never_personal.py`.

### What it does not do

- It does not flag, move, bin, or reply. It withholds mail from *unattended*
  filing and nothing more.
- It does not override flagging, which stays absolute.
- It does not change interactive `triage` at all beyond showing the reason.

## Scheduling — built, then removed the same day

A launchd agent was specified here, built, installed and tested. It is gone,
and the reversal is the more useful record.

It worked as designed and would still have failed every morning: macOS grants
Full Disk Access per responsible binary, so the agent — not being the terminal
that had held that grant for months — hit `PermissionError` on
`Envelope Index-wal`. Found by kickstarting a `--dry-run` copy under a separate
label rather than waiting for 08:30.

That bug was fixable. The permission was not. There is no TCC grant narrower
than Full Disk Access covering `~/Library/Mail`, so any unattended runner holds
blanket disk read permanently. Pointing it at a project-specific Python
interpreter narrowed blast radius across *projects* whilst doing nothing about
blast radius across *dependencies* — every package in the venv would execute
unattended with full disk read, which is the worse exposure and the one that
was initially presented as an improvement.

The user's judgement, and the right one: not a secure way of doing things for
the sake of saving a command. `--auto` remains, invoked by hand from a terminal
that already has the grant.

If this is revisited, the prerequisite is reading Mail without the database —
Apple Events automation is a far narrower grant. The blocker is the folder
list: AppleScript returns flat leaf names and loses the nested paths filing
depends on. The inbox read would be trivial; the 75-day deletion index would
not.

## The regular part

Unattended must not mean invisible. `mail-triage report [--since]` reads the
run journals and prints:

- what was filed, by folder and by source;
- **what the security guard held, in full, every message** — this is the
  section the report exists for, and it is printed first;
- what the other guards held, counted;
- anything that failed to move.

Run it on demand. It was to have run on a weekly cadence from the same
scheduled job; with that gone, it is a command like any other.

## Bounding an unattended run

Two additions, both cheap:

- `auto_limit` in config (default 50): the most messages one unattended run
  will file. A misclassification storm is then bounded and undoable in one
  `undo` rather than discovered a week later.
- Journal retention long enough that `undo` reaches back across unattended
  runs — currently nothing prunes `local/journal/`, so this is a documented
  guarantee rather than new code.

## Verification

Per the repository's rules: no live run, `FakeMail` and the suite only.

1. Unit tests for the guard: each vocabulary term, the declared-sender path,
   and the precedence assertion that a `file` rule cannot override it.
2. A test that `auto_decisions` returns nothing for a security-vetoed
   proposal — the property the whole spec exists to establish.
3. **The measurement, before the guard is trusted.** Run the guard over the
   existing training corpus and report what share of filed mail it would
   have held. If that is a large fraction, the vocabulary is too broad and
   gets cut before anything is scheduled. The number goes in the commit
   message, as the conventions require. **Done — see below.**
4. `triage --auto --dry-run` is refused by design, so the preview is
   `triage --dry-run` with the auto threshold shown in the summary — already
   the case.

## Open questions

- **Cadence.** Twice daily is a guess. It only matters for how stale the
  inbox gets between runs.
- **Whether the weekly report should mail itself.** The tool can send (it
  sends unsubscribe requests), but a tool that sends mail to its own user on
  a schedule is a new category of thing. Terminal output only, unless asked.

## The measurement

`mail-triage security --measure`, run against the real corpus on 17 August
2026:

    178 of 13,993 filed messages (1.3%) would be held back as
    security-relevant.

    What fired, commonest first:
         40  security alert
         22  new sign-in
         12  verification code
         11  password reset
         10  new login
          9  new sign in
          9  new device
          8  two-factor
          6  personal access token
          5  password has been changed
        ... 30 distinct terms in all, tailing off into individual CVEs

1.3% is comfortably below the 10% at which the vocabulary would have been cut,
and the distribution is the shape you want: no single term running away with
it, and a long tail of specific one-off matches rather than one blunt word
doing all the work. The vocabulary stands as written.

## What was built

- `security.py` — the guard, the vocabulary, the declared-sender list.
- `model/classify.py` — `_security_guard`, applied outermost.
- `review.py` — security mail never binnable, always offered in `review_held`,
  and leading `summarise`.
- `report.py` and `mail-triage report` — the regular part.
- `mail-triage security --add / --forget / --measure`.
- `auto_limit` (default 50), enforced in `auto_decisions`, reported by `--auto`.
- 57 tests across `test_security.py`, `test_report.py`, `test_cli_security.py`.

Both open questions were left at their defaults: twice daily, and the report
prints to the terminal rather than mailing itself.
