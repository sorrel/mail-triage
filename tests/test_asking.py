"""Asking about senders the model knows but cannot call.

The first live dry run left 30 of 55 inbox messages in this state — the
largest group by some margin — from 24 distinct senders. Ranking is therefore
the load-bearing part: five questions a run only pay off if they land on the
senders who write most often, not on whoever happens to have three messages
in today's inbox.
"""

import pytest

from mail_triage.asking import UncertainSender, ask, ask_all, rank_uncertain
from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.envelope import MessageRow
from mail_triage.folders import match_folders
from mail_triage.model.classify import Classifier, Proposal
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import TrainedModel
from mail_triage.rules import load_rules

FOLDERS = ["Parent/Keep", "Parent/Reading", "Orders"]


def make_model(examples):
    sender_model = SenderModel()
    sender_model.train(examples)
    return TrainedModel(sender=sender_model, trained_at=0, example_count=len(examples))


def example(sender, folder, weight=1.0):
    return TrainingExample(sender=sender, domain=sender.split("@")[1], subject="s",
                           folder=folder, weight=weight, year=2026)


def message(sender, subject="Subject", rowid=1):
    return MessageRow(rowid=rowid, sender=sender, subject=subject, date_sent=1_700_000_000,
                      mailbox_url="imap://AAAAAAAA/INBOX", read=False, flagged=False)


def config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def split_sender(name, folder_a="parent/keep", folder_b="parent/reading"):
    """A sender whose mail is split, so confidence falls below the threshold."""
    return ([example(name, folder_a) for _ in range(11)]
            + [example(name, folder_b) for _ in range(9)])


def classify_all(model, tmp_path, senders, folders=FOLDERS, **kwargs):
    classifier = Classifier(model, config(tmp_path), available_folders=folders, **kwargs)
    return [classifier.classify(message(sender)) for sender in senders]


# --- Which senders qualify --------------------------------------------------

def test_an_inconsistent_sender_is_offered_for_asking(tmp_path):
    model = make_model(split_sender("mixed@shop.example"))
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"])
    uncertain = rank_uncertain(proposals, model, FOLDERS, {"mixed@shop.example": 27})
    assert [item.sender for item in uncertain] == ["mixed@shop.example"]
    assert uncertain[0].kind == "inconsistent"


def test_an_orphaned_prediction_is_offered_for_asking(tmp_path):
    model = make_model([example("gone@shop.example", "deleted folder") for _ in range(10)])
    proposals = classify_all(model, tmp_path, ["gone@shop.example"])
    uncertain = rank_uncertain(proposals, model, FOLDERS, {"gone@shop.example": 5})
    assert uncertain[0].kind == "orphaned"


def test_a_sender_with_no_history_is_never_asked_about(tmp_path):
    """No candidate folders to offer — the question degrades into typing a path."""
    model = make_model([example("orders@shop.example", "orders")])
    proposals = classify_all(model, tmp_path, ["stranger@nowhere.example"])
    assert rank_uncertain(proposals, model, FOLDERS, {}) == []


