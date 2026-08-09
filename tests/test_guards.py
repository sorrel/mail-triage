"""Tests for the hard veto: mail awaiting a reply or action stays in the inbox.

The user, 26 July 2026: "If anything requires me to do something or needs a
reply they mustn't be filed away unless that has happened." Two conditions,
both his: he flagged it, or a human (not bulk mail) wrote it. Unread status
is deliberately not a guard — clearing the unread pile is the point of the
tool — so no test here exercises ``read``.
"""

from __future__ import annotations

from mail_triage.envelope import MessageRow
from mail_triage.guards import Veto, is_bulk, needs_attention


def message(sender="someone@work.example", flagged=False):
    return MessageRow(
        rowid=1, sender=sender, subject="Question about the report",
        date_sent=1_700_000_000, mailbox_url="imap://AAAAAAAA/INBOX",
        read=False, flagged=flagged,
    )


# --- is_bulk --------------------------------------------------------------

def test_no_reply_address_is_bulk_without_needing_headers():
    assert is_bulk("no-reply@shop.example", None) is True


def test_no_reply_style_variants_are_bulk():
    assert is_bulk("noreply@shop.example", None) is True
    assert is_bulk("donotreply@shop.example", None) is True
    assert is_bulk("notifications@service.example", None) is True
    assert is_bulk("bounce@service.example", None) is True


def test_separated_do_not_reply_variants_are_bulk():
    # Real senders use an underscored "do_not_reply@" and add no
    # List-Unsubscribe header, so the unseparated "donotreply@" pattern
    # missed them entirely and bulk notifications were held back as though a
    # person had written them.
    assert is_bulk("do_not_reply@email.apple.example", None) is True
    assert is_bulk("do-not-reply@service.example", None) is True
    assert is_bulk("do.not.reply@service.example", None) is True


def test_opaque_token_local_part_is_bulk_without_needing_headers():
    """A machine-minted local part is nobody's address.

    Live evidence, 9 August 2026: a marketplace's despatch and delivery
    notices arrive from a per-subscriber token address carrying no
    List-Unsubscribe, no Precedence and no Auto-Submitted — no bulk signal of
    any kind — so the guard held them back as though a person had written
    them, and they sat in the inbox indefinitely. Nobody is called
    a1b2c3d4e5f60718.
    """
    assert is_bulk("a1b2c3d4e5f60718@members.shop.example", None) is True
    assert is_bulk("Shop <a1b2c3d4e5f60718@members.shop.example>", None) is True


def test_consonant_run_local_part_is_bulk():
    """The other shape a generated mailbox takes: long, and unpronounceable."""
    assert is_bulk("bqxzhtrmvkwjnp@mailer.example", None) is True


def test_real_names_and_words_are_never_read_as_opaque():
    """The false positive that would matter: filing a person's mail away.

    Everything here is longer than a short address and must stay personal —
    a run-together human name, a hyphenated one, and ordinary role
    addresses, including the short hex-looking words a name can spell.
    """
    for sender in (
        "elizabeth.warrington@work.example",
        "christopher-mccarthy@work.example",
        "elizabethbennet@work.example",
        "accountsreceivable@supplier.example",
        "ada@work.example",
        "deb@work.example",          # entirely hex letters, but short
        "decaf@work.example",        # ditto
        "j.r.hartley@work.example",
    ):
        assert is_bulk(sender, None) is False, sender


def test_list_unsubscribe_header_marks_bulk():
    assert is_bulk("newsletter@shop.example", {"List-Unsubscribe": "<mailto:x@shop.example>"}) is True


def test_precedence_bulk_header_marks_bulk():
    # A sign-in alert or service notification often carries neither a
    # no-reply address nor List-Unsubscribe — there is nothing to
    # unsubscribe from — but does declare itself automated.
    assert is_bulk("gitlab@service.example", {"Precedence": "bulk"}) is True
    assert is_bulk("gitlab@service.example", {"Precedence": "list"}) is True


def test_auto_submitted_header_marks_bulk():
    assert is_bulk("alerts@service.example", {"Auto-Submitted": "auto-generated"}) is True


