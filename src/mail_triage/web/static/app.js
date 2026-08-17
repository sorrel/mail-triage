"use strict";

/* The page.
 *
 * Two absolutes here, both about text written by strangers:
 *
 * - Nothing is ever assigned to innerHTML. Subjects and sender names reach
 *   the DOM through textContent, always, so markup in a subject line is a
 *   subject line and not markup.
 * - An unsubscribe URL is checked to be https before it touches an iframe
 *   src or an anchor href. The server checks too; this is the layer that
 *   survives somebody loosening the policy.
 */

const token = document.querySelector('meta[name="triage-token"]').content;

/* The token arrives in the URL because a browser opening a page cannot set a
 * header. Drop it from the address bar at once — the server has already spent
 * it, and it should not sit in history or in a screenshot. */
history.replaceState(null, "", "/");

const chosen = new Map();
let folders = [];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Mail-Triage-Token": token, "Content-Type": "application/json" },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Silence is the wrong failure mode for a tool that moves mail: a failed
    // Apply must not look like one that moved nothing.
    throw new Error(payload.error || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fail(error) {
  const alert = document.getElementById("alert");
  alert.textContent = error.message;
  alert.hidden = false;
}

function clearFailure() {
  document.getElementById("alert").hidden = true;
}

function httpsTarget(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/* --- the list ------------------------------------------------------------ */

/* Single-key equivalents for the row's buttons, filing included: the whole
 * point of a proposed folder is that agreeing with it costs one key. "f" was
 * two — open the box, press Return — for a destination already on the screen.
 * Disagreeing is what the folder box is for, and Return still opens it. */
const KEYSTROKES = { file: "f", bin: "b", skip: "s" };

function row(proposal) {
  const article = el("article", "row");
  article.tabIndex = 0;
  article.dataset.id = proposal.id;
  // Read by the keyboard handler: a held message is never actioned by a
  // keystroke, only by its button, which asks first.
  if (proposal.veto) article.dataset.held = proposal.veto_kind || "held";

  const body = el("div", "body");
  body.append(el("div", "sender", proposal.sender));
  body.append(el("h3", "subject", proposal.subject));

  if (proposal.veto) {
    if (proposal.held_folder) {
      body.append(
        el("div", "destination overridden", `would have filed → ${proposal.held_folder}`)
      );
    }
    body.append(el("div", "veto", `held — ${proposal.veto}`));
  } else {
    let where = `→ ${proposal.folder}`;
    if (proposal.action === "delete") where = "→ the bin";
    else if (!proposal.folder) where = "nowhere to file this yet";
    const destination = el("div", "destination", where);
    if (proposal.action === "delete") destination.dataset.action = "bin";
    if (!proposal.folder && proposal.action !== "delete") {
      destination.classList.add("unplaced");
    }
    body.append(destination);
    body.append(el("div", "reason", proposal.reason));
  }
  article.append(body);

  const metrics = el("div", "metrics");
  metrics.append(el("div", "confidence", proposal.confidence.toFixed(2)));
  const meter = el("span", "meter");
  meter.setAttribute("aria-hidden", "true");
  if (proposal.veto) meter.dataset.held = "true";
  const fill = el("span");
  fill.style.width = `${Math.round(proposal.confidence * 100)}%`;
  meter.append(fill);
  metrics.append(meter);
  const state = el("span", "state");
  metrics.append(state);
  article.append(metrics);

  const actions = el("div", "row-actions");
  // Every message that could be filed gets the box, not only the ones with
  // nowhere to go. With a destination already expected it leads the list, so
  // Return accepts it and typing is only needed to disagree.
  if (proposal.action !== "delete") {
    actions.append(picker(proposal, article));
  }
  for (const offer of offers(proposal)) {
    const button = el("button", null, offer.label);
    button.type = "button";
    // The keystroke is the button, so it exists exactly when the button does
    // and never when the button would stop to ask. Deciding this here rather
    // than in the keyboard handler is what stopped the two disagreeing.
    if (!offer.confirm && KEYSTROKES[offer.action]) {
      button.dataset.key = KEYSTROKES[offer.action];
    }
    button.addEventListener("click", async () => {
      if (offer.confirm && !(await confirmOverride(offer.confirm))) return;
      choose(proposal.id, offer.action, article, offer.override);
    });
    actions.append(button);
  }
  if (actions.children.length) article.append(actions);
  return article;
}

/* What may be done to a message, mirroring the server's own rules in
 * routes._permitted. The server is the authority — this only decides which
 * buttons to draw, and a client that guessed wrong is refused there. */
function offers(proposal) {
  if (!proposal.veto) {
    // No "File" button when there is no folder: it would move nothing and say
    // nothing, which is the failure mode this project least tolerates. The
    // folder box beside it is how such a message gets filed.
    const offered = proposal.folder ? [{ label: "File", action: "file" }] : [];
    offered.push({ label: "Bin", action: "bin" }, { label: "Skip", action: "skip" });
    return offered;
  }
  if (proposal.veto_kind === "deletion") {
    // Binning is the obvious answer, not an override: the veto exists
    // because this sender's recent mail is only ever binned.
    const offered = [{ label: "Bin", action: "bin" }];
    if (proposal.held_folder) {
      offered.push({ label: `File → ${proposal.held_folder}`, action: "file" });
    }
    offered.push({ label: "Skip", action: "skip" });
    return offered;
  }
  // Attention and invoice: the same three answers as any other message, and
  // binning is one of them on the same terms — one key, no question. The
  // guard's job is to stop mail being filed *away* unseen; it was never a
  // reason to make throwing it out harder than throwing anything else out,
  // and in practice held mail is binned as often as not.
  //
  // Filing still asks, because that is the outcome the guard exists for: a
  // bill or a chase filed into a folder is one you stop seeing.
  const offered = [];
  if (proposal.held_folder) {
    offered.push({
      label: `File anyway → ${proposal.held_folder}`,
      action: "file",
      override: true,
      confirm: confirmationFor(proposal),
    });
  }
  offered.push({ label: "Bin", action: "bin" }, { label: "Skip", action: "skip" });
  return offered;
}

/* Choosing where a message goes. The chosen folder is recorded as a
 * correction and weighted above plain history at the next 'learn', so
 * answering once teaches the model rather than only moving one message. */
const VISIBLE_MATCHES = 7;

function picker(proposal, article) {
  const expected = proposal.folder || proposal.held_folder || null;
  const wrapper = el("span", "picker");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "folder-input";
  input.placeholder = expected ? `File to… (${expected})` : "File to…";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-label", "Folder to file this to");

  const list = el("ul", "matches");
  list.setAttribute("role", "listbox");
  list.hidden = true;

  let matches = [];
  let active = 0;

  function draw() {
    matches = rankFolders(input.value.trim(), folders, expected).slice(0, VISIBLE_MATCHES);
    active = 0;
    list.replaceChildren();
    for (const [index, name] of matches.entries()) {
      const option = el("li", "match", name);
      option.setAttribute("role", "option");
      option.id = `${proposal.id}-match-${index}`;
      if (index === active) option.dataset.active = "true";
      // mousedown, not click: click lands after blur has already closed the
      // list, so the option would be gone before the press completed.
      option.addEventListener("mousedown", (event) => {
        event.preventDefault();
        commit(name);
      });
      list.append(option);
    }
    list.hidden = matches.length === 0;
    input.setAttribute("aria-expanded", String(!list.hidden));
    highlight();
  }

  function highlight() {
    for (const [index, option] of [...list.children].entries()) {
      if (index === active) {
        option.dataset.active = "true";
        input.setAttribute("aria-activedescendant", option.id);
      } else {
        delete option.dataset.active;
      }
    }
  }

  async function commit(name) {
    if (!folders.includes(name)) return;
    const question = confirmationFor(proposal);
    if (question && !(await confirmOverride(question))) return;
    close();
    choose(proposal.id, "file", article, Boolean(question), name);
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.value = "";
  }

  input.addEventListener("focus", draw);
  input.addEventListener("input", draw);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (event) => {
    // Apply is deliberately let through: finishing a choice and applying in
    // one breath is the whole point of a keyboard flow.
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) return;
    // Everything else is kept off the page's own j/k/f handler: inside this
    // box those are just letters somebody is typing.
    event.stopPropagation();
    if (event.key === "ArrowDown" || (event.key === "Tab" && !event.shiftKey && matches.length)) {
      event.preventDefault();
      active = (active + 1) % matches.length;
      highlight();
    } else if (event.key === "ArrowUp" || (event.key === "Tab" && event.shiftKey && matches.length)) {
      event.preventDefault();
      active = (active - 1 + matches.length) % matches.length;
      highlight();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (matches[active]) commit(matches[active]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
      article.focus();
    }
  });

  wrapper.append(input, list);
  return wrapper;
}

function confirmationFor(proposal) {
  if (!proposal.veto) return null;
  if (proposal.veto_kind === "deletion") return null;
  if (proposal.veto_kind === "security") {
    return "This looks security-relevant. File it away anyway?";
  }
  return proposal.veto_kind === "invoice"
    ? "This looks like a bill. File it away anyway?"
    : "This may be waiting on a reply from you. File it away anyway?";
}

/* Buttons rather than <form method="dialog">: the page's own CSP sets
 * form-action 'none', and a form here would be betting on how each browser
 * scopes that directive. Escape and the backdrop resolve to "no", so every
 * way out that is not the explicit button leaves the mail alone. */
function confirmOverride(question) {
  const dialog = document.getElementById("confirm");
  document.getElementById("confirm-question").textContent = question;
  dialog.returnValue = "no";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "yes"),
      { once: true }
    );
  });
}

