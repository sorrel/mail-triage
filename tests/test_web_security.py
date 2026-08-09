"""The five defences, each tested against the attack it answers.

These are not incidental tests. A process that can move and delete mail is
listening on a port, and every page in the user's browser can send it
requests.
"""

from __future__ import annotations

from mail_triage.web.security import CSP, SECURITY_HEADERS, Request, TokenGate, check_request

TOKEN = "a-very-secret-token"
PORT = 8765


def gate():
    return TokenGate(TOKEN)


def request(path="/api/proposals", method="GET", headers=None, query=None):
    base = {"Host": f"127.0.0.1:{PORT}", "X-Mail-Triage-Token": TOKEN}
    base.update(headers or {})
    return Request(method=method, path=path, query=query or {}, headers=base, body=b"")


def page(query=None, headers=None):
    base = {"Host": f"127.0.0.1:{PORT}"}
    base.update(headers or {})
    return Request(method="GET", path="/", query=query or {}, headers=base, body=b"")


def test_a_well_formed_request_is_allowed():
    assert check_request(request(), gate(), PORT) is None


def test_a_missing_token_is_refused():
    denied = check_request(request(headers={"X-Mail-Triage-Token": None}), gate(), PORT)
    assert denied.status == 403


def test_a_wrong_token_is_refused():
    assert check_request(request(headers={"X-Mail-Triage-Token": "guess"}), gate(), PORT).status == 403


def test_a_foreign_host_header_is_refused():
    """DNS rebinding: an attacker's name resolving to 127.0.0.1 makes their
    page same-origin with us. Binding to loopback does not stop that."""
    assert check_request(request(headers={"Host": "evil.example:8765"}), gate(), PORT).status == 403


def test_localhost_is_accepted_as_well_as_the_numeric_address():
    assert check_request(request(headers={"Host": f"localhost:{PORT}"}), gate(), PORT) is None


def test_the_right_host_on_the_wrong_port_is_refused():
    assert check_request(request(headers={"Host": "127.0.0.1:9999"}), gate(), PORT).status == 403


def test_a_cross_origin_request_is_refused():
    denied = check_request(request(headers={"Origin": "https://evil.example"}), gate(), PORT)
    assert denied.status == 403


def test_our_own_origin_is_allowed():
    allowed = request(headers={"Origin": f"http://127.0.0.1:{PORT}"})
    assert check_request(allowed, gate(), PORT) is None


def test_a_cross_site_fetch_metadata_header_is_refused():
    denied = check_request(request(headers={"Sec-Fetch-Site": "cross-site"}), gate(), PORT)
    assert denied.status == 403


def test_same_origin_fetch_metadata_is_allowed():
    assert check_request(request(headers={"Sec-Fetch-Site": "same-origin"}), gate(), PORT) is None


def test_a_top_level_navigation_to_the_api_is_refused():
    """"none" means a user-initiated navigation. Right for the page, and
    never right for an API call."""
    denied = check_request(request(headers={"Sec-Fetch-Site": "none"}), gate(), PORT)
    assert denied.status == 403


def test_a_top_level_navigation_to_the_page_is_allowed():
    allowed = page(query={"k": TOKEN}, headers={"Sec-Fetch-Site": "none"})
    assert check_request(allowed, gate(), PORT) is None


def test_the_page_takes_its_token_from_the_query():
    """A browser opening a URL cannot set a header."""
    assert check_request(page(query={"k": TOKEN}), gate(), PORT) is None


def test_the_page_without_the_token_is_refused():
    assert check_request(page(), gate(), PORT).status == 403


def test_the_url_token_works_once_and_then_never_again():
    """It survives in the process table and in terminal scrollback. Burning
    it on first use means a leaked URL is worth nothing."""
    one_gate = gate()
    assert check_request(page(query={"k": TOKEN}), one_gate, PORT) is None
    assert check_request(page(query={"k": TOKEN}), one_gate, PORT).status == 403


def test_spending_the_url_token_does_not_disturb_the_header_token():
    """The page loads once; its script then calls the API many times."""
    one_gate = gate()
    check_request(page(query={"k": TOKEN}), one_gate, PORT)
    assert check_request(request(), one_gate, PORT) is None
    assert check_request(request(), one_gate, PORT) is None


def test_a_wrong_url_token_does_not_burn_the_real_one():
    """Otherwise anything could lock the user out by guessing once."""
    one_gate = gate()
    assert check_request(page(query={"k": "guess"}), one_gate, PORT).status == 403
    assert check_request(page(query={"k": TOKEN}), one_gate, PORT) is None


def test_the_security_headers_cover_referrer_framing_and_sniffing():
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert SECURITY_HEADERS["Cache-Control"] == "no-store"


def test_the_policy_allows_framing_https_only_and_forbids_inline_script():
    assert "frame-src https:" in CSP
    assert "script-src 'self'" in CSP
    assert "'unsafe-inline'" not in CSP
    assert "object-src 'none'" in CSP
    assert "form-action 'none'" in CSP


def test_a_refresh_works_once_the_url_token_has_been_spent():
    """The bug this exists for: the page drops the token from the address
    bar, so after the first load a refresh asks for a bare "/". Without the
    session cookie that answers 403 with a JSON blob instead of the page."""
    one_gate = gate()
    assert check_request(page(query={"k": TOKEN}), one_gate, PORT) is None
    refreshed = page(headers={"Cookie": f"mail_triage_session={TOKEN}"})
    assert check_request(refreshed, one_gate, PORT) is None


def test_a_forged_session_cookie_is_refused():
    refreshed = page(headers={"Cookie": "mail_triage_session=guess"})
    assert check_request(refreshed, gate(), PORT).status == 403


def test_the_cookie_does_not_authorise_the_api():
    """CSRF stays dead: a cookie is sent automatically, so it must never be
    enough to move mail. Only the header token, which no other origin can
    read out of our HTML, authorises an API call."""
    with_cookie = Request(
        method="POST", path="/api/decisions", query={},
        headers={"Host": f"127.0.0.1:{PORT}", "Cookie": f"mail_triage_session={TOKEN}"},
        body=b"{}",
    )
    assert check_request(with_cookie, gate(), PORT).status == 403


def test_the_cookie_is_httponly_and_samesite_strict():
    from mail_triage.web.security import session_cookie
    value = session_cookie(TOKEN)
    assert "HttpOnly" in value
    assert "SameSite=Strict" in value
    assert value.startswith(f"mail_triage_session={TOKEN};")


def test_one_cookie_is_found_among_several():
    request = page(headers={"Cookie": f"other=1; mail_triage_session={TOKEN}; third=3"})
    assert check_request(request, gate(), PORT) is None
