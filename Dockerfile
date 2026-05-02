FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml README.md VERSION uv.lock ./
COPY packages/s3_archiver_core/pyproject.toml packages/s3_archiver_core/pyproject.toml
COPY packages/s3_archiver_core/README.md packages/s3_archiver_core/README.md
COPY packages/s3_archiver_cli/pyproject.toml packages/s3_archiver_cli/pyproject.toml
COPY packages/s3_archiver_cli/README.md packages/s3_archiver_cli/README.md
COPY packages/s3_archiver_core/src packages/s3_archiver_core/src
COPY packages/s3_archiver_cli/src packages/s3_archiver_cli/src
RUN mkdir -p /dist \
    && uv build --package s3-archiver-core --wheel --out-dir /dist \
    && uv build --package s3-archiver-cli --wheel --out-dir /dist

FROM builder AS dev
COPY . .
RUN uv sync --frozen --all-packages --all-groups

FROM python:3.12-slim AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
ENV PATH="/opt/venv/bin:${PATH}" \
    PIP_NO_CACHE_DIR=1
ENV LOG_DIR=/var/log/s3-archiver
WORKDIR /app
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app \
    && python -m venv /opt/venv \
    && mkdir -p /var/log/s3-archiver
COPY --from=builder /dist /dist
RUN /opt/venv/bin/pip install /dist/*.whl \
    && rm -rf /dist \
    && chown -R app:app /app /var/log/s3-archiver
USER app:app
CMD ["s3-archiver", "check"]
