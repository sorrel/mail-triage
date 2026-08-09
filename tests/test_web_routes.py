"""Every route, driven as a pure function. No socket, no browser, no mail."""

from __future__ import annotations

import json

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.mail_app import FakeMail
from mail_triage.model.classify import Proposal
from mail_triage.unsubscribe import UnsubscribeOption
from mail_triage.web.routes import Router
from mail_triage.web.security import Request
from mail_triage.web.session import Session

TOKEN = "a-very-secret-token"
PORT = 8765
ACCOUNTS = {"imap://AAAAAAAA": "iCloud"}


def proposal(rowid=1, folder="Filed/Orders", veto=None):
    message = MessageRow(
        rowid=rowid, sender="Shop <shop@shop.example>", subject="Order confirmed",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX", read=False,
    )
    return Proposal(
        message, None if veto else folder, 0.99, "12 filings", "sender", veto=veto
    )


def fake_mail(rowids=(1,)):
    """A mailbox the move can actually succeed in.

    ``keys`` matters: execute.py refuses to move a message whose durable
    RFC-822 key it cannot read, so without them every move is a *failure*
    and the test passes for the wrong reason.
    """
    return FakeMail(
        inbox=list(rowids),
        mailboxes=["Filed/Orders", "Filed/Keep", "Deleted Messages"],
        keys={rowid: f"<message-{rowid}@shop.example>" for rowid in rowids},
    )


def build(tmp_path, proposals=None, mail=None):
    config = Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")
    session = Session(proposals if proposals is not None else [proposal()])
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    router = Router(
        session=session, config=config, mail=mail or fake_mail(),
        accounts=ACCOUNTS, static_dir=static, token=TOKEN, port=PORT,
    )
    return router, session


def api(path, method="GET", payload=None):
    return Request(
        method=method, path=path, query={},
        headers={"Host": f"127.0.0.1:{PORT}", "X-Mail-Triage-Token": TOKEN},
        body=json.dumps(payload).encode() if payload is not None else b"",
    )


def option(method="mailto", target="leave@shop.example", sender="news@shop.example", subject="unsubscribe"):
    return UnsubscribeOption(
        sender=sender, domain=sender.rpartition("@")[2], method=method, target=target,
        message_count=1, unread_count=1, deleted_count=40, account="iCloud", subject=subject,
    )


# --- proposals --------------------------------------------------------------

def test_proposals_are_served_as_json(tmp_path):
    router, _ = build(tmp_path)
    response = router.handle(api("/api/proposals"))
    assert response.status == 200
    body = json.loads(response.body)
    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["subject"] == "Order confirmed"


def test_an_unauthenticated_request_never_reaches_a_route(tmp_path):
    router, _ = build(tmp_path)
    denied = router.handle(Request(
        method="GET", path="/api/proposals", query={},
        headers={"Host": f"127.0.0.1:{PORT}"}, body=b"",
    ))
    assert denied.status == 403


def test_an_unknown_path_is_a_404(tmp_path):
    router, _ = build(tmp_path)
    assert router.handle(api("/api/nonsense")).status == 404


# --- deciding ---------------------------------------------------------------

def test_applying_a_decision_moves_the_mail_and_reports_the_run(tmp_path):
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    response = router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "file"}]},
    ))
    assert response.status == 200
    body = json.loads(response.body)
    assert body["moved"] == 1
    assert body["failed"] == 0
    assert body["run_id"]
    assert len(mail.moved) == 1


def test_a_decision_naming_an_unknown_id_is_refused(tmp_path):
    router, _ = build(tmp_path)
    response = router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": "not-an-id", "action": "file"}]},
    ))
    assert response.status == 400


def test_the_same_message_cannot_be_applied_twice(tmp_path):
    """A double-click must not move mail twice — the second move is the one
    undo cannot reason about."""
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    payload = {"decisions": [{"id": identifier, "action": "file"}]}
    first = router.handle(api("/api/decisions", method="POST", payload=payload))
    second = router.handle(api("/api/decisions", method="POST", payload=payload))
    assert json.loads(first.body)["moved"] == 1
    assert json.loads(second.body)["moved"] == 0
    assert len(mail.moved) == 1


def test_a_vetoed_message_is_refused_even_when_the_page_asks_to_file_it(tmp_path):
    """The guards are the point. A client must not be able to talk past one."""
    mail = fake_mail()
    router, session = build(
        tmp_path, proposals=[proposal(veto="looks personal, may need a reply")], mail=mail
    )
    (identifier,) = session.entries
    response = router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "file"}]},
    ))
    assert response.status == 400
    assert not mail.moved


def test_a_folder_override_files_where_the_page_said(tmp_path):
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "file", "folder": "Filed/Keep"}]},
    ))
    assert mail.moved[0][1] == "Filed/Keep"


def test_binning_sends_the_message_to_the_accounts_own_trash(tmp_path):
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "bin"}]},
    ))
    assert mail.moved[0][1] == "Deleted Messages"


def test_skipping_moves_nothing(tmp_path):
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    response = router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "skip"}]},
    ))
    assert json.loads(response.body)["moved"] == 0
    assert not mail.moved


def test_a_body_that_is_not_json_is_refused(tmp_path):
    router, _ = build(tmp_path)
    request = api("/api/decisions", method="POST")
    request = Request(
        method="POST", path="/api/decisions", query={}, headers=request.headers,
        body=b"not json at all",
    )
    assert router.handle(request).status == 400


# --- undo -------------------------------------------------------------------

