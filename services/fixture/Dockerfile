# syntax=docker/dockerfile:1.14.0@sha256:4c68376a702446fc3c79af22de146a148bc3367e73c25a5803d453b6b3f722fb
ARG PYTHON_IMAGE=python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6
FROM ${PYTHON_IMAGE} AS build
ARG TARGETARCH
RUN python -m venv /opt/poetry && /opt/poetry/bin/pip install --no-cache-dir poetry==2.1.3
ENV POETRY_VIRTUALENVS_CREATE=false VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:${PATH}"
RUN python -m venv /opt/venv
COPY pkg/polyad-types /src/pkg/polyad-types
COPY pkg/client /src/pkg/client
COPY pkg/polyad-benchmarks /src/pkg/polyad-benchmarks
WORKDIR /src/pkg/polyad-benchmarks
RUN --mount=type=cache,id=polyad-benchmarks-${TARGETARCH},target=/root/.cache/pypoetry,sharing=locked \
    /opt/poetry/bin/poetry check --lock && /opt/poetry/bin/poetry sync --only main && python -m pip check

FROM ${PYTHON_IMAGE}
ARG TINI_VERSION=0.19.0-1+b3
RUN apt-get update && apt-get install --yes --no-install-recommends "tini=${TINI_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:${PATH}"
COPY --from=build /opt/venv /opt/venv
# Poetry installs the root in editable mode; retain only this lightweight package's sources.
COPY --from=build /src/pkg /src/pkg
USER 65532:65532
WORKDIR /tmp
STOPSIGNAL SIGTERM
LABEL org.opencontainers.image.source="https://github.com/astrivant/polyad" \
      org.opencontainers.image.licenses="GPL-3.0-only"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "polyad_benchmarks.fixture"]
CMD []
