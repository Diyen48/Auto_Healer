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
                from github import GithubIntegration
                gi = GithubIntegration(int(app_id), private_key)

                if installation_id:
                    access_token = gi.get_access_token(int(installation_id)).token
                else:
                    parts = repo_name.split("/")
                    if len(parts) == 2:
                        inst = gi.get_repo_installation(parts[0], parts[1])
                    else:
                        all_insts = list(gi.get_installations())
                        if not all_insts:
                            raise ValueError(f"No installations found for GitHub App ID {app_id}")
                        inst = all_insts[0]
                    access_token = gi.get_access_token(inst.id).token

                self._gh = Github(access_token)
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
        critic_review=None,
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
            critic_review,
        )

    # ── Internal ────────────────────────────────────────────────────

    def check_open_pr_exists(self, crash_file: str, error_type: str) -> str | None:
        """
        Check if an open remediation PR already exists for this file or error type.
        Prevents creating multiple duplicate branches for repeated crashes.
        """
        from pathlib import Path
        try:
            open_prs = self._repo.get_pulls(state="open")
            err_slug = self._extract_error_type(error_type).lower()
            file_stem = Path(crash_file).stem.lower()

            for pr in open_prs:
                ref_name = pr.head.ref.lower()
                title_name = pr.title.lower()
                if ref_name.startswith("fix/sentinel-"):
                    if err_slug in ref_name or file_stem in title_name or file_stem in ref_name:
                        logger.info("🛡️ Found existing open PR #%d: %s", pr.number, pr.html_url)
                        return pr.html_url
        except Exception as err:
            logger.warning("Could not check existing open PRs: %s", err)
        return None

    def _create_pr_sync(
        self,
        crash_event: CrashEvent,
        patched_files: dict[str, str],
        root_cause: str,
        sandbox_output: str | None,
        critic_review=None,
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
        resolved_patched_files = {}
        for rel_file_path, file_content in patched_files.items():
            target_repo_path = self._resolve_target_repo_path(rel_file_path, default_branch)
            resolved_patched_files[target_repo_path] = file_content

        for target_repo_path, file_content in resolved_patched_files.items():
            current_sha = None
            orig_content = None
            try:
                file_contents = self._repo.get_contents(
                    target_repo_path, ref=default_branch
                )
                current_sha = file_contents.sha
                orig_content = file_contents.decoded_content.decode("utf-8", errors="replace")
            except GithubException:
                pass

            # Safety Shield: Prevent LLM truncation of un-edited methods/classes
            final_content = self._ensure_full_file_integrity(orig_content, file_content)

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
                    content=final_content,
                    sha=current_sha,
                    branch=branch_name,
                )
            else:
                self._repo.create_file(
                    path=target_repo_path,
                    message=commit_message,
                    content=final_content,
                    branch=branch_name,
                )
            logger.info("📝 Committed fix for %s to branch '%s'", target_repo_path, branch_name)

        # 4 ─ Open the Pull Request
        pr_title = (
            f"fix: auto-remediate {self._extract_error_type(crash_event.error)} "
            f"in {list(resolved_patched_files.keys())[0]}"
        )
        pr_body = self._build_pr_body(crash_event, root_cause, sandbox_output, critic_review)

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
        Dynamically map a container/local path to the actual file path inside the target GitHub repository
        using full recursive git tree suffix matching.
        E.g., '/app/src/validators.js' -> 'backend/src/validators.js'
        """
        clean_path = rel_file_path.replace("\\", "/").strip("/")

        # 1. Fetch full repo tree once
        repo_files = []
        try:
            tree = self._repo.get_git_tree(ref, recursive=True).tree
            repo_files = [item.path for item in tree if item.type == "blob"]
        except Exception as err:
            logger.warning("Could not fetch git tree: %s", err)

        if not repo_files:
            return clean_path

        # 2. Direct match
        if clean_path in repo_files:
            return clean_path

        # 3. Match by suffix (e.g. 'src/validators.js' or 'validators.js')
        parts = [p for p in clean_path.split("/") if p]
        for i in range(len(parts)):
            sub_path = "/".join(parts[i:])
            for rf in repo_files:
                if rf == sub_path or rf.endswith("/" + sub_path):
                    logger.info("🎯 Dynamic target repo tree matched: '%s' -> '%s'", rel_file_path, rf)
                    return rf

        return clean_path

    @staticmethod
    def _ensure_full_file_integrity(original_content: str | None, patched_content: str) -> str:
        """
        Ensures that if the patched_content provided by LLM accidentally omitted unaffected methods/functions
        from original_content, the fixed method is cleanly spliced back into original_content.
        """
        if not original_content or not patched_content:
            return patched_content or original_content or ""

        orig_lines = original_content.splitlines()
        patch_lines = patched_content.splitlines()

        # If patch is smaller than 60% of original file and original file is larger than 20 lines
        if len(patch_lines) < 0.6 * len(orig_lines) and len(orig_lines) > 20:
            logger.info("🛡️ Patch integrity check triggered: Patch has %d lines vs Original %d lines. Splicing fix into original file...", len(patch_lines), len(orig_lines))

            fn_names = re.findall(r'(?:def|function|async\s+function|\b)(\w+)\s*\(', patched_content)
            keywords = {"if", "for", "while", "switch", "catch", "return", "require", "import", "class"}
            fn_names = [fn for fn in fn_names if fn not in keywords]

            patched_code_final = original_content
            for fn in fn_names:
                orig_pattern = re.compile(rf'(\b{fn}\s*\([^)]*\)\s*\{{[\s\S]*?\n\s*\}}|\bdef\s+{fn}\s*\([^)]*\):[\s\S]*?(?=\ndef|\nclass|\Z))')
                patch_pattern = re.compile(rf'(\b{fn}\s*\([^)]*\)\s*\{{[\s\S]*?\n\s*\}}|\bdef\s+{fn}\s*\([^)]*\):[\s\S]*?(?=\ndef|\nclass|\Z))')

                orig_match = orig_pattern.search(original_content)
                patch_match = patch_pattern.search(patched_content)

                if orig_match and patch_match:
                    logger.info("🎯 Splicing fixed method '%s' into original file structure...", fn)
                    patched_code_final = patched_code_final.replace(orig_match.group(1), patch_match.group(1))

            return patched_code_final

        return patched_content

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
        critic_review=None,
    ) -> str:
        """Build a structured PR description in Markdown."""
        sandbox_section = ""
        if sandbox_output:
            truncated = sandbox_output[:2000]
            if len(sandbox_output) > 2000:
                truncated += "\n\n… (truncated)"
            sandbox_section = f"""
