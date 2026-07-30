import pytest

from mail_triage.corpus import TrainingExample
from mail_triage.model.sender import DriftEntry, SenderModel


def example(sender, folder, weight=1.0, subject="Subject", year=2024):
    domain = sender.split("@", 1)[1]
    return TrainingExample(sender=sender, domain=domain, subject=subject,
                           folder=folder, weight=weight, year=year)


def test_consistent_sender_predicts_confidently():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders") for _ in range(10)])
    prediction = model.predict("orders@shop.example", "shop.example")
    assert prediction.folder == "orders"
    # Ten unanimous sightings, damped by the prior: 10 / (10 + 1).
    assert prediction.confidence == pytest.approx(10 / 11)
    assert "orders@shop.example" in prediction.reason


def test_split_sender_yields_low_confidence():
    model = SenderModel()
    model.train(
        [example("mixed@shop.example", "orders") for _ in range(11)]
        + [example("mixed@shop.example", "finance") for _ in range(9)]
    )
    prediction = model.predict("mixed@shop.example", "shop.example")
    assert prediction.folder == "orders"
    assert 0.5 < prediction.confidence < 0.6


def test_falls_back_to_domain_for_unknown_sender():
    model = SenderModel()
    model.train([example(f"user{i}@shop.example", "orders") for i in range(5)])
    prediction = model.predict("brand-new@shop.example", "shop.example")
    assert prediction.folder == "orders"
    assert "domain" in prediction.reason


def test_unknown_sender_and_domain_returns_none():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders")])
    assert model.predict("nobody@elsewhere.example", "elsewhere.example") is None


def test_recent_evidence_outweighs_old():
    model = SenderModel()
    model.train(
        [example("drift@shop.example", "home tech", weight=0.05) for _ in range(20)]
        + [example("drift@shop.example", "security & tech", weight=1.0) for _ in range(3)]
    )
    prediction = model.predict("drift@shop.example", "shop.example")
    assert prediction.folder == "security & tech"


def test_round_trips_through_dict():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders")])
    restored = SenderModel.from_dict(model.to_dict())
    assert restored.predict("orders@shop.example", "shop.example").folder == "orders"


def test_single_observation_is_not_fully_confident():
    model = SenderModel()
    model.train([example("once@shop.example", "orders")])
    prediction = model.predict("once@shop.example", "shop.example")
    assert prediction.confidence < 1.0


def test_confidence_thresholds_need_concrete_evidence():
    # Damping means confidence approaches 1.0 but never reaches it, so the
    # pipeline's thresholds translate into concrete weight requirements:
    # share = w / (w + PRIOR_STRENGTH) with PRIOR_STRENGTH = 1.0, so clearing
    # the 0.7 propose-threshold needs total weight >= 7/3 ~= 2.34, and
    # clearing the 0.9 auto-threshold needs total weight >= 9. Weight is
    # recency-decayed (see corpus.recency_weight), so "weight >= 9" means
    # roughly nine *recent-equivalent* sightings, not nine messages of any
    # age - old mail counts for less.
    model = SenderModel()
    model.train([example("propose@shop.example", "orders", weight=2.34)])
    prediction = model.predict("propose@shop.example", "shop.example")
    assert prediction.confidence >= 0.7

    model = SenderModel()
    model.train([example("almost@shop.example", "orders", weight=2.3)])
    prediction = model.predict("almost@shop.example", "shop.example")
    assert prediction.confidence < 0.7

    model = SenderModel()
    model.train([example("auto@shop.example", "orders", weight=9.0)])
    prediction = model.predict("auto@shop.example", "shop.example")
    assert prediction.confidence >= 0.9

    model = SenderModel()
    model.train([example("almost-auto@shop.example", "orders", weight=8.9)])
    prediction = model.predict("almost-auto@shop.example", "shop.example")
    assert prediction.confidence < 0.9


def test_drift_report_flags_sender_whose_folder_changed():
    model = SenderModel()
    examples = (
        [example("drift@shop.example", "home tech", year=2023) for _ in range(4)]
        + [example("drift@shop.example", "security & tech", year=2026) for _ in range(4)]
    )
    model.train_drift(examples)
    report = model.drift_report()
    assert report == [
        DriftEntry(key="drift@shop.example", old_folder="home tech",
                   new_folder="security & tech", switch_year=2026)
    ]


def test_drift_report_omits_stable_sender():
    model = SenderModel()
    examples = (
        [example("steady@shop.example", "orders", year=2023) for _ in range(4)]
        + [example("steady@shop.example", "orders", year=2026) for _ in range(4)]
    )
    model.train_drift(examples)
    assert model.drift_report() == []


def test_drift_report_ignores_sender_seen_in_one_year_only():
    model = SenderModel()
    model.train_drift([example("onceyear@shop.example", "orders", year=2024)])
    assert model.drift_report() == []


def test_drift_report_survives_round_trip_through_dict():
    model = SenderModel()
    examples = (
        [example("drift@shop.example", "home tech", year=2023) for _ in range(4)]
        + [example("drift@shop.example", "security & tech", year=2026) for _ in range(4)]
    )
    model.train_drift(examples)
    original_report = model.drift_report()

    restored = SenderModel.from_dict(model.to_dict())

    assert restored.drift_report() == original_report
    assert original_report == [
        DriftEntry(key="drift@shop.example", old_folder="home tech",
                   new_folder="security & tech", switch_year=2026)
    ]


def test_round_trip_without_drift_training_returns_empty_report():
    model = SenderModel()
    model.train([example("orders@shop.example", "orders")])
    restored = SenderModel.from_dict(model.to_dict())
    assert restored.drift_report() == []
