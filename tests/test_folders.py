import pytest

from mail_triage.folders import account_prefix, folder_path, is_excluded, normalise_folder


def test_account_prefix_truncates_uuid():
    assert account_prefix("imap://AAAAAAAA-1111-2222/Orders") == "imap://AAAAAAAA"


def test_folder_path_decodes_and_strips():
    assert folder_path("imap://AAAAAAAA/Home%20Tech") == "Home Tech"


def test_folder_path_keeps_nesting():
    assert folder_path("imap://AAAAAAAA/Work/Meetings") == "Work/Meetings"


def test_normalise_is_case_and_space_insensitive():
    assert normalise_folder("Home  Tech") == normalise_folder("home tech")


def test_normalise_keeps_distinct_folders_distinct():
    assert normalise_folder("Home Tech") != normalise_folder("Work Tech")


@pytest.mark.parametrize(
    "folder,expected",
    [("INBOX", True), ("Deleted Messages", True), ("Sent", True), ("Orders", False)],
)
def test_exclusion_patterns(folder, expected):
    patterns = ["INBOX", "Deleted*", "Sent*"]
    assert is_excluded(folder, patterns) is expected


# --- The [Gmail] glob trap ------------------------------------------------------
#
# Patterns are fnmatch globs, so "[Gmail]" is a character class matching one of
# G m a i l. The obvious pattern is wrong in both directions, and the folder it
# fails to exclude holds every message in the account.

def test_the_naive_gmail_pattern_over_matches_ordinary_folders():
    """"[Gmail]*" is a character class, and it swallows innocent folders.

    Measured, 30 July 2026: it excludes any folder whose leaf name begins
    with g, m, a, i or l. It happens to catch "[Gmail]/All Mail" too — via
    the "a" of "All Mail", not the bracket — so a careless author would see
    it "work" whilst it quietly dropped a large part of the filing tree out
    of training.
    """
    for folder in ("Accounts", "Local", "Invoices", "Music", "Games", "Parent/Local"):
        assert is_excluded(folder, ["[Gmail]*"]), folder
    assert is_excluded("[Gmail]/All Mail", ["[Gmail]*"])


def test_the_escaped_pattern_is_the_one_that_means_what_it_says():
    assert is_excluded("[Gmail]/All Mail", ["[[]Gmail]*"])
    for folder in ("Accounts", "Local", "Invoices", "Music", "Games", "Parent/Local"):
        assert not is_excluded(folder, ["[[]Gmail]*"]), folder


def test_escaped_gmail_pattern_excludes_the_pseudo_folders():
    patterns = ["[[]Gmail]*"]
    for folder in ("[Gmail]/All Mail", "[Gmail]/Bin", "[Gmail]/Important",
                   "[Gmail]/Starred", "[Gmail]All Mail"):
        assert is_excluded(folder, patterns), folder


def test_escaped_gmail_pattern_does_not_match_a_plain_folder():
    """The unescaped form would match "G/anything"; the escaped form must not."""
    for folder in ("Gmail/Notes", "G/Something", "Parent/Child"):
        assert not is_excluded(folder, ["[[]Gmail]*"]), folder
