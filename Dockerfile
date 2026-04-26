FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git curl ca-certificates \
    patch diffutils \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv globally
RUN curl -Lsf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:/root/.cargo/bin:$PATH"

# Pre-install common Python tools into a shared venv at /opt/venv
# Projects can use this venv or create their own with uv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx aiofiles \
    pytest pytest-asyncio \
    requests pyyaml toml

# aider — heavy dep, pre-baked so the model never has to wait for it
RUN pip install --no-cache-dir aider-chat

WORKDIR /workspace
