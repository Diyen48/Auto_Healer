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
    """Simulate a data processing function that crashes."""
    try:
        print("[INFO] Processing data...")
        denominator = 0
        result = 100 / denominator
        return result
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()

        # Log it to the file
        logger.error("CRASH REPORT:\n%s", tb)

        print(f"[CRASH] CRASH: {error_msg}")
        print("[LOG] Logged traceback to server.log (no webhooks sent).")


if __name__ == "__main__":
    process_data()
