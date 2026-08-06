"""Unsubscribe candidates, and the one path that sends mail."""

from __future__ import annotations

import pytest

from mail_triage.config import Config, Source
from mail_triage.envelope import EnvelopeReader
from mail_triage.mail_app import FakeMail
from mail_triage.unsubscribe import (
    UnsubscribeOption,
    find_candidates,
    parse_list_unsubscribe,
    rank_candidates,
    send_unsubscribe,
    tally_senders,
)

from tests.conftest import build_fixture_db

PREFIX = "imap://AAAAAAAA/"
DAY = 86_400
NOW = 1_750_000_000


def make_config(tmp_path, **overrides):
    return Config(
        account_url_prefix=PREFIX,
        local_dir=tmp_path / "local",
        **overrides,
    )


def option(sender, *, messages=10, unread=9, deleted=0, method="mailto"):
    domain = sender.split("@", 1)[1]
    return UnsubscribeOption(
        sender=sender,
        domain=domain,
        method=method,
        target=f"leave@{domain}",
        message_count=messages,
        unread_count=unread,
        deleted_count=deleted,
    )


# --- Header parsing -------------------------------------------------------


def test_parses_a_mailto_target():
    assert parse_list_unsubscribe("<mailto:leave@list.example>") == (
        "mailto",
        "leave@list.example",
    )


def test_prefers_mailto_over_http():
    header = "<https://list.example/u?x=1>, <mailto:leave@list.example>"
    assert parse_list_unsubscribe(header) == ("mailto", "leave@list.example")


def test_returns_http_when_that_is_all_there_is():
    assert parse_list_unsubscribe("<https://list.example/u>") == (
        "http",
        "https://list.example/u",
    )


def test_strips_mailto_query_parameters():
    header = "<mailto:leave@list.example?subject=unsubscribe>"
    assert parse_list_unsubscribe(header) == ("mailto", "leave@list.example")


def test_returns_none_for_junk():
    assert parse_list_unsubscribe("not a header") is None
    assert parse_list_unsubscribe("") is None


# --- Ranking --------------------------------------------------------------


def test_ranking_puts_the_most_ignored_first():
    options = [
        option("a@x.example", messages=10, unread=1),
        option("b@y.example", messages=40, unread=39),
    ]
    assert rank_candidates(options)[0].sender == "b@y.example"


def test_deletions_count_as_ignoring():
    """A sender whose mail you bin unread is as ignored as one you never open.

    The read flag alone cannot see this: mail you deleted has left the inbox,
    and Mail marks plenty of it read on the way past.
    """
    read_but_binned = option("binned@x.example", messages=4, unread=0, deleted=30)
    merely_unread = option("unread@y.example", messages=12, unread=11, deleted=0)
    assert rank_candidates([merely_unread, read_but_binned])[0].sender == "binned@x.example"


def test_a_sender_you_actually_read_ranks_last():
    options = [
        option("read@x.example", messages=50, unread=0, deleted=0),
        option("ignored@y.example", messages=3, unread=1, deleted=2),
    ]
    assert rank_candidates(options)[-1].sender == "read@x.example"


def test_ignored_share_counts_deletions_too():
    assert option("a@x.example", messages=4, unread=0, deleted=12).ignored_share == 0.75
    assert option("a@x.example", messages=0, unread=0, deleted=0).ignored_share == 0.0


# --- Tallying -------------------------------------------------------------


def test_tally_counts_messages_and_unread_per_sender():
    reader_rows = [
        _row("news@x.example", read=False),
        _row("news@x.example", read=True),
        _row("news@x.example", read=False),
        _row("quiet@y.example", read=True),
    ]
    assert tally_senders(reader_rows) == {
        "news@x.example": (3, 2),
        "quiet@y.example": (1, 0),
    }


def test_tally_normalises_the_sender():
    rows = [_row("News <news@X.example>"), _row("news@x.example")]
    assert tally_senders(rows) == {"news@x.example": (2, 2)}


# --- Candidates -----------------------------------------------------------


def _row(sender, *, subject="Weekly", read=False, mailbox=None, days_ago=1, rowid=None):
    from mail_triage.envelope import MessageRow

    return MessageRow(
        rowid=rowid or 0,
        sender=sender,
        subject=subject,
        date_sent=NOW - days_ago * DAY,
        mailbox_url=mailbox or f"{PREFIX}INBOX",
        read=read,
    )