def test_a_confidently_filed_sender_is_not_asked_about(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    proposals = classify_all(model, tmp_path, ["orders@shop.example"])
    assert rank_uncertain(proposals, model, FOLDERS, {"orders@shop.example": 40}) == []


def test_a_vetoed_sender_is_not_asked_about(tmp_path):
    """The veto already settled it; a filing rule would not change the outcome."""
    model = make_model(split_sender("mixed@shop.example"))
    proposals = classify_all(
        model, tmp_path, ["mixed@shop.example"], guard=lambda msg: {"Subject": "hi"}
    )
    proposals = [Proposal(item.message, None, item.confidence, item.reason,
                          item.stage, veto="looks personal") for item in proposals]
    assert rank_uncertain(proposals, model, FOLDERS, {"mixed@shop.example": 27}) == []


def test_a_rule_pointing_at_a_deleted_folder_is_re_asked(tmp_path):
    from mail_triage.rules import Rule

    model = make_model(split_sender("mixed@shop.example"))
    rules = {"mixed@shop.example": Rule(sender="mixed@shop.example", action="file",
                                        folder="Home/Gone", answered_at=1, candidates={})}
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"], rules=rules)
    uncertain = rank_uncertain(proposals, model, FOLDERS, {"mixed@shop.example": 27})
    assert [item.sender for item in uncertain] == ["mixed@shop.example"]


def test_a_sender_already_answered_is_not_asked_again(tmp_path):
    from mail_triage.rules import Rule

    model = make_model(split_sender("mixed@shop.example"))
    rules = {"mixed@shop.example": Rule(sender="mixed@shop.example", action="leave",
                                        folder=None, answered_at=1, candidates={})}
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"], rules=rules)
    assert rank_uncertain(proposals, model, FOLDERS, {"mixed@shop.example": 27}) == []


# --- Ranking ----------------------------------------------------------------

def test_ranking_is_by_messages_sent_in_the_last_year(tmp_path):
    """Not by inbox count: a sender who wrote 3 times ever must not outrank
    one who writes 27 times a year."""
    model = make_model(split_sender("quiet@shop.example") + split_sender("busy@shop.example"))
    proposals = classify_all(
        model, tmp_path,
        ["quiet@shop.example", "quiet@shop.example", "quiet@shop.example", "busy@shop.example"],
    )
    uncertain = rank_uncertain(
        proposals, model, FOLDERS, {"quiet@shop.example": 3, "busy@shop.example": 27}
    )
    assert [item.sender for item in uncertain] == ["busy@shop.example", "quiet@shop.example"]


def test_inbox_count_breaks_a_tie_on_yearly_rate(tmp_path):
    model = make_model(split_sender("one@shop.example") + split_sender("two@shop.example"))
    proposals = classify_all(
        model, tmp_path, ["one@shop.example", "two@shop.example", "two@shop.example"]
    )
    uncertain = rank_uncertain(
        proposals, model, FOLDERS, {"one@shop.example": 10, "two@shop.example": 10}
    )
    assert [item.sender for item in uncertain] == ["two@shop.example", "one@shop.example"]


def test_at_most_five_senders_are_asked_about_per_run(tmp_path):
    senders = [f"sender{index}@shop.example" for index in range(9)]
    examples = [item for sender in senders for item in split_sender(sender)]
    proposals = classify_all(make_model(examples), tmp_path, senders)
    uncertain = rank_uncertain(
        proposals, make_model(examples), FOLDERS,
        {sender: 100 - index for index, sender in enumerate(senders)},
    )
    assert len(uncertain) == 5


def test_a_sender_counts_once_however_many_messages_are_in_the_inbox(tmp_path):
    model = make_model(split_sender("mixed@shop.example"))
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"] * 3)
    uncertain = rank_uncertain(proposals, model, FOLDERS, {"mixed@shop.example": 27})
    assert len(uncertain) == 1
    assert uncertain[0].inbox_count == 3


def test_a_sender_absent_from_the_yearly_counts_ranks_last_but_still_appears(tmp_path):
    model = make_model(split_sender("quiet@shop.example") + split_sender("busy@shop.example"))
    proposals = classify_all(model, tmp_path, ["quiet@shop.example", "busy@shop.example"])
    uncertain = rank_uncertain(proposals, model, FOLDERS, {"busy@shop.example": 5})
    assert [item.sender for item in uncertain] == ["busy@shop.example", "quiet@shop.example"]
    assert uncertain[1].yearly_count == 0


def test_candidates_carry_real_folder_names_and_weights(tmp_path):
    model = make_model(split_sender("mixed@shop.example"))
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"])
    candidates = rank_uncertain(proposals, model, FOLDERS, {})[0].candidates
    assert set(candidates) == {"Parent/Keep", "Parent/Reading"}
    assert candidates["Parent/Keep"] > candidates["Parent/Reading"]


def test_candidates_exclude_folders_that_no_longer_exist(tmp_path):
    model = make_model(
        [example("mixed@shop.example", "parent/keep") for _ in range(11)]
        + [example("mixed@shop.example", "deleted folder") for _ in range(9)]
    )
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"])
    candidates = rank_uncertain(proposals, model, FOLDERS, {})[0].candidates
    assert set(candidates) == {"Parent/Keep"}


# --- The question ------------------------------------------------------------

def uncertain_sender(sender="mixed@shop.example"):
    return UncertainSender(
        sender=sender,
        candidates={"Parent/Keep": 12.0, "Parent/Reading": 8.0},
        yearly_count=27,
        inbox_count=3,
        kind="inconsistent",
    )


def scripted(answers):
    """A prompt that replays answers and records what it was asked."""
    asked = []

    def prompt(text):
        asked.append(text)
        return answers.pop(0)

    return prompt, asked


def folder_lookup(name, folders=FOLDERS):
    """Resolve a typed folder to the real mailboxes it could mean."""
    return match_folders(name, folders)


def test_choosing_a_candidate_by_number_yields_a_file_rule():
    prompt, _ = scripted(["1"])
    rule = ask(uncertain_sender(), prompt, folder_lookup, now=1_785_000_000)
    assert rule.action == "file"
    assert rule.folder == "Parent/Keep"
    assert rule.sender == "mixed@shop.example"
    assert rule.answered_at == 1_785_000_000


def test_the_second_candidate_is_reachable_by_number():
    prompt, _ = scripted(["2"])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0).folder == "Parent/Reading"


