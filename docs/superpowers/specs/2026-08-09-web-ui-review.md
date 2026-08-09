# Review of the web interface design and plan

9 August 2026. Reviewing `2026-08-09-web-ui-design.md` and
`2026-08-09-web-ui.md` before any of it is built.

Findings are ordered by whether they change the design, and each says what
to do. Nothing here has been applied to the spec or plan yet.

---

## Security

### S1 — The unsubscribe target is never checked to be `https` (must fix)

`parse_list_unsubscribe` selects an http target with
`target.casefold().startswith("http")`. That accepts `http://`, and it also
accepts anything merely *beginning* with those four letters — `httpfoo:bar`
is a match. The value comes from a `List-Unsubscribe` header, which the
sender writes.

The plan then hands it to the browser twice: `view.src = candidate.target`
and `tab.href = candidate.target`. The CSP (`frame-src https:`) saves the
first — a plaintext or odd-scheme URL simply refuses to frame — but
**`tab.href` is not covered by any policy**, and an anchor is the one place a
scheme like `javascript:` would be dangerous. `parse_list_unsubscribe`
happens to exclude `javascript:` today because of the `http` prefix test, so
this is a latent hazard rather than a live hole: the anchor's safety rests
on a string test in a different module written for a different purpose.

**Do:** validate in `payloads.candidates_payload` that the target parses as
`https://…`, and drop or mark any candidate that does not. Then check again
in `app.js` before assigning either `src` or `href`:

```js
const url = new URL(candidate.target);
if (url.protocol !== "https:") return;   // never framed, never linked
```

Two layers because they fail differently: the server-side check is the
correctness one, the client-side check is what survives someone later
loosening the CSP.

### S2 — `allow-popups` buys nothing and costs containment (should fix)

The sandbox is `allow-scripts allow-forms allow-popups`. The first two are
needed: unsubscribe pages are usually a form and a confirmation. Popups are
not part of that flow, and allowing them lets a sender-controlled page open
windows over the interface. They inherit the sandbox (there is no
`allow-popups-to-escape-sandbox`), so this is nuisance rather than breach —
but it is nuisance bought for nothing.

**Do:** `sandbox="allow-scripts allow-forms"`, and update the assertion in
`tests/test_web_static.py` to match.

### S3 — The token survives in places the address bar does not (should fix)

`history.replaceState` clears the address bar, and `Referrer-Policy` keeps
the token out of `Referer`. Neither helps with:

- `webbrowser.open(url)`, which on macOS reaches `open(1)` — the full URL,
  token included, is visible in the process table to anything running as the
  user for as long as that call takes.
- The URL printed to the terminal under `--no-open`, which lives in
  scrollback.

The design already declares local processes out of scope, so this is not a
new boundary — but it is cheap to shrink the window.

**Do:** make the query token **single-use**. `check_request` accepts `?k=`
for `/` once; the first successful page load burns it, and thereafter only
the header token works. A leaked URL is then worthless the moment the page
has loaded, which is the common case for every leak above.

### S4 — `Sec-Fetch-Site: none` should not be accepted on `/api/` (should fix)

`check_request` allows `none`, which means a top-level user-initiated
navigation. That is right for `/` and wrong for the API: no legitimate API
call is ever a top-level navigation. The token still protects those routes,
so this is defence in depth rather than a hole.

**Do:** accept `none` only when `request.path == "/"`; require
`same-origin` for anything under `/api/`.

### S5 — The framed page still runs sender code (accepted, document it)

Without `allow-same-origin` the framed document has no cookies and no
storage, and cannot read our DOM. It *can* still run JavaScript, fingerprint
the browser, and talk to its own origin — our `connect-src 'self'` governs
our document, not theirs. That is inherent to the user's choice and not
fixable while the feature exists.

**Do:** nothing in code. Say it plainly in the modal's note, which currently
reads "runs sandboxed and cannot see anything else here" — true, and it
should not be heard as "this page can do nothing".

### S6 — Closing the modal with Escape leaves the sender's page running (must fix)

`<dialog>` closes on Escape without firing the close button's handler, so
`view.src = "about:blank"` never runs. The sender's page keeps executing,
keeps any timers, and keeps whatever it was doing, invisibly.

**Do:** bind the reset to the dialog's own `close` event, not the button:

```js
document.getElementById("frame").addEventListener("close", () => {
  document.getElementById("frame-view").src = "about:blank";
});
```

### What holds up well

- The **strict `Host` check** is the right answer to DNS rebinding, and the
  design is unusual in naming that explicitly rather than assuming a loopback
  bind is sufficient. It is not.
- **Token in a header, not a cookie** removes CSRF as a category rather than
  mitigating it, and does not depend on `SameSite` behaviour.
- **The server makes no outbound request.** Framing happens in the browser,
  so there is no SSRF surface at all — the strongest structural property of
  this design.
- **Guards are enforced server-side.** A client that asks to file a vetoed
  message is refused. The browser cannot talk past the safety rules.
- **No rowids in payloads**, so a stale tab cannot name a message.
- **Path traversal** in `_static` is handled correctly: `resolve()` followed
  by a `parents` check catches both `..` and a symlink out.
