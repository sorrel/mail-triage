from mail_triage.config import Config
from mail_triage.corpus import TrainingExample
from mail_triage.deletion import DeletionStats, PerAccountDeletionIndex
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Classifier
from mail_triage.model.sender import SenderModel
from mail_triage.model.store import TrainedModel


def make_model(examples):
    sender_model = SenderModel()
    sender_model.train(examples)
    return TrainedModel(sender=sender_model, trained_at=0, example_count=len(examples))


def example(sender, folder, weight=1.0):
    return TrainingExample(sender=sender, domain=sender.split("@")[1], subject="s",
                           folder=folder, weight=weight, year=2026)


def message(sender="orders@shop.example", subject="Your order", flagged=False,
            prefix="imap://AAAAAAAA"):
    return MessageRow(rowid=1, sender=sender, subject=subject, date_sent=1_700_000_000,
                      mailbox_url=f"{prefix}/INBOX", read=False, flagged=flagged)


def config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://AAAAAAAA", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def test_confident_sender_produces_a_proposal(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.stage == "sender"


def test_low_confidence_produces_no_folder(tmp_path):
    model = make_model(
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders", "Finance"])
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder is None
    assert "below threshold" in proposal.reason


def test_folder_absent_from_account_is_rejected(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Finance"])
    proposal = classifier.classify(message())
    assert proposal.folder is None
    assert "does not exist" in proposal.reason


def test_unknown_sender_produces_no_folder(tmp_path):
    model = make_model([example("orders@shop.example", "orders")])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message(sender="stranger@nowhere.example"))
    assert proposal.folder is None
    assert proposal.stage == "none"


def test_proposal_preserves_original_folder_capitalisation(tmp_path):
    model = make_model([example("orders@shop.example", "home tech") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Home Tech"])
    assert classifier.classify(message()).folder == "Home Tech"


# --- Nested-folder cases -----------------------------------------------
#
# 41 of the 44 folders the trained model actually learns from are nested
# (``Parent/Child``, ``Parent/Child/Grandchild``). available_folders must be
# sourced from folder_path() over the envelope database (full nested paths),
# never from Mail's flat AppleScript folder list — otherwise every nested
# prediction would be rejected as "does not exist in this account".

def test_nested_folder_prediction_maps_back_to_real_capitalisation(tmp_path):
    model = make_model([example("orders@shop.example", "parent/child") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Parent/Child"])
    proposal = classifier.classify(message())
    assert proposal.folder == "Parent/Child"
    assert proposal.stage == "sender"


def test_deeply_nested_folder_prediction_maps_back(tmp_path):
    model = make_model(
        [example("orders@shop.example", "parent/child/grandchild") for _ in range(10)]
    )
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Parent/Child/Grandchild"]
    )
    proposal = classifier.classify(message())
    assert proposal.folder == "Parent/Child/Grandchild"


def test_nested_folder_absent_from_account_is_rejected(tmp_path):
    # Prediction is for a nested folder that isn't present in this account's
    # folder list, even though a similarly-named leaf folder is.
    model = make_model([example("orders@shop.example", "parent/child") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Other Parent/Child"]
    )
    proposal = classifier.classify(message())
    assert proposal.folder is None
    assert "does not exist" in proposal.reason


def test_nested_folder_whitespace_and_case_normalise_across_accounts(tmp_path):
    # The model stores the normalised key (as build_corpus() would produce via
    # normalise_folder()); the real account may have different whitespace or
    # capitalisation that still normalises to the same key.
    model = make_model([example("orders@shop.example", "parent / child") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Parent  /  Child"])
    proposal = classifier.classify(message())
    assert proposal.folder == "Parent  /  Child"


# --- Threshold boundary --------------------------------------------------
#
# Mutation guard: the comparison must be strict (`<`), not `<=`. A confidence
# exactly equal to the configured threshold should still be accepted — the
# threshold marks the lowest confidence that counts as confident, not the
# highest that is still rejected. With 7 sightings of "orders" and 2 of
# "finance" (weight 1 each), share = 7 / (9 + PRIOR_STRENGTH) = 7/10 = 0.70,
# exactly Config's default confidence_threshold.

def test_confidence_exactly_at_threshold_is_accepted(tmp_path):
    examples = (
        [example("boundary@shop.example", "orders") for _ in range(7)]
        + [example("boundary@shop.example", "finance") for _ in range(2)]
    )
    model = make_model(examples)
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Finance"]
    )
    proposal = classifier.classify(message(sender="boundary@shop.example"))
    assert proposal.confidence == 0.7
    assert proposal.folder == "Orders"


def test_confidence_just_below_threshold_is_rejected(tmp_path):
    # One fewer "orders" sighting (6 vs 3) drops share to 6/10 = 0.60 < 0.7.
    examples = (
        [example("belowline@shop.example", "orders") for _ in range(6)]
        + [example("belowline@shop.example", "finance") for _ in range(3)]
    )
    model = make_model(examples)
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Finance"]
    )
    proposal = classifier.classify(message(sender="belowline@shop.example"))
    assert proposal.folder is None
    assert "below threshold" in proposal.reason


# --- Task 11B: do-not-file veto ------------------------------------------
#
# A veto overrides confidence entirely and is checked only once a message
# would otherwise be filed. The flagged check needs no header fetch (it is
# read straight off MessageRow), so it applies even with no guard wired at
# all — costing nothing, exactly as the user specified. The bulk/human check
# does need a header fetch capability, so it only runs when a guard hook is
# supplied; without one, pre-existing filing behaviour is untouched.

def test_flagged_message_is_vetoed_even_at_high_confidence(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message(flagged=True))
    assert proposal.folder is None
    assert proposal.veto is not None
    assert "flagged" in proposal.veto
    # Confidence is preserved, not zeroed — the veto overrides filing, not the score.
    assert proposal.confidence > 0.9


def test_flagged_veto_needs_no_guard_hook(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"], guard=None)
    proposal = classifier.classify(message(flagged=True))
    assert proposal.folder is None
    assert proposal.veto is not None


def test_no_guard_hook_leaves_ordinary_filing_unaffected(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.veto is None


def test_no_reply_sender_is_filed_without_fetching_headers(tmp_path):
    calls = []

    def guard(msg):
        calls.append(msg)
        return {}

    model = make_model([example("no-reply@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], guard=guard
    )
    proposal = classifier.classify(message(sender="no-reply@shop.example"))
    assert proposal.folder == "Orders"
    assert proposal.veto is None
    assert calls == []  # no header fetch needed — the sender pattern already proved bulk


def test_plain_sender_is_vetoed_when_headers_show_no_list_unsubscribe(tmp_path):
    model = make_model([example("someone@work.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        guard=lambda msg: {"Subject": "hi"},
    )
    proposal = classifier.classify(message(sender="someone@work.example"))
    assert proposal.folder is None
    assert proposal.veto is not None
    assert "personal" in proposal.veto


def test_plain_sender_is_filed_when_list_unsubscribe_present(tmp_path):
    model = make_model([example("someone@work.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        guard=lambda msg: {"List-Unsubscribe": "<mailto:x@work.example>"},
    )
    proposal = classifier.classify(message(sender="someone@work.example"))
    assert proposal.folder == "Orders"
    assert proposal.veto is None


def test_guard_failure_fails_safe_by_vetoing_not_filing(tmp_path):
    def broken_guard(msg):
        raise RuntimeError("Mail is not running")

    model = make_model([example("someone@work.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], guard=broken_guard
    )
    proposal = classifier.classify(message(sender="someone@work.example"))
    assert proposal.folder is None
    assert proposal.veto is not None


def test_unplaced_message_never_triggers_a_header_fetch(tmp_path):
    # A message with no filing history stays in the inbox regardless — there
    # is nothing to veto, so no header fetch should be attempted at all.
    calls = []

    def guard(msg):
        calls.append(msg)
        return {}

    model = make_model([example("orders@shop.example", "orders")])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], guard=guard
    )
    proposal = classifier.classify(message(sender="stranger@nowhere.example"))
    assert proposal.folder is None
    assert calls == []


# --- Task 11C: deletion veto ------------------------------------------------
#
# A sender the classifier would otherwise file confidently, but who the
# deletion index says is only being binned lately, must be vetoed — and
# without needing a guard hook at all, since it costs no header fetch, same
# as the flagged check.

def test_only_deletes_sender_is_vetoed_even_at_high_confidence(tmp_path):
    model = make_model([example("news@bulletin.example", "orders") for _ in range(10)])
    deletion_index = {"news@bulletin.example": DeletionStats(filed=0, deleted=9)}
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], deletion_index=deletion_index
    )
    proposal = classifier.classify(message(sender="news@bulletin.example"))
    assert proposal.folder is None
    assert proposal.veto is not None
    assert "9" in proposal.veto
    # Confidence is preserved, not zeroed — same contract as the other vetoes.
    assert proposal.confidence > 0.9


def test_mixed_sender_is_still_proposed_despite_some_deletions(tmp_path):
    model = make_model([example("dan@bulletin.example", "orders") for _ in range(10)])
    deletion_index = {"dan@bulletin.example": DeletionStats(filed=5, deleted=21)}
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], deletion_index=deletion_index
    )
    proposal = classifier.classify(message(sender="dan@bulletin.example"))
    assert proposal.folder == "Orders"
    assert proposal.veto is None


def test_sender_absent_from_deletion_index_is_unaffected(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], deletion_index={}
    )
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.veto is None


def test_no_deletion_index_leaves_ordinary_filing_unaffected(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], deletion_index=None
    )
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.veto is None


# --- Rules: precedence ------------------------------------------------------
#
# A rule is an instruction about a *sender*. The do-not-file guards are
# judgements about an *individual message*. Messages win. Ordering, highest
# first: per-message guards, then rules, then the deletion veto, then the
# statistical stages. These tests pin every boundary in that list, because
# getting the order wrong files mail contrary to an explicit instruction.

def rule(sender="mixed@shop.example", action="file", folder="Parent/Keep"):
    from mail_triage.rules import Rule

    return Rule(sender=sender, action=action, folder=folder,
                answered_at=1_785_000_000, candidates={})


def test_a_rule_files_a_sender_the_statistics_could_not_call(tmp_path):
    model = make_model(
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Finance", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder == "Parent/Keep"
    assert proposal.stage == "rule"
    assert proposal.confidence == 1.0


def test_a_rule_overrides_what_the_statistics_would_have_chosen(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
    )
    assert classifier.classify(message(sender="mixed@shop.example")).folder == "Parent/Keep"


def test_a_rule_beats_the_deletion_veto(tmp_path):
    # The veto is inference from Trash contents; the rule is the user speaking
    # directly and recently. The direct answer wins.
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
        deletion_index={"mixed@shop.example": DeletionStats(filed=0, deleted=9)},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder == "Parent/Keep"
    assert proposal.veto is None


def test_a_flagged_message_beats_its_senders_rule(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example", flagged=True))
    assert proposal.folder is None
    assert proposal.veto is not None


def test_a_message_needing_a_reply_beats_its_senders_rule(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
        guard=lambda msg: {"Subject": "hi"},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder is None
    assert "personal" in proposal.veto


def test_leave_alone_rule_keeps_the_message_in_the_inbox(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"mixed@shop.example": rule(action="leave", folder=None)},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder is None
    assert proposal.stage == "rule"
    assert "leave" in proposal.reason


def test_a_rule_naming_a_deleted_folder_is_reported_not_silently_dropped(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"mixed@shop.example": rule()},
    )
    proposal = classifier.classify(message(sender="mixed@shop.example"))
    assert proposal.folder is None
    assert proposal.stage == "rule"
    assert "no longer exists" in proposal.reason


def test_a_rule_matches_regardless_of_sender_capitalisation(tmp_path):
    model = make_model([example("mixed@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
    )
    proposal = classifier.classify(message(sender="Shop <Mixed@Shop.Example>"))
    assert proposal.folder == "Parent/Keep"


def test_senders_without_a_rule_are_classified_as_before(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"mixed@shop.example": rule()},
    )
    proposal = classifier.classify(message())
    assert proposal.folder == "Orders"
    assert proposal.stage == "sender"


# --- Veto kinds -------------------------------------------------------------
#
# The reason string is written for a person to read. Deciding *policy* on it
# by substring match would be fragile, and one policy now depends on it: mail
# held back because it may need a reply must never be offered for binning,
# whereas mail held back because you keep binning that sender is exactly what
# should be offered. Hence a machine-readable kind alongside the prose.

def test_a_flagged_message_records_an_attention_veto(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    assert classifier.classify(message(flagged=True)).veto_kind == "attention"


def test_a_message_needing_a_reply_records_an_attention_veto(tmp_path):
    model = make_model([example("someone@work.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        guard=lambda msg: {"Subject": "hi"},
    )
    assert classifier.classify(message(sender="someone@work.example")).veto_kind == "attention"


def test_a_veto_remembers_the_folder_it_overrode(tmp_path):
    # ``folder`` stays None so nothing downstream can file a vetoed message
    # by accident, but the destination the veto overrode is what the held-mail
    # review has to offer — without it there is nothing to say yes to.
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(message(flagged=True))
    assert proposal.folder is None
    assert proposal.held_folder == "Orders"


def test_a_message_with_no_folder_to_begin_with_holds_no_folder(tmp_path):
    model = make_model([])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    assert classifier.classify(message(flagged=True)).held_folder is None


def test_an_only_deletes_sender_records_a_deletion_veto(tmp_path):
    model = make_model([example("news@bulletin.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        deletion_index={"news@bulletin.example": DeletionStats(filed=0, deleted=9)},
    )
    proposal = classifier.classify(message(sender="news@bulletin.example"))
    assert proposal.veto_kind == "deletion"


def test_an_unvetoed_proposal_has_no_veto_kind(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    assert classifier.classify(message()).veto_kind is None


# --- Invoices ---------------------------------------------------------------
#
# "You don't unsubscribe from invoices. If a message has an included invoice
# we want to deal with that first." An invoice outranks everything, including
# a hard rule: a "file everything from this sender" instruction must not sweep
# away a bill. It is also marked when the message could not be placed at all,
# because an unplaced invoice would otherwise be offered for binning — which
# is the harm the requirement exists to prevent.

def test_an_invoice_is_never_filed_however_confident(tmp_path):
    model = make_model([example("accounts@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(
        message(sender="accounts@shop.example", subject="Your invoice from Northwind.")
    )
    assert proposal.folder is None
    assert proposal.veto_kind == "invoice"


def test_an_invoice_beats_a_hard_rule(tmp_path):
    model = make_model([example("accounts@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders", "Parent/Keep"],
        rules={"accounts@shop.example": rule(sender="accounts@shop.example")},
    )
    proposal = classifier.classify(
        message(sender="accounts@shop.example", subject="Your invoice from Northwind.")
    )
    assert proposal.folder is None
    assert proposal.veto_kind == "invoice"


def test_an_invoice_is_marked_even_when_it_could_not_be_placed(tmp_path):
    """Otherwise it would be offered for binning, which is the real harm."""
    model = make_model([example("orders@shop.example", "orders")])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(
        message(sender="stranger@nowhere.example", subject="Invoice #67791")
    )
    assert proposal.folder is None
    assert proposal.veto_kind == "invoice"


def test_an_invoice_is_detected_from_an_attachment(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        attachments={1: ["Invoice-424102.pdf"]},
    )
    proposal = classifier.classify(message(subject="Confirmation of your order"))
    assert proposal.folder is None
    assert proposal.veto_kind == "invoice"
    assert "Invoice-424102.pdf" in proposal.veto


def test_the_invoice_veto_explains_itself_in_plain_english(tmp_path):
    model = make_model([example("accounts@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(
        message(sender="accounts@shop.example", subject="Your invoice from Northwind.")
    )
    assert "bill" in proposal.veto.casefold() or "invoice" in proposal.veto.casefold()


def test_ordinary_mail_is_unaffected_by_the_invoice_guard(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        attachments={1: ["beach.jpg"]},
    )
    proposal = classifier.classify(message(subject="Holiday photos"))
    assert proposal.folder == "Orders"
    assert proposal.veto_kind is None


def test_no_attachment_data_still_detects_invoices_by_subject(tmp_path):
    model = make_model([example("accounts@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"], attachments=None
    )
    proposal = classifier.classify(
        message(sender="accounts@shop.example", subject="Your invoice from Northwind.")
    )
    assert proposal.veto_kind == "invoice"


# --- Bin rules --------------------------------------------------------------
#
# "Bin these from now on." A move to the Trash, journalled and undoable like
# any other — never a hard delete. It sits at the same precedence as any other
# rule: below the per-message guards, above the deletion veto and the stages.
#
# The safety-critical case is a billing sender under a bin rule. A bill from
# them must still be held in the inbox — the alternative is the tool binning
# an invoice because the marketing from the same address was unwanted.

def bin_rule(sender="junk@shop.example"):
    from mail_triage.rules import Rule

    return Rule(sender=sender, action="bin", folder=None,
                answered_at=1_785_000_000, candidates={})


def test_a_bin_rule_marks_the_message_for_deletion(tmp_path):
    model = make_model([example("junk@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"junk@shop.example": bin_rule()},
    )
    proposal = classifier.classify(message(sender="junk@shop.example"))
    assert proposal.action == "delete"
    assert proposal.stage == "rule"
    assert proposal.folder is None


def test_a_bin_rule_beats_what_the_statistics_would_have_filed(tmp_path):
    model = make_model([example("junk@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"junk@shop.example": bin_rule()},
    )
    assert classifier.classify(message(sender="junk@shop.example")).action == "delete"


def test_an_invoice_beats_a_bin_rule(tmp_path):
    """The whole reason bin rules were deferred until invoice detection."""
    model = make_model([example("junk@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"junk@shop.example": bin_rule()},
    )
    proposal = classifier.classify(
        message(sender="junk@shop.example", subject="Your invoice from Northwind.")
    )
    assert proposal.action == "file"
    assert proposal.folder is None
    assert proposal.veto_kind == "invoice"


def test_a_flagged_message_beats_a_bin_rule(tmp_path):
    model = make_model([example("junk@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"junk@shop.example": bin_rule()},
    )
    proposal = classifier.classify(message(sender="junk@shop.example", flagged=True))
    assert proposal.action == "file"
    assert proposal.veto is not None


def test_a_message_needing_a_reply_beats_a_bin_rule(tmp_path):
    model = make_model([example("junk@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Orders"],
        rules={"junk@shop.example": bin_rule()}, guard=lambda msg: {"Subject": "hi"},
    )
    proposal = classifier.classify(message(sender="junk@shop.example"))
    assert proposal.action == "file"
    assert "personal" in proposal.veto


def test_an_ordinary_proposal_has_no_delete_action(tmp_path):
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    assert classifier.classify(message()).action == "file"


# --- Stage B: subject tokens --------------------------------------------------
#
# Stage A files by who sent a message. Stage B is tried when that produces no
# folder — an unknown sender, a split one, or a prediction naming a folder that
# no longer exists — because the subject line is often what actually separates
# the destinations.

def make_model_with_tokens(sender_examples, token_examples):
    from mail_triage.model.tokens import TokenModel

    sender_model = SenderModel()
    sender_model.train(sender_examples)
    token_model = TokenModel()
    token_model.train(token_examples)
    return TrainedModel(sender=sender_model, trained_at=0,
                        example_count=len(sender_examples), tokens=token_model)


def token_example(subject, folder, sender="a@example.com", weight=1.0):
    return TrainingExample(sender=sender, domain="example.com", subject=subject,
                           folder=folder, weight=weight, year=2026)


def test_an_unknown_sender_is_placed_by_its_subject(tmp_path):
    model = make_model_with_tokens(
        [example("orders@shop.example", "orders")],
        [token_example("gardening tips for autumn", "garden") for _ in range(30)],
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Garden", "Orders"])
    proposal = classifier.classify(
        message(sender="stranger@nowhere.example", subject="gardening tips")
    )
    assert proposal.folder == "Garden"
    assert proposal.stage == "tokens"


def test_a_split_sender_is_separated_by_its_subject(tmp_path):
    """The motivating case: stage A cannot call it, stage B can."""
    split = (
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    model = make_model_with_tokens(
        split,
        [token_example("gardening tips for autumn", "garden", sender="mixed@shop.example")
         for _ in range(40)]
        + [token_example("cycling club ride sunday", "sport", sender="mixed@shop.example")
           for _ in range(40)],
    )
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Garden", "Sport", "Orders", "Finance"]
    )
    garden = classifier.classify(
        message(sender="mixed@shop.example", subject="gardening tips for autumn")
    )
    sport = classifier.classify(
        message(sender="mixed@shop.example", subject="cycling club ride sunday")
    )
    assert (garden.folder, garden.stage) == ("Garden", "tokens")
    assert (sport.folder, sport.stage) == ("Sport", "tokens")


def test_a_confident_sender_still_wins_over_the_subject(tmp_path):
    model = make_model_with_tokens(
        [example("orders@shop.example", "orders") for _ in range(10)],
        [token_example("gardening tips for autumn", "garden") for _ in range(30)],
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Finance", "Orders"])
    proposal = classifier.classify(message(subject="gardening tips for autumn"))
    assert proposal.folder == "Orders"
    assert proposal.stage == "sender"


def test_an_unconfident_token_prediction_places_nothing(tmp_path):
    model = make_model_with_tokens(
        [example("orders@shop.example", "orders")],
        [token_example("shared word here", "finance") for _ in range(20)]
        + [token_example("shared word here", "orders") for _ in range(20)],
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Finance", "Orders"])
    proposal = classifier.classify(
        message(sender="stranger@nowhere.example", subject="shared word here")
    )
    assert proposal.folder is None


def test_a_token_prediction_for_a_missing_folder_is_rejected(tmp_path):
    model = make_model_with_tokens(
        [example("orders@shop.example", "orders")],
        [token_example("gardening tips for autumn", "garden") for _ in range(30)],
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    proposal = classifier.classify(
        message(sender="stranger@nowhere.example", subject="gardening tips")
    )
    assert proposal.folder is None


def test_a_failed_stage_b_keeps_stage_as_reason_for_asking(tmp_path):
    """rank_uncertain keys off stage 'sender' and the below-threshold reason,
    so a stage B miss must not overwrite them."""
    split = (
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    model = make_model_with_tokens(
        split,
        [token_example("shared word here", "finance") for _ in range(20)]
        + [token_example("shared word here", "orders") for _ in range(20)],
    )
    classifier = Classifier(model, config(tmp_path), available_folders=["Finance", "Orders"])
    proposal = classifier.classify(
        message(sender="mixed@shop.example", subject="shared word here")
    )
    assert proposal.folder is None
    assert proposal.stage == "sender"
    assert "below threshold" in proposal.reason


def test_a_bin_rule_still_beats_stage_b(tmp_path):
    model = make_model_with_tokens(
        [], [token_example("gardening tips for autumn", "garden") for _ in range(30)]
    )
    classifier = Classifier(
        model, config(tmp_path), available_folders=["Finance"],
        rules={"junk@shop.example": bin_rule()},
    )
    proposal = classifier.classify(
        message(sender="junk@shop.example", subject="Your invoice is ready")
    )
    # Invoice guard outranks even this, so use a neutral subject to test the rule.
    assert proposal.veto_kind == "invoice"


def test_a_model_without_a_token_stage_still_classifies(tmp_path):
    """Backwards compatible: stage B simply does not fire."""
    model = make_model([example("orders@shop.example", "orders") for _ in range(10)])
    classifier = Classifier(model, config(tmp_path), available_folders=["Orders"])
    assert classifier.classify(message()).folder == "Orders"


# --- Deletion evidence is looked up per account ---------------------------------
#
# Evidence is built per source, but the classifier asks for one sender's stats
# without knowing which account the message came from. PerAccountDeletionIndex
# closes that gap; a plain dict must keep working exactly as before.

def _vetoing_model():
    """Ten filings, enough for a confident proposal the veto can then override."""
    return make_model([example("news@bulletin.example", "orders") for _ in range(10)])


def test_a_plain_deletion_index_still_vetoes(tmp_path):
    classifier = Classifier(
        _vetoing_model(), config(tmp_path), available_folders=["Orders"],
        deletion_index={"news@bulletin.example": DeletionStats(filed=0, deleted=9)},
    )
    assert classifier.classify(message(sender="news@bulletin.example")).veto is not None


def test_a_per_account_index_vetoes_using_the_messages_own_account(tmp_path):
    index = PerAccountDeletionIndex({
        "imap://BBBBBBBB": {"news@bulletin.example": DeletionStats(filed=0, deleted=9)},
    })
    classifier = Classifier(
        _vetoing_model(), config(tmp_path), available_folders=["Orders"],
        deletion_index=index,
    )
    from_gmail = classifier.classify(
        message(sender="news@bulletin.example", prefix="imap://BBBBBBBB")
    )
    assert from_gmail.veto is not None


def test_one_accounts_deletions_do_not_veto_another_accounts_mail(tmp_path):
    """The whole point of counting per account: no leakage between them."""
    index = PerAccountDeletionIndex({
        "imap://BBBBBBBB": {"news@bulletin.example": DeletionStats(filed=0, deleted=9)},
    })
    classifier = Classifier(
        _vetoing_model(), config(tmp_path), available_folders=["Orders"],
        deletion_index=index,
    )
    from_icloud = classifier.classify(
        message(sender="news@bulletin.example", prefix="imap://AAAAAAAA")
    )
    assert from_icloud.veto is None
    assert from_icloud.folder == "Orders"
