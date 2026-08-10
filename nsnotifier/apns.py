"""The APNs provider client.

Almost every line here is a correction of something the old script did per
notification. It is worth listing them, because they are the difference between
"it works on my phone" and "it works at 4 a.m. six months later":

* **The provider token is cached.** The old code built a fresh JWT for every
  push and, before that, downloaded the .p8 over HTTP to sign it. Apple
  explicitly rejects a provider that refreshes its token more than once every
  20 minutes with ``TooManyProviderTokenUpdates``, and requires one no older
  than an hour. One token per 40 minutes satisfies both with room on each side.
* **The connection is reused.** ``httpx.AsyncClient`` was constructed inside the
  send function, so every notification paid a fresh TLS and HTTP/2 handshake to
  Apple. Multiplexing over one connection is most of what makes APNs fast.
* **The host is not hardcoded.** ``api.sandbox.push.apple.com`` was, which is
  why the old setup worked from Xcode and went silent through TestFlight. Each
  device says which environment its token came from.
* **410 and 400 are acted on.** A retired token answers ``410 Unregistered``
  forever; a registry that never prunes accumulates dead rows and, worse, hides
  the fact that a live device has stopped being reachable.
* **Failures are per device.** One bad token used to be able to take out the
  loop over all of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from .config import APNsConfig

logger = logging.getLogger(__name__)

PRODUCTION_HOST = "https://api.push.apple.com"
DEVELOPMENT_HOST = "https://api.sandbox.push.apple.com"

# Apple: a provider token must be less than an hour old, and must not be
# refreshed more than once every 20 minutes. Halfway between is the only
# sensible place to sit.
TOKEN_LIFETIME_SECONDS = 40 * 60


class PushType:
    ALERT = "alert"
    BACKGROUND = "background"
    LIVE_ACTIVITY = "liveactivity"


@dataclass(frozen=True)
class PushResult:
    status: int
    reason: str = ""
    apns_id: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def token_is_dead(self) -> bool:
        """Whether the registry should drop this token.

        ``410`` is Apple saying the token is retired. ``400 BadDeviceToken``
        usually means the token belongs to the *other* environment — which,
        since the device tells us its environment, means the app was
        reinstalled from a different channel and will re-register shortly. Both
        are reasons to stop pushing to this token now.
        """
        return self.status == 410 or (self.status == 400 and self.reason == "BadDeviceToken")

    @property
    def is_retryable(self) -> bool:
        # 429 is Apple asking for less; 5xx is Apple having a moment.
        return self.status == 429 or self.status >= 500


class APNsClient:
    """One long-lived client for both APNs environments.

    Both hosts are served by the same ``httpx.AsyncClient`` — it pools per
    origin, so a deployment with only production devices never opens a
    connection to sandbox.
    """

    def __init__(self, config: APNsConfig, host_override: str = "", timeout: float = 10.0):
        self._config = config
        self._host_override = host_override
        self._client = httpx.AsyncClient(http2=True, timeout=timeout)
        self._token: Optional[str] = None
        self._token_issued_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # Parsed once. Loading a PEM is not expensive, but doing it per push
        # was one more thing between a hypo and a phone.
        self._private_key = serialization.load_pem_private_key(
            config.auth_key.encode(), password=None, backend=default_backend()
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "APNsClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # --- provider token ---------------------------------------------------

    async def _provider_token(self) -> str:
        async with self._token_lock:
            now = time.time()
            if self._token and (now - self._token_issued_at) < TOKEN_LIFETIME_SECONDS:
                return self._token

            self._token = jwt.encode(
                {"iss": self._config.team_id, "iat": int(now)},
                self._private_key,
                algorithm="ES256",
                headers={"alg": "ES256", "kid": self._config.key_id},
            )
            self._token_issued_at = now
            logger.info("apns: minted a new provider token")
            return self._token

    def _host(self, production: bool) -> str:
        if self._host_override:
            return self._host_override
        return PRODUCTION_HOST if production else DEVELOPMENT_HOST

    # --- sending ----------------------------------------------------------

    async def send(
        self,
        *,
        token: str,
        payload: dict[str, Any],
        push_type: str,
        production: bool,
        topic: Optional[str] = None,
        priority: int = 10,
        expiration: Optional[int] = None,
        collapse_id: Optional[str] = None,
        attempts: int = 3,
    ) -> PushResult:
        """Deliver one notification. Never raises for a delivery failure.

        A push that cannot be delivered is data — which device, which reason —
        not an exception to unwind a tick over. The caller decides whether a
        given failure means "prune this token" or "try again next time".
        """
        url = f"{self._host(production)}/3/device/{token}"
        apns_id = str(uuid.uuid4())

        headers = {
            "apns-topic": topic or self._config.bundle_id,
            # Required since iOS 13. Without it APNs rejects the push outright,
            # and the old script omitted it — it worked only because alert
            # pushes to older topics were still tolerated.
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-id": apns_id,
            "authorization": f"bearer {await self._provider_token()}",
            "content-type": "application/json",
        }
        if expiration is not None:
            headers["apns-expiration"] = str(expiration)
        if collapse_id:
            # APNs keeps only the newest undelivered notification per collapse
            # id. For glucose that is exactly right: a phone that comes back
            # online after twenty minutes should get the current number, not a
            # queue of four historic ones.
            headers["apns-collapse-id"] = collapse_id[:64]

        delay = 1.0
        for attempt in range(attempts):
            try:
                response = await self._client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as error:
                if attempt + 1 == attempts:
                    logger.warning("apns: transport failure after %d attempts: %s", attempts, error)
                    return PushResult(status=0, reason=str(error), apns_id=apns_id)
                await asyncio.sleep(delay)
                delay *= 2
                continue

            reason = ""
            if response.status_code >= 400:
                try:
                    reason = response.json().get("reason", "")
                except ValueError:
                    reason = response.text[:200]

            result = PushResult(
                status=response.status_code,
                reason=reason,
                apns_id=response.headers.get("apns-id", apns_id),
            )

            if result.ok or not result.is_retryable or attempt + 1 == attempts:
                if not result.ok:
                    logger.warning(
                        "apns: %s push rejected: %s %s", push_type, result.status, result.reason
                    )
                return result

            # ExpiredProviderToken means the cached JWT went stale early —
            # clock skew, or a key rotation. Drop it and the retry mints a new
            # one.
            if result.reason == "ExpiredProviderToken":
                async with self._token_lock:
                    self._token = None
                headers["authorization"] = f"bearer {await self._provider_token()}"

            await asyncio.sleep(delay)
            delay *= 2

        return PushResult(status=0, reason="exhausted", apns_id=apns_id)

    # --- payload shapes ---------------------------------------------------

    async def send_alert(
        self,
        *,
        token: str,
        production: bool,
        title: str,
        body: str,
        envelope: dict[str, Any],
        collapse_id: Optional[str] = None,
        interruption_level: str = "time-sensitive",
    ) -> PushResult:
        payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
                "interruption-level": interruption_level,
                # Tells iOS this is worth surfacing in a summary. Purely
                # cosmetic, but a glucose alert buried under a notification
                # summary is a glucose alert that did not happen.
                "relevance-score": 1.0,
            },
            "gloo": envelope,
        }
        return await self.send(
            token=token,
            payload=payload,
            push_type=PushType.ALERT,
            production=production,
            priority=10,
            # An alert about a reading is worthless an hour later. Let APNs
            # drop it rather than deliver history.
            expiration=int(time.time()) + 30 * 60,
            collapse_id=collapse_id,
        )

    async def send_background(
        self,
        *,
        token: str,
        production: bool,
        envelope: dict[str, Any],
    ) -> PushResult:
        """A silent nudge asking the app to run a sync cycle.

        Priority 5 is mandatory for a background push — APNs rejects priority 10
        with ``BadPriority``. iOS budgets these per app per day and will drop
        them freely, so nothing may depend on one arriving.
        """
        payload = {"aps": {"content-available": 1}, "gloo": envelope}
        return await self.send(
            token=token,
            payload=payload,
            push_type=PushType.BACKGROUND,
            production=production,
            priority=5,
            expiration=int(time.time()) + 10 * 60,
        )

    async def send_live_activity(
        self,
        *,
        token: str,
        production: bool,
        event: str,
        content_state: dict[str, Any],
        attributes_type: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
        stale_at: Optional[float] = None,
        dismiss_at: Optional[float] = None,
        alert: Optional[dict[str, str]] = None,
        relevance_score: Optional[float] = None,
        collapse_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> PushResult:
        """Start, update or end a Live Activity.

        ``event`` is ``start``, ``update`` or ``end``. A ``start`` must carry
        ``attributes`` and ``attributes-type`` and must be sent to the device's
        *push-to-start* token; ``update`` and ``end`` go to the running
        activity's own token. Sending one to the other's token fails in ways
        that are not obviously about that, so the service keeps them apart.

        ``timestamp`` is APNs' own ordering field and is required. It is not the
        same thing as the ``sequence`` inside the content state: this one lets
        *APNs* discard an out-of-order push, and the sequence lets the *phone*
        discard one that got through anyway.
        """
        aps: dict[str, Any] = {
            "timestamp": int(timestamp if timestamp is not None else time.time()),
            "event": event,
            "content-state": content_state,
        }
        if event == "start":
            if not attributes_type or attributes is None:
                raise ValueError("a start event needs attributes-type and attributes")
            aps["attributes-type"] = attributes_type
            aps["attributes"] = attributes
        if stale_at is not None:
            aps["stale-date"] = int(stale_at)
        if dismiss_at is not None:
            aps["dismissal-date"] = int(dismiss_at)
        if alert is not None:
            # An alert on a Live Activity push is what makes it announce itself
            # on a locked phone and on the watch, rather than appearing
            # silently on a screen nobody is looking at.
            aps["alert"] = alert
        if relevance_score is not None:
            aps["relevance-score"] = relevance_score

        payload = {"aps": aps}
        # Live Activity content states are capped at 4 KB by ActivityKit, and it
        # enforces that *silently*: APNs returns 200, the phone drops the
        # update, and the Lock Screen simply stops moving. Measuring it here is
        # the only place the number is ever visible.
        size = len(json.dumps(payload).encode())
        if size > 3500:
            logger.warning(
                "apns: live activity %s payload is %d bytes, close to ActivityKit's 4096 ceiling",
                event,
                size,
            )
        logger.debug("apns: live activity %s, %d bytes, schema %s", event, size, content_state.get("schema"))

        return await self.send(
            token=token,
            payload=payload,
            push_type=PushType.LIVE_ACTIVITY,
            production=production,
            topic=self._config.live_activity_topic,
            # Always 10.
            #
            # Priority 5 is documented as "the system may delay delivery, and
            # may coalesce or drop updates to save power", and that is exactly
            # what it does: APNs returns 200 and the update never reaches the
            # phone, most reliably in Low Power Mode and once the day's budget
            # is spent. That failure is indistinguishable from a broken service
            # from every angle except a device console.
            #
            # An app declaring `NSSupportsLiveActivitiesFrequentUpdates` — Gloo
            # does — is telling iOS these updates are worth the battery, and the
            # only thing on this Lock Screen is a hypo or a meal in progress.
            # Sending them at 5 was undoing that declaration.
            priority=10,
            expiration=int(time.time()) + 30 * 60,
            collapse_id=collapse_id,
        )
