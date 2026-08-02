"""Tests for the proposal table, summary, and confirm loop.

The review loop takes an injected ``prompt`` callable precisely so it is
testable without a terminal — no test here touches stdin or a real mailbox.
"""

from __future__ import annotations

from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.review import (
    Decision, display_width, held_back, render_table, review, review_held,
    review_unplaced, summarise,
)


def proposal(
    folder="Orders",
    subject="Your order",
    confidence=0.95,
    rowid=1,
    stage="sender",
    reason="sender seen often",
    veto=None,
    veto_kind=None,
    prefix="imap://A",
    sender="orders@shop.example",
):
    message = MessageRow(
        rowid=rowid, sender=sender, subject=subject,
        date_sent=1_700_000_000, mailbox_url=f"{prefix}/INBOX", read=False,
    )
    return Proposal(message, folder, confidence, reason, stage, veto=veto,
                    veto_kind=veto_kind)


# --- brief's baseline contract -------------------------------------------------

def test_table_shows_subject_and_destination():
    table = render_table([proposal()])
    assert "Your order" in table
    assert "Orders" in table


def test_table_truncates_long_subjects():
    table = render_table([proposal(subject="x" * 200)])
    assert max(len(line) for line in table.splitlines()) < 160


def test_summary_counts_placed_and_unplaced():
    text = summarise([proposal(), proposal(folder=None, rowid=2)])
    assert "1" in text and "inbox" in text.lower()


# --- Task 11B: vetoed messages must be visible, not silently dropped ------

def test_summary_reports_vetoed_message_separately_from_no_history():
    text = summarise([
        proposal(),
        proposal(folder=None, rowid=2, veto="you flagged this"),
    ])
    assert "1" in text
    assert "you flagged this" in text


def test_summary_shows_the_vetoed_subject_and_reason_together():
    text = summarise([
        proposal(folder=None, rowid=2, subject="Contract renewal", veto="looks personal, may need a reply"),
    ])
    assert "Contract renewal" in text
    assert "looks personal, may need a reply" in text


def test_summary_names_the_account_on_held_lines_when_several_are_triaged():
    # The proposals table carries an Account column; the held-back list did
    # not, so a whole account's inbox could be held and read as ignored.
    text = summarise(
        [
            proposal(folder=None, rowid=1, subject="Assessor availability",
                     prefix="ews://C", veto="looks personal, may need a reply"),
            proposal(folder=None, rowid=2, subject="Your order", prefix="imap://A",
                     veto="you flagged this"),
        ],
        accounts={"imap://A": "iCloud", "ews://C": "Exchange"},
    )
    assert "Exchange — Assessor availability" in text
    assert "iCloud — Your order" in text


def test_summary_omits_the_account_when_only_one_is_triaged():
    # One source repeats the same name on every line and buys nothing.
    text = summarise(
        [proposal(folder=None, subject="Your order", veto="you flagged this")],
        accounts={"imap://A": "iCloud"},
    )
    assert "iCloud —" not in text
    assert "Your order — you flagged this" in text


def test_summary_does_not_double_count_a_veto_as_no_history():
    # A vetoed proposal's stage can be "sender" and its reason can read like a
    # normal filing reason — veto must take priority over the stage/reason
    # based categorisation used for ordinary unplaced messages.
    text = summarise([
        proposal(folder=None, rowid=2, stage="sender", reason="sender seen often",
                  veto="you flagged this"),
    ])
    assert "no filing history" not in text
    assert "you flagged this" in text


def test_review_accepts_all_on_a():
    decisions = review([proposal(), proposal(rowid=2)], prompt=lambda _: "a")
    assert all(decision.accepted for decision in decisions)


def test_review_rejects_all_on_q():
    decisions = review([proposal()], prompt=lambda _: "q")
    assert decisions == []


def test_review_per_message_yes_and_no():
    answers = iter(["s", "y", "n", ""])  # step through, accept first, reject second
    decisions = review([proposal(), proposal(rowid=2)], prompt=lambda _: next(answers))
    assert [decision.accepted for decision in decisions] == [True, False]


def test_unplaced_proposals_are_never_offered():
    decisions = review([proposal(folder=None)], prompt=lambda _: "a")
    assert decisions == []


