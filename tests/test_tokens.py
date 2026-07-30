"""Stage B: naive Bayes over subject and sender tokens.

Handles what stage A cannot: a sender with no history at all, and — the larger
case on the real mailbox — a sender whose *filing* history is too split to
call, where the subject line is what actually distinguishes one destination
from another.
"""

from mail_triage.corpus import TrainingExample
from mail_triage.model.tokens import TokenModel, tokenise


def example(subject, folder, sender="a@example.com", weight=1.0):
    return TrainingExample(sender=sender, domain="example.com", subject=subject,
                           folder=folder, weight=weight, year=2026)


# --- Tokenising -------------------------------------------------------------

def test_tokenise_lowercases_and_splits():
    assert "invoice" in tokenise("Your INVOICE is ready", "billing@shop.example")


def test_tokenise_includes_sender_domain_parts():
    tokens = tokenise("Hello", "billing@shop.example")
    assert "shop.example" in tokens


def test_tokenise_drops_very_short_tokens():
    assert "a" not in tokenise("a big thing", "x@y.example")


def test_tokenise_survives_a_missing_subject():
    assert tokenise("", "x@y.example") == ["y.example"]


def test_tokenise_survives_a_missing_sender():
    assert "hello" in tokenise("Hello there", "")


def test_tokenise_keeps_emoji_out_of_the_way():
    """Subjects routinely carry emoji; they must not become tokens."""
    tokens = tokenise("Nvidia's $250B deal 💰 today", "a@b.example")
    assert "💰" not in tokens
    assert "today" in tokens


# --- Predicting -------------------------------------------------------------

def test_predicts_from_subject_words():
    model = TokenModel()
    model.train(
        [example("Your invoice is ready", "finance") for _ in range(20)]
        + [example("Order dispatched today", "orders") for _ in range(20)]
    )
    prediction = model.predict("invoice attached", "new@stranger.example")
    assert prediction.folder == "finance"
    assert 0.0 < prediction.confidence <= 1.0


def test_returns_none_when_untrained():
    assert TokenModel().predict("anything", "a@b.example") is None


def test_unknown_words_do_not_crash():
    model = TokenModel()
    model.train([example("Your invoice", "finance")])
    assert model.predict("zzzz qqqq", "a@b.example") is not None


def test_round_trips_through_dict():
    model = TokenModel()
    model.train([example("Your invoice is ready", "finance") for _ in range(5)])
    restored = TokenModel.from_dict(model.to_dict())
    assert restored.predict("invoice", "a@b.example").folder == "finance"


def test_recency_weighting_reaches_the_token_counts():
    """Recency weighting still applies: a folder's token counts are weighted,
    so a word used recently counts for more than the same word used in 2015."""
    model = TokenModel()
    model.train(
        [example("gardening tips", "garden", weight=0.01)]
        + [example("gardening tips", "garden", weight=5.0)]
    )
    assert model.counts["garden"]["gardening"] == 5.01


def test_folder_weight_alone_does_not_break_a_tie():
    """The plan originally asserted the opposite — that a heavier folder should
    win when the subjects are identical. Measured on 2,568 held-out messages,
    that is wrong: precision falls monotonically as the folder-size prior is
    given more influence (77.8% at full weight, 82.0% at half, 84.8% at a
    tenth, 85.1% at none). With nothing in the subject to separate them, the
    honest answer is no answer."""
    model = TokenModel()
    model.train(
        [example("shared word here", "orders", weight=0.01) for _ in range(50)]
        + [example("shared word here", "finance", weight=5.0) for _ in range(2)]
    )
    assert model.predict("shared word here", "a@b.example") is None


def test_the_reason_names_the_tokens_that_drove_it():
    """The training data is known to be imperfect, so a decision that cannot
    explain itself is not worth acting on."""
    model = TokenModel()
    model.train([example("Your invoice is ready", "finance") for _ in range(20)])
    assert "invoice" in model.predict("invoice attached", "a@b.example").reason