def test_candidates_are_offered_most_used_first():
    prompt, asked = scripted([""])
    ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert asked[0].index("Parent/Keep") < asked[0].index("Parent/Reading")


def test_the_question_shows_how_often_the_sender_writes():
    prompt, asked = scripted([""])
    ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert "27" in asked[0]


def test_typing_a_folder_outside_the_candidates_yields_a_rule_for_it():
    prompt, _ = scripted(["Orders"])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0).folder == "Orders"


def test_a_typed_folder_is_stored_with_the_accounts_capitalisation():
    prompt, _ = scripted(["parent/keep"])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0).folder == "Parent/Keep"


def test_a_leaf_name_is_enough_to_name_a_nested_folder():
    """Typing the whole path is a chore; "Keep" means "Parent/Keep"."""
    prompt, _ = scripted(["keep"])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0).folder == "Parent/Keep"


def test_a_leaf_name_shared_by_two_folders_is_put_back_to_the_user():
    """Guessing between them would file mail somewhere the user did not name."""
    folders = ["Personal/Health", "Work/Health", "Orders"]
    lookup = lambda name: folder_lookup(name, folders)  # noqa: E731
    prompt, asked = scripted(["Health", "Work/Health"])
    rule = ask(uncertain_sender(), prompt, lookup, now=0)
    assert rule.folder == "Work/Health"
    assert "Personal/Health" in asked[1] and "Work/Health" in asked[1]


def test_a_typo_is_re_prompted_rather_than_stored():
    """A typo must not create a rule pointing nowhere."""
    prompt, asked = scripted(["Hoem/Kep", "Parent/Keep"])
    rule = ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert rule.folder == "Parent/Keep"
    assert len(asked) == 2
    assert "Hoem/Kep" in asked[1]


def test_leaving_a_sender_alone_is_a_recorded_answer():
    prompt, _ = scripted(["l"])
    rule = ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert rule.action == "leave"
    assert rule.folder is None


def test_skipping_records_nothing():
    prompt, _ = scripted([""])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0) is None


def test_skip_is_distinct_from_leaving_alone():
    skip_prompt, _ = scripted([""])
    leave_prompt, _ = scripted(["l"])
    assert ask(uncertain_sender(), skip_prompt, folder_lookup, now=0) is None
    assert ask(uncertain_sender(), leave_prompt, folder_lookup, now=0) is not None


def test_the_answer_records_what_was_on_offer():
    prompt, _ = scripted(["1"])
    rule = ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert rule.candidates == {"Parent/Keep": 12.0, "Parent/Reading": 8.0}


# --- The bin answer, unblocked 27 July 2026 by invoice detection --------------

