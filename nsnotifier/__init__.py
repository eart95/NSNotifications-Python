"""Gloo's notification service.

Watches a Nightscout site and pushes to registered iPhones: visible glucose
alerts, silent sync nudges, and Live Activities that start, update and end
themselves while the app is not running.

The decision rules — ``episodes`` and ``alerts`` — mirror pure Swift code in
MDIKit, and the wire formats in ``models`` and ``activity`` are that app's
types. Read those Swift files alongside these; a change to one belongs in both.
"""

__version__ = "2.0.0"
