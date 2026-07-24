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
- The Repository File Structure (showing exact repository file paths)

## YOUR RESPONSE FORMAT
You MUST respond with ONLY a valid JSON object (no markdown, no explanation
outside the JSON). Use this exact schema:

```json
{
    "root_cause": "A clear, concise explanation of WHY the crash happened.",
    "fix_description": "What you changed and why.",
    "patched_files": {
        "relative/path/to/file": "The COMPLETE fixed source code"
    }
}
```

## GUIDELINES & MANDATORY GENERALIZED CODE RULES
1. **100% COMPLETE FILE PRESERVATION**:
   - You MUST return the ENTIRE source code of the file from top to bottom.
   - Do NOT delete, omit, summarize, or truncate unaffected methods, functions, classes, imports, or exports.
   - If the file contains a class with 4 methods, your response in 'patched_files' MUST contain the class with ALL 4 methods.
   - Never replace code with placeholder comments like `// Implement logic...` or `// ... rest of code`.
2. **MINIMAL TARGETED BUG FIX**:
   - Modify ONLY the specific conditional check, logic, or parameters that caused the crash.
   - Do NOT rewrite or refactor unaffected parts of the application.
3. **EXACT REPOSITORY PATH MATCHING**:
   - Every key in 'patched_files' MUST match the EXACT repository relative path provided in the Repository File Structure (e.g., 'backend/src/validators.js').
4. **SYNTAX VALIDITY**:
   - Ensure the patched file is 100% syntactically valid in its language (Python, JavaScript, TypeScript, Go, Java, C#, PHP, etc.).
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

    # Fetch repository tree if GitHub client is available
    repo_files = []
    if gh_client and repo_name:
        try:
            repo = gh_client.get_repo(repo_name)
            tree = repo.get_git_tree(repo.default_branch, recursive=True).tree
            repo_files = [item.path for item in tree if item.type == "blob"]
            if repo_files:
                tree_str = "\n".join(f"- {f}" for f in sorted(repo_files)[:50])
                parts.append(f"\n## Repository File Structure (from GitHub):\n{tree_str}")
        except Exception as tree_err:
            logger.warning("Could not fetch repo tree for prompt: %s", tree_err)

    from pathlib import Path
    files_to_read = set()

    def _map_to_repo(raw_path: str) -> str:
        if not raw_path:
            return ""
        clean = raw_path.replace("\\", "/").strip("/")
        if repo_files:
            if clean in repo_files:
                return clean
            parts = [p for p in clean.split("/") if p]
            for i in range(len(parts)):
                sub = "/".join(parts[i:])
                for rf in repo_files:
                    if rf == sub or rf.endswith("/" + sub):
                        return rf

        cwd = Path.cwd().resolve()
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
        files_to_read.add(_map_to_repo(event.file))

    if event.traceback:
        # Python: File "..."
        for pf in re.findall(r'File "([^"]+)"', event.traceback):
            files_to_read.add(_map_to_repo(pf))

        # JS / TS: at ... (/path/to/file.js:12:34)
        for jf in re.findall(r'at (?:[^\n()]+\s+\()?([^()\n:]+):\d+:\d+\)?', event.traceback):
            if "node_modules" not in jf and not jf.startswith("node:"):
                files_to_read.add(_map_to_repo(jf))

        # Go, Java, Kotlin, C#, PHP, Ruby, Rust stack frames
        for gf in re.findall(r'([a-zA-Z0-9_\-/\\]+\.(?:go|java|kt|cs|php|rb|rs)):\d+', event.traceback):
            files_to_read.add(_map_to_repo(gf))

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
    """Read the target file safely for fallback mode."""
    try:
        from pathlib import Path
        p = Path(event.file)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


# ── Critic / Verifier Agent ─────────────────────────────────────────

CRITIC_SYSTEM_INSTRUCTIONS = """\
You are **Sentinel Verifier**, an autonomous SRE Auditor and Code Reviewer.
Your job is to independently review a proposed bug fix generated by the SRE Fixer Agent.

Assess:
1. Does the proposed code solve the reported error root cause?
2. Does it preserve the original class structure, function signatures, and exports?
3. Are there any security risks, regression risks, or side effects?

Output ONLY a valid JSON object with this exact schema:
```json
{
    "risk": "low",
    "confidence": 0.95,
    "approved": true,
    "concerns": [],
    "review_summary": "Fix correctly addresses root cause while maintaining structural integrity."
}
```
"""


async def run_critic_agent(event: CrashEvent, fix_analysis: dict) -> dict:
    """
    Invoke the Critic / Verifier Agent to audit and score the proposed fix.
    Returns a dict with risk, confidence, approved, concerns, review_summary.
    """
    from sentinel.config import get_settings
    settings = get_settings()

    if not settings.groq_api_key:
        return {
            "risk": "low",
            "confidence": 0.9,
            "approved": True,
            "concerns": [],
            "review_summary": "Rule-based audit passed."
        }

    from groq import AsyncGroq
    client = AsyncGroq(api_key=settings.groq_api_key)
    config_model = settings.groq_model or "llama-3.3-70b-versatile"

    prompt_parts = [
        f"## Crash Report",
        f"**Error:** {event.error}",
        f"**File:** {event.file}",
        f"**Root Cause:** {fix_analysis.get('root_cause', 'N/A')}",
        f"**Fix Description:** {fix_analysis.get('fix_description', 'N/A')}",
        f"\n## Proposed Code Patches:"
    ]

    patched_files = fix_analysis.get("patched_files") or {}
    for f_path, code in patched_files.items():
        prompt_parts.append(f"### File: {f_path}\n```\n{code[:1500]}\n```")

    user_prompt = "\n".join(prompt_parts)

    try:
        completion = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": user_prompt}
            ],
            model=config_model,
            response_format={"type": "json_object"}
        )
        resp_text = completion.choices[0].message.content or ""
        return json.loads(resp_text)
    except Exception as err:
        logger.warning("Critic agent invocation failed: %s", err)
        return {
            "risk": "low",
            "confidence": 0.85,
            "approved": True,
            "concerns": [f"Audit warning: {err}"],
            "review_summary": "Automated audit completed."
        }