def test_deleting_a_senders_mail_is_an_offered_answer():
    prompt, asked = scripted([""])
    ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert "[d]elete" in asked[0]


def test_the_delete_answer_is_never_bound_to_b():
    """"b" means *back* in the review loops; it must not delete here."""
    prompt, _ = scripted(["b", ""])
    assert ask(uncertain_sender(), prompt, folder_lookup, now=0) is None


def test_choosing_to_delete_yields_a_bin_rule():
    prompt, _ = scripted(["d"])
    rule = ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert rule.action == "bin"
    assert rule.folder is None


def test_a_billing_sender_is_never_offered_the_bin_answer():
    """"A sender flagged as sending invoices must never acquire a bin rule."
    The harm is binning a bill because the marketing was unwanted."""
    prompt, asked = scripted([""])
    billing = UncertainSender(sender="accounts@shop.example",
                              candidates={"Parent/Keep": 12.0}, yearly_count=27,
                              inbox_count=3, kind="inconsistent", sends_invoices=True)
    ask(billing, prompt, folder_lookup, now=0)
    assert "[d]elete" not in asked[0]


def test_a_billing_sender_typing_d_does_not_get_a_bin_rule():
    """Withholding the option from the prompt is not enough on its own."""
    prompt, _ = scripted(["d", ""])
    billing = UncertainSender(sender="accounts@shop.example",
                              candidates={"Parent/Keep": 12.0}, yearly_count=27,
                              inbox_count=3, kind="inconsistent", sends_invoices=True)
    assert ask(billing, prompt, folder_lookup, now=0) is None


def test_a_billing_sender_can_still_be_filed():
    prompt, _ = scripted(["1"])
    billing = UncertainSender(sender="accounts@shop.example",
                              candidates={"Parent/Keep": 12.0}, yearly_count=27,
                              inbox_count=3, kind="inconsistent", sends_invoices=True)
    assert ask(billing, prompt, folder_lookup, now=0).folder == "Parent/Keep"


def test_ranking_marks_senders_who_send_invoices(tmp_path):
    model = make_model(split_sender("accounts@shop.example"))
    proposals = classify_all(model, tmp_path, ["accounts@shop.example"])
    uncertain = rank_uncertain(proposals, model, FOLDERS, {},
                               billing_senders={"accounts@shop.example"})
    assert uncertain[0].sends_invoices is True


def test_ranking_leaves_ordinary_senders_unmarked(tmp_path):
    model = make_model(split_sender("mixed@shop.example"))
    proposals = classify_all(model, tmp_path, ["mixed@shop.example"])
    assert rank_uncertain(proposals, model, FOLDERS, {})[0].sends_invoices is False


def test_billing_senders_are_found_from_recent_subjects(tmp_path):
    from mail_triage.asking import build_billing_senders

    now = 1_800_000_000
    rows = [
        MessageRow(rowid=1, sender="accounts@shop.example", subject="Your invoice from Northwind.",
                   date_sent=now - 86_400, mailbox_url="imap://AAAAAAAA/Orders",
                   read=True, flagged=False),
        MessageRow(rowid=2, sender="news@shop.example", subject="Weekly round-up",
                   date_sent=now - 86_400, mailbox_url="imap://AAAAAAAA/Orders",
                   read=True, flagged=False),
    ]

    class FakeReader:
        def all_messages(self):
            return iter(rows)

    found = build_billing_senders(FakeReader(), config(tmp_path), now=now)
    assert found == {"accounts@shop.example"}


def test_an_orphaned_sender_with_no_surviving_candidates_can_still_be_answered():
    prompt, _ = scripted(["Orders"])
    orphan = UncertainSender(sender="gone@shop.example", candidates={}, yearly_count=5,
                             inbox_count=1, kind="orphaned")
    assert ask(orphan, prompt, folder_lookup, now=0).folder == "Orders"


# --- Asking a whole run's worth ---------------------------------------------

def test_every_answer_is_written_as_it_is_given(tmp_path):
    path = tmp_path / "rules.json"
    prompt, _ = scripted(["1", "l"])
    ask_all([uncertain_sender("first@shop.example"), uncertain_sender("second@shop.example")],
            prompt, folder_lookup, path, now=0)
    assert set(load_rules(path)) == {"first@shop.example", "second@shop.example"}


