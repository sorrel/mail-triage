"""Telling it where mail goes, and what it must never file unattended."""

from __future__ import annotations

import click

from mail_triage.commands._shared import _snapshot
from mail_triage.config import load_config
from mail_triage.corpus import build_corpus
from mail_triage.corrections import load_corrections
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.model.store import load_model, save_model, train_from_history
from mail_triage.never_personal import (
    NeverPersonalError,
    declare_never_personal,
    forget_never_personal as forget_never_personal_sender,
    load_never_personal,
)
from mail_triage.rules import RulesError, forget_rule, load_rules
from mail_triage.security import (
    SecuritySendersError,
    declare_security_sender,
    forget_security_sender,
    load_security_senders,
    security_reason,
)

# How many drift entries the `learn` report lists before summarising the rest.
DRIFT_REPORT_LIMIT = 20

@click.command()
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
            for entry in entries[:DRIFT_REPORT_LIMIT]:
                click.echo(f"  {entry.key}: '{entry.old_folder}' → '{entry.new_folder}' (by {entry.switch_year})")
            if len(entries) > DRIFT_REPORT_LIMIT:
                click.echo(f"  ... and {len(entries) - DRIFT_REPORT_LIMIT} more")


@click.command()
@click.option("--forget", default=None, metavar="SENDER", help="Remove one sender's rule.")
@click.option(
    "--never-personal",
    default=None,
    metavar="SENDER",
    help="Vouch that this sender never awaits a reply, so the reply guard "
    "stops holding their mail back. Flagging still wins.",
)
@click.option(
    "--forget-never-personal",
    default=None,
    metavar="SENDER",
    help="Withdraw a --never-personal declaration.",
)
def rules(forget: str | None, never_personal: str | None, forget_never_personal: str | None) -> None:
    """List the answers you have given about where senders' mail goes."""
    config = load_config()
    if never_personal is not None:
        try:
            changed = declare_never_personal(config.never_personal_path, never_personal)
        except NeverPersonalError as error:
            raise click.ClickException(str(error)) from error
        click.echo(
            f"{never_personal} will no longer be held back for a reply."
            if changed
            else f"{never_personal} was already declared never-personal."
        )
        return
    if forget_never_personal is not None:
        try:
            removed = forget_never_personal_sender(
                config.never_personal_path, forget_never_personal
            )
        except NeverPersonalError as error:
            raise click.ClickException(str(error)) from error
        if not removed:
            raise click.ClickException(f"{forget_never_personal} was not declared never-personal.")
        click.echo(f"{forget_never_personal} may be held back for a reply again.")
        return
    if forget is not None:
        if not forget_rule(config.rules_path, forget):
            raise click.ClickException(f"No rule for {forget}.")
        click.echo(f"Forgot the rule for {forget}.")
        return
    try:
        known = load_rules(config.rules_path)
        vouched = load_never_personal(config.never_personal_path)
    except (RulesError, NeverPersonalError) as error:
        raise click.ClickException(str(error)) from error
    if not known and not vouched:
        click.echo("No rules yet. 'mail-triage triage' will ask about uncertain senders.")
        return
    for sender, rule in sorted(known.items()):
        target = {"file": rule.folder, "bin": "(delete)"}.get(rule.action, "(left alone)")
        click.echo(f"{sender:<44}{target}")
    if known:
        click.echo(f"\n{len(known)} rules. Remove one with: mail-triage rules --forget <sender>")
    if vouched:
        # Listed apart because it answers a different question: not where the
        # mail goes, but whether it might be waiting on a reply.
        click.echo("\nNever personal — not held back for a reply:")
        for sender in sorted(vouched):
            click.echo(f"  {sender}")
        click.echo(
            "\nWithdraw one with: mail-triage rules --forget-never-personal <sender>"
        )


@click.command()
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


