"""Pieces more than one command needs.

Deliberately small and deliberately dull: anything that belongs to a single
command stays with that command, so a reader of one file is not sent here to
understand it."""

from __future__ import annotations

import click
import sqlite3

from contextlib import contextmanager
from mail_triage.envelope import DEFAULT_DB_PATH, MessageRow, open_snapshot
from mail_triage.inputs import InputError
from mail_triage.mail_app import MailError, MailNotRunningError
from mail_triage.never_personal import NeverPersonalError
from mail_triage.rules import RulesError
from mail_triage.security import SecuritySendersError

# Wide enough to blank the longest in-place progress line written by the
# header guard before the table is printed over it.
PROGRESS_LINE_WIDTH = 60

def _no_disk_access(error: PermissionError) -> click.ClickException:
    """Turn "Operation not permitted" into the thing to actually go and do.

    Mail's database is TCC-protected, so reading it needs Full Disk Access for
    whichever terminal you are running in. Without this the failure arrives as
    a bare traceback from deep inside ``snapshot_database`` — ``accounts`` and
    ``size`` had handled it all along, whilst ``triage``, ``web`` and
    ``report`` did not.

    It fails safe: no database means no proposals, so a run that cannot read
    moves nothing.
    """
    return click.ClickException(
        f"Cannot read Mail's database ({error.filename or 'Envelope Index'}): "
        "operation not permitted.\n\n"
        "This needs Full Disk Access. In System Settings → Privacy & Security "
        "→ Full Disk Access, add the terminal you are running mail-triage in."
    )


@contextmanager
def _run_errors():
    """Every way loading a run can fail, said once.

    The four loaders and ``gather`` each have their own exception type, and
    all five mean the same thing to somebody at a terminal: the run cannot
    start, here is why. Translating them at each of the three call sites was
    fifteen lines apiece and had already drifted apart.
    """
    try:
        yield
    except (RulesError, NeverPersonalError, SecuritySendersError, InputError) as error:
        raise click.ClickException(str(error)) from error
    except PermissionError as error:
        raise _no_disk_access(error) from error


@contextmanager
def _snapshot():
    """``open_snapshot``, with the database's failures said in English.

    The three things that go wrong reading Mail's database — it is not there,
    we are not allowed, or the copy will not open — were translated in
    ``accounts`` and ``size`` and nowhere else, in twenty duplicated lines
    apiece. Everywhere else they arrived as tracebacks.

    Wrapping the body as well as the open is deliberate: a
    ``sqlite3.OperationalError`` from a query is the same "this snapshot will
    not read" problem as one from the connect, and a caller should not have to
    know which half it came from.
    """
    try:
        with open_snapshot(DEFAULT_DB_PATH) as reader:
            yield reader
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Cannot find {DEFAULT_DB_PATH}. Is this macOS with Apple Mail configured?"
        ) from error
    except PermissionError as error:
        raise _no_disk_access(error) from error
    except sqlite3.OperationalError as error:
        raise click.ClickException(
            f"Could not read the envelope database snapshot: {error}"
        ) from error


def _select_sources(config, source_names: tuple[str, ...]):
    """The sources to triage, narrowed by any --source given.

    An unknown name is refused rather than ignored: silently triaging every
    source when the user asked for one is the sort of surprise that moves
    mail they were not looking at.
    """
    sources = list(config.sources)
    if not source_names:
        return sources
    known = {source.name: source for source in sources}
    missing = [name for name in source_names if name not in known]
    if missing:
        raise click.ClickException(
            f"No source named {', '.join(repr(n) for n in missing)}. "
            f"Configured sources: {', '.join(sorted(known))}."
        )
    return [known[name] for name in source_names]


def _make_header_guard(mail):
    """Build the do-not-file guard hook (Task 11B) around a mail interface.

    Only called by ``Classifier`` for a message that would otherwise be
    filed, and only when the sender address alone is inconclusive — so the
    AppleScript round trip this triggers lands on a couple of dozen messages
    at most for a typical inbox, not all of them.

    A progress line is written to stderr as fetches happen: at ~0.1–0.5s per
    message, a couple of dozen round trips is a few seconds of otherwise
    silent, apparently-frozen terminal, which is worse than a ticking counter.

    Mail not being open is not an error to surface as a crash: every fetch
    will fail identically, ``Classifier`` already turns that into a veto per
    message (fail safe, not fail open), and this only adds one plain-English
    explanation the first time it happens, so the user understands *why*
    everything that would have been filed is staying put.
    """
    state = {"fetches": 0, "warned": False}

    def guard(message: MessageRow) -> dict[str, str] | None:
        state["fetches"] += 1
        click.echo(
            f"\rChecking whether senders need a reply... {state['fetches']}",
            nl=False, err=True,
        )
        try:
            return mail.message_headers(message.rowid)
        except MailNotRunningError:
            if not state["warned"]:
                state["warned"] = True
                click.echo(
                    "\nMail is not running — cannot check which of these senders are "
                    "bulk mail, so anything that would otherwise be filed is staying "
                    "in the inbox until Mail is open.",
                    err=True,
                )
            raise
        except MailError as error:
            if not state["warned"]:
                state["warned"] = True
                click.echo(
                    f"\nCould not check message headers ({error}); keeping affected "
                    "messages in the inbox to be safe.",
                    err=True,
                )
            raise

    return guard, state
