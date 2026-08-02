"""Corrections: where the user actually wanted a message to go.

History is evidence, a correction is instruction — so a correction enters
training at ``correction_weight`` times the weight of a historical filing.
"""

from mail_triage.config import Config
from mail_triage.corrections import (
    Correction, corrections_as_examples, load_corrections, record_correction,
)


def make_config(tmp_path, **overrides):
    values = dict(account_url_prefix="imap://A", local_dir=tmp_path)
    values.update(overrides)
    return Config(**values)


def correction(chosen="Finance", rejected="Orders"):
    return Correction(sender="bills@shop.example", domain="shop.example", subject="Invoice",
                      chosen_folder=chosen, rejected_folder=rejected, recorded_at=1_700_000_000)


def test_corrections_round_trip(tmp_path):
    config = make_config(tmp_path)
    record_correction(correction(), config)
    loaded = load_corrections(config)
    assert loaded[0].chosen_folder == "Finance"
    assert loaded[0].rejected_folder == "Orders"


def test_corrections_append_rather_than_overwrite(tmp_path):
    config = make_config(tmp_path)
    record_correction(correction(), config)
    record_correction(correction(chosen="Admin"), config)
    assert len(load_corrections(config)) == 2


def test_corrections_become_heavily_weighted_examples(tmp_path):
    config = make_config(tmp_path, correction_weight=10.0)
    examples = corrections_as_examples([correction()], config)
    assert examples[0].folder == "finance"
    assert examples[0].weight == 10.0


def test_loading_with_no_file_returns_empty(tmp_path):
    assert load_corrections(make_config(tmp_path)) == []


def test_a_correction_naming_no_folder_trains_nothing(tmp_path):
    """A rejection says "not there" and names nowhere to put it instead.

    Turned into an example it would teach the empty folder, which is worse
    than not learning at all — so it is dropped here rather than guarded for
    at every point that trains.
    """
    config = make_config(tmp_path)
    assert corrections_as_examples([correction(chosen="")], config) == []


def test_a_damaged_line_does_not_lose_the_rest(tmp_path):
    """One bad line should cost one correction, not every correction."""
    config = make_config(tmp_path)
    record_correction(correction(), config)
    with config.corrections_path.open("a") as handle:
        handle.write("{not json\n")
    record_correction(correction(chosen="Admin"), config)
    assert [item.chosen_folder for item in load_corrections(config)] == ["Finance", "Admin"]


# --- Turning review decisions into corrections -----------------------------

def decision(override="Ledger/Finance", proposed="Ledger/Orders", sender="Bills <BILLS@Shop.example>"):
    from mail_triage.envelope import MessageRow
    from mail_triage.model.classify import Proposal
    from mail_triage.review import Decision

    message = MessageRow(rowid=1, sender=sender, subject="Invoice 42",
                         date_sent=1_700_000_000, mailbox_url="imap://A/INBOX", read=False)
    proposal = Proposal(message, proposed, 0.95, "sender seen often", "sender")
    return Decision(proposal, accepted=True, override_folder=override)


def test_an_override_is_recorded_as_a_correction(tmp_path):
    from mail_triage.corrections import record_overrides

    config = make_config(tmp_path)
    assert record_overrides([decision()], config) == 1
    recorded = load_corrections(config)[0]
    assert recorded.chosen_folder == "Ledger/Finance"
    assert recorded.rejected_folder == "Ledger/Orders"
    # Stored in the same shape the corpus uses, or the correction would key
    # against a different sender than the history it is meant to outweigh.
    assert recorded.sender == "bills@shop.example"
    assert recorded.domain == "shop.example"


def test_accepting_a_proposal_records_nothing(tmp_path):
    """Agreement is not a correction: the model was already right."""
    from mail_triage.corrections import record_overrides

    config = make_config(tmp_path)
    assert record_overrides([decision(override=None)], config) == 0
    assert load_corrections(config) == []


def test_overriding_a_proposal_that_named_nowhere_is_still_a_correction(tmp_path):
    from mail_triage.corrections import record_overrides

    config = make_config(tmp_path)
    record_overrides([decision(proposed=None)], config)
    assert load_corrections(config)[0].rejected_folder is None
