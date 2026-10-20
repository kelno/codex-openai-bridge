# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

ARG CODEX_UID=1000
ARG CODEX_GID=1000

# Install system dependencies and Codex CLI runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg git build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @openai/codex \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Create application user
RUN groupadd --gid "${CODEX_GID}" codex \
    && useradd --uid "${CODEX_UID}" --gid "${CODEX_GID}" --create-home codex

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# Copy the rest of the project source
COPY . /app

# Prepare writable locations for Codex CLI
RUN mkdir -p /workspace \
    && chown -R codex:codex /workspace /home/codex

ENV CODEX_PATH=codex \
    CODEX_WORKDIR=/workspace \
    CODEX_HOME=/home/codex/.codex

USER codex

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
