# Migrating

Two migrations live here. **If you are already running `nsnotifier`** — the
long-running service, with `APNS_AUTH_KEY` in the environment — you want
[§0a](#0a-upgrading-an-existing-nsnotifier-deployment) and nothing else: no
secret moves, no variable is renamed, and the deploy is the whole job.

The rest of the page is the older migration, from the original `script.py` that
fetched a `.p8` over HTTP before every push.

---

## 0a. Upgrading an existing nsnotifier deployment

**Nothing about your configuration changes.** `APNS_AUTH_KEY` (or
`APNS_AUTH_KEY_PATH`), `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`,
`NIGHTSCOUT_URL`, `NIGHTSCOUT_TOKEN`, `RELAY_SHARED_SECRET`, `DATABASE_PATH`,
`HEARTBEAT_URL` — all keep their names and their values. Redeploy the image and
the running secrets carry over untouched. Nothing needs to be re-pasted, and the
`.p8` never has to be handled again.

Two things do change, and both settle by themselves:

* **`POLL_INTERVAL_SECONDS` now defaults to 60** rather than 120. The tick is no
  longer what decides when a push goes out — each episode kind sets its own
  cadence — so the interval now governs how quickly the service *notices* a low
  or a meal. If you set the variable explicitly, lower it to 60; if you did not,
  there is nothing to do.
* **The per-device working state has a new shape.** The row that used to hold
  `episode` and `manual` now holds a single `session`. Old rows are simply not
  read: the service starts the next tick believing no card is running, which is
  true within a minute of the deploy anyway. Alert cooldowns (`lastFired`) are
  untouched, so a redeploy still cannot re-announce a low you are treating.

If a Live Activity happens to be on the Lock Screen across the upgrade, it is
from the previous wire format and cannot be addressed by the new one. The app
build ends any such card on its first launch; if the phone is not opened, iOS
expires it on its own. Nothing has to be done about it.

---

# Migrating from the old script

You are running `script.py` against a previous version of Gloo. This is what
changes, in the order you have to do it, and why each change exists.

The short version: **the script's job has changed from "decide and shout" to
"decide, and address a specific device that told you how to reach it"**. Almost
everything else follows from that.

---

## 0. What still works unchanged

`script.py` is still there and still runs one cycle and exits, so an existing
scheduled command (`python script.py`) keeps working through the transition. It
now forwards to `python -m nsnotifier once`. Nothing about your scheduler has to
change on day one.

What *cannot* keep working is the configuration: the new code refuses to start
without the variables below, rather than starting and quietly notifying nobody,
which is what the old one did when `APNS_P8_FILE` was unreachable.

---

## 1. Stop hosting the device tokens and the private key

The old setup fetched two things over HTTP from `nightscout.enricoartuso.com`
on every run:

* `device_tokens.txt`, a comma-separated list of APNs tokens, edited by hand;
* the `.p8` APNs signing key, over Basic auth, **before every single push**.

Both have to go, and not only for tidiness:

* **A device token is not a constant.** iOS reissues it after a restore, after
  some updates, and whenever it feels like it. APNs answers a push to a retired
  token with `410 Unregistered`. A hand-edited file cannot learn that, so it
  accumulates dead tokens — and, much worse, silently stops covering a phone
  whose token moved. You would find out during a hypo.
* **A private key fetched per push** is a network round trip and a plaintext
  secret on the exact path where a hypo alert most needs to be fast, plus a
  total outage of your glucose alarms whenever an unrelated web host hiccups.

Replace them with:

* **Registration.** The app now `PUT`s its tokens and its thresholds to
  `/v1/devices/{id}` on every launch and on every settings change. The service
  stores them in SQLite and prunes what APNs tells it is dead.
* **`APNS_AUTH_KEY`**, the *contents* of the `.p8`, in the environment. Paste
  the whole file, `BEGIN`/`END` lines included. (`APNS_AUTH_KEY_PATH` works too
  if your platform mounts secrets as files.)

You can delete `device_tokens.txt`, `NSNotifier-Persistent.json` and
`write-JSON.php` from that web server once the new service has been running for
a day.

## 2. New and changed environment variables

| Variable | Status | Notes |
| --- | --- | --- |
| `NIGHTSCOUT_URL` | unchanged | Site root, e.g. `https://you.example.com`. No trailing path. |
| `NIGHTSCOUT_TOKEN` | unchanged | Scoped read token. |
| `NIGHTSCOUT_API_SECRET` | new, optional | Only if your site needs it instead of a token. |
| `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID` | unchanged | |
| `APNS_AUTH_KEY` | **replaces `APNS_P8_FILE`** | The key itself, not a URL to it. |
| `RELAY_SHARED_SECRET` | **new, required** | Bearer token the app presents when registering. Generate one: `openssl rand -hex 32`. Paste the same value into Gloo → Settings → Notifications → Notification service. |
| `DATABASE_PATH` | **new** | Defaults to `/data/nsnotifier.sqlite3`. **Must be on a persistent volume** — see §5. |
| `PERSISTENT_STORAGE_URL`, `PERSISTENT_STORAGE_USERNAME`, `PERSISTENT_STORAGE_PW` | **delete** | Replaced by SQLite. |
| `DEVICE_TOKENS` | **delete** | Replaced by registration. |
| `POLL_INTERVAL_SECONDS` | new, optional | Default 60. Only used by `serve`/`worker`. How quickly the service notices; each episode kind sets its own push cadence. |
| `REFRESH_PUSH_INTERVAL_SECONDS` | new, optional | Default 1800. Silent "go and sync" pushes while nothing is on the Lock Screen. 0 disables. While a card runs, one is paired with every activity push regardless. |
| `HEARTBEAT_URL` | new, optional | Strongly recommended. See §6. |
| `PORT` | new, optional | Default 8080. The registration API. |

## 3. The service now needs to be reachable from the phone

This is the one genuinely new operational requirement. The old script only made
outbound connections; this one also serves `PUT /v1/devices/{id}` so the app can
register.

It must be **HTTPS** — the app refuses a plain-`http://` service URL, because a
bearer token and a stream of glucose readings should not travel in the clear.
Any platform that terminates TLS for you (Northflank, Fly, Railway, Cloud Run, a
Caddy or Traefik in front of a VPS) is fine.

Then, in the app: **Settings → Notifications → Notification service**, switch it
on, paste the URL and the shared secret, and tap *Save and Register*. The status
card underneath tells you whether iOS issued a push token, whether the
push-to-start token arrived, which APNs environment the server must use, and
whether the last registration was accepted.

## 4. Your APNs host is no longer hardcoded

The old script always pushed to `api.sandbox.push.apple.com`. That is why it
worked from an Xcode build and went silent the moment the same app arrived
through TestFlight: a token minted against production is meaningless to the
sandbox host, and the `400 BadDeviceToken` you get back looks exactly like a
corrupt token.

The phone now reports its own environment — read from the `aps-environment`
entitlement in its embedded provisioning profile — and the service pushes to
whichever host matches. **You do not have to configure anything for this**, but
it does mean a TestFlight build and a debug build on the same phone are two
different device rows, which is correct.

## 5. The state file has to survive a restart

The old script kept its alert cooldowns in a JSON file it fetched and re-uploaded
over HTTP. Two problems: a read-modify-write with no locking, so two overlapping
runs lost one of their updates; and a dependency on an unrelated web host.

State is now SQLite at `DATABASE_PATH`. **Mount a volume there.** Without one the
service still runs, but every restart forgets which devices exist and when each
alert last fired — so a redeploy during a hypo re-announces it, at the moment
the user is least able to tell a new alert from an echo.

## 6. Set up a dead-man's switch

Set `HEARTBEAT_URL` to a check on [healthchecks.io](https://healthchecks.io) or
equivalent. The service pings it after every *successful* tick.

This is the single highest-value thing on this page. Everything else in this
system fails loudly. A notifier that has simply **stopped** fails silently — and
by then the user has delegated noticing to it. The absence of the pings is what
raises the alarm; a period of "no alerts" otherwise looks exactly like a good
night.

## 7. What the alerts do now

The old script had ten kinds — extreme high, extreme low, high, low, rapid rise,
rapid fall, upward trend, downward trend, time-in-range, post-meal — behind a
priority ladder. There are four now: **predicted low, low, high, no data.**

That is a deliberate reduction, and there was also a bug worth knowing about:
the per-kind cooldowns were all read from a single shared key
(`data.get('last_alert_time')` regardless of `alert_name`), so the ten
individually tuned cooldown periods were never actually per-kind. With a second
device registered, they were not even per-device.

Ten kinds is not ten times the information. It is a phone that buzzes so often
that the one alert that mattered arrives looking like all the others.

The high threshold now comes off the phone (Settings → Notifications → High
threshold, default 250) rather than being a constant in the script, and so do
the low threshold (your suspend threshold), the in-range band, and every
episode threshold. The phone owns its settings; the service is told them, on
every registration.

## 8. What is new: Live Activities

The service can now start, update and end the Lock Screen Live Activity while
the app is not running at all. That is what push-to-start tokens are for, and it
is not something the app can do for itself — ActivityKit refuses a local start
from the background and offers no way to queue one.

There is exactly one card — a **session** — and four reasons for it to exist:

* **Low glucose** — under the suspend threshold + 10 (80 mg/dL by default).
  Clears once glucose has been above threshold + 15 for a quarter of an hour.
  Refreshed every 2 minutes.
* **Moving fast** — ±3 mg/dL/min in the same direction for 15 minutes. Clears
  after 15 minutes under 1 mg/dL/min. Refreshed every 2 minutes falling, 5
  rising: a fast fall is the one worth watching closely.
* **After a meal** — 30 g or more of carbohydrate logged inside 20 minutes.
  Ends after 3 hours, or early once glucose has been steady and in range for
  half an hour. Refreshed every 5 minutes.
* **Manual** — the button in the app. Two hours, and nothing about glucose ends
  it. Refreshed every 5 minutes.

The card **switches between them in place**: going low during a meal is one
update push, not an end and a new start, and the meal comes back afterwards if
it still has time. Every push also carries a silent sync alongside it, so the
app's own data keeps step with the Lock Screen.

See `nsnotifier/episodes.py` for the implementation — which mirrors
`GlucoseEpisode.swift` line for line, with `tests/test_episodes.py` mirroring
`GlucoseEpisodeTests.swift` case for case.

**Note on the forecast.** This service does not have your insulin sensitivity or
carb ratio, and should not: dosing maths that is subtly wrong is dangerous in a
way that a missed notification is not. Its projection is momentum only —
velocity decaying to nothing over half an hour — which is a poor predictor of an
hour out and a decent one of twenty minutes, which is the window a hypo warning
needs. The app's own forecast runs the real algorithm and takes over whenever
the app is awake.

## 9. Two systems, one voice

Both the phone and the service can decide the same low needs announcing, often
within a minute of each other. When the service delivers an alert, its payload
carries a `gloo` envelope naming the kind; the app records that as a delivery in
the same ledger its local re-alert damping already reads, so it falls quiet for
the same 30 minutes it would after one of its own.

It works in one direction only, on purpose. A *local* alert may never actually
be delivered — the user can have notifications off, or iOS may simply never wake
the app — and silencing the service on the strength of one nobody saw would turn
a silent phone into a silent everything.

---

## Checklist

- [ ] Generate `RELAY_SHARED_SECRET` (`openssl rand -hex 32`).
- [ ] Move the `.p8` contents into `APNS_AUTH_KEY`; remove `APNS_P8_FILE`.
- [ ] Remove `PERSISTENT_STORAGE_*` and `DEVICE_TOKENS`.
- [ ] Mount a volume and point `DATABASE_PATH` at it.
- [ ] Expose the service over HTTPS.
- [ ] Set `HEARTBEAT_URL`.
- [ ] Switch the deployment from a cron job to `serve` (see `DEPLOYMENT.md`) —
      or keep the cron job for now; `once` still works.
- [ ] In Gloo: Settings → Notifications → Notification service → URL, secret,
      *Save and Register*. Check the status card says a token was issued.
- [ ] `curl -H "Authorization: Bearer $RELAY_SHARED_SECRET" https://…/v1/diagnostics`
      and confirm your phone is listed with both tokens.
- [ ] Delete `device_tokens.txt`, `NSNotifier-Persistent.json` and
      `write-JSON.php` from the web server.
