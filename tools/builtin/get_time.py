"""
tools/builtin/get_time.py

Simple, dependency-free tool -- the current UTC date/time. Useful for
grounding "as of today" or "current events" style claims during research
passes without needing a network call.
"""

from datetime import datetime, timezone

TOOL_SCHEMA = {
    "name": "get_time",
    "description": "Get the current date and time (UTC).",
    "input_schema": {"type": "object", "properties": {}},  # takes no arguments
}


def run(params: dict) -> dict:
    now = datetime.now(timezone.utc)
    return {"utc_datetime": now.isoformat()}
