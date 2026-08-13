"""The tick, end to end, against a fake APNs and a fake Nightscout.

These are the tests that catch the bookkeeping mistakes — the ones where every
pure function is right and the service still sends two low alerts, or updates a
card that has already ended, or forgets a cooldown across a restart. That last
one is the reason the state lives in SQLite at all: a container platform
restarts this process on every deploy, and a cooldown that lived in memory would
re-announce a low the user is in the middle of treating.
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


def flat(value, ending_at=ANCHOR):
    return series([value] * 5, ending_at=ending_at)


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
        "activitySessionID": None,
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


def pushes(apns, event: str) -> list[dict[str, Any]]:
    return [push for push in apns.activities if push["event"] == event]


def session_id_of(apns) -> str:
    return pushes(apns, "start")[0]["attributes"]["sessionID"]


async def pair(store, session_id: str, **overrides: Any) -> str:
    """Do what the phone does once a card appears: register the token that
    addresses it, against the session it belongs to."""
    await store.upsert_device(
        "device-1",
        registration(activityToken="token-activity", activitySessionID=session_id, **overrides),
    )
    return session_id


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
    # The whole reason state is in SQLite. A redeploy mid-low used to
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


# --- starting a card -------------------------------------------------------


async def test_a_low_starts_a_card_through_push_to_start(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())

    await service.tick(now=ANCHOR)
    start = pushes(apns, "start")[0]
    assert start["token"] == "token-start"
    assert start["attributes_type"] == "GlucoseActivityAttributes"
    # The attributes say which card this is and nothing about what it is for:
    # the kind is state, so that the card can change its mind later.
    assert start["attributes"]["sessionID"].startswith("s.")
    assert "episodeKind" not in start["attributes"]
    assert start["content_state"]["kind"] == "low"
    # A low announces itself. A card that appears silently on a locked phone at
    # night has not warned anybody.
    assert start["alert"]["sound"] == "default"


async def test_a_meal_starts_quietly(tmp_path):
    treatments = [Treatment(at=ANCHOR - 300, carbs=60)]
    _, store, apns, service = await build(tmp_path, series([105, 108, 112, 118]), treatments)
    await store.upsert_device("device-1", registration())

    await service.tick(now=ANCHOR)
    start = pushes(apns, "start")[0]
    assert start["content_state"]["kind"] == "meal"
    # Quietly means no sound, not no alert: ActivityKit requires an alert on a
    # start and discards a start push without one — accepted by APNs with a 200
    # and gone by the time it reaches the phone.
    assert start["alert"]["title"]
    assert "sound" not in start["alert"]


async def test_updates_only_the_card_the_phone_says_is_running(tmp_path):
    # After a push-to-start the phone has to come back with the activity's own
    # token. Until it does, there is nothing to address.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    session_id = session_id_of(apns)

    apns.activities.clear()
    await service.tick(now=ANCHOR + 300)
    assert apns.activities == []

    await pair(store, session_id)
    apns.activities.clear()
    await service.tick(now=ANCHOR + 600)
    assert pushes(apns, "update")[0]["token"] == "token-activity"


async def test_the_sequence_only_ever_goes_up(tmp_path):
    # APNs promises no ordering, and the phone drops anything not ahead of what
    # it already shows. A sequence that repeated would freeze the Lock Screen
    # with no error anywhere.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns))

    for offset in (300, 600, 900):
        await service.tick(now=ANCHOR + offset)

    sequences = [push["content_state"]["sequence"] for push in apns.activities]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


# --- one card, changing its mind -------------------------------------------


async def test_a_low_takes_over_a_meal_card_in_place(tmp_path):
    # The heart of the rework. The old shape ended the meal activity and sent a
    # push-to-start for a new one, which meant an empty Lock Screen and a
    # registration round trip through the phone at the exact moment glucose was
    # going low.
    treatments = [Treatment(at=ANCHOR - 300, carbs=60)]
    _, store, apns, service = await build(tmp_path, flat(120), treatments)
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    session_id = await pair(store, session_id_of(apns))

    apns.activities.clear()
    service._nightscout = FakeNightscout(series([95, 90, 86, 72], ending_at=ANCHOR + 600), treatments)
    await service.tick(now=ANCHOR + 600)

    assert pushes(apns, "start") == []
    assert pushes(apns, "end") == []
    update = pushes(apns, "update")[0]
    assert update["content_state"]["kind"] == "low"
    assert update["collapse_id"] == session_id
    # A change of kind is worth noticing, so this one push carries an alert —
    # unlike the ordinary refreshes either side of it.
    assert update["alert"]["sound"] == "default"


async def test_the_meal_comes_back_when_the_low_clears(tmp_path):
    treatments = [Treatment(at=ANCHOR - 300, carbs=60)]
    _, store, apns, service = await build(tmp_path, flat(120), treatments)
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns))

    # Low at +10 min, back up at +20, and stayed up for the quarter of an hour
    # the rules ask for.
    service._nightscout = FakeNightscout(series([95, 90, 86, 72], ending_at=ANCHOR + 600), treatments)
    await service.tick(now=ANCHOR + 600)
    service._nightscout = FakeNightscout(flat(110, ending_at=ANCHOR + 1200), treatments)
    await service.tick(now=ANCHOR + 1200)

    apns.activities.clear()
    service._nightscout = FakeNightscout(flat(112, ending_at=ANCHOR + 2200), treatments)
    await service.tick(now=ANCHOR + 2200)

    update = pushes(apns, "update")[0]
    # Back to the meal it interrupted, on the same card, with the meal's own
    # three hours still running.
    assert update["content_state"]["kind"] == "meal"
    assert pushes(apns, "end") == []


async def test_each_kind_is_pushed_at_its_own_cadence(tmp_path):
    # A low every two minutes; a meal every five. The tick runs faster than
    # both so that a switch is noticed quickly, and the pushes are rationed to
    # what each kind asked for.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns))

    apns.activities.clear()
    await service.tick(now=ANCHOR + 60)
    assert pushes(apns, "update") == []
    await service.tick(now=ANCHOR + 120)
    assert len(pushes(apns, "update")) == 1

    # A meal card, same arithmetic, five minutes apart.
    treatments = [Treatment(at=ANCHOR + 3600, carbs=60)]
    _, store, apns, service = await build(
        tmp_path / "meal", flat(120, ending_at=ANCHOR + 3600), treatments
    )
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR + 3600)
    await pair(store, session_id_of(apns))

    apns.activities.clear()
    await service.tick(now=ANCHOR + 3600 + 120)
    assert pushes(apns, "update") == []
    await service.tick(now=ANCHOR + 3600 + 300)
    assert len(pushes(apns, "update")) == 1


async def test_every_card_push_is_paired_with_a_sync(tmp_path):
    # A Live Activity push runs no app code at all — iOS renders it in the
    # widget extension and the app never hears about it. Without this the Lock
    # Screen would be current while the app behind it showed whatever it had
    # when it was last opened.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())

    result = await service.tick(now=ANCHOR)
    assert result.refreshes_sent == 1
    envelope = apns.backgrounds[0]["envelope"]
    assert envelope["purpose"] == "refresh"
    assert envelope["sessionID"].startswith("s.")
    assert envelope["episodeKind"] == "low"


async def test_recovery_ends_the_card_and_says_so(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns))

    # Back over 85 — but the card stays until it has been there a quarter of an
    # hour, because a low that bounces to 90 and back to 70 has not finished.
    service._nightscout = FakeNightscout(flat(104, ending_at=ANCHOR + 1800))
    await service.tick(now=ANCHOR + 1800)
    assert pushes(apns, "end") == []

    service._nightscout = FakeNightscout(flat(106, ending_at=ANCHOR + 2760))
    await service.tick(now=ANCHOR + 2760)

    end = pushes(apns, "end")[0]
    assert end["content_state"]["headline"] == "Back in range"
    # Held on screen briefly rather than yanked: the only good news this system
    # ever gets to deliver.
    assert end["dismiss_at"] > ANCHOR + 2760


async def test_turning_live_activities_off_takes_down_a_running_card(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns), liveActivitiesEnabled=False)

    apns.activities.clear()
    await service.tick(now=ANCHOR + 300)
    assert pushes(apns, "end")


async def test_falls_back_to_stand_down_when_there_is_no_activity_token(tmp_path):
    # The end push cannot be addressed — the token was reissued, or the start
    # never produced one. Without this the card would sit on the Lock Screen
    # until iOS expired it, describing a low that finished an hour ago.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)

    service._nightscout = FakeNightscout(flat(104, ending_at=ANCHOR + 40 * 60))
    await service.tick(now=ANCHOR + 40 * 60)
    await service.tick(now=ANCHOR + 60 * 60)

    stand_downs = [push for push in apns.backgrounds if push["envelope"]["purpose"] == "standDown"]
    assert stand_downs
    assert stand_downs[0]["envelope"]["sessionID"].startswith("s.")


async def test_a_dismissed_card_is_not_pushed_again(tmp_path):
    # The user swiped it away. Without `dismiss`, the service would find no
    # token, decide the start push never arrived, and helpfully send another
    # one — a card that comes back after being dismissed is worse than one that
    # never appeared.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    session_id = apns.activities[0]["attributes"]["sessionID"]

    result = await service.dismiss("device-1", session_id=session_id, now=ANCHOR + 60)
    assert result["status"] == "ok"

    apns.activities.clear()
    await service.tick(now=ANCHOR + 120)
    assert apns.activities == []


async def test_a_late_dismissal_cannot_take_down_a_later_card(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)

    result = await service.dismiss("device-1", session_id="s.1", now=ANCHOR + 60)
    assert result["status"] == "ignored"


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


async def test_stale_readings_end_a_card_rather_than_freezing_it(tmp_path):
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    await pair(store, session_id_of(apns))

    apns.activities.clear()
    # Readings stopped: an hour later, the newest is the same one.
    await service.tick(now=ANCHOR + 60 * 60)
    assert pushes(apns, "end")[0]["content_state"]["headline"] == "No glucose data"


async def test_a_silent_refresh_is_rationed_when_nothing_is_on_screen(tmp_path):
    _, store, apns, service = await build(tmp_path, flat(115), refresh_push_interval=1800.0)
    await store.upsert_device("device-1", registration())

    assert (await service.tick(now=ANCHOR)).refreshes_sent == 1
    assert (await service.tick(now=ANCHOR + 300)).refreshes_sent == 0
    assert (await service.tick(now=ANCHOR + 2000)).refreshes_sent == 1
