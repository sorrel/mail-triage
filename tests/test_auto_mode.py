"""Filing without being asked: which proposals qualify, and which never do.

The safety of this mode is entirely in what it declines to touch. Everything
a guard held back, everything a rule would bin, and everything the classifier
is merely fairly sure about stays exactly where it is.
"""

from mail_triage.config import Config
from mail_triage.envelope import MessageRow
from mail_triage.model.classify import Proposal
from mail_triage.review import auto_decisions


def proposal(confidence, folder="Orders", rowid=1, **overrides):
    message = MessageRow(rowid=rowid, sender="a@b.example", subject="s", date_sent=1,
                         mailbox_url="imap://A/INBOX", read=False)
    return Proposal(message, folder, confidence, "reason", "sender", **overrides)


def config(tmp_path, auto_threshold=0.9):
    return Config(account_url_prefix="imap://A", local_dir=tmp_path, auto_threshold=auto_threshold)


def test_auto_accepts_only_above_threshold(tmp_path):
    decisions = auto_decisions([proposal(0.95), proposal(0.5, rowid=2)], config(tmp_path))
    assert [decision.proposal.message.rowid for decision in decisions] == [1]


def test_auto_ignores_unplaced_proposals(tmp_path):
    assert auto_decisions([proposal(0.99, folder=None)], config(tmp_path)) == []


def test_threshold_is_configurable(tmp_path):
    decisions = auto_decisions([proposal(0.6)], config(tmp_path, auto_threshold=0.5))
    assert len(decisions) == 1


def test_a_message_a_guard_held_back_is_never_auto_filed(tmp_path):
    """The whole point of a veto is that nobody acts on it unprompted.

    A vetoed proposal carries its destination in ``held_folder`` and leaves
    ``folder`` as None precisely so that no path can file it by accident.
    Confidence is untouched by a veto, so a 0.99 held message would sail
    through a check that read confidence alone.
    """
    held = proposal(0.99, folder=None, veto="may need a reply",
                    veto_kind="attention", held_folder="Orders")
    assert auto_decisions([held], config(tmp_path)) == []


def test_auto_never_bins_anything(tmp_path):
    """A bin rule is an instruction, but an unattended run is the wrong place
    to carry it out: the one irreversible-feeling answer stays a decision
    somebody makes whilst watching."""
    binned = proposal(0.99, folder=None, action="delete")
    assert auto_decisions([binned], config(tmp_path)) == []


def test_accepted_decisions_file_rather_than_delete(tmp_path):
    decision = auto_decisions([proposal(0.95)], config(tmp_path))[0]
    assert decision.accepted is True
    assert decision.is_delete is False
    assert decision.folder == "Orders"
