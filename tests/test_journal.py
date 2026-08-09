"""Tests for the run journal: entries must be recoverable even if the process
dies mid-batch, and undo must never re-move a message it has already reversed.
"""

from __future__ import annotations

import pytest

from mail_triage.config import Config
from mail_triage.journal import Journal, JournalEntry, list_runs, new_run_id, undo_run
from mail_triage.mail_app import FakeMail, MailNotRunningError


def make_config(tmp_path):
    return Config(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)


def test_entries_round_trip(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(
        JournalEntry(message_id=1, subject="Order", from_folder="INBOX", to_folder="Orders", status="planned")
    )
    assert [entry.message_id for entry in Journal(config).load("run-1")] == [1]


def test_entry_round_trips_its_message_key(tmp_path):
    """message_key is the durable RFC-822 Message-ID undo depends on — it must
    survive a write/read cycle just like every other field."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(
        JournalEntry(
            message_id=1, subject="Order", from_folder="INBOX", to_folder="Orders",
            status="planned", message_key="<order@example.com>",
        )
    )
    assert Journal(config).load("run-1")[0].message_key == "<order@example.com>"


def test_mark_updates_status(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    journal.mark(1, "moved")
    assert Journal(config).load("run-1")[0].status == "moved"


def test_mark_uses_in_memory_cache_not_a_full_reread(tmp_path):
    """mark() must not need to re-parse the whole file to find the entry it is
    updating — it can use the state already built up by begin()/record() in
    this instance. Proven by removing the file after begin()/record(): if
    mark() depended on re-reading, it would raise or silently no-op."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    path = config.journal_dir / "run-1.jsonl"
    path.unlink()
    journal.mark(1, "moved")
    # The mark() call must have recreated the file via its own append.
    assert Journal(config).load("run-1")[0].status == "moved"


def test_mark_on_unrecorded_message_raises_rather_than_silently_creating_an_entry(tmp_path):
    """Marking an id that was never record()-ed is a programming error, not a
    silent no-op — silently accepting it could hide a lost entry."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    with pytest.raises(KeyError):
        journal.mark(999, "moved")


def test_repeated_marks_keep_only_the_latest_status_on_load(tmp_path):
    """The journal is append-only, so load() must fold repeated entries for
    the same message down to the last one written, not the first."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    journal.mark(1, "moved")
    journal.mark(1, "undone")
    entries = Journal(config).load("run-1")
    assert len(entries) == 1
    assert entries[0].status == "undone"


def test_run_ids_are_unique_and_sortable():
    """Unique, and ordered by time to the second.

    Two ids from the same second no longer compare in creation order — the
    random suffix that makes them unique is what breaks the tie, arbitrarily.
    That is the intended trade: ordering *within* one second was never
    meaningful, whereas two runs sharing a journal file corrupted both.
    """
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert first[:19] <= second[:19]


def test_list_runs_returns_newest_first(tmp_path):
    config = make_config(tmp_path)
    for run_id in ("2026-01-01T00-00-00", "2026-06-01T00-00-00"):
        journal = Journal(config)
        journal.begin(run_id)
        journal.record(JournalEntry(1, "s", "INBOX", "Orders", "planned"))
    assert list_runs(config)[0] == "2026-06-01T00-00-00"


def test_list_runs_on_empty_project_returns_empty_list(tmp_path):
    config = make_config(tmp_path)
    assert list_runs(config) == []


# --- undo_run ---------------------------------------------------------------


def test_undo_reverses_only_completed_moves(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    journal.mark(1, "moved")
    journal.record(JournalEntry(2, "Digest", "INBOX", "Newsletters", "planned", message_key="<digest@example.com>"))

    # Message 1 really is in "Orders" (the move completed); message 2 is still
    # in INBOX (its move was only ever "planned", never carried out).
    mail = FakeMail(
        inbox=[2],
        mailboxes=["Orders", "Newsletters", "INBOX"],
        folders={"Orders": [1]},
        keys={1: "<order@example.com>"},
    )

    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 1
    assert failed == 0
    assert mail.moved == [(1, "INBOX", "iCloud", "Orders")]
    # Message 2 was never touched — still exactly where it started; message 1
    # is back in the inbox after being reversed from "Orders".
    assert mail.inbox_message_ids("iCloud") == [2, 1]
    assert mail.folder_message_ids("Newsletters") == []


def test_undo_recovers_a_planned_entry_whose_move_actually_completed(tmp_path):
    """The dangerous case: the process died between the move succeeding and
    the journal being marked "moved", so the entry is stuck at "planned" even
    though the message really is sitting in to_folder. Undo must still find
    and reverse it rather than silently leaving it stranded."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    # No .mark("moved") — simulating the crash — but the message is really there.
    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [1]},
        keys={1: "<order@example.com>"},
    )

    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 1
    assert failed == 0
    assert mail.moved == [(1, "INBOX", "iCloud", "Orders")]
    assert Journal(config).load("run-1")[0].status == "undone"


def test_undo_does_not_count_a_never_attempted_planned_entry_as_failed(tmp_path):
    """A "planned" entry whose move never happened at all (message still
    sitting in from_folder) is not an error — there is nothing to reverse —
    but it must not be silently forgotten either: it should be visible in the
    journal under a distinct terminal status, not left at "planned" forever."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    mail = FakeMail(inbox=[1], mailboxes=["Orders", "INBOX"])

    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 0
    assert failed == 0
    assert mail.moved == []
    status = Journal(config).load("run-1")[0].status
    assert status != "planned"


def test_running_undo_twice_does_not_move_the_message_back_twice(tmp_path):
    """Undo of an undo: once a message has been reversed, a second undo_run
    on the same run must be a no-op for it, not move it again."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    journal.mark(1, "moved")
    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [1]},
        keys={1: "<order@example.com>"},
    )

    first = undo_run("run-1", config, mail, account="iCloud")
    second = undo_run("run-1", config, mail, account="iCloud")

    assert first == (1, 0)
    assert second == (0, 0)
    assert mail.moved == [(1, "INBOX", "iCloud", "Orders")]


def test_undo_leaves_a_genuine_failure_retryable(tmp_path):
    """If Mail isn't running, the whole batch fails — but the journal must
    not mark those entries as permanently done, or a retry after the user
    reopens Mail would silently skip them."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    journal.mark(1, "moved")

    class _DownMail(FakeMail):
        def move_message(self, message_id, folder, account, source_folder="INBOX",
                         message_key=None, source_account=None):
            raise MailNotRunningError("Mail is not running.")

    mail = _DownMail(
        inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [1]},
        keys={1: "<order@example.com>"},
    )
    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 0
    assert failed == 1
    assert Journal(config).load("run-1")[0].status == "moved"


def test_undo_of_pre_migration_entry_without_message_key_warns_and_is_not_reversed(tmp_path):
    """A journal written before message_key existed has no durable identity to
    undo by — the numeric id recorded at move time is worthless once a move
    has happened. undo_run must not crash and must not silently pretend to
    have reversed it; it must warn, naming the message, and count it as
    unreversed rather than leaving the caller thinking undo succeeded."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    # A line with no "message_key" key at all, exactly as an old journal would
    # have written it before this field existed.
    path = config.journal_dir / "run-1.jsonl"
    with path.open("a") as handle:
        handle.write(
            '{"message_id": 1, "subject": "Order", "from_folder": "INBOX", '
            '"to_folder": "Orders", "status": "moved"}\n'
        )
    mail = FakeMail(inbox=[], mailboxes=["Orders", "INBOX"], folders={"Orders": [1]})

    with pytest.warns(RuntimeWarning, match="message_key"):
        reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 0
    assert failed == 1
    assert mail.moved == []


def test_undo_of_unknown_run_raises(tmp_path):
    config = make_config(tmp_path)
    mail = FakeMail(inbox=[], mailboxes=[])
    with pytest.raises(FileNotFoundError):
        undo_run("nonexistent-run", config, mail, account="iCloud")


def test_undo_reverses_by_message_key_not_numeric_id(tmp_path):
    """The numeric AppleScript id recorded at move time is stale by the time
    undo runs — moving a message changes its numeric id, and moving it back
    does not restore the old value. Undo must resolve the durable RFC-822
    message_key to whatever numeric id currently holds it, not trust the
    numeric id recorded in the journal."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(
        JournalEntry(
            message_id=1,
            message_key="<abc@example.com>",
            subject="s",
            from_folder="INBOX",
            to_folder="Parent/Child",
            status="moved",
        )
    )
    mail = FakeMail(
        inbox=[],
        mailboxes=["INBOX", "Parent/Child"],
        folders={"Parent/Child": [99]},
        keys={99: "<abc@example.com>"},
    )
    reversed_count, failed = undo_run("run-1", config, mail, account="Test")

    assert reversed_count == 1
    assert failed == 0
    # The message now has numeric id 99, not the 1 recorded at move time.
    assert mail.moved == [(99, "INBOX", "Test", "Parent/Child")]