## 🧪 Sandbox Test Results

```
{truncated}
```
"""

        critic_section = ""
        if critic_review:
            risk_val = getattr(critic_review, "risk", "low")
            conf_val = getattr(critic_review, "confidence", 0.95)
            appr_val = getattr(critic_review, "approved", True)
            concerns = getattr(critic_review, "concerns", [])
            summary = getattr(critic_review, "review_summary", "Audit completed.")

            badge = "🟢 LOW RISK" if risk_val == "low" else ("🟡 MEDIUM RISK" if risk_val == "medium" else "🔴 HIGH RISK")
            concerns_text = "\n".join(f"- {c}" for c in concerns) if concerns else "- None"

            critic_section = f"""
## 🧐 Multi-Agent Verifier Evaluation

| Metric | Value |
|--------|-------|
| **Risk Level** | `{badge}` |
| **Verifier Confidence** | `{int(conf_val * 100)}%` |
| **Audit Status** | `{"APPROVED ✅" if appr_val else "NEEDS REVIEW ⚠️"}` |

**Verifier Summary:** {summary}

**Identified Concerns:**
{concerns_text}
"""

        return f"""## 🤖 Sentinel Auto-Remediation (Multi-Agent System)

> This Pull Request was automatically generated by **Sentinel**, an autonomous
> multi-agent SRE system (Fixer Agent + Verifier Agent). A human engineer must review and approve before merging.

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

{critic_section}
{sandbox_section}

---

> ⚠️ **Review Checklist:**
> - [ ] Root cause analysis is accurate
> - [ ] Fix addresses the root cause (not just the symptom)
> - [ ] No unintended side effects
> - [ ] Tests pass in CI
"""
