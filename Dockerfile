# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6

FROM ${PYTHON_IMAGE} AS base
LABEL org.opencontainers.image.title="Polyad" \
      org.opencontainers.image.description="Graph-based workload scheduler for Kubernetes" \
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
RUN groupadd --gid 65532 polyad \
    && useradd --no-log-init --uid 65532 --gid 65532 --create-home --home-dir /home/polyad --shell /bin/sh polyad
WORKDIR /app
EXPOSE 8080 8090 8091 8092
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
ENTRYPOINT ["python", "-m", "polyad.operator.runtime"]
CMD ["--liveness=http://0.0.0.0:8080/healthz"]

FROM base AS build-tools
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_CACHE_DIR=/var/cache/pypoetry \
    HOME=/root
COPY .tool-versions ./
COPY scripts/tool-version.sh ./scripts/tool-version.sh
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install "poetry==$(bash scripts/tool-version.sh poetry)" \
    && python -m venv /opt/venv
COPY pyproject.toml poetry.lock ./
COPY pkg/polyad-types ./pkg/polyad-types
RUN /opt/poetry/bin/poetry lock

FROM build-tools AS production-build
RUN --mount=type=cache,target=/var/cache/pypoetry \
    /opt/poetry/bin/poetry sync --only main --no-root
COPY README.md LICENSE ./
COPY pkg ./pkg
RUN /opt/poetry/bin/poetry build --format wheel \
    && python -m pip install --no-cache-dir --no-deps dist/*.whl \
    && python -m pip check

FROM build-tools AS development
ENV PATH="/opt/venv/bin:/opt/poetry/bin:${PATH}" \
    POLYAD_LOG_LEVEL=DEBUG
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/var/cache/pypoetry \
    /opt/poetry/bin/poetry sync --with dev --no-root
COPY --chown=65532:65532 . .
RUN /opt/poetry/bin/poetry install --only-root \
    && python -m pip check \
    && chown -R 65532:65532 /app /opt/venv
ENV POLYAD_CRD_DIRECTORY=/app/charts/polyad/crds \
    POETRY_CACHE_DIR=/home/polyad/.cache/pypoetry \
    HOME=/home/polyad
USER 65532:65532
ARG VERSION
ARG VCS_REF
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      com.astrivant.polyad.profile="development"

# Keep production last: an ordinary docker build creates the deployment image.
FROM base AS production
COPY --from=production-build /opt/venv /opt/venv
COPY charts/polyad/crds /opt/polyad/crds
USER 65532:65532
ARG VERSION
ARG VCS_REF
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      com.astrivant.polyad.profile="production"
