# hermes-plugins

Small, independent [Hermes Agent](https://hermes-agent.nousresearch.com) plugins that fix and
observe things the core leaves hardcoded or invisible.

Both plugins here work the same way: they load at Hermes startup, run inside the agent process,
and **never modify Hermes core**. That matters because `hermes update` overwrites anything edited
in the core tree — a plugin lives in `~/.hermes/plugins/` and survives updates untouched.

| Plugin | What it does | Changes behaviour? |
|---|---|---|
| [`heartbeat-guard/`](heartbeat-guard/) | Raises the subagent stale-heartbeat threshold from 450 s to 1800 s so slow-but-alive children are not abandoned | yes, deliberately |
| [`api-recorder/`](api-recorder/) | Records per-model call health — empty responses, rate limits, timeouts, latency | no, observation only |

## Why these exist

Both came out of a real production problem: subagents were dying mid-task, and the cause was
invisible in the logs.

Three separate things turned out to be involved, and only one of them was guessable:

1. **The 450 s heartbeat threshold** is hardcoded in `tools/delegate_tool.py` and abandons a child
   that is still working. `heartbeat-guard` raises it.
2. **A free-tier model returned HTTP 200 with an empty `choices` array.** The gateway logged
   `status=success`. There was no error to grep for — the failure was a *success* with no content.
   Finding it meant reading 1.1 GB of call logs by hand. `api-recorder` makes it a counter.
3. **Daily rate limits** on the model provider, which surface as instant 429s that retry forever.

Point 2 is the general lesson: *a failure that is reported as success cannot be found by looking
for errors.* If you are debugging an agent that seems to stall for no reason, that is where to
look first.

## Install

Each plugin installs independently. Copy or symlink the directory you want into your Hermes
plugins folder:

```sh
git clone https://github.com/KiOs13/hermes-plugins.git
ln -s "$(pwd)/hermes-plugins/heartbeat-guard" ~/.hermes/plugins/heartbeat-guard
ln -s "$(pwd)/hermes-plugins/api-recorder"   ~/.hermes/plugins/api-recorder
```

Then enable them — a manifest is not enough on its own, `register(ctx)` is only called for
plugins listed in `plugins.enabled`:

```sh
hermes plugins enable heartbeat-guard
hermes plugins enable api-recorder
systemctl --user restart hermes-gateway
```

Verify:

```sh
hermes plugins list | grep -E 'heartbeat-guard|api-recorder'
journalctl --user -u hermes-gateway -p warning -g api-recorder
```

Each plugin's own README has details, caveats and a self-test script.

## Compatibility

Verified against **Hermes Agent v0.21.5+5369.g34f8ec3** (build 2026.9.24) on Python 3.14.7.
The plugins also passed on v0.21.5+4636.g99721dc; the patched internals they rely on were
identical across both.

Both plugins depend on module-level internals in the Hermes core tree
(`tools/delegate_tool.py` and the `post_api_request` / `api_request_error` hook payloads). If a
future Hermes refactor moves those, the plugins log a warning and no-op rather than breaking
startup — but their effect is lost, so check the WARNING lines after updating Hermes.

## License

MIT — see [LICENSE](LICENSE).