# --- filtering correctness (mutation coverage) ---------------------------------
# The "which messages count as offered/shown" filter is the safety-relevant
# logic in this module (Decision objects downstream drive live moves in the
# next task), so it gets more than the brief's single-item checks.

def test_render_table_excludes_unplaced_rows_from_a_mixed_list():
    table = render_table([
        proposal(rowid=1, subject="Placed one", folder="Orders"),
        proposal(rowid=2, subject="Unplaced one", folder=None, stage="none",
                 reason="no history for this sender or domain"),
    ])
    assert "Placed one" in table
    assert "Unplaced one" not in table


def test_review_skips_unplaced_items_in_a_mixed_list_and_keeps_correspondence():
    # Middle item is unplaced. If the filter used the wrong condition (e.g.
    # inverted, or filtered by index/position instead of by proposal.folder),
    # this would either offer the unplaced item or misalign decisions against
    # the wrong proposal.
    items = [
        proposal(rowid=1, subject="First", folder="Orders"),
        proposal(rowid=2, subject="Second", folder=None, stage="none",
                 reason="no history for this sender or domain"),
        proposal(rowid=3, subject="Third", folder="Receipts"),
    ]
    answers = iter(["s", "y", "n", ""])
    decisions = review(items, prompt=lambda _: next(answers))
    assert [decision.proposal.message.rowid for decision in decisions] == [1, 3]
    assert [decision.accepted for decision in decisions] == [True, False]


def test_review_accept_all_never_includes_unplaced_even_when_mixed():
    items = [
        proposal(rowid=1, folder="Orders"),
        proposal(rowid=2, folder=None, stage="none", reason="no history for this sender or domain"),
    ]
    decisions = review(items, prompt=lambda _: "a")
    assert [decision.proposal.message.rowid for decision in decisions] == [1]


# --- alignment with wide characters (emoji) ------------------------------------
# Subjects routinely contain emoji, which occupy two terminal columns but count
# as one Python character. Padding with len() would silently misalign columns;
# these tests fail if that regresses.

def test_display_width_counts_emoji_as_two_columns():
    assert display_width("a") == 1
    assert display_width("🎉") == 2
    assert display_width("a🎉b") == 4


def test_table_rows_stay_aligned_with_emoji_subjects():
    table = render_table([
        proposal(rowid=1, subject="Plain subject"),
        proposal(rowid=2, subject="🎉🎉 Party invite 🎉🎉"),
        proposal(rowid=3, subject="Another plain one"),
    ])
    lines = table.splitlines()
    widths = {display_width(line) for line in lines}
    # Every rendered line (header and data rows) should occupy the same
    # display width; a len()-based padder would make the emoji row too wide.
    assert len(widths) == 1


# --- summary detail: the majority outcome must be legible ----------------------

def test_summary_distinguishes_no_history_from_inconsistent_history():
    text = summarise([
        proposal(rowid=1, folder=None, stage="none", reason="no history for this sender or domain"),
        proposal(rowid=2, folder=None, stage="sender",
                 reason="sender seen inconsistently — below threshold (0.40 < 0.70)"),
    ])
    lower = text.lower()
    assert "no history" in lower or "no filing history" in lower
    assert "inconsistent" in lower


def test_summary_reports_missing_folder_case_separately():
    text = summarise([
        proposal(rowid=1, folder=None, stage="sender",
                 reason="'Old/Folder' does not exist in this account"),
    ])
    assert "no longer exist" in text.lower() or "does not exist" in text.lower()


def test_summary_all_placed_has_no_unplaced_breakdown_lines():
    text = summarise([proposal(rowid=1), proposal(rowid=2)])
    assert "no history" not in text.lower()
    assert "inconsistent" not in text.lower()


# --- Decision.folder override --------------------------------------------------

def test_decision_folder_prefers_override():
    decision = Decision(proposal(folder="Orders"), accepted=True, override_folder="Receipts")
    assert decision.folder == "Receipts"


def test_decision_folder_falls_back_to_proposal():
    decision = Decision(proposal(folder="Orders"), accepted=True)
    assert decision.folder == "Orders"


