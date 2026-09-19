# syntax=docker/dockerfile:1.14.0@sha256:4c68376a702446fc3c79af22de146a148bc3367e73c25a5803d453b6b3f722fb
ARG PYTHON_IMAGE=python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6

FROM ${PYTHON_IMAGE} AS base
ARG TARGETARCH
ARG TARGETVARIANT
ARG TINI_VERSION=0.19.0-1+b3
LABEL org.opencontainers.image.title="Polyad" \
      org.opencontainers.image.description="Kubernetes operator for graph-based applications across clusters, with Cheeger constraints, demand-driven topology and traffic adaptation, and coordinated autoscaling" \
      org.opencontainers.image.authors="Emma Doyle" \
      org.opencontainers.image.vendor="Astrivant" \
      org.opencontainers.image.url="https://github.com/astrivant/polyad" \
      org.opencontainers.image.documentation="https://github.com/astrivant/polyad/tree/main/docs" \
      org.opencontainers.image.source="https://github.com/astrivant/polyad" \
      org.opencontainers.image.licenses="GPL-3.0-only"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    HOME=/home/polyad \
    PATH="/opt/venv/bin:${PATH}"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends "tini=${TINI_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 65532 polyad \
    && useradd --no-log-init --uid 65532 --gid 65532 --create-home --home-dir /home/polyad --shell /bin/sh polyad
WORKDIR /app
EXPOSE 8080 8090 8091 8092
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "polyad.operator.lifecycle.probes"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "polyad.operator.runtime"]
CMD []

FROM base AS build-tools
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR=/var/cache/pypoetry \
    HOME=/root
COPY .tool-versions ./
COPY scripts/tooling/tool-version.sh ./scripts/tooling/tool-version.sh
# All stages run for the target platform so native Python extensions match it.
RUN --mount=type=cache,id=polyad-pip-${TARGETARCH}${TARGETVARIANT},target=/root/.cache/pip,sharing=locked \
    python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install "poetry==$(bash scripts/tooling/tool-version.sh poetry)" \
    && python -m venv /opt/venv
COPY pyproject.toml poetry.lock README.md LICENSE ./
COPY pkg/polyad-types ./pkg/polyad-types
COPY pkg/polyad-schemas ./pkg/polyad-schemas
RUN /opt/poetry/bin/poetry check --lock

FROM build-tools AS production-build
RUN --mount=type=cache,id=polyad-poetry-${TARGETARCH}${TARGETVARIANT},target=/var/cache/pypoetry,sharing=locked \
    /opt/poetry/bin/poetry sync --only main --no-root --all-extras
COPY pkg ./pkg
RUN /opt/poetry/bin/poetry build --format wheel \
    && python -m pip install --no-cache-dir --no-deps dist/*.whl \
    && python -m pip check

FROM build-tools AS development
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ENV PATH="/opt/venv/bin:/opt/poetry/bin:${PATH}" \
    POLYAD_LOG_LEVEL=DEBUG
RUN apt-get update \
    && apt-get install --yes --no-install-recommends "git=${GIT_VERSION}" "git-man=${GIT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,id=polyad-poetry-${TARGETARCH}${TARGETVARIANT},target=/var/cache/pypoetry,sharing=locked \
    /opt/poetry/bin/poetry sync --with dev --no-root --all-extras
COPY --chown=65532:65532 . .
RUN /opt/poetry/bin/poetry install --only-root --all-extras \
    && python -m pip check \
    && chown -R 65532:65532 /app /opt/venv
ENV POLYAD_CRD_DIRECTORY=/app/charts/polyad-crds/crds \
    POETRY_CACHE_DIR=/home/polyad/.cache/pypoetry \
    HOME=/home/polyad
USER 65532:65532
ARG VERSION
ARG VCS_REF
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      com.astrivant.polyad.profile="development"

# Keep production last: a build without --target creates the deployment image.
FROM base AS production
COPY --from=production-build /opt/venv /opt/venv
COPY charts/polyad-crds/crds /opt/polyad/crds
USER 65532:65532
ARG VERSION
ARG VCS_REF
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      com.astrivant.polyad.profile="production"
