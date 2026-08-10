"""Starting a Live Activity because a person asked, not because glucose did.

Everything else in the service starts one on evidence. This route starts one on
request, which makes it the only way to answer "does push-to-start reach this
phone" without waiting for a hypo — and the only path where the caller, rather
than the rules, decides what appears on someone's Lock Screen. So the tests are
mostly about what it *refuses*.
"""

from __future__ import annotations

from typing import Any

import pytest

from nsnotifier.apns import PushResult
from nsnotifier.models import Reading, Treatment
from nsnotifier.service import (
    MANUAL_START_DEFAULT_DURATION,
    MANUAL_START_MAX_DURATION,
    MANUAL_START_MIN_DURATION,
)
from tests.test_service import ANCHOR, FakeNightscout, build, registration, series

pytestmark = pytest.mark.asyncio


async def ready(tmp_path, readings=None, **overrides):
    """A device that could host an activity, and a service that could start one."""
    _, store, apns, service = await build(tmp_path, readings or series([120, 118, 116, 115]))
    await store.upsert_device("device-1", registration(**overrides))
    return store, apns, service


def starts(apns) -> list[dict[str, Any]]:
    return [push for push in apns.activities if push["event"] == "start"]


# --- the happy path --------------------------------------------------------


async def test_starts_an_activity_on_request(tmp_path):
    store, apns, service = await ready(tmp_path)

    result = await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)

    assert result.status == 200
    push = starts(apns)[0]
    # The push-to-start token, not the activity one: there is no activity yet.
    assert push["token"] == "token-start"
    assert push["attributes_type"] == "GlucoseActivityAttributes"
    assert push["attributes"]["episodeID"] == result.body["episodeID"]
    # The one lever a start push has over how long the card stays useful.
    assert push["stale_at"] == ANCHOR + 7200


async def test_the_response_says_enough_to_act_on(tmp_path):
    store, apns, service = await ready(tmp_path)

    result = await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)

    assert result.body["episodeKind"] == "carbRise"
    assert result.body["durationSeconds"] == 7200
    assert result.body["environment"] == "development"
    assert result.body["apnsStatus"] == 200
    # Not "ok": the useful thing to say is that Apple accepted it and the phone
    # has not necessarily done anything yet.
    assert "APNs" in result.detail


async def test_a_manual_start_is_recorded_in_the_audit_trail(tmp_path):
    store, apns, service = await ready(tmp_path)
    await service.request_start("device-1", now=ANCHOR)

    kinds = [row["kind"] for row in await store.recent_pushes()]
    assert any(kind.startswith("request-start.") for kind in kinds)


# --- which card ------------------------------------------------------------


async def test_uses_the_meal_card_when_glucose_is_unremarkable(tmp_path):
    # The neutral one: it does not claim an emergency that is not happening.
    store, apns, service = await ready(tmp_path, readings=series([120, 118, 116, 115]))
    result = await service.request_start("device-1", now=ANCHOR)
    assert result.body["episodeKind"] == "carbRise"


async def test_uses_the_hypo_card_during_an_actual_hypo(tmp_path):
    # If the newest reading is below the device's own low threshold, a hypo
    # card is simply the truth, and it is the one that gets a banner.
    store, apns, service = await ready(tmp_path, readings=series([110, 95, 80, 64]))
    result = await service.request_start("device-1", now=ANCHOR)
    assert result.body["episodeKind"] == "hypoRisk"
    assert starts(apns)[0]["relevance_score"] == 100


async def test_the_card_carries_the_current_numbers(tmp_path):
    store, apns, service = await ready(tmp_path)
    service._nightscout = FakeNightscout(
        series([120, 118, 116, 115]), [Treatment(at=ANCHOR - 600, carbs=40, insulin=3)]
    )

    await service.request_start("device-1", now=ANCHOR)
    state = starts(apns)[0]["content_state"]
    assert state["mgdL"] == 115
    assert state["carbsOnBoard"] > 0
    assert state["insulinOnBoard"] > 0
    assert state["source"] == "server"


# --- refusals --------------------------------------------------------------


async def test_unknown_device_is_a_404(tmp_path):
    _, _, service = await ready(tmp_path)
    result = await service.request_start("nobody", now=ANCHOR)
    assert result.status == 404
    assert "nobody" in result.detail