def test_a_split_sender_is_separated_by_its_subject():
    """The real motivating case: one sender, two destinations, distinguishable
    only by what the subject says."""
    model = TokenModel()
    model.train(
        [example("I want to connect", "team/people", sender="n@linkedin.example")
         for _ in range(20)]
        + [example("Security alert for your account", "parent/accounts/security",
                   sender="n@linkedin.example") for _ in range(20)]
    )
    connect = model.predict("I want to connect", "n@linkedin.example")
    alert = model.predict("Security alert for your account", "n@linkedin.example")
    assert connect.folder == "team/people"
    assert alert.folder == "parent/accounts/security"


def test_confidence_is_a_probability_across_folders():
    model = TokenModel()
    model.train(
        [example("Your invoice is ready", "finance") for _ in range(20)]
        + [example("Order dispatched today", "orders") for _ in range(20)]
    )
    prediction = model.predict("invoice", "a@b.example")
    assert 0.5 < prediction.confidence <= 1.0


def test_an_ambiguous_subject_produces_no_answer_at_all():
    """Identical evidence for two folders must not read as certainty. It now
    abstains outright rather than returning a low-confidence guess."""
    model = TokenModel()
    model.train(
        [example("shared word here", "orders") for _ in range(20)]
        + [example("shared word here", "finance") for _ in range(20)]
    )
    assert model.predict("shared word here", "a@b.example") is None
    assert model.predict("shared word here", "a@b.example", margin=1.0).confidence < 0.7


def test_a_long_subject_does_not_overflow_to_zero():
    """Log-probabilities exist so a 40-token subject does not underflow."""
    model = TokenModel()
    model.train([example("alpha beta gamma", "finance") for _ in range(10)])
    prediction = model.predict(" ".join(["alpha"] * 200), "a@b.example")
    assert prediction is not None
    assert prediction.confidence > 0.0


# --- Folder size must not decide -----------------------------------------------
#
# Measured against 2,568 held-out real messages on 27 July 2026: weighting by
# folder size scored 77.8% precision (459 wrong), and dropping it scored 85.1%
# (297 wrong) — strictly better on both counts, more right *and* fewer wrong.
# The largest folder held 26.9% of all filed mail and was winning ties on bulk
# rather than on evidence.

def test_a_large_folder_does_not_win_on_size_alone():
    model = TokenModel()
    model.train(
        # "orders" is ten times the size, but says nothing about gardening.
        [example(f"order dispatched {index}", "orders") for index in range(100)]
        + [example("gardening tips for autumn", "garden") for _ in range(10)]
    )
    assert model.predict("gardening tips for autumn", "a@b.example").folder == "garden"


def test_evidence_still_decides_between_folders():
    model = TokenModel()
    model.train(
        [example("order dispatched today", "orders") for _ in range(10)]
        + [example("gardening tips for autumn", "garden") for _ in range(10)]
    )
    assert model.predict("order dispatched", "a@b.example").folder == "orders"


# --- Abstaining when the runner-up is close -------------------------------------

def test_a_close_runner_up_makes_the_model_abstain():
    """Naive Bayes probabilities are badly calibrated — the independence
    assumption pushes them to extremes — so the gap to the runner-up is a
    better guide than the headline confidence."""
    model = TokenModel()
    model.train(
        [example("newsletter weekly update", "news") for _ in range(20)]
        + [example("newsletter weekly digest", "reading") for _ in range(20)]
    )
    assert model.predict("newsletter weekly", "a@b.example") is None


def test_a_clear_winner_is_still_returned():
    model = TokenModel()
    model.train(
        [example("gardening tips for autumn", "garden") for _ in range(20)]
        + [example("order dispatched today", "orders") for _ in range(20)]
    )
    assert model.predict("gardening tips for autumn", "a@b.example").folder == "garden"


def test_the_margin_is_configurable():
    model = TokenModel()
    model.train(
        [example("newsletter weekly update", "news") for _ in range(20)]
        + [example("newsletter weekly digest", "reading") for _ in range(20)]
    )
    # With no margin required, the same ambiguous subject does produce a guess.
    assert model.predict("newsletter weekly", "a@b.example", margin=1.0) is not None
