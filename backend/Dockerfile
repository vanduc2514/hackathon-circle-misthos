# The API and the sweeper: one image, two commands (see compose.yaml).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

WORKDIR /app

# Dependencies first, from the lock, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY alembic.ini ./
COPY src ./src
RUN uv sync --frozen

RUN useradd --create-home --uid 10001 misthos
USER misthos

EXPOSE 8000
# Behind the web app's proxy the client is in X-Forwarded-For, and nginx rewrites that
# header with the address it saw, so the caller cannot choose it. Trust is limited to
# the addresses a proxy can actually come from rather than `*`: loopback, and the
# private ranges Docker allocates networks from — 172.16/12 on Linux, and 192.168/16
# on Docker Desktop, where a compose network lands outside 172.16/12 (192.168.107.0/24
# on the machine this was measured on). Trusting only 172.16/12 left the header
# unread, which made every client share one rate-limit bucket.
CMD ["uvicorn", "misthos.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", \
     "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
