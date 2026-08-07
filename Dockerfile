# A glucose notifier is a thing you want to be boring, so: a pinned slim base,
# no build toolchain in the final image, a non-root user, and a health check the
# platform can actually act on.
FROM python:3.12-slim

# Unbuffered so logs appear in a platform's log viewer as they happen rather
# than when the buffer fills — which, for a process that writes a line every
# five minutes, can be hours.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/nsnotifier.sqlite3

WORKDIR /app

# Requirements first, so a code change does not re-resolve dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY nsnotifier ./nsnotifier
COPY script.py ./

# The database has to outlive the container. Without a volume mounted here the
# service still runs, but every restart forgets which devices exist and when
# each alert last fired — so a redeploy during a hypo re-announces it.
VOLUME ["/data"]

RUN useradd --create-home --uid 10001 nsnotifier \
    && mkdir -p /data \
    && chown -R nsnotifier:nsnotifier /data /app
USER nsnotifier

EXPOSE 8080

# Reports degraded — not merely alive — when the last tick failed, so a platform
# restarts a service that is up but has not managed to read Nightscout.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/health', timeout=5).status == 200 else 1)"

# `serve` runs the HTTP API and the polling loop in one process. Override with
# `once` for a cron-style deployment, or `api` when something external owns the
# schedule and calls POST /v1/tick.
CMD ["python", "-m", "nsnotifier", "serve"]
