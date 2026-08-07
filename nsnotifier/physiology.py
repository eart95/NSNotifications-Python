"""Insulin on board, carbs on board, and a deliberately modest forecast.

A word about what this file is *not*. The app runs LoopKit's algorithm over the
user's own insulin sensitivity and carb ratio schedules and produces a real
prediction. This service does not, and should not try to: it has none of those
settings, dosing maths that is subtly wrong is dangerous in a way that a missing
notification is not, and the phone's forecast is available whenever the phone is
awake.

So the projection here is **momentum only** — where glucose goes if it carries
on doing what it is doing, with the velocity decaying to nothing over half an
hour. That is a poor predictor of an hour from now and a decent one of twenty
minutes from now, which happens to be exactly the window a hypo warning needs.
It is used for one thing: deciding *when* to warn. Everything it produces is
labelled as a forecast on the phone and never as a number to dose from.

Insulin and carbs on board are different: both are arithmetic over logged
treatments, neither needs a sensitivity factor, and both are just facts about
what has been taken. They appear on the Live Activity's face because "1.4 u
still on board" is the single most useful thing to know while deciding how much
sugar to eat.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from .models import Reading, Treatment, slope_per_minute

# LoopKit's exponential insulin model, with the parameters Loop uses for rapid
# acting adult curves. Peak and duration in minutes.
INSULIN_PEAK_MINUTES = 75.0
INSULIN_DURATION_MINUTES = 360.0

# Carbohydrate absorbs linearly over this long. Cruder than the app's dynamic
# absorption, and knowingly so: the number is shown, never dosed from.
CARB_ABSORPTION_MINUTES = 180.0

# How long the momentum forecast takes to decay to a flat line.
MOMENTUM_DECAY_MINUTES = 30.0


def insulin_remaining_fraction(minutes_since: float) -> float:
    """Fraction of a dose still to act, ``minutes_since`` after it was given.

    The closed form of LoopKit's exponential model. Written out rather than
    approximated because the shape near the peak is what decides whether a
    warning says "2.1 u on board" or "0.4 u on board", and those lead to
    different amounts of sugar.
    """
    if minutes_since <= 0:
        return 1.0
    if minutes_since >= INSULIN_DURATION_MINUTES:
        return 0.0

    td = INSULIN_DURATION_MINUTES
    tp = INSULIN_PEAK_MINUTES
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))

    t = minutes_since
    effect_remaining = 1 - s * (1 - a) * (
        ((t**2) / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
    )
    return min(1.0, max(0.0, effect_remaining))


def insulin_on_board(treatments: Sequence[Treatment], now: float) -> Optional[float]:
    """Units still acting, or ``None`` when nothing has ever been logged.

    ``None`` and ``0.0`` are different answers and the difference matters: a
    zero says "nothing on board", while nothing at all says "this service has no
    idea what you have taken", which is the truth for a user who logs insulin in
    the app but does not push treatments to Nightscout. Printing 0 u for that on
    a hypo warning would be actively misleading.
    """
    doses = [t for t in treatments if t.insulin > 0]
    if not doses:
        return None

    total = 0.0
    for dose in doses:
        minutes = (now - dose.at) / 60
        if minutes < 0 or minutes >= INSULIN_DURATION_MINUTES:
            continue
        total += dose.insulin * insulin_remaining_fraction(minutes)
    return round(total, 2)


def carbs_on_board(treatments: Sequence[Treatment], now: float) -> Optional[float]:
    """Grams still absorbing, linearly. ``None`` when nothing has been logged."""
    meals = [t for t in treatments if t.carbs > 0]
    if not meals:
        return None

    total = 0.0
    for meal in meals:
        minutes = (now - meal.at) / 60
        if minutes < 0 or minutes >= CARB_ABSORPTION_MINUTES:
            continue
        total += meal.carbs * (1 - minutes / CARB_ABSORPTION_MINUTES)
    return round(total, 1)


def carbs_entering(treatments: Sequence[Treatment], now: float, window_seconds: float) -> float:
    """Grams logged inside the window.

    This — not carbs on board — is what "carbs going up steeply" means. A 60 g
    meal still leaves 40 g on board an hour later, and an activity that reacted
    to that would start long after the interesting part was over. What matters
    is the ramp.
    """
    cutoff = now - window_seconds
    return sum(t.carbs for t in treatments if t.carbs > 0 and cutoff <= t.at <= now)


def momentum_forecast(
    readings: Sequence[Reading],
    now: float,
    horizon_seconds: float,
    step_seconds: float = 5 * 60,
) -> list[Reading]:
    """Where glucose goes if it keeps doing what it is doing.

    Velocity decays linearly to zero over ``MOMENTUM_DECAY_MINUTES``, so the
    curve bends rather than running off to nothing or to nine hundred. Returns
    an empty list when there is not enough recent data to have a velocity at
    all — an honest silence, and the episode rules treat a missing forecast as
    "no crossing", which is the safe direction for a *start* condition and is
    corrected by the measured rule the moment glucose actually goes low.
    """
    if not readings:
        return []
    latest = readings[-1]
    slope = slope_per_minute(readings)
    if slope is None:
        return []

    points: list[Reading] = []
    steps = max(1, int(horizon_seconds // step_seconds))
    for index in range(1, steps + 1):
        minutes = (index * step_seconds) / 60
        capped = min(minutes, MOMENTUM_DECAY_MINUTES)
        # Displacement of a velocity falling linearly from `slope` to zero.
        displacement = slope * (capped - (capped**2) / (2 * MOMENTUM_DECAY_MINUTES))
        if minutes > MOMENTUM_DECAY_MINUTES:
            displacement = slope * MOMENTUM_DECAY_MINUTES / 2
        projected = latest.mgdl + displacement
        # Floored: glucose does not go to zero, and a curve that says it will
        # makes a "forecast dips to 4 mg/dL" headline that no one can act on.
        points.append(Reading(at=latest.at + index * step_seconds, mgdl=max(30.0, projected)))
    return points