def test_interrupting_the_questions_keeps_the_answers_already_given(tmp_path):
    path = tmp_path / "rules.json"

    def prompt(text):
        if "first@shop.example" in text:
            return "1"
        raise KeyboardInterrupt

    answered = ask_all(
        [uncertain_sender("first@shop.example"), uncertain_sender("second@shop.example")],
        prompt, folder_lookup, path, now=0,
    )
    assert [rule.sender for rule in answered] == ["first@shop.example"]
    assert set(load_rules(path)) == {"first@shop.example"}


def test_skipped_senders_leave_no_rule_behind(tmp_path):
    path = tmp_path / "rules.json"
    prompt, _ = scripted(["", "1"])
    ask_all([uncertain_sender("skipped@shop.example"), uncertain_sender("answered@shop.example")],
            prompt, folder_lookup, path, now=0)
    assert set(load_rules(path)) == {"answered@shop.example"}


# --- Yearly counts -----------------------------------------------------------

def test_yearly_counts_ignore_mail_older_than_a_year(tmp_path):
    from mail_triage.asking import build_yearly_counts

    now = 1_800_000_000
    day = 86_400
    rows = [
        message("recent@shop.example", rowid=1),
        message("recent@shop.example", rowid=2),
        message("old@shop.example", rowid=3),
    ]
    rows = [
        MessageRow(rowid=row.rowid, sender=row.sender, subject=row.subject,
                   date_sent=now - (30 if row.sender.startswith("recent") else 400) * day,
                   mailbox_url="imap://AAAAAAAA/Orders", read=True, flagged=False)
        for row in rows
    ]

    class FakeReader:
        def all_messages(self):
            return iter(rows)

    counts = build_yearly_counts(FakeReader(), config(tmp_path), now=now)
    assert counts["recent@shop.example"] == 2
    assert "old@shop.example" not in counts


def test_yearly_counts_ignore_other_accounts(tmp_path):
    from mail_triage.asking import build_yearly_counts

    now = 1_800_000_000
    rows = [
        MessageRow(rowid=1, sender="a@shop.example", subject="s", date_sent=now - 86_400,
                   mailbox_url="imap://AAAAAAAA/Orders", read=True, flagged=False),
        MessageRow(rowid=2, sender="b@shop.example", subject="s", date_sent=now - 86_400,
                   mailbox_url="local://BBBBBBBB/Orders", read=True, flagged=False),
    ]

    class FakeReader:
        def all_messages(self):
            return iter(rows)

    counts = build_yearly_counts(FakeReader(), config(tmp_path), now=now)
    assert set(counts) == {"a@shop.example"}


def test_a_faded_candidate_is_not_shown_as_zero_sightings():
    """Weights are recency-decayed, so a real past filing can fall below 1.
    Rounding it to "(0)" would read as "never used here", which is false."""
    prompt, asked = scripted([""])
    faded = UncertainSender(sender="mixed@shop.example", candidates={"Parent/Keep": 0.4},
                            yearly_count=5, inbox_count=1, kind="inconsistent")
    ask(faded, prompt, folder_lookup, now=0)
    assert "(0)" not in asked[0]
    assert "0.4" in asked[0]


def test_a_substantial_candidate_is_shown_as_a_whole_number():
    prompt, asked = scripted([""])
    ask(uncertain_sender(), prompt, folder_lookup, now=0)
    assert "(12)" in asked[0]


def test_a_barely_present_candidate_reads_as_less_than_one_tenth():
    """One decimal still rounds 0.04 to "0.0", which reads as zero."""
    prompt, asked = scripted([""])
    faint = UncertainSender(sender="mixed@shop.example", candidates={"Parent/Keep": 0.04},
                            yearly_count=5, inbox_count=1, kind="inconsistent")
    ask(faint, prompt, folder_lookup, now=0)
    assert "0.0)" not in asked[0]
    assert "<0.1" in asked[0]
