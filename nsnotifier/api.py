"""The HTTP surface: device registration, health, and an external trigger.

Four endpoints, and each one exists to remove a moving part from the old setup:

* ``PUT /v1/devices/{id}`` replaces a comma-separated ``device_tokens.txt``
  edited by hand and fetched over plain HTTP. Tokens change; a file cannot
  learn that.
* ``DELETE /v1/devices/{id}`` gives a user who switches the service off a way to
  actually be switched off.
* ``GET /v1/health`` is what a platform's health check, and the app's own
  "Check Connection" button, both call. "Is it running" should not require
  waiting for a hypo.
* ``POST /v1/tick`` lets something *else* own the schedule — a platform cron, a
  GitHub Action, a Nightscout webhook — without this process having to be the
  thing that wakes up. See ``docs/DEPLOYMENT.md``.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import Config
from .service import NotifierService
from .store import Store

logger = logging.getLogger(__name__)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    return value


def create_app(config: Config, store: Store, service: NotifierService) -> FastAPI:
    app = FastAPI(title="Gloo notification service", docs_url=None, redoc_url=None)

    def authorise(token: str = Depends(_bearer)) -> None:
        # Constant time, because a shared secret checked with `==` leaks its
        # length and then its prefix to anyone patient.
        if not hmac.compare_digest(token, config.shared_secret):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad shared secret")

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        """Unauthenticated on purpose: it reveals nothing and is what a load
        balancer, a platform health check and an uptime monitor all call.

        It reports *liveness*, and separately whether the last tick worked. A
        service that is up but has not managed to read Nightscout for an hour is
        not healthy, and answering a flat 200 to that is how an outage goes
        unnoticed.
        """
        last = service.last_tick
        body: dict[str, Any] = {
            "status": "ok",
            "now": time.time(),
            "lastTick": last.as_dict() if last else None,
        }
        if last is not None and not last.ok:
            body["status"] = "degraded"
            return JSONResponse(body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse(body)

    @app.put("/v1/devices/{device_id}", dependencies=[Depends(authorise)])
    async def register(device_id: str, registration: dict[str, Any]) -> dict[str, Any]:
        body_id = str(registration.get("deviceID") or "")
        if body_id != device_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="deviceID in the body must match the path",
            )
        if not registration.get("apnsToken") and not registration.get("pushToStartToken"):
            # Nothing to address it by. Accepting this would create a row that
            # can never be pushed to and that looks, in the device list, exactly
            # like a working one.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="a registration needs at least one of apnsToken or pushToStartToken",
            )

        registration.setdefault("registeredAt", time.time())
        await store.upsert_device(device_id, registration)
        logger.info(
            "register: %s (%s, activity=%s)",
            device_id,
            registration.get("environment"),
            registration.get("activityEpisodeID") or "-",
        )
        return {"status": "ok"}

    @app.delete("/v1/devices/{device_id}", dependencies=[Depends(authorise)])
    async def deregister(device_id: str) -> dict[str, Any]:
        await store.delete_device(device_id)
        logger.info("deregister: %s", device_id)
        return {"status": "ok"}

    @app.post("/v1/tick", dependencies=[Depends(authorise)])
    async def trigger_tick() -> dict[str, Any]:
        """Run one cycle now.

        Idempotent in the way that matters: the cooldowns and episode state are
        persisted, so calling this ten times in a minute produces at most the
        notifications one call would have.
        """
        result = await service.tick()
        return result.as_dict()

    @app.post("/v1/devices/{device_id}/test", dependencies=[Depends(authorise)])
    async def send_test(device_id: str) -> dict[str, Any]:
        """Send a test alert now, and a test Live Activity update if one is live.

        Exists because a 200 from APNs does not mean anything arrived. The
        payload can still be dropped on the device — priority throttling, a
        content state that will not decode, Low Power Mode — and every one of
        those looks identical from here. This is the only way to answer the
        question in five seconds rather than by waiting for a hypo.
        """
        return await service.send_test(device_id)

    @app.get("/v1/diagnostics", dependencies=[Depends(authorise)])
    async def diagnostics(limit: int = 50) -> dict[str, Any]:
        """What was actually sent, and what APNs said about it.

        The question this answers is "did it send anything last night", which
        the old setup could only answer by hoping the logs had not rotated.
        """
        devices = await store.list_devices()
        return {
            "devices": [
                {
                    "deviceID": device.get("deviceID"),
                    "environment": device.get("environment"),
                    "hasAPNsToken": bool(device.get("apnsToken")),
                    "hasPushToStartToken": bool(device.get("pushToStartToken")),
                    "activityEpisodeID": device.get("activityEpisodeID"),
                    "registeredAt": device.get("registeredAt"),
                    "alertsEnabled": device.get("alertsEnabled"),
                    "liveActivitiesEnabled": device.get("liveActivitiesEnabled"),
                }
                for device in devices
            ],
            "recentPushes": await store.recent_pushes(limit=min(limit, 200)),
            "lastTick": service.last_tick.as_dict() if service.last_tick else None,
        }

    return app
