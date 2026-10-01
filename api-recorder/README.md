# api-recorder

Hermes Agent plugin that **observes** model API calls and periodically reports
their health. It changes no behaviour — it is a measurement tool only.

---

## Why it exists

Two classes of failure are otherwise invisible in production:

1. **HTTP 200 with empty assistant content** — the provider returns a success
   response but the message body is blank. Hermes sees a valid reply; nothing
   in journalctl shows a problem. These accumulate silently.

2. **Daily free-tier rate limits (429)** — logged per-attempt by the error
   classifier, but not aggregated across a session or day. Without bucketed
   counts you cannot tell whether you hit the limit once or fifty times.

Additionally, stream timeouts (504) and client-aborted connections (499) often
look identical in the agent log — both surface as a retry — but have different
root causes (provider slowness vs. network interruption).

api-recorder surfaces all four as named counters per model/provider.

---

## What it measures (per model × provider bucket)

| Counter | Meaning |
|---|---|
| `total` | Every `post_api_request` + `api_request_error` event |
| `success` | `post_api_request` with content chars **or** tool calls (`assistant_tool_call_count > 0`) |
| `empty_content` | `post_api_request` with **no** content chars **and** **no** tool calls — provider answered with nothing |
| `aborted` | `post_api_request` with no content and no tool calls **and** an abort-shaped `finish_reason` (`error`/`abort`/`aborted`/`cancelled`/`canceled`/`client_abort`) — client went away mid-call. The success hook carries no `status_code`, so a real HTTP 499 can only appear via the error path; see caveat below. |
| `rate_limit` | `api_request_error` where reason contains `rate_limit` / `429` |
| `stream_timeout` | `api_request_error` where reason contains `stream_timeout` / `504` |
| `client_abort` | `api_request_error` where reason contains `client_abort` / `499` |
| `other_error` | Any other `api_request_error` |
| `latency_s` | `{n, min, avg, p95, max}` from `api_duration` (seconds); omitted if no successful timing |

Latency samples are capped at 1 000 per interval; p95 is exact within that
window. Counters are reset to zero after every flush — no cumulative totals,
no time-series database.

---

## Installation

```
cp -r /path/to/api-recorder-plugin ~/.hermes/plugins/api-recorder
```

Or symlink (survives plugin edits without re-copying):

```
ln -s "$(pwd)" ~/.hermes/plugins/api-recorder
```

Restart the Hermes service:

```
systemctl --user restart hermes-gateway
```

Verify registration:

```console
$ journalctl --user -u hermes-gateway -n 50 | grep api-recorder
... WARNING ... api-recorder: registered hooks post_api_request + api_request_error, interval=600s, state_dir=...
```

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `API_RECORDER_INTERVAL` | `600` | Flush interval in seconds (10 min). Set to `60` for quick testing. |
| `HERMES_HOME` | `~/.hermes` | Where to write JSONL files (`$HERMES_HOME/cache/api-recorder/`). |

---

### The three "no output" counters

| | Source hook | Trigger | Who caused it |
|---|---|---|---|
| `empty_content` | `post_api_request` | no content, no tool calls, normal `finish_reason` (`stop`/`length`/`tool_calls`/missing) | provider — it answered with an empty body |
| `aborted` | `post_api_request` | no content, no tool calls, abort-shaped `finish_reason` | client — connection went away mid-call |
| `client_abort` | `api_request_error` | HTTP 499 (`status_code == 499`) | client, reported as a failure |

`aborted` and `client_abort` describe the same root cause from two different
hook surfaces. They are deliberately separate: a reader must be able to see
"the client keeps hanging up" (`client_abort`, hard failures) versus "calls
return 200 but deliver nothing because the client vanished" (`aborted`).

`post_api_request` does **not** carry `status_code` (checked in
`agent/turn_response_intake.py::_fire_post_api_request_hook` and
`agent/api_request_hooks.py::_invoke_api_request_error_hook` — only the error
hook has it). So `aborted` cannot test for 499; it tests only keys that really
exist on that payload: `finish_reason`, `assistant_content_chars`,
`assistant_tool_call_count`.

---

## Reading the output

### Journal (WARNING level)