# --- Delete as a first-class answer ------------------------------------------
#
# The user, 26 July 2026: "When unsure you can ask about moving versus delete,
# then learn from that." Accept/reject could only ever learn "this folder was
# right" or "this folder was wrong"; the commonest reason for a rejection —
# he did not want the message at all — was invisible.
#
# Deletion goes to the Trash by the same journalled, undoable path as a move.
# It is never a hard delete, and it is never something "accept all" can do.

def placed(folder="Orders"):
    return proposal(folder=folder)


def test_step_mode_offers_delete():
    proposals = [placed("Orders")]
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "s" if len(prompts) == 1 else ("n" if len(prompts) == 2 else "")

    review(proposals, prompt)
    assert "[y/n/d" in prompts[1]


def test_choosing_delete_yields_an_accepted_delete_decision():
    answers = iter(["s", "d", ""])
    decisions = review([placed("Orders")], lambda text: next(answers))
    assert decisions[0].accepted is True
    assert decisions[0].action == "delete"


def test_a_filed_decision_is_not_a_delete():
    answers = iter(["s", "y", ""])
    decisions = review([placed("Orders")], lambda text: next(answers))
    assert decisions[0].action == "file"


def test_accept_all_never_deletes_anything():
    """The batch answer must not be able to bin mail — only 'd', per message."""
    decisions = review([placed("Orders"), placed("Finance")], lambda text: "a")
    assert all(decision.action == "file" for decision in decisions)


def test_rejecting_is_still_distinct_from_deleting():
    answers = iter(["s", "n", ""])
    decisions = review([placed("Orders")], lambda text: next(answers))
    assert decisions[0].accepted is False
    assert decisions[0].action == "file"


# --- Binning mail the classifier could not place ------------------------------
#
# The main review loop only ever offered messages it had a folder for, which
# left the largest group — mail staying in the inbox because the sender's
# history is inconsistent, or unknown — with no answer available at all. That
# is precisely the mail most likely to want binning.
#
# One exclusion is safety-critical: a message held back because it may need a
# reply, or because it was flagged, is never offered. Binning it would defeat
# the guard that held it back, and more finally than filing would.

def unplaced(subject="Left alone", reason="too inconsistent to call",
             stage="sender", veto=None, veto_kind=None, rowid=1):
    message = MessageRow(
        rowid=rowid, sender="orders@shop.example", subject=subject,
        date_sent=1_700_000_000, mailbox_url="imap://A/INBOX", read=False,
    )
    return Proposal(message, None, 0.4, reason, stage, veto=veto, veto_kind=veto_kind)


def test_no_eligible_messages_means_no_question_at_all():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "y"

    assert review_unplaced([placed("Orders")], prompt) == []
    assert prompts == []


def test_declining_the_offer_records_nothing():
    assert review_unplaced([unplaced()], lambda text: "n") == []


def test_deleting_an_unplaced_message_yields_a_delete_decision():
    answers = iter(["y", "d", ""])
    decisions = review_unplaced([unplaced()], lambda text: next(answers))
    assert len(decisions) == 1
    assert decisions[0].accepted is True
    assert decisions[0].action == "delete"
    assert decisions[0].proposal.folder is None


def test_keeping_a_message_records_nothing():
    answers = iter(["y", "k"])
    assert review_unplaced([unplaced()], lambda text: next(answers)) == []


def test_enter_keeps_the_message():
    """The safe answer must be the default, since this loop only bins."""
    answers = iter(["y", ""])
    assert review_unplaced([unplaced()], lambda text: next(answers)) == []


def test_quitting_stops_without_touching_the_rest():
    answers = iter(["y", "q", "d"])
    decisions = review_unplaced(
        [unplaced(subject="First", rowid=1), unplaced(subject="Second", rowid=2)],
        lambda text: next(answers),
    )
    assert decisions == []


def test_a_message_that_may_need_a_reply_is_never_offered():
    proposals = [unplaced(veto="looks personal, may need a reply", veto_kind="attention")]
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "y"

    assert review_unplaced(proposals, prompt) == []
    assert prompts == []


def test_a_flagged_message_is_never_offered():
    proposals = [unplaced(veto="you flagged this", veto_kind="attention")]
    assert review_unplaced(proposals, lambda text: "y") == []


