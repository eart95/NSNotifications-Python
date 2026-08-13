"""Starting a Live Activity because a person asked, not because glucose did.

Everything else in the service starts one on evidence. This route starts one on
request, which makes it both a feature in its own right and the only way to
answer "does push-to-start reach this phone" without waiting for a low — and the
only path where the caller, rather than the rules, decides what appears on
someone's Lock Screen. So the tests are as much about what it *refuses*.

The manual card is an ordinary session holding a `manual` episode. It is not
kept in a separate slot any more: with the session model, what used to need one
— a card the rules would otherwise end immediately — is now just an episode
whose exit condition is its clock.
"""

from __future__ import annotations

from typing import Any

import pytest

from nsnotifier.apns import PushResult
from nsnotifier.models import EpisodeConfiguration, Reading, Treatment
from nsnotifier.service import MANUAL_START_MAX_DURATION, MANUAL_START_MIN_DURATION
from tests.test_service import ANCHOR, FakeNightscout, build, flat, registration, series

pytestmark = pytest.mark.asyncio


async def ready(tmp_path, readings=None, **overrides):
    """A device that could host an activity, and a service that could start one."""
    _, store, apns, service = await build(tmp_path, readings or flat(118))
    await store.upsert_device("device-1", registration(**overrides))
    return store, apns, service


def starts(apns) -> list[dict[str, Any]]:
    return [push for push in apns.activities if push["event"] == "start"]


def updates(apns) -> list[dict[str, Any]]:
    return [push for push in apns.activities if push["event"] == "update"]


# --- the happy path --------------------------------------------------------


async def test_starts_a_card_on_request(tmp_path):
    store, apns, service = await ready(tmp_path)

    result = await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)

    assert result.status == 200
    push = starts(apns)[0]
    # The push-to-start token, not the activity one: there is no activity yet.
    assert push["token"] == "token-start"
    assert push["attributes_type"] == "GlucoseActivityAttributes"
    assert push["attributes"]["sessionID"] == result.body["sessionID"]
    # The one lever a start push has over how long the card stays useful.
    assert push["stale_at"] == ANCHOR + 7200
    # ActivityKit requires an alert on a start and discards a start push
    # without one — accepted by APNs with a 200, gone by the time it reaches
    # the phone, which is exactly how a requested card used to fail.
    assert push["alert"]["title"]
    # Quietly, though: nothing is wrong, someone just asked to watch.
    assert "sound" not in push["alert"]


async def test_the_response_says_enough_to_act_on(tmp_path):
    store, apns, service = await ready(tmp_path)

    result = await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)

    assert result.body["episodeKind"] == "manual"
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


async def test_a_requested_card_is_always_the_manual_kind(tmp_path):
    # Even during a low. The rules take the card over on the very next tick —
    # in place, with the low's own copy and cadence — and that path is the one
    # that has been thought about; guessing a kind at request time was a second
    # way to decide the same thing.
    store, apns, service = await ready(tmp_path, readings=series([110, 95, 80, 64]))
    result = await service.request_start("device-1", now=ANCHOR)
    assert result.body["episodeKind"] == "manual"

    await store.upsert_device(
        "device-1",
        registration(activityToken="token-activity", activitySessionID=result.body["sessionID"]),
    )
    apns.activities.clear()
    await service.tick(now=ANCHOR + 60)
    assert updates(apns)[0]["content_state"]["kind"] == "low"


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
    # A service apiece: the second request would otherwise be refused with a
    # 409 by the first one's card, which is its own test further down.
    _, _, service = await ready(tmp_path / "a")
    default = await service.request_start("device-1", now=ANCHOR)
    # The device's own setting, which is where the two hours actually lives.
    assert default.body["durationSeconds"] == EpisodeConfiguration().manual.duration

    # Twelve hours is a reasonable thing to want; eight is the honest answer,
    # because iOS ends the activity there whatever anyone asks for.
    _, _, service = await ready(tmp_path / "b")
    long = await service.request_start("device-1", duration_seconds=12 * 3600, now=ANCHOR)
    assert long.body["durationSeconds"] == MANUAL_START_MAX_DURATION

    _, _, service = await ready(tmp_path / "c")
    short = await service.request_start("device-1", duration_seconds=30, now=ANCHOR)
    assert short.body["durationSeconds"] == MANUAL_START_MIN_DURATION


# --- the card's own life ---------------------------------------------------


async def vouched(store, apns, service, at=ANCHOR):
    """Start a manual card and have the phone register its token, as it would."""
    result = await service.request_start("device-1", duration_seconds=7200, now=at)
    await store.upsert_device(
        "device-1",
        registration(activityToken="token-activity", activitySessionID=result.body["sessionID"]),
    )
    apns.activities.clear()
    return result.body["sessionID"]


