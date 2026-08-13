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
| **Live Activity** | One card, four reasons for it to exist — see below. Push-to-start once, updates at the cadence the current reason asks for, end once | Push-to-start, updates, end |
| **Silent refresh** | Alongside every Live Activity push, and at most every 30 minutes otherwise | `content-available`, so the app syncs and re-arms its own local alerts |

### One card, four reasons

There is never more than one Live Activity. It is called a **session**, it is
started once and ended once, and the *reason* it is on screen — an **episode** —
can change underneath it without the card going anywhere:

| Episode | Starts | Ends | Refreshed |
| --- | --- | --- | --- |
| **Low glucose** | Below the suspend threshold + 10 (80 mg/dL by default) | Above threshold + 15 for 15 minutes | every 2 min |
| **Moving fast** | ±3 mg/dL/min in one direction for 15 minutes | Under 1 mg/dL/min for 15 minutes | every 2 min falling, 5 rising |
| **After a meal** | 30 g or more of carbohydrate logged inside 20 minutes | 3 hours, or 30 minutes steady and in range | every 5 min |
| **Manual** | `POST /v1/devices/{id}/request-start`, i.e. the button in the app | 2 hours | every 5 min |

When a more urgent episode applies, the card **switches in place** — one update
push, no end, no push-to-start, no gap on the Lock Screen — and the displaced
episode waits: a manual card interrupted by a low comes back for the rest of its
two hours when the low clears. That is the whole reason the episode kind lives
in the content state rather than in the activity's attributes, which ActivityKit
freezes at creation.

The rules live in `nsnotifier/episodes.py` and `nsnotifier/alerts.py`, both of
which are pure functions and both of which **mirror Swift files in the app's
MDIKit package**. `tests/test_episodes.py` mirrors `GlucoseEpisodeTests.swift`
case for case. If you change a rule in one, change it in both — otherwise a Live
Activity appears or disappears depending on whether the app happened to be open.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 154 tests, no network, ~4s
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
| `POLL_INTERVAL_SECONDS` | | `60` | `serve`/`worker` only. How fast the service *notices*; each episode's own cadence decides when it pushes |
| `REFRESH_PUSH_INTERVAL_SECONDS` | | `1800` | Silent pushes while nothing is on screen. `0` disables. While a card is running, one is paired with every activity push regardless |
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
| `POST /v1/devices/{id}/test` | bearer | Send a test alert now, and a test Live Activity update if one is running |
| `POST /v1/devices/{id}/request-start` | bearer | Start a Live Activity now, outside the rules |
| `POST /v1/devices/{id}/dismiss` | bearer | The user swiped the card away; stop keeping it alive |
| `GET /v1/diagnostics` | bearer | Registered devices and every push of the last seven days, with Apple's reason for each |

### `POST /v1/devices/{id}/request-start`

```bash
curl -X POST -H "Authorization: Bearer $RELAY_SHARED_SECRET" \
     -H 'Content-Type: application/json' -d '{"durationSeconds": 7200}' \
     https://your-service/v1/devices/$DEVICE_ID/request-start
```

Everything else here starts a Live Activity because glucose said so. This starts
one because a person asked — both a feature in its own right and the only way to
answer "does push-to-start reach this phone" without waiting for a low.

