# Pins Python in a second place (see .python-version and pyproject.toml).
# 3.12 rather than latest, deliberately: the Render build failed on 3.14
# because pydantic-core shipped no wheel for it and fell back to compiling
# Rust against a read-only cargo cache.
FROM python:3.12-slim

# Keeps the image slim and the logs unbuffered, so `docker logs` shows the
# structured request lines as they happen rather than when a buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, as their own layer: application code changes far more
# often than requirements.txt, and this way a code edit doesn't reinstall
# httpx and pydantic every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Not root. Nothing here needs it, and the process holds a LinkedIn session
# cookie in memory.
RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

EXPOSE 8000

# ONE worker, deliberately - this is not a default worth "improving".
# Each worker gets its own pooled httpx client and therefore its own TLS
# connections, and LinkedIn revokes a replayed session after only a handful
# of new connections (measured at about three). Multiple workers reinstate a
# bug that killed sessions after roughly three requests. If you need
# throughput here, cache harder rather than adding workers.
#
# Shell form so ${PORT} expands at runtime. PaaS hosts (Render, Fly, Cloud
# Run) inject the port to listen on and fail the health check if the process
# binds a different one; 8000 is only the local default.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
