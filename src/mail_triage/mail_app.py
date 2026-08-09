"""The only component that changes anything in Mail.

Everything else in mail-triage is read-only. Mutations go through AppleScript
because it is the sole supported write path — writing to Mail's SQLite database
directly corrupts it.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


# AppleScript calls are slow by nature: a ``whose`` query over a large mailbox
# costs seconds. This is a ceiling for a wedged Mail, not a target.
OSASCRIPT_TIMEOUT = 120


class MailError(RuntimeError):
    """Base class for Mail interaction failures."""


class MailNotRunningError(MailError):
    """Mail is not running. We never launch it on the user's behalf."""


class MailboxNotFoundError(MailError):
    """The target mailbox does not exist in the account."""


class MessageNotFoundError(MailError):
    """The message is no longer where we expected it."""


class AmbiguousMessageError(MailError):
    """More than one message shares a message_key in the searched mailbox.

    Message-IDs are supposed to be unique but duplicates genuinely occur (a
    message copied between mailboxes, a list that sends both a direct and a
    list copy, a re-delivered message). Silently acting on an arbitrary match
    would risk moving the wrong copy, which is worse than refusing outright —
    so this is raised instead of picking one.
    """


class MailInterface(Protocol):
    def inbox_message_ids(self, account: str) -> list[int]: ...
    def mailbox_names(self, account: str) -> list[str]: ...
    def move_message(
        self,
        message_id: int,
        folder: str,
        account: str,
        source_folder: str = "INBOX",
        message_key: str | None = None,
        source_account: str | None = None,
    ) -> None: ...
    def message_headers(
        self, message_id: int, mailbox: str | None = None, account: str | None = None
    ) -> dict[str, str]: ...
    def message_key(self, message_id: int, source_folder: str, account: str) -> str: ...
    def message_exists(self, message_key: str, folder: str, account: str) -> bool: ...
    def send_mail(
        self, to_address: str, subject: str, body: str, from_account: str
    ) -> str: ...


