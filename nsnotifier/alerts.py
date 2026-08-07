"""Which visible alerts to push, mirroring ``GlucoseAlert.derive``.

The Swift version has a constraint this one does not: a local notification
fires at a *time*, never on a condition, so the app has to schedule a predicted
low twenty minutes ahead and hope the forecast it was scheduled from still
holds. Here the service is awake and looking, so an alert is simply decided and
sent at the moment it becomes true.

That difference is the whole argument for the service, and it is also the reason
the two must not both shout. The phone damps its own copy of a kind for
``re_alert_interval`` after it hears that this service delivered one — see
``GlucoseAlertScheduler.recordRemoteDelivery``. Nothing damps this service in
the other direction, on purpose: a local alert may never actually be delivered,
and going quiet on the strength of one the user never saw would turn a silent
phone into a silent everything.

One rule below is worth calling out because it looks like an omission. There is
no "extreme high" or "rapid rise" or "out of range for 45 minutes" any more.
The old script had ten alert kinds with a priority ladder and per-kind cooldowns
that — because of a bug in how the state was keyed — all shared one timestamp.
Ten kinds is not ten times the information; it is a phone that buzzes so often
that the one alert that mattered arrives looking like all the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .episodes import predicted_crossing
from .models import (
    AlertKind,
    AlertThresholds,
    Device,
    Reading,
    TrendDirection,
    Unit,
    trend_from,
)


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    title: str
    body: str
    mgdl: Optional[float]
    # Stable per decision, so a redelivery is recognisable as one. Bucketed to
    # the reading it is about rather than to `now`, so two ticks that see the
    # same reading produce the same id.
    identifier: str

    @property
    def interruption_level(self) -> str:
        # Time-sensitive pierces Focus when the app carries the capability. It
        # never overrides the ringer switch — that needs Critical Alerts, which
        # this app does not have, and claiming otherwise in a footer would be
        # the most dangerous kind of documentation.
        return "time-sensitive"


def derive(
    device: Device,
    readings: Sequence[Reading],
    prediction: Sequence[Reading],
    last_fired: Mapping[AlertKind, float],
    now: float,
) -> list[Alert]:
    """Everything that should be sent to this device right now.

    Pure: the caller supplies ``now`` and the last-fired times, so the same
    inputs always produce the same answer and the whole rule set is testable
    without an APNs connection.
    """
    if not device.alerts_enabled or not readings:
        return []

    thresholds = device.alert_thresholds
    unit = device.unit
    latest = readings[-1]
    age = now - latest.at
    is_fresh = age <= thresholds.stale_after
    trend = trend_from(readings)

    def enabled(kind: AlertKind) -> bool:
        return kind in device.enabled_alert_kinds

    def damped(kind: AlertKind) -> bool:
        fired = last_fired.get(kind)
        return fired is not None and (now - fired) < thresholds.re_alert_interval

    alerts: list[Alert] = []

    # --- Measured, right now -------------------------------------------
    #
    # Only from a fresh reading: alerting on a value that arrived an hour ago
    # tells you where you *were*, which is worse than silence.

    if is_fresh and enabled(AlertKind.MEASURED_LOW) and latest.mgdl < thresholds.low and not damped(AlertKind.MEASURED_LOW):
        alerts.append(
            Alert(
                kind=AlertKind.MEASURED_LOW,
                title=AlertKind.MEASURED_LOW.title,
                body=_measured_body(latest.mgdl, trend, unit),
                mgdl=latest.mgdl,
                identifier=f"{AlertKind.MEASURED_LOW.value}.{int(latest.at)}",
            )
        )

    if is_fresh and enabled(AlertKind.MEASURED_HIGH) and latest.mgdl > thresholds.high and not damped(AlertKind.MEASURED_HIGH):
        alerts.append(
            Alert(
                kind=AlertKind.MEASURED_HIGH,
                title=AlertKind.MEASURED_HIGH.title,
                body=_measured_body(latest.mgdl, trend, unit),
                mgdl=latest.mgdl,
                identifier=f"{AlertKind.MEASURED_HIGH.value}.{int(latest.at)}",
            )
        )

    # --- Predicted low ---------------------------------------------------
    #
    # Suppressed while already low: the measured alert has that covered, and
    # two notifications about one hypo is one too many.

    if (
        is_fresh
        and enabled(AlertKind.PREDICTED_LOW)
        and latest.mgdl >= thresholds.low
        and not damped(AlertKind.PREDICTED_LOW)
    ):
        crossing = predicted_crossing(prediction, thresholds.low, thresholds.prediction_horizon, now)
        # Only warn once the crossing is inside the lead time. Earlier than
        # that and there is nothing to do yet; the service will be back in five
        # minutes and the forecast will have firmed up or gone away.
        if crossing is not None and (crossing.at - now) <= thresholds.prediction_lead_time:
            alerts.append(
                Alert(
                    kind=AlertKind.PREDICTED_LOW,
                    title=AlertKind.PREDICTED_LOW.title,
                    body=f"Forecast dips to {unit.label(crossing.mgdl)} {unit.suffix}.",
                    mgdl=crossing.mgdl,
                    identifier=f"{AlertKind.PREDICTED_LOW.value}.{int(crossing.at)}",
                )
            )

    # --- Staleness -------------------------------------------------------
    #
    # The app does this one better than a server can, because a local
    # notification fires precisely when nothing is arriving to trigger
    # anything. This is the backstop for a phone that is off, or has had the
    # app force-quit, or never got the wake.

    if enabled(AlertKind.DATA_STALE) and not is_fresh and not damped(AlertKind.DATA_STALE):
        minutes = int(age // 60)
        alerts.append(
            Alert(
                kind=AlertKind.DATA_STALE,
                title=AlertKind.DATA_STALE.title,
                body=f"Nothing for {minutes} minutes. Check your sensor or the app it uploads through.",
                mgdl=None,
                identifier=f"{AlertKind.DATA_STALE.value}.{int(latest.at)}",
            )
        )

    return alerts


def _measured_body(mgdl: float, trend: Optional[TrendDirection], unit: Unit) -> str:
    value = f"{unit.label(mgdl)} {unit.suffix}"
    if trend is None:
        return value + "."
    return f"{value}, {trend.spoken_movement}."
