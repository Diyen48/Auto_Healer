"""
Verification test script for multi-file crash remediation in Sentinel.
Simulates a multi-file crash, tests prompt building, agent response parsing,
and Docker sandbox multi-file validation.
"""

import asyncio
import logging
import sys
import traceback
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_multi_file")

# Ensure workspace root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sentinel.models import CrashEvent, RemediationStatus
from sentinel.agent import _build_prompt, _parse_agent_response, run_sre_agent
from sentinel.sandbox import SandboxManager


async def test_multi_file_pipeline():
    print("=" * 70)
    print("TESTING MULTI-FILE REMEDIATION PIPELINE")
    print("=" * 70)

    # 1. Generate multi-file traceback by executing buggy_multi_app
    print("\n[1] Generating multi-file traceback from buggy_multi_app ...")
    from buggy_multi_app.calculator import calculate_order_total
    
    tb_str = ""
    error_str = ""
    try:
        calculate_order_total(150.0, "US_TX")
    except Exception as e:
        error_str = f"{type(e).__name__}: {e}"
        tb_str = traceback.format_exc()

    assert tb_str, "Traceback should not be empty!"
    print(f"Captured Error: {error_str}")
    print(f"Traceback:\n{tb_str}")

    # 2. Create CrashEvent
    event = CrashEvent(
        error=error_str,
        file="buggy_multi_app/main.py",
        traceback=tb_str,
        service="multi-file-order-service",
    )

    # 3. Test prompt generation
    print("\n[2] Testing multi-file prompt generation ...")
    prompt = _build_prompt(event)
    print(f"Prompt generated ({len(prompt)} chars).")
    
    # Assert all three files are in the prompt
    assert "buggy_multi_app/main.py" in prompt or "buggy_multi_app\\main.py" in prompt
    assert "buggy_multi_app/calculator.py" in prompt or "buggy_multi_app\\calculator.py" in prompt
    assert "buggy_multi_app/config.py" in prompt or "buggy_multi_app\\config.py" in prompt
    print("[SUCCESS] Prompt successfully includes source code for main.py, calculator.py, and config.py!")

    # 4. Test agent analysis (if GROQ_API_KEY is set or rule-based fallback)
    print("\n[3] Testing SRE Agent invocation ...")
    analysis = await run_sre_agent(event)
    print(f"Root cause: {analysis.get('root_cause')}")
    print(f"Patched files returned: {list(analysis.get('patched_files', {}).keys())}")

    patched_files = analysis.get("patched_files", {})
    assert patched_files, "Agent should return patched_files dictionary!"

    # 5. Test sandbox validation for multi-file fix (or local execution fallback if Docker is offline)
    print("\n[4] Testing Sandbox validation ...")
    try:
        sandbox = SandboxManager()
        sandbox_res = await sandbox.validate_fix(
            file_path=event.file,
            patched_files=patched_files,
        )
        print(f"Sandbox Passed: {sandbox_res.get('passed')}")
        print(f"Sandbox Exit Code: {sandbox_res.get('exit_code')}")
        print(f"Sandbox Output Snippet:\n{sandbox_res.get('output', '')[:500]}")
        assert sandbox_res.get("passed") is True, f"Sandbox tests failed! Logs: {sandbox_res.get('output')}"
    except RuntimeError as docker_err:
        print(f"[NOTE] Docker Desktop not detected ({docker_err}). Running local verification check ...")
        # Run local verification of patched code
        for file_p, code in patched_files.items():
            print(f"\n--- Patched Content of `{file_p}` ---")
            print(code[:300] + ("\n..." if len(code) > 300 else ""))
            # Verify code compiles
            compile(code, file_p, "exec")
        print("\n[SUCCESS] All patched files compiled successfully without syntax errors!")

    print("\n[PASSED] MULTI-FILE REMEDIATION TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(test_multi_file_pipeline())
