"""Command-line entry point."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import click

from mail_triage.accounts import account_names, resolve_account_name, truncate_name
from mail_triage.asking import (
    ask_all, build_billing_senders, build_yearly_counts, rank_uncertain,
)
from mail_triage.cli_help import ColouredGroup
from mail_triage.config import load_config
from mail_triage.corrections import load_corrections, record_overrides
from mail_triage.deletion import PerAccountDeletionIndex, build_deletion_index
from mail_triage.envelope import DEFAULT_DB_PATH, EnvelopeReader, MessageRow, snapshot_database
from mail_triage.execute import execute
from mail_triage.folders import account_prefix, folder_path, match_folders
from mail_triage.journal import Journal, list_runs, new_run_id, undo_run
from mail_triage.mail_app import AppleScriptMail, MailError, MailNotRunningError
from mail_triage.model.classify import Classifier
from mail_triage.model.store import load_model, save_model, train_from_history
from mail_triage.review import (
    render_table, review, review_held, review_unplaced, summarise,
)
from mail_triage.rules import RulesError, forget_rule, load_rules
from mail_triage.size_report import (
    parse_size, render_account, render_maildata, render_summary,
)
from mail_triage.sizes import build_account_usage, maildata_usage


@click.group(cls=ColouredGroup)
@click.version_option()
def cli() -> None:
    """Local-first triage for Apple Mail."""


@cli.command()
def accounts() -> None:
    """List mail accounts with mailbox and message counts.

    The prefixes shown here are what local/config.toml wants: one as each
    [[source]]'s 'prefix', and the account you file into as
    'filing_account_prefix'.
    """
    with tempfile.TemporaryDirectory() as work:
        reader = None
        try:
            snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
            reader = EnvelopeReader(snapshot)
            summary = reader.account_summary()
        except FileNotFoundError as error:
            raise click.ClickException(
                f"Cannot find {DEFAULT_DB_PATH}. Is this macOS with Apple Mail configured?"
            ) from error
        except PermissionError as error:
            raise click.ClickException(
                "Cannot read Mail's database. Grant Full Disk Access to your terminal "
                "in System Settings → Privacy & Security → Full Disk Access."
            ) from error
        except sqlite3.OperationalError as error:
            raise click.ClickException(
                f"Could not read the envelope database snapshot: {error}"
            ) from error
        finally:
            if reader is not None:
                reader.close()

        names = account_names()
        name_width = 22
        click.echo(f"{'Account':<28}{'Name':<{name_width}}{'Mailboxes':>10}{'Messages':>10}")
        for prefix, mailbox_count, message_count in summary:
            name = truncate_name(resolve_account_name(prefix, names), name_width - 2)
            click.echo(f"{prefix:<28}{name:<{name_width}}{mailbox_count:>10}{message_count:>10}")


@cli.command()
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
    with tempfile.TemporaryDirectory() as work:
        reader = None
        try:
            snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
            reader = EnvelopeReader(snapshot)
            mailbox_sizes = reader.mailbox_sizes()
        except FileNotFoundError as error:
            raise click.ClickException(
                f"Cannot find {DEFAULT_DB_PATH}. Is this macOS with Apple Mail configured?"
            ) from error
        except PermissionError as error:
            raise click.ClickException(
                "Cannot read Mail's database. Grant Full Disk Access to your terminal "
                "in System Settings → Privacy & Security → Full Disk Access."
            ) from error
        except sqlite3.OperationalError as error:
            raise click.ClickException(
                f"Could not read the envelope database snapshot: {error}"
            ) from error
        finally:
            if reader is not None:
                reader.close()

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


@cli.command()
@click.option("--drift/--no-drift", default=True, help="Show senders whose destination changed.")
def learn(drift: bool) -> None:
    """Build the classifier from your filing history."""
    config = load_config()
    model = train_from_history(config, DEFAULT_DB_PATH)
    save_model(model, config.model_path)
    click.echo(f"Trained on {model.example_count:,} filed messages.")
    # Reported separately from the total because they are not equal evidence:
    # each one counts for correction_weight historical filings, and knowing
    # how many there are is what says whether the model is being argued with.
    corrections = load_corrections(config)
    if corrections:
        click.echo(
            f"Including {len(corrections):,} correction"
            f"{'s' if len(corrections) != 1 else ''} at {config.correction_weight:g}× weight."
        )
    click.echo(f"Known senders: {len(model.sender.by_sender):,}")
    click.echo(f"Known domains: {len(model.sender.by_domain):,}")
    click.echo(f"Model written to {config.model_path}")
    if drift:
        entries = model.sender.drift_report()
        if entries:
            click.echo(f"\n{len(entries)} senders changed destination over time:")
            for entry in entries[:20]:
                click.echo(f"  {entry.key}: '{entry.old_folder}' → '{entry.new_folder}' (by {entry.switch_year})")
            if len(entries) > 20:
                click.echo(f"  ... and {len(entries) - 20} more")


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


def _ask_about_uncertain_senders(
    proposals, model, folders, yearly_counts, config, rules, classify_all,
    billing_senders=frozenset(),
):
    """Ask about the highest-leverage uncertain senders, then re-classify.

    Asking happens *before* the proposal table so answers apply to the current
    run rather than only the next one. Re-classification runs over everything
    again for simplicity; the inbox is small enough (tens of messages) that
    confining it to the answered senders would not be a measurable saving.
    """
    uncertain = rank_uncertain(proposals, model, folders, yearly_counts,
                               billing_senders=billing_senders)
    if not uncertain:
        return proposals
    click.echo(
        f"\n{len(uncertain)} senders I can't call. One answer settles every message "
        "from them, now and in future."
    )
    answered = ask_all(
        uncertain,
        lambda text: click.prompt(text, default="", show_default=False, prompt_suffix=""),
        lambda typed: match_folders(typed, folders),
        config.rules_path,
    )
    click.echo()
    if not answered:
        return proposals
    click.echo(f"Recorded {len(answered)} rules.\n")
    rules = dict(rules)
    rules.update({rule.sender: rule for rule in answered})
    return classify_all(rules)


@cli.command()
@click.option("--forget", default=None, metavar="SENDER", help="Remove one sender's rule.")
def rules(forget: str | None) -> None:
    """List the answers you have given about where senders' mail goes."""
    config = load_config()
    if forget is not None:
        if not forget_rule(config.rules_path, forget):
            raise click.ClickException(f"No rule for {forget}.")
        click.echo(f"Forgot the rule for {forget}.")
        return
    try:
        known = load_rules(config.rules_path)
    except RulesError as error:
        raise click.ClickException(str(error)) from error
    if not known:
        click.echo("No rules yet. 'mail-triage triage' will ask about uncertain senders.")
        return
    for sender, rule in sorted(known.items()):
        target = {"file": rule.folder, "bin": "(delete)"}.get(rule.action, "(left alone)")
        click.echo(f"{sender:<44}{target}")
    click.echo(f"\n{len(known)} rules. Remove one with: mail-triage rules --forget <sender>")


