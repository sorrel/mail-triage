"""Mailbox URL parsing and folder-name normalisation.

Mail stores mailboxes as ``<scheme>://<account-uuid>/<url-encoded/path>``.
Filing history is spread across several accounts — notably a large On My Mac
archive and the live IMAP account — and the same folder name recurs in both.
Keying the model on a normalised folder name pools that evidence.
"""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import unquote, urlparse

_WHITESPACE = re.compile(r"\s+")


def account_prefix(url: str) -> str:
    """Return ``scheme://`` plus the first eight characters of the account UUID."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc[:8]}"


def folder_path(url: str) -> str:
    """Return the decoded, slash-separated folder path with no leading slash."""
    return unquote(urlparse(url).path).lstrip("/")


def normalise_folder(name: str) -> str:
    """Casefold and collapse whitespace so the same folder matches across accounts."""
    return _WHITESPACE.sub(" ", name.strip()).casefold()


# Patterns are fnmatch globs, so square brackets are character classes and a
# literal bracket must be escaped as "[[]". Write "[[]Gmail]*", never
# "[Gmail]*": the latter matches any name beginning with g, m, a, i or l —
# Accounts, Local, Invoices, Music — and would silently drop a large part of
# the filing tree out of training. It appears to work, because it also catches
# "[Gmail]/All Mail" via the "a" of "All Mail". See tests/test_folders.py.
def is_excluded(folder: str, patterns: list[str]) -> bool:
    """True if the folder's leaf name matches any fnmatch pattern, case-insensitively."""
    leaf = folder.rsplit("/", 1)[-1].casefold()
    whole = folder.casefold()
    return any(
        fnmatch.fnmatch(leaf, pattern.casefold()) or fnmatch.fnmatch(whole, pattern.casefold())
        for pattern in patterns
    )
