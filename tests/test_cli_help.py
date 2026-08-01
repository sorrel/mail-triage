"""The grouped, coloured help screen."""

import re

import click
from click.testing import CliRunner

from mail_triage.cli import cli
from mail_triage.cli_help import OTHER, SECTIONS, ColouredGroup
from mail_triage.review import display_width


def plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_every_command_appears_in_the_help():
    """A help screen that omits a command is worse than an ugly one."""
    result = CliRunner().invoke(cli, ["--help"], color=True)
    listed = plain(result.output)
    for name in cli.list_commands(None):
        assert re.search(rf"^\s+{re.escape(name)}\s", listed, re.MULTILINE), name


def test_a_command_nobody_grouped_still_appears():
    """The section map will rot the moment someone adds a command."""

    @click.group(cls=ColouredGroup)
    def group():
        pass

    @group.command()
    def newcomer():
        """A command added after the sections were written."""

    result = CliRunner().invoke(group, ["--help"], color=True)
    assert OTHER in plain(result.output)
    assert "newcomer" in plain(result.output)


def test_sections_are_titled_and_coloured():
    result = CliRunner().invoke(cli, ["--help"], color=True)
    for title, _ in SECTIONS:
        assert title in plain(result.output)
    assert "\x1b[36m" in result.output


def test_command_names_are_coloured():
    result = CliRunner().invoke(cli, ["--help"], color=True)
    assert "\x1b[33m" in result.output


def test_descriptions_align_across_every_section():
    """One column width for the whole screen, not one per group."""
    result = CliRunner().invoke(cli, ["--help"], color=True)
    starts = set()
    for line in plain(result.output).splitlines():
        match = re.match(r"^    (\S+)( +)\S", line)
        if match:
            starts.add(display_width(match.group(1) + match.group(2)))
    assert len(starts) == 1, f"ragged description column: {starts}"


def test_help_is_plain_when_not_a_terminal():
    """Redirecting the help to a file must not fill it with escapes."""
    result = CliRunner().invoke(cli, ["--help"])
    assert "\x1b[" not in result.output


def test_the_size_command_is_listed_where_you_would_look_for_it():
    result = CliRunner().invoke(cli, ["--help"], color=True)
    listed = plain(result.output)
    look = listed.index("Look at what you have")
    teach = listed.index("Teach it")
    assert look < listed.index("size") < teach
