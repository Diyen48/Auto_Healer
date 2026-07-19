"""
Docker Sandbox Manager — validates proposed fixes in isolated containers.

Uses the Docker SDK for Python to:
    1. Spin up an ephemeral container replicating the target environment.
    2. Inject the patched source file.
    3. Run pytest inside the container.
    4. Capture results and tear down the container.

The container is time-bounded (killed after SANDBOX_TIMEOUT_SECONDS) to
prevent runaway processes.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound

from sentinel.config import get_settings

logger = logging.getLogger("sentinel.sandbox")


class SandboxManager:
    """Manages ephemeral Docker containers for fix validation."""

    def __init__(self) -> None:
        self._settings = get_settings()
        try:
            self._client = docker.from_env()
            self._client.ping()
        except DockerException as exc:
            logger.error("🐳 Docker is not available: %s", exc)
            raise RuntimeError(
                "Docker Desktop must be running to use the sandbox."
            ) from exc

    # ── Public API ──────────────────────────────────────────────────

    async def validate_fix(
        self,
        file_path: str,
        patched_code: str,
    ) -> dict:
        """
        Validate a proposed fix inside a Docker container.

        Args:
            file_path:    Relative path of the file to patch (e.g. "buggy_app.py").
            patched_code: The full contents of the fixed file.

        Returns:
            dict with keys:
                passed (bool):  Whether pytest exited 0.
                output (str):   Combined stdout/stderr from the container.
                exit_code (int): Raw exit code.
        """
        # Run the blocking Docker SDK calls in a thread executor so we
        # don't block the async event loop.
        return await asyncio.to_thread(
            self._validate_sync, file_path, patched_code
        )

    # ── Internal ────────────────────────────────────────────────────

    def _validate_sync(self, file_path: str, patched_code: str) -> dict:
        """Synchronous implementation called inside a thread."""
        image = self._settings.sandbox_image
        timeout = self._settings.sandbox_timeout_seconds

        # Ensure the sandbox image exists
        try:
            self._client.images.get(image)
        except ImageNotFound:
            logger.info(
                "🐳 Sandbox image '%s' not found — building from Dockerfile …",
                image,
            )
            self._build_sandbox_image(image)

        container = None
        try:
            # Create a temp directory and write the patched file into it
            with tempfile.TemporaryDirectory() as tmp_dir:
                patched_file = Path(tmp_dir) / Path(file_path).name
                patched_file.write_text(patched_code, encoding="utf-8")

                # Write a minimal test file that imports and runs the patched code
                test_file = Path(tmp_dir) / "test_fix.py"
                test_file.write_text(
                    self._generate_test(file_path, patched_code),
                    encoding="utf-8",
                )

                logger.info("🐳 Starting sandbox container …")
                container = self._client.containers.run(
                    image=image,
                    command=f"python -m pytest /workspace/test_fix.py -v --tb=short",
                    volumes={
                        tmp_dir: {"bind": "/workspace", "mode": "ro"},
                    },
                    working_dir="/workspace",
                    detach=True,
                    mem_limit="256m",
                    network_disabled=True,  # no network access in sandbox
                    user="1000:1000",        # non-root
                )

                # Wait for the container to finish (with timeout)
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
                logs = container.logs(stdout=True, stderr=True).decode(
                    "utf-8", errors="replace"
                )

            logger.info(
                "🐳 Sandbox finished (exit_code=%d). Output:\n%s",
                exit_code, logs[:500],
            )
            return {
                "passed": exit_code == 0,
                "output": logs,
                "exit_code": exit_code,
            }

        except Exception as exc:
            logger.exception("❌ Sandbox execution failed")
            return {
                "passed": False,
                "output": str(exc),
                "exit_code": -1,
            }
        finally:
            if container:
                try:
                    container.remove(force=True)
                    logger.info("🐳 Sandbox container removed.")
                except Exception:
                    pass

    def _build_sandbox_image(self, tag: str) -> None:
        """Build the sandbox Docker image from docker/Dockerfile.sandbox."""
        dockerfile_dir = Path(__file__).resolve().parent.parent / "docker"
        if not (dockerfile_dir / "Dockerfile.sandbox").exists():
            raise FileNotFoundError(
                f"Cannot build sandbox image — {dockerfile_dir / 'Dockerfile.sandbox'} "
                "not found. Run from the project root."
            )
        logger.info("🐳 Building sandbox image '%s' …", tag)
        self._client.images.build(
            path=str(dockerfile_dir),
            dockerfile="Dockerfile.sandbox",
            tag=tag,
            rm=True,
        )
        logger.info("🐳 Sandbox image '%s' built successfully.", tag)

    @staticmethod
    def _generate_test(file_path: str, patched_code: str) -> str:
        """
        Generate a minimal pytest test that verifies the patched code
        can be imported and executed without raising exceptions.
        """
        module_name = Path(file_path).stem  # e.g. "buggy_app"
        return f'''"""Auto-generated smoke test for the patched file."""
import importlib.util
import sys
from pathlib import Path


def _load_module(name: str, path: str):
    """Dynamically load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_patched_code_does_not_crash():
    """The patched file must be importable without raising exceptions."""
    mod = _load_module("{module_name}", "/workspace/{Path(file_path).name}")
    # If the module has a main callable, invoke it
    for entry in ("main", "process_data", "run", "handler"):
        fn = getattr(mod, entry, None)
        if callable(fn):
            try:
                fn()
            except SystemExit:
                pass  # allow sys.exit() calls
            break


def test_patched_code_syntax():
    """The patched code must compile without SyntaxError."""
    code = Path("/workspace/{Path(file_path).name}").read_text()
    compile(code, "{file_path}", "exec")
'''