@cli.command()
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Report only; move nothing. Live runs still confirm before acting.",
)
@click.option(
    "--limit",
    default=0,
    help="Act on at most N messages (0 means no limit). Applies to the messages "
    "actually offered for filing, so --limit 1 reliably yields one move.",
)
@click.option(
    "--ask/--no-ask",
    default=True,
    help="Ask where mail from up to five uncertain senders should go, before proposing.",
)
@click.option(
    "--source",
    "source_names",
    multiple=True,
    help="Triage only these sources, by name. Repeatable. Default: all of them.",
)
def triage(dry_run: bool, limit: int, ask: bool, source_names: tuple[str, ...]) -> None:
    """Classify every configured inbox, then file what you approve.

    Each source's inbox is scanned and the proposals are shown together;
    mail is filed into the filing account's folder tree whichever account
    it arrived in. Binning stays in the account the message came from.

    Nothing moves without a confirmation at the prompt, and every move is
    journalled beforehand so 'mail-triage undo' can reverse the whole run.
    """
    config = load_config()
    sources = list(config.sources)
    if source_names:
        known = {source.name: source for source in sources}
        missing = [name for name in source_names if name not in known]
        if missing:
            raise click.ClickException(
                f"No source named {', '.join(repr(n) for n in missing)}. "
                f"Configured sources: {', '.join(sorted(known))}."
            )
        sources = [known[name] for name in source_names]
    model = load_model(config.model_path)
    # Loaded before anything else: an unreadable rules file must stop the run,
    # not be silently ignored, or mail gets filed contrary to instructions.
    try:
        rules = load_rules(config.rules_path)
    except RulesError as error:
        raise click.ClickException(str(error)) from error
    # The folder list comes from the database, not AppleScript: it preserves
    # full nested paths ("Parent/Child") and real capitalisation, both of
    # which AppleScript's flat leaf-name list would lose.
    with tempfile.TemporaryDirectory() as work:
        snapshot = snapshot_database(DEFAULT_DB_PATH, Path(work))
        reader = EnvelopeReader(snapshot)
        try:
            mailbox_urls = reader.mailbox_urls()
            messages = []
            indices: dict[str, dict] = {}
            source_folders: dict[str, set[str]] = {}
            for source in sources:
                inbox_url = next(
                    (
                        url for url in mailbox_urls
                        if url.startswith(source.prefix)
                        and folder_path(url).casefold() == source.inbox.casefold()
                    ),
                    None,
                )
                if inbox_url is None:
                    raise click.ClickException(
                        f"No mailbox '{source.inbox}' under {source.prefix} for source "
                        f"'{source.name}'. Run 'mail-triage accounts' to check the prefix."
                    )
                # inbox_messages, not messages_in_mailbox: a Gmail inbox is a
                # label, so filtering on the mailbox URL alone finds nothing.
                messages.extend(reader.inbox_messages(inbox_url))
                # Task 11C: built from this same open reader/snapshot, not a
                # second one — filing and deletion counts must come from
                # identical data or the same-window guarantee is meaningless.
                # One index per account: habits differ between them.
                indices[source.prefix] = build_deletion_index(reader, config, source)
                # That source's own mailboxes, for the pre-flight bin check
                # below: a bin stays in its own account, so the filing
                # account's folder list cannot answer for it.
                source_folders[source.prefix] = {
                    folder_path(url).casefold()
                    for url in mailbox_urls
                    if url.startswith(source.prefix) and folder_path(url)
                }
            deletion_index = PerAccountDeletionIndex(indices)
            # Candidate folders come from the filing account alone: there is
            # one filing tree and every source's mail goes into it.
            folders = [
                folder_path(url)
                for url in mailbox_urls
                if url.startswith(config.filing_account_prefix) and folder_path(url)
            ]
            # Attachment names for the invoice guard, scoped to the inbox
            # rather than the whole table, and read from this same snapshot.
            attachments = reader.attachment_names(m.rowid for m in messages)
            # Ranking basis for the questions below: how often each sender
            # writes, not how many of their messages are in today's inbox.
            yearly_counts = build_yearly_counts(reader, config) if ask else {}
            # Senders who send bills are never offered the bin answer.
            billing_senders = build_billing_senders(reader, config) if ask else set()
        finally:
            reader.close()
    mail = AppleScriptMail()
    guard, guard_state = _make_header_guard(mail)

    def classify_all(current_rules):
        classifier = Classifier(
            model, config, folders, guard=guard,
            deletion_index=deletion_index, rules=current_rules,
            attachments=attachments,
        )
        return [classifier.classify(message) for message in messages]

    proposals = classify_all(rules)
    if guard_state["fetches"]:
        click.echo("\r" + " " * 60 + "\r", nl=False, err=True)

    if ask:
        proposals = _ask_about_uncertain_senders(
            proposals, model, folders, yearly_counts, config, rules, classify_all,
            billing_senders,
        )

    click.echo(render_table(proposals, {s.prefix: s.name for s in sources}))
    click.echo()
    click.echo(summarise(proposals, {s.prefix: s.name for s in sources}))

    if dry_run:
        click.echo("\nDry run — nothing was moved.")
        return

    # --limit caps the messages *offered*, not the messages classified: a cap
    # applied before classification could pick N messages that all turn out
    # to be unfilable, so "--limit 1" would move nothing and prove nothing.
    if limit:
        placed = [item for item in proposals if item.folder is not None]
        if len(placed) > limit:
            not_offered = len(placed) - limit
            proposals = placed[:limit]
            click.echo(f"\nLimited to {limit} of {len(placed)} filable messages "
                       f"({not_offered} not offered this run).")

    decisions = review(
        proposals,
        lambda text: click.prompt(text, default="q", show_default=False),
        lambda typed: match_folders(typed, folders),
    )
    # Then the mail the attention guard held back. Offered before the binning
    # pass because it is the more consequential of the two: these are
    # messages the classifier was confident about and something else
    # overrode, which is precisely what a person should see whilst still
    # paying attention.
    decisions += review_held(
        proposals, lambda text: click.prompt(text, default="l", show_default=False)
    )
    # Last, a pass over what stayed put. Without it the largest group in a
    # typical run — mail the classifier could not place — has no answer
    # available at all, run after run.
    decisions += review_unplaced(
        proposals, lambda text: click.prompt(text, default="k", show_default=False)
    )
    accepted = [decision for decision in decisions if decision.accepted]
    if not accepted:
        click.echo("Nothing accepted — no mail was moved.")
        return

    # Every folder the user typed over a proposal, recorded before anything
    # moves: it is an instruction about where mail belongs, and it holds
    # whether or not the move that follows happens to succeed. The next
    # 'learn' weights these at correction_weight against plain history.
    corrected = record_overrides(accepted, config)
    if corrected:
        click.echo(
            f"Recorded {corrected} correction{'s' if corrected != 1 else ''} — "
            "run 'mail-triage learn' to fold them in."
        )

    to_bin = sum(1 for decision in accepted if decision.is_delete)
    if to_bin:
        # Checked only when something is actually being binned, so an account
        # whose Trash is named differently still files mail normally. Checked
        # *before* the batch starts, so a misconfigured name costs nothing
        # rather than failing message by message.
        #
        # Per source, against that source's own mailboxes: a bin never
        # crosses accounts, so the filing account's folder list cannot
        # answer for whether Gmail has a bin.
        binning_prefixes = {
            account_prefix(decision.proposal.message.mailbox_url)
            for decision in accepted if decision.is_delete
        }
        for source in sources:
            if source.prefix not in binning_prefixes:
                continue
            if source.trash.casefold() not in source_folders.get(source.prefix, set()):
                raise click.ClickException(
                    f"No mailbox '{source.trash}' in account '{source.name}', so "
                    "there is nowhere to delete to. Set that source's 'trash' in "
                    "local/config.toml to the name the account uses."
                )

    journal = Journal(config)
    run_id = new_run_id()
    journal.begin(run_id)
    moved, failed = execute(accepted, mail, journal, config)
    binned_note = f", {to_bin} binned" if to_bin else ""
    click.echo(f"Moved {moved} messages{binned_note} ({failed} failed). Run id: {run_id}")
    click.echo(f"Reverse this with: mail-triage undo {run_id}")