def test_a_sender_you_keep_binning_is_offered():
    """The deletion veto means exactly "you bin this sender" — prime candidate."""
    answers = iter(["y", "d", ""])
    proposals = [unplaced(veto="you have binned the last 7 from this sender",
                          veto_kind="deletion")]
    decisions = review_unplaced(proposals, lambda text: next(answers))
    assert decisions[0].action == "delete"


def test_a_sender_with_no_history_is_offered():
    answers = iter(["y", "d", ""])
    decisions = review_unplaced(
        [unplaced(stage="none", reason="no history for this sender or domain")],
        lambda text: next(answers),
    )
    assert decisions[0].action == "delete"


def test_the_question_explains_why_each_message_stayed():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "y" if len(prompts) == 1 else ("k" if len(prompts) == 2 else "")

    review_unplaced([unplaced(reason="sender known but inconsistent")], prompt)
    assert "sender known but inconsistent" in prompts[1]


def test_the_opening_question_counts_the_messages():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "n"

    review_unplaced([unplaced(rowid=1), unplaced(rowid=2)], prompt)
    assert "2" in prompts[0]


def test_an_invoice_is_never_offered_for_binning():
    """Binning a bill is the harm the invoice requirement exists to prevent."""
    proposals = [unplaced(veto="this looks like a bill", veto_kind="invoice")]
    assert review_unplaced(proposals, lambda text: "y") == []


def test_an_invoice_is_not_counted_in_the_binning_offer():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "n"

    review_unplaced(
        [unplaced(rowid=1), unplaced(rowid=2, veto="a bill", veto_kind="invoice")], prompt
    )
    assert "1 message" in prompts[0]


def test_bills_are_surfaced_above_the_other_vetoes():
    """"An invoice should be surfaced prominently rather than merely held
    back, since the point is that it needs action."""
    text = summarise([
        proposal(rowid=1, folder=None, veto="you flagged this"),
        proposal(rowid=2, folder=None, subject="Your invoice from Northwind.",
                 veto="this looks like a bill — the subject looks like a bill or invoice",
                 veto_kind="invoice"),
    ])
    assert text.index("Your invoice from Northwind.") < text.index("you flagged this")


def test_bills_get_their_own_heading():
    text = summarise([
        proposal(rowid=2, folder=None, subject="Your invoice from Northwind.",
                 veto="this looks like a bill", veto_kind="invoice"),
    ])
    assert "need dealing with" in text.casefold()
    assert "Your invoice from Northwind." in text


def test_a_run_with_no_bills_has_no_bill_heading():
    text = summarise([proposal(rowid=1, folder=None, veto="you flagged this")])
    assert "need dealing with" not in text.casefold()


# --- Proposals that a bin rule marked for deletion ----------------------------

def to_bin(subject="Junk newsletter", rowid=9):
    message = MessageRow(rowid=rowid, sender="junk@shop.example", subject=subject,
                         date_sent=1_700_000_000, mailbox_url="imap://A/INBOX", read=False)
    return Proposal(message, None, 1.0, "your rule: bin mail from junk@shop.example",
                    "rule", action="delete")


def test_the_table_shows_a_bin_proposal_as_a_deletion():
    table = render_table([to_bin()])
    assert "Junk newsletter" in table
    assert "delete" in table.casefold()


def test_accept_all_includes_bin_proposals_as_deletes():
    """Unlike the per-message 'd', this is a rule you already agreed to."""
    decisions = review([to_bin()], lambda text: "a")
    assert [decision.action for decision in decisions] == ["delete"]
    assert decisions[0].accepted is True


def test_stepping_through_offers_a_bin_proposal():
    answers = iter(["s", "y", ""])
    decisions = review([to_bin()], lambda text: next(answers))
    assert decisions[0].accepted is True
    assert decisions[0].action == "delete"


def test_rejecting_a_bin_proposal_bins_nothing():
    answers = iter(["s", "n", ""])
    decisions = review([to_bin()], lambda text: next(answers))
    assert decisions[0].accepted is False


def test_a_bin_proposal_is_not_also_offered_in_the_binning_pass():
    """It is already actionable; offering it twice would be confusing."""
    assert review_unplaced([to_bin()], lambda text: "y") == []


