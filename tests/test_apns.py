"""The APNs client's headers and its provider-token cache.

Every assertion here corresponds to something the old script got wrong and that
fails silently in production: a missing ``apns-push-type``, the sandbox host
hardcoded, a fresh JWT minted per notification (which Apple answers with
``TooManyProviderTokenUpdates`` and then stops accepting), and the Live Activity
topic without its mandatory suffix.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nsnotifier.apns import DEVELOPMENT_HOST, PRODUCTION_HOST, APNsClient, PushResult
from nsnotifier.config import APNsConfig

pytestmark = pytest.mark.asyncio


def make_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class Recorder:
    """Stands in for the client's httpx session."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self.body = body if body is not None else {}

    async def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        self.requests.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(
            self.status,
            json=self.body,
            headers={"apns-id": "recorded"},
            request=httpx.Request("POST", url),
        )


def client_with(recorder: Recorder) -> APNsClient:
    client = APNsClient(
        APNsConfig(key_id="KEY", team_id="TEAM", bundle_id="com.enricoartuso.GlooMDI", auth_key=make_key())
    )
    client._client = recorder  # type: ignore[assignment]
    return client


async def test_alert_headers_name_the_push_type_and_the_right_host():
    recorder = Recorder()
    client = client_with(recorder)

    await client.send_alert(
        token="abc",
        production=True,
        title="Glucose low",
        body="64 mg/dL",
        envelope={"purpose": "alert", "id": "x", "sentAt": 0, "schema": 1},
        collapse_id="measuredLow",
    )

    sent = recorder.requests[0]
    assert sent["url"] == f"{PRODUCTION_HOST}/3/device/abc"
    # Required since iOS 13; the old script omitted it entirely.
    assert sent["headers"]["apns-push-type"] == "alert"
    assert sent["headers"]["apns-priority"] == "10"
    assert sent["headers"]["apns-topic"] == "com.enricoartuso.GlooMDI"
    assert sent["headers"]["apns-collapse-id"] == "measuredLow"
    assert sent["json"]["aps"]["interruption-level"] == "time-sensitive"
    assert sent["json"]["gloo"]["purpose"] == "alert"


async def test_a_development_token_goes_to_the_sandbox_host():
    # The device says which environment its token came from. Hardcoding one is
    # why the old setup worked from Xcode and went silent through TestFlight.
    recorder = Recorder()
    await client_with(recorder).send_background(
        token="abc", production=False, envelope={"purpose": "refresh", "id": "x", "sentAt": 0, "schema": 1}
    )
    assert recorder.requests[0]["url"].startswith(DEVELOPMENT_HOST)


async def test_a_background_push_must_be_low_priority():
    # APNs rejects a content-available push at priority 10 with BadPriority.
    recorder = Recorder()
    await client_with(recorder).send_background(
        token="abc", production=True, envelope={"purpose": "refresh", "id": "x", "sentAt": 0, "schema": 1}
    )
    sent = recorder.requests[0]
    assert sent["headers"]["apns-push-type"] == "background"
    assert sent["headers"]["apns-priority"] == "5"
    assert sent["json"]["aps"]["content-available"] == 1


async def test_live_activity_pushes_carry_the_suffixed_topic():
    recorder = Recorder()
    await client_with(recorder).send_live_activity(
        token="abc",
        production=True,
        event="update",
        content_state={"sequence": 2},
        timestamp=1_770_000_000,
    )
    sent = recorder.requests[0]
    assert sent["headers"]["apns-topic"] == "com.enricoartuso.GlooMDI.push-type.liveactivity"
    assert sent["headers"]["apns-push-type"] == "liveactivity"
    # APNs' own ordering field, distinct from the sequence inside the state.
    assert sent["json"]["aps"]["timestamp"] == 1_770_000_000
    assert sent["json"]["aps"]["event"] == "update"


async def test_a_start_event_refuses_to_go_out_without_attributes():
    # A start push with no attributes is accepted by APNs and then discarded by
    # the device, which is the least debuggable possible outcome.
    with pytest.raises(ValueError):
        await client_with(Recorder()).send_live_activity(
            token="abc", production=True, event="start", content_state={}
        )


async def test_the_provider_token_is_reused_across_pushes():
    recorder = Recorder()
    client = client_with(recorder)
    for _ in range(3):
        await client.send_background(
            token="abc", production=True, envelope={"purpose": "refresh", "id": "x", "sentAt": 0, "schema": 1}
        )

    tokens = {request["headers"]["authorization"] for request in recorder.requests}
    assert len(tokens) == 1, "a fresh JWT per push is what Apple rate-limits"


