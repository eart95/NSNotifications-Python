# Gloo notification service

Watches a Nightscout site and pushes to registered iPhones: glucose alerts,
silent sync nudges, and Live Activities that start, update and end themselves on
the Lock Screen while the app is not running.

It is the server half of a system whose other half is
[glooMDI-healthkit](https://github.com/eart95/glooMDI-healthkit). Neither half
depends on the other being up — the app schedules its own local notifications
and the two are deduplicated — but the app on its own can only act when iOS
chooses to wake it, which for a phone whose glucose arrives from Nightscout can
be an hour, and overnight can be longer.

**This is not a hypo alarm.** Nothing here can override the ringer switch; that
needs Apple's Critical Alerts entitlement. Keep your CGM app's own alarms on.

---

## What it sends

| | When | How |
| --- | --- | --- |
| **Alerts** | Low, high, predicted low, no data — four kinds, each damped for 30 minutes after it fires | Visible `time-sensitive` push |
| **Live Activity** | A hypo (measured or forecast), or 30 g+ of carbohydrate in half an hour. Both clear when glucose is back in range | Push-to-start, then an update every 2 minutes, then end |
| **Silent refresh** | At most every 30 minutes | `content-available`, so the app syncs and re-arms its own local alerts |

The rules live in `nsnotifier/episodes.py` and `nsnotifier/alerts.py`, both of
which are pure functions and both of which **mirror Swift files in the app's
MDIKit package**. `tests/test_episodes.py` mirrors `GlucoseEpisodeTests.swift`
case for case. If you change a rule in one, change it in both — otherwise a Live
Activity appears or disappears depending on whether the app happened to be open.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 87 tests, no network, ~2s
```

```bash
docker build -t nsnotifier .
docker run -p 8080:8080 -v nsnotifier-data:/data \
  -e NIGHTSCOUT_URL='https://you.example.com' \
  -e NIGHTSCOUT_TOKEN='...' \
  -e APNS_KEY_ID='...' -e APNS_TEAM_ID='...' \
  -e APNS_BUNDLE_ID='com.enricoartuso.GlooMDI' \
  -e APNS_AUTH_KEY="$(cat AuthKey_XXXX.p8)" \
  -e RELAY_SHARED_SECRET="$(openssl rand -hex 32)" \
  -e HEARTBEAT_URL='https://hc-ping.com/your-uuid' \
  nsnotifier
```

Then, in Gloo: **Settings → Notifications → Notification service** — switch it
on, paste the HTTPS URL and the same shared secret, and tap *Save and Register*.
The status card underneath reports whether iOS issued a token, whether the
push-to-start token arrived, which APNs environment the server will use, and
when the last registration was accepted.

**Already running the old `script.py`? Read [`docs/MIGRATION.md`](docs/MIGRATION.md) first** —
several environment variables changed, and the device-token file and the
web-hosted `.p8` both have to go.

## Configuration

| Variable | Required | Default | |
| --- | --- | --- | --- |
| `NIGHTSCOUT_URL` | ✔ | | Site root, no trailing path |
| `NIGHTSCOUT_TOKEN` | | | Scoped read token |
| `NIGHTSCOUT_API_SECRET` | | | Only if your site needs it instead |
| `APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_BUNDLE_ID` | ✔ | | |
| `APNS_AUTH_KEY` | ✔ | | The `.p8` **contents**, `BEGIN`/`END` lines included |
| `APNS_AUTH_KEY_PATH` | | | Alternative, if secrets are mounted as files |
| `RELAY_SHARED_SECRET` | ✔ | | Bearer token the app presents; also goes in the app |
| `DATABASE_PATH` | | `/data/nsnotifier.sqlite3` | **Put it on a volume** |
| `POLL_INTERVAL_SECONDS` | | `120` | `serve`/`worker` only. Two minutes, so a running Live Activity is never far behind |
| `REFRESH_PUSH_INTERVAL_SECONDS` | | `1800` | `0` disables silent pushes |
| `REGISTRATION_TTL_HOURS` | | `72` | Devices quieter than this stop being pushed to |
| `HEARTBEAT_URL` | | | Dead-man's switch. Strongly recommended |
| `PORT` / `HOST` | | `8080` / `0.0.0.0` | |
| `LOG_LEVEL` | | `INFO` | |

Anything missing or nonsensical stops the process at startup, rather than
producing a notifier that silently notifies nobody.

## Modes

```
python -m nsnotifier serve    # HTTP API + polling loop, one process (default)
python -m nsnotifier api      # HTTP only; something external calls POST /v1/tick
python -m nsnotifier worker   # loop only; pair with an `api` process on the same volume
python -m nsnotifier once     # one tick, then exit — the cron-job shape
```

`once` exits non-zero when the tick failed, which is what makes a bad run
*visible* to whatever scheduled it.

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) works through when each is the right
answer, and what is actually unreliable about a cron job.

## HTTP API

| | | |
| --- | --- | --- |
| `GET /v1/health` | open | `503` when the last tick failed — not merely when the process is gone |
| `PUT /v1/devices/{id}` | bearer | Register or update a device. Idempotent |
| `DELETE /v1/devices/{id}` | bearer | Take a device off |
| `POST /v1/tick` | bearer | Run one cycle now |
| `GET /v1/diagnostics` | bearer | Registered devices and every push of the last seven days, with Apple's reason for each |

## Layout

```
nsnotifier/
  config.py       environment, validated at startup
  models.py       the vocabulary shared with the phone — field names are the wire format
  nightscout.py   entries and treatments, in UTC, without interpolating over gaps
  physiology.py   IOB, COB, and a deliberately momentum-only forecast
  episodes.py     when a Live Activity should exist   ←→ GlucoseEpisode.swift
  alerts.py       when to interrupt someone           ←→ GlucoseAlert.swift
  activity.py     the Live Activity content state     ←→ GlucoseActivityAttributes.swift
  apns.py         provider tokens, push types, hosts, and pruning dead tokens
  store.py        SQLite: devices, per-device state, push log
  service.py      one tick
  api.py          registration and health
  runtime.py      wiring and the loop
```

## Notes for anyone changing this

* **Every instant on the wire is Unix epoch seconds, as a number.** ActivityKit
  decodes a pushed `content-state` with a stock `JSONDecoder`, whose default
  date strategy is seconds since 2001. A date encoded any other way arrives 31
  years wrong, on the Lock Screen, during a hypo.
* **Copy contains no clock times.** The state carries `eventAt` as an instant
  and the widget formats it; a server does not know the phone's locale, its
  12/24-hour preference or its time zone.
* **A content state must stay under 4 KB.** ActivityKit drops oversized ones
  silently — the activity simply never updates again.
* **Sequence numbers only ever increase.** APNs promises no ordering, and the
  phone drops any state not ahead of what it already shows.
* **A two-minute update cadence needs the app's consent.** iOS budgets Live
  Activity pushes and drops them silently past it unless the app declares
  `NSSupportsLiveActivitiesFrequentUpdates`. Gloo does; anything else consuming
  this service would have to.
* **An update is only sent to a token the phone has vouched for.** The service
  pushes when `activityToken` is present *and* `activityEpisodeID` matches the
  episode it believes is running, and skips otherwise. That is deliberate — the
  alternative is pushing to a token that may address an activity that ended —
  but it means a phone that loses track of the pairing produces a frozen Lock
  Screen and a *correctly* skipped push. `GlucoseActivityRegistrar.swift` in the
  app is what keeps that pair current, and every skip here says which half was
  missing.
* **A 200 to a start push is not a Live Activity.** Apple accepted the push; the
  phone still has to create the activity and come back with its token. When it
  does not, the episode would otherwise spend its whole life on the update path
  skipping every push for a card that does not exist — so a start is retried up
  to four times, eight minutes apart, before the episode carries on as
  alert-only.
* **This service is the sole owner of Live Activities.** The app does not start,
  end or replace them; it discovers what the service started and reports the
  token that addresses it. Two systems minting episode identities for the same
  hypo is what froze Lock Screens before, because neither could push to the
  other's activity.
