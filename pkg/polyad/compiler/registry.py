"""
Public, immutable catalog of supported Kubernetes ASTs and their scheduling capabilities.

Descriptors are declared once on their AST classes. All collections in this module
are projections of those descriptors; querying the catalog does not register new
CRDs, install optional APIs or enable arbitrary Kubernetes kinds.
"""

from __future__ import annotations

from polyad.compiler.asts.common import ResourceType as ResourceType
from polyad.compiler.asts.resources import AUXILIARY_KINDS as AUXILIARY_KINDS
from polyad.compiler.asts.resources import BOUNDARY_KINDS as BOUNDARY_KINDS
from polyad.compiler.asts.resources import CAPACITY_KINDS as CAPACITY_KINDS
from polyad.compiler.asts.resources import COMPOSABLE_KINDS as COMPOSABLE_KINDS
from polyad.compiler.asts.resources import DEFINITION_KINDS as DEFINITION_KINDS
from polyad.compiler.asts.resources import GRAPH_OWNED_KINDS as GRAPH_OWNED_KINDS
from polyad.compiler.asts.resources import NETWORK_POLICY_KINDS as NETWORK_POLICY_KINDS
from polyad.compiler.asts.resources import POLYAD_KINDS as POLYAD_KINDS
from polyad.compiler.asts.resources import RECONCILED_KINDS as RECONCILED_KINDS
from polyad.compiler.asts.resources import RESOURCE_REGISTRY
from polyad.compiler.asts.resources import RESOURCE_TYPES as RESOURCE_TYPES

RESOURCE_MODELS = RESOURCE_REGISTRY
