"""The mirror of ``GlucoseActivityStateTests.swift``.

What is pinned here is what the phone will refuse to decode or will draw
wrongly: the key names, the fact that every instant is a plain number, the spark
grid, the exact copy, and the size ceiling. None of those failures is visible
from looking at a Lock Screen — a state that exceeds 4 KB is dropped by
ActivityKit silently, and one with a renamed key simply never updates again.
"""

from __future__ import annotations

import json

from nsnotifier.activity import ATTRIBUTES_TYPE, attributes_for, build_state, spark
from nsnotifier.models import (
    Direction,
    Episode,
    EpisodeConfiguration,
    EpisodeKind,
    Reading,
    Session,
    Unit,
)

ANCHOR = 1_770_000_000.0
CONFIG = EpisodeConfiguration()


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def forecast(values, spacing=300.0):
    return [Reading(at=ANCHOR + (index + 1) * spacing, mgdl=value) for index, value in enumerate(values)]


def session_for(kind, sequence=1, direction=None):
    return Session(
        id=Session.identifier(ANCHOR),
        started_at=ANCHOR,
        sequence=sequence,
        episode=Episode(
            kind=kind,
            started_at=ANCHOR,
            ends_at=ANCHOR + CONFIG.duration(kind),
            direction=direction,
        ),
    )


def state_for(kind, readings, prediction=(), cob=None, iob=None, sequence=1, direction=None, unit=Unit.MGDL):
    return build_state(
        session=session_for(kind, sequence, direction),
        readings=readings,
        prediction=prediction,
        configuration=CONFIG,
        unit=unit,
        carbs_on_board=cob,
        insulin_on_board=iob,
        now=ANCHOR,
    )


def test_every_instant_encodes_as_a_number():
    # ActivityKit decodes a pushed content-state with a stock JSONDecoder,
    # whose default date strategy is seconds since 2001. Anything that is not a
    # plain Unix number here arrives 31 years wrong.
    state = state_for(EpisodeKind.LOW, series([110, 95, 80, 64]), forecast([58, 54]))
    for key in ("updatedAt", "readingAt", "eventAt", "sparkStart", "sparkInterval", "episodeStartedAt"):
        if key in state:
            assert isinstance(state[key], (int, float)) and not isinstance(state[key], bool)
    assert state["updatedAt"] == ANCHOR


def test_keeps_the_agreed_key_names():
    state = state_for(EpisodeKind.MEAL, series([120, 130, 145, 165]), cob=48, iob=3.2)
    required = {
        "schema", "sequence", "source", "kind", "episodeStartedAt",
        "updatedAt", "readingAt", "mgdL", "headline", "detail", "spark",
        "rangeLower", "rangeUpper", "lowThreshold", "unit",
    }
    assert required <= set(state)
    assert state["source"] == "server"
    assert state["unit"] == "mgdL"
    assert state["schema"] == 3


def test_carries_the_kind_so_the_card_can_change_its_mind():
    # The whole reason schema 3 exists. Attributes are frozen at creation;
    # anything the card has to be able to change has to be state.
    meal = state_for(EpisodeKind.MEAL, series([120, 140, 165, 180]), cob=40)
    assert meal["kind"] == "meal"

    falling = state_for(
        EpisodeKind.VARIATION, series([220, 190, 160, 130]), direction=Direction.FALLING
    )
    assert falling["kind"] == "variation"
    assert falling["direction"] == "falling"


def test_promises_an_end_time_only_for_the_kinds_that_keep_one():
    # A low also has a deadline, but it is a four-hour safety ceiling, and a
    # Lock Screen counting down to it would be telling the user something
    # untrue about when their low will be over.
    manual = state_for(EpisodeKind.MANUAL, series([120, 118, 116, 115]))
    assert manual["endsAt"] == ANCHOR + 2 * 3600
    assert "endsAt" not in state_for(EpisodeKind.LOW, series([90, 80, 70, 62]))


def test_stays_well_under_activity_kits_payload_ceiling():
    values = [120.0 + index for index in range(48)]
    state = state_for(EpisodeKind.MEAL, series(values), forecast([200, 215, 230]), cob=96, iob=12.5)
    size = len(json.dumps({"aps": {"event": "update", "content-state": state, "timestamp": 0}}).encode())
    assert size < 2048, f"payload is {size} bytes; ActivityKit's ceiling is 4096"


