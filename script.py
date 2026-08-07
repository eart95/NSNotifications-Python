#!/usr/bin/env python3
"""Compatibility shim for the old scheduled job.

The service lives in the ``nsnotifier`` package now; this file exists so an
existing cron entry that runs ``python script.py`` keeps working through the
change rather than going quiet on the day of the deploy — which, for a glucose
notifier, is the single worst way to be upgraded.

It runs exactly one tick and exits, which is what the old script did. See
``docs/DEPLOYMENT.md`` for why a long-running ``serve`` process is a better
answer than a cron job, and ``docs/MIGRATION.md`` for what changed underneath.
"""

import sys

from nsnotifier.__main__ import main

if __name__ == "__main__":
    print(
        "script.py is a shim: running `python -m nsnotifier once`. "
        "Update your scheduled command when convenient.",
        file=sys.stderr,
    )
    raise SystemExit(main(["once"]))
