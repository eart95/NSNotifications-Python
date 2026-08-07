"""The mirror of ``GlucoseEpisodeTests.swift``.

Read the two side by side. Every case here has a Swift twin with the same name
and the same numbers, and that is the only mechanism keeping the phone and the
service agreeing about what a hypo is. A case added to one belongs in the other.
"""

from __future__ import annotations

import pytest

from nsnotifier.episodes import EpisodeInput, evaluate
from nsnotifier.models import EndReason, Episode, EpisodeConfiguration, EpisodeKind, Reading

ANCHOR = 1_770_000_000.0
CONFIG = EpisodeConfiguration()


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def forecast(values, spacing=300.0, starting_at=ANCHOR):
    return [Reading(at=starting_at + (index + 1) * spacing, mgdl=value) for index, value in enumerate(values)]


def running(kind, minutes_ago):
    return Episode(kind, started_at=ANCHOR - minutes_ago * 60)


def decide(current=None, *, readings, prediction=(), carbs=0.0, last_ended=None, now=ANCHOR):
    return evaluate(
        current=current,
        data=EpisodeInput(readings=readings, prediction=prediction, carbs_entering_window=carbs),
        configuration=CONFIG,
        last_ended=last_ended or {},
        now=now,
    )


# --- entry -----------------------------------------------------------------


def test_stays_idle_in_range_with_nothing_happening():
    assert decide(readings=series([120, 118, 116, 115])).action == "idle"


def test_starts_on_a_measured_low():
    decision = decide(readings=series([110, 95, 80, 64]))
    assert decision.action == "start"
    assert decision.episode.kind is EpisodeKind.HYPO_RISK


def test_starts_on_a_forecast_crossing_while_still_in_range():
    decision = decide(readings=series([140, 128, 116, 104]), prediction=forecast([95, 84, 72, 61]))
    assert decision.episode.kind is EpisodeKind.HYPO_RISK


def test_ignores_a_forecast_crossing_beyond_the_horizon():
    decision = decide(
        readings=series([140, 138, 136, 135]),
        prediction=forecast([130, 120, 110, 100, 90, 80, 70, 60], spacing=900.0),
    )
    assert decision.action == "idle"


def test_never_starts_on_stale_readings():
    # A 40-minute-old 55 says where you were, not where you are, and an
    # activity is a claim about now.
    decision = decide(readings=series([90, 75, 62, 55], ending_at=ANCHOR - 40 * 60))
    assert decision.action == "idle"


def test_starts_on_steeply_arriving_carbs():
    decision = decide(readings=series([105, 108, 112, 118]), carbs=60)
    assert decision.episode.kind is EpisodeKind.CARB_RISE


def test_ignores_a_hypo_treatment_sized_carb_entry():
    # 15 g of glucose tablets in half an hour is 30 g/h.
    assert decide(readings=series([88, 92, 98, 105]), carbs=15).action == "idle"


def test_does_not_start_a_carb_rise_while_low():
    decision = decide(readings=series([80, 74, 68, 62]), carbs=60)
    assert decision.episode.kind is EpisodeKind.HYPO_RISK


def test_respects_the_restart_cooldown():
    decision = decide(
        readings=series([110, 95, 80, 64]),
        last_ended={EpisodeKind.HYPO_RISK: ANCHOR - 5 * 60},
    )
    assert decision.action == "idle"


def test_starts_again_once_the_cooldown_has_passed():
    decision = decide(
        readings=series([110, 95, 80, 64]),
        last_ended={EpisodeKind.HYPO_RISK: ANCHOR - 20 * 60},
    )
    assert decision.episode.kind is EpisodeKind.HYPO_RISK


# --- exit ------------------------------------------------------------------


def test_holds_below_the_minimum_duration_even_when_recovered():
    decision = decide(running(EpisodeKind.HYPO_RISK, 5), readings=series([70, 90, 110, 130]))
    assert decision.action == "update"


def test_clears_a_hypo_once_back_in_range():
    decision = decide(running(EpisodeKind.HYPO_RISK, 40), readings=series([62, 74, 88, 104]))
    assert decision.action == "end"
    assert decision.reason is EndReason.RECOVERED


def test_does_not_clear_a_hypo_on_a_value_that_is_still_falling():
    decision = decide(
        running(EpisodeKind.HYPO_RISK, 40),
        readings=series([140, 125, 110, 95]),
        prediction=forecast([84, 74, 64]),
    )
    assert decision.action == "update"


def test_does_not_clear_a_hypo_inside_the_hysteresis_margin():
    # 74 is above the 70 threshold but inside the 10 mg/dL margin.
    decision = decide(running(EpisodeKind.HYPO_RISK, 40), readings=series([62, 66, 70, 74]))
    assert decision.action == "update"


def test_clears_a_carb_rise_once_glucose_is_back_in_range():
    decision = decide(running(EpisodeKind.CARB_RISE, 90), readings=series([210, 190, 172, 158]))
    assert decision.action == "end"
    assert decision.reason is EndReason.RECOVERED


def test_keeps_a_carb_rise_alive_while_more_carbs_are_landing():
    decision = decide(running(EpisodeKind.CARB_RISE, 90), readings=series([170, 165, 162, 160]), carbs=45)
    assert decision.action == "update"


def test_a_hypo_displaces_a_running_carb_rise_immediately():
    decision = decide(running(EpisodeKind.CARB_RISE, 8), readings=series([120, 100, 82, 64]), carbs=60)
    assert decision.action == "replace"
    assert decision.reason is EndReason.SUPERSEDED
    assert decision.starting.kind is EpisodeKind.HYPO_RISK


def test_ends_at_the_ceiling_however_the_rules_feel():
    decision = decide(running(EpisodeKind.CARB_RISE, 5 * 60), readings=series([260, 255, 250, 248]), carbs=80)
    assert decision.reason is EndReason.EXPIRED


def test_ends_when_readings_stop():
    decision = decide(
        running(EpisodeKind.HYPO_RISK, 45),
        readings=series([64, 62, 60, 58], ending_at=ANCHOR - 35 * 60),
    )
    assert decision.reason is EndReason.WENT_STALE


def test_ends_when_there_are_no_readings_at_all():
    assert decide(running(EpisodeKind.HYPO_RISK, 20), readings=[]).reason is EndReason.WENT_STALE


# --- identity --------------------------------------------------------------


def test_episode_identifier_matches_the_swift_format():
    # The phone compares this against the attributes of whatever activity is on
    # screen, so the format is a protocol detail, not a debugging convenience.
    assert Episode(EpisodeKind.HYPO_RISK, ANCHOR).identifier == "hypoRisk.1770000000"
