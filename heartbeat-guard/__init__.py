"""Raise subagent stale-heartbeat thresholds so slow-but-alive children survive.

Core defaults (15 idle cycles / 40 in-tool cycles = 450s / 1200s at the 30s
heartbeat) abandon healthy slow children. This plugin raises them at runtime;
the values are re-read fresh on every heartbeat tick, so no core edit is needed
and the patch survives `hermes update`.

Env: HERMES_STALE_CYCLES (default 60) -> idle cycles; in-tool = 2x idle.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Raise stale-activity thresholds; never break plugin loading."""
    try:
        import tools.delegate_tool as dt

        idle = max(1, int(os.environ.get("HERMES_STALE_CYCLES", "60")))
        in_tool = idle * 2
        dt._HEARTBEAT_STALE_CYCLES_IDLE = idle
        dt._HEARTBEAT_STALE_CYCLES_IN_TOOL = in_tool
        logger.warning(
            "heartbeat-guard: stale cycles idle=%d in_tool=%d "
            "(heartbeat %ss -> %ss/%ss)",
            idle, in_tool, dt._HEARTBEAT_INTERVAL,
            idle * dt._HEARTBEAT_INTERVAL, in_tool * dt._HEARTBEAT_INTERVAL,
        )
    except Exception as exc:
        logger.warning("heartbeat-guard: failed to patch stale cycles: %s", exc)