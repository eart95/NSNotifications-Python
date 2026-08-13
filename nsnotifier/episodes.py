"""The episode rules, mirroring ``GlucoseEpisode.swift`` in MDIKit.

This is the file to read next to the Swift one. They implement the same state
machine over the same thresholds, and they have to agree: the phone evaluates
these rules whenever it is awake, and this service evaluates them the rest of
the time. If they disagree, a Live Activity appears and disappears depending on
whether the app happened to be open, which reads as a bug even when both
answers are defensible.

Keeping two implementations in step is a real cost, and it is paid
deliberately. The alternative is one of them holding rules the other cannot
see, and this is a feature where the two writers have to describe the same
Lock Screen.

``tests/test_episodes.py`` encodes the same scenarios as
``GlucoseEpisodeTests.swift``. A case added to one belongs in the other.

The shape of a decision changed with the session model, and the change is the
point: there is no "replace" any more. A card that goes from a meal to a low is
an ``update`` carrying a session whose episode is a different one, because the
card itself never goes anywhere — ActivityKit freezes attributes at creation,
so anything that must be able to change lives in the state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import (
    Direction,
    EndReason,
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    Reading,
    Session,
    slope_over_window,
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

    ``action`` is one of ``idle``, ``start``, ``update``, ``end``.
    """

    action: str
    episode: Optional[Episode] = None
    session: Optional[Session] = None
    reason: Optional[EndReason] = None

    @property
    def live(self) -> Optional[Episode]:
        if self.action == "start":
            return self.episode
        if self.action == "update" and self.session is not None:
            return self.session.episode
        return None


IDLE = Decision(action="idle")


@dataclass(frozen=True)
class Variation:
    """Fast, sustained, one-directional movement over the trailing window."""

    slope_per_minute: float
    direction: Direction
    span: float


def evaluate(
    session: Optional[Session],
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    last_ended: Mapping[EpisodeKind, float],
    now: float,
) -> Decision:
    """Decide what should happen to the card. Pure; no I/O, no clock."""
    readings = list(data.readings)
    latest = readings[-1] if readings else None
    is_fresh = latest is not None and (now - latest.at) <= configuration.stale_after

    if session is None:
        # Nothing running. Only a fresh reading may start something: a card
        # that opens with a number from an hour ago describes nothing.
        if not is_fresh or latest is None:
            return IDLE
        candidate = _best_candidate(latest, data, configuration, last_ended, now)
        return Decision("start", episode=candidate) if candidate else IDLE

    # A running session outlives everything except a loss of data.
    if not is_fresh or latest is None:
        return Decision("end", session=session, reason=EndReason.WENT_STALE)

    current = session.episode

    # 1. Does something more urgent want the card?
    #
    # Checked before the current episode's own ending, so going low during a
    # meal switches immediately rather than waiting out whatever dwell the meal
    # was in the middle of. A candidate of the *same* kind is not a takeover —
    # it is the episode already running, found again by the rule that started
    # it — and is handled further down.
    candidate = _best_candidate(latest, data, configuration, last_ended, now)
    if candidate is not None and candidate.kind is not current.kind and candidate.priority < current.priority:
        return Decision(
            "update",
            session=Session(
                id=session.id,
                started_at=session.started_at,
                sequence=session.sequence,
                episode=candidate,
                suspended=_suspend(current, session.suspended),
            ),
        )

    # 2. A second helping extends the meal it belongs to rather than queueing
    # behind it.
    if (
        current.kind is EpisodeKind.MEAL
        and data.carbs_entering_window >= configuration.meal.grams
        and latest.mgdl >= configuration.low_entry_level
    ):
        extended = now + configuration.meal.duration
        if extended > current.ends_at:
            current = current.ending(extended)

    # 3. A variation that turned round is a different movement, not the same
    # one continuing. Restarting it here — rather than ending and letting the
    # entry rules find it again — is what stops the card blinking out at the
    # one moment it is most interesting.
    if current.kind is EpisodeKind.VARIATION:
        measured = variation(readings, configuration, now)
        if (
            measured is not None
            and abs(measured.slope_per_minute) >= configuration.variation.slope_per_minute
            and measured.direction is not current.direction
        ):
            return Decision(
                "update",
                session=session.with_episode(
                    new_episode(EpisodeKind.VARIATION, configuration, now, measured.direction)
                ),
            )

    # 4. Is the current episode over?
    progressed = _advance_clearing(current, latest, data, configuration, now)
    reason = _end_reason(progressed, configuration, now)
    if reason is not None:
        # Something displaced earlier may still have life in it. The card goes
        # back to what it was showing rather than disappearing because an
        # unrelated low happened to finish.
        remaining = list(session.suspended)
        resumed = _pop_resumable(remaining, now)
        if resumed is not None:
            return Decision(
                "update",
                session=Session(
                    id=session.id,
                    started_at=session.started_at,
                    sequence=session.sequence,
                    episode=resumed,
                    suspended=tuple(remaining),
                ),
            )
        return Decision("end", session=session.with_episode(progressed), reason=reason)

    return Decision("update", session=session.with_episode(progressed))


