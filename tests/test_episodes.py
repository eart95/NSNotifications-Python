"""The mirror of ``GlucoseEpisodeTests.swift``.

Same scenarios, same numbers, against the Python implementation of the same
state machine. The point of duplicating them is that the phone and this service
both decide what belongs on the Lock Screen, and a rule that drifts on one side
produces a card that behaves differently depending on whether the app happened
to be open — which reads as a bug even when both answers are defensible.

If a case is added here and not there, they have started disagreeing.
"""

from __future__ import annotations

from nsnotifier.episodes import EpisodeInput, evaluate, new_episode
from nsnotifier.models import (
    Direction,
    EndReason,
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    Reading,
    Session,
)

ANCHOR = 1_770_000_000.0
CONFIG = EpisodeConfiguration()


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def flat(value, ending_at=ANCHOR):
    return series([value] * 5, ending_at=ending_at)


def moving(per_minute, start=220.0, ending_at=ANCHOR):
    """Twenty minutes of steady movement at ``per_minute`` mg/dL.

    ``start`` is the value twenty minutes ago, so a fall starts high enough
    that the series is testing movement rather than quietly testing the low
    rule.
    """
    return [
        Reading(at=ending_at - minutes_ago * 60, mgdl=start + per_minute * (20 - minutes_ago))
        for minutes_ago in (20, 15, 10, 5, 0)
    ]


def episode(kind, started_at=ANCHOR, direction=None, clearing_since=None, configuration=CONFIG):
    return Episode(
        kind=kind,
        started_at=started_at,
        ends_at=started_at + configuration.duration(kind),
        direction=direction,
        clearing_since=clearing_since,
    )


def session(episode_, started_at=ANCHOR, sequence=1, suspended=()):
    return Session(
        id=Session.identifier(started_at),
        started_at=started_at,
        sequence=sequence,
        episode=episode_,
        suspended=tuple(suspended),
    )


def decide(session_, readings, carbs=0.0, prediction=(), last_ended=None, now=ANCHOR, configuration=CONFIG):
    return evaluate(
        session=session_,
        data=EpisodeInput(readings=readings, prediction=prediction, carbs_entering_window=carbs),
        configuration=configuration,
        last_ended=last_ended or {},
        now=now,
    )


# --- entry -----------------------------------------------------------------


def test_stays_idle_in_range_with_nothing_happening():
    assert decide(None, flat(115)).action == "idle"


def test_starts_a_low_below_the_entry_level():
    # 70 suspend threshold + 10 = 80 in.
    decision = decide(None, series([95, 90, 86, 79]))
    assert decision.action == "start"
    assert decision.episode.kind is EpisodeKind.LOW


def test_leaves_an_unremarkable_number_just_above_the_entry_level_alone():
    assert decide(None, series([95, 90, 86, 82])).action == "idle"


def test_does_not_start_a_low_on_a_forecast_alone():
    # Deliberate: the low episode is measured. The predicted-low *alert* is what
    # warns early — a Lock Screen card for a forecast that a single reading can
    # revoke is how the card stops being believed.
    prediction = [Reading(at=ANCHOR + 600, mgdl=74), Reading(at=ANCHOR + 900, mgdl=62)]
    assert decide(None, series([140, 125, 112, 100]), prediction=prediction).action == "idle"


def test_starts_a_variation_on_a_sustained_steep_fall():
    decision = decide(None, moving(-3))
    assert decision.action == "start"
    assert decision.episode.kind is EpisodeKind.VARIATION
    assert decision.episode.direction is Direction.FALLING


def test_ignores_a_movement_that_is_merely_brisk():
    # 2 mg/dL/min: real, and not what "changing rapidly" was asked to mean.
    assert decide(None, moving(2)).action == "idle"


def test_ignores_a_steep_move_that_reversed_part_way_through():
    # Same endpoints as a qualifying rise, but it went the other way in the
    # middle: two movements, not one sustained one.
    assert decide(None, series([120, 180, 140, 200])).action == "idle"


def test_starts_a_meal_on_enough_carbohydrate():
    decision = decide(None, flat(120), carbs=45)
    assert decision.action == "start"
    assert decision.episode.kind is EpisodeKind.MEAL


def test_ignores_a_small_snack():
    assert decide(None, flat(120), carbs=18).action == "idle"


def test_treats_carbohydrate_during_a_low_as_a_treatment_not_a_meal():
    decision = decide(None, series([80, 74, 68, 62]), carbs=40)
    assert decision.episode.kind is EpisodeKind.LOW


