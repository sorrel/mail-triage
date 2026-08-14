"""The socket half: it binds loopback, it serves, it refuses a foreign Host."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.mail_app import FakeMail
from mail_triage.model.classify import Proposal
from mail_triage.web.routes import Router
from mail_triage.web.server import build_handler, serve
from mail_triage.web.session import Session

TOKEN = "a-very-secret-token"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    message = MessageRow(
        rowid=1, sender="shop@shop.example", subject="Order confirmed",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX", read=False,
    )
    session = Session([Proposal(message, "Filed/Orders", 0.99, "12 filings", "sender")])
    config = Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>Triage</h1>")
    port = free_port()
    router = Router(
        session=session, config=config,
        mail=FakeMail(inbox=[1], mailboxes=["Filed/Orders"]), accounts={},
        static_dir=static, token=TOKEN, port=port,
    )
    running = ThreadingHTTPServer(("127.0.0.1", port), build_handler(router))
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    yield running, port
    running.shutdown()
    running.server_close()
    thread.join(timeout=5)


def get(port, path="/api/proposals", headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Mail-Triage-Token": TOKEN, **(headers or {})},
    )
    return urllib.request.urlopen(request, timeout=5)


def test_it_binds_loopback_only(server):
    running, _ = server
    assert running.server_address[0] == "127.0.0.1"


def test_it_serves_the_api_to_a_request_carrying_the_token(server):
    _, port = server
    with get(port) as response:
        assert response.status == 200
        body = json.loads(response.read())
    assert body["proposals"][0]["subject"] == "Order confirmed"


def test_it_refuses_a_request_with_a_foreign_host_header(server):
    """The DNS-rebinding case, end to end through a real socket."""
    _, port = server
    with pytest.raises(urllib.error.HTTPError) as error:
        get(port, headers={"Host": "evil.example"})
    assert error.value.code == 403


def test_it_refuses_a_request_with_no_token(server):
    _, port = server
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/proposals")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403


def test_every_response_carries_the_security_headers(server):
    _, port = server
    with get(port) as response:
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-src https:" in response.headers["Content-Security-Policy"]


def test_the_page_is_served_once_for_the_url_token(server):
    """The second load must fail: the URL copy is spent."""
    _, port = server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?k={TOKEN}", timeout=5
    ) as response:
        assert response.status == 200
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?k={TOKEN}", timeout=5)
    assert error.value.code == 403


def test_an_oversized_body_is_refused_without_being_read(server):
    _, port = server
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/decisions",
        data=b"{}",
        headers={"X-Mail-Triage-Token": TOKEN, "Content-Length": "2000000"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 413


def test_quitting_from_the_page_stops_the_server(tmp_path):
    """q in the browser ends the run: serve() returns, and the process with
    it. The reply is written first — the page must be told, not dropped."""
    message = MessageRow(
        rowid=1, sender="shop@shop.example", subject="Order confirmed",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX", read=False,
    )
    static = tmp_path / "static"
    static.mkdir()
    router = Router(
        session=Session([Proposal(message, "Filed/Orders", 0.99, "12 filings", "sender")]),
        config=Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local"),
        mail=FakeMail(inbox=[1], mailboxes=["Filed/Orders"]), accounts={},
        static_dir=static, token=TOKEN, port=0,
    )
    ready = threading.Event()
    bound = {}

    def on_ready(url, actual_port):
        bound["port"] = actual_port
        router.port = actual_port  # the Host check compares against it
        ready.set()

    thread = threading.Thread(
        target=serve,
        kwargs={"router": router, "port": 0, "open_browser": False, "on_ready": on_ready},
        daemon=True,
    )
    thread.start()
    assert ready.wait(5)

    request = urllib.request.Request(
        f"http://127.0.0.1:{bound['port']}/api/quit",
        data=b"{}",
        headers={"X-Mail-Triage-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["stopping"] is True

    thread.join(timeout=5)
    assert not thread.is_alive()
