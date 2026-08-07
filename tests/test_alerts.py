"""What the service is allowed to interrupt someone for.

The two failures worth testing are opposite and equally invisible: an alert that
fires too often gets muted, and one that never fires looks exactly like a quiet
night. Neither shows up by running the thing for an afternoon.
"""

from __future__ import annotations

from nsnotifier.alerts import derive
from nsnotifier.models import (
    AlertKind,
    AlertThresholds,
    Device,
    EpisodeConfiguration,
    Reading,
    Unit,
)

ANCHOR = 1_770_000_000.0


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def forecast(values, spacing=300.0):
    return [Reading(at=ANCHOR + (index + 1) * spacing, mgdl=value) for index, value in enumerate(values)]


def device(**overrides):
    base = dict(
        device_id="device-1",
        apns_token="abc",
        push_to_start_token="def",
        activity_token=None,
        activity_episode_id=None,
        bundle_id="com.enricoartuso.GlooMDI",
        environment="development",
        unit=Unit.MGDL,
        configuration=EpisodeConfiguration(),
        alert_thresholds=AlertThresholds(),
        enabled_alert_kinds=frozenset(AlertKind),
        alerts_enabled=True,
        live_activities_enabled=True,
        registered_at=ANCHOR,
    )
    base.update(overrides)
    return Device(**base)


def kinds(alerts):
    return {alert.kind for alert in alerts}


def test_fires_below_the_low_threshold():
    alerts = derive(device(), series([110, 95, 80, 64]), [], {}, ANCHOR)
    assert AlertKind.MEASURED_LOW in kinds(alerts)
    assert alerts[0].body == "64 mg/dL, falling quickly."


def test_stays_quiet_in_range():
    assert derive(device(), series([120, 118, 116, 115]), [], {}, ANCHOR) == []


def test_high_uses_the_alert_threshold_not_the_chart_ceiling():
    # 200 is out of range on the chart and nowhere near worth a banner. An
    # alert you learn to ignore is worse than no alert.
    assert derive(device(), series([180, 190, 196, 200]), [], {}, ANCHOR) == []
    assert AlertKind.MEASURED_HIGH in kinds(derive(device(), series([230, 245, 255, 262]), [], {}, ANCHOR))


def test_damps_a_repeat_inside_the_re_alert_interval():
    last = {AlertKind.MEASURED_LOW: ANCHOR - 10 * 60}
    assert derive(device(), series([110, 95, 80, 64]), [], last, ANCHOR) == []


def test_speaks_again_once_the_interval_has_passed():
    last = {AlertKind.MEASURED_LOW: ANCHOR - 40 * 60}
    assert AlertKind.MEASURED_LOW in kinds(derive(device(), series([110, 95, 80, 64]), [], last, ANCHOR))


def test_never_alerts_on_a_stale_reading():
    # Telling someone they were 64 forty minutes ago is worse than silence: it
    # invites a treatment decision from a number that no longer exists.
    alerts = derive(device(), series([110, 95, 80, 64], ending_at=ANCHOR - 40 * 60), [], {}, ANCHOR)
    assert kinds(alerts) == {AlertKind.DATA_STALE}


def test_warns_on_a_predicted_low_only_inside_the_lead_time():
    # A crossing 50 minutes out is real but not yet actionable; the service is
    # back in five minutes and it will have firmed up or gone away.
    far = derive(device(), series([160, 150, 140, 130]), forecast([120, 110, 100, 90, 80, 70, 60], spacing=480), {}, ANCHOR)
    assert AlertKind.PREDICTED_LOW not in kinds(far)

    near = derive(device(), series([140, 128, 116, 104]), forecast([95, 84, 72, 61]), {}, ANCHOR)
    assert AlertKind.PREDICTED_LOW in kinds(near)


def test_suppresses_a_predicted_low_while_already_low():
    # The measured alert has that covered, and two notifications about one hypo
    # is one too many.
    alerts = derive(device(), series([90, 80, 70, 64]), forecast([58, 52]), {}, ANCHOR)
    assert kinds(alerts) == {AlertKind.MEASURED_LOW}


def test_honours_the_kinds_the_user_switched_off():
    off = device(enabled_alert_kinds=frozenset({AlertKind.DATA_STALE}))
    assert derive(off, series([110, 95, 80, 64]), [], {}, ANCHOR) == []


def test_says_nothing_at_all_when_alerts_are_off():
    assert derive(device(alerts_enabled=False), series([110, 95, 80, 40]), [], {}, ANCHOR) == []


def test_alert_ids_are_stable_across_ticks_on_the_same_reading():
    # Two ticks that see the same reading must produce the same id, or the
    # phone's duplicate ledger cannot recognise a redelivery.
    readings = series([110, 95, 80, 64])
    first = derive(device(), readings, [], {}, ANCHOR)
    second = derive(device(), readings, [], {}, ANCHOR + 30)
    assert first[0].identifier == second[0].identifier


def test_writes_copy_in_the_users_own_unit():
    alerts = derive(device(unit=Unit.MMOLL), series([110, 95, 80, 64]), [], {}, ANCHOR)
    assert alerts[0].body == "3.6 mmol/L, falling quickly."
