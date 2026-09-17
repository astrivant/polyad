"""
Load versioned JSON Schemas without the operator, shared types or validator dependencies.
"""

from __future__ import annotations

from polyad_schemas._catalog import available_schemas as available_schemas
from polyad_schemas._catalog import load_schema as load_schema
from polyad_schemas.events import event_schema as event_schema
from polyad_schemas.helm import values_schema as values_schema
from polyad_schemas.models import schema_for as schema_for
from polyad_schemas.resources import resource_schema as resource_schema
