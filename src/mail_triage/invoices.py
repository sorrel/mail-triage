"""Detecting invoices and bills.

The user, 26 July 2026: *"You don't unsubscribe from invoices. If a message has
an included invoice we want to deal with that first."*

Two rules follow from that, and this module supplies the signal for both:

1. **An invoice must be dealt with before it can be filed.** It joins the
   do-not-file guards, and outranks even a hard rule — a "file everything from
   this sender" instruction must not sweep away a bill.
2. **A sender who sends invoices is never an unsubscribe candidate**, however
   often their marketing gets binned. See ``sends_invoices``.

**Which way the errors run.** A false positive costs one message staying in
the inbox — mildly annoying, and visible. A false negative files or bins a
bill unseen, which is the harm the requirement names. So these rules lean
towards catching things.

**Calibrated against the real mailbox, 27 July 2026.** Over 3,878 messages
from the last year, the subject rules fire on 7.3%, and a hand-check of the
first thirty found no false positives. Two terms were tested and rejected:

- ``bill`` fired six times in two years and half were wrong — a parliamentary
  Bill ("Terminally Ill Adults (End of Life) Bill") and a newsletter headline
  ("Will elec VAT cut really reduce your bill?"). Genuine bills say invoice or
  statement, so nothing is lost by dropping it.
- bare ``payment`` matched 1,174 subjects, most of them "payment method
  updated" and similar. It is only accepted next to a due/overdue word, a
  currency amount, or "direct debit".

``statement`` is kept despite one known false positive in two years
("Financial Statements: Building Blocks for Level-Headed Investing"), because
credit-card and energy statements are exactly the correspondence rule 2 must
protect.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Word-boundary matching throughout: "statement" must not fire on a substring,
# and the terms deliberately exclude "bill" (see the module docstring).
_SUBJECT = re.compile(
    r"""
      \b(invoices?|receipts?|statements?|remittance)\b
    | \b(payments?|amounts?|balances?)\s+(is\s+|are\s+)?(due|overdue)\b
    | \boverdue\b
    | \bdirect\s+debit\b
    | payment.{0,20}[£$€]\s?\d          # "we'll take a £60.00 payment"
    | [£$€]\s?[\d,.]+.{0,20}payment
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Attachments match on substring, not word boundary: real filenames run words
# together ("COPYINVOICE_HURW2...", "emailreceipt_2011..."), and a filename is
# far stronger evidence than a subject line, so the looser match is warranted.
_ATTACHMENT = re.compile(r"invoice|receipt|statement|remittance", re.IGNORECASE)


def invoice_reason(subject: str | None, attachment_names: Iterable[str | None] = ()) -> str | None:
    """Why this message looks like a bill, or ``None`` if it doesn't.

    The attachment is checked first because it is both stronger evidence and
    the commonest real shape: a neutral subject ("Confirmation of your order:
    A22670576936") with the invoice attached.
    """
    for name in attachment_names or ():
        if name and _ATTACHMENT.search(name):
            return f"has an attachment called '{name}'"
    if subject and _SUBJECT.search(subject):
        return "the subject looks like a bill or invoice"
    return None


def sends_invoices(subjects: Iterable[str | None]) -> bool:
    """Whether a sender's recent mail includes anything billing-shaped.

    Rule 2 above: a sender this returns ``True`` for must never be offered as
    an unsubscribe candidate, and must never acquire a "bin these from now on"
    rule, however much of their other mail gets binned. Unsubscribing from
    billing correspondence is the one case where that feature must not fire.
    """
    return any(invoice_reason(subject) is not None for subject in subjects)