# --- surviving a truncated or corrupted journal file ------------------------


def test_load_recovers_entries_either_side_of_a_truncated_final_line(tmp_path):
    """Simulates a process killed mid-write: the last line is a partial JSON
    fragment (as ``open(...).write()`` would leave it if the kernel had only
    flushed part of the buffer). load() must still return the entries that
    were written completely before the crash, not raise and lose all of them."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    journal.record(JournalEntry(2, "Digest", "INBOX", "Newsletters", "planned"))
    path = config.journal_dir / "run-1.jsonl"
    with path.open("a") as handle:
        handle.write('{"message_id": 3, "subject": "Trunc')  # no closing brace/newline

    with pytest.warns(RuntimeWarning, match="run-1"):
        entries = Journal(config).load("run-1")

    assert sorted(entry.message_id for entry in entries) == [1, 2]


def test_load_recovers_entries_either_side_of_a_malformed_middle_line(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned"))
    path = config.journal_dir / "run-1.jsonl"
    with path.open("a") as handle:
        handle.write("not even json\n")
    journal.record(JournalEntry(2, "Digest", "INBOX", "Newsletters", "planned"))

    with pytest.warns(RuntimeWarning, match="run-1"):
        entries = Journal(config).load("run-1")

    assert sorted(entry.message_id for entry in entries) == [1, 2]


def test_undo_still_reverses_good_entries_when_the_journal_has_a_bad_line(tmp_path):
    """The consequence that matters: a single damaged line must not take down
    undo for the whole run — every cleanly-written entry must still be
    reversible."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(1, "Order", "INBOX", "Orders", "planned", message_key="<order@example.com>"))
    journal.mark(1, "moved")
    path = config.journal_dir / "run-1.jsonl"
    with path.open("a") as handle:
        handle.write("{garbage\n")
    journal.record(JournalEntry(2, "Digest", "INBOX", "Newsletters", "planned", message_key="<digest@example.com>"))
    journal.mark(2, "moved")

    mail = FakeMail(
        inbox=[], mailboxes=["Orders", "Newsletters", "INBOX"],
        folders={"Orders": [1], "Newsletters": [2]},
        keys={1: "<order@example.com>", 2: "<digest@example.com>"},
    )

    with pytest.warns(RuntimeWarning):
        reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")

    assert reversed_count == 2
    assert failed == 0
    assert sorted(mail.moved) == [
        (1, "INBOX", "iCloud", "Orders"),
        (2, "INBOX", "iCloud", "Newsletters"),
    ]


