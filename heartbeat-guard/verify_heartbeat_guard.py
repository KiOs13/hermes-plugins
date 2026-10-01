#!/usr/bin/env python3
"""Show the subagent stale-heartbeat thresholds before and after heartbeat-guard.

Run with the Hermes venv interpreter and cwd set to the Hermes core checkout:

    cd ~/.hermes/hermes-agent && ./venv/bin/python /path/to/verify_heartbeat_guard.py

Expected: 'before: 15 40' then 'after:  60 120'. Other pairs mean the plugin is
not being applied, or HERMES_STALE_CYCLES is set to something else.
"""
import importlib.util
import os
import sys

home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
sys.path.insert(0, os.path.join(home, "hermes-agent"))

import tools.delegate_tool as dt  # noqa: E402

print("before:", dt._HEARTBEAT_STALE_CYCLES_IDLE, dt._HEARTBEAT_STALE_CYCLES_IN_TOOL)

plugin = os.path.join(home, "plugins", "heartbeat-guard", "__init__.py")
spec = importlib.util.spec_from_file_location("heartbeat_guard", plugin)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.register(None)

print("after: ", dt._HEARTBEAT_STALE_CYCLES_IDLE, dt._HEARTBEAT_STALE_CYCLES_IN_TOOL)