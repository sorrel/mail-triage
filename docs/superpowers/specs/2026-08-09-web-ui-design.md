# A visual place to triage

Design for the browser interface. 9 August 2026.

## The problem

Triage is a table in a terminal. It reads well and it decides well, but it
is a poor place to *look*. A proposal table shows one line per message; the
reasoning is a summary paragraph underneath; and the one thing you most want
to do with a suspicious sender — see their unsubscribe page and leave the
list — cannot be done from there at all. `unsubscribe.py` says so plainly:
an `http` target is printed with the note "open in a browser yourself".

The user wants somewhere to see the inbox, click the mail, and unsubscribe
quickly, and wants it to look good — stated as a preference in its own
right, not a requirement borrowed from some other audience. Nothing here is
going anywhere else.

## Constraints that shape everything below

1. **Loopback only.** The server binds `127.0.0.1` and is never reachable
   from the network. The user confirmed this explicitly. It is enforced at
   the bind *and* re-checked per request (see Security).
2. **One runtime dependency.** The project depends on `click` and nothing
   else. This adds no framework, no build step and no npm: `http.server`
   from the standard library, and hand-written HTML, CSS and JavaScript
   served as static files. No web fonts, no CDN, no analytics — the page
   makes no external request of any kind except the unsubscribe iframe the
   user opens deliberately.
3. **Every existing safety rule still holds.** Mail moves only through
   `execute.py`, journalled before acting and undoable. The database is read
   from a snapshot, read-only. Guards, rules and precedence are untouched:
   the browser is a new *front end* over the existing decision pipeline, not
   a second implementation of it.
4. **Nothing personal in the repository.** Fixtures stay synthetic and
   `tests/test_no_personal_data.py` still governs.

## What this overturns, deliberately

`unsubscribe.py:5` refuses HTTP one-click unsubscribe:

> HTTP one-click unsubscribe is deliberately unsupported: it would mean
> arbitrary outbound requests to addresses supplied by the sender.

The user has chosen to open those targets in a sandboxed iframe. The
decision is knowingly taken and the reasoning has changed in one material
way: **the request is made by the browser, at the user's click, not by the
tool.** That is the difference between the tool fetching sender-supplied
URLs on its own initiative — which remains refused, and which no code path
here introduces — and the user visiting a page they chose to visit, which is
what they would otherwise do by hand.

What the user is accepting, and should know they are accepting:

- Loading the page confirms to the sender that the address is live and
  attended. For a list you intend to leave, that is the point.
- The page is sender-controlled content. It is sandboxed (below), but
  sandboxing limits damage rather than eliminating it.
- Many providers refuse to be framed (`X-Frame-Options`, CSP
  `frame-ancestors`). Those simply will not render, and the design must
  handle that as a normal outcome rather than a bug.

`send_unsubscribe` is **unchanged**: the tool still sends only `mailto`
requests, still validates the address, and still records the send for the
bounce check.

## Shape

A new command:

```
$ mail-triage web
Serving on http://127.0.0.1:8765
Opening your browser…   (ctrl-C to stop)
```

Port 8765 by default, `--port` to change it, and `--no-open` to print the
URL instead of launching a browser. The URL it opens carries the token
(`/?k=…`); the URL it *prints* carries it too, because without it the page
will not load. Tests bind port 0 and let the kernel choose.

It runs one triage pass — same snapshot, same classifier, same guards — holds
the proposals in memory, and serves them. Clicking decides; pressing Apply
executes through `execute.py` and reports what moved. The terminal stays
where it is, printing a line per significant event, and Ctrl-C stops it.

### Modules

Kept small and separable, so each can be understood and tested alone. A new
`web/` subpackage, because these five files are one concern and belong
together:

| Module | Responsibility |
|---|---|
| `web/server.py` | Socket, threading, lifecycle, and every security check |
| `web/routes.py` | Pure `Request -> Response`. No sockets, no globals |
| `web/session.py` | The run's in-memory state: proposals, decisions, run id |
| `web/payloads.py` | Dataclasses to JSON-safe dicts, and back |
| `web/static/` | `index.html`, `app.css`, `app.js` |

`routes.py` being pure is what makes the whole surface testable without a
browser or a socket: a test builds a `Request`, calls the router, and asserts
on the `Response`. `server.py` holds the parts that need a real socket, and
stays thin enough to read in one sitting.

