"""The browser interface, on the loopback interface only."""

from __future__ import annotations

import click
import secrets

from mail_triage.commands._shared import _make_header_guard, _run_errors, _select_sources
from mail_triage.commands.unsubscribe import _unsubscribe_candidates
from mail_triage.config import load_config
from mail_triage.envelope import DEFAULT_DB_PATH
from mail_triage.mail_app import AppleScriptMail
from mail_triage.pipeline import classify_run
from mail_triage.web.routes import Router
from mail_triage.web.server import serve
from mail_triage.web.session import Session
from pathlib import Path
from mail_triage.commands._shared import PROGRESS_LINE_WIDTH

@click.command()
@click.option("--port", default=8765, help="Port to listen on. Loopback only.")
@click.option(
    "--open/--no-open", "open_browser", default=True,
    help="Open your browser. --no-open prints the URL instead.",
)
@click.option(
    "--source", "source_names", multiple=True,
    help="Triage only these sources, by name. Repeatable. Default: all of them.",
)
def web(port: int, open_browser: bool, source_names: tuple[str, ...]) -> None:
    """Triage in a browser, on 127.0.0.1 only.

    Runs one triage pass — same snapshot, same classifier, same guards as the
    terminal — and serves the proposals to a page you click through. Nothing
    moves until you press Apply, and everything that moves is journalled and
    undoable exactly as a terminal run is.

    The server is reachable only from this machine. The URL carries a
    one-time token; the page trades it for a header token and drops it from
    the address bar. The server stops on Ctrl-C, or after 30 minutes idle.
    """
    config = load_config()
    sources = _select_sources(config, source_names)
    mail = AppleScriptMail()
    guard, guard_state = _make_header_guard(mail)
    with _run_errors():
        run = classify_run(config, sources, guard=guard, db_path=DEFAULT_DB_PATH)
    inputs = run.inputs
    proposals = run.proposals
    if guard_state["fetches"]:
        click.echo("\r" + " " * PROGRESS_LINE_WIDTH + "\r", nl=False, err=True)

    router = Router(
        session=Session(proposals),
        config=config,
        mail=mail,
        accounts={source.prefix: source.name for source in sources},
        static_dir=Path(__file__).parent / "web" / "static",
        token=secrets.token_urlsafe(32),
        port=port,
        folders=inputs.folders,
        unsubscribe_source=lambda: _unsubscribe_candidates(config, mail),
    )

    def ready(url: str, actual_port: int) -> None:
        click.echo(f"Serving {len(proposals)} messages on http://127.0.0.1:{actual_port}")
        if open_browser:
            click.echo("Opening your browser…   (q there, or ctrl-C here, to stop)")
        else:
            click.echo(f"Open this yourself (the token works once):\n  {url}")

    serve(router, port=port, open_browser=open_browser, on_ready=ready)
    click.echo("Stopped.")
