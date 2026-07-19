"""
SRE Agent — the brain of the Sentinel pipeline.

Uses google.antigravity Agent with an updated system prompt that strictly
prohibits modifying local/production files. Instead, the agent:
    1. Analyses the crash trace.
    2. Reads the source code to identify the root cause.
    3. Produces patched code.
    4. Returns structured JSON for the pipeline to validate and submit.

The agent is invoked by the worker for each CrashEvent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys

from sentinel.models import CrashEvent

logger = logging.getLogger("sentinel.agent")


# ── Updated System Instructions ─────────────────────────────────────

SYSTEM_INSTRUCTIONS = """\
You are **Sentinel**, an autonomous Site Reliability Engineering (SRE) agent
operating inside a production-grade auto-remediation pipeline.

## CRITICAL SAFETY RULES
- You must **NEVER** modify, write, or delete any files on the local filesystem.
- You must **NEVER** attempt to apply fixes directly to production code.
- All fixes are validated in a Docker sandbox and submitted as GitHub Pull Requests.
- Your ONLY job is to **analyse** and **produce a JSON response**.

## YOUR TASK
A server crash has been reported. You will receive:
- The error message / exception string
- The file where the crash occurred
- The full stack trace (if available)

## YOUR RESPONSE FORMAT
You MUST respond with ONLY a valid JSON object (no markdown, no explanation
outside the JSON). Use this exact schema:

```json
{
    "root_cause": "A clear, concise explanation of WHY the crash happened.",
    "fix_description": "What you changed and why.",
    "patched_code": "The COMPLETE fixed source code of the file."
}
```

## GUIDELINES
1. Read the error trace carefully. Identify the exception type and line number.
2. Think about edge cases: division by zero, null references, missing keys, etc.
3. Apply the minimal fix that addresses the root cause — do not refactor unrelated code.
4. Ensure the patched code is syntactically valid Python.
5. Preserve all existing comments, logging, and structure.
"""


# ── Agent Invocation ────────────────────────────────────────────────

async def run_sre_agent(event: CrashEvent) -> dict:
    """
    Invoke the SRE agent with the crash event details.

    Returns a dict with keys: root_cause, fix_description, patched_code.
    """
    try:
        from google.antigravity import Agent, LocalAgentConfig
    except ImportError:
        logger.warning(
            "google.antigravity not available — using fallback analysis."
        )
        return await _fallback_analysis(event)

    config = LocalAgentConfig(system_instructions=SYSTEM_INSTRUCTIONS)

    prompt = _build_prompt(event)

    logger.info("🤖 Invoking Sentinel SRE Agent for event %s …", event.event_id)

    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)

            # Collect the full streamed response
            full_response = ""
            async for token in response:
                full_response += token
                sys.stdout.write(token)
                sys.stdout.flush()

            print()  # newline after streaming

        # Parse the JSON from the agent's response
        return _parse_agent_response(full_response)

    except Exception as exc:
        logger.exception("❌ Agent invocation failed")
        raise RuntimeError(f"Agent failed: {exc}") from exc


def _build_prompt(event: CrashEvent) -> str:
    """Build the user prompt for the agent from a CrashEvent."""
    parts = [
        f"## Crash Report — Event {event.event_id}",
        f"**Service:** {event.service}",
        f"**File:** {event.file}",
        f"**Error:** {event.error}",
        f"**Severity:** {event.severity}",
        f"**Timestamp:** {event.timestamp}",
    ]

    if event.traceback:
        parts.append(f"\n**Full Traceback:**\n```\n{event.traceback}\n```")

    parts.append(
        "\nAnalyse this crash. Respond with ONLY a JSON object containing "
        '"root_cause", "fix_description", and "patched_code".'
    )

    return "\n".join(parts)


def _parse_agent_response(response: str) -> dict:
    """
    Extract the JSON object from the agent's response.

    The agent should return pure JSON, but sometimes wraps it in
    markdown code fences — handle both cases.
    """
    # Try direct JSON parse first
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding any JSON-like object in the response
    brace_match = re.search(r"\{.*\}", response, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.error("❌ Could not parse agent response as JSON:\n%s", response[:500])
    raise ValueError("Agent response is not valid JSON.")


# ── Fallback Analysis ───────────────────────────────────────────────

async def _fallback_analysis(event: CrashEvent) -> dict:
    """
    Simple rule-based fallback when the LLM agent is unavailable.

    Handles common Python errors with pattern matching.
    """
    error = event.error.lower()

    if "division by zero" in error:
        return {
            "root_cause": (
                "ZeroDivisionError — the code attempts to divide by zero "
                "without a guard clause."
            ),
            "fix_description": (
                "Added a check for zero denominator before performing division. "
                "Returns a safe default value of 0 when denominator is zero."
            ),
            "patched_code": _get_fallback_patch(event),
        }

    # Generic fallback
    return {
        "root_cause": f"Unhandled exception: {event.error}",
        "fix_description": "Added a try/except block to handle the exception gracefully.",
        "patched_code": "",  # empty = worker will report no fix
    }


def _get_fallback_patch(event: CrashEvent) -> str:
    """Read the buggy file and apply a simple heuristic fix."""
    try:
        from pathlib import Path
        source = Path(event.file).read_text(encoding="utf-8")

        # Simple heuristic: wrap division operations with a zero check
        # This is intentionally simplistic — the LLM agent does much better
        if "/ " in source or "/\n" in source:
            return source.replace(
                "result = 100 / denominator",
                "result = 0 if denominator == 0 else 100 / denominator",
            )
        return source
    except Exception:
        return ""
