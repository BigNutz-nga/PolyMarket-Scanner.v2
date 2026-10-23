import pytest

from fees import FeeSchedule, hold_to_settlement, round_trip


def test_round_trip_matches_unrounded_fee_formula() -> None:
    exact = FeeSchedule(rounding="none")
    # 50c -> 53c: 3c gross minus 1.75c + 1.7437c in fees.
    assert round_trip(0.50, 0.53, 1, exact)["net"] == pytest.approx(-0.004937, abs=1e-6)
    # 50c -> 55c: 5c gross minus 1.75c + 1.7325c in fees.
    assert round_trip(0.50, 0.55, 1, exact)["net"] == pytest.approx(0.015175, abs=1e-6)


def test_cent_rounding_rounds_each_order_up() -> None:
    fees = FeeSchedule()
    assert fees.taker_fee(0.50) == 0.02
    assert fees.taker_fee(0.50, 100) == 1.75
    assert round_trip(0.50, 0.53)["net"] == pytest.approx(-0.01)
    assert round_trip(0.50, 0.55)["net"] == pytest.approx(0.01)


def test_maker_fraction_and_multiplier() -> None:
    assert FeeSchedule().maker_fee(0.50, 100) == 0.0
    assert FeeSchedule(fee_type="quadratic_with_maker_fees").maker_fee(0.50, 100) == pytest.approx(0.44)
    assert FeeSchedule(multiplier=0.0).taker_fee(0.50, 10) == 0.0
    assert FeeSchedule(multiplier=2.0, rounding="none").taker_fee(0.50, 1) == pytest.approx(0.035)


def test_hold_to_settlement_pays_only_the_entry_fee() -> None:
    assert hold_to_settlement(0.40, True)["net"] == pytest.approx(0.58)
    assert hold_to_settlement(0.40, False)["net"] == pytest.approx(-0.42)


def test_multi_level_fee_rounds_per_level() -> None:
    fees = FeeSchedule()
    assert fees.taker_fee_for_levels([(0.50, 1), (0.51, 1)]) == pytest.approx(0.04)


def test_unmodeled_fee_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        FeeSchedule(fee_type="flat")