# --- entry -----------------------------------------------------------------


def _best_candidate(
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    last_ended: Mapping[EpisodeKind, float],
    now: float,
) -> Optional[Episode]:
    """The most urgent kind that could start right now, or None.

    ``manual`` is never returned: nothing about glucose implies that someone
    pressed a button.
    """
    best: Optional[Episode] = None
    for kind in EpisodeKind:
        if not kind.starts_automatically:
            continue
        if not configuration.is_enabled(kind):
            continue
        if _is_cooling_down(kind, last_ended, configuration, now):
            continue
        candidate = _entry_episode(kind, latest, data, configuration, now)
        if candidate is None:
            continue
        if best is None or candidate.priority < best.priority:
            best = candidate
    return best


def _entry_episode(
    kind: EpisodeKind,
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    now: float,
) -> Optional[Episode]:
    if kind is EpisodeKind.LOW:
        if latest.mgdl < configuration.low_entry_level:
            return new_episode(kind, configuration, now)
        return None

    if kind is EpisodeKind.VARIATION:
        measured = variation(data.readings, configuration, now)
        if measured is not None and abs(measured.slope_per_minute) >= configuration.variation.slope_per_minute:
            return new_episode(kind, configuration, now, measured.direction)
        return None

    if kind is EpisodeKind.MEAL:
        # Guarded by "not currently low", because the fastest carbohydrate
        # anyone ever eats is a hypo treatment, and turning that into a *meal*
        # card would be exactly backwards.
        if latest.mgdl < configuration.low_entry_level:
            return None
        if data.carbs_entering_window >= configuration.meal.grams:
            return new_episode(kind, configuration, now)
        return None

    return None


def new_episode(
    kind: EpisodeKind,
    configuration: EpisodeConfiguration,
    now: float,
    direction: Optional[Direction] = None,
) -> Episode:
    """A fresh episode of ``kind``, with its deadline already set."""
    return Episode(
        kind=kind,
        started_at=now,
        ends_at=now + configuration.duration(kind),
        direction=direction,
    )


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


# --- exit ------------------------------------------------------------------


