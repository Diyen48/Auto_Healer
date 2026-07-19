"""
Buggy Application — simulates a crashing service.

When run, this script:
    1. Attempts to perform a calculation that crashes.
    2. Captures the full stack trace.
    3. Sends a structured CrashAlert to the Sentinel webhook.

This represents a production microservice that has instrumentation
to report crashes to the Sentinel pipeline.

Usage:
    python buggy_app.py
"""

import logging
import traceback
import requests

# Set up our dummy server log
logging.basicConfig(filename="server.log", level=logging.ERROR)
logger = logging.getLogger(__name__)

SENTINEL_WEBHOOK = "http://127.0.0.1:8000/webhook/crash"


def process_data():
    """Simulate a data processing function that crashes."""
    try:
        # BUG: Division by zero — this will crash
        denominator = 0
        result = 0 if denominator == 0 else 100 / denominator
        return result
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()

        logger.error("CRASH: %s\n%s", error_msg, tb)

        # Send a rich crash alert to Sentinel
        payload = {
            "error": error_msg,
            "file": "buggy_app.py",
            "traceback": tb,
            "service": "data-processor",
        }

        print(f"🚨 CRASH: {error_msg}")
        print(f"📡 Sending alert to Sentinel at {SENTINEL_WEBHOOK} …")

        try:
            resp = requests.post(
                SENTINEL_WEBHOOK, json=payload, timeout=10
            )
            data = resp.json()
            print(f"✅ Alert accepted — Event ID: {data.get('event_id', 'N/A')}")
        except requests.ConnectionError:
            print(
                "❌ Could not reach Sentinel webhook. "
                "Is the API running? (python main.py)"
            )
        except Exception as post_err:
            print(f"❌ Failed to send alert: {post_err}")


if __name__ == "__main__":
    process_data()