"""Persist the trained model to the gitignored local area."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mail_triage.config import Config
from mail_triage.corpus import build_corpus
from mail_triage.corrections import corrections_as_examples, load_corrections
from mail_triage.envelope import EnvelopeReader, snapshot_database
from mail_triage.model.sender import SenderModel
from mail_triage.model.tokens import TokenModel

MODEL_VERSION = 2


@dataclass
class TrainedModel:
    sender: SenderModel
    trained_at: int
    example_count: int
    # Stage B. Optional so a Classifier can be built without one in tests and
    # so older callers keep working; when absent, stage B simply never fires.
    tokens: TokenModel | None = None


def save_model(model: TrainedModel, path: Path) -> None:
    """Write the model atomically so a crash mid-write cannot corrupt a good model.

    The payload is serialised and written to a temporary file in the same
    directory as ``path``, then moved into place with ``os.replace``, which is
    atomic on the same filesystem (a cross-filesystem rename would not be).
    If serialisation or the write fails, the temporary file is removed and the
    existing model at ``path``, if any, is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MODEL_VERSION,
        "trained_at": model.trained_at,
        "example_count": model.example_count,
        "sender": model.sender.to_dict(),
        "tokens": model.tokens.to_dict() if model.tokens is not None else None,
    }
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def load_model(path: Path) -> TrainedModel:
    if not path.exists():
        raise FileNotFoundError(f"No model at {path}. Run 'mail-triage learn' first.")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Model at {path} is corrupt (invalid JSON: {error}). "
            "Run 'mail-triage learn' to rebuild it."
        ) from error
    if payload.get("version") != MODEL_VERSION:
        raise ValueError(
            f"Model at {path} is version {payload.get('version')}, expected {MODEL_VERSION}. "
            "Run 'mail-triage learn' to rebuild it."
        )
    try:
        token_data = payload.get("tokens")
        return TrainedModel(
            sender=SenderModel.from_dict(payload["sender"]),
            trained_at=payload["trained_at"],
            example_count=payload["example_count"],
            tokens=TokenModel.from_dict(token_data) if token_data else None,
        )
    except KeyError as error:
        raise ValueError(
            f"Model at {path} is missing expected field {error}. "
            "Run 'mail-triage learn' to rebuild it."
        ) from error


def train_from_history(config: Config, db_path: Path) -> TrainedModel:
    """Snapshot the database, build the corpus, and train.

    Corrections join the corpus at ``correction_weight`` times the weight of a
    historical filing, which is how a changed mind overrides an old habit
    without re-filing thousands of past messages by hand.
    """
    with tempfile.TemporaryDirectory() as work:
        snapshot = snapshot_database(db_path, Path(work))
        reader = EnvelopeReader(snapshot)
        try:
            examples = build_corpus(reader.all_messages(), config)
        finally:
            reader.close()
    examples.extend(corrections_as_examples(load_corrections(config), config))
    sender_model = SenderModel()
    sender_model.train(examples)
    sender_model.train_drift(examples)
    token_model = TokenModel()
    token_model.train(examples)
    return TrainedModel(
        sender=sender_model, trained_at=int(time.time()), example_count=len(examples),
        tokens=token_model,
    )
