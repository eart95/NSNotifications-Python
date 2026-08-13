"""Building the Live Activity content state, mirroring MDIKit.

Two Swift files are mirrored here: ``GlucoseActivityState.make`` and
``GlucoseActivityCopy``. The state is what APNs carries; the copy is the words
on its face.

The rule that shapes the copy is worth restating because it looks like an
omission: **no clock times, ever.** "around 14:20" cannot be produced correctly
by a server that does not know the phone's locale, its 12- or 24-hour
preference, or its time zone, and being an hour out on a low warning is not a
cosmetic failure. The state carries ``eventAt`` and ``endsAt`` as instants and
the widget renders them with the system's own formatting. Numbers are safe —
``Unit.label`` is arithmetic, and the Swift implementation is the same two
lines.

The one structural thing to know: **the episode kind is in the content state,
not the attributes.** ActivityKit freezes attributes at creation, so a card
whose kind lived there could only change kind by ending and being started
again. The attributes now identify the *session* — one card — and the kind is
one more field that a push can change. That is what makes "the type changes" a
single update push rather than an end, a push-to-start and a token round trip
through a phone that may be in a pocket.

``tests/test_activity.py`` pins the wire keys and the exact strings.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .episodes import predicted_crossing, predicted_peak
from .models import (
    Direction,
    EndReason,
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    Reading,
    Session,
    TrendDirection,
    Unit,
    slope_per_minute,
    trend_from,
)

SCHEMA = 3
MAX_SPARK_POINTS = 16
SPARK_WINDOW_SECONDS = 2 * 3600
# The literal string an APNs start payload's `attributes-type` must carry.
ATTRIBUTES_TYPE = "GlucoseActivityAttributes"


# --- copy ------------------------------------------------------------------


def _capitalised_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _units(value: float) -> str:
    return f"{value:.1f}"


def _grams(value: float) -> str:
    return str(int(round(value)))


def copy_for(
    episode: Episode,
    mgdl: float,
    trend: Optional[TrendDirection],
    event: Optional[Reading],
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
    unit: Unit,
    configuration: EpisodeConfiguration,
) -> tuple[str, str]:
    """(headline, detail), byte-identical to ``GlucoseActivityCopy.make``."""
    if episode.kind is EpisodeKind.LOW:
        return _low_copy(mgdl, trend, event, insulin_on_board, unit, configuration)
    if episode.kind is EpisodeKind.VARIATION:
        return _variation_copy(
            episode.direction or Direction.RISING, event, carbs_on_board, insulin_on_board, unit
        )
    if episode.kind is EpisodeKind.MEAL:
        return _meal_copy(event, carbs_on_board, insulin_on_board, unit)
    return _manual_copy(trend, carbs_on_board, insulin_on_board)


def _low_copy(
    mgdl: float,
    trend: Optional[TrendDirection],
    event: Optional[Reading],
    insulin_on_board: Optional[float],
    unit: Unit,
    configuration: EpisodeConfiguration,
) -> tuple[str, str]:
    parts: list[str] = []
    below = mgdl < configuration.low_entry_level

    if below:
        if trend is not None:
            parts.append(_capitalised_first(trend.spoken_movement))
    elif event is not None:
        parts.append(f"Forecast dips to {unit.label(event.mgdl)} {unit.suffix}")
    elif trend is not None:
        parts.append(_capitalised_first(trend.spoken_movement))

    if insulin_on_board is not None and insulin_on_board > 0.05:
        parts.append(f"{_units(insulin_on_board)} u on board")

    # The card stays for a quarter of an hour after glucose comes back, and
    # saying "Low glucose" over 90 for all of it is how a user learns to
    # distrust the headline.
    headline = "Low glucose" if below else "Coming back up"
    return headline, " · ".join(parts) if parts else "Watch it."


def _variation_copy(
    direction: Direction,
    event: Optional[Reading],
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
    unit: Unit,
) -> tuple[str, str]:
    parts: list[str] = []

    if event is not None:
        parts.append(
            f"Forecast dips to {unit.label(event.mgdl)} {unit.suffix}"
            if direction is Direction.FALLING
            else f"Peak near {unit.label(event.mgdl)} {unit.suffix}"
        )
    if carbs_on_board is not None and carbs_on_board > 0.5:
        parts.append(f"{_grams(carbs_on_board)} g absorbing")
    if insulin_on_board is not None and insulin_on_board > 0.05:
        parts.append(f"{_units(insulin_on_board)} u on board")

    headline = "Falling fast" if direction is Direction.FALLING else "Rising fast"
    return headline, " · ".join(parts) if parts else "Moving quickly."


def _meal_copy(
    event: Optional[Reading],
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
    unit: Unit,
) -> tuple[str, str]:
    parts: list[str] = []

    if carbs_on_board is not None and carbs_on_board > 0.5:
        parts.append(f"{_grams(carbs_on_board)} g absorbing")
    if event is not None:
        parts.append(f"peak near {unit.label(event.mgdl)} {unit.suffix}")
    if insulin_on_board is not None and insulin_on_board > 0.05:
        parts.append(f"{_units(insulin_on_board)} u on board")

    return "After a meal", " · ".join(parts) if parts else "A meal is absorbing."


def _manual_copy(
    trend: Optional[TrendDirection],
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
) -> tuple[str, str]:
    parts: list[str] = []

    if trend is not None:
        parts.append(_capitalised_first(trend.spoken_movement))
    if carbs_on_board is not None and carbs_on_board > 0.5:
        parts.append(f"{_grams(carbs_on_board)} g absorbing")
    if insulin_on_board is not None and insulin_on_board > 0.05:
        parts.append(f"{_units(insulin_on_board)} u on board")

    return "Glucose", " · ".join(parts) if parts else "Following along."


def farewell_copy(kind: EpisodeKind, reason: EndReason) -> tuple[str, str]:
    """The last thing an activity says before it goes away.

    An activity that simply vanishes leaves the user unsure whether the
    situation resolved or something broke.
    """
    if reason is EndReason.RECOVERED:
        return {
            EpisodeKind.LOW: ("Back in range", "The low has passed."),
            EpisodeKind.VARIATION: ("Steady again", "Glucose has stopped moving."),
            EpisodeKind.MEAL: ("Settled", "Glucose has been steady and in range."),
            EpisodeKind.MANUAL: ("Finished", "The Live Activity you started has ended."),
        }[kind]
    if reason is EndReason.COMPLETED:
        if kind is EpisodeKind.MANUAL:
            return "Finished", "The Live Activity you started has run out."
        if kind is EpisodeKind.MEAL:
            return "Finished", "Three hours since the meal."
        return kind.title, "Ended."
    if reason is EndReason.WENT_STALE:
        return "No glucose data", "Check your sensor or the app it uploads through."
    if reason is EndReason.EXPIRED:
        return "Still going", "Open Gloo for the current picture."
    return kind.title, "Ended."


# --- spark -----------------------------------------------------------------


def spark(
    readings: Sequence[Reading],
    limit: int = MAX_SPARK_POINTS,
    window: float = SPARK_WINDOW_SECONDS,
) -> tuple[Optional[float], Optional[float], list[float]]:
    """Resample onto the regular grid the state carries.

    Nearest-sample rather than interpolating: a CGM gap should look like a flat
    stretch, not like a plausible line drawn through nothing. Mirrors
    ``GlucoseActivityState.spark(from:limit:window:)`` sample for sample.
    """
    readings = list(readings)
    if not readings or limit <= 1:
        return None, None, []

    last = readings[-1]
    end = last.at
    start = end - window
    in_window = [r for r in readings if r.at >= start]
    if len(in_window) <= 1:
        return end, None, [last.mgdl]

    interval = window / (limit - 1)
    values: list[float] = []
    cursor = 0
    for index in range(limit):
        target = start + index * interval
        while cursor + 1 < len(in_window) and abs(in_window[cursor + 1].at - target) <= abs(
            in_window[cursor].at - target
        ):
            cursor += 1
        values.append(in_window[cursor].mgdl)

    return start, interval, values


# --- state -----------------------------------------------------------------


def forecast_event(
    episode: Episode,
    prediction: Sequence[Reading],
    configuration: EpisodeConfiguration,
    now: float,
) -> Optional[Reading]:
    """Which forecast point this episode is about.

    A low or a fall is about where the curve crosses the low line; a meal or a
    rise is about how high it gets. A manual card takes either, preferring the
    crossing.
    """
    crossing = predicted_crossing(
        prediction, configuration.low_entry_level, configuration.prediction_horizon, now
    )
    peak = predicted_peak(prediction, configuration.prediction_horizon, now)

    if episode.kind is EpisodeKind.LOW:
        return crossing
    if episode.kind is EpisodeKind.VARIATION:
        return crossing if episode.direction is Direction.FALLING else peak
    if episode.kind is EpisodeKind.MEAL:
        return peak
    return crossing if crossing is not None else peak


def shows_countdown(episode: Episode, configuration: EpisodeConfiguration) -> bool:
    """Whether this episode's deadline is a promise worth showing.

    A low also has a deadline, but it is a four-hour safety ceiling, and a Lock
    Screen counting down to it would be telling the user something untrue about
    when their low will be over.
    """
    return configuration.expiry_reason(episode.kind) is EndReason.COMPLETED


def build_state(
    session: Session,
    readings: Sequence[Reading],
    prediction: Sequence[Reading],
    configuration: EpisodeConfiguration,
    unit: Unit,
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
    now: float,
) -> Optional[dict[str, Any]]:
    """The ``content-state`` dictionary for one published moment.

    Returns ``None`` when there is no reading to build it around — pushing an
    activity with no number on it is worse than not pushing.
    """
    readings = list(readings)
    if not readings:
        return None
    latest = readings[-1]
    episode = session.episode

    event = forecast_event(episode, prediction, configuration, now)
    trend = trend_from(readings)
    headline, detail = copy_for(
        episode=episode,
        mgdl=latest.mgdl,
        trend=trend,
        event=event,
        carbs_on_board=carbs_on_board,
        insulin_on_board=insulin_on_board,
        unit=unit,
        configuration=configuration,
    )
    spark_start, spark_interval, spark_values = spark(readings)

    state: dict[str, Any] = {
        "schema": SCHEMA,
        "sequence": session.sequence,
        # Always "server" from here. Never drawn; logged on both sides, because
        # "the Lock Screen is stale" and "the service stopped pushing" used to
        # be indistinguishable.
        "source": "server",
        "kind": episode.kind.value,
        "episodeStartedAt": episode.started_at,
        "updatedAt": now,
        "readingAt": latest.at,
        "mgdL": latest.mgdl,
        "headline": headline,
        "detail": detail,
        "spark": spark_values,
        "rangeLower": configuration.in_range_lower,
        "rangeUpper": configuration.in_range_upper,
        "lowThreshold": configuration.suspend_threshold,
        "unit": unit.value,
    }

    # Optionals are omitted rather than sent as null, matching Swift's
    # `encodeIfPresent`. A null would decode to nil anyway, but an absent key is
    # fewer bytes against ActivityKit's 4 KB ceiling and makes a captured
    # payload easier to read.
    if episode.direction is not None:
        state["direction"] = episode.direction.value
    if shows_countdown(episode, configuration):
        state["endsAt"] = episode.ends_at
    if trend is not None:
        state["trend"] = trend.value
    slope = slope_per_minute(readings)
    if slope is not None:
        state["slopePerMinute"] = slope
    if carbs_on_board is not None:
        state["carbsOnBoard"] = carbs_on_board
    if insulin_on_board is not None:
        state["insulinOnBoard"] = insulin_on_board
    if event is not None:
        state["eventAt"] = event.at
        state["eventMgdL"] = event.mgdl
    if spark_start is not None:
        state["sparkStart"] = spark_start
    if spark_interval is not None:
        state["sparkInterval"] = spark_interval

    return state


def attributes_for(session: Session) -> dict[str, Any]:
    """The fixed half of an activity, needed only by a start push.

    Two fields, and neither can change: which card this is, and when it
    appeared. Everything about *what it says* is state.
    """
    return {
        "sessionID": session.id,
        "startedAt": session.started_at,
    }


def with_farewell(state: dict[str, Any], kind: EpisodeKind, reason: EndReason) -> dict[str, Any]:
    headline, detail = farewell_copy(kind, reason)
    closing = dict(state)
    closing["headline"] = headline
    closing["detail"] = detail
    return closing
