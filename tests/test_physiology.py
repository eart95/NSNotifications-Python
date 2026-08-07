"""Insulin, carbs and the momentum forecast.

The IOB curve is the one piece of arithmetic here whose exact shape matters:
it is printed on a hypo warning, and "0.4 u on board" and "2.1 u on board" lead
to different amounts of sugar. The rest is bounded-sanity checking — the
forecast is deliberately crude, and these tests pin *that* rather than pretend
it is a physiological model.
"""

from __future__ import annotations

from nsnotifier.models import Reading, Treatment
from nsnotifier.physiology import (
    CARB_ABSORPTION_MINUTES,
    INSULIN_DURATION_MINUTES,
    MOMENTUM_DECAY_MINUTES,
    carbs_entering,
    carbs_on_board,
    insulin_on_board,
    insulin_remaining_fraction,
    momentum_forecast,
)

ANCHOR = 1_770_000_000.0


def series(values, spacing=300.0):
    last = len(values) - 1
    return [Reading(at=ANCHOR + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


# --- insulin ---------------------------------------------------------------


def test_insulin_curve_starts_whole_and_ends_at_nothing():
    assert insulin_remaining_fraction(0) == 1.0
    assert insulin_remaining_fraction(INSULIN_DURATION_MINUTES) == 0.0
    assert insulin_remaining_fraction(INSULIN_DURATION_MINUTES + 60) == 0.0


def test_insulin_curve_decreases_monotonically():
    previous = 1.0
    for minutes in range(0, int(INSULIN_DURATION_MINUTES) + 1, 5):
        current = insulin_remaining_fraction(minutes)
        assert current <= previous + 1e-9, f"IOB went back up at {minutes} min"
        previous = current


def test_about_half_a_dose_remains_at_the_peak():
    # Not a magic number so much as a shape check: for the exponential model
    # with a 75-minute peak and a six-hour tail, roughly half the dose is still
    # to act at the peak. A curve that failed this would be printing badly
    # wrong numbers on a hypo warning.
    remaining = insulin_remaining_fraction(75)
    assert 0.4 < remaining < 0.7


def test_insulin_on_board_is_none_when_nothing_was_ever_logged():
    # `None` and `0.0` are different answers. A zero says "nothing on board";
    # nothing at all says "this service does not know what you have taken",
    # which is the truth for someone who does not push treatments to Nightscout.
    assert insulin_on_board([], ANCHOR) is None
    assert insulin_on_board([Treatment(at=ANCHOR - 600, carbs=30)], ANCHOR) is None
    assert insulin_on_board([Treatment(at=ANCHOR - 600, insulin=4)], ANCHOR) is not None


def test_insulin_on_board_sums_overlapping_doses():
    doses = [
        Treatment(at=ANCHOR - 30 * 60, insulin=3),
        Treatment(at=ANCHOR - 120 * 60, insulin=5),
    ]
    total = insulin_on_board(doses, ANCHOR)
    assert 0 < total < 8


def test_insulin_on_board_ignores_doses_past_their_duration():
    old = [Treatment(at=ANCHOR - (INSULIN_DURATION_MINUTES + 10) * 60, insulin=6)]
    assert insulin_on_board(old, ANCHOR) == 0.0


# --- carbs -----------------------------------------------------------------


def test_carbs_absorb_linearly_to_nothing():
    meal = [Treatment(at=ANCHOR - (CARB_ABSORPTION_MINUTES / 2) * 60, carbs=60)]
    assert carbs_on_board(meal, ANCHOR) == 30.0
    stale = [Treatment(at=ANCHOR - (CARB_ABSORPTION_MINUTES + 30) * 60, carbs=60)]
    assert carbs_on_board(stale, ANCHOR) == 0.0


def test_carbs_entering_counts_the_ramp_not_the_remainder():
    # An hour-old 60 g meal still leaves plenty on board, but nothing is
    # *arriving*, which is what a carb-rise episode is about.
    meal = [Treatment(at=ANCHOR - 60 * 60, carbs=60)]
    assert carbs_entering(meal, ANCHOR, 30 * 60) == 0
    assert carbs_on_board(meal, ANCHOR) > 0

    fresh = [Treatment(at=ANCHOR - 10 * 60, carbs=60)]
    assert carbs_entering(fresh, ANCHOR, 30 * 60) == 60


# --- forecast --------------------------------------------------------------


def test_forecast_is_empty_without_enough_history():
    assert momentum_forecast([], ANCHOR, 1800) == []
    assert momentum_forecast(series([120]), ANCHOR, 1800) == []


def test_forecast_follows_the_recent_slope():
    falling = momentum_forecast(series([140, 125, 110, 95]), ANCHOR, 1800)
    assert falling
    assert falling[-1].mgdl < 95
    rising = momentum_forecast(series([95, 110, 125, 140]), ANCHOR, 1800)
    assert rising[-1].mgdl > 140


def test_forecast_flattens_rather_than_running_away():
    # Velocity decays to zero, so the curve bends. A linear extrapolation of a
    # -3 mg/dL/min slope reaches zero in half an hour and negative numbers
    # after that, and "forecast dips to -20" is not something anyone can act on.
    fast = momentum_forecast(series([200, 160, 120, 80]), ANCHOR, 4 * 3600)
    assert min(point.mgdl for point in fast) >= 30
    tail = [point.mgdl for point in fast if point.at > ANCHOR + MOMENTUM_DECAY_MINUTES * 60]
    assert len(set(round(value, 6) for value in tail)) == 1, "the tail should be flat"