def test_auto_submitted_none_does_not_mark_bulk():
    # RFC 3834: "auto-submitted: no" is the explicit way of saying a human
    # sent it. Treating the header's mere presence as bulk would invert it.
    assert is_bulk("someone@work.example", {"Auto-Submitted": "no"}) is False


def test_no_reply_display_name_marks_bulk():
    # The address can be ordinary whilst the display name says plainly that
    # nobody reads replies.
    assert is_bulk("Do Not Reply <mailer@service.example>", None) is True


def test_ordinary_address_with_no_headers_is_not_proven_bulk():
    # Absence of a no-reply pattern does not prove a human wrote it — the
    # header check is required before calling anything else bulk.
    assert is_bulk("someone@work.example", None) is False


def test_ordinary_address_without_list_unsubscribe_is_not_bulk():
    assert is_bulk("someone@work.example", {"Subject": "hello"}) is False


# --- never-personal senders -------------------------------------------------

def test_never_personal_sender_is_not_vetoed():
    """A sender you have vouched for is not held for a reply.

    The case this exists for, 9 August 2026: a marketplace sends order
    confirmations from an ordinary-word address with no List-Unsubscribe, no
    Precedence and no Auto-Submitted. Nothing in the message says "bulk", so
    only the user can say it.
    """
    veto = needs_attention(
        message(sender="Shop <orders@shop.example>"),
        headers={"Subject": "Order confirmed"},
        never_personal=frozenset({"orders@shop.example"}),
    )
    assert veto is None


def test_never_personal_matches_the_address_not_the_display_name():
    """The message's display name must not defeat the match.

    The declared set arrives already lower-cased from
    ``never_personal.load_never_personal``; what varies per message is the
    "Name <address>" form the sender chooses to send under.
    """
    veto = needs_attention(
        message(sender="Shop Orders <Orders@Shop.Example>"),
        headers={"Subject": "hi"},
        never_personal=frozenset({"orders@shop.example"}),
    )
    assert veto is None


def test_never_personal_does_not_touch_other_senders():
    veto = needs_attention(
        message(sender="someone@work.example"),
        headers={"Subject": "hi"},
        never_personal=frozenset({"orders@shop.example"}),
    )
    assert isinstance(veto, Veto)


def test_flagged_still_beats_a_never_personal_declaration():
    """Flagging is the one absolute. Declaring a sender bulk must not undo it."""
    veto = needs_attention(
        message(sender="orders@shop.example", flagged=True),
        headers=None,
        never_personal=frozenset({"orders@shop.example"}),
    )
    assert isinstance(veto, Veto)
    assert "flagged" in veto.reason


def test_never_personal_works_without_headers():
    """The declaration is evidence in itself — no header fetch required."""
    veto = needs_attention(
        message(sender="orders@shop.example"),
        headers=None,
        never_personal=frozenset({"orders@shop.example"}),
    )
    assert veto is None


# --- needs_attention --------------------------------------------------------

def test_flagged_message_is_vetoed():
    veto = needs_attention(message(flagged=True), headers={"List-Unsubscribe": "<x>"})
    assert isinstance(veto, Veto)
    assert "flagged" in veto.reason


def test_bulk_message_with_list_unsubscribe_is_not_vetoed():
    veto = needs_attention(
        message(sender="newsletter@shop.example"),
        headers={"List-Unsubscribe": "<mailto:x@shop.example>"},
    )
    assert veto is None


def test_plain_human_sender_is_vetoed():
    veto = needs_attention(message(sender="someone@work.example"), headers={"Subject": "hi"})
    assert isinstance(veto, Veto)
    assert veto.reason  # non-empty, human-readable


def test_no_reply_sender_is_not_vetoed_even_without_headers():
    veto = needs_attention(message(sender="no-reply@shop.example"), headers=None)
    assert veto is None


def test_headers_unavailable_fails_safe_by_vetoing():
    # Mail not running / AppleScript error / timeout: an unavailable signal
    # is not permission to file — must veto, not file.
    veto = needs_attention(message(sender="someone@work.example"), headers=None)
    assert isinstance(veto, Veto)


def test_flagged_overrides_everything_even_a_bulk_sender():
    veto = needs_attention(
        message(sender="no-reply@shop.example", flagged=True),
        headers=None,
    )
    assert isinstance(veto, Veto)
    assert "flagged" in veto.reason
