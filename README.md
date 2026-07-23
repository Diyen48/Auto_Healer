# 🛡️ Sentinel — Event-Driven Auto-Remediation Pipeline

An autonomous SRE agent that detects server crashes, analyses root causes,
validates fixes in Docker sandboxes, and submits GitHub Pull Requests —
without ever touching production code directly.

---

## Architecture

## Architecture

```
┌─────────────────┐       writes to      ┌────────────────┐
│   billing_app   │ ───────────────────► │   server.log   │
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

## Universal Plugin Setup (Plug into Any Project)

To connect Sentinel to **any external application repository** and log file:

```bash
uv run python setup_plugin.py
```

The interactive wizard prompts you for:
1. **GitHub Repository URL / Name** (e.g. `owner/my-app`)
2. **Log File Path** (e.g. `/var/log/server.log` or `server.log`)
3. **GitHub Access Token & Groq Key**

Sentinel auto-verifies repository connectivity and prepares the pipeline to monitor your application logs and automatically submit 1-click mergeable PRs on crash!

---

## Quick Start

### 1. Start Redis & Sentinel Stack (Terminal 1)
```bash
docker compose -f docker/docker-compose.yml up -d --build
```

### 2. Start Log Monitor (Terminal 2)
```bash
uv run python log_monitor.py
```

### 3. Launch Interactive Billing Web App (Terminal 3)
```bash
uv run python -m billing_app.server
```
Open **`http://localhost:5000`** in your web browser. Submit order calculations or click **"Trigger KeyError Edge-Case Crash"** buttons to watch real-time server exception telemetry and automated GitHub PR creation!

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
├── billing_app/                 # Interactive web application & billing services
│   ├── services/                # Interdependent modules (currency.py, checkout.py)
│   ├── static/                  # Glassmorphism dark-mode UI (index.html, style.css)
│   └── server.py                # FastAPI web app on port 5000 (logs to server.log)
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

## Production Deployment & Log Mounting Architectures

Sentinel supports 4 production log tailing and telemetry ingestion patterns:

### 1. Docker / Docker Compose Production Mount
Mount your production application's log directory into `sentinel-log-monitor` as a **Read-Only (`:ro`) volume**:

```yaml
services:
  production-api:
    image: mycompany/api:v1.0.0
    volumes:
      - app-logs:/var/log/app

  sentinel-log-monitor:
    image: sentinel-log-monitor:latest
    environment:
      - LOG_FILE_PATH=/var/log/app/server.log
      - SENTINEL_WEBHOOK=http://sentinel-api:8000/webhook/crash
    volumes:
      - app-logs:/var/log/app:ro # Read-only mount (100% safe)

volumes:
  app-logs:
```

### 2. Kubernetes (K8s) Sidecar Pattern
Deploy `sentinel-log-monitor` as a **Sidecar Container** inside your application Pod sharing an `emptyDir` log volume:
- Main container writes logs to `/var/log/app/server.log`.
- Sentinel Sidecar container tails `/var/log/app/server.log` and fires alerts to `sentinel-api`.

### 3. Linux EC2 Systemd Daemon
Run `log_monitor.py` as a background `systemd` daemon tailing `/var/log/nginx/error.log` or `/var/log/syslog`.

---

## Why GitHub App (Approach C) is Essential for Production Plugins

| Feature | Personal Access Token (PAT) ❌ | GitHub App (Approach C) ✅ |
|:---|:---|:---|
| **User Secret Security** | High risk — Users give you raw personal tokens. | **Zero Risk** — Users never share passwords or PATs. |
| **Credential Lifetime** | Static string; breaks if user leaves org. | **Ephemeral 1-hour tokens** issued dynamically. |
| **Scope Control** | PAT grants broad access to all repos. | Restricted **strictly to installed repositories**. |
| **Audit & Visibility** | PRs appear as posted by personal account. | PRs appear as **`bot [sentinel-auto-healer]`** with verified badge. |
| **Client Onboarding** | Manual token creation & copying. | **1-click install link** in 5 seconds. |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `GITHUB_TOKEN` | _(optional)_ | GitHub PAT (for PAT mode) |
| `GITHUB_REPO` | _(required)_ | Target repo as `owner/name` |
| `GITHUB_APP_ID` | _(optional)_ | GitHub App ID (for SaaS App mode) |
| `GITHUB_PRIVATE_KEY` | _(optional)_ | GitHub App RSA Private Key PEM string |
| `SANDBOX_IMAGE` | `sentinel-sandbox:latest` | Docker image for sandbox |
| `SANDBOX_TIMEOUT_SECONDS` | `60` | Max sandbox run time |
| `MAX_FIX_ATTEMPTS` | `3` | Retry limit per crash event |

---

## License

MIT
