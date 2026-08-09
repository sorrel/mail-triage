import pytest

from mail_triage.mail_app import (
    AmbiguousMessageError,
    AppleScriptMail,
    FakeMail,
    MailboxNotFoundError,
    MailNotRunningError,
    MessageNotFoundError,
    _classify_move_failure,
    _escape_applescript_string,
    _parse_headers,
)

# Real AppleScript/AppleEvent error text captured from the live application
# (account and folder names replaced with neutral placeholders — the shape,
# the typographic apostrophe and the trailing error code are what matter).
_REAL_MESSAGE_NOT_FOUND_ERROR = (
    'Mail got an error: Can’t get message 1 of mailbox "INBOX" of account '
    '"iCloud" whose id = 9.99999999E+8. Invalid index. (-1719)'
)
_REAL_MAILBOX_NOT_FOUND_ERROR = (
    'Mail got an error: Can’t get mailbox "Parent/Child" of account '
    '"iCloud". (-1728)'
)


def test_fake_lists_inbox():
    mail = FakeMail(inbox=[1, 2, 3], mailboxes=["Orders"])
    assert mail.inbox_message_ids("iCloud") == [1, 2, 3]


def test_fake_move_records_the_move():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    mail.move_message(1, "Orders", "iCloud")
    assert mail.moved == [(1, "Orders", "iCloud", "INBOX")]
    assert mail.inbox_message_ids("iCloud") == []


def test_fake_move_to_unknown_mailbox_raises():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    with pytest.raises(MailboxNotFoundError, match="Nonexistent"):
        mail.move_message(1, "Nonexistent", "iCloud")


def test_fake_move_of_unknown_message_raises():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    with pytest.raises(MessageNotFoundError):
        mail.move_message(99, "Orders", "iCloud")


def test_fake_returns_configured_headers():
    mail = FakeMail(
        inbox=[1],
        mailboxes=["Orders"],
        headers={1: {"List-Unsubscribe": "<mailto:leave@list.example>"}},
    )
    assert mail.message_headers(1)["List-Unsubscribe"] == "<mailto:leave@list.example>"


def test_parses_folded_headers():
    raw = "Subject: A subject\nList-Unsubscribe: <mailto:a@b.example>,\n <https://c.example/u>\n"
    headers = _parse_headers(raw)
    assert headers["Subject"] == "A subject"
    assert headers["List-Unsubscribe"] == "<mailto:a@b.example>, <https://c.example/u>"


def test_fake_move_uses_configurable_source_folder():
    """Undo (Task 10) must be able to move a message back from a non-INBOX folder."""
    mail = FakeMail(inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [42]})
    mail.move_message(42, "INBOX", "iCloud", source_folder="Orders")
    assert mail.moved == [(42, "INBOX", "iCloud", "Orders")]
    assert mail.folder_message_ids("Orders") == []
    assert mail.inbox_message_ids("iCloud") == [42]


def test_fake_move_of_missing_message_from_existing_source_folder_raises_message_not_found():
    """Source mailbox exists but doesn't contain the message: MessageNotFoundError."""
    mail = FakeMail(inbox=[1], mailboxes=["Orders", "Spam"])
    with pytest.raises(MessageNotFoundError):
        mail.move_message(99, "Orders", "iCloud", source_folder="Spam")


def test_fake_move_from_nonexistent_source_folder_raises_mailbox_not_found():
    """A source_folder that isn't a tracked mailbox at all: MailboxNotFoundError,
    matching AppleScriptMail rather than being misreported as a missing message."""
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    with pytest.raises(MailboxNotFoundError):
        mail.move_message(1, "Orders", "iCloud", source_folder="Nonexistent")


def test_escape_applescript_string_handles_quotes_and_backslashes():
    assert _escape_applescript_string('Say "hi"') == 'Say \\"hi\\"'
    assert _escape_applescript_string("back\\slash") == "back\\\\slash"


def test_escape_applescript_string_used_in_move_script_for_folder_with_quote():
    """A folder name containing a double quote must not corrupt the generated script."""
    mail = AppleScriptMail()
    script = mail._move_script(1, 'Weird "Folder"', "iCloud", "INBOX")
    # The embedded quote must be escaped, not left to terminate the AppleScript string early.
    assert 'mailbox "Weird \\"Folder\\"" of account "iCloud"' in script