async def test_no_push_to_start_token_is_a_400(tmp_path):
    store, apns, service = await ready(tmp_path, pushToStartToken=None)
    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 400
    assert "push-to-start" in result.detail
    assert apns.activities == []


async def test_live_activities_switched_off_is_a_400(tmp_path):
    # The flag is the user's own setting, arriving on every registration.
    # Anything holding the bearer token could otherwise put a card on their
    # Lock Screen against a preference they set.
    store, apns, service = await ready(tmp_path, liveActivitiesEnabled=False)
    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 400
    assert apns.activities == []


async def test_a_nonsense_duration_is_a_400(tmp_path):
    store, apns, service = await ready(tmp_path)
    for bad in ("soon", 0, -60, float("nan")):
        result = await service.request_start("device-1", duration_seconds=bad, now=ANCHOR)
        assert result.status == 400, bad
    assert apns.activities == []


async def test_an_apns_refusal_becomes_a_502_that_says_why(tmp_path):
    store, apns, service = await ready(tmp_path)
    apns.activity_result = PushResult(status=403, reason="ExpiredProviderToken")

    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 502
    assert result.body["apnsReason"] == "ExpiredProviderToken"
    assert "403" in result.detail


async def test_a_dead_push_to_start_token_is_dropped_as_well_as_reported(tmp_path):
    store, apns, service = await ready(tmp_path)
    apns.activity_result = PushResult(status=410, reason="Unregistered")

    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 502
    assert (await store.list_devices())[0]["pushToStartToken"] is None


async def test_no_glucose_is_not_an_apns_error(tmp_path):
    # Distinct from 502 on purpose: "Apple refused it" and "there was nothing
    # to put on the card" send you to completely different places.
    store, apns, service = await ready(tmp_path, readings=[])
    service._nightscout = FakeNightscout([])

    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 503
    assert apns.activities == []


async def test_a_nightscout_outage_is_reported_as_such(tmp_path):
    from nsnotifier.nightscout import NightscoutError

    class Broken:
        async def entries(self, *_: Any, **__: Any):
            raise NightscoutError("502 from Nightscout")

        async def treatments(self, *_: Any, **__: Any):
            return []

    store, apns, service = await ready(tmp_path)
    service._nightscout = Broken()

    result = await service.request_start("device-1", now=ANCHOR)
    assert result.status == 503
    assert "Nightscout" in result.detail


# --- duration --------------------------------------------------------------


async def test_duration_defaults_and_clamps(tmp_path):
    store, apns, service = await ready(tmp_path)

    default = await service.request_start("device-1", now=ANCHOR)
    assert default.body["durationSeconds"] == MANUAL_START_DEFAULT_DURATION

    # Twelve hours is a reasonable thing to want; eight is the honest answer,
    # because iOS ends the activity there whatever anyone asks for.
    long = await service.request_start("device-1", duration_seconds=12 * 3600, now=ANCHOR)
    assert long.body["durationSeconds"] == MANUAL_START_MAX_DURATION

    short = await service.request_start("device-1", duration_seconds=30, now=ANCHOR)
    assert short.body["durationSeconds"] == MANUAL_START_MIN_DURATION


# --- and afterwards --------------------------------------------------------


async def test_the_tick_is_not_confused_by_a_manual_activity(tmp_path):
    # Nothing is persisted, so the automatic path carries on as if this had not
    # happened — and when a real episode does start, the phone's registration
    # still points at the manual one, which the existing mismatch recovery
    # handles by sending its own start.
    store, apns, service = await ready(tmp_path)
    result = await service.request_start("device-1", now=ANCHOR)
    manual_episode = result.body["episodeID"]

    assert (await store.get_state("device-1")).get("episode") is None

    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=manual_episode)
    )
    apns.activities.clear()
    service._nightscout = FakeNightscout(series([110, 95, 80, 64], ending_at=ANCHOR + 300))
    await service.tick(now=ANCHOR + 300)

    # A real hypo episode: its own start push, not an update aimed at the
    # manual card.
    assert [push["event"] for push in apns.activities] == ["start"]
    assert starts(apns)[0]["attributes"]["episodeID"] != manual_episode
