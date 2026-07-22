"""
Main entry point for multi-file order processing application.
Demonstrates inter-file calls: main.py -> calculator.py -> config.py
Logs crash tracebacks to server.log for log_monitor.py to detect.
"""

import logging
import sys
import traceback
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Configure logging to write error tracebacks to server.log
logging.basicConfig(
    filename="server.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.ERROR,
)
logger = logging.getLogger("buggy_multi_app")

from buggy_multi_app.calculator import calculate_order_total


def process_customer_order():
    """Simulate order processing that fails due to cross-module dependency bug."""
    try:
        print("[ORDER] Processing order: subtotal=150.00, region='US_TX' ...")
        total = calculate_order_total(150.00, "US_TX")
        print(f"[SUCCESS] Calculated order total: ${total}")
        return total
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()

        print(f"[CRASH] Multi-file Application Crashed: {error_msg}")
        print("[LOG] Writing crash traceback to server.log for log_monitor.py ...")

        # Write traceback to server.log
        logger.error("APPLICATION CRASH: %s\n%s", error_msg, tb)


if __name__ == "__main__":
    process_customer_order()
