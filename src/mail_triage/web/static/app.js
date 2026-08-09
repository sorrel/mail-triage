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
    const destination = el(
      "div",
      "destination",
      proposal.action === "delete" ? "→ the bin" : `→ ${proposal.folder}`
    );
    if (proposal.action === "delete") destination.dataset.action = "bin";
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

  if (!proposal.veto) {
    const actions = el("div", "row-actions");
    for (const [label, action] of [["File", "file"], ["Bin", "bin"], ["Skip", "skip"]]) {
      const button = el("button", null, label);
      button.type = "button";
      button.addEventListener("click", () => choose(proposal.id, action, article));
      actions.append(button);
    }
    article.append(actions);
  }
  return article;
}

function choose(id, action, article) {
  chosen.set(id, action);
  article.dataset.chosen = action;
  const state = article.querySelector(".state");
  state.dataset.action = action;
  state.textContent = { file: "will file", bin: "will bin", skip: "skipping" }[action];
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
          "Nothing to file. Every message below was held back deliberately — "
        )
      );
      note.append(el("strong", null, "the reason is beside each one"));
      note.append(
        document.createTextNode(
          ". A held message cannot be filed from here; deal with it in Mail, "
        )
      );
      note.append(
        document.createTextNode("or try Mailing lists to leave what keeps arriving.")
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

document.getElementById("apply").addEventListener("click", async () => {
  clearFailure();
  const decisions = [...chosen].map(([id, action]) => ({ id, action }));
  if (decisions.length === 0) return;
  try {
    const result = await api("/api/decisions", {
      method: "POST",
      body: JSON.stringify({ decisions }),
    });
    await load();
    document.getElementById("tally").textContent =
      `Moved ${result.moved}${result.failed ? `, ${result.failed} failed` : ""}.`;
    document.getElementById("undo").hidden = result.moved === 0;
  } catch (error) {
    fail(error);
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
  if (current && !current.querySelector(".row-actions")) return;
  if (current && "fbs".includes(event.key)) {
    choose(current.dataset.id, { f: "file", b: "bin", s: "skip" }[event.key], current);
  }
  if (event.key === "Enter" && event.metaKey) document.getElementById("apply").click();
});

load();
