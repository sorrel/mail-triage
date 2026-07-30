"""Stage B: a weighted multinomial naive Bayes over subject and sender tokens.

Hand-rolled rather than pulled from scikit-learn: it is a page of arithmetic,
adds no install burden, and can name the tokens that drove a decision — which
matters when the training data is known to be imperfect.

**What it is for.** Stage A files by who sent a message, and on the real
mailbox that leaves two gaps. A sender with no history at all is the obvious
one. The larger one, measured 27 July 2026, is a sender whose filing history is
*split*: 31 of 65 inbox messages stayed put for that reason alone. For those,
the subject line is the thing that actually separates one destination from
another — "I want to connect" against "Security alert for your account", from
the same address.

That is also why subject-*pattern* rules were not built. The distinction is
learnable from filing history already recorded; asking the user to write and
maintain patterns by hand would be work they have effectively already done.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import Prediction

MIN_TOKEN_LENGTH = 2
SMOOTHING = 1.0

# How many times more likely the winning folder must be than the runner-up
# before stage B will commit. Naive Bayes probabilities are badly calibrated —
# the independence assumption drives them to 0.96 on thin evidence — so the gap
# to the second-placed folder is a better guide than the headline confidence.
# Measured on 2,568 held-out messages: a 10x margin raised precision from 85.1%
# to 87.1%, trading 112 correct filings to avoid 63 misfilings.
DEFAULT_MARGIN = 10.0

# Words, numbers, and the punctuation that appears inside domains. Anything
# else — emoji especially, which subjects are full of — is a separator rather
# than a token.
_WORD = re.compile(r"[a-z0-9.\-_]+")


def tokenise(subject: str, sender: str) -> list[str]:
    """Lower-case word tokens from the subject, plus the sender's domain.

    The domain is included so the model can still lean on who sent something
    when the subject is uninformative, without needing stage A's per-address
    counts.
    """
    tokens = [
        word for word in _WORD.findall((subject or "").casefold())
        if len(word) >= MIN_TOKEN_LENGTH
    ]
    if "@" in (sender or ""):
        tokens.append(sender.casefold().split("@", 1)[1])
    return tokens


class TokenModel:
    """Weighted token counts per folder, plus folder priors."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, float]] = {}
        self.folder_totals: dict[str, float] = {}
        self.priors: dict[str, float] = {}
        self.vocabulary_size: int = 0

    def train(self, examples: list[TrainingExample]) -> None:
        counts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        priors: dict[str, float] = defaultdict(float)
        vocabulary: set[str] = set()
        for item in examples:
            priors[item.folder] += item.weight
            for token in tokenise(item.subject, item.sender):
                counts[item.folder][token] += item.weight
                vocabulary.add(token)
        self.counts = {folder: dict(tokens) for folder, tokens in counts.items()}
        self.folder_totals = {
            folder: sum(tokens.values()) for folder, tokens in self.counts.items()
        }
        self.priors = dict(priors)
        self.vocabulary_size = len(vocabulary)

    def predict(
        self, subject: str, sender: str, margin: float = DEFAULT_MARGIN
    ) -> Prediction | None:
        """Most likely folder for this subject, or ``None`` if it cannot tell.

        Scores are accumulated as log-probabilities so that a long subject
        cannot underflow to zero, then converted back to a normalised
        probability for comparison against the confidence threshold.

        **Folder size is deliberately not a factor.** A conventional naive
        Bayes multiplies in each class's prior probability, but filing history
        is wildly imbalanced — the largest folder holds 26.9% of everything —
        and that made big folders win ties on bulk rather than on evidence.
        Measured on 2,568 held-out real messages, dropping the size prior was
        strictly better on both counts: 1,698 right and 297 wrong, against
        1,607 right and 459 wrong. Every folder is treated as equally likely
        before the subject is read.
        """
        if not self.priors:
            return None
        tokens = tokenise(subject, sender)
        scores: dict[str, float] = {}
        for folder in self.priors:
            score = 0.0
            total = self.folder_totals.get(folder, 0.0)
            denominator = total + SMOOTHING * max(self.vocabulary_size, 1)
            for token in tokens:
                occurrences = self.counts.get(folder, {}).get(token, 0.0)
                score += math.log((occurrences + SMOOTHING) / denominator)
            scores[folder] = score
        best_folder = max(scores, key=scores.get)
        # Shift by the highest score before exponentiating: the raw values are
        # large negatives, and exp() of them underflows to zero.
        highest = scores[best_folder]
        exponentials = {folder: math.exp(score - highest) for folder, score in scores.items()}
        confidence = exponentials[best_folder] / sum(exponentials.values())
        ranked = sorted(exponentials.values(), reverse=True)
        if len(ranked) > 1 and ranked[1] > 0 and ranked[0] / ranked[1] < margin:
            # Two folders fit nearly as well. Leaving the message in the inbox
            # is the safe outcome here, not a failure.
            return None
        contributing = ", ".join(
            token for token in tokens if self.counts.get(best_folder, {}).get(token)
        )[:80]
        return Prediction(
            folder=best_folder,
            confidence=confidence,
            reason=f"subject tokens ({contributing or 'prior only'}) suggest '{best_folder}'",
        )

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "folder_totals": self.folder_totals,
            "priors": self.priors,
            "vocabulary_size": self.vocabulary_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenModel:
        model = cls()
        model.counts = data.get("counts", {})
        model.folder_totals = data.get("folder_totals", {})
        model.priors = data.get("priors", {})
        model.vocabulary_size = data.get("vocabulary_size", 0)
        return model