document.getElementById("confirm-no").addEventListener("click", () => {
  document.getElementById("confirm").close("no");
});
document.getElementById("confirm-yes").addEventListener("click", () => {
  document.getElementById("confirm").close("yes");
});

/* The page's own shortcuts stop at an open dialog — otherwise j and f would
 * be doing things to the list behind it. That left this box with nothing but
 * Tab and Escape, which is not a keyboard flow, it is an interruption to one.
 * So it gets its own: arrows to choose, the initial letter of either answer,
 * Return to confirm the one in focus, Escape to leave the mail alone. */
document.getElementById("confirm").addEventListener("keydown", (event) => {
  const dialog = document.getElementById("confirm");
  const buttons = [
    document.getElementById("confirm-no"),
    document.getElementById("confirm-yes"),
  ];
  const key = event.key.toLowerCase();

  if (["arrowleft", "arrowright", "arrowup", "arrowdown"].includes(key)) {
    event.preventDefault();
    const at = buttons.indexOf(document.activeElement);
    const forward = key === "arrowright" || key === "arrowdown";
    // From nowhere in particular, land on the safe answer first.
    const next = at < 0 ? 0 : (at + (forward ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
    return;
  }
  // "l" and "f" are the initials shown on the buttons; "n" and "y" because
  // the question reads as a yes-or-no one and fingers go there.
  if (key === "l" || key === "n") {
    event.preventDefault();
    dialog.close("no");
  } else if (key === "f" || key === "y") {
    event.preventDefault();
    dialog.close("yes");
  }
});

function choose(id, action, article, override = false, folder = null) {
  chosen.set(id, { action, override, folder });
  article.dataset.chosen = action;
  const state = article.querySelector(".state");
  state.dataset.action = action;
  const words = { file: "will file", bin: "will bin", skip: "skipping" };
  state.textContent = folder ? `will file → ${folder}` : words[action];
  tally();
}

let loaded = [];

function tally() {
  const held = loaded.filter((proposal) => proposal.veto).length;
  const parts = [`${loaded.length} messages`];
  if (chosen.size) parts.push(`${chosen.size} chosen`);
  if (held) parts.push(`${held} held back`);
  document.getElementById("tally").textContent = parts.join(" · ");
  // Nothing chosen means Apply would do nothing; saying so beats a button
  // that looks live and silently does nothing when pressed.
  document.getElementById("apply").disabled = chosen.size === 0;
}

async function load() {
  try {
    if (folders.length === 0) {
      ({ folders } = await api("/api/folders"));
    }
    const { proposals } = await api("/api/proposals");
    loaded = proposals;
    chosen.clear();
    const main = document.getElementById("proposals");
    main.replaceChildren();
    if (proposals.length === 0) {
      main.append(el("p", "empty", "Nothing to triage. Your inbox is clear."));
      document.getElementById("tally").textContent = "";
      return;
    }
    // Every message held is a perfectly ordinary outcome, and the first time
    // it happened the page showed rows with no buttons and said nothing about
    // why — which reads as broken rather than as finished.
    if (proposals.every((proposal) => proposal.veto)) {
      const note = el("p", "empty");
      note.append(
        document.createTextNode(
          "Everything below was held back by a guard, and the reason is beside "
        )
      );
      note.append(el("strong", null, "each one"));
      note.append(
        document.createTextNode(
          ". Mail you keep binning can be binned from here; mail that may want a "
        )
      );
      note.append(
        document.createTextNode("reply asks first. A bill is never binned from here at all.")
      );
      main.append(note);
    }
    const groups = new Map();
    for (const proposal of proposals) {
      if (!groups.has(proposal.account)) groups.set(proposal.account, []);
      groups.get(proposal.account).push(proposal);
    }
    for (const [account, rows] of groups) {
      main.append(el("h2", "account", account || "Inbox"));
      for (const proposal of rows) main.append(row(proposal));
    }
    tally();
    focusFirstRow();
  } catch (error) {
    fail(error);
  }
}

/* Every key below the Apply shortcut acts on the message you are on — and
 * until this existed, you never were on one. A freshly drawn list left focus
 * on <body>, so the arrows and "b" did nothing at all whilst plainly looking
 * as though they should, and rebuilding the list threw focus back to <body>
 * after every apply. Only claimed when nothing else holds it, so it cannot
 * take the folder box out from under someone mid-word. */
function focusFirstRow() {
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected) return;
  document.querySelector(".row")?.focus({ preventScroll: true });
}

/* --- applying ------------------------------------------------------------ */

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

let applying = false;

function pause(milliseconds) {
  if (REDUCED_MOTION.matches) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/* Applying should be something you watch happen, not a list that blinks and
 * comes back different. Each chosen row says what is being done to it whilst
 * it is being done, then leaves — one after another, so the order is legible.
 * Under prefers-reduced-motion the states still appear; the waiting does not.
 *
 * The row's word is not an animation any more: "filing…" goes up before the
 * request and stays up for exactly as long as Mail takes, so the page is
 * saying what is true rather than what it expects. */
/* Binning runs to its own, much shorter clock — see the note beside
 * .row[data-leaving="bin"] in app.css. The numbers are paired with the CSS
 * transitions: the last pause is the length of the collapse, so the next row
 * does not start leaving until this one's gap has closed. */
const DEPARTURE = {
  file: { done: 420, leaving: 560 },
  bin: { done: 140, leaving: 200 },
};

function markActing(article, action) {
  const state = article.querySelector(".state");
  if (state) {
    state.dataset.action = action;
    state.textContent = action === "bin" ? "binning…" : "filing…";
  }
  article.dataset.acting = "true";
}

/* A message that did not move stays exactly where it is and says so. It must
 * never be animated away as though it had worked — a failed Apply that looks
 * like a successful one is the failure mode this project least tolerates. */
function markStalled(article) {
  delete article.dataset.acting;
  article.dataset.stalled = "true";
  const state = article.querySelector(".state");
  if (state) {
    state.dataset.action = "failed";
    state.textContent = "did not move";
  }
}

async function showDeparture(article, action) {
  const timing = DEPARTURE[action] ?? DEPARTURE.file;
  const state = article.querySelector(".state");
  if (state) state.textContent = action === "bin" ? "binned" : "filed";
  article.dataset.done = "true";
  await pause(timing.done);
  // Fix the height first, so the collapse animates from a real number
  // rather than from "auto", which does not transition.
  article.style.height = `${article.offsetHeight}px`;
  // Two frames: one for the browser to accept the fixed height, one for the
  // change to it to count as a transition rather than an initial value.
  await new Promise(requestAnimationFrame);
  await new Promise(requestAnimationFrame);
  // The value names the pace, not just the fact: the CSS picks the bin
  // timings off it.
  article.dataset.leaving = action === "bin" ? "bin" : "file";
  article.style.height = "0px";
  await pause(timing.leaving);
}

/* One message per request, and the page never waits on itself.
 *
 * It used to be one request for the whole press: every message moved, in
 * series, behind a single "Moving…", and only once the last one had landed
 * did the rows begin their departures — also in series, and also whilst
 * nothing else was happening. Mail takes seconds per message, so a press of
 * ten was most of a minute of a page saying nothing, followed by twelve
 * seconds of animation with nothing left to wait for.
 *
 * Now each message is asked for on its own, and the row that has just moved
 * leaves whilst the next one is already on its way. Two things run at once
 * deliberately: the moves, which are serial because Mail is, and the
 * departures, which are chained to each other so they stay in order but are
 * never waited on before the next request goes out.
 *
 * The one thing that must not be split is undo: every message of one press
 * shares a batch id, so the server writes them all to a single run. */
async function applyChosen() {
  const apply = document.getElementById("apply");
  const decisions = [...chosen]
    .map(([id, choice]) => ({
      id,
      action: choice.action,
      override: choice.override,
      folder: choice.folder,
    }))
    // A skip is a message left alone. Sending it costs a round trip to be
    // told what we already know.
    .filter((decision) => decision.action !== "skip");
  if (decisions.length === 0) return;

  clearFailure();
  apply.disabled = true;
  // The button's disabled state cannot say this: it is also how "nothing is
  // chosen" is shown. Quitting needs to know the difference.
  applying = true;
  const batch = batchId();
  const tally = document.getElementById("tally");

  let moved = 0;
  let failed = 0;
  let corrections = 0;
  let lastError = null;
  let departures = Promise.resolve();

  // Whatever happens below, the run must not end with a dead Apply button on
  // a page that still thinks it is moving mail — 'q' is refused whilst it is.
  try {
    for (const [index, decision] of decisions.entries()) {
      tally.textContent =
        decisions.length === 1
          ? "Moving…"
          : `Moving ${index + 1} of ${decisions.length}…`;
      const article = document.querySelector(`.row[data-id="${decision.id}"]`);
      if (article) markActing(article, decision.action);
      try {
        const result = await api("/api/decisions", {
          method: "POST",
          body: JSON.stringify({ batch, decisions: [decision] }),
        });
        moved += result.moved;
        failed += result.failed;
        corrections += result.corrections || 0;
        if (!article) continue;
        if (result.moved > 0) {
          // Not awaited: the next message starts moving now. Chained so the
          // departures still play one after another rather than at once.
          departures = departures.then(() => showDeparture(article, decision.action));
        } else {
          markStalled(article);
        }
      } catch (error) {
        // One message refused is that message's problem; the rest of the
        // press is still worth carrying out. The reason is reported at the
        // end, so a failure early on is not scrolled away by what follows.
        lastError = error;
        failed += 1;
        if (article) markStalled(article);
      }
    }

    await departures;
    // Reports its own failure, so it is deliberately not wrapped here.
    await load();
    const bits = [`Moved ${moved}`];
    if (failed) bits.push(`${failed} failed`);
    if (corrections) {
      bits.push(`${corrections} correction${corrections === 1 ? "" : "s"} recorded`);
    }
    tally.textContent = `${bits.join(" · ")}.`;
    document.getElementById("undo").hidden = moved === 0;
    if (lastError) fail(lastError);
  } finally {
    applying = false;
    apply.disabled = chosen.size === 0;
  }
}

/* crypto.randomUUID needs a secure context, and a loopback origin counts as
 * one by every browser's own rule — but the id only has to be unique within
 * this process, so there is no reason to fall over if that stops being true. */
function batchId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

document.getElementById("apply").addEventListener("click", () => {
  // Never twice at once: a second press mid-batch would interleave two
  // sequences of requests over the same rows.
  if (applying) return;
  applyChosen();
});

document.getElementById("undo").addEventListener("click", async () => {
  clearFailure();
  try {
    const result = await api("/api/undo", { method: "POST", body: "{}" });
    await load();
    document.getElementById("tally").textContent =
      `Put ${result.reversed} back. Restart the run to act on them again.`;
    document.getElementById("undo").hidden = true;
  } catch (error) {
    fail(error);
  }
});

/* --- stopping ------------------------------------------------------------ */

/* q ends the run: the server stops listening and the process exits, exactly
 * as ctrl-C in the terminal would. Nothing that has been applied is affected
 * — that mail has already moved — and choices not yet applied are simply not
 * applied, so the worst case is a run started again.
 *
 * The page says so itself rather than leaving a dead tab pointing at a
 * refused connection, and the request is deliberately not awaited before
 * that: the server answers and then closes, so a slow reply must not leave
 * the page looking as though nothing happened. */
let quitting = false;

async function quit() {
  if (quitting) return;
  quitting = true;
  clearFailure();
  document.getElementById("proposals").replaceChildren(
    el("p", "empty", "Stopped. You can close this tab.")
  );
  document.getElementById("tally").textContent = "Run ended.";
  for (const id of ["apply", "undo", "open-lists"]) {
    document.getElementById(id).disabled = true;
  }
  try {
    await api("/api/quit", { method: "POST", body: "{}" });
  } catch {
    // The socket closing under the reply is a successful quit, not a failure.
  }
}

/* --- mailing lists ------------------------------------------------------- */

document.getElementById("open-lists").addEventListener("click", async () => {
  clearFailure();
  const list = document.getElementById("candidates");
  list.replaceChildren(el("li", null, "Reading headers…"));
  document.getElementById("lists").showModal();
  try {
    const { candidates } = await api("/api/unsubscribe");
    list.replaceChildren();
    if (candidates.length === 0) {
      list.append(el("li", null, "Nothing worth leaving."));
      return;
    }
    for (const candidate of candidates) list.append(candidateRow(candidate));
  } catch (error) {
    list.replaceChildren(el("li", null, error.message));
  }
});

function candidateRow(candidate) {
  const item = el("li");
  item.append(el("span", "who", candidate.sender));
  item.append(
    el("span", "counts",
      `${candidate.deleted_count} binned · ${candidate.unread_count} unread`)
  );
  if (candidate.method === "blocked") {
    item.append(el("span", "counts", "no usable link"));
    return item;
  }
  const button = el("button", null,
    candidate.method === "mailto" ? "Unsubscribe" : "Open their page");
  button.type = "button";
  button.addEventListener("click", () => leave(candidate, item, button));
  item.append(button);
  return item;
}

async function leave(candidate, item, button) {
  if (candidate.method === "mailto") {
    button.disabled = true;
    button.textContent = "sending…";
    try {
      await api("/api/unsubscribe/send", {
        method: "POST",
        body: JSON.stringify({ sender: candidate.sender }),
      });
      // Not "unsubscribed": a send that reports success is not a request that
      // landed. The bounce arrives seconds later, and only --check sees it.
      button.textContent = "sent";
    } catch (error) {
      button.disabled = false;
      button.textContent = "failed";
      fail(error);
    }
    return;
  }
  const target = httpsTarget(candidate.target);
  if (!target) return;
  document.getElementById("frame-title").textContent = candidate.domain;
  document.getElementById("frame-tab").href = target;
  document.getElementById("frame-view").src = target;
  document.getElementById("frame").showModal();
}

/* --- dialogs ------------------------------------------------------------- */

for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () =>
    document.getElementById(button.dataset.close).close());
}

