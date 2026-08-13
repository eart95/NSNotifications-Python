"""Configuration, read once from the environment and validated loudly.

The old script read ``os.getenv`` at import time, all over the module, and
carried on regardless when something was missing — including the APNs key,
which it fetched over plain HTTP from a web server on every single push. A
misconfigured deployment therefore looked exactly like a quiet night.

Everything the service needs is gathered here, checked at startup, and a
missing or nonsensical value stops the process rather than producing a
notifier that silently notifies nobody.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ConfigError(RuntimeError):
    """A deployment problem, not a runtime one. Always fatal at startup."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from error


@dataclass(frozen=True)
class APNsConfig:
    """The provider-token credentials, held in memory for the process's life.

    ``auth_key`` is the *contents* of the .p8, not a URL to it. The old script
    downloaded the key from a password-protected web directory before every
    notification: a network round trip and an exposed private key on the path
    where a hypo alert has to be fastest, and a total outage whenever that web
    server hiccupped. Secrets belong in the environment, or in whatever secret
    store the platform provides that populates it.
    """

    key_id: str
    team_id: str
    bundle_id: str
    auth_key: str

    @property
    def live_activity_topic(self) -> str:
        """APNs requires this exact suffix for Live Activity pushes."""
        return f"{self.bundle_id}.push-type.liveactivity"


@dataclass(frozen=True)
class NightscoutConfig:
    base_url: str
    token: str = ""
    api_secret: str = ""
    # How much history to pull each tick. Two hours covers the sparkline the
    # Live Activity draws with room for a gap; more is wasted bandwidth on a
    # cycle that runs every five minutes.
    history_hours: float = 3.0
    # Treatments reach further back because carbohydrate absorbs for hours and
    # insulin acts for six.
    treatment_hours: float = 8.0


@dataclass(frozen=True)
class Config:
    apns: APNsConfig
    nightscout: NightscoutConfig
    # Bearer token devices present when registering. Without it, anyone who
    # finds the URL can enrol a device token or read one back.
    shared_secret: str
    database_path: Path
    # Seconds between ticks in `serve`/`worker`.
    #
    # One minute, and deliberately faster than any card's own cadence. The tick
    # is no longer what decides when a push goes out — each kind sets that for
    # itself, two minutes for a low and five for a meal — so what the interval
    # actually governs is how quickly the service *notices*: that glucose went
    # under 80, that a meal was logged, that a fall needs to take the card over
    # from a meal. At a five-minute tick a low could be four minutes old before
    # anything appeared, which is most of the time in which it mattered.
    #
    # The cost is reads, not pushes: most ticks re-read the same Nightscout
    # reading and decide nothing is due.
    poll_interval: int = 60
    # Jitter, so a fleet of these does not stampede a Nightscout site on the
    # minute boundary.
    poll_jitter: int = 10
    # How often, at most, to send a device a silent "go and sync" push *while
    # nothing is on the Lock Screen*. iOS budgets background pushes per app per
    # day and drops them silently once an app spends too many, so this is
    # rationed rather than sent every tick. Zero disables them entirely.
    #
    # While a card *is* running, this does not apply: a sync is paired with
    # every activity push instead, because a Live Activity push runs no app
    # code at all and the app would otherwise be showing stale data behind a
    # current Lock Screen. See `NotifierService._maybe_refresh`.
    refresh_push_interval: float = 1800.0
    # A device that has not re-registered in this long has stopped launching
    # the app. Pushing to it wastes APNs calls and, for Live Activities,
    # addresses tokens that are certainly dead.
    registration_ttl_hours: float = 72.0
    # Optional dead-man's switch. Hit after every successful tick; if the
    # service dies, the *absence* of these is what raises the alarm. A glucose
    # notifier that fails silently is worse than one that was never deployed,
    # because the user has stopped watching for themselves.
    heartbeat_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    # Only for local development against a fake APNs.
    apns_host_override: str = ""

    @staticmethod
    def from_environment() -> "Config":
        auth_key = os.getenv("APNS_AUTH_KEY", "")
        key_path = _optional("APNS_AUTH_KEY_PATH")
        if not auth_key.strip() and key_path:
            try:
                auth_key = Path(key_path).read_text()
            except OSError as error:
                raise ConfigError(f"APNS_AUTH_KEY_PATH could not be read: {error}") from error
        if not auth_key.strip():
            raise ConfigError(
                "APNS_AUTH_KEY (the contents of your .p8) or APNS_AUTH_KEY_PATH is required"
            )
        if "PRIVATE KEY" not in auth_key:
            raise ConfigError(
                "APNS_AUTH_KEY does not look like a PEM private key. "
                "Paste the whole .p8 file including the BEGIN/END lines."
            )

        base_url = _require("NIGHTSCOUT_URL").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("NIGHTSCOUT_URL must include a scheme")

        return Config(
            apns=APNsConfig(
                key_id=_require("APNS_KEY_ID"),
                team_id=_require("APNS_TEAM_ID"),
                bundle_id=_require("APNS_BUNDLE_ID"),
                auth_key=auth_key,
            ),
            nightscout=NightscoutConfig(
                base_url=base_url,
                token=_optional("NIGHTSCOUT_TOKEN"),
                api_secret=_optional("NIGHTSCOUT_API_SECRET"),
                history_hours=_float("NIGHTSCOUT_HISTORY_HOURS", 3.0),
                treatment_hours=_float("NIGHTSCOUT_TREATMENT_HOURS", 8.0),
            ),
            shared_secret=_require("RELAY_SHARED_SECRET"),
            database_path=Path(_optional("DATABASE_PATH", "/data/nsnotifier.sqlite3")),
            poll_interval=_int("POLL_INTERVAL_SECONDS", 60),
            poll_jitter=_int("POLL_JITTER_SECONDS", 10),
            refresh_push_interval=_float("REFRESH_PUSH_INTERVAL_SECONDS", 1800.0),
            registration_ttl_hours=_float("REGISTRATION_TTL_HOURS", 72.0),
            heartbeat_url=_optional("HEARTBEAT_URL"),
            host=_optional("HOST", "0.0.0.0"),
            port=_int("PORT", 8080),
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
            apns_host_override=_optional("APNS_HOST_OVERRIDE"),
        )
