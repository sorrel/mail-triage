"""The repository is destined for GitHub. Nothing personal may reach src/.

Everything identifying — the trained model, rules, journals, real config,
cached headers — lives in the gitignored ``local/`` directory. These tests are
the mechanical check on that, because a personal literal is easy to add by
accident while debugging against a real mailbox and very hard to spot later.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"

# Reserved for documentation and tests (RFC 2606). Anything else is suspect.
ALLOWED_DOMAINS = {"example.com", "example.org", "example.net"}

# The denylist of identifying terms: gitignored, because writing a name
# here in order to assert its absence would publish it.
TERMS_FILE = ROOT / "local" / "identifying-terms.txt"


def _source_files():
    return sorted(SOURCE.rglob("*.py"))


def test_source_files_exist_to_check():
    """Guard against the guard silently passing on an empty glob."""
    assert _source_files(), "no source files found — the checks below would be vacuous"


def test_no_real_email_addresses_in_source():
    pattern = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
    offences = []
    for path in _source_files():
        for match in pattern.findall(path.read_text()):
            domain = match.split("@", 1)[1].casefold()
            if domain in ALLOWED_DOMAINS or domain.endswith(".example"):
                continue
            offences.append(f"{path.relative_to(ROOT)}: {match}")
    assert offences == [], f"Possible real addresses in source: {offences}"


def test_no_account_uuids_in_source():
    uuid = re.compile(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"
    )
    offences = [
        str(path.relative_to(ROOT)) for path in _source_files() if uuid.search(path.read_text())
    ]
    assert offences == [], f"Account UUIDs found in: {offences}"


def test_no_real_folder_paths_in_source():
    """The user's actual folder names must not be baked into the code.

    The list of real folders used to be a literal here — which made this
    guard against publishing personal data into a published list of the
    user's folder names. It now comes from the same gitignored denylist as
    every other identifying term (see ``TERMS_FILE`` below), so there is one
    place to maintain and nothing to leak. Skipped, visibly, when that file
    is absent — as it will be for anyone who clones this repository, for whom
    there is nothing to protect.
    """
    import pytest

    if not TERMS_FILE.exists():
        pytest.skip(f"no denylist at {TERMS_FILE}; nothing to check against")
    forbidden = [
        line.strip() for line in TERMS_FILE.read_text().splitlines()
        if "/" in line and not line.startswith("#")
    ]
    offences = []
    for path in _source_files():
        text = path.read_text()
        offences.extend(
            f"{path.relative_to(ROOT)}: {name}" for name in forbidden if name in text
        )
    assert offences == [], f"Real folder names in source: {offences}"


def test_local_directory_is_gitignored():
    assert "local/" in (ROOT / ".gitignore").read_text()


def test_nothing_under_local_is_tracked_by_git():
    """The strongest form of the check: ask git, not the ignore file."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "local"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    assert tracked == "", f"local/ files are tracked by git: {tracked}"


# The checks above cover src/. Publishing exposes the tests and the design
# docs too, and those turned out to be the richer source: quoted requirements
# naming the author, real nested folder paths, and test fixtures built from
# real subject lines that named a bank, a supermarket and several suppliers.

