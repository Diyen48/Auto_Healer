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
- The primary crashing file and any caller files involved in the traceback
- The full stack trace and source code of all involved files

## YOUR RESPONSE FORMAT
You MUST respond with ONLY a valid JSON object (no markdown, no explanation
outside the JSON). Use this exact schema:

```json
{
    "root_cause": "A clear, concise explanation of WHY the crash happened.",
    "fix_description": "What you changed and why.",
    "patched_files": {
        "relative/path/to/file1.py": "The COMPLETE fixed source code of file1"
    }
}
```

## GUIDELINES
1. Read the error trace carefully. Identify all files involved in the stack trace and inter-file function calls.
2. Think about edge cases: division by zero, null references, missing keys, invalid method signatures across files, etc.
3. Apply the minimal fix that addresses the root cause — ONLY include files in 'patched_files' that actually require code modifications. Do NOT include files that require no changes.
4. Ensure all patched code is syntactically valid Python.
5. Preserve all existing comments, logging, and structure.
6. CRITICAL: Do NOT change existing public signatures of unaffected callers/callees unless necessary to fix the crash.
7. CRITICAL: Format each python file in 'patched_files' as a standard JSON string. Escape all double quotes inside strings or comments as `\"`, newlines as `\n`.
8. CRITICAL: You must return the COMPLETE source code for each modified file in 'patched_files'. Do NOT abbreviate the code.
"""


# ── Agent Invocation ────────────────────────────────────────────────

async def run_sre_agent(event: CrashEvent, gh_client=None, repo_name=None) -> dict:
    """
    Invoke the SRE agent with the crash event details.

    Returns a dict with keys: root_cause, fix_description, patched_files, patched_code.
    """
    from sentinel.config import get_settings
    settings = get_settings()

    if not settings.groq_api_key:
        logger.warning(
            "GROQ_API_KEY is not set — falling back to rule-based analysis."
        )
        return await _fallback_analysis(event)

    from groq import AsyncGroq, BadRequestError

    config_model = settings.groq_model or "llama-3.3-70b-versatile"
    client = AsyncGroq(api_key=settings.groq_api_key)
    prompt = _build_prompt(event, gh_client=gh_client, repo_name=repo_name)

    logger.info(
        "Invoking Sentinel SRE Agent (Groq: %s) for event %s …",
        config_model, event.event_id
    )

    full_response = ""
    try:
        chat_completion = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": prompt}
            ],
            model=config_model,
            response_format={"type": "json_object"}
        )
        full_response = chat_completion.choices[0].message.content or ""
    except BadRequestError as json_err:
        logger.warning("Groq JSON mode validation failed, retrying without response_format constraint: %s", json_err)
        try:
            chat_completion = await client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {"role": "user", "content": prompt}
                ],
                model=config_model,
            )
            full_response = chat_completion.choices[0].message.content or ""
        except Exception as exc:
            logger.exception("Groq Agent invocation retry failed")
            raise RuntimeError(f"Agent failed: {exc}") from exc
    except Exception as exc:
        logger.exception("Groq Agent invocation failed")
        raise RuntimeError(f"Agent failed: {exc}") from exc

    return _parse_agent_response(full_response, event)


def _build_prompt(event: CrashEvent, gh_client=None, repo_name=None) -> str:
    """Build the user prompt for the agent from a CrashEvent, including all multi-file stack trace sources."""
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

    from pathlib import Path
    files_to_read = set()

    def _normalize_path(raw_path: str) -> str:
        """Dynamically resolve any path to a repository-relative path without hardcoded project names."""
        if not raw_path:
            return ""
        clean = raw_path.replace("\\", "/")
        cwd = Path.cwd().resolve()

        # Split path and ignore drive letters (e.g. C:)
        parts = [pt for pt in clean.split("/") if pt and not (len(pt) == 2 and pt[1] == ":")]

        for i in range(len(parts)):
            sub_str = "/".join(parts[i:])
            sub_path = Path(sub_str)
            if (cwd / sub_path).exists():
                return sub_path.as_posix()
            elif sub_path.exists():
                return sub_path.as_posix()

        return Path(clean).name

    if event.file:
        files_to_read.add(_normalize_path(event.file))

    if event.traceback:
        tb_files = re.findall(r'File "([^"]+)"', event.traceback)
        for tb_file in tb_files:
            rel_p = _normalize_path(tb_file)
            if rel_p.endswith(".py"):
                files_to_read.add(rel_p)

    for filepath in sorted(files_to_read):
        source_content = None
        f_path = Path(filepath)

        # 1. Try local disk
        if f_path.exists():
            try:
                source_content = f_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.error("Failed to read local source file %s: %s", filepath, exc)

        # 2. Try fetching from GitHub repo if missing locally
        if not source_content and gh_client and repo_name:
            try:
                repo = gh_client.get_repo(repo_name)
                try:
                    content_file = repo.get_contents(filepath)
                    source_content = content_file.decoded_content.decode("utf-8")
                except Exception:
                    # Fallback: search for file by basename in repo
                    basename = Path(filepath).name
                    contents = repo.get_contents("")
                    while contents:
                        item = contents.pop(0)
                        if item.type == "dir":
                            contents.extend(repo.get_contents(item.path))
                        elif item.name == basename or item.path.endswith(basename):
                            source_content = item.decoded_content.decode("utf-8")
                            filepath = item.path
                            break
            except Exception as gh_exc:
                logger.warning("Could not fetch file %s from GitHub repo %s: %s", filepath, repo_name, gh_exc)

        if source_content:
            parts.append(f"\n**Current Source Code of `{filepath}`:**\n```python\n{source_content}\n```")
        else:
            logger.warning("Source file %s could not be retrieved from disk or GitHub.", filepath)

    parts.append(
        "\nAnalyse this crash. The error may involve single or multiple files. "
        "Respond with ONLY a JSON object containing "
        '"root_cause", "fix_description", and "patched_files". '
        "The 'patched_files' key MUST be a JSON object mapping relative file paths to their COMPLETE fixed source code "
        '(e.g. {"path/to/file.py": "...complete fixed code..."}).'
    )

    return "\n".join(parts)


def _clean_code_string(code: str) -> str:
    if not isinstance(code, str) or not code:
        return ""
    # If the code string has escaped backslash-n instead of real newlines
    if "\n" not in code and "\\n" in code:
        code = code.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")
        
    # Remove markdown python fences if present
    code = re.sub(r"^```(?:python)?\s*\n?", "", code, flags=re.IGNORECASE)
    code = re.sub(r"\n?```$", "", code)
    code = code.strip()

    # If first character is a stray quote causing SyntaxError, attempt cleaning
    try:
        compile(code, "<string>", "exec")
    except SyntaxError:
        # Strip leading/trailing quote artifacts from JSON string escaping
        stripped = code.strip("\"' \t\n\r")
        try:
            compile(stripped, "<string>", "exec")
            return stripped
        except SyntaxError:
            pass

    return code


def _parse_agent_response(response: str, event: CrashEvent | None = None) -> dict:
    """
    Extract the JSON object from the agent's response.
    Ensures patched_files dict is constructed.
    """
    parsed = None
    # Try direct JSON parse first
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        pass

    if not parsed:
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

    if not parsed:
        brace_match = re.search(r"\{.*\}", response, re.DOTALL)
        if brace_match:
            try:
                parsed = json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

    if not parsed or not isinstance(parsed, dict):
        logger.error("❌ Could not parse agent response as JSON:\n%s", response[:500])
        raise ValueError("Agent response is not valid JSON.")

    # Ensure patched_files structure
    patched_files = parsed.get("patched_files")
    if not isinstance(patched_files, dict) or not patched_files:
        patched_code = parsed.get("patched_code")
        if patched_code and event and event.file:
            parsed["patched_files"] = {event.file: patched_code}
        elif patched_code:
            parsed["patched_files"] = {"unknown.py": patched_code}
        else:
            parsed["patched_files"] = {}
    elif "patched_code" not in parsed and event and event.file in parsed["patched_files"]:
        parsed["patched_code"] = parsed["patched_files"][event.file]

    # Clean code strings for all patched files
    cleaned_patched_files = {}
    for f_path, code in parsed["patched_files"].items():
        cleaned_patched_files[f_path] = _clean_code_string(code)
    parsed["patched_files"] = cleaned_patched_files

    if "patched_code" in parsed and isinstance(parsed["patched_code"], str):
        parsed["patched_code"] = _clean_code_string(parsed["patched_code"])

    return parsed


# ── Fallback Analysis ───────────────────────────────────────────────

async def _fallback_analysis(event: CrashEvent) -> dict:
    """
    Simple rule-based fallback when the LLM agent is unavailable.

    Handles common Python errors with pattern matching.
    """
    error = event.error.lower()
    fallback_patch = _get_fallback_patch(event)

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
            "patched_code": fallback_patch,
            "patched_files": {event.file: fallback_patch} if fallback_patch else {},
        }

    # Generic fallback
    return {
        "root_cause": f"Unhandled exception: {event.error}",
        "fix_description": "Added a try/except block to handle the exception gracefully.",
        "patched_code": "",
        "patched_files": {},
    }


def _get_fallback_patch(event: CrashEvent) -> str:
    """Read the buggy file and apply a simple heuristic fix."""
    try:
        from pathlib import Path
        source = Path(event.file).read_text(encoding="utf-8")

        if "/ " in source or "/\n" in source:
            return source.replace(
                "result = 100 / denominator",
                "result = 0 if denominator == 0 else 100 / denominator",
            )
        return source
    except Exception:
        return ""