def test_escape_applescript_string_escapes_newlines():
    assert _escape_applescript_string("Weird\nFolder") == "Weird\\nFolder"
    assert _escape_applescript_string("Weird\rFolder") == "Weird\\rFolder"


def test_classify_move_failure_on_real_message_not_found_text():
    """A missing message's error text is itself phrased around 'mailbox', so
    naive prose matching on 'mailbox' would misclassify this as a missing
    mailbox. Classification must go by the trailing AppleEvent error code."""
    error = _classify_move_failure(
        _REAL_MESSAGE_NOT_FOUND_ERROR,
        message_id=999999999,
        folder="Parent/Child",
        account="iCloud",
        source_folder="INBOX",
    )
    assert isinstance(error, MessageNotFoundError)


def test_classify_move_failure_on_real_mailbox_not_found_text():
    error = _classify_move_failure(
        _REAL_MAILBOX_NOT_FOUND_ERROR,
        message_id=1,
        folder="Parent/Child",
        account="iCloud",
        source_folder="INBOX",
    )
    assert isinstance(error, MailboxNotFoundError)


def test_applescript_mail_move_message_raises_message_not_found(monkeypatch):
    """End-to-end: a real 'invalid index' failure from osascript must surface
    as MessageNotFoundError, not MailboxNotFoundError."""

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = _REAL_MESSAGE_NOT_FOUND_ERROR

    monkeypatch.setattr(
        "mail_triage.mail_app.subprocess.run", lambda *a, **k: _FakeCompletedProcess()
    )
    mail = AppleScriptMail()
    with pytest.raises(MessageNotFoundError):
        mail.move_message(999999999, "Parent/Child", "iCloud")


def test_applescript_mail_move_message_raises_mailbox_not_found(monkeypatch):
    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = _REAL_MAILBOX_NOT_FOUND_ERROR

    monkeypatch.setattr(
        "mail_triage.mail_app.subprocess.run", lambda *a, **k: _FakeCompletedProcess()
    )
    mail = AppleScriptMail()
    with pytest.raises(MailboxNotFoundError):
        mail.move_message(1, "Parent/Child", "iCloud")


def test_applescript_mail_move_message_still_raises_mail_not_running(monkeypatch):
    """MailNotRunningError classification (by '-600' / 'not running') must not
    be swallowed by the new move-failure classifier."""

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "Mail got an error: Application isn’t running. (-600)"

    monkeypatch.setattr(
        "mail_triage.mail_app.subprocess.run", lambda *a, **k: _FakeCompletedProcess()
    )
    mail = AppleScriptMail()
    with pytest.raises(MailNotRunningError):
        mail.move_message(1, "Parent/Child", "iCloud")


def test_fake_message_key_returns_configured_key():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys={1: "<abc@example.com>"})
    assert mail.message_key(1, source_folder="INBOX", account="iCloud") == "<abc@example.com>"


def test_fake_message_key_defaults_to_empty_string_when_unconfigured():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"])
    assert mail.message_key(1, source_folder="INBOX", account="iCloud") == ""


def test_fake_message_key_requires_source_folder_and_account_arguments():
    """MailInterface.message_key requires source_folder and account explicitly
    — no INBOX/empty-account defaults that would silently query the wrong
    place. A caller that omits either must get a TypeError, not a quiet
    fallback."""
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys={1: "<abc@example.com>"})
    with pytest.raises(TypeError):
        mail.message_key(1)


def test_fake_message_key_from_nonexistent_mailbox_raises():
    mail = FakeMail(inbox=[1], mailboxes=["Orders"], keys={1: "<abc@example.com>"})
    with pytest.raises(MailboxNotFoundError):
        mail.message_key(1, source_folder="Nonexistent", account="iCloud")


def test_fake_move_by_message_key_resolves_to_current_numeric_id():
    """Mirrors the real bridge: the numeric id recorded at move time may no
    longer hold the message (it changes on every move), so a message_key
    lookup must resolve to whatever numeric id currently holds that key in
    source_folder, not the stale id the caller happens to pass."""
    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [99]},
        keys={99: "<abc@example.com>"},
    )
    mail.move_message(1, "INBOX", "iCloud", source_folder="Orders", message_key="<abc@example.com>")
    assert mail.moved == [(99, "INBOX", "iCloud", "Orders")]
    assert mail.inbox_message_ids("iCloud") == [99]