def _advance_clearing(
    episode: Episode,
    latest: Reading,
    data: EpisodeInput,
    configuration: EpisodeConfiguration,
    now: float,
) -> Episode:
    """Whether the exit condition holds *at this instant*, moving the dwell
    clock forward or resetting it.

    Split from ``_end_reason`` on purpose: one answers "is it clear now", the
    other "has it been clear long enough", and conflating them is how a card
    ends on a single optimistic reading.
    """
    if episode.kind is EpisodeKind.MANUAL:
        # Nothing but its clock ends a manual card. The user asked for two
        # hours of card; giving them twenty minutes because glucose looked tidy
        # is not answering the request.
        holds = False
    elif episode.kind is EpisodeKind.LOW:
        holds = latest.mgdl > configuration.low_exit_level
    elif episode.kind is EpisodeKind.VARIATION:
        measured = variation(data.readings, configuration, now)
        if measured is None:
            # Not enough readings to say it is still moving. Treated as
            # settling rather than as continuing: the alternative is a card that
            # outlives its own evidence.
            holds = True
        else:
            holds = abs(measured.slope_per_minute) < configuration.variation.steady_slope_per_minute
    else:
        in_range = configuration.in_range_lower <= latest.mgdl <= configuration.in_range_upper
        slope = slope_over_window(data.readings, configuration.meal.window)
        steady = slope is not None and abs(slope) < configuration.meal.steady_slope_per_minute
        holds = in_range and steady

    if holds:
        # Keep the existing mark: the dwell is measured from when the condition
        # *started* holding, not from the last time it was checked.
        return episode.clearing(episode.clearing_since if episode.clearing_since is not None else now)
    return episode.clearing(None)


def _end_reason(
    episode: Episode,
    configuration: EpisodeConfiguration,
    now: float,
) -> Optional[EndReason]:
    if now >= episode.ends_at:
        return configuration.expiry_reason(episode.kind)
    dwell = _clear_dwell(episode.kind, configuration)
    if dwell is None or episode.clearing_since is None:
        return None
    return EndReason.RECOVERED if (now - episode.clearing_since) >= dwell else None


def _clear_dwell(kind: EpisodeKind, configuration: EpisodeConfiguration) -> Optional[float]:
    if kind is EpisodeKind.MANUAL:
        return None
    if kind is EpisodeKind.LOW:
        return configuration.low_glucose.clear_for
    if kind is EpisodeKind.VARIATION:
        return configuration.variation.settle_for
    return configuration.meal.settle_for


# --- suspend and resume ----------------------------------------------------


def _suspend(episode: Episode, stack: Sequence[Episode]) -> tuple[Episode, ...]:
    # A kind can only be waiting once. Without this, a low that flickers in and
    # out of a meal would push a copy of the meal each time and resume the
    # oldest of them.
    kept = [item for item in stack if item.kind is not episode.kind]
    kept.append(episode.suspended())
    if len(kept) > Session.MAXIMUM_SUSPENDED:
        kept = kept[len(kept) - Session.MAXIMUM_SUSPENDED :]
    return tuple(kept)


def _pop_resumable(stack: list[Episode], now: float) -> Optional[Episode]:
    """The newest suspended episode that still has a reason to be on screen.

    Anything whose deadline passed while it was waiting is dropped rather than
    resumed for the second it has left.
    """
    while stack:
        candidate = stack.pop()
        if now >= candidate.ends_at:
            continue
        # A resumed episode earns its dwell again from here.
        return candidate.suspended()
    return None


# --- movement --------------------------------------------------------------


def variation(
    readings: Sequence[Reading],
    configuration: EpisodeConfiguration,
    now: float,
) -> Optional[Variation]:
    """Measure the movement over the variation window, or None when the series
    cannot support a claim about it.

    None is not "steady". A gap in the CGM data means *no idea*, and the two
    callers treat it differently on purpose: it can never start an episode, and
    it lets a running one settle.
    """
    window = configuration.variation.window
    points = [r for r in readings if now - window <= r.at <= now]
    if len(points) < 3:
        return None

    first, last = points[0], points[-1]
    span = last.at - first.at
    # Most of the window, not all of it: CGM samples land every five minutes
    # and never on the boundary.
    if span < window * 0.8:
        return None

    slope = (last.mgdl - first.mgdl) / (span / 60)
    direction = Direction.RISING if slope >= 0 else Direction.FALLING
    sign = 1.0 if direction is Direction.RISING else -1.0

    # "In the same direction" — a run that spent five minutes going the other
    # way is two movements, not one, however impressive its endpoints look.
    for previous, following in zip(points, points[1:]):
        if (following.mgdl - previous.mgdl) * sign < -configuration.variation.reversal_tolerance:
            return None

    return Variation(slope_per_minute=slope, direction=direction, span=span)


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
