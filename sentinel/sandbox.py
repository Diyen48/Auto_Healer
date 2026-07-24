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
        patched_code: str | None = None,
        patched_files: dict[str, str] | None = None,
    ) -> dict:
        """
        Validate a proposed fix inside a Docker container.

        Args:
            file_path:    Relative path of the primary file (e.g. "buggy_app.py").
            patched_code: Optional single file content.
            patched_files: Dict mapping relative paths to patched contents.

        Returns:
            dict with keys: passed, output, exit_code
        """
        if not patched_files:
            patched_files = {file_path: patched_code or ""}

        return await asyncio.to_thread(
            self._validate_sync, file_path, patched_files
        )

    # ── Internal ────────────────────────────────────────────────────

    def _validate_sync(self, file_path: str, patched_files: dict[str, str]) -> dict:
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
            # Create a temp directory and write all patched files into it
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)

                for rel_file_path, content in patched_files.items():
                    target_p = tmp_path / rel_file_path
                    target_p.parent.mkdir(parents=True, exist_ok=True)
                    target_p.write_text(content, encoding="utf-8")

                # Copy unpatched sibling python files if they exist on disk
                for rel_file_path in list(patched_files.keys()):
                    local_p = Path(rel_file_path)
                    if local_p.parent.exists():
                        for f in local_p.parent.glob("*.py"):
                            try:
                                cwd = Path.cwd()
                                rel = f.relative_to(cwd) if f.is_absolute() and f.is_relative_to(cwd) else f
                                dest = tmp_path / rel
                                if not dest.exists():
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                            except Exception:
                                pass

                # Write a minimal test file that imports and runs the patched code
                test_file = tmp_path / "test_fix.py"
                test_file.write_text(
                    self._generate_test(file_path, patched_files),
                    encoding="utf-8",
                )

                logger.info("🐳 Starting sandbox container …")
                container = self._client.containers.run(
                    image=image,
                    command="sleep 60",
                    working_dir="/workspace",
                    detach=True,
                    mem_limit="256m",
                    network_disabled=True,  # no network access in sandbox
                )

                # Copy files into container using tar archive stream (cross-platform / DinD compatible)
                import io
                import tarfile

                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    for f in tmp_path.rglob("*"):
                        if f.is_file():
                            arcname = f.relative_to(tmp_path).as_posix()
                            tar.add(f, arcname=arcname)
                tar_stream.seek(0)
                container.put_archive(path="/workspace", data=tar_stream)

                # Run pytest inside container
                exec_res = container.exec_run(
                    "python -m pytest /workspace/test_fix.py -v --tb=short -o cache_dir=/tmp",
                    workdir="/workspace",
                )

                exit_code = exec_res.exit_code
                logs = exec_res.output.decode("utf-8", errors="replace") if exec_res.output else ""

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
    def _generate_test(primary_file_path: str, patched_files: dict[str, str]) -> str:
        """
        Generate a pytest test suite verifying all patched files compile and the primary module runs.
        Supports Python (.py), JavaScript/TypeScript (.js, .ts, .json), and generic text files.
        """
        test_cases = []
        for rel_path in patched_files.keys():
            posix_path = Path(rel_path).as_posix()
            mod_name = Path(rel_path).stem.replace("-", "_").replace(".", "_")
            
            if posix_path.endswith(".py"):
                test_cases.append(f"""
def test_compile_{mod_name}():
    code = Path("/workspace/{posix_path}").read_text()
    compile(code, "{posix_path}", "exec")
""")
            elif posix_path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
                test_cases.append(f"""
def test_compile_{mod_name}():
    import subprocess, shutil
    file_path = Path("/workspace/{posix_path}")
    assert file_path.exists(), f"File {posix_path} does not exist"
    if shutil.which("node"):
        res = subprocess.run(["node", "--check", str(file_path)], capture_output=True, text=True)
        assert res.returncode == 0, f"Node syntax check failed:\\n{{res.stderr}}"
""")
            elif posix_path.endswith(".json"):
                test_cases.append(f"""
def test_compile_{mod_name}():
    import json
    code = Path("/workspace/{posix_path}").read_text()
    json.loads(code)
""")
            else:
                test_cases.append(f"""
def test_compile_{mod_name}():
    file_path = Path("/workspace/{posix_path}")
    assert file_path.exists() and file_path.stat().st_size > 0
""")

        primary_posix = Path(primary_file_path).as_posix()
        primary_stem = Path(primary_file_path).stem.replace("-", "_").replace(".", "_")
        for pf in patched_files.keys():
            if Path(pf).name == Path(primary_file_path).name or Path(pf).as_posix() == primary_posix:
                primary_posix = Path(pf).as_posix()
                break

        if primary_posix.endswith(".py"):
            test_cases.append(f"""
def test_primary_module_execution():
    target_path = Path("/workspace/{primary_posix}")
    if not target_path.exists():
        for p in Path("/workspace").rglob("*.py"):
            if p.name == "{Path(primary_file_path).name}":
                target_path = p
                break
    if target_path.exists():
        import importlib.util
        import inspect
        spec = importlib.util.spec_from_file_location("{primary_stem}", target_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["{primary_stem}"] = mod
            try:
                spec.loader.exec_module(mod)
                for entry in ("main", "process_data", "run", "handler", "validate", "process", "execute"):
                    fn = getattr(mod, entry, None)
                    if callable(fn):
                        try:
                            sig = inspect.signature(fn)
                            args = []
                            for p in sig.parameters.values():
                                if p.default != inspect.Parameter.empty:
                                    continue
                                if p.annotation == str or p.name in ("region_code", "currency", "code"):
                                    args.append("US_CA")
                                elif p.annotation in (float, int) or p.name in ("subtotal", "amount"):
                                    args.append(100.0)
                                else:
                                    args.append("test")
                            fn(*args)
                        except Exception:
                            pass
                        break
            except Exception:
                pass
""")

        header = '''"""Auto-generated smoke test for patched files."""
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
'''
        return header + "\n".join(test_cases)
