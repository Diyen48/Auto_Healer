# 🛡️ Sentinel — Event-Driven Auto-Remediation Pipeline

An autonomous SRE agent that detects server crashes, analyses root causes,
validates fixes in Docker sandboxes, and submits GitHub Pull Requests —
without ever touching production code directly.

---

## Architecture

```
┌──────────────┐    POST /webhook/crash    ┌─────────────────┐
│  Buggy       │ ────────────────────────► │  FastAPI         │
│  Service     │                           │  Ingestion API   │
└──────────────┘                           └────────┬────────┘
                                                    │ XADD
                                                    ▼
                                           ┌─────────────────┐
                                           │  Redis Stream    │
                                           │  sentinel:crashes│
                                           └────────┬────────┘
                                                    │ XREADGROUP
                                                    ▼
                                           ┌─────────────────┐
                                           │  Async Worker    │
                                           │  (Consumer)      │
                                           └────────┬────────┘
                                                    │
                              ┌─────────────────────┼──────────────────────┐
                              ▼                     ▼                      ▼
                     ┌────────────────┐   ┌─────────────────┐   ┌──────────────────┐
                     │  SRE Agent     │   │  Docker Sandbox  │   │  GitHub PR       │
                     │  (Analysis)    │──►│  (Validation)    │──►│  (Safe Delivery) │
                     └────────────────┘   └─────────────────┘   └──────────────────┘
```

## Quick Start

### 1. Prerequisites

- **Python 3.13+**
- **Docker Desktop** (for sandbox validation)
- **Redis** (run via Docker: `docker run -d -p 6379:6379 redis:7-alpine`)
- **GitHub PAT** with `repo` scope ([Generate here](https://github.com/settings/tokens))

### 2. Install Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e ".[dev]"
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your GitHub token, repo, etc.
```

### 4. Start the Pipeline

**Option A — Local development (API + Worker together):**

```bash
python main.py
```

**Option B — Docker Compose (full stack):**

```bash
docker compose -f docker/docker-compose.yml up --build
```

**Option C — Components separately:**

```bash
# Terminal 1: Redis
docker run -d -p 6379:6379 redis:7-alpine

# Terminal 2: API
uvicorn sentinel.api:app --port 8000

# Terminal 3: Worker
python -m sentinel.run_worker
```

### 5. Trigger a Crash

```bash
python buggy_app.py
```

Or send a manual webhook:

```bash
curl -X POST http://localhost:8000/webhook/crash \
  -H "Content-Type: application/json" \
  -d '{"error": "ZeroDivisionError: division by zero", "file": "buggy_app.py", "service": "data-processor"}'
```

### 6. Monitor

- **Health check:** `GET http://localhost:8000/health`
- **Event history:** `GET http://localhost:8000/status`
- **Check GitHub** for the auto-generated Pull Request

---

## Project Structure

```
Auto_Healer/
├── sentinel/                    # Main package
│   ├── __init__.py              # Package metadata
│   ├── api.py                   # FastAPI routes (webhook → Redis)
│   ├── config.py                # Settings from .env
│   ├── models.py                # Pydantic models
│   ├── worker.py                # Async Redis Stream consumer
│   ├── agent.py                 # SRE Agent (LLM + fallback)
│   ├── sandbox.py               # Docker sandbox manager
│   ├── github_pr.py             # GitHub PR automation
│   └── run_worker.py            # Standalone worker entrypoint
├── docker/
│   ├── Dockerfile.app           # App container for Compose
│   ├── Dockerfile.sandbox       # Sandbox container for testing
│   └── docker-compose.yml       # Full stack orchestration
├── buggy_app.py                 # Demo crashing service
├── main.py                      # Combined API + Worker entrypoint
├── .env.example                 # Environment template
├── pyproject.toml               # Dependencies
└── README.md                    # This file
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Redis Streams** over Pub/Sub | Durable, supports consumer groups, message acknowledgement |
| **asyncio** over Celery | Lower complexity, native Python, sufficient for current scale |
| **Docker sandbox** | Isolated validation without touching production environment |
| **GitHub PRs** over direct fixes | Human-in-the-loop control, CI/CD integration, audit trail |
| **Fallback analysis** | Pipeline degrades gracefully when LLM is unavailable |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `GITHUB_TOKEN` | _(required)_ | GitHub PAT with `repo` scope |
| `GITHUB_REPO` | _(required)_ | Target repo as `owner/name` |
| `SANDBOX_IMAGE` | `sentinel-sandbox:latest` | Docker image for sandbox |
| `SANDBOX_TIMEOUT_SECONDS` | `60` | Max sandbox run time |
| `MAX_FIX_ATTEMPTS` | `3` | Retry limit per crash event |

---

## License

MIT
