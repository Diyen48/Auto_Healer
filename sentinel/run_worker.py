"""
Standalone worker entrypoint.

Usage:
    python -m sentinel.run_worker

This is used by Docker Compose to start the worker as a separate process.
"""

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
)

from sentinel.worker import run_worker  # noqa: E402

if __name__ == "__main__":
    asyncio.run(run_worker())
