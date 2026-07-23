"""
Pydantic models shared across the pipeline.

CrashAlert   — inbound webhook payload from a crashing service.
CrashEvent   — enriched internal representation with metadata.
RemediationResult — outcome of the full remediation cycle.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Inbound Webhook ─────────────────────────────────────────────────────

class CrashAlert(BaseModel):
    """Payload sent by a crashing service to POST /webhook/crash."""

    error: str = Field(..., description="Error message or exception string")
    file: str = Field(..., description="Source file where the crash occurred")
    traceback: Optional[str] = Field(
        None, description="Full stack trace, if available"
    )
    service: str = Field(
        "unknown", description="Name of the originating service"
    )
    # Multi-Tenant fields (optional per-request credentials)
    project_id: Optional[str] = Field(None, description="Multi-Tenant Project / Client ID")
    github_repo: Optional[str] = Field(None, description="Target GitHub Repository (owner/repo)")
    github_token: Optional[str] = Field(None, description="Target GitHub PAT / Installation Token")
    github_app_id: Optional[str] = Field(None, description="GitHub App ID")
    github_installation_id: Optional[str] = Field(None, description="GitHub App Installation ID")


# ── Internal Event ──────────────────────────────────────────────────────

class CrashEvent(BaseModel):
    """
    Enriched event placed on the Redis stream.

    Adds a unique id, timestamp, and severity so downstream consumers
    have full context without querying back.
    """

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: str
    file: str
    traceback: Optional[str] = None
    service: str = "unknown"
    severity: str = "critical"

    # Multi-Tenant overrides
    project_id: Optional[str] = None
    github_repo: Optional[str] = None
    github_token: Optional[str] = None
    github_app_id: Optional[str] = None
    github_installation_id: Optional[str] = None

    @classmethod
    def from_alert(cls, alert: CrashAlert) -> "CrashEvent":
        """Create a CrashEvent from an inbound CrashAlert."""
        return cls(
            error=alert.error,
            file=alert.file,
            traceback=alert.traceback,
            service=alert.service,
            project_id=alert.project_id,
            github_repo=alert.github_repo,
            github_token=alert.github_token,
            github_app_id=alert.github_app_id,
            github_installation_id=alert.github_installation_id,
        )


# ── Remediation Outcome ────────────────────────────────────────────────

class RemediationStatus(str, Enum):
    PENDING = "pending"
    ANALYZING = "analyzing"
    FIX_PROPOSED = "fix_proposed"
    SANDBOX_PASS = "sandbox_pass"
    SANDBOX_FAIL = "sandbox_fail"
    PR_CREATED = "pr_created"
    FAILED = "failed"


class RemediationResult(BaseModel):
    """Tracks the outcome of a single remediation attempt."""

    event_id: str
    status: RemediationStatus = RemediationStatus.PENDING
    root_cause: Optional[str] = None
    patched_code: Optional[str] = None
    patched_files: Optional[dict[str, str]] = None
    pr_url: Optional[str] = None
    sandbox_output: Optional[str] = None
    attempts: int = 0
    error_detail: Optional[str] = None
    completed_at: Optional[str] = None
