ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3
ARG RUNTIME_IMAGE=gcr.io/distroless/base-debian12:nonroot@sha256:63f52bd27b6aa6555f5d56500b70d7bb0afe51c654905be88a2c1cf967a77b1a

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY packages/database/pyproject.toml packages/database/pyproject.toml
COPY packages/search-core/pyproject.toml packages/search-core/pyproject.toml
RUN uv sync --frozen --no-dev --package work-retrieval-api --no-install-workspace

COPY apps/api/src apps/api/src
COPY packages/database/src packages/database/src
COPY packages/search-core/src packages/search-core/src
RUN uv sync --frozen --no-dev --package work-retrieval-api --no-editable

FROM ${RUNTIME_IMAGE} AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY --from=builder /lib/x86_64-linux-gnu/libgcc_s.so.1 /lib/x86_64-linux-gnu/libgcc_s.so.1
COPY --from=builder /lib/x86_64-linux-gnu/libstdc++.so.6 /lib/x86_64-linux-gnu/libstdc++.so.6
COPY --from=builder /lib/x86_64-linux-gnu/libz.so.1 /lib/x86_64-linux-gnu/libz.so.1
COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "work_retrieval_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
