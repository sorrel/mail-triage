"""Stage A: file mail by who sent it.

Sender address first, then sender domain. Most mail is decided here — a
newsletter or a shop always goes to the same place. Confidence is the weighted
share of the winning folder, so a sender whose mail is scattered across folders
produces a low score and, above in the pipeline, no proposal at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from mail_triage.corpus import TrainingExample

# A single observation should not read as certainty. This pseudo-count damps
# confidence for thinly-evidenced senders: one sighting gives 1/(1+1) = 0.5.
PRIOR_STRENGTH = 1.0


@dataclass(frozen=True)
class Prediction:
    """A stage's answer, with a human-readable justification."""

    folder: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class DriftEntry:
    """A key whose destination folder changed over time."""

    key: str
    old_folder: str
    new_folder: str
    switch_year: int


class SenderModel:
    """Weighted folder counts per sender address and per sender domain."""

    def __init__(self) -> None:
        self.by_sender: dict[str, dict[str, float]] = {}
        self.by_domain: dict[str, dict[str, float]] = {}
        self._per_year: dict[str, dict[int, dict[str, float]]] = {}
        # Restored drift entries, used only when _per_year is empty (i.e. the
        # model was loaded from disk rather than freshly trained). See
        # to_dict/from_dict: we persist the derived entries, not the raw
        # per-year counts, to keep the model file small.
        self._drift_entries: list[DriftEntry] = []

    def train(self, examples: list[TrainingExample]) -> None:
        senders: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        domains: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for item in examples:
            if item.sender:
                senders[item.sender][item.folder] += item.weight
            if item.domain:
                domains[item.domain][item.folder] += item.weight
        self.by_sender = {key: dict(value) for key, value in senders.items()}
        self.by_domain = {key: dict(value) for key, value in domains.items()}

    @staticmethod
    def _best(counts: dict[str, float]) -> tuple[str, float, float]:
        """Return (folder, share, total) for the highest-weighted folder."""
        total = sum(counts.values())
        folder, weight = max(counts.items(), key=lambda item: item[1])
        share = weight / (total + PRIOR_STRENGTH) if total else 0.0
        return folder, share, total

    def predict(self, sender: str, domain: str) -> Prediction | None:
        counts = self.by_sender.get(sender)
        if counts:
            folder, share, total = self._best(counts)
            return Prediction(
                folder=folder,
                confidence=share,
                reason=f"sender {sender} filed to '{folder}' ({total:.1f} weighted sightings)",
            )
        counts = self.by_domain.get(domain)
        if counts:
            folder, share, total = self._best(counts)
            return Prediction(
                folder=folder,
                confidence=share,
                reason=f"sender domain {domain} filed to '{folder}' ({total:.1f} weighted sightings)",
            )
        return None

    def to_dict(self) -> dict:
        return {
            "by_sender": self.by_sender,
            "by_domain": self.by_domain,
            "drift_entries": [
                {
                    "key": entry.key,
                    "old_folder": entry.old_folder,
                    "new_folder": entry.new_folder,
                    "switch_year": entry.switch_year,
                }
                for entry in self.drift_report()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> SenderModel:
        model = cls()
        model.by_sender = data.get("by_sender", {})
        model.by_domain = data.get("by_domain", {})
        model._drift_entries = [
            DriftEntry(**entry) for entry in data.get("drift_entries", [])
        ]
        return model

    def train_drift(self, examples: list[TrainingExample]) -> None:
        """Record, per sender, the dominant folder in each year."""
        per_year: dict[str, dict[int, dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float))
        )
        for item in examples:
            if item.sender:
                per_year[item.sender][item.year][item.folder] += 1.0
        self._per_year = {
            sender: {year: dict(folders) for year, folders in years.items()}
            for sender, years in per_year.items()
        }

    def drift_report(self) -> list[DriftEntry]:
        """Senders whose dominant folder changed between their first and last year.

        Computed from ``_per_year`` when available (a freshly trained model).
        Otherwise falls back to ``_drift_entries``, the derived entries
        restored by ``from_dict`` — the raw per-year counts are not persisted.
        """
        if not self._per_year:
            return list(self._drift_entries)
        entries: list[DriftEntry] = []
        for sender, years in self._per_year.items():
            if len(years) < 2:
                continue
            ordered = sorted(years.items())
            first_year, first_folders = ordered[0]
            last_year, last_folders = ordered[-1]
            old = max(first_folders.items(), key=lambda item: item[1])[0]
            new = max(last_folders.items(), key=lambda item: item[1])[0]
            if old != new:
                entries.append(DriftEntry(key=sender, old_folder=old, new_folder=new,
                                          switch_year=last_year))
        return sorted(entries, key=lambda entry: entry.key)
