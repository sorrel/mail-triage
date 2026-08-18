"""Leaving mailing lists, and finding out whether the request landed."""

from __future__ import annotations

import click
import time

from mail_triage.bounces import attribute, candidate_rows, render_report
from mail_triage.commands._shared import _snapshot
from mail_triage.config import load_config
from mail_triage.mail_app import AppleScriptMail, MailError
from mail_triage.sends import (
    FailedRequest,
    SentRequest,
    list_batches,
    load_batch,
    new_batch_id,
    record_failure,
    record_send,
)
from mail_triage.unsubscribe import (
    SelectionError,
    find_candidates,
    folder_url,
    parse_selection,
    render_candidates,
    send_unsubscribe,
)

# Default number of senders `unsubscribe` fetches headers for. Each is an
# AppleScript round trip, so this bounds the slow part of the command.
DEFAULT_UNSUBSCRIBE_LIMIT = 20

def _check_batch(config, mail, batch: list[SentRequest]):
    """Bounces for one batch, or a reason it could not be checked.

    A fresh snapshot every time: the one taken to find candidates predates
    the requests and cannot contain a reply to them.
    """
    account = batch[0].from_account
    source = next((item for item in config.sources if item.name == account), None)
    if source is None:
        return [], (
            f"Sent from {account or 'an account this tool could not identify'}, which is "
            "not a configured source — so the bounce cannot be looked for. Add it to "
            "[[source]] in your config, or check that inbox yourself."
        )
    with _snapshot() as reader:
        inbox_url = folder_url(reader, source, source.inbox)
        if inbox_url is None:
            return [], f"No inbox named {source.inbox!r} in {source.name}."
        rows = candidate_rows(reader.inbox_messages(inbox_url), batch)

    pairs = []
    unreadable = 0
    for row in rows:
        try:
            pairs.append((row, mail.message_headers(row.rowid, source.inbox, source.name)))
        except MailError:
            # A message can be moved or deleted between snapshot and fetch.
            # One unreadable candidate is not a reason to abandon the check.
            unreadable += 1
    bounces = attribute(pairs, batch)
    if unreadable:
        click.echo(
            f"({unreadable} candidate "
            f"{'message' if unreadable == 1 else 'messages'} could not be read — "
            "moved or deleted since the snapshot.)"
        )
    return bounces, None


