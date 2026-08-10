"""The registration endpoint.

This is the part of the system that replaced a comma-separated text file on a
web server, so the tests are about the things a text file could not do: reject a
caller without the secret, refuse a registration that could never be pushed to,
and let a device take itself off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nsnotifier.api import create_app
from nsnotifier.config import APNsConfig, Config, NightscoutConfig
from nsnotifier.service import ManualStartResult, NotifierService, TickResult
from nsnotifier.store import Store

SECRET = "a-shared-secret"


class StubService(NotifierService):
    def __init__(self) -> None:  # noqa: D107 - deliberately not calling super
        self._last_tick = TickResult(at=0, readings=12, devices=1)
        self.ticks = 0
        self.start_requests: list[tuple[str, object]] = []
        self.start_result = ManualStartResult(200, "accepted", {"episodeID": "carbRise.1"})

    async def request_start(self, device_id, duration_seconds=None, now=None):
        self.start_requests.append((device_id, duration_seconds))
        return self.start_result

    @property
    def last_tick(self):
        return self._last_tick

    async def tick(self, now: float | None = None) -> TickResult:
        self.ticks += 1
        return self._last_tick


@pytest.fixture()
def service() -> StubService:
    return StubService()


@pytest.fixture()
def client(tmp_path: Path, service: StubService):
    config = Config(
        apns=APNsConfig(key_id="K", team_id="T", bundle_id="com.enricoartuso.GlooMDI", auth_key="x"),
        nightscout=NightscoutConfig(base_url="https://example.invalid"),
        shared_secret=SECRET,
        database_path=tmp_path / "api.sqlite3",
    )
    store = Store(config.database_path)
    return TestClient(create_app(config, store, service))


def registration(**overrides: Any) -> dict[str, Any]:
    payload = {
        "deviceID": "device-1",
        "apnsToken": "token-alert",
        "pushToStartToken": "token-start",
        "environment": "development",
        "unit": "mgdL",
        "alertsEnabled": True,
        "liveActivitiesEnabled": True,
    }
    payload.update(overrides)
    return payload


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRET}"}


def test_health_is_open_and_says_what_the_last_tick_did(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["lastTick"]["readings"] == 12


def test_health_reports_degraded_when_the_last_tick_failed(client, service):
    # A service that is up but has not managed to read Nightscout is not
    # healthy, and answering a flat 200 to that is how an outage goes
    # unnoticed by every monitor pointed at it.
    service._last_tick.error = "502 from Nightscout"
    response = client.get("/v1/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_registration_requires_the_shared_secret(client):
    assert client.put("/v1/devices/device-1", json=registration()).status_code == 401
    assert (
        client.put("/v1/devices/device-1", json=registration(), headers={"Authorization": "Bearer nope"}).status_code
        == 403
    )


def test_registration_round_trips(client):
    assert client.put("/v1/devices/device-1", json=registration(), headers=auth()).status_code == 200

    listed = client.get("/v1/diagnostics", headers=auth()).json()["devices"]
    assert listed[0]["deviceID"] == "device-1"
    assert listed[0]["hasPushToStartToken"] is True


def test_the_path_and_the_body_must_agree(client):
    response = client.put("/v1/devices/other", json=registration(), headers=auth())
    assert response.status_code == 400


def test_refuses_a_registration_with_nothing_to_push_to(client):
    # Accepting this would create a row that can never be pushed to and that
    # looks, in the device list, exactly like a working one.
    response = client.put(
        "/v1/devices/device-1",
        json=registration(apnsToken=None, pushToStartToken=None),
        headers=auth(),
    )
    assert response.status_code == 400


def test_a_device_can_take_itself_off(client):
    client.put("/v1/devices/device-1", json=registration(), headers=auth())
    assert client.delete("/v1/devices/device-1", headers=auth()).status_code == 200
    assert client.get("/v1/diagnostics", headers=auth()).json()["devices"] == []


def test_an_external_scheduler_can_drive_a_tick(client):
    assert client.post("/v1/tick", headers=auth()).status_code == 200
    assert client.post("/v1/tick").status_code == 401


# --- request-start ---------------------------------------------------------


def test_request_start_needs_the_shared_secret(client, service):
    assert client.post("/v1/devices/device-1/request-start", json={}).status_code == 401
    assert (
        client.post(
            "/v1/devices/device-1/request-start",
            json={},
            headers={"Authorization": "Bearer nope"},
        ).status_code
        == 403
    )
    assert service.start_requests == []


def test_request_start_passes_the_duration_through(client, service):
    response = client.post(
        "/v1/devices/device-1/request-start", json={"durationSeconds": 7200}, headers=auth()
    )
    assert response.status_code == 200
    assert response.json()["episodeID"] == "carbRise.1"
    assert service.start_requests == [("device-1", 7200)]


def test_request_start_works_with_no_body_at_all(client, service):
    # An empty body is a request for the default duration, not an error.
    assert client.post("/v1/devices/device-1/request-start", headers=auth()).status_code == 200
    assert service.start_requests == [("device-1", None)]


def test_request_start_propagates_the_status_the_service_chose(client, service):
    service.start_result = ManualStartResult(
        502, "APNs refused the push: 403 ExpiredProviderToken.", {"apnsReason": "ExpiredProviderToken"}
    )
    response = client.post("/v1/devices/device-1/request-start", json={}, headers=auth())

    assert response.status_code == 502
    body = response.json()
    # The useful part of a 502 here is what Apple said, which a bare `detail`
    # string could not carry.
    assert body["apnsReason"] == "ExpiredProviderToken"
    assert "403" in body["detail"]


def test_request_start_reports_an_unknown_device_as_404(client, service):
    service.start_result = ManualStartResult(404, "No device is registered as ghost.")
    response = client.post("/v1/devices/ghost/request-start", json={}, headers=auth())
    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]
