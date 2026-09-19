"""
Run repeatable load studies against Polyad's real composition and activation APIs.

Render and submit plans, serve a mock application, generate bounded arrivals and
measure activation acceptance and completion. Shared run IDs connect results to
logs and traces; study refresh commands capture and verify experiment artifacts.
"""

from __future__ import annotations
