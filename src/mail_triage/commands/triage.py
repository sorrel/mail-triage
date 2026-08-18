"""The main event: classify every configured inbox, then file what is approved."""

from __future__ import annotations

import click

from mail_triage.asking import ask_all, rank_uncertain
from mail_triage.commands._shared import _make_header_guard, _run_errors, _select_sources
from mail_triage.config import load_config
from mail_triage.corrections import record_overrides
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.execute import execute
from mail_triage.folders import account_prefix, match_folders
from mail_triage.journal import Journal, new_run_id
from mail_triage.mail_app import AppleScriptMail
from mail_triage.pipeline import classify_run
from mail_triage.review import (
    auto_decisions,
    render_table,
    review,
    review_held,
    review_unplaced,
    summarise,
)
from mail_triage.commands._shared import PROGRESS_LINE_WIDTH

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


@click.command()
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
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="File mail at or above auto_threshold without asking. Never bins, never "
    "touches mail a guard held back, and is still fully undoable.",
)
def triage(
    dry_run: bool, limit: int, ask: bool, source_names: tuple[str, ...], auto: bool
) -> None:
    """Classify every configured inbox, then file what you approve.

    Each source's inbox is scanned and the proposals are shown together;
    mail is filed into the filing account's folder tree whichever account
    it arrived in. Binning stays in the account the message came from.

    Nothing moves without a confirmation at the prompt, and every move is
    journalled beforehand so 'mail-triage undo' can reverse the whole run.

    With --auto there is no prompt: everything at or above auto_threshold is
    filed and everything else is left alone. It files only — it never bins,
    and never touches mail a guard held back — and the run is journalled and
    undoable exactly as an interactive one is.
    """
    if auto and dry_run:
        raise click.ClickException(
            "--auto files without asking and --dry-run moves nothing, so together "
            "they contradict each other. Use --dry-run to see what --auto would do."
        )
    if auto:
        # Asking is a conversation about senders the model cannot call, and
        # in an unattended run there is nobody there to have it.
        ask = False
    config = load_config()
    sources = _select_sources(config, source_names)
    mail = AppleScriptMail()
    guard, guard_state = _make_header_guard(mail)
    with _run_errors():
        run = classify_run(config, sources, ask=ask, guard=guard, db_path=DEFAULT_DB_PATH)
    model, rules, inputs = run.model, run.rules, run.inputs
    folders = run.folders
    classify_all = run.reclassify
    proposals = run.proposals
    if guard_state["fetches"]:
        click.echo("\r" + " " * PROGRESS_LINE_WIDTH + "\r", nl=False, err=True)

    if ask:
        proposals = _ask_about_uncertain_senders(
            proposals, model, folders, inputs.yearly_counts, config, rules, classify_all,
            inputs.billing_senders,
        )

    click.echo(render_table(proposals, {s.prefix: s.name for s in sources}))
    click.echo()
    click.echo(summarise(proposals, {s.prefix: s.name for s in sources}, config.auto_threshold))

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

    if auto:
        decisions = auto_decisions(proposals, config)
        if not decisions:
            click.echo(
                f"\nNothing was confident enough to file on its own "
                f"(auto_threshold is {config.auto_threshold:g})."
            )
            return
        click.echo(
            f"\nFiling {len(decisions)} message{'s' if len(decisions) != 1 else ''} "
            f"at {config.auto_threshold:g} confidence or above, unprompted."
        )
        eligible = sum(
            1 for item in proposals
            if item.folder is not None and item.confidence >= config.auto_threshold
        )
        if eligible > len(decisions):
            # Said out loud rather than left to be inferred from a count that
            # happens to equal the cap. A capped run is a normal outcome, but
            # a silently capped one looks like a model that suddenly went shy.
            click.echo(
                f"Capped at auto_limit ({config.auto_limit}); "
                f"{eligible - len(decisions)} left for the next run."
            )
        held_security = [item for item in proposals if item.veto_kind == "security"]
        if held_security:
            # Printed by the unattended run itself, not only by 'report'. The
            # log file is where a scheduled run's output goes, and this is the
            # line that makes reading it worthwhile.
            click.echo(
                f"\n{len(held_security)} held back as security-relevant "
                "— run 'mail-triage report' to read them."
            )
        _act_on(decisions, sources, inputs.source_folders, config, mail)
        return

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
    _act_on(decisions, sources, inputs.source_folders, config, mail)


def _act_on(decisions, sources, source_folders, config, mail) -> None:
    """Journal and carry out the accepted decisions, however they were reached.

    Shared by the interactive review and by --auto so that both go through
    the same trash check, the same journal, and the same undo instructions.
    An unattended run must be no less reversible than one somebody watched.
    """
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