Nothing in `web/` re-implements a decision. It calls `inputs.gather`,
`Classifier`, `review.Decision`, `execute.execute`, `journal`, and
`unsubscribe`. If the browser and the terminal ever disagree about where a
message goes, that is a bug in this layer by definition.

### Data flow

```
mail-triage web
  │
  ├─ load config, model, rules, never-personal
  ├─ inputs.gather(...)          one snapshot, as the CLI does
  ├─ Classifier.classify(...)    proposals, guards applied
  ├─ Session(proposals)          held in memory, given opaque ids
  └─ serve 127.0.0.1:<port>
                                   │
   GET  /            ─────────────▶ index.html (token embedded)
   GET  /api/proposals ───────────▶ what to show, and why
   POST /api/decisions ───────────▶ journal → execute.py → moved/failed
   POST /api/undo     ───────────▶ journal.undo_run, this run only
   GET  /api/unsubscribe ────────▶ ranked candidates (on demand: costs
   POST /api/unsubscribe/send ───▶ mailto only, existing path      AppleScript)
   GET  /api/unsubscribe/check ──▶ bounces against what we sent
```

The unsubscribe iframe is **not** in that list. The browser loads the
sender's URL directly; the server never fetches it, never proxies it and
never sees the response. That keeps server-side request forgery off the
table entirely: there is no code path in which mail-triage makes an outbound
HTTP request.

### Session state

Proposals live in memory for the life of the process, keyed by an opaque id
rather than a rowid, so a stale or guessed page cannot address a message by
its database id. Decisions accumulate against those ids. Applying them:

1. Maps ids back to proposals, refusing any id it does not know.
2. Builds `review.Decision` objects — the same type the terminal builds.
3. Calls `execute.execute`, which journals `planned` before each move and
   captures the durable `Message-ID` first, exactly as today.
4. Returns per-message outcomes and the run id.

A message can be applied once. A second Apply naming the same id is refused,
so a double-click or a re-posted form cannot move mail twice.

## Security

The threat model is small but real: a process that can move and delete mail
is listening on a port, and every page in the user's browser can send it
requests. Five defences, each independently sufficient for the case it
covers.

1. **Bind to `127.0.0.1`.** Not `0.0.0.0`, not `::`. Nothing off the machine
   can reach it.
2. **Strict `Host` check.** Every request must carry `Host: 127.0.0.1:<port>`
   or `localhost:<port>`; anything else is `403`. This is what stops DNS
   rebinding, where an attacker's hostname resolves to `127.0.0.1` and their
   page then talks to us as same-origin. Binding to loopback alone does not
   stop that; the `Host` check does.
3. **A per-run capability token.** 32 random bytes from `secrets`, generated
   at start. The browser is opened at `/?k=<token>`; the server returns the
   page with the token in a `<meta>` tag, and `app.js` immediately calls
   `history.replaceState` to drop it from the address bar. Every `/api/`
   request must carry it in an `X-Mail-Triage-Token` header. A cross-origin
   page cannot read our HTML, so it cannot learn the token — which defeats
   cross-site request forgery without cookies, and without relying on
   `SameSite` semantics.
4. **Origin and fetch-metadata checks.** `/api/` requests must have either no
   `Origin` or exactly ours, and `Sec-Fetch-Site: same-origin` where the
   header is present. Belt and braces over (3).
5. **`Referrer-Policy: no-referrer`** on every response, and
   `referrerpolicy="no-referrer"` on the iframe. Without this the token in
   the initial URL could leak to the sender in a `Referer` header the moment
   an unsubscribe page loads. This is the specific reason the token is
   stripped from the address bar rather than left there.

**Content Security Policy** on the page:

```
default-src 'self'; script-src 'self'; style-src 'self';
img-src 'self' data:; font-src 'self'; connect-src 'self';
frame-src https:; form-action 'none'; base-uri 'none';
object-src 'none'
```

`frame-src https:` is the one permissive directive and exists solely for the
unsubscribe modal — `http:` is excluded, so a plaintext unsubscribe URL is
offered as a link rather than framed. No inline script or style, so the
policy needs no nonce and an injected `<script>` cannot run.

**The iframe:**

```html
<iframe sandbox="allow-scripts allow-forms allow-popups"
        referrerpolicy="no-referrer"
        src="…the sender's https URL…"></iframe>
```

`allow-same-origin` is deliberately absent, and must never be added:
combined with `allow-scripts` it lets the framed page remove its own sandbox.
Without it the page runs in an opaque origin — no cookies, no storage — which
is also why it cannot be used to act as the user on a site they are logged
in to. Most unsubscribe pages carry their token in the URL and work fine
under that restriction. `allow-top-navigation` is absent so the page cannot
navigate the browser away from the tool.