`durationSeconds` is optional (default two hours, from the device's own settings)
and clamped to between five minutes and eight hours, because iOS ends an activity
at eight whatever anyone asks for.

The card it starts is an ordinary session holding a `manual` episode: refreshed
by every tick on the manual cadence, and **ended on its own clock** when the
duration runs out. Unlike every other episode, nothing about glucose ends it —
the user asked for two hours of card, and giving them twenty minutes because
glucose looked tidy is not answering the request.

What it does not get is priority. The moment the rules say something real is
happening the card **changes to it in place**, and the manual episode waits its
turn and comes back afterwards if it has time left. A request is refused with
`409` while a card is already running; `POST /test` is the right tool then.

| | |
| --- | --- |
| `200` | APNs accepted the push. Body carries `sessionID`, `episodeKind`, `durationSeconds`, `expiresAt`, `apnsStatus` |
| `400` | No push-to-start token registered, Live Activities switched off in Gloo, or a `durationSeconds` that is not a positive number |
| `404` | No such device |
| `409` | A card is already on the Lock Screen |
| `502` | APNs refused it. Body carries `apnsStatus` and `apnsReason`; a 410 also drops the dead token |
| `503` | Nightscout unreachable, or no recent reading to put on the card — deliberately not a 502, because that sends you somewhere else entirely |

### `POST /v1/devices/{id}/dismiss`

```bash
curl -X POST -H "Authorization: Bearer $RELAY_SHARED_SECRET" \
     -H 'Content-Type: application/json' -d '{"sessionID": "s.1770000000"}' \
     https://your-service/v1/devices/$DEVICE_ID/dismiss
```

Sent by the app when the user swipes the card away. Without it the service keeps
an ended activity in its head, finds no token to update, and — because a session
with no activity looks exactly like a start push that never arrived — pushes a
start again. A card that comes back after being dismissed is worse than one that
never appeared.

`sessionID` is optional but worth sending: a dismissal that arrives after the
card it refers to has been replaced answers `{"status": "ignored"}` rather than
taking down its successor. Dismissing stamps the episode kind's restart cooldown,
so the same glucose cannot immediately re-derive the same card.

## Layout

```
nsnotifier/
  config.py       environment, validated at startup
  models.py       the vocabulary shared with the phone — field names are the wire format
  nightscout.py   entries and treatments, in UTC, without interpolating over gaps
  physiology.py   IOB, COB, and a deliberately momentum-only forecast
  episodes.py     what the one card should be about  ←→ GlucoseEpisode.swift
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
  pushes when `activityToken` is present *and* `activitySessionID` matches the
  card it believes is running, and skips otherwise. That is deliberate — the
  alternative is pushing to a token that may address an activity that ended —
  but it means a phone that loses track of the pairing produces a frozen Lock
  Screen and a *correctly* skipped push. `GlucoseActivityRegistrar.swift` in the
  app is what keeps that pair current, and every skip here says which half was
  missing.
* **A 200 from APNs is not a delivery.** It means Apple accepted the push. The
  payload can still be dropped on the device with no trace anywhere: at
  `apns-priority: 5` the system is explicitly allowed to defer or discard Live
  Activity updates, a content state over 4 KB is refused silently, and Low Power
  Mode suspends them outright. Every Live Activity push here therefore goes at
  priority 10, payload sizes are logged, and `POST /v1/devices/{id}/test` exists
  so the question can be answered in five seconds rather than by waiting for a
  hypo. The phone's own answer is in Settings → Notifications → "Last push
  received".
* **A 200 to a start push is not a Live Activity.** Apple accepted the push; the
  phone still has to create the activity and come back with its token. When it
  does not, the episode would otherwise spend its whole life on the update path
  skipping every push for a card that does not exist — so a start is retried up
  to four times, eight minutes apart, before the episode carries on as
  alert-only.
* **A start push has three requirements APNs does not check.** `attributes-type`
  and `attributes`; an **`alert`**; and **`input-push-token: 1`**. Apple lists
  all three in [Starting and updating Live Activities with ActivityKit push
  notifications](https://developer.apple.com/documentation/activitykit/starting-and-updating-live-activities-with-activitykit-push-notifications).
  A start missing any of them is accepted with a 200 and discarded on the
  device — no activity, no error, nothing in any log anywhere. Two of them were
  missing here, and between them they account for every symptom this feature has
  ever had:
  * The **alert** was sent only for hypo cards, on the reasoning that a meal
    should not buzz. That is a real concern and it belongs in `sound`, which is
    the optional part; the alert dictionary itself is not optional. Meal cards
    therefore never appeared at all — and since `request-start` picks the meal
    kind unless the user is actually hypo, the one path anyone ever tests was
    the one path that was malformed. `POST /v1/devices/{id}/request-start`
    passed no alert whatever.
  * **`input-push-token: 1`** is what asks iOS 18+ to mint an update token for
    the activity being started and hand it to `pushTokenUpdates`. Without it a
    card can appear and still have nothing addressing it, so every later update
    is skipped for a pairing the phone was never given — which reads from the
    server, and from the app, as "the device failed to register its token".
  `send_live_activity` now sets `input-push-token` itself and raises on a start
  with no alert, rather than leaving either to a call site. The failure is
  invisible everywhere else, so the only place it can be caught is before the
  push goes out.
* **This service is the sole owner of Live Activities.** The app does not start,
  end or replace them; it discovers what the service started and reports the
  token that addresses it. Two systems minting identities for the same card is
  what froze Lock Screens before, because neither could push to the other's
  activity. The one thing the app now tells the service is that the *user* ended
  a card — `POST /dismiss` — which is not a decision, it is a fact only the
  phone has.
* **The kind belongs in the state, never in the attributes.** ActivityKit freezes
  attributes at creation. Anything in them is a thing the card cannot change its
  mind about, and this feature is largely about changing its mind — a meal card
  that goes low becomes a low card, with the low's copy and the low's
  two-minute cadence, on the same activity, with one push. The attributes carry
  a session id and a start instant and nothing else.
* **A Live Activity push runs no app code.** iOS renders it in the widget
  extension; the app is not woken and never learns it happened. So a silent
  `content-available` push is sent alongside every activity push — otherwise the
  Lock Screen would be current while the app behind it showed whatever it had
  when it was last opened. iOS budgets those and will drop them; nothing depends
  on one arriving.