def test_undo_reverses_the_run_this_server_made(tmp_path):
    mail = fake_mail()
    router, session = build(tmp_path, mail=mail)
    (identifier,) = session.entries
    router.handle(api(
        "/api/decisions", method="POST",
        payload={"decisions": [{"id": identifier, "action": "file"}]},
    ))
    response = router.handle(api("/api/undo", method="POST", payload={}))
    assert response.status == 200
    assert json.loads(response.body)["reversed"] == 1


def test_undo_before_anything_was_applied_is_refused(tmp_path):
    router, _ = build(tmp_path)
    assert router.handle(api("/api/undo", method="POST", payload={})).status == 400


# --- unsubscribing ----------------------------------------------------------

def test_unsubscribe_candidates_are_listed_from_the_injected_source(tmp_path):
    router, _ = build(tmp_path)
    router.unsubscribe_source = lambda: [option()]
    response = router.handle(api("/api/unsubscribe"))
    assert response.status == 200
    assert json.loads(response.body)["candidates"][0]["deleted_count"] == 40


def test_sending_an_unsubscribe_uses_the_mailto_path_with_the_senders_token(tmp_path):
    mail = fake_mail()
    router, _ = build(tmp_path, mail=mail)
    router.unsubscribe_source = lambda: [option(subject="unsub/CgxnVLz")]
    response = router.handle(api(
        "/api/unsubscribe/send", method="POST", payload={"sender": "news@shop.example"},
    ))
    assert response.status == 200
    assert json.loads(response.body)["sent"] is True
    assert mail.sent == [("leave@shop.example", "unsub/CgxnVLz")]


def test_an_http_target_is_never_sent_by_the_server(tmp_path):
    """The tool makes no outbound HTTP request. The browser frames it instead."""
    mail = fake_mail()
    router, _ = build(tmp_path, mail=mail)
    router.unsubscribe_source = lambda: [
        option(method="http", target="https://other.example/unsub?t=abc")
    ]
    response = router.handle(api(
        "/api/unsubscribe/send", method="POST", payload={"sender": "news@shop.example"},
    ))
    assert response.status == 400
    assert not mail.sent


def test_sending_to_a_sender_that_is_not_a_candidate_is_refused(tmp_path):
    mail = fake_mail()
    router, _ = build(tmp_path, mail=mail)
    router.unsubscribe_source = lambda: []
    response = router.handle(api(
        "/api/unsubscribe/send", method="POST",
        payload={"sender": "stranger@elsewhere.example"},
    ))
    assert response.status == 400
    assert not mail.sent


def test_the_candidate_list_is_read_once_not_per_request(tmp_path):
    """Each entry costs an AppleScript round trip."""
    calls = []

    def source():
        calls.append(1)
        return [option()]

    router, _ = build(tmp_path)
    router.unsubscribe_source = source
    router.handle(api("/api/unsubscribe"))
    router.handle(api("/api/unsubscribe"))
    assert len(calls) == 1


# --- static files -----------------------------------------------------------

def test_the_served_page_carries_the_token_and_the_file_on_disk_does_not(tmp_path):
    router, _ = build(tmp_path)
    (tmp_path / "static" / "index.html").write_text('<meta content="__TOKEN__">')
    response = router.handle(Request(
        method="GET", path="/", query={"k": TOKEN},
        headers={"Host": f"127.0.0.1:{PORT}"}, body=b"",
    ))
    assert response.status == 200
    assert TOKEN.encode() in response.body
    assert b"__TOKEN__" not in response.body
    assert (tmp_path / "static" / "index.html").read_text() == '<meta content="__TOKEN__">'


def asset(path):
    """A subresource request as a *browser* makes it: cookie, no custom
    header. A browser cannot put one on a <link> or a <script>."""
    return Request(
        method="GET", path=path, query={},
        headers={
            "Host": f"127.0.0.1:{PORT}",
            "Cookie": f"mail_triage_session={TOKEN}",
        },
        body=b"",
    )


def test_a_path_climbing_out_of_the_static_directory_is_refused(tmp_path):
    router, _ = build(tmp_path)
    (tmp_path / "secret.txt").write_text("not for you")
    for path in ("/../secret.txt", "/../../secret.txt", "//../secret.txt"):
        assert router.handle(asset(path)).status == 404, path


def test_a_static_file_carries_the_security_headers(tmp_path):
    router, _ = build(tmp_path)
    (tmp_path / "static" / "app.css").write_text("body { margin: 0 }")
    response = router.handle(asset("/app.css"))
    assert response.status == 200
    assert response.extra_headers["Referrer-Policy"] == "no-referrer"
    assert "frame-src https:" in response.extra_headers["Content-Security-Policy"]


def test_the_page_sets_a_session_cookie_so_a_refresh_still_works(tmp_path):
    router, _ = build(tmp_path)
    (tmp_path / "static" / "index.html").write_text("<h1>Triage</h1>")
    response = router.handle(Request(
        method="GET", path="/", query={"k": TOKEN},
        headers={"Host": f"127.0.0.1:{PORT}"}, body=b"",
    ))
    assert response.status == 200
    assert response.extra_headers["Set-Cookie"].startswith(f"mail_triage_session={TOKEN};")

    refreshed = router.handle(Request(
        method="GET", path="/", query={},
        headers={"Host": f"127.0.0.1:{PORT}", "Cookie": f"mail_triage_session={TOKEN}"},
        body=b"",
    ))
    assert refreshed.status == 200


def test_a_stylesheet_does_not_set_a_cookie(tmp_path):
    router, _ = build(tmp_path)
    (tmp_path / "static" / "app.css").write_text("body { margin: 0 }")
    response = router.handle(api("/app.css"))
    assert "Set-Cookie" not in response.extra_headers
