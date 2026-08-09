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
from pathlib import Path

from mail_triage.config import Config
from mail_triage.execute import execute
from mail_triage.journal import Journal, new_run_id, undo_run
from mail_triage.mail_app import MailInterface
from mail_triage.review import Decision
from mail_triage.sends import new_batch_id, record_send
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
        unsubscribe_source=None,
    ) -> None:
        self.session = session
        self.config = config
        self.mail = mail
        self.accounts = accounts
        self.static_dir = Path(static_dir)
        self.gate = TokenGate(token)
        self.token = token
        self.port = port
        self.unsubscribe_source = unsubscribe_source
        self.last_request = 0.0
        self._cached_candidates = None
        self._batch_id = None

    # --- routing ----------------------------------------------------------

    def handle(self, request: Request) -> Response:
        denied = check_request(request, self.gate, self.port)
        if denied is not None:
            return denied
        if request.path == "/api/proposals" and request.method == "GET":
            return Response.json(proposals_payload(self.session, self.accounts))
        if request.path == "/api/decisions" and request.method == "POST":
            return self._decisions(request)
        if request.path == "/api/undo" and request.method == "POST":
            return self._undo()
        if request.path == "/api/unsubscribe" and request.method == "GET":
            return Response.json(candidates_payload(self._candidates()))
        if request.path == "/api/unsubscribe/send" and request.method == "POST":
            return self._unsubscribe_send(request)
        if request.method == "GET" and not request.path.startswith("/api/"):
            return self._static(request.path)
        return Response.json({"error": "no such route"}, status=404)

    # --- deciding ---------------------------------------------------------

    def _decisions(self, request: Request) -> Response:
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
            if proposal.veto is not None:
                # The guards decide this, not the page.
                return Response.json(
                    {"error": f"held back: {proposal.veto}"}, status=400
                )
            if self.session.is_applied(identifier) or item.get("action") == "skip":
                continue
            decisions.append(
                Decision(
                    proposal=proposal,
                    accepted=True,
                    override_folder=item.get("folder"),
                    action="delete" if item.get("action") == "bin" else "file",
                )
            )
            chosen.append(identifier)

        if not decisions:
            return Response.json(outcome_payload(0, 0, self.session.run_id or ""))

        journal = Journal(self.config)
        run_id = new_run_id()
        journal.begin(run_id)
        moved, failed = execute(decisions, self.mail, journal, self.config)
        self.session.mark_applied(chosen)
        self.session.run_id = run_id
        return Response.json(outcome_payload(moved, failed, run_id))

    def _undo(self) -> Response:
        if not self.session.run_id:
            return Response.json({"error": "nothing has been applied yet"}, status=400)
        reversed_count, failed = undo_run(
            self.session.run_id, self.config, self.mail, self.config.filing_account
        )
        return Response.json({"reversed": reversed_count, "failed": failed})

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
        record = send_unsubscribe(option, self.mail)
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


def _body(request: Request) -> dict | None:
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
