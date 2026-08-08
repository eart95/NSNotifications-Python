# Running this thing so it stays running

You asked for more stable ways to do what the cron job does. Here they are,
roughly in order of how much they improve things per unit of work.

First, though, it is worth being precise about **what is actually unreliable
about a five-minute cron job**, because "cron is flaky" is not really true and
the real problems are more specific:

1. **Cold start per run.** Every five minutes the platform starts a container,
   Python imports (the old script imported pandas — several seconds on its own),
   a TLS handshake to Nightscout, another to Apple, a JWT is signed, and then
   the process exits. Most of the wall clock is setup. It also means every
   single push pays a fresh connection to APNs, which is the opposite of what
   HTTP/2 multiplexing is for.
2. **A missed run is invisible.** The old script exited `0` whatever happened,
   so a Nightscout outage, a bad APNs key and a completely quiet night were the
   same event as far as any monitoring was concerned.
3. **Overlap.** If a run takes longer than the interval, most schedulers start
   the next one anyway. With state in a fetch-modify-upload JSON file, two
   overlapping runs silently lose one of their updates.
4. **Five minutes is a floor.** Nothing can react faster than the next tick,
   and most cron implementations do not go below one minute anyway. The service
   now ticks every **two** minutes by default, so a running Live Activity is
   never more than that behind; a five-minute cron cannot do that, and a
   two-minute one would be paying its cold start 720 times a day.
5. **Nothing can call you.** A cron job cannot serve the device-registration
   endpoint the app now needs. Something has to be listening.

Point 5 alone means a pure cron deployment is no longer sufficient by itself.

---

## Option A — one long-running process (recommended)

```
CMD ["python", "-m", "nsnotifier", "serve"]
```

`serve` runs the HTTP API and the polling loop in one process, one container,
one volume, no coordination. This is the default in the `Dockerfile` and it is
what you want unless you have a specific reason otherwise.

What it fixes: the cold start (imports and connections are paid once), the
overlap (there is one loop and it awaits its own tick), the reaction floor (the
interval is a variable, and jittered so a fleet does not stampede Nightscout on
the minute), and the registration endpoint.

What to configure:

* a **persistent volume** at `DATABASE_PATH`;
* the platform's **health check** pointed at `GET /v1/health` — it reports `503`
  when the last tick failed, not merely when the process is gone, so a service
  that is up but has not read Nightscout for an hour gets restarted;
* `HEARTBEAT_URL` (see below);
* the platform's own **restart policy** set to always. `SIGTERM` is handled, so
  a deploy is a clean stop rather than a `SIGKILL` mid-write.

This works on Northflank (Combined service, or a Deployment with a volume), Fly,
Railway, Render, Cloud Run with min-instances ≥ 1, or a `systemd` unit on any
VPS — see Option D.

**Cost**: one always-on container instead of 288 short ones a day. On most
platforms that is cheaper, not dearer.

## Option B — external scheduler, HTTP trigger

```
CMD ["python", "-m", "nsnotifier", "api"]
```

…and have something call `POST /v1/tick` with the bearer token. The process
stays up (so registration works and connections are warm) but *when* it runs is
someone else's problem.

Use this when you want the schedule owned by infrastructure that is more
observable than a loop inside your own process — a platform scheduler, an AWS
EventBridge rule, a GitHub Actions `schedule:` workflow, an
[Upstash QStash](https://upstash.com/) schedule, or a Cloudflare Worker cron
trigger doing nothing but a `fetch`.

The trigger is idempotent in the way that matters: cooldowns and episode state
are persisted, so ten calls in a minute produce at most the notifications one
call would have. That means a scheduler with at-least-once delivery is safe.

## Option C — event-driven, no polling at all

The most responsive option, and the one that removes the schedule entirely:
have **Nightscout tell you** when a reading arrives, instead of asking every
five minutes.

* Nightscout's `/api/v1/entries` uploads can be mirrored with its own
  **webhook/`WS` bridge**; the simplest robust version is to point a small
  websocket client at the site's socket.io feed and call `POST /v1/tick` on each
  `dataUpdate`.
* If your CGM uploader is under your control, have it call `/v1/tick` directly
  after each successful upload.

Latency drops from "up to five minutes" to "seconds", and the load on Nightscout
drops to almost nothing. Keep a slow poll (say every 15 minutes) underneath it
as a floor: an event-driven system that stops getting events looks exactly like
one where nothing is happening, which for glucose is precisely the state you
most need to notice. That floor is also what fires the "no data" alert.

## Option D — a VPS with systemd

If the platform *is* the unreliable part, this is the least moving parts of any
option:

```ini
# /etc/systemd/system/nsnotifier.service
[Unit]
Description=Gloo notification service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=nsnotifier
WorkingDirectory=/opt/nsnotifier
EnvironmentFile=/etc/nsnotifier.env
ExecStart=/opt/nsnotifier/.venv/bin/python -m nsnotifier serve
Restart=always
RestartSec=10
# Do not let a restart loop hammer Nightscout or Apple.
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
```

`Restart=always` with a burst limit gives you supervision without a platform,
and `systemd` will restart it after a reboot, an OOM, or a crash. Put Caddy in
front for TLS (two lines of config, automatic certificates).

Note the deliberate *absence* of a `systemd` timer here. A timer would be
Option A's loop with extra steps and would reintroduce the cold start.

---

## Whichever option you pick

### Set up a dead-man's switch

```
HEARTBEAT_URL=https://hc-ping.com/your-uuid
```

The service pings it after every **successful** tick — not on a failed one, so a
Nightscout outage shows up too. Configure the check to expect a ping every 2
minutes with a 10-minute grace, and to alert you by a channel that is not this
system.

This is the most important line in this document. Everything else here fails
loudly. A notifier that has *stopped* fails silently, and by then its user has
delegated noticing to it. A quiet night and a dead service are indistinguishable
from the inside; the absence of the pings is the only thing that tells them
apart.

### Keep the phone's own alerts on

Gloo still schedules its own local notifications, and its "no data" alert is
genuinely better than anything a server can do — it is re-armed by every reading
that arrives, so it fires precisely when readings stop, with no network involved
at all. The service is a large improvement on top of that, not a replacement for
it, and the two are deduplicated (see `MIGRATION.md` §9).

**And keep your CGM app's own alarms on.** Neither the app nor this service can
override the ringer switch — that needs Apple's Critical Alerts entitlement,
which Gloo does not have. Nothing in this repository is a hypo alarm.

### Watch what it actually did

```bash
curl -s -H "Authorization: Bearer $RELAY_SHARED_SECRET" \
     https://your-service/v1/diagnostics | python -m json.tool
```

Every push, its APNs status and Apple's reason, for the last seven days —
because "did it send anything last night" should not be answerable only by
hoping the logs have not rotated.
