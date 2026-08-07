"""Reading glucose and treatments out of Nightscout.

Three things here are different from the old script and all three were bugs:

* **Times are queried in epoch milliseconds, in UTC.** The old code built its
  window with ``datetime.now()`` — naive, therefore local — and then appended a
  literal ``"Z"`` to the ISO string, which asserts UTC. On a machine running
  anything but UTC that asked Nightscout for a window offset by the timezone;
  in Europe in summer, for two hours in the wrong direction. Querying ``date``
  (Nightscout's own epoch-ms field) sidesteps string formatting entirely.
* **Gaps are not interpolated.** The old code resampled onto a one-minute grid
  with pandas and linearly filled everything between, so a two-hour sensor
  outage became a smooth, entirely fictional line — and the alert rules then ran
  against invented values. A gap is information; it is what the staleness alert
  exists to notice.
* **pandas is gone.** It was pulled in to do a resample and an interpolation
  neither of which should have been happening. Removing it takes about 60 MB
  off the image and several seconds off a cold start, which for something that
  may run as a scheduled one-shot is most of its runtime.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Sequence

import httpx

from .config import NightscoutConfig
from .models import Reading, Treatment, sorted_readings

logger = logging.getLogger(__name__)


class NightscoutError(RuntimeError):
    pass


class NightscoutClient:
    def __init__(self, config: NightscoutConfig, timeout: float = 15.0):
        self._config = config
        headers = {"Accept": "application/json"}
        if config.api_secret:
            # Nightscout accepts the SHA-1 of the API secret in this header;
            # sites that use a token instead pass it as a query parameter.
            headers["api-secret"] = config.api_secret
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NightscoutClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params = dict(extra)
        if self._config.token:
            params["token"] = self._config.token
        return params

    async def _get(self, path: str, params: dict[str, Any], attempts: int = 3) -> Any:
        url = f"{self._config.base_url}{path}"
        delay = 1.0
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                response = await self._client.get(url, params=self._params(params))
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt + 1 == attempts:
                    break
                await asyncio.sleep(delay)
                delay *= 2

        raise NightscoutError(f"GET {path} failed after {attempts} attempts: {last_error}")

    async def entries(self, since: float, now: Optional[float] = None) -> list[Reading]:
        """Glucose readings from ``since`` (epoch seconds) to now, ascending."""
        now = time.time() if now is None else now
        raw = await self._get(
            "/api/v1/entries.json",
            {
                "find[date][$gte]": int(since * 1000),
                "find[date][$lte]": int(now * 1000),
                # Generous but bounded: three hours of five-minute readings is
                # 36, and a site that returns thousands is one with duplicate
                # uploaders, not one with more information.
                "count": 600,
            },
        )

        readings: list[Reading] = []
        for entry in raw or []:
            value = entry.get("sgv")
            if value is None:
                # Calibrations and meter readings come back on the same
                # endpoint with `mbg` instead. Skipped rather than mixed in:
                # they are not what the CGM trend is made of.
                continue
            at = entry.get("date")
            if at is None:
                continue
            try:
                readings.append(Reading(at=float(at) / 1000.0, mgdl=float(value)))
            except (TypeError, ValueError):
                continue

        return _deduplicated(sorted_readings(readings))

    async def treatments(self, since: float) -> list[Treatment]:
        """Carbohydrate and insulin entries, ascending by time."""
        raw = await self._get(
            "/api/v1/treatments.json",
            {
                # `created_at` is an ISO string on this collection, so this one
                # genuinely has to be formatted — in UTC, explicitly.
                "find[created_at][$gte]": _iso_utc(since),
                "count": 300,
            },
        )

        treatments: list[Treatment] = []
        for entry in raw or []:
            at = _parse_created_at(entry.get("created_at"))
            if at is None:
                continue
            carbs = _number(entry.get("carbs"))
            insulin = _number(entry.get("insulin"))
            if carbs <= 0 and insulin <= 0:
                continue
            treatments.append(Treatment(at=at, carbs=carbs, insulin=insulin))

        treatments.sort(key=lambda t: t.at)
        return treatments


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _iso_utc(epoch_seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_created_at(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone

    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Nightscout occasionally stores a naive string. Treating it as UTC is
        # what the rest of the site does, and guessing the server's local zone
        # would be worse.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _deduplicated(readings: Sequence[Reading]) -> list[Reading]:
    """Collapse readings that share a timestamp.

    Two uploaders writing the same sensor is common, and a duplicated tail
    makes the slope calculation read zero — which is a *steady* arrow beside a
    falling number, the one direction the trend must never be wrong in.
    """
    result: list[Reading] = []
    for reading in readings:
        if result and abs(result[-1].at - reading.at) < 1:
            continue
        result.append(reading)
    return result