@click.command()
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="List the candidates and send nothing.",
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    help="Report bounces for the last batch of requests instead of sending more.",
)
@click.option(
    "--limit",
    default=DEFAULT_UNSUBSCRIBE_LIMIT,
    help="How many senders to fetch unsubscribe headers for. Each is an "
    "AppleScript round trip, so this is the slow part.",
)
@click.option(
    "--sender",
    default=None,
    metavar="TEXT",
    help="Only offer senders whose address contains TEXT. Naming the sender "
    "is a safer way to send one than answering the first prompt, since the "
    "ranking shifts as mail arrives.",
)
def unsubscribe(dry_run: bool, check: bool, limit: int, sender: str | None) -> None:
    """List the lists worth leaving, then unsubscribe from the ones you pick.

    This is the only command that sends mail. It prints the whole ranked list
    and asks which of them to act on — several at once if you like ("1,4" or
    "1-3"). Nothing is sent for a sender you did not name, and the selection
    is confirmed as a set before anything goes out.

    Ranking counts the mail you binned as well as the mail you never opened:
    deleting a newsletter unread is the clearest statement there is that you
    do not want it.

    Only 'mailto:' unsubscribe targets are sent. HTTP one-click unsubscribe is
    deliberately unsupported — it would mean firing off arbitrary web requests
    to an address the sender chose — so those are listed with their URL for
    you to open yourself.

    A sent request is not a completed unsubscribe: a rejection comes back as
    a bounce moments later. Each run looks once before it finishes, and
    '--check' reports on the last batch any time afterwards. It can tell you
    which request bounced, not why — the reason is in the message body, which
    this tool does not read — and it never reports a request as delivered,
    because a silently discarded one looks exactly the same from here.
    """
    config = load_config()
    mail = AppleScriptMail()

    if check:
        batches = list_batches(config)
        if not batches:
            click.echo("No unsubscribe requests recorded yet.")
            return
        batch = load_batch(config, batches[0])
        if not batch:
            click.echo(f"Batch {batches[0]} recorded no sent requests.")
            return
        bounces, problem = _check_batch(config, mail, batch)
        if problem:
            click.echo(problem)
            return
        click.echo(render_report(batch, bounces, batches[0]))
        return

    with _snapshot() as reader:
        candidates = find_candidates(reader, config, mail, limit=limit)

    if sender:
        # Filtered after ranking, not before: the counts and the ordering are
        # the same ones a full run would show, so what you see here is what
        # you would have seen there.
        candidates = [
            option for option in candidates if sender.casefold() in option.sender.casefold()
        ]
        if not candidates:
            raise click.ClickException(f"No candidate sender contains {sender!r}.")

    if not candidates:
        click.echo("Nothing to unsubscribe from — no candidate carried a List-Unsubscribe header.")
        return

    click.echo(render_candidates(candidates))
    sendable = [number for number, option in enumerate(candidates, start=1)
                if option.method == "mailto"]
    click.echo(
        f"\n{len(candidates)} candidates, {len(sendable)} of them sendable "
        f"({len(candidates) - len(sendable)} are HTTP-only — open those yourself)."
    )

    if dry_run:
        click.echo("Nothing sent (--dry-run).")
        return

    if not sendable:
        click.echo("None of these can be unsubscribed from by email.")
        return

    try:
        typed = click.prompt(
            "Which to unsubscribe from? (numbers, or Enter for none)",
            default="",
            show_default=False,
        )
    except click.Abort:
        click.echo("\nNothing sent.")
        return
    try:
        chosen = parse_selection(typed, len(candidates))
    except SelectionError as error:
        raise click.ClickException(str(error)) from error
    if not chosen:
        click.echo("Nothing selected, nothing sent.")
        return

    # An HTTP-only sender cannot be acted on, and picking its number is a
    # misreading of the list rather than an instruction — say so and stop,
    # instead of sending the rest and mentioning it afterwards.
    picked = [(number, candidates[number - 1]) for number in chosen]
    unsendable = [number for number, option in picked if option.method != "mailto"]
    if unsendable:
        raise click.ClickException(
            f"{', '.join(str(n) for n in unsendable)} "
            f"{'is' if len(unsendable) == 1 else 'are'} HTTP-only and cannot be sent to. "
            "Open the URL yourself, and choose again without it."
        )

    click.echo("\nAbout to unsubscribe from:")
    for number, option in picked:
        click.echo(f"  {number}. {option.sender} → {option.target}")
    if not click.confirm(f"Send {len(picked)} unsubscribe "
                         f"{'request' if len(picked) == 1 else 'requests'}?", default=False):
        click.echo("Nothing sent.")
        return

    batch_id = new_batch_id()
    recorded: list[SentRequest] = []
    sent = 0
    failed = 0
    for number, option in picked:
        try:
            request = send_unsubscribe(option, mail)
        except (ValueError, MailError) as error:
            click.echo(click.style(f"  {option.sender}: not sent — {error}", fg="red"))
            # Printed *and* written down. Scrollback is not a log, and the
            # one failure that mattered was found by noticing stray drafts.
            record_failure(
                config,
                batch_id,
                FailedRequest(
                    sender=option.sender,
                    to_address=option.target,
                    subject=option.subject,
                    attempted_at=int(time.time()),
                    from_account=option.account,
                    reason=str(error),
                ),
            )
            failed += 1
            continue
        # Recorded only now, after the send returned. A record written first
        # would describe a request that might never have gone out, and
        # --check would then find no bounce for it and call it fine.
        record_send(config, batch_id, request)
        recorded.append(request)
        sent += 1
        click.echo(click.style(f"  {option.sender}: sent", fg="green"))

    click.echo(f"\nSent {sent}, failed {failed}.")
    if not recorded:
        return

    bounces, problem = _check_batch(config, mail, recorded)
    if problem:
        click.echo(problem)
    elif bounces:
        click.echo()
        click.echo(render_report(recorded, bounces, batch_id))
    else:
        click.echo(
            "No bounces yet — a rejection can take a minute to come back.\n"
            "Run 'mail-triage unsubscribe --check' shortly to confirm."
        )


def _unsubscribe_candidates(config, mail):
    """The ranked candidates, read from a fresh snapshot on demand.

    Deliberately lazy: this costs one AppleScript round trip per candidate,
    so it happens when the panel is opened rather than on every page load.
    """
    with _snapshot() as reader:
        return find_candidates(reader, config, mail)
