"""The tick, end to end, against a fake APNs and a fake Nightscout.

These are the tests that catch the bookkeeping mistakes — the ones where every
pure function is right and the service still sends two hypo alerts, or updates
an activity that has already ended, or forgets a cooldown across a restart. That
last one is the reason the state lives in SQLite at all: a container platform
restarts this process on every deploy, and a cooldown that lived in memory would
re-announce a hypo the user is in the middle of treating.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import pytest

from nsnotifier.apns import PushResult
from nsnotifier.config import APNsConfig, Config, NightscoutConfig
from nsnotifier.models import Reading, Treatment
from nsnotifier.service import NotifierService
from nsnotifier.store import Store

ANCHOR = 1_770_000_000.0

pytestmark = pytest.mark.asyncio


class FakeAPNs:
    """Records what would have been sent, and can be told to refuse.

    The result is settable per push type, because that is how APNs actually
    behaves: one token going dead says nothing about the other two on the same
    device, and a fake that refused everything at once would let a bug that
    pruned all three pass.
    """

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []
        self.activities: list[dict[str, Any]] = []
        self.backgrounds: list[dict[str, Any]] = []
        self.alert_result = PushResult(status=200)
        self.activity_result = PushResult(status=200)
        self.background_result = PushResult(status=200)

    async def send_alert(self, **kwargs: Any) -> PushResult:
        self.alerts.append(kwargs)
        return self.alert_result

    async def send_background(self, **kwargs: Any) -> PushResult:
        self.backgrounds.append(kwargs)
        return self.background_result

    async def send_live_activity(self, **kwargs: Any) -> PushResult:
        self.activities.append(kwargs)
        return self.activity_result


class FakeNightscout:
    def __init__(self, readings: list[Reading], treatments: Optional[list[Treatment]] = None) -> None:
        self.readings = readings
        self.treatments_list = treatments or []

    async def entries(self, since: float, now: Optional[float] = None) -> list[Reading]:
        return list(self.readings)

    async def treatments(self, since: float) -> list[Treatment]:
        return list(self.treatments_list)


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    base = dict(
        apns=APNsConfig(key_id="K", team_id="T", bundle_id="com.enricoartuso.GlooMDI", auth_key="x"),
        nightscout=NightscoutConfig(base_url="https://example.invalid"),
        shared_secret="secret",
        database_path=tmp_path / "test.sqlite3",
        refresh_push_interval=0.0,
    )
    base.update(overrides)
    return Config(**base)


def registration(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "deviceID": "device-1",
        "apnsToken": "token-alert",
        "pushToStartToken": "token-start",
        "activityToken": None,
        "activityEpisodeID": None,
        "bundleID": "com.enricoartuso.GlooMDI",
        "environment": "development",
        "appVersion": "1.0",
        "systemVersion": "27.0",
        "unit": "mgdL",
        "episodeConfiguration": {},
        "alertThresholds": {},
        "enabledAlertKinds": ["predictedLow", "measuredLow", "measuredHigh", "dataStale"],
        "alertsEnabled": True,
        "liveActivitiesEnabled": True,
        "registeredAt": ANCHOR,
    }
    payload.update(overrides)
    return payload


async def build(tmp_path: Path, readings, treatments=None, **config_overrides):
    config = make_config(tmp_path, **config_overrides)
    store = Store(config.database_path)
    apns = FakeAPNs()
    service = NotifierService(config, store, FakeNightscout(readings, treatments), apns)
    return config, store, apns, service


# --- alerts ----------------------------------------------------------------


async def test_a_low_produces_one_alert_and_then_falls_quiet(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())

    first = await service.tick(now=ANCHOR)
    assert first.alerts_sent == 1
    assert apns.alerts[0]["title"] == "Glucose low"

    # Five minutes later, still low. The user does not need telling again.
    second = await service.tick(now=ANCHOR + 300)
    assert second.alerts_sent == 0


async def test_the_cooldown_survives_a_restart(tmp_path):
    # The whole reason state is in SQLite. A redeploy mid-hypo used to
    # re-announce it, which is the moment the user is least able to tell a new
    # alert from an echo.
    config, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)

    reborn_store = Store(config.database_path)
    reborn = NotifierService(config, reborn_store, FakeNightscout(series([110, 95, 80, 64])), FakeAPNs())
    assert (await reborn.tick(now=ANCHOR + 120)).alerts_sent == 0


async def test_the_envelope_tells_the_phone_what_to_damp(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)

    envelope = apns.alerts[0]["envelope"]
    assert envelope["purpose"] == "alert"
    assert envelope["alertKind"] == "measuredLow"
    assert envelope["sentAt"] == ANCHOR
    # Stable per decision, so a redelivery is recognisable as one.
    assert envelope["id"].startswith("measuredLow.")


async def test_a_dead_token_is_pruned_rather_than_retried_forever(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    apns.alert_result = PushResult(status=410, reason="Unregistered")

    result = await service.tick(now=ANCHOR)
    assert result.tokens_pruned >= 1
    stored = (await store.list_devices())[0]
    assert stored["apnsToken"] is None
    # The row survives: the push-to-start token on it may still be good, and
    # the device id is what lets the next registration be recognised as the
    # same phone.
    assert stored["pushToStartToken"] == "token-start"


async def test_one_bad_device_does_not_silence_the_others(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("broken", {"deviceID": "broken", "unit": 17})
    await store.upsert_device("device-1", registration())

    result = await service.tick(now=ANCHOR)
    assert result.alerts_sent == 1


# --- episodes --------------------------------------------------------------


async def test_a_hypo_starts_an_activity_through_push_to_start(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())

    await service.tick(now=ANCHOR)
    start = next(push for push in apns.activities if push["event"] == "start")
    assert start["token"] == "token-start"
    assert start["attributes"]["episodeKind"] == "hypoRisk"
    assert start["attributes_type"] == "GlucoseActivityAttributes"
    # A hypo announces itself. An activity that appears silently on a locked
    # phone at night has not warned anybody.
    assert start["alert"] is not None


async def test_a_meal_starts_quietly(tmp_path):
    treatments = [Treatment(at=ANCHOR - 300, carbs=60)]
    _, store, apns, service = await build(tmp_path, series([105, 108, 112, 118]), treatments)
    await store.upsert_device("device-1", registration())

    await service.tick(now=ANCHOR)
    start = next(push for push in apns.activities if push["event"] == "start")
    assert start["attributes"]["episodeKind"] == "carbRise"
    # No banner: an activity that buzzes for every plate of pasta gets the
    # whole feature switched off.
    assert start["alert"] is None


async def test_updates_only_the_activity_the_phone_says_is_running(tmp_path):
    # After a push-to-start the phone has to come back with the new activity's
    # own token. Until it does, pushing to the previous episode's token would
    # update an activity that is gone.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    started = apns.activities[0]["attributes"]["episodeID"]

    apns.activities.clear()
    await service.tick(now=ANCHOR + 300)
    assert apns.activities == []

    # The phone registers the running activity's token.
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=started)
    )
    await service.tick(now=ANCHOR + 600)
    update = next(push for push in apns.activities if push["event"] == "update")
    assert update["token"] == "token-activity"


async def test_the_sequence_only_ever_goes_up(tmp_path):
    # APNs promises no ordering, and the phone drops anything not ahead of what
    # it already shows. A sequence that repeated would freeze the Lock Screen
    # with no error anywhere.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    episode_id = apns.activities[0]["attributes"]["episodeID"]
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )
    for offset in (300, 600, 900):
        await service.tick(now=ANCHOR + offset)

    sequences = [push["content_state"]["sequence"] for push in apns.activities]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


async def test_recovery_ends_the_activity_and_says_so(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    episode_id = apns.activities[0]["attributes"]["episodeID"]
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )

    service._nightscout = FakeNightscout(series([62, 74, 88, 104], ending_at=ANCHOR + 40 * 60))
    await service.tick(now=ANCHOR + 40 * 60)

    end = next(push for push in apns.activities if push["event"] == "end")
    assert end["content_state"]["headline"] == "Back in range"
    # Held on screen briefly rather than yanked: the only good news this system
    # ever gets to deliver.
    assert end["dismiss_at"] > ANCHOR + 40 * 60


async def test_turning_live_activities_off_takes_down_a_running_one(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    episode_id = apns.activities[0]["attributes"]["episodeID"]

    await store.upsert_device(
        "device-1",
        registration(
            activityToken="token-activity",
            activityEpisodeID=episode_id,
            liveActivitiesEnabled=False,
        ),
    )
    apns.activities.clear()
    await service.tick(now=ANCHOR + 300)
    assert any(push["event"] == "end" for push in apns.activities)


async def test_falls_back_to_stand_down_when_there_is_no_activity_token(tmp_path):
    # The end push cannot be addressed — the token was reissued, or the start
    # never produced one. Without this the activity would sit on the Lock
    # Screen until iOS expired it, describing a hypo that finished an hour ago.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)

    service._nightscout = FakeNightscout(series([62, 74, 88, 104], ending_at=ANCHOR + 40 * 60))
    await service.tick(now=ANCHOR + 40 * 60)

    assert any(push["envelope"]["purpose"] == "standDown" for push in apns.backgrounds)


# --- resilience ------------------------------------------------------------


async def test_a_nightscout_outage_is_reported_not_swallowed(tmp_path):
    from nsnotifier.nightscout import NightscoutError

    class Broken:
        async def entries(self, *_: Any, **__: Any):
            raise NightscoutError("502 from Nightscout")

        async def treatments(self, *_: Any, **__: Any):
            return []

    config, store, apns, service = await build(tmp_path, [])
    service._nightscout = Broken()
    result = await service.tick(now=ANCHOR)

    assert not result.ok
    assert "502" in (result.error or "")
    # No heartbeat is sent on a failed tick, so a dead-man's switch notices.
    assert apns.alerts == []


async def test_stale_readings_end_an_activity_rather_than_freezing_it(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    episode_id = apns.activities[0]["attributes"]["episodeID"]
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )

    apns.activities.clear()
    # Readings stopped: an hour later, the newest is the same one.
    await service.tick(now=ANCHOR + 60 * 60)
    end = next(push for push in apns.activities if push["event"] == "end")
    assert end["content_state"]["headline"] == "No glucose data"


async def test_a_silent_refresh_is_rationed(tmp_path):
    _, store, apns, service = await build(
        tmp_path, series([120, 118, 116, 115]), refresh_push_interval=1800.0
    )
    await store.upsert_device("device-1", registration())

    assert (await service.tick(now=ANCHOR)).refreshes_sent == 1
    assert (await service.tick(now=ANCHOR + 300)).refreshes_sent == 0
    assert (await service.tick(now=ANCHOR + 2000)).refreshes_sent == 1