/* Escape closes a dialog without the button, so the reset hangs off the
 * dialog's own close event. Otherwise the sender's page keeps running,
 * invisibly, for as long as the tab is open. */
document.getElementById("frame").addEventListener("close", () => {
  document.getElementById("frame-view").src = "about:blank";
});

/* --- keyboard ------------------------------------------------------------ */

/* Two axes. Up and down move between messages; left and right move along
 * the controls of the message you are on. j/k stay as they were, so the
 * habit still works, and the arrows mean nobody has to know that.
 *
 * Inside the folder box the arrows belong to its match list and the letters
 * are just letters, which is why that box stops keystrokes at its own edge. */
function controlsOf(row) {
  return [...row.querySelectorAll(".folder-input, .row-actions button")];
}

function moveAlong(row, step) {
  const controls = controlsOf(row);
  if (controls.length === 0) return;
  const at = controls.indexOf(document.activeElement);
  if (at < 0) {
    // Coming from the row itself: rightwards picks up the first control,
    // leftwards the last, so both directions land somewhere useful.
    (step > 0 ? controls[0] : controls[controls.length - 1]).focus();
    return;
  }
  const next = at + step;
  // Off the left-hand end returns to the message, which is where the
  // up/down navigation lives. Off the right-hand end simply stops.
  if (next < 0) row.focus();
  else if (next < controls.length) controls[next].focus();
}

