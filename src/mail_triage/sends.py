"""What was actually sent, so that a bounce can be matched against it.

Until this existed, ``send_unsubscribe`` fired and returned ``None``: the
tool printed "sent" and kept no record at all, so there was nothing a bounce
could be checked against. A batch of ten could report a perfect score with
all ten rejected.

**A send is recorded after it succeeds, not before.** That inverts the run
journal's record-then-act discipline, deliberately. The journal records
intent first because an interrupted batch must still be reversible, and a
move that never happened is harmless to attempt to undo. Here the risk runs
the other way: a record written before the send describes a request that
might never have gone out, and the bounce check would then find no bounce
for it and report it as fine — recreating, in a new place, the exact false
clean bill of health this whole feature exists to abolish. Losing a record
to a crash between the send and the write merely returns that one request to
the old behaviour.

One file per batch, mirroring the journal's convention, so "the last batch"
is simply the newest filename.
"""

from __future__ import annotations

import json
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from mail_triage.config import Config

SENDS_DIRNAME = "unsubscribe-sends"


@dataclass(frozen=True)
class SentRequest:
    """One unsubscribe request that definitely left the machine."""

    sender: str
    to_address: str
    subject: str
    sent_at: int
    # The account Mail actually sent from, captured rather than assumed:
    # ``send_mail`` uses Mail's default account, which need not be any
    # configured source. The bounce comes back to this account's inbox, so
    # guessing here means searching the wrong mailbox and reporting a clean
    # run that never happened.
    from_account: str


def sends_dir(config: Config) -> Path:
    return config.local_dir / SENDS_DIRNAME


def new_batch_id() -> str:
    """A batch id that is both unique enough and sortable by time."""
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())


def _batch_path(config: Config, batch_id: str) -> Path:
    return sends_dir(config) / f"{batch_id}.jsonl"


def record_send(config: Config, batch_id: str, request: SentRequest) -> None:
    """Append one sent request. Call only after the send has succeeded."""
    directory = sends_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    with _batch_path(config, batch_id).open("a") as handle:
        handle.write(json.dumps(asdict(request)) + "\n")


def list_batches(config: Config) -> list[str]:
    """Batch ids, newest first. The ids sort by time, so this is a sort."""
    directory = sends_dir(config)
    if not directory.is_dir():
        return []
    return sorted((path.stem for path in directory.glob("*.jsonl")), reverse=True)


def load_batch(config: Config, batch_id: str) -> list[SentRequest]:
    """Every request in a batch, in send order.

    A line that will not parse is skipped with a warning rather than taken as
    the end of the file: only the most recent write can be truncated, and the
    entries either side of it are complete and worth keeping.
    """
    path = _batch_path(config, batch_id)
    if not path.exists():
        return []
    requests: list[SentRequest] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            requests.append(SentRequest(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            warnings.warn(f"Skipping unreadable line in send log {path.name}", stacklevel=2)
    return requests