def _publishable_files():
    import subprocess

    listed = subprocess.run(
        ["git", "ls-files", "src", "tests", "docs", "README.md", "CLAUDE.md", "config.example.toml"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    return [ROOT / name for name in listed]


# The denylist of identifying terms cannot live in this file: writing
# a name here in order to assert that name is absent would publish it. It lives in
# ``local/identifying-terms.txt`` (gitignored), one term per line, and the
# check skips with an explanation when that file is absent — as it will be for
# anyone who clones this repository, for whom there is nothing to protect.


def test_no_identifying_terms_anywhere_publishable():
    import pytest

    if not TERMS_FILE.exists():
        pytest.skip(f"no denylist at {TERMS_FILE}; nothing to check against")
    terms = [
        line.strip() for line in TERMS_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert terms, f"{TERMS_FILE} is empty — the check would be vacuous"
    offences = []
    for path in _publishable_files():
        if not path.exists():
            continue
        lowered = path.read_text().casefold()
        offences.extend(
            f"{path.relative_to(ROOT)}: {term}"
            for term in terms
            if term.casefold() in lowered
        )
    assert offences == [], f"Identifying terms found: {offences}"


def test_no_real_email_addresses_anywhere_publishable():
    pattern = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
    offences = []
    for path in _publishable_files():
        if not path.exists():
            continue
        for match in pattern.findall(path.read_text()):
            domain = match.split("@", 1)[1].casefold().rstrip(".,)\"'")
            if domain in ALLOWED_DOMAINS or domain.endswith(".example"):
                continue
            if domain.endswith("users.noreply.github.com") or "anthropic.com" in domain:
                continue
            offences.append(f"{path.relative_to(ROOT)}: {match}")
    assert offences == [], f"Possible real addresses: {offences}"


def test_the_example_config_carries_no_real_account():
    """Every account prefix in the example must be an obvious placeholder.

    Checked as a property of each prefix rather than by looking for one
    literal: the example now documents several accounts, and it needs a
    distinct placeholder per account to show which is which. A run of one
    repeated character is a placeholder; a real prefix is eight hex digits
    from a UUID and will not be.
    """
    text = (ROOT / "config.example.toml").read_text()
    # Only the configured values, not the prose: the surrounding comments
    # explain the format with placeholders of their own ("scheme://...").
    prefixes = re.findall(r"^\s*(?:filing_account_)?prefix\s*=\s*\"[a-z]+://([^\"]+)\"",
                          text, re.MULTILINE)
    assert prefixes, "the example config should show at least one account prefix"
    for prefix in prefixes:
        assert len(set(prefix)) == 1, (
            f"{prefix!r} in config.example.toml looks like a real account prefix; "
            "use a placeholder such as AAAAAAAA"
        )


# --- What .gitignore must cover -------------------------------------------------
#
# Two near-misses found on 27 July 2026 while preparing to publish:
# ``.pytest_cache`` was ignored only because pytest writes its own .gitignore
# inside it, and ``.superpowers/`` was ignored by nothing at all — it merely
# happened to be empty, and git does not track empty directories. Both would
# have been swept in by ``git add -A`` the moment that changed. These assert
# the rules exist rather than relying on luck.

import subprocess

import pytest


def _is_ignored(relative_path: str) -> bool:
    """Ask git, not the file, whether a path would be ignored."""
    return subprocess.run(
        ["git", "check-ignore", "-q", relative_path], cwd=ROOT
    ).returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        # Everything identifying lives here.
        "local/model.json",
        "local/rules.json",
        "local/config.toml",
        "local/journal/2026-01-01T00-00-00.jsonl",
        "local/identifying-terms.txt",
        # Tooling and caches.
        ".venv/pyvenv.cfg",
        ".pytest_cache/CACHEDIR.TAG",
        ".ruff_cache/anything",
        ".mypy_cache/anything",
        "src/mail_triage/__pycache__/cli.cpython-313.pyc",
        ".superpowers/sdd/anything.md",
        # Secrets and OS clutter.
        ".env",
        ".env.local",
        ".DS_Store",
        "src/.DS_Store",
        # Build output.
        "dist/mail_triage-0.1.0.tar.gz",
        "build/lib/anything.py",
        "src/mail_triage.egg-info/PKG-INFO",
        # A stray copy of the mail database would be the worst possible leak.
        "Envelope Index",
        "Envelope Index-wal",
        "snapshot/Envelope Index-shm",
        "mail.mbox",
        "backup.sqlite",
    ],
)
def test_dangerous_paths_are_gitignored(path):
    assert _is_ignored(path), f"{path} is not covered by .gitignore"


def test_source_and_docs_are_not_accidentally_ignored():
    """An over-broad rule that hid the actual project would be worse."""
    for path in ["src/mail_triage/cli.py", "tests/conftest.py", "README.md",
                 "docs/superpowers/plans/2026-07-26-mail-triage.md"]:
        assert not _is_ignored(path), f"{path} is wrongly ignored"