def test_never_starts_a_manual_episode_on_its_own():
    for readings in (flat(120), series([95, 90, 86, 79]), moving(-3)):
        decision = decide(None, readings, carbs=60)
        assert decision.live is None or decision.live.kind is not EpisodeKind.MANUAL


def test_refuses_to_start_on_a_reading_too_old_to_describe_now():
    stale = series([95, 90, 86, 62], ending_at=ANCHOR - 40 * 60)
    assert decide(None, stale).action == "idle"


def test_honours_the_restart_cooldown():
    cooling = decide(None, series([95, 90, 86, 74]), last_ended={EpisodeKind.LOW: ANCHOR - 300})
    assert cooling.action == "idle"

    past = decide(None, series([95, 90, 86, 74]), last_ended={EpisodeKind.LOW: ANCHOR - 1200})
    assert past.episode.kind is EpisodeKind.LOW


def test_respects_a_kind_the_user_turned_off():
    configuration = EpisodeConfiguration(enabled_kinds=frozenset({EpisodeKind.LOW}))
    assert decide(None, flat(120), carbs=60, configuration=configuration).action == "idle"


# --- switching -------------------------------------------------------------


def test_a_low_takes_over_a_manual_card_without_ending_the_session():
    running = session(episode(EpisodeKind.MANUAL, started_at=ANCHOR - 20 * 60))
    decision = decide(running, series([95, 90, 86, 72]))

    assert decision.action == "update"
    assert decision.session.episode.kind is EpisodeKind.LOW
    # The card itself is the same card: same session id, same activity, no
    # push-to-start, no gap on the Lock Screen.
    assert decision.session.id == running.id
    assert [item.kind for item in decision.session.suspended] == [EpisodeKind.MANUAL]


def test_the_manual_card_comes_back_when_the_low_clears():
    manual = episode(EpisodeKind.MANUAL, started_at=ANCHOR - 40 * 60)
    low = episode(EpisodeKind.LOW, started_at=ANCHOR - 30 * 60, clearing_since=ANCHOR - 16 * 60)
    decision = decide(session(low, suspended=[manual]), flat(110))

    assert decision.session.episode.kind is EpisodeKind.MANUAL
    # Resumed, not restarted: it keeps its original deadline, so an
    # interruption cannot extend a two-hour card into three.
    assert decision.session.episode.ends_at == manual.ends_at
    assert decision.session.suspended == ()


def test_does_not_resume_something_whose_time_ran_out_while_it_waited():
    manual = Episode(kind=EpisodeKind.MANUAL, started_at=ANCHOR - 3 * 3600, ends_at=ANCHOR - 3600)
    low = episode(EpisodeKind.LOW, started_at=ANCHOR - 30 * 60, clearing_since=ANCHOR - 16 * 60)
    decision = decide(session(low, suspended=[manual]), flat(110))
    assert decision.action == "end"
    assert decision.reason is EndReason.RECOVERED


def test_a_fall_steep_enough_displaces_a_meal():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 40 * 60)
    decision = decide(session(meal), moving(-3, start=200))
    assert decision.session.episode.kind is EpisodeKind.VARIATION
    assert decision.session.episode.direction is Direction.FALLING


def test_a_rise_does_not_displace_the_meal_that_caused_it():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 40 * 60)
    decision = decide(session(meal), moving(3, start=120))
    assert decision.session.episode.kind is EpisodeKind.MEAL


def test_a_variation_that_turns_round_becomes_the_other_direction():
    rising = episode(EpisodeKind.VARIATION, started_at=ANCHOR - 30 * 60, direction=Direction.RISING)
    decision = decide(session(rising), moving(-3, start=250))
    assert decision.session.episode.direction is Direction.FALLING
    # A turn is a new movement, so its own ceiling restarts with it.
    assert decision.session.episode.started_at == ANCHOR
    assert decision.session.suspended == ()


def test_a_second_helping_extends_the_meal_rather_than_queueing_behind_it():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 90 * 60)
    decision = decide(session(meal), flat(160), carbs=40)
    assert decision.session.episode.ends_at == ANCHOR + CONFIG.meal.duration
    # Extended, not restarted: still the meal that started 90 minutes ago.
    assert decision.session.episode.started_at == meal.started_at


def test_never_stacks_more_than_two_waiting_episodes():
    manual = episode(EpisodeKind.MANUAL, started_at=ANCHOR - 30 * 60)
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 20 * 60)
    variation = episode(
        EpisodeKind.VARIATION, started_at=ANCHOR - 10 * 60, direction=Direction.RISING
    )
    decision = decide(session(variation, suspended=[manual, meal]), series([95, 90, 86, 72]))

    assert decision.session.episode.kind is EpisodeKind.LOW
    assert len(decision.session.suspended) == Session.MAXIMUM_SUSPENDED


