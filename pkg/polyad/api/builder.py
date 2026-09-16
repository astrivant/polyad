"""
Compose authenticated API services with an immutable fluent builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from attrs import evolve, field, frozen

from polyad.api.app import _build_app
from polyad.api.limits import RateLimitPolicy
from polyad_types.requests import ActivationRequest, CompositionRequest

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
        rate_limits (RateLimitPolicy | None): Optional shared shard intake policy.
        activate (Callable[[ActivationRequest], dict[str, Any]] | None): Durable pulse submission.
        activation_lookup (Callable[[str], dict[str, Any] | None] | None): Pulse observations.
        activation_stop (Callable[[str], dict[str, Any] | None] | None): Pulse stop signal.
    """

    submit: Callable[[CompositionRequest], dict[str, Any]] | None = None
    lookup: Callable[[str, bool], dict[str, Any] | None] | None = None
    token: str = field(default="", repr=False)
    title: str = "Polyad Composition API"
    version: str = "v1alpha1"
    rate_limits: RateLimitPolicy | None = None
    activate: Callable[[ActivationRequest], dict[str, Any]] | None = None
    activation_lookup: Callable[[str], dict[str, Any] | None] | None = None
    activation_stop: Callable[[str], dict[str, Any] | None] | None = None

    def with_activation_handlers(
        self,
        submit: Callable[[ActivationRequest], dict[str, Any]],
        lookup: Callable[[str], dict[str, Any] | None],
        stop: Callable[[str], dict[str, Any] | None],
    ) -> Self:
        """
        Configure durable pulse intake and observation callbacks.

        Args:
            submit (Callable[[ActivationRequest], dict[str, Any]]): Pulse receipt submission.
            lookup (Callable[[str], dict[str, Any] | None]): Pulse status lookup.
            stop (Callable[[str], dict[str, Any] | None]): Durable stop request.

        Returns:
            Self: A builder with activation support.
        """
        return evolve(self, activate=submit, activation_lookup=lookup, activation_stop=stop)

    def with_rate_limits(self, policy: RateLimitPolicy) -> Self:
        """
        Apply shared shard quotas when constructing the service.

        Args:
            policy (RateLimitPolicy): Namespace and Redis-backed request budget.

        Returns:
            Self: Builder containing the rate-limit policy.
        """
        return evolve(self, rate_limits=policy)

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
        return _build_app(
            self.submit,
            self.lookup,
            token=self.token,
            title=self.title,
            version=self.version,
            rate_limits=self.rate_limits,
            activate=self.activate,
            activation_lookup=self.activation_lookup,
            activation_stop=self.activation_stop,
        )