- **`innerHTML` forbidden by test.** Subjects are attacker-controlled text
  and this is the right place to be absolute.

---

## Aesthetics

### A1 — The grid will not lay out as drawn (must fix)

`.row` is `grid-template-columns: 1fr auto`, and `renderRow` appends: sender,
confidence, subject, one empty div, then destination, reason, actions. Grid
fills row by row, so destination and reason land in adjacent *columns*, not
on their own lines — the reason will sit in the right-hand column where the
confidence meter belongs, and the layout drifts from there.

**Do:** stop counting cells. Put the left-hand stack in its own element:

```html
<article class="row">
  <div class="body">     <!-- sender, subject, destination, reason -->
  <div class="metrics">  <!-- confidence, meter -->
  <div class="row-actions">
```

with `.row { display: grid; grid-template-columns: 1fr auto; }` and
`.row-actions { grid-column: 1 / -1; }`. Fewer implicit dependencies between
DOM order and appearance.

### A2 — The chosen state is carried by colour alone (must fix)

The spec says: "no meaning is carried by colour alone — every state has a
word beside it." But `.row[data-chosen]` is only a background tint, and the
tint is the same whether the row is set to file or to bin. Two different
outcomes, one appearance.

**Do:** render the choice as a word in the row — "will file", "will bin",
"skipping" — and keep the tint as reinforcement.

### A3 — Amber on paper is the weakest contrast in the palette (should fix)

`--held: #8a6d1f` on `--paper: #faf8f5` is about 4.5:1 — right at the AA
threshold, and it is used for *italic serif at 0.875rem*, which is the
hardest text in the design to read. The veto reason is also the text most
worth reading, since it explains why something is being held.

**Do:** darken to roughly `#7a5e14` (≈5.6:1). The dark-mode `#d8b45e` on
`#171512` is fine at about 9:1.

### A4 — There is no empty state and no failure state (should fix)

With nothing to triage the page renders a masthead and a void. And `api()`
never inspects the response: a 403 or a 500 returns JSON the code then reads
as data, so a failed Apply looks exactly like a successful one that moved
nothing. For a tool that moves mail, silence is the wrong failure mode — this
is the same class of defect as the unsubscribe send that reported success and
bounced 18 seconds later.

**Do:** an empty state in the editorial voice ("Nothing to triage. Your inbox
is clear."), and have `api()` throw on `!response.ok` with the message shown
in the masthead.

### A5 — Smaller notes

- `.masthead` uses `align-items: baseline`, which will hang the button group
  off the `h1`'s baseline and look misaligned. Use `center` for `.actions`.
- `.meter > i` uses `<i>` as a bar. Use a `<span>` with `aria-hidden="true"`;
  the numeral beside it already carries the value.
- `color-mix()` needs Safari 16.2+. Fine on this machine, worth a fallback
  `background` declaration before it.
- No loading state: the tally is empty until the first fetch resolves. One
  line — "Reading your inbox…" — costs nothing.

### What holds up well

The direction is coherent and the restraint is real. Serif for subjects and
for the tool's own reasoning, sans for chrome, is the correct split: it makes
the machine's voice visually distinct from the mail's. Tabular figures,
hairline rules instead of cards, and colour reserved for signal are all
consistent with the stated intent. Keeping `#frame-view` white in dark mode
is a good call — sender pages assume a light ground and would otherwise look
broken. Keyboard-first with visible focus rings, and honouring
`prefers-reduced-motion`, are not afterthoughts here.

---

## Plan accuracy

Checked against the real code. Three of the plan's tests would fail as
written — not design problems, but they would block Task 4 immediately.

1. **`FakeMail()` takes required arguments.** Its signature is
   `FakeMail(inbox: list[int], mailboxes: list[str], …)`. Every test in
   Tasks 4 and 6 calls it bare.
2. **A move needs a durable key.** `execute.py` refuses to move a message
   whose `message_key` cannot be read, and `FakeMail` returns `""` unless
   given `keys={rowid: "…"}`. `test_applying_a_decision_moves_the_mail`
   would see `moved == 0`, and the cause would not be obvious.
3. **`record_send` takes a batch id**: `record_send(config, batch_id,
   request)`, not `record_send(config, record)`. Task 6 needs
   `sends.new_batch_id()`.

`mail.moved` and `mail.sent` do exist, and `sent` is `(to_address, subject)`
as the plan assumes.

**Do:** correct the fixtures in Tasks 4 and 6 to

```python
FakeMail(inbox=[1], mailboxes=["Home/Orders", "Home/Keep"], keys={1: "<a@b>"})
```

---

## Recommendation

Six of these change code that is not yet written, which is the cheapest
moment to change it. I would apply S1, S2, S3, S4, S6, A1, A2, A3, A4 and
the three plan corrections to the spec and plan before Task 1 begins, and
treat S5 as a documentation change to the modal's wording.

None of them undermines the shape of the design. The structural decisions —
loopback with a Host check, a header token, no outbound request from the
server, guards enforced on the server, no rowids in the browser — are sound,
and they are the ones that would have been expensive to change later.
