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
    HYPO_RISK = "hypoRisk"
    CARB_RISE = "carbRise"

    @property
    def priority(self) -> int:
        return 0 if self is EpisodeKind.HYPO_RISK else 1

    @property
    def title(self) -> str:
        return "Heading low" if self is EpisodeKind.HYPO_RISK else "Carbs on board"


class EndReason(str, Enum):
    RECOVERED = "recovered"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    WENT_STALE = "wentStale"
    CANCELLED = "cancelled"


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
    kind: EpisodeKind
    started_at: float
    sequence: int = 0

    @property
    def identifier(self) -> str:
        # Must match `GlucoseEpisode.id` in Swift exactly — the phone compares
        # it against the attributes of whatever activity is on screen.
        return f"{self.kind.value}.{int(self.started_at)}"

    def advanced(self) -> "Episode":
        return Episode(self.kind, self.started_at, self.sequence + 1)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "startedAt": self.started_at, "sequence": self.sequence}

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> Optional["Episode"]:
        if not raw:
            return None
        try:
            return Episode(
                kind=EpisodeKind(raw["kind"]),
                started_at=float(raw["startedAt"]),
                sequence=int(raw.get("sequence", 0)),
            )
        except (KeyError, ValueError):
            return None


@dataclass(frozen=True)
class ManualEpisode:
    """An episode a person asked for, rather than one glucose implied.

    It is an ordinary `Episode` with an expiry bolted on, and it is kept apart
    from the automatic one in the device's state for a reason: the episode rules
    would end it almost immediately. A meal card with no carbohydrate behind it
    is "settled" fifteen minutes in by every measure `episodes.evaluate` has, and
    it would be right — the card is not there because of a meal, it is there
    because someone asked for it. So it lives its own life and ends on its own
    clock.

    What it does *not* get is priority. A real episode takes the Lock Screen
    back the moment the rules say one has begun; a card someone asked for should
    never be the reason a hypo warning has nowhere to go.
    """

    episode: Episode
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"episode": self.episode.to_dict(), "expiresAt": self.expires_at}

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> Optional["ManualEpisode"]:
        if not raw:
            return None
        episode = Episode.from_dict(raw.get("episode"))
        if episode is None:
            return None
        try:
            return ManualEpisode(episode=episode, expires_at=float(raw["expiresAt"]))
        except (KeyError, TypeError, ValueError):
            return None

    def with_episode(self, episode: Episode) -> "ManualEpisode":
        return ManualEpisode(episode=episode, expires_at=self.expires_at)


@dataclass(frozen=True)
class EpisodeConfiguration:
    """Mirror of ``GlucoseEpisodeEvaluator.Configuration``.

    The defaults exist only so a malformed registration degrades to something
    sane. In normal operation every value here came off the phone, because the
    phone is where the user edits them — a service holding its own copy is a
    service that will one day alert at a threshold changed a month ago.
    """

    low: float = 70.0
    in_range_lower: float = 70.0
    in_range_upper: float = 180.0
    clear_margin: float = 10.0
    prediction_horizon: float = 30 * 60
    carb_rise_grams_per_hour: float = 60.0
    carb_rise_window: float = 30 * 60
    minimum_duration: float = 15 * 60
    maximum_duration: float = 4 * 3600
    restart_cooldown: float = 15 * 60
    stale_after: float = 25 * 60

    @property
    def hypo_clear_level(self) -> float:
        return self.in_range_lower + self.clear_margin

    @property
    def carb_clear_level(self) -> float:
        return self.in_range_upper - self.clear_margin

    @property
    def carb_rise_grams_in_window(self) -> float:
        return self.carb_rise_grams_per_hour * (self.carb_rise_window / 3600)

    @staticmethod
    def from_dict(raw: Optional[dict[str, Any]]) -> "EpisodeConfiguration":
        raw = raw or {}
        defaults = EpisodeConfiguration()

        def number(key: str, fallback: float) -> float:
            value = raw.get(key, fallback)
            try:
                value = float(value)
            except (TypeError, ValueError):
                return fallback
            # A NaN or an infinity in a threshold would make every comparison
            # below silently false, which reads as "the service never fires".
            return fallback if not math.isfinite(value) else value

        return EpisodeConfiguration(
            low=number("low", defaults.low),
            in_range_lower=number("inRangeLower", defaults.in_range_lower),
            in_range_upper=number("inRangeUpper", defaults.in_range_upper),
            clear_margin=number("clearMargin", defaults.clear_margin),
            prediction_horizon=number("predictionHorizon", defaults.prediction_horizon),
            carb_rise_grams_per_hour=number("carbRiseGramsPerHour", defaults.carb_rise_grams_per_hour),
            carb_rise_window=number("carbRiseWindow", defaults.carb_rise_window),
            minimum_duration=number("minimumDuration", defaults.minimum_duration),
            maximum_duration=number("maximumDuration", defaults.maximum_duration),
            restart_cooldown=number("restartCooldown", defaults.restart_cooldown),
            stale_after=number("staleAfter", defaults.stale_after),
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

        def number(key: str, fallback: float) -> float:
            try:
                value = float(raw.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return fallback if not math.isfinite(value) else value

        return AlertThresholds(
            low=number("low", defaults.low),
            high=number("high", defaults.high),
            stale_after=number("staleAfter", defaults.stale_after),
            prediction_horizon=number("predictionHorizon", defaults.prediction_horizon),
            prediction_lead_time=number("predictionLeadTime", defaults.prediction_lead_time),
            re_alert_interval=number("reAlertInterval", defaults.re_alert_interval),
        )


@dataclass(frozen=True)
class Device:
    """One registered phone, as it described itself."""

    device_id: str
    apns_token: Optional[str]
    push_to_start_token: Optional[str]
    activity_token: Optional[str]
    activity_episode_id: Optional[str]
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
                # A kind this build has never heard of. Ignored rather than
                # fatal: a newer app must not be able to break registration.
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
            activity_episode_id=raw.get("activityEpisodeID") or None,
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