# --- Cross-account moves --------------------------------------------------------
#
# Filing Gmail into the iCloud folder structure means a message's source and
# destination are in different Mail accounts. A journal that records only
# folders cannot reverse that: undo would look for the message in the wrong
# account. Each entry therefore carries both accounts.

def test_an_entry_records_the_source_and_destination_accounts(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", message_key="<a@b.example>",
        from_account="Gmail", to_account="iCloud",
    ))
    entry = Journal(config).load(run_id)[0]
    assert entry.from_account == "Gmail"
    assert entry.to_account == "iCloud"


def test_an_older_journal_without_accounts_still_loads(tmp_path):
    """Journals written before cross-account support must remain undoable."""
    config = make_config(tmp_path)
    run_id = new_run_id()
    path = config.journal_dir / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"message_id": 1, "subject": "s", "from_folder": "INBOX", '
        '"to_folder": "Orders", "status": "moved", "message_key": "<a@b.example>"}\n'
    )
    entry = Journal(config).load(run_id)[0]
    assert entry.from_account == ""
    assert entry.to_account == ""


def test_undo_returns_a_message_to_the_account_it_came_from(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", message_key="<one@example.com>",
        from_account="Gmail", to_account="iCloud",
    ))
    mail = FakeMail(inbox=[], mailboxes=["Parent/Child", "INBOX"],
                    folders={"Parent/Child": [1]}, keys={1: "<one@example.com>"})
    reversed_count, failed = undo_run(run_id, config, mail, "iCloud")
    assert (reversed_count, failed) == (1, 0)
    # It must be put back into the Gmail account, not the run's default.
    assert [(entry[1], entry[2]) for entry in mail.moved] == [("INBOX", "Gmail")]


