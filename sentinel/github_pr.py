"""
GitHub Pull Request automation — the safe remediation layer.

Uses PyGithub to:
    1. Read the current file from the default branch.
    2. Create a fix branch.
    3. Commit the patched code.
    4. Open a Pull Request with a structured body (root cause, fix, test results).

The agent NEVER touches live code — human engineers review and merge via
standard CI/CD workflows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from github import Github, GithubException

from sentinel.config import get_settings
from sentinel.models import CrashEvent

logger = logging.getLogger("sentinel.github_pr")


class GitHubRemediator:
    """Creates remediation Pull Requests on GitHub."""

    def __init__(self) -> None:
        self._settings = get_settings()
        if not self._settings.github_token:
            raise ValueError(
                "GITHUB_TOKEN is not set. "
                "Generate a PAT at https://github.com/settings/tokens "
                "and add it to your .env file."
            )
        self._gh = Github(self._settings.github_token)
        self._repo = self._gh.get_repo(self._settings.github_repo)

    # ── Public API ──────────────────────────────────────────────────

    async def create_remediation_pr(
        self,
        crash_event: CrashEvent,
        patched_code: str | None = None,
        patched_files: dict[str, str] | None = None,
        root_cause: str = "",
        sandbox_output: str | None = None,
    ) -> str:
        """
        Create a GitHub Pull Request with the validated fix.

        Returns the PR HTML URL.
        """
        if not patched_files:
            patched_files = {crash_event.file: patched_code or ""}

        return await asyncio.to_thread(
            self._create_pr_sync,
            crash_event,
            patched_files,
            root_cause,
            sandbox_output,
        )

    # ── Internal ────────────────────────────────────────────────────

    def _create_pr_sync(
        self,
        crash_event: CrashEvent,
        patched_files: dict[str, str],
        root_cause: str,
        sandbox_output: str | None,
    ) -> str:
        """Synchronous PR creation called inside a thread."""
        branch_name = self._make_branch_name(crash_event)
        default_branch = self._repo.default_branch

        # 1 ─ Get the latest commit SHA on the default branch
        base_ref = self._repo.get_git_ref(f"heads/{default_branch}")
        base_sha = base_ref.object.sha

        # 2 ─ Create the fix branch
        try:
            self._repo.create_git_ref(
                ref=f"refs/heads/{branch_name}", sha=base_sha
            )
            logger.info("🌿 Created branch '%s'", branch_name)
        except GithubException as exc:
            if exc.status == 422:  # branch already exists
                logger.warning("⚠️  Branch '%s' already exists.", branch_name)
            else:
                raise

        # 3 ─ Commit each patched file
        for rel_file_path, file_content in patched_files.items():
            try:
                file_contents = self._repo.get_contents(
                    rel_file_path, ref=default_branch
                )
                current_sha = file_contents.sha
            except GithubException:
                current_sha = None

            commit_message = (
                f"fix: auto-remediate {self._extract_error_type(crash_event.error)} "
                f"in {rel_file_path}\n\n"
                f"Root cause: {root_cause}\n"
                f"Sentinel event: {crash_event.event_id}"
            )

            if current_sha:
                self._repo.update_file(
                    path=rel_file_path,
                    message=commit_message,
                    content=file_content,
                    sha=current_sha,
                    branch=branch_name,
                )
            else:
                self._repo.create_file(
                    path=rel_file_path,
                    message=commit_message,
                    content=file_content,
                    branch=branch_name,
                )
            logger.info("📝 Committed fix for %s to branch '%s'", rel_file_path, branch_name)

        # 4 ─ Open the Pull Request
        pr_title = (
            f"🤖 [Sentinel] Fix {self._extract_error_type(crash_event.error)} "
            f"in `{crash_event.file}` ({len(patched_files)} file(s))"
        )
        pr_body = self._build_pr_body(crash_event, root_cause, sandbox_output)

        pr = self._repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=branch_name,
            base=default_branch,
        )
        pr.add_to_labels("sentinel-auto-fix", "needs-review")

        logger.info("🔀 PR #%d created: %s", pr.number, pr.html_url)
        return pr.html_url

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _make_branch_name(event: CrashEvent) -> str:
        """Generate a clean branch name from the crash event."""
        error_slug = re.sub(r"[^a-z0-9]+", "-", event.error.lower())[:40]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"fix/sentinel-{error_slug}-{ts}"

    @staticmethod
    def _extract_error_type(error: str) -> str:
        """Pull the exception class name from an error string."""
        # e.g. "ZeroDivisionError: division by zero" → "ZeroDivisionError"
        match = re.match(r"(\w+Error|\w+Exception)", error)
        return match.group(1) if match else "Error"

    @staticmethod
    def _build_pr_body(
        event: CrashEvent,
        root_cause: str,
        sandbox_output: str | None,
    ) -> str:
        """Build a structured PR description in Markdown."""
        sandbox_section = ""
        if sandbox_output:
            # Truncate long output
            truncated = sandbox_output[:2000]
            if len(sandbox_output) > 2000:
                truncated += "\n\n… (truncated)"
            sandbox_section = f"""
## 🧪 Sandbox Test Results

```
{truncated}
```
"""

        return f"""## 🤖 Sentinel Auto-Remediation

> This Pull Request was automatically generated by **Sentinel**, the
> autonomous SRE agent. A human engineer must review and approve before
> merging.

---

## 📋 Crash Details

| Field      | Value |
|------------|-------|
| **Event ID**  | `{event.event_id}` |
| **File**      | `{event.file}` |
| **Service**   | `{event.service}` |
| **Severity**  | `{event.severity}` |
| **Timestamp** | `{event.timestamp}` |

## 🔍 Error

```
{event.error}
```

{f"### Full Traceback" if event.traceback else ""}
{f"```{chr(10)}{event.traceback}{chr(10)}```" if event.traceback else ""}

## 🧠 Root Cause Analysis

{root_cause}
{sandbox_section}

---

> ⚠️ **Review Checklist:**
> - [ ] Root cause analysis is accurate
> - [ ] Fix addresses the root cause (not just the symptom)
> - [ ] No unintended side effects
> - [ ] Tests pass in CI
"""
