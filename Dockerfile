FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY pkg ./pkg
RUN pip install --no-cache-dir .
RUN useradd --uid 65532 --user-group --no-create-home polyad
USER 65532:65532
ENTRYPOINT ["python", "-m", "polyad.operator.runtime"]
CMD ["--liveness=http://0.0.0.0:8080/healthz"]
