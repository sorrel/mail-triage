"""Look at what you have. Both read-only; neither touches a message."""

from __future__ import annotations

import click

from mail_triage.accounts import account_names, resolve_account_name, truncate_name
from mail_triage.commands._shared import _snapshot
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.size_report import (
    parse_size,
    render_account,
    render_maildata,
    render_summary,
)
from mail_triage.sizes import build_account_usage, maildata_usage

# Width of the account-name column in `accounts`.
ACCOUNT_NAME_WIDTH = 22

@click.command()
def accounts() -> None:
    """List mail accounts with mailbox and message counts.

    The prefixes shown here are what local/config.toml wants: one as each
    [[source]]'s 'prefix', and the account you file into as
    'filing_account_prefix'.
    """
    with _snapshot() as reader:
        summary = reader.account_summary()

    names = account_names()
    name_width = ACCOUNT_NAME_WIDTH
    click.echo(f"{'Account':<28}{'Name':<{name_width}}{'Mailboxes':>10}{'Messages':>10}")
    for prefix, mailbox_count, message_count in summary:
        name = truncate_name(resolve_account_name(prefix, names), name_width - 2)
        click.echo(f"{prefix:<28}{name:<{name_width}}{mailbox_count:>10}{message_count:>10}")


@click.command()
@click.option(
    "--min-size",
    default="2MB",
    help="Collapse folders smaller than this into a single line. '0' shows everything.",
)
@click.option(
    "--account",
    "account_filter",
    default=None,
    metavar="NAME",
    help="Report one account only, matched on its name or prefix.",
)
@click.option(
    "--bytes",
    "exact",
    is_flag=True,
    default=False,
    help="Show exact byte counts instead of rounded sizes.",
)
def size(min_size: str, account_filter: str | None, exact: bool) -> None:
    """Show how much space each mail folder occupies.

    Two figures per folder. 'In Mail' is the size of every message Mail knows
    about, from the envelope database, including bodies never downloaded.
    'On disk' is what the folder actually costs this Mac. Where they differ
    markedly, that is a fact about the account — usually mail held on the
    server and not cached locally — and not an error in the report.

    Read-only throughout: it copies the database, stats files, and touches
    neither Mail nor a single message.
    """
    try:
        minimum = parse_size(min_size)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--min-size") from error

    mail_root = DEFAULT_DB_PATH.parent.parent
    with _snapshot() as reader:
        mailbox_sizes = reader.mailbox_sizes()

    usages = build_account_usage(mailbox_sizes, mail_root, account_names())
    maildata = maildata_usage(mail_root)

    if account_filter is not None:
        wanted = account_filter.casefold()
        matched = [
            usage for usage in usages
            if wanted in usage.name.casefold() or wanted in usage.prefix.casefold()
        ]
        if not matched:
            available = ", ".join(sorted(usage.name for usage in usages)) or "none"
            raise click.ClickException(
                f"No account matching {account_filter!r}. Available: {available}."
            )
        if len(matched) > 1:
            # Listed rather than silently picking one: choosing for the user
            # here would quietly report the wrong account's figures.
            candidates = ", ".join(sorted(usage.name for usage in matched))
            raise click.ClickException(
                f"{account_filter!r} matches more than one account: {candidates}. "
                "Use a longer fragment, or the account prefix."
            )
        usages = matched
        maildata = []

    if not usages:
        click.echo("No accounts found in Mail's database.")
        return

    if account_filter is None:
        click.echo(render_summary(usages, sum(size for _, size in maildata), exact))
        click.echo()
    for usage in usages:
        click.echo(render_account(usage, minimum, exact))
        click.echo()
    if maildata:
        click.echo(render_maildata(maildata, exact, minimum))
