"""Map Apple Mail account UUIDs to their human-readable names.

The Envelope Index database keys accounts by an opaque prefix — a scheme
plus the first eight characters of the account UUID (e.g. ``imap://AAAAAAAA``)
— which is meaningless to a person picking an account for
``account_url_prefix``. AppleScript is the only way to recover the name Mail
actually shows the user, so this module asks Mail via ``osascript`` and
normalises the result into a small lookup table.

This is a read-only diagnostic helper: it must never raise, even if Mail
is not running or AppleScript fails.
"""

from __future__ import annotations

import subprocess

LOCAL_ACCOUNT_NAME = "On My Mac"
NOT_IN_MAIL = "(not in Mail)"

_SCRIPT = """
tell application "Mail"
  set out to ""
  repeat with a in every account
    set out to out & (name of a) & " | " & (id of a) & linefeed
  end repeat
  return out
end tell
"""


def account_names() -> dict[str, str]:
    """Map the first eight characters of each account UUID to its name in Mail.

    Returns an empty dict if Mail is not running, ``osascript`` is missing,
    AppleScript fails, or the output cannot be parsed — never raises.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if result.returncode != 0:
        return {}

    return _parse(result.stdout)


def _parse(output: str) -> dict[str, str]:
    """Parse ``name | uuid`` lines into a short-id -> name lookup."""
    names: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or " | " not in line:
            continue
        name, _, uuid = line.rpartition(" | ")
        name = name.strip()
        short_id = uuid.strip()[:8]
        if not name or len(short_id) < 8:
            continue
        names[short_id.upper()] = name
    return names


def resolve_account_name(prefix: str, names: dict[str, str]) -> str:
    """Look up the display name for an account prefix like ``imap://AAAAAAAA``.

    Local accounts (Apple Mail's "On My Mac" store) never appear in the
    AppleScript output, so they are special-cased. Anything else with no
    match is an account still indexed by the database but no longer
    configured in Mail.
    """
    if prefix.startswith("local://"):
        return LOCAL_ACCOUNT_NAME
    _, _, short_id = prefix.partition("://")
    return names.get(short_id.upper(), NOT_IN_MAIL)


def truncate_name(name: str, width: int) -> str:
    """Shorten ``name`` to fit ``width`` columns, marking truncation with an ellipsis."""
    if len(name) <= width:
        return name
    return name[: max(width - 1, 0)] + "…"
