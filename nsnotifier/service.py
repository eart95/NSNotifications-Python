"""One tick: look at Nightscout, then tell each device what it needs to know.

The shape of a tick is deliberately flat — fetch once, then a loop over devices
that cannot throw — because the failure mode to avoid is one device's bad token
taking down everyone else's notifications. The old script had a single global
try-nothing structure and a single global cooldown timestamp shared by ten alert
kinds; a second phone would have silenced the first.

Everything decided here is decided by pure functions in ``alerts`` and
``episodes``. This module does the I/O and the bookkeeping, and it is written so
that the bookkeeping is the only thing you have to check when a decision looks
wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import httpx

from . import activity as activity_builder
from .alerts import Alert, derive as derive_alerts
from .apns import APNsClient, PushResult
from .config import Config
from .episodes import Decision, EpisodeInput, evaluate
from .models import (
    AlertKind,
    Device,
    Episode,
    EpisodeKind,
    EndReason,
    Reading,
    Treatment,
)
from .nightscout import NightscoutClient, NightscoutError
from .physiology import carbs_entering, carbs_on_board, insulin_on_board, momentum_forecast
from .store import Store

logger = logging.getLogger(__name__)

# How long the phone should consider a pushed activity state current before it
# dims. Matches `GlucoseActivityController.staleWindow` in the app.
ACTIVITY_STALE_WINDOW = 20 * 60
# How long a finished activity lingers before iOS removes it, so "Back in
# range" is actually read.
ACTIVITY_DISMISS_AFTER = 120


@dataclass
class TickResult:
    at: float = field(default_factory=time.time)
    readings: int = 0
    devices: int = 0
    alerts_sent: int = 0
    activity_pushes: int = 0
    refreshes_sent: int = 0
    tokens_pruned: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "readings": self.readings,
            "devices": self.devices,
            "alertsSent": self.alerts_sent,
            "activityPushes": self.activity_pushes,
            "refreshesSent": self.refreshes_sent,
            "tokensPruned": self.tokens_pruned,
            "error": self.error,
        }


class NotifierService:
    def __init__(self, config: Config, store: Store, nightscout: NightscoutClient, apns: APNsClient):
        self._config = config
        self._store = store
        self._nightscout = nightscout
        self._apns = apns
        self._last_tick: Optional[TickResult] = None

    @property
    def last_tick(self) -> Optional[TickResult]:
        return self._last_tick

    # --- the tick ---------------------------------------------------------

    async def tick(self, now: Optional[float] = None) -> TickResult:
        now = time.time() if now is None else now
        result = TickResult(at=now)

        try:
            readings, treatments = await asyncio.gather(
                self._nightscout.entries(now - self._config.nightscout.history_hours * 3600, now),
                self._nightscout.treatments(now - self._config.nightscout.treatment_hours * 3600),
            )
        except NightscoutError as error:
            # Nightscout being down is not this service being down, and it must
            # not look like a successful quiet tick. No heartbeat is sent, so a
            # dead-man's switch notices if it stays down.
            result.error = str(error)
            logger.error("tick: %s", error)
            self._last_tick = result
            return result

        result.readings = len(readings)
        registrations = await self._store.list_devices(
            max_age_seconds=self._config.registration_ttl_hours * 3600
        )
        result.devices = len(registrations)

        for raw in registrations:
            try:
                device = Device.from_registration(raw)
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("tick: skipping a malformed registration: %s", error)
                continue

            try:
                await self._process(device, readings, treatments, now, result)
            except Exception:  # noqa: BLE001 - one device must never take out the rest
                logger.exception("tick: device %s failed", device.device_id)

        await self._store.prune()
        await self._heartbeat()
        self._last_tick = result
        logger.info(
            "tick: %d readings, %d devices, %d alerts, %d activity pushes, %d refreshes, %d tokens pruned",
            result.readings,
            result.devices,
            result.alerts_sent,
            result.activity_pushes,
            result.refreshes_sent,
            result.tokens_pruned,
        )
        return result

    # --- per device -------------------------------------------------------

    async def _process(
        self,
        device: Device,
        readings: list[Reading],
        treatments: list[Treatment],
        now: float,
        result: TickResult,
    ) -> None:
        state = await self._store.get_state(device.device_id)
        last_fired = _epoch_map(state.get("lastFired"), AlertKind)
        last_ended = _epoch_map(state.get("lastEnded"), EpisodeKind)
        current = Episode.from_dict(state.get("episode"))

        # One forecast, reaching far enough for whichever rule looks furthest
        # ahead. See `physiology.momentum_forecast` for why it is momentum only.
        horizon = max(device.configuration.prediction_horizon, device.alert_thresholds.prediction_horizon)
        prediction = momentum_forecast(readings, now, horizon)

        iob = insulin_on_board(treatments, now)
        cob = carbs_on_board(treatments, now)
        entering = carbs_entering(treatments, now, device.configuration.carb_rise_window)

        # --- alerts -------------------------------------------------------

        if device.apns_token:
            for alert in derive_alerts(device, readings, prediction, last_fired, now):
                push = await self._send_alert(device, alert, now)
                if push.ok:
                    last_fired[alert.kind] = now
                    result.alerts_sent += 1
                elif push.token_is_dead:
                    # The device token is gone, so nothing else in this loop can
                    # succeed either. Stop, and let the next registration bring
                    # a live one.
                    await self._store.clear_token(device.device_id, "apnsToken")
                    result.tokens_pruned += 1
                    device = replace(device, apns_token=None)
                    break

        # --- episodes -----------------------------------------------------

        if device.live_activities_enabled:
            decision = evaluate(
                current=current,
                data=EpisodeInput(
                    readings=readings,
                    prediction=prediction,
                    carbs_entering_window=entering,
                    carbs_on_board=cob,
                    insulin_on_board=iob,
                ),
                configuration=device.configuration,
                last_ended=last_ended,
                now=now,
            )
            current = await self._apply(
                decision,
                device=device,
                readings=readings,
                prediction=prediction,
                cob=cob,
                iob=iob,
                last_ended=last_ended,
                now=now,
                result=result,
            )
        elif current is not None:
            # The user turned Live Activities off. Take down whatever is
            # running rather than leaving it there being wrong.
            await self._end_activity(device, current, EndReason.CANCELLED, readings, prediction, cob, iob, now, result)
            current = None

        # --- silent refresh -----------------------------------------------

        if await self._maybe_refresh(device, state, now):
            state["lastRefreshAt"] = now
            result.refreshes_sent += 1

        state["lastFired"] = {kind.value: at for kind, at in last_fired.items()}
        state["lastEnded"] = {kind.value: at for kind, at in last_ended.items()}
        state["episode"] = current.to_dict() if current else None
        await self._store.put_state(device.device_id, state)

    # --- alerts -----------------------------------------------------------

    async def _send_alert(self, device: Device, alert: Alert, now: float) -> PushResult:
        assert device.apns_token
        envelope = {
            "schema": 1,
            "purpose": "alert",
            "id": alert.identifier,
            "sentAt": now,
            "alertKind": alert.kind.value,
        }
        if alert.mgdl is not None:
            envelope["mgdL"] = alert.mgdl

        push = await self._apns.send_alert(
            token=device.apns_token,
            production=device.is_production,
            title=alert.title,
            body=alert.body,
            envelope=envelope,
            # One outstanding notification per kind. A phone that comes back
            # online after half an hour should not receive four "Glucose low"
            # banners describing four readings it can no longer act on.
            collapse_id=alert.kind.value,
            interruption_level=alert.interruption_level,
        )
        await self._store.record_push(device.device_id, f"alert.{alert.kind.value}", push.status, push.reason)
        return push

    # --- episodes ---------------------------------------------------------

    async def _apply(
        self,
        decision: Decision,
        *,
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        last_ended: dict[EpisodeKind, float],
        now: float,
        result: TickResult,
    ) -> Optional[Episode]:
        if decision.action == "idle":
            return None

        if decision.action == "update":
            episode = decision.episode.advanced()  # type: ignore[union-attr]
            pushed = await self._update_activity(device, episode, readings, prediction, cob, iob, now, result)
            # A failed update leaves the sequence where it was, so the next
            # attempt does not skip a number the phone would then use as its
            # floor.
            return episode if pushed else decision.episode

        if decision.action == "start":
            return await self._start_activity(decision.episode, device, readings, prediction, cob, iob, now, result)  # type: ignore[arg-type]

        if decision.action == "end":
            await self._end_activity(
                device, decision.episode, decision.reason or EndReason.CANCELLED, readings, prediction, cob, iob, now, result  # type: ignore[arg-type]
            )
            last_ended[decision.episode.kind] = now  # type: ignore[union-attr]
            return None

        if decision.action == "replace":
            await self._end_activity(
                device, decision.episode, decision.reason or EndReason.SUPERSEDED, readings, prediction, cob, iob, now, result  # type: ignore[arg-type]
            )
            last_ended[decision.episode.kind] = now  # type: ignore[union-attr]
            return await self._start_activity(decision.starting, device, readings, prediction, cob, iob, now, result)  # type: ignore[arg-type]

        return decision.live

    async def _start_activity(
        self,
        episode: Episode,
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
    ) -> Optional[Episode]:
        """Begin an activity on a phone that may not be running the app.

        This is the whole point of the service. A local start is impossible from
        the background — ActivityKit refuses one and offers no way to queue it —
        so without a push-to-start token a hypo at 4 a.m. reaches the Lock Screen
        only when the user next opens the app, which is to say afterwards.
        """
        if not device.push_to_start_token:
            logger.info("start: %s has no push-to-start token yet", device.device_id)
            return None

        episode = episode.advanced()
        state = self._build_state(episode, device, readings, prediction, cob, iob, now)
        if state is None:
            return None

        push = await self._apns.send_live_activity(
            token=device.push_to_start_token,
            production=device.is_production,
            event="start",
            content_state=state,
            attributes_type=activity_builder.ATTRIBUTES_TYPE,
            attributes=activity_builder.attributes_for(episode),
            stale_at=self._stale_at(readings, now),
            # A hypo announces itself; a meal does not. An activity that
            # appears silently on a locked phone at night has not warned
            # anyone, and one that buzzes for every plate of pasta gets the
            # whole feature turned off.
            alert=(
                {"title": state["headline"], "body": state["detail"]}
                if episode.kind is EpisodeKind.HYPO_RISK
                else None
            ),
            relevance_score=100 if episode.kind is EpisodeKind.HYPO_RISK else 50,
            timestamp=now,
        )
        await self._store.record_push(device.device_id, f"activity.start.{episode.kind.value}", push.status, push.reason)
        result.activity_pushes += 1

        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "pushToStartToken")
            result.tokens_pruned += 1
            return None
        if not push.ok:
            return None
        return episode

    async def _update_activity(
        self,
        device: Device,
        episode: Episode,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
    ) -> bool:
        # Only ever address the activity the phone says is running. After a
        # push-to-start the phone has to come back with the new activity's own
        # token, and until it does there is nothing here to update — pushing to
        # the previous episode's token would update an activity that is gone.
        if not device.activity_token or device.activity_episode_id != episode.identifier:
            return False

        state = self._build_state(episode, device, readings, prediction, cob, iob, now)
        if state is None:
            return False

        push = await self._apns.send_live_activity(
            token=device.activity_token,
            production=device.is_production,
            event="update",
            content_state=state,
            stale_at=self._stale_at(readings, now),
            relevance_score=100 if episode.kind is EpisodeKind.HYPO_RISK else 50,
            collapse_id=episode.identifier,
            timestamp=now,
        )
        await self._store.record_push(device.device_id, f"activity.update.{episode.kind.value}", push.status, push.reason)
        result.activity_pushes += 1

        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "activityToken")
            result.tokens_pruned += 1
        return push.ok

    async def _end_activity(
        self,
        device: Device,
        episode: Episode,
        reason: EndReason,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
    ) -> None:
        closing = self._build_state(episode.advanced(), device, readings, prediction, cob, iob, now)

        if device.activity_token and device.activity_episode_id == episode.identifier and closing is not None:
            push = await self._apns.send_live_activity(
                token=device.activity_token,
                production=device.is_production,
                event="end",
                content_state=activity_builder.with_farewell(closing, episode.kind, reason),
                # Held briefly rather than yanked: an activity that simply
                # vanishes leaves the user unsure whether the low passed or the
                # app died, and "Back in range" is the only good news this
                # system ever gets to deliver.
                dismiss_at=now + ACTIVITY_DISMISS_AFTER if reason is EndReason.RECOVERED else now,
                timestamp=now,
            )
            await self._store.record_push(device.device_id, f"activity.end.{episode.kind.value}", push.status, push.reason)
            result.activity_pushes += 1
            if push.ok:
                return
            if push.token_is_dead:
                await self._store.clear_token(device.device_id, "activityToken")
                result.tokens_pruned += 1

        # No usable activity token, or the end push failed. Fall back to asking
        # the app to stand down: without this the activity sits on the Lock
        # Screen until iOS expires it, still describing a hypo that finished an
        # hour ago.
        if device.apns_token:
            push = await self._apns.send_background(
                token=device.apns_token,
                production=device.is_production,
                envelope={
                    "schema": 1,
                    "purpose": "standDown",
                    "id": f"standDown.{episode.identifier}",
                    "sentAt": now,
                },
            )
            await self._store.record_push(device.device_id, "activity.standDown", push.status, push.reason)

    def _build_state(
        self,
        episode: Episode,
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
    ) -> Optional[dict[str, Any]]:
        return activity_builder.build_state(
            episode=episode,
            readings=readings,
            prediction=prediction,
            configuration=device.configuration,
            unit=device.unit,
            carbs_on_board=cob,
            insulin_on_board=iob,
            now=now,
        )

    @staticmethod
    def _stale_at(readings: list[Reading], now: float) -> float:
        """When the phone should start dimming the activity.

        Anchored to the reading, not to the push: the question is how old the
        *data* is, and a push carrying no new reading must not refresh that
        claim. Floored just ahead of now so a state is never handed over
        already stale, which renders as broken rather than as outdated.
        """
        latest = readings[-1].at if readings else now
        return max(latest + ACTIVITY_STALE_WINDOW, now + 120)

    # --- silent refresh ---------------------------------------------------

    async def _maybe_refresh(self, device: Device, state: dict[str, Any], now: float) -> bool:
        """Occasionally ask the app to sync, so its own local safety net stays armed.

        Rationed rather than sent every tick. iOS budgets background pushes per
        app per day and starts dropping them silently once an app is spending
        too many, and the ones worth having are the ones that keep the app's
        local staleness alert re-armed — not a wake every five minutes to
        re-read a number the service already knows.
        """
        interval = self._config.refresh_push_interval
        if interval <= 0 or not device.apns_token:
            return False
        last = float(state.get("lastRefreshAt") or 0)
        if now - last < interval:
            return False

        push = await self._apns.send_background(
            token=device.apns_token,
            production=device.is_production,
            envelope={
                "schema": 1,
                "purpose": "refresh",
                "id": f"refresh.{int(now // interval)}",
                "sentAt": now,
            },
        )
        await self._store.record_push(device.device_id, "refresh", push.status, push.reason)
        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "apnsToken")
        return push.ok

    # --- heartbeat --------------------------------------------------------

    async def _heartbeat(self) -> None:
        """Tell a dead-man's switch that a tick completed.

        This is the single most valuable line in the file. Everything else here
        fails loudly; a *stopped* notifier fails silently, and the user has by
        then delegated noticing to it. The absence of these pings is what raises
        the alarm — a cron job that stopped firing, a container that OOMed, a
        deploy that never came back.
        """
        if not self._config.heartbeat_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(self._config.heartbeat_url)
        except httpx.HTTPError as error:
            logger.warning("heartbeat: failed: %s", error)


def _epoch_map(raw: Any, enum_type: Any) -> dict[Any, float]:
    result: dict[Any, float] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        try:
            result[enum_type(key)] = float(value)
        except (ValueError, TypeError):
            continue
    return result