document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;

  // Apply, from anywhere — including mid-word in the folder box, which is
  // where you tend to be when you finish choosing. Checked before every
  // other rule for that reason.
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    document.getElementById("apply").click();
    return;
  }

  const typing = event.target.matches("input, textarea");
  const rows = [...document.querySelectorAll(".row")];
  const current = document.activeElement.closest?.(".row");
  const index = rows.indexOf(current);

  const goDown = event.key === "ArrowDown" || (!typing && event.key === "j");
  const goUp = event.key === "ArrowUp" || (!typing && event.key === "k");
  if (goDown && index < rows.length - 1) {
    event.preventDefault();
    rows[index + 1].focus();
    return;
  }
  if (goUp && index > 0) {
    event.preventDefault();
    rows[index - 1].focus();
    return;
  }

  if (typing) return;

  // Quitting. Not whilst an Apply is in flight: mail in the middle of moving
  // is the one moment the run should not be ended.
  if (event.key === "q" && !applying) {
    event.preventDefault();
    quit();
    return;
  }

  if (current && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
    event.preventDefault();
    moveAlong(current, event.key === "ArrowRight" ? 1 : -1);
    return;
  }

  // Filing, binning and skipping are each one keystroke. Which rows allow
  // which is not decided here: the keystroke presses the button, so it can
  // only ever do what a click would, and mail whose button asks first has no
  // key at all. Held mail is not blanket-refused — a message held because you
  // keep binning this sender offers Bin without asking, and refusing "b"
  // there was friction with nothing behind it.
  if (current && Object.values(KEYSTROKES).includes(event.key)) {
    const button = current.querySelector(`.row-actions button[data-key="${event.key}"]`);
    if (button) {
      event.preventDefault();
      button.click();
      return;
    }
  }

  // Disagreeing with the proposal: Return opens the folder box, arrows choose
  // in it, Escape comes back. "f" falls through to the same place when there
  // is no File button to press — a message with nowhere to file to is exactly
  // the one that needs somewhere typed.
  if (current && (event.key === "f" || (event.key === "Enter" && event.target === current))) {
    const input = current.querySelector(".folder-input");
    if (input) {
      event.preventDefault();
      input.focus();
    }
  }
});

load();