**Rendering sender data.** Subjects, sender names and folder names are
inserted as text nodes, never as HTML, and never with `innerHTML`. Message
*bodies* are not read at all — consistent with the existing design, which has
never read them.

**Lifetime.** The server exits on Ctrl-C, and also after 30 minutes with no
request, so a forgotten tab does not leave a mail-moving endpoint listening
overnight. The token dies with the process; there is no persistence.

**What is deliberately not defended against.** Another process running as the
user on this machine can read the token from the process table or attach to
the browser. That is not a boundary this design claims to hold, and the
existing tool makes the same assumption — `local/` is readable by anything
the user runs.

## Aesthetic

Quiet editorial, as chosen. The intent is a page that reads like something
set rather than something generated, where the data is foremost and the
chrome recedes.

- **Type.** A system serif for subjects and for the tool's own explanations
  (`ui-serif, 'New York', Iowan Old Style, Georgia`), the system sans for
  labels and controls, and tabular figures for every number so columns line
  up. No web fonts: they would be an external request, and the system faces
  on this machine are good.
- **Ground.** Warm paper rather than white — `#faf8f5` — with ink at
  `#1c1a17`. A dark mode follows `prefers-color-scheme`, inverting to a warm
  near-black rather than pure black.
- **Colour is signal, not decoration.** One accent for the tool's voice.
  Filing is stated in ink, binning in a muted oxblood, a held message in a
  quiet amber. Nothing is coloured merely to be colourful, and no meaning is
  carried by colour alone — every state has a word beside it.
- **Structure.** A single column at a comfortable measure (about 78ch),
  messages grouped under their account, separated by hairline rules rather
  than boxes or cards. Generous vertical rhythm; the page should feel unhurried
  even at fifteen messages.
- **Confidence** as a numeral in tabular figures with a hairline meter beneath
  — legible at a glance, honest about precision, and never a traffic light.
- **The reasoning is the interesting part**, so it is not hidden behind a
  disclosure triangle. Each row carries its one-line reason in italic serif,
  the way the terminal explains itself: "12 filings, 0.98", "you have binned
  the last 4 from this sender", "looks personal, may need a reply".
- **Motion** is minimal and respects `prefers-reduced-motion`: 120 ms fades,
  no sliding, no bounce.
- **Keyboard first.** `j`/`k` to move, `f` file, `b` bin, `s` skip, `u`
  unsubscribe, `⌘⏎` apply. Visible focus rings; the pointer is an
  alternative, not the assumption.

## Testing

Every test runs without a browser, without a socket where possible, and
without touching mail — `FakeMail` throughout, as the suite does today.

- **Routes**, exhaustively, as pure functions: proposals shape, decisions
  applied, ids refused, double-apply refused, undo scoped to this run.
- **Security**, as first-class tests rather than afterthoughts: missing token
  → 403; wrong token → 403; foreign `Host` → 403; cross-origin `Origin` →
  403; `Sec-Fetch-Site: cross-site` → 403; the CSP and `Referrer-Policy`
  headers present on every response; `allow-same-origin` absent from the
  served HTML (a regression here silently removes the sandbox).
- **Server**, minimally, on an ephemeral port: it binds loopback, it serves
  the page, it refuses a request with a foreign `Host`.
- **No sender HTML reaches the DOM as markup** — asserted on the payload
  layer, which is where it would go wrong.

## Scope

**In:** the `web` command; the proposal view; file / bin / skip; a folder
override recorded as a correction (reusing `corrections.py`, weighted 10× as
it already is); Apply and Undo; the unsubscribe panel with mailto sending,
the bounce check, and the sandboxed iframe for https targets.

**Out, by decision:**

- *Asking about uncertain senders* (`--ask`, `rules.py`). It is a
  conversational flow with its own design; the terminal does it well. The
  folder override covers the common case of correcting one message.
- *Reading or rendering message bodies.* Never done today, and rendering
  sender HTML is the largest single risk this design could take on. A subject
  line and the sender is what a filing decision needs.
- *Multiple concurrent clients.* One browser, one run. The token makes a
  second client possible in principle; nothing is done to support it.
- *Serving to other machines.* Loopback only, by the user's instruction and
  by the `Host` check.
- *HTTP one-click unsubscribe performed by the tool.* Still refused. The
  iframe is the browser's request, made on a click.
