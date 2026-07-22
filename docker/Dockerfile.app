# ── Sentinel Application Container ──────────────────────────────────
# Used by docker-compose for both the API and worker services.

FROM python:3.13-slim

LABEL maintainer="sentinel-pipeline"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies for Docker SDK
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    "redis[hiredis]" \
    docker \
    PyGithub \
    pydantic-settings \
    python-dotenv \
    groq \
    requests

# Copy application code
COPY sentinel/ ./sentinel/
COPY buggy_multi_app/ ./buggy_multi_app/
COPY main.py log_monitor.py ./

CMD ["uvicorn", "sentinel.api:app", "--host", "0.0.0.0", "--port", "8000"]
