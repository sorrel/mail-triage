"""Helpers shared by more than one CLI test module.

Anything used by a single module stays in that module: this file is for the
few pieces genuinely common to several, so that a reader of one test file is
not sent here to understand it.
"""

from __future__ import annotations

import time

from mail_triage.config import Config

from tests.conftest import build_fixture_db


class StubMail:
    """Fake stand-in for ``AppleScriptMail`` (Task 11B guard wiring tests).

    Never touches real mail or shells out to ``osascript`` — headers are
    supplied directly, or a chosen error is raised, to prove the guard
    genuinely runs (and fails safe) without needing a live bridge.
    """

    def __init__(self, headers: dict[int, dict[str, str]] | None = None, error: Exception | None = None):
        self._headers = headers or {}
        self._error = error

    def message_headers(self, message_id: int) -> dict[str, str]:
        if self._error is not None:
            raise self._error
        return dict(self._headers.get(message_id, {}))


def stub_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path / "local")
    values.update(overrides)
    return Config(**values)


# --- Task 11B: the do-not-file guard must actually be wired into `triage` -----
#
# A test that only checks the command exits zero would not catch a reversion
# to the unwired state (Classifier constructed with no `guard` at all) — the
# defect the coordinator's review caught. These assert the guard's effect on
# the outcome: a message with real filing history is held back specifically
# because its headers prove it isn't bulk, or because Mail can't be reached.

def triage_fixture_with_one_strong_sender(tmp_path):

    now = int(time.time())
    day = 86_400
    db_path = tmp_path / "Envelope Index"

    build_fixture_db(db_path, strong_sender_rows(now, day))
    return db_path


def strong_sender_rows(now, day):
    return [
        {"sender": "person@work.example", "subject": "Old thread", "date_sent": now - 30 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Another thread", "date_sent": now - 20 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Yet another", "date_sent": now - 10 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"sender": "person@work.example", "subject": "Fourth", "date_sent": now - 5 * day,
         "mailbox_url": "imap://AAAAAAAA/Projects", "read": 1},
        {"rowid": 900, "sender": "person@work.example", "subject": "Can you take a look",
         "date_sent": now - 1 * day, "mailbox_url": "imap://AAAAAAAA/INBOX", "read": 0},
    ]


# --- patching a name the commands share -------------------------------------

COMMAND_MODULE_NAMES = (
    "_shared", "inspect", "teach", "triage", "unsubscribe", "journal", "web",
)


def patch_all(monkeypatch, name, value):
    """Set ``name`` on every command module that imported it.

    The commands are a package of siblings sharing a handful of imports —
    ``load_config``, ``AppleScriptMail``, ``DEFAULT_DB_PATH``. A test drives
    one command and should not have to know which module that command
    currently lives in, nor break when one moves between them.

    Patching nothing is treated as an error rather than a no-op. A patch that
    silently misses is the worst outcome available here: the test would carry
    on against the *real* configuration and the real database, look like it
    passed, and prove nothing.
    """
    import importlib

    patched = 0
    for module_name in COMMAND_MODULE_NAMES:
        module = importlib.import_module(f"mail_triage.commands.{module_name}")
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched += 1
    if patched == 0:
        raise AttributeError(f"No command module has {name!r} to patch")
    return patched