async def test_a_retired_token_is_reported_as_dead_not_retried():
    recorder = Recorder(status=410, body={"reason": "Unregistered"})
    result = await client_with(recorder).send_alert(
        token="abc",
        production=True,
        title="t",
        body="b",
        envelope={"purpose": "alert", "id": "x", "sentAt": 0, "schema": 1},
    )
    assert isinstance(result, PushResult)
    assert result.token_is_dead
    assert not result.is_retryable
    assert len(recorder.requests) == 1


async def test_a_server_error_is_retried():
    recorder = Recorder(status=503, body={"reason": "ServiceUnavailable"})
    client = client_with(recorder)
    result = await client.send(
        token="abc", payload={"aps": {}}, push_type="alert", production=True, attempts=2
    )
    assert not result.ok
    assert len(recorder.requests) == 2


async def test_live_activity_updates_go_out_at_high_priority():
    # Priority 5 is documented as "the system may delay delivery, and may
    # coalesce or drop updates to save power", and that is exactly what it
    # does: APNs returns 200 and the update never reaches the phone. From the
    # server, every one of those is indistinguishable from a delivery — which
    # is how a Lock Screen sits frozen for an hour with nothing but successes
    # in the log.
    recorder = Recorder()
    await client_with(recorder).send_live_activity(
        token="abc",
        production=True,
        event="update",
        content_state={"schema": 2, "sequence": 3},
        timestamp=1_770_000_000,
    )
    assert recorder.requests[0]["headers"]["apns-priority"] == "10"


async def test_every_live_activity_event_is_high_priority():
    recorder = Recorder()
    client = client_with(recorder)
    await client.send_live_activity(
        token="abc", production=True, event="start", content_state={},
        attributes_type="GlucoseActivityAttributes", attributes={},
        alert={"title": "Heading low", "body": "Falling quickly"}, timestamp=1,
    )
    await client.send_live_activity(
        token="abc", production=True, event="end", content_state={}, timestamp=2
    )
    assert {r["headers"]["apns-priority"] for r in recorder.requests} == {"10"}


async def test_a_start_asks_for_an_update_token():
    # `input-push-token: 1` is what makes iOS mint a push token for the
    # activity being started and deliver it to `pushTokenUpdates`. Without it
    # the card can appear and still have nothing addressing it, so every
    # update afterwards is skipped for want of a pairing the phone was never
    # given — which is indistinguishable from an app that failed to register.
    recorder = Recorder()
    await client_with(recorder).send_live_activity(
        token="abc", production=True, event="start", content_state={},
        attributes_type="GlucoseActivityAttributes", attributes={},
        alert={"title": "Carbs on board", "body": "58 g absorbing"}, timestamp=1,
    )
    assert recorder.requests[0]["json"]["aps"]["input-push-token"] == 1


async def test_a_start_without_an_alert_is_refused_here_rather_than_by_the_device():
    # ActivityKit requires an alert on a start and discards a start push
    # without one — accepted by APNs with a 200 and gone by the time it reaches
    # the device, leaving nothing in any log on either side. Raising is the
    # only way that failure is ever visible.
    with pytest.raises(ValueError):
        await client_with(Recorder()).send_live_activity(
            token="abc", production=True, event="start", content_state={},
            attributes_type="GlucoseActivityAttributes", attributes={}, timestamp=1,
        )


async def test_an_update_needs_no_alert():
    # The requirement is specific to `start`. An update that alerted every two
    # minutes would be unusable.
    recorder = Recorder()
    await client_with(recorder).send_live_activity(
        token="abc", production=True, event="update", content_state={}, timestamp=1
    )
    assert "alert" not in recorder.requests[0]["json"]["aps"]
    assert "input-push-token" not in recorder.requests[0]["json"]["aps"]


async def test_a_live_activity_payload_stays_under_the_ceiling():
    # ActivityKit drops a content state over 4 KB silently — APNs still returns
    # 200. A payload this size would be invisible in every log there is.
    recorder = Recorder()
    await client_with(recorder).send_live_activity(
        token="abc",
        production=True,
        event="update",
        content_state={"spark": [120.0] * 16, "headline": "Heading low", "detail": "Falling quickly"},
        timestamp=1_770_000_000,
    )
    import json as _json

    assert len(_json.dumps(recorder.requests[0]["json"]).encode()) < 4096
