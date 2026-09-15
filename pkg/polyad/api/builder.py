"""
Compose authenticated API services with an immutable fluent builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from attrs import evolve, field, frozen

from polyad.api.app import _build_app
from polyad.compiler.composition import CompositionRequest

if TYPE_CHECKING:
    from typing import Self

    from flask import Flask


@frozen
class APIBuilder:
    """
    Configure a composition service before constructing its Flask application.

    Attributes:
        submit (Callable[[CompositionRequest], dict[str, Any]] | None): Durable receipt submission handler.
        lookup (Callable[[str, bool], dict[str, Any] | None] | None): Status and manifest audit handler.
        token (str): Namespace-scoped bearer credential, excluded from representations.
        title (str): Service title exposed by the schema endpoint.
        version (str): API contract version exposed by the schema endpoint.
    """

    submit: Callable[[CompositionRequest], dict[str, Any]] | None = None
    lookup: Callable[[str, bool], dict[str, Any] | None] | None = None
    token: str = field(default="", repr=False)
    title: str = "Polyad Composition API"
    version: str = "v1alpha1"

    def with_handlers(
        self, submit: Callable[[CompositionRequest], dict[str, Any]], lookup: Callable[[str, bool], dict[str, Any] | None]
    ) -> Self:
        """
        Supply storage or transport adapters without changing the original builder.

        Args:
            submit (Callable[[CompositionRequest], dict[str, Any]]): Durable receipt submission handler.
            lookup (Callable[[str, bool], dict[str, Any] | None]): Status and audit read handler.

        Returns:
            Self: Builder containing these handlers.
        """
        return evolve(self, submit=submit, lookup=lookup)

    def with_bearer_token(self, token: str) -> Self:
        """
        Configure the credential shared by submission and audit endpoints.

        Args:
            token (str): Namespace-scoped credential, normally provided by a Kubernetes Secret.

        Returns:
            Self: Builder containing the credential.
        """
        return evolve(self, token=token)

    def with_metadata(self, title: str, version: str) -> Self:
        """
        Describe this service in its OpenAPI schema.

        Args:
            title (str): Service title.
            version (str): API contract version.

        Returns:
            Self: Builder containing the document metadata.
        """
        return evolve(self, title=title, version=version)

    def build(self) -> Flask:
        """
        Validate required collaborators and create a fresh Flask service instance.

        Returns:
            Flask: Authenticated composition application ready for WSGI hosting.
        """
        if self.submit is None or self.lookup is None:
            raise ValueError("the composition API requires submit and lookup handlers")
        if not self.token:
            raise ValueError("the composition API requires a bearer token")
        if not self.title.strip() or not self.version.strip():
            raise ValueError("OpenAPI title and version must be nonempty")
        return _build_app(self.submit, self.lookup, token=self.token, title=self.title, version=self.version)