# --- exit ------------------------------------------------------------------


def test_keeps_a_low_until_glucose_has_stayed_up_for_a_quarter_of_an_hour():
    low = episode(EpisodeKind.LOW, started_at=ANCHOR - 30 * 60)

    just_back = decide(session(low), flat(95))
    assert just_back.action == "update"
    assert just_back.session.episode.clearing_since == ANCHOR

    dwelt = decide(session(low.clearing(ANCHOR - 16 * 60)), flat(95))
    assert dwelt.action == "end"
    assert dwelt.reason is EndReason.RECOVERED


def test_restarts_the_dwell_when_glucose_dips_again():
    low = episode(EpisodeKind.LOW, started_at=ANCHOR - 30 * 60, clearing_since=ANCHOR - 12 * 60)
    decision = decide(session(low), flat(83))
    # 83 is under the 85 exit level: the quarter of an hour starts again.
    assert decision.session.episode.clearing_since is None


def test_ends_a_variation_once_it_has_been_still_for_long_enough():
    variation = episode(
        EpisodeKind.VARIATION,
        started_at=ANCHOR - 40 * 60,
        direction=Direction.RISING,
        clearing_since=ANCHOR - 16 * 60,
    )
    decision = decide(session(variation), flat(190))
    assert decision.reason is EndReason.RECOVERED


def test_ends_a_meal_after_three_hours():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 3 * 3600 - 60)
    decision = decide(session(meal), flat(150))
    # Completed, not expired: three hours is what a meal card is for.
    assert decision.reason is EndReason.COMPLETED


def test_ends_a_meal_early_once_glucose_is_steady_and_in_range():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 2 * 3600, clearing_since=ANCHOR - 31 * 60)
    decision = decide(session(meal), flat(130))
    assert decision.reason is EndReason.RECOVERED


def test_does_not_end_a_meal_that_is_steady_but_out_of_range():
    meal = episode(EpisodeKind.MEAL, started_at=ANCHOR - 2 * 3600, clearing_since=ANCHOR - 31 * 60)
    decision = decide(session(meal), flat(230))
    assert decision.action == "update"
    assert decision.session.episode.clearing_since is None


def test_ends_a_manual_card_on_its_own_clock_and_nothing_else():
    midway = decide(session(episode(EpisodeKind.MANUAL, started_at=ANCHOR - 90 * 60)), flat(115))
    # Perfectly steady, perfectly in range, and still there: the user asked for
    # two hours.
    assert midway.action == "update"

    expired = decide(
        session(episode(EpisodeKind.MANUAL, started_at=ANCHOR - 2 * 3600 - 60)), flat(115)
    )
    assert expired.reason is EndReason.COMPLETED


def test_ends_anything_when_readings_stop():
    low = episode(EpisodeKind.LOW, started_at=ANCHOR - 30 * 60)
    decision = decide(session(low), series([70, 68, 66], ending_at=ANCHOR - 40 * 60))
    assert decision.reason is EndReason.WENT_STALE


def test_gives_up_on_a_low_that_never_clears():
    decision = decide(session(episode(EpisodeKind.LOW, started_at=ANCHOR - 5 * 3600)), flat(65))
    # Expired, not completed: a low still going at its ceiling is a rule that
    # failed to clear, and the copy says so.
    assert decision.reason is EndReason.EXPIRED


# --- cadence ---------------------------------------------------------------


def test_each_kind_sets_its_own_cadence():
    assert CONFIG.update_interval(new_episode(EpisodeKind.LOW, CONFIG, ANCHOR)) == 120
    assert CONFIG.update_interval(new_episode(EpisodeKind.MEAL, CONFIG, ANCHOR)) == 300
    assert CONFIG.update_interval(new_episode(EpisodeKind.MANUAL, CONFIG, ANCHOR)) == 300
    falling = new_episode(EpisodeKind.VARIATION, CONFIG, ANCHOR, Direction.FALLING)
    rising = new_episode(EpisodeKind.VARIATION, CONFIG, ANCHOR, Direction.RISING)
    assert CONFIG.update_interval(falling) == 120
    assert CONFIG.update_interval(rising) == 300


def test_derives_the_low_levels_from_the_suspend_threshold():
    assert CONFIG.low_entry_level == 80
    assert CONFIG.low_exit_level == 85

    raised = EpisodeConfiguration(suspend_threshold=75)
    assert raised.low_entry_level == 85
    assert raised.low_exit_level == 90