async def test_the_session_is_recorded_so_the_tick_can_keep_it(tmp_path):
    store, apns, service = await ready(tmp_path)
    result = await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)

    session = (await store.get_state("device-1"))["session"]
    assert session["episode"]["kind"] == "manual"
    assert session["episode"]["endsAt"] == ANCHOR + 7200
    assert result.body["expiresAt"] == ANCHOR + 7200


async def test_the_tick_keeps_a_manual_card_current(tmp_path):
    store, apns, service = await ready(tmp_path)
    session_id = await vouched(store, apns, service)

    for offset in (300, 600, 900):
        service._nightscout = FakeNightscout(flat(118 + offset / 300, ending_at=ANCHOR + offset))
        await service.tick(now=ANCHOR + offset)

    sent = updates(apns)
    # Five-minute cadence, so one push per five minutes and no more.
    assert len(sent) == 3
    assert all(push["collapse_id"] == session_id for push in sent)
    sequences = [push["content_state"]["sequence"] for push in sent]
    assert sequences == sorted(sequences) and len(set(sequences)) == 3
    assert sent[-1]["content_state"]["mgdL"] == 121


async def test_a_manual_card_ends_itself_when_the_time_is_up(tmp_path):
    store, apns, service = await ready(tmp_path)
    await vouched(store, apns, service)

    service._nightscout = FakeNightscout(flat(118, ending_at=ANCHOR + 3600))
    await service.tick(now=ANCHOR + 3600)
    assert [push["event"] for push in apns.activities] == ["update"]

    apns.activities.clear()
    service._nightscout = FakeNightscout(flat(118, ending_at=ANCHOR + 7200))
    await service.tick(now=ANCHOR + 7200 + 1)

    ended = next(push for push in apns.activities if push["event"] == "end")
    # Not the automatic "Still going", which describes an episode cut off at its
    # ceiling while the situation continues. Nothing is still going here.
    assert ended["content_state"]["headline"] == "Finished"
    assert (await store.get_state("device-1")).get("session") is None


async def test_a_low_takes_the_card_over_without_taking_it_away(tmp_path):
    # A card someone asked for must never be the reason a low warning has
    # nowhere to go — and it must not be torn down to make room either.
    store, apns, service = await ready(tmp_path)
    session_id = await vouched(store, apns, service)

    service._nightscout = FakeNightscout(series([110, 95, 80, 64], ending_at=ANCHOR + 300))
    await service.tick(now=ANCHOR + 300)

    assert [push["event"] for push in apns.activities] == ["update"]
    update = updates(apns)[0]
    assert update["content_state"]["kind"] == "low"
    assert update["collapse_id"] == session_id

    session = (await store.get_state("device-1"))["session"]
    # The manual episode is waiting, not gone: it comes back for the rest of
    # its two hours once the low clears.
    assert [item["kind"] for item in session["suspended"]] == ["manual"]


async def test_a_manual_request_is_refused_while_a_card_is_running(tmp_path):
    store, apns, service = await ready(tmp_path, readings=series([110, 95, 80, 64]))
    await service.tick(now=ANCHOR)
    apns.activities.clear()

    result = await service.request_start("device-1", now=ANCHOR + 120)
    assert result.status == 409
    assert result.body["episodeKind"] == "low"
    # And it points at the thing that *is* the right tool at that moment.
    assert "/test" in result.detail
    assert apns.activities == []


async def test_turning_live_activities_off_takes_a_manual_card_down_too(tmp_path):
    store, apns, service = await ready(tmp_path)
    session_id = await vouched(store, apns, service)

    await store.upsert_device(
        "device-1",
        registration(
            activityToken="token-activity",
            activitySessionID=session_id,
            liveActivitiesEnabled=False,
        ),
    )
    await service.tick(now=ANCHOR + 120)

    assert (await store.get_state("device-1")).get("session") is None


async def test_a_manual_card_the_phone_never_registered_is_restarted(tmp_path):
    # Same recovery as an automatic card: a 200 to a start push is not a Live
    # Activity, and the only evidence that one exists is the phone registering
    # a token against this session.
    store, apns, service = await ready(tmp_path)
    await service.request_start("device-1", duration_seconds=7200, now=ANCHOR)
    apns.activities.clear()

    await service.tick(now=ANCHOR + 120)
    assert starts(apns) == []

    await service.tick(now=ANCHOR + 10 * 60)
    assert len(starts(apns)) == 1


async def test_nothing_is_recorded_when_apns_refuses_the_start(tmp_path):
    # A session written before a refused push would leave the tick updating a
    # card that does not exist.
    store, apns, service = await ready(tmp_path)
    apns.activity_result = PushResult(status=403, reason="ExpiredProviderToken")

    await service.request_start("device-1", now=ANCHOR)
    assert (await store.get_state("device-1")).get("session") is None
