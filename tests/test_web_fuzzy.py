"""The folder matcher, driven through node.

The only real logic in the page, so it is tested rather than eyeballed. Node
is not a dependency of this project — if it is absent these skip, and every
other test still covers the page's security properties.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FUZZY = (
    Path(__file__).resolve().parents[1]
    / "src" / "mail_triage" / "web" / "static" / "fuzzy.js"
)

FOLDERS = [
    "House",
    "House/Web",
    "House/Parcels",
    "House/Admin",
    "House/House Kit",
    "Office",
    "Office/Conf",
    "Office/Office Kit/Newsletters",
    "Shopping/Wishlist",
    "Show/answers",
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def rank(query, folders=None, expected=None):
    script = (
        f"const {{rankFolders}} = require({str(FUZZY)!r});"
        f"process.stdout.write(JSON.stringify("
        f"rankFolders({json.dumps(query)}, {json.dumps(folders or FOLDERS)}, "
        f"{json.dumps(expected)})));"
    )
    finished = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def test_an_initialism_finds_the_nested_folder():
    """The point of the whole thing: a deep tree in three keystrokes."""
    assert rank("hw")[0] == "House/Web"


def test_a_segment_start_beats_a_letter_in_the_middle():
    assert rank("hw").index("House/Web") < rank("hw").index("Show/answers")


def test_typing_the_leaf_finds_it():
    assert rank("news")[0] == "Office/Office Kit/Newsletters"
    assert rank("parcels")[0] == "House/Parcels"


def test_a_non_matching_query_returns_nothing():
    assert rank("zzzz") == []


def test_matching_is_case_insensitive():
    assert rank("HOUSE/WEB")[0] == "House/Web"
    assert rank("house/web")[0] == "House/Web"


def test_the_shorter_name_wins_a_tie():
    """"office" matches both; the one actually called Office should lead."""
    assert rank("office")[0] == "Office"


def test_with_nothing_typed_the_expected_folder_leads():
    """Open the picker, press Return, and it files where the tool expected."""
    ranked = rank("", expected="House/Parcels")
    assert ranked[0] == "House/Parcels"
    assert len(ranked) == len(FOLDERS)


def test_with_nothing_typed_and_no_expectation_everything_is_offered():
    assert rank("") == FOLDERS


def test_an_expected_folder_that_is_not_a_real_folder_is_ignored():
    """A bill's predicted folder can be one this account does not have — that
    is exactly why the picker exists."""
    ranked = rank("", expected="Nowhere/Real")
    assert ranked == FOLDERS


def test_the_expected_folder_is_not_forced_ahead_of_what_you_typed():
    """Typing is disagreeing. It must win."""
    assert rank("conf", expected="House/Parcels")[0] == "Office/Conf"
