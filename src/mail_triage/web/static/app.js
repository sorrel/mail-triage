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
    return offered;
  }
  // Attention and invoice: filing only, once, and asked about by name.
  // Never binning — a message that may want a reply, or that looks like a
  // bill, must not be throwable away on one click.
  if (!proposal.held_folder) return [];
  return [
    {
      label: `File anyway → ${proposal.held_folder}`,
      action: "file",
      override: true,
      confirm:
        proposal.veto_kind === "invoice"
          ? "This looks like a bill. File it away anyway?"
          : "This may be waiting on a reply from you. File it away anyway?",
    },
  ];
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
  } catch (error) {
    fail(error);
  }
}

/* --- applying ------------------------------------------------------------ */

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

function pause(milliseconds) {
  if (REDUCED_MOTION.matches) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/* Applying should be something you watch happen, not a list that blinks and
 * comes back different. Each chosen row says what is being done to it, then
 * leaves — one after another, so the order is legible — and only then is the
 * list reloaded. Under prefers-reduced-motion the states still appear; the
 * waiting does not. */
async function showDeparture(article, action) {
  const state = article.querySelector(".state");
  if (state) {
    state.textContent = action === "bin" ? "binning…" : "filing…";
  }
  article.dataset.acting = "true";
  await pause(260);
  if (state) state.textContent = action === "bin" ? "binned" : "filed";
  article.dataset.done = "true";
  await pause(420);
  // Fix the height first, so the collapse animates from a real number
  // rather than from "auto", which does not transition.
  article.style.height = `${article.offsetHeight}px`;
  // Two frames: one for the browser to accept the fixed height, one for the
  // change to it to count as a transition rather than an initial value.
  await new Promise(requestAnimationFrame);
  await new Promise(requestAnimationFrame);
  article.dataset.leaving = "true";
  article.style.height = "0px";
  await pause(560);
}

document.getElementById("apply").addEventListener("click", async () => {
  clearFailure();
  const decisions = [...chosen].map(([id, choice]) => ({
    id,
    action: choice.action,
    override: choice.override,
    folder: choice.folder,
  }));
  if (decisions.length === 0) return;
  const apply = document.getElementById("apply");
  apply.disabled = true;
  document.getElementById("tally").textContent = "Moving…";
  try {
    const result = await api("/api/decisions", {
      method: "POST",
      body: JSON.stringify({ decisions }),
    });
    // Only the moves that actually happened are shown leaving. A skip stays
    // exactly where it is, and a failure must not be animated away as though
    // it had worked.
    for (const decision of decisions) {
      if (decision.action === "skip") continue;
      const article = document.querySelector(`.row[data-id="${decision.id}"]`);
      if (article && result.moved > 0) await showDeparture(article, decision.action);
    }
    await load();
    const bits = [`Moved ${result.moved}`];
    if (result.failed) bits.push(`${result.failed} failed`);
    if (result.corrections) {
      bits.push(
        `${result.corrections} correction${result.corrections === 1 ? "" : "s"} recorded`
      );
    }
    document.getElementById("tally").textContent = `${bits.join(" · ")}.`;
    document.getElementById("undo").hidden = result.moved === 0;
  } catch (error) {
    fail(error);
  } finally {
    apply.disabled = chosen.size === 0;
  }
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

  if (current && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
    event.preventDefault();
    moveAlong(current, event.key === "ArrowRight" ? 1 : -1);
    return;
  }

  // "f" opens the folder box rather than filing outright. The expected
  // folder leads the list, so f-Return is still two keys to accept — and the
  // same two keys are the start of typing somewhere else instead.
  if (current && event.key === "f") {
    const input = current.querySelector(".folder-input");
    if (input) {
      event.preventDefault();
      input.focus();
      return;
    }
  }
  // Binning and skipping stay single keystrokes, but never on held mail: its
  // buttons ask a question, and a keystroke is too cheap a way to answer one.
  if (current && current.dataset.held) return;
  if (current && !current.querySelector(".row-actions")) return;
  if (current && "bs".includes(event.key)) {
    choose(current.dataset.id, { b: "bin", s: "skip" }[event.key], current);
  }
});

load();
