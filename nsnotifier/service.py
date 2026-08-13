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

Three things about Live Activities are worth having in mind before reading the
episode half:

* **There is one card per device, and it is called a session.** It is started
  once, updated for as long as anything is worth showing, and ended once. The
  *kind* of thing it is about — a low, a fast movement, a meal, a card the user
  asked for — is content, not identity, so a change of kind is one ordinary
  update push rather than an end and a push-to-start.
* **Each kind sets its own cadence.** A low is refreshed every two minutes and
  a meal every five, so the tick runs faster than any of them and pushes only
  what is due.
* **A Live Activity push runs no app code at all.** iOS hands the payload
  straight to the widget extension; the app does not wake, does not sync, and
  does not know it happened. The only way the app's own data moves in step with
  the card is a silent background push sent *alongside* the activity push,
  which is what `_maybe_refresh` does with `paired=True`.
"""

from __future__ import annotations

import asyncio
import logging
import math
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
    EndReason,
    Episode,
    EpisodeKind,
    Reading,
    Session,
    Treatment,
)
from .nightscout import NightscoutClient, NightscoutError
from .physiology import carbs_entering, carbs_on_board, insulin_on_board, momentum_forecast
from .store import Store

logger = logging.getLogger(__name__)

# How long a finished activity lingers before iOS removes it, so "Back in
# range" is actually read.
ACTIVITY_DISMISS_AFTER = 120

# How much slack a pushed state gets before the phone dims it.
#
# Anchored to the episode's own cadence rather than a constant: a low is
# refreshed every two minutes, so a card that has had nothing for six is
# genuinely behind, while a meal card at five-minute cadence is not.
STALE_WINDOW_MULTIPLE = 3
MINIMUM_STALE_WINDOW = 10 * 60

# A session can be running with no activity on the phone to show it: the
# push-to-start token had not arrived when it began, the start push failed, or
# the phone came back with nothing. Those are all recoverable, so a start is
# retried rather than the session spending its whole life on the update path
# skipping every push.
#
# Bounded, though. A phone with Live Activities switched off at the OS level
# accepts a start push and does nothing with it, and there is no way to tell
# that apart from a phone that is merely slow — so after a few attempts the
# session gives up.
ACTIVITY_START_RETRY_AFTER = 8 * 60
ACTIVITY_START_MAX_ATTEMPTS = 4

# The shortest gap between two silent "go and sync" pushes while a card is on
# screen. Just under the fastest cadence any kind asks for, so a start and an
# update landing in the same tick send one refresh rather than two.
PAIRED_REFRESH_MIN_GAP = 110

# Bounds on a manually requested card's lifetime.
#
# The ceiling is iOS's, not ours: ActivityKit ends an activity after eight
# hours whatever anyone asks for, so accepting a larger number would be
# accepting a promise that cannot be kept. The floor stops a zero or a typo
# producing a card that goes stale before anyone has looked at it.
MANUAL_START_MIN_DURATION = 5 * 60
MANUAL_START_MAX_DURATION = 8 * 3600
# How far back to look for a reading to put on the card.
MANUAL_START_HISTORY = 3600


@dataclass(frozen=True)
class ManualStartResult:
    """What `POST /v1/devices/{id}/request-start` should answer with.

    The HTTP status is decided here rather than in the route because every one
    of them is a fact about the *device or the push*, not about the request:
    whether the row exists, whether it can be addressed, what Apple said. The
    route's job is to render it.
    """

    status: int
    detail: str
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200

    def as_dict(self) -> dict[str, Any]:
        return {"detail": self.detail, **self.body}


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
        session = Session.from_dict(state.get("session"))

        # One forecast, reaching far enough for whichever rule looks furthest
        # ahead. See `physiology.momentum_forecast` for why it is momentum only.
        horizon = max(device.configuration.prediction_horizon, device.alert_thresholds.prediction_horizon)
        prediction = momentum_forecast(readings, now, horizon)

        iob = insulin_on_board(treatments, now)
        cob = carbs_on_board(treatments, now)
        entering = carbs_entering(treatments, now, device.configuration.meal.window)

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

        # --- the card -----------------------------------------------------

        pushed_activity = False

        if device.live_activities_enabled:
            decision = evaluate(
                session=session,
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
            session, pushed_activity = await self._apply(
                decision,
                previous=session,
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
        elif session is not None:
            # The user turned Live Activities off. Take down whatever is
            # running rather than leaving it there being wrong.
            await self._end_session(device, session, EndReason.CANCELLED, readings, prediction, cob, iob, now, result)
            last_ended[session.episode.kind] = now
            session = None

        # --- keeping the app in step --------------------------------------

        if await self._maybe_refresh(device, state, now, paired=pushed_activity, session=session):
            state["lastRefreshAt"] = now
            result.refreshes_sent += 1

        state["lastFired"] = {kind.value: at for kind, at in last_fired.items()}
        state["lastEnded"] = {kind.value: at for kind, at in last_ended.items()}
        state["session"] = session.to_dict() if session else None
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

    # --- the card ---------------------------------------------------------

    async def _apply(
        self,
        decision: Decision,
        *,
        previous: Optional[Session],
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        last_ended: dict[EpisodeKind, float],
        device_state: dict[str, Any],
        now: float,
        result: TickResult,
    ) -> tuple[Optional[Session], bool]:
        """Carry out one decision. Returns the session to store, and whether an
        activity push actually went out."""
        if decision.action == "idle":
            return None, False

        if decision.action == "start":
            session = await self._start_session(
                decision.episode, device, readings, prediction, cob, iob, now, result, state=device_state
            )
            return session, session is not None

        if decision.action == "end":
            ending = decision.session or previous
            if ending is not None:
                await self._end_session(
                    device,
                    ending,
                    decision.reason or EndReason.CANCELLED,
                    readings,
                    prediction,
                    cob,
                    iob,
                    now,
                    result,
                )
                last_ended[ending.episode.kind] = now
            return None, ending is not None

        session = decision.session
        if session is None:
            return previous, False

        # The card exists in this service's head but not on the phone: no start
        # push ever landed, or the phone never came back with a token for it.
        # Same bounded retry as a fresh start.
        if self._should_retry_start(device, session, device_state, now):
            started = await self._start_session(
                session.episode,
                device,
                readings,
                prediction,
                cob,
                iob,
                now,
                result,
                state=device_state,
                session_id=session.id,
                started_at=session.started_at,
                sequence=session.sequence,
                suspended=session.suspended,
            )
            return (started or session), started is not None

        previous_kind = previous.episode.kind if previous else None
        switched = previous_kind is not None and previous_kind is not session.episode.kind
        if switched:
            # A change of kind is never held back for the cadence. The card
            # saying "After a meal" while glucose is 63 is the exact failure
            # this rework exists to remove.
            logger.info(
                "activity: %s switching %s -> %s on session %s",
                device.device_id,
                previous_kind.value if previous_kind else "-",
                session.episode.kind.value,
                session.id,
            )
        elif not self._is_push_due(session, device, device_state, now):
            # Nothing to say yet. The tick runs faster than any kind's cadence
            # so that a switch is noticed quickly; the pushes themselves are
            # rationed to what each kind asked for.
            return session, False

        advanced = session.advanced()
        pushed = await self._update_activity(
            device, advanced, readings, prediction, cob, iob, now, result, announce=switched
        )
        if pushed:
            device_state["lastActivityPushAt"] = now
            return advanced, True
        # A failed push leaves the sequence where it was, so the next attempt
        # does not skip a number the phone would then treat as its floor.
        return session, False

    def _is_push_due(
        self,
        session: Session,
        device: Device,
        state: dict[str, Any],
        now: float,
    ) -> bool:
        last = float(state.get("lastActivityPushAt") or 0)
        interval = device.configuration.update_interval(session.episode)
        # Half a tick of slack, so a cadence that is a whole multiple of the
        # poll interval does not slip a whole tick every time.
        return (now - last) >= (interval - self._config.poll_interval / 2)

    # --- has this session actually got an activity? -----------------------

    @staticmethod
    def _should_retry_start(
        device: Device,
        session: Session,
        state: dict[str, Any],
        now: float,
    ) -> bool:
        """Whether to (re)send a start push for a session already under way.

        Not the same question as "did the start push succeed". A 200 from APNs
        means Apple accepted it, not that a Live Activity exists — the phone
        still has to be running Live Activities, create it, and come back with
        its token. The only evidence that counts is the phone registering a
        token paired with *this session*.
        """
        if not device.live_activities_enabled or not device.push_to_start_token:
            return False
        # The phone has vouched for this session: there is something to update.
        if device.activity_token and device.activity_session_id == session.id:
            return False

        ledger = state.get("activityStart") or {}
        if ledger.get("session") != session.id:
            # Never started for this session at all.
            return True
        if int(ledger.get("attempts") or 0) >= ACTIVITY_START_MAX_ATTEMPTS:
            return False
        return (now - float(ledger.get("at") or 0)) >= ACTIVITY_START_RETRY_AFTER

    @staticmethod
    def _alert_for(content: dict[str, Any], kind: EpisodeKind) -> dict[str, str]:
        """The alert a start — or a switch — carries.

        Required by ActivityKit on a start, so for that the only question is how
        loud it is. A low gets a sound; everything else gets the same words
        silently, which lights the screen without waking anyone.
        """
        alert = {"title": content["headline"], "body": content["detail"]}
        if kind is EpisodeKind.LOW:
            alert["sound"] = "default"
        return alert

    async def _start_session(
        self,
        episode: Optional[Episode],
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
        started_at: Optional[float] = None,
        sequence: int = 0,
        suspended: tuple[Episode, ...] = (),
    ) -> Optional[Session]:
        """Put a card on a phone that may not be running the app.

        This is the whole point of the service. A local start is impossible from
        the background — ActivityKit refuses one and offers no way to queue it —
        so without a push-to-start token a low at 4 a.m. reaches the Lock Screen
        only when the user next opens the app, which is to say afterwards.
        """
        if episode is None:
            return None

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

        started_at = now if started_at is None else started_at
        session = Session(
            id=session_id or Session.identifier(started_at),
            started_at=started_at,
            sequence=sequence + 1,
            episode=episode,
            suspended=suspended,
        )

        if state is not None:
            ledger = state.get("activityStart") or {}
            attempts = int(ledger.get("attempts") or 0) if ledger.get("session") == session.id else 0
            state["activityStart"] = {"session": session.id, "at": now, "attempts": attempts + 1}
            if attempts:
                logger.info(
                    "start: retrying %s for %s (attempt %d) — the phone has not registered an "
                    "activity token for it",
                    session.id,
                    device.device_id,
                    attempts + 1,
                )

        content = self._build_state(session, device, readings, prediction, cob, iob, now)
        if content is None:
            return None

        push = await self._apns.send_live_activity(
            token=device.push_to_start_token,
            production=device.is_production,
            event="start",
            content_state=content,
            attributes_type=activity_builder.ATTRIBUTES_TYPE,
            attributes=activity_builder.attributes_for(session),
            stale_at=self._stale_at(session, device, readings, now),
            # Always an alert, because ActivityKit requires one on a start and
            # discards a start push without it — accepted by APNs with a 200,
            # gone by the time it reaches the device.
            alert=self._alert_for(content, episode.kind),
            relevance_score=100 if episode.kind is EpisodeKind.LOW else 50,
            timestamp=now,
        )
        await self._store.record_push(
            device.device_id, f"activity.start.{episode.kind.value}", push.status, push.reason
        )
        result.activity_pushes += 1

        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "pushToStartToken")
            result.tokens_pruned += 1
            return None
        if not push.ok:
            return None
        if state is not None:
            state["lastActivityPushAt"] = now
        return session

    async def _update_activity(
        self,
        device: Device,
        session: Session,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
        announce: bool = False,
    ) -> bool:
        # Only ever address the activity the phone says is running. After a
        # push-to-start the phone has to come back with the new activity's own
        # token, and until it does there is nothing here to update.
        #
        # A skip here is correct, and it is also the single most confusing thing
        # this service does: from the outside it looks identical to a Lock
        # Screen that has simply stopped working. So it says which half is
        # missing, every time, rather than returning quietly.
        if not device.activity_token:
            logger.info(
                "activity: no update for %s — the phone has not registered an activity token "
                "(session %s). Nothing to address.",
                device.device_id,
                session.id,
            )
            await self._store.record_push(
                device.device_id, "activity.update.skipped", 0, "no activityToken registered"
            )
            return False
        if device.activity_session_id != session.id:
            logger.info(
                "activity: no update for %s — registered session %s, current session %s. "
                "The phone is describing a different card from the one this service is running.",
                device.device_id,
                device.activity_session_id or "-",
                session.id,
            )
            await self._store.record_push(
                device.device_id,
                "activity.update.skipped",
                0,
                f"session mismatch: registered {device.activity_session_id or '-'}, current {session.id}",
            )
            return False

        content = self._build_state(session, device, readings, prediction, cob, iob, now)
        if content is None:
            return False

        push = await self._apns.send_live_activity(
            token=device.activity_token,
            production=device.is_production,
            event="update",
            content_state=content,
            stale_at=self._stale_at(session, device, readings, now),
            # A card that changes what it is about should be noticed. Ordinary
            # refreshes stay silent — one that buzzed every two minutes during a
            # low would get the whole feature switched off.
            alert=self._alert_for(content, session.episode.kind) if announce else None,
            relevance_score=100 if session.episode.kind is EpisodeKind.LOW else 50,
            collapse_id=session.id,
            timestamp=now,
        )
        await self._store.record_push(
            device.device_id, f"activity.update.{session.episode.kind.value}", push.status, push.reason
        )
        result.activity_pushes += 1

        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "activityToken")
            result.tokens_pruned += 1
        return push.ok

    async def _end_session(
        self,
        device: Device,
        session: Session,
        reason: EndReason,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
        result: TickResult,
        farewell: Optional[tuple[str, str]] = None,
    ) -> None:
        closing = self._build_state(session.advanced(), device, readings, prediction, cob, iob, now)

        if device.activity_token and device.activity_session_id == session.id and closing is not None:
            push = await self._apns.send_live_activity(
                token=device.activity_token,
                production=device.is_production,
                event="end",
                content_state=(
                    {**closing, "headline": farewell[0], "detail": farewell[1]}
                    if farewell is not None
                    else activity_builder.with_farewell(closing, session.episode.kind, reason)
                ),
                # Held briefly rather than yanked: an activity that simply
                # vanishes leaves the user unsure whether the low passed or the
                # app died, and "Back in range" is the only good news this
                # system ever gets to deliver.
                dismiss_at=now + ACTIVITY_DISMISS_AFTER
                if reason in (EndReason.RECOVERED, EndReason.COMPLETED)
                else now,
                timestamp=now,
            )
            await self._store.record_push(
                device.device_id, f"activity.end.{session.episode.kind.value}", push.status, push.reason
            )
            result.activity_pushes += 1
            if push.ok:
                return
            if push.token_is_dead:
                await self._store.clear_token(device.device_id, "activityToken")
                result.tokens_pruned += 1

        # No usable activity token, or the end push failed. Fall back to asking
        # the app to stand down: without this the card sits on the Lock Screen
        # until iOS expires it, still describing a low that finished an hour ago.
        if device.apns_token:
            push = await self._apns.send_background(
                token=device.apns_token,
                production=device.is_production,
                envelope={
                    "schema": 1,
                    "purpose": "standDown",
                    "id": f"standDown.{session.id}",
                    "sentAt": now,
                    "sessionID": session.id,
                    "episodeKind": session.episode.kind.value,
                },
            )
            await self._store.record_push(device.device_id, "activity.standDown", push.status, push.reason)

    def _build_state(
        self,
        session: Session,
        device: Device,
        readings: list[Reading],
        prediction: list[Reading],
        cob: Optional[float],
        iob: Optional[float],
        now: float,
    ) -> Optional[dict[str, Any]]:
        return activity_builder.build_state(
            session=session,
            readings=readings,
            prediction=prediction,
            configuration=device.configuration,
            unit=device.unit,
            carbs_on_board=cob,
            insulin_on_board=iob,
            now=now,
        )

    @staticmethod
    def _stale_at(session: Session, device: Device, readings: list[Reading], now: float) -> float:
        """When the phone should start dimming the card.

        Anchored to the reading, not to the push: the question is how old the
        *data* is, and a push carrying no new reading must not refresh that
        claim. The window scales with the episode's own cadence, so a low that
        has had nothing for six minutes dims while a five-minute meal card does
        not. A manual or meal card never outlives its own deadline.
        """
        interval = device.configuration.update_interval(session.episode)
        window = max(interval * STALE_WINDOW_MULTIPLE, MINIMUM_STALE_WINDOW)
        latest = readings[-1].at if readings else now
        # Floored just ahead of now so a state is never handed over already
        # stale, which renders as broken rather than as outdated.
        stale_at = max(latest + window, now + 120)
        if activity_builder.shows_countdown(session.episode, device.configuration):
            stale_at = min(stale_at, session.episode.ends_at)
        return stale_at

    # --- keeping the app in step ------------------------------------------

    async def _maybe_refresh(
        self,
        device: Device,
        state: dict[str, Any],
        now: float,
        paired: bool = False,
        session: Optional[Session] = None,
    ) -> bool:
        """Ask the app to sync.

        Two rates, for two different jobs.

        **Paired with an activity push** (`paired=True`), this is the only way
        the app's own data moves with the card. A Live Activity push runs no app
        code — iOS renders it in the widget extension and the app never hears
        about it — so without this the Lock Screen would be current while the
        app behind it showed whatever it had when it was last opened.

        **On its own**, it is rationed hard. iOS budgets background pushes per
        app per day and drops them silently once an app spends too many, and the
        ones worth having when nothing is on screen are the ones that keep the
        app's local staleness alert re-armed — not a wake every five minutes to
        re-read a number the service already knows.

        Either way it is best-effort, and nothing depends on one arriving.
        """
        if not device.apns_token:
            return False

        if paired:
            interval: float = PAIRED_REFRESH_MIN_GAP
        else:
            interval = self._config.refresh_push_interval
            if interval <= 0:
                return False

        last = float(state.get("lastRefreshAt") or 0)
        if now - last < interval:
            return False

        envelope: dict[str, Any] = {
            "schema": 1,
            "purpose": "refresh",
            "id": f"refresh.{int(now)}" if paired else f"refresh.{int(now // max(interval, 1))}",
            "sentAt": now,
        }
        if session is not None:
            envelope["sessionID"] = session.id
            envelope["episodeKind"] = session.episode.kind.value

        push = await self._apns.send_background(
            token=device.apns_token,
            production=device.is_production,
            envelope=envelope,
        )
        await self._store.record_push(
            device.device_id, "refresh.paired" if paired else "refresh", push.status, push.reason
        )
        if push.token_is_dead:
            await self._store.clear_token(device.device_id, "apnsToken")
        return push.ok

    # --- manual start -----------------------------------------------------

    async def request_start(
        self,
        device_id: str,
        duration_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> ManualStartResult:
        """Start a card on demand, outside the rules.

        Everything else in this service starts a card because glucose said so.
        This starts one because a person asked, which is both a feature in its
        own right and the only way to answer "does push-to-start work on this
        phone" without waiting for a low.

        The card it starts is an ordinary one: a session holding a `manual`
        episode, refreshed by every tick on the manual cadence, and ended on its
        own clock when the duration runs out. What it does *not* get is
        priority — the moment the rules say something real is happening, the
        manual episode is suspended and the card changes to it, in place, and
        comes back afterwards if it has time left.
        """
        now = time.time() if now is None else now

        devices = {d.get("deviceID"): d for d in await self._store.list_devices()}
        raw = devices.get(device_id)
        if raw is None:
            return ManualStartResult(404, f"No device is registered as {device_id}.")

        try:
            device = Device.from_registration(raw)
        except (KeyError, TypeError, ValueError) as error:
            return ManualStartResult(404, f"The registration for {device_id} could not be read: {error}")

        if not device.push_to_start_token:
            return ManualStartResult(
                400,
                "This device has not registered a push-to-start token, so nothing can begin a "
                "Live Activity on it. Open Gloo with Live Activities enabled in iOS Settings and "
                "let it register.",
            )
        if not device.live_activities_enabled:
            # The flag is the user's own setting, arriving on every
            # registration. Honouring it here costs one toggle to work around
            # and is the difference between a debugging tool and a way to put a
            # card on someone's Lock Screen against their preference.
            return ManualStartResult(
                400,
                "This device has Live Activities switched off in Gloo. Turn on Settings › "
                "Notifications › Live Activity and try again.",
            )

        duration = self._manual_duration(duration_seconds, device)
        if duration is None:
            return ManualStartResult(400, "durationSeconds must be a positive number of seconds.")

        state = await self._store.get_state(device_id)
        running = Session.from_dict(state.get("session"))
        if running is not None:
            # There is one card, and the one describing something that is
            # actually happening outranks a card someone asked for.
            return ManualStartResult(
                409,
                f"A {running.episode.kind.value} card is already on the Lock Screen. "
                "Use POST /v1/devices/{id}/test to send it an update instead.",
                {"sessionID": running.id, "episodeKind": running.episode.kind.value},
            )

        # A Live Activity with no glucose on it is not worth starting, and
        # `build_state` refuses to invent one.
        try:
            readings = await self._nightscout.entries(now - MANUAL_START_HISTORY, now)
            treatments = await self._nightscout.treatments(
                now - self._config.nightscout.treatment_hours * 3600
            )
        except NightscoutError as error:
            # Upstream, but not APNs — worth its own status so "Apple refused
            # it" and "there was nothing to show" are not the same answer.
            return ManualStartResult(503, f"Could not read Nightscout: {error}")

        if not readings:
            return ManualStartResult(
                503,
                f"No glucose readings in the last {int(MANUAL_START_HISTORY / 60)} minutes, so there "
                "would be nothing on the card.",
            )

        episode = Episode(kind=EpisodeKind.MANUAL, started_at=now, ends_at=now + duration)
        session = Session(id=Session.identifier(now), started_at=now, sequence=1, episode=episode)

        prediction = momentum_forecast(readings, now, device.configuration.prediction_horizon)
        content = self._build_state(
            session,
            device,
            readings,
            prediction,
            carbs_on_board(treatments, now),
            insulin_on_board(treatments, now),
            now,
        )
        if content is None:
            return ManualStartResult(503, "Could not build a content state from the current data.")

        push = await self._apns.send_live_activity(
            token=device.push_to_start_token,
            production=device.is_production,
            event="start",
            content_state=content,
            attributes_type=activity_builder.ATTRIBUTES_TYPE,
            attributes=activity_builder.attributes_for(session),
            # Dim the card when the request runs out, not when the reading goes
            # stale: the tick keeps the reading fresh, and the expiry is the
            # thing the caller actually chose.
            stale_at=episode.ends_at,
            # Required, like every start: a start push without an alert is
            # accepted by APNs with a 200 and then silently discarded.
            alert=self._alert_for(content, EpisodeKind.MANUAL),
            relevance_score=50,
            timestamp=now,
        )
        await self._store.record_push(device_id, "request-start.manual", push.status, push.reason)
        logger.info(
            "request-start: %s %s for %ds -> %s %s",
            device_id,
            session.id,
            int(duration),
            push.status,
            push.reason or "ok",
        )

        if push.token_is_dead:
            await self._store.clear_token(device_id, "pushToStartToken")
            return ManualStartResult(
                502,
                f"APNs rejected the push-to-start token ({push.status} {push.reason}); it has been "
                "dropped. Open Gloo to register a new one.",
                {"apnsStatus": push.status, "apnsReason": push.reason},
            )
        if not push.ok:
            return ManualStartResult(
                502,
                f"APNs refused the push: {push.status} {push.reason or 'no reason given'}.",
                {"apnsStatus": push.status, "apnsReason": push.reason, "apnsID": push.apns_id},
            )

        # Recorded only now that APNs has taken it: a session written before a
        # refused push would leave the tick updating a card that does not exist.
        state["session"] = session.to_dict()
        state["activityStart"] = {"session": session.id, "at": now, "attempts": 1}
        state["lastActivityPushAt"] = now
        await self._store.put_state(device_id, state)

        return ManualStartResult(
            200,
            "Push-to-start accepted by APNs. The card appears once iOS creates it, Gloo registers "
            "its update token, and the service keeps it current until it expires.",
            {
                "sessionID": session.id,
                "episodeKind": EpisodeKind.MANUAL.value,
                "durationSeconds": duration,
                "expiresAt": episode.ends_at,
                "staleAt": episode.ends_at,
                "environment": device.environment,
                "apnsStatus": push.status,
                "apnsID": push.apns_id,
            },
        )

    @staticmethod
    def _manual_duration(requested: Optional[float], device: Device) -> Optional[float]:
        """Clamp, or reject outright.

        Clamping rather than refusing an out-of-range value: asking for twelve
        hours is a reasonable thing to want and getting eight is a reasonable
        answer, whereas asking for "soon" is not a duration at all.
        """
        if requested is None:
            return device.configuration.manual.duration
        try:
            value = float(requested)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return min(max(value, MANUAL_START_MIN_DURATION), MANUAL_START_MAX_DURATION)

    # --- the user swiped it away ------------------------------------------

    async def dismiss(
        self,
        device_id: str,
        session_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Forget the card the user has just swiped away.

        Without this the service would keep an ended activity in its head, find
        no token to update, and — because a session with no activity looks
        exactly like a start push that never arrived — helpfully push a start
        again. Dismissing a card and having it reappear twice is worse than the
        card never having existed.

        The kind's restart cooldown is stamped as if it had ended normally, so
        the rules cannot immediately re-derive the same card from the same
        glucose.
        """
        now = time.time() if now is None else now
        state = await self._store.get_state(device_id)
        session = Session.from_dict(state.get("session"))

        if session is None:
            return {"status": "ok", "detail": "No card was running for this device."}
        if session_id and session_id != session.id:
            # A dismissal that arrives after the card it refers to has already
            # been replaced must not take down its successor.
            return {
                "status": "ignored",
                "detail": f"This device is running {session.id}, not {session_id}.",
            }

        last_ended = _epoch_map(state.get("lastEnded"), EpisodeKind)
        last_ended[session.episode.kind] = now
        state["lastEnded"] = {kind.value: at for kind, at in last_ended.items()}
        state["session"] = None
        state.pop("activityStart", None)
        await self._store.put_state(device_id, state)
        await self._store.record_push(device_id, f"activity.dismissed.{session.episode.kind.value}", 0, "by the user")
        logger.info("dismiss: %s gave up %s (%s)", device_id, session.id, session.episode.kind.value)

        return {
            "status": "ok",
            "detail": "Card forgotten.",
            "sessionID": session.id,
            "episodeKind": session.episode.kind.value,
        }

    # --- diagnostics ------------------------------------------------------

    async def send_test(self, device_id: str, now: Optional[float] = None) -> dict[str, Any]:
        """Push a test alert, and a test Live Activity update if one is running.

        The problem this solves: every other way of answering "does a push from
        this service actually reach that phone" involves waiting for a low.
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
            "activitySessionID": device.activity_session_id,
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

        # An update, not a start: a test that started a real card would put a
        # low warning on someone's Lock Screen for a situation that is not
        # happening.
        state = await self._store.get_state(device_id)
        session = Session.from_dict(state.get("session"))
        if device.activity_token and session and device.activity_session_id == session.id:
            content = self._build_state(session.advanced(), device, [], [], None, None, now)
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
