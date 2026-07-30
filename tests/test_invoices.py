"""Detecting invoices and bills.

The user, 26 July 2026: "You don't unsubscribe from invoices. If a message has
an included invoice we want to deal with that first."

The asymmetry that shapes these rules: a false positive costs a message
staying in the inbox, which is mildly annoying. A false negative files or bins
a bill unseen, which is the harm the requirement exists to prevent. So the
rules lean towards catching things — but not so far that ordinary mail becomes
permanently unfilable.
"""

import pytest

from mail_triage.invoices import invoice_reason


# --- Subjects ---------------------------------------------------------------

@pytest.mark.parametrize(
    "subject",
    [
        "Your invoice from Northwind.",
        "[Hosting Co] Your 2025-07 invoice is available",
        "Invoice #67791 from Contoso Brewery",
        "Your App Store Order Receipt from 2 Aug 2024",
        "Your receipt for today's grocery delivery",
        "Your Contoso credit card statement",
        "Your July statement summary is ready",
        "Upcoming Direct Debit payment to a broadband provider",
        "Your payment is overdue",
        "Amount due on your account",
        "Remittance advice",
        "FYI: We'll take a £60.00 payment on 3rd August 2026",
    ],
)
def test_billing_subjects_are_detected(subject):
    assert invoice_reason(subject) is not None


@pytest.mark.parametrize(
    "subject",
    [
        # A parliamentary Bill, not a bill. Measured against real mail: "bill"
        # fired six times in two years and half were wrong, so it is not a
        # subject term at all.
        "Terminally Ill Adults (End of Life) Bill",
        "Will elec VAT cut really reduce your bill?",
        "Missing export from August bill",
        # Ordinary mail that mentions money without being billing correspondence.
        "Your payment method has been updated",
        "Weekly news round-up",
        "Zeus's Law",
        "Nvidia's $250B OpenAI deal, Meta Sells Out",
    ],
)
def test_ordinary_subjects_are_not_detected(subject):
    assert invoice_reason(subject) is None


def test_the_reason_names_the_subject_as_the_evidence():
    reason = invoice_reason("Your invoice from Northwind.")
    assert "subject" in reason.casefold()


def test_detection_is_case_insensitive():
    assert invoice_reason("YOUR INVOICE FROM APPLE") is not None


def test_a_missing_subject_is_not_an_invoice():
    assert invoice_reason("") is None
    assert invoice_reason(None) is None


# --- Attachments ------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "Invoice-424102.pdf",
        "Invoice.PDF",
        "invoice.html",
        "COPYINVOICE_HURW2_121115-20120515-103958.pdf",
        "Retailer_SalesReceipt.pdf",
        "emailreceipt_20110818R1136381652.pdf",
        "UK_Receipt.pdf",
    ],
)
def test_billing_attachments_are_detected(name):
    assert invoice_reason("Order confirmation", [name]) is not None


def test_an_attachment_beats_an_innocent_subject():
    """The commonest real shape: a neutral subject with the invoice attached."""
    reason = invoice_reason("Confirmation of your order: A22670576936", ["Invoice.PDF"])
    assert reason is not None
    assert "Invoice.PDF" in reason


def test_ordinary_attachments_are_not_detected():
    assert invoice_reason("Holiday photos", ["beach.jpg", "notes.txt"]) is None


def test_attachment_names_run_together_are_still_matched():
    """Filenames often lack separators, so attachments match on substring."""
    assert invoice_reason("Order", ["MYINVOICE2024.pdf"]) is not None


def test_a_message_with_no_attachments_falls_back_to_the_subject():
    assert invoice_reason("Your invoice from Northwind.", []) is not None
    assert invoice_reason("Holiday photos", []) is None


def test_an_empty_attachment_name_is_ignored():
    assert invoice_reason("Holiday photos", ["", None]) is None
