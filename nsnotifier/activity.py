"""Building the Live Activity content state, mirroring MDIKit.

Two Swift files are mirrored here: ``GlucoseActivityState.make`` and
``GlucoseActivityCopy``. The state is what APNs carries; the copy is the words
on its face.

The rule that shapes the copy is worth restating because it looks like an
omission: **no clock times, ever.** "around 14:20" cannot be produced correctly
by a server that does not know the phone's locale, its 12- or 24-hour
preference, or its time zone, and being an hour out on a hypo warning is not a
cosmetic failure. The state carries ``eventAt`` as an instant and the widget
renders it with the system's own formatting. Numbers are safe — ``Unit.label``
is arithmetic, and the Swift implementation is the same two lines.

``tests/test_activity.py`` pins the wire keys and the exact strings.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .episodes import predicted_crossing, predicted_peak
from .models import (
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    EndReason,
    Reading,
    TrendDirection,
    Unit,
    slope_per_minute,
    trend_from,
)

SCHEMA = 2
MAX_SPARK_POINTS = 16
SPARK_WINDOW_SECONDS = 2 * 3600
# The literal string an APNs start payload's `attributes-type` must carry.
ATTRIBUTES_TYPE = "GlucoseActivityAttributes"


# --- copy ------------------------------------------------------------------


def _capitalised_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def copy_for(
    kind: EpisodeKind,
    mgdl: float,
    trend: Optional[TrendDirection],
    event: Optional[Reading],
    carbs_on_board: Optional[float],
    insulin_on_board: Optional[float],
    unit: Unit,
    configuration: EpisodeConfiguration,
) -> tuple[str, str]:
    """(headline, detail), byte-identical to ``GlucoseActivityCopy.make``."""
    parts: list[str] = []

    if kind is EpisodeKind.HYPO_RISK:
        if mgdl < configuration.low:
            if trend is not None:
                parts.append(_capitalised_first(trend.spoken_movement))
        elif event is not None:
            parts.append(f"Forecast dips to {unit.label(event.mgdl)} {unit.suffix}")
        elif trend is not None:
            parts.append(_capitalised_first(trend.spoken_movement))

        if insulin_on_board is not None and insulin_on_board > 0.05:
            parts.append(f"{insulin_on_board:.1f} u on board")

        headline = "Low glucose" if mgdl < configuration.low else "Heading low"
        return headline, " · ".join(parts) if parts else "Watch it."

    if carbs_on_board is not None and carbs_on_board > 0.5:
        parts.append(f"{int(round(carbs_on_board))} g absorbing")
    if event is not None:
        parts.append(f"peak near {unit.label(event.mgdl)} {unit.suffix}")
    if insulin_on_board is not None and insulin_on_board > 0.05:
        parts.append(f"{insulin_on_board:.1f} u on board")

    return "Carbs on board", " · ".join(parts) if parts else "A meal is absorbing."


def farewell_copy(kind: EpisodeKind, reason: EndReason) -> tuple[str, str]:
    """The last thing an activity says before it goes away.

    An activity that simply vanishes leaves the user unsure whether the
    situation resolved or something broke.
    """
    if reason is EndReason.RECOVERED:
        detail = "The low has passed." if kind is EpisodeKind.HYPO_RISK else "The meal has settled."
        return "Back in range", detail
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


def build_state(
    episode: Episode,
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

    if episode.kind is EpisodeKind.HYPO_RISK:
        event = predicted_crossing(prediction, configuration.low, configuration.prediction_horizon, now)
    else:
        event = predicted_peak(prediction, configuration.prediction_horizon, now)

    trend = trend_from(readings)
    headline, detail = copy_for(
        kind=episode.kind,
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
        "sequence": episode.sequence,
        # Always "server" from here. Never drawn; logged on both sides, because
        # "the Lock Screen is stale" and "the service stopped pushing" used to
        # be indistinguishable.
        "source": "server",
        "updatedAt": now,
        "readingAt": latest.at,
        "mgdL": latest.mgdl,
        "headline": headline,
        "detail": detail,
        "spark": spark_values,
        "rangeLower": configuration.in_range_lower,
        "rangeUpper": configuration.in_range_upper,
        "lowThreshold": configuration.low,
        "unit": unit.value,
    }

    # Optionals are omitted rather than sent as null, matching Swift's
    # `encodeIfPresent`. A null would decode to nil anyway, but an absent key is
    # fewer bytes against ActivityKit's 4 KB ceiling and makes a captured
    # payload easier to read.
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


def attributes_for(episode: Episode) -> dict[str, Any]:
    """The fixed half of an activity, needed only by a start push."""
    return {
        "episodeKind": episode.kind.value,
        "episodeID": episode.identifier,
        "startedAt": episode.started_at,
    }


def with_farewell(state: dict[str, Any], kind: EpisodeKind, reason: EndReason) -> dict[str, Any]:
    headline, detail = farewell_copy(kind, reason)
    closing = dict(state)
    closing["headline"] = headline
    closing["detail"] = detail
    return closing
