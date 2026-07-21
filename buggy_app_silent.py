"""
Buggy Application (Silent) — simulates a crashing service that only logs to a file.
This represents a standard legacy application that does not have Sentinel integrations.

When run, this script:
    1. Attempts to perform a calculation that crashes.
    2. Captures the traceback.
    3. Writes it to 'server.log' using the standard logging module.
    4. Exits (with zero webhooks sent).
"""

import logging
import traceback

# Configure standard error logging to server.log
logging.basicConfig(
    filename="server.log",
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger(__name__)


def process_data():
    """Simulate a data processing function that crashes multiple times."""
    # Crash 1: KeyError
    try:
        print("[INFO] Step 1: Accessing user ID...")
        data = {}
        user_id = data["user_id"]  # Will raise KeyError
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()
        logger.error("CRASH REPORT:\n%s", tb)
        print(f"[CRASH] CRASH 1: {error_msg}")
        print("[LOG] Logged KeyError traceback to server.log.")

    # Crash 2: ZeroDivisionError
    try:
        print("[INFO] Step 2: Calculating ratio...")
        denominator = 0
        result = 100 / denominator  # Will raise ZeroDivisionError
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()
        logger.error("CRASH REPORT:\n%s", tb)
        print(f"[CRASH] CRASH 2: {error_msg}")
        print("[LOG] Logged ZeroDivisionError traceback to server.log.")


if __name__ == "__main__":
    process_data()
