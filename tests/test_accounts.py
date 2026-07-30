"""Tests for mail_triage.accounts — mapping Apple Mail account UUIDs to names.

Injects a fake ``subprocess.run`` so no test invokes real AppleScript or
touches the real Mail application. All ids below are synthetic.
"""

from __future__ import annotations

import mail_triage.accounts as accounts_module
from mail_triage.accounts import (
    LOCAL_ACCOUNT_NAME,
    NOT_IN_MAIL,
    account_names,
    resolve_account_name,
    truncate_name,
)


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_account_names_parses_well_formed_output(monkeypatch):
    output = (
        "Work | AAAAAAAA-1111-2222-3333-444444444444\n"
        "Personal | BBBBBBBB-1111-2222-3333-444444444444\n"
    )
    monkeypatch.setattr(
        accounts_module.subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompletedProcess(output),
    )
    assert account_names() == {"AAAAAAAA": "Work", "BBBBBBBB": "Personal"}


def test_account_names_returns_empty_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        accounts_module.subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompletedProcess("", returncode=1),
    )
    assert account_names() == {}


def test_account_names_returns_empty_when_osascript_missing(monkeypatch):
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("no osascript")

    monkeypatch.setattr(accounts_module.subprocess, "run", raise_missing)
    assert account_names() == {}


def test_account_names_handles_malformed_output(monkeypatch):
    monkeypatch.setattr(
        accounts_module.subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompletedProcess(
            "garbage\n\n | \nno-uuid-here | \n | AAAAAAAA-short\n"
        ),
    )
    assert account_names() == {}


def test_account_names_handles_empty_output(monkeypatch):
    monkeypatch.setattr(
        accounts_module.subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompletedProcess(""),
    )
    assert account_names() == {}


def test_resolve_account_name_local_account():
    assert resolve_account_name("local://CCCCCCCC", {}) == LOCAL_ACCOUNT_NAME


def test_resolve_account_name_not_in_mail():
    assert resolve_account_name("imap://DDDDDDDD", {"AAAAAAAA": "Work"}) == NOT_IN_MAIL


def test_resolve_account_name_match():
    names = {"AAAAAAAA": "Work"}
    assert resolve_account_name("imap://AAAAAAAA", names) == "Work"


def test_resolve_account_name_is_case_insensitive():
    names = {"AAAAAAAA": "Work"}
    assert resolve_account_name("imap://aaaaaaaa", names) == "Work"


def test_truncate_name_leaves_short_names_untouched():
    assert truncate_name("Work", 20) == "Work"


def test_truncate_name_shortens_long_names_with_ellipsis():
    result = truncate_name("A Very Long Account Name Indeed", 10)
    assert result == "A Very Lo…"
    assert len(result) == 10
