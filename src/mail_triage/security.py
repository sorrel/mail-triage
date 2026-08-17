"""Security-relevant mail is never filed by a run nobody watched.

Auto mode is already safe against doing the *wrong* thing: it files only, it
never bins, it never touches mail a guard held back, and every move is
journalled and undoable. What it cannot protect against is the right thing
done to mail that wanted eyes on it — filed correctly, quietly, at 07:00, by
a process nobody was watching.

For this user that mail is security mail, and the existing guards do not hold
it. Measured against ``guards.needs_attention`` on 17 August 2026, with a
``List-Unsubscribe`` header supplied so each message had every chance of
being held back:

    notifications@code-host.example    Dependabot alert: critical vuln   files
    no-reply@accounts.search.example   Security alert: new sign-in       files
    no-reply@signin.cloud.example      Your root account was accessed    files
    noreply@breach-check.example       You have been pwned in a breach   files

Not one was held, and the reason is structural rather than an oversight:
``_NO_REPLY_WORDS`` contains "notifications" and "noreply", so ``is_bulk``
settles every one of them from the address alone and ``needs_attention``
returns None before a header is read. That is the correct answer to the
question that guard asks — nobody is awaiting a reply to a Dependabot alert
— but it is not the only question worth asking.

These are also the senders auto mode is *most* confident about: high volume,
consistent, and a long filing history for stage A to learn from. A breach
notification reads 0.97 and files itself.

**The asymmetry here runs opposite to the reply guard's, and that is what
licenses a broader rule.** For the reply guard a false negative loses a
message that wanted an answer, so it is written narrowly and fails safe by
holding back. Here *both* directions fail into the inbox: a false positive
costs one message filed by hand next run, whilst a false negative files a
breach notice away silently. The costs are not close, so this guard is
deliberately generous where the others are careful.

The limit on that generosity is usefulness rather than safety — a guard that
holds back half the inbox makes auto mode pointless — which is why the
vocabulary below is measured against the real corpus before it is trusted
(``mail-triage security measure``) rather than argued about.

What this does *not* do: it does not flag, move, bin or reply. It withholds
mail from unattended filing, and it is offered in an interactive run exactly
as any other held mail is, because then there is somebody there to read it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mail_triage.corpus import normalise_sender, sender_domain


class SecuritySendersError(Exception):
    """The declared-senders file exists but cannot be trusted."""


# Matched against the subject, case-insensitively, on whole words. Word
# boundaries matter more than they look: "breach" as a substring also matches
# "breaches" (wanted) but an unanchored "mfa" would match inside any number of
# ordinary words, and "2fa" inside a product code.
#
# Each entry earns its place by naming something that, if filed away unseen,
# is a problem hours later rather than a mild annoyance. Marketing copy about
# security products is the expected false positive, and it is an acceptable
# one: the message stays in the inbox and is filed by hand next run.
_SUBJECT_PATTERNS = (
    r"security alert",
    r"security notice",
    r"suspicious (sign|log|activity|attempt)",
    r"unusual (sign|log|activity|attempt)",
    r"new sign[- ]?in",
    r"signed in",
    r"new login",
    r"unauthoris?z?ed access",
    r"password (was |has been )?(reset|changed)",
    r"reset your password",
    r"verification code",
    r"two[- ]factor",
    r"\b2fa\b",
    r"\bmfa\b",
    r"one[- ]time (code|password|passcode)",
    r"recovery (code|codes|email|phone)",
    r"new device",
    r"data breach",
    r"\bbreach(ed|es)?\b",
    r"\bpwned\b",
    r"\bCVE-\d{4}-\d+",
    r"vulnerabilit(y|ies)",
    r"security advisory",
    r"\bexploit(ed|able)?\b",
    r"api (key|token) (created|generated|revoked)",
    r"(access|personal access) token",
    r"certificate (expir|renew)",
    r"\bphishing\b",
    r"\bmalware\b",
    r"\bransomware\b",
)

# Compiled once. re.IGNORECASE rather than case-folding the subject, so the
# CVE pattern's explicit case stays readable.
_SUBJECT = re.compile("|".join(_SUBJECT_PATTERNS), re.IGNORECASE)


def load_security_senders(path: Path) -> frozenset[str]:
    """Load the declared senders, lower-cased. Addresses and bare domains.

    A missing file means nobody has been declared, which is the state before
    the first declaration and not an error.

    An unreadable one *is* an error, and unlike ``never_personal`` it does not
    lean safe in both directions: an empty set here means security mail is
    filed unattended, which is precisely the outcome this module exists to
    prevent. So it raises, loudly, rather than starting a scheduled run with a
    guard the user believes is in force and is not.
    """
    if not path.exists():
        return frozenset()
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise SecuritySendersError(
            f"{path}: cannot parse the security-sender list at line {error.lineno}, "
            f"column {error.colno} ({error.msg}). Fix or delete the file — until it "
            "parses, mail from these senders can be filed by an unattended run."
        ) from error
    if not isinstance(payload, list):
        raise SecuritySendersError(f"{path}: expected a list of addresses or domains")
    return frozenset(str(entry).strip().casefold() for entry in payload if str(entry).strip())


def _declared(sender: str, declared: frozenset[str]) -> bool:
    """Whether this sender was declared, by address or by domain.

    Domains as well as addresses because the senders worth declaring mint a
    fresh local part per message — a token address matches no list anybody
    could maintain, whilst its domain is stable.
    """
    if not declared:
        return False
    address = normalise_sender(sender)
    if address and address in declared:
        return True
    domain = sender_domain(sender)
    if not domain:
        return False
    if domain in declared:
        return True
    # A declared parent domain covers its subdomains: declaring "cloud.example"
    # should cover "signin.cloud.example", which is where such mail comes from.
    # Matched on a label boundary, never as a substring — "cloud.example" must
    # not be satisfied by "cloud.example.attacker.example".
    return any(domain.endswith("." + entry) for entry in declared)


def security_reason(sender: str, subject: str, declared: frozenset[str]) -> str | None:
    """Why this message must not be filed unattended, or ``None``.

    The reason is written for the person reading the proposal table, and names
    which of the two layers fired so a surprising hold can be traced to either
    a declaration the user made or a word in the subject.
    """
    if _declared(sender, declared):
        return "you declared this sender security-relevant"
    match = _SUBJECT.search(subject or "")
    if match is not None:
        return f"looks security-relevant ({match.group(0).strip().lower()})"
    return None


def _require_entry(value: str) -> str:
    """An address or a domain, or refuse.

    A blank or malformed entry would be a declaration matching nothing,
    silently — and silence is the failure mode here, so a typo is refused
    where it is made rather than discovered as a breach notice that was filed.
    """
    entry = (value or "").strip().casefold()
    if not entry:
        raise SecuritySendersError("a security sender cannot be blank")
    address = normalise_sender(entry)
    if address:
        return address
    # Not an address, so it must at least look like a domain: something with a
    # dot and no spaces or "@". Anything else is a typo.
    if "@" in entry or " " in entry or "." not in entry:
        raise SecuritySendersError(
            f"{value!r} is neither an email address nor a domain"
        )
    return entry


def _save(path: Path, entries: frozenset[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(entries), indent=2) + "\n")


def declare_security_sender(path: Path, value: str) -> bool:
    """Declare an address or domain security-relevant. Returns whether it changed."""
    entry = _require_entry(value)
    known = load_security_senders(path)
    if entry in known:
        return False
    _save(path, known | {entry})
    return True


def forget_security_sender(path: Path, value: str) -> bool:
    """Undeclare an address or domain. Returns whether there was one to remove."""
    entry = _require_entry(value)
    known = load_security_senders(path)
    if entry not in known:
        return False
    _save(path, known - {entry})
    return True
