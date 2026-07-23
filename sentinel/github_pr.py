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

from github import Auth, Github, GithubException, GithubIntegration

from sentinel.config import get_settings
from sentinel.models import CrashEvent

logger = logging.getLogger("sentinel.github_pr")


class GitHubRemediator:
    """Creates remediation Pull Requests on GitHub."""

    def __init__(
        self,
        token_override: str | None = None,
        repo_override: str | None = None,
        app_id_override: str | None = None,
        private_key_override: str | None = None,
        installation_id_override: str | None = None,
    ) -> None:
        self._settings = get_settings()

        repo_name = (repo_override or self._settings.github_repo).strip()
        if "github.com/" in repo_name:
            repo_name = repo_name.split("github.com/")[-1].replace(".git", "").strip("/")
        elif "/" not in repo_name and "." in repo_name:
            repo_name = repo_name.replace(".", "/", 1)

        self.repo_name = repo_name

        app_id = app_id_override or self._settings.github_app_id
        private_key = private_key_override or self._settings.github_private_key
        if not private_key and self._settings.github_private_key_file:
            from pathlib import Path
            pk_path = Path(self._settings.github_private_key_file)
            if pk_path.exists():
                private_key = pk_path.read_text("utf-8")

        if private_key:
            private_key = private_key.strip().strip("'\"").replace("\r\n", "\n")
            try:
                from cryptography.hazmat.primitives import serialization
                key_obj = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
                private_key = key_obj.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ).decode("utf-8")
            except Exception as key_err:
                logger.warning("Could not re-encode private key: %s", key_err)

        installation_id = installation_id_override or self._settings.github_installation_id

        # Mode A: GitHub App SaaS Authentication (Approach C)
        authenticated = False
        if app_id and private_key:
            try:
                logger.info("🤖 Authenticating as GitHub App (App ID: %s) for repo: '%s'", app_id, repo_name)
                app_auth = Auth.AppAuth(app_id=int(app_id), private_key=private_key)

                if installation_id:
                    inst_auth = app_auth.get_installation_auth(int(installation_id))
                else:
                    gi = Github(auth=app_auth)
                    owner = repo_name.split("/")[0] if "/" in repo_name else ""
                    target_inst_id = None
                    for inst in gi.get_app().get_installations():
                        account_login = getattr(inst.account, "login", None) if hasattr(inst, "account") else None
                        if account_login == owner:
                            target_inst_id = inst.id
                            break
                    if not target_inst_id:
                        all_insts = list(gi.get_app().get_installations())
                        if all_insts:
                            target_inst_id = all_insts[0].id
                    if not target_inst_id:
                        raise ValueError(f"No installation found for GitHub App ID {app_id} for repo {repo_name}")
                    inst_auth = app_auth.get_installation_auth(target_inst_id)

                self._gh = Github(auth=inst_auth)
                self._repo = self._gh.get_repo(repo_name)
                authenticated = True
            except Exception as app_err:
                logger.warning("⚠️ GitHub App authentication failed: %s. Trying PAT fallback...", app_err)

        if not authenticated:
            # Mode B: Personal Access Token (PAT) Authentication
            token = token_override or self._settings.github_token
            if not token:
                raise ValueError(
                    "GitHub authentication failed. Neither valid GitHub App credentials nor GITHUB_TOKEN is available."
                )
            self._gh = Github(token)
            logger.info("🔑 Connecting to GitHub repository via PAT: '%s'", repo_name)
            self._repo = self._gh.get_repo(repo_name)

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
            target_repo_path = self._resolve_target_repo_path(rel_file_path, default_branch)
            try:
                file_contents = self._repo.get_contents(
                    target_repo_path, ref=default_branch
                )
                current_sha = file_contents.sha
            except GithubException:
                current_sha = None

            commit_message = (
                f"fix: auto-remediate {self._extract_error_type(crash_event.error)} "
                f"in {target_repo_path}\n\n"
                f"Root cause: {root_cause}\n"
                f"Sentinel event: {crash_event.event_id}"
            )

            if current_sha:
                self._repo.update_file(
                    path=target_repo_path,
                    message=commit_message,
                    content=file_content,
                    sha=current_sha,
                    branch=branch_name,
                )
            else:
                self._repo.create_file(
                    path=target_repo_path,
                    message=commit_message,
                    content=file_content,
                    branch=branch_name,
                )
            logger.info("📝 Committed fix for %s to branch '%s'", target_repo_path, branch_name)

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
        try:
            pr.add_to_labels("sentinel-auto-fix", "needs-review")
        except Exception:
            pass

        logger.info("🔀 PR #%d created: %s", pr.number, pr.html_url)
        return pr.html_url

    def _resolve_target_repo_path(self, rel_file_path: str, ref: str) -> str:
        """
        Dynamically map a local path to the actual file path inside the target GitHub repository.
        E.g., if local path is 'billing_app/services/currency.py', but in GitHub repo the file
        exists at 'services/currency.py', returns 'services/currency.py'.
        """
        clean_path = rel_file_path.replace("\\", "/").strip("/")

        # 1. Direct match check
        try:
            self._repo.get_contents(clean_path, ref=ref)
            return clean_path
        except GithubException:
            pass

        # 2. Check by stripping leading container directory components
        parts = [p for p in clean_path.split("/") if p]
        for i in range(1, len(parts)):
            candidate = "/".join(parts[i:])
            try:
                self._repo.get_contents(candidate, ref=ref)
                logger.info("🎯 Dynamic target repo path matched: '%s' -> '%s'", rel_file_path, candidate)
                return candidate
            except GithubException:
                pass

        return clean_path

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