def test_fake_move_by_unknown_message_key_raises_message_not_found():
    mail = FakeMail(inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [99]}, keys={99: "<a@example.com>"})
    with pytest.raises(MessageNotFoundError):
        mail.move_message(1, "INBOX", "iCloud", source_folder="Orders", message_key="<missing@example.com>")


def test_fake_move_by_message_key_matching_multiple_messages_raises_ambiguous():
    """Message-IDs are supposed to be unique but duplicates genuinely occur
    (a copied message, a direct-plus-list-copy delivery, a re-delivery).
    Moving an arbitrary one of them silently would risk moving the wrong
    copy — undo must refuse rather than guess."""
    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [1, 2]},
        keys={1: "<dup@example.com>", 2: "<dup@example.com>"},
    )
    with pytest.raises(AmbiguousMessageError):
        mail.move_message(1, "INBOX", "iCloud", source_folder="Orders", message_key="<dup@example.com>")
    # Refusing to act means neither message moved.
    assert mail.folder_message_ids("Orders") == [1, 2]
    assert mail.moved == []


def test_applescript_move_script_uses_message_key_lookup_when_given():
    """When a message_key is supplied, the generated script must look the
    message up by its durable RFC-822 id, not the numeric one — the numeric
    id is unreliable by the time undo runs."""
    mail = AppleScriptMail()
    script = mail._move_script(1, "Orders", "iCloud", "INBOX", message_key="<abc@example.com>")
    assert 'whose message id is "<abc@example.com>"' in script
    assert "whose id is 1" not in script


def test_applescript_move_script_uses_numeric_id_when_no_message_key_given():
    mail = AppleScriptMail()
    script = mail._move_script(1, "Orders", "iCloud", "INBOX")
    assert "whose id is 1" in script


def test_applescript_move_script_by_key_counts_matches_before_moving():
    """The message_key branch must not use 'first message ... whose message
    id is' — that silently takes an arbitrary match when the id is
    duplicated. It must fetch every match and check the count before
    committing to one."""
    mail = AppleScriptMail()
    script = mail._move_script(1, "Orders", "iCloud", "INBOX", message_key="<abc@example.com>")
    assert "first message" not in script
    assert "count of matchingMessages" in script
    assert "item 1 of matchingMessages" in script


def test_applescript_move_script_key_branch_escapes_quotes_backslashes_and_angle_brackets():
    """The escaping regression class the folder-name branch already covers
    (test_escape_applescript_string_used_in_move_script_for_folder_with_quote)
    must also hold on the message_key branch — a real Message-ID is wrapped
    in angle brackets and can, in principle, carry a quote or backslash."""
    mail = AppleScriptMail()
    tricky_key = 'weird"key\\with<angle>brackets@example.com'
    script = mail._move_script(1, "Orders", "iCloud", "INBOX", message_key=tricky_key)
    escaped = _escape_applescript_string(tricky_key)
    assert f'whose message id is "{escaped}"' in script
    # The raw unescaped quote/backslash must not appear unescaped in the script.
    assert 'id is "weird"key' not in script


def test_classify_move_failure_on_ambiguous_message_key_text():
    """Our own script raises a distinct, literal marker (not a real AppleEvent
    error number) when a message_key matches more than one message — this
    must classify as AmbiguousMessageError, not MailboxNotFoundError or
    MessageNotFoundError."""
    error = _classify_move_failure(
        'mail-triage: ambiguous message_key — message id "<dup@example.com>" '
        'matches 2 messages in mailbox "Orders".',
        message_id=1,
        folder="Orders",
        account="iCloud",
        source_folder="INBOX",
    )
    assert isinstance(error, AmbiguousMessageError)


def test_applescript_mail_move_message_by_key_raises_ambiguous_message_error(monkeypatch):
    """End-to-end: a script failure carrying our ambiguous-match marker must
    surface as AmbiguousMessageError from move_message."""

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = (
            'mail-triage: ambiguous message_key — message id "<dup@example.com>" '
            'matches 2 messages in mailbox "Orders".'
        )

    monkeypatch.setattr(
        "mail_triage.mail_app.subprocess.run", lambda *a, **k: _FakeCompletedProcess()
    )
    mail = AppleScriptMail()
    with pytest.raises(AmbiguousMessageError):
        mail.move_message(1, "Somewhere", "iCloud", message_key="<dup@example.com>")


