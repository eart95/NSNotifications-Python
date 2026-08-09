"""The token/episode pairing contract, from the service's side.

The service will only update a Live Activity when the phone has registered an
`activityToken` *and* an `activityEpisodeID` that matches the episode currently
running. That rule is correct — pushing to a token you cannot account for
updates an activity that may have ended — but it is also unforgiving, and a
phone that loses track of the pair produces a frozen Lock Screen and a push that
was *correctly* skipped.

So these pin both halves: that a mismatch is skipped, and that the skip is
recorded with a reason. The second is not decoration. Without it the only
evidence of the whole failure mode is an absence.
"""

from __future__ import annotations

from typing import Any

import pytest

from nsnotifier.models import Reading
from tests.test_service import ANCHOR, FakeNightscout, build, registration, series

pytestmark = pytest.mark.asyncio


async def start_an_activity(tmp_path):
    """Get a device to the state where the server has started an activity."""
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration())
    await service.tick(now=ANCHOR)
    episode_id = apns.activities[0]["attributes"]["episodeID"]
    apns.activities.clear()
    return store, apns, service, episode_id


def updates(apns) -> list[dict[str, Any]]:
    return [push for push in apns.activities if push["event"] == "update"]


async def skips(store) -> list[dict[str, Any]]:
    return [row for row in await store.recent_pushes() if row["kind"] == "activity.update.skipped"]


async def test_a_complete_pair_is_updated(tmp_path):
    store, apns, service, episode_id = await start_an_activity(tmp_path)
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )

    await service.tick(now=ANCHOR + 120)
    assert len(updates(apns)) == 1
    assert await skips(store) == []


async def test_a_missing_token_is_skipped_and_says_so(tmp_path):
    # The phone knows which episode is running but has not managed to register
    # the activity's push token — the exact state a cold launch used to leave it
    # in. There is nothing to address, so nothing is sent.
    store, apns, service, episode_id = await start_an_activity(tmp_path)
    await store.upsert_device(
        "device-1", registration(activityToken=None, activityEpisodeID=episode_id)
    )

    await service.tick(now=ANCHOR + 120)
    assert updates(apns) == []
    reasons = [row["reason"] for row in await skips(store)]
    assert any("no activityToken" in reason for reason in reasons)


async def test_a_stale_episode_id_is_skipped_and_says_which(tmp_path):
    # A token paired with an episode that is not the one running addresses an
    # activity the server is not managing. Pushing to it would put this
    # episode's glucose on a previous episode's Lock Screen card.
    store, apns, service, _ = await start_an_activity(tmp_path)
    await store.upsert_device(
        "device-1",
        registration(activityToken="token-activity", activityEpisodeID="hypoRisk.1"),
    )

    await service.tick(now=ANCHOR + 120)
    assert updates(apns) == []
    reasons = [row["reason"] for row in await skips(store)]
    assert any("episode mismatch" in reason for reason in reasons)
    # The log has to name both sides, or the next step is guessing.
    assert any("hypoRisk.1" in reason for reason in reasons)


async def test_the_pair_recovers_without_restarting_the_episode(tmp_path):
    # The phone catches up — which is what `GlucoseActivityRegistrar` exists to
    # make happen. The episode must carry on, not be torn down and restarted:
    # a restarted episode means a new activity, a fresh cooldown, and a hypo
    # warning that disappears and reappears.
    store, apns, service, episode_id = await start_an_activity(tmp_path)

    await service.tick(now=ANCHOR + 120)
    assert updates(apns) == []

    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )
    await service.tick(now=ANCHOR + 240)

    sent = updates(apns)
    assert len(sent) == 1
    # Same episode as before the gap: nothing was restarted.
    assert sent[0]["collapse_id"] == episode_id
    # And the sequence carried on climbing across the gap, so the phone will
    # accept it.
    assert sent[0]["content_state"]["sequence"] > 1


async def test_a_two_minute_cadence_produces_a_push_per_tick(tmp_path):
    store, apns, service, episode_id = await start_an_activity(tmp_path)
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )

    # Four ticks two minutes apart, with a fresh reading arriving partway
    # through, as a real CGM would.
    for offset in (120, 240, 360, 480):
        readings = series([110, 95, 80, 64], ending_at=ANCHOR + (300 if offset >= 300 else 0))
        service._nightscout = FakeNightscout(readings)
        await service.tick(now=ANCHOR + offset)

    sequences = [push["content_state"]["sequence"] for push in updates(apns)]
    assert len(sequences) == 4
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == 4


# --- an episode with no activity to show it --------------------------------


async def test_restarts_an_episode_whose_activity_never_appeared(tmp_path):
    # APNs returning 200 to a start push means Apple accepted it, not that a
    # Live Activity exists: the phone still has to create one and come back with
    # its token. When it never does, the episode used to spend its whole life on
    # the update path skipping every push for a card that was not there.
    store, apns, service, _ = await start_an_activity(tmp_path)

    # Two minutes later — inside the retry interval — nothing new is sent.
    await service.tick(now=ANCHOR + 120)
    assert [push for push in apns.activities if push["event"] == "start"] == []

    # Well past it, the start is tried again, for the *same* episode: a new
    # episode would mean a new identity and a fresh cooldown.
    await service.tick(now=ANCHOR + 10 * 60)
    restarts = [push for push in apns.activities if push["event"] == "start"]
    assert len(restarts) == 1
    assert restarts[0]["attributes"]["episodeID"].startswith("hypoRisk.")


async def test_stops_retrying_the_start_eventually(tmp_path):
    # A phone with Live Activities switched off at the OS level accepts every
    # start push and does nothing with it, and there is no way to tell that
    # apart from one that is merely slow. So the retries are bounded and the
    # episode carries on as an alert-only episode.
    store, apns, service, _ = await start_an_activity(tmp_path)

    for minute in range(10, 120, 10):
        await service.tick(now=ANCHOR + minute * 60)

    starts = [push for push in apns.activities if push["event"] == "start"]
    assert 0 < len(starts) < 6


async def test_does_not_restart_once_the_phone_has_vouched_for_the_episode(tmp_path):
    store, apns, service, episode_id = await start_an_activity(tmp_path)
    await store.upsert_device(
        "device-1", registration(activityToken="token-activity", activityEpisodeID=episode_id)
    )

    await service.tick(now=ANCHOR + 20 * 60)
    assert [push for push in apns.activities if push["event"] == "start"] == []
    assert len(updates(apns)) == 1


async def test_records_why_a_start_could_not_be_sent(tmp_path):
    # Without a push-to-start token nothing can put a Live Activity on the
    # phone at all, and that is worth an audit-trail row rather than one log
    # line that scrolls away.
    _, store, apns, service = await build(tmp_path, series([110, 95, 80, 64]))
    await store.upsert_device("device-1", registration(pushToStartToken=None))

    await service.tick(now=ANCHOR)
    assert apns.activities == []
    reasons = [
        row["reason"] for row in await store.recent_pushes() if row["kind"] == "activity.start.skipped"
    ]
    assert any("pushToStartToken" in reason for reason in reasons)
