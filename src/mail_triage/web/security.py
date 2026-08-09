"""What makes a mail-moving port safe to leave listening.

The threat model is small and real. This process can move and delete mail,
it is listening on a TCP port, and every page in the user's browser can send
it requests. Five defences, each independently sufficient for its own case,
are applied to every request before any route sees it.

1. Loopback bind (in ``server``) — nothing off the machine can connect.
2. A strict ``Host`` check — the answer to DNS rebinding, where an
   attacker's hostname resolves to 127.0.0.1 and their page then addresses
   us as same-origin. Binding to loopback does not prevent that. This does.
3. A per-run capability token, required in a header on every ``/api/``
   request. A cross-origin page cannot read our HTML, so it cannot learn the
   token: that defeats cross-site request forgery without cookies and
   without relying on ``SameSite`` semantics. The copy in the opening URL is
   accepted **once** — see ``TokenGate``.
4. ``Origin`` and ``Sec-Fetch-Site`` checks, as belt and braces over (3).
5. ``Referrer-Policy: no-referrer`` on every response, so the token in the
   opening URL cannot leak to a sender in a ``Referer`` header when their
   unsubscribe page loads.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-src https:; form-action 'none'; base-uri 'none'; object-src 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    # Without this the token in the opening URL travels to the sender in a
    # Referer header the moment an unsubscribe page loads in the iframe.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # We frame others; nobody frames us.
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str | None]
    body: bytes

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup.

        HTTP header names are case-insensitive by RFC 9110 and clients
        genuinely vary, so an exact-match lookup would miss a header that is
        plainly present — and here that would mean failing open.
        """
        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return None


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: dict, status: int = 200) -> "Response":
        return cls(status, json.dumps(payload).encode(), "application/json")


class TokenGate:
    """The run's token, with the URL copy good for one page load only.

    A browser opening a page cannot set a header, so the token has to travel
    in the URL once. That copy then exists in places the address bar does
    not: ``webbrowser.open`` reaches ``open(1)`` on macOS, where the whole
    URL is visible in the process table, and under ``--no-open`` it is
    printed into terminal scrollback.

    Burning it on first use shrinks that window to the moment before the
    page loads. Afterwards only the header copy works, which no other origin
    can read, and a leaked URL is worth nothing.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self._url_token_spent = False

    def matches(self, supplied: str | None) -> bool:
        # compare_digest, not ==: a timing oracle on a local token is a thin
        # attack, but constant-time comparison is free.
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def spend_url_token(self, supplied: str | None) -> bool:
        """Accept the URL copy of the token, once."""
        if self._url_token_spent or not self.matches(supplied):
            return False
        self._url_token_spent = True
        return True


SESSION_COOKIE = "mail_triage_session"


def session_cookie(token: str) -> str:
    """The page's session cookie.

    ``HttpOnly`` so no script can read it back out — the page gets its token
    from the meta tag instead. ``SameSite=Strict`` so it is never sent on a
    navigation that began anywhere else. No ``Secure``: this is plain http on
    loopback by design, and marking it Secure would stop it being set at all.
    """
    return (
        f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
    )


def _cookie(request: Request, name: str) -> str | None:
    """One cookie's value from the request's ``Cookie`` header."""
    raw = request.header("Cookie") or ""
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _forbidden(why: str) -> Response:
    return Response.json({"error": why}, status=403)


def check_request(request: Request, gate: TokenGate, port: int) -> Response | None:
    """Return a refusal, or ``None`` if the request may proceed."""
    host = (request.header("Host") or "").casefold()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return _forbidden("unrecognised Host")

    origin = request.header("Origin")
    if origin is not None and origin.casefold() not in {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }:
        return _forbidden("cross-origin request")

    # The split that matters: an /api/ call *acts*, and is made by our own
    # script, which can set a header. Everything else is a document or an
    # asset, fetched by the browser itself — and a browser cannot put a custom
    # header on a <link>, a <script> or an address-bar navigation. Requiring
    # one there refused the page its own stylesheet and script, which is
    # precisely what happened: the interface rendered unstyled and stuck on
    # "Reading your inbox…".
    is_api = request.path.startswith("/api/")

    fetch_site = request.header("Sec-Fetch-Site")
    if fetch_site is not None:
        # "none" means a top-level, user-initiated navigation: right for a
        # document, and never right for an API call.
        allowed = {"same-origin"} if is_api else {"same-origin", "none"}
        if fetch_site.casefold() not in allowed:
            return _forbidden("cross-site request")

    if is_api:
        # The header token, and only the header token. A cookie rides along
        # automatically on any request a browser can be tricked into making,
        # so it must never be enough to move mail. This is what keeps CSRF
        # closed now that a cookie exists at all.
        if not gate.matches(request.header("X-Mail-Triage-Token")):
            return _forbidden("bad or missing token")
        return None

    # The cookie is what makes a refresh — and every asset — work. Once the
    # URL token has been spent the address bar holds a bare "/", so without
    # this a ⌘R answers a JSON error instead of the page.
    if gate.matches(_cookie(request, SESSION_COOKIE)):
        return None
    if request.path == "/" and gate.spend_url_token(request.query.get("k")):
        return None
    return _forbidden("bad, missing or already-used token")
