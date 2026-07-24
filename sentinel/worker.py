"""
Async Redis Stream consumer — the worker layer of the Sentinel pipeline.

Pulls CrashEvent messages from the Redis Stream using a consumer group,
invokes the SRE agent to analyse and fix the crash, validates the fix
in a Docker sandbox, and submits a GitHub Pull Request if tests pass.

Features:
    - Consumer group for reliable delivery (messages are ACK'd after processing)
    - Exponential backoff on transient failures
    - Concurrent worker limit via asyncio.Semaphore
    - Graceful shutdown on cancellation
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from sentinel.config import get_settings
from sentinel.models import CrashEvent, RemediationResult, RemediationStatus
from sentinel.agent import run_sre_agent
from sentinel.sandbox import SandboxManager
from sentinel.github_pr import GitHubRemediator

logger = logging.getLogger("sentinel.worker")

# Concurrency guard — at most 3 events processed simultaneously
_semaphore = asyncio.Semaphore(3)


async def _process_event(event: CrashEvent) -> RemediationResult:
    """
    Full remediation pipeline for a single crash event.

    1. Invoke the SRE agent to analyse the crash and propose a fix.
    2. Spin up a Docker sandbox and validate the fix.
    3. If sandbox tests pass, submit a GitHub PR.
    4. If sandbox tests fail, retry up to max_fix_attempts.
    """
    settings = get_settings()
    result = RemediationResult(event_id=event.event_id)

    for attempt in range(1, settings.max_fix_attempts + 1):
        result.attempts = attempt
        logger.info(
            "🔬 [%s] Attempt %d/%d — invoking SRE agent …",
            event.event_id, attempt, settings.max_fix_attempts,
        )

        # Instantiate GitHubRemediator first to obtain gh_client and target repo
        gh = GitHubRemediator(
            token_override=event.github_token,
            repo_override=event.github_repo,
            app_id_override=event.github_app_id,
            installation_id_override=event.github_installation_id,
        )

        # ── Step 0: Open PR Deduplication Safeguard ─────────────────
        try:
            existing_pr_url = await asyncio.to_thread(
                gh.check_open_pr_exists, event.file, event.error
            )
            if existing_pr_url:
                logger.info(
                    "🛡️ [%s] Deduplication: An open PR already exists for %s (%s). Skipping duplicate branch creation.",
                    event.event_id, event.file, existing_pr_url
                )
                result.status = RemediationStatus.PR_CREATED
                result.pr_url = existing_pr_url
                return result
        except Exception as dedup_err:
            logger.warning("PR deduplication check error: %s", dedup_err)

        # ── Step 1: Agent analysis & Multi-Agent Verification ────────
        try:
            result.status = RemediationStatus.ANALYZING
            analysis = await run_sre_agent(event, gh_client=gh._gh, repo_name=gh.repo_name)
            result.root_cause = analysis.get("root_cause", "Unknown")
            result.patched_files = analysis.get("patched_files")
            result.patched_code = analysis.get("patched_code")

            if not result.patched_files and result.patched_code:
                result.patched_files = {event.file: result.patched_code}

            if not result.patched_files:
                result.status = RemediationStatus.FAILED
                result.error_detail = "Agent did not produce patched files."
                logger.error("❌ [%s] Agent returned no fix.", event.event_id)
                continue

            # Multi-Agent Critic / Verifier Review
            try:
                from sentinel.agent import run_critic_agent
                from sentinel.models import CriticReview
                critic_raw = await run_critic_agent(event, analysis)
                result.critic_review = CriticReview(**critic_raw)
                logger.info(
                    "🧐 [%s] Critic Audit: Risk=%s, Confidence=%.2f, Approved=%s",
                    event.event_id, result.critic_review.risk,
                    result.critic_review.confidence, result.critic_review.approved
                )
            except Exception as critic_err:
                logger.warning("Critic review failed, proceeding with default audit: %s", critic_err)

            result.status = RemediationStatus.FIX_PROPOSED
            logger.info("💡 [%s] Fix proposed for %d file(s). Root cause: %s",
                        event.event_id, len(result.patched_files), result.root_cause)
        except Exception as exc:
            result.status = RemediationStatus.FAILED
            result.error_detail = f"Agent error: {exc}"
            logger.exception("❌ [%s] Agent failure", event.event_id)
            continue

        # ── Step 2: Sandbox validation ──────────────────────────────
        try:
            sandbox = SandboxManager()
            sandbox_result = await sandbox.validate_fix(
                file_path=event.file,
                patched_code=result.patched_code,
                patched_files=result.patched_files,
            )

            result.sandbox_output = sandbox_result.get("output", "")

            if sandbox_result.get("passed"):
                result.status = RemediationStatus.SANDBOX_PASS
                logger.info("✅ [%s] Sandbox tests passed!", event.event_id)
            else:
                result.status = RemediationStatus.SANDBOX_FAIL
                logger.warning(
                    "⚠️  [%s] Sandbox tests failed (attempt %d). Output: %s",
                    event.event_id, attempt, result.sandbox_output,
                )
                continue  # retry with a new agent invocation
        except Exception as exc:
            result.status = RemediationStatus.SANDBOX_FAIL
            result.sandbox_output = str(exc)
            logger.exception("❌ [%s] Sandbox error", event.event_id)
            continue

        # ── Step 3: GitHub PR ───────────────────────────────────────
        try:
            gh = GitHubRemediator(
                token_override=event.github_token,
                repo_override=event.github_repo,
                app_id_override=event.github_app_id,
                installation_id_override=event.github_installation_id,
            )
            pr_url = await gh.create_remediation_pr(
                crash_event=event,
                patched_code=result.patched_code,
                patched_files=result.patched_files,
                root_cause=result.root_cause,
                sandbox_output=result.sandbox_output,
                critic_review=result.critic_review,
            )
            result.pr_url = pr_url
            result.status = RemediationStatus.PR_CREATED
            result.completed_at = datetime.now(timezone.utc).isoformat()
            logger.info("🎉 [%s] PR created: %s", event.event_id, pr_url)
            return result
        except Exception as exc:
            result.status = RemediationStatus.FAILED
            result.error_detail = f"GitHub PR error: {exc}"
            logger.exception("❌ [%s] PR creation failed", event.event_id)
            return result

    # Exhausted all attempts
    if result.status != RemediationStatus.PR_CREATED:
        result.status = RemediationStatus.FAILED
        result.error_detail = (
            f"Exhausted {settings.max_fix_attempts} fix attempts."
        )
        result.completed_at = datetime.now(timezone.utc).isoformat()
    return result


async def _handle_message(
    r: aioredis.Redis,
    stream: str,
    group: str,
    msg_id: str,
    fields: dict,
) -> None:
    """Deserialise, process, and ACK a single stream message."""
    async with _semaphore:
        try:
            raw = fields.get("data", "{}")
            event = CrashEvent.model_validate_json(raw)
            logger.info(
                "📨 [%s] Processing crash in %s …", event.event_id, event.file
            )

            result = await _process_event(event)

            logger.info(
                "📋 [%s] Result: status=%s pr=%s",
                event.event_id, result.status.value, result.pr_url or "N/A",
            )
        except Exception:
            logger.exception("❌ Failed to process message %s", msg_id)
        finally:
            # Always acknowledge — a failed message should not block the queue.
            # Dead-letter / retry logic can be layered on later.
            await r.xack(stream, group, msg_id)


async def run_worker() -> None:
    """
    Main worker loop.

    Connects to Redis and reads from the consumer group in an infinite
    loop, blocking for up to 5 seconds between polls.
    """
    settings = get_settings()
    r = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=10,      # must exceed XREADGROUP block time (5 s)
        socket_connect_timeout=5,
    )

    stream = settings.redis_stream
    group = settings.redis_consumer_group
    consumer = settings.redis_consumer_name

    # Ensure group exists
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    logger.info("⚙️  Worker '%s' listening on stream '%s' …", consumer, stream)

    try:
        while True:
            # XREADGROUP blocks for up to 5 000 ms, returns new messages
            messages = await r.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=5,
                block=5000,
            )
            if not messages:
                continue

            for _stream_name, entries in messages:
                tasks = [
                    _handle_message(r, stream, group, msg_id, fields)
                    for msg_id, fields in entries
                ]
                await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("🛑 Worker shutting down …")
    finally:
        await r.aclose()
