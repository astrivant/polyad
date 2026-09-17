"""
Access Polyad without installing its operator or Kubernetes dependencies.
"""

from __future__ import annotations

from polyad_client.client import APIError as APIError
from polyad_client.client import Client as Client
from polyad_client.filters import Filter as Filter
from polyad_client.subscriptions import StreamInterrupted as StreamInterrupted
from polyad_client.subscriptions import Subscription as Subscription
from polyad_types.events import Event as Event
