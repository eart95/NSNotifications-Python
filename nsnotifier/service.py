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

# An episode can be running with no activity on the phone to show it: the
# push-to-start token had not arrived when it began, the start push failed, or
# the phone came back with nothing. Those are all recoverable, so a start is
# retried rather than the episode spending its whole life on the update path
# skipping every push.
#
# Bounded, though. A phone with Live Activities switched off at the OS level
# accepts a start push and does nothing with it, and there is no way to tell
# that apart from a phone that is merely slow — so after a few attempts the
# episode gives up and carries on as an alert-only episode.
ACTIVITY_START_RETRY_AFTER = 8 * 60
ACTIVITY_START_MAX_ATTEMPTS = 4


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
                device_state=state,
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
        device_state: dict[str, Any],
        now: float,
        result: TickResult,
    ) -> Optional[Episode]:
        if decision.action == "idle":
            return None

        if decision.action == "update":
            running = decision.episode  # type: ignore[union-attr]
            # Before pushing an update, check there is anything to update. An
            # episode whose activity was never started — no push-to-start token
            # at the time, a start push that failed, a phone that never came
            # back with a token — would otherwise spend its entire life here,
            # skipping every push for a Lock Screen card that does not exist.
            if self._should_retry_start(device, running, device_state, now):
                started = await self._start_activity(
                    running, device, readings, prediction, cob, iob, now, result, state=device_state
                )
                return started or running

            episode = running.advanced()
            pushed = await self._update_activity(device, episode, readings, prediction, cob, iob, now, result)
            # A failed update leaves the sequence where it was, so the next
            # attempt does not skip a number the phone would then use as its
            # floor.
            return episode if pushed else running

        if decision.action == "start":
            return await self._start_activity(
                decision.episode, device, readings, prediction, cob, iob, now, result, state=device_state  # type: ignore[arg-type]
            )

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
            return await self._start_activity(
                decision.starting, device, readings, prediction, cob, iob, now, result, state=device_state  # type: ignore[arg-type]
            )

        return decision.live

    # --- has this episode actually got an activity? -----------------------

    @staticmethod
    def _should_retry_start(
        device: Device,
        episode: Episode,
        state: dict[str, Any],
        now: float,
    ) -> bool:
        """Whether to (re)send a start push for an episode already under way.

        Not the same question as "did the start push succeed". A 200 from APNs
        means Apple accepted it, not that a Live Activity exists — the phone
        still has to be running Live Activities, create it, and come back with
        its token. The only evidence that actually counts is the phone
        registering a token paired with *this* episode.
        """
        if not device.live_activities_enabled or not device.push_to_start_token:
            return False
        # The phone has vouched for this episode: there is something to update.
        if device.activity_token and device.activity_episode_id == episode.identifier:
            return False

        ledger = state.get("activityStart") or {}
        if ledger.get("episode") != episode.identifier:
            # Never started for this episode at all.
            return True
        if int(ledger.get("attempts") or 0) >= ACTIVITY_START_MAX_ATTEMPTS:
            return False
        return (now - float(ledger.get("at") or 0)) >= ACTIVITY_START_RETRY_AFTER

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
        state: Optional[dict[str, Any]] = None,
    ) -> Optional[Episode]:
        """Begin an activity on a phone that may not be running the app.

        This is the whole point of the service. A local start is impossible from
        the background — ActivityKit refuses one and offers no way to queue it —
        so without a push-to-start token a hypo at 4 a.m. reaches the Lock Screen
        only when the user next opens the app, which is to say afterwards.
        """
        if not device.push_to_start_token:
            logger.info(
                "start: %s has no push-to-start token yet — nothing can put a Live Activity "
                "on this phone until the app registers one",
                device.device_id,
            )
            await self._store.record_push(
                device.device_id, "activity.start.skipped", 0, "no pushToStartToken registered"
            )
            return None

        if state is not None:
            ledger = state.get("activityStart") or {}
            attempts = int(ledger.get("attempts") or 0) if ledger.get("episode") == episode.identifier else 0
            state["activityStart"] = {
                "episode": episode.identifier,
                "at": now,
                "attempts": attempts + 1,
            }
            if attempts:
                logger.info(
                    "start: retrying %s for %s (attempt %d) — the phone has not registered an "
                    "activity token for it",
                    episode.identifier,
                    device.device_id,
                    attempts + 1,
                )

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
        #
        # A skip here is correct, and it is also the single most confusing thing
        # this service does: from the outside it looks identical to a Lock
        # Screen that has simply stopped working. So it says which half is
        # missing, every time, rather than returning quietly.
        if not device.activity_token:
            logger.info(
                "activity: no update for %s — the phone has not registered an activity token "
                "(episode %s). Nothing to address.",
                device.device_id,
                episode.identifier,
            )
            await self._store.record_push(
                device.device_id, "activity.update.skipped", 0, "no activityToken registered"
            )
            return False
        if device.activity_episode_id != episode.identifier:
            logger.info(
                "activity: no update for %s — registered episode %s, current episode %s. "
                "The phone is describing a different activity from the one this service is running.",
                device.device_id,
                device.activity_episode_id or "-",
                episode.identifier,
            )
            await self._store.record_push(
                device.device_id,
                "activity.update.skipped",
                0,
                f"episode mismatch: registered {device.activity_episode_id or '-'}, current {episode.identifier}",
            )
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

    # --- diagnostics ------------------------------------------------------

    async def send_test(self, device_id: str, now: Optional[float] = None) -> dict[str, Any]:
        """Push a test alert, and a test Live Activity update if one is running.

        The problem this solves: every other way of answering "does a push from
        this service actually reach that phone" involves waiting for a hypo.
        APNs returning 200 does not answer it — the payload can still be
        dropped on the device, silently, for half a dozen reasons — so the only
        real test is to send one and have a person look at the screen.
        """
        now = time.time() if now is None else now
        devices = {d.get("deviceID"): d for d in await self._store.list_devices()}
        raw = devices.get(device_id)
        if raw is None:
            return {"error": f"no device registered as {device_id}"}

        device = Device.from_registration(raw)
        report: dict[str, Any] = {
            "deviceID": device_id,
            "environment": device.environment,
            "hasAPNsToken": bool(device.apns_token),
            "hasPushToStartToken": bool(device.push_to_start_token),
            "activityEpisodeID": device.activity_episode_id,
        }

        if device.apns_token:
            push = await self._apns.send_alert(
                token=device.apns_token,
                production=device.is_production,
                title="Gloo test",
                body="If you can see this, alerts from the notification service reach this phone.",
                envelope={
                    "schema": 1,
                    "purpose": "alert",
                    "id": f"test.{int(now)}",
                    "sentAt": now,
                },
            )
            await self._store.record_push(device_id, "test.alert", push.status, push.reason)
            report["alert"] = {"status": push.status, "reason": push.reason}
        else:
            report["alert"] = {"skipped": "no apnsToken registered"}

        # An update, not a start: a test that started a real Live Activity would
        # put a hypo card on someone's Lock Screen for a situation that is not
        # happening.
        state = await self._store.get_state(device_id)
        episode = Episode.from_dict(state.get("episode"))
        if device.activity_token and episode and device.activity_episode_id == episode.identifier:
            content = self._build_state(episode.advanced(), device, [], [], None, None, now)
            if content is None:
                report["liveActivity"] = {"skipped": "no readings to build a state from"}
            else:
                push = await self._apns.send_live_activity(
                    token=device.activity_token,
                    production=device.is_production,
                    event="update",
                    content_state=content,
                    timestamp=now,
                )
                await self._store.record_push(device_id, "test.activity", push.status, push.reason)
                report["liveActivity"] = {"status": push.status, "reason": push.reason}
        else:
            report["liveActivity"] = {
                "skipped": "no Live Activity is running that this phone has registered a token for"
            }

        return report

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
