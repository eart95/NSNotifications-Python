"""The vocabulary shared with the phone.

Every class here has a Swift counterpart in MDIKit's ``MDISnapshot`` target,
and the field names are the wire format — not an internal convenience. Renaming
one of them is a protocol change, not a refactor.

Two conventions run through the whole module and both come from the phone:

* **Instants are Unix epoch seconds, as floats.** ActivityKit decodes a pushed
  Live Activity ``content-state`` with a stock ``JSONDecoder``, whose default
  date strategy is seconds since 2001. Anything encoded as a date would arrive
  31 years wrong. Nothing here is ever serialised as an ISO string or a
  reference-date offset.
* **Glucose is canonical mg/dL everywhere.** ``unit`` says how to *print* it and
  nothing else, exactly as in the app.

The nouns to have straight before reading anything else:

* a **session** is one Live Activity — one card on the Lock Screen, from the
  moment it appears until it goes away. There is never more than one.
* an **episode** is a *reason* for that card: a low, a fast movement, a meal,
  or that the user asked for one. A session runs through as many episodes as it
  needs to, without the card ever leaving the screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

MG_DL_PER_MMOL_L = 18.0182


class Unit(str, Enum):
    MGDL = "mgdL"
    MMOLL = "mmolL"

    @property
    def suffix(self) -> str:
        return "mg/dL" if self is Unit.MGDL else "mmol/L"

    def label(self, canonical_mgdl: float) -> str:
        """The number as the phone would print it.

        Deliberately arithmetic rather than a locale-aware formatter: this has
        to produce byte-identical output to ``GlucoseWidgetSnapshot.Unit.label``
        in Swift, and a formatter is exactly the thing that wouldn't.
        """
        if self is Unit.MGDL:
            return str(int(round(canonical_mgdl)))
        value = canonical_mgdl / MG_DL_PER_MMOL_L
        rounded = round(value * 10) / 10
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:.1f}"


class TrendDirection(str, Enum):
    FALLING_FAST = "fallingFast"
    FALLING = "falling"
    STEADY = "steady"
    RISING = "rising"
    RISING_FAST = "risingFast"

    @staticmethod
    def from_slope(slope_per_minute: float) -> "TrendDirection":
        # The same bands as `GlucoseTrendDirection(slopePerMinute:)`.
        if slope_per_minute >= 3:
            return TrendDirection.RISING_FAST
        if slope_per_minute >= 1:
            return TrendDirection.RISING
        if slope_per_minute >= -1:
            return TrendDirection.STEADY
        if slope_per_minute >= -3:
            return TrendDirection.FALLING
        return TrendDirection.FALLING_FAST

    @property
    def spoken_movement(self) -> str:
        return {
            TrendDirection.RISING_FAST: "rising quickly",
            TrendDirection.RISING: "rising",
            TrendDirection.STEADY: "steady",
            TrendDirection.FALLING: "falling",
            TrendDirection.FALLING_FAST: "falling quickly",
        }[self]


@dataclass(frozen=True)
class Reading:
    """One glucose value. ``at`` is Unix epoch seconds."""

    at: float
    mgdl: float


@dataclass(frozen=True)
class Treatment:
    """A logged carbohydrate and/or insulin event."""

    at: float
    carbs: float = 0.0
    insulin: float = 0.0


class EpisodeKind(str, Enum):
    """Mirrors ``GlucoseEpisodeKind``."""

    MANUAL = "manual"
    LOW = "low"
    VARIATION = "variation"
    MEAL = "meal"

    @property
    def title(self) -> str:
        return {
            EpisodeKind.MANUAL: "Glucose",
            EpisodeKind.LOW: "Low glucose",
            EpisodeKind.VARIATION: "Moving fast",
            EpisodeKind.MEAL: "After a meal",
        }[self]

    @property
    def starts_automatically(self) -> bool:
        """Only ``manual`` may not: it exists because a person pressed a
        button, and nothing about glucose can imply that."""
        return self is not EpisodeKind.MANUAL


class Direction(str, Enum):
    RISING = "rising"
    FALLING = "falling"


class EndReason(str, Enum):
    RECOVERED = "recovered"
    COMPLETED = "completed"
    EXPIRED = "expired"
    WENT_STALE = "wentStale"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class AlertKind(str, Enum):
    """Mirrors ``GlucoseAlert.Kind``.

    The values matter: the phone damps its own local copy of an alert by this
    exact string, so a typo here means the user gets told twice.
    """

    PREDICTED_LOW = "predictedLow"
    MEASURED_LOW = "measuredLow"
    MEASURED_HIGH = "measuredHigh"
    DATA_STALE = "dataStale"

    @property
    def title(self) -> str:
        return {
            AlertKind.PREDICTED_LOW: "Heading low",
            AlertKind.MEASURED_LOW: "Glucose low",
            AlertKind.MEASURED_HIGH: "Glucose high",
            AlertKind.DATA_STALE: "No glucose data",
        }[self]


@dataclass(frozen=True)
class Episode:
    """One run of one kind, with its own deadline and its own progress towards
    clearing."""

    kind: EpisodeKind
    started_at: float
    #: Set for every kind. For ``manual`` and ``meal`` it is the ordinary way
    #: the episode finishes; for ``low`` and ``variation`` it is a backstop
    #: against a rule that never clears.
    ends_at: float
    direction: Optional[Direction] = None
    #: When the exit condition most recently *started* holding, or None if it
    #: does not hold now. This is what "over 85 for 15 minutes" is made of.
    clearing_since: Optional[float] = None

    @property
    def identifier(self) -> str:
        # Must match `GlucoseEpisode.id` in Swift exactly.
        return f"{self.kind.value}.{int(self.started_at)}"

    @property
    def priority(self) -> int:
        """Which episode wins when two apply at once. Lower is more urgent.

        The interesting entry is ``variation``, which sits on both sides of
        ``meal``. A fall steep enough to qualify while a meal is on screen is
        the overcorrection you want to know about; a *rise* that steep during a
        meal is the meal.
        """
        if self.kind is EpisodeKind.LOW:
            return 0
        if self.kind is EpisodeKind.VARIATION:
            return 1 if self.direction is Direction.FALLING else 3
        if self.kind is EpisodeKind.MEAL:
            return 2
        return 4

    def suspended(self) -> "Episode":
        """Cleared on suspension, so a resumed episode earns its dwell again
        from the glucose actually in front of it."""
        return Episode(self.kind, self.started_at, self.ends_at, self.direction, None)

    def clearing(self, since: Optional[float]) -> "Episode":
        return Episode(self.kind, self.started_at, self.ends_at, self.direction, since)

    def ending(self, at: float) -> "Episode":
        return Episode(self.kind, self.started_at, at, self.direction, self.clearing_since)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "startedAt": self.started_at,
            "endsAt": self.ends_at,
            "direction": self.direction.value if self.direction else None,
            "clearingSince": self.clearing_since,
        }

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> Optional["Episode"]:
        if not raw:
            return None
        try:
            direction = raw.get("direction")
            clearing = raw.get("clearingSince")
            return Episode(
                kind=EpisodeKind(raw["kind"]),
                started_at=float(raw["startedAt"]),
                ends_at=float(raw["endsAt"]),
                direction=Direction(direction) if direction else None,
                clearing_since=float(clearing) if clearing is not None else None,
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True)
class Session:
    """One Live Activity, and everything both writers need to agree about it."""

    id: str
    started_at: float
    #: Monotonic across the whole session, not per episode. The card survives a
    #: change of kind, so a counter that restarted with each episode would have
    #: every update after a switch discarded by the phone as stale.
    sequence: int
    episode: Episode
    #: Episodes displaced by something more urgent, newest last.
    suspended: tuple[Episode, ...] = ()

    #: Deep enough for the only case that happens in practice — a manual card
    #: interrupted by a meal, interrupted by a low.
    MAXIMUM_SUSPENDED = 2

    @staticmethod
    def identifier(started_at: float) -> str:
        """``s.`` prefixed so it can never be mistaken for an episode id."""
        return f"s.{int(started_at)}"

    def advanced(self) -> "Session":
        return Session(self.id, self.started_at, self.sequence + 1, self.episode, self.suspended)

    def with_episode(self, episode: Episode) -> "Session":
        return Session(self.id, self.started_at, self.sequence, episode, self.suspended)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "startedAt": self.started_at,
            "sequence": self.sequence,
            "episode": self.episode.to_dict(),
            "suspended": [episode.to_dict() for episode in self.suspended],
        }

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> Optional["Session"]:
        if not raw:
            return None
        episode = Episode.from_dict(raw.get("episode"))
        if episode is None:
            return None
        try:
            suspended = [Episode.from_dict(item) for item in raw.get("suspended") or []]
            return Session(
                id=str(raw["id"]),
                started_at=float(raw["startedAt"]),
                sequence=int(raw.get("sequence", 0)),
                episode=episode,
                suspended=tuple(item for item in suspended if item is not None),
            )
        except (KeyError, ValueError, TypeError):
            return None


# --- configuration ---------------------------------------------------------


def _number(raw: dict[str, Any], key: str, fallback: float) -> float:
    value = raw.get(key, fallback)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    # A NaN or an infinity in a threshold would make every comparison silently
    # false, which reads as "the service never fires".
    return fallback if not math.isfinite(value) else value


@dataclass(frozen=True)
class ManualRules:
    """A card someone asked for. Its life is a clock, not a condition."""

    duration: float = 2 * 3600
    update_interval: float = 5 * 60

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "ManualRules":
        raw = raw or {}
        defaults = ManualRules()
        return ManualRules(
            duration=_number(raw, "duration", defaults.duration),
            update_interval=_number(raw, "updateInterval", defaults.update_interval),
        )


@dataclass(frozen=True)
class LowGlucoseRules:
    """Below the line, and how far back above it counts as over.

    Both offsets hang off the suspend threshold rather than being absolute, so
    the card follows the number the user actually tunes. At the default suspend
    threshold of 70 they are the familiar 80 in and 85 out.
    """

    entry_offset: float = 10
    exit_offset: float = 15
    clear_for: float = 15 * 60
    update_interval: float = 2 * 60
    maximum_duration: float = 4 * 3600

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "LowGlucoseRules":
        raw = raw or {}
        defaults = LowGlucoseRules()
        return LowGlucoseRules(
            entry_offset=_number(raw, "entryOffset", defaults.entry_offset),
            exit_offset=_number(raw, "exitOffset", defaults.exit_offset),
            clear_for=_number(raw, "clearFor", defaults.clear_for),
            update_interval=_number(raw, "updateInterval", defaults.update_interval),
            maximum_duration=_number(raw, "maximumDuration", defaults.maximum_duration),
        )


@dataclass(frozen=True)
class VariationRules:
    """Fast, sustained, one-directional movement.

    ``slope_per_minute`` is deliberately steep — 3 mg/dL/min is 45 mg/dL inside
    the window. A card that appeared for every ordinary post-breakfast rise
    would be on screen most mornings, and a card that is usually there is one
    nobody reads.
    """

    slope_per_minute: float = 3
    window: float = 15 * 60
    steady_slope_per_minute: float = 1
    settle_for: float = 15 * 60
    reversal_tolerance: float = 3
    rising_update_interval: float = 5 * 60
    falling_update_interval: float = 2 * 60
    maximum_duration: float = 4 * 3600

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "VariationRules":
        raw = raw or {}
        defaults = VariationRules()
        return VariationRules(
            slope_per_minute=_number(raw, "slopePerMinute", defaults.slope_per_minute),
            window=_number(raw, "window", defaults.window),
            steady_slope_per_minute=_number(
                raw, "steadySlopePerMinute", defaults.steady_slope_per_minute
            ),
            settle_for=_number(raw, "settleFor", defaults.settle_for),
            reversal_tolerance=_number(raw, "reversalTolerance", defaults.reversal_tolerance),
            rising_update_interval=_number(
                raw, "risingUpdateInterval", defaults.rising_update_interval
            ),
            falling_update_interval=_number(
                raw, "fallingUpdateInterval", defaults.falling_update_interval
            ),
            maximum_duration=_number(raw, "maximumDuration", defaults.maximum_duration),
        )


@dataclass(frozen=True)
class MealRules:
    """Carbohydrate going in, not carbohydrate on board.

    A 60 g meal still leaves 40 g on board an hour later, and a card that
    reacted to that would appear long after the interesting part.
    """

    grams: float = 30
    window: float = 20 * 60
    duration: float = 3 * 3600
    settle_for: float = 30 * 60
    steady_slope_per_minute: float = 1
    update_interval: float = 5 * 60

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "MealRules":
        raw = raw or {}
        defaults = MealRules()
        return MealRules(
            grams=_number(raw, "grams", defaults.grams),
            window=_number(raw, "window", defaults.window),
            duration=_number(raw, "duration", defaults.duration),
            settle_for=_number(raw, "settleFor", defaults.settle_for),
            steady_slope_per_minute=_number(
                raw, "steadySlopePerMinute", defaults.steady_slope_per_minute
            ),
            update_interval=_number(raw, "updateInterval", defaults.update_interval),
        )


@dataclass(frozen=True)
class EpisodeConfiguration:
    """Mirror of ``GlucoseEpisodeEvaluator.Configuration``.

    The defaults exist only so a malformed registration degrades to something
    sane. In normal operation every value here came off the phone, because the
    phone is where the user edits them — a service holding its own copy is a
    service that will one day decide something at a threshold changed a month
    ago.
    """

    suspend_threshold: float = 70.0
    in_range_lower: float = 70.0
    in_range_upper: float = 180.0
    stale_after: float = 25 * 60
    restart_cooldown: float = 15 * 60
    prediction_horizon: float = 30 * 60
    enabled_kinds: frozenset[EpisodeKind] = frozenset(EpisodeKind)
    manual: ManualRules = field(default_factory=ManualRules)
    low_glucose: LowGlucoseRules = field(default_factory=LowGlucoseRules)
    variation: VariationRules = field(default_factory=VariationRules)
    meal: MealRules = field(default_factory=MealRules)

    @property
    def low_entry_level(self) -> float:
        """Below this, a low episode starts. 80 mg/dL at the default."""
        return self.suspend_threshold + self.low_glucose.entry_offset

    @property
    def low_exit_level(self) -> float:
        """Above this — for ``low_glucose.clear_for`` — it ends. 85 by default."""
        return self.suspend_threshold + self.low_glucose.exit_offset

    def is_enabled(self, kind: EpisodeKind) -> bool:
        # `manual` is always available: it is a button, and a button that does
        # nothing is worse than no button.
        return kind is EpisodeKind.MANUAL or kind in self.enabled_kinds

    def update_interval(self, episode: Episode) -> float:
        """How often to push a state for this episode."""
        if episode.kind is EpisodeKind.MANUAL:
            return self.manual.update_interval
        if episode.kind is EpisodeKind.LOW:
            return self.low_glucose.update_interval
        if episode.kind is EpisodeKind.VARIATION:
            return (
                self.variation.falling_update_interval
                if episode.direction is Direction.FALLING
                else self.variation.rising_update_interval
            )
        return self.meal.update_interval

    def duration(self, kind: EpisodeKind) -> float:
        """How long an episode of this kind lives if nothing ends it sooner."""
        if kind is EpisodeKind.MANUAL:
            return self.manual.duration
        if kind is EpisodeKind.LOW:
            return self.low_glucose.maximum_duration
        if kind is EpisodeKind.VARIATION:
            return self.variation.maximum_duration
        return self.meal.duration

    def expiry_reason(self, kind: EpisodeKind) -> EndReason:
        """What reaching ``ends_at`` means for this kind.

        A manual card and a meal card are *finished* when their clock runs out —
        that is what they were for. A low still going at its ceiling is a rule
        that failed to clear, which is a different thing and says so.
        """
        if kind in (EpisodeKind.MANUAL, EpisodeKind.MEAL):
            return EndReason.COMPLETED
        return EndReason.EXPIRED

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "EpisodeConfiguration":
        raw = raw or {}
        defaults = EpisodeConfiguration()

        kinds: set[EpisodeKind] = set()
        listed = raw.get("enabledKinds")
        if listed is None:
            kinds = set(EpisodeKind)
        else:
            for value in listed:
                try:
                    kinds.add(EpisodeKind(value))
                except ValueError:
                    # A kind this build has never heard of. Ignored rather than
                    # fatal: a newer app must not be able to break registration.
                    continue

        return EpisodeConfiguration(
            suspend_threshold=_number(raw, "suspendThreshold", defaults.suspend_threshold),
            in_range_lower=_number(raw, "inRangeLower", defaults.in_range_lower),
            in_range_upper=_number(raw, "inRangeUpper", defaults.in_range_upper),
            stale_after=_number(raw, "staleAfter", defaults.stale_after),
            restart_cooldown=_number(raw, "restartCooldown", defaults.restart_cooldown),
            prediction_horizon=_number(raw, "predictionHorizon", defaults.prediction_horizon),
            enabled_kinds=frozenset(kinds),
            manual=ManualRules.from_dict(raw.get("manual")),
            low_glucose=LowGlucoseRules.from_dict(raw.get("lowGlucose")),
            variation=VariationRules.from_dict(raw.get("variation")),
            meal=MealRules.from_dict(raw.get("meal")),
        )


@dataclass(frozen=True)
class AlertThresholds:
    """Mirror of ``GlucoseAlert.Thresholds``.

    A different set of numbers from ``EpisodeConfiguration`` on purpose. "High
    enough to put on your Lock Screen" and "high enough to wake you up" are not
    the same question, and 180 — where the chart stops calling you in range —
    would fire most days for most people. An alert you learn to ignore is worse
    than no alert.
    """

    low: float = 70.0
    high: float = 250.0
    stale_after: float = 25 * 60
    prediction_horizon: float = 60 * 60
    prediction_lead_time: float = 20 * 60
    re_alert_interval: float = 30 * 60

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "AlertThresholds":
        raw = raw or {}
        defaults = AlertThresholds()
        return AlertThresholds(
            low=_number(raw, "low", defaults.low),
            high=_number(raw, "high", defaults.high),
            stale_after=_number(raw, "staleAfter", defaults.stale_after),
            prediction_horizon=_number(raw, "predictionHorizon", defaults.prediction_horizon),
            prediction_lead_time=_number(raw, "predictionLeadTime", defaults.prediction_lead_time),
            re_alert_interval=_number(raw, "reAlertInterval", defaults.re_alert_interval),
        )


@dataclass(frozen=True)
class Device:
    """One registered phone, as it described itself."""

    device_id: str
    apns_token: Optional[str]
    push_to_start_token: Optional[str]
    activity_token: Optional[str]
    activity_session_id: Optional[str]
    bundle_id: str
    environment: str
    unit: Unit
    configuration: EpisodeConfiguration
    alert_thresholds: AlertThresholds
    enabled_alert_kinds: frozenset[AlertKind]
    alerts_enabled: bool
    live_activities_enabled: bool
    registered_at: float

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @staticmethod
    def from_registration(raw: dict[str, Any]) -> "Device":
        kinds: set[AlertKind] = set()
        for value in raw.get("enabledAlertKinds") or []:
            try:
                kinds.add(AlertKind(value))
            except ValueError:
                continue

        try:
            unit = Unit(raw.get("unit", Unit.MGDL.value))
        except ValueError:
            unit = Unit.MGDL

        return Device(
            device_id=str(raw["deviceID"]),
            apns_token=raw.get("apnsToken") or None,
            push_to_start_token=raw.get("pushToStartToken") or None,
            activity_token=raw.get("activityToken") or None,
            # `activityEpisodeID` is what builds before the session rework sent.
            # Read as a fallback so a phone that has not been updated yet
            # registers something addressable rather than nothing at all.
            activity_session_id=raw.get("activitySessionID") or raw.get("activityEpisodeID") or None,
            bundle_id=str(raw.get("bundleID", "")),
            environment="production" if raw.get("environment") == "production" else "development",
            unit=unit,
            configuration=EpisodeConfiguration.from_dict(raw.get("episodeConfiguration")),
            alert_thresholds=AlertThresholds.from_dict(raw.get("alertThresholds")),
            enabled_alert_kinds=frozenset(kinds),
            alerts_enabled=bool(raw.get("alertsEnabled", False)),
            live_activities_enabled=bool(raw.get("liveActivitiesEnabled", False)),
            registered_at=float(raw.get("registeredAt") or 0.0),
        )


def sorted_readings(readings: Iterable[Reading]) -> list[Reading]:
    """Ascending by time, which everything downstream assumes."""
    return sorted(readings, key=lambda reading: reading.at)


def trend_from(readings: Sequence[Reading], sample_count: int = 4) -> Optional[TrendDirection]:
    slope = slope_per_minute(readings, sample_count)
    return None if slope is None else TrendDirection.from_slope(slope)


def slope_per_minute(readings: Sequence[Reading], sample_count: int = 4) -> Optional[float]:
    """mg/dL per minute over the tail of the series.

    Returns ``None`` rather than 0 when there is not enough to say. A caller
    that renders "steady" for "no idea" is lying in the one direction that
    matters — a flat arrow beside a falling number.
    """
    recent = list(readings)[-sample_count:]
    if len(recent) < 2:
        return None
    minutes = (recent[-1].at - recent[0].at) / 60
    if minutes <= 0:
        return None
    return (recent[-1].mgdl - recent[0].mgdl) / minutes


def slope_over_window(readings: Sequence[Reading], window: float) -> Optional[float]:
    """mg/dL per minute across a trailing window, or None when there is not
    enough of one to say. Mirrors ``GlucoseEpisodeEvaluator.slopePerMinute``."""
    readings = list(readings)
    if not readings:
        return None
    last = readings[-1]
    points = [r for r in readings if r.at >= last.at - window]
    if len(points) < 2:
        return None
    minutes = (last.at - points[0].at) / 60
    if minutes <= 0:
        return None
    return (last.mgdl - points[0].mgdl) / minutes
