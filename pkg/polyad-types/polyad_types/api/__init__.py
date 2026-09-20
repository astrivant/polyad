"""
Share authentication, discovery, request and demand reporting contracts.
"""

from __future__ import annotations

from polyad_types.api.adaptation import AdaptationReport as AdaptationReport
from polyad_types.api.auth import APIKey as APIKey
from polyad_types.api.auth import Authentication as Authentication
from polyad_types.api.auth import CredentialAssignment as CredentialAssignment
from polyad_types.api.auth import GraphAccess as GraphAccess
from polyad_types.api.auth import KeyDirection as KeyDirection
from polyad_types.api.discovery import AccessMode as AccessMode
from polyad_types.api.discovery import AtlasAccess as AtlasAccess
from polyad_types.api.discovery import ServiceAccess as ServiceAccess
from polyad_types.api.discovery import ServiceEndpoint as ServiceEndpoint
from polyad_types.api.requests import ActivationRequest as ActivationRequest
from polyad_types.api.requests import CompositionItem as CompositionItem
from polyad_types.api.requests import CompositionRequest as CompositionRequest
from polyad_types.api.requests import ConnectionRequest as ConnectionRequest
from polyad_types.api.requests import ConnectionResponse as ConnectionResponse
from polyad_types.api.requests import ServiceConnectionRequest as ServiceConnectionRequest
from polyad_types.api.service_level import AdaptationBudget as AdaptationBudget
from polyad_types.api.service_level import ServiceLevelPolicy as ServiceLevelPolicy
from polyad_types.api.service_level import ServiceLevelReport as ServiceLevelReport
from polyad_types.api.throughput import DemandSample as DemandSample
from polyad_types.api.throughput import DemandSource as DemandSource
from polyad_types.api.throughput import ThroughputSample as ThroughputSample
from polyad_types.api.throughput import TrafficSample as TrafficSample

__all__ = (
    "APIKey",
    "AccessMode",
    "ActivationRequest",
    "AdaptationBudget",
    "AdaptationReport",
    "AtlasAccess",
    "Authentication",
    "CompositionItem",
    "CompositionRequest",
    "ConnectionRequest",
    "ConnectionResponse",
    "CredentialAssignment",
    "DemandSample",
    "DemandSource",
    "GraphAccess",
    "KeyDirection",
    "ServiceAccess",
    "ServiceConnectionRequest",
    "ServiceEndpoint",
    "ServiceLevelPolicy",
    "ServiceLevelReport",
    "ThroughputSample",
    "TrafficSample",
)