def test_resamples_the_spark_onto_a_regular_grid():
    values = [100.0 + index for index in range(48)]
    start, interval, points = spark(series(values))
    assert len(points) == 16
    assert interval == 7200 / 15
    # The newest reading is the number the activity prints, so it has to
    # survive the thinning.
    assert points[-1] == 147
    assert start == ANCHOR - 7200


def test_survives_a_single_reading():
    _, _, points = spark(series([88]))
    assert points == [88]


def test_stops_saying_low_once_glucose_is_back():
    # The card stays for a quarter of an hour after glucose comes back, and a
    # headline reading "Low glucose" over 100 for all of it is how a user
    # learns to stop believing the headline.
    below = state_for(EpisodeKind.LOW, series([90, 80, 70, 62]))
    recovering = state_for(EpisodeKind.LOW, series([140, 125, 112, 100]), forecast([88, 74, 62]))

    assert below["headline"] == "Low glucose"
    assert recovering["headline"] == "Coming back up"
    # The crossing is the first forecast point under the level this card is
    # about — 80, the low entry level — not the curve's eventual minimum.
    assert "74" in recovering["detail"]
    # No clock times anywhere: the widget renders `eventAt` itself, because a
    # server cannot know the phone's locale or time zone.
    assert ":" not in recovering["detail"]
    assert recovering["eventMgdL"] == 74


def test_low_copy_matches_the_swift_wording():
    measured = state_for(EpisodeKind.LOW, series([90, 80, 70, 62]), iob=1.4)
    # `GlucoseActivityCopy.low` builds exactly this, joined with " · ".
    assert measured["detail"] == "Falling · 1.4 u on board"


def test_meal_copy_matches_the_swift_wording():
    state = state_for(EpisodeKind.MEAL, series([120, 130, 145, 165]), forecast([190, 205]), cob=48, iob=3.2)
    assert state["headline"] == "After a meal"
    assert state["detail"] == "48 g absorbing · peak near 205 mg/dL · 3.2 u on board"


def test_variation_copy_says_which_way_and_where_it_is_heading():
    rising = state_for(
        EpisodeKind.VARIATION, series([120, 145, 170, 195]), forecast([210, 225]), direction=Direction.RISING
    )
    assert rising["headline"] == "Rising fast"
    assert rising["detail"] == "Peak near 225 mg/dL"

    falling = state_for(
        EpisodeKind.VARIATION, series([200, 170, 140, 110]), forecast([88, 70]), direction=Direction.FALLING
    )
    assert falling["headline"] == "Falling fast"
    assert falling["detail"] == "Forecast dips to 70 mg/dL"


def test_manual_copy_is_matter_of_fact():
    state = state_for(EpisodeKind.MANUAL, series([120, 122, 124, 126]), iob=0.8)
    assert state["headline"] == "Glucose"
    assert state["detail"] == "Steady · 0.8 u on board"


def test_omits_optional_keys_rather_than_sending_null():
    # Swift's synthesised encoder uses `encodeIfPresent`, so an absent key and a
    # null decode the same. Fewer bytes, and a captured payload stays readable.
    state = state_for(EpisodeKind.MEAL, series([120, 130]))
    assert "carbsOnBoard" not in state
    assert "insulinOnBoard" not in state
    assert "eventAt" not in state
    assert "direction" not in state


def test_attributes_carry_only_what_cannot_change():
    # Two fields, and neither can change: which card this is, and when it
    # appeared. The kind is deliberately *not* here — that is the whole point of
    # the session model.
    attributes = attributes_for(session_for(EpisodeKind.LOW))
    assert attributes == {"sessionID": "s.1770000000", "startedAt": ANCHOR}
    assert ATTRIBUTES_TYPE == "GlucoseActivityAttributes"


def test_returns_nothing_without_a_reading():
    assert state_for(EpisodeKind.LOW, []) is None


def test_mmol_labels_match_the_swift_formatter():
    state = state_for(
        EpisodeKind.LOW, series([140, 125, 112, 100]), forecast([88, 74, 62]), unit=Unit.MMOLL
    )
    # 74 mg/dL is 4.10 mmol/L, which rounds to 4.1 and keeps its decimal.
    assert state["detail"] == "Forecast dips to 4.1 mmol/L"
    # mg/dL stays canonical on the wire whatever the display unit says.
    assert state["mgdL"] == 100