def test_undo_falls_back_to_the_given_account_when_none_was_recorded(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Orders",
        status="moved", message_key="<one@example.com>",
    ))
    mail = FakeMail(inbox=[], mailboxes=["Orders", "INBOX"],
                    folders={"Orders": [1]}, keys={1: "<one@example.com>"})
    undo_run(run_id, config, mail, "iCloud")
    assert [entry[2] for entry in mail.moved] == ["iCloud"]


# --- Undoing a cross-account move -----------------------------------------------
#
# A Gmail message filed into the iCloud tree has a different account at each
# end. The reversal must look for it where it is *now* (to_folder of
# to_account) and return it to where it came from (from_folder of
# from_account). Looking in the wrong account simply finds nothing.

def test_undo_returns_a_cross_account_move_to_its_source(tmp_path):
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", from_account="Gmail", to_account="iCloud",
        message_key="<one@example.com>",
    ))
    mail = FakeMail(
        inbox=[], mailboxes=["INBOX", "Parent/Child"],
        accounts={"Gmail": {"INBOX": []}, "iCloud": {"Parent/Child": [1]}},
        keys={1: "<one@example.com>"},
    )
    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")
    assert (reversed_count, failed) == (1, 0)
    assert mail.inbox_message_ids("Gmail") == [1]
    assert mail.folder_message_ids("Parent/Child", account="iCloud") == []


def test_undo_of_a_single_account_move_is_unchanged(tmp_path):
    """A journal with no accounts recorded still reverses against the fallback."""
    config = make_config(tmp_path)
    journal = Journal(config)
    journal.begin("run-1")
    journal.record(JournalEntry(
        message_id=1, subject="s", from_folder="INBOX", to_folder="Parent/Child",
        status="moved", message_key="<one@example.com>",
    ))
    mail = FakeMail(inbox=[], mailboxes=["INBOX", "Parent/Child"],
                    folders={"Parent/Child": [1]}, keys={1: "<one@example.com>"})
    reversed_count, failed = undo_run("run-1", config, mail, account="iCloud")
    assert (reversed_count, failed) == (1, 0)
    assert mail.inbox_message_ids("iCloud") == [1]


def test_two_run_ids_in_the_same_second_are_still_different():
    """Second resolution alone is not uniqueness.

    Two runs starting in the same second shared a journal file, and because
    load() folds repeated entries for a message down to the last one written,
    one run's 'failed' overwrote the other's 'moved'. Undo then skipped
    messages that really had moved — observed on 9 August 2026.
    """
    from mail_triage.journal import new_run_id

    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50


def test_a_run_id_still_sorts_by_time():
    """list_runs orders lexicographically to find the most recent, so the
    timestamp must lead and the suffix must not disturb that.

    Asserted on the *shape* and on two fabricated ids rather than against a
    hardcoded moment: comparing a freshly generated id with a fixed string
    passes or fails according to the time of day the suite happens to run,
    which is how this first went green locally and red in CI.
    """
    import re

    from mail_triage.journal import new_run_id

    earlier = "2026-08-09T14-31-07-ffff"
    later = "2026-08-09T14-31-08-0000"
    assert sorted([later, earlier]) == [earlier, later]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}", new_run_id())
