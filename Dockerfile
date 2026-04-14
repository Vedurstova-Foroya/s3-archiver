FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml README.md VERSION ./
COPY packages/s3_archiver_core/pyproject.toml packages/s3_archiver_core/pyproject.toml
COPY packages/s3_archiver_cli/pyproject.toml packages/s3_archiver_cli/pyproject.toml
COPY packages/s3_archiver_core/src packages/s3_archiver_core/src
COPY packages/s3_archiver_cli/src packages/s3_archiver_cli/src
RUN uv sync --frozen --package s3-archiver-cli

FROM builder AS dev
COPY . .
RUN uv sync --frozen --all-packages --all-groups

FROM python:3.12-slim AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
ENV PATH="/app/.venv/bin:${PATH}"
ENV LOG_DIR=/var/log/s3-archiver
WORKDIR /app
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app
COPY --from=builder /app /app
RUN mkdir -p /var/log/s3-archiver && chown -R app:app /app /var/log/s3-archiver
USER app:app
CMD ["s3-archiver", "check", "--json"]
