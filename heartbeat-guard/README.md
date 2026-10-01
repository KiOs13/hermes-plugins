# heartbeat-guard

A Hermes Agent plugin that raises subagent stale-heartbeat thresholds without
touching — or breaking — Hermes core.

---

## What it does

Hermes supervises subagent processes via a 30-second heartbeat loop in
`tools/delegate_tool.py`. Two module-level constants control when a child is
declared stale and killed:

| Constant | Core default | Timeout at 30s heartbeat |
|---|---|---|
| `_HEARTBEAT_STALE_CYCLES_IDLE` | 15 | **450 s** |
| `_HEARTBEAT_STALE_CYCLES_IN_TOOL` | 40 | **1 200 s** |

450 s is enough for most tasks. It is not enough for slow model providers,
large file operations, or tool calls that legitimately run for 10–30 minutes.
When the threshold is crossed Hermes silently abandons a child that is still
alive, losing all its work.

This plugin patches both constants to safer values at startup.

---

## Why not edit core?

`hermes update` overwrites `tools/delegate_tool.py`. Any manual patch is gone
on the next update. The plugin survives updates because it is a module-attribute
assignment — not a file edit — applied once at load time. Crucially,
`tools/delegate_tool_child_run.py` **re-imports** these constants on every
heartbeat tick inside `tick()`, so the patched value is picked up with no
core change required.

---

## How it works

```python
# __init__.py (entire implementation)
import tools.delegate_tool as dt
idle = int(os.environ.get("HERMES_STALE_CYCLES", "60"))
dt._HEARTBEAT_STALE_CYCLES_IDLE = idle
dt._HEARTBEAT_STALE_CYCLES_IN_TOOL = idle * 2
```

`register(ctx)` is called once by Hermes `PluginManager` during startup. After
that the module attribute is the live value; the child heartbeat loop reads it
fresh on every tick.

---

## Installation

1. Copy this directory into `~/.hermes/plugins/`:

   ```sh
   cp -r heartbeat-guard-plugin ~/.hermes/plugins/heartbeat-guard
   ```

2. Enable it in `~/.hermes/config.yaml`:

   ```yaml
   plugins:
     enabled:
       - heartbeat-guard
   ```

3. Restart Hermes (or reload plugins). You should see a `WARNING` log line:

   ```
   heartbeat-guard: stale cycles idle=60 in_tool=120 (heartbeat 30s -> 1800s/3600s)
   ```

---

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `HERMES_STALE_CYCLES` | `60` | Idle stale cycles. Timeout = value × 30 s. |

In-tool threshold is always `2 × HERMES_STALE_CYCLES`.

**Arithmetic:**

```
HERMES_STALE_CYCLES=60  →  idle timeout = 60 × 30 s = 1 800 s (30 min)
                            in-tool timeout = 120 × 30 s = 3 600 s (60 min)

HERMES_STALE_CYCLES=120 →  idle timeout = 120 × 30 s = 3 600 s (60 min)
                            in-tool timeout = 240 × 30 s = 7 200 s (2 h)
```

Set it in `~/.hermes/config.yaml` under `environment:`, or export before
starting Hermes.

---

## Verification

`verify_heartbeat_guard.py` prints the thresholds before and after the plugin
is applied. Run it with the Hermes venv interpreter, from the Hermes core
checkout — the core modules need `ruamel.yaml` and resolve siblings by path:

```sh
cd ~/.hermes/hermes-agent && ./venv/bin/python /path/to/verify_heartbeat_guard.py
```

Expected:

```
heartbeat-guard: stale cycles idle=60 in_tool=120 (heartbeat 30s -> 1800s/3600s)
before: 15 40
after:  60 120
```

Other numbers mean the plugin is not being applied, or `HERMES_STALE_CYCLES` is
set to something other than the default.

Note that a bare `python -c "import tools.delegate_tool"` will still print
`15 40` even with the plugin installed: plugin discovery is lazy and runs on
first hook delivery, not at import. Use the script above.

---

## Caveats

- **Child threshold only.** This patches the subagent heartbeat stale counter.
  It does not affect the per-call provider stale timeout (`PROVIDER_TIMEOUT` or
  equivalent) — that is a separate, unrelated mechanism.
- **HTTP 200 with empty content.** Some providers return a successful status
  code with an empty or truncated body. This plugin does not fix that; the
  child process appears to be working but produces no output regardless of the
  stale cycle count.
- **Core internals.** Relies on `_HEARTBEAT_STALE_CYCLES_IDLE` and
  `_HEARTBEAT_STALE_CYCLES_IN_TOOL` being module-level names in
  `tools/delegate_tool.py`, and on `delegate_tool_child_run.py` re-reading them
  per tick. If Hermes refactors this into a frozen dataclass or reads constants
  once at import time, the plugin will log a warning and become a no-op
  (it wraps the patch in `try/except`).

---

## Compatibility

Verified on:

- **Hermes Agent v0.21.5+5369.g34f8ec3** (build 2026.9.24) and v0.21.5+4636.g99721dc
- Python 3.14.7

---

## Plugin manifest fields (plugin.yaml)

```yaml
name: heartbeat-guard
version: 1.0.0
description: Raise subagent stale-heartbeat thresholds so slow-but-alive children are not abandoned at 450s.
```
