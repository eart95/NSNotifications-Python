"""The episode rules, mirroring ``GlucoseEpisode.swift`` in MDIKit.

This is the file to read next to the Swift one. They implement the same state
machine over the same thresholds, and they have to agree: the phone evaluates
these rules whenever it is awake, and this service evaluates them the rest of
the time. If they disagree, a Live Activity appears and disappears depending on
whether the app happened to be open, which reads as a bug even when both
answers are defensible.

Keeping two implementations in step is a real cost, and it is paid deliberately.
The alternative is the phone doing nothing on its own, which would mean a hypo
goes unannounced whenever this service is redeploying.

``tests/test_episodes.py`` encodes the same scenarios as
``GlucoseEpisodeTests.swift``. A case added to one belongs in the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import (
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    EndReason,
    Reading,
    TrendDirection,
    trend_from,
)


@dataclass(frozen=True)
class EpisodeInput:
    readings: Sequence[Reading]
    prediction: Sequence[Reading] = ()
    carbs_entering_window: float = 0.0
    carbs_on_board: Optional[float] = None
    insulin_on_board: Optional[float] = None


@dataclass(frozen=True)
class Decision:
    """What the single Live Activity should be doing.

    ``action`` is one of ``idle``, ``start``, ``update``, ``end``, ``replace``.
    A ``replace`` carries both halves: the ending episode with its reason, and
    the one taking over.
    """

    action: str
    episode: Optional[Episode] = None
    reason: Optional[EndReason] = None
    starting: Optional[Episode] = None

    @property
    def live(self) -> Optional[Episode]:
        if self.action in ("start", "update"):
            return self.episode
        if self.action == "replace":
            return self.starting
        return None


IDLE = Decision(action="idle")


def evaluate(
    current: Optional[Episode],
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    last_ended: Mapping[EpisodeKind, float],
    now: float,
) -> Decision:
    """Decide what should happen to the activity. Pure; no I/O, no clock."""
    readings = list(data.readings)
    if not readings:
        # Not "in range", not "out of range" — nothing to describe. End
        # anything running rather than leave it showing the last number it
        # happened to have.
        if current is not None:
            return Decision("end", current, EndReason.WENT_STALE)
        return IDLE

    latest = readings[-1]
    is_fresh = (now - latest.at) <= configuration.stale_after

    if current is not None:
        if now - current.started_at >= configuration.maximum_duration:
            return Decision("end", current, EndReason.EXPIRED)
        if not is_fresh:
            return Decision("end", current, EndReason.WENT_STALE)

    hypo = _hypo_condition_holds(latest, data, configuration, now)

    # Escalation first: a hypo displaces a meal immediately, without waiting out
    # the meal's minimum duration or the hypo's own cooldown. The cooldown
    # exists to stop flapping, and being pulled low mid-meal is not flapping.
    if current is not None and current.kind is EpisodeKind.CARB_RISE and hypo:
        return Decision(
            "replace",
            episode=current,
            reason=EndReason.SUPERSEDED,
            starting=Episode(EpisodeKind.HYPO_RISK, started_at=now),
        )

    if current is not None:
        return _continuation(current, latest, data, configuration, now)

    for kind in sorted(EpisodeKind, key=lambda k: k.priority):
        if not is_fresh or _is_cooling_down(kind, last_ended, configuration, now):
            continue
        holds = hypo if kind is EpisodeKind.HYPO_RISK else _carb_condition_holds(latest, data, configuration)
        if holds:
            return Decision("start", Episode(kind, started_at=now))

    return IDLE


# --- entry -----------------------------------------------------------------


def _hypo_condition_holds(
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    now: float,
) -> bool:
    """Below the threshold now, or forecast to cross it inside the horizon.

    The forecast half is why this is *hypo risk* and not *hypo*: an activity
    that appears once you are already at 55 has told you something you could
    have read off your own hands.
    """
    if latest.mgdl < configuration.low:
        return True
    return predicted_crossing(data.prediction, configuration.low, configuration.prediction_horizon, now) is not None


def _carb_condition_holds(
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
) -> bool:
    """Carbohydrate arriving steeply.

    Guarded by "not currently low", because the fastest carbohydrate anyone ever
    eats is a hypo treatment, and turning that into a *meal* activity would be
    exactly backwards.
    """
    if latest.mgdl < configuration.low:
        return False
    return data.carbs_entering_window >= configuration.carb_rise_grams_in_window


# --- exit ------------------------------------------------------------------


def _continuation(
    current: Episode,
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    now: float,
) -> Decision:
    if now - current.started_at < configuration.minimum_duration:
        return Decision("update", current)

    trend = trend_from(data.readings)
    falling = trend in (TrendDirection.FALLING, TrendDirection.FALLING_FAST)
    rising = trend in (TrendDirection.RISING, TrendDirection.RISING_FAST)

    if current.kind is EpisodeKind.HYPO_RISK:
        recovered = (
            latest.mgdl >= configuration.hypo_clear_level
            and not falling
            and predicted_crossing(
                data.prediction, configuration.low, configuration.prediction_horizon, now
            )
            is None
        )
        return Decision("end", current, EndReason.RECOVERED) if recovered else Decision("update", current)

    settled = (
        latest.mgdl <= configuration.carb_clear_level
        and latest.mgdl >= configuration.hypo_clear_level
        and not rising
        and data.carbs_entering_window < configuration.carb_rise_grams_in_window
    )
    return Decision("end", current, EndReason.RECOVERED) if settled else Decision("update", current)


def _is_cooling_down(
    kind: EpisodeKind,
    last_ended: Mapping[EpisodeKind, float],
    configuration: EpisodeConfiguration,
    now: float,
) -> bool:
    ended = last_ended.get(kind)
    if ended is None:
        return False
    return (now - ended) < configuration.restart_cooldown


# --- forecast helpers ------------------------------------------------------


def predicted_crossing(
    prediction: Sequence[Reading],
    level: float,
    horizon: float,
    now: float,
) -> Optional[Reading]:
    for point in prediction:
        if point.mgdl < level and now < point.at <= now + horizon:
            return point
    return None


def predicted_peak(
    prediction: Sequence[Reading],
    horizon: float,
    now: float,
) -> Optional[Reading]:
    inside = [p for p in prediction if now < p.at <= now + horizon]
    if not inside:
        return None
    return max(inside, key=lambda p: p.mgdl)
