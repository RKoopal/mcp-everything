# The connector server. Build and start: docker compose up -d --build (see README, "Quick start").
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY connector ./connector
COPY sites ./sites
COPY extension ./extension

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin connector \
    && mkdir /data && chown connector /data
USER connector
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DATA_DIR=/data HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "connector.server"]
