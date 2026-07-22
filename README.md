# 🛡️ Sentinel — Event-Driven Auto-Remediation Pipeline

An autonomous SRE agent that detects server crashes, analyses root causes,
validates fixes in Docker sandboxes, and submits GitHub Pull Requests —
without ever touching production code directly.

---

## Architecture

## Architecture

```
┌─────────────────┐       writes to      ┌────────────────┐
│ buggy_multi_app │ ───────────────────► │   server.log   │
└─────────────────┘                      └───────┬────────┘
                                                 │ tail -f
                                                 ▼
                                         ┌────────────────┐
                                         │ log_monitor.py │
                                         └───────┬────────┘
                                                 │ POST /webhook/crash
                                                 ▼
                                         ┌────────────────┐
                                         │  FastAPI API   │
                                         └───────┬────────┘
                                                 │ XADD
                                                 ▼
                                         ┌────────────────┐
                                         │  Redis Stream  │
                                         └───────┬────────┘
                                                 │ XREADGROUP
                                                 ▼
                                         ┌────────────────┐
                                         │  Async Worker  │
                                         └───────┬────────┘
                                                 │
                                ┌────────────────┼────────────────┐
                                ▼                ▼                ▼
                       ┌────────────────┐ ┌──────────────┐ ┌──────────────┐
                       │   SRE Agent    │ │Docker Sandbox│ │  GitHub PR   │
                       │ (Multi-File)   │ │ (Validation) │ │ (Safe Fix)   │
                       └────────────────┘ └──────────────┘ └──────────────┘
```

## Quick Start

### 1. Start Redis
```bash
docker run -d -p 6379:6379 redis:7-alpine
```

### 2. Start Sentinel Pipeline
```bash
uv run python main.py
```

### 3. Start Log Monitor (Terminal 2)
```bash
uv run python log_monitor.py
```

### 4. Trigger Multi-File App Crash (Terminal 3)
```bash
uv run python buggy_multi_app/main.py
```

---

## Project Structure

```
Auto_Healer/
├── sentinel/                    # Main Sentinel SRE package
│   ├── api.py                   # FastAPI routes (webhook → Redis Stream)
│   ├── config.py                # Pydantic settings from .env
│   ├── models.py                # Shared Pydantic data models
│   ├── worker.py                # Async Redis Stream consumer
│   ├── agent.py                 # Multi-file SRE Agent (LLM diagnosis)
│   ├── sandbox.py               # Docker sandbox manager
│   ├── github_pr.py             # Multi-file GitHub PR automation
│   └── run_worker.py            # Worker entrypoint
├── buggy_multi_app/             # Interdependent multi-module application
│   ├── config.py                # Regional tax rates & helper logic
│   ├── calculator.py            # Order calculation module
│   └── main.py                  # Entrypoint (logs crash to server.log)
├── log_monitor.py               # Background log tailer & webhook publisher
├── main.py                      # Combined API + Worker entrypoint
├── test_multi_file_remediation.py # Automated multi-file pipeline test
├── pyproject.toml               # Package dependencies
└── README.md                    # Documentation
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
