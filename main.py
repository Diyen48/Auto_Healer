"""
Sentinel Pipeline — main entrypoint.

Runs both the FastAPI ingestion API and the background Redis worker
concurrently using asyncio.

Usage:
    python main.py
"""

import asyncio
import logging

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
)

logger = logging.getLogger("sentinel")


async def main():
    """Start the API server and the background worker concurrently."""
    from sentinel.config import get_settings
    from sentinel.worker import run_worker

    settings = get_settings()

    # Configure uvicorn to run as an async server
    config = uvicorn.Config(
        "sentinel.api:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    logger.info("=" * 60)
    logger.info("  🛡️  SENTINEL — Auto-Remediation Pipeline")
    logger.info("=" * 60)
    logger.info("  API:    http://%s:%d", settings.api_host, settings.api_port)
    logger.info("  Redis:  %s", settings.redis_url)
    logger.info("  GitHub: %s", settings.github_repo or "(not configured)")
    logger.info("=" * 60)

    # Run both concurrently — if either crashes, both shut down
    await asyncio.gather(
        server.serve(),
        run_worker(),
    )


if __name__ == "__main__":
    asyncio.run(main())