def test_applescript_message_key_script_reads_message_id_by_numeric_id(monkeypatch):
    """Capturing the durable key must happen via the numeric id — this is the
    only point at which the numeric id is still guaranteed correct, i.e.
    before any move."""
    captured = {}

    def fake_run(args, capture_output, text, timeout):
        captured["script"] = args[2]

        class _Result:
            returncode = 0
            stdout = "<abc@example.com>\n"
            stderr = ""

        return _Result()

    monkeypatch.setattr("mail_triage.mail_app.subprocess.run", fake_run)
    mail = AppleScriptMail()
    result = mail.message_key(1, source_folder="INBOX", account="iCloud")
    assert result == "<abc@example.com>"
    assert "whose id is 1" in captured["script"]
    assert "message id of theMessage" in captured["script"]


def test_parse_headers_keeps_last_occurrence_of_a_repeated_header():
    """Documents current behaviour: repeated headers (e.g. Received) collapse
    to their last occurrence rather than being lost entirely undocumented."""
    raw = "Received: first hop\nReceived: second hop\nSubject: A subject\n"
    headers = _parse_headers(raw)
    assert headers["Received"] == "second hop"
    assert headers["Subject"] == "A subject"


# --- Cross-account moves --------------------------------------------------------
#
# Filing a Gmail message into the iCloud tree means a different account at
# each end. ``account`` stays the *target*; ``source_account`` defaults to it,
# so every within-account call is byte-for-byte what it was.

def test_move_script_addresses_source_and_target_accounts_separately():
    script = AppleScriptMail()._move_script(
        1, "Parent/Child", "iCloud", "INBOX", message_key="<k@example.com>",
        source_account="Gmail",
    )
    assert 'mailbox "Parent/Child" of account "iCloud"' in script
    assert 'of account "Gmail" whose message id is "<k@example.com>"' in script
    assert 'of account "iCloud" whose message id' not in script


def test_move_script_by_numeric_id_also_separates_the_accounts():
    script = AppleScriptMail()._move_script(
        1, "Parent/Child", "iCloud", "INBOX", source_account="Gmail"
    )
    assert 'mailbox "Parent/Child" of account "iCloud"' in script
    assert 'of account "Gmail" whose id is 1' in script


def test_move_script_defaults_the_source_account_to_the_target():
    script = AppleScriptMail()._move_script(1, "Parent/Child", "iCloud", "INBOX")
    assert script.count('of account "iCloud"') == 2


def test_fake_mail_keeps_accounts_separate():
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {"INBOX": [2]}},
    )
    assert mail.inbox_message_ids("Gmail") == [1]
    assert mail.inbox_message_ids("iCloud") == [2]


def test_fake_mail_moves_across_accounts():
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": [1]}, "iCloud": {}},
        keys={1: "<one@example.com>"},
    )
    mail.move_message(1, "Parent/Child", "iCloud", source_folder="INBOX",
                      source_account="Gmail")
    assert mail.inbox_message_ids("Gmail") == []
    assert mail.folder_message_ids("Parent/Child", account="iCloud") == [1]


def test_fake_mail_without_accounts_behaves_as_before():
    """The legacy single-namespace fake must be untouched."""
    mail = FakeMail(inbox=[1], mailboxes=["INBOX", "Parent/Child"])
    mail.move_message(1, "Parent/Child", "anything", source_folder="INBOX")
    assert mail.inbox_message_ids("anything") == []
    assert mail.folder_message_ids("Parent/Child") == [1]


def test_send_script_asks_which_account_it_sent_from():
    """The bounce comes back to the sending account, so we must know it."""
    script = AppleScriptMail()._send_script("leave@list.example", "token", "unsubscribe", "iCloud")
    assert "send newMessage" in script
    assert "email addresses of acct" in script
    # The account must be read before the compose object is discarded.
    assert script.index("email addresses of acct") < script.index("delete newMessage")


def test_fake_mail_reports_its_sending_account():
    mail = FakeMail(inbox=[], mailboxes=[], sending_account="Gmail")
    assert mail.send_mail("leave@list.example", "token", "unsubscribe", "") == "Gmail"