def _escape_applescript_string(value: str) -> str:
    """Escape a string for safe interpolation inside an AppleScript double-quoted literal.

    Folder names, account names and other values interpolated into generated
    AppleScript source come from the user's own mailbox names, not from an
    attacker — but an unescaped double quote or backslash would still corrupt
    the generated script and could send a message to the wrong place. Escape
    backslashes first, then double quotes, matching AppleScript's own string
    escaping rules. A literal newline or carriage return in a folder name would
    otherwise split the ``-e`` script across lines and corrupt its structure,
    so those are escaped too, using AppleScript's own ``\\n``/``\\r`` string
    escapes (which osascript reconstitutes as the real character at run time).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\n", "\\n").replace("\r", "\\r")


def _run(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT
    )
    if result.returncode != 0:
        error = result.stderr.strip()
        if "not running" in error or "-600" in error:
            raise MailNotRunningError("Mail is not running. Please open it and try again.")
        raise MailError(error)
    return result.stdout.strip()


def _run_list(script: str) -> list[str]:
    """Run ``script`` and split AppleScript's comma-separated list output.

    AppleScript renders a list as ``a, b, c``, so this is the shape every
    ``get <property> of <plural>`` query comes back in.
    """
    return [part.strip() for part in _run(script).split(", ") if part.strip()]


# AppleScript/AppleEvent error numbers, trailing in parentheses on the error
# text, e.g. "Mail got an error: Can't get mailbox ... (-1728)". Numbers are
# stable across macOS locales; the surrounding prose is not — it is localised
# and macOS renders it with a typographic apostrophe ("Can't", U+2019), so
# matching on the number is the only reliable way to tell "no such mailbox"
# apart from "no such message" apart from other failures. A missing object
# (the destination mailbox does not exist) raises -1728; an out-of-range/absent
# element (the message id is not present in the source mailbox) raises -1719.
_ERROR_NO_SUCH_OBJECT = "(-1728)"
_ERROR_INVALID_INDEX = "(-1719)"

# Marker text for the multiple-match case, raised by our own generated
# AppleScript (see ``_move_script``'s message_key branch) rather than by
# Mail itself, so there is no AppleEvent error number to key off — the text
# is ours, verbatim, so a plain substring check is exact rather than a guess.
_ERROR_AMBIGUOUS_MESSAGE_KEY = "mail-triage: ambiguous message_key"


def _classify_move_failure(
    text: str, message_id: int, folder: str, account: str, source_folder: str
) -> MailError:
    """Turn a raw AppleScript failure from a move attempt into a typed exception.

    Both native failure modes go through the same object-specifier chain
    (mailbox, then message, then move), so naive prose matching (e.g.
    "mailbox" appears in the text) cannot distinguish them: the
    message-not-found error text is *itself* phrased around the mailbox the
    message was expected to be in (see the docstring above). Classify on the
    trailing AppleEvent error number instead. The ambiguous-match case is
    raised by our own script (not by Mail), so it is classified on its own
    literal marker text rather than a number.
    """
    if _ERROR_AMBIGUOUS_MESSAGE_KEY in text:
        return AmbiguousMessageError(
            f"Message key matches more than one message in '{source_folder}'; refusing to guess which"
        )
    if _ERROR_NO_SUCH_OBJECT in text:
        return MailboxNotFoundError(f"No mailbox '{folder}' in account '{account}'")
    if _ERROR_INVALID_INDEX in text:
        return MessageNotFoundError(f"Message {message_id} not found in '{source_folder}'")
    # Unrecognised failure shape — do not guess which of the two it is.
    return MailError(text)


class AppleScriptMail:
    """Drives Mail.app via osascript."""

    def inbox_message_ids(self, account: str) -> list[int]:
        account_escaped = _escape_applescript_string(account)
        script = (
            f'tell application "Mail" to get id of messages of mailbox "INBOX" '
            f'of account "{account_escaped}"'
        )
        return [int(part) for part in _run_list(script)]

    def mailbox_names(self, account: str) -> list[str]:
        """Leaf names of the account's mailboxes.

        Note: Mail returns a flat list of LEAF names, so this cannot be used to
        match nested folders — 41 of the 44 folders in the trained model are
        nested. The authoritative folder list comes from the envelope database
        via ``folder_path``, which preserves full paths and capitalisation. This
        method is kept only for checking that an account is reachable.
        """
        account_escaped = _escape_applescript_string(account)
        script = f'tell application "Mail" to get name of mailboxes of account "{account_escaped}"'
        return _run_list(script)

    def _move_script(
        self,
        message_id: int,
        folder: str,
        account: str,
        source_folder: str,
        message_key: str | None = None,
        source_account: str | None = None,
    ) -> str:
        # ``folder`` is a full path such as "Parent/Child". Path addressing is
        # required: the user's folders are nested, and a leaf-name lookup is both
        # unable to reach them and ambiguous when a leaf name repeats.
        #
        # ``message_id`` is interpolated as a plain integer literal here, so no
        # precision is lost in the script we generate. Note, though, that when
        # Mail itself *reports* an invalid-index failure it renders the id in
        # scientific notation (e.g. "id = 9.99999999E+8"), i.e. AppleScript
        # coerces it to a real for the error message. Today's ids are around
        # 450,000 — far below the point where a double loses integer precision
        # (2**53) — but this is worth remembering if ids ever grow much larger.
        #
        # ``message_key``, when supplied, is the durable RFC-822 Message-ID and
        # is used for the lookup instead of the numeric id. This matters for
        # undo: a message's numeric id changes when it moves and moving it
        # back does not restore the old value, so by the time undo runs the
        # numeric id recorded at move time is worthless — only the message_key
        # still identifies the same message.
        #
        # ``account`` names the *target*. A Gmail message filed into the iCloud
        # tree has a different account at each end, so the source mailbox is
        # addressed with ``source_account``, which defaults to the target and
        # thereby leaves every within-account move exactly as it was. Note that
        # a cross-account move is a copy-and-delete over IMAP rather than a
        # relabelling. That holds for ordinary IMAP and Exchange, and it is
        # *false for Gmail*, where the inbox is a label: the copy lands and
        # the label stays, so the message never leaves the inbox whilst the
        # move reports success. execute.py verifies rather than believing it.
        folder_escaped = _escape_applescript_string(folder)
        account_escaped = _escape_applescript_string(account)
        source_account_escaped = _escape_applescript_string(source_account or account)
        source_escaped = _escape_applescript_string(source_folder)
        if message_key is not None:
            # A message_key lookup can match more than one message: RFC-822
            # Message-IDs are supposed to be unique but duplicates genuinely
            # occur (a message copied between mailboxes, a list that sends
            # both a direct and a list copy, a re-delivered message). Fetch
            # every match and refuse to act if there is more than one, rather
            # than "first message ... whose message id is" silently picking
            # an arbitrary one — undo moving the wrong copy without a word
            # would be worse than undo refusing to proceed.
            key_escaped = _escape_applescript_string(message_key)
            return (
                'tell application "Mail"\n'
                f'  set theBox to mailbox "{folder_escaped}" of account "{account_escaped}"\n'
                f'  set matchingMessages to (messages of mailbox "{source_escaped}" '
                f'of account "{source_account_escaped}" whose message id is "{key_escaped}")\n'
                "  if (count of matchingMessages) is 0 then\n"
                f'    error "No message with message id \\"{key_escaped}\\" in mailbox '
                f'\\"{source_escaped}\\"." number -1719\n'
                "  else if (count of matchingMessages) > 1 then\n"
                f'    error "mail-triage: ambiguous message_key — message id '
                f'\\"{key_escaped}\\" matches " & (count of matchingMessages) & '
                f'" messages in mailbox \\"{source_escaped}\\"."\n'
                "  end if\n"
                "  set theMessage to item 1 of matchingMessages\n"
                "  move theMessage to theBox\n"
                "end tell"
            )
        return (
            'tell application "Mail"\n'
            f'  set theBox to mailbox "{folder_escaped}" of account "{account_escaped}"\n'
            f'  set theMessage to (first message of mailbox "{source_escaped}" '
            f'of account "{source_account_escaped}" whose id is {message_id})\n'
            "  move theMessage to theBox\n"
            "end tell"
        )

    def message_exists(self, message_key: str, folder: str, account: str) -> bool:
        """Whether a message with this RFC-822 key is still in that mailbox.

        The question ``execute`` must ask after every move, because a move
        that reports success is not a message that left the inbox. A Gmail
        inbox is a label rather than a mailbox: a cross-account move copies
        the message and leaves the label in place, so Mail returns happily
        whilst the original sits exactly where it was. Measured on a live
        mailbox, 9 August 2026 — three attempts produced three copies in the
        destination and left the original in the Gmail inbox every time.
        """
        key_escaped = _escape_applescript_string(message_key)
        folder_escaped = _escape_applescript_string(folder)
        account_escaped = _escape_applescript_string(account)
        script = (
            'tell application "Mail"\n'
            f'  set matches to (messages of mailbox "{folder_escaped}" '
            f'of account "{account_escaped}" whose message id is "{key_escaped}")\n'
            "  return (count of matches)\n"
            "end tell"
        )
        try:
            return int(_run(script) or 0) > 0
        except MailNotRunningError:
            raise
        except (MailError, ValueError):
            # Cannot tell. Reported as still present, so the caller fails safe
            # and says the move is unproven rather than claiming success.
            return True

    def move_message(
        self,
        message_id: int,
        folder: str,
        account: str,
        source_folder: str = "INBOX",
        message_key: str | None = None,
        source_account: str | None = None,
    ) -> None:
        script = self._move_script(
            message_id, folder, account, source_folder, message_key, source_account
        )
        try:
            _run(script)
        except MailNotRunningError:
            raise
        except MailError as error:
            raise _classify_move_failure(
                str(error), message_id, folder, account, source_folder
            ) from error

    def _headers_script(
        self, message_id: int, mailbox: str | None, account: str | None
    ) -> str:
        """The AppleScript for one header read.

        With no mailbox, this reads Mail's unified ``inbox``, which is what
        the do-not-file guards want: they are looking at mail that is, by
        definition, still in an inbox. A named mailbox is needed for anything
        that has left one — the unsubscribe candidates drawn from the Trash,
        whose whole qualification is that nothing of theirs remains in the
        inbox for the unified query to find.
        """
        if mailbox is None:
            target = "inbox"
        else:
            target = f'mailbox "{_escape_applescript_string(mailbox)}"'
            if account is not None:
                target += f' of account "{_escape_applescript_string(account)}"'
        return (
            'tell application "Mail"\n'
            f"  set theMessage to (first message of {target} whose id is {message_id})\n"
            "  return all headers of theMessage\n"
            "end tell"
        )

    def message_headers(
        self, message_id: int, mailbox: str | None = None, account: str | None = None
    ) -> dict[str, str]:
        """Fetch raw headers. Mail's database does not store these."""
        return _parse_headers(_run(self._headers_script(message_id, mailbox, account)))

    def message_key(self, message_id: int, source_folder: str, account: str) -> str:
        """Return the RFC-822 Message-ID, which survives moves.

        The numeric AppleScript id does not: moving a message changes it, and
        moving it back does not restore the old value. Callers that need to
        undo a move later must capture this *before* the move, whilst the
        message is still findable by its numeric id.

        ``source_folder`` and ``account`` are required, not defaulted: a
        default of ``account=""`` cannot produce a valid query for any real
        account, and a default ``source_folder="INBOX"`` is silently wrong
        for a message anywhere else. Getting either wrong should fail loudly
        at the call site, not quietly query the wrong mailbox.
        """
        source_escaped = _escape_applescript_string(source_folder)
        account_escaped = _escape_applescript_string(account)
        script = (
            'tell application "Mail"\n'
            f'  set theMessage to (first message of mailbox "{source_escaped}" '
            f'of account "{account_escaped}" whose id is {message_id})\n'
            "  return message id of theMessage\n"
            "end tell"
        )
        return _run(script)

    def _send_script(
        self, to_address: str, subject: str, body: str, from_account: str
    ) -> str:
        """The AppleScript for one outgoing message.

        Every argument is escaped, unlike elsewhere in this module where the
        interpolated values are the user's own folder and account names. The
        recipient here is lifted from a ``List-Unsubscribe`` header, which the
        *sender* wrote: an unescaped quote in it would close the string literal
        and let the rest of the header become script. Escaping is the whole
        reason this is a separate, testable method.

        ``delete newMessage`` follows the send because the outgoing message
        object outlives it, and Mail's autosave then writes it to Drafts —
        observed on the first live send (6 August 2026), where the sent copy
        reached Sent Messages at 19:48:56 and a stray draft appeared in the
        same account at 19:49:25. Deleting the object before the autosave
        timer fires prevents the draft rather than tidying it up afterwards,
        which matters: the alternative is hunting a message in the Drafts
        mailbox and deleting it, and deleting stored mail on a guess is a far
        worse failure mode than leaving a draft behind. ``delete`` applies to
        the outgoing message — the compose object — not to anything in a
        mailbox.

        The account is resolved by matching the outgoing message's ``sender``
        against each account's ``email addresses``. Mail has no "default
        account" property worth reading, and the account is needed because
        the bounce comes back to *its* inbox. Read before ``delete``, which
        discards the compose object. An unmatched sender yields "", which the
        caller reports rather than guessing at.

        **The sender is set explicitly, before anything is composed.** The
        account is looked up first and the script errors if it does not exist
        or has no address, so a name that cannot be resolved sends nothing at
        all — rather than falling through to Mail's own default, which is
        simply the first account in its list and has no relationship to the
        list being left. Where an account has several addresses the first is
        its primary, and that is the one used; the address a given newsletter
        was delivered to is not recorded anywhere we could consult.
        """
        account_escaped = _escape_applescript_string(from_account)
        return (
            'tell application "Mail"\n'
            f'  set wanted to (every account whose name is "{account_escaped}")\n'
            "  if wanted is {} then error "
            f'"No account named {account_escaped}" number -1728\n'
            "  set fromAddresses to email addresses of item 1 of wanted\n"
            "  if fromAddresses is missing value or (count of fromAddresses) is 0 then error "
            f'"Account {account_escaped} has no address to send from" number -1728\n'
            "  set fromAddress to item 1 of fromAddresses as string\n"
            "  set newMessage to make new outgoing message with properties "
            "{sender:fromAddress, "
            f'subject:"{_escape_applescript_string(subject)}", '
            f'content:"{_escape_applescript_string(body)}", visible:false}}\n'
            "  tell newMessage\n"
            "    make new to recipient at end of to recipients with properties "
            f'{{address:"{_escape_applescript_string(to_address)}"}}\n'
            "  end tell\n"
            "  send newMessage\n"
            "  set senderValue to sender of newMessage as string\n"
            '  set accountName to ""\n'
            "  repeat with acct in accounts\n"
            "    repeat with addr in email addresses of acct\n"
            "      if senderValue contains (addr as string) then\n"
            "        set accountName to name of acct as string\n"
            "        exit repeat\n"
            "      end if\n"
            "    end repeat\n"
            '    if accountName is not "" then exit repeat\n'
            "  end repeat\n"
            "  delete newMessage\n"
            "  return accountName\n"
            "end tell"
        )

    def send_mail(self, to_address: str, subject: str, body: str, from_account: str) -> str:
        """Send from ``from_account``'s own address; return the account's name.

        The only method in mail-triage that sends anything. Callers must have
        an explicit per-message confirmation in hand before calling it.

        ``from_account`` is not optional and there is no default. Letting Mail
        choose is what broke the live send on 9 August 2026: with no ``sender``
        set, Mail composed from the first account in its list — a Yahoo account
        that is not a configured source — whilst the subscription being left
        was on iCloud. Yahoo's server would not send it, so three attempts
        produced three drafts and nothing reached the list. Had it sent, the
        request would have come from an address that never subscribed, which
        identifies nobody.

        Returns "" if the account could not be matched back from the sender,
        which the caller reports honestly rather than substituting a guess.
        """
        return _run(self._send_script(to_address, subject, body, from_account)).strip()


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse RFC-822 headers, joining folded continuation lines.

    Real messages can repeat a header name (``Received`` is the classic case,
    once per relay hop). This is a flat ``dict``, so a repeated name keeps only
    its *last* occurrence — harmless for the fields mail-triage currently reads
    (e.g. ``List-Unsubscribe``, ``Subject``), which are not normally repeated,
    but worth knowing if a future caller needs a header that legitimately is.
    """
    headers: dict[str, str] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and current:
            headers[current] += " " + line.strip()
        elif ":" in line:
            name, _, value = line.partition(":")
            current = name.strip()
            headers[current] = value.strip()
    return headers


class FakeMail:
    """In-memory stand-in so the suite never touches real mail.

    Message ids are tracked per mailbox (INBOX plus any named folders), so
    tests can exercise ``move_message``'s ``source_folder`` parameter — which
    the undo path (Task 10) needs to move a message back from wherever it was
    filed, not just from INBOX.
    """

    def __init__(
        self,
        inbox: list[int],
        mailboxes: list[str],
        headers: dict[int, dict[str, str]] | None = None,
        folders: dict[str, list[int]] | None = None,
        keys: dict[int, str] | None = None,
        accounts: dict[str, dict[str, list[int]]] | None = None,
        sending_account: str = "iCloud",
        leaves_original: bool = False,
    ) -> None:
        self._mailboxes = list(mailboxes)
        self._headers = headers or {}
        # Contents are keyed (account, folder). The legacy ``inbox``/``folders``
        # arguments land under "*", a wildcard that answers for any account
        # name — exactly the account-blind behaviour every test predating
        # cross-account filing expects. ``accounts`` names them explicitly,
        # which is what a two-account test needs so that "INBOX" in Gmail and
        # "INBOX" in iCloud do not collide.
        self._folder_contents: dict[tuple[str, str], list[int]] = {
            ("*", "INBOX"): list(inbox)
        }
        for name, ids in (folders or {}).items():
            self._folder_contents[("*", name)] = list(ids)
        for account_name, contents in (accounts or {}).items():
            for name, ids in contents.items():
                self._folder_contents[(account_name, name)] = list(ids)
        # Without ``accounts`` the fake is account-blind: every lookup and
        # every move resolves to the wildcard bucket whatever account name it
        # is handed. Anything else would strand a moved message under the
        # account it was moved to, where the legacy readers cannot see it.
        self._per_account = bool(accounts)
        # Simulates the Gmail case: a move that copies and leaves the
        # original where it was. See move_message.
        self.leaves_original = leaves_original
        # Numeric id -> durable RFC-822 message_key, mirroring the real bridge:
        # the numeric id is whatever database row currently holds the message,
        # the key travels with the message across moves. Kept per numeric id
        # (not per message) because that is exactly how ``message_key`` and the
        # key-based lookup in ``move_message`` resolve one to the other in
        # practice — via whichever numeric id currently holds a given key.
        self._keys = dict(keys or {})
        # Which account send_mail reports having sent from. Mail's default
        # account is not necessarily one this tool triages, and the bounce
        # check depends on knowing which it was.
        self._sending_account = sending_account
        self.moved: list[tuple[int, str, str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.sent_from: list[str] = []
        self.header_reads: list[tuple[int, str | None, str | None]] = []

    def _contents(self, account: str, folder: str) -> list[int]:
        """The list backing one mailbox, creating it on first use.

        Falls back to the wildcard bucket so a fake built the legacy way
        answers for whatever account name it is asked about.
        """
        if not self._per_account:
            account = "*"
        key = (account, folder)
        if key not in self._folder_contents and ("*", folder) in self._folder_contents:
            key = ("*", folder)
        return self._folder_contents.setdefault(key, [])

    def inbox_message_ids(self, account: str) -> list[int]:
        return list(self._contents(account, "INBOX"))

    def folder_message_ids(self, folder: str, account: str = "*") -> list[int]:
        return list(self._contents(account, folder))

    def mailbox_names(self, account: str) -> list[str]:
        return list(self._mailboxes)

    def message_key(self, message_id: int, source_folder: str, account: str) -> str:
        # source_folder/account are accepted (not just ignored) to match
        # MailInterface exactly and to mirror the real bridge's requirement
        # that both name a mailbox that actually exists before it will answer.
        if source_folder not in self._mailboxes and source_folder != "INBOX":
            raise MailboxNotFoundError(f"No mailbox '{source_folder}'")
        return self._keys.get(message_id, "")

    def move_message(
        self,
        message_id: int,
        folder: str,
        account: str,
        source_folder: str = "INBOX",
        message_key: str | None = None,
        source_account: str | None = None,
    ) -> None:
        source_account = source_account or account
        # Mirror AppleScriptMail's classification: a mailbox that does not
        # exist at all (destination or source) is MailboxNotFoundError; a
        # message absent from a mailbox that does exist is MessageNotFoundError.
        if folder not in self._mailboxes and folder != "INBOX":
            raise MailboxNotFoundError(f"No mailbox '{folder}'")
        if source_folder not in self._mailboxes and source_folder != "INBOX":
            raise MailboxNotFoundError(f"No mailbox '{source_folder}'")
        source_contents = self._contents(source_account, source_folder)
        if message_key is not None:
            # Resolve the durable key to whatever numeric id currently holds
            # it in source_folder — the numeric id recorded at move time is
            # not trustworthy here, exactly as with the real bridge. A key
            # is supposed to be unique but duplicates genuinely occur, so
            # every match is counted (not just the first) and more than one
            # refuses to act, mirroring AppleScriptMail's count-based check.
            matches = [mid for mid in source_contents if self._keys.get(mid) == message_key]
            if not matches:
                raise MessageNotFoundError(
                    f"No message with key '{message_key}' in '{source_folder}'"
                )
            if len(matches) > 1:
                raise AmbiguousMessageError(
                    f"Message key '{message_key}' matches {len(matches)} messages in "
                    f"'{source_folder}'; refusing to guess which"
                )
            message_id = matches[0]
        elif message_id not in source_contents:
            raise MessageNotFoundError(f"Message {message_id} not in '{source_folder}'")
        # A Gmail inbox is a label, not a mailbox, and a *cross-account* move
        # out of one copies the message without clearing the label — so the
        # original stays put whilst the move reports success. Measured on a
        # live mailbox, 9 August 2026: three attempts, three copies in the
        # destination, and the message still in the Gmail inbox each time.
        #
        # Scoped to cross-account deliberately, because that is where the
        # real boundary lies: a move *within* the account does clear the
        # label, which is exactly what makes the archive step work. Proved
        # against live Gmail on 9 August 2026 — INBOX -> [Gmail]/All Mail
        # left the message in All Mail and gone from the inbox. A fake that
        # left the original on every move could only ever test the failure.
        crossing = (source_account or account) != account
        if not (self.leaves_original and crossing):
            source_contents.remove(message_id)
        self._contents(account, folder).append(message_id)
        self.moved.append((message_id, folder, account, source_folder))

    def message_exists(self, message_key: str, folder: str, account: str) -> bool:
        return any(
            self._keys.get(mid) == message_key
            for mid in self._contents(account, folder)
        )

    def message_headers(
        self, message_id: int, mailbox: str | None = None, account: str | None = None
    ) -> dict[str, str]:
        # Recorded so a test can prove *where* a header was read from: fetching
        # a binned message from the inbox would find nothing, and a fake that
        # answered regardless would hide exactly that bug.
        self.header_reads.append((message_id, mailbox, account))
        return dict(self._headers.get(message_id, {}))

    def send_mail(self, to_address: str, subject: str, body: str, from_account: str) -> str:
        # Body deliberately not recorded: what matters to a test is that
        # exactly one message went to exactly one address. The account is
        # recorded separately, because sending from the wrong one is the
        # defect this parameter exists to prevent and a test must be able to
        # see which was used, not merely that something was sent.
        self.sent.append((to_address, subject))
        self.sent_from.append(from_account)
        return from_account or self._sending_account
