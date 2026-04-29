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

# Install uv system-wide so any user can run it
RUN curl -Lsf https://astral.sh/uv/install.sh | env HOME=/root sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv \
    && chmod 755 /usr/local/bin/uv

# Pre-built venv at /opt/venv — owned by root but world-writable so the
# host-user (passed via --user at runtime) can install into it freely.
RUN python3 -m venv /opt/venv \
    && chmod -R 777 /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/bin:$PATH"

RUN pip install --no-cache-dir \
    fastapi uvicorn httpx aiofiles \
    pytest pytest-asyncio \
    requests pyyaml toml

# Allow the runtime user to run apt and sudo without a password.
# The actual UID is not known at build time (passed via --user), so we
# grant ALL users passwordless sudo. This is intentional for a local
# dev sandbox — do not use in production.
RUN echo "ALL ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /workspace
