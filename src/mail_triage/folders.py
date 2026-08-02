"""Mailbox URL parsing and folder-name normalisation.

Mail stores mailboxes as ``<scheme>://<account-uuid>/<url-encoded/path>``.
Filing history is spread across several accounts — notably a large On My Mac
archive and the live IMAP account — and the same folder name recurs in both.
Keying the model on a normalised folder name pools that evidence.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
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


def match_folders(typed: str, folders: Iterable[str]) -> list[str]:
    """Return every folder a typed name could mean, on the best reading of it.

    Typing the whole path is a chore and remembering an account's exact
    capitalisation is a memory test, so a leaf name is enough: "health" finds
    "Personal/Health". Readings are tried in order — whole path, then leaf,
    then any path ending — and only the best one that matches anything is
    returned, so an exactly-typed folder is never made ambiguous by a
    same-named leaf somewhere else in the tree.

    Several folders can share a leaf name, so the caller gets a list and must
    decide what to do with more than one; picking silently would file mail
    somewhere the user did not name.
    """
    wanted = normalise_folder(typed).strip("/")
    if not wanted:
        return []
    exact: list[str] = []
    leaf: list[str] = []
    ending: list[str] = []
    for folder in folders:
        path = normalise_folder(folder)
        if path == wanted:
            exact.append(folder)
        elif path.rsplit("/", 1)[-1] == wanted:
            leaf.append(folder)
        elif path.endswith(f"/{wanted}"):
            ending.append(folder)
    return sorted(exact) or sorted(leaf) or sorted(ending)


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
