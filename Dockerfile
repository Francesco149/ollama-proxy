FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git curl ca-certificates \
    patch diffutils jq \
    build-essential sudo \
    && rm -rf /var/lib/apt/lists/*

# Install uv system-wide so any UID can run it
RUN curl -Lsf https://astral.sh/uv/install.sh | env HOME=/root sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv \
    && chmod 755 /usr/local/bin/uv

# Point uv cache/config to /tmp so any runtime UID can write without errors
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV UV_CONFIG_FILE=/tmp/uv-config.toml

# Pre-built venv at /opt/venv — world-writable so the host UID can pip install
RUN python3 -m venv /opt/venv \
    && chmod -R 777 /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/bin:$PATH"

RUN pip install --no-cache-dir \
    fastapi uvicorn httpx aiofiles \
    pytest pytest-asyncio \
    requests pyyaml toml

# Allow any runtime user passwordless sudo — intentional for local dev sandbox
RUN echo "ALL ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /workspace
