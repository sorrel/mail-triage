"""Tests for the never-personal list: senders the user has vouched for.

The reply guard asks "did a human write this to me?" and answers it from the
message alone. Some senders it cannot answer for: a marketplace's order
confirmations come from an ordinary-word address with no List-Unsubscribe, no
Precedence and no Auto-Submitted, so there is no evidence of bulk anywhere in
the envelope.

Measured on the live mailbox, 9 August 2026, the tempting general fix was
disproved: ``Feedback-Id`` is carried by those order confirmations *and* by a
heating firm's personal chase about a heat pump enquiry, sent through their
CRM. Any header rule that files the first files the second. So the remaining
evidence is not in the message at all — it is the user's, stated directly.
"""

from __future__ import annotations

import json

import pytest

from mail_triage.never_personal import (
    NeverPersonalError,
    declare_never_personal,
    forget_never_personal,
    load_never_personal,
)


def test_missing_file_means_nobody_is_declared(tmp_path):
    """The state before the first declaration is not an error."""
    assert load_never_personal(tmp_path / "absent.json") == frozenset()


def test_declared_senders_load_lower_cased(tmp_path):
    path = tmp_path / "never-personal.json"
    path.write_text(json.dumps(["Orders@Shop.Example", "alerts@service.example"]))
    assert load_never_personal(path) == frozenset(
        {"orders@shop.example", "alerts@service.example"}
    )


def test_declaring_a_sender_is_written_immediately(tmp_path):
    path = tmp_path / "never-personal.json"
    assert declare_never_personal(path, "Orders@Shop.Example") is True
    assert load_never_personal(path) == frozenset({"orders@shop.example"})


def test_declaring_the_same_sender_twice_reports_no_change(tmp_path):
    path = tmp_path / "never-personal.json"
    declare_never_personal(path, "orders@shop.example")
    assert declare_never_personal(path, "ORDERS@shop.example") is False
    assert load_never_personal(path) == frozenset({"orders@shop.example"})


def test_a_display_name_is_reduced_to_its_address(tmp_path):
    """Declared from what the proposal table showed, which includes the name."""
    path = tmp_path / "never-personal.json"
    declare_never_personal(path, "Shop <orders@shop.example>")
    assert load_never_personal(path) == frozenset({"orders@shop.example"})


def test_something_that_is_not_an_address_is_refused(tmp_path):
    """A typo must not silently become a declaration that matches nothing."""
    path = tmp_path / "never-personal.json"
    with pytest.raises(NeverPersonalError):
        declare_never_personal(path, "orders-at-shop")


def test_forgetting_reports_whether_there_was_anything_to_forget(tmp_path):
    path = tmp_path / "never-personal.json"
    declare_never_personal(path, "orders@shop.example")
    assert forget_never_personal(path, "Orders@Shop.Example") is True
    assert forget_never_personal(path, "orders@shop.example") is False
    assert load_never_personal(path) == frozenset()


def test_unparseable_file_is_an_error_naming_itself(tmp_path):
    """Loud, not silent.

    Failing to load leans safe here — an empty set holds *more* mail back, the
    opposite of the rules file, where silence would file mail contrary to
    instructions. It is still an error, because a declaration the user
    believes is in force and is not would explain nothing about the inbox.
    """
    path = tmp_path / "never-personal.json"
    path.write_text("{not json")
    with pytest.raises(NeverPersonalError) as error:
        load_never_personal(path)
    assert str(path) in str(error.value)


def test_a_file_that_is_not_a_list_is_an_error(tmp_path):
    path = tmp_path / "never-personal.json"
    path.write_text(json.dumps({"orders@shop.example": True}))
    with pytest.raises(NeverPersonalError):
        load_never_personal(path)