def test_the_summary_counts_a_bin_proposal_as_something_being_done():
    text = summarise([to_bin()])
    assert "0 of 1" not in text


# --- "Whoops, got that wrong" -------------------------------------------------
#
# The user, 27 July 2026, from using it: "I need a 'whoops, got that wrong'
# option on the step through for the answer above."
#
# Safe to offer because nothing has moved yet: the step loop only collects
# decisions, and execution happens after the whole review. Going back is
# therefore just editing a list.

def test_going_back_re_asks_the_previous_message():
    asked = []

    def prompt(text):
        asked.append(text)
        return ["s", "y", "b", "n", "y", ""][len(asked) - 1]

    review([proposal(rowid=1, subject="First"), proposal(rowid=2, subject="Second")], prompt)
    # After stepping back, the third question must be about "First" again.
    assert "First" in asked[3]


def test_going_back_replaces_the_earlier_answer():
    answers = iter(["s", "y", "b", "n", "y", ""])
    decisions = review(
        [proposal(rowid=1, subject="First"), proposal(rowid=2, subject="Second")],
        lambda text: next(answers),
    )
    by_rowid = {d.proposal.message.rowid: d.accepted for d in decisions}
    assert by_rowid == {1: False, 2: True}


def test_going_back_undoes_a_mistaken_delete():
    answers = iter(["s", "d", "b", "y", ""])
    decisions = review([proposal(rowid=1)], lambda text: next(answers))
    assert len(decisions) == 1
    assert decisions[0].action == "file"
    assert decisions[0].accepted is True


def test_going_back_twice_reaches_the_first_message():
    asked = []

    def prompt(text):
        asked.append(text)
        return ["s", "y", "y", "b", "b", "n", "n", ""][len(asked) - 1]

    review(
        [proposal(rowid=1, subject="First"), proposal(rowid=2, subject="Second")],
        prompt,
    )
    assert "First" in asked[5]


def test_going_back_from_the_first_message_is_harmless():
    answers = iter(["s", "b", "y", ""])
    decisions = review([proposal(rowid=1)], lambda text: next(answers))
    assert [d.accepted for d in decisions] == [True]


def test_the_step_prompt_mentions_going_back():
    asked = []

    def prompt(text):
        asked.append(text)
        return "s" if len(asked) == 1 else ("n" if len(asked) == 2 else "")

    review([proposal()], prompt)
    assert "b" in asked[1] and "back" in asked[1].casefold()


def test_going_back_in_the_binning_pass_re_asks():
    asked = []

    def prompt(text):
        asked.append(text)
        return ["y", "d", "b", "k", "k", ""][len(asked) - 1]

    review_unplaced(
        [unplaced(subject="First", rowid=1), unplaced(subject="Second", rowid=2)], prompt
    )
    assert "First" in asked[3]


def test_going_back_in_the_binning_pass_removes_a_mistaken_delete():
    answers = iter(["y", "d", "b", "k", "k", ""])
    decisions = review_unplaced(
        [unplaced(subject="First", rowid=1), unplaced(subject="Second", rowid=2)],
        lambda text: next(answers),
    )
    assert decisions == []


# --- The Account column ---------------------------------------------------------
#
# Shown only when more than one account is being triaged: with a single source
# it is the same value on every row and buys nothing but width.

ACCOUNTS = {"imap://A": "iCloud", "imap://B": "Gmail"}


def test_table_has_no_account_column_for_one_source():
    proposals = [proposal()]
    assert "Account" not in render_table(proposals)
    assert render_table(proposals) == render_table(proposals, {"imap://A": "iCloud"})


def test_table_shows_the_account_when_there_are_two():
    table = render_table([proposal(prefix="imap://B")], ACCOUNTS)
    assert "Account" in table
    assert "Gmail" in table


def test_each_row_names_its_own_account():
    table = render_table(
        [proposal(prefix="imap://A"), proposal(prefix="imap://B")], ACCOUNTS
    )
    body = table.splitlines()[1:]
    assert body[0].startswith("iCloud")
    assert body[1].startswith("Gmail")


def test_an_unconfigured_account_is_marked_rather_than_guessed():
    table = render_table([proposal(prefix="imap://Z")], ACCOUNTS)
    assert "?" in table.splitlines()[1]