def test_find_candidates_uses_deleted_mail_as_evidence(tmp_path):
    """Deleted mail never reaches the inbox scan, so it must be counted apart."""
    path = tmp_path / "Envelope Index"
    rows = [
        # Two left in the inbox, unread...
        {"sender": "news@x.example", "subject": "One", "date_sent": NOW - DAY,
         "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 1},
        {"sender": "news@x.example", "subject": "Two", "date_sent": NOW - DAY,
         "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 2},
        # ...and twenty binned, which is the stronger signal.
        *[
            {"sender": "news@x.example", "subject": f"Old {n}", "date_sent": NOW - 3 * DAY,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 100 + n}
            for n in range(20)
        ],
    ]
    build_fixture_db(path, rows)
    reader = EnvelopeReader(path)
    config = make_config(tmp_path)
    mail = FakeMail(
        inbox=[1, 2],
        mailboxes=[],
        headers={1: {"List-Unsubscribe": "<mailto:leave@x.example>"}},
    )

    candidates = find_candidates(reader, config, mail, limit=10, now=NOW)

    assert [c.sender for c in candidates] == ["news@x.example"]
    assert candidates[0].deleted_count == 20
    assert candidates[0].message_count == 2
    assert candidates[0].target == "leave@x.example"
    reader.close()


def test_find_candidates_skips_senders_with_no_unsubscribe_header(tmp_path):
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "person@x.example", "subject": "Hello", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 1},
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(inbox=[1], mailboxes=[], headers={1: {"Subject": "Hello"}})

    assert find_candidates(reader, make_config(tmp_path), mail, limit=10, now=NOW) == []
    reader.close()


def test_find_candidates_counts_deletions_per_account(tmp_path):
    """Binning in one account must not be attributed to a sender in another."""
    other = "imap://BBBBBBBB/"
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "One", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 1},
            {"sender": "news@x.example", "subject": "Binned elsewhere", "date_sent": NOW - DAY,
             "mailbox_url": f"{other}Deleted%20Messages", "read": 1, "rowid": 2},
        ],
    )
    reader = EnvelopeReader(path)
    config = make_config(tmp_path)
    mail = FakeMail(
        inbox=[1], mailboxes=[], headers={1: {"List-Unsubscribe": "<mailto:leave@x.example>"}}
    )

    candidates = find_candidates(reader, config, mail, limit=10, now=NOW)

    assert candidates[0].deleted_count == 0
    reader.close()


def test_find_candidates_honours_the_limit_on_header_fetches(tmp_path):
    """Each header fetch is an AppleScript round trip, so the cap must bite
    before the fetching, not after it."""
    path = tmp_path / "Envelope Index"
    rows = [
        {"sender": f"s{n}@x.example", "subject": "Hi", "date_sent": NOW - DAY,
         "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": n}
        for n in range(1, 11)
    ]
    build_fixture_db(path, rows)
    reader = EnvelopeReader(path)
    fetched: list[int] = []

    class CountingMail(FakeMail):
        def message_headers(self, message_id, mailbox=None, account=None):
            fetched.append(message_id)
            return {"List-Unsubscribe": "<mailto:leave@x.example>"}

    mail = CountingMail(inbox=list(range(1, 11)), mailboxes=[])
    find_candidates(reader, make_config(tmp_path), mail, limit=3, now=NOW)

    assert len(fetched) == 3
    reader.close()


def test_find_candidates_spans_every_source(tmp_path):
    gmail = "imap://BBBBBBBB/"
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "One", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 1},
            {"sender": "list@y.example", "subject": "Two", "date_sent": NOW - DAY,
             "mailbox_url": f"{gmail}%5BGmail%5D/All%20Mail", "read": 0, "rowid": 2,
             "labels": [f"{gmail}INBOX"]},
        ],
    )
    reader = EnvelopeReader(path)
    config = make_config(
        tmp_path,
        sources=[
            Source(name="iCloud", prefix=PREFIX),
            Source(name="Gmail", prefix=gmail, trash="[Gmail]/Bin", ignore=["[[]Gmail]*"]),
        ],
    )
    mail = FakeMail(
        inbox=[1, 2],
        mailboxes=[],
        headers={
            1: {"List-Unsubscribe": "<mailto:leave@x.example>"},
            2: {"List-Unsubscribe": "<mailto:leave@y.example>"},
        },
    )

    candidates = find_candidates(reader, config, mail, limit=10, now=NOW)

    assert {c.sender for c in candidates} == {"news@x.example", "list@y.example"}
    reader.close()


def test_find_candidates_reaches_senders_with_nothing_left_in_the_inbox(tmp_path):
    """The best candidates have no inbox mail at all — that is why they qualify.

    A sender you bin on sight leaves nothing behind for an inbox scan to find,
    so drawing candidates from the inbox alone hides exactly the senders worth
    leaving. Their header comes from a message still in the Trash.
    """
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": f"Binned {n}", "date_sent": NOW - 2 * DAY,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 50 + n}
            for n in range(9)
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(
        inbox=[],
        mailboxes=["Deleted Messages"],
        headers={50: {"List-Unsubscribe": "<mailto:leave@x.example>"}},
    )

    candidates = find_candidates(reader, make_config(tmp_path), mail, limit=10, now=NOW)

    assert [c.sender for c in candidates] == ["news@x.example"]
    assert candidates[0].deleted_count == 9
    assert candidates[0].message_count == 0
    assert candidates[0].target == "leave@x.example"
    reader.close()


def test_a_binned_sender_header_is_read_from_that_account_trash(tmp_path):
    """Fetching from the inbox would find nothing: the message is not there."""
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "Binned", "date_sent": NOW - 2 * DAY,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 7},
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(inbox=[], mailboxes=["Deleted Messages"])
    config = make_config(tmp_path, sources=[Source(name="iCloud", prefix=PREFIX)])

    find_candidates(reader, config, mail, limit=10, now=NOW)

    assert mail.header_reads == [(7, "Deleted Messages", "iCloud")]
    reader.close()