@click.command()
@click.option(
    "--add",
    default=None,
    metavar="SENDER|DOMAIN",
    help="Declare an address or domain security-relevant: its mail is never "
    "filed by an unattended run. A domain covers its subdomains.",
)
@click.option(
    "--forget",
    default=None,
    metavar="SENDER|DOMAIN",
    help="Withdraw a declaration.",
)
@click.option(
    "--measure",
    is_flag=True,
    default=False,
    help="Report what share of your filing history the subject vocabulary "
    "would hold back. Read-only; moves nothing.",
)
def security(add: str | None, forget: str | None, measure: bool) -> None:
    """Mail that must never be filed by a run nobody watched.

    Auto mode files only what it is sure about, and it is surest about
    exactly the senders that matter here — high-volume, consistent alert
    mail with a long filing history. A breach notice reads 0.97 and files
    itself. This guard holds such mail in the inbox instead, whatever the
    confidence and whatever rule names the sender.

    Two layers: a subject vocabulary that always applies, and the senders
    you declare here for mail whose subjects give nothing away.
    """
    config = load_config()
    if add is not None:
        try:
            changed = declare_security_sender(config.security_senders_path, add)
        except SecuritySendersError as error:
            raise click.ClickException(str(error)) from error
        click.echo(
            f"{add} is security-relevant — its mail will not be filed unattended."
            if changed
            else f"{add} was already declared security-relevant."
        )
        return
    if forget is not None:
        try:
            removed = forget_security_sender(config.security_senders_path, forget)
        except SecuritySendersError as error:
            raise click.ClickException(str(error)) from error
        if not removed:
            raise click.ClickException(f"{forget} was not declared security-relevant.")
        click.echo(f"{forget} may be filed unattended again.")
        return
    if measure:
        _measure_security_vocabulary(config)
        return
    try:
        declared = load_security_senders(config.security_senders_path)
    except SecuritySendersError as error:
        raise click.ClickException(str(error)) from error
    if not declared:
        click.echo(
            "No senders declared. The subject vocabulary still applies to every "
            "message — run 'mail-triage security --measure' to see its reach."
        )
        return
    for entry in sorted(declared):
        click.echo(f"  {entry}")
    click.echo(
        f"\n{len(declared)} declared. Withdraw one with: "
        "mail-triage security --forget <sender>"
    )


def _measure_security_vocabulary(config) -> None:
    """How much of the real corpus the guard would hold back.

    The conventions here say measure rather than argue, and this guard is
    written deliberately broadly — the asymmetry licenses it, since a false
    positive costs one message filed by hand. What the asymmetry does *not*
    license is a guard so broad that auto mode files nothing, and that is a
    question about this mailbox rather than about the vocabulary. So: run it
    over the filing history and print the share.
    """
    # The training corpus rather than the raw database: it is already
    # filtered to messages that represent a real filing decision, which is
    # exactly the population auto mode would act on. Measuring against every
    # message ever received would flatter the guard by diluting it with mail
    # nobody files.
    with _snapshot() as reader:
        history = build_corpus(reader.all_messages(), config)
    if not history:
        click.echo("No filing history to measure against.")
        return
    declared = load_security_senders(config.security_senders_path)
    held = [
        example for example in history
        if security_reason(example.sender, example.subject, declared) is not None
    ]
    share = len(held) / len(history)
    click.echo(
        f"{len(held)} of {len(history)} filed messages ({share:.1%}) would be "
        "held back as security-relevant."
    )
    if share > 0.10:
        click.echo(
            "\nThat is high enough to make auto mode do very little. Consider "
            "cutting the vocabulary in security.py — the terms are listed there "
            "with the reasoning for each."
        )
    reasons: dict[str, int] = {}
    for example in held:
        reason = security_reason(example.sender, example.subject, declared)
        reasons[reason or ""] = reasons.get(reason or "", 0) + 1
    click.echo("\nWhat fired, commonest first:")
    for reason, count in sorted(reasons.items(), key=lambda pair: -pair[1])[:15]:
        click.echo(f"  {count:>5}  {reason}")