def test_account_column_is_padded_by_display_width():
    """Emoji in a sender must not shift the columns that follow it."""
    lines = render_table(
        [
            proposal(prefix="imap://A", sender="📧@example.com"),
            proposal(prefix="imap://B", sender="b@example.com"),
        ],
        ACCOUNTS,
    ).splitlines()
    assert len({display_width(line) for line in lines}) == 1


# --- reviewing mail the attention guard held back --------------------------
#
# The guard is right to hold this mail: it is flagged, or it looks like a
# person expecting a reply. But "held back" was previously the end of the
# story — the summary named the messages and offered nothing to do about
# them, so the same mail was reported run after run with no way to clear it
# short of filing by hand. This loop is the deliberate override: opt-in,
# one message at a time, never a batch answer, with the destination the veto
# overrode shown so there is something concrete to accept.

def held(subject="Assessor availability", veto="looks personal, may need a reply",
         veto_kind="attention", held_folder="Colleagues/Enquiries", rowid=1):
    message = MessageRow(
        rowid=rowid, sender="someone@work.example", subject=subject,
        date_sent=1_700_000_000, mailbox_url="imap://A/INBOX", read=False,
    )
    return Proposal(message, None, 0.91, "sender seen often", "sender",
                    veto=veto, veto_kind=veto_kind, held_folder=held_folder)


def test_no_held_mail_means_no_question_at_all():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "y"

    assert review_held([placed("Orders")], prompt) == []
    assert prompts == []


def test_declining_the_held_offer_records_nothing():
    assert review_held([held()], lambda text: "n") == []


def test_the_offer_is_declined_by_default():
    # Enter must not start the loop: the guard's whole purpose is that this
    # mail is not dealt with casually.
    assert review_held([held()], lambda text: "") == []


def test_filing_held_mail_uses_the_folder_the_veto_overrode():
    replies = iter(["y", "f", ""])
    decisions = review_held([held()], lambda text: next(replies))
    assert len(decisions) == 1
    assert decisions[0].accepted is True
    assert decisions[0].folder == "Colleagues/Enquiries"
    assert decisions[0].is_delete is False


def test_leaving_held_mail_records_no_decision():
    replies = iter(["y", "l", ""])
    assert review_held([held()], lambda text: next(replies)) == []


def test_held_mail_can_be_binned_one_at_a_time():
    replies = iter(["y", "d", ""])
    decisions = review_held([held()], lambda text: next(replies))
    assert len(decisions) == 1
    assert decisions[0].is_delete is True


def test_held_mail_with_no_folder_cannot_be_filed_only_binned():
    # A flagged message the classifier never had a destination for: filing it
    # would have nowhere to go, so "f" must not invent one.
    replies = iter(["y", "f", ""])
    decisions = review_held([held(held_folder=None)], lambda text: next(replies))
    assert decisions == []


def test_the_prompt_names_the_destination_and_the_reason():
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "y" if len(prompts) == 1 else "l"

    review_held([held()], prompt)
    assert "Colleagues/Enquiries" in prompts[1]
    assert "looks personal, may need a reply" in prompts[1]


def test_going_back_discards_the_previous_answer():
    # First is answered "f", then "b" reopens it and "l" leaves it alone;
    # the second is filed. Only the second survives.
    replies = iter(["y", "f", "b", "l", "f", ""])
    decisions = review_held(
        [held(rowid=1), held(rowid=2, subject="Second")], lambda text: next(replies)
    )
    assert [d.proposal.message.rowid for d in decisions] == [2]


def test_quitting_stops_without_touching_the_rest():
    replies = iter(["y", "q"])
    assert review_held(
        [held(rowid=1), held(rowid=2, subject="Second")], lambda text: next(replies)
    ) == []


def test_only_attention_vetoes_are_offered():
    # A bill has its own handling and must not be quietly filed away here;
    # a deletion veto already belongs to the binning loop.
    invoice = held(veto="this looks like a bill", veto_kind="invoice")
    deletion = held(veto="you keep binning this sender", veto_kind="deletion")
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "n"

    review_held([invoice, deletion], prompt)
    assert prompts == []
