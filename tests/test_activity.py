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
from nsnotifier.models import Episode, EpisodeConfiguration, EpisodeKind, Reading, Unit

ANCHOR = 1_770_000_000.0
CONFIG = EpisodeConfiguration()


def series(values, spacing=300.0, ending_at=ANCHOR):
    last = len(values) - 1
    return [Reading(at=ending_at + (index - last) * spacing, mgdl=value) for index, value in enumerate(values)]


def forecast(values, spacing=300.0):
    return [Reading(at=ANCHOR + (index + 1) * spacing, mgdl=value) for index, value in enumerate(values)]


def state_for(kind, readings, prediction=(), cob=None, iob=None, sequence=1):
    return build_state(
        episode=Episode(kind, ANCHOR, sequence),
        readings=readings,
        prediction=prediction,
        configuration=CONFIG,
        unit=Unit.MGDL,
        carbs_on_board=cob,
        insulin_on_board=iob,
        now=ANCHOR,
    )


def test_every_instant_encodes_as_a_number():
    # ActivityKit decodes a pushed content-state with a stock JSONDecoder,
    # whose default date strategy is seconds since 2001. Anything that is not a
    # plain Unix number here arrives 31 years wrong.
    state = state_for(EpisodeKind.HYPO_RISK, series([110, 95, 80, 64]), forecast([58, 54]))
    for key in ("updatedAt", "readingAt", "eventAt", "sparkStart", "sparkInterval"):
        if key in state:
            assert isinstance(state[key], (int, float)) and not isinstance(state[key], bool)
    assert state["updatedAt"] == ANCHOR


def test_keeps_the_agreed_key_names():
    state = state_for(EpisodeKind.CARB_RISE, series([120, 130, 145, 165]), cob=48, iob=3.2)
    required = {
        "schema", "sequence", "source", "updatedAt", "readingAt", "mgdL",
        "headline", "detail", "spark", "rangeLower", "rangeUpper",
        "lowThreshold", "unit",
    }
    assert required <= set(state)
    assert state["source"] == "server"
    assert state["unit"] == "mgdL"
    assert state["schema"] == 2


def test_stays_well_under_activity_kits_payload_ceiling():
    values = [120.0 + index for index in range(48)]
    state = state_for(EpisodeKind.CARB_RISE, series(values), forecast([200, 215, 230]), cob=96, iob=12.5)
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


def test_names_a_measured_low_differently_from_a_predicted_one():
    measured = state_for(EpisodeKind.HYPO_RISK, series([90, 80, 70, 62]))
    predicted = state_for(EpisodeKind.HYPO_RISK, series([140, 125, 112, 100]), forecast([88, 74, 62]))

    assert measured["headline"] == "Low glucose"
    assert predicted["headline"] == "Heading low"
    assert "62" in predicted["detail"]
    # No clock times anywhere: the widget renders `eventAt` itself, because a
    # server cannot know the phone's locale or time zone.
    assert ":" not in predicted["detail"]
    assert predicted["eventMgdL"] == 62


def test_measured_low_copy_matches_the_swift_wording():
    measured = state_for(EpisodeKind.HYPO_RISK, series([90, 80, 70, 62]), iob=1.4)
    # `GlucoseActivityCopy.hypo` builds exactly this, joined with " · ".
    assert measured["detail"] == "Falling · 1.4 u on board"


def test_carb_rise_copy_matches_the_swift_wording():
    state = state_for(EpisodeKind.CARB_RISE, series([120, 130, 145, 165]), forecast([190, 205]), cob=48, iob=3.2)
    assert state["headline"] == "Carbs on board"
    assert state["detail"] == "48 g absorbing · peak near 205 mg/dL · 3.2 u on board"


def test_omits_optional_keys_rather_than_sending_null():
    # Swift's synthesised encoder uses `encodeIfPresent`, so an absent key and a
    # null decode the same. Fewer bytes, and a captured payload stays readable.
    state = state_for(EpisodeKind.CARB_RISE, series([120, 130]))
    assert "carbsOnBoard" not in state
    assert "insulinOnBoard" not in state
    assert "eventAt" not in state


def test_attributes_carry_only_what_cannot_change():
    attributes = attributes_for(Episode(EpisodeKind.HYPO_RISK, ANCHOR, 4))
    assert attributes == {
        "episodeKind": "hypoRisk",
        "episodeID": "hypoRisk.1770000000",
        "startedAt": ANCHOR,
    }
    assert ATTRIBUTES_TYPE == "GlucoseActivityAttributes"


def test_returns_nothing_without_a_reading():
    assert state_for(EpisodeKind.HYPO_RISK, []) is None


def test_mmol_labels_match_the_swift_formatter():
    state = build_state(
        episode=Episode(EpisodeKind.HYPO_RISK, ANCHOR, 1),
        readings=series([140, 125, 112, 100]),
        prediction=forecast([88, 74, 62]),
        configuration=CONFIG,
        unit=Unit.MMOLL,
        carbs_on_board=None,
        insulin_on_board=None,
        now=ANCHOR,
    )
    # 62 mg/dL is 3.44 mmol/L, which rounds to 3.4 and keeps its decimal.
    assert state["detail"] == "Forecast dips to 3.4 mmol/L"
    # mg/dL stays canonical on the wire whatever the display unit says.
    assert state["mgdL"] == 100
