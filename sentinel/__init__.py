"""
Sentinel — Event-Driven Auto-Remediation Pipeline.

An autonomous SRE agent that ingests crash events via Redis Streams,
validates fixes in Docker sandboxes, and submits GitHub Pull Requests.
"""

__version__ = "1.0.0"