```
WARNING api-recorder: interval=600.0s buckets=2
  gpt-4o/openai: total=47 ok=44 empty=2 abrt=0 rl=1 timeout=0 cabort=0 err=0 lat={'n':44,'min':0.8,'avg':2.1,'p95':4.2,'max':7.3}
  claude-3-opus/anthropic: total=12 ok=11 empty=0 abrt=0 rl=0 timeout=1 cabort=0 err=0
```

```bash
journalctl --user -u hermes-gateway -p warning -g api-recorder
```

### JSONL file (`~/.hermes/cache/api-recorder/YYYY-MM-DD.jsonl`)

One line per flush interval. Each line is a JSON object:

```json
{"ts":"2026-10-01T12:00:00Z","interval_s":600.0,"buckets":[
  {"model":"gpt-4o","provider":"openai","total":47,"success":44,"empty_content":2,"aborted":0,"rate_limit":1,"stream_timeout":0,"client_abort":0,"other_error":0,"latency_s":{"n":44,"min":0.8,"avg":2.1,"p95":4.2,"max":7.3}},
  {"model":"claude-3-opus","provider":"anthropic","total":12,"success":11,"empty_content":0,"aborted":0,"rate_limit":0,"stream_timeout":1,"client_abort":0,"other_error":0}
]}
```

Parse with any JSON tool:

```bash
# Last flush for today
tail -1 ~/.hermes/cache/api-recorder/$(date +%Y-%m-%d).jsonl | python3 -m json.tool

# All empty-content events across all days
cat ~/.hermes/cache/api-recorder/*.jsonl | \
  python3 -c "import sys,json; [print(r) for l in sys.stdin for r in json.loads(l)['buckets'] if r['empty_content']>0]"
```

---

## Caveats

- **Observer only** — this plugin records what the hook surface reports; it
  cannot fix failures or change retry logic.
- **Provider-level visibility** — the `reason` field in `api_request_error` is
  whatever Hermes's error classifier produces. If the classifier mis-labels a
  529 as a generic error, so will this plugin.
- **Counters reset each interval** — no persistence across restarts beyond the
  JSONL file. If the service restarts mid-interval, that interval's counts are
  lost.
- **Latency is from the agent's perspective** — includes connection overhead,
  streaming time, and any Hermes-side buffering. It is not provider TTFT.
- **No memory growth** — the plugin holds at most `buckets × 1 000` latency
  floats in RAM. At 64 bytes per float that is ≤ 64 KB for ten distinct models.

---

## Self-test

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python ~/.hermes/plugins/api-recorder/__init__.py
```

Expected: `[demo] ALL ASSERTIONS PASSED` after printing the JSONL record.

Full check (registration, ingest, compile, file inventory):

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python ~/.hermes/plugins/api-recorder/verify_api_recorder.py
```

Expected: `ALL CHECKS PASSED`.

## Report

`api_recorder_report.py` reads the JSONL files and prints per-model health:

```bash
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py --hours 6
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py --with-synthetic
```

Sample output:

```
api-recorder report  |  8 intervals  |  2026-10-01T17:50:10Z .. 2026-10-01T19:12:22Z

model                             prov          tot   ok  EMPTY  429  to  499  err    avg_s  alert
--------------------------------------------------------------------------------------------
hermes-worker                     custom:omni   437  434      1    0   0    0    2     14.4  empty_content=1
gateway combo                     custom         67   67      0    0   0    0    0     10.4
review combo                      custom         39   39      0    0   0    0    0      5.6
main combo                        custom         12   12      0    0   0    0    0     10.9

total calls: 555
  success             552  (99.5%)
  empty_content         1  (0.2%)
  other_error           2  (0.4%)
```

`EMPTY`, `429` and `to` (stream timeout) are the columns worth watching — those are the
failures that are otherwise invisible. The script exits non-zero and says so plainly if the
plugin has recorded nothing, rather than printing an empty table that reads like "all fine".

Records from the plugin's own self-test are skipped by default: a test run forces a flush and
so emits a burst of records seconds apart, while real flushes are one interval (default 600 s)
or more apart. Pass `--with-synthetic` to include them.