@cli.command()
@click.argument("sender")
def explain(sender: str) -> None:
    """Show why mail from a sender is filed where it is.

    Read-only: it inspects the trained model and your rules, and touches no
    mailbox. Rules are reported first because they outrank the model — an
    explanation that gave only stage A's opinion would state the opposite of
    what actually happens whenever a rule disagrees with the history.
    """
    config = load_config()
    try:
        model = load_model(config.model_path)
    except (FileNotFoundError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    # Loaded strictly, as `triage` does: treating an unreadable rules file as
    # "no rules" would make this command misreport what would happen.
    try:
        known = load_rules(config.rules_path)
    except RulesError as error:
        raise click.ClickException(str(error)) from error

    address = sender.casefold()
    domain = address.split("@", 1)[1] if "@" in address else address

    rule = known.get(address)
    if rule is not None:
        decision = {
            "file": f"filed to '{rule.folder}'",
            "bin": "deleted",
            "leave": "left alone",
        }[rule.action]
        click.echo(f"You have a rule for {address}: mail is {decision}.")
        click.echo("A rule outranks the model below (but not a per-message guard).")
        click.echo(f"Forget it with: mail-triage rules --forget {address}\n")

    prediction = model.sender.predict(address, domain)
    if prediction is None:
        click.echo(
            f"No filing history for {address} or for {domain}. "
            "Stage B would decide from the subject line instead."
        )
        return
    click.echo(f"{address} → '{prediction.folder}' at confidence {prediction.confidence:.2f}")
    click.echo(f"Reason: {prediction.reason}")
    counts = model.sender.by_sender.get(address) or model.sender.by_domain.get(domain, {})
    for folder, weight in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        click.echo(f"  {folder:<40}{weight:>8.2f}")


@cli.command()
@click.argument("run_id", required=False)
@click.option(
    "--account",
    default="iCloud",
    help="Fallback account for journal entries written before runs recorded "
    "their own. Ignored for anything triaged since.",
)
def undo(run_id: str | None, account: str) -> None:
    """Reverse a triage run. Defaults to the most recent."""
    config = load_config()
    runs = list_runs(config)
    if not runs:
        raise click.ClickException("No runs to undo.")
    target = run_id or runs[0]
    if target not in runs:
        raise click.ClickException(
            f"No journal for run {target!r}. Known runs: {', '.join(runs[:5])}"
        )
    reversed_count, failed = undo_run(target, config, AppleScriptMail(), account)
    click.echo(f"Reversed {reversed_count} moves from run {target} ({failed} failed).")


@cli.command()
def runs() -> None:
    """List triage runs that can be undone, newest first."""
    config = load_config()
    known = list_runs(config)
    if not known:
        click.echo("No runs recorded yet.")
        return
    journal = Journal(config)
    for run_id in known:
        entries = journal.load(run_id)
        moved = sum(1 for entry in entries if entry.status == "moved")
        undone = sum(1 for entry in entries if entry.status == "undone")
        click.echo(f"{run_id}  {moved} moved, {undone} undone")


if __name__ == "__main__":
    cli()
