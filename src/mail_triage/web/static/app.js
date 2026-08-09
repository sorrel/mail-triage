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
  // A message with nowhere to go needs to be told where. That is the only
  // way to file a bill whose predicted folder is not in the filing account,
  // and the only way to file mail with no history at all.
  if (!proposal.folder && !proposal.held_folder && proposal.action !== "delete") {
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
    // No folder means the classifier could not place it — too little history,
    // or too inconsistent. Offering "File" anyway would move nothing and say
    // nothing, which is the failure mode this project least tolerates.
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

/* Buttons rather than <form method="dialog">: the page's own CSP sets
 * form-action 'none', and a form here would be betting on how each browser
 * scopes that directive. Escape and the backdrop resolve to "no", so every
 * way out that is not the explicit button leaves the mail alone. */
/* Choosing a destination for a message that has none. The chosen folder is
 * recorded as a correction and weighted above plain history at the next
 * 'learn', so answering this once teaches the model rather than only moving
 * one message. */
function picker(proposal, article) {
  const wrapper = el("span", "picker");
  const select = document.createElement("select");
  select.className = "folders";
  select.setAttribute("aria-label", "Folder to file this to");
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "File to…";
  select.append(blank);
  for (const name of folders) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.append(option);
  }
  select.addEventListener("change", async () => {
    if (!select.value) return;
    const question = confirmationFor(proposal);
    if (question && !(await confirmOverride(question))) {
      select.value = "";
      return;
    }
    choose(proposal.id, "file", article, Boolean(question), select.value);
  });
  wrapper.append(select);
  return wrapper;
}

function confirmationFor(proposal) {
  if (!proposal.veto) return null;
  if (proposal.veto_kind === "deletion") return null;
  return proposal.veto_kind === "invoice"
    ? "This looks like a bill. File it away anyway?"
    : "This may be waiting on a reply from you. File it away anyway?";
}

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
  await pause(140);
  if (state) state.textContent = action === "bin" ? "binned" : "filed";
  article.dataset.done = "true";
  await pause(220);
  article.dataset.leaving = "true";
  await pause(REDUCED_MOTION.matches ? 0 : 260);
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
    document.getElementById("tally").textContent = `Put ${result.reversed} back.`;
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

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea")) return;
  if (document.querySelector("dialog[open]")) return;
  const rows = [...document.querySelectorAll(".row")];
  const current = document.activeElement.closest?.(".row");
  const index = rows.indexOf(current);
  if (event.key === "j" && index < rows.length - 1) rows[index + 1].focus();
  if (event.key === "k" && index > 0) rows[index - 1].focus();
  // Held mail is deliberately not keyboard-actionable. Its buttons ask a
  // question first, and a single keystroke is too cheap a way to answer one.
  if (current && current.dataset.held) return;
  if (current && !current.querySelector(".row-actions")) return;
  if (current && "fbs".includes(event.key)) {
    choose(current.dataset.id, { f: "file", b: "bin", s: "skip" }[event.key], current);
  }
  if (event.key === "Enter" && event.metaKey) document.getElementById("apply").click();
});

load();