def test_inbox_mail_is_still_read_from_the_inbox(tmp_path):
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "Here", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 3},
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(inbox=[3], mailboxes=[])
    config = make_config(tmp_path, sources=[Source(name="iCloud", prefix=PREFIX)])

    find_candidates(reader, config, mail, limit=10, now=NOW)

    assert mail.header_reads == [(3, "INBOX", "iCloud")]
    reader.close()


def test_an_inbox_message_is_preferred_over_a_binned_one(tmp_path):
    """One fetch per sender, from the mailbox least likely to have purged it."""
    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "Binned", "date_sent": NOW - 2 * DAY,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 1},
            {"sender": "news@x.example", "subject": "Here", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}INBOX", "read": 0, "rowid": 2},
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(inbox=[2], mailboxes=["Deleted Messages"])
    config = make_config(tmp_path, sources=[Source(name="iCloud", prefix=PREFIX)])

    find_candidates(reader, config, mail, limit=10, now=NOW)

    assert mail.header_reads == [(2, "INBOX", "iCloud")]
    reader.close()


def test_deletions_outside_the_window_are_not_counted(tmp_path):
    """The Trash purges on a rolling window; the counts must match it."""
    path = tmp_path / "Envelope Index"
    config = make_config(tmp_path)
    old = NOW - (config.deletion_window_days + 5) * DAY
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "Ancient", "date_sent": old,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 1},
        ],
    )
    reader = EnvelopeReader(path)
    mail = FakeMail(inbox=[], mailboxes=["Deleted Messages"])

    assert find_candidates(reader, config, mail, limit=10, now=NOW) == []
    assert mail.header_reads == []
    reader.close()


def test_a_header_that_cannot_be_read_drops_the_candidate(tmp_path):
    """Trash purges between the snapshot and the fetch; that is not an error."""
    from mail_triage.mail_app import MessageNotFoundError

    path = tmp_path / "Envelope Index"
    build_fixture_db(
        path,
        [
            {"sender": "news@x.example", "subject": "Gone", "date_sent": NOW - DAY,
             "mailbox_url": f"{PREFIX}Deleted%20Messages", "read": 1, "rowid": 1},
        ],
    )
    reader = EnvelopeReader(path)

    class VanishedMail(FakeMail):
        def message_headers(self, message_id, mailbox=None, account=None):
            raise MessageNotFoundError("gone")

    mail = VanishedMail(inbox=[], mailboxes=["Deleted Messages"])
    assert find_candidates(reader, make_config(tmp_path), mail, limit=10, now=NOW) == []
    reader.close()


# --- Sending --------------------------------------------------------------


def test_sending_uses_the_mail_bridge():
    mail = FakeMail(inbox=[], mailboxes=[])
    send_unsubscribe(option("a@x.example", method="mailto"), mail)
    assert mail.sent == [("leave@x.example", "unsubscribe")]


def test_refuses_to_send_to_an_http_target():
    mail = FakeMail(inbox=[], mailboxes=[])
    http = UnsubscribeOption(
        "a@x.example", "x.example", "http", "https://x.example/u", 10, 9, 0
    )
    with pytest.raises(ValueError, match="mailto"):
        send_unsubscribe(http, mail)
    assert mail.sent == []


def test_refuses_a_target_that_is_not_an_address():
    """The target comes from a header the sender controls; it is not trusted."""
    mail = FakeMail(inbox=[], mailboxes=[])
    bogus = UnsubscribeOption("a@x.example", "x.example", "mailto", "not an address", 10, 9, 0)
    with pytest.raises(ValueError, match="address"):
        send_unsubscribe(bogus, mail)
    assert mail.sent == []


def test_headers_script_addresses_the_named_mailbox():
    from mail_triage.mail_app import AppleScriptMail

    script = AppleScriptMail()._headers_script(7, "Deleted Messages", "iCloud")

    assert 'mailbox "Deleted Messages" of account "iCloud"' in script
    assert "id is 7" in script


def test_headers_script_still_reads_the_inbox_by_default():
    from mail_triage.mail_app import AppleScriptMail

    assert "of inbox" in AppleScriptMail()._headers_script(7, None, None)


def test_the_outgoing_message_is_deleted_after_sending():
    """Otherwise Mail's autosave writes it to Drafts (seen live, 6 Aug 2026)."""
    from mail_triage.mail_app import AppleScriptMail

    script = AppleScriptMail()._send_script("leave@x.example", "unsubscribe", "unsubscribe")

    assert "delete newMessage" in script
    # Order matters absolutely: deleting first would send nothing at all.
    assert script.index("send newMessage") < script.index("delete newMessage")


def test_send_mail_escapes_its_arguments():
    """A quote in a sender-supplied address must not break out of the script."""
    from mail_triage.mail_app import AppleScriptMail

    script = AppleScriptMail()._send_script('a"b@x.example', 'un"subscribe', "body")

    assert '\\"' in script
    assert 'a"b@x.example' not in script
