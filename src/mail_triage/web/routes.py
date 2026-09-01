"""Every route, as a pure function of a request.

No sockets and no globals live here, which is what lets the whole HTTP
surface be tested by building a ``Request`` and asserting on a ``Response``.
The socket is ``server``'s business.

Three rules this module exists to hold:

- **A client cannot talk past a guard.** A vetoed proposal is refused
  outright, whatever the page asks for. The browser is a front end over the
  existing decision pipeline; it is not permitted to overrule it.
- **A message moves once.** Ids already applied are dropped before anything
  is executed, so a double-click, a retry or a re-posted request cannot file
  the same mail twice.
- **The tool makes no outbound HTTP request.** An http unsubscribe target is
  handed to the browser to frame, never fetched here. There is therefore no
  server-side request forgery surface at all.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from pathlib import Path

from mail_triage.config import Config
from mail_triage.corrections import record_overrides
from mail_triage.execute import execute
from mail_triage.journal import Journal, new_run_id, undo_run
from mail_triage.mail_app import MailError, MailInterface
from mail_triage.review import Decision
from mail_triage.sends import FailedRequest, new_batch_id, record_failure, record_send
from mail_triage.unsubscribe import send_unsubscribe
from mail_triage.web.payloads import candidates_payload, outcome_payload, proposals_payload
from mail_triage.web.security import (
    SECURITY_HEADERS,
    Request,
    Response,
    TokenGate,
    check_request,
    session_cookie,
)
from mail_triage.web.session import Session

TOKEN_PLACEHOLDER = "__TOKEN__"


class Router:
    def __init__(
        self,
        session: Session,
        config: Config,
        mail: MailInterface,
        accounts: dict[str, str],
        static_dir: Path,
        token: str,
        port: int,
        folders: list[str] | None = None,
        unsubscribe_source=None,
        refresh=None,
        on_quit=None,
    ) -> None:
        self.session = session
        self.config = config
        self.mail = mail
        self.accounts = accounts
        # The filing account's real folders, in their real capitalisation.
        # Also the allowlist: a client may only name a folder from this list,
        # so no page can send mail to a mailbox the run never offered.
        self.folders = sorted(folders or [])
        self.static_dir = Path(static_dir)
        self.gate = TokenGate(token)
        self.token = token
        self.port = port
        self.unsubscribe_source = unsubscribe_source
        # Re-runs classify_run against a fresh snapshot. None in tests that
        # never call /api/refresh.
        self.refresh_run = refresh
        # Set by ``serve``; stopping the socket is the socket's business.
        self.on_quit = on_quit
        self.last_request = 0.0
        self._cached_candidates = None
        self._batch_id = None
        # One Apply at a time. See _decisions.
        self._applying = threading.Lock()
        # The press currently being applied, and the run it writes to. See
        # _run_for: the page sends one message per request now, and undo is
        # about the press rather than the message.
        self._apply_batch: str | None = None
        self._apply_run: tuple[Journal, str] | None = None

    # --- routing ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        denied = check_request(request, self.gate, self.port)
        if denied is not None:
            return denied
        if request.path == "/api/proposals" and request.method == "GET":
            return Response.json(proposals_payload(self.session, self.accounts))
        if request.path == "/api/folders" and request.method == "GET":
            return Response.json({"folders": self.folders})
        if request.path == "/api/decisions" and request.method == "POST":
            return self._decisions(request)
        if request.path == "/api/undo" and request.method == "POST":
            return self._undo()
        if request.path == "/api/refresh" and request.method == "POST":
            return self._refresh()
        if request.path == "/api/unsubscribe" and request.method == "GET":
            return Response.json(candidates_payload(self._candidates()))
        if request.path == "/api/unsubscribe/send" and request.method == "POST":
            return self._unsubscribe_send(request)
        if request.path == "/api/quit" and request.method == "POST":
            return self._quit()
        if request.method == "GET" and not request.path.startswith("/api/"):
            return self._static(request.path)
        return Response.json({"error": "no such route"}, status=404)

    # --- deciding ---------------------------------------------------------

    def _decisions(self, request: Request) -> Response:
        # Serialised, because the server is threaded and two Apply requests
        # genuinely overlapped in use: the "already applied?" check and the
        # marking that follows it are not one operation, so both requests
        # passed the check and both moved the same mail. The second move
        # failed harmlessly, but it wrote 'failed' over the first's 'moved'
        # in the journal, and undo skips a failed entry — so messages that
        # had really moved could not be put back.
        with self._applying:
            return self._apply(request)

    def _apply(self, request: Request) -> Response:
        payload = _body(request)
        if payload is None:
            return Response.json({"error": "body is not JSON"}, status=400)

        decisions: list[Decision] = []
        chosen: list[str] = []
        for item in payload.get("decisions") or []:
            identifier = item.get("id")
            proposal = self.session.get(identifier)
            if proposal is None:
                return Response.json({"error": f"unknown message {identifier!r}"}, status=400)
            action = item.get("action", "file")
            folder = item.get("folder") or None
            if folder is not None and folder not in self.folders:
                # Never trust a client with a destination. The list it was
                # given is the only list it may name.
                return Response.json(
                    {"error": f"{folder!r} is not a folder in the filing account"},
                    status=400,
                )
            refusal = _permitted(proposal, action, bool(item.get("override")), folder)
            if refusal is not None:
                return Response.json({"error": refusal}, status=400)
            if self.session.is_applied(identifier) or action == "skip":
                continue
            decisions.append(
                Decision(
                    proposal=proposal,
                    accepted=True,
                    # A held message has no folder of its own — the veto reset
                    # it — so the destination it *would* have used has to be
                    # supplied explicitly here.
                    override_folder=folder or proposal.held_folder,
                    action="delete" if action == "bin" else "file",
                )
            )
            chosen.append(identifier)

        if not decisions:
            return Response.json(outcome_payload(0, 0, self.session.run_id or ""))

        # Before the moves, not after: a typed folder is an instruction about
        # where mail belongs, and it holds whether or not the move that
        # follows happens to succeed. 'learn' weights these above plain
        # history. Same call the terminal makes, for the same reason.
        corrected = record_overrides(decisions, self.config)

        journal, run_id = self._run_for(payload.get("batch"))
        # Claimed before the moves, not after. If this process dies mid-batch
        # the messages are marked as dealt with and the journal says exactly
        # what was attempted; marking afterwards leaves a window in which the
        # same mail can be offered — and moved — twice.
        self.session.mark_applied(chosen)
        moved, failed = execute(decisions, self.mail, journal, self.config)
        self.session.run_id = run_id
        payload = outcome_payload(moved, failed, run_id)
        payload["corrections"] = corrected
        return Response.json(payload)

    def _run_for(self, batch) -> tuple[Journal, str]:
        """The journal and run id this request's moves belong to.

        The page applies one message per request, so that a row can finish
        leaving the screen whilst the next message is already moving —
        Mail takes seconds per message, and a batch that reported nothing
        until the last one had gone was the whole of the wait.

        Undo, though, is about the press and not the message. Every message
        of one press has to land in one run, or "Undo" would put back only
        whichever went last. So the page names its press with an opaque
        batch id: the same id continues the run it started, and anything
        else starts a new one.

        A request with no batch id gets a run to itself, which is the
        behaviour every other client has always had.
        """
        if isinstance(batch, str) and batch and batch == self._apply_batch:
            if self._apply_run is not None:
                return self._apply_run
        journal = Journal(self.config)
        run_id = new_run_id()
        journal.begin(run_id)
        self._apply_batch = batch if isinstance(batch, str) and batch else None
        self._apply_run = (journal, run_id)
        return journal, run_id

    def _undo(self) -> Response:
        if not self.session.run_id:
            return Response.json({"error": "nothing has been applied yet"}, status=400)
        reversed_count, failed = undo_run(
            self.session.run_id, self.config, self.mail, self.config.filing_account
        )
        return Response.json({"reversed": reversed_count, "failed": failed})

    # --- refreshing ---------------------------------------------------------

    def _refresh(self) -> Response:
        """Re-snapshot the mailbox and redraw proposals, in place.

        Held under the apply lock, so a refresh cannot land mid-move. The
        old ``run_id`` carries forward: it names an Undo the page may still
        need to make, and a fresh run cannot re-offer whatever it already
        moved, so nothing is lost by dropping the old proposals.
        """
        if self.refresh_run is None:
            return Response.json({"error": "refresh is not available"}, status=400)
        with self._applying:
            run = self.refresh_run()
            self.session = Session(run.proposals, run_id=self.session.run_id)
            self.folders = sorted(run.inputs.folders)
        return Response.json(proposals_payload(self.session, self.accounts))

    # --- stopping ---------------------------------------------------------

    def _quit(self) -> Response:
        """End the run from the browser — the equivalent of ctrl-C.

        The apply lock is held whilst answering, so a quit that arrives whilst
        mail is being moved waits for the batch rather than pulling the server
        out from under it. The reply is written before anything is stopped;
        ``on_quit`` only asks for the socket to close, it does not close it
        here.
        """
        with self._applying:
            if self.on_quit is not None:
                self.on_quit()
        return Response.json({"stopping": True})

    # --- unsubscribing ----------------------------------------------------

    def _candidates(self):
        """Cached: each call costs an AppleScript round trip per sender."""
        if self._cached_candidates is None:
            self._cached_candidates = (
                list(self.unsubscribe_source()) if self.unsubscribe_source else []
            )
        return self._cached_candidates

    def _unsubscribe_send(self, request: Request) -> Response:
        payload = _body(request)
        if payload is None:
            return Response.json({"error": "body is not JSON"}, status=400)
        wanted = payload.get("sender")
        for option in self._candidates():
            if option.sender == wanted:
                break
        else:
            return Response.json({"error": f"{wanted!r} is not a candidate"}, status=400)
        if option.method != "mailto":
            # Unchanged from the CLI: the tool sends mailto requests and makes
            # no outbound HTTP request of any kind. An http target is framed
            # by the browser, on the user's click, and never fetched by us.
            return Response.json(
                {"error": "an http unsubscribe is opened in the browser, not sent"},
                status=400,
            )
        if self._batch_id is None:
            self._batch_id = new_batch_id()
        try:
            record = send_unsubscribe(option, self.mail)
        except (ValueError, MailError) as error:
            # Uncaught, this killed the connection: the browser's fetch
            # rejected, the page could only say "failed", and the reason
            # existed nowhere but a traceback in the terminal. Written down
            # and handed back instead — as a failure record, never as a send,
            # so the bounce check cannot mistake it for a request that left.
            record_failure(
                self.config,
                self._batch_id,
                FailedRequest(
                    sender=option.sender,
                    to_address=option.target,
                    subject=option.subject,
                    attempted_at=int(time.time()),
                    from_account=option.account,
                    reason=str(error),
                ),
            )
            return Response.json({"error": str(error)}, status=502)
        record_send(self.config, self._batch_id, record)
        # Deliberately not "unsubscribed": a send that reports success is not
        # a request that landed. 'mail-triage unsubscribe --check' reads this
        # same log for the bounce.
        return Response.json({"sent": True, "to": record.to_address})

    # --- static files -----------------------------------------------------

    def _static(self, path: str) -> Response:
        name = "index.html" if path == "/" else path.lstrip("/")
        root = self.static_dir.resolve()
        target = (root / name).resolve()
        # The one thing a static handler must never do is serve a path that
        # climbs out of its directory. resolve() first, so "..", a symlink and
        # an absolute path are all caught by the same check.
        if root not in target.parents or not target.is_file():
            return Response.json({"error": "no such file"}, status=404)
        body = target.read_bytes()
        headers = dict(SECURITY_HEADERS)
        if target.name == "index.html":
            # The page needs the token to call the API, and it cannot be given
            # one in a header. Substituted on the way out so the file on disk
            # never holds a live secret.
            body = body.replace(TOKEN_PLACEHOLDER.encode(), self.token.encode())
            # And a session cookie, so a refresh still works once the one-time
            # URL token has been spent.
            headers["Set-Cookie"] = session_cookie(self.token)
        content_type, _ = mimetypes.guess_type(target.name)
        return Response(200, body, content_type or "application/octet-stream", headers)


def _permitted(proposal, action: str, override: bool, folder: str | None = None) -> str | None:
    """Why this action on this message is refused, or ``None`` if it is allowed.

    The precedence rules in one place, enforced on the server so no client
    can talk past them. What each veto permits follows ``guards``:

    - **No veto** — anything. This is ordinary mail.
    - **A deletion veto** — file or bin freely. It means "the recent mail
      from this sender is only ever binned", so binning is not an override
      at all; it is the obvious answer, and ``guards`` calls such mail "the
      prime candidate" for it.
    - **An attention, invoice or security veto** — binning freely, filing only
      with an explicit per-message ``override``. The guard exists to stop mail
      being
      filed *away* unseen, which is the outcome you cannot notice; binning is
      a decision taken in front of you, undoable from the journal, and is not
      made harder here than it is for any other message. It was once refused
      outright, which left a held row with no folder of its own a dead end.
    """
    if action not in ("file", "bin", "skip"):
        return f"{action!r} is not a thing that can be done to a message"
    if proposal.veto is None or action in ("skip", "bin"):
        return None
    if proposal.veto_kind == "deletion":
        return None
    if not override:
        return f"held back: {proposal.veto}"
    if not (folder or proposal.held_folder):
        # A held message often has no destination of its own — the veto reset
        # it, or the classifier's choice was not a folder in the filing
        # account. Then the only way to file it is to be told where, which is
        # what the folder picker is for.
        return "no destination — choose a folder to file this to"
    return None


def _body(request: Request) -> dict | None:
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
